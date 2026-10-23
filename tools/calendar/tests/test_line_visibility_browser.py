"""見せる登録の決まりは1つ ── 本物のブラウザで (VER4.2.6)

L-1 の端末で、コイルの休み・L-1 の休み・HVC 宛の連絡がある日を開く:

- マスにマウスを乗せたとき(title)・内容閲覧・削除の選択肢のどれにも、コイルの休みと HVC 宛の連絡が出ない
- L-1 の休みはどれにも出る(マス目と同じ)
"""
from __future__ import annotations

import datetime as _dt

from calendar_app import config, repository
from calendar_app import settings as user_settings

from tests import _web
from tests._page_browser import PageBrowserCase


class LineVisibilityBrowserTests(PageBrowserCase):
    def setUp(self) -> None:
        super().setUp()
        _web.with_source(self)
        self.addCleanup(user_settings.save_my_line, "")
        user_settings.save_my_line("L-1")
        today = _dt.date.today()
        self.date = today.strftime(config.DATE_KEY_FORMAT)
        repo = repository.Repository(self.conn)
        repo.save_record(today, config.KUBUN_REST, "小林コイル", "1", shift="1", group="A", line="コイル")
        repo.save_record(today, config.KUBUN_REST, "山田エル", "2", shift="1", group="B", line="L-1")
        repo.save_record(today, config.KUBUN_OTHER, "HVCへの連絡です", "", group="A", line="HVC",
                         writer_group="A")

    def test_マウス_内容閲覧_削除のどれもマス目と同じ(self) -> None:
        page = self.open("/calendar")
        cell = f'.day[data-date="{self.date}"]'
        page.wait_for_selector(cell)
        page.wait_for_function(f"document.querySelector('{cell}').innerText.includes('山田エル')", timeout=5000)
        body = page.inner_text(cell)
        tip = page.get_attribute(cell, "title") or ""
        self.assertNotIn("小林コイル", body)
        self.assertNotIn("小林コイル", tip, "マウスを乗せたときにコイルの休みが出ています")
        self.assertIn("山田エル", tip)

        page.click(cell)
        page.click(".modal button.choice:has-text('内容閲覧')")
        page.wait_for_selector(".modal .viewer")
        viewer = page.inner_text(".modal .viewer")
        self.assertIn("山田エル", viewer)
        self.assertNotIn("小林コイル", viewer, "内容閲覧にコイルの休みが出ています")
        self.assertNotIn("HVCへの連絡です", viewer, "内容閲覧にほかのライン宛の連絡が出ています")
        page.keyboard.press("Escape")
        page.wait_for_selector(".modal", state="detached")

        page.click(cell)
        page.click(".modal button.choice:has-text('削除')")
        page.wait_for_selector("#del-list")
        choices = page.inner_text("#del-list")
        self.assertIn("山田エル", choices)
        self.assertNotIn("小林コイル", choices, "削除の選択肢にコイルの休みが出ています")
        self.assertNotIn("HVCへの連絡です", choices)
        self.assertEqual(self.errors, [])
