"""止める入口の決まり(業務ツール統合ランチャー 1.8.9「更新のため全 PC で止める」)

管理者が［更新のため全 PC で止める］を押すと、各 PC のランチャーが止める入口を呼ぶ:

    終了コード  0 = 止めた・もともと動いていない
                3 = 処理中なので止めない(ランチャーは止めずに待ち、1 分ごとに止め直す)
                1 = そのほか止められなかった
    標準出力の **1行目が理由**(管理者の一覧・利用者への知らせに出る。cp932 の窓でも化けない)
    --force     処理中でも止める(途中の処理は捨てる。統合は4ツール・入口・外枠すべて)
    pause       /nopause か STOP_NOPAUSE=1 で飛ばす(ランチャーは STOP_NOPAUSE=1 を付けて呼ぶ)
    道          "%~dp0" を使い、全部 " で囲む(フォルダー名に空白・全角の空白があっても動く)

.bat は Linux では動かせないので、中身を読んで決まりを確かめる。.bat が呼ぶ Python
(`process_manager.py`)の振る舞いは、入口(ポータル)の答えを差し替えて確かめる。
Windows でしか確かめられないこと(cmd での終了コード・全角空白のフォルダー)は
docs/ランチャー連携.md の「Windows で確かめる」に手順がある。
"""
from __future__ import annotations

import io
import re
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import process_manager
from portal import browser_tools, instance_guard, proc_identity

ROOT = Path(__file__).resolve().parent.parent

#: 止める入口(起動ファイルの名前・置き場所は変えない)
STOP_ENTRIES = ("stop.bat", "launcher_stop.bat", "tools/nippou/stop.bat", "tools/kanban/stop.bat",
                "tools/calendar/stop.bat", "tools/inspection/stop.bat")


def bat(name: str) -> str:
    return (ROOT / name).read_bytes().decode("cp932")


def commands(text: str) -> list[str]:
    """rem を除いた行。"""
    return [line for line in text.splitlines() if not re.match(r"(?i)^\s*(rem\b|::|@?echo off)", line)]


class BatContractTests(unittest.TestCase):
    def test_起動ファイルの名前と置き場所は変えない(self) -> None:
        for name in STOP_ENTRIES + ("Start.vbs", "start.bat", "launcher_check.bat"):
            self.assertTrue((ROOT / name).is_file(), name)
        for tool in ("nippou", "kanban", "calendar", "inspection"):
            for name in ("Start.vbs", "start.bat", "stop.bat"):
                self.assertTrue((ROOT / "tools" / tool / name).is_file(), f"{tool}/{name}")

    def test_道はdp0で全部囲む(self) -> None:
        for name in STOP_ENTRIES:
            with self.subTest(name=name):
                text = bat(name)
                self.assertIn('pushd "%~dp0"', text, "共有フォルダー(UNC)でも移動できる pushd")
                for line in commands(text):
                    for m in re.finditer(r"%~dp0", line):
                        self.assertEqual(line[m.start() - 1], '"', f"%~dp0 が \" で囲まれていない: {line}")
                        self.assertEqual(line.count('"') % 2, 0, f"\" の数が合わない: {line}")
                    self.assertNotIn("cd /d", line.lower(), "UNC を現在地にできない")

    def test_Pythonに引数をそのまま渡し_終了コードをそのまま返す(self) -> None:
        """--force が Python に届く。0 / 3 / 1 をランチャーへそのまま返す(以前の看板・日報・
        カレンダー・点検表は 0 以外をすべて 1 にしていた)。"""
        for name in STOP_ENTRIES:
            with self.subTest(name=name):
                text = bat(name)
                self.assertRegex(text, r"(?m)^python process_manager\.py( --all| --launcher)? %\*\r?$")
                self.assertIn('set "code=%errorlevel%"', text)
                self.assertIn("endlocal & exit /b %code%", text)
                self.assertNotRegex(text, r"(?mi)^\s*if errorlevel 1 \(\s*\r?\n\s*echo\.",
                                    "処理中(3)を 1 にまとめない")

    def test_pauseはnopauseとSTOP_NOPAUSEで飛ばす(self) -> None:
        for name in STOP_ENTRIES:
            with self.subTest(name=name):
                lines = commands(bat(name))
                pauses = [line.strip() for line in lines if re.search(r"(?i)\bpause\b", line)
                          and "NOPAUSE" not in line.upper().replace("IF NOT DEFINED NOPAUSE PAUSE", "")]
                for line in pauses:
                    self.assertEqual(line.lower(), "if not defined nopause pause", f"飛ばせない pause: {line}")
                if name == "launcher_stop.bat":
                    self.assertEqual(pauses, [], "ランチャーの入口は pause を置かない")
                    continue
                self.assertTrue(pauses, "失敗したら理由を読ませてから閉じる(手で実行したとき)")
                text = "\n".join(lines)
                self.assertIn('if /i "%STOP_NOPAUSE%"=="1" set "NOPAUSE=1"', text)
                self.assertIn('for %%A in (%*) do if /i "%%~A"=="/nopause" set "NOPAUSE=1"', text)
                # pause の判断より前に印を作る(pushd に失敗したときの pause も飛ばす)
                self.assertLess(text.index('set "NOPAUSE="'), text.index('pushd "%~dp0"'))

    def test_Pythonより前に標準出力へ何も出さない(self) -> None:
        """1行目は Python が出す理由。前に出してよいのは、そこで終わる失敗の知らせだけ。"""
        for name in STOP_ENTRIES:
            with self.subTest(name=name):
                lines = commands(bat(name))
                call = next(i for i, line in enumerate(lines) if line.startswith("python process_manager.py"))
                depth, block = 0, []
                for line in lines[:call]:
                    stripped = line.strip()
                    if depth == 0 and stripped.lower().startswith("echo"):
                        self.fail(f"Python の前に出している: {line}")
                    if depth == 0 and ">nul" not in stripped and re.match(r"(?i)^(python|chcp)\b", stripped):
                        self.fail(f"出力を捨てていない: {line}")
                    opened = stripped.endswith("(")
                    if depth or opened:
                        block.append(stripped)
                    depth += 1 if opened else 0
                    if stripped == ")":
                        depth -= 1
                        if any(b.lower().startswith("echo") for b in block):
                            self.assertTrue(any(b.lower().startswith("exit /b") for b in block),
                                            f"知らせのあとで終わらない: {block}")
                        block = []

    def test_括弧の中のechoに半角の括弧を書かない(self) -> None:
        """`if ( ... )` の中の `)` はブロックの終わりとして読まれる。道は " で囲めば ) が入っても崩れない。"""
        for name in STOP_ENTRIES:
            with self.subTest(name=name):
                depth = 0
                for line in commands(bat(name)):
                    stripped = line.strip()
                    if depth > 0 and stripped.lower().startswith("echo "):
                        body = re.sub(r'"[^"]*"', "", stripped[5:])
                        self.assertNotIn("(", body, line)
                        self.assertNotIn(")", body, line)
                        continue
                    depth = max(0, depth + stripped.count("(") - stripped.count(")"))

    def test_決まりをファイルの頭に書く(self) -> None:
        for name in STOP_ENTRIES:
            with self.subTest(name=name):
                head = bat(name).split("setlocal", 1)[0]
                self.assertIn("3 = 処理中", head)
                self.assertIn("1行目が理由" if name != "launcher_stop.bat" else "理由の1行だけ", head)
                self.assertIn("--force", head)
                self.assertIn("STOP_NOPAUSE=1", head)


class Portal:
    """入口(ブラウザ版のポータル)の代わり。`/api/shutdown` の答えを決めておく。"""

    def __init__(self, status: int, body: dict, *, exits: bool = True) -> None:
        self.status, self.body, self.exits = status, body, exits
        self.asked: list[dict] = []
        self.alive = True

    def request(self, url, *, data=None, headers=None, timeout=0):  # noqa: ANN001
        import json
        self.asked.append(json.loads(data.decode("utf-8")) if data else {})
        if self.status == 200 and self.exits:
            self.alive = False
        return self.status, self.body

    def is_alive(self, pid: int) -> bool:
        return pid == 999 and self.alive


class BrowserStopTests(unittest.TestCase):
    """`stop.bat`(`process_manager.py`)── ブラウザ版の入口と4ツール。"""

    def setUp(self) -> None:
        patches = [
            mock.patch.object(instance_guard, "running", return_value=instance_guard.BROWSER),
            mock.patch("start_app.read_lock", return_value={"pid": 999, "port": 8700, "token": "t"}),
            mock.patch.object(browser_tools.BrowserTools, "running", return_value=[]),
            mock.patch.object(process_manager, "ASKING_WAIT_SEC", 0.3),
            mock.patch.object(process_manager, "PORTAL_EXIT_WAIT_SEC", 0.3),
            mock.patch.object(process_manager, "PORTAL_FORCE_WAIT_SEC", 0.3),
            mock.patch.object(process_manager, "cleanup_inspection_excel"),
            # 印の本人か(起動時刻まで)は確かめられない環境として、今までどおり PID の生き死にで見る。
            # 起動時刻で見分けるところは `PidReuseStopTests`
            mock.patch("start_app.lock_owner", side_effect=lambda lock: (
                proc_identity.UNKNOWN if lock and browser_tools.is_alive(int(lock.get("pid") or 0))
                else proc_identity.GONE)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def stop(self, portal: Portal, **kw) -> tuple[int, list[str], str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("portal.browser_tools._request", side_effect=portal.request), \
                mock.patch("portal.browser_tools.is_alive", side_effect=portal.is_alive), \
                redirect_stdout(out), redirect_stderr(err):
            code = process_manager.stop(**kw)
        return code, out.getvalue().splitlines(), err.getvalue()

    def test_処理中なら3で_1行目にどのツールの何の処理か(self) -> None:
        body = {"reason": "busy", "running": ["日報: 実行中の処理があります。中断して終了しますか?"],
                "tools": [{"title": "日報", "reason": "標準作業時間の取り込み",
                           "message": "実行中の処理があります。中断して終了しますか?"},
                          {"title": "看板", "reason": "共有DBへ未反映の操作が 3 件あります",
                           "message": "共有DBへ未反映の操作が 3 件あります"}]}
        code, out, _ = self.stop(Portal(409, body))
        self.assertEqual(code, 3)
        self.assertEqual(out[0], "実行中の処理があります: 日報(標準作業時間の取り込み)、"
                                 "看板(共有DBへ未反映の操作が 3 件あります)")
        out[0].encode("cp932")
        self.assertIn("stop.bat --force", "\n".join(out))

    def test_前の入口の答え_toolsが無くても1行目を作る(self) -> None:
        body = {"reason": "busy", "running": ["点検表: 印刷の途中です。中断して終了しますか?"],
                "message": "点検表: 印刷の途中です。\n中断して終了しますか?"}
        code, out, _ = self.stop(Portal(409, body))
        self.assertEqual(code, 3)
        self.assertTrue(out[0].startswith("実行中の処理があります: 点検表("), out[0])
        self.assertNotIn("\n", out[0])

    def test_画面で閉じないが選ばれたら3(self) -> None:
        from portal import web
        body = {"reason": "refused", "running": [web.REFUSED_MESSAGE], "message": web.REFUSED_MESSAGE}
        code, out, _ = self.stop(Portal(409, body))
        self.assertEqual((code, out[0]), (3, web.REFUSED_MESSAGE))

    def test_画面で確認中のまま終わらなければ3_済めば0(self) -> None:
        from portal import web
        body = {"reason": "asking", "running": [web.ASKING_MESSAGE], "message": web.ASKING_MESSAGE}
        code, out, err = self.stop(Portal(409, body))
        self.assertEqual(code, 3)
        self.assertIn("閉じる前の確認", out[0])
        self.assertIn(web.ASKING_MESSAGE, err, "途中の経過は標準エラーへ(1行目を空けておく)")

        portal = Portal(409, body)
        real = portal.request

        def answer_then_exit(*a, **kw):     # noqa: ANN002, ANN003
            result = real(*a, **kw)
            portal.alive = False           # 画面が打ちかけを置けて、入口が自分で終わった
            return result

        portal.request = answer_then_exit
        code, out, _ = self.stop(portal)
        self.assertEqual(code, 0, out)

    def test_止めたら0_forceは入口にforceで頼む(self) -> None:
        portal = Portal(200, {"stopped": True})
        code, out, _ = self.stop(portal)
        self.assertEqual(code, 0)
        self.assertEqual(portal.asked, [{"force": False}])
        self.assertIn("止めました", out[0])
        portal = Portal(200, {"stopped": True})
        code, out, _ = self.stop(portal, force=True)
        self.assertEqual(code, 0)
        self.assertEqual(portal.asked, [{"force": True}])

    def test_動いていなければ0(self) -> None:
        with mock.patch("start_app.read_lock", return_value=None), \
                mock.patch.object(instance_guard, "running", return_value=""):
            code, out, _ = self.stop(Portal(200, {}))
        self.assertEqual((code, out[0]), (0, "日報複合ツールは動いていません"))

    def test_起動の途中なら動いていないと言わない(self) -> None:
        """錠は握ったが待ち受けの印がまだ(配布設定・共有の DB を読んでいる)。上書きさせない。"""
        with mock.patch("start_app.read_lock", return_value=None), \
                mock.patch.object(process_manager, "STARTING_WAIT_SEC", 0.3):
            code, out, _ = self.stop(Portal(200, {}))
        self.assertEqual(code, 3)
        self.assertIn("起動しています", out[0])

    def test_受け付けても入口が終わらなければ止めたと言わない(self) -> None:
        """掴んだまま「止めた」と言うと、ランチャーがフォルダーを上書きするときに失敗する。"""
        code, out, _ = self.stop(Portal(200, {"stopped": True}, exits=False))
        self.assertEqual(code, 3)
        self.assertIn("まだ終わっていません", out[0])
        self.assertNotIn("止めました", "\n".join(out))

    def test_forceで入口が終わらなければ子ごと止める(self) -> None:
        portal = Portal(200, {"stopped": True}, exits=False)

        def kill(pid):
            portal.alive = False
            return True

        with mock.patch("portal.browser_tools.terminate_tree", side_effect=kill) as cut:
            code, out, _ = self.stop(portal, force=True)
        cut.assert_called_once_with(999)
        self.assertEqual(code, 0, out)
        process_manager.cleanup_inspection_excel.assert_called_once()

    def test_入口が答えなければ1_起動時刻を確かめられないとき(self) -> None:
        """本人と確かめられたときは、5秒待って終わらせて 0(`PidReuseStopTests`)。"""
        portal = Portal(0, {}, exits=False)

        def unreachable(*a, **kw):          # noqa: ANN002, ANN003
            raise OSError("refused")

        portal.request = unreachable
        with mock.patch.object(process_manager, "_wait_pid", return_value=False):
            code, out, _ = self.stop(portal)
        self.assertEqual(code, 1)
        self.assertIn("答えません", out[0])

    def test_入口が居なくても残ったツールが処理中なら3(self) -> None:
        tool = mock.Mock()
        tool.title = "カレンダー"
        left = [browser_tools.Running(tool, {"pid": 5, "port": 1, "token": "t"})]
        busy = [{"title": "カレンダー", "reason": "取り込み元との同期", "message": "同期しています"}]
        with mock.patch("start_app.read_lock", return_value=None), \
                mock.patch.object(instance_guard, "running", return_value=""), \
                mock.patch.object(browser_tools.BrowserTools, "running", return_value=left), \
                mock.patch.object(browser_tools.BrowserTools, "busy_tools", return_value=busy), \
                mock.patch.object(browser_tools.BrowserTools, "stop_all") as stop_all:
            code, out, _ = self.stop(Portal(200, {}))
        self.assertEqual(code, 3)
        self.assertEqual(out[0], "実行中の処理があります: カレンダー(取り込み元との同期)")
        stop_all.assert_not_called()


class DesktopStopBatTests(unittest.TestCase):
    """`stop.bat` はデスクトップ版の窓を閉じない(1)。`--force` のときだけ外枠ごと止める。"""

    def test_窓を閉じずに1と理由(self) -> None:
        out = io.StringIO()
        with mock.patch.object(instance_guard, "running", return_value=instance_guard.DESKTOP), \
                mock.patch.object(instance_guard, "bring_desktop_to_front") as front, \
                redirect_stdout(out):
            code = process_manager.stop()
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue().splitlines()[0], process_manager.DESKTOP_MESSAGE)
        front.assert_called_once()

    def test_forceは外枠ごと止める(self) -> None:
        done = process_manager.Outcome(0, "日報複合ツールを強制終了しました(途中の処理は捨てました)")
        out = io.StringIO()
        with mock.patch.object(instance_guard, "running", return_value=instance_guard.DESKTOP), \
                mock.patch.object(process_manager, "force_stop_desktop", return_value=done) as force, \
                redirect_stdout(out):
            code = process_manager.stop(force=True)
        force.assert_called_once()
        self.assertEqual(code, 0)


class PortalAnswerTests(unittest.TestCase):
    """入口の 409 は、止める入口の1行目に使う `tools`・`line` を持つ。"""

    def setUp(self) -> None:
        from tests.test_web import TOKEN
        from portal import web

        self.web, self.token = web, TOKEN
        self.app = web.create_app(token=TOKEN, bridge=False)
        self.addCleanup(web.set_browser_tools, None)

    def post(self, body: dict):
        return self.app.test_client().post("/api/shutdown", json=body, headers={"X-Tool-Token": self.token},
                                           base_url="http://127.0.0.1:8700")

    def test_ツールごとの短い理由と1行目(self) -> None:
        class Busy:
            def busy_tools(self):
                return [{"title": "日報", "reason": "標準作業時間の取り込み",
                         "message": "実行中の処理があります。中断して終了しますか?"}]

            def busy(self):
                return ["日報: 実行中の処理があります。中断して終了しますか?"]

        self.web.set_browser_tools(Busy())
        res = self.post({})
        self.assertEqual(res.status_code, 409)
        body = res.get_json()
        self.assertEqual(body["line"], "実行中の処理があります: 日報(標準作業時間の取り込み)")
        self.assertEqual(body["tools"][0]["title"], "日報")
        self.assertEqual(body["running"], ["日報: 実行中の処理があります。中断して終了しますか?"],
                         "画面の確認に出す文は前のまま")

    def test_busyしか無い係でも1行目を作る(self) -> None:
        class Old:
            def busy(self):
                return ["看板: 書き戻しの途中です"]

        self.web.set_browser_tools(Old())
        body = self.post({}).get_json()
        self.assertEqual(body["line"], "実行中の処理があります: 看板(書き戻しの途中です)")


class BusyReasonTests(unittest.TestCase):
    def test_何をしているかを先に使う(self) -> None:
        """外枠の closing.rs の busy_reason と同じ順(running → busy → error.message → message)。"""
        reason = browser_tools.busy_reason
        self.assertEqual(reason({"running": ["取り込み", "印刷"], "message": "中断して終了しますか?"}),
                         "取り込み・印刷")
        self.assertEqual(reason({"busy": "書き戻しの途中です", "error": {"message": "x"}}), "書き戻しの途中です")
        self.assertEqual(reason({"error": {"message": "x"}}), "x")
        self.assertEqual(reason({"running": [], "message": "y\n z"}), "y z")
        self.assertEqual(reason({}), "実行中の処理があります")
        self.assertEqual(reason(None), "実行中の処理があります")

    def test_1行目はツールの名前と理由(self) -> None:
        line = browser_tools.busy_line([{"title": "日報", "reason": "取り込み"}, {"title": "点検表", "reason": "印刷"}])
        self.assertEqual(line, "実行中の処理があります: 日報(取り込み)、点検表(印刷)")
        line.encode("cp932")


class MainTests(unittest.TestCase):
    def test_nopauseは読み飛ばし_forceは渡す(self) -> None:
        with mock.patch.object(process_manager, "stop", return_value=0) as stop:
            self.assertEqual(process_manager.main(["/nopause"]), 0)
            stop.assert_called_with(force=False)
            process_manager.main(["--force", "/NOPAUSE"])
            stop.assert_called_with(force=True)
        with mock.patch.object(process_manager, "launcher_stop", return_value=3) as launcher:
            self.assertEqual(process_manager.main(["--launcher", "--force", "/nopause"]), 3)
            launcher.assert_called_with(force=True)

    def test_1行にして_cp932で書けない字を直す(self) -> None:
        line = process_manager.one_line("実行中の処理があります:\n  看板 — 書き戻し 中")
        self.assertEqual(line, "実行中の処理があります: 看板 ― 書き戻し 中")
        line.encode("cp932")

    def test_パイプへはcmdの文字コードで書く(self) -> None:
        """Windows でパイプ(ランチャーが受け取る)なら、窓の文字コード(chcp 932)に合わせる。"""
        class Stream:
            def __init__(self, tty: bool) -> None:
                self.tty, self.config = tty, None

            def isatty(self) -> bool:
                return self.tty

            def reconfigure(self, **kw) -> None:
                self.config = kw

        import ctypes  # noqa: F401 - os.name を "nt" にする前に読んでおく(読むときに os.name を見る)

        kernel = mock.Mock()
        kernel.GetConsoleOutputCP.return_value = 932
        for tty, env, expected in ((False, {}, {"encoding": "cp932", "errors": "replace"}),
                                   (True, {}, None),
                                   (False, {"PYTHONIOENCODING": "utf-8"}, None)):
            with self.subTest(tty=tty, env=env):
                out, err = Stream(tty), Stream(tty)
                with mock.patch.object(process_manager.os, "name", "nt"), \
                        mock.patch.dict(process_manager.os.environ, env, clear=False), \
                        mock.patch("ctypes.WinDLL", return_value=kernel, create=True), \
                        mock.patch.object(process_manager.sys, "stdout", out), \
                        mock.patch.object(process_manager.sys, "stderr", err):
                    if not env:
                        process_manager.os.environ.pop("PYTHONIOENCODING", None)
                    process_manager.console_stdout()
                self.assertEqual(out.config, expected)
                self.assertEqual(err.config, expected)


if __name__ == "__main__":
    unittest.main()
