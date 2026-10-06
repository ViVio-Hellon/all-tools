"""ライン毎目標と45度線 ── **メモ帳で直せるCSVを正にする**

VBA は 梱包資材マスタ「ライン毎目標」を `目標値抜き取り` で引き、
`グラフ挿入` が `Targetline` の ON のときだけ赤い「目標値」線を足して
いました。目標を1つ変えるのに Access なりツールなりを開くのは重すぎる
ので、Web版は**マスタとCSVの両方**から読み、同じラインが両方にあれば
**CSVを採ります**(「DBの書き換えよりCSVの書き換えのほうが簡単だから」)。

ここで守るのは:

    ・**マスタとCSVの両方**を読む。同じラインならCSVが勝つ
    ・勝ち負けは**ラインごと**(CSVに1行書いても、他のラインは消えない)
    ・**読めない行を黙って捨てない**(捨てると目標線がひっそり消える)
    ・現場の書き方の揺れで落ちない(全角・タブ・`L-1` と `L1`)
    ・cp932 で保存されたメモ帳のファイルが読める
    ・累積は 目標×k、直枚数は 目標 で横ばい(**VBAどおり**)
    ・マスタの呼び名(機側 / ﾄｯﾄ / ﾊﾞﾗﾝｻｰ)は読むときに読み替える
    ・目標が無くても、実績のグラフは今までどおり出る
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import line_target as lt


class NormalizeTests(unittest.TestCase):
    """ライン名の突き合わせ。**書き方の揺れで線を消さない。**"""

    def test_master_l1_matches_tool_l1(self):
        # 「L-1」と前の名前「L1」。ここが合わないのが
        # いちばん困る(黙って目標線が出なくなる)
        self.assertEqual(lt.normalize_line("L-1"), lt.normalize_line("L1"))
        self.assertEqual(lt.normalize_line("L1"), "L-1")     # ツールの名前(正規)で返す(v4.13.0)

    def test_full_width_matches_half_width(self):
        self.assertEqual(lt.normalize_line("ＮＳ１"), "NS1")

    def test_case_and_spaces(self):
        self.assertEqual(lt.normalize_line("  lvc "), "LVC")
        self.assertEqual(lt.normalize_line("L 1"), "L-1")
        self.assertEqual(lt.normalize_line("L　1"), "L-1")

    def test_underscore_and_dot(self):
        self.assertEqual(lt.normalize_line("L_1"), "L-1")
        self.assertEqual(lt.normalize_line("L.1"), "L-1")

    def test_ツールに無い名前は揺れを落とした形(self):
        self.assertEqual(lt.normalize_line("予備 PC"), "予備PC")
        self.assertEqual(lt.normalize_line("ﾄｯﾄ"), "トット")

    def test_empty_is_empty(self):
        self.assertEqual(lt.normalize_line(""), "")
        self.assertEqual(lt.normalize_line(None), "")


class NumberTests(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(lt.to_number("12"), 12.0)

    def test_full_width_digits(self):
        self.assertEqual(lt.to_number("１１９"), 119.0)

    def test_thousands_separator(self):
        self.assertEqual(lt.to_number("1,200"), 1200.0)

    def test_decimal(self):
        self.assertEqual(lt.to_number("12.5"), 12.5)

    def test_not_a_number(self):
        self.assertIsNone(lt.to_number("abc"))
        self.assertIsNone(lt.to_number(""))
        self.assertIsNone(lt.to_number("   "))


class ParseTests(unittest.TestCase):
    def test_basic(self):
        found = lt.parse("L1,12\nLVC,119\n")
        self.assertEqual(found.values, {"L-1": 12.0, "LVC": 119.0})
        self.assertEqual(found.problems, [])

    def test_comment_and_blank_lines_are_skipped_quietly(self):
        found = lt.parse("# めも\n\n// これも\n' これも\nL1,12\n")
        self.assertEqual(found.values, {"L-1": 12.0})
        # 覚え書きは困りごとではない。**ここで数えると毎回赤くなる**
        self.assertEqual(found.problems, [])

    def test_tab_and_space_separators(self):
        found = lt.parse("機側\t100\nNS1  10\nHVC;50\n")
        self.assertEqual(found.values,
                         {"機側": 100.0, "NS1": 10.0, "HVC": 50.0})

    def test_前の名前で書いたCSVも読む(self) -> None:
        """v4.12 までの CSV はツールの名前 = VBA の名前(LS・TOT)で書いてある(v4.13.0)。"""
        found = lt.parse("LS,100\nTOT,10\nBALA,5\nMARU,3\nL1,12\n")
        self.assertEqual(found.values,
                         {"機側": 100.0, "トット": 10.0, "バランサー": 5.0, "中板": 3.0, "L-1": 12.0})
        self.assertEqual(found.of("機側"), 100.0)
        self.assertEqual(found.of("L-1"), 12.0)
        self.assertEqual(found.problems, [])

    def test_full_width_comma(self):
        # 全角のまま打たれても読む。読まないと、その行の目標が
        # **黙って1つ減る**
        found = lt.parse("ＬＶＣ，１１９\n")
        self.assertEqual(found.values, {"LVC": 119.0})
        self.assertEqual(found.problems, [])

    def test_broken_line_is_reported_not_dropped(self):
        found = lt.parse("L1\n")
        self.assertEqual(found.values, {})
        self.assertEqual(len(found.problems), 1)
        self.assertEqual(found.problems[0].line_no, 1)

    def test_non_numeric_target_is_reported(self):
        found = lt.parse("L1,12\nTOT,あとで\n")
        self.assertEqual(found.values, {"L-1": 12.0})
        self.assertEqual(len(found.problems), 1)
        self.assertIn("あとで", found.problems[0].reason)
        self.assertEqual(found.problems[0].line_no, 2)

    def test_negative_target_is_refused(self):
        found = lt.parse("L1,-5\n")
        self.assertEqual(found.values, {})
        self.assertIn("マイナス", found.problems[0].reason)

    def test_zero_is_stored_but_draws_no_line(self):
        # VBA も `累積目標 <> 0` を見ていた。0 は「引かない」の意味
        found = lt.parse("L1,0\n")
        self.assertEqual(found.values, {"L-1": 0.0})
        self.assertIsNone(found.of("L1"))

    def test_duplicate_last_wins_and_is_reported(self):
        found = lt.parse("# めも\nL1,12\nLVC,119\nL1,13\n")
        self.assertEqual(found.of("L1"), 13.0)
        self.assertEqual(len(found.problems), 1)
        # **どの行と重なっているか**まで言う(直す場所が分かるように)
        self.assertIn("2行目", found.problems[0].reason)
        self.assertEqual(found.problems[0].line_no, 4)

    def test_duplicate_matches_across_spellings(self):
        # `L-1` と `L1` は同じライン。**別物として両方入れない**
        found = lt.parse("L-1,12\nL1,13\n")
        self.assertEqual(found.values, {"L-1": 13.0})
        self.assertEqual(len(found.problems), 1)

    def test_line_name_made_only_of_symbols_is_reported(self):
        found = lt.parse("-,12\n")
        self.assertEqual(found.values, {})
        self.assertIn("ライン名", found.problems[0].reason)

    def test_missing_line_name_is_reported(self):
        found = lt.parse(",12\n")
        self.assertEqual(found.values, {})
        self.assertEqual(len(found.problems), 1)
        self.assertEqual(found.problems[0].line_no, 1)

    def test_bom_on_first_line(self):
        found = lt.parse("﻿L1,12\n")
        self.assertEqual(found.values, {"L-1": 12.0})

    def test_of_uses_normalized_name(self):
        found = lt.parse("L-1,12\n")
        self.assertEqual(found.of("L1"), 12.0)
        self.assertEqual(found.of("l 1"), 12.0)

    def test_of_returns_none_for_unknown(self):
        self.assertIsNone(lt.parse("L1,12\n").of("MARU"))

    def test_problem_text_is_plain_japanese(self):
        # 画面へそのまま出す文言。**Markdown の印を混ぜない**
        for text in ("L1\n", "L1,x\n", "L1,-1\n", ",1\n"):
            for problem in lt.parse(text).problems:
                self.assertNotIn("**", problem.reason)

    def test_source_is_carried(self):
        found = lt.parse("L1,12\n", source="/共有/目標.csv")
        self.assertEqual(found.source, "/共有/目標.csv")
        self.assertEqual(found.as_dict()["source"], "/共有/目標.csv")


class MergeTests(unittest.TestCase):
    """2つの出どころを重ねる。**CSVが勝つ。ただしラインごと。**

    利用者の言葉:「DBから目標を読む手段とCSVから読む手段2つ用意して
    ほしいだけだよ / 両方あったらCSVが優先 / DBの書き換えより
    CSVの書き換えのほうが簡単だから」
    """

    def merged(self, master: dict, csv: dict) -> lt.Targets:
        return lt.merge(lt.Targets(values=master, source="マスタ"),
                        lt.Targets(values=csv, source="目標.csv"),
                        base_name=lt.FROM_MASTER, top_name=lt.FROM_CSV)

    def test_同じラインならCSVが勝つ(self):
        found = self.merged({"L-1": 10.0}, {"L-1": 777.0})
        self.assertEqual(found.of("L1"), 777.0)
        self.assertEqual(found.origin_of("L1"), lt.FROM_CSV)

    def test_CSVに無いラインはマスタが残る(self):
        """**ここが肝。** ファイルごとの勝ち負けにすると、CSVに1行
        書いた瞬間に他のラインの目標が全部消えます ── 使うには全部を
        書き写すことになり、「CSVのほうが簡単」が成り立ちません。
        """
        found = self.merged({"L-1": 10.0, "LVC": 20.0}, {"L-1": 777.0})
        self.assertEqual(found.of("LVC"), 20.0)
        self.assertEqual(found.origin_of("LVC"), lt.FROM_MASTER)

    def test_マスタに無いラインもCSVで足せる(self):
        found = self.merged({"L-1": 10.0}, {"中板": 5.0})
        self.assertEqual(found.of("中板"), 5.0)
        self.assertEqual(found.origin_of("中板"), lt.FROM_CSV)

    def test_片方が空でも落ちない(self):
        self.assertEqual(self.merged({}, {"L-1": 1.0}).of("L1"), 1.0)
        self.assertEqual(self.merged({"L-1": 1.0}, {}).of("L1"), 1.0)
        self.assertEqual(self.merged({}, {}).values, {})

    def test_困りごとは両方ぶん残す(self):
        """片方が読めていても、もう片方が読めなかったことは知りたい。"""
        master = lt.Targets(values={"L-1": 1.0},
                            problems=[lt.Problem(0, "マスタ", "読めません")])
        csv = lt.Targets(values={}, problems=[lt.Problem(2, "x", "数ではない")])
        found = lt.merge(master, csv, base_name=lt.FROM_MASTER,
                         top_name=lt.FROM_CSV)
        self.assertEqual(len(found.problems), 2)

    def test_出どころは辞書にも出る(self):
        found = self.merged({"L-1": 10.0, "LVC": 20.0}, {"L-1": 777.0})
        self.assertEqual(found.as_dict()["origins"],
                         {"L-1": lt.FROM_CSV, "LVC": lt.FROM_MASTER})


class FromRowsTests(unittest.TestCase):
    """マスタの行から作る(取り込みの下ごしらえ)。"""

    def test_master_rows(self):
        found = lt.from_rows([{"管理番号": 1, "ライン": "L-1", "目標": 12},
                              {"管理番号": 2, "ライン": "LVC", "目標": 119}])
        self.assertEqual(found.values, {"L-1": 12.0, "LVC": 119.0})

    def test_blank_rows_are_skipped(self):
        found = lt.from_rows([{"ライン": "", "目標": ""},
                              {"ライン": "L1", "目標": 12}])
        self.assertEqual(found.values, {"L-1": 12.0})


class AsCsvTests(unittest.TestCase):
    def test_order_is_respected(self):
        found = lt.parse("LVC,119\nL1,12\n")
        text = lt.as_csv(found, order=("L-1", "LVC"))
        self.assertEqual(text.splitlines(), ["L-1,12", "LVC,119"])

    def test_names_outside_the_order_are_kept(self):
        # 知らない名前でも**捨てない** ── 書いた人の意図が消える
        found = lt.parse("予備PC,500\n")
        text = lt.as_csv(found, order=("L-1",))
        self.assertIn("500", text)

    def test_note_becomes_comments(self):
        text = lt.as_csv(lt.parse("L1,12\n"), note="めも\n2行目")
        self.assertTrue(text.startswith("# めも\n# 2行目\n"))

    def test_integers_stay_integers(self):
        self.assertIn("L-1,12", lt.as_csv(lt.parse("L-1,12\n")))

    def test_round_trip(self):
        first = lt.parse("L1,12\nLVC,119.5\n")
        again = lt.parse(lt.as_csv(first))
        self.assertEqual(again.values, first.values)


class TargetLineTests(unittest.TestCase):
    """45度線の形。**VBAどおり。**"""

    def test_cumulative_rises_by_the_target_each_day(self):
        # targetData(k) = 累積目標 + (k-1)*累積上昇、どちらも「目標」
        self.assertEqual(lt.cumulative_line(12, 4), [12.0, 24.0, 36.0, 48.0])

    def test_flat_stays_at_the_target(self):
        # 直枚上昇 = 0。**1直あたりに割らない**
        self.assertEqual(lt.flat_line(12, 3), [12.0, 12.0, 12.0])

    def test_no_target_draws_nothing(self):
        self.assertEqual(lt.cumulative_line(None, 5), [])
        self.assertEqual(lt.cumulative_line(0, 5), [])
        self.assertEqual(lt.flat_line(None, 5), [])
        self.assertEqual(lt.flat_line(0, 5), [])

    def test_no_points_draws_nothing(self):
        self.assertEqual(lt.cumulative_line(12, 0), [])
        self.assertEqual(lt.flat_line(12, -1), [])


# ======================================================================
# 読み込みと取り込み (services/targets.py)
# ======================================================================
class LoadTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def write(self, text: str, encoding: str = "utf-8") -> Path:
        path = self.dir / "ライン毎目標.csv"
        path.write_text(text, encoding=encoding)
        return path

    def test_utf8(self):
        from nippou.services import targets

        found = targets.load_csv(self.write("L1,12\n"))
        self.assertEqual(found.values, {"L-1": 12.0})

    def test_utf8_with_bom(self):
        from nippou.services import targets

        found = targets.load_csv(self.write("L1,12\n", encoding="utf-8-sig"))
        self.assertEqual(found.values, {"L-1": 12.0})

    def test_cp932_from_notepad(self):
        # 現場のメモ帳は既定が cp932 のことがある。ここで落とすと
        # 「直したのに読まれない」になる
        from nippou.services import targets

        found = targets.load_csv(self.write("# 目標\nL1,12\n", encoding="cp932"))
        self.assertEqual(found.values, {"L-1": 12.0})

    def test_missing_file_is_empty_not_an_error(self):
        """CSVが無いのは**困りごとではありません。**

        目標はマスタとCSVの両方から読み、同じラインがあればCSVが勝ちます。
        CSVは「マスタと違う値にしたいとき」に置くものなので、無ければ
        マスタの値がそのまま効くだけです。
        """
        from nippou.services import targets

        found = targets.load_csv(self.dir / "ない.csv")
        self.assertEqual(found.values, {})
        self.assertEqual(len(found.problems), 1)
        # 行の困りごとではない。**ファイルまるごとの話**
        self.assertEqual(found.problems[0].line_no, 0)
        self.assertIn("マスタの値", found.problems[0].reason)

    def test_of_shortcut(self):
        from nippou.services import targets

        path = self.write("L-1,12\n")
        self.assertEqual(targets.of("L1", path), 12.0)
        self.assertIsNone(targets.of("MARU", path))


class MasterImportTests(unittest.TestCase):
    """マスタ → CSV。**呼び名の違いはここでだけ吸収する。**"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.master = self.dir / "梱包資材マスタ.sqlite3"
        conn = sqlite3.connect(str(self.master))
        conn.execute('CREATE TABLE "ライン毎目標" '
                     '("管理番号" INTEGER, "ライン" TEXT, "目標" INTEGER)')
        conn.executemany('INSERT INTO "ライン毎目標" VALUES (?,?,?)', [
            (1, "L-1", 12), (2, "LVC", 119), (3, "HVC", 50),
            (4, "機側", 100), (5, "NS1", 10), (6, "ﾄｯﾄ", 10),
            (7, "ﾊﾞﾗﾝｻｰ", 10), (8, "予備PC", 500)])
        conn.commit()
        conn.close()

    def point_settings_here(self):
        """`SETTINGS` の参照先をこの一時フォルダへ向ける。"""
        from nippou import config, user_settings

        before = user_settings.get(config.KEY_REFERENCE_DIR)
        user_settings.save_many({config.KEY_REFERENCE_DIR: str(self.dir)})
        self.addCleanup(user_settings.save_many,
                        {config.KEY_REFERENCE_DIR: before or ""})

    def test_master_names_are_renamed_to_tool_names(self):
        from nippou.services import targets

        self.point_settings_here()
        found = targets.read_master()
        # 機側 / ﾄｯﾄ → トット / ﾊﾞﾗﾝｻｰ → バランサー(ツールの名前 = 正規の呼び名。v4.13.0)
        self.assertEqual(found.values["機側"], 100.0)
        self.assertEqual(found.values["トット"], 10.0)
        self.assertEqual(found.values["バランサー"], 10.0)
        # L-1 も同じ定義の表で(`logic/line_names`)。突き合わせる形は記号を外した L1
        self.assertEqual(found.of("L-1"), 12.0)

    # -- 出どころが2つあるとき ------------------------------------------
    def test_CSVが無くてもマスタの目標が効く(self):
        """**取り込みボタンを押さなくても、マスタの値が出ます。**

        以前は読むのがCSV1本だけで、押すまでマスタの目標は使われません
        でした ── 押し忘れれば、マスタに入れたのに線が出ません。
        """
        from nippou.config import SETTINGS
        from nippou.services import targets

        self.point_settings_here()
        self.assertFalse(SETTINGS.line_target_path.exists())
        found = targets.load()
        self.assertEqual(found.of("L1"), 12.0)
        self.assertEqual(found.origin_of("L1"), lt.FROM_MASTER)

    def test_CSVに書いた行だけがマスタに勝つ(self):
        """1行だけ書き換えれば済む ── それが「CSVのほうが簡単」の中身。"""
        from nippou.config import SETTINGS
        from nippou.services import targets

        self.point_settings_here()
        SETTINGS.line_target_path.write_text("L1,777\n", encoding="utf-8")
        found = targets.load()
        self.assertEqual(found.of("L1"), 777.0)          # CSVが勝つ
        self.assertEqual(found.origin_of("L1"), lt.FROM_CSV)
        self.assertEqual(found.of("LS"), 100.0)          # 他はマスタのまま
        self.assertEqual(found.origin_of("LS"), lt.FROM_MASTER)

    def test_マスタが読めなくてもCSVで出る(self):
        from nippou.config import SETTINGS
        from nippou.services import targets

        self.point_settings_here()
        SETTINGS.line_target_path.write_text("L1,777\n", encoding="utf-8")
        self.master.unlink()
        self.assertEqual(targets.load().of("L1"), 777.0)

    def test_グラフもその値を使う(self):
        """**画面まで届いていること。** 読めているだけでは足りない。"""
        from nippou.config import SETTINGS
        from nippou.services import targets

        self.point_settings_here()
        SETTINGS.line_target_path.write_text("L1,777\n", encoding="utf-8")
        self.assertEqual(targets.of("L1"), 777.0)
        self.assertEqual(targets.of("LS"), 100.0)

    def test_import_writes_a_csv_that_reads_back(self):
        from nippou.config import SETTINGS
        from nippou.services import targets

        self.point_settings_here()
        written = targets.import_from_master()
        self.assertTrue(SETTINGS.line_target_path.is_file())
        back = targets.load()
        self.assertEqual(back.values, written.values)
        self.assertEqual(back.of("LS"), 100.0)

    def test_import_writes_tool_names(self):
        from nippou.config import SETTINGS
        from nippou.services import targets

        self.point_settings_here()
        targets.import_from_master()
        text = SETTINGS.line_target_path.read_text(encoding="utf-8")
        # **CSVはツールの名前で書く。** 現場が直すときに、画面に出ている
        # ラインと同じ綴りでないと探せない
        self.assertIn("機側,100", text)
        self.assertNotIn("LS", text)                             # 前の名前では書かない(v4.13.0)
        self.assertIn("トット,10", text)
        self.assertNotIn("ﾄｯﾄ", text)                            # 字の幅も正規に揃える

    def test_ラインのつもりで読めない呼び名は読まずに言う(self):
        """正規しか読まない(v4.12.3)。L1 と書いても L1 にはしない ── 気づけるように言う。"""
        from nippou.services import targets

        conn = sqlite3.connect(str(self.master))
        conn.execute('UPDATE "ライン毎目標" SET "ライン"=\'L1\' WHERE "ライン"=\'L-1\'')
        conn.commit()
        conn.close()
        self.point_settings_here()
        found = targets.read_master()
        self.assertNotIn("L-1", found.values)
        self.assertEqual([(p.line_no, p.text) for p in found.problems], [(1, "L1")])
        self.assertIn("正規は「L-1」", found.problems[0].reason)
        self.assertEqual(found.values["機側"], 100.0)            # ほかは読める

    def test_import_keeps_unknown_master_names(self):
        from nippou.config import SETTINGS
        from nippou.services import targets

        self.point_settings_here()
        targets.import_from_master()
        text = SETTINGS.line_target_path.read_text(encoding="utf-8")
        # 「予備PC」はツールのラインに無いが、**消さない**
        self.assertIn("予備PC,500", text)

    def test_unreadable_master_leaves_the_csv_alone(self):
        from nippou.config import SETTINGS
        from nippou.services import targets

        self.point_settings_here()
        SETTINGS.line_target_path.write_text("L1,999\n", encoding="utf-8")
        self.master.unlink()
        found = targets.import_from_master()
        self.assertEqual(found.values, {})
        self.assertTrue(found.problems)
        # **読めないマスタで、いま効いている目標を消さない**
        self.assertEqual(targets.load().of("L1"), 999.0)


if __name__ == "__main__":
    unittest.main()
