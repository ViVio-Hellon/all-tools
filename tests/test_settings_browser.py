"""入口: **大設定の画面**(`portal/static/js/settings.js`)を本物のブラウザで(段階1)

打ちかけ(置き場所・行の窓)を消さない・2重に書かない・古い答えで描き直さない・
配布設定をまとめて書き出す。要求は Flask のテストクライアントへ回し(`tests/_shell_browser.py`)、
欄の値・出ている文・送った要求を見る。

以前は字を探す試験だけでした(`test_shell_contract.py` の LocationViewTests・SettingsFormTests、
`test_distribution.py` の大設定の画面の文言)。
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from unittest import mock

from tests._shell_browser import ShellBrowserCase


class SettingsCase(ShellBrowserCase):
    def open_settings(self):
        page = self.open_shell(first="settings")
        page.wait_for_function("document.getElementById('lock-state').textContent.includes('鍵が')"
                               " && document.getElementById('loc-state').textContent !== ''")
        return page

    def unlock_in_page(self, page) -> None:
        page.fill("#lock-pass", "nisk")
        page.press("#lock-pass", "Enter")
        page.wait_for_function("document.getElementById('lock-state').textContent.includes('鍵が開いています')")

    def rows(self, page) -> list[list[str]]:
        return page.evaluate("[...document.querySelectorAll('#rights-grid tbody tr:not(.empty)')]"
                             ".map(tr => [...tr.cells].map(td => td.textContent))")

    def add_rule(self, **values) -> dict:
        """共有の DB に行を足す(ほかの端末の行。この端末のタブは「登録なし」で大設定だけになる)。"""
        self.unlock()
        self.post("/api/rights/create-table")
        res = self.post("/api/rights/add", {"login_id": "", "pc_name": "P1", "tab_ids": ["nippou"], **values})
        self.assertEqual(res.status_code, 200, res.get_json())
        return res.get_json()["rules"][-1]


class LocationTests(SettingsCase):
    """共有の DB の置き場所: 反映しているか打ちかけかが見分けられ、描き直しで打ちかけを消さない。"""

    def setUp(self) -> None:
        super().setUp()
        from portal import user_settings
        from portal.rights_store import store

        def restore() -> None:
            user_settings.update({user_settings.KEY_SHARED_DIR: None, user_settings.KEY_SHARED_NAME: None})
            store.sync(force=True)

        self.addCleanup(restore)

    def test_反映済みと打ちかけで見え方が変わり_描き直しても打ちかけは残る(self) -> None:
        page = self.open_settings()
        state = page.locator("#loc-state")
        self.assertIn("反映しています", state.inner_text())
        self.assertIn("(環境変数の場所)", state.inner_text())
        # 欄に入れるのは大設定で決めた値だけ(効いている環境変数の場所は入れない)
        self.assertEqual(page.input_value("#loc-folder"), "")
        saved_look = page.eval_on_selector("#loc-folder", "el => getComputedStyle(el).borderColor")
        page.fill("#loc-folder", r"\\srv\打ちかけ")
        self.assertIn("まだ反映していません", state.inner_text())
        self.assertNotEqual(page.eval_on_selector("#loc-folder", "el => getComputedStyle(el).borderColor"),
                            saved_look, "打ちかけの欄の見え方が同じ")
        # 鍵を開ける・共有から読み直す・タブを行き来する(どれも描き直す)
        self.unlock_in_page(page)
        page.click("#rights-sync")
        self.wait_until(page, lambda: self.posts("/api/rights/sync"), "読み直し")
        page.click("#tab-kanban")
        page.click("#tab-settings")
        page.wait_for_timeout(500)
        self.assertEqual(page.input_value("#loc-folder"), r"\\srv\打ちかけ", "描き直しで打ちかけが消えた")
        self.assertIn("まだ反映していません", state.inner_text())

    def test_変える_既定に戻すが通ったら欄を反映した値に戻す(self) -> None:
        page = self.open_settings()
        self.unlock_in_page(page)
        folder = tempfile.mkdtemp(prefix="alltools_loc_")
        page.fill("#loc-folder", f"  {folder}  ")                # 前後の空白はサーバが落とす
        page.click("#loc-form button[type=submit]")
        page.wait_for_function("document.getElementById('loc-state').textContent.includes('反映しています')")
        self.assertEqual(page.input_value("#loc-folder"), folder)
        page.fill("#loc-folder", r"\\srv\打ちかけ")
        page.click("#loc-default")
        page.wait_for_function("document.getElementById('loc-state').textContent.includes('反映しています')")
        self.assertEqual(page.input_value("#loc-folder"), "")
        self.assertIn("(環境変数の場所)", page.inner_text("#loc-state"))

    def test_反映していない置き場所があれば配布設定を書き出さない(self) -> None:
        page = self.open_settings()
        self.unlock_in_page(page)
        page.fill("#loc-folder", r"\\srv\打ちかけ")
        for button in ("#dist-export", "#dist-export-all"):
            with self.subTest(button=button):
                page.evaluate("document.getElementById('toasts').replaceChildren()")
                page.click(button)
                page.wait_for_function("document.getElementById('toasts').textContent.includes('まだ反映していない値があります')")
                self.assertEqual(page.evaluate("document.activeElement.id"), "loc-folder")
        page.wait_for_timeout(300)
        self.assertEqual(self.posts("/api/distribution/export") + self.posts("/api/distribution/export-all"), [])


class ExportAllTests(SettingsCase):
    """大設定の分と4ツールの分をまとめて書き出す。結果はツールごとに1行、一覧も描き直す。"""

    def test_まとめて書き出すとツールごとに1行出し_一覧も描き直す_書き出すあいだは押せない(self) -> None:
        from portal import distribution

        real = distribution.summary
        made = {"nippou"}

        def summary():
            out = real()
            out["tools"] = [{**t, "exists": t["id"] in made, "created_at": "2026/10/21 09:00" if t["id"] in made else ""}
                            for t in out["tools"]]
            return out

        outcome = {"ok": False, "message": "書き出した 1・書き出せなかった 1", "results": [
            {"id": "portal", "title": "大設定", "state": "ok", "line": "書き出しました", "message": "書き出しました"},
            {"id": "kanban", "title": "看板", "state": "failed", "line": "書き出せませんでした(理由)",
             "message": "書き出せませんでした(理由の長い文)"},
        ]}
        patches = [mock.patch.object(distribution, "summary", summary),
                   mock.patch.object(distribution, "export_all", side_effect=lambda: (made.add("kanban"), outcome)[1])]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        page = self.open_settings()
        # 画面に手順と一覧がある(各ツールの配布設定は、そのツールのフォルダの中にできる)
        steps = page.inner_text(".dist-steps")
        self.assertIn("「4ツールの分もまとめて書き出す」を押す", steps)
        self.assertIn(r"scripts\make_dist.bat", steps)
        table = page.evaluate("[...document.querySelectorAll('#dist-tools tbody tr')].map(tr => tr.cells[0].textContent + ':' + tr.cells[1].textContent)")
        self.assertEqual(len(table), len(self.catalog.tools))
        self.assertIn("日報:あり(作成 2026/10/21 09:00)", table)
        self.assertIn("看板:なし(書き出していません)", table)
        self.unlock_in_page(page)
        button = page.get_by_role("button", name="4ツールの分もまとめて書き出す")
        # 書き出してあるツールは既定の選び方で書き直す。やめれば書き出さない
        self.answers_for_dialogs = [False]
        button.click()
        self.wait_until(page, lambda: self.dialogs, "確かめの窓")
        self.assertIn("各ツールの配布設定を", self.dialogs[0])
        page.wait_for_timeout(200)
        self.assertEqual(self.posts("/api/distribution/export-all"), [])
        self.hold = ("/api/distribution/export-all",)
        button.click()
        self.wait_until(page, lambda: self.held, "書き出しの頼み")
        page.wait_for_function("document.getElementById('dist-export-all').disabled")
        self.assertEqual(page.inner_text("#dist-export-all"), "書き出しています…")
        page.click("#dist-export-all", force=True)                 # 書き出しているあいだに2度押し
        page.wait_for_timeout(200)
        self.assertEqual(len(self.posts("/api/distribution/export-all")), 1)
        self.release(page)
        page.wait_for_function("!document.getElementById('dist-export-all').disabled")
        lines = page.evaluate("[...document.querySelectorAll('#dist-all-results li')].map(li => [li.textContent, li.title])")
        self.assertEqual(lines, [["大設定: 書き出しました", ""],
                                 ["看板: 書き出せませんでした(理由)", "書き出せませんでした(理由の長い文)"]])
        table = page.evaluate("[...document.querySelectorAll('#dist-tools tbody tr')].map(tr => tr.cells[0].textContent + ':' + tr.cells[1].textContent)")
        self.assertIn("看板:あり(作成 2026/10/21 09:00)", table, "一覧を描き直していない")
        self.assertEqual(page.inner_text("#dist-export-all"), "4ツールの分もまとめて書き出す")


    def test_前に入れていたラインと音声ファイルは残すと訊き_残したことを1行で出す(self) -> None:
        """まとめて書き出したら、日報の設定画面で入れていた音声ファイルとラインが消えた
        →「音声ファイルとラインは残して書き出す」。確かめの窓で「外れます」と言わず、結果に残したと出す。"""
        from portal import distribution

        real = distribution.summary

        def summary():
            out = real()
            out["tools"] = [{**t, "exists": t["id"] == "nippou"} for t in out["tools"]]
            return out

        # 1行は本物の組み立て(各ツールの入口の答えの kept)で作る
        nippou = distribution._outcome("nippou", "日報", distribution.Result(
            True, "書き出しました", applied=["共有の日報管理のパス"], carried=["音声ファイル 2 個", "ライン(L-1)"]))
        outcome = {"ok": True, "message": "書き出した 1", "results": [nippou]}
        patches = [mock.patch.object(distribution, "summary", summary),
                   mock.patch.object(distribution, "export_all", return_value=outcome)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        page = self.open_settings()
        self.unlock_in_page(page)
        page.click("#dist-export-all")
        page.wait_for_function("document.querySelectorAll('#dist-all-results li').length === 1")
        self.assertEqual(len(self.dialogs), 1)
        self.assertIn("前に入れていたラインと日報の音声ファイルは、そのまま残します", self.dialogs[0])
        self.assertNotIn("ライン・日報の音声ファイルなど", self.dialogs[0], "残すものを「外れます」と言っている")
        self.assertEqual(page.inner_text("#dist-all-results li"),
                         "日報: 書き出しました(1項目: 共有の日報管理のパス)。音声ファイル 2 個・ライン(L-1)はそのまま残しました")
        steps = page.inner_text(".dist-steps")
        self.assertIn("前に各ツールの設定画面で入れていたライン(値もそのまま)と日報の音声ファイルは、消さずに残します", steps)
        self.assertNotIn("まとめて書き出すと、既定の選び方で書き直します", steps)


class ToolDistributionListTests(SettingsCase):
    """大設定の「配布設定」に、4ツールの配布設定の**中身**が出る(配る前に確かめるため。読むだけ)。

        日報・看板・カレンダーの参照設定は大設定のファイルに入らず、各ツールのフォルダの別ファイルに入る
        ──「そりゃぁ反映しないわな」「両方やってください」

    中身の決め方(新しいほう・パスワードを出さない・古い印)は `test_distribution.CombinedFileTests`。
    ここでは画面に出ること(使うほう・項目と値・一言・なし)を見る。
    """

    def test_ツールごとに使う配布設定と中身と一言が出る_書き出していないツールはなし(self) -> None:
        from portal import distribution

        real = distribution.summary
        rows = {
            "nippou": {"exists": True, "use": "folder", "use_label": r"ツールのフォルダ(tools\nippou\配布設定)",
                       "created_at": "2026-10-27 09:00", "created_on": "PC1", "version": "4.44.6",
                       "contents": [{"key": "gw_reference_dir", "label": "参照用マスタの参照パス",
                                     "value": r"\\srv\参照"},
                                    {"key": "admin_password", "label": "管理者パスワード", "value": "(設定済み)"}],
                       "notes": ["音声ファイル 2件は、まとめたファイルには入りません。"]},
            "kanban": {"exists": True, "use": "integrated", "use_label": r"まとめたファイル(配布設定\日報複合ツール.json)",
                       "created_at": "2026/10/20 09:00:00", "created_on": "PC1", "version": "",
                       "contents": [{"key": "shared_db_path", "label": "接続先(共有DB)",
                                     "value": r"\\srv\看板マスタ.sqlite3"}],
                       "notes": ["大設定の書き出し(2026/10/27 09:00:00)より古いものです(配る前に、まとめて書き出し直すか中身を確かめてください)"]},
        }

        def summary():
            out = real()
            out["file_exists"] = True
            out["file_created_at"] = "2026/10/27 09:00:00"
            out["tools"] = [{**t, **rows.get(t["id"], {"exists": False, "contents": [], "notes": []})}
                            for t in out["tools"]]
            return out

        patcher = mock.patch.object(distribution, "summary", summary)
        patcher.start()
        self.addCleanup(patcher.stop)
        page = self.open_settings()
        page.wait_for_function("document.querySelectorAll('#dist-tools tbody tr').length > 0")
        heads = page.evaluate("[...document.querySelectorAll('#dist-tools thead th')].map(th => th.textContent)")
        self.assertEqual(heads, ["ツール", "配った先が使う配布設定", "中身"])
        cells = page.evaluate("Object.fromEntries([...document.querySelectorAll('#dist-tools tbody tr')]"
                              ".map(tr => [tr.dataset.tool, [...tr.cells].map(td => td.innerText)]))")
        nippou = cells["nippou"]
        self.assertIn("あり(作成 2026-10-27 09:00 / PC1 / VER4.44.6)", nippou[1])
        self.assertIn(r"使うもの: ツールのフォルダ(tools\nippou\配布設定)", nippou[1])
        self.assertIn("※ 音声ファイル 2件は、まとめたファイルには入りません。", nippou[1])
        self.assertIn(r"参照用マスタの参照パス: \\srv\参照", nippou[2])
        self.assertIn("管理者パスワード: (設定済み)", nippou[2])
        self.assertIn(r"使うもの: まとめたファイル(配布設定\日報複合ツール.json)", cells["kanban"][1])
        self.assertIn("※ 大設定の書き出し(2026/10/27 09:00:00)より古いものです", cells["kanban"][1])
        self.assertIn(r"接続先(共有DB): \\srv\看板マスタ.sqlite3", cells["kanban"][2])
        self.assertIn("なし(書き出していません)", cells["calendar"][1])
        lead = page.inner_text("#dist-tools >> xpath=preceding-sibling::p[1]")
        self.assertIn("作った時刻が新しいほう", lead)
        self.assertIn("日報複合ツール.json", lead)
        state = page.inner_text("#dist-state")
        self.assertIn("4ツールの分だけ入っています", state, "大設定の分が既定のままでも、まとめたファイルがあると出す")


class RowDialogTests(SettingsCase):
    """タブ表示権限の行の窓: 2重に足さない・断られても打った値を残す・やめるは訊く。"""

    def open_row(self, page, key=None) -> None:
        if key is None:
            page.click("#rights-add")
        else:
            page.locator("#rights-grid tbody tr", has_text=f"{key}").get_by_role("button", name="直す").click()
        page.wait_for_selector("#row-dialog[open]")

    def test_保存するの2度押しで2行足さない(self) -> None:
        self.unlock()
        self.post("/api/rights/create-table")
        page = self.open_settings()
        self.open_row(page)
        page.fill("#row-login", "taro")
        page.check("#row-tabs input[value=kanban]")
        self.hold = ("/api/rights/add",)
        page.click("#row-save")
        self.wait_until(page, lambda: self.held, "足す頼み")
        self.assertIn("最初の1行です", self.dialogs[0])
        page.click("#row-save", force=True)
        page.press("#row-note", "Enter")
        page.wait_for_timeout(300)
        self.assertEqual(len(self.posts("/api/rights/add")), 1, "2度押しで2行足す")
        self.release(page)
        page.wait_for_selector("#row-dialog", state="hidden")
        self.assertEqual([r[1] for r in self.rows(page)], ["taro"])

    def test_ほかの人が先に直していたら打った値を残し_もう一度で通る(self) -> None:
        rule = self.add_rule(note="もと")
        page = self.open_settings()
        self.open_row(page, rule["key"])
        page.fill("#row-note", "わたしの")
        db = sqlite3.connect(self.db)
        db.execute('UPDATE "タブ表示権限" SET "備考" = \'ほかの端末\'')
        db.commit()
        db.close()
        page.click("#row-save")
        page.wait_for_function("!document.getElementById('row-error').hidden")
        self.assertIn("ほかの人が先にこの行を直していました", page.inner_text("#row-error"))
        self.assertTrue(page.is_visible("#row-dialog"), "窓を閉じた(打った値が消える)")
        self.assertEqual(page.input_value("#row-note"), "わたしの")
        self.assertEqual(self.rows(page)[0][6], "ほかの端末", "読み直した表を描いていない")
        page.click("#row-save")
        page.wait_for_selector("#row-dialog", state="hidden")
        self.assertEqual(self.rows(page)[0][6], "わたしの")

    def test_鍵が掛かったら窓の中で開け直せて_打った値は残る(self) -> None:
        from portal import admin_password

        rule = self.add_rule()
        page = self.open_settings()
        self.open_row(page, rule["key"])
        page.fill("#row-note", "わたしの")
        admin_password.session.lock()                       # 30分さわらなかった
        page.click("#row-save")
        page.wait_for_selector("#row-unlock:not([hidden])")
        self.assertIn("鍵が掛かりました", page.inner_text("#row-error"))
        self.assertTrue(page.is_visible("#row-dialog"))
        self.assertEqual(page.input_value("#row-note"), "わたしの")
        page.fill("#row-unlock-pass", "nisk")
        page.click("#row-unlock-go")
        page.wait_for_function("document.getElementById('row-error').textContent.includes('鍵を開けました')")
        page.click("#row-save")
        page.wait_for_selector("#row-dialog", state="hidden")
        self.assertEqual(self.rows(page)[0][6], "わたしの")

    def test_古い答えで描き直さない(self) -> None:
        self.add_rule(pc_name="TEST-PC", note="1行め")       # この端末の行(日報のタブが出る)
        page = self.open_settings()
        page.click("#tab-nippou")
        self.hold_answered = ("/api/settings$",)            # タブを開いたときの読み直し(1行のときの答え)
        page.click("#tab-settings")
        self.wait_until(page, lambda: self.held, "読み直し")
        self.hold_answered = ()
        self.open_row(page)
        page.fill("#row-pc", "P2")
        page.check("#row-tabs input[value=kanban]")
        page.click("#row-save")
        page.wait_for_selector("#row-dialog", state="hidden")
        self.assertEqual(len(self.rows(page)), 2)
        self.release(page)                                  # 先に頼んだ古い答えが、あとから届く
        page.wait_for_timeout(300)
        self.assertEqual(len(self.rows(page)), 2, "古い答えで描き直した")

    def test_やめる_Escは打った値があれば訊く(self) -> None:
        self.add_rule()
        page = self.open_settings()
        self.open_row(page)
        page.click("#row-cancel")
        page.wait_for_selector("#row-dialog", state="hidden")
        self.assertEqual(self.dialogs, [], "打っていないのに訊いた")
        self.open_row(page)
        page.fill("#row-login", "taro")
        self.answers_for_dialogs = [False, True]
        page.click("#row-cancel")
        self.wait_until(page, lambda: self.dialogs, "確かめの窓")
        self.assertIn("打った値はまだ保存していません", self.dialogs[0])
        self.assertTrue(page.is_visible("#row-dialog"))
        page.press("#row-login", "Escape")
        self.wait_until(page, lambda: len(self.dialogs) == 2, "Esc の確かめ")
        page.wait_for_selector("#row-dialog", state="hidden")


if __name__ == "__main__":
    unittest.main()
