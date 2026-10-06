"""変換ツール (tools/convert_accdb.py) の確認

【一度きりだから、余計に固める】
移行の日に1回だけ動かすコマンドなので、間違っていても**その場では
気づけない**。気づくのは数週間後、「去年の休みが出てこない」という形。

見張るのは4つ。

1. 変換したあと、**アプリがそのまま開けて読める**こと
   (``source_db`` で開き、取り込みまで通す)
2. 日付が手元の SQLite と**同じ形**であること ── ここがずれると
   突き合わせが静かに外れる (削除の照合が効かなくなる)
3. ``journal_mode`` が DELETE であること ── WAL のまま共有フォルダへ
   置くと、そのファイルは誰からも開けなくなる
4. **黙って上書きしない**こと。移行の日にもう一度叩くのはよくある
"""

from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

from . import _isolation

_isolation.ensure_isolated()

from calendar_app import config, db  # noqa: E402
from calendar_app.dbkit import source_db  # noqa: E402

SAMPLE = Path(__file__).parent / "data" / "sample.accdb"
TOOL = Path(__file__).resolve().parent.parent / "tools" / "convert_accdb.py"


def _load():
    """``tools/`` は package ではないので、ファイルから直に読む。"""
    spec = importlib.util.spec_from_file_location("convert_accdb", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(SAMPLE.exists(), f"サンプル DB がありません: {SAMPLE}")
class ConvertTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tool = _load()

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.target = self.folder / "連絡帳.sqlite3"

    def convert(self, **kwargs):
        return self.tool.convert(SAMPLE, self.target, **kwargs)

    def test_カレンダーが読む表を変換する(self) -> None:
        report = self.convert()
        self.assertIn(config.TABLE_DATA, report["tables"])
        self.assertGreater(report["tables"][config.TABLE_DATA], 0)

    def test_変換しなかった表は黙って落とさない(self) -> None:
        """移行後に「あの表はどこへ行った」とならないように。"""
        report = self.convert()
        self.assertIsInstance(report["skipped"], list)

    def test_共有フォルダに置ける形で作る(self) -> None:
        """WAL のままだと、共有フォルダ(SMB)では誰からも開けなくなる。"""
        self.convert()
        conn = sqlite3.connect(self.target)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(mode.lower(), "delete")

    def test_アプリが開いて読める(self) -> None:
        """変換の合否は「アプリが読めたか」で決める。"""
        self.convert()
        result = source_db.probe(self.target)
        self.assertTrue(result.ok, result.describe())
        self.assertIn(config.TABLE_DATA, result.tables)

        with source_db.connect(self.target, read_only=True) as source:
            rows = source.query(
                f'SELECT * FROM "{config.TABLE_DATA}"')
        self.assertTrue(rows)
        self.assertIn("登録内容", rows[0])

    def test_日付は手元と同じ形(self) -> None:
        """形がずれると、削除の突き合わせが静かに外れる。"""
        self.convert()
        with source_db.connect(self.target, read_only=True) as source:
            rows = source.query(
                f'SELECT "日付" FROM "{config.TABLE_DATA}" WHERE "日付" <> \'\'')
        self.assertTrue(rows, "日付の入った行が1つもありません")
        for row in rows:
            value = row["日付"]
            self.assertRegex(value, r"^\d{4}/\d{2}/\d{2}$")

    def test_変換したものを取り込める(self) -> None:
        """移行の最後まで通す ── 変換 → 参照 → 手元へ取り込み。"""
        from calendar_app.importer import import_source

        self.convert()
        conn = db.connect(":memory:")
        self.addCleanup(conn.close)
        result = import_source(conn, str(self.target))
        self.assertGreater(result.total, 0, result.describe())

        count = conn.execute(
            f'SELECT COUNT(*) FROM "{config.TABLE_DATA}"').fetchone()[0]
        self.assertGreater(count, 0)

    def test_黙って上書きしない(self) -> None:
        """移行の日にもう一度叩くのはよくある。"""
        self.convert()
        with self.assertRaises(SystemExit):
            self.convert()

    def test_forceなら控えを取ってから置き換える(self) -> None:
        self.convert()
        self.convert(force=True)
        backups = list(self.folder.glob("連絡帳_変換前_*.sqlite3"))
        self.assertTrue(backups, "控えが作られていません")


if __name__ == "__main__":
    unittest.main()
