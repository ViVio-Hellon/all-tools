"""保存前チェック5本 (`logic/save_checks.py`)

VBA `ExecutePrintProcess` が印刷保存のところで通していたもの:
Number_Count / 内訳チェックRun / 時間計算_シート版 / Last_Confi /
Opetime_Calcul。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.models import DetailRecord
from nippou.logic import save_checks


def row(row_no: int, page: int = 1, **values) -> DetailRecord:
    base = dict(report_date="2026年8月31日", line="HVC", shift="3直",
                page=page, row_no=row_no)
    base.update(values)
    return DetailRecord(**base)


class PackCountTests(unittest.TestCase):
    """Number_Count: 梱包数(枚数×包数) が検入枚数を超えていないか。"""

    def test_超えたら挙げる(self) -> None:
        found = save_checks.check_pack_count([
            row(1, lot="H5422S0", ken="9", mai="3", tut="5"),
        ])
        self.assertEqual([f.code for f in found], [save_checks.PACK_OVER])
        self.assertIn("15", found[0].message)
        self.assertIn("9", found[0].message)

    def test_ちょうどは通す(self) -> None:
        self.assertEqual(save_checks.check_pack_count([
            row(1, lot="A", ken="9", mai="3", tut="3")]), [])

    def test_ロット番号の空欄は上の行を引き継ぐ(self) -> None:
        """続きの行にはロット番号も検入枚数も入っていない(VBA `Kara`)。"""
        found = save_checks.check_pack_count([
            row(1, lot="A", ken="10", mai="3", tut="2"),   # 6
            row(2, lot="", mai="3", tut="2"),               # +6 = 12 > 10
        ])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].row, 1)             # 色を付けるのは検入の行

    def test_ページをまたいで足す(self) -> None:
        found = save_checks.check_pack_count([
            row(12, page=1, lot="A", ken="10", mai="5", tut="1"),
            row(1, page=2, lot="", mai="6", tut="1"),
        ])
        self.assertEqual([f.code for f in found], [save_checks.PACK_OVER])

    def test_検入枚数が無いロットは見送る(self) -> None:
        self.assertEqual(save_checks.check_pack_count([
            row(1, lot="A", mai="9", tut="9")]), [])

    def test_音を持つ(self) -> None:
        found = save_checks.check_pack_count([
            row(1, lot="A", ken="1", mai="2", tut="2")])
        self.assertEqual(found[0].sound, "pack_over")


class StopCodeTests(unittest.TestCase):
    """内訳チェックRun: 停止の記号が内訳マスタにあるか。"""

    def test_無い記号を挙げる(self) -> None:
        found = save_checks.check_stop_codes(
            [row(1, s="0", ss="ZZ")], known_codes=["0", "イ", "A"])
        self.assertEqual([f.code for f in found], [save_checks.UNKNOWN_STOP])
        self.assertEqual(found[0].field, "SS")

    def test_3つの欄すべてを見る(self) -> None:
        found = save_checks.check_stop_codes(
            [row(1, s="X", ss="Y", sth="Z")], known_codes=["0"])
        self.assertEqual([f.field for f in found], ["S", "SS", "STH"])

    def test_マスタが読めないときは何も言わない(self) -> None:
        """一覧が空のまま全部を「無い記号」と言うと、保存が丸ごと止まる。"""
        self.assertEqual(
            save_checks.check_stop_codes([row(1, s="X")], known_codes=[]), [])

    def test_全角半角は同じものとして扱う(self) -> None:
        self.assertEqual(save_checks.check_stop_codes(
            [row(1, s="１")], known_codes=["1"]), [])
        self.assertEqual(save_checks.check_stop_codes(
            [row(1, s="ｲ")], known_codes=["イ"]), [])

    def test_空欄は見ない(self) -> None:
        self.assertEqual(save_checks.check_stop_codes(
            [row(1, s="", ss="  ")], known_codes=["0"]), [])


class RowTimeTests(unittest.TestCase):
    """時間計算_シート版: 1行の長さと、同時刻。"""

    def test_直より長い行を挙げる(self) -> None:
        found = save_checks.check_row_times(
            [row(1, kz="8", kh="0", sz="20", sh="0")], limit_minutes=480)
        self.assertEqual([f.code for f in found], [save_checks.OVER_SHIFT_ROW])

    def test_同時刻を挙げる(self) -> None:
        found = save_checks.check_row_times(
            [row(1, kz="8", kh="0", sz="8", sh="0")], limit_minutes=480)
        self.assertEqual([f.code for f in found], [save_checks.SAME_TIME])

    def test_日をまたぐ3直は通る(self) -> None:
        self.assertEqual(save_checks.check_row_times(
            [row(1, kz="22", kh="30", sz="6", sh="30")], limit_minutes=480), [])

    def test_ページの変わり目の行は見送る(self) -> None:
        """次のページへ続く行(VBA `IsBridgeRow`)は、長くても構わない。"""
        rows = [row(12, page=1, kz="8", kh="0", sz="20", sh="0"),
                row(1, page=2, kz="20", kh="0", sz="20", sh="30")]
        self.assertEqual(save_checks.check_row_times(rows, limit_minutes=480), [])

    def test_最終ページの最後の行は見送らない(self) -> None:
        found = save_checks.check_row_times(
            [row(1, page=1, kz="8", kh="0", sz="20", sh="0")], limit_minutes=480)
        self.assertEqual(len(found), 1)

    def test_上限が分からなければ長さは見ない(self) -> None:
        self.assertEqual(save_checks.check_row_times(
            [row(1, kz="8", kh="0", sz="20", sh="0")], limit_minutes=0), [])

    def test_打ちかけの行は見ない(self) -> None:
        self.assertEqual(save_checks.check_row_times(
            [row(1, kz="8", kh="0", sz="", sh="")], limit_minutes=480), [])


class ShiftEndTests(unittest.TestCase):
    """定時エンドチェック + Last_Confi 前半: 最終行が定時に終わっているか。"""

    def test_定時ちょうどなら通す(self) -> None:
        self.assertEqual(save_checks.check_shift_end(
            [row(1, sz="6", sh="30")], "3直", "06:30"), [])

    def test_途中で止まっていたら挙げる(self) -> None:
        found = save_checks.check_shift_end(
            [row(1, sz="4", sh="0")], "3直", "06:30")
        self.assertEqual([f.code for f in found], [save_checks.SHIFT_END])
        self.assertIn("04:00", found[0].message)

    def test_見るのは最終ページの最後の行(self) -> None:
        rows = [row(1, page=1, sz="6", sh="30"), row(1, page=2, sz="4", sh="0")]
        found = save_checks.check_shift_end(rows, "3直", "06:30")
        self.assertEqual(found[0].page, 2)

    def test_日勤の残業は通す(self) -> None:
        """VBA では前段(定時エンド)に阻まれて一度も通らなかった道。

        通さないと、残業した日勤はどうやっても保存できない。
        """
        for end in ("18", "19"):
            self.assertEqual(save_checks.check_shift_end(
                [row(1, sz=end, sh="0")], "日勤", "17:00"), [], end)

    def test_日勤以外の残業は通さない(self) -> None:
        found = save_checks.check_shift_end(
            [row(1, sz="18", sh="0")], "1直", "17:00")
        self.assertEqual(len(found), 1)

    def test_通せる印が付く(self) -> None:
        found = save_checks.check_shift_end(
            [row(1, sz="4", sh="0")], "3直", "06:30")
        self.assertTrue(found[0].skippable)

    def test_定時が分からなければ見ない(self) -> None:
        self.assertEqual(save_checks.check_shift_end(
            [row(1, sz="4", sh="0")], "3直", ""), [])


class BreakTests(unittest.TestCase):
    """Last_Confi 後半: 休憩(停止の記号「0」)が60分あるか。"""

    def test_足りなければ挙げる(self) -> None:
        found = save_checks.check_break([row(1, s="0", th="30")], day_work=False)
        self.assertEqual([f.code for f in found], [save_checks.SHORT_BREAK])
        self.assertIn("30分", found[0].message)

    def test_足りていれば通す(self) -> None:
        self.assertEqual(save_checks.check_break(
            [row(1, s="0", th="45"), row(2, ss="0", ths="15")],
            day_work=False), [])

    def test_昼稼働なら見ない(self) -> None:
        self.assertEqual(save_checks.check_break(
            [row(1)], day_work=True), [])

    def test_休憩の小数も切り捨てずに足す(self) -> None:
        """30.5 + 29.5 = 60。切り捨てると 59 分と数えて断っていた。"""
        self.assertEqual(save_checks.check_break(
            [row(1, s="0", th="30.5"), row(2, s="0", th="29.5")],
            day_work=False), [])
        found = save_checks.check_break([row(1, s="0", th="59.5")], day_work=False)
        self.assertIn("59.5分", found[0].message)

    def test_記号が0の停止だけ数える(self) -> None:
        self.assertEqual(save_checks.break_minutes(
            [row(1, s="0", th="30", ss="イ", ths="90")]), 30)

    def test_通せる印が付く(self) -> None:
        found = save_checks.check_break([row(1)], day_work=False)
        self.assertTrue(found[0].skippable)


class TotalWorkTests(unittest.TestCase):
    """Opetime_Calcul: 作業時間の合計が直の長さを超えていないか。"""

    def test_超えたら挙げる(self) -> None:
        found = save_checks.check_total_work(
            [row(1, tim="300"), row(2, tim="300")], "1直", 480)
        self.assertEqual([f.code for f in found], [save_checks.OVER_SHIFT_TOTAL])
        self.assertIn("120分", found[0].message)     # 超過ぶん

    def test_ちょうどは通す(self) -> None:
        self.assertEqual(save_checks.check_total_work(
            [row(1, tim="480")], "1直", 480), [])

    def test_小数の作業時間も切り捨てずに足す(self) -> None:
        """240.5 + 240 = 480.5 は 480 分の直を超える(切り捨てると 480 で通っていた)。"""
        found = save_checks.check_total_work(
            [row(1, tim="240.5"), row(2, tim="240")], "1直", 480)
        self.assertEqual([f.code for f in found], [save_checks.OVER_SHIFT_TOTAL])
        self.assertIn("合計 480.5分 / 超過 0.5分", found[0].message)

    def test_停止の小数でマイナスになれば挙げる(self) -> None:
        found = save_checks.check_negative_time([row(
            1, kz="07", kh="00", sz="08", sh="00", th="60.5")])
        self.assertEqual([f.code for f in found], [save_checks.NEGATIVE_TIME])

    def test_長さが分からなければ見ない(self) -> None:
        self.assertEqual(save_checks.check_total_work(
            [row(1, tim="9999")], "1直", 0), [])


class RunAllTests(unittest.TestCase):
    def test_全部まとめて返る(self) -> None:
        rows = [
            row(1, lot="A", ken="1", mai="9", tut="9", s="ZZ", th="10",
                kz="8", kh="0", sz="8", sh="0", tim="600"),
        ]
        found = save_checks.run_all(
            rows, shift="1直", shift_start="08:00", shift_end="17:00",
            limit_minutes=540, known_stop_codes=["0"])
        codes = {f.code for f in found}
        self.assertEqual(codes, {
            save_checks.PACK_OVER, save_checks.UNKNOWN_STOP,
            save_checks.SAME_TIME, save_checks.SHIFT_END,
            save_checks.SHORT_BREAK, save_checks.OVER_SHIFT_TOTAL,
            # 開始8:00・終了8:00 で停止10分 → 作業時間がマイナス
            save_checks.NEGATIVE_TIME})


class NegativeTimeTests(unittest.TestCase):
    """停止が作業時間を超える行。**入力画面を通らずに入った値のため。**

    自動保存は打ちかけを残すために止めないので、直しきらないまま直が
    終わると、この形の行が手元に残ったまま共有へ出ようとします。
    """

    def test_stop_longer_than_the_work_time(self):
        found = save_checks.check_negative_time(
            [row(1, kz="8", kh="0", sz="9", sh="0", th="120")])
        self.assertEqual([f.code for f in found],
                         [save_checks.NEGATIVE_TIME])

    def test_a_stop_with_no_work_time_at_all(self):
        # 開始・終了が無くても停止だけ入っていれば 0 から引く(VBA と同じ)
        found = save_checks.check_negative_time([row(1, th="20")])
        self.assertEqual(len(found), 1)

    def test_the_three_stops_are_added_up(self):
        found = save_checks.check_negative_time(
            [row(1, kz="8", kh="0", sz="9", sh="0",
                 th="30", ths="20", tht="20")])   # 70 > 60
        self.assertEqual(len(found), 1)

    def test_a_row_that_fits_is_quiet(self):
        found = save_checks.check_negative_time(
            [row(1, kz="8", kh="0", sz="10", sh="0", th="30")])
        self.assertEqual(found, [])

    def test_a_row_without_stops_is_quiet(self):
        found = save_checks.check_negative_time(
            [row(1, kz="8", kh="0", sz="9", sh="0")])
        self.assertEqual(found, [])

    def test_it_points_at_the_stop_field(self):
        found = save_checks.check_negative_time([row(1, th="20")])
        self.assertEqual(found[0].field, "TH")
        self.assertEqual(found[0].row, 1)

    def test_行が無ければ何も言わない(self) -> None:
        self.assertEqual(save_checks.run_all([], shift="1直"), [])

    def test_通せるのは最終時間と休憩だけ(self) -> None:
        rows = [row(1, lot="A", ken="1", mai="2", tut="2", sz="4", sh="0")]
        found = save_checks.run_all(
            rows, shift="3直", shift_start="22:30", shift_end="06:30")
        left = save_checks.blocking(found, skip=True)
        self.assertEqual([f.code for f in left], [save_checks.PACK_OVER])

    def test_鳴らす音は1つだけ(self) -> None:
        rows = [row(1, lot="A", ken="1", mai="2", tut="2", s="ZZ")]
        found = save_checks.run_all(rows, shift="1直", known_stop_codes=["0"])
        self.assertEqual(save_checks.first_sound(found), "pack_over")

    def test_まとめの文言(self) -> None:
        self.assertIn("ありません", save_checks.summary([]))
        found = save_checks.check_break([row(1)], day_work=False)
        self.assertIn("1件", save_checks.summary(found))


class NormalizeTests(unittest.TestCase):
    def test_空白を落とす(self) -> None:
        self.assertEqual(save_checks.normalize(" 1 "), "1")
        self.assertEqual(save_checks.normalize("１　"), "1")

    def test_ゼロ埋めは残す(self) -> None:
        """VBA も `Val` を通さず文字列のまま比べていた。"""
        self.assertEqual(save_checks.normalize("01"), "01")


class FindingTests(unittest.TestCase):
    def test_場所の書き方(self) -> None:
        f = save_checks.Finding("x", "message", page=2, row=5)
        self.assertEqual(f.where, "2ページ 5行目")

    def test_場所が無ければ空(self) -> None:
        self.assertEqual(save_checks.Finding("x", "message").where, "")

    def test_辞書にできる(self) -> None:
        body = save_checks.Finding("x", "m", how="h", row=1).as_dict()
        self.assertEqual(body["code"], "x")
        self.assertEqual(body["how"], "h")
        self.assertIn("where", body)


if __name__ == "__main__":
    unittest.main()
