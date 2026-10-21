"""設定画面「ファイルの場所」は、置き場所を変えたら**その場で描き直す**(本物のブラウザで。段階1)。

設定画面は開いたときにサーバが描くので、以前は接続先などを変えても、読み直すまで
前の場所のままだった(読み直しが見送られると、ずっと古いまま)。以前は
``views/settings.js`` の関数の中に ``refreshPlaces();`` の字があるかを見ていた
(`test_theme.PlacesRefreshTest`)。ここでは利用者と同じに変えて、**読み直さずに**
「この端末について」の一覧が新しい場所になることを見る。
"""
from __future__ import annotations

import json
import os
import sqlite3
import unittest
from unittest import mock

from kanban import config
from tests._kanban_browser import KanbanBrowserCase


class PlacesFollowTest(KanbanBrowserCase):
    def setUp(self) -> None:
        super().setUp()
        from kanban import applog, trace

        # 記録の置き場所を変えると、プロセスの記録の書き先が切り替わる。終わったら戻す
        saved = (trace._dir, dict(trace._context))

        def restore() -> None:
            applog.close()
            trace._dir = saved[0]
            trace._context.clear()
            trace._context.update(saved[1])

        self.addCleanup(restore)
        self.shared = self.dir / "看板マスタ.sqlite3"
        self.make_db(self.shared)
        (self.dir / "config.json").write_text(json.dumps({
            "shared_db_path": str(self.shared), "admin_password_hash": config.hash_password("秘密"),
            "line": "LVC"}, ensure_ascii=False), encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {"KANBAN_LOCAL_DIR": str(self.dir / "local")})
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def make_db(path) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        conn.execute('CREATE TABLE IF NOT EXISTS "看板_LVC" ("管理番号" TEXT, "資材" TEXT)')
        conn.commit()
        conn.close()
        return str(path)

    @staticmethod
    def places(page, group: str) -> dict[str, str]:
        """一覧のその組の {もの: 場所}(隠れた面でも中身を読む)。"""
        return page.evaluate(
            """(g) => Object.fromEntries([...document.querySelectorAll(`[data-places="${g}"] tbody tr`)]
                 .map((tr) => [tr.cells[0].textContent.trim(), tr.cells[1].textContent.trim()]))""",
            group)

    def wait_place(self, page, group: str, label: str, path: str) -> None:
        self.wait_until(page, lambda: self.places(page, group).get(label) == path,
                        f"「{label}」が {path}(いま {self.places(page, group).get(label)})")

    def change(self, page, tab: str, field: str, button: str, value: str) -> None:
        page.click(f"#tab-{tab}")
        page.fill(f"#{field}", value)
        page.click(f"#{button}")

    def test_置き場所を変えたら_読み直さずに一覧を描き直す(self) -> None:
        new_shared = self.make_db(self.dir / "新しい接続先" / "看板マスタ.sqlite3")
        history = self.dir / "履歴の場所" / "看板履歴.sqlite3"
        history.parent.mkdir()
        packing = self.dir / "権限の場所" / "梱包資材マスタ.sqlite3"   # 看板の表は持たない(看板マスタと取り違えない)
        packing.parent.mkdir()
        sqlite3.connect(str(packing)).execute("CREATE TABLE t (a)").connection.close()
        packing = str(packing)
        logs = self.dir / "記録の場所"
        logs.mkdir()
        csv = self.dir / "書き出し"
        csv.mkdir()

        page = self.open_settings()
        self.assertEqual(self.places(page, "shared")["看板マスタ(共有DB)"], str(self.shared))
        self.assertTrue(self.places(page, "app"), "アプリのフォルダの組が無い")
        # 共有するものとこの端末だけのものを、組の見出しで言い分ける
        heads = page.evaluate("""(gs) => gs.map((g) => document.querySelector(`[data-places="${g}"]`)
                                   .closest('section').querySelector('h2').textContent)""", ["shared", "local"])
        self.assertIn("全端末で共有", heads[0])
        self.assertIn("この端末だけ", heads[1])
        page.evaluate("window.__same = true")   # 読み直したら消える印

        with self.subTest("接続先(共有DB)。既定が付いていく看板履歴・梱包資材マスタも"):
            self.change(page, "source", "accdb", "save-accdb", new_shared)
            self.answer_password(page)
            self.wait_place(page, "shared", "看板マスタ(共有DB)", new_shared)
            folder = self.dir / "新しい接続先"
            self.wait_place(page, "shared", "看板履歴", str(folder / config.HISTORY_DB_NAME))
            self.wait_place(page, "shared", "梱包資材マスタ", str(folder / config.ACCESS_DB_NAME))
        # ここから先は認証済み(確認画面で入れると開く)なので訊かない
        cases = (
            ("看板履歴", "shared", "source", "history-path", "history-save", str(history)),
            ("梱包資材マスタ", "shared", "source", "access-path", "access-save", packing),
            ("ログ・記録", "local", "trace", "log-dir", "log-save", str(logs)),
            ("CSV の書き出し先", "local", "source", "csv-dir", "csv-save", str(csv)),
        )
        for label, group, tab, field, button, value in cases:
            with self.subTest(label):
                self.change(page, tab, field, button, value)
                self.wait_place(page, group, label, value)
        self.assertTrue(page.evaluate("window.__same === true"), "読み直して描いていた")
        self.assertFalse(self.visible(page, "#pw"), "認証済みなのにまた訊いた")

        with self.subTest("読み直しても新しい場所(サーバも設定を読み直して描く)"):
            page.reload(wait_until="load")
            page.wait_for_function("(document.getElementById('trace-from') || {}).value")
            got = self.places(page, "shared")
            self.assertEqual((got["看板マスタ(共有DB)"], got["看板履歴"], got["梱包資材マスタ"]),
                             (new_shared, str(history), packing))
            local = self.places(page, "local")
            self.assertEqual((local["ログ・記録"], local["CSV の書き出し先"]), (str(logs), str(csv)))
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
