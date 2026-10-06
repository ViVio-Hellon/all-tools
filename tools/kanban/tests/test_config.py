"""kanban.config の管理者パスワード・モード表示名まわりのテスト。"""

from __future__ import annotations

import unittest
from pathlib import Path

from kanban import config


class ModeDisplayNameTest(unittest.TestCase):
    def test_known_modes_have_japanese_labels(self):
        for mode in config.ALL_MODES:
            self.assertNotEqual(config.mode_display_name(mode), mode)

    def test_unknown_mode_returns_value_itself(self):
        self.assertEqual(config.mode_display_name("nosuch"), "nosuch")


class PasswordHashTest(unittest.TestCase):
    def test_correct_password_verifies(self):
        stored = config.hash_password("hunter2")
        self.assertTrue(config.verify_password("hunter2", stored))

    def test_wrong_password_fails(self):
        stored = config.hash_password("hunter2")
        self.assertFalse(config.verify_password("wrong", stored))

    def test_hash_is_salted_differently_each_time(self):
        first = config.hash_password("same-password")
        second = config.hash_password("same-password")
        self.assertNotEqual(first, second)
        self.assertTrue(config.verify_password("same-password", first))
        self.assertTrue(config.verify_password("same-password", second))

    def test_empty_stored_hash_never_verifies(self):
        self.assertFalse(config.verify_password("anything", ""))

    def test_malformed_stored_hash_never_verifies(self):
        self.assertFalse(config.verify_password("anything", "not-a-real-hash"))
        self.assertFalse(config.verify_password("anything", "zzzz$zzzz"))


class ConfigRoundtripTest(unittest.TestCase):
    def test_admin_password_hash_is_persisted(self):
        import shutil
        import tempfile
        from pathlib import Path

        d = Path(tempfile.mkdtemp(prefix="kanban_cfg_"))
        try:
            path = d / "config.json"
            cfg = config.Config(source_path=str(path))
            cfg.admin_password_hash = config.hash_password("secret")
            config.save_config(cfg)

            reloaded = config.load_config(path)
            self.assertEqual(reloaded.admin_password_hash, cfg.admin_password_hash)
            self.assertTrue(config.verify_password("secret", reloaded.admin_password_hash))
        finally:
            shutil.rmtree(d, ignore_errors=True)


class SettingsFileTest(unittest.TestCase):
    """この端末の設定は ``%APPDATA%\\KanbanSystem\\config.json``(アプリのフォルダの外)。

    一時期はアプリのフォルダの ``data\\config.json`` に置いていたが、新しい版を
    ダウンロードしてフォルダごと入れ替えると ``data\\`` が無くなり、**管理者
    パスワードも接続先も消えていた**(パスワードが「未設定」に戻った)。
    """

    def setUp(self) -> None:
        import os
        import tempfile
        from pathlib import Path

        self.dir = Path(tempfile.mkdtemp(prefix="kanban_settings_"))
        self.pc = self.dir / "appdata" / "config.json"
        self.old_app_data = self.dir / "旧アプリ" / "data"
        self._env = {k: os.environ.get(k) for k in ("KANBAN_SETTINGS_DIR", "KANBAN_CONFIG")}
        os.environ["KANBAN_CONFIG"] = str(self.pc)
        os.environ["KANBAN_SETTINGS_DIR"] = str(self.old_app_data)

    def tearDown(self) -> None:
        import os
        import shutil

        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.dir, ignore_errors=True)

    def read(self, path):
        import json

        return json.loads(path.read_text(encoding="utf-8-sig"))

    def write(self, path, values, encoding="utf-8"):
        import json

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(values, ensure_ascii=False), encoding=encoding)

    def configure(self):
        cfg = config.load_config()
        cfg.shared_db_path = r"\\server\看板マスタ.sqlite3"
        cfg.import_interval_sec = 45
        cfg.admin_password_hash = config.hash_password("secret")
        cfg.line = "AIM"
        config.save_config(cfg)

    # -- 置き場所 ----------------------------------------------------------
    def test_settings_go_to_the_pcs_own_file(self):
        self.configure()
        saved = self.read(self.pc)
        self.assertEqual(saved["shared_db_path"], r"\\server\看板マスタ.sqlite3")
        self.assertEqual(saved["line"], "AIM")
        self.assertIn("admin_password_hash", saved)
        self.assertFalse(self.old_app_data.exists(), "アプリのフォルダに書いている")

    def test_replacing_the_app_folder_keeps_the_settings(self):
        """**ここが本題。** 新しい版のフォルダに入れ替えても、パスワード・接続先が残る。"""
        import os

        self.configure()
        os.environ["KANBAN_SETTINGS_DIR"] = str(self.dir / "ダウンロードした新しい版" / "data")
        cfg = config.load_config()
        self.assertTrue(config.verify_password("secret", cfg.admin_password_hash),
                        "入れ替えたらパスワードが消えた(未設定に戻る)")
        self.assertEqual(cfg.shared_db_path, r"\\server\看板マスタ.sqlite3")
        self.assertEqual(cfg.line, "AIM")

    def test_builtin_defaults_are_not_written(self):
        """ファイルを開けば「何を変えてあるか」がそのまま読めること。"""
        cfg = config.load_config()
        cfg.admin_password_hash = config.hash_password("secret")
        config.save_config(cfg)
        self.assertEqual(set(self.read(self.pc)), {config.LOCAL_FORMAT_KEY, "admin_password_hash"})

    def test_nothing_is_created_just_by_reading(self):
        config.load_config()
        self.assertFalse(self.pc.exists())

    # -- 一時期の版(アプリのフォルダの data\config.json)からの引き継ぎ ----
    def test_settings_left_in_the_app_folder_are_carried_over(self):
        """その時期の版で設定した端末は、パスワードや接続先がアプリのフォルダにしか無い。"""
        self.write(self.old_app_data / "config.json", {
            "shared_db_path": r"\\server\Y.sqlite3",
            "admin_password_hash": config.hash_password("own"),
        })
        self.write(self.pc, {"_形式": 2, "line": "AIM"})
        cfg = config.load_config()
        self.assertEqual(cfg.shared_db_path, r"\\server\Y.sqlite3")
        self.assertTrue(config.verify_password("own", cfg.admin_password_hash))
        self.assertEqual(cfg.line, "AIM")
        self.assertEqual(self.read(self.pc)["shared_db_path"], r"\\server\Y.sqlite3",
                         "引き継いだ値をこの端末の設定に書いていない")

    def test_what_the_pc_already_has_wins(self):
        self.write(self.old_app_data / "config.json", {"import_interval_sec": 45})
        self.write(self.pc, {"import_interval_sec": 10})
        self.assertEqual(config.load_config().import_interval_sec, 10)

    def test_it_is_carried_over_only_once(self):
        """引き継いだあとでこの端末で直した値を、次の起動で戻さない。"""
        self.write(self.old_app_data / "config.json", {"import_interval_sec": 45})
        cfg = config.load_config()
        self.assertEqual(cfg.import_interval_sec, 45)
        cfg.import_interval_sec = config.Config().import_interval_sec   # 既定へ戻した
        config.save_config(cfg)
        self.assertEqual(config.load_config().import_interval_sec,
                         config.Config().import_interval_sec)

    def test_an_old_full_dump_in_appdata_is_read_sensibly(self):
        """前の版が %APPDATA% に全項目を書いていたファイル。空欄は「決めていない」。"""
        self.write(self.pc, config.Config(
            line="AIM", shared_db_path=r"\\server\Y.sqlite3",
            admin_password_hash=config.hash_password("own"),
        ).to_dict())
        cfg = config.load_config()
        self.assertEqual(cfg.shared_db_path, r"\\server\Y.sqlite3")
        self.assertTrue(config.verify_password("own", cfg.admin_password_hash))
        self.assertEqual(cfg.line, "AIM")

    # -- 手で直したファイル -------------------------------------------------
    def test_a_file_with_a_bom_is_read(self):
        """メモ帳で直すと BOM が付く。以前はそれだけでファイル全体を読まなかった。"""
        self.write(self.pc, {"import_interval_sec": 45, "line": "AIM"}, encoding="utf-8-sig")
        cfg = config.load_config()
        self.assertEqual((cfg.import_interval_sec, cfg.line), (45, "AIM"))

    def test_an_unreadable_file_is_kept_not_overwritten(self):
        """読めない設定ファイルを黙って上書きしない(直せば取り戻せる)。"""
        self.pc.parent.mkdir(parents=True)
        self.pc.write_text('{"import_interval_sec": 45 "x": 1}', encoding="utf-8")
        self.assertTrue(config.read_settings_file().error)
        cfg = config.load_config()
        cfg.admin_password_hash = config.hash_password("secret")
        config.save_config(cfg)
        kept = [p for p in self.pc.parent.iterdir() if "読めなかった" in p.name]
        self.assertEqual(len(kept), 1)

    def test_hand_written_values_are_read_by_their_meaning(self):
        """手で書くと ``"60"`` や ``"false"`` になる。``"false"`` を「する」と
        読んでいた(文字があるので真)。読めない値は理由付きで読まない。"""
        self.write(self.pc, {"import_interval_sec": "60", "auto_print": "false",
                             "export_interval_sec": "abc"})
        status = config.read_settings_file()
        self.assertEqual(status.values, {"import_interval_sec": 60, "auto_print": False})
        self.assertTrue(any("書き戻し間隔" in x for x in status.ignored), status.ignored)

    def test_blank_values_mean_not_set(self):
        self.write(self.pc, {"admin_password_hash": "", "shared_db_path": ""})
        cfg = config.load_config()
        self.assertEqual(cfg.admin_password_hash, "")
        self.assertEqual(cfg.resolved_shared_db_path(),
                         str(Path(config.PATH_AIM_REFERENCE) / config.TARGET_SHARED_DB_NAME))


class ExplicitConfigFileTest(unittest.TestCase):
    """``--config`` で渡したファイルは、その 1 つに全部入る(試験・検証用)。"""

    def test_everything_goes_into_the_given_file(self):
        import json
        import shutil
        import tempfile

        d = Path(tempfile.mkdtemp(prefix="kanban_cfg_"))
        try:
            path = d / "config.json"
            cfg = config.load_config(path)
            cfg.line = "LVC"
            cfg.import_interval_sec = 45
            config.save_config(cfg)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual((saved["line"], saved["import_interval_sec"]), ("LVC", 45))
            self.assertEqual(config.load_config(path).line, "LVC")
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
