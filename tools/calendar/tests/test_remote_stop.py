"""管理者以外停止モード(`remote_stop.py`。日報複合ツールの大設定から管理者が出すお願い)── カレンダーの受け方

日報複合ツールの大設定で、管理者(管理者パスワード かつ Administrator)が「ほかの PC を止める」を押すと、
共有DBの置き場所の `日報複合ツール_管理者以外停止\\お願い.json` にお願いが置かれる。単体で起動したカレンダーも同じフォルダを見る。

    何も残っていなければ        帯を見せてから、まもなく止まる
    残っているもの(同期・送信待ち・画面の保存していない入力)   利用者を待つ。期限(5分。試験は短く)で強制で止める。止める前に控えを書き、
                                次の起動で一度だけ知らせる
    お願いが出ているあいだ      起動しない(待機画面に理由を出して、しばらくして終わる)
    stop.bat                    止めずに理由を返して 3(ランチャーの「全 PC で止める」と同時に使わない)

本物の子プロセス(`start_app.py --no-browser`)で確かめる。時間は `ALLTOOLS_REMOTE_STOP_TIMES` で短くする。
「残っているもの」は試験用の `ALLTOOLS_REMOTE_STOP_TEST_LEFT` で作る(ツールの事情は下の受け方の試験)。
"""
from __future__ import annotations

import http.client
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

import remote_stop  # noqa: E402

LOCAL_ENV = "CALENDAR_LOCAL_DIR"
EXTRA_ARGS: list = []
EXTRA_ENV: dict = {"CALENDAR_HOME": "{tmp}/home"}
TIMES = "grace=4,notice=0.5,poll=0.2,flush=0.5,block=6,beat=1"


class _Child(unittest.TestCase):
    """本物の子プロセスを起こす土台。共有のフォルダ・手元の領域は一時フォルダ。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.share = self.tmp / "共有"
        self.share.mkdir()
        self.local = self.tmp / "local"
        self.left_file = self.tmp / "残っているもの.txt"
        self.env = dict(os.environ)
        for name in ("ALLTOOLS_PORTAL", "ALLTOOLS_SHELL", "ALLTOOLS_TOOL", "ALLTOOLS_ROOT"):
            self.env.pop(name, None)
        self.env.update({LOCAL_ENV: str(self.local), "ALLTOOLS_REMOTE_STOP_DIR": str(self.share),
                         "ALLTOOLS_PC_NAME": "PC-LINE1", "ALLTOOLS_LOGIN_ID": "genba",
                         "ALLTOOLS_REMOTE_STOP_TIMES": TIMES,
                         "ALLTOOLS_REMOTE_STOP_TEST_LEFT": str(self.left_file),
                         "PYTHONIOENCODING": "utf-8"})
        self.env.update({k: v.replace("{tmp}", str(self.tmp)) for k, v in EXTRA_ENV.items()})
        self.procs: list = []
        self.addCleanup(self._kill)
        saved = remote_stop.Setup(**vars(remote_stop.setup()))
        self.addCleanup(lambda: remote_stop.configure(**vars(saved)))
        remote_stop.configure(root=lambda: self.share, local_root=self.tmp / "管理者の手元", app="日報複合ツール")
        remote_stop.reset()
        self.addCleanup(remote_stop.reset)

    def _kill(self) -> None:
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait(10)
            for stream in (proc.stdout,):
                if stream:
                    stream.close()

    def start(self) -> subprocess.Popen:
        proc = subprocess.Popen([sys.executable, str(TOOL_DIR / "start_app.py"), "--no-browser", *EXTRA_ARGS],
                                cwd=str(TOOL_DIR), env=self.env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.procs.append(proc)
        return proc

    def port(self, proc) -> int:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                self.fail(f"起動しませんでした: {proc.stdout.read().decode('utf-8', 'replace')[-2000:]}")
            for path in (self.local / "runtime").glob("*.lock"):
                try:
                    info = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(info, dict) and int(info.get("port") or 0) and info.get("pid") == proc.pid:
                    return int(info["port"])
            time.sleep(0.1)
        self.fail("起動しませんでした(印が出ない)")

    def health(self, port: int, query: str = "") -> dict:
        # **こちらから閉じる**(keep-alive で受け取ってから閉じる)。サーバが先に閉じると、その番号が
        # しばらく TIME_WAIT で塞がり、続けて起こしたツールが「使えるポートがありません」になる
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request("GET", f"/api/health{query}")
            return json.loads(conn.getresponse().read().decode("utf-8"))
        finally:
            conn.close()

    def wait_ready(self, proc, port: int) -> None:
        """見張りが立つまで待つ(`/api/health` の `remote_stop` が出る)。"""
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                if self.health(port).get("remote_stop") is not None:
                    return
            except (OSError, ValueError):
                pass
            if proc.poll() is not None:
                self.fail("起動の途中で終わりました")
            time.sleep(0.2)
        self.fail("見張りが立ちません")

    def issue(self) -> None:
        with mock.patch.dict(os.environ, {"ALLTOOLS_PC_NAME": "PC-ADMIN", "ALLTOOLS_LOGIN_ID": "kanri"}), \
                mock.patch.object(remote_stop, "GRACE_SEC", 4.0):
            self.assertTrue(remote_stop.issue(True, True, "更新のため").ok)

    def clear(self) -> None:
        with mock.patch.dict(os.environ, {"ALLTOOLS_PC_NAME": "PC-ADMIN"}):
            self.assertTrue(remote_stop.clear(True, True).ok)

    def answer(self) -> dict:
        found = [json.loads(p.read_text(encoding="utf-8"))
                 for p in (self.share / remote_stop.FOLDER / "稼働中").glob("*.json")]
        mine = [a for a in found if a.get("pc") == "PC-LINE1"]
        self.assertEqual(len(mine), 1, found)
        return mine[0]


class RealProcessTests(_Child):
    def test_お願いで止まる_何も残っていなければまもなく(self) -> None:
        proc = self.start()
        port = self.port(proc)
        self.wait_ready(proc, port)
        self.issue()
        self.assertEqual(proc.wait(40), 0, "お願いで止まらない")
        answer = self.answer()
        self.assertEqual((answer["state"], answer["app"]), (remote_stop.STATE_STOPPED, "カレンダー"))

    def test_残っていれば待ち_期限で強制で止め_控えを次の起動で知らせる(self) -> None:
        self.left_file.write_text("試験の打ちかけ\n", encoding="utf-8")
        proc = self.start()
        port = self.port(proc)
        self.wait_ready(proc, port)
        self.issue()
        time.sleep(1.5)
        state = self.health(port, "?remote_stop=1&page=t")["remote_stop"]["state"]
        self.assertEqual(state["left"], ["試験の打ちかけ"], "帯に残っているものを出す")
        self.assertIsNone(proc.poll(), "残っているのに止まった")
        self.assertEqual(proc.wait(40), 0)
        answer = self.answer()
        self.assertEqual(answer["state"], remote_stop.STATE_FORCED)
        self.assertIn("試験の打ちかけ", answer["detail"])
        keeps = list((self.local / "data" / "停止控え").glob("keep-*.json"))
        self.assertEqual(len(keeps), 1, "この PC の手元の控え")
        self.assertFalse(any(self.share.rglob("keep-*")), "共有には書かない")

        # 解除して起動し直す → 一度だけ知らせる
        self.clear()
        self.left_file.unlink()
        again = self.start()
        port = self.port(again)
        self.wait_ready(again, port)
        notes = self.health(port, "?remote_stop=1&page=t")["remote_stop"]["notes"]
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("強制で止めました", notes[0])
        self.assertIn("試験の打ちかけ", notes[0])
        self.assertEqual(self.health(port, "?remote_stop=1&page=t")["remote_stop"]["notes"], [], "一度だけ")
        self.assertEqual(self.health(port)["remote_stop"]["notes"], [], "帯でない問い合わせには渡さない")

    def test_お願い中は起動せず理由を出して終わる_解除すれば起動できる(self) -> None:
        self.issue()
        proc = self.start()
        port = self.port(proc)
        deadline = time.monotonic() + 30
        why = ""
        while time.monotonic() < deadline and "管理者" not in why:
            try:
                why = str(self.health(port).get("startup_error") or "")
            except OSError:
                pass
            time.sleep(0.2)
        self.assertIn("更新が終わるまで起動できません", why)
        self.assertIn("別の名前に変えると解除できます", why)
        self.assertEqual(proc.wait(40), 0, "しばらくして終わる")
        self.clear()
        again = self.start()
        port = self.port(again)
        self.wait_ready(again, port)
        self.assertIsNone(again.poll())

    def test_共有に届かなくても起動はする(self) -> None:
        # 届かない共有のまね: 親がファイル(一覧できない)。「無い」とは確かめられない
        (self.tmp / "ファイル").write_text("x", encoding="utf-8")
        self.env["ALLTOOLS_REMOTE_STOP_DIR"] = str(self.tmp / "ファイル" / "共有")
        proc = self.start()
        port = self.port(proc)
        self.wait_ready(proc, port)
        self.assertIsNone(proc.poll())


class StopEntryTests(unittest.TestCase):
    """stop.bat(`process_manager.py`)。お願いが出ていて動いていれば、止めずに理由を返して 3。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.share = Path(tmp.name)
        saved = remote_stop.Setup(**vars(remote_stop.setup()))
        self.addCleanup(lambda: remote_stop.configure(**vars(saved)))
        remote_stop.configure(root=lambda: self.share)
        patcher = mock.patch.dict(os.environ, {"ALLTOOLS_REMOTE_STOP_DIR": str(self.share),
                                               "ALLTOOLS_PC_NAME": "PC-ADMIN"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_main(self, running: bool, pc: str = "PC-LINE1"):
        import process_manager
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.dict(os.environ, {"ALLTOOLS_PC_NAME": pc}), \
                mock.patch.object(process_manager, "_lock_present", return_value=running), \
                mock.patch.object(process_manager, "stop") as stop:
            stop.return_value = process_manager.StopResult()
            code = process_manager.main(["/nopause"])
        return code, out.getvalue(), stop

    def test_お願いが出ていれば止めずに理由を返して3(self) -> None:
        import process_manager
        self.assertTrue(remote_stop.issue(True, True, "更新のため").ok)
        code, out, stop = self.run_main(True)
        self.assertEqual(code, process_manager.EXIT_BUSY)
        self.assertTrue(out.startswith("止めませんでした: 管理者以外停止モード中"), out)
        stop.assert_not_called()

    def test_お願いを出したPCでは断らずふつうに止める(self) -> None:
        self.assertTrue(remote_stop.issue(True, True, "更新のため").ok)
        _code, out, stop = self.run_main(True, pc="PC-ADMIN")
        self.assertNotIn("管理者以外停止モード中", out)
        stop.assert_called()

    def test_動いていない_お願いが無いならいつもどおり(self) -> None:
        self.assertTrue(remote_stop.issue(True, True, "更新のため").ok)
        _code, _out, stop = self.run_main(False)
        stop.assert_called()
        self.assertTrue(remote_stop.clear(True, True).ok)
        _code, _out, stop = self.run_main(True)
        stop.assert_called()


class AdapterTests(unittest.TestCase):
    """このツールの受け方(何を「残っているもの」に数え、強制の前に何を控えるか)。"""

    def setUp(self) -> None:
        saved = remote_stop.Setup(**vars(remote_stop.setup()))
        self.addCleanup(lambda: remote_stop.configure(**vars(saved)))

    def test_このツールの名前と手元の領域を渡す_単体と中を見分ける(self) -> None:
        import start_app
        with mock.patch.dict(os.environ, {"ALLTOOLS_PORTAL": "", "ALLTOOLS_SHELL": ""}):
            rs = start_app._remote_stop()
            self.assertEqual((rs.setup().app, rs.setup().app_id, rs.setup().hosted), ("カレンダー", "calendar", False))
        with mock.patch.dict(os.environ, {"ALLTOOLS_PORTAL": "1"}):
            self.assertTrue(start_app._remote_stop().setup().hosted)
        with mock.patch.dict(os.environ, {"ALLTOOLS_SHELL": "1"}):
            self.assertTrue(start_app._remote_stop().setup().hosted)
        self.assertIsNotNone(rs.setup().local_root)
        self.assertEqual(rs.bundle_of(TOOL_DIR), TOOL_DIR.parent.parent if (TOOL_DIR.parent.parent / "portal").is_dir()
                         else None)

class CalendarAdapterTests(unittest.TestCase):
    def test_同期と送信待ちを残っているものに数え_強制の前に控える(self) -> None:
        import start_app
        from calendar_app import sync_service
        service = mock.Mock()
        service.busy_labels.return_value = ["取り込み元との同期"]
        with mock.patch.object(sync_service, "get_service", return_value=service), \
                mock.patch.object(sync_service, "unsent_count", return_value=3):
            self.assertEqual(start_app._remote_stop_left(), ["取り込み元との同期", "取り込み元へまだ送っていない入力 3 件"])
            kept = start_app._remote_stop_keep()
        self.assertTrue(any("手元(作業用DB)に残しています" in k for k in kept), kept)

    def test_画面の保存していない入力を言い添える口がある(self) -> None:
        text = (TOOL_DIR / "app" / "static" / "js" / "app.js").read_text(encoding="utf-8")
        self.assertIn("window.__remoteStopUnsaved = () => leave.unsaved()", text)
        remote_stop.reset()
        self.addCleanup(remote_stop.reset)
        remote_stop.screen_report("p1", ["設定の欄(まだ保存していません)"])
        self.assertEqual(remote_stop.screen_left(), ["設定の欄(まだ保存していません)"])
        remote_stop.screen_report("p1", [])
        self.assertEqual(remote_stop.screen_left(), [])


class BandBrowserTests(_Child):
    """画面の上の帯(本物のブラウザ。`ALLTOOLS_REQUIRE_BROWSER=1` なら飛ばさない)。"""

    @classmethod
    def setUpClass(cls) -> None:
        from tests._browser import chromium_or_skip
        cls.pw, cls.browser = chromium_or_skip()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.pw.stop()

    def token(self, proc) -> str:
        for path in (self.local / "runtime").glob("*.lock"):
            try:
                info = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(info, dict) and info.get("pid") == proc.pid and info.get("token"):
                return str(info["token"])
        return ""

    def test_帯で知らせて残りを数え_止まったら理由を出す(self) -> None:
        self.left_file.write_text("試験の打ちかけ\n", encoding="utf-8")
        proc = self.start()
        port = self.port(proc)
        self.wait_ready(proc, port)
        page = self.browser.new_page()
        self.addCleanup(page.close)
        errors: list = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"http://127.0.0.1:{port}/?t={self.token(proc)}", wait_until="load")
        self.issue()
        page.wait_for_selector("#remote-stop-band", state="visible", timeout=20000)
        text = page.inner_text("#remote-stop-band")
        self.assertIn("管理者が更新のため停止を求めています", text)
        self.assertIn("試験の打ちかけ", text)
        self.assertIn("強制で止めます", text)
        self.left_file.unlink()                       # 片付いた → まもなく止まる
        self.assertEqual(proc.wait(40), 0)
        page.wait_for_function("document.getElementById('remote-stop-band').textContent.includes('止めました')",
                               timeout=20000)
        self.assertEqual(errors, [], "画面の誤り")


if __name__ == "__main__":
    unittest.main()
