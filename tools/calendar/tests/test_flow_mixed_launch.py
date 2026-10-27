"""exe 起動と vbs 起動が**混ざった現場**の通し(PC ごとに別の手元の領域・同じ共有フォルダ)

【現場】PC によって起こし方が違う。
- **exe 起動**: 日報複合ツールの窓(Tauri の外枠)が、このツールの ``bridge.py`` を子として起こし、
  標準入出力で要求を渡す(ポートで待ち受けない)。
- **vbs 起動**: ツールのフォルダの ``Start.vbs`` → ``pythonw start_app.py``(コンソール無し・引数無し)
  → waitress がポートで待ち受け、ブラウザが開く。

【まね方】
- **PC 1台 = 手元の領域1つ**: ``%LOCALAPPDATA%``(ツールは ``<LOCALAPPDATA>\\LineCalendar`` に設定・
  作業用DB・錠・ログを置く)・PC 名(``CALENDAR_PC_NAME``)・ログインID。試験の
  ``CALENDAR_HOME`` / ``CALENDAR_LOCAL_DIR`` は子に渡さない(本物と同じ道で場所を決めさせる)。
- **共有フォルダ = 一時フォルダ1つ**(保存用DB・マスタDB)。どの PC も同じ所を見る。
- **vbs 起動**: Windows の WSH は Linux で動かないので、``Start.vbs`` がすること
  (作業フォルダ = ツールのフォルダ・``pythonw start_app.py``)をまねる。pythonw のまね
  として標準出力・標準エラーを ``None`` にし、ブラウザを開く所(``webbrowser.open``)だけ
  「開いた宛先を書き留める」に替える。開いた宛先は**本物の Chromium** で開いて押す。
- **exe 起動**: 外枠と同じ道(作業フォルダ = ツールのフォルダ・``CALENDAR_TOKEN``・
  ``PYTHONIOENCODING=utf-8``・``ALLTOOLS_SHELL`` など。``../../tests/test_flow_shell.py`` と
  ``../../scripts/desktop_smoke.py`` の ``make_work`` と同じ)で ``bridge.py`` を起こし、外枠の
  代わりに見出し1行 + 本文で要求を渡す。
- ポートは空いているものを取る(PC ごとの ``config/app.json`` の写しを ``CALENDAR_APP_CONFIG`` で渡す。
  本物の PC はそれぞれ自分の 127.0.0.1 を持つので、1台の Linux に並べたときだけ要る)。

**時間には頼らず、状態を待つ**(Windows の CI は遅い)。パスは文字列で比べず、解いた値で比べる。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import socket
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
from typing import Any, Callable, Optional

import bridge as bridge_module
from calendar_app import config
from calendar_app import settings as user_settings
from tests import _web
from tests._browser import chromium_or_skip

ROOT = Path(__file__).resolve().parents[1]          # tools/calendar
REPO = ROOT.parents[1]                               # 日報複合ツールの一式
APP_JSON = ROOT / "config" / "app.json"

#: 状態を待つ上限。時間を計るためではなく、止まったまま待ち続けないため
WAIT = 90.0

#: 班員名簿(マスタDB)。管理番号, 苗字, 班, 名前, 読み, 担当ライン。
#: 日勤の人は直を訊かれない(画面で押す手順が短い)
MEMBERS = (
    ("101", "山田", "B", "山田太郎", "やまだ", "L-1"),
    ("102", "佐藤", "日勤", "佐藤花子", "さとう", "L-1"),
    ("103", "加藤", "日勤", "加藤眞一", "かとう", "L-1"),
    ("104", "伊藤", "日勤", "伊藤五郎", "いとう", "L-1"),
    ("201", "鈴木", "A", "鈴木一郎", "すずき", "コイル"),
)

#: 子に渡さない(本物の PC と同じ道で場所を決めさせる。親の試験の逃がし先が混ざらないように)
_DROP = ("CALENDAR_HOME", "CALENDAR_LOCAL_DIR", "CALENDAR_DISTRIBUTION_DIR", "CALENDAR_APP_CONFIG",
         "CALENDAR_DATA_DB_DIR", "CALENDAR_MASTER_DB_DIR", "ALLTOOLS_DISTRIBUTION_DIR",
         "XDG_DATA_HOME", "PYTHONUTF8", "CALENDAR_TOKEN")

#: vbs 起動のまね(``Start.vbs`` → ``pythonw start_app.py``)。**tests パッケージを読まない**
_VBS_CHILD = r"""
import os, runpy, sys, webbrowser

def _opened(url, *args, **kwargs):
    # ブラウザを開く代わりに、開いた宛先を書き留める(試験が本物の Chromium で開く)
    with open(os.environ["FLOW_OPENED"], "a", encoding="utf-8") as fh:
        fh.write(url + "\n")
    return True

webbrowser.open = _opened
if os.environ.get("FLOW_BEFORE_DESKTOP_GUARD") == "1":
    # デスクトップ版の錠(desktop.lock)を知らない前の版(日報複合ツールに入る前の単品のころ)のまね
    sys.path.insert(0, os.getcwd())
    import launch_guard
    launch_guard.desktop_running = lambda: False
# pythonw.exe にはコンソールが無い(標準出力・標準エラーは None)
sys.stdout = None
sys.stderr = None
script = os.path.join(os.getcwd(), "start_app.py")
sys.argv = [script]
runpy.run_path(script, run_name="__main__")
"""


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _rename_when_free(src: Path, dst: Path, timeout: float = 30.0) -> bool:
    """フォルダの名前を変える。Windows で中のファイルがまだつかまれていれば、放されるまで繰り返す。"""
    deadline = time.monotonic() + timeout
    while True:
        try:
            src.rename(dst)
            return True
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)


def wait_for(ok: Callable[[], Any], what: str, timeout: float = WAIT) -> Any:
    """``ok()`` が真になるまで待つ(状態を待つ。時間で決め打ちしない)。"""
    deadline = time.monotonic() + timeout
    while True:
        value = ok()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"{what} になりません")
        time.sleep(0.1)


def wait_members(run) -> None:
    """起動したときの取り込みで、班員名簿が手元に入るのを待つ。"""
    wait_for(lambda: [w for line in run.get("/api/workers")["lines"]
                      for g in line["groups"] for w in g["workers"]],
             f"{run.pc.name}({run.edition}) の班員名簿")


def same_path(a: Any, b: Any) -> bool:
    return Path(str(a)).resolve() == Path(str(b)).resolve()


def make_shared(folder: Path, members=MEMBERS) -> Path:
    """共有フォルダ: 保存用DB(連絡帳)とマスタDB(班員名簿入り)。"""
    folder.mkdir(parents=True, exist_ok=True)
    _web.make_source(folder, config.SOURCE_FILE_DATA, _web.SOURCE_SCHEMA)
    master = _web.make_source(folder, config.SOURCE_FILE_MASTER, _web.MASTER_SCHEMA)
    conn = sqlite3.connect(master)
    try:
        conn.executemany(f'INSERT INTO "{config.TABLE_MEMBER}" VALUES (?,?,?,?,?,?)', members)
        conn.commit()
    finally:
        conn.close()
    return folder


def future_workday(offset_days: int) -> _dt.date:
    """来月の平日(今月の残りの日数に左右されない)。"""
    from calendar_app.holiday import holiday_name

    today = _dt.date.today()
    year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    day = _dt.date(year, month, 1) + _dt.timedelta(days=offset_days)
    while day.weekday() >= 5 or holiday_name(day):
        day += _dt.timedelta(days=1)
    return day


def key(day: _dt.date) -> str:
    return day.strftime(config.DATE_KEY_FORMAT)


# ===========================================================================
# PC 1台(手元の領域)
# ===========================================================================
class Pc:
    """PC 1台。``%LOCALAPPDATA%``・PC 名・ログインIDが PC ごとに違う。"""

    def __init__(self, base: Path, name: str, *, line: str = "", shared: Optional[Path] = None,
                 tool_dir: Path = ROOT, settings: Optional[dict] = None,
                 version: str = "") -> None:
        self.name = name
        self.pc_name = f"PC-{name}"
        self.login = f"user{name.lower()}"
        self.tool_dir = tool_dir
        self.dir = base / name
        self.local_app_data = self.dir / "AppData" / "Local"
        self.local_app_data.mkdir(parents=True, exist_ok=True)
        # PC ごとの config/app.json(空いているポート。版を変えるときもここ)
        conf = json.loads((tool_dir / "config" / "app.json").read_text(encoding="utf-8"))
        conf["server"]["port"] = free_port()
        conf["server"]["port_retry"] = 0
        if version:
            conf["version"] = version
        self.app_json = self.dir / "app.json"
        self.app_json.write_text(json.dumps(conf, ensure_ascii=False), encoding="utf-8")
        self.version = conf["version"]
        values: dict[str, Any] = {}
        if shared is not None:
            values.update({user_settings.KEY_DATA_DB_DIR: str(shared),
                           user_settings.KEY_MASTER_DB_DIR: str(shared),
                           # 自動同期は切る: 届く時機を試験が決める(「今すぐ同期」で取り込む)
                           user_settings.KEY_AUTO_SYNC: False})
        if line:
            values[user_settings.KEY_MY_LINE] = line
        values.update(settings or {})
        if values:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            self.settings_path.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")

    # -- この PC の場所(ツールが決める道と同じ) ------------------------------
    @property
    def local_root(self) -> Path:
        return self.local_app_data / "LineCalendar"

    @property
    def settings_path(self) -> Path:
        return self.local_root / "data" / "settings.json"

    def settings(self) -> dict:
        try:
            return json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def env(self, *, distribution: Optional[Path] = None) -> dict:
        env = {k: v for k, v in os.environ.items() if k not in _DROP}
        env.update({
            "LOCALAPPDATA": str(self.local_app_data),
            "CALENDAR_PC_NAME": self.pc_name,
            "COMPUTERNAME": self.pc_name,
            "CALENDAR_LOGIN_ID": self.login,
            "USERNAME": self.login,
            "CALENDAR_APP_CONFIG": str(self.app_json),
            "PYTHONIOENCODING": "utf-8",
        })
        # 配布設定: 指定が無ければ、この PC だけの無い場所(ツールのフォルダの本物を読まない)
        if distribution is not None:
            env["CALENDAR_DISTRIBUTION_DIR"] = str(distribution)
        elif self.tool_dir == ROOT:
            env["CALENDAR_DISTRIBUTION_DIR"] = str(self.dir / "配布設定")
        return env

    def vbs(self, case: unittest.TestCase, **kwargs) -> "VbsRun":
        run = VbsRun(self, **kwargs)
        case.addCleanup(run.stop)
        return run

    def exe(self, case: unittest.TestCase, **kwargs) -> "ExeRun":
        run = ExeRun(self, **kwargs)
        case.addCleanup(run.stop)
        return run


# ===========================================================================
# 起こし方
# ===========================================================================
class VbsRun:
    """vbs 起動(``Start.vbs`` → ``pythonw start_app.py``)。ブラウザで開いた宛先を持つ。"""

    edition = "vbs"

    def __init__(self, pc: Pc, *, distribution: Optional[Path] = None, wait: bool = True,
                 before_desktop_guard: bool = False) -> None:
        self.pc = pc
        self.opened = pc.dir / f"opened-{uuid.uuid4().hex[:6]}.txt"
        env = pc.env(distribution=distribution)
        env["FLOW_OPENED"] = str(self.opened)
        if before_desktop_guard:
            env["FLOW_BEFORE_DESKTOP_GUARD"] = "1"
        # Start.vbs: shell.CurrentDirectory = here / pythonw "<here>\start_app.py"
        self.proc = subprocess.Popen([sys.executable, "-c", _VBS_CHILD], cwd=str(pc.tool_dir), env=env,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        self.url = ""
        self.base = ""
        self.token = ""
        if wait:
            self.wait_opened()

    def opened_urls(self) -> list[str]:
        try:
            return [u for u in self.opened.read_text(encoding="utf-8").splitlines() if u.strip()]
        except OSError:
            return []

    def wait_opened(self) -> str:
        """ブラウザを開くところまで来るのを待つ(開いた宛先 = 利用者が見る画面)。"""
        def ready() -> Optional[str]:
            urls = self.opened_urls()
            if urls:
                return urls[-1]
            if self.proc.poll() is not None:
                raise AssertionError(f"{self.pc.name} の vbs 起動が先に終わりました"
                                     f"(終了コード {self.proc.returncode})\n{self.log_tail()}")
            return None
        self.url = wait_for(ready, f"{self.pc.name} の vbs 起動がブラウザを開く")
        if self.url.startswith("http"):
            head, _, query = self.url.partition("?")
            self.base = head.rstrip("/")
            self.token = dict(p.split("=", 1) for p in query.split("&") if "=" in p).get("t", "")
        return self.url

    def log_tail(self) -> str:
        folder = self.pc.local_root / "logs"
        text = "".join(p.read_text(encoding="utf-8", errors="replace")
                       for p in sorted(folder.glob("*.log"))) if folder.is_dir() else ""
        return text[-3000:]

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base}{path}", data=data, method=method,
            headers={"X-Tool-Token": self.token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw, status = response.read(), response.status
        except urllib.error.HTTPError as exc:
            raw, status = exc.read(), exc.code
        try:
            return status, json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            return status, raw.decode("utf-8", "replace")

    def get(self, path: str) -> Any:
        status, body = self.call("GET", path)
        if status != 200:
            raise AssertionError(f"{self.pc.name}(vbs) GET {path}: {status} {body}")
        return body

    def wait_ready(self) -> None:
        wait_for(lambda: self.get("/api/health").get("ready"), f"{self.pc.name}(vbs) の準備完了")

    def stop(self) -> None:
        if self.proc.poll() is None:
            if self.base:
                try:
                    self.call("POST", "/api/shutdown", {"force": True})
                except OSError:
                    pass
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=15)


class ExeRun:
    """exe 起動(外枠が ``bridge.py`` を子として起こす)。外枠の代わりに要求を渡す。"""

    edition = "exe"

    def __init__(self, pc: Pc, *, distribution: Optional[Path] = None, claim: bool = True) -> None:
        self.pc = pc
        self.token = f"tok-{uuid.uuid4().hex[:8]}"
        env = pc.env(distribution=distribution)
        # 外枠が渡すもの(src-tauri/src/shell.rs の Shell::new と bridge.rs の spawn)
        env.update({"CALENDAR_TOKEN": self.token, "ALLTOOLS_SHELL": "1",
                    "ALLTOOLS_TOOL": "calendar", "ALLTOOLS_ROOT": str(pc.tool_dir.parents[1])})
        self.proc = subprocess.Popen([sys.executable, str(pc.tool_dir / "bridge.py")], cwd=str(pc.tool_dir),
                                     env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
        self.events: list[dict] = []
        self.replies: dict[int, tuple[dict, bytes]] = {}
        self.err: list[str] = []
        self.cond = threading.Condition()
        self.next_id = 1
        self.screen = ""
        threading.Thread(target=self._drain, daemon=True).start()
        threading.Thread(target=self._read, daemon=True).start()
        self.first = self.first_word()
        if self.first.get("event") == "started" and claim:
            self.wait_ready()
            self.claim()

    def _drain(self) -> None:
        for line in self.proc.stderr:            # type: ignore[union-attr]
            self.err.append(line.decode("utf-8", "replace").rstrip())
            del self.err[:-200]

    def _read(self) -> None:
        while True:
            try:
                frame = bridge_module.read_frame(self.proc.stdout)   # type: ignore[arg-type]
            except (ValueError, EOFError, OSError):
                frame = None
            with self.cond:
                if frame is None:
                    self.events.append({"event": "<eof>"})
                    self.cond.notify_all()
                    return
                head, body = frame
                if "event" in head:
                    self.events.append(head)
                else:
                    self.replies[head.get("id")] = (head, body)
                self.cond.notify_all()

    def first_word(self) -> dict:
        deadline = time.monotonic() + WAIT
        with self.cond:
            while not self.events and time.monotonic() < deadline:
                self.cond.wait(0.2)
            if not self.events:
                raise AssertionError(f"{self.pc.name}(exe): 1行目が来ません\n" + "\n".join(self.err[-20:]))
            return self.events[0]

    def raw(self, method: str, path: str, data: bytes = b"",
            headers: Optional[list] = None) -> tuple[int, list, bytes]:
        """要求1件(外枠の relay と同じ: Host は app.localhost・合言葉を付ける)。"""
        with self.cond:
            rid = self.next_id
            self.next_id += 1
        sent = [["Host", "app.localhost"], ["X-Tool-Token", self.token], *(headers or [])]
        path, _, query = path.partition("?")
        head = {"id": rid, "method": method, "path": path, "query": query, "headers": sent,
                "len": len(data)}
        assert self.proc.stdin is not None
        with self.cond:
            self.proc.stdin.write(json.dumps(head, ensure_ascii=False).encode("utf-8") + b"\n" + data)
            self.proc.stdin.flush()
        deadline = time.monotonic() + WAIT
        with self.cond:
            while rid not in self.replies and time.monotonic() < deadline:
                self.cond.wait(0.2)
            if rid not in self.replies:
                raise AssertionError(f"{self.pc.name}(exe) {method} {path} に答えません\n"
                                     + "\n".join(self.err[-20:]))
            reply, body = self.replies.pop(rid)
        return reply["status"], reply.get("headers", []), body

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        data = b"" if body is None else json.dumps(body).encode("utf-8")
        headers = [["Content-Type", "application/json"], ["Accept", "application/json"]]
        if self.screen:
            headers.append(["X-Screen-Id", self.screen])
        status, _, raw = self.raw(method, path, data, headers)
        try:
            return status, json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            return status, raw.decode("utf-8", "replace")

    def get(self, path: str) -> Any:
        status, body = self.call("GET", path)
        if status != 200:
            raise AssertionError(f"{self.pc.name}(exe) GET {path}: {status} {body}")
        return body

    def post(self, path: str, body: Any) -> tuple[int, Any]:
        return self.call("POST", path, body)

    def wait_ready(self) -> None:
        wait_for(lambda: self.get("/api/health").get("ready"), f"{self.pc.name}(exe) の準備完了")

    def claim(self) -> None:
        """画面を名乗る(外枠の窓のタブを開いたときと同じ)。"""
        self.screen = uuid.uuid4().hex
        status, body = self.post("/api/screen/claim", {"screen_id": self.screen, "force": True})
        if status != 200:
            raise AssertionError(f"{self.pc.name}(exe) 画面を名乗れません: {body}")

    def stop(self) -> None:
        """外枠が閉じた(標準入力を閉じる)。bridge は送れるだけ送って終わる。"""
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        except OSError:
            pass
        if self.proc.poll() is None:
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=15)
        for stream in (self.proc.stdout, self.proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass


# ===========================================================================
# 試験の土台
# ===========================================================================
class _MixedCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="calendar_mixed_")
        self.addCleanup(_web._remove_when_released, tmp)
        self.base = Path(tmp.name)
        self.shared = make_shared(self.base / "共有")
        self.data_db = self.shared / config.SOURCE_FILE_DATA

    def shared_rows(self, day: Optional[_dt.date] = None, table: str = config.TABLE_DATA) -> list[dict]:
        rows = _web.read_source(self.data_db, table)
        if day is None:
            return rows
        return [r for r in rows if str(r.get("日付", "")).startswith(key(day))
                or str(r.get("日付", "")).replace("-", "/").startswith(key(day))]


class _BrowserMixedCase(_MixedCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.pw, cls.browser = chromium_or_skip()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.pw.stop()

    def setUp(self) -> None:
        super().setUp()
        self.errors: list[str] = []

    # -- vbs の PC の画面(本物の Chromium) ---------------------------------
    def open_screen(self, run: VbsRun):
        ctx = self.browser.new_context(viewport={"width": 1400, "height": 900})
        self.addCleanup(ctx.close)
        page = ctx.new_page()
        page.set_default_timeout(int(WAIT * 1000))
        page.on("pageerror", lambda e: self.errors.append(f"{run.pc.name}: {e}"))
        page.on("dialog", lambda d: d.accept())
        page.goto(run.url, wait_until="load")      # Start.vbs が開いた宛先そのもの
        page.wait_for_selector(".day[data-date]")
        return page

    def open_exe_screen(self, run: "ExeRun"):
        """exe の PC の画面: 外枠の窓の代わりに Chromium で開き、要求は全部 bridge.py へ渡す。"""
        origin = "http://calendar.localhost"
        ctx = self.browser.new_context(viewport={"width": 1400, "height": 900})
        self.addCleanup(ctx.close)
        # 外枠の窓の中(window.__TAURI__ がある)。窓まわりの頼みごとは何もしない
        ctx.add_init_script("window.__TAURI__ = { core: { invoke: async () => null } };")

        def forward(route) -> None:
            req = route.request
            headers = [[k, v] for k, v in req.headers.items()
                       if k.lower() in ("content-type", "accept", "x-screen-id")]   # 合言葉は raw が付ける
            status, got, body = run.raw(req.method, req.url.split(origin, 1)[1] or "/",
                                        req.post_data_buffer or b"", headers)
            kept = {k: v for k, v in got if k.lower() in ("content-type", "location", "cache-control")}
            route.fulfill(status=status, body=body, headers=kept)

        ctx.route(f"{origin}/**", forward)
        page = ctx.new_page()
        page.set_default_timeout(int(WAIT * 1000))
        page.on("pageerror", lambda e: self.errors.append(f"{run.pc.name}(exe): {e}"))
        page.on("dialog", lambda d: d.accept())
        page.goto(f"{origin}/calendar", wait_until="load")
        page.wait_for_selector(".day[data-date]")
        return page

    def show_month(self, page, day: _dt.date) -> None:
        """その日の月を出す(来月より先へは「次の月」を押す。押すたびに題が変わるのを待つ)。"""
        title = f"{day.year}年 {day.month}月"
        for _ in range(14):
            shown = page.inner_text("#cal-title").strip()
            if shown == title:
                break
            page.click("#cal-next")
            page.wait_for_function("(b) => document.getElementById('cal-title').textContent.trim() !== b",
                                   arg=shown)
        page.wait_for_function("(t) => document.getElementById('cal-title').textContent.trim() === t", arg=title)
        page.wait_for_selector(f'.day[data-date="{key(day)}"]')

    def choose(self, page, day: _dt.date, action: str) -> None:
        self.show_month(page, day)
        page.click(f'.day[data-date="{key(day)}"]')
        page.wait_for_selector(".modal button.choice")
        page.click(f".modal button.choice:has-text('{action}')")

    def press(self, page, label: str) -> None:
        page.click(f".modal:last-of-type .modal__foot button:text-is('{label}')")

    def wait_closed(self, page) -> None:
        page.wait_for_selector(".modal", state="detached")

    def wait_cell(self, page, day: _dt.date, text: str, *, gone: bool = False) -> None:
        self.show_month(page, day)
        page.wait_for_function(
            "([sel, text, gone]) => { const c = document.querySelector(sel);"
            " const has = !!c && (c.innerText || '').includes(text); return gone ? !has : has; }",
            arg=[f'.day[data-date="{key(day)}"]', text, gone])

    def ui_rest(self, page, day: _dt.date, name: str) -> None:
        """休み(日勤の人: 直を訊かれない)。"""
        self.choose(page, day, "休み")
        page.click(f".modal:last-of-type button.worker:text-is('{name}')")
        self.wait_closed(page)
        self.wait_cell(page, day, f"{name} 休")

    def ui_comment(self, page, day: _dt.date, line: str, group: str, text: str) -> None:
        self.choose(page, day, "コメント・連絡")
        page.wait_for_selector("#cm-group")
        if not page.is_disabled("#cm-line"):
            page.select_option("#cm-line", line)     # 作業長の端末だけ選べる
        page.select_option("#cm-group", group)
        self.press(page, "次へ")
        page.wait_for_selector("#cm-text")
        page.fill("#cm-text", text)
        self.press(page, "登録する")
        self.wait_closed(page)

    def ui_delete(self, page, day: _dt.date, words: list[str]) -> None:
        self.choose(page, day, "削除")
        page.wait_for_selector("#del-list")
        for word in words:
            page.check(f"#del-list .pick:has-text('{word}') input")
        self.press(page, "選んだものを削除")
        page.wait_for_selector(".modal .modal__head h2:text-is('削除の確認')")
        self.press(page, "削除する")
        self.wait_closed(page)

    def ui_sync(self, page) -> dict:
        """「今すぐ同期」を押し、答えが来るまで待つ。"""
        with page.expect_response(lambda r: "/api/sync/now" in r.url, timeout=WAIT * 1000) as info:
            page.click("#sync-now")
        body = info.value.json()
        self.assertEqual(info.value.status, 200, body)
        return body

    def ui_viewer(self, page, day: _dt.date) -> str:
        self.choose(page, day, "内容閲覧")
        page.wait_for_selector(".modal .viewer")
        text = page.inner_text(".modal .viewer")
        self.press(page, "閉じる")
        self.wait_closed(page)
        return text

    def ribbon_sync(self, page) -> str:
        return page.inner_text("#rb-sync").strip()


# ===========================================================================
# 1. カレンダー: vbs のラインと exe のラインで、登録 → 届く → 消す → 消える(両向き)
# ===========================================================================
class _RoundTrip(_BrowserMixedCase):
    """ライン A = L-1、ライン B = 作業長(全部見える)。どちらが vbs かは下の2つの組で入れ替える。"""

    A_VBS = True

    def setUp(self) -> None:
        super().setUp()
        self.pc_a = Pc(self.base, "A", line="L-1", shared=self.shared)
        self.pc_b = Pc(self.base, "B", line="作業長", shared=self.shared)
        vbs_pc, exe_pc = (self.pc_a, self.pc_b) if self.A_VBS else (self.pc_b, self.pc_a)
        self.vbs = vbs_pc.vbs(self)
        self.exe = exe_pc.exe(self)
        self.assertEqual(self.exe.first.get("event"), "started", self.exe.first)
        self.vbs.wait_ready()
        self.page = self.open_screen(self.vbs)
        # 起動したときの取り込みで班員名簿が入るのを待つ(両方)
        for run in (self.vbs, self.exe):
            wait_members(run)

    # -- 片方ずつの操作(vbs は画面を押す・exe は外枠の代わりに要求を渡す) -----
    def is_vbs(self, pc: Pc) -> bool:
        return self.vbs.pc is pc

    def run_of(self, pc: Pc):
        return self.vbs if self.is_vbs(pc) else self.exe

    def rest(self, pc: Pc, day: _dt.date, code: str, name: str) -> None:
        if self.is_vbs(pc):
            self.ui_rest(self.page, day, name)
            return
        status, body = self.exe.post("/api/rest", {"date": key(day), "code": code})
        self.assertEqual(status, 200, body)

    def comment(self, pc: Pc, day: _dt.date, line: str, group: str, text: str) -> None:
        if self.is_vbs(pc):
            self.ui_comment(self.page, day, line, group, text)
            return
        status, body = self.exe.post("/api/comment", {"date": key(day), "line": line,
                                                      "group": group, "text": text})
        self.assertEqual(status, 200, body)

    def delete(self, pc: Pc, day: _dt.date, words: list[str]) -> None:
        if self.is_vbs(pc):
            self.ui_delete(self.page, day, words)
            return
        items = [i for i in self.exe.get(f"/api/day/{key(day)}")["items"]
                 if any(w in i["caption"] for w in words)]
        self.assertEqual(len(items), len(words), items)
        status, body = self.exe.post("/api/delete", {
            "date": key(day), "ids": [i["id"] for i in items],
            "marks": {str(i["id"]): i["mark"] for i in items}})
        self.assertEqual(status, 200, body)

    def sync(self, pc: Pc) -> dict:
        if self.is_vbs(pc):
            return self.ui_sync(self.page)["sync"]
        status, body = self.exe.post("/api/sync/now", {})
        self.assertEqual(status, 200, body)
        return body["sync"]

    def captions(self, pc: Pc, day: _dt.date) -> list[str]:
        return [i["caption"] for i in self.run_of(pc).get(f"/api/day/{key(day)}")["items"]]

    def pending(self, pc: Pc) -> int:
        return int(self.run_of(pc).get("/api/sync")["pending"])

    def sync_both(self) -> None:
        """送り終えてから、両方が取り込む(どちらが先に送っても届くように2巡)。"""
        for pc in (self.pc_a, self.pc_b, self.pc_a, self.pc_b):
            self.sync(pc)
        for pc in (self.pc_a, self.pc_b):
            self.assertEqual(self.pending(pc), 0, f"{pc.name} に未送信が残っている")

    # -- 通し ---------------------------------------------------------------
    def test_登録が相手に届き_相手が消すとこちらからも消える(self) -> None:
        day = future_workday(3)
        # A(L-1)が休みと連絡、B(作業長)が L-1 の人の休みと L-1 宛の連絡
        self.rest(self.pc_a, day, "102", "佐藤花子")
        self.comment(self.pc_a, day, "L-1", "B", "Aから:2号機の段取り替え")
        self.rest(self.pc_b, day, "103", "加藤眞一")
        self.comment(self.pc_b, day, "L-1", "日勤", "Bから:午後は検査")
        self.sync_both()
        self.assertEqual(len(self.shared_rows(day)), 4, "共有には4件が1回ずつ")

        # どちらの PC にも4件。二重に届いていない
        for pc in (self.pc_a, self.pc_b):
            with self.subTest(pc=pc.name):
                captions = self.captions(pc, day)
                self.assertEqual(len(captions), 4, captions)
                for word in ("佐藤花子", "加藤眞一", "2号機の段取り替え", "午後は検査"):
                    self.assertTrue(any(word in c for c in captions), (word, captions))
        # vbs の画面: 開いたままでも、取り込んだものがマスに出る
        self.wait_cell(self.page, day, "佐藤花子 休")
        self.wait_cell(self.page, day, "加藤眞一 休")
        viewer = self.ui_viewer(self.page, day)
        self.assertIn("2号機の段取り替え", viewer)
        self.assertIn("午後は検査", viewer)

        # **相手が登録したもの**を消す(A は B の分、B は A の分)
        self.delete(self.pc_a, day, ["加藤眞一", "午後は検査"])
        self.delete(self.pc_b, day, ["佐藤花子", "2号機の段取り替え"])
        self.sync_both()
        self.assertEqual(self.shared_rows(day), [], "共有から消えた")
        deleted = {r["登録内容"] for r in self.shared_rows(table=config.TABLE_DEL_HISTORY)}
        self.assertTrue({"佐藤花子", "加藤眞一"} <= deleted, deleted)
        for pc in (self.pc_a, self.pc_b):
            with self.subTest(pc=pc.name):
                self.assertEqual(self.captions(pc, day), [], f"{pc.name} から消えていない")
        self.wait_cell(self.page, day, "佐藤花子", gone=True)
        self.wait_cell(self.page, day, "加藤眞一", gone=True)
        self.assertEqual(self.errors, [])

    def test_同じ人の同じ日をほぼ同時に送っても1件だけ_未送信の数も両方で正しい(self) -> None:
        day = future_workday(5)
        # 共有フォルダが見えないあいだに、両方で同じ人(加藤眞一)の同じ日を登録する
        hidden = self.shared.with_name("共有_見えない")
        self.shared.rename(hidden)
        renamed = [True]

        def back() -> None:
            if renamed[0]:
                hidden.rename(self.shared)
                renamed[0] = False
        self.addCleanup(back)
        self.rest(self.pc_a, day, "103", "加藤眞一")
        self.rest(self.pc_b, day, "103", "加藤眞一")
        for pc in (self.pc_a, self.pc_b):
            with self.subTest(pc=pc.name):
                status = wait_for(lambda pc=pc: (lambda s: s if s["pending"] == 1 else None)(
                    self.run_of(pc).get("/api/sync")), f"{pc.name} の未送信 1 件")
                self.assertIn("未送信 1 件", status["text"])
                self.assertTrue(status["offline"])
        # vbs の帯にも同じ数
        self.page.wait_for_function("document.getElementById('rb-sync').textContent.includes('未送信 1 件')")

        # 共有が戻った。**2台がほぼ同時に送る**(exe は別のスレッドから、vbs は画面のボタン)
        back()
        exe_pc = self.exe.pc
        result: dict = {}
        go = threading.Event()

        def exe_send() -> None:
            go.wait(WAIT)
            result["exe"] = self.sync(exe_pc)
        sender = threading.Thread(target=exe_send, daemon=True)
        sender.start()
        go.set()
        result["vbs"] = self.sync(self.vbs.pc)
        sender.join(WAIT)
        self.assertFalse(sender.is_alive())

        # 共有には1件だけ(insert_unless: 確かめと書き込みが共有の錠1つの中)
        wait_for(lambda: all(self.pending(pc) == 0 for pc in (self.pc_a, self.pc_b)), "両方の未送信 0 件")
        rows = [r for r in self.shared_rows(day) if str(r.get("識別コード")) == "103"]
        self.assertEqual(len(rows), 1, rows)
        # 後から送ったほうには「送らなかった」の知らせ(どちらか1台だけ)
        skipped = {run.edition: int((run.get("/api/sync").get("skipped") or {}).get("count") or 0)
                   for run in (self.vbs, self.exe)}
        self.assertEqual(sum(skipped.values()), 1, skipped)
        if skipped["vbs"]:
            # vbs の画面にも出る(確かめたと押すまで残る)
            self.page.wait_for_selector(".modal .modal__head h2:text-is('送らなかった登録があります')")
            self.assertIn("加藤眞一", self.page.inner_text(".modal:last-of-type"))
            self.press(self.page, "閉じる")
            self.wait_closed(self.page)
        # 取り込んだあとも、どちらの PC にも1件
        self.sync_both()
        for pc in (self.pc_a, self.pc_b):
            with self.subTest(pc=pc.name):
                captions = [c for c in self.captions(pc, day) if "加藤眞一" in c]
                self.assertEqual(len(captions), 1, captions)
        self.wait_cell(self.page, day, "加藤眞一 休")
        self.assertEqual(self.errors, [])


class RoundTripVbsA_ExeB(_RoundTrip):
    """ライン A(L-1)= vbs 起動、ライン B(作業長)= exe 起動。"""
    A_VBS = True


class RoundTripExeA_VbsB(_RoundTrip):
    """その逆: ライン A(L-1)= exe 起動、ライン B(作業長)= vbs 起動。"""
    A_VBS = False


# ===========================================================================
# 1'. 参照パスが未設定の帯は、どちらの起こし方でも同じ
# ===========================================================================
class UnconfiguredTests(_BrowserMixedCase):
    def test_参照パスが未設定なら_vbsもexeも帯でそう言い_登録を断る(self) -> None:
        vbs = Pc(self.base, "V", line="L-1").vbs(self)
        exe = Pc(self.base, "E", line="L-1").exe(self, claim=False)
        self.assertEqual(exe.first.get("event"), "started", exe.first)
        vbs.wait_ready()
        exe.wait_ready()
        for page in (self.open_screen(vbs), self.open_exe_screen(exe)):
            page.wait_for_function(
                "document.getElementById('rb-sync').textContent.trim() === '参照パスが未設定'")
        exe.claim()
        for run in (vbs, exe):
            with self.subTest(edition=run.edition):
                status = run.get("/api/sync")
                self.assertEqual(status["text"], "参照パスが未設定")
                self.assertFalse(status["configured"])
                self.assertEqual(status["pending"], 0)
        status, body = exe.post("/api/rest", {"date": key(future_workday(2)), "code": "102"})
        self.assertNotEqual(status, 200)
        self.assertIn("参照パス", json.dumps(body, ensure_ascii=False))
        self.assertEqual(self.errors, [])


class RibbonTests(_BrowserMixedCase):
    def test_共有が見えないあいだの未送信の数が_vbsとexeの画面の帯に同じく出る(self) -> None:
        vbs = Pc(self.base, "V", line="L-1", shared=self.shared).vbs(self)
        exe = Pc(self.base, "E", line="作業長", shared=self.shared).exe(self, claim=False)
        self.assertEqual(exe.first.get("event"), "started", exe.first)
        vbs.wait_ready()
        exe.wait_ready()
        for run in (vbs, exe):
            wait_members(run)
        pages = {"vbs": self.open_screen(vbs), "exe": self.open_exe_screen(exe)}
        for page in pages.values():
            page.wait_for_function("document.getElementById('rb-sync').textContent.startsWith('同期済み')")
        day = future_workday(10)
        hidden = self.shared.with_name("共有_見えない")
        self.shared.rename(hidden)
        self.addCleanup(lambda: hidden.exists() and hidden.rename(self.shared))
        self.ui_rest(pages["vbs"], day, "佐藤花子")
        self.ui_rest(pages["exe"], day, "伊藤五郎")
        for edition, page in pages.items():
            with self.subTest(edition=edition):
                page.wait_for_function("document.getElementById('rb-sync').textContent.includes('未送信 1 件')")
        self.ui_comment(pages["vbs"], day, "L-1", "B", "vbsから")
        self.ui_comment(pages["exe"], day, "L-1", "日勤", "exeから")
        for edition, page in pages.items():
            with self.subTest(edition=edition):
                page.wait_for_function("document.getElementById('rb-sync').textContent.includes('未送信 2 件')")
        # 共有が戻った → 送って取り込む。帯は「同期済み」に戻り、相手の分がマスに出る
        hidden.rename(self.shared)
        for _ in range(2):
            for page in pages.values():
                self.ui_sync(page)
        for edition, page in pages.items():
            with self.subTest(edition=edition):
                page.wait_for_function("document.getElementById('rb-sync').textContent.startsWith('同期済み')")
                self.wait_cell(page, day, "佐藤花子 休")
                self.wait_cell(page, day, "伊藤五郎 休")
        self.assertEqual(len(self.shared_rows(day)), 4)
        self.assertEqual(self.errors, [])


# ===========================================================================
# 3. 多重起動の守り: 同じ PC なら片方が理由を言って止まる・違う PC なら両方動く
# ===========================================================================
class GuardTests(_MixedCase):
    def test_同じPCでvbsが先なら_exeは理由を言って止まり_vbsは動き続ける(self) -> None:
        pc = Pc(self.base, "X", line="L-1", shared=self.shared)
        vbs = pc.vbs(self)
        vbs.wait_ready()
        exe = pc.exe(self, claim=False)
        self.assertEqual(exe.first.get("event"), "fatal", exe.first)
        self.assertIn("ブラウザ版が動いています", exe.first.get("message", ""))
        self.assertIn("「終了」", exe.first.get("hint", ""))
        exe.proc.wait(timeout=WAIT)
        self.assertTrue(vbs.get("/api/health")["ready"], "先に動いていた vbs は動き続ける")
        self.assertIsNone(vbs.proc.poll())

    def test_同じPCでexeが先なら_vbsはエラーの画面を開いて止まり_exeは動き続ける(self) -> None:
        pc = Pc(self.base, "X", line="L-1", shared=self.shared)
        exe = pc.exe(self)
        self.assertEqual(exe.first.get("event"), "started", exe.first)
        vbs = pc.vbs(self, wait=False)
        url = vbs.wait_opened()
        self.assertEqual(vbs.proc.wait(timeout=WAIT), 1)
        # pythonw なのでコンソールは誰も見ない。**ブラウザでエラーの画面を開く**
        self.assertTrue(url.startswith("file:"), url)
        page = (pc.local_root / "work" / "起動エラー.html").read_text(encoding="utf-8")
        self.assertIn("デスクトップ版(日報複合ツールの窓)で動いています", page)
        self.assertIn("日報複合ツールの窓の「カレンダー」のタブ", page)
        self.assertTrue(exe.get("/api/health")["ready"], "先に動いていた exe は動き続ける")

    def test_同じPCでexeが動いていれば_錠を知らない前の版のvbsも一緒には動かない(self) -> None:
        """単品のころのカレンダー(日報複合ツールに入る前の版)の Start.vbs が、同じ PC に残っている。

        前の版はデスクトップ版の錠(desktop.lock)を見ない。前は exe が動いていても前の版の vbs が
        **同じ手元の作業用DB・設定のまま一緒に動き出した**(逆の順なら exe が止まる)。
        exe はブラウザ版の入口の錠(instance.lock)も握るので、前の版は入口が空くのを待ってあきらめる。
        """
        pc = Pc(self.base, "X", line="L-1", shared=self.shared)
        exe = pc.exe(self)
        self.assertEqual(exe.first.get("event"), "started", exe.first)
        old = pc.vbs(self, wait=False, before_desktop_guard=True)
        # 終わるか、画面を開く(= 一緒に動き出した)かのどちらかを待つ
        wait_for(lambda: old.proc.poll() is not None
                 or [u for u in old.opened_urls() if u.startswith("http")], "前の版の vbs の行き先")
        self.assertFalse([u for u in old.opened_urls() if u.startswith("http")],
                         "前の版の vbs が、exe と同じ手元の領域で一緒に動き出した")
        self.assertEqual(old.proc.wait(timeout=WAIT), 0)
        self.assertTrue(exe.get("/api/health")["ready"], "exe は動き続ける")
        # いまの版の vbs は、待たずに理由を言って止まる(エラーの画面)
        vbs = pc.vbs(self, wait=False)
        self.assertTrue(vbs.wait_opened().startswith("file:"))
        self.assertEqual(vbs.proc.wait(timeout=WAIT), 1)

    def test_違うPCならvbsとexeが両方動き_錠は手元の領域にあって共有には無い(self) -> None:
        before = sorted(p.relative_to(self.shared) for p in self.shared.rglob("*"))
        pc_x = Pc(self.base, "X", line="L-1", shared=self.shared)
        pc_y = Pc(self.base, "Y", line="L-1", shared=self.shared)
        vbs = pc_x.vbs(self)
        exe = pc_y.exe(self)
        self.assertEqual(exe.first.get("event"), "started", exe.first)
        vbs.wait_ready()
        # 逆の起こし方も: Y で vbs、X で exe(どちらも相手の PC の錠を見ない)
        vbs2 = Pc(self.base, "Z", line="L-1", shared=self.shared).vbs(self)
        exe2 = Pc(self.base, "W", line="L-1", shared=self.shared).exe(self)
        self.assertEqual(exe2.first.get("event"), "started", exe2.first)
        vbs2.wait_ready()
        self.assertTrue(vbs.get("/api/health")["ready"])
        self.assertTrue(exe.get("/api/health")["ready"])
        # 錠は PC ごとの手元の領域(runtime)
        self.assertTrue((pc_x.local_root / "runtime" / "instance.lock").is_file())
        self.assertTrue((pc_y.local_root / "runtime" / "desktop.lock").is_file())
        # 共有フォルダに錠・ログ・作業用の物を置かない(SQLite のジャーナルの消え残りも無い)。
        # 裏の同期が書いている最中は、SQLite が一時的に「-journal」を作る(書き終われば消える。
        # Windows の CI でその瞬間に当たった)ので、**元の中身に戻るのを待って**比べる。
        # 消え残っていれば戻らないので、今までどおり落ちる
        listing = lambda: sorted(p.relative_to(self.shared) for p in self.shared.rglob("*"))  # noqa: E731
        try:
            wait_for(lambda: listing() == before, "共有フォルダの中身が元のまま")
        except AssertionError:
            self.assertEqual(listing(), before)


# ===========================================================================
# 3. 同じ PC で起こし方を替える(きのうは exe・きょうは vbs、またはその逆)
# ===========================================================================
class SamePcSwitchTests(_MixedCase):
    """共有が見えないあいだに打って終えた未送信は、**次にどちらで起こしても**送られ、
    「未送信のまま終了」が端末一覧に出る(管理者が「この端末だけ届いていない」を探す所)。"""

    def setUp(self) -> None:
        super().setUp()
        self.pc = Pc(self.base, "X", line="L-1", shared=self.shared)
        self.hidden = self.shared.with_name("共有_見えない")

    def share_down(self, run=None) -> None:
        """共有フォルダが見えなくなる(フォルダの名前を変えてまねる)。

        **裏の同期が終わって共有のファイルを放してから**名前を変えます(`test_flow_multi_terminal` の
        `OfflineTests.down` と同じ)。Windows では、開いているファイルのあるフォルダは名前を変えられず
        「アクセスが拒否されました」になります(Windows の CI で実際に落ちた)。放した直後でも一瞬
        つかまれていることがあるので、しばらく繰り返します。
        """
        if run is not None:
            wait_for(lambda: not run.get("/api/sync").get("busy"), f"{run.edition} の同期が止まる")
        _rename_when_free(self.shared, self.hidden)
        self.addCleanup(lambda: self.hidden.exists() and _rename_when_free(self.hidden, self.shared))

    def share_up(self) -> None:
        _rename_when_free(self.hidden, self.shared)

    def register_offline(self, run, day: _dt.date, code: str) -> None:
        screen = uuid.uuid4().hex
        if isinstance(run, ExeRun):
            status, body = run.post("/api/rest", {"date": key(day), "code": code})
        else:
            run.call("POST", "/api/screen/claim", {"screen_id": screen, "force": True})
            request = urllib.request.Request(
                f"{run.base}/api/rest", method="POST",
                data=json.dumps({"date": key(day), "code": code}).encode("utf-8"),
                headers={"X-Tool-Token": run.token, "Content-Type": "application/json", "X-Screen-Id": screen})
            with urllib.request.urlopen(request, timeout=60) as response:
                status, body = response.status, json.loads(response.read().decode("utf-8"))
        self.assertEqual(status, 200, body)
        wait_for(lambda: (lambda s: s["pending"] == 1 and s["offline"])(run.get("/api/sync")),
                 f"{run.edition} の未送信 1 件")

    def unsent_note(self) -> str:
        rows = [r for r in _web.read_source(self.data_db, "端末設定") if r.get("PC名") == self.pc.pc_name]
        self.assertEqual(len(rows), 1, rows)
        return str(rows[0].get("未送信のまま終了") or "")

    def check_next_start(self, run, day: _dt.date, code: str) -> None:
        wait_for(lambda: any(str(r.get("識別コード")) == code for r in self.shared_rows(day)),
                 f"次に {run.edition} で起こしたときに送られる")
        wait_for(lambda: run.get("/api/sync")["pending"] == 0, f"{run.edition} の未送信 0 件")
        note = wait_for(lambda: self.unsent_note(), "端末一覧の「未送信のまま終了」")
        self.assertTrue(note.startswith("1件"), note)

    def test_exeで打って閉じた未送信を_次の日vbsで起こすと送る(self) -> None:
        day = future_workday(7)
        exe = self.pc.exe(self)
        wait_members(exe)
        self.share_down(exe)
        self.register_offline(exe, day, "102")
        exe.stop()                                   # 窓を閉じた(送れずに 1 件残る)
        self.share_up()
        vbs = self.pc.vbs(self)
        vbs.wait_ready()
        self.check_next_start(vbs, day, "102")

    def test_vbsの終了で閉じた未送信を_次の日exeで起こすと送る(self) -> None:
        day = future_workday(8)
        vbs = self.pc.vbs(self)
        vbs.wait_ready()
        wait_members(vbs)
        self.share_down(vbs)
        self.register_offline(vbs, day, "103")
        # 画面の「終了」(stop.bat も同じ道)
        status, body = vbs.call("POST", "/api/shutdown", {"force": False})
        self.assertEqual(status, 200, body)
        self.assertEqual(vbs.proc.wait(timeout=WAIT), 0)
        self.share_up()
        exe = self.pc.exe(self)
        self.assertEqual(exe.first.get("event"), "started", exe.first)
        self.check_next_start(exe, day, "103")


# ===========================================================================
# 3. ログ: 共有のログフォルダを指しても、PC ごとに別のファイル
# ===========================================================================
class SharedLogTests(_MixedCase):
    def test_共有のログフォルダでも_vbsとexeは別々のPCのフォルダに書く(self) -> None:
        logs = self.base / "ログ置き場"
        logs.mkdir()
        extra = {user_settings.KEY_LOG_DIR: str(logs)}
        pc_v = Pc(self.base, "V", line="L-1", shared=self.shared, settings=extra)
        pc_e = Pc(self.base, "E", line="作業長", shared=self.shared, settings=extra)
        vbs = pc_v.vbs(self)
        exe = pc_e.exe(self)
        vbs.wait_ready()
        self.assertEqual(exe.first.get("event"), "started", exe.first)
        day = future_workday(4)
        wait_members(exe)
        status, body = exe.post("/api/rest", {"date": key(day), "code": "102"})
        self.assertEqual(status, 200, body)
        wait_for(lambda: exe.get("/api/sync")["pending"] == 0, "exe が送り終える")
        vbs.stop()
        exe.stop()
        names = {p.relative_to(logs).parts[0] for p in logs.rglob("*.log")}
        self.assertEqual(names, {pc_v.pc_name, pc_e.pc_name}, "PC ごとのフォルダだけ")
        v_text = "".join(p.read_text(encoding="utf-8") for p in (logs / pc_v.pc_name).glob("*.log"))
        e_text = "".join(p.read_text(encoding="utf-8") for p in (logs / pc_e.pc_name).glob("*.log"))
        self.assertIn(f"PC名={pc_v.pc_name}", v_text)
        self.assertNotIn(f"PC名={pc_e.pc_name}", v_text, "相手の PC の記録が混ざっている")
        self.assertIn(f"PC名={pc_e.pc_name}", e_text)
        self.assertNotIn(f"PC名={pc_v.pc_name}", e_text)
        self.assertIn("デスクトップ版", e_text)


# ===========================================================================
# 3. 配布設定の読み方: 一式の中で、まとめたファイル・ツールのフォルダの設定.json
# ===========================================================================
class DistributionTests(_MixedCase):
    """一式(``<一式>\\config\\tools.json`` と ``<一式>\\tools\\calendar``)をそのまま写して起こす。

    exe も vbs も同じツールのフォルダの同じコードで読む。どちらから起こしても同じ参照先に
    なること(片方だけ・両方・作った時刻の新旧)を、PC を分けて確かめる。
    """

    def setUp(self) -> None:
        super().setUp()
        self.kit = self.base / "日報複合ツール"
        (self.kit / "config").mkdir(parents=True)
        shutil.copy2(REPO / "config" / "tools.json", self.kit / "config" / "tools.json")
        self.tool = self.kit / "tools" / "calendar"
        shutil.copytree(ROOT, self.tool, ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", "tests", "配布設定", "配布設定.作成中"))
        self.own_share = make_shared(self.base / "ツールの設定の共有")
        self.kit_share = make_shared(self.base / "まとめた設定の共有")

    def write_own(self, folder: Path, created_at: str) -> None:
        target = self.tool / "配布設定"
        target.mkdir(exist_ok=True)
        (target / "設定.json").write_text(json.dumps({
            "format": 1, "created_at": created_at, "created_on": "PC-ADMIN", "version": "4.2.14",
            "settings": {user_settings.KEY_DATA_DB_DIR: str(folder),
                         user_settings.KEY_MASTER_DB_DIR: str(folder)}}, ensure_ascii=False), encoding="utf-8")

    def write_kit(self, folder: Path, created_at: str) -> None:
        target = self.kit / "配布設定"
        target.mkdir(exist_ok=True)
        (target / "日報複合ツール.json").write_text(json.dumps({
            "format": 1, "created_at": created_at, "tools": {"calendar": {
                "format": 1, "created_at": created_at, "created_on": "PC-ADMIN", "version": "4.2.14",
                "settings": {user_settings.KEY_DATA_DB_DIR: str(folder),
                             user_settings.KEY_MASTER_DB_DIR: str(folder)}}}},
            ensure_ascii=False), encoding="utf-8")

    def read_by_both(self, tag: str) -> dict[str, Any]:
        """vbs の PC と exe の PC(どちらもまっさら)で起こし、読んだ参照パスを返す。"""
        out: dict[str, Any] = {}
        for edition in ("vbs", "exe"):
            pc = Pc(self.base, f"{tag}{edition}", line="L-1", tool_dir=self.tool)
            run = pc.vbs(self) if edition == "vbs" else pc.exe(self)
            if edition == "exe":
                self.assertEqual(run.first.get("event"), "started", run.first)
            run.wait_ready()
            sync = wait_for(lambda run=run: (lambda s: s if s["configured"] else None)(run.get("/api/sync")),
                            f"{tag} {edition} の参照パス")
            settings = pc.settings()
            out[edition] = settings.get(user_settings.KEY_DATA_DB_DIR, "")
            self.assertEqual(sync["state"], "synced", (edition, sync))
            run.stop()
        return out

    def assert_both(self, got: dict[str, Any], folder: Path) -> None:
        for edition in ("vbs", "exe"):
            with self.subTest(edition=edition):
                self.assertTrue(same_path(got[edition], folder), (edition, got[edition], folder))

    def test_ツールのフォルダの設定json_だけのとき(self) -> None:
        self.write_own(self.own_share, "2026/10/20 08:00:00")
        self.assert_both(self.read_by_both("own"), self.own_share)

    def test_まとめたファイルだけのとき(self) -> None:
        self.write_kit(self.kit_share, "2026-10-20 08:00")
        self.assert_both(self.read_by_both("kit"), self.kit_share)

    def test_両方あるときは作った時刻が新しいほう_どちらの起こし方でも同じ(self) -> None:
        self.write_own(self.own_share, "2026/10/20 08:00:00")
        self.write_kit(self.kit_share, "2026-10-21 08:00")
        self.assert_both(self.read_by_both("new_kit"), self.kit_share)
        self.write_own(self.own_share, "2026/10/22 08:00:00")
        self.assert_both(self.read_by_both("new_own"), self.own_share)


# ===========================================================================
# 3. 版が違う2台(片方だけ古い版)
# ===========================================================================
class VersionMixTests(_MixedCase):
    """古い版の一式を置いたままの PC が混ざる(ツールのフォルダごと写して版だけ変える)。"""

    def test_版の違うvbsとexeでも届き_端末一覧に両方の版が出る(self) -> None:
        old_tool = self.base / "古い一式" / "tools" / "calendar"
        shutil.copytree(ROOT, old_tool, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests"))
        old = Pc(self.base, "OLD", line="L-1", shared=self.shared, tool_dir=old_tool, version="4.2.0")
        new = Pc(self.base, "NEW", line="作業長", shared=self.shared)
        vbs = old.vbs(self)
        exe = new.exe(self)
        self.assertEqual(exe.first.get("event"), "started", exe.first)
        vbs.wait_ready()
        self.assertEqual(vbs.get("/api/health")["version"], "4.2.0")
        self.assertEqual(exe.get("/api/health")["version"], new.version)
        day = future_workday(6)
        wait_members(exe)
        status, body = exe.post("/api/rest", {"date": key(day), "code": "104"})
        self.assertEqual(status, 200, body)
        wait_for(lambda: exe.get("/api/sync")["pending"] == 0, "新しい版が送り終える")
        # 古い版(vbs)の PC は画面を持たないので、名乗ってから「今すぐ同期」
        screen = uuid.uuid4().hex
        vbs.call("POST", "/api/screen/claim", {"screen_id": screen, "force": True})
        request = urllib.request.Request(f"{vbs.base}/api/sync/now", data=b"{}", method="POST", headers={
            "X-Tool-Token": vbs.token, "Content-Type": "application/json", "X-Screen-Id": screen})
        with urllib.request.urlopen(request, timeout=60) as response:
            self.assertEqual(response.status, 200)
        captions = [i["caption"] for i in vbs.get(f"/api/day/{key(day)}")["items"]]
        self.assertTrue(any("伊藤五郎" in c for c in captions), captions)
        rows = _web.read_source(self.data_db, "端末設定")
        versions = {r["PC名"]: r["版"] for r in rows}
        self.assertEqual(versions.get(old.pc_name), "VER4.2.0", rows)
        self.assertEqual(versions.get(new.pc_name), f"VER{new.version}", rows)


if __name__ == "__main__":
    unittest.main()
