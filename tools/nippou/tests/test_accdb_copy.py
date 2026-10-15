"""日報データ.accdb から写す・共有の表の「頁」「ページ」を両方読む (v4.36.0)

    「日報データ.accdb から写す」ボタンを作ってよいか → お願いします
    共有の表の列名「頁」と「ページ」のずれを直してよいか → 直してください
    VBA の日報をまだしばらく使い続けるか → 使います
    .accdb から写す はほかのツールでもある機能ですが、数値の丸め方が間違っていたり、
    重複が発生したり不具合が多数出ました 注意してください

【約束】
    ・共有の表は「頁」でも「ページ」でも読む。書くときはその表にある列の名前で書く。
      新しく作る表は VBA と同じ「頁」。両方ある表は両方に同じ数を書く
    ・書き先が Access のときは VBA の表に列を足さない(ALTER TABLE を出さない)。
      ④⑤の列が無ければ④⑤は書かず「④⑤は共有へ出ていません」と知らせる
    ・写す: 下見(書かない)→ 写す の2段。管理者だけ
    ・値は打ったままの字(先頭の0・小数・全角・空白・改行もそのまま)。ページ・行番号は整数、
      保存日時は秒まで(一番近い秒。ISO の形)
    ・重なりを作らない: 写し直しは鍵で置き換え。Access 側の同じページは、同じなら1つ・
      違えば保存日時の新しいほう・決められなければ写さずに出す。報告日の書き方の揺れも1つ
    ・何度押しても同じ(2回目・3回目で行数も中身も変わらない)
    ・保存日時の新しいほうを残す。このツールで共有へ保存したページを古い Access で上書きしない。
      Access から消えたページは消さない
    ・知らないライン(予備PC)は写さずに出す。表の定義の行数と読めた行数が違う表は写さない
    ・読めない形(古い .mdb・暗号化・壊れ・Access でない)は理由を言って断る
    ・ほかの PC が書いている最中は待ってから断る(何も書かない)。書く前に控えを取る
    ・写したあと、書いた中身を読み直して Access と1文字ずつ比べる(合わなければ戻す)

**試験の .accdb は `tests/_accdb.py` で作る架空のもの**です(本番の 日報データ.accdb は使わない)。
"""
from __future__ import annotations

import datetime as dt
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _accdb  # noqa: E402
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402

DAY = "2026年7月22日"
T1 = dt.datetime(2026, 7, 22, 15, 30, 0)
T2 = dt.datetime(2026, 7, 22, 18, 0, 0)


def rows(path: Path, sql: str, *params) -> list[tuple]:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def columns(path: Path, table: str) -> list[str]:
    return [r[1] for r in rows(path, f'PRAGMA table_info("{table}")')]


def old_table(path: Path, table: str, cols: tuple[str, ...], key: tuple[str, ...]) -> None:
    """v4.35.3 までのこのツールが作った形(ページの列が「ページ」)の表を作る。"""
    from nippou.access_bridge import sqlite_backend as sb

    conn = sqlite3.connect(str(path))
    with conn:
        conn.execute(sb._create_sql(table, cols, key))
    conn.close()


def v435(cols: tuple[str, ...]) -> tuple[str, ...]:
    return tuple("ページ" if c == "頁" else c for c in cols)


# ======================================================================
# 1. 共有の表の「頁」「ページ」
# ======================================================================
class SharedPageColumnTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "日報データ.sqlite3"

    def push(self, page: int = 1, lot: str = "H6081A1", saved: str = "", line: str = "L-1"):
        from nippou.access_bridge import pusher, sqlite_backend as sb
        from nippou.db.models import DetailRecord, HeaderRecord

        key = dict(report_date=DAY, line=line, shift="1直", page=page)
        result = sb.push_records(self.db, HeaderRecord(**key, worker="ツール"),
                                 [DetailRecord(**key, row_no=1, lot=lot, mai="08")],
                                 pusher.header_table_name(line), pusher.detail_table_name(line),
                                 saved_at=saved)
        self.assertTrue(result.success, result.error)

    def test_新しく作る表はVBAと同じ頁(self) -> None:
        self.push()
        self.assertEqual(columns(self.db, "T_日報ヘッダー_L-1")[3], "頁")
        self.assertIn("頁", columns(self.db, "T_日報明細_L-1"))
        self.assertNotIn("ページ", columns(self.db, "T_日報明細_L-1"))
        self.assertEqual(rows(self.db, 'SELECT "頁", typeof("頁"), "MAI" FROM "T_日報明細_L-1"'),
                         [(1, "integer", "08")])

    def test_ページの表にはページで書き_頁を足さない(self) -> None:
        from nippou.access_bridge import sqlite_backend as sb

        old_table(self.db, "T_日報ヘッダー_L-1", v435(sb.HEADER_COLUMNS), v435(sb.HEADER_KEY))
        old_table(self.db, "T_日報明細_L-1", v435(sb.DETAIL_COLUMNS), v435(sb.DETAIL_KEY))
        self.push()
        self.push()                                    # 同じページを保存し直しても1つ
        self.assertNotIn("頁", columns(self.db, "T_日報明細_L-1"))
        self.assertEqual(rows(self.db, 'SELECT "ページ", "担当者" FROM "T_日報ヘッダー_L-1"'),
                         [(1, "ツール")])
        self.assertEqual(rows(self.db, 'SELECT COUNT(*) FROM "T_日報明細_L-1"'), [(1,)])

    def test_両方ある表は両方に書き_中身のあるほうで読む(self) -> None:
        """v4.35.3 までの `add_missing_columns` が「頁」の明細表に「ページ」を足してしまった形。"""
        from nippou.access_bridge import sqlite_backend as sb
        from nippou.services import shared_reports

        old_table(self.db, "T_日報ヘッダー_L-1", sb.HEADER_COLUMNS, sb.HEADER_KEY)
        old_table(self.db, "T_日報明細_L-1", sb.DETAIL_COLUMNS + ("ページ",), sb.DETAIL_KEY)
        conn = sqlite3.connect(self.db)
        with conn:   # 前に「ページ」だけに書かれた行(頁は空)
            conn.execute('INSERT INTO "T_日報明細_L-1" ("報告日","ライン","直","頁","ページ",'
                         '"行番号","LOT") VALUES (?,?,?,?,?,?,?)',
                         (DAY, "L-1", "2直", 0, 3, 1, "R5083Z1"))
            conn.execute('UPDATE "T_日報明細_L-1" SET "頁"=NULL')
        conn.close()
        self.push(page=2)
        self.assertEqual(rows(self.db, 'SELECT "頁", "ページ" FROM "T_日報明細_L-1" WHERE "直"=?',
                              "1直"), [(2, 2)])
        page = sb.page_column(sqlite3.connect(self.db), "T_日報明細_L-1")
        self.assertEqual(page.names, ("頁", "ページ"))
        self.assertEqual(page.main, "頁")
        found = shared_reports.lot_rows(["L-1"], dt.date(2026, 7, 1), dt.date(2026, 7, 31),
                                        lambda lot: True, path=self.db)
        self.assertEqual(sorted(d.page for _h, d in found.rows), [2])   # 見出しがあるのは2ページだけ

    def _shared(self, page_column: str) -> None:
        from nippou.access_bridge import sqlite_backend as sb

        head, detail = sb.HEADER_COLUMNS, sb.DETAIL_COLUMNS
        if page_column == "ページ":
            old_table(self.db, "T_日報ヘッダー_HVC", v435(head), v435(sb.HEADER_KEY))
            old_table(self.db, "T_日報明細_HVC", v435(detail), v435(sb.DETAIL_KEY))
        self.push(page=1, lot="H6081A1", line="HVC")
        self.push(page=2, lot="H6081B2", line="HVC")

    def test_頁の表でもページの表でもロットを探す_紙と下見(self) -> None:
        from nippou.services import shared_reports

        for column in ("頁", "ページ"):
            with self.subTest(column=column):
                if self.db.exists():
                    self.db.unlink()
                self._shared(column)
                self.assertIn(column, columns(self.db, "T_日報ヘッダー_HVC"))
                found = shared_reports.lot_rows(
                    ["HVC"], dt.date(2026, 7, 1), dt.date(2026, 7, 31),
                    lambda lot: lot.startswith("H6081"), path=self.db)
                self.assertEqual(found.status, "ok")
                self.assertEqual(sorted((h.page, d.lot) for h, d in found.rows),
                                 [(1, "H6081A1"), (2, "H6081B2")])
                pages = shared_reports.shift_pages(DAY, "HVC", "1直", path=self.db)
                self.assertEqual([h.page for h, _ in pages.pages], [1, 2])
                self.assertEqual(pages.pages[0][1][0].mai, "08")


class AccessTargetTests(unittest.TestCase):
    """書き先が .accdb のとき(偽の台本で、送った SQL を見る)。"""

    def setUp(self) -> None:
        from nippou.access_bridge import pusher
        from nippou.db.connection import connect
        from nippou.db.models import DetailRecord, HeaderRecord
        from nippou.db.repository import NippouRepository

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.conn = connect(self.tmp / "手元.sqlite3")
        self.addCleanup(self.conn.close)
        self.repo = NippouRepository(self.conn)
        key = dict(report_date=DAY, line="L-1", shift="1直", page=1)
        self.repo.save(HeaderRecord(**key, worker="青木"),
                       [DetailRecord(**key, row_no=1, lot="H6081A1", s4="5", th4="10")])
        self.pusher = pusher
        self.scripts: list[str] = []

    def runner(self):
        from nippou.access_bridge.runner import ScriptResult

        scripts = self.scripts

        class Fake:
            def run(self, script_text: str) -> ScriptResult:
                scripts.append(script_text)
                return ScriptResult(success=True)

        return Fake()

    def target(self, **kwargs) -> Path:
        page = _accdb.nippou_page("L-1")
        return _accdb.write(self.tmp / "日報データ.accdb",
                            _accdb.nippou_tables("L-1", [page], **kwargs))

    def test_VBAの表には頁で書き_列を足さず_45は出ていないと知らせる(self) -> None:
        summary = self.pusher.push_pending(self.repo, self.target(), runner=self.runner())
        self.assertEqual(len(summary.succeeded), 1)
        text = "\n".join(self.scripts)
        self.assertNotIn("ALTER TABLE", text)
        self.assertIn("[頁]=1", text)
        self.assertNotIn("[ページ]", text)
        self.assertNotIn("[S4]", text)
        self.assertEqual(len(summary.notices), 1)
        self.assertIn("Access の表に作業停止④⑤の列が無いので、④⑤は共有へ出ていません",
                      summary.notices[0])
        self.assertIn("手元には残っています", summary.notices[0])
        # 手元には残っている
        _h, details = self.repo.load(DAY, "L-1", "1直", 1)
        self.assertEqual((details[0].s4, details[0].th4), ("5", "10"))

    def test_45の列がある表には書き_ページの表にはページで書く(self) -> None:
        summary = self.pusher.push_pending(
            self.repo, self.target(page_column="ページ", stops45=True), runner=self.runner())
        text = "\n".join(self.scripts)
        self.assertIn("[S4],[TH4],[S5],[TH5]", text)
        self.assertIn("[ページ]=1", text)
        self.assertNotIn("[頁]", text)
        self.assertNotIn("ALTER TABLE", text)
        self.assertEqual(summary.notices, [])

    def test_表を読めなければVBAの形とみなす(self) -> None:
        summary = self.pusher.push_pending(self.repo, self.tmp / "無い.accdb",
                                           runner=self.runner())
        text = "\n".join(self.scripts)
        self.assertIn("[頁]", text)
        self.assertNotIn("[S4]", text)
        self.assertNotIn("ALTER TABLE", text)
        self.assertEqual(len(summary.notices), 1)


# ======================================================================
# 2. 写す
# ======================================================================
class FormatTests(unittest.TestCase):
    """読めない形は理由を言って断る(`check_format`)。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        from nippou.services import accdb_copy

        self.svc = accdb_copy

    def make(self, name: str = "日報データ.accdb", **kwargs) -> Path:
        tables = _accdb.nippou_tables("L-1", [_accdb.nippou_page("L-1")])
        return _accdb.write(self.tmp / name, tables, **kwargs)

    def test_読める形(self) -> None:
        self.assertEqual(self.svc.check_format(self.make()), "")

    def test_読めない形(self) -> None:
        good = self.make().read_bytes()
        broken = bytearray(good)
        broken[2 * 4096] = 0x01                           # 表の一覧のページが壊れた
        cases = {
            "古い形": self.make("古い.mdb", version=_accdb.VERSION_JET3),
            "暗号化": self.make("鍵.accdb", db_key=0x12345678),
            "表の一覧のページが読めません": self.tmp / "壊れ.accdb",
            "空です": self.tmp / "空.accdb",
            "sqlite3 のファイル": self.tmp / "名前だけ.accdb",
            "途中で切れて": self.tmp / "途中.accdb",
            "Excel など": self.tmp / "表計算.accdb",
            "Access のファイルではない": self.tmp / "文字.accdb",
        }
        (self.tmp / "壊れ.accdb").write_bytes(bytes(broken))
        (self.tmp / "空.accdb").write_bytes(b"")
        conn = sqlite3.connect(self.tmp / "名前だけ.accdb")
        conn.execute("CREATE TABLE t (a)")
        conn.commit()
        conn.close()
        (self.tmp / "途中.accdb").write_bytes(good[:4096 * 2])
        (self.tmp / "表計算.accdb").write_bytes(b"PK\x03\x04" + b"\x00" * 100)
        (self.tmp / "文字.accdb").write_text("日報データ", encoding="utf-8")
        for word, path in cases.items():
            with self.subTest(word=word):
                why = self.svc.check_format(path)
                self.assertIn(word, why)
                with self.assertRaises(self.svc.SourceError):
                    self.svc.read_source(path)

    @unittest.skipUnless(shutil.which("mdb-export"), "mdbtools が入っていない")
    def test_試験の_accdbはmdbtoolsでも同じ中身に読める(self) -> None:
        """試験のファイルが、読み取りの部品だけに合わせた形ではないことの確かめ。"""
        import csv
        import io

        page = _accdb.nippou_page("L-1", lots=("H6081A1", "R5083 "), extra={"MAI": "０８"})
        path = _accdb.write(self.tmp / "日報データ.accdb",
                            _accdb.nippou_tables("L-1", [page]))
        out = subprocess.run(["mdb-export", str(path), "T_日報明細_L-1"], capture_output=True,
                             env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}, check=True)
        got = list(csv.DictReader(io.StringIO(out.stdout.decode("utf-8"), newline="")))
        self.assertEqual([(r["頁"], r["行番号"], r["LOT"], r["MAI"]) for r in got],
                         [("1", "1", "H6081A1", "０８"), ("1", "2", "R5083 ", "０８")])
        tables = self.svc.read_source(path)
        self.assertEqual([r["LOT"] for r in tables["T_日報明細_L-1"].rows], ["H6081A1", "R5083 "])


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class CopyTests(WebTestCase):
    """共有の日報データ(写す先)と 日報データ.accdb(写す元)を一時フォルダに置いて試す。"""

    def setUp(self) -> None:
        super().setUp()
        from nippou import config, user_settings

        self.share = self.tmp / "share"
        self.share.mkdir()
        user_settings.save_many({config.KEY_ACCESS_DIR: str(self.share)})
        from nippou.config import SETTINGS
        from nippou.logic import accdb_copy as rule
        from nippou.services import accdb_copy

        self.dest = SETTINGS.access_db_path
        self.source = self.share / "日報データ.accdb"
        self.svc = accdb_copy
        self.rule = rule
        accdb_copy.forget()

    # -- 写す元を作る -----------------------------------------------------
    def access(self, *tables: _accdb.Table) -> Path:
        self.svc.forget()
        return _accdb.write(self.source, list(tables))

    def vba(self, line: str = "L-1", pages=None, **kwargs) -> list[_accdb.Table]:
        pages = pages if pages is not None else [_accdb.nippou_page(line)]
        return _accdb.nippou_tables(line, pages, **kwargs)

    def dest_rows(self, table: str, order: str = "1") -> list[tuple]:
        return rows(self.dest, f'SELECT * FROM "{table}" ORDER BY {order}')

    def count(self, table: str) -> int:
        return rows(self.dest, f'SELECT COUNT(*) FROM "{table}"')[0][0]

    # -- 下見 → 写す → 写し直し -------------------------------------------
    def test_下見は書かず_写すと頁の表ができ_2回目3回目は変わらない(self) -> None:
        pages = [_accdb.nippou_page("L-1", page=p, lots=[f"H608{p}{i:02d}" for i in range(12)])
                 for p in (1, 2, 3)]
        self.access(*self.vba("L-1", pages), *self.vba("HVC"))
        report = self.svc.survey()
        self.assertTrue(report.dry_run)
        self.assertEqual(report.total(self.rule.ADD), 4)
        self.assertIn("下見しました(まだ写していません): 足す 4ページ", report.message())
        self.assertFalse(self.dest.exists())                    # 下見は何も作らない

        report = self.svc.run()
        self.assertTrue(report.ok, report.as_dict())
        self.assertEqual(report.total(self.rule.ADD), 4)
        l1 = report.lines[0]
        self.assertEqual((l1.line, l1.checked), ("L-1", "見出し 3/3 ・明細 36/36"))
        self.assertEqual(columns(self.dest, "T_日報ヘッダー_L-1")[3], "頁")
        self.assertEqual(report.backup, "")                     # 写す先が無かったので控えは要らない
        first = (self.dest_rows("T_日報ヘッダー_L-1"), self.dest_rows("T_日報明細_L-1", "4, 5"))
        for _ in range(2):
            again = self.svc.run()
            self.assertEqual((again.total(self.rule.ADD), again.total(self.rule.UPDATE),
                              again.total(self.rule.SAME)), (0, 0, 4))
            self.assertEqual((self.dest_rows("T_日報ヘッダー_L-1"),
                              self.dest_rows("T_日報明細_L-1", "4, 5")), first)
        self.assertEqual(self.count("T_日報明細_L-1"), 36)

    def test_値は打ったままの字で写す(self) -> None:
        """丸め・先頭の0・全角・空白・小数・改行・整数の列・日時。"""
        odd = {"KZ": "08", "KH": "05", "UNI": "1.50", "WEI": "116.0", "MAI": "０８",
               "KEN": " 40 ", "ET": "反転\r\nEX", "TIM": "1e3", "Others1": "0012",
               "CON": "", "SIZ": "0.10×1000×2000"}
        saved = dt.datetime(2026, 7, 22, 10, 35, 4, 999_600)    # 日数の小数の誤差 → 10:35:05
        page = _accdb.nippou_page("L-1", saved=saved, extra=odd)
        self.access(*self.vba("L-1", [page]))
        report = self.svc.run()
        self.assertTrue(report.ok, report.as_dict())
        conn = sqlite3.connect(self.dest)
        conn.row_factory = sqlite3.Row
        got = dict(conn.execute('SELECT * FROM "T_日報明細_L-1"').fetchone())
        head = dict(conn.execute('SELECT * FROM "T_日報ヘッダー_L-1"').fetchone())
        types = conn.execute('SELECT typeof("頁"), typeof("行番号"), typeof("KZ"), typeof("UNI")'
                             ' FROM "T_日報明細_L-1"').fetchone()
        conn.close()
        for column, value in odd.items():
            self.assertEqual(got[column], value, column)
        self.assertEqual(tuple(types), ("integer", "integer", "text", "text"))
        self.assertEqual((got["頁"], got["行番号"]), (1, 1))
        self.assertIsNone(got["S"])                               # NULL は NULL のまま
        self.assertIsNone(got["S4"])                              # Access の表に無い列は空
        self.assertEqual(head["保存日時"], "2026-07-22T10:35:05")
        self.assertEqual(head["昼勤"], "")                        # 長さ0の字は空の字のまま
        self.assertIsNone(head["理由"])

    def test_保存日時の新しいほうを残す_このツールの分は上書きしない(self) -> None:
        from nippou.access_bridge import sqlite_backend as sb
        from nippou.db.models import DetailRecord, HeaderRecord

        self.access(*self.vba("L-1", [_accdb.nippou_page("L-1", page=1, saved=T1),
                                       _accdb.nippou_page("L-1", page=2, saved=T1)]))
        self.svc.run()
        # このツールが2ページ目を共有へ保存した(Access より新しい)
        key = dict(report_date=DAY, line="L-1", shift="1直", page=2)
        sb.push_records(self.dest, HeaderRecord(**key, worker="ツール"),
                        [DetailRecord(**key, row_no=1, lot="T0001")],
                        "T_日報ヘッダー_L-1", "T_日報明細_L-1", saved_at="2026-10-01T09:00:00")
        # VBA は1ページ目を直した(保存日時が新しくなる)
        self.access(*self.vba("L-1", [
            _accdb.nippou_page("L-1", page=1, saved=T2, worker="木村", lots=("H6081Z9",)),
            _accdb.nippou_page("L-1", page=2, saved=T1)]))
        survey = self.svc.survey()
        self.assertEqual(survey.lines[0].counts,
                         {"足す": 0, "新しくする": 1, "そのまま": 0, "共有を残す": 1})
        report = self.svc.run()
        self.assertTrue(report.ok, report.as_dict())
        self.assertEqual(rows(self.dest, 'SELECT "頁", "担当者", "保存日時" FROM "T_日報ヘッダー_L-1"'
                                         ' ORDER BY 1'),
                         [(1, "木村", "2026-07-22T18:00:00"), (2, "ツール", "2026-10-01T09:00:00")])
        self.assertEqual(rows(self.dest, 'SELECT "頁", "LOT" FROM "T_日報明細_L-1" ORDER BY 1'),
                         [(1, "H6081Z9"), (2, "T0001")])
        self.assertTrue(any("上書きしません" in n for n in report.lines[0].notes))
        self.assertTrue(report.backup)                            # 書く前の控え
        self.assertTrue(Path(report.backup).is_file())
        self.assertEqual(Path(report.backup).parent, self.share / "backup")

    def test_Accessから消えたページは消さない(self) -> None:
        self.access(*self.vba("L-1", [_accdb.nippou_page("L-1", page=1),
                                       _accdb.nippou_page("L-1", page=2)]))
        self.svc.run()
        self.access(*self.vba("L-1", [_accdb.nippou_page("L-1", page=1)]))
        report = self.svc.run()
        self.assertEqual(report.total(self.rule.SAME), 1)
        self.assertEqual(self.count("T_日報ヘッダー_L-1"), 2)

    def test_ページの表へはページで写す(self) -> None:
        from nippou.access_bridge import sqlite_backend as sb

        old_table(self.dest, "T_日報ヘッダー_L-1", v435(sb.HEADER_COLUMNS), v435(sb.HEADER_KEY))
        old_table(self.dest, "T_日報明細_L-1", v435(sb.DETAIL_COLUMNS), v435(sb.DETAIL_KEY))
        self.access(*self.vba("L-1"))
        report = self.svc.run()
        self.assertTrue(report.ok, report.as_dict())
        self.assertNotIn("頁", columns(self.dest, "T_日報ヘッダー_L-1"))
        self.assertEqual(rows(self.dest, 'SELECT "ページ" FROM "T_日報明細_L-1"'), [(1,)])
        self.assertEqual(self.svc.run().total(self.rule.SAME), 1)

    def test_知らないラインは写さずに出す_前の名前の表は正規へ(self) -> None:
        self.access(*self.vba("予備PC"),
                    *self.vba("LS", [_accdb.nippou_page("LS", lots=("L5120Z0",))]))
        report = self.svc.run()
        self.assertEqual([u.as_dict()["line"] for u in report.unknown], ["予備PC"])
        self.assertEqual(report.unknown[0].pages, 1)
        self.assertIn("知らないライン(写さない): 予備PC 1ページ", report.message())
        tables = {r[0] for r in rows(self.dest, "SELECT name FROM sqlite_master")}
        self.assertNotIn("T_日報ヘッダー_予備PC", tables)
        self.assertEqual(rows(self.dest, 'SELECT "ライン", "LOT" FROM "T_日報明細_機側"'),
                         [("機側", "L5120Z0")])

    # -- 重なりを作らない -----------------------------------------------
    def test_Access側の同じページ_同じなら1つ_違えば新しいほう_決められなければ出す(self) -> None:
        head1, rows1 = _accdb.nippou_page("L-1", page=1, saved=T1)
        head2, rows2 = _accdb.nippou_page("L-1", page=2, saved=T1, worker="古い")
        head2b, _ = _accdb.nippou_page("L-1", page=2, saved=T2, worker="新しい")
        head3, rows3 = _accdb.nippou_page("L-1", page=3, saved=T1, worker="甲")
        head3b, _ = _accdb.nippou_page("L-1", page=3, saved=T1, worker="乙")
        head4, rows4 = _accdb.nippou_page("L-1", page=4, saved=T1)
        rows4b = [dict(rows4[0], LOT="別のロット")]
        header, detail = self.vba("L-1", [])
        header.rows = [head1, dict(head1), head2, head2b, head3, head3b, head4]
        detail.rows = rows1 + [dict(rows1[0])] + rows2 + rows3 + rows4 + rows4b
        self.access(header, detail)
        report = self.svc.run()
        line = report.lines[0]
        self.assertEqual(line.counts["足す"], 2)                  # 1ページ目と2ページ目
        self.assertEqual(len(line.skipped), 2)                     # 3ページ目と4ページ目
        self.assertIn("3ページ: 見出しが 2 行あり", line.skipped[0])
        self.assertIn("4ページ: 行番号 1 の明細が 2 行あり", line.skipped[1])
        self.assertEqual(rows(self.dest, 'SELECT "頁", "担当者" FROM "T_日報ヘッダー_L-1" ORDER BY 1'),
                         [(1, "青木"), (2, "新しい")])
        self.assertEqual(rows(self.dest, 'SELECT "頁", COUNT(*) FROM "T_日報明細_L-1" GROUP BY 1'),
                         [(1, 1), (2, 1)])
        # 写さなかったページは「写せなかったライン」ではない(ほかのページは写した)
        self.assertTrue(report.ok, report.as_dict())
        self.assertIn("写していません", "\n".join(line.notes))
        again = self.svc.run()                                     # 2回目も重ならない
        self.assertEqual(again.lines[0].counts["そのまま"], 2)
        self.assertEqual(self.count("T_日報明細_L-1"), 2)

    def test_報告日と直の書き方の揺れは1つのページ(self) -> None:
        from nippou.access_bridge import sqlite_backend as sb
        from nippou.db.models import DetailRecord, HeaderRecord

        key = dict(report_date=DAY, line="L-1", shift="1直", page=1)
        sb.push_records(self.dest, HeaderRecord(**key, worker="ツール"),
                        [DetailRecord(**key, row_no=1, lot="T0001")],
                        "T_日報ヘッダー_L-1", "T_日報明細_L-1", saved_at="2026-10-01T09:00:00")
        page = _accdb.nippou_page("L-1", day="2026/07/22", shift="１直")
        self.access(*self.vba("L-1", [page, _accdb.nippou_page("L-1", day="2026/7/23")]))
        report = self.svc.run()
        self.assertEqual(report.lines[0].counts,
                         {"足す": 1, "新しくする": 0, "そのまま": 0, "共有を残す": 1})
        self.assertEqual(rows(self.dest, 'SELECT "報告日", "直", "担当者" FROM "T_日報ヘッダー_L-1"'
                                         ' ORDER BY 1'),
                         [("2026年7月22日", "1直", "ツール"), ("2026年7月23日", "1直", "青木")])

    def test_前の名前の表と正規の表に同じページ_同じなら1つ(self) -> None:
        same = _accdb.nippou_page("L-1")
        old = ({**same[0], "ライン": "L1"}, [{**r, "ライン": "L1"} for r in same[1]])
        self.access(*self.vba("L-1", [same]), *self.vba("L1", [old]))
        report = self.svc.run()
        self.assertEqual(report.lines[0].counts["足す"], 1)
        self.assertEqual(self.count("T_日報明細_L-1"), 1)
        self.assertTrue(any("1つにして写します" in n for n in report.lines[0].notes))

    def test_消した行は写さない(self) -> None:
        header, detail = self.vba("L-1")
        header.deleted.append(dict(header.rows[0], 担当者="消した"))
        detail.deleted.append(dict(detail.rows[0], LOT="消した"))
        self.access(header, detail)
        self.svc.run()
        self.assertEqual(rows(self.dest, 'SELECT "担当者" FROM "T_日報ヘッダー_L-1"'), [("青木",)])
        self.assertEqual(rows(self.dest, 'SELECT "LOT" FROM "T_日報明細_L-1"'), [("H6081A1",)])

    # -- 守り -----------------------------------------------------------
    def test_表の定義の行数と違う表は写さない(self) -> None:
        header, detail = self.vba("L-1")
        detail.defined_rows = 5                                   # 読み落としの疑い
        self.access(header, detail, *self.vba("HVC"))
        report = self.svc.run()
        l1 = report.lines[0]
        self.assertIn("表の定義では 5行 ですが、読めたのは 1行", l1.error)
        self.assertIn("写せなかったライン: L-1", report.message())
        tables = {r[0] for r in rows(self.dest, "SELECT name FROM sqlite_master")}
        self.assertNotIn("T_日報ヘッダー_L-1", tables)
        self.assertEqual(self.count("T_日報ヘッダー_HVC"), 1)    # ほかのラインは写す

    def test_読み直して合わなければそのラインは書く前に戻す(self) -> None:
        self.access(*self.vba("L-1"), *self.vba("HVC"))
        real = self.svc.compare
        calls = []

        def wrong(conn, src, keys):
            calls.append(src.line)
            return ["2026年7月22日 L-1 1直 1ページ の明細"] if src.line == "L-1" else real(
                conn, src, keys)

        with mock.patch.object(self.svc, "compare", side_effect=wrong):
            report = self.svc.run()
        self.assertIn("書いた中身が Access と合いません", report.lines[0].error)
        tables = {r[0] for r in rows(self.dest, "SELECT name FROM sqlite_master")}
        self.assertNotIn("T_日報ヘッダー_L-1", tables)            # 表を作ったところから戻す
        self.assertEqual(self.count("T_日報明細_HVC"), 1)

    def test_ほかのPCが書いている最中なら待ってから断る(self) -> None:
        from nippou.access_bridge import sqlite_backend as sb

        self.access(*self.vba("L-1"))
        self.dest.parent.mkdir(exist_ok=True)
        other = sqlite3.connect(self.dest, timeout=0.1)
        other.execute("CREATE TABLE x (a)")
        other.commit()
        other.execute("BEGIN IMMEDIATE")
        try:
            with mock.patch.object(sb, "BUSY_TIMEOUT_MS", 300):
                report = self.svc.run()
        finally:
            other.rollback()
            other.close()
        self.assertIn("ほかの PC が共有の日報データに書いている最中でした", report.error)
        self.assertIn("何も写していません", report.error)
        tables = {r[0] for r in rows(self.dest, "SELECT name FROM sqlite_master")}
        self.assertEqual(tables, {"x"})

    def test_読めない形と_写す先がAccessのときは断る(self) -> None:
        self.source.write_bytes(b"")
        self.assertIn("空です", self.svc.survey().error)
        self.assertIn("見つかりません", self.svc.survey(str(self.share / "無い.accdb")).error)
        from nippou import config, user_settings

        user_settings.save_many({config.KEY_ACCESS_DB_FILE: "日報データ.accdb"})
        self.assertIn("sqlite3 のときだけ", self.svc.run().error)

    # -- 写したあと --------------------------------------------------------
    def test_写したあとロットを探すと紙に出る(self) -> None:
        from nippou.presenters import lot_search

        self.access(*self.vba("HVC", [_accdb.nippou_page("HVC", lots=("H6081A1", "R5083B2"))]))
        self.svc.run()
        query = lot_search.LotQuery.of({"lot": "H6081", "start": "2026-07-01",
                                        "end": "2026-07-31"}, "HVC")
        found = lot_search.search(self.repo(), query, own_line="L-1", editor=True)
        self.assertEqual([(h["lot"], h["source"]) for h in found.hits], [("H6081A1", "共有")])
        res = self.get("/report/nippou", query_string={"report_date": DAY, "line": "HVC",
                                                        "shift": "1直", "page": "all"})
        self.assertEqual(res.status_code, 200)
        self.assertIn("R5083B2", res.get_data(as_text=True))

    def test_後日作成の確かめは頁の表もページの表も読む(self) -> None:
        from nippou.access_bridge import sqlite_backend as sb
        from nippou.services import backfill

        self.access(*self.vba("L-1", [_accdb.nippou_page("L-1", page=1),
                                       _accdb.nippou_page("L-1", page=2)]))
        self.svc.run()
        self.assertEqual(backfill.shared_pages(DAY, "L-1", "1直"), [1, 2])
        self.dest.unlink()
        old_table(self.dest, "T_日報ヘッダー_L-1", v435(sb.HEADER_COLUMNS), v435(sb.HEADER_KEY))
        old_table(self.dest, "T_日報明細_L-1", v435(sb.DETAIL_COLUMNS), v435(sb.DETAIL_KEY))
        from nippou import source_db

        source_db.forget()
        self.svc.run()
        self.assertEqual(backfill.shared_pages(DAY, "L-1", "1直"), [1, 2])

    # -- 画面 ----------------------------------------------------------
    def test_押せるのは管理者だけ_下見してから写す(self) -> None:
        self.access(*self.vba("L-1"))
        page = self.get("/settings?tab=data").get_data(as_text=True)
        self.assertIn('id="accdb-copy"', page)
        self.assertIn("日報データ.accdb から写す", page)
        self.assertIn(f'value="{self.source}"', page)
        self.assertIn(f"写す先: <code>{self.dest}</code>", page)
        self.assertRegex(page, r'id="accdb-copy-survey"\s+disabled')
        self.assertRegex(page, r'id="accdb-copy-run"\s+disabled')
        self.assertEqual(self.get("/api/settings/accdb-copy").status_code, 403)
        self.assertEqual(self.post("/api/settings/accdb-copy", {}).status_code, 403)
        self.assertFalse(self.dest.exists())

        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        body = self.get(f"/api/settings/accdb-copy?source={self.source}").get_json()
        self.assertTrue(body["dry_run"])
        self.assertEqual(body["kinds"], ["足す", "新しくする", "そのまま", "共有を残す"])
        self.assertEqual(body["lines"][0]["counts"]["足す"], 1)
        res = self.post("/api/settings/accdb-copy", {"source": str(self.source)})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(res.get_json()["lines"][0]["checked"], "見出し 1/1 ・明細 1/1")
        self.assertEqual(self.count("T_日報明細_L-1"), 1)
        page = self.get("/settings?tab=data").get_data(as_text=True)
        self.assertNotRegex(page, r'id="accdb-copy-survey"\s+disabled')


class RuleTests(unittest.TestCase):
    def test_ページごとに決める(self) -> None:
        from nippou.logic import accdb_copy as rule

        a, b, c, d, e = ((DAY, "1直", n) for n in range(1, 6))
        plan = rule.decide(
            {a: "2026-07-22T10:00:00", b: "2026-07-22T12:00:00", c: "2026-07-22T10:00:00",
             d: "2026-07-22T10:00:00", e: "読めない"},
            {b: "2026/07/22 10:00:00", c: "2026-07-22T10:00:00", d: "2026-10-01T09:00:00",
             e: "2026-07-22T10:00:00"})
        self.assertEqual(plan.pages, {a: rule.ADD, b: rule.UPDATE, c: rule.SAME,
                                      d: rule.KEEP, e: rule.KEEP})
        self.assertEqual(plan.unknown, 1)

    def test_保存日時は一番近い秒_鍵の揃え方(self) -> None:
        from nippou.logic import accdb_copy as rule

        self.assertEqual(rule.saved_text(dt.datetime(2026, 7, 22, 10, 35, 4, 962_000)),
                         "2026-07-22T10:35:05")
        self.assertEqual(rule.saved_text(dt.datetime(2026, 7, 22, 10, 35, 5, 4)),
                         "2026-07-22T10:35:05")
        self.assertEqual(rule.saved_text("2026/7/22 9:05:00"), "2026-07-22T09:05:00")
        for text in ("2026年7月22日", "2026/7/22", "2026-07-22", "2026年07月22日"):
            self.assertEqual(rule.normal_date(text), "2026年7月22日", text)
        self.assertEqual(rule.normal_date("七月"), "七月")        # 読めない字はそのまま
        self.assertEqual(rule.normal_shift("１直"), "1直")
        self.assertEqual(rule.normal_shift("夜勤"), "夜勤")
        self.assertEqual(rule.page_number("1"), 1)
        self.assertIsNone(rule.page_number("1.5"))


if __name__ == "__main__":
    unittest.main()
