"""取り込み元 (共有の sqlite3) との往復

【Access のころとの違い】
書き込み経路が**この環境でも試せる**ようになった。以前は ODBC ドライバか
cscript が要り、Linux では1行も動かせなかったので、送信は「差し替えた
偽の Transport」でしか確かめられなかった。いまは本物の sqlite3 を作って、
**実際に書かれた中身**まで見ている。
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from . import _web
from calendar_app import (
    config, db, importer, settings as user_settings, sources, sync_service,
)
from calendar_app.dbkit import outbox_sync, source_db
from calendar_app.repository import Repository
from calendar_app.sync import specs as sync_specs
from calendar_app.sync import total_pending_count
from calendar_app.sync.autosync import AutoSync, SqliteTransport, SyncState

import datetime as _dt

D = _dt.date(2026, 8, 3)
D2 = _dt.date(2026, 8, 4)


class _Base(unittest.TestCase):
    """手元のDBと、本物の取り込み元を1組ずつ持つ。"""

    def setUp(self) -> None:
        _web.reset_sync()
        self.addCleanup(_web.reset_sync)
        outbox_sync.reset_op_id_cache()
        self.addCleanup(outbox_sync.reset_op_id_cache)

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.data_db = _web.make_source(
            self.folder, config.SOURCE_FILE_DATA, _web.SOURCE_SCHEMA)
        self.master_db = _web.make_source(
            self.folder, config.SOURCE_FILE_MASTER, _web.MASTER_SCHEMA)

        user_settings.set_value(user_settings.KEY_DATA_DB_DIR, str(self.folder))
        user_settings.set_value(user_settings.KEY_MASTER_DB_DIR, str(self.folder))
        self.addCleanup(user_settings.set_value, user_settings.KEY_DATA_DB_DIR, "")
        self.addCleanup(user_settings.set_value, user_settings.KEY_MASTER_DB_DIR, "")
        user_settings.save_my_line("L-1")

        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.repo = Repository(self.conn)

    def rows_in_source(self, table: str = config.TABLE_DATA) -> list[dict]:
        return _web.read_source(self.data_db, table)

    def send(self) -> None:
        SqliteTransport().send(self.conn, str(self.data_db))


class SendTests(_Base):
    """送信 (手元 → 取り込み元)。**実際に書かれた中身まで見る。**"""

    def test_休みが取り込み元に入る(self) -> None:
        self.repo.save_record(D, config.KUBUN_REST, "山田太郎", "10",
                              shift="1", group="B", line="L-1")
        self.send()

        rows = self.rows_in_source()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["登録内容"], "山田太郎")
        self.assertEqual(rows[0]["日付"], "2026/08/03")
        self.assertEqual(rows[0]["直"], "1")

    def test_送ったら送信待ちが減る(self) -> None:
        self.repo.save_record(D, config.KUBUN_REST, "山田太郎", "10", shift="1")
        self.assertEqual(total_pending_count(self.conn), 1)
        self.send()
        self.assertEqual(total_pending_count(self.conn), 0)

    def test_二度送っても増えない(self) -> None:
        """送信済みの行は拾わない(送信記録)。"""
        self.repo.save_record(D, config.KUBUN_REST, "山田太郎", "10", shift="1")
        self.send()
        self.send()
        self.assertEqual(len(self.rows_in_source()), 1)

    def test_access_id列は送らない(self) -> None:
        """SQLite だけが持つ列。取り込み元には無いので送ると失敗する。"""
        self.repo.save_record(D, config.KUBUN_REST, "山田太郎", "10", shift="1")
        self.send()
        self.assertNotIn("access_id", self.rows_in_source()[0])

    def test_削除履歴も送る(self) -> None:
        record_id = self.repo.save_record(D, config.KUBUN_REST, "消される人", "20",
                                          shift="1")
        self.send()
        self.repo.delete_records_by_ids([record_id])
        self.send()

        history = self.rows_in_source(config.TABLE_DEL_HISTORY)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["登録内容"], "消される人")

    def test_削除が取り込み元にも効く(self) -> None:
        record_id = self.repo.save_record(D, config.KUBUN_REST, "消される人", "20",
                                          shift="1")
        self.send()
        self.assertEqual(len(self.rows_in_source()), 1)

        self.repo.delete_records_by_ids([record_id])
        self.send()
        self.assertEqual(self.rows_in_source(), [])

    def test_送る前に消した分は送らない(self) -> None:
        """登録してすぐ削除した行は、取り込み元へ往復させない。"""
        record_id = self.repo.save_record(D, config.KUBUN_REST, "取消される人", "20",
                                          shift="1")
        self.repo.delete_records_by_ids([record_id])
        self.send()

        self.assertEqual(self.rows_in_source(), [])
        # 削除履歴だけは残る
        self.assertEqual(len(self.rows_in_source(config.TABLE_DEL_HISTORY)), 1)
        self.assertEqual(total_pending_count(self.conn), 0)

    def test_送信ID列が足される(self) -> None:
        """再送で二重に入らないよう、取り込み元に送信ID列と一意索引を作る。"""
        self.repo.save_record(D, config.KUBUN_REST, "山田太郎", "10", shift="1")
        self.send()
        self.assertIn(outbox_sync.DEFAULT_OP_ID_COLUMN, self.rows_in_source()[0])


class DuplicateGuardTests(_Base):
    """休みの二重登録防止 (VBA の ExistsRecord 相当)。"""

    def test_他ラインが先に入れていたら送らない(self) -> None:
        # 別のラインが同じ人の同じ日を先に登録した状態を作る
        conn = sqlite3.connect(self.data_db)
        conn.execute(
            f'INSERT INTO "{config.TABLE_DATA}" '
            '("日付","区分","登録内容","識別コード") VALUES (?,?,?,?)',
            ("2026/08/03", config.KUBUN_REST, "山田太郎", "10"))
        conn.commit()
        conn.close()

        self.repo.save_record(D, config.KUBUN_REST, "山田太郎", "10", shift="1")
        self.send()

        # 増えていない = 送らずに済ませた
        self.assertEqual(len(self.rows_in_source()), 1)
        # 送信待ちからは外れる(送る必要が無いと分かったので)
        self.assertEqual(total_pending_count(self.conn), 0)

    def test_送らずに取りやめたらその端末に知らせる(self) -> None:
        """**黙って取りやめない。** 先に届いた側と直や繋ぎが違えば、
        こちらで選んだ内容は消えている(2台で同時に操作して見つかった)。"""
        from calendar_app.sync import notices

        conn = sqlite3.connect(self.data_db)
        conn.execute(
            f'INSERT INTO "{config.TABLE_DATA}" '
            '("日付","区分","登録内容","識別コード","直") VALUES (?,?,?,?,?)',
            ("2026/08/03", config.KUBUN_REST, "山田太郎", "10", "2"))
        conn.commit()
        conn.close()

        self.repo.save_record(D, config.KUBUN_REST, "山田太郎", "10", shift="1")
        self.send()
        items = notices.pending(self.conn)
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]["date"], items[0]["name"], items[0]["shift"]),
                         ("2026/08/03", "山田太郎", "1"))
        text = notices.describe(items)["text"]
        self.assertIn("8月3日 山田太郎さんの休み(1直)", text)
        self.assertIn("先に", text)
        # 確かめたら消える
        self.assertEqual(notices.acknowledge(self.conn), 1)
        self.assertEqual(notices.pending(self.conn), [])

    def test_送れたときは知らせない(self) -> None:
        from calendar_app.sync import notices

        self.repo.save_record(D, config.KUBUN_REST, "山田太郎", "10", shift="1")
        self.send()
        self.assertEqual(notices.pending(self.conn), [])

    def test_同じ日でも別の人なら送る(self) -> None:
        self.repo.save_record(D, config.KUBUN_REST, "山田太郎", "10", shift="1")
        self.repo.save_record(D, config.KUBUN_REST, "鈴木一郎", "11", shift="2")
        self.send()
        self.assertEqual(len(self.rows_in_source()), 2)

    def test_コメントは何件でも送る(self) -> None:
        """同じ日に複数の連絡を出せる ── 重複確認をしない。"""
        self.repo.save_record(D, config.KUBUN_OTHER, "連絡1", "-", line="L-1", group="B")
        self.repo.save_record(D, config.KUBUN_OTHER, "連絡2", "-", line="L-1", group="B")
        self.send()
        self.assertEqual(len(self.rows_in_source()), 2)


class ReceiveTests(_Base):
    """受信 (取り込み元 → 手元)。"""

    def _put(self, **values) -> None:
        conn = sqlite3.connect(self.data_db)
        cols = ", ".join(f'"{c}"' for c in values)
        marks = ", ".join("?" for _ in values)
        conn.execute(
            f'INSERT INTO "{config.TABLE_DATA}" ({cols}) VALUES ({marks})',
            list(values.values()))
        conn.commit()
        conn.close()

    def test_取り込める(self) -> None:
        self._put(ID=5, 日付="2026/08/03", 区分=config.KUBUN_REST,
                  登録内容="他ラインの人", 識別コード="99", ライン="L-1", 班="B")
        importer.import_source(self.conn, self.data_db)

        records = self.repo.get_day_records(D)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].naiyou, "他ラインの人")
        # 取り込み元の ID をそのまま主キーに使う
        self.assertEqual(records[0].id, 5)

    def test_総入れ替えになる(self) -> None:
        """取り込み元が正。手元にしか無い行は消える。"""
        self.repo.save_record(D, config.KUBUN_REST, "手元だけの人", "77", shift="1")
        self.send()                       # 送ってから (未送信があると取り込まない)
        # 取り込み元から消してしまう = 他の端末が消した状態
        conn = sqlite3.connect(self.data_db)
        conn.execute(f'DELETE FROM "{config.TABLE_DATA}"')
        conn.commit()
        conn.close()

        importer.import_source(self.conn, self.data_db)
        self.assertEqual(self.repo.get_day_records(D), [])

    def test_未送信があると断る(self) -> None:
        self.repo.save_record(D, config.KUBUN_REST, "未送信の人", "99", shift="1")
        with self.assertRaises(importer.PendingChangesError):
            importer.import_source(self.conn, self.data_db)

    def test_forceなら破棄して取り込む(self) -> None:
        self.repo.save_record(D, config.KUBUN_REST, "未送信の人", "99", shift="1")
        importer.import_source(self.conn, self.data_db, force=True)
        self.assertEqual(self.repo.get_day_records(D), [])

    def test_取り込み直しても二重送信しない(self) -> None:
        """取り込んだ行は「送信済み」として記録し直す。

        ここを飛ばすと、取り込んだばかりの行が「未送信」に見えて、
        次の送信が取り込み元へもう一度 INSERT する(倍に増える)。
        """
        self._put(ID=5, 日付="2026/08/03", 区分=config.KUBUN_REST,
                  登録内容="他ラインの人", 識別コード="99")
        importer.import_source(self.conn, self.data_db)
        self.assertEqual(total_pending_count(self.conn), 0)

        self.send()
        self.assertEqual(len(self.rows_in_source()), 1)

    def test_新しいIDが取り込み元と衝突しない(self) -> None:
        self._put(ID=100, 日付="2026/08/01", 区分=config.KUBUN_REST,
                  登録内容="既存", 識別コード="1")
        importer.import_source(self.conn, self.data_db)

        new_id = self.repo.save_record(D, config.KUBUN_REST, "新しい人", "50",
                                       shift="1")
        self.assertGreater(new_id, 100)

    def test_ISO形式の日付も読める(self) -> None:
        """別の手段で作られた取り込み元に備える。"""
        self._put(ID=7, 日付="2026-08-03", 区分=config.KUBUN_REST,
                  登録内容="ISOの人", 識別コード="70")
        importer.import_source(self.conn, self.data_db)
        self.assertEqual(len(self.repo.get_day_records(D)), 1)


class MasterImportTests(_Base):
    """班員名簿の取り込み。"""

    def _put_member(self, code: str, name: str, group: str, line: str) -> None:
        conn = sqlite3.connect(self.master_db)
        conn.execute(
            f'INSERT OR REPLACE INTO "{config.TABLE_MEMBER}" '
            '("管理番号","苗字","班","名前","読み","担当ライン") VALUES (?,?,?,?,?,?)',
            (code, name, group, name, name, line))
        conn.commit()
        conn.close()

    def test_取り込める(self) -> None:
        self._put_member("10", "山田太郎", "B", "L-1")
        importer.import_source(self.conn, self.master_db)
        self.assertEqual(self.repo.member_count(), 1)

    def test_管理番号が無い行は捨てる(self) -> None:
        self._put_member("", "名無し", "B", "L-1")
        importer.import_source(self.conn, self.master_db)
        self.assertEqual(self.repo.member_count(), 0)

    def test_1表だけ取り込み直せる(self) -> None:
        """マスタ管理が書いた直後に、手元を追いつかせるために使う。"""
        self._put_member("10", "山田太郎", "B", "L-1")
        count = importer.import_master_table(
            self.conn, self.master_db, config.TABLE_MEMBER)
        self.assertEqual(count, 1)
        self.assertEqual(self.repo.member_count(), 1)

    def test_書き戻す表は単独で取り込めない(self) -> None:
        """休み管理を単独で入れ替えると、未送信の入力が消える。"""
        with self.assertRaises(ValueError):
            importer.import_master_table(
                self.conn, self.data_db, config.TABLE_DATA)

    def test_未送信があっても名簿は取り込める(self) -> None:
        """班員名簿は書き戻しが無いので、送信待ちを気にしなくてよい。"""
        self.repo.save_record(D, config.KUBUN_REST, "未送信の人", "99", shift="1")
        self._put_member("10", "山田太郎", "B", "L-1")
        importer.import_master_table(self.conn, self.master_db, config.TABLE_MEMBER)
        self.assertEqual(self.repo.member_count(), 1)
        # 未送信の入力は残っている
        self.assertEqual(total_pending_count(self.conn), 1)


class AutoSyncTests(_Base):
    """送信 → 受信 を1回まわす。"""

    def test_一巡できる(self) -> None:
        sync = AutoSync(":memory:", str(self.data_db))
        self.repo.save_record(D, config.KUBUN_REST, "山田太郎", "10", shift="1")
        sync.send(self.conn)

        status = sync.current_status(self.conn)
        self.assertIs(status.state, SyncState.SYNCED)
        self.assertEqual(len(self.rows_in_source()), 1)

    def test_取り込み元が無ければ状態で伝える(self) -> None:
        sync = AutoSync(":memory:", str(self.folder / "無い.sqlite3"))
        status = sync.sync_once()
        self.assertIs(status.state, SyncState.OFFLINE)
        self.assertIn("見つかりません", status.message)

    def test_未設定なら自動同期オフ(self) -> None:
        sync = AutoSync(":memory:", "")
        self.assertFalse(sync.enabled)
        self.assertIs(sync.sync_once().state, SyncState.DISABLED)

    def _put_master_member(self) -> None:
        conn = sqlite3.connect(self.master_db)
        conn.execute(
            f'INSERT OR REPLACE INTO "{config.TABLE_MEMBER}" '
            '("管理番号","苗字","班","名前","読み","担当ライン") VALUES (?,?,?,?,?,?)',
            ("10", "山田", "B", "山田太郎", "ヤマダ", "L-1"))
        conn.commit()
        conn.close()

    def test_受信で班員名簿もマスタDBから入る(self) -> None:
        """**配ったばかりの端末が名簿0件で始まらない。**

        班員名簿はマスタDBにあり、保存用DBの取り込みでは入らない。
        以前は「取り込む」を押すまで0件で、休みの作業者を選べなかった。
        """
        self._put_master_member()
        sync = AutoSync(":memory:", str(self.data_db), master_path=str(self.master_db))
        self.assertTrue(sync.receive(self.conn))
        self.assertEqual(self.repo.member_count(), 1)

    def test_マスタDBが見えなくても受信は通る(self) -> None:
        """休み・連絡が届くことのほうが大事。見えないことは設定画面が言う。"""
        sync = AutoSync(":memory:", str(self.data_db),
                        master_path=str(self.folder / "無い.sqlite3"))
        self.assertTrue(sync.receive(self.conn))

    def test_同期サービスはマスタDBも渡す(self) -> None:
        service = sync_service.get_service()
        service.reload()
        self.assertEqual(service._auto.master_path, str(self.master_db))

    def test_送れなければ送信待ちに残す(self) -> None:
        """**入力が失われないこと。** 送れなかった分は次回やり直す。"""
        sync = AutoSync(":memory:", str(self.folder / "無い.sqlite3"))
        self.repo.save_record(D, config.KUBUN_REST, "山田太郎", "10", shift="1")

        status = sync.sync_once()
        self.assertIs(status.state, SyncState.OFFLINE)
        self.assertEqual(total_pending_count(self.conn), 1)
        self.assertEqual(len(self.repo.get_day_records(D)), 1)


class SourceLookupTests(_Base):
    """参照パスからの探索。"""

    def test_既定の名前で見つかる(self) -> None:
        self.assertEqual(sources.find_data_db(), self.data_db)
        self.assertEqual(sources.find_master_db(), self.master_db)

    def test_拡張子違いも同じものとして扱う(self) -> None:
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: None)
        _web.make_source(folder, "連絡帳.db", _web.SOURCE_SCHEMA)
        self.assertIsNotNone(sources.find_data_db(folder))

    def test_名前が違っても保存用として拾う(self) -> None:
        folder = Path(tempfile.mkdtemp())
        _web.make_source(folder, "連絡帳2026.sqlite3", _web.SOURCE_SCHEMA)
        found = sources.find_data_db(folder)
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "連絡帳2026.sqlite3")

    def test_マスタは名前が一致したときだけ(self) -> None:
        """保存用と同じフォルダにあるので、緩い一致だと取り違える。"""
        folder = Path(tempfile.mkdtemp())
        _web.make_source(folder, "連絡帳.sqlite3", _web.SOURCE_SCHEMA)
        self.assertIsNone(sources.find_master_db(folder))

    def test_マスタ未設定なら保存用と同じ場所を見る(self) -> None:
        user_settings.set_value(user_settings.KEY_MASTER_DB_DIR, "")
        self.assertEqual(sources.find_master_db(), self.master_db)


class ProbeTests(unittest.TestCase):
    """開いてみる ── **駄目な理由と次の一手まで返す。**"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)

    def test_開ける(self) -> None:
        path = _web.make_source(self.folder, "連絡帳.sqlite3", _web.SOURCE_SCHEMA)
        result = source_db.probe(path)
        self.assertTrue(result.ok)
        self.assertEqual(result.journal.lower(), "delete")
        self.assertIn(config.TABLE_DATA, result.tables)

    def test_sqlite3でなければ理由が出る(self) -> None:
        path = self.folder / "壊れ.sqlite3"
        path.write_bytes(b"not a database")
        result = source_db.probe(path)
        self.assertFalse(result.ok)
        self.assertFalse(result.is_sqlite)
        self.assertIn("変換ツール", result.hint())

    def test_無いファイル(self) -> None:
        result = source_db.probe(self.folder / "無い.sqlite3")
        self.assertFalse(result.ok)

    def test_WALは共有で開けないと伝える(self) -> None:
        """WAL は共有メモリを使うので、SMB 上では読むことすらできない。"""
        path = self.folder / "wal.sqlite3"
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("CREATE TABLE t (a)")
        conn.commit()
        conn.close()

        result = source_db.probe(path)
        # 手元では開ける(共有ではないので)が、WAL であることは伝わる
        if result.ok:
            self.assertEqual(result.journal.lower(), "wal")
            self.assertIn("WAL", result.describe())


class WriteGuardTests(unittest.TestCase):
    """取り込み元への書き込みの守り。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = _web.make_source(
            Path(self.tmp.name), "連絡帳.sqlite3", _web.SOURCE_SCHEMA)

    def test_読み取り専用では書けない(self) -> None:
        with source_db.connect(self.path, read_only=True) as source:
            with self.assertRaises(source_db.SourceError):
                source.insert(config.TABLE_DATA, {"登録内容": "x"})

    def test_WHEREの無いUPDATEを断る(self) -> None:
        """空の WHERE は「全行」。共有のデータなので取り違えは全員に効く。"""
        with self.assertRaises(source_db.SourceError):
            source_db.build_update(config.TABLE_DATA, {"登録内容": "x"}, {})

    def test_WHEREの無いDELETEを断る(self) -> None:
        with self.assertRaises(source_db.SourceError):
            source_db.build_delete(config.TABLE_DATA, {})

    def test_WALにしない(self) -> None:
        """WAL は SMB で開けなくなるので、書き込みで開くときに戻す。"""
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.close()

        with source_db.connect(self.path, read_only=False) as source:
            mode = source.query("PRAGMA journal_mode")[0]["journal_mode"]
        self.assertEqual(str(mode).lower(), "delete")

    def test_値はプレースホルダで渡す(self) -> None:
        """引用符を含む値でも壊れない (SQL を組み立てていないこと)。"""
        with source_db.connect(self.path, read_only=False) as source:
            source.insert(config.TABLE_DATA,
                          {"登録内容": "山田'太郎", "識別コード": "10"})
            rows = source.query(f'SELECT * FROM "{config.TABLE_DATA}"')
        self.assertEqual(rows[0]["登録内容"], "山田'太郎")


class ReplacedSourceTests(_Base):
    """取り込み元が差し替わっても送り続けられるか

    【なぜ要るか】
    控えから戻す・年度で入れ替える、で**同じパスのファイルが別物になる**。
    「送信ID列はある」を覚えたまま長時間動いている端末は、列の無い
    ファイルへ送り続けて毎行 no such column で落ちる ── しかも
    **プロセスを開けっぱなしにしているあいだ直らない**。
    §4.2 で「壊れたら控えから戻す」と勧めている手順そのものなので、
    ここで塞いでおく。
    """

    def replace_source(self) -> None:
        """同じ場所に、送信ID列の無い取り込み元を置き直す。"""
        self.data_db.unlink()
        _web.make_source(self.folder, config.SOURCE_FILE_DATA, _web.SOURCE_SCHEMA)

    def test_差し替えても送れる(self) -> None:
        self.repo.save_record(D, config.KUBUN_REST, "山田太郎", "10", shift="1")
        self.send()
        self.assertEqual(len(self.rows_in_source()), 1)

        # ここで控えから戻した = 送信ID列が無いファイルになる
        self.replace_source()
        self.repo.save_record(D2, config.KUBUN_REST, "佐藤次郎", "20", shift="2")
        self.send()

        rows = self.rows_in_source()
        self.assertEqual([r["登録内容"] for r in rows], ["佐藤次郎"])

    def test_送信ID列を作り直す(self) -> None:
        """二重適用の防ぎ方(送信IDの一意索引)も戻ること。"""
        self.repo.save_record(D, config.KUBUN_REST, "山田太郎", "10", shift="1")
        self.send()
        self.replace_source()
        self.repo.save_record(D2, config.KUBUN_REST, "佐藤次郎", "20", shift="2")
        self.send()

        with source_db.connect(self.data_db, read_only=True) as source:
            self.assertIn(outbox_sync.DEFAULT_OP_ID_COLUMN,
                          source.columns(config.TABLE_DATA))
            self.assertTrue(source.has_index(
                f"IX_{config.TABLE_DATA}_{outbox_sync.DEFAULT_OP_ID_COLUMN}"))

    def test_整っていれば作り直さない(self) -> None:
        """普段の道では DDL を投げない(共有への書き込みを増やさない)。"""
        self.repo.save_record(D, config.KUBUN_REST, "山田太郎", "10", shift="1")
        self.send()

        with source_db.connect(self.data_db, read_only=True) as source:
            spec = sync_specs.WRITE_BACK_SPECS[0]
            # 読み取り専用で開いた接続でも通る = 書いていない
            self.assertTrue(outbox_sync.ensure_op_id_column(source, spec))


class WriteBackSpecTests(unittest.TestCase):
    def test_送信用ビュー経由で送る(self) -> None:
        """SQLite だけが持つ access_id 列を取り込み元へ送らないため。"""
        for spec in sync_specs.WRITE_BACK_SPECS:
            self.assertTrue(spec.sqlite_table.endswith("_送信用"))
            self.assertFalse(spec.source_table.endswith("_送信用"))


if __name__ == "__main__":
    unittest.main()
