"""土曜・日曜・祝日の「発注のできない日です」の案内(:mod:`kanban.closed_day`)。

現場の要望: 「土曜日曜祝日は現場倉庫ともに案内を出してほしい(簡易でいい)。発注のできない日だよ、
ぐらいが分かればいい」

    - 土・日・祝日(振替休日・国民の休日・五輪の年の移動を含む)は帯が出る。平日は出ない
    - 看板の画面(現場・倉庫・倉庫参照)のどれでも出る。発注は止めない(案内だけ)
    - 開いたままでも日付が変われば出し直す・消す(``/api/status``。時計を差し替えて確かめる)
    - 祝日の計算はカレンダーの写し。元と中身が離れていない
"""
from __future__ import annotations

import datetime as dt
import re
import unittest
from pathlib import Path
from unittest import mock

from kanban import closed_day, config, holiday
from tests.test_web_app import HAS_FLASK, RouteTestBase

D = dt.date.fromisoformat


class NoticeTest(unittest.TestCase):
    def test_weekend(self):
        self.assertEqual(closed_day.notice(D("2026-11-07")), "今日は土曜日です。発注のできない日です。")
        self.assertEqual(closed_day.notice(D("2026-11-08")), "今日は日曜日です。発注のできない日です。")

    def test_weekdays_have_no_notice(self):
        for day in ("2026-11-02", "2026-11-04", "2026-11-05", "2026-11-06", "2021-10-11"):
            self.assertEqual(closed_day.notice(D(day)), "", day)

    def test_holidays(self):
        self.assertEqual(closed_day.notice(D("2026-11-03")), "今日は祝日(文化の日)です。発注のできない日です。")
        self.assertIn("祝日(秋分の日)", closed_day.notice(D("2026-09-23")))
        self.assertIn("祝日(成人の日)", closed_day.notice(D("2030-01-14")))

    def test_substitute_and_national_holidays(self):
        # 日曜の祝日の次の月曜(振替休日)・5/3 が日曜の年の 5/6・祝日に挟まれた日(国民の休日)
        self.assertEqual(closed_day.notice(D("2025-11-24")), "今日は振替休日です。発注のできない日です。")
        self.assertEqual(closed_day.notice(D("2024-02-12")), "今日は振替休日です。発注のできない日です。")
        self.assertEqual(closed_day.notice(D("2026-05-06")), "今日は振替休日です。発注のできない日です。")
        self.assertEqual(closed_day.notice(D("2026-09-22")), "今日は国民の休日です。発注のできない日です。")

    def test_special_years(self):
        """2019 の即位まわり・2020/2021 の五輪の移動も元(カレンダー)のとおり。"""
        for day, name in (("2019-04-30", "国民の休日"), ("2019-05-01", "即位の日"), ("2019-05-02", "国民の休日"),
                          ("2020-07-23", "海の日"), ("2020-07-24", "スポーツの日"), ("2020-08-10", "山の日"),
                          ("2021-07-22", "海の日"), ("2021-07-23", "スポーツの日"), ("2021-08-08", "山の日"),
                          ("2021-08-09", "振替休日")):
            self.assertIn(name, closed_day.notice(D(day)), day)
        # 移った先の元の日は平日(2020/2021 のスポーツの日・海の日の元の日)
        for day in ("2020-10-12", "2021-10-11", "2021-07-19", "2020-07-20"):
            self.assertEqual(closed_day.notice(D(day)), "", day)

    def test_today_comes_from_the_clock(self):
        with mock.patch.object(closed_day, "today", return_value=D("2026-11-07")):
            self.assertIn("土曜日", closed_day.notice())
        with mock.patch.object(closed_day, "today", return_value=D("2026-11-02")):
            self.assertEqual(closed_day.notice(), "")


class CopyMatchesCalendarTest(unittest.TestCase):
    """祝日の計算は ``tools/calendar/calendar_app/holiday.py`` の写し。**元と中身が離れていない。**"""

    SOURCE = Path(__file__).resolve().parents[2] / "calendar" / "calendar_app" / "holiday.py"

    @staticmethod
    def _body(text: str) -> str:
        # 冒頭の説明(写しの断り書き)を除いた中身
        return re.sub(r'^""".*?"""', "", text, count=1, flags=re.S).strip()

    def test_same_logic_as_calendar(self):
        if not self.SOURCE.is_file():
            self.skipTest("カレンダーが同じリポジトリに無い(看板だけ配った形)")
        mine = Path(holiday.__file__).read_text(encoding="utf-8")
        self.assertIn("tools/calendar/calendar_app/holiday.py", mine, "写した元を書いていない")
        self.assertEqual(self._body(mine), self._body(self.SOURCE.read_text(encoding="utf-8")),
                         "カレンダーの祝日の計算と中身が違う(直すときは両方を直す)")


@unittest.skipUnless(HAS_FLASK, "Flask が無い")
class BoardBannerTest(RouteTestBase):
    """看板の画面の帯。現場・倉庫・倉庫参照のどれでも出す。"""

    def _page(self, day: str) -> str:
        with mock.patch.object(closed_day, "today", return_value=D(day)):
            return self.get("/board").get_data(as_text=True)

    def _status(self, day: str) -> str:
        with mock.patch.object(closed_day, "today", return_value=D(day)):
            return self.get("/api/status").json["closed_day"]

    def _banner(self, html: str) -> re.Match:
        found = re.search(r'<div class="banner banner--warn" id="closed-day"[^>]*>([^<]*)</div>', html)
        self.assertIsNotNone(found, "帯の置き場所が無い")
        return found

    def test_banner_on_closed_days_only(self):
        for mode, line in ((config.MODE_SITE, "LVC"), (config.MODE_WAREHOUSE, ""),
                           (config.MODE_WAREHOUSE_VIEW, "")):
            with self.subTest(mode=mode):
                self.app.config.update(MODE=mode, LINE=line)
                for day in ("2026-11-07", "2026-11-08", "2026-11-03", "2025-11-24"):
                    banner = self._banner(self._page(day))
                    self.assertNotIn("hidden", banner.group(0), day)
                    self.assertIn("発注のできない日です", banner.group(1), day)
                    self.assertIn("発注のできない日です", self._status(day), day)
                banner = self._banner(self._page("2026-11-02"))
                self.assertIn("hidden", banner.group(0))
                self.assertEqual(banner.group(1), "")
                self.assertEqual(self._status("2026-11-02"), "")

    def test_ordering_is_not_stopped(self):
        """案内だけ。土曜でも発注は通る。"""
        with mock.patch.object(closed_day, "today", return_value=D("2026-11-07")):
            res = self.post("/api/board/order", {"line": "LVC", "mgmt_no": "1", "rev": self.rev("1")})
        self.assertEqual(res.status_code, 200, res.json)


if __name__ == "__main__":
    unittest.main()
