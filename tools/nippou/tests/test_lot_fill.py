"""ロット番号を打つと行が埋まる (VBA `SQLiteLot検索`)

【何が抜けていたか】
日報入力の LOT 欄は、VBAでは `LOT1_Change`〜`LOT12_Change` から
`SQLiteLot検索` を呼び、7桁そろった時点で**3つのファイルを辿って
その行のほとんどを埋めて**いました。Web版にはこの仕組みが無く、
材・調質も寸法も合紙もＶＣも、全部手で打つことになっていました。

    SIKALOT  ﾛｯﾄ番号 → 材・調質 / 寸法 / 検入枚数 / 合紙 / 用途
          ↓ (ﾛｯﾄ番号)
    SIKAHIKI ﾛｯﾄ番号 → 受注番号          ← ここが唯一の橋
          ↓ (受注番号)
    SIKAODR  受注番号 → ＶＣ / 単重 / 合紙 / 包装仕様NO / EX

ここは書式と判定を守ります。紙の実物(2026年8月31日3直 HVC)の
1行目と同じ文字になることを、実際の値で確かめています。
"""
from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import lot_fill  # noqa: E402


@dataclass
class FakeLot:
    """`access_bridge.gw_master.LotInfo` の、式に要るところだけ。"""

    material: str = "52S"
    temper: str = "R"
    thickness_mm: float = 20.0
    width_mm: float = 1528.0
    length_mm: float = 3053.0
    box_sheets: str = "9"
    interleaf_flag: str = ""
    usage_code: str = "H283"
    usage_name: str = "JISNﾌﾗﾂﾄANF"
    delivery: str = "ﾅﾒｶﾜｱﾙﾐ(ｶ"
    course: str = ""


@dataclass
class FakeOrder:
    vc_front: str = "VE-20NF"
    vc_back: str = "VE-20NF"
    unit_weight_kg: float = 250.04314
    pack_spec_no: str = "1P1186"
    interleaf_flag: str = ""
    export_flag: str = ""


@dataclass
class FakeAllocation:
    order_no: str = "AB2234"
    hiki_no: str = "60717001"


class FormatTests(unittest.TestCase):
    """紙に出る文字。**書式が変わると紙が変わる。**"""

    def test_材調質はハイフンのうしろに空白(self) -> None:
        """VBA `製造材質 & "-" & " " & 製造調質`。紙の実物は「52S- R」。"""
        self.assertEqual(lot_fill.format_zai("52S", "R"), "52S- R")
        self.assertEqual(lot_fill.format_zai("F52S", "R"), "F52S- R")

    def test_調質が無ければ後ろの空白は落とす(self) -> None:
        self.assertEqual(lot_fill.format_zai("52S", ""), "52S-")

    def test_寸法は厚3桁_幅丈1桁(self) -> None:
        """紙の実物は「20.000×1528.0×3053.0」。"""
        self.assertEqual(lot_fill.format_siz(20.0, 1528.0, 3053.0),
                         "20.000×1528.0×3053.0")
        self.assertEqual(lot_fill.format_siz(130.0, 1529.0, 3054.0),
                         "130.000×1529.0×3054.0")

    def test_寸法の端数はVBAと同じく0_5を切り上げる(self) -> None:
        """Python の書式指定では 1528.25 → 1528.2、0.0625 → 0.062 になる。"""
        self.assertEqual(lot_fill.format_siz(0.0625, 1528.25, 3053.75),
                         "0.063×1528.3×3053.8")

    def test_機側とNS1で丈0ならコイル(self) -> None:
        for line in ("機側", "NS1"):
            with self.subTest(line=line):
                self.assertEqual(lot_fill.format_siz(2.0, 1000.0, 0.0, line=line),
                                 "2.000×1000.0×ｺｲﾙ")

    def test_AIMはコイルにしない(self) -> None:
        """VBA の `Select Case` に AIM は無い。丈0でも数字を書く。"""
        self.assertEqual(lot_fill.format_siz(2.0, 1000.0, 0.0, line="AIM"),
                         "2.000×1000.0×0.0")

    def test_丈があればコイルにしない(self) -> None:
        self.assertEqual(lot_fill.format_siz(2.0, 1000.0, 500.0, line="機側"),
                         "2.000×1000.0×500.0")

    def test_VCは表裏の有無で4通り(self) -> None:
        """紙の実物は「A:VE-20NF_B:VE-20NF」「A:V325NW」。"""
        self.assertEqual(lot_fill.format_vc("VE-20NF", "VE-20NF"),
                         "A:VE-20NF_B:VE-20NF")
        self.assertEqual(lot_fill.format_vc("V325NW", ""), "A:V325NW")
        self.assertEqual(lot_fill.format_vc("", "V325NW"), "B:V325NW")
        self.assertEqual(lot_fill.format_vc("", ""), "")


class InterleafTests(unittest.TestCase):
    """合紙。**出どころが2つある。**"""

    def test_1なら有_0なら無(self) -> None:
        self.assertEqual(lot_fill.interleaf("1"), "有")
        self.assertEqual(lot_fill.interleaf("0"), "無")

    def test_それ以外は決めない(self) -> None:
        for text in ("", " ", "x", None):
            with self.subTest(text=text):
                self.assertEqual(lot_fill.interleaf(text), "")

    def test_受注が先_ロットが控え(self) -> None:
        """現場は「SIKAODR の合紙を参照」。VBAの日報入力は
        SIKALOT の `梱包_合紙` を見ていたので、そちらは控えにする。"""
        self.assertEqual(lot_fill.interleaf_of("1", "0"), "有")
        self.assertEqual(lot_fill.interleaf_of("", "1"), "有")
        self.assertEqual(lot_fill.interleaf_of("", ""), "")


class UnitWeightTests(unittest.TestCase):
    def test_単重はそのまま(self) -> None:
        self.assertEqual(lot_fill.unit_weight("250.04314"), "250.04314")

    def test_コイルのラインには入れない(self) -> None:
        """VBA `Case UFdaily.LS: UFdaily("UNI" & j) = ""`。"""
        for line in ("機側", "NS1"):
            with self.subTest(line=line):
                self.assertEqual(lot_fill.unit_weight("250.0", line=line), "")

    def test_数字でなければ空(self) -> None:
        self.assertEqual(lot_fill.unit_weight("---"), "")


class LengthTests(unittest.TestCase):
    def test_7桁ちょうどで引く(self) -> None:
        self.assertIsNone(lot_fill.check_length("H5422S0"))

    def test_足りなければ黙って待つ(self) -> None:
        """VBA も `Exit Sub` するだけで何も言わない ── 打っている途中に
        毎回「短い」と出ると打ち終われない。"""
        refusal = lot_fill.check_length("H542")
        self.assertIsNotNone(refusal)
        self.assertEqual(refusal.reason, lot_fill.REFUSE_SHORT)
        self.assertEqual(refusal.message, "")

    def test_長すぎても引かない(self) -> None:
        self.assertIsNotNone(lot_fill.check_length("H5422S012"))


class BuildTests(unittest.TestCase):
    """紙の1行目(H5422S0)と同じ文字になるか。"""

    def build(self, **kwargs):
        lot = kwargs.pop("lot", FakeLot())
        order = kwargs.pop("order", FakeOrder())
        allocation = kwargs.pop("allocation", FakeAllocation())
        return lot_fill.build(line=kwargs.pop("line", "HVC"), lot=lot,
                              allocation=allocation, order=order)

    def test_紙の1行目と同じになる(self) -> None:
        values = self.build().values
        self.assertEqual(values["ZAI"], "52S- R")
        self.assertEqual(values["SIZ"], "20.000×1528.0×3053.0")
        self.assertEqual(values["KEN"], "9")
        self.assertEqual(values["VC"], "A:VE-20NF_B:VE-20NF")
        self.assertEqual(values["UNI"], "250.04314")

    def test_印刷範囲外の6つも埋まる(self) -> None:
        """紙の AN〜AS 列(用途コード/用途名/納入先/包装仕様書No/…)。"""
        values = self.build().values
        self.assertEqual(values["others1"], "H283")
        self.assertEqual(values["others2"], "JISNﾌﾗﾂﾄANF")
        self.assertEqual(values["others3"], "ﾅﾒｶﾜｱﾙﾐ(ｶ")
        self.assertEqual(values["others4"], "1P1186")

    def test_打ち直すと前の行の値は消える(self) -> None:
        """VBA も先頭で一括クリアしていた。**LOT自身は消さない。**"""
        self.assertNotIn("LOT", lot_fill.CLEARED_FAMILIES)
        for family in ("ZAI", "SIZ", "KEN", "VC", "ET", "CON", "WEI", "TIM"):
            with self.subTest(family=family):
                self.assertIn(family, lot_fill.CLEARED_FAMILIES)

    def test_受注が引けなくても止まらない(self) -> None:
        """VBA も「VC/単重/包装仕様は空のまま続行」していた。"""
        values = self.build(order=None).values
        self.assertEqual(values["ZAI"], "52S- R")
        self.assertEqual(values["VC"], "")
        self.assertEqual(values["UNI"], "")

    def test_合紙は決まったときだけ書く(self) -> None:
        """空で上書きすると、作業者が選んだ「有」が打ち直すたび消える。"""
        self.assertNotIn("AI", self.build().values)
        filled = self.build(order=FakeOrder(interleaf_flag="1")).values
        self.assertEqual(filled["AI"], "有")

    def test_ロット側の合紙も拾う(self) -> None:
        filled = self.build(lot=FakeLot(interleaf_flag="0"),
                            order=FakeOrder()).values
        self.assertEqual(filled["AI"], "無")

    def test_受注番号3桁目が1ならEXが立つ(self) -> None:
        fill = self.build(allocation=FakeAllocation(order_no="AB1234"))
        self.assertIn("ex", fill.auto_marks)
        self.assertIn("EX", fill.values["ET"])

    def test_輸出区分でもEXが立つ(self) -> None:
        fill = self.build(order=FakeOrder(export_flag="1"))
        self.assertIn("ex", fill.auto_marks)

    def test_設備コースがGFSなら外注が立つ(self) -> None:
        fill = self.build(lot=FakeLot(course="A-GCT-B"))
        self.assertIn("outsource", fill.auto_marks)
        self.assertIn("外注出荷", fill.values["ET"])

    def test_どちらでもなければ印は付かない(self) -> None:
        fill = self.build()
        self.assertEqual(fill.auto_marks, [])
        self.assertEqual(fill.values["ET"], "")

    def test_使った引当を持ち帰る(self) -> None:
        fill = self.build()
        self.assertEqual(fill.order_no, "AB2234")
        self.assertEqual(fill.hiki_no, "60717001")



class SheetCountTests(unittest.TestCase):
    """検入枚数は**整数**(v4.18.0 ──「検入枚数は整数です」)。"""

    def test_実数で来ても整数で書く(self) -> None:
        for raw, want in (("2874.0", "2874"), ("2874", "2874"), (2874.0, "2874"),
                          ("１２", "12"), ("12.6", "13"), ("12.5", "13"), ("12.4", "12"),
                          ("", ""), (None, ""),
                          ("不明", "不明")):
            with self.subTest(raw=raw):
                self.assertEqual(lot_fill.sheet_count(raw), want)


if __name__ == "__main__":
    unittest.main()
