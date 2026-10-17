"""マスタ管理(テーブル)の看板の追加・削除と、現場の提出(発注)の関係

【利用者から】

    テーブルで看板の追加後、すぐ現場で提出
    テーブルで看板の削除後、提出した削除した看板がどうなるか
    そもそも消させないのか
    テーブル追加削除と提出の関連テストをしてください

共有DBは本番の形(Access を変換した型なしの表・管理番号は数)。現場 2 台 + 倉庫 1 台が、アプリと同じ
書き戻し(:class:`sync.Exporter`)と取り込み(:class:`sync.Importer`)で回ります。マスタ管理は画面と同じ
:mod:`kanban.presenters.master` の口(消すときは、その端末の未送信を ``unsent`` で渡す)。
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from kanban import config, keys
from kanban.db import sync
from kanban.db.shared import SharedDb
from kanban.db.store import ConflictError
from kanban.presenters import master
from tests import test_shared_shapes as shapes
from tests.test_shared_shapes import LINE, TABLE, Terminal, _norm

ON = config.MARK_ON


class MasterVsOrdersTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="kanban_master_orders_"))
        self.shared = shapes.SharedShapesTest.make_shared(self, self.root, "", lambda n: n, "", None,
                                                   "2026-10-16 08:32:00")
        self.t = {name: Terminal(self.root, name, self.shared) for name in ("現場PC1", "現場PC2", "倉庫PC")}
        sync.ensure_comment_table(self.t["倉庫PC"].gateway)
        self.ex = {n: sync.Exporter(t.store, t.gateway, interval_sec=0) for n, t in self.t.items()}
        self.im = {n: sync.Importer(t.store, t.gateway, [LINE], interval_sec=0) for n, t in self.t.items()}
        self.cycle()

    def tearDown(self) -> None:
        for t in self.t.values():
            t.close()
        shutil.rmtree(self.root, ignore_errors=True)

    # -- 道具 ----------------------------------------------------------------
    def cycle(self) -> None:
        """全端末が 1 回ずつ送って、1 回ずつ取り込む(アプリの周期 1 回ぶん)。"""
        for name, ex in self.ex.items():
            result = ex.run_once()
            self.t[name].errors += result.errors if result else []
        for name, im in self.im.items():
            self.t[name].gateway.forget_copy()
            im.run_once()

    def press(self, name: str, action: str, no: str, **kw) -> None:
        t = self.t[name]
        getattr(t.svc, action)(LINE, no, expected_rev=t.item(no).rev, **kw)

    def admin(self, name: str = "倉庫PC") -> SharedDb:
        """マスタ管理を開いた端末の共有DBの口(画面を開くたびに読み直す)。"""
        gw = self.t[name].gateway
        gw.forget_copy()
        return gw

    def add(self, no: str, material: str = "中性紙", size: str = "1000 × 100M:4本", *, name: str = "倉庫PC"):
        return master.insert_row(self.admin(name), TABLE, {config.COL_KEY: no, config.COL_MATERIAL: material,
                                                           config.COL_SIZE: size, config.COL_PERMANENT: ON})

    def row_key(self, gw: SharedDb, no: str) -> str:
        """画面が送る行の印(マスタ管理の表の隠し列 = 共有DBの rowid)。"""
        view = master.open_table(gw, TABLE)
        row = next(r for r in view.rows if keys.same(r.get(config.COL_KEY), no))
        return str(row[master.ROW_KEY])

    def delete(self, no: str, *, name: str = "倉庫PC", fresh: bool = True):
        """画面の「消す」。押した端末の未送信を渡す(app/routes/settings.py と同じ)。"""
        gw = self.admin(name) if fresh else self.t[name].gateway
        store = self.t[name].store
        return master.delete_row(gw, TABLE, self.row_key(gw, no), expect_key=no,
                                 unsent=lambda key: store.has_unsent(LINE, key))

    def shared_row(self, no: str) -> dict | None:
        conn = sqlite3.connect(str(self.shared))
        conn.row_factory = sqlite3.Row
        try:
            return next((dict(r) for r in conn.execute(f"SELECT * FROM [{TABLE}]")
                         if keys.same(r[config.COL_KEY], no)), None)
        finally:
            conn.close()

    def on_board(self, name: str, no: str) -> bool:
        return any(keys.same(i.mgmt_no, no) for i in self.t[name].store.items(LINE))

    def states(self, no: str) -> dict[str, tuple]:
        out = {}
        for n, t in self.t.items():
            item = t.item(no) if self.on_board(n, no) else None
            out[n] = (item.want, item.shipped, item.hold) if item else None
        row = self.shared_row(no)
        out["共有DB"] = (_norm(row["欲"]), _norm(row["発送"]), _norm(row["保留"])) if row else None
        return out

    def assert_all(self, no: str, want) -> None:
        got = self.states(no)
        self.assertTrue(all(v == want for v in got.values()), f"{no}: {got}")

    def errors(self) -> list[str]:
        return [e for t in self.t.values() for e in t.errors]

    # ======================================================================
    # 1. テーブルで看板を足して、すぐ現場で提出
    # ======================================================================
    def test_足した看板を次の取り込みで現場が提出し_倉庫まで回る(self) -> None:
        res = self.add("5")
        self.assertTrue(res.ok, res.message)
        # 足した直後、現場の手元にはまだ無い(次の取り込みで来る)。押そうとしても看板が無いと断る
        self.assertFalse(self.on_board("現場PC1", "5"))
        with self.assertRaises(ConflictError):
            self.t["現場PC1"].svc.toggle_order(LINE, "5")
        self.cycle()                                   # 取り込み 1 回で全端末に出る
        for name in self.t:
            self.assertTrue(self.on_board(name, "5"), name)
        self.assert_all("5", ("", "", ""))
        # すぐ提出 → 倉庫に届く → 発送 → 現場が届いた確認
        self.press("現場PC1", "toggle_order", "5")
        self.cycle()
        self.assert_all("5", (ON, "", ""))
        self.press("倉庫PC", "toggle_ship", "5")
        self.cycle()
        self.assert_all("5", (ON, ON, ""))
        self.press("現場PC2", "toggle_order", "5", treat_as_delivered=True)
        self.cycle()
        self.assert_all("5", ("", "", ""))
        self.assertEqual(self.errors(), [])
        self.assertEqual(sum(t.store.pending_count() for t in self.t.values()), 0)

    def test_足した直後に別の端末でも足す_同じ番号は2行にならない(self) -> None:
        self.assertTrue(self.add("6", name="倉庫PC").ok)
        again = self.add("6", name="現場PC1")
        self.assertFalse(again.ok)
        self.assertIn(again.reason, ("duplicate_key", "duplicate_kanban"))
        conn = sqlite3.connect(str(self.shared))
        try:
            count = sum(1 for (k,) in conn.execute(f"SELECT 管理番号 FROM [{TABLE}]") if keys.same(k, "6"))
        finally:
            conn.close()
        self.assertEqual(count, 1)

    # ======================================================================
    # 2. 提出(発注)中・発送済み・注文中の看板は消させない
    # ======================================================================
    def test_提出中_発送済み_注文中は消せない_片付けたら消せる(self) -> None:
        self.press("現場PC1", "toggle_order", "2")
        self.cycle()
        res = self.delete("2")
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "in_use")
        self.assertIn("発注中", res.message)
        self.press("倉庫PC", "toggle_hold", "2")      # 注文中(黄)
        self.cycle()
        res = self.delete("2")
        self.assertFalse(res.ok)
        self.assertIn("注文中", res.message)
        self.press("倉庫PC", "toggle_hold", "2")
        self.press("倉庫PC", "toggle_ship", "2")      # 発送済み(緑)
        self.cycle()
        res = self.delete("2")
        self.assertFalse(res.ok)
        self.assertIn("発送", res.message)
        self.assertIsNotNone(self.shared_row("2"), "断ったのに消えた")
        # 現場が届いた確認をして片付いたら消せる → 全端末の盤から消える
        self.press("現場PC1", "toggle_order", "2", treat_as_delivered=True)
        self.cycle()
        res = self.delete("2")
        self.assertTrue(res.ok, res.message)
        self.cycle()
        self.assertIsNone(self.shared_row("2"))
        for name in self.t:
            self.assertFalse(self.on_board(name, "2"), name)
        # 消したあと、古い画面のまま押しても書き込まない(看板が無いと断る)
        with self.assertRaises(ConflictError):
            self.t["現場PC2"].svc.toggle_order(LINE, "2")
        self.assertEqual(self.errors(), [])

    def test_この端末から送っていない提出がある看板は消せない(self) -> None:
        self.press("現場PC1", "toggle_order", "1")    # まだ送っていない
        res = self.delete("1", name="現場PC1")
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "in_use")
        self.assertIn("まだ共有DBへ届いていない", res.message)
        self.cycle()
        self.assert_all("1", (ON, "", ""))

    def test_消す画面を開いたあとに提出が届いても消さない(self) -> None:
        gw = self.admin("倉庫PC")
        gw.read_table(TABLE)                           # 消す前に表を読んだ(発注はまだ無い)
        self.press("現場PC1", "toggle_order", "1")
        self.ex["現場PC1"].run_once()                  # 提出が共有DBに届いた
        res = self.delete("1", fresh=False)            # 読んだ写しのまま消そうとする
        self.assertFalse(res.ok)
        self.assertIsNotNone(self.shared_row("1"), "消す直前に届いた提出ごと消した")
        self.cycle()
        self.assert_all("1", (ON, "", ""))

    # ======================================================================
    # 3. ほかの端末が送る前に消された(行き違い)
    # ======================================================================
    def test_ほかの端末が送る前に消されると_提出は届かず現場に知らせる(self) -> None:
        self.press("現場PC1", "toggle_order", "1")    # 現場PC1 はまだ送っていない
        res = self.delete("1", name="倉庫PC")          # 倉庫では空の看板に見えるので消せる
        self.assertTrue(res.ok, res.message)
        self.cycle()
        self.cycle()
        self.assertIsNone(self.shared_row("1"), "消した看板が戻ってきた")
        for name in self.t:
            self.assertFalse(self.on_board(name, "1"), name)
        site = self.t["現場PC1"].store
        self.assertEqual(site.pending_count(), 0, "送り先の無い操作を預かり続けている")
        # 黙って捨てない: 現場の画面に出す知らせが残る
        notes = site.dropped_notices()
        self.assertEqual([(n["line"], n["mgmt_no"]) for n in notes], [(LINE, "1")])
        self.assertIn("発注", notes[0]["what"])
        # ほかの看板の提出は今までどおり回る
        self.press("現場PC1", "toggle_order", "2")
        self.cycle()
        self.assert_all("2", (ON, "", ""))

    # ======================================================================
    # 4. 消して同じ番号で足し直す
    # ======================================================================
    def test_消して同じ番号で足し直すと前の状態は残らない(self) -> None:
        self.press("現場PC1", "toggle_order", "4")
        self.cycle()
        self.press("現場PC1", "toggle_order", "4")    # 取り消して片付ける
        self.cycle()
        self.assertTrue(self.delete("4").ok)
        self.cycle()
        self.assertTrue(self.add("4", material="HB", size="2000 × 50M:1本").ok)
        self.cycle()
        for name, t in self.t.items():
            item = t.item("4")
            self.assertEqual((item.material, item.size), ("HB", "2000 × 50M:1本"), name)
            self.assertEqual((item.want, item.shipped, item.hold), ("", "", ""), name)
        self.press("現場PC2", "toggle_order", "4")
        self.cycle()
        self.assert_all("4", (ON, "", ""))
        self.assertEqual(self.errors(), [])


if __name__ == "__main__":
    unittest.main()


class DroppedNoticeScreenTest(MasterVsOrdersTest):
    """行き違いで届かなかった提出を、現場の看板の画面(帯)に出す。経路と本物のブラウザで。"""

    # 親の試験はここでは流さない
    for _name in [n for n in dir(MasterVsOrdersTest) if n.startswith("test_")]:
        locals()[_name] = None
    del _name

    def make_lost_order(self) -> None:
        self.press("現場PC1", "toggle_order", "1")
        self.assertTrue(self.delete("1", name="倉庫PC").ok)
        self.cycle()

    def app_for(self, name: str):
        from app import create_app
        from kanban import admin_lock

        admin_lock.reset()
        self.addCleanup(admin_lock.reset)
        app = create_app(config.MODE_SITE, token="test-token", port=8741,
                         store=self.t[name].store, line=LINE)
        app.config["CONFIG_PATH"] = str(self.root / f"config_{name}.json")
        return app.test_client()

    def test_経路_帯に出して_わかったで消える(self) -> None:
        self.make_lost_order()
        client = self.app_for("現場PC1")
        headers = {"X-Tool-Token": "test-token"}
        status = client.get("/api/status", headers=headers).get_json()
        self.assertEqual([(d["mgmt_no"], d["what"]) for d in status["dropped"]], [("1", "発注")])
        board = client.get(f"/api/board?line={LINE}", headers=headers).get_json()
        self.assertEqual(len(board["dropped"]), 1)
        self.assertEqual(client.post("/api/dropped/clear", json={}, headers=headers).status_code, 200)
        self.assertEqual(client.get("/api/status", headers=headers).get_json()["dropped"], [])
        # 現場PC2(押していない端末)には出ない
        other = self.app_for("現場PC2").get("/api/status", headers=headers).get_json()
        self.assertEqual(other["dropped"], [])

    def test_画面_帯を見て_わかったを押す(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("playwright が無い")
        import os

        self.make_lost_order()
        client = self.app_for("現場PC1")

        def forward(route) -> None:
            req = route.request
            path = req.url.split("http://kanban.test", 1)[1]
            res = client.open(path, method=req.method, headers={"X-Tool-Token": "test-token",
                                                                  "Content-Type": req.headers.get("content-type", "")},
                              data=req.post_data_buffer)
            route.fulfill(status=res.status_code, body=res.get_data(),
                          headers={"content-type": res.headers.get("Content-Type", "text/plain")})

        with sync_playwright() as pw:
            exe = "/opt/pw-browsers/chromium"
            launch = {"executable_path": exe} if os.path.exists(exe) else {}
            try:
                browser = pw.chromium.launch(**launch)
            except Exception as exc:                      # noqa: BLE001
                self.skipTest(f"ブラウザを起こせない: {exc}")
            try:
                page = browser.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.route("http://kanban.test/**", forward)
                page.goto("http://kanban.test/board", wait_until="load")
                page.wait_for_selector("#dropped:not([hidden])", timeout=8000)
                text = page.text_content("#dropped-text")
                self.assertIn("マスタ管理で消された看板への操作は届きませんでした", text)
                self.assertIn(f"{LINE} 1番", text)
                self.assertIn("発注", text)
                page.click("#dropped-ok")
                page.wait_for_selector("#dropped", state="hidden", timeout=5000)
                self.assertEqual(self.t["現場PC1"].store.dropped_notices(), [])
                self.assertEqual(errors, [])
            finally:
                browser.close()


class DroppedWhatTest(unittest.TestCase):
    """帯に出す「何が届かなかったか」。発注は発送の列も一緒に書くので、発送とは言わない。"""

    def what(self, cols: str, **values) -> str:
        from kanban.db.store import _dropped_what
        return _dropped_what({"dirty_columns": cols, **values})

    def test_names(self) -> None:
        self.assertEqual(self.what("want,unwant,ordered_at,shipped,confirmed_at", want=ON), "発注")
        self.assertEqual(self.what("want,unwant,ordered_at,shipped", want=""), "発注の取り消し・届いた確認")
        self.assertEqual(self.what("shipped,confirmed_at", shipped=ON), "発送")
        self.assertEqual(self.what("shipped", shipped=""), "発送の取り消し")
        self.assertEqual(self.what("hold,hold_at", hold=ON), "注文中")
        self.assertEqual(self.what("confirmed_at"), "確認")
        self.assertEqual(self.what(""), "操作")
