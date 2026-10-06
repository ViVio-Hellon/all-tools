"""負荷係数(係数処理ﾛｯﾄ数) ── コイルのラインだけの数え方

VBA `負荷係数反映(27)` → `不要係数処理ロットクリア` → `Lot数計算`。

ここで守りたいのは2つです:
  ・コイル(丈0)のライン**だけ**で、用途コードごとの重みを掛けて数える
  ・同じロットを2度数えない(前の直から続いている / ページをまたいでいる)
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.models import DetailRecord, HeaderRecord
from nippou.logic import load_factor
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase

DAY = "2026年8月31日"
LINE = "機側"           # 機側。コイル(丈0)のライン
FACTORS = {"H283": 1.4, "H197": 0.6}


def row(row_no: int, *, page: int = 1, lot: str = "", usage: str = "",
        shift: str = "1直", keisu: str = "", day: str = DAY,
        line: str = LINE) -> DetailRecord:
    return DetailRecord(report_date=day, line=line, shift=shift, page=page,
                        row_no=row_no, lot=lot, others1=usage, keisu=keisu)


class AppliesTests(unittest.TestCase):
    """ﾛｯﾄ数は機側・NS1・AIM、係数は機側・NS1 だけ。

        係数Lot数：計算は機側とNS1だけでいいです(v4.18.0)
        AIM の ﾛｯﾄ数は自動のまま / 機側、NS1はコイル形状担当です その他は板形状です(v4.20.0)
    """

    def test_ﾛｯﾄ数は機側とNS1とAIM(self) -> None:
        for line in ("機側", "NS1", "AIM"):
            self.assertTrue(load_factor.applies(line), line)
        for line in ("L-1", "LVC", "HVC", "トット", "バランサー", "中板"):
            self.assertFalse(load_factor.applies(line), line)

    def test_係数はコイル形状の機側とNS1だけ(self) -> None:
        for line in ("機側", "NS1"):
            self.assertTrue(load_factor.counts_coefficient(line), line)
        for line in ("AIM", "L-1", "LVC", "HVC", "トット", "バランサー", "中板"):
            self.assertFalse(load_factor.counts_coefficient(line), line)

    def test_計算になる合計欄(self) -> None:
        """計算で決まる欄は読み取り専用。AIM は ﾛｯﾄ数だけ。"""
        from nippou.presenters import entry

        def calculated(line):
            return {name for name, _, _, calc in entry.total_fields(line)
                    if calc and name in ("lot_count", "coefficient_lot_count")}

        self.assertEqual(calculated("機側"), {"lot_count", "coefficient_lot_count"})
        self.assertEqual(calculated("NS1"), {"lot_count", "coefficient_lot_count"})
        self.assertEqual(calculated("AIM"), {"lot_count"})
        self.assertEqual(calculated("HVC"), set())

    def test_AIMはﾛｯﾄ数だけ数えて_係数の欄に触れない(self) -> None:
        rows = [row(1, lot="A", usage="H283", line="AIM", keisu="打った"),
                row(2, lot="A", usage="H283", line="AIM"),
                row(3, lot="B", usage="H197", line="AIM")]
        previous = [[row(5, lot="B", shift="3直", line="AIM")]]   # B は前の直から続き
        found = load_factor.recalculate(rows, line="AIM", factors=FACTORS, previous=previous)
        self.assertTrue(found.applied)
        self.assertFalse(found.coefficient)
        self.assertEqual(found.totals.lot_count, 1)                # A だけ(2度目・続きは数えない)
        self.assertEqual([r.keisu for r in rows], ["打った", "", ""])   # 27列目はそのまま
        self.assertEqual(found.as_dict()["coefficient"], "")
        self.assertNotIn("係数Lot数", found.message)

    def test_AIMは包み数は変わらず_コイル割り数は引かない(self) -> None:
        """「AIMはコイル割り数不要です」(v4.21.0)。"""
        from nippou.constants import PACKAGE_CALC_LINES
        from nippou.logic import lot_fill

        self.assertIn("AIM", PACKAGE_CALC_LINES)
        self.assertFalse(lot_fill.needs_coil_split("AIM"))

    def test_走らないラインでは何も返らない(self) -> None:
        rows = [row(1, lot="A", usage="H283")]
        found = load_factor.recalculate(rows, line="HVC", factors=FACTORS)
        self.assertFalse(found.applied)
        self.assertEqual(rows[0].keisu, "")        # 触らない


class FactorTests(unittest.TestCase):
    """用途コード → 換算係数(`用途名負荷係数算出`)。"""

    def test_登録があればその係数(self) -> None:
        self.assertEqual(load_factor.factor_for("H283", FACTORS), 1.4)

    def test_登録が無ければ1(self) -> None:
        """VBA `負荷LotC = 1`。読めない日でも数えられるようにする。"""
        self.assertEqual(load_factor.factor_for("ZZZZ", FACTORS), 1.0)
        self.assertEqual(load_factor.factor_for("", FACTORS), 1.0)

    def test_書き込む形は小数1桁(self) -> None:
        """VBA `Format(負荷LotC, "0.0")`。"""
        self.assertEqual(load_factor.format_factor(1.32257358137498), "1.3")
        self.assertEqual(load_factor.format_factor(1.0), "1.0")

    def test_ロット番号のある行だけ埋める(self) -> None:
        rows = [row(1, lot="A", usage="H283"), row(2, usage="H283")]
        load_factor.apply_factors(rows, FACTORS)
        self.assertEqual(rows[0].keisu, "1.4")
        self.assertEqual(rows[1].keisu, "")

    def test_打ち直したら係数も入れ直す(self) -> None:
        rows = [row(1, lot="A", usage="H197", keisu="9.9")]
        load_factor.apply_factors(rows, FACTORS)
        self.assertEqual(rows[0].keisu, "0.6")


class CountTests(unittest.TestCase):
    """`Lot数計算` ── 同じ直の中で2度目のロットは数えない。"""

    def test_係数を足す(self) -> None:
        rows = [row(1, lot="A", keisu="1.4"), row(2, lot="B", keisu="0.6")]
        totals = load_factor.count(rows)
        self.assertEqual(totals.lot_count, 2)
        self.assertEqual(totals.coefficient_text, "2.0")

    def test_同じロットは1回だけ(self) -> None:
        """1つのロットを何行にも分けて打つ。行の数で足すと何倍にもなる。"""
        rows = [row(1, lot="A", keisu="1.4"), row(2, lot="A", keisu="1.4")]
        totals = load_factor.count(rows)
        self.assertEqual(totals.lot_count, 1)
        self.assertEqual(totals.coefficient_text, "1.4")
        # 2度目の行の係数は消える(VBA も同じ場所で消す)
        self.assertEqual(rows[1].keisu, "")

    def test_ページをまたいでも1回だけ(self) -> None:
        rows = [row(12, page=1, lot="A", keisu="1.4"),
                row(1, page=2, lot="A", keisu="1.4")]
        totals = load_factor.count(rows)
        self.assertEqual(totals.lot_count, 1)
        self.assertEqual(rows[1].keisu, "")

    def test_係数の無い行は数えない(self) -> None:
        """VBA `If IWS1.Cells(i, 27) <> "" Then count = count + 1`。"""
        rows = [row(1, lot="A", keisu=""), row(2, lot="B", keisu="1.0")]
        self.assertEqual(load_factor.count(rows).lot_count, 1)

    def test_ロット番号の無い行は見ない(self) -> None:
        self.assertEqual(load_factor.count([row(1, keisu="1.4")]).lot_count, 0)


class CarriedOverTests(unittest.TestCase):
    """`不要係数処理ロットクリア` ── 前の直から続くロットは数えない。

    1直でロットAが終わらず、2直で続けて終える。どちらの直にもロットAの
    行があるので、放っておくと係数が2回足される。
    """

    def test_前の直にもあれば消す(self) -> None:
        now = [row(1, lot="A", shift="2直", keisu="1.4"),
               row(2, lot="B", shift="2直", keisu="0.6")]
        before = [[row(12, lot="A", shift="1直", keisu="1.4")]]
        cleared = load_factor.clear_carried_over(now, before)
        self.assertEqual(cleared, ["A"])
        self.assertEqual(now[0].keisu, "")         # 続きのぶんは数えない
        self.assertEqual(now[1].keisu, "0.6")      # 新しいロットは残る

    def test_1直と2直で合わせて1回になる(self) -> None:
        """利用者の例そのもの: 1直でA未完 → 2直でA完了。"""
        first = [row(1, lot="A", shift="1直", usage="H283")]
        load_factor.recalculate(first, line=LINE, factors=FACTORS)
        self.assertEqual(load_factor.count(first).coefficient_text, "1.4")

        second = [row(1, lot="A", shift="2直", usage="H283")]
        found = load_factor.recalculate(second, line=LINE, factors=FACTORS,
                                        previous=[first])
        self.assertEqual(found.totals.lot_count, 0)
        self.assertEqual(found.totals.coefficient_text, "0.0")
        self.assertEqual(found.cleared_lots, ["A"])

    def test_遡るのは3枚まで(self) -> None:
        now = [row(1, lot="A", shift="3直", keisu="1.4")]
        far = [[], [], [], [row(1, lot="A", keisu="1.4")]]   # 4枚前
        self.assertEqual(load_factor.clear_carried_over(now, far), [])
        self.assertEqual(now[0].keisu, "1.4")

    def test_3枚目にあれば消す(self) -> None:
        """**1枚目で見つからなくても止めない。**

        VBA は1枚目で1件でも消せたら残りを見なかった。2枚前から続いて
        いるロットを取りこぼすので、ここでは3枚とも見る。
        """
        now = [row(1, lot="A", shift="3直", keisu="1.4")]
        before = [[row(1, lot="X")], [row(1, lot="Y")],
                  [row(1, lot="A")]]
        self.assertEqual(load_factor.clear_carried_over(now, before), ["A"])

    def test_今の直の2ページ目も見る(self) -> None:
        """VBA は1ページ目しか見ていなかった(引き継ぎが2ページ目だと素通り)。"""
        now = [row(12, page=1, lot="B", keisu="0.6"),
               row(1, page=2, lot="A", keisu="1.4")]
        before = [[row(1, lot="A")]]
        self.assertEqual(load_factor.clear_carried_over(now, before), ["A"])
        self.assertEqual(now[1].keisu, "")

    def test_前の直が無ければ何もしない(self) -> None:
        now = [row(1, lot="A", keisu="1.4")]
        self.assertEqual(load_factor.clear_carried_over(now, []), [])
        self.assertEqual(now[0].keisu, "1.4")


class OrderTests(unittest.TestCase):
    """順番を変えると数が変わる。"""

    def test_入れる_消す_数えるの順(self) -> None:
        rows = [row(1, lot="A", usage="H283"), row(2, lot="B", usage="H197")]
        before = [[row(1, lot="A")]]
        found = load_factor.recalculate(rows, line=LINE, factors=FACTORS,
                                        previous=before)
        # Aは消えて、Bだけが残る
        self.assertEqual(found.totals.lot_count, 1)
        self.assertEqual(found.totals.coefficient_text, "0.6")

    def test_文言に何件消したかが出る(self) -> None:
        rows = [row(1, lot="A", usage="H283")]
        found = load_factor.recalculate(rows, line=LINE, factors=FACTORS,
                                        previous=[[row(1, lot="A")]])
        self.assertIn("前の直から続き 1件", found.message)


# ======================================================================
# マスタの読み取り
# ======================================================================
class MasterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _write(self, rows) -> Path:
        path = self.tmp / "伝送用ファイル.sqlite3"
        conn = sqlite3.connect(str(path))
        with conn:
            conn.execute('CREATE TABLE "用途名負荷係数算出" ('
                         '"用途コード" TEXT, "用途名" TEXT, "負荷係数" TEXT,'
                         '"PENAを1とした場合の換算係数" TEXT, "係数丸め" TEXT)')
            conn.executemany('INSERT INTO "用途名負荷係数算出" VALUES (?,?,?,?,?)',
                             rows)
        conn.close()
        return path

    def test_換算係数を読む(self) -> None:
        from nippou.access_bridge import factor_master

        path = self._write([("H283", "JISNﾌﾗﾂﾄ", "1.13", "1.32257", "1.4")])
        found = factor_master.load_factors(path)
        self.assertAlmostEqual(found["H283"], 1.32257)

    def test_使うのは換算係数で負荷係数ではない(self) -> None:
        """**VBA は負荷係数の列を読み込むだけで使っていない。**"""
        from nippou.access_bridge import factor_master

        path = self._write([("H283", "x", "1.13", "1.32257", "1.4")])
        found = factor_master.load_factors(path)
        self.assertNotAlmostEqual(found["H283"], 1.13)

    def test_同じ用途コードは先に出たほう(self) -> None:
        from nippou.access_bridge import factor_master

        path = self._write([("H283", "x", "", "1.0", ""),
                            ("H283", "y", "", "2.0", "")])
        self.assertEqual(factor_master.load_factors(path)["H283"], 1.0)

    def test_数字でない行は飛ばす(self) -> None:
        from nippou.access_bridge import factor_master

        path = self._write([("H283", "x", "", "", ""),
                            ("H197", "y", "", "0.6", "")])
        found = factor_master.load_factors(path)
        self.assertNotIn("H283", found)
        self.assertIn("H197", found)

    def test_ファイルが無ければ空(self) -> None:
        """係数が出ないだけ。日報の入力は止めない。"""
        from nippou.access_bridge import factor_master

        self.assertEqual(factor_master.load_factors(self.tmp / "無い.sqlite3"), {})


# ======================================================================
# サービス(DBを読み書きする)
# ======================================================================
class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        from nippou.db.connection import connect
        from nippou.db.repository import NippouRepository
        from nippou.db.schema import ensure_schema

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        conn = connect(Path(self._tmp.name) / "t.sqlite3")
        ensure_schema(conn)
        self.addCleanup(conn.close)
        self.repo = NippouRepository(conn)

    def save(self, shift: str, rows, *, page: int = 1, day: str = DAY,
             line: str = LINE) -> None:
        self.repo.save(
            HeaderRecord(report_date=day, line=line, shift=shift, page=page),
            rows)

    def test_保存した直を数え直して書き戻す(self) -> None:
        from nippou.services import load_factor as service

        self.save("1直", [row(1, lot="A", usage="H283", shift="1直"),
                          row(2, lot="B", usage="H197", shift="1直")])
        found = service.recalculate(self.repo, DAY, LINE, "1直", table=FACTORS)
        self.assertTrue(found.applied)
        header, details = self.repo.load(DAY, LINE, "1直", 1)
        self.assertEqual([d.keisu for d in details], ["1.4", "0.6"])
        self.assertEqual(header.lot_count, "2")
        self.assertEqual(header.coefficient_lot_count, "2.0")

    def test_前の直から続くロットは数えない(self) -> None:
        """利用者の例: 1直でA未完 → 2直でA完了。合計は1回ぶん。"""
        from nippou.services import load_factor as service

        self.save("1直", [row(1, lot="A", usage="H283", shift="1直")])
        service.recalculate(self.repo, DAY, LINE, "1直", table=FACTORS)

        self.save("2直", [row(1, lot="A", usage="H283", shift="2直"),
                          row(2, lot="C", usage="H197", shift="2直")])
        service.recalculate(self.repo, DAY, LINE, "2直", table=FACTORS)

        first = self.repo.load(DAY, LINE, "1直", 1)[0]
        second = self.repo.load(DAY, LINE, "2直", 1)[0]
        self.assertEqual(first.coefficient_lot_count, "1.4")
        self.assertEqual(second.coefficient_lot_count, "0.6")   # Aは入らない
        self.assertEqual(second.lot_count, "1")

    def test_ページが2つでも合計は直ぶん(self) -> None:
        from nippou.services import load_factor as service

        self.save("1直", [row(12, page=1, lot="A", usage="H283", shift="1直")],
                  page=1)
        self.save("1直", [row(1, page=2, lot="A", usage="H283", shift="1直"),
                          row(2, page=2, lot="D", usage="H197", shift="1直")],
                  page=2)
        service.recalculate(self.repo, DAY, LINE, "1直", table=FACTORS)
        for page in (1, 2):
            header, _ = self.repo.load(DAY, LINE, "1直", page)
            # **どのページにも同じ合計**を入れる(1枚だけ見て読み違えない)
            self.assertEqual(header.coefficient_lot_count, "2.0")
            self.assertEqual(header.lot_count, "2")

    def test_AIMはﾛｯﾄ数だけ書き戻す(self) -> None:
        from nippou.services import load_factor as service

        self.repo.save(HeaderRecord(report_date=DAY, line="AIM", shift="1直", page=1,
                                    coefficient_lot_count="3.5"),
                       [row(1, lot="A", usage="H283", line="AIM"),
                        row(2, lot="B", usage="H197", line="AIM")])
        found = service.recalculate(self.repo, DAY, "AIM", "1直", table=FACTORS)
        self.assertTrue(found.applied)
        header, details = self.repo.load(DAY, "AIM", "1直", 1)
        self.assertEqual(header.lot_count, "2")
        self.assertEqual(header.coefficient_lot_count, "3.5")     # 打った値のまま
        self.assertEqual([d.keisu for d in details], ["", ""])

    def test_板のラインでは書き換えない(self) -> None:
        from nippou.services import load_factor as service

        self.save("1直", [row(1, lot="A", usage="H283", shift="1直",
                              line="HVC")], line="HVC")
        found = service.recalculate(self.repo, DAY, "HVC", "1直", table=FACTORS)
        self.assertFalse(found.applied)
        header, details = self.repo.load(DAY, "HVC", "1直", 1)
        self.assertEqual(details[0].keisu, "")
        self.assertEqual(header.lot_count, "")

    def test_前の直は同じラインだけ見る(self) -> None:
        from nippou.services import load_factor as service

        self.save("1直", [row(1, lot="A", usage="H283", shift="1直",
                              line="NS1")], line="NS1")
        self.save("2直", [row(1, lot="A", usage="H283", shift="2直")])
        service.recalculate(self.repo, DAY, LINE, "2直", table=FACTORS)
        header, _ = self.repo.load(DAY, LINE, "2直", 1)
        self.assertEqual(header.coefficient_lot_count, "1.4")

    def test_遡る順は時間の順(self) -> None:
        from nippou.services import load_factor as service

        self.save("1直", [row(1, lot="A", shift="1直")])
        self.save("2直", [row(1, lot="B", shift="2直")])
        pages = service.previous_pages(self.repo, DAY, LINE, "3直")
        self.assertEqual([p[0].lot for p in pages], ["B", "A"])

    def test_前の日まで遡る(self) -> None:
        from nippou.services import load_factor as service

        self.save("3直", [row(1, lot="A", shift="3直", day="2026年8月30日")],
                  day="2026年8月30日")
        pages = service.previous_pages(self.repo, DAY, LINE, "1直")
        self.assertEqual([p[0].lot for p in pages], ["A"])

    def test_保存が無ければ何も起きない(self) -> None:
        from nippou.services import load_factor as service

        found = service.recalculate(self.repo, DAY, LINE, "1直", table=FACTORS)
        self.assertEqual(found.written, 0)


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class ScreenTests(WebTestCase):
    """画面まで通す。"""

    def _use_line(self, line: str) -> None:
        # ラインを変えるのは管理者モードだけ(据え付けの設定)
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        self.post("/api/entry/line", {"line": line})

    def test_コイルのラインでは合計欄が読み取り専用(self) -> None:
        self._use_line("機側")
        body = self.get("/").get_data(as_text=True)
        head = body.split('data-header="lot_count"')[1][:200]
        self.assertIn("readonly", head)
        self.assertIn("負荷係数", body)

    def test_板のラインでは手入力のまま(self) -> None:
        self._use_line("HVC")
        body = self.get("/").get_data(as_text=True)
        head = body.split('data-header="lot_count"')[1][:200]
        self.assertNotIn("readonly", head)
        self.assertIn("手入力", body)

    def test_保存すると合計が入る(self) -> None:
        self._use_line("機側")
        payload = {"rows": {"1": {"LOT": "H5422S0", "others1": "H283"}}}
        body = self.post("/api/entry/save", payload).get_json()
        # マスタが無いので係数は 1.0(登録が無ければ1)
        self.assertEqual(body["header"]["lot_count"], "1")
        self.assertEqual(body["header"]["coefficient_lot_count"], "1.0")
        self.assertTrue(body["load_factor"]["applied"])

    def test_AIMはﾛｯﾄ数だけ入り_係数Lot数は打った値(self) -> None:
        self._use_line("AIM")
        page = self.get("/").get_data(as_text=True)
        self.assertIn("readonly", page.split('data-header="lot_count"')[1][:200])
        self.assertNotIn("readonly", page.split('data-header="coefficient_lot_count"')[1][:200])
        self.assertIn("<b>係数Lot数</b>は手入力です", page)
        payload = {"rows": {"1": {"LOT": "H5422S0", "others1": "H283"}},
                   "header": {"coefficient_lot_count": "2.5"}}
        body = self.post("/api/entry/save", payload).get_json()
        self.assertEqual(body["header"]["lot_count"], "1")
        self.assertEqual(body["header"]["coefficient_lot_count"], "2.5")
        self.assertTrue(body["load_factor"]["applied"])

    def stale_totals(self, line: str) -> dict:
        """保存したあと、**保存の前の画面の値**(ﾛｯﾄ数が空)で応答を作らせる。"""
        self._use_line(line)
        saved = self.post("/api/entry/save", {
            "rows": {"1": {"LOT": "H5422S0", "others1": "H283"}},
            "header": {"coefficient_lot_count": "2.5"}}).get_json()
        self.assertEqual(saved["header"]["lot_count"], "1", saved.get("message"))
        return self.post("/api/entry/state", {
            "rows": {"1": {"LOT": "H5422S0", "others1": "H283"}},
            "header": {"lot_count": "", "coefficient_lot_count": "2.5"}}).get_json()["header"]

    def test_古い画面の値で計算した合計欄を消さない_機側(self) -> None:
        """保存の直前に出た自動保存の応答が、あとから届いても空で塗り直さない。"""
        header = self.stale_totals("機側")
        self.assertEqual((header["lot_count"], header["coefficient_lot_count"]), ("1", "1.0"))

    def test_古い画面の値で計算した合計欄を消さない_AIM(self) -> None:
        """AIM は ﾛｯﾄ数だけ保存済みの値。係数Lot数は打った値のまま。"""
        header = self.stale_totals("AIM")
        self.assertEqual((header["lot_count"], header["coefficient_lot_count"]), ("1", "2.5"))

    def test_板のラインでは触らない(self) -> None:
        self._use_line("HVC")
        payload = {"rows": {"1": {"LOT": "H5422S0", "others1": "H283"}}}
        body = self.post("/api/entry/save", payload).get_json()
        self.assertIsNone(body["load_factor"])
        self.assertEqual(body["header"]["lot_count"], "")


if __name__ == "__main__":
    unittest.main()
