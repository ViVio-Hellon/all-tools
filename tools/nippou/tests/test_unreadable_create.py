"""共有を**確かめられない**のに「無い」とみなして、作る・書く・入れ替える・覚える、をしない(第2弾)

    共有ファイルが読み込めなかったことを「実績の表が無い」と誤認し、手元の実績を送り直していた
    (別のツールの事案)。「無い」とするのは、読めたうえで本当に無いと確かめたときだけ

「確かめた」= 置き場所(親のフォルダ)に届いて一覧が取れ、そこにファイルが無い
(`db.connection.file_state`)。届かない・権限・0バイト・見えているのに開けない、は「読めない」。
読めないときは作らない・書かない・覚えない。次の回にやり直し、理由を出す。

    1. 共有の日報データが無いとき、共有へ保存が新しく作っていた(`access_bridge/sqlite_backend`)
       → 置き場所に届いて本当に無いときだけ作る。届かない・確かめられない・0バイトなら作らず未送信のまま
    2. 標準作業時間.sqlite3 も同じ(`services/standard_time`)
    3. マスタ管理の書き込み・VC計算マスタ・設定画面の表の一覧が、開くだけで空のファイルを作っていた
       (`source_db.SourceConnection`・`vc/db`・`presenters/settings`)
    4. 控え(LocalBackup)を確かめられないのを「控えが無い」とみなし、空の控えを作る・
       起動のときに全ページを写す待ちに入れていた(`services/local_backup`)
    5. 日報データ.accdb から写す: 写す先を確かめられないのに、控えを取らずに新しい日報データを作って写した
       (`services/accdb_copy`)
    6. 停止内訳.csv を確かめられない・0バイトのとき、黙って伝送用ファイルの表に入れ替えていた
       (`access_bridge/stop_master`)。設定画面の「見本を出す」「決定」は、届かないのを「無い」とみなして
       手で直した CSV を上書き・前の CSV を残さずに置き換えていた(`app/routes/settings`)
    7. 用途名負荷係数算出を読めないとき、係数を全部1で書き直していた(`services/load_factor`)
    8. 後から作る直の確かめで、表の一覧を読めないのを「共有にページが無い」と答えていた(`services/backfill`)
    9. 月替わり: 共有の日報データを確かめられないのを「本当に無い」とみなしていた(`services/month_rollover`)
   10. アクセス権限: 先に見るファイルを読めないとき、後ろのファイルの行でラインを当て直し、
       読めないだけで前に読めた結果(Administrator など)を空で置き換えていた(`services/access_rights`)
   11. 直の時刻(時間用): 読み損ねたのを「読んだ」と覚え、マスタが次に変わるまで読み直さなかった
       (`services/shift_times`)
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import PropertyMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.connection import connect  # noqa: E402
from nippou.db.models import DetailRecord, HeaderRecord  # noqa: E402
from nippou.db.repository import NippouRepository  # noqa: E402
from nippou.db.schema import ensure_schema  # noqa: E402

DAY = "2026年10月1日"


@contextmanager
def listing_fails(folder: Path):
    """その置き場所だけ**一覧が取れない**(共有が一瞬届かない・権限)。フォルダの stat は通る。"""
    real = os.listdir
    target = os.path.normcase(os.path.abspath(folder))

    def fake(path="."):
        if os.path.normcase(os.path.abspath(path)) == target:
            raise PermissionError(13, "アクセスが拒否されました", str(path))
        return real(path)

    with patch("os.listdir", fake):
        yield


@contextmanager
def share_gone(folder: Path):
    """その置き場所の下が**全部見えない**(Windows で届かない共有は FileNotFoundError になる)。

    ファイルは本当はある。見えないだけ。
    """
    real_stat, real_listdir = os.stat, os.listdir
    root = os.path.normcase(os.path.abspath(folder))

    def under(path) -> bool:
        text = os.path.normcase(os.path.abspath(os.fspath(path)))
        return text == root or text.startswith(root + os.sep)

    def fake_stat(path, *args, **kwargs):
        if not isinstance(path, int) and under(path):
            raise FileNotFoundError(2, "ネットワーク パスが見つかりません", os.fspath(path))
        return real_stat(path, *args, **kwargs)

    def fake_listdir(path="."):
        if under(path):
            raise FileNotFoundError(2, "ネットワーク パスが見つかりません", os.fspath(path))
        return real_listdir(path)

    with patch("os.stat", fake_stat), patch("os.listdir", fake_listdir):
        yield


@contextmanager
def hidden(path: Path):
    """そのファイルだけ**一瞬見えない**(Windows で共有が一瞬切れると FileNotFoundError)。本当はある。"""
    real = os.stat
    target = os.path.normcase(os.path.abspath(path))

    def fake(name, *args, **kwargs):
        if not isinstance(name, int) and os.path.normcase(
                os.path.abspath(os.fspath(name))) == target:
            raise FileNotFoundError(2, "ネットワーク パスが見つかりません", os.fspath(name))
        return real(name, *args, **kwargs)

    with patch("os.stat", fake):
        yield


def make_sqlite(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("VACUUM")
    conn.close()
    return path


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.conn = connect(self.tmp / "local.sqlite3")
        ensure_schema(self.conn)
        self.addCleanup(self.conn.close)
        self.repo = NippouRepository(self.conn)
        self.share = self.tmp / "共有"
        self.share.mkdir()

    def save(self, *, line="L-1", shift="1直", page=1, usage="", day=DAY):
        key = dict(report_date=day, line=line, shift=shift, page=page)
        self.repo.save(HeaderRecord(**key, worker="山田"), [DetailRecord(
            **key, row_no=1, lot="N7131T0", ken="30", mai="10", tut="1",
            kz="07", kh="00", sz="15", sh="00", tim="420", wei="360", con="30",
            others1=usage)])
        return (day, line, shift, page)


# ======================================================================
# 1. 共有の日報データ: 作るのは、置き場所に届いて本当に無いときだけ
# ======================================================================
class SharedPushTests(_Case):
    def push(self, target: Path):
        from nippou.access_bridge import pusher

        with patch.object(pusher, "_push_summaries", return_value=0):
            return pusher.push_pending(self.repo, target)

    def test_置き場所の一覧が取れなければ共有の日報データを作らず未送信のまま(self) -> None:
        target = self.share / "日報データ.sqlite3"
        key = self.save()
        with listing_fails(self.share):
            summary = self.push(target)
        self.assertFalse(target.exists(), "確かめられないのに空の日報データを作った")
        self.assertEqual(summary.succeeded, [])
        self.assertEqual([h.key() for h in self.repo.pending_sync_headers()], [key])
        self.assertIn("共有フォルダを読めませんでした", summary.failed[0].error.message)

    def test_0バイトの共有の日報データには書かない(self) -> None:
        target = self.share / "日報データ.sqlite3"
        target.write_bytes(b"")                   # コピーの最中・ウイルス対策が掴んだ など
        key = self.save()
        summary = self.push(target)
        self.assertEqual(target.stat().st_size, 0, "0バイトのファイルに表を作って書いた")
        self.assertEqual([h.key() for h in self.repo.pending_sync_headers()], [key])
        self.assertIn("0バイト", summary.failed[0].error.message)

    def test_置き場所に届いて本当に無ければ今までどおり作って送る(self) -> None:
        target = self.share / "日報データ.sqlite3"
        self.save()
        summary = self.push(target)
        self.assertEqual(len(summary.succeeded), 1)
        self.assertTrue(target.stat().st_size > 0)
        self.assertEqual(self.repo.pending_sync_headers(), [])

    def test_置き場所のフォルダが無ければ作らない(self) -> None:
        target = self.share / "無いフォルダ" / "日報データ.sqlite3"
        self.save()
        summary = self.push(target)
        self.assertFalse(target.parent.exists())
        self.assertEqual(summary.succeeded, [])

    def test_共有保存の履歴は_確かめられなければまだ無いとは言わない(self) -> None:
        from nippou.services import push_history

        target = self.share / "日報データ.sqlite3"
        with listing_fails(self.share):
            readout = push_history.read(self.repo, target)
        self.assertIn("共有フォルダを読めませんでした", readout.warning)
        self.assertNotIn("まだ無い", readout.source)

    def test_読み返しの表があるかは_確かめられなければ例外(self) -> None:
        from nippou.access_bridge import sqlite_backend
        from nippou.db.connection import SharedUnreadable

        target = make_sqlite(self.share / "日報データ.sqlite3")
        with share_gone(self.share):
            with self.assertRaises(SharedUnreadable):
                sqlite_backend.table_exists(target, "T_日報ヘッダー_L-1")
        self.assertFalse(sqlite_backend.table_exists(self.share / "無い.sqlite3", "x"))


# ======================================================================
# 2. 標準作業時間.sqlite3
# ======================================================================
class StandardTimeCreateTests(_Case):
    def test_置き場所の一覧が取れなければ標準作業時間を作らない(self) -> None:
        from nippou.services import standard_time

        target = self.share / "標準作業時間.sqlite3"
        with listing_fails(self.share):
            with self.assertRaises(sqlite3.Error):
                standard_time._connect(target)
        self.assertFalse(target.exists())

    def test_本当に無ければ作る(self) -> None:
        from nippou.services import standard_time

        target = self.share / "標準作業時間.sqlite3"
        standard_time._connect(target).close()
        self.assertTrue(target.stat().st_size > 0)


# ======================================================================
# 3. 開くだけで空のファイルを作らない
# ======================================================================
class OpenDoesNotCreateTests(_Case):
    def test_マスタ管理の書き込みは無いマスタを作らない(self) -> None:
        from nippou import source_db

        target = self.share / "梱包資材マスタ.sqlite3"   # 一覧を出したあとに見えなくなった
        with self.assertRaises(source_db.SourceError):
            source_db.connect(target)
        self.assertFalse(target.exists(), "空のマスタを共有に作った")

    def test_VC計算マスタは_一覧が取れなければ初期値で作らない(self) -> None:
        from nippou.vc import db

        target = self.share / "VC計算マスタ.sqlite3"
        with listing_fails(self.share):
            with self.assertRaises(db.DbUnavailable):
                db.ensure_database(target)
        self.assertFalse(target.exists(), "確かめられないのに初期値のマスタを作った")
        self.assertEqual(db.ensure_database(target), "created")    # 本当に無ければ作る

    def test_VC計算マスタを読む_書く接続は無いファイルを作らない(self) -> None:
        from nippou.vc import db

        target = self.share / "VC計算マスタ.sqlite3"
        with self.assertRaises(sqlite3.Error):
            db._open(target)
        self.assertFalse(target.exists())

    def test_班員名簿の点検は_列を読めなければ指摘なしとは言わない(self) -> None:
        from nippou import master_admin, source_db

        target = make_sqlite(self.share / "梱包資材マスタ.sqlite3")
        with patch.object(source_db, "local_copy",
                          side_effect=source_db.SourceError("ロックされています")):
            found = master_admin._staff_checks(target, "班員名簿")
        self.assertTrue(found and "点検できませんでした" in found[0],
                        "列を読めないのを「オペレーターの列が無い = 指摘なし」にした")

    def test_設定画面の表の一覧は書き先を作らない(self) -> None:
        from nippou.presenters import settings

        target = self.share / "日報データ.sqlite3"
        with self.assertRaises(sqlite3.Error):
            settings._sqlite_tables(target)
        self.assertFalse(target.exists())


# ======================================================================
# 4. 控え(LocalBackup)
# ======================================================================
class BackupTests(_Case):
    def setUp(self) -> None:
        super().setUp()
        from nippou import config
        from nippou.services import local_backup as lb

        patcher = patch.object(type(config.SETTINGS), "local_backup_base",
                               new_callable=PropertyMock, return_value=self.share)
        patcher.start()
        self.addCleanup(patcher.stop)
        lb.reset()
        self.addCleanup(lb.reset)
        self.lb = lb
        self.folder = self.share / "LocalBackup" / "L-1"
        self.folder.mkdir(parents=True)

    def test_控えを確かめられなければ空の控えを作らず_待ちに残す(self) -> None:
        key = self.save()
        with listing_fails(self.folder):
            result = self.lb.flush(self.repo, force=True)
        self.assertFalse(self.lb.path_for("L-1").exists(), "確かめられないのに空の控えを作った")
        self.assertTrue(result.error)
        self.assertEqual(self.repo.backup_pending(), [key])
        result = self.lb.flush(self.repo, force=True)               # 届けば写る
        self.assertEqual((result.written, result.error), (1, ""))

    def test_起動のとき控えを確かめられなければ_全ページを写す待ちに入れない(self) -> None:
        key = self.save()
        stamps = dict(self.repo.backup_pending_stamped())
        self.repo.clear_backup_pending([key], stamps)
        with listing_fails(self.folder):
            result = self.lb.pull(self.repo, "L-1")
        self.assertIn("読めませんでした", result.error)
        self.assertEqual(self.repo.backup_pending(), [],
                         "控えを読めないのを「控えに無い」とみなして写す待ちに入れた")

    def test_記録を見るは_控えを確かめられなければ無いとは言わない(self) -> None:
        with listing_fails(self.folder):
            with self.assertRaises(sqlite3.Error):
                with self.lb.reader("L-1"):
                    pass
        with self.lb.reader("L-1") as backup:      # 本当に無い
            self.assertIsNone(backup)

    def test_ラインのフォルダが本当に無ければ作って写す(self) -> None:
        self.folder.rmdir()
        self.save()
        result = self.lb.flush(self.repo, force=True)
        self.assertEqual((result.written, result.error), (1, ""))
        self.assertTrue(self.lb.path_for("L-1").is_file())


# ======================================================================
# 6. 停止内訳.csv
# ======================================================================
def make_transmission(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    for no, rows in ((1, [("休憩食事", "0"), ("人員不足", "2")]),
                     (2, [("突発停止(機械)", "イ")]),
                     (3, [("段取り変更", "A")])):
        conn.execute(f'CREATE TABLE "作業停止時間内訳_{no}" ("内訳" TEXT, "内訳番号" TEXT)')
        conn.executemany(f'INSERT INTO "作業停止時間内訳_{no}" VALUES (?,?)', rows)
    conn.commit()
    conn.close()
    return path


class StopCsvTests(_Case):
    def setUp(self) -> None:
        super().setUp()
        from nippou import source_db
        from nippou.access_bridge import stop_master

        source_db.forget()
        stop_master.forget_last_read()
        self.addCleanup(stop_master.forget_last_read)
        self.ref = self.share / "ref"
        self.master = make_transmission(self.ref / "伝送用ファイル.sqlite3")
        self.csv = self.ref / "停止内訳.csv"
        self.csv.write_text("1,0,休憩(CSVで直した名前)\n1,9,CSVで足した理由\n",
                            encoding="utf-8-sig")

    def load(self):
        from nippou.access_bridge import stop_master

        return stop_master.load_sources(self.master, csv_path=self.csv)

    def test_CSVの置き場所が見えなければ表の一覧に入れ替えない(self) -> None:
        from nippou import source_db

        self.assertEqual([r.code for r in self.load().effective["設備停止"]], ["0", "9"])
        source_db.forget()
        # 表(伝送用ファイル)は手元の写しから読めるが、CSV は見えない
        source_db.local_copy(self.master)
        with hidden(self.csv):
            src = self.load()
        self.assertTrue(src.csv_error)
        self.assertEqual([r.code for r in src.effective["設備停止"]], ["0", "9"],
                         "CSVを読めないのに、表の一覧に入れ替えた/画面から消した"
                         "(最後に読めたCSVの一覧のまま使う)")
        self.assertEqual(src.effective["設備停止"][0].label, "休憩(CSVで直した名前)")
        from nippou.services.month_rollover import read_stop_labels

        with hidden(self.csv), \
                patch("nippou.config.Settings.stop_reason_csv_path",
                      new_callable=PropertyMock, return_value=self.csv), \
                patch("nippou.config.Settings.stop_reason_master_path",
                      new_callable=PropertyMock, return_value=self.master):
            _labels, problem = read_stop_labels()
        self.assertIn("停止内訳CSV", problem)        # 集計は前の名前のまま作る(`summary`)

    def test_一度も読めていなければ_表に入れ替えず空(self) -> None:
        from nippou import source_db

        source_db.local_copy(self.master)
        with hidden(self.csv):
            src = self.load()
        self.assertTrue(src.csv_error)
        self.assertEqual(src.effective["設備停止"], [])

    def test_0バイトのCSVは空の一覧として使わない(self) -> None:
        self.csv.write_bytes(b"")
        src = self.load()
        self.assertIn("0バイト", src.csv_error)
        self.assertEqual(src.effective["設備停止"], [])

    def test_CSVが本当に無ければ今までどおり表(self) -> None:
        self.csv.unlink()
        src = self.load()
        self.assertEqual(src.csv_error, "")
        self.assertEqual([r.code for r in src.effective["設備停止"]], ["0", "2"])


# ======================================================================
# 7. 用途名負荷係数算出
# ======================================================================
class LoadFactorTests(_Case):
    LINE = "機側"

    def setUp(self) -> None:
        super().setUp()
        from nippou import source_db

        source_db.forget()
        self.master = self.share / "伝送用ファイル.sqlite3"
        conn = sqlite3.connect(str(self.master))
        conn.execute('CREATE TABLE "用途名負荷係数算出" ("用途コード" TEXT,'
                     ' "PENAを1とした場合の換算係数" TEXT)')
        conn.execute('INSERT INTO "用途名負荷係数算出" VALUES ("H283", "1.4")')
        conn.commit()
        conn.close()
        patcher = patch("nippou.config.Settings.transmission_master_path",
                        new_callable=PropertyMock, return_value=self.master)
        patcher.start()
        self.addCleanup(patcher.stop)

    def keisu(self) -> list[str]:
        return [d.keisu for d in self.repo.load(DAY, self.LINE, "1直", 1)[1]]

    def test_係数の表を読めなければ全部1で書き直さない(self) -> None:
        from nippou import source_db
        from nippou.services import load_factor

        self.save(line=self.LINE, usage="H283")
        load_factor.recalculate(self.repo, DAY, self.LINE, "1直")
        self.assertEqual(self.keisu(), ["1.4"])
        self.repo.mark_synced((DAY, self.LINE, "1直", 1))
        with patch.object(source_db, "local_copy",
                          side_effect=source_db.SourceError("ネットワーク パスが見つかりません")):
            load_factor.recalculate(self.repo, DAY, self.LINE, "1直")
        self.assertEqual(self.keisu(), ["1.4"], "読めないだけで係数を1に書き直した")
        self.assertEqual(self.repo.pending_sync_headers(), [],
                         "書き直して未送信に戻り、係数1で共有へ送り直す")

    def test_表のファイルが本当に無ければ今までどおり1(self) -> None:
        from nippou.services import load_factor

        self.master.unlink()
        self.save(line=self.LINE, usage="H283")
        load_factor.recalculate(self.repo, DAY, self.LINE, "1直")
        self.assertEqual(self.keisu(), ["1.0"])


# ======================================================================
# 8. 後から作る直の確かめ
# ======================================================================
class BackfillTests(_Case):
    def test_表の一覧を読めなければ_共有にページが無いとは答えない(self) -> None:
        from nippou import source_db
        from nippou.services import backfill

        target = make_sqlite(self.share / "日報データ.sqlite3")
        with patch("nippou.config.Settings.access_db_path",
                   new_callable=PropertyMock, return_value=target), \
                patch.object(source_db, "local_copy",
                             side_effect=source_db.SourceError("ロックされています")):
            found = backfill.shared_pages(DAY, "L-1", "1直")
        self.assertIsNone(found, "読めないのに「共有にページが無い」と答えた")

    def test_置き場所に届いてファイルが無ければ_まだ送っていない(self) -> None:
        from nippou.services import backfill

        with patch("nippou.config.Settings.access_db_path", new_callable=PropertyMock,
                   return_value=self.share / "日報データ.sqlite3"):
            self.assertEqual(backfill.shared_pages(DAY, "L-1", "1直"), [])


# ======================================================================
# 9. 月替わり
# ======================================================================
class MonthTests(_Case):
    def test_共有の日報データを確かめられなければ本当に無いとはみなさない(self) -> None:
        from nippou.services import month_rollover

        target = self.share / "日報データ.sqlite3"      # 一覧が取れないので、あるか分からない
        with patch("nippou.config.Settings.access_db_path",
                   new_callable=PropertyMock, return_value=target), \
                patch.object(month_rollover, "_shared_place_set", return_value=True), \
                listing_fails(self.share):
            with self.assertRaises(month_rollover._SharedUnread):
                month_rollover._shared_month_pages("L-1", 2026, 10)


# ======================================================================
# 11. 直の時刻(時間用): 読めなかったのを「読んだ」と覚えない
# ======================================================================
class ShiftTimesTests(_Case):
    def test_読み損ねたマスタは_次に変わるのを待たずに読み直す(self) -> None:
        from nippou.access_bridge import importer
        from nippou.services import shift_times

        master = self.share / "伝送用ファイル.sqlite3"
        conn = sqlite3.connect(str(master))
        with conn:
            conn.execute('CREATE TABLE "時間用" ("番号", "直", "開始", "終了")')
            conn.executemany('INSERT INTO "時間用" VALUES (?,?,?,?)',
                             [(1, "1", "07:00", "15:00"), (2, "2", "15:00", "22:50")])
        conn.close()
        shift_times.reset()
        self.addCleanup(shift_times.reset)
        with patch("nippou.config.Settings.transmission_master_path",
                   new_callable=PropertyMock, return_value=master), \
                patch.object(shift_times, "RETRY_FAILED_SEC", 0.0, create=True):
            shift_times.refresh_if_changed(self.repo, now=0.0)
            self.assertEqual(self.repo.get_shift_times()["1"], ("07:00", "15:00"))
            conn = sqlite3.connect(str(master))         # ほかのPCが1直を延ばした
            with conn:
                conn.execute('UPDATE "時間用" SET "終了" = "15:30" WHERE "直" = "1"')
            conn.close()
            os.utime(master, ns=(10**18, 10**18))
            with patch.object(importer, "import_shift_times", return_value=None):  # ロック中
                self.assertIsNone(shift_times.refresh_if_changed(self.repo, now=100.0))
            self.assertEqual(self.repo.get_shift_times()["1"], ("07:00", "15:00"))
            shift_times.refresh_if_changed(self.repo, now=200.0)
        self.assertEqual(self.repo.get_shift_times()["1"], ("07:00", "15:30"),
                         "読めなかったのを「読んだ」と覚え、マスタが次に変わるまで読み直さない")


# ======================================================================
# 5・6(画面)・10 は画面ごと
# ======================================================================
from tests._web import HAS_FLASK, HEADERS, SKIP_REASON, WebTestCase  # noqa: E402


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class StopCsvScreenTests(WebTestCase):
    def setUp(self) -> None:
        super().setUp()
        make_transmission(self.tmp / "ref" / "伝送用ファイル.sqlite3")
        self.csv = self.tmp / "ref" / "停止内訳.csv"
        self.csv.write_text("1,0,手で直した\n", encoding="utf-8-sig")

    def test_見本は_いまのCSVが見えなければ上書きしない(self) -> None:
        with hidden(self.csv):
            res = self.post("/api/settings/stop-reasons/template")
        self.assertNotEqual(res.status_code, 200)
        self.assertIn("手で直した", self.csv.read_text(encoding="utf-8-sig"),
                      "見えなかっただけで、手で直したCSVを見本で上書きした")

    def test_決定は_いまのCSVが見えなければ書き換えない(self) -> None:
        import io

        data = {"file": (io.BytesIO("1,0,新しい\n".encode("utf-8-sig")), "停止内訳.見本.csv"),
                "apply": "1"}
        with hidden(self.csv):
            res = self.client.post("/api/settings/stop-reasons/upload", data=data,
                                   headers=HEADERS, content_type="multipart/form-data")
        self.assertNotEqual(res.status_code, 200)
        self.assertIn("手で直した", self.csv.read_text(encoding="utf-8-sig"),
                      "前のCSVを残さずに置き換えた")


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class AccdbCopyUnreadableTests(WebTestCase):
    def test_写す先を確かめられなければ_新しい日報データを作って写さない(self) -> None:
        from tests import _accdb

        from nippou import config, user_settings

        share = self.tmp / "share"
        share.mkdir()
        user_settings.save_many({config.KEY_ACCESS_DIR: str(share)})
        from nippou.config import SETTINGS
        from nippou.services import accdb_copy

        dest = SETTINGS.access_db_path
        _accdb.write(share / "日報データ.accdb",
                     _accdb.nippou_tables("L-1", [_accdb.nippou_page("L-1")]))
        accdb_copy.forget()
        with listing_fails(share):
            report = accdb_copy.run()
        self.assertFalse(dest.exists(), "確かめられないのに新しい日報データを作って写した")
        self.assertIn("共有フォルダを読めませんでした", report.error)


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class AccessRightsUnreadableTests(WebTestCase):
    terminal_line = None

    def setUp(self) -> None:
        super().setUp()
        from tests.test_access_rights import PCS, on

        from nippou.services import access_rights

        self.svc = access_rights
        access_rights.reset()
        self.addCleanup(access_rights.reset)
        patcher = patch.object(access_rights, "identity",
                               side_effect=lambda: on(PCS["L-1"]))
        patcher.start()
        self.addCleanup(patcher.stop)

    def master(self, file: str, *rights: str) -> Path:
        from nippou import source_db
        from nippou.config import SETTINGS
        from tests.test_access_rights import FLOOR, PCS

        path = (SETTINGS.gw_material_master_path if file == "material"
                else SETTINGS.transmission_master_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.execute('CREATE TABLE "アクセス権限" (管理番号 INTEGER, ログインID TEXT, PC名 TEXT,'
                     ' 権限 TEXT, 有効 INTEGER, 備考 TEXT)')
        conn.executemany('INSERT INTO "アクセス権限" VALUES (?,?,?,?,?,?)',
                         [(n, FLOOR, PCS["L-1"], value, 1, "")
                          for n, value in enumerate(rights, 1)])
        conn.commit()
        conn.close()
        source_db.forget(path)
        return path

    def unreadable(self, path: Path):
        from nippou import source_db

        real = source_db.local_copy

        def fake(target):
            if Path(target) == path:
                raise source_db.SourceError("ネットワーク パスが見つかりません")
            return real(target)

        return patch.object(source_db, "local_copy", side_effect=fake)

    def test_先に見るファイルを読めなければ_後ろのファイルの行でラインを当て直さない(self) -> None:
        from nippou import work_context

        first = self.master("transmission", "HVC")             # 伝送用ファイル(先に見る)
        self.master("material", "L-1")
        self.svc.apply()
        self.assertEqual(work_context.get_context().terminal_line, "HVC")

        with self.unreadable(first):
            status = self.svc.apply()
        self.assertEqual(work_context.get_context().terminal_line, "HVC",
                         "読めないだけで後ろのファイルのラインに当て直した")
        self.assertIn("読めませんでした", status.summary())

    def test_読めないときは前に読めた結果のまま(self) -> None:
        material = self.master("material", "Administrator")
        self.svc.load()
        self.assertTrue(self.svc.is_administrator())
        with self.unreadable(material):
            self.svc.load()
        self.assertTrue(self.svc.is_administrator(),
                        "読めないだけで Administrator の結果を空で置き換えた")


if __name__ == "__main__":
    unittest.main()
