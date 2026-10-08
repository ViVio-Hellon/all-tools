"""作業停止を5つまで(v4.24.0)

    日報を入力: 作業停止を5つまで入力できるようにしたい
    CSVの出し方にも影響がある？そこも含めて修正願う
    入力欄を広げすぎないようにしてください

紙(梱包実績日報表)と VBA は作業停止①②③まで。④⑤は画面・手元と共有の
日報・集計・CSV に入り、紙には使っているページだけ③の右に足す。
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou import constants, layout
from nippou.access_bridge import pusher, sqlite_backend
from nippou.db.connection import connect
from nippou.db.models import DetailRecord, HeaderRecord
from nippou.db.repository import NippouRepository
from nippou.logic import fingerprint, packing_agg, work_time

KEY = dict(report_date="2026年10月7日", line="L-1", shift="1直", page=1)


def detail(**values) -> DetailRecord:
    return DetailRecord(**KEY, row_no=values.pop("row_no", 1), **values)


class TmpCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class LocalDbTests(TmpCase):
    def test_手元に保存して読み直せる(self) -> None:
        conn = connect(self.dir / "local.sqlite3")
        repo = NippouRepository(conn)
        repo.save(HeaderRecord(**KEY), [detail(lot="1234567", s="0", th="10",
                                              s4="2", th4="7", s5="3", th5="4")])
        _, rows = repo.load(*KEY.values())
        self.assertEqual((rows[0].s4, rows[0].th4, rows[0].s5, rows[0].th5), ("2", "7", "3", "4"))
        conn.close()

    def test_前からある手元のDBに列を足す(self) -> None:
        path = self.dir / "old.sqlite3"
        old = sqlite3.connect(path)
        old.execute("CREATE TABLE daily_detail (report_date TEXT, line TEXT, shift TEXT,"
                    " page INTEGER, row_no INTEGER, lot TEXT, s TEXT, th TEXT)")
        old.commit()
        old.close()
        conn = connect(path)
        names = {r[1] for r in conn.execute("PRAGMA table_info(daily_detail)")}
        self.assertTrue({"s4", "th4", "s5", "th5"} <= names)
        conn.close()


class WorkTimeTests(unittest.TestCase):
    def test_作業時間から45も引く(self) -> None:
        rows = {1: {"KZ": "08", "KH": "00", "SZ": "09", "SH": "00",
                    "TH": "10", "THS": "5", "THT": "", "TH4": "7", "TH5": "3"}}
        result = work_time.compute(rows)
        self.assertIsNone(result.problem)
        self.assertEqual(str(result.times[1]), "35")

    def test_集計の停止は5組まで縦にする(self) -> None:
        stops = packing_agg.build_stops(detail(s="0", th="10", s5="A1", th5="4"))
        self.assertEqual([(s.stop_no, s.stop_code) for s in stops], [(1, "0"), (5, "A1")])


class FingerprintTests(unittest.TestCase):
    def test_45が空なら前と同じ指紋(self) -> None:
        """**送り済みの日報が一斉に「未送信」へ戻らない。**"""
        header = HeaderRecord(**KEY)
        row = detail(lot="1234567", s="0", th="10")
        legacy = fingerprint.digest(
            [[fingerprint.text(getattr(header, f, "")) for f in fingerprint.HEADER_FIELDS],
             ["1"] + [fingerprint.text(getattr(row, f, "")) for f in fingerprint.DETAIL_FIELDS]])
        self.assertEqual(fingerprint.page_fingerprint(header, [row]), legacy)

    def test_45を打てば指紋が変わる(self) -> None:
        header = HeaderRecord(**KEY)
        plain = fingerprint.page_fingerprint(header, [detail(lot="1234567")])
        self.assertNotEqual(plain, fingerprint.page_fingerprint(
            header, [detail(lot="1234567", s4="2", th4="7")]))


class SharedDbTests(TmpCase):
    def test_前からある共有の表に列を足して送れる(self) -> None:
        path = self.dir / "日報データ.sqlite3"
        conn = sqlite3.connect(path)
        old = [c for c in sqlite_backend.DETAIL_COLUMNS if c not in ("S4", "TH4", "S5", "TH5")]
        conn.execute(sqlite_backend._create_sql("日報明細_L-1", tuple(old), sqlite_backend.DETAIL_KEY))
        conn.commit()
        conn.close()
        result = sqlite_backend.push_records(
            path, HeaderRecord(**KEY), [detail(lot="1234567", s4="2", th4="7")],
            "日報ヘッダー_L-1", "日報明細_L-1")
        self.assertTrue(result.success, result.error)
        conn = sqlite3.connect(path)
        got = conn.execute('SELECT "S4", "TH4" FROM "日報明細_L-1"').fetchone()
        conn.close()
        self.assertEqual(got, ("2", "7"))

    def test_Accessへも45を書き_列を足す文を出す(self) -> None:
        statements = pusher.build_detail_statements(HeaderRecord(**KEY), [detail(s4="2", th4="7")])
        self.assertIn("[S4],[TH4],[S5],[TH5]", statements[1])
        self.assertEqual(pusher.add_columns_statements("日報明細_L-1")[0],
                         "ALTER TABLE [日報明細_L-1] ADD COLUMN [S4] TEXT(255)")


class CsvAndPaperTests(unittest.TestCase):
    def test_CSVは前からある列の位置を動かさず後ろに足す(self) -> None:
        from nippou.reporting import csv_export

        names = csv_export.DETAIL_FIELDNAMES
        self.assertEqual(names[-4:], ["作業停止④ 記号", "作業停止④ 時間(分)",
                                      "作業停止⑤ 記号", "作業停止⑤ 時間(分)"])
        self.assertEqual(names.index("作業停止③ 時間(分)") + 1,
                         names.index("実績合計 枚数"))

    def test_CSVの行に45が入る(self) -> None:
        from nippou.reporting import csv_export

        row = packing_agg.build_row(HeaderRecord(**KEY), detail(lot="1234567", s4="2", th4="7"))
        out = csv_export.detail_row_dict(None, row)
        self.assertEqual((out["作業停止④ 記号"], out["作業停止④ 時間(分)"]), ("2", "7"))
        self.assertEqual(out["作業停止⑤ 記号"], "")

    def test_取り込みは見出しの名前で45を読む(self) -> None:
        from nippou.logic import csv_import

        names = csv_import._column_names()
        self.assertEqual(names["作業停止④ 記号"], "s4")
        self.assertEqual(names["作業停止⑤ 時間(分)"], "th5")

    def test_紙は使っているページだけ45を足す(self) -> None:
        from nippou.reporting.print_format import build_print_html

        plain = build_print_html(HeaderRecord(**KEY), [detail(lot="1234567", s="0", th="10")])
        self.assertNotIn("作業停止④", plain)
        used = build_print_html(HeaderRecord(**KEY), [detail(lot="1234567", s5="A1", th5="4")])
        self.assertIn("作業停止④", used)
        self.assertIn("作業停止⑤", used)


class ScreenTests(unittest.TestCase):
    def test_停止の組はひとつの決まりから(self) -> None:
        self.assertEqual(constants.STOP_SLOTS[3:], (("S4", "TH4"), ("S5", "TH5")))
        self.assertEqual(constants.FOCUS_CHAIN["THT"][0], "S4")
        self.assertIsNone(constants.FOCUS_CHAIN["TH5"][0])
        self.assertEqual([c.family for c in layout.ADDED_COLUMNS], ["S4", "TH4", "S5", "TH5"])

    def test_表は1組ずつ出して切り替える(self) -> None:
        root = Path(__file__).resolve().parent.parent
        html = (root / "app/templates/entry.html").read_text(encoding="utf-8")
        self.assertIn('data-stop-show="1"', html)
        self.assertIn('data-stop-to="{{ n }}"', html)
        self.assertIn('data-stopsum-row="{{ row }}"', html)
        css = (root / "app/static/css/components.css").read_text(encoding="utf-8")
        for n in range(1, 6):
            self.assertIn(f'table.grid[data-stop-show="{n}"] [data-stop-slot]:not([data-stop-slot="{n}"])', css)
        js = (root / "app/static/js/views/entry.js").read_text(encoding="utf-8")
        self.assertIn("function revealStop(", js)
        self.assertIn("const skipStops =", js)


if __name__ == "__main__":
    unittest.main()
