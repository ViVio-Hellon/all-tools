"""梱包資材重量計算のスピンボタン / 作業者を選ぶ窓の並べ方 (v4.16.0)

    梱包締枚数 縦バンド本数 横バンド本数 にスピンボタンを追加し数値入力できるように
    (直接入力はそのまま)
    作業者を選ぶ: スクロールなしで見れるように並べてください

【約束】
    ・3つの欄だけに ▲▼ が付く。欄は打てるまま(type="text")で、ボタンは Tab で止まらない
    ・下限と上限はサーバが決める(締枚数は 1 から、縦バンドは 2 本まで)
    ・作業者を選ぶ窓は広い窓で、班を横に並べる(CSS の後勝ちで幅が戻らないこと)
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SPIN_NAMES = ("count", "vertical_bands", "horizontal_bands")


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class GwSpinTests(WebTestCase):
    def html(self) -> str:
        return self.get("/gw").get_data(as_text=True)

    def field(self, html: str, name: str) -> str:
        """欄 `dim-<name>` の label 1つぶん。"""
        start = html.index(f'id="dim-{name}"')
        return html[html.rindex("<label", 0, start):html.index("</label>", start)]

    def test_3つの欄だけにスピンボタン(self) -> None:
        html = self.html()
        for name in SPIN_NAMES:
            part = self.field(html, name)
            self.assertIn('class="spin"', part, name)
            self.assertIn('data-spin="1"', part)
            self.assertIn('data-spin="-1"', part)
            self.assertEqual(part.count('tabindex="-1"'), 2)      # Tab は欄から欄へ
            self.assertRegex(part, rf'id="dim-{name}"[^>]*type="text"')  # 打てるまま
        for name in ("thickness_mm", "width_mm", "length_mm", "pack_height_mm", "pallet_weight_kg"):
            self.assertNotIn('class="spin"', self.field(html, name), name)

    def test_下限と上限(self) -> None:
        html = self.html()
        count, vertical, horizontal = (self.field(html, n) for n in SPIN_NAMES)
        self.assertIn('data-spin-min="1"', count)
        self.assertNotIn("data-spin-max", count)
        self.assertIn('data-spin-min="0"', vertical)
        self.assertIn('data-spin-max="2"', vertical)
        self.assertIn('data-spin-min="0"', horizontal)
        self.assertNotIn("data-spin-max", horizontal)

    def test_画面のJSが配線する(self) -> None:
        js = (ROOT / "app" / "static" / "js" / "views" / "gw.js").read_text(encoding="utf-8")
        self.assertIn("function stepSpin(", js)
        self.assertRegex(js, r"wireSpins\(\);")
        self.assertIn('"ArrowUp"', js)


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class StaffModalLayoutTests(WebTestCase):
    def test_広い窓で班を横に並べる(self) -> None:
        html = self.get("/").get_data(as_text=True)
        start = html.index('id="staff-modal"')
        modal = html[start:html.index('id="staff-teams"', start) + 80]
        self.assertIn('class="modal__box modal__box--wide"', modal)
        self.assertIn('class="staff-teams"', modal)

    def test_広い窓の幅はモーダルの決まりより後に書く(self) -> None:
        css = (ROOT / "app" / "static" / "css" / "components.css").read_text(encoding="utf-8")
        base = re.search(r"^\.modal__box\{", css, re.M)
        wide = re.search(r"^\.modal__box--wide\{", css, re.M)
        self.assertIsNotNone(base)
        self.assertIsNotNone(wide)
        self.assertGreater(wide.start(), base.start())      # 同じ強さなら後勝ち
        self.assertRegex(css, r"\.staff-teams\{[^}]*grid-template-columns:repeat\(auto-fit")


if __name__ == "__main__":
    unittest.main()
