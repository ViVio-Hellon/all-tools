"""統合ツール一式の配布用フォルダ(`scripts/make_dist.py`)

- 配るものだけを写す(tests・src-tauri・__pycache__・.git は入れない)
- 各ツールは、そのツールの make_dist の決まりのまま `tools\\<ツール>\\` へ
- デスクトップ版の exe は、あれば 統合ツール.exe の名前で直下に入れる
- 直下に何かを足したら、配るかどうかを決めさせる(一覧と食い違えば落ちる)
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import tests  # noqa: F401  (一時フォルダへ向ける)

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("alltools_make_dist", ROOT / "scripts" / "make_dist.py")
make_dist = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(make_dist)


class BuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="alltools_dist_"))
        exe = cls.tmp / "AllTools.exe"
        exe.write_bytes(b"MZ-fake")
        cls.out, cls.lines = make_dist.build(cls.tmp / "dist", exe=exe)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_入口と4ツールが入る(self) -> None:
        for name in make_dist.INCLUDE:
            self.assertTrue((self.out / name).exists(), name)
        for tool in ("nippou", "kanban", "calendar", "inspection"):
            with self.subTest(tool=tool):
                for name in ("bridge.py", "start_app.py", "config/app.json", "app/static"):
                    self.assertTrue((self.out / "tools" / tool / name).exists(), name)
                self.assertFalse((self.out / "tools" / tool / "配布メモ.txt").exists(),
                                 "ツールの配布メモは一式の配布メモにまとめる")

    def test_exeは配る名前で直下に入る(self) -> None:
        self.assertEqual((self.out / make_dist.EXE_NAME).read_bytes(), b"MZ-fake")
        memo = (self.out / "配布メモ.txt").read_text(encoding="utf-8-sig")
        self.assertIn("デスクトップ版を入れました", memo)
        self.assertIn("[看板] VER", memo)
        self.assertIn("\r\n", (self.out / "配布メモ.txt").read_bytes().decode("utf-8-sig"))

    def test_配ってはいけないものが入らない(self) -> None:
        for pattern in ("tests", "src-tauri", "__pycache__", "*.pyc", ".git*", "*.sqlite3"):
            with self.subTest(pattern=pattern):
                self.assertEqual(list(self.out.rglob(pattern)), [])
        for name in make_dist.FORBIDDEN:
            self.assertFalse((self.out / name).exists(), name)


class RuleTests(unittest.TestCase):
    def test_exeが無ければブラウザ版で動くと書く(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out, lines = make_dist.build(Path(tmp) / "dist", exe=Path(tmp) / "無い.exe")
            self.assertFalse((out / make_dist.EXE_NAME).exists())
            self.assertIn("入っていません", "\n".join(lines))

    def test_一式の中には作らない_中身のある場所は作り直すと言われたときだけ(self) -> None:
        with self.assertRaises(SystemExit):
            make_dist.build(ROOT / "dist")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "dist"
            out.mkdir()
            (out / "前の.txt").write_text("x", encoding="utf-8")
            with self.assertRaises(SystemExit):
                make_dist.build(out)
            self.assertTrue((out / "前の.txt").exists())

    def test_直下の一覧と食い違わない(self) -> None:
        """直下に何かを足したら、**配るかどうかを決めさせる**(INCLUDE か DEV_ONLY へ)。"""
        done = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True,
                              encoding="utf-8", check=True)
        tracked = {line.split("/")[0].strip('"') for line in done.stdout.splitlines()}
        tracked = {t for t in tracked if t}
        undecided = tracked - set(make_dist.INCLUDE) - set(make_dist.DEV_ONLY)
        self.assertEqual(undecided, set(), "配るかどうか決まっていない")
        self.assertEqual(set(make_dist.INCLUDE) & set(make_dist.DEV_ONLY), set())

    def test_ツールの一覧と食い違わない(self) -> None:
        import json
        tools = json.loads((ROOT / "config" / "tools.json").read_text(encoding="utf-8"))["tools"]
        for tool in tools:
            with self.subTest(tool=tool["id"]):
                self.assertTrue(tool["id"] in make_dist.TOOL_BUILDERS or tool["id"] in make_dist.GENERIC_INCLUDE)

    def test_make_distを持たないツールの一覧はそのツールの直下と食い違わない(self) -> None:
        for tool_id, include in make_dist.GENERIC_INCLUDE.items():
            done = subprocess.run(["git", "ls-files", f"tools/{tool_id}"], cwd=ROOT, capture_output=True,
                                  text=True, encoding="utf-8", check=True)
            tracked = {line.split("/")[2].strip('"') for line in done.stdout.splitlines() if line.count("/") >= 2}
            dev_only = {"tests", ".gitignore", ".gitattributes"}
            with self.subTest(tool=tool_id):
                self.assertEqual(set(include), tracked - dev_only)


if __name__ == "__main__":
    unittest.main()
