"""アクセス権限(kanban/access_control.py)

梱包資材マスタの ``アクセス権限`` を、python-web-tools と**同じ表・同じ読み方**で
読めているかを確かめる。表の形は実物の梱包資材マスタと同じ(:data:`DDL`)。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from kanban import access_control as ac
from kanban import config
from kanban.db.store import Store

ME = ac.Identity("Yamada", "NGY-PC-0001")


def rule(login="", pc="", code="mode:field", enabled=True, no=None):
    return ac.Rule(no=no, login_id=login, pc_name=pc, permission=code, enabled=enabled)


class ResolveTest(unittest.TestCase):
    def test_union_of_matching_rows(self):
        grant = ac.resolve([rule("yamada", "", "mode:field"),
                            rule("", "ngy-pc-0001", "mode:material")], ME)
        self.assertEqual(grant.codes, {"mode:field", "mode:material"})
        self.assertEqual(grant.allowed_modes(), list(config.ALL_MODES))
        self.assertEqual(grant.reason, "")

    def test_case_is_ignored_and_blank_means_any(self):
        """Windows の ID と PC名は大文字小文字を区別しない。空欄は「問わない」。"""
        grant = ac.resolve([rule("YAMADA", "", "mode:material")], ME)
        self.assertTrue(grant.allows(config.MODE_WAREHOUSE))

    def test_both_conditions_must_match(self):
        grant = ac.resolve([rule("yamada", "OTHER-PC", "mode:material")], ME)
        self.assertFalse(grant.allows(config.MODE_WAREHOUSE))

    def test_row_with_no_condition_is_ignored(self):
        """ID も PC名 も空の行は全員への許可になるので効かせない(python-web-tools と同じ)。"""
        grant = ac.resolve([rule("", "", "mode:material")], ME)
        self.assertFalse(grant.allows(config.MODE_WAREHOUSE))

    def test_disabled_rows_do_not_count(self):
        grant = ac.resolve([rule("yamada", "", "mode:material", enabled=False)], ME)
        self.assertFalse(grant.allows(config.MODE_WAREHOUSE))

    def test_other_tools_codes_are_kept_but_not_used(self):
        """権限にはほかのツールの文字列も入る。**使わないが、当てはまった行としては見せる。**"""
        grant = ac.resolve([rule("yamada", "", "master:edit"), rule("yamada", "", "mode:material")], ME)
        self.assertEqual(grant.codes, {"mode:material"})
        self.assertEqual([r.permission for r in grant.matched], ["master:edit", "mode:material"])
        other = grant.to_dict()["matched"][0]
        self.assertFalse(other["known"])
        self.assertIn("ほかのツール", other["meaning"])

    def test_only_other_codes_falls_back_to_site(self):
        grant = ac.resolve([rule("yamada", "", "master:edit")], ME)
        self.assertEqual(grant.codes, ac.FALLBACK)
        self.assertIn("このツールのモードの行がありません", grant.reason)

    def test_unregistered_terminal_gets_site_only(self):
        """当てはまる行が無ければ現場モードだけ(締め出さない。強い権限も渡さない)。"""
        grant = ac.resolve([rule("suzuki", "", "mode:material")], ME)
        self.assertEqual(grant.allowed_modes(), [config.MODE_SITE, config.MODE_WAREHOUSE_VIEW])
        self.assertIn("登録がありません", grant.reason)

    def test_empty_table_gives_site_only(self):
        grant = ac.resolve([], ME)
        self.assertFalse(grant.has_master)
        self.assertEqual(grant.codes, ac.FALLBACK)

    def test_view_is_always_allowed(self):
        grant = ac.resolve([rule("yamada", "", "master:edit")], ME)
        self.assertTrue(grant.allows(config.MODE_WAREHOUSE_VIEW))

    def test_startup_mode_falls_back_to_the_narrower_one(self):
        site_only = ac.resolve([], ME)
        self.assertEqual(site_only.startup_mode(config.MODE_WAREHOUSE), config.MODE_SITE)
        warehouse_only = ac.resolve([rule("yamada", "", "mode:material")], ME)
        self.assertEqual(warehouse_only.startup_mode(config.MODE_SITE), config.MODE_WAREHOUSE)
        self.assertEqual(warehouse_only.startup_mode(config.MODE_WAREHOUSE_VIEW),
                         config.MODE_WAREHOUSE_VIEW)

    def test_how_to_allow_names_the_row_to_add(self):
        text = ac.resolve([], ME).how_to_allow(config.MODE_WAREHOUSE)
        for part in ("Yamada", "NGY-PC-0001", "mode:material", "アクセス権限", "マスタ管理"):
            self.assertIn(part, text)
        self.assertEqual(ac.resolve([], ME).how_to_allow(config.MODE_WAREHOUSE_VIEW), "")

    def test_parse_enabled(self):
        for value in (None, "", "1", 1, "true", "有効", "〇"):
            self.assertTrue(ac.parse_enabled(value), value)
        for value in ("0", 0, "false", "FALSE", "no", "×", "x", "無効", "off"):
            self.assertFalse(ac.parse_enabled(value), value)


class ProblemsTest(unittest.TestCase):
    def test_rows_without_condition_are_reported(self):
        out = ac.problems([rule("", "", "mode:field", no=3)])
        self.assertEqual(len(out), 1)
        self.assertIn("全員への許可", out[0])

    def test_typos_are_reported_other_tools_are_not(self):
        out = ac.problems([rule("a", "", "mode:feild", no=4), rule("a", "", "master:edit", no=5),
                           rule("a", "", "Mode:Material", no=6)])
        self.assertEqual(len(out), 1)
        self.assertIn("mode:feild → mode:field", out[0])
        self.assertIn("Mode:Material → mode:material", out[0])
        self.assertNotIn("master:edit", out[0])
        self.assertEqual(ac.near_known("master:edit"), "")
        self.assertEqual(ac.near_known("mode:field"), "")


class FileTest(unittest.TestCase):
    """梱包資材マスタのファイルを読む・表を用意する。"""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="kanban_access_"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        patcher = mock.patch.dict(os.environ, {"KANBAN_LOCAL_DIR": str(self.dir / "local")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = self.dir / "梱包資材マスタ.sqlite3"
        conn = sqlite3.connect(str(self.path))
        conn.execute('CREATE TABLE "BoardMaster" ("ID" INTEGER, "名前" TEXT)')
        conn.execute("INSERT INTO BoardMaster VALUES (1, 'ほかのツールの行')")
        conn.commit()
        conn.close()
        self.cfg = config.Config(access_db_path=str(self.path))
        self.store = Store(str(self.dir / "kanban.sqlite3"), host_name="PC")
        self.store.ensure_schema()
        self.addCleanup(self.store.close)

    def add(self, *rows):
        conn = sqlite3.connect(str(self.path))
        conn.executemany('INSERT INTO "アクセス権限" (ログインID, PC名, 権限, 有効, 備考) VALUES (?, ?, ?, ?, ?)', rows)
        conn.commit()
        conn.close()

    def test_default_path_is_next_to_the_shared_db(self):
        cfg = config.Config(shared_db_path=str(self.dir / "看板マスタ.sqlite3"))
        self.assertEqual(cfg.resolved_access_db_path(), str(self.dir / "梱包資材マスタ.sqlite3"))
        self.assertEqual(self.cfg.resolved_access_db_path(), str(self.path))

    def test_ensure_table_creates_the_same_table_as_the_real_one(self):
        """無ければ作る。**実物と同じ定義**(python-web-tools が読める形)。ほかの表には触らない。"""
        ok, message = ac.ensure_table(ac.open_db(self.cfg))
        self.assertTrue(ok)
        self.assertIn("作りました", message)
        conn = sqlite3.connect(str(self.path))
        info = [(r[1], r[2], r[3], r[4], r[5]) for r in conn.execute('PRAGMA table_info("アクセス権限")')]
        others = conn.execute("SELECT * FROM BoardMaster").fetchall()
        conn.close()
        self.assertEqual(info, [
            ("管理番号", "INTEGER", 0, None, 1),
            ("ログインID", "TEXT", 0, "''", 0),
            ("PC名", "TEXT", 0, "''", 0),
            ("権限", "TEXT", 1, None, 0),
            ("有効", "INTEGER", 0, "1", 0),
            ("備考", "TEXT", 0, "''", 0),
        ])
        self.assertEqual(others, [(1, "ほかのツールの行")])
        # 2 回目は何もしない
        self.assertEqual(ac.ensure_table(ac.open_db(self.cfg)), (True, ""))

    def test_ensure_table_refuses_the_kanban_master(self):
        kanban = self.dir / "看板マスタ.sqlite3"
        conn = sqlite3.connect(str(kanban))
        conn.execute('CREATE TABLE "看板_LVC" ("管理番号" TEXT)')
        conn.commit()
        conn.close()
        ok, message = ac.ensure_table(ac.open_db(config.Config(access_db_path=str(kanban))))
        self.assertFalse(ok)
        self.assertIn("看板マスタ", message)
        conn = sqlite3.connect(str(kanban))
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        conn.close()
        self.assertNotIn("アクセス権限", names)

    def test_path_problem(self):
        self.assertEqual(ac.path_problem(str(self.path)), "")
        self.assertIn("見つかりません", ac.path_problem(str(self.dir / "無い.sqlite3")))
        self.assertIn("ファイルではありません", ac.path_problem(str(self.dir)))
        junk = self.dir / "junk.sqlite3"
        junk.write_bytes(b"not a database at all" * 100)
        self.assertIn("sqlite3 として開けません", ac.path_problem(str(junk)))

    def test_only_the_access_table_is_exposed(self):
        ac.ensure_table(ac.open_db(self.cfg))
        db = ac.open_db(self.cfg)
        self.assertEqual(db.table_names(), ["アクセス権限"])
        self.assertIn("BoardMaster", db.all_table_names())

    def test_grant_reads_rows_like_python_web_tools(self):
        ac.ensure_table(ac.open_db(self.cfg))
        self.add(("yamada", "NGY-PC-0001", "mode:material", 1, ""),
                 ("yamada", "NGY-PC-0001", "mode:field", 1, ""),
                 ("yamada", "", "packing:print", 1, "ほかのツール"),
                 ("", "NGY-PC-0001", "mode:material", 0, "無効にしてある"),
                 ("suzuki", "", "mode:material", None, "有効が空 = 有効"))
        grant = ac.grant_for(self.cfg, self.store, identity=ME)
        self.assertEqual(grant.source, "fresh")
        self.assertEqual(grant.codes, {"mode:field", "mode:material"})
        self.assertEqual(len(grant.matched), 3)
        suzuki = ac.grant_for(self.cfg, self.store, identity=ac.Identity("SUZUKI", "X"))
        self.assertTrue(suzuki.allows(config.MODE_WAREHOUSE))

    def test_missing_table_means_site_only(self):
        grant = ac.grant_for(self.cfg, self.store, identity=ME)
        self.assertEqual(grant.codes, ac.FALLBACK)
        self.assertEqual(grant.source, "fresh")

    def test_unreachable_master_uses_the_last_good_read(self):
        """届かないたびに現場モードへ落とすと、倉庫の端末が倉庫として開けなくなる。"""
        ac.ensure_table(ac.open_db(self.cfg))
        self.add(("yamada", "", "mode:material", 1, ""))
        self.assertTrue(ac.grant_for(self.cfg, self.store, identity=ME).allows(config.MODE_WAREHOUSE))

        self.path.rename(self.dir / "どこかへ.sqlite3")
        grant = ac.grant_for(self.cfg, self.store, identity=ME)
        self.assertEqual(grant.source, "cache")
        self.assertTrue(grant.allows(config.MODE_WAREHOUSE))
        self.assertIn("前回読めた内容", grant.reason)

        # 置き場所を変えたら、前の場所で覚えたものは使わない
        other = config.Config(access_db_path=str(self.dir / "別の場所.sqlite3"))
        grant = ac.grant_for(other, self.store, identity=ME)
        self.assertEqual(grant.source, "none")
        self.assertFalse(grant.allows(config.MODE_WAREHOUSE))

    def test_never_read_and_unreachable_gives_site_only(self):
        cfg = config.Config(access_db_path=str(self.dir / "無い.sqlite3"))
        grant = ac.grant_for(cfg, self.store, identity=ME)
        self.assertEqual(grant.source, "none")
        self.assertEqual(grant.codes, ac.FALLBACK)
        self.assertIn("見つかりません", grant.reason)

    def test_slow_share_does_not_hold_up_startup(self):
        """届かない共有フォルダで待たされても、上限で切り上げて前回の内容で判断する。"""
        ac.ensure_table(ac.open_db(self.cfg))
        self.add(("yamada", "", "mode:material", 1, ""))
        ac.grant_for(self.cfg, self.store, identity=ME)

        class Slow(ac.AccessDb):
            def all_table_names(self):
                time.sleep(2)
                return super().all_table_names()

        started = time.monotonic()
        grant = ac.grant_for(self.cfg, self.store, identity=ME, timeout=0.2,
                             db=Slow(str(self.path)))
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(grant.source, "cache")
        self.assertTrue(grant.allows(config.MODE_WAREHOUSE))


class StartupTest(unittest.TestCase):
    """起動するとき(start_app._apply_access)。"""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="kanban_access_start_"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        patcher = mock.patch.dict(os.environ, {"KANBAN_LOCAL_DIR": str(self.dir / "local")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = Store(str(self.dir / "kanban.sqlite3"), host_name="PC")
        self.store.ensure_schema()
        self.addCleanup(self.store.close)

    def test_mode_without_permission_opens_the_narrower_one(self):
        import start_app

        self.store.set_device_mode(config.MODE_WAREHOUSE)
        cfg = config.Config(access_db_path=str(self.dir / "無い.sqlite3"))
        mode, grant, notice = start_app._apply_access(cfg, self.store, config.MODE_WAREHOUSE)
        self.assertEqual(mode, config.MODE_SITE)
        self.assertIn("mode:material", notice)
        self.assertIn("現場モードで開きました", notice)
        # **保存してあるモードは書き換えない**(行を足せば次から倉庫で開く)
        self.assertEqual(self.store.get_device_mode(), config.MODE_WAREHOUSE)

    def test_allowed_mode_opens_as_asked(self):
        import start_app

        path = self.dir / "梱包資材マスタ.sqlite3"
        conn = sqlite3.connect(str(path))
        conn.execute(ac.DDL)
        me = ac.current_identity()
        conn.execute('INSERT INTO "アクセス権限" (ログインID, PC名, 権限) VALUES (?, ?, ?)',
                     (me.login_id, me.pc_name, "mode:material"))
        conn.commit()
        conn.close()
        mode, grant, notice = start_app._apply_access(
            config.Config(access_db_path=str(path)), self.store, config.MODE_WAREHOUSE)
        self.assertEqual((mode, notice), (config.MODE_WAREHOUSE, ""))
        self.assertTrue(grant.allows(config.MODE_WAREHOUSE))


if __name__ == "__main__":
    unittest.main()
