"""配布用フォルダを作る (``scripts/make_dist.py``)

**配るものだけ**を写す。端末ごとの ``data\\`` を配ると、配った先はそれを自分の
設定として持ち、配布設定を読み込まない。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_make_dist():
    spec = importlib.util.spec_from_file_location("make_dist_for_test", ROOT / "scripts" / "make_dist.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MakeDistTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="kanban_make_dist_"))
        self._env = os.environ.get("KANBAN_DISTRIBUTION_DIR")
        self.bundle = self.dir / "配布設定"
        os.environ["KANBAN_DISTRIBUTION_DIR"] = str(self.bundle)
        self.make_dist = load_make_dist()

    def tearDown(self) -> None:
        if self._env is None:
            os.environ.pop("KANBAN_DISTRIBUTION_DIR", None)
        else:
            os.environ["KANBAN_DISTRIBUTION_DIR"] = self._env
        shutil.rmtree(self.dir, ignore_errors=True)

    def put_bundle(self) -> None:
        self.bundle.mkdir()
        (self.bundle / "設定.json").write_text(json.dumps({
            "format": 1, "created_at": "2026/09/25 10:00:00", "created_on": "PC-01",
            "settings": {"import_interval_sec": 45},
        }), encoding="utf-8")

    def test_only_what_should_be_distributed_goes_in(self):
        self.put_bundle()
        out, lines = self.make_dist.build(self.dir / "out")
        self.assertTrue((out / "start_app.py").is_file())
        self.assertTrue((out / "kanban" / "distribution.py").is_file())
        self.assertTrue((out / "配布設定" / "設定.json").is_file())
        self.assertTrue((out / "配布メモ.txt").is_file())
        for forbidden in ("data", "tests", ".git"):
            self.assertFalse((out / forbidden).exists(), forbidden)
        self.assertFalse(list(out.rglob("__pycache__")))
        self.assertTrue(any("取り込み間隔" in line for line in lines), lines)

    def test_desktop_is_the_integrated_window(self):
        """看板だけの exe は無い。デスクトップ版は日報複合ツールの窓で使うと書く(bridge.py は入る)。"""
        out, lines = self.make_dist.build(self.dir / "out")
        self.assertTrue((out / "bridge.py").is_file(), "日報複合ツールの外枠が子として起動する")
        self.assertFalse(list(out.glob("*.exe")))
        self.assertFalse((out / "src-tauri").exists())
        self.assertTrue(any("日報複合ツール" in line for line in lines), lines)
        self.assertIn("Start.vbs", (out / "配布メモ.txt").read_text(encoding="utf-8-sig"))

    def test_no_settings_leaves_the_bundle_out(self):
        self.put_bundle()
        out, lines = self.make_dist.build(self.dir / "out", with_settings=False)
        self.assertFalse((out / "配布設定").exists())
        self.assertTrue(any("入れていません" in line for line in lines))

    def test_default_out_is_dist_ver_name(self) -> None:
        """版は親のフォルダーに入れ、作る物のフォルダー名は版なし(ランチャーの「道を変えない」決まり)。"""
        out = self.make_dist.default_out()
        self.assertEqual(out, self.make_dist.ROOT.parent / "配布" / f"VER{self.make_dist._version()}" / self.make_dist.DISPLAY_NAME)
        self.assertNotIn("VER", out.name)

    def test_zip_name_has_version_but_top_folder_does_not(self) -> None:
        name = self.make_dist.DISPLAY_NAME
        out, lines = self.make_dist.build(self.dir / "配布" / f"VER{self.make_dist._version()}" / name,
                                  with_settings=False, make_zip=True)
        archive = out.parent / f"{name}_VER{self.make_dist._version()}.zip"
        self.assertTrue(archive.is_file(), list(out.parent.iterdir()))
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
        self.assertEqual({n.split("/", 1)[0] for n in names}, {name})
        self.assertIn(f"{name}/配布メモ.txt", names)
        self.assertIn(f"{name}/start_app.py", names)
        self.assertTrue(any("zip も作りました" in line for line in lines), lines)
        memo = (out / "配布メモ.txt").read_text(encoding="utf-8-sig")
        self.assertIn(f"決まった場所の {name}\\ に中身ごと上書きする", memo)
        self.assertIn("フォルダー名を変えない・版の付いた名前で置かない", memo)

    def test_stamped_out_puts_the_stamp_in_the_parent_folder(self):
        """設定画面の「配布用フォルダを作る」は、日時を親のフォルダーに付ける(作る物は版なし)。"""
        out = self.make_dist.default_out(stamp=True)
        self.assertEqual(out.name, self.make_dist.DISPLAY_NAME)
        self.assertEqual(out.parent.parent, ROOT.parent / "配布")
        self.assertRegex(out.parent.name, r"^VER.+_\d{8}_\d{6}$")

    def test_refuses_to_build_inside_the_app_folder(self):
        with self.assertRaises(SystemExit):
            self.make_dist.build(ROOT / "dist_test_should_not_exist")
        self.assertFalse((ROOT / "dist_test_should_not_exist").exists())

    def test_refuses_to_overwrite_without_force(self):
        out = self.dir / "out"
        out.mkdir()
        (out / "何か.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(SystemExit):
            self.make_dist.build(out)
        self.assertTrue((out / "何か.txt").exists())
        self.make_dist.build(out, force=True)
        self.assertFalse((out / "何か.txt").exists())


if __name__ == "__main__":
    unittest.main()
