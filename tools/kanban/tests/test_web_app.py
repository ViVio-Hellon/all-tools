"""Web 画面(Flask)のテスト。

tkinter 版の ``test_ui_smoke.py`` を置き換えるもの。実際にサーバを立てず、
Flask の ``test_client`` で経路を叩く ── 画面を開かずに済むので速く、
表示先(DISPLAY)も要らない。

**確かめているのは3つ。**

1. ビューモデルがモードごとに正しい操作可否を返すか(:mod:`kanban.presenters.board`)
2. 経路が守るべきものを守るか(トークン・モード・楽観ロック)
3. 状態遷移が VBA と同じ結果になるか(注文中と発送の相互解除など)
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from kanban import config
from kanban.db.store import DEFAULT_COLUMN_MAP, Store
from kanban.domain import models
from kanban.presenters import board as presenter

try:  # Flask が無い環境ではこのモジュールごとスキップする
    import flask  # noqa: F401

    _FLASK_ERROR = ""
except Exception as exc:  # pragma: no cover
    _FLASK_ERROR = str(exc)

HAS_FLASK = not _FLASK_ERROR


def sample_rows() -> list[dict[str, str]]:
    data = [
        ("1", "外装紙", "2200 × 50M : 3本", "", "〇", "〇"),
        ("2", "外装紙", "1900 × 50M : 5本", "〇", "", "〇"),
        ("3", "外装紙", "1600 × 50M : 5本", "", "〇", "〇"),
        ("4", "アングル", "2510 : 3束", "〇", "", "〇"),
        ("5", "アングル", "2010 : 3束", "", "〇", "〇"),
        ("6", "ハードボード", "660 × 1050 : 100枚", "", "〇", "×"),
    ]
    return [
        {
            "mgmt_no": no,
            "material": material,
            "size": size,
            "want": want,
            "unwant": unwant,
            "ordered_at": "2026/07/14 09:05:00" if want else "",
            "shipped": "",
            "confirmed_at": "",
            "permanent": permanent,
        }
        for no, material, size, want, unwant, permanent in data
    ]


class StoreFixture(unittest.TestCase):
    """ラインを 2 本取り込んだ手元の SQLite。"""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="kanban_web_"))
        self.store = Store(str(self.dir / "kanban.sqlite3"), host_name="PC-WEB")
        self.store.ensure_schema()
        for line in ("LVC", "LS"):
            self.store.import_line(
                line=line,
                table_name=f"看板_{line}",
                key_column="管理番号",
                key_category="TEXT",
                column_map=dict(DEFAULT_COLUMN_MAP),
                categories={name: "TEXT" for name in DEFAULT_COLUMN_MAP.values()},
                rows=sample_rows(),
                source_path="dummy.accdb",
            )

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# ビューモデル
# ---------------------------------------------------------------------------
class PolicyTest(unittest.TestCase):
    """モードごとの操作可否(tkinter 版の3つのフォームに相当)。"""

    def test_site_line_with_shipping_disabled(self):
        # LVC は 発送無効 なので、現場ではサイズを押せて発送は押せない
        p = presenter.policy_for(config.MODE_SITE, "LVC")
        self.assertTrue(p.size)
        self.assertFalse(p.ship)
        self.assertFalse(p.hold)  # 注文中は倉庫だけ

    def test_warehouse_can_ship_and_hold(self):
        p = presenter.policy_for(config.MODE_WAREHOUSE, "LVC")
        self.assertFalse(p.size)  # 倉庫では発注しない
        self.assertTrue(p.ship)
        self.assertTrue(p.hold)

    def test_view_is_read_only(self):
        p = presenter.policy_for(config.MODE_WAREHOUSE_VIEW, "LVC")
        self.assertFalse(p.size)
        self.assertFalse(p.ship)
        self.assertFalse(p.hold)
        self.assertTrue(p.read_only)

    def test_a_button_whose_column_is_missing_is_not_offered(self):
        """**共有DBに書けないボタンは出さない。**

        ``保留`` と ``注文中日時`` は任意列(:data:`kanban.config.REQUIRED_COLUMNS`)
        なので、持たない看板テーブルが実際にありえます。出してしまうと、
        押せて・色が変わって・手元にも残るのに**共有DBには一生届きません**
        ── 倉庫は「注文中にした」と思い、現場には何も見えない。しかも
        書き戻しは送り先が無いので失敗し続けます。
        """
        storable = {"want", "unwant", "ordered_at", "shipped", "confirmed_at"}
        p = presenter.policy_for(config.MODE_WAREHOUSE, "LVC", storable)
        self.assertFalse(p.hold, "書けない列のボタンを出している")
        self.assertTrue(p.ship, "書ける列まで巻き添えにしている")

    def test_columns_are_only_narrowed_never_widened(self):
        """列があっても、モードで許していない操作は許さないこと。"""
        everything = set(models.MUTABLE_FIELDS)
        p = presenter.policy_for(config.MODE_WAREHOUSE_VIEW, "LVC", everything)
        self.assertTrue(p.read_only)


class BoardViewTest(StoreFixture):
    def _view(self, mode=config.MODE_WAREHOUSE):
        from kanban.domain.service import KanbanService

        return presenter.build(KanbanService(self.store), "LVC", mode)

    def test_groups_and_counts(self):
        view = self._view()
        self.assertEqual(view.line_label, "LVC")
        # 資材は 3 種類(外装紙・アングル・ハードボード)
        self.assertEqual(len(view.groups), 3)
        self.assertEqual(view.ordered_count, 2)
        self.assertEqual(view.batch_ship_count, 2)

    def test_non_permanent_note(self):
        # ハードボードが 常設品=× なので注意書きが出る
        self.assertIn("非常設品", self._view().note)

    def test_states_map_to_token_names(self):
        """色そのものではなく**状態の名前**を渡す(色の出どころは tokens.css)。"""
        view = self._view()
        row = next(r for g in view.groups for r in g.rows if r.mgmt_no == "2")
        self.assertEqual(row.size.state, presenter.STATE_ORDERED)
        self.assertEqual(row.ship.state, presenter.STATE_NEUTRAL)
        self.assertEqual(row.hold.state, presenter.STATE_NEUTRAL)

    def test_held_row_shows_time_in_tip(self):
        self.store.apply_transition(
            "LVC", "2", models.hold_button_changes, operation="hold"
        )
        row = next(r for g in self._view().groups for r in g.rows if r.mgmt_no == "2")
        self.assertEqual(row.hold.state, presenter.STATE_HELD)
        self.assertIn("注文中", row.hold.tip)

    def test_ordered_and_held_locks_size_in_site_mode(self):
        """発注中 かつ 注文中 の間、現場はサイズを押せない(VBA の無言のガード)。

        **押せないことを見せて、理由も添える** ── 押しても何も起きないと
        壊れているようにしか見えない。
        """
        self.store.apply_transition(
            "LVC", "2", models.hold_button_changes, operation="hold"
        )
        view = self._view(config.MODE_SITE)
        row = next(r for g in view.groups for r in g.rows if r.mgmt_no == "2")
        self.assertTrue(row.locked)
        self.assertFalse(row.size.enabled)
        self.assertIn("注文中", row.locked_note)

    def test_serializes_to_json_safe_dict(self):
        data = presenter.to_dict(self._view())
        self.assertEqual(data["line"], "LVC")
        self.assertIn("groups", data)
        self.assertIn("state", data["groups"][0]["rows"][0]["size"])


# ---------------------------------------------------------------------------
# 経路
# ---------------------------------------------------------------------------
@unittest.skipUnless(HAS_FLASK, f"Flask が入っていないためスキップ ({_FLASK_ERROR})")
class RouteTestBase(StoreFixture):
    MODE = config.MODE_SITE
    LINE = "LVC"

    def setUp(self) -> None:
        super().setUp()
        from app import create_app
        from kanban import admin_lock

        # 鍵はプロセスに 1 つ。前の試験で開けた鍵を持ち越さない
        admin_lock.reset()
        self.addCleanup(admin_lock.reset)
        self.app = create_app(
            self.MODE, token="test-token", port=8741, store=self.store, line=self.LINE
        )
        self.app.config["CONFIG_PATH"] = str(self.dir / "config.json")
        self.client = self.app.test_client()

    # -- 小道具 ---------------------------------------------------------
    def get(self, path, token="test-token"):
        headers = {"X-Tool-Token": token} if token else {}
        return self.client.get(path, headers=headers)

    def post(self, path, body=None, token="test-token"):
        headers = {"X-Tool-Token": token} if token else {}
        return self.client.post(path, json=body or {}, headers=headers)

    #: ``rev`` を引くときの既定ライン。倉庫・倉庫参照モードは担当ラインを
    #: 持たない(``LINE = ""``)ので、明示しておく
    DEFAULT_LINE = "LVC"

    def rev(self, mgmt_no, line=None):
        return self.store.item(line or self.LINE or self.DEFAULT_LINE, mgmt_no).rev

    def batch_items(self, line=None):
        """一括の対象として送る ``[{mgmt_no, rev}]``(画面に出ていた行。いまの版で)。"""
        return [{"mgmt_no": i.mgmt_no, "rev": i.rev}
                for i in self.store.items(line or self.LINE or self.DEFAULT_LINE)]


class HealthTest(RouteTestBase):
    def test_health_is_reachable_without_token(self):
        """トークンを知らない相手も呼べる(起動待機画面・多重起動の判定)。"""
        res = self.client.get("/api/health")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json["app_id"], self.app.config["APP_ID"])
        self.assertEqual(res.json["mode"], config.MODE_SITE)

    def test_health_carries_app_id_for_instance_check(self):
        # 同じポートに別のアプリが居る場合の見分けに使う(基盤仕様書 2.3)
        self.assertIn("app_id", self.client.get("/api/health").json)


class SecurityTest(RouteTestBase):
    def test_api_requires_token(self):
        self.assertEqual(self.get("/api/board", token=None).status_code, 401)

    def test_wrong_token_is_rejected(self):
        self.assertEqual(self.get("/api/board", token="nope").status_code, 401)

    def test_report_requires_token(self):
        """帳票にも業務データが丸ごと入るので、画面より緩い口を作らない。"""
        res = self.client.get("/report/site?line=LVC")
        self.assertEqual(res.status_code, 401)

    def test_cross_origin_is_rejected(self):
        res = self.client.get(
            "/api/board",
            headers={"X-Tool-Token": "test-token", "Sec-Fetch-Site": "cross-site"},
        )
        self.assertEqual(res.status_code, 403)

    def test_bad_host_is_rejected(self):
        """DNS リバインディング対策。Host が一致しなければ弾く。"""
        res = self.client.get(
            "/api/health", headers={"Host": "evil.example.com"}
        )
        self.assertEqual(res.status_code, 400)

    def test_api_is_never_cached(self):
        """看板は「いまの状態」を見る画面なので、古い盤面が出るのは事故。"""
        res = self.get("/api/board")
        self.assertIn("no-store", res.headers["Cache-Control"])


class SiteModeTest(RouteTestBase):
    MODE = config.MODE_SITE

    def test_board_page_renders(self):
        res = self.get("/board")
        self.assertEqual(res.status_code, 200)

    def test_order_toggles(self):
        res = self.post(
            "/api/board/order", {"line": "LVC", "mgmt_no": "1", "rev": self.rev("1")}
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(self.store.item("LVC", "1").is_ordered)
        # 応答は**更新後の盤面ぜんぶ**(差分ではない)
        self.assertIn("board", res.json)

    def test_stale_rev_is_conflict(self):
        stale = self.rev("1")
        self.post("/api/board/order", {"line": "LVC", "mgmt_no": "1", "rev": stale})
        res = self.post(
            "/api/board/order", {"line": "LVC", "mgmt_no": "1", "rev": stale}
        )
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json["error"]["code"], "conflict")
        # 断られたときも盤面を返す(押す前の状態に戻さない)
        self.assertIn("board", res.json)

    def test_hold_is_rejected_in_site_mode(self):
        res = self.post(
            "/api/board/hold", {"line": "LVC", "mgmt_no": "1", "rev": self.rev("1")}
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json["error"]["code"], "wrong_mode")

    def test_ship_is_rejected_on_shipping_disabled_line(self):
        """LVC は 発送無効。画面がボタンを出さないだけでなく、経路でも断る。"""
        res = self.post(
            "/api/board/ship", {"line": "LVC", "mgmt_no": "2", "rev": self.rev("2")}
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json["error"]["code"], "not_allowed")

    def test_other_line_is_not_visible(self):
        """現場は担当ラインしか見えない。URL を直接叩かれても触らせない。"""
        res = self.post(
            "/api/board/order", {"line": "LS", "mgmt_no": "1", "rev": 1}
        )
        self.assertEqual(res.status_code, 400)

    def test_batch_reset(self):
        # 赤と緑が両方点いている状態を作る
        self.store.apply_transition("LVC", "1", models.order_button_changes, operation="order")
        self.store.apply_transition("LVC", "1", models.ship_button_changes, operation="ship")
        res = self.post("/api/board/batch-reset", {"line": "LVC", "items": self.batch_items()})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json["done"], 1)
        self.assertFalse(self.store.item("LVC", "1").is_ordered)


class WarehouseModeTest(RouteTestBase):
    MODE = config.MODE_WAREHOUSE
    LINE = ""

    def test_all_warehouse_lines_are_visible(self):
        res = self.get("/api/board?line=LS")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json["board"]["line"], "LS")

    def test_ship_requires_an_order(self):
        """発注が無いのに発送しようとすると、業務として断る(422)。"""
        res = self.post(
            "/api/board/ship", {"line": "LVC", "mgmt_no": "1", "rev": self.rev("1")}
        )
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.json["error"]["code"], "order_missing")

    def test_hold_clears_shipped(self):
        """発送済みの行で注文中を押すと、注文中=黄 / 発送=解除。"""
        self.post("/api/board/ship", {"line": "LVC", "mgmt_no": "2", "rev": self.rev("2")})
        self.assertTrue(self.store.item("LVC", "2").is_shipped)

        res = self.post(
            "/api/board/hold", {"line": "LVC", "mgmt_no": "2", "rev": self.rev("2")}
        )
        self.assertEqual(res.status_code, 200)
        item = self.store.item("LVC", "2")
        self.assertTrue(item.is_held)
        self.assertFalse(item.is_shipped)
        self.assertTrue(item.hold_at)

    def test_ship_clears_hold(self):
        """注文中の行で発送を押すと、発送=緑 / 注文中=解除。

        以前はエラーで発送を拒んでいたが、現在は自動解除する
        (VBA ``cls_ShipButton`` の ``needClearHold``)。
        """
        self.post("/api/board/hold", {"line": "LVC", "mgmt_no": "2", "rev": self.rev("2")})
        self.assertTrue(self.store.item("LVC", "2").is_held)

        res = self.post(
            "/api/board/ship", {"line": "LVC", "mgmt_no": "2", "rev": self.rev("2")}
        )
        self.assertEqual(res.status_code, 200)
        item = self.store.item("LVC", "2")
        self.assertTrue(item.is_shipped)
        self.assertFalse(item.is_held)
        self.assertEqual(item.hold_at, "")

    def test_hold_needs_no_dialog_round_trip(self):
        """理由入力も確認も無く、1 回の要求で切り替わる。"""
        self.post("/api/board/hold", {"line": "LVC", "mgmt_no": "2", "rev": self.rev("2")})
        self.assertTrue(self.store.item("LVC", "2").is_held)
        self.post("/api/board/hold", {"line": "LVC", "mgmt_no": "2", "rev": self.rev("2")})
        self.assertFalse(self.store.item("LVC", "2").is_held)

    def test_order_is_rejected_in_warehouse_mode(self):
        res = self.post(
            "/api/board/order", {"line": "LVC", "mgmt_no": "1", "rev": self.rev("1")}
        )
        self.assertEqual(res.status_code, 403)

    def test_batch_ship(self):
        res = self.post("/api/board/batch-ship", {"line": "LVC", "items": self.batch_items()})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json["done"], 2)


class ViewModeTest(RouteTestBase):
    MODE = config.MODE_WAREHOUSE_VIEW
    LINE = ""

    def test_board_is_read_only(self):
        res = self.get("/api/board?line=LVC")
        self.assertTrue(res.json["board"]["read_only"])

    def test_every_button_is_disabled(self):
        rows = [r for g in self.get("/api/board?line=LVC").json["board"]["groups"]
                for r in g["rows"]]
        for row in rows:
            for kind in ("size", "ship", "hold"):
                self.assertFalse(row[kind]["enabled"], f"{kind} が押せてしまう")

    def test_writes_are_rejected(self):
        """読むだけのモードでは、どのボタンも通らない。"""
        for path in ("ship", "hold", "order"):
            res = self.post(
                f"/api/board/{path}", {"line": "LVC", "mgmt_no": "2", "rev": self.rev("2")}
            )
            self.assertIn(res.status_code, (403, 404), f"{path} が通ってしまう")

    def test_no_reports_in_view_mode(self):
        """倉庫参照モードには印刷ボタンが無かった(tkinter 版と同じ)。"""
        self.assertEqual(self.get("/api/report/available?line=LVC").json["reports"], [])


class ReportTest(RouteTestBase):
    MODE = config.MODE_WAREHOUSE
    LINE = ""

    def test_available_reports_carry_counts(self):
        """押す前に何件出るかを見せる(0 件の帳票で白紙を出さない)。"""
        data = self.get("/api/report/available?line=LVC").json
        kinds = {r["kind"]: r["count"] for r in data["reports"]}
        self.assertEqual(set(kinds), {"warehouse", "non_permanent"})

    def test_report_renders_html(self):
        self.store.apply_transition("LVC", "2", models.ship_button_changes, operation="ship")
        res = self.get("/report/warehouse?line=LVC")
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/html", res.headers["Content-Type"])

    def test_empty_report_says_so(self):
        res = self.get("/report/warehouse?line=LVC")
        self.assertEqual(res.status_code, 200)
        self.assertIn("印刷する明細はありません", res.get_data(as_text=True))

    def test_report_of_other_mode_is_404(self):
        """現場の注文票は倉庫モードでは出せない。"""
        self.assertEqual(self.get("/report/site?line=LVC").status_code, 404)


class PrintButtonTest(RouteTestBase):
    """**どの帳票(印刷プレビュー)にも「🖨 印刷する」ボタンがある**(押すと印刷ダイアログ)。

    帳票を足したときに道具立てを付け忘れないよう、出せる帳票を全部開いて確かめる。
    """

    MODE = config.MODE_SITE

    def assert_print_button(self, kind):
        html = self.get(f"/report/{kind}?line=LVC").get_data(as_text=True)
        self.assertIn('id="go"', html, kind)
        self.assertIn("🖨 印刷する", html, kind)
        self.assertIn("window.print()", html, kind)
        self.assertIn(".tools, th.pick, td.pick { display: none !important; }", html,
                      f"{kind}: ボタンが紙に出る")

    def test_every_report_of_this_mode(self):
        from app.routes.report import MODE_KINDS

        # 明細のある状態にする(0 件だと「明細はありません」の画面になる)
        self.store.apply_transition("LVC", "6", models.order_button_changes, operation="order")
        if self.MODE == config.MODE_WAREHOUSE:
            self.store.apply_transition("LVC", "2", models.ship_button_changes, operation="ship")
        kinds = MODE_KINDS[self.MODE]
        self.assertTrue(kinds)
        for kind in kinds:
            with self.subTest(kind=kind):
                self.assert_print_button(kind)


class WarehousePrintButtonTest(PrintButtonTest):
    MODE = config.MODE_WAREHOUSE
    LINE = ""


class SettingsTest(RouteTestBase):
    MODE = config.MODE_SITE

    def test_settings_page_renders(self):
        self.assertEqual(self.get("/settings").status_code, 200)

    def test_state_reports_current_mode(self):
        data = self.get("/api/settings").json
        self.assertEqual(data["mode"], config.MODE_SITE)
        self.assertEqual(len(data["modes"]), len(config.ALL_MODES))

    def _grant(self, *codes: str) -> None:
        """この端末(ログインID・PC名)に権限を与えた梱包資材マスタを作り、設定で指す。"""
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
        patcher = mock.patch.dict(os.environ, {"KANBAN_LOCAL_DIR": str(self.dir / "local")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_mode_change_needs_permission_not_password(self):
        """**関門はアクセス権限。** 梱包資材マスタに行が無ければ倉庫にはできない。

        パスワードを入れても通らない(パスワードは教え合える)。断るときは、
        何を足せば使えるのか(ログインID・PC名・mode:material)まで言う。
        """
        res = self.post("/api/mode", {"mode": config.MODE_WAREHOUSE,
                                      "password": "秘密", "password_confirm": "秘密"})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json["error"]["code"], "no_permission")
        self.assertIn("mode:material", res.json["error"]["message"])
        self.assertIn("アクセス権限", res.json["error"]["message"])
        self.assertIn("access", res.json, "画面が権限を描き直せない")
        self.assertNotEqual(self.store.get_device_mode(), config.MODE_WAREHOUSE, "断ったのに変わっている")

    def test_mode_change_with_permission_needs_no_password(self):
        """権限があれば**パスワードを訊かない**。倉庫参照は権限を問わない。"""
        self._grant("mode:material", "mode:field")
        res = self.post("/api/mode", {"mode": config.MODE_WAREHOUSE})
        self.assertEqual(res.status_code, 200, res.json)
        self.assertEqual(self.store.get_device_mode(), config.MODE_WAREHOUSE)
        res = self.post("/api/mode", {"mode": config.MODE_WAREHOUSE_VIEW})
        self.assertEqual(res.status_code, 200)
        res = self.post("/api/mode", {"mode": config.MODE_SITE})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.store.get_device_mode(), config.MODE_SITE)

    def test_unregistered_terminal_can_use_site_and_view(self):
        """行の無い端末は**現場モードだけ**(締め出さない。強い権限も渡さない)。"""
        res = self.post("/api/mode", {"mode": config.MODE_WAREHOUSE_VIEW})
        self.assertEqual(res.status_code, 200)
        res = self.post("/api/mode", {"mode": config.MODE_SITE})
        self.assertEqual(res.status_code, 200)

    def test_mode_change_does_not_relabel_the_running_process(self):
        """**走ったままモードを名乗り変えない。**

        モードはポート(現場 8741 / 倉庫 8751 / 倉庫参照 8761)とロックに
        結びついている。名乗りだけ変えると、``site.lock`` を掴んだまま
        「倉庫です」と答えるプロセスになり、次の起動が別人と誤認して
        2 つめ・3 つめを立ち上げる ── 実機でそうなった。
        """
        self._grant("mode:material")
        res = self.post("/api/mode", {"mode": config.MODE_WAREHOUSE})
        self.assertEqual(res.status_code, 200)
        # 次に開いたときのモードは変わる
        self.assertEqual(self.store.get_device_mode(), config.MODE_WAREHOUSE)
        # いま動いているプロセスは現場のまま
        self.assertEqual(self.app.config["MODE"], config.MODE_SITE)
        self.assertEqual(self.get("/api/health").json["mode"], config.MODE_SITE)
        self.assertEqual(res.json["running_mode"], config.MODE_SITE)
        self.assertIn("次にアプリを開いたとき", res.json["message"])

    def test_password_setup_requires_matching_confirmation(self):
        res = self.post(
            "/api/line",
            {"line": "LS", "password": "あ", "password_confirm": "い"},
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json["error"]["code"], "password_mismatch")

    def test_first_password_is_registered_then_required(self):
        """未設定の端末では最初の1回だけ登録する(tkinter 版と同じ流れ)。"""
        res = self.post("/api/line", {"line": "LS", "password": "秘密", "password_confirm": "秘密"})
        self.assertEqual(res.status_code, 200)
        from kanban import admin_lock

        admin_lock.reset()   # 認証済みを解いて、2 回目は訊かれる状態にする
        res = self.post("/api/line", {"line": "LVC", "password": "ちがう"})
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json["error"]["code"], "bad_password")
        res = self.post("/api/line", {"line": "LVC", "password": "秘密"})
        self.assertEqual(res.status_code, 200)

    def test_line_change_needs_the_password(self):
        """担当ラインを変えると取り込む/書き戻すラインが入れ替わる。

        現場の端末が別ラインを指したまま操作されると、そのラインの発注が
        本来の担当者の知らないところで動く。
        """
        res = self.post("/api/line", {"line": "LS"})
        self.assertEqual(res.status_code, 401)
        self.assertEqual(self.app.config["LINE"], "LVC", "断ったのに変わっている")

        res = self.post(
            "/api/line", {"line": "LS", "password": "秘密", "password_confirm": "秘密"}
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.app.config["LINE"], "LS")

    def test_changing_the_line_imports_it_right_away(self):
        """**変えた先のラインをその場で取り込む。**

        以前は起動したときのラインを取り込み続けていたので、変えた先のラインは
        一度も取り込まれず、看板画面は空のまま、マスタで足した看板も出なかった。
        """
        called: list[bool] = []
        self.app.config["MANUAL_REFRESH"] = lambda: called.append(True)
        res = self.post("/api/line", {"line": "LS", "password": "秘密", "password_confirm": "秘密"})
        self.assertEqual(res.status_code, 200, res.json)
        self.assertTrue(called, "ラインを変えたのに取り込んでいない")
        self.assertTrue(res.json["imported"])

    def test_unknown_line_is_rejected_before_the_password(self):
        """入力の形の誤りは、パスワードを訊く前に断る。"""
        self.assertEqual(self.post("/api/line", {"line": "架空"}).status_code, 400)


class DistributionRouteTest(RouteTestBase):
    """配布設定(経路側。python-web-tools と同じつくり)。

    書き出し・読み込み直し・消す・配布用フォルダを作る、はどれも管理者
    パスワードが要る(鍵が開いていれば訊かない)。ふだんと同じ置き方
    (アプリのフォルダの data\\config.json + 担当ラインは %APPDATA%)で試す。
    """

    ENV = ("KANBAN_SETTINGS_DIR", "KANBAN_CONFIG", "KANBAN_DISTRIBUTION_DIR")

    def setUp(self) -> None:
        import os

        super().setUp()
        self._env = {k: os.environ.get(k) for k in self.ENV}
        os.environ["KANBAN_SETTINGS_DIR"] = str(self.dir / "app" / "data")
        os.environ["KANBAN_CONFIG"] = str(self.dir / "appdata" / "config.json")
        self.folder = self.dir / "app" / "配布設定"
        os.environ["KANBAN_DISTRIBUTION_DIR"] = str(self.folder)
        self.app.config["CONFIG_PATH"] = ""
        # 配る元の端末の設定
        cfg = config.load_config()
        cfg.import_interval_sec = 45
        cfg.admin_password_hash = config.hash_password("秘密")
        config.save_config(cfg)

    def tearDown(self) -> None:
        import os

        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        super().tearDown()

    def export(self, **creds):
        return self.post("/api/distribution/export",
                         {"items": ["import_interval_sec", "admin_password_hash"],
                          **(creds or {"password": "秘密"})})

    def test_export_needs_the_password(self):
        res = self.post("/api/distribution/export", {"items": ["import_interval_sec"]})
        self.assertEqual(res.status_code, 401)
        self.assertFalse(self.folder.exists(), "パスワード無しで書いている")

    def test_export_writes_and_checks_right_away(self):
        res = self.export()
        self.assertEqual(res.status_code, 200, res.json)
        self.assertTrue(all(c["ok"] for c in res.json["checks"]), res.json["checks"])
        self.assertTrue(res.json["distribution"]["exists"])
        labels = {c["label"] for c in res.json["distribution"]["contents"]}
        self.assertEqual(labels, {"取り込み間隔", "管理者パスワード"})

    def test_the_screen_shows_what_is_there(self):
        self.export()
        dist = self.get("/api/settings").json["distribution"]
        self.assertTrue(dist["exists"])
        items = {i["key"]: i for i in dist["items"]}
        self.assertEqual(items["import_interval_sec"]["here"], "45 秒")
        self.assertFalse(items["line"]["default"], "担当ラインが既定で入る")

    def test_the_hash_is_never_sent_to_the_screen(self):
        self.export()
        from kanban import distribution

        stored = distribution.read().settings["admin_password_hash"]
        self.assertNotIn(stored, self.get("/api/settings").get_data(as_text=True))

    def test_reapply_and_remove(self):
        self.export()
        cfg = config.load_config()
        cfg.import_interval_sec = 10
        config.save_config(cfg)
        res = self.post("/api/distribution/reapply", {"password": "秘密"})
        self.assertEqual(res.status_code, 200, res.json)
        self.assertEqual(config.load_config().import_interval_sec, 45)
        self.assertEqual(self.post("/api/distribution/remove", {"password": "秘密"}).status_code, 200)
        self.assertFalse(self.folder.exists())

    def test_build_makes_the_folder_to_hand_out(self):
        """「配布用フォルダを作る」は scripts/make_dist.py と同じものを作る。"""
        from unittest import mock
        from app.routes import settings as routes

        made = []

        class Stub:
            @staticmethod
            def default_out(stamp=False):
                return self.dir / "配布用"

            @staticmethod
            def build(out, with_settings=True):
                made.append((out, with_settings))
                return out, [f"配布用フォルダを作りました: {out}"]

        with mock.patch.object(routes, "_make_dist_module", lambda: Stub):
            self.assertEqual(self.post("/api/distribution/build", {}).status_code, 401)
            res = self.post("/api/distribution/build", {"password": "秘密"})
        self.assertEqual(res.status_code, 200, res.json)
        self.assertEqual(made, [(self.dir / "配布用", True)])
        self.assertEqual(res.json["path"], str(self.dir / "配布用"))

    def test_the_old_routes_are_gone(self):
        self.assertEqual(self.post("/api/deploy", {"password": "秘密"}).status_code, 404)
        self.assertEqual(self.post("/api/settings-folder/open", {}).status_code, 404)


class BehaviorRouteTest(RouteTestBase):
    """取り込み・書き戻し間隔と自動印刷(以前は配布設定の中にしか無かった)。"""

    PW = {"password": "秘密", "password_confirm": "秘密"}

    def save(self, values, **creds):
        return self.post("/api/behavior", {"values": values, **(creds or self.PW)})

    def test_saving_needs_the_password(self):
        self.save({"import_interval_sec": "45", "export_interval_sec": "20"})   # 登録
        from kanban import admin_lock

        admin_lock.get().lock()
        res = self.post("/api/behavior", {"values": {"import_interval_sec": "5", "export_interval_sec": "5"}})
        self.assertEqual(res.status_code, 401)
        self.assertEqual(config.load_config(self.dir / "config.json").import_interval_sec, 45)

    def test_a_bad_number_points_at_its_field(self):
        res = self.save({"import_interval_sec": "すぐ", "export_interval_sec": "20"})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json["error"]["field"], "import_interval_sec")

    def test_values_equal_to_the_builtin_default_are_not_written(self):
        """ファイルを開けば「何を変えてあるか」がそのまま読めること。"""
        import json

        d = config.Config()
        self.save({"import_interval_sec": str(d.import_interval_sec),
                   "export_interval_sec": "20"})
        saved = json.loads((self.dir / "config.json").read_text(encoding="utf-8"))
        self.assertNotIn("import_interval_sec", saved)
        self.assertEqual(saved["export_interval_sec"], 20)


class UnlockRouteTest(RouteTestBase):
    """**鍵を開けるためだけの入口**(設定画面の「パスワード認証」)。

    以前は守られた操作を押すたびに訊いていて、パスワードだけを入れる場所が
    無かった。1 度開けたら、鍵が閉まるまで訊かない(python-web-tools の
    「マスタ編集の認証」と同じ)。
    """

    def setUp(self) -> None:
        super().setUp()
        self.post("/api/admin/password", {"new_password": "秘密", "new_password_confirm": "秘密"})
        from kanban import admin_lock

        admin_lock.get().lock()   # 設定したときに開いた鍵を閉めてから始める

    def state(self):
        return self.get("/api/admin/state").json

    def test_protected_operations_ask_while_locked(self):
        res = self.post("/api/line", {"line": "LS"})
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json["error"]["code"], "locked")

    def test_unlocking_lets_protected_operations_through(self):
        res = self.post("/api/admin/unlock", {"password": "秘密"})
        self.assertEqual(res.status_code, 200, res.json)
        self.assertTrue(self.state()["admin_unlocked"])
        self.assertEqual(self.post("/api/line", {"line": "LS"}).status_code, 200)

    def test_a_wrong_password_does_not_unlock(self):
        self.assertEqual(self.post("/api/admin/unlock", {"password": "ちがう"}).status_code, 401)
        self.assertFalse(self.state()["admin_unlocked"])

    def test_locking_asks_again(self):
        self.post("/api/admin/unlock", {"password": "秘密"})
        self.assertEqual(self.post("/api/admin/lock", {}).status_code, 200)
        self.assertEqual(self.post("/api/line", {"line": "LS"}).status_code, 401)

    def test_a_password_in_the_prompt_also_unlocks(self):
        """確認画面でパスワードを入れて通ったら、続けて打たせない。"""
        self.assertEqual(self.post("/api/line", {"line": "LS", "password": "秘密"}).status_code, 200)
        self.assertEqual(self.post("/api/line", {"line": "LVC"}).status_code, 200)

    def test_changing_the_password_still_needs_the_current_one(self):
        """鍵が開いていても、パスワードを変えるときは今のパスワードを訊く。
        開けっぱなしの端末で、通りがかった人に変えられないように。"""
        self.post("/api/admin/unlock", {"password": "秘密"})
        res = self.post("/api/admin/password", {"new_password": "x", "new_password_confirm": "x"})
        self.assertEqual(res.status_code, 401)

    def test_unlocking_is_refused_before_a_password_is_set(self):
        import os

        os.remove(self.dir / "config.json")
        res = self.post("/api/admin/unlock", {"password": "なんでも"})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json["error"]["code"], "need_password_setup")

    def test_the_screen_has_the_unlock_form(self):
        html = self.get("/settings").get_data(as_text=True)
        self.assertIn('id="unlock-form"', html)
        self.assertIn('id="lockbar"', html)


class AdminPasswordTest(RouteTestBase):
    """**パスワードだけを入れる場所。**

    以前は操作のときに出る確認画面しか無く、1 度決めたパスワードを変える
    手段が無かった。未設定の端末では「最初に保護された操作をした人」が
    決めるしかなかった。
    """

    def change(self, new, confirm=None, current=None):
        body = {"new_password": new, "new_password_confirm": new if confirm is None else confirm}
        if current is not None:
            body["current_password"] = current
        return self.post("/api/admin/password", body)

    def hash(self):
        return config.load_config(self.dir / "config.json").admin_password_hash

    def test_it_can_be_set_when_there_is_none(self):
        self.assertFalse(self.get("/api/settings").json["has_admin_password"])
        res = self.change("はじめて")
        self.assertEqual(res.status_code, 200, res.json)
        self.assertTrue(config.verify_password("はじめて", self.hash()))
        self.assertTrue(self.get("/api/settings").json["has_admin_password"])

    def test_changing_needs_the_current_password(self):
        self.change("もと")
        res = self.change("あたらしい", current="ちがう")
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json["error"]["field"], "current_password")
        self.assertTrue(config.verify_password("もと", self.hash()), "違うのに変わった")

        res = self.change("あたらしい", current="もと")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(config.verify_password("あたらしい", self.hash()))
        self.assertFalse(config.verify_password("もと", self.hash()))

    def test_changing_without_a_current_password_is_refused(self):
        self.change("もと")
        self.assertEqual(self.change("あたらしい").status_code, 401)

    def test_empty_or_mismatched_is_refused(self):
        self.assertEqual(self.change("").status_code, 400)
        res = self.change("あ", confirm="い")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json["error"]["field"], "new_password_confirm")
        self.assertFalse(self.hash(), "断ったのに設定された")

    def test_the_new_password_opens_protected_operations(self):
        """変えたあとは、確認画面でも新しいパスワードが通ること。"""
        self.change("もと")
        self.change("あたらしい", current="もと")
        self.assertEqual(self.post("/api/admin/verify", {"password": "あたらしい"}).status_code, 200)
        self.assertEqual(self.post("/api/admin/verify", {"password": "もと"}).status_code, 401)

    def test_without_a_password_the_screen_offers_to_set_it_right_there(self):
        """**未設定なら、その場で決められること。** 以前は「下で設定すると、鍵を
        開けられるようになります」と別の欄へ回していて、何をすればよいのか
        分からなかった。"""
        html = self.get("/settings").get_data(as_text=True)
        auth = html[html.index('id="panel-auth"'):html.index('id="panel-dist"')]
        self.assertIn('id="setup-form"', auth)
        self.assertNotIn('id="setup-form" autocomplete="off" hidden', auth)
        self.assertIn('id="unlock-form" autocomplete="off" hidden', auth)
        self.assertIn("未設定", auth)

    def test_the_screen_has_its_own_fields(self):
        html = self.get("/settings").get_data(as_text=True)
        self.assertIn('id="password-form"', html)
        self.assertIn('id="admin-pw-new"', html)
        # 「この端末」ではなく「パスワード認証」の面にある
        auth = html[html.index('id="panel-auth"'):html.index('id="panel-dist"')]
        self.assertIn('id="password-form"', auth)


class SettingsAdminRouteTest(RouteTestBase):
    """設定画面の「参照...」とマスタ管理(経路側)。"""

    MODE = config.MODE_SITE

    def test_fs_list_needs_a_token(self):
        self.assertEqual(
            self.post("/api/fs/list", {"path": "/"}, token=None).status_code, 401
        )

    def test_fs_list_needs_the_password(self):
        """この PC のフォルダ構成を返す口。**マスタの読み取り以外は守る。**"""
        res = self.post("/api/fs/list", {"path": ""})
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json["error"]["code"], "need_password_setup")

    def test_fs_list_returns_roots_with_the_password(self):
        data = self.post(
            "/api/fs/list", {"path": "", "password": "秘密", "password_confirm": "秘密"}
        ).json
        self.assertTrue(data["roots"], "出発点が無いとどこから探せばよいか分からない")

    def test_master_read_needs_no_password(self):
        """**開けておくのはマスタの読み取りだけ。**

        「なぜデータが古いのか」「マスタに何が入っているのか」を確かめるのに、
        いちいち管理者を呼ばずに済むようにする。
        """
        self.assertEqual(self.get("/api/master").status_code, 200)
        self.assertEqual(self.get("/api/master/table?table=x").status_code, 200)

    def test_master_is_blocked_without_a_path(self):
        """**パスが無ければ何もできない。** 空表を出すと「消えた」に見える。"""
        data = self.get("/api/master").json
        self.assertFalse(data["available"])
        self.assertEqual(data["blocked"], "no_path")
        self.assertIn("設定されていません", data["why"])

    def test_master_update_needs_a_path(self):
        res = self.post(
            "/api/master/update",
            {"table": "看板_LVC", "key": 1, "column": "資材", "value": "x",
             "password": "p", "password_confirm": "p"},
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json["error"]["code"], "no_path")

    def test_master_update_needs_the_admin_password(self):
        """**書く前に通す関門。** ここより下で書くと、断ったのに一部だけ変わる。"""
        res = self.post(
            "/api/master/update",
            {"table": "看板_LVC", "key": 1, "column": "資材", "value": "x"},
        )
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json["error"]["code"], "need_password_setup")

    def test_master_update_rejects_missing_target(self):
        res = self.post("/api/master/update", {"table": "", "column": ""})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json["error"]["code"], "bad_request")

    def test_master_insert_needs_the_admin_password(self):
        res = self.post(
            "/api/master/insert", {"table": "看板_LVC", "values": {"管理番号": "9"}}
        )
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json["error"]["code"], "need_password_setup")

    def test_master_delete_needs_the_admin_password(self):
        res = self.post("/api/master/delete", {"table": "看板_LVC", "row_key": 1})
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json["error"]["code"], "need_password_setup")

    def test_master_insert_rejects_missing_target(self):
        self.assertEqual(
            self.post("/api/master/insert", {"table": "看板_LVC"}).status_code, 400
        )
        self.assertEqual(
            self.post("/api/master/delete", {"table": "看板_LVC"}).status_code, 400
        )


class PresenceRouteTest(RouteTestBase):
    """帯に出す「いま開いている端末」(経路側)。"""

    MODE = config.MODE_SITE
    LINE = "LVC"

    def saw(self, line, status=config.STATE_OPEN, host="PC-B"):
        self.store.upsert_line_status_rows(
            [{"line": line, "status": status, "sort_order": 1,
              "stamp": "2026/08/31 09:00:00", "host": host}]
        )

    def test_status_lists_other_open_terminals(self):
        self.saw(config.WAREHOUSE_LINE_NAME)
        rows = self.get("/api/status").json["presence"]
        self.assertEqual([r["label"] for r in rows], [config.WAREHOUSE_LINE_NAME])
        self.assertTrue(rows[0]["live"])
        self.assertEqual(rows[0]["host"], "PC-B")

    def test_status_leaves_me_out(self):
        self.saw("LVC")
        self.assertEqual(self.get("/api/status").json["presence"], [])

    def test_board_payload_carries_it_too(self):
        """開いた直後の数秒を「誰も居ない」にしないため、盤面にも載せる。"""
        self.saw(config.WAREHOUSE_LINE_NAME)
        self.assertTrue(self.get("/api/board?line=LVC").json["presence"])

    def test_status_stays_local(self):
        """**数秒おきの経路に共有フォルダを混ぜない。**

        ここが共有へ行くと、端末が増えるほど共有が重くなる。
        """
        self.assertEqual(self.get("/api/status").status_code, 200)
        self.assertIn("presence", self.get("/api/status").json)

    def test_status_says_since_when_it_has_not_reached_the_shared_db(self):
        """共有へ届いていないあいだは、いつから・なぜを返す(画面の「まだ共有に届いていません」)。"""
        self.store.set_meta("undelivered_since", "2026/10/07 09:00:00")
        self.store.set_meta("undelivered_why", "共有DBが見えません")
        body = self.get("/api/status").json
        self.assertEqual(body["undelivered_since"], "", "残りが無いのに届いていないと出した")
        self.store.apply_transition("LVC", "1", models.order_button_changes, operation="order")
        body = self.get("/api/status").json
        self.assertEqual(body["undelivered_since"], "2026/10/07 09:00:00")
        self.assertEqual(body["undelivered_why"], "共有DBが見えません")


class ImportFreshnessTest(RouteTestBase):
    """**自動更新は、動かなくなったことが見えて初めて信用できる。**

    共有フォルダが落ちていると、画面は元気に動いているのに中身だけ古いまま
    になる ── VBA 版の「更新できていないのに気付かない」と同じ形。
    取り込めていない時間が延びたら帯でそう言う。
    """

    MODE = config.MODE_SITE
    LINE = "LVC"

    def setUp(self) -> None:
        super().setUp()
        self.app.config["IMPORT_INTERVAL_SEC"] = 30

    def imported(self, at) -> None:
        self.store.set_meta("last_import_at", at.strftime("%Y/%m/%d %H:%M:%S"))

    def status(self) -> dict:
        return self.get("/api/status").json

    def test_fresh_import_is_not_flagged(self):
        from datetime import datetime

        self.imported(datetime.now())
        s = self.status()
        self.assertFalse(s["import_stale"])
        self.assertLess(s["import_age_sec"], 5)

    def test_a_short_hiccup_is_not_flagged(self):
        """1 回や 2 回の失敗で騒がない。共有フォルダは瞬間的に落ちる。"""
        from datetime import datetime, timedelta

        self.imported(datetime.now() - timedelta(seconds=70))
        self.assertFalse(self.status()["import_stale"])

    def test_a_long_silence_is_flagged(self):
        from datetime import datetime, timedelta

        self.imported(datetime.now() - timedelta(minutes=20))
        s = self.status()
        self.assertTrue(s["import_stale"])
        self.assertGreater(s["import_age_sec"], 1000)

    def test_never_imported_is_flagged(self):
        """起動時の取り込みは画面を出す前に終わる。空なら本当に失敗している。"""
        self.assertTrue(self.status()["import_stale"])

    def test_terminals_without_periodic_import_are_not_flagged(self):
        """定期取り込みをしない端末で警告しても、直しようがない。"""
        self.app.config["IMPORT_INTERVAL_SEC"] = 0
        s = self.status()
        self.assertFalse(s["import_stale"])
        self.assertIsNone(s["import_age_sec"])

    def test_the_threshold_follows_the_interval(self):
        """取り込み間隔を延ばした端末で、すぐ警告が出ないこと。"""
        from datetime import datetime, timedelta

        self.app.config["IMPORT_INTERVAL_SEC"] = 300
        self.imported(datetime.now() - timedelta(minutes=15))
        self.assertFalse(self.status()["import_stale"], "300秒×4 はまだ我慢する")
        self.imported(datetime.now() - timedelta(minutes=25))
        self.assertTrue(self.status()["import_stale"])


class ViewModePresenceTest(RouteTestBase):
    """倉庫参照は名乗らないが、**見えるものは全部見える**。"""

    MODE = config.MODE_WAREHOUSE_VIEW
    LINE = "LVC"

    def test_view_mode_hides_nobody(self):
        """名乗っていないのに、設定のラインだけ見えないのはおかしい。"""
        self.store.upsert_line_status_rows(
            [{"line": "LVC", "status": config.STATE_OPEN, "sort_order": 1,
              "stamp": "2026/08/31 09:00:00", "host": "PC-B"}]
        )
        rows = self.get("/api/status").json["presence"]
        self.assertEqual([r["line"] for r in rows], ["LVC"])


class MasterWriteRouteTest(RouteTestBase):
    """マスタを**足す・消す**(経路側。共有DBを本当に置いて確かめる)。

    看板を 1 枚増やせないと、結局 Access を開くことになる ── その Access
    をやめたので、ここが通らないと運用が回らない。
    """

    MODE = config.MODE_SITE
    TABLE = "看板_LVC"

    def setUp(self) -> None:
        super().setUp()
        import json
        import sqlite3

        self.shared = self.dir / "看板マスタ.sqlite3"
        conn = sqlite3.connect(str(self.shared))
        conn.execute(
            f"CREATE TABLE [{self.TABLE}] "
            "([管理番号] NUMERIC, [資材] TEXT, [サイズ] TEXT, [不] TEXT)"
        )
        conn.execute(
            f"INSERT INTO [{self.TABLE}] ([管理番号], [資材]) VALUES (1, '外装紙')"
        )
        conn.commit()
        conn.close()

        (self.dir / "config.json").write_text(
            json.dumps(
                {
                    "shared_db_path": str(self.shared),
                    "admin_password_hash": config.hash_password("秘密"),
                    "line": "LVC",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def keys(self) -> list:
        import sqlite3

        conn = sqlite3.connect(str(self.shared))
        try:
            return [
                r[0]
                for r in conn.execute(f"SELECT [管理番号] FROM [{self.TABLE}] ORDER BY 1")
            ]
        finally:
            conn.close()

    def row_key(self, mgmt_no):
        """その管理番号の行を指す ``__行``。画面が表を開いたときに受け取る値。"""
        data = self.get(f"/api/master/table?table={self.TABLE}").json
        for row in data["rows"]:
            if str(row.get(config.COL_KEY, "")).strip() == str(mgmt_no):
                return row[data["row_key"]]
        raise AssertionError(f"管理番号 {mgmt_no} の行がない")

    def add(self, values, password="秘密"):
        return self.post(
            "/api/master/insert",
            {"table": self.TABLE, "values": values, "password": password},
        )

    def test_adds_a_kanban_row(self):
        res = self.add({"管理番号": "2", "資材": "アングル", "サイズ": "2510"})
        self.assertEqual(res.status_code, 200, res.json)
        self.assertEqual(self.keys(), [1, 2])

    def test_an_unwritable_share_is_reported_not_a_bare_500(self):
        """**共有DBを開けないときに「通信に失敗しました (500)」で終わらせない。**

        開くところで失敗すると ``SharedDbError`` が上がる。素通しにすると
        画面には 500 としか出ず、共有フォルダに届いていないのか、アプリが
        壊れたのかが読めない(マスタに行を足せない不具合がまさにこれだった)。
        """
        from unittest import mock

        from kanban.db.shared import SharedDbError

        # 読む(重複の確認)は通し、書くところだけ開けなくする
        with mock.patch(
            "kanban.db.shared.SharedDb.execute",
            side_effect=SharedDbError("共有DBを開けませんでした (\\\\server\\share): テスト"),
        ):
            res = self.add({"管理番号": "2", "資材": "アングル", "サイズ": "サイズ2"})
        self.assertEqual(res.status_code, 503, res.get_data(as_text=True))
        self.assertEqual(res.json["error"]["code"], "unreachable")
        self.assertIn("共有フォルダに届いているか", res.json["error"]["message"])
        self.assertEqual(self.keys(), [1])

    def test_any_unexpected_error_says_why(self):
        """想定外の例外でも、**理由を返し、記録に残す。**

        画面の「通信に失敗しました (500)」は、応答に理由が入っていないときの
        文言。理由を返せば画面にそのまま出る。経緯は DebugLog に残す
        (waitress 側に流れて、このアプリの記録には残っていなかった)。
        """
        from unittest import mock

        with mock.patch(
            "kanban.presenters.master.insert_row", side_effect=RuntimeError("想定外テスト")
        ), self.assertLogs("kanban", level="ERROR") as logged:
            res = self.add({"管理番号": "2", "資材": "アングル", "サイズ": "サイズ2"})
        self.assertEqual(res.status_code, 500)
        self.assertIn("想定外テスト", res.json["error"]["message"])
        self.assertTrue(any("想定外のエラー" in line for line in logged.output))

    def test_wrong_password_writes_nothing(self):
        res = self.add({"管理番号": "2", "資材": "x", "サイズ": "サイズ2"}, password="ちがう")
        self.assertEqual(res.status_code, 401)
        self.assertEqual(self.keys(), [1], "断ったのに入っている")

    def test_duplicate_key_is_refused(self):
        res = self.add({"管理番号": "1", "資材": "x", "サイズ": "サイズ1"})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json["error"]["code"], "duplicate_key")

    def test_duplicate_key_written_another_way_is_refused(self):
        """共有DBに ``'５'``(全角・文字)で入っている看板へ ``"5"`` を足させない(kanban.keys)。

        文字の完全一致で比べていたころは別物とみなし、同じ看板がもう 1 行入った。
        """
        import sqlite3

        conn = sqlite3.connect(str(self.shared))
        conn.execute(f"INSERT INTO [{self.TABLE}] ([管理番号], [資材]) VALUES ('５', '外装紙')")
        conn.commit()
        conn.close()
        res = self.add({"管理番号": "5", "資材": "x", "サイズ": "サイズ5"})
        self.assertEqual(res.status_code, 400, res.json)
        self.assertEqual(res.json["error"]["code"], "duplicate_key")

    def test_deletes_a_row(self):
        res = self.post(
            "/api/master/delete",
            {"table": self.TABLE, "row_key": self.row_key(1), "password": "秘密"},
        )
        self.assertEqual(res.status_code, 200, res.json)
        self.assertEqual(self.keys(), [])

    def test_deleting_a_vanished_row_is_a_conflict(self):
        """他端末が先に消していた場合。画面を取り直せば正しい状態が見える。"""
        res = self.post(
            "/api/master/delete",
            {"table": self.TABLE, "row_key": 999_999, "password": "秘密"},
        )
        self.assertEqual(res.status_code, 409)

    def test_write_pulls_the_change_into_the_local_copy(self):
        """**直したのに効かない、を作らない。**

        共有DBは直っているのに盤が変わらなければ、足せていないのと
        区別が付かない。書けたらその場で取り込む。
        """
        called: list[bool] = []
        self.app.config["MANUAL_REFRESH"] = lambda: called.append(True)

        res = self.add({"管理番号": "2", "資材": "x", "サイズ": "サイズ2"})
        self.assertEqual(res.status_code, 200, res.json)
        self.assertTrue(called, "書いたのに取り込んでいない")
        self.assertIn("看板画面にもすぐ出ます", res.json["message"])

    def test_each_operation_says_what_happens_on_the_board(self):
        """消したのに「看板画面にもすぐ出ます」と言わない。"""
        self.app.config["MANUAL_REFRESH"] = lambda: None
        res = self.add({"管理番号": "2", "資材": "x", "サイズ": "サイズ2"})
        self.assertIn("看板画面にもすぐ出ます", res.json["message"])
        table = self.get(f"/api/master/table?table={self.TABLE}").json
        row = next(r for r in table["rows"] if str(r["管理番号"]) == "2")
        res = self.post("/api/master/delete", {
            "table": self.TABLE, "row_key": row[table["row_key"]], "key": row["管理番号"],
            "password": "秘密", "password_confirm": "秘密"})
        self.assertEqual(res.status_code, 200, res.json)
        self.assertIn("看板画面からも消えます", res.json["message"])
        self.assertNotIn("すぐ出ます", res.json["message"])

    def test_another_lines_table_says_it_will_not_show_here(self):
        """看板画面は担当ラインだけを出す。別のラインの表を直したら、そう言う。

        言わないと「足したのに出ない」に見える(担当 機側 のまま 看板_AIM に
        足していた)。
        """
        import sqlite3

        conn = sqlite3.connect(str(self.shared))
        conn.execute("CREATE TABLE [看板_LS] ([管理番号] NUMERIC, [資材] TEXT, [サイズ] TEXT)")
        conn.commit()
        conn.close()
        self.app.config["MANUAL_REFRESH"] = lambda: None
        res = self.post("/api/master/insert", {
            "table": "看板_LS", "values": {"管理番号": "1", "資材": "x", "サイズ": "1"},
            "password": "秘密", "password_confirm": "秘密"})
        self.assertEqual(res.status_code, 200, res.json)
        self.assertIn("この端末の看板画面には出ません", res.json["message"])
        self.assertNotIn("看板画面にもすぐ出ます", res.json["message"])

    def test_a_failing_import_still_reports_the_write(self):
        """取り込みに失敗しても、**書けたことは書けた**。

        ここで失敗を返すと、実際には入っているのに「入らなかった」と
        読ませてしまう。
        """
        def boom():
            raise RuntimeError("共有が落ちています")

        self.app.config["MANUAL_REFRESH"] = boom
        res = self.add({"管理番号": "2", "資材": "x", "サイズ": "サイズ2"})
        self.assertEqual(res.status_code, 200)
        self.assertIn("取り込めませんでした", res.json["message"])
        self.assertEqual(self.keys(), [1, 2], "書けているはず")

    def test_the_add_form_gets_its_material(self):
        """画面が入力欄を組むための材料が、経路からも取れること。"""
        data = self.get(
            f"/api/master/table?table={self.TABLE}"
        ).json
        self.assertEqual(data["next_key"], "2")
        # 不 は状態なので既定値ではなく、固定で入る値として渡る
        self.assertEqual(data["defaults"], {})
        self.assertEqual(data["fixed"], {"不": config.MARK_ON})
        required = {c["name"] for c in data["column_info"] if c["required"]}
        self.assertEqual(required, {config.COL_KEY, config.COL_MATERIAL, config.COL_SIZE})
        state = {c["name"] for c in data["column_info"] if c["state"]}
        self.assertEqual(state, {"不"})


class AliveTest(RouteTestBase):
    """心拍 (:mod:`kanban.idle_exit`)。"""

    def setUp(self) -> None:
        super().setUp()
        from kanban import idle_exit

        idle_exit.reset()

    def tearDown(self) -> None:
        from kanban import idle_exit

        idle_exit.reset()
        super().tearDown()

    def test_alive_needs_no_token(self):
        """**トークンを要求しない。**

        要求すると、トークンが切れた画面が黙って死んだ扱いになり、
        開いているのに終了してしまう。
        """
        res = self.client.post("/api/alive")
        self.assertEqual(res.status_code, 200)

    def test_alive_without_a_watch_is_harmless(self):
        """見張りを立てていない起動(テスト・--no-browser)でも落ちない。"""
        self.assertFalse(self.client.post("/api/alive").json["watching"])

    def test_beat_and_leaving_reach_the_watch(self):
        from kanban import idle_exit

        watch = idle_exit.install(lambda: None, lambda: "", tick_sec=5.0)
        self.assertFalse(watch.connected)

        self.assertTrue(self.client.post("/api/alive").json["watching"])
        self.assertTrue(watch.connected)
        self.assertIsNone(watch.overdue())

        self.client.post("/api/alive?leaving=1")
        # 猶予のあいだはまだ落とさない
        self.assertIsNone(watch.overdue())

    def test_a_beat_says_whether_this_screen_may_operate(self):
        first = self.client.post("/api/alive?screen=A").json
        self.assertTrue(first["active"])
        self.assertEqual(first["others"], 0)

        second = self.client.post("/api/alive?screen=B").json
        self.assertFalse(second["active"], "2枚目が持ち主になっている")
        self.assertEqual(second["others"], 1)

    def test_the_hidden_signal_keeps_the_screen_counted(self):
        """裏に回るときの合図(``hidden=1``)が一覧まで届くこと。

        ブラウザは裏のタブの心拍を間引く・止めるので、心拍が途切れても
        閉じたとは読まない(:mod:`kanban.screen`)。
        """
        from kanban import screen

        self.client.post("/api/alive?screen=A")
        self.client.post("/api/alive?hidden=1&screen=A")
        live = screen.get().live()
        self.assertEqual([(s.id, s.hidden) for s in live], [("A", True)])
        # 戻ってきたら見えている扱いに戻る
        self.assertTrue(self.client.post("/api/alive?screen=A").json["active"])
        self.assertFalse(screen.get().live()[0].hidden)

    def test_a_discarded_tab_takes_over_its_old_name(self):
        from kanban import screen

        self.client.post("/api/alive?hidden=1&screen=old")
        res = self.client.post("/api/alive?screen=new&replaces=old").json
        self.assertTrue(res["active"])
        self.assertEqual([s.id for s in screen.get().live()], ["new"])

    def test_take_moves_the_holder(self):
        self.client.post("/api/alive?screen=A")
        self.client.post("/api/alive?screen=B")
        self.assertTrue(self.client.post("/api/alive?take=1&screen=B").json["active"])
        self.assertFalse(self.client.post("/api/alive?screen=A").json["active"])

    def test_closing_one_of_two_screens_keeps_the_app_alive(self):
        """**2枚のうち1枚を閉じただけで終わらせない。**

        以前は心拍の送り主を区別していなかったので、閉じた合図が来た時点で
        猶予が始まり、残った画面ごと落ちていた(:mod:`kanban.screen`)。
        """
        from kanban import idle_exit

        watch = idle_exit.install(lambda: None, lambda: "", tick_sec=5.0, grace_sec=0.0)
        self.client.post("/api/alive?screen=A")
        self.client.post("/api/alive?screen=B")

        self.client.post("/api/alive?leaving=1&screen=A")
        self.assertIsNone(watch.overdue(), "1枚残っているのに終わろうとしている")

        self.client.post("/api/alive?leaving=1&screen=B")
        self.assertEqual(watch.overdue(), "画面が閉じられました")


class ScreenGuardTest(RouteTestBase):
    """持ち主でない画面からの操作を断る (:mod:`kanban.screen`)。

    **プロセスが1つでも、タブは何枚でも繋がる。** ショートカットをもう一度
    押せばタブは増えるし、アドレスを控えて開くこともできる。2枚とも押せる
    状態にしておくと、同じ看板を2画面で取り合い、片方が古い盤のまま押す。

    画面側の覆いだけに任せないのは、覆いが出るまでの数秒と、覆いを消された
    場合に素通しになるため。
    """

    MODE = config.MODE_WAREHOUSE
    LINE = ""

    def setUp(self) -> None:
        super().setUp()
        from kanban import screen

        screen.reset()

    def tearDown(self) -> None:
        from kanban import screen

        screen.reset()
        super().tearDown()

    def as_screen(self, sid, path, body=None):
        return self.client.post(
            path, json=body or {}, headers={"X-Tool-Token": "test-token", "X-Screen": sid}
        )

    def ship(self, sid, mgmt_no="2"):
        return self.as_screen(
            sid, "/api/board/ship",
            {"line": "LVC", "mgmt_no": mgmt_no, "rev": self.rev(mgmt_no, "LVC")},
        )

    def test_the_holder_can_operate(self):
        self.client.post("/api/alive?screen=A")
        self.assertEqual(self.ship("A").status_code, 200)

    def test_the_second_screen_is_refused(self):
        self.client.post("/api/alive?screen=A")
        self.client.post("/api/alive?screen=B")

        res = self.ship("B")
        self.assertEqual(res.status_code, 423)
        self.assertEqual(res.json["error"]["code"], "not_active")
        self.assertIn("別のタブ", res.json["error"]["message"])

    def test_taking_over_swaps_who_may_operate(self):
        self.client.post("/api/alive?screen=A")
        self.client.post("/api/alive?screen=B")
        self.client.post("/api/alive?take=1&screen=B")

        self.assertEqual(self.ship("B").status_code, 200)
        self.assertEqual(self.ship("A").status_code, 423)

    def test_reading_is_never_refused(self):
        """**読むのは断らない。**

        古い盤を見ていること自体は害が無く、むしろ「もう1枚で何が起きたか」が
        見えたほうがよい。
        """
        self.client.post("/api/alive?screen=A")
        self.client.post("/api/alive?screen=B")
        res = self.client.get(
            "/api/board?line=LVC",
            headers={"X-Tool-Token": "test-token", "X-Screen": "B"},
        )
        self.assertEqual(res.status_code, 200)

    def test_quitting_is_never_refused(self):
        """**使えない画面にも出口を残す。**

        閉じる手段まで無いと、どうすればよいか分からなくなる
        (``stop.bat`` もここを叩く)。
        """
        self.client.post("/api/alive?screen=A")
        self.client.post("/api/alive?screen=B")
        res = self.as_screen("B", "/api/shutdown", {"force": False})
        self.assertNotEqual(res.status_code, 423)

    def test_a_screen_that_does_not_say_who_it_is_passes(self):
        """名乗らない相手(``stop.bat``・試験・手元の ``curl``)は巻き添えに
        しない。ここは事故を防ぐ仕切りで、守りはトークンと Host 検証が持つ。
        """
        self.client.post("/api/alive?screen=A")
        self.assertEqual(self.post(
            "/api/board/ship",
            {"line": "LVC", "mgmt_no": "2", "rev": self.rev("2", "LVC")},
        ).status_code, 200)

    def test_nobody_is_refused_before_the_first_beat(self):
        """心拍より先に操作が飛ぶ(開いた直後に押す)場面で断らないこと。"""
        self.assertEqual(self.ship("A").status_code, 200)


class ReimportTest(RouteTestBase):
    """取り込み直す手段を画面に置く。

    「開き直してください」と言われても、窓の無いアプリでは開き直せない
    ことがある(閉じてもプロセスが残り、次の起動が「すでに起動しています」
    で止まる)。
    """

    def a_real_db(self, name: str = "別.sqlite3") -> str:
        """本物の SQLite を1つ置いて、その場所を返す。

        接続先の変更は**届く場所であること**を確かめてから保存するので、
        でっち上げのパスでは通らない。
        """
        import sqlite3

        path = self.dir / name
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE [看板_LVC] ([管理番号] TEXT)")
        conn.commit()
        conn.close()
        return str(path)

    def test_path_change_refuses_another_tools_file(self):
        """梱包資材マスタなど、看板の表が無い sqlite3 は接続先にしない(そこへ表を作ってしまう)。"""
        import sqlite3

        other = self.dir / "梱包資材マスタ.sqlite3"
        conn = sqlite3.connect(str(other))
        conn.execute("CREATE TABLE PalletMaster (幅 INTEGER)")
        conn.commit()
        conn.close()
        res = self.post("/api/shared-db-path",
                        {"path": str(other), "password": "秘密", "password_confirm": "秘密"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("看板の表(看板_LVC など)が 1 つもありません", res.json["error"]["message"])
        self.assertIn("PalletMaster", res.json["error"]["message"])
        conn = sqlite3.connect(str(other))
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        conn.close()
        self.assertEqual(names, ["PalletMaster"])

    def test_path_change_refuses_a_place_that_is_not_there(self):
        """**届かないパスは保存しない。**

        保存してしまうと、この端末は次の起動からも届かない場所を指し続ける
        ── 画面は「取り込めていません」と言うだけなので、打ち間違いなのか
        共有が落ちているのかが分からない。打った直後なら、はっきりしている。
        """
        before = self.post(
            "/api/shared-db-path",
            {"path": str(self.dir / "ない.sqlite3"), "password": "秘密",
             "password_confirm": "秘密"},
        )
        self.assertEqual(before.status_code, 400)
        self.assertEqual(before.json["error"]["code"], "unreachable_path")
        self.assertIn("見つかりません", before.json["error"]["message"])

    def test_path_change_refuses_something_that_is_not_sqlite(self):
        """変換前の .accdb を指したまま保存させない。"""
        bogus = self.dir / "看板マスタ.accdb"
        bogus.write_text("これは SQLite ではない", encoding="utf-8")
        res = self.post(
            "/api/shared-db-path",
            {"path": str(bogus), "password": "秘密", "password_confirm": "秘密"},
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json["error"]["code"], "unreachable_path")
        self.assertIn("SQLite として開けません", res.json["error"]["message"])

    def test_path_change_refuses_a_folder(self):
        res = self.post(
            "/api/shared-db-path",
            {"path": str(self.dir), "password": "秘密", "password_confirm": "秘密"},
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("ファイルではありません", res.json["error"]["message"])

    def test_reimport_needs_the_password(self):
        self.assertEqual(self.post("/api/reimport").status_code, 401)

    def test_reimport_without_an_importer_is_refused(self):
        res = self.post(
            "/api/reimport", {"password": "秘密", "password_confirm": "秘密"}
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json["error"]["code"], "no_importer")

    def test_reimport_calls_the_hook(self):
        class Result:
            ok_lines = ["LVC", "LS"]
            failed_lines = []

        called = []
        self.app.config["MANUAL_REFRESH"] = lambda: (called.append(1), Result())[1]
        res = self.post(
            "/api/reimport", {"password": "秘密", "password_confirm": "秘密"}
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(called), 1)
        self.assertEqual(res.json["imported_lines"], 2)

    def test_path_change_reconnects_instead_of_asking_to_reopen(self):
        """**開き直しを求めない。** その場で繋ぎ直して取り込む。"""

        class Result:
            ok_lines = ["LVC"]
            failed_lines = []

        seen = []
        target = self.a_real_db("新しい.sqlite3")
        self.app.config["RECONNECT"] = lambda path: (seen.append(path), Result())[1]
        res = self.post(
            "/api/shared-db-path",
            {"path": target, "password": "秘密", "password_confirm": "秘密"},
        )
        self.assertEqual(res.status_code, 200, res.json)
        self.assertTrue(res.json["reconnected"])
        self.assertEqual(seen, [target])
        self.assertNotIn("restart_required", res.json)

    def test_path_change_survives_a_failing_reconnect(self):
        """繋ぎ直しに失敗しても、保存はできたことを伝える。"""

        def boom(path):
            raise RuntimeError("共有フォルダに届きません")

        self.app.config["RECONNECT"] = boom
        res = self.post(
            "/api/shared-db-path",
            {"path": self.a_real_db(), "password": "秘密",
             "password_confirm": "秘密"},
        )
        self.assertEqual(res.status_code, 200, res.json)
        self.assertFalse(res.json["reconnected"])
        self.assertIn("届きません", res.json["message"])


    # -- 受け付けられたかが分かること --------------------------------------
    def change(self, path):
        return self.post("/api/shared-db-path",
                         {"path": path, "password": "秘密", "password_confirm": "秘密"})

    def test_same_place_says_it_did_not_change(self):
        target = self.a_real_db()
        self.assertTrue(self.change(target).json["changed"])
        again = self.change(target.upper() if os.name == "nt" else target + "/")
        self.assertEqual(again.status_code, 200)
        self.assertFalse(again.json["changed"], "綴りの揺れで別の場所扱いになった")
        self.assertIn("変わっていません", again.json["message"])

    def test_an_unset_path_is_saved_even_if_it_equals_the_default(self):
        """決めていない端末で既定の場所のまま「変更」を押したら、その場所に決める。

        以前は「変更はありません」と返して何も保存しなかった。
        """
        from unittest import mock

        target = self.a_real_db("既定.sqlite3")
        with mock.patch.object(config.Config, "resolved_shared_db_path", return_value=target):
            res = self.change(target)
        self.assertTrue(res.json["changed"], res.json)
        self.assertEqual(config.load_config(str(self.dir / "config.json")).shared_db_path, target)

    def test_status_says_where_and_whether_it_is_reachable(self):
        target = self.a_real_db()
        self.change(target)
        st = self.get("/api/shared-db-status").json
        self.assertEqual((st["path"], st["configured"], st["problem"]), (target, True, ""))
        Path(target).unlink()
        self.assertIn("見つかりません", self.get("/api/shared-db-status").json["problem"])

class CancelledWhileShippingTest(RouteTestBase):
    """発送処理中に注文が取り消された(赤なし・緑あり)を、現場に確かめさせる。

    倉庫が発送したのを、現場の端末が取り込む前に現場が赤を消すと起きる
    (赤を消しても発送の印には触れない。VBA と同じ)。
    """

    MODE = config.MODE_SITE

    def setUp(self) -> None:
        super().setUp()
        conn = self.store.connection
        conn.execute("UPDATE kanban_item SET shipped = '〇', confirmed_at = '2026/09/26 10:00:00'"
                     " WHERE line = 'LVC' AND mgmt_no = '1'")
        conn.commit()

    def board(self):
        return self.get("/api/board?line=LVC").json["board"]

    def test_the_board_says_so(self):
        board = self.board()
        self.assertEqual(len(board["alerts"]), 1)
        alert = board["alerts"][0]
        self.assertEqual((alert["line"], alert["mgmt_no"], alert["message"]),
                         ("LVC", "1", "発送処理中に注文が取り消されました"))
        self.assertTrue(alert["can_acknowledge"])
        row = next(r for g in board["groups"] for r in g["rows"] if r["mgmt_no"] == "1")
        self.assertEqual(row["alert"], "発送処理中に注文が取り消されました")

    def test_ordering_again_waits_for_the_site_to_confirm(self):
        """黙って出し直すと発送の印が消え、倉庫が出した物の行方が分からなくなる。"""
        res = self.post("/api/board/order", {"line": "LVC", "mgmt_no": "1", "rev": self.rev("1")})
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json["error"]["code"], "cancelled_while_shipping")
        self.assertTrue(self.store.item("LVC", "1").is_shipped)

    def test_a_stale_screen_is_told_the_same(self):
        """画面が古いまま押した(取り込みで版が進んでいる)ときも、ただの競合ではなく
        「発送処理中に注文が取り消されました」と返す(実機で見つかった件)。"""
        res = self.post("/api/board/order", {"line": "LVC", "mgmt_no": "1", "rev": self.rev("1") - 1})
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json["error"]["code"], "cancelled_while_shipping")
        self.assertTrue(self.store.item("LVC", "1").is_shipped)

    def test_confirming_clears_the_green_and_sends_it(self):
        res = self.post("/api/board/acknowledge", {"line": "LVC", "mgmt_no": "1", "rev": self.rev("1")})
        self.assertEqual(res.status_code, 200, res.json)
        self.assertEqual(res.json["board"]["alerts"], [])
        item = self.store.item("LVC", "1")
        self.assertFalse(item.is_shipped)
        dirty = self.store.connection.execute(
            "SELECT dirty_columns FROM kanban_item WHERE line='LVC' AND mgmt_no='1'").fetchone()[0]
        self.assertIn("shipped", dirty, "共有DBへ送られない")
        self.assertEqual(self.store.unsent_event_count(), 0, "集計の記録を増やしてしまう")
        # 確かめたあとは、ふつうに出し直せる
        again = self.post("/api/board/order", {"line": "LVC", "mgmt_no": "1", "rev": self.rev("1")})
        self.assertEqual(again.status_code, 200)

    def test_confirming_a_normal_row_changes_nothing(self):
        before = self.store.item("LVC", "3")
        res = self.post("/api/board/acknowledge", {"line": "LVC", "mgmt_no": "3", "rev": before.rev})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.store.item("LVC", "3").rev, before.rev)

    def test_a_press_is_sent_right_away(self):
        """押した直後に書き戻しを頼む(周期を待たない)。"""
        called = []
        self.app.config["ON_CHANGED"] = called.append
        self.post("/api/board/order", {"line": "LVC", "mgmt_no": "3", "rev": self.rev("3")})
        self.assertEqual(called, ["LVC"])


class CancelledWhileShippingWarehouseTest(CancelledWhileShippingTest):
    """倉庫には「現場の確認待ち」と出すだけ。確かめるのは現場。"""

    MODE = config.MODE_WAREHOUSE
    LINE = ""

    def test_the_board_says_so(self):
        alert = self.board()["alerts"][0]
        self.assertFalse(alert["can_acknowledge"])

    def test_every_line_is_watched(self):
        """倉庫は別のライン(機側)のタブを開いていても、LVC で起きたものが見える。"""
        alerts = self.get("/api/board?line=LS").json["board"]["alerts"]
        self.assertEqual([(a["line"], a["line_label"], a["mgmt_no"]) for a in alerts], [("LVC", "LVC", "1")])
        rows = [r for g in self.get("/api/board?line=LS").json["board"]["groups"] for r in g["rows"]]
        self.assertFalse(any(r["alert"] for r in rows), "別のラインの行に印を付けた")

    def test_ordering_again_waits_for_the_site_to_confirm(self):
        self.skipTest("倉庫は発注しない")

    def test_a_stale_screen_is_told_the_same(self):
        self.skipTest("倉庫は発注しない")

    def test_confirming_clears_the_green_and_sends_it(self):
        res = self.post("/api/board/acknowledge", {"line": "LVC", "mgmt_no": "1", "rev": self.rev("1")})
        self.assertEqual(res.status_code, 403)
        self.assertTrue(self.store.item("LVC", "1").is_shipped)

    def test_confirming_a_normal_row_changes_nothing(self):
        self.skipTest("倉庫は確認できない")

    def test_a_press_is_sent_right_away(self):
        called = []
        self.app.config["ON_CHANGED"] = called.append
        self.post("/api/board/ship", {"line": "LVC", "mgmt_no": "2", "rev": self.rev("2")})
        self.assertEqual(called, ["LVC"])


class CommentRouteTest(RouteTestBase):
    """看板ごとのコメント(画面から)。"""

    MODE = config.MODE_SITE

    def arrive(self, line="LVC", no="1", body="来週入荷です", uid="u1"):
        """倉庫の端末が書いて、取り込みで届いた。"""
        self.store.merge_comments([{"uid": uid, "at": "2026/09/27 10:00:00", "line": line,
                                    "mgmt_no": no, "kind": "コメント", "side": "倉庫",
                                    "host": "PC-WH", "body": body}])

    def row(self, board, no):
        return next(r for g in board["groups"] for r in g["rows"] if r["mgmt_no"] == no)

    def test_the_board_shows_unread(self):
        self.arrive()
        board = self.get("/api/board?line=LVC").json["board"]
        self.assertEqual((self.row(board, "1")["comments"], self.row(board, "1")["unread"]), (1, 1))
        self.assertEqual([(a["mgmt_no"], a["unread"], a["last_body"]) for a in board["comment_alerts"]],
                         [("1", 1, "来週入荷です")])
        self.assertTrue(board["can_comment"])
        self.assertEqual(board["comment_side"], "現場")

    def test_opening_reads_it(self):
        self.arrive(body="届いた一言")
        res = self.get("/api/comments?line=LVC&mgmt_no=1")
        self.assertEqual(res.status_code, 200)
        self.assertEqual([c["body"] for c in res.json["open"]], ["届いた一言"])
        self.assertEqual(res.json["board"]["comment_alerts"], [], "開いたのに未読のまま")

    def test_opening_shares_the_read_with_the_same_side(self):
        """開いたら既読の印を積んで**すぐ送る**(同じ側のほかの端末の未読を消す)。2 回目は積まない。"""
        called = []
        self.app.config["ON_CHANGED"] = called.append
        self.arrive()
        self.get("/api/comments?line=LVC&mgmt_no=1")
        self.assertEqual(called, ["LVC"])
        marks = [r for r in self.store.unsent_comments() if r["kind"] == "既読"]
        self.assertEqual([(m["side"], m["body"]) for m in marks], [(self.store_side(), "u1")])
        self.get("/api/comments?line=LVC&mgmt_no=1")
        self.assertEqual(called, ["LVC"], "読んだものにもう一度印を積んだ")

    def store_side(self):
        return "現場"

    def press(self, *buttons, no="1"):
        """看板を押した状態にする(赤 = order / 黄 = hold / 緑 = ship)。"""
        for b in buttons:
            self.store.apply_transition("LVC", no, getattr(models, f"{b}_button_changes"), operation=b)

    def make_writable(self):
        """この側が書ける段階にする(現場: 赤だけ)。"""
        self.press("order")

    def test_writing(self):
        self.make_writable()
        called = []
        self.app.config["ON_CHANGED"] = called.append
        res = self.post("/api/comments", {"line": "LVC", "mgmt_no": "1", "body": "  急ぎで\nお願いします  "})
        self.assertEqual(res.status_code, 200, res.json)
        self.assertEqual(res.json["open"][-1]["body"], "急ぎで お願いします")
        self.assertEqual(res.json["open"][-1]["side"], "現場")
        self.assertEqual(called, ["LVC"], "押した直後に送っていない")
        self.assertEqual(self.store.unsent_comment_count(), 1)

    def test_site_writes_only_while_red_alone(self):
        """現場は赤を付けたあと、倉庫が黄・緑を付けるまで。赤の無い看板・受け取る段階は書けない。"""
        res = self.post("/api/comments", {"line": "LVC", "mgmt_no": "1", "body": "x"})
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json["error"]["code"], "comment_closed")
        self.assertIn("赤が付いていない", res.json["error"]["message"])
        self.assertFalse(res.json["can_comment"], "書けない理由と一緒にやり取りを返す")
        self.press("order")
        thread = self.get("/api/comments?line=LVC&mgmt_no=1").json
        self.assertTrue(thread["can_comment"])
        self.assertTrue(thread["hint"])
        self.press("ship")
        thread = self.get("/api/comments?line=LVC&mgmt_no=1").json
        self.assertFalse(thread["can_comment"])
        self.assertIn("受け取るだけ", thread["cannot_reason"])
        self.assertEqual(self.post("/api/comments", {"line": "LVC", "mgmt_no": "1", "body": "x"}).status_code, 409)

    def test_unread_comment_must_be_read_before_pressing(self):
        """相手のコメントを読んでからでないと押せない(届いたばかりで画面に出ていない分も)。"""
        self.press("order", no="1")
        self.store.merge_comments([{"uid": "w1", "at": "2099/01/01 00:00:00", "line": "LVC",
                                    "mgmt_no": "1", "kind": "コメント", "side": "倉庫",
                                    "host": "PC-WH", "body": "サイズ違いです、出し直してください"}])
        rev = self.store.item("LVC", "1").rev
        res = self.post("/api/board/order", {"line": "LVC", "mgmt_no": "1", "rev": rev})
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json["error"]["code"], "unread_comments")
        self.assertEqual(res.json["unread_nos"], ["1"])
        self.assertTrue(self.store.item("LVC", "1").is_ordered, "読まないうちに変わった")
        self.get("/api/comments?line=LVC&mgmt_no=1")          # 開いて読んだ(画面の小窓)
        res = self.post("/api/board/order", {"line": "LVC", "mgmt_no": "1", "rev": rev})
        self.assertEqual(res.status_code, 200, res.json)
        self.assertFalse(self.store.item("LVC", "1").is_ordered)

    def test_own_comment_does_not_block(self):
        """自分の側のコメントだけなら止めない(読む相手は向こう)。"""
        self.press("order", no="1")
        self.store.add_comment("LVC", "1", "現場", "急ぎ")
        rev = self.store.item("LVC", "1").rev
        self.assertEqual(self.post("/api/board/order", {"line": "LVC", "mgmt_no": "1", "rev": rev}).status_code, 200)

    def test_refused_when_empty_too_long_or_unknown(self):
        self.make_writable()
        self.assertEqual(self.post("/api/comments", {"line": "LVC", "mgmt_no": "1", "body": " "}).status_code, 400)
        self.assertEqual(self.post("/api/comments", {"line": "LVC", "mgmt_no": "1", "body": "あ" * 201}).status_code, 400)
        self.assertEqual(self.post("/api/comments", {"line": "LVC", "mgmt_no": "99", "body": "x"}).status_code, 400)
        self.assertEqual(self.post("/api/comments", {"line": "HVC", "mgmt_no": "1", "body": "x"}).status_code, 400,
                         "担当外のラインに書けた")


class CommentWarehouseRouteTest(CommentRouteTest):
    MODE = config.MODE_WAREHOUSE
    LINE = ""

    def arrive(self, line="LVC", no="1", body="急ぎです", uid="u1"):
        self.store.merge_comments([{"uid": uid, "at": "2026/09/27 10:00:00", "line": line,
                                    "mgmt_no": no, "kind": "コメント", "side": "現場",
                                    "host": "PC-SITE", "body": body}])

    def test_the_board_shows_unread(self):
        self.arrive()
        board = self.get("/api/board?line=LVC").json["board"]
        self.assertEqual(self.row(board, "1")["unread"], 1)
        self.assertEqual(board["comment_side"], "倉庫")

    def store_side(self):
        return "倉庫"

    def make_writable(self):
        """倉庫が書ける段階(赤のあと、黄を付けた)。"""
        self.press("order", "hold")

    def test_site_writes_only_while_red_alone(self):
        """(現場の決まり。倉庫は下で確かめる)"""

    def test_unread_comment_must_be_read_before_pressing(self):
        """倉庫: 現場の未読があると黄・一括発送は止まる。読めば押せる。"""
        self.store.merge_comments([{"uid": "s1", "at": "2099/01/01 00:00:00", "line": "LVC",
                                    "mgmt_no": "2", "kind": "コメント", "side": "現場",
                                    "host": "PC-SITE", "body": "100 個で"}])
        rev = self.store.item("LVC", "2").rev
        res = self.post("/api/board/hold", {"line": "LVC", "mgmt_no": "2", "rev": rev})
        self.assertEqual((res.status_code, res.json["error"]["code"]), (409, "unread_comments"))
        res = self.post("/api/board/batch-ship", {"line": "LVC", "items": self.batch_items()})
        self.assertEqual((res.status_code, res.json["unread_nos"]), (409, ["2"]))
        self.assertFalse(self.store.item("LVC", "4").is_shipped, "一括は 1 枚も進めない")
        self.get("/api/comments?line=LVC&mgmt_no=2")
        self.assertEqual(self.post("/api/board/hold", {"line": "LVC", "mgmt_no": "2", "rev": rev}).status_code, 200)

    def test_own_comment_does_not_block(self):
        self.press("hold", no="2")
        self.store.add_comment("LVC", "2", "倉庫", "来週入荷")
        rev = self.store.item("LVC", "2").rev
        self.assertEqual(self.post("/api/board/ship", {"line": "LVC", "mgmt_no": "2", "rev": rev}).status_code, 200)

    def test_warehouse_writes_after_yellow_or_green_and_replies_during_red(self):
        """倉庫は黄・緑を付けたあと。赤だけのあいだは、現場のコメントへの返事だけ書ける。"""
        self.press("order")
        res = self.post("/api/comments", {"line": "LVC", "mgmt_no": "1", "body": "x"})
        self.assertEqual(res.status_code, 409)
        self.assertIn("注文中(黄)か発送(緑)", res.json["error"]["message"])
        self.store.merge_comments([{"uid": "s1", "at": "2099/01/01 00:00:00", "line": "LVC",
                                    "mgmt_no": "1", "kind": "コメント", "side": "現場",
                                    "host": "PC-SITE", "body": "50 と 100 どっち?"}])
        res = self.post("/api/comments", {"line": "LVC", "mgmt_no": "1", "body": "100 で出します"})
        self.assertEqual(res.status_code, 200, res.json)
        self.press("ship", no="2")   # 2 は赤が付いている
        res = self.post("/api/comments", {"line": "LVC", "mgmt_no": "2", "body": "発送しました"})
        self.assertEqual(res.status_code, 200, res.json)

    def test_every_line_is_watched(self):
        """倉庫は別のライン(機側)を見ていても、LVC の新しいコメントが帯に出る。"""
        self.arrive()
        alerts = self.get("/api/board?line=LS").json["board"]["comment_alerts"]
        self.assertEqual([(a["line"], a["mgmt_no"]) for a in alerts], [("LVC", "1")])

    def test_writing(self):
        self.make_writable()
        res = self.post("/api/comments", {"line": "LVC", "mgmt_no": "1", "body": "来週入荷です"})
        self.assertEqual(res.status_code, 200, res.json)
        self.assertEqual(res.json["open"][-1]["side"], "倉庫")

    def test_refused_when_empty_too_long_or_unknown(self):
        self.assertEqual(self.post("/api/comments", {"line": "LVC", "mgmt_no": "1", "body": ""}).status_code, 400)


class CommentViewRouteTest(RouteTestBase):
    MODE = config.MODE_WAREHOUSE_VIEW
    LINE = ""

    def test_view_mode_reads_but_does_not_write(self):
        self.assertEqual(self.get("/api/comments?line=LVC&mgmt_no=1").status_code, 200)
        self.assertFalse(self.get("/api/board?line=LVC").json["board"]["can_comment"])
        self.assertEqual(self.post("/api/comments", {"line": "LVC", "mgmt_no": "1", "body": "x"}).status_code, 403)

    def test_view_mode_does_not_read_for_the_warehouse(self):
        """倉庫参照は見るだけ。開いても既読の印は積まない(倉庫が読んだことにしない)。"""
        self.store.merge_comments([{"uid": "u1", "at": "2026/09/27 10:00:00", "line": "LVC", "mgmt_no": "1",
                                    "kind": "コメント", "side": "現場", "host": "PC-SITE", "body": "急ぎ"}])
        self.get("/api/comments?line=LVC&mgmt_no=1")
        self.assertEqual([r for r in self.store.unsent_comments() if r["kind"] == "既読"], [])


class TableRefreshRouteTest(RouteTestBase):
    """Access の最新で表の中身を入れ替える(画面から)。管理者パスワードが要る。"""

    def setUp(self) -> None:
        super().setUp()
        import json
        import sqlite3

        from kanban import table_refresh

        self.addCleanup(table_refresh._cache.clear)
        cols = "[管理番号] NUMERIC, [資材] TEXT, [サイズ] TEXT, [欲] TEXT, [不] TEXT, [発送] TEXT"
        self.shared = self.dir / "看板マスタ.sqlite3"
        c = sqlite3.connect(str(self.shared))
        c.execute(f"CREATE TABLE [看板_LVC] ({cols})")
        c.execute("INSERT INTO [看板_LVC] VALUES (1, '外装紙', 'A', '〇', '', '')")
        c.commit()
        c.close()
        self.src = self.dir / "最新.sqlite3"
        c = sqlite3.connect(str(self.src))
        c.execute(f"CREATE TABLE [看板_LVC] ({cols})")
        c.executemany("INSERT INTO [看板_LVC] VALUES (?, ?, ?, '', '〇', '')", [(1, "外装紙X", "A"), (2, "テープ", "B")])
        c.commit()
        c.close()
        (self.dir / "config.json").write_text(json.dumps({
            "shared_db_path": str(self.shared), "admin_password_hash": config.hash_password("秘密"),
            "line": "LVC"}, ensure_ascii=False), encoding="utf-8")
        # 控え・受け取ったファイルは、この試験の中の手元領域へ
        patcher = mock.patch.dict(os.environ, {"KANBAN_LOCAL_DIR": str(self.dir / "local")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_needs_the_password(self):
        self.assertEqual(self.post("/api/table-refresh/plan", {"path": str(self.src)}).status_code, 401)
        self.assertEqual(self.post("/api/table-refresh/run", {"path": str(self.src), "tables": ["看板_LVC"]}).status_code, 401)

    def test_plan_then_run(self):
        plan = self.post("/api/table-refresh/plan", {"path": str(self.src), "password": "秘密"}).json["plan"]
        self.assertEqual([(t["name"], t["can_refresh"]) for t in plan["tables"]], [("看板_LVC", True)])
        called = []
        self.app.config["MANUAL_REFRESH"] = lambda: called.append(1)
        res = self.post("/api/table-refresh/run", {"path": str(self.src), "tables": ["看板_LVC"], "password": "秘密"})
        self.assertEqual(res.status_code, 200, res.json)
        self.assertEqual(res.json["refreshed"], [{"name": "看板_LVC", "before": 1, "after": 2}])
        self.assertEqual(called, [1], "入れ替えたあと取り込み直していない")
        self.assertTrue(Path(res.json["backup"]).is_file())
        import sqlite3
        c = sqlite3.connect(str(self.shared))
        rows = c.execute("SELECT 管理番号, 資材, 欲 FROM [看板_LVC] ORDER BY 管理番号").fetchall()
        c.close()
        self.assertEqual(rows, [(1, "外装紙X", "〇"), (2, "テープ", "")], "状態を消した・足していない")

    def test_bad_requests(self):
        body = {"path": str(self.src), "password": "秘密"}
        self.assertEqual(self.post("/api/table-refresh/run", {**body, "tables": "看板_LVC"}).status_code, 400)
        self.assertEqual(self.post("/api/table-refresh/run", {**body, "tables": []}).status_code, 400)
        res = self.post("/api/table-refresh/run", {"path": str(self.shared), "tables": ["看板_LVC"], "password": "秘密"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("共有DBそのもの", res.json["error"]["message"])

    def test_upload(self):
        data = {"file": (open(self.src, "rb"), "看板マスタ.sqlite3"), "password": "秘密"}
        res = self.client.post("/api/table-refresh/upload", data=data,
                               content_type="multipart/form-data", headers={"X-Tool-Token": "test-token"})
        data["file"][0].close()
        self.assertEqual(res.status_code, 200, res.json)
        self.assertTrue(res.json["path"].endswith("看板マスタ.sqlite3"))
        self.assertIn(str(self.dir / "local"), res.json["path"], "手元の作業フォルダに置いていない")
        self.assertEqual(res.json["plan"]["tables"][0]["name"], "看板_LVC")

    def test_upload_as_bytes(self):
        """画面は中身をバイト列のまま送る(デスクトップ版の窓は FormData のファイルの中身を渡さないことがある)。"""
        from urllib.parse import quote

        payload = self.src.read_bytes()
        res = self.client.post(f"/api/table-refresh/upload?name={quote('看板マスタ.sqlite3')}&size={len(payload)}",
                               data=payload, content_type="application/octet-stream",
                               headers={"X-Tool-Token": "test-token", "X-Admin-Password": quote("秘密")})
        self.assertEqual(res.status_code, 200, res.json)
        saved = Path(res.json["path"])
        self.assertEqual(saved.read_bytes(), payload, "1バイトでも違って置いた")
        self.assertEqual(res.json["plan"]["tables"][0]["name"], "看板_LVC")
        self.assertEqual(res.json["plan"]["source_facts"]["size"], len(payload))

    def test_upload_that_arrived_short_is_refused(self):
        """届いた大きさが画面の言う大きさと違えば置かない(欠けたファイルを読んで「小さすぎます」にしない)。"""
        from urllib.parse import quote

        payload = self.src.read_bytes()
        res = self.client.post(f"/api/table-refresh/upload?name={quote('看板マスタ.accdb')}&size={len(payload) + 10}",
                               data=payload, content_type="application/octet-stream",
                               headers={"X-Tool-Token": "test-token", "X-Admin-Password": quote("秘密")})
        self.assertEqual(res.status_code, 400)
        self.assertIn("欠けて届きました", res.json["error"]["message"])
        self.assertFalse(any((self.dir / "local").rglob("看板マスタ.accdb")), "欠けたファイルを置いた")

    def test_upload_as_bytes_needs_the_password(self):
        res = self.client.post("/api/table-refresh/upload?name=a.accdb&size=1", data=b"x",
                               content_type="application/octet-stream", headers={"X-Tool-Token": "test-token"})
        self.assertEqual(res.status_code, 401)

    def test_the_settings_page_has_the_card(self):
        html = self.get("/settings").get_data(as_text=True)
        for needle in ('id="refresh-card"', 'id="rf-drop"', 'id="rf-run"'):
            self.assertIn(needle, html)


class TableAndRowLifecycleTest(RouteTestBase):
    """表・行の追加削除まわり(実機テストで見つけた件)。"""

    MODE = config.MODE_SITE

    def setUp(self) -> None:
        super().setUp()
        import json
        import sqlite3

        self.shared = self.dir / "看板マスタ.sqlite3"
        c = sqlite3.connect(str(self.shared))
        for t in ("看板_LVC", "看板_LS"):
            c.execute(f"CREATE TABLE [{t}] ([管理番号] NUMERIC, [資材] TEXT, [サイズ] TEXT, [欲] TEXT,"
                      " [不] TEXT, [更新日] TEXT, [発送] TEXT, [倉庫確認日時] TEXT, [常設品] TEXT,"
                      " [保留] TEXT, [注文中日時] TEXT)")
            c.executemany(f"INSERT INTO [{t}] VALUES (?, '外装紙', ?, ?, ?, '', ?, '', '〇', ?, '')", [
                (1, "A", "", "〇", "", ""),         # 出していない
                (2, "B", "〇", "", "", ""),         # 発注中
                (3, "C", "〇", "", "〇", ""),       # 発送済み
                (4, "D", "〇", "", "", "〇"),       # 注文中
            ])
        c.commit()
        c.close()
        (self.dir / "config.json").write_text(json.dumps({
            "shared_db_path": str(self.shared), "admin_password_hash": config.hash_password("秘密"),
            "line": "LVC"}, ensure_ascii=False), encoding="utf-8")

    def rowid(self, table, no):
        rows = self.get(f"/api/master/table?table={table}").json["rows"]
        return next(r["__行"] for r in rows if int(r["管理番号"]) == no)

    def delete(self, no, table="看板_LVC"):
        return self.post("/api/master/delete", {"table": table, "row_key": self.rowid(table, no),
                                                "key": no, "password": "秘密"})

    def test_every_write_names_the_file_it_wrote(self):
        """マスタで書いたら、書き込み先(共有DB)と更新日時を返す。一覧にも読み書きする共有DBを出す。"""
        self.assertEqual(self.get("/api/master").json["path"], str(self.shared))
        res = self.post("/api/master/insert", {"table": "看板_LVC", "password": "秘密",
                                               "values": {"管理番号": "9", "資材": "テープ", "サイズ": "幅9"}})
        self.assertEqual(res.status_code, 200, res.json)
        self.assertEqual(res.json["written"], str(self.shared))
        self.assertRegex(res.json["written_at"], r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertEqual(self.delete(9).json["written"], str(self.shared))

    def test_a_kanban_in_use_is_not_deleted(self):
        """発注中・発送済み・注文中の看板は消さない(消すと注文の行き場が無くなる)。"""
        for no, what in ((2, "発注中"), (3, "発送済み"), (4, "注文中")):
            res = self.delete(no)
            self.assertEqual(res.status_code, 409, (no, res.json))
            self.assertEqual(res.json["error"]["code"], "in_use")
            self.assertIn(what, res.json["error"]["message"])
        self.assertEqual(self.delete(1).status_code, 200, "出していない看板まで消せない")

    def test_deleting_a_kanban_clears_its_comments(self):
        """消した看板のコメントが、同じ番号で足し直した看板に出ない。"""
        self.store.add_comment("LVC", "1", "倉庫", "消す前のコメント")
        self.assertEqual(self.delete(1).status_code, 200)
        self.assertEqual(self.store.comment_thread("LVC", "1")["open"], [])
        self.assertEqual(self.store.unsent_comment_count(), 2, "片付けの印を共有DBへ送る列に積んでいない")
        res = self.post("/api/master/insert", {"table": "看板_LVC", "password": "秘密",
                                               "values": {"管理番号": "1", "資材": "別", "サイズ": "Z"}})
        self.assertEqual(res.status_code, 200, res.json)
        self.assertEqual(self.store.comment_counts("LVC", "現場"), {})

    def test_a_table_removed_from_the_shared_db(self):
        """見ているラインの表が共有DBから消えた: 帯で言い、ボタンを止める。戻れば元どおり。"""
        import sqlite3

        from kanban.db import sync
        from kanban.db.shared import SharedDb

        gateway = SharedDb(str(self.shared), cache_dir=str(self.dir / "cache"))
        sync.import_all(self.store, gateway, lines=["LVC"])
        c = sqlite3.connect(str(self.shared))
        c.execute("ALTER TABLE [看板_LVC] RENAME TO [看板_LVC_old]")
        c.commit()
        c.close()
        gateway.forget_copy()
        sync.import_all(self.store, gateway, lines=["LVC"])
        board = self.get("/api/board?line=LVC").json["board"]
        self.assertIn("共有DBに 看板_LVC がありません", board["missing_table"])
        buttons = [r[k]["enabled"] for g in board["groups"] for r in g["rows"] for k in ("size", "ship", "hold")]
        self.assertTrue(buttons and not any(buttons), "押しても届かないボタンが出ている")

        called = []
        self.app.config["MANUAL_REFRESH"] = lambda: (called.append(1), sync.import_all(self.store, gateway, lines=["LVC"]))[1]
        res = self.post("/api/refresh?line=LVC")
        self.assertFalse(res.json["imported"])
        self.assertIn("看板_LVC がありません", res.json["import_message"])

        c = sqlite3.connect(str(self.shared))
        c.execute("ALTER TABLE [看板_LVC_old] RENAME TO [看板_LVC]")
        c.commit()
        c.close()
        gateway.forget_copy()
        res = self.post("/api/refresh?line=LVC")
        self.assertTrue(res.json["imported"])
        self.assertEqual(res.json["board"]["missing_table"], "", "表が戻っても帯が消えない")

    def test_other_lines_do_not_clear_the_mark(self):
        """倉庫と現場で取り込むラインが違っても、今回見なかったラインの印は消さない。"""
        self.store.set_meta("missing_line_tables", "HVC")
        from kanban.db import sync
        from kanban.db.shared import SharedDb

        sync.import_all(self.store, SharedDb(str(self.shared), cache_dir=str(self.dir / "c2")), lines=["LVC"])
        self.assertEqual(self.store.get_meta("missing_line_tables", ""), "HVC")


class UnsendableOperationsTest(RouteTestBase):
    """「対象行なし」で送れない操作(要確認)の原因と片付け(現場から届いた質問の件)。

    共有DBの看板_L1 から看板が消えた・番号の書き方が違う、などで送り先が無い操作は、
    待っても減らない。原因を書き、捨てて共有DBの状態に戻す手段を画面に置く。
    """

    MODE = config.MODE_WAREHOUSE
    LINE = ""

    def setUp(self) -> None:
        super().setUp()
        import json
        import sqlite3

        from kanban.db import sync
        from kanban.db.shared import SharedDb

        self.sync = sync
        self.shared = self.dir / "看板マスタ.sqlite3"
        c = sqlite3.connect(str(self.shared))
        c.execute("CREATE TABLE [看板_LVC] ([管理番号] NUMERIC, [資材] TEXT, [サイズ] TEXT, [欲] TEXT,"
                  " [不] TEXT, [更新日] TEXT, [発送] TEXT, [倉庫確認日時] TEXT, [常設品] TEXT)")
        c.executemany("INSERT INTO [看板_LVC] VALUES (?, '外装紙', ?, '〇', '', '2026/09/01 09:00:00', '', '', '〇')",
                      [(2, "B"), (4, "D"), (6, "F")])
        c.commit()
        c.close()
        (self.dir / "config.json").write_text(json.dumps({
            "shared_db_path": str(self.shared), "admin_password_hash": config.hash_password("秘密")},
            ensure_ascii=False), encoding="utf-8")
        self.gateway = SharedDb(str(self.shared), cache_dir=str(self.dir / "cache"))
        self.sync.import_all(self.store, self.gateway, lines=["LVC"])
        self.app.config["MANUAL_REFRESH"] = lambda: self.sync.import_all(self.store, self.gateway, lines=["LVC"])

    def ship_then_break(self, no, how):
        """倉庫が発送した(未反映)あとで、共有DBの側からその行が見えなくなる。"""
        import sqlite3

        from kanban.domain import models

        self.store.apply_transition("LVC", no, models.ship_button_changes, operation="ship")
        c = sqlite3.connect(str(self.shared))
        if how == "deleted":
            c.execute("DELETE FROM [看板_LVC] WHERE 管理番号 = ?", (int(no),))
        else:   # 全角の番号(文字として入っている)
            c.execute("UPDATE [看板_LVC] SET 管理番号 = ? WHERE 管理番号 = ?", (chr(0xFF10 + int(no)), int(no)))
        c.commit()
        c.close()
        self.gateway.forget_copy()
        for _ in range(5):
            self.sync.export_pending(self.store, self.gateway)

    def test_the_reason_says_which(self):
        self.ship_then_break("2", "deleted")
        self.ship_then_break("4", "fullwidth")
        errors = {f["mgmt_no"]: f["error"] for f in self.get("/api/settings").json["failures"]}
        self.assertIn("管理番号 2 の行がありません", errors["2"])
        self.assertIn("「４」と書かれていて", errors["4"])

    def test_discard_goes_back_to_the_shared_state(self):
        self.ship_then_break("2", "deleted")
        self.assertEqual(self.post("/api/sync/discard-failed").status_code, 401, "パスワード無しで捨てられた")
        res = self.post("/api/sync/discard-failed", {"password": "秘密"})
        self.assertEqual(res.status_code, 200, res.json)
        self.assertEqual([d["mgmt_no"] for d in res.json["discarded"]], ["2"])
        self.assertEqual(self.store.pending_count(), 0)
        self.assertIsNone(self.store.item("LVC", "2"), "共有DBに無い看板が手元に残った")
        self.assertEqual(self.get("/api/settings").json["failures"], [])

    def test_discard_keeps_rows_that_still_exist_as_shared_says(self):
        """番号の書き方が違うだけの行は、捨てたあと共有DBの状態(発送していない)に戻る。"""
        self.ship_then_break("6", "fullwidth")
        self.post("/api/sync/discard-failed", {"password": "秘密"})
        self.assertEqual(self.store.pending_count(), 0)

    def test_retry_after_fixing_the_shared_side(self):
        import sqlite3

        self.ship_then_break("2", "deleted")
        c = sqlite3.connect(str(self.shared))
        c.execute("INSERT INTO [看板_LVC] VALUES (2, '外装紙', 'B', '〇', '', '', '', '', '〇')")
        c.commit()
        c.close()
        self.gateway.forget_copy()
        res = self.post("/api/sync/retry-failed", {"password": "秘密"})
        self.assertEqual(res.json["retried"], 1)
        self.sync.export_pending(self.store, self.gateway)
        self.assertEqual(self.store.pending_count(), 0, "直したあとに送れない")

    def test_warehouse_header_does_not_claim_a_line(self):
        """倉庫モードでは、設定に残っている現場の頃の担当ラインを帯に出さない。"""
        from app import create_app

        app = create_app(config.MODE_WAREHOUSE, token="t", port=8751, store=self.store, line="L1")
        html = app.test_client().get("/board", headers={"X-Tool-Token": "t"}).get_data(as_text=True)
        self.assertNotIn('<span class="k">担当</span>', html)


class ShutdownTest(RouteTestBase):
    def test_shutdown_refuses_while_pending(self):
        """未反映の書き戻しがあるうちは止めない(基盤仕様書 2.8)。"""
        from app.routes import health as health_routes

        health_routes.set_busy_check(lambda: "書き戻しの途中です")
        try:
            res = self.post("/api/shutdown", {"force": False})
            self.assertEqual(res.status_code, 409)
            self.assertFalse(res.json["stopped"])
        finally:
            health_routes.set_busy_check(lambda: "")

    def test_force_stops_even_when_busy(self):
        from app.routes import health as health_routes

        stopped = []
        health_routes.set_busy_check(lambda: "書き戻しの途中です")
        health_routes.set_shutdown_hook(lambda: stopped.append(True))
        try:
            res = self.post("/api/shutdown", {"force": True})
            self.assertEqual(res.status_code, 200)
            self.assertTrue(res.json["stopped"])
        finally:
            health_routes.set_busy_check(lambda: "")
            health_routes.set_shutdown_hook(None)


class StaticCacheTest(RouteTestBase):
    """**アプリを入れ替えたら、画面の JS・CSS も必ず入れ替わること。**

    以前は ``/static/`` をすべて 7 日・``immutable`` で渡し、印は版
    (``config/app.json``)だった。版を上げずに入れ替えると URL が変わらず、
    設定画面の JS だけ古いまま残って、新しいサーバに古い画面で送って断られた
    (看板の状態を読むだけにした直後、「不」が空で送られた)。
    """

    def test_the_stamp_is_a_fingerprint_of_the_contents(self):
        from app import _static_stamp
        import shutil, tempfile

        d = Path(tempfile.mkdtemp(prefix="kanban_static_"))
        try:
            (d / "js").mkdir()
            (d / "js" / "a.js").write_text("one", encoding="utf-8")
            before = _static_stamp(d)
            (d / "js" / "a.js").write_text("two", encoding="utf-8")
            self.assertNotEqual(before, _static_stamp(d), "中身を変えても印が変わらない")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_only_the_current_stamp_is_kept_long(self):
        stamp = self.app.config["STATIC_STAMP"]
        fresh = self.get(f"/static/js/api.js?v={stamp}")
        self.assertIn("immutable", fresh.headers["Cache-Control"])
        for url in ("/static/js/api.js", "/static/js/api.js?v=3.0.0"):
            self.assertEqual(self.get(url).headers["Cache-Control"], "no-cache", url)

    def test_pages_carry_the_stamp_and_an_import_map(self):
        """JS の中の ``import '../api.js'`` にも同じ印が付くこと(import map)。"""
        stamp = self.app.config["STATIC_STAMP"]
        html = self.get("/board").get_data(as_text=True)
        self.assertIn(f"js/app.js?v={stamp}", html)
        self.assertIn('<script type="importmap">', html)
        self.assertIn(f'"/static/js/api.js": "/static/js/api.js?v={stamp}"', html)
        # import map はどの module より先に置く(後ろだと効かない)
        self.assertLess(html.index("importmap"), html.index('type="module"'))


class StatsRouteTest(RouteTestBase):
    """看板集計(読むだけ。パスワードは訊かない)。

    ボタンを押す → 書き戻しで共有DBの [看板履歴] へ届く → 設定の「看板集計」と
    CSV に出る、までを通しで確かめる。
    """

    def setUp(self) -> None:
        super().setUp()
        import json
        import sqlite3

        self.shared = self.dir / "看板マスタ.sqlite3"
        conn = sqlite3.connect(str(self.shared))
        conn.execute("CREATE TABLE [看板_LVC] ([管理番号] NUMERIC, [資材] TEXT, [サイズ] TEXT)")
        conn.executemany("INSERT INTO [看板_LVC] VALUES (?, ?, ?)",
                         [(1, "外装紙", "A"), (2, "外装紙", "B")])
        conn.commit()
        conn.close()
        (self.dir / "config.json").write_text(
            json.dumps({"shared_db_path": str(self.shared), "line": "LVC"}, ensure_ascii=False),
            encoding="utf-8",
        )

    def press_and_send(self):
        from kanban.db import sync
        from kanban.db.shared import SharedDb
        from kanban.domain import models

        no = sample_rows()[0]["mgmt_no"]
        item = self.store.item("LVC", no)
        if item.is_ordered:     # 見本の行が赤のときは、一度届いたことにしてから
            self.store.apply_transition("LVC", no, models.ship_button_changes, operation="ship")
            self.store.apply_transition("LVC", no, models.order_button_changes, operation="order")
        self.store.apply_transition("LVC", no, models.order_button_changes, operation="order")
        gateway = SharedDb(str(self.shared), cache_dir=str(self.dir / "cache"))
        self.assertGreater(sync.export_events(self.store, gateway), 0)

    def test_view_counts_what_was_pressed(self):
        self.press_and_send()
        res = self.get("/api/stats?line=LVC")
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertTrue(body["available"])
        self.assertEqual(body["selected_lines"], ["LVC"])
        this_month = body["months"][-1]
        row = [r for r in body["rates"]["LVC"] if r["month"] == this_month][0]
        self.assertGreaterEqual(row["count"], 1)
        self.assertEqual(row["total"], 2)
        self.assertEqual(body["lines"][0]["slot"], 0, "色の番号はラインの並びで決める")

    def test_period_and_line_are_checked(self):
        body = self.get("/api/stats?from=2026-03&to=2026-01&line=どこか").get_json()
        self.assertEqual(body["months"], ["2026-01", "2026-02", "2026-03"], "逆の期間は入れ替える")
        self.assertGreater(len(body["selected_lines"]), 0, "知らないラインは全ラインにする")

    def test_csv_opens_in_excel(self):
        self.press_and_send()
        # リンクで落とすのでヘッダを付けられない。起動トークンは ?t= で渡る
        res = self.client.get("/api/stats/csv?kind=cycles&line=LVC&t=test-token")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.mimetype, "text/csv")
        raw = res.get_data()
        self.assertTrue(raw.startswith("\ufeff".encode("utf-8")), "BOM が無いと Excel で化ける")
        self.assertIn("filename*=UTF-8''", res.headers["Content-Disposition"])
        text = raw.decode("utf-8-sig")
        self.assertTrue(text.startswith("ライン,管理番号"))
        self.assertIn("\r\nLVC,", text)

    def test_csv_needs_the_token_and_a_known_kind(self):
        self.assertEqual(self.client.get("/api/stats/csv?kind=cycles").status_code, 401)
        self.assertEqual(self.get("/api/stats/csv?kind=なに").status_code, 400)

    def test_settings_page_has_the_tab(self):
        html = self.get("/settings").get_data(as_text=True)
        self.assertIn('id="tab-stats"', html)
        self.assertIn('id="panel-stats"', html)
        for kind in ("cycles", "weekly", "monthly", "lead", "summary", "rate"):
            self.assertIn(f'data-csv="{kind}"', html)


class AccessRouteTest(RouteTestBase):
    """アクセス権限(梱包資材マスタ)をマスタ管理で足す・直す・消す / モードの切り替え。"""

    MODE = config.MODE_SITE

    def setUp(self) -> None:
        super().setUp()
        from kanban import access_control as ac

        self.ac = ac
        self.me = ac.current_identity()
        self.shared = self.dir / "看板マスタ.sqlite3"
        conn = sqlite3.connect(str(self.shared))
        conn.execute('CREATE TABLE "看板_LVC" ("管理番号" TEXT, "資材" TEXT)')
        # 看板マスタにたまたま同じ名前の表があっても、そちらは直させない
        conn.execute('CREATE TABLE "アクセス権限" ("管理番号" INTEGER, "権限" TEXT)')
        conn.commit()
        conn.close()
        # 梱包資材マスタ。**アクセス権限の表はまだ無い**(ほかのツールの表だけ)
        self.packing = self.dir / "梱包資材マスタ.sqlite3"
        conn = sqlite3.connect(str(self.packing))
        conn.execute('CREATE TABLE "BoardMaster" ("ID" INTEGER, "名前" TEXT)')
        conn.execute("INSERT INTO BoardMaster VALUES (1, 'そのまま')")
        conn.commit()
        conn.close()
        (self.dir / "config.json").write_text(json.dumps({
            "shared_db_path": str(self.shared), "admin_password_hash": config.hash_password("秘密"),
            "line": "LVC"}, ensure_ascii=False), encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {"KANBAN_LOCAL_DIR": str(self.dir / "local")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def rows(self):
        conn = sqlite3.connect(str(self.packing))
        try:
            return conn.execute(
                'SELECT 管理番号, ログインID, PC名, 権限, 有効 FROM "アクセス権限" ORDER BY 管理番号').fetchall()
        finally:
            conn.close()

    def insert(self, **values):
        base = {"管理番号": "", "ログインID": self.me.login_id, "PC名": self.me.pc_name,
                "権限": "mode:material", "有効": "1", "備考": ""}
        base.update(values)
        return self.post("/api/master/insert", {"table": "アクセス権限", "db": "access",
                                                "values": base, "password": "秘密"})

    def open_access(self):
        self.get("/api/master")   # 表が無ければ作る
        return self.get("/api/master/table?table=%E3%82%A2%E3%82%AF%E3%82%BB%E3%82%B9%E6%A8%A9%E9%99%90&db=access").json

    # -- 表を用意する ---------------------------------------------------
    def test_master_list_offers_the_access_table_and_creates_it(self):
        data = self.get("/api/master").json
        self.assertIn("看板_LVC", [t["name"] for t in data["tables"]])
        extra = data["extra"][0]
        self.assertEqual((extra["db"], extra["name"], extra["available"]), ("access", "アクセス権限", True))
        self.assertIn("作りました", extra["created"])
        self.assertEqual(extra["path"], str(self.packing))
        self.assertEqual(self.rows(), [])
        conn = sqlite3.connect(str(self.packing))
        self.assertEqual(conn.execute("SELECT * FROM BoardMaster").fetchall(), [(1, "そのまま")])
        conn.close()
        # 2 回目は作らない
        self.assertEqual(self.get("/api/master").json["extra"][0]["created"], "")

    def test_missing_packing_master_is_explained(self):
        self.packing.unlink()
        extra = self.get("/api/master").json["extra"][0]
        self.assertFalse(extra["available"])
        self.assertIn("梱包資材マスタが見つかりません", extra["why"])

    def test_open_shows_how_to_fill_it(self):
        v = self.open_access()
        self.assertEqual(v["view_only_why"], "")
        self.assertEqual(v["db"], "access")
        self.assertEqual(v["path"], str(self.packing))
        self.assertEqual(v["columns"], ["管理番号", "ログインID", "PC名", "権限", "有効", "備考"])
        self.assertEqual(v["choices"]["権限"][:2], ["mode:field", "mode:material"])
        self.assertEqual(v["defaults"]["ログインID"], self.me.login_id)
        self.assertEqual(v["defaults"]["PC名"], self.me.pc_name)
        self.assertIn("ほかのツール", v["note"])
        required = {c["name"] for c in v["column_info"] if c["required"]}
        self.assertEqual(required, {"管理番号", "権限"})

    def test_sorting_by_column_name(self):
        self.open_access()
        self.insert(管理番号="1", ログインID="b-user")
        self.insert(管理番号="2", ログインID="a-user")
        v = self.get("/api/master/table?table=%E3%82%A2%E3%82%AF%E3%82%BB%E3%82%B9%E6%A8%A9%E9%99%90"
                     "&db=access&sort=%E3%83%AD%E3%82%B0%E3%82%A4%E3%83%B3ID&dir=asc").json
        self.assertEqual(v["sort"], "ログインID")
        self.assertEqual([r["ログインID"] for r in v["rows"]], ["a-user", "b-user"])

    def test_other_tables_of_the_packing_master_are_not_exposed(self):
        self.get("/api/master")
        v = self.get("/api/master/table?table=BoardMaster&db=access").json
        self.assertFalse(v["available"])
        res = self.post("/api/master/update", {"table": "BoardMaster", "db": "access", "row_key": 1,
                                               "column": "名前", "value": "x", "password": "秘密"})
        self.assertEqual(res.status_code, 400)

    def test_same_named_table_in_the_kanban_master_stays_read_only(self):
        v = self.get("/api/master/table?table=%E3%82%A2%E3%82%AF%E3%82%BB%E3%82%B9%E6%A8%A9%E9%99%90").json
        self.assertTrue(v["view_only_why"])

    # -- 足す -----------------------------------------------------------
    def test_insert_needs_the_password(self):
        self.open_access()
        res = self.post("/api/master/insert", {"table": "アクセス権限", "db": "access",
                                               "values": {"管理番号": "1", "権限": "mode:field",
                                                          "ログインID": "a"}})
        self.assertEqual(res.status_code, 401)
        self.assertEqual(self.rows(), [])

    def test_adding_a_row_grants_the_mode_right_away(self):
        """足したら**そのまま**切り替えられる(開き直さなくてよい)。"""
        self.open_access()
        self.assertEqual(self.post("/api/mode", {"mode": config.MODE_WAREHOUSE}).status_code, 403)
        res = self.insert(管理番号="1")
        self.assertEqual(res.status_code, 200, res.json)
        self.assertEqual(res.json["written"], str(self.packing))
        self.assertIn("倉庫モード", res.json["message"])
        self.assertIn(config.MODE_WAREHOUSE, res.json["access"]["grant"]["allowed_modes"])
        self.assertEqual(self.rows(), [(1, self.me.login_id, self.me.pc_name, "mode:material", 1)])
        self.assertEqual(self.post("/api/mode", {"mode": config.MODE_WAREHOUSE}).status_code, 200)
        self.assertEqual(self.store.get_device_mode(), config.MODE_WAREHOUSE)

    def test_other_tools_permissions_are_accepted(self):
        self.open_access()
        res = self.insert(管理番号="1", 権限="master:edit")
        self.assertEqual(res.status_code, 200, res.json)
        self.assertIn("ほかのツールの権限", res.json["message"])
        res = self.insert(管理番号="2", 権限="mode:feild")
        self.assertEqual(res.status_code, 200)
        self.assertIn("mode:field の打ち間違いなら", res.json["message"])
        self.assertEqual([r[3] for r in self.rows()], ["master:edit", "mode:feild"])
        # どちらもこのツールの権限にはならない
        self.assertEqual(self.post("/api/mode", {"mode": config.MODE_WAREHOUSE}).status_code, 403)

    def test_row_without_condition_is_refused(self):
        self.open_access()
        res = self.insert(管理番号="1", ログインID="", PC名="")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json["error"]["code"], "no_condition")
        self.assertEqual(self.rows(), [])

    def test_blank_permission_is_refused(self):
        self.open_access()
        res = self.insert(管理番号="1", 権限="")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.rows(), [])

    def test_duplicate_row_is_refused(self):
        self.open_access()
        self.assertEqual(self.insert(管理番号="1").status_code, 200)
        res = self.insert(管理番号="2", ログインID=self.me.login_id.upper())
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json["error"]["code"], "duplicate_rule")

    # -- 直す・消す -----------------------------------------------------
    def test_disable_then_delete(self):
        self.open_access()
        self.insert(管理番号="1")
        self.insert(管理番号="2", 権限="mode:field")
        v = self.open_access()
        row = next(r for r in v["rows"] if r["権限"] == "mode:material")
        res = self.post("/api/master/update", {"table": "アクセス権限", "db": "access",
                                               "row_key": row[v["row_key"]], "key": row["管理番号"],
                                               "column": "有効", "value": "0", "password": "秘密"})
        self.assertEqual(res.status_code, 200, res.json)
        self.assertNotIn(config.MODE_WAREHOUSE, res.json["access"]["grant"]["allowed_modes"])
        self.assertEqual(self.post("/api/mode", {"mode": config.MODE_WAREHOUSE}).status_code, 403)

        res = self.post("/api/master/delete", {"table": "アクセス権限", "db": "access",
                                               "row_key": row[v["row_key"]], "key": row["管理番号"],
                                               "password": "秘密"})
        self.assertEqual(res.status_code, 200, res.json)
        self.assertEqual([r[3] for r in self.rows()], ["mode:field"])

    def test_removing_the_running_modes_permission_is_explained(self):
        """いま動いているモードの権限を消したら、次はどのモードで開くかまで言う。"""
        self.app.config["MODE"] = config.MODE_WAREHOUSE
        self.open_access()
        res = self.insert(管理番号="1")
        v = self.open_access()
        row = v["rows"][0]
        res = self.post("/api/master/delete", {"table": "アクセス権限", "db": "access",
                                               "row_key": row[v["row_key"]], "key": row["管理番号"],
                                               "password": "秘密"})
        self.assertEqual(res.status_code, 200)
        self.assertIn("次に開いたときは現場モードで開きます", res.json["message"])

    # -- 置き場所・状態 -------------------------------------------------
    def test_access_state_names_this_terminal(self):
        self.open_access()
        self.insert(管理番号="1")
        data = self.get("/api/access").json
        self.assertEqual(data["path"], str(self.packing))
        self.assertTrue(data["table_exists"])
        self.assertEqual(data["rows"], 1)
        self.assertEqual(data["grant"]["login_id"], self.me.login_id)
        self.assertEqual(data["grant"]["source"], "fresh")
        self.assertIn(config.MODE_WAREHOUSE, data["grant"]["allowed_modes"])

    def test_access_db_path_refuses_the_kanban_master(self):
        res = self.post("/api/access-db-path", {"path": str(self.shared), "password": "秘密"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("看板マスタ", res.json["error"]["message"])
        self.assertEqual(config.load_config(str(self.dir / "config.json")).access_db_path, "")

    def test_access_db_path_saves_and_creates_the_table(self):
        other = self.dir / "別" / "梱包資材マスタ.sqlite3"
        other.parent.mkdir()
        sqlite3.connect(str(other)).execute("CREATE TABLE t (a)").connection.close()
        self.assertEqual(self.post("/api/access-db-path", {"path": str(other)}).status_code, 401)
        res = self.post("/api/access-db-path", {"path": str(other), "password": "秘密"})
        self.assertEqual(res.status_code, 200, res.json)
        self.assertIn("作りました", res.json["message"])
        self.assertEqual(config.load_config(str(self.dir / "config.json")).access_db_path, str(other))
        # 空にすると既定(共有DBと同じフォルダ)へ戻る
        res = self.post("/api/access-db-path", {"path": "", "password": "秘密"})
        self.assertEqual(res.json["path"], str(self.packing))
        self.assertFalse(res.json["configured"])

    def test_settings_page_marks_modes_without_permission(self):
        self.app.config["ACCESS_GRANT"] = self.ac.resolve([], self.me)
        html = self.get("/settings").get_data(as_text=True)
        self.assertIn('id="access-card"', html)
        self.assertIn("(権限なし)", html)
        self.assertIn('id="access-path-card"', html)

    def test_startup_notice_is_shown_on_every_page(self):
        self.app.config["STARTUP_NOTICE"] = "倉庫モードの権限がないので、現場モードで開きました。"
        for path in ("/board", "/settings"):
            self.assertIn("現場モードで開きました", self.get(path).get_data(as_text=True), path)



class HistoryAndCsvRouteTest(RouteTestBase):
    """看板履歴.sqlite3 の置き場所・CSV の書き出し先・ファイルの場所の一覧。"""

    MODE = config.MODE_SITE

    def setUp(self) -> None:
        super().setUp()
        self.shared = self.dir / "看板マスタ.sqlite3"
        conn = sqlite3.connect(str(self.shared))
        conn.execute('CREATE TABLE "看板_LVC" ("管理番号" TEXT, "資材" TEXT, "サイズ" TEXT)')
        conn.execute("INSERT INTO 看板_LVC VALUES ('1', '外装紙', 'A')")
        conn.commit()
        conn.close()
        self.csv_dir = self.dir / "書き出し"
        self.csv_dir.mkdir()
        (self.dir / "config.json").write_text(json.dumps({
            "shared_db_path": str(self.shared), "admin_password_hash": config.hash_password("秘密"),
            "line": "LVC"}, ensure_ascii=False), encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {"KANBAN_LOCAL_DIR": str(self.dir / "local")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def cfg(self):
        return config.load_config(str(self.dir / "config.json"))

    def add_history(self, path):
        from kanban.db import history, sync

        db = history.HistoryDb(str(path))
        sync.ensure_history_table(db)
        conn = sqlite3.connect(str(path))
        conn.execute("INSERT INTO 看板履歴 (ID, 日時, ライン, 管理番号, 出来事, 資材, サイズ, 出した日時, 端末)"
                     " VALUES ('u1', '2026/09/01 09:00:00', 'LVC', '1', '出した', '外装紙', 'A',"
                     " '2026/09/01 09:00:00', 'PC-1')")
        conn.commit()
        conn.close()

    def test_status_of_the_default_place(self):
        data = self.get("/api/history-db-status").json
        self.assertEqual(data["path"], str(self.dir / "看板履歴.sqlite3"))
        self.assertFalse(data["configured"])
        self.assertFalse(data["exists"])
        self.assertEqual(data["problem"], "")

    def test_changing_the_place_needs_the_password_and_makes_the_file(self):
        target = self.dir / "別" / "看板履歴.sqlite3"
        target.parent.mkdir()
        self.assertEqual(self.post("/api/history-db-path", {"path": str(target)}).status_code, 401)
        from kanban.db import history, sync

        called = []

        def switch(path):   # 起動側の差し替え(start_app._change_history)の代わり
            called.append(path)
            sync.ensure_history_table(history.HistoryDb(path))

        self.app.config["SET_HISTORY_DB"] = switch
        res = self.post("/api/history-db-path", {"path": str(target), "password": "秘密"})
        self.assertEqual(res.status_code, 200, res.json)
        self.assertEqual(called, [str(target)], "この端末の送り先を差し替えていない")
        self.assertTrue(target.is_file())
        self.assertEqual(res.json["rows"], 0)
        self.assertEqual(self.cfg().history_db_path, str(target))
        # 空にすると既定へ
        res = self.post("/api/history-db-path", {"path": "", "password": "秘密"})
        self.assertEqual(res.json["path"], str(self.dir / "看板履歴.sqlite3"))
        self.assertEqual(self.cfg().history_db_path, "")

    def test_records_follow_to_the_new_place(self):
        """置き場所を変えたら、前の場所の記録も写す(まだ無い行だけ。前のファイルは残す)。"""
        self.add_history(self.dir / "看板履歴.sqlite3")
        target = self.dir / "別" / "看板履歴.sqlite3"
        target.parent.mkdir()
        res = self.post("/api/history-db-path", {"path": str(target), "password": "秘密"})
        self.assertEqual(res.status_code, 200, res.json)
        self.assertIn("1 件写しました", res.json["message"])
        self.assertEqual(res.json["rows"], 1)
        self.assertTrue((self.dir / "看板履歴.sqlite3").is_file(), "前のファイルを消した")

    def test_the_kanban_master_cannot_be_the_history_file(self):
        res = self.post("/api/history-db-path", {"path": str(self.shared), "password": "秘密"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("看板マスタ", res.json["error"]["message"])
        self.assertEqual(self.cfg().history_db_path, "")

    def test_stats_read_the_history_file(self):
        self.add_history(self.dir / "看板履歴.sqlite3")
        data = self.get("/api/stats?from=2026-09&to=2026-09").json
        self.assertEqual(data["cycle_count"], 1)
        self.assertEqual(data["history_path"], str(self.dir / "看板履歴.sqlite3"))

    def test_history_csv_download(self):
        self.add_history(self.dir / "看板履歴.sqlite3")
        res = self.get("/api/stats/csv?kind=history&from=2026-09&to=2026-09")
        self.assertEqual(res.status_code, 200)
        text = res.get_data().decode("utf-8-sig")
        self.assertTrue(text.startswith("日時,ライン,ライン名,管理番号,出来事"))
        self.assertIn("2026/09/01 09:00:00,LVC,", text)

    def test_csv_is_saved_to_the_folder_without_overwriting(self):
        self.add_history(self.dir / "看板履歴.sqlite3")
        self.assertEqual(self.post("/api/csv-dir", {"path": str(self.csv_dir)}).status_code, 401)
        res = self.post("/api/csv-dir", {"path": str(self.csv_dir), "password": "秘密"})
        self.assertEqual(res.status_code, 200, res.json)
        body = {"kind": "history", "from": "2026-09", "to": "2026-09"}
        first = self.post("/api/stats/csv/save", body)
        self.assertEqual(first.status_code, 200, first.json)
        path = Path(first.json["path"])
        self.assertEqual(path.parent, self.csv_dir)
        self.assertEqual(path.name, "看板履歴_2026-09_2026-09.csv")
        raw = path.read_bytes()
        self.assertTrue(raw.startswith("\ufeff".encode("utf-8")), "BOM が無いと Excel で化ける")
        self.assertIn("出した", raw.decode("utf-8-sig"))
        second = self.post("/api/stats/csv/save", body)
        self.assertNotEqual(second.json["path"], first.json["path"], "前のファイルを上書きした")
        self.assertEqual(len(list(self.csv_dir.glob("*.csv"))), 2)
        # 集計の CSV も同じ所へ
        res = self.post("/api/stats/csv/save", {"kind": "cycles", "from": "2026-09", "to": "2026-09"})
        self.assertEqual(Path(res.json["path"]).parent, self.csv_dir)

    def test_csv_goes_to_the_local_area_by_default(self):
        res = self.post("/api/stats/csv/save", {"kind": "summary"})
        self.assertEqual(res.status_code, 200, res.json)
        self.assertEqual(Path(res.json["path"]).parent, self.dir / "local" / "export")

    def test_csv_dir_must_be_an_existing_folder(self):
        res = self.post("/api/csv-dir", {"path": str(self.dir / "無い"), "password": "秘密"})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.cfg().csv_dir, "")
        self.assertEqual(self.post("/api/stats/csv/save", {"kind": "なに"}).status_code, 400)

    def test_settings_page_tells_shared_files_from_local_ones(self):
        html = self.get("/settings").get_data(as_text=True)
        for part in ("複数の PC で共有するファイル", "この PC だけに置いて引き継ぐファイル",
                     "看板履歴.sqlite3", 'id="history-path-card"', 'id="csv-dir-card"',
                     'data-csv-save="history"', "全端末で共有", "この端末だけ"):
            self.assertIn(part, html)
        places = self.get("/api/settings").json["places"]
        self.assertEqual([f["label"] for f in places["shared"]], ["看板マスタ(共有DB)", "看板履歴", "梱包資材マスタ"])
        self.assertIn("手元の SQLite", [f["label"] for f in places["local"]])
        self.assertIn("設定ファイル", [f["label"] for f in places["local"]])

if __name__ == "__main__":
    unittest.main()
