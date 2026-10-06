"""ライン名は画面と紙でも、中でも正規の呼び名 (v4.12.5 / v4.13.0)

    上の帯の「ライン LS」や、記録を見る・集計などのライン選択も「機側」のような
    正規の呼び名に揃えますか? → 変えてください
    これを機にそこ(キー・DB・CSV)も変えませんか? → v4.13.0

【約束】
    ・帯・見出し・選ぶ欄・表のライン列・知らせ・紙は正規の呼び名(v4.12.5)
    ・**値(キー)・DB・共有の表の名前・CSV の中身・フォルダ名も正規の呼び名**(v4.13.0)
    ・中板は設備番号を続けて「中板4」(`line_label.js` も同じ)
    ・JS は `<body data-line-labels>` の表を `line_label.js` で引く(前の名前も正規へ引ける)
    ・同条件の作業のラインの欄は、正規の呼び名でも前の名前でも受ける
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import line_names as ln  # noqa: E402
from tests._web import WebTestCase  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class LabelTests(unittest.TestCase):
    def test_前の名前も正規の呼び名で出す(self) -> None:
        self.assertEqual([ln.label(c) for c in ("L1", "LVC", "HVC", "LS", "NS1", "AIM",
                                                "TOT", "BALA", "MARU")],
                         ["L-1", "LVC", "HVC", "機側", "NS1", "AIM", "トット", "バランサー", "中板"])
        self.assertEqual(ln.label("中板", "4"), "中板4")
        self.assertEqual(ln.label("機側", "4"), "機側")              # 番号は中板だけ

    def test_知らないものはそのまま(self) -> None:
        for text in ("未設定", "—", "", "予備PC"):
            self.assertEqual(ln.label(text), text)
        self.assertEqual(ln.label(None), "")

    def test_JSに渡す表(self) -> None:
        self.assertEqual(ln.labels()["LS"], "機側")
        self.assertEqual(ln.labels()["機側"], "機側")
        self.assertEqual(len(ln.labels()), 14)                     # 正規9 + 字の違う前5


class LabelWebTests(WebTestCase):
    terminal_line = "機側"

    def test_帯は正規の呼び名(self) -> None:
        page = self.get("/").get_data(as_text=True)
        self.assertRegex(page, r'id="rb-line"[^>]*>機側</span>')
        body = self.get("/api/shift/key").get_json()
        self.assertEqual(body["ribbon"]["line"], "機側")

    def test_選ぶ欄は値も字も正規の呼び名(self) -> None:
        page = self.get("/records").get_data(as_text=True)
        self.assertIn('<option value="L-1" >L-1</option>', page)
        self.assertIn('<option value="機側" selected>機側</option>', page)
        self.assertIn('<option value="中板" >中板</option>', page)
        self.assertIn("/ 機側 /", page)                              # いまの直

    def test_ほかの画面の見出し(self) -> None:
        for path, needle in (("/standard-time", "このライン(機側)"), ("/agg", "このライン(機側)"),
                             ("/graph", "/ 機側</span>")):
            with self.subTest(path=path):
                self.assertIn(needle, self.get(path).get_data(as_text=True))

    def test_JSが引く表(self) -> None:
        page = self.get("/").get_data(as_text=True)
        found = re.search(r'data-line-labels="([^"]+)"', page)
        self.assertIsNotNone(found)
        table = json.loads(found.group(1).replace("&#34;", '"').replace("&quot;", '"'))
        self.assertEqual(table["トット"], "トット")
        self.assertEqual(table["TOT"], "トット")
        js = (ROOT / "app" / "static" / "js" / "line_label.js").read_text(encoding="utf-8")
        self.assertIn("export function lineLabel", js)
        for view in ("records", "agg", "settings", "entry", "standard_time"):
            text = (ROOT / "app" / "static" / "js" / "views" / f"{view}.js").read_text(
                encoding="utf-8")
            self.assertIn('import { lineLabel } from "../line_label.js";', text, view)

    def test_紙もファイル名も正規の呼び名(self) -> None:
        from nippou.db.models import DetailRecord, HeaderRecord
        from nippou.reporting import print_format

        key = dict(report_date="2026年10月1日", line="機側", shift="1直", page=1)
        html = print_format.build_print_html(HeaderRecord(**key), [DetailRecord(**key, row_no=1)])
        self.assertIn("<title>日報 2026年10月1日 機側 1直", html)
        self.assertNotIn(">LS<", html)
        path = print_format.default_output_path(self.tmp, HeaderRecord(**key))
        self.assertIn("_機側_", path.name)                          # ファイル名・フォルダも(v4.13.0)
        self.assertIn("機側", path.parts)

    def test_同条件の作業は正規の呼び名でも前の名前でも(self) -> None:
        from nippou.presenters import standard_time as view

        base = {"purpose_code": "H176"}
        self.assertEqual(view.WorkFilters.of({**base, "line": "機側"}, "L-1").line, "機側")
        self.assertEqual(view.WorkFilters.of({**base, "line": "LS"}, "L-1").line, "機側")
        self.assertEqual(view.WorkFilters.of({**base, "line": "ﾄｯﾄ"}, "L-1").line, "トット")
        self.assertEqual(view.WorkFilters.of({**base, "line": ""}, "L-1").line, "")
        self.assertIn('value="機側"', self.get("/standard-time").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
