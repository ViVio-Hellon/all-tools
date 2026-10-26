"""通しの試験 ── **複数台・複数ライン**と直の切り替わり(端末ごとに別のプロセスを立てる)

【現場の依頼】

    日報周りの一連動作チェックお願いします

カレンダー・看板・点検表で、別プロセスの端末を何台も立てて共有フォルダだけ同じにした
通しの試験を作ったところ、番号の使い回しで他人の登録が消える不具合が出ました。日報でも
**台をまたいだときだけ起きること**をここで確かめます。1台の中の流れ(打つ・保存・
自動確定・赤い帯・過去分を送る)は `test_scenario_shift_flow.py` が1つのプロセスの中で
細かく見ています。ここでは同じ流れを**本物の起動の手順で立てた端末**で通し、

    1. 直の切り替わり(1直→2直→3直→翌日1直・日勤・残業)を、開いたままの画面がまたぐ
    2. 直の終わり: 15分前の帯・5分前の自動確定(綺麗なら共有へ)・渡さないまま終わった直
    3. 前の直が未送信・エラーのまま次の直: 赤い帯・「過去分を共有へ送る」
    4. 複数台・複数ライン: L-1 と HVC の端末、同じラインの2台目(前の直の続き・引き継ぎ)
       ── 共有の中身がそろうか、**黙ってほかの端末・ほかのラインの分を消したり
       上書きしたりしないか**。同じ直を2台が同時に共有へ保存
    5. 共有が見えないあいだの保存 → 送信待ち → 戻ったら送られる
    6. 記録を見る・呼び出し(過去の直を開く・直す・送り直す)・後日作成
    7. 月替わり

を見ます。

【どう立てるか】
端末1台 = **別々のプロセス**(本物の waitress で待ち受け、起動は `start_app._initialize`
と同じ手順 ── 配布設定 → 直の時間 → アクセス権限 → 控え(LocalBackup))。手元の領域
(`NIPPOU_LOCAL_DIR`・`NIPPOU_APP_DIR`・ホーム)は台ごとに別、共有フォルダ(日報データ・
参照用マスタ・LocalBackup・自動集計)だけ同じ所を指します。直・固定・呼び出し・締めの
見張りはプロセスに1つずつなので、1つのプロセスの中で台を入れ替えると前の台の状態を
持ち越して「本当は届いていないのに通る」が起きます。

ラインは**アクセス権限の表**で決めます(PC名ごとの行。ログイン名は全端末で同じ ──
現場と同じ形)。

【時計】
全部の端末が**同じ時計ファイル**を見ます(`Clock`)。合わせた時刻で**止まっています**
(`test_scenario_shift_flow` と同じ)。本物の秒で進めると、14:54 に合わせた直後に CI が
1秒もたつけば残りが「5分」に数えられ(切り捨て)、走らないはずの自動確定が走ります。
控えの「保存した時刻の新しいほうが正」は、端末ごとに違う時刻へ合わせてから保存して確かめます。

【待ち方】
Windows の CI は遅いので、**時間に頼らず状態を待ちます**(`wait_for`)。控えへ写すのは
保存の応答を返したあと(`call_on_close`)なので、写ったかは控えのファイルを見て待ちます。

子プロセスは `tests` パッケージを読みません(下の `_CHILD`)。本物の利用者のフォルダは
使いません(全部この試験の一時フォルダ。ホームも差し替えます)。
"""
from __future__ import annotations

import json
import os
import queue
import site
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.connection import connect_readonly  # noqa: E402
from tests._web import HAS_FLASK, SKIP_REASON, TOKEN  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

#: 子プロセスが立ち上がるのを待つ上限(秒)。Windows の CI は遅い
BOOT_SEC = 120.0
#: 状態を待つ上限(秒)
WAIT_SEC = 60.0

#: 直の時刻(現場と同じ形。3直は日をまたぐ)── 共有の伝送用ファイルの「時間用」に置く
SHIFT_TIMES = (("1", "1", "07:00", "15:00"), ("2", "2", "15:00", "22:50"),
               ("3", "3", "22:50", "07:00"), ("4", "昼", "08:00", "17:00"))

#: アクセス権限の表(管理番号, ログインID, PC名, 権限, 有効, 備考)。
#: **ログイン名は全端末で同じ**(現場の形)なので、ログインIDの欄は空にしてPC名で当てる
RIGHTS = ((1, "", "PC-L1A", "L-1", 1, "L-1 の1台目"),
          (2, "", "PC-L1B", "L-1", 1, "L-1 の2台目(引き継ぎ・予備)"),
          (3, "", "PC-HVC", "HVC", 1, ""),
          (4, "", "PC-TOT", "トット", 1, "日勤のライン"),
          (5, "", "PC-ADMIN", "Administrator", 1, "管理者"))

#: 全端末で同じログイン名
LOGIN = "genba"

#: 設備移動のため保存(一文で通す)
SKIP = "設備移動のため保存"


# ======================================================================
# 子プロセス
# ======================================================================
#: 子プロセスの中身。**tests パッケージを読まない**(冒頭の説明)。
#: 時計を差し替えてから、本物の起動の順(`start_app._initialize`)で立てる
_CHILD = r"""
import os, signal, sys, threading, time as _time
sys.path.insert(0, os.environ["FLOW_ROOT"])
from datetime import date as _real_date, datetime as _real_datetime

_CLOCK = os.environ["FLOW_CLOCK"]
_cache = {"text": None, "value": None}


def _now():
    # 毎回読む(小さいファイル)。更新時刻で覚えると、同じ秒のうちに2度合わせたとき取りこぼす
    try:
        with open(_CLOCK, encoding="utf-8") as fh:
            text = fh.read().strip()
        if text != _cache["text"]:
            _cache.update(text=text, value=_real_datetime.strptime(text, "%Y-%m-%d %H:%M:%S"))
    except (OSError, ValueError):              # 書き換えの最中に読んだ ── 前の値で
        if _cache["value"] is None:
            return _real_datetime.now()
    return _cache["value"]


class _DTMeta(type):
    def __instancecheck__(cls, obj):
        return isinstance(obj, _real_datetime)

    def __subclasscheck__(cls, sub):
        return issubclass(sub, _real_datetime)


class _DMeta(type):
    def __instancecheck__(cls, obj):
        return isinstance(obj, _real_date)

    def __subclasscheck__(cls, sub):
        return issubclass(sub, _real_date)


class FrozenDateTime(_real_datetime, metaclass=_DTMeta):
    @classmethod
    def now(cls, tz=None):
        value = _now()
        return value if tz is None else value.replace(tzinfo=tz)

    @classmethod
    def today(cls):
        return _now()


class FrozenDate(_real_date, metaclass=_DMeta):
    @classmethod
    def today(cls):
        return _now().date()


import importlib, pkgutil
for _name in ("nippou", "app"):
    _pkg = importlib.import_module(_name)
    for _info in pkgutil.walk_packages(_pkg.__path__, _name + "."):
        try:
            importlib.import_module(_info.name)
        except Exception:
            pass
import start_app, server as server_module
for _mname, _module in list(sys.modules.items()):
    if _module is None or not (_mname in ("nippou", "app", "start_app", "server")
                               or _mname.startswith(("nippou.", "app."))):
        continue
    for _attr, _real, _fake in (("datetime", _real_datetime, FrozenDateTime),
                                ("date", _real_date, FrozenDate)):
        if getattr(_module, _attr, None) is _real:
            setattr(_module, _attr, _fake)

srv = server_module.AppServer(0, token=os.environ["FLOW_TOKEN"])
thread = server_module.run_in_background(srv)
deadline = _time.monotonic() + 60
while srv._server is None or not getattr(srv._server, "effective_port", 0):
    if _time.monotonic() > deadline:
        raise SystemExit("待ち受けを始められませんでした")
    _time.sleep(0.05)
srv.build()
start_app._initialize(srv, watch_idle=False)


def _stop(*_):
    threading.Thread(target=srv.stop, daemon=True).start()


signal.signal(signal.SIGTERM, _stop)
if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, _stop)
print("PORT=%s" % srv._server.effective_port, flush=True)
while thread.is_alive():
    thread.join(0.5)
"""


class Clock:
    """全部の端末が見る時計。合わせた時刻で止まっている。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def set(self, when: str) -> None:
        """'2026-10-05 14:55' の形で合わせる。"""
        value = datetime.strptime(when, "%Y-%m-%d %H:%M")
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(f"{value:%Y-%m-%d %H:%M:%S}", encoding="utf-8")
        # Windows は、子が読んでいる最中のファイルへ置き換えられない(使用中)。離すまで待つ
        wait_for("時計を合わせる", lambda: self._replace(tmp), timeout=30)

    def _replace(self, tmp: Path) -> bool:
        try:
            os.replace(tmp, self.path)
            return True
        except PermissionError:
            return False


def wait_for(what: str, check: Callable[[], Any], timeout: float = WAIT_SEC) -> Any:
    """`check()` が真を返すまで待つ。**時間に頼らず状態を待つ。**"""
    deadline = time.monotonic() + timeout
    last: Any = None
    while True:
        try:
            last = check()
        except (OSError, sqlite3.Error) as exc:   # 共有を書いている最中に読んだ
            last = exc
        else:
            if last:
                return last
        if time.monotonic() > deadline:
            raise AssertionError(f"{what} を待ちましたが、そうなりません(最後: {last!r})")
        time.sleep(0.1)


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Terminal:
    """端末1台。別プロセスで立てて、HTTP で画面と同じ道を叩く。"""

    def __init__(self, case: "FlowCase", name: str, *, pc: str) -> None:
        self.case = case
        self.name = name
        self.pc = pc
        self.dir = case.base / "端末" / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.proc: Optional[subprocess.Popen] = None
        self.port = 0
        #: 画面が開いている直(画面の `opened`)
        self.opened: Optional[dict] = None
        self.start()
        case.addCleanup(self.stop)

    # -- 立てる・止める ----------------------------------------------------
    def env(self) -> dict:
        env = dict(os.environ)
        for key in [k for k in env if k.startswith("NIPPOU_")]:
            del env[key]
        home = self.dir / "home"
        env.update({
            "FLOW_ROOT": str(ROOT),
            "FLOW_CLOCK": str(self.case.clock.path),
            "FLOW_TOKEN": TOKEN,
            "NIPPOU_LOCAL_DIR": str(self.dir / "local"),
            "NIPPOU_APP_DIR": str(self.dir / "app"),
            "NIPPOU_DIST_DIR": str(self.dir / "配布設定"),
            "NIPPOU_SOUND_DIR": str(self.dir / "sounds"),
            "NIPPOU_ACCESS_DIR": str(self.case.shared),
            "NIPPOU_GW_REF_DIR": str(self.case.ref),
            "COMPUTERNAME": self.pc,
            "USERNAME": LOGIN,
            "HOME": str(home), "USERPROFILE": str(home),
            "LOCALAPPDATA": str(home / "AppData" / "Local"),
            # ホームを差し替えても、利用者ごとに入れたパッケージ(Flask など)は読めるように
            "PYTHONUSERBASE": site.getuserbase(),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        return env

    def start(self) -> None:
        self._stderr = open(self.dir / "stderr.txt", "ab")
        self.proc = subprocess.Popen(
            [sys.executable, "-c", _CHILD], cwd=str(self.dir), env=self.env(),
            stdout=subprocess.PIPE, stderr=self._stderr)
        lines: "queue.Queue[str]" = queue.Queue()

        def pump() -> None:
            for raw in self.proc.stdout:          # type: ignore[union-attr]
                lines.put(raw.decode("utf-8", "replace").strip())

        threading.Thread(target=pump, daemon=True).start()
        deadline = time.monotonic() + BOOT_SEC
        while True:
            try:
                text = lines.get(timeout=0.5)
            except queue.Empty:
                if self.proc.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError(f"端末 {self.name} が立ち上がりません:\n{self.stderr()}")
                continue
            if text.startswith("PORT="):
                self.port = int(text.split("=", 1)[1])
                break
        self.opened = None

    def stop(self) -> None:
        if self.proc is None:
            return
        proc, self.proc = self.proc, None
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=20)
        if proc.stdout is not None:
            proc.stdout.close()
        self._stderr.close()

    def restart(self) -> None:
        """窓を閉じて開き直す(手元の領域はそのまま)。"""
        self.stop()
        self.start()

    def stderr(self) -> str:
        try:
            return (self.dir / "stderr.txt").read_text("utf-8", "replace")[-4000:]
        except OSError:
            return ""

    # -- HTTP --------------------------------------------------------------
    def call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"X-Tool-Token": TOKEN, "Content-Type": "application/json"})
        try:
            with _OPENER.open(request, timeout=120) as response:
                raw, status = response.read(), response.status
        except urllib.error.HTTPError as exc:
            raw, status = exc.read(), exc.code
        try:
            return status, json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            return status, raw.decode("utf-8", "replace")

    def get(self, path: str) -> Any:
        status, body = self.call("GET", path)
        self.case.assertEqual(status, 200, f"{self.name} GET {path}: {body}")
        return body

    def post(self, path: str, body: Any = None) -> tuple[int, Any]:
        return self.call("POST", path, body if body is not None else {})

    def ok(self, path: str, body: Any = None) -> Any:
        status, got = self.post(path, body)
        self.case.assertEqual(status, 200, f"{self.name} POST {path}: {got}")
        return got

    # -- 画面でやること(`test_scenario_shift_flow.ScenarioCase` と同じ道) ----
    def remember(self, body: Any) -> None:
        if isinstance(body, dict) and body.get("report_date") and body.get("shift"):
            self.opened = {"report_date": body["report_date"], "shift": body["shift"],
                           "page": str(body.get("page", 1)), "line": body.get("line", "")}

    def sheet(self, rows: dict, worker: str = "", **extra) -> dict:
        body: dict = {"rows": rows, "header": {"worker": worker} if worker else {},
                      "checks": {}}
        if self.opened:
            body["opened"] = dict(self.opened)
        body.update(extra)
        return body

    def open_screen(self) -> dict:
        """日報入力を開く(描いた直がそのまま画面の直になる)。"""
        status, _ = self.call("GET", "/")
        self.case.assertEqual(status, 200)
        self.opened = None
        body = self.ok("/api/entry/state", {"rows": {}, "header": {}, "checks": {}})
        self.remember(body)
        return body

    def choose_worker(self, worker: str) -> dict:
        """「作業者を選ぶ」── ここで直が固定される。"""
        body = self.ok("/api/entry/state", self.sheet({}, worker))
        self.remember(body)
        return body

    def state(self, rows: Optional[dict] = None, worker: str = "") -> dict:
        return self.ok("/api/entry/state", self.sheet(rows or {}, worker))

    def save(self, rows: dict, worker: str, **extra) -> tuple[int, Any]:
        status, body = self.post("/api/entry/save", self.sheet(rows, worker, **extra))
        if status == 200:
            self.remember(body)
        return status, body

    def saved(self, rows: dict, worker: str, **extra) -> dict:
        status, body = self.save(rows, worker, **extra)
        self.case.assertEqual(status, 200, f"{self.name} 保存: {body}")
        return body

    def autosave(self, rows: dict, worker: str) -> tuple[int, Any]:
        return self.save(rows, worker, silent=True)

    def push_now(self, **body) -> tuple[int, Any]:
        """入力画面のふだんの「共有へ保存」── いまの直だけ。"""
        return self.post("/api/settings/push", dict(body, scope="current"))

    def push_past(self, **body) -> tuple[int, Any]:
        """赤い帯の「過去分を共有へ送る」── いまの直ではない未送信の直ぜんぶ。"""
        return self.post("/api/settings/push", dict(body, scope="previous"))

    def close_tick(self) -> dict:
        """画面の1分タイマーが叩く「残り5分の自動確定」(と、終わった直の拾い)。"""
        return self.ok("/api/entry/close", {})

    def shift_key(self) -> dict:
        """画面の1分タイマーが叩く見張り(帯・またいだか・固定の様子)。"""
        query = ""
        if self.opened:
            query = ("?report_date=" + urllib.request.quote(self.opened["report_date"])
                     + "&shift=" + urllib.request.quote(self.opened["shift"])
                     + "&page=" + self.opened["page"])
        return self.get("/api/shift/key" + query)

    def start_next(self) -> dict:
        body = self.ok("/api/entry/start-next", {})
        self.opened = None
        return body

    def start_shift(self, worker: str) -> dict:
        """開いて作業者を選ぶ(直の始まり)。"""
        self.open_screen()
        return self.choose_worker(worker)

    def take_over(self, worker: str) -> dict:
        return self.ok("/api/entry/handover", {"worker": worker})

    def admin_on(self) -> None:
        self.ok("/api/settings/admin", {"enable": True, "password": "nisk"})

    def recall(self, report_date: str, shift: str, page: int = 1, line: str = "") -> dict:
        """「記録を見る」から呼び出す(画面はそのページを開き直す)。"""
        self.ok("/api/settings/recall", {"report_date": report_date, "line": line or self.line,
                                         "shift": shift, "page": page})
        return self.open_screen()

    def back(self) -> dict:
        self.ok("/api/settings/back", {})
        return self.open_screen()

    # -- 確かめるもの --------------------------------------------------------
    @property
    def line(self) -> str:
        return self.get("/api/shift/key")["line"]

    @property
    def local_db(self) -> Path:
        return self.dir / "app" / "nippou_local.sqlite3"

    def local_rows(self, sql: str, args: tuple = ()) -> list[dict]:
        conn = connect_readonly(self.local_db)
        try:
            return [dict(r) for r in conn.execute(sql, args)]
        finally:
            conn.close()

    def local_lots(self) -> dict[tuple, list[str]]:
        """手元の (報告日, ライン, 直, ページ) → LOT の並び。"""
        out: dict[tuple, list[tuple[int, str]]] = {}
        for r in self.local_rows("SELECT report_date, line, shift, page, row_no, lot"
                                 " FROM daily_detail"):
            out.setdefault((r["report_date"], r["line"], r["shift"], int(r["page"])),
                           []).append((int(r["row_no"]), r["lot"] or ""))
        return {k: [lot for _, lot in sorted(v) if lot] for k, v in out.items()}

    def dirty_keys(self) -> set[tuple]:
        return {(r["report_date"], r["line"], r["shift"], int(r["page"]))
                for r in self.local_rows("SELECT report_date, line, shift, page"
                                         " FROM daily_header WHERE dirty=1")}

    def backup_pending(self) -> int:
        return len(self.local_rows("SELECT * FROM backup_pending"))

    def wait_backup(self) -> None:
        """控え(LocalBackup)へ写し終えるのを待つ(写すのは応答を返したあと)。"""
        wait_for(f"{self.name} が控えへ写し終える", lambda: self.backup_pending() == 0)

    def screen_html(self) -> str:
        """日報入力の画面(サーバが描いた最初の表。保存してある行が入る)。"""
        status, body = self.call("GET", "/")
        self.case.assertEqual(status, 200)
        return body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)

    def sound_due(self) -> Optional[dict]:
        """画面の1分ごとの見張りが鳴らす音(15分前の催促など)。"""
        return self.get("/api/sound/due")["play"]

    def history(self) -> list[dict]:
        """この端末の共有保存の履歴(手元)。"""
        return self.local_rows("SELECT * FROM push_history ORDER BY pressed_at, rowid")


# ======================================================================
# 打つ中身
# ======================================================================
def row(lot: str, start: str, end: str, **extra) -> dict:
    values = {"LOT": lot, "KZ": start[:2], "KH": start[3:], "SZ": end[:2],
              "SH": end[3:], "MAI": "10", "TUT": "1", "WEI": "100"}
    values.update(extra)
    return values


def rest(start: str, end: str, minutes: str = "60") -> dict:
    return {"KZ": start[:2], "KH": start[3:], "SZ": end[:2], "SH": end[3:],
            "S": "0", "TH": minutes}


def rows_of(*items: dict) -> dict:
    return {str(i): values for i, values in enumerate(items, start=1)}


SPANS = {"1直": ("07:00", "15:00", "10:00", "11:00"),
         "2直": ("15:00", "22:50", "18:00", "19:00"),
         "3直": ("22:50", "07:00", "02:00", "03:00"),
         "日勤": ("08:00", "17:00", "12:00", "13:00")}


def clean_shift_rows(shift: str, prefix: str) -> dict:
    """その直を**綺麗に**最後まで打った1ページ(休憩60分・定時まで)。"""
    start, end, mid1, mid2 = SPANS[shift]
    return rows_of(row(f"{prefix}01", start, mid1), rest(mid1, mid2),
                   row(f"{prefix}02", mid2, end))


def half_shift_rows(shift: str, prefix: str) -> dict:
    """直の途中まで(最終時間に届かない ── 共有へ送るときに断られる形)。"""
    start, _end, mid1, _ = SPANS[shift]
    return rows_of(row(f"{prefix}01", start, mid1))


# ======================================================================
# 土台
# ======================================================================
def make_shared(folder: Path) -> tuple[Path, Path]:
    """共有フォルダを1つ作る。参照用マスタ(伝送用ファイル: 時間用・停止内訳・アクセス権限)。

    日報データ.sqlite3 は**作らない**(最初に送った端末が作る ── 現場と同じ)。
    """
    ref = folder / "参照"
    ref.mkdir(parents=True)
    conn = sqlite3.connect(ref / "伝送用ファイル.sqlite3")
    try:
        for no, items in ((1, [("休憩食事", "0"), ("人員不足", "2")]),
                          (2, [("突発停止(機械)", "イ")]),
                          (3, [("段取り変更", "A")])):
            conn.execute(f'CREATE TABLE "作業停止時間内訳_{no}" '
                         '("管理番号" INTEGER, "内訳" TEXT, "内訳番号" TEXT, "備考" TEXT)')
            conn.executemany(f'INSERT INTO "作業停止時間内訳_{no}" VALUES (?,?,?,?)',
                             [(i, label, code, "") for i, (label, code) in enumerate(items, 1)])
        conn.execute('CREATE TABLE "時間用" ("番号" INTEGER, "直" TEXT, "開始" TEXT,'
                     ' "終了" TEXT, "更新日" TEXT)')
        conn.executemany('INSERT INTO "時間用" VALUES (?,?,?,?,"2026/10/01")', SHIFT_TIMES)
        conn.execute('CREATE TABLE "アクセス権限" (管理番号 INTEGER, ログインID TEXT, PC名 TEXT,'
                     ' 権限 TEXT, 有効 INTEGER, 備考 TEXT)')
        conn.executemany('INSERT INTO "アクセス権限" VALUES (?,?,?,?,?,?)', RIGHTS)
        conn.commit()
    finally:
        conn.close()
    return folder, ref


def _page(row_: dict) -> int:
    value = row_.get("頁")
    return int(value if value is not None else row_.get("ページ"))


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class FlowCase(unittest.TestCase):
    """共有フォルダ1つと、そこを見る端末たち。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="nippou_flow_")
        self.addCleanup(self._cleanup, tmp)
        self.base = Path(tmp.name)
        self.shared, self.ref = make_shared(self.base / "共有")
        self.clock = Clock(self.base / "clock.txt")
        self.clock.set("2026-10-05 07:05")

    @staticmethod
    def _cleanup(tmp) -> None:
        deadline = time.monotonic() + 30
        while True:
            try:
                tmp.cleanup()
                return
            except PermissionError:                # Windows: 子が離すまで待つ
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.2)

    def terminal(self, name: str, pc: str) -> Terminal:
        return Terminal(self, name, pc=pc)

    def at(self, when: str) -> None:
        self.clock.set(when)

    # -- 共有 --------------------------------------------------------------
    @property
    def shared_db(self) -> Path:
        return self.shared / "日報データ.sqlite3"

    def shared_rows(self, table: str) -> list[dict]:
        if not self.shared_db.exists():
            return []
        conn = connect_readonly(self.shared_db)
        try:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                return []
            return [dict(r) for r in conn.execute(f'SELECT * FROM "{table}"')]
        finally:
            conn.close()

    def shared_lots(self, line: str) -> dict[tuple, list[str]]:
        """共有の (報告日, 直, ページ) → LOT の並び(行番号順)。"""
        found: dict[tuple, list[tuple[int, str]]] = {}
        for d in self.shared_rows(f"T_日報明細_{line}"):
            found.setdefault((d["報告日"], d["直"], _page(d)), []).append(
                (int(d["行番号"]), d["LOT"] or ""))
        return {k: [lot for _, lot in sorted(v) if lot] for k, v in found.items()}

    def shared_workers(self, line: str) -> dict[tuple, str]:
        return {(h["報告日"], h["直"], _page(h)): h["担当者"]
                for h in self.shared_rows(f"T_日報ヘッダー_{line}")}

    def shared_history(self) -> list[dict]:
        return self.shared_rows("T_共有保存履歴")

    def backup_lots(self, line: str) -> dict[tuple, list[str]]:
        """LocalBackup のそのラインの控え: (報告日, 直, ページ) → LOT。"""
        path = self.shared / "LocalBackup" / line / "日報入力データ.sqlite3"
        if not path.exists():
            return {}
        conn = connect_readonly(path)
        try:
            found: dict[tuple, list[tuple[int, str]]] = {}
            for r in conn.execute("SELECT report_date, shift, page, row_no, lot FROM daily_detail"):
                found.setdefault((r[0], r[1], int(r[2])), []).append((int(r[3]), r[4] or ""))
        finally:
            conn.close()
        return {k: [lot for _, lot in sorted(v) if lot] for k, v in found.items()}

    def shared_offline(self) -> None:
        """共有のフォルダに届かない(ネットワークが切れた)。"""
        wait_for("共有を外す", lambda: self._rename(self.shared, self.base / "共有_off"))

    def shared_online(self) -> None:
        wait_for("共有を戻す", lambda: self._rename(self.base / "共有_off", self.shared))

    @staticmethod
    def _rename(src: Path, dst: Path) -> bool:
        try:
            src.rename(dst)
            return True
        except PermissionError:                    # Windows: 誰かが開いている
            return False


DAY = "2026年10月5日"
NEXT_DAY = "2026年10月6日"




def _handover(body: dict) -> dict:
    return body.get("handover") or {}


# ======================================================================
# 1・2. 直の切り替わりと直の終わり
# ======================================================================
class A_ShiftChangeTests(FlowCase):
    """1直 → 2直 → 3直 → 翌日1直 を、**開いたままの画面**で通す(L-1)。
    同じ共有を見る日勤のライン(トット)の端末も並べて動かし、互いに混ざらないことを見る。"""

    def test_1直から翌日1直まで_開いたままの画面が変わり目をまたぎ_日勤のラインは別に進む(self) -> None:
        a = self.terminal("A", "PC-L1A")
        t = self.terminal("T", "PC-TOT")
        self.assertEqual((a.line, t.line), ("L-1", "トット"))

        # --- 1直(L-1)と日勤(トット)を始める --------------------------------
        self.at("2026-10-05 07:05")
        body = a.start_shift("山田")
        self.assertEqual((body["report_date"], body["shift"]), (DAY, "1直"))
        self.assertFalse(_handover(body).get("any_left"))
        self.at("2026-10-05 08:05")
        body = t.start_shift("日勤者")
        self.assertEqual((body["report_date"], body["shift"]), (DAY, "日勤"))

        # --- 1直の終わり: 15分前の催促 → 5分前の自動確定(綺麗なので共有へ) ------
        self.at("2026-10-05 14:30")
        a.saved(clean_shift_rows("1直", "A"), "山田")
        self.at("2026-10-05 14:44")
        self.assertIsNone(a.sound_due())
        self.at("2026-10-05 14:46")
        play = a.sound_due()
        self.assertEqual(play["key"], "print_reminder")
        self.assertIn("1直 終了14分前", play["message"])
        self.at("2026-10-05 14:54")
        self.assertFalse(a.close_tick()["ran"])
        self.at("2026-10-05 14:55")
        done = a.close_tick()
        self.assertTrue(done["ran"], done)
        self.assertEqual((done["shift"], done["pushed"], done["findings"]), ("1直", 1, []))
        self.assertEqual(self.shared_lots("L-1"), {(DAY, "1直", 1): ["A01", "A02"]})
        self.assertEqual(a.dirty_keys(), set())
        self.at("2026-10-05 14:57")
        mark = a.state(clean_shift_rows("1直", "A"), "山田")["push_mark"]
        self.assertEqual((mark["auto"], mark["text"]), (True, "この直は自動で共有へ保存済み(14:55)"))
        self.assertIsNone(a.sound_due(), "確定して送った直の催促が鳴り続ける")

        # --- 開いたままの画面が 15:00 をまたぐ: 帯も書き先も1直のまま・見るだけ ----
        self.at("2026-10-05 15:01")
        body = a.state(clean_shift_rows("1直", "A"), "山田")
        self.assertEqual((body["shift"], body["ribbon"]["shift"]), ("1直", "1直"))
        self.assertTrue(body["read_only"])
        self.assertIn("いま 2直", [c["text"] for c in body["ribbon"]["chips"]])
        watch = a.shift_key()
        self.assertEqual(watch["shift"], "1直")
        self.assertTrue(watch["anchor_standing"]["expired"])
        status, _ = a.save(clean_shift_rows("1直", "A"), "山田")
        self.assertEqual(status, 403, "15:00 を過ぎた1直の画面から書けた")

        # --- 次の人が「次の直を始める」→ 2直 ---------------------------------
        self.at("2026-10-05 15:05")
        a.start_next()
        body = a.start_shift("佐藤")
        self.assertEqual((body["report_date"], body["shift"]), (DAY, "2直"))
        self.assertFalse(_handover(body).get("any_left"), "送り済みの1直が残っている")

        # --- 日勤: 5分前に確定して送る → 残業を打つと未送信に戻る → 手で送る -------
        day_rows = rows_of(row("D01", "08:00", "12:00"), rest("12:00", "13:00"),
                           row("D02", "13:00", "17:00"))
        self.at("2026-10-05 16:30")
        t.saved(day_rows, "日勤者")
        self.at("2026-10-05 16:55")
        self.assertEqual(t.close_tick()["pushed"], 1)
        self.at("2026-10-05 17:50")
        day_rows["4"] = row("D03", "17:00", "18:00")
        body = t.saved(day_rows, "日勤者")
        self.assertEqual((body["report_date"], body["shift"]), (DAY, "日勤"))
        self.assertEqual(t.dirty_keys(), {(DAY, "トット", "日勤", 1)})
        status, body = t.push_now(worker="日勤者")
        self.assertEqual(status, 200, body)
        self.assertEqual(self.shared_lots("トット"), {(DAY, "日勤", 1): ["D01", "D02", "D03"]})

        # --- 2直の終わり → 3直(日をまたぐ。報告日は始めた日) ----------------------
        self.at("2026-10-05 22:30")
        a.saved(clean_shift_rows("2直", "B"), "佐藤")
        self.at("2026-10-05 22:45")
        self.assertEqual(a.close_tick()["pushed"], 1)
        self.at("2026-10-05 22:55")
        a.start_next()
        body = a.start_shift("夜勤")
        self.assertEqual((body["report_date"], body["shift"]), (DAY, "3直"))
        self.at("2026-10-06 00:10")
        body = a.saved(rows_of(row("C01", "22:50", "02:00")), "夜勤")
        self.assertEqual((body["report_date"], body["shift"]), (DAY, "3直"))
        self.at("2026-10-06 06:30")
        a.saved(clean_shift_rows("3直", "C"), "夜勤")
        self.at("2026-10-06 06:55")
        done = a.close_tick()
        self.assertEqual((done["report_date"], done["shift"], done["pushed"]), (DAY, "3直", 1))

        # --- 翌日の1直 ------------------------------------------------------
        self.at("2026-10-06 07:01")
        a.start_next()
        body = a.start_shift("山田")
        self.assertEqual((body["report_date"], body["shift"]), (NEXT_DAY, "1直"))
        self.assertFalse(_handover(body).get("any_left"))

        # --- 共有: ラインごとの表にそろい、互いに混ざらない -------------------------
        self.assertEqual(self.shared_lots("L-1"), {
            (DAY, "1直", 1): ["A01", "A02"], (DAY, "2直", 1): ["B01", "B02"],
            (DAY, "3直", 1): ["C01", "C02"]})
        self.assertEqual(self.shared_lots("トット"), {(DAY, "日勤", 1): ["D01", "D02", "D03"]})
        self.assertEqual(a.dirty_keys() | t.dirty_keys(), set())
        found = sorted((r["ライン"], r["直"], r["結果"], r["押した人"], r["通し方"])
                       for r in self.shared_history())
        self.assertEqual(found, sorted([
            ("L-1", "1直", "送れた", "(自動確定)", "残り5分の自動確定"),
            ("L-1", "2直", "送れた", "(自動確定)", "残り5分の自動確定"),
            ("L-1", "3直", "送れた", "(自動確定)", "残り5分の自動確定"),
            ("トット", "日勤", "送れた", "(自動確定)", "残り5分の自動確定"),
            ("トット", "日勤", "送れた", "日勤者", "いまの直")]))
        summary = {(r["報告日"], r["直"]): r["担当者"] for r in self.shared_rows("T_日報集計_L-1")}
        self.assertEqual(summary, {(DAY, "1直"): "山田", (DAY, "2直"): "佐藤",
                                   (DAY, "3直"): "夜勤"})


# ======================================================================
# 3. 前の直が未送信・エラーのまま次の直へ(次の直は別の端末)
# ======================================================================
class B_PreviousShiftTests(FlowCase):

    def test_渡さないまま直が終わった_次の直の端末が控えから拾って送り_元の端末は送り済みになる(self) -> None:
        """【直した】1直の端末(A)が直の終わり前に落ち、2直は同じラインの別の端末(B)で始める。
        B は起動で控え(LocalBackup)から1直を受け取り、赤い帯 → 次の直の画面の拾いで送る。

        翌朝 A を起動すると、**A の手元ではその1直がまだ未送信**のままでした(送ったのは B)。
        A の画面は送り済みの1直を「前の直(…1直)が共有へ出ていません。片付けるまで、この直の
        入力は始められません」と赤い帯で止めていました。起動のときに控えの「共有へ済」を見て、
        同じ中身なら送った印だけ揃えます(`services/local_backup.pull`)。
        """
        a = self.terminal("A", "PC-L1A")
        a.start_shift("山田")
        self.at("2026-10-05 14:30")
        a.saved(clean_shift_rows("1直", "A"), "山田")
        a.wait_backup()
        self.at("2026-10-05 14:50")
        a.stop()                                   # 直の終わりの前に落ちた(自動確定は走らない)

        self.at("2026-10-05 15:05")
        b = self.terminal("B", "PC-L1B")
        self.assertEqual(b.dirty_keys(), {(DAY, "L-1", "1直", 1)}, "控えから受け取っていない")
        body = b.open_screen()
        self.assertEqual(body["shift"], "2直")
        band = _handover(body)
        self.assertTrue(band["blocked"])
        self.assertTrue(band["all_clean"])
        self.assertIn(f"{DAY} L-1 1直", band["message"])
        tick = b.close_tick()
        self.assertEqual([(s["report_date"], s["shift"]) for s in tick["swept"]], [(DAY, "1直")])
        self.assertIn("共有へも送りました", tick["message"])
        self.assertEqual(self.shared_lots("L-1"), {(DAY, "1直", 1): ["A01", "A02"]})
        self.assertFalse(_handover(b.choose_worker("佐藤")).get("any_left"))
        b.wait_backup()
        self.assertEqual([(r["直"], r["結果"], r["通し方"]) for r in self.shared_history()],
                         [("1直", "送れた", "次の直の画面が拾って送った")])

        # 翌朝、直った A を起動する: 送り済みの1直で止めない
        self.at("2026-10-06 07:05")
        a.start()
        self.assertEqual(a.dirty_keys(), set(), "B が送った1直が A ではまだ未送信のまま")
        body = a.open_screen()
        self.assertEqual((body["report_date"], body["shift"]), (NEXT_DAY, "1直"))
        self.assertFalse(_handover(body).get("any_left"), _handover(body).get("message"))
        # A の画面の拾いが、送り済みの1直をもう一度送り直さない
        self.assertEqual(a.close_tick()["swept"], [])
        self.assertEqual(len(self.shared_history()), 1)

    def test_エラーの直を次の端末が引き継ぎ_管理者が別のPCで直して送ると_各端末にそろう(self) -> None:
        """1直(A)は休憩が足りないまま直が終わる → 5分前の自動確定は送らない。2直は別の端末(B)。
        B の赤い帯: 過去分を送ってもエラーの1直は残る → 直せないまま引き継ぐ → 2直は自動で送る。
        管理者のPCが控えから1直を呼び出して直し、共有へ送る。A・B は起動し直すと直した中身を受け取る。"""
        a = self.terminal("A", "PC-L1A")
        a.start_shift("山田")
        bad = clean_shift_rows("1直", "A")
        bad["2"]["TH"] = "30"                          # 休憩30分
        self.at("2026-10-05 14:30")
        a.saved(bad, "山田")
        self.at("2026-10-05 14:55")
        done = a.close_tick()
        self.assertTrue(done["ran"])
        self.assertEqual(done["pushed"], 0)
        self.assertIn("休憩", done["findings"][0])
        a.wait_backup()
        a.stop()

        self.at("2026-10-05 15:05")
        b = self.terminal("B", "PC-L1B")
        band = _handover(b.open_screen())
        self.assertTrue(band["blocked"])
        self.assertFalse(band["all_clean"])
        self.assertIn("take", band["actions"])
        status, body = b.push_past(worker="佐藤")
        self.assertEqual(status, 422, body)
        self.assertIn("過去分: 送った直 0件 / エラーで残した直 1件", body["error"]["message"])
        b.choose_worker("佐藤")
        b.take_over("佐藤")
        self.at("2026-10-05 22:30")
        b.saved(clean_shift_rows("2直", "B"), "佐藤")
        self.at("2026-10-05 22:45")
        self.assertEqual(b.close_tick()["pushed"], 1, "前の直のエラーにいまの直が止められた")
        self.assertEqual(self.shared_lots("L-1"), {(DAY, "2直", 1): ["B01", "B02"]})
        b.wait_backup()

        # 管理者のPC: 控えから1直を呼び出して直し、共有へ
        self.at("2026-10-06 09:00")
        m = self.terminal("M", "PC-ADMIN")
        listed = m.get("/api/records/backup?line=L-1")["shifts"]
        self.assertEqual(sorted((s["shift"], s["synced"]) for s in listed),
                         [("1直", False), ("2直", True)])
        m.ok("/api/records/backup/open", {"report_date": DAY, "line": "L-1", "shift": "1直"})
        body = m.open_screen()
        self.assertEqual((body["report_date"], body["line"], body["shift"]), (DAY, "L-1", "1直"))
        m.saved(clean_shift_rows("1直", "A"), "山田")
        m.back()
        status, body = m.post("/api/settings/push", {"worker": "管理"})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.shared_lots("L-1"), {(DAY, "1直", 1): ["A01", "A02"],
                                                  (DAY, "2直", 1): ["B01", "B02"]})
        breaks = {int(d["行番号"]): d["TH"] for d in self.shared_rows("T_日報明細_L-1")
                  if d["直"] == "1直"}
        self.assertEqual(breaks[2], "60", "直した休憩が共有に出ていない")
        m.wait_backup()

        # A・B を起動し直すと、直した1直を受け取り、どちらも未送信は無い
        for term in (a, b) if a.proc is None else (b,):
            if term.proc is not None:
                term.restart()
            else:
                term.start()
            self.assertEqual(term.dirty_keys(), set(), term.name)
            th = {r["row_no"]: r["th"] for r in term.local_rows(
                "SELECT row_no, th FROM daily_detail WHERE shift='1直'")}
            self.assertEqual(th[2], "60", term.name)
            self.assertFalse(_handover(term.open_screen()).get("any_left"), term.name)
        results = [(r["直"], r["結果"]) for r in self.shared_history()]
        for expected in [("1直", "関門で止めた"), ("1直", "直せないまま引き継いだ"),
                         ("2直", "送れた"), ("1直", "送れた")]:
            self.assertIn(expected, results)


# ======================================================================
# 4. 複数台・複数ライン
# ======================================================================
class C_MultiTerminalTests(FlowCase):

    def test_L1とHVCが同じ共有へ同時に何度送っても_それぞれの表にそろい_ほかのラインに触らない(self) -> None:
        a = self.terminal("A", "PC-L1A")
        h = self.terminal("H", "PC-HVC")
        a.start_shift("山田")
        h.start_shift("田中")
        rows_a = clean_shift_rows("1直", "A")
        rows_h = clean_shift_rows("1直", "H")
        for n, when in enumerate(("14:20", "14:30", "14:40"), start=1):
            self.at(f"2026-10-05 {when}")
            rows_a["1"]["MAI"] = str(10 + n)
            rows_h["1"]["MAI"] = str(20 + n)
            a.saved(rows_a, "山田")
            h.saved(rows_h, "田中")
            got: dict[str, tuple[int, Any]] = {}
            pushers = [threading.Thread(target=lambda t=t, w=w: got.__setitem__(
                t.name, t.push_now(worker=w))) for t, w in ((a, "山田"), (h, "田中"))]
            for th in pushers:
                th.start()
            for th in pushers:
                th.join()
            for name, (status, body) in got.items():
                self.assertEqual(status, 200, f"{n}回目 {name}: {body}")
            self.assertEqual(self.shared_lots("L-1"), {(DAY, "1直", 1): ["A01", "A02"]})
            self.assertEqual(self.shared_lots("HVC"), {(DAY, "1直", 1): ["H01", "H02"]})
            mai = {d["LOT"]: d["MAI"] for line in ("L-1", "HVC")
                   for d in self.shared_rows(f"T_日報明細_{line}") if d["LOT"]}
            self.assertEqual((mai["A01"], mai["H01"]), (str(10 + n), str(20 + n)), f"{n}回目")
        self.assertEqual(a.dirty_keys() | h.dirty_keys(), set())
        self.assertEqual(sorted((r["ライン"], r["結果"], r["押した人"]) for r in self.shared_history()),
                         sorted([("L-1", "送れた", "山田")] * 3 + [("HVC", "送れた", "田中")] * 3))
        self.assertEqual({r["担当者"] for r in self.shared_rows("T_日報集計_L-1")}, {"山田"})
        self.assertEqual({r["担当者"] for r in self.shared_rows("T_日報集計_HVC")}, {"田中"})
        # 控えはラインごとの別のファイル
        a.wait_backup()
        h.wait_backup()
        self.assertEqual(self.backup_lots("L-1"), {(DAY, "1直", 1): ["A01", "A02"]})
        self.assertEqual(self.backup_lots("HVC"), {(DAY, "1直", 1): ["H01", "H02"]})

    def test_同じラインの2台目が前の直の続きを打つ_起動で控えを受け取り_続きのまま送る(self) -> None:
        """1直の途中で A が壊れた。同じラインの B を起動すると、A が打った行が画面に出て、
        続きを打って送れる(A の行を消さない)。直った A は次の起動で B の続きを受け取る。"""
        a = self.terminal("A", "PC-L1A")
        a.start_shift("山田")
        self.at("2026-10-05 10:00")
        a.saved(rows_of(row("A01", "07:00", "09:00"), row("A02", "09:00", "10:00")), "山田")
        a.wait_backup()
        a.stop()

        self.at("2026-10-05 10:05")
        b = self.terminal("B", "PC-L1B")
        html = b.screen_html()
        self.assertIn("A01", html, "B の画面に A が打った行が出ていない")
        self.assertIn("A02", html)
        body = b.start_shift("山田")
        self.assertEqual((body["report_date"], body["shift"], str(body["page"])), (DAY, "1直", "1"))
        self.assertFalse(_handover(body).get("any_left"), "いまの直の打ちかけを前の直と取り違えた")
        self.at("2026-10-05 14:30")
        full = rows_of(row("A01", "07:00", "09:00"), row("A02", "09:00", "10:00"),
                       rest("10:00", "11:00"), row("B03", "11:00", "15:00"))
        b.saved(full, "山田")
        self.at("2026-10-05 14:55")
        self.assertEqual(b.close_tick()["pushed"], 1)
        self.assertEqual(self.shared_lots("L-1"), {(DAY, "1直", 1): ["A01", "A02", "B03"]})
        b.wait_backup()

        self.at("2026-10-05 15:30")
        a.start()
        self.assertEqual(a.local_lots(), {(DAY, "L-1", "1直", 1): ["A01", "A02", "B03"]})
        self.assertEqual(a.dirty_keys(), set())

    def test_後日作成は_ほかの端末が送ったページを上書きさせない_抜けた直は作れる(self) -> None:
        a = self.terminal("A", "PC-L1A")
        b = self.terminal("B", "PC-L1B")
        b.start_shift("佐藤")
        self.at("2026-10-05 14:30")
        b.saved(clean_shift_rows("1直", "B"), "佐藤")
        self.assertEqual(b.push_now(worker="佐藤")[0], 200)

        self.at("2026-10-06 09:00")
        a.admin_on()
        status, body = a.post("/api/settings/backfill",
                              {"report_date": DAY, "line": "L-1", "shift": "1直", "dry_run": True})
        self.assertEqual(status, 409, body)
        self.assertEqual(body["error"]["code"], "in_shared")
        # 2直は誰も打っていない(抜けた直)→ 作れる
        a.ok("/api/settings/backfill", {"report_date": DAY, "line": "L-1", "shift": "2直",
                                        "note": "打ち忘れ"})
        body = a.open_screen()
        self.assertEqual((body["report_date"], body["shift"]), (DAY, "2直"))
        a.saved(clean_shift_rows("2直", "K"), "佐藤")
        a.back()
        status, body = a.push_past(worker="管理")
        self.assertEqual(status, 200, body)
        self.assertEqual(self.shared_lots("L-1"), {(DAY, "1直", 1): ["B01", "B02"],
                                                  (DAY, "2直", 1): ["K01", "K02"]})
        made = [r["理由"] for r in self.shared_history() if r["直"] == "2直"]
        self.assertTrue(made and "後日作成" in made[0], made)


# ======================================================================
# 5. 共有が見えないあいだ
# ======================================================================
class D_OfflineTests(FlowCase):

    def test_共有が落ちているあいだの自動確定は送信待ちに残り_戻ったら過去分で送られる(self) -> None:
        a = self.terminal("A", "PC-L1A")
        h = self.terminal("H", "PC-HVC")
        a.start_shift("山田")
        h.start_shift("田中")
        self.at("2026-10-05 14:30")
        a.saved(clean_shift_rows("1直", "A"), "山田")
        h.saved(clean_shift_rows("1直", "H"), "田中")
        a.wait_backup()
        h.wait_backup()

        self.shared_offline()
        self.at("2026-10-05 14:55")
        for term in (a, h):
            done = term.close_tick()
            self.assertTrue(done["ran"], term.name)
            self.assertEqual(done["pushed"], 0, term.name)
            self.assertIn("送れなかった", done["message"], term.name)
        self.at("2026-10-05 15:05")
        a.start_next()
        band = _handover(a.open_screen())
        self.assertTrue(band["blocked"])
        self.assertIn("届きませんでした", band["message"])
        self.assertIn("take", band["actions"])
        # 届かないまま「過去分を共有へ送る」→ 送れず、未送信のまま残る
        status, body = a.push_past(worker="佐藤")
        self.assertIn("失敗: 1件", body.get("message") or json.dumps(body, ensure_ascii=False))
        self.assertEqual(a.dirty_keys(), {(DAY, "L-1", "1直", 1)})
        # 直せないまま引き継いで2直を打ち始める(手元にだけ入る)
        a.choose_worker("佐藤")
        a.take_over("佐藤")
        self.at("2026-10-05 16:00")
        a.saved(rows_of(row("B01", "15:00", "16:00")), "佐藤")

        # 共有が戻る → 過去分で1直が送られる。HVC も次の直の始めに送る
        self.shared_online()
        status, body = a.push_past(worker="佐藤")
        self.assertEqual(status, 200, body)
        self.assertIn("送った直 1件", body["message"])
        h.start_next()
        h.open_screen()
        status, body = h.push_past(worker="鈴木")
        self.assertEqual(status, 200, body)
        self.assertEqual(self.shared_lots("L-1"), {(DAY, "1直", 1): ["A01", "A02"]})
        self.assertEqual(self.shared_lots("HVC"), {(DAY, "1直", 1): ["H01", "H02"]})
        self.assertEqual(a.dirty_keys(), {(DAY, "L-1", "2直", 1)}, "打ちかけの2直まで送った")
        self.assertEqual(h.dirty_keys(), set())
        # 届かなかったことも履歴に残り、届いてから共有の表へ写る
        found = [(r["ライン"], r["結果"], r["押した人"]) for r in self.shared_history()]
        for expected in [("L-1", "送れなかった", "(自動確定)"), ("HVC", "送れなかった", "(自動確定)"),
                         ("L-1", "送れた", "佐藤"), ("HVC", "送れた", "鈴木")]:
            self.assertIn(expected, found)
        # 控え: 落ちているあいだに打った2直も、起動し直せば写る
        a.restart()
        a.wait_backup()
        self.assertIn((DAY, "2直", 1), self.backup_lots("L-1"))


# ======================================================================
# 6・7. 記録を見る(ほかのPC)と月替わり
# ======================================================================
class E_RecordsAndMonthTests(FlowCase):

    def month_rows(self, line: str, month: str) -> dict[str, list[str]]:
        """共有の 自動集計\\<ライン>\\<年.月> の明細CSV: LOT → 枚数。"""
        import csv

        folder = self.shared / "自動集計" / line / month
        files = sorted(folder.glob("*_明細.csv"))
        if not files:
            return {}
        with files[0].open(encoding="utf-8-sig", newline="") as fh:
            items = list(csv.reader(fh))
        head = items[0]
        lot = next(i for i, name in enumerate(head) if "ロット" in name or name == "LOT")
        return {r[lot]: r for r in items[1:] if len(r) > lot and r[lot]}

    def test_月替わり_それぞれのラインが前の月を書き出し_互いに消さない(self) -> None:
        self.at("2026-09-30 22:55")
        a = self.terminal("A", "PC-L1A")
        h = self.terminal("H", "PC-HVC")
        for term, worker in ((a, "夜勤"), (h, "夜勤H")):
            body = term.start_shift(worker)
            self.assertEqual((body["report_date"], body["shift"]), ("2026年9月30日", "3直"))
        self.at("2026-10-01 06:30")
        a.saved(clean_shift_rows("3直", "N"), "夜勤")
        h.saved(clean_shift_rows("3直", "V"), "夜勤H")
        self.at("2026-10-01 06:55")
        for term in (a, h):
            done = term.close_tick()
            self.assertEqual(done["pushed"], 1, term.name)
            self.assertIn("2026年9月 を書き出しました", done["message"], term.name)
        self.assertEqual(set(self.month_rows("L-1", "2026.09")), {"N01", "N02"})
        self.assertEqual(set(self.month_rows("HVC", "2026.09")), {"V01", "V02"})
        # 10月の1直: 9月は済んでいるので書き出し直さない
        self.at("2026-10-01 07:05")
        a.start_next()
        body = a.start_shift("山田")
        self.assertEqual((body["report_date"], body["shift"]), ("2026年10月1日", "1直"))
        self.at("2026-10-01 14:30")
        a.saved(clean_shift_rows("1直", "A"), "山田")
        status, body = a.push_now(worker="山田")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["rollovers"], [])
        self.assertFalse((self.shared / "自動集計" / "L-1" / "2026.10").exists())
        self.assertEqual(set(self.month_rows("HVC", "2026.09")), {"V01", "V02"})
        summary = {(r["報告日"], r["直"]) for r in self.shared_rows("T_日報集計_L-1")}
        self.assertEqual(summary, {("2026年9月30日", "3直"), ("2026年10月1日", "1直")})

    def test_管理者のPCがほかのラインの直を直して送っても_そのラインの月の書き出しを欠けさせない(self) -> None:
        """【直した】HVC の端末が9月を書き出したあと、管理者のPCが控えから HVC の 9/30 2直を
        呼び出して直し、共有へ送る。管理者のPCの手元にある9月の HVC はその1直だけなので、
        月替わりの書き出しが共有の 自動集計\\HVC\\2026.09 を**その1直だけで上書き**して、
        HVC の端末が書き出した 1直 が消えていました(`services/month_rollover._partial_lines`)。

        直したあと: 管理者のPCは HVC の9月を書き出さない(知らせで言う)。直した中身は共有の日報に
        入り、HVC の端末が次の起動で受け取って、次に送ったときに9月を書き出し直す。"""
        self.at("2026-09-30 07:05")
        h = self.terminal("H", "PC-HVC")
        h.start_shift("田中")
        self.at("2026-09-30 14:30")
        h.saved(clean_shift_rows("1直", "H"), "田中")
        self.at("2026-09-30 14:55")
        self.assertEqual(h.close_tick()["pushed"], 1)
        self.at("2026-09-30 15:05")
        h.start_next()
        h.start_shift("鈴木")
        self.at("2026-09-30 22:30")
        h.saved(clean_shift_rows("2直", "J"), "鈴木")
        self.at("2026-09-30 22:45")
        self.assertEqual(h.close_tick()["pushed"], 1)
        self.at("2026-10-01 07:05")
        h.start_next()
        h.start_shift("田中")
        self.at("2026-10-01 14:30")
        h.saved(clean_shift_rows("1直", "K"), "田中")
        status, body = h.push_now(worker="田中")
        self.assertEqual(status, 200, body)
        self.assertIn("2026年9月 を書き出しました", body["message"])
        self.assertEqual(set(self.month_rows("HVC", "2026.09")), {"H01", "H02", "J01", "J02"})
        h.wait_backup()

        # 管理者のPC: 控えから 9/30 2直 を呼び出して直し、共有へ
        self.at("2026-10-02 09:00")
        m = self.terminal("M", "PC-ADMIN")
        m.ok("/api/records/backup/open",
             {"report_date": "2026年9月30日", "line": "HVC", "shift": "2直"})
        body = m.open_screen()
        self.assertEqual((body["line"], body["shift"]), ("HVC", "2直"))
        fixed = clean_shift_rows("2直", "J")
        fixed["1"]["MAI"] = "30"
        m.saved(fixed, "鈴木")
        m.back()
        status, body = m.post("/api/settings/push", {"worker": "管理"})
        self.assertEqual(status, 200, body)
        self.assertIn("HVC はこの端末では書き出しません", body["message"])
        shared_mai = {d["LOT"]: d["MAI"] for d in self.shared_rows("T_日報明細_HVC") if d["LOT"]}
        self.assertEqual(shared_mai["J01"], "30", "直した中身が共有に出ていない")
        self.assertEqual(set(self.month_rows("HVC", "2026.09")), {"H01", "H02", "J01", "J02"},
                         "管理者のPCが HVC の9月の書き出しを欠けさせた")
        m.wait_backup()

        # HVC の端末: 次の起動で直した中身を受け取り、次に送ったときに9月を書き出し直す
        self.at("2026-10-02 09:30")
        h.restart()
        mai = {r["lot"]: r["mai"] for r in h.local_rows("SELECT lot, mai FROM daily_detail")}
        self.assertEqual(mai["J01"], "30")
        self.assertEqual(h.dirty_keys(), set())
        h.start_shift("田中")
        self.at("2026-10-02 14:30")
        h.saved(clean_shift_rows("1直", "L"), "田中")
        status, body = h.push_now(worker="田中")
        self.assertEqual(status, 200, body)
        self.assertIn("2026年9月 を書き出しました", body["message"])
        rows = self.month_rows("HVC", "2026.09")
        self.assertEqual(set(rows), {"H01", "H02", "J01", "J02"})
        self.assertIn("30", rows["J01"], "直した枚数が9月の書き出しに入っていない")


if __name__ == "__main__":
    unittest.main()
