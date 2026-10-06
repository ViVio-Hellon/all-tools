"""直の締めくくり ── **その画面で終わらせる**

    共有保存にまで持っていく導線も確保したい
    今のままでは忘れると思う
    vbaでは保存するとグラフが全画面で描写されるのでそれが合図に近い
    ものだったが、いまはなんのメリハリも無い

直の終わりに出る画面いっぱいの確認は、出口が「確認しました」だけで
そこで終わっていました。**成り行きを出して、次に押すもの1つを決めます。**
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import shift_closing, shift_review

try:
    from tests._web import WebTestCase
    HAS_FLASK = True
except Exception:                                 # noqa: BLE001
    HAS_FLASK = False
    WebTestCase = object                          # type: ignore
SKIP = "Flask が入っていません"

FINDING = {"where": "2ページ 1行目", "message": "停止理由が空です"}


class ActionTests(unittest.TestCase):
    """次に押すものは1つ。**場面で決まる。**"""

    def test_直すところがあれば直しに戻る(self) -> None:
        got = shift_closing.build(shift="1直", pages=3, unsynced=3,
                                  findings=[FINDING])
        self.assertEqual(got.action, shift_closing.ACT_FIX)
        self.assertEqual(got.label, "直しに戻る")
        self.assertEqual(got.level, shift_closing.LEVEL_ERROR)
        self.assertFalse(got.can_push)

    def test_綺麗でまだなら共有へ保存(self) -> None:
        got = shift_closing.build(shift="1直", pages=3, unsynced=3)
        self.assertEqual(got.action, shift_closing.ACT_PUSH)
        self.assertEqual(got.label, "共有へ保存して終わる")
        self.assertTrue(got.can_push)

    def test_渡してあれば見て終わるだけ(self) -> None:
        got = shift_closing.build(shift="1直", pages=3, unsynced=0)
        self.assertEqual(got.action, shift_closing.ACT_DONE)
        self.assertEqual(got.level, shift_closing.LEVEL_OK)
        self.assertFalse(got.can_push)

    def test_1ページも無ければ押すものがない(self) -> None:
        got = shift_closing.build(shift="1直", pages=0)
        self.assertEqual(got.action, shift_closing.ACT_NONE)
        self.assertIn("まだ1ページも保存されていません", got.headline)

    def test_直すところがあるあいだは未送信でも共有へ行かせない(self) -> None:
        """**順番が要る。** 直すのが先で、共有はそのあと。"""
        got = shift_closing.build(shift="1直", pages=3, unsynced=3,
                                  findings=[FINDING, FINDING])
        self.assertEqual(got.action, shift_closing.ACT_FIX)


class HeadlineTests(unittest.TestCase):
    """**成り行きを言い切る。** 読んだ人がそのまま押せるように。"""

    def test_渡したその場では渡しましたと言う(self) -> None:
        got = shift_closing.build(shift="2直", pages=2, unsynced=0,
                                  just_pushed=True)
        self.assertIn("共有へ渡しました", got.headline)

    def test_前から渡してあるのとは言い分けている(self) -> None:
        got = shift_closing.build(shift="2直", pages=2, unsynced=0)
        self.assertIn("渡してあります", got.headline)

    def test_件数を出す(self) -> None:
        got = shift_closing.build(shift="1直", pages=1, unsynced=1,
                                  findings=[FINDING, FINDING])
        self.assertIn("2件", got.headline)

    def test_確かめられなければそう書く(self) -> None:
        """**確かめていないことを、確かめたことにしない。**"""
        got = shift_closing.build(shift="1直", pages=3, unsynced=3,
                                  counted=False)
        self.assertIn("確かめられませんでした", got.headline)
        # それでも共有への道は塞がない(押した先が本物の関門を通す)
        self.assertTrue(got.can_push)

    def test_飾りを画面に出さない(self) -> None:
        """`**…**` はそのまま字で出ます。"""
        for kw in ({"pages": 0}, {"pages": 2, "unsynced": 2},
                   {"pages": 2, "unsynced": 0},
                   {"pages": 2, "unsynced": 2, "findings": [FINDING]}):
            got = shift_closing.build(shift="1直", **kw)
            with self.subTest(action=got.action):
                self.assertNotIn("**", got.headline)
                self.assertNotIn("**", got.note)
                self.assertNotIn("**", got.label)


class DictTests(unittest.TestCase):
    def test_画面が要るものが揃っている(self) -> None:
        got = shift_closing.build(report_date="2026-09-17", line="機側",
                                  shift="1直", pages=3, unsynced=3).as_dict()
        for key in ("report_date", "line", "shift", "pages", "unsynced",
                    "findings", "counted", "just_pushed", "action", "label",
                    "level", "headline", "note", "can_push"):
            self.assertIn(key, got)


class EndStageTests(unittest.TestCase):
    """**終わった直を、もう一度だけ呼び戻す。**

    「あとで」で閉じたまま共有へ渡せずに直が終わったときだけです。
    """

    def setUp(self) -> None:
        shift_review.reset()

    def tearDown(self) -> None:
        shift_review.reset()

    def test_未送信なら出す(self) -> None:
        got = shift_review.evaluate_ended("2026-09-17", "1直", unsynced=True)
        self.assertTrue(got.due)
        self.assertEqual(got.stage, shift_review.STAGE_END)
        self.assertIn("まだ共有へ渡していません", got.reason)

    def test_渡してあれば出さない(self) -> None:
        self.assertFalse(
            shift_review.evaluate_ended("2026-09-17", "1直",
                                        unsynced=False).due)

    def test_呼出モード中は出さない(self) -> None:
        """過去のものを直している最中に、別の直の話を割り込ませない。"""
        self.assertFalse(
            shift_review.evaluate_ended("2026-09-17", "1直", unsynced=True,
                                        recall_mode=True).due)

    def test_2度目は出さない(self) -> None:
        """**3度目はありません。** 何度も出すと読まずに閉じます。"""
        shift_review.gate().mark("2026-09-17", "1直", shift_review.STAGE_END)
        self.assertFalse(
            shift_review.evaluate_ended("2026-09-17", "1直",
                                        unsynced=True).due)

    def test_15分前を閉じても終わりの呼び戻しは残る(self) -> None:
        """機会を分けている理由がこれ。"""
        shift_review.gate().mark("2026-09-17", "1直", shift_review.STAGE_WARN)
        self.assertTrue(
            shift_review.evaluate_ended("2026-09-17", "1直",
                                        unsynced=True).due)


@unittest.skipUnless(HAS_FLASK, SKIP)
class ScreenTests(WebTestCase):
    """全画面の確認に、締めくくりが出ているか。"""

    SHEET = {"rows": {"1": {"LOT": "N7131T0", "KZ": "08", "KH": "00",
                            "SZ": "10", "SH": "00", "CON": "100",
                            "WEI": "1000"}},
             "header": {"worker": "山田"}, "checks": {}}

    def setUp(self) -> None:
        super().setUp()
        self.post("/api/entry/line", {"line": "L-1"})

    def test_締めくくりの帯が出ている(self) -> None:
        html = self.get("/graph/review").get_data(as_text=True)
        self.assertIn('id="closing"', html)
        self.assertIn('id="review-act"', html)

    def test_サーバが描いたぶんを持っている(self) -> None:
        """応答を1つ待つあいだ空の帯が見える、をなくす(v3.59.0 と同じ)。"""
        html = self.get("/graph/review").get_data(as_text=True)
        self.assertIn('id="closing-data"', html)

    def test_打ってあれば枚数と未送信を数えている(self) -> None:
        self.post("/api/entry/save", self.SHEET)
        body = self.get("/api/graph/review/closing").get_json()
        self.assertEqual(body["pages"], 1)
        self.assertEqual(body["unsynced"], 1)

    def test_直の終わりのぶんが空なら直しに戻すと言う(self) -> None:
        """**この画面は直の終わりに出ます。**

        最終時間と休憩(`save_checks.AT_SHIFT_END`)は、直が終わって
        いなければ必ず足りません。ここで「共有へどうぞ」と言うと、
        押した先で断られます ── 先に直すところへ戻します。
        """
        self.post("/api/entry/save", self.SHEET)
        body = self.get("/api/graph/review/closing").get_json()
        self.assertEqual(body["action"], shift_closing.ACT_FIX)
        self.assertFalse(body["can_push"])
        self.assertTrue(body["findings"])

    def test_断りの中身が画面まで来る(self) -> None:
        """何が残っているかを、その画面で読めるように。"""
        self.post("/api/entry/save", self.SHEET)
        html = self.get("/graph/review").get_data(as_text=True)
        self.assertIn("最終時間", html)

    def test_渡したあとは渡しましたと出る(self) -> None:
        body = self.get("/api/graph/review/closing?pushed=1").get_json()
        self.assertTrue(body["just_pushed"])

    def test_閉じた機会を覚える(self) -> None:
        res = self.post("/api/graph/review/ack", {"stage": "end"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["stage"], "end")

    def test_知らない機会は既定に倒す(self) -> None:
        """画面から来るものは信用しきらない。"""
        res = self.post("/api/graph/review/ack", {"stage": "へんなもの"})
        self.assertEqual(res.get_json()["stage"], shift_review.STAGE_WARN)


class LeaveAlwaysAnswersTests(unittest.TestCase):
    """**画面を出るときは、必ず答えを残す。**

    詰まりの正体はサーバではなく画面のほうでした ── 「直しに戻る」だけ
    ack を飛ばさずに `location.href` で出ていたので、戻った先で1分
    タイマーに連れ戻されます。サーバのテストでは捕まらないので、ここで
    **出口の形**を見ます(`test_stale_screen.py` と同じやり方)。
    """

    def source(self) -> str:
        path = (Path(__file__).resolve().parent.parent / "app" / "static" /
                "js" / "views" / "review.js")
        return path.read_text(encoding="utf-8")

    def test_覚えずに出る道を作っていない(self) -> None:
        self.assertNotIn("ack: false", self.source(),
                         "覚えずに閉じると、1分タイマーに連れ戻されます")

    def test_出口は1つで覚えたあとにある(self) -> None:
        """`location.href` は ack のあとに1回だけ。"""
        src = self.source()
        self.assertEqual(src.count("location.href"), 1,
                         "出口が増えています(覚えずに出る道ができます)")
        self.assertLess(src.index('"/api/graph/review/ack"'),
                        src.index("location.href"),
                        "覚える前に画面を出ています")


@unittest.skipUnless(HAS_FLASK, SKIP)
class NoLoopTests(WebTestCase):
    """**閉じたら、連れ戻さない。**

        直しに戻る押す → 一瞬だけ画面が切り替わるがすぐ実績画面に戻され
        何もできない

    「直しに戻る」だけ覚えさせない作りにしていたのが原因でした。戻った先で
    1分タイマーが「まだ出す用がある」と見て、そのまま連れ戻します ──
    直すところは**直してからでないと消えない**ので、必ずこうなります。
    """

    #: **紙は短く。** 直の窓は日の端で縮むので、紙が2時間もあると
    #: 「作業時間が直の規定時間を超えています」で保存できません
    #: (`logic/work_time.shift_limit`)。ここで見たいのは直の変わり目で、
    #: 紙の長さではありません
    SHEET = {"rows": {"1": {"LOT": "N7131T0", "KZ": "08", "KH": "00",
                            "SZ": "08", "SH": "10", "CON": "100",
                            "WEI": "1000"}},
             "header": {"worker": "山田"}, "checks": {}}

    def setUp(self) -> None:
        super().setUp()
        shift_review.reset()
        self.post("/api/entry/line", {"line": "L-1"})

    def tearDown(self) -> None:
        shift_review.reset()
        super().tearDown()

    def hhmm(self, when):
        return when.strftime("%H:%M")

    #: 直の終わりから15分前を切ると催促が出ます。ここで見たいのは
    #: 「終わったのに渡していない」ほうなので、終わりから**離して**置きます
    _TAIL = 60

    def put_now_in(self, which: str) -> None:
        """いまが `which` 直の中(終わりからは遠いところ)になるように動かす。

        時計は動かせないので、境界のほうを動かします(境界のテストと同じ手)。
        並べ方は `tests/_shift_clock.chain` に寄せてあります ── 素直に
        `いま ± 60分` で置くと真夜中の前後で日をまたぎ、`TimeCheck` の
        1直・2直に当たらず3直へ落ちるので、**夜中にだけ落ちて**いました。
        """
        from datetime import datetime

        from tests import _shift_clock as shift_clock

        now = datetime.now()
        windows = shift_clock.chain(now, which, head=60, tail=self._TAIL,
                                    width=120, min_width=self._TAIL + 1)
        if windows is None:
            self.skipTest(
                f"いまは {shift_clock.hhmm(now)} で、日をまたがない窓を"
                "3本並べられません(日の端のわずかなあいだだけ)")
        # **終わりから遠いこと**も確かめる。日の端では窓の終わりが 23:59 に詰まり、
        # いまの直が催促の時間(終わりの15分前から)に入る ── 前の直の呼び出しより
        # いまの直の催促が先に出て、23:45 過ぎにだけ落ちていました
        from datetime import timedelta

        from nippou.config import SETTINGS

        lead = timedelta(minutes=SETTINGS.print_warning_minutes)
        if windows[which][1] - now <= lead:
            self.skipTest(
                f"いまは {shift_clock.hhmm(now)} で、直の終わりまで"
                f"{SETTINGS.print_warning_minutes}分を取れません(日の端のわずかなあいだだけ)")
        shift_clock.write(self.repo(), windows)

    def test_終わった直の呼び出しは閉じたら止まる(self) -> None:
        self.put_now_in("1")
        self.post("/api/entry/save", self.SHEET)
        # 直が変わった。1直は終わったのに共有へ渡していない
        self.put_now_in("2")

        first = self.get("/api/graph/review").get_json()
        self.assertTrue(first["due"], "終わった直の呼び出しが出ていません")
        self.assertEqual(first["stage"], shift_review.STAGE_END)

        # 「直しに戻る」も「確認しました」も、ここを通ります
        self.post("/api/graph/review/ack", {"stage": first["stage"]})

        again = self.get("/api/graph/review").get_json()
        self.assertFalse(again["due"],
                         "閉じたのに、また連れ戻されます(画面が詰まります)")

    def test_覚える相手が確認の相手と同じ(self) -> None:
        """**ここがずれると、閉じても閉じても出ます。**

        画面は1直の話をしているのに覚えるのは2直、という食い違いです。
        """
        self.put_now_in("1")
        self.post("/api/entry/save", self.SHEET)
        self.put_now_in("2")

        state = self.get("/api/graph/review").get_json()
        closing = self.get("/api/graph/review/closing").get_json()
        self.assertTrue(state["due"])
        self.assertEqual((closing["report_date"], closing["shift"]),
                         (state["report_date"], state["shift"]))


if __name__ == "__main__":
    unittest.main()
