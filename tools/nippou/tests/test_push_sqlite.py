"""③反映の書き先が sqlite3 のとき (`access_bridge/sqlite_backend.py`)

【なぜこれが要るのか】
参照用マスタ4つは読むだけなので `source_db` で sqlite3 に移せました。
残っていたのが **`日報管理` への書き込み** ── このアプリ唯一の「外の
ファイルへ書く」処理です。ここが Access のままだと、Windows と ACE
ドライバが要り、`.laccdb` の排他に当たると反映できません。

【ここで見ること】
書けること。**上書きになること**(同じキーを何度反映しても増えない)。
1つのキーが失敗しても他のキーは反映されること。表が無ければ作ること。
そして、値を文字列に埋めずにプレースホルダで渡していること。
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.access_bridge import pusher, sqlite_backend
from nippou.db.models import DetailRecord, HeaderRecord


def _header(**over) -> HeaderRecord:
    base = dict(report_date="2026年1月5日", line="L-1", shift="1直", page=1,
                worker="山田", count="10", weight_kg="1000")
    base.update(over)
    return HeaderRecord(**base)


def _detail(**over) -> DetailRecord:
    base = dict(report_date="2026年1月5日", line="L-1", shift="1直", page=1,
                row_no=1, lot="A1", wei="500")
    base.update(over)
    return DetailRecord(**base)


class DispatchTests(unittest.TestCase):
    """振り分けは道の拡張子で決まる(`importer` と同じ考え方)。"""

    def test_sqlite3は書き込み先として扱う(self) -> None:
        for name in ("日報管理.sqlite3", "日報管理.db"):
            self.assertTrue(pusher.is_sqlite_target(Path("/x") / name), name)

    def test_accdbはAccessのまま(self) -> None:
        self.assertFalse(pusher.is_sqlite_target(Path("/x/日報管理.accdb")))


class TableTests(unittest.TestCase):
    """表の作りが VBA `NippouDB_EnsureTables` と揃っていること。

    揃っていないと、同じファイルを Access 版と行き来させたときに
    片方から読めなくなる。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.db = self.tmp / "日報管理.sqlite3"

    def test_無ければ作る(self) -> None:
        conn = sqlite3.connect(str(self.db))
        self.addCleanup(conn.close)
        sqlite_backend.ensure_tables(conn, "T_日報ヘッダー_L-1", "T_日報明細_L-1")
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(names, {"T_日報ヘッダー_L-1", "T_日報明細_L-1"})

    def test_2度呼んでも落ちない(self) -> None:
        conn = sqlite3.connect(str(self.db))
        self.addCleanup(conn.close)
        sqlite_backend.ensure_tables(conn, "T_日報ヘッダー_L-1", "T_日報明細_L-1")
        sqlite_backend.ensure_tables(conn, "T_日報ヘッダー_L-1", "T_日報明細_L-1")

    def test_ヘッダーの列はVBAのまま(self) -> None:
        self.assertEqual(sqlite_backend.HEADER_COLUMNS[:4],
                         ("報告日", "ライン", "直", "ページ"))
        self.assertIn("保存日時", sqlite_backend.HEADER_COLUMNS)
        self.assertEqual(len(sqlite_backend.HEADER_COLUMNS), 12)

    def test_明細の列はVBAのまま(self) -> None:
        cols = sqlite_backend.DETAIL_COLUMNS
        self.assertEqual(cols[:5], ("報告日", "ライン", "直", "ページ", "行番号"))
        self.assertEqual(cols[-1], "係数")
        # LOT〜UNI の24項目 + Others1-6 + 係数 + キー5
        self.assertEqual(len(cols), 36)

    def test_主キーはキー4つと行番号(self) -> None:
        self.assertEqual(sqlite_backend.HEADER_KEY, ("報告日", "ライン", "直", "ページ"))
        self.assertEqual(sqlite_backend.DETAIL_KEY,
                         ("報告日", "ライン", "直", "ページ", "行番号"))


class PushRecordsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.db = self.tmp / "日報管理.sqlite3"

    def _push(self, header, details):
        return sqlite_backend.push_records(
            self.db, header, details, "T_日報ヘッダー_L-1", "T_日報明細_L-1")

    def test_ファイルが無くても書ける(self) -> None:
        """**初回の反映でファイルごと作られる。** 事前準備が要らない。"""
        self.assertFalse(self.db.exists())
        result = self._push(_header(), [_detail()])
        self.assertTrue(result.success)
        self.assertTrue(self.db.exists())

    def test_ヘッダーと明細が入る(self) -> None:
        self._push(_header(), [_detail(row_no=1), _detail(row_no=2, lot="A2")])
        heads = sqlite_backend.read_table(self.db, "T_日報ヘッダー_L-1")
        rows = sqlite_backend.read_table(self.db, "T_日報明細_L-1")
        self.assertEqual(len(heads), 1)
        self.assertEqual(heads[0]["担当者"], "山田")
        self.assertEqual(sorted(r["LOT"] for r in rows), ["A1", "A2"])

    def test_同じキーは上書きになる(self) -> None:
        """DELETE+INSERT なので、何度反映しても増えない(冪等)。"""
        self._push(_header(), [_detail(), _detail(row_no=2)])
        self._push(_header(worker="直した"), [_detail(wei="999")])

        heads = sqlite_backend.read_table(self.db, "T_日報ヘッダー_L-1")
        rows = sqlite_backend.read_table(self.db, "T_日報明細_L-1")
        self.assertEqual(len(heads), 1)
        self.assertEqual(heads[0]["担当者"], "直した")
        # **前の明細も消えていること。** 消さないと2行目が残る
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["WEI"], "999")

    def test_別のキーは並んで入る(self) -> None:
        self._push(_header(page=1), [_detail(page=1)])
        self._push(_header(page=2), [_detail(page=2)])
        self.assertEqual(
            len(sqlite_backend.read_table(self.db, "T_日報ヘッダー_L-1")), 2)

    def test_引用符を含む値でも壊れない(self) -> None:
        """**プレースホルダで渡していること。**

        SQL文に埋めていると、担当者名に `'` が入っただけで壊れる。
        """
        result = self._push(_header(worker="山'田\"太--郎"),
                            [_detail(lot="A'1; DROP TABLE x--")])
        self.assertTrue(result.success, result.error)
        heads = sqlite_backend.read_table(self.db, "T_日報ヘッダー_L-1")
        self.assertEqual(heads[0]["担当者"], "山'田\"太--郎")
        rows = sqlite_backend.read_table(self.db, "T_日報明細_L-1")
        self.assertEqual(rows[0]["LOT"], "A'1; DROP TABLE x--")
        # 表が消えていないこと(埋め込みなら消えうる)
        self.assertTrue(sqlite_backend.table_exists(self.db, "T_日報明細_L-1"))

    def test_明細が無くてもヘッダーは入る(self) -> None:
        result = self._push(_header(), [])
        self.assertTrue(result.success)
        self.assertEqual(
            len(sqlite_backend.read_table(self.db, "T_日報ヘッダー_L-1")), 1)

    def test_途中で失敗したら何も残さない(self) -> None:
        """ヘッダーだけ入って明細が入っていない、を共有のDBに残さない。"""
        with patch.object(sqlite_backend, "_detail_values",
                          side_effect=sqlite3.Error("途中で失敗")):
            result = self._push(_header(), [_detail()])
        self.assertFalse(result.success)
        # 表は作られるが、行は1つも残らない
        self.assertEqual(
            sqlite_backend.read_table(self.db, "T_日報ヘッダー_L-1"), [])

    def test_開けなければ理由を返す(self) -> None:
        result = sqlite_backend.push_records(
            self.tmp / "無い" / "x.sqlite3", _header(), [],
            "T_日報ヘッダー_L-1", "T_日報明細_L-1")
        self.assertFalse(result.success)
        self.assertIsNotNone(result.error)


class PushPendingTests(unittest.TestCase):
    """`pusher.push_pending` から通したとき。"""

    def setUp(self) -> None:
        import os

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

        self._saved = {k: os.environ.get(k)
                       for k in ("NIPPOU_LOCAL_DIR", "NIPPOU_APP_DIR")}
        os.environ["NIPPOU_LOCAL_DIR"] = str(self.tmp / "local")
        os.environ["NIPPOU_APP_DIR"] = str(self.tmp / "app")
        self.addCleanup(self._restore_env)

        for name in [m for m in list(sys.modules) if m.startswith("nippou")]:
            del sys.modules[name]

        from nippou.config import SETTINGS
        from nippou.db.connection import connect
        from nippou.db.repository import NippouRepository

        self.conn = connect(SETTINGS.sqlite_path)
        self.addCleanup(self.conn.close)
        self.repo = NippouRepository(self.conn)
        self.target = self.tmp / "日報管理.sqlite3"

    def _restore_env(self) -> None:
        import os

        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _save(self, **over) -> None:
        self.repo.save(_header(**over),
                       [_detail(**{k: v for k, v in over.items()
                                   if k in ("report_date", "line", "shift", "page")})])

    def test_未反映のぶんが反映される(self) -> None:
        from nippou.access_bridge import pusher as p

        self._save()
        summary = p.push_pending(self.repo, self.target)
        self.assertEqual(len(summary.succeeded), 1)
        self.assertEqual(summary.failed, [])
        self.assertEqual(self.repo.pending_sync_headers(), [])

    def test_排他の警告を出さない(self) -> None:
        """`.laccdb` は Access のしくみ。sqlite3 には無い。

        毎回「誰かが使っています」が出ると、本当に困ったときに読まれない。
        """
        from nippou.access_bridge import pusher as p

        self._save()
        summary = p.push_pending(self.repo, self.target)
        self.assertEqual(summary.concurrency_warning, "")

    def test_ラインごとに表が分かれる(self) -> None:
        from nippou.access_bridge import pusher as p

        self._save(line="L-1")
        self._save(line="LVC")
        from nippou.access_bridge import sqlite_backend as fresh

        p.push_pending(self.repo, self.target)
        self.assertTrue(fresh.table_exists(self.target, "T_日報ヘッダー_L-1"))
        self.assertTrue(fresh.table_exists(self.target, "T_日報ヘッダー_LVC"))

    def test_1キーが失敗しても他は反映される(self) -> None:
        """1つの悪いキーが、残り全部を止めない(VBA と同じ per-key 原子性)。"""
        from nippou.access_bridge import pusher as p
        from nippou.access_bridge.runner import ScriptResult

        self._save(page=1)
        self._save(page=2)

        # **読み直したほうを差し替える。** setUp が `nippou*` を捨てて
        # いるので、このファイル冒頭で束ねた `sqlite_backend` は別物になる
        from nippou.access_bridge import sqlite_backend as fresh

        calls = []
        real = fresh.push_records_batch

        def flaky(db_path, items, saved_at=""):
            pages = [h.page for h, *_ in items]
            calls.append(pages)
            if 1 in pages:
                return ScriptResult(success=False, error=None)
            return real(db_path, items, saved_at)

        with patch.object(fresh, "push_records_batch", flaky):
            summary = p.push_pending(self.repo, self.target)

        # 束(1・2)で失敗 → 1ページずつ送り直して、悪いほうだけ残す
        self.assertEqual(calls, [[1, 2], [1], [2]])
        self.assertEqual(len(summary.succeeded), 1)
        self.assertEqual(len(summary.failed), 1)
        # 成功したほうだけ反映済みになる
        self.assertEqual([h.page for h in self.repo.pending_sync_headers()], [1])

    def test_束で送る_開くのは束に1回(self) -> None:
        """**1ページごとに共有を開いて確定していた**(何百ページで何十分)。"""
        from nippou.access_bridge import pusher as p
        from nippou.access_bridge import sqlite_backend as fresh

        for page in range(1, 31):
            self._save(page=page)
        opened = []
        real = fresh._connect

        def counting(path):
            opened.append(path)
            return real(path)

        with patch.object(fresh, "_connect", counting):
            summary = p.push_pending(self.repo, self.target)
        self.assertEqual(len(summary.succeeded), 30)
        self.assertEqual(self.repo.pending_sync_headers(), [])
        # 日報は 25 + 5 の2束。集計は直1つで1束
        self.assertLessEqual(len(opened), 3, opened)

    def test_中止すると束の切れ目で止まり残りは未送信のまま(self) -> None:
        from nippou import job_progress
        from nippou.access_bridge import pusher as p
        from nippou.access_bridge import sqlite_backend as fresh
        from nippou.logic import progress

        for page in range(1, 31):
            self._save(page=page)
        real = fresh.push_records_batch

        def stop_after_first(db_path, items, saved_at=""):
            result = real(db_path, items, saved_at)
            job_progress.request_stop()
            return result

        with job_progress.watching(job=progress.JOB_PUSH), \
                patch.object(fresh, "push_records_batch", stop_after_first):
            summary = p.push_pending(self.repo, self.target)
        self.assertEqual(len(summary.succeeded), p.BATCH_PAGES)
        self.assertEqual(summary.stopped, 30 - p.BATCH_PAGES)
        self.assertEqual(len(self.repo.pending_sync_headers()), 30 - p.BATCH_PAGES)
        # 次に押せば続きから
        summary = p.push_pending(self.repo, self.target)
        self.assertEqual(len(summary.succeeded), 30 - p.BATCH_PAGES)
        self.assertEqual(self.repo.pending_sync_headers(), [])

    def test_Accessの道ならAccess経路へ行く(self) -> None:
        from nippou.access_bridge import pusher as p

        from nippou.access_bridge import sqlite_backend as fresh

        self._save()
        with patch.object(fresh, "push_records") as fake:
            p.push_pending(self.repo, self.tmp / "日報管理.accdb")
        fake.assert_not_called()


if __name__ == "__main__":
    unittest.main()
