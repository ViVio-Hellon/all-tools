"""GW計算の計算式の点検 (v4.37.3)

    ＧＷ計算で計算処理（計算式）自体がおかしいものはありますか？チェックしてください

8つの資材の式を、**手で計算した数**(電卓で叩ける形)と突き合わせます。単位質量は試験用の
資材重量マスタ(`tests/_gw_master.py`)の値。1山積みと、枚数が割り切れない2山積みの2通り。

点検で見つかった誤り: VCフィルムを A面・B面とも使い、**片方だけ**VC重量マスタに無い品名だと、
その面が 0 kg のまま素通りしていた(合計が 0 のときしか断っていなかった)。
"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import gw_calculation as gw  # noqa: E402
from tests._gw_master import GwWebTestCase  # noqa: E402
from tests._web import HAS_FLASK, SKIP_REASON  # noqa: E402

RATES = gw.MaterialRates(
    dunplate=gw.MaterialRate(0.250, 1), outer_paper=gw.MaterialRate(0.0842, 1),
    interleaf=gw.MaterialRate(0.023, 1), band=gw.MaterialRate(0.077, 1),
    poly_sheet=gw.MaterialRate(0.141, 1), angle=gw.MaterialRate(0.05, 1),
    hardboard=gw.MaterialRate(2.387, 1))
ALL = gw.MaterialFlags()


class HandCalculationTests(unittest.TestCase):
    def check(self, dims: gw.PackingDimensions, expected: dict[str, float]) -> None:
        got = gw.compute_material_weights(dims, ALL, RATES, gw.VcFilmSelection())
        for key, value in expected.items():
            self.assertAlmostEqual(getattr(got, key), value, places=6, msg=key)
        self.assertAlmostEqual(got.total, sum(expected.values()), places=6)

    def test_1山積み(self) -> None:
        # 板厚1 × 幅1250 × 丈2500 を 500 枚、縦バンド2・横バンド4、梱包高さ700(パレット含)
        d = gw.PackingDimensions(1, 1250, 2500, 500, 2, 4, 700, 40, 1)
        h = 1 * 500                                         # 山の高さ 500mm
        self.check(d, {
            # 側面ぐるり: (幅+丈)×2×高さ
            "dunplate": (1250 + 2500) * 2 * h / 1e6 * 0.250,               # 3.75 m²
            # 側面ぐるり + 上下: (幅+丈)×2×高さ + 幅×丈×2
            "outer_paper": ((1250 + 2500) * 2 * h + 1250 * 2500 * 2) / 1e6 * 0.0842,   # 10.0 m²
            # 各寸法+200: (1450+2700)×2×700 + 1450×2700(天面だけ)
            "poly_sheet": ((1450 + 2700) * 2 * 700 + 1450 * 2700) / 1e6 * 0.141,       # 9.725 m²
            # 合紙は 枚数+1 枚
            "interleaf": 1250 * 2500 * 501 / 1e6 * 0.023,                   # 1565.625 m²
            # 縦: (丈+山の高さ)×2×2本 / 横: (幅+梱包高さ)×2×4本 → m
            "band": ((2500 + h) * 2 * 2 + (1250 + 700) * 2 * 4) / 1e3 * 0.077,  # 27.6 m
            # 縦バンド1本につき4個
            "angle": 2 * 4 * 0.05,
            # 上下2枚
            "hardboard": 1250 * 2500 * 2 / 1e6 * 2.387,                     # 6.25 m²
        })

    def test_2山積み_割り切れない枚数(self) -> None:
        # 501 枚を2山 → 1山 251 枚(切り上げ)。幅は 2列ぶん(2500)
        d = gw.PackingDimensions(1, 1250, 2500, 501, 1, 4, 600, 40, 2)
        tumi = math.ceil(501 / 2)
        self.assertEqual(tumi, 251)
        h, w = 1 * tumi, 1250 * 2
        self.check(d, {
            "dunplate": (w + 2500) * 2 * h / 1e6 * 0.250,
            "outer_paper": ((w + 2500) * 2 * h + w * 2500 * 2) / 1e6 * 0.0842,
            "poly_sheet": (((w + 200) + 2700) * 2 * (h + 200) + (w + 200) * 2700) / 1e6 * 0.141,
            "interleaf": 1250 * 2500 * (501 + 1) / 1e6 * 0.023,              # 1枚あたりの面積は1列ぶん
            "band": ((2500 + h) * 2 * 1 + (w + 600) * 2 * 4) / 1e3 * 0.077,
            "angle": 1 * 4 * 0.05,
            "hardboard": w * 2500 * 2 / 1e6 * 2.387,
        })

    def test_VCフィルムは枚数ぶん_面ごと(self) -> None:
        d = gw.PackingDimensions(1, 1250, 2500, 500, 2, 4, 700, 40, 1)
        both = gw.VcFilmSelection(True, True, True)
        kg = gw.vc_film_weight(d, both, gw.MaterialRate(0.05, 1), gw.MaterialRate(0.04, 1))
        self.assertAlmostEqual(kg, 1250 * 2500 * 500 / 1e6 * (0.05 + 0.04), places=6)

    def test_風袋とGW(self) -> None:
        self.assertAlmostEqual(gw.total_packing_weight(40, 55.5, 10), 105.5)
        self.assertAlmostEqual(gw.gross_weight(105.5, 5000), 5105.5)
        self.assertIsNone(gw.gross_weight(105.5, 0))


class VcSideTests(unittest.TestCase):
    """見つかった誤り: A面・B面の片方だけマスタに無いと、その面が 0 kg のまま通っていた。"""

    def test_片方だけ無ければ断る(self) -> None:
        both = gw.VcFilmSelection(True, True, True)
        known, unknown = gw.MaterialRate(0.05, 1), gw.ZERO_RATE
        self.assertIsNone(gw.unregistered_vc_problem(both, 12.3), "合計だけでは見つからない")
        got = gw.unregistered_vc_side_problems(both, known, unknown, "テストVC", "無いVC")
        self.assertEqual([p.field for p in got], ["vc_name_b"])
        self.assertIn("B面", got[0].message)
        self.assertIn("無いVC", got[0].message)

    def test_使わない面は見ない(self) -> None:
        only_a = gw.VcFilmSelection(True, True, False)
        self.assertEqual(gw.unregistered_vc_side_problems(
            only_a, gw.MaterialRate(0.05, 1), gw.ZERO_RATE, "テストVC", ""), [])
        self.assertEqual(gw.unregistered_vc_side_problems(
            gw.VcFilmSelection(), gw.ZERO_RATE, gw.ZERO_RATE), [])


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class VcSideScreenTests(GwWebTestCase):
    def setUp(self) -> None:
        super().setUp()
        from tests.test_web_screens import GwTests
        self.ok = dict(GwTests.OK)

    def test_画面からも_片方だけ無いVCは断る(self) -> None:
        both = {**self.ok, "use_vc": True, "vc_side_a": True, "vc_side_b": True,
                "vc_name_a": "テストVC", "vc_name_b": "無いVC"}
        res = self.post("/api/gw/calculate", both)
        self.assertEqual(res.status_code, 422, res.get_json())
        self.assertEqual([p["field"] for p in res.get_json()["problems"]], ["vc_name_b"])
        ok = self.post("/api/gw/calculate", {**both, "vc_name_b": "テストVC"})
        self.assertEqual(ok.status_code, 200, ok.get_json())
        self.assertGreater(ok.get_json()["weights"]["VCフィルム"], 0)


if __name__ == "__main__":
    unittest.main()
