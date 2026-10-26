"""共有へ保存のあと(本物のブラウザ。段階1): **グラフが画面いっぱいに出る**・**入力画面に印が残る**

    VBAでは入力完了後に保存で集計後のグラフを前面に出してました。現ツールでは共有保存を
    してもそうはなりません。(…)現ツールでは何をもってこの直を打ち切ったと確認するのか?

現場の答えは「両方入れてください」。画面で押して確かめます:

    共有へ保存 → 送った直の実績のグラフが画面いっぱいに出る → 「確認しました」で入力画面へ
    → 入力画面に「この直は共有へ保存済み(時刻 作業者)」→ 行を直して保存すると
    「共有へ保存したあとに直した行があります」→ もう一度押すと戻る

    送れなかったとき・断られたとき・過去分のときは出ない(入力画面に留まる)

**時間では待ちません**(Windows の CI は遅い)。画面の状態(道・印の字)か、送った要求を待ちます。
決まりの細かいところはサーバの試験(`test_push_mark`)。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._other_screen_browser import ScreenBrowserCase  # noqa: E402
from tests.test_entry_shell_browser import INIT_SCRIPT  # noqa: E402
from tests.test_scenario_shift_flow import (SHIFT_TIMES, _Now,  # noqa: E402
                                            clean_shift_rows)

PUSH = "/api/settings/push"


def sheet_rows(shift: str = "1直") -> dict:
    """綺麗な1直に、休憩の時刻も入れたもの(空だと画面が休憩の窓を開いて押せない)。"""
    rows = clean_shift_rows(shift, "A")
    rows["2"]["break_range"] = "10:00〜11:00"
    return rows
SAVED = "この直は共有へ保存済み"
STALE = "共有へ保存したあとに直した行があります"


class AfterPushBrowserCase(ScreenBrowserCase):
    def setUp(self) -> None:
        super().setUp()
        from nippou import config, user_settings
        from nippou.logic import shift_review
        from nippou.services import shift_close
        from tests.test_stop_reason_csv import make_transmission

        self.share = self.tmp / "共有"
        self.share.mkdir()
        user_settings.save_many({config.KEY_ACCESS_DIR: str(self.share),
                                 config.KEY_REPORT_OUT_DIR: str(self.tmp / "出力")})
        # 停止の記号のマスタと時間マスタ(綺麗な1直が7項目を通るように)
        make_transmission(self.tmp / "ref" / "伝送用ファイル.sqlite3")
        self.repo().replace_shift_times(SHIFT_TIMES)
        shift_review.reset()
        shift_close.reset()
        self.addCleanup(shift_review.reset)
        self.addCleanup(shift_close.reset)
        self.post("/api/entry/line", {"line": "L-1"})
        # 1分ごとの見張り(`shift_end.js` の tick)を**試験が回せるように**掴んでおく
        self.ctx.add_init_script(INIT_SCRIPT)
        # 1直の途中(終わりの15分前より前 ── 時計の確認はまだ出ない)
        self.at("2026-10-05 07:05")
        self.opened: dict = {}
        self.choose("山田")

    # -- サーバ側の下ごしらえ(画面と同じ道) ---------------------------------
    @staticmethod
    def at(when: str) -> None:
        _Now.value = datetime.strptime(when, "%Y-%m-%d %H:%M")

    def choose(self, worker: str) -> None:
        self.assertEqual(self.get("/").status_code, 200)
        body = self.post("/api/entry/state",
                         {"rows": {}, "header": {"worker": worker}, "checks": {}}).get_json()
        self.opened = {"report_date": body["report_date"], "shift": body["shift"],
                       "page": str(body.get("page", 1)), "line": body.get("line", "")}

    def save_clean(self, shift: str, worker: str) -> None:
        res = self.post("/api/entry/save", {"rows": sheet_rows(shift),
                                            "header": {"worker": worker}, "checks": {},
                                            "opened": dict(self.opened)})
        self.assertEqual(res.status_code, 200, res.get_json())

    # -- 画面 -----------------------------------------------------------
    def push_count(self) -> int:
        return sum(1 for p, _ in self.sent if p.split("?")[0] == PUSH)

    def mark(self, page) -> dict:
        return page.evaluate("""() => {
          const el = document.getElementById('push-mark');
          return el ? {text: el.textContent.trim(), hidden: el.hidden,
                       state: el.dataset.state, level: el.dataset.level} : null;
        }""")

    def wait_mark(self, page, state: str, text: str) -> dict:
        page.wait_for_function(
            """([state, text]) => {
              const el = document.getElementById('push-mark');
              return el && !el.hidden && el.dataset.state === state
                && el.textContent.includes(text);
            }""", arg=[state, text], timeout=10000)
        return self.mark(page)

    def push_to_review(self, page):
        """「共有へ保存」→ **送った直の実績が画面いっぱいに出る**まで。"""
        page.click("#push-shared")
        page.wait_for_function(
            "location.pathname === '/graph/review'"
            " && document.getElementById('review')?.dataset.after === 'push'",
            timeout=15000)
        # 推移のグラフが描けるまで(`chart.js`)
        page.wait_for_selector("#review-charts svg", timeout=10000)

    def back_to_entry(self, page) -> None:
        page.click("#review-act")
        page.wait_for_function("location.pathname === '/'", timeout=10000)
        page.wait_for_selector("#grid-body")

    def stays_on_entry(self, page, before: int, *, refreshed: bool) -> None:
        """押した要求が済み、**入力画面に留まった**こと。

        送れたあとは画面を引き直す(`refresh`)ので、その `GET /` を待つ。断られたときは
        引き直さず断りを並べるので、その帯を待つ。どちらも**時間ではなく状態**を待つ。
        """
        self.wait_until(page, lambda: self.push_count() > before, "共有へ保存の要求")
        if refreshed:
            self.wait_until(page, lambda: any(p == "/" for p, _ in
                                              self.sent[self._push_at(before) + 1:]),
                            "送ったあとの引き直し", timeout=15.0)
        else:
            page.wait_for_selector("#check-panel:not([hidden])", timeout=10000)
        page.wait_for_function("!document.querySelector('main#main')?.hasAttribute('aria-busy')",
                               timeout=10000)
        self.assertEqual(page.evaluate("location.pathname"), "/")
        self.assertEqual(page.locator("#review").count(), 0, "確認画面が出ています")

    def tick(self, page) -> None:
        """1分ごとの見張りを1回まわし、**3つ目(確認画面へ・帯)の答えを受け取るまで**待つ。"""
        page.wait_for_function("window.__ticks && window.__ticks.length > 0", timeout=10000)
        mark = len(self.sent)
        page.evaluate("window.__ticks.forEach((f) => f())")
        self.wait_until(page, lambda: any(p.split("?")[0] == "/api/graph/review"
                                          for p, _ in self.sent[mark:]),
                        "見張りの3つ目(確認画面へ・帯)", timeout=15.0)

    def notice(self, page) -> dict:
        return page.evaluate("""() => {
          const el = document.getElementById('shift-notice');
          return {hidden: !el || el.hidden, level: el?.dataset.level || '',
                  text: document.getElementById('shift-notice-text')?.textContent || ''};
        }""")

    def _push_at(self, nth: int) -> int:
        """`nth` 本目(0 から)の共有へ保存の要求が、送った要求の何番目か。"""
        seen = -1
        for i, (p, _) in enumerate(self.sent):
            if p.split("?")[0] == PUSH:
                seen += 1
                if seen == nth:
                    return i
        return len(self.sent)


class PushThenReviewTests(AfterPushBrowserCase):
    def test_保存でグラフが出て_確認しましたで戻り_印が残り_直すと変わり_押し直すと戻る(self) -> None:
        self.save_clean("1直", "山田")
        self.at("2026-10-05 14:20")
        page = self.open_screen("/", "#push-shared")
        mark = self.mark(page)
        self.assertEqual((mark["state"], mark["hidden"]), ("unsent", False))
        self.assertNotIn(SAVED, mark["text"])

        # 共有へ保存 → 送った直の実績が画面いっぱいに
        self.push_to_review(page)
        self.assertIn("1直 を共有へ渡しました", page.inner_text("#closing-headline"))
        self.assertEqual(page.inner_text("#review-act").strip(), "確認しました")
        self.assertTrue(page.locator("#review-later").is_hidden(), "「あとで」が出ています")
        self.assertIn("共有へ保存しました", page.inner_text("#review-reason"))
        # 覆いが画面いっぱい
        box = page.locator("#review").bounding_box()
        self.assertGreaterEqual(box["width"], 1300)

        # 「確認しました」→ 入力画面 → 印
        self.back_to_entry(page)
        mark = self.wait_mark(page, "saved", f"{SAVED}(14:20 山田)")
        self.assertEqual(mark["level"], "ok")

        # 開き直しても出る(サーバが描いた印)
        page.reload(wait_until="load")
        page.wait_for_selector("#grid-body")
        self.assertIn(f"{SAVED}(14:20 山田)", self.mark(page)["text"])

        # 行を直して「保存(確定)」→ 印が変わる
        self.at("2026-10-05 14:25")
        self.name_worker(page)
        self.type_in(page, "WEI1", "120")
        page.click("#save")
        self.wait_mark(page, "stale", STALE)

        # もう一度「共有へ保存」→ また出る → 戻ると保存済み(押し直した時刻)
        self.at("2026-10-05 14:26")
        self.push_to_review(page)
        self.back_to_entry(page)
        self.wait_mark(page, "saved", f"{SAVED}(14:26 山田)")
        self.assertEqual(self.errors, [])


class NoReviewTests(AfterPushBrowserCase):
    def test_送れなかったときは出さない(self) -> None:
        self.save_clean("1直", "山田")
        self.at("2026-10-05 14:20")
        page = self.open_screen("/", "#push-shared")
        self.share.rename(self.tmp / "共有_切れた")             # 共有のフォルダに届かない
        before = self.push_count()
        page.click("#push-shared")
        self.stays_on_entry(page, before, refreshed=True)
        self.wait_mark(page, "failed", "送れなかったページがあります")
        self.assertEqual(self.errors, [])

    def test_断られたときは出さない(self) -> None:
        # 最終時間が直の終わりまで無い(関門で断られる)
        rows = sheet_rows()
        rows["3"]["SZ"], rows["3"]["SH"] = "13", "00"
        res = self.post("/api/entry/save", {"rows": rows, "header": {"worker": "山田"},
                                            "checks": {}, "opened": dict(self.opened)})
        self.assertEqual(res.status_code, 200)
        self.at("2026-10-05 14:20")
        page = self.open_screen("/", "#push-shared")
        before = self.push_count()
        page.click("#push-shared")
        self.stays_on_entry(page, before, refreshed=False)
        self.assertEqual(self.mark(page)["state"], "unsent")

    def test_過去分を共有へ送るときは出さない(self) -> None:
        self.save_clean("1直", "山田")
        # 1直を共有へ出さないまま2直へ(次の人が「次の直を始める」)
        self.at("2026-10-05 15:05")
        self.assertEqual(self.post("/api/entry/start-next", {}).status_code, 200)
        # 1分タイマーの自動確定は**終わった綺麗な直を拾って送る**ので、ここでは止める
        # (残っている前の直を、人が「過去分を共有へ送る」で送る場面にする)
        self.refuse["/api/entry/close"] = 503
        # 終わったのに渡していない1直の確認(時計の1度)は先に済ませる(ここで見たいのは過去分のボタン)
        self.settle_review()
        page = self.open_screen("/", "#handover-push")
        before = self.push_count()
        page.click("#handover-push")
        self.wait_until(page, lambda: self.push_count() > before, "過去分を共有へ送る要求")
        self.assertEqual(self.sent[self._push_at(before)][1].get("scope"), "previous")
        # 送れて、赤い帯が消える(引き直した画面)まで待つ
        page.wait_for_selector("#handover", state="hidden", timeout=15000)
        self.assertEqual(page.evaluate("location.pathname"), "/")
        self.assertEqual(page.locator("#review").count(), 0, "過去分なのに確認画面が出ています")
        self.assertEqual(self.errors, [])


class ShiftEndTests(AfterPushBrowserCase):
    """直の終わり: 15分前は帯(打つ手を止めない)・5分前の自動確定で送れたらグラフ・送れなければ帯。"""

    def test_15分前は帯で知らせ_打っている欄も打ちかけもそのまま_閉じたら出ない(self) -> None:
        self.save_clean("1直", "山田")
        self.at("2026-10-05 14:40")
        page = self.open_screen("/", "#push-shared")
        self.assertTrue(self.notice(page)["hidden"])
        # 打っている最中(離れていない)
        self.type_in(page, "WEI1", "123")
        self.at("2026-10-05 14:46")
        self.tick(page)
        page.wait_for_selector("#shift-notice:not([hidden])", timeout=10000)
        self.assertEqual(self.notice(page)["text"],
                         "あと14分で直の終わりです。打ち終えたら「共有へ保存」を押してください")
        self.assertEqual(page.evaluate("location.pathname"), "/", "画面いっぱいへ連れて行きました")
        self.assertEqual(page.locator("#review").count(), 0)
        self.assertEqual(page.evaluate("document.activeElement?.id"), "WEI1", "焦点を奪いました")
        self.assertEqual(self.value(page, "WEI1"), "123", "打ちかけが消えました")
        # 閉じる → 次の見張りでも出ない(覚えるのはサーバ)
        mark = len(self.sent)
        page.click("#shift-notice-close")
        self.wait_until(page, lambda: "/api/graph/review/notice/ack" in
                        [p for p, _ in self.sent[mark:]], "閉じた知らせを覚える要求")
        self.at("2026-10-05 14:47")
        self.tick(page)
        page.wait_for_function("document.getElementById('shift-notice').hidden", timeout=10000)
        self.assertEqual(self.errors, [])

    def test_共有へ保存済みなら15分前の帯は出ない(self) -> None:
        self.save_clean("1直", "山田")
        self.at("2026-10-05 14:20")
        page = self.open_screen("/", "#push-shared")
        self.push_to_review(page)
        self.back_to_entry(page)
        self.wait_mark(page, "saved", SAVED)
        self.at("2026-10-05 14:46")
        with page.expect_response(lambda r: r.url.split("?")[0].endswith("/api/graph/review")):
            self.tick(page)
        # 出さないことはサーバが決める(画面はその答えを写すだけ)
        self.assertIsNone(self.get("/api/graph/review").get_json()["notice"])
        self.assertTrue(self.notice(page)["hidden"], "保存済みなのに帯が出ています")

    def test_5分前の自動確定で送れたら_画面いっぱいのグラフと自動での印(self) -> None:
        self.save_clean("1直", "山田")
        self.at("2026-10-05 14:50")
        page = self.open_screen("/", "#push-shared")
        self.at("2026-10-05 14:55")
        self.tick(page)
        page.wait_for_function("location.pathname === '/graph/review'", timeout=15000)
        page.wait_for_selector("#review-charts svg", timeout=10000)
        page.wait_for_selector("#review-reason:not([hidden])", timeout=10000)
        self.assertIn("共有へ自動で保存しました", page.inner_text("#review-reason"))
        self.assertIn("1直 は共有へ渡してあります", page.inner_text("#closing-headline"))
        self.back_to_entry(page)
        self.wait_mark(page, "saved", "この直は自動で共有へ保存済み(14:55)")
        # 閉じたら、次の見張りで連れ戻さない
        self.at("2026-10-05 14:56")
        self.tick(page)
        self.assertEqual(page.evaluate("location.pathname"), "/")
        self.assertEqual(self.errors, [])

    def test_5分前の自動確定で送れなければ帯で_入力画面に留まる(self) -> None:
        rows = sheet_rows()
        rows["3"]["SZ"], rows["3"]["SH"] = "13", "00"        # 最終時間が直の終わりまで無い
        res = self.post("/api/entry/save", {"rows": rows, "header": {"worker": "山田"},
                                            "checks": {}, "opened": dict(self.opened)})
        self.assertEqual(res.status_code, 200)
        self.at("2026-10-05 14:50")
        page = self.open_screen("/", "#push-shared")
        self.at("2026-10-05 14:55")
        self.tick(page)
        page.wait_for_function(
            "!document.getElementById('shift-notice').hidden && document.getElementById("
            "'shift-notice-text').textContent.includes('自動では共有へ送れませんでした')",
            timeout=15000)
        self.assertIn("直すところがあります", self.notice(page)["text"])
        self.assertEqual(self.notice(page)["level"], "error")
        self.assertEqual(page.evaluate("location.pathname"), "/")
        self.assertEqual(page.locator("#review").count(), 0)

    def test_渡さないまま直が終わったら_今までどおりグラフへ(self) -> None:
        self.save_clean("1直", "山田")
        self.at("2026-10-05 15:05")
        self.assertEqual(self.post("/api/entry/start-next", {}).status_code, 200)
        self.refuse["/api/entry/close"] = 503            # 拾って送らせない(渡していないまま)
        page = self.watch(self.ctx.new_page())
        page.goto("http://app.test/", wait_until="load")
        page.wait_for_function("location.pathname === '/graph/review'", timeout=15000)
        page.wait_for_selector("#review-reason:not([hidden])", timeout=10000)
        self.assertIn("まだ共有へ渡していません", page.inner_text("#review-reason"))


if __name__ == "__main__":
    import unittest

    unittest.main()
