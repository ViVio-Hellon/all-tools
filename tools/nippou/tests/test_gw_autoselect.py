"""オーダーからの梱包仕様の自動選択 (`logic/gw_autoselect.py`)

VBA ``VC選択`` / ``合紙選択`` / ``バンド選択`` の移植。LotNo を入れて
オーダーが決まると、その場でチェックが付け直されます ── VC を使うか、
合紙を挟むか、帯鉄かPETバンドか、縦バンドは何本か。

【なぜ機械で見るのか】
どれも**間違えても画面はふつうに出ます。** PETバンドで梱包するはずの
輸出品を帯鉄で計算しても、数字は出てしまう。気づくのは出荷のあとです。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import gw_autoselect as a


class VcTests(unittest.TestCase):
    """port of ``VC選択``: VC_表 / VC_裏 の入っているほうを使う。"""

    def test_両面(self) -> None:
        out = a.select_vc("VC-A", "VC-B")
        self.assertTrue(out.use_vc)
        self.assertTrue(out.vc_side_a)
        self.assertTrue(out.vc_side_b)
        self.assertEqual((out.vc_name_a, out.vc_name_b), ("VC-A", "VC-B"))

    def test_表だけ(self) -> None:
        out = a.select_vc("VC-A", "")
        self.assertTrue(out.use_vc)
        self.assertTrue(out.vc_side_a)
        self.assertFalse(out.vc_side_b)

    def test_裏だけ(self) -> None:
        out = a.select_vc("", "VC-B")
        self.assertTrue(out.use_vc)
        self.assertFalse(out.vc_side_a)
        self.assertTrue(out.vc_side_b)

    def test_どちらも空ならVCを使わない(self) -> None:
        out = a.select_vc("", "")
        self.assertFalse(out.use_vc)
        self.assertEqual(out.decided, [])

    def test_空白だけの値は空として扱う(self) -> None:
        # Access の固定長列は空白で埋まっていることがある
        self.assertFalse(a.select_vc("   ", "\t").use_vc)


class InterleafTests(unittest.TestCase):
    """port of ``合紙選択``: 「合紙」列が "1" のときだけ。"""

    def test_1なら使う(self) -> None:
        self.assertTrue(a.select_interleaf("1"))

    def test_それ以外は使わない(self) -> None:
        for value in ("0", "", "  ", "2", "はい"):
            self.assertFalse(a.select_interleaf(value), value)


class BandTests(unittest.TestCase):
    """port of ``バンド選択``。"""

    def test_既定は帯鉄シール無(self) -> None:
        out = a.select_band("どこかの会社", "どこか")
        self.assertEqual(out.band_kind, a.BAND_NO_SEAL)
        self.assertIsNone(out.vertical_bands)
        self.assertEqual(out.decided, [])

    def test_ナメカワは納入先を問わずPET(self) -> None:
        # VBA の最初の `If`(2017.11.29 新輸出形態)
        out = a.select_band("ﾅﾒｶﾜｱﾙﾐ(ｶ", "どこか")
        self.assertEqual(out.band_kind, a.BAND_PET)

    def test_ナメカワKIZANは縦1本と合紙(self) -> None:
        out = a.select_band("ﾅﾒｶﾜｱﾙﾐ(ｶ", "KIZAN CHUANFU")
        self.assertEqual(out.band_kind, a.BAND_PET)
        self.assertEqual(out.vertical_bands, 1)
        self.assertTrue(out.interleaf)

    def test_ニツケイ中国と台湾の両方が効く(self) -> None:
        for delivery in ("ﾁﾕｳｺﾞｸ", "ﾀｲﾜﾝ"):
            out = a.select_band("ﾆﾂｹｲｻﾝｷﾞﾖｳ(ｶ :ｷﾕｳｼﾖｳｼﾞｸﾞﾁ", delivery)
            self.assertEqual(out.band_kind, a.BAND_PET, delivery)
            self.assertEqual(out.vertical_bands, 1, delivery)

    def test_イイダ台湾は合紙を使わない(self) -> None:
        """**ここだけ合紙をOFFに倒す。** VBAのコメントも「無し指定」。"""
        out = a.select_band("ｲｲﾀﾞｹｲｷﾝ(ｶ", "ﾀｲﾜﾝ")
        self.assertEqual(out.band_kind, a.BAND_PET)
        self.assertFalse(out.interleaf)
        self.assertIn("interleaf", out.decided)

    def test_納入先が違えば効かない(self) -> None:
        # 取引先だけ一致しても、納入先が違えば輸出形態ではない
        out = a.select_band("ｲｲﾀﾞｹｲｷﾝ(ｶ", "ｱｲﾁ")
        self.assertEqual(out.band_kind, a.BAND_NO_SEAL)
        self.assertIsNone(out.vertical_bands)

    def test_包装仕様7P0106はPETと縦1本(self) -> None:
        out = a.select_band("どこかの会社", "どこか", "7P0106")
        self.assertEqual(out.band_kind, a.BAND_PET)
        self.assertEqual(out.vertical_bands, 1)

    def test_包装仕様はあとから効く(self) -> None:
        """VBA は輸出形態の `Select Case` の**あと**に包装仕様を見ていた。"""
        out = a.select_band("ｲｲﾀﾞｹｲｷﾝ(ｶ", "ﾀｲﾜﾝ", "7P0106")
        self.assertEqual(out.band_kind, a.BAND_PET)
        self.assertEqual(out.vertical_bands, 1)


class AngleTests(unittest.TestCase):
    """縦バンドアングルは**縦バンドを掛けるときだけ**(縦1本でも角は要る)。

    VBA ``TABA_Change`` は「縦2本のときだけ」でした。
    """

    def test_縦バンドがあれば使う(self) -> None:
        for n in (1, 2):
            self.assertTrue(a.use_angle(n), n)

    def test_縦バンドが無ければ使わない(self) -> None:
        for n in (0, None):
            self.assertFalse(a.use_angle(n), n)


class SelectAllTests(unittest.TestCase):
    """VBA ``Hiki展開`` 末尾の VC選択 → 合紙選択 → バンド選択 の並び。"""

    def test_国内は既定のまま(self) -> None:
        out = a.select_all(customer="ｺｸﾅｲ(ｶ", delivery="ｱｲﾁ",
                           interleaf_flag="1", vertical_bands=2)
        self.assertFalse(out.use_vc)
        self.assertTrue(out.interleaf)          # 合紙列が "1"
        self.assertEqual(out.band_kind, a.BAND_NO_SEAL)
        self.assertTrue(out.angle)              # 縦2本

    def test_輸出形態が合紙を上書きする(self) -> None:
        """オーダーの合紙列が "1" でも、ｲｲﾀﾞ/ﾀｲﾜﾝ は「無し指定」が勝つ。"""
        out = a.select_all(customer="ｲｲﾀﾞｹｲｷﾝ(ｶ", delivery="ﾀｲﾜﾝ",
                           interleaf_flag="1")
        self.assertFalse(out.interleaf)

    def test_自動で決まった縦バンドがアングルの判断に使われる(self) -> None:
        # 画面に0本と入っていても、輸出形態が1本に決めれば縦バンドアングルを使う
        out = a.select_all(customer="ﾅﾒｶﾜｱﾙﾐ(ｶ", delivery="KIZAN CHUANFU",
                           vertical_bands=0)
        self.assertEqual(out.vertical_bands, 1)
        self.assertTrue(out.angle)
        # 縦バンドが無ければ使わない
        self.assertFalse(a.select_all(customer="ｺｸﾅｲ(ｶ", vertical_bands=0).angle)

    def test_決まった項目が分かる(self) -> None:
        """**どれが自動で入ったのか**が画面から分からないと、直してよいか
        判断できない(VBA が赤色でしていたこと)。"""
        out = a.select_all(vc_front="VC-A", customer="ﾅﾒｶﾜｱﾙﾐ(ｶ",
                           delivery="KIZAN CHUANFU")
        for name in ("vc", "band_kind", "vertical_bands", "interleaf"):
            self.assertIn(name, out.decided)

    def test_何も当たらなければ何も決めない(self) -> None:
        out = a.select_all(customer="ｺｸﾅｲ(ｶ", delivery="ｱｲﾁ")
        self.assertEqual(out.decided, [])
        self.assertEqual(out.band_kind, a.BAND_NO_SEAL)

    def test_輸出形態の覚え書きが残る(self) -> None:
        out = a.select_all(customer="ﾅﾒｶﾜｱﾙﾐ(ｶ", delivery="KIZAN CHUANFU")
        self.assertIn("2017.11.29", out.note)


class RuleTableTests(unittest.TestCase):
    """表そのものの決めごと。"""

    def test_取引先名は半角カナのまま(self) -> None:
        """**見た目で書き換えると一致しなくなる。** Access に入っている
        ままの文字であること(閉じ括弧が無いのも原文どおり)。"""
        names = {r.customer for r in a.EXPORT_RULES}
        self.assertIn("ﾅﾒｶﾜｱﾙﾐ(ｶ", names)
        self.assertNotIn("ナメカワアルミ(株)", names)

    def test_全部の行が納入先まで持つ(self) -> None:
        # VBA はどれも取引先の中で納入先を見ていた。取引先だけで決まる行を
        # 足すと、同じ取引先の国内向けまで巻き込む
        for rule in a.EXPORT_RULES:
            self.assertTrue(rule.delivery, rule.customer)

    def test_見つからなければNone(self) -> None:
        self.assertIsNone(a.find_export_rule("知らない会社", "どこか"))


if __name__ == "__main__":
    unittest.main()
