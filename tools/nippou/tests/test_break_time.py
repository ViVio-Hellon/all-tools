"""休憩の時刻(v4.37.0)── 作業停止に休憩(記号 0)を入れた行は、何時から何時までかも聞く。

現場から: 作業標準時間が溜まってくると、標準を大きく超過した作業者を見つけられるようになる。
そのとき休憩時間を正確にとるため、停止項目で休憩を入れた場合に、その他の理由のように
休憩の時刻(xx:xx〜zz:zz)を入力させ、停止 CSV にも残してほしい。
"""
from __future__ import annotations

import csv
import os
import sys
import tempfile
import unittest
from pathlib import Path

from nippou.db.connection import connect
from nippou.db.models import DetailRecord, HeaderRecord
from nippou.db.repository import NippouRepository
from nippou.logic import break_time
from nippou.presenters import entry as presenter
from nippou.reporting import csv_export


class ParseTests(unittest.TestCase):
    def test_いろいろな書き方をそろえる(self) -> None:
        for text in ("11:30〜12:30", "11:30~12:30", "11:30-12:30", "1130-1230",
                     "１１：３０－１２：３０", "11時30分〜12時30分", " 11:30 〜 12:30 "):
            self.assertEqual(break_time.normalize(text), "11:30〜12:30", text)
            self.assertEqual(break_time.parse(text).minutes, 60, text)

    def test_日をまたぐ休憩(self) -> None:
        self.assertEqual(break_time.parse("23:30〜0:30").minutes, 60)

    def test_読めないものは打ったまま残す(self) -> None:
        for text in ("昼", "11:30", "25:00〜26:00", "11:30〜11:30"):
            self.assertIsNone(break_time.parse(text), text)
            self.assertEqual(break_time.normalize(text), text)


class ProblemTests(unittest.TestCase):
    ROW = {"S": "0", "TH": "60", "KZ": "11", "KH": "00", "SZ": "13", "SH": "00"}

    def check(self, text: str, **extra) -> list[str]:
        return break_time.problems({**self.ROW, **extra, "break_range": text})

    def test_合っていれば何も言わない(self) -> None:
        self.assertEqual(self.check("11:30〜12:30"), [])

    def test_分と時刻の長さが違う(self) -> None:
        self.assertEqual(self.check("11:30〜12:00"), ["時刻は 30 分、作業停止の休憩は 60 分です"])

    def test_休憩を2つの欄に分けて打っても合計で見る(self) -> None:
        self.assertEqual(self.check("11:30〜12:30", TH="30", SS="0", THS="30"), [])

    def test_行の作業時間の外(self) -> None:
        self.assertIn("外にはみ出しています", self.check("12:30〜13:30")[0])

    def test_日をまたぐ行(self) -> None:
        row = {"KZ": "23", "KH": "00", "SZ": "1", "SH": "00"}
        self.assertEqual(self.check("23:30〜0:30", **row), [])
        self.assertEqual(self.check("0:00〜1:00", **row), [])

    def test_読めない時刻(self) -> None:
        self.assertIn("時刻として読めません", self.check("昼休み")[0])


class EntryTests(unittest.TestCase):
    def state(self, **rows) -> presenter.GridState:
        st = presenter.empty_state()
        for row, values in rows.items():
            for family, value in values.items():
                st.set(int(row.lstrip("r")), family, value)
        return st

    def test_休憩を入れた行だけ聞く(self) -> None:
        st = self.state(r1={"S": "0", "TH": "60"}, r2={"S": "1", "TH": "20"},
                        r3={"SS": "0", "THS": "45", "break_range": "15:00〜15:45"})
        entries = presenter.break_entries(st)
        self.assertEqual([(e["row"], e["empty"], e["minutes"]) for e in entries],
                         [(1, True, "60"), (3, False, "45")])

    def test_休憩を消しても書いた時刻は消さない(self) -> None:
        st = self.state(r1={"break_range": "11:30〜12:30"})
        self.assertEqual([(e["row"], e["needed"]) for e in presenter.break_entries(st)], [(1, False)])

    def test_画面へ渡す(self) -> None:
        view = presenter.view_model(self.state(r1={"S": "0", "TH": "60"}), report_date="2026年10月17日",
                                    line="L-1", shift="1直", page=1, page_count=1, recall=False,
                                    package_calc=False)
        self.assertEqual([e["row"] for e in view["break_entries"]], [1])

    def test_保存するときはそろえた書き方(self) -> None:
        st = self.state(r1={"S": "0", "TH": "60", "break_range": "1130-1230"})
        _header, details = presenter.to_records(st, "2026年10月17日", "L-1", "1直", 1)
        self.assertEqual(details[0].break_range, "11:30〜12:30")


class StoreAndCsvTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = NippouRepository(connect(Path(self._tmp.name) / "t.sqlite3"))
        self.addCleanup(self.repo.conn.close)
        before = os.environ.get("NIPPOU_GW_REF_DIR")
        os.environ["NIPPOU_GW_REF_DIR"] = str(Path(self._tmp.name) / "ref")
        self.addCleanup(lambda: os.environ.pop("NIPPOU_GW_REF_DIR", None) if before is None
                        else os.environ.__setitem__("NIPPOU_GW_REF_DIR", before))

    def save(self) -> HeaderRecord:
        header = HeaderRecord(report_date="2026年8月3日", line="L-1", shift="1直", page=1, worker="山田")
        self.repo.save(header, [
            DetailRecord(report_date=header.report_date, line="L-1", shift="1直", page=1, row_no=1,
                         lot="H5422S0", kz="11", kh="00", sz="13", sh="00", con="10", wei="1000",
                         tim="60", s="0", th="60", ss="1", ths="0", break_range="11:30〜12:30"),
        ])
        return header

    def test_保存して読み直しても残る(self) -> None:
        header = self.save()
        loaded = self.repo.load(header.report_date, header.line, header.shift, header.page)
        details = loaded[1] if isinstance(loaded, tuple) else loaded.details
        self.assertEqual(details[0].break_range, "11:30〜12:30")

    def test_停止CSVの休憩の1件ずつの行に出る(self) -> None:
        self.save()
        rows = csv_export.build_stop_rows(self.repo, "2026年8月3日", "L-1")
        self.assertIn("休憩の時刻", csv_export.STOP_FIELDNAMES)
        ones = [r for r in rows if r["区分"] == csv_export.STOP_KIND_ONE]
        by_code = {r["記号"]: r.get("休憩の時刻", "") for r in ones}
        self.assertEqual(by_code["0"], "11:30〜12:30")
        self.assertEqual(by_code["1"], "", "休憩でない停止には付けない")
        totals = [r for r in rows if r["区分"] == csv_export.STOP_KIND_TOTAL]
        self.assertTrue(all(not r.get("休憩の時刻") for r in totals), "まとめの行には付けない")

    def test_書き出したファイルにも列がある(self) -> None:
        self.save()
        out = Path(self._tmp.name) / "停止.csv"
        csv_export.write_stop_rows(csv_export.build_stop_rows(self.repo, "2026年8月3日", "L-1"), out)
        with open(out, encoding="utf-8-sig", newline="") as f:
            got = list(csv.DictReader(f))
        self.assertIn("11:30〜12:30", [r["休憩の時刻"] for r in got])

    def test_時刻を書いても共有へ出る中身の指紋は変わらない(self) -> None:
        from nippou.logic.fingerprint import page_fingerprint

        header = HeaderRecord(report_date="2026年8月3日", line="L-1", shift="1直", page=1)
        base = dict(report_date=header.report_date, line="L-1", shift="1直", page=1, row_no=1,
                    lot="H1", s="0", th="60")
        self.assertEqual(page_fingerprint(header, [DetailRecord(**base)]),
                         page_fingerprint(header, [DetailRecord(**base, break_range="11:30〜12:30")]))


# ======================================================================
# v4.37.1 確かめ ── 休憩とほかの停止の重なり・ありえない時刻(大きく超過の土台)
# ======================================================================
class MultiRangeTests(unittest.TestCase):
    def test_2つの休憩を並べて書ける(self) -> None:
        for text in ("10:00〜10:10、12:00〜12:50", "1000-1010,1200-1250", "10:00~10:10 / 12:00~12:50"):
            self.assertEqual(break_time.normalize(text), "10:00〜10:10、12:00〜12:50", text)
            self.assertEqual(break_time.total_minutes(break_time.parse_all(text)), 60, text)

    def test_1つでも読めなければ読めない(self) -> None:
        self.assertIsNone(break_time.parse_all("10:00〜10:10、昼"))
        self.assertEqual(break_time.normalize("10:00〜10:10、昼"), "10:00〜10:10、昼")


class CheckTests(unittest.TestCase):
    """`issues` の1つずつ。壊したところだけが出て、ほかは出ないこと。"""

    ROW = {"S": "0", "TH": "60", "KZ": "11", "KH": "00", "SZ": "13", "SH": "00"}
    DAY = break_time.shift_window("07:00", "15:00")

    def codes(self, text: str, *, window=DAY, others=(), **extra) -> list[str]:
        values = {**self.ROW, **extra, "break_range": text}
        return [i.code for i in break_time.issues(values, window=window, shift="1直", others=others)]

    def test_合っていれば何も出ない(self) -> None:
        self.assertEqual(self.codes("11:30〜12:30"), [])
        self.assertEqual(self.codes("11:00〜11:10、12:00〜12:50"), [])

    def test_空は止めない(self) -> None:
        self.assertEqual(self.codes(""), [])

    def test_ありえない時刻(self) -> None:
        for text in ("25:00〜26:00", "11:70〜12:00", "昼休み", "11:30", "11:30〜11:30"):
            self.assertEqual(self.codes(text), [break_time.UNREADABLE], text)

    def test_直の外(self) -> None:
        got = break_time.issues({**self.ROW, "break_range": "3:00〜4:00"}, window=self.DAY, shift="1直")
        self.assertEqual([i.code for i in got], [break_time.OUTSIDE_SHIFT])
        self.assertIn("1直(7:00〜15:00)の時間ではありません", got[0].message)

    def test_直を越える残業の行の中なら直の外とは言わない(self) -> None:
        row = {"KZ": "14", "KH": "00", "SZ": "17", "SH": "00", "TH": "30"}
        self.assertEqual(self.codes("15:30〜16:00", **row), [])

    def test_3直は日をまたいで見る(self) -> None:
        night = break_time.shift_window("22:50", "07:00")
        row = {"KZ": "23", "KH": "00", "SZ": "3", "SH": "00"}
        self.assertEqual(self.codes("0:30〜1:30", window=night, **row), [])
        self.assertEqual(self.codes("12:00〜13:00", window=night, **row), [break_time.OUTSIDE_SHIFT])

    def test_行の外(self) -> None:
        self.assertEqual(self.codes("12:30〜13:30"), [break_time.OUTSIDE_ROW])

    def test_分が合わない(self) -> None:
        self.assertEqual(self.codes("11:30〜12:00"), [break_time.LENGTH])

    def test_休憩の記号が無いのに時刻だけ(self) -> None:
        self.assertEqual(self.codes("11:30〜12:30", S="1"), [break_time.NO_BREAK_CODE])

    def test_同じ行の休憩どうしが重なる(self) -> None:
        self.assertEqual(self.codes("11:30〜12:00、11:50〜12:20"), [break_time.OVERLAP_SELF])

    def test_ほかの行の休憩と重なる(self) -> None:
        other = [("2ページ 3行目", break_time.parse_all("12:00〜12:30"))]
        got = break_time.issues({**self.ROW, "break_range": "11:30〜12:30"}, window=self.DAY, others=other)
        self.assertEqual([i.code for i in got], [break_time.OVERLAP_ROW])
        self.assertIn("2ページ 3行目の休憩(12:00〜12:30)と重なっています", got[0].message)
        # 端が接するだけは重ならない
        touching = [("3行目", break_time.parse_all("12:30〜12:40"))]
        self.assertEqual(self.codes("11:30〜12:30", others=touching), [])

    def test_ほかの停止と足すと行に入りきらない(self) -> None:
        # 11:00〜13:00 の 120 分に、休憩 60 分 + 段取り 70 分
        got = break_time.issues({**self.ROW, "SS": "1", "THS": "70", "break_range": "11:30〜12:30"})
        self.assertEqual([i.code for i in got], [break_time.OTHER_STOPS])
        self.assertIn("休憩 60 分とほかの停止 70 分を足すと 130 分", got[0].message)
        self.assertEqual(self.codes("11:30〜12:30", SS="1", THS="60"), [])


class SaveCheckTests(unittest.TestCase):
    """保存の確かめ(7項目と一緒に走る)。直ぜんぶ・ページをまたいで見る。"""

    def run_all(self, rows):
        from nippou.logic import save_checks
        return save_checks.run_all(rows, shift="1直", shift_start="07:00", shift_end="15:00",
                                   limit_minutes=480, known_stop_codes={"0", "1"})

    def row(self, page, n, kz, sz, br="", **kw):
        return DetailRecord(report_date="2026年8月3日", line="L-1", shift="1直", page=page,
                            row_no=n, lot=f"L{page}{n}", kz=kz[:2], kh=kz[2:], sz=sz[:2],
                            sh=sz[2:], break_range=br, **kw)

    def test_ほかのページの休憩と重なると止める(self) -> None:
        from nippou.logic import save_checks
        rows = [self.row(1, 1, "1100", "1300", "11:30〜12:30", s="0", th="60"),
                self.row(2, 1, "1100", "1300", "12:00〜13:00", s="0", th="60")]
        found = [f for f in self.run_all(rows) if f.code == save_checks.BREAK_TIME]
        self.assertEqual([(f.page, f.row) for f in found], [(1, 1), (2, 1)])
        self.assertIn("2ページ 1行目の休憩", found[0].message)
        self.assertFalse(any(f.skippable for f in found), "設備移動のため保存でも通さない")
        self.assertEqual(save_checks.during_input(found), found, "保存(確定)で止める")

    def test_空なら何も言わない(self) -> None:
        from nippou.logic import save_checks
        rows = [self.row(1, 1, "1100", "1300", s="0", th="60")]
        self.assertFalse([f for f in self.run_all(rows) if f.code == save_checks.BREAK_TIME])

    def test_作業時間がマイナスの行は重ねて言わない(self) -> None:
        from nippou.logic import save_checks
        rows = [self.row(1, 1, "1100", "1300", "11:30〜12:30", s="0", th="60", ss="1", ths="70")]
        found = self.run_all(rows)
        self.assertEqual([f.code for f in found if f.code in (save_checks.BREAK_TIME,
                                                                save_checks.NEGATIVE_TIME)],
                         [save_checks.NEGATIVE_TIME])
        self.assertIn("休憩とほかの停止が重なっていないかも",
                      next(f for f in found if f.code == save_checks.NEGATIVE_TIME).how)

    def test_直の外は止める(self) -> None:
        from nippou.logic import save_checks
        rows = [self.row(1, 1, "1100", "1300", "3:00〜4:00", s="0", th="60")]
        found = [f for f in self.run_all(rows) if f.code == save_checks.BREAK_TIME]
        self.assertEqual(len(found), 1)
        self.assertIn("1直(7:00〜15:00)の時間ではありません", found[0].message)


class EntryCheckTests(unittest.TestCase):
    def test_画面の一覧にも同じ確かめが出る(self) -> None:
        st = presenter.empty_state()
        for row, values in {1: {"S": "0", "TH": "60", "KZ": "11", "KH": "00", "SZ": "13", "SH": "00",
                                "break_range": "11:30〜12:30"},
                            2: {"S": "0", "TH": "30", "KZ": "13", "KH": "00", "SZ": "15", "SH": "00",
                                "break_range": "3:00〜3:30"}}.items():
            for family, value in values.items():
                st.set(row, family, value)
        got = presenter.break_entries(
            st, window=break_time.shift_window("07:00", "15:00"), shift="1直",
            other_pages=[("2ページ 4行目", break_time.parse_all("12:00〜12:10"))])
        by_row = {e["row"]: e["problems"] for e in got}
        self.assertEqual(len(by_row[1]), 1)
        self.assertIn("2ページ 4行目の休憩", by_row[1][0])
        self.assertIn("1直(7:00〜15:00)の時間ではありません", by_row[2][0])


if __name__ == "__main__":
    unittest.main()
