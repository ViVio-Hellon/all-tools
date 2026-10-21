"""日報入力: 入力の近道と、開始・終了のダブルクリック (本物のブラウザ。段階1)

前は字を探していました(`test_input_shortcuts` の「画面の配線」・`test_time_stamp` の
「ダブルクリックの口」)。ここでは本当に押して・打って確かめます:

    - 枚数のダブルクリック → 上の行と同じ枚数・包数が入る
    - Enter で次の欄・Shift+Enter で前の欄。**日本語の変換を確定する Enter では動かない**
    - 変換のあいだは欄を書き換えない(確定してから半角・大文字へ直す)
    - 枚数を離れると包数に 1 が入り、**入った欄が光る**
    - 終了の時/分に入ると「直の終わり」が出て、押すと時と分に入る
    - 欄にマウスを乗せると簡易説明が出る
    - 作業者の窓: いまの名前に最初からチェック・班の見出しで班の全員
    - 開始の時/分のダブルクリック → その時刻が時と分の両方に入る(時計は 10:00 に止めてある)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._entry_browser import EntryBrowserCase  # noqa: E402


def focused(page) -> str:
    return page.evaluate("document.activeElement && document.activeElement.id")


class ShortcutTests(EntryBrowserCase):
    def test_枚数をダブルクリックすると上の行と同じ枚数と包数が入る(self) -> None:
        page = self.open_entry()
        self.type_in(page, "MAI1", "2")
        self.type_in(page, "TUT1", "3")
        page.locator("#TUT1").press("Tab")
        page.wait_for_timeout(500)
        page.dblclick("#MAI2")
        page.wait_for_function("document.getElementById('MAI2').value === '2'", timeout=5000)
        self.assertEqual(self.value(page, "TUT2"), "3")
        self.assertEqual(self.errors, [])

    def test_Enterで次の欄_ShiftEnterで前の欄_変換の確定では動かない(self) -> None:
        page = self.open_entry()
        page.click("#ZAI1")
        page.keyboard.press("Enter")
        self.assertEqual(focused(page), "SIZ1")
        page.keyboard.press("Shift+Enter")
        self.assertEqual(focused(page), "ZAI1")
        # 日本語の変換を確定する Enter(`isComposing`)は取らない
        page.evaluate("""() => document.getElementById('ZAI1').dispatchEvent(
            new KeyboardEvent('keydown', {key: 'Enter', isComposing: true, bubbles: true, cancelable: true}))""")
        page.wait_for_timeout(100)
        self.assertEqual(focused(page), "ZAI1", "変換を確定する Enter で次の欄へ動いています")

    def test_変換のあいだは書き換えず_確定してから直す(self) -> None:
        """変換中に欄を書き換えると変換そのものを壊す(打ったかなが消える)。"""
        page = self.open_entry()
        page.click("#LOT1")
        page.evaluate("""() => {
            const el = document.getElementById('LOT1');
            el.value = 'ｎ７';
            el.dispatchEvent(new InputEvent('input', {isComposing: true, bubbles: true}));
        }""")
        self.assertEqual(self.value(page, "LOT1"), "ｎ７", "変換のあいだに書き換えています")
        page.evaluate("""() => document.getElementById('LOT1').dispatchEvent(
            new CompositionEvent('compositionend', {data: 'ｎ７', bubbles: true}))""")
        self.assertEqual(self.value(page, "LOT1"), "N7")

    def test_枚数を離れると包数に1が入って光る(self) -> None:
        page = self.open_entry()
        self.type_in(page, "MAI1", "2")
        page.click("#LOT3")                       # 包数から離れた所へ(打っている欄は書き換えない)
        page.wait_for_function(
            "document.getElementById('TUT1').classList.contains('is-filled')", timeout=5000)
        self.assertEqual(self.value(page, "TUT1"), "1")

    def test_終了の欄に入ると直の終わりが出て_押すと時と分に入る(self) -> None:
        page = self.open_entry()
        self.assertTrue(page.locator("#shift-end-chip").is_hidden())
        page.click("#SZ1")
        page.wait_for_selector("#shift-end-chip:not([hidden])", timeout=3000)
        end = page.text_content("#shift-end-text").strip()
        self.assertRegex(end, r"^\d\d:\d\d$")
        page.click("#shift-end-btn")
        page.wait_for_function("document.getElementById('SH1').value !== ''", timeout=5000)
        self.assertEqual(f"{self.value(page, 'SZ1')}:{self.value(page, 'SH1')}", end)
        page.click("#LOT2")
        page.wait_for_selector("#shift-end-chip", state="hidden", timeout=3000)

    def test_欄にマウスを乗せると簡易説明が出る(self) -> None:
        page = self.open_entry()
        # **先に見える所まで巻き戻して待つ。** 巻き戻しの合図(scroll)が乗せたあとに届くと、
        # 「画面が動いたら消す」で説明が消える(`test_row_clear_browser` の ✕ と同じ)
        page.locator("#MAI3").scroll_into_view_if_needed()
        page.mouse.move(5, 5)
        page.wait_for_timeout(300)
        page.hover("#MAI3")
        page.wait_for_selector("#hint-bubble:not([hidden])", timeout=3000)
        hint = page.get_attribute("#MAI3", "data-hint")
        self.assertTrue(hint)
        self.assertEqual(page.text_content("#hint-bubble"), hint)
        page.keyboard.press("Shift")              # 打てば消える
        page.wait_for_selector("#hint-bubble", state="hidden", timeout=3000)

    def test_開始の時をダブルクリックするといまの時刻が時と分に入る(self) -> None:
        page = self.open_entry()
        # **欄そのものにダブルクリックの合図を送る。** マウスで位置を決めて押すと、決めてから押すまでに
        # 表が1行ぶん動いて3行目に当たることがあった(Windows の CI。残した様子: 焦点 KZ3・stamp は3行目)。
        # 受けるのは表に1つの見張り(`grid-body` の dblclick)で、どの欄かは合図の宛先で決まる
        page.dispatch_event("#KZ2", "dblclick")
        try:
            page.wait_for_function("document.getElementById('KH2').value !== ''", timeout=10000)
        except Exception as exc:                    # noqa: BLE001 - 何が起きていたかを残す
            state = page.evaluate("""() => ({
                kz2: document.getElementById('KZ2').value, kh2: document.getElementById('KH2').value,
                focus: document.activeElement && document.activeElement.id,
                toasts: [...document.querySelectorAll('.toast__text')].map(t => t.textContent),
                scroll: [scrollX, scrollY]})""")
            stamps = [b.get("stamp") for path, b in self.sent if path.startswith("/api/entry/state")]
            self.fail(f"ダブルクリックで時刻が入りません: {exc}\n画面: {state}\n送った stamp: {stamps}")
        self.assertEqual((self.value(page, "KZ2"), self.value(page, "KH2")), ("10", "00"))
        self.assertEqual(self.value(page, "KZ1"), "")
        self.assertEqual(self.errors, [])


class StaffPickTests(EntryBrowserCase):
    def setUp(self) -> None:
        super().setUp()
        from nippou.logic.staff import StaffMember

        roster = [StaffMember("青木", "A", "あおき"), StaffMember("井上", "A", "いのうえ"),
                  StaffMember("江藤", "B", "えとう")]
        patcher = patch("app.routes.staff.staff_master.load_staff_members", return_value=roster)
        patcher.start()
        self.addCleanup(patcher.stop)

    def checked(self, page) -> list[str]:
        return page.evaluate(
            "[...document.querySelectorAll('[data-staff]:checked')].map(el => el.value)")

    def test_いまの名前に最初からチェック_班の見出しで班の全員(self) -> None:
        page = self.open_entry()
        page.fill("#hd-worker", "新人 江藤")
        page.dispatch_event("#hd-worker", "change")
        page.click("#open-staff")
        page.wait_for_selector("#staff-modal:not([hidden]) [data-staff]", timeout=5000)
        page.wait_for_function(
            "[...document.querySelectorAll('[data-staff]:checked')].length > 0", timeout=5000)
        self.assertEqual(self.checked(page), ["江藤"])
        page.click('[data-staff-team="A"]')
        self.assertEqual(sorted(self.checked(page)), sorted(["青木", "井上", "江藤"]))
        page.click('[data-staff-team="A"]')       # 全員に付いていれば全員外す
        self.assertEqual(self.checked(page), ["江藤"])
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
