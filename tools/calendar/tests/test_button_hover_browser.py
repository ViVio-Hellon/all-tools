"""色の付いたボタンは、**マウスを乗せても文字が読める** (VER4.2.5。本物のブラウザ)

一般のボタンの「乗ったときの地」(`.btn:hover`)が、色の付いたボタン(`.btn--primary` など)の地より強く
効いていました。白い文字が淡い地に乗り、連絡の窓で「次へ」を押した直後(マウスが同じ所に残る)
「登録する」の文字が消えていた(説明書の写真を撮り直すときに見つかった)。看板は前に同じ直しが入っている。
"""
from __future__ import annotations

import unittest

from tests._page_browser import PageBrowserCase
from tests.test_theme import _contrast

_HOVERED = """async (cls) => {
    const host = document.querySelector(cls.includes('hist') ? '.cal' : 'main') || document.body;
    if (cls.includes('hist')) host.classList.add('cal--history');
    const b = document.createElement('button');
    b.className = cls.replace('hist ', ''); b.textContent = '登録する'; b.id = 'probe-btn';
    b.style.position = 'fixed'; b.style.top = '40px'; b.style.left = '40px'; b.style.zIndex = 9999;
    host.appendChild(b);
    return true;
}"""


def _hex(rgb: str) -> str:
    parts = [int(float(p)) for p in rgb[rgb.index("(") + 1:rgb.index(")")].split(",")[:3]]
    return "#" + "".join(f"{p:02x}" for p in parts)


class ButtonHoverTests(PageBrowserCase):
    def test_色の付いたボタンにマウスを乗せても文字が読める(self) -> None:
        for scheme in ("light", "dark"):
            for cls in ("btn btn--primary", "btn btn--danger", "hist btn btn--primary"):
                with self.subTest(scheme=scheme, cls=cls):
                    page = self.open("/calendar", self.new_context(color_scheme=scheme))
                    page.evaluate(_HOVERED, cls)
                    page.hover("#probe-btn")
                    page.wait_for_timeout(200)
                    got = page.evaluate("""() => { const s = getComputedStyle(document.getElementById('probe-btn'));
                        return [s.color, s.backgroundColor]; }""")
                    ratio = _contrast(_hex(got[0]), _hex(got[1]))
                    self.assertGreaterEqual(ratio, 3.0, f"{scheme} {cls}: 文字 {got[0]} / 地 {got[1]} = {ratio:.2f}")
                    page.context.close()
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
