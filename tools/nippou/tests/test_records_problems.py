"""記録を見る「保存した直」: 直すところがある直を赤く・見出しで並べ替え

    入力画面にあるエラーのあるデータは 保存した直 でも赤くしてくれないと探すのが大変です
    保存した直 で表示されているリストはヘッダークリックでソートするようにしてください

過去の日報をたくさん取り込んだあと、「前の直の間違い」の名指しが何十件も
出るのに、一覧ではどの直に間違いがあるのか見分けがつかなかった。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.models import DetailRecord, HeaderRecord
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class RecordsProblemTests(WebTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.post("/api/entry/line", {"line": "L-1"})
        repo = self.repo()
        # 梱包数 20 が検入枚数 15 を超えている(入力画面でも赤くなる間違い)
        bad = dict(report_date="2026年9月15日", line="L-1", shift="1直", page=1)
        repo.save(HeaderRecord(**bad, worker="前の人"),
                  [DetailRecord(**bad, row_no=1, lot="H573E52", ken="15", mai="10", tut="2",
                                kz="07", kh="00", sz="15", sh="00", s="0", th="60")])
        good = dict(report_date="2026年9月16日", line="L-1", shift="1直", page=1)
        repo.save(HeaderRecord(**good, worker="前の人"),
                  [DetailRecord(**good, row_no=1, lot="A1", ken="10", mai="5", tut="1",
                                kz="07", kh="00", sz="15", sh="00", s="0", th="60")])

    def html(self) -> str:
        return self.get("/records").get_data(as_text=True)

    def row_of(self, html: str, date: str) -> str:
        start = html.index(f'>{date}</td>')
        return html[html.rindex("<tr", 0, start):html.index("</tr>", start)]

    def test_直すところがある直は赤く件数と中身を出す(self) -> None:
        html = self.html()
        bad = self.row_of(html, "2026年9月15日")
        self.assertIn('class="row-bad"', bad)
        self.assertIn("直すところ", bad)
        self.assertIn("検入枚数 15 を超えています", bad)
        self.assertIn("直すところがある直が 1直 あります", html)

    def test_問題の無い直は赤くしない(self) -> None:
        good = self.row_of(self.html(), "2026年9月16日")
        self.assertNotIn("row-bad", good)
        self.assertIn("なし", good)

    def test_見出しで並べ替えられる(self) -> None:
        html = self.html()
        self.assertIn('id="records-table"', html)
        for kind in ('data-sort="date"', 'data-sort="num"', 'data-sort="text"'):
            self.assertIn(kind, html)
        # 報告日は 2026-09-15 の形で並べる(「2026年9月15日」の字の順では 10月が9月より前に来る)
        self.assertIn('data-value="2026-09-15"', html)
        js = (ROOT / "app/static/js/views/records.js").read_text(encoding="utf-8")
        self.assertIn('wireSort(document.getElementById("records-table"))', js)
        self.assertIn("aria-sort", js)


if __name__ == "__main__":
    unittest.main()
