"""直ひと回りの動線 ── **いまどこで、次に何をするか**

画面の名前も置き場所もそろえましたが、**順番が画面に出ていません**
でした。日報入力の帯は

    作業者 → 日報を入力 → 保存(確定)

までで終わっていて、保存したあとに何が残っているかを誰も言わない。
直の終わりの「確かめる」も「共有へ保存」も、覚えている人だけがやる
作業になっていました(VBA も同じで、印刷ボタンを押した人だけが
`ExecutePrintProcess` の中でチェックを通っていた)。

ここで守るのは:

    ・帯が**保存で終わらない**(確かめる・共有へ保存まで1本)
    ・**同じ帯が3つの画面に出る**(行った先で順番が途切れない)
    ・いまやることが**1つだけ**立ち、そこへ行く道が付く
    ・**直の途中で締めを急かさない**(9時間ある直の09:00に
      「次は確かめる」と出すのは案内として嘘)
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase

DAY = "2026年8月3日"
LINE = "L-1"
SHIFT = "1直"


class FlowTestCase(unittest.TestCase):
    """一時DBだけ。Flask は要らない(`presenters/flow.py` は素のロジック)。"""

    def setUp(self) -> None:
        from nippou.db.connection import connect
        from nippou.db.repository import NippouRepository
        from nippou.db.schema import ensure_schema

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        conn = connect(Path(self._tmp.name) / "t.sqlite3")
        ensure_schema(conn)
        self.addCleanup(conn.close)
        self.repo = NippouRepository(conn)

    def save(self, page: int = 1, **values) -> None:
        from nippou.db.models import DetailRecord, HeaderRecord

        row = dict(report_date=DAY, line=LINE, shift=SHIFT, page=page,
                   row_no=1, lot="A1234", ken="10", mai="10", tut="1",
                   kz="8", kh="0", sz="9", sh="0", tim="60",
                   wei="1000", con="10")
        row.update(values)
        self.repo.save(
            HeaderRecord(report_date=DAY, line=LINE, shift=SHIFT, page=page,
                         worker="山田"),
            [DetailRecord(**row)])

    def build(self, **kwargs):
        from nippou.presenters import flow

        kwargs.setdefault("minutes_left", 5)      # 既定は終わり間際で見る
        return flow.build(self.repo, DAY, LINE, SHIFT, **kwargs)

    def step(self, built, key):
        return next(s for s in built.steps if s.key == key)


class OrderTests(FlowTestCase):
    """帯の並びと、いまどこか。"""

    def test_保存で終わらない(self) -> None:
        """**ここが本題。** 保存のあとに2つ残っている。"""
        keys = [s.key for s in self.build().steps]
        self.assertEqual(
            keys, ["worker", "rows", "save", "verify", "push"])

    def test_何も打っていなければ作業者が次(self) -> None:
        built = self.build()
        self.assertEqual(built.current.key, "worker")
        self.assertIn("作業者", built.headline)

    def test_作業者を入れたら入力が次(self) -> None:
        built = self.build(worker="山田", rows_used=0)
        self.assertEqual(built.current.key, "rows")

    def test_打ちかけでも帯が進む(self) -> None:
        """保存前の画面の値で前半を判定する。"""
        built = self.build(worker="山田", rows_used=3)
        self.assertTrue(self.step(built, "rows").done)
        # まだ保存していないので、保存は済んでいない
        self.assertFalse(self.step(built, "save").done)
        self.assertEqual(built.current.key, "save")

    def test_保存したら確かめるが次(self) -> None:
        self.save()
        built = self.build()
        self.assertTrue(self.step(built, "save").done)
        self.assertEqual(built.current.key, "verify")

    def test_いまやることは1つだけ(self) -> None:
        self.save()
        built = self.build()
        self.assertEqual(sum(1 for s in built.steps if s.current), 1)

    def test_ラインは帯に入れない(self) -> None:
        """**直ひと回りの作業ではない。** 据え付けのときに1度決めるもの。

        ここに並んでいると「毎回まず選ぶもの」に見えて、押し間違えた
        まま打ち始められます ── ラインが違えば保存先のキーごと変わる
        ので、気づくのは翌日の集計です。いま何なのかは上の帯に出て
        いて、変えるのは設定・管理者(管理者モードが要る)。
        """
        self.assertNotIn("line", [s.key for s in self.build().steps])


class TimingTests(FlowTestCase):
    """**直の途中で締めを急かさない。**"""

    def test_途中なら確かめるを次と言わない(self) -> None:
        self.save()
        built = self.build(minutes_left=240)
        self.assertIsNone(built.current)
        self.assertIn("作業を続けて", built.headline)

    def test_途中は直の終わりにと書く(self) -> None:
        self.save()
        built = self.build(minutes_left=240)
        for key in ("verify", "push"):
            self.assertIn("直の終わりに", self.step(built, key).note, key)
            self.assertIn("240分", self.step(built, key).note, key)

    def test_終わり間際なら確かめるが次(self) -> None:
        self.save()
        built = self.build(minutes_left=10)
        self.assertEqual(built.current.key, "verify")
        self.assertIn("あと10分", built.headline)

    def test_境目は催促と同じ15分(self) -> None:
        self.save()
        self.assertIsNone(self.build(minutes_left=16).current)
        self.assertIsNotNone(self.build(minutes_left=15).current)

    def test_時計が読めなくても帯は出る(self) -> None:
        """残り時間を渡さなければ、順番どおりに出す。"""
        self.save()
        built = self.build(minutes_left=None)
        self.assertEqual(built.current.key, "verify")

    def test_途中でも前半の抜けは次と言う(self) -> None:
        """**打っていないことは、いつでも言う。**"""
        built = self.build(minutes_left=240)
        self.assertEqual(built.current.key, "worker")


class NoteTests(FlowTestCase):
    """一言の中身。**数で言う。**"""

    def test_保存したページの数が出る(self) -> None:
        self.save(page=1)
        self.save(page=2)
        self.assertEqual(self.step(self.build(), "save").note, "2ページ 保存済み")

    def test_指摘の件数が出る(self) -> None:
        # 梱包数(10)が検入枚数(1)を超えている
        self.save(ken="1", mai="10", tut="10")
        note = self.step(self.build(), "verify").note
        self.assertRegex(note, r"直すところが\d+件")

    def test_未保存のページ数が出る(self) -> None:
        self.save(page=1)
        self.save(page=2)
        self.assertEqual(self.step(self.build(), "push").note, "未保存 2ページ")

    def test_共有へ渡したら済みになる(self) -> None:
        self.save()
        self.repo.mark_synced((DAY, LINE, SHIFT, 1))
        built = self.build()
        self.assertTrue(self.step(built, "push").done)
        self.assertEqual(self.step(built, "push").note, "渡し済み")

    def test_紙は動線に入れない(self) -> None:
        """**要るときだけ**で、順番のどこでもない。"""
        built = self.build()
        self.assertNotIn("print", [s.key for s in built.steps])
        self.assertIn("印刷は要るときだけ", built.paper_note)


class RobustTests(FlowTestCase):
    """**帯は出す。** 数えられないものがあっても止めない。"""

    def test_チェックが落ちても帯は出る(self) -> None:
        self.save()
        with patch("nippou.services.shift_check.run",
                   side_effect=RuntimeError("想定外")):
            built = self.build()
        self.assertEqual(len(built.steps), 5)
        self.assertFalse(self.step(built, "verify").done)
        self.assertIn("確かめる", self.step(built, "verify").note)

    def test_未保存を数えられなくても帯は出る(self) -> None:
        self.save()
        with patch.object(type(self.repo), "pending_sync_headers",
                          side_effect=RuntimeError("想定外")):
            built = self.build()
        self.assertEqual(len(built.steps), 5)
        self.assertFalse(self.step(built, "push").done)

    def test_呼出中はそう言う(self) -> None:
        """帯の前提(いまの直)が崩れる。黙って別の直の進み具合を出さない。"""
        self.save()
        built = self.build(recall=True)
        self.assertIn("過去データ", built.headline)


class GoTests(FlowTestCase):
    """**次へ行く道が付いているか。**"""

    def test_次の段には行き先がある(self) -> None:
        self.save()
        built = self.build()
        self.assertTrue(built.current.url)
        self.assertTrue(built.current.action)

    def test_共有へ保存は設定へ送る(self) -> None:
        self.save()
        # 確かめるを済ませた形にして、共有へ保存を次にする
        with patch("nippou.presenters.flow._findings", return_value=0):
            built = self.build()
        self.assertEqual(built.current.key, "push")
        self.assertEqual(built.current.url, "/settings")

    def test_全部済んだら次は無い(self) -> None:
        self.save()
        self.repo.mark_synced((DAY, LINE, SHIFT, 1))
        with patch("nippou.presenters.flow._findings", return_value=0):
            built = self.build()
        self.assertIsNone(built.current)
        self.assertIn("お疲れさま", built.headline)

    def test_返す形に必要なものがそろう(self) -> None:
        body = self.build().as_dict()
        for key in ("steps", "headline", "paper_note", "current"):
            self.assertIn(key, body)
        for key in ("key", "label", "note", "done", "current", "url",
                    "action", "focus"):
            self.assertIn(key, body["steps"][0])


# ======================================================================
# 画面 (Flask が要る)
# ======================================================================
@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class ScreenTests(WebTestCase):
    """**同じ帯が3つの画面に出る。** 行った先で順番が途切れない。"""

    def _current(self) -> tuple[str, str, str]:
        from nippou import work_context

        from app.routes.entry import build_shift_calculator

        return work_context.get_context().current_key(
            build_shift_calculator(self.repo().get_shift_times()))

    def save(self) -> tuple[str, str, str]:
        from nippou.db.models import DetailRecord, HeaderRecord

        day, line, shift = self._current()
        self.repo().save(
            HeaderRecord(report_date=day, line=line, shift=shift, page=1,
                         worker="山田"),
            [DetailRecord(report_date=day, line=line, shift=shift, page=1,
                          row_no=1, lot="A1234", wei="1000", con="10")])
        return day, line, shift

    def test_3つの画面に同じ帯が出る(self) -> None:
        for path in ("/", "/records", "/settings"):
            html = self.get(path).get_data(as_text=True)
            self.assertIn('class="flow"', html, path)
            for label in ("保存(この端末)", "確かめる", "共有へ保存"):
                self.assertIn(label, html, f"{path} / {label}")

    def test_帯の頭に次の一言が出る(self) -> None:
        html = self.get("/").get_data(as_text=True)
        self.assertIn("flow__head", html)

    def test_別の画面でやることには行き先が付く(self) -> None:
        """記録・設定から「日報入力へ」で戻れる。"""
        from nippou.logic.shift import ShiftCalculator

        self.save()
        with patch.object(ShiftCalculator, "minutes_until_shift_end",
                          return_value=10):
            html = self.get("/records").get_data(as_text=True)
        self.assertIn("step__go", html)
        self.assertIn("日報入力へ", html)

    def test_この画面でやることは運ぶボタンになる(self) -> None:
        """長い画面では下のほうにある。「どこ?」で止めない。"""
        from nippou.logic.shift import ShiftCalculator

        self.save()
        with patch.object(ShiftCalculator, "minutes_until_shift_end",
                          return_value=10):
            html = self.get("/").get_data(as_text=True)
        self.assertIn('data-goto="check-shift"', html)

    def test_自分自身へのリンクは出さない(self) -> None:
        """道しるべにならない。"""
        html = self.get("/").get_data(as_text=True)
        self.assertNotIn('class="btn btn--sm btn--primary step__go" href="/"',
                         html)

    def test_入力のたびに帯が返る(self) -> None:
        body = self.post("/api/entry/state", {"rows": {}, "header": {}}).get_json()
        self.assertIn("flow", body)
        self.assertIn("steps", body["flow"])


if __name__ == "__main__":
    unittest.main()
