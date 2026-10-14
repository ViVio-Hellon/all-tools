"""共有へ保存の範囲 ── **いまの直** と **過去分** (v4.35.0)

    現在の直と前直は処理の違うボタンを設けたほうがいいと思うのですが...
    ・普段の共有へ送る は現在の直用
    ・エラー時に出ている 共有へ送る は 前直処理用

    前の直を送る は溜まったエラー過去直全部送れると思っていいですよね?
    ボタンには 前の直を送る ではなく 過去分を共有へ送る とかわかるようにしてください

ここで押さえるのは:

    1. どの直を送るか(`logic/push_scope`)── いまの直だけ / いまの直ではない直ぜんぶ(古い順)
    2. 送るのは範囲の直のページだけ。**範囲の外の直の集計も共有へ出さない**(`pusher`)
    3. 確かめるのも範囲の直だけ(`shift_check.run_pending(keys=)`)
    4. 画面: 入力画面と設定画面に2つのボタン、押す前の下見も範囲ごと

通しの流れ(1直は過去分のボタン・2直はふだんのボタン、エラー混じりの過去分)は
`tests/test_scenario_shift_flow.py` の C2・C3・D2 にあります。
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import push_scope  # noqa: E402
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402

NOW = ("2026年10月5日", "L-1", "2直")


class KeysTests(unittest.TestCase):
    """どの直を送るか。**純ロジック。**"""

    PENDING = [("2026年10月5日", "L-1", "2直"), ("2026年10月5日", "L-1", "1直"),
               ("2026年9月30日", "L-1", "3直"), ("2026年10月4日", "L-1", "日勤"),
               ("2026年10月5日", "L-1", "1直")]

    def test_いまの直のボタンは_いまの直だけ(self) -> None:
        self.assertEqual(push_scope.keys_in(push_scope.SCOPE_CURRENT, self.PENDING, NOW), [NOW])

    def test_いまの直が未送信でなければ_送るものは無い(self) -> None:
        self.assertEqual(push_scope.keys_in(push_scope.SCOPE_CURRENT, self.PENDING[1:], NOW), [])

    def test_過去分のボタンは_いまの直ではない直ぜんぶを古い順に(self) -> None:
        """何日前でも1回で。同じ直は1つに。"""
        self.assertEqual(push_scope.keys_in(push_scope.SCOPE_PREVIOUS, self.PENDING, NOW),
                         [("2026年9月30日", "L-1", "3直"), ("2026年10月4日", "L-1", "日勤"),
                          ("2026年10月5日", "L-1", "1直")])

    def test_同じ日は集計と同じ直の並び(self) -> None:
        from nippou.logic.aggregation import SHIFT_NAMES

        day = "2026年10月5日"
        keys = [(day, "L-1", s) for s in ("3直", "2直", "日勤", "1直")]
        found = [k[2] for k in push_scope.oldest_first(keys)]
        self.assertEqual(found, sorted(found, key=list(SHIFT_NAMES).index))
        self.assertEqual(found[:3], ["1直", "2直", "3直"])

    def test_まとめて送るときと_いまの直が分からないときは_ぜんぶ(self) -> None:
        for scope, current in ((push_scope.SCOPE_ALL, NOW), (push_scope.SCOPE_PREVIOUS, None),
                               (push_scope.SCOPE_CURRENT, None)):
            self.assertEqual(len(push_scope.keys_in(scope, self.PENDING, current)), 4, scope)

    def test_範囲の字(self) -> None:
        self.assertEqual(push_scope.normalize("current"), push_scope.SCOPE_CURRENT)
        self.assertEqual(push_scope.normalize(" Previous "), push_scope.SCOPE_PREVIOUS)
        self.assertEqual(push_scope.normalize(None), push_scope.SCOPE_ALL)
        self.assertEqual(push_scope.normalize("everything"), push_scope.SCOPE_ALL)

    def test_止めた直だけ残すのは過去分のボタンだけ(self) -> None:
        self.assertTrue(push_scope.holds_one_by_one(push_scope.SCOPE_PREVIOUS))
        self.assertFalse(push_scope.holds_one_by_one(push_scope.SCOPE_CURRENT))
        self.assertFalse(push_scope.holds_one_by_one(push_scope.SCOPE_ALL))


class WordingTests(unittest.TestCase):
    """ボタンの名前・履歴の「通し方」・知らせの一行。"""

    def test_ボタンの名前で見分けられる(self) -> None:
        self.assertEqual(push_scope.BUTTON_CURRENT, "共有へ保存")
        self.assertEqual(push_scope.BUTTON_PREVIOUS, "過去分を共有へ送る")

    def test_履歴の通し方にどちらのボタンかが入る(self) -> None:
        self.assertEqual(push_scope.via_text(push_scope.SCOPE_CURRENT), "いまの直")
        self.assertEqual(push_scope.via_text(push_scope.SCOPE_PREVIOUS), "過去分")
        self.assertEqual(push_scope.via_text(push_scope.SCOPE_CURRENT, skipped=True,
                                             skip_phrase="設備移動のため保存"),
                         "いまの直・設備移動のため保存")
        # まとめて送るときは今までどおり(一文で通したときだけ一文)
        self.assertEqual(push_scope.via_text(push_scope.SCOPE_ALL), "")
        self.assertEqual(push_scope.via_text(push_scope.SCOPE_ALL, skipped=True,
                                             skip_phrase="設備移動のため保存"), "設備移動のため保存")

    def test_送った直とエラーで残した直を数で言う(self) -> None:
        self.assertEqual(push_scope.tally_text(["a", "b", "c"], ["d", "e"]),
                         "過去分: 送った直 3件 / エラーで残した直 2件")

    def test_いまの直の断りに添える一文は過去分のボタンを名指しする(self) -> None:
        self.assertIn("「過去分を共有へ送る」", push_scope.PREVIOUS_HINT)


class _RepoCase(unittest.TestCase):
    """一時の手元DBと、共有の日報データ(sqlite3)。"""

    def setUp(self) -> None:
        from nippou.db.connection import connect
        from nippou.db.repository import NippouRepository
        from nippou.db.schema import ensure_schema

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        conn = connect(self.tmp / "local.sqlite3")
        ensure_schema(conn)
        self.addCleanup(conn.close)
        self.repo = NippouRepository(conn)
        self.shared = self.tmp / "日報データ.sqlite3"

    def save(self, shift: str) -> tuple:
        from nippou.db.models import DetailRecord, HeaderRecord
        from nippou.services import summary

        key = dict(report_date="2026年10月5日", line="L-1", shift=shift, page=1)
        self.repo.save(HeaderRecord(**key, worker="山田"),
                       [DetailRecord(**key, row_no=1, lot=f"{shift}01", ken="10", mai="10",
                                     tut="1", kz="07", kh="00", sz="08", sh="00", tim="60",
                                     con="10")])
        summary.refresh_shift(self.repo, "2026年10月5日", "L-1", shift)
        return ("2026年10月5日", "L-1", shift)

    def shared_shifts(self, table: str) -> set:
        conn = sqlite3.connect(self.shared)
        try:
            return {r[0] for r in conn.execute(f'SELECT "直" FROM "{table}"')}
        finally:
            conn.close()


class PusherKeysTests(_RepoCase):
    """`pusher.push_pending(keys=)` ── **範囲の直だけ送る。集計も。**"""

    def test_範囲の直だけ送り_ほかは未送信のまま(self) -> None:
        from nippou.access_bridge import pusher

        first, second = self.save("1直"), self.save("2直")
        result = pusher.push_pending(self.repo, self.shared, keys=[first])
        self.assertEqual([k[:3] for k in result.succeeded], [first])
        self.assertEqual({(h.report_date, h.line, h.shift) for h in self.repo.pending_sync_headers()},
                         {second})
        self.assertEqual(self.shared_shifts("T_日報ヘッダー_L-1"), {"1直"})
        # **送らなかった2直の集計も出さない**(日報は手元なのに集計だけ共有へ、にしない)
        self.assertEqual(self.shared_shifts("T_日報集計_L-1"), {"1直"})
        self.assertEqual([r.shift for r in self.repo.pending_packing_reports()], ["2直"])

    def test_範囲を言わなければ_今までどおりぜんぶ(self) -> None:
        from nippou.access_bridge import pusher

        self.save("1直"), self.save("2直")
        result = pusher.push_pending(self.repo, self.shared)
        self.assertEqual(len(result.succeeded), 2)
        self.assertEqual(self.shared_shifts("T_日報集計_L-1"), {"1直", "2直"})


class RunPendingKeysTests(_RepoCase):
    """`shift_check.run_pending(keys=)` ── **確かめるのも範囲の直だけ。**"""

    def test_渡した直だけ確かめる(self) -> None:
        from nippou.services import shift_check

        first, _second = self.save("1直"), self.save("2直")
        reports = shift_check.run_pending(self.repo, keys=[first])
        self.assertEqual([(r.report_date, r.line, r.shift) for r in reports], [first])
        self.assertEqual(len(shift_check.run_pending(self.repo)), 2)


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class ScreenTests(WebTestCase):
    """画面 ── **ボタンは2つ、名前で見分けられる。** 下見も範囲ごと。"""

    def leave_old(self) -> None:
        """ずっと前の直(いまの直ではない)を1ページ、未送信のまま置く。"""
        from nippou.db.models import DetailRecord, HeaderRecord

        key = dict(report_date="2026年1月5日", line="L-1", shift="1直", page=1)
        self.repo().save(HeaderRecord(**key, worker="山田"),
                         [DetailRecord(**key, row_no=1, lot="OLD01")])

    def test_設定画面にもいまの直と過去分の2つ(self) -> None:
        self.leave_old()
        html = self.get("/settings").get_data(as_text=True)
        self.assertRegex(html, r'id="push"[^>]*data-scope="current"[^>]*>共有へ保存\(いまの直\)</button>')
        self.assertRegex(html, r'id="push-prev"[^>]*data-scope="previous">過去分を共有へ送る</button>')
        self.assertIn("1直ぶん</b>", html)

    def test_下見はボタンの範囲ぶんだけ数える(self) -> None:
        self.leave_old()
        past = self.get("/api/settings/push/plan?scope=previous").get_json()
        now = self.get("/api/settings/push/plan?scope=current").get_json()
        whole = self.get("/api/settings/push/plan").get_json()
        self.assertEqual((past["pages"], past["shifts"]), (1, 1))
        self.assertEqual((now["pages"], now["shifts"]), (0, 0))
        self.assertEqual(whole["pages"], 1)

    def test_いまの直のボタンは過去分を送らず_そう言う(self) -> None:
        self.leave_old()
        res = self.post("/api/settings/push", {"scope": "current"})
        self.assertEqual(res.status_code, 200, res.get_json())
        body = res.get_json()
        self.assertEqual((body["scope"], body["succeeded"]), ("current", 0))
        self.assertIn("いまの直は共有と同じ中身です", body["message"])
        self.assertIn("「過去分を共有へ送る」から送れます", body["message"])
        self.assertEqual(len(self.repo().pending_sync_headers()), 1)
        row, = self.repo().push_history()
        self.assertEqual((row["result"], row["via"]), ("送るもの無し", "いまの直"))

    def test_過去分が無ければそう言う(self) -> None:
        res = self.post("/api/settings/push", {"scope": "previous"})
        self.assertEqual(res.status_code, 200)
        self.assertIn("共有へ出ていない過去分(前の直)はありません", res.get_json()["message"])

    def test_入力画面のボタンと説明(self) -> None:
        html = self.get("/").get_data(as_text=True)
        self.assertRegex(html, r'id="handover-push"[^>]*>\s*過去分を共有へ送る</button>')
        self.assertRegex(html, r'id="push-shared"[^>]*>\s*共有へ保存</button>')
        self.assertIn("前の直(過去分)</b>は、上の赤い帯の<b>「過去分を共有へ送る」</b>", html)

    def test_画面のボタンは範囲を添えて送る(self) -> None:
        """押した先のサーバが範囲を知るのは、画面が `scope` を添えるからです。"""
        root = Path(__file__).resolve().parent.parent / "app" / "static" / "js"
        entry = (root / "views" / "entry.js").read_text(encoding="utf-8")
        self.assertIn('pushShared("", "current")', entry)
        self.assertIn('pushShared("", "previous")', entry)
        settings = (root / "views" / "settings.js").read_text(encoding="utf-8")
        self.assertIn('pushToShared("", "previous")', settings)
        review = (root / "views" / "review.js").read_text(encoding="utf-8")
        self.assertIn("closing.push_scope", review)


if __name__ == "__main__":
    unittest.main()
