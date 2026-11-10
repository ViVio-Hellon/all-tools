"""4直・5直 ── 決まったラインだけの直 (v4.49.0)

【利用者から】

    日報で テーブル：時間用で1，2，3直の時間の管理を行っているが
    直の概念が増え4直、5直が増える
    ただ4直、5直で見る必要があるのは決まったラインだけである
    テーブル：時間用に対象ラインの列を追加して管理できるようにしたい
    ただし1，2，3直の概念はなくならないので設定がなければ1，2，3直で見るままでよい

決めたこと:

    4直 11:00〜19:25 / 5直 21:20〜05:45(特殊勤務。NS1 だけ。日で切り替え)
    ・重なる時間に作業者を選ぶときは「1直 / 4直」を先に選ぶ
    ・早出・残業の幅(時間用の「早出」「残業」分)。4直の残業は 5直の始まり(21:20)で切る
        「4直の残業が続いたとしても21：20までに区切って入力しなければ
          123直と同じように入力できなくすべきです」
    ・4直が開いている間は 5直を始められない(4直に入力する)
    ・最後の行の終わりは 定時〜閉じる時刻 の間なら通す
    ・対象ラインの行が無いラインは、いままでとまったく同じ
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import prev_shift, save_checks, shift_rows  # noqa: E402
from nippou.logic.shift import ShiftCalculator, times_from_master  # noqa: E402
from nippou.logic.shift_rows import ShiftRow  # noqa: E402
from tests._web import HAS_FLASK, SKIP_REASON  # noqa: E402
from tests import test_scenario_shift_flow as flow  # noqa: E402

BASE = [ShiftRow("1", "07:00", "15:00"), ShiftRow("2", "15:00", "22:50"),
        ShiftRow("3", "22:50", "07:00"), ShiftRow("昼", "08:00", "17:00")]
NS1 = [ShiftRow("4", "11:00", "19:25", ("NS1",), 120, 120),
       ShiftRow("5", "21:20", "05:45", ("NS1",), 0, 120)]


def at(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M")


def labels(calc: ShiftCalculator, when: str) -> list[tuple[str, str]]:
    return [(c.business_date.isoformat(), c.label) for c in calc.candidates(at(when))]


class RowsTests(unittest.TestCase):
    """時間用の行を読む ── **列が無い表でも、いままでどおり。**"""

    def test_列が無い表は全部共通(self) -> None:
        rows = shift_rows.from_table([{"直": "1", "開始": "07:00", "終了": "15:00"}])
        self.assertEqual(rows, [ShiftRow("1", "07:00", "15:00")])

    def test_対象ラインと早出残業を読む(self) -> None:
        rows = shift_rows.from_table([{"直": "4直", "開始": "11:00", "終了": "19:25",
                                       "対象ライン": "NS1、LS", "早出(分)": "１２０", "残業": "90"}])
        self.assertEqual(rows, [ShiftRow("4", "11:00", "19:25", ("NS1", "機側"), 120, 90)])

    def test_ほかのラインは4直を持たない(self) -> None:
        times, extras = shift_rows.for_line(BASE + NS1, "機側")
        self.assertEqual(extras, ())
        self.assertNotIn("4", times)
        calc = shift_rows.calculator_for_line(BASE + NS1, "機側")
        self.assertEqual(calc.times, times_from_master(shift_rows.common(BASE)))
        self.assertEqual(labels(calc, "2026-11-10 12:00"), [("2026-11-10", "1直")])

    def test_ラインの行は共通の行に勝つ(self) -> None:
        rows = BASE + [ShiftRow("2", "15:30", "22:50", ("NS1",))]
        self.assertEqual(shift_rows.for_line(rows, "NS1")[0]["2"], ("15:30", "22:50"))
        self.assertEqual(shift_rows.for_line(rows, "機側")[0]["2"], ("15:00", "22:50"))

    def test_分の読み方(self) -> None:
        self.assertEqual(shift_rows.parse_minutes(""), 0)
        self.assertEqual(shift_rows.parse_minutes("120分"), 120)
        self.assertIsNone(shift_rows.parse_minutes("二時間"))
        self.assertIsNone(shift_rows.parse_minutes("-5"))


class CandidateTests(unittest.TestCase):
    """**作業者を選ぶときに、なれる直。** 2つ以上なら先に選んでもらう。"""

    def setUp(self) -> None:
        self.calc = shift_rows.calculator_for_line(BASE + NS1, "NS1")

    def test_重ならない時間は聞かない(self) -> None:
        self.assertEqual(labels(self.calc, "2026-11-10 08:30"), [("2026-11-10", "1直")])
        self.assertEqual(labels(self.calc, "2026-11-11 07:45"), [("2026-11-11", "1直")])

    def test_早出(self) -> None:
        self.assertEqual(labels(self.calc, "2026-11-10 09:00"),
                         [("2026-11-10", "1直"), ("2026-11-10", "4直(早出)")])

    def test_4直の残業と5直の始まり(self) -> None:
        self.assertEqual(labels(self.calc, "2026-11-10 21:19"),
                         [("2026-11-10", "2直"), ("2026-11-10", "4直(残業)")])
        # 21:20 からは 4直は選べない(5直の定時の始まりで切る)
        self.assertEqual(labels(self.calc, "2026-11-10 21:20"),
                         [("2026-11-10", "2直"), ("2026-11-10", "5直")])

    def test_5直は始まった日の日報(self) -> None:
        self.assertEqual(labels(self.calc, "2026-11-11 03:00"),
                         [("2026-11-10", "3直"), ("2026-11-10", "5直")])
        self.assertEqual(labels(self.calc, "2026-11-11 07:44"),
                         [("2026-11-11", "1直"), ("2026-11-10", "5直(残業)")])

    def test_4直の残業はいくら書いても5直の始まりで閉じる(self) -> None:
        rows = BASE + [ShiftRow("4", "11:00", "19:25", ("NS1",), 0, 600), NS1[1]]
        calc = shift_rows.calculator_for_line(rows, "NS1")
        self.assertEqual(calc.input_close_at("4直", date(2026, 11, 10)), at("2026-11-10 21:20"))
        self.assertEqual(calc.input_close_at("5直", date(2026, 11, 10)), at("2026-11-11 07:45"))

    def test_固定した4直がいまの直(self) -> None:
        pinned = self.calc.pinned(date(2026, 11, 10), "4直")
        for when, shift, left in (("2026-11-10 12:00", "4直", 445),
                                  ("2026-11-10 19:20", "4直", 5),
                                  ("2026-11-10 21:19", "4直", -114),
                                  ("2026-11-10 21:20", "2直", 90)):
            with self.subTest(when=when):
                self.assertEqual(pinned.time_check(at(when)), shift)
                self.assertEqual(pinned.minutes_until_shift_end(at(when)), left)

    def test_固定した5直は翌朝も前の日(self) -> None:
        pinned = self.calc.pinned(date(2026, 11, 10), "5直")
        self.assertEqual(pinned.time_check(at("2026-11-11 07:00")), "5直")
        self.assertEqual(pinned.today_check(at("2026-11-11 07:00")), date(2026, 11, 10))
        self.assertEqual(pinned.time_check(at("2026-11-11 07:45")), "1直")

    def test_1直から3直は固定しない(self) -> None:
        self.assertIsNone(self.calc.pinned(date(2026, 11, 10), "1直").pin)


class ShiftEndTests(unittest.TestCase):
    """**最後の行の終わりは、定時〜閉じる時刻の間なら通す。**"""

    def finding(self, end: str, shift: str = "4直", until: str = "21:20") -> list:
        from nippou.db.models import DetailRecord

        d = DetailRecord(report_date="x", line="NS1", shift=shift, page=1, row_no=1,
                         lot="A", kz="18", kh="00", sz=end[:2], sh=end[3:])
        return save_checks.check_shift_end([d], shift, {"4直": "19:25", "5直": "05:45"}[shift], until)

    def test_残業の中なら通す(self) -> None:
        self.assertEqual(self.finding("19:25"), [])
        self.assertEqual(self.finding("20:30"), [])
        self.assertEqual(self.finding("21:20"), [])
        self.assertEqual(self.finding("07:00", "5直", "07:45"), [])

    def test_定時より前と閉じたあとは止める(self) -> None:
        self.assertTrue(self.finding("19:00"))
        found = self.finding("21:30")
        self.assertTrue(found)
        self.assertIn("19:25〜21:20", found[0].message)


class PrevShiftTests(unittest.TestCase):
    """**特殊勤務の翌朝に「昨日の3直が空」と言わない**(逆も)。"""

    def test_もう一方の勤務が回っていれば黙る(self) -> None:
        self.assertTrue(prev_shift.other_pattern_ran("3直", {"4直", "5直"}))
        self.assertTrue(prev_shift.other_pattern_ran("5直", {"1直", "2直"}))
        self.assertFalse(prev_shift.other_pattern_ran("3直", {"1直", "2直"}))
        self.assertEqual(prev_shift.previous_of("4直", date(2026, 11, 11)),
                         ("5直", date(2026, 11, 10)))


ROWS_4 = flow.rows_of(flow.row("A01", "11:00", "15:00"), flow.rest("15:00", "16:00"),
                      flow.row("A02", "16:00", "19:25"))
ROWS_4_OVER = flow.rows_of(flow.row("A01", "11:00", "15:00"), flow.rest("15:00", "16:00"),
                           flow.row("A02", "16:00", "20:30"))


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class ExtraShiftScreenTests(flow.ScenarioCase):
    """日報入力の画面で 4直・5直を通す。**行のあるラインだけ**(ここでは L-1)。"""

    def setUp(self) -> None:
        super().setUp()
        self.repo().replace_shift_rows(
            [ShiftRow(k, s, e) for k, (s, e) in flow.SHIFT_TIMES.items()]
            + [ShiftRow("4", "11:00", "19:25", ("L-1",), 120, 120),
               ShiftRow("5", "21:20", "05:45", ("L-1",), 0, 120)])

    def pick(self, report_date: str, shift: str):
        return self.post("/api/entry/pick-shift", {"report_date": report_date, "shift": shift})

    def start_4(self) -> dict:
        self.at("2026-10-05 12:00")
        body = self.open_screen()
        self.assertEqual([c["label"] for c in body["shift_pick"]["choices"]], ["1直", "4直"])
        self.assertTrue(body["needs_worker"])
        res = self.pick("2026年10月5日", "4直")
        self.assertEqual(res.status_code, 200, res.get_json())
        body = self.open_screen()
        self.assertEqual(body["shift"], "4直")
        self.assertEqual(body["shift_pick"], {})
        self.choose_worker("山田")
        return body

    def test_直を選ぶまで書かない(self) -> None:
        self.at("2026-10-05 12:00")
        self.open_screen()
        res = self.save(ROWS_4)
        self.assertEqual(res.status_code, 409)
        self.assertIn("どちらの直ですか", res.get_json()["message"])
        self.assertEqual(self.new_page(flow.full_page_rows("P", "11:00")).status_code, 409)
        self.assertEqual(self.repo().saved_pages("2026年10月5日", "L-1", "1直"), [])

    def test_作業者を先に打っても時計の直へ固定しない(self) -> None:
        self.at("2026-10-05 12:00")
        self.open_screen()
        body = self.choose_worker("山田")
        self.assertTrue(body["shift_pick"])
        self.assertTrue(body["needs_worker"])

    def test_4直を選んで保存し残業も打てる(self) -> None:
        self.start_4()
        self.at("2026-10-05 13:00")
        self.assertEqual(self.save(ROWS_4).status_code, 200)
        self.assertEqual(self.repo().saved_pages("2026年10月5日", "L-1", "4直"), [1])
        # 定時5分前の自動確定は 4直
        done = self.close_tick("2026-10-05 19:20")
        self.assertTrue(done["ran"], done)
        self.assertEqual(done["shift"], "4直")
        self.assertEqual(done["findings"], [])
        # 残業の時間はまだ打てる
        self.at("2026-10-05 20:40")
        state = self.post("/api/entry/state", self.sheet(ROWS_4_OVER)).get_json()
        self.assertFalse(state["read_only"], state.get("read_only_reason"))
        self.assertEqual(state["shift"], "4直")
        self.assertEqual(self.save(ROWS_4_OVER).status_code, 200)
        res = self.push_now()
        self.assertEqual(res.status_code, 200, res.get_json())
        # 5直の定時の始まり(21:20)で見るだけ
        self.at("2026-10-05 21:20")
        state = self.post("/api/entry/state", self.sheet(ROWS_4_OVER)).get_json()
        self.assertTrue(state["read_only"])
        self.assertEqual(state["shift"], "4直")

    def test_4直が開いている間は5直を始められない(self) -> None:
        self.start_4()
        self.at("2026-10-05 21:10")
        body = self.open_screen()
        self.assertEqual(body["shift"], "4直")
        self.assertEqual(body["shift_pick"], {})
        self.assertEqual(self.pick("2026年10月5日", "5直").status_code, 409)

    def test_4直のあと5直を始める(self) -> None:
        self.start_4()
        self.assertEqual(self.save(ROWS_4).status_code, 200)
        self.at("2026-10-05 21:25")
        self.assertEqual(self.post("/api/entry/start-next", {}).status_code, 200)
        body = self.open_screen()
        self.assertEqual([c["label"] for c in body["shift_pick"]["choices"]], ["2直", "5直"])
        self.assertEqual(self.pick("2026年10月5日", "5直").status_code, 200)
        self.at("2026-10-06 03:00")
        body = self.open_screen()
        self.assertEqual((body["report_date"], body["shift"]), ("2026年10月5日", "5直"))

    def test_もう選べない直は断る(self) -> None:
        self.at("2026-10-05 12:00")
        self.open_screen()
        res = self.pick("2026年10月5日", "5直")
        self.assertEqual(res.status_code, 409)
        self.assertEqual([c["label"] for c in res.get_json()["choices"]], ["1直", "4直"])


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class OtherLineTests(flow.ScenarioCase):
    """**4直・5直の行が無いラインは、いままでとまったく同じ。**"""

    def setUp(self) -> None:
        super().setUp()
        self.repo().replace_shift_rows(
            [ShiftRow(k, s, e) for k, (s, e) in flow.SHIFT_TIMES.items()]
            + [ShiftRow("4", "11:00", "19:25", ("NS1",), 120, 120)])

    def test_聞かずに時計の直(self) -> None:
        self.at("2026-10-05 12:00")
        body = self.open_screen()
        self.assertEqual(body["shift_pick"], {})
        self.assertEqual(body["shift"], "1直")
        self.choose_worker("山田")
        self.assertEqual(self.save(flow.rows_of(flow.row("A01", "07:00", "10:00"))).status_code, 200)


if __name__ == "__main__":
    unittest.main()
