"""過去の直を後から作る(後日作成) (v4.0.0)

    例えば前日の1直分を丸々打ち忘れた場合に、前日分を作成するには?

    過去分を作ることはある一定で仕方がない場面はあると思っています。
    ・管理者であること
    ・現在分とぶつかって整合性がとれなくならない
    ・抜けた部分に保存される
    ・後日作ったことがわかる

前は「呼び出す」が保存のある直しか開けず、丸ごと抜けた直はどこからも
作れませんでした(しかも前の直が空の知らせは「呼び出せば保存できます」と
案内していた)。4つの条件をそのまま関門にして、作る道を1つ足します。
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import backfill as rule  # noqa: E402
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402

NOW = datetime(2026, 10, 1, 10, 0)
ENDED = NOW - timedelta(hours=3)
LATER = NOW + timedelta(hours=5)


def decide(**over):
    base = dict(admin=True, report_date="2026年9月30日", line="L-1", shift="1直",
                shift_end=ENDED, now=NOW, local_pages=[], shared_pages=[],
                writing=False)
    base.update(over)
    return rule.decide(**base)


class DecideTests(unittest.TestCase):
    """**4つの条件をそのまま関門に。** 判断はここ1か所。"""

    def test_管理者でなければ断る(self) -> None:
        d = decide(admin=False)
        self.assertEqual((d.ok, d.code), (False, rule.NOT_ADMIN))
        self.assertIn("管理者モード", d.message)

    def test_終わっていない直は作らない(self) -> None:
        """**現在分とぶつからない。** いまの直・これからの直は日報入力から打つ。"""
        d = decide(shift_end=LATER)
        self.assertEqual((d.ok, d.code), (False, rule.NOT_ENDED))
        self.assertIn("日報入力からふつうに打って", d.message)
        self.assertEqual(decide(shift_end=None).code, rule.BAD_KEY)

    def test_この端末がまだ打てる直は作らない(self) -> None:
        self.assertEqual(decide(writing=True).code, rule.WRITING)

    def test_空いている枠に作る(self) -> None:
        """**抜けた部分に保存。** 保存が無ければ1ページ目、あれば続き。"""
        first = decide()
        self.assertEqual((first.ok, first.page), (True, 1))
        self.assertIn("保存が1ページもありません", first.message)
        more = decide(local_pages=[1, 2], shared_pages=[1, 2])
        self.assertEqual(more.page, 3)
        self.assertIn("第1・2ページは保存済み", more.message)

    def test_共有に別のPCのページがあれば断る(self) -> None:
        """ここで作って送ると、共有のそのページを上書きするため。"""
        d = decide(local_pages=[], shared_pages=[1])
        self.assertEqual((d.ok, d.code), (False, rule.IN_SHARED))
        self.assertIn("上書き", d.message)
        # 手元にもあるページなら、別のPCのものではない
        self.assertTrue(decide(local_pages=[1], shared_pages=[1]).ok)

    def test_共有を確かめられなければ添えて通す(self) -> None:
        d = decide(shared_pages=None)
        self.assertTrue(d.ok)
        self.assertIn("確かめられませんでした", d.caution)
        self.assertEqual(decide().caution, "")

    def test_印の字(self) -> None:
        text = rule.stamp_text({"opened_at": "2026-10-01T09:12:34",
                                "terminal": "PC-01", "note": "打ち忘れ"})
        self.assertEqual(text, "後日作成(2026/10/01 09:12 PC-01) 理由: 打ち忘れ")
        self.assertEqual(rule.stamp_text({}), "後日作成")
        self.assertEqual(rule.clean_note("  打ち\n忘れ  " + "x" * 100)[:5], "打ち 忘れ")
        self.assertLessEqual(len(rule.clean_note("x" * 100)), rule.NOTE_MAX)

    def test_確認画面は別の日の直なら日を添える(self) -> None:
        """後から作った前日の1直を、今日の1直の最中に締めくくる ──
        「1直 は終わりましたが」だけでは、いまの1直のことに読める。"""
        from nippou.logic import shift_closing, shift_review

        other = shift_closing.build(report_date="2026年9月30日", shift="1直",
                                    pages=1, unsynced=1,
                                    shown_date="2026年10月1日")
        self.assertTrue(other.headline.startswith("2026年9月30日 1直 の1ページ"))
        same = shift_closing.build(report_date="2026年10月1日", shift="1直",
                                   pages=1, unsynced=1,
                                   shown_date="2026年10月1日")
        self.assertTrue(same.headline.startswith("1直 の1ページ"))
        ended = shift_review.evaluate_ended("2026年9月30日", "1直", unsynced=True)
        self.assertIn("2026年9月30日 1直 は終わりましたが", ended.reason)


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class BackfillWebTests(WebTestCase):
    """記録を見る → 日付を指定して見る → この直を後から作る。"""

    def setUp(self) -> None:
        super().setUp()
        self.post("/api/entry/line", {"line": "L-1"})
        self.yesterday = self.day(-1)
        self.key = {"report_date": self.yesterday, "line": "L-1", "shift": "1直"}

    def repo(self):
        from nippou.config import SETTINGS
        from nippou.db.connection import connect
        from nippou.db.repository import NippouRepository
        return NippouRepository(connect(SETTINGS.sqlite_path))

    def day(self, offset: int) -> str:
        from app.routes.entry import build_shift_calculator
        from nippou.services.nippou_service import format_business_date
        calc = build_shift_calculator(self.repo().get_shift_times())
        return format_business_date(calc.today_check(datetime.now(), False)
                                    + timedelta(days=offset))

    def admin(self) -> None:
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})

    def make(self, note: str = "打ち忘れ") -> dict:
        res = self.post("/api/settings/backfill", {**self.key, "note": note})
        self.assertEqual(res.status_code, 200, res.get_json())
        return res.get_json()

    def save_row(self, lot: str = "BACK001") -> None:
        res = self.post("/api/entry/save", {
            "rows": {"1": {"LOT": lot, "CON": "100", "WEI": "1000"}},
            "header": {"worker": "山田"}, "checks": {}})
        self.assertEqual(res.status_code, 200, res.get_json())

    # -- 関門 ------------------------------------------------------------
    def test_管理者でなければ403(self) -> None:
        res = self.post("/api/settings/backfill", {**self.key, "dry_run": True})
        self.assertEqual((res.status_code, res.get_json()["error"]["code"]),
                         (403, rule.NOT_ADMIN))

    def test_明日の直は作れない(self) -> None:
        self.admin()
        res = self.post("/api/settings/backfill",
                        {**self.key, "report_date": self.day(+1), "dry_run": True})
        self.assertEqual((res.status_code, res.get_json()["error"]["code"]),
                         (409, rule.NOT_ENDED))

    def test_確かめるだけなら何も変わらない(self) -> None:
        self.admin()
        res = self.post("/api/settings/backfill", {**self.key, "dry_run": True})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["decision"]["page"], 1)
        self.assertEqual(self.repo().backfills(), {})
        self.assertFalse(self.post("/api/entry/state", {}).get_json().get("backfill"))

    def test_共有に別のPCのページがある直は作れない(self) -> None:
        """**整合性。** 作って送ると共有のページ1を上書きする。"""
        from nippou import config, user_settings
        from nippou.access_bridge import pusher, sqlite_backend
        from nippou.config import SETTINGS
        from nippou.db.models import DetailRecord, HeaderRecord

        shared = self.tmp / "shared"
        shared.mkdir()
        user_settings.save_many({config.KEY_ACCESS_DIR: str(shared)})
        page = dict(self.key, page=1)
        result = sqlite_backend.push_records(
            SETTINGS.access_db_path, HeaderRecord(**page, worker="別の人"),
            [DetailRecord(**page, row_no=1, lot="OTHER01")],
            pusher.header_table_name("L-1"), pusher.detail_table_name("L-1"))
        self.assertTrue(result.success)
        self.admin()
        res = self.post("/api/settings/backfill", {**self.key, "dry_run": True})
        self.assertEqual((res.status_code, res.get_json()["error"]["code"]),
                         (409, rule.IN_SHARED))
        # 共有のフォルダはあるがファイルがまだ無いなら、確かめた(空)として通す
        user_settings.save_many({config.KEY_ACCESS_DIR: str(self.tmp / "empty")})
        (self.tmp / "empty").mkdir()
        res = self.post("/api/settings/backfill", {**self.key, "dry_run": True})
        self.assertEqual(res.get_json()["decision"]["caution"], "")

    # -- 作る・保存する ------------------------------------------------
    def test_作って打って保存すると抜けた直に入り_印が残る(self) -> None:
        self.admin()
        body = self.make()
        self.assertEqual(body["decision"]["page"], 1)
        state = self.post("/api/entry/state", {}).get_json()
        self.assertEqual((state["report_date"], state["shift"], state["page"]),
                         (self.yesterday, "1直", 1))
        self.assertFalse(state["read_only"])
        self.assertIn("後日作成", state["backfill"]["stamp"])
        self.save_row()

        # 抜けた部分(昨日の1直 ページ1)に入る
        header, details = self.repo().load(self.yesterday, "L-1", "1直", 1)
        self.assertEqual((header.worker, details[0].lot), ("山田", "BACK001"))
        made = self.repo().backfills()
        self.assertEqual(list(made), [(self.yesterday, "L-1", "1直", 1)])
        self.assertEqual(made[(self.yesterday, "L-1", "1直", 1)]["note"], "打ち忘れ")

        # いまの直へ戻る(画面と同じく、いまの値を添えて)。**いまの直の書き先は動いていない**
        self.post("/api/settings/back", {
            "rows": {"1": {"LOT": "BACK001", "CON": "100", "WEI": "1000"}},
            "header": {"worker": "山田"}, "checks": {}})
        now_state = self.post("/api/entry/state", {}).get_json()
        self.assertNotEqual((now_state["report_date"], now_state["shift"]),
                            (self.yesterday, "1直"))
        self.assertIsNone(now_state["backfill"])

        # 後日作ったことが分かる ── 記録の一覧・紙
        html = self.get("/records").get_data(as_text=True)
        self.assertIn("後日作成", html)
        self.assertIn("理由: 打ち忘れ", html)
        paper = self.get(f"/report/nippou?report_date={self.yesterday}&line=L-1"
                         "&shift=1直&page=all").get_data(as_text=True)
        self.assertIn('class="backfill-stamp"', paper)
        self.assertIn("後日作成", paper)

        # 続きは次のページ
        again = self.post("/api/settings/backfill", {**self.key, "dry_run": True})
        self.assertEqual(again.get_json()["decision"]["page"], 2)

    def test_作りかけて打たずに戻れば何も残らない(self) -> None:
        """画面の「最新のページに戻る」は空の12行を送ってくる ── 空のページを作らない。"""
        self.admin()
        self.make()
        empty = {"rows": {str(n): {"LOT": "", "CON": "", "WEI": ""} for n in range(1, 13)},
                 "header": {"worker": ""}, "checks": {}}
        self.post("/api/settings/back", empty)
        self.assertEqual(self.repo().saved_pages(self.yesterday, "L-1", "1直"), [])
        self.assertEqual(self.repo().backfills(), {})            # 印も出ない
        self.assertNotIn("後日作成</span>", self.get("/records").get_data(as_text=True))

    def test_共有保存の履歴にも出る(self) -> None:
        from nippou.services import push_history

        self.admin()
        self.make()
        self.save_row()
        self.post("/api/settings/back", {})
        repo = self.repo()

        class Summary:
            succeeded = [(self.yesterday, "L-1", "1直", 1)]
            failed: list = []

        rows = push_history.record_push(
            repo, push_history.Pressed(at="2026-10-01T10:00:00"), Summary(),
            (self.yesterday, "L-1", "1直"))
        self.assertIn("後日作成", rows[0]["detail"])
        self.assertIn("ページ1は", rows[0]["detail"])

    # -- 案内 ------------------------------------------------------------
    def test_前の直が空の知らせは作る道を指す(self) -> None:
        """前は「呼び出せば保存できます」── 丸ごと抜けた直では行き止まりだった。"""
        found = rule  # noqa: F841 - 読みやすさのため
        from nippou.logic import prev_shift
        warn = prev_shift.evaluate(
            report_date="2026年10月1日", line="L-1", shift="1直",
            prev_report_date="2026年9月30日", prev_shift="3直",
            prev_has_data=False, seen_recently=True)
        self.assertIn("この直を後から作る", warn.reason)
        self.assertNotIn("呼び出せば", warn.reason)

    def test_画面に作る口がある(self) -> None:
        html = self.get("/records").get_data(as_text=True)
        self.assertIn('id="backfill-here"', html)
        self.assertIn('id="backfill-reason"', html)
        self.assertIn("管理者モードにすると使えます", html)       # 管理者でなければ押せない
        self.admin()
        html = self.get("/records").get_data(as_text=True)
        button = html[html.index('id="backfill-here"'):][:200]
        self.assertNotIn("disabled", button)
        root = Path(__file__).resolve().parent.parent
        js = (root / "app/static/js/views/records.js").read_text(encoding="utf-8")
        self.assertIn('"/api/settings/backfill", { ...target, dry_run: true }', js)


if __name__ == "__main__":
    unittest.main()
