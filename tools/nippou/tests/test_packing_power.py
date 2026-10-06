"""梱包力 ── ゲームの DPS のように、標準と比べた梱包の速さ (v4.10.0)

    ゲームでdpsっていう概念があるが(だーめじぱーせかんど)
    同じように梱包力みたいな概念を作りたい
    標準梱包時間を集計する機能はすでにあるはずなので、そこと連携させてください

【約束】
    ・梱包力 = 標準人分の合計 ÷ 実人分の合計 × 100(100 が標準のペース)
    ・標準人分 = 全班の標準(人分/梱包) × 梱包数(無ければ 人分/枚 × 枚数)
    ・実人分 = 作業時間 × 作業人数
    ・比べる標準は**いつも全班**(班で絞っても) ── 班どうしを比べるため
    ・標準が無い・「仮」(3件未満)の作業は比べない(件数には出す)
    ・作業者でまとめるときは、居た全員に数える。全体には1回だけ
    ・班・作業者・ラインは梱包力の高い順、直・日は時間の順
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.models import DetailRecord, HeaderRecord  # noqa: E402
from nippou.logic import standard_time as logic  # noqa: E402
from nippou.presenters import standard_time as view  # noqa: E402
from nippou.services import summary  # noqa: E402
from unittest.mock import patch  # noqa: E402

from tests import test_standard_time as base  # noqa: E402
from tests._web import WebTestCase  # noqa: E402
from tests.test_standard_time import ROOT, TEAMS, WorksFixture, sample  # noqa: E402


def standard_for(samples):
    """実績から全班の標準を作り、`Works.standard_for` と同じ引き方で返す。"""
    found = {(st.line, st.purpose_code, st.packing_spec_no, st.size): st
             for st in logic.standards(samples) if st.team == logic.TEAM_ALL}
    return lambda s: found.get((s.line, s.purpose_code, s.packing_spec_no, s.size))


def crew() -> list[logic.Sample]:
    """同じ条件(4梱包・2人)の6作業。全班の標準は 30 人分/梱包。

        A班 山田  60分 ×3   → 30 人分/梱包(標準どおり)
        B班 佐藤  90分 ×2   → 45 人分/梱包(遅め)
        C班 田中  45分 ×1   → 22.5 人分/梱包(速い)
    """
    return ([sample(row_no=i, worker="山田", team="A", work_minutes=60) for i in (1, 2, 3)]
            + [sample(row_no=i, worker="佐藤", team="B", work_minutes=90) for i in (4, 5)]
            + [sample(row_no=6, worker="田中", team="C", work_minutes=45)])


# ======================================================================
# 純ロジック
# ======================================================================
class PowerLogicTests(unittest.TestCase):
    def test_標準どおりなら100_速ければ大きく_遅ければ小さく(self) -> None:
        rows = crew()
        total, groups = logic.packing_power(rows, standard_for(rows), "team")
        got = {p.label: round(p.power, 1) for p in groups}
        self.assertEqual(got, {"A": 100.0, "B": 66.7, "C": 133.3})
        # 全体: 標準 30×4×6 = 720人分 ÷ 実際 360+360+90 = 810人分
        self.assertEqual(total.standard_pm, 720)
        self.assertEqual(total.actual_pm, 810)
        self.assertAlmostEqual(total.power, 720 / 810 * 100)

    def test_班は梱包力の高い順(self) -> None:
        rows = crew()
        _, groups = logic.packing_power(rows, standard_for(rows), "team")
        self.assertEqual([p.label for p in groups], ["C", "A", "B"])

    def test_難しい仕事は大きく数える(self) -> None:
        """同じ1時間でも、標準で2倍かかる条件を同じ速さでやれば同じ梱包力。"""
        easy = [sample(row_no=i, work_minutes=60) for i in range(1, 4)]
        hard = [sample(row_no=i, purpose_code="H162", packages=4, work_minutes=120,
                       worker="佐藤", team="B") for i in range(4, 7)]
        rows = easy + hard
        _, groups = logic.packing_power(rows, standard_for(rows), "team")
        self.assertEqual({p.label: round(p.power) for p in groups}, {"A": 100, "B": 100})
        # 素の速さ(1人1時間あたりの梱包数)は倍ちがう ── だから標準で均す
        per_hour = {p.label: p.packages_per_person_hour for p in groups}
        self.assertEqual(per_hour, {"A": 2.0, "B": 1.0})

    def test_標準が無い条件と仮の標準とは比べない(self) -> None:
        rows = crew()
        # 2件しかない条件(仮) と、標準に無い条件
        few = [sample(row_no=i, packing_spec_no="7P0106", work_minutes=10) for i in (7, 8)]
        lonely = sample(row_no=9, purpose_code="X999", work_minutes=10)
        found = standard_for(rows + few)
        self.assertTrue(found(few[0]).provisional)
        total, groups = logic.packing_power(rows + few + [lonely], found, "team")
        self.assertEqual((total.works, total.compared), (9, 6))
        a = next(p for p in groups if p.label == "A")
        self.assertEqual((a.works, a.compared, round(a.power)), (6, 3, 100))

    def test_数えない作業は対象にも入れない(self) -> None:
        rows = crew()
        broken = sample(row_no=9, workers=0)
        self.assertFalse(broken.usable)
        total, _ = logic.packing_power(rows + [broken], standard_for(rows), "team")
        self.assertEqual(total.works, 6)

    def test_梱包数が無ければ枚あたりの標準で(self) -> None:
        rows = crew()
        sheets_only = sample(row_no=9, packages=0, sheets=40, work_minutes=60)
        st = standard_for(rows)(sheets_only)
        self.assertEqual(st.per_sheet.median, 3.0)
        self.assertEqual(logic.standard_person_minutes(st, sheets_only), 120.0)

    def test_作業者は居た全員に数え_全体には1回(self) -> None:
        rows = crew()
        pair = sample(row_no=9, worker="山田 田中(新人教育)", team="A", work_minutes=60)
        total, groups = logic.packing_power(rows + [pair], standard_for(rows), "worker")
        works = {p.label: p.works for p in groups}
        self.assertEqual(works, {"山田": 4, "佐藤": 2, "田中": 2})
        self.assertEqual(total.works, 7)

    def test_直と日は時間の順(self) -> None:
        rows = [sample(report_date="2026年9月10日", shift="1直", row_no=1),
                sample(report_date="2026年9月2日", shift="2直", row_no=2),
                sample(report_date="2026年9月2日", shift="1直", row_no=3)]
        found = standard_for(rows)
        _, shifts = logic.packing_power(rows, found, "shift")
        self.assertEqual([p.label for p in shifts],
                         ["2026年9月2日 1直", "2026年9月2日 2直", "2026年9月10日 1直"])
        _, days = logic.packing_power(rows, found, "date")
        self.assertEqual([p.label for p in days], ["2026年9月2日", "2026年9月10日"])

    def test_知らないまとめ方は班(self) -> None:
        rows = crew()
        _, groups = logic.packing_power(rows, standard_for(rows), "nope")
        self.assertEqual({p.label for p in groups}, {"A", "B", "C"})

    def test_式と意味(self) -> None:
        p = logic.Power("A", works=2, compared=2, standard_pm=360, actual_pm=300)
        self.assertEqual(logic.power_formula(p), "標準 360人分 ÷ 実際 300人分 × 100 = 120")
        self.assertEqual(logic.power_meaning(120), "標準の1.20倍の速さ")
        self.assertEqual(logic.power_meaning(100.2), "標準どおりの速さ")
        self.assertEqual(logic.power_meaning(85), "標準の0.85倍(標準より遅め)")
        self.assertEqual(logic.power_meaning(None), "")
        self.assertIsNone(logic.Power("空").power)
        self.assertEqual(logic.power_formula(logic.Power("空")), "標準と比べられる作業がありません")

    def test_作業者欄の名前(self) -> None:
        self.assertEqual(logic.names_of("山田(新人教育) 鈴木・山田"), ["山田", "鈴木"])
        self.assertEqual(logic.names_of(None), [])
        self.assertEqual(logic.team_of("山田 鈴木", {"山田": "A", "鈴木": "A"}), "A")


# ======================================================================
# 画面の組み立て(共有の 標準作業時間.sqlite3 から)
# ======================================================================
class PowerViewTests(WorksFixture):
    """下ごしらえ(`WorksFixture`): 全班の標準は 37.5 人分/梱包(30・35・40・50 の中央値)。

        9/1 1直 山田(A班) 4梱包×3 … 実 120+140+160 = 420人分 / 標準 450人分
        9/2 2直 佐藤(B班) 4梱包×1 … 実 200人分 / 標準 150人分
        9/3 1直 山田(A班) 別の条件2件 … 標準が「仮」なので比べない
    """

    def power(self, **payload) -> dict:
        return view.build_power(self.db, view.PowerFilters.of(payload, "L-1")).as_dict()

    def test_全体の梱包力が大きな数(self) -> None:
        got = self.power()
        self.assertEqual(got["hero"]["value"], "97")            # 600 ÷ 620 × 100
        self.assertEqual(got["hero"]["meaning"], "標準の0.97倍(標準より遅め)")
        self.assertEqual(got["hero"]["compared"], "比べた作業 4/6件")
        self.assertEqual(got["hero"]["formula"], "標準 600人分 ÷ 実際 620人分 × 100 = 97")

    def test_班ごとの表と棒(self) -> None:
        got = self.power()
        table = got["table"]
        self.assertEqual(table["columns"][:3], ["班", "梱包力", "比べた作業(件)"])
        rows = [dict(zip(table["columns"], r)) for r in table["rows"]]
        self.assertEqual([(r["班"], r["梱包力"]) for r in rows],
                         [("A", "107"), ("B", "75"), ("全体", "97")])
        a = rows[0]
        self.assertEqual((a["比べた作業(件)"], a["対象の作業(件)"]), ("3", "5"))
        self.assertEqual((a["標準人分"], a["実人分"]), ("450.0", "420.0"))
        self.assertEqual(a["差(人分・+は標準より多くかかった)"], "-30.0")
        self.assertEqual(a["梱包数"], "12")
        self.assertEqual(a["1人1時間あたり梱包数"], "1.7")       # 12 ÷ 7時間

        # 棒: 目盛りは 150 まで、100 の線は 2/3 のところ。全体の行は棒にしない
        self.assertEqual(got["scale"], {"max": 150.0, "ref": 66.7})
        self.assertEqual([(m["label"], m["value"], m["width"]) for m in got["meter"]],
                         [("A", "107", 71.4), ("B", "75", 50.0)])
        self.assertTrue(got["meter"][0]["hint"].startswith("A: 梱包力 107(標準の1.07倍の速さ)\n"))
        self.assertIn("標準 450人分 ÷ 実際 420人分 × 100 = 107", got["meter"][0]["hint"])
        self.assertEqual(got["teams"], ["A", "B"])

    def test_班で絞っても標準は全班のもの(self) -> None:
        got = self.power(team="B")
        rows = got["table"]["rows"]
        self.assertEqual([r[:2] for r in rows], [["B", "75"], ["全体", "75"]])
        self.assertEqual(got["teams"], ["A", "B"])             # 選び直せるように全部返す
        self.assertIn("B", got["table"]["note"])

    def test_作業者ごと(self) -> None:
        got = self.power(group="worker")
        # 作業者の右隣に区分(v4.15.0。名簿に無ければ「区分なし」、全体は空)
        self.assertEqual(got["table"]["columns"][:3], ["作業者", "オペレーター", "梱包力"])
        self.assertEqual([[r[0], r[2]] for r in got["table"]["rows"]],
                         [["山田", "107"], ["佐藤", "75"], ["全体", "97"]])
        self.assertEqual([r[1] for r in got["table"]["rows"]], ["区分なし", "区分なし", ""])

    def test_直ごとは時間の順_比べられない直は棒を出さない(self) -> None:
        got = self.power(group="shift")
        self.assertEqual([r[0] for r in got["table"]["rows"]],
                         ["2026年9月1日 1直", "2026年9月2日 2直", "2026年9月3日 1直", "全体"])
        # 棒の見出しは短く(年まで入った見出しは、乗せたときの説明に)
        self.assertEqual([m["label"] for m in got["meter"]], ["9/1 1直", "9/2 2直", "9/3 1直"])
        self.assertTrue(got["meter"][0]["hint"].startswith("2026年9月1日 1直: 梱包力"))
        self.assertEqual([m["label"] for m in self.power(group="date")["meter"]],
                         ["9/1", "9/2", "9/3"])
        last = got["meter"][-1]
        self.assertEqual((last["value"], last["width"], last["muted"]), ("—", 0.0, True))
        self.assertIn("標準と比べられる作業がありません", last["hint"])

    def test_目盛りは一番速いものが入るまで広げる(self) -> None:
        # 田中(C班)がとても速い直: 4梱包を2人で20分 = 10 人分/梱包
        self.pushed(self.page("2026年9月4日", "1直", "田中", [(4, 2, 20, 40)]))
        got = self.power()
        # 全班の標準は 35(10・30・35・40・50 の中央値)→ C班は 140 ÷ 40 × 100 = 350
        self.assertEqual(got["meter"][0]["label"], "C")
        self.assertEqual(got["meter"][0]["value"], "350")
        self.assertEqual(got["scale"], {"max": 350.0, "ref": 28.6})
        self.assertEqual(got["meter"][0]["width"], 100.0)

    def test_期間で絞る(self) -> None:
        got = self.power(start="2026-09-02", end="2026-09-02")
        self.assertEqual([r[:2] for r in got["table"]["rows"]], [["B", "75"], ["全体", "75"]])
        self.assertIn("2026-09-02 〜 2026-09-02", got["table"]["note"])

    def test_計算式の列(self) -> None:
        got = self.power(formulas=True)
        self.assertEqual(got["table"]["columns"][-1], "式: 梱包力")
        self.assertEqual(got["table"]["rows"][0][-1], "標準 450人分 ÷ 実際 420人分 × 100 = 107")
        self.assertNotIn("式: 梱包力", self.power()["table"]["columns"])

    def test_日付の向きが逆なら断る(self) -> None:
        with self.assertRaises(view.BadRequest):
            view.PowerFilters.of({"start": "2026-09-30", "end": "2026-09-01"}, "L-1")

    def test_作業が無いとき(self) -> None:
        got = view.build_power(self.tmp / "無い.sqlite3", view.PowerFilters()).as_dict()
        self.assertEqual(got["hero"]["value"], "—")
        self.assertEqual(got["hero"]["meaning"], "この期間の作業はありません")
        self.assertEqual(got["table"]["rows"], [])
        self.assertEqual(got["meter"], [])

    def test_比べられる作業が無いとき(self) -> None:
        got = self.power(start="2026-09-03", end="2026-09-03")
        self.assertEqual(got["hero"]["value"], "—")
        self.assertEqual(got["hero"]["meaning"], "標準と比べられる作業がまだありません")

    def test_棒は30本まで_残りは表に(self) -> None:
        for i in range(32):
            key = dict(report_date=f"2026年8月{i % 28 + 1}日", line="L-1",
                       shift=("1直", "2直")[i // 28], page=1)
            self.repo.save(HeaderRecord(**key, worker=f"作業者{i:02d}"), [
                DetailRecord(**key, row_no=1, lot="N7300T0", siz="8.000×1528.0×3053.0",
                             tut="4", hit="2", tim="75", con="40", others1="H176",
                             others4="1P0001")])
            summary.refresh_shift(self.repo, key["report_date"], "L-1", key["shift"])
            self.pushed((key["report_date"], "L-1", key["shift"]))
        got = self.power(group="worker", start="2026-08-01", end="2026-08-31")
        self.assertEqual(len(got["meter"]), view.POWER_METER_ROWS)
        self.assertEqual(got["more"], 2)
        self.assertEqual(got["more_note"], "ほか 2件(下位)は下の表に")
        self.assertEqual(len(got["table"]["rows"]), 33)       # 32人 + 全体
        # 直ごとは**新しいほう**の30本(推移は最近が見たい)
        got = self.power(group="shift", start="2026-08-01", end="2026-09-30")
        self.assertEqual(got["more_note"], "ほか 5件(古いほう)は下の表に")
        self.assertEqual(got["meter"][-1]["label"], "9/3 1直")
        self.assertEqual(got["meter"][0]["label"], "8/3 2直")  # 古い5直(8/1 1直〜8/3 1直)が外れる


# ======================================================================
# 画面
# ======================================================================
class PowerWebTests(WebTestCase):
    def setUp(self) -> None:
        super().setUp()
        from nippou import config, user_settings
        self.share = self.tmp / "共有"
        self.share.mkdir()
        user_settings.save_many({config.KEY_ACCESS_DIR: str(self.share),
                                 config.KEY_REPORT_OUT_DIR: str(self.tmp / "出力")})
        patcher = patch("nippou.services.standard_time.load_teams", return_value=dict(TEAMS))
        patcher.start()
        self.addCleanup(patcher.stop)

    # 1直ぶん打って「共有へ保存」する手順は、標準作業時間のテストと同じ
    save_shift = base.WebTests.save_shift
    push = base.WebTests.push

    def three_shifts(self):
        keys = [self.save_shift(day=f"2026年9月{d}日") for d in (1, 2, 3)]
        self.push(*keys)

    def test_梱包力(self) -> None:
        self.three_shifts()
        body = self.post("/api/standard-time/power", {
            "start": "2026-09-01", "end": "2026-09-30"}).get_json()
        self.assertEqual(body["hero"]["value"], "100", body)
        self.assertEqual(body["hero"]["compared"], "比べた作業 3/3件")
        self.assertEqual(body["table"]["rows"][0][:2], ["A", "100"])
        self.assertIn("L-1", body["table"]["note"])          # ライン未指定ならこの端末(正規の呼び名)
        body = self.post("/api/standard-time/power", {"scope": "all", "group": "date",
                                                      "formulas": True}).get_json()
        self.assertEqual(body["group_label"], "日")
        self.assertIn("全ライン", body["table"]["note"])
        self.assertEqual(body["table"]["columns"][-1], "式: 梱包力")
        res = self.post("/api/standard-time/power", {"start": "2026/09/01"})
        self.assertEqual(res.status_code, 400)
        res = self.post("/api/standard-time/power", {"start": "2026-09-30",
                                                     "end": "2026-09-01"})
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["field"], "start")

    def test_CSV(self) -> None:
        self.three_shifts()
        body = self.post("/api/standard-time/power/csv", {"group": "worker"}).get_json()
        out = Path(body["file"])
        self.assertTrue(out.exists(), body)
        self.assertTrue(out.name.startswith("梱包力_作業者ごと_"), out.name)
        self.assertTrue(out.name.endswith("_L-1.csv"), out.name)
        lines = out.read_text(encoding="utf-8-sig").splitlines()
        self.assertTrue(lines[0].startswith("作業者,オペレーター,梱包力,比べた作業(件)"), lines[0])
        self.assertEqual([line.split(",")[0] for line in lines[1:]], ["山田", "鈴木", "全体"])

    def test_画面(self) -> None:
        page = self.get("/standard-time").get_data(as_text=True)
        for part in ('data-key="power"', 'id="panel-power"', 'data-table="power"',
                     'id="power-group"', 'value="worker"', 'id="power-start"',
                     'id="power-csv"', 'id="power-meter"', 'id="power-value"',
                     view.POWER_LEAD):
            self.assertIn(part, page)
        # 説明は**1行だけ**。式は乗せたときに(長い説明は読まれない)
        self.assertIn('data-hint="梱包力 = 標準人分の合計', page)
        self.assertLess(len(view.POWER_LEAD), 50)
        js = (ROOT / "app" / "static" / "js" / "views" / "standard_time.js").read_text(
            encoding="utf-8")
        for needle in ("/api/standard-time/power", "/api/standard-time/power/csv",
                       '"tab:select"', 'key === "power"'):
            self.assertIn(needle, js)


if __name__ == "__main__":
    unittest.main()
