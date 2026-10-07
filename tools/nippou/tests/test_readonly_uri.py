"""共有フォルダ(UNC)の上の sqlite3 を読むだけで開く (v4.18.0)

    AIM の控えを読めませんでした: invalid uri authority: nlmfangyshrd
    控えを読めませんでした: AIM:
    よくわかりません

【何が起きていたか】
記録を見る(控え)は `file:{道.as_posix()}?mode=ro` で開いていました。UNC の道は
`//サーバ/共有/…` になり、SQLite は `file://` の次をサーバ名(authority)と読んで
「空か localhost 以外は駄目」と断ります。

【約束】
    ・UNC はそのまま(Windows では `\\\\サーバ\\…`)、`//` で始まる道には空の authority を足す
    ・`%` `?` `#` は URI の中で意味を持つので % で書く
    ・書けない・無ければ作らない(mode=ro)
    ・開けなかったときの文は、どのファイルを・どの設定を確かめればよいかまで言う
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path, PureWindowsPath
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.connection import connect_readonly, readonly_uri  # noqa: E402
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402


class UriTests(unittest.TestCase):
    def test_UNCはサーバ名をauthorityにしない(self) -> None:
        unc = PureWindowsPath(r"\\nlmfangyshrd\共有\LocalBackup\AIM\日報入力データ.sqlite3")
        uri = readonly_uri(unc)
        self.assertEqual(uri, r"file:\\nlmfangyshrd\共有\LocalBackup\AIM\日報入力データ.sqlite3?mode=ro")
        self.assertFalse(uri.startswith("file://nlmfangyshrd"))

    def test_斜線のUNCは空のauthorityを足す(self) -> None:
        self.assertEqual(readonly_uri("//nlmfangyshrd/共有/x.sqlite3"),
                         "file:////nlmfangyshrd/共有/x.sqlite3?mode=ro")

    def test_ドライブとふつうの道(self) -> None:
        self.assertEqual(readonly_uri(PureWindowsPath(r"C:\Users\a\x.sqlite3")),
                         r"file:C:\Users\a\x.sqlite3?mode=ro")
        self.assertEqual(readonly_uri("/tmp/x.sqlite3"), "file:/tmp/x.sqlite3?mode=ro")

    def test_URIで意味を持つ字はパーセントで(self) -> None:
        self.assertEqual(readonly_uri("/tmp/a#1?100%.sqlite3"),
                         "file:/tmp/a%231%3f100%25.sqlite3?mode=ro")

    def test_本当に開けて_書けない(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # Windows のファイル名には「?」を使えない(その道は Windows の上には無い)
            odd = "a#1.sqlite3" if sys.platform == "win32" else "a#1?.sqlite3"
            for name in ("日報 控え.sqlite3", odd, "100%.sqlite3"):
                with self.subTest(name=name):
                    path = Path(tmp) / name
                    with sqlite3.connect(path) as conn:
                        conn.execute("CREATE TABLE t (x)")
                        conn.execute("INSERT INTO t VALUES (1)")
                    conn.close()
                    ro = connect_readonly(path)
                    try:
                        self.assertEqual(ro.execute("SELECT x FROM t").fetchone()["x"], 1)
                        with self.assertRaises(sqlite3.OperationalError):
                            ro.execute("INSERT INTO t VALUES (2)")
                    finally:
                        ro.close()

    def test_無ければ作らない(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "無い.sqlite3"
            with self.assertRaises(sqlite3.OperationalError):
                connect_readonly(path).execute("SELECT 1")
            self.assertFalse(path.exists())

    def test_URIで開くのは1か所だけ(self) -> None:
        """道から URI を組み立てるのは `db.connection.readonly_uri` だけ(ほかで組むと
        また `as_posix()` で UNC を壊す)。"""
        root = Path(__file__).resolve().parent.parent
        for path in [*root.joinpath("nippou").rglob("*.py"), *root.joinpath("app").rglob("*.py")]:
            if path.name == "connection.py" and path.parent.name == "db":
                continue
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=str(path.relative_to(root))):
                self.assertNotIn("uri=True", text)


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class BackupMessageTests(WebTestCase):
    def test_開けなかったときは_ファイルと設定の名前を言う(self) -> None:
        from nippou.services import access_rights, local_backup

        patcher = mock.patch.object(access_rights, "is_administrator", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        with mock.patch.object(local_backup, "list_shifts",
                               side_effect=sqlite3.OperationalError(
                                   "invalid uri authority: nlmfangyshrd")):
            res = self.get("/api/records/backup?line=AIM")
        self.assertEqual(res.status_code, 500)
        message = res.get_json()["error"]["message"]
        self.assertIn("AIM の控えのファイルを開けませんでした", message)
        self.assertIn(str(local_backup.path_for("AIM")), message)
        self.assertIn("「日報入力データの控えの置き場所」", message)
        self.assertIn("(詳しく: invalid uri authority: nlmfangyshrd)", message)


class WalSidecarTests(unittest.TestCase):
    """**読み終えたら、共有に -wal / -shm を残さない。**

    WAL の DB を読むだけ(`?mode=ro`)で開くと、閉じても付き添いを片付けられない。
    古い付き添いの隣でマスタを差し替えると、新しいマスタに古い -wal が当たって壊れる。
    """

    def test_WALのマスタを読んでも付き添いが残らず_書けない(self) -> None:
        import tempfile

        for wal in (True, False):
            with self.subTest(wal=wal), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "m.sqlite3"
                conn = sqlite3.connect(path)
                if wal:
                    conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("CREATE TABLE t (x)")
                conn.execute("INSERT INTO t VALUES (1)")
                conn.commit()
                conn.close()
                reader = connect_readonly(path)
                self.assertEqual(reader.execute("SELECT count(*) FROM t").fetchone()[0], 1)
                with self.assertRaises(sqlite3.Error):
                    reader.execute("INSERT INTO t VALUES (2)")
                reader.close()
                left = sorted(p.name for p in Path(tmp).iterdir() if p.name != path.name)
                self.assertEqual(left, [], "付き添いが残った")


if __name__ == "__main__":
    unittest.main()
