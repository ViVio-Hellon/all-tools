"""祝日判定 (holiday.py) が VBA 版と同じ結果を返すか確認する。"""

import datetime as dt
import unittest

from calendar_app.holiday import holiday_name, is_holiday


class HolidayTest(unittest.TestCase):
    def assert_holiday(self, year: int, month: int, day: int, expected: str) -> None:
        got = holiday_name(dt.date(year, month, day))
        self.assertEqual(got, expected, f"{year}/{month}/{day}")

    def test_fixed_holidays(self) -> None:
        self.assert_holiday(2026, 1, 1, "元日")
        self.assert_holiday(2026, 2, 11, "建国記念の日")
        self.assert_holiday(2026, 5, 3, "憲法記念日")
        self.assert_holiday(2026, 5, 5, "こどもの日")
        self.assert_holiday(2026, 11, 3, "文化の日")
        self.assert_holiday(2026, 11, 23, "勤労感謝の日")

    def test_happy_monday(self) -> None:
        """ハッピーマンデー制度の第 N 月曜日判定。"""
        self.assert_holiday(2026, 1, 12, "成人の日")  # 第2月曜
        self.assert_holiday(2026, 7, 20, "海の日")  # 第3月曜
        self.assert_holiday(2026, 9, 21, "敬老の日")  # 第3月曜
        self.assert_holiday(2026, 10, 12, "スポーツの日")  # 第2月曜
        # 制度導入前は固定日だった
        self.assert_holiday(1999, 1, 15, "成人の日")
        self.assert_holiday(1999, 1, 11, "")

    def test_equinox(self) -> None:
        self.assert_holiday(2026, 3, 20, "春分の日")
        self.assert_holiday(2026, 9, 23, "秋分の日")

    def test_kokumin_no_kyujitsu(self) -> None:
        """敬老の日と秋分の日に挟まれた火曜日は国民の休日。"""
        self.assert_holiday(2026, 9, 22, "国民の休日")

    def test_substitute_holiday(self) -> None:
        """日曜が祝日の場合、翌月曜が振替休日になる。"""
        self.assert_holiday(2026, 5, 6, "振替休日")  # 5/3(日)の振替
        self.assert_holiday(2024, 2, 12, "振替休日")  # 2/11(日)の振替
        # 振替休日施行日(1973/4/12)より前は振替休日にならない
        self.assertEqual(holiday_name(dt.date(1972, 1, 3)), "")

    def test_2020_olympic_moves(self) -> None:
        """五輪特措法による 2020 年の祝日移動。"""
        self.assert_holiday(2020, 7, 23, "海の日")
        self.assert_holiday(2020, 7, 24, "スポーツの日")
        self.assert_holiday(2020, 8, 10, "山の日")
        self.assert_holiday(2020, 10, 12, "")  # 移動したので祝日ではない
        self.assert_holiday(2020, 7, 20, "")  # 本来の海の日も祝日ではない

    def test_2019_imperial_transition(self) -> None:
        """2019 年の即位関連の休日。"""
        self.assert_holiday(2019, 4, 30, "国民の休日")
        self.assert_holiday(2019, 5, 1, "即位の日")
        self.assert_holiday(2019, 5, 2, "国民の休日")
        self.assert_holiday(2019, 10, 22, "即位礼正殿の儀")

    def test_emperor_birthday_changes(self) -> None:
        """天皇誕生日は代替わりで移動している。"""
        self.assert_holiday(2018, 12, 23, "天皇誕生日")  # 平成
        self.assert_holiday(2019, 12, 23, "")  # 2019 年には無い
        self.assert_holiday(2020, 2, 23, "天皇誕生日")  # 令和
        self.assert_holiday(1988, 4, 29, "天皇誕生日")  # 昭和
        self.assert_holiday(2026, 4, 29, "昭和の日")

    def test_before_holiday_law(self) -> None:
        """祝日法施行(1948/7/20)以前は祝日なし。"""
        self.assertEqual(holiday_name(dt.date(1948, 1, 1)), "")

    def test_plain_weekday(self) -> None:
        self.assertEqual(holiday_name(dt.date(2026, 7, 21)), "")
        self.assertFalse(is_holiday(dt.date(2026, 7, 21)))
        self.assertTrue(is_holiday(dt.date(2026, 1, 1)))

    def test_accepts_datetime(self) -> None:
        """datetime を渡しても日付として扱う。"""
        self.assertEqual(holiday_name(dt.datetime(2026, 1, 1, 13, 30)), "元日")


if __name__ == "__main__":
    unittest.main()
