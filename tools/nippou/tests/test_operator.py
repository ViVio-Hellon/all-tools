"""オペレーター区分を標準作業時間・梱包力に使う (v4.15.0)

    テーブル:班員名簿に 列:オペレーター を追加した
    AOP:検査できる人  ABOP:どっちもできる人  BOP:検査できない人 で分けてある
    標準時間と梱包力に使用してください

【約束】
    ・読むのは AOP / ABOP / BOP だけ(全角半角・大文字小文字・空白は揃える)。ほかの値は読まず
      「区分なし」に数え、どの人かを言う(標準作業時間の画面の上・マスタ管理の班員名簿)
    ・作業のオペレーター構成 = 作業者欄の名前を区分で数えたもの(「AOP1・BOP2」。順番は効かない)
    ・標準の面: まとめ方「オペレーター構成ごと」(いつでも選べる。名前は出さない)。班で絞れる
    ・同条件の作業: 1件ずつ「オペレーター構成」の列
    ・梱包力: まとめ方「オペレーター構成」。比べる標準はいつもの全班。作業者ごとは名前の右に区分
    ・CSV は `標準作業時間_オペレーター構成ごと_…`(班ごとのCSVを上書きしない)
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import operator  # noqa: E402
from nippou.logic import standard_time as logic  # noqa: E402
from nippou.presenters import standard_time as view  # noqa: E402
from nippou.services import standard_time as service  # noqa: E402
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402
from tests.test_standard_time import TEAMS, WorksFixture, sample  # noqa: E402

KINDS = {"山田": "AOP", "鈴木": "BOP", "佐藤": "BOP", "高橋": "ABOP"}


class ReadTests(unittest.TestCase):
    def test_読むのは3つだけ(self) -> None:
        for text, kind in (("AOP", "AOP"), (" abop ", "ABOP"), ("ＢＯＰ", "BOP"), ("Bop", "BOP")):
            self.assertEqual(operator.read(text), kind, text)
        for text in ("OP", "A", "AOP1", "検査", "", None):
            self.assertEqual(operator.read(text), "", text)

    def test_読めない値だけ言う(self) -> None:
        self.assertIn("「OP」は読みません", operator.problem("OP"))
        self.assertIn("AOP / ABOP / BOP", operator.problem("OP"))
        for text in ("", None, "  ", "AOP", "abop"):                     # 空(作業長など)は言わない
            self.assertEqual(operator.problem(text), "", text)
        rows = [{"名前": "青木", "オペレーター": "AOP"}, {"名前": "木村", "オペレーター": "OP"},
                {"名前": "作業長", "オペレーター": None}]
        self.assertEqual([f.describe() for f in operator.inspect(rows)],
                         ["木村: 「OP」は読みません(AOP / ABOP / BOP のどれかで入れてください)"])

    def test_構成は区分で数える_順番は効かない(self) -> None:
        self.assertEqual(operator.composition(["山田", "鈴木", "佐藤"], KINDS), "AOP1・BOP2")
        self.assertEqual(operator.composition(["佐藤", "山田", "鈴木"], KINDS), "AOP1・BOP2")
        self.assertEqual(operator.composition(["高橋", "田中"], KINDS), "ABOP1・区分なし1")
        self.assertEqual(operator.composition([], KINDS), operator.NO_WORKER)

    def test_並びは人数の少ない順_検査できる人の多い順(self) -> None:
        labels = ["AOP1・BOP2", "BOP2", "AOP2", "ABOP1・BOP1", "AOP1・BOP1", "区分なし1"]
        self.assertEqual(sorted(labels, key=operator.sort_key),
                         ["区分なし1", "AOP2", "AOP1・BOP1", "ABOP1・BOP1", "BOP2", "AOP1・BOP2"])

    def test_意味(self) -> None:
        self.assertEqual(operator.legend(),
                         "AOP 検査できる人 / ABOP どっちもできる人 / BOP 検査できない人")


class LogicTests(unittest.TestCase):
    def rows(self) -> list[logic.Sample]:
        # AOP1・BOP1 で 60分・80分 / BOP2 で 100分(どれも 4梱包・2人)
        return [sample(row_no=1, worker="山田 鈴木", work_minutes=60),
                sample(row_no=2, worker="鈴木 山田", work_minutes=80),      # 順番違いでも同じ構成
                sample(row_no=3, worker="鈴木 佐藤", work_minutes=100, team="B"),
                sample(row_no=4, worker="山田", workers=0)]                  # 数えない作業

    def test_条件と構成ごとの標準(self) -> None:
        got = {st.team: st for st in logic.operator_standards(self.rows(), KINDS)}
        self.assertEqual(list(got), ["AOP1・BOP1", "BOP2"])                # 全班の行は付けない
        self.assertEqual(got["AOP1・BOP1"].count, 2)
        self.assertEqual(got["AOP1・BOP1"].per_package.median, 35.0)       # (30 + 40) ÷ 2
        self.assertEqual(got["BOP2"].per_package.median, 50.0)

    def test_梱包力を構成でまとめる(self) -> None:
        rows = self.rows()
        standard = logic.standards(rows)
        all_team = next(st for st in standard if st.team == logic.TEAM_ALL)  # 中央値 40.0・3件
        total, groups = logic.packing_power(rows, lambda s: all_team, "operator", kinds=KINDS)
        got = {p.label: round(p.power) for p in groups}
        self.assertEqual(got, {"AOP1・BOP1": 114, "BOP2": 80})              # 320÷280 / 160÷200
        self.assertEqual(total.compared, 3)

    def test_名簿が無ければ区分なし(self) -> None:
        got = [st.team for st in logic.operator_standards(self.rows(), {})]
        self.assertEqual(got, ["区分なし2"])


class LoadTests(unittest.TestCase):
    def test_名簿から区分と読めない人(self) -> None:
        from nippou.logic.staff import StaffMember

        members = [StaffMember("青木", "A", operator="AOP"), StaffMember("木村", "B", operator="bop"),
                   StaffMember("佐藤", "C", operator="ＡＢＯＰ"), StaffMember("田中", "昼", operator="OP"),
                   StaffMember("作業長", "A", operator="")]
        with patch("nippou.access_bridge.staff_master.load_staff_members", return_value=members):
            found = service.load_operators()
        self.assertEqual(found.kinds, {"青木": "AOP", "木村": "BOP", "佐藤": "ABOP"})
        self.assertEqual([f.name for f in found.findings], ["田中"])
        self.assertIn("班員名簿のオペレーターに読めない値があります(田中: 「OP」は読みません", found.warning())
        self.assertIn("区分なしとして数えています", found.warning())

    def test_列の無い名簿では言わない(self) -> None:
        from nippou.logic.staff import StaffMember

        with patch("nippou.access_bridge.staff_master.load_staff_members",
                   return_value=[StaffMember("青木", "A")]):
            found = service.load_operators()
        self.assertEqual((found.kinds, found.findings, found.warning()), ({}, [], ""))


class OperatorViewTests(WorksFixture):
    """下ごしらえ(WorksFixture)に、2人・3人で組んだ直を足す。"""

    def setUp(self) -> None:
        super().setUp()
        self.page("2026年9月4日", "1直", "山田 鈴木", [(4, 2, 60, 40), (4, 2, 80, 40)])
        self.page("2026年9月5日", "2直", "鈴木 佐藤", [(4, 2, 100, 40)])
        self.pushed(("2026年9月4日", "L-1", "1直"), ("2026年9月5日", "L-1", "2直"))
        patcher = patch.object(service, "load_operators",
                               return_value=service.Operators(kinds=dict(KINDS), has_column=True))
        patcher.start()
        self.addCleanup(patcher.stop)

    def build(self, **payload):
        return view.build(self.db, view.Filters.of(payload, "L-1"))

    def rows(self, table) -> list[dict]:
        return [dict(zip(table.columns, r)) for r in table.rows]

    def test_標準をオペレーター構成ごとに(self) -> None:
        built = self.build(group="operator", purpose_code="H176", packing_spec_no="1P0001")
        self.assertEqual(built.table.title, "標準作業時間(オペレーター構成ごと)")
        self.assertEqual(built.table.columns[:2], ["ライン", "オペレーター構成"])
        self.assertIn("AOP 検査できる人", built.table.note)
        got = [(r["オペレーター構成"], r["件数"], r["標準(人分/梱包)"]) for r in self.rows(built.table)]
        # 山田ひとり(AOP1)3件・佐藤ひとり(BOP1)1件・山田と鈴木(AOP1・BOP1)2件・鈴木と佐藤(BOP2)1件
        self.assertEqual(got, [("AOP1", "3", "35.0"), ("BOP1", "1", "50.0"),
                               ("AOP1・BOP1", "2", "35.0"), ("BOP2", "1", "50.0")])

    def test_班で絞ってから構成で数える(self) -> None:
        built = self.build(group="operator", team="B", purpose_code="H176")
        self.assertEqual({r["オペレーター構成"] for r in self.rows(built.table)}, {"BOP1"})

    def test_計算式も構成ごとの実績から(self) -> None:
        built = self.build(group="operator", formulas=True, purpose_code="H176",
                           packing_spec_no="1P0001")
        row = next(r for r in self.rows(built.table) if r["オペレーター構成"] == "AOP1・BOP1")
        self.assertIn("30.0", row["式: 標準(人分/梱包)"])
        self.assertIn("40.0", row["式: 標準(人分/梱包)"])

    def test_作業者ごとは名前の右に区分(self) -> None:
        built = self.build(group="worker")
        self.assertEqual(built.table.columns[:4], ["ライン", "作業者", "班", "オペレーター"])
        kinds = {r["作業者"]: r["オペレーター"] for r in self.rows(built.table)}
        self.assertEqual(kinds["山田"], "AOP")
        self.assertEqual(kinds["佐藤"], "BOP")

    def test_同条件の作業に構成の列(self) -> None:
        got = view.build_works(self.db, view.WorkFilters.of(
            {"purpose_code": "H176", "packing_spec_no": "1P0001", "size": "8x1528x3053"}, "L-1"))
        self.assertIn("オペレーター構成", got.table.columns)
        values = [r["オペレーター構成"] for r in self.rows(got.table)]
        self.assertIn("AOP1・BOP1", values)
        self.assertIn("BOP2", values)

    def test_梱包力を構成で(self) -> None:
        got = view.build_power(self.db, view.PowerFilters.of({"group": "operator"}, "L-1"))
        self.assertEqual(got.table.columns[0], "オペレーター構成")
        labels = [r[0] for r in got.table.rows]
        self.assertIn("AOP1・BOP1", labels)
        self.assertEqual(labels[-1], "全体")
        self.assertIn("AOP 検査できる人", got.table.note)

    def test_梱包力の作業者ごとは名前の右に区分(self) -> None:
        got = view.build_power(self.db, view.PowerFilters.of({"group": "worker"}, "L-1"))
        self.assertEqual(got.table.columns[:2], ["作業者", "オペレーター"])
        kinds = {r[0]: r[1] for r in got.table.rows}
        self.assertEqual((kinds["山田"], kinds["鈴木"], kinds["全体"]), ("AOP", "BOP", ""))

    def test_読めない値は画面の上に(self) -> None:
        bad = service.Operators(kinds=dict(KINDS), has_column=True, findings=[
            operator.Finding("田中", "OP", operator.problem("OP"))])
        with patch.object(service, "load_operators", return_value=bad):
            built = self.build(group="operator")
            works = view.build_works(self.db, view.WorkFilters.of({"purpose_code": "H176"}, "L-1"))
            power = view.build_power(self.db, view.PowerFilters.of({"group": "operator"}, "L-1"))
        for warning in (built.warning, works.warning, power.warning):
            self.assertIn("田中: 「OP」は読みません", warning)


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class OperatorWebTests(WebTestCase):
    def setUp(self) -> None:
        super().setUp()
        for target, value in (("load_teams", dict(TEAMS)),
                              ("load_operators", service.Operators(kinds=dict(KINDS),
                                                                   has_column=True))):
            patcher = patch(f"nippou.services.standard_time.{target}", return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_画面_構成ごとはいつでも選べる(self) -> None:
        page = self.get("/standard-time").get_data(as_text=True)
        self.assertIn('<option value="operator">オペレーター構成ごと</option>', page)
        self.assertIn('<option value="operator">オペレーター構成ごと</option>', page)  # 梱包力
        self.assertIn('id="guide-operator"', page)
        self.assertIn("AOP 検査できる人", page)
        self.assertIn("作業者「Aさん Bさん Cさん」 → AOP1・BOP2", page)

    def test_APIとCSV(self) -> None:
        body = self.post("/api/standard-time/standards", {"group": "operator"}).get_json()
        self.assertEqual(body["table"]["title"], "標準作業時間(オペレーター構成ごと)")
        body = self.post("/api/standard-time/standards/csv", {"group": "operator"}).get_json()
        self.assertTrue(Path(body["file"]).name.startswith("標準作業時間_オペレーター構成ごと_"),
                        body["file"])
        body = self.post("/api/standard-time/power/csv", {"group": "operator"}).get_json()
        self.assertTrue(Path(body["file"]).name.startswith("梱包力_オペレーター構成ごと_"),
                        body["file"])


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class StaffMasterCheckTests(WebTestCase):
    """マスタ管理で班員名簿を開くと、読めないオペレーターの人が上に出る。"""

    def setUp(self) -> None:
        super().setUp()
        self.ref = self.tmp / "ref"
        self.ref.mkdir(exist_ok=True)
        conn = sqlite3.connect(str(self.ref / "梱包資材マスタ.sqlite3"))
        with conn:
            conn.execute('CREATE TABLE "班員名簿" ("名前" TEXT, "班" TEXT, "オペレーター" TEXT)')
            conn.executemany('INSERT INTO "班員名簿" VALUES (?,?,?)',
                             [("青木", "A", "AOP"), ("木村", "B", "OP"), ("作業長", "A", None)])
        conn.close()
        self.post("/api/settings/paths", {"gw_reference_dir": str(self.ref), "password": "nisk"})

    def test_点検が出る(self) -> None:
        body = self.get("/api/master/browse?file=material&table=班員名簿").get_json()
        self.assertEqual(body["page"]["checks"],
                         ["木村: 「OP」は読みません(AOP / ABOP / BOP のどれかで入れてください)"])
        self.assertIn("オペレーターが読めない人が1人います", body["page"]["checks_head"])

    def test_名簿から読む(self) -> None:
        from nippou.access_bridge import staff_master

        members = {m.name: m.operator for m in staff_master.load_staff_members()}
        self.assertEqual(members, {"青木": "AOP", "木村": "OP", "作業長": ""})
        from nippou.services import standard_time as svc

        self.assertEqual(svc.load_operators().kinds, {"青木": "AOP"})


if __name__ == "__main__":
    unittest.main()
