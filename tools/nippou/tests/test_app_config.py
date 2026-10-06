"""アプリ固有値(config/app.json)の読み取りのテスト。

設定ファイルが壊れていても**起動は止めず、理由を残して既定値で続ける**
という基盤仕様書の方針(ステップ5)が守られていることを主に確かめる。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou import app_config


class _ConfigCase(unittest.TestCase):
    """`app_config` はモジュール変数にキャッシュを持つので、毎回消す。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        app_config._cache = None
        app_config._load_error = ""

    def tearDown(self) -> None:
        app_config._cache = None
        app_config._load_error = ""
        self._tmpdir.cleanup()

    def _write_config(self, data) -> Path:
        path = self.tmp / "app.json"
        path.write_text(
            data if isinstance(data, str) else json.dumps(data, ensure_ascii=False),
            encoding="utf-8")
        return path


class LoadTests(_ConfigCase):
    def test_reads_values_from_file(self) -> None:
        path = self._write_config({"app_id": "x.test", "display_name": "試験",
                                   "version": "1.2.3"})
        with patch.object(app_config, "CONFIG_PATH", path):
            self.assertEqual(app_config.app_id(), "x.test")
            self.assertEqual(app_config.display_name(), "試験")
            self.assertEqual(app_config.version(), "1.2.3")
            self.assertEqual(app_config.load_error(), "")

    def test_missing_keys_fall_back_to_defaults(self) -> None:
        # port を書いていなくても既定値で埋まる(キー1つ足りないだけで
        # 起動できなくなることがない)
        path = self._write_config({"app_id": "x.test"})
        with patch.object(app_config, "CONFIG_PATH", path):
            self.assertEqual(app_config.app_id(), "x.test")
            self.assertEqual(app_config.port(), 8733)

    def test_missing_file_falls_back_with_reason(self) -> None:
        with patch.object(app_config, "CONFIG_PATH", self.tmp / "no-such.json"):
            self.assertEqual(app_config.app_id(), "nlm.nippou-tool")
            self.assertIn("設定ファイルがありません", app_config.load_error())

    def test_broken_json_falls_back_with_reason(self) -> None:
        path = self._write_config("{ this is not json")
        with patch.object(app_config, "CONFIG_PATH", path):
            self.assertEqual(app_config.app_id(), "nlm.nippou-tool")
            self.assertIn("読めませんでした", app_config.load_error())

    def test_non_object_toplevel_falls_back(self) -> None:
        path = self._write_config("[1, 2, 3]")
        with patch.object(app_config, "CONFIG_PATH", path):
            self.assertEqual(app_config.app_id(), "nlm.nippou-tool")
            self.assertIn("読めませんでした", app_config.load_error())

    def test_defaults_are_not_mutated_by_a_load(self) -> None:
        path = self._write_config({"server": {"port": 9999}})
        with patch.object(app_config, "CONFIG_PATH", path):
            self.assertEqual(app_config.port(), 9999)
        # 既定値そのものが書き換わっていないこと
        self.assertEqual(app_config._FALLBACK["server"]["port"], 8733)


class VersionTests(_ConfigCase):
    def test_version_label_has_prefix(self) -> None:
        path = self._write_config({"version": "3.0.0"})
        with patch.object(app_config, "CONFIG_PATH", path):
            self.assertEqual(app_config.version_label(), "VER3.0.0")

    def test_well_formed_version_has_no_problem(self) -> None:
        path = self._write_config({"version": "10.2.30"})
        with patch.object(app_config, "CONFIG_PATH", path):
            self.assertEqual(app_config.version_problem(), "")

    def test_malformed_version_is_reported(self) -> None:
        path = self._write_config({"version": "3.0"})
        with patch.object(app_config, "CONFIG_PATH", path):
            self.assertIn("版の書き方が違います", app_config.version_problem())


class PortTests(_ConfigCase):
    def test_port_candidates_follow_retry_count(self) -> None:
        path = self._write_config({"server": {"port": 8000, "port_retry": 2}})
        with patch.object(app_config, "CONFIG_PATH", path):
            self.assertEqual(app_config.port_candidates(), [8000, 8001, 8002])

    def test_host_is_loopback_by_default(self) -> None:
        with patch.object(app_config, "CONFIG_PATH", self.tmp / "no-such.json"):
            self.assertEqual(app_config.host(), "127.0.0.1")


class LocalAreaTests(_ConfigCase):
    def test_env_override_wins(self) -> None:
        with patch.dict(os.environ, {"NIPPOU_LOCAL_DIR": str(self.tmp / "here")}):
            self.assertEqual(app_config.local_root(), self.tmp / "here")

    def test_localappdata_is_used_on_windows_like_env(self) -> None:
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.tmp / "AppData")},
                        clear=False):
            os.environ.pop("NIPPOU_LOCAL_DIR", None)
            self.assertEqual(app_config.local_root(),
                             self.tmp / "AppData" / "NippouTool")

    def test_ensure_local_dirs_creates_every_subdir(self) -> None:
        with patch.dict(os.environ, {"NIPPOU_LOCAL_DIR": str(self.tmp / "local")}):
            root = app_config.ensure_local_dirs()
            for name in app_config.LOCAL_SUBDIRS:
                self.assertTrue((root / name).is_dir(), f"{name} が作られていない")

    def test_local_dir_rejects_unknown_name(self) -> None:
        with self.assertRaises(ValueError):
            app_config.local_dir("nope")

    def test_describe_mentions_key_values(self) -> None:
        with patch.dict(os.environ, {"NIPPOU_LOCAL_DIR": str(self.tmp / "local")}):
            text = app_config.describe()
        self.assertIn("アプリID", text)
        self.assertIn("ポート", text)
        self.assertIn(str(self.tmp / "local"), text)


class SettingsIntegrationTests(_ConfigCase):
    """`config.SETTINGS` 側が app_config のローカル領域を向いていること。"""

    def test_runtime_paths_default_to_local_area(self) -> None:
        from nippou.config import Settings

        with patch.dict(os.environ, {"NIPPOU_LOCAL_DIR": str(self.tmp / "local")}):
            os.environ.pop("NIPPOU_APP_DIR", None)
            settings = Settings()
            self.assertEqual(settings.app_dir, self.tmp / "local" / "data")
            self.assertEqual(settings.log_dir, self.tmp / "local" / "logs")
            self.assertEqual(settings.report_output_dir, self.tmp / "local" / "work")

    def test_app_dir_override_keeps_everything_in_one_folder(self) -> None:
        from nippou.config import Settings

        with patch.dict(os.environ, {"NIPPOU_APP_DIR": str(self.tmp / "one")}):
            settings = Settings()
            self.assertEqual(settings.app_dir, self.tmp / "one")
            self.assertEqual(settings.log_dir, self.tmp / "one" / "logs")
            self.assertEqual(settings.report_output_dir, self.tmp / "one" / "reports")


if __name__ == "__main__":
    unittest.main()
