"""配布用フォルダを作るスクリプト(`scripts/make_dist.py`、python-web-tools と同じ)"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

_spec = importlib.util.spec_from_file_location("make_dist", _ROOT / "scripts" / "make_dist.py")
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
        for bad in ("tests", ".git", "配布先", "配布設定"):
            self.assertNotIn(bad, names)
        self.assertFalse(list(out.rglob("__pycache__")))
        self.assertFalse(list(out.rglob("*.pyc")))
        self.assertTrue((out / "nippou" / "distribution.py").exists())
        self.assertTrue((out / "配布メモ.txt").exists())

    def test_配布設定は起動用のファイルと同じ階層に入れる(self) -> None:
        from nippou import distribution
        src = self.tmp / "配布設定"
        (src / distribution.SOUNDS_DIRNAME).mkdir(parents=True)
        distribution.settings_path(src).write_text(json.dumps({
            "format": 1, "settings": {"access_dir": r"\\srv\日報",
                                      "admin_password": "pbkdf2$1$x$y"}},
            ensure_ascii=False), encoding="utf-8")
        (distribution.sounds_dir(src) / "a.wav").write_bytes(b"RIFF")
        out, _lines = make_dist.build(self.out, settings_src=src)
        out2, _ = make_dist.build(self.tmp / "dist2", with_settings=False,
                                  settings_src=src)
        self.assertTrue(distribution.settings_path(out / "配布設定").exists())
        self.assertTrue((out / "配布設定").parent.joinpath("Start.vbs").exists())
        self.assertTrue((out / "配布設定" / "音" / "a.wav").exists())
        memo = (out / "配布メモ.txt").read_text(encoding="utf-8-sig")
        self.assertIn(r"共有の日報管理のパス: \\srv\日報", memo)
        self.assertIn("音声ファイル", memo)
        self.assertNotIn("pbkdf2$1$x$y", memo)
        self.assertIn("この端末のライン", memo)
        self.assertFalse((out2 / "配布設定").exists())

    def test_ツールのフォルダの中には作らない(self) -> None:
        with self.assertRaises(SystemExit):
            make_dist.build(_ROOT / "dist_in_repo")
        self.assertFalse((_ROOT / "dist_in_repo").exists())

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
        here = {p.name for p in _ROOT.iterdir()}
        undecided = here - set(make_dist.INCLUDE) - set(make_dist.DEV_ONLY)
        undecided = {n for n in undecided if not n.startswith(".")}
        self.assertEqual(undecided, set(), "make_dist.INCLUDE か DEV_ONLY に入れてください")

    def test_バッチは英字だけ(self) -> None:
        """cmd.exe はコンソールのコードページで読むので、日本語を入れない。"""
        raw = (_ROOT / "scripts" / "make_dist.bat").read_bytes()
        self.assertTrue(raw.isascii())
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))


if __name__ == "__main__":
    unittest.main()
