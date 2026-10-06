"""アクセス権限 ── この端末が使えるラインを、ログインID と PC名 で決める

【見張ること】
1. 表の読み方が**別ツール(梱包資材総合ツール)と同じ**
   (空欄は問わない・両方空の行は効かない・大文字小文字を区別しない・和を取る)
2. このツールが読むのは**権限がライン名の行だけ**。``mode:field`` などは読み飛ばし、
   消しも直しもしない
3. 表で決まっている端末は**そのラインに固定**(複数ならその中だけ)。表に無いラインは
   パスワードでも選べない。**切り替えにはいつでもパスワード**
4. マスタ管理で**行を足せる**(管理番号は自動)。足す・直す・消すには管理者パスワード
5. 表が無ければ**作る**(形は別ツールと同じ)
6. 表が変われば、同期でこの端末のラインが合う(閉じて開き直さない)
"""

from __future__ import annotations

import os
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from . import _web
from calendar_app import (
    access_control,
    config,
    db,
    master_admin,
    settings as user_settings,
)
from calendar_app.dbkit import source_db

ME = {"CALENDAR_LOGIN_ID": "yamada", "CALENDAR_PC_NAME": "NLM-PC-042"}

#: 梱包資材マスタにすでにある表の形(別ツールが作ったもの)
OTHER_TOOL_SCHEMA = (
    'CREATE TABLE "アクセス権限" ("管理番号" INTEGER PRIMARY KEY, '
    '"ログインID" TEXT DEFAULT \'\', "PC名" TEXT DEFAULT \'\', '
    '"権限" TEXT NOT NULL, "有効" INTEGER DEFAULT 1, "備考" TEXT DEFAULT \'\')')


def _as_me(case: unittest.TestCase) -> None:
    patcher = mock.patch.dict(os.environ, ME)
    patcher.start()
    case.addCleanup(patcher.stop)


def _local(rows: list[tuple]) -> sqlite3.Connection:
    conn = db.connect(":memory:")
    conn.executemany(
        'INSERT INTO "アクセス権限" ("ログインID","PC名","権限","有効") VALUES (?,?,?,?)',
        rows)
    conn.commit()
    return conn


class ResolveTests(unittest.TestCase):
    def setUp(self) -> None:
        _as_me(self)

    def lines(self, rows):
        conn = _local(rows)
        self.addCleanup(conn.close)
        return access_control.resolve(conn).lines

    def test_IDだけ_PCだけ_両方で当たる(self) -> None:
        self.assertEqual(self.lines([("yamada", "", "コイル", 1)]), ("コイル",))
        self.assertEqual(self.lines([("", "NLM-PC-042", "HVC", 1)]), ("HVC",))
        self.assertEqual(self.lines([("yamada", "NLM-PC-042", "機側", 1)]), ("機側",))

    def test_大文字小文字は区別しない(self) -> None:
        self.assertEqual(self.lines([("YAMADA", "nlm-pc-042", "コイル", 1)]), ("コイル",))

    def test_両方空の行は効かない(self) -> None:
        """全員への許可になり、権限を設ける意味が消える(別ツールと同じ決まり)。"""
        self.assertEqual(self.lines([("", "", "コイル", 1)]), ())

    def test_無効の行は効かない(self) -> None:
        self.assertEqual(self.lines([("yamada", "", "コイル", 0)]), ())

    def test_別の人_別のPCには効かない(self) -> None:
        self.assertEqual(self.lines([("suzuki", "", "コイル", 1),
                                     ("yamada", "OTHER-PC", "HVC", 1)]), ())

    def test_他ツールの値は読み飛ばす(self) -> None:
        self.assertEqual(self.lines([("yamada", "", "mode:field", 1),
                                     ("yamada", "", "mode:material", 1)]), ())

    def test_当たる行の和を_ラインの並び順で(self) -> None:
        self.assertEqual(self.lines([("yamada", "", "コイル", 1),
                                     ("", "NLM-PC-042", "作業長", 1),
                                     ("yamada", "", "mode:field", 1)]),
                         ("作業長", "コイル"))

    def test_表が無くても落ちない(self) -> None:
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        grant = access_control.resolve(conn)
        self.assertFalse(grant.managed)
        self.assertFalse(grant.has_table)
        self.assertIn("取り込まれていません", grant.explain())

    def test_登録が無ければ_足す値まで言う(self) -> None:
        conn = _local([("suzuki", "", "コイル", 1)])
        self.addCleanup(conn.close)
        text = access_control.resolve(conn).explain()
        self.assertIn("ログインID = yamada", text)
        self.assertIn("PC名 = NLM-PC-042", text)
        self.assertIn("パスワード", text)

    def test_打ち間違いらしいものだけ指摘する(self) -> None:
        """共用の表なので、他ツールの値(mode:...)は疑わせない。"""
        conn = _local([("yamada", "", "ｺｲﾙ", 1), ("yamada", "", "mode:field", 1),
                       ("yamada", "", "作業長 ", 1), ("", "", "HVC", 1)])
        self.addCleanup(conn.close)
        text = " / ".join(access_control.problems(conn))
        self.assertIn("ｺｲﾙ → コイル", text)
        self.assertNotIn("mode:field", text)
        self.assertIn("ログインID も PC名 も空", text)


class EnforceTests(unittest.TestCase):
    def setUp(self) -> None:
        _as_me(self)
        user_settings.save_my_line("作業長")
        self.addCleanup(user_settings.save_my_line, "")

    def test_表に無いラインなら合わせる(self) -> None:
        conn = _local([("yamada", "", "コイル", 1)])
        self.addCleanup(conn.close)
        applied = access_control.enforce(conn)
        self.assertTrue(applied.changed)
        self.assertEqual(user_settings.get_my_line(), "コイル")

    def test_表の中なら変えない(self) -> None:
        """利用者が表の中から選んだものは尊重する。"""
        conn = _local([("yamada", "", "コイル", 1), ("yamada", "", "作業長", 1)])
        self.addCleanup(conn.close)
        self.assertFalse(access_control.enforce(conn).changed)
        self.assertEqual(user_settings.get_my_line(), "作業長")

    def test_表で決まっていなければ何もしない(self) -> None:
        conn = _local([("suzuki", "", "コイル", 1)])
        self.addCleanup(conn.close)
        self.assertFalse(access_control.enforce(conn).changed)
        self.assertEqual(user_settings.get_my_line(), "作業長")


class SourceTableTests(unittest.TestCase):
    """取り込み元(梱包資材マスタ)の表。"""

    def setUp(self) -> None:
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / config.SOURCE_FILE_MASTER
        sqlite3.connect(self.path).close()

    def test_無ければ別ツールと同じ形で作る(self) -> None:
        with source_db.connect(self.path, read_only=False) as source:
            self.assertTrue(access_control.ensure_table(source))
            self.assertFalse(access_control.ensure_table(source))
        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)
        made = conn.execute("SELECT sql FROM sqlite_master WHERE name='アクセス権限'"
                            ).fetchone()[0]
        norm = lambda text: " ".join(text.replace("IF NOT EXISTS ", "").split())
        self.assertEqual(norm(made), norm(OTHER_TOOL_SCHEMA))

    def test_あれば形を変えない(self) -> None:
        conn = sqlite3.connect(self.path)
        conn.execute(OTHER_TOOL_SCHEMA)
        conn.execute('INSERT INTO "アクセス権限" ("ログインID","PC名","権限") '
                     "VALUES ('x','PC','mode:field')")
        conn.commit()
        conn.close()
        with source_db.connect(self.path, read_only=False) as source:
            self.assertFalse(access_control.ensure_table(source))
            self.assertEqual(len(source.query('SELECT * FROM "アクセス権限"')), 1)

    def test_手元へは全部の行を写す(self) -> None:
        """他ツールの行も写す(調べるときに、取り込み元と同じ中身で迷わない)。"""
        from calendar_app import importer

        conn = sqlite3.connect(self.path)
        conn.execute(OTHER_TOOL_SCHEMA)
        conn.executemany('INSERT INTO "アクセス権限" ("ログインID","PC名","権限","有効") '
                         'VALUES (?,?,?,?)',
                         [("yamada", "", "コイル", 1), ("x", "PC", "mode:field", None)])
        conn.commit()
        conn.close()
        local = db.connect(":memory:")
        self.addCleanup(local.close)
        self.assertEqual(importer.import_master_table(local, self.path, "アクセス権限"), 2)
        rows = local.execute('SELECT "権限","有効" FROM "アクセス権限" ORDER BY 1').fetchall()
        self.assertEqual([tuple(r) for r in rows], [("mode:field", 1), ("コイル", 1)])


class MasterAdminTests(unittest.TestCase):
    """マスタ管理で行を足す・直す・消す。"""

    def setUp(self) -> None:
        _as_me(self)
        self.folder = _web.with_source(self, master=True)
        self.master = self.folder / config.SOURCE_FILE_MASTER
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()

    def post(self, path: str, body: dict):
        return self.client.post(path, json={"table": "アクセス権限", **body},
                                headers=_web.auth())

    def rows(self) -> list[tuple]:
        conn = sqlite3.connect(self.master)
        try:
            return conn.execute('SELECT "管理番号","ログインID","PC名","権限","有効" '
                                'FROM "アクセス権限" ORDER BY 1').fetchall()
        finally:
            conn.close()

    def add(self, values: dict, password: str = config.ADMIN_PASSWORD):
        return self.post("/api/master/add", {"values": values, "password": password})

    def test_表の選択肢に出る(self) -> None:
        body = _web.json_of(self.client.get("/api/master?table=アクセス権限",
                                            headers=_web.auth()))
        self.assertIn("アクセス権限", [t["table"] for t in body["tables"]])
        names = {c["name"]: c for c in body["columns"]}
        self.assertFalse(names["管理番号"]["at_create"], "管理番号は打たせない")
        self.assertIn("コイル", names["権限"]["suggestions"])
        self.assertIn("mode:field", names["権限"]["suggestions"])

    def test_表が無ければ足すときに作る(self) -> None:
        res = self.add({"ログインID": "yamada", "PC名": "", "権限": "コイル"})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(self.rows(), [(1, "yamada", "", "コイル", 1)])

    def test_足すには管理者パスワード(self) -> None:
        res = self.add({"ログインID": "yamada", "権限": "コイル"}, password="")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.get_json()["error"]["code"], "need_password")

    def test_ライン以外の値も入れられる(self) -> None:
        """他ツールと共用の表なので、ライン名以外の文字列も入る。"""
        res = self.add({"ログインID": "yamada", "権限": "mode:material", "有効": "1"})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(self.rows()[0][3], "mode:material")

    def test_両方空の行は断る(self) -> None:
        res = self.add({"ログインID": "", "PC名": "", "権限": "コイル"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("少なくとも一方", res.get_json()["error"]["message"])

    def test_有効は1か0(self) -> None:
        self.assertEqual(self.add({"PC名": "PC", "権限": "コイル", "有効": "はい"}
                                  ).status_code, 400)
        self.assertEqual(self.add({"PC名": "PC", "権限": "コイル", "有効": "0"}
                                  ).status_code, 200)
        self.assertEqual(self.rows()[0][4], 0)

    def test_足したらこの端末のラインに効く(self) -> None:
        """手元にもすぐ写る(直したのに効かない、を作らない)。"""
        self.add({"ログインID": "yamada", "権限": "コイル"})
        body = _web.json_of(self.client.get("/api/settings", headers=_web.auth()))
        self.assertEqual(body["line_options"], ["コイル"])
        self.assertTrue(body["access"]["managed"])
        self.assertTrue(body["access"]["fixed"])

    def test_直すにも消すにも管理者パスワード(self) -> None:
        self.add({"ログインID": "yamada", "権限": "コイル"})
        res = self.post("/api/master/save", {"key": "1", "values": {"権限": "HVC"}})
        self.assertEqual(res.status_code, 403)
        res = self.post("/api/master/save", {"key": "1", "values": {"権限": "HVC"},
                                             "password": config.ADMIN_PASSWORD})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(self.rows()[0][3], "HVC")
        self.assertEqual(self.post("/api/master/delete", {"key": "1"}).status_code, 403)
        res = self.post("/api/master/delete", {"key": "1",
                                               "password": config.ADMIN_PASSWORD})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.rows(), [])

    def test_他ツールの行は残る(self) -> None:
        conn = sqlite3.connect(self.master)
        conn.execute(OTHER_TOOL_SCHEMA)
        conn.execute('INSERT INTO "アクセス権限" ("ログインID","PC名","権限") '
                     "VALUES ('yamada','NLM-PC-042','mode:field')")
        conn.commit()
        conn.close()
        self.add({"ログインID": "yamada", "権限": "コイル"})
        self.assertEqual([r[3] for r in self.rows()], ["mode:field", "コイル"])


class LineSettingTests(unittest.TestCase):
    """この端末のラインを変える(``POST /api/settings/line``)。"""

    def setUp(self) -> None:
        _as_me(self)
        _web.reset_sync()
        self.addCleanup(_web.reset_sync)
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()
        user_settings.save_my_line("コイル")
        self.addCleanup(user_settings.save_my_line, "")

    def grant(self, *rows) -> None:
        self.conn.executemany('INSERT INTO "アクセス権限" ("ログインID","PC名","権限","有効") '
                              'VALUES (?,?,?,1)', rows)
        self.conn.commit()

    def set_line(self, line: str, password: str = ""):
        return self.client.post("/api/settings/line",
                                json={"line": line, "password": password},
                                headers=_web.auth())

    def test_1つなら固定_画面でも選べない(self) -> None:
        self.grant(("yamada", "", "コイル"), ("yamada", "", "mode:field"))
        body = _web.json_of(self.client.get("/api/settings", headers=_web.auth()))
        self.assertEqual(body["line_options"], ["コイル"])
        self.assertTrue(body["access"]["fixed"])
        self.assertIn("固定", body["access"]["explain"])

    def test_表の中でも切り替えにはパスワード(self) -> None:
        """権限は「使ってよいライン」で、「勝手に変えてよい」ではない。"""
        self.grant(("yamada", "", "コイル"), ("", "NLM-PC-042", "HVC"))
        res = self.set_line("HVC")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(user_settings.get_my_line(), "コイル")
        res = self.set_line("HVC", password=config.ADMIN_PASSWORD)
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(user_settings.get_my_line(), "HVC")
        body = _web.json_of(self.client.get("/api/settings", headers=_web.auth()))
        self.assertFalse(body["access"]["fixed"])

    def test_表に無いラインは選べない(self) -> None:
        """パスワードを知っていても通さない(管理の出どころは表)。"""
        self.grant(("yamada", "", "コイル"))
        res = self.set_line("作業長", password=config.ADMIN_PASSWORD)
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"], "not_granted")
        self.assertIn("固定", res.get_json()["error"]["message"])
        self.assertEqual(user_settings.get_my_line(), "コイル")

    def test_表で決まっていなければ今までどおりパスワード(self) -> None:
        self.grant(("suzuki", "", "HVC"))
        res = self.set_line("HVC")
        self.assertEqual(res.status_code, 403)
        body = _web.json_of(self.client.get("/api/settings", headers=_web.auth()))
        self.assertEqual(body["line_options"], list(config.ALL_LINE_NAMES))
        self.assertFalse(body["access"]["managed"])


class SyncTests(unittest.TestCase):
    """同期で表を取り込み直すと、この端末のラインが合う。"""

    def setUp(self) -> None:
        _as_me(self)
        self.folder = _web.with_source(self, master=True)
        self.master = self.folder / config.SOURCE_FILE_MASTER
        user_settings.save_my_line("作業長")
        self.addCleanup(user_settings.save_my_line, "")

    def test_表が無ければ作って_取り込んで_ラインを合わせる(self) -> None:
        from calendar_app.sync.autosync import AutoSync

        data = self.folder / config.SOURCE_FILE_DATA
        local = db.connect(":memory:")
        self.addCleanup(local.close)
        sync = AutoSync(":memory:", str(data), master_path=str(self.master))
        self.assertTrue(sync.receive(local))
        conn = sqlite3.connect(self.master)
        self.assertTrue(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='アクセス権限'").fetchone())
        # 管理者がマスタ管理で行を足した
        conn.execute('INSERT INTO "アクセス権限" ("ログインID","PC名","権限") '
                     "VALUES ('yamada','','コイル')")
        conn.commit()
        conn.close()
        self.assertTrue(sync.receive(local))
        self.assertEqual(user_settings.get_my_line(), "コイル")


if __name__ == "__main__":
    unittest.main()
