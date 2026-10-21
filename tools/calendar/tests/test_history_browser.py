"""履歴表示を**本物のブラウザ**で ── 読むだけで、現行のカレンダーと見た目を変える (段階1)

前は ``history.js`` に書き込む API の字が無いこと・``history.html`` にボタンの字が無いこと・
``components.css`` の差し替えの字を数えていた。ここでは画面を開いて押して回り、

- 日を押すと中身は読めるが、**書き込む要求は1本も出ない**。休み・削除・コメントのボタンも出ない
  (同じ押し方で、カレンダー画面には出ることも見て、見落としでないことを確かめる)
- 文字でも「履歴・閲覧のみ」と出す(色だけで運ばない)
- マス目の色は**履歴の色**で、カレンダー画面の色と違う。明暗どちらでも
"""
from __future__ import annotations

import datetime as _dt
import unittest

from calendar_app import config, repository
from calendar_app import settings as user_settings
from calendar_app.presenters import calendar as cal

from tests._page_browser import PageBrowserCase

#: 見えているボタンの文字(1行目)。モーダルの中も含む
_BUTTONS = """() => [...document.querySelectorAll('button')]
    .filter((b) => b.offsetParent !== null)
    .map((b) => b.innerText.trim().split('\\n')[0].trim())"""

#: 平日・土曜・日祝のマスと曜日の見出しの背景色と、その画面で履歴の色の札が指す色
_CELL_COLORS = """() => {
    const out = {};
    for (const tone of ['', 'sat', 'holiday']) {
        const sel = tone ? `.day[data-tone="${tone}"][data-date]` : '.day[data-date]:not([data-tone="sat"]):not([data-tone="holiday"])';
        const cell = document.querySelector(sel);
        if (cell) out[tone || 'weekday'] = getComputedStyle(cell).backgroundColor;
    }
    const head = document.querySelector('.cal__head:not([data-tone])');
    if (head) out.head = getComputedStyle(head).backgroundColor;
    const probe = document.createElement('i');
    document.body.appendChild(probe);
    const paint = (name) => { probe.style.color = `var(${name})`; return getComputedStyle(probe).color; };
    out.hist = { weekday: paint('--hist-day-bg'), sat: paint('--hist-day-sat-bg'),
                 holiday: paint('--hist-day-sun-bg'), head: paint('--hist-head-bg') };
    probe.remove();
    return out;
}"""


def _months_ago(n: int) -> tuple[int, int]:
    today = _dt.date.today()
    return cal.shift_month(today.year, today.month, -n)


class HistoryBrowserTests(PageBrowserCase):
    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(user_settings.set_value, user_settings.KEY_THEME,
                        user_settings.THEME_AUTO)
        user_settings.set_theme("light")
        repo = repository.Repository(self.conn)
        for n, name in ((1, "山田太郎"), (2, "鈴木一郎")):
            year, month = _months_ago(n)
            repo.save_record(_dt.date(year, month, 10), config.KUBUN_REST,
                             name, "10", shift="1", group="A", line="コイル")
        year, month = _months_ago(1)
        self.day = f"{year:04d}/{month:02d}/10"

    def open_day(self, page, date: str) -> None:
        page.click(f'.day[data-date="{date}"]')
        page.wait_for_selector(".modal")

    def test_日を押して回っても書き込みの要求もボタンも出ない_文字でも閲覧のみと出す(self) -> None:
        # 見落としでないことの確かめ: カレンダー画面で同じ日の押し方をすると、書き込むボタンが出る
        today = _dt.date.today()
        cal_page = self.open("/calendar")
        cal_page.wait_for_selector(".day[data-date]")
        self.open_day(cal_page, f"{today.year:04d}/{today.month:02d}/{today.day:02d}")
        self.assertIn("休み", cal_page.evaluate(_BUTTONS))
        cal_page.context.close()
        self.sent.clear()

        page = self.open("/history")
        page.wait_for_selector(f'.day[data-date="{self.day}"]')
        self.assertIn("履歴・閲覧のみ", page.inner_text("#main"))

        # 日の中身は読める(閲覧のみの札つき)。ボタンは「閉じる」だけ
        self.open_day(page, self.day)
        page.wait_for_selector(".modal .viewer")
        self.assertIn("山田太郎", page.inner_text(".modal .viewer"))
        self.assertIn("閲覧のみ", page.inner_text(".modal"))
        self.assertEqual([t for t in page.evaluate("""() => [...document.querySelectorAll('.modal button')]
            .map((b) => b.innerText.trim())""") if t], ["閉じる"])
        page.click(".modal button:has-text('閉じる')")
        page.wait_for_selector(".modal", state="detached")

        # 前の月へ。そこでも日を押す
        before = page.inner_text("#his-title")
        page.click("#his-prev")
        page.wait_for_function("(t) => document.getElementById('his-title').textContent !== t", arg=before)
        year, month = _months_ago(2)
        self.open_day(page, f"{year:04d}/{month:02d}/10")
        page.wait_for_selector(".modal .viewer")
        self.assertIn("鈴木一郎", page.inner_text(".modal .viewer"))
        page.keyboard.press("Escape")
        page.wait_for_selector(".modal", state="detached")

        # 年月を選ぶ窓も開いて閉じる
        page.click("#his-pick")
        page.wait_for_selector(".modal")
        page.keyboard.press("Escape")
        page.wait_for_selector(".modal", state="detached")

        for label in ("休み", "削除", "コメント・連絡"):
            self.assertNotIn(label, page.evaluate(_BUTTONS), label)
        self.assertTrue(any(p.startswith("/api/history/day/") for _, p, _ in self.sent), "日を読みに行っていない")
        self.assertEqual(self.writes(), [], "履歴の画面から書き込む要求が出た")
        self.assertEqual(self.errors, [])

    def test_マス目は明暗どちらでも履歴の色で_カレンダー画面の色と違う(self) -> None:
        seen = {}
        for theme in ("light", "dark"):
            with self.subTest(theme=theme):
                user_settings.set_theme(theme)
                ctx = self.new_context(color_scheme="light")
                history = self.open("/history", ctx)
                history.wait_for_selector(".day[data-date]")
                mine = history.evaluate(_CELL_COLORS)
                current = self.open("/calendar", ctx)
                current.wait_for_selector(".day[data-date]")
                theirs = current.evaluate(_CELL_COLORS)
                ctx.close()
                for tone in ("weekday", "sat", "holiday", "head"):
                    if tone not in mine or tone not in theirs:
                        continue                      # その月にその曜日の色のマスが無い(祝日など)
                    self.assertEqual(mine[tone], mine["hist"][tone], tone)
                    self.assertNotEqual(mine[tone], theirs[tone], f"{tone}: カレンダー画面と同じ色")
                self.assertIn("weekday", mine)
                seen[theme] = mine["weekday"]
        # ダークの節に履歴の色が無いと、ダークでも明るい履歴の色のまま(読めない)
        self.assertNotEqual(seen["light"], seen["dark"])
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
