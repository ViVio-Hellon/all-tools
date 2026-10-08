"""タブ表示権限の決め方(`portal/tab_rights.py`)

python-web-tools のアクセス権限と同じ考え方: 空欄は「問わない」・大文字小文字と
全角半角は区別しない・当てはまる行を全部足す・両方空の行は効かない・有効=0 は飛ばす。
表がまだ無い・1行も無い・読めないときは**全部出す**(ラインを止めない)。
表に行があるのにこの端末の行が無ければ、ツールのタブは出さない(大設定だけ)。
"""
from __future__ import annotations

import unittest

import tests  # noqa: F401  (一時フォルダへ向ける)

from portal import catalog as catalog_mod
from portal import tab_rights
from portal.identity import Identity
from portal.tab_rights import Rule, decide

CATALOG = catalog_mod.load()
ALL = ["nippou", "kanban", "calendar", "inspection"]
ME = Identity("yamada", "LINE1-PC")


def rule(login="", pc="", tabs="", default="", enabled=True, key=None) -> Rule:
    return Rule(key=key, login_id=login, pc_name=pc, tabs=tabs, default_tab=default, enabled=enabled)


class CatalogTests(unittest.TestCase):
    def test_並びは大きなタブの並び(self) -> None:
        self.assertEqual(CATALOG.ids(), ALL)

    def test_名前_短い名前_別名_英字のどれでも引ける(self) -> None:
        for word, tool in (("日報", "nippou"), ("日報管理ツール", "nippou"), ("看板システム", "kanban"),
                           ("ＫＡＮＢＡＮ", "kanban"), ("カレンダー", "calendar"), ("暦", "calendar"),
                           ("点検表", "inspection"), ("点検表 選択・印刷", "inspection")):
            with self.subTest(word=word):
                found = CATALOG.find(word)
                self.assertIsNotNone(found)
                self.assertEqual(found.id, tool)
        self.assertIsNone(CATALOG.find("ほげ"))
        self.assertIsNone(CATALOG.find(""))


class DecideTests(unittest.TestCase):
    def test_表が無い_読めない_1行も無いときは全部出す(self) -> None:
        for state in ("missing", "unreadable"):
            with self.subTest(state=state):
                d = decide([], ME, CATALOG, table_state=state)
                self.assertEqual(d.tabs, ALL)
        d = decide([], ME, CATALOG)
        self.assertEqual((d.tabs, d.source), (ALL, tab_rights.SOURCE_UNCONFIGURED))
        # 効かない行(両方空・無効)しか無いのも「まだ運用前」
        d = decide([rule(tabs="日報"), rule(pc="X", tabs="看板", enabled=False)], ME, CATALOG)
        self.assertEqual((d.tabs, d.source), (ALL, tab_rights.SOURCE_UNCONFIGURED))

    def test_行があるのにこの端末の行が無ければツールのタブは出さない(self) -> None:
        d = decide([rule(pc="LINE2-PC", tabs="日報")], ME, CATALOG)
        self.assertEqual(d.tabs, [])
        self.assertEqual(d.source, tab_rights.SOURCE_UNREGISTERED)
        self.assertIn("LINE1-PC", d.reason)
        self.assertIn("yamada", d.reason)

    def test_空欄は問わない_大文字小文字と全角半角は区別しない(self) -> None:
        rules = [rule(pc="ｌｉｎｅ1-pc", tabs="日報"), rule(login="YAMADA", tabs="看板")]
        d = decide(rules, ME, CATALOG)
        self.assertEqual(d.tabs, ["nippou", "kanban"])
        self.assertEqual(d.source, tab_rights.SOURCE_ROWS)

    def test_IDとPCの両方がある行は両方合うときだけ(self) -> None:
        rules = [rule(login="yamada", pc="LINE2-PC", tabs="カレンダー"), rule(pc="LINE1-PC", tabs="日報")]
        self.assertEqual(decide(rules, ME, CATALOG).tabs, ["nippou"])
        self.assertEqual(decide(rules, Identity("yamada", "LINE2-PC"), CATALOG).tabs, ["calendar"])

    def test_当てはまる行を全部足し_並びはいつも同じ(self) -> None:
        rules = [rule(pc="LINE1-PC", tabs="点検表、日報"), rule(login="yamada", tabs="看板・日報")]
        self.assertEqual(decide(rules, ME, CATALOG).tabs, ["nippou", "kanban", "inspection"])

    def test_すべて_と区切りの揺れ(self) -> None:
        for text in ("すべて", "全部", "ALL", "*"):
            with self.subTest(text=text):
                self.assertEqual(decide([rule(pc="LINE1-PC", tabs=text)], ME, CATALOG).tabs, ALL)
        text = "日報,看板 カレンダー／点検表"
        self.assertEqual(decide([rule(pc="LINE1-PC", tabs=text)], ME, CATALOG).tabs, ALL)

    def test_両方空の行は書かれていても効かない(self) -> None:
        rules = [rule(tabs="すべて"), rule(pc="LINE1-PC", tabs="日報")]
        self.assertEqual(decide(rules, ME, CATALOG).tabs, ["nippou"])

    def test_有効が0の行は飛ばす(self) -> None:
        rules = [rule(pc="LINE1-PC", tabs="看板", enabled=False), rule(login="yamada", tabs="日報")]
        self.assertEqual(decide(rules, ME, CATALOG).tabs, ["nippou"])
        flags = {"0": False, "false": False, "無効": False, "×": False, "": True, "1": True, "-1": True}
        for text, expected in flags.items():
            with self.subTest(text=text):
                row = {"ログインID": "", "PC名": "X", "表示タブ": "日報", "有効": text}
                self.assertEqual(Rule.from_row(row).enabled, expected)

    def test_既定タブはいちばん絞った行のもの(self) -> None:
        rules = [rule(login="yamada", tabs="すべて", default="看板"),
                 rule(pc="LINE1-PC", tabs="日報、カレンダー", default="カレンダー"),
                 rule(login="yamada", pc="LINE1-PC", tabs="日報", default="日報")]
        self.assertEqual(decide(rules, ME, CATALOG).default_tab, "nippou")
        rules = rules[:2]
        self.assertEqual(decide(rules, ME, CATALOG).default_tab, "calendar", "PC だけ > ID だけ")
        # 出していないタブは既定にしない
        d = decide([rule(pc="LINE1-PC", tabs="日報", default="看板")], ME, CATALOG)
        self.assertEqual(d.default_tab, "")

    def test_出すタブが書かれていない行(self) -> None:
        d = decide([rule(pc="LINE1-PC", tabs="")], ME, CATALOG)
        self.assertEqual(d.tabs, [])
        self.assertEqual(d.source, tab_rights.SOURCE_ROWS)
        self.assertIn("書かれていません", d.reason)


class ProblemsTests(unittest.TestCase):
    def test_書き方の問題を挙げる_打ち間違いには候補を出す(self) -> None:
        rules = [rule(key=1, tabs="日報"), rule(key=2, pc="X", tabs="カレンダ, ほげ", default="ふが")]
        found = "\n".join(tab_rights.problems(rules, CATALOG))
        self.assertIn("管理番号 1: ログインIDもPC名も空", found)
        self.assertIn("「カレンダ」が分かりません(「カレンダー」のことですか?)", found)
        self.assertIn("「ほげ」が分かりません", found)
        self.assertIn("既定タブの「ふが」", found)

    def test_画面で選んだタブは表の書き方にする(self) -> None:
        self.assertEqual(tab_rights.canonical_tabs(["kanban", "nippou"], CATALOG), "日報, 看板")
        self.assertEqual(tab_rights.canonical_tabs(ALL, CATALOG), "すべて")
        self.assertEqual(tab_rights.canonical_tabs([], CATALOG), "")


    def test_直した行の表示タブは_チェックを変えていなければそのまま(self) -> None:
        """知らない語(「在庫」)・名前の並び・すべて を、備考だけ直したときに書き換えない。"""
        edited = tab_rights.edited_tabs
        self.assertEqual(edited("日報, 在庫", ["nippou"], CATALOG), "日報, 在庫")
        self.assertEqual(edited("日報・看板・カレンダー・点検表", ALL, CATALOG), "日報・看板・カレンダー・点検表")
        self.assertEqual(edited("すべて", ALL, CATALOG), "すべて")
        # チェックを変えたら書き直す。知らない語は後ろに残す
        self.assertEqual(edited("日報, 在庫", ["nippou", "kanban"], CATALOG), "日報, 看板, 在庫")
        self.assertEqual(edited("日報, 在庫", [], CATALOG), "在庫")
        # 全部にしても、名前で並べていた行は名前のまま(「すべて」は後で足すツールにも効く)
        self.assertEqual(edited("日報", ALL, CATALOG), "日報, 看板, カレンダー, 点検表")
        # もとが「すべて」なら、外したときは名前で、全部に戻したら「すべて」
        self.assertEqual(edited("すべて", ["nippou"], CATALOG), "日報")
        self.assertEqual(edited("すべて, 在庫", ["nippou"], CATALOG), "日報, 在庫")


if __name__ == "__main__":
    unittest.main()
