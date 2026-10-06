"""版の決まり(python-web-tools の test_app_config.py と同じ網)

版は `config/app.json` の `version` ただ1つが出どころで、画面・`/api/health`・
ログ・`stop.bat --status` はすべてここを読む。上げ方は `docs/変更履歴.md`。
"""
from __future__ import annotations

import unittest
from pathlib import Path

import tests.helpers  # noqa: F401  (ローカル領域を一時フォルダへ)
from core import app_config

_ROOT = Path(__file__).resolve().parent.parent


class VersionTests(unittest.TestCase):
    def setUp(self) -> None:
        app_config.load(force=True)

    def test_実ファイルが読める(self) -> None:
        self.assertEqual(app_config.load_error(), "")

    def test_版は数字3つ(self) -> None:
        """並べて比べられる形であること。"""
        self.assertEqual(app_config.version_problem(), "")

    def test_画面に出す形(self) -> None:
        self.assertEqual(app_config.version_label(), f"VER{app_config.version()}")

    def test_変更履歴にいまの版がある(self) -> None:
        """版を上げたら `docs/変更履歴.md` に1節足す。足していなければ落ちる。"""
        text = (_ROOT / "docs" / "変更履歴.md").read_text(encoding="utf-8")
        self.assertIn(f"## {app_config.version_label()}", text,
                      "docs/変更履歴.md にいまの版の節がありません")

    def test_変更履歴は新しい版が上(self) -> None:
        text = (_ROOT / "docs" / "変更履歴.md").read_text(encoding="utf-8")
        found = [line[len("## VER"):].strip() for line in text.splitlines()
                 if line.startswith("## VER")]
        self.assertTrue(found)
        self.assertEqual(found[0], app_config.version(), "先頭の節がいまの版ではありません")
        as_numbers = [tuple(int(x) for x in v.split(".")) for v in found]
        self.assertEqual(as_numbers, sorted(as_numbers, reverse=True))

    def test_診断起動の表示に版がある(self) -> None:
        self.assertIn(f"バージョン    : {app_config.version()}", app_config.describe())


if __name__ == "__main__":
    unittest.main()


class StorePythonTests(unittest.TestCase):
    """Microsoft Store 版の Python を見分ける(ログの場所を正しく案内するため)。"""

    def test_見分ける(self) -> None:
        exe = (r"C:\Users\xfa-ngykonpo\AppData\Local\Microsoft\WindowsApps"
               r"\PythonSoftwareFoundation.Python.3.12_qbz5n2kfra8p0\python.exe")
        prefix = (r"C:\Program Files\WindowsApps"
                  r"\PythonSoftwareFoundation.Python.3.12_3.12.2800.0_x64__qbz5n2kfra8p0")
        for path in (exe, prefix):
            with self.subTest(path=path):
                self.assertEqual(app_config.store_python_family([path]),
                                 "PythonSoftwareFoundation.Python.3.12_qbz5n2kfra8p0")

    def test_普通のPythonは違う(self) -> None:
        for path in (r"C:\Users\x\AppData\Local\Programs\Python\Python312\python.exe",
                     r"C:\Python311\python.exe", "/usr/bin/python3"):
            self.assertEqual(app_config.store_python_family([path]), "")
