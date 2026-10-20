"""同じ決まりを1か所に ── 見つかった不具合の修正と一緒に (v4.39.2)

【利用者から】

    大きなリファクタリングはせず、見つかった不具合の修正を優先してください
    直す箇所に限って「同じ決まりを1か所にまとめる」小さな整理を一緒に行ってください
    全角の「０」を休憩として扱ってください

1. 休憩「0」かどうかは `break_time.is_rest_code` の1か所で決める。前は時間の割り振り・保存の
   確かめは全角「０」を休憩とみなし、休憩の時刻の確かめ・休憩の時刻の欄・停止内訳CSVは
   休憩でないとみなしていた(同じ行で答えが割れる)
2. 日勤の残業の終わり(18:00・19:00)は `constants.DAY_SHIFT_OVERTIME_ENDS` の1か所。前は次の行へ
   終了を写さない時刻だけが 18:00 を直に持っていた(19:00 で終わった行の終了が次の行の開始へ写る)
3. 「当直をDBから復旧」の印刷用HTMLも `printing.save_paper` を通す(前は別の書き出しで、
   「後日作成」の印が載らなかった)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou import constants  # noqa: E402
from nippou.db.models import DetailRecord, HeaderRecord  # noqa: E402
from nippou.logic import break_time, navigation, packing_agg, save_checks, time_split  # noqa: E402
from tests._web import HAS_FLASK, SKIP_REASON  # noqa: E402

REST_FORMS = ["0", "０", " 0 ", "　０　"]
NOT_REST = ["", "1", "イ", "00", "０１", "O"]


class RestCodeOneRuleTests(unittest.TestCase):
    def values(self, code: str) -> dict:
        return {"S": code, "TH": "60", "SS": "イ", "THS": "10", "break_range": "12:00〜13:00"}

    def test_休憩かどうかはどこで訊いても同じ答え(self) -> None:
        for code in REST_FORMS + NOT_REST:
            with self.subTest(code=code):
                want = code in REST_FORMS
                values = self.values(code)
                self.assertEqual(break_time.is_rest_code(code), want)
                self.assertEqual(time_split.is_rest(values, "S"), want)
                self.assertEqual(break_time.has_break(values), want)
                self.assertEqual(break_time.break_minutes(values), 60.0 if want else 0.0)
                d = DetailRecord(report_date="2026年10月5日", line="L-1", shift="1直", page=1, row_no=1,
                                 s=code, th="60", ss="イ", ths="10")
                self.assertEqual(save_checks.break_minutes([d]), 60 if want else 0)

    def test_全角の休憩でも休憩の時刻の確かめが通る(self) -> None:
        """前は「作業停止に休憩(0)が無いのに、休憩の時刻が書いてあります」と断っていた。"""
        self.assertEqual(break_time.problems(self.values("０")), [])
        self.assertEqual(break_time.other_stop_minutes(self.values("０")), 10.0)

    def test_全角の休憩も停止内訳CSVに休憩の時刻が載る(self) -> None:
        header = HeaderRecord(report_date="2026年10月5日", line="L-1", shift="1直", page=1)
        d = DetailRecord(report_date="2026年10月5日", line="L-1", shift="1直", page=1, row_no=1,
                         s="０", th="60", break_range="12:00〜13:00")
        rows = [r for r in packing_agg.build_row(header, d).stops if r.stop_minutes]
        self.assertTrue(rows, "停止の行がある")
        self.assertEqual(rows[0].break_range, "12:00〜13:00")

    def test_決まりの置き場所は1つ(self) -> None:
        self.assertIs(time_split.STOP_SLOTS, constants.STOP_SLOTS)
        self.assertEqual(time_split.REST_CODE, break_time.REST_STOP_CODE)
        self.assertEqual(save_checks.REST_STOP_CODE, break_time.REST_STOP_CODE)
        self.assertEqual(time_split.STOP_MINUTE_FAMILIES, constants.STOP_TIME_FAMILIES)


class DayOvertimeOneRuleTests(unittest.TestCase):
    def test_日勤の残業の終わりでは次の行へ写さない(self) -> None:
        for end in constants.DAY_SHIFT_OVERTIME_ENDS:
            with self.subTest(end=end):
                h, m = end.split(":")
                self.assertTrue(navigation.ends_shift(h, m, day_temp="日勤", shift_end_times={}))
        self.assertFalse(navigation.ends_shift("19", "00", day_temp="1直", shift_end_times={}))
        self.assertFalse(navigation.ends_shift("18", "30", day_temp="日勤", shift_end_times={}))

    def test_保存の確かめと同じ時刻を見る(self) -> None:
        self.assertEqual(save_checks.DAY_SHIFT_OVERTIME_ENDS, constants.DAY_SHIFT_OVERTIME_ENDS)


if HAS_FLASK:
    from tests import test_web_admin_db as admin_db

    class RestorePaperStampTests(admin_db.RestoreShiftTests):
        """「当直をDBから復旧」の紙にも「後日作成」の印が載る。"""

        def test_復旧した紙にも後日作成の印(self) -> None:
            day, line, shift = self._save_shift([1, 2])
            self.repo().record_backfill(day, line, shift, 2, terminal="PC-01", note="打ち忘れ")
            self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
            body = self.post("/api/settings/restore-shift", {}).get_json()
            self.assertEqual(len(body["files"]), 2, body)
            texts = {Path(p).name: Path(p).read_text(encoding="utf-8") for p in body["files"]}
            page2 = next(t for n, t in texts.items() if n.endswith("_2.html"))
            page1 = next(t for n, t in texts.items() if n.endswith("_1.html"))
            self.assertIn("後日作成", page2)
            self.assertNotIn("後日作成(", page1)

    for _name in [n for n in dir(admin_db.RestoreShiftTests) if n.startswith("test_")]:
        setattr(RestorePaperStampTests, _name, None)
else:                                                    # pragma: no cover
    RestorePaperStampTests = unittest.skip(SKIP_REASON)(unittest.TestCase)


if __name__ == "__main__":
    unittest.main()
