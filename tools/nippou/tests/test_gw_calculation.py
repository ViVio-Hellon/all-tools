import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic.gw_calculation import (
    PER_PACK_COLUMNS,
    InvalidPackHeightError,
    MaterialFlags,
    MaterialRate,
    MaterialRates,
    MaterialWeights,
    PackingDimensions,
    VcFilmSelection,
    angle_weight,
    band_weight,
    compute_material_weights,
    dunplate_weight,
    gross_weight,
    hardboard_weight,
    interleaf_weight,
    ex2_band_problem,
    outer_paper_weight,
    per_pack_row,
    per_pack_table,
    poly_sheet_weight,
    stack_count,
    total_packing_weight,
    unregistered_material_problems,
    unregistered_vc_problem,
    validate_inputs,
    validate_pack_height,
    vc_film_weight,
)

UNIT_RATE = MaterialRate(unit_mass=1.0, coefficient=1.0)

# 板厚1.0mm 板幅1000mm 板丈2000mm 枚数10枚、縦横バンド各2本、梱包高さ50mm、
# 1山積み。単位質量・係数はいずれも1.0にして、計算式そのものを手計算で
# 検証しやすくしてある。
DIMS = PackingDimensions(
    thickness_mm=1.0, width_mm=1000.0, length_mm=2000.0, count=10.0,
    vertical_bands=2.0, horizontal_bands=2.0, pack_height_mm=50.0,
    pallet_weight_kg=30.0, stack_pattern=1,
)


class StackCountTests(unittest.TestCase):
    def test_single_stack_uses_full_count(self) -> None:
        dims = PackingDimensions(1, 1, 1, 10, 1, 1, 100, 0, stack_pattern=1)
        self.assertEqual(stack_count(dims), 10)

    def test_double_stack_rounds_up(self) -> None:
        dims = PackingDimensions(1, 1, 1, 11, 1, 1, 100, 0, stack_pattern=2)
        self.assertEqual(stack_count(dims), 6)  # ceil(11/2)

    def test_triple_stack_rounds_up(self) -> None:
        dims = PackingDimensions(1, 1, 1, 10, 1, 1, 100, 0, stack_pattern=3)
        self.assertEqual(stack_count(dims), 4)  # ceil(10/3)


class ValidationTests(unittest.TestCase):
    def test_raises_when_pack_height_too_small(self) -> None:
        dims = PackingDimensions(2, 100, 100, 10, 1, 1, pack_height_mm=15, pallet_weight_kg=0)
        # 梱包高さ15 <= 板厚2 x 枚数10=20 -> 不正
        with self.assertRaises(InvalidPackHeightError):
            validate_pack_height(dims)

    def test_passes_when_pack_height_sufficient(self) -> None:
        dims = PackingDimensions(2, 100, 100, 10, 1, 1, pack_height_mm=25, pallet_weight_kg=0)
        validate_pack_height(dims)  # 例外が出ないこと

    def test_2山は山の高さと比べる(self) -> None:
        """**VBA は全枚数を掛けていた。** 2山・3山で正しい梱包を断ります。

        2山・板厚1mm・1000枚 → 山の高さ 500mm。梱包高さ 700mm は正しい
        (VBA は 700 <= 1 × 1000 で「梱包高さの数値が不正です」)。
        """
        dims = PackingDimensions(1, 800, 1500, 1000, 2, 4, pack_height_mm=700,
                                 pallet_weight_kg=20, stack_pattern=2)
        validate_pack_height(dims)  # 例外が出ないこと
        self.assertEqual(validate_inputs(dims), [])

    def test_2山でも山より低ければ断る(self) -> None:
        dims = PackingDimensions(1, 800, 1500, 1000, 2, 4, pack_height_mm=500,
                                 pallet_weight_kg=20, stack_pattern=2)
        with self.assertRaises(InvalidPackHeightError):
            validate_pack_height(dims)

    def test_3山は切り上げた積み枚数で見る(self) -> None:
        """1000枚の3山は 334枚積み(切り上げ)。334mm ちょうどは断る。"""
        base = dict(thickness_mm=1, width_mm=800, length_mm=1500, count=1000,
                    vertical_bands=2, horizontal_bands=4,
                    pallet_weight_kg=20, stack_pattern=3)
        with self.assertRaises(InvalidPackHeightError):
            validate_pack_height(PackingDimensions(**base, pack_height_mm=334))
        validate_pack_height(PackingDimensions(**base, pack_height_mm=335))


class MaterialWeightFormulaTests(unittest.TestCase):
    """VBAの計算式をそのまま手計算した値と突き合わせる回帰テスト。"""

    def test_dunplate(self) -> None:
        # (1.0*10) * ((1000*1)+2000) * 2 / 1e6 = 0.06
        self.assertAlmostEqual(dunplate_weight(DIMS, UNIT_RATE), 0.06)

    def test_outer_paper(self) -> None:
        # ((((1000*1)+2000)*2)*(1.0*10) + ((1000*1)*2000)*2) / 1e6 = 4.06
        self.assertAlmostEqual(outer_paper_weight(DIMS, UNIT_RATE), 4.06)

    def test_poly_sheet(self) -> None:
        """側面ぐるり + 天面1面、**各寸法 +200mm**(大きめに包む)。

            幅 1000×1+200=1200 / 丈 2000+200=2200 / 高さ 1.0×10+200=210
            ((1200 + 2200) × 2 × 210 + 1200 × 2200) / 1e6 = 4.068
        """
        self.assertAlmostEqual(poly_sheet_weight(DIMS, UNIT_RATE), 4.068)

    def test_poly_sheet_2山は列を並べた全幅に200を足す(self) -> None:
        dims = replace(DIMS, stack_pattern=2)
        # 幅 1000×2+200=2200 / 丈 2200 / 高さ 1.0×5+200=205
        expected = ((2200 + 2200) * 2 * 205 + 2200 * 2200) / 1e6
        self.assertAlmostEqual(poly_sheet_weight(dims, UNIT_RATE), expected)

    def test_合紙は山が複数でも枚数に1枚足すだけ(self) -> None:
        """**山の数は足さない。** 山が複数でも合紙1枚でまとめることが多い
        (現場に確認済み)。「枚数+山数」に直さないための歯止めです。"""
        one = interleaf_weight(DIMS, UNIT_RATE)
        for pattern in (2, 3):
            dims = replace(DIMS, stack_pattern=pattern)
            with self.subTest(pattern=pattern):
                self.assertAlmostEqual(interleaf_weight(dims, UNIT_RATE), one)

    def test_interleaf(self) -> None:
        # 1000*2000*(10+1) / 1e6 = 22.0
        self.assertAlmostEqual(interleaf_weight(DIMS, UNIT_RATE), 22.0)

    def test_band(self) -> None:
        # BandL=(2000+1.0*10)*2*2=8040, BandW=((1000*1)+50)*2*2=4200
        # (4200+8040)/1000 = 12.24
        self.assertAlmostEqual(band_weight(DIMS, UNIT_RATE), 12.24)

    def test_angle(self) -> None:
        """縦バンドアングルは**個数**。縦バンド1本につき4個(長さではない)。"""
        # 縦バンド2本 × 4個 × 1kg/個 = 8.0
        self.assertAlmostEqual(angle_weight(DIMS, UNIT_RATE), 8.0)
        self.assertAlmostEqual(angle_weight(replace(DIMS, vertical_bands=1), UNIT_RATE), 4.0)
        self.assertAlmostEqual(angle_weight(replace(DIMS, vertical_bands=0), UNIT_RATE), 0.0)

    def test_angle_は板丈に左右されない(self) -> None:
        """以前は「板丈 × 2」の長さ(蓋を使わない頃の全丈アングル)でした。"""
        longer = replace(DIMS, length_mm=DIMS.length_mm * 2)
        self.assertAlmostEqual(angle_weight(longer, UNIT_RATE), angle_weight(DIMS, UNIT_RATE))

    def test_hardboard(self) -> None:
        # (1000*1)*2000*2/1e6 = 4.0
        self.assertAlmostEqual(hardboard_weight(DIMS, UNIT_RATE), 4.0)

    def test_rate_scales_linearly(self) -> None:
        rate = MaterialRate(unit_mass=2.0, coefficient=3.0)
        self.assertAlmostEqual(dunplate_weight(DIMS, rate), 0.06 * 2.0 * 3.0)


class VcFilmWeightTests(unittest.TestCase):
    """VCだけは**係数を掛けない**。

    VBA `NewGW` は VC を `検算VC_A.caption` の値でそのまま計算していて、
    その caption は `ComboBox1_Change` が**単位質量だけ**を入れている。
    `NewGW` の VC の列検索も「管理番号」と「品名」しか拾っておらず、
    係数の列を見ていない。

    係数の列自体は VC重量マスタに**ある**ので、掛けるのが本来の意図だった
    可能性はある。ただ現場が使っている数値はVBAが出したものなので、
    移植では VBA に合わせる(`gw_calculation.vc_film_weight` の説明)。
    ここでは**係数を 1 以外にして**、掛かっていないことを確かめる。
    """

    # 係数を掛けてしまうと 1.5*2.0=3.0 になる。掛けなければ 1.5 のまま
    VC_A_RATE = MaterialRate(unit_mass=1.5, coefficient=2.0)
    VC_B_RATE = MaterialRate(unit_mass=2.5, coefficient=4.0)

    def test_both_sides(self) -> None:
        dims = PackingDimensions(1, 1000, 2000, 10, 1, 1, 100, 0)
        sel = VcFilmSelection(use_vc=True, use_side_a=True, use_side_b=True)
        base = 1000 * 2000 * 10 / 10**6
        expected = base * 1.5 + base * 2.5
        self.assertAlmostEqual(vc_film_weight(dims, sel, self.VC_A_RATE, self.VC_B_RATE), expected)

    def test_side_a_only(self) -> None:
        dims = PackingDimensions(1, 1000, 2000, 10, 1, 1, 100, 0)
        sel = VcFilmSelection(use_vc=True, use_side_a=True, use_side_b=False)
        base = 1000 * 2000 * 10 / 10**6
        self.assertAlmostEqual(vc_film_weight(dims, sel, self.VC_A_RATE, self.VC_B_RATE), base * 1.5)

    def test_side_b_only(self) -> None:
        dims = PackingDimensions(1, 1000, 2000, 10, 1, 1, 100, 0)
        sel = VcFilmSelection(use_vc=True, use_side_a=False, use_side_b=True)
        base = 1000 * 2000 * 10 / 10**6
        self.assertAlmostEqual(vc_film_weight(dims, sel, self.VC_A_RATE, self.VC_B_RATE), base * 2.5)

    def test_係数は掛からない(self) -> None:
        """**この試験が落ちたら、VBAと数値が合わなくなっている。**"""
        dims = PackingDimensions(1, 1000, 2000, 10, 1, 1, 100, 0)
        sel = VcFilmSelection(use_vc=True, use_side_a=True, use_side_b=False)
        base = 1000 * 2000 * 10 / 10**6
        got = vc_film_weight(dims, sel, self.VC_A_RATE, self.VC_B_RATE)
        self.assertNotAlmostEqual(got, base * 1.5 * 2.0)

    def test_vc_not_used_is_zero(self) -> None:
        dims = PackingDimensions(1, 1000, 2000, 10, 1, 1, 100, 0)
        sel = VcFilmSelection(use_vc=False, use_side_a=True, use_side_b=True)
        self.assertEqual(vc_film_weight(dims, sel, self.VC_A_RATE, self.VC_B_RATE), 0.0)

    def test_vc_used_but_no_side_selected_is_zero(self) -> None:
        dims = PackingDimensions(1, 1000, 2000, 10, 1, 1, 100, 0)
        sel = VcFilmSelection(use_vc=True, use_side_a=False, use_side_b=False)
        self.assertEqual(vc_film_weight(dims, sel, self.VC_A_RATE, self.VC_B_RATE), 0.0)


class ComputeMaterialWeightsTests(unittest.TestCase):
    def test_disabled_material_is_zero_even_if_formula_would_be_nonzero(self) -> None:
        flags = MaterialFlags(dunplate=False)
        rates = MaterialRates(dunplate=UNIT_RATE, outer_paper=UNIT_RATE, interleaf=UNIT_RATE,
                               band=UNIT_RATE, poly_sheet=UNIT_RATE, angle=UNIT_RATE, hardboard=UNIT_RATE)
        weights = compute_material_weights(DIMS, flags, rates, VcFilmSelection())
        self.assertEqual(weights.dunplate, 0.0)
        self.assertGreater(weights.outer_paper, 0.0)

    def test_total_sums_all_materials(self) -> None:
        flags = MaterialFlags()
        rates = MaterialRates(dunplate=UNIT_RATE, outer_paper=UNIT_RATE, interleaf=UNIT_RATE,
                               band=UNIT_RATE, poly_sheet=UNIT_RATE, angle=UNIT_RATE, hardboard=UNIT_RATE)
        weights = compute_material_weights(DIMS, flags, rates, VcFilmSelection())
        expected_total = (
            weights.dunplate + weights.outer_paper + weights.interleaf + weights.band
            + weights.poly_sheet + weights.angle + weights.hardboard + weights.vc_film
        )
        self.assertAlmostEqual(weights.total, expected_total)


class UnregisteredMaterialTests(unittest.TestCase):
    """**使う資材がマスタに無ければ計算させない。**

    VBA は見つからないと ``Array("", "")`` が返り、``"" * 数`` の型の
    不一致で止まっていました。移植では一時0kgで通していて、その資材
    ぶん軽いGWが出ていました。
    """

    FULL = MaterialRates(**{key: UNIT_RATE for key in (
        "dunplate", "outer_paper", "interleaf", "band", "poly_sheet",
        "angle", "hardboard")})

    def test_揃っていれば何も言わない(self) -> None:
        self.assertEqual(unregistered_material_problems(MaterialFlags(), self.FULL), [])

    def test_使う資材が0なら断る(self) -> None:
        rates = replace(self.FULL, hardboard=MaterialRate(0.0, 1.0))
        problems = unregistered_material_problems(MaterialFlags(), rates)
        self.assertEqual([p.field for p in problems], ["hardboard"])
        self.assertIn("ハードボード", problems[0].message)

    def test_使わない資材は見ない(self) -> None:
        """チェックを外した資材は0kgで正しい。"""
        rates = replace(self.FULL, hardboard=MaterialRate(0.0, 1.0))
        flags = replace(MaterialFlags(), hardboard=False)
        self.assertEqual(unregistered_material_problems(flags, rates), [])

    def test_バンドはマスタの名前で言う(self) -> None:
        """どの名前で引いて無かったのかが分からないと、登録しようがない。"""
        rates = replace(self.FULL, band=MaterialRate(0.0, 1.0))
        problems = unregistered_material_problems(
            MaterialFlags(), rates, {"band": "帯鉄シール有"})
        self.assertEqual(problems[0].field, "band_kind")
        self.assertIn("帯鉄シール有", problems[0].message)

    def test_全部無ければ全部言う(self) -> None:
        problems = unregistered_material_problems(MaterialFlags(), MaterialRates())
        self.assertEqual(len(problems), 7)


class TareAndGrossWeightTests(unittest.TestCase):
    def test_total_packing_weight_without_combined_load(self) -> None:
        self.assertAlmostEqual(total_packing_weight(pallet_weight_kg=30, material_total_kg=50.06), 80.06)

    def test_total_packing_weight_with_combined_load(self) -> None:
        self.assertAlmostEqual(
            total_packing_weight(pallet_weight_kg=30, material_total_kg=50.06, combined_load_kg=5), 85.06
        )

    def test_gross_weight_none_when_input_missing(self) -> None:
        self.assertIsNone(gross_weight(tare_weight_kg=80.06, input_weight_kg=None))
        self.assertIsNone(gross_weight(tare_weight_kg=80.06, input_weight_kg=0))

    def test_gross_weight_adds_input(self) -> None:
        self.assertAlmostEqual(gross_weight(tare_weight_kg=80.06, input_weight_kg=1200.0), 1280.06)


if __name__ == "__main__":
    unittest.main()


class InputValidationTests(unittest.TestCase):
    """計算前チェック (VBA ``CommandButton2_Click``)。

    **全部まとめて返す。** VBA は1つ見つけるたびに `MsgBox` して
    `Exit Sub` していたので、3か所間違っていると3回押し直すことになった。
    判定そのものは変えていない。
    """

    OK = PackingDimensions(
        thickness_mm=1.0, width_mm=1000.0, length_mm=2000.0, count=10.0,
        vertical_bands=2.0, horizontal_bands=2.0, pack_height_mm=100.0,
        pallet_weight_kg=20.0)

    def _fields(self, dims, **kwargs):
        return {p.field for p in validate_inputs(dims, **kwargs)}

    def test_揃っていれば通る(self) -> None:
        self.assertEqual(validate_inputs(self.OK), [])

    def test_空なら全部まとめて返す(self) -> None:
        empty = PackingDimensions(0, 0, 0, 0, 0, 0, 0, 0)
        fields = self._fields(empty)
        for name in ("thickness_mm", "width_mm", "length_mm", "count",
                     "horizontal_bands", "pack_height_mm", "pallet_weight_kg"):
            self.assertIn(name, fields)

    def test_横バンド1本は断る(self) -> None:
        dims = replace(self.OK, horizontal_bands=1)
        self.assertIn("horizontal_bands", self._fields(dims))

    def test_縦バンドは2本まで(self) -> None:
        """1本は中央・2本は左右対称。**3本目の置き場所は決まっていない。**"""
        for bands in (0, 1, 2):
            # 1本は EX2方向なので横バンド4本にしておく(ここで見たいのは縦)
            dims = replace(self.OK, vertical_bands=bands, horizontal_bands=4)
            with self.subTest(bands=bands):
                self.assertNotIn("vertical_bands", self._fields(dims))
        for bands in (3, 4, 10):
            dims = replace(self.OK, vertical_bands=bands)
            with self.subTest(bands=bands):
                problems = [p for p in validate_inputs(dims)
                            if p.field == "vertical_bands"]
                self.assertEqual(len(problems), 1)
                self.assertIn("2本まで", problems[0].message)

    def test_縦バンドの半端や負は断る(self) -> None:
        for bands in (1.5, -1):
            dims = replace(self.OK, vertical_bands=bands)
            with self.subTest(bands=bands):
                self.assertIn("vertical_bands", self._fields(dims))

    def test_梱包高さが板厚掛ける枚数以下なら断る(self) -> None:
        dims = replace(self.OK, pack_height_mm=10)   # 1.0 * 10 = 10
        self.assertIn("pack_height_mm", self._fields(dims))

    def test_積合せ有りで空なら断る(self) -> None:
        fields = self._fields(self.OK, use_combined_load=True, combined_load_kg=0)
        self.assertIn("combined_load_kg", fields)

    def test_積合せ無しなら空でも通る(self) -> None:
        self.assertEqual(validate_inputs(self.OK, combined_load_kg=0), [])

    def test_VCを使うのに品名が空なら断る(self) -> None:
        sel = VcFilmSelection(use_vc=True, use_side_a=True)
        self.assertIn("vc_name_a", self._fields(self.OK, vc_selection=sel))

    def test_VCの品名が入っていれば通る(self) -> None:
        sel = VcFilmSelection(use_vc=True, use_side_a=True)
        self.assertEqual(validate_inputs(self.OK, vc_selection=sel,
                                         vc_name_a="VC-A"), [])

    def test_VCを使うのに面を選んでいなければ断る(self) -> None:
        sel = VcFilmSelection(use_vc=True)
        self.assertIn("vc_name_a", self._fields(self.OK, vc_selection=sel))


class Ex2BandRuleTests(unittest.TestCase):
    """「EX2方向は横バンド4本」(VBA ``CommandButton2_Click`` 冒頭)。

    縦バンドが1本 = EX2方向。例外が2つあり、**どちらも実在の理由**で
    足されたもの(板幅1220〜1300 / 包装仕様NO 7P0106)。
    """

    EX2 = PackingDimensions(
        thickness_mm=1.0, width_mm=1000.0, length_mm=2000.0, count=10.0,
        vertical_bands=1.0, horizontal_bands=2.0, pack_height_mm=100.0,
        pallet_weight_kg=20.0)

    def test_縦1本で横4本以外は断る(self) -> None:
        problem = ex2_band_problem(self.EX2)
        self.assertIsNotNone(problem)
        self.assertIn("EX2", problem.message)

    def test_横4本なら通る(self) -> None:
        self.assertIsNone(ex2_band_problem(replace(self.EX2, horizontal_bands=4)))

    def test_板幅1220から1300は通る(self) -> None:
        for width in (1220, 1250, 1300):
            self.assertIsNone(ex2_band_problem(replace(self.EX2, width_mm=width)),
                              width)

    def test_範囲の外は断る(self) -> None:
        for width in (1219, 1301):
            self.assertIsNotNone(ex2_band_problem(replace(self.EX2, width_mm=width)),
                                 width)

    def test_包装仕様7P0106は通る(self) -> None:
        # 2024.11.12 ｲｲﾀﾞｹｲｷﾝ(ｶ 向けに足された例外
        self.assertIsNone(ex2_band_problem(self.EX2, pack_spec_no="7P0106"))

    def test_別の包装仕様は断る(self) -> None:
        self.assertIsNotNone(ex2_band_problem(self.EX2, pack_spec_no="7P0107"))

    def test_縦1本でなければ何も言わない(self) -> None:
        # EX2方向でないときの横バンド本数は、ここでは決まらない
        self.assertIsNone(ex2_band_problem(replace(self.EX2, vertical_bands=2)))


class UnregisteredVcTests(unittest.TestCase):
    """「登録されていないVCです」。マスタに無い品名を黙って0で通さない。"""

    def test_VCを使うのに0なら断る(self) -> None:
        sel = VcFilmSelection(use_vc=True, use_side_a=True)
        problem = unregistered_vc_problem(sel, 0.0)
        self.assertIsNotNone(problem)
        self.assertIn("登録されていない", problem.message)

    def test_重量が出ていれば通る(self) -> None:
        sel = VcFilmSelection(use_vc=True, use_side_a=True)
        self.assertIsNone(unregistered_vc_problem(sel, 12.3))

    def test_VCを使わないなら何も言わない(self) -> None:
        self.assertIsNone(unregistered_vc_problem(VcFilmSelection(), 0.0))


class PerPackTableTests(unittest.TestCase):
    """梱包数ごとの重量 (VBA ``RangePaste``)。"""

    WEIGHTS = MaterialWeights(dunplate=1.0, outer_paper=2.0, interleaf=3.0,
                              band=4.0, poly_sheet=5.0, angle=6.0,
                              hardboard=7.0, vc_film=8.0)

    def test_100行返る(self) -> None:
        self.assertEqual(len(per_pack_table(self.WEIGHTS, 20.0)), 100)

    def test_1行目は計算結果そのもの(self) -> None:
        row = per_pack_table(self.WEIGHTS, 20.0)[0]
        self.assertEqual(row[0], 1.0)
        self.assertEqual(row[8], self.WEIGHTS.total)
        self.assertEqual(row[9], self.WEIGHTS.total + 20.0)

    def test_N行目はN倍(self) -> None:
        table = per_pack_table(self.WEIGHTS, 20.0)
        for n in (2, 10, 100):
            for col in range(10):
                self.assertAlmostEqual(table[n - 1][col], table[0][col] * n)

    def test_列の並びはVBAのまま(self) -> None:
        self.assertEqual(PER_PACK_COLUMNS[0], "ダンプレート")
        self.assertEqual(PER_PACK_COLUMNS[7], "VCフィルム")
        self.assertEqual(PER_PACK_COLUMNS[-1], "風袋総重量")
        self.assertEqual(len(PER_PACK_COLUMNS), 10)

    def test_行数を指定できる(self) -> None:
        self.assertEqual(len(per_pack_table(self.WEIGHTS, 20.0, rows=5)), 5)
        # 0や負を渡されても1行は返す(空の表を描かせない)
        self.assertEqual(len(per_pack_table(self.WEIGHTS, 20.0, rows=0)), 1)


class PerPackRowTests(unittest.TestCase):
    """1行だけ欲しいとき (紙が使う)。**表を作らずに出す。**"""

    WEIGHTS = PerPackTableTests.WEIGHTS

    def test_表のN行目と同じ(self) -> None:
        """掛け算は `per_pack_unit` の1か所 ── 表と1行は必ず同じ数字。"""
        table = per_pack_table(self.WEIGHTS, 20.0)
        for n in (1, 2, 12, 100):
            self.assertEqual(per_pack_row(self.WEIGHTS, 20.0, n), table[n - 1])

    def test_0や負でも1梱包ぶん(self) -> None:
        one = per_pack_row(self.WEIGHTS, 20.0, 1)
        self.assertEqual(per_pack_row(self.WEIGHTS, 20.0, 0), one)
        self.assertEqual(per_pack_row(self.WEIGHTS, 20.0, -5), one)

    def test_大きな数でも積み上がらない(self) -> None:
        """**「100000 と入れられたら」への答えの、もう一段下。**

        以前は表を丸ごと作って最後の1行だけ使っていたので、手間が梱包数に
        比例しました。いまは上限(100梱包)で丸めていますが、安全がその
        丸め1枚に乗っている状態でした。ここが O(1) なら、仮に丸めを
        すり抜けても何も積み上がりません。
        """
        row = per_pack_row(self.WEIGHTS, 20.0, 10 ** 9)
        self.assertEqual(len(row), 10)
        self.assertAlmostEqual(row[0], 1.0 * 10 ** 9)
