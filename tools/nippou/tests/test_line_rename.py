"""コンバート ── 前の名前の表(VBA)を、正規の名前の表へ写す (v4.13.0)

    これを機にそこも変えませんか？ … このデータをコンバートする仕組みだけで済む
    ラインごと・並行になるので本ツールは正規名、VBAは旧で蓄積されてしまうと思います
    これがあれば任意のタイミングで変えれるんですよね？

【約束】
    ・前の表には触らない(VBA はそのまま。何度でもやり直せる)
    ・何回押しても同じ(写し済みは飛ばす)。VBA で直した分は写し直し、VBA で消した分は写しも消す
    ・**このツールで書いたページは上書きしない**。このツールで消した写しは写し直さない
    ・写すのは名前の変わる5ライン。LVC・HVC・NS1・AIM は触らない
    ・ページ・行番号は数に揃える(このツールと同じ ── 同じページを2つ作らない)
    ・写した数を突き合わせ、合わなければそのラインは取り消す
    ・書かずに試すときは共有のファイルを開かない(写しを読む)
    ・押せるのは管理者だけ(管理者モード / Administrator)
    ・手元のDB・端末の設定・CSV に残った前の名前も正規へ読み替える
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import line_rename as rule  # noqa: E402
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402

DAY = "2026年9月1日"


class RuleTests(unittest.TestCase):
    def test_ページごとに決める(self) -> None:
        a, b, c, d, e, f = ((DAY, "1直", n) for n in range(1, 7))
        plan = rule.decide(
            old={a: "t1", b: "t2", c: "t1", d: "t1", e: "t1"},
            new={b: "t1", c: "t1", d: "tool", f: "t1"},
            copied={b: "t1", c: "t1", e: "t1", f: "t1"})
        self.assertEqual(plan.pages, {a: rule.COPY,           # まだ無い
                                      b: rule.RECOPY,         # VBA で直した
                                      c: rule.SAME,           # 写し済み
                                      d: rule.KEEP,           # このツールで書いた
                                      e: rule.DELETED_HERE,   # このツールで消した
                                      f: rule.GONE})          # VBA で消した
        self.assertTrue(plan.writes)

    def test_このツールが書き直した写しは消さない(self) -> None:
        key = (DAY, "1直", 1)
        plan = rule.decide(old={}, new={key: "tool"}, copied={key: "t1"})
        self.assertEqual(plan.pages, {})
        self.assertFalse(plan.writes)

    def test_ページと行番号は数に揃える(self) -> None:
        for value in (1, 1.0, "1", " 1 ", "１", "1.0"):
            self.assertEqual(rule.number_of(value), 1, value)
        self.assertEqual(rule.number_of("A"), "A")
        self.assertEqual(rule.number_of(None), "")
        self.assertEqual(rule.page_key({"報告日": DAY, "直": "1直", "ページ": "2"}),
                         (DAY, "1直", 2))

    def test_数の突き合わせ(self) -> None:
        key = (DAY, "1直", 1)
        self.assertTrue(rule.check({key: (True, 3)}, {key: (True, 3)}).ok)
        found = rule.check({key: (True, 3)}, {key: (True, 2)})
        self.assertFalse(found.ok)
        self.assertEqual(found.describe(), "見出し 1/1 ・明細 2/3")


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class ConvertTests(WebTestCase):
    """共有の日報データ(sqlite3)を一時フォルダに作って試す。"""

    def setUp(self) -> None:
        super().setUp()
        from nippou import config, user_settings

        self.share = self.tmp / "share"
        self.share.mkdir()
        user_settings.save_many({config.KEY_ACCESS_DIR: str(self.share)})
        from nippou.config import SETTINGS

        self.path = SETTINGS.access_db_path
        from nippou.services import line_rename

        self.svc = line_rename

    # -- 共有のファイルを VBA の形で作る ---------------------------------
    def vba_table(self, old: str) -> None:
        from nippou.access_bridge import sqlite_backend as sb

        conn = sqlite3.connect(self.path)
        for prefix, cols in (("T_日報ヘッダー_", sb.HEADER_COLUMNS), ("T_日報明細_", sb.DETAIL_COLUMNS)):
            conn.execute(f'CREATE TABLE IF NOT EXISTS "{prefix}{old}" '
                         f'({", ".join(sb.quote(c) for c in cols)})')
        conn.commit()
        conn.close()

    def vba_page(self, old: str, page: object = 1, saved: str = "2026/09/01 10:00",
                 rows: int = 3, day: str = DAY, line: str = "") -> None:
        """VBA が書いたページ(ページ・行番号は数で入れることも、字で入れることもある)。"""
        from nippou.access_bridge import sqlite_backend as sb

        self.vba_table(old)
        conn = sqlite3.connect(self.path)
        conn.execute(f'INSERT INTO "T_日報ヘッダー_{old}" VALUES ({",".join("?" * 12)})',
                     (day, line or old, "1直", page, "青木", "", "10", "100", "1", "1", "", saved))
        for number in range(1, rows + 1):
            conn.execute(f'INSERT INTO "T_日報明細_{old}" '
                         f'VALUES ({",".join("?" * len(sb.DETAIL_COLUMNS))})',
                         (day, line or old, "1直", page, str(number), f"N710{number}T0")
                         + ("1",) * (len(sb.DETAIL_COLUMNS) - 6))
        conn.commit()
        conn.close()

    def rows(self, sql: str, *params) -> list[tuple]:
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def tables(self) -> set[str]:
        return {r[0] for r in self.rows("SELECT name FROM sqlite_master WHERE type='table'")}

    # -- 試験 ----------------------------------------------------------
    def test_写す_前の表には触らない(self) -> None:
        self.vba_page("LS", page=1)
        self.vba_page("LS", page="2")
        self.vba_page("TOT", page=1.0)
        before = self.rows('SELECT * FROM "T_日報明細_LS" ORDER BY 4, 5')
        report = self.svc.run()
        self.assertTrue(report.ok, report.as_dict())
        self.assertEqual(report.total(rule.COPY), 3)
        # 正規の名前の表に、正規のライン名で。ページは数に揃える
        self.assertEqual(self.rows('SELECT "ライン", "ページ", typeof("ページ") '
                                   'FROM "T_日報ヘッダー_機側" ORDER BY 2'),
                         [("機側", 1, "integer"), ("機側", 2, "integer")])
        self.assertEqual(self.rows('SELECT COUNT(*), MIN("ライン") FROM "T_日報明細_トット"'),
                         [(3, "トット")])
        self.assertEqual(self.rows('SELECT DISTINCT typeof("行番号") FROM "T_日報明細_機側"'),
                         [("integer",)])
        # 前の表はそのまま
        self.assertEqual(self.rows('SELECT * FROM "T_日報明細_LS" ORDER BY 4, 5'), before)
        machine = next(line for line in report.lines if line.official == "機側")
        self.assertEqual(machine.checked, "見出し 2/2 ・明細 6/6")
        self.assertIn("コンバートしました: 写したページ 3", report.message())

    def test_何回押しても同じ(self) -> None:
        self.vba_page("L1")
        self.svc.run()
        first = self.rows('SELECT * FROM "T_日報明細_L-1" ORDER BY 4, 5')
        report = self.svc.run()
        self.assertEqual((report.total(rule.COPY), report.total(rule.SAME)), (0, 1))
        self.assertEqual(self.rows('SELECT * FROM "T_日報明細_L-1" ORDER BY 4, 5'), first)

    def test_並行のあいだVBAが書き足した分だけ写す(self) -> None:
        self.vba_page("BALA", day="2026年9月1日")
        self.svc.run()
        self.vba_page("BALA", day="2026年9月2日")
        report = self.svc.run()
        self.assertEqual((report.total(rule.COPY), report.total(rule.SAME)), (1, 1))
        self.assertEqual(len(self.rows('SELECT * FROM "T_日報ヘッダー_バランサー"')), 2)

    def test_VBAで直した分は写し直す_消した分は写しも消す(self) -> None:
        self.vba_page("MARU", page=1)
        self.vba_page("MARU", page=2)
        self.svc.run()
        conn = sqlite3.connect(self.path)
        conn.execute('UPDATE "T_日報ヘッダー_MARU" SET "担当者"=?, "保存日時"=? WHERE "ページ"=1',
                     ("木村", "2026/09/01 12:00"))
        conn.execute('DELETE FROM "T_日報ヘッダー_MARU" WHERE "ページ"=2')
        conn.execute('DELETE FROM "T_日報明細_MARU" WHERE "ページ"=2')
        conn.commit()
        conn.close()
        report = self.svc.run()
        self.assertEqual((report.total(rule.RECOPY), report.total(rule.GONE)), (1, 1))
        self.assertEqual(self.rows('SELECT "ページ", "担当者" FROM "T_日報ヘッダー_中板"'),
                         [(1, "木村")])
        self.assertEqual(self.rows('SELECT COUNT(*) FROM "T_日報明細_中板"'), [(3,)])

    def test_このツールで書いたページは上書きしない(self) -> None:
        from nippou.access_bridge import sqlite_backend as sb
        from nippou.db.models import DetailRecord, HeaderRecord

        self.vba_page("LS", page=1)
        self.svc.run()
        # 切り替えた日: このツールが同じページを共有へ保存した
        key = dict(report_date=DAY, line="機側", shift="1直", page=1)
        sb.push_records(self.path, HeaderRecord(**key, worker="ツール"),
                        [DetailRecord(**key, row_no=1)],
                        "T_日報ヘッダー_機側", "T_日報明細_機側")
        # VBA の側も直された
        conn = sqlite3.connect(self.path)
        conn.execute('UPDATE "T_日報ヘッダー_LS" SET "保存日時"=?', ("2026/09/02 08:00",))
        conn.commit()
        conn.close()
        report = self.svc.run()
        self.assertEqual(report.total(rule.KEEP), 1)
        self.assertEqual(self.rows('SELECT "担当者" FROM "T_日報ヘッダー_機側"'), [("ツール",)])
        self.assertTrue(any("このツールの分を残します" in n
                            for line in report.lines for n in line.notes))

    def test_このツールで消した写しは写し直さない(self) -> None:
        self.vba_page("LS")
        self.svc.run()
        conn = sqlite3.connect(self.path)
        conn.execute('DELETE FROM "T_日報ヘッダー_機側"')
        conn.execute('DELETE FROM "T_日報明細_機側"')
        conn.commit()
        conn.close()
        report = self.svc.run()
        self.assertEqual(report.total(rule.DELETED_HERE), 1)
        self.assertEqual(self.rows('SELECT COUNT(*) FROM "T_日報ヘッダー_機側"'), [(0,)])

    def test_名前が同じラインと_知らない名前の表(self) -> None:
        self.vba_page("LVC")
        self.vba_page("コイル")
        report = self.svc.run()
        self.assertEqual(report.same_name, ["LVC", "HVC", "NS1", "AIM"])
        self.assertEqual([line.official for line in report.lines],
                         ["L-1", "機側", "トット", "バランサー", "中板"])
        self.assertIn("T_日報ヘッダー_コイル", report.unknown_tables)
        self.assertNotIn("T_日報ヘッダー_LVC", report.unknown_tables)
        self.assertEqual(self.rows('SELECT COUNT(*) FROM "T_日報ヘッダー_LVC"'), [(1,)])

    def test_ライン列が違う行は写さずに言う(self) -> None:
        self.vba_page("LS", page=1)
        self.vba_page("LS", page=2, line="NS1")
        report = self.svc.run()
        machine = next(line for line in report.lines if line.official == "機側")
        self.assertEqual((machine.counts[rule.COPY], machine.other_rows), (1, 4))
        self.assertIn("ライン列が「LS」でない行が 4 行", machine.notes[0])

    def test_書かずに試す(self) -> None:
        self.vba_page("L1")
        before = self.path.read_bytes()
        report = self.svc.survey()
        self.assertTrue(report.dry_run)
        self.assertEqual(report.total(rule.COPY), 1)
        self.assertIn("書かずに試しました: 写すページ 1", report.message())
        self.assertEqual(self.path.read_bytes(), before)
        self.assertNotIn("T_日報ヘッダー_L-1", self.tables())

    def test_数が合わなければそのラインは取り消す(self) -> None:
        from unittest import mock

        from nippou.logic import line_rename as now   # setUp が読み直したほう

        self.vba_page("L1")
        self.vba_page("LS")
        wrong = now.Check(expected_pages=1, found_pages=1, expected_rows=3, found_rows=2)
        real = now.check
        calls = []

        def fake(expected, found):
            calls.append(1)
            return wrong if len(calls) == 1 else real(expected, found)

        with mock.patch.object(now, "check", side_effect=fake):
            report = self.svc.run()
        self.assertFalse(report.ok)
        l1 = report.lines[0]
        self.assertIn("写した数が合いません", l1.error)
        self.assertNotIn("T_日報ヘッダー_L-1", self.tables())    # 表を作ったところから取り消す
        self.assertEqual(self.rows('SELECT COUNT(*) FROM "T_日報ヘッダー_機側"'), [(1,)])  # ほかは進む
        self.assertIn("できなかったライン: L-1", report.message())

    def test_Accessや見つからないときは断る(self) -> None:
        self.assertIn("見つかりません", self.svc.run().error)
        from nippou import config, user_settings

        user_settings.save_many({config.KEY_ACCESS_DB_FILE: "日報データ.accdb"})
        self.assertIn("sqlite3 のときだけ", self.svc.survey().error)

    # -- 画面 ----------------------------------------------------------
    def test_押せるのは管理者だけ(self) -> None:
        self.vba_page("LS")
        page = self.get("/settings?tab=data").get_data(as_text=True)
        self.assertIn('id="line-rename"', page)
        self.assertRegex(page, r'id="line-rename-run"\s+disabled')
        self.assertIn("LS → 機側", page)
        # どのファイルか(「共有へ保存」と同じ共有の日報データ ── v4.16.0)
        self.assertIn(f"対象のファイル: <code>{self.path}</code>", page)
        self.assertIn("「共有の日報管理のパス」で決まります", page)
        self.assertEqual(self.get("/api/settings/line-rename").status_code, 403)
        self.assertEqual(self.post("/api/settings/line-rename", {}).status_code, 403)
        self.assertNotIn("T_日報ヘッダー_機側", self.tables())

        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        body = self.get("/api/settings/line-rename").get_json()
        self.assertTrue(body["dry_run"])
        self.assertEqual(body["kinds"][0], "写す")
        res = self.post("/api/settings/line-rename", {})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(res.get_json()["lines"][1]["counts"]["写す"], 1)
        self.assertIn("T_日報ヘッダー_機側", self.tables())
        page = self.get("/settings?tab=data").get_data(as_text=True)
        self.assertNotRegex(page, r'id="line-rename-run"\s+disabled')


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class LeftoverNameTests(WebTestCase):
    """v4.12 までの名前が、端末の設定・手元のDBに残っていても読める。"""

    terminal_line = "LS"

    def test_端末の設定の前の名前は正規で読む(self) -> None:
        from nippou import work_context

        ctx = work_context.reset()
        self.assertEqual(work_context.get_context().terminal_line, "機側")
        self.assertTrue(work_context.terminal_line_decided())
        self.assertIsNotNone(ctx)
        self.assertRegex(self.get("/").get_data(as_text=True), r'id="rb-line"[^>]*>機側</span>')

    def test_手元のDBの前の名前は1度だけ正規へ揃える(self) -> None:
        from nippou.config import SETTINGS
        from nippou.db import schema
        from nippou.db.connection import connect

        conn = connect(SETTINGS.sqlite_path)
        conn.execute("PRAGMA user_version=0")       # v4.12 までのDBの再現
        conn.execute("INSERT INTO daily_header (report_date, line, shift, page, saved_at) "
                     "VALUES (?, 'MARU', '1直', 1, 't')", (DAY,))
        conn.execute("INSERT INTO daily_detail (report_date, line, shift, page, row_no) "
                     "VALUES (?, 'MARU', '1直', 1, 1)", (DAY,))
        conn.execute("INSERT INTO packing_report (work_date, line_name, shift, created_at, "
                     "updated_at) VALUES (?, 'TOT', '1直', 't', 't')", (DAY,))
        conn.commit()
        conn.close()
        conn = connect(SETTINGS.sqlite_path)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT line FROM daily_header").fetchall()[0][0], "中板")
        self.assertEqual(conn.execute("SELECT line FROM daily_detail").fetchall()[0][0], "中板")
        self.assertEqual(conn.execute("SELECT line_name FROM packing_report").fetchall()[0][0],
                         "トット")
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0],
                         schema.LINE_NAMES_VERSION)
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_CSVの前の名前は正規へ(self) -> None:
        from nippou.config import SETTINGS
        from nippou.db.models import DetailRecord, HeaderRecord
        from nippou.logic import csv_import
        from nippou.reporting import csv_export

        key = dict(report_date=DAY, line="機側", shift="1直", page=1)
        repo = self.repo()
        repo.save(HeaderRecord(**key), [DetailRecord(**key, row_no=1, lot="N7101T0", mai="5")])
        out = self.tmp / "明細.csv"
        csv_export.write_daily_detail_csv(repo, DAY, "機側", out)
        # VBA・変換器の CSV はライン列が前の名前
        old = self.tmp / "前の名前.csv"
        old.write_text(out.read_text(encoding="utf-8-sig").replace("機側", "LS"),
                       encoding="utf-8-sig")
        found = csv_import.parse_file(old)
        self.assertTrue(found.ok, found.problems)
        self.assertEqual({p.header.line for p in found.pages}, {"機側"})
        self.assertEqual({d.line for p in found.pages for d in p.details}, {"機側"})
        self.assertTrue(SETTINGS.sqlite_path.exists())


if __name__ == "__main__":
    unittest.main()
