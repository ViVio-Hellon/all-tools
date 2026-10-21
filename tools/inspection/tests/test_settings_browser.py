"""設定の配布設定を**本物のブラウザ**で ── 画面の既定のチェックのまま書き出すと、大設定の入口と同じ中身 (段階1)

前は ``settings.js`` の ``(it.default && it.set)`` と ``export_distribution.py`` の ``if default]`` の
字を探していた。ここではこの端末で項目を設定しておき、設定の「配布設定」を開いて**チェックに触らずに**
書き出す。日報複合ツールの大設定が起こす入口(``export_distribution.run``)で書き出し直した中身と同じこと。

- 表示(ダーク/ライト)は既定で外す(人ごとの好み)。この端末で設定していても入らない
- この端末で変えていない項目は、選べない(既定のまま)
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import unittest
from pathlib import Path

from app.services import admin_password, distribution

from tests._page_browser import PageBrowserCase

_ROOT = Path(__file__).resolve().parent.parent


def _entry():
    spec = importlib.util.spec_from_file_location("inspection_export_entry_t", _ROOT / "export_distribution.py")
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    return entry


class DistributionBrowserTests(PageBrowserCase):
    def setUp(self) -> None:
        super().setUp()
        folder = distribution.default_dir()
        self.written = folder / distribution.SETTINGS_NAME
        self.addCleanup(shutil.rmtree, folder, True)
        keep = self.biz.settings.values()
        self.addCleanup(self.biz.settings.put_many, {k: keep.get(k) for k in ("log_keep_days", "theme_choice")})
        # この端末で変えた項目: ログの保存日数(既定で入る)・表示(既定で外す)
        self.biz.settings.put_many({"log_keep_days": 45})
        self.biz.settings.update(theme="dark")

    def contents(self) -> dict:
        return json.loads(self.written.read_text(encoding="utf-8"))["settings"]

    def test_既定のチェックのまま書き出すと_大設定の入口と同じ中身になる(self) -> None:
        page = self.open("/")
        page.click("#btn-settings")
        page.wait_for_selector("#settings-dialog[open]")
        page.click("#tab-distribution")
        page.wait_for_selector("#dist-items input[data-dist-item]")
        boxes = page.evaluate("""() => Object.fromEntries([...document.querySelectorAll('#dist-items input[data-dist-item]')]
            .map((i) => [i.dataset.distItem, { checked: i.checked, disabled: i.disabled }]))""")
        self.assertEqual(boxes["theme_choice"], {"checked": False, "disabled": False}, "表示は既定で外す")
        self.assertEqual(boxes["log_keep_days"], {"checked": True, "disabled": False})

        page.fill("#dist-password", admin_password.DEFAULT)
        page.click("#dist-export")
        self.wait_until(page, self.written.exists, "配布設定を書き出す")
        from_screen = self.contents()
        self.assertEqual(from_screen.get("log_keep_days"), 45)
        self.assertNotIn("theme_choice", from_screen)

        self.written.unlink()
        reply = _entry().run()
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(self.contents(), from_screen)
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
