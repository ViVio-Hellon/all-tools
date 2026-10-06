import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.connection import connect
from nippou.db.models import DetailRecord, HeaderRecord
from nippou.db.repository import NippouRepository


def make_header(**overrides) -> HeaderRecord:
    base = dict(report_date="2026年8月3日", line="L-1", shift="1直", page=1, worker="山田")
    base.update(overrides)
    return HeaderRecord(**base)


def make_details(report_date: str, line: str, shift: str, page: int, count: int = 12) -> list[DetailRecord]:
    return [
        DetailRecord(report_date=report_date, line=line, shift=shift, page=page, row_no=i, lot=f"LOT{i}")
        for i in range(1, count + 1)
    ]


class RepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmpdir.name) / "test.sqlite3")
        self.repo = NippouRepository(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmpdir.cleanup()

    def test_save_then_load_round_trips(self) -> None:
        header = make_header()
        details = make_details(header.report_date, header.line, header.shift, header.page)
        self.repo.save(header, details)

        loaded = self.repo.load(header.report_date, header.line, header.shift, header.page)
        self.assertIsNotNone(loaded)
        loaded_header, loaded_details = loaded
        self.assertEqual(loaded_header.worker, "山田")
        self.assertEqual(len(loaded_details), 12)
        self.assertEqual(loaded_details[0].lot, "LOT1")

    def test_save_overwrites_same_key(self) -> None:
        header = make_header()
        self.repo.save(header, make_details(header.report_date, header.line, header.shift, header.page))
        header2 = make_header(worker="佐藤")
        self.repo.save(header2, make_details(header.report_date, header.line, header.shift, header.page, count=3))

        loaded_header, loaded_details = self.repo.load(header.report_date, header.line, header.shift, header.page)
        self.assertEqual(loaded_header.worker, "佐藤")
        self.assertEqual(len(loaded_details), 3)

    def test_load_missing_key_returns_none(self) -> None:
        self.assertIsNone(self.repo.load("nope", "L-1", "1直", 1))

    def test_page_count_and_latest_page(self) -> None:
        for page in (1, 2, 3):
            header = make_header(page=page)
            self.repo.save(header, make_details(header.report_date, header.line, header.shift, page))
        self.assertEqual(self.repo.page_count("2026年8月3日", "L-1", "1直"), 3)
        self.assertEqual(self.repo.latest_page("2026年8月3日", "L-1", "1直"), 3)

    def test_save_marks_dirty_and_sync_clears_it(self) -> None:
        header = make_header()
        self.repo.save(header, make_details(header.report_date, header.line, header.shift, header.page))
        pending = self.repo.pending_sync_headers()
        self.assertEqual(len(pending), 1)
        self.assertTrue(pending[0].dirty)

        self.repo.mark_synced(header.key())
        self.assertEqual(self.repo.pending_sync_headers(), [])

    def test_list_keys_sorted_desc(self) -> None:
        for d, page in (("2026年8月1日", 1), ("2026年8月3日", 1), ("2026年8月2日", 1)):
            header = make_header(report_date=d, page=page)
            self.repo.save(header, make_details(d, header.line, header.shift, page))
        keys = self.repo.list_keys()
        self.assertEqual([k[0] for k in keys], ["2026年8月3日", "2026年8月2日", "2026年8月1日"])

    def test_list_keys_sorted_desc_across_non_padded_day_counts(self) -> None:
        # report_dateはゼロ埋めされない("8月30日"のような)文字列なので、
        # 単純な文字列比較だと "8月30日" が "8月3日" より前に来てしまう
        # (bug)。日付としてパースして正しく並ぶことを確認する。
        for d in ("2026年8月3日", "2026年8月30日", "2026年8月9日"):
            header = make_header(report_date=d)
            self.repo.save(header, make_details(d, header.line, header.shift, header.page))
        keys = self.repo.list_keys()
        self.assertEqual([k[0] for k in keys], ["2026年8月30日", "2026年8月9日", "2026年8月3日"])

    def test_list_headers_for_date_orders_by_shift_then_page(self) -> None:
        for shift, page in (("2直", 1), ("1直", 2), ("1直", 1)):
            header = make_header(shift=shift, page=page)
            self.repo.save(header, make_details(header.report_date, header.line, shift, page))
        records = self.repo.list_headers_for_date("2026年8月3日")
        self.assertEqual(
            [(h.shift, h.page) for h, _ in records],
            [("1直", 1), ("1直", 2), ("2直", 1)],
        )

    def test_list_headers_for_date_filters_by_line(self) -> None:
        self.repo.save(make_header(line="L-1"), make_details("2026年8月3日", "L-1", "1直", 1))
        self.repo.save(make_header(line="L2"), make_details("2026年8月3日", "L2", "1直", 1))
        records = self.repo.list_headers_for_date("2026年8月3日", line="L-1")
        self.assertEqual([h.line for h, _ in records], ["L-1"])

    def test_list_headers_for_date_returns_matching_details(self) -> None:
        header = make_header()
        self.repo.save(header, make_details(header.report_date, header.line, header.shift, header.page, count=3))
        records = self.repo.list_headers_for_date(header.report_date)
        self.assertEqual(len(records), 1)
        _, details = records[0]
        self.assertEqual(len(details), 3)

    def test_list_headers_between_filters_by_date_range(self) -> None:
        for d in ("2026年7月31日", "2026年8月1日", "2026年8月15日", "2026年9月1日"):
            header = make_header(report_date=d)
            self.repo.save(header, make_details(d, header.line, header.shift, header.page))
        records = self.repo.list_headers_between(date(2026, 8, 1), date(2026, 8, 31))
        self.assertEqual([h.report_date for h, _ in records], ["2026年8月1日", "2026年8月15日"])

    def test_list_headers_between_orders_by_date_across_day_counts(self) -> None:
        # "8月30日"と"8月3日"のような非ゼロ埋め日付が正しい暦順で返ること
        for d in ("2026年8月30日", "2026年8月3日", "2026年8月9日"):
            header = make_header(report_date=d)
            self.repo.save(header, make_details(d, header.line, header.shift, header.page))
        records = self.repo.list_headers_between(date(2026, 8, 1), date(2026, 8, 31))
        self.assertEqual(
            [h.report_date for h, _ in records],
            ["2026年8月3日", "2026年8月9日", "2026年8月30日"],
        )

    def test_list_headers_between_filters_by_line(self) -> None:
        self.repo.save(make_header(line="L-1"), make_details("2026年8月3日", "L-1", "1直", 1))
        self.repo.save(make_header(line="L2"), make_details("2026年8月3日", "L2", "1直", 1))
        records = self.repo.list_headers_between(date(2026, 8, 1), date(2026, 8, 31), line="L2")
        self.assertEqual([h.line for h, _ in records], ["L2"])

    def test_shift_time_config_round_trips(self) -> None:
        self.repo.set_shift_time("1", "08:00", "17:00")
        self.repo.set_shift_time("2", "17:00", "22:00")
        times = self.repo.get_shift_times()
        self.assertEqual(times["1"], ("08:00", "17:00"))
        self.assertEqual(times["2"], ("17:00", "22:00"))

    def test_締めた印が残る(self) -> None:
        """`print_status` の表。**「刷ったか」ではなく「締めたか」。**"""
        self.assertFalse(self.repo.is_shift_closed("1直", date(2026, 8, 3), "L-1"))
        self.repo.mark_shift_closed("1直", date(2026, 8, 3), "L-1")
        self.assertTrue(self.repo.is_shift_closed("1直", date(2026, 8, 3), "L-1"))
        self.assertFalse(self.repo.is_shift_closed("1直", date(2026, 8, 4), "L-1"))


if __name__ == "__main__":
    unittest.main()
