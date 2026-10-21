"""書き出すファイルの名前: **ファイル名に使えない字を除く**決まりを1か所に(段階2・案2)

前は同じ字の並び `\\/:*?"<>|` が3つのモジュールにあり、除いて空なら何にするかも
呼ぶ所ごとに書いていました(11か所)。並びを1つ直すと、ほかの所だけ古いまま残ります。

ここでは**名前の出来上がり**を見ます(整理の前と後で同じ名前になること)。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.models import HeaderRecord  # noqa: E402
from nippou.logic import month_roll  # noqa: E402
from nippou.logic.standard_time import Criteria  # noqa: E402
from nippou.reporting import csv_export, print_format  # noqa: E402

BASE = Path("/out")
BAD = 'L\\/:*?"<>|1'          # 使えない字を全部はさんだライン名


class SafeNameTests(unittest.TestCase):
    def test_使えない字を除いて前後の空白を落とす(self) -> None:
        from nippou.file_names import safe_name

        self.assertEqual(safe_name(BAD), "L1")
        self.assertEqual(safe_name("  L-1 "), "L-1")

    def test_除いて空なら決めた代わりの名前(self) -> None:
        from nippou.file_names import safe_name

        self.assertEqual(safe_name('/:*'), "Unknown")
        self.assertEqual(safe_name("", default="-"), "-")
        self.assertEqual(safe_name(None), "Unknown")
        self.assertEqual(safe_name("", default=""), "")

    def test_長さを切るのは前後を落としたあと(self) -> None:
        from nippou.file_names import safe_name

        self.assertEqual(safe_name("  " + "あ" * 100, limit=80), "あ" * 80)

    def test_月替わりの名前も同じ決まり(self) -> None:
        self.assertEqual(month_roll.safe_name(BAD), "L1")
        self.assertEqual(month_roll.month_file_stem(2026, 8, BAD), "2026年08月_L1")


class OutputNameTests(unittest.TestCase):
    """どの書き出しも、使えない字を除いた名前になる(空なら決めた代わり)。"""

    def test_集計CSV(self) -> None:
        self.assertEqual(csv_export.default_output_path(BASE, "2026-08-03", BAD).name,
                         "集計_2026-08-03_L1.csv")
        self.assertEqual(csv_export.default_detail_output_path(BASE, "2026-08-03", "*").name,
                         "集計明細_2026-08-03_Unknown.csv")
        self.assertEqual(csv_export.default_stop_output_path(BASE, "2026-08-03", BAD).name,
                         "停止内訳_2026-08-03_L1.csv")

    def test_標準作業時間(self) -> None:
        self.assertEqual(csv_export.standard_time_output_path(BASE, "2026-09-26", BAD).name,
                         "標準作業時間_2026-09-26_L1.csv")
        self.assertEqual(csv_export.standard_time_output_path(
            BASE, "2026-09-26", "?", by_worker=True).name,
            "標準作業時間_作業者ごと_2026-09-26_Unknown.csv")

    def test_同条件作業(self) -> None:
        c = Criteria.parse(purpose_code="H1/76", packing_spec_no="1P0001")
        self.assertEqual(csv_export.works_output_path(BASE, "2026-09-26", BAD, c).name,
                         "同条件作業_2026-09-26_L1_H176_1P0001.csv")
        self.assertEqual(csv_export.works_output_path(BASE, "2026-09-26", "", c).name,
                         "同条件作業_2026-09-26_全ライン_H176_1P0001.csv")
        self.assertEqual(csv_export.works_output_path(BASE, "2026-09-26", "|", c).name,
                         "同条件作業_2026-09-26_-_H176_1P0001.csv")
        long = Criteria.parse(purpose_code="A" * 120)
        self.assertEqual(csv_export.works_output_path(BASE, "d", "L1", long).name,
                         f"同条件作業_d_L1_{'A' * 80}.csv")
        self.assertEqual(csv_export.works_output_path(BASE, "d", "L1",
                                                      Criteria.parse(purpose_code=":")).name,
                         "同条件作業_d_L1_-.csv")

    def test_梱包力(self) -> None:
        self.assertEqual(csv_export.power_output_path(BASE, "2026-10-03", BAD, "班/").name,
                         "梱包力_班ごと_2026-10-03_L1.csv")
        self.assertEqual(csv_export.power_output_path(BASE, "2026-10-03", "", "*").name,
                         "梱包力_-ごと_2026-10-03_全ライン.csv")

    def test_集計管理とロットを探す(self) -> None:
        self.assertEqual(csv_export.agg_output_path(BASE, "by_key", "s", "e", BAD).name,
                         "集計_ロット別_s_e_L1.csv")
        self.assertEqual(csv_export.agg_output_path(BASE, "days", "s", "e", "<>").name,
                         "集計_日別_s_e_Unknown.csv")
        self.assertEqual(csv_export.lot_search_output_path(BASE, "N7/1", "s", "e", BAD).name,
                         "ロットを探す_N71_s_e_L1.csv")
        self.assertEqual(csv_export.lot_search_output_path(BASE, "?", "s", "e", "").name,
                         "ロットを探す_-_s_e_Unknown.csv")

    def test_印刷(self) -> None:
        header = HeaderRecord(report_date="2026/08/03", line=BAD, shift="1直", page=1)
        self.assertEqual(print_format.default_output_path(BASE, header).name,
                         "印刷_20260803_L1_1直_1.html")
        header = HeaderRecord(report_date="2026-08-03", line="|", shift="2直", page=2)
        self.assertEqual(print_format.default_output_path(BASE, header).name,
                         "印刷_2026-08-03_Unknown_2直_2.html")


class OneListTests(unittest.TestCase):
    def test_使えない字の並びは1か所だけ(self) -> None:
        """並びを直すとき、ほかの所に古い並びが残らないように。"""
        root = Path(__file__).resolve().parent.parent / "nippou"
        hits = [str(p.relative_to(root)) for p in root.rglob("*.py")
                if "*?\"<>|" in p.read_text(encoding="utf-8")]
        self.assertEqual(hits, ["file_names.py"])


if __name__ == "__main__":
    unittest.main()
