"""日報入力: 帯の主語・ページの案内・畳んだ説明を本物のブラウザで (段階1)

前は `entry.js` / `components.css` / `entry.html` の字を探していました(`test_band_subject` の
`test_塗り直しでも主語を入れる` と畳んだ説明の CSS の3本・`test_page_guide` の
`test_画面に案内の箱と戻り方の説明` の JS の部分)。ここでは打って、塗り直させて確かめます:

    - 応答で帯を塗り直したとき、主語の札(「前の直 ◯◯ のこと」「いまの直」)も新しくなる
    - ページの案内は応答のたびに塗り直し、「次ページ発行」の確かめは**サーバの文言**で訊く。
      案内の「最新のページに戻る」で戻る
    - 畳んだ説明は、乗せたとき・キーボードで焦点を当てたときだけ残りを出し、
      **下の行を動かさない**(浮かせて出す)

帯・案内の中身は試験が決めます(何を出すかの判断は `test_band_subject` / `test_page_guide` の
Python 側の試験が見ています)。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._screen_browser import ScreenBrowserCase  # noqa: E402

REFUSED = {"error": {"code": "test", "message": "試験で断りました"}}


class EntryBandsTests(ScreenBrowserCase):
    def answer_with(self, change) -> None:
        """日報入力の答え(画面ぜんぶ)を、塗り直す前に少し変える。"""
        def edit(body):
            if isinstance(body, dict) and "rows" in body:
                change(body)
            return body
        self.rewrite["/api/entry/"] = edit

    def type_and_leave(self, page, value: str) -> None:
        before = len(self.asked("/api/entry/", "POST"))
        self.type_in(page, "MAI1", value)
        page.locator("#MAI1").press("Tab")
        self.wait_until(page, lambda: len(self.asked("/api/entry/", "POST")) > before, "打った欄の要求")
        page.wait_for_timeout(500)

    def test_塗り直しで帯の主語も新しくなる(self) -> None:
        page = self.open_entry()

        def bands(body) -> None:
            body["handover"] = {**(body.get("handover") or {}), "any_left": True, "blocked": False,
                                "about": "9月20日 2直(試験)", "message": "前の直が共有へ出ていません(試験)"}
            body["shift_findings"] = {**(body.get("shift_findings") or {}), "counted": True,
                                      "about": "10月21日 1直(試験)", "headline": "直すところ(試験)",
                                      "now": [], "at_end": []}
        self.answer_with(bands)
        self.type_and_leave(page, "10")
        self.assertEqual(page.text_content("#handover-about").strip(), "9月20日 2直(試験)")
        self.assertEqual(page.text_content("#shift-findings-about").strip(), "10月21日 1直(試験)")
        self.assertTrue(page.is_visible("#handover"))
        self.assertTrue(page.is_visible("#shift-findings"))

    def test_ページの案内は塗り直し_発行はサーバの文言で訊き_戻るで戻る(self) -> None:
        page = self.open_entry()
        guide = {"mode": "ready", "ready": True, "action": "goto-new-page",
                 "text": "12行目まで埋まりました(試験)", "detail": "",
                 "confirm": "第1ページを保存して、第2ページを出します(試験)。よろしいですか?"}
        self.answer_with(lambda body: body.update(page_guide=dict(guide)))
        with self.subTest("応答で案内を塗り直す"):
            self.type_and_leave(page, "10")
            self.assertTrue(page.is_visible("#page-guide"))
            self.assertEqual(page.get_attribute("#page-guide", "data-mode"), "ready")
            self.assertEqual(page.text_content("#page-guide-text"), guide["text"])
        with self.subTest("次ページ発行はサーバの文言で確かめる"):
            self.answers["/api/entry/newpage"] = (422, REFUSED)
            mark = len(self.dialogs)
            page.click("#new-page")
            self.wait_until(page, lambda: self.asked("/api/entry/newpage", "POST"), "発行の要求")
            self.assertEqual(self.dialogs[mark:mark + 1], [guide["confirm"]])
        with self.subTest("前のページを直しているときは「最新のページに戻る」"):
            guide.update(mode="editing", action="back", text="第1ページを直しています(試験)")
            self.answers["/api/settings/back"] = (200, {"message": "最新のページに戻りました(試験)",
                                                       "next": "/records"})
            self.type_and_leave(page, "11")
            page.click("#page-guide-back")
            self.wait_until(page, lambda: self.asked("/api/settings/back", "POST"), "戻る要求")
            page.wait_for_function("location.pathname === '/records'", timeout=8000)

    def test_畳んだ説明は乗せたときと焦点で開き_下の行を動かさない(self) -> None:
        page = self.open_entry()
        tip = "#stamp-tip"
        more = f"{tip} .tip__more"
        below = page.evaluate(f"""() => {{
            const el = document.querySelector('{tip}').nextElementSibling;
            el.id = el.id || '__below'; return '#' + el.id; }}""")
        top = lambda: page.evaluate(f"document.querySelector('{below}').getBoundingClientRect().top")  # noqa: E731
        before = top()
        self.assertFalse(page.is_visible(more), "畳んだまま出ています")
        with self.subTest("乗せると開く・下の行は動かない"):
            page.hover(f"{tip} .tip__line")
            self.assertTrue(page.is_visible(more))
            self.assertEqual(top(), before, "開くと下の行が動きます")
            page.mouse.move(5, 5)
            self.assertFalse(page.is_visible(more))
        with self.subTest("キーボードで焦点を当てても開く"):
            page.focus(tip)
            self.assertEqual(page.evaluate("document.activeElement.id"), "stamp-tip",
                             "焦点を当てられません(tabindex が要ります)")
            self.assertTrue(page.is_visible(more))
            self.assertEqual(top(), before)


if __name__ == "__main__":
    unittest.main()
