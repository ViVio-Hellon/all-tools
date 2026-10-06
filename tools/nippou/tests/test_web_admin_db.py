"""管理者モードまわりと、都度DB保存 (VBA 2026.1.14 更新分)

【この回の仕様変更】
    NippouDB_AutoSave        呼出モード中は他日・他直へ自動保存しない
    NippouDB_BackToCurrent   解除する前に保存する
    NippouDB_EditPage        ページ移動は管理者専用
    NippouDB_GetPageCount    ページ数は MAX(ページ)(数え上げではない)
    NippouDB_FillDBList      保存日時の新しい順
    NippouDB_BlockIfRecallMode 呼出モード中は危ない操作を断る
    NippouDB_RestoreShift    当直の全ページをDBから読み直す

**判断はどれもサーバ側**。画面は理由を出すだけなので、ここで固定する。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import WebTestCase


class AutosaveTests(WebTestCase):
    """`silent=true` は自動保存。**書いてよいかはサーバが決める。**"""

    BODY = {"rows": {"1": {"LOT": "A", "WEI": "10"}}, "header": {}, "checks": {},
            "silent": True}

    def test_1回目は書く(self) -> None:
        body = self.post("/api/entry/save", dict(self.BODY)).get_json()
        self.assertTrue(body["saved"])
        self.assertTrue(body["saved_at"])

    def test_続けて送っても間引く(self) -> None:
        self.post("/api/entry/save", dict(self.BODY))
        body = self.post("/api/entry/save", dict(self.BODY)).get_json()
        self.assertFalse(body["saved"])
        # **理由を返す。** 画面が「保存されていない」と誤解しないため
        self.assertIn("秒しか経っていません", body["skipped"])

    def test_間引きは断りではない(self) -> None:
        """200 で返す ── まだその時ではないだけで、失敗ではない。"""
        self.post("/api/entry/save", dict(self.BODY))
        res = self.post("/api/entry/save", dict(self.BODY))
        self.assertEqual(res.status_code, 200)

    def test_確定保存は間引かない(self) -> None:
        self.post("/api/entry/save", dict(self.BODY))
        body = self.post("/api/entry/save", {
            "rows": {"1": {"LOT": "A"}}, "header": {}, "checks": {}}).get_json()
        self.assertTrue(body["saved"])
        self.assertEqual(body["message"], "保存しました")

    def test_他直を開いている最中は自動保存しない(self) -> None:
        """**ここが今回の要点。**

        管理者が先週の2直を見ているだけのつもりでも、画面の値が流れ込んで
        過去のデータを書き換えてしまう。そちらへ書くのは確定保存だけ。
        """
        from nippou import work_context

        ctx = work_context.get_context()
        ctx.admin = True
        ctx.recall = work_context.RecallState(True, "2020年1月1日", ctx.line, "3直", 1)

        body = self.post("/api/entry/save", dict(self.BODY)).get_json()
        self.assertFalse(body["saved"])
        self.assertIn("他の直", body["skipped"])

    def test_同じ直のページ移動中なら自動保存する(self) -> None:
        """ページだけ動かしているのは「いまの作業」。止める理由が無い。"""
        from datetime import datetime

        from nippou import work_context
        from app.routes.entry import build_service, current_calculator

        ctx = work_context.get_context()
        ctx.admin = True
        with self.app.test_request_context():
            calc = current_calculator()
            service = build_service(ctx, calc)
            shift, day = service.current_shift_info(
                datetime.now(), ctx.force_day_shift())
        ctx.recall = work_context.RecallState(True, day, ctx.line, shift, 1)

        body = self.post("/api/entry/save", dict(self.BODY)).get_json()
        self.assertTrue(body["saved"], body.get("skipped"))


class PageMoveTests(WebTestCase):
    """ページ移動 (`NippouDB_EditPage` / `NippouDB_InitPageSpinner`)。"""

    def _save_pages(self, pages) -> None:
        """指定のページを作る。**確定保存を使う**(自動保存は間引かれる)。"""
        from datetime import datetime

        from nippou import work_context
        from app.routes.entry import build_service, current_calculator

        ctx = work_context.get_context()
        with self.app.test_request_context():
            service = build_service(ctx, current_calculator())
            shift, day = service.current_shift_info(
                datetime.now(), ctx.force_day_shift())
        repo = self.repo()
        from nippou.db.models import DetailRecord, HeaderRecord
        for page in pages:
            repo.save(HeaderRecord(report_date=day, line=ctx.line, shift=shift,
                                   page=page, worker=f"ページ{page}"),
                      [DetailRecord(report_date=day, line=ctx.line, shift=shift,
                                    page=page, row_no=1, lot=f"L{page}")])
        return day, ctx.line, shift

    def test_保存が無ければ押せない(self) -> None:
        body = self.get("/api/settings/page").get_json()
        self.assertFalse(body["enabled"])
        self.assertIn("保存データなし", body["status"])

    def test_管理者でなくても押せる(self) -> None:
        """**この直の中でページを戻るのは誰でも。**

        自分がさっき打った紙を自分で直しているだけで、誰の記録かは
        変わらない。管理者モードが要るのは他の日・他の直・他のライン
        (`recall_refusal`)。
        """
        self._save_pages([1, 2])
        body = self.get("/api/settings/page").get_json()
        self.assertTrue(body["enabled"])
        self.assertEqual(body["max"], 2)
        self.assertNotIn("管理者", body["note"])

    def test_1ページだけならそう言う(self) -> None:
        # 空の選択欄を黙って置かない。移れる先が無い理由を書く
        self._save_pages([1])
        body = self.get("/api/settings/page").get_json()
        self.assertIn("1ページ", body["note"])

    def test_ページ数は最大のページ番号(self) -> None:
        """**数え上げではない。**

        ページ1とページ3だけが保存されているとき、数え上げなら2になり、
        ページ移動の上限が2になってページ3を開けなくなる。
        """
        self._save_pages([1, 3])
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        body = self.get("/api/settings/page").get_json()
        self.assertEqual(body["max"], 3)
        # 歯抜けも分かる形で返す(押せるページだけを画面に出すため)
        self.assertEqual(body["pages"], [1, 3])

    def test_管理者でなくてもページを開ける(self) -> None:
        """2ページ目を出したあと1ページ目に戻れる ── **人を呼ばずに。**"""
        day, line, shift = self._save_pages([1, 2])
        res = self.post("/api/settings/page", {"page": 1})
        self.assertEqual(res.status_code, 200)

        from nippou import work_context
        ctx = work_context.get_context()
        self.assertTrue(ctx.recall.active)
        self.assertEqual(ctx.recall.page, 1)
        # **動いたのはページだけ。** 日付も直もラインもそのまま
        self.assertEqual(ctx.recall.report_date, day)
        self.assertEqual(ctx.recall.shift, shift)
        self.assertEqual(ctx.recall.line, line)

    def test_管理者ならページを開ける(self) -> None:
        self._save_pages([1, 2])
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        res = self.post("/api/settings/page", {"page": 1})
        self.assertEqual(res.status_code, 200)

        from nippou import work_context
        ctx = work_context.get_context()
        self.assertTrue(ctx.recall.active)
        self.assertEqual(ctx.recall.page, 1)

    def test_無いページは404(self) -> None:
        self._save_pages([1])
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        self.assertEqual(self.post("/api/settings/page", {"page": 9}).status_code, 404)

    def test_ページの指定がおかしければ400(self) -> None:
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        self.assertEqual(self.post("/api/settings/page", {"page": 0}).status_code, 400)


class BackToCurrentTests(WebTestCase):
    """`NippouDB_BackToCurrent` ── **解除する前に保存する。**"""

    def _recall_page1(self):
        from datetime import datetime

        from nippou import work_context
        from nippou.db.models import DetailRecord, HeaderRecord
        from app.routes.entry import build_service, current_calculator

        ctx = work_context.get_context()
        ctx.admin = True
        with self.app.test_request_context():
            service = build_service(ctx, current_calculator())
            shift, day = service.current_shift_info(
                datetime.now(), ctx.force_day_shift())
        repo = self.repo()
        for page in (1, 2):
            repo.save(HeaderRecord(report_date=day, line=ctx.line, shift=shift,
                                   page=page, worker=f"元{page}"),
                      [DetailRecord(report_date=day, line=ctx.line, shift=shift,
                                    page=page, row_no=1, lot=f"L{page}")])
        self.post("/api/settings/page", {"page": 1})
        return day, ctx.line, shift

    def test_直した値を保存してから戻る(self) -> None:
        day, line, shift = self._recall_page1()
        res = self.post("/api/settings/back", {
            "rows": {"1": {"LOT": "L-1", "WEI": "99"}},
            "header": {"worker": "直した人"}, "checks": {}})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["saved"])

        loaded = self.repo().load(day, line, shift, 1)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded[0].worker, "直した人")

    def test_戻ると呼出モードが解ける(self) -> None:
        from nippou import work_context

        self._recall_page1()
        self.post("/api/settings/back", {})
        self.assertFalse(work_context.get_context().recall.active)

    def test_値を送らなければ保存しない(self) -> None:
        day, line, shift = self._recall_page1()
        body = self.post("/api/settings/back", {}).get_json()
        self.assertFalse(body["saved"])
        # DBにあるものはそのまま
        self.assertEqual(self.repo().load(day, line, shift, 1)[0].worker, "元1")


class BlockRecallTests(WebTestCase):
    """`NippouDB_BlockIfRecallMode` ── 呼出中は危ない操作を断る。"""

    def _recall(self) -> None:
        from nippou import work_context

        ctx = work_context.get_context()
        ctx.admin = True
        ctx.recall = work_context.RecallState(True, "2020年1月1日", ctx.line, "3直", 1)

    def test_呼出中のAccess反映は断る(self) -> None:
        """直している過去のデータが、そのまま共有のAccessへ行ってしまう。"""
        self._recall()
        res = self.post("/api/settings/push", {})
        self.assertEqual(res.status_code, 422)
        body = res.get_json()
        self.assertEqual(body["error"]["code"], "recall_mode")
        # **逃げ道を添える。** 画面は「いま作業に戻りますか」を出せる
        self.assertTrue(body["can_return"])
        self.assertIn("2020年1月1日", body["recall"])

    def test_呼出していなければ通る(self) -> None:
        # この環境ではAccessに繋がらないので中身は失敗するが、
        # **関門は通っている**(422 recall_mode ではない)
        res = self.post("/api/settings/push", {})
        self.assertNotEqual(res.status_code, 422)

    def test_記録画面は止めないが呼出中だと言う(self) -> None:
        """過去データをわざと刷る使い方があるので止めない。

        ただし初期値が呼出中のキーになるので、それを画面に書く。
        """
        self._recall()
        body = self.get("/records").get_data(as_text=True)
        self.assertIn("呼出中", body)
        self.assertIn("2020年1月1日", body)


class DbKeyListTests(WebTestCase):
    """`NippouDB_FillDBList` ── 保存日時の新しい順。"""

    def _make(self, pages) -> None:
        from nippou.db.models import DetailRecord, HeaderRecord

        repo = self.repo()
        for i, (day, shift, page) in enumerate(pages):
            repo.save(HeaderRecord(report_date=day, line="L-1", shift=shift,
                                   page=page, worker=f"w{i}"),
                      [DetailRecord(report_date=day, line="L-1", shift=shift,
                                    page=page, row_no=1, lot=f"L{i}")])

    def test_保存日時つきで返る(self) -> None:
        self._make([("2026年1月5日", "1直", 1)])
        keys = self.get("/api/settings/db-keys").get_json()["keys"]
        self.assertEqual(len(keys), 1)
        self.assertTrue(keys[0]["saved_at"])
        self.assertIn("synced", keys[0])

    def test_保存日時の新しい順(self) -> None:
        """報告日順だと、直したばかりの過去データが下に埋もれる。"""
        self._make([("2026年1月5日", "1直", 1)])
        self._make([("2020年1月1日", "2直", 1)])   # 古い日付を後から保存
        keys = self.get("/api/settings/db-keys").get_json()["keys"]
        self.assertEqual(keys[0]["report_date"], "2020年1月1日")

    def test_既定はこのラインだけ(self) -> None:
        from nippou.db.models import HeaderRecord

        self._make([("2026年1月5日", "1直", 1)])
        self.repo().save(HeaderRecord(report_date="2026年1月5日", line="LVC",
                                      shift="1直", page=1), [])
        keys = self.get("/api/settings/db-keys").get_json()["keys"]
        self.assertEqual({k["line"] for k in keys}, {"L-1"})

    def test_全ラインも出せる(self) -> None:
        from nippou.db.models import HeaderRecord

        self._make([("2026年1月5日", "1直", 1)])
        self.repo().save(HeaderRecord(report_date="2026年1月5日", line="LVC",
                                      shift="1直", page=1), [])
        keys = self.get("/api/settings/db-keys?all=1").get_json()["keys"]
        self.assertEqual({k["line"] for k in keys}, {"L-1", "LVC"})


class RestoreShiftTests(WebTestCase):
    """`NippouDB_RestoreShift` ── 当直の全ページをDBから読み直す。"""

    def _save_shift(self, pages):
        from datetime import datetime

        from nippou import work_context
        from nippou.db.models import DetailRecord, HeaderRecord
        from app.routes.entry import build_service, current_calculator

        ctx = work_context.get_context()
        with self.app.test_request_context():
            service = build_service(ctx, current_calculator())
            shift, day = service.current_shift_info(
                datetime.now(), ctx.force_day_shift())
        repo = self.repo()
        for page in pages:
            repo.save(HeaderRecord(report_date=day, line=ctx.line, shift=shift,
                                   page=page, worker=f"ページ{page}"),
                      [DetailRecord(report_date=day, line=ctx.line, shift=shift,
                                    page=page, row_no=1, lot=f"L{page}")])
        return day, ctx.line, shift

    def test_管理者でなければ断る(self) -> None:
        self._save_shift([1])
        self.assertEqual(self.post("/api/settings/restore-shift", {}).status_code, 403)

    def test_全ページぶんの印刷用HTMLを作り直す(self) -> None:
        self._save_shift([1, 2, 3])
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        body = self.post("/api/settings/restore-shift", {}).get_json()
        self.assertEqual(body["pages"], 3)
        self.assertEqual(len(body["files"]), 3)
        for path in body["files"]:
            self.assertTrue(Path(path).is_file(), path)

    def test_歯抜けでも読めたぶんは復旧する(self) -> None:
        # ページ1と3だけ。**そこで止めない**
        self._save_shift([1, 3])
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        body = self.post("/api/settings/restore-shift", {}).get_json()
        self.assertEqual(body["pages"], 2)

    def test_データが無ければ404(self) -> None:
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        self.assertEqual(self.post("/api/settings/restore-shift", {}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
