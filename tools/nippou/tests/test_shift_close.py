"""直の終わり間際の自動確定 (VBA `ShouldExecuteAutoPrint` → `ExecuteAutoPrint`)

VBA は1分タイマー(`Application.OnTime` → `CheckPrintReminder`)で

    残り15分  印刷忘れの催促
    残り 5分  **印刷処理を自動で走らせる**

をやっていました。こちらは**紙を作業者が欲しいときだけ**出すものに
したので、残り5分でやるのは「締める」ことです ── チェック → 集計の
作り直し → 締めた印、まで。**紙は1枚も出しません。**

ここで確かめるのは:

    ・断る5つの場面が VBA と同じ順で効くこと
    ・走ったときに「チェック → 集計 → 締めた印」が全部通ること
    ・**紙を作らない**こと
    ・**チェックで何か見つかっても止まらない**こと(VBA の自動モードと同じ)
    ・2枚のタブが同じ分に叩いても1回だけになること
    ・締めた印が立つと催促が止まり、**刷っただけでは止まらない**こと
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase

DAY = "2026年8月3日"
BUSINESS_DATE = date(2026, 8, 3)
LINE = "L-1"
SHIFT = "1直"
#: 1直は 08:00-17:00。残り3分 = **自動確定の刻**
NEAR_END = datetime(2026, 8, 3, 16, 57)
#: まだ1時間ある
EARLY = datetime(2026, 8, 3, 16, 0)


def detail(row_no: int, page: int = 1, **values):
    from nippou.db.models import DetailRecord

    base = dict(report_date=DAY, line=LINE, shift=SHIFT, page=page,
                row_no=row_no, lot="A1234", ken="10", mai="10", tut="1",
                kz="8", kh="0", sz="9", sh="0", tim="60",
                wei="1000", con="10")
    base.update(values)
    return DetailRecord(**base)


def header(page: int = 1, **values):
    from nippou.db.models import HeaderRecord

    base = dict(report_date=DAY, line=LINE, shift=SHIFT, page=page,
                worker="山田", day_shift="無")
    base.update(values)
    return HeaderRecord(**base)


# ======================================================================
# サービス層 (Flask 不要)
# ======================================================================
class ShiftCloseTestCase(unittest.TestCase):
    """一時DBと、既定の直時刻で動く計算機。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

        from nippou.db.connection import connect
        from nippou.db.repository import NippouRepository
        from nippou.db.schema import ensure_schema
        from nippou.logic.shift import ShiftCalculator, ShiftTimes
        from nippou.services import shift_close

        conn = connect(self.tmp / "t.sqlite3")
        ensure_schema(conn)
        self.addCleanup(conn.close)
        self.repo = NippouRepository(conn)
        # **既定値には頼りません。** 既定は現場の 07:00-15:00-22:50 に
        # 合わせて動くので、このファイルの NEAR_END(16:57)などが
        # 一斉に意味を失います。見たいのは締めの手順のほうです。
        #
        # **時間マスタにも同じ値を入れます。** 保存前チェックは計算機では
        # なく `shift_config` を見る(`shift_check.bounds_of`)ので、片方
        # だけ置き換えると、同じ直の終わりが2通りになります ── これは
        # 現場で実際に起きた食い違い(全停が 22:00、チェックが 22:50)と
        # 同じ形です
        times = ShiftTimes(start1="08:00", end1="17:00",
                           start2="17:00", end2="22:00",
                           start3="22:00", end3="08:00",
                           start_day="08:00", end_day="18:00")
        self.calc = ShiftCalculator(times)
        for key, (start, end) in (("1", (times.start1, times.end1)),
                                  ("2", (times.start2, times.end2)),
                                  ("3", (times.start3, times.end3)),
                                  ("昼", (times.start_day, times.end_day))):
            self.repo.set_shift_time(key, start, end)

        self.out = self.tmp / "out"
        # **見張りはプロセスに1つ。** 件をまたいで引きずらせない
        shift_close.reset()
        self.addCleanup(shift_close.reset)

    def save(self, rows=None, page: int = 1, **head) -> None:
        self.repo.save(header(page=page, **head),
                       rows if rows is not None else [detail(1, page=page)])

    def run_auto(self, now=NEAR_END, **kwargs):
        from nippou.services import shift_close

        return shift_close.run(self.repo, self.calc, line=LINE, now=now,
                               **kwargs)


class RefuseTests(ShiftCloseTestCase):
    """**断る場面。** VBA `ShouldExecuteAutoPrint` の `If` と同じ並び。"""

    def test_呼出モード中は見送る(self) -> None:
        self.save()
        result = self.run_auto(recall_mode=True)
        self.assertFalse(result.ran)
        self.assertIn("過去データ", result.reason)

    def test_もう走ったなら二度目は無い(self) -> None:
        self.save()
        result = self.run_auto(already_ran=True)
        self.assertFalse(result.ran)

    def test_締め済みなら走らない(self) -> None:
        """**一度締めた直を勝手に作り直さない。**"""
        self.save()
        self.repo.mark_shift_closed(SHIFT, BUSINESS_DATE, LINE)
        result = self.run_auto()
        self.assertFalse(result.ran)
        self.assertIn("確定済み", result.reason)

    def test_まだ早ければ走らない(self) -> None:
        self.save()
        result = self.run_auto(now=EARLY)
        self.assertFalse(result.ran)
        self.assertIn("まだ早い", result.reason)
        self.assertEqual(result.minutes_left, 60)

    def test_打ってあるものが無ければ走らない(self) -> None:
        """空の紙を作っても仕方がない(VBA `HasDataInPrintRange`)。"""
        result = self.run_auto()
        self.assertFalse(result.ran)
        self.assertIn("打ってあるもの", result.reason)

    def test_別のラインの入力では走らない(self) -> None:
        """ラインごとに別の直。**隣のラインの入力で確定しない。**"""
        self.save()
        from nippou.services import shift_close

        result = shift_close.run(self.repo, self.calc, line="HVC",
                                 now=NEAR_END)
        self.assertFalse(result.ran)

    def test_断る理由は必ず言葉で返る(self) -> None:
        """「走らなかった」だけでは追えない。"""
        for kwargs in ({"recall_mode": True}, {"already_ran": True},
                       {"now": EARLY}, {}):
            with self.subTest(**kwargs):
                result = self.run_auto(**kwargs)
                self.assertTrue(result.reason, kwargs)
                self.assertEqual(result.message, result.reason)


class RunTests(ShiftCloseTestCase):
    """**走ったとき。** 確定 → 集計 → 紙の書き出し → 印刷済み。"""

    def test_残り5分以内なら走る(self) -> None:
        self.save()
        result = self.run_auto()
        self.assertTrue(result.ran, result.reason)
        self.assertEqual(result.shift, SHIFT)
        self.assertEqual(result.report_date, DAY)
        self.assertEqual(result.minutes_left, 3)

    def test_集計が残る(self) -> None:
        """紙とグラフが同じ数字を見るように、**値で残す。**"""
        self.save()
        self.run_auto()
        found = self.repo.load_packing_report(DAY, LINE, SHIFT)
        self.assertIsNotNone(found)
        self.assertAlmostEqual(found.total_quantity, 10.0)
        self.assertAlmostEqual(found.total_weight, 1000.0)
        self.assertEqual(found.total_lot_count, 1)

    def test_締めたページの数を返す(self) -> None:
        self.save(page=1)
        self.save(page=2, rows=[detail(1, page=2, lot="B5678")])
        result = self.run_auto()
        self.assertEqual(result.pages, 2)

    def test_紙は1枚も作らない(self) -> None:
        """**ここがいちばん大事。** 紙は「印刷」ボタンからだけ。"""
        from nippou.reporting import print_format

        self.save()
        with patch.object(print_format, "write_print_html") as writer:
            result = self.run_auto()
        self.assertTrue(result.ran)
        writer.assert_not_called()

    def test_締めた印が立つ(self) -> None:
        """**ここで催促が止まります。**"""
        self.save()
        self.assertFalse(self.repo.is_shift_closed(SHIFT, BUSINESS_DATE, LINE))
        self.run_auto()
        self.assertTrue(self.repo.is_shift_closed(SHIFT, BUSINESS_DATE, LINE))

    def test_走ったあとはもう走らない(self) -> None:
        self.save()
        self.assertTrue(self.run_auto().ran)
        second = self.run_auto()
        self.assertFalse(second.ran)

    def test_知らせる文が出る(self) -> None:
        self.save()
        result = self.run_auto()
        self.assertIn("確定しました", result.message)
        # **紙は任意**であることが文言に出ている
        self.assertIn("紙が要るときは", result.message)

    def test_返す形に必要なものがそろう(self) -> None:
        self.save()
        body = self.run_auto().as_dict()
        for key in ("ran", "reason", "report_date", "line", "shift",
                    "minutes_left", "pages", "findings", "message"):
            self.assertIn(key, body)
        # 紙は作らないので、書き出し先を返す欄そのものが無い
        self.assertNotIn("files", body)


class DoNotStopTests(ShiftCloseTestCase):
    """**チェックで見つかっても止めない。** VBA の `isAutoMode` と同じ。"""

    def test_指摘があっても確定する(self) -> None:
        # 梱包数(10)が検入枚数(1)を超えている = `pack_over`
        self.save(rows=[detail(1, ken="1", mai="10", tut="10")])
        result = self.run_auto()
        self.assertTrue(result.ran, result.reason)
        self.assertTrue(result.findings, "指摘が拾えていません")
        self.assertIn("直すところ", result.message)

    def test_チェックが落ちても確定は続く(self) -> None:
        self.save()
        with patch("nippou.services.shift_check.run",
                   side_effect=RuntimeError("想定外")):
            result = self.run_auto()
        self.assertTrue(result.ran)
        self.assertTrue(self.repo.is_shift_closed(SHIFT, BUSINESS_DATE, LINE))

    def test_集計が落ちても締める(self) -> None:
        """**印が立たないと、催促が鳴り続けます。**"""
        self.save()
        with patch("nippou.services.summary.refresh_shift",
                   side_effect=RuntimeError("想定外")):
            result = self.run_auto()
        self.assertTrue(result.ran)
        self.assertEqual(result.pages, 1)
        self.assertTrue(self.repo.is_shift_closed(SHIFT, BUSINESS_DATE, LINE))

    def test_印が書けなくても走ったことにする(self) -> None:
        """チェックも集計も済んでいる。催促が止まらないだけ。"""
        self.save()
        with patch.object(type(self.repo), "mark_shift_closed",
                          side_effect=RuntimeError("想定外")):
            result = self.run_auto()
        self.assertTrue(result.ran)


class AutoPushTests(ShiftCloseTestCase):
    """**綺麗なら共有へも送る** ── 押し忘れを機械が消す。

    締めても共有へは出ていませんでした。押し忘れたまま直の時間が過ぎ、
    次の直がそのまま進む ── その流れの一番手前がここです。断りが1件も
    無いのなら、押すか押さないかを人に委ねる理由がありません。

    **断りがあるものは今までどおり送りません。** そちらは次の直の
    引き継ぎ(`logic/handover.py`)が受け止めます。
    """

    def clean_rows(self):
        """1直(08:00-17:00)を**最後まで埋めて休憩60分**を入れた1行。

        これで7項目に1つも引っかかりません ── 「押し忘れただけ」の直。
        """
        return [detail(1, kz="8", kh="0", sz="17", sh="0", tim="480",
                       s="0", th="60")]

    def test_綺麗なら送る(self) -> None:
        self.save(rows=self.clean_rows())
        with patch("nippou.access_bridge.pusher.push_pending") as push:
            push.return_value = type("S", (), {
                "succeeded": [1], "failed": [], "concurrency_warning": ""})()
            result = self.run_auto()
        self.assertTrue(result.ran, result.reason)
        push.assert_called_once()
        self.assertEqual(result.pushed, 1)
        self.assertIn("共有へも送りました", result.message)

    def test_直すところがあれば送らない(self) -> None:
        """**ここを送ってしまうと、関門の意味がなくなります。**"""
        self.save(rows=[detail(1, ken="1", mai="10", tut="10")])
        with patch("nippou.access_bridge.pusher.push_pending") as push:
            result = self.run_auto()
        push.assert_not_called()
        self.assertEqual(result.pushed, 0)
        self.assertIn("共有へは送っていません", result.push_note)
        # どの直が引っかかったのかまで書く
        self.assertIn(SHIFT, result.push_note)

    def test_送れなくても締めた印は残る(self) -> None:
        """共有が落ちている日に、直の確定までできなくなるほうが重い。"""
        self.save(rows=self.clean_rows())
        with patch("nippou.access_bridge.pusher.push_pending",
                   side_effect=RuntimeError("共有が見つかりません")):
            result = self.run_auto()
        self.assertTrue(result.ran)
        self.assertTrue(self.repo.is_shift_closed(SHIFT, BUSINESS_DATE, LINE))
        self.assertEqual(result.pushed, 0)
        self.assertIn("送れませんでした", result.push_note)

    def test_チェックが落ちたら送らない(self) -> None:
        """確かめられないものを、黙って共有へ出さない。"""
        self.save(rows=self.clean_rows())
        with patch("nippou.services.shift_check.run_pending",
                   side_effect=RuntimeError("想定外")), \
             patch("nippou.access_bridge.pusher.push_pending") as push:
            result = self.run_auto()
        push.assert_not_called()
        self.assertTrue(result.ran)
        self.assertIn("確かめられませんでした", result.push_note)

    def test_送るものが無ければ何もしない(self) -> None:
        self.save(rows=self.clean_rows())
        for header_ in self.repo.pending_sync_headers():
            self.repo.mark_synced((header_.report_date, header_.line,
                                   header_.shift, header_.page))
        with patch("nippou.access_bridge.pusher.push_pending") as push:
            result = self.run_auto()
        push.assert_not_called()
        self.assertIn("未送信のものはありません", result.push_note)

    def test_返す形に送信のぶんも入る(self) -> None:
        self.save(rows=self.clean_rows())
        with patch("nippou.access_bridge.pusher.push_pending") as push:
            push.return_value = type("S", (), {
                "succeeded": [1], "failed": [], "concurrency_warning": ""})()
            body = self.run_auto().as_dict()
        self.assertIn("pushed", body)
        self.assertIn("push_note", body)


class SweepTests(ShiftCloseTestCase):
    """**終わったのに締まっていない直**を、あとから拾う。

    残り5分の自動確定には2つ穴があります:

        1. 画面が開いていなければ動かない(タブを閉じるとアプリごと
           終わるので、これは直しようがありません)
        2. **`run` が見るのはいまの直だけ** ── 17:05 に開いたときには
           「2直の終了まで118分(まだ早い)」しか言わず、17:00 に終わった
           1直は二度と自動確定されません

    2つめをここで塞ぎます。**次に誰かが開いたときに拾う。**
    """

    def other(self) -> str:
        """いまの直(1直)ではない、終わった直。"""
        return "3直"

    def leave_behind(self, *, broken: bool = False) -> None:
        """終わった直に、共有へ未送信のまま1ページ置く。"""
        rows = dict(shift=self.other(), kz="22", kh="0", sz="8", sh="0",
                    tim="540", s="0", th="60")
        if broken:
            rows |= {"ken": "1", "mai": "10", "tut": "10"}   # 梱包数 > 検入枚数
        self.repo.save(header(shift=self.other()), [detail(1, **rows)])

    def run_sweep(self, now=EARLY, **kwargs):
        from nippou.services import shift_close

        return shift_close.sweep(self.repo, self.calc, line=LINE, now=now,
                                 **kwargs)

    def test_終わった直を拾って締める(self) -> None:
        self.leave_behind()
        with patch("nippou.access_bridge.pusher.push_pending") as push:
            push.return_value = type("S", (), {
                "succeeded": [1], "failed": [], "concurrency_warning": ""})()
            done = self.run_sweep()
        self.assertEqual([r.shift for r in done], [self.other()])
        self.assertTrue(self.repo.is_shift_closed(
            self.other(), BUSINESS_DATE, LINE))

    def test_綺麗なら共有へも送る(self) -> None:
        """**押し忘れて帰った直が、翌朝いちばんに片付きます。**"""
        self.leave_behind()
        with patch("nippou.access_bridge.pusher.push_pending") as push:
            push.return_value = type("S", (), {
                "succeeded": [1], "failed": [], "concurrency_warning": ""})()
            done = self.run_sweep()
        push.assert_called_once()
        self.assertEqual(done[-1].pushed, 1)

    def test_直すところがあれば締めるだけ(self) -> None:
        """締めはするが送らない ── そちらは次の直の引き継ぎが受け止める。"""
        self.leave_behind(broken=True)
        with patch("nippou.access_bridge.pusher.push_pending") as push:
            done = self.run_sweep()
        push.assert_not_called()
        self.assertTrue(done[0].findings)
        self.assertTrue(self.repo.is_shift_closed(
            self.other(), BUSINESS_DATE, LINE))

    def test_いまの直は拾わない(self) -> None:
        """**打っている最中の直は `run` の担当。** 二重に締めない。"""
        self.save()
        self.assertEqual(self.run_sweep(), [])
        self.assertFalse(self.repo.is_shift_closed(SHIFT, BUSINESS_DATE, LINE))

    def test_締め済みなら拾わない(self) -> None:
        self.leave_behind()
        self.repo.mark_shift_closed(self.other(), BUSINESS_DATE, LINE)
        self.assertEqual(self.run_sweep(), [])

    def test_共有へ出ていれば拾わない(self) -> None:
        """送り終えた直をいまさら締め直しても、変わるものがありません。"""
        self.leave_behind()
        for head in self.repo.pending_sync_headers():
            self.repo.mark_synced((head.report_date, head.line, head.shift,
                                   head.page))
        self.assertEqual(self.run_sweep(), [])

    def test_2度目は拾わない(self) -> None:
        self.leave_behind()
        with patch("nippou.access_bridge.pusher.push_pending") as push:
            push.return_value = type("S", (), {
                "succeeded": [1], "failed": [], "concurrency_warning": ""})()
            self.assertEqual(len(self.run_sweep()), 1)
            self.assertEqual(self.run_sweep(), [])

    def test_呼出モード中は拾わない(self) -> None:
        """過去データを開いているあいだは触らない(`run` と同じ)。"""
        self.leave_behind()
        self.assertEqual(self.run_sweep(recall_mode=True), [])

    def test_他のラインは拾わない(self) -> None:
        """ラインごとに別の直。**隣のラインのぶんまで締めない。**"""
        key = dict(report_date=DAY, line="L2", shift=self.other(), page=1)
        from nippou.db.models import DetailRecord, HeaderRecord

        self.repo.save(HeaderRecord(**key, worker="隣の人"),
                       [DetailRecord(**key, row_no=1, lot="B1")])
        self.assertEqual(self.run_sweep(), [])

    def test_何直ぶんあっても共有は1度だけ(self) -> None:
        """直の数だけ共有を開かない。"""
        self.leave_behind()
        self.repo.save(header(shift="2直"),
                       [detail(1, shift="2直", kz="17", kh="0", sz="22",
                               sh="0", tim="240", s="0", th="60")])
        with patch("nippou.access_bridge.pusher.push_pending") as push:
            push.return_value = type("S", (), {
                "succeeded": [2], "failed": [], "concurrency_warning": ""})()
            done = self.run_sweep()
        self.assertEqual(len(done), 2)
        push.assert_called_once()


class GateTests(ShiftCloseTestCase):
    """**タブが2枚あっても1回だけ。**

    VBA はフォーム1つの中の `AutoPrintExecuted` で足りたが、こちらは
    画面が何枚も開ける。同じ分に2枚が叩くと、印(`print_status`)が入る
    前にすれ違いうる。
    """

    def test_同じ分に2回叩いても確定は1回(self) -> None:
        self.save()
        first = self.run_auto()
        # 印が入る前だったことにして、2枚目を叩かせる
        self.repo.conn.execute("DELETE FROM print_status")
        self.repo.conn.commit()
        second = self.run_auto()
        self.assertTrue(first.ran)
        self.assertFalse(second.ran, "2枚目も走っています")

    def test_直が違えば別に数える(self) -> None:
        from nippou.services import shift_close

        gate = shift_close._Gate()
        self.assertTrue(gate.take((DAY, LINE, "1直")))
        self.assertTrue(gate.take((DAY, LINE, "2直")))
        self.assertFalse(gate.take((DAY, LINE, "1直")))

    def test_ラインが違えば別に数える(self) -> None:
        from nippou.services import shift_close

        gate = shift_close._Gate()
        self.assertTrue(gate.take((DAY, "L-1", SHIFT)))
        self.assertTrue(gate.take((DAY, "HVC", SHIFT)))

    def test_印が書けなかったら覚えを手放す(self) -> None:
        """**覚えだけ残るのが、いちばん悪い形です。**

        印(`print_status`)が書けないと DB は「まだ締まっていない」と
        言い続けるので、催促は鳴りやみません。それなのに見張りが
        「締めた」を覚えていると、もう一度締めようとしても
        「確定済みです」で断られます ── **どちらからも直せません。**
        """
        from nippou.db.repository import NippouRepository

        self.save()
        with patch.object(NippouRepository, "mark_shift_closed",
                          side_effect=RuntimeError("DBが書けません")):
            first = self.run_auto()
        self.assertTrue(first.ran)
        self.assertFalse(first.marked, "印が書けたことになっている")
        # 印は立っていない
        self.assertFalse(self.repo.is_shift_closed(
            SHIFT, date(2026, 8, 3), LINE))

        # **次の1分でやり直せる**
        second = self.run_auto()
        self.assertTrue(second.ran, "やり直せません(覚えが残っている)")
        self.assertTrue(second.marked)
        self.assertTrue(self.repo.is_shift_closed(
            SHIFT, date(2026, 8, 3), LINE))

    def test_書けなかったことを文言に出す(self) -> None:
        """**黙って「確定しました」で終わらせない。**"""
        from nippou.db.repository import NippouRepository

        self.save()
        with patch.object(NippouRepository, "mark_shift_closed",
                          side_effect=RuntimeError("DBが書けません")):
            found = self.run_auto()
        self.assertIn("印を残せませんでした", found.message)


class MessageTests(ShiftCloseTestCase):
    """締めの文言。**負の分を出さない。**"""

    def result(self, minutes_left: int, shift: str = "1直"):
        from nippou.services.shift_close import ShiftCloseResult

        return ShiftCloseResult(ran=True, shift=shift,
                                minutes_left=minutes_left, pages=2)

    def test_残りがあるうちは分を出す(self) -> None:
        self.assertIn("残り3分で", self.result(3).message)

    def test_過ぎていたら分を出さない(self) -> None:
        """日勤のラインは時刻に関わらず「日勤」なので、終了時刻(17:00)を
        過ぎると残りが負になります ── 「残り-60分で確定しました」は
        読んだ人が意味を取れません。
        """
        text = self.result(-60, "日勤").message
        self.assertNotIn("-60", text)
        self.assertIn("終了時刻を過ぎたので", text)

    def test_ちょうど0でも分を出さない(self) -> None:
        """あとから締めるぶん(`sweep`)は残り0で来ます。"""
        text = self.result(0).message
        self.assertNotIn("残り0分", text)
        self.assertIn("終了時刻を過ぎたので", text)

    def test_日勤で終了を過ぎても自動確定は走る(self) -> None:
        """**止めません。** 走らないほうが困ります ── 打ってあるものが
        締まらないまま残ります。直したのは文言のほうです。
        """
        from nippou.logic.shift import ShiftCloseChecker

        checker = ShiftCloseChecker(
            self.calc, is_shift_closed=lambda s, d: False,
            warn_minutes=15, auto_close_minutes=5)
        past = datetime(2026, 8, 3, 19, 0)       # 日勤の終わり(18:00)の1時間後
        self.assertLess(self.calc.minutes_until_shift_end(past, True), 0)
        self.assertTrue(checker.should_auto_close(
            past, True, recall_mode=False, already_closed=False,
            has_pending_data=True))


class ReminderTests(ShiftCloseTestCase):
    """締めた印と、催促(`ShiftCloseChecker`)のつながり。"""

    WARN = datetime(2026, 8, 3, 16, 50)          # 残り10分

    def _checker(self):
        from nippou.logic.shift import ShiftCloseChecker

        return ShiftCloseChecker(
            self.calc,
            is_shift_closed=lambda s, d: self.repo.is_shift_closed(s, d, LINE))

    def test_印を立てる前は催促する(self) -> None:
        self.assertTrue(self._checker().close_forgotten(self.WARN))

    def test_確定したら催促が止まる(self) -> None:
        self.save()
        self.run_auto()
        self.assertFalse(self._checker().close_forgotten(self.WARN))

    def test_手で印を立てても催促が止まる(self) -> None:
        self.repo.mark_shift_closed(SHIFT, BUSINESS_DATE, LINE)
        self.assertFalse(self._checker().close_forgotten(self.WARN))


# ======================================================================
# 画面側 (Flask が要る)
# ======================================================================
@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class RouteTests(WebTestCase):
    """`POST /api/entry/close` と、紙を出しても印が立たないこと。"""

    def setUp(self) -> None:
        super().setUp()
        from nippou.services import shift_close

        shift_close.reset()
        self.addCleanup(shift_close.reset)

    def _current(self) -> tuple[str, str, str]:
        from nippou import work_context

        from app.routes.entry import build_shift_calculator

        ctx = work_context.get_context()
        calc = build_shift_calculator(self.repo().get_shift_times())
        return ctx.current_key(calc)

    def _save_now(self) -> tuple[str, str, str]:
        """いまの直に1ページだけ入れる。"""
        from nippou.db.models import DetailRecord, HeaderRecord

        day, line, shift = self._current()
        rows = [DetailRecord(report_date=day, line=line, shift=shift, page=1,
                             row_no=1, lot="A1234", ken="10", mai="10",
                             tut="1", kz="8", kh="0", sz="9", sh="0",
                             tim="60", wei="1000", con="10")]
        self.repo().save(HeaderRecord(report_date=day, line=line, shift=shift,
                                      page=1, worker="山田"), rows)
        return day, line, shift

    def test_走らなくても200で返る(self) -> None:
        """**ふつうはいつも「まだ早い」。** それは失敗ではない。"""
        res = self.post("/api/entry/close")
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertFalse(body["ran"])
        self.assertTrue(body["reason"])
        self.assertEqual(body["swept"], [])

    def test_終わっていた直を拾って返す(self) -> None:
        """**「まだ早い」で終わらせない。**

        `run` が見るのはいまの直だけなので、17:05 に開いたときには
        17:00 に終わった直が拾われないままでした。ここで拾います。
        """
        from nippou.db.models import DetailRecord, HeaderRecord

        day, line, shift = self._current()
        other = "3直" if shift != "3直" else "1直"
        key = dict(report_date=day, line=line, shift=other, page=1)
        self.repo().save(HeaderRecord(**key, worker="前の人"),
                         [DetailRecord(**key, row_no=1, lot="A1",
                                       ken="1", mai="10", tut="10")])
        body = self.post("/api/entry/close").get_json()
        self.assertFalse(body["ran"], "いまの直はまだ終わっていない")
        self.assertEqual([r["shift"] for r in body["swept"]], [other])
        self.assertIn("確定しました", body["message"])
        # 直すところがあるので共有へは出さない(引き継ぎが受け止める)
        self.assertEqual(body["swept"][0]["pushed"], 0)

    def test_呼出モード中は見送ると返る(self) -> None:
        from nippou import work_context
        from nippou.services.nippou_service import RecallState

        work_context.get_context().recall = RecallState(
            active=True, report_date="2026年7月1日", line="L-1", shift="2直")
        body = self.post("/api/entry/close").get_json()
        self.assertFalse(body["ran"])
        self.assertIn("過去データ", body["reason"])

    def test_残り5分なら走る(self) -> None:
        from nippou.logic.shift import ShiftCalculator

        day, line, shift = self._save_now()
        # **時計だけを動かす。** 直の決め方はそのままにしたいので、
        # 残り分数を返すところ1点だけ差し替える
        with patch.object(ShiftCalculator, "minutes_until_shift_end",
                          return_value=3):
            body = self.post("/api/entry/close").get_json()
        self.assertTrue(body["ran"], body["reason"])
        self.assertEqual(body["shift"], shift)
        self.assertEqual(body["pages"], 1)

    def test_紙を出しても締めたことにはならない(self) -> None:
        """**紙は任意。** VBA は刷った時刻をレジストリに入れて催促を
        止めていたが、途中で1枚刷っただけで止まると、そのあとに打った
        行が確かめられないまま直が終わる。"""
        from nippou.logic.shift import parse_business_date

        day, line, shift = self._save_now()
        res = self.get(f"/report/nippou?report_date={day}&line={line}"
                       f"&shift={shift}&page=1")
        self.assertEqual(res.status_code, 200)
        self.assertFalse(self.repo().is_shift_closed(
            shift, parse_business_date(day), line))

    def test_刷ったあとでも自動確定は走る(self) -> None:
        """上の裏返し ── 刷ってあっても、締めはちゃんと来る。"""
        from nippou.logic.shift import ShiftCalculator

        day, line, shift = self._save_now()
        self.get(f"/report/nippou?report_date={day}&line={line}"
                 f"&shift={shift}&page=1")
        with patch.object(ShiftCalculator, "minutes_until_shift_end",
                          return_value=3):
            body = self.post("/api/entry/close").get_json()
        self.assertTrue(body["ran"], body["reason"])

    def test_下見では何も起きない(self) -> None:
        from nippou.logic.shift import parse_business_date

        day, line, shift = self._save_now()
        self.post("/api/print/load",
                  {"report_date": day, "line": line, "shift": shift, "page": 1})
        self.assertFalse(self.repo().is_shift_closed(
            shift, parse_business_date(day), line))

    def test_入力画面に印刷ボタンがある(self) -> None:
        """**保存とは別のボタン。** 紙は要るときだけ出す。"""
        html = self.get("/").get_data(as_text=True)
        self.assertIn('id="print-page"', html)
        self.assertIn('id="save"', html)


# ======================================================================
# 紙は「いつでも見られる」こと
#
# 自動で出すのをやめただけで、**見る道はふさいでいない**。保存して
# ある直なら、その場で組み立てて何度でも同じものが出る。
# ======================================================================
@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class ViewPaperTests(WebTestCase):

    def save(self, day: str, line: str, shift: str, pages: int = 1) -> None:
        from nippou.db.models import DetailRecord, HeaderRecord

        for n in range(1, pages + 1):
            rows = [DetailRecord(report_date=day, line=line, shift=shift,
                                 page=n, row_no=1, lot=f"LOT{n}", ken="10",
                                 mai="10", tut="1", kz="8", kh="0", sz="9",
                                 sh="0", tim="60", wei="1000", con="10")]
            self.repo().save(HeaderRecord(report_date=day, line=line,
                                          shift=shift, page=n,
                                          worker="山田"), rows)

    def paper(self, day: str, line: str, shift: str, page="1"):
        return self.get(f"/report/nippou?report_date={day}&line={line}"
                        f"&shift={shift}&page={page}")

    def test_過去の直でもいつでも開ける(self) -> None:
        """**何日前でも関係ない。** 保存してあれば出る。"""
        self.save("2025年1月5日", "HVC", "3直")
        res = self.paper("2025年1月5日", "HVC", "3直")
        self.assertEqual(res.status_code, 200)
        self.assertIn("LOT1", res.get_data(as_text=True))

    def test_何度開いても同じものが出る(self) -> None:
        """貯めたファイルではなく、そのつど明細から組み立てる。"""
        self.save("2025年1月5日", "HVC", "3直")
        first = self.paper("2025年1月5日", "HVC", "3直").get_data(as_text=True)
        second = self.paper("2025年1月5日", "HVC", "3直").get_data(as_text=True)
        # 生成日時の行だけは違いうるので、そこを外して比べる
        strip = lambda s: "\n".join(                       # noqa: E731
            ln for ln in s.splitlines() if "生成日時" not in ln)
        self.assertEqual(strip(first), strip(second))

    def test_直ぜんぶを1つの窓で開ける(self) -> None:
        self.save("2026年8月3日", "L-1", "1直", pages=3)
        res = self.paper("2026年8月3日", "L-1", "1直", page="all")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertEqual(html.count('<section class="sheet">'), 3)
        for n in (1, 2, 3):
            self.assertIn(f"LOT{n}", html)

    def test_歯抜けのページでもぜんぶ出る(self) -> None:
        """ページ1と3だけ保存されている直。**あるものを全部。**"""
        from nippou.db.models import DetailRecord, HeaderRecord

        for n in (1, 3):
            self.repo().save(
                HeaderRecord(report_date="2026年8月3日", line="L-1",
                             shift="2直", page=n, worker="山田"),
                [DetailRecord(report_date="2026年8月3日", line="L-1",
                              shift="2直", page=n, row_no=1, lot=f"LOT{n}")])
        html = self.paper("2026年8月3日", "L-1", "2直",
                          page="all").get_data(as_text=True)
        self.assertEqual(html.count('<section class="sheet">'), 2)
        self.assertIn("LOT3", html)

    def test_無い直はぜんぶ指定でも404(self) -> None:
        res = self.paper("2020年1月1日", "L-1", "1直", page="all")
        self.assertEqual(res.status_code, 404)

    def test_下見はどのページがあるか教える(self) -> None:
        """**ページ番号を当てずっぽうで打たせない。**"""
        self.save("2026年8月3日", "L-1", "1直", pages=2)
        body = self.post("/api/print/load",
                         {"report_date": "2026年8月3日", "line": "L-1",
                          "shift": "1直", "page": 1}).get_json()
        self.assertTrue(body["found"])
        self.assertEqual(body["pages"], [1, 2])

    def test_無いページを指定したら在るページを言う(self) -> None:
        self.save("2026年8月3日", "L-1", "1直", pages=2)
        body = self.post("/api/print/load",
                         {"report_date": "2026年8月3日", "line": "L-1",
                          "shift": "1直", "page": 9}).get_json()
        self.assertFalse(body["found"])
        self.assertEqual(body["pages"], [1, 2])
        self.assertIn("1ページ・2ページ", body["message"])

    def test_記録画面は直の単位で並べる(self) -> None:
        """3ページある直が3行に見えると、選ぶのが一仕事になる。"""
        self.save("2026年8月3日", "L-1", "1直", pages=3)
        html = self.get("/records").get_data(as_text=True)
        self.assertIn("1・2・3ページ", html)

    def test_記録画面は他のラインも出す(self) -> None:
        """いまのライン以外が出てこないと、そこで行き止まりになる。"""
        self.save("2026年8月3日", "HVC", "2直")
        html = self.get("/records").get_data(as_text=True)
        self.assertIn("HVC", html)
        self.assertIn('data-shift="2直"', html)


if __name__ == "__main__":
    unittest.main()
