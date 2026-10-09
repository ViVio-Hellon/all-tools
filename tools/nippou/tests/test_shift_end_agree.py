"""直の終わりを決める 3 つの道が、**どの時刻でも同じ答えを出す**(安全網)

直の終わりは次の 3 か所で決めている。食い違うと「最も重く壊れる」(README v3.59.1):

- ``ShiftCalculator.minutes_until_shift_end`` … 催促・自動確定(いま時計が指す直)
- ``ShiftCalculator.shift_end_at``            … 画面の固定(名指しした直)
- ``presenters.push_log.shift_end``           … 押した記録(直内か直後か)

時間マスタの形を何通りか変えて、1 日の全部の分(1440 分)で 3 つを突き合わせる。
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic.shift import DAY_SHIFT  # noqa: E402
from nippou.presenters import push_log  # noqa: E402

#: 直と直の間に隙間が無く、3直が 0 時をまたぐ形(既定・ふつうの現場)。**全部の分で同じ答え**
MASTERS = {
    "既定(マスタ無し)": {},
    "重なりのある 3 交替": {"1": ("06:50", "15:00"), "2": ("14:50", "22:50"), "3": ("22:50", "07:00"),
                         "昼": ("08:00", "17:00")},
    "全角・片側だけ": {"1": ("０６:５０", "１５:００"), "2": ("14:50", ""), "3": ("22:50", "07:00")},
}

#: 直と直の間に隙間がある・3直が 0:00 に始まるマスタでは、隙間の時刻を time_check は「3直」、
#: today_check は「今日」と読むため、時計で決める終わり(今日の終わり)と名指しで決める終わり
#: (翌日の終わり)が食い違う。**決めたこと(2026/10/09): 現場の時間マスタに隙間は無い。
#: 隙間ができる登録はマスタ管理で断る**(master_admin._shift_gap_problem)。Access で直に
#: 入れられたときは帯で知らせる(shift_times_note)。いまの食い違いの分数は固定しておく。
KNOWN_GAPS = {
    "直の間に隙間(3直 06:30 まで・1直 07:00 から)": (
        {"1": ("07:00", "15:00"), "2": ("15:30", "22:00"), "3": ("22:30", "06:30")}, ("06:31", "07:00"), 30),
    "3直が 0:00 に始まる": (
        {"1": ("08:00", "16:00"), "2": ("16:00", "23:59"), "3": ("00:00", "08:00")}, ("23:59", "23:59"), 1),
}


def _calculator(raw):
    from app.routes.entry import build_shift_calculator

    return build_shift_calculator(raw)


class ShiftEndAgreeTests(unittest.TestCase):
    def test_3つの道がどの分でも同じ終わりを出す(self) -> None:
        base = datetime(2026, 10, 8)
        for name, raw in MASTERS.items():
            calc = _calculator(raw)
            wrong = []
            for minute in range(24 * 60):
                now = base + timedelta(minutes=minute)
                shift = calc.time_check(now)
                day = calc.today_check(now)
                by_clock = now + timedelta(minutes=calc.minutes_until_shift_end(now))
                named = calc.shift_end_at(shift, day)
                logged = push_log.shift_end(day, shift, raw)
                if not (by_clock == named == logged):
                    wrong.append((now.strftime("%H:%M"), shift, by_clock, named, logged))
            with self.subTest(master=name):
                self.assertEqual(wrong[:5], [], f"{name}: {len(wrong)} 分で食い違う")

    def test_隙間のあるマスタの食い違いは今のまま(self) -> None:
        base = datetime(2026, 10, 8)
        for name, (raw, (first, last), count) in KNOWN_GAPS.items():
            calc = _calculator(raw)
            wrong = []
            for minute in range(24 * 60):
                now = base + timedelta(minutes=minute)
                shift = calc.time_check(now)
                day = calc.today_check(now)
                by_clock = now + timedelta(minutes=calc.minutes_until_shift_end(now))
                if not (by_clock == calc.shift_end_at(shift, day) == push_log.shift_end(day, shift, raw)):
                    wrong.append(now.strftime("%H:%M"))
            with self.subTest(master=name):
                self.assertEqual((wrong[0], wrong[-1], len(wrong)), (first, last, count))

    def test_日勤も同じ(self) -> None:
        base = datetime(2026, 10, 8)
        for name, raw in MASTERS.items():
            calc = _calculator(raw)
            for minute in range(0, 24 * 60, 7):
                now = base + timedelta(minutes=minute)
                by_clock = now + timedelta(minutes=calc.minutes_until_shift_end(now, force_day_shift=True))
                with self.subTest(master=name, at=now.strftime("%H:%M")):
                    self.assertEqual(by_clock, calc.shift_end_at(DAY_SHIFT, now.date()))
                    self.assertEqual(by_clock, push_log.shift_end(now.date(), DAY_SHIFT, raw))


class MasterReadersAgreeTests(unittest.TestCase):
    """時間マスタを読むところ(画面の計算機・保存前チェック・過去日報の取り込み・全停入力)が
    **同じ読み方**をする(logic/shift.times_from_master)。以前は過去日報の取り込みが始まりだけを
    独自に読んでいて、全角は控えに落ち、片側だけの値はそのまま使っていた。"""

    def test_読む場所が同じ時刻を出す(self) -> None:
        from app.routes import staff
        from nippou.logic import nippou_sheet
        from nippou.logic.shift import SHIFT_1, SHIFT_2, SHIFT_3, normalize_hhmm
        from nippou.services.shift_check import bounds_of

        for name, raw in list(MASTERS.items()) + [(k, v[0]) for k, v in KNOWN_GAPS.items()]:
            calc = _calculator(raw)
            starts = nippou_sheet.shift_starts(raw)
            for shift in (SHIFT_1, SHIFT_2, SHIFT_3, DAY_SHIFT):
                with self.subTest(master=name, shift=shift):
                    start_t, end_t = calc.times.bounds(shift)
                    start, end = bounds_of(raw, shift)
                    self.assertEqual((start, end), (start_t.strftime("%H:%M"), end_t.strftime("%H:%M")))
                    self.assertEqual(staff._parse_hhmm(start), start_t)
                    if shift != DAY_SHIFT:
                        self.assertEqual(starts[shift], start_t.hour * 60 + start_t.minute)

    def test_全角と片側だけ(self) -> None:
        from app.routes import staff
        from nippou.logic import nippou_sheet

        raw = {"1": ("０６:５０", "１５:００"), "2": ("14:50", "")}
        starts = nippou_sheet.shift_starts(raw)
        self.assertEqual(starts["1直"], 6 * 60 + 50, "全角を控えに落とした")
        self.assertEqual(starts["2直"], 15 * 60, "片側だけの値を使った(ほかの画面は両方とも控えに落とす)")
        self.assertEqual(staff._parse_hhmm("２２:５０").hour, 22)


if __name__ == "__main__":
    unittest.main()
