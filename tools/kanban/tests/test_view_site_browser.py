"""倉庫参照 + 現場のライン1つを、本物のブラウザで(看板の画面と設定画面)。

    - 開くと担当ライン(現場)のタブが先頭で選ばれ、サイズ(発注)が押せる・一括確認がある・
      「参照専用」の帯は出ない。押すと送られて赤になる
    - 倉庫参照のライン(LVC)のタブへ移ると、どのボタンも押せず、一括確認は消え、帯が出る
      (「このラインは参照だけです…担当ライン「機側」のタブでできます」)。上の札は「倉庫参照(担当: 機側)」
    - 設定 →「この端末」のモードのすぐ下の「担当ライン(倉庫参照のまま、このラインだけ現場の操作をする)」
      でラインを選ぶと併用になり、「なし」で切れる(VER3.6.0)。現場モードの権限がある端末は
      パスワードを訊かない。権限が無い端末は管理者パスワードを訊き、入れれば通る(やめれば変わらない)

無い環境では飛ばします(CI では飛ばしません: ``ALLTOOLS_REQUIRE_BROWSER=1``)。
"""
from __future__ import annotations

import json
import os
import sqlite3
from unittest import mock

from kanban import config
from tests._kanban_browser import KanbanBrowserCase
from tests.test_view_site import _import
from tests.test_web_app import TEST_ADMIN_PASSWORD

SIZE_2 = '#board .row[data-no="2"] .cell[data-kind="size"]'


class ViewWithSiteBoardBrowserTest(KanbanBrowserCase):
    MODE = config.MODE_WAREHOUSE_VIEW
    LINE = "LS"

    def setUp(self) -> None:
        super().setUp()
        self.app.config["VIEW_SITE"] = True
        self.store.set_view_site(True)

    def test_担当ラインは現場の操作ができ_ほかのラインは見るだけ(self) -> None:
        page = self.open("/board")
        page.wait_for_selector('#board[data-line="LS"] .row[data-no="2"]')
        tab = page.locator('#line-tabs .tab[data-line="LS"]')
        self.assertEqual(tab.get_attribute("aria-selected"), "true")
        self.assertIn("(現場)", tab.inner_text())
        self.assertTrue(page.locator(SIZE_2).is_enabled(), "担当ラインのサイズ(発注)が押せない")
        self.assertTrue(self.visible(page, "#batch-reset"), "一括確認が無い")
        self.assertFalse(self.visible(page, "#read-only-note"), "担当ラインなのに「参照専用」が出ている")
        self.assertEqual(self.text(page, ".mode-chip"), "倉庫参照(担当: 機側)")

        # 2 番は発注中。押すと取り消し(確かめの窓は決める)
        page.click(SIZE_2)
        page.wait_for_selector(f'{SIZE_2}[data-state="neutral"]')
        orders = self.posted("/api/board/order")
        self.assertEqual(orders[-1]["line"], "LS", orders)

        # 倉庫参照のラインへ
        page.click('#line-tabs .tab[data-line="LVC"]')
        page.wait_for_selector('#board[data-line="LVC"]:not([aria-busy]) .row[data-no="2"]')
        page.wait_for_selector("#batch-reset", state="hidden")
        self.assertTrue(self.visible(page, "#read-only-note"), "倉庫参照のラインで「参照専用」が出ない")
        self.assertIn("担当ライン「機側」のタブでできます", self.text(page, "#read-only-note"))
        enabled = page.evaluate(
            "() => [...document.querySelectorAll('#board .cell[data-kind]')]"
            ".filter((b) => ['size', 'ship', 'hold'].includes(b.dataset.kind) && !b.disabled).length")
        self.assertEqual(enabled, 0, "倉庫参照のラインで押せるボタンがある")
        self.assertEqual(self.errors, [])


class ViewSiteSettingsBrowserTest(KanbanBrowserCase):
    MODE = config.MODE_WAREHOUSE_VIEW
    LINE = "LS"

    def setUp(self) -> None:
        super().setUp()
        _import(self.store, "HVC")
        patcher = mock.patch.dict(os.environ, {"KANBAN_LOCAL_DIR": str(self.dir / "local")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def deny_site(self) -> None:
        """この端末の権限を倉庫モードだけにする(現場モード mode:field が無い)。"""
        from kanban import access_control as ac

        path = self.dir / "梱包資材マスタ.sqlite3"
        conn = sqlite3.connect(str(path))
        conn.execute(ac.DDL)
        me = ac.current_identity()
        conn.execute('INSERT INTO "アクセス権限" (ログインID, PC名, 権限, 有効) VALUES (?, ?, ?, 1)',
                     (me.login_id, me.pc_name, "mode:material"))
        conn.commit()
        conn.close()
        (self.dir / "config.json").write_text(json.dumps({"access_db_path": str(path)}), encoding="utf-8")
        self.app.config["ACCESS_GRANT"] = ac.grant_for(config.load_config(str(self.dir / "config.json")),
                                                       self.store)

    def wait_chip(self, page, text: str) -> None:
        """読み直して、上の札が ``text`` になるまで。"""
        page.wait_for_function(
            "(t) => (document.querySelector('.mode-chip') || {}).textContent === t", arg=text, timeout=15000)

    def test_この端末で担当ラインを選ぶと併用になり_なしで切れる(self) -> None:
        page = self.open_settings("device")
        self.assertEqual(page.locator("#view-site").input_value(), "", "併用していないのに「なし」でない")
        self.assertEqual(page.locator("#line").count(), 0, "倉庫参照に現場モードの担当ラインの欄がある")
        self.assertTrue(page.locator("#view-site").is_enabled(), "担当ラインが灰色で選べない")
        self.assertIn("担当ライン(倉庫参照のまま、このラインだけ現場の操作をする)",
                      self.text(page, "#view-site-field"))
        # モードの選択のすぐ下にある(この端末の面の、モードの次の欄)
        self.assertTrue(page.evaluate(
            "() => document.getElementById('mode').closest('.field').nextElementSibling"
            " === document.getElementById('view-site-field')"), "モードのすぐ下に無い")
        self.assertIn("パスワードは要りません", self.text(page, "#view-site-why"))
        self.assertEqual(self.text(page, ".mode-chip"), "倉庫参照モード(操作不可)")

        page.select_option("#view-site", "HVC")
        self.wait_until(page, lambda: bool(self.posted("/api/view-site")), "担当ラインを送った")
        self.assertEqual(self.posted("/api/view-site")[-1], {"line": "HVC"}, "パスワードを送っている")
        self.assertFalse(self.visible(page, "#pw"), "権限があるのにパスワードを訊いた")
        self.wait_chip(page, "倉庫参照(担当: HVC)")            # 読み直すと札に出る
        self.assertEqual(page.locator("#view-site").input_value(), "HVC")
        self.assertTrue(self.store.get_view_site())
        self.assertFalse(self.store.view_site_by_admin())
        self.assertEqual(self.app.config["LINE"], "HVC")

        page.select_option("#view-site", "")
        self.wait_until(page, lambda: self.posted("/api/view-site")[-1] == {"line": ""}, "「なし」を送った")
        self.wait_chip(page, "倉庫参照モード(操作不可)")
        self.assertEqual(page.locator("#view-site").input_value(), "")
        self.assertFalse(self.store.get_view_site())
        self.assertFalse(self.app.config["VIEW_SITE"])
        self.assertEqual(self.errors, [])

    def test_権限が無い端末は管理者パスワードで担当ラインを決める(self) -> None:
        self.deny_site()
        page = self.open_settings("device")
        page.wait_for_function(
            "() => document.getElementById('access-source').textContent.includes('いま読みました')")
        why = self.text(page, "#view-site-why")
        self.assertIn("現場モード(mode:field)の権限がありません", why)
        self.assertIn("管理者パスワードを訊きます", why)
        self.assertTrue(page.locator("#view-site").is_enabled(), "権限が無いだけで灰色になっている")

        # やめれば変わらない(送らない)
        page.select_option("#view-site", "HVC")
        page.wait_for_selector("#pw:not([hidden])")
        self.assertIn("管理者パスワードが要ります", self.text(page, "#pw-note"))
        page.click("#pw-cancel")
        page.wait_for_selector("#pw", state="hidden")
        self.assertEqual(page.locator("#view-site").input_value(), "")
        self.assertEqual(self.posted("/api/view-site"), [])

        # 違うパスワードは断る(変わらない)
        page.select_option("#view-site", "HVC")
        self.answer_password(page, TEST_ADMIN_PASSWORD + "違う")
        self.wait_toast(page, "パスワードが違います")
        page.wait_for_function("() => document.getElementById('view-site').value === ''")
        self.assertFalse(self.store.get_view_site())

        # 入れれば通る
        page.select_option("#view-site", "HVC")
        self.answer_password(page, TEST_ADMIN_PASSWORD)
        self.wait_chip(page, "倉庫参照(担当: HVC)")
        self.assertEqual(self.posted("/api/view-site")[-1].get("line"), "HVC")
        self.assertTrue(self.store.view_site_by_admin(), "パスワードで決めた印が無い")
        self.assertEqual(page.locator("#view-site").input_value(), "HVC")
        self.assertEqual(self.errors, [])
