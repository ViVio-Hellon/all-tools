"""「業者に注文できない日です」の帯を、本物のブラウザで。**開いたまま日付が変わったら出し直す。**

時計(:func:`kanban.closed_day.today`)を差し替えて、金曜 → 土曜 → 月曜 と進める。画面は読み直さない
(数秒おきの ``/api/status`` だけで出る・消える)。

無い環境では飛ばします(CI では飛ばしません: ``ALLTOOLS_REQUIRE_BROWSER=1``)。
"""
from __future__ import annotations

import datetime as dt
from unittest import mock

from kanban import closed_day, config
from tests._kanban_browser import KanbanBrowserCase


class ClosedDayBannerBrowserTest(KanbanBrowserCase):
    MODE = config.MODE_SITE
    LINE = "LVC"

    def setUp(self) -> None:
        super().setUp()
        self.day = dt.date(2026, 11, 6)                     # 金曜
        patcher = mock.patch.object(closed_day, "today", side_effect=lambda: self.day)
        patcher.start()
        self.addCleanup(patcher.stop)

    def status_calls(self) -> int:
        return sum(1 for method, path, _ in self.sent if method == "GET" and path.startswith("/api/status"))

    def wait_status(self, page) -> None:
        """次の ``/api/status`` が返って描かれるまで(2 回進めば、前の答えは描き終わっている)。"""
        start = self.status_calls()
        self.wait_until(page, lambda: self.status_calls() >= start + 2, "状態を 2 回読んだ", timeout=20)

    def test_開いたまま日付が変わると帯が出て_平日に戻ると消える(self) -> None:
        page = self.open("/board")
        page.wait_for_selector('#board .row[data-no="2"]')
        self.wait_status(page)
        self.assertFalse(self.visible(page, "#closed-day"), "金曜に帯が出ている")

        self.day = dt.date(2026, 11, 7)                     # 日付が変わって土曜
        page.wait_for_selector("#closed-day:not([hidden])", timeout=20000)
        self.assertEqual(self.text(page, "#closed-day"), "今日は土曜日です。業者に注文できない日です。")

        self.day = dt.date(2026, 11, 3)                     # 祝日(文化の日)
        page.wait_for_function(
            "document.getElementById('closed-day').textContent.includes('文化の日')", timeout=20000)

        self.day = dt.date(2026, 11, 9)                     # 月曜
        page.wait_for_selector("#closed-day", state="hidden", timeout=20000)
        self.assertEqual(self.errors, [])
