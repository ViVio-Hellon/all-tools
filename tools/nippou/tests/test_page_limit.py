"""ページを増やすときの歯止め ── **止めずに、いつもと違うことは言う**

VBA はブックに印刷用シートが3枚しか無く、そこが天井でした
(12行 × 3ページ = 36行/直)。業務としてそう決めたというより、シートを3枚
用意したらそうなった、という性質のものです ── 20年間それで足りていた
ので誰も困らなかった、という経緯を聞いています。

Web版にシートはないので、**制約だけを移植しません。** 直の途中で
「もう打てません」になるのが日報ツールとして一番まずい止まり方で、
現場は紙に書いて後で入れ直すことになります。そのかわり青天井にも
しません ── 誤操作でページが増え続けても誰も気づけないので。

ここで守るのは:

    ・3ページまでは何も言わない(いつもの範囲)
    ・4ページ目からは**聞き返す**。はいと言われたら通る(断らない)
    ・全停入力は**直に1回きり**。2回目は断る
    ・全停は**白紙のページがあればそこへ書く**(白紙を1枚目に残さない)
    ・それでも**打ってあるページは消さない**(VBA はシートを消していた)
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.models import DetailRecord, HeaderRecord
from nippou.logic import pages
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase


class NewPageWarningTests(unittest.TestCase):
    """4ページ目の一声。**断りではない。**"""

    def test_up_to_three_pages_says_nothing(self):
        for latest in (0, 1, 2):
            self.assertEqual(pages.new_page_warning(latest), "")

    def test_the_fourth_page_asks(self):
        # latest=3 は「いま3ページ目」= 次に作るのは4ページ目
        warning = pages.new_page_warning(3)
        self.assertTrue(warning)
        self.assertIn("3ページ", warning)
        self.assertIn("36行", warning)          # 12行 × 3ページ

    def test_it_keeps_asking_beyond_that(self):
        self.assertTrue(pages.new_page_warning(7))
        self.assertIn("7ページ", pages.new_page_warning(7))

    def test_the_wording_is_plain(self):
        # 画面へそのまま出す文言。**Markdown の印を混ぜない**
        self.assertNotIn("**", pages.new_page_warning(3))

    def test_the_threshold_can_move(self):
        self.assertEqual(pages.new_page_warning(3, usual=5), "")
        self.assertTrue(pages.new_page_warning(5, usual=5))


class AllStopRefusalTests(unittest.TestCase):
    def test_no_all_stop_yet_passes(self):
        self.assertEqual(pages.all_stop_refusal(None), "")
        self.assertEqual(pages.all_stop_refusal(0), "")

    def test_an_existing_one_is_refused_with_its_page(self):
        refusal = pages.all_stop_refusal(2)
        self.assertIn("第2ページ", refusal)
        # **直し方まで書く。** 断られただけでは消す手立てを探すことになる
        #
        # 「開いて直してください」だけでは足りませんでした ── 開き方も
        # 消し方も書いていないので、「消しても直せません」で止まります。
        # 押すものの名前(ボタンの字)まで入っているかを見ます
        self.assertIn("第2ページを開く", refusal)
        self.assertIn("✕", refusal)
        self.assertIn("保存(確定)", refusal)
        self.assertNotIn("**", refusal)


class AllStopRowTests(unittest.TestCase):
    """どの行が全停で書かれたものか。"""

    def test_a_row_without_a_lot_but_with_the_whole_shift(self):
        self.assertTrue(pages.is_all_stop_row(
            lot="", kz="08", kh="00", sz="16", sh="30", s="停電"))

    def test_a_typed_row_is_not_an_all_stop(self):
        # ロットがある = 打った行。時間が入っていても全停ではない
        self.assertFalse(pages.is_all_stop_row(
            lot="H1234S", kz="08", kh="00", sz="16", sh="30", s="停電"))

    def test_a_half_filled_row_is_not_an_all_stop(self):
        self.assertFalse(pages.is_all_stop_row(
            lot="", kz="08", kh="00", sz="", sh="", s="停電"))
        # 理由が無ければ全停ではない(ただの時間入力)
        self.assertFalse(pages.is_all_stop_row(
            lot="", kz="08", kh="00", sz="16", sh="30", s=""))

    def test_an_empty_row_is_not_an_all_stop(self):
        self.assertFalse(pages.is_all_stop_row())

    def test_used_page(self):
        self.assertTrue(pages.used_page(["H1234S", "", ""]))
        self.assertFalse(pages.used_page(["", "  ", None]))
        self.assertFalse(pages.used_page([]))


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class NewPageScreenTests(WebTestCase):
    """4ページ目を出すところ。**聞き返して、通す。**"""

    def now(self):
        from nippou import work_context

        from app.routes.entry import build_service, current_calculator

        ctx = work_context.get_context()
        with self.app.test_request_context():
            service = build_service(ctx, current_calculator())
            shift, day = service.current_shift_info(
                datetime.now(), ctx.force_day_shift())
        return day, ctx.line, shift

    def save_pages(self, count):
        day, line, shift = self.now()
        for page in range(1, count + 1):
            self.repo().save(
                HeaderRecord(report_date=day, line=line, shift=shift,
                             page=page, worker="山田"),
                [DetailRecord(report_date=day, line=line, shift=shift,
                              page=page, row_no=1, lot=f"P{page}ROW1",
                              con="100", wei="1000")])
        return day, line, shift

    def typed(self, lot="NEWROW"):
        """**12行ぜんぶ打った状態**の本文。

        「次ページ発行」は12行目まで埋まってからしか通りません
        (`logic/pages.new_page_check`)── 1行だけで押せた頃は、1行の紙が
        何枚も残りました。ここで見たいのは*そのあと*の聞き返しなので、
        12行は埋めておきます。**12行目は終了まで**(v4.8.0 から、写っただけの
        開始時刻やロット№だけでは「埋まった」と見ません)。
        """
        # **LOTは7桁まで。** 長い名前で12行作ると8桁目が切られ、
        # `NEWROW10` と `NEWROW1` が同じになって「LOT重複」で断られます
        rows = {str(n): {"LOT": f"{lot[:4]}{n:02d}", "CON": "10", "WEI": "100"}
                for n in range(1, 13)}
        rows["12"].update(KZ="08", KH="00", SZ="08", SH="30")
        return {"rows": rows, "header": {"worker": "山田"}, "checks": {}}

    def test_the_second_page_goes_through_silently(self):
        self.save_pages(1)
        res = self.post("/api/entry/newpage", self.typed())
        self.assertEqual(res.status_code, 200)

    def test_the_third_page_goes_through_silently(self):
        self.save_pages(2)
        res = self.post("/api/entry/newpage", self.typed())
        self.assertEqual(res.status_code, 200)

    def test_the_fourth_page_asks_first(self):
        day, line, shift = self.save_pages(3)
        res = self.post("/api/entry/newpage", self.typed())
        self.assertEqual(res.status_code, 422)
        body = res.get_json()
        self.assertTrue(body["needs_confirm"])
        self.assertEqual(body["error"]["code"], "many_pages")
        self.assertIn("3ページ", body["error"]["message"])
        # **何も作っていない。** 聞いているだけ
        self.assertEqual(self.repo().saved_pages(day, line, shift), [1, 2, 3])

    def test_saying_yes_makes_the_page(self):
        day, line, shift = self.save_pages(3)
        res = self.post("/api/entry/newpage",
                        {**self.typed(), "confirm": True})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.repo().saved_pages(day, line, shift),
                         [1, 2, 3, 4])

    def test_it_never_becomes_a_hard_wall(self):
        """**打てなくはしない。** 直の途中で止まるのが一番まずい。"""
        self.save_pages(3)
        for _ in range(3):
            res = self.post("/api/entry/newpage",
                            {**self.typed(), "confirm": True})
            self.assertEqual(res.status_code, 200)
        day, line, shift = self.now()
        # 1回押すごとに1ページ増える(打ったページを保存 → 次のページを空で作る)
        self.assertEqual(self.repo().saved_pages(day, line, shift),
                         [1, 2, 3, 4, 5, 6])

    def test_an_empty_page_is_still_refused(self):
        # 4ページ目の聞き返しより先に、空のページの断りが来る
        self.save_pages(3)
        res = self.post("/api/entry/newpage",
                        {"rows": {}, "header": {}, "checks": {}})
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"], "page_not_full")


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class AllStopScreenTests(WebTestCase):
    """全停入力。**直に1回きり、白紙は使い回す。**"""

    def now(self):
        from nippou import work_context

        from app.routes.entry import build_service, current_calculator

        ctx = work_context.get_context()
        with self.app.test_request_context():
            service = build_service(ctx, current_calculator())
            shift, day = service.current_shift_info(
                datetime.now(), ctx.force_day_shift())
        return day, ctx.line, shift

    def test_the_first_one_lands_on_page_one(self):
        res = self.post("/api/formstop/execute", {"reason": "停電", "code": "1", "worker": "山田"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["page"], 1)

    def test_a_blank_page_is_reused_not_appended(self):
        """白紙のページを残したまま足さない(VBA はそのシートを消していた)。"""
        day, line, shift = self.now()
        self.repo().save(
            HeaderRecord(report_date=day, line=line, shift=shift, page=1,
                         worker="山田"),
            [DetailRecord(report_date=day, line=line, shift=shift, page=1,
                          row_no=1, lot="")])
        res = self.post("/api/formstop/execute", {"reason": "停電", "code": "1", "worker": "山田"})
        self.assertEqual(res.get_json()["page"], 1)
        self.assertEqual(self.repo().saved_pages(day, line, shift), [1])

    def test_a_typed_page_is_not_overwritten(self):
        """**打ってあるものは消さない。** VBA はシートごと消していた。"""
        day, line, shift = self.now()
        self.repo().save(
            HeaderRecord(report_date=day, line=line, shift=shift, page=1,
                         worker="山田"),
            [DetailRecord(report_date=day, line=line, shift=shift, page=1,
                          row_no=1, lot="UTTA", con="100", wei="1000")])
        res = self.post("/api/formstop/execute", {"reason": "停電", "code": "1", "worker": "山田"})
        self.assertEqual(res.get_json()["page"], 2)
        _, details = self.repo().load(day, line, shift, 1)
        self.assertEqual(details[0].lot, "UTTA")

    def test_pressing_twice_is_refused(self):
        self.post("/api/formstop/execute", {"reason": "停電", "code": "1", "worker": "山田"})
        res = self.post("/api/formstop/execute", {"reason": "停電", "code": "1", "worker": "山田"})
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"], "already_all_stop")

    def test_pressing_twice_makes_no_page(self):
        self.post("/api/formstop/execute", {"reason": "停電", "code": "1", "worker": "山田"})
        self.post("/api/formstop/execute", {"reason": "別の理由", "worker": "山田"})
        day, line, shift = self.now()
        self.assertEqual(self.repo().saved_pages(day, line, shift), [1])

    def test_the_refusal_names_the_page_to_fix(self):
        self.post("/api/formstop/execute", {"reason": "停電", "code": "1", "worker": "山田"})
        body = self.post("/api/formstop/execute", {"reason": "停電", "code": "1", "worker": "山田"}).get_json()
        self.assertEqual(body["page"], 1)
        self.assertIn("第1ページを開く", body["error"]["message"])

    def test_another_shift_is_not_blocked(self):
        """断るのは**その直**だけ。他の直の全停まで塞がない。"""
        day, line, shift = self.now()
        other = "3直" if shift != "3直" else "1直"
        self.repo().save(
            HeaderRecord(report_date=day, line=line, shift=other, page=1,
                         worker="山田"),
            [DetailRecord(report_date=day, line=line, shift=other, page=1,
                          row_no=1, lot="", kz="08", kh="00", sz="16",
                          sh="30", s="停電", th="510")])
        # **共有へ出したことにする。** 出ていないままだと、前の直の
        # 引き継ぎ(`logic/handover.py`)が先に止めます ── そちらが
        # 見たいのは `test_handover.py` のほうで、ここは全停の話です
        self.repo().mark_synced((day, line, other, 1))
        res = self.post("/api/formstop/execute", {"reason": "停電", "code": "1", "worker": "山田"})
        self.assertEqual(res.status_code, 200)

    # ------------------------------------------------------------------
    # **作業者を決める前には押させない。**
    #
    # 「作業者を選ぶ前に全停入力できてしまう」と言われたところです。
    # 全停は押した時点で1ページを書いて保存するので、その前に通すと
    # **誰の直か分からない紙**が1枚できます。
    # ------------------------------------------------------------------
    def test_作業者がいなければ断る(self):
        res = self.post("/api/formstop/execute", {"reason": "停電", "code": "1"})
        self.assertEqual(res.status_code, 422)
        body = res.get_json()
        self.assertEqual(body["error"]["code"], "no_worker")
        self.assertIn("作業者を選ぶ", body["error"]["message"])

    def test_断ったならページも作らない(self):
        self.post("/api/formstop/execute", {"reason": "停電", "code": "1"})
        day, line, shift = self.now()
        self.assertEqual(self.repo().saved_pages(day, line, shift), [])

    def test_作業者は全停のページにも残る(self):
        """VBA は `.Sname = ""` で消していた。**残す** ── 全停の紙も
        「その直の紙」で、誰が居たのか分からない1枚が混ざると読めない。
        """
        self.post("/api/formstop/execute",
                  {"reason": "停電", "code": "1", "worker": "山田 田中"})
        day, line, shift = self.now()
        header, _ = self.repo().load(day, line, shift, 1)
        self.assertEqual(header.worker, "山田 田中")


if __name__ == "__main__":
    unittest.main()
