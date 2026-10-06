import sys
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic.shift import (
    DEFAULT_SHIFT_TIMES,
    ShiftCloseChecker,
    ShiftCalculator,
    ShiftTimes,
    missing_shift_times,
    parse_business_date,
    shift_times_note,
)


def dt(hour: int, minute: int, day: int = 3) -> datetime:
    return datetime(2026, 8, day, hour, minute)


class ParseBusinessDateTests(unittest.TestCase):
    def test_parses_non_padded_date(self) -> None:
        self.assertEqual(parse_business_date("2026年8月3日"), date(2026, 8, 3))

    def test_parses_double_digit_month_and_day(self) -> None:
        self.assertEqual(parse_business_date("2026年12月31日"), date(2026, 12, 31))

    def test_blank_returns_none(self) -> None:
        self.assertIsNone(parse_business_date(""))

    def test_garbage_returns_none(self) -> None:
        self.assertIsNone(parse_business_date("not a date"))

    def test_invalid_calendar_date_returns_none(self) -> None:
        self.assertIsNone(parse_business_date("2026年2月30日"))

    def test_strips_surrounding_whitespace(self) -> None:
        self.assertEqual(parse_business_date("  2026年8月3日  "), date(2026, 8, 3))


#: このファイルの時刻は、**工場の既定値とは切り離した直**で考えます。
#:
#: `DEFAULT_SHIFT_TIMES` は「マスタを取り込む前でも動かす」ための控えで、
#: 現場の値(07:00-15:00-22:50)に合わせて動きます。ここで見たいのは
#: 境界の**算数**なので、控えが変わるたびに落ちるのでは、どちらが
#: 壊れたのか分かりません。値はここに書いて固定します。
SHEET_TIMES = ShiftTimes(start1="08:00", end1="17:00",
                         start2="17:00", end2="22:00",
                         start3="22:00", end3="08:00",
                         start_day="08:00", end_day="18:00")


class TimeCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calc = ShiftCalculator(SHEET_TIMES)

    def test_morning_is_shift1(self) -> None:
        self.assertEqual(self.calc.time_check(dt(9, 0)), "1直")

    def test_evening_is_shift2(self) -> None:
        self.assertEqual(self.calc.time_check(dt(17, 0)), "2直")
        self.assertEqual(self.calc.time_check(dt(21, 59)), "2直")

    def test_late_night_is_shift3(self) -> None:
        self.assertEqual(self.calc.time_check(dt(23, 30)), "3直")

    def test_early_morning_tail_is_still_shift3(self) -> None:
        self.assertEqual(self.calc.time_check(dt(2, 0)), "3直")

    def test_start1_boundary_falls_through_to_shift3(self) -> None:
        # VBA uses a *strict* '>' against Start1, so exactly 08:00 is not
        # "1直" -- it falls all the way to the 3-shift's end3 catch-all.
        self.assertEqual(self.calc.time_check(dt(8, 0)), "3直")

    def test_forced_day_shift_line_is_always_day_shift(self) -> None:
        self.assertEqual(self.calc.time_check(dt(23, 30), force_day_shift=True), "日勤")


class TodayCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calc = ShiftCalculator(SHEET_TIMES)

    def test_daytime_is_todays_date(self) -> None:
        self.assertEqual(self.calc.today_check(dt(9, 0)), date(2026, 8, 3))

    def test_evening_shift3_is_todays_date(self) -> None:
        self.assertEqual(self.calc.today_check(dt(23, 30)), date(2026, 8, 3))

    def test_early_morning_tail_is_yesterdays_date(self) -> None:
        self.assertEqual(self.calc.today_check(dt(2, 0)), date(2026, 8, 2))


class MinutesUntilShiftEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calc = ShiftCalculator(SHEET_TIMES)

    def test_within_shift1(self) -> None:
        self.assertEqual(self.calc.minutes_until_shift_end(dt(16, 45)), 15)

    def test_shift3_morning_portion_ends_same_day(self) -> None:
        self.assertEqual(self.calc.minutes_until_shift_end(dt(7, 50)), 10)

    def test_shift3_evening_portion_ends_next_day(self) -> None:
        self.assertEqual(self.calc.minutes_until_shift_end(dt(23, 50)), 8 * 60 + 10)

    def test_is_near_shift_end_within_window(self) -> None:
        self.assertTrue(self.calc.is_near_shift_end(dt(16, 50)))
        self.assertFalse(self.calc.is_near_shift_end(dt(16, 30)))


class ShiftCloseCheckerTests(unittest.TestCase):
    """直の終わりの催促と、自動で締める判断。

    VBA の `CheckCurrentShiftPrintForgotten` / `ShouldExecuteAutoPrint`
    ですが、**追いかける相手は「印刷」ではなく「確定」**です(紙は
    作業者が欲しいときだけ出すものにしたため)。
    """

    def setUp(self) -> None:
        self.calc = ShiftCalculator(SHEET_TIMES)
        self.closed: set[tuple[str, date]] = set()
        self.checker = ShiftCloseChecker(
            self.calc, is_shift_closed=lambda s, d: (s, d) in self.closed)

    def test_forgotten_within_warning_window(self) -> None:
        self.assertTrue(self.checker.close_forgotten(dt(16, 50)))

    def test_not_forgotten_outside_window(self) -> None:
        self.assertFalse(self.checker.close_forgotten(dt(12, 0)))

    def test_not_forgotten_once_closed(self) -> None:
        self.closed.add(("1直", date(2026, 8, 3)))
        self.assertFalse(self.checker.close_forgotten(dt(16, 50)))

    def test_催促の文言は紙のことを言わない(self) -> None:
        """紙は任意なので、「印刷をお忘れなく」とは言えない。"""
        text = self.checker.realtime_warning_message(dt(16, 50))
        self.assertIn("1直 終了10分前", text)
        self.assertNotIn("印刷", text)

    def test_should_auto_close_within_5_minutes_with_data(self) -> None:
        self.assertTrue(
            self.checker.should_auto_close(
                dt(16, 56), force_day_shift=False, recall_mode=False,
                already_closed=False, has_pending_data=True,
            )
        )

    def test_should_not_auto_close_without_data(self) -> None:
        self.assertFalse(
            self.checker.should_auto_close(
                dt(16, 56), force_day_shift=False, recall_mode=False,
                already_closed=False, has_pending_data=False,
            )
        )

    def test_should_not_auto_close_during_recall_mode(self) -> None:
        self.assertFalse(
            self.checker.should_auto_close(
                dt(16, 56), force_day_shift=False, recall_mode=True,
                already_closed=False, has_pending_data=True,
            )
        )

class FactoryTimesTests(unittest.TestCase):
    """**控えの値は、現場の直と同じにする。**

    `DEFAULT_SHIFT_TIMES` は「時間マスタを取り込む前でも動かす」ための
    控えです。長らく 08:00-17:00-22:00 が入っていましたが、現場は
    **07:00-15:00-22:50-07:00** です。

    控えが2時間ずれていると、**取り込めていない端末だけが黙って別の
    時刻で動きます** ── 催促も、残り5分の自動確定も、最終時間チェックも、
    全停が書く直の終わりも、全部ずれるのに画面には何も出ません。
    「使う前に取り込むから大丈夫」は、取り込めなかった日に効きません。

    日勤は現場の値が未確認なので、1直の始まりに合わせた8時間を控えて
    あります(分かり次第ここを直す)。
    """

    def test_現場の直と同じ値が入っている(self) -> None:
        t = DEFAULT_SHIFT_TIMES
        self.assertEqual((t.start1, t.end1), ("07:00", "15:00"))
        self.assertEqual((t.start2, t.end2), ("15:00", "22:50"))
        self.assertEqual((t.start3, t.end3), ("22:50", "07:00"))

    def test_直はすき間なく回る(self) -> None:
        """1直の終わり = 2直の始まり。**すき間があるとどの直にも当たらない。**"""
        t = DEFAULT_SHIFT_TIMES
        self.assertEqual(t.end1, t.start2)
        self.assertEqual(t.end2, t.start3)
        self.assertEqual(t.end3, t.start1)

    def test_控えのままでも直が決まる(self) -> None:
        """マスタを取り込む前でも、どの時刻もどれかの直に落ちる。"""
        calc = ShiftCalculator(DEFAULT_SHIFT_TIMES)
        for hour, expected in ((8, "1直"), (14, "1直"), (16, "2直"),
                               (22, "2直"), (23, "3直"), (3, "3直")):
            with self.subTest(hour=hour):
                self.assertEqual(
                    calc.time_check(datetime(2026, 8, 3, hour, 30)), expected)


if __name__ == "__main__":
    unittest.main()


class ShiftTimesSourceTests(unittest.TestCase):
    """**控えで動いているなら、黙らない。**

    値を現場に合わせても、**合っている保証がどこにも無い**ことは変わり
    ません。出どころのほうを画面に出します ── 取り込めていない端末だけ
    が別の時刻で動いていても、いままで誰も気づけませんでした。
    """

    def full(self) -> dict:
        return {"1": ("07:00", "15:00"), "2": ("15:00", "22:50"),
                "3": ("22:50", "07:00")}

    def test_3直ぶんそろっていれば何も言わない(self) -> None:
        self.assertEqual(missing_shift_times(self.full()), [])
        self.assertEqual(shift_times_note(self.full()), "")

    def test_日勤は数えない(self) -> None:
        """1直・2直の代わりに置く直。**入っていないほうが普通。**"""
        self.assertEqual(missing_shift_times(self.full()), [])

    def test_無い直は名前で言う(self) -> None:
        raw = self.full()
        del raw["2"]
        self.assertEqual(missing_shift_times(raw), ["2直"])
        self.assertIn("2直", shift_times_note(raw))

    def test_片側だけでも読めていない扱い(self) -> None:
        """読む側が「片側でも欠ければ控えに落とす」のに合わせる。"""
        raw = self.full()
        raw["3"] = ("22:50", "")
        self.assertEqual(missing_shift_times(raw), ["3直"])

    def test_空なら3直ぶんとも言う(self) -> None:
        self.assertEqual(missing_shift_times({}), ["1直", "2直", "3直"])
        note = shift_times_note({})
        self.assertIn("控え", note)
        # **次にやることまで書く。** 「読めません」だけでは動けない
        self.assertIn("時間マスタを取り込む", note)
        self.assertNotIn("**", note)

    def test_Noneでも落ちない(self) -> None:
        self.assertEqual(missing_shift_times(None), ["1直", "2直", "3直"])
