"""集計管理の画面 (`presenters/agg_admin.py` / `app/routes/agg.py`)

VBA では、`Agg_OutPut` が並べた集計シートそのものが**見る場所**でした。
その見る場所をここへ移します。出すのは4つ ── 日別 / 直別 / ロット別 /
ロット一覧。

ここで守るのは:

    ・**紙に載らない欄が画面に出ること**(集計だけが残す場所なので、
      見る場所にも出ていないと残した意味がない)
    ・丸めるのは画面に出す直前の1回だけであること
    ・ロット別が直またぎを1行にまとめること
    ・JSが動かなくても表が読めること(サーバが描いてある)
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.connection import connect
from nippou.db.models import DetailRecord, HeaderRecord
from nippou.db.repository import NippouRepository
from nippou.db.schema import ensure_schema
from nippou.presenters import agg_admin
from nippou.services import summary as summary_service
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase

DAY = "2026年8月31日"
LINE = "L-1"


class AggAdminTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        conn = connect(Path(self._tmp.name) / "t.sqlite3")
        ensure_schema(conn)
        self.addCleanup(conn.close)
        self.repo = NippouRepository(conn)

    def _save(self, shift: str = "1直", **values) -> None:
        base = dict(lot="A", con="100", wei="2000", tim="400",
                    kz="8", kh="30", sz="10", sh="0",
                    others1="H176", others2="ｼﾔ-ｼ", others3="納入先A",
                    others4="1P0001", s="1", th="30", tut="2")
        base.update(values)
        self.repo.save(
            HeaderRecord(report_date=DAY, line=LINE, shift=shift, page=1,
                         worker="山田"),
            [DetailRecord(report_date=DAY, line=LINE, shift=shift, page=1,
                          row_no=1, **base)])
        summary_service.refresh_shift(self.repo, DAY, LINE, shift,
                                      labels={"1": "TPM活動"})

    def _build(self):
        return agg_admin.build(self.repo, line=LINE, start=date(2026, 8, 1),
                               end=date(2026, 8, 31))

    def test_表が4つそろう(self) -> None:
        self._save()
        self.assertEqual([t.key for t in self._build().tables],
                         ["days", "shifts", "by_key", "lots"])

    def test_粗いほうから細かいほうへ(self) -> None:
        """まず「今月どうなっているか」、それから降りていく順。"""
        self._save()
        titles = [t.title for t in self._build().tables]
        self.assertEqual(titles[0], "日別の集計")
        self.assertEqual(titles[-1], "ロット一覧")

    def test_ロット一覧に紙に載らない欄が出る(self) -> None:
        self._save()
        table = self._build().table("lots")
        for label in ("用途コード", "用途名", "納入先", "包装仕様NO",
                      "コイル縦割", "コイル横縦割"):
            self.assertIn(label, table.columns, label)
        self.assertIn("納入先A", table.rows[0])
        self.assertIn("H176", table.rows[0])

    def test_時刻は時分でまとめて出す(self) -> None:
        self._save()
        row = self._build().table("lots").rows[0]
        columns = self._build().table("lots").columns
        self.assertEqual(row[columns.index("開始")], "8:30")
        self.assertEqual(row[columns.index("終了")], "10:00")

    def test_打っていない時刻は空(self) -> None:
        self._save(kz="", kh="", sz="", sh="")
        table = self._build().table("lots")
        self.assertEqual(table.rows[0][table.columns.index("開始")], "")

    def test_停止が行に出る(self) -> None:
        self._save()
        table = self._build().table("lots")
        self.assertIn("1 TPM活動 30分",
                      table.rows[0][table.columns.index("作業停止")])

    def test_直別に稼働率と生産性が出る(self) -> None:
        self._save()
        table = self._build().table("shifts")
        for label in ("ﾛｯﾄ数", "係数ﾛｯﾄ数", "稼働率(%)", "生産性(t/h)",
                      "操業(分)"):
            self.assertIn(label, table.columns, label)
        row = table.rows[0]
        self.assertEqual(row[table.columns.index("枚数")], "100")
        self.assertEqual(row[table.columns.index("重量(t)")], "2.000")

    def test_日別に累積枚数が出る(self) -> None:
        self._save()
        table = self._build().table("days")
        self.assertIn("累積枚数(枚)", table.columns)
        self.assertEqual(table.rows[0][table.columns.index("累積枚数(枚)")],
                         "100")

    def test_ロット別は直またぎを1行にする(self) -> None:
        self._save("1直", tim="300", th="30")
        self._save("2直", tim="200", s="ｲ", th="20")
        table = self._build().table("by_key")
        self.assertEqual(len(table.rows), 1)
        row = table.rows[0]
        self.assertIn("またぎ", row[table.columns.index("直")])
        self.assertEqual(row[table.columns.index("作業(分)")], "500")
        self.assertEqual(row[table.columns.index("停止(分)")], "50")

    def test_数字の列は右寄せにする(self) -> None:
        self._save()
        table = self._build().table("shifts")
        self.assertTrue(table.numeric[table.columns.index("枚数")])
        self.assertFalse(table.numeric[table.columns.index("作業日")])

    def test_行が無くても表は出る(self) -> None:
        """**題だけでも出す。** 空白の画面は「壊れた」に見える。"""
        tables = self._build().tables
        self.assertEqual(len(tables), 4)
        self.assertTrue(all(t.empty for t in tables))

    def test_辞書にすると画面が読める形になる(self) -> None:
        self._save()
        body = self._build().as_dict()
        self.assertEqual(body["line"], LINE)
        self.assertTrue(all("numeric" in t and "count" in t
                            for t in body["tables"]))


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class AggScreenTests(WebTestCase):
    def _save(self) -> None:
        from nippou import work_context
        from nippou.db.models import DetailRecord as D
        from nippou.db.models import HeaderRecord as H

        from app.routes.entry import build_shift_calculator

        ctx = work_context.get_context()
        day, line, shift = ctx.current_key(
            build_shift_calculator(self.repo().get_shift_times()))
        self.repo().save(
            H(report_date=day, line=line, shift=shift, page=1, worker="山田"),
            [D(report_date=day, line=line, shift=shift, page=1, row_no=1,
               lot="A", con="120", wei="3000", tim="400",
               others3="納入先A", s="1", th="45")])

    def test_表がサーバ側で描かれている(self) -> None:
        """**JSが動かなくても読める。**"""
        self._save()
        body = self.get("/agg").get_data(as_text=True)
        self.assertIn("ロット一覧", body)
        self.assertIn("ロット・用途・納入先ごと", body)
        self.assertIn("納入先A", body)
        self.assertIn('data-table="lots"', body)

    def test_日報CSVの流れの図を開けて_その図が配られている(self) -> None:
        """図はツールの中の静的なページ(外へ取りに行かない・ダウンロードではなく開くだけ)。"""
        import re
        body = self.get("/agg").get_data(as_text=True)
        link = re.search(r'<a class="btn" id="csv-flow" href="([^"]+)"\s+target="_blank"', body)
        self.assertIsNotNone(link, "集計管理に「日報CSVの流れ(図)」がある")
        self.assertIn("日報CSVの流れ(図)", body)
        page = self.get(link.group(1))
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("<title>日報CSVの流れ</title>", html)
        self.assertIn("<svg", html)
        self.assertNotRegex(html, r"https?://", "外のサイトに取りに行かない(現場のPCは外に出られないことがある)")

    def test_レールに出ている(self) -> None:
        body = self.get("/agg").get_data(as_text=True)
        self.assertIn("集計管理", body)

    def test_期間を変えて取り直せる(self) -> None:
        self._save()
        res = self.post("/api/agg/tables",
                        {"start": "2020-01-01", "end": "2099-12-31"})
        self.assertEqual(res.status_code, 200)
        keys = [t["key"] for t in res.get_json()["tables"]]
        self.assertEqual(keys, ["days", "shifts", "by_key", "lots"])

    def test_日付の形が違えば400(self) -> None:
        res = self.post("/api/agg/tables", {"start": "x", "end": "y"})
        self.assertEqual(res.status_code, 400)

    def test_逆さの期間は422(self) -> None:
        res = self.post("/api/agg/tables",
                        {"start": "2026-12-31", "end": "2026-01-01"})
        self.assertEqual(res.status_code, 422)

    def test_4本ともCSVに出る(self) -> None:
        """**ツールが無くても読める形**にしておくのが目的。"""
        self._save()
        res = self.post("/api/agg/csv",
                        {"start": "2020-01-01", "end": "2099-12-31"})
        self.assertEqual(res.status_code, 200)
        files = res.get_json()["files"]
        self.assertEqual(len(files), 4)
        for name in ("日別", "直別", "ロット別", "ロット一覧"):
            self.assertTrue(any(name in f for f in files), name)

    def test_CSVはExcelで開ける形(self) -> None:
        import csv

        self._save()
        self.post("/api/agg/csv", {"start": "2020-01-01", "end": "2099-12-31"})
        found = list(Path(self.tmp).rglob("集計_ロット一覧_*.csv"))
        self.assertTrue(found, "書き出したファイルが見つかりません")
        with open(found[0], encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        self.assertIn("納入先", rows[0])
        self.assertTrue(any("納入先A" in row for row in rows[1:]))


if __name__ == "__main__":
    unittest.main()
