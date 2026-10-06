import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.connection import connect
from nippou.db.models import DetailRecord, HeaderRecord
from nippou.db.repository import NippouRepository
from nippou.logic.shift import DEFAULT_SHIFT_TIMES, ShiftCalculator
from nippou.services.nippou_service import NippouService


class NippouServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmpdir.name) / "test.sqlite3")
        self.repo = NippouRepository(self.conn)
        self.is_admin = False
        self.confirm_result = True
        self.confirm_calls: list[tuple] = []

        self.service = NippouService(
            self.repo,
            ShiftCalculator(DEFAULT_SHIFT_TIMES),
            is_admin=lambda: self.is_admin,
            confirm_other_shift_edit=self._confirm,
        )

        header = HeaderRecord(report_date="2026年8月2日", line="L-1", shift="2直", page=1, worker="past")
        details = [
            DetailRecord(report_date=header.report_date, line=header.line, shift=header.shift, page=1, row_no=i)
            for i in range(1, 13)
        ]
        self.repo.save(header, details)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmpdir.cleanup()

    def _confirm(self, *args) -> bool:
        self.confirm_calls.append(args)
        return self.confirm_result

    def test_same_shift_recall_needs_no_admin(self) -> None:
        result = self.service.recall_data(
            "2026年8月2日", "L-1", "2直", 1,
            current_line="L-1", current_date="2026年8月2日", current_shift="2直",
        )
        self.assertIsNotNone(result)
        self.assertTrue(self.service.recall.active)

    def test_other_shift_recall_denied_without_admin(self) -> None:
        self.is_admin = False
        result = self.service.recall_data(
            "2026年8月2日", "L-1", "2直", 1,
            current_line="L-1", current_date="2026年8月3日", current_shift="1直",
        )
        self.assertIsNone(result)
        self.assertFalse(self.service.recall.active)

    def test_other_shift_recall_allowed_with_admin_confirmation(self) -> None:
        self.is_admin = True
        self.confirm_result = True
        result = self.service.recall_data(
            "2026年8月2日", "L-1", "2直", 1,
            current_line="L-1", current_date="2026年8月3日", current_shift="1直",
        )
        self.assertIsNotNone(result)
        self.assertEqual(len(self.confirm_calls), 1)

    def test_admin_can_decline_confirmation(self) -> None:
        self.is_admin = True
        self.confirm_result = False
        result = self.service.recall_data(
            "2026年8月2日", "L-1", "2直", 1,
            current_line="L-1", current_date="2026年8月3日", current_shift="1直",
        )
        self.assertIsNone(result)

    def test_other_line_recall_denied_without_admin(self) -> None:
        self.is_admin = False
        result = self.service.recall_data(
            "2026年8月2日", "LVC", "2直", 1,
            current_line="L-1", current_date="2026年8月2日", current_shift="2直",
        )
        self.assertIsNone(result)

    def test_edit_page_needs_no_admin(self) -> None:
        """同じ直のページを戻るのは**誰でも**。

        自分がさっき打った紙を自分で直しているだけで、誰の記録かは
        変わりません。12行を使い切って2ページ目を出したあと1ページ目の
        打ち間違いに気づくのは普通に起きるので、そのたびに人を
        呼ばせると現場が止まります。
        """
        self.is_admin = False
        result = self.service.edit_page(1, current_date="2026年8月2日",
                                        current_shift="2直", current_line="L-1")
        self.assertIsNotNone(result)
        self.assertTrue(self.service.recall.active)
        # **開いたのはいまの直のページ。** 日付も直もラインも動いていない
        self.assertEqual(self.service.recall.report_date, "2026年8月2日")
        self.assertEqual(self.service.recall.shift, "2直")
        self.assertEqual(self.service.recall.line, "L-1")

    def test_edit_page_cannot_reach_another_shift(self) -> None:
        """**動くのはページだけ。** 他の直へはこの道では入れない。"""
        self.is_admin = False
        # 渡した「いま」に無いページは開けない(他の直のページ1があっても)
        result = self.service.edit_page(9, current_date="2026年8月2日",
                                        current_shift="2直", current_line="L-1")
        self.assertIsNone(result)
        self.assertFalse(self.service.recall.active)

    def test_back_to_current_clears_recall_and_reloads_latest(self) -> None:
        header2 = HeaderRecord(report_date="2026年8月3日", line="L-1", shift="1直", page=1, worker="current")
        self.repo.save(header2, [
            DetailRecord(report_date=header2.report_date, line="L-1", shift="1直", page=1, row_no=i) for i in range(1, 13)
        ])

        self.is_admin = True
        self.service.recall_data(
            "2026年8月2日", "L-1", "2直", 1,
            current_line="L-1", current_date="2026年8月3日", current_shift="1直",
        )
        self.assertTrue(self.service.recall.active)

        result = self.service.back_to_current("2026年8月3日", "1直", "L-1")
        self.assertFalse(self.service.recall.active)
        self.assertIsNotNone(result)
        self.assertEqual(result[0].worker, "current")

    def test_back_to_current_with_no_saved_data_returns_none(self) -> None:
        result = self.service.back_to_current("2099年1月1日", "1直", "NOPE")
        self.assertIsNone(result)

    def test_autosave_throttle(self) -> None:
        from datetime import datetime, timedelta

        now = datetime(2026, 8, 3, 10, 0, 0)
        self.assertTrue(self.service.should_autosave(now, interval_sec=60))
        self.service.mark_autosaved(now)
        self.assertFalse(self.service.should_autosave(now + timedelta(seconds=30), interval_sec=60))
        self.assertTrue(self.service.should_autosave(now + timedelta(seconds=61), interval_sec=60))


if __name__ == "__main__":
    unittest.main()
