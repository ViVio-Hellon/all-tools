"""設定: 画面の配線を**本物のブラウザ**で確かめる (段階1)

前は `views/settings.js` の字を探して「文言が入っていること」だけを見ていました
(`test_web_settings` の SettingsWordingTests の一部)。ここでは画面を開いて押します:

    - 取り込みの下見: 何の一覧か(入れる日報 = 日付・ライン・直・ページ)と、重複したら
      **中身をまるごと入れ替える**(いまある行は消える)ことを、押す前に表の上で言う

外からの名指し(`?tab=`)で面を開くのは、`tabs.js` と `settings.js` の二重の守りで、
片方を外しても画面では区別できないので字の試験のまま(`test_web_settings`)。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._other_screen_browser import ScreenBrowserCase  # noqa: E402

DAY = "2026年8月3日"


class ImportPreviewTests(ScreenBrowserCase):
    def test_取り込みの下見は何の一覧かと重複したらまるごと入れ替えることを言う(self) -> None:
        from nippou.db.models import DetailRecord, HeaderRecord

        key = dict(report_date=DAY, line="L-1", shift="1直", page=1)
        self.repo().save(HeaderRecord(**key, worker="山田"),
                         [DetailRecord(**key, row_no=n, lot=f"A{n}") for n in (1, 2, 3)])
        csv = self.tmp / "in.csv"
        csv.write_text(f"日付,ライン,直,ロット№\n{DAY},L-1,1直,B\n", encoding="utf-8-sig")
        self.admin()
        page = self.open_screen("/settings?tab=data", "#import-path")
        page.fill("#import-path", str(csv))
        page.click("#import-preview")
        page.wait_for_selector("#import-plan table", timeout=8000)
        plan = page.inner_text("#import-plan")
        self.assertIn("中身をまるごと入れ替えます", plan)
        self.assertIn("いまある行は消えて", plan)
        heads = page.locator("#import-plan th").all_text_contents()
        self.assertEqual(heads[0], "入れる日報(日付・ライン・直・ページ)", "何の一覧かが見出しから読めません")
        row = page.locator("#import-plan tbody tr").first.inner_text()
        self.assertIn(f"{DAY} L-1 1直 1ページ", row)
        self.assertIn("置き換え(いま 3行)", row.replace("\t", ""))
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    import unittest

    unittest.main()
