"""作業者名で見る ── 標準作業時間・梱包力を、班ごとに加えて作業者ごとにも (v4.12.4)

    梱包力、梱包標準時間において班での収集になっているが、作業者名でもわかるように
    してください(普段は見えなくていいです)

【約束】
    ・画面の頭に「作業者名で見る」。**既定は切**(覚えない ── 開くたびに切)
    ・入れると、標準の面に「まとめ方: 作業者ごと」、梱包力のまとめ方に「作業者ごと」が出る。
      切ると隠れ、作業者ごとで出していた表は班ごとへ戻る
    ・作業者ごとの標準は実績から作る。1つの作業は**その場に居た全員に**数える(梱包力と同じ)
    ・「班」の列には作業者の名前、右隣に名簿の班。班で絞ると名簿の班で絞る
    ・CSV は `標準作業時間_作業者ごと_…`(班ごとのCSVを上書きしない)
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import standard_time as logic  # noqa: E402
from nippou.presenters import standard_time as view  # noqa: E402
from tests._web import WebTestCase  # noqa: E402
from tests.test_standard_time import ROOT, TEAMS, WorksFixture, sample  # noqa: E402


class WorkerStandardsLogicTests(unittest.TestCase):
    def test_1つの作業を居た全員に数える(self) -> None:
        rows = [sample(row_no=1, worker="山田 鈴木(新人教育)", work_minutes=60),
                sample(row_no=2, worker="山田", work_minutes=80)]
        got = {st.team: st for st in logic.worker_standards(rows)}
        self.assertEqual(set(got), {"山田", "鈴木"})
        self.assertEqual(got["山田"].count, 2)
        self.assertEqual(got["鈴木"].count, 1)
        self.assertEqual(got["鈴木"].per_package.median, 30.0)      # 60分×2人÷4梱包

    def test_全班の行は付けない_数えない作業も入れない(self) -> None:
        rows = [sample(row_no=1), sample(row_no=2, workers=0)]
        got = logic.worker_standards(rows)
        self.assertEqual([st.team for st in got], ["山田"])
        self.assertEqual(got[0].count, 1)


class WorkerOrderTests(unittest.TestCase):
    """**登録の順番が違っても、同じ人は同じ人**(v4.12.5)。

        作業者 A B C / 作業者 A C B ── 同じメンツであるのに順番が違うと
        別のカウントでは困ります

    数えるのは**1人ずつ**です(並びの文字列そのものを鍵にしない)。順番・区切り
    (半角/全角の空白・「・」「、」)・同じ名前の重なり・(新人教育)のような添え書きは
    数に効きません。
    """

    ORDERS = ("山田 鈴木 佐藤", "山田 佐藤 鈴木", "佐藤　山田　鈴木",   # 全角の空白
              "鈴木・佐藤・山田", "山田 鈴木 佐藤 山田", "佐藤(新人教育) 鈴木 山田")

    def test_名前の並びが違っても同じ3人(self) -> None:
        for text in self.ORDERS:
            self.assertEqual(sorted(logic.names_of(text)), ["佐藤", "山田", "鈴木"], text)

    def test_標準も梱包力も_順番で別の数にならない(self) -> None:
        rows = [sample(row_no=i, worker=w, work_minutes=60 + i)
                for i, w in enumerate(self.ORDERS, 1)]
        got = {st.team: st.count for st in logic.worker_standards(rows)}
        self.assertEqual(got, {"佐藤": 6, "山田": 6, "鈴木": 6})        # 6件ぜんぶ、3人とも
        _, by_worker = logic.packing_power(rows, lambda s: None, "worker")
        self.assertEqual({p.label: p.works for p in by_worker}, got)

    def test_班の決め方も順番に左右されない(self) -> None:
        teams = {"山田": "A", "鈴木": "A", "佐藤": "B"}
        self.assertEqual({logic.team_of(w, teams) for w in self.ORDERS}, {"A"})


class WorkerStandardsViewTests(WorksFixture):
    """下ごしらえ: 9/1 山田(A)3件・9/2 佐藤(B)1件(条件 H176/1P0001/8×1528×3053)ほか。"""

    def build(self, **payload):
        return view.build(self.db, view.Filters.of(payload, "L-1"))

    def rows(self, built) -> list[dict]:
        return [dict(zip(built.table.columns, r)) for r in built.table.rows]

    def test_既定は班ごとのまま(self) -> None:
        built = self.build()
        self.assertEqual(built.table.columns[:2], ["ライン", "班"])
        self.assertNotIn("作業者", built.table.columns)
        self.assertEqual(built.table.title, "標準作業時間")

    def test_作業者ごと_名前の右に名簿の班(self) -> None:
        built = self.build(group="worker")
        self.assertEqual(built.table.columns[:3], ["ライン", "作業者", "班"])
        self.assertEqual(built.table.title, "標準作業時間(作業者ごと)")
        self.assertIn("作業者ごと", built.table.note)
        cond = [r for r in self.rows(built)
                if (r["用途コード"], r["包装仕様NO"]) == ("H176", "1P0001")]
        self.assertEqual([(r["作業者"], r["班"], r["件数"], r["標準(人分/梱包)"], r["仮"])
                          for r in cond],
                         [("佐藤", "B", "1", "50.0", "仮"), ("山田", "A", "3", "35.0", "")])
        # 「抽出」は班を問わずに(作業者の列で見分ける)
        self.assertTrue(all(c["team"] == "" for c in built.conditions))

    def test_班で絞ると名簿の班で(self) -> None:
        built = self.build(group="worker", team="A")
        self.assertEqual({r["作業者"] for r in self.rows(built)}, {"山田"})

    def test_計算式も作業者ごとの実績から(self) -> None:
        built = self.build(group="worker", formulas=True,
                           purpose_code="H176", packing_spec_no="1P0001")
        row = next(r for r in self.rows(built) if r["作業者"] == "山田")
        self.assertIn("35.0", row["式: 標準(人分/梱包)"])
        self.assertNotIn("50.0", row["式: 標準(人分/梱包)"])    # 佐藤の作業は入らない


class WorkerModeWebTests(WebTestCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = patch("nippou.services.standard_time.load_teams", return_value=dict(TEAMS))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_画面_普段は出さない(self) -> None:
        page = self.get("/standard-time").get_data(as_text=True)
        self.assertRegex(page, r'<input type="checkbox" id="worker-mode">')   # 既定は切
        # まとめ方の欄はいつも出す(オペレーター構成ごと ── v4.15.0)。作業者ごとだけ隠す
        self.assertRegex(page, r'<div class="field">\s*<label for="standard-group">')
        self.assertIn('<option value="worker" data-worker-only hidden disabled>作業者ごと</option>',
                      page)
        js = (ROOT / "app" / "static" / "js" / "views" / "standard_time.js").read_text(
            encoding="utf-8")
        for needle in ('"worker-mode"', "[data-worker-only]", '!workerMode()'):
            self.assertIn(needle, js)

    def test_作業者ごとのAPIとCSV(self) -> None:
        body = self.post("/api/standard-time/standards", {"group": "worker"}).get_json()
        self.assertEqual(body["table"]["title"], "標準作業時間(作業者ごと)")
        self.assertEqual(body["table"]["columns"][:3], ["ライン", "作業者", "班"])
        body = self.post("/api/standard-time/standards/csv", {"group": "worker"}).get_json()
        self.assertTrue(Path(body["file"]).name.startswith("標準作業時間_作業者ごと_"),
                        body["file"])
        body = self.post("/api/standard-time/standards/csv", {}).get_json()
        self.assertTrue(re.match(r"標準作業時間_\d{4}-", Path(body["file"]).name), body["file"])


if __name__ == "__main__":
    unittest.main()
