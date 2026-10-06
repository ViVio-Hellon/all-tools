"""前の直に一切の入力が無いときの警告

【なぜ要るのか】
直の終わりの催促も、残り5分の自動確定も、**画面が開いていなければ
動きません**。VBA も同じで、フォームを閉じていた直はタイマーが一度も
回りませんでした。しかも「一行も打たなかった」場合は催促する相手すら
居ません ── タイマーはその直の中でしか効かない、ということです。

抜けたぶんは次の直が気づくしかない。そこで:

    **前の直に1ページも保存が無ければ、次の直の人に一度だけ知らせる。**

ここで守るのは:

    ・前の直がどれかを間違えないこと(3直は昨日の日付)
    ・**一度だけ**であること(毎回出すと読まずに閉じる癖がつく)
    ・**空振りさせない**こと(回していない直の「空」は知らせない)
"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase


def evaluate(**values):
    from nippou.logic import prev_shift

    base = dict(report_date="2026年8月3日", line="L-1", shift="1直",
                prev_report_date="2026年8月2日", prev_shift="3直",
                prev_has_data=False, seen_recently=True)
    base.update(values)
    return prev_shift.evaluate(**base)


class PreviousOfTests(unittest.TestCase):
    """どれが「前の直」か。**3直は昨日の日付。**"""

    def setUp(self) -> None:
        from nippou.logic import prev_shift

        self.prev = prev_shift

    def test_1直の前は昨日の3直(self) -> None:
        """3直は 22:00→翌08:00 で、報告日は始めた日(`TodayCheck`)。"""
        self.assertEqual(self.prev.previous_of("1直", date(2026, 8, 3)),
                         ("3直", date(2026, 8, 2)))

    def test_2直の前は同じ日の1直(self) -> None:
        self.assertEqual(self.prev.previous_of("2直", date(2026, 8, 3)),
                         ("1直", date(2026, 8, 3)))

    def test_3直の前は同じ日の2直(self) -> None:
        self.assertEqual(self.prev.previous_of("3直", date(2026, 8, 3)),
                         ("2直", date(2026, 8, 3)))

    def test_日勤の前も昨日の3直(self) -> None:
        """日勤は1直・2直の代わりに置く直。"""
        self.assertEqual(self.prev.previous_of("日勤", date(2026, 8, 3)),
                         ("3直", date(2026, 8, 2)))

    def test_月をまたぐ(self) -> None:
        self.assertEqual(self.prev.previous_of("1直", date(2026, 9, 1)),
                         ("3直", date(2026, 8, 31)))

    def test_知らない直はNone(self) -> None:
        self.assertIsNone(self.prev.previous_of("4直", date(2026, 8, 3)))


class WarnTests(unittest.TestCase):
    """出す・出さないの判断。"""

    def setUp(self) -> None:
        from nippou.logic import prev_shift

        prev_shift.reset()
        self.addCleanup(prev_shift.reset)

    def test_前の直が空なら知らせる(self) -> None:
        found = evaluate()
        self.assertTrue(found.warn)
        self.assertIn("2026年8月2日 3直", found.message)
        self.assertIn("1ページもありません", found.message)

    def test_前の直に保存があれば黙る(self) -> None:
        self.assertFalse(evaluate(prev_has_data=True).warn)

    def test_回していない直なら黙る(self) -> None:
        """**狼少年にしない。** 3直の無いラインで毎朝出さない。"""
        self.assertFalse(evaluate(seen_recently=False).warn)

    def test_呼出モード中は黙る(self) -> None:
        """過去のデータを直している最中。「前の直」の話が当てはまらない。"""
        self.assertFalse(evaluate(recall_mode=True).warn)

    def test_入れ直せることを書く(self) -> None:
        """**分かっただけで終わらせない。** 丸ごと抜けた直も後から作れる
        (v4.0.0。前は「呼び出せば」と案内していたが、保存の無い直は呼び出せない)。"""
        found = evaluate()
        self.assertIn("後から作れます", found.reason)
        self.assertIn("記録を見る", found.reason)
        self.assertIn("この直を後から作る", found.reason)

    def test_文言に飾り記号を入れない(self) -> None:
        """そのまま画面に出る文字列。`**` は太字にならず、そう見える。"""
        found = evaluate()
        for text in (found.message, found.reason):
            self.assertNotIn("**", text)

    def test_返す形に必要なものがそろう(self) -> None:
        body = evaluate().as_dict()
        for key in ("warn", "report_date", "shift", "line", "message",
                    "reason"):
            self.assertIn(key, body)


class OnceTests(unittest.TestCase):
    """**一度だけ。** 毎回出すと読まずに閉じる癖がつく。"""

    def setUp(self) -> None:
        from nippou.logic import prev_shift

        self.prev = prev_shift
        prev_shift.reset()
        self.addCleanup(prev_shift.reset)

    def test_知らせたら次からは黙る(self) -> None:
        self.assertTrue(evaluate().warn)
        self.prev.gate().mark("2026年8月3日", "L-1", "1直")
        self.assertFalse(evaluate().warn)

    def test_直が変われば忘れる(self) -> None:
        self.prev.gate().mark("2026年8月3日", "L-1", "1直")
        # 同じ日の2直は別の鍵
        self.assertTrue(evaluate(shift="2直", prev_shift="1直",
                                 prev_report_date="2026年8月3日").warn)

    def test_ラインが変われば別に数える(self) -> None:
        self.prev.gate().mark("2026年8月3日", "L-1", "1直")
        self.assertTrue(evaluate(line="HVC").warn)

    def test_日が変われば忘れる(self) -> None:
        self.prev.gate().mark("2026年8月3日", "L-1", "1直")
        self.assertTrue(evaluate(report_date="2026年8月4日",
                                 prev_report_date="2026年8月3日").warn)


# ======================================================================
# 画面 (Flask が要る)
# ======================================================================
@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class ScreenTests(WebTestCase):

    def setUp(self) -> None:
        super().setUp()
        from nippou.logic import prev_shift

        prev_shift.reset()
        self.addCleanup(prev_shift.reset)

    def _current(self):
        from nippou import work_context

        from app.routes.entry import build_shift_calculator

        ctx = work_context.get_context()
        calc = build_shift_calculator(self.repo().get_shift_times())
        return ctx.current_key(calc), ctx.business_date(calc)

    def _previous(self):
        from nippou.logic import prev_shift
        from nippou.services.nippou_service import format_business_date

        (_day, line, shift), business = self._current()
        found = prev_shift.previous_of(shift, business)
        prev_name, prev_date = found
        return format_business_date(prev_date), line, prev_name

    def save(self, day, line, shift) -> None:
        from nippou.db.models import DetailRecord, HeaderRecord

        self.repo().save(
            HeaderRecord(report_date=day, line=line, shift=shift, page=1,
                         worker="山田"),
            [DetailRecord(report_date=day, line=line, shift=shift, page=1,
                          row_no=1, lot="A1234", wei="1000", con="10")])

    def test_回していない直なら出ない(self) -> None:
        """まっさらな端末では、前の直の実績も無い。"""
        html = self.get("/").get_data(as_text=True)
        self.assertIn('id="prev-shift"', html)     # 枠はある
        self.assertIn("hidden", html.split('id="prev-shift"')[1][:120])

    def test_前の直が空なら出る(self) -> None:
        """その直を回している(最近の実績がある)のに、空。"""
        from datetime import timedelta

        (_day, line, shift), business = self._current()
        prev_day, _line, prev_name = self._previous()
        # 1週間前に同じ直の実績がある = そのラインはその直を回している
        from nippou.services.nippou_service import format_business_date
        self.save(format_business_date(business - timedelta(days=7)),
                  line, prev_name)

        html = self.get("/").get_data(as_text=True)
        block = html.split('id="prev-shift"')[1][:400]
        self.assertNotIn("hidden", block)
        self.assertIn("1ページもありません", html)
        self.assertIn(prev_day, html)

    def test_前の直に保存があれば出ない(self) -> None:
        from datetime import timedelta

        from nippou.services.nippou_service import format_business_date

        (_day, line, shift), business = self._current()
        prev_day, _line, prev_name = self._previous()
        self.save(format_business_date(business - timedelta(days=7)),
                  line, prev_name)
        self.save(prev_day, line, prev_name)       # 前の直にも入っている

        html = self.get("/").get_data(as_text=True)
        self.assertIn("hidden", html.split('id="prev-shift"')[1][:120])

    def test_分かりましたで止まる(self) -> None:
        from datetime import timedelta

        from nippou.services.nippou_service import format_business_date

        (_day, line, shift), business = self._current()
        _prev_day, _line, prev_name = self._previous()
        self.save(format_business_date(business - timedelta(days=7)),
                  line, prev_name)

        self.assertNotIn(
            "hidden", self.get("/").get_data(as_text=True)
            .split('id="prev-shift"')[1][:400])
        res = self.post("/api/entry/prev-shift-ack")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["acknowledged"])
        self.assertIn(
            "hidden", self.get("/").get_data(as_text=True)
            .split('id="prev-shift"')[1][:120])

    def test_入力のたびに状態が返る(self) -> None:
        body = self.post("/api/entry/state", {"rows": {}, "header": {}}).get_json()
        self.assertIn("prev_shift", body)
        self.assertIn("warn", body["prev_shift"])

    def test_組み立てに失敗しても画面は出る(self) -> None:
        from unittest.mock import patch

        with patch("nippou.logic.prev_shift.previous_of",
                   side_effect=RuntimeError("想定外")):
            res = self.get("/")
        self.assertEqual(res.status_code, 200)


if __name__ == "__main__":
    unittest.main()
