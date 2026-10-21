"""設定画面の面(中身の入れ替え・看板集計・この端末の権限・記録)と、看板の画面の
「⇄ 〜モードへ」を**本物のブラウザで押して**確かめる(段階1)。

以前は ``/settings``・``/board`` の HTML に ``id="rf-run"``・``data-csv="cycles"``・
``(権限なし)``・``data-switch-mode`` などの字があるかを見ていた(`test_web_app` の
``*_has_the_card`` / ``*_has_the_tab`` など)。字が残っていれば、押しても何も起きない
画面でも緑になる。ここでは利用者と同じに押して、**画面に出たもの・サーバに届いたもの**を見る。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kanban import config
from tests._kanban_browser import KanbanBrowserCase
from tests.test_web_app import sample_rows


def _local_dir(case) -> None:
    """控え・CSV などの手元領域を、この試験の中へ。"""
    patcher = mock.patch.dict(os.environ, {"KANBAN_LOCAL_DIR": str(case.dir / "local")})
    patcher.start()
    case.addCleanup(patcher.stop)


def _write_config(case, **values) -> None:
    (case.dir / "config.json").write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")


class RefreshCardTest(KanbanBrowserCase):
    """「Access の最新で表の中身を入れ替える」(マスタ管理の面)。ファイルを落として、選んで、入れ替える。"""

    COLS = "[管理番号] NUMERIC, [資材] TEXT, [サイズ] TEXT, [欲] TEXT, [不] TEXT, [発送] TEXT"

    def setUp(self) -> None:
        super().setUp()
        from kanban import table_refresh

        self.addCleanup(table_refresh._cache.clear)
        self.shared = self.dir / "看板マスタ.sqlite3"
        c = sqlite3.connect(str(self.shared))
        c.execute(f"CREATE TABLE [看板_LVC] ({self.COLS})")
        c.execute("INSERT INTO [看板_LVC] VALUES (1, '外装紙', 'A', '〇', '', '')")
        c.commit()
        c.close()
        self.src = self.dir / "最新.sqlite3"
        c = sqlite3.connect(str(self.src))
        c.execute(f"CREATE TABLE [看板_LVC] ({self.COLS})")
        c.executemany("INSERT INTO [看板_LVC] VALUES (?, ?, ?, '', '〇', '')", [(1, "外装紙X", "A"), (2, "テープ", "B")])
        c.commit()
        c.close()
        _write_config(self, shared_db_path=str(self.shared), admin_password_hash=config.hash_password("秘密"),
                      line="LVC")
        _local_dir(self)

    def shared_rows(self) -> list[tuple]:
        c = sqlite3.connect(str(self.shared))
        try:
            return c.execute("SELECT 管理番号, 資材, 欲 FROM [看板_LVC] ORDER BY 管理番号").fetchall()
        finally:
            c.close()

    def test_ファイルを落として表を選ぶと入れ替えられ_状態は残る(self) -> None:
        page = self.open_settings("master")
        run = page.locator("#rf-run")
        self.assertTrue(run.is_disabled(), "読む前から入れ替えを押せる")

        page.set_input_files("#rf-file", str(self.src))
        self.answer_password(page)
        box = page.locator('#rf-table input[data-table="看板_LVC"]')
        box.wait_for()
        self.assertEqual(self.posted("/api/table-refresh/plan"), [], "落としたのに場所で読みに行った")
        self.assertTrue(run.is_disabled(), "選ぶ前から押せる")

        box.check()
        page.wait_for_function("!document.getElementById('rf-run').disabled")
        self.assertIn("(1)", run.inner_text(), "選んだ数が出ない")
        run.click()
        page.wait_for_selector("#rf-result:not([hidden])")
        self.assertIn("✔", self.text(page, "#rf-result"), self.text(page, "#rf-result"))
        self.assertTrue(any("入れ替えます" in d for d in self.dialogs), self.dialogs)
        self.assertEqual(self.shared_rows(), [(1, "外装紙X", "〇"), (2, "テープ", "")],
                         "中身が入れ替わっていない(または状態が消えた)")
        self.assertEqual(self.errors, [])


class StatsTabTest(KanbanBrowserCase):
    """看板集計の面。開くと読み、CSV のリンクとフォルダへの「保存」が使える。"""

    def setUp(self) -> None:
        super().setUp()
        from kanban.db import sync
        from kanban.db.shared import SharedDb
        from kanban.domain import models

        self.shared = self.dir / "看板マスタ.sqlite3"
        conn = sqlite3.connect(str(self.shared))
        conn.execute("CREATE TABLE [看板_LVC] ([管理番号] NUMERIC, [資材] TEXT, [サイズ] TEXT)")
        conn.executemany("INSERT INTO [看板_LVC] VALUES (?, ?, ?)", [(1, "外装紙", "A"), (2, "外装紙", "B")])
        conn.commit()
        conn.close()
        _write_config(self, shared_db_path=str(self.shared), line="LVC")
        _local_dir(self)
        # 押した記録を 1 件、看板履歴へ届けておく
        no = sample_rows()[0]["mgmt_no"]
        self.store.apply_transition("LVC", no, models.order_button_changes, operation="order")
        gateway = SharedDb(str(self.shared), cache_dir=str(self.dir / "cache"))
        self.assertGreater(sync.export_events(self.store, gateway), 0)

    def test_面を開くと集計し_CSVのリンクと保存が使える(self) -> None:
        page = self.open_settings()
        self.assertEqual([p for _, p, _ in self.sent if p.startswith("/api/stats?")], [], "開く前から集計した")
        page.click("#tab-stats")
        page.wait_for_function("[...document.querySelectorAll('[data-csv]')].every((a) => a.href.includes('kind='))")
        kinds = page.evaluate("[...document.querySelectorAll('[data-csv]')].map((a) => a.dataset.csv)")
        self.assertLessEqual({"cycles", "weekly", "monthly", "lead", "summary", "rate"}, set(kinds))
        got = page.evaluate("""async () => {
          const out = {};
          for (const a of document.querySelectorAll('[data-csv]')) {
            const r = await fetch(a.href);
            out[a.dataset.csv] = [r.status, r.headers.get('content-type') || ''];
          }
          return out;
        }""")
        for kind, (status, kind_type) in got.items():
            with self.subTest(kind=kind):
                self.assertEqual(status, 200, f"{kind} の CSV が落とせない")
                self.assertIn("text/csv", kind_type)

        with self.subTest("保存: 書き出し先のフォルダへ"):
            page.click('[data-csv-save="history"]')
            page.wait_for_selector("#csv-result:not([hidden])")
            self.assertIn("✔ 保存しました", self.text(page, "#csv-result"))
            self.assertEqual([b.get("kind") for b in self.posted("/api/stats/csv/save")], ["history"])
            saved = self.text(page, "#csv-result").split("保存しました:", 1)[1].strip()
            self.assertTrue(Path(saved).is_file(), saved)
        self.assertEqual(self.errors, [])


class _Access:
    """アクセス権限(梱包資材マスタ)のある端末。"""

    MODE = config.MODE_SITE

    def setUp(self) -> None:
        super().setUp()
        from kanban import access_control as ac

        self.ac = ac
        self.me = ac.current_identity()
        self.shared = self.dir / "看板マスタ.sqlite3"
        conn = sqlite3.connect(str(self.shared))
        conn.execute('CREATE TABLE "看板_LVC" ("管理番号" TEXT, "資材" TEXT)')
        conn.commit()
        conn.close()
        self.packing = self.dir / "梱包資材マスタ.sqlite3"
        conn = sqlite3.connect(str(self.packing))
        conn.execute('CREATE TABLE "BoardMaster" ("ID" INTEGER, "名前" TEXT)')
        conn.commit()
        conn.close()
        _write_config(self, shared_db_path=str(self.shared), admin_password_hash=config.hash_password("秘密"),
                      line="LVC")
        _local_dir(self)

    def grant_admin(self) -> None:
        """この端末に Administrator の行を足す(マスタ管理で足したのと同じ)。"""
        self.get("/api/master")   # 表が無ければ作る
        res = self.post("/api/master/insert", {"table": "アクセス権限", "db": "access", "password": "秘密", "values": {
            "管理番号": "1", "ログインID": self.me.login_id, "PC名": self.me.pc_name,
            "権限": "Administrator", "有効": "1", "備考": ""}})
        self.assertEqual(res.status_code, 200, res.json)


class AccessCardTest(_Access, KanbanBrowserCase):
    @staticmethod
    def option(page, key: str) -> tuple[bool, str]:
        return page.evaluate("(k) => { const o = document.querySelector(`#mode option[value='${k}']`);"
                             " return [o.disabled, o.textContent.trim()]; }", key)

    def test_権限の無いモードは選べず_行を足して読み直すと選べる(self) -> None:
        self.app.config["ACCESS_GRANT"] = self.ac.resolve([], self.me)
        page = self.open_settings()
        page.wait_for_function("document.querySelectorAll('#access-modes li').length > 0")
        disabled, label = self.option(page, config.MODE_WAREHOUSE)
        self.assertTrue(disabled, "権限の無いモードを選べる")
        self.assertIn("(権限なし)", label)
        self.assertIn("権限なし", page.locator("#access-modes li.is-no").first.inner_text())

        self.grant_admin()
        page.click("#access-reload")
        page.wait_for_function("(k) => !document.querySelector(`#mode option[value='${k}']`).disabled",
                               arg=config.MODE_WAREHOUSE)
        self.assertNotIn("(権限なし)", self.option(page, config.MODE_WAREHOUSE)[1], "読み直しても付いたまま")

        page.select_option("#mode", config.MODE_WAREHOUSE)
        page.wait_for_selector("#mode-notice:not([hidden])")
        self.assertIn("次にアプリを開いたときから", self.text(page, "#mode-notice"))
        self.assertEqual(self.posted("/api/mode"), [{"mode": config.MODE_WAREHOUSE}])
        self.assertEqual(self.errors, [])


class ModeSwitchTest(_Access, KanbanBrowserCase):
    """Administrator は看板の画面から倉庫 ⇔ 現場へ切り替えられる(設定の奥まで行かない)。"""

    def test_看板の画面から権限のあるほかのモードへ切り替える(self) -> None:
        with self.subTest("現場だけの端末には何も出さない(倉庫参照へも)"):
            self.app.config["ACCESS_GRANT"] = self.ac.resolve([], self.me)
            page = self.open("/board")
            page.wait_for_selector("#board")
            self.assertEqual(page.locator("[data-switch-mode]").count(), 0)

        self.grant_admin()
        self.app.config["ACCESS_GRANT"] = self.ac.grant_for(config.load_config(self.dir / "config.json"), self.store)
        from kanban import screen

        screen.reset()
        page = self.open("/board", page)
        button = page.get_by_role("button", name="⇄ 倉庫モードへ")
        self.assertTrue(button.is_visible(), "権限があるのに切り替えのボタンが無い")
        self.assertEqual(page.locator("[data-switch-mode]").count(), 1, "いまのモードまで出している")
        button.click()
        self.wait_toast(page, "次にアプリを開いたときから")
        self.assertEqual(self.posted("/api/mode"), [{"mode": config.MODE_WAREHOUSE}])
        self.assertEqual(self.errors, [])


class TraceTabTest(KanbanBrowserCase):
    """「記録」の面。開くと探し、行を押すか記録番号を入れると、なぜなぜ分析シートの下書きが出る。"""

    def setUp(self) -> None:
        from kanban import applog, trace

        self.logdir = Path(tempfile.mkdtemp(prefix="kanban_trace_browser_"))
        super().setUp()
        self.addCleanup(shutil.rmtree, self.logdir, True)
        saved = (trace._dir, dict(trace._context))

        def restore() -> None:
            applog.close()
            trace._dir = saved[0]
            trace._context.clear()
            trace._context.update(saved[1])

        self.addCleanup(restore)
        applog.initialize(str(self.logdir), line_name="LVC")
        trace.set_context(pc="PC-ONE", login="yamada", mode="site", line="LVC")
        _write_config(self, admin_password_hash=config.hash_password("秘密"), line="LVC", log_dir=str(self.logdir))

    def refused(self) -> str:
        res = self.post("/api/line", {"line": "LS", "password": "ちがう"})
        self.assertEqual(res.status_code, 401)
        return res.json["error"]["ref"]

    def test_面を開くと探し_行を押すか番号を入れると下書きが出る(self) -> None:
        first = self.refused()
        page = self.open_settings()
        page.click("#tab-trace")
        row = page.locator("#trace-rows tr", has_text=first)
        row.wait_for()

        with self.subTest("一覧の行を押す"):
            row.click()
            page.wait_for_function("(r) => document.getElementById('trace-sheet').value.includes(r)", arg=first)
            sheet = page.input_value("#trace-sheet")
            self.assertIn("なぜなぜ分析シート", sheet)
            self.assertIn(first, self.text(page, "#trace-detail-title"))

        with self.subTest("記録番号を入れて Enter"):
            second = self.refused()
            page.fill("#trace-ref", second)
            page.press("#trace-ref", "Enter")
            page.wait_for_function("(r) => document.getElementById('trace-sheet').value.includes(r)", arg=second)
            self.assertIn(second, self.text(page, "#trace-detail-title"))
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
