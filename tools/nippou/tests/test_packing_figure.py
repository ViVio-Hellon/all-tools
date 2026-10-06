"""梱包図 (VBA ``図形展開``)

【何のための図か】
使い方を聞いたところ「**輸出梱包の完成確認程度**」「印刷不要」でした。
出来上がった梱包を見て合っているか確かめるものなので、要るのは
「どんな形か / バンドは何本どこを通るか / 縦バンドアングルは付くか /
パレットの脚は何本か」です。

だからここで見るのも**そこ**です ── 箱の座標が1つずつVBAと同じか、では
なく、**数えるものが数どおりに出ているか**。

中身(`中身`)と寸法矢印(`分析矢印`)は移植していません。合紙やポリシートは
梱包すると見えないので、完成確認の役に立ちません。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import packing_figure as fig
from tests._gw_master import GwWebTestCase
from tests._web import WebTestCase

#: 通る入力ひとそろい(1山・横4本・縦2本・縦バンドアングル有)
BASE = dict(thickness_mm=1.99, width_mm=810.0, length_mm=1500.0, count=40.0,
            horizontal_bands=4.0, vertical_bands=2.0, pack_height_mm=300.0,
            columns=1, use_angle=True)


def _kinds(figure: fig.Figure, kind: str) -> list[fig.Box]:
    return [b for b in figure.boxes if b.kind == kind]


def _labels(figure: fig.Figure, label: str) -> list[fig.Box]:
    return [b for b in figure.boxes if b.label == label]


class ShapeTests(unittest.TestCase):
    """外形。**cm で持つ**(画面の大きさは画面が決める)。"""

    def test_描ける(self) -> None:
        self.assertTrue(fig.build(**BASE).drawable)

    def test_外寸はmmを10で割ったもの(self) -> None:
        """VBA も cm(mm ÷ 10)で組み立てていた。"""
        f = fig.build(**BASE)
        self.assertAlmostEqual(f.width_cm, 81.0)
        self.assertAlmostEqual(f.depth_cm, 150.0)
        # 高さ = 積み枚数 × 板厚 = 40 × 0.199
        self.assertAlmostEqual(f.height_cm, 7.96, places=2)

    def test_2山なら幅が倍で高さが半分(self) -> None:
        """列を横に並べるので、同じ枚数なら幅が増えて山が低くなる。"""
        one = fig.build(**BASE)
        two = fig.build(**{**BASE, "columns": 2})
        self.assertAlmostEqual(two.width_cm, one.width_cm * 2)
        # 40枚を2列 → 1列20枚
        self.assertAlmostEqual(two.height_cm, 20 * 0.199, places=3)

    def test_積み枚数は切り上げ(self) -> None:
        """VBA `Application.RoundUp(MAI / retu, 0)`。端数は上の段へ。"""
        f = fig.build(**{**BASE, "count": 41.0, "columns": 2})
        self.assertAlmostEqual(f.height_cm, 21 * 0.199, places=3)

    def test_製品は1つ(self) -> None:
        """**図の主役。** 板の山をまとめて1つの箱で出す(VBAも同じ)。"""
        self.assertEqual(len(_kinds(fig.build(**BASE), fig.KIND_PRODUCT)), 1)


class CountTests(unittest.TestCase):
    """**数えるものが数どおりに出ているか。** 完成確認で見るのはここ。"""

    def test_横バンドの本数(self) -> None:
        """上を走るぶんと、側面を降りるぶんが同じ数だけ出る。"""
        for bands in (2, 3, 4, 6):
            f = fig.build(**{**BASE, "horizontal_bands": float(bands)})
            self.assertEqual(len(_labels(f, "横バンド(上)")), bands, bands)
            self.assertEqual(len(_labels(f, "横バンド(横)")), bands, bands)

    def test_縦バンドの本数(self) -> None:
        for bands in (1, 2):
            f = fig.build(**{**BASE, "vertical_bands": float(bands)})
            self.assertEqual(len(_labels(f, "縦バンド(天面)")), bands, bands)
            self.assertEqual(len(_labels(f, "縦バンド(端面)")), bands, bands)

    def test_縦バンドは天面も走る(self) -> None:
        """**端面にしか掛かっていない帯にしない。**

        縦バンドは天面を手前から奥へ走り、端面を降りて締まります。VBA も
        2つの部材(`縦バンド◯上` と `縦バンド◯手前`)で描いていて、天面の
        ぶんは奥行きいっぱい(`Depth = TAKE`)です ── ここを落とすと、
        端面だけに帯が立っている図になります。
        """
        f = fig.build(**BASE)
        for top in _labels(f, "縦バンド(天面)"):
            # 天面のぶんは**奥行きいっぱい**
            self.assertAlmostEqual(top.depth, f.depth_cm)
            self.assertAlmostEqual(top.h, 1.0)
        for face in _labels(f, "縦バンド(端面)"):
            # 端面のぶんは薄く、製品の高さぶん降りる
            self.assertAlmostEqual(face.depth, 1.0)
            self.assertGreater(face.h, f.height_cm)

    def test_横バンドの厚みはVBAどおり(self) -> None:
        """VBA の `ThreeD.Depth = 2 * v`。薄すぎると図で見えない。"""
        f = fig.build(**BASE)
        for band in _labels(f, "横バンド(上)") + _labels(f, "横バンド(横)"):
            self.assertAlmostEqual(band.depth, 2.0)

    def test_蓋はいつも上に載る(self) -> None:
        """いまの梱包は蓋しか使わない(VBA の合板・全丈アングルは描かない)。"""
        for bands in (0.0, 1.0, 2.0):
            f = fig.build(**{**BASE, "vertical_bands": bands})
            lids = _kinds(f, fig.KIND_LID)
            self.assertEqual(len(lids), 1, bands)
            top_board = _labels(f, "ハードボード(上)")[0]
            self.assertAlmostEqual(lids[0].y + lids[0].h, top_board.y)
            self.assertFalse(hasattr(fig, "KIND_PLYWOOD"))

    # ---- 蓋は板と桟(v3.99.0) ------------------------------------
    #
    #     あれー蓋描写しないね
    #
    # 前は蓋を板1枚(パレットと同じ色の平らな箱)で描いていて、蓋に見えなかった。
    # VBA の梱包図どおり、蓋板の上に縦桟と横桟を組む
    def test_蓋は板の上に縦桟と横桟を組む(self) -> None:
        f = fig.build(**BASE)
        lid = _kinds(f, fig.KIND_LID)[0]
        runners = _labels(f, "蓋の縦桟")
        crosses = _labels(f, "蓋の横桟")
        # 縦桟はパレットの角材と同じ本数、手前から奥まで蓋板の上に載る
        timbers = fig.pallet_timbers(BASE["width_mm"], 1, four_way=True) + 1
        self.assertEqual(len(runners), timbers)
        for r in runners:
            self.assertAlmostEqual(r.y + r.h, lid.y)
            self.assertAlmostEqual(r.depth, lid.depth)
            self.assertGreaterEqual(r.x, 0)
            self.assertLessEqual(r.x + r.w, lid.w + 1e-9)
        self.assertAlmostEqual(min(r.x for r in runners), 0)            # 端は蓋板の端
        self.assertAlmostEqual(max(r.x + r.w for r in runners), lid.w)
        # 横桟は横バンドの数だけ、左右にはみ出す
        self.assertEqual(len(crosses), int(BASE["horizontal_bands"]))
        for c in crosses:
            self.assertGreater(c.w, lid.w)
            self.assertEqual(c.kind, fig.KIND_LID_CLEAT)

    def test_横バンドは横桟の上を通る(self) -> None:
        """横桟の上面にちょうど載る(浮かない・めり込まない)。奥行きも横桟の中。"""
        f = fig.build(**BASE)
        bands = sorted(_labels(f, "横バンド(上)"), key=lambda b: b.y)
        crosses = sorted(_labels(f, "蓋の横桟"), key=lambda b: b.y)
        ratio = fig.DEPTH_RATIO
        for band, cross in zip(bands, crosses):
            band_d = (band.x + 2) / ratio            # 手前からの奥行き
            cross_d = (cross.x + fig.CROSS_OVERHANG) / ratio
            self.assertGreaterEqual(band_d, cross_d)
            self.assertLessEqual(band_d + band.depth, cross_d + cross.depth + 1e-9)
            # 上面の高さ: 横桟の上面(その奥行きでの)= 横バンドの下面
            cross_top_at_band = cross.y + (cross_d - band_d) * ratio
            self.assertAlmostEqual(band.y + band.h, cross_top_at_band)

    def test_縦バンドは桟の上を通る(self) -> None:
        f = fig.build(**BASE)
        top = min(b.y for b in _labels(f, "蓋の縦桟"))
        for band in _labels(f, "縦バンド(天面)"):
            self.assertLess(band.y + band.h, top)

    def test_蓋の板と桟は色が違う(self) -> None:
        """板は濃く、桟は明るく ── 平らな1枚に見えないように。色は CSS が持つ。"""
        root = Path(__file__).resolve().parent.parent / "app" / "static" / "css"
        tokens = (root / "tokens.css").read_text(encoding="utf-8")
        components = (root / "components.css").read_text(encoding="utf-8")
        self.assertEqual(tokens.count("--fig-lid:"), 3)     # 明るい + 暗い2か所
        self.assertIn(".fig-box--lid .fig-face{ fill:var(--fig-lid); }", components)
        self.assertIn(".fig-box--lid-cleat .fig-face{ fill:var(--fig-timber); }", components)

    def test_縦バンドアングルは縦バンド1本につき4つ(self) -> None:
        """縦バンドが角を回る4か所(天面・底面の手前と奥)に1つずつ。"""
        for bands in (1.0, 2.0):
            f = fig.build(**{**BASE, "vertical_bands": bands})
            angles = _kinds(f, fig.KIND_ANGLE)
            self.assertEqual(len(angles), int(bands) * 4, bands)
            where = sorted(b.label for b in angles)
            self.assertEqual(where, sorted(
                f"縦バンドアングル({w})" for w in ("天・手前", "天・奥", "底・手前", "底・奥")
                for _ in range(int(bands))))

    def test_縦バンドが無ければ縦バンドアングルも無い(self) -> None:
        f = fig.build(**{**BASE, "vertical_bands": 0.0})
        self.assertEqual(_kinds(f, fig.KIND_ANGLE), [])

    def test_全丈アングルは描かない(self) -> None:
        """丈いっぱいの部材は製品・ハードボード・蓋(板・縦桟)・縦バンド(天面)だけ。"""
        f = fig.build(**BASE)
        for b in _kinds(f, fig.KIND_ANGLE):
            self.assertLess(b.depth, 5, "アングルは角に当てる短い部材")

    def test_アングル無しなら出ない(self) -> None:
        f = fig.build(**{**BASE, "use_angle": False})
        self.assertEqual(len(_kinds(f, fig.KIND_ANGLE)), 0)
        self.assertTrue(f.drawable)

    def test_パレット脚は横バンドと同じ数(self) -> None:
        """VBA の `For i = (z * 2) - 1 To 1 Step -2` は z 回まわる。"""
        for bands in (2, 4, 6):
            f = fig.build(**{**BASE, "horizontal_bands": float(bands)})
            self.assertEqual(len(_labels(f, "パレット脚")), bands, bands)

    def test_ハードボードは上下に1枚ずつ(self) -> None:
        self.assertEqual(len(_kinds(fig.build(**BASE), fig.KIND_HARDBOARD)), 2)


class VerticalBandPlaceTests(unittest.TestCase):
    """縦バンドの**置き場所** ── 1本は中央、2本は左右対称。

        縦バンド１の時は中央　２で写真の位置と反対側に1本追加
        2本以上は受け付けない形でよいです

    以前は1本のときも右寄り(2本のときの右と同じ位置)に立てていました。
    """

    def centers(self, figure: fig.Figure) -> list[float]:
        """縦バンド(天面)の**真ん中**の x。左から並べる。"""
        return sorted(b.x + b.w / 2 for b in _labels(figure, "縦バンド(天面)"))

    def test_1本は中央(self) -> None:
        f = fig.build(**{**BASE, "vertical_bands": 1.0})
        self.assertEqual(len(self.centers(f)), 1)
        self.assertAlmostEqual(self.centers(f)[0], f.width_cm / 2)

    def test_2本は中央を挟んで左右対称(self) -> None:
        f = fig.build(**BASE)
        left, right = self.centers(f)
        self.assertAlmostEqual(left + right, f.width_cm)
        self.assertLess(left, f.width_cm / 2)
        self.assertGreater(right, f.width_cm / 2)

    def test_2本の右はこれまでの位置(self) -> None:
        """**写真の位置は動かさない。** 足すのは反対側の1本だけ。"""
        f = fig.build(**BASE)
        y = fig.pallet_timbers(BASE["width_mm"], 1, four_way=True)
        span = (f.width_cm + 4 - 8) / y
        right = max(b.x for b in _labels(f, "縦バンド(天面)"))
        self.assertAlmostEqual(right, -2 + 8 + 4 + span * (y - 1))

    def test_積み形態や幅が変わっても対称(self) -> None:
        for width in (500.0, 810.0, 1250.0, 1500.0):
            for columns in (1, 2, 3):
                for angle in (True, False):
                    f = fig.build(**{**BASE, "width_mm": width,
                                     "columns": columns, "use_angle": angle})
                    with self.subTest(width=width, columns=columns, angle=angle):
                        left, right = self.centers(f)
                        self.assertAlmostEqual(left + right, f.width_cm)

    def test_1本でも2本でも製品の上に乗る(self) -> None:
        for bands in (1.0, 2.0):
            f = fig.build(**{**BASE, "vertical_bands": bands})
            for box in _labels(f, "縦バンド(天面)"):
                with self.subTest(bands=bands):
                    self.assertGreaterEqual(box.x, 0)
                    self.assertLessEqual(box.x + box.w, f.width_cm)

    def test_0本なら描かない(self) -> None:
        """計算は0本(バンドの長さに入らない)なのに、図に1本立っていました。"""
        f = fig.build(**{**BASE, "vertical_bands": 0.0})
        self.assertTrue(f.drawable)
        self.assertEqual(_labels(f, "縦バンド(天面)"), [])
        self.assertEqual(_labels(f, "縦バンド(端面)"), [])

    def test_3本は描けない(self) -> None:
        f = fig.build(**{**BASE, "vertical_bands": 3.0})
        self.assertFalse(f.drawable)
        self.assertEqual([p.field for p in f.problems], ["vertical_bands"])

    def test_上限は計算側と同じ数(self) -> None:
        """**2か所に書かない。** 図と計算で上限が違うと、片方だけ通ります。"""
        from nippou.logic import gw_calculation

        self.assertIs(fig.MAX_VERTICAL_BANDS, gw_calculation.MAX_VERTICAL_BANDS)

    def test_どこを通るかを字でも言う(self) -> None:
        for bands, text in ((0, "0 本"), (1, "1 本(中央)"), (2, "2 本(左右)")):
            f = fig.build(**{**BASE, "vertical_bands": float(bands)})
            labels = {x["label"]: x["value"] for x in f.facts}
            self.assertEqual(labels["縦バンド"], text)


class BandToneTests(unittest.TestCase):
    """バンドの色 ── **PETバンドは緑、それ以外は青。**

    色そのものは `tokens.css` が持ちます。ここで見るのは「どのバンドが
    どの材質として渡るか」です。
    """

    def bands(self, kind: str) -> list[fig.Box]:
        return _kinds(fig.build(**{**BASE, "band_kind": kind}), fig.KIND_BAND)

    def test_PETバンドはPET(self) -> None:
        tones = {b.tone for b in self.bands("PETバンド")}
        self.assertEqual(tones, {fig.TONE_PET})

    def test_帯鉄は帯鉄(self) -> None:
        for kind in ("シール無", "シール有"):
            with self.subTest(kind=kind):
                self.assertEqual({b.tone for b in self.bands(kind)},
                                 {fig.TONE_STEEL})

    def test_縦も横も同じ材質(self) -> None:
        """1つの梱包を2種類のバンドで締めることはありません。"""
        labels = {b.label for b in self.bands("PETバンド")}
        self.assertEqual(labels, {"縦バンド(天面)", "縦バンド(端面)",
                                  "横バンド(上)", "横バンド(横)"})

    def test_バンドのほかには材質を付けない(self) -> None:
        f = fig.build(**{**BASE, "band_kind": "PETバンド"})
        for box in f.boxes:
            if box.kind != fig.KIND_BAND:
                self.assertEqual(box.tone, "", box.label)

    def test_種別の名前は自動選択と同じもの(self) -> None:
        """**名前を2か所に書かない。** 綴りが1文字違うと青で出ます。"""
        from nippou.logic import gw_autoselect

        self.assertEqual(fig.band_tone(gw_autoselect.BAND_PET), fig.TONE_PET)
        self.assertEqual(fig.band_tone(gw_autoselect.BAND_NO_SEAL), fig.TONE_STEEL)
        self.assertEqual(fig.band_tone(gw_autoselect.BAND_WITH_SEAL), fig.TONE_STEEL)

    def test_色の意味を字でも言う(self) -> None:
        for kind, word in (("PETバンド", "PETバンド(緑)"),
                           ("シール無", "シール無(青)")):
            f = fig.build(**{**BASE, "band_kind": kind})
            labels = {x["label"]: x["value"] for x in f.facts}
            self.assertEqual(labels["バンド種別"], word)

    def test_色はCSSが持つ(self) -> None:
        """明るい画面と暗い画面の両方に、緑と青が用意されていること。"""
        root = Path(__file__).resolve().parent.parent / "app" / "static" / "css"
        tokens = (root / "tokens.css").read_text(encoding="utf-8")
        components = (root / "components.css").read_text(encoding="utf-8")
        for name in ("--fig-band-pet", "--fig-band-steel"):
            # 明るい1か所 + 暗い2か所(OS の設定・画面の切り替え)
            self.assertEqual(tokens.count(f"{name}:"), 3, name)
        self.assertIn(".fig-box--band-pet", components)
        self.assertIn(".fig-box--band-steel", components)


class PalletTimberTests(unittest.TestCase):
    """パレット角材の本数 (VBA の `y`)。"""

    def test_4方向で600から1300は3本(self) -> None:
        for width in (601.0, 810.0, 1299.0):
            self.assertEqual(fig.pallet_timbers(width, 1, four_way=True), 3,
                             width)

    def test_その外は330ごとに切り上げ(self) -> None:
        self.assertEqual(fig.pallet_timbers(600.0, 1, four_way=True), 2)
        self.assertEqual(fig.pallet_timbers(1300.0, 1, four_way=True), 4)
        self.assertEqual(fig.pallet_timbers(1400.0, 1, four_way=True), 5)

    def test_2方向は3本で固定(self) -> None:
        """2方向パレット ── 幅で変わらない。"""
        for width in (400.0, 810.0, 2000.0):
            self.assertEqual(fig.pallet_timbers(width, 1, four_way=False), 3,
                             width)

    def test_列の数だけ増える(self) -> None:
        self.assertEqual(fig.pallet_timbers(810.0, 2, four_way=True), 6)
        self.assertEqual(fig.pallet_timbers(810.0, 3, four_way=True), 9)

    def test_図には角材がy_1本出る(self) -> None:
        """VBA `For i = 0 To y` なので、y ではなく **y+1 本**。"""
        f = fig.build(**BASE)
        y = fig.pallet_timbers(810.0, 1, four_way=True)
        self.assertEqual(len(_labels(f, "パレット角材")), y + 1)


class DrawOrderTests(unittest.TestCase):
    """**並び順は描く順。** 奥から手前へ(後のものが前に出る)。"""

    def test_製品はパレットより後(self) -> None:
        f = fig.build(**BASE)
        kinds = [b.kind for b in f.boxes]
        self.assertLess(kinds.index(fig.KIND_PALLET),
                        kinds.index(fig.KIND_PRODUCT))

    def test_バンドはいちばん後(self) -> None:
        """バンドは外側を通るので、最後に描かないと中に埋もれる。"""
        f = fig.build(**BASE)
        last_band = max(i for i, b in enumerate(f.boxes)
                        if b.kind == fig.KIND_BAND)
        self.assertEqual(last_band, len(f.boxes) - 1)

    def test_手前のアングルは製品より後_底の奥は製品より前(self) -> None:
        """底の奥の角は製品の陰。先に描かないと製品の前に浮いて見える。"""
        f = fig.build(**BASE)
        product = [b.kind for b in f.boxes].index(fig.KIND_PRODUCT)
        for i, b in enumerate(f.boxes):
            if b.kind != fig.KIND_ANGLE:
                continue
            with self.subTest(label=b.label):
                if b.label.endswith("(底・奥)"):
                    self.assertLess(i, product)
                else:
                    self.assertGreater(i, product)


class FactTests(unittest.TestCase):
    """**形だけに頼らない。** 図から読み取るものを字でも出す。"""

    def test_数えるものが字でも出る(self) -> None:
        labels = {f["label"]: f["value"] for f in fig.build(**BASE).facts}
        self.assertEqual(labels["積み形態"], "1山積み")
        self.assertEqual(labels["横バンド"], "4 本")
        self.assertEqual(labels["縦バンド"], "2 本(左右)")
        self.assertEqual(labels["縦バンドアングル"], "8 個(縦バンド 2 本 × 4)")
        self.assertEqual(labels["蓋"], "あり(縦桟 4 本・横桟 4 本)")
        self.assertIn("81.0", labels["外寸(幅×高さ×奥行)"])

    def test_バンド種別は渡されたときだけ(self) -> None:
        plain = {f["label"] for f in fig.build(**BASE).facts}
        self.assertNotIn("バンド種別", plain)
        named = {f["label"] for f in
                 fig.build(**{**BASE, "band_kind": "PETバンド"}).facts}
        self.assertIn("バンド種別", named)


class NotDrawableTests(unittest.TestCase):
    """形にならないとき。**入力の関門ではありません。**

    通る道では `gw_calculation.validate_inputs` を通った値しか来ないので、
    ここが何か言うことはまずありません ── `build()` を単体で呼んだときに、
    黙って潰れた絵を返さないための歯止めです。
    """

    def test_0の寸法では描けない(self) -> None:
        for key in ("thickness_mm", "width_mm", "length_mm", "count"):
            f = fig.build(**{**BASE, key: 0.0})
            self.assertFalse(f.drawable, key)
            self.assertEqual(f.boxes, [], key)
            self.assertTrue(any(p.field == key for p in f.problems), key)

    def test_横バンド1本では描けない(self) -> None:
        """図の上を走る帯が引けない(VBA も `YOBA <= 1` で断っていた)。"""
        self.assertFalse(fig.build(**{**BASE, "horizontal_bands": 1.0}).drawable)

    def test_計算側の文言を写さない(self) -> None:
        """**同じ事実を2か所に持たない。**

        入力の断り文句は `gw_calculation` が持っています。ここが同じ文言を
        持つと、片方を直した日にもう片方が古いことを言い始めます。
        """
        f = fig.build(**{**BASE, "horizontal_bands": 1.0})
        self.assertNotIn("入力が不正", f.problems[0].message)
        self.assertIn("図にできません", f.problems[0].message)


class ViewModelTests(unittest.TestCase):
    """画面へ渡す形。**画面は写すだけ。**"""

    def test_倒し方も一緒に渡す(self) -> None:
        """奥行きを45度に倒す割合。VBA が座標に掛けていた 0.354 そのもの。"""
        self.assertEqual(fig.build(**BASE).as_dict()["depth_ratio"], 0.354)

    def test_箱は種類と寸法だけ(self) -> None:
        """**色は入っていない。** 部材の色は `tokens.css` が持つ。

        `tone` は材質(PET か帯鉄か)で、色ではありません。
        """
        box = fig.build(**BASE).as_dict()["boxes"][0]
        self.assertEqual(set(box), {"kind", "x", "y", "w", "h", "depth",
                                    "label", "tone"})
        for body in fig.build(**BASE).as_dict()["boxes"]:
            self.assertNotIn("#", body["tone"], "色を書いている")

    def test_描けないときも形は同じ(self) -> None:
        """画面が「描けない」を出し分けられるように、鍵は欠けない。"""
        body = fig.build(**{**BASE, "width_mm": 0.0}).as_dict()
        self.assertFalse(body["drawable"])
        self.assertEqual(body["boxes"], [])
        self.assertTrue(body["problems"])


class ScreenTests(GwWebTestCase):
    """画面に届いているか。**描くのは JS だが、中身はサーバが決める。**"""

    def payload(self, **extra) -> dict:
        body = {
            "thickness_mm": "1.99", "width_mm": "810", "length_mm": "1500",
            "count": "40", "vertical_bands": "2", "horizontal_bands": "4",
            "pack_height_mm": "300", "pallet_weight_kg": "18",
            "stack_pattern": "1", "band_kind": "シール無",
        }
        body.update(extra)
        return body

    def test_計算の答えに図が付く(self) -> None:
        """**押すのは「計算」1つ。** 図のためにもう1度押させない
        (VBA は梱包図が別のボタンだった)。"""
        body = self.post("/api/gw/calculate", self.payload()).get_json()
        self.assertIn("figure", body)
        self.assertTrue(body["figure"]["drawable"])
        self.assertTrue(body["figure"]["boxes"])

    def test_アングルのチェックが図に効く(self) -> None:
        """「使う資材」の縦バンドアングルを外したら、図からも消える。"""
        flags = {name: True for name in (
            "dunplate", "outer_paper", "interleaf", "band", "poly_sheet",
            "angle", "hardboard")}
        on = self.post("/api/gw/calculate",
                       self.payload(flags=flags)).get_json()
        flags = {**flags, "angle": False}
        off = self.post("/api/gw/calculate",
                        self.payload(flags=flags)).get_json()
        kinds = lambda b: {x["kind"] for x in b["figure"]["boxes"]}
        self.assertIn("angle", kinds(on))
        self.assertNotIn("angle", kinds(off))

    def test_積み形態が図に効く(self) -> None:
        one = self.post("/api/gw/calculate", self.payload()).get_json()
        two = self.post("/api/gw/calculate",
                        self.payload(stack_pattern="2")).get_json()
        self.assertAlmostEqual(two["figure"]["width_cm"],
                               one["figure"]["width_cm"] * 2, places=1)

    def test_画面に図の札がある(self) -> None:
        body = self.get("/gw").get_data(as_text=True)
        self.assertIn('id="figure"', body)
        self.assertIn("梱包図", body)
        # **完成確認のためのもの**だと画面に書いてある
        self.assertIn("完成確認", body)

    def test_中身は描かないと分かる(self) -> None:
        """描いていないものを「あるはず」と思わせない。"""
        body = self.get("/gw").get_data(as_text=True)
        self.assertIn("外形だけ", body)

    def test_PETバンドは図でもPET(self) -> None:
        body = self.post("/api/gw/calculate",
                         self.payload(band_kind="PETバンド")).get_json()
        tones = {b["tone"] for b in body["figure"]["boxes"]
                 if b["kind"] == "band"}
        self.assertEqual(tones, {"pet"})

    def test_帯鉄は図でも帯鉄(self) -> None:
        body = self.post("/api/gw/calculate", self.payload()).get_json()
        tones = {b["tone"] for b in body["figure"]["boxes"]
                 if b["kind"] == "band"}
        self.assertEqual(tones, {"steel"})

    def test_縦バンド3本は計算させない(self) -> None:
        """**計算だけ通して図が描けない、を作らない。**"""
        res = self.post("/api/gw/calculate",
                        self.payload(vertical_bands="3"))
        self.assertEqual(res.status_code, 422)
        fields = [p["field"] for p in res.get_json()["problems"]]
        self.assertIn("vertical_bands", fields)

    def test_縦バンドは2本までと打つ前に分かる(self) -> None:
        body = self.get("/gw").get_data(as_text=True)
        self.assertIn("2本まで", body)


if __name__ == "__main__":
    unittest.main()
