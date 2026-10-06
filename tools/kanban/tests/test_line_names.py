"""ライン名(アクセス権限の「権限」から担当ラインを決める)。日報と同じ定義・同じ読み方。"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kanban import access_control as ac
from kanban import config, line_names
from kanban.db.store import Store

from test_web_app import RouteTestBase

ME = ac.Identity("genba", "PC-L1")


def rule(permission, login="genba", pc="PC-L1", no=1, enabled=True):
    return ac.Rule(no=no, login_id=login, pc_name=pc, permission=permission, enabled=enabled)


class DefinitionTest(unittest.TestCase):
    def test_the_table(self):
        """しっかり定義(正規 → このツールの看板_<コード>)。トット・バランサーは同じ 大板小板。"""
        self.assertEqual([(d.official, d.code) for d in line_names.DEFINITIONS], [
            ("L-1", "L1"), ("LVC", "LVC"), ("HVC", "HVC"), ("機側", "LS"), ("AIM", "AIM"),
            ("コイル", "コイル"), ("トット", "大板小板"), ("バランサー", "大板小板"), ("NS1", "NS1")])

    def test_every_line_of_this_tool_has_an_official_name(self):
        """このツールの看板(看板_AIM / LVC / HVC / コイル / 大板小板 / L1 / LS …)は、どれも正規の名前で書ける。"""
        for code in config.all_line_codes():
            self.assertTrue(line_names.officials_of(code), code)
        self.assertEqual(line_names.officials_of("大板小板"), ["トット", "バランサー"])
        self.assertTrue(all(row["supported"] for row in line_names.table_rows()))

    def test_only_the_official_names_are_read(self):
        for text, code in (("L-1", "L1"), ("機側", "LS"), ("ＬＶＣ", "LVC"), ("ｌ-1".upper(), "L1"),
                           (" HVC ", "HVC"), ("ﾄｯﾄ", "大板小板"), ("ﾊﾞﾗﾝｻｰ", "大板小板"), ("Ｌ－１", "L1"),
                           ("コイル", "コイル"), ("ｺｲﾙ", "コイル"), ("AIM", "AIM")):
            self.assertEqual(line_names.by_official(text).code, code, text)

    def test_not_read(self):
        """このツールの表の名前(L1・LS・大板小板)・記号/大文字小文字/空白違いは読まない。"""
        for text in ("L1", "LS", "大板小板", "l-1", "ｌ―１", "L 1", "L―1", "lvc", "TOT", "BALA",
                     "mode:field", "作業長"):
            self.assertIsNone(line_names.by_official(text), text)

    def test_hints(self):
        for text, official in (("L1", "L-1"), ("LS", "機側"), ("l-1", "L-1"), ("ｌ―１", "L-1"), ("L 1", "L-1"),
                               ("lvc", "LVC"), ("大板小板", "トット」か「バランサー")):
            self.assertEqual(line_names.hint(text), f"正規は「{official}」", text)
        for text in ("コイル", "mode:field", "作業長", "L-1", "トット"):
            self.assertEqual(line_names.hint(text), "", text)

    def test_tokens(self):
        self.assertEqual(line_names.tokens("L-1、mode:field / HVC,AIM"), ["L-1", "mode:field", "HVC", "AIM"])
        self.assertEqual(line_names.tokens("L 1"), ["L 1"], "空白では区切らない")


class DecideTest(unittest.TestCase):
    def test_one_line(self):
        g = ac.resolve([rule("mode:field"), rule("L-1", no=2)], ME)
        self.assertEqual((g.line, g.lines_found, g.line_problem, g.ignored), ("L1", ("L-1",), "", ()))
        self.assertTrue(g.allows(config.MODE_SITE), "モードの読み方は変わらない")

    def test_width_only_is_the_same(self):
        self.assertEqual(ac.resolve([rule("ＬＶＣ")], ME).line, "LVC")

    def test_unread_values_are_listed_with_the_right_spelling(self):
        g = ac.resolve([rule("L1"), rule("ｌ―１"), rule("大板小板"), rule("作業長"), rule("mode:field")], ME)
        self.assertEqual(g.line, "")
        self.assertEqual(g.ignored, ("L1(正規は「L-1」)", "ｌ―１(正規は「L-1」)",
                                     "大板小板(正規は「トット」か「バランサー」)", "作業長"))

    def test_two_lines_decide_nothing(self):
        g = ac.resolve([rule("L-1"), rule("HVC")], ME)
        self.assertEqual(g.line, "")
        self.assertIn("2 つ", g.line_problem)
        self.assertEqual(ac.resolve([rule("L-1、L-1")], ME).line, "L1", "同じラインが 2 回は 1 つ")

    def test_tot_and_balancer_are_the_same_kanban_here(self):
        """トット・バランサーはどちらも 看板_大板小板。両方書いてあっても同じラインなので決まる。"""
        self.assertEqual(ac.resolve([rule("トット")], ME).line, "大板小板")
        self.assertEqual(ac.resolve([rule("バランサー")], ME).line, "大板小板")
        g = ac.resolve([rule("トット、バランサー")], ME)
        self.assertEqual((g.line, g.line_problem), ("大板小板", ""))
        self.assertEqual(ac.resolve([rule("コイル")], ME).line, "コイル")
        self.assertEqual(ac.resolve([rule("機側")], ME).line, "LS")

    def test_administrator_can_use_both_modes(self):
        """権限: Administrator は現場・倉庫の両方(日報と同じ書き方・幅と大文字小文字は問わない)。"""
        for text in ("Administrator", "administrator", "ＡＤＭＩＮＩＳＴＲＡＴＯＲ", " Administrator "):
            g = ac.resolve([rule(text)], ME)
            self.assertTrue(g.allows(config.MODE_SITE) and g.allows(config.MODE_WAREHOUSE), text)
            self.assertTrue(g.admin)
            self.assertEqual((g.line, g.ignored), ("", ()), "ラインでも読まなかった値でもない")
        g = ac.resolve([rule("Administrator、L-1")], ME)
        self.assertEqual((g.allows(config.MODE_WAREHOUSE), g.line), (True, "L1"), "担当ラインと並べて書ける")
        modes = {m["key"]: m for m in g.to_dict()["modes"]}
        self.assertEqual(modes[config.MODE_WAREHOUSE]["via"], "Administrator", "画面: 使えます(Administrator)")
        self.assertIn("管理者", rule("Administrator").meaning())
        self.assertEqual(ac.problems([rule("Administrator", no=9)]), [], "打ち間違い扱いしない")
        g = ac.resolve([rule("Admin")], ME)
        self.assertFalse(g.allows(config.MODE_WAREHOUSE), "Administrator だけ(略さない)")

    def test_rows_of_other_pcs_and_disabled_rows_do_not_count(self):
        g = ac.resolve([rule("HVC", pc="PC-HVC"), rule("L-1", enabled=False), rule("LVC")], ME)
        self.assertEqual(g.line, "LVC")

    def test_whole_table_check(self):
        out = ac.problems([rule("L1", no=3), rule("L-1", no=4), rule("HVC", no=5), rule("作業長", no=6),
                           rule("大板小板", no=7)])
        joined = "\n".join(out)
        self.assertIn("管理番号 3: 「L1」は読みません(正規は「L-1」)", joined)
        self.assertIn("管理番号 7: 「大板小板」は読みません(正規は「トット」か「バランサー」)", joined)
        self.assertIn("genba / PC-L1: HVC・L-1", joined)
        self.assertNotIn("作業長", joined, "ラインのつもりでない値は言わない")


class ApplyTest(unittest.TestCase):
    """表のラインが前に当てたものから変わったときだけ当てる。"""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="kanban_line_"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.store = Store(str(self.dir / "x.sqlite3"), host_name="PC")
        self.store.ensure_schema()
        self.addCleanup(self.store.close)

    def test_apply_once_then_keep_a_manual_change(self):
        g = ac.resolve([rule("L-1")], ME)
        g.source = "fresh"
        self.assertEqual(ac.line_to_apply(g, self.store), "L1")
        ac.mark_line_applied(self.store, "L1")
        self.assertEqual(ac.line_to_apply(g, self.store), "", "同じ表で 2 回目は当てない(手で変えた担当ラインを戻さない)")
        g2 = ac.resolve([rule("HVC")], ME)
        g2.source = "fresh"
        self.assertEqual(ac.line_to_apply(g2, self.store), "HVC", "表を書き換えたら当てる")

    def test_every_startup_uses_the_table(self):
        """起動するたびに表のライン(パスワードで変えたものは、その起動のあいだだけ)。"""
        g = ac.resolve([rule("L-1")], ME)
        g.source = "fresh"
        ac.mark_line_applied(self.store, "L1")
        self.assertEqual(ac.line_at_startup(g), "L1", "前に当てていても、起動のときは当てる")
        g.source = "cache"
        self.assertEqual(ac.line_at_startup(g), "L1", "届かなくても前回読めた表で")
        g.source = "none"
        self.assertEqual(ac.line_at_startup(g), "")
        self.assertEqual(ac.line_at_startup(ac.resolve([rule("L-1"), rule("HVC")], ME)), "")

    def test_nothing_when_the_table_is_unreachable(self):
        g = ac.resolve([rule("L-1")], ME)
        g.source = "none"
        self.assertEqual(ac.line_to_apply(g, self.store), "")


class RouteTest(RouteTestBase):
    MODE = config.MODE_SITE

    def setUp(self) -> None:
        super().setUp()
        me = ac.current_identity()
        self.packing = self.dir / "梱包資材マスタ.sqlite3"
        c = sqlite3.connect(str(self.packing))
        c.execute(ac.DDL)
        c.executemany('INSERT INTO "アクセス権限" (ログインID, PC名, 権限) VALUES (?, ?, ?)',
                      [(me.login_id, me.pc_name, "mode:field"), (me.login_id, me.pc_name, "ＨＶＣ"),
                       (me.login_id, me.pc_name, "L1")])
        c.commit()
        c.close()
        (self.dir / "config.json").write_text(json.dumps({
            "access_db_path": str(self.packing), "line": "LVC",
            "admin_password_hash": config.hash_password("秘密")}), encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {"KANBAN_LOCAL_DIR": str(self.dir / "local")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def cfg(self):
        return config.load_config(str(self.dir / "config.json"))

    def test_reading_applies_the_line_once(self):
        data = self.get("/api/access").json
        self.assertIn("HVC", data["line_applied"])
        self.assertEqual(data["current_line"], "HVC")
        self.assertEqual(self.cfg().line, "HVC")
        self.assertEqual(self.app.config["LINE"], "HVC")
        self.assertEqual(data["grant"]["ignored"], ["L1(正規は「L-1」)"])
        # 管理者パスワードで変えられる(その起動のあいだは、読み直しても戻さない)
        res = self.post("/api/line", {"line": "LVC", "password": "秘密"})
        self.assertEqual(res.status_code, 200)
        self.assertIn("次に起動すると", res.json["note"])
        self.assertIn("HVC", res.json["note"])
        data = self.get("/api/access").json
        self.assertEqual(data["line_applied"], "")
        self.assertEqual(self.cfg().line, "LVC")

    def test_page_shows_the_definitions(self):
        html = self.get("/settings").get_data(as_text=True)
        self.assertIn('id="access-line"', html)
        self.assertIn("バランサー", html)


class WarehouseRouteTest(RouteTest):
    MODE = config.MODE_WAREHOUSE
    LINE = ""

    def test_reading_applies_the_line_once(self):
        """倉庫は担当ラインを使わないので当てない(表のラインは見せる)。"""
        data = self.get("/api/access").json
        self.assertEqual(data["line_applied"], "")
        self.assertEqual(data["grant"]["line"], "HVC")
        self.assertEqual(self.cfg().line, "LVC")


class MistakeMinutesTest(RouteTestBase):
    def setUp(self) -> None:
        super().setUp()
        (self.dir / "config.json").write_text(json.dumps({"admin_password_hash": config.hash_password("秘密")}),
                                              encoding="utf-8")

    def test_setting(self):
        self.assertEqual(self.post("/api/stats/mistake-minutes", {"minutes": "10"}).status_code, 401)
        self.assertEqual(self.post("/api/stats/mistake-minutes", {"minutes": "-1", "password": "秘密"}).status_code, 400)
        res = self.post("/api/stats/mistake-minutes", {"minutes": "10", "password": "秘密"})
        self.assertEqual(res.status_code, 200, res.json)
        self.assertEqual(config.load_config(str(self.dir / "config.json")).mistake_minutes, 10)
        self.assertIn('value="10"', self.get("/settings").get_data(as_text=True))

    def test_the_minutes_change_the_count(self):
        from datetime import datetime

        from kanban.domain import events as ev
        from kanban.presenters import stats

        def e(at, kind):
            return stats.Event(at=datetime.strptime(at, "%Y/%m/%d %H:%M"), line="LVC", mgmt_no="1", kind=kind,
                               ordered_at=datetime(2026, 9, 1, 9, 0))
        evts = [e("2026/09/01 09:00", ev.ORDERED), e("2026/09/01 10:00", ev.HOLD), e("2026/09/01 10:08", ev.UNHOLD)]
        self.assertEqual(len(stats.build_cycles(evts)[0].holds), 1, "既定 5 分: 8 分は数える")
        self.assertEqual(len(stats.build_cycles(evts, 10)[0].holds), 0, "10 分: 8 分は押し間違い")
        self.assertEqual(len(stats.build_cycles(evts, 0)[0].holds), 1)


if __name__ == "__main__":
    unittest.main()
