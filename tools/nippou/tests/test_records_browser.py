"""記録を見る・標準作業時間: 画面の配線を**本物のブラウザ**で確かめる (段階1)

前は `views/records.js`・`views/standard_time.js` の字を探して「仕組みが入っていること」だけを
見ていました(`test_records_problems`・`test_backfill`・`test_worker_view` の一部)。
ここでは画面を開いて押します:

    - 保存した直の表を見出しで並べ替える(報告日は日の順 ── 字の順なら 10月が9月より前に来る)。
      もう一度押すと逆順
    - この直を後から作る: **まず確かめるだけ**(`dry_run`)を送り、サーバの言葉で訊いてから作る。
      作ったら日報入力にその直が開く
    - ほかのラインの直の「呼び出す」は、サーバが行ごとに決めた道(控えから入れて開く)で頼む
    - 作業者名で見る: 普段は「作業者ごと」を出さない。入れたら選べる。切ったら班ごとへ戻す
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.models import DetailRecord, HeaderRecord  # noqa: E402
from tests._other_screen_browser import ScreenBrowserCase  # noqa: E402
from tests.test_records_line import SavedLineCase  # noqa: E402


class RecordsBrowserCase(ScreenBrowserCase):
    def setUp(self) -> None:
        super().setUp()
        self.post("/api/entry/line", {"line": "L-1"})

    def open_records(self):
        self.settle_review()
        return self.open_screen("/records", "#records-table")

    @staticmethod
    def dates(page) -> list[str]:
        return page.eval_on_selector_all(
            "#records-table tbody tr", "(rows) => rows.filter((r) => r.cells.length > 1)"
            ".map((r) => r.cells[0].textContent.trim())")


class SortTests(RecordsBrowserCase):
    def setUp(self) -> None:
        super().setUp()
        repo = self.repo()
        for day in ("2026年9月15日", "2026年10月1日", "2026年9月16日"):
            key = dict(report_date=day, line="L-1", shift="1直", page=1)
            repo.save(HeaderRecord(**key, worker="前の人"),
                      [DetailRecord(**key, row_no=1, lot="A1", ken="10", mai="5", tut="1",
                                    kz="07", kh="00", sz="15", sh="00", s="0", th="60")])
            repo.mark_synced((day, "L-1", "1直", 1))

    def test_保存した直の表を見出しで並べ替え_報告日は日の順(self) -> None:
        page = self.open_records()
        head = '#records-table th[data-sort="date"]'
        page.click(f"{head} button")
        self.assertEqual(page.get_attribute(head, "aria-sort"), "ascending")
        self.assertEqual(self.dates(page), ["2026年9月15日", "2026年9月16日", "2026年10月1日"],
                         "報告日が日の順に並びません")
        page.click(f"{head} button")
        self.assertEqual(page.get_attribute(head, "aria-sort"), "descending")
        self.assertEqual(self.dates(page), ["2026年10月1日", "2026年9月16日", "2026年9月15日"])
        self.assertEqual(self.errors, [])


class BackfillTests(RecordsBrowserCase):
    def test_後から作るはまず確かめるだけを送り_訊いてから作って開く(self) -> None:
        from datetime import datetime, timedelta

        from app.routes.entry import build_shift_calculator
        from nippou.services.nippou_service import format_business_date

        calc = build_shift_calculator(self.repo().get_shift_times())
        yesterday = format_business_date(calc.today_check(datetime.now(), False) - timedelta(days=1))
        self.admin()
        page = self.open_records()
        page.fill("#p-date", yesterday)
        page.select_option("#p-shift", "1直")
        page.fill("#backfill-reason", "打ち忘れ")
        mark = len(self.sent)
        page.click("#backfill-here")
        page.wait_for_function("location.pathname === '/'", timeout=8000)
        calls = [b for p, b in self.sent[mark:] if p == "/api/settings/backfill"]
        self.assertEqual(len(calls), 2, calls)
        self.assertIs(calls[0].get("dry_run"), True, "確かめずにいきなり作っています")
        self.assertNotIn("dry_run", calls[1])
        self.assertEqual((calls[1]["report_date"], calls[1]["shift"], calls[1]["note"]),
                         (yesterday, "1直", "打ち忘れ"))
        self.assertEqual(len(self.dialogs), 1)
        self.assertIn("「後日作成」の印が残ります", self.dialogs[0])
        page.wait_for_function(
            f"document.getElementById('sheet-key')?.dataset.openedDate === {yesterday!r}", timeout=8000)


class RecallPathTests(SavedLineCase, RecordsBrowserCase):
    """ほかのラインの直の「呼び出す」は、**サーバが行ごとに決めた道**(控えから入れて開く)で頼む。"""

    def test_ほかのラインの行はサーバが決めた控えの道で呼び出す(self) -> None:
        self.administrator()
        self.lb.flush(self.repo(), force=True)            # HVC の控えもある
        page = self.open_records()
        page.select_option("#saved-line", "HVC")
        page.wait_for_function(
            "document.querySelector('#records-table tbody [data-recall]')?.dataset.line === 'HVC'",
            timeout=5000)
        want = page.get_attribute("#records-table tbody [data-recall]", "data-recall-path")
        self.assertEqual(want, "/api/records/backup/open")
        mark = len(self.sent)
        page.click("#records-table tbody [data-recall]")
        page.wait_for_function("location.pathname === '/'", timeout=8000)
        asked = [p for p in self.paths(mark) if p in ("/api/records/backup/open", "/api/settings/recall")]
        self.assertEqual(asked, [want], "行ごとの道ではなく、いつもの呼び出しで頼んでいます")


class WorkerModeTests(ScreenBrowserCase):
    def setUp(self) -> None:
        super().setUp()
        from tests.test_standard_time import TEAMS

        patcher = patch("nippou.services.standard_time.load_teams", return_value=dict(TEAMS))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_作業者名で見るを入れたときだけ作業者ごとを選べ_切ったら班ごとへ戻す(self) -> None:
        self.settle_review()
        page = self.open_screen("/standard-time", "#worker-mode")
        option = '#standard-group option[value="worker"]'
        shown = f"(() => {{ const o = document.querySelector('{option}'); return !o.hidden && !o.disabled; }})()"
        self.assertFalse(page.evaluate(shown), "普段から作業者ごとが選べます")
        page.check("#worker-mode")
        self.assertTrue(page.evaluate(shown), "作業者名で見るを入れても選べません")
        page.select_option("#standard-group", "worker")
        page.uncheck("#worker-mode")
        self.assertFalse(page.evaluate(shown))
        self.assertEqual(page.input_value("#standard-group"), "team", "切っても作業者ごとのままです")
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    import unittest

    unittest.main()
