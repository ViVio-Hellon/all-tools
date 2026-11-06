"""管理者以外停止モード(`remote_stop.py`。日報複合ツールの大設定から管理者が出すお願い)── 日報の受け方

日報複合ツールの大設定で、管理者(管理者パスワード かつ Administrator)が「ほかの PC を止める」を押すと、
共有DBの置き場所の `日報複合ツール_管理者以外停止\\お願い.json` にお願いが置かれる。単体で起動した日報も同じフォルダを見る。

    何も残っていなければ        帯を見せてから、まもなく止まる
    残っているもの(走っている処理(共有へ保存・取り込み))   利用者を待つ。期限(5分。試験は短く)で強制で止める。止める前に控えを書き、
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

LOCAL_ENV = "NIPPOU_LOCAL_DIR"
EXTRA_ARGS: list = []
EXTRA_ENV: dict = {}
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
            proc.out_file.close()

    def start(self) -> subprocess.Popen:
        # 子の出力は**ファイルへ**。パイプにすると読む人が居ないあいだに詰まり(Windows のパイプは
        # 数KB)、子が print で止まって終われない(強制で止める道はログが多い)
        out = open(self.tmp / f"子{len(self.procs)}.log", "wb")
        proc = subprocess.Popen([sys.executable, str(TOOL_DIR / "start_app.py"), "--no-browser", *EXTRA_ARGS],
                                cwd=str(TOOL_DIR), env=self.env, stdin=subprocess.DEVNULL,
                                stdout=out, stderr=subprocess.STDOUT)
        proc.out_file = out
        self.procs.append(proc)
        return proc

    def output(self, proc) -> str:
        proc.out_file.flush()
        return Path(proc.out_file.name).read_bytes().decode("utf-8", "replace")[-3000:]

    def wait_exit(self, proc, timeout: float = 40) -> int:
        """終わるのを待つ。終わらなければ子の出力を添えて落とす(何で止まっているかが分かるように)。"""
        try:
            return proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self.fail(f"{timeout:.0f}秒たっても終わりません。子の出力:\n{self.output(proc)}")

    def port(self, proc) -> int:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                self.fail(f"起動しませんでした: {self.output(proc)}")
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
        self.assertEqual(self.wait_exit(proc), 0, "お願いで止まらない")
        answer = self.answer()
        self.assertEqual((answer["state"], answer["app"]), (remote_stop.STATE_STOPPED, "日報"))

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
        self.assertEqual(self.wait_exit(proc), 0)
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
        self.assertEqual(self.wait_exit(proc), 0, "しばらくして終わる")
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
            self.assertEqual((rs.setup().app, rs.setup().app_id, rs.setup().hosted), ("日報", "nippou", False))
        with mock.patch.dict(os.environ, {"ALLTOOLS_PORTAL": "1"}):
            self.assertTrue(start_app._remote_stop().setup().hosted)
        with mock.patch.dict(os.environ, {"ALLTOOLS_SHELL": "1"}):
            self.assertTrue(start_app._remote_stop().setup().hosted)
        self.assertIsNotNone(rs.setup().local_root)
        self.assertEqual(rs.bundle_of(TOOL_DIR), TOOL_DIR.parent.parent if (TOOL_DIR.parent.parent / "portal").is_dir()
                         else None)

class NippouAdapterTests(unittest.TestCase):
    def test_走っている処理を残っているものに数え_強制の前に控える(self) -> None:
        import start_app
        from nippou import running
        self.assertEqual(start_app._remote_stop_left(), [])
        with running.running("共有へ保存"):
            self.assertEqual(start_app._remote_stop_left(), ["共有へ保存"])
            kept = start_app._remote_stop_keep()
        self.assertTrue(any("打ちかけ" in k for k in kept), kept)
        self.assertTrue(any("共有へ保存" in k and "続きから送ります" in k for k in kept), kept)

    def test_共有への保存の最中は書いている最中と答える(self) -> None:
        """期限を過ぎても強制せずに待つ(最長さらに5分)。ほかの処理(取り込みなど)は待たない。"""
        import start_app
        from nippou import running
        from nippou.services import push_flow
        self.assertEqual(start_app._remote_stop_writing(), [])
        with running.running("標準作業時間の取り込み"):
            self.assertEqual(start_app._remote_stop_writing(), [])
        with running.running(push_flow.PUSH_ACTION):
            self.assertEqual(start_app._remote_stop_writing(), [push_flow.PUSH_ACTION])

    def test_止めると決まったら画面が打ちかけを置く口がある(self) -> None:
        text = (TOOL_DIR / "app" / "static" / "js" / "app.js").read_text(encoding="utf-8")
        self.assertIn("window.__remoteStopFlush = () => nav.leaving()", text)
        base = (TOOL_DIR / "app" / "templates" / "base.html").read_text(encoding="utf-8")
        self.assertIn("js/remote_stop_band.js", base)


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
        self.assertEqual(self.wait_exit(proc), 0)
        page.wait_for_function("document.getElementById('remote-stop-band').textContent.includes('止めました')",
                               timeout=20000)
        self.assertEqual(errors, [], "画面の誤り")

    def test_置く先の無い打ちかけは_残っているものに数えて待つ(self) -> None:
        """出る前の確かめを持たない画面(GW計算など)で打った欄は、置く先が無く止めると消える。
        何も無いとして帯を見せる間(現場 30 秒)で止めない(帯に出して利用者を待つ。期限で強制)。

        画面が「保存していない入力」を伝えるのは帯の問い合わせ(3秒ごと)。帯を見せる間を試験の 0.5 秒の
        ままにすると、遅い PC(Windows CI)では伝わる前に「何も無い」で止まり、現場(30 秒)と違う道を
        試してしまう。この試験だけ現場と同じく、問い合わせより長く見せる(期限はその後)。"""
        self.env["ALLTOOLS_REMOTE_STOP_TIMES"] = TIMES + ",notice=8,grace=12"
        proc = self.start()
        port = self.port(proc)
        self.wait_ready(proc, port)
        page = self.browser.new_page()
        self.addCleanup(page.close)
        page.goto(f"http://127.0.0.1:{port}/gw?t={self.token(proc)}", wait_until="load")
        page.wait_for_function("window.APP && window.APP.booted === true", timeout=30000)
        # 遅い PC(Windows CI)では、起きた直後に画面が中身を引き直すことがある。引き直しの前に打つと
        # 打った欄ごと消えて「打ちかけ」にならない ── 落ち着いてから打ち、画面が打ちかけと見たのを確かめる
        page.wait_for_load_state("networkidle")
        # 打つのは**寸法の欄**(打った寸法は、あとで引いた答えで戻さない ── `gw.js` の editAt)。
        # 「最初の文字の欄」だと、引いた答えで埋め直す欄に当たることがあり、遅い PC(Windows CI)では
        # 打ったあとに埋め直されて打ちかけにならなかった。念のため、打ちかけと見えるまで打ち直す
        # 「見えている」は待たない(Windows CI で寸法の欄が見えている扱いにならず30秒待って落ちた)。
        # 欄に焦点を当て、キーボードで打つ(人が打ったのと同じ扱い ── `nav.remember` は isTrusted を見る)
        dim_id = page.evaluate("(document.querySelector('main#main [data-dim]:not([readonly]):not([disabled])')"
                               " || {}).id || ''")
        self.assertTrue(dim_id, f"寸法の欄がありません: {page.url} "
                        f"欄の数={page.evaluate('document.querySelectorAll(\'[data-dim]\').length')}")
        for attempt in range(3):
            page.focus(f"#{dim_id}")
            page.keyboard.press("Control+A")
            page.keyboard.type(str(98765 + attempt))
            try:
                page.wait_for_function("window.__remoteStopUnsaved().length > 0", timeout=5000)
                break
            except Exception:                         # noqa: BLE001 - 打ち直す
                if attempt == 2:
                    raise
        time.sleep(3.5)                               # 帯の問い合わせ(3秒ごと)が、お願いの前に言い添える
        self.issue()
        issued = time.monotonic()
        try:
            page.wait_for_function("document.getElementById('remote-stop-band')"
                                   " && document.getElementById('remote-stop-band').textContent"
                                   ".includes('この画面で保存していない入力')", timeout=20000)
        except Exception as exc:                      # 何が出ていたかを添えて落とす
            band = page.evaluate("(document.getElementById('remote-stop-band') || {}).textContent || '(帯なし)'")
            seen = page.evaluate("window.__remoteStopUnsaved()")
            self.fail(f"帯に保存していない入力が出ない: {exc}\n帯: {band}\n画面の言い分: {seen}\n"
                      f"子の出力:\n{self.output(proc)}")
        # 帯を見せる間(この試験は 8 秒)をすぎても止めない(期限はこの試験は 12 秒)
        time.sleep(max(0.0, 9.0 - (time.monotonic() - issued)))
        self.assertEqual(self.health(port, "?remote_stop=1&page=t")["remote_stop"]["state"]["state"],
                         remote_stop.STATE_WAITING)
        self.assertIsNone(proc.poll(), "保存していない入力があるのに止まった")
        self.assertEqual(self.wait_exit(proc), 0, "期限では止まる")
        self.assertEqual(self.answer()["state"], remote_stop.STATE_FORCED)


if __name__ == "__main__":
    unittest.main()
