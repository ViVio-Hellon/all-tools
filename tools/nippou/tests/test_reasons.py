"""理由は行ごとに持つ ── 紙が1つだったのは**紙の都合**

梱包実績日報表の「ヨ：その他（理由を記載）」は欄が1つしかなく、VBA も
それに合わせてページに1つ(`daily_header.reason`)でした。ですが停止理由で
「その他」を選ぶのは行ごとで、1つのページに2つあれば理由も2つあります。

DBは行ごと(`daily_detail.reason`)、紙と共有へ出すときだけ行番号を付けて
1つにまとめます:

    3行目: 棚卸し準備 / 7行目: 点検表の差し替え

ここで守るのは:

    ・まとめ方は `logic/reasons` の1か所(紙・共有・画面で食い違わない)
    ・**行の順**に並ぶ(書いた順ではなく、紙を上から追う順)
    ・行ごとに持つ前のデータを**捨てない**(行が分からないものは頭に残す)
    ・「その他」を選んだ行にだけ欄が出る。**空欄を並べない**
    ・理由が空でも**保存は止めない**(後から書き足すこともある)
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.models import DetailRecord, HeaderRecord
from nippou.logic import reasons
from nippou.presenters import entry as presenter
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase


class CombineTests(unittest.TestCase):
    def test_one_row(self):
        self.assertEqual(reasons.combine({3: "棚卸し準備"}), "3行目: 棚卸し準備")

    def test_rows_are_in_order(self):
        # 書いた順ではなく、紙を上から追う順
        text = reasons.combine({7: "点検表の差し替え", 3: "棚卸し準備"})
        self.assertEqual(text, "3行目: 棚卸し準備 / 7行目: 点検表の差し替え")

    def test_empty_rows_are_skipped(self):
        # 行だけあって中身が無いものを「3行目: 」と出しても読めない
        self.assertEqual(reasons.combine({3: "", 5: "  ", 7: "点検"}),
                         "7行目: 点検")

    def test_nothing_is_empty(self):
        self.assertEqual(reasons.combine({}), "")
        self.assertEqual(reasons.combine({1: "", 2: ""}), "")

    def test_a_comma_inside_the_reason_survives(self):
        # 区切りを読点にすると「点検表を差し替え、その後清掃」が切れる
        text = reasons.combine({1: "点検表を差し替え、その後清掃"})
        self.assertEqual(reasons.split(text), {1: "点検表を差し替え、その後清掃"})


class SplitTests(unittest.TestCase):
    """`combine` の逆。**古い書き方も読める。**"""

    def test_round_trip(self):
        by_row = {3: "棚卸し準備", 7: "点検表の差し替え"}
        self.assertEqual(reasons.split(reasons.combine(by_row)), by_row)

    def test_a_plain_reason_goes_to_row_zero(self):
        # 行ごとに持つ前のデータ。**捨てない**(どの行かが分からないだけ)
        self.assertEqual(reasons.split("棚卸し準備"), {0: "棚卸し準備"})

    def test_mixed_old_and_new(self):
        found = reasons.split("3行目: 棚卸し / むかし書いたもの")
        self.assertEqual(found, {3: "棚卸し", 0: "むかし書いたもの"})

    def test_empty(self):
        self.assertEqual(reasons.split(""), {})

    def test_a_stray_colon_is_not_a_row(self):
        self.assertEqual(reasons.split("14:30 に停止"), {0: "14:30 に停止"})


class NeedsReasonTests(unittest.TestCase):
    def test_any_of_the_three_stop_codes(self):
        for family in ("S", "SS", "STH"):
            with self.subTest(family=family):
                self.assertTrue(
                    reasons.needs_reason({family: "ヨ"}, {"ヨ"}))

    def test_another_code_does_not(self):
        self.assertFalse(reasons.needs_reason({"S": "0"}, {"ヨ"}))

    def test_no_other_codes_means_no(self):
        self.assertFalse(reasons.needs_reason({"S": "ヨ"}, set()))

    def test_missing_lists_the_empty_ones(self):
        self.assertEqual(reasons.missing({3: "書いた", 7: ""}, [3, 7]), [7])
        self.assertIsNone(reasons.first_missing({3: "書いた"}, [3]))
        self.assertEqual(reasons.first_missing({}, [7, 3]), 3)


class RecordRoundTripTests(unittest.TestCase):
    """`GridState` ↔ レコード。**行ごとに持ち、頭では1つにまとめる。**"""

    def state_with(self, by_row):
        state = presenter.empty_state()
        for row, text in by_row.items():
            state.set(row, "reason", text)
        return state

    def test_the_header_gets_the_combined_text(self):
        header, details = presenter.to_records(
            self.state_with({3: "棚卸し", 7: "点検"}),
            "2026年9月11日", "L-1", "1直", 1)
        self.assertEqual(header.reason, "3行目: 棚卸し / 7行目: 点検")
        # 行にもそのまま残る
        self.assertEqual(details[2].reason, "棚卸し")
        self.assertEqual(details[6].reason, "点検")

    def test_loading_puts_them_back_on_the_rows(self):
        header, details = presenter.to_records(
            self.state_with({3: "棚卸し"}), "2026年9月11日", "L-1", "1直", 1)
        state = presenter.from_records(header, details)
        self.assertEqual(state.value(3, "reason"), "棚卸し")
        # 頭には残さない(行へ戻したので)
        self.assertEqual(state.header["reason"], "")

    def test_old_data_is_read_back_onto_the_rows(self):
        """行ごとに持つ前のページ。**行番号が付いていれば行へ戻す。**"""
        header = HeaderRecord(report_date="2026年9月11日", line="L-1",
                              shift="1直", page=1,
                              reason="3行目: 棚卸し / 7行目: 点検")
        details = [DetailRecord(report_date="2026年9月11日", line="L-1",
                                shift="1直", page=1, row_no=r)
                   for r in range(1, 13)]
        state = presenter.from_records(header, details)
        self.assertEqual(state.value(3, "reason"), "棚卸し")
        self.assertEqual(state.value(7, "reason"), "点検")

    def test_a_plain_old_reason_is_kept_at_the_head(self):
        header = HeaderRecord(report_date="2026年9月11日", line="L-1",
                              shift="1直", page=1, reason="むかし書いたもの")
        details = [DetailRecord(report_date="2026年9月11日", line="L-1",
                                shift="1直", page=1, row_no=r)
                   for r in range(1, 13)]
        state = presenter.from_records(header, details)
        # どの行か分からないので頭に残す。**捨てない**
        self.assertEqual(state.header["reason"], "むかし書いたもの")
        # 保存し直しても消えない
        again, _ = presenter.to_records(state, "2026年9月11日", "L-1", "1直", 1)
        self.assertIn("むかし書いたもの", again.reason)

    def test_the_row_wins_over_the_head(self):
        """明細に理由があれば**そちらが正**(あとから直したもの)。"""
        header = HeaderRecord(report_date="2026年9月11日", line="L-1",
                              shift="1直", page=1, reason="3行目: ふるい")
        details = [DetailRecord(report_date="2026年9月11日", line="L-1",
                                shift="1直", page=1, row_no=r,
                                reason="あたらしい" if r == 3 else "")
                   for r in range(1, 13)]
        state = presenter.from_records(header, details)
        self.assertEqual(state.value(3, "reason"), "あたらしい")


class ReasonEntriesTests(unittest.TestCase):
    """どの行に欄を出すか。**空欄を並べない。**"""

    def test_only_the_rows_that_chose_other(self):
        state = presenter.empty_state()
        state.set(4, "SS", "ヨ")
        found = presenter.reason_entries(state, {"ヨ"})
        self.assertEqual([e["row"] for e in found], [4])
        self.assertTrue(found[0]["empty"])

    def test_a_written_reason_stays_even_if_the_code_changes(self):
        """記号を選び直しても、書いた文字は黙って消さない。"""
        state = presenter.empty_state()
        state.set(4, "reason", "棚卸し")
        found = presenter.reason_entries(state, {"ヨ"})
        self.assertEqual([e["row"] for e in found], [4])
        self.assertFalse(found[0]["needed"])
        self.assertFalse(found[0]["empty"])

    def test_nothing_chosen_means_no_entries(self):
        self.assertEqual(presenter.reason_entries(presenter.empty_state(),
                                                  {"ヨ"}), [])

    def test_entries_are_in_row_order(self):
        state = presenter.empty_state()
        state.set(7, "S", "ヨ")
        state.set(2, "STH", "ヨ")
        found = presenter.reason_entries(state, {"ヨ"})
        self.assertEqual([e["row"] for e in found], [2, 7])


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class ReasonScreenTests(WebTestCase):
    def make_master(self):
        ref = self.tmp / "ref"
        ref.mkdir(exist_ok=True)
        conn = sqlite3.connect(str(ref / "伝送用ファイル.sqlite3"))
        with conn:
            for table, rows in (
                ("作業停止時間内訳_1", [("1", "休憩食事", "0"),
                                        ("2", "その他（理由を記載）", "ヨ")]),
                ("作業停止時間内訳_2", [("1", "突発停止(機械)", "イ")]),
                ("作業停止時間内訳_3", [("1", "ビニール交換", "G")]),
            ):
                conn.execute(
                    f'CREATE TABLE "{table}" ("管理番号", "内訳", "内訳番号", "備考")')
                conn.executemany(
                    f'INSERT INTO "{table}" VALUES (?, ?, ?, "")', rows)
        conn.close()
        self.post("/api/settings/paths",
                  {"gw_reference_dir": str(ref), "password": "nisk"})

    def test_an_entry_appears_for_the_chosen_row(self):
        self.make_master()
        body = self.post("/api/entry/state",
                         {"rows": {"4": {"SS": "ヨ"}}}).get_json()
        self.assertEqual([e["row"] for e in body["reason_entries"]], [4])

    def test_two_rows_keep_two_reasons(self):
        self.make_master()
        self.post("/api/entry/save", {
            "rows": {"3": {"S": "ヨ", "reason": "棚卸し準備"},
                     "7": {"S": "ヨ", "reason": "点検表の差し替え"}},
            "header": {}, "checks": {}})
        html = self.get("/").get_data(as_text=True)
        self.assertIn("棚卸し準備", html)
        self.assertIn("点検表の差し替え", html)
        self.assertIn('id="reason-3"', html)
        self.assertIn('id="reason-7"', html)

    def test_the_paper_carries_both_with_row_numbers(self):
        self.make_master()
        self.post("/api/entry/save", {
            "rows": {"3": {"S": "ヨ", "reason": "棚卸し準備"},
                     "7": {"S": "ヨ", "reason": "点検表の差し替え"}},
            "header": {}, "checks": {}})
        from nippou import work_context
        ctx = work_context.get_context()
        key = self.repo().list_keys()[0]
        paper = self.get(f"/report/nippou?report_date={key[0]}&line={key[1]}"
                         f"&shift={key[2]}&page=1").get_data(as_text=True)
        # **紙の欄は1つのまま。** 行番号を付けて並べる
        self.assertIn("3行目: 棚卸し準備 / 7行目: 点検表の差し替え", paper)

    def test_an_empty_reason_does_not_block_the_save(self):
        """後から書き足すこともある。**打つ手を止めない。**"""
        self.make_master()
        res = self.post("/api/entry/save", {
            "rows": {"3": {"LOT": "N7131T0", "S": "ヨ", "TH": "0",
                           "KZ": "08", "KH": "00", "SZ": "10", "SH": "00"}},
            "header": {}, "checks": {}})
        self.assertEqual(res.status_code, 200)

    def test_the_shared_side_gets_the_combined_text(self):
        """共有の日報管理は欄が1つ(`T_日報ヘッダー` の [理由])。"""
        self.make_master()
        self.post("/api/entry/save", {
            "rows": {"3": {"S": "ヨ", "reason": "棚卸し準備"}},
            "header": {}, "checks": {}})
        key = self.repo().list_keys()[0]
        header, _ = self.repo().load(*key)
        self.assertEqual(header.reason, "3行目: 棚卸し準備")


if __name__ == "__main__":
    unittest.main()
