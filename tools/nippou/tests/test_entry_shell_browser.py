"""日報入力の外まわり: **終了・帯の印・済んだ操作・出たら取り消す・待ちの姿・見るだけ** (本物のブラウザ。段階1)

前は字を探して「仕組みが入っていること」だけを見ていました(`test_entry_leave` の
`PageOverwriteTests` ・ `test_readonly_recall` の一部)。ここでは本当に押して、
**送った要求とその順番・画面の姿**で確かめます:

    - 「終了」: 打ちかけを置いてから終わる(`/api/shutdown` は試験が答える。サーバは止めない)
    - 帯の「過去データ ✕」「管理者 ✕」: 打ちかけを置いてから閉じる
    - 次ページ発行・最新のページに戻る・全停入力: 済んだら「書いたもの」にしてから移る
      (移る前の保存・閉じる間際の送信を、もう出さない)
    - 画面を出たら、答えを待っている要求は取り消す
    - 押したボタンを待ちの姿にするのは、押した処理の通信だけ
    - 見るだけの過去データは、描かれた直後から触れない
"""
from __future__ import annotations

import json
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._entry_browser import ORIGIN, EntryBrowserCase  # noqa: E402

#: 画面に仕込むもの(試験の目と手。画面の動きは変えない):
#:
#:   1分ごとの見張り(`shift_end.js` の `tick`)を、試験から回せるようにする。時計を
#:   進めると自動保存(これも1分)まで走って区別がつかないので、見張りだけを拾う
#:
#:   閉じる間際の送信(`keepalive`)を、タブの控え(`sessionStorage`)へ書き写す。
#:   離れていくページの送信は、取り次ぎ(`route`)に届いたり届かなかったりするので、
#:   **送ろうとしたこと**を、移った先の画面で読む
INIT_SCRIPT = """(() => {
  const real = window.setInterval;
  window.__ticks = [];
  window.setInterval = function (fn, ms, ...rest) {
    if (ms === 60000 && typeof fn === "function" && fn.name === "tick") window.__ticks.push(fn);
    return real.call(this, fn, ms, ...rest);
  };
  const send = window.fetch;
  window.fetch = function (url, options = {}) {
    if (options && options.keepalive) {
      try {
        const kept = JSON.parse(sessionStorage.getItem("__beacons") || "[]");
        kept.push({ url: String(url), body: String(options.body || "") });
        sessionStorage.setItem("__beacons", JSON.stringify(kept));
      } catch (err) { /* 書けなくても送る */ }
    }
    return send.apply(this, arguments);
  };
})();"""


def today_label() -> str:
    d = date.today()
    return f"{d.year}年{d.month}月{d.day}日"


class ShellCase(EntryBrowserCase):
    """土台に足すもの(この段の試験で共通。土台そのものは変えない):

        answers         道 → (番号, JSON)。**試験が答える**(サーバに渡さない)。`sent` にも残す
        confirm_answer  確かめの窓(confirm)に「はい」と答えるか
        fulfill         止めた要求に、試験が決めた JSON で答える
        tick            1分ごとの見張りを1回まわす
    """

    def setUp(self) -> None:
        super().setUp()
        self.answers: dict[str, tuple[int, dict]] = {}
        self.confirm_answer = True
        self.ctx.add_init_script(INIT_SCRIPT)

    def _route(self, route) -> None:
        path = route.request.url.split(ORIGIN, 1)[1]
        bare = path.split("?", 1)[0]
        if bare in self.answers and path not in self.pages:
            req = route.request
            try:
                body = json.loads(req.post_data or "{}") if req.method == "POST" else {}
            except ValueError:
                body = {}
            self.sent.append((path, body if isinstance(body, dict) else {}))
            status, answer = self.answers[bare]
            route.fulfill(status=status, body=json.dumps(answer),
                          headers={"content-type": "application/json"})
            return
        super()._route(route)

    def watch(self, page):
        page.on("pageerror", lambda e: self.errors.append(str(e)))

        def answer(dialog) -> None:
            self.dialogs.append(dialog.message)
            if dialog.type == "confirm" and not self.confirm_answer:
                dialog.dismiss()
            else:
                dialog.accept()

        page.on("dialog", answer)
        return page

    def fulfill(self, page, body: dict, index: int = 0, status: int = 200) -> None:
        route = self.held.pop(index)
        route.fulfill(status=status, body=json.dumps(body),
                      headers={"content-type": "application/json"})
        page.wait_for_timeout(300)

    @staticmethod
    def beacons(page, path: str = "/api/entry/save") -> list[dict]:
        """閉じる間際に送ろうとした本文(`INIT_SCRIPT`)。"""
        kept = page.evaluate("JSON.parse(sessionStorage.getItem('__beacons') || '[]')")
        return [json.loads(b["body"] or "{}") for b in kept if b["url"].startswith(path)]

    @staticmethod
    def tick(page) -> None:
        page.wait_for_function("window.__ticks && window.__ticks.length > 0", timeout=5000)
        page.evaluate("window.__ticks.forEach((f) => f())")

    # -- 送った要求 ------------------------------------------------------
    def paths(self, since: int = 0) -> list[str]:
        return [p for p, _ in self.sent[since:]]

    def first(self, since: int, match) -> int | None:
        """`since` から数えて、`match(道, 本文)` に合う最初の要求の位置。"""
        for i, (p, b) in enumerate(self.sent[since:]):
            if match(p, b):
                return i
        return None

    def wait_sent(self, page, since: int, path: str, what: str = "") -> int:
        bare = lambda p: p.split("?", 1)[0]
        self.wait_until(page, lambda: any(bare(p) == path for p in self.paths(since)),
                        what or f"{path} の要求")
        return next(i for i, p in enumerate(self.paths(since)) if bare(p) == path)

    @staticmethod
    def is_draft(p: str, b: dict) -> bool:
        return p.startswith("/api/entry/save") and bool(b.get("draft"))

    @staticmethod
    def is_save(p: str, b: dict) -> bool:
        return p.startswith("/api/entry/save")

    # -- 下ごしらえ ------------------------------------------------------
    def seed_sheet(self, rows: int = 1, shift: str = "1直") -> None:
        """いまの日の紙を1枚置く(作業者つき)。`rows` 行ぶん、終了まで打ってある。"""
        from nippou.db.models import DetailRecord, HeaderRecord

        key = dict(report_date=today_label(), line="L-1", shift=shift, page=1)
        details = [DetailRecord(**key, row_no=n, lot=f"N71{n:02d}T0",
                                kz=f"{7 + (n - 1) // 2:02d}", kh="00" if n % 2 else "30",
                                sz=f"{7 + n // 2:02d}", sh="00" if n % 2 == 0 else "30",
                                con=str(100 + n), wei=str(1200 + n * 90))
                   for n in range(1, rows + 1)]
        self.repo().save(HeaderRecord(**key, worker="山田"), details)

    def admin(self, on: bool = True) -> None:
        body = {"enable": True, "password": "nisk"} if on else {"enable": False}
        self.assertEqual(self.post("/api/settings/admin", body).status_code, 200)

    def recall(self, shift: str = "1直") -> None:
        res = self.post("/api/settings/recall", {"report_date": today_label(), "line": "L-1",
                                                 "shift": shift, "page": 1})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))


class QuitTests(ShellCase):
    def test_終了は打ちかけを置いてから終わる(self) -> None:
        self.answers["/api/shutdown"] = (200, {"message": "終了します"})
        page = self.open_entry()
        self.type_in(page, "ZAI1", "52S- R")
        mark = len(self.sent)
        page.click("#quit")
        at = self.wait_sent(page, mark, "/api/shutdown")
        draft = self.first(mark, self.is_draft)
        self.assertIsNotNone(draft, f"終了の前に打ちかけを置いていません: {self.paths(mark)}")
        self.assertLess(draft, at, "終わってから置こうとしています")
        self.assertEqual(self.sent[mark + draft][1]["rows"]["1"].get("ZAI"), "52S- R")
        page.wait_for_function("document.body.textContent.includes('終了しました')", timeout=5000)
        self.assertEqual(self.errors, [])


class ChipTests(ShellCase):
    """帯の印を押して、そのモードを終わる。**閉じる前に、直していたぶんを置く。**"""

    def press_chip(self, page, action: str) -> int:
        # 欄を離れた答えは止めておく(自動保存が先に書くと、置いたかどうかが見えない)
        self.hold = ("/api/entry/state",)
        mark = len(self.sent)
        page.click(f'#rb-chips [data-chip-action="{action}"]')
        return mark

    def test_過去データを閉じる前に打ちかけを置く(self) -> None:
        self.seed_sheet()
        self.admin()
        self.recall()
        page = self.open_entry()
        self.type_in(page, "ZAI1", "52S- R")
        mark = self.press_chip(page, "back")
        at = self.wait_sent(page, mark, "/api/settings/back")
        draft = self.first(mark, self.is_draft)
        self.assertIsNotNone(draft, f"閉じる前に置いていません: {self.paths(mark)}")
        self.assertLess(draft, at)
        self.assertEqual(self.sent[mark + draft][1]["rows"]["1"].get("ZAI"), "52S- R")
        self.assertTrue(self.dialogs, "押し間違いで閉じないよう確かめていません")
        page.wait_for_selector("#grid-body")

    def test_管理者モードを終える前に打ちかけを置く(self) -> None:
        self.admin()
        page = self.open_entry()
        self.type_in(page, "ZAI1", "52S- R")
        mark = self.press_chip(page, "admin-off")
        at = self.wait_sent(page, mark, "/api/settings/admin")
        self.assertEqual(self.sent[mark + at][1], {"enable": False})
        draft = self.first(mark, self.is_draft)
        self.assertIsNotNone(draft, f"終える前に置いていません: {self.paths(mark)}")
        self.assertLess(draft, at)
        self.assertEqual(self.sent[mark + draft][1]["rows"]["1"].get("ZAI"), "52S- R")

    def test_確かめでやめたら閉じない(self) -> None:
        self.admin()
        page = self.open_entry()
        self.confirm_answer = False
        mark = len(self.sent)
        page.click('#rb-chips [data-chip-action="admin-off"]')
        page.wait_for_timeout(600)
        self.assertNotIn("/api/settings/admin", self.paths(mark))


class DoneThenMoveTests(ShellCase):
    """**済んだ操作のあとは「書いたもの」にしてから移る。**

    そうしないと、移る前の保存・閉じる間際の送信が、古いページの中身を新しい書き先へ
    送ります(「打った行が消えることがありました」)。済んだあとに保存が出ないことを見る。
    """

    def test_次ページ発行のあとは移る前の保存を出さない(self) -> None:
        self.seed_sheet(rows=12)
        # 発行そのものは試験が答える(12行の中身の関門は別の試験 ── ここは発行のあと)
        self.answers["/api/entry/newpage"] = (200, {"message": "2ページ目を出しました"})
        page = self.open_entry()
        self.type_in(page, "ZAI1", "52S- R")
        mark = len(self.sent)
        # **欄から離れずに押す**(離れた答えの自動保存が先に書くと、区別がつかない)
        page.evaluate("document.getElementById('new-page').click()")
        at = self.wait_sent(page, mark, "/api/entry/newpage")
        self.assertEqual(self.sent[mark + at][1]["rows"]["1"].get("ZAI"), "52S- R")
        # 引き直しまで待つ(引き直すと表が作り直される)
        self.wait_until(page, lambda: "/" in self.paths(mark + at), "画面の引き直し")
        page.wait_for_selector("#grid-body")
        page.wait_for_timeout(500)
        after = [p for p, b in self.sent[mark + at + 1:] if self.is_save(p, b)]
        self.assertEqual(after, [], "発行して書けたのに、移る前にもう一度保存しています")
        self.assertEqual(len(self.dialogs), 1, self.dialogs)     # 発行の確かめだけ
        self.assertEqual(self.errors, [])

    def test_最新のページに戻るのあとは閉じる間際に送らない(self) -> None:
        self.seed_sheet()
        self.admin()
        self.recall()
        page = self.open_entry()
        self.type_in(page, "ZAI1", "52S- R")
        mark = len(self.sent)
        page.evaluate("document.getElementById('back-to-current').click()")
        at = self.wait_sent(page, mark, "/api/settings/back")
        self.assertEqual(self.sent[mark + at][1]["rows"]["1"].get("ZAI"), "52S- R")
        page.wait_for_function("!document.getElementById('back-to-current')", timeout=8000)
        page.wait_for_selector("#grid-body")
        page.wait_for_timeout(500)
        after = [p for p, b in self.sent[mark + at + 1:] if self.is_save(p, b)]
        self.assertEqual(after, [], "戻って書けたのに、もう一度保存しています")
        self.assertEqual(self.beacons(page), [], "戻って書けたのに、閉じる間際にもう一度送っています")

    def run_formstop(self, page) -> int:
        """全停入力の窓を開き、理由を選んで実行する。押す直前の `sent` の長さを返す。"""
        self.hold = ("/api/entry/state",)               # 自動保存を先に走らせない
        page.click("#open-formstop")
        page.check('input[name="formstop-reason"]')
        mark = len(self.sent)
        page.click("#formstop-run")
        return mark

    def formstop_page(self):
        self.answers["/api/formstop/reasons"] = (200, {"categories": [
            {"category": "設備停止", "reasons": [{"code": "11", "label": "設備故障"}]}]})
        self.answers["/api/formstop/execute"] = (200, {"message": "全停入力しました", "next": "/"})
        page = self.open_entry()
        self.type_in(page, "ZAI1", "52S- R")
        return page

    def test_全停入力は打ちかけを置いてから書く(self) -> None:
        page = self.formstop_page()
        mark = self.run_formstop(page)
        at = self.wait_sent(page, mark, "/api/formstop/execute")
        draft = self.first(mark, self.is_draft)
        self.assertIsNotNone(draft, f"全停の前に打ちかけを置いていません: {self.paths(mark)}")
        self.assertLess(draft, at)
        self.assertEqual(self.sent[mark + draft][1]["rows"]["1"].get("ZAI"), "52S- R")
        page.wait_for_selector("#grid-body")
        page.wait_for_timeout(800)
        self.assertEqual(self.beacons(page), [], "全停のページを書いたあとに、閉じる間際の送信を出しています")

    def test_全停のあとは_打ちかけを置けずに続けたときも閉じる間際に送らない(self) -> None:
        """置けなかった打ちかけは「消えます」と訊いて続ける。**全停の紙へ、あとから書かない。**"""
        page = self.formstop_page()
        self.refuse["/api/entry/save"] = 500
        mark = self.run_formstop(page)
        self.wait_sent(page, mark, "/api/formstop/execute")
        self.assertTrue(any("保存していない行は消えます" in m for m in self.dialogs), self.dialogs)
        page.wait_for_selector("#grid-body")
        page.wait_for_timeout(800)
        self.assertEqual(self.beacons(page), [], "全停のページを書いたあとに、閉じる間際の送信を出しています")


class LeaveCancelTests(ShellCase):
    def test_画面を出たら答えを待っている要求を取り消す(self) -> None:
        page = self.open_entry()
        failed: list[str] = []
        page.on("requestfailed", lambda r: failed.append(r.url.split(ORIGIN, 1)[1]))
        self.hold = ("/api/entry/state",)
        self.type_in(page, "MAI1", "10")
        page.locator("#MAI1").press("Tab")             # 離れる → 答えを止める
        self.wait_held(page, 1)
        self.hold = ()
        page.click('nav.rail a[href="/records"]')
        page.wait_for_function("location.pathname === '/records'", timeout=8000)
        self.wait_until(page, lambda: "/api/entry/state" in failed,
                        "前の画面の答えを待つ要求の取り消し")


class BusyTests(ShellCase):
    """**押したボタンを待ちの姿にするのは、押した処理の通信だけ。**

    前は最後に押したボタンをずっと覚えていて、あとから走った関係のない通信(欄を
    離れたときの「決まる値」)まで、そのボタンの待ちにしていました。250ms を超えると
    ボタンが押せなくなり、「この直をチェック」を押しても何も起きませんでした。
    """

    @staticmethod
    def busy(page, selector: str) -> bool:
        return page.evaluate(
            """(sel) => { const b = document.querySelector(sel);
                          return b.getAttribute('aria-busy') === 'true' || b.disabled; }""", selector)

    def test_押した処理の通信を待つあいだは待ちの姿(self) -> None:
        """対照: 押した処理の答えを止めれば、250ms で待ちの姿になる。"""
        page = self.open_entry()
        self.hold = ("/api/entry/verify",)
        page.click("#check-shift")
        self.wait_held(page, 1)
        page.wait_for_function(
            "document.getElementById('check-shift').getAttribute('aria-busy') === 'true'",
            timeout=3000)
        self.assertTrue(self.busy(page, "#check-shift"))
        self.hold = ()
        self.release(page)
        page.wait_for_function("!document.getElementById('check-shift').disabled", timeout=3000)

    def test_押したあとに始まった関係ない通信では待ちにしない(self) -> None:
        page = self.open_entry()
        self.hold = ("/api/entry/state",)
        self.type_in(page, "MAI1", "10")
        button = '[data-stop-to="2"]'
        page.click(button)                             # 押すと欄を離れる → 少しあとに「決まる値」
        self.wait_held(page, 1)
        page.wait_for_timeout(500)                     # 待ちの姿になるなら、もうなっている
        self.assertFalse(self.busy(page, button), "押したボタンが、関係ない通信の待ちになっています")

    def test_打ったら押したボタンを忘れる(self) -> None:
        """押してすぐ(50ms 以内)に始まった通信は、そのボタンの待ち。**ただし打ったら別の操作。**"""
        page = self.open_entry()
        button = '[data-stop-to="2"]'
        for typed in (False, True):
            with self.subTest(typed=typed):
                self.hold = ("/api/entry/state",)
                page.evaluate("""([sel, typed]) => {
                    document.querySelector(sel).click();
                    if (typed) document.dispatchEvent(new KeyboardEvent("keydown", { key: "Shift" }));
                    const w = document.getElementById("hd-worker");
                    w.value = typed ? "田中" : "山田 田中";
                    w.dispatchEvent(new Event("change", { bubbles: true }));
                }""", [button, typed])
                self.wait_held(page, 1)
                page.wait_for_timeout(500)
                self.assertEqual(self.busy(page, button), not typed)
                self.hold = ()
                while self.held:
                    self.release(page)


class ReadOnlyTests(ShellCase):
    """他の直の過去データを、管理者モードでないまま開いている(`test_readonly_recall`)。"""

    def test_描かれた直後から触れない(self) -> None:
        """`paint()` を待つと、**最初の1回だけ打ててしまう。** 答えを止めて、描かれただけの画面を見る。"""
        repo = self.repo()
        # 10:00 が 2直 になるように境界を置き、1直 を開く
        repo.set_shift_time("1", "01:00", "09:00")
        repo.set_shift_time("2", "09:00", "17:00")
        repo.set_shift_time("3", "17:00", "01:00")
        self.seed_sheet()
        self.admin()
        self.recall("1直")
        self.admin(False)
        self.hold = ("/api/entry/state",)
        page = self.watch(self.ctx.new_page())
        page.goto(f"{ORIGIN}/", wait_until="load")
        page.wait_for_selector("#grid-body")
        # 画面のモジュールが動き出した印(取り合いの名乗り。`start()` の中で、触れなくするより前に始める)
        self.wait_until(page, lambda: "/api/tab/claim" in self.paths(), "画面の動き出し")
        self.assertFalse(page.is_hidden("#read-only"))
        self.assertTrue(page.evaluate("document.body.classList.contains('is-readonly')"))
        for field in ("ZAI1", "HIT1", "LOT2"):
            with self.subTest(field=field):
                self.assertTrue(page.is_disabled(f"#{field}"), f"{field} が打てます")
        self.assertTrue(page.is_disabled("#save"))
        self.assertEqual(self.value(page, "LOT1"), "N7101T0")


if __name__ == "__main__":
    unittest.main()
