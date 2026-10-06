"""起動ファイル(Start.vbs / start.bat / stop.bat)の決まり事

【なぜ機械で見るのか】
どれも**日本語Windowsの ANSI コードページ(CP932)で読まれる**。UTF-8 で
保存すると、日本語が化けるだけでなく **CP932 の2バイト目が 0x5C
(バックスラッシュ)や 0x40(@)になる文字**があるため、コメントの中の
1文字がコマンドとして解釈されて壊れる。

しかもこの壊れ方は**この開発機では再現しない**(Linux で読む限り
問題なく見える)。置いた先で初めて分かるので、ここで固定しておく。
"""
from __future__ import annotations

import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

LAUNCH_FILES = ("Start.vbs", "start.bat", "stop.bat")


class EncodingTests(unittest.TestCase):
    def test_全ての起動ファイルがある(self) -> None:
        for name in LAUNCH_FILES:
            self.assertTrue((_ROOT / name).is_file(), f"{name} がありません")

    def test_CP932で読める(self) -> None:
        for name in LAUNCH_FILES:
            data = (_ROOT / name).read_bytes()
            try:
                data.decode("cp932")
            except UnicodeDecodeError as exc:
                self.fail(f"{name} は CP932 で読めません(UTF-8で保存されている"
                          f"可能性があります): {exc}")

    def test_改行がCRLF(self) -> None:
        for name in LAUNCH_FILES:
            data = (_ROOT / name).read_bytes()
            # LF だけの行が無いこと
            self.assertNotIn(b"\n", data.replace(b"\r\n", b""),
                             f"{name} に LF だけの改行があります")

    def test_UTF8のBOMが付いていない(self) -> None:
        # BOM が付くと cmd.exe が1行目を解釈できない
        for name in LAUNCH_FILES:
            data = (_ROOT / name).read_bytes()
            self.assertFalse(data.startswith(b"\xef\xbb\xbf"),
                             f"{name} に UTF-8 BOM が付いています")


class BatchOrderTests(unittest.TestCase):
    """`chcp` より前は ASCII だけにする。

    cmd.exe は .bat を**そのときのコンソールのコードページ**で読む。
    つまり `chcp 932` より前に非ASCIIバイトがあると、それが化ける。
    コメントの中であっても同じ。
    """

    def test_chcpより前はASCIIだけ(self) -> None:
        for name in ("start.bat", "stop.bat"):
            data = (_ROOT / name).read_bytes()
            head, sep, _ = data.partition(b"chcp")
            self.assertTrue(sep, f"{name} に chcp がありません")
            non_ascii = [b for b in head if b > 0x7F]
            self.assertEqual(non_ascii, [],
                             f"{name} の chcp より前に非ASCIIバイトがあります")


class ContentTests(unittest.TestCase):
    def test_Start_vbsはstart_app_pyを呼ぶ(self) -> None:
        text = (_ROOT / "Start.vbs").read_bytes().decode("cp932")
        self.assertIn("start_app.py", text)
        # コンソールを出さないために pythonw を使う
        self.assertIn("pythonw", text)

    def test_start_batはstart_app_pyを呼ぶ(self) -> None:
        text = (_ROOT / "start.bat").read_bytes().decode("cp932")
        self.assertIn("start_app.py", text)

    def test_stop_batはprocess_managerを呼ぶ(self) -> None:
        text = (_ROOT / "stop.bat").read_bytes().decode("cp932")
        self.assertIn("process_manager.py", text)

    def test_引数をそのまま渡す(self) -> None:
        # `start.bat --check` / `stop.bat --force` が通ること
        for name in ("start.bat", "stop.bat"):
            text = (_ROOT / name).read_bytes().decode("cp932")
            self.assertIn("%*", text, f"{name} が引数を渡していません")


if __name__ == "__main__":                       # pragma: no cover
    unittest.main()
