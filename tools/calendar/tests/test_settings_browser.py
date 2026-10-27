"""帯の版・設定画面の配布設定を**本物のブラウザ**で (段階1)

- 帯の版は、どの画面でも**読める大きさ**で出る(前は ``layout.css`` の ``.ver`` の字を読んでいた)
- 配布設定の「書き出す」を**画面の既定のチェックのまま**押すと、日報複合ツールの大設定が起こす
  入口(``export_distribution.py``)と同じ項目を送る(前は ``settings.js`` の
  ``input.checked = it.default;`` の字を探していた)
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest import mock

from calendar_app import app_config, distribution

from tests import _web
from tests._page_browser import PageBrowserCase

_ROOT = Path(__file__).resolve().parent.parent


def _entry_items() -> list[str]:
    """入口(``export_distribution.run``)が書き出しに選ぶ項目。書き出しそのものはさせない。"""
    spec = importlib.util.spec_from_file_location("calendar_export_entry_t", _ROOT / "export_distribution.py")
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    seen: list[list[str]] = []

    def export(password, items, *, admin=False, keep_previous=False):
        seen.append(list(items))
        return distribution.Result(False, "試験", distribution.REFUSE_NOTHING)

    with mock.patch.object(distribution, "export", export):
        entry.run()
    return seen[0]


class SettingsBrowserTests(PageBrowserCase):
    def test_帯の版はどの画面でも読める大きさで出る(self) -> None:
        for path in ("/calendar", "/history", "/settings"):
            with self.subTest(path=path):
                page = self.open(path)
                ver = page.locator(".ribbon .ver")
                self.assertTrue(ver.is_visible())
                self.assertEqual(ver.inner_text().strip(), app_config.version_label())
                size = float(ver.evaluate("(e) => parseFloat(getComputedStyle(e).fontSize)"))
                self.assertGreaterEqual(size, 12, f"{path}: 版の文字が {size}px")
                page.context.close()
        self.assertEqual(self.errors, [])

    def test_配布設定を既定のチェックのまま書き出すと_大設定の入口と同じ項目を送る(self) -> None:
        # 書き出しそのものは別の試験(test_distribution)が見る。ここは何を送るかだけ
        self.answers["/api/settings/distribution/export"] = (403, {"error": {
            "code": "test", "message": "試験で止めました"}})
        page = self.open("/settings")
        page.wait_for_selector("[data-dist-item]", state="attached")
        page.fill("#st-dist-password", _web.ADMIN_PASSWORD)
        page.click("#st-dist-export")
        self.wait_until(page, lambda: self.posts_to("/api/settings/distribution/export"), "書き出しを送る")
        sent = self.posts_to("/api/settings/distribution/export")[0]
        expected = _entry_items()
        self.assertTrue(expected)
        self.assertEqual(sent["items"], expected)
        self.assertEqual(sent["password"], _web.ADMIN_PASSWORD)
        # 既定で入れない項目(ライン)は、画面でもチェックが外れている
        unchecked = page.evaluate("""() => [...document.querySelectorAll('[data-dist-item]')]
            .filter((n) => !n.checked).map((n) => n.dataset.distItem)""")
        self.assertEqual(set(unchecked), {key for key, _, default in distribution.ITEMS if not default})
        self.assertTrue(unchecked, "既定で外す項目が無い(見分けられない)")
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
