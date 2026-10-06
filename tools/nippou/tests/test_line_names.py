"""ライン名の定義 ── 正規の呼び名がこのツールの名前、前は VBA の名前(v4.12.2 / v4.13.0)

    しっかり定義しましょうか
    正規 L-1 → L1 / LVC / HVC / 機側 → LS / NS1 / AIM / トット → TOT /
    バランサー → BALA / 中板(ラインNO とセット)→ MARU
    「コイル」はこのツールにはないラインです / AIM・LS・NS1・MARU は全部別のライン
    マスタに L1 と書いても L-1 と読んでくれる? ｌ―１ でも? あまり良くない

【約束】
    ・読むのは**正規の呼び名だけ**。前の名前(L1・LS)も、記号・大文字小文字の
      違う書き方(l-1・L―1・L 1)も読まない。揃えるのは文字の幅だけ(ﾄｯﾄ = トット)
    ・**ツールの名前は正規の呼び名**(v4.13.0)。前の名前は VBA が貯めたものを読むとき
      だけ `upgrade` で読み替える(LVC・HVC・NS1・AIM は前と同じ字)
    ・中板はラインNO(1〜7)とセット。**`中板3` のように続けて書く**(v4.12.3 で1つに決めた)
    ・ラインのつもりで読めない値は `check` が理由を言う(気づく仕組み)。ラインでない値は言わない
    ・表はツールの全ラインをちょうど1回ずつ持つ
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou import constants  # noqa: E402
from nippou.logic import line_names as ln  # noqa: E402

#: 決めてもらった表(正規 → 前)そのまま
AGREED = {"L-1": "L1", "LVC": "LVC", "HVC": "HVC", "機側": "LS", "NS1": "NS1",
          "AIM": "AIM", "トット": "TOT", "バランサー": "BALA"}


class DefinitionTests(unittest.TestCase):
    def test_決めた表のとおり(self) -> None:
        for official, old in AGREED.items():
            self.assertEqual(ln.read(official).code, official, official)   # ツールの名前 = 正規
            self.assertEqual(ln.by_official(official).old, old)
            self.assertEqual(ln.official_of(old), official)
        self.assertEqual(ln.read("中板3").code, "中板")
        self.assertEqual(ln.official_of("MARU"), "中板")

    def test_ツールの全ラインがちょうど1回ずつ(self) -> None:
        codes = [d.code for d in ln.DEFINITIONS]
        self.assertEqual(sorted(codes), sorted(constants.LINE_NAMES))
        self.assertEqual(codes, [d.official for d in ln.DEFINITIONS])
        self.assertEqual(len(set(codes)), len(codes))
        # AIM・LS・NS1・MARU は全部別のライン
        self.assertEqual(len({ln.read(n).code for n in ("AIM", "機側", "NS1", "中板1")}), 4)

    def test_正規でない書き方は読まない(self) -> None:
        for text in ("L1", "l-1", "ｌ―１", "L―1", "L 1", "LS", "TOT", "BALA", "MARU",
                     "hvc", "コイル", "作業長", "mode:field", ""):
            self.assertEqual(ln.read(text).code, "", text)

    def test_揃えるのは文字の幅だけ(self) -> None:
        self.assertEqual(ln.read("ﾄｯﾄ").code, "トット")          # 現物のライン毎目標の書き方
        self.assertEqual(ln.read("ﾊﾞﾗﾝｻｰ").code, "バランサー")
        self.assertEqual(ln.read("ＬＶＣ").code, "LVC")
        self.assertEqual(ln.read(" HVC ").code, "HVC")          # 前後の空白

    def test_中板はラインNOとセット(self) -> None:
        self.assertEqual(ln.read("中板3"), ln.Reading(code="中板", number="3"))
        self.assertEqual(ln.read("中板７"), ln.Reading(code="中板", number="7"))   # 全角の数字
        self.assertIn("ラインNO(1〜7)とセット", ln.read("中板").problem)
        self.assertIn("1〜7 ではありません", ln.read("中板9").problem)
        self.assertIn("1〜7 ではありません", ln.read("中板0").problem)
        for text in ("中板 3", "中板-3"):                       # 続けて書いたものだけ
            self.assertEqual(ln.read(text).code, "", text)
        # ほかのラインは数字を続けても読まない
        self.assertEqual(ln.read("HVC3").code, "")
        self.assertEqual(ln.to_code("中板"), "中板")             # 目標など、番号の要らない読み方

    def test_間違いに気づく_ラインのつもりで読めない値だけ言う(self) -> None:
        self.assertEqual(ln.check("L1"), "「L1」は読みません(正規は「L-1」)")
        self.assertEqual(ln.check("ｌ―１"), "「ｌ―１」は読みません(正規は「L-1」)")  # 書いてあるまま
        self.assertEqual(ln.check("LS"), "「LS」は読みません(正規は「機側」)")
        self.assertIn("ラインNO(1〜7)とセット", ln.check("中板"))
        self.assertIn("1〜7 ではありません", ln.check("中板9"))
        for text in ("中板 3", "中板-3", "中板No3"):
            self.assertIn("「中板3」のようにラインNO を続けて", ln.check(text), text)
        # 読めるもの・ラインでないものは言わない(スルーの決まり)
        for text in ("L-1", "機側", "ﾄｯﾄ", "中板3", "mode:field", "mode:material", "作業長",
                     "コイル", "Administrator", "予備PC", ""):
            self.assertEqual(ln.check(text), "", text)

    def test_読めなかった書き方に正規を添える(self) -> None:
        self.assertEqual(ln.hint("L1"), "正規は「L-1」")
        self.assertEqual(ln.hint("ｌ―１"), "正規は「L-1」")
        self.assertEqual(ln.hint("LS"), "正規は「機側」")
        self.assertEqual(ln.hint("TOT"), "正規は「トット」")
        self.assertEqual(ln.hint("MARU"), "正規は「中板」")
        self.assertEqual(ln.hint("コイル"), "")                  # このツールに無いライン
        self.assertEqual(ln.hint("mode:field"), "")


class UpgradeTests(unittest.TestCase):
    """前の名前 → 正規(VBA が貯めたもの・v4.12 までの値を読むときだけ)(v4.13.0)。"""

    def test_前の名前を正規へ(self) -> None:
        for official, old in AGREED.items():
            self.assertEqual(ln.upgrade(old), official)
        self.assertEqual(ln.upgrade("MARU"), "中板")
        self.assertEqual(ln.upgrade("ﾄｯﾄ"), "トット")                  # 正規は正規の字に揃える
        self.assertEqual(ln.upgrade("機側"), "機側")
        for text in ("未設定", "", "コイル", "l-1"):                    # どちらでもない値はそのまま
            self.assertEqual(ln.upgrade(text), text)
        self.assertEqual(ln.upgrade(None), "")

    def test_当てた印も揃える(self) -> None:
        self.assertEqual(ln.upgrade_key("MARU:3"), "中板:3")
        self.assertEqual(ln.upgrade_key("L1"), "L-1")
        self.assertEqual(ln.upgrade_key(""), "")

    def test_名前が変わるのは5ライン(self) -> None:
        self.assertEqual([(d.old, d.official) for d in ln.renamed()],
                         [("L1", "L-1"), ("LS", "機側"), ("TOT", "トット"),
                          ("BALA", "バランサー"), ("MARU", "中板")])

    def test_見せる字(self) -> None:
        self.assertEqual(ln.label("中板", "4"), "中板4")
        self.assertEqual(ln.label("LS"), "機側")                        # 前の名前が来ても正規で
        self.assertEqual(ln.labels()["MARU"], "中板")
        self.assertEqual(ln.labels()["機側"], "機側")


if __name__ == "__main__":
    unittest.main()
