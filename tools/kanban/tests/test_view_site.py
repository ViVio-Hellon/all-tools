"""倉庫参照モード(操作不可)+ 現場モードのライン1つ。

現場の要望: 「この端末: 倉庫参照モード(操作不可)の場合は現場モードのライン1つと併用できるようにしてほしい」

    - 倉庫参照の端末でも、担当ライン1つを選べ、そのラインの現場の操作(発注・届いた資材の
      確認・注文票・コメント)はできる
    - 倉庫の画面(ほかのライン)は見るだけ。倉庫の操作(発送・注文中・一括発送)は断る
    - 併用の記録が無い端末(前の版から使っている端末)は今までどおり倉庫参照だけ
    - 関門はアクセス権限(現場モード)。開き直さずに効く(モードは倉庫参照のまま)
"""
from __future__ import annotations

import json
import os
import sqlite3
import unittest
from unittest import mock

from kanban import config, presence
from kanban.db.store import DEFAULT_COLUMN_MAP
from tests.test_web_app import HAS_FLASK, RouteTestBase, sample_rows


def _import(store, line: str) -> None:
    store.import_line(
        line=line, table_name=f"看板_{line}", key_column="管理番号", key_category="TEXT",
        column_map=dict(DEFAULT_COLUMN_MAP),
        categories={name: "TEXT" for name in DEFAULT_COLUMN_MAP.values()},
        rows=sample_rows(), source_path="dummy.accdb")


@unittest.skipUnless(HAS_FLASK, "Flask が無い")
class ViewWithSiteLineTest(RouteTestBase):
    """倉庫参照 + 現場の担当ライン「機側」(LS)。LVC は倉庫参照(見るだけ)。"""

    MODE = config.MODE_WAREHOUSE_VIEW
    LINE = "LS"
    VIEW_SITE = True

    def setUp(self) -> None:
        super().setUp()
        self.app.config["VIEW_SITE"] = self.VIEW_SITE
        self.store.set_view_site(self.VIEW_SITE)

    def board(self, line: str) -> dict:
        res = self.get(f"/api/board?line={line}")
        self.assertEqual(res.status_code, 200, res.json)
        return res.json["board"]

    def press(self, path: str, line: str, no: str = "2"):
        return self.post(f"/api/board/{path}",
                         {"line": line, "mgmt_no": no, "rev": self.rev(no, line)})

    def test_site_line_is_operable_and_others_are_read_only(self):
        site = self.board("LS")
        self.assertEqual(site["mode"], config.MODE_SITE)
        self.assertFalse(site["read_only"])
        self.assertTrue(site["can_comment"])
        self.assertEqual(site["comment_side"], "現場")
        sizes = [r["size"]["enabled"] for g in site["groups"] for r in g["rows"]]
        self.assertTrue(any(sizes), "担当ラインのサイズ(発注)が押せない")

        view = self.board("LVC")
        self.assertEqual(view["mode"], config.MODE_WAREHOUSE_VIEW)
        self.assertTrue(view["read_only"])
        self.assertFalse(view["can_comment"])
        for row in (r for g in view["groups"] for r in g["rows"]):
            for kind in ("size", "ship", "hold"):
                self.assertFalse(row[kind]["enabled"], f"倉庫参照のラインで {kind} が押せる")

    def test_site_line_tab_comes_first_and_is_labelled(self):
        html = self.get("/board").get_data(as_text=True)
        self.assertIn("機側(現場)", html)
        self.assertLess(html.index('data-line="LS"'), html.index('data-line="LVC"'),
                        "担当ラインのタブが先頭に無い")
        self.assertIn('id="batch-reset"', html, "一括確認のボタンが無い")

    def test_site_operations_pass_on_site_line(self):
        res = self.press("order", "LS")
        self.assertEqual(res.status_code, 200, res.json)
        self.assertEqual(res.json["board"]["mode"], config.MODE_SITE)
        self.assertGreater(self.store.pending_count(), 0, "押した操作を送る形で預かっていない")

        res = self.post("/api/board/batch-reset", {"line": "LS", "items": self.batch_items("LS")})
        self.assertEqual(res.status_code, 200, res.json)

    def test_operations_on_other_lines_are_refused(self):
        before = self.store.item("LVC", "2")
        for path in ("order", "ship", "hold", "acknowledge"):
            res = self.press(path, "LVC")
            self.assertIn(res.status_code, (403, 404), f"倉庫参照のラインで {path} が通った")
        res = self.post("/api/board/batch-reset", {"line": "LVC", "items": self.batch_items("LVC")})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(self.store.item("LVC", "2").rev, before.rev, "断ったのに変わっている")

    def test_warehouse_operations_are_refused_even_on_site_line(self):
        """倉庫(操作可)との併用ではない。担当ラインでも注文中・一括発送はできない。"""
        for path in ("hold",):
            self.assertEqual(self.press(path, "LS").status_code, 403, path)
        res = self.post("/api/board/batch-ship", {"line": "LS", "items": self.batch_items("LS")})
        self.assertEqual(res.status_code, 403)

    def test_comments_as_site_only_on_site_line(self):
        res = self.post("/api/comments", {"line": "LS", "mgmt_no": "2", "body": "急ぎです"})
        self.assertEqual(res.status_code, 200, res.json)
        self.assertEqual(res.json["side"], "現場")
        res = self.post("/api/comments", {"line": "LVC", "mgmt_no": "2", "body": "急ぎです"})
        self.assertEqual(res.status_code, 403)

    def test_reports_follow_the_line(self):
        site = self.get("/api/report/available?line=LS").json["reports"]
        self.assertEqual([r["kind"] for r in site], ["site"])
        self.assertEqual(self.get("/api/report/available?line=LVC").json["reports"], [])
        self.assertEqual(self.get("/report/site?line=LS").status_code, 200)
        self.assertEqual(self.get("/report/site?line=LVC").status_code, 404)
        self.assertEqual(self.get("/report/warehouse?line=LVC").status_code, 404)

    def test_presence_names_the_site_line_and_hides_itself(self):
        self.assertTrue(presence.registers(config.MODE_WAREHOUSE_VIEW, True))
        self.assertEqual(presence.registered_line(config.MODE_WAREHOUSE_VIEW, "LS", True), "LS")
        self.store.upsert_line_status_rows([
            {"line": "LS", "status": config.STATE_OPEN, "sort_order": 1,
             "stamp": "2026/08/31 09:00:00", "host": "PC-WEB"},
            {"line": "LVC", "status": config.STATE_OPEN, "sort_order": 2,
             "stamp": "2026/08/31 09:00:00", "host": "PC-B"},
        ])
        rows = self.get("/api/status").json["presence"]
        self.assertEqual([r["line"] for r in rows], ["LVC"], "自分(担当ライン)が他人として並ぶ")

    def test_line_can_be_changed_with_password(self):
        res = self.post("/api/line", {"line": "LVC", "password": "秘密"})
        self.assertEqual(res.status_code, 200, res.json)
        self.assertEqual(self.app.config["LINE"], "LVC")
        self.assertEqual(self.board("LVC")["mode"], config.MODE_SITE)
        self.assertEqual(self.board("LS")["mode"], config.MODE_WAREHOUSE_VIEW)

    def test_settings_show_the_choice_and_the_line(self):
        data = self.get("/api/settings").json
        self.assertTrue(data["view_site"])
        html = self.get("/settings").get_data(as_text=True)
        self.assertIn('id="view-site"', html)
        self.assertIn('id="line"', html)
        self.assertIn("倉庫参照モード(操作不可)+現場", html)

    def test_own_comments_are_not_new_on_warehouse_tabs(self):
        """担当ラインで自分が書いたコメントを、倉庫参照の盤で「新しいコメント」と出さない。"""
        self.post("/api/comments", {"line": "LS", "mgmt_no": "2", "body": "急ぎです"})
        view = self.board("LVC")
        self.assertEqual([a["line"] for a in view["comment_alerts"]], [])


@unittest.skipUnless(HAS_FLASK, "Flask が無い")
class ViewWithoutSiteLineTest(ViewWithSiteLineTest):
    """**併用しない倉庫参照は今までどおり。** 設定に現場の頃の担当ラインが残っていても見るだけ。"""

    VIEW_SITE = False

    def test_site_line_is_operable_and_others_are_read_only(self):
        for line in ("LS", "LVC"):
            board = self.board(line)
            self.assertEqual(board["mode"], config.MODE_WAREHOUSE_VIEW)
            self.assertTrue(board["read_only"])

    def test_site_line_tab_comes_first_and_is_labelled(self):
        html = self.get("/board").get_data(as_text=True)
        self.assertNotIn("(現場)", html)
        self.assertNotIn('id="batch-reset"', html)

    def test_site_operations_pass_on_site_line(self):
        self.assertIn(self.press("order", "LS").status_code, (403, 404))
        self.assertEqual(self.store.pending_count(), 0)

    def test_comments_as_site_only_on_site_line(self):
        res = self.post("/api/comments", {"line": "LS", "mgmt_no": "2", "body": "急ぎです"})
        self.assertEqual(res.status_code, 403)

    def test_reports_follow_the_line(self):
        self.assertEqual(self.get("/api/report/available?line=LS").json["reports"], [])

    def test_presence_names_the_site_line_and_hides_itself(self):
        self.assertFalse(presence.registers(config.MODE_WAREHOUSE_VIEW))
        self.assertEqual(presence.registered_line(config.MODE_WAREHOUSE_VIEW, "LS"), "")

    def test_line_can_be_changed_with_password(self):
        res = self.post("/api/line", {"line": "LVC", "password": "秘密"})
        self.assertEqual(res.status_code, 403)

    def test_settings_show_the_choice_and_the_line(self):
        self.assertFalse(self.get("/api/settings").json["view_site"])
        html = self.get("/settings").get_data(as_text=True)
        self.assertIn('id="view-site"', html)
        self.assertNotIn('id="line"', html, "併用していないのに担当ラインの欄が出る")

    def test_own_comments_are_not_new_on_warehouse_tabs(self):
        pass

    def test_old_store_has_no_choice(self):
        """前の版の手元の SQLite(記録が無い)は併用しない。"""
        self.store.set_meta("device_view_site", "")
        self.assertFalse(self.store.get_view_site())


@unittest.skipUnless(HAS_FLASK, "Flask が無い")
class ViewSiteSwitchTest(RouteTestBase):
    """設定 → この端末 の「現場モードのライン1つも使う」(``POST /api/view-site``)。"""

    MODE = config.MODE_WAREHOUSE_VIEW
    LINE = "LS"

    def setUp(self) -> None:
        super().setUp()
        self.applied: list[bool] = []
        self.app.config["APPLY_VIEW_SITE"] = self.applied.append
        patcher = mock.patch.dict(os.environ, {"KANBAN_LOCAL_DIR": str(self.dir / "local")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _grant(self, *codes: str) -> None:
        from kanban import access_control as ac

        path = self.dir / "梱包資材マスタ.sqlite3"
        conn = sqlite3.connect(str(path))
        conn.execute(ac.DDL)
        me = ac.current_identity()
        conn.executemany('INSERT INTO "アクセス権限" (ログインID, PC名, 権限, 有効) VALUES (?, ?, ?, 1)',
                         [(me.login_id, me.pc_name, code) for code in codes])
        conn.commit()
        conn.close()
        (self.dir / "config.json").write_text(json.dumps({"access_db_path": str(path)}), encoding="utf-8")

    def test_turning_on_applies_without_reopening(self):
        res = self.post("/api/view-site", {"on": True})
        self.assertEqual(res.status_code, 200, res.json)
        self.assertTrue(res.json["changed"])
        self.assertTrue(self.store.get_view_site(), "次の起動に残らない")
        self.assertTrue(self.app.config["VIEW_SITE"])
        self.assertEqual(self.applied, [True], "走っているプロセスに当てていない")
        self.assertEqual(self.post("/api/board/order", {
            "line": "LS", "mgmt_no": "2", "rev": self.rev("2", "LS")}).status_code, 200)

        res = self.post("/api/view-site", {"on": False})
        self.assertEqual(res.status_code, 200)
        self.assertFalse(self.store.get_view_site())
        self.assertEqual(self.applied, [True, False])
        self.assertIn(self.post("/api/board/order", {
            "line": "LS", "mgmt_no": "2", "rev": self.rev("2", "LS")}).status_code, (403, 404))

    def test_needs_site_permission_not_password(self):
        """関門はモードと同じアクセス権限。現場の権限が無い端末は併用できない。"""
        self._grant("mode:material")
        res = self.post("/api/view-site", {"on": True, "password": "秘密"})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json["error"]["code"], "no_permission")
        self.assertIn("mode:field", res.json["error"]["message"])
        self.assertFalse(self.store.get_view_site())
        self.assertFalse(self.app.config["VIEW_SITE"])
        self.assertEqual(self.applied, [])

    def test_turning_off_needs_no_permission(self):
        self.store.set_view_site(True)
        self.app.config["VIEW_SITE"] = True
        self._grant("mode:material")
        self.assertEqual(self.post("/api/view-site", {"on": False}).status_code, 200)
        self.assertFalse(self.store.get_view_site())


@unittest.skipUnless(HAS_FLASK, "Flask が無い")
class ViewSiteOtherModesTest(RouteTestBase):
    """併用は倉庫参照のときだけ。倉庫(操作可)とは併用しない。"""

    MODE = config.MODE_WAREHOUSE
    LINE = ""

    def test_refused_outside_view_mode(self):
        res = self.post("/api/view-site", {"on": True})
        self.assertEqual(res.status_code, 403)
        self.assertFalse(self.store.get_view_site())

    def test_flag_is_ignored_outside_view_mode(self):
        from app import create_app

        app = create_app(config.MODE_WAREHOUSE, token="t", port=8751, store=self.store,
                         line="LS", view_site=True)
        self.assertFalse(app.config["VIEW_SITE"])
        self.assertNotIn('id="view-site"', self.get("/settings").get_data(as_text=True))


class StartupTest(unittest.TestCase):
    """起動のとき(``start_app``)の、取り込むライン・併用するか。"""

    def test_relevant_lines_add_a_non_warehouse_site_line(self):
        import start_app

        lines = start_app._relevant_lines(config.MODE_WAREHOUSE_VIEW, "NS1", True)
        self.assertIn("NS1", lines, "倉庫対象でない担当ラインを取り込まない")
        self.assertNotIn("NS1", start_app._relevant_lines(config.MODE_WAREHOUSE_VIEW, "NS1"))
        self.assertEqual(lines.count("LVC"), 1)
        self.assertEqual(start_app._relevant_lines(config.MODE_WAREHOUSE_VIEW, "LVC", True),
                         config.warehouse_line_codes())

    def test_view_site_needs_view_mode_record_and_permission(self):
        import start_app

        store = mock.Mock()
        store.get_view_site.return_value = True
        allow = mock.Mock()
        allow.allows.return_value = True
        deny = mock.Mock()
        deny.allows.return_value = False
        self.assertTrue(start_app._view_site_on(config.MODE_WAREHOUSE_VIEW, store, allow))
        self.assertFalse(start_app._view_site_on(config.MODE_WAREHOUSE_VIEW, store, deny))
        self.assertFalse(start_app._view_site_on(config.MODE_WAREHOUSE, store, allow))
        self.assertFalse(start_app._view_site_on(config.MODE_SITE, store, allow))
        store.get_view_site.return_value = False
        self.assertFalse(start_app._view_site_on(config.MODE_WAREHOUSE_VIEW, store, allow))
        store.get_view_site.side_effect = OSError("読めない")
        self.assertFalse(start_app._view_site_on(config.MODE_WAREHOUSE_VIEW, store, allow))


@unittest.skipUnless(HAS_FLASK, "Flask が無い")
class NonWarehouseSiteLineTest(RouteTestBase):
    """倉庫対象でないライン(NS1)を担当にしても、そのタブが出て操作できる。"""

    MODE = config.MODE_WAREHOUSE_VIEW
    LINE = "NS1"

    def setUp(self) -> None:
        super().setUp()
        _import(self.store, "NS1")
        self.app.config["VIEW_SITE"] = True

    def test_tab_and_operation(self):
        html = self.get("/board").get_data(as_text=True)
        self.assertIn('data-line="NS1"', html)
        res = self.post("/api/board/order", {"line": "NS1", "mgmt_no": "2", "rev": self.rev("2", "NS1")})
        self.assertEqual(res.status_code, 200, res.json)


if __name__ == "__main__":
    unittest.main()
