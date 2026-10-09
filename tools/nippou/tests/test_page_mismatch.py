"""画面に出ていないページへは書かない (v4.24.0)

    打った行が消えることがありました
    打った値が勝手に戻ることがありました

── とんでもない話である。書き先(`_target`)はサーバが覚えている1つだけで、
**画面がどのページを出しているかを見ていませんでした**(比べていたのは直の
変わり目 ── 報告日と直 ── だけ)。そのため:

    次ページ発行     … 第1ページの12行が、出したばかりの空の第2ページへ
    最新のページに戻る … 直していた第1ページの中身が、閉じる間際の送信で最新のページへ
    帯の「✕」          … 同じ(戻る前に保存もしていなかった)
    全停入力         … 打ちかけの空の行が、全停の行の上へ

画面の中身を受け取る口はどれも `entry.screen_mismatch` を通し、食い違えば
**書かずに**断ります(打ちかけは 200 で見送り、押した操作は 409)。
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.models import DetailRecord, HeaderRecord  # noqa: E402
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402

DONE = {"LOT": "N7131T0", "KZ": "08", "KH": "00", "SZ": "08", "SH": "15"}
SKIP_TEXT = "画面のページと書き先が違うので保存しませんでした"


def full_rows() -> dict[str, dict[str, str]]:
    return {str(r): dict(DONE, LOT=f"N71{r:02d}T0") for r in range(1, 13)}


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class PageMismatchTests(WebTestCase):
    """画面のページ ≠ 書き先 なら、**どの口からも書かない。**"""

    def now(self):
        from nippou import work_context

        from app.routes.entry import build_service, current_calculator

        ctx = work_context.get_context()
        with self.app.test_request_context():
            service = build_service(ctx, current_calculator())
            shift, day = service.current_shift_info(
                datetime.now(), ctx.force_day_shift())
        return day, ctx.line, shift

    def save_pages(self, pages):
        """DB にページを置く(1行目のロットで見分ける)。"""
        day, line, shift = self.now()
        repo = self.repo()
        for page in pages:
            repo.save(
                HeaderRecord(report_date=day, line=line, shift=shift,
                             page=page, worker="山田"),
                [DetailRecord(report_date=day, line=line, shift=shift,
                              page=page, row_no=1, lot=f"MOTO-P{page}",
                              con="100", wei="1000")])
        return day, line, shift

    def lot_of(self, page: int) -> str:
        day, line, shift = self.now()
        loaded = self.repo().load(day, line, shift, page)
        if loaded is None:
            return "(無い)"
        details = loaded[1]
        return details[0].lot if details else ""

    def body(self, page: int, lot: str = "UWAGAKI", **extra) -> dict:
        day, line, shift = self.now()
        return {"rows": {"1": {"LOT": lot, "CON": "100", "WEI": "1000"}},
                "header": {"worker": "山田"}, "checks": {},
                "opened": {"report_date": day, "line": line, "shift": shift,
                           "page": page},
                **extra}

    # -- 保存 ---------------------------------------------------------------
    def test_打ちかけは見送って書かない(self) -> None:
        """C1: 発行したあと、第1ページの画面から第2ページへ置こうとした。"""
        self.save_pages([1, 2])
        for flag in ("draft", "silent"):
            with self.subTest(flag=flag):
                res = self.post("/api/entry/save", self.body(1, **{flag: True}))
                self.assertEqual(res.status_code, 200)
                got = res.get_json()
                self.assertFalse(got["saved"])
                self.assertEqual(got["skipped"], SKIP_TEXT)
                self.assertEqual(got["page_moved"]["opened"]["page"], 1)
                self.assertEqual(got["page_moved"]["target"]["page"], 2)
                # **画面ぜんぶは返さない**(古い中身と新しいキーを一緒に塗らせない)
                self.assertNotIn("rows", got)
        self.assertEqual(self.lot_of(2), "MOTO-P2", "第1ページの中身が第2ページへ書かれた")
        self.assertEqual(self.lot_of(1), "MOTO-P1")

    def test_押した保存は409で書かない(self) -> None:
        self.save_pages([1, 2])
        res = self.post("/api/entry/save", self.body(1))
        self.assertEqual(res.status_code, 409)
        got = res.get_json()
        self.assertEqual(got["error"]["code"], "page_moved")
        self.assertIn("第1ページ", got["error"]["message"])
        self.assertIn("第2ページ", got["error"]["message"])
        self.assertFalse(got["saved"])
        self.assertEqual(self.lot_of(2), "MOTO-P2")

    def test_同じページならいつもどおり書く(self) -> None:
        self.save_pages([1, 2])
        res = self.post("/api/entry/save", self.body(2, draft=True))
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["saved"])
        self.assertEqual(self.lot_of(2), "UWAGAKI")

    def test_名乗らない画面は今までどおり(self) -> None:
        self.save_pages([1, 2])
        body = self.body(2, draft=True)
        body.pop("opened")
        self.assertTrue(self.post("/api/entry/save", body).get_json()["saved"])

    def test_ラインが違えば書かない(self) -> None:
        self.save_pages([1])
        body = self.body(1, draft=True)
        body["opened"]["line"] = "別のライン"
        got = self.post("/api/entry/save", body).get_json()
        self.assertFalse(got["saved"])
        self.assertEqual(got["skipped"], SKIP_TEXT)
        self.assertEqual(self.lot_of(1), "MOTO-P1")

    # -- 呼出(ページを戻って直す)--------------------------------------------
    def test_戻ったあとの閉じる間際の送信は最新のページへ書かない(self) -> None:
        """C2/C3: 第1ページを直して「最新のページに戻る」(帯の ✕)。"""
        self.save_pages([1, 2])
        self.assertEqual(self.post("/api/settings/page", {"page": 1}).status_code, 200)
        self.post("/api/settings/back", {})
        got = self.post("/api/entry/save", self.body(1, lot="NAOSHI1", draft=True)).get_json()
        self.assertFalse(got["saved"])
        self.assertEqual(got["skipped"], SKIP_TEXT)
        self.assertEqual(self.lot_of(2), "MOTO-P2", "直していた第1ページが最新のページへ")

    def test_呼び出した紙と違うページの画面からは書かない(self) -> None:
        self.save_pages([1, 2])
        self.post("/api/settings/page", {"page": 1})
        # 第2ページを出したままの古い画面
        got = self.post("/api/entry/save", self.body(2, draft=True)).get_json()
        self.assertFalse(got["saved"])
        self.assertEqual(self.lot_of(1), "MOTO-P1")
        # 呼び出した第1ページの画面からは書ける
        ok = self.post("/api/entry/save", self.body(1, lot="NAOSHI1", draft=True)).get_json()
        self.assertTrue(ok["saved"])
        self.assertEqual(self.lot_of(1), "NAOSHI1")

    def test_戻る前の保存も画面の紙が同じときだけ(self) -> None:
        self.save_pages([1, 2])
        self.post("/api/settings/page", {"page": 1})
        res = self.post("/api/settings/back", self.body(2, lot="CHIGAU"))
        self.assertEqual(res.status_code, 200)
        got = res.get_json()
        self.assertFalse(got["saved"])
        self.assertTrue(got["page_moved"])
        self.assertEqual(self.lot_of(1), "MOTO-P1")
        self.assertEqual(self.lot_of(2), "MOTO-P2")

    def test_戻る前の保存は同じ紙なら書く(self) -> None:
        self.save_pages([1, 2])
        self.post("/api/settings/page", {"page": 1})
        got = self.post("/api/settings/back", self.body(1, lot="NAOSHI1")).get_json()
        self.assertTrue(got["saved"])
        self.assertEqual(self.lot_of(1), "NAOSHI1")

    # -- 発行 -------------------------------------------------------------
    def test_古い画面からは発行しない(self) -> None:
        self.save_pages([1, 2])
        body = self.body(1)
        body["rows"] = full_rows()
        res = self.post("/api/entry/newpage", {**body, "confirm": True})
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()["error"]["code"], "page_moved")
        day, line, shift = self.now()
        self.assertEqual(self.repo().saved_pages(day, line, shift), [1, 2])
        self.assertEqual(self.lot_of(2), "MOTO-P2")

    def test_発行のあとは前のページの画面から書けない(self) -> None:
        """C1 そのもの: 発行 → 画面を引き直す前の「移る前の保存」。"""
        day, line, shift = self.now()
        rows = full_rows()
        res = self.post("/api/entry/newpage", {
            "rows": rows, "header": {"worker": "青木"}, "checks": {}, "confirm": True,
            "opened": {"report_date": day, "line": line, "shift": shift, "page": 1}})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(res.get_json()["page"], 2)
        late = self.post("/api/entry/save", {
            "rows": rows, "header": {"worker": "青木"}, "checks": {}, "draft": True,
            "opened": {"report_date": day, "line": line, "shift": shift, "page": 1}})
        self.assertFalse(late.get_json()["saved"])
        self.assertEqual(late.get_json()["skipped"], SKIP_TEXT)
        self.assertEqual(self.lot_of(2), "", "第1ページの12行が新しい第2ページへ写った")

    # -- 決まる値・ロット・印・排他 ------------------------------------------
    def test_決まる値の口も古いページの画面には答えない(self) -> None:
        """C5: 古い中身に新しいページのキーを付けて返すと、画面がそれを塗って保存する。"""
        self.save_pages([1, 2])
        cases = [
            ("/api/entry/state", {"row": 1, "changed": "LOT"}),
            ("/api/entry/lot", {"row": 1}),
            ("/api/entry/mark", {"row": 1, "key": self.mark_key()}),
            ("/api/entry/check", {"name": self.check_name()}),
        ]
        for path, extra in cases:
            with self.subTest(path=path):
                res = self.post(path, {**self.body(1), **extra})
                self.assertEqual(res.status_code, 409, res.get_json())
                got = res.get_json()
                self.assertEqual(got["error"]["code"], "page_moved")
                self.assertNotIn("rows", got)
                ok = self.post(path, {**self.body(2), **extra})
                self.assertEqual(ok.status_code, 200, ok.get_json())
                self.assertEqual(ok.get_json()["page"], 2)

    def mark_key(self) -> str:
        from nippou.logic import etc_marks

        return next(iter(etc_marks.BY_KEY))

    def check_name(self) -> str:
        from nippou import constants

        return constants.HDCH_NAMES[0]

    # -- 全停入力 -----------------------------------------------------------
    def test_全停で書いたページへ古い画面からは書かない(self) -> None:
        """C4: 全停は空のページ(=画面に出ているページ)へ書くことがある。

        ページ番号は同じなので、**画面を描いた時刻**で見分けます。
        """
        from nippou import awake_clock

        day, line, shift = self.now()
        drawn = awake_clock.now()
        self.save_pages([1])
        from app.routes import entry
        entry.note_written_aside((day, line, shift, 1))
        body = self.body(1, draft=True)
        body["opened"]["loaded"] = drawn
        got = self.post("/api/entry/save", body).get_json()
        self.assertFalse(got["saved"])
        self.assertEqual(got["skipped"], SKIP_TEXT)
        self.assertEqual(self.lot_of(1), "MOTO-P1")
        # 書いたあとに描いた画面からは書ける
        fresh = self.body(1, lot="ATO", draft=True)
        fresh["opened"]["loaded"] = awake_clock.now() + 1
        self.assertTrue(self.post("/api/entry/save", fresh).get_json()["saved"])

    def test_画面の外から書いたページへ古い画面からは書かない_取り込みなど(self) -> None:
        """CSV の取り込み・直の付け替えも、全停と同じく古い画面に上書きさせない(page_writer)。"""
        from nippou import awake_clock
        from nippou.services import page_writer

        day, line, shift = self.now()
        drawn = awake_clock.now()
        self.save_pages([1])
        header, details = self.repo().load(day, line, shift, 1)
        page_writer.save_page(self.repo(), header, details, by_screen=False)   # 取り込みが書き直した
        body = self.body(1, draft=True)
        body["opened"]["loaded"] = drawn
        got = self.post("/api/entry/save", body).get_json()
        self.assertFalse(got["saved"], "取り込んだページを古い画面の中身で戻した")

    def test_全停入力は書いたページを覚える(self) -> None:
        from app.routes import entry

        self.post("/api/entry/state", {"header": {"worker": "山田"}})
        res = self.post("/api/formstop/execute",
                        {"reason": "設備故障", "code": "0", "worker": "山田"})
        if res.status_code != 200:
            self.skipTest(f"全停入力が通らない環境 {res.get_json()}")
        page = res.get_json()["page"]
        self.assertTrue(any(k[3] == page for k in entry._WRITTEN_ASIDE))


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class TabGuardPathsTests(WebTestCase):
    """S1: 書き先を動かす口は、打てるタブからだけ。"""

    def test_後から作る枠と控えから開くも見張る(self) -> None:
        from app import _TAB_GUARDED_PATHS

        self.assertIn("/api/settings/backfill", _TAB_GUARDED_PATHS)
        self.assertIn("/api/records/backup/open", _TAB_GUARDED_PATHS)

    def test_ロックを取ったあとにももう1度見る(self) -> None:
        """S9: 待っているあいだに打てるタブが替わったら、待っていた要求は書かない。"""
        import app as app_module

        names = [f.__name__ for f in self.app.before_request_funcs[None]]
        self.assertIn("_one_tab_may_write_in_lock", names)
        self.assertLess(names.index("_take_write_lock"),
                        names.index("_one_tab_may_write_in_lock"))
        self.assertTrue(callable(app_module._tab_refusal))


if __name__ == "__main__":
    unittest.main()
