"""exe 起動と vbs 起動が**混ざった現場**の日報(PC ごとに別の手元の領域・同じ共有フォルダ)

    倉庫モードはexe起動、現場はvbs起動をしてた場合や、違うラインでvbs起動、
    exe起動していた場合でのテストをしてください

【現場】PC によって起こし方が違う。
- **exe 起動**: 日報複合ツールの窓(Tauri の外枠)が ``bridge.py`` を子として起こし、標準入出力で
  要求を渡す(ポートで待ち受けない)。
- **vbs 起動**: ``Start.vbs`` → ``pythonw start_app.py``(コンソール無し・引数無し)→ ポートで待ち受け、
  ブラウザが開く。

どちらの PC も、配布設定で同じ共有フォルダ(日報データ・参照用マスタ・控え・ログの出力パス)を見る。

【ここで見ること】
    1. ライン L-1 = vbs・HVC = exe(とその逆)で、それぞれ1直を打って保存・共有へ保存
       → 相手の端末(管理者モードの「記録を見る」)・Administrator の PC(集計)に出る。
       ログは ``<ログの出力パス>\\nippou\\nippou_…_<PC>`` に PC ごとに分かれる
    2. 同じラインの2台(片方 vbs・片方 exe)で、「ほかの端末でも入力されています」が両向きに出る
    3. 直の切り替わりで、次の直の端末が控え(LocalBackup)から受け取る(vbs → exe・exe → vbs)
    4. 同じ PC で vbs と exe を両方起こしたら片方が理由を言って止まる/違う PC なら両方動く
    5. 配布設定の読み方(ツールのフォルダの ``設定.json`` だけ/まとめたファイルだけ/両方)で、
       vbs と exe の参照先が食い違わない
    6. 版の違う2台(版の数字だけ変えた写し)

【この試験で見つけて直したもの】
- ログのファイル名・共有保存の履歴の「端末」が、ネットワークの名前(``socket.gethostname()``)
  だった。アクセス権限の表・「ほかの端末」の知らせは ``COMPUTERNAME`` なので、名前の違う PC では
  食い違う(ここでは PC ごとに分かれるはずのログが1つに混ざった)→ ``push_history.terminal_name``
- 同じ PC の vbs を閉じてすぐ開き直すと、Linux では閉じたばかりの接続の名残で番号が「使用中」に
  見え、候補が尽きると「使えるポートがありません」で起動できなかった → ``launch_guard.is_port_free``
- ラインを持たない Administrator の PC が集計で L-1 を選ぶと、L-1 の控えではなく手元の空の表を
  出していた(仮のラインが L-1 のため)→ ``services/line_view.reads_local``

【まね方】
- **PC 1台 = 手元の領域1つ**: ``%LOCALAPPDATA%``(``<LOCALAPPDATA>\\NippouTool``)・ホーム・
  ``COMPUTERNAME``。ログイン名は全端末で同じ(現場の形。ラインはアクセス権限の表の PC 名で決まる)。
  試験の ``NIPPOU_LOCAL_DIR`` などは子に渡さない(本物と同じ道で場所を決めさせる)。共有フォルダの
  場所は配布設定で配る(``NIPPOU_DIST_DIR`` で、共有に置いた ``配布設定\\`` を読ませる。5 だけは
  一式の写しの中の本物の置き場所を読ませる)。
- **同時起動の錠は手元の領域のファイル**(``runtime\\nippou.lock`` と、デスクトップ版が握る
  ``runtime\\desktop.lock``。Windows でも名前付きミューテックスは使わない)。PC ごとに手元の領域を
  分けるので、1台の機械でまねても違う PC どうしの錠はぶつからない(点検表のような差し替えは要らない)。
  ポートの番号は PC ごとに空いているものを1つだけ(``port_retry = 0``)── 起動の判定はポートを
  叩いて自分を探すので、ほかの PC の番号まで探させない。
- **vbs 起動**: WSH は Linux で動かないので、``Start.vbs`` がすること(作業フォルダ = ツールの
  フォルダ・``pythonw start_app.py``)をまねる。pythonw のまねとして標準出力・標準エラーを ``None``
  にし、ブラウザを開く所だけ「開いた宛先を書き留める」に替える(起動できないときに開く案内の
  ページも書き留めるだけ ── 日報は Windows の窓(MessageBox)を出さないので、押す人が居なくても
  閉じない窓は無い)。
- **exe 起動**: 外枠と同じ道(作業フォルダ・``NIPPOU_TOKEN``・``PYTHONIOENCODING=utf-8``・
  ``ALLTOOLS_SHELL``・``ALLTOOLS_TOOL``・``ALLTOOLS_ROOT``)で ``bridge.py`` を起こし、外枠の代わりに
  要求を渡す。
- **時計**: 直を打つ試験は、全部の PC が同じ時計ファイルを見る(``test_flow_multi_terminal`` と
  同じ。止まった時計)。差し替えは起動の前に済ませ、そのあとは本物の ``start_app.py`` / ``bridge.py``
  を走らせる。起動だけを見る試験(4・5)は時計を差し替えない。

**時間には頼らず、状態を待つ**。パスは文字列で比べず、解いた値で比べる。
"""
from __future__ import annotations

import json
import os
import shutil
import site
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bridge as bridge_module  # noqa: E402
from tests._web import HAS_FLASK, SKIP_REASON  # noqa: E402
from tests.test_flow_multi_terminal import (  # noqa: E402
    DAY, LOGIN, NEXT_DAY, FlowCase, Terminal, _handover, clean_shift_rows, row, rows_of)
from tests.test_flow_multi_terminal import wait_for as _wait_for  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent       # tools/nippou
REPO = ROOT.parents[1]

#: 起動を待つ上限(秒)。Windows の CI は遅い
WAIT = 120.0

#: 子に渡さない環境変数(試験の差し替え・ほかのツールの差し替え)
_DROP_PREFIXES = ("NIPPOU_", "ALLTOOLS_", "FLOW_")
_DROP = ("PYTHONUTF8", "XDG_DATA_HOME")

#: 時計の差し替え(`FLOW_CLOCK` があるときだけ)。`test_flow_multi_terminal._CHILD` と同じ形で、
#: **起動の前に**日報の部品を読み込んでおき、日付と時刻の出どころを時計ファイルへ向ける
_CLOCK = r"""
import os, sys
sys.path.insert(0, os.getcwd())
if os.environ.get("FLOW_CLOCK"):
    from datetime import date as _real_date, datetime as _real_datetime

    _CLOCK = os.environ["FLOW_CLOCK"]
    _cache = {"text": None, "value": None}

    def _now():
        try:
            with open(_CLOCK, encoding="utf-8") as fh:
                text = fh.read().strip()
            if text != _cache["text"]:
                _cache.update(text=text, value=_real_datetime.strptime(text, "%Y-%m-%d %H:%M:%S"))
        except (OSError, ValueError):
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
    import start_app, server  # noqa: F401
    for _mname, _module in list(sys.modules.items()):
        if _module is None or not (_mname in ("nippou", "app", "start_app", "server")
                                   or _mname.startswith(("nippou.", "app."))):
            continue
        for _attr, _real, _fake in (("datetime", _real_datetime, FrozenDateTime),
                                    ("date", _real_date, FrozenDate)):
            if getattr(_module, _attr, None) is _real:
                setattr(_module, _attr, _fake)
"""

#: vbs 起動(`pythonw start_app.py`)。**標準出力・標準エラーは最初から無い**(pythonw)。
#: 起動の前に倒れたら、理由を `<FLOW_OPENED>.crash` に残す(画面にも記録にも出ないので)
_VBS_CHILD = r"""
import os, sys, traceback
sys.stdout = None          # pythonw.exe にはコンソールが無い
sys.stderr = None
try:
    exec(compile(os.environ["FLOW_PRELUDE"], "<prelude>", "exec"))
    import runpy, webbrowser

    def _opened(url, *args, **kwargs):
        with open(os.environ["FLOW_OPENED"], "a", encoding="utf-8") as fh:
            fh.write(url + "\n")
        return True

    webbrowser.open = _opened
    script = os.path.join(os.getcwd(), "start_app.py")
    sys.argv = [script]
    runpy.run_path(script, run_name="__main__")
except SystemExit:
    raise
except BaseException:
    with open(os.environ["FLOW_OPENED"] + ".crash", "a", encoding="utf-8") as fh:
        fh.write(traceback.format_exc())
    raise
"""

#: exe 起動(外枠が起こす `bridge.py`)
_EXE_CHILD = r"""
import os, sys
exec(compile(os.environ["FLOW_PRELUDE"], "<prelude>", "exec"))
import runpy
script = os.path.join(os.getcwd(), "bridge.py")
sys.argv = [script]
runpy.run_path(script, run_name="__main__")
"""

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_for(ok: Callable[[], Any], what: str, timeout: float = WAIT) -> Any:
    return _wait_for(what, ok, timeout=timeout)


def same_path(a: Any, b: Any) -> bool:
    return Path(str(a)).resolve() == Path(str(b)).resolve()


def page_text(url: str) -> str:
    """vbs 起動が開いた案内のページ(`file:`)の字だけ。"""
    import html
    import re
    try:
        path = Path(urllib.request.url2pathname(urllib.parse.urlparse(url).path))
        body = path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"(案内を読めません: {exc})"
    body = re.sub(r"(?s)<style.*?</style>", "", body)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", body))).strip()


def write_distribution(folder: Path, settings: dict, created_at: str = "2026-10-01 08:00:00") -> None:
    """ツールのフォルダの `配布設定\\設定.json`(設定画面の「配布設定を書き出す」と同じ形)。"""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "設定.json").write_text(json.dumps({
        "format": 1, "created_at": created_at, "created_on": "PC-ADMIN",
        "settings": settings}, ensure_ascii=False), encoding="utf-8")


# ===========================================================================
# PC 1台
# ===========================================================================
class Pc:
    def __init__(self, base: Path, pc_name: str, *, tool_dir: Path = ROOT, version: str = "",
                 folder: str = "") -> None:
        self.pc_name = pc_name
        self.name = folder or pc_name
        self.tool_dir = tool_dir
        self.dir = base / "PC" / self.name
        self.home = self.dir / "home"
        self.local_app_data = self.home / "AppData" / "Local"
        self.local_app_data.mkdir(parents=True, exist_ok=True)
        conf = json.loads((tool_dir / "config" / "app.json").read_text(encoding="utf-8"))
        conf["server"]["port"] = free_port()
        conf["server"]["port_retry"] = 0
        if version:
            conf["version"] = version
        self.version = str(conf["version"])
        self.app_json = self.dir / "app.json"
        self.app_json.write_text(json.dumps(conf, ensure_ascii=False), encoding="utf-8")

    @property
    def local_root(self) -> Path:
        return self.local_app_data / "NippouTool"

    def env(self, distribution: Optional[Path], clock: Optional[Path]) -> dict:
        env = {k: v for k, v in os.environ.items()
               if k not in _DROP and not k.startswith(_DROP_PREFIXES)}
        env.update({
            "LOCALAPPDATA": str(self.local_app_data),
            "HOME": str(self.home), "USERPROFILE": str(self.home),
            "COMPUTERNAME": self.pc_name,
            "USERNAME": LOGIN, "LOGNAME": LOGIN, "USER": LOGIN,
            "NIPPOU_APP_CONFIG": str(self.app_json),
            # ホームを差し替えても、利用者ごとに入れたパッケージ(Flask など)は読めるように
            "PYTHONUSERBASE": site.getuserbase(),
            "PYTHONDONTWRITEBYTECODE": "1",
            "FLOW_PRELUDE": _CLOCK,
        })
        if distribution is not None:
            env["NIPPOU_DIST_DIR"] = str(distribution)
        if clock is not None:
            env["FLOW_CLOCK"] = str(clock)
        return env


class _Run:
    edition = ""
    pc: Pc

    def get(self, path: str) -> Any:
        status, body = self.call("GET", path)
        if status != 200:
            raise AssertionError(f"{self.pc.name}({self.edition}) GET {path}: {status} {body}")
        return body

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:   # pragma: no cover
        raise NotImplementedError

    def wait_ready(self) -> dict:
        def ready() -> Optional[dict]:
            health = self.get("/api/health")
            if health.get("startup_error"):
                raise AssertionError(f"{self.pc.name}({self.edition}) の起動が止まりました: "
                                     f"{health['startup_error']}")
            return health if health.get("ready") else None
        return wait_for(ready, f"{self.pc.name}({self.edition}) の準備完了")

    @staticmethod
    def _decode(status: int, raw: bytes) -> tuple[int, Any]:
        try:
            return status, json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            return status, raw.decode("utf-8", "replace")


class VbsRun(_Run):
    edition = "vbs"

    def __init__(self, pc: Pc, distribution: Optional[Path], *, clock: Optional[Path] = None,
                 wait: bool = True) -> None:
        self.pc = pc
        self.opened = pc.dir / f"opened-{uuid.uuid4().hex[:6]}.txt"
        env = pc.env(distribution, clock)
        env["FLOW_OPENED"] = str(self.opened)
        self.proc = subprocess.Popen([sys.executable, "-c", _VBS_CHILD], cwd=str(pc.tool_dir), env=env,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        self.url = self.base = self.token = ""
        if wait:
            self.wait_opened()

    def opened_urls(self) -> list[str]:
        try:
            return [u for u in self.opened.read_text(encoding="utf-8").splitlines() if u.strip()]
        except OSError:
            return []

    def wait_opened(self) -> str:
        def ready() -> Optional[str]:
            urls = self.opened_urls()
            if urls:
                return urls[-1]
            if self.proc.poll() is not None:
                raise AssertionError(f"{self.pc.name} の vbs 起動が先に終わりました(終了コード "
                                     f"{self.proc.returncode})\n{self.crash()}\n{self.log_text()[-3000:]}")
            return None
        self.url = wait_for(ready, f"{self.pc.name} の vbs 起動がブラウザを開く")
        if not self.url.startswith("http"):
            raise AssertionError(f"{self.pc.name} の vbs 起動が画面ではなく案内を開きました: {self.url}\n"
                                 f"{page_text(self.url)}\n{self.log_text()[-3000:]}")
        head, _, query = self.url.partition("?")
        self.base = head.rstrip("/")
        self.token = dict(p.split("=", 1) for p in query.split("&") if "=" in p).get("t", "")
        return self.url

    def crash(self) -> str:
        try:
            return Path(str(self.opened) + ".crash").read_text(encoding="utf-8")
        except OSError:
            return ""

    def log_text(self) -> str:
        """手元の起動の記録(出力パスへ届く前の分)。"""
        folder = self.pc.local_root / "logs" / "nippou"
        return "".join(p.read_text(encoding="utf-8", errors="replace")
                       for p in sorted(folder.glob("*.log"))) if folder.is_dir() else ""

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(f"{self.base}{path}", data=data, method=method, headers={
            "X-Tool-Token": self.token, "Content-Type": "application/json"})
        try:
            with _OPENER.open(request, timeout=120) as response:
                raw, status = response.read(), response.status
        except urllib.error.HTTPError as exc:
            raw, status = exc.read(), exc.code
        return self._decode(status, raw)

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


class ExeRun(_Run):
    edition = "exe"

    def __init__(self, pc: Pc, distribution: Optional[Path], *, clock: Optional[Path] = None) -> None:
        self.pc = pc
        self.token = f"tok-{uuid.uuid4().hex[:8]}"
        env = pc.env(distribution, clock)
        env.pop("PYTHONUTF8", None)
        env.update({"NIPPOU_TOKEN": self.token, "PYTHONIOENCODING": "utf-8", "ALLTOOLS_SHELL": "1",
                    "ALLTOOLS_TOOL": "nippou", "ALLTOOLS_ROOT": str(pc.tool_dir.parents[1])})
        self.proc = subprocess.Popen([sys.executable, "-c", _EXE_CHILD], cwd=str(pc.tool_dir),
                                     env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
        self.events: list[dict] = []
        self.replies: dict[int, tuple[dict, bytes]] = {}
        self.err: list[str] = []
        self.cond = threading.Condition()
        self.next_id = 1
        threading.Thread(target=self._drain, daemon=True).start()
        threading.Thread(target=self._read, daemon=True).start()
        self.first = self._first_word()

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

    def _first_word(self) -> dict:
        deadline = time.monotonic() + WAIT
        with self.cond:
            while not self.events and time.monotonic() < deadline:
                self.cond.wait(0.2)
            if not self.events:
                raise AssertionError(f"{self.pc.name}(exe): 1行目が来ません\n" + "\n".join(self.err[-20:]))
            return self.events[0]

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        data = b"" if body is None else json.dumps(body).encode("utf-8")
        with self.cond:
            rid = self.next_id
            self.next_id += 1
        path, _, query = path.partition("?")
        head = {"id": rid, "method": method, "path": path, "query": query, "len": len(data),
                "headers": [["Host", "app.localhost"], ["X-Tool-Token", self.token],
                            ["Content-Type", "application/json"]]}
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(head, ensure_ascii=False).encode("utf-8") + b"\n" + data)
        self.proc.stdin.flush()
        deadline = time.monotonic() + WAIT
        with self.cond:
            while rid not in self.replies and time.monotonic() < deadline:
                self.cond.wait(0.2)
            if rid not in self.replies:
                raise AssertionError(f"{self.pc.name}(exe) {method} {path} に答えません\n"
                                     + "\n".join(self.err[-20:]))
            reply, raw = self.replies.pop(rid)
        return self._decode(reply["status"], raw)

    def stop(self) -> None:
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
# 端末(画面でやること)── 起こし方だけ vbs / exe に替える
# ===========================================================================
class Term(Terminal):
    """`test_flow_multi_terminal.Terminal` と同じ画面の道。立て方だけ本物の vbs / exe。"""

    def __init__(self, case: "_MixedCase", pc: Pc, edition: str, *,
                 distribution: Optional[Path] = None) -> None:
        self.pc_obj = pc
        self.edition = edition
        self.distribution = distribution or case.dist
        self.run: Optional[_Run] = None
        super().__init__(case, f"{pc.name}_{edition}", pc=pc.pc_name)

    def start(self) -> None:
        if self.edition == "vbs":
            self.run = VbsRun(self.pc_obj, self.distribution, clock=self.case.clock.path)
        else:
            run = ExeRun(self.pc_obj, self.distribution, clock=self.case.clock.path)
            self.run = run
            self.case.assertEqual(run.first.get("event"), "started",
                                  f"{self.name}: {run.first}\n" + "\n".join(run.err[-20:]))
        self.run.wait_ready()
        self.opened = None

    def stop(self) -> None:
        run, self.run = self.run, None
        if run is not None:
            run.stop()

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        assert self.run is not None, f"{self.name} は止まっています"
        return self.run.call(method, path, body)

    @property
    def local_db(self) -> Path:
        return self.pc_obj.local_root / "data" / "nippou_local.sqlite3"


def _other(body: dict) -> dict:
    return body["other_terminal"]


# ===========================================================================
# 土台: 共有フォルダ1つ・配布設定・時計
# ===========================================================================
class _MixedCase(FlowCase):
    def setUp(self) -> None:
        super().setUp()                 # 一時フォルダ・共有(参照用マスタ・アクセス権限)・時計
        self.output = self.shared / "ログ出力"
        self.output.mkdir()
        # 配布設定(アプリを共有フォルダに置いて配った形: どの PC も同じ 配布設定\ を見る)
        self.dist = self.shared / "アプリ" / "配布設定"
        write_distribution(self.dist, self.dist_settings())

    def dist_settings(self) -> dict:
        return {"access_dir": str(self.shared), "gw_reference_dir": str(self.ref),
                "log_dir": str(self.output)}

    def pc(self, pc_name: str, **kwargs) -> Pc:
        return Pc(self.base, pc_name, **kwargs)

    def term(self, pc_name: str, edition: str) -> Term:
        return Term(self, self.pc(pc_name), edition)

    def assert_settings(self, term: Term) -> None:
        """配布設定の置き場所を、どちらの起こし方でも読んでいる。"""
        paths = term.get("/api/settings/state")["path_map"]
        for key, want in (("access_dir", self.shared), ("gw_reference_dir", self.ref),
                          ("log_dir", self.output)):
            with self.subTest(term=term.name, key=key):
                self.assertTrue(same_path(paths[key]["resolved"], want), (key, paths[key]))

    def log_names(self) -> list[str]:
        folder = self.output / "nippou"
        return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


def _editions(first_vbs: bool) -> tuple[str, str]:
    return ("vbs", "exe") if first_vbs else ("exe", "vbs")


# ===========================================================================
# 1. 違うライン: L-1 と HVC
# ===========================================================================
@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class _TwoLines(_MixedCase):
    L1_VBS = True

    def test_それぞれ1直を打って共有へ保存_相手と管理者のPCに出る_ログはPCごと(self) -> None:
        e_l1, e_hvc = _editions(self.L1_VBS)
        l1 = self.term("PC-L1A", e_l1)
        hvc = self.term("PC-HVC", e_hvc)
        self.assertEqual((l1.line, hvc.line), ("L-1", "HVC"))
        for term in (l1, hvc):
            self.assert_settings(term)

        self.at("2026-10-05 07:05")
        l1.start_shift("山田")
        hvc.start_shift("田中")
        self.at("2026-10-05 14:30")
        l1.saved(clean_shift_rows("1直", "A"), "山田")
        hvc.saved(clean_shift_rows("1直", "H"), "田中")
        for term, worker in ((l1, "山田"), (hvc, "田中")):
            status, body = term.push_now(worker=worker)
            self.assertEqual(status, 200, f"{term.name}: {body}")
        self.assertEqual(self.shared_lots("L-1"), {(DAY, "1直", 1): ["A01", "A02"]})
        self.assertEqual(self.shared_lots("HVC"), {(DAY, "1直", 1): ["H01", "H02"]})
        self.assertEqual(l1.dirty_keys() | hvc.dirty_keys(), set())
        l1.wait_backup()
        hvc.wait_backup()

        # 相手の端末: 共有保存の履歴(全ライン)と、管理者モードの「記録を見る」(控え)
        for me, other, line in ((l1, hvc, "HVC"), (hvc, l1, "L-1")):
            with self.subTest(me=me.name, other=other.name):
                log = me.ok("/api/agg/push-log", {"scope": "all", "start": "2026-10-01",
                                                  "end": "2026-10-05"})
                text = json.dumps(log, ensure_ascii=False)
                self.assertIn(line, text)
                self.assertIn("田中" if line == "HVC" else "山田", text)
                status, _ = me.call("GET", f"/api/records/backup?line={line}")
                self.assertEqual(status, 403, "管理者モードでなくても、ほかのラインの控えが見えた")
                me.admin_on()
                listed = me.get(f"/api/records/backup?line={line}")["shifts"]
                self.assertEqual([(s["report_date"], s["shift"], s["synced"]) for s in listed],
                                 [(DAY, "1直", True)])

        # Administrator の PC(起こし方は L-1 と逆): 集計でどちらのラインも見られる(混ぜない)
        admin = self.term("PC-ADMIN", e_hvc)
        self.assert_settings(admin)
        for line, lots in (("L-1", ("A01", "A02")), ("HVC", ("H01", "H02"))):
            with self.subTest(admin_line=line):
                view = admin.ok("/api/agg/tables", {"line": line, "start": "2026-10-01",
                                                    "end": "2026-10-05"})
                self.assertEqual(view["line"], line)
                text = json.dumps(view["tables"], ensure_ascii=False)
                for lot in lots:
                    self.assertIn(lot, text)
                other_lots = ("H01",) if line == "L-1" else ("A01",)
                self.assertNotIn(other_lots[0], text, "ほかのラインの行が混ざった")

        # ログ: <ログの出力パス>\nippou\ に、PC の名前の入った名前で別々に
        self.assertEqual(sorted(p.name for p in self.output.iterdir()), ["nippou"],
                         "出力パスの直下にはツールのフォルダだけ")
        names = self.log_names()
        for term in (l1, hvc, admin):
            with self.subTest(log=term.name):
                self.assertIn(f"nippou_動作記録_{term.pc}.log", names)
                self.assertTrue(any(n.startswith("nippou_出来事_") and n.endswith(f"_{term.pc}.jsonl")
                                    for n in names), names)
        self.assertTrue(all(n.startswith("nippou_") for n in names), names)
        self.assertNotIn("nippou.log", names, "端末の名前の無い前の形に書いた")
        # 出来事の記録は、書いた PC の行だけ(相手の行が混ざらない)
        for term in (l1, hvc, admin):
            with self.subTest(events=term.name):
                rows = [json.loads(line) for n in names
                        if n.startswith("nippou_出来事_") and n.endswith(f"_{term.pc}.jsonl")
                        for line in (self.output / "nippou" / n).read_text(encoding="utf-8").splitlines()
                        if line.strip()]
                self.assertTrue(rows)
                self.assertEqual({r.get("terminal") or r.get("pc") for r in rows}, {term.pc},
                                 rows[:3])


class TwoLinesL1Vbs_HvcExe(_TwoLines):
    """L-1 = vbs 起動(現場)、HVC = exe 起動。"""
    L1_VBS = True


class TwoLinesL1Exe_HvcVbs(_TwoLines):
    """その逆: L-1 = exe 起動、HVC = vbs 起動。"""
    L1_VBS = False


# ===========================================================================
# 2. 同じラインの2台(片方 vbs・片方 exe): ほかの端末でも入力されています
# ===========================================================================
@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class _OtherTerminal(_MixedCase):
    A_VBS = True

    def test_同じラインのvbsとexeで同じ直を打つと_両向きに知らせる(self) -> None:
        a_edition, b_edition = _editions(self.A_VBS)
        day, label = "2026-10-05", DAY
        self.at(f"{day} 07:05")
        a = self.term("PC-L1A", a_edition)
        b = self.term("PC-L1B", b_edition)
        self.assertEqual((a.line, b.line), ("L-1", "L-1"))
        a.start_shift("山田")
        b.start_shift("佐藤")
        self.at(f"{day} 09:00")
        self.assertFalse(_other(a.saved(rows_of(row("A01", "07:00", "09:00")), "山田"))["shown"])
        a.wait_backup()
        self.assertFalse(_other(a.shift_key())["shown"])

        # B(もう一方の起こし方)は保存する前に、A の保存を知らされる
        self.at(f"{day} 09:10")
        got = _other(b.shift_key())
        self.assertTrue(got["shown"], f"{b.name} に {a.name} の行があることを知らせていない")
        self.assertEqual((got["kind"], got["pc"], got["at"]), ("conflict", "PC-L1A", "09:00"))
        self.assertIn("この直は ほかの端末(PC-L1A)でも入力されています(09:00 に保存)", got["text"])

        # 止めはしない: B の保存は通り、控えは B の中身。A には B が保存したことを言う
        b.saved(rows_of(row("B01", "07:00", "09:10")), "佐藤")
        b.wait_backup()
        self.assertEqual(self.backup_lots("L-1").get((label, "1直", 1)), ["B01"])
        got = _other(b.shift_key())
        self.assertEqual((got["kind"], got["pc"]), ("replaced", "PC-L1A"))
        got = _other(a.shift_key())
        self.assertEqual((got["kind"], got["pc"], got["at"]), ("conflict", "PC-L1B", "09:10"),
                         f"{a.name} に {b.name} の保存を知らせていない")


class OtherTerminalVbsA_ExeB(_OtherTerminal):
    """先に打つ L-1 の1台目 = vbs、あとから打つ2台目 = exe。"""
    A_VBS = True


class OtherTerminalExeA_VbsB(_OtherTerminal):
    """その逆。"""
    A_VBS = False


# ===========================================================================
# 3. 直の切り替わり: 次の直の端末が控えから受け取る(vbs → exe・exe → vbs)
# ===========================================================================
@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class _Handover(_MixedCase):
    FIRST_VBS = True

    def test_渡さないまま直が終わった_次の直の端末が控えから拾って送る(self) -> None:
        first, second = _editions(self.FIRST_VBS)
        a = self.term("PC-L1A", first)
        a.start_shift("山田")
        self.at("2026-10-05 14:30")
        a.saved(clean_shift_rows("1直", "A"), "山田")
        a.wait_backup()
        self.at("2026-10-05 14:50")
        a.stop()                                   # 直の終わりの前に閉じた(自動確定は走らない)

        self.at("2026-10-05 15:05")
        b = self.term("PC-L1B", second)
        self.assertEqual(b.dirty_keys(), {(DAY, "L-1", "1直", 1)}, "控えから受け取っていない")
        body = b.open_screen()
        self.assertEqual(body["shift"], "2直")
        band = _handover(body)
        self.assertTrue(band["blocked"])
        self.assertIn(f"{DAY} L-1 1直", band["message"])
        tick = b.close_tick()
        self.assertEqual([(s["report_date"], s["shift"]) for s in tick["swept"]], [(DAY, "1直")])
        self.assertEqual(self.shared_lots("L-1"), {(DAY, "1直", 1): ["A01", "A02"]})
        self.assertFalse(_handover(b.choose_worker("佐藤")).get("any_left"))
        b.wait_backup()
        self.assertEqual([(r["直"], r["結果"], r["通し方"]) for r in self.shared_history()],
                         [("1直", "送れた", "次の直の画面が拾って送った")])
        self.assertEqual([r["端末"] for r in self.shared_history()], ["PC-L1B"],
                         "送ったのは次の直の端末")

        # 翌朝、最初の端末を起こし直す(起こし方はそのまま): 送り済みの1直で止めない
        self.at("2026-10-06 07:05")
        a.start()
        self.assertEqual(a.dirty_keys(), set(), "次の直の端末が送った1直が、まだ未送信のまま")
        body = a.open_screen()
        self.assertEqual((body["report_date"], body["shift"]), (NEXT_DAY, "1直"))
        self.assertFalse(_handover(body).get("any_left"), _handover(body).get("message"))
        self.assertEqual(a.close_tick()["swept"], [])
        self.assertEqual(len(self.shared_history()), 1)


class HandoverVbsToExe(_Handover):
    FIRST_VBS = True


class HandoverExeToVbs(_Handover):
    FIRST_VBS = False


# ===========================================================================
# 4. 多重起動の守り
# ===========================================================================
@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class GuardTests(_MixedCase):
    def test_同じPCでvbsが先なら_exeは理由を言って止まる(self) -> None:
        pc = self.pc("PC-L1A")
        vbs = VbsRun(pc, self.dist)
        self.addCleanup(vbs.stop)
        vbs.wait_ready()
        exe = ExeRun(pc, self.dist)
        self.addCleanup(exe.stop)
        self.assertEqual(exe.first.get("event"), "fatal", exe.first)
        self.assertIn("ブラウザ版が動いています", exe.first.get("message", ""))
        self.assertIn("ブラウザの画面の「終了」", exe.first.get("hint", ""))
        exe.proc.wait(timeout=WAIT)
        self.assertEqual(exe.proc.returncode, 1)
        self.assertTrue(vbs.get("/api/health")["ready"], "先に動いていた vbs は動き続ける")
        # 外枠の失敗の画面の「ログの場所」は、起動の記録を実際に書いたフォルダ
        log_dir = Path(exe.first.get("log_dir", ""))
        self.assertTrue(any(log_dir.glob("nippou_起動_PC-L1A.log")), exe.first)
        text = (log_dir / "nippou_起動_PC-L1A.log").read_text(encoding="utf-8")
        self.assertIn("ブラウザ版が動いています", text)

    def test_同じPCでexeが先なら_vbsは止まり_理由を画面と記録に残す(self) -> None:
        pc = self.pc("PC-L1A")
        exe = ExeRun(pc, self.dist)
        self.addCleanup(exe.stop)
        self.assertEqual(exe.first.get("event"), "started", exe.first)
        exe.wait_ready()
        vbs = VbsRun(pc, self.dist, wait=False)
        self.addCleanup(vbs.stop)
        self.assertEqual(vbs.proc.wait(timeout=WAIT), 1, vbs.crash())
        # pythonw なので画面(ブラウザ)に案内を出す。待ち受けの画面は開かない
        urls = vbs.opened_urls()
        self.assertEqual(len(urls), 1, urls)
        self.assertTrue(urls[0].startswith("file:"), urls)
        page = Path(urllib.request.url2pathname(urllib.parse.urlparse(urls[0]).path))
        self.assertIn(pc.local_root.resolve(), page.resolve().parents,
                      "案内をこの PC の手元の領域の外に書いた")
        html = page.read_text(encoding="utf-8")
        self.assertIn("デスクトップ版(日報複合ツールの窓)で動いています", html)
        self.assertIn("日報複合ツールの窓の「日報」のタブ", html)
        # 起動の記録にも同じ理由(exe が配布設定を読んだので、ログの出力パスの nippou フォルダ)
        launch = self.output / "nippou" / "nippou_起動_PC-L1A.log"
        self.assertIn("デスクトップ版が動いているので、ブラウザ版は起動しません",
                      launch.read_text(encoding="utf-8"))
        self.assertTrue(exe.get("/api/health")["ready"], "先に動いていた exe は動き続ける")
        # vbs は印(ロック)を残していかない ── exe を閉じたあと vbs で開ける
        self.assertFalse((pc.local_root / "runtime" / "nippou.lock").exists())
        exe.stop()
        again = VbsRun(pc, self.dist)
        self.addCleanup(again.stop)
        again.wait_ready()

    def test_違うPCならvbsとexeが両方動く(self) -> None:
        runs: list[_Run] = []
        for pc_name, edition in (("PC-L1A", "vbs"), ("PC-HVC", "exe"), ("PC-L1B", "exe"),
                                 ("PC-TOT", "vbs")):
            pc = self.pc(pc_name)
            run = VbsRun(pc, self.dist) if edition == "vbs" else ExeRun(pc, self.dist)
            self.addCleanup(run.stop)
            if isinstance(run, ExeRun):
                self.assertEqual(run.first.get("event"), "started", run.first)
            runs.append(run)
        for run in runs:
            run.wait_ready()
        for run in runs:
            with self.subTest(pc=run.pc.name, edition=run.edition):
                health = run.get("/api/health")
                self.assertTrue(health["ready"])
                self.assertEqual(health["pid"], run.proc.pid)
                self.assertEqual(run.get("/api/shift/key")["line"],
                                 {"PC-L1A": "L-1", "PC-L1B": "L-1", "PC-HVC": "HVC",
                                  "PC-TOT": "トット"}[run.pc.pc_name])
        # 錠はそれぞれの PC の手元の領域(共有には置かない)
        self.assertFalse([p for p in self.shared.rglob("*.lock")])


# ===========================================================================
# 5. 配布設定の読み方: 一式の中の、ツールのフォルダの設定.json・まとめたファイル
# ===========================================================================
@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class DistributionTests(_MixedCase):
    def setUp(self) -> None:
        super().setUp()
        self.kit = self.base / "日報複合ツール"
        (self.kit / "config").mkdir(parents=True)
        shutil.copy2(REPO / "config" / "tools.json", self.kit / "config" / "tools.json")
        self.tool = self.kit / "tools" / "nippou"
        shutil.copytree(ROOT, self.tool, ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", "tests", "配布設定", "配布設定.作成中"))
        self.other_ref = self.share_dir("別の参照")

    def share_dir(self, name: str) -> Path:
        path = self.shared / name
        path.mkdir(parents=True)
        return path

    def write_kit(self, settings: dict, created_at: str) -> None:
        target = self.kit / "配布設定"
        target.mkdir(exist_ok=True)
        (target / "日報複合ツール.json").write_text(json.dumps({
            "format": 1, "created_at": created_at, "tools": {"nippou": {
                "format": 1, "app_id": "nlm.nippou-tool", "created_at": created_at,
                "created_on": "PC-ADMIN", "settings": settings}}},
            ensure_ascii=False), encoding="utf-8")

    def read_by_both(self, tag: str) -> dict[str, dict[str, str]]:
        """vbs と exe(それぞれ初めて起こす PC)が読んだ置き場所。"""
        out: dict[str, dict[str, str]] = {}
        for edition in ("vbs", "exe"):
            pc = self.pc(f"PC-{tag}-{edition}", tool_dir=self.tool)
            # 一式の中のツールのフォルダの 配布設定\ をそのまま読ませる(差し替えない)
            run: _Run = VbsRun(pc, None) if edition == "vbs" else ExeRun(pc, None)
            self.addCleanup(run.stop)
            if isinstance(run, ExeRun):
                self.assertEqual(run.first.get("event"), "started", run.first)
            run.wait_ready()
            paths = run.get("/api/settings/state")["path_map"]
            out[edition] = {k: paths[k]["resolved"] for k in ("access_dir", "gw_reference_dir")}
            run.stop()
        return out

    def assert_both(self, got: dict[str, dict[str, str]], ref: Path) -> None:
        for edition in ("vbs", "exe"):
            with self.subTest(edition=edition):
                self.assertTrue(same_path(got[edition]["gw_reference_dir"], ref), (edition, got[edition]))
                self.assertTrue(same_path(got[edition]["access_dir"], self.shared), (edition, got[edition]))

    def test_ツールのフォルダの設定json_だけのとき(self) -> None:
        write_distribution(self.tool / "配布設定", self.dist_settings())
        self.assert_both(self.read_by_both("own"), self.ref)

    def test_まとめたファイルだけのとき(self) -> None:
        self.write_kit(dict(self.dist_settings(), gw_reference_dir=str(self.other_ref)),
                       "2026-10-20 08:00")
        self.assert_both(self.read_by_both("kit"), self.other_ref)

    def test_両方あるときは作った時刻が新しいほう_どちらの起こし方でも同じ(self) -> None:
        write_distribution(self.tool / "配布設定", self.dist_settings(), "2026-10-20 08:00:00")
        self.write_kit(dict(self.dist_settings(), gw_reference_dir=str(self.other_ref)),
                       "2026-10-21 08:00")
        self.assert_both(self.read_by_both("newkit"), self.other_ref)
        write_distribution(self.tool / "配布設定", self.dist_settings(), "2026-10-22 08:00:00")
        self.assert_both(self.read_by_both("newown"), self.ref)


# ===========================================================================
# 6. 版が違う2台(片方だけ古い版)
# ===========================================================================
@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class VersionMixTests(_MixedCase):
    def test_版の違うvbsとexeが同じ共有を使っても_それぞれの版で動き_記録が分かれる(self) -> None:
        old_tool = self.base / "古い一式" / "tools" / "nippou"
        shutil.copytree(ROOT, old_tool, ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", "tests", "配布設定", "配布設定.作成中"))
        old = Term(self, self.pc("PC-L1A", tool_dir=old_tool, version="4.40.0"), "vbs")
        new = self.term("PC-HVC", "exe")
        self.assertEqual(old.get("/api/health")["version"], "4.40.0")
        self.assertEqual(new.get("/api/health")["version"], new.pc_obj.version)
        self.assertTrue(same_path(old.get("/api/health")["app_root"], old_tool))
        self.assertTrue(same_path(new.get("/api/health")["app_root"], ROOT))
        old.start_shift("山田")
        new.start_shift("田中")
        self.at("2026-10-05 14:30")
        old.saved(clean_shift_rows("1直", "A"), "山田")
        new.saved(clean_shift_rows("1直", "H"), "田中")
        for term, worker in ((old, "山田"), (new, "田中")):
            status, body = term.push_now(worker=worker)
            self.assertEqual(status, 200, f"{term.name}: {body}")
        self.assertEqual(self.shared_lots("L-1"), {(DAY, "1直", 1): ["A01", "A02"]})
        self.assertEqual(self.shared_lots("HVC"), {(DAY, "1直", 1): ["H01", "H02"]})
        # 出来事の記録は PC ごとのファイルで、それぞれの版で書く
        names = self.log_names()
        for term, version in ((old, "4.40.0"), (new, new.pc_obj.version)):
            with self.subTest(pc=term.pc):
                files = [n for n in names if n.startswith("nippou_出来事_") and n.endswith(f"_{term.pc}.jsonl")]
                self.assertTrue(files, names)
                rows = [json.loads(line) for n in files
                        for line in (self.output / "nippou" / n).read_text(encoding="utf-8").splitlines()
                        if line.strip()]
                self.assertIn(f"起動しました(版 {version})", [r.get("message") for r in rows])


# 土台の組(起こし方を決めていない)は流さない。流すのは起こし方を決めた組だけ
del _TwoLines, _OtherTerminal, _Handover


if __name__ == "__main__":
    unittest.main()
