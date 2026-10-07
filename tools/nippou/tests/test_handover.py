"""直の引き継ぎ ── **押し忘れのまま次の直が進むのを止める関門**

【止めたい流れ】

    共有へ保存を押し忘れる(または打ち間違いで通らない)
      → そのまま直の時間を過ぎる
      → 次の直の人が来て、そのまま入力を始める
      → 前の直は共有へ出ないまま、誰も直さない
      → 直せるのは管理者だけ(運用でそう決まっている)
      → 翌月の集計まで、誰も気づかない

途中に「気づく人」が居ません。**必ず1人いる場所**は次の直の始まりだけ
なので、そこへ関門を置きます。

【関門が守る約束 ── ここで見るのはこの5つ】

    1. 前の直が共有へ出ていなければ、次の直は**入力を始められない**
    2. 出口は必ずある。**中身が綺麗なら「共有へ保存」1押し**(誰でも)
    3. 直せないものが残っていても**引き継げる**。誰が引き継いだかを残す
    4. **打ち始めた人は止めない。** 1ページでも保存があれば通す
    5. 引き継いでも**消えない。** 共有へ出るまで知らせは出続ける

4 が要ります ── 打ちかけを抱えたまま止められるのが、現場にとって
一番悪い止まり方だからです。
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.models import DetailRecord, HeaderRecord
from nippou.logic import handover
from nippou.logic.handover import Left
from tests._web import HEADERS, HAS_FLASK, SKIP_REASON, WebTestCase


# ======================================================================
# 1. 判断そのもの (Flask が無くても通る)
# ======================================================================
def _left(shift="1直", problems=(), taken_by="") -> Left:
    return Left(report_date="2026年9月17日", line="L-1", shift=shift,
                pages=1, problems=list(problems), taken_by=taken_by,
                taken_at="2026-09-17T22:10:00" if taken_by else "")


class DecideTests(unittest.TestCase):
    """`logic/handover.decide` ── **止めるのは3つそろったときだけ。**"""

    def test_残っていなければ何も言わない(self):
        gate = handover.decide([], started=False)
        self.assertFalse(gate.blocked)
        self.assertFalse(gate.any_left)
        self.assertEqual(gate.message, "")

    def test_残っていて打ち始める前なら止める(self):
        gate = handover.decide([_left()], started=False)
        self.assertTrue(gate.blocked)
        self.assertIn("共有へ出ていません", gate.message)
        self.assertIn("始められません", gate.message)

    def test_打ち始めていれば止めない(self):
        """**これが 4 番目の約束。** 打ちかけを抱えたまま止めない。"""
        gate = handover.decide([_left()], started=True)
        self.assertFalse(gate.blocked)
        # ただし知らせは残る ── 押し忘れは押し忘れのまま
        self.assertTrue(gate.any_left)
        self.assertIn("共有へ出ていません", gate.message)

    def test_見るだけの画面では止めない(self):
        """過去の直を開いている人に「片付けてください」は筋が違う。"""
        gate = handover.decide([_left()], started=False, read_only=True)
        self.assertFalse(gate.blocked)

    def test_引き継がれていれば止めない(self):
        """**出口を通ったあと。** 消えはしないが、通れる。"""
        gate = handover.decide([_left(problems=["1ページ 1行目 だめ"],
                                      taken_by="山田")], started=False)
        self.assertFalse(gate.blocked)
        self.assertTrue(gate.any_left)

    def test_1つでも引き継がれていなければ止める(self):
        gate = handover.decide(
            [_left(shift="1直", taken_by="山田"), _left(shift="2直")],
            started=False)
        self.assertTrue(gate.blocked)


class ActionsTests(unittest.TestCase):
    """出口 ── **中身が綺麗なら押すだけ、そうでなければ引き継ぐ。**"""

    def test_綺麗なら押すだけ(self):
        gate = handover.decide([_left()], started=False)
        self.assertEqual(gate.actions, [handover.ACTION_PUSH])
        self.assertTrue(gate.all_clean)
        self.assertIn("「共有へ保存」を押せば済みます", gate.message)
        self.assertIn("誰でも押せます", gate.message)

    def test_直すところがあれば引き継ぎも出す(self):
        gate = handover.decide([_left(problems=["1ページ 1行目 だめ"])],
                               started=False)
        self.assertIn(handover.ACTION_TAKE, gate.actions)
        self.assertFalse(gate.all_clean)
        self.assertIn("この画面からは直せません", gate.message)
        self.assertIn("引き継ぐ", gate.message)

    def test_引き継いでも消えないと書いてある(self):
        """**5番目の約束。** ここを書かないと「押せば消える」に読めます。"""
        gate = handover.decide([_left(problems=["だめ"])], started=False)
        self.assertIn("引き継いでも消えません", gate.message)

    def test_断りはどの直のものか分かる(self):
        gate = handover.decide(
            [_left(shift="1直", problems=["1ページ 1行目 だめ"])],
            started=False)
        self.assertEqual(gate.problems,
                         ["2026年9月17日 L-1 1直 1ページ 1行目 だめ"])

    def test_文言に印を混ぜない(self):
        """画面へそのまま出す文言。**Markdown の印を混ぜない。**"""
        for item in ([_left()], [_left(problems=["だめ"])]):
            self.assertNotIn("**", handover.decide(item, started=False).message)


class TakenNoteTests(unittest.TestCase):
    """引き継いだあとに残る一行。"""

    def test_引き継いでいなければ空(self):
        self.assertEqual(handover.taken_note(_left()), "")

    def test_誰がいつ引き継いだかを書く(self):
        note = handover.taken_note(_left(taken_by="山田"))
        self.assertIn("山田", note)
        self.assertIn("2026-09-17 22:10", note)

    def test_残っている件数も書く(self):
        note = handover.taken_note(
            _left(problems=["だめ", "これも"], taken_by="山田"))
        self.assertIn("2件", note)
        self.assertIn("管理者が直して共有へ出すまで消えません", note)


class WorkerOfTests(unittest.TestCase):
    def test_先に見つかったものを使う(self):
        self.assertEqual(handover.worker_of(["山田", "田中"]), "山田")

    def test_空は飛ばす(self):
        self.assertEqual(handover.worker_of(["", "  ", "田中"]), "田中")

    def test_無ければ空のまま(self):
        """**名前を作りません。** 誰か分からない引き継ぎは残す意味がない。"""
        self.assertEqual(handover.worker_of([None, "", "  "]), "")


# ======================================================================
# 2. 押したときにどうなるか (Flask が要る)
# ======================================================================
@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class HandoverGateTests(WebTestCase):
    """**入力を始められないこと**と、その出口。"""

    def now(self):
        from nippou import work_context

        from app.routes.entry import build_service, current_calculator

        ctx = work_context.get_context()
        with self.app.test_request_context():
            service = build_service(ctx, current_calculator())
            shift, day = service.current_shift_info(
                datetime.now(), ctx.force_day_shift())
        return day, ctx.line, shift

    def other_shift(self) -> str:
        _, _, shift = self.now()
        return "3直" if shift != "3直" else "1直"

    def leave_behind(self, *, broken: bool = False) -> tuple[str, str, str]:
        """前の直を**共有へ未送信のまま**置く。`broken` なら直すところつき。"""
        day, line, _ = self.now()
        shift = self.other_shift()
        key = dict(report_date=day, line=line, shift=shift, page=1)
        row = dict(row_no=1, lot="A1", kz="08", kh="00", sz="09", sh="00")
        if broken:
            # 梱包数 > 検入枚数。**「設備移動のため保存」でも通せない**もの
            row |= {"ken": "15", "mai": "10", "tut": "2"}
        self.repo().save(HeaderRecord(**key, worker="前の人"),
                         [DetailRecord(**key, **row)])
        return day, line, shift

    def sheet(self, lot="1111111") -> dict:
        return {"rows": {"1": {"LOT": lot, "KZ": "08", "KH": "00",
                               "SZ": "09", "SH": "00"}},
                "header": {"worker": "次の人"}, "checks": {}}

    def test_前の直を呼び出せば作業者が空でも表を伏せない(self):
        """**直す道が行き止まりにならない。**

        作業者が空の前の直を「記録を見る → 呼び出す」(管理者)で開くと、「まず作業者を
        選んでください」で12行の表が全部伏せられ、どこも直せなかった。作業者を先に
        選ばせるのは新しい直を始めるときだけ。
        """
        day, line, _ = self.now()
        shift = self.other_shift()
        key = dict(report_date=day, line=line, shift=shift, page=1)
        self.repo().save(HeaderRecord(**key, worker=""),
                         [DetailRecord(**key, row_no=1, kz="07", kh="00", sz="15", sh="00",
                                       s="2", th="480")])
        self.assertTrue(self.post("/api/entry/state", {}).get_json()["needs_worker"])  # いまの直は先に選ぶ
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        res = self.post("/api/settings/recall", {"report_date": day, "line": line, "shift": shift, "page": 1})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        body = self.post("/api/entry/state", {"header": {"worker": ""}}).get_json()
        self.assertFalse(body["read_only"])
        self.assertFalse(body["needs_worker"])
        self.assertFalse(body["handover"]["blocked"])
        html = self.client.get("/", headers=HEADERS).get_data(as_text=True)
        start = html.index('id="start-shift"')
        self.assertIn("hidden", html[start:start + 80])

    # ---- 1. 止まること ------------------------------------------------
    def test_前の直が残っていれば保存できない(self):
        self.leave_behind()
        res = self.post("/api/entry/save", self.sheet())
        self.assertEqual(res.status_code, 422)
        body = res.get_json()
        self.assertEqual(body["error"]["code"], "handover_required")
        self.assertTrue(body["handover"]["blocked"])

    def test_止めたなら1行も書かれない(self):
        """**これが要点。** 止めたのに書くなら、止める意味がない。"""
        day, line, _ = self.leave_behind()
        _, _, shift = self.now()
        self.post("/api/entry/save", self.sheet())
        self.assertEqual(self.repo().saved_pages(day, line, shift), [])

    def test_発行も止まる(self):
        """発行は保存の入口。**ここだけ素通りだと関門を回り込めます。**"""
        self.leave_behind()
        rows = {str(n): {"LOT": f"P{n:02d}", "CON": "10", "WEI": "100"}
                for n in range(1, 13)}
        rows["12"].update(KZ="08", KH="00", SZ="08", SH="30")   # 12行目は終了まで
        res = self.post("/api/entry/newpage",
                        {"rows": rows, "header": {"worker": "次の人"},
                         "checks": {}})
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"], "handover_required")

    def test_全停も止まる(self):
        """全停も1ページを書いて保存する ── 同じ関門を通します。"""
        self.leave_behind()
        res = self.post("/api/formstop/execute",
                        {"reason": "停電", "code": "1", "worker": "次の人"})
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"], "handover_required")

    def test_打ちかけを置くだけなら通る(self):
        """**見ることは止めません**(`draft`)。止めるのは確定のほうだけ。"""
        self.leave_behind()
        res = self.post("/api/entry/save", {**self.sheet(), "draft": True})
        self.assertEqual(res.status_code, 200)

    def test_自動保存は止めない(self):
        """誰も押していないところで止めても、打ちかけが残らないだけ。"""
        self.leave_behind()
        res = self.post("/api/entry/save", {**self.sheet(), "silent": True})
        self.assertEqual(res.status_code, 200)

    # ---- 2. 出口 ------------------------------------------------------
    def test_共有へ出ていれば止めない(self):
        """押し忘れが無ければ、そもそも関門は立ちません。"""
        day, line, shift = self.leave_behind()
        self.repo().mark_synced((day, line, shift, 1))
        res = self.post("/api/entry/save", self.sheet())
        self.assertEqual(res.status_code, 200)

    def test_引き継げば始められる(self):
        self.leave_behind(broken=True)
        taken = self.post("/api/entry/handover", {"worker": "次の人"})
        self.assertEqual(taken.status_code, 200, taken.get_json())
        res = self.post("/api/entry/save", self.sheet())
        self.assertEqual(res.status_code, 200, res.get_json().get("message"))

    def test_引き継いだ人が残る(self):
        day, line, shift = self.leave_behind(broken=True)
        self.post("/api/entry/handover", {"worker": "次の人"})
        found = self.repo().handover_of(day, line, shift)
        self.assertIsNotNone(found)
        self.assertEqual(found["taken_by"], "次の人")
        # 引き継いだ時点で何が残っていたかも残す
        self.assertIn("梱包数", found["findings"])

    def test_名前が無ければ引き継げない(self):
        """**誰か分からない引き継ぎは、残す意味がありません。**"""
        self.leave_behind(broken=True)
        res = self.post("/api/entry/handover", {"worker": ""})
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"], "no_worker")

    # ---- 3. 引き継いでも消えない --------------------------------------
    def test_引き継いでも共有へは出ていない(self):
        day, line, shift = self.leave_behind(broken=True)
        self.post("/api/entry/handover", {"worker": "次の人"})
        pending = {(h.report_date, h.line, h.shift)
                   for h in self.repo().pending_sync_headers()}
        self.assertIn((day, line, shift), pending)

    def test_引き継いだあとも画面に残る(self):
        self.leave_behind(broken=True)
        self.post("/api/entry/handover", {"worker": "次の人"})
        body = self.post("/api/entry/state",
                         {"rows": {}, "header": {}, "checks": {}}).get_json()
        self.assertTrue(body["handover"]["any_left"])
        self.assertFalse(body["handover"]["blocked"])
        self.assertTrue(body["handover"]["notes"])
        self.assertIn("次の人", body["handover"]["notes"][0])

    # ---- 4. 打ち始めた人は止めない ------------------------------------
    def test_1ページでも保存があれば止めない(self):
        day, line, shift = self.now()
        key = dict(report_date=day, line=line, shift=shift, page=1)
        self.repo().save(HeaderRecord(**key, worker="次の人"),
                         [DetailRecord(**key, row_no=1, lot="B1")])
        self.leave_behind()
        res = self.post("/api/entry/save", self.sheet())
        self.assertEqual(res.status_code, 200, res.get_json().get("message"))

    # ---- 5. 画面に出ること --------------------------------------------
    def test_画面が関門を描いている(self):
        self.leave_behind(broken=True)
        html = self.get("/").get_data(as_text=True)
        self.assertIn('id="handover"', html)
        self.assertIn('data-blocked="1"', html)
        self.assertIn("直せないまま引き継ぐ", html)

    def test_残っていなければ伏せてある(self):
        html = self.get("/").get_data(as_text=True)
        self.assertIn('id="handover"', html)
        self.assertNotIn('data-blocked="1"', html)

    def test_いまの直は残り物に数えない(self):
        """打っている最中の直を「共有へ出ていません」とは言いません。"""
        day, line, shift = self.now()
        key = dict(report_date=day, line=line, shift=shift, page=1)
        self.repo().save(HeaderRecord(**key, worker="次の人"),
                         [DetailRecord(**key, row_no=1, lot="B1")])
        body = self.post("/api/entry/state",
                         {"rows": {}, "header": {}, "checks": {}}).get_json()
        self.assertFalse(body["handover"]["any_left"])


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class ShiftTimesNoticeTests(WebTestCase):
    """控えの時刻で動いていることが、**入力画面に出ている**こと。"""

    def test_マスタが無ければ画面が言う(self) -> None:
        html = self.get("/").get_data(as_text=True)
        self.assertIn('id="shift-times-note"', html)
        self.assertIn("控えの時刻で動いています", html)

    def test_取り込んであれば黙る(self) -> None:
        for key, (start, end) in (("1", ("07:00", "15:00")),
                                  ("2", ("15:00", "22:50")),
                                  ("3", ("22:50", "07:00"))):
            self.repo().set_shift_time(key, start, end)
        html = self.get("/").get_data(as_text=True)
        self.assertIn('id="shift-times-note"', html)
        self.assertNotIn("控えの時刻で動いています", html)

    def test_応答にも入っている(self) -> None:
        body = self.post("/api/entry/state",
                         {"rows": {}, "header": {}, "checks": {}}).get_json()
        self.assertIn("shift_times_note", body)
