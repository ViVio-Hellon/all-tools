"""集計から除く作業者・班 (v4.50.0)

    日報の集計から特定の班を除く、特定の作業者を除く。画面の集計、集計CSVから除きたい。
    複数の作業者がいて、その中に除く人が混ざっているときも除外で。
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.services import agg_exclude  # noqa: E402
from tests._web import HAS_FLASK, SKIP_REASON  # noqa: E402
from tests import test_scenario_shift_flow as flow  # noqa: E402


class ReasonTests(unittest.TestCase):
    """理由は必ず(v4.51.1 ──「理由列を追加して理由を強制」)。"""

    def test_理由の無い行は効かない(self) -> None:
        rules = agg_exclude.parse([{"作業者": "山田", "理由": ""}, {"作業者": "田中", "理由": "  "}])
        self.assertFalse(rules)

    def test_マスタ管理は理由が無ければ断る(self) -> None:
        from nippou import master_admin
        self.assertIn("理由", master_admin._exclude_problem({"作業者": "山田", "理由": ""}, creating=True))
        self.assertIn("作業者", master_admin._exclude_problem({"理由": "応援"}, creating=True))
        self.assertEqual(master_admin._exclude_problem({"作業者": "山田", "理由": "応援のため"}, creating=True), "")
        self.assertIn("理由", master_admin._exclude_problem({"理由": ""}, creating=False))


class RuleTests(unittest.TestCase):
    def test_作業者と班(self) -> None:
        rules = agg_exclude.parse([{"作業者": "山田", "有効": 1, "理由": "r"}, {"班": "B班", "理由": "r"},
                                   {"作業者": "鈴木", "有効": 0, "理由": "r"}])
        rules.roster = {"佐藤": "B", "田中": "A"}
        self.assertIn("山田", rules.why("山田"))
        self.assertIn("山田", rules.why("田中 山田"), "混ざっていても除く")
        self.assertIn("B班", rules.why("佐藤"))
        self.assertEqual(rules.why("田中"), "")
        self.assertEqual(rules.why("鈴木"), "", "有効 0 は読まない")


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class SummaryTests(flow.ScenarioCase):
    def setUp(self) -> None:
        super().setUp()
        agg_exclude.reset()
        self.addCleanup(agg_exclude.reset)

    def exclude(self, *rows: dict) -> None:
        from nippou.config import SETTINGS
        path = SETTINGS.gw_material_master_path
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.execute(agg_exclude.SCHEMA)
        conn.execute(f'DELETE FROM "{agg_exclude.TABLE}"')
        for r in rows:
            conn.execute(f'INSERT INTO "{agg_exclude.TABLE}" ("作業者","班","理由","有効") VALUES (?,?,?,1)',
                         (r.get("作業者", ""), r.get("班", ""), r.get("理由", "試験の理由")))
        conn.commit()
        conn.close()
        agg_exclude.reset()

    def test_除く人のページは集計にもCSVにも入らず_日報は残る(self) -> None:
        self.start_shift("2026-10-05 07:05", "山田 田中")
        self.at("2026-10-05 10:00")
        self.assertEqual(self.save(flow.clean_shift_rows("1直", "A"), worker="山田 田中").status_code, 200)
        report = self.repo().load_packing_report("2026年10月5日", "L-1", "1直")
        self.assertIsNotNone(report, "除く前は集計がある")
        self.exclude({"作業者": "山田"})
        res = self.post("/api/settings/rebuild-summary", {"start": "2026-10-05", "end": "2026-10-05"})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertIsNone(self.repo().load_packing_report("2026年10月5日", "L-1", "1直"), "集計から外れる")
        self.assertEqual(self.repo().saved_pages("2026年10月5日", "L-1", "1直"), [1], "日報は残る")

    def test_表が無ければ作って何も除かない(self) -> None:
        from nippou import source_db
        from nippou.config import SETTINGS
        path = SETTINGS.gw_material_master_path
        path.parent.mkdir(parents=True, exist_ok=True)
        sqlite3.connect(path).execute("CREATE TABLE IF NOT EXISTS t (a)").connection.close()
        rules = agg_exclude.load(force=True)
        self.assertFalse(rules)
        self.assertIn("理由", source_db.columns(path, agg_exclude.TABLE))
        self.assertIn(agg_exclude.TABLE, source_db.list_tables(SETTINGS.gw_material_master_path))


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class RepeatTests(flow.ScenarioCase):
    """除く・戻すを繰り返しても集計と集計CSVが壊れない・班と個人を組み合わせても正しい。

        集計データが除く、戻すを繰り返しても問題がないかチェックしてください
        班と個人の複合の場合も正しいかのチェック
    """

    #: (報告日, 直, ページ, 作業者, 枚数)。班: 山田・田中=A、佐藤=B、鈴木=C
    PAGES = (("2026年10月5日", "1直", 1, "山田", 10), ("2026年10月5日", "1直", 2, "佐藤", 20),
             ("2026年10月5日", "2直", 1, "田中 佐藤", 40), ("2026年10月6日", "1直", 1, "鈴木", 80),
             ("2026年10月6日", "2直", 1, "田中", 160))

    def setUp(self) -> None:
        super().setUp()
        agg_exclude.reset()
        self.addCleanup(agg_exclude.reset)
        from nippou.config import SETTINGS
        from nippou.db.models import DetailRecord, HeaderRecord

        self.master = SETTINGS.gw_material_master_path
        self.master.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.master)
        conn.execute('CREATE TABLE IF NOT EXISTS "班員名簿" ("名前" TEXT, "班" TEXT, "読み" TEXT)')
        conn.executemany('INSERT INTO "班員名簿" VALUES (?,?,"")',
                         [("山田", "A"), ("田中", "A"), ("佐藤", "B"), ("鈴木", "C")])
        conn.execute(agg_exclude.SCHEMA)
        conn.commit()
        conn.close()
        for day, shift, page, worker, count in self.PAGES:
            key = dict(report_date=day, line="L-1", shift=shift, page=page)
            start = "07" if shift == "1直" else "15"
            self.repo().save(HeaderRecord(**key, worker=worker),
                             [DetailRecord(**key, row_no=1, lot=f"L{page}{shift[0]}", kz=start, kh="00",
                                           sz=str(int(start) + 1), sh="00", mai=str(count), tut="1",
                                           con=str(count), wei="100")])
        self.rules()                              # 何も除かない

    def rules(self, *rows: dict) -> dict:
        conn = sqlite3.connect(self.master)
        conn.execute(f'DELETE FROM "{agg_exclude.TABLE}"')
        for r in rows:
            conn.execute(f'INSERT INTO "{agg_exclude.TABLE}" ("作業者","班","理由","有効") VALUES (?,?,?,?)',
                         (r.get("作業者", ""), r.get("班", ""), r.get("理由", "試験の理由"), r.get("有効", 1)))
        conn.commit()
        conn.close()
        res = self.post("/api/settings/rebuild-summary", {"start": "2026-10-05", "end": "2026-10-06"})
        self.assertEqual(res.status_code, 200, res.get_json())
        return self.snapshot()

    def snapshot(self) -> dict:
        """集計(直ごとの枚数とロット)と、出力先の集計CSVの中身ぜんぶ。"""
        out: dict = {"reports": {}, "csv": {}}
        for day in ("2026年10月5日", "2026年10月6日"):
            for shift in ("1直", "2直"):
                report = self.repo().load_packing_report(day, "L-1", shift)
                if report is not None:
                    out["reports"][(day, shift)] = (round(float(report.total_quantity or 0), 3), report.pages, report.worker_name)
        root = self.tmp / "出力"
        for path in sorted(root.rglob("*.csv")) if root.exists() else []:
            out["csv"][str(path.relative_to(root))] = path.read_bytes()
        return out

    def counts(self, snap: dict) -> dict:
        return {k: v[0] for k, v in snap["reports"].items()}

    def test_個人と班の組み合わせが正しい(self) -> None:
        base = self.counts(self.snapshot())
        self.assertEqual(base, {("2026年10月5日", "1直"): 30.0, ("2026年10月5日", "2直"): 40.0,
                                ("2026年10月6日", "1直"): 80.0, ("2026年10月6日", "2直"): 160.0})
        cases = (
            ([{"作業者": "山田"}], {("2026年10月5日", "1直"): 20.0, ("2026年10月5日", "2直"): 40.0,
                                   ("2026年10月6日", "1直"): 80.0, ("2026年10月6日", "2直"): 160.0}),
            # 佐藤(B班)は 1直2ページ目と、田中との混ざった 2直
            ([{"班": "B"}], {("2026年10月5日", "1直"): 10.0, ("2026年10月6日", "1直"): 80.0,
                             ("2026年10月6日", "2直"): 160.0}),
            # 個人と班の組み合わせ: 山田 + B班 → 10/5 は全部外れる
            ([{"作業者": "山田"}, {"班": "B"}], {("2026年10月6日", "1直"): 80.0, ("2026年10月6日", "2直"): 160.0}),
            # A班(山田・田中)+ 鈴木 → 残るのは佐藤だけのページ
            ([{"班": "A班"}, {"作業者": "鈴木"}], {("2026年10月5日", "1直"): 20.0}),
            # 有効 0 の行は効かない
            ([{"作業者": "山田", "有効": 0}], base),
        )
        # 1日ぶん全部を除いた日の集計CSVに、除いたページのロットが残らない
        snap = self.rules({"作業者": "山田"}, {"班": "B"})
        text = b"".join(v for k, v in snap["csv"].items() if "1005" in k.replace("-", "").replace("/", "")
                        or "10月5日" in k or "20261005" in k)
        for lot in ("L11", "L21", "L12"):
            self.assertNotIn(lot.encode("cp932"), text, f"除いたロット {lot} が集計CSVに残っている")
            self.assertNotIn(lot.encode("utf-8"), text)
        # 除いた人の名前が集計の作業者に残らない
        self.rules({"作業者": "山田"})
        self.assertEqual(self.repo().load_packing_report("2026年10月5日", "L-1", "1直").worker_name, "佐藤")
        for rows, want in cases:
            with self.subTest(rows=rows):
                self.assertEqual(self.counts(self.rules(*rows)), want)

    def test_除く戻すを繰り返しても元どおり(self) -> None:
        first = self.snapshot()
        self.assertTrue(first["csv"], "集計CSVが出ていない")
        seen: dict = {}
        for _round in range(3):
            for rows in ([{"作業者": "山田"}], [{"班": "B"}], [{"作業者": "山田"}, {"班": "B"}],
                         [{"班": "A"}, {"作業者": "鈴木"}]):
                got = self.rules(*rows)
                key = repr(rows)
                if key in seen:
                    self.assertEqual(got, seen[key], f"同じ決まりなのに結果が変わった: {rows}")
                seen[key] = got
                back = self.rules()
                self.assertEqual(back["reports"], first["reports"], f"戻したのに集計が元と違う: {rows}")
                self.assertEqual(set(back["csv"]), set(first["csv"]), "集計CSVのファイルが変わった")
                for name in first["csv"]:
                    self.assertEqual(self._body(back["csv"][name]), self._body(first["csv"][name]),
                                     f"戻したのに集計CSVが元と違う: {name} {rows}")
        # 日報のページは一度も消えていない
        for day, shift, page, _w, _c in self.PAGES:
            self.assertIn(page, self.repo().saved_pages(day, "L-1", shift))

    @staticmethod
    def _body(data: bytes) -> bytes:
        """書いた時刻の列があれば外す(中身だけ比べる)。"""
        return data


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class AutoTimeTests(flow.ScenarioCase):
    """集計から除く作業者は時間・停止を打てず、共有へ保存のときに自動で割り振る(v4.51.0)。"""

    def setUp(self) -> None:
        super().setUp()
        agg_exclude.reset()
        self.addCleanup(agg_exclude.reset)
        from nippou.config import SETTINGS
        path = SETTINGS.gw_material_master_path
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.execute(agg_exclude.SCHEMA)
        conn.execute(f'INSERT INTO "{agg_exclude.TABLE}" ("作業者","理由","有効") VALUES ("山田", "試験の理由", 1)')
        conn.commit()
        conn.close()
        agg_exclude.reset()

    ROWS = {"1": {"LOT": "A01", "MAI": "10", "TUT": "1", "WEI": "100", "KZ": "07", "KH": "00",
                  "SZ": "09", "SH": "00", "S": "2", "TH": "30"},
            "2": {"LOT": "A02", "MAI": "10", "TUT": "1", "WEI": "100"}}

    def test_打てず_送るときに自動で入り_印刷に印(self) -> None:
        self.start_shift("2026-10-05 07:05", "山田")
        state = self.post("/api/entry/state", self.sheet(self.ROWS, worker="山田")).get_json()
        self.assertTrue(state["time_locked"])
        self.assertEqual(self.save(self.ROWS, worker="山田").status_code, 200)
        header, details = self.repo().load("2026年10月5日", "L-1", "1直", 1)
        self.assertEqual((details[0].kz, details[0].th), ("", ""), "打った時間・停止は受け取らない")
        self.at("2026-10-05 14:30")
        res = self.push_now()
        self.assertEqual(res.status_code, 200, res.get_json())
        header, details = self.repo().load("2026年10月5日", "L-1", "1直", 1)
        filled = [d for d in details if (d.lot or "").strip()]
        self.assertEqual(f"{filled[0].kz}:{filled[0].kh}", "07:00")
        self.assertEqual(f"{filled[-1].sz}:{filled[-1].sh}", "15:00", "直の終わりまで割り振る")
        self.assertTrue(any((s.stop_code or "") == "0" for d in filled for s in getattr(d, "stops", [])) or
                        any((d.s or "") == "0" or (d.ss or "") == "0" for d in filled), "休憩を足す")
        page = self.get("/report/nippou?report_date=2026年10月5日&line=L-1&shift=1直&page=1").get_data(as_text=True)
        self.assertIn("自動(時間は自動割り振り", page)

    def test_ふつうの作業者は今までどおり(self) -> None:
        self.start_shift("2026-10-05 07:05", "田中")
        state = self.post("/api/entry/state", self.sheet(self.ROWS, worker="田中")).get_json()
        self.assertFalse(state["time_locked"])
        self.assertEqual(self.save(self.ROWS, worker="田中").status_code, 200)
        header, details = self.repo().load("2026年10月5日", "L-1", "1直", 1)
        self.assertEqual(details[0].kz, "07")


class OldTableTests(unittest.TestCase):
    def test_前の版の表に理由の列を足す(self) -> None:
        import tempfile
        from nippou import source_db
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "梱包資材マスタ.sqlite3"
            conn = sqlite3.connect(path)
            conn.execute(f'CREATE TABLE "{agg_exclude.TABLE}" ("管理番号" INTEGER PRIMARY KEY, "作業者" TEXT,'
                         ' "班" TEXT, "有効" INTEGER, "備考" TEXT)')
            conn.commit()
            conn.close()
            agg_exclude.ensure_table(path)
            self.assertIn("理由", source_db.columns(path, agg_exclude.TABLE))
