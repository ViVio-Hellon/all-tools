"""**exe 起動(日報複合ツールの窓)と vbs 起動(ブラウザ版)が混ざった現場**で、看板の流れを本物で通す

現場では、PC によって看板を日報複合ツールの窓(exe)で開いたり、``Start.vbs``(ブラウザ版)で
開いたりが混ざります。どちらで開いても、同じ共有フォルダの看板マスタを通して台がつながります。
ここでは**別々の PC**を、PC ごとに別の手元の領域で真似て、1 つの共有フォルダ(一時フォルダ)を
見させます。

【何を確かめるか】

1. 倉庫 exe・現場A vbs・現場B exe(とその逆)で、欲しい → 発送 → 注文中 → 届いた確認 → 注文中を外す、
   が**両向きに**相手の画面に出る(押すのも見るのも本物の Chromium の画面)。開いている端末も出る
2. 違うラインを片方 vbs・片方 exe で同時に押しても、ほかのラインの看板を消さない・上書きしない。
   同じ看板を vbs と exe がほぼ同時に押す・古い盤のまま押した 2 台目(VER3.5.12 の守り)も混在で効く。
   同じ PC が vbs で押して届く前に閉じた操作は、次に exe で開いたら送る(逆も同じ所を使う)
3. 多重起動の守り: 同じ PC で exe と vbs を両方起こすと、後のほうが**理由を言って**止まる
   (vbs はコンソールが無いので画面で)。もう一度 vbs を押すと動いている画面を開く。違う PC なら
   両方動き、錠・印は手元にだけ置く(共有フォルダに置かない)。倉庫モードの bat
4. 配布設定: まとめたファイルだけ・看板のフォルダだけ・両方(新しいほう)・単体のフォルダで、
   exe と vbs の接続先が食い違わない(解いたパスで比べ、実際にそこへ書き戻す)
5. ログ・記録: 共有フォルダに集めても、同じラインの exe と vbs が 1 つのファイルに混ぜて書かない
   (名前と頭に PC 名)。記録に「どちらの画面か」が入る。アプリのフォルダに __pycache__ を書かない
6. 版が違う 2 台: ひとつ前の版(git の履歴から取り出す。履歴の無い取り出し方では飛ばす)と混ぜても届く

【PC をどう真似るか】(ツールが PC を見分けるのに使うものを、PC ごとに分ける)

* ``LOCALAPPDATA``(手元の SQLite・ログ・錠・印)・``APPDATA``(この端末の設定ファイル)・
  ``HOME`` / ``USERPROFILE``・``TEMP``
* ``COMPUTERNAME`` / ``USERNAME``(アクセス権限・記録の PC 名)と ``socket.gethostname()``
  (心拍・更新者の名前。子の頭で差し替える)
* ``KANBAN_*`` / ``ALLTOOLS_*`` の差し替えは**渡さない**(本物の起動はどちらも渡さない)。
  例外はポートだけ: 1 台の機械に何台ものブラウザ版を立てるので、PC ごとに空いている番号を
  書いた ``config/app.json`` の写しを ``KANBAN_APP_CONFIG`` で渡す(本物は PC ごとに別の機械なので
  同じ番号でぶつからない)。部品(Flask など)を利用者ごとの場所に入れた機械のために
  ``PYTHONUSERBASE`` も元のまま渡す(HOME・APPDATA を替えると見つからなくなる)

【起動のしかた】アプリは**写した一式**(``<一時>/日報複合ツール/tools/kanban``。本物の配布と同じ形。
リポジトリの ``配布設定`` を読まない・書かない)から起こす。

* **exe 起動** … 外枠(``src-tauri`` の ``bridge.rs`` の ``spawn``)と同じ道: ツールのフォルダを
  作業フォルダにして ``bridge.py`` を起こし、``KANBAN_TOKEN``・``ALLTOOLS_SHELL``・``ALLTOOLS_TOOL``・
  ``ALLTOOLS_ROOT``・``PYTHONIOENCODING=utf-8`` を渡す(``PYTHONUTF8`` は外す)。標準入出力で要求を渡す。
  画面は本物の Chromium で開き、要求を外枠の代わりにそのまま ``bridge.py`` へ渡す(Host を
  ``app.localhost`` に直し、合言葉を付ける。``relay.rs`` と同じ)
* **vbs 起動** … ``Start.vbs`` がするのは ``pythonw "<ツールのフォルダ>\\start_app.py"``(作業フォルダは
  ツールのフォルダ・引数なし・待たない)。**vbs そのものは Linux では動かせない**ので、そこから先を
  同じ形で起こす: 引数なしの ``start_app.py``、標準出力・標準エラーが無い(``pythonw``)。
  ブラウザを開く(``webbrowser.open``)ところだけ、開いた宛先を書き留めるようにして、その宛先を
  本物の Chromium で開く(利用者がブラウザで見るのと同じ)

待つときは時間ではなく**状態**を待つ(画面の升の状態・送信待ちが 0)。取り込みの周期は短く
配る(配布設定)ので、↻ を押さなくても相手の画面に出るのを待てる。
"""
from __future__ import annotations

import json
import os
import shutil
import site
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

from kanban import config, keys
from kanban.db.store import Store
from tests.test_flow_multi_terminal import (HVC, KANBAN, L1, TABLE, _again, link_dir, make_shared,
                                            unlink_dir)

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
sys.path.insert(0, str(ROOT))
import bridge as bridge_proto  # noqa: E402  (やりとりの形を読むだけ)

ON = config.MARK_ON
ADMIN_PASSWORD = "mix-admin"
#: 子が立ち上がるのを待つ上限(秒)。Windows の CI は遅い
BOOT_SEC = 120.0
#: 状態を待つ上限(秒)
WAIT_SEC = 60.0
#: 画面の状態を待つ上限(ミリ秒。取り込みの周期 + 画面の読み直しの周期より十分長く)
SCREEN_MS = 60_000

#: 子の頭。台ごとの PC 名(``socket.gethostname``)・pythonw(標準出力・標準エラーが無い)・
#: ブラウザを開いた宛先の書き留め。そのあと本物の入口(``bridge.py`` / ``start_app.py``)を
#: ``__main__`` として流す。**tests パッケージは読まない**
_WRAP = r"""
import os, runpy, socket, sys, traceback
_pc = os.environ["MIX_PC"]
socket.gethostname = lambda: _pc
_opened = os.environ.get("MIX_BROWSER_FILE", "")
if _opened:
    import webbrowser
    def _open(url, new=0, autoraise=True):
        with open(_opened, "a", encoding="utf-8") as fh:
            fh.write(url + "\n")
        return True
    webbrowser.open = webbrowser.open_new = webbrowser.open_new_tab = _open
if os.environ.get("MIX_PYTHONW") == "1":
    sys.stdout = sys.stderr = None        # pythonw.exe: コンソールが無い
script = sys.argv[1]
sys.argv = [script] + sys.argv[2:]
try:
    runpy.run_path(script, run_name="__main__")
except SystemExit:
    raise
except BaseException:
    os.write(2, traceback.format_exc().encode("utf-8", "replace"))
    raise SystemExit(99)
"""

#: 写さないもの(試験・キャッシュ・配布設定・この端末のもの)
_SKIP = shutil.ignore_patterns("tests", "__pycache__", "*.pyc", "配布設定", "配布設定.作成中", "data",
                               ".pytest_cache", "*.broken-*")


# ---------------------------------------------------------------------------
# 一式・共有フォルダ
# ---------------------------------------------------------------------------
def copy_tool(dest: Path) -> Path:
    """看板のフォルダを写す(配った先と同じ中身)。"""
    shutil.copytree(ROOT, dest, ignore=_SKIP)
    return dest


def make_bundle(base: Path) -> Path:
    """日報複合ツールの一式(``<一式>\\config\\tools.json`` + ``<一式>\\tools\\kanban``)。看板のフォルダを返す。"""
    whole = base / "日報複合ツール"
    (whole / "config").mkdir(parents=True)
    catalog = REPO / "config" / "tools.json"
    if catalog.is_file():
        shutil.copy2(catalog, whole / "config" / "tools.json")
    else:                                          # 看板だけのリポジトリでも試せるように
        (whole / "config" / "tools.json").write_text(
            json.dumps({"tools": [{"id": "kanban", "dir": "tools/kanban", "env_prefix": "KANBAN"}]}),
            encoding="utf-8")
    return copy_tool(whole / "tools" / "kanban")


def bundle_root(app: Path) -> Path:
    """看板のフォルダ → 一式の直下(``ALLTOOLS_ROOT``)。"""
    return app.parent.parent


def write_tool_distribution(app: Path, settings: dict, created_at: str) -> Path:
    """看板のフォルダの ``配布設定\\設定.json``(看板の設定画面の「配布設定」が書くもの)。"""
    folder = app / "配布設定"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "設定.json"
    path.write_text(json.dumps({"format": 1, "created_at": created_at, "created_on": "管理者のPC",
                                "settings": settings}, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_integrated_distribution(app: Path, settings: dict, created_at: str) -> Path:
    """一式の直下の ``配布設定\\日報複合ツール.json``(大設定の「まとめて書き出す」が書くもの)。"""
    folder = bundle_root(app) / "配布設定"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "日報複合ツール.json"
    path.write_text(json.dumps({
        "format": 1, "created_at": created_at, "created_on": "管理者のPC", "settings": {},
        "tools": {"kanban": {"format": 1, "created_at": created_at, "settings": settings}},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


#: 1 つのモードの候補ポートの数(``port_retry`` + 1)。閉じた直後の番号は塞がって見えることが
#: あるので(TIME_WAIT)、本物と同じく隣へずれられるようにしておく
PORT_RETRY = 3
_taken: set[int] = set()


def free_ports(count: int) -> int:
    """続けて ``count`` 個空いている番号の頭(この試験の中でほかに配っていないもの)。"""
    for _ in range(200):
        base = free_port()
        wanted = range(base, base + count)
        if base + count > 65535 or _taken.intersection(wanted):
            continue
        free = True
        for port in wanted:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                try:
                    sock.bind(("127.0.0.1", port))
                except OSError:
                    free = False
                    break
        if free:
            _taken.update(wanted)
            return base
    raise RuntimeError("続けて空いているポートが見つかりません")


def same_path(a: Any, b: Any) -> bool:
    """パスを**解いた値**で比べる(書き方・区切り・大文字小文字・つなぎ口に左右されない)。"""
    return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(os.path.realpath(str(b)))


def _remove_tree(path: Path) -> None:
    for _ in range(20):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.5)           # Windows: 止めたばかりのプロセスがまだ掴んでいる
    shutil.rmtree(path, ignore_errors=True)


_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def wait_until(check: Callable[[], Any], what: str, timeout: float = WAIT_SEC,
               alive: Callable[[], Optional[str]] = lambda: None) -> Any:
    """``check()`` が真を返すまで待つ(**時間ではなく状態を待つ**)。``alive`` が理由を返したら打ち切る。"""
    deadline = time.monotonic() + timeout
    while True:
        try:
            got = check()
        except (OSError, ValueError, KeyError):
            got = None
        if got:
            return got
        dead = alive()
        if dead:
            raise AssertionError(f"{what} を待つあいだに止まりました:\n{dead}")
        if time.monotonic() > deadline:
            raise AssertionError(f"{what} まで待ちましたが、そうなりません")
        time.sleep(0.2)


# ---------------------------------------------------------------------------
# PC
# ---------------------------------------------------------------------------
class PC:
    """PC 1 台(手元の領域・設定ファイル・PC 名・ログインID)。看板を exe でも vbs でも起こせる。"""

    def __init__(self, case: "MixedCase", name: str, *, login: str, app: Path) -> None:
        self.case = case
        self.name = name
        self.login = login
        self.app = app
        self.dir = case.base / "PC" / name
        self.local_appdata = self.dir / "AppData" / "Local"
        self.roaming = self.dir / "AppData" / "Roaming"
        self.home = self.dir / "home"
        self.temp = self.dir / "Temp"
        for folder in (self.local_appdata, self.roaming, self.home, self.temp):
            folder.mkdir(parents=True, exist_ok=True)
        self.ports = {mode: free_ports(PORT_RETRY + 1) for mode in config.ALL_MODES}
        self.app_json = self.dir / "app.json"
        self.install(app)

    def install(self, app: Path) -> None:
        """この PC で使う一式を替える(新しい版を入れた、など)。

        ポートだけは PC ごとに空いている番号(冒頭の説明)。ほかはその一式の app.json のまま(版も)。
        """
        self.app = app
        conf = json.loads((app / "config" / "app.json").read_text(encoding="utf-8"))
        conf["server"]["port_retry"] = PORT_RETRY
        for mode in config.ALL_MODES:
            conf["server"]["roles"][mode] = {"port": self.ports[mode]}
        self.app_json.write_text(json.dumps(conf, ensure_ascii=False), encoding="utf-8")

    # -- 共有フォルダへのつなぎ口(この PC だけ見えなくする) ---------------------
    def use_link(self) -> Path:
        """この PC は共有フォルダを**この PC のつなぎ口**から見る(この端末の設定で接続先を決める)。

        つなぎ口を外すと、この PC だけ共有フォルダが見えなくなる(ネットワークが切れた端末)。
        時間に頼らずに「お互いの操作を取り込む前に押した」行き違いを作れる。
        """
        self.link = self.dir / "share"
        link_dir(self.case.shared, self.link)
        self.case.addCleanup(lambda: os.path.lexists(str(self.link)) and _again(lambda: unlink_dir(self.link)))
        values = self.settings()
        values.pop(config.LOCAL_FORMAT_KEY, None)
        self.write_settings(**{**values, "shared_db_path": str(self.link / config.TARGET_SHARED_DB_NAME)})
        return self.link

    def go_offline(self) -> None:
        _again(lambda: unlink_dir(self.link))

    def go_online(self) -> None:
        _again(lambda: link_dir(self.case.shared, self.link))

    # -- この PC の場所(アプリが既定で使う所) -------------------------------
    @property
    def local_root(self) -> Path:
        return self.local_appdata / config.APP_NAME

    @property
    def settings_file(self) -> Path:
        return self.roaming / config.APP_NAME / "config.json"

    @property
    def sqlite_file(self) -> Path:
        return self.local_root / "data" / "kanban.sqlite3"

    def env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items()
               if not k.upper().startswith(("KANBAN_", "ALLTOOLS_", "XDG_", "MIX_", "PYTHONPYCACHEPREFIX"))}
        env.update({
            "LOCALAPPDATA": str(self.local_appdata), "APPDATA": str(self.roaming),
            "HOME": str(self.home), "USERPROFILE": str(self.home),
            "TEMP": str(self.temp), "TMP": str(self.temp), "TMPDIR": str(self.temp),
            "COMPUTERNAME": self.name, "USERNAME": self.login, "MIX_PC": self.name,
            "KANBAN_APP_CONFIG": str(self.app_json),
            # 部品(Flask など)を利用者ごとの場所(%APPDATA%\Python・~/.local)に入れた機械でも
            # 見つかるように。HOME・APPDATA を PC ごとに替えると、その場所も替わってしまう
            "PYTHONUSERBASE": os.environ.get("PYTHONUSERBASE") or site.getuserbase(),
        })
        env.pop("PYTHONUTF8", None)
        env.pop("PYTHONDONTWRITEBYTECODE", None)        # どちらの起動も付けない
        return env

    # -- この PC で決めること -------------------------------------------------
    def set_mode(self, mode: str) -> None:
        """この端末のモード(手元の SQLite に覚える。設定 →「この端末」と同じ置き場所)。"""
        self.sqlite_file.parent.mkdir(parents=True, exist_ok=True)
        store = Store(str(self.sqlite_file), host_name=self.name)
        try:
            store.ensure_schema()
            store.set_device_mode(mode)
        finally:
            store.close()

    def write_settings(self, **values: Any) -> None:
        """この端末の設定ファイル(``%APPDATA%\\KanbanSystem\\config.json``)。"""
        self.settings_file.parent.mkdir(parents=True, exist_ok=True)
        self.settings_file.write_text(json.dumps(
            {config.LOCAL_FORMAT_KEY: config.LOCAL_FORMAT, **values}, ensure_ascii=False), encoding="utf-8")

    def settings(self) -> dict:
        try:
            return json.loads(self.settings_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    # -- 起こす --------------------------------------------------------------
    def exe(self, *, wait_ready: bool = True) -> "ExeTerminal":
        term = ExeTerminal(self)
        self.case.addCleanup(term.stop)
        if wait_ready:
            term.wait_ready()
        return term

    def vbs(self, *args: str, wait_ready: bool = True, script: str = "start_app.py") -> "VbsTerminal":
        term = VbsTerminal(self, *args, script=script)
        self.case.addCleanup(term.stop)
        if wait_ready:
            term.wait_ready()
        return term

    def logs(self) -> list[Path]:
        return sorted((self.local_root / "logs").rglob("kanban_DebugLog_*.txt"))

    def log_text(self) -> str:
        return "\n".join(p.read_text("utf-8", "replace")[-3000:] for p in self.logs()[-2:])


# ---------------------------------------------------------------------------
# 端末(起こした看板 1 つ)
# ---------------------------------------------------------------------------
class Terminal:
    """起こした看板 1 つ。操作は画面と同じ API(``/api/...``)か、本物の Chromium の画面で。"""

    kind = ""
    proc: subprocess.Popen

    def __init__(self, pc: PC) -> None:
        self.pc = pc
        self.case = pc.case
        self.name = f"{pc.name}({self.kind})"
        self.mode = ""

    # -- 共通 ----------------------------------------------------------------
    def call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        raise NotImplementedError

    def why_dead(self) -> Optional[str]:
        if self.proc.poll() is None:
            return None
        return f"{self.name} が終わりました(終了コード {self.proc.returncode})\n{self.tail()}"

    def tail(self) -> str:
        return self.pc.log_text()[-4000:]

    def wait(self, check: Callable[[], Any], what: str, timeout: float = WAIT_SEC) -> Any:
        return wait_until(check, f"{self.name}: {what}", timeout, alive=self.why_dead)

    def health(self) -> dict:
        status, body = self.call("GET", "/api/health")
        return body if status == 200 and isinstance(body, dict) else {}

    def wait_ready(self) -> dict:
        health = self.wait(lambda: (lambda h: h if h.get("ready") else None)(self.health()),
                           "準備が済む", timeout=BOOT_SEC)
        self.case.assertEqual(health.get("startup_error", ""), "", self.tail())
        self.mode = health["mode"]
        return health

    def get(self, path: str) -> Any:
        status, body = self.call("GET", path)
        self.case.assertEqual(status, 200, f"{self.name} GET {path}: {body}")
        return body

    def post(self, path: str, body: Any = None) -> tuple[int, Any]:
        return self.call("POST", path, body or {})

    def status(self) -> dict:
        return self.get("/api/status")

    def wait_sent(self) -> None:
        self.wait(lambda: (lambda s: s["pending"] == 0 and s["undelivered"] == 0)(self.status()),
                  "送信待ちが 0 になる")

    def refresh(self, line: str = "") -> dict:
        status, body = self.post(f"/api/refresh?line={line}")
        self.case.assertEqual(status, 200, f"{self.name} 更新: {body}")
        return body

    def rows(self, line: str) -> dict[str, dict]:
        board = self.get(f"/api/board?line={line}")["board"]
        return {r["mgmt_no"]: r for g in board["groups"] for r in g["rows"]}

    def marks(self, line: str, no: str) -> tuple[str, str, str]:
        r = self.rows(line)[no]
        return (ON if r["size"]["state"] == "ordered" else "",
                ON if r["ship"]["state"] == "shipped" else "",
                ON if r["hold"]["state"] == "held" else "")

    def press(self, kind: str, line: str, no: str, *, rev: Optional[int] = None, **extra) -> tuple[int, Any]:
        """盤のボタン(``order`` / ``ship`` / ``hold`` / ``acknowledge``)。``rev`` は画面が見ていた版。"""
        if rev is None:
            rev = self.rows(line)[no]["rev"]
        return self.post(f"/api/board/{kind}", {"line": line, "mgmt_no": no, "rev": rev, **extra})

    def ok(self, kind: str, line: str, no: str, **extra) -> dict:
        status, body = self.press(kind, line, no, **extra)
        self.case.assertEqual(status, 200, f"{self.name} {kind} {line} {no}: {body}")
        return body

    def presence(self) -> dict[str, str]:
        """この端末から見た「開いている端末」``{ライン: PC名}``。"""
        return {p["line"]: p.get("host", "") for p in self.status()["presence"] if p.get("live")}

    def open_page(self, ctx):
        """本物の Chromium で画面を開く(盤が出るまで待つ)。"""
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError


class ExeTerminal(Terminal):
    """exe 起動。外枠(``bridge.rs``)の代わりに ``bridge.py`` を起こし、標準入出力で要求を渡す。"""

    kind = "exe"

    def __init__(self, pc: PC) -> None:
        super().__init__(pc)
        self.token = f"shell-token-{pc.name}"
        self.events: list[dict] = []
        self.replies: dict[int, tuple[dict, bytes]] = {}
        self.err: list[str] = []
        self.cond = threading.Condition()
        self.next_id = 1
        self.write_lock = threading.Lock()
        env = pc.env()
        env.update({
            "KANBAN_TOKEN": self.token,
            "ALLTOOLS_SHELL": "1", "ALLTOOLS_TOOL": "kanban",
            "ALLTOOLS_ROOT": str(bundle_root(pc.app)),
            "PYTHONIOENCODING": "utf-8",
        })
        self.proc = subprocess.Popen(
            [sys.executable, "-c", _WRAP, str(pc.app / "bridge.py")], cwd=str(pc.app), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        threading.Thread(target=self._drain, daemon=True).start()
        threading.Thread(target=self._read, daemon=True).start()

    def _drain(self) -> None:
        for line in self.proc.stderr:              # type: ignore[union-attr]
            self.err.append(line.decode("utf-8", "replace").rstrip())
            del self.err[:-300]

    def _read(self) -> None:
        while True:
            try:
                frame = bridge_proto.read_frame(self.proc.stdout)
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

    def tail(self) -> str:
        return "\n".join(self.err[-40:]) + "\n" + super().tail()

    def first_event(self) -> dict:
        """1 行目(started / fatal / 何も言わずに終わった ``<eof>``)。"""
        deadline = time.monotonic() + BOOT_SEC
        with self.cond:
            while not self.events and time.monotonic() < deadline:
                self.cond.wait(0.2)
        if not self.events:
            raise AssertionError(f"{self.name}: 1 行目が来ません\n{self.tail()}")
        return self.events[0]

    def wait_ready(self) -> dict:
        first = self.first_event()
        self.case.assertEqual(first.get("event"), "started", f"{self.name}: {first}\n{self.tail()}")
        return super().wait_ready()

    def raw(self, method: str, path: str, *, query: str = "", headers=(), body: bytes = b"",
            timeout: float = 90) -> tuple[int, list, bytes]:
        """外枠と同じに渡す: 合言葉を付け、Host を ``app.localhost`` にする(``bridge.rs`` の ``call_with``)。"""
        if self.proc.poll() is not None:
            raise OSError(f"{self.name} は終わっています")
        with self.cond:
            rid = self.next_id
            self.next_id += 1
        hdrs = [[k, v] for k, v in headers if k.lower() not in ("host", "x-tool-token")]
        hdrs += [["Host", "app.localhost"], ["X-Tool-Token", self.token]]
        head = {"id": rid, "method": method, "path": path, "query": query, "headers": hdrs, "len": len(body)}
        with self.write_lock:
            self.proc.stdin.write(json.dumps(head, ensure_ascii=False).encode("utf-8") + b"\n" + body)
            self.proc.stdin.flush()
        deadline = time.monotonic() + timeout
        with self.cond:
            while rid not in self.replies:
                if time.monotonic() > deadline or any(e["event"] == "<eof>" for e in self.events):
                    raise OSError(f"{self.name}: {method} {path} に答えません\n{self.tail()}")
                self.cond.wait(0.2)
            reply, data = self.replies.pop(rid)
        return reply["status"], reply["headers"], data

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        path, _, query = path.partition("?")
        data = b"" if body is None else json.dumps(body).encode("utf-8")
        status, _headers, raw = self.raw(method, path, query=query, body=data, headers=[
            ("Content-Type", "application/json"), ("Accept", "application/json")])
        try:
            return status, json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            return status, raw.decode("utf-8", "replace")

    def open_page(self, ctx):
        """窓(WebView)の代わりの Chromium。要求は外枠と同じに ``bridge.py`` へ渡す(``relay.rs``)。"""
        origin = f"http://kanban-{self.pc.name.lower()}.localhost"

        def relay(route) -> None:
            req = route.request
            parts = urllib.parse.urlsplit(req.url)
            try:
                status, headers, data = self.raw(
                    req.method, parts.path or "/", query=parts.query,
                    headers=[(k, v) for k, v in req.headers.items()], body=req.post_data_buffer or b"")
            except OSError:
                route.abort()
                return
            out = {k: v for k, v in headers if k.lower() not in (
                "content-length", "transfer-encoding", "x-frame-options")}
            if req.is_navigation_request() and 300 <= status < 400:
                # 画面の移り先(302)は、外枠と同じに「移り先へ自分で移るページ」に替える
                # (`relay.rs` の moving_to。WebView は独自の宛先では移り先をたどらない)
                target = next((v for k, v in headers if k.lower() == "location"), "/")
                target = target.removeprefix("http://" + bridge_proto.DEFAULT_HOST) or "/"
                status, out = 200, {"content-type": "text/html; charset=utf-8"}
                data = f"<script>location.replace({json.dumps(target)})</script>".encode("utf-8")
            route.fulfill(status=status, headers=out, body=data)

        ctx.route(lambda url: url.startswith(origin + "/"), relay)
        page = ctx.new_page()
        page.goto(f"{origin}/", wait_until="load")
        page.wait_for_selector("#board .row", timeout=SCREEN_MS)
        return page

    def stop(self) -> None:
        """窓を閉じる(外枠が標準入力を閉じる。最後の書き戻しを済ませて終わる)。"""
        if self.proc.poll() is None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            try:
                self.proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=15)
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass


class VbsTerminal(Terminal):
    """vbs 起動(``Start.vbs`` → ``pythonw start_app.py``)。ブラウザ版はポートで待ち受ける。"""

    kind = "vbs"

    def __init__(self, pc: PC, *args: str, script: str = "start_app.py") -> None:
        super().__init__(pc)
        self.opened = pc.dir / f"ブラウザで開いた_{time.monotonic_ns()}.txt"
        self.err_file = pc.dir / f"stderr_{time.monotonic_ns()}.txt"
        env = pc.env()
        env.update({"MIX_BROWSER_FILE": str(self.opened), "MIX_PYTHONW": "1"})
        env.pop("PYTHONIOENCODING", None)           # Start.vbs は何も足さない
        self._err = open(self.err_file, "wb")
        self.proc = subprocess.Popen(
            [sys.executable, "-c", _WRAP, str(pc.app / script), *args], cwd=str(pc.app), env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=self._err)
        self.url = ""
        self.token = ""
        self.port = 0

    def tail(self) -> str:
        try:
            err = self.err_file.read_text("utf-8", "replace")[-2000:]
        except OSError:
            err = ""
        return err + "\n" + super().tail()

    def opened_urls(self) -> list[str]:
        try:
            return [u for u in self.opened.read_text(encoding="utf-8").splitlines() if u.strip()]
        except OSError:
            return []

    def wait_ready(self) -> dict:
        # ブラウザが開かれた(= 待ち受けを確かめた)のを待つ。宛先にポートと合言葉が入っている
        self.url = self.wait(lambda: (self.opened_urls() or [""])[0], "ブラウザを開く", timeout=BOOT_SEC)
        parts = urllib.parse.urlsplit(self.url)
        self.port = parts.port or 0
        self.token = urllib.parse.parse_qs(parts.query).get("t", [""])[0]
        return super().wait_ready()

    def call(self, method: str, path: str, body: Any = None, *, timeout: float = 90) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"X-Tool-Token": self.token, "Content-Type": "application/json"})
        try:
            with _NO_PROXY.open(request, timeout=timeout) as response:
                raw, status = response.read(), response.status
        except urllib.error.HTTPError as exc:
            raw, status = exc.read(), exc.code
        try:
            return status, json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            return status, raw.decode("utf-8", "replace")

    def open_page(self, ctx):
        """利用者が見るのと同じ: アプリが開いた宛先(``?t=`` 付き)をブラウザで開く。"""
        page = ctx.new_page()
        page.goto(self.url, wait_until="load")
        page.wait_for_selector("#board .row", timeout=SCREEN_MS)
        return page

    def stop(self) -> None:
        """画面の「終了」と同じ(最後の書き戻しをしてから終わる)。"""
        if self.proc.poll() is None and self.port:
            try:
                self.call("POST", "/api/shutdown", {"force": True}, timeout=10)
            except OSError:
                pass
        if self.proc.poll() is None:
            try:
                self.proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=15)
        self._err.close()


# ---------------------------------------------------------------------------
# 試験の土台
# ---------------------------------------------------------------------------
class MixedCase(unittest.TestCase):
    """共有フォルダ 1 つ・写した一式 1 つと、そこを使う PC たち。"""

    #: 配る取り込み間隔(秒)。相手の操作が ↻ なしで届くのを待つ
    IMPORT_SEC = 2

    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="kanban_mixed_"))
        self.addCleanup(_remove_tree, self.base)
        self.shared = make_shared(self.base / "共有")
        self.shared_db = self.shared / config.TARGET_SHARED_DB_NAME
        self.app = make_bundle(self.base / "配った一式")
        self.admin_hash = config.hash_password(ADMIN_PASSWORD)

    def distribute(self, **extra: Any) -> None:
        """管理者が配る設定(大設定の「まとめて書き出す」= 一式の直下の 日報複合ツール.json)。"""
        settings = {"shared_db_path": str(self.shared_db), "import_interval_sec": self.IMPORT_SEC,
                    "export_interval_sec": 30, "admin_password_hash": self.admin_hash, **extra}
        write_integrated_distribution(self.app, settings, "2026/10/26 08:00:00")

    def pc(self, name: str, *, mode: str, line: str = "", login: str = "") -> PC:
        machine = PC(self, name, login=login or f"user-{name.lower()}", app=self.app)
        machine.set_mode(mode)
        if line:
            machine.write_settings(line=line)
        return machine

    # -- 共有DB --------------------------------------------------------------
    def shared_rows(self, line: str) -> list[dict]:
        conn = sqlite3.connect(f"file:{self.shared_db.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(f"SELECT rowid AS _rowid, * FROM [{TABLE[line]}] ORDER BY rowid")]
        finally:
            conn.close()

    def shared_marks(self, line: str, no: Any) -> Optional[tuple[str, str, str]]:
        found = [r for r in self.shared_rows(line) if keys.same(r[config.COL_KEY], no)]
        self.assertLessEqual(len(found), 1, f"共有DBで {line} {no} 番が重なっています: {found}")
        if not found:
            return None
        norm = lambda v: "" if v is None else str(v).strip()      # noqa: E731
        row = found[0]
        return norm(row[config.COL_WANT]), norm(row[config.COL_SHIPPED]), norm(row[config.COL_HOLD])

    def state_rows(self) -> list[dict]:
        conn = sqlite3.connect(f"file:{self.shared_db.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(f"SELECT * FROM [{config.TABLE_STATE}]")]
        finally:
            conn.close()



class ScreenCase(MixedCase):
    """本物の Chromium の画面を使う試験。無ければ飛ばす(``ALLTOOLS_REQUIRE_BROWSER=1`` なら失敗)。"""

    @classmethod
    def setUpClass(cls) -> None:
        from tests._browser import chromium_or_skip

        super().setUpClass()
        cls.pw, cls.browser = chromium_or_skip()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.pw.stop()
        super().tearDownClass()

    def screen(self, term: Terminal):
        """その端末の画面を 1 枚開く(PC ごとに別のブラウザ = 別の文脈)。確かめの窓は「はい」で閉じる。"""
        ctx = self.browser.new_context(viewport={"width": 1400, "height": 900})
        self.addCleanup(ctx.close)
        ctx.set_default_timeout(SCREEN_MS)
        errors: list[str] = []
        page = term.open_page(ctx)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("dialog", lambda d: (self.dialogs.append(d.message), d.accept()))
        self.addCleanup(lambda: self.assertEqual(errors, [], f"{term.name} の画面の誤り"))
        return page

    def setUp(self) -> None:
        super().setUp()
        self.dialogs: list[str] = []

    @staticmethod
    def cell(page, no: str, kind: str):
        return page.locator(f'#board .row[data-no="{no}"] .cell[data-kind="{kind}"]')

    def wait_cell(self, page, no: str, kind: str, want: str, who: str) -> None:
        """画面の升がその状態になるまで待つ(取り込み → 画面の読み直しが済むまで)。"""
        try:
            page.wait_for_function(
                "([no, kind, want]) => { const c = document.querySelector("
                "`#board .row[data-no=\"${no}\"] .cell[data-kind=\"${kind}\"]`);"
                " return c && c.dataset.state === want; }", arg=[no, kind, want], timeout=SCREEN_MS)
        except Exception as exc:                   # noqa: BLE001 - いまの状態を添えて落とす
            now = self.cell(page, no, kind).get_attribute("data-state")
            raise AssertionError(f"{who} の画面で {no} 番の {kind} が {want} になりません(いま {now})") from exc

    @staticmethod
    def show_line(page, line: str) -> None:
        """倉庫の画面でラインのタブを選ぶ。"""
        page.click(f'#line-tabs .tab[data-line="{line}"]')
        page.wait_for_function(
            "(line) => { const b = document.getElementById('board');"
            " return b && b.dataset.line === line && b.getAttribute('aria-busy') !== 'true'; }", arg=line)


class RoundTripTests(ScreenCase):
    """欲しい → 発送 → 注文中 → 届いた確認 が、exe と vbs をまたいで**両向きに**届く。

    どちらで押した操作も、もう一方の**画面**に出る(押した側は本物の画面のボタンで押す)。
    """

    def test_倉庫exe_現場Aはvbs_現場Bはexe(self) -> None:
        self.distribute()
        warehouse = self.pc("SOUKO-PC", mode=config.MODE_WAREHOUSE, login="userw").exe()
        line_a = self.pc("LINE-A-PC", mode=config.MODE_SITE, line=L1).vbs()
        line_b = self.pc("LINE-B-PC", mode=config.MODE_SITE, line=HVC).exe()
        self.assertEqual((warehouse.mode, line_a.mode, line_b.mode),
                         (config.MODE_WAREHOUSE, config.MODE_SITE, config.MODE_SITE))
        w_page, a_page, b_page = self.screen(warehouse), self.screen(line_a), self.screen(line_b)
        self.round_trip(w_page, "倉庫(exe)", {L1: (a_page, "現場A(vbs)"), HVC: (b_page, "現場B(exe)")})
        for term in (warehouse, line_a, line_b):
            term.wait_sent()
        self.assertEqual(self.shared_marks(L1, "1"), ("", "", ""))
        self.assertEqual(self.shared_marks(HVC, "1"), ("", "", ""))
        # 開いている端末: 倉庫からは両方の現場が、現場からは倉庫が見える(どちらの起動でも名乗る)
        warehouse.wait(lambda: warehouse.presence() == {L1: "LINE-A-PC", HVC: "LINE-B-PC"} or None,
                       "倉庫に、開いている現場 2 台が出る")
        line_a.wait(lambda: line_a.presence().get(config.WAREHOUSE_LINE_NAME) == "SOUKO-PC", "現場Aに倉庫が出る")
        line_b.wait(lambda: line_b.presence().get(config.WAREHOUSE_LINE_NAME) == "SOUKO-PC", "現場Bに倉庫が出る")

    def test_倉庫vbs_現場Aはexe_現場Bはvbs(self) -> None:
        self.distribute()
        warehouse = self.pc("SOUKO-PC", mode=config.MODE_WAREHOUSE, login="userw").vbs()
        line_a = self.pc("LINE-A-PC", mode=config.MODE_SITE, line=L1).exe()
        line_b = self.pc("LINE-B-PC", mode=config.MODE_SITE, line=HVC).vbs()
        w_page, a_page, b_page = self.screen(warehouse), self.screen(line_a), self.screen(line_b)
        self.round_trip(w_page, "倉庫(vbs)", {L1: (a_page, "現場A(exe)"), HVC: (b_page, "現場B(vbs)")})
        warehouse.wait(lambda: warehouse.presence() == {L1: "LINE-A-PC", HVC: "LINE-B-PC"} or None,
                       "倉庫に、開いている現場 2 台が出る")

    def round_trip(self, w_page, w_name: str, sites: dict) -> None:
        for line, (page, name) in sites.items():
            with self.subTest(line=line, 現場=name, 倉庫=w_name):
                self.show_line(w_page, line)
                # 1. 現場が欲しいを出す → 倉庫の画面に赤
                self.cell(page, "1", "size").click()
                self.wait_cell(page, "1", "size", "ordered", name)
                self.wait_cell(w_page, "1", "size", "ordered", w_name)
                # 2. 倉庫が発送 → 現場の画面に緑
                self.cell(w_page, "1", "ship").click()
                self.wait_cell(w_page, "1", "ship", "shipped", w_name)
                self.wait_cell(page, "1", "ship", "shipped", name)
                # 3. もう 1 枚欲しい → 倉庫が注文中 → 現場の画面に黄(押せなくなる)
                self.cell(page, "2", "size").click()
                self.wait_cell(w_page, "2", "size", "ordered", w_name)
                self.cell(w_page, "2", "hold").click()
                self.wait_cell(w_page, "2", "hold", "held", w_name)
                self.wait_cell(page, "2", "hold", "held", name)
                self.assertTrue(self.cell(page, "2", "size").is_disabled(), f"{name}: 注文中なのに押せる")
                # 4. 現場が届いた確認 → 倉庫の画面で赤も緑も消える
                self.dialogs.clear()
                self.cell(page, "1", "size").click()
                self.wait_cell(page, "1", "size", "neutral", name)
                self.assertTrue(any("届いていますか" in d for d in self.dialogs), self.dialogs)
                self.wait_cell(w_page, "1", "size", "neutral", w_name)
                self.wait_cell(w_page, "1", "ship", "neutral", w_name)
                # 5. 倉庫が注文中を外す → 現場の画面で押せるようになる
                self.cell(w_page, "2", "hold").click()
                self.wait_cell(w_page, "2", "hold", "neutral", w_name)
                self.wait_cell(page, "2", "hold", "neutral", name)
                page.wait_for_function(
                    "() => !document.querySelector('#board .row[data-no=\"2\"] .cell[data-kind=\"size\"]').disabled")
                # 片付け: 2 番の欲しいを取り消す(次のラインの確かめに持ち越さない)
                self.cell(page, "2", "size").click()
                self.wait_cell(w_page, "2", "size", "neutral", w_name)



# ---------------------------------------------------------------------------
# 2. 違うライン・同じ看板を同時に(exe と vbs が混ざっても守りが効く)
# ---------------------------------------------------------------------------
class OtherLineTests(MixedCase):
    """片方 vbs・片方 exe でも、ほかのラインの看板を消したり上書きしたりしない。同時の操作も守る。"""

    def snapshot(self, line: str) -> list[tuple]:
        """共有DBのその表の中身(行の順・全部の列)。"""
        return [tuple(sorted(r.items())) for r in self.shared_rows(line)]

    def test_違うラインを同時に押しても_ほかのラインを消さない(self) -> None:
        self.distribute()
        warehouse = self.pc("SOUKO-PC", mode=config.MODE_WAREHOUSE, login="userw").exe()
        line_a = self.pc("LINE-A-PC", mode=config.MODE_SITE, line=L1).vbs()
        line_b = self.pc("LINE-B-PC", mode=config.MODE_SITE, line=HVC).exe()
        hvc_before = self.snapshot(HVC)
        l1_count, hvc_count = len(self.shared_rows(L1)), len(hvc_before)

        # 2 台が**同時に**それぞれのラインの看板を全部出す(vbs は HTTP、exe は標準入出力)
        jobs = {line_a: (L1, [r for r in line_a.rows(L1)]), line_b: (HVC, [r for r in line_b.rows(HVC)])}
        failures: list[str] = []
        start = threading.Barrier(len(jobs))

        def press_all(term: Terminal, line: str, nos: list[str]) -> None:
            start.wait(timeout=30)
            for no in nos:
                status, body = term.press("order", line, no)
                if status != 200:
                    failures.append(f"{term.name} {line} {no}: {status} {body}")

        threads = [threading.Thread(target=press_all, args=(t, line, nos)) for t, (line, nos) in jobs.items()]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=WAIT_SEC)
        self.assertEqual(failures, [])
        for term in (line_a, line_b):
            term.wait_sent()
        self.assertEqual({no: self.shared_marks(L1, no) for no in line_a.rows(L1)},
                         {no: (ON, "", "") for no in line_a.rows(L1)})
        self.assertEqual({no: self.shared_marks(HVC, no) for no in line_b.rows(HVC)},
                         {no: (ON, "", "") for no in line_b.rows(HVC)})
        self.assertEqual((len(self.shared_rows(L1)), len(self.shared_rows(HVC))), (l1_count, hvc_count),
                         "看板の行が増えた・消えた")

        # 倉庫がそれぞれ発送 → 現場はそれぞれ自分のラインだけ届いた確認(同時に)
        warehouse.wait(lambda: all(warehouse.marks(L1, no)[0] for no in ("1", "2")) and
                       all(warehouse.marks(HVC, no)[0] for no in ("1", "2")), "倉庫に両方のラインの赤が出る")
        for line in (L1, HVC):
            for no in ("1", "2"):
                warehouse.ok("ship", line, no)
        warehouse.wait_sent()
        for term, line in ((line_a, L1), (line_b, HVC)):
            term.wait(lambda: term.marks(line, "1")[1] == ON and term.marks(line, "2")[1] == ON, "緑が届く")
        hvc_mid = self.snapshot(HVC)
        a_done = threading.Thread(target=lambda: [line_a.ok("order", L1, no, delivered=True) for no in ("1", "2")])
        a_done.start()
        line_b.ok("order", HVC, "3")        # 同じあいだに HVC は 3 番を取り消す
        a_done.join(timeout=WAIT_SEC)
        for term in (line_a, line_b):
            term.wait_sent()
        self.assertEqual([self.shared_marks(L1, no) for no in ("1", "2", "3")],
                         [("", "", ""), ("", "", ""), (ON, "", "")])
        self.assertEqual([self.shared_marks(HVC, no) for no in ("1", "2", "3")],
                         [(ON, ON, ""), (ON, ON, ""), ("", "", "")])
        # HVC の表で変わったのは HVC の現場が触った 3 番の列だけ(L1 の届いた確認が HVC に及ばない)
        changed = [dict(a) for a, b in zip(hvc_mid, self.snapshot(HVC)) if a != b]
        self.assertEqual([keys.normalize(r[config.COL_KEY]) for r in changed], ["3"], changed)
        # 画面も同じ: 現場の盤には自分のラインだけ・倉庫には両方
        self.assertEqual(sorted(line_a.rows(L1)), sorted(str(n) for n, *_ in KANBAN[L1]))
        status, _body = line_a.call("GET", f"/api/board?line={HVC}")
        self.assertNotEqual(status, 200, "現場Aの画面で HVC の盤が開けた")

    def test_同じ看板を_vbsとexeがほぼ同時に押す(self) -> None:
        """同じラインの 2 台(片方 vbs・片方 exe)が同じ看板を同時に出す → 1 つにまとまり、誰の操作も消えない。"""
        self.distribute()
        warehouse = self.pc("SOUKO-PC", mode=config.MODE_WAREHOUSE, login="userw").vbs()
        site_vbs = self.pc("LINE-A-PC", mode=config.MODE_SITE, line=L1).vbs()
        site_exe = self.pc("LINE-A2-PC", mode=config.MODE_SITE, line=L1).exe()
        start = threading.Barrier(2)
        results: dict[str, tuple] = {}

        def press(term: Terminal) -> None:
            rev = term.rows(L1)["4"]["rev"]
            start.wait(timeout=30)
            results[term.name] = term.press("order", L1, "4", rev=rev)

        threads = [threading.Thread(target=press, args=(t,)) for t in (site_vbs, site_exe)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=WAIT_SEC)
        self.assertEqual({k: v[0] for k, v in results.items()}, {site_vbs.name: 200, site_exe.name: 200}, results)
        for term in (site_vbs, site_exe):
            term.wait_sent()
        self.assertEqual(self.shared_marks(L1, "4"), (ON, "", ""))
        for term in (warehouse, site_vbs, site_exe):
            term.wait(lambda: term.marks(L1, "4") == (ON, "", ""), "4 番が赤でそろう")
            self.assertEqual(term.status()["failures"], 0, f"{term.name}: 要確認が出た")

    def test_古い盤のまま押した2台目は_倉庫の発送を消さない_両向き(self) -> None:
        """vbs で出して倉庫が発送 → 取り込む前の exe(共有が見えない)が同じ看板を押す。その逆も。

        押した時点の値と照らして書き戻す守り(VER3.5.12。``sync._base_guards``)が、
        exe と vbs が混ざっても効く: 発送は残り、押した側には帯で知らせる。
        """
        self.distribute()
        warehouse = self.pc("SOUKO-PC", mode=config.MODE_WAREHOUSE, login="userw").exe()
        for no, (first_kind, second_kind) in (("1", ("vbs", "exe")), ("2", ("exe", "vbs"))):
            with self.subTest(先に出す=first_kind, 古い盤=second_kind):
                first_pc = self.pc(f"FIRST-{no}-PC", mode=config.MODE_SITE, line=L1)
                second_pc = self.pc(f"SECOND-{no}-PC", mode=config.MODE_SITE, line=L1)
                second_pc.use_link()
                first = getattr(first_pc, first_kind)()
                second = getattr(second_pc, second_kind)()
                second_pc.go_offline()           # 2 台目は共有フォルダが見えない(取り込めない)
                first.ok("order", L1, no)
                first.wait_sent()
                warehouse.wait(lambda: warehouse.marks(L1, no)[0] == ON, "倉庫に赤が出る")
                warehouse.ok("ship", L1, no)
                warehouse.wait_sent()
                self.assertEqual(self.shared_marks(L1, no), (ON, ON, ""))
                # 2 台目の盤はまだ「出していない」。押すと提出になる(送信待ちに積む)
                self.assertEqual(second.marks(L1, no), ("", "", ""))
                second.ok("order", L1, no)
                second_pc.go_online()
                second.wait(lambda: second.status()["pending"] == 0, "送信待ちが片付く")
                self.assertEqual(self.shared_marks(L1, no), (ON, ON, ""), "古い盤の操作が倉庫の発送を消した")
                second.wait(lambda: second.marks(L1, no) == (ON, ON, ""), "2 台目の盤が共有のいまにそろう")
                first.stop()
                second.stop()


# ---------------------------------------------------------------------------
# 同じ PC が、ある日は vbs・ある日は exe で開く
# ---------------------------------------------------------------------------
class SwitchLaunchTests(MixedCase):
    def test_vbsで押して届く前に閉じた操作を_次にexeで開いたら送る(self) -> None:
        """手元の SQLite・設定・モードは exe と vbs で同じ所(``%LOCALAPPDATA%`` / ``%APPDATA%``)。"""
        self.distribute()
        machine = self.pc("LINE-A-PC", mode=config.MODE_SITE, line=L1)
        machine.use_link()
        vbs = machine.vbs()
        machine.go_offline()
        vbs.ok("order", L1, "5")
        self.assertEqual(vbs.status()["pending"], 1)
        vbs.stop()                              # 共有が見えないまま閉じた(送れずに残る)
        self.assertEqual(self.shared_marks(L1, "5"), ("", "", ""))
        machine.go_online()
        exe = machine.exe()
        self.assertEqual(exe.mode, config.MODE_SITE)
        exe.wait_sent()
        self.assertEqual(self.shared_marks(L1, "5"), (ON, "", ""), "vbs で押した操作が exe で届かない")
        self.assertEqual(exe.marks(L1, "5"), (ON, "", ""))
        exe.stop()
        # 逆向き: exe で選んだ担当ライン・モードを、次の vbs も使う
        again = machine.vbs()
        self.assertEqual((again.mode, again.health()["line"]), (config.MODE_SITE, L1))
        self.assertEqual(again.marks(L1, "5"), (ON, "", ""))


# ---------------------------------------------------------------------------
# 3. 多重起動の守り(同じ PC / 違う PC)
# ---------------------------------------------------------------------------
class LaunchGuardTests(MixedCase):
    def test_同じPCで_exeが動いていればvbsは理由を画面に出して止まる(self) -> None:
        self.distribute()
        machine = self.pc("LINE-A-PC", mode=config.MODE_SITE, line=L1)
        exe = machine.exe()
        vbs = VbsTerminal(machine)
        self.addCleanup(vbs.stop)
        self.assertEqual(vbs.proc.wait(timeout=BOOT_SEC), 0, vbs.tail())
        # pythonw にはコンソールが無い。理由はブラウザに出す(前は print だけで、誰にも見えなかった)
        opened = vbs.opened_urls()
        self.assertEqual(len(opened), 1, f"理由の画面を開いていない: {opened}")
        self.assertTrue(opened[0].startswith("file:"), opened)
        page = Path(urllib.request.url2pathname(urllib.parse.urlsplit(opened[0]).path))
        self.assertTrue(same_path(page.parent, machine.local_root / "work"), page)
        text = page.read_text(encoding="utf-8")
        self.assertIn("デスクトップ版(日報複合ツールの窓)で動いています", text)
        self.assertIn("「看板」のタブ", text)
        self.assertTrue(exe.health().get("ready"), "動いていた exe が止まった")
        left = [p.name for p in (machine.local_root / "runtime").glob("*.lock") if p.name != "desktop.lock"]
        self.assertEqual(left, [], "止まった vbs の印が残った")

    def test_同じPCで_vbsが動いていればexeは理由を言って止まる(self) -> None:
        self.distribute()
        machine = self.pc("LINE-A-PC", mode=config.MODE_SITE, line=L1)
        vbs = machine.vbs()
        exe = machine.exe(wait_ready=False)
        first = exe.first_event()
        self.assertEqual(first.get("event"), "fatal", first)
        self.assertIn("ブラウザ版が動いています", first.get("message", ""))
        self.assertIn("閉じてから", first.get("hint", ""))
        self.assertTrue(same_path(first.get("log_dir", ""), machine.local_root / "logs"), first)
        self.assertEqual(exe.proc.wait(timeout=WAIT_SEC), 1)
        self.assertTrue(vbs.health().get("ready"), "動いていた vbs が止まった")
        # ブラウザ版を閉じれば exe で開ける(タブの「もう一度開く」)
        vbs.stop()
        self.assertEqual(machine.exe().mode, config.MODE_SITE)

    def test_同じPCで_vbsをもう一度押すと_動いている画面を開く(self) -> None:
        self.distribute()
        machine = self.pc("LINE-A-PC", mode=config.MODE_SITE, line=L1)
        first = machine.vbs()
        again = VbsTerminal(machine)
        self.addCleanup(again.stop)
        self.assertEqual(again.proc.wait(timeout=BOOT_SEC), 0, again.tail())
        opened = again.opened_urls()
        # 前は開く前にプロセスが終わり(開くのが別のスレッドだった)、何も出なかった
        self.assertEqual(len(opened), 1, f"動いている画面を開いていない: {opened}")
        self.assertEqual(urllib.parse.urlsplit(opened[0]).port, first.port)
        self.assertTrue(first.health().get("ready"))

    def test_違うPCなら_exeとvbsは両方動き_共有フォルダに錠や印を置かない(self) -> None:
        self.distribute()
        machines = [self.pc(f"LINE-{i}-PC", mode=config.MODE_SITE, line=L1) for i in "ABCD"]
        before = sorted(p.name for p in self.shared.iterdir())
        terms = [machines[0].exe(), machines[1].vbs(), machines[2].exe(), machines[3].vbs()]
        for term in terms:
            self.assertTrue(term.health().get("ready"), term.name)
        after = sorted(p.name for p in self.shared.rglob("*") if p.is_file())
        extra = [n for n in after if n not in before and not n.endswith(("-journal", "-wal", "-shm"))]
        self.assertEqual(extra, [config.HISTORY_DB_NAME] if config.HISTORY_DB_NAME not in before else [],
                         "共有フォルダに看板の DB 以外のものを置いた")
        for machine, term in zip(machines, terms):
            runtime = machine.local_root / "runtime"
            want = "desktop.lock" if term.kind == "exe" else f"{config.MODE_SITE}.lock"
            self.assertIn(want, [p.name for p in runtime.iterdir()], f"{term.name} の錠・印が手元に無い")


class LaunchFileTests(MixedCase):
    def test_倉庫モードのbatで_倉庫の画面が開く(self) -> None:
        """``run_kanban_warehouse.bat`` がするのは ``pythonw main.py --mode warehouse``(作業フォルダはツールのフォルダ)。

        前は main.py が「Start.vbs を使ってください」を標準エラーに出して終わり(pythonw なので見えない)、
        **押しても何も起きなかった**。
        """
        bat = (ROOT / "run_kanban_warehouse.bat").read_bytes().decode("cp932")
        self.assertIn('pythonw "%~dp0main.py" --mode warehouse', bat)
        self.distribute()
        machine = self.pc("SOUKO-PC", mode=config.MODE_SITE, login="userw")
        term = machine.vbs("--mode", "warehouse", script="main.py")
        self.assertEqual(term.mode, config.MODE_WAREHOUSE)
        self.assertTrue(term.rows(HVC), "倉庫の盤が出ない")
        # 1 回だけ。この端末に保存したモードは変えない
        term.stop()
        store = Store(str(machine.sqlite_file), host_name=machine.name)
        try:
            self.assertEqual(store.get_device_mode(), config.MODE_SITE)
        finally:
            store.close()

    def test_batは共有フォルダに置いても動く形(self) -> None:
        """cmd.exe は共有フォルダ(\\\\サーバ\\…)を今いる場所にできない(``cd /d`` が失敗する)。"""
        for name in ("run_kanban.bat", "run_kanban_warehouse.bat"):
            with self.subTest(name=name):
                data = (ROOT / name).read_bytes()
                head, sep, _rest = data.partition(b"chcp 932")
                self.assertTrue(sep)
                head.decode("ascii")
                self.assertEqual(data.count(b"\n"), data.count(b"\r\n"))
                text = data.decode("cp932")
                commands = [ln for ln in text.splitlines() if not ln.lower().startswith("rem")]
                self.assertFalse([ln for ln in commands if "cd /d" in ln.lower()], commands)
                self.assertIn('start "" /D "%~dp0." pythonw "%~dp0main.py"', text)


# ---------------------------------------------------------------------------
# 3. 配布設定の読み方(exe と vbs で接続先が食い違わない)
# ---------------------------------------------------------------------------
class DistributionTests(MixedCase):
    """一式の ``配布設定\\日報複合ツール.json`` と看板のフォルダの ``配布設定\\設定.json``。"""

    def shares(self) -> tuple[Path, Path]:
        """まとめたファイルが指す共有 X と、看板のフォルダの設定.json が指す共有 Y。"""
        x = make_shared(self.base / "共有X") / config.TARGET_SHARED_DB_NAME
        y = make_shared(self.base / "共有Y") / config.TARGET_SHARED_DB_NAME
        return x, y

    def connected(self, term: Terminal) -> str:
        return term.get("/api/shared-db-status")["path"]

    def check_both(self, app: Path, want: Path, label: str) -> None:
        """同じ一式を、PC1 は exe・PC2 は vbs で開く。どちらも ``want`` を見る(画面・設定ファイル・書き戻し先)。"""
        exe_pc = PC(self, f"EXE-{label}", login="user1", app=app)
        vbs_pc = PC(self, f"VBS-{label}", login="user2", app=app)
        for machine, line in ((exe_pc, L1), (vbs_pc, HVC)):
            machine.set_mode(config.MODE_SITE)
            machine.write_settings(line=line)
        exe, vbs = exe_pc.exe(), vbs_pc.vbs()
        for machine, term, line in ((exe_pc, exe, L1), (vbs_pc, vbs, HVC)):
            with self.subTest(配布=label, 起動=term.kind):
                self.assertTrue(same_path(self.connected(term), want), f"{term.name}: {self.connected(term)}")
                self.assertTrue(same_path(machine.settings()["shared_db_path"], want))
                term.ok("order", line, "1")
                term.wait_sent()
                conn = sqlite3.connect(str(want))
                try:
                    got = conn.execute(f"SELECT [欲] FROM [{TABLE[line]}] WHERE [管理番号] = 1").fetchone()[0]
                finally:
                    conn.close()
                self.assertEqual(got, ON, f"{term.name} の操作が {want} に届かない")
        exe.stop()
        vbs.stop()

    def test_まとめたファイルだけ(self) -> None:
        x, _y = self.shares()
        write_integrated_distribution(self.app, {"shared_db_path": str(x)}, "2026/10/26 08:00:00")
        self.check_both(self.app, x, "まとめ")

    def test_看板のフォルダの設定だけ(self) -> None:
        _x, y = self.shares()
        write_tool_distribution(self.app, {"shared_db_path": str(y)}, "2026/10/26 08:00:00")
        self.check_both(self.app, y, "看板")

    def test_両方あれば新しいほう(self) -> None:
        x, y = self.shares()
        write_integrated_distribution(self.app, {"shared_db_path": str(x)}, "2026/10/26 09:00:00")
        write_tool_distribution(self.app, {"shared_db_path": str(y)}, "2026/10/25 17:00:00")
        self.check_both(self.app, x, "まとめが新しい")
        other = make_bundle(self.base / "もう1つの一式")
        write_integrated_distribution(other, {"shared_db_path": str(x)}, "2026/10/25 17:00:00")
        write_tool_distribution(other, {"shared_db_path": str(y)}, "2026/10/26 09:00:00")
        self.check_both(other, y, "看板が新しい")

    def test_単体のフォルダは_まとめたファイルを読まない(self) -> None:
        """看板だけを配ったフォルダ(``make_dist`` の形)。一式の外なので、上の階層の 日報複合ツール.json は読まない。"""
        x, y = self.shares()
        single = copy_tool(self.base / "単体" / "資材発注看板システム")
        write_tool_distribution(single, {"shared_db_path": str(y)}, "2026/10/25 17:00:00")
        # 紛らわしい置き方: 単体のフォルダの上に、よその一式のまとめたファイルがある
        (self.base / "単体" / "配布設定").mkdir()
        write_integrated_distribution(single, {"shared_db_path": str(x)}, "2026/10/26 09:00:00")
        self.check_both(single, y, "単体")


# ---------------------------------------------------------------------------
# 3. ログ・記録の置き場所(共有フォルダに集めたとき、PC が混ざらない)
# ---------------------------------------------------------------------------
class LogPlacementTests(MixedCase):
    def test_同じラインのexeとvbsが_共有のログに混ぜて書かない(self) -> None:
        logs = self.base / "共有" / "ログ"
        self.distribute(log_dir=str(logs))
        machines = {"exe": self.pc("LINE-A-PC", mode=config.MODE_SITE, line=L1),
                    "vbs": self.pc("LINE-A2-PC", mode=config.MODE_SITE, line=L1)}
        terms = {kind: getattr(machine, kind)() for kind, machine in machines.items()}
        for term in terms.values():
            term.ok("order", L1, "1" if term.kind == "exe" else "2")
            term.wait_sent()
        for term in terms.values():
            term.stop()
        files = sorted((logs / "kanban" / L1).glob("kanban_DebugLog_*.txt"))
        self.assertTrue(files, f"共有のログに書いていない: {list(logs.rglob('*'))}")
        by_pc: dict[str, list[str]] = {}
        for f in files:
            text = f.read_text(encoding="utf-8")
            who = [m.name for m in machines.values() if f"PC名: {m.name}" in text]
            self.assertEqual(len(who), 1, f"{f.name}: どの PC のログか分からない/混ざっている")
            # 名前を見れば、開かなくてもどの PC か分かる(同じ秒に開いた 2 台が同じファイルにならない)
            self.assertTrue(f.stem.endswith("_" + who[0]), f.name)
            by_pc.setdefault(who[0], []).append(f.name)
        self.assertEqual(sorted(by_pc), sorted(m.name for m in machines.values()))
        # 記録(なぜなぜ用)は PC ごとのファイル。起動の記録に「どちらの画面か」が入る
        seen = {}
        for f in (logs / "kanban" / "記録").rglob("kanban_記録_*.jsonl"):
            for line in f.read_text(encoding="utf-8").splitlines():
                rec = json.loads(line)
                if rec.get("kind") == "startup" and str(rec.get("what", "")).startswith("起動"):
                    self.assertIn(f"_{rec['pc']}_", f.name)
                    seen.setdefault(rec["pc"], set()).add(str((rec.get("extra") or {}).get("画面", "")))
        self.assertEqual(seen.get("LINE-A-PC"), {"デスクトップ版(ポートなし)"}, seen)
        self.assertEqual(seen.get("LINE-A2-PC"), {"ブラウザ版"}, seen)


# ---------------------------------------------------------------------------
# アプリのフォルダ(共有フォルダに置くと、全部の PC が同じ所)に書かない
# ---------------------------------------------------------------------------
class AppFolderTests(MixedCase):
    def test_exeとvbsのどちらで開いても_アプリのフォルダにキャッシュを書かない(self) -> None:
        self.distribute()
        machines = [self.pc("LINE-A-PC", mode=config.MODE_SITE, line=L1),
                    self.pc("LINE-B-PC", mode=config.MODE_SITE, line=HVC)]
        terms = [machines[0].exe(), machines[1].vbs()]
        for term in terms:
            term.rows(term.health()["line"])
        caches = sorted(str(p.relative_to(self.app)) for p in self.app.rglob("__pycache__"))
        self.assertEqual(caches, [], "アプリのフォルダに __pycache__ を書いた(共有フォルダなら全部の PC が書く)")
        for machine in machines:
            self.assertTrue(list((machine.local_root / "pycache").rglob("*.pyc")), f"{machine.name} の手元に無い")



# ---------------------------------------------------------------------------
# 3. 版が違う 2 台(片方の PC だけ古い版のまま)
# ---------------------------------------------------------------------------
#: 古い版として使う版(ひとつ前の看板 3.5.14)。git の履歴から取り出す
OLD_COMMIT = "baa34b6"
OLD_VERSION = "3.5.14"


def extract_old_bundle(dest: Path) -> Optional[Path]:
    """git の履歴から古い版の一式を取り出す。取り出せなければ ``None``(履歴の無い取り出し方など)。"""
    import io
    import tarfile

    git = shutil.which("git")
    if not git:
        return None

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([git, *args], cwd=str(ROOT), capture_output=True, timeout=120)

    top = run("rev-parse", "--show-toplevel")
    if top.returncode != 0 or run("cat-file", "-e", f"{OLD_COMMIT}^{{commit}}").returncode != 0:
        return None
    top_dir = Path(top.stdout.decode("utf-8").strip()).resolve()
    rel = ROOT.resolve().relative_to(top_dir).as_posix()
    paths = [rel] + (["config/tools.json"] if rel == "tools/kanban" else [])
    done = run("-C", str(top_dir), "archive", "--format=tar", OLD_COMMIT, "--", *paths)
    if done.returncode != 0:
        return None
    with tarfile.open(fileobj=io.BytesIO(done.stdout)) as tar:
        members = [m for m in tar.getmembers() if "/tests/" not in f"/{m.name}/"]
        safe = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
        tar.extractall(dest, members=members, **safe)   # noqa: S202 - 自分のリポジトリの履歴
    app = dest / rel
    if rel != "tools/kanban":                          # 看板だけのリポジトリ: 一式の形にする
        whole = dest / "日報複合ツール"
        (whole / "config").mkdir(parents=True)
        (whole / "config" / "tools.json").write_text('{"tools": []}', encoding="utf-8")
        shutil.move(str(app), str(whole / "tools" / "kanban"))
        app = whole / "tools" / "kanban"
    return app


class OldVersionTests(MixedCase):
    """片方の PC だけ古い版(``OLD_VERSION``)のまま。新しい版の exe / vbs と混ぜても流れが届く。"""

    def setUp(self) -> None:
        super().setUp()
        self.old_app = extract_old_bundle(self.base / "古い一式")
        if self.old_app is None:
            self.skipTest(f"古い版({OLD_COMMIT})を git の履歴から取り出せません(履歴の無い取り出し方)")
        self.distribute()
        # 古い版の一式にも同じ配布設定(大設定がまとめて書き出したもの)
        write_integrated_distribution(self.old_app, json.loads(
            (bundle_root(self.app) / "配布設定" / "日報複合ツール.json").read_text(encoding="utf-8")
        )["tools"]["kanban"]["settings"], "2026/10/26 08:00:00")

    def old_pc(self, name: str, *, mode: str, line: str = "", login: str = "") -> PC:
        machine = PC(self, name, login=login or f"user-{name.lower()}", app=self.old_app)
        machine.set_mode(mode)
        if line:
            machine.write_settings(line=line)
        return machine

    def test_古い版と新しい版が混ざっても_往復が両向きに届く(self) -> None:
        warehouse = self.pc("SOUKO-PC", mode=config.MODE_WAREHOUSE, login="userw").exe()
        old_vbs = self.old_pc("LINE-A-PC", mode=config.MODE_SITE, line=L1).vbs()
        old_exe = self.old_pc("LINE-B-PC", mode=config.MODE_SITE, line=HVC).exe()
        self.assertEqual({t.health()["version"] for t in (old_vbs, old_exe)}, {OLD_VERSION})
        self.assertNotEqual(warehouse.health()["version"], OLD_VERSION)
        for site, line in ((old_vbs, L1), (old_exe, HVC)):
            with self.subTest(現場=site.name):
                self.assertTrue(same_path(site.get("/api/shared-db-status")["path"], self.shared_db))
                site.ok("order", line, "1")
                site.wait_sent()
                warehouse.wait(lambda: warehouse.marks(line, "1")[0] == ON, "倉庫に赤が出る")
                warehouse.ok("ship", line, "1")
                warehouse.wait_sent()
                site.wait(lambda: site.marks(line, "1") == (ON, ON, ""), "古い版の現場に緑が出る")
                site.ok("order", line, "2")
                site.wait_sent()
                warehouse.wait(lambda: warehouse.marks(line, "2")[0] == ON, "倉庫に赤が出る")
                warehouse.ok("hold", line, "2")
                warehouse.wait_sent()
                site.wait(lambda: site.marks(line, "2") == (ON, "", ON), "古い版の現場に黄が出る")
                site.ok("order", line, "1", delivered=True)
                site.wait_sent()
                warehouse.wait(lambda: warehouse.marks(line, "1") == ("", "", ""), "倉庫で届いた確認が出る")
        self.assertEqual([self.shared_marks(L1, n) for n in ("1", "2")], [("", "", ""), (ON, "", ON)])
        self.assertEqual([self.shared_marks(HVC, n) for n in ("1", "2")], [("", "", ""), (ON, "", ON)])
        warehouse.wait(lambda: warehouse.presence() == {L1: "LINE-A-PC", HVC: "LINE-B-PC"} or None,
                       "倉庫に、古い版の現場 2 台が開いていると出る")

    def test_古い版の古い盤の2台目も_倉庫の発送を消さない(self) -> None:
        warehouse = self.pc("SOUKO-PC", mode=config.MODE_WAREHOUSE, login="userw").vbs()
        new_site = self.pc("LINE-A-PC", mode=config.MODE_SITE, line=L1).exe()
        old_machine = self.old_pc("LINE-A2-PC", mode=config.MODE_SITE, line=L1)
        old_machine.use_link()
        old_site = old_machine.vbs()
        old_machine.go_offline()
        new_site.ok("order", L1, "3")
        new_site.wait_sent()
        warehouse.wait(lambda: warehouse.marks(L1, "3")[0] == ON, "倉庫に赤が出る")
        warehouse.ok("ship", L1, "3")
        warehouse.wait_sent()
        old_site.ok("order", L1, "3")          # 古い版の古い盤には、まだ出していないように見える
        old_machine.go_online()
        old_site.wait(lambda: old_site.status()["pending"] == 0, "送信待ちが片付く")
        self.assertEqual(self.shared_marks(L1, "3"), (ON, ON, ""), "古い版の操作が倉庫の発送を消した")

    def test_同じPCで_古い版のvbsが動いていれば_新しい版のexeは止まる(self) -> None:
        machine = self.old_pc("LINE-A-PC", mode=config.MODE_SITE, line=L1)
        old_vbs = machine.vbs()
        machine.install(self.app)                # 同じ PC に新しい一式を入れた(exe はそちら)
        exe = machine.exe(wait_ready=False)
        first = exe.first_event()
        self.assertEqual(first.get("event"), "fatal", first)
        self.assertIn("ブラウザ版が動いています", first.get("message", ""))
        self.assertTrue(old_vbs.health().get("ready"))


if __name__ == "__main__":
    unittest.main()
