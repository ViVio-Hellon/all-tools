"""日報入力: 休憩の窓(①休憩の分・②何時から何時まで)と「時間を割り振る」を合わせて使う (v4.38.2)

【利用者から】

    窓で「① 休憩の分」「② 何時から何時まで」の整合性
    時間割り振りを合わせて使用した場合、不具合がないかのチェック

本物のブラウザで、窓で入れた休憩 → 割り振る、割り振る → 窓で別の休憩 → もう一度割り振る、の順に通し、
休憩が2回数えられないこと・もう打ってある休憩は動かさず足さないこと・窓が勝手に開き直らないこと・
合わなくなったら窓で知らせることを確かめます。
無い環境では飛ばします。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HAS_FLASK, SKIP_REASON  # noqa: E402
from tests import test_break_popup_browser as popup  # noqa: E402


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class BreakTimeSplitBrowserTests(popup.BreakPopupBrowserTests):
    pass


# 親の試験はここでは流さない(親のファイルで流れる)
for _name in [n for n in dir(popup.BreakPopupBrowserTests) if n.startswith("test_")]:
    setattr(BreakTimeSplitBrowserTests, _name, None)


def _minute(text: str) -> int:
    h, m = text.split(":")
    return int(h) * 60 + int(m)


def _hhmm(minute: int) -> str:
    minute %= 1440
    return f"{minute // 60:02d}:{minute % 60:02d}"


class _Cases:
    def lunch(self):
        """いまの直と、その決まった休憩(始まり, 終わり)。決まりが無い直なら飛ばす。"""
        from nippou import work_context
        from nippou.logic import time_split as ts
        from app.routes.entry import build_shift_calculator
        ctx = work_context.get_context()
        _day, _line, shift = ctx.current_key(build_shift_calculator(self.repo().get_shift_times()))
        if shift not in ts.BREAKS:
            self.skipTest(f"{shift} には休憩の決まりが無い")
        from nippou.services import shift_check
        start, _end = shift_check.bounds_of(self.repo().get_shift_times(), shift)
        return shift, ts.BREAKS[shift], start

    def open_split(self):
        from nippou import user_settings
        from nippou.config import KEY_TIME_SPLIT
        user_settings.save(KEY_TIME_SPLIT, True)
        page = self.open_entry()
        self.dialogs: list[str] = []
        page.on("dialog", lambda d: self.dialogs.append(d.message))
        for row in range(1, 5):
            for fam, value in (("MAI", "10"), ("TUT", "1")):
                page.fill(f"#{fam}{row}", value)
                page.dispatch_event(f"#{fam}{row}", "change")
        page.wait_for_timeout(500)
        return page

    def split(self, page) -> None:
        before = len(self.dialogs)
        page.click("#time-split")
        page.wait_for_function("document.getElementById('KZ1').value !== ''", timeout=8000)
        page.wait_for_timeout(1200)
        self.assertGreater(len(self.dialogs), before, "割り振る前に確かめる")

    def rest_rows(self, page) -> dict[int, list[tuple[str, str]]]:
        """行ごとの休憩「0」の (記号の欄, 分)。"""
        out: dict[int, list[tuple[str, str]]] = {}
        for row in range(1, 5):
            for code, minutes in (("S", "TH"), ("SS", "THS"), ("STH", "THT"), ("S4", "TH4"), ("S5", "TH5")):
                if page.input_value(f"#{code}{row}") == "0":
                    out.setdefault(row, []).append((code, page.input_value(f"#{minutes}{row}")))
        return out

    def test_窓で入れた休憩はその行に残して足さない(self) -> None:
        """休憩がすでに入っている場合は、時間がまたいでいるところに休憩は足さない(打った行に残す)。

        前(v4.38.1〜)は、窓で入れた休憩をまたぐ行へ移して 60分 1つにまとめていた。"""
        shift, (begin, end), start = self.lunch()
        page = self.open_split()
        # 1行目に、窓で決まった休憩を30分ずらして入れる
        typed = f"{_hhmm(_minute(begin) - 30)}〜{_hhmm(_minute(end) - 30)}"
        page.select_option("#S1", "0")
        self.assertTrue(self.break_open(page))
        page.fill("#break-minutes", "60")
        page.fill("#break-text", typed)
        page.click("#break-apply")
        page.wait_for_selector("#break-modal", state="hidden")
        page.wait_for_function("document.getElementById('break-1')?.value !== ''", timeout=8000)

        self.split(page)
        self.assertIn(f"{begin} の休憩はもう入っているので足しません(1行目の休憩「0」60分", self.dialogs[-1],
                      "足さないことを先に言う")
        self.assertNotIn("は飛ばします。", self.dialogs[-1])
        page.wait_for_function(
            "(t) => [...document.querySelectorAll('.toast__text')].some((x) => x.textContent.includes(t))",
            arg=f"{begin} の休憩はもう入っているので足しませんでした", timeout=8000)
        rests = self.rest_rows(page)
        self.assertEqual({r: [m for _c, m in got] for r, got in rests.items()}, {1: ["60"]},
                         "打った行に残り、ほかの行には足さない")
        from nippou.logic import break_time
        self.assertEqual(break_time.normalize(page.input_value("#break-1")), break_time.normalize(typed))
        self.assertEqual(page.locator("#break-rows .break__problem").count(), 0,
                         page.text_content("#break-rows"))
        self.assertFalse(page.is_visible("#break-modal"), "合っているので窓は出ない")
        # 休憩は1回だけ: 4行の作業時間の合計は 直の長さ − 60
        from nippou.services import shift_check
        s_text, e_text = shift_check.bounds_of(self.repo().get_shift_times(), shift)
        length = (_minute(e_text) - _minute(s_text)) % 1440
        tims = [float(page.input_value(f"#TIM{n}") or 0) for n in range(1, 5)]
        self.assertEqual(sum(tims), length - 60, tims)
        row = 1

        # そのあと表の分だけ 50 にすると、窓で知らせる
        page.fill(f"#TH{row}", "50")
        page.dispatch_event(f"#TH{row}", "change")
        page.locator(f"#TH{row}").blur()
        page.wait_for_function(
            "!document.getElementById('break-modal').hidden"
            " && document.getElementById('break-problems').textContent.includes('休憩は 50 分')",
            timeout=5000)
        self.assertEqual(self.errors, [])

    def test_割り振ったあと窓で別の休憩を足してもう一度割り振る(self) -> None:
        shift, (begin, end), start = self.lunch()
        page = self.open_split()
        self.split(page)
        rests = self.rest_rows(page)
        [(row, _got)] = rests.items()
        self.assertNotEqual(row, 1)
        page.wait_for_timeout(600)
        self.assertFalse(page.is_visible("#break-modal"), "割り振りが入れた休憩には時刻もあるので聞かない")

        # 1行目に別の休憩(直の始め +10〜+20 分)を窓で足す
        page.select_option("#S1", "0")
        self.assertTrue(self.break_open(page))
        self.assertEqual(page.evaluate("document.activeElement.id"), "break-minutes", "分から聞く")
        page.fill("#break-minutes", "10")
        page.fill("#break-text", f"{_hhmm(_minute(start) + 10)}〜{_hhmm(_minute(start) + 20)}")
        page.click("#break-apply")
        page.wait_for_selector("#break-modal", state="hidden")
        page.wait_for_timeout(800)
        self.assertFalse(page.is_visible("#break-modal"))

        self.split(page)
        self.assertIn("は短いので、休憩の時刻の休憩には数えません", self.dialogs[-1])
        self.assertIn(f"{begin} の休憩はもう入っているので足しません", self.dialogs[-1],
                      "1回目に足した休憩があるので、もう一度押しても足さない")
        rests = self.rest_rows(page)
        self.assertEqual([m for _c, m in rests.get(1, [])], ["10"], "別の休憩は残る")
        self.assertEqual([m for _c, m in rests.get(row, [])], ["60"])
        self.assertEqual(page.locator("#break-rows .break__problem").count(), 0,
                         page.text_content("#break-rows"))
        self.assertFalse(page.is_visible("#break-modal"))
        self.assertEqual(self.errors, [])


for _name, _fn in vars(_Cases).items():
    if _name.startswith("test_") or not _name.startswith("__"):
        setattr(BreakTimeSplitBrowserTests, _name, _fn)


if __name__ == "__main__":
    unittest.main()
