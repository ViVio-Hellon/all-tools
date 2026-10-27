"""画面の心拍を**本物のブラウザ**で ── 画面の時計が後ろへ飛んでも「戻りました」を言う

表に戻った・フォーカスした・凍結から戻った(`resume`)は続けて起きるので、画面は
「戻りました」を1秒のうちに1つへ束ねる。以前は束ねるのを画面の時計(`Date.now`)で
数えていたので、時刻合わせで時計が後ろへ飛ぶと「さっき送ったばかり」と思い込み、
**時計が追いつくまで戻ったことを言わなかった**(裏に回っていた画面が表に戻っても、
サーバは裏のままだと思っている)。束ねるのは `performance.now()` で数える。
"""
from __future__ import annotations

import unittest

from tests._page_browser import PageBrowserCase


class ResumeSignalBrowserTests(PageBrowserCase):
    def resumed(self) -> list[dict]:
        return [b for b in self.posts_to("/api/alive") if b.get("resumed")]

    def open_and_come_back(self):
        page = self.open()
        self.wait_until(page, lambda: self.posts_to("/api/alive"), "最初の心拍")
        page.evaluate("window.dispatchEvent(new Event('focus'))")
        self.wait_until(page, lambda: self.resumed(), "戻りましたの合図")
        return page

    def after_a_second(self, page) -> None:
        """束ねる間(1秒)を過ぎるまで待つ(画面の `performance.now()` が進むのを見る)。"""
        page.wait_for_function("(t) => performance.now() > t + 1100", arg=page.evaluate("performance.now()"))

    def test_続けて起きた戻りは1つに束ねる(self) -> None:
        page = self.open_and_come_back()
        self.after_a_second(page)
        before = len(self.resumed())
        page.evaluate("window.dispatchEvent(new Event('focus'));"
                      "document.dispatchEvent(new Event('resume'));"
                      "window.dispatchEvent(new Event('focus'))")
        self.wait_until(page, lambda: len(self.resumed()) > before, "戻りましたの合図")
        # 束ねたぶんが遅れて着いていないかを見るため、1秒たってからもう1度戻る(これは届く)
        self.after_a_second(page)
        page.evaluate("window.dispatchEvent(new Event('focus'))")
        self.wait_until(page, lambda: len(self.resumed()) > before + 1, "1秒たってからの戻り")
        self.assertEqual(len(self.resumed()), before + 2, "続けて起きた戻りを束ねていない")

    def test_画面の時計が後ろへ飛んでも戻りましたを言う(self) -> None:
        page = self.open_and_come_back()
        before = len(self.resumed())
        page.evaluate("(() => { const real = Date.now; Date.now = () => real() - 3600e3; })()")
        self.after_a_second(page)
        page.evaluate("window.dispatchEvent(new Event('focus'))")
        self.wait_until(page, lambda: len(self.resumed()) > before,
                        "画面の時計が1時間戻ったあとの「戻りました」")
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
