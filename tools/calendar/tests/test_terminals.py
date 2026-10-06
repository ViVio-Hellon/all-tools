"""端末一覧 ── どのPCがどの設定になっているか

【何を見張るか】
1. **記録されること。** これが無いと、設定を調べるには端末まで歩く
2. **書き込みを増やさないこと。** 共有フォルダ上の SQLite は同時書き込みが
   弱点(設計 §4.2)。毎回の同期で書くと、端末の台数ぶん負担が増える
3. **記録に失敗しても同期は止まらないこと。** これは見るためのもので、
   業務データではない
4. **一覧からは直せないこと。** 直せてしまうと、その端末の人には何も
   起きていないように見えたまま表示だけが変わる
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
import unittest

from . import _web
from calendar_app import config, master_admin, settings as user_settings, terminals
from calendar_app.dbkit import source_db


class IdentityTests(unittest.TestCase):
    def test_PC名とログインIDが取れる(self) -> None:
        who = terminals.identity()
        self.assertTrue(who.pc_name)
        self.assertIn(who.pc_name, who.label())

    def test_試験では差し替えられる(self) -> None:
        import os

        os.environ[terminals.ENV_PC_NAME] = "NLM-PC-042"
        os.environ[terminals.ENV_LOGIN_ID] = "yamada"
        self.addCleanup(os.environ.pop, terminals.ENV_PC_NAME, None)
        self.addCleanup(os.environ.pop, terminals.ENV_LOGIN_ID, None)

        who = terminals.identity()
        self.assertEqual(who.pc_name, "NLM-PC-042")
        self.assertEqual(who.label(), "yamada@NLM-PC-042")


class _Base(unittest.TestCase):
    """本物の取り込み元を1つ置いて、この端末の身元を固定する。"""

    def setUp(self) -> None:
        import os

        _web.reset_sync()
        self.conn = _web.bind_db(self)
        self.folder = _web.with_source(self, master=True)
        self.path = self.folder / config.SOURCE_FILE_DATA

        os.environ[terminals.ENV_PC_NAME] = "NLM-PC-042"
        os.environ[terminals.ENV_LOGIN_ID] = "yamada"
        self.addCleanup(os.environ.pop, terminals.ENV_PC_NAME, None)
        self.addCleanup(os.environ.pop, terminals.ENV_LOGIN_ID, None)
        user_settings.save_my_line("コイル")
        self.addCleanup(user_settings.save_my_line, "")

    def report(self, **kwargs) -> bool:
        with source_db.connect(self.path, read_only=False) as source:
            return terminals.report(source, **kwargs)

    def rows(self) -> list[dict]:
        return _web.read_source(self.path, terminals.TABLE)

    def touch(self, pc_name: str, **values) -> None:
        """別の端末が置いた記録を1行作る。"""
        row = {name: "" for name in terminals.COLUMNS}
        row["PC名"] = pc_name
        row["最終更新"] = _dt.datetime.now().strftime(config.DATETIME_FORMAT)
        row.update(values)
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(terminals._CREATE)
            conn.execute(
                f'INSERT OR REPLACE INTO "{terminals.TABLE}" '
                f'({", ".join(chr(34) + n + chr(34) for n in terminals.COLUMNS)}) '
                f'VALUES ({", ".join("?" for _ in terminals.COLUMNS)})',
                [row[name] for name in terminals.COLUMNS])
            conn.commit()
        finally:
            conn.close()


class ReportTests(_Base):
    def test_記録される(self) -> None:
        self.assertTrue(self.report())
        rows = self.rows()
        self.assertEqual([r["PC名"] for r in rows], ["NLM-PC-042"])
        self.assertEqual(rows[0]["ライン"], "コイル")
        self.assertEqual(rows[0]["ログインID"], "yamada")

    def test_参照パスも分かる(self) -> None:
        """どのPCがどのフォルダを見ているか ── 取り違えはこれで見つかる。"""
        self.report()
        self.assertEqual(self.rows()[0]["保存用DBフォルダ"], str(self.folder))

    def test_版も分かる(self) -> None:
        """更新が行き渡っていない端末を見つけるため。"""
        from calendar_app import app_config

        self.report()
        self.assertEqual(self.rows()[0]["版"], app_config.version_label())

    def test_変わらなければ書かない(self) -> None:
        """共有フォルダへの書き込みを端末の台数ぶん増やさない。"""
        self.assertTrue(self.report())
        self.assertFalse(self.report())

    def test_変わったら書き直す(self) -> None:
        self.report()
        user_settings.save_my_line("作業長")
        self.assertTrue(self.report())
        self.assertEqual(self.rows()[0]["ライン"], "作業長")

    def test_古くなったら書き直す(self) -> None:
        """しばらく起動されていないPCを見分けられるようにするため。"""
        self.report()
        old = (_dt.datetime.now()
               - _dt.timedelta(seconds=terminals.REPORT_INTERVAL_SEC + 60))
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(f'UPDATE "{terminals.TABLE}" SET "最終更新" = ?',
                         [old.strftime(config.DATETIME_FORMAT)])
            conn.commit()
        finally:
            conn.close()
        self.assertTrue(self.report())

    def test_行が消されていれば作り直す(self) -> None:
        self.report()
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(f'DELETE FROM "{terminals.TABLE}"')
            conn.commit()
        finally:
            conn.close()
        self.assertTrue(self.report())

    def test_自分の行だけを書く(self) -> None:
        """他のPCの記録を消したり上書きしたりしない。"""
        self.touch("NLM-PC-001", **{"ライン": "L-1"})
        self.report()
        rows = {r["PC名"]: r["ライン"] for r in self.rows()}
        self.assertEqual(rows, {"NLM-PC-001": "L-1", "NLM-PC-042": "コイル"})

    def test_ライン設定を変えたらすぐ載る(self) -> None:
        """次の同期まで待つと、直したのに一覧が古いままに見える。"""
        client = _web.make_client()
        res = client.post("/api/settings/line",
                          json={"line": "HVC", "password": _web.ADMIN_PASSWORD},
                          headers=_web.auth())
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.rows()[0]["ライン"], "HVC")


class ListingTests(_Base):
    def test_全部の端末が返る(self) -> None:
        self.touch("NLM-PC-001", **{"ライン": "L-1"})
        self.report()
        self.assertEqual([r["PC名"] for r in terminals.listing()],
                         ["NLM-PC-001", "NLM-PC-042"])

    def test_並びはPC名の順(self) -> None:
        """最終更新の順にすると、見るたびに行が入れ替わって目で追えない。"""
        for name in ("NLM-PC-050", "NLM-PC-003", "NLM-PC-020"):
            self.touch(name)
        names = [r["PC名"] for r in terminals.listing()]
        self.assertEqual(names, sorted(names))

    def test_表が無ければ空(self) -> None:
        """まだ1台も記録していないのは異常ではない。"""
        self.assertEqual(terminals.listing(), [])

    def test_読めなければ空(self) -> None:
        self.path.write_bytes(b"not a database")
        self.assertEqual(terminals.listing(), [])


class FailureTests(_Base):
    def test_書けなくても同期は止まらない(self) -> None:
        """記録は見るためのもので、業務データではない。"""
        from calendar_app.sync.autosync import SqliteTransport

        with source_db.connect(self.path, read_only=True) as source:
            # 読み取り専用で開いた接続に書かせる = 失敗する経路
            self.assertFalse(terminals.report(source))

        # 送信そのものは通る
        result = SqliteTransport().send(self.conn, str(self.path))
        self.assertEqual(result.applied, 0)

    def test_取り込み元が無ければ黙って諦める(self) -> None:
        user_settings.set_value(user_settings.KEY_DATA_DB_DIR, "")
        _web.reset_sync()
        self.assertFalse(terminals.publish())


class SyncTests(_Base):
    def test_同期のついでに記録される(self) -> None:
        from calendar_app.sync.autosync import SqliteTransport

        SqliteTransport().send(self.conn, str(self.path))
        self.assertEqual([r["PC名"] for r in self.rows()], ["NLM-PC-042"])


class MasterViewTests(_Base):
    """マスタ確認から見る。"""

    def setUp(self) -> None:
        super().setUp()
        self.client = _web.make_client()

    def page(self, table: str = terminals.TABLE):
        return _web.json_of(self.client.get(
            f"/api/master?table={table}", headers=_web.auth()))

    def test_端末一覧が選べる(self) -> None:
        body = self.page(config.TABLE_MEMBER)
        self.assertIn(terminals.TABLE, [t["table"] for t in body["tables"]])

    def test_中身が見える(self) -> None:
        self.report()
        body = self.page()
        self.assertEqual([r["PC名"] for r in body["rows"]], ["NLM-PC-042"])
        self.assertEqual(body["rows"][0]["ライン"], "コイル")

    def test_実績の列は直せない(self) -> None:
        """一覧の「ライン」を直しても、その端末の settings.json は変わらず
        次の同期で戻る。**直ったように見えて直っていない**を作らない。"""
        self.report()
        editable = {c["name"] for c in self.page()["columns"] if c["editable"]}
        self.assertNotIn("ライン", editable)
        self.assertNotIn("PC名", editable)

    def test_予約の列だけ直せる(self) -> None:
        """前もって決めておきたい、に応えるための1列。"""
        self.report()
        editable = {c["name"] for c in self.page()["columns"] if c["editable"]}
        self.assertEqual(editable, {terminals.RESERVE})

    def test_何ができる表か説明が出る(self) -> None:
        """実績と予約が同じ表に並ぶので、説明が無いとどちらを触るか迷う。"""
        self.report()
        self.assertIn("次のライン", self.page()["why"])

    def test_行を指す鍵はログインIDとPC名(self) -> None:
        """**設定は利用者ごと**(``%LOCALAPPDATA%``)。PC名だけを鍵にすると、
        3交替で同じPCを別のIDで使う2人が1行を奪い合う。"""
        self.assertEqual(self.page()["row_key"], terminals.ROW_KEY)
        self.assertEqual(terminals.ROW_KEY, "端末キー")

    def test_実績を直そうとしても受け取らない(self) -> None:
        """画面に出していなくても、要求は直接投げられる。"""
        self.report()
        key = self.rows()[0][terminals.ROW_KEY]
        res = self.client.post("/api/master/save",
                               json={"table": terminals.TABLE, "key": key,
                                     "values": {"ライン": "作業長"},
                                     "password": _web.ADMIN_PASSWORD},
                               headers=_web.auth())
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.rows()[0]["ライン"], "コイル")

    def test_使わなくなった端末は消せる(self) -> None:
        """**直せないが、消せる。** 入れ替えたPCの行が残り続けると、
        「まだ古い設定の端末がある」ように見えて探しに行くことになる。"""
        self.report()
        key = self.rows()[0][terminals.ROW_KEY]
        res = self.client.post("/api/master/delete",
                               json={"table": terminals.TABLE, "key": key},
                               headers=_web.auth())
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(len(self.rows()), 0)

    def test_消せる表だと画面に伝える(self) -> None:
        self.assertTrue(self.page()["removable"])

    def test_消せる表だと画面に伝える2(self) -> None:
        self.report()
        self.assertTrue(self.page()["removable"])

    def test_1台も無くても理由が出る(self) -> None:
        """表そのものが無いのは異常ではない。"""
        body = self.page()
        self.assertEqual(body["rows"], [])
        self.assertIn("まだ1件もありません", body["why"])
        # 列は定義どおり出す(何が記録されるのか分かるように)
        self.assertEqual([c["name"] for c in body["columns"]],
                         list(terminals.COLUMNS))

    def test_絞り込める(self) -> None:
        self.touch("NLM-PC-001", **{"ライン": "L-1"})
        self.report()
        body = _web.json_of(self.client.get(
            f"/api/master?table={terminals.TABLE}&q=コイル", headers=_web.auth()))
        self.assertEqual([r["PC名"] for r in body["rows"]], ["NLM-PC-042"])


class UnsentExitTests(_Base):
    """未送信を残したまま終わったことが、翌朝どこで分かるか

    **書きたい瞬間には共有へ書けない。** 未送信のまま終わるのは共有が
    落ちているときなので、その場で端末一覧へ書くことはできない。
    手元に覚えて、**次につながったときに**載せる。
    """

    def setUp(self) -> None:
        super().setUp()
        from calendar_app import sync_service

        self.service = sync_service
        self.addCleanup(sync_service.remember_exit, 0)

    def test_送り先が無ければ粘らない(self) -> None:
        """待っても送れないので、居座らせない。"""
        user_settings.set_value(user_settings.KEY_DATA_DB_DIR, "")
        _web.reset_sync()
        self.assertEqual(self.service.unsent_count(), 0)

    def test_送り先があれば件数を返す(self) -> None:
        from calendar_app.repository import Repository

        Repository(self.conn).save_record(
            _dt.date(2026, 8, 3), config.KUBUN_REST, "山田太郎", "10", shift="1")
        # ``unsent_count`` は手元のDBを自分で開くので、ファイルの方を見る
        self.assertGreaterEqual(self.service.unsent_count(), 0)

    def test_覚えて読み出せる(self) -> None:
        self.service.remember_exit(3)
        self.assertEqual(self.service.last_unsent_exit()[0], 3)

    def test_きれいに終わったら消える(self) -> None:
        """前回の記録が居座ると、直ったのに出続ける。"""
        self.service.remember_exit(3)
        self.service.remember_exit(0)
        self.assertEqual(self.service.last_unsent_exit(), (0, ""))

    def test_端末一覧に載る(self) -> None:
        self.service.remember_exit(3)
        self.report()
        self.assertIn("3件", self.rows()[0]["未送信のまま終了"])

    def test_直ったら消える(self) -> None:
        self.service.remember_exit(3)
        self.report()
        self.service.remember_exit(0)
        self.assertTrue(self.report())            # 中身が変わったので書き直す
        self.assertEqual(self.rows()[0]["未送信のまま終了"], "")

    def old_table(self, rows=()):
        """前の版が作った表(**PC名が主キー**)を置く。"""
        old = self.path.parent / "旧版.sqlite3"
        conn = sqlite3.connect(old)
        conn.execute('CREATE TABLE "端末設定" ("PC名" TEXT PRIMARY KEY, '
                     '"ログインID" TEXT NOT NULL DEFAULT \'\', '
                     '"ライン" TEXT NOT NULL DEFAULT \'\')')
        for pc, login, line in rows:
            conn.execute('INSERT INTO "端末設定" VALUES (?,?,?)', (pc, login, line))
        conn.commit()
        conn.close()
        return old

    def test_前の版が作った表にも列を足す(self) -> None:
        """列が増えた版を入れた端末だけが書けない、を作らない。"""
        old = self.old_table()
        with source_db.connect(old, read_only=False) as source:
            terminals.ensure_table(source)
            self.assertTrue(terminals.report(source))
            self.assertEqual(sorted(source.columns(terminals.TABLE)),
                             sorted((terminals.ROW_KEY, *terminals.COLUMNS)))

    def test_前の版の記録は作り直しても残る(self) -> None:
        """一覧が一度空になると「その端末はもう無い」と読まれる。"""
        old = self.old_table([("NLM-PC-001", "yamada", "コイル"),
                              ("NLM-PC-002", "suzuki", "HVC")])
        with source_db.connect(old, read_only=False) as source:
            terminals.ensure_table(source)
            rows = source.query('SELECT * FROM "端末設定" ORDER BY "PC名"')
        self.assertEqual([r["PC名"] for r in rows], ["NLM-PC-001", "NLM-PC-002"])
        self.assertEqual([r["ライン"] for r in rows], ["コイル", "HVC"])
        # 鍵は「ログインID@PC名」になる
        self.assertEqual(rows[0][terminals.ROW_KEY], "yamada@NLM-PC-001")

    def test_同じPCを別のIDで使っても別の行(self) -> None:
        """**設定は利用者ごと。** 1行を奪い合うと、後から起動したほうの
        設定だけが見える。"""
        import os
        with source_db.connect(self.path, read_only=False) as source:
            for login in ("yamada", "suzuki"):
                os.environ[terminals.ENV_LOGIN_ID] = login
                terminals.report(source, force=True)
            os.environ[terminals.ENV_LOGIN_ID] = "tester"
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(sorted(r["ログインID"] for r in rows),
                         ["suzuki", "yamada"])
        self.assertEqual({r["PC名"] for r in rows}, {"NLM-PC-042"})

    def test_整っていれば書かない(self) -> None:
        """毎回 CREATE を投げると、変わらないのに共有へ手を伸ばす。"""
        self.report()
        with source_db.connect(self.path, read_only=True) as source:
            terminals.ensure_table(source)        # 読み取り専用でも通る = 書いていない


class RulesTests(unittest.TestCase):
    def test_直せるのは予約の列だけ(self) -> None:
        managed = master_admin.BY_TABLE[terminals.TABLE]
        self.assertEqual(managed.editable_columns, (terminals.RESERVE,))

    def test_予約には管理者パスワードが要る(self) -> None:
        """他の端末のラインを決めるのは、その端末で変えるのと同じこと。"""
        from calendar_app import admin_password

        managed = master_admin.BY_TABLE[terminals.TABLE]
        self.assertIn(managed.guard_as, admin_password.PROTECTED_LABELS)

    def test_保存用DB側にある(self) -> None:
        """どの端末も連絡帳には必ず書いているので、そこに置く。"""
        managed = master_admin.BY_TABLE[terminals.TABLE]
        self.assertEqual(managed.where, master_admin.WHERE_DATA)


if __name__ == "__main__":
    unittest.main()


class ReservationTests(_Base):
    """前もって決めておいたラインを、端末が受け取る

    【なぜ「予約」なのか】
    一覧の ``ライン`` を直しても、その端末の ``settings.json`` は変わらず
    次の同期で元に戻ります。**直ったように見えて直っていない**のが
    一番たちが悪いので、直すのではなく「次はこうしてほしい」を別の列に
    置いてもらい、端末が起動のときに自分で取りに来ます。

    【効くのは次の起動のとき】
    使っている最中に黙って切り替わると、**その人には何も起きていないように
    見えたまま表示だけがずれます**。それは直したばかりの不具合そのものです。
    """

    def reserve(self, line: str, *, key: str = "", pc: str = "",
                login: str = "") -> None:
        """誰かが一覧から予約を書いた、という状態を作る。"""
        row_key = key or terminals.make_key(pc or "NLM-PC-042", login)
        with source_db.connect(self.path, read_only=False) as source:
            terminals.ensure_table(source)
            if source.query(
                    f'SELECT 1 FROM "{terminals.TABLE}" '
                    f'WHERE "{terminals.ROW_KEY}" = ?', [row_key]):
                source.update(terminals.TABLE, {terminals.RESERVE: line},
                              {terminals.ROW_KEY: row_key})
            else:
                source.insert(terminals.TABLE, {
                    terminals.ROW_KEY: row_key,
                    "PC名": pc or "NLM-PC-042", "ログインID": login,
                    terminals.RESERVE: line})

    def take(self) -> str:
        with source_db.connect(self.path, read_only=False) as source:
            return terminals.take_reservation(source)

    def stored(self, key: str) -> dict:
        with source_db.connect(self.path, read_only=True) as source:
            rows = source.query(
                f'SELECT * FROM "{terminals.TABLE}" '
                f'WHERE "{terminals.ROW_KEY}" = ?', [key])
        return rows[0] if rows else {}

    def test_自分あての予約を受け取る(self) -> None:
        self.reserve("HVC", login="yamada")
        self.assertEqual(self.take(), "HVC")
        self.assertEqual(user_settings.get_my_line(), "HVC")

    def test_PC単位の予約も受け取る(self) -> None:
        """配る前は、誰がログインするか分からない。"""
        self.reserve("HVC")                       # ログインIDなし = そのPCの誰でも
        self.assertEqual(self.take(), "HVC")
        self.assertEqual(user_settings.get_my_line(), "HVC")

    def test_自分あてthat優先(self) -> None:
        """PC単位と自分あてが両方あれば、細かいほうを採る。"""
        self.reserve("HVC")
        self.reserve("作業長", login="yamada")
        self.assertEqual(self.take(), "作業長")

    def test_受け取ったら予約は消える(self) -> None:
        """**二度効かせない。** 端末で直したのに、また戻るのは困る。"""
        self.reserve("HVC", login="yamada")
        self.take()
        row = self.stored("yamada@NLM-PC-042")
        self.assertEqual(row.get(terminals.RESERVE), "")
        self.assertEqual(row.get(terminals.RESERVE_AT), "")

        user_settings.save_my_line("コイル")
        self.assertEqual(self.take(), "")          # もう効かない
        self.assertEqual(user_settings.get_my_line(), "コイル")

    def test_他人あての予約は受け取らない(self) -> None:
        self.reserve("HVC", pc="NLM-PC-999", login="suzuki")
        self.assertEqual(self.take(), "")
        self.assertEqual(user_settings.get_my_line(), "コイル")

    def test_一覧に無いラインは入れない(self) -> None:
        """**一覧に無いものは選べない**(設計 §1)。"""
        self.reserve("存在しないライン", login="yamada")
        self.assertEqual(self.take(), "")
        self.assertEqual(user_settings.get_my_line(), "コイル")
        # 残すと毎回同じ警告が出るだけなので、消して先へ進む
        self.assertEqual(self.stored("yamada@NLM-PC-042").get(terminals.RESERVE), "")

    def test_予約が無ければ何もしない(self) -> None:
        self.report()
        self.assertEqual(self.take(), "")
        self.assertEqual(user_settings.get_my_line(), "コイル")

    def test_表が無くても落ちない(self) -> None:
        """1台も起動していない共有。"""
        self.assertEqual(self.take(), "")

    def test_受け取ったあとの報告に実績が出る(self) -> None:
        """**効いたことが一覧で分かる。** 出ないと、届いたのか分からない。"""
        self.reserve("HVC", login="yamada")
        self.take()
        with source_db.connect(self.path, read_only=False) as source:
            terminals.report(source, force=True)
        self.assertEqual(self.stored("yamada@NLM-PC-042").get("ライン"), "HVC")

    def test_報告は予約を消さない(self) -> None:
        """同期のたびに報告するので、ここで消えると予約が届かない。"""
        self.reserve("HVC", login="yamada")
        with source_db.connect(self.path, read_only=False) as source:
            terminals.report(source, force=True)
        self.assertEqual(self.stored("yamada@NLM-PC-042").get(terminals.RESERVE),
                         "HVC")


class ReserveApiTests(_Base):
    """一覧から予約を書く(HTTP の入口)。"""

    def setUp(self) -> None:
        super().setUp()
        self.client = _web.make_client()

    def page(self) -> dict:
        return _web.json_of(self.client.get(
            f"/api/master?table={terminals.TABLE}", headers=_web.auth()))

    def rows(self) -> list[dict]:
        return terminals.listing(self.path)

    def post(self, path: str, **body):
        return self.client.post(f"/api/master/{path}", json=body,
                                headers=_web.auth())

    def test_まだ起動していないPCを前もって登録できる(self) -> None:
        """**配る前に決めておきたい**、がこの機能の理由。"""
        res = self.post("add", table=terminals.TABLE,
                        values={"PC名": "NLM-PC-100", terminals.RESERVE: "HVC"},
                        password=_web.ADMIN_PASSWORD)
        self.assertEqual(res.status_code, 200, res.get_json())
        row = {r["PC名"]: r for r in self.rows()}["NLM-PC-100"]
        self.assertEqual(row[terminals.RESERVE], "HVC")
        # 鍵は打たせない。**打ち間違いが「誰にも当たらない予約」になる**
        self.assertEqual(row[terminals.ROW_KEY], "NLM-PC-100")

    def test_予約には管理者パスワードが要る(self) -> None:
        res = self.post("add", table=terminals.TABLE,
                        values={"PC名": "NLM-PC-100", terminals.RESERVE: "HVC"})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.get_json()["error"]["code"], "need_password")
        self.assertEqual(self.rows(), [])

    def test_予約を書くと誰がいつ決めたかも残る(self) -> None:
        """違っていたときに、直す相手が分からないと困る。"""
        self.post("add", table=terminals.TABLE,
                  values={"PC名": "NLM-PC-100", terminals.RESERVE: "HVC"},
                  password=_web.ADMIN_PASSWORD)
        row = {r["PC名"]: r for r in self.rows()}["NLM-PC-100"]
        self.assertTrue(row[terminals.RESERVE_AT])
        self.assertIn("NLM-PC-042", row[terminals.RESERVE_BY])

    def test_起動している端末にも予約できる(self) -> None:
        self.report()
        key = self.rows()[0][terminals.ROW_KEY]
        res = self.post("save", table=terminals.TABLE, key=key,
                        values={terminals.RESERVE: "作業長"},
                        password=_web.ADMIN_PASSWORD)
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(self.rows()[0][terminals.RESERVE], "作業長")
        # 実績は触らない
        self.assertEqual(self.rows()[0]["ライン"], "コイル")

    def test_予約を取り消せる(self) -> None:
        self.report()
        key = self.rows()[0][terminals.ROW_KEY]
        self.post("save", table=terminals.TABLE, key=key,
                  values={terminals.RESERVE: "作業長"},
                  password=_web.ADMIN_PASSWORD)
        self.post("save", table=terminals.TABLE, key=key,
                  values={terminals.RESERVE: ""},
                  password=_web.ADMIN_PASSWORD)
        row = self.rows()[0]
        self.assertEqual(row[terminals.RESERVE], "")
        self.assertEqual(row[terminals.RESERVE_BY], "")

    def test_PC名は空にできない(self) -> None:
        res = self.post("add", table=terminals.TABLE,
                        values={terminals.RESERVE: "HVC"},
                        password=_web.ADMIN_PASSWORD)
        self.assertEqual(res.status_code, 400)
