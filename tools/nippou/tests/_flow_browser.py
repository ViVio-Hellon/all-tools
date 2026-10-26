"""日報の**入力画面以外の流れ**を本物のブラウザで通す試験の土台(`test_flow_screens_browser`・
`test_flow_views_browser`)。

`_other_screen_browser.ScreenBrowserCase` に、次を足しただけです:

    - **共有の置き場所も一時フォルダへ**(既定は利用者のフォルダの `NippouAccess`。本物を読まない・書かない)
    - **console の error も拾う**(`console`)。ただし「Failed to load resource」は外す ── 断り(403・422)は
      ブラウザがそう書くだけで、断りの文言は試験が画面で確かめる
    - `assert_clean(page)` … 画面の誤り・console の error・「操作を読み込めませんでした」の札が無い
    - `tab(page)` … 設定画面でいま選ばれている面
    - `save_path(page, key, path)` … 参照設定の1行を打って保存し、保存し終えるのを待つ
    - `put_access_table(*rights)` … アクセス権限の表(梱包資材マスタ)に**このPCの行**を置く

時間には頼らず、画面の状態(要素・字・送った要求)を待ちます(Windows の CI は遅い)。
パスは字のまま比べず `Path` にしてから比べます(Windows の `\\`)。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from tests._other_screen_browser import ORIGIN, ScreenBrowserCase

__all__ = ["ORIGIN", "FlowCase"]


class FlowCase(ScreenBrowserCase):
    #: 共有の置き場所を一時フォルダへ寄せるか(まっさらの起動を見る試験は False)
    share_in_tmp = True

    def setUp(self) -> None:
        super().setUp()
        self.console: list[str] = []
        if self.share_in_tmp:
            from nippou import config, user_settings

            self.share = self.tmp / "共有"
            self.share.mkdir(parents=True, exist_ok=True)
            user_settings.save_many({config.KEY_ACCESS_DIR: str(self.share),
                                     config.KEY_BACKUP_DIR: str(self.share / "LocalBackup")})

    # -- 画面 ------------------------------------------------------------
    def watch(self, page):
        super().watch(page)
        page.set_default_timeout(15000)

        def seen(msg) -> None:
            if msg.type == "error" and not msg.text.startswith("Failed to load resource"):
                self.console.append(msg.text)

        page.on("console", seen)
        return page

    def assert_clean(self, page) -> None:
        self.assertEqual(self.errors, [], "画面の誤り(pageerror)が出ました")
        self.assertEqual(self.console, [], "console に error が出ました")
        wire = page.query_selector(".msg--wire")
        self.assertTrue(wire is None or not wire.is_visible(),
                        "「この画面の操作を読み込めませんでした」が出ています")

    def assertSamePath(self, got, want, msg: str = "") -> None:
        """同じ場所か。**字では比べない**(Windows の `\\`・一時フォルダの短い名前 RUNNER~1)。"""
        self.assertEqual(Path(got).resolve(), Path(want).resolve(), msg)

    @staticmethod
    def tab(page, group: str = "#settingsTabs") -> str:
        return page.eval_on_selector(
            group, "(root) => root.querySelector(':scope > .tabs__bar > .tab[aria-selected=\"true\"]')"
                   "?.dataset.key || ''")

    def open_screen(self, path: str, ready: str = "main#main", **_):
        """開いて、画面のモジュールが繋がるまで待つ(決まった時間は待たない)。"""
        page = self.watch(self.ctx.new_page())
        page.goto(f"{ORIGIN}{path}", wait_until="load")
        page.wait_for_selector(ready, state="attached")
        page.wait_for_function("window.APP && window.APP.booted === true")
        self.settle(page)
        return page

    @staticmethod
    def settle(page) -> None:
        """この画面のモジュール(`#pagescripts [data-view]`)が読み込まれ、`start()` まで済むのを待つ。

        `nav.mountViews` は同じモジュールを `import()` して `start()` を呼ぶ。こちらも同じものを
        `import()` して待てば、向こうの `start()` も済んでいる(読み込みは1回きりで、続きは先に並ぶ)。
        """
        page.wait_for_function("!document.querySelector('main#main')?.hasAttribute('aria-busy')")
        page.evaluate("""async () => {
            for (const tag of document.querySelectorAll('#pagescripts [data-view]')) {
                try { await import(tag.dataset.view); } catch (e) { /* 札は nav が出す */ }
            }
            await new Promise((done) => setTimeout(done, 0));
        }""")

    def go_rail(self, page, href: str, ready: str = "main#main") -> None:
        """左の並びで移る(画面の差し替え `nav.go` を通る)。"""
        page.click(f".rail a[href='{href}']")
        page.wait_for_function(f"location.pathname === {href!r}")
        page.wait_for_selector(ready, state="attached")
        self.settle(page)

    def unlock_bar(self, page, key: str, password: str = "nisk") -> None:
        """面の上の鍵の帯で鍵を開ける(ボタンで)。開いた帯が出るまで待つ。"""
        page.fill(f"#lock-pw-{key}", password)
        page.click(f"[data-lock='{key}'] [data-lock-on]")
        page.wait_for_selector(f"[data-lock='{key}'].lockbar--open", state="attached")

    def save_path(self, page, key: str, path: Path) -> str:
        """参照設定の1行を打って保存する。保存したあとの行の字を返す。"""
        row = f"[data-path-key='{key}']"
        page.fill(f"#path-{key}", str(path))
        page.click(f"{row} [data-save-path]")
        page.wait_for_function(
            f"document.querySelector(\"{row} [data-path-state]\")?.textContent.includes('保存しました')")
        return page.inner_text(row)

    # -- マスタ ----------------------------------------------------------
    def put_access_table(self, *rights: str) -> None:
        """アクセス権限の表(現物の置き場所 = 梱包資材マスタ)に、このPCの行を置く。"""
        from nippou import source_db
        from nippou.config import SETTINGS
        from nippou.services import access_rights

        me = access_rights.identity()
        path = Path(SETTINGS.gw_material_master_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        with conn:
            conn.execute('DROP TABLE IF EXISTS "アクセス権限"')
            conn.execute('CREATE TABLE "アクセス権限" (管理番号 INTEGER, ログインID TEXT, PC名 TEXT,'
                         ' 権限 TEXT, 有効 INTEGER, 備考 TEXT)')
            conn.executemany('INSERT INTO "アクセス権限" VALUES (?, ?, ?, ?, 1, "")',
                             [(n, me.login, me.pc, r) for n, r in enumerate(rights, 1)])
        conn.close()
        source_db.forget(path)                       # 写しを捨てて読み直させる

    @staticmethod
    def rail(page) -> list[str]:
        """左の並びで見えている画面の名前。"""
        return page.eval_on_selector_all(
            ".rail li", "(items) => items.filter((li) => !li.hidden)"
                        ".map((li) => li.querySelector('.label')?.textContent.trim())")
