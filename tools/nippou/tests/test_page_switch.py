"""ページを戻って直す ── **2ページ目を出したあと、1ページ目を書き直せること**

紙は12行しかないので、使い切ったら2ページ目を出します。そのあと1ページ目の
打ち間違いに気づくのは普通に起きます。VBA は `NippouDB_EditPage` を
「過去ページの修正は管理者モードでのみ可能です」と断っていましたが、
**同じ直の中でページを戻るのは自分がさっき打った紙を自分で直している**
だけなので、関門を外しました。

ここで守るのは:

    ・同じ直・同じラインのページ移動は**誰でも**通る
    ・他の日・他の直・他のラインは**管理者モードだけ**(そこは他人の記録)
    ・どちらの判断も `recall_refusal` 1か所から出る(道と画面は決めない)
    ・終わった直でも**ページを選んで**呼び出せる(一覧がページ1しか開けなかった)
    ・直した内容が、DBにも紙にも載る
    ・動くのはページだけ ── 日付も直もラインも動かない
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.models import DetailRecord, HeaderRecord
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase

PAST = "2026年8月2日"


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class PageSwitchTests(WebTestCase):
    """いまの直の中でページを戻る。**管理者モードは要らない。**"""

    def now(self):
        """いまの (報告日, ライン, 直)。"""
        from nippou import work_context

        from app.routes.entry import build_service, current_calculator

        ctx = work_context.get_context()
        with self.app.test_request_context():
            service = build_service(ctx, current_calculator())
            shift, day = service.current_shift_info(
                datetime.now(), ctx.force_day_shift())
        return day, ctx.line, shift

    def save_pages(self, pages, *, day=None, line=None, shift=None):
        now_day, now_line, now_shift = self.now()
        day, line = day or now_day, line or now_line
        shift = shift or now_shift
        repo = self.repo()
        for page in pages:
            repo.save(
                HeaderRecord(report_date=day, line=line, shift=shift,
                             page=page, worker="山田"),
                [DetailRecord(report_date=day, line=line, shift=shift,
                              page=page, row_no=1, lot=f"MOTO-P{page}",
                              con="100", wei="1000")])
        return day, line, shift

    # -- ページを戻れること ------------------------------------------------
    def test_switch_back_to_page_one(self):
        day, line, shift = self.save_pages([1, 2])
        res = self.post("/api/settings/page", {"page": 1})
        self.assertEqual(res.status_code, 200)

        body = self.post("/api/entry/state", {}).get_json()
        self.assertEqual(body["page"], 1)
        # 中身は画面そのものから見る(`/api/entry/state` は送った値を返す道)
        html = self.get("/").get_data(as_text=True)
        self.assertIn('value="MOTO-P1"', html)

    def test_only_the_page_moves(self):
        day, line, shift = self.save_pages([1, 2])
        self.post("/api/settings/page", {"page": 1})
        body = self.post("/api/entry/state", {}).get_json()
        # **日付も直もラインも動かない。** ここが `recall` との違い
        self.assertEqual(body["report_date"], day)
        self.assertEqual(body["shift"], shift)
        self.assertEqual(body["line"], line)

    def test_the_screen_offers_the_pages(self):
        self.save_pages([1, 2, 3])
        body = self.post("/api/entry/state", {}).get_json()
        self.assertEqual(body["pages"], [1, 2, 3])

    def test_gaps_are_not_invented(self):
        # ページ1と3だけ保存されているとき、2を選べては困る
        self.save_pages([1, 3])
        body = self.post("/api/entry/state", {}).get_json()
        self.assertEqual(body["pages"], [1, 3])

    def test_one_page_offers_no_switch(self):
        self.save_pages([1])
        html = self.get("/").get_data(as_text=True)
        # 選ぶ先が1つしかない欄を置かない
        self.assertNotIn('id="page-switch"', html)

    def test_the_switch_is_on_the_entry_screen(self):
        self.save_pages([1, 2])
        html = self.get("/").get_data(as_text=True)
        # **打ち間違いに気づく場所に置く**(設定画面の奥ではなく)
        self.assertIn('id="page-switch"', html)

    def test_the_settings_screen_no_longer_holds_it(self):
        self.save_pages([1, 2])
        html = self.get("/settings").get_data(as_text=True)
        self.assertNotIn('id="page-pick"', html)
        self.assertNotIn('id="go-page"', html)

    # -- 直した内容が残ること ------------------------------------------
    def test_the_fix_lands_in_the_db_and_on_paper(self):
        day, line, shift = self.save_pages([1, 2])
        self.post("/api/settings/page", {"page": 1})
        self.post("/api/settings/back", {
            "rows": {"1": {"LOT": "NAOSHI9", "CON": "100", "WEI": "1000"}},
            "header": {"worker": "山田"}, "checks": {}})

        header, details = self.repo().load(day, line, shift, 1)
        self.assertEqual(details[0].lot, "NAOSHI9")
        # ページ2は触っていない
        self.assertEqual(self.repo().load(day, line, shift, 2)[1][0].lot,
                         "MOTO-P2")

        paper = self.get(f"/report/nippou?report_date={day}&line={line}"
                         f"&shift={shift}&page=1").get_data(as_text=True)
        self.assertIn("NAOSHI9", paper)
        self.assertNotIn("MOTO-P1", paper)

    def test_back_returns_to_the_latest_page(self):
        self.save_pages([1, 2])
        self.post("/api/settings/page", {"page": 1})
        self.post("/api/settings/back", {})
        body = self.post("/api/entry/state", {}).get_json()
        self.assertEqual(body["page"], 2)
        self.assertFalse(body["recall"])

    # -- 言葉 ----------------------------------------------------------
    def test_same_shift_is_not_called_past_data(self):
        """同じ直のページを直しているのに「過去データ」とは言わない。"""
        self.save_pages([1, 2])
        self.post("/api/settings/page", {"page": 1})
        body = self.post("/api/entry/state", {}).get_json()
        self.assertTrue(body["recall"])
        self.assertFalse(body["recall_other"])

        html = self.get("/").get_data(as_text=True)
        self.assertIn("この直の第1ページを直しています", html)
        self.assertNotIn("他の直のデータを開いています", html)

    def test_another_shift_is_called_out(self):
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        day, line, shift = self.save_pages([1], day=PAST, shift="1直")
        self.post("/api/settings/recall", {
            "report_date": PAST, "line": line, "shift": "1直", "page": 1})
        body = self.post("/api/entry/state", {}).get_json()
        self.assertTrue(body["recall_other"])

    # -- 断るところ ----------------------------------------------------
    def test_new_page_is_refused_while_switched_back(self):
        # ページ1を直している最中に新しいページを作ると、どこに足したのか
        # 押した人には分からない
        self.save_pages([1, 2])
        self.post("/api/settings/page", {"page": 1})
        res = self.post("/api/entry/newpage", {"rows": {}, "header": {},
                                               "checks": {}})
        self.assertEqual(res.status_code, 422)

    def test_push_is_refused_while_switched_back(self):
        self.save_pages([1, 2])
        self.post("/api/settings/page", {"page": 1})
        res = self.post("/api/settings/push", {})
        self.assertEqual(res.status_code, 422)
        self.assertTrue(res.get_json()["can_return"])


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class RecallScopeTests(WebTestCase):
    """呼び出しの関門。**自分の直か、他人の記録か**で分ける。"""

    def now(self):
        from nippou import work_context

        from app.routes.entry import build_service, current_calculator

        ctx = work_context.get_context()
        with self.app.test_request_context():
            service = build_service(ctx, current_calculator())
            shift, day = service.current_shift_info(
                datetime.now(), ctx.force_day_shift())
        return day, ctx.line, shift

    def save(self, day, line, shift, pages):
        for page in pages:
            self.repo().save(
                HeaderRecord(report_date=day, line=line, shift=shift,
                             page=page, worker="山田"),
                [DetailRecord(report_date=day, line=line, shift=shift,
                              page=page, row_no=1, lot=f"MOTO-P{page}")])

    def test_own_shift_needs_no_admin(self):
        day, line, shift = self.now()
        self.save(day, line, shift, [1, 2])
        res = self.post("/api/settings/recall", {
            "report_date": day, "line": line, "shift": shift, "page": 1})
        self.assertEqual(res.status_code, 200)

    def test_another_day_needs_admin(self):
        _, line, shift = self.now()
        self.save(PAST, line, shift, [1])
        res = self.post("/api/settings/recall", {
            "report_date": PAST, "line": line, "shift": shift, "page": 1})
        self.assertEqual(res.status_code, 403)
        self.assertIn("管理者", res.get_json()["error"]["message"])

    def test_another_line_needs_admin(self):
        day, line, shift = self.now()
        other = "HVC" if line != "HVC" else "LVC"
        self.save(day, other, shift, [1])
        res = self.post("/api/settings/recall", {
            "report_date": day, "line": other, "shift": shift, "page": 1})
        self.assertEqual(res.status_code, 403)

    def test_admin_can_reach_another_day(self):
        _, line, shift = self.now()
        self.save(PAST, line, shift, [1])
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        res = self.post("/api/settings/recall", {
            "report_date": PAST, "line": line, "shift": shift, "page": 1})
        self.assertEqual(res.status_code, 200)

    # -- 終わった直の2ページ目 ----------------------------------------------
    def test_a_finished_shift_can_open_its_second_page(self):
        """**これが塞いだ穴。** 一覧はページ1しか開けなかった。"""
        _, line, shift = self.now()
        self.save(PAST, line, shift, [1, 2])
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        res = self.post("/api/settings/recall", {
            "report_date": PAST, "line": line, "shift": shift, "page": 2})
        self.assertEqual(res.status_code, 200)

        body = self.post("/api/entry/state", {}).get_json()
        self.assertEqual(body["page"], 2)
        html = self.get("/").get_data(as_text=True)
        self.assertIn('value="MOTO-P2"', html)

    def test_the_list_offers_a_page_picker(self):
        _, line, shift = self.now()
        self.save(PAST, line, shift, [1, 2])
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        html = self.get("/records").get_data(as_text=True)
        # 「1・2ページ」と出ているのにページ1しか開けない、を無くす
        self.assertIn("data-recall-page", html)
        self.assertIn("ページ2", html)

    def test_a_single_page_shift_needs_no_picker(self):
        _, line, shift = self.now()
        self.save(PAST, line, shift, [1])
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        html = self.get("/records").get_data(as_text=True)
        self.assertNotIn("data-recall-page", html)

    def test_the_current_shift_row_is_open_without_admin(self):
        """いまの直の行は**管理者モードでなくても押せる。**

        日報入力のページ移動と同じことなので、片方だけ縛ると食い違う。
        """
        day, line, shift = self.now()
        self.save(day, line, shift, [1, 2])
        html = self.get("/records").get_data(as_text=True)
        self.assertIn("ページを直す", html)

    def test_another_days_row_stays_locked(self):
        _, line, shift = self.now()
        self.save(PAST, line, shift, [1])
        html = self.get("/records").get_data(as_text=True)
        self.assertIn("他の日・他の直を開くには管理者モードが要ります", html)

    def test_admin_can_press_a_past_row_but_it_is_not_called_own(self):
        """管理者はどの行も押せる。**それでも昨日の直は「呼び出す」。**

        「ページを直す」は自分の直の言い方で、押せるかどうかとは別の話。
        """
        _, line, shift = self.now()
        self.save(PAST, line, shift, [1])
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        html = self.get("/records").get_data(as_text=True)
        self.assertIn("呼び出す", html)
        self.assertNotIn("他の日・他の直を開くには管理者モードが要ります", html)

    def test_the_date_form_can_recall(self):
        # 一覧に載らないほど古い直へ行く道
        html = self.get("/records").get_data(as_text=True)
        self.assertIn('id="recall-here"', html)

    def test_a_missing_page_is_404(self):
        _, line, shift = self.now()
        self.save(PAST, line, shift, [1])
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        res = self.post("/api/settings/recall", {
            "report_date": PAST, "line": line, "shift": shift, "page": 9})
        self.assertEqual(res.status_code, 404)

    def test_an_empty_key_is_400(self):
        res = self.post("/api/settings/recall", {"report_date": "", "line": "",
                                                 "shift": ""})
        self.assertEqual(res.status_code, 400)


if __name__ == "__main__":
    unittest.main()
