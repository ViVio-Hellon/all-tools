"""打った行・打った値を黙って失わない(VER4.1.0 の直し)

「打った行が消えることがありました」「打った値が勝手に戻ることが
ありました」── とんでもない話である。ここでは、そのとき見つかった
抜け道を1つずつ塞いだことを確かめる。

* 取り込みが共有を読んでいるあいだに登録された行が、入れ替えで消えた
* 送れない日に「消して同じ人・同じ日を登録し直す」と、両方とも消えた
* 送っている最中の行を消すと、次の取り込みで戻ってきた
* 同時に届いた2つの登録が、どちらも「まだ無い」と見て2行できた
* 返事が届かずに送り直した連絡が、2行できた
* 設定の保存が重なると、片方の値が古い値に戻った
* 参照パスを保存するたびに、切ったはずの自動同期が入り直った
* 背景の同期の最中に頼んだ送信が捨てられた
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from . import _web
from calendar_app import (
    config, db, importer, settings as user_settings, sync_service,
)
from calendar_app.dbkit import outbox_sync, source_db
from calendar_app.repository import Repository
from calendar_app.sync import deletes as sync_deletes
from calendar_app.sync import specs as sync_specs
from calendar_app.sync import total_pending_count
from calendar_app.sync.autosync import AutoSync, SqliteTransport

D = _dt.date(2026, 8, 3)
DS = "2026/08/03"


def _add_member(conn, code, name, group, line):
    conn.execute(
        f'INSERT INTO "{config.TABLE_MEMBER}" '
        '("管理番号","苗字","班","名前","読み","担当ライン") VALUES (?,?,?,?,?,?)',
        (code, name, group, name, name, line))
    conn.commit()


class _FileBase(unittest.TestCase):
    """手元のDBも**ファイル**で持つ(2本の接続で取り合うため)。"""

    def setUp(self) -> None:
        _web.reset_sync()
        self.addCleanup(_web.reset_sync)
        outbox_sync.reset_op_id_cache()
        self.addCleanup(outbox_sync.reset_op_id_cache)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        self.data_db = _web.make_source(
            self.folder, config.SOURCE_FILE_DATA, _web.SOURCE_SCHEMA)
        self.local_path = self.folder / "local.sqlite3"
        self.conn = db.connect(self.local_path)
        self.addCleanup(self.conn.close)
        self.repo = Repository(self.conn)

    def other(self) -> sqlite3.Connection:
        """同じ手元DBへの、別の接続(登録の API に当たる)。"""
        conn = db.connect(self.local_path)
        self.addCleanup(conn.close)
        return conn

    def source_rows(self) -> list[dict]:
        return _web.read_source(self.data_db, config.TABLE_DATA)


# ---------------------------------------------------------------------------
# 1. 取り込みのあいだに入った登録
# ---------------------------------------------------------------------------
class ImportRaceTests(_FileBase):
    """共有を読んでいるあいだに登録された行を、入れ替えで消さない。"""

    def _register_while_reading(self) -> None:
        """共有を読んでいる最中に、別の接続から登録する。"""
        original = source_db.SourceConnection.table_names
        other = self.other()

        def slow(source_self):
            names = original(source_self)
            Repository(other).save_record(D, config.KUBUN_OTHER, "読んでいる最中の連絡",
                                          "-", group="B", line="L-1")
            return names

        source_db.SourceConnection.table_names = slow
        self.addCleanup(setattr, source_db.SourceConnection, "table_names", original)

    def test_入れ替えずにやめて行は残る(self) -> None:
        self.assertEqual(total_pending_count(self.conn), 0)
        self._register_while_reading()
        with self.assertRaises(importer.PendingChangesError):
            importer.import_source(self.conn, self.data_db)
        rows = self.conn.execute(
            f'SELECT "登録内容" FROM "{config.TABLE_DATA}"').fetchall()
        self.assertEqual([r[0] for r in rows], ["読んでいる最中の連絡"])
        self.assertEqual(total_pending_count(self.conn), 1)
        # 錠は残していない(次の登録が待たされない)
        self.assertFalse(self.conn.in_transaction)

    def test_自動同期は見送って次に送る(self) -> None:
        auto = AutoSync(self.local_path, self.data_db)
        self._register_while_reading()
        self.assertFalse(auto.receive(self.conn))
        # 次の周期: 送ってから取り込み直す → 取り込み元にも手元にも1行
        source_db.SourceConnection.table_names = _ORIGINAL_TABLE_NAMES
        status = auto.sync_once(receive=True)
        self.assertEqual(status.pending, 0)
        self.assertEqual([r["登録内容"] for r in self.source_rows()], ["読んでいる最中の連絡"])
        local = self.conn.execute(
            f'SELECT COUNT(*) FROM "{config.TABLE_DATA}"').fetchone()[0]
        self.assertEqual(local, 1)

    def test_取り込みは手元の錠を取ってから入れ替える(self) -> None:
        """入れ替えの最中、別の接続は書けない(待たされる)。"""
        original = importer._IMPORTERS[config.TABLE_DATA]
        seen: list[str] = []

        def probe(conn, rows):
            other = sqlite3.connect(self.local_path, timeout=0.05)
            try:
                other.execute(f'INSERT INTO "{config.TABLE_DATA}" ("日付","区分") '
                              "VALUES ('2026/08/03','その他')")
                other.commit()
                seen.append("書けた")
            except sqlite3.OperationalError as exc:
                seen.append(str(exc))
            finally:
                other.close()
            return original(conn, rows)

        importer._IMPORTERS[config.TABLE_DATA] = probe
        self.addCleanup(importer._IMPORTERS.__setitem__, config.TABLE_DATA, original)
        importer.import_source(self.conn, self.data_db)
        self.assertEqual(len(seen), 1)
        self.assertIn("locked", seen[0])

    def test_名簿だけの取り込み元なら送信待ちがあっても断らない(self) -> None:
        master = _web.make_source(self.folder, config.SOURCE_FILE_MASTER,
                                  _web.MASTER_SCHEMA)
        self.repo.save_record(D, config.KUBUN_OTHER, "未送信", "-", group="B", line="L-1")
        result = importer.import_source(self.conn, master)
        self.assertIn(config.TABLE_MEMBER, result.tables)
        self.assertEqual(total_pending_count(self.conn), 1)

    def test_呼んだ側の取引を勝手に閉じない(self) -> None:
        """送信待ちを数える道(``ensure_sync_table``)が commit しない。"""
        self.conn.execute("BEGIN IMMEDIATE")
        total_pending_count(self.conn)
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()


_ORIGINAL_TABLE_NAMES = source_db.SourceConnection.table_names


# ---------------------------------------------------------------------------
# 2. 消してから登録し直す(送れない日)
# ---------------------------------------------------------------------------
class DeleteThenReRegisterTests(_FileBase):

    def _send(self) -> None:
        SqliteTransport().send(self.conn, str(self.data_db))

    def _rest(self, shift: str) -> int:
        return self.repo.save_record(D, config.KUBUN_REST, "山田太郎", "10",
                                     shift=shift, group="B", line="L-1")

    def test_消して登録し直した休みが残る(self) -> None:
        rid = self._rest("1")
        self._send()
        self.repo.delete_records_by_ids([rid])
        self._rest("2")
        self._send()
        rows = self.source_rows()
        self.assertEqual([(r["識別コード"], r["直"]) for r in rows], [("10", "2")])

    def test_削除が届くまで登録を送らずに待つ(self) -> None:
        """削除が届かなかった周期は、登録も「先に登録済み」で取りやめない。"""
        rid = self._rest("1")
        self._send()
        self.repo.delete_records_by_ids([rid])
        self._rest("2")

        original = sync_deletes.forward_pending_deletes
        sync_deletes.forward_pending_deletes = lambda conn, source, table: (0, ["届かない"])
        self.addCleanup(setattr, sync_deletes, "forward_pending_deletes", original)
        from calendar_app.sync.autosync import TransportError
        with self.assertRaises(TransportError):
            self._send()
        # 取りやめていない(送信待ちに残っている)
        self.assertGreaterEqual(total_pending_count(self.conn), 2)
        from calendar_app.sync import notices
        self.assertEqual(notices.pending(self.conn), [])

        sync_deletes.forward_pending_deletes = original
        self._send()
        rows = self.source_rows()
        self.assertEqual([(r["識別コード"], r["直"]) for r in rows], [("10", "2")])


# ---------------------------------------------------------------------------
# 3. 送っている最中に消す
# ---------------------------------------------------------------------------
class DeleteWhileSendingTests(_FileBase):

    def test_送っている最中の行も削除を予約し送信IDで消す(self) -> None:
        rid = self.repo.save_record(D, config.KUBUN_OTHER, "送信中に消す", "-",
                                    group="B", line="L-1")
        source = source_db.connect(self.data_db, read_only=False)
        self.addCleanup(source.close)
        outbox_sync.ensure_op_id_column(source, sync_specs.DATA_SPEC)
        # 背景の送信が予約して INSERT まで済ませ、「済」を書く前
        rows, op_ids = outbox_sync.claim_rows(self.other(), sync_specs.DATA_SPEC)
        values = {k: rows[0][k] for k in rows[0].keys() if k != "ID"}
        values[outbox_sync.DEFAULT_OP_ID_COLUMN] = op_ids[rid]
        source.insert(config.TABLE_DATA, values)

        self.repo.delete_records_by_ids([rid])
        pending = db.pending_deletes(self.conn)
        self.assertEqual(len(pending), 1)
        self.assertEqual(json.loads(pending[0]["natural_key"])[sync_deletes.OP_ID_KEY],
                         op_ids[rid])

        sent, errors = sync_deletes.forward_pending_deletes(
            self.conn, source, config.TABLE_DATA)
        self.assertEqual((sent, errors), (1, []))
        self.assertEqual(self.source_rows(), [])

    def test_送信IDで消すとき同じ文面の別の行は消さない(self) -> None:
        source = source_db.connect(self.data_db, read_only=False)
        self.addCleanup(source.close)
        outbox_sync.ensure_op_id_column(source, sync_specs.DATA_SPEC)
        other_line = {"日付": DS, "区分": config.KUBUN_OTHER, "登録内容": "残業なし",
                      "識別コード": "-", "直": "", "残業者": "", "早出者": "",
                      "班": "B", "ライン": "L-1", outbox_sync.DEFAULT_OP_ID_COLUMN: "other"}
        source.insert(config.TABLE_DATA, other_line)
        db.queue_pending_delete(self.conn, config.TABLE_DATA, access_id=None, natural_key={
            "日付": DS, "区分": config.KUBUN_OTHER, "登録内容": "残業なし",
            "識別コード": "-", "班": "B", "ライン": "L-1",
            sync_deletes.OP_ID_KEY: "never-arrived"})
        self.conn.commit()
        sync_deletes.forward_pending_deletes(self.conn, source, config.TABLE_DATA)
        self.assertEqual(len(self.source_rows()), 1)


# ---------------------------------------------------------------------------
# 4. 登録の API: 同時に届く・送り直す・使用中
# ---------------------------------------------------------------------------
class RegisterApiTests(unittest.TestCase):

    def setUp(self) -> None:
        _web.reset_sync()
        self.addCleanup(_web.reset_sync)
        _web.with_source(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local_path = Path(tmp.name) / "local.sqlite3"
        self.conn = db.connect(self.local_path)
        _add_member(self.conn, "10", "山田太郎", "B", "L-1")
        _web.bind_db(self, self.conn)
        self.client = _web.make_client()
        user_settings.save_my_line("L-1")

    def count(self, where: str = "1=1", params=()) -> int:
        return self.conn.execute(
            f'SELECT COUNT(*) FROM "{config.TABLE_DATA}" WHERE {where}', params).fetchone()[0]

    def test_同じ札の連絡は2行にならない(self) -> None:
        body = {"date": DS, "line": "L-1", "group": "B", "text": "送り直し",
                "submit_id": "abc-1"}
        first = self.client.post("/api/comment", json=body, headers=_web.auth())
        again = self.client.post("/api/comment", json=body, headers=_web.auth())
        self.assertEqual((first.status_code, again.status_code), (200, 200))
        self.assertEqual(self.count('"登録内容"=?', ("送り直し",)), 1)

    def test_札が違えば2行(self) -> None:
        for sid in ("a", "b"):
            self.client.post("/api/comment", json={
                "date": DS, "line": "L-1", "group": "B", "text": "別々", "submit_id": sid},
                headers=_web.auth())
        self.assertEqual(self.count('"登録内容"=?', ("別々",)), 2)

    def test_同じ札の休みも2行にならず断りもしない(self) -> None:
        body = {"date": DS, "code": "10", "shift": "1", "submit_id": "rest-1"}
        first = self.client.post("/api/rest", json=body, headers=_web.auth())
        again = self.client.post("/api/rest", json=body, headers=_web.auth())
        self.assertEqual((first.status_code, again.status_code), (200, 200))
        self.assertEqual(self.count('"識別コード"=?', ("10",)), 1)

    def test_手元が使用中なら503で入力を残すよう伝える(self) -> None:
        self.conn.execute("PRAGMA busy_timeout = 50")
        holder = sqlite3.connect(self.local_path)
        self.addCleanup(holder.close)
        holder.execute("BEGIN IMMEDIATE")
        try:
            res = self.client.post("/api/comment", json={
                "date": DS, "line": "L-1", "group": "B", "text": "使用中"},
                headers=_web.auth())
        finally:
            holder.rollback()
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.get_json()["error"]["code"], "busy")
        self.assertIn("入力はそのまま", res.get_json()["error"]["message"])
        self.assertEqual(self.count(), 0)
        self.assertFalse(self.conn.in_transaction)
        # 待てば通る(札は覚えていない)
        res = self.client.post("/api/comment", json={
            "date": DS, "line": "L-1", "group": "B", "text": "使用中"},
            headers=_web.auth())
        self.assertEqual(res.status_code, 200)

    def test_削除も使用中なら503(self) -> None:
        rid = Repository(self.conn).save_record(D, config.KUBUN_OTHER, "消す", "-",
                                                group="B", line="L-1")
        self.conn.execute("PRAGMA busy_timeout = 50")
        holder = sqlite3.connect(self.local_path)
        self.addCleanup(holder.close)
        holder.execute("BEGIN IMMEDIATE")
        try:
            res = self.client.post("/api/delete", json={"date": DS, "ids": [rid]},
                                   headers=_web.auth())
        finally:
            holder.rollback()
        self.assertEqual(res.status_code, 503)
        self.assertEqual(self.count(), 1)


class ConcurrentRestTests(unittest.TestCase):
    """同時に届いた同じ人・同じ日の休みは1行だけ(もう1つは 409)。"""

    def setUp(self) -> None:
        _web.reset_sync()
        self.addCleanup(_web.reset_sync)
        _web.with_source(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local_path = Path(tmp.name) / "local.sqlite3"
        setup = db.connect(self.local_path)
        _add_member(setup, "10", "山田太郎", "B", "L-1")
        setup.close()
        user_settings.save_my_line("L-1")

        from app import routes
        from app.routes import calendar as calendar_routes
        local = threading.local()
        self.local = local

        def get_db():
            if not hasattr(local, "conn"):
                local.conn = db.connect(self.local_path)
            return local.conn

        for module in (routes, calendar_routes):
            original = module.get_db
            module.get_db = get_db
            self.addCleanup(setattr, module, "get_db", original)

        # 確かめと書き込みの間で2つをそろえる(以前はここで両方とも「まだ無い」と見た)
        self.barrier = threading.Barrier(2, timeout=2)
        original_exists = Repository.exists_record

        def exists(repo_self, d, code):
            found = original_exists(repo_self, d, code)
            try:
                self.barrier.wait(timeout=0.5)
            except threading.BrokenBarrierError:
                pass
            return found

        Repository.exists_record = exists
        self.addCleanup(setattr, Repository, "exists_record", original_exists)

    def test_1行だけ(self) -> None:
        statuses: list[int] = []

        def post():
            client = _web.make_client()
            res = client.post("/api/rest", json={"date": DS, "code": "10", "shift": "1"},
                              headers=_web.auth())
            statuses.append(res.status_code)
            if hasattr(self.local, "conn"):
                self.local.conn.close()     # 自分のスレッドで閉じる

        threads = [threading.Thread(target=post) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        conn = sqlite3.connect(self.local_path)
        try:
            n = conn.execute(f'SELECT COUNT(*) FROM "{config.TABLE_DATA}"').fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 1)
        self.assertEqual(sorted(statuses), [200, 409])


# ---------------------------------------------------------------------------
# 5. 設定の保存
# ---------------------------------------------------------------------------
class SettingsWriteTests(unittest.TestCase):

    def setUp(self) -> None:
        self.before = user_settings.load()
        self.addCleanup(user_settings.save, self.before)

    def test_同時に保存しても両方残る(self) -> None:
        keys = [f"試験キー{i}" for i in range(12)]
        threads = [threading.Thread(target=user_settings.set_value, args=(key, i))
                   for i, key in enumerate(keys)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        data = user_settings.load()
        for i, key in enumerate(keys):
            self.assertEqual(data.get(key), i, key)
        # 一時ファイルを残さない
        leftovers = list(config.settings_path().parent.glob("*.tmp"))
        self.assertEqual(leftovers, [])

    def test_まとめて書く(self) -> None:
        user_settings.update({"試験A": 1, "試験B": 2})
        data = user_settings.load()
        self.assertEqual((data["試験A"], data["試験B"]), (1, 2))


class PathSaveKeepsAutoSyncOffTests(unittest.TestCase):

    def setUp(self) -> None:
        _web.reset_sync()
        self.addCleanup(_web.reset_sync)
        self.folder = _web.with_source(self)
        _web.bind_db(self)
        self.client = _web.make_client()
        self.before = user_settings.load()
        self.addCleanup(user_settings.save, self.before)

    def _save_path(self):
        return self.client.post("/api/settings/paths", json={
            "data_db_dir": str(self.folder), "password": _web.ADMIN_PASSWORD},
            headers=_web.auth())

    def test_切った自動同期を入れ直さない(self) -> None:
        user_settings.set_auto_sync(False)
        res = self._save_path()
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertFalse(user_settings.auto_sync_enabled())
        self.assertIn("切ったまま", res.get_json()["message"])

    def test_はじめて決めた端末は入る(self) -> None:
        data = user_settings.load()
        data.pop(user_settings.KEY_AUTO_SYNC, None)
        user_settings.save(data)
        res = self._save_path()
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertTrue(user_settings.has_value(user_settings.KEY_AUTO_SYNC))
        self.assertTrue(user_settings.auto_sync_enabled())

    def test_間隔で断ったら入切も変えない(self) -> None:
        user_settings.set_auto_sync(True)
        res = self.client.post("/api/settings/auto-sync",
                               json={"enabled": False, "interval": 1},
                               headers=_web.auth())
        self.assertEqual(res.status_code, 422)
        self.assertTrue(user_settings.auto_sync_enabled())


# ---------------------------------------------------------------------------
# 6. 同期の頼みを捨てない
# ---------------------------------------------------------------------------
class SyncRequestRerunTests(unittest.TestCase):

    def setUp(self) -> None:
        _web.reset_sync()
        self.addCleanup(_web.reset_sync)
        _web.with_source(self)
        self.service = sync_service.get_service()

    def test_走っているあいだの頼みは終わってからもう1回(self) -> None:
        calls: list[bool] = []
        gate = threading.Event()

        def fake(receive: bool) -> None:
            calls.append(receive)
            if len(calls) == 1:
                gate.wait(timeout=5)

        self.service._run_one = fake
        self.assertTrue(self.service.request(receive=True))
        time.sleep(0.05)
        # 取り込みの最中に登録 → 送信を頼む(以前は黙って捨てていた)
        self.assertFalse(self.service.request(receive=False))
        gate.set()
        self.assertTrue(sync_service.wait_idle(5))
        self.assertEqual(calls, [True, False])

    def test_頼まれなければ1回で終わる(self) -> None:
        calls: list[bool] = []
        self.service._run_one = lambda receive: calls.append(receive)
        self.service.request(receive=False)
        self.assertTrue(sync_service.wait_idle(5))
        self.assertEqual(calls, [False])


if __name__ == "__main__":
    unittest.main()
