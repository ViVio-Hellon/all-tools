"""複数のPCが同じ共有を触ったときに、黙って消えないこと

【なぜこの試験があるか】
どれも**その場では気づけない**形で出ます。消した人の画面では消えて
いますし、直した人の画面では直っています。気づくのは何日かあとに
「あの連絡が来ていない」「班を直したのに戻っている」と言われたときで、
そのころには原因を辿れません。

1. **消しすぎない** ── 自分の1件を消したつもりで、他のラインの行まで
   消えない
2. **上書きしない** ── 同じ行を2人が開いたとき、後の保存が先の修正を
   黙って消さない
3. **控えが戻せる** ── 書かれている最中に取った控えが、開けること
4. **取り込みは1つの時点から** ── 表どうしがずれた組み合わせを
   取り込まない
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from . import _web
from calendar_app import (
    config,
    db as appdb,
    importer,
    master_admin,
    repository,
    settings as user_settings,
)
from calendar_app.dbkit import source_db
from calendar_app.sync.autosync import SqliteTransport

DAY = _dt.date(2026, 10, 5)


class _Shared(unittest.TestCase):
    """共有の取り込み元を1つ置いて、そこへ何台かの端末をぶら下げる。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.share = self.home / "share"
        self.share.mkdir()
        self.data = _web.make_source(self.share, config.SOURCE_FILE_DATA,
                                     _web.SOURCE_SCHEMA)
        self.master = _web.make_source(self.share, config.SOURCE_FILE_MASTER,
                                       _web.MASTER_SCHEMA)
        source_db.reset_backup_memory()
        self.addCleanup(source_db.reset_backup_memory)

    def terminal(self, name: str) -> sqlite3.Connection:
        """端末を1台。**手元のDBは端末ごとに別**。"""
        conn = appdb.connect(self.home / f"{name}.db")
        self.addCleanup(conn.close)
        return conn

    def send(self, conn: sqlite3.Connection) -> None:
        SqliteTransport().send(conn, str(self.data))

    def source_rows(self, table: str = config.TABLE_DATA,
                    path: Path | None = None) -> list[dict]:
        with source_db.connect(path or self.data, read_only=True) as source:
            return source.query(f'SELECT * FROM "{table}"')


class DeleteTests(_Shared):
    """**消しすぎない。**

    削除の転送は、取り込み元の ID がまだ分からないあいだ自然キーで
    対象を指す。連絡は識別コードが ``-`` 固定なので、**日付と文面しか
    手がかりが無い** ── 別のラインが同じ日に同じ文面を出していると、
    そちらまで当たる。実際に当たった(「残業なし」で他ラインの行が消えた)。
    """

    def two_comments(self, text: str = "残業なし"):
        """2台が、同じ日に同じ文面の連絡を出す。"""
        one, two = self.terminal("pc1"), self.terminal("pc2")
        repository.Repository(one).save_record(
            DAY, config.KUBUN_OTHER, text, "-", group="A", line="L-1")
        repository.Repository(two).save_record(
            DAY, config.KUBUN_OTHER, text, "-", group="B", line="HVC")
        self.send(one)
        self.send(two)
        return one, two

    def test_他のラインの同じ文面を消さない(self) -> None:
        one, _two = self.two_comments()
        self.assertEqual(len(self.source_rows()), 2)

        row_id = one.execute(
            f'SELECT "ID" FROM "{config.TABLE_DATA}"').fetchone()["ID"]
        repository.Repository(one).delete_records_by_ids([row_id])
        self.send(one)

        left = self.source_rows()
        self.assertEqual(len(left), 1, "他のラインの連絡まで消えている")
        self.assertEqual(left[0]["ライン"], "HVC")

    def test_自分の分はちゃんと消える(self) -> None:
        """消しすぎないために消し足りない、では意味が無い。"""
        one, _two = self.two_comments()
        row_id = one.execute(
            f'SELECT "ID" FROM "{config.TABLE_DATA}"').fetchone()["ID"]
        repository.Repository(one).delete_records_by_ids([row_id])
        self.send(one)
        self.assertNotIn("L-1", [r["ライン"] for r in self.source_rows()])

    def test_同じ行が2つあっても1つずつしか消さない(self) -> None:
        """自然キーは一意とはかぎらない。**取り返しがつく側に倒す。**"""
        with source_db.connect(self.data, read_only=False) as source:
            for _ in range(2):
                source.insert(config.TABLE_DATA, {
                    "日付": "2026/10/05", "区分": config.KUBUN_OTHER,
                    "登録内容": "同じ文面", "識別コード": "-",
                    "班": "A", "ライン": "L-1"})
            sql, params = source_db.build_delete_one(config.TABLE_DATA, {
                "日付": "2026/10/05", "登録内容": "同じ文面"})
            source.execute(sql, params)
        self.assertEqual(len(self.source_rows()), 1)

    def test_班とラインまで覚えておく(self) -> None:
        """消すときの手がかりを増やす ── 覚えていなければ絞れない。"""
        one = self.terminal("pc1")
        repository.Repository(one).save_record(
            DAY, config.KUBUN_OTHER, "連絡です", "-", group="A", line="L-1")
        self.send(one)
        row_id = one.execute(
            f'SELECT "ID" FROM "{config.TABLE_DATA}"').fetchone()["ID"]
        repository.Repository(one).delete_records_by_ids([row_id])

        row = one.execute(
            'SELECT "natural_key" FROM "_pending_deletes"').fetchone()
        import json
        key = json.loads(row["natural_key"])
        self.assertEqual(key["班"], "A")
        self.assertEqual(key["ライン"], "L-1")


class MasterOverwriteTests(_Shared):
    """**黙って上書きしない。**

    マスタ管理の画面は鍵以外の**全列**を送る(``views/master.js``)。
    2人が同じ行を開くと、後から保存したほうが、自分が触っていない欄まで
    開いたときの写しで上書きする ── 先に直した人の修正は消え、
    両方に「直しました」と出る。
    """

    ORIGINAL = {"管理番号": "10", "苗字": "山田", "班": "A",
                "名前": "山田太郎", "読み": "ヤマダ", "担当ライン": "L-1"}

    def setUp(self) -> None:
        super().setUp()
        with source_db.connect(self.master, read_only=False) as source:
            source.insert(config.TABLE_MEMBER, self.ORIGINAL)
        user_settings.set_value(user_settings.KEY_MASTER_DB_DIR, str(self.share))
        user_settings.set_value(user_settings.KEY_DATA_DB_DIR, str(self.share))
        self.addCleanup(user_settings.set_value,
                        user_settings.KEY_MASTER_DB_DIR, "")
        self.addCleanup(user_settings.set_value,
                        user_settings.KEY_DATA_DB_DIR, "")
        self.conn = self.terminal("master")
        #: 2人とも「開いた時点の写し」を持っている ── 画面の実際の動き
        self.opened = {k: v for k, v in self.ORIGINAL.items() if k != "管理番号"}

    def save(self, values: dict, expected=None):
        return master_admin.save_row(self.conn, config.TABLE_MEMBER, "10",
                                     values, expected)

    def member(self) -> dict:
        return self.source_rows(config.TABLE_MEMBER, self.master)[0]

    def test_先に直した人の修正が消えない(self) -> None:
        first = self.save(dict(self.opened, 班="B"), self.opened)
        self.assertTrue(first.ok, first.message)

        second = self.save(dict(self.opened, 名前="山田花子"), self.opened)
        self.assertFalse(second.ok, "開いたあとの変更を見ずに上書きした")
        self.assertEqual(second.reason, master_admin.REFUSE_STALE)
        self.assertEqual(self.member()["班"], "B")

    def test_断るときは何をすればよいか言う(self) -> None:
        self.save(dict(self.opened, 班="B"), self.opened)
        result = self.save(dict(self.opened, 名前="山田花子"), self.opened)
        self.assertIn("更新", result.message)

    def test_誰も触っていなければ直せる(self) -> None:
        result = self.save(dict(self.opened, 班="C"), self.opened)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(self.member()["班"], "C")

    def test_画面を更新すれば直せる(self) -> None:
        """断りは行き止まりではない ── いまの中身で出し直せば通る。"""
        self.save(dict(self.opened, 班="B"), self.opened)
        now = {k: v for k, v in self.member().items() if k != "管理番号"}
        result = self.save(dict(now, 名前="山田花子"), now)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(self.member()["班"], "B")
        self.assertEqual(self.member()["名前"], "山田花子")

    def test_開いたときの値を渡さない相手は素通し(self) -> None:
        """コマンド経由など。**確かめようが無いものを断る理由にしない。**"""
        self.assertTrue(self.save(dict(self.opened, 班="D")).ok)


class ApiOverwriteTests(unittest.TestCase):
    """入口(HTTP)でも同じことが言えるか。"""

    def setUp(self) -> None:
        _web.reset_sync()
        self.folder = _web.with_source(self, master=True)
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()
        source_db.reset_backup_memory()
        self.addCleanup(source_db.reset_backup_memory)
        path = self.folder / config.SOURCE_FILE_MASTER
        with source_db.connect(path, read_only=False) as source:
            source.insert(config.TABLE_MEMBER, {
                "管理番号": "10", "苗字": "山田", "班": "A",
                "名前": "山田太郎", "読み": "ヤマダ", "担当ライン": "L-1"})

    def save(self, values: dict, expected=None):
        body = {"table": config.TABLE_MEMBER, "key": "10", "values": values}
        if expected is not None:
            body["expected"] = expected
        return self.client.post("/api/master/save", json=body,
                                headers=_web.auth())

    def test_先に直されていれば409(self) -> None:
        """形は正しく、**別の誰かが先に直した**(設計 §1)。"""
        opened = {"苗字": "山田", "班": "A", "名前": "山田太郎",
                  "読み": "ヤマダ", "担当ライン": "L-1"}
        self.assertEqual(self.save(dict(opened, 班="B"), opened).status_code, 200)

        res = self.save(dict(opened, 名前="山田花子"), opened)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()["error"]["code"], "stale_row")

    def test_断っても今の中身は返す(self) -> None:
        """読みながら、いまどうなっているかを確かめられるように。"""
        opened = {"苗字": "山田", "班": "A", "名前": "山田太郎",
                  "読み": "ヤマダ", "担当ライン": "L-1"}
        self.save(dict(opened, 班="B"), opened)
        body = self.save(dict(opened, 名前="山田花子"), opened).get_json()
        self.assertIn("columns", body)
        self.assertIn("rows", body)


class BackupTests(_Shared):
    """**戻せる控えを取る。**

    ファイルを丸ごと写すと、写している最中に他の端末が書いていれば
    途中の状態が混ざりうる。「控えがあるつもりで戻せない」が一番困る。
    """

    def test_書かれている最中でも開ける控えになる(self) -> None:
        stop = threading.Event()

        def writer() -> None:
            conn = sqlite3.connect(str(self.data), timeout=10)
            conn.execute("PRAGMA busy_timeout = 10000")
            i = 0
            while not stop.is_set():
                try:
                    with conn:
                        conn.execute(
                            f'INSERT INTO "{config.TABLE_DATA}" '
                            '("日付","区分","登録内容","識別コード") '
                            "VALUES (?,?,?,?)",
                            ("2026/10/05", config.KUBUN_OTHER, f"連絡{i}", "-"))
                except sqlite3.Error:
                    pass
                i += 1
            conn.close()

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            copy = source_db.backup(self.data, self.home / "backup")
        finally:
            stop.set()
            thread.join()

        self.assertIsNotNone(copy)
        check = sqlite3.connect(str(copy))
        try:
            self.assertEqual(
                check.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            check.close()

    def test_続けて取り直さない(self) -> None:
        """1行直すたびに共有を丸ごと読むと、台数ぶん行き来が増える。"""
        first = source_db.backup(self.data, self.home / "backup")
        again = source_db.backup(self.data, self.home / "backup")
        self.assertEqual(first, again)

    def test_間隔を過ぎたら取り直す(self) -> None:
        first = source_db.backup(self.data, self.home / "backup")
        again = source_db.backup(self.data, self.home / "backup",
                                 min_interval_sec=0)
        self.assertNotEqual(first, again)

    def test_sqlite3でないファイルでも控えは残す(self) -> None:
        """中身が読めなくても、手元に置いておくこと自体に意味がある。"""
        junk = self.home / "こわれもの.sqlite3"
        junk.write_bytes("これは sqlite3 ではありません".encode("utf-8"))
        copy = source_db.backup(junk, self.home / "backup")
        self.assertIsNotNone(copy)
        self.assertEqual(copy.read_bytes(), junk.read_bytes())


class ImportSnapshotTests(_Shared):
    """**取り込みは1つの時点から。**

    休み管理・削除履歴・班員名簿を続けて読むので、1文ずつ読むと合間に
    他の端末が書ける ── 片方には有るのにもう片方から消えている、という
    組み合わせを取り込みうる。
    """

    def add_row(self, text: str) -> None:
        with source_db.connect(self.data, read_only=False) as source:
            source.insert(config.TABLE_DATA, {
                "日付": "2026/10/06", "区分": config.KUBUN_OTHER,
                "登録内容": text, "識別コード": "-"})

    def count(self, source) -> int:
        return int(source.query(
            f'SELECT COUNT(*) AS n FROM "{config.TABLE_DATA}"')[0]["n"])

    def test_まとめ読みの途中で書かれても眺めが変わらない(self) -> None:
        self.add_row("はじめの1件")

        writer = threading.Thread(target=self.add_row, args=("あとの1件",))
        with source_db.connect(self.data, read_only=True) as source:
            with source.reading():
                before = self.count(source)
                writer.start()          # 別の端末が書きにくる(待たされる)
                writer.join(timeout=1.0)
                after = self.count(source)
            self.assertEqual(before, after, "まとめ読みの途中で眺めが変わった")
        writer.join(timeout=15.0)
        self.assertFalse(writer.is_alive())

    def test_まとめ読みを抜ければ新しいものが見える(self) -> None:
        """一貫させるのは読んでいる間だけ。**居座らない。**"""
        with source_db.connect(self.data, read_only=True) as source:
            with source.reading():
                self.count(source)
            self.add_row("あとの1件")           # 抜けたので書ける
            self.assertEqual(self.count(source), 1)

    def test_取り込みは今までどおり通る(self) -> None:
        with source_db.connect(self.data, read_only=False) as source:
            source.insert(config.TABLE_DATA, {
                "日付": "2026/10/05", "区分": config.KUBUN_OTHER,
                "登録内容": "連絡", "識別コード": "-"})
        conn = self.terminal("pc1")
        result = importer.import_source(conn, self.data)
        self.assertEqual(result.tables[config.TABLE_DATA], 1)


if __name__ == "__main__":
    unittest.main()
