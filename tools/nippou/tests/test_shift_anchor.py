"""入力先の直は、**作業者を選んだ時点で決まる**

【なぜ書いたか】

    「作業者登録するまで入力できないんじゃないの？
      作業者登録した時間が入力する直の入力ですよ
      何故またいで入力できるのですか」

直の始まりを「作業者を選ぶこと」に決めた(v3.54.0)のに、**入力先だけが
要求のたびに時計から引き直されて**いました。15:00 をまたいだ瞬間に、
打っている本人は1直のつもりなのに、画面とサーバは2直を指します。

ここで確かめるのは4つです:

    1. 作業者が決まった**瞬間の時計の直**に固定される(15:05 なら2直)
    2. 固定してあるあいだ、時計が進んでも**書き先は動かない**
    3. だから「どちらの直へ入れますか」は**訊かれない**
    4. 終わりの時刻ちょうどで**見るだけ**になる

4 が要ります。過ぎても打てたままにすると:

    「忘れて立ち去った場合に次の人が打たなきゃいけなくなるよ
      さすがにそれは求めれないでしょ」

そのとおりで、開いたままの紙を次に座った人に押し付けることになります。

猶予は置きません(既定 0)。理由も残しておきます:

    「入力時間が足りなくなった場合しか得がない
      でもほとんどのケースは入力忘れ、保存忘れ
      時間が伸びることによって次の直が尻ぬぐいをすることになる」
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import shift_anchor
from nippou.logic.shift import ShiftCalculator, ShiftTimes
from nippou.logic.shift_anchor import Anchor
from tests import _shift_clock as shift_clock
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase

#: 現場と同じ直(07:00-15:00-22:50-07:00)
TIMES = ShiftTimes(start1="07:00", end1="15:00",
                   start2="15:00", end2="22:50",
                   start3="22:50", end3="07:00",
                   start_day="07:00", end_day="17:00")


def _anchor(shift="1直", day="2026年9月17日") -> Anchor:
    return Anchor(active=True, report_date=day, line="L-1", shift=shift,
                  started_at="2026-09-17T08:00:00")


# ======================================================================
# 1. 判断そのもの (Flask が無くても通る)
# ======================================================================
class ShiftEndAtTests(unittest.TestCase):
    """**名指しした直**の終わり。日をまたぐ直は翌日。"""

    def setUp(self) -> None:
        self.calc = ShiftCalculator(TIMES)
        self.day = date(2026, 9, 17)

    def test_1直は同じ日の15時(self) -> None:
        self.assertEqual(self.calc.shift_end_at("1直", self.day),
                         datetime(2026, 9, 17, 15, 0))

    def test_2直は同じ日の2250(self) -> None:
        self.assertEqual(self.calc.shift_end_at("2直", self.day),
                         datetime(2026, 9, 17, 22, 50))

    def test_3直は翌日の7時(self) -> None:
        """**報告日は始めた日。** 22:50 に始めて翌 07:00 に終わる。"""
        self.assertEqual(self.calc.shift_end_at("3直", self.day),
                         datetime(2026, 9, 18, 7, 0))

    def test_知らない直はNone(self) -> None:
        self.assertIsNone(self.calc.shift_end_at("4直", self.day))

    def test_3直を名指ししていない(self) -> None:
        """「終わりが始まりより前なら翌日」で決める ── マスタが変わっても
        ここを直さずに済むように。
        """
        odd = ShiftCalculator(ShiftTimes(start1="20:00", end1="04:00"))
        self.assertEqual(odd.shift_end_at("1直", self.day),
                         datetime(2026, 9, 18, 4, 0))


class DecideTests(unittest.TestCase):
    """固定がまだ効いているか。"""

    def at(self, hour, minute=0, day=17) -> datetime:
        return datetime(2026, 9, day, hour, minute)

    def end1(self) -> datetime:
        return datetime(2026, 9, 17, 15, 0)

    def test_固定していなければ何も言わない(self) -> None:
        found = shift_anchor.decide(Anchor(), now=self.at(10),
                                    shift_end=self.end1())
        self.assertFalse(found.open)
        self.assertFalse(found.expired)
        self.assertEqual(found.message, "")

    def test_直の中なら開いている(self) -> None:
        found = shift_anchor.decide(_anchor(), now=self.at(10),
                                    shift_end=self.end1())
        self.assertTrue(found.open)
        self.assertFalse(found.expired)
        # **まだ何も言わない。** 直の途中で毎回出しても邪魔なだけ
        self.assertEqual(found.message, "")

    def test_猶予を置いた現場では過ぎても打てる(self) -> None:
        """**既定ではありません。** 猶予を置くと決めた現場のための目盛り。"""
        found = shift_anchor.decide(_anchor(), now=self.at(15, 10),
                                    shift_end=self.end1(), grace_minutes=30)
        self.assertTrue(found.open)
        self.assertFalse(found.expired)
        self.assertEqual(found.minutes_over, 10)
        self.assertEqual(found.minutes_left, 20)
        self.assertIn("10分前に終わっています", found.message)
        self.assertIn("あと20分", found.message)

    def test_終わりの時刻ちょうどで見るだけ(self) -> None:
        """**猶予を渡さない**(=現場の既定)なら 15:00 ちょうどで閉じます。"""
        found = shift_anchor.decide(_anchor(), now=self.at(15, 0),
                                    shift_end=self.end1())
        self.assertFalse(found.open)
        self.assertTrue(found.expired)
        self.assertIn("見るだけです", found.message)
        # **次にやることまで書く。** 止めるだけでは動けない
        self.assertIn("作業者を選ぶ", found.message)
        self.assertIn("記録を見る", found.message)

    def test_1分前はまだ打てる(self) -> None:
        """閉じるのは**過ぎてから**。14:59 で手を止めさせません。"""
        found = shift_anchor.decide(_anchor(), now=self.at(14, 59),
                                    shift_end=self.end1())
        self.assertTrue(found.open)
        self.assertFalse(found.expired)

    def test_終わりが読めなければ閉じない(self) -> None:
        """**時刻が読めないことを理由に打てなくしない。**"""
        found = shift_anchor.decide(_anchor(), now=self.at(23),
                                    shift_end=None)
        self.assertTrue(found.open)
        self.assertFalse(found.expired)

    def test_文言に印を混ぜない(self) -> None:
        for now in (self.at(15, 10), self.at(16)):
            found = shift_anchor.decide(_anchor(), now=now,
                                        shift_end=self.end1())
            self.assertNotIn("**", found.message)


class GraceDefaultTests(unittest.TestCase):
    """**猶予の既定は 0。** 延ばさないと決めた理由を、ここに残します。

        「入力時間が足りなくなった場合しか得がない
          でもほとんどのケースは入力忘れ、保存忘れ
          時間が伸びることによって次の直が尻ぬぐいをすることになる」

    得をするのは打ち終わらなかった直(まれ)、損をするのは忘れて帰った
    直の後始末(よくある)。割に合わないので 0 にしました。閉じる**前**の
    催促(15分前の声かけ・5分前の自動確定)で間に合わせます。
    """

    def test_判断の既定が0(self) -> None:
        self.assertEqual(shift_anchor.DEFAULT_GRACE_MINUTES, 0)

    def test_設定の既定も0(self) -> None:
        """設定(`config.SETTINGS`)と判断(`logic`)が食い違うと、
        どちらが効いているのか分からなくなります。
        """
        from nippou.config import Settings

        self.assertEqual(Settings().shift_grace_minutes, 0)


class StartKeyTests(unittest.TestCase):
    """**選んだ瞬間の時計の直**に固定する。"""

    def test_選んだ直がそのまま入る(self) -> None:
        now = datetime(2026, 9, 17, 15, 5)
        found = shift_anchor.start_key(("2026年9月17日", "L-1", "2直"), now)
        self.assertTrue(found.filled)
        self.assertEqual(found.key, ("2026年9月17日", "L-1", "2直"))
        self.assertEqual(found.started_at, "2026-09-17T15:05:00")

    def test_15時5分に選んだら2直(self) -> None:
        """**迷いを残さない。** 15:00 を過ぎてから1直の続きを打つのは
        「過ぎた直を直す」話で、「記録を見る」から呼び出します。
        """
        calc = ShiftCalculator(TIMES)
        now = datetime(2026, 9, 17, 15, 5)
        self.assertEqual(calc.time_check(now), "2直")


class ShouldRestartTests(unittest.TestCase):
    def test_ラインが変われば取り直す(self) -> None:
        self.assertTrue(shift_anchor.should_restart(
            _anchor(), ("2026年9月17日", "L2", "1直")))

    def test_直が進んだだけでは取り直さない(self) -> None:
        """**ここが要点。** 時計の直を合図にすると、固定した意味が無い。"""
        self.assertFalse(shift_anchor.should_restart(
            _anchor(), ("2026年9月17日", "L-1", "2直")))

    def test_固定していなければ取り直しも無い(self) -> None:
        self.assertFalse(shift_anchor.should_restart(
            Anchor(), ("2026年9月17日", "L-1", "1直")))


# ======================================================================
# 2. 押したときにどうなるか (Flask が要る)
# ======================================================================
@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class AnchorRouteTests(WebTestCase):
    """作業者を決めると固定され、**時計が進んでも書き先が動かない**こと。"""

    def put_now_in(self, key: str) -> None:
        """いまが `key` の直に入るように、時間マスタのほうを動かす。

        並べ方は `tests/_shift_clock.chain` に寄せてあります ── 素直に
        `いま ± 30分` で置くと真夜中の前後で日をまたぎ、`TimeCheck` の
        1直・2直に当たらず3直へ落ちるので、**夜中にだけ落ちて**いました。
        """
        now = datetime.now()
        windows = shift_clock.chain(now, key, head=30, tail=30, width=60,
                                    min_width=20)
        if windows is None:
            self.skipTest(
                f"いまは {shift_clock.hhmm(now)} で、日をまたがない窓を"
                "3本並べられません(日の端のわずかなあいだだけ)")
        shift_clock.write(self.repo(), windows)

    def state(self, worker: str = "") -> dict:
        body = self.post("/api/entry/state", {
            "rows": {}, "header": {"worker": worker}, "checks": {}})
        return body.get_json()

    def anchor(self) -> dict:
        from nippou import work_context

        return work_context.get_context().anchor.as_dict()

    def test_作業者が空なら固定しない(self) -> None:
        """直が始まっていないので、固定するものがありません。"""
        body = self.state("")
        self.assertFalse(body["anchor"]["active"])

    def test_作業者を決めたら固定する(self) -> None:
        body = self.state("山田")
        self.assertTrue(body["anchor"]["active"])
        self.assertEqual(body["anchor"]["shift"], body["shift"])
        self.assertTrue(body["anchor"]["started_at"])

    def test_時計が進んでも書き先は動かない(self) -> None:
        """**これが本題。** 15:00 をまたいでも1直へ書き続けます。"""
        self.put_now_in("1")
        first = self.state("山田")
        self.assertEqual(first["shift"], "1直")

        self.put_now_in("2")                 # 時計だけ2直へ進める
        after = self.state("山田")
        self.assertEqual(after["shift"], "1直", "書き先が時計に引きずられた")
        self.assertEqual(after["anchor"]["shift"], "1直")

    def test_固定は最初の1回だけ(self) -> None:
        """打っている途中に取り直されると、書き先が動いてしまいます。"""
        self.put_now_in("1")
        self.state("山田")
        started = self.anchor()["started_at"]
        self.put_now_in("2")
        self.state("田中")                   # 作業者を足しても取り直さない
        self.assertEqual(self.anchor()["started_at"], started)
        self.assertEqual(self.anchor()["shift"], "1直")

    def test_応答に様子が入っている(self) -> None:
        body = self.state("山田")
        self.assertIn("anchor_standing", body)
        for key in ("open", "expired", "minutes_left", "message"):
            self.assertIn(key, body["anchor_standing"])

    def test_1分の見張りにも入っている(self) -> None:
        """見るだけになった瞬間に画面を塗り直す合図(`shift_end.js`)。"""
        self.state("山田")
        body = self.get("/api/shift/key").get_json()
        self.assertIn("anchor_standing", body)
        self.assertIn("anchor", body)


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class ExpiredTests(WebTestCase):
    """**終わりの時刻を過ぎたら見るだけ。** 忘れて立ち去った紙を次の人に
    押し付けない。
    """

    def start_and_expire(self) -> None:
        """1直を始めてから、その直がとっくに終わった形にする。"""
        now = datetime.now()
        # いまが1直の中
        self.put_inside(now)
        self.post("/api/entry/state",
                  {"rows": {}, "header": {"worker": "山田"}, "checks": {}})
        # 1直の終わりを**猶予のぶんより前**へ動かす(=とっくに過ぎた。
        # 猶予0の既定なら 10分前で足りるが、現場が延ばしていても通るように)
        self.put_ended(now)

    def put_inside(self, now: datetime) -> None:
        """いまが1直の中に入る窓。**その日の中に収める。**"""
        window = shift_clock.window_around(now, head=30, tail=30)
        if window is None:
            self.skipTest(f"いまは {shift_clock.hhmm(now)} で、日をまたがない"
                          "窓が作れません(日の端の1分だけ)")
        self.repo().set_shift_time("1", shift_clock.hhmm(window[0]),
                                   shift_clock.hhmm(window[1]))

    def put_ended(self, now: datetime) -> None:
        """1直が**とっくに終わった**形。終わりは今日のうちに置く。"""
        from nippou.config import SETTINGS

        over = SETTINGS.shift_grace_minutes + 10
        window = shift_clock.window_before(now, minutes_ago=over, width=60)
        if window is None:
            self.skipTest(f"いまは {shift_clock.hhmm(now)} で、今日のうちに"
                          "終わった直を作れません(真夜中すぎだけ)")
        self.repo().set_shift_time("1", shift_clock.hhmm(window[0]),
                                   shift_clock.hhmm(window[1]))

    def test_見るだけになる(self) -> None:
        self.start_and_expire()
        body = self.post("/api/entry/state",
                         {"rows": {}, "header": {}, "checks": {}}).get_json()
        self.assertTrue(body["anchor_standing"]["expired"])
        self.assertTrue(body["read_only"], "打てるままになっている")

    def test_保存も断る(self) -> None:
        """**画面だけ伏せても意味がありません。** 断るのはサーバ。"""
        self.start_and_expire()
        res = self.post("/api/entry/save", {
            "rows": {"1": {"LOT": "A1"}},
            "header": {"worker": "山田"}, "checks": {}})
        self.assertEqual(res.status_code, 403)

    def test_出口が画面に出ている(self) -> None:
        """**閉じるからには出口を置きます。**

        止まったまま次へ進む道が無いと、次に座った人は何を押せばよいのか
        分かりません。理由が「時間が過ぎた」なら「次の直を始める」を出し、
        過去データを開いているときの「最新のページに戻る」は伏せます。
        """
        self.start_and_expire()
        html = self.get("/").get_data(as_text=True)
        self.assertIn('id="start-next-shift"', html)
        head = html.index('id="start-next-shift"')
        # **伏せていない**こと(`hidden` が付いていない)
        self.assertNotIn("hidden", html[head:head + 120])

    def test_次の直を始めれば打てるようになる(self) -> None:
        """止めっぱなしにしません ── 出口は2押し(始める → 作業者を選ぶ)。"""
        self.start_and_expire()
        started = self.post("/api/entry/start-next", {})
        self.assertEqual(started.status_code, 200)
        self.assertIn("始めます", started.get_json()["message"])

        body = self.post("/api/entry/state", {
            "rows": {}, "header": {"worker": "次の人"},
            "checks": {}}).get_json()
        self.assertFalse(body["read_only"], "出口を通っても閉じたまま")
        self.assertTrue(body["anchor"]["active"])

    def test_始めた直後は作業者から(self) -> None:
        """手放した直後は固定が空 ── 直の始まりは作業者を選ぶことなので。"""
        self.start_and_expire()
        self.post("/api/entry/start-next", {})
        body = self.post("/api/entry/state",
                         {"rows": {}, "header": {}, "checks": {}}).get_json()
        self.assertFalse(body["anchor"]["active"])
        self.assertTrue(body["needs_worker"])

    def test_過去のページを開いたままでも次の直へ行ける(self) -> None:
        """**1押しで次の直の白紙まで行き着くこと。**

        手放すのが固定だけだと、呼出中(過去のページを開いている)に
        押しても書き先は呼出の鍵のままで、**画面は前の直の紙を映した
        まま**です ── 押したのに何も起きていないように見えます。
        """
        from nippou import work_context

        now = datetime.now()
        # まず直の中で1ページ保存し、そのページを開く(呼出モード)
        self.put_inside(now)
        self.post("/api/entry/save", {"rows": {"1": {"LOT": "A1"}},
                                      "header": {"worker": "山田"},
                                      "checks": {}, "silent": True})
        self.post("/api/settings/page", {"page": 1})
        self.assertTrue(work_context.get_context().recall.active)

        # そのまま直の終わりを過ぎる
        self.put_ended(now)

        self.post("/api/entry/start-next", {})
        self.assertFalse(work_context.get_context().recall.active,
                         "過去のページを開いたままになっている")

    def test_理由を返している(self) -> None:
        """出口を選ぶのに要ります(`_read_only_reason`)。"""
        self.start_and_expire()
        body = self.post("/api/entry/state",
                         {"rows": {}, "header": {}, "checks": {}}).get_json()
        self.assertEqual(body["read_only_reason"], "anchor")


if __name__ == "__main__":
    unittest.main()
