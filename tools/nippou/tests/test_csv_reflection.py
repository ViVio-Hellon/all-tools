"""直しが集計CSVに入ったかを、共有フォルダの集計CSVを読んで確かめる (v4.46.9)

    直しが入ったかの確認は実際開いて確認しかないですよね
    → 共有フォルダの集計CSVを読み、直したあとに書き直されたかを見る仕組みを作ってください
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_correction_outputs import CorrectionCase  # noqa: E402


class CsvReflectionTests(CorrectionCase):
    def check(self, line: str, shift: str):
        from nippou.services import csv_reflection

        return csv_reflection.check(self.repo(), self.yesterday, line, shift)

    def write_csv(self, line: str) -> None:
        """そのラインのPCが書いた集計CSV(いまのこのPCの中身で)。"""
        from nippou.reporting import csv_export
        from nippou.services import summary

        repo = self.repo()
        for shift in ("1直", "2直", "3直"):
            summary.refresh_shift(repo, self.yesterday, line, shift)
        csv_export.write_daily_set(repo, self.yesterday, line, self.out)

    def test_CSVが無ければまだ(self) -> None:
        self.put("HVC", "2直", "HVC0002")
        got = self.check("HVC", "2直")
        self.assertEqual(got.state, "no_csv")
        self.assertIn("まだです", got.text)

    def test_同じ中身なら入っている(self) -> None:
        self.put("HVC", "2直", "HVC0002")
        self.write_csv("HVC")
        got = self.check("HVC", "2直")
        self.assertEqual(got.state, "done", got.diffs)

    def test_直したあと書き直されていなければまだ_違うところを出す(self) -> None:
        from nippou.services import summary

        self.put("HVC", "2直", "OLD0002")
        self.write_csv("HVC")                     # そのラインのPCが前に書いたCSV
        self.put("HVC", "2直", "NEW0002")         # 管理者のPCで直した
        summary.refresh_shift(self.repo(), self.yesterday, "HVC", "2直")
        got = self.check("HVC", "2直")
        self.assertEqual(got.state, "not_yet")
        self.assertTrue(any("ロット№" in d and "OLD0002" in d and "NEW0002" in d
                            for d in got.diffs), got.diffs)
        self.write_csv("HVC")                     # そのラインのPCが受け取って書き直した
        self.assertEqual(self.check("HVC", "2直").state, "done")

    def test_ほかの直の行は比べない(self) -> None:
        """集計CSVは1日分。直した直の行だけを見る。"""
        self.put("HVC", "1直", "HVC0001")
        self.put("HVC", "2直", "HVC0002")
        self.write_csv("HVC")
        with self.repo().conn as conn:            # このPCには2直しか無い(管理者のPC)
            conn.execute("DELETE FROM daily_detail WHERE shift='1直'")
            conn.execute("DELETE FROM daily_header WHERE shift='1直'")
        self.assertEqual(self.check("HVC", "2直").state, "done")

    def test_係数処理ロット数は比べない(self) -> None:
        """負荷係数はそのラインのPCが前の直を見て数え直すので、このPCと違ってよい。"""
        from nippou.reporting import csv_export

        self.put("HVC", "2直", "HVC0002")
        self.write_csv("HVC")
        path = csv_export.daily_output_paths(self.out, self.yesterday, "HVC")["detail"]
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        head = lines[0].split(",")
        col = head.index("係数処理ﾛｯﾄ数")
        cells = lines[1].split(",")
        cells[col] = "0.75"
        lines[1] = ",".join(cells)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
        self.assertEqual(self.check("HVC", "2直").state, "done")

    def test_行が足りなければまだ(self) -> None:
        from nippou.reporting import csv_export

        self.put("HVC", "2直", "HVC0002")
        self.write_csv("HVC")
        path = csv_export.daily_output_paths(self.out, self.yesterday, "HVC")["detail"]
        head = path.read_text(encoding="utf-8-sig").splitlines()[0]
        path.write_text(head + "\n", encoding="utf-8-sig")
        got = self.check("HVC", "2直")
        self.assertEqual(got.state, "not_yet")
        self.assertTrue(any("集計CSVに無い" in d for d in got.diffs), got.diffs)

    def test_画面から一覧の直をまとめて確かめる(self) -> None:
        self.put("L-1", "1直", "L1A0001")
        self.write_csv("L-1")
        res = self.post("/api/records/csv-check", {"keys": [
            {"report_date": self.yesterday, "line": "L-1", "shift": "1直"},
            {"report_date": self.yesterday, "line": "HVC", "shift": "2直"}]})
        self.assertEqual(res.status_code, 200, res.get_json())
        states = [r["state"] for r in res.get_json()["results"]]
        self.assertEqual(states, ["done", "empty"])
        self.assertEqual(self.post("/api/records/csv-check", {"keys": []}).status_code, 400)

    def test_記録を見るに集計CSVの欄がある(self) -> None:
        self.put("L-1", "1直", "L1A0001")
        html = self.client.get("/records").get_data(as_text=True)
        self.assertIn("data-csv-check", html)
        self.assertIn("集計CSVを読み直す", html)

    def test_ほかのラインを直したときの一言で見るところを言う(self) -> None:
        from nippou.services import day_csv

        self.assertIn("「記録を見る」の「集計CSV」の欄", day_csv.refusal("HVC"))


if __name__ == "__main__":
    unittest.main()
