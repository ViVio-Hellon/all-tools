import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.models import DetailRecord, HeaderRecord
from nippou.logic.aggregation import (
    ShiftAggregate,
    aggregate_by_date,
    aggregate_day,
    aggregate_shift,
    classify_stop_code,
)


def make_header(**overrides) -> HeaderRecord:
    base = dict(report_date="2026年8月3日", line="L-1", shift="1直", page=1, count="10", weight_kg="1000")
    base.update(overrides)
    return HeaderRecord(**base)


def make_detail(**overrides) -> DetailRecord:
    base = dict(report_date="2026年8月3日", line="L-1", shift="1直", page=1, row_no=1)
    base.update(overrides)
    return DetailRecord(**base)


class ClassifyStopCodeTests(unittest.TestCase):
    def test_blank_is_empty(self) -> None:
        self.assertEqual(classify_stop_code(""), "空")
        self.assertEqual(classify_stop_code("   "), "空")

    def test_digit_is_numeric(self) -> None:
        self.assertEqual(classify_stop_code("1"), "数値")
        self.assertEqual(classify_stop_code("0"), "数値")

    def test_full_width_katakana(self) -> None:
        self.assertEqual(classify_stop_code("イ"), "カタカナ")
        self.assertEqual(classify_stop_code("チ"), "カタカナ")

    def test_half_width_katakana(self) -> None:
        self.assertEqual(classify_stop_code("ｲ"), "カタカナ")

    def test_alphabet(self) -> None:
        self.assertEqual(classify_stop_code("A"), "アルファベット")
        self.assertEqual(classify_stop_code("z"), "アルファベット")

    def test_mixed_is_other(self) -> None:
        self.assertEqual(classify_stop_code("1A"), "その他")
        self.assertEqual(classify_stop_code("イA"), "その他")

    def test_kanji_is_other(self) -> None:
        self.assertEqual(classify_stop_code("清掃"), "その他")


class AggregateShiftTests(unittest.TestCase):
    def test_枚数と重量は明細を足して出す(self) -> None:
        header = make_header()
        details = [make_detail(row_no=1, con="7", wei="1000.5"),
                   make_detail(row_no=2, con="5", wei="234.0")]
        agg = aggregate_shift(header, details)
        self.assertEqual(agg.sheet_count, 12)
        self.assertEqual(agg.weight_kg, 1234.5)
        self.assertEqual(agg.weight_ton, 1.2345)

    def test_合計欄が食い違っていても行を信じる(self) -> None:
        """**表を直に書き換えたとき**に食い違う。信じるのは行のほう。"""
        header = make_header(count="999", weight_kg="999")
        agg = aggregate_shift(header, [make_detail(con="7", wei="100")])
        self.assertEqual(agg.sheet_count, 7)
        self.assertEqual(agg.weight_kg, 100)

    def test_work_minutes_sum_tim_across_rows(self) -> None:
        header = make_header()
        details = [make_detail(row_no=i, tim="10") for i in range(1, 4)]
        agg = aggregate_shift(header, details)
        self.assertEqual(agg.work_minutes, 30)

    def test_numeric_code_goes_to_management_loss(self) -> None:
        header = make_header()
        details = [make_detail(s="1", th="20")]
        agg = aggregate_shift(header, details)
        self.assertEqual(agg.management_loss_minutes, 20)
        self.assertEqual(agg.unplanned_stop_minutes, 0)
        self.assertEqual(agg.handling_stop_minutes, 0)

    def test_katakana_code_goes_to_unplanned_stop(self) -> None:
        header = make_header()
        details = [make_detail(ss="イ", ths="15")]
        agg = aggregate_shift(header, details)
        self.assertEqual(agg.unplanned_stop_minutes, 15)
        self.assertEqual(agg.management_loss_minutes, 0)

    def test_alphabet_code_goes_to_handling_stop(self) -> None:
        header = make_header()
        details = [make_detail(sth="A", tht="5")]
        agg = aggregate_shift(header, details)
        self.assertEqual(agg.handling_stop_minutes, 5)

    def test_all_three_pairs_on_one_row(self) -> None:
        header = make_header()
        details = [make_detail(s="1", th="10", ss="イ", ths="20", sth="A", tht="30")]
        agg = aggregate_shift(header, details)
        self.assertEqual(agg.management_loss_minutes, 10)
        self.assertEqual(agg.unplanned_stop_minutes, 20)
        self.assertEqual(agg.handling_stop_minutes, 30)

    def test_blank_or_mixed_code_is_excluded(self) -> None:
        header = make_header()
        details = [
            make_detail(s="", th="10"),  # 空コードは無視
            make_detail(s="1A", th="20"),  # 混在も無視
        ]
        agg = aggregate_shift(header, details)
        self.assertEqual(agg.management_loss_minutes, 0)
        self.assertEqual(agg.total_stop_minutes, 0)

    def test_zero_minutes_pair_does_not_raise(self) -> None:
        header = make_header()
        details = [make_detail(s="", th="")]
        agg = aggregate_shift(header, details)  # 例外にならないことを確認
        self.assertEqual(agg.total_stop_minutes, 0)


class DerivedMetricsTests(unittest.TestCase):
    def test_operating_time_and_rate(self) -> None:
        agg = ShiftAggregate(
            report_date="d", line="L-1", shift="1直",
            management_loss_minutes=60, unplanned_stop_minutes=30, handling_stop_minutes=10,
        )
        self.assertEqual(agg.total_stop_minutes, 100)
        self.assertEqual(agg.operating_minutes, 1440 - 100)
        self.assertAlmostEqual(agg.operating_rate_pct, (1440 - 100) / 1440 * 100)

    def test_operational_minutes_only_subtracts_management_loss(self) -> None:
        agg = ShiftAggregate(
            report_date="d", line="L-1", shift="1直",
            management_loss_minutes=60, unplanned_stop_minutes=30, handling_stop_minutes=10,
        )
        self.assertEqual(agg.operational_minutes, 1440 - 60)

    def test_productivity_t_per_h(self) -> None:
        agg = ShiftAggregate(report_date="d", line="L-1", shift="1直", weight_kg=2000)
        # 停止なし: 稼働時間=1440分=24h, 重量=2t -> 生産性=2/24
        self.assertAlmostEqual(agg.productivity_t_per_h, 2 / 24)

    def test_productivity_is_zero_when_fully_stopped(self) -> None:
        agg = ShiftAggregate(
            report_date="d", line="L-1", shift="1直", weight_kg=2000,
            management_loss_minutes=1440,
        )
        self.assertEqual(agg.operating_minutes, 0)
        self.assertEqual(agg.productivity_t_per_h, 0.0)


class AggregateDayTests(unittest.TestCase):
    def test_one_row_per_shift(self) -> None:
        records = [
            (make_header(shift="1直", count="5", weight_kg="500"), []),
            (make_header(shift="2直", count="7", weight_kg="700"), []),
        ]
        rows = aggregate_day(records)
        self.assertEqual([r.shift for r in rows], ["1直", "2直"])

    def test_multiple_pages_of_same_shift_are_summed(self) -> None:
        records = [
            (make_header(shift="1直", page=1),
             [make_detail(page=1, con="5", wei="500", tim="10")]),
            (make_header(shift="1直", page=2),
             [make_detail(page=2, con="3", wei="300", tim="15")]),
        ]
        rows = aggregate_day(records)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].sheet_count, 8)
        self.assertEqual(rows[0].weight_kg, 800)
        self.assertEqual(rows[0].work_minutes, 25)

    def test_rows_are_ordered_1_2_3_shift(self) -> None:
        records = [
            (make_header(shift="3直"), []),
            (make_header(shift="1直"), []),
            (make_header(shift="2直"), []),
        ]
        rows = aggregate_day(records)
        self.assertEqual([r.shift for r in rows], ["1直", "2直", "3直"])

    def test_empty_input_returns_empty_list(self) -> None:
        self.assertEqual(aggregate_day([]), [])


class AggregateByDateTests(unittest.TestCase):
    def test_one_row_per_calendar_date(self) -> None:
        records = [
            (make_header(report_date="2026年8月1日", shift="1直", count="5", weight_kg="500"), []),
            (make_header(report_date="2026年8月2日", shift="1直", count="7", weight_kg="700"), []),
        ]
        rows = aggregate_by_date(records)
        self.assertEqual([r.report_date for r in rows], ["2026年8月1日", "2026年8月2日"])

    def test_multiple_shifts_of_same_day_are_summed(self) -> None:
        records = [
            (make_header(shift="1直"),
             [make_detail(shift="1直", con="5", wei="500", tim="10")]),
            (make_header(shift="2直"),
             [make_detail(shift="2直", con="3", wei="300", tim="15")]),
        ]
        rows = aggregate_by_date(records)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].sheet_count, 8)
        self.assertEqual(rows[0].weight_kg, 800)
        self.assertEqual(rows[0].work_minutes, 25)
        self.assertEqual(rows[0].shift, "全直")

    def test_rows_are_ordered_by_calendar_date_across_day_counts(self) -> None:
        # "8月30日"のような非ゼロ埋め日付が正しい暦順で並ぶこと
        records = [
            (make_header(report_date="2026年8月30日"), []),
            (make_header(report_date="2026年8月3日"), []),
            (make_header(report_date="2026年8月9日"), []),
        ]
        rows = aggregate_by_date(records)
        self.assertEqual(
            [r.report_date for r in rows],
            ["2026年8月3日", "2026年8月9日", "2026年8月30日"],
        )

    def test_empty_input_returns_empty_list(self) -> None:
        self.assertEqual(aggregate_by_date([]), [])


if __name__ == "__main__":
    unittest.main()
