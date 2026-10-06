"""配布設定 (:mod:`kanban.distribution`) ── python-web-tools と同じつくり

1 台で決めた設定を「配布設定」フォルダへ書き出し、フォルダごと配った先は
起動したときに読み込む。**その端末にすでにある設定は読み込まない。**
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from kanban import config, distribution


class DistributionTestBase(unittest.TestCase):
    ENV = ("KANBAN_SETTINGS_DIR", "KANBAN_CONFIG", "KANBAN_DISTRIBUTION_DIR")

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="kanban_dist_"))
        self._env = {k: os.environ.get(k) for k in self.ENV}
        self.use_terminal("配る元")
        self.folder = self.dir / "app" / "配布設定"
        os.environ["KANBAN_DISTRIBUTION_DIR"] = str(self.folder)

    def tearDown(self) -> None:
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.dir, ignore_errors=True)

    def use_terminal(self, name: str) -> None:
        """別の端末になる(その端末の data\\ と %APPDATA% を使う)。"""
        os.environ["KANBAN_SETTINGS_DIR"] = str(self.dir / name / "data")
        os.environ["KANBAN_CONFIG"] = str(self.dir / name / "appdata" / "config.json")

    def configure(self, **values) -> None:
        cfg = config.load_config()
        for key, value in values.items():
            setattr(cfg, key, value)
        config.save_config(cfg)

    def configure_source(self) -> None:
        self.configure(
            shared_db_path=r"\\server\看板マスタ.sqlite3",
            import_interval_sec=45,
            admin_password_hash=config.hash_password("secret"),
            line="AIM",
        )

    def export_default(self) -> distribution.Result:
        items = [k for k, _, default in distribution.ITEMS if default]
        return distribution.export(items)


class ExportTest(DistributionTestBase):
    def test_writes_the_folder_with_the_data_set(self):
        self.configure_source()
        result = self.export_default()
        self.assertTrue(result.ok, result.message)
        self.assertTrue((self.folder / "設定.json").is_file())
        self.assertTrue((self.folder / "はじめに読む.txt").is_file())
        data = json.loads((self.folder / "設定.json").read_text(encoding="utf-8"))
        self.assertEqual(data["format"], distribution.FORMAT)
        self.assertEqual(data["settings"]["import_interval_sec"], 45)

    def test_checks_the_folder_right_after_writing(self):
        """**書き出したら、配った先と同じ読み方で読み戻して確かめる。**"""
        self.configure_source()
        result = self.export_default()
        labels = {c["label"]: c["ok"] for c in result.checks}
        for label in ("「配布設定」フォルダがある", "設定.json がある",
                      "設定.json を読める(配った先と同じ読み方で)",
                      "書き出した値がそのまま入っている", "管理者パスワードが入っている",
                      "はじめに読む.txt がある"):
            self.assertTrue(labels.get(label), label)

    def test_the_line_is_left_out_unless_chosen(self):
        """担当ラインは端末ごと。入れたまま配ると全部の現場が同じラインを名乗る。"""
        self.configure_source()
        self.export_default()
        self.assertNotIn("line", distribution.read().settings)
        distribution.export(["line", "shared_db_path"])
        self.assertEqual(distribution.read().settings["line"], "AIM")

    def test_values_left_at_the_default_are_not_written(self):
        """この端末で変えていない項目は入れない(配った先も同じ既定で動く)。"""
        self.configure_source()
        result = self.export_default()
        self.assertNotIn("export_interval_sec", distribution.read().settings)
        self.assertIn("書き戻し間隔", result.message)

    def test_nothing_to_write_is_refused(self):
        result = distribution.export(["export_interval_sec"])
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "bad_input")
        self.assertFalse(self.folder.exists())

    def test_unknown_items_are_refused(self):
        self.assertEqual(distribution.export(["架空"]).reason, "bad_input")

    def test_the_readme_opens_cleanly_in_notepad(self):
        """メモ帳で開かれる前提で、BOM 付き UTF-8 + CRLF。"""
        self.configure_source()
        self.export_default()
        raw = (self.folder / "はじめに読む.txt").read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"\r\n", raw)

    def test_the_password_value_is_never_shown(self):
        self.configure_source()
        self.export_default()
        stored = distribution.read().settings["admin_password_hash"]
        text = json.dumps(distribution.summary(), ensure_ascii=False)
        self.assertNotIn(stored, text)
        self.assertNotIn(stored, (self.folder / "はじめに読む.txt").read_text(encoding="utf-8-sig"))


class ApplyTest(DistributionTestBase):
    def distribute(self) -> None:
        self.configure_source()
        self.export_default()

    def test_a_new_terminal_reads_it_on_start(self):
        """**配った先は起動したときに読み込む。** パスワードも一緒に。"""
        self.distribute()
        self.use_terminal("配った先")
        result = distribution.apply_on_start()
        self.assertTrue(result.applied)
        cfg = config.load_config()
        self.assertEqual(cfg.shared_db_path, r"\\server\看板マスタ.sqlite3")
        self.assertEqual(cfg.import_interval_sec, 45)
        self.assertTrue(
            config.verify_password("secret", cfg.admin_password_hash),
            "配った先が未設定のパスワードで始まっている(最初に押した人が決められる)",
        )
        self.assertEqual(cfg.line, "", "担当ラインまで入った")

    def test_what_the_terminal_already_has_is_kept(self):
        """**その端末にすでにある設定は読み込まない。**"""
        self.distribute()
        self.use_terminal("配った先")
        self.configure(import_interval_sec=10)
        result = distribution.apply_on_start()
        self.assertIn("取り込み間隔", result.kept)
        self.assertEqual(config.load_config().import_interval_sec, 10)

    def test_reading_again_on_the_next_start_changes_nothing(self):
        """埋まった項目は次から「すでにある」。端末で直した値を戻さない。"""
        self.distribute()
        self.use_terminal("配った先")
        distribution.apply_on_start()
        self.configure(import_interval_sec=10)
        self.assertEqual(distribution.apply_on_start().applied, [])
        self.assertEqual(config.load_config().import_interval_sec, 10)

    def test_reapply_overwrites(self):
        """「読み込み直す」は、すでにある設定も上書きして揃える。"""
        self.distribute()
        self.use_terminal("配った先")
        self.configure(import_interval_sec=10)
        result = distribution.reapply()
        self.assertTrue(result.ok)
        self.assertEqual(config.load_config().import_interval_sec, 45)

    def test_the_time_it_was_read_is_kept(self):
        self.distribute()
        self.use_terminal("配った先")
        distribution.apply_on_start()
        self.assertTrue(distribution.summary()["applied_at"])
        # 設定を保存し直しても、付記は消えない
        self.configure(export_interval_sec=20)
        self.assertTrue(distribution.summary()["applied_at"])

    def test_nothing_is_read_without_the_folder(self):
        self.assertEqual(distribution.apply_on_start().applied, [])

    def test_a_bundle_of_the_wrong_format_is_ignored(self):
        self.folder.mkdir(parents=True)
        (self.folder / "設定.json").write_text(json.dumps({"format": 99, "settings": {}}),
                                             encoding="utf-8")
        self.assertIsNone(distribution.read())

    def test_remove(self):
        self.distribute()
        self.assertTrue(distribution.remove().ok)
        self.assertFalse(self.folder.exists())
        self.assertEqual(config.load_config().import_interval_sec, 45, "この端末の設定まで消えた")


class LegacyTest(DistributionTestBase):
    """前の版が書いた ``配布設定\\config.json``(項目が平らに並ぶ形)も読む。"""

    def test_the_old_flat_file_is_read(self):
        self.folder.mkdir(parents=True)
        (self.folder / "config.json").write_text(json.dumps({
            "_保存日時": "2026/09/24 10:00:00", "_保存した端末": "PC-01",
            "import_interval_sec": 45, "admin_password_hash": config.hash_password("dist"),
            "line": "AIM",
        }), encoding="utf-8")
        bundle = distribution.read()
        self.assertTrue(bundle.legacy)
        self.assertEqual(bundle.created_on, "PC-01")
        distribution.apply_on_start()
        cfg = config.load_config()
        self.assertEqual(cfg.import_interval_sec, 45)
        self.assertTrue(config.verify_password("dist", cfg.admin_password_hash))

    def test_an_old_pc_keeps_what_it_chose(self):
        """前の版の端末(%APPDATA% に全項目)では、その端末で決めた値が勝つ。
        空欄のパスワードで配布設定のパスワードを消さない。"""
        self.folder.mkdir(parents=True)
        (self.folder / "config.json").write_text(json.dumps({
            "shared_db_path": r"\\server\X.sqlite3",
            "admin_password_hash": config.hash_password("dist"),
        }), encoding="utf-8")
        pc = Path(os.environ["KANBAN_CONFIG"])
        pc.parent.mkdir(parents=True)
        pc.write_text(json.dumps(config.Config(
            line="AIM", shared_db_path=r"\\server\Y.sqlite3").to_dict()), encoding="utf-8")

        distribution.apply_on_start()
        cfg = config.load_config()
        self.assertEqual(cfg.shared_db_path, r"\\server\Y.sqlite3")
        self.assertTrue(config.verify_password("dist", cfg.admin_password_hash))
        self.assertEqual(cfg.line, "AIM")


if __name__ == "__main__":
    unittest.main()
