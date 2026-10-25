"""共有フォルダを使って**複数台で**使う一連の流れ(端末ごとに別のプロセスを立てる)

【何を確かめるか】
現場では、端末ごとにライン(作業長・コイル・L-1・HVC …)を決め、同じ共有フォルダの
取り込み元(保存用DB=連絡帳・マスタDB=班員名簿とアクセス権限)と同期して使う。
1台の中の画面操作はほかの試験(``test_web_calendar.py``・``*_browser.py``)が見ている。
ここで見るのは **台をまたいだときだけ起きること**:

1. 登録 → 同期 → ほかの端末に届き、見える決まり(``record_shown``)どおりに出る
   (マス・マウスの説明・内容閲覧・登録の件数・履歴)
2. 削除がほかの端末にも効く。見えない登録は消せない。2台がほぼ同時に消す等の行き違い
3. 同じ人の同じ日の休みを2台がほぼ同時に登録する(二重登録の防ぎが同期をまたいで効くか)
4. 共有フォルダが見えないあいだの登録は送信待ちに残り、戻ったら送られる(画面の知らせ)
5. マスタ管理で名簿・アクセス権限を直すと、ほかの端末に届く
6. 端末のラインを変えたとき(設定・次のライン予約)、見えるものが変わる
7. 開いたままの古い画面から操作しても、黙って間違ったことをしない

【どう立てるか】
端末1台 = **別々のプロセス**(本物の waitress で待ち受ける)。手元の領域
(``CALENDAR_HOME``・``CALENDAR_LOCAL_DIR``)は台ごとに別、共有フォルダだけ同じ所を指す。
ライン設定・同期サービス・画面の錠はプロセスに1つずつなので、1つのプロセスの中で
台を入れ替えて真似ると、どこかで前の台の状態を持ち越して「本当は届いていないのに
通る」が起きる。台を分ければ、届くのは共有フォルダを通ったものだけになる。

子プロセスは ``tests`` パッケージを読まない(下の ``_CHILD``)。読むと一時フォルダを
作って止めたときに残る。本物の利用者のフォルダは使わない(全部この試験の一時フォルダ)。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Optional

from . import _web
from calendar_app import config

ROOT = Path(__file__).resolve().parents[1]

#: 子プロセスが立ち上がるのを待つ上限(秒)
BOOT_SEC = 60.0

#: 班員名簿(マスタDB)。管理番号, 苗字, 班, 名前, 読み, 担当ライン
MEMBERS = (
    ("101", "山田", "B", "山田太郎", "やまだ", "L-1"),
    ("102", "佐藤", "日勤", "佐藤花子", "さとう", "L-1"),
    ("201", "鈴木", "A", "鈴木一郎", "すずき", "コイル"),
    ("202", "高橋", "日勤", "高橋次郎", "たかはし", "コイル"),
    ("301", "田中", "C", "田中三郎", "たなか", "HVC"),
)

#: 子プロセスの中身。**tests パッケージを読まない**(説明は冒頭)。
#: 起動の手順は ``start_app._initialize`` と同じ順(予約 → アクセス権限 → 同期)
_CHILD = r"""
import os, signal, sys
sys.path.insert(0, os.environ["FLOW_ROOT"])
from calendar_app import logging_utils
logging_utils.silence_console()
from calendar_app import access_control, db, settings as s, sync_service, terminals

if not s.has_value(s.KEY_DATA_DB_DIR):              # 初めて起動した端末
    s.update({s.KEY_DATA_DB_DIR: os.environ["FLOW_SHARED"],
              s.KEY_MASTER_DB_DIR: os.environ["FLOW_SHARED"],
              s.KEY_MY_LINE: os.environ.get("FLOW_LINE", ""),
              s.KEY_SYNC_INTERVAL: int(os.environ.get("FLOW_INTERVAL", "20"))})
db.connect().close()
terminals.apply_reservation()
conn = db.connect()
try:
    access_control.enforce(conn)
finally:
    conn.close()
service = sync_service.get_service()
if os.environ.get("FLOW_AUTOSYNC") == "1":
    service.start()
else:
    service.run_now(receive=True)

from app import create_app
from waitress.server import create_server

app = create_app(token=os.environ["FLOW_TOKEN"])
app.config["READY"] = True
server = create_server(app, host="127.0.0.1", port=0, threads=8, ident="flow")

def _stop(*_):
    raise SystemExit(0)

signal.signal(signal.SIGTERM, _stop)
print("PORT=%s" % server.effective_port, flush=True)
try:
    server.run()
finally:
    server.close()
"""


class Terminal:
    """端末1台。別プロセスで立てて、HTTP で操作する。"""

    def __init__(self, case: unittest.TestCase, base: Path, name: str, *,
                 line: str, shared: Path, pc: str = "", login: str = "",
                 autosync: bool = False, interval: int = 20, claim: bool = True) -> None:
        self.case = case
        self.name = name
        self.line = line
        self.shared = shared
        self.pc = pc or f"PC-{name}"
        self.login = login or f"user{name.lower()}"
        self.autosync = autosync
        self.interval = interval
        #: 試験から画面を名乗るか。ブラウザで画面を開く端末は名乗らない(2枚目になる)
        self.claims = claim
        self.dir = base / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.proc: Optional[subprocess.Popen] = None
        self.port = 0
        self.screen = ""
        self.start()
        case.addCleanup(self.stop)

    # -- 立てる・止める ----------------------------------------------------
    def start(self) -> None:
        env = dict(os.environ)
        env.update({
            "FLOW_ROOT": str(ROOT),
            "FLOW_SHARED": str(self.shared),
            "FLOW_LINE": self.line,
            "FLOW_TOKEN": _web.TOKEN,
            "FLOW_AUTOSYNC": "1" if self.autosync else "0",
            "FLOW_INTERVAL": str(self.interval),
            "CALENDAR_HOME": str(self.dir / "home"),
            "CALENDAR_LOCAL_DIR": str(self.dir / "local"),
            "CALENDAR_DISTRIBUTION_DIR": str(self.dir / "配布設定"),
            "CALENDAR_PC_NAME": self.pc,
            "CALENDAR_LOGIN_ID": self.login,
            "PYTHONIOENCODING": "utf-8",
        })
        self._stderr = open(self.dir / "stderr.txt", "ab")
        self.proc = subprocess.Popen(
            [sys.executable, "-c", _CHILD], cwd=str(self.dir), env=env,
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
        if self.claims:
            self.claim()

    def claim(self) -> None:
        """画面を名乗る(ブラウザの画面が開いたときと同じ)。"""
        self.screen = uuid.uuid4().hex
        status, body = self.post("/api/screen/claim", {"screen_id": self.screen, "force": True})
        self.case.assertEqual(status, 200, body)

    def stop(self) -> None:
        if self.proc is None:
            return
        proc, self.proc = self.proc, None
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=15)
        if proc.stdout is not None:
            proc.stdout.close()
        self._stderr.close()

    def restart(self) -> None:
        """閉じて開き直す(手元の領域はそのまま)。"""
        self.stop()
        self.start()

    def stderr(self) -> str:
        try:
            return (self.dir / "stderr.txt").read_text("utf-8", "replace")[-4000:]
        except OSError:
            return ""

    # -- HTTP --------------------------------------------------------------
    def call(self, method: str, path: str, body: Any = None, *,
             screen: Optional[str] = None) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"X-Tool-Token": _web.TOKEN, "Content-Type": "application/json",
                     "X-Screen-Id": self.screen if screen is None else screen})
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
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

    def post(self, path: str, body: Any, **kwargs) -> tuple[int, Any]:
        return self.call("POST", path, body, **kwargs)

    # -- 業務の操作 --------------------------------------------------------
    def sync(self) -> dict:
        """「いますぐ同期」。"""
        status, body = self.post("/api/sync/now", {})
        self.case.assertEqual(status, 200, f"{self.name} 同期: {body}")
        return body

    def rest(self, day: _dt.date, code: str, **extra) -> tuple[int, Any]:
        payload = {"date": day.strftime("%Y/%m/%d"), "code": code, **extra}
        return self.post("/api/rest", payload)

    def comment(self, day: _dt.date, line: str, group: str, text: str) -> tuple[int, Any]:
        return self.post("/api/comment", {"date": day.strftime("%Y/%m/%d"),
                                          "line": line, "group": group, "text": text})

    def month(self, day: _dt.date) -> dict:
        return self.get(f"/api/calendar?year={day.year}&month={day.month}")

    def cell(self, day: _dt.date) -> dict:
        key = day.strftime("%Y/%m/%d")
        return next(c for c in self.month(day)["cells"] if c["date"] == key)

    def day(self, day: _dt.date) -> dict:
        return self.get(f"/api/day/{day.strftime('%Y/%m/%d')}")

    def captions(self, day: _dt.date) -> list[str]:
        return [item["caption"] for item in self.day(day)["items"]]

    def delete(self, day: _dt.date, items: list[dict]) -> tuple[int, Any]:
        return self.post("/api/delete", {
            "date": day.strftime("%Y/%m/%d"),
            "ids": [item["id"] for item in items],
            "marks": {str(item["id"]): item["mark"] for item in items}})

    def item(self, day: _dt.date, words: str) -> dict:
        """内容閲覧・削除の選択肢から、見出しに ``words`` を含む1行。"""
        found = [i for i in self.day(day)["items"] if words in i["caption"]]
        self.case.assertEqual(len(found), 1, f"{self.name} {day} {words}: {self.captions(day)}")
        return found[0]

    def status(self) -> dict:
        return self.get("/api/sync")

    def worker_codes(self) -> list[str]:
        return [w["code"] for line in self.get("/api/workers")["lines"]
                for group in line["groups"] for w in group["workers"]]

    def set_line(self, line: str) -> tuple[int, Any]:
        return self.post("/api/settings/line", {"line": line, "password": _web.ADMIN_PASSWORD})

    def my_line(self) -> str:
        return self.month(_dt.date.today())["my_line"]


# ---------------------------------------------------------------------------
# 共有フォルダ
# ---------------------------------------------------------------------------
def make_shared(folder: Path) -> Path:
    """共有フォルダを1つ作る。保存用DB(連絡帳)とマスタDB(班員名簿入り)。"""
    folder.mkdir(parents=True, exist_ok=True)
    _web.make_source(folder, config.SOURCE_FILE_DATA, _web.SOURCE_SCHEMA)
    master = _web.make_source(folder, config.SOURCE_FILE_MASTER, _web.MASTER_SCHEMA)
    conn = sqlite3.connect(master)
    try:
        conn.executemany(
            f'INSERT INTO "{config.TABLE_MEMBER}" VALUES (?,?,?,?,?,?)', MEMBERS)
        conn.commit()
    finally:
        conn.close()
    return folder


def next_month_day(day: int) -> _dt.date:
    """来月の ``day`` 日。**今月の残りの日数に左右されない**登録日にする。"""
    today = _dt.date.today()
    year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return _dt.date(year, month, day)


def last_month_day(day: int) -> _dt.date:
    today = _dt.date.today()
    year, month = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
    return _dt.date(year, month, day)


class _Flow(unittest.TestCase):
    """共有フォルダ1つと、そこを見る端末たち。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="calendar_flow_")
        self.addCleanup(_web._remove_when_released, tmp)
        self.base = Path(tmp.name)
        self.shared = make_shared(self.base / "共有")
        self.data_db = self.shared / config.SOURCE_FILE_DATA
        self.master_db = self.shared / config.SOURCE_FILE_MASTER

    def terminal(self, name: str, line: str, **kwargs) -> Terminal:
        return Terminal(self, self.base, name, line=line, shared=self.shared, **kwargs)

    def source_rows(self, table: str = config.TABLE_DATA) -> list[dict]:
        return _web.read_source(self.data_db, table)

    def sync_all(self, *terminals: Terminal) -> None:
        """送ってから、もう1巡して全員が取り込む(送る順に左右されないように)。"""
        for t in terminals:
            t.sync()
        for t in terminals:
            t.sync()



# ---------------------------------------------------------------------------
# 1. 登録 → 同期 → ほかの端末に届き、見える決まりどおりに出る
# ---------------------------------------------------------------------------
class DeliveryTests(_Flow):
    """作業長・L-1・コイル・HVC の4台。どれも同じ共有フォルダを見る。"""

    def setUp(self) -> None:
        super().setUp()
        self.a = self.terminal("A", "作業長")
        self.b = self.terminal("B", "L-1")
        self.c = self.terminal("C", "コイル")
        self.h = self.terminal("H", "HVC")
        self.all = (self.a, self.b, self.c, self.h)

    def register_everything(self, day: _dt.date) -> None:
        ok = lambda res: self.assertEqual(res[0], 200, res[1])      # noqa: E731
        ok(self.b.rest(day, "101", shift="1"))                      # L-1 の休み(L-1 端末から)
        ok(self.c.rest(day, "201", shift="2"))                      # コイルの休み(コイル端末から)
        ok(self.a.rest(day, "301", shift="3"))                      # HVC の休み(作業長から)
        ok(self.a.comment(day, "L-1", "B", "作業長からL-1へ"))
        ok(self.a.comment(day, "コイル", "A", "作業長からコイルへ"))
        ok(self.b.comment(day, "L-1", "B", "L-1のB班から"))
        ok(self.c.comment(day, "コイル", "A", "コイルのA班から"))
        self.sync_all(*self.all)

    #: 端末ごとに見えるはずの登録(内容閲覧・削除の選択肢の見出しに含まれる語)
    EXPECTED = {
        "A": {"山田太郎", "鈴木一郎", "田中三郎", "作業長からL-1へ", "作業長からコイルへ",
              "L-1のB班から", "コイルのA班から"},
        # L-1: 休みはコイル以外ぜんぶ、連絡は L-1 宛だけ
        "B": {"山田太郎", "田中三郎", "作業長からL-1へ", "L-1のB班から"},
        # コイル: 休みはコイルだけ、連絡はコイル宛だけ
        "C": {"鈴木一郎", "作業長からコイルへ", "コイルのA班から"},
        # HVC: 休みはコイル以外ぜんぶ、連絡は HVC 宛だけ(無い)
        "H": {"山田太郎", "田中三郎"},
    }
    EVERYTHING = set().union(*EXPECTED.values())

    def assert_visible(self, terminal: Terminal, day: _dt.date) -> None:
        want = self.EXPECTED[terminal.name]
        hidden = self.EVERYTHING - want
        captions = terminal.captions(day)
        seen = {w for w in self.EVERYTHING if any(w in c for c in captions)}
        self.assertEqual(seen, want, f"{terminal.name}({terminal.line}) の内容閲覧: {captions}")
        self.assertEqual(len(captions), len(want), f"{terminal.name}: 二重に届いていない")
        detail = terminal.day(day)["detail"]
        for word in hidden:
            self.assertNotIn(word, detail, f"{terminal.name} の内容閲覧に {word}")
        cell = terminal.cell(day)
        self.assertEqual(cell["count"], len(want), f"{terminal.name} の「登録 N 件」")
        # マスとマウスの説明: 休みは名前、連絡は「ライン:班 コメント有」で出る
        for word in ("山田太郎", "鈴木一郎", "田中三郎"):
            self.assertEqual(word in cell["body"], word in want, f"{terminal.name} のマス: {cell['body']}")
            self.assertEqual(word in cell["tip"], word in want, f"{terminal.name} の説明: {cell['tip']}")
        coil_comment = "コイル:A班 コメント有" in cell["body"]
        l1_comment = "L-1:B班 コメント有" in cell["body"]
        self.assertEqual(coil_comment, terminal.name in ("A", "C"), cell["body"])
        self.assertEqual(l1_comment, terminal.name in ("A", "B"), cell["body"])

    def test_登録がほかの端末に届き_見える決まりどおりに出る(self) -> None:
        day = next_month_day(10)
        self.register_everything(day)
        self.assertEqual(len(self.source_rows()), 7, "共有には全部が1回ずつ")
        for terminal in self.all:
            with self.subTest(terminal=terminal.name):
                self.assert_visible(terminal, day)

    def test_履歴も同じ決まりで数える(self) -> None:
        day = last_month_day(12)
        self.register_everything(day)
        for terminal in self.all:
            with self.subTest(terminal=terminal.name):
                body = terminal.get("/api/history")
                months = {(y["year"], m["month"]): m["count"]
                          for y in body["choices"]["years"] for m in y["months"]}
                want = len(self.EXPECTED[terminal.name])
                self.assertEqual(months[(day.year, day.month)], want, "月の選択肢の件数")
                self.assertEqual(body["month"]["count"], want, "履歴の月の件数")
                detail = terminal.get(f"/api/history/day/{day.strftime('%Y/%m/%d')}")
                self.assertEqual(detail["count"], want, "履歴の内容閲覧の件数")
                for word in self.EVERYTHING - self.EXPECTED[terminal.name]:
                    self.assertNotIn(word, detail["detail"])

    def test_印刷も同じ決まり(self) -> None:
        day = next_month_day(10)
        self.register_everything(day)
        status, html = self.b.call("GET", f"/print?year={day.year}&month={day.month}")
        self.assertEqual(status, 200)
        self.assertIn("山田太郎", html)
        self.assertNotIn("鈴木一郎", html)

    def test_自動同期だけでも届く(self) -> None:
        """「いますぐ同期」を押さなくても、間隔ごとの同期で届く(既定20秒。ここは5秒)。"""
        day = next_month_day(11)
        sender = self.terminal("S", "L-1", autosync=True, interval=5)
        watcher = self.terminal("W", "作業長", autosync=True, interval=5)
        self.assertEqual(sender.rest(day, "102")[0], 200)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if any("佐藤花子" in c for c in watcher.captions(day)):
                break
            time.sleep(0.5)
        else:
            self.fail("自動同期で届きませんでした")
        self.assertEqual(sender.status()["pending"], 0)


# ---------------------------------------------------------------------------
# 2. 削除
# ---------------------------------------------------------------------------
class DeleteTests(_Flow):
    def setUp(self) -> None:
        super().setUp()
        self.a = self.terminal("A", "作業長")
        self.b = self.terminal("B", "L-1")
        self.c = self.terminal("C", "コイル")
        self.day = next_month_day(12)

    def test_見える登録は消せて_ほかの端末からも消える(self) -> None:
        self.assertEqual(self.a.rest(self.day, "101", shift="1")[0], 200)
        self.assertEqual(self.a.rest(self.day, "102")[0], 200)
        self.sync_all(self.a, self.b, self.c)
        status, body = self.b.delete(self.day, [self.b.item(self.day, "山田太郎")])
        self.assertEqual(status, 200, body)
        self.sync_all(self.b, self.a)
        self.assertEqual([r["登録内容"] for r in self.source_rows()], ["佐藤花子"])
        self.assertEqual(self.a.captions(self.day), ["[休み] 佐藤花子"])
        history = self.source_rows(config.TABLE_DEL_HISTORY)
        self.assertEqual([h["登録内容"] for h in history], ["山田太郎"])

    def test_見えない登録は消せない(self) -> None:
        self.assertEqual(self.c.rest(self.day, "201", shift="1")[0], 200)
        self.assertEqual(self.c.comment(self.day, "コイル", "A", "コイルの連絡")[0], 200)
        self.sync_all(self.c, self.b)
        self.assertEqual(self.b.captions(self.day), [], "L-1 にコイルの休み・連絡は出ない")
        # 古い画面・手で組んだ要求で、コイル端末の ID を投げても通さない
        for item in self.c.day(self.day)["items"]:
            status, body = self.b.delete(self.day, [item])
            self.assertEqual(status, 403, body)
            self.assertEqual(body["error"]["code"], "not_your_line")
        self.sync_all(self.b, self.c)
        self.assertEqual(len(self.source_rows()), 2)

    def test_2台がほぼ同時に同じ登録を消しても_ほかは消えない(self) -> None:
        self.assertEqual(self.b.rest(self.day, "101", shift="1")[0], 200)
        self.assertEqual(self.b.rest(self.day, "102")[0], 200)
        self.assertEqual(self.a.comment(self.day, "L-1", "B", "残業なし")[0], 200)
        self.assertEqual(self.b.comment(self.day, "L-1", "B", "残業なし")[0], 200)
        self.sync_all(self.a, self.b)
        target_a = self.a.item(self.day, "山田太郎")
        target_b = self.b.item(self.day, "山田太郎")
        results: list = []
        threads = [threading.Thread(target=lambda t=t, i=i: results.append(t.delete(self.day, [i])))
                   for t, i in ((self.a, target_a), (self.b, target_b))]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(sorted(r[0] for r in results), [200, 200])
        self.sync_all(self.a, self.b)
        names = sorted(r["登録内容"] for r in self.source_rows())
        self.assertEqual(names, ["佐藤花子", "残業なし", "残業なし"])
        for t in (self.a, self.b):
            self.assertEqual(len(t.captions(self.day)), 3)

    def test_同じ文面の連絡を片方だけ消す(self) -> None:
        """連絡は識別コードが「-」で同じ。消すのは選んだ1行だけ(ほかの端末の行は残る)。"""
        self.assertEqual(self.a.comment(self.day, "L-1", "B", "残業なし")[0], 200)
        self.assertEqual(self.b.comment(self.day, "L-1", "B", "残業なし")[0], 200)
        self.sync_all(self.a, self.b)
        items = self.b.day(self.day)["items"]
        self.assertEqual(len(items), 2)
        self.assertEqual(self.b.delete(self.day, [items[0]])[0], 200)
        self.sync_all(self.b, self.a)
        self.assertEqual(len(self.source_rows()), 1)
        self.assertEqual(len(self.a.captions(self.day)), 1)

    def test_消した番号が使い回されても_ほかの人の登録を消さない(self) -> None:
        """**共有の ID は使い回される**(``INTEGER PRIMARY KEY``)。

        A が山田さんの休み(いちばん大きい ID)を消して送る → 共有からその行が消える →
        C がコイルの鈴木さんの休みを登録すると、**同じ ID** が振られる。
        そこへ、まだ取り込んでいない B が古い写しの山田さんを消すと、B の削除は
        「ID = その番号」で共有へ届く。以前はそれで**鈴木さんの休みが消えた**
        (L-1 の端末から、見えもしないコイルの休みが消える。消した人は気づけない)。
        """
        self.assertEqual(self.b.rest(self.day, "102")[0], 200)
        self.assertEqual(self.b.rest(self.day, "101", shift="1")[0], 200)
        self.sync_all(self.a, self.b, self.c)
        yamada = self.b.item(self.day, "山田太郎")

        self.assertEqual(self.a.delete(self.day, [self.a.item(self.day, "山田太郎")])[0], 200)
        self.a.sync()
        self.assertEqual(self.c.rest(self.day, "201", shift="1")[0], 200)
        self.c.sync()
        reused = [r for r in self.source_rows() if r["登録内容"] == "鈴木一郎"]
        self.assertEqual(reused[0]["ID"], yamada["id"], "前提: 同じ番号が振られた")

        # B はまだ取り込んでいない(同期の合間・共有が落ちていた等)。古い写しの山田さんを消す
        status, body = self.b.delete(self.day, [yamada])
        self.assertEqual(status, 200, body)
        self.sync_all(self.b, self.a, self.c)
        self.assertEqual(sorted(r["登録内容"] for r in self.source_rows()),
                         ["佐藤花子", "鈴木一郎"], "コイルの鈴木さんの休みは残る")
        self.assertEqual(self.c.captions(self.day), ["[休み] 鈴木一郎 (1直)"])

    def reuse_id(self, delete_on: _dt.date, words: str, register) -> dict:
        """B の古い写しの行(``words``)を A が消して送り、C が登録して同じ番号を振らせる。

        戻り値は B の古い写しの行(B はまだ取り込んでいない)。
        """
        stale = self.b.item(delete_on, words)
        self.assertEqual(self.a.delete(delete_on, [self.a.item(delete_on, words)])[0], 200)
        self.a.sync()
        register()
        self.sync_registered()
        ids = [r["ID"] for r in self.source_rows()]
        self.assertIn(stale["id"], ids, "前提: 同じ番号が別の登録に振られた")
        return stale

    def sync_registered(self) -> None:
        self.c.sync()
        self.a.sync()

    def test_番号が同じ人の別の日の休みに使い回されても消さない(self) -> None:
        other_day = self.day + _dt.timedelta(days=1)
        self.assertEqual(self.b.rest(self.day, "102")[0], 200)
        self.assertEqual(self.b.rest(self.day, "101", shift="1")[0], 200)
        self.sync_all(self.a, self.b)
        stale = self.reuse_id(self.day, "山田太郎",
                              lambda: self.assertEqual(self.a.rest(other_day, "101", shift="2")[0], 200))
        self.assertEqual(self.b.delete(self.day, [stale])[0], 200)
        self.sync_all(self.b, self.a)
        self.assertEqual(self.a.captions(other_day), ["[休み] 山田太郎 (2直)"],
                         "次の日の山田さんの休みは残る")

    def test_番号がほかのラインの同じ文面の連絡に使い回されても消さない(self) -> None:
        self.assertEqual(self.b.rest(self.day, "102")[0], 200)
        self.assertEqual(self.b.comment(self.day, "L-1", "B", "残業なし")[0], 200)
        self.sync_all(self.a, self.b, self.c)
        stale = self.reuse_id(self.day, "残業なし",
                              lambda: self.assertEqual(self.c.comment(self.day, "コイル", "A", "残業なし")[0], 200))
        self.assertEqual(self.b.delete(self.day, [stale])[0], 200)
        self.sync_all(self.b, self.c)
        self.assertEqual(self.c.captions(self.day), ["[連絡] 残業なし"], "コイルの連絡は残る")

    def test_消したあと同じ人を登録し直すと_ほかの端末には新しいほうが出る(self) -> None:
        self.assertEqual(self.b.rest(self.day, "101", shift="1")[0], 200)
        self.sync_all(self.a, self.b)
        self.assertEqual(self.b.delete(self.day, [self.b.item(self.day, "山田太郎")])[0], 200)
        self.assertEqual(self.b.rest(self.day, "101", shift="3")[0], 200)
        self.sync_all(self.b, self.a)
        self.assertEqual([r["直"] for r in self.source_rows()], ["3"])
        self.assertEqual(self.a.captions(self.day), ["[休み] 山田太郎 (3直)"])

    def test_ほかの端末が消した日に連絡を足す(self) -> None:
        """消された休みと、あとから足した連絡は別の行。足した連絡は消えない。"""
        self.assertEqual(self.b.rest(self.day, "101", shift="1")[0], 200)
        self.sync_all(self.a, self.b)
        self.assertEqual(self.a.delete(self.day, [self.a.item(self.day, "山田太郎")])[0], 200)
        # B はまだ山田さんが見えている画面のまま、同じ日に連絡を足す
        self.assertEqual(self.b.comment(self.day, "L-1", "B", "山田さん休みの件")[0], 200)
        self.sync_all(self.a, self.b)
        self.assertEqual([r["登録内容"] for r in self.source_rows()], ["山田さん休みの件"])
        for t in (self.a, self.b):
            self.assertEqual(len(t.captions(self.day)), 1, t.captions(self.day))


# ---------------------------------------------------------------------------
# 3. 同じ人・同じ日の休みを2台がほぼ同時に登録する
# ---------------------------------------------------------------------------
class DoubleRegisterTests(_Flow):
    def setUp(self) -> None:
        super().setUp()
        self.a = self.terminal("A", "作業長")
        self.b = self.terminal("B", "L-1")
        self.day = next_month_day(14)

    def test_後から送ったほうは取りやめて_その端末に知らせる(self) -> None:
        self.assertEqual(self.a.rest(self.day, "101", shift="1")[0], 200)
        self.assertEqual(self.b.rest(self.day, "101", shift="2")[0], 200)
        self.a.sync()
        body = self.b.sync()
        self.assertEqual(body["sync"]["skipped"]["count"], 1, body["sync"])
        self.assertIn("山田太郎", body["sync"]["skipped"]["text"])
        self.sync_all(self.a, self.b)
        self.assertEqual(len(self.source_rows()), 1)
        for t in (self.a, self.b):
            self.assertEqual(t.captions(self.day), ["[休み] 山田太郎 (1直)"])
        # 確かめたと押せば知らせは消える
        status, after = self.b.post("/api/sync/skipped/ack", {})
        self.assertEqual(status, 200)
        self.assertEqual(after["skipped"]["count"], 0)

    def test_取り込んだあとは登録の時点で断る(self) -> None:
        self.assertEqual(self.a.rest(self.day, "101", shift="1")[0], 200)
        self.sync_all(self.a, self.b)
        status, body = self.b.rest(self.day, "101", shift="2")
        self.assertEqual(status, 409, body)
        self.assertEqual(body["error"]["code"], "already_registered")

    def test_同時に送っても1行だけ(self) -> None:
        """2台の送信が重なっても、共有に同じ人・同じ日が2行できない。"""
        for n in range(1, 6):
            day = next_month_day(n)
            self.assertEqual(self.a.rest(day, "101", shift="1")[0], 200)
            self.assertEqual(self.b.rest(day, "101", shift="2")[0], 200)
            # 登録の直後の送信(背景)が終わってから、2台いっせいに送る
            barrier = threading.Barrier(2)

            def go(t: Terminal) -> None:
                barrier.wait()
                t.sync()

            threads = [threading.Thread(target=go, args=(t,)) for t in (self.a, self.b)]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
        rows = self.source_rows()
        keys = [(r["日付"], r["識別コード"]) for r in rows]
        self.assertEqual(len(keys), len(set(keys)), f"二重登録: {sorted(keys)}")


# ---------------------------------------------------------------------------
# 4. 共有フォルダが見えないあいだ
# ---------------------------------------------------------------------------
class OfflineTests(_Flow):
    def setUp(self) -> None:
        super().setUp()
        self.a = self.terminal("A", "作業長")
        self.b = self.terminal("B", "L-1")
        self.day = next_month_day(16)
        self.down_path = self.base / "共有_落ちている"

    def down(self) -> None:
        """共有フォルダが見えなくなる(ネットワーク断・共有の停止)。"""
        self.wait_idle(self.a, self.b)
        self.shared.rename(self.down_path)
        self.addCleanup(self.up)

    def up(self) -> None:
        if self.down_path.exists():
            self.down_path.rename(self.shared)

    def wait_idle(self, *terminals: Terminal) -> None:
        for t in terminals:
            deadline = time.monotonic() + 30
            while t.status()["busy"] and time.monotonic() < deadline:
                time.sleep(0.1)

    def test_落ちているあいだの登録は送信待ちに残り_戻ったら送られる(self) -> None:
        self.assertEqual(self.b.rest(self.day, "102")[0], 200)
        self.sync_all(self.a, self.b)
        self.down()

        status, body = self.b.rest(self.day, "101", shift="1")
        self.assertEqual(status, 200, "見えないだけなら断らない(送信待ちに残す)")
        self.assertEqual(self.b.comment(self.day, "L-1", "B", "落ちているあいだの連絡")[0], 200)
        self.assertEqual(self.b.delete(self.day, [self.b.item(self.day, "佐藤花子")])[0], 200)
        result = self.b.sync()
        self.assertTrue(result["sync"]["offline"], result)
        self.assertIn("送れません", result["message"])
        sync = self.b.status()
        self.assertTrue(sync["offline"])
        # 休み・連絡・削除の3つ。**操作の数で数える**(削除1回=1件。VER4.2.10)
        self.assertEqual(sync["pending"], 3, sync)
        self.assertIn("未送信 3 件", sync["text"])
        # 打った端末では、送れていなくても出ている
        self.assertEqual(sorted(self.b.captions(self.day)),
                         ["[休み] 山田太郎 (1直)", "[連絡] 落ちているあいだの連絡"])
        self.assertEqual([r["登録内容"] for r in self.source_rows_down()], ["佐藤花子"])

        self.up()
        result = self.b.sync()
        self.assertFalse(result["sync"]["offline"], result)
        self.assertEqual(result["sync"]["pending"], 0)
        self.a.sync()
        self.assertEqual(sorted(self.a.captions(self.day)),
                         ["[休み] 山田太郎 (1直)", "[連絡] 落ちているあいだの連絡"])
        self.assertEqual(sorted(r["登録内容"] for r in self.source_rows()),
                         ["山田太郎", "落ちているあいだの連絡"])

    def source_rows_down(self) -> list[dict]:
        return _web.read_source(self.down_path / config.SOURCE_FILE_DATA, config.TABLE_DATA)

    def test_起動したときに落ちていても登録でき_戻れば送る(self) -> None:
        self.down()
        self.b.restart()
        sync = self.b.status()
        self.assertTrue(sync["offline"], sync)
        self.assertIn("見えません", sync["message"])
        self.assertEqual(self.b.rest(self.day, "101", shift="1")[0], 200)
        # 「いますぐ同期」を押す。**「いま同期しています」ではなく、見えないと伝える**
        status, body = self.b.post("/api/sync/now", {})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["sync"]["offline"], body)
        self.assertIn("見えません", body["message"])
        self.assertEqual(body["sync"]["pending"], 1)
        self.up()
        self.b.sync()
        self.assertEqual(self.b.status()["pending"], 0)
        self.a.sync()
        self.assertEqual(self.a.captions(self.day), ["[休み] 山田太郎 (1直)"])

    def test_落ちているあいだにほかの端末が同じ人を登録していたら_戻ったときに知らせる(self) -> None:
        self.down()
        self.assertEqual(self.b.rest(self.day, "101", shift="2")[0], 200)
        self.up_for_a_only()
        self.up()
        body = self.b.sync()
        self.assertEqual(body["sync"]["skipped"]["count"], 1, body["sync"])
        self.b.sync()
        self.assertEqual(self.b.captions(self.day), ["[休み] 山田太郎 (1直)"])
        self.assertEqual(len(self.source_rows()), 1)

    def up_for_a_only(self) -> None:
        """B からは見えないまま、A だけが登録して送る(A は共有へ届く)。"""
        self.down_path.rename(self.shared)
        self.assertEqual(self.a.rest(self.day, "101", shift="1")[0], 200)
        self.a.sync()
        self.wait_idle(self.a)
        self.shared.rename(self.down_path)


# ---------------------------------------------------------------------------
# 5. マスタ管理(班員名簿・アクセス権限)
# ---------------------------------------------------------------------------
class MasterTests(_Flow):
    def setUp(self) -> None:
        super().setUp()
        self.a = self.terminal("A", "作業長")
        self.b = self.terminal("B", "L-1")
        self.h = self.terminal("H", "HVC")
        self.day = next_month_day(18)

    def master(self, path: str, table: str = config.TABLE_MEMBER, **body) -> dict:
        status, reply = self.a.post(path, {"table": table, "password": _web.ADMIN_PASSWORD, **body})
        self.assertEqual(status, 200, reply)
        return reply

    def names(self, t: Terminal) -> dict[str, str]:
        return {w["code"]: w["name"] for line in t.get("/api/workers")["lines"]
                for group in line["groups"] for w in group["workers"]}

    def test_名簿を直す_足す_消すとほかの端末に届く(self) -> None:
        self.master("/api/master/save", key="101", values={"名前": "山田太一"})
        self.assertEqual(self.names(self.a)["101"], "山田太一", "直した端末にはすぐ")
        self.master("/api/master/add", values={"管理番号": "103", "苗字": "伊藤", "班": "B",
                                               "名前": "伊藤四郎", "読み": "いとう", "担当ライン": "L-1"})
        self.master("/api/master/delete", key="102")
        self.b.sync()
        self.assertEqual(self.names(self.b), {"101": "山田太一", "103": "伊藤四郎"})
        self.assertEqual(self.b.rest(self.day, "103", shift="1")[0], 200)
        status, body = self.b.rest(self.day, "102")
        self.assertEqual(status, 422, body)
        self.assertEqual(body["error"]["code"], "not_listed")

    def test_異動しても先の休みは登録したときの班とラインのまま(self) -> None:
        """現場が了承した仕様: 休みは登録したときの班・ラインを写して持つ。"""
        self.assertEqual(self.b.rest(self.day, "101", shift="1")[0], 200)
        self.sync_all(self.a, self.b, self.h)
        self.master("/api/master/save", key="101", values={"班": "C", "担当ライン": "HVC"})
        self.sync_all(self.b, self.h)
        self.assertNotIn("101", self.names(self.b), "L-1 の選択肢から外れる")
        self.assertIn("101", self.names(self.h), "HVC の選択肢に入る")
        self.assertIn("L-1:B班:山田太郎 休", self.b.cell(self.day)["body"])
        # 連絡の宛先(ライン・班)も名簿に合う
        targets = self.a.get("/api/comment-targets")
        self.assertIn("C", targets["groups"]["HVC"])

    def test_アクセス権限を直すと端末のラインが合う(self) -> None:
        self.assertEqual(self.a.comment(self.day, "L-1", "B", "L-1宛")[0], 200)
        self.a.sync()
        reply = self.master("/api/master/add", table="アクセス権限",
                            values={"ログインID": self.b.login, "PC名": self.b.pc, "権限": "HVC"})
        self.assertIn("取り込み直し", reply["message"])
        self.b.sync()
        self.assertEqual(self.b.my_line(), "HVC")
        self.assertEqual(self.b.captions(self.day), [], "L-1 宛の連絡はもう見えない")
        status, body = self.b.set_line("L-1")
        self.assertEqual(status, 422, body)
        self.assertEqual(body["error"]["code"], "not_granted")
        # 表を直す → 次の同期で合う
        key = str(self.master_rows("アクセス権限")[0]["管理番号"])
        self.master("/api/master/save", table="アクセス権限", key=key, values={"権限": "L-1"})
        self.b.sync()
        self.assertEqual(self.b.my_line(), "L-1")
        self.assertEqual(self.b.captions(self.day), ["[連絡] L-1宛"])

    def test_外のツールの直し_差し替えたマスタDBも開いたままの端末に届く(self) -> None:
        """WAL のまま外のツールが名簿を直す → 届く。マスタDBを .db に差し替える → 探し直して届く。"""
        writer = sqlite3.connect(self.master_db)            # 外のツール(開いたまま)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute(f'UPDATE "{config.TABLE_MEMBER}" SET "名前"=? WHERE "管理番号"=?',
                       ("山田次郎", "101"))
        writer.commit()
        self.b.sync()
        self.assertEqual(self.names(self.b)["101"], "山田次郎")
        writer.execute("PRAGMA journal_mode=DELETE")
        writer.close()

        replaced = self.master_db.with_suffix(".db")
        conn = sqlite3.connect(self.master_db)
        try:
            conn.execute(f'UPDATE "{config.TABLE_MEMBER}" SET "名前"=? WHERE "管理番号"=?',
                         ("山田三郎", "101"))
            conn.commit()
        finally:
            conn.close()
        self.master_db.rename(replaced)
        self.b.sync()
        self.assertEqual(self.names(self.b)["101"], "山田三郎")
        self.h.sync()
        self.assertIn("101", self.names(self.a))

    def master_rows(self, table: str) -> list[dict]:
        return _web.read_source(self.master_db, table)


# ---------------------------------------------------------------------------
# 6. 端末のラインを変えたとき
# ---------------------------------------------------------------------------
class LineChangeTests(_Flow):
    def setUp(self) -> None:
        super().setUp()
        self.a = self.terminal("A", "作業長")
        self.b = self.terminal("B", "L-1")
        self.day = next_month_day(20)
        self.assertEqual(self.a.rest(self.day, "101", shift="1")[0], 200)
        self.assertEqual(self.a.rest(self.day, "201", shift="1")[0], 200)
        self.assertEqual(self.a.comment(self.day, "コイル", "A", "コイル宛")[0], 200)
        self.sync_all(self.a, self.b)

    def test_設定で変えるとすぐ見えるものが変わる(self) -> None:
        self.assertEqual(self.b.captions(self.day), ["[休み] 山田太郎 (1直)"])
        status, body = self.b.set_line("コイル")
        self.assertEqual(status, 200, body)
        self.assertEqual(sorted(self.b.captions(self.day)), ["[休み] 鈴木一郎 (1直)", "[連絡] コイル宛"])
        self.assertEqual(self.b.cell(self.day)["count"], 2)
        self.assertEqual(sorted(self.b.worker_codes()), ["201", "202"])
        # 消せるものも変わる(さっきまで見えていた L-1 の休みはもう消せない)
        old = self.a.item(self.day, "山田太郎")
        status, body = self.b.delete(self.day, [old])
        self.assertEqual(status, 403, body)
        # 端末一覧にもすぐ載る(マスタ確認で見る)
        rows = {r["PC名"]: r["ライン"] for r in self.source_rows("端末設定")}
        self.assertEqual(rows[self.b.pc], "コイル")

    def test_次のライン予約は次の起動で効く(self) -> None:
        key = f"{self.b.login}@{self.b.pc}"
        status, body = self.a.post("/api/master/save", {
            "table": "端末設定", "key": key, "values": {"次のライン": "コイル"},
            "password": _web.ADMIN_PASSWORD})
        self.assertEqual(status, 200, body)
        self.b.sync()
        self.assertEqual(self.b.my_line(), "L-1", "使っている最中には切り替えない")
        self.b.restart()
        self.assertEqual(self.b.my_line(), "コイル")
        self.assertEqual(sorted(self.b.captions(self.day)), ["[休み] 鈴木一郎 (1直)", "[連絡] コイル宛"])
        self.b.restart()
        self.assertEqual(self.b.my_line(), "コイル", "予約は1度だけ効く")


# ---------------------------------------------------------------------------
# 7. 開いたままの古い画面から操作する
# ---------------------------------------------------------------------------
class StaleScreenTests(_Flow):
    def setUp(self) -> None:
        super().setUp()
        self.a = self.terminal("A", "作業長")
        self.b = self.terminal("B", "L-1")
        self.c = self.terminal("C", "コイル")
        self.day = next_month_day(22)

    def test_ほかの端末が先に消した登録を古い画面から消すと断る(self) -> None:
        self.assertEqual(self.b.rest(self.day, "102")[0], 200)
        self.assertEqual(self.b.rest(self.day, "101", shift="1")[0], 200)
        self.sync_all(self.a, self.b)
        stale = self.b.item(self.day, "山田太郎")              # B の削除の窓が開いたまま
        self.assertEqual(self.a.delete(self.day, [self.a.item(self.day, "山田太郎")])[0], 200)
        self.sync_all(self.a, self.b)
        status, body = self.b.delete(self.day, [stale])
        self.assertEqual(status, 409, body)
        self.assertEqual(body["error"]["code"], "gone")
        self.assertEqual([r["登録内容"] for r in self.source_rows()], ["佐藤花子"])

    def test_番号が別のラインの登録に使い回されていたら_先に変わったと伝える(self) -> None:
        """古い窓の行が、取り込み直しで**見えないほかのラインの登録**に入れ替わっていた。

        断るのは当然として、「ほかのラインの登録は削除できません」と言われると、
        自分のラインの山田さんを消そうとした人には訳が分からない。
        「先に変わっていた・画面を更新して」と伝える。
        """
        self.assertEqual(self.b.rest(self.day, "102")[0], 200)
        self.assertEqual(self.b.rest(self.day, "101", shift="1")[0], 200)
        self.sync_all(self.a, self.b, self.c)
        stale = self.b.item(self.day, "山田太郎")
        self.assertEqual(self.a.delete(self.day, [self.a.item(self.day, "山田太郎")])[0], 200)
        self.a.sync()
        self.assertEqual(self.c.rest(self.day, "201", shift="1")[0], 200)
        self.c.sync()
        self.b.sync()                                       # 取り込み直し: 同じ番号がコイルの行に
        status, body = self.b.delete(self.day, [stale])
        self.assertEqual(status, 409, body)
        self.assertEqual(body["error"]["code"], "gone")
        self.assertEqual(sorted(r["登録内容"] for r in self.source_rows()), ["佐藤花子", "鈴木一郎"])

    def test_作業長の古い窓の連絡が_ほかのラインの同じ文面に入れ替わっていたら断る(self) -> None:
        """作業長はどのラインの登録も見える・消せる。だから中身の印で見分けるしかない。

        L-1 の「残業なし」を開いたまま、ほかの端末がそれを消し、コイルが同じ文面の
        「残業なし」を出して同じ番号が振られた。取り込み直したあとで古い窓から消すと、
        以前は**コイルの連絡が消えた**(文面と区分・識別コード「-」が同じなので印が合った)。
        """
        self.assertEqual(self.b.rest(self.day, "102")[0], 200)
        self.assertEqual(self.b.comment(self.day, "L-1", "B", "残業なし")[0], 200)
        self.sync_all(self.a, self.b, self.c)
        stale = self.a.item(self.day, "残業なし")             # 作業長の削除の窓が開いたまま
        self.assertEqual(self.b.delete(self.day, [self.b.item(self.day, "残業なし")])[0], 200)
        self.b.sync()
        self.assertEqual(self.c.comment(self.day, "コイル", "A", "残業なし")[0], 200)
        self.c.sync()
        self.a.sync()
        self.assertEqual(self.a.item(self.day, "残業なし")["id"], stale["id"], "前提: 同じ番号")
        status, body = self.a.delete(self.day, [stale])
        self.assertEqual(status, 409, body)
        self.assertEqual(body["error"]["code"], "gone")
        self.assertEqual(self.c.captions(self.day), ["[連絡] 残業なし"])

    def test_名簿から消えた人を古い窓から登録すると断る(self) -> None:
        self.assertIn("102", self.b.worker_codes())          # B の登録の窓が開いたまま
        status, body = self.a.post("/api/master/delete", {
            "table": config.TABLE_MEMBER, "key": "102", "password": _web.ADMIN_PASSWORD})
        self.assertEqual(status, 200, body)
        self.b.sync()
        status, body = self.b.rest(self.day, "102")
        self.assertEqual(status, 422, body)
        self.assertEqual(body["error"]["code"], "not_listed")
        self.assertEqual(self.source_rows(), [])

    def test_無くなった班へ古い窓から連絡すると断る(self) -> None:
        status, body = self.a.post("/api/master/save", {
            "table": config.TABLE_MEMBER, "key": "101", "values": {"班": "C"},
            "password": _web.ADMIN_PASSWORD})
        self.assertEqual(status, 200, body)
        self.b.sync()
        status, body = self.b.comment(self.day, "L-1", "B", "古い窓からの連絡")
        self.assertEqual(status, 422, body)
        self.assertEqual(body["error"]["code"], "not_listed")

    def test_ラインが変わったあとに古い窓から前のラインへ連絡すると断る(self) -> None:
        status, body = self.a.post("/api/master/add", {
            "table": "アクセス権限", "values": {"ログインID": self.b.login, "PC名": self.b.pc,
                                               "権限": "HVC"},
            "password": _web.ADMIN_PASSWORD})
        self.assertEqual(status, 200, body)
        self.b.sync()
        status, body = self.b.comment(self.day, "L-1", "B", "前のラインのつもり")
        self.assertEqual(status, 422, body)
        self.assertEqual(body["error"]["code"], "line_locked")
        # 古い窓の休み(L-1 の人)も、いまの HVC 端末の選択肢には無いので断る
        status, body = self.b.rest(self.day, "101", shift="1")
        self.assertEqual(status, 422, body)


# ---------------------------------------------------------------------------
# 2'. 削除の転送(1つのプロセスの中で速く。上の DeleteTests の芯だけ)
# ---------------------------------------------------------------------------
class ForwardDeleteTests(unittest.TestCase):
    """手元で消した行を、共有へ **ID で** 消しに行くときの照らし合わせ。"""

    def setUp(self) -> None:
        from calendar_app import db
        from calendar_app.dbkit import outbox_sync

        outbox_sync.reset_op_id_cache()
        self.addCleanup(outbox_sync.reset_op_id_cache)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = _web.make_source(Path(tmp.name), config.SOURCE_FILE_DATA, _web.SOURCE_SCHEMA)
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def put(self, *rows: tuple) -> None:
        """共有に行を置く: (ID, 日付, 区分, 登録内容, 識別コード, 班, ライン)。"""
        conn = sqlite3.connect(self.path)
        try:
            conn.executemany(
                f'INSERT OR REPLACE INTO "{config.TABLE_DATA}" '
                '("ID","日付","区分","登録内容","識別コード","班","ライン") VALUES (?,?,?,?,?,?,?)', rows)
            conn.commit()
        finally:
            conn.close()

    def drop(self, record_id: int) -> None:
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(f'DELETE FROM "{config.TABLE_DATA}" WHERE "ID"=?', (record_id,))
            conn.commit()
        finally:
            conn.close()

    def delete_here_and_forward(self, record_id: int):
        """取り込んだ写しで消し(予約される)、取り込み直さずに共有へ送る。"""
        from calendar_app.dbkit import source_db
        from calendar_app.importer import import_source
        from calendar_app.repository import Repository
        from calendar_app.sync import deletes

        import_source(self.conn, self.path)
        self.assertEqual(Repository(self.conn).delete_records_by_ids([record_id]), 1)
        return lambda: self._forward(deletes, source_db)

    def _forward(self, deletes, source_db) -> None:
        with source_db.connect(self.path, read_only=False) as source:
            sent, errors = deletes.forward_pending_deletes(self.conn, source, config.TABLE_DATA)
        self.assertEqual(errors, [])
        self.assertEqual(deletes.pending_delete_count(self.conn), 0, "予約は片づく")

    def left(self) -> list[str]:
        return [r["登録内容"] for r in _web.read_source(self.path, config.TABLE_DATA)]

    def test_同じ中身ならIDで消す(self) -> None:
        self.put((1, "2026/08/03", "休み", "山田太郎", "101", "B", "L-1"))
        self.delete_here_and_forward(1)()
        self.assertEqual(self.left(), [])

    def test_共有の日付がISO形式でも消せる(self) -> None:
        """別の手段で作った共有は ``2026-08-03`` で持つことがある(取り込みは読める)。"""
        self.put((1, "2026-08-03", "休み", "山田太郎", "101", "B", "L-1"))
        self.delete_here_and_forward(1)()
        self.assertEqual(self.left(), [])

    def test_番号が別の登録に使い回されていたら消さない(self) -> None:
        self.put((1, "2026/08/03", "休み", "山田太郎", "101", "B", "L-1"))
        forward = self.delete_here_and_forward(1)
        # 送る前に、ほかの端末が同じ行を消し、別の登録に同じ番号が振られた
        self.drop(1)
        self.put((1, "2026/08/03", "休み", "鈴木一郎", "201", "A", "コイル"))
        forward()
        self.assertEqual(self.left(), ["鈴木一郎"])

    def test_もう無ければ何もしない(self) -> None:
        self.put((1, "2026/08/03", "休み", "山田太郎", "101", "B", "L-1"),
                 (2, "2026/08/03", "休み", "佐藤花子", "102", "日勤", "L-1"))
        forward = self.delete_here_and_forward(1)
        self.drop(1)
        forward()
        self.assertEqual(self.left(), ["佐藤花子"])


# ---------------------------------------------------------------------------
# 本物のブラウザ: 開いたままの画面に、ほかの端末の登録・削除が届く
# ---------------------------------------------------------------------------
class OpenScreenBrowserTests(_Flow):
    """L-1 の端末の画面を本物のブラウザで開いたまま、作業長の端末が登録・削除する。

    画面は帯の同期の状態(``last_received_at``)を見て描き直す。開き直さなくても
    ほかの端末の登録が出る・消える、古い削除の窓からは消さずに知らせる、を見る。
    """

    @classmethod
    def setUpClass(cls) -> None:
        from tests._browser import chromium_or_skip

        cls.pw, cls.browser = chromium_or_skip()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.pw.stop()

    def setUp(self) -> None:
        super().setUp()
        self.a = self.terminal("A", "作業長")
        # B は画面をブラウザに名乗らせる(試験からは名乗らない)。自動同期は5秒ごと
        self.b = self.terminal("B", "L-1", autosync=True, interval=5, claim=False)
        self.day = _dt.date.today()
        self.key = self.day.strftime("%Y/%m/%d")
        ctx = self.browser.new_context(viewport={"width": 1400, "height": 900})
        self.addCleanup(ctx.close)
        self.page = ctx.new_page()
        self.page.set_default_timeout(15000)
        self.errors: list[str] = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.goto(f"http://127.0.0.1:{self.b.port}/calendar?t={_web.TOKEN}")
        self.page.wait_for_selector(f'button.day[data-date="{self.key}"]')

    def cell_text(self) -> str:
        return self.page.locator(f'button.day[data-date="{self.key}"]').inner_text()

    def wait_text(self, ok, what: str, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while not ok(self.cell_text()):
            if time.monotonic() > deadline:
                self.fail(f"{what}: {self.cell_text()!r}")
            self.page.wait_for_timeout(250)

    def test_ほかの端末の登録と削除が開いたままの画面に出る(self) -> None:
        self.assertEqual(self.a.rest(self.day, "101", shift="1")[0], 200)
        self.assertEqual(self.a.rest(self.day, "201", shift="1")[0], 200)  # コイルは L-1 に出ない
        self.a.sync()
        self.wait_text(lambda t: "山田太郎" in t, "作業長の登録が出ない")
        self.assertNotIn("鈴木一郎", self.cell_text())
        self.assertEqual(self.a.delete(self.day, [self.a.item(self.day, "山田太郎")])[0], 200)
        self.a.sync()
        self.wait_text(lambda t: "山田太郎" not in t, "作業長の削除が消えない")
        self.assertEqual(self.errors, [])

    def test_古い削除の窓からは消さずに知らせ_描き直す(self) -> None:
        self.assertEqual(self.a.rest(self.day, "101", shift="1")[0], 200)
        self.a.sync()
        self.wait_text(lambda t: "山田太郎" in t, "作業長の登録が出ない")
        page = self.page
        page.locator(f'button.day[data-date="{self.key}"]').click()
        page.locator(".modal button", has_text="削除").first.click()
        page.locator("#del-list input[type=checkbox]").first.check()
        # 窓を開いたまま、作業長が先に消して送り、B も取り込む
        self.assertEqual(self.a.delete(self.day, [self.a.item(self.day, "山田太郎")])[0], 200)
        self.a.sync()
        before = len(self.source_rows(config.TABLE_DEL_HISTORY))
        deadline = time.monotonic() + 30
        while self.b.status()["last_received_at"] == "" or any(
                "山田太郎" in c for c in self.b.captions(self.day)):
            if time.monotonic() > deadline:
                self.fail("B が取り込みません")
            page.wait_for_timeout(250)
        page.locator(".modal button", has_text="選んだものを削除").click()
        page.locator(".modal button", has_text="削除する").click()
        page.wait_for_selector(".modal >> text=削除できません")
        self.assertIn("既に削除されています", page.locator(".modal").inner_text())
        page.locator(".modal button").last.click()
        self.wait_text(lambda t: "山田太郎" not in t, "描き直されない")
        self.b_sync_quiet()
        self.assertEqual(len(self.source_rows(config.TABLE_DEL_HISTORY)), before,
                         "削除履歴を二重に残さない")
        self.assertEqual(self.errors, [])

    def b_sync_quiet(self) -> None:
        deadline = time.monotonic() + 30
        while self.b.status()["pending"] and time.monotonic() < deadline:
            time.sleep(0.25)
