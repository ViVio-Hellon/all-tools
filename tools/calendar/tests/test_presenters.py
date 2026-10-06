"""ビューモデルの組み立てと印刷

API を経由せず、presenters を直接呼んで確かめる。
**受け口(routes)を通さずに済むものはここで見る** ── HTTP の往復を
挟まないぶん、どこが壊れているかがはっきりする。
"""

from __future__ import annotations

import datetime as _dt
import unittest

from . import _isolation

_isolation.ensure_isolated()

from calendar_app import config, db, printing, settings as user_settings  # noqa: E402
from calendar_app.presenters import calendar as presenter  # noqa: E402
from calendar_app.repository import Repository  # noqa: E402


class GridTests(unittest.TestCase):
    """月の割り出し (VBA: ShowMonth の先頭)。"""

    def test_日曜始まり(self) -> None:
        """VBA: firstDay - (Weekday(firstDay) - 1)"""
        start = presenter.grid_start(2026, 8)
        self.assertEqual(start.weekday(), 6)          # 6 = 日曜
        self.assertLessEqual(start, _dt.date(2026, 8, 1))
        self.assertGreater(start + _dt.timedelta(days=7), _dt.date(2026, 8, 1))

    def test_1日が日曜の月(self) -> None:
        """2026/11/01 は日曜。その日から始まる。"""
        self.assertEqual(presenter.grid_start(2026, 11), _dt.date(2026, 11, 1))

    def test_月をずらす(self) -> None:
        self.assertEqual(presenter.shift_month(2026, 12, 1), (2027, 1))
        self.assertEqual(presenter.shift_month(2026, 1, -1), (2025, 12))
        self.assertEqual(presenter.shift_month(2026, 8, 5), (2027, 1))

    def test_範囲外へは出ない(self) -> None:
        """行き先の無い月を組み立てない。"""
        self.assertEqual(presenter.clamp(1800, 5)[0], presenter.MIN_YEAR)
        self.assertEqual(presenter.clamp(3000, 5)[0], presenter.MAX_YEAR)

    def test_月の繰り上がり(self) -> None:
        self.assertEqual(presenter.clamp(2026, 13), (2027, 1))
        self.assertEqual(presenter.clamp(2026, 0), (2025, 12))

    def test_日付の読み取り(self) -> None:
        self.assertEqual(presenter.parse_date("2026/08/03"), _dt.date(2026, 8, 3))
        with self.assertRaises(ValueError):
            presenter.parse_date("2026-08-03")
        with self.assertRaises(ValueError):
            presenter.parse_date("2026/02/30")     # 実在しない日


class MonthViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)
        user_settings.save_my_line("L-1")

    def test_42セル(self) -> None:
        view = presenter.month_view(self.conn, 2026, 8)
        self.assertEqual(len(view.cells), presenter.CELL_COUNT)

    def test_当日は指定できる(self) -> None:
        """テストがカレンダーの実日付に左右されないように。"""
        view = presenter.month_view(self.conn, 2026, 8, today=_dt.date(2026, 8, 15))
        marked = [c for c in view.cells if c.today]
        self.assertEqual([c.date for c in marked], ["2026/08/15"])

    def test_別の月の当日には印をつけない(self) -> None:
        view = presenter.month_view(self.conn, 2026, 9, today=_dt.date(2026, 8, 15))
        self.assertEqual([c for c in view.cells if c.today], [])

    def test_見出しの色分け(self) -> None:
        view = presenter.month_view(self.conn, 2026, 8)
        self.assertEqual(view.weekdays[0]["tone"], "sun")
        self.assertEqual(view.weekdays[6]["tone"], "sat")
        self.assertEqual(view.weekdays[3]["tone"], "")

    def test_辞書にできる(self) -> None:
        payload = presenter.month_dict(presenter.month_view(self.conn, 2026, 8))
        self.assertEqual(len(payload["cells"]), 42)
        self.assertIn("can_prev", payload)


class DayViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.repo = Repository(self.conn)
        user_settings.save_my_line("L-1")

    def test_休みと連絡を見分ける(self) -> None:
        day = _dt.date(2026, 8, 3)
        self.repo.save_record(day, config.KUBUN_REST, "山田太郎", "10",
                              shift="1", group="B", line="L-1")
        self.repo.save_record(day, config.KUBUN_OTHER, "工程変更", "-",
                              group="B", line="L-1")

        view = presenter.day_view(self.conn, day)
        kinds = [item["kind"] for item in view.items]
        self.assertEqual(sorted(kinds), ["comment", "rest"])
        rest = next(i for i in view.items if i["kind"] == "rest")
        self.assertIn("[休み]", rest["caption"])
        self.assertIn("1直", rest["caption"])

    def test_登録が無い日(self) -> None:
        view = presenter.day_view(self.conn, _dt.date(2026, 8, 4))
        self.assertEqual(view.items, [])
        self.assertIn("2026年8月4日", view.title)


class PrintingTests(unittest.TestCase):
    """印刷用の1枚 (VBA: btnPrint_Click)。"""

    def setUp(self) -> None:
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.repo = Repository(self.conn)

    def render(self, year=2026, month=8, my_line="L-1"):
        start = presenter.grid_start(year, month)
        records = self.repo.get_month_records(start, start + _dt.timedelta(days=41))
        return printing.render_month_document(
            year=year, month=month, start=start, records=records, my_line=my_line)

    def test_A4横に組む(self) -> None:
        html = self.render()
        self.assertIn("size: A4 landscape", html)
        self.assertIn("2026年 8月", html)

    def test_登録内容が出る(self) -> None:
        self.repo.save_record(_dt.date(2026, 8, 3), config.KUBUN_REST,
                              "山田太郎", "10", shift="1", group="B", line="L-1")
        self.assertIn("山田太郎", self.render())

    def test_HTMLを埋め込ませない(self) -> None:
        """登録内容に HTML が入っても壊れない。

        セルに出るのは休みの**作業者名**(コメントは「コメント有」としか
        出ない ── ``build_day_text``)。名簿から来る名前とはいえ、
        ここを素通しにすると Access 側の1行で画面が壊れる。
        """
        self.repo.save_record(_dt.date(2026, 8, 3), config.KUBUN_REST,
                              "<script>alert(1)</script>", "10",
                              shift="1", group="B", line="L-1")
        html = self.render()
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_ライン設定が出る(self) -> None:
        self.assertIn("L-1", self.render(my_line="L-1"))

    def test_ファイルにも書ける(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            start = presenter.grid_start(2026, 8)
            path = printing.render_month_html(
                year=2026, month=8, start=start, records={}, my_line="",
                output_dir=Path(tmp))
            self.assertTrue(path.exists())
            self.assertIn("2026年 8月", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
