"""exe 起動と vbs 起動が**混ざった現場**の点検表(PC ごとに別の手元の領域・同じ共有フォルダ)

【現場】PC によって起こし方が違う。
- **exe 起動**: 日報複合ツールの窓(Tauri の外枠)が ``bridge.py`` を子として起こし、標準入出力で
  要求を渡す(ポートで待ち受けない)。
- **vbs 起動**: ``Start.vbs`` → ``pythonw start_app.py``(コンソール無し・引数無し)→ ポートで待ち受け、
  ブラウザが開く。

どちらの PC も同じ共有フォルダの点検表を探し、プレビューし、印刷する。ログの出力先を共有に
すると、全ラインのログが ``<出力先>\\inspection\\inspection_…_<PC>_<利用者>`` に集まる。

【まね方】
- **PC 1台 = 手元の領域1つ**: ``%LOCALAPPDATA%``(``<LOCALAPPDATA>\\InspectionSheetPrint``)・
  ``COMPUTERNAME``・利用者名(``USERNAME`` など)。試験の ``INSPECTION_LOCAL_DIR`` は子に渡さない
  (本物と同じ道で場所を決めさせる)。
- **Excel / Word は模擬**(``excel.backend = dummy``。既存の試験と同じ。Linux に Excel は無い)。
  点検表のファイルは中身の無い本物のファイルを共有フォルダに置く。
- **vbs 起動**: WSH は Linux で動かないので、``Start.vbs`` がすること(作業フォルダ = ツールの
  フォルダ・``pythonw start_app.py``)をまねる。pythonw のまねとして標準出力・標準エラーを ``None``
  にし、ブラウザを開く所だけ「開いた宛先を書き留める」に替える。
- **exe 起動**: 外枠と同じ道(作業フォルダ・``INSPECTION_TOKEN``・``INSPECTION_DEMO=0``・
  ``PYTHONIOENCODING=utf-8``・``ALLTOOLS_SHELL``)で ``bridge.py`` を起こし、外枠の代わりに要求を渡す。
- ポートは空いているものを取る(PC ごとの ``config/app.json`` の写し)。

**時間には頼らず、状態を待つ**。パスは文字列で比べず、解いた値で比べる。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from datetime import date
from pathlib import Path
from typing import Any, Callable, Optional

import tests.helpers  # noqa: F401  (この試験のプロセス自身は一時フォルダへ)

import bridge as bridge_module

ROOT = Path(__file__).resolve().parent.parent       # tools/inspection
REPO = ROOT.parents[1]

WAIT = 90.0

#: 共有フォルダの点検表(カテゴリ > サブカテゴリ > カテゴリ_サブカテゴリ_名前)
FILES = ("梱包/日常点検/梱包_日常点検_台車.xlsx",
         "梱包/日常点検/梱包_日常点検_コンベア.xlsx",
         "押出/設備点検/押出_設備点検_プレス機.xlsm",
         "押出/設備点検/押出_設備点検_手順書.docx")

_DROP = ("INSPECTION_LOCAL_DIR", "INSPECTION_DISTRIBUTION_DIR", "INSPECTION_APP_CONFIG",
         "INSPECTION_BUSINESS_CONFIG", "ALLTOOLS_DISTRIBUTION_DIR", "PYTHONUTF8", "INSPECTION_TOKEN",
         "INSPECTION_DEMO", "XDG_DATA_HOME")

_VBS_CHILD = r"""
import os, runpy, sys, webbrowser

def _opened(url, *args, **kwargs):
    with open(os.environ["FLOW_OPENED"], "a", encoding="utf-8") as fh:
        fh.write(url + "\n")
    return True

webbrowser.open = _opened
sys.stdout = None          # pythonw.exe にはコンソールが無い
sys.stderr = None
script = os.path.join(os.getcwd(), "start_app.py")
sys.argv = [script]
runpy.run_path(script, run_name="__main__")
"""


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_for(ok: Callable[[], Any], what: str, timeout: float = WAIT) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = ok()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"{what} になりません")
        time.sleep(0.1)


def same_path(a: Any, b: Any) -> bool:
    return Path(str(a)).resolve() == Path(str(b)).resolve()


def tree(folder: Path) -> list[tuple[str, int, int]]:
    """フォルダの中身(相対パス・大きさ・更新時刻)。書き込みが無いことを確かめる。"""
    out = []
    for p in sorted(folder.rglob("*")):
        st = p.stat()
        out.append((str(p.relative_to(folder)), st.st_size if p.is_file() else -1, st.st_mtime_ns))
    return out


def write_distribution(folder: Path, settings: dict, created_at: str = "2026-10-20 08:00:00") -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "設定.json").write_text(json.dumps({
        "format": 1, "app_id": "nlm.inspection-sheet-print", "created_at": created_at,
        "created_on": "PC-ADMIN", "settings": settings}, ensure_ascii=False), encoding="utf-8")


# ===========================================================================
# PC 1台
# ===========================================================================
class Pc:
    def __init__(self, base: Path, name: str, *, tool_dir: Path = ROOT, version: str = "") -> None:
        self.name = name
        self.pc_name = f"PC-{name}"
        self.login = f"user{name.lower()}"
        self.owner = f"{self.pc_name}_{self.login}"
        self.tool_dir = tool_dir
        self.dir = base / name
        self.local_app_data = self.dir / "AppData" / "Local"
        self.local_app_data.mkdir(parents=True, exist_ok=True)
        conf = json.loads((tool_dir / "config" / "app.json").read_text(encoding="utf-8"))
        conf["server"]["port"] = free_port()
        conf["server"]["port_retry"] = 0
        if version:
            conf["version"] = version
        self.version = conf["version"]
        self.app_json = self.dir / "app.json"
        self.app_json.write_text(json.dumps(conf, ensure_ascii=False), encoding="utf-8")
        # 業務の設定(config/inspection.json)の写し: Excel は模擬、既定の点検表フォルダは無い場所
        # (点検表フォルダは配布設定で配る)
        business = json.loads((tool_dir / "config" / "inspection.json").read_text(encoding="utf-8-sig"))
        business["root_folder"] = str(self.dir / "既定の点検表フォルダ(無い)")
        business.setdefault("excel", {})["backend"] = "dummy"
        self.business_json = self.dir / "inspection.json"
        self.business_json.write_text(json.dumps(business, ensure_ascii=False), encoding="utf-8")

    @property
    def local_root(self) -> Path:
        return self.local_app_data / "InspectionSheetPrint"

    def env(self, distribution: Optional[Path]) -> dict:
        env = {k: v for k, v in os.environ.items() if k not in _DROP}
        env.update({
            "LOCALAPPDATA": str(self.local_app_data),
            "COMPUTERNAME": self.pc_name,
            "USERNAME": self.login, "LOGNAME": self.login, "USER": self.login,
            "INSPECTION_APP_CONFIG": str(self.app_json),
            "INSPECTION_BUSINESS_CONFIG": str(self.business_json),
            "PYTHONIOENCODING": "utf-8",
        })
        if distribution is not None:
            env["INSPECTION_DISTRIBUTION_DIR"] = str(distribution)
        return env

    def vbs(self, case: unittest.TestCase, distribution: Optional[Path] = None, *, wait: bool = True) -> "VbsRun":
        run = VbsRun(self, distribution, wait=wait)
        case.addCleanup(run.stop)
        return run

    def exe(self, case: unittest.TestCase, distribution: Optional[Path] = None) -> "ExeRun":
        run = ExeRun(self, distribution)
        case.addCleanup(run.stop)
        return run


class _Run:
    edition = ""
    pc: Pc
    screen = ""

    def get(self, path: str) -> Any:
        status, body = self.call("GET", path)
        if status != 200:
            raise AssertionError(f"{self.pc.name}({self.edition}) GET {path}: {status} {body}")
        return body

    def post(self, path: str, body: Any) -> tuple[int, Any]:
        return self.call("POST", path, body)

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:   # pragma: no cover
        raise NotImplementedError

    def wait_ready(self) -> None:
        wait_for(lambda: self.get("/api/health").get("ready"), f"{self.pc.name}({self.edition}) の準備完了")

    def items(self) -> dict[str, dict]:
        """一覧の点検表(ファイル名 → 点検表)。調べ終わるまで待つ。"""
        def done() -> Optional[dict]:
            body = self.get("/api/inventory?wait=2")
            if body["scan"].get("running") or body["inventory"] is None:
                return None
            return body
        body = wait_for(done, f"{self.pc.name}({self.edition}) の一覧")
        inv = body["inventory"]
        return {it["file_name"]: it for c in inv["categories"] for s in c["subcategories"] for it in s["items"]}

    def print_and_wait(self, ids: list[str]) -> dict:
        status, body = self.post("/api/print", {"ids": ids, "copies": 1})
        if status != 202:
            raise AssertionError(f"{self.pc.name}({self.edition}) 印刷: {status} {body}")
        ref = body["job"]["ref"] if isinstance(body.get("job"), dict) else ""

        def finished() -> Optional[dict]:
            job = self.get("/api/print/status")["job"]
            return job if job and job.get("state") not in ("queued", "running") else None
        job = wait_for(finished, f"{self.pc.name}({self.edition}) の印刷")
        job.setdefault("ref", ref)
        return job


class VbsRun(_Run):
    edition = "vbs"

    def __init__(self, pc: Pc, distribution: Optional[Path], *, wait: bool) -> None:
        self.pc = pc
        self.opened = pc.dir / f"opened-{uuid.uuid4().hex[:6]}.txt"
        env = pc.env(distribution)
        env["FLOW_OPENED"] = str(self.opened)
        self.proc = subprocess.Popen([sys.executable, "-c", _VBS_CHILD], cwd=str(pc.tool_dir), env=env,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        self.url = self.base = self.token = ""
        # 画面の番号(ブラウザの画面が付けてくる)。要求が来ていれば「誰かが見ている」
        self.screen = uuid.uuid4().hex
        if wait:
            self.wait_opened()

    def wait_opened(self) -> str:
        def ready() -> Optional[str]:
            try:
                urls = [u for u in self.opened.read_text(encoding="utf-8").splitlines() if u.strip()]
            except OSError:
                urls = []
            if urls:
                return urls[-1]
            if self.proc.poll() is not None:
                raise AssertionError(f"{self.pc.name} の vbs 起動が先に終わりました(終了コード "
                                     f"{self.proc.returncode})\n{self.log_text()[-3000:]}")
            return None
        self.url = wait_for(ready, f"{self.pc.name} の vbs 起動がブラウザを開く")
        head, _, query = self.url.partition("?")
        self.base = head.rstrip("/")
        self.token = dict(p.split("=", 1) for p in query.split("&") if "=" in p).get("t", "")
        return self.url

    def log_text(self) -> str:
        folder = self.pc.local_root / "logs" / "inspection"
        return "".join(p.read_text(encoding="utf-8", errors="replace")
                       for p in sorted(folder.glob("*.log"))) if folder.is_dir() else ""

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(f"{self.base}{path}", data=data, method=method, headers={
            "X-Tool-Token": self.token, "Content-Type": "application/json", "X-Screen-Id": self.screen})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw, status = response.read(), response.status
        except urllib.error.HTTPError as exc:
            raw, status = exc.read(), exc.code
        try:
            return status, json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            return status, raw.decode("utf-8", "replace")

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

    def __init__(self, pc: Pc, distribution: Optional[Path]) -> None:
        self.pc = pc
        self.token = f"tok-{uuid.uuid4().hex[:8]}"
        self.screen = uuid.uuid4().hex
        env = pc.env(distribution)
        env.update({"INSPECTION_TOKEN": self.token, "INSPECTION_DEMO": "0", "ALLTOOLS_SHELL": "1",
                    "ALLTOOLS_TOOL": "inspection", "ALLTOOLS_ROOT": str(pc.tool_dir.parents[1])})
        self.proc = subprocess.Popen([sys.executable, str(pc.tool_dir / "bridge.py")], cwd=str(pc.tool_dir),
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
                            ["Content-Type", "application/json"], ["X-Screen-Id", self.screen]]}
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(head, ensure_ascii=False).encode("utf-8") + b"\n" + data)
        self.proc.stdin.flush()
        deadline = time.monotonic() + WAIT
        with self.cond:
            while rid not in self.replies and time.monotonic() < deadline:
                self.cond.wait(0.2)
            if rid not in self.replies:
                raise AssertionError(f"{self.pc.name}(exe) {method} {path} に答えません\n" + "\n".join(self.err[-20:]))
            reply, raw = self.replies.pop(rid)
        try:
            return reply["status"], json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            return reply["status"], raw.decode("utf-8", "replace")

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


class _MixedCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="isp_mixed_")
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.share = self.base / "共有"
        self.root = self.share / "一括管理_点検表"
        for rel in FILES:
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(rel.encode("utf-8"))
        self.output = self.share / "ログ出力"
        self.output.mkdir()
        # 配布設定(アプリを共有フォルダに置いて配った形: どの PC も同じ 配布設定\ を見る)
        self.dist = self.share / "アプリ" / "配布設定"
        write_distribution(self.dist, {"root_folder": str(self.root), "log_dir": str(self.output)})


# ===========================================================================
# 2. 同じ共有フォルダを見る vbs と exe の2台
# ===========================================================================
class _TwoPcs(_MixedCase):
    A_VBS = True

    def setUp(self) -> None:
        super().setUp()
        self.pc_a = Pc(self.base, "A")
        self.pc_b = Pc(self.base, "B")
        before = tree(self.root)
        if self.A_VBS:
            self.a, self.b = self.pc_a.vbs(self, self.dist), self.pc_b.exe(self, self.dist)
        else:
            self.a, self.b = self.pc_a.exe(self, self.dist), self.pc_b.vbs(self, self.dist)
        for run in (self.a, self.b):
            if isinstance(run, ExeRun):
                self.assertEqual(run.first.get("event"), "started", run.first)
            run.wait_ready()
        self.root_before = before

    def test_探す_プレビュー_印刷_記録がぶつからない(self) -> None:
        runs = (self.a, self.b)
        # 配布設定の点検表フォルダ・ログの出力先を、どちらの起こし方でも読んでいる
        for run in runs:
            with self.subTest(pc=run.pc.name, edition=run.edition):
                diag = run.get("/api/diagnostics")
                self.assertEqual(diag["app"]["edition"], "browser" if run.edition == "vbs" else "desktop")
                self.assertTrue(same_path(diag["settings"]["root_folder"], self.root), diag["settings"])
                logs = diag["paths"]["logs"]
                self.assertFalse(logs["is_local"], logs)
                self.assertTrue(same_path(logs["dir"], self.output / "inspection"), logs)
                self.assertEqual(logs["owner"], run.pc.owner)
                self.assertTrue(same_path(diag["paths"]["local_root"], run.pc.local_root))
        # 探す: 同じ一覧
        lists = [run.items() for run in runs]
        self.assertEqual(sorted(lists[0]), sorted(lists[1]))
        self.assertEqual(len(lists[0]), len(FILES))

        # プレビュー: 2台が同じ点検表をほぼ同時に
        names = ["梱包_日常点検_台車.xlsx", "押出_設備点検_手順書.docx"]
        results: dict[str, Any] = {}

        def preview(run: _Run, items: dict) -> None:
            results[run.pc.name] = [run.post("/api/preview", {"id": items[n]["id"]}) for n in names]
        threads = [threading.Thread(target=preview, args=(run, items)) for run, items in zip(runs, lists)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(WAIT)
        for run in runs:
            with self.subTest(pc=run.pc.name):
                for status, body in results[run.pc.name]:
                    self.assertEqual(status, 200, body)
                    self.assertTrue(body["ok"], body)
                # 写しは**その PC の手元**(共有には置かない)
                cache = run.pc.local_root / "cache"
                self.assertTrue(any(cache.rglob("*")), f"{run.pc.name} の控えがありません")

        # 印刷: 2台がほぼ同時に、同じ点検表を
        jobs: dict[str, dict] = {}

        def do_print(run: _Run, items: dict) -> None:
            jobs[run.pc.name] = run.print_and_wait([items[n]["id"] for n in sorted(items)])
        threads = [threading.Thread(target=do_print, args=(run, items)) for run, items in zip(runs, lists)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(WAIT * 2)
        for run in runs:
            with self.subTest(pc=run.pc.name):
                job = jobs[run.pc.name]
                self.assertEqual(job["state"], "done", job)
                self.assertEqual([i["status"] for i in job["items"]], ["ok"] * len(FILES), job)

        # 共有の点検表のフォルダには何も書かない(ロック・控え・一時ファイル)
        self.assertEqual(tree(self.root), self.root_before)

        # ログ: <出力先>\inspection\ に、PC と利用者の入った名前で別々に
        folder = self.output / "inspection"
        self.assertEqual(sorted(p.name for p in self.output.iterdir()), ["inspection"],
                         "出力先の直下にはツールのフォルダだけ")
        today = f"{date.today():%Y%m%d}"
        files = sorted(p.name for p in folder.iterdir())
        for run in runs:
            with self.subTest(pc=run.pc.name):
                self.assertIn(f"inspection_{today}_{run.pc.owner}.log", files)
                self.assertIn(f"inspection_events_{today}_{run.pc.owner}.jsonl", files)
        self.assertTrue(all(n.startswith("inspection_") and (self.pc_a.owner in n or self.pc_b.owner in n)
                            for n in files), files)
        # 出来事の記録は、書いた PC の行だけ(相手の行が混ざらない)
        for run in runs:
            with self.subTest(pc=run.pc.name):
                path = folder / f"inspection_events_{today}_{run.pc.owner}.jsonl"
                rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
                self.assertEqual({r.get("pc") for r in rows}, {run.pc.pc_name}, path.name)
                printed = [r for r in rows if r.get("op") == "print.item" and r.get("result") == "ok"]
                self.assertEqual(len(printed), len(FILES), "1件ずつの印刷の記録")
                log = (folder / f"inspection_{today}_{run.pc.owner}.log").read_text(encoding="utf-8")
                other = self.pc_b if run.pc is self.pc_a else self.pc_a
                self.assertNotIn(other.pc_name, log, "相手の PC の記録が混ざっている")

        # 問い合わせ番号で引く: 相手の PC で出た番号も、共有のログから引ける(どちらの起こし方でも)
        for run, other in ((self.a, self.b), (self.b, self.a)):
            with self.subTest(find_from=run.pc.name):
                ref = jobs[other.pc.name]["ref"]
                self.assertTrue(ref)
                found = run.get(f"/api/logs/find?ref={ref}")
                rows = found.get("rows") or found.get("entries") or found.get("events") or []
                self.assertTrue(rows, found)
                self.assertEqual({r.get("pc") for r in rows}, {other.pc.pc_name}, rows)


class TwoPcsVbsA_ExeB(_TwoPcs):
    """PC A = vbs 起動、PC B = exe 起動。"""
    A_VBS = True


class TwoPcsExeA_VbsB(_TwoPcs):
    """その逆: PC A = exe 起動、PC B = vbs 起動。"""
    A_VBS = False


# ===========================================================================
# 3. 多重起動の守り
# ===========================================================================
class GuardTests(_MixedCase):
    def test_同じPCでvbsが先なら_exeは理由を言って止まる(self) -> None:
        pc = Pc(self.base, "X")
        vbs = pc.vbs(self, self.dist)
        vbs.wait_ready()
        exe = pc.exe(self, self.dist)
        self.assertEqual(exe.first.get("event"), "fatal", exe.first)
        self.assertIn("ブラウザ版が動いています", exe.first.get("message", ""))
        exe.proc.wait(timeout=WAIT)
        self.assertTrue(vbs.get("/api/health")["ready"], "先に動いていた vbs は動き続ける")

    def test_同じPCでexeが先なら_vbsは止まり_理由が記録に残る(self) -> None:
        pc = Pc(self.base, "X")
        exe = pc.exe(self, self.dist)
        self.assertEqual(exe.first.get("event"), "started", exe.first)
        exe.wait_ready()
        vbs = pc.vbs(self, self.dist, wait=False)
        self.assertEqual(vbs.proc.wait(timeout=WAIT), 1)
        # Windows では窓(MessageBox)で知らせる。記録には同じ理由が残る
        events = (self.output / "inspection")
        text = "".join(p.read_text(encoding="utf-8") for p in events.glob(f"inspection_events_*_{pc.owner}.jsonl"))
        self.assertIn("デスクトップ版(日報複合ツールの窓)で動いています", text)
        self.assertTrue(exe.get("/api/health")["ready"], "先に動いていた exe は動き続ける")

    def test_違うPCならvbsとexeが両方動く(self) -> None:
        runs = [Pc(self.base, "X").vbs(self, self.dist), Pc(self.base, "Y").exe(self, self.dist),
                Pc(self.base, "Z").exe(self, self.dist), Pc(self.base, "W").vbs(self, self.dist)]
        for run in runs:
            if isinstance(run, ExeRun):
                self.assertEqual(run.first.get("event"), "started", run.first)
            run.wait_ready()
        for run in runs:
            self.assertTrue(run.get("/api/health")["ready"])
        # 錠はそれぞれの PC の手元の領域(共有には置かない)
        self.assertFalse([p for p in self.share.rglob("*.lock")])


# ===========================================================================
# 3. ログの片付けは自分の分だけ(版の違う PC が同じ出力先を使っていても)
# ===========================================================================
class SharedLogPruneTests(_MixedCase):
    def test_古いログの片付けは自分のPCの分だけ_相手と前の版の名前には触らない(self) -> None:
        pc_a, pc_b = Pc(self.base, "A"), Pc(self.base, "B")
        folder = self.output / "inspection"
        folder.mkdir()
        old_a = folder / f"inspection_20200101_{pc_a.owner}.log"
        old_b = folder / f"inspection_20200101_{pc_b.owner}.log"
        legacy_b = self.output / f"inspection_20200101_{pc_b.owner}.log"     # VER2.4.5 までの置き場所
        other_tool = self.output / "calendar_20200101.log"
        for path in (old_a, old_b, legacy_b, other_tool):
            path.write_text("old", encoding="utf-8")
        vbs = pc_a.vbs(self, self.dist)
        vbs.wait_ready()
        wait_for(lambda: not old_a.exists(), "A の古いログが片付く")
        exe = pc_b.exe(self, self.dist)
        self.assertEqual(exe.first.get("event"), "started", exe.first)
        exe.wait_ready()
        wait_for(lambda: not old_b.exists() and not legacy_b.exists(), "B の古いログが片付く")
        self.assertTrue(other_tool.exists(), "ほかのツールのログには触らない")


# ===========================================================================
# 3. 配布設定の読み方: 一式の中で、まとめたファイル・ツールのフォルダの設定.json
# ===========================================================================
class DistributionTests(_MixedCase):
    def setUp(self) -> None:
        super().setUp()
        self.kit = self.base / "日報複合ツール"
        (self.kit / "config").mkdir(parents=True)
        shutil.copy2(REPO / "config" / "tools.json", self.kit / "config" / "tools.json")
        self.tool = self.kit / "tools" / "inspection"
        shutil.copytree(ROOT, self.tool, ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", "tests", "配布設定", "配布設定.作成中"))
        self.other_root = self.share / "別の点検表"
        (self.other_root / "品質" / "検査").mkdir(parents=True)
        (self.other_root / "品質" / "検査" / "品質_検査_外観.xlsx").write_bytes(b"x")

    def write_kit(self, folder: Path, created_at: str) -> None:
        target = self.kit / "配布設定"
        target.mkdir(exist_ok=True)
        (target / "日報複合ツール.json").write_text(json.dumps({
            "format": 1, "created_at": created_at, "tools": {"inspection": {
                "format": 1, "app_id": "nlm.inspection-sheet-print", "created_at": created_at,
                "created_on": "PC-ADMIN", "settings": {"root_folder": str(folder)}}}},
            ensure_ascii=False), encoding="utf-8")

    def read_by_both(self, tag: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for edition in ("vbs", "exe"):
            pc = Pc(self.base, f"{tag}{edition}", tool_dir=self.tool)
            # 一式の中のツールのフォルダの 配布設定\ をそのまま読ませる(差し替えない)
            run = pc.vbs(self, None) if edition == "vbs" else pc.exe(self, None)
            if isinstance(run, ExeRun):
                self.assertEqual(run.first.get("event"), "started", run.first)
            run.wait_ready()
            out[edition] = run.get("/api/diagnostics")["settings"]["root_folder"]
            run.stop()
        return out

    def assert_both(self, got: dict[str, str], folder: Path) -> None:
        for edition in ("vbs", "exe"):
            with self.subTest(edition=edition):
                self.assertTrue(same_path(got[edition], folder), (edition, got[edition], folder))

    def test_ツールのフォルダの設定json_だけのとき(self) -> None:
        write_distribution(self.tool / "配布設定", {"root_folder": str(self.root)})
        self.assert_both(self.read_by_both("own"), self.root)

    def test_まとめたファイルだけのとき(self) -> None:
        self.write_kit(self.other_root, "2026-10-20 08:00")
        self.assert_both(self.read_by_both("kit"), self.other_root)

    def test_両方あるときは作った時刻が新しいほう_どちらの起こし方でも同じ(self) -> None:
        write_distribution(self.tool / "配布設定", {"root_folder": str(self.root)}, "2026-10-20 08:00:00")
        self.write_kit(self.other_root, "2026-10-21 08:00")
        self.assert_both(self.read_by_both("new_kit"), self.other_root)
        write_distribution(self.tool / "配布設定", {"root_folder": str(self.root)}, "2026-10-22 08:00:00")
        self.assert_both(self.read_by_both("new_own"), self.root)


# ===========================================================================
# 3. 版が違う2台(片方だけ古い版)
# ===========================================================================
class VersionMixTests(_MixedCase):
    def test_版の違うvbsとexeが同じ共有を使っても_それぞれの版で記録し刷れる(self) -> None:
        old_tool = self.base / "古い一式" / "tools" / "inspection"
        shutil.copytree(ROOT, old_tool, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests"))
        old, new = Pc(self.base, "OLD", tool_dir=old_tool, version="2.4.0"), Pc(self.base, "NEW")
        vbs, exe = old.vbs(self, self.dist), new.exe(self, self.dist)
        self.assertEqual(exe.first.get("event"), "started", exe.first)
        for run in (vbs, exe):
            run.wait_ready()
        self.assertEqual(vbs.get("/api/health")["version"], "2.4.0")
        self.assertEqual(exe.get("/api/health")["version"], new.version)
        for run in (vbs, exe):
            items = run.items()
            job = run.print_and_wait([items["梱包_日常点検_台車.xlsx"]["id"]])
            self.assertEqual(job["state"], "done", job)
        today = f"{date.today():%Y%m%d}"
        for pc, version in ((old, "2.4.0"), (new, new.version)):
            path = self.output / "inspection" / f"inspection_events_{today}_{pc.owner}.jsonl"
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual({r.get("ver") for r in rows}, {version}, path.name)


if __name__ == "__main__":
    unittest.main()
