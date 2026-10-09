"""看板の状態の列と「動いている看板か」は kanban/domain/state.py の 1 か所で決める

以前はマスタ管理・中身の入れ替え・画面(JS)に別々に書かれていて、状態の列を 1 つ足すのに
約 12 か所を直すことになっていた。写しが戻ってきたら落ちる。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from kanban import table_refresh
from kanban.db.store import DEFAULT_COLUMN_MAP
from kanban.domain import models, state
from kanban.presenters import master

ROOT = Path(__file__).resolve().parent.parent


class StateSingleSourceTest(unittest.TestCase):
    def test_状態の列はstateの1つだけ(self):
        self.assertIs(master.STATE_COLUMNS, state.STATE_COLUMNS)
        self.assertIs(table_refresh.STATE_COLUMNS, state.STATE_COLUMNS)
        self.assertIs(master.KANBAN_INITIAL_STATE, state.INITIAL_STATE)
        self.assertIs(table_refresh.INITIAL_STATE, state.INITIAL_STATE)

    def test_手元の論理名と共有DBの列名が対応する(self):
        """手元の SQLite の状態の列(MUTABLE_FIELDS)と、共有DBの状態の列が同じ並び。"""
        self.assertEqual(tuple(DEFAULT_COLUMN_MAP[f] for f in models.MUTABLE_FIELDS), state.STATE_COLUMNS)

    def test_動いている看板の判定(self):
        row = {"欲": "〇", "保留": " 〇 ", "発送": ""}
        self.assertTrue(state.is_active(row))
        self.assertEqual(state.active_labels(row), ["発注中", "注文中"])
        self.assertEqual(master._busy_state(row), "発注中(赤)・注文中(黄)")
        self.assertEqual(table_refresh._active(row), True)
        self.assertFalse(state.is_active({"欲": "", "不": "〇"}))

    def test_画面へ渡す形に列と印が入る(self):
        data = master.to_dict(master.MasterView())
        self.assertEqual(data["kanban_state"]["mark_on"], "〇")
        self.assertEqual([a["column"] for a in data["kanban_state"]["active"]], list(state.ACTIVE_COLUMNS))

    def test_JSに状態の列名と印を書き写さない(self):
        js = (ROOT / "app" / "static" / "js" / "views" / "settings.js").read_text(encoding="utf-8")
        body = js[js.index("async function deleteRow"):]
        body = body[:body.index("\n}\n")]
        self.assertNotRegex(body, r"'(欲|保留|発送|〇)'", "JS に列名・印を書き写している")
        self.assertIn("kanban_state", body)


if __name__ == "__main__":
    unittest.main()
