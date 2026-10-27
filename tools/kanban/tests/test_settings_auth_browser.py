"""設定画面「パスワード認証」を**本物のブラウザで押して**確かめる(段階1)。

以前は ``/settings`` の HTML に ``id="unlock-form"`` などの字があるかを見ていた
(`test_web_app.UnlockRouteTest` / `AdminPasswordTest`)。字が残っていれば、入れても
認証できない・決めても画面が変わらない画面でも緑になる。ここでは利用者と同じに打って、
**帯と面の表示・サーバに通ったか**を見る。
"""
from __future__ import annotations

import unittest

from kanban import config
from tests._kanban_browser import KanbanBrowserCase


class _Auth(KanbanBrowserCase):
    def hash(self) -> str:
        return config.load_config(self.dir / "config.json").admin_password_hash

    def chip(self, page) -> str:
        return self.text(page, "#auth-state").strip()


class NoSetupTest(_Auth):
    """**決めていない端末も既定の値で開く**(日報・カレンダー・点検表と同じ)。

    以前は「未設定」の面が出て、最初に鍵を開けようとした人がその場で新しい
    パスワードを決められた。いまは決めさせる欄が無く、認証の欄が出る。
    """

    def test_決めていない端末は_認証の欄が出て既定の値のままと言う(self) -> None:
        page = self.open_settings("auth")
        self.assertEqual(self.chip(page), "未認証")
        self.assertEqual(page.locator("#setup-form").count(), 0, "決めさせる欄が残っている")
        self.assertTrue(self.visible(page, "#unlock-form"), "認証の欄が出ていない")
        self.assertNotIn("未設定", self.text(page, "#lockbar-text"))
        self.assertTrue(self.visible(page, "#password-card"), "変える欄が出ていない")
        self.assertIn("既定のパスワードのまま", self.text(page, "#password-source"))

        with self.subTest("最初に入れた値では開かず、パスワードにもならない"):
            page.fill("#unlock-pw", "はじめて")
            page.click("#unlock-save")
            self.wait_until(page, lambda: self.posted("/api/admin/unlock"), "認証を頼んだ状態")
            page.wait_for_function("document.getElementById('unlock-pw').value === ''")
            self.assertEqual(self.chip(page), "未認証")
            self.assertEqual(self.hash(), "", "最初に入れた値でパスワードが決まった")

        with self.subTest("既定の値で開く"):
            page.fill("#unlock-pw", "秘密")             # 試験の既定の値(test_web_app.RouteTestBase)
            page.click("#unlock-save")
            page.wait_for_function("document.getElementById('auth-state').textContent.trim() === '認証済み'")
            self.assertIn("認証済み", self.text(page, "#lockbar-text"))
        self.assertEqual(self.errors, [])


class UnlockTest(_Auth):
    """**鍵を開けるためだけの入口。** 1 度開けたら、鍵が閉まるまで訊かない。"""

    def setUp(self) -> None:
        super().setUp()
        # この端末で決めた値(既定の値ではない)で試す
        self.post("/api/admin/password", {"current_password": "秘密", "new_password": "鍵の値",
                                          "new_password_confirm": "鍵の値"})
        from kanban import admin_lock

        admin_lock.get().lock()   # 変えたときに開いた鍵を閉めてから始める

    def unlocked(self) -> bool:
        return self.get("/api/admin/state").json["admin_unlocked"]

    def test_認証の面で開けて_帯で閉める(self) -> None:
        page = self.open_settings("auth")
        self.assertEqual(self.chip(page), "未認証")
        self.assertTrue(self.visible(page, "#unlock-form"))
        self.assertIn("未認証", self.text(page, "#lockbar-text"))
        self.assertIn("この端末で変えた値", self.text(page, "#password-source"))

        with self.subTest("違うパスワードでは開かない"):
            page.fill("#unlock-pw", "ちがう")
            page.click("#unlock-save")
            self.wait_until(page, lambda: self.posted("/api/admin/unlock"), "認証を頼んだ状態")
            page.wait_for_function("document.getElementById('unlock-pw').value === ''")
            self.assertFalse(self.unlocked())
            self.assertEqual(self.chip(page), "未認証")

        with self.subTest("正しいパスワードで開き、帯と面が変わる"):
            page.fill("#unlock-pw", "鍵の値")
            page.click("#unlock-save")
            page.wait_for_function("document.getElementById('auth-state').textContent === '認証済み'")
            self.assertTrue(self.unlocked())
            self.assertIn("認証済み", self.text(page, "#lockbar-text"))
            self.assertFalse(self.visible(page, "#unlock-form"))
            self.assertTrue(self.visible(page, "#lockbar-lock"), "帯に閉めるボタンが出ない")

        with self.subTest("帯のボタンで閉めると、また訊く"):
            page.click("#lockbar-lock")
            page.wait_for_function("document.getElementById('auth-state').textContent === '未認証'")
            self.assertFalse(self.unlocked())
            self.assertTrue(self.visible(page, "#unlock-form"))
            self.assertIn("未認証", self.text(page, "#lockbar-text"))
        self.assertEqual(self.errors, [])

    def test_認証の面でパスワードを変える_今のパスワードが要る(self) -> None:
        page = self.open_settings("auth")
        self.assertTrue(self.visible(page, "#password-form"), "「パスワード認証」の面に変える欄が無い")

        with self.subTest("確認が違えば送らない"):
            page.fill("#admin-pw-new", "あたらしい")
            page.fill("#admin-pw-new-confirm", "あたらしく")
            page.click("#password-save")
            self.wait_toast(page, "一致しません")
            self.assertEqual(self.posted("/api/admin/password"), [])

        with self.subTest("今のパスワードが違えば変わらず、その欄へ戻る"):
            page.fill("#admin-pw-current", "ちがう")
            page.fill("#admin-pw-new-confirm", "あたらしい")
            page.click("#password-save")
            self.wait_until(page, lambda: self.posted("/api/admin/password"), "変更を頼んだ状態")
            page.wait_for_function("document.activeElement && document.activeElement.id === 'admin-pw-current'")
            self.assertTrue(config.verify_password("鍵の値", self.hash()), "違うのに変わった")

        with self.subTest("今のパスワードが合えば変わり、欄は空になる"):
            page.fill("#admin-pw-current", "鍵の値")
            page.click("#password-save")
            page.wait_for_function("document.getElementById('admin-pw-new').value === ''")
            self.assertTrue(config.verify_password("あたらしい", self.hash()))
            self.assertFalse(config.verify_password("鍵の値", self.hash()))
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
