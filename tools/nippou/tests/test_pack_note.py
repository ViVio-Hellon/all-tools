"""包装仕様の注意表記 (`logic/pack_note.py` / `access_bridge/pack_note_master.py`)

VBA ``PackagingSpecificationNo`` の移植。包装仕様NO に付いている
「ふだんと違うこと」を、**ロット番号を打った時点で**出します ──
打ち終わってから気づくと、荷を解くことになります。

ここで守るのは:

    ・フラグ(用途コード/寸法/納入先/取引先)の判定
    ・**組み合わせのフラグ**(`寸法納入` / `寸法取引`)は、並んでいる
      条件を**すべて**満たしたときだけ出すこと
    ・除外(`≠`)は、フラグに書いていなくても効くこと
    ・複数あたったら**重ねる**こと(並びも VBA と同じ)
    ・**コイルだけ**画面の前に出すこと(板は etc 欄へ入るだけ)
    ・読めない書き方で**勝手に出さない**こと
    ・マスタが読めなくても、日報の入力は止まらないこと
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import pack_note
from nippou.logic.pack_note import NoteRow


def row(**values) -> NoteRow:
    base = dict(pack_spec_no="1P0001", comment="裸梱包", shape="板", flag="",
                usage_code="＊", thickness_low="＊", thickness_high="＊",
                width_low="＊", width_high="＊", length_low="＊",
                length_high="＊", delivery="")
    base.update(values)
    return NoteRow(**base)


def size_row(**values) -> NoteRow:
    """実データと同じ形の寸法行(厚0.1〜13 / 幅1490〜1540 / 丈2700〜3090)。"""
    base = dict(flag="寸法", comment="桁角材高さ90mm",
                thickness_low="0.1", thickness_high="13.0",
                width_low="1490.0", width_high="1540.0",
                length_low="2700.0", length_high="3090.0")
    base.update(values)
    return row(**base)


IN_RANGE = dict(thickness=5.0, width=1500.0, length=3000.0)


class FlagTests(unittest.TestCase):
    def test_フラグが空ならいつでも出す(self) -> None:
        note = pack_note.build("1P0113", [row(comment="裸梱包")])
        self.assertEqual(note.comments, ["裸梱包"])

    def test_用途コードが一致したときだけ(self) -> None:
        rows = [row(flag="用途コード", usage_code="R112",
                    comment="ｽﾌﾟﾚｰﾏｰｷﾝｸﾞ")]
        self.assertEqual(
            pack_note.build("x", rows, usage_code="R112").comments,
            ["ｽﾌﾟﾚｰﾏｰｷﾝｸﾞ"])
        self.assertEqual(
            pack_note.build("x", rows, usage_code="R999").comments, [])

    def test_用途コードが空の行は出さない(self) -> None:
        """`＊` でも一致しない。**打った用途が `＊` になることは無い。**"""
        rows = [row(flag="用途コード", usage_code="＊", comment="出ないはず")]
        self.assertEqual(pack_note.build("x", rows, usage_code="R112").comments, [])

    def test_寸法は3つとも範囲に入ったとき(self) -> None:
        rows = [size_row()]
        self.assertEqual(pack_note.build("x", rows, **IN_RANGE).comments,
                         ["桁角材高さ90mm"])
        # 幅だけ外す
        self.assertEqual(pack_note.build("x", rows, thickness=5.0, width=1000.0,
                                         length=3000.0).comments, [])
        # 丈だけ外す
        self.assertEqual(pack_note.build("x", rows, thickness=5.0, width=1500.0,
                                         length=9999.0).comments, [])

    def test_寸法は境界を含む(self) -> None:
        """VBA も `>=` と `<=`。ちょうどの寸法で出なくなると困る。"""
        rows = [size_row()]
        self.assertEqual(pack_note.build("x", rows, thickness=0.1, width=1490.0,
                                         length=2700.0).comments,
                         ["桁角材高さ90mm"])
        self.assertEqual(pack_note.build("x", rows, thickness=13.0, width=1540.0,
                                         length=3090.0).comments,
                         ["桁角材高さ90mm"])

    def test_範囲が全角アスタリスクなら通らない(self) -> None:
        """`Val("＊")` は 0。**0以上0以下**になるので、まず通りません。

        VBA と同じ振る舞いです ── ここを「＊なら全部通す」に変えると、
        VBA では出ていなかった注意が出はじめます。
        """
        rows = [row(flag="寸法")]      # 範囲は全部 ＊
        self.assertEqual(pack_note.build("x", rows, **IN_RANGE).comments, [])
        # 0 のときだけは通る(VBA も同じ)
        self.assertEqual(pack_note.build("x", rows, thickness=0, width=0,
                                         length=0).comments, ["裸梱包"])

    def test_納入先は部分一致(self) -> None:
        """行の納入先に、引いた納入先が**含まれる**か(VBA `Like "*…*"`)。"""
        rows = [row(flag="納入先", delivery="ﾅﾒｶﾜｱﾙﾐ(ｶ",
                    comment="ラテラルボー測定")]
        self.assertEqual(pack_note.build("x", rows, delivery="ﾅﾒｶﾜｱﾙﾐ").comments,
                         ["ラテラルボー測定"])
        self.assertEqual(pack_note.build("x", rows, delivery="よそ").comments, [])

    def test_納入先が空なら出さない(self) -> None:
        """**欠陥引当などで納入先が空のことがある。**

        部分一致のままだと空文字が全部にあたって、どのロットでも
        ラテラルボー扱いになります(VBA の 2024.3.11 の直しと同じ)。
        """
        rows = [row(flag="納入先", delivery="ﾅﾒｶﾜｱﾙﾐ(ｶ", comment="出ないはず")]
        self.assertEqual(pack_note.build("x", rows, delivery="").comments, [])

    def test_取引先が一致したときだけ(self) -> None:
        rows = [row(flag="取引先", customer="ﾅﾒｶﾜｱﾙﾐ(ｶ", comment="ラテラルボー測定")]
        self.assertEqual(
            pack_note.build("x", rows, customer="ﾅﾒｶﾜｱﾙﾐ(ｶ").comments,
            ["ラテラルボー測定"])
        self.assertEqual(
            pack_note.build("x", rows, customer="よその取引先").comments, [])

    def test_取引先が空なら出さない(self) -> None:
        rows = [row(flag="取引先", customer="ﾅﾒｶﾜｱﾙﾐ(ｶ", comment="出ないはず")]
        self.assertEqual(pack_note.build("x", rows, customer="").comments, [])

    def test_読めない書き方では出さない(self) -> None:
        """知らない言葉を勝手に解釈して、意図と違う注意を出さない。"""
        rows = [row(flag="なぞの条件", comment="出ないはず"),
                row(flag="寸法なぞ", comment="これも出ないはず")]
        self.assertEqual(pack_note.build("x", rows, **IN_RANGE).comments, [])


class FlagWordTests(unittest.TestCase):
    """フラグの読み分け(`parse_flag`)。**長いものから**照合する。"""

    def test_1つだけ(self) -> None:
        for flag, expected in (("", []), ("寸法", ["寸法"]),
                               ("用途コード", ["用途コード"]),
                               ("納入先", ["納入先"]), ("取引先", ["取引先"])):
            self.assertEqual(pack_note.parse_flag(flag), (expected, ""), flag)

    def test_組み合わせ(self) -> None:
        self.assertEqual(pack_note.parse_flag("寸法納入"),
                         (["寸法", "納入先"], ""))
        self.assertEqual(pack_note.parse_flag("寸法取引"),
                         (["寸法", "取引先"], ""))

    def test_読めない残りを返す(self) -> None:
        self.assertEqual(pack_note.parse_flag("寸法なぞ"), (["寸法"], "なぞ"))
        self.assertEqual(pack_note.parse_flag("なぞ"), ([], "なぞ"))


class CombinedFlagTests(unittest.TestCase):
    """**組み合わせは「すべて満たしたとき」。**

    VBA の `Select Case` には `寸法納入`(実データ6行)と `寸法取引`
    (2行)が無く、どちらも**注意が1つも出ていませんでした**。
    """

    def test_寸法納入は両方そろって出る(self) -> None:
        rows = [size_row(flag="寸法納入", delivery="ﾆﾎﾝｼﾔﾘﾖｳｾｲｿﾞｳ(ｶ",
                         comment="ラテラルボー測定")]
        self.assertEqual(
            pack_note.build("x", rows, delivery="ﾆﾎﾝｼﾔﾘﾖｳｾｲｿﾞｳ(ｶ",
                            **IN_RANGE).comments, ["ラテラルボー測定"])

    def test_寸法だけ合っても出ない(self) -> None:
        rows = [size_row(flag="寸法納入", delivery="ﾆﾎﾝｼﾔﾘﾖｳｾｲｿﾞｳ(ｶ",
                         comment="出ないはず")]
        self.assertEqual(
            pack_note.build("x", rows, delivery="よその会社",
                            **IN_RANGE).comments, [])

    def test_納入先だけ合っても出ない(self) -> None:
        rows = [size_row(flag="寸法納入", delivery="ﾆﾎﾝｼﾔﾘﾖｳｾｲｿﾞｳ(ｶ",
                         comment="出ないはず")]
        self.assertEqual(
            pack_note.build("x", rows, delivery="ﾆﾎﾝｼﾔﾘﾖｳｾｲｿﾞｳ(ｶ",
                            thickness=5.0, width=100.0,
                            length=3000.0).comments, [])

    def test_寸法取引も同じ(self) -> None:
        rows = [size_row(flag="寸法取引", customer="ﾅﾒｶﾜｱﾙﾐ(ｶ",
                         comment="ラテラルボー測定")]
        self.assertEqual(
            pack_note.build("x", rows, customer="ﾅﾒｶﾜｱﾙﾐ(ｶ",
                            **IN_RANGE).comments, ["ラテラルボー測定"])
        self.assertEqual(
            pack_note.build("x", rows, customer="よそ", **IN_RANGE).comments, [])


class ExcludeTests(unittest.TestCase):
    """除外(`≠`)。**フラグに書いていなくても効かせる。**

    実データの `寸法取引` 行は、取引先の条件に加えて納入先の欄に
    `≠K.T.N. CO. LTD,≠ﾆﾎﾝﾊﾂｼﾞﾖｳ*` を持っています。無視すると、
    **わざわざ「除く」と書いてある相手に注意を出す**ことになります。
    """

    EXPR = "≠K.T.N. CO. LTD,≠ﾆﾎﾝﾊﾂｼﾞﾖｳ*"

    def _rows(self):
        return [size_row(flag="寸法取引", customer="ﾅﾒｶﾜｱﾙﾐ(ｶ",
                         delivery=self.EXPR, comment="ラテラルボー測定")]

    def test_除かれていなければ出る(self) -> None:
        self.assertEqual(
            pack_note.build("x", self._rows(), customer="ﾅﾒｶﾜｱﾙﾐ(ｶ",
                            delivery="ふつうの会社", **IN_RANGE).comments,
            ["ラテラルボー測定"])

    def test_除外にあたれば出ない(self) -> None:
        self.assertEqual(
            pack_note.build("x", self._rows(), customer="ﾅﾒｶﾜｱﾙﾐ(ｶ",
                            delivery="K.T.N. CO. LTD", **IN_RANGE).comments, [])

    def test_ワイルドカードの除外(self) -> None:
        """`ﾆﾎﾝﾊﾂｼﾞﾖｳ*` は「で始まるところ全部」。"""
        self.assertEqual(
            pack_note.build("x", self._rows(), customer="ﾅﾒｶﾜｱﾙﾐ(ｶ",
                            delivery="ﾆﾎﾝﾊﾂｼﾞﾖｳ(ｶ", **IN_RANGE).comments, [])

    def test_式の読み方(self) -> None:
        terms = pack_note.parse_terms(self.EXPR)
        self.assertEqual(len(terms), 2)
        self.assertTrue(all(t.negate for t in terms))
        self.assertTrue(pack_note.excluded(self.EXPR, "K.T.N. CO. LTD"))
        self.assertTrue(pack_note.excluded(self.EXPR, "ﾆﾎﾝﾊﾂｼﾞﾖｳ(ｶ"))
        self.assertFalse(pack_note.excluded(self.EXPR, "ふつうの会社"))

    def test_除外だけの式は除かれなければ一致(self) -> None:
        self.assertTrue(pack_note.expression_hit(self.EXPR, "ふつうの会社"))
        self.assertFalse(pack_note.expression_hit(self.EXPR, "K.T.N. CO. LTD"))

    def test_空の値は何にもあたらない(self) -> None:
        self.assertFalse(pack_note.excluded(self.EXPR, ""))
        self.assertFalse(pack_note.expression_hit("A社", ""))


class MultipleTests(unittest.TestCase):
    def test_あたったぶんを重ねる(self) -> None:
        rows = [row(comment="コイル巻き、反時計方向", shape="コイル"),
                row(comment="梱包毎 ｺｲﾙ明細を添付", shape="コイル"),
                row(comment="リプラ", shape="コイル")]
        note = pack_note.build("1C1273", rows)
        self.assertEqual(len(note.comments), 3)

    def test_並びはVBAと同じ逆順(self) -> None:
        """VBA は `Kari & " " & 既にあるもの` と**前へ積んで**いた。

        紙に出る順が変わると「いつもと違う」と見えるので合わせます。
        """
        rows = [row(comment="1つ目"), row(comment="2つ目"), row(comment="3つ目")]
        self.assertEqual(pack_note.build("x", rows).comments,
                         ["3つ目", "2つ目", "1つ目"])

    def test_あたらない行は飛ばす(self) -> None:
        rows = [row(comment="いつでも"),
                size_row(comment="寸法のとき"),
                row(flag="用途コード", usage_code="R999", comment="用途のとき")]
        self.assertEqual(pack_note.build("x", rows, **IN_RANGE).comments,
                         ["寸法のとき", "いつでも"])

    def test_納入先は行ごとに見る(self) -> None:
        """**VBAから直したところ。**

        あちらは複数行のとき、どの行でも `TempHiki(1, C_納入先)`
        (=1行目の納入先)を見ていました。1行だけのときは正しいので、
        複数行のほうへ写したときの取り違えです。
        """
        rows = [row(flag="納入先", delivery="A社", comment="Aの注意"),
                row(flag="納入先", delivery="B社", comment="Bの注意")]
        self.assertEqual(pack_note.build("x", rows, delivery="B社").comments,
                         ["Bの注意"])

    def test_空のコメントは重ねない(self) -> None:
        rows = [row(comment=""), row(comment="出るほう")]
        self.assertEqual(pack_note.build("x", rows).comments, ["出るほう"])

    def test_同じ文言は1度だけ(self) -> None:
        """実データの `寸法納入` は厚みで2行に分かれ、文言は同じ。

        両方あたると「ラテラルボー測定 / ラテラルボー測定」になります。
        """
        rows = [row(comment="ラテラルボー測定"), row(comment="ラテラルボー測定")]
        self.assertEqual(pack_note.build("x", rows).comments,
                         ["ラテラルボー測定"])


class ShapeTests(unittest.TestCase):
    def test_コイルは画面の前に出す(self) -> None:
        note = pack_note.build("1C1296", [row(shape="コイル", comment="巻き向き")])
        self.assertTrue(note.is_coil)
        self.assertIn("コイルの注意", note.message)

    def test_板は出さない(self) -> None:
        """VBA `If arr(0) <> "板" Then MsgBox` ──
        板は検査側で出るので、日報では etc 欄へ入れるだけ。"""
        note = pack_note.build("1P0113", [row(shape="板", comment="裸梱包")])
        self.assertFalse(note.is_coil)
        self.assertIn("包装仕様の注意", note.message)

    def test_形状は1行目のもの(self) -> None:
        rows = [row(shape="コイル", comment="1つ目"),
                row(shape="コイル", comment="2つ目")]
        self.assertEqual(pack_note.build("x", rows).shape, "コイル")


class EmptyTests(unittest.TestCase):
    def test_番号が空なら何もしない(self) -> None:
        note = pack_note.build("", [row()])
        self.assertFalse(note.found)
        self.assertFalse(note.has_note)

    def test_行が無ければ何もしない(self) -> None:
        note = pack_note.build("1P9999", [])
        self.assertFalse(note.found)
        self.assertEqual(note.text, "")
        self.assertEqual(note.message, "")

    def test_あたらなくても表にあったことは分かる(self) -> None:
        """**「番号が無い」と「条件にあたらない」は別。** 記録に使う。"""
        note = pack_note.build("x", [row(flag="用途コード", usage_code="R112")],
                               usage_code="R999")
        self.assertTrue(note.found)
        self.assertFalse(note.has_note)


class TextTests(unittest.TestCase):
    def test_etc欄は区切りを入れる(self) -> None:
        """**改行は使えません。**

        画面の etc 欄は1行の `<input>` で、改行を入れるとブラウザが
        黙って落とします ── `リプラ` と `明細を添付` が
        `リプラ明細を添付` になって読めなくなります。
        """
        rows = [row(comment="1つ目"), row(comment="2つ目")]
        self.assertEqual(pack_note.build("x", rows).text, "2つ目 / 1つ目")
        self.assertNotIn("\n", pack_note.build("x", rows).text)

    def test_辞書にすると画面が読める形になる(self) -> None:
        body = pack_note.build("1C1296",
                               [row(shape="コイル", comment="巻き向き")]).as_dict()
        self.assertTrue(body["is_coil"])
        self.assertTrue(body["has_note"])
        self.assertEqual(body["comments"], ["巻き向き"])
        self.assertEqual(body["pack_spec_no"], "1C1296")


class MasterTests(unittest.TestCase):
    """ファイルを読む側。**読めなくても入力は止めません。**"""

    def test_番号が空なら読みに行かない(self) -> None:
        from nippou.access_bridge import pack_note_master

        self.assertEqual(pack_note_master.load_notes(""), [])
        self.assertEqual(pack_note_master.load_notes("   "), [])

    def test_読めなくても空を返す(self) -> None:
        from nippou.access_bridge import pack_note_master

        self.assertEqual(
            pack_note_master.load_notes(
                "1P0001", master_path=Path("/どこにも/無い.sqlite3")), [])

    def test_拾う列がVBAとそろっている(self) -> None:
        from nippou.access_bridge import pack_note_master

        for column in ("包装仕様NO", "コメント", "形状", "フラグ", "用途コード",
                       "厚下", "厚上", "板幅下", "板幅上", "板丈下", "板丈上",
                       "納入先"):
            self.assertIn(column, pack_note_master.COLUMNS, column)


if __name__ == "__main__":
    unittest.main()
