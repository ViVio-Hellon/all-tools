"""早見表を別の窓で開く ── 早見表だけ (v4.16.0)

    別で開くのはよいのだが、早見表だけにしておかないと別で開いたもので日報に戻れてしまう

【約束】
    ・別窓(`/vc?window=quick`)には帯もレールも出さない(F キーで画面を移る道もレールから来る)
    ・面の札も出さない。早見表の面だけが開いている。設定の面へのリンクも出さない
    ・いつもの VC長さ計算(`/vc`)は変わらない
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class QuickWindowTests(WebTestCase):
    def test_別窓は早見表だけ(self) -> None:
        page = self.get("/vc?window=quick&tab=quick").get_data(as_text=True)
        self.assertIn('data-bare="1"', page)
        self.assertNotIn('<nav class="rail"', page)               # ほかの画面へ移る道が無い
        self.assertNotIn('<header class="ribbon">', page)
        self.assertNotIn('id="quit"', page)
        self.assertRegex(page, r'class="tabs__bar"[^>]*hidden')  # 面の札も出さない
        self.assertRegex(page, r'id="panel-quick"[^>]*>')
        self.assertNotRegex(page, r'id="panel-quick"[^>]*hidden')
        self.assertRegex(page, r'id="panel-calc"[^>]*hidden')
        self.assertNotIn('data-vc-tab="settings"', page)
        self.assertNotIn("<h1>VC長さ計算</h1>", page)
        self.assertIn("VCフィルム長さ早見表", page)

    def test_いつもの画面は変わらない(self) -> None:
        page = self.get("/vc").get_data(as_text=True)
        self.assertIn('<nav class="rail"', page)
        self.assertIn('<header class="ribbon">', page)
        self.assertNotIn('data-bare="1"', page)
        self.assertNotRegex(page, r'class="tabs__bar"[^>]*hidden')

    def test_開くボタンは別窓の道へ(self) -> None:
        js = (ROOT / "app" / "static" / "js" / "views" / "vc.js").read_text(encoding="utf-8")
        self.assertIn('window.open("/vc?window=quick&tab=quick"', js)
        self.assertIsNone(re.search(r'window\.open\("/vc\?tab=quick"', js))


if __name__ == "__main__":
    unittest.main()
