"""アクセス権限の mode:fullaccess と、左のタブ (v4.25.0)

    マスタ：アクセス権限の追加 mode:fullaccess
    日報管理ツールでの左側のタブが
    mode:fullaccessであれば現状の通り1～8まで全表示
    mode:fullaccessがついていない場合 1，2，3，5，8だけの表示

- このPCの行に `mode:fullaccess`(か Administrator)があれば 1〜8 全部
- 表を読めたのに無ければ 1 日報入力・2 梱包資材重量計算・3 VC長さ計算・5 集計・グラフ・
  8 設定・管理者 だけ。**番号は元のまま**(4・6・7 が抜ける)
- 表が無い・読めない・まだ読んでいないときは、これまでどおり全部
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import access_rights as logic  # noqa: E402
from nippou.services import access_rights  # noqa: E402
from tests._web import WebTestCase  # noqa: E402

ME = logic.Identity(login="genba", pc="LINE-PC-01")


def rows(*rights: str) -> list[dict[str, str]]:
    return [{"管理番号": str(i), "ログインID": "genba", "PC名": "LINE-PC-01", "権限": r, "有効": "1"}
            for i, r in enumerate(rights, 1)] + [
        {"管理番号": "99", "ログインID": "genba", "PC名": "OTHER-PC", "権限": "mode:fullaccess", "有効": "1"}]


class DecideTests(unittest.TestCase):
    def test_mode_fullaccess_を読む(self) -> None:
        d = logic.decide(rows("HVC", "mode:fullaccess"), ME)
        self.assertTrue(d.full_access)
        self.assertEqual(d.line, "HVC", "ラインはこれまでどおり決まる")
        self.assertNotIn("mode:fullaccess", " ".join(d.ignored), "読まなかった権限に並べない")
        self.assertIn("mode:fullaccess", d.summary())

    def test_全角や大文字でも読む_1つの欄に並べても読む(self) -> None:
        self.assertTrue(logic.decide(rows("ｍｏｄｅ：ＦｕｌｌＡｃｃｅｓｓ"), ME).full_access)
        self.assertTrue(logic.decide(rows("HVC, mode:fullaccess"), ME).full_access)

    def test_ほかのPCの行の_fullaccess_は効かない(self) -> None:
        d = logic.decide(rows("HVC", "mode:field"), ME)
        self.assertFalse(d.full_access)

    def test_有効が0の行は読まない(self) -> None:
        table = rows("HVC") + [{"管理番号": "5", "ログインID": "genba", "PC名": "LINE-PC-01",
                                "権限": "mode:fullaccess", "有効": "0"}]
        self.assertFalse(logic.decide(table, ME).full_access)

    def test_点検で言わない(self) -> None:
        findings = logic.inspect(rows("HVC", "mode:fullaccess"))
        self.assertFalse([f for f in findings if "fullaccess" in f.message])


class AllTabsTests(unittest.TestCase):
    def status(self, table: list[dict[str, str]]) -> access_rights.Status:
        return access_rights.Status(decision=logic.decide(table, ME), identity=ME,
                                    source="梱包資材マスタ の アクセス権限", checked=len(table))

    def test_fullaccess_か_Administrator_なら全部(self) -> None:
        self.assertTrue(access_rights.all_tabs(self.status(rows("HVC", "mode:fullaccess"))))
        self.assertTrue(access_rights.all_tabs(self.status(rows("Administrator"))))

    def test_無ければ絞る_このPCの行が無くても絞る(self) -> None:
        self.assertFalse(access_rights.all_tabs(self.status(rows("HVC", "mode:field"))))
        other = [{"管理番号": "1", "ログインID": "genba", "PC名": "OTHER-PC", "権限": "HVC", "有効": "1"}]
        self.assertFalse(access_rights.all_tabs(self.status(other)))

    def test_表を読めない_まだ読んでいないなら全部(self) -> None:
        self.assertTrue(access_rights.all_tabs(access_rights.Status(problem="表がありません")))
        with mock.patch.object(access_rights, "_status", None):
            self.assertTrue(access_rights.all_tabs())


def live_rights():
    """いま読み込まれている `services.access_rights`(試験の土台が読み直すことがある)。"""
    from nippou.services import access_rights as current

    return current


class RailTests(WebTestCase):
    def rail(self) -> list[tuple[str, str]]:
        html = self.get("/settings").get_data(as_text=True)
        nav = html[html.index('<nav class="rail"'):html.index("</nav>", html.index('<nav class="rail"'))]
        return re.findall(r'<span class="n" aria-hidden="true">(\d+)</span>\s*<span class="rail__text">\s*'
                          r'<b class="label">([^<]+)</b>', nav)

    def test_fullaccess_なら1から8まで全部(self) -> None:
        with mock.patch.object(live_rights(), "all_tabs", return_value=True):
            got = self.rail()
        self.assertEqual([n for n, _ in got], [str(i) for i in range(1, 9)])

    def test_無ければ_1_2_3_5_8_だけ_番号は元のまま(self) -> None:
        with mock.patch.object(live_rights(), "all_tabs", return_value=False):
            got = self.rail()
        self.assertEqual(got, [("1", "日報入力"), ("2", "梱包資材重量計算"), ("3", "VC長さ計算"),
                               ("5", "集計・グラフ"), ("8", "設定・管理者")])

    def test_設定の画面に左のタブの状態が出る(self) -> None:
        with mock.patch.object(live_rights(), "all_tabs", return_value=False):
            html = self.get("/settings").get_data(as_text=True)
        self.assertIn('id="access-tabs"', html)


if __name__ == "__main__":
    unittest.main()
