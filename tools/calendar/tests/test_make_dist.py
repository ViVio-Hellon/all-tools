"""配布用フォルダを作るスクリプト(`tools/make_dist.py`)"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from calendar_app import admin_password, distribution, settings as user_settings

_ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("make_dist", _ROOT / "tools" / "make_dist.py")
make_dist = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(make_dist)


class MakeDistTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.out = self.tmp / "dist"

    def test_配るものだけを写す(self) -> None:
        out, _lines = make_dist.build(self.out, with_settings=False)
        names = {p.name for p in out.iterdir()}
        self.assertTrue(set(make_dist.INCLUDE) <= names)
        for bad in ("tests", ".git", "sync", "logs", "print", "settings.json"):
            self.assertNotIn(bad, names)
        self.assertFalse(list(out.rglob("__pycache__")))
        self.assertFalse(list(out.rglob("*.pyc")))
        self.assertTrue((out / "calendar_app" / "distribution.py").exists())
        self.assertTrue((out / "配布メモ.txt").exists())

    def test_配布設定フォルダは入れると決めたときだけ(self) -> None:
        tool = self.tmp / "tool"
        tool.mkdir()
        for name in make_dist.INCLUDE:          # 写す元の形だけ作る(中身は要らない)
            (tool / name).mkdir() if "." not in name else (tool / name).write_text("")
        (tool / "config").mkdir(exist_ok=True)
        (tool / "config" / "app.json").write_text(
            json.dumps({"version": "9.9.9"}), encoding="utf-8")
        src = tool / "配布設定"
        src.mkdir()
        distribution.settings_path(src).write_text(json.dumps({
            "format": 1, "settings": {user_settings.KEY_DATA_DB_DIR: r"\\srv\共有",
                                      admin_password.KEY: "pbkdf2$1$x$y"}},
            ensure_ascii=False), encoding="utf-8")
        with mock.patch.object(make_dist, "ROOT", tool):
            out, _lines = make_dist.build(self.out)
            out2, _ = make_dist.build(self.tmp / "dist2", with_settings=False)
        self.assertTrue(distribution.settings_path(out / "配布設定").exists())
        memo = (out / "配布メモ.txt").read_text(encoding="utf-8-sig")
        self.assertIn(r"保存用DBの置き場所: \\srv\共有", memo)
        self.assertIn("管理者パスワード: (設定済み)", memo)
        self.assertNotIn("pbkdf2$1$x$y", memo)
        self.assertIn("VER9.9.9", memo)
        self.assertFalse((out2 / "配布設定").exists())

    def test_既定の作る場所はツールの隣の配布_VER版_表示名(self) -> None:
        """版は親のフォルダーに入れ、作る物のフォルダー名は版なし(ランチャーの「道を変えない」決まり)。"""
        out = make_dist.default_out()
        self.assertEqual(out, make_dist.ROOT.parent / f"{make_dist._display_name()}_VER{make_dist._version()}" / make_dist._display_name())
        self.assertNotIn("VER", out.name)

    def test_zipの名前には版が入り_中の最上位は版なしの名前(self) -> None:
        name = make_dist._display_name()
        out, lines = make_dist.build(self.tmp / f"{name}_VER{make_dist._version()}" / name,
                                  with_settings=False, make_zip=True)
        archive = out.parent / f"{name}_VER{make_dist._version()}.zip"
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

    def test_ツールのフォルダの中には作らない(self) -> None:
        with self.assertRaises(SystemExit):
            make_dist.build(_ROOT / "export" / "dist")

    def test_中身のある場所は作り直すと言われたときだけ(self) -> None:
        self.out.mkdir()
        (self.out / "前の.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(SystemExit):
            make_dist.build(self.out, with_settings=False)
        self.assertTrue((self.out / "前の.txt").exists())
        make_dist.build(self.out, with_settings=False, force=True)
        self.assertFalse((self.out / "前の.txt").exists())

    def test_直下の一覧と食い違わない(self) -> None:
        """直下に何かを足したら、**配るかどうかを決めさせる**。"""
        done = subprocess.run(["git", "ls-files"], cwd=_ROOT, capture_output=True,
                              text=True, encoding="utf-8", check=True)
        tracked = {line.split("/")[0].strip('"') for line in done.stdout.splitlines()}
        tracked = {name for name in tracked if not name.startswith("docs")} | {"docs"}
        dev_only = {"tests", ".gitignore", ".gitattributes"}
        self.assertEqual(set(make_dist.INCLUDE), tracked - dev_only)

    def test_デスクトップ版は日報複合ツールの窓と書く(self) -> None:
        """カレンダーだけの exe は無い。bridge.py は入る(日報複合ツールの外枠が子として起動する)。"""
        out, lines = make_dist.build(self.out, with_settings=False)
        self.assertTrue((out / "bridge.py").exists())
        self.assertFalse(list(out.glob("*.exe")))
        self.assertIn("日報複合ツール", "\n".join(lines))
        memo = (out / "配布メモ.txt").read_text(encoding="utf-8-sig")
        self.assertIn("Start.vbs", memo)
        self.assertFalse((out / "src-tauri").exists())
        self.assertFalse((out / ".github").exists())

    def test_バッチは英字だけ(self) -> None:
        """cmd.exe はコンソールのコードページで読むので、日本語を入れない。"""
        raw = (_ROOT / "tools" / "make_dist.bat").read_bytes()
        self.assertTrue(raw.isascii())
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))


if __name__ == "__main__":
    unittest.main()
