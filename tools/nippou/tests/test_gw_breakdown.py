"""GW計算の内訳 ── **何を、どう使って、その数になったのか**

VBA の `NewGW` は押すと欄に数字が出るだけで、疑われたときに確かめる手が
**ソースを読む**しかありませんでした。現場でそれはできないので、事実上
「そういうものだ」と受け入れるしかなかった。

ここで守るのは:

    ・4段(式 / 数字を入れた式 / 基準量 / ×単位質量×係数 = 重量)が出ること
    ・**内訳の数が、実際の計算の数と同じ**であること(作り直さない)
    ・使わなかった資材も**消さずに**理由つきで並ぶこと
    ・VCフィルムが「係数を掛けない」ことを画面で言うこと
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import gw_calculation as gw
from nippou.presenters import gw_breakdown as bd


def dims(**values) -> gw.PackingDimensions:
    base = dict(thickness_mm=1.99, width_mm=1000.0, length_mm=2000.0,
                count=40, vertical_bands=2, horizontal_bands=4,
                pack_height_mm=200.0, pallet_weight_kg=25.0,
                stack_pattern=1)
    base.update(values)
    return gw.PackingDimensions(**base)


def rates(value: float = 0.5, coef: float = 1.1) -> gw.MaterialRates:
    one = gw.MaterialRate(unit_mass=value, coefficient=coef)
    return gw.MaterialRates(dunplate=one, outer_paper=one, interleaf=one,
                            band=one, poly_sheet=one, angle=one,
                            hardboard=one)


def build(*, d=None, flags=None, r=None, vc=None,
          vc_a=gw.ZERO_RATE, vc_b=gw.ZERO_RATE, **kwargs):
    d = d or dims()
    flags = flags or gw.MaterialFlags()
    r = r or rates()
    vc = vc or gw.VcFilmSelection()
    weights = gw.compute_material_weights(d, flags, r, vc, vc_a, vc_b)
    tare = gw.total_packing_weight(d.pallet_weight_kg, weights.total,
                                   kwargs.get("combined_load_kg", 0.0)
                                   if kwargs.get("use_combined_load") else 0.0)
    gross = gw.gross_weight(tare, kwargs.get("input_weight_kg"))
    return bd.build(d, flags, r, weights, vc_selection=vc,
                    vc_a_rate=vc_a, vc_b_rate=vc_b,
                    tare_weight_kg=tare, gross_weight_kg=gross, **kwargs), weights


def line_of(built, key):
    return next(x for x in built.lines if x.key == key)


class BaseTests(unittest.TestCase):
    """**基準量は計算そのものから取る。** 内訳のために式を書き直さない。"""

    def test_基準量に単位質量と係数を掛けると重量になる(self) -> None:
        d = dims()
        rate = gw.MaterialRate(unit_mass=0.5, coefficient=1.1)
        pairs = (
            (gw.dunplate_base, gw.dunplate_weight),
            (gw.outer_paper_base, gw.outer_paper_weight),
            (gw.poly_sheet_base, gw.poly_sheet_weight),
            (gw.interleaf_base, gw.interleaf_weight),
            (gw.band_base, gw.band_weight),
            (gw.angle_base, gw.angle_weight),
            (gw.hardboard_base, gw.hardboard_weight),
        )
        for base_fn, weight_fn in pairs:
            with self.subTest(base_fn.__name__):
                self.assertAlmostEqual(
                    base_fn(d) * rate.unit_mass * rate.coefficient,
                    weight_fn(d, rate), places=9)

    def test_ポリシートは各寸法に200足して側面と天面(self) -> None:
        d = dims()
        w, length, h = gw.poly_sheet_sizes(d)
        self.assertEqual((w, length, h), (d.width_mm * d.stack_pattern + 200,
                                          d.length_mm + 200,
                                          d.thickness_mm * gw.stack_count(d) + 200))
        self.assertAlmostEqual(gw.poly_sheet_base(d),
                               ((w + length) * 2 * h + w * length) / 1e6)

    def test_バンドは縦と横の合計(self) -> None:
        d = dims()
        vertical, horizontal = gw.band_lengths(d)
        self.assertAlmostEqual((vertical + horizontal) / 1000,
                               gw.band_base(d), places=9)


class ShapeTests(unittest.TestCase):
    """出てくる形。"""

    def test_資材が8つ並ぶ(self) -> None:
        built, _w = build()
        self.assertEqual(
            [line.key for line in built.lines],
            ["dunplate", "outer_paper", "interleaf", "band", "poly_sheet",
             "angle", "hardboard", "vc_film"])

    def test_4段そろう(self) -> None:
        built, _w = build()
        line = line_of(built, "dunplate")
        self.assertTrue(line.formula)          # ① 式
        self.assertTrue(line.substituted)      # ② 数字を入れた式
        self.assertTrue(line.base)             # ③ 基準量
        self.assertIn("=", line.last_step)     # ④ ×単位質量×係数 = 重量

    def test_数字を入れた式に実際の値が出る(self) -> None:
        """**間違いはたいてい入れた数字のほう。** 見えれば自分で気づける。"""
        built, _w = build(d=dims(thickness_mm=1.99, width_mm=1000.0,
                                 length_mm=2000.0))
        line = line_of(built, "dunplate")
        for text in ("1.99", "1,000", "2,000"):
            self.assertIn(text, line.substituted)

    def test_内訳の重量は計算の重量と同じ(self) -> None:
        """**作り直さない。** 食い違ったら内訳の意味が無い。"""
        built, weights = build()
        for key, value in (("dunplate", weights.dunplate),
                           ("outer_paper", weights.outer_paper),
                           ("interleaf", weights.interleaf),
                           ("band", weights.band),
                           ("poly_sheet", weights.poly_sheet),
                           ("angle", weights.angle),
                           ("hardboard", weights.hardboard)):
            self.assertAlmostEqual(line_of(built, key).weight, value, places=9)

    def test_面積と長さと個数で単位を分ける(self) -> None:
        """単位質量が kg/m² か kg/m か kg/個 かで意味が変わる。"""
        built, _w = build()
        for key in ("dunplate", "outer_paper", "interleaf", "poly_sheet",
                    "hardboard"):
            self.assertEqual(line_of(built, key).base_unit, "m²", key)
        self.assertEqual(line_of(built, "band").base_unit, "m")
        angle = line_of(built, "angle")
        self.assertEqual((angle.base_unit, angle.unit_mass_unit), ("個", "kg/個"))
        self.assertEqual(angle.label, "縦バンドアングル")

    def test_出どころを書く(self) -> None:
        built, _w = build()
        self.assertIn("資材重量マスタ", line_of(built, "dunplate").source)
        self.assertIn("ダンプレート", line_of(built, "dunplate").source)

    def test_バンドは選んだ種別でマスタを引くと書く(self) -> None:
        built, _w = build(band_kind="シール有", band_material="帯鉄シール有")
        line = line_of(built, "band")
        self.assertIn("シール有", line.label)
        self.assertIn("帯鉄シール有", line.source)


class SkippedTests(unittest.TestCase):
    """使わなかった資材も**消さずに**並べる。"""

    def test_チェックOFFの資材は理由つきで残る(self) -> None:
        built, _w = build(flags=gw.MaterialFlags(interleaf=False))
        line = line_of(built, "interleaf")
        self.assertFalse(line.used)
        self.assertEqual(line.weight, 0.0)
        self.assertIn("チェックOFF", line.skipped)
        # 並びは変えない ── 変わると「どれが抜けたのか」が読めない
        self.assertEqual(built.lines.index(line), 2)

    def test_VC未使用もその旨が出る(self) -> None:
        built, _w = build()
        line = line_of(built, "vc_film")
        self.assertFalse(line.used)
        self.assertIn("VC使用がOFF", line.skipped)


class VcTests(unittest.TestCase):
    """VCフィルムだけ**係数を掛けない**。"""

    def test_係数を掛けないと書く(self) -> None:
        built, _w = build(
            vc=gw.VcFilmSelection(use_vc=True, use_side_a=True),
            vc_a=gw.MaterialRate(unit_mass=0.02, coefficient=9.9),
            vc_name_a="V325NW")
        line = line_of(built, "vc_film")
        self.assertTrue(line.used)
        self.assertFalse(line.uses_coefficient)
        self.assertIn("係数は掛けません", line.note)
        self.assertNotIn("係数", line.last_step)

    def test_文言に飾り記号を入れない(self) -> None:
        """そのまま画面に出る文字列。`**` は太字にならず、そう見える。"""
        built, _w = build(
            vc=gw.VcFilmSelection(use_vc=True, use_side_a=True),
            vc_a=gw.MaterialRate(unit_mass=0.02, coefficient=1.0),
            vc_name_a="V325NW")
        for line in built.lines:
            for text in (line.formula, line.substituted, line.note,
                         line.source, line.skipped):
                self.assertNotIn("**", text, line.key)
        for note in built.notes:
            self.assertNotIn("**", note)

    def test_両面なら両方の単位質量が出る(self) -> None:
        built, _w = build(
            vc=gw.VcFilmSelection(use_vc=True, use_side_a=True, use_side_b=True),
            vc_a=gw.MaterialRate(unit_mass=0.02, coefficient=1.0),
            vc_b=gw.MaterialRate(unit_mass=0.03, coefficient=1.0),
            vc_name_a="V325NW", vc_name_b="V400NW")
        line = line_of(built, "vc_film")
        self.assertIn("A面", line.note)
        self.assertIn("B面", line.note)
        self.assertIn("V325NW", line.note)
        self.assertIn("V400NW", line.note)

    def test_内訳の重量は計算の重量と同じ(self) -> None:
        built, weights = build(
            vc=gw.VcFilmSelection(use_vc=True, use_side_a=True),
            vc_a=gw.MaterialRate(unit_mass=0.02, coefficient=1.0),
            vc_name_a="V325NW")
        self.assertAlmostEqual(line_of(built, "vc_film").weight,
                               weights.vc_film, places=9)


class CommonTests(unittest.TestCase):
    """先に決まる値 ── 積み形態・積み枚数。"""

    def test_1山なら枚数そのまま(self) -> None:
        built, _w = build(d=dims(count=40, stack_pattern=1))
        row = next(r for r in built.common if r["label"] == "積み枚数")
        self.assertIn("40", row["value"])
        self.assertIn("そのまま", row["note"])

    def test_2山なら切り上げて等分と書く(self) -> None:
        built, _w = build(d=dims(count=41, stack_pattern=2))
        row = next(r for r in built.common if r["label"] == "積み枚数")
        self.assertIn("21", row["value"])       # ceil(41/2)
        self.assertIn("切り上げ", row["note"])


class TotalTests(unittest.TestCase):
    """合計の積み上げ。"""

    def test_3段になる(self) -> None:
        built, _w = build()
        self.assertEqual([t.label for t in built.totals],
                         ["梱包資材重量(資材計)", "風袋総重量", "GW"])

    def test_風袋はパレットを足すと書く(self) -> None:
        built, weights = build()
        total = built.totals[1]
        self.assertIn("パレット", total.formula)
        self.assertAlmostEqual(total.value, weights.total + 25.0, places=9)

    def test_Aインプットが無ければGWは出ない(self) -> None:
        built, _w = build()
        gross = built.totals[2]
        self.assertIsNone(gross.value)
        self.assertIn("Aインプット重量が空", gross.note)

    def test_Aインプットがあれば足す(self) -> None:
        built, weights = build(input_weight_kg=5000.0)
        gross = built.totals[2]
        self.assertAlmostEqual(gross.value, 25.0 + weights.total + 5000.0,
                               places=9)

    def test_積合せは有りのときだけ足すと書く(self) -> None:
        off, _w = build()
        self.assertIn("積合せは無し", off.totals[1].note)
        on, weights = build(use_combined_load=True, combined_load_kg=300.0)
        self.assertIn("積合せ", on.totals[1].formula)
        self.assertAlmostEqual(on.totals[1].value,
                               25.0 + weights.total + 300.0, places=9)


class NoteTests(unittest.TestCase):
    """読むときの注意。"""

    def test_換算の意味を書く(self) -> None:
        built, _w = build()
        joined = " ".join(built.notes)
        self.assertIn("1,000,000", joined)
        self.assertIn("kg/m", joined)

    def test_単位質量0ならマスタを疑えと書く(self) -> None:
        built, _w = build(r=gw.MaterialRates())   # 全部 ZERO_RATE
        self.assertTrue(any("マスタ" in n and "0" in n for n in built.notes))

    def test_引けていれば余計なことは言わない(self) -> None:
        built, _w = build()
        self.assertFalse(any("単位質量が 0" in n for n in built.notes))


class TextTests(unittest.TestCase):
    """**そのまま送れる形。** 数字を疑われたときに使う。"""

    def test_全部の段が文にも出る(self) -> None:
        built, _w = build(band_kind="シール無", band_material="帯鉄シール無")
        text = built.as_text()
        for word in ("■ 入力", "■ 先に決まる値", "■ 資材ごと", "■ 合計",
                     "式", "数字", "出どころ", "ダンプレート", "風袋総重量"):
            self.assertIn(word, text)

    def test_使わなかった資材も文に出る(self) -> None:
        built, _w = build(flags=gw.MaterialFlags(angle=False))
        self.assertIn("アングル: 0 kg", built.as_text())


class NumTests(unittest.TestCase):
    def test_桁区切りを入れる(self) -> None:
        """1000000 は目で数えない。"""
        self.assertEqual(bd.num(1000000), "1,000,000")

    def test_整数は小数点を出さない(self) -> None:
        self.assertEqual(bd.num(40.0), "40")

    def test_端数は落とさない(self) -> None:
        self.assertEqual(bd.num(1.99), "1.99")


if __name__ == "__main__":
    unittest.main()
