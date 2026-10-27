"""通しの試験: 休憩(窓で入れる分と時刻)と「時間を割り振る」を合わせて使う ── 1直・2直・3直 (v4.38.2)

【利用者から】

    窓で「① 休憩の分」「② 何時から何時まで」の整合性
    時間割り振りを合わせて使用した場合、不具合がないかのチェック

止めた時計で各直を開き、LOT を打った4行に休憩をいろいろな形で入れてから割り振り、
(1) 休憩が2回数えられない(作業時間の合計 = 直の長さ − 休憩)
(2) 休憩の分と時刻が合っている(下の一覧に直すところが無い)
(3) もう一度割り振っても同じ
(4) 保存(確定)が通る
を確かめます。もう打ってある休憩(窓で入れた・時刻を書かない)は打った行に残してその時刻には足さず、
決まった休憩とは別の短い休憩(10分休憩・打ちかけの 1分)は残したうえで60分を足し、
またぐ行の停止①〜⑤が埋まっていれば割り振らずに断ります。
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import break_time, time_split as ts  # noqa: E402
from tests import test_scenario_break_overrun as base  # noqa: E402
from tests._web import HAS_FLASK, SKIP_REASON  # noqa: E402

FAMILIES = ("KZ", "KH", "SZ", "SH", "S", "TH", "SS", "THS", "STH", "THT", "S4", "TH4", "S5", "TH5",
            "break_range", "TIM")
FULL = dict(S="イ", TH="1", SS="ロ", THS="1", STH="ハ", THT="1", S4="ニ", TH4="1", S5="ホ", TH5="1")


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class BreakTimeSplitScenarioTests(base.BreakOverrunScenarioTests):
    pass


for _name in [n for n in dir(base.BreakOverrunScenarioTests) if n.startswith("test_")]:
    setattr(BreakTimeSplitScenarioTests, _name, None)


def _open(self, shift: str, extra: dict) -> tuple[dict, int]:
    from nippou import user_settings
    from nippou.config import KEY_TIME_SPLIT
    user_settings.save(KEY_TIME_SPLIT, True)
    start, length = base.SHIFTS[shift]
    begin = datetime.strptime(f"2026-10-05 {start}", "%Y-%m-%d %H:%M")
    body = self.start_shift((begin + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M"), base.WORKER)
    self.assertEqual(body["shift"], shift, body.get("message"))
    rows = self.typed({str(n): {"LOT": self.lots[n]} for n in range(1, 5)})
    for n, values in extra.items():
        rows[str(n)].update(values)
    self.at((begin + timedelta(minutes=length - 3)).strftime("%Y-%m-%d %H:%M"))
    return rows, length


def _split(self, rows: dict):
    return self.post("/api/entry/distribute-times", self.sheet(rows, base.WORKER))


def _rests(rows: dict) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for n in range(1, 5):
        for code, minutes in ts.STOP_SLOTS:
            if rows[str(n)].get(code) == "0":
                out.setdefault(n, []).append(rows[str(n)][minutes])
    return out


def _check(self, shift: str, extra: dict, rest_total: int, *, label: str) -> dict:
    """割り振って (1)〜(4) を確かめる。割り振ったあとの応答を返す。"""
    rows, length = _open(self, shift, extra)
    dry = self.post("/api/entry/distribute-times", self.sheet(rows, base.WORKER, dry_run=True)).get_json()
    res = _split(self, rows)
    self.assertEqual(res.status_code, 200, f"{shift} {label}: {res.get_json()}")
    body = res.get_json()
    out = body["rows"]
    tims = sum(Decimal(out[str(n)]["TIM"] or 0) for n in range(1, 5))
    self.assertEqual(tims, length - rest_total, f"{shift} {label}: 休憩は {rest_total} 分だけ引く {out}")
    self.assertEqual({e["row"]: e["problems"] for e in body["break_entries"] if e["problems"]}, {},
                     f"{shift} {label}: 休憩の分と時刻が合っている")
    again = _split(self, out).get_json()["rows"]
    self.assertEqual({n: {f: again[str(n)].get(f) for f in FAMILIES} for n in range(1, 5)},
                     {n: {f: out[str(n)].get(f) for f in FAMILIES} for n in range(1, 5)},
                     f"{shift} {label}: もう一度割り振っても同じ")
    saved = self.save(out, base.WORKER)
    self.assertEqual(saved.status_code, 200, f"{shift} {label}: {self.codes_of(saved)}")
    body["dry"] = dry
    return body


def _make(shift: str):
    lunch = ts.BREAKS[shift]
    window = break_time.normalize("〜".join(lunch))
    start, _length = base.SHIFTS[shift]
    s0 = base.datetime.strptime(start, "%H:%M")
    early = f"{(s0 + timedelta(minutes=10)):%H:%M}〜{(s0 + timedelta(minutes=20)):%H:%M}"
    shifted = (f"{(base.datetime.strptime(lunch[0], '%H:%M') - timedelta(minutes=30)):%H:%M}〜"
               f"{(base.datetime.strptime(lunch[1], '%H:%M') - timedelta(minutes=30)):%H:%M}")

    def test_休憩なしで割り振るとまたぐ行に0と時刻(self) -> None:
        body = _check(self, shift, {}, 60, label="休憩なし")
        rests = _rests(body["rows"])
        [(row, minutes)] = rests.items()
        self.assertEqual(minutes, ["60"])
        self.assertEqual(body["rows"][str(row)]["break_range"], window)

    def test_窓で入れた休憩はその行に残して足さない(self) -> None:
        # 休憩がすでに入っている場合は、またいでいるところに休憩は足さない。窓で30分ずらして入れた
        # 休憩もその時刻の休憩。打った行に残し、その行がその時刻をまたぐように割り振る
        body = _check(self, shift, {1: {"S": "0", "TH": "60", "break_range": shifted}}, 60, label="窓で入れた")
        self.assertIsNone(body["dry"]["rest"], "足さない")
        self.assertIn(f"{lunch[0]} の休憩はもう入っているので足しません(1行目の休憩「0」60分",
                      "\n".join(body["dry"]["break_notes"]))
        self.assertEqual(_rests(body["rows"]), {1: ["60"]})
        self.assertEqual(break_time.normalize(body["rows"]["1"]["break_range"]), break_time.normalize(shifted))

    def test_時刻を書かない休憩もその行に残して足さない(self) -> None:
        body = _check(self, shift, {2: {"S": "0", "TH": "60"}}, 60, label="時刻なし")
        self.assertIsNone(body["dry"]["rest"])
        self.assertEqual(_rests(body["rows"]), {2: ["60"]})
        self.assertEqual(body["rows"]["2"]["break_range"], "")

    def test_打ちかけの短い休憩は数えず60分を足す(self) -> None:
        # 1分の「0」はその時刻の休憩ではない(休憩の長さの半分より短い)。60分を足し、1分は打った行に残す
        body = _check(self, shift, {1: {"S": "0", "TH": "1"}}, 61, label="1分")
        rests = _rests(body["rows"])
        self.assertEqual(rests[1], ["1"])
        self.assertEqual(sorted(rests.values()), [["1"], ["60"]])

    def test_直の外の時刻の休憩も足さず打った行に残す(self) -> None:
        # 直の時間ではない時刻(ありえない)。時刻の無い休憩として数え、その時刻には足さない。
        # 打った時刻は消さない(保存の確かめが「直の外」と言うので、そこで直してもらう)
        wrong = "11:30〜12:30" if shift != "1直" else "19:00〜20:00"
        rows, _length = _open(self, shift, {1: {"S": "0", "TH": "60", "break_range": wrong}})
        body = _split(self, rows).get_json()
        self.assertEqual(_rests(body["rows"]), {1: ["60"]})
        self.assertEqual(body["rows"]["1"]["break_range"], wrong)
        self.assertIn("足しませんでした", body["message"])

    def test_別の休憩は残し決まった休憩と並べる(self) -> None:
        body = _check(self, shift, {1: {"S": "0", "TH": "10", "break_range": early}}, 70, label="別の休憩")
        self.assertIn("は短いので、休憩の時刻の休憩には数えません(そのまま残します)",
                      "\n".join(body["dry"]["break_notes"]))
        rests = _rests(body["rows"])
        self.assertEqual(rests[1], ["10"])
        self.assertEqual(sorted(rests.values()), [["10"], ["60"]])

    def test_ほかの停止と休憩が同じ行でも数え違えない(self) -> None:
        _check(self, shift, {2: {"S": "イ", "TH": "20", "SS": "0", "THS": "60", "break_range": window}},
               80, label="停止と休憩")

    def test_またぐ行の停止が埋まっていれば断る(self) -> None:
        rows, _length = _open(self, shift, {n: dict(FULL) for n in (1, 2, 3, 4)})
        res = _split(self, rows)
        self.assertEqual(res.status_code, 422)
        self.assertIn("停止①〜⑤が埋まっていて", res.get_json()["error"]["message"])

    return {n: f for n, f in locals().items() if n.startswith("test_")}


for _shift in ("1直", "2直", "3直"):
    for _name, _fn in _make(_shift).items():
        setattr(BreakTimeSplitScenarioTests, f"{_name}_{_shift}", _fn)


if __name__ == "__main__":
    unittest.main()
