"""共有フォルダを使って**現場2台 + 倉庫1台**で使う一連の流れ(端末ごとに別のプロセスを立てる)

【何を確かめるか】
現場では、ラインごとの端末(現場モード)と倉庫の端末(倉庫モード)が、同じ共有フォルダの
看板マスタ(``看板_<ライン>``・``Form状態管理``)を見る。各端末は手元の写しへ取り込み
(周期・共有DBが変わったのを見て・「↻ 更新」)、押した操作は送信待ちに積んで共有DBへ書き戻す。
1台の中の画面操作はほかの試験(``test_web_app.py``・``*_browser.py``)が見ている。
ここで見るのは **台をまたいだときだけ起きること**:

1. 提出 → 倉庫に届く → 発送 → 現場に届く → 届いた確認 → 倉庫に届く(盤・未反映・開いている端末)
2. 同じ看板を2台がほぼ同時に操作する(現場2台の提出/取り消し・倉庫の発送と現場の取り消し・
   発送と届いた確認)。最後に共有DBと各端末の盤がそろい、黙って誰かの操作が消えない
3. 共有フォルダが見えないあいだの操作は送信待ちに残り、戻ったら送られる(帯の知らせ)
4. マスタ管理で看板を足す・直す・消す(別の端末で)→ ほかの端末の盤に届く。消されたあとの操作
5. Access の最新で入れ替えたとき、ほかの端末と送信待ちが壊れない
6. 開いたままの古い画面から操作しても(版が進んでいる)、黙って間違ったことをしない
7. 倉庫モード ⇄ 現場モードの切り替え・担当ラインの切り替えの直後に届く操作
8. 管理番号の使い回し・書き方の揺れで、別の看板に操作が当たらない

【どう立てるか】
端末1台 = **別々のプロセス**(本物の waitress。起動は ``start_app._prepare`` と ``start_app._serve``
をそのまま通す = 取り込み・書き戻し・心拍・送り直しの見張りも本物)。手元の領域
(``KANBAN_LOCAL_DIR``・設定ファイル)は台ごとに別、共有フォルダだけ同じ所を指す。

**共有フォルダへは台ごとの「つなぎ口」を通す**(台ごとのフォルダに置いたシンボリックリンク。
Windows はジャンクション)。つなぎ口をよけると、**その台だけ**共有フォルダが見えなくなる
(ネットワークが切れた端末)。これで「2台がお互いの操作を取り込む前に押した」行き違いを、
時間に頼らずに作れる(Windows の CI は遅いので、時間で行き違いを作ると揺れる)。

待つときは時間ではなく**状態**を待つ(未反映が 0 になった・盤が変わった)。子プロセスは
``tests`` パッケージを読まない。本物の利用者のフォルダは使わない(全部この試験の一時フォルダ)。
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

from kanban import access_control, config, keys
from kanban.db.store import Store

ROOT = Path(__file__).resolve().parents[1]

TOKEN = "flow-token"
ADMIN_PASSWORD = "flow-admin"
ON = config.MARK_ON

#: 子プロセスが立ち上がるのを待つ上限(秒)。Windows の CI は遅い
BOOT_SEC = 90.0
#: 状態を待つ上限(秒)。送り直しの見張りは 5 秒ごと。Windows の CI では60秒で間に合わない
#: ことがあった(状態を待つので、そろえばすぐ進む)
WAIT_SEC = 120.0

L1, HVC = "L1", "HVC"
TABLE = {L1: config.table_name_for(L1), HVC: config.table_name_for(HVC)}

#: 看板。管理番号, 資材, サイズ, 常設品(本番の形: 型の無い表に数で入っている)
KANBAN = {
    L1: [(1, "中性紙", "1600 × 150M:4本", ON), (2, "中性紙", "1600 × 150M:2本", ON),
         (3, "HB", "11000 × 150M:4本", "×"), (4, "アングル", "2510:3束", ON),
         (5, "アングル", "2010:3束", ON)],
    HVC: [(1, "外装紙", "2200 × 50M:3本", ON), (2, "外装紙", "1900 × 50M:5本", ON),
          (3, "ハードボード", "660 × 1050:100枚", ON)],
}
COLUMNS = [config.COL_KEY, config.COL_MATERIAL, config.COL_SIZE, config.COL_WANT, config.COL_UNWANT,
           config.COL_ORDERED_AT, config.COL_SHIPPED, config.COL_CONFIRMED_AT, config.COL_PERMANENT,
           config.COL_HOLD, config.COL_HOLD_AT]

#: 子プロセスの中身。**tests パッケージを読まない**(説明は冒頭)。
#: 起動は ``start_app.main`` と同じ順(``_prepare`` → 待ち受け → ``_serve``)。ポートだけ 0(空いている所)
_CHILD = r"""
import os, socket, sys, threading, time
sys.path.insert(0, os.environ["FLOW_ROOT"])
_pc = os.environ["FLOW_PC"]
socket.gethostname = lambda: _pc            # 台ごとの PC 名(心拍・更新者の名前)
import start_app, server
from kanban import app_config

args = start_app.build_parser().parse_args(["--no-browser", "--config", os.environ["FLOW_CONFIG"]])
root = app_config.ensure_local_dirs()
cfg, store, mode, line, grant, notice = start_app._prepare(args)
srv = server.AppServer(mode, 0, token=os.environ["FLOW_TOKEN"])
thread = threading.Thread(target=srv.serve_forever, name="flow-serve", daemon=True)
thread.start()
while srv._server is None and thread.is_alive():
    time.sleep(0.05)
print("PORT=%s" % srv._server.effective_port, flush=True)
sys.exit(start_app._serve(args, cfg, store, mode, line, root, srv, thread,
                          grant=grant, access_notice=notice, watch_idle=False))
"""


# ---------------------------------------------------------------------------
# 共有フォルダ
# ---------------------------------------------------------------------------
def make_shared(folder: Path) -> Path:
    """共有フォルダを1つ作る。看板マスタ(本番の形)と梱包資材マスタ(アクセス権限)。"""
    folder.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(folder / config.TARGET_SHARED_DB_NAME))
    try:
        for line, rows in KANBAN.items():
            # Access を変換した表と同じく**型を書かない**(管理番号は数で入る)
            conn.execute(f"CREATE TABLE [{TABLE[line]}] ({', '.join(f'[{c}]' for c in COLUMNS)})")
            conn.executemany(
                f"INSERT INTO [{TABLE[line]}] VALUES ({', '.join('?' for _ in COLUMNS)})",
                [(no, material, size, None, ON, None, None, None, permanent, None, None)
                 for no, material, size, permanent in rows])
        conn.execute(f"CREATE TABLE [{config.TABLE_STATE}] ([管理番号], [ライン名], [状態], [更新日時], [ホスト名])")
        conn.executemany(f"INSERT INTO [{config.TABLE_STATE}] VALUES (?, ?, '閉', NULL, NULL)",
                         [(1, L1), (2, HVC), (3, config.WAREHOUSE_LINE_NAME)])
        conn.commit()
    finally:
        conn.close()
    conn = sqlite3.connect(str(folder / config.ACCESS_DB_NAME))
    try:
        conn.execute(access_control.DDL)
        # 倉庫の人(userw)は倉庫モードと現場モードの両方。ほかの端末は行が無い = 現場モードだけ
        conn.executemany(
            f'INSERT INTO "{access_control.TABLE}" (ログインID, PC名, 権限, 有効, 備考) VALUES (?, ?, ?, 1, ?)',
            [("userw", "", access_control.PERM_MATERIAL, "倉庫"), ("userw", "", access_control.PERM_FIELD, "倉庫")])
        conn.commit()
    finally:
        conn.close()
    return folder


def link_dir(target: Path, link: Path) -> None:
    """``link`` から ``target`` へのつなぎ口(シンボリックリンク。作れなければジャンクション)。"""
    try:
        os.symlink(str(target), str(link), target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        if sys.platform != "win32":
            raise
    import _winapi  # Windows: 管理者でなくても作れるジャンクション

    _winapi.CreateJunction(str(target), str(link))


def unlink_dir(link: Path) -> None:
    """つなぎ口だけを取り除く(先のフォルダの中身は消さない)。"""
    if os.path.islink(str(link)):
        os.unlink(str(link))
    else:
        os.rmdir(str(link))          # ジャンクションは rmdir で口だけ消える


def _again(action: Callable[[], None], tries: int = 40) -> None:
    """つなぎ口の付け外し。Windows ではほかのプロセスが一瞬掴んでいて断られることがあるので、やり直す。"""
    for attempt in range(tries):
        try:
            action()
            return
        except OSError:
            if attempt == tries - 1:
                raise
            time.sleep(0.25)


def _remove_tree(path: Path) -> None:
    """一時フォルダを消す。**つなぎ口の先(共有フォルダ)をたどらない。**"""
    for _ in range(20):
        try:
            for link in path.glob("*/share*"):
                try:
                    unlink_dir(link)
                except OSError:
                    pass
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.5)           # Windows: 止めたばかりのプロセスがまだ掴んでいる
    shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------------------
# 端末
# ---------------------------------------------------------------------------
class Terminal:
    """端末1台。別プロセスで立てて、HTTP で操作する。"""

    def __init__(self, case: "_Flow", name: str, *, mode: str, line: str = "",
                 login: str = "", import_sec: int = 30, export_sec: int = 60) -> None:
        self.case = case
        self.name = name
        self.pc = f"PC-{name}"
        self.login = login or f"user{name.lower()}"
        self.dir = case.base / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.local = self.dir / "local"
        self.link = self.dir / "share"
        link_dir(case.shared, self.link)
        self.config_path = self.dir / "config.json"
        self.config_path.write_text(json.dumps({
            config.LOCAL_FORMAT_KEY: config.LOCAL_FORMAT,
            "shared_db_path": str(self.link / config.TARGET_SHARED_DB_NAME),
            "sqlite_path": str(self.local / "data" / "kanban.sqlite3"),
            "line": line,
            "import_interval_sec": import_sec,
            "export_interval_sec": export_sec,
            "admin_password_hash": case.admin_hash,
        }, ensure_ascii=False), encoding="utf-8")
        # この端末のモード(手元の SQLite に覚える。設定 →「この端末」と同じ置き場所)
        (self.local / "data").mkdir(parents=True, exist_ok=True)
        store = Store(str(self.local / "data" / "kanban.sqlite3"), host_name=self.pc)
        try:
            store.ensure_schema()
            store.set_device_mode(mode)
        finally:
            store.close()
        self.proc: Optional[subprocess.Popen] = None
        self.port = 0
        self.mode = ""
        self.start()
        case.addCleanup(self.stop)

    # -- 立てる・止める ----------------------------------------------------
    def start(self) -> None:
        env = dict(os.environ)
        env.update({
            "FLOW_ROOT": str(ROOT),
            "FLOW_PC": self.pc,
            "FLOW_CONFIG": str(self.config_path),
            "FLOW_TOKEN": TOKEN,
            "KANBAN_CONFIG": str(self.config_path),
            "KANBAN_LOCAL_DIR": str(self.local),
            "KANBAN_DISTRIBUTION_DIR": str(self.dir / "配布設定"),
            "KANBAN_SETTINGS_DIR": str(self.dir / "旧設定"),
            "KANBAN_LOGIN_ID": self.login,
            "KANBAN_PC_NAME": self.pc,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        env.pop("ALLTOOLS_PORTAL", None)
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
        # 準備(起動時の取り込み)が済むまで待つ
        health = self.wait_for(lambda: self._health() if self._health().get("ready") else None,
                               what="準備が済む", timeout=BOOT_SEC)
        self.case.assertEqual(health.get("startup_error", ""), "", self.stderr())
        self.mode = health["mode"]

    def _health(self) -> dict:
        status, body = self.call("GET", "/api/health")
        return body if status == 200 and isinstance(body, dict) else {}

    def stop(self) -> None:
        """閉じる(画面の「終了」と同じ。最後の書き戻しをしてから終わる)。"""
        if self.proc is None:
            return
        proc, self.proc = self.proc, None
        if proc.poll() is None:
            try:
                self.call("POST", "/api/shutdown", {"force": True}, timeout=10)
            except OSError:
                pass
            try:
                proc.wait(timeout=40)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=15)
        if proc.stdout is not None:
            proc.stdout.close()
        self._stderr.close()

    def restart(self) -> None:
        """閉じて開き直す(手元の領域はそのまま。モードは手元の SQLite に覚えたもの)。"""
        self.stop()
        self.start()

    def stderr(self) -> str:
        try:
            return (self.dir / "stderr.txt").read_text("utf-8", "replace")[-4000:]
        except OSError:
            return ""

    # -- 共有フォルダのつなぎ口 --------------------------------------------
    def go_offline(self) -> None:
        """この台だけ共有フォルダが見えなくなる(ネットワークが切れた)。"""
        _again(lambda: unlink_dir(self.link))

    def go_online(self) -> None:
        _again(lambda: link_dir(self.case.shared, self.link))

    def set_meta(self, key: str, value: str) -> None:
        """この端末の手元の SQLite の覚え書きを直す(時間が経ったことにする、など)。"""
        conn = sqlite3.connect(str(self.local / "data" / "kanban.sqlite3"), timeout=30)
        try:
            conn.execute("UPDATE meta SET value = ? WHERE key = ?", (value, key))
            conn.commit()
        finally:
            conn.close()

    # -- HTTP --------------------------------------------------------------
    def call(self, method: str, path: str, body: Any = None, *,
             timeout: float = 90) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"X-Tool-Token": TOKEN, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
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
        return self.call("POST", path, body or {})

    def wait_for(self, check: Callable[[], Any], *, what: str, timeout: float = WAIT_SEC) -> Any:
        """``check()`` が真を返すまで待つ(**時間ではなく状態を待つ**)。"""
        deadline = time.monotonic() + timeout
        while True:
            try:
                got = check()
            except (OSError, ValueError):
                got = None
            if got:
                return got
            if self.proc is not None and self.proc.poll() is not None:
                raise AssertionError(f"{self.name} が止まりました({what}):\n{self.stderr()}")
            if time.monotonic() > deadline:
                raise AssertionError(f"{self.name}: {what} まで待ちましたが、そうなりません:\n{self.stderr()}")
            time.sleep(0.2)

    # -- 盤 ----------------------------------------------------------------
    def board(self, line: str) -> dict:
        return self.get(f"/api/board?line={line}")

    def rows(self, line: str) -> dict[str, dict]:
        """``{管理番号: 行}``(盤に出ている看板)。"""
        board = self.board(line)["board"]
        return {r["mgmt_no"]: dict(r, material=g.get("material", ""))
                for g in board["groups"] for r in g["rows"]}

    def row(self, line: str, no: str) -> dict:
        rows = self.rows(line)
        self.case.assertIn(no, rows, f"{self.name} の盤に {line} {no} 番がありません: {sorted(rows)}")
        return rows[no]

    def marks(self, line: str, no: str) -> tuple[str, str, str]:
        """盤に出ている印 ``(赤, 緑, 黄)``。点いていれば 〇。"""
        r = self.row(line, no)
        return (ON if r["size"]["state"] == "ordered" else "",
                ON if r["ship"]["state"] == "shipped" else "",
                ON if r["hold"]["state"] == "held" else "")

    def status(self) -> dict:
        return self.get("/api/status")

    def refresh(self, line: str = "") -> dict:
        """「↻ 更新」(いますぐ取り込む)。"""
        status, body = self.post(f"/api/refresh?line={line}")
        self.case.assertEqual(status, 200, f"{self.name} 更新: {body}")
        return body

    def wait_sent(self) -> None:
        """送信待ちが全部届くまで待つ(押した直後の書き戻し・送り直しの見張り)。"""
        self.wait_for(lambda: self.status()["pending"] == 0 and self.status()["undelivered"] == 0,
                      what="送信待ちが 0 になる")

    # -- 操作 --------------------------------------------------------------
    def press(self, kind: str, line: str, no: str, *, rev: Optional[int] = None, **extra) -> tuple[int, Any]:
        """盤のボタン(``order`` / ``ship`` / ``hold`` / ``acknowledge``)。``rev`` は画面が見ていた版。"""
        if rev is None:
            rev = self.row(line, no)["rev"]
        return self.post(f"/api/board/{kind}", {"line": line, "mgmt_no": no, "rev": rev, **extra})

    def ok(self, kind: str, line: str, no: str, **extra) -> dict:
        status, body = self.press(kind, line, no, **extra)
        self.case.assertEqual(status, 200, f"{self.name} {kind} {line} {no}: {body}")
        return body

    # -- マスタ管理 ----------------------------------------------------------
    def master(self, table: str) -> dict:
        return self.get(f"/api/master/table?table={urllib.request.quote(table)}")

    def master_row(self, table: str, no: Any) -> dict:
        view = self.master(table)
        found = [r for r in view["rows"] if keys.same(r.get(config.COL_KEY), no)]
        self.case.assertEqual(len(found), 1, f"{table} {no}: {view['rows']}")
        return found[0]

    def master_insert(self, table: str, values: dict) -> tuple[int, Any]:
        return self.post("/api/master/insert", {"table": table, "values": values, "password": ADMIN_PASSWORD})

    def master_update(self, table: str, no: Any, column: str, value: Any) -> tuple[int, Any]:
        row = self.master_row(table, no)
        return self.post("/api/master/update", {
            "table": table, "row_key": row["__行"], "key": row[config.COL_KEY], "column": column,
            "value": value, "before": row.get(column), "password": ADMIN_PASSWORD})

    def master_delete(self, table: str, no: Any) -> tuple[int, Any]:
        row = self.master_row(table, no)
        return self.post("/api/master/delete", {
            "table": table, "row_key": row["__行"], "key": row[config.COL_KEY], "password": ADMIN_PASSWORD})


# ---------------------------------------------------------------------------
# 試験の土台
# ---------------------------------------------------------------------------
class _Flow(unittest.TestCase):
    """共有フォルダ1つと、そこを見る端末たち。"""

    admin_hash = ""

    @classmethod
    def setUpClass(cls) -> None:
        cls.admin_hash = config.hash_password(ADMIN_PASSWORD)

    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="kanban_flow_"))
        self.addCleanup(_remove_tree, self.base)
        self.shared = make_shared(self.base / "共有")
        self.shared_db = self.shared / config.TARGET_SHARED_DB_NAME

    def terminal(self, name: str, **kwargs) -> Terminal:
        return Terminal(self, name, **kwargs)

    def site(self, name: str, line: str = L1, **kwargs) -> Terminal:
        return self.terminal(name, mode=config.MODE_SITE, line=line, **kwargs)

    def warehouse(self, name: str = "W", **kwargs) -> Terminal:
        return self.terminal(name, mode=config.MODE_WAREHOUSE, line=L1, login="userw", **kwargs)

    # -- 共有DB --------------------------------------------------------------
    def shared_rows(self, line: str) -> list[dict]:
        conn = sqlite3.connect(f"file:{self.shared_db.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(f"SELECT rowid AS _rowid, * FROM [{TABLE[line]}] ORDER BY rowid")]
        finally:
            conn.close()

    def shared_row(self, line: str, no: Any) -> Optional[dict]:
        found = [r for r in self.shared_rows(line) if keys.same(r[config.COL_KEY], no)]
        self.assertLessEqual(len(found), 1, f"共有DBで {line} {no} 番が重なっています: {found}")
        return found[0] if found else None

    def shared_marks(self, line: str, no: Any) -> Optional[tuple[str, str, str]]:
        row = self.shared_row(line, no)
        if row is None:
            return None
        norm = lambda v: "" if v is None else str(v).strip()      # noqa: E731
        return norm(row[config.COL_WANT]), norm(row[config.COL_SHIPPED]), norm(row[config.COL_HOLD])

    def state_rows(self) -> list[dict]:
        conn = sqlite3.connect(f"file:{self.shared_db.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(f"SELECT * FROM [{config.TABLE_STATE}]")]
        finally:
            conn.close()

    def write_shared(self, sql: str, params: tuple = ()) -> None:
        """Access 側など、アプリの外から共有DBを直す。"""
        conn = sqlite3.connect(str(self.shared_db), timeout=30)
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    # -- そろえる ------------------------------------------------------------
    def settle(self, *terminals: Terminal, line: str = L1) -> None:
        """全員が送り終えてから、全員が取り込む(アプリの周期 1 回ぶん。順番に左右されない)。"""
        for t in terminals:
            t.wait_sent()
        for t in terminals:
            t.refresh(line)

    def assert_everywhere(self, line: str, no: str, want: tuple[str, str, str], *terminals: Terminal) -> None:
        """共有DBと、各端末の盤の印がそろっている。"""
        got = {"共有DB": self.shared_marks(line, no)}
        for t in terminals:
            got[t.name] = t.marks(line, no)
        self.assertTrue(all(v == want for v in got.values()), f"{line} {no} 番: {got}(期待 {want})")


# ---------------------------------------------------------------------------
# 1. 提出 → 倉庫 → 発送 → 現場 → 届いた確認
# ---------------------------------------------------------------------------
class DeliveryFlowTest(_Flow):
    """現場A(L-1)・現場B(HVC)・倉庫W。自動の取り込み・書き戻しだけで回る(「↻ 更新」を押さない)。"""

    def setUp(self) -> None:
        super().setUp()
        # 取り込みの周期は短く(共有DBが変わったのを見て取り込む見張りは本物のまま 2 秒)
        self.a = self.site("A", L1, import_sec=5)
        self.b = self.site("B", HVC, import_sec=5)
        self.w = self.warehouse(import_sec=5)

    def wait_marks(self, t: Terminal, line: str, no: str, want: tuple) -> None:
        t.wait_for(lambda: t.marks(line, no) == want, what=f"{line} {no} 番が {want} になる")

    def test_提出から届いた確認まで_押さなくても全員に届く(self) -> None:
        self.a.ok("order", L1, "1")
        self.b.ok("order", HVC, "2")
        # 押した端末の盤にはすぐ出る
        self.assertEqual(self.a.marks(L1, "1"), (ON, "", ""))
        self.a.wait_sent()
        self.b.wait_sent()
        self.assertEqual(self.shared_marks(L1, "1"), (ON, "", ""))
        self.assertEqual(self.shared_marks(HVC, "2"), (ON, "", ""))
        # 倉庫に届く(両ラインのタブ)
        self.wait_marks(self.w, L1, "1", (ON, "", ""))
        self.wait_marks(self.w, HVC, "2", (ON, "", ""))
        # 倉庫が発送・注文中
        self.w.ok("ship", L1, "1")
        self.w.ok("hold", HVC, "2")
        self.w.wait_sent()
        self.wait_marks(self.a, L1, "1", (ON, ON, ""))
        self.wait_marks(self.b, HVC, "2", (ON, "", ON))
        # 注文中のあいだ、現場は押せない(押しても何も変わらない)
        locked = self.b.row(HVC, "2")
        self.assertTrue(locked["locked"], locked)
        # 現場Aが届いた確認 → 倉庫に届く
        self.a.ok("order", L1, "1", delivered=True)
        self.a.wait_sent()
        self.wait_marks(self.w, L1, "1", ("", "", ""))
        self.assertEqual(self.shared_marks(L1, "1"), ("", "", ""))
        # 倉庫が発送すると注文中も外れる → 現場Bへ
        self.w.ok("ship", HVC, "2")
        self.w.wait_sent()
        self.wait_marks(self.b, HVC, "2", (ON, ON, ""))
        # ほかのラインの盤は見えない(現場は担当ラインだけ)
        status, body = self.a.call("GET", f"/api/board?line={HVC}")
        self.assertEqual(status, 404, body)
        for t in (self.a, self.b, self.w):
            s = t.status()
            self.assertEqual((s["pending"], s["failures"], s["undelivered"]), (0, 0, 0), f"{t.name}: {s}")
            self.assertFalse(s["import_stale"], f"{t.name}: {s}")

    def test_開いている端末が見える(self) -> None:
        """心拍(Form状態管理)。倉庫からは L-1・HVC が、現場からは倉庫が開いて見える。"""
        def live(t: Terminal) -> set[str]:
            t.refresh(L1)
            return {p["line"] for p in t.status()["presence"] if p.get("live")}

        self.w.wait_for(lambda: {L1, HVC} <= live(self.w), what="倉庫に L-1・HVC が開いて見える")
        self.a.wait_for(lambda: config.WAREHOUSE_LINE_NAME in live(self.a), what="現場Aに倉庫が開いて見える")
        self.assertNotIn(L1, live(self.a), "自分のラインを他人として出している")
        # 閉じると「閉」になって消える
        self.b.stop()
        self.w.wait_for(lambda: HVC not in live(self.w), what="閉じた HVC が消える")

    def test_同じラインの2台目を閉じても_開いている1台目は消えない(self) -> None:
        """``Form状態管理`` はライン 1 行。2 台目(A2)のあとに 1 台目(A)が名乗り直したあと、A2 を閉じる。

        前は閉じた端末が誰の印かを見ずに「閉」を書いたので、開いている A まで倉庫から消えた
        (A の次の心拍 = 最大 2 分まで)。
        """
        a2 = self.site("A2", L1, import_sec=5)

        def l1_row() -> tuple[str, str]:
            row = next(r for r in self.state_rows() if r["ライン名"] == L1)
            return str(row["状態"] or ""), str(row["ホスト名"] or "")

        self.a.wait_for(lambda: l1_row() == ("開", a2.pc), what="A2 が L-1 を名乗る")
        self.a.restart()                                  # A が名乗り直す(心拍 1 回ぶん)
        self.a.wait_for(lambda: l1_row() == ("開", self.a.pc), what="A が L-1 を名乗る")
        a2.stop()
        self.assertEqual(l1_row(), ("開", self.a.pc), "閉じた A2 が、開いている A の印まで「閉」にした")
        self.w.refresh(L1)
        self.assertIn(L1, {p["line"] for p in self.w.status()["presence"] if p.get("live")})
        # 印を持っている端末(A)が閉じれば「閉」になる
        self.a.stop()
        self.assertEqual(l1_row()[0], "閉")

    def test_倉庫参照は見るだけ(self) -> None:
        v = self.terminal("V", mode=config.MODE_WAREHOUSE_VIEW, import_sec=5)
        self.a.ok("order", L1, "3")
        self.a.wait_sent()
        v.wait_for(lambda: v.marks(L1, "3") == (ON, "", ""), what="倉庫参照に提出が届く")
        for kind in ("order", "ship", "hold"):
            status, body = v.press(kind, L1, "3")
            self.assertIn(status, (403, 404), f"倉庫参照で {kind} が通った: {body}")
        self.assertEqual(v.status()["pending"], 0)
        self.assertEqual(self.shared_marks(L1, "3"), (ON, "", ""))


# ---------------------------------------------------------------------------
# 2. 同じ看板を 2 台がほぼ同時に操作する(行き違い)
# ---------------------------------------------------------------------------
class SameKanbanTest(_Flow):
    """現場A・現場A2(同じ L-1 の 2 台目)・倉庫W。

    行き違いは**つなぎ口をよけて**作る(押した台は、ほかの台の操作を取り込めない。押したものは
    送信待ちに残る)。戻すのは 1 台ずつ、送り終えてから次 ── 送る順で結果が決まるので、順を決めておく。
    """

    def setUp(self) -> None:
        super().setUp()
        self.a = self.site("A", L1)
        self.a2 = self.site("A2", L1)
        self.w = self.warehouse()
        self.all = (self.a, self.a2, self.w)

    def back(self, *terminals: Terminal) -> None:
        """つなぎ口を戻して、送り終えるまで待つ(1 台ずつ)。"""
        for t in terminals:
            t.go_online()
            t.wait_sent()

    def no_failures(self) -> None:
        for t in self.all:
            s = t.status()
            self.assertEqual((s["pending"], s["failures"], s["dropped"]), (0, 0, []), f"{t.name}: {s}")

    def test_現場2台が同じ看板をほぼ同時に提出_取り消し(self) -> None:
        self.a.go_offline()
        self.a2.go_offline()
        self.a.ok("order", L1, "2")
        self.a2.ok("order", L1, "2")
        self.back(self.a, self.a2)
        self.settle(*self.all)
        self.assert_everywhere(L1, "2", (ON, "", ""), *self.all)
        # 2 台とも取り消す
        self.a.go_offline()
        self.a2.go_offline()
        self.a.ok("order", L1, "2")
        self.a2.ok("order", L1, "2")
        self.back(self.a2, self.a)
        self.settle(*self.all)
        self.assert_everywhere(L1, "2", ("", "", ""), *self.all)
        self.no_failures()

    def test_片方が取り消し_もう片方が古い盤のまま押す(self) -> None:
        """A が出した看板を A2 が取り消す。A は取り消しを取り込む前に押す(A の盤では赤 → 取り消し)。"""
        self.a.ok("order", L1, "1")
        self.settle(*self.all)
        self.a.go_offline()
        self.a2.ok("order", L1, "1")           # 取り消し(届く)
        self.a2.wait_sent()
        self.a.ok("order", L1, "1")            # A の盤ではまだ赤 → 取り消しのつもり
        self.back(self.a)
        self.settle(*self.all)
        self.assert_everywhere(L1, "1", ("", "", ""), *self.all)
        self.no_failures()

    def test_倉庫の発送と現場の取り消しが行き違う(self) -> None:
        self.a.ok("order", L1, "1")
        self.settle(*self.all)
        self.a.go_offline()
        self.w.ok("ship", L1, "1")
        self.w.wait_sent()
        self.a.ok("order", L1, "1")            # 発送を取り込む前に取り消す
        self.back(self.a)
        self.settle(*self.all)
        # 赤なし・緑あり(発送処理中に注文が取り消された)。どちらの操作も消えていない
        self.assert_everywhere(L1, "1", ("", ON, ""), *self.all)
        for t in (self.a, self.a2):
            self.assertIn("取り消され", t.row(L1, "1")["alert"], t.name)
        # 確かめずに出し直そうとしても断る(発送の行方が分からなくなる)
        status, body = self.a2.press("order", L1, "1")
        self.assertEqual((status, body["error"]["code"]), (409, "cancelled_while_shipping"), body)
        # 現場が確かめる → 全員そろう
        self.a2.ok("acknowledge", L1, "1")
        self.settle(*self.all)
        self.assert_everywhere(L1, "1", ("", "", ""), *self.all)
        self.no_failures()

    def test_発送と届いた確認が行き違う(self) -> None:
        self.a.ok("order", L1, "4")
        self.settle(*self.all)
        self.w.ok("ship", L1, "4")
        self.settle(*self.all)
        self.w.go_offline()
        self.a.ok("order", L1, "4", delivered=True)    # 届いた確認(赤と緑を消す)
        self.a.wait_sent()
        self.w.ok("ship", L1, "4")                      # 倉庫は古い盤のまま発送を取り消す
        self.back(self.w)
        self.settle(*self.all)
        self.assert_everywhere(L1, "4", ("", "", ""), *self.all)
        self.no_failures()

    def test_一括発送と現場の取り消しが行き違う(self) -> None:
        self.a.ok("order", L1, "1")
        self.a.ok("order", L1, "2")
        self.settle(*self.all)
        self.w.go_offline()
        items = [{"mgmt_no": no, "rev": r["rev"]} for no, r in self.w.rows(L1).items() if r["batch_ship"]]
        status, body = self.w.post("/api/board/batch-ship", {"line": L1, "items": items})
        self.assertEqual((status, body.get("done")), (200, 2), body)
        self.a.ok("order", L1, "2")                     # 倉庫が送る前に 2 番を取り消す
        self.a.wait_sent()
        self.back(self.w)
        self.settle(*self.all)
        self.assert_everywhere(L1, "1", (ON, ON, ""), *self.all)
        self.assert_everywhere(L1, "2", ("", ON, ""), *self.all)   # 取り消された発送(現場が確かめる)
        self.no_failures()

    def test_注文中と現場の取り消しが行き違う(self) -> None:
        self.a.ok("order", L1, "5")
        self.settle(*self.all)
        self.w.go_offline()
        self.w.ok("hold", L1, "5")
        self.a.ok("order", L1, "5")                     # 取り消す(倉庫の注文中はまだ見えない)
        self.a.wait_sent()
        self.back(self.w)
        self.settle(*self.all)
        # 現場の取り消しも倉庫の注文中も残る(黄だけ点いた看板。倉庫が外せる)
        self.assert_everywhere(L1, "5", ("", "", ON), *self.all)
        self.w.ok("hold", L1, "5")
        self.settle(*self.all)
        self.assert_everywhere(L1, "5", ("", "", ""), *self.all)
        self.no_failures()


    def conflict_notice(self, t: Terminal) -> list[tuple[str, str, str]]:
        notes = t.wait_for(lambda: t.status()["dropped"], what="帯に「ほかの端末が先に操作していた」が出る")
        return [(n["mgmt_no"], n["what"], n["reason"]) for n in notes]

    def test_古い盤の2台目の提出が_倉庫の発送を消さない(self) -> None:
        """A が提出 → W が発送 → A2(どちらもまだ取り込んでいない)が同じ看板を押す。

        A2 の盤では未発注なので「提出」になる。前は列ごとの後勝ちで発送の列を空にして送り、
        **倉庫の発送が黙って消えていた**(倉庫は二重に出しかねない)。押したときに見えていた値
        (発送なし)と共有DBのいま(発送あり)が違うので、送らずに下ろして A2 に知らせる。
        """
        self.a2.go_offline()                      # A2 はこのあいだの操作を取り込めない
        self.a.ok("order", L1, "3")
        self.a.wait_sent()
        self.w.refresh(L1)
        self.w.ok("ship", L1, "3")
        self.w.wait_sent()
        self.assertEqual(self.shared_marks(L1, "3"), (ON, ON, ""))
        self.assertEqual(self.a2.marks(L1, "3"), ("", "", ""), "試験の前提: A2 の盤は古い")
        self.a2.ok("order", L1, "3")               # 古い盤のまま「提出」
        self.a2.go_online()
        self.assertEqual(self.conflict_notice(self.a2), [("3", "発注", "conflict")])
        self.a2.wait_sent()
        self.settle(*self.all)
        self.assert_everywhere(L1, "3", (ON, ON, ""), *self.all)
        for t in (self.a, self.w):
            self.assertEqual(t.status()["dropped"], [], f"{t.name} に知らせが出た")
        # 知らせを閉じたら、あとはふつうに使える(届いた確認 → 倉庫に届く)
        self.assertEqual(self.a2.post("/api/dropped/clear")[0], 200)
        self.a2.ok("order", L1, "3", delivered=True)
        self.settle(*self.all)
        self.assert_everywhere(L1, "3", ("", "", ""), *self.all)
        self.no_failures()

    def test_古い盤の2台目の届いた確認が_出し直した発注を消さない(self) -> None:
        """発送済みの看板を A が届いた確認して出し直す → A2(古い盤では 赤+緑)が「届いた」を押す。

        前は A2 の届いた確認が後勝ちで、A の新しい発注を黙って消していた。
        """
        self.a.ok("order", L1, "4")
        self.settle(*self.all)
        self.w.ok("ship", L1, "4")
        self.settle(*self.all)
        self.a2.go_offline()
        self.a.ok("order", L1, "4", delivered=True)
        self.a.ok("order", L1, "4")               # 出し直す
        self.a.wait_sent()
        self.assertEqual(self.shared_marks(L1, "4"), (ON, "", ""))
        self.assertEqual(self.a2.marks(L1, "4"), (ON, ON, ""), "試験の前提: A2 の盤は古い")
        self.a2.ok("order", L1, "4", delivered=True)
        self.a2.go_online()
        self.assertEqual(self.conflict_notice(self.a2), [("4", "発注の取り消し・届いた確認", "conflict")])
        self.a2.wait_sent()
        self.settle(*self.all)
        self.assert_everywhere(L1, "4", (ON, "", ""), *self.all)
        self.assertTrue(self.shared_row(L1, "4")[config.COL_ORDERED_AT], "出し直した発注の時刻が消えた")
        self.w.ok("ship", L1, "4")                 # 倉庫は出し直した発注をふつうに発送できる
        self.settle(*self.all)
        self.assert_everywhere(L1, "4", (ON, ON, ""), *self.all)

# ---------------------------------------------------------------------------
# 3. 共有フォルダが見えないあいだ
# ---------------------------------------------------------------------------
class OutageTest(_Flow):
    def setUp(self) -> None:
        super().setUp()
        self.a = self.site("A", L1)
        self.w = self.warehouse()

    def test_見えないあいだの操作は送信待ちに残り_戻ったら送られる(self) -> None:
        self.a.go_offline()
        before = self.a.status()["last_import_at"]
        self.a.ok("order", L1, "3")
        self.assertEqual(self.a.marks(L1, "3"), (ON, "", ""), "押した盤に出ない")
        s = self.a.wait_for(lambda: (lambda s: s if "見えません" in s["undelivered_why"] else None)(self.a.status()),
                            what="帯に「共有DBが見えません」が出る")
        self.assertEqual((s["pending"], s["undelivered"]), (1, 1), s)
        self.assertTrue(s["undelivered_since"], s)
        # 「↻ 更新」は取り込めなかったと言い、「最後に取り込めた時刻」は進まない
        body = self.a.refresh(L1)
        self.assertFalse(body["imported"], body)
        self.assertIn("取り込めない", body["import_message"])
        self.assertEqual(self.a.status()["last_import_at"], before)
        self.assertEqual(self.a.marks(L1, "3"), (ON, "", ""), "取り込めなかった更新で押した印が消えた")
        # しばらく取り込めないまま(最後に取り込めたのを 10 分前にする)→「⚠ 取り込めていません」と理由
        stamp = time.strftime("%Y/%m/%d %H:%M:%S", time.localtime(time.time() - 600))
        self.a.set_meta("last_import_at", stamp)
        s = self.a.status()
        self.assertTrue(s["import_stale"], s)
        self.assertTrue(s["import_problem"], s)
        # そのあいだ倉庫には出ない。倉庫が別の看板を動かしても A には届かない
        self.w.refresh(L1)
        self.assertEqual(self.w.marks(L1, "3"), ("", "", ""))
        self.assertEqual(self.shared_marks(L1, "3"), ("", "", ""))
        # 戻ったら(押し直さなくても)送られる
        self.a.go_online()
        self.a.wait_sent()
        self.assertEqual(self.shared_marks(L1, "3"), (ON, "", ""))
        self.w.wait_for(lambda: self.w.marks(L1, "3") == (ON, "", ""), what="倉庫に届く")
        s = self.a.status()
        self.assertEqual((s["undelivered_since"], s["undelivered_why"]), ("", ""), s)
        self.a.refresh(L1)
        s = self.a.status()
        self.assertNotEqual(s["last_import_at"], stamp, "戻っても取り込めた時刻が進まない")
        self.assertFalse(s["import_stale"], s)

    def test_見えないまま閉じても_次に開いて戻ったら送られる(self) -> None:
        self.a.go_offline()
        self.a.ok("order", L1, "2")
        self.a.restart()                       # 閉じる(最後の書き戻しも届かない)→ 開き直す(取り込めない)
        s = self.a.status()
        self.assertEqual(s["pending"], 1, s)
        self.assertEqual(self.a.marks(L1, "2"), (ON, "", ""), "開き直したら押した印が消えた")
        self.a.go_online()
        self.a.wait_sent()
        self.assertEqual(self.shared_marks(L1, "2"), (ON, "", ""))
        self.w.refresh(L1)
        self.assertEqual(self.w.marks(L1, "2"), (ON, "", ""))


# ---------------------------------------------------------------------------
# 4 / 8. マスタ管理(別の端末で足す・直す・消す)と、管理番号の使い回し
# ---------------------------------------------------------------------------
class MasterFlowTest(_Flow):
    def setUp(self) -> None:
        super().setUp()
        self.a = self.site("A", L1)
        self.a2 = self.site("A2", L1)
        self.w = self.warehouse()
        self.all = (self.a, self.a2, self.w)

    def test_倉庫で足した看板を現場が提出し_直すと届き_片付けたら消せる(self) -> None:
        status, body = self.w.master_insert(TABLE[L1], {
            config.COL_KEY: "6", config.COL_MATERIAL: "中性紙", config.COL_SIZE: "1000 × 100M:4本",
            config.COL_PERMANENT: ON})
        self.assertEqual(status, 200, body)
        self.a.wait_for(lambda: "6" in self.a.rows(L1), what="足した看板が現場の盤に出る")
        self.a.ok("order", L1, "6")
        self.settle(*self.all)
        self.assert_everywhere(L1, "6", (ON, "", ""), *self.all)
        # 発注中は消せない
        status, body = self.w.master_delete(TABLE[L1], "6")
        self.assertEqual((status, body["error"]["code"]), (409, "in_use"), body)
        self.assertIsNotNone(self.shared_row(L1, "6"))
        # サイズを直す → 現場の盤に届く(発注はそのまま)
        status, body = self.w.master_update(TABLE[L1], "6", config.COL_SIZE, "1000 × 100M:2本")
        self.assertEqual(status, 200, body)
        self.a.wait_for(lambda: self.a.row(L1, "6")["size"]["label"] == "1000 × 100M:2本",
                        what="直したサイズが現場の盤に出る")
        self.assertEqual(self.a.marks(L1, "6"), (ON, "", ""))
        # 取り消して片付けたら消せる → 全員の盤から消える
        self.a.ok("order", L1, "6")
        self.settle(*self.all)
        status, body = self.w.master_delete(TABLE[L1], "6")
        self.assertEqual(status, 200, body)
        self.settle(*self.all)
        for t in self.all:
            self.assertNotIn("6", t.rows(L1), t.name)
        # 消されたあと、古い盤のまま押しても書き込まない
        status, body = self.a2.press("order", L1, "6", rev=10**6)
        self.assertEqual(status, 409, body)
        self.assertIsNone(self.shared_row(L1, "6"))

    def test_送る前に消された看板への提出は届かず_帯で知らせる(self) -> None:
        self.a.go_offline()
        self.a.ok("order", L1, "5")
        status, body = self.w.master_delete(TABLE[L1], "5")      # 倉庫では空の看板に見える
        self.assertEqual(status, 200, body)
        self.a.go_online()
        dropped = self.a.wait_for(lambda: self.a.status()["dropped"], what="帯に届かなかった操作が出る")
        self.assertEqual([(d["line"], d["mgmt_no"], d["what"]) for d in dropped], [(L1, "5", "発注")])
        self.assertEqual(self.a.status()["pending"], 0)
        self.assertNotIn("5", self.a.rows(L1))
        self.assertIsNone(self.shared_row(L1, "5"), "消した看板が戻ってきた")

    def test_消して同じ番号で足し直しても_前の看板への提出は新しい看板に当たらない(self) -> None:
        """**管理番号の使い回し。** 送る前に消されて、同じ番号で別の看板が足された。

        マスタ管理は「いちばん大きい番号 + 1」を勧めるので、最後の看板を消して足すと同じ番号になる。
        前の看板(アングル 2010)への提出が、新しい看板(HB)に乗ってはいけない(倉庫が違う資材を出す)。
        """
        self.a.go_offline()
        self.a.ok("order", L1, "5")                              # アングル 2010:3束
        self.assertEqual(self.w.master_delete(TABLE[L1], "5")[0], 200)
        status, body = self.w.master_insert(TABLE[L1], {
            config.COL_KEY: "5", config.COL_MATERIAL: "HB", config.COL_SIZE: "2000 × 50M:1本",
            config.COL_PERMANENT: ON})
        self.assertEqual(status, 200, body)
        self.a.go_online()
        dropped = self.a.wait_for(lambda: self.a.status()["dropped"], what="帯に届かなかった操作が出る")
        self.assertEqual([(d["mgmt_no"], d["material"], d["what"]) for d in dropped], [("5", "アングル", "発注")])
        self.a.wait_sent()
        self.settle(*self.all)
        self.assert_everywhere(L1, "5", ("", "", ""), *self.all)
        self.assertEqual(self.shared_row(L1, "5")[config.COL_MATERIAL], "HB")
        for t in self.all:
            self.assertEqual(t.row(L1, "5")["material"], "HB", t.name)
        # 新しい看板はふつうに出せる
        self.a2.ok("order", L1, "5")
        self.settle(*self.all)
        self.assert_everywhere(L1, "5", (ON, "", ""), *self.all)

    def test_管理番号の書き方が変わっても_同じ看板に当たる(self) -> None:
        """共有DBの番号が ``2`` → ``'２'``(全角)・``'3.0'`` に変わっても、ほかの看板に当たらない。"""
        self.a.go_offline()
        self.a.ok("order", L1, "2")
        self.a.ok("order", L1, "3")
        self.write_shared(f"UPDATE [{TABLE[L1]}] SET [管理番号] = '２' WHERE [管理番号] = 2")
        self.write_shared(f"UPDATE [{TABLE[L1]}] SET [管理番号] = '3.0' WHERE [管理番号] = 3")
        self.a.go_online()
        self.a.wait_sent()
        self.settle(*self.all)
        for no in ("2", "3"):
            self.assert_everywhere(L1, no, (ON, "", ""), *self.all)
        for no in ("1", "4", "5"):
            self.assert_everywhere(L1, no, ("", "", ""), *self.all)
        self.assertEqual([str(r[config.COL_KEY]) for r in self.shared_rows(L1)], ["1", "２", "3.0", "4", "5"],
                         "書き方を変えた・行が増えた")
        self.w.ok("ship", L1, "2")
        self.settle(*self.all)
        self.assert_everywhere(L1, "2", (ON, ON, ""), *self.all)
        for t in self.all:
            self.assertEqual(sorted(t.rows(L1)), ["1", "2", "3", "4", "5"], t.name)
            self.assertEqual(t.status()["failures"], 0, t.name)


# ---------------------------------------------------------------------------
# 5. Access の最新で入れ替え
# ---------------------------------------------------------------------------
class AccessRefreshTest(_Flow):
    def setUp(self) -> None:
        super().setUp()
        self.a = self.site("A", L1)
        self.w = self.warehouse()

    def make_access(self) -> Path:
        """Access を変換した .sqlite3(管理番号は Access の Double = 1.0)。4 番が無く、6 番が増え、1 番の名前が直っている。"""
        path = self.base / "Access" / "看板マスタ_最新.sqlite3"
        path.parent.mkdir()
        conn = sqlite3.connect(str(path))
        try:
            conn.execute(f"CREATE TABLE [{TABLE[L1]}] ({', '.join(f'[{c}]' for c in COLUMNS)})")
            rows = [(1.0, "中性紙(新)", "1600 × 150M:4本"), (2.0, "中性紙", "1600 × 150M:2本"),
                    (3.0, "HB", "11000 × 150M:4本"), (5.0, "アングル", "2010:3束"), (6.0, "PP バンド", "15mm")]
            conn.executemany(
                f"INSERT INTO [{TABLE[L1]}] VALUES ({', '.join('?' for _ in COLUMNS)})",
                [(no, m, s, None, ON, None, None, None, ON, None, None) for no, m, s in rows])
            conn.commit()
        finally:
            conn.close()
        return path

    def test_入れ替えても_ほかの端末の発注と送信待ちは壊れない(self) -> None:
        self.a.ok("order", L1, "2")             # 届いている発注
        self.settle(self.a, self.w)
        self.a.go_offline()
        self.a.ok("order", L1, "3")             # まだ送っていない発注
        source = self.make_access()
        status, body = self.w.post("/api/table-refresh/run", {
            "path": str(source), "tables": [TABLE[L1]], "password": ADMIN_PASSWORD})
        self.assertEqual(status, 200, body)
        self.a.go_online()
        self.a.wait_sent()
        self.settle(self.a, self.w)
        self.assert_everywhere(L1, "2", (ON, "", ""), self.a, self.w)
        self.assert_everywhere(L1, "3", (ON, "", ""), self.a, self.w)
        for t in (self.a, self.w):
            rows = t.rows(L1)
            self.assertEqual(sorted(rows), ["1", "2", "3", "5", "6"], t.name)
            self.assertEqual(rows["1"]["material"], "中性紙(新)", t.name)
            self.assertEqual(t.status()["failures"], 0, t.name)
        # 管理番号は前の書き方(数の 2・3)のまま。行も増えていない
        self.assertEqual([r[config.COL_KEY] for r in self.shared_rows(L1)], [1, 2, 3, 5, 6])


# ---------------------------------------------------------------------------
# 6. 開いたままの古い画面
# ---------------------------------------------------------------------------
class StaleScreenTest(_Flow):
    def setUp(self) -> None:
        super().setUp()
        self.a = self.site("A", L1)
        self.a2 = self.site("A2", L1)
        self.w = self.warehouse()
        self.all = (self.a, self.a2, self.w)

    def test_古い画面から押しても_黙って間違ったことをしない(self) -> None:
        self.a.ok("order", L1, "1")
        self.settle(*self.all)
        old = self.a.row(L1, "1")["rev"]          # A の画面に出ている版
        self.a2.ok("order", L1, "1")              # 別の端末が取り消した
        self.settle(*self.all)
        # A の画面はまだ赤のまま(古い)。押すと、取り消しを取り消すつもりの「提出」にはならず断る
        status, body = self.a.press("order", L1, "1", rev=old)
        self.assertEqual((status, body["error"]["code"]), (409, "conflict"), body)
        self.assertEqual(self.a.marks(L1, "1"), ("", "", ""))
        self.assertEqual(self.a.status()["pending"], 0)
        # 倉庫: L-1 の盤の版で HVC の同じ番号を押しても断る(版は全行通しの番号)
        rev_l1 = self.w.row(L1, "1")["rev"]
        status, body = self.w.press("ship", HVC, "1", rev=rev_l1)
        self.assertEqual(status, 409, body)
        self.assertEqual(self.shared_marks(HVC, "1"), ("", "", ""))
        # 古い盤のまま一括発送: 版の変わった行は飛ばす
        self.a.ok("order", L1, "2")
        self.a.ok("order", L1, "3")
        self.settle(*self.all)
        # 確認で訊いた対象(画面の一括発送の対象 = 発注済み・未発送)
        items = [{"mgmt_no": no, "rev": r["rev"]} for no, r in self.w.rows(L1).items() if r["batch_ship"]]
        self.assertEqual([i["mgmt_no"] for i in items], ["2", "3"])
        self.a.ok("order", L1, "3")               # 3 番を取り消す(倉庫はまだ古い盤)
        self.settle(self.a, self.w)
        status, body = self.w.post("/api/board/batch-ship", {"line": L1, "items": items})
        self.assertEqual(status, 200, body)
        self.assertEqual((body["done"], body["skipped"]), (1, ["3"]), body)
        self.settle(*self.all)
        self.assert_everywhere(L1, "2", (ON, ON, ""), *self.all)
        self.assert_everywhere(L1, "3", ("", "", ""), *self.all)


# ---------------------------------------------------------------------------
# 7. モード・担当ラインの切り替えの直後
# ---------------------------------------------------------------------------
class SwitchTest(_Flow):
    def setUp(self) -> None:
        super().setUp()
        self.a = self.site("A", L1)
        self.w = self.warehouse()

    def test_倉庫から現場へ切り替えた直後_送信待ちは送られ_古い倉庫の画面の操作は断る(self) -> None:
        self.a.ok("order", L1, "1")
        self.settle(self.a, self.w)
        old_rev = self.w.row(L1, "1")["rev"]
        self.w.go_offline()
        self.w.ok("ship", L1, "1")                # 送る前に切り替える
        status, body = self.w.post("/api/mode", {"mode": config.MODE_SITE})
        self.assertEqual(status, 200, body)
        self.w.restart()                           # デスクトップ版はその場で開き直す
        self.assertEqual(self.w.mode, config.MODE_SITE)
        self.assertEqual(self.w.status()["pending"], 1, "倉庫で押した発送を捨てた")
        # 開いたままの倉庫の画面から押しても、現場モードでは発送できない
        status, body = self.w.press("ship", L1, "1", rev=old_rev)
        self.assertEqual(status, 403, body)
        status, body = self.w.press("hold", L1, "1", rev=old_rev)
        self.assertEqual(status, 403, body)
        self.w.go_online()
        self.w.wait_sent()
        self.settle(self.a, self.w)
        self.assert_everywhere(L1, "1", (ON, ON, ""), self.a, self.w)
        # 現場 → 倉庫へ戻す
        self.assertEqual(self.w.post("/api/mode", {"mode": config.MODE_WAREHOUSE})[0], 200)
        self.w.restart()
        self.assertEqual(self.w.mode, config.MODE_WAREHOUSE)
        self.assertEqual(self.w.marks(HVC, "1"), ("", "", ""))

    def test_担当ラインを切り替えた直後_前のラインの送信待ちは送られ_前のラインの画面は断る(self) -> None:
        old_rev = self.a.row(L1, "1")["rev"]
        self.a.go_offline()
        self.a.ok("order", L1, "1")              # 送る前にラインを変える
        status, body = self.a.post("/api/line", {"line": HVC, "password": ADMIN_PASSWORD})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.a.status()["pending"], 1, "ラインを変えたら前のラインの提出を捨てた")
        self.a.go_online()
        self.a.wait_sent()
        self.assertEqual(self.shared_marks(L1, "1"), (ON, "", ""), "前のラインの提出が届かない")
        self.a.refresh(HVC)
        self.assertEqual(sorted(self.a.rows(HVC)), ["1", "2", "3"])
        # 前のライン(L-1)の画面のまま押しても断る
        status, body = self.a.press("order", L1, "2", rev=old_rev)
        self.assertEqual(status, 400, body)
        self.assertEqual(self.shared_marks(L1, "2"), ("", "", ""))
        # HVC の看板はふつうに出せる・倉庫に届く
        self.a.ok("order", HVC, "1")
        self.settle(self.a, self.w, line=HVC)
        self.assert_everywhere(HVC, "1", (ON, "", ""), self.a, self.w)
        self.assertEqual(self.shared_marks(L1, "1"), (ON, "", ""))


# ---------------------------------------------------------------------------
# 本物のブラウザ: 開いている画面に、ほかの端末の操作が押さなくても出る
# ---------------------------------------------------------------------------
class BrowserFlowTest(_Flow):
    """現場A と倉庫W の画面を本物のブラウザで開いたまま、ボタンで回す(画面の自動の読み直しで届くか)。"""

    def setUp(self) -> None:
        super().setUp()
        try:
            from _browser import chromium_or_skip
        except ImportError:                          # python -m unittest tests.test_flow_multi_terminal
            from tests._browser import chromium_or_skip
        self.pw, self.browser = chromium_or_skip()
        self.addCleanup(self.pw.stop)
        self.addCleanup(self.browser.close)
        self.a = self.site("A", L1)
        self.w = self.warehouse()

    def open(self, t: Terminal, line: str = L1):
        try:
            from _dialogs import answer_dialogs, native_guard
        except ImportError:                          # python -m unittest tests.test_flow_multi_terminal
            from tests._dialogs import answer_dialogs, native_guard
        page = self.browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        # 確かめの窓(画面の中の窓)は「はい」で閉じる。ブラウザ標準の窓が出たら誤りにする
        answer_dialogs(page, lambda _text: True)
        native_guard(page, errors)
        page.goto(f"http://127.0.0.1:{t.port}/board?t={TOKEN}&line={line}", wait_until="load")
        page.wait_for_selector('#board .row[data-no="1"]', timeout=30000)
        self.addCleanup(lambda: self.assertEqual(errors, [], f"{t.name} の画面のエラー"))
        return page

    @staticmethod
    def cell(no: str, kind: str, state: str = "") -> str:
        return f'#board .row[data-no="{no}"] .cell[data-kind="{kind}"]' + (f'[data-state="{state}"]' if state else "")

    def test_ほかの端末の操作が開いている画面に届き_落ちているあいだは帯が出る(self) -> None:
        site, ware = self.open(self.a), self.open(self.w)
        site.click(self.cell("1", "size"))
        # 倉庫の画面は押さなくても赤になる(共有DBが変わったのを見て取り込み → 画面の読み直し)。
        # 待ちは 120 秒: 変わったのを見逃しても取り込みの周期(30秒)+画面の読み直し(5秒)で届く作りだが、
        # 重い Windows の CI では 45 秒では足りなかった(試験全体が普段の1.5倍かかった回)
        ware.wait_for_selector(self.cell("1", "size", "ordered"), timeout=120000)
        ware.click(self.cell("1", "ship"))
        site.wait_for_selector(self.cell("1", "ship", "shipped"), timeout=120000)
        self.assertEqual(self.shared_marks(L1, "1"), (ON, ON, ""))
        # 現場Aだけ共有フォルダが見えなくなる → 押したものは帯「届いていません」に出る
        self.a.go_offline()
        site.click(self.cell("2", "size"))
        site.wait_for_selector(self.cell("2", "size", "ordered"), timeout=10000)
        site.wait_for_function(
            "() => { const b = document.getElementById('undelivered');"
            " return b && !b.hidden && b.textContent.includes('見えません'); }", timeout=120000)
        self.assertIn("届いていません", site.text_content("#undelivered"))
        # 戻ったら帯が消え、倉庫に届く
        self.a.go_online()
        site.wait_for_selector("#undelivered", state="hidden", timeout=120000)
        ware.wait_for_selector(self.cell("2", "size", "ordered"), timeout=120000)
        self.assertEqual(self.shared_marks(L1, "2"), (ON, "", ""))


# ---------------------------------------------------------------------------
# 8. 管理番号の使い回し(1 プロセスの中で、書き戻しと取り込みを 1 段ずつ)
# ---------------------------------------------------------------------------
class NumberReuseLayersTest(unittest.TestCase):
    """送る前に看板が消され、同じ番号で別の資材が足された。**書き戻し・取り込みのどちらが先でも**、
    前の看板への操作は新しい看板に乗らない(上の ``MasterFlowTest`` はどちらが先かを選べない)。"""

    def setUp(self) -> None:
        try:
            import test_shared_shapes as shapes
        except ImportError:                          # python -m unittest tests.test_flow_multi_terminal
            from tests import test_shared_shapes as shapes
        from kanban.db import sync

        self.shapes, self.sync = shapes, sync
        self.root = Path(tempfile.mkdtemp(prefix="kanban_reuse_"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.shared = shapes.SharedShapesTest.make_shared(self, self.root, "", lambda n: n, "", None,
                                                          "2026-10-16 08:32:00")
        self.site = shapes.Terminal(self.root, "現場PC", self.shared)
        self.ware = shapes.Terminal(self.root, "倉庫PC", self.shared)
        self.addCleanup(self.site.close)
        self.addCleanup(self.ware.close)
        for t in (self.site, self.ware):
            t.take()
        # 現場が 4 番(中性紙 1300)を押して、まだ送っていない
        self.site.svc.toggle_order(shapes.LINE, "4", expected_rev=self.site.item("4").rev)
        # 倉庫のマスタ管理で 4 番を消し、同じ番号で HB を足す(倉庫からは空の看板に見える)
        from kanban.presenters import master

        gw = self.ware.gateway
        gw.forget_copy()
        view = master.open_table(gw, shapes.TABLE)
        row = next(r for r in view.rows if keys.same(r[config.COL_KEY], 4))
        self.assertTrue(master.delete_row(gw, shapes.TABLE, row[master.ROW_KEY], expect_key=4).ok)
        gw.forget_copy()
        added = master.insert_row(gw, shapes.TABLE, {config.COL_KEY: "4", config.COL_MATERIAL: "HB",
                                                     config.COL_SIZE: "2000 × 50M:1本", config.COL_PERMANENT: ON})
        self.assertTrue(added.ok, added.message)

    def shared_4(self) -> dict:
        conn = sqlite3.connect(str(self.shared))
        conn.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in conn.execute(f"SELECT * FROM [{self.shapes.TABLE}]")
                    if keys.same(r[config.COL_KEY], 4)]
        finally:
            conn.close()
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def assert_not_on_new_kanban(self) -> None:
        row = self.shared_4()
        self.assertEqual(row[config.COL_MATERIAL], "HB")
        self.assertIn(row[config.COL_WANT] or "", ("",), f"前の看板への発注が新しい看板に乗った: {row}")
        item = self.site.item("4")
        self.assertEqual((item.material, item.want), ("HB", ""), "現場の盤で新しい看板が発注中に見える")
        self.assertEqual(self.site.store.pending_count(), 0, "送り先の無い操作を預かり続けている")
        notes = self.site.store.dropped_notices()
        self.assertEqual([(n["mgmt_no"], n["material"], n["what"]) for n in notes], [("4", "中性紙", "発注")])

    def test_書き戻しが先(self) -> None:
        result = self.sync.export_pending(self.site.store, self.site.gateway)
        self.assertEqual((result.succeeded, result.failed), (0, 1), result.errors)
        self.assertIn("入れ替わった", result.errors[0])
        self.assertIn("中性紙", result.errors[0])
        self.assertEqual(self.shared_4()[config.COL_WANT] or "", "", "前の看板への発注が新しい看板に乗った")
        self.site.take()                    # 取り込みで手元の操作を下ろし、帯で知らせる
        self.assert_not_on_new_kanban()

    def test_取り込みが先(self) -> None:
        self.site.take()
        self.site.send()
        self.assert_not_on_new_kanban()
        self.assertEqual(self.site.errors, [])

    def test_資材が同じなら_サイズを直しても発注は残って届く(self) -> None:
        """サイズ(1 回の量)を直すのは同じ看板(``test_master_freshness`` の決まり)。発注は新しいサイズで届く。"""
        from kanban.presenters import master

        self.site.svc.toggle_order(self.shapes.LINE, "2", expected_rev=self.site.item("2").rev)   # まだ送らない
        gw = self.ware.gateway
        gw.forget_copy()
        view = master.open_table(gw, self.shapes.TABLE)
        row = next(r for r in view.rows if keys.same(r[config.COL_KEY], 2))
        self.assertTrue(master.update_cell(gw, self.shapes.TABLE, row[master.ROW_KEY], config.COL_SIZE,
                                           "1600 × 150M:8本", expect_key=2).ok)
        for first in ("send", "take"):
            with self.subTest(first=first):
                getattr(self.site, first)()
        self.site.take()
        conn = sqlite3.connect(str(self.shared))
        try:
            got = {str(k): (w, size) for k, w, size in conn.execute(
                f"SELECT 管理番号, 欲, サイズ FROM [{self.shapes.TABLE}]")}
        finally:
            conn.close()
        self.assertEqual(got["2"], (ON, "1600 × 150M:8本"), "資材の変わらない看板の発注が届かない")
        item = self.site.item("2")
        self.assertEqual((item.want, item.size), (ON, "1600 × 150M:8本"))
        self.assertEqual([n["mgmt_no"] for n in self.site.store.dropped_notices()], ["4"])

    def test_資材が同じなら_サイズを直しても発注は残って届く_取り込みが先(self) -> None:
        from kanban.presenters import master

        self.site.svc.toggle_order(self.shapes.LINE, "2", expected_rev=self.site.item("2").rev)
        gw = self.ware.gateway
        gw.forget_copy()
        view = master.open_table(gw, self.shapes.TABLE)
        row = next(r for r in view.rows if keys.same(r[config.COL_KEY], 2))
        self.assertTrue(master.update_cell(gw, self.shapes.TABLE, row[master.ROW_KEY], config.COL_SIZE,
                                           "1600 × 150M:8本", expect_key=2).ok)
        self.site.take()
        self.assertEqual(self.site.item("2").want, ON, "サイズを直しただけで送っていない発注を下ろした")
        self.site.send()
        conn = sqlite3.connect(str(self.shared))
        try:
            got = {str(k): w for k, w in conn.execute(f"SELECT 管理番号, 欲 FROM [{self.shapes.TABLE}]")}
        finally:
            conn.close()
        self.assertEqual(got["2"], ON)


# ---------------------------------------------------------------------------
# 2. 古い盤からの操作(1 プロセスの中で、書き戻しを 1 段ずつ)
# ---------------------------------------------------------------------------
class StaleBoardWriteBackTest(unittest.TestCase):
    """書き戻しは、押したときに見えていた値の行にだけ書く(コンペア・アンド・スワップ)。

    現場PC1・現場PC2(同じライン)・倉庫PC。共有DBは本番の形(型なし・管理番号は数)。
    """

    def setUp(self) -> None:
        try:
            import test_shared_shapes as shapes
        except ImportError:                          # python -m unittest tests.test_flow_multi_terminal
            from tests import test_shared_shapes as shapes
        from kanban.db import sync

        self.shapes, self.sync = shapes, sync
        self.root = Path(tempfile.mkdtemp(prefix="kanban_cas_"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.shared = shapes.SharedShapesTest.make_shared(self, self.root, "", lambda n: n, "", None,
                                                          "2026-10-16 08:32:00")
        self.t = {n: shapes.Terminal(self.root, n, self.shared) for n in ("PC1", "PC2", "倉庫")}
        for t in self.t.values():
            self.addCleanup(t.close)
            t.take()

    def press(self, name: str, action: str, no: str, **kw) -> None:
        t = self.t[name]
        getattr(t.svc, action)(self.shapes.LINE, no, expected_rev=t.item(no).rev, **kw)

    def send(self, name: str):
        t = self.t[name]
        result = self.sync.export_pending(t.store, t.gateway)
        self.assertEqual(result.errors, [], name)
        return result

    def shared_marks(self, no: str) -> tuple:
        conn = sqlite3.connect(str(self.shared))
        try:
            for k, w, sh, at in conn.execute(f"SELECT 管理番号, 欲, 発送, 更新日 FROM [{self.shapes.TABLE}]"):
                if keys.same(k, no):
                    return (w or "", sh or "", at or "")
        finally:
            conn.close()
        raise AssertionError(no)

    def marks(self, name: str, no: str) -> tuple:
        item = self.t[name].item(no)
        return (item.want, item.shipped)

    def events(self, name: str, no: str) -> list[str]:
        return [e["kind"] for e in self.t[name].store.unsent_events() if e["mgmt_no"] == no]

    def test_古い盤の提出は_発送を消さずに下ろして知らせる(self) -> None:
        self.press("PC1", "toggle_order", "1")
        self.send("PC1")
        self.t["倉庫"].take()
        self.press("倉庫", "toggle_ship", "1")
        self.send("倉庫")
        self.press("PC2", "toggle_order", "1")     # PC2 の盤は古い(未発注に見えている)
        self.assertEqual(self.events("PC2", "1"), ["出した"])
        result = self.send("PC2")
        self.assertEqual((result.succeeded, result.failed, result.conflicts), (0, 0, 1))
        self.assertEqual(self.shared_marks("1")[:2], (ON, ON), "倉庫の発送が消えた")
        self.assertEqual(self.marks("PC2", "1"), (ON, ON), "PC2 の盤が共有DBにそろわない")
        store = self.t["PC2"].store
        self.assertEqual(store.pending_count(), 0)
        self.assertEqual([(n["mgmt_no"], n["what"], n["reason"]) for n in store.dropped_notices()],
                         [("1", "発注", "conflict")])
        self.assertEqual(self.events("PC2", "1"), [], "届かなかった提出が看板履歴に数えられる")

    def test_古い盤の届いた確認は_出し直した発注を消さない(self) -> None:
        self.press("PC1", "toggle_order", "2")
        self.send("PC1")
        self.t["倉庫"].take()
        self.press("倉庫", "toggle_ship", "2")
        self.send("倉庫")
        for n in ("PC1", "PC2"):
            self.t[n].take()
        self.press("PC1", "toggle_order", "2", treat_as_delivered=True)
        self.press("PC1", "toggle_order", "2")      # 出し直す(続けて押した 2 つ。まとめて送る)
        self.assertEqual(self.send("PC1").succeeded, 1, "自分の前の操作のせいで弾かれた")
        reordered = self.shared_marks("2")
        self.assertEqual(reordered[:2], (ON, ""))
        self.press("PC2", "toggle_order", "2", treat_as_delivered=True)   # PC2 の盤では 赤+緑
        self.assertEqual(self.send("PC2").conflicts, 1)
        self.assertEqual(self.shared_marks("2"), reordered, "出し直した発注が消えた")
        self.assertEqual(self.marks("PC2", "2"), (ON, ""))
        self.assertEqual([n["reason"] for n in self.t["PC2"].store.dropped_notices()], ["conflict"])

    def test_結果が同じなら黙って下ろす(self) -> None:
        """2 台がほぼ同時に同じ看板を出した。後のほうは送らない(先の時刻が残る)が、知らせない。"""
        self.press("PC1", "toggle_order", "3")
        self.press("PC2", "toggle_order", "3")
        self.send("PC1")
        first = self.shared_marks("3")
        result = self.send("PC2")
        self.assertEqual(result.conflicts, 1)
        self.assertEqual(self.shared_marks("3"), first)
        self.assertEqual(self.t["PC2"].store.dropped_notices(), [])
        self.assertEqual(self.t["PC2"].store.pending_count(), 0)

    def test_送っている最中に押し直しても_自分の送った値で弾かれない(self) -> None:
        """送る(1 回目)と手元の印を下ろすあいだに押し直すと、行は送信待ちのまま残る。
        次の書き戻しは、いま送ったばかりの値を「ほかの端末の操作」と取り違えない。"""
        store = self.t["PC1"].store
        original = store.mark_items_synced

        def press_in_between(keys_, sent=None):
            store.mark_items_synced = original
            self.press("PC1", "toggle_order", "1")  # 送っている最中に取り消す
            return original(keys_, sent)

        self.press("PC1", "toggle_order", "1")
        store.mark_items_synced = press_in_between
        self.send("PC1")
        self.assertEqual(self.shared_marks("1")[:2], (ON, ""))
        self.assertEqual(store.pending_count(), 1)
        result = self.send("PC1")
        self.assertEqual((result.succeeded, result.conflicts), (1, 0), "自分の送った値で弾いた")
        self.assertEqual(self.shared_marks("1")[:2], ("", ""))
        self.assertEqual(store.dropped_notices(), [])

    def test_押す前に取り込んでいれば_今までどおり送れる(self) -> None:
        """正しく取り込んでから押したとき・列が重ならない行き違い(発送と取り消し)は弾かない。"""
        self.press("PC1", "toggle_order", "4")
        self.send("PC1")
        for n in ("PC2", "倉庫"):
            self.t[n].take()
        self.press("倉庫", "toggle_ship", "4")
        self.press("PC2", "toggle_order", "4")      # 取り消し(倉庫の発送はまだ見えない)
        self.assertEqual(self.send("倉庫").succeeded, 1)
        self.assertEqual(self.send("PC2").succeeded, 1, "列の重ならない行き違いを弾いた")
        self.assertEqual(self.shared_marks("4")[:2], ("", ON))   # 取り消された発送(現場が確かめる)
        for t in self.t.values():
            self.assertEqual(t.store.dropped_notices(), [])

    def test_元の値を持たない古い送信待ちは_今までどおり後勝ちで送る(self) -> None:
        self.press("PC1", "toggle_order", "1")
        self.send("PC1")
        self.t["倉庫"].take()
        self.press("倉庫", "toggle_ship", "1")
        self.send("倉庫")
        self.press("PC2", "toggle_order", "1")
        conn = sqlite3.connect(self.t["PC2"].store.path)
        conn.execute("UPDATE kanban_item SET dirty_base = '' WHERE mgmt_no = '1'")   # 前の版で押した
        conn.commit()
        conn.close()
        result = self.send("PC2")
        self.assertEqual((result.succeeded, result.conflicts), (1, 0))

    def test_画面_帯に現場の言葉で出て_わかったで消える(self) -> None:
        """本物のブラウザで、PC2 の看板画面に「ほかの端末が先に操作していた」の帯。"""
        try:
            from _browser import chromium_or_skip
        except ImportError:
            from tests._browser import chromium_or_skip
        from app import create_app
        from kanban import admin_lock

        self.press("PC1", "toggle_order", "1")
        self.send("PC1")
        self.t["倉庫"].take()
        self.press("倉庫", "toggle_ship", "1")
        self.send("倉庫")
        self.press("PC2", "toggle_order", "1")
        self.assertEqual(self.send("PC2").conflicts, 1)

        pw, browser = chromium_or_skip()
        self.addCleanup(pw.stop)
        self.addCleanup(browser.close)
        admin_lock.reset()
        self.addCleanup(admin_lock.reset)
        app = create_app(config.MODE_SITE, token=TOKEN, port=8741, store=self.t["PC2"].store,
                         line=self.shapes.LINE)
        app.config["CONFIG_PATH"] = str(self.root / "config_PC2.json")
        client = app.test_client()

        def forward(route) -> None:
            req = route.request
            path = req.url.split("http://kanban.test", 1)[1]
            res = client.open(path, method=req.method, data=req.post_data_buffer,
                              headers={"X-Tool-Token": TOKEN,
                                       "Content-Type": req.headers.get("content-type", "")})
            route.fulfill(status=res.status_code, body=res.get_data(),
                          headers={"content-type": res.headers.get("Content-Type", "text/plain")})

        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.route("http://kanban.test/**", forward)
        page.goto("http://kanban.test/board", wait_until="load")
        page.wait_for_selector("#dropped:not([hidden])", timeout=8000)
        text = page.text_content("#dropped-text")
        self.assertIn("ほかの端末が先に操作していたので", text)
        self.assertIn(f"{config.display_name(self.shapes.LINE)} 1番", text)
        self.assertIn("の発注は送りませんでした", text)
        self.assertIn("盤を見直して、要ればもう一度押してください", text)
        self.assertNotIn("マスタ管理で消された", text)
        page.wait_for_selector('#board .row[data-no="1"] .cell[data-kind="ship"][data-state="shipped"]',
                               timeout=8000)
        page.click("#dropped-ok")
        page.wait_for_selector("#dropped", state="hidden", timeout=5000)
        self.assertEqual(self.t["PC2"].store.dropped_notices(), [])
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
