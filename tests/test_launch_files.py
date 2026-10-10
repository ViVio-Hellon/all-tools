"""起動ファイル(ブラウザ版の Start.vbs / start.bat / stop.bat、配布の make_dist.bat)

- **CP932(Shift-JIS)・CRLF。** WSH は .vbs をシステムの ANSI コードページ(日本語の
  Windows では 932)で読む。cmd.exe は .bat をコンソールのコードページで読む
- .bat は chcp の行より前を ASCII だけにする(切り替える前に日本語のバイトが来ると崩れる)
- 呼んでいるファイルが同じフォルダにある
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCHERS = ("Start.vbs", "start.bat", "stop.bat", "launcher_check.bat", "launcher_stop.bat")


class EncodingTests(unittest.TestCase):
    def test_CP932でCRLF(self) -> None:
        for name in LAUNCHERS:
            with self.subTest(name=name):
                data = (ROOT / name).read_bytes()
                data.decode("cp932")                       # 読めること
                self.assertNotIn(b"\r\r", data)
                self.assertEqual(data.count(b"\n"), data.count(b"\r\n"), "LF だけの行がある")
                self.assertFalse(data.startswith(b"\xef\xbb\xbf"), "BOM を付けない")

    def test_batはchcpより前がASCIIだけ(self) -> None:
        for name in ("start.bat", "stop.bat", "launcher_check.bat", "launcher_stop.bat"):
            with self.subTest(name=name):
                data = (ROOT / name).read_bytes()
                head, sep, _rest = data.partition(b"chcp 932")
                self.assertTrue(sep, "chcp 932 が無い")
                head.decode("ascii")

    def test_配布のbatは英字だけ(self) -> None:
        data = (ROOT / "scripts" / "make_dist.bat").read_bytes()
        data.decode("ascii")
        self.assertEqual(data.count(b"\n"), data.count(b"\r\n"))

    def test_git_はこれらを書き換えない(self) -> None:
        """.gitattributes で -text(CRLF・CP932 をそのまま残す)。"""
        attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8")
        for pattern in ("*.vbs", "*.bat"):
            self.assertRegex(attrs, rf"(?m)^{re.escape(pattern)}\s+.*-text")


class ContentTests(unittest.TestCase):
    def text(self, name: str) -> str:
        return (ROOT / name).read_bytes().decode("cp932")

    def test_Start_vbsはpythonwでstart_appを絶対パスで起こす(self) -> None:
        text = self.text("Start.vbs")
        self.assertIn('fso.BuildPath(here, "start_app.py")', text)
        self.assertIn('cmd = "pythonw " & Chr(34) & script & Chr(34)', text)
        self.assertIn("shell.Run cmd, 0, False", text)
        self.assertIn("日報複合ツール.exe", text, "ふだんは exe を使うと書く")

    def test_ランチャーの入口(self) -> None:
        """業務ツール統合ランチャー 1.7 の入口。繰り返し・隠れて呼ばれるので pause を置かない。"""
        check = self.text("launcher_check.bat")
        stop = self.text("launcher_stop.bat")
        self.assertIn("python process_manager.py --check", check)
        self.assertIn("python process_manager.py --launcher %*", stop, "--force を渡す")
        for text in (check, stop):
            self.assertNotRegex(text, r"(?mi)^\s*pause\b")
            self.assertIn('endlocal & exit /b %code%', text, "終了コードをそのまま返す")

    def test_stop_batは戻り値をそのまま返す(self) -> None:
        """0 止めた / 1 デスクトップ版 / 2 止めなかった / 3 Python が無い(docs/ランチャー連携.md)。"""
        text = self.text("stop.bat")
        self.assertIn('set "code=%errorlevel%"', text)
        self.assertIn("endlocal & exit /b %code%", text)
        self.assertIn("exit /b 3", text)

    def test_呼ぶファイルがある(self) -> None:
        self.assertIn("python start_app.py --check", self.text("start.bat"))
        self.assertIn("python start_app.py %*", self.text("start.bat"))
        self.assertIn("python process_manager.py %*", self.text("stop.bat"))
        for name in ("start_app.py", "process_manager.py", "bridge.py", "scripts/make_dist.py"):
            self.assertTrue((ROOT / name).is_file(), name)

    def test_ログの場所は手元の領域の名前と同じ(self) -> None:
        import json
        name = json.loads((ROOT / "config" / "app.json").read_text(encoding="utf-8"))["local_dir_name"]
        self.assertIn(f"%LOCALAPPDATA%\\{name}\\logs", self.text("start.bat"))


if __name__ == "__main__":
    unittest.main()
