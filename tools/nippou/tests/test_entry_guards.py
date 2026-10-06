"""日報入力の打ち心地 ── **ロットの桁・打つ順・直るまで保存させない**

現場から出た3つの困りごとを直したぶんです。

    ロット№が8桁打ててしまう   現物は7桁ちょうど(`N7131T0` `L715C50`)。
                              ロット検索は7桁で走るので、8桁目は
                              「打てたのに引けない」になる
    焦点が下の行へ飛ぶ         終了時刻を入れた瞬間に次の行へ連れて
                              行かれ、同じ行の梱包数・重量・実働が
                              空のまま残る
    警告が出ても入力が進む     「作業時間がマイナスです」と出たまま
                              保存でき、気づくのは翌日の集計

ここで守るのは:

    ・ロットは7桁で頭打ち。**全角で打たれても弾かずに半角へ直す**
    ・終了時刻は次の行へ**値だけ**運ぶ(焦点は動かさない = VBAどおり)
    ・直っていない行があるうちは**保存(確定)を断る**。行にも印が残る
    ・自動保存は止めない(打ちかけが手元から消えるほうが困る)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import input_rules, work_time
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase


class LotLengthTests(unittest.TestCase):
    """ロット№は7桁の英数字。**直せるものは直してから受ける。**"""

    def test_seven_is_kept(self):
        self.assertEqual(input_rules.normalize("LOT", "N7131T0"), "N7131T0")

    def test_the_eighth_character_is_cut(self):
        self.assertEqual(input_rules.normalize("LOT", "N7131T0X"), "N7131T0")

    def test_full_width_becomes_half_width(self):
        # IME が全角のままだった / 貼り付けた元が全角だった、はどちらも起きる。
        # **弾くと「打てているのに入らない」になる**
        self.assertEqual(input_rules.normalize("LOT", "Ｎ７１３１Ｔ０"), "N7131T0")

    def test_lower_case_becomes_upper(self):
        self.assertEqual(input_rules.normalize("LOT", "n7131t0"), "N7131T0")

    def test_symbols_are_dropped(self):
        self.assertEqual(input_rules.normalize("LOT", "N-7131/T0"), "N7131T0")

    def test_spaces_are_dropped(self):
        self.assertEqual(input_rules.normalize("LOT", " N7131T0 "), "N7131T0")

    def test_empty_stays_empty(self):
        self.assertEqual(input_rules.normalize("LOT", ""), "")

    def test_other_fields_are_untouched(self):
        # 材・調質やサイズは自由入力。**勝手に大文字にしない**
        self.assertEqual(input_rules.normalize("ZAI", "3SCA-h14"), "3SCA-h14")

    def test_short_numeric_fields_are_still_capped(self):
        self.assertEqual(input_rules.normalize("KZ", "0812"), "08")

    def test_the_screen_gets_the_limit(self):
        self.assertEqual(input_rules.as_attributes("LOT")["maxlength"], "7")


class AllProblemsTests(unittest.TestCase):
    """**12行のどこが直っていないか**を全部出す(`compute` は最初の1つ)。"""

    def rows(self, by_row):
        """12行ぶんの入れもの。`{行番号: {欄: 値}}` を重ねる。"""
        base = {r: {} for r in range(1, 13)}
        base.update(by_row)
        return base

    def test_a_clean_sheet_has_none(self):
        found = work_time.problems(self.rows(
            {1: {"KZ": "08", "KH": "00", "SZ": "10", "SH": "00"}}))
        self.assertEqual(found, [])

    def test_every_bad_row_is_listed(self):
        found = work_time.problems(self.rows({
            # 停止が作業時間を超える
            1: {"KZ": "08", "KH": "00", "SZ": "09", "SH": "00", "TH": "120"},
            # 時の範囲が変
            3: {"KZ": "99", "KH": "00", "SZ": "10", "SH": "00"},
            # 開始と終了が同じ
            5: {"KZ": "08", "KH": "00", "SZ": "08", "SH": "00"},
        }))
        self.assertEqual([p.row for p in found], [1, 3, 5])

    def test_compute_still_stops_at_the_first(self):
        """計算のほうは VBA どおり ── 混ぜると計算の順が変わる。"""
        rows = self.rows({
            1: {"KZ": "99", "KH": "00", "SZ": "10", "SH": "00"},
            3: {"KZ": "08", "KH": "00", "SZ": "08", "SH": "00"},
        })
        result = work_time.compute(rows)
        self.assertEqual(result.problem.row, 1)
        self.assertEqual(len(work_time.problems(rows)), 2)

    def test_rows_are_in_order(self):
        # 直す人は上から見る。見つけた順や重さの順に並べ替えない
        found = work_time.problems(self.rows({
            7: {"KZ": "08", "KH": "00", "SZ": "08", "SH": "00"},
            2: {"KZ": "99", "KH": "00"},
        }))
        self.assertEqual([p.row for p in found], [2, 7])

    def test_a_stop_without_a_work_time_is_negative(self):
        # 停止だけ入っている行は 0 から引くのでマイナス(VBA と同じ)
        found = work_time.problems(self.rows({1: {"TH": "20"}}))
        self.assertEqual([p.reason for p in found], [work_time.REFUSE_NEGATIVE])

    def test_the_wording_is_plain(self):
        found = work_time.problems(self.rows({1: {"TH": "20"}}))
        self.assertNotIn("**", found[0].message)


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class EntryGuardScreenTests(WebTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.post("/api/entry/line", {"line": "L-1"})

    def ok_row(self, **extra):
        row = {"LOT": "N7131T0", "KZ": "08", "KH": "00",
               "SZ": "10", "SH": "00"}
        row.update(extra)
        return {"rows": {"1": row}, "header": {"worker": "山田"}, "checks": {}}

    # -- ロット --------------------------------------------------------
    def test_an_over_long_lot_is_cut_on_the_way_in(self):
        body = self.post("/api/entry/state",
                         {"rows": {"1": {"LOT": "N7131T0XYZ"}}}).get_json()
        self.assertEqual(body["rows"]["1"]["LOT"], "N7131T0")

    def test_a_full_width_lot_is_folded(self):
        body = self.post("/api/entry/state",
                         {"rows": {"1": {"LOT": "Ｎ７１３１Ｔ０"}}}).get_json()
        self.assertEqual(body["rows"]["1"]["LOT"], "N7131T0")

    def test_the_grid_carries_the_limit(self):
        html = self.get("/").get_data(as_text=True)
        self.assertIn('id="LOT1"', html)
        self.assertIn('maxlength="7"', html)

    # -- 打つ順 --------------------------------------------------------
    def test_the_end_time_carries_without_moving_the_focus(self):
        """**値だけ運ぶ。** 焦点はその行に残す(VBA `Same_Text` と同じ)。"""
        body = self.post("/api/entry/state", {
            "rows": {"1": {"KZ": "08", "KH": "00", "SZ": "10", "SH": "00"}},
            "row": 1,
        }).get_json()
        # 次の行の開始時刻には入る
        self.assertEqual(body["rows"]["2"]["KZ"], "10")
        self.assertEqual(body["rows"]["2"]["KH"], "00")
        # **焦点は動かさない** ── 同じ行の梱包数・重量がまだ空なので
        self.assertEqual(body["focus"], "")

    def test_直の終わりの時刻を打ったら写さない(self):
        """現場の終了時刻は 15:00 / 22:50 / 07:00。

        そこで直が終わったのなら次の行に続きは無く、写せば
        「終わった時刻」が次の行の開始として残ります。

        **打っている直のぶんだけでなく、3つとも見ます** ── 直の
        変わり目をまたいで打っているとき、画面の直と打っている時刻は
        食い違うためです(判断は `logic/navigation.same_text`)。
        """
        repo = self.repo()
        repo.set_shift_time("1", "07:00", "15:00")
        repo.set_shift_time("2", "15:00", "22:50")
        repo.set_shift_time("3", "22:50", "07:00")
        for sz, sh in (("15", "00"), ("22", "50"), ("07", "00")):
            with self.subTest(終了=f"{sz}:{sh}"):
                body = self.post("/api/entry/state", {
                    "rows": {"1": {"KZ": "08", "KH": "00",
                                   "SZ": sz, "SH": sh}},
                    "row": 1,
                }).get_json()
                self.assertEqual(body["rows"]["2"]["KZ"], "")
                self.assertEqual(body["rows"]["2"]["KH"], "")

    def site_times(self):
        repo = self.repo()
        repo.set_shift_time("1", "07:00", "15:00")
        repo.set_shift_time("2", "15:00", "22:50")
        repo.set_shift_time("3", "22:50", "07:00")

    def test_写した行に印が付いて返る(self):
        body = self.post("/api/entry/state", {
            "rows": {"1": {"KZ": "08", "KH": "00", "SZ": "10", "SH": "00"}},
            "row": 1,
        }).get_json()
        self.assertEqual(body["carried"], [2])

    def test_直の終わりに直したら写したぶんが引っ込む(self):
        """**2行目にロットNoが入っていても引っ込みます。**

        「2行目が空なら消す」では、先にロットNoを打たれた時点で
        効かなくなります ── 直しに気づくのは先へ進んだあとなので、
        肝心な場面で効きません。印で見分けます。
        """
        self.site_times()
        body = self.post("/api/entry/state", {
            "rows": {"1": {"KZ": "08", "KH": "00", "SZ": "15", "SH": "00"},
                     "2": {"LOT": "N7131T0", "KZ": "14", "KH": "59"}},
            "row": 1, "carried": [2],
        }).get_json()
        self.assertEqual(body["rows"]["2"]["KZ"], "")
        self.assertEqual(body["rows"]["2"]["KH"], "")
        # ロットNoは残る ── 消すのは写した開始時刻だけ
        self.assertEqual(body["rows"]["2"]["LOT"], "N7131T0")
        self.assertEqual(body["carried"], [])

    def test_人が打った開始時刻は消さない(self):
        """印が無い = 人が打った。**迷ったら消さない。**"""
        self.site_times()
        body = self.post("/api/entry/state", {
            "rows": {"1": {"KZ": "08", "KH": "00", "SZ": "15", "SH": "00"},
                     "2": {"KZ": "14", "KH": "59"}},
            "row": 1,
        }).get_json()
        self.assertEqual(body["rows"]["2"]["KZ"], "14")
        self.assertEqual(body["rows"]["2"]["KH"], "59")

    def test_おかしな印は読み飛ばす(self):
        """画面から来るものは信用しきらない(13行目・文字・数でないもの)。"""
        self.site_times()
        body = self.post("/api/entry/state", {
            "rows": {"1": {"KZ": "08", "KH": "00", "SZ": "15", "SH": "00"},
                     "2": {"KZ": "14", "KH": "59"}},
            "row": 1, "carried": [99, "あ", None, 0],
        }).get_json()
        self.assertEqual(body["rows"]["2"]["KZ"], "14")
        self.assertEqual(body["carried"], [])

    # -- 空の紙を作らない ----------------------------------------------
    def test_1行も打っていない紙は作らない(self):
        """**作業者を選んだだけで空のページが1枚書かれていました。**

        害が無いように見えて、そうではありません ── 共有へ未送信に
        数えられ、紙の束に空の紙が並び、直が終わると「まだ共有へ渡して
        いません」と全画面が出ます。ところが休憩60分が無いので共有へは
        出せない ── 誰も何もしていない直が、片付けようのない宿題として
        残ります。
        """
        empty = {"rows": {str(i): {} for i in range(1, 13)},
                 "header": {"worker": "山田"}, "checks": {}}
        res = self.post("/api/entry/save", empty)
        self.assertEqual(res.status_code, 422)
        self.assertIn("1行も打っていません", res.get_json()["message"])
        self.assertEqual(self.repo().list_keys(), [])
        self.assertEqual(self.repo().pending_sync_headers(), [])

    def test_自動保存は黙って見送る(self):
        """打ち始めれば自然に保存されるので、騒ぎません。"""
        body = self.post("/api/entry/save", {
            "rows": {}, "header": {"worker": "山田"}, "checks": {},
            "silent": True}).get_json()
        self.assertFalse(body["saved"])
        self.assertIn("1行も打っていない", body["skipped"])

    def test_停止だけの行も中身として数える(self):
        """`used_rows` は停止だけの行を数えません ── そちらは**紙の行数**を
        数えるものなので、保存してよいかの判断には使えません。

        (保存そのものは別の関門に当たります ── 作業時間の無い行に
        60分の停止を入れれば「作業時間がマイナス」で断られます。ここで
        見たいのは**中身と見なすか**だけなので、そこを直に見ます)
        """
        from nippou.presenters import entry as presenter

        state = presenter.parse_state(
            {"rows": {"3": {"S": "0", "TH": "60"}}, "header": {}, "checks": {}})
        self.assertTrue(presenter.sheet_has_anything(state))
        self.assertEqual(presenter.used_rows(state), 0, "紙の行数は0のまま")

    def test_理由だけでも中身として数える(self):
        from nippou.presenters import entry as presenter

        state = presenter.parse_state(
            {"rows": {"5": {"reason": "棚卸し準備"}},
             "header": {}, "checks": {}})
        self.assertTrue(presenter.sheet_has_anything(state))

    def test_作業者だけでは中身にしない(self):
        """**作業者を選んだだけでは、まだ何もしていません。**"""
        from nippou.presenters import entry as presenter

        state = presenter.parse_state(
            {"rows": {}, "header": {"worker": "山田"}, "checks": {}})
        self.assertFalse(presenter.sheet_has_anything(state))

    def test_打ったあとなら全部消して保存できる(self):
        """**消すのは正しい操作。** ここで止めると打ち間違いを消せません。"""
        self.post("/api/entry/save", self.ok_row(CON="100", WEI="1000"))
        res = self.post("/api/entry/save", {
            "rows": {str(i): {} for i in range(1, 13)},
            "header": {"worker": "山田"}, "checks": {}})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(self.repo().list_keys()), 1)

    # -- 直るまで保存させない ------------------------------------------
    def test_a_bad_row_blocks_the_save(self):
        res = self.post("/api/entry/save", self.ok_row(TH="900"))
        self.assertEqual(res.status_code, 422)
        self.assertEqual([b["row"] for b in res.get_json()["bad_rows"]], [1])

    def test_nothing_is_written_when_blocked(self):
        self.post("/api/entry/save", self.ok_row(TH="900"))
        self.assertEqual(self.repo().list_keys(), [])

    def test_the_rows_are_marked_while_typing(self):
        body = self.post("/api/entry/state", self.ok_row(TH="900")).get_json()
        # 打っている最中から印は出る(**止めはしない**)
        self.assertEqual([b["row"] for b in body["bad_rows"]], [1])

    def test_a_clean_sheet_saves(self):
        res = self.post("/api/entry/save", self.ok_row(CON="100", WEI="1000"))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["bad_rows"], [])

    def test_the_message_names_every_row(self):
        res = self.post("/api/entry/save", {
            "rows": {"1": {"LOT": "N7131T0", "TH": "20"},
                     "2": {"LOT": "N7131T1", "KZ": "99"}},
            "header": {"worker": "山田"}, "checks": {}})
        message = res.get_json()["message"]
        self.assertIn("1行目", message)
        self.assertIn("2行目", message)

    def test_the_screen_has_a_place_to_say_it(self):
        html = self.get("/").get_data(as_text=True)
        self.assertIn('id="bad-note"', html)


if __name__ == "__main__":
    unittest.main()
