"""共有へ保存のあと: **グラフを画面いっぱいに出す**・**入力画面に「この直は共有へ保存済み」を残す**

    VBAでは入力完了後に保存で集計後のグラフを前面に出してました。現ツールでは共有保存を
    してもそうはなりません。
    1. その直集計後のグラフは出ないのか?
    2. VBAでは直終わりにグラフが出ていることで保存ボタンを押した確認にもなっていたが、
       現ツールでは何をもってこの直を打ち切ったと確認するのか?

現場の答えは「両方入れてください」:

    A. 入力画面の「共有へ保存」(いまの直)が通ったら、送ったその直の実績の確認画面へ連れて行く
       (`routes/settings._after_push_review` が道を返す)。送れなかった・一部だけ・過去分のときは出さない
    B. 入力画面に「この直は共有へ保存済み(16:52 山田)」を出し続ける(`logic/push_mark`)。
       保存したあとに直したら「共有へ保存したあとに直した行があります…」に変わる

画面の動きは `test_push_after_review_browser`(本物のブラウザ)。
"""
from __future__ import annotations

import re
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import push_mark  # noqa: E402
from tests.test_scenario_shift_flow import (ScenarioCase, clean_shift_rows,  # noqa: E402
                                            row, rows_of)

DAY = "2026年10月5日"


def _h(result: str, at: str, by: str = "山田", worker: str = "山田") -> dict:
    return {"result": result, "pressed_at": at, "pressed_by": by, "worker": worker}


# ======================================================================
# B. 印の決まり(純粋)
# ======================================================================
class DecideTests(unittest.TestCase):
    NOW = datetime(2026, 10, 5, 17, 0)

    def decide(self, **kw) -> push_mark.PushMark:
        kw.setdefault("now", self.NOW)
        return push_mark.decide(**kw)

    def test_保存したページが無ければ出さない(self) -> None:
        mark = self.decide(pages=0, unsynced=0, history=[_h("送れた", "2026-10-05T16:52:00")])
        self.assertFalse(mark.shown)
        self.assertEqual(mark.text, "")

    def test_送ったあと手元が共有と同じなら_保存済みと時刻と押した人(self) -> None:
        mark = self.decide(pages=2, unsynced=0, history=[_h("送れた", "2026-10-05T16:52:10")])
        self.assertEqual(mark.state, push_mark.STATE_SAVED)
        self.assertEqual(mark.text, "この直は共有へ保存済み(16:52 山田)")
        self.assertEqual(mark.level, "ok")

    def test_最後に押した時刻を出す_送るもの無しで押し直しても保存済み(self) -> None:
        mark = self.decide(pages=1, unsynced=0, history=[
            _h("送れた", "2026-10-05T14:00:00", by="佐藤"),
            _h("送るもの無し", "2026-10-05T16:30:00", by="山田")])
        self.assertEqual(mark.text, "この直は共有へ保存済み(16:30 山田)")

    def test_送ったあとに直した行があれば_もう一度押すように言う(self) -> None:
        mark = self.decide(pages=1, unsynced=1, history=[_h("送れた", "2026-10-05T16:52:00")])
        self.assertEqual(mark.state, push_mark.STATE_STALE)
        self.assertEqual(mark.text, "共有へ保存したあとに直した行があります。"
                                    "もう一度「共有へ保存」を押してください")
        self.assertEqual(mark.level, "todo")

    def test_最後に送れなかったなら_そう言う(self) -> None:
        mark = self.decide(pages=2, unsynced=1, history=[
            _h("送れた", "2026-10-05T14:00:00"),
            _h("一部送れなかった", "2026-10-05T16:52:00")])
        self.assertEqual(mark.state, push_mark.STATE_FAILED)
        self.assertIn("送れなかったページがあります(16:52 山田)", mark.text)

    def test_関門で止めた押し方は_送ったことにも送れなかったことにもしない(self) -> None:
        mark = self.decide(pages=1, unsynced=1, history=[_h("関門で止めた", "2026-10-05T16:52:00")])
        self.assertEqual(mark.state, push_mark.STATE_UNSENT)
        self.assertEqual(mark.text, "この直はまだ共有へ保存していません")
        self.assertEqual(mark.level, "quiet")

    def test_まだ押していなければ控えめに(self) -> None:
        mark = self.decide(pages=1, unsynced=1, history=[])
        self.assertEqual((mark.state, mark.level), (push_mark.STATE_UNSENT, "quiet"))

    def test_押した記録が無いのに共有と同じなら_保存済みとは言わない(self) -> None:
        self.assertFalse(self.decide(pages=1, unsynced=0, history=[]).shown)

    def test_自動確定で送った直は自動でと分かるように出す(self) -> None:
        mark = self.decide(pages=1, unsynced=0,
                           history=[_h("送れた", "2026-10-05T14:55:00", by="(自動確定)")])
        self.assertEqual(mark.text, "この直は自動で共有へ保存済み(14:55)")
        self.assertTrue(mark.auto)

    def test_押した人が空なら担当者_日が違えば月日も添える(self) -> None:
        mark = self.decide(pages=1, unsynced=0,
                           history=[_h("送れた", "2026-10-06T01:10:00", by="", worker="夜勤")])
        self.assertEqual(mark.text, "この直は共有へ保存済み(10/6 01:10 夜勤)")


# ======================================================================
# サーバ: 押してから・開き直して・直して・押し直す
# ======================================================================
class AfterPushCase(ScenarioCase):
    def setUp(self) -> None:
        super().setUp()
        from nippou.logic import shift_review
        shift_review.reset()
        self.addCleanup(shift_review.reset)
        self.start_shift("2026-10-05 07:05", "山田")
        self.at("2026-10-05 14:20")
        self.assertEqual(self.save(clean_shift_rows("1直", "A")).status_code, 200)

    def mark(self) -> dict:
        """入力画面を開き直したときの印(画面の応答)。"""
        return self.state(worker="山田")["push_mark"]

    def page_mark(self) -> str:
        """入力画面の HTML に描かれた印の字(開き直した・ほかの人が開いた)。"""
        html = self.get("/").get_data(as_text=True)
        found = re.search(r'id="push-mark"[^>]*>([^<]*)</span>', html)
        self.assertIsNotNone(found, "入力画面に印の場所がありません")
        return found.group(1).strip()


class MarkTests(AfterPushCase):
    def test_押す前は控えめ_押したら時刻と押した人_直したら変わり_押し直すと戻る(self) -> None:
        self.assertEqual(self.mark()["state"], "unsent")
        self.at("2026-10-05 14:21")
        self.assertEqual(self.push_now(worker="山田").status_code, 200)
        self.assertEqual(self.mark()["text"], "この直は共有へ保存済み(14:21 山田)")
        # **開き直しても・ほかの人が開いても**(サーバが描いた HTML に出ている)
        self.assertEqual(self.page_mark(), "この直は共有へ保存済み(14:21 山田)")

        # 行を直して保存する → 保存の応答のその場で変わる
        rows = clean_shift_rows("1直", "A")
        rows["1"]["WEI"] = "120"
        self.at("2026-10-05 14:25")
        saved = self.save(rows).get_json()
        self.assertEqual(saved["push_mark"]["state"], "stale")
        self.assertIn("共有へ保存したあとに直した行があります", saved["push_mark"]["text"])
        self.assertIn("共有へ保存したあとに直した行があります", self.page_mark())

        self.at("2026-10-05 14:26")
        self.assertEqual(self.push_now(worker="佐藤").status_code, 200)
        self.assertEqual(self.mark()["text"], "この直は共有へ保存済み(14:26 佐藤)")

    def test_送れなかったときは_送れなかったと出す(self) -> None:
        self.shared_offline()
        self.at("2026-10-05 14:21")
        self.push_now(worker="山田")
        mark = self.mark()
        self.assertEqual(mark["state"], "failed", mark)
        self.assertIn("送れなかったページがあります(14:21 山田)", mark["text"])

    def test_過去の直を開いても_その直が保存済みかを出す(self) -> None:
        self.at("2026-10-05 14:21")
        self.push_now(worker="山田")
        # 次の直へ進み、管理者で1直を呼び出す
        self.close_tick("2026-10-05 14:55")
        self.next_shift("2026-10-05 15:05", "佐藤")
        self.save(rows_of(row("B01", "15:00", "16:00")), "佐藤")
        self.assertEqual(self.mark()["state"], "unsent")      # いまの2直はまだ
        self.admin_on()
        opened = self.recall(DAY, "1直")
        self.assertEqual(opened["shift"], "1直")
        self.assertEqual(opened["push_mark"]["text"], "この直は共有へ保存済み(14:21 山田)")


class AfterPushReviewTests(AfterPushCase):
    """A. いまの直を送り切れたら、確認画面への道を返す。"""

    def test_いまの直を送り切れたら確認画面の道を返す(self) -> None:
        self.at("2026-10-05 14:21")
        body = self.push_now(worker="山田").get_json()
        self.assertEqual(body["review"],
                         "/graph/review?after=push&report_date=2026%E5%B9%B410%E6%9C%885%E6%97%A5"
                         "&shift=1%E7%9B%B4")

    def test_送るもの無しで押し直しても出す(self) -> None:
        self.at("2026-10-05 14:21")
        self.push_now(worker="山田")
        self.at("2026-10-05 14:22")
        self.assertTrue(self.push_now(worker="山田").get_json()["review"])

    def test_断られたときは出さない(self) -> None:
        rows = clean_shift_rows("1直", "A")
        rows["3"]["SZ"], rows["3"]["SH"] = "13", "00"        # 最終時間が直の終わりまで無い
        self.save(rows)
        res = self.push_now(worker="山田")
        self.assertEqual(res.status_code, 422)
        self.assertNotIn("review", res.get_json())

    def test_送れなかったときは出さない(self) -> None:
        self.shared_offline()
        body = self.push_now(worker="山田").get_json()
        self.assertGreater(body["failed"], 0)
        self.assertEqual(body["review"], "")

    def test_過去分を共有へ送るときは出さない(self) -> None:
        # 1直を残したまま2直へ進み、2直の画面から「過去分を共有へ送る」
        self.next_shift("2026-10-05 15:05", "佐藤")
        body = self.push_past(worker="佐藤").get_json()
        self.assertEqual(body["sent_shifts"], [f"{DAY} L-1 1直"], body)
        self.assertEqual(body["review"], "")

    def test_まとめて送るときも出さない(self) -> None:
        self.assertEqual(self.push(worker="山田").get_json()["review"], "")


class AfterPushPageTests(AfterPushCase):
    """確認画面: 送った直を映し、「確認しました」で閉じる。時計の確認の覚えには触らない。"""

    def pushed(self, when: str = "2026-10-05 14:21") -> str:
        self.at(when)
        return self.push_now(worker="山田").get_json()["review"]

    def test_送った直の実績を_確認しましたで閉じる画面で出す(self) -> None:
        html = self.get(self.pushed()).get_data(as_text=True)
        self.assertIn('data-after="push"', html)
        self.assertIn("1直 を共有へ渡しました(1ページ)", html)
        self.assertIn("共有へ保存しました。1直 の実績です", html)
        self.assertRegex(html, r'id="review-act"\s+data-action="done">確認しました</button>')

    def test_送ったあとの数字を映す(self) -> None:
        html = self.get(self.pushed()).get_data(as_text=True)
        # 綺麗な1直: 10枚×1 を2行 = 20枚
        self.assertRegex(html, r"<td>1直</td>\s*<td class=\"num\">20(\.0)?</td>")

    def test_前の直が終わったまま残っていても_送ったいまの直を映す(self) -> None:
        # 1直を共有へ出さないまま2直へ進む(引き継いで打ち始める)。時計の確認なら1直を映す
        self.next_shift("2026-10-05 15:05", "佐藤")
        self.take_over("佐藤")
        self.at("2026-10-05 22:30")
        self.assertEqual(self.save(clean_shift_rows("2直", "B"), "佐藤").status_code, 200)
        state = self.get("/api/graph/review").get_json()
        self.assertEqual((state["due"], state["shift"]), (True, "1直"))
        self.at("2026-10-05 22:31")
        body = self.push_now(worker="佐藤").get_json()
        self.assertTrue(body.get("review"), body.get("message"))
        html = self.get(body["review"]).get_data(as_text=True)
        self.assertIn('data-shift="2直"', html)
        self.assertIn("2直 を共有へ渡しました", html)

    def test_閉じても時計の覚えには触らない_保存済みなら15分前の帯も出ない(self) -> None:
        self.pushed()
        res = self.post("/api/graph/review/ack",
                        {"after": "push", "report_date": DAY, "shift": "1直"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["stage"], "")         # 時計の覚えには触らない
        from nippou.logic import shift_review
        self.assertFalse(shift_review.gate().acknowledged(DAY, "1直"))
        self.at("2026-10-05 14:46")
        state = self.get("/api/graph/review").get_json()
        self.assertFalse(state["due"])
        self.assertIsNone(state["notice"], "共有へ保存済みなのに15分前の帯が出ています")

    def test_15分前を過ぎてから送っても_閉じたら連れ戻さない(self) -> None:
        self.pushed("2026-10-05 14:50")
        self.post("/api/graph/review/ack",
                  {"after": "push", "report_date": DAY, "shift": "1直"})
        state = self.get("/api/graph/review").get_json()
        self.assertFalse(state["due"], "入力画面へ戻った途端に、見たばかりの確認へ連れ戻されます")
        self.assertIsNone(state["notice"])

    def test_本日の梱包結果の音は直ごとに1回のまま(self) -> None:
        from nippou.logic import sound
        from nippou.services import sound_files

        sound.gate().forget()
        self.addCleanup(sound.gate().forget)
        with unittest.mock.patch.object(sound_files, "playable", return_value=True):
            first = self.get(self.pushed()).get_data(as_text=True)
            again = self.get(self.pushed("2026-10-05 14:22")).get_data(as_text=True)
            # 直の終わりの確認(時計)でも、もう鳴らさない
            self.at("2026-10-05 14:46")
            later = self.get("/graph/review").get_data(as_text=True)
        self.assertIn(f'data-sound-cue="{sound.KEY_DAILY_RESULT}"', first)
        self.assertIn('data-sound-cue=""', again)
        self.assertIn('data-sound-cue=""', later)


class ShiftEndNoticeTests(AfterPushCase):
    """直の終わり: **15分前は帯**・**5分前の自動確定で送れたら画面いっぱい**・送れなければ帯。"""

    def review(self) -> dict:
        return self.get("/api/graph/review").get_json()

    def test_15分前は画面いっぱいにせず帯で知らせる_閉じたら出ない(self) -> None:
        self.at("2026-10-05 14:46")
        state = self.review()
        self.assertFalse(state["due"], "15分前に画面いっぱいへ連れて行きます")
        self.assertEqual(state["notice"]["text"],
                         "あと14分で直の終わりです。打ち終えたら「共有へ保存」を押してください")
        self.assertEqual(state["notice"]["stage"], "warn")
        res = self.post("/api/graph/review/notice/ack", state["notice"])
        self.assertEqual(res.status_code, 200)
        self.at("2026-10-05 14:47")
        self.assertIsNone(self.review()["notice"])

    def test_15分前の帯では本日の梱包結果を鳴らさない(self) -> None:
        from nippou.logic import sound
        sound.gate().forget()
        self.addCleanup(sound.gate().forget)
        self.at("2026-10-05 14:46")
        self.assertTrue(self.review()["notice"])
        self.assertFalse(sound.gate().already(f"{sound.KEY_DAILY_RESULT}:{DAY}:1直"))

    def test_保存したあとに直していれば帯は出る(self) -> None:
        self.at("2026-10-05 14:21")
        self.push_now(worker="山田")
        rows = clean_shift_rows("1直", "A")
        rows["1"]["WEI"] = "120"
        self.save(rows)
        self.at("2026-10-05 14:46")
        self.assertTrue(self.review()["notice"], "直したのに「保存済み」扱いで帯が出ません")

    def test_5分前の自動確定で送れたら画面いっぱい_印は自動で(self) -> None:
        done = self.close_tick("2026-10-05 14:55")
        self.assertEqual(done["pushed"], 1)
        state = self.review()
        self.assertEqual((state["due"], state["stage"]), (True, "auto"))
        self.assertIn("共有へ自動で保存しました", state["reason"])
        self.assertIsNone(state["notice"])
        html = self.get("/graph/review").get_data(as_text=True)
        self.assertIn('data-shift="1直"', html)
        self.assertIn("1直 は共有へ渡してあります", html)
        self.assertEqual(self.mark()["text"], "この直は自動で共有へ保存済み(14:55)")
        # 「確認しました」で閉じたら、もう出さない
        self.post("/api/graph/review/ack", {"stage": "auto"})
        self.at("2026-10-05 14:56")
        self.assertFalse(self.review()["due"])

    def test_自動で送れなければ帯で_画面いっぱいにはしない(self) -> None:
        rows = clean_shift_rows("1直", "A")
        rows["3"]["SZ"], rows["3"]["SH"] = "13", "00"        # 最終時間が直の終わりまで無い
        self.save(rows)
        done = self.close_tick("2026-10-05 14:55")
        self.assertTrue(done["ran"])
        self.assertEqual(done["pushed"], 0)
        state = self.review()
        self.assertFalse(state["due"])
        self.assertEqual(state["notice"]["stage"], "autofail")
        self.assertIn("自動では共有へ送れませんでした。直すところがあります",
                      state["notice"]["text"])
        self.post("/api/graph/review/notice/ack", state["notice"])
        self.assertIsNone(self.review()["notice"])

    def test_渡さないまま直が終わったら_今までどおり1度だけ画面いっぱい(self) -> None:
        self.next_shift("2026-10-05 15:05", "佐藤")
        state = self.review()
        self.assertEqual((state["due"], state["stage"], state["shift"]), (True, "end", "1直"))
        self.post("/api/graph/review/ack", {"stage": "end"})
        self.assertFalse(self.review()["due"])

    def test_知らない知らせは閉じられない(self) -> None:
        res = self.post("/api/graph/review/notice/ack",
                        {"stage": "end", "report_date": DAY, "shift": "1直"})
        self.assertEqual(res.status_code, 400)


import unittest.mock  # noqa: E402  (上の試験で使う)

if __name__ == "__main__":
    unittest.main()
