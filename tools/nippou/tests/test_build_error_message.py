"""本体を組み立てられないときの文(フォルダ一式がそろっていない)。

現場から「アプリを組み立てられませんでした: No module named 'nippou.services'」と届いた。
コピーの途中で止まった・ウイルス対策ソフトの隔離などで、このフォルダの部品が無いときは、
どのフォルダが無いのかと、直し方(配布フォルダを丸ごと入れ直す)を言う。
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import start_app


class BuildErrorMessageTest(unittest.TestCase):
    def test_missing_folder_of_this_app_is_named(self):
        root = Path(tempfile.mkdtemp(prefix="nippou_missing_"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        (root / "nippou").mkdir()
        exc = ModuleNotFoundError("No module named 'nippou.services'", name="nippou.services")
        text = start_app.build_error_message(exc, app_root=root)
        self.assertIn("No module named 'nippou.services'", text, "元の文も残す(問い合わせのため)")
        self.assertIn("日報のフォルダ一式がそろっていません", text)
        self.assertIn(str(root / "nippou" / "services"), text)
        self.assertIn("丸ごと入れ直してください", text)

    def test_other_errors_keep_the_plain_message(self):
        self.assertEqual(start_app.build_error_message(ValueError("x")), "アプリを組み立てられませんでした: x")
        exc = ModuleNotFoundError("No module named 'flask'", name="flask")
        self.assertNotIn("フォルダ一式", start_app.build_error_message(exc), "外の部品の不足はフォルダの話ではない")

    def test_both_starts_use_it(self):
        text = Path(start_app.__file__).read_text(encoding="utf-8")
        self.assertEqual(text.count("srv.boot.mark_error(build_error_message(exc))"), 2,
                         "ブラウザ版とデスクトップ版の両方で")


if __name__ == "__main__":
    unittest.main()
