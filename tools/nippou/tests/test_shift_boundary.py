"""開いたままで直の変わり目をまたいだとき

【この不具合はどう見つかったか】
「ツールを開いたままにすることによっておこる不具合は想定されるか」と
訊かれて調べたところ、**同じ12行が1直と2直の両方に残る**ことが実際に
起きました。VBA は保存処理の最後で ``Unload UFdaily`` していたので、
フォームが 17:00 をまたいで生き残ることがまずありませんでした
(`9de1bb84-_______.txt` の ``ExecutePrintProcess``:
``graphF.Show`` → ``Unload UFProgress`` / ``Unload UFdaily``)。

【時計を動かさずに試す】
境界時刻のほうを動かします。サーバから見れば同じことです ──
``time_check`` は時計と境界を比べているだけなので、**いまが1直に入る
境界**と**いまが2直に入る境界**を順に置けば、17:00 をまたいだのと
同じ状態になります。テストを特定の時刻に縛らずに済むのが利点です。
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import shift_boundary as sb
from tests import _shift_clock as shift_clock
from tests._shift_clock import hhmm
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase


# ======================================================================
# 判断そのもの (Flask が無くても通る)
# ======================================================================
class CheckTests(unittest.TestCase):
    """`logic/shift_boundary.check` ── **またいだかどうかだけ。**"""

    def key(self, date="2026年9月12日", shift="1直", page=1):
        return sb.Key(date, shift, page)

    def test_同じ直ならまたいでいない(self):
        found = sb.check(self.key(), self.key())
        self.assertFalse(found.crossed)

    def test_ページが動いただけならまたいでいない(self):
        # 12行を使い切って2ページ目を出しただけ。直は変わっていない
        found = sb.check(self.key(page=1), self.key(page=2))
        self.assertFalse(found.crossed)

    def test_直が変わればまたいでいる(self):
        found = sb.check(self.key(shift="1直"), self.key(shift="2直"))
        self.assertTrue(found.crossed)
        self.assertEqual(found.kind, "shift")

    def test_報告日が変わればまたいでいる(self):
        found = sb.check(self.key(date="2026年9月11日"),
                         self.key(date="2026年9月12日"))
        self.assertTrue(found.crossed)
        self.assertEqual(found.kind, "date")

    def test_3直から1直は両方変わる(self):
        """**08:00 は直も報告日も動く。** 3直の朝は前日付。"""
        found = sb.check(self.key(date="2026年9月11日", shift="3直"),
                         self.key(date="2026年9月12日", shift="1直"))
        self.assertEqual(found.kind, "both")
        self.assertIn("報告日と直", found.message)

    def test_画面が名乗らなければまたいでいない扱い(self):
        """開いた直後・この仕組みより前の画面。**断ると保存できなくなる。**"""
        self.assertFalse(sb.check(None, self.key()).crossed)
        self.assertFalse(sb.check(sb.Key(), self.key()).crossed)
        self.assertFalse(sb.check(self.key(), sb.Key()).crossed)

    def test_文言に両方の直が出る(self):
        found = sb.check(self.key(shift="1直"), self.key(shift="2直"))
        self.assertIn("1直", found.message)
        self.assertIn("2直", found.message)

    def test_選択肢は2つで結果が書いてある(self):
        """**「はい / いいえ」にしない。** 押した人に結果が見えなくなる。"""
        found = sb.check(self.key(shift="1直", page=2),
                         self.key(shift="2直", page=1))
        values = [c["value"] for c in found.choices]
        self.assertEqual(values, [sb.CHOICE_OPENED, sb.CHOICE_CURRENT])
        for choice in found.choices:
            self.assertTrue(choice["label"])
            self.assertTrue(choice["note"], "何が起きるかを書いていない")
        # ページまで書く ── どの紙に足されるのかが分かれ目になる
        self.assertIn("ページ2", found.choices[0]["note"])
        self.assertIn("ページ1", found.choices[1]["note"])


class ResolveTests(unittest.TestCase):
    """`resolve` ── **選ばれるまで `None`。**"""

    def setUp(self):
        self.crossing = sb.check(sb.Key("2026年9月12日", "1直", 2),
                                 sb.Key("2026年9月12日", "2直", 1))

    def test_選ばれていなければNone(self):
        """**「選ばれていない」と「今の直」を同じ値にしない。**

        同じにすると、聞き返す前に書いてしまいます。
        """
        self.assertIsNone(sb.resolve(self.crossing, ""))
        self.assertIsNone(sb.resolve(self.crossing, "なにか"))

    def test_打っていた直を選べる(self):
        chosen = sb.resolve(self.crossing, sb.CHOICE_OPENED)
        self.assertEqual((chosen.shift, chosen.page), ("1直", 2))

    def test_今の直を選べる(self):
        chosen = sb.resolve(self.crossing, sb.CHOICE_CURRENT)
        self.assertEqual((chosen.shift, chosen.page), ("2直", 1))

    def test_またいでいなければ今の直をそのまま返す(self):
        same = sb.check(sb.Key("2026年9月12日", "1直"),
                        sb.Key("2026年9月12日", "1直"))
        self.assertEqual(sb.resolve(same, "").shift, "1直")


# ======================================================================
# 画面と道 (Flask が要る)
# ======================================================================
#: 直として置きたい窓の幅。端に寄ると縮みます
_WIDTH = 60

#: 打つ紙の長さ(09:00〜09:10)と、直に要る最小の長さ。
#:
#: **紙より短い直には入りません** ── 保存前チェックが「作業時間が直の
#: 規定時間を超えています」で断ります(`logic/work_time.shift_limit`)。
#: 紙を60分にしていたので、直も60分ずつ要りました ── その3本ぶんが
#: 真夜中すぎに入らず、**夜中にだけ落ちて**いました。試しているのは
#: 直の変わり目であって紙の長さではないので、紙を短くします。
_PAPER = 10
_MIN_WIDTH = _PAPER + 2

#: いまを直の**入口寄り**(始まりの5分後)に置きます。
#:
#: 「直が変わったばかり」を作りたいからです ── 1つ前の直は5分前に
#: 終わったところ、という形。真ん中に置くと30分前に終わったことになり、
#: またいだ直後の話をしているつもりが、そうでなくなります。
_HEAD = 5


def day_window(now: datetime):
    """`now` を含む60分の窓。**日をまたがない形**で返す。作れなければ None。

    中身は `tests/_shift_clock` に移しました ── 同じ下ごしらえを
    `test_shift_anchor` と `test_shift_closing` も持っていて、3つとも
    夜中に落ちていたためです。ここは呼び名を残すだけにします。
    """
    return shift_clock.window_around(now, head=_HEAD, tail=_WIDTH - _HEAD,
                                     min_width=_WIDTH)


class DayWindowTests(unittest.TestCase):
    """下ごしらえそのもの ── **1日のどの時刻でも組めること。**

    このテストは「テストが落ちないこと」を確かめるテストです。ふつうは
    書きませんが、ここは**時刻に依る**ので書きます ── 夜中にだけ落ちる
    仕掛けが入っていて、それに気づくまでに夜の実行が何度も赤くなりました。
    走らせる時刻で結果が変わるテストは、そのうち誰も見なくなります。
    """

    def minutes_of_day(self):
        base = datetime(2026, 9, 13, 0, 0, 30)
        return [base + timedelta(minutes=n) for n in range(24 * 60)]

    def layout(self, now: datetime):
        return shift_clock.chain(now, "2", head=_HEAD, tail=_WIDTH - _HEAD,
                                 width=_WIDTH, min_width=_MIN_WIDTH,
                                 prev_min_width=_MIN_WIDTH)

    def test_組めないのは日の端だけ(self) -> None:
        """**組めない時刻を、数えて書き出しておきます。**

        「1つ前の直」はいまより前に終わっていなければならないので、
        日の初めの十数分は置き場所がありません(日付が変わったばかりの
        ときに「今日のうちに終わった直」は作れません)。日の終わりも
        同じだけ要ります。**そこだけは呼ぶ側が理由を言って飛ばします。**
        """
        cannot = [hhmm(now) for now in self.minutes_of_day()
                  if self.layout(now) is None]
        self.assertEqual(cannot[0], "00:00")
        self.assertEqual(cannot[-1], "23:59")
        self.assertLessEqual(len(cannot), 30, "組めない時刻が増えています")

    def test_窓は日をまたがない(self) -> None:
        """またぐと `TimeCheck` の 1直・2直に当たらず、3直へ落ちます。"""
        for now in self.minutes_of_day():
            windows = self.layout(now)
            if windows is None:
                continue
            for key, (start, end) in windows.items():
                with self.subTest(now=hhmm(now), shift=key):
                    self.assertEqual(start.date(), now.date())
                    self.assertEqual(end.date(), now.date())
                    self.assertLess(start, end)

    def test_いまの直はいまを含む(self) -> None:
        for now in self.minutes_of_day():
            windows = self.layout(now)
            if windows is None:
                continue
            start, end = windows["2"]
            with self.subTest(now=hhmm(now)):
                # `TimeCheck` は `start < t < end`(両端を含まない)
                self.assertLess(start, now)
                self.assertGreater(end, now)

    def test_どの直も紙より長い(self) -> None:
        """**紙より短い直には入りません。** 保存前チェックが断ります。"""
        for now in self.minutes_of_day():
            windows = self.layout(now)
            if windows is None:
                continue
            for key in ("1", "2"):
                with self.subTest(now=hhmm(now), shift=key):
                    self.assertGreater(windows[key][1] - windows[key][0],
                                       timedelta(minutes=_PAPER))

    def test_1つ前の直は終わっている(self) -> None:
        """**ここが本題。** 「またいだ直後」は1つ前が終わっていること。"""
        for now in self.minutes_of_day():
            windows = self.layout(now)
            if windows is None:
                continue
            with self.subTest(now=hhmm(now)):
                self.assertLessEqual(windows["1"][1], now)

    def test_どの窓も重ならない(self) -> None:
        """重なると `TimeCheck` が先に当たったほうを返して、話が狂います。"""
        for now in self.minutes_of_day():
            windows = self.layout(now)
            if windows is None:
                continue
            spans = sorted(windows.values())
            with self.subTest(now=hhmm(now)):
                for (_, end), (start, _) in zip(spans, spans[1:]):
                    self.assertLessEqual(end, start)


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class BoundaryWebTestCase(WebTestCase):
    """境界時刻を動かして 17:00 をまたいだ状態を作る。"""

    def put_now_in(self, shift_key: str) -> None:
        """いまの時刻が `shift_key` の中に入るように境界を置き直す。

        置き方は `tests/_shift_clock.chain` に寄せてあります ──
        **窓は必ずその日の中**に収め、1つ前の直はその真ん前(=たった今
        終わったところ)に置きます。日をまたぐ窓を作ると `TimeCheck` の
        1直・2直に当たらず3直へ落ちるので、**夜中にだけ落ちる**テストに
        なります。

        日の端の10分だけはどう置いても組めないので、そのときは理由を
        言って飛ばします ── **時刻に依るテストは、黙って落ちるのが
        いちばん悪い**(そのうち誰も赤を見なくなります)。
        """
        now = datetime.now()
        windows = shift_clock.chain(now, shift_key, head=_HEAD,
                                    tail=_WIDTH - _HEAD, width=_WIDTH,
                                    min_width=_MIN_WIDTH,
                                    prev_min_width=_MIN_WIDTH)
        if windows is None:
            self.skipTest(
                f"いまは {hhmm(now)} で、日をまたがない窓を3本並べられません"
                "(日の端のわずかなあいだだけ)")
        shift_clock.write(self.repo(), windows)

    def sheet(self, lot: str = "1111111", **extra) -> dict:
        """打ちかけの1行。**時間の断りが出ない形**にしておく。"""
        payload = {
            "rows": {"1": {"LOT": lot, "ZAI": "SPCC", "SIZ": "1.0",
                           "KEN": "10", "KZ": "09", "KH": "00",
                           "SZ": "09", "SH": "10", "HIT": "1",
                           "MAI": "10", "TUT": "1"}},
            "header": {"worker": "作業者A"}, "checks": {},
        }
        payload.update(extra)
        return payload

    def opened(self, report_date: str, shift: str, page: int = 1) -> dict:
        return {"report_date": report_date, "shift": shift, "page": page}

    def keys(self) -> list[tuple[str, str, int]]:
        from nippou import constants

        return sorted((row["report_date"], row["shift"], row["page"])
                      for row in self.repo().saved_keys(constants.LINE_NAMES[0]))

    def clear_anchor(self) -> None:
        """**固定した直を外す。**

        作業者を選ぶと書き先の直が固定され(`logic/shift_anchor`)、直の
        変わり目の聞き返しは起きなくなります。そちらが本筋ですが、
        **固定が無い画面**(作業者がまだ決まっていない・古い画面)では
        今までどおり聞き返す必要があるので、そこを試すために外します。
        """
        from nippou import work_context
        from nippou.logic.shift_anchor import Anchor

        work_context.get_context().anchor = Anchor()

    def settle_shared(self) -> None:
        """打ってあるぶんを**共有へ出したことにする**。

        直の変わり目で「今の直へ入れる」を選ぶと、前の直は共有へ未送信の
        まま残ります ── そこは **前の直の引き継ぎ**(`logic/handover.py`)が
        止める場面で、こちらのテストが見たいのは境界の動きのほうです。
        片付いた状態から始めれば、見たいものだけが動きます。

        引き継ぎ側の関門そのものは `test_handover.py` で見ます。
        """
        repo = self.repo()
        for header in repo.pending_sync_headers():
            repo.mark_synced((header.report_date, header.line, header.shift,
                              header.page))


class SaveGateTests(BoundaryWebTestCase):
    """保存の関門。**書く前に止めて選ばせる。**"""

    def test_固定してあれば訊かれない(self):
        """**作業者を選んだ時点で書き先は決まっています**(`shift_anchor`)。

        「どちらの直へ入れますか」は、書き先が時計まかせだったから必要
        でした ── 15:00 をまたいだ瞬間に書き先が動くので、打っている人に
        訊くしかなかった。固定してあるなら動かないので、訊くことがありません。

        **またいだ先で返るのは 409 ではなく 403。** 固定した1直は 5分前に
        終わっているので(猶予は置きません)、訊く相手がいません ──
        「どちらへ入れますか」ではなく「その直は終わっています」です。
        """
        self.put_now_in("1")
        first = self.post("/api/entry/save", self.sheet()).get_json()
        self.assertTrue(first["saved"])
        self.assertEqual(first["shift"], "1直")

        self.put_now_in("2")
        res = self.post("/api/entry/save",
                        self.sheet(opened=self.opened(first["report_date"], "1直")))
        body = res.get_json()
        self.assertNotEqual(res.status_code, 409, "固定してあるのに訊いている")
        self.assertEqual(res.status_code, 403)
        self.assertIn("終わっています", body["message"])
        # **書き先は動いていない。** 断りの画面まで1直のまま返る
        self.assertEqual(body["shift"], "1直")
        self.assertEqual(body["ribbon"]["shift"], "1直")

    def test_終わった直へは自動保存も書かない(self):
        """**誰も押していないぶんこそ止めます。**

        自動保存は 200 で「見送りました」を返す形ですが、終わった直へは
        そもそも書きません ── 忘れて立ち去った画面が、裏で書き足し続ける
        ことになります。
        """
        self.put_now_in("1")
        self.post("/api/entry/save", self.sheet())
        before = self.keys()

        self.put_now_in("2")
        res = self.post("/api/entry/save", self.sheet(lot="2222222", silent=True))
        self.assertEqual(res.status_code, 403)
        self.assertEqual(self.keys(), before, "終わった直へ書き足している")

    def test_固定していなければ今までどおり訊く(self):
        """作業者がまだ決まっていない画面(固定が無い)。**古い道を残す。**"""
        self.put_now_in("1")
        first = self.post("/api/entry/save", self.sheet()).get_json()
        report_date = first["report_date"]
        self.clear_anchor()

        self.put_now_in("2")
        res = self.post("/api/entry/save", {
            **self.sheet(opened=self.opened(report_date, "1直")),
            "header": {"worker": ""}})
        self.assertEqual(res.status_code, 409)
        body = res.get_json()
        self.assertTrue(body["shift_changed"]["crossed"])
        self.assertFalse(body["saved"])
        self.assertIn("直が変わりました", body["message"])

    def test_止まっているあいだは1行も書かれない(self):
        """**これが要点。** 訊いている最中に書くなら、訊く意味がない。"""
        self.put_now_in("1")
        self.post("/api/entry/save", self.sheet())
        before = self.keys()

        self.put_now_in("2")
        report_date = before[0][0]
        self.post("/api/entry/save",
                  self.sheet(lot="2222222", opened=self.opened(report_date, "1直")))
        self.assertEqual(self.keys(), before, "断ったのに書かれている")

    def test_打っていた直を選べば元の直へ入る(self):
        self.put_now_in("1")
        first = self.post("/api/entry/save", self.sheet()).get_json()
        report_date = first["report_date"]
        self.clear_anchor()

        self.put_now_in("2")
        body = self.post("/api/entry/save", self.sheet(
            lot="2222222", opened=self.opened(report_date, "1直"),
            shift_choice="opened")).get_json()
        self.assertTrue(body["saved"])
        self.assertEqual(body["shift"], "1直")
        self.assertEqual(self.keys(), [(report_date, "1直", 1)])

    def test_打ちかけも選べば元の直へ置ける(self):
        """v4.24.0: 移る前・読み直す前の保存は、訊いたうえで打ちかけのまま置く。

        選ばずに送った打ちかけは今までどおり見送り(どちらへも書かない)、
        選んだ答えが付いていれば、その直へ**確定の関門を通さずに**置きます。
        """
        self.put_now_in("1")
        first = self.post("/api/entry/save", self.sheet()).get_json()
        report_date = first["report_date"]
        self.clear_anchor()

        self.put_now_in("2")
        skipped = self.post("/api/entry/save", self.sheet(
            lot="2222222", draft=True, header={"worker": ""},
            opened=self.opened(report_date, "1直"))).get_json()
        self.assertFalse(skipped["saved"])
        self.assertTrue(skipped["shift_changed"]["crossed"])

        placed = self.post("/api/entry/save", self.sheet(
            lot="2222222", draft=True, header={"worker": ""},
            opened=self.opened(report_date, "1直"), shift_choice="opened")).get_json()
        self.assertTrue(placed["saved"])
        self.assertEqual(placed["shift"], "1直")
        self.assertEqual(self.keys(), [(report_date, "1直", 1)])
        from nippou import constants

        _header, details = self.repo().load(report_date, constants.LINE_NAMES[0], "1直", 1)
        self.assertEqual(details[0].lot, "2222222")

    def test_選んだその1回は終わった直でも通る(self):
        """**押した人のぶんは行き先を残します。**

        直の変わり目で「打っていた直へ入れる」を押したのなら、打ってある
        12行はその直のものです。終わった直だからと断ると、行き先は管理者
        モードの中にしか無くなります ── 打った本人には開けません。

        通すのは押したその1回だけ。**固定は選ばれた直に替わる**ので、
        返ってきた画面はもう見るだけです(次は断られます)。
        """
        self.put_now_in("1")
        first = self.post("/api/entry/save", self.sheet()).get_json()
        report_date = first["report_date"]
        self.clear_anchor()

        self.put_now_in("2")                 # 1直は5分前に終わっている
        res = self.post("/api/entry/save", self.sheet(
            lot="2222222", opened=self.opened(report_date, "1直"),
            shift_choice="opened"))
        self.assertEqual(res.status_code, 200, res.get_json().get("message"))
        body = res.get_json()
        self.assertTrue(body["saved"])
        self.assertEqual(body["shift"], "1直")
        # **通ったその場で閉じる。** 続きを打てるようにはしません
        self.assertTrue(body["read_only"], "通したまま開いている")
        self.assertEqual(body["read_only_reason"], "anchor")

        # もう一度押しても、今度は断られる(選び直したわけではないので)
        again = self.post("/api/entry/save", self.sheet(
            lot="3333333", opened=self.opened(report_date, "1直")))
        self.assertEqual(again.status_code, 403)

    def test_今の直を選べば新しい紙になる(self):
        self.put_now_in("1")
        first = self.post("/api/entry/save", self.sheet()).get_json()
        report_date = first["report_date"]
        self.settle_shared()          # 前の直の引き継ぎは別のテストで見る
        # 固定が無い形にする ── 固定してあれば聞き返し自体が起きません
        self.clear_anchor()

        self.put_now_in("2")
        body = self.post("/api/entry/save", self.sheet(
            lot="2222222", opened=self.opened(report_date, "1直"),
            shift_choice="current")).get_json()
        self.assertTrue(body["saved"])
        self.assertEqual(body["shift"], "2直")
        self.assertEqual(self.keys(),
                         [(report_date, "1直", 1), (report_date, "2直", 1)])

    def test_画面が名乗らなければ今までどおり通る(self):
        """古い画面・開いた直後。**保存できない画面を作らない。**"""
        self.put_now_in("1")
        res = self.post("/api/entry/save", self.sheet())
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["saved"])

    def test_またいでいなければ訊かない(self):
        self.put_now_in("1")
        first = self.post("/api/entry/save", self.sheet()).get_json()
        res = self.post("/api/entry/save", self.sheet(
            opened=self.opened(first["report_date"], "1直")))
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["saved"])

    def test_書いた先が画面にも返る(self):
        """**「1直へ書いたのに画面は2直」を作らない。**"""
        self.put_now_in("1")
        first = self.post("/api/entry/save", self.sheet()).get_json()
        report_date = first["report_date"]

        self.put_now_in("2")
        body = self.post("/api/entry/save", self.sheet(
            opened=self.opened(report_date, "1直"),
            shift_choice="opened")).get_json()
        self.assertEqual(body["shift"], "1直")
        self.assertEqual(body["ribbon"]["shift"], "1直",
                         "帯が書いた先と食い違っている")


class AutosaveTests(BoundaryWebTestCase):
    """自動保存。**訊かない・書かない・黙って見送る。**"""

    def test_またいだら自動保存は見送られる(self):
        self.put_now_in("1")
        first = self.post("/api/entry/save", self.sheet()).get_json()
        report_date = first["report_date"]
        before = self.keys()
        # **固定を外して**、書き先が時計まかせの形にする ── 固定して
        # あれば、またぐこと自体が起きません(上のテスト)
        self.clear_anchor()

        self.put_now_in("2")
        res = self.post("/api/entry/save", self.sheet(
            lot="2222222", silent=True, header={"worker": ""},
            opened=self.opened(report_date, "1直")))
        # **200 で返す。** 断りではなく「いま書くべきでない」だけ
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertFalse(body["saved"])
        self.assertIn("直が変わった", body["skipped"])
        self.assertTrue(body["shift_changed"]["crossed"])
        self.assertEqual(self.keys(), before, "自動保存が勝手に書いている")


class PageMoveTests(BoundaryWebTestCase):
    """当直のページを開いたまま、またいだとき。

    **これまでは 403「他の直のデータです」でした。** さっきまで自分の直
    だった紙が、時計が進んだだけで他人の記録になり、しかも理由が
    分かりませんでした。
    """

    def test_ページを開いたままでも403ではなく409(self):
        self.put_now_in("1")
        first = self.post("/api/entry/save", self.sheet()).get_json()
        report_date = first["report_date"]
        opened = self.post("/api/settings/page", {"page": 1})
        self.assertEqual(opened.status_code, 200)

        self.put_now_in("2")
        res = self.post("/api/entry/save", self.sheet(lot="2222222"))
        self.assertEqual(res.status_code, 409)
        body = res.get_json()
        self.assertTrue(body["shift_changed"]["crossed"])
        self.assertEqual(body["shift_changed"]["opened"]["report_date"],
                         report_date)

    def test_打っていた直を選べばページごと保存できる(self):
        self.put_now_in("1")
        first = self.post("/api/entry/save", self.sheet()).get_json()
        report_date = first["report_date"]
        self.post("/api/settings/page", {"page": 1})

        self.put_now_in("2")
        body = self.post("/api/entry/save", self.sheet(
            lot="2222222", shift_choice="opened")).get_json()
        self.assertTrue(body["saved"])
        self.assertEqual(self.keys(), [(report_date, "1直", 1)])

    def test_今の直を選べば呼出は解ける(self):
        """**解かないと、次の保存でまた同じことを訊かれます。**"""
        self.put_now_in("1")
        self.post("/api/entry/save", self.sheet())
        self.post("/api/settings/page", {"page": 1})
        self.settle_shared()          # 前の直の引き継ぎは別のテストで見る

        self.put_now_in("2")
        body = self.post("/api/entry/save", self.sheet(
            lot="2222222", shift_choice="current")).get_json()
        self.assertTrue(body["saved"])
        self.assertFalse(body["recall"], "呼出が残っている")
        # もう一度押しても訊かれない
        again = self.post("/api/entry/save", self.sheet(
            lot="2222222", opened=self.opened(body["report_date"], "2直")))
        self.assertEqual(again.status_code, 200)

    def test_わざわざ過去を開いたぶんは今までどおり403(self):
        """**他人の記録に触る話は別。** そちらは断るのが正しい。"""
        self.put_now_in("1")
        self.post("/api/entry/save", self.sheet())
        # 管理者で過去を開き、管理者モードを解いてから保存する
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        from nippou import constants
        saved = self.repo().saved_keys(constants.LINE_NAMES[0])[0]
        self.post("/api/settings/recall", {
            "report_date": saved["report_date"], "line": constants.LINE_NAMES[0],
            "shift": saved["shift"], "page": saved["page"]})
        self.post("/api/settings/admin", {"enable": False})

        self.put_now_in("2")
        res = self.post("/api/entry/save", self.sheet(lot="2222222"))
        self.assertEqual(res.status_code, 403)
        self.assertIn("管理者モード", res.get_json()["message"])


class NewPageTests(BoundaryWebTestCase):
    """新規発行。**ここでは選ばせず、保存へ送る。**"""

    def test_またいでいたら新しいページは作らせない(self):
        self.put_now_in("1")
        first = self.post("/api/entry/save", self.sheet()).get_json()
        report_date = first["report_date"]
        before = self.keys()

        self.put_now_in("2")
        res = self.post("/api/entry/newpage", self.sheet(
            opened=self.opened(report_date, "1直")))
        self.assertEqual(res.status_code, 409)
        self.assertIn("保存(確定)", res.get_json()["error"]["message"])
        self.assertEqual(self.keys(), before)


class ShiftKeyRouteTests(BoundaryWebTestCase):
    """`GET /api/shift/key` ── **押さなくても効く見張りの口。**"""

    def test_いまの直と帯を返す(self):
        self.put_now_in("1")
        body = self.get("/api/shift/key").get_json()
        self.assertEqual(body["shift"], "1直")
        self.assertEqual(body["ribbon"]["shift"], "1直")
        self.assertFalse(body["shift_changed"]["crossed"])

    def test_画面の直を添えるとまたいだか教える(self):
        self.put_now_in("1")
        first = self.get("/api/shift/key").get_json()
        self.put_now_in("2")
        body = self.get(
            f"/api/shift/key?report_date={first['report_date']}&shift=1直"
        ).get_json()
        self.assertTrue(body["shift_changed"]["crossed"])
        self.assertIn("2直", body["shift_changed"]["message"])

    def test_トークンが要る(self):
        from tests._web import NO_TOKEN_HEADERS

        res = self.client.get("/api/shift/key", headers=NO_TOKEN_HEADERS)
        self.assertEqual(res.status_code, 401)


class RollOverTests(BoundaryWebTestCase):
    """直が変わったら管理者モードを落とす (VBA の `Unload UFdaily` 相当)。"""

    def test_直が変わると管理者モードが解ける(self):
        from nippou import work_context

        self.put_now_in("1")
        self.get("/")                            # いまの直を覚えさせる
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        self.assertTrue(work_context.get_context().admin)

        self.put_now_in("2")
        self.get("/api/shift/key")               # 押さなくても通る道
        self.assertFalse(work_context.get_context().admin,
                         "直が変わったのに管理者モードが残っている")

    def test_マスタ編集の許可も落ちる(self):
        from nippou import work_context

        self.put_now_in("1")
        self.get("/")
        ctx = work_context.get_context()
        ctx.master_edit = True

        self.put_now_in("2")
        self.get("/api/shift/key")
        self.assertFalse(work_context.get_context().master_edit)

    def test_呼出モードは落とさない(self):
        """**落とすと保存先が黙って動く。** VBA も落としていなかった

        (``g_NDB_RecallMode`` は標準モジュールの変数で、``Unload UFdaily``
        では消えない)。またいだことは保存の関門が止めます。
        """
        from nippou import work_context

        self.put_now_in("1")
        self.post("/api/entry/save", self.sheet())
        self.post("/api/settings/page", {"page": 1})
        self.assertTrue(work_context.get_context().recall.active)

        self.put_now_in("2")
        self.get("/api/shift/key")
        self.assertTrue(work_context.get_context().recall.active,
                        "呼出が黙って解けている(保存先が動く)")

    def test_ラインを変えただけでは落ちない(self):
        """丸徳・トット・バランサーは常に日勤。**直の名前が変わるだけ。**

        これを「直が変わった」と読むと、**ラインを変えたその操作のために
        開けた管理者モードが、その場で落ちます**(ラインの切り替えは
        管理者モード専用なので、以後1回も変えられなくなる)。
        """
        from nippou import work_context

        self.put_now_in("1")
        self.get("/")
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        res = self.post("/api/entry/line", {"line": "中板", "maru_sub": "3"})
        self.assertEqual(res.status_code, 200)
        self.get("/")
        self.assertTrue(work_context.get_context().admin,
                        "ラインを変えただけで管理者モードが落ちている")

    def test_起動して最初の1回では落とさない(self):
        """「変わった」のではなく、まだ一度も見ていなかっただけ。"""
        from nippou import work_context

        self.put_now_in("1")
        ctx = work_context.get_context()
        ctx.admin = True
        self.get("/")
        self.assertTrue(work_context.get_context().admin)


class ScreenTests(BoundaryWebTestCase):
    """画面が「自分は何の直か」を名乗っているか。"""

    def test_日報入力は自分の直を名乗る(self):
        self.put_now_in("1")
        html = self.get("/").get_data(as_text=True)
        self.assertIn('id="sheet-key"', html)
        self.assertIn('data-opened-date=', html)
        self.assertIn('data-opened-shift="1直"', html)

    def test_帯は応答のたびに返る(self):
        """サーバが描いたきりだと、またいだときに嘘が残る。"""
        self.put_now_in("1")
        body = self.post("/api/entry/state", self.sheet()).get_json()
        self.assertIn("ribbon", body)
        self.assertEqual(body["ribbon"]["shift"], "1直")

    def test_直が変わったの帯が外枠にある(self):
        """**トーストにしない。** 数秒で消えると、離れているあいだに消える。"""
        html = self.get("/").get_data(as_text=True)
        self.assertIn('id="shift-moved"', html)
        # 閉じる釦は付けない ── 消せるようにすると消して忘れる
        self.assertNotIn('data-close="shift-moved"', html)

    def test_どちらに入れるかの窓がある(self):
        html = self.get("/").get_data(as_text=True)
        self.assertIn('id="shift-modal"', html)
        self.assertIn('id="shift-modal-choices"', html)

    def test_集計もグラフもいつの数字かを出す(self):
        for path, marker in (("/graph", "graph-asof"), ("/agg", "agg-asof")):
            with self.subTest(path=path):
                html = self.get(path).get_data(as_text=True)
                self.assertIn(f'id="{marker}"', html)
                self.assertIn("時点", html)
                self.assertIn('data-opened-shift=', html)


if __name__ == "__main__":                       # pragma: no cover
    unittest.main()
