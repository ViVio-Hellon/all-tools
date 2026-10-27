"""入口(大きなタブ・大設定)の**一連の動き**を、本物のブラウザで通しで確かめる

    「入口(大設定)周りの一連動作チェックお願いします」

カレンダー・看板・点検表と同じく、現場の人が順に触る流れを1本ずつ試験に残す
(画面の一部だけを見る試験は `test_settings_browser.py`・`test_shell_*_browser.py`)。

    1. 初めての起動      手元の領域がまっさら → 大きなタブ・名前と版・説明書(F1)・ログインID@PC名
    2. 鍵とパスワード    鍵を開ける/閉める・違うパスワード・パスワードを変える。鍵が掛かっていると
                         押しても書かない(確かめの窓も出さない)
    3. タブ表示権限      行を足す・直す・消す → 大きなタブがその場で増える/外れた印/戻った印/次の起動
    4. 共有の DB の置き場所  決める・既定に戻す・届かない場所
    5. 配布設定          書き出す/まとめて書き出す/中身の一覧/読み込み直す/消す
    6. 画面の色の既定    4ツールの枠へ届く(開き直しても)
    7. 大きなタブを切り替える・ブラウザ版の心拍・裏に回る・閉じる

どの流れでも、画面の誤り(pageerror)と console の error が出ないことを見る(断られたときの
4xx の「Failed to load resource」だけは、断る場面の答えなので数えない)。

本物の領域は汚さない(`tests/__init__.py` が手元の領域・共有の DB・配布設定を一時フォルダへ向ける)。
各ツールの配布設定は**ツールのフォルダに書かない**よう、書き出しを差し替える。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._shell_browser import ORIGIN, DesktopShellBrowserCase, ShellBrowserCase

TOOL_IDS = ["nippou", "kanban", "calendar", "inspection"]
#: 断る場面の答え(画面は文を出す)。Chromium は console に「Failed to load resource」と出す
REFUSALS = re.compile(r"^Failed to load resource: the server responded with a status of (400|403|409|422) ")


class FlowCase(ShellBrowserCase):
    """通しの流れの土台: console の error を集める・この端末の設定を元に戻す。"""

    def setUp(self) -> None:
        super().setUp()
        from portal import distribution, user_settings, web
        from portal.rights_store import store

        self.console: list[str] = []
        self.ctx.on("console", self._console)

        def restore() -> None:
            user_settings.update({key: None for key in (
                user_settings.KEY_SHARED_DIR, user_settings.KEY_SHARED_NAME, user_settings.KEY_THEME_DEFAULT,
                user_settings.KEY_DISTRIBUTION_APPLIED, distribution.KEY_FROM_DIST)})
            shutil.rmtree(distribution.directory(), ignore_errors=True)
            store.sync(force=True)
            web.forget_screens()

        restore()
        self.addCleanup(restore)

    def _console(self, message) -> None:
        if message.type == "error" and not REFUSALS.match(message.text):
            self.console.append(message.text)

    def tearDown(self) -> None:
        self.assertEqual(self.errors, [], "画面の誤り(pageerror)")
        self.assertEqual(self.console, [], "console の error")
        super().tearDown()

    # -- 画面 ------------------------------------------------------------
    def open_settings(self):
        page = self.open_shell(first="settings")
        page.wait_for_function("document.getElementById('lock-state').textContent.includes('鍵が')"
                               " && document.getElementById('me-reason').textContent !== ''")
        return page

    def unlock_in_page(self, page) -> None:
        page.fill("#lock-pass", "nisk")
        page.press("#lock-pass", "Enter")
        page.wait_for_function("document.getElementById('lock-state').textContent.includes('鍵が開いています')")

    def clear_toasts(self, page) -> None:
        page.evaluate("document.getElementById('toasts').replaceChildren()")

    def wait_toast(self, page, text: str) -> str:
        page.wait_for_function("t => document.getElementById('toasts').textContent.includes(t)", arg=text)
        return self.toasts(page)

    def toast_kind(self, page, text: str) -> str:
        return page.evaluate("t => [...document.querySelectorAll('#toasts .toast')]"
                             ".find(n => n.textContent.includes(t))?.className || ''", text)

    def tabs(self, page) -> list[str]:
        return page.evaluate("[...document.querySelectorAll('#bigtabs .bigtab')].map(b => b.dataset.tab)")

    def removed_tabs(self, page) -> list[str]:
        return page.evaluate("[...document.querySelectorAll('#bigtabs .bigtab.is-removed')].map(b => b.dataset.tab)")

    def selected(self, page) -> str:
        return page.evaluate("document.querySelector('#bigtabs .bigtab[aria-selected=true]')?.dataset.tab || ''")

    def rows(self, page) -> list[list[str]]:
        return page.evaluate("[...document.querySelectorAll('#rights-grid tbody tr')]"
                             ".map(tr => [...tr.cells].map(td => td.innerText.trim()))")

    def save_row(self, page) -> None:
        page.click("#row-save")
        page.wait_for_selector("#row-dialog", state="hidden")

    def fresh_page(self, *, last_tab: str = ""):
        """次の起動(新しい画面)。`last_tab` を前に開いたタブとして覚えさせる(空なら覚えていない)。"""
        page = self.ctx.new_page()
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.on("dialog", lambda d: (self.dialogs.append(d.message), d.accept()))
        if last_tab:
            page.add_init_script(f"localStorage.setItem('alltools.lastTab', {json.dumps(last_tab)})")
        page.goto(f"{ORIGIN}/", wait_until="load")
        page.wait_for_selector("#bigtabs .bigtab[aria-selected=true]")
        return page


# ======================================================================
# 1. 初めての起動
# ======================================================================
class FirstStartFlowTests(FlowCase):
    """手元の領域がまっさらで入口を開く(初めての端末)。"""

    def wipe_local(self) -> None:
        """手元の領域を空にする(ログは残す: 開いている記録のファイルを消すと、あとの試験の記録が
        消える。Windows では消せない)。共有の読み直しは、最初に画面が訊いたとき(30秒たっている扱い)。"""
        from portal.rights_store import store

        local = Path(os.environ["ALLTOOLS_LOCAL_DIR"])
        for child in (local.iterdir() if local.is_dir() else ()):
            if child.name == "logs":
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        store._checked_at = 0.0

    def test_まっさらで開くと4ツールと大設定のタブ_名前と版_説明書_ログインIDとPC名(self) -> None:
        from portal import app_config

        self.wipe_local()
        page = self.fresh_page()
        # 大きなタブ: 4ツール(catalog の並び)と大設定。前に開いたタブが無いので先頭のツール
        self.assertEqual(self.tabs(page), TOOL_IDS + ["settings"])
        self.assertEqual(self.selected(page), "nippou")
        for tool_id in TOOL_IDS:
            with self.subTest(tool=tool_id):
                tool = self.catalog.by_id(tool_id)
                self.assertEqual(page.inner_text(f"#tab-{tool_id} .bigtab__title"), tool.title)
                self.assertEqual(page.inner_text(f"#tab-{tool_id} .bigtab__ver"), f"VER{tool.version()}")
                self.assertEqual(page.inner_text(f"#tab-{tool_id} .bigtab__sub"), tool.name)
        # 名前と版・ブラウザ版の印・題名・ログインID@PC名
        brand = page.inner_text(".brand")
        self.assertIn(app_config.display_name(), brand)
        self.assertIn(f"VER{app_config.version()}", brand)
        self.assertIn("ブラウザ版", brand)
        self.assertEqual(page.title(), f"{app_config.display_name()} — 日報")
        self.assertEqual(page.inner_text("#who"), "tester @ TEST-PC")
        self.tool_frame(page, "nippou")
        # 説明書(F1): いまのタブの説明書。もう一度 F1 で閉じる
        page.focus("#help")
        page.keyboard.press("F1")
        page.wait_for_selector("#manual:not([hidden])")
        page.wait_for_function("document.getElementById('manual-frame').contentWindow.location.pathname"
                               " === '/static/manual/nippou.html'")
        page.keyboard.press("F1")
        page.wait_for_selector("#manual", state="hidden")
        # 大設定: この端末・決め方・表がまだ無い・鍵が掛かっている
        page.click("#tab-settings")
        page.wait_for_function("document.getElementById('me-reason').textContent !== ''")
        self.assertEqual(page.title(), f"{app_config.display_name()} — 大設定")
        self.assertEqual(page.inner_text("#me-login"), "tester")
        self.assertEqual(page.inner_text("#me-pc"), "TEST-PC")
        self.assertIn("表がまだありません", page.inner_text("#me-reason"))
        self.assertIn("すべてのタブを出しています", page.inner_text("#me-reason"))
        self.assertIn("まだ運用を始めていません", page.inner_text("#me-warn"))
        self.assertTrue(page.is_visible("#rights-missing"))
        self.assertEqual(self.rows(page), [["表がまだありません(上の「表を作る」で作れます)"]],
                         "表が無いのに「読めていません」と出す")
        self.assertIn("鍵が掛かっています", page.inner_text("#lock-state"))
        self.assertIn("まだ変えていません", page.inner_text("#pw-lead"))
        self.assertIn("置いてありません", page.inner_text("#dist-state"))
        # 鍵が要るボタンは押せない見た目で、理由が出る
        locked = page.evaluate("[...document.querySelectorAll('[data-admin]')].filter(b => !b.hidden)"
                               ".map(b => [b.getAttribute('aria-disabled'), b.title])")
        self.assertTrue(locked)
        self.assertEqual({tuple(x) for x in locked}, {("true", "管理者パスワードで鍵を開けると使えます")})
        # 説明書は大設定の説明書になる
        page.click("#help")
        page.wait_for_function("document.getElementById('manual-frame').contentWindow.location.pathname"
                               " === '/static/manual/portal.html'")
        page.click("#manual-close")
        # 手元の領域に写しができた(次の起動は共有に届かなくてもこれで決める)
        cache = Path(os.environ["ALLTOOLS_LOCAL_DIR"]) / "data" / "tab_rights_cache.json"
        self.wait_until(page, cache.is_file, "タブ表示権限の写し")

    def test_まっさらで共有に届かなければ全部のタブを出し_理由を出す(self) -> None:
        from portal import user_settings

        missing = Path(tempfile.gettempdir()) / "alltools_無い場所" / "その下"
        self.wipe_local()
        user_settings.update({user_settings.KEY_SHARED_DIR: str(missing)})
        page = self.fresh_page(last_tab="settings")
        self.assertEqual(self.tabs(page), TOOL_IDS + ["settings"], "ラインを止めない")
        page.wait_for_function("document.getElementById('me-reason').textContent !== ''")
        self.assertIn("手元の写しもありません", page.inner_text("#me-reason"))
        source = page.inner_text("#rights-source")
        self.assertIn("共有の DB に届きません", source)
        self.assertIn("手元の写しも無いので、すべてのタブを出しています", source)
        self.assertIn("最後に読めた: まだ", source)
        self.assertEqual(self.rows(page), [["表を読めていません"]])


# ======================================================================
# 2. 鍵(管理者パスワード)
# ======================================================================
class LockFlowTests(FlowCase):
    def test_鍵が掛かっていると書かない_理由を出して鍵の欄へ_確かめの窓も出さない(self) -> None:
        from portal import admin_password, distribution, user_settings

        # 表と行・配布設定を置いておく(「直す」「消す」「読み込み直す」「消す」が出るように)
        self.unlock()
        self.post("/api/rights/create-table")
        self.post("/api/rights/add", {"pc_name": "TEST-PC", "tab_ids": ["kanban"]})
        user_settings.update({user_settings.KEY_SHARED_NAME: "梱包資材マスタ.sqlite3"})
        self.assertTrue(distribution.export().ok)
        admin_password.session.lock()
        page = self.open_settings()
        self.assertTrue(page.is_visible("#dist-reapply"))
        before = len(self.sent)
        buttons = {
            "+ 行を足す": "#rights-add",
            "直す": "#rights-grid tbody tr >> text=直す",
            "消す(行)": "#rights-grid tbody tr >> text=消す",
            "変える(置き場所)": "#loc-form button[type=submit]",
            "既定に戻す": "#loc-default",
            "配布設定を書き出す": "#dist-export",
            "まとめて書き出す": "#dist-export-all",
            "読み込み直す": "#dist-reapply",
            "消す(配布設定)": "#dist-remove",
        }
        for name, selector in buttons.items():
            with self.subTest(button=name):
                self.clear_toasts(page)
                page.evaluate("document.getElementById('lock-pass').blur()")
                page.click(selector, force=True)        # 押せない見た目(aria-disabled)でも押せる
                self.wait_toast(page, "管理者パスワードで鍵を開けてください")
                self.assertEqual(self.toast_kind(page, "鍵を開けて"), "toast toast--ng")
                self.assertEqual(page.evaluate("document.activeElement.id"), "lock-pass", "鍵の欄へ案内しない")
                self.assertEqual(self.dialogs, [], "「よろしいですか?」に答えさせてから断った")
                self.assertFalse(page.is_visible("#row-dialog"))
        writes = [p for m, p, _ in self.sent[before:] if m == "POST" and not p.startswith(("/api/alive", "/api/log", "/api/tools/"))]
        # 置き場所の「変える」「既定に戻す」と「書き出す」は入口が断る(打った値を残して鍵の欄へ)。
        # ほかは画面で止める(行の窓を開かない・確かめの窓を出さない)
        self.assertEqual(writes, ["/api/settings/location", "/api/settings/location", "/api/distribution/export"])
        self.assertTrue(distribution.directory().is_dir(), "鍵が掛かっているのに消した")

    def test_鍵を開ける_違うパスワード_掛ける(self) -> None:
        page = self.open_settings()
        for wrong in ("bad", ""):
            with self.subTest(password=wrong):
                self.clear_toasts(page)
                page.fill("#lock-pass", wrong)
                page.press("#lock-pass", "Enter")
                self.wait_toast(page, "パスワードが違います")
                self.assertIn("鍵が掛かっています", page.inner_text("#lock-state"))
        self.unlock_in_page(page)
        self.assertEqual(page.input_value("#lock-pass"), "", "開けたあとにパスワードを欄に残す")
        self.assertTrue(page.is_visible("#lock-close"))
        self.assertFalse(page.is_visible("#lock-open"))
        self.assertEqual(page.evaluate("[...new Set([...document.querySelectorAll('[data-admin]')]"
                                       ".map(b => b.getAttribute('aria-disabled')))]"), ["false"])
        page.click("#lock-close")
        page.wait_for_function("document.getElementById('lock-state').textContent.includes('鍵が掛かっています')")
        self.assertTrue(page.is_visible("#lock-open"))

    def test_パスワードを変えると古いのでは開かない_断る文が分かる(self) -> None:
        page = self.open_settings()
        cases = [("bad", "abcd", "abcd", "いまのパスワードが違います。"),
                 ("nisk", "ab", "ab", "新しいパスワードは 4 文字以上にしてください。"),
                 ("nisk", "abcd", "abce", "確認のために入れたものと一致しません。"),
                 ("nisk", "nisk", "nisk", "いまと同じパスワードです。")]
        for current, new, again, message in cases:
            with self.subTest(message=message):
                self.clear_toasts(page)
                page.fill("#pw-current", current)
                page.fill("#pw-new", new)
                page.fill("#pw-confirm", again)
                page.click("#pw-form button[type=submit]")
                self.wait_toast(page, message)
                self.assertEqual(page.input_value("#pw-new"), new, "断られたら打った値を残す")
        page.fill("#pw-current", "nisk")
        page.fill("#pw-new", "abcd")
        page.fill("#pw-confirm", "abcd")
        page.click("#pw-form button[type=submit]")
        self.wait_toast(page, "管理者パスワードを変えました。")
        self.assertEqual([page.input_value(i) for i in ("#pw-current", "#pw-new", "#pw-confirm")], ["", "", ""])
        page.wait_for_function("document.getElementById('pw-lead').textContent.includes('この端末で変えたパスワード')")
        self.clear_toasts(page)
        page.fill("#lock-pass", "nisk")
        page.press("#lock-pass", "Enter")
        self.wait_toast(page, "パスワードが違います")
        page.fill("#lock-pass", "abcd")
        page.press("#lock-pass", "Enter")
        page.wait_for_function("document.getElementById('lock-state').textContent.includes('鍵が開いています')")


# ======================================================================
# 3. タブ表示権限 → 大きなタブ
# ======================================================================
class RightsFlowTests(FlowCase):
    def test_表を作り_行を足す_直す_消す_大きなタブがその場で出入りし_次の起動で決まる(self) -> None:
        page = self.open_settings()
        self.unlock_in_page(page)
        self.assertEqual(self.tabs(page), TOOL_IDS + ["settings"])
        # 表が無いうちに足そうとすると、窓の中で「表を作る」へ案内する(打った値は残る)
        page.click("#rights-add")
        page.wait_for_selector("#row-dialog[open]")
        self.assertEqual(page.input_value("#row-pc"), "TEST-PC", "足す窓の PC名 はこの端末の値")
        page.check("#row-tabs input[value=kanban]")
        page.click("#row-save")
        page.wait_for_function("!document.getElementById('row-error').hidden")
        self.assertIn("先に「表を作る」を押してください", page.inner_text("#row-error"))
        self.assertTrue(page.is_checked("#row-tabs input[value=kanban]"))
        page.click("#row-cancel")                    # 打った値があるので訊く(はい)
        page.wait_for_selector("#row-dialog", state="hidden")
        self.dialogs.clear()
        page.click("#rights-create")
        self.wait_toast(page, "タブ表示権限の表を作りました")
        page.wait_for_selector("#rights-missing", state="hidden")
        self.assertEqual(self.rows(page), [["まだ1行もありません"]])
        # 1行め: この端末に 日報・看板(既定は看板)。最初の1行は確かめる
        page.click("#rights-add")
        page.wait_for_selector("#row-dialog[open]")
        page.check("#row-tabs input[value=nippou]")
        page.check("#row-tabs input[value=kanban]")
        page.select_option("#row-default", "kanban")
        self.clear_toasts(page)
        self.save_row(page)
        self.assertIn("最初の1行です", self.dialogs[0])
        self.wait_toast(page, "カレンダー、点検表 のタブは、この端末では出さない設定になりました")
        self.assertEqual(self.tabs(page), TOOL_IDS + ["settings"], "外れたタブをその場で消した(打ちかけが消える)")
        self.assertEqual(self.removed_tabs(page), ["calendar", "inspection"])
        self.assertIn("次に日報複合ツールを開いたときに消えます", page.get_attribute("#tab-calendar", "title"))
        self.assertEqual(self.rows(page)[0][1:7], ["(問わない)", "TEST-PC", "日報\n看板", "看板", "有効", ""])
        self.assertIn("PC TEST-PC", page.inner_text("#me-reason"))
        # 直す: 点検表を戻す → 外れた印が消える(戻ったと知らせる)。外れたままのカレンダーは印のまま
        self.clear_toasts(page)
        page.locator("#rights-grid tbody tr").first.get_by_role("button", name="直す").click()
        page.wait_for_selector("#row-dialog[open]")
        page.check("#row-tabs input[value=inspection]")
        self.save_row(page)
        self.wait_toast(page, "点検表 のタブは、また出す設定になりました")
        self.assertEqual(self.removed_tabs(page), ["calendar"])
        self.assertEqual(page.get_attribute("#tab-inspection", "title"),
                         f"{self.catalog.by_id('inspection').name} VER{self.catalog.by_id('inspection').version()}")
        # 次の起動: 権限のタブだけ。既定タブ(看板)を開く
        restart = self.fresh_page(last_tab="nippou")
        self.assertEqual(self.tabs(restart), ["nippou", "kanban", "inspection", "settings"])
        self.assertEqual(self.selected(restart), "kanban", "既定タブで開かない")
        restart.close()
        # 消す(確かめる)→ 1行も無い = 全部出す。外れた印は全部消える
        self.clear_toasts(page)
        self.dialogs.clear()
        page.locator("#rights-grid tbody tr").first.get_by_role("button", name="消す").click()
        self.wait_toast(page, "タブ表示権限から1行消しました")
        self.assertIn("管理番号 1(PC TEST-PC)を消します", self.dialogs[0])
        self.wait_toast(page, "カレンダー のタブは、また出す設定になりました")
        self.assertEqual(self.removed_tabs(page), [])
        # ほかの端末の行だけ → この端末は登録が無い(大設定だけになる)。もう一度外れたら、また知らせる
        page.click("#rights-add")
        page.wait_for_selector("#row-dialog[open]")
        page.fill("#row-pc", "OTHER-PC")
        page.check("#row-tabs input[value=calendar]")
        self.clear_toasts(page)
        self.save_row(page)
        self.wait_toast(page, "日報、看板、カレンダー、点検表 のタブは、この端末では出さない設定になりました")
        self.assertEqual(self.removed_tabs(page), TOOL_IDS)
        self.assertIn("この端末は登録されていません", page.inner_text("#me-warn"))
        self.assertIn("(ツールのタブは出していません)", page.inner_text("#me-tabs"))
        restart = self.fresh_page(last_tab="kanban")
        self.assertEqual(self.tabs(restart), ["settings"])
        self.assertEqual(self.selected(restart), "settings")
        restart.wait_for_function("document.getElementById('me-warn').textContent.includes('登録されていません')")
        restart.close()
        # 両方空の行は作れない(窓の中で断る。要求は送らない)
        page.click("#rights-add")
        page.wait_for_selector("#row-dialog[open]")
        page.fill("#row-pc", "")
        adds = len(self.posts("/api/rights/add"))
        page.click("#row-save")
        page.wait_for_function("!document.getElementById('row-error').hidden")
        self.assertIn("ログインID か PC名 の少なくとも一方を入れてください", page.inner_text("#row-error"))
        self.assertEqual(len(self.posts("/api/rights/add")), adds)

    def test_権限で増えたタブはその場で足し_開ける(self) -> None:
        self.unlock()
        self.post("/api/rights/create-table")
        rule = self.post("/api/rights/add", {"pc_name": "TEST-PC", "tab_ids": ["kanban"]}).get_json()["rules"][-1]
        page = self.open_shell(first="kanban")
        self.assertEqual(self.tabs(page), ["kanban", "settings"])
        page.click("#tab-settings")
        page.wait_for_function("document.getElementById('lock-state').textContent.includes('鍵が開いています')")
        page.locator("#rights-grid tbody tr", has_text=str(rule["key"])).get_by_role("button", name="直す").click()
        page.wait_for_selector("#row-dialog[open]")
        page.check("#row-tabs input[value=calendar]")
        self.save_row(page)
        self.wait_toast(page, "タブが増えました: カレンダー")
        self.assertEqual(self.tabs(page), ["kanban", "calendar", "settings"])
        self.assertEqual(self.selected(page), "settings", "足したときに別のタブへ移った")
        page.click("#tab-calendar")
        self.tool_frame(page, "calendar")
        self.assertEqual(len(self.posts("/api/tools/calendar/open")), 1)

    def test_届かない場所では書けない理由を窓の中に出し_読み直しも理由を出す(self) -> None:
        from portal import user_settings

        missing = str(Path(tempfile.gettempdir()) / "alltools_無い場所")
        page = self.open_settings()
        self.unlock_in_page(page)
        user_settings.update({user_settings.KEY_SHARED_DIR: missing})
        self.clear_toasts(page)
        page.click("#rights-sync")
        self.wait_toast(page, "共有の DB に届きません")
        self.assertEqual(self.toast_kind(page, "届きません"), "toast toast--ng")
        page.click("#rights-add")
        page.wait_for_selector("#row-dialog[open]")
        page.fill("#row-login", "taro")
        page.check("#row-tabs input[value=kanban]")
        page.click("#row-save")
        page.wait_for_function("!document.getElementById('row-error').hidden")
        self.assertIn("共有の DB に届きません", page.inner_text("#row-error"))
        self.assertTrue(page.is_visible("#row-dialog"), "窓を閉じた(打った値が消える)")
        self.assertEqual(page.input_value("#row-login"), "taro")
        self.assertFalse(page.is_disabled("#row-save"), "断られたあと「保存する」が押せない")


# ======================================================================
# 4. 共有の DB の置き場所
# ======================================================================
class LocationFlowTests(FlowCase):
    def test_決める_届かない場所は目立つ文で知らせる_既定に戻す(self) -> None:
        page = self.open_settings()
        lead = page.inner_text("#loc-form >> xpath=preceding-sibling::p[1]")
        self.assertIn("タブ表示権限の表", lead)
        self.assertIn("各ツールには効きません", lead, "各ツールの共有の DB との関係が分からない")
        self.assertIn("(環境変数の場所)", page.inner_text("#loc-state"))
        self.unlock_in_page(page)
        # ファイル名にフォルダは入れられない
        self.clear_toasts(page)
        page.fill("#loc-name", "下/梱包資材マスタ.sqlite3")
        page.click("#loc-form button[type=submit]")
        self.wait_toast(page, "ファイル名にフォルダを入れないでください")
        self.assertIn("まだ反映していません", page.inner_text("#loc-state"))
        page.fill("#loc-name", "")
        # 届く場所(表がまだ無い DB)へ
        folder = tempfile.mkdtemp(prefix="alltools_loc_")
        self.addCleanup(shutil.rmtree, folder, True)
        import sqlite3

        sqlite3.connect(Path(folder) / "梱包資材マスタ.sqlite3").close()
        self.clear_toasts(page)
        page.fill("#loc-folder", folder)
        page.click("#loc-form button[type=submit]")
        text = self.wait_toast(page, "共有の DB の置き場所を変えました")
        self.assertEqual(self.toast_kind(page, "置き場所を変えました"), "toast toast--ok")
        self.assertIn(str(Path(folder) / "梱包資材マスタ.sqlite3"), text)
        page.wait_for_function("document.getElementById('rights-source').textContent.includes('決めたところ: 大設定')")
        self.assertTrue(page.is_visible("#rights-missing"))
        # 届かない場所へ: 「反映しました」とだけ出さない。前の場所の写しの時刻・「写しで決めています」も出さない
        missing = str(Path(tempfile.gettempdir()) / "alltools_無い場所" / "共有")
        self.clear_toasts(page)
        page.fill("#loc-folder", missing)
        page.click("#loc-form button[type=submit]")
        text = self.wait_toast(page, "共有の DB に届きません")
        self.assertIn("場所が正しいか確かめてください", text)
        self.assertEqual(self.toast_kind(page, "届きません"), "toast toast--ng", "届かないのに目立たない")
        page.wait_for_function("t => document.getElementById('loc-state').textContent.includes(t)", arg=missing)
        source = page.inner_text("#rights-source")
        self.assertNotIn("前回読めた手元の写しで決めています", source, "使っていない写しで決めていると言う")
        self.assertIn("手元の写しも無いので、すべてのタブを出しています", source)
        self.assertIn("最後に読めた: まだ", source, "前の場所の読めた時刻を出す")
        self.assertIn("手元の写しもありません", page.inner_text("#me-reason"))
        # 既定に戻す(環境変数の場所)
        self.clear_toasts(page)
        page.click("#loc-default")
        self.wait_toast(page, "共有の DB の置き場所を既定に戻しました")
        page.wait_for_function("document.getElementById('loc-state').textContent.includes('(環境変数の場所)')")
        self.assertEqual(page.input_value("#loc-folder"), "")


# ======================================================================
# 5. 配布設定
# ======================================================================
class DistributionFlowTests(FlowCase):
    """書き出す → まとめて書き出す → 中身の一覧 → 読み込み直す → 消す。

    各ツールの書き出し(そのツールのフォルダで子を起こす)は差し替え、ツールのフォルダには書かない。
    日報だけ「ツールのフォルダに書き出した」ことにする(`folder`)。
    """

    def setUp(self) -> None:
        super().setUp()
        from portal import distribution

        self.folder: dict[str, dict] = {}

        def export_tool(tool):
            if tool.id == "nippou":
                self.folder["nippou"] = {
                    "created_at": distribution._now(), "created_on": "TEST-PC",
                    "version": tool.version(),
                    "settings": {"gw_reference_dir": r"\\srv\参照", "admin_password": "pbkdf2$1$c2FsdA==$eA=="},
                    "labels": {"gw_reference_dir": "参照用マスタの参照パス", "admin_password": "管理者パスワード"}}
                return distribution.Result(True, "書き出しました", applied=["参照用マスタの参照パス"])
            if tool.id == "kanban":
                return distribution.Result(False, "設定ファイルを読めません(壊れています)", "failed")
            return distribution.Result(False, "書き出せる設定がありません", "nothing")

        for patcher in (mock.patch.object(distribution, "export_tool", export_tool),
                        mock.patch.object(distribution, "_tool_folder_data",
                                          lambda tool: self.folder.get(tool.id))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def dist_rows(self, page) -> dict[str, list[str]]:
        return page.evaluate("Object.fromEntries([...document.querySelectorAll('#dist-tools tbody tr')]"
                             ".map(tr => [tr.dataset.tool, [...tr.cells].slice(1).map(td => td.innerText)]))")

    def test_書き出す_まとめて書き出す_中身の一覧_読み込み直す_消す(self) -> None:
        from portal import distribution

        page = self.open_settings()
        self.assertFalse(page.is_visible("#dist-reapply"), "無いのに読み込み直すが出る")
        self.assertFalse(page.is_visible("#dist-remove"))
        self.unlock_in_page(page)
        # 既定のままなら書き出すものが無い(理由が分かる)
        self.clear_toasts(page)
        page.click("#dist-export")
        self.wait_toast(page, "書き出せる設定がありません")
        # 置き場所を決めて書き出す
        folder = tempfile.mkdtemp(prefix="alltools_dist_")
        self.addCleanup(shutil.rmtree, folder, True)
        page.fill("#loc-folder", folder)
        page.click("#loc-form button[type=submit]")
        page.wait_for_function("document.getElementById('loc-state').textContent.includes('反映しています')")
        self.clear_toasts(page)
        page.click("#dist-export")
        self.wait_toast(page, "配布設定を書き出しました(1項目: 共有の DB のフォルダ)")
        page.wait_for_function("document.getElementById('dist-state').textContent.includes('置いてあります')")
        self.assertEqual(page.inner_text("#dist-contents"), f"共有の DB のフォルダ: {folder}")
        self.assertTrue(page.is_visible("#dist-reapply"))
        self.assertTrue(page.is_visible("#dist-remove"))
        # 4ツールの分もまとめて: ツールごとに1行(書き出せなかったものは理由)
        self.clear_toasts(page)
        page.click("#dist-export-all")
        self.wait_toast(page, "配布設定をまとめて書き出しました")
        self.assertEqual(self.dialogs, [], "まだ1つも書き出していないのに書き直すと訊いた")
        lines = page.evaluate("[...document.querySelectorAll('#dist-all-results li')].map(li => li.textContent)")
        self.assertEqual(len(lines), 5)
        self.assertTrue(lines[0].startswith("大設定: 書き出しました"))
        self.assertIn("4ツールの分(日報)も 配布設定\\日報複合ツール.json にまとめました", lines[0])
        self.assertEqual(lines[1], "日報: 書き出しました(1項目: 参照用マスタの参照パス)")
        self.assertEqual(lines[2], "看板: 書き出せませんでした: 設定ファイルを読めません(壊れています)")
        self.assertIn("既定のままなので要りません", lines[3])
        self.assertEqual(self.toast_kind(page, "まとめて書き出しました"), "toast toast--ng", "書き出せなかったのに目立たない")
        # 中身の一覧: 使うほう・項目と値。パスワードの値は出さない
        rows = self.dist_rows(page)
        self.assertIn("使うもの: ツールのフォルダ(tools\\nippou\\配布設定)", rows["nippou"][0])
        self.assertIn("参照用マスタの参照パス: \\\\srv\\参照", rows["nippou"][1])
        self.assertIn("管理者パスワード: (設定済み)", rows["nippou"][1])
        self.assertNotIn("pbkdf2", page.inner_text("#panel-settings"), "パスワードの値を出した")
        self.assertEqual(rows["kanban"][0], "なし(書き出していません)")
        # ツールのフォルダのものが無くなれば、まとめたファイルの分を使う
        self.folder.clear()
        page.click("#rights-sync")
        page.wait_for_function("document.querySelector('#dist-tools tr[data-tool=nippou]').innerText.includes('まとめたファイル')")
        # ツールのフォルダに新しい・中身の違う・版の違うもの → ※ を添える
        self.folder["nippou"] = {"created_at": "2099/01/01 00:00:00", "version": "0.0.1",
                                 "settings": {"gw_reference_dir": r"\\srv\別"}, "labels": {}}
        page.click("#rights-sync")
        page.wait_for_function("document.querySelector('#dist-tools tr[data-tool=nippou]').innerText.includes('中身が違います')")
        nippou = self.dist_rows(page)["nippou"][0]
        self.assertIn("※ ツールのフォルダとまとめたファイルの両方にあり、中身が違います", nippou)
        self.assertIn("※ 作った版は VER0.0.1", nippou)
        # 書き出してあるツールがあると、まとめて書き出す前に訊く(やめれば書き出さない)
        self.answers_for_dialogs = [False]
        sent = len(self.posts("/api/distribution/export-all"))
        page.click("#dist-export-all")
        self.wait_until(page, lambda: self.dialogs, "確かめの窓")
        self.assertIn("既定の選び方で書き直します", self.dialogs[-1])
        self.assertFalse(page.is_disabled("#dist-export-all"))
        self.assertEqual(len(self.posts("/api/distribution/export-all")), sent)
        # 読み込み直す: 確かめて、この端末の置き場所を配布設定の値に
        page.click("#loc-default")
        page.wait_for_function("document.getElementById('loc-state').textContent.includes('(環境変数の場所)')")
        self.dialogs.clear()
        self.answers_for_dialogs = [False]
        page.click("#dist-reapply")
        self.wait_until(page, lambda: self.dialogs, "確かめの窓")
        self.assertIn("配布設定の値で上書きされます", self.dialogs[0])
        self.assertEqual(self.posts("/api/distribution/reapply"), [])
        self.clear_toasts(page)
        page.click("#dist-reapply")
        self.wait_toast(page, "配布設定を読み込み直しました(1項目)")
        page.wait_for_function("t => document.getElementById('loc-state').textContent.includes(t)", arg=folder)
        self.assertEqual(page.input_value("#loc-folder"), folder)
        # 消す: 確かめてから。まとめた4ツールの分も消えると書いてある
        self.dialogs.clear()
        self.clear_toasts(page)
        page.click("#dist-remove")
        self.wait_toast(page, "配布設定を消しました")
        self.assertIn("まとめて入れた4ツールの分も消えます", self.dialogs[0])
        page.wait_for_function("document.getElementById('dist-state').textContent.includes('置いてありません')")
        self.assertFalse(distribution.directory().exists())
        self.assertFalse(page.is_visible("#dist-reapply"))
        self.assertFalse(page.is_visible("#dist-remove"))


# ======================================================================
# 6. 画面の色の既定
# ======================================================================
class ThemeFlowMixin:
    def themes(self, tool: str) -> list[str]:
        return [d["theme"] for d in self.kinds("tool", tool=tool, type="alltools:theme")]

    def check_theme_flow(self) -> None:
        page = self.open_shell(first="kanban")
        self.wait_until(page, lambda: all(self.themes(t) for t in TOOL_IDS), "4ツールの枠が開いて既定を受ける", timeout=20)
        self.assertEqual({t: self.themes(t) for t in TOOL_IDS}, {t: ["light"] for t in TOOL_IDS})
        page.click("#tab-settings")
        page.wait_for_function("document.querySelector('[data-theme-default=light]').getAttribute('aria-pressed') === 'true'")
        page.click("[data-theme-default=dark]")
        self.wait_until(page, lambda: all(self.themes(t)[-1:] == ["dark"] for t in TOOL_IDS),
                        "4ツールの枠(出していないタブも)へ届く")
        self.assertEqual(page.get_attribute("html", "data-theme"), "dark")
        self.assertEqual(page.get_attribute("[data-theme-default=dark]", "aria-pressed"), "true")
        self.assertIn("画面の色の既定をダークにしました", self.toasts(page))
        # 次の起動: 描く前からダーク・枠には開いたときにダークを渡す
        self.events.clear()
        again = self.fresh_page(last_tab="calendar")
        self.assertEqual(again.get_attribute("html", "data-theme"), "dark")
        self.wait_until(again, lambda: self.themes("calendar"), "開いたときの既定")
        self.assertEqual(self.themes("calendar"), ["dark"])


class ThemeFlowTests(ThemeFlowMixin, FlowCase):
    def test_ブラウザ版_変えると4ツールの枠へ届き_次の起動も既定の色(self) -> None:
        self.check_theme_flow()


class DesktopThemeFlowTests(ThemeFlowMixin, DesktopShellBrowserCase):
    def setUp(self) -> None:
        super().setUp()
        from portal import user_settings

        user_settings.update({user_settings.KEY_THEME_DEFAULT: None})
        self.addCleanup(user_settings.update, {user_settings.KEY_THEME_DEFAULT: None})

    fresh_page = FlowCase.fresh_page

    def test_デスクトップ版_変えると4ツールの枠へ届き_次の起動も既定の色(self) -> None:
        self.check_theme_flow()
        self.assertEqual(self.errors, [])


# ======================================================================
# 7. 大きなタブを切り替える・ブラウザ版
# ======================================================================
class SwitchFlowTests(FlowCase):
    def test_切り替えても枠は読み直さず打ちかけが残る_矢印キーは帯に留まる_Altと数字(self) -> None:
        page = self.open_shell(first="kanban")
        self.wait_until(page, lambda: all(self.tool_loads.get(t) for t in TOOL_IDS),
                        "ほかのツールも裏で起こす", timeout=20)
        for tool_id in TOOL_IDS:
            self.tool_frame(page, tool_id)
        self.tool_frame(page, "kanban").fill("#typed", "看板の打ちかけ")
        page.click("#tab-nippou")
        page.wait_for_selector("#panel-nippou.is-shown")
        self.assertEqual(page.title(), "日報複合ツール — 日報")
        self.assertEqual(page.evaluate("[...document.querySelectorAll('.panel.is-shown')].map(p => p.id)"), ["panel-nippou"])
        # 帯の矢印キー: 続けて押せば続けて動く(ツールの画面へ入らない)
        page.focus("#tab-nippou")
        page.keyboard.press("ArrowRight")
        page.wait_for_selector("#panel-kanban.is-shown")
        page.keyboard.press("ArrowRight")
        page.wait_for_selector("#panel-calendar.is-shown")
        self.assertEqual(page.evaluate("document.activeElement.id"), "tab-calendar", "矢印キーでツールの画面へ入った")
        page.keyboard.press("End")
        page.wait_for_selector("#panel-settings.is-shown")
        page.keyboard.press("Home")
        page.wait_for_selector("#panel-nippou.is-shown")
        page.keyboard.press("ArrowLeft")
        page.wait_for_selector("#panel-settings.is-shown")
        page.keyboard.press("Alt+2")
        page.wait_for_selector("#panel-kanban.is-shown")
        self.assertEqual(self.tool_frame(page, "kanban").input_value("#typed"), "看板の打ちかけ")
        self.assertEqual(self.tool_loads, {t: 1 for t in TOOL_IDS}, "切り替えで読み直した")
        self.assertEqual(sorted(p.split("/")[3] for _, p, _ in self.sent if p.endswith("/open")), sorted(TOOL_IDS),
                         "同じツールを2度起こした")
        # 前に開いたタブを覚えている
        self.assertEqual(page.evaluate("localStorage.getItem('alltools.lastTab')"), "kanban")

    def test_ブラウザ版の心拍_裏に回る_表に戻る_閉じる(self) -> None:
        from portal import web

        page = self.open_shell(first="kanban")
        self.wait_until(page, lambda: self.posts("/api/alive"), "最初の心拍")
        page_id = self.posts("/api/alive")[0]["page"]
        self.assertTrue(page_id)

        def deliver(kind: str) -> dict:
            beacons = [json.loads(b["parts"]) for b in self.kinds("beacon") if b["url"] == "/api/alive"]
            found = [b for b in beacons if b.get(kind)]
            self.assertTrue(found, f"{kind} の合図を送らない")
            self.assertEqual(self.post("/api/alive", found[-1]).status_code, 200)   # 合図は取り次ぎを通らない
            return found[-1]

        page.evaluate("Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'hidden'});"
                      "document.dispatchEvent(new Event('visibilitychange'))")
        self.wait_until(page, lambda: self.kinds("beacon"), "裏に回る合図")
        self.assertEqual(deliver("hidden")["page"], page_id)
        self.assertTrue(web.in_background(), "裏に回っても心拍が途切れたら終わる")
        beats = len(self.posts("/api/alive"))
        page.evaluate("Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'visible'});"
                      "document.dispatchEvent(new Event('visibilitychange'))")
        self.wait_until(page, lambda: len(self.posts("/api/alive")) > beats, "表に戻ってすぐの心拍")
        self.assertFalse(web.in_background())
        page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted: false}))")
        self.wait_until(page, lambda: any("leaving" in b["parts"] for b in self.kinds("beacon")), "閉じた合図")
        self.assertEqual(deliver("leaving")["page"], page_id)
        self.assertFalse(web.in_background())
        # 閉じた画面から遅れて届いた「隠れます」で裏に戻さない
        self.post("/api/alive", {"page": page_id, "hidden": True})
        self.assertFalse(web.in_background())

    def test_ブラウザ版の心拍_裏のあいだの心拍も裏と言う_凍結から戻ればすぐ送る(self) -> None:
        """裏のタブでも最初の5分は20秒ごとのタイマーが動く。以前の心拍は見え方を言わず、
        入口は「表に戻った」と受け取っていた ── そのあとタブが黙って止まる(メモリセーバーの
        破棄など)と、90秒で入口が終わり、日報・点検表も止めていた。

        凍結から戻った合図(`resume`)は document に届く。以前は window で待っていて、来なかった。
        """
        from portal import web

        # 20秒ごとの心拍を、試験が呼べるように控える(20秒は待たない)
        self.ctx.add_init_script(f"""(() => {{
          if (location.origin !== "{ORIGIN}") return;
          const real = window.setInterval;
          window.setInterval = function (fn, ms, ...rest) {{
            if (ms === 20000) window.__periodicBeat = fn;
            return real.call(this, fn, ms, ...rest);
          }};
        }})();""")
        page = self.open_shell(first="kanban")
        self.wait_until(page, lambda: self.posts("/api/alive"), "最初の心拍")
        self.assertTrue(page.evaluate("typeof window.__periodicBeat === 'function'"), "20秒ごとの心拍が無い")
        self.assertIs(self.posts("/api/alive")[0].get("hidden"), False, "表の心拍が見え方を言わない")

        page.evaluate("Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'hidden'});"
                      "document.dispatchEvent(new Event('visibilitychange'))")
        self.wait_until(page, lambda: self.kinds("beacon"), "裏に回る合図")
        hidden = [json.loads(b["parts"]) for b in self.kinds("beacon") if b["url"] == "/api/alive"][-1]
        self.assertEqual(self.post("/api/alive", hidden).status_code, 200)   # 合図は取り次ぎを通らない
        self.assertTrue(web.in_background())

        # 裏のまま、20秒ごとの心拍が回る(間引かれる前の最初の5分)
        beats = len(self.posts("/api/alive"))
        page.evaluate("window.__periodicBeat()")
        self.wait_until(page, lambda: len(self.posts("/api/alive")) > beats, "裏のあいだの心拍")
        self.assertIs(self.posts("/api/alive")[-1].get("hidden"), True, "裏のあいだの心拍が「裏」と言わない")
        self.assertTrue(web.in_background(), "裏のタブの心拍で「表に戻った」と取り違えた")

        # 凍結から戻った(まだ裏のまま)。すぐ心拍を送る
        beats = len(self.posts("/api/alive"))
        page.evaluate("document.dispatchEvent(new Event('resume'))")
        self.wait_until(page, lambda: len(self.posts("/api/alive")) > beats, "凍結から戻ったときの心拍")
        self.assertIs(self.posts("/api/alive")[-1].get("hidden"), True)
        self.assertTrue(web.in_background())

        # 表に戻れば見張りを再開する
        beats = len(self.posts("/api/alive"))
        page.evaluate("Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'visible'});"
                      "document.dispatchEvent(new Event('visibilitychange'))")
        self.wait_until(page, lambda: len(self.posts("/api/alive")) > beats, "表に戻ってすぐの心拍")
        self.assertIs(self.posts("/api/alive")[-1].get("hidden"), False)
        self.assertFalse(web.in_background())


if __name__ == "__main__":
    unittest.main()
