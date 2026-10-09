"""管理番号のそろえ方 (kanban/keys.py) ── 比べる場所が全部同じ答えを出す。"""
from __future__ import annotations

import unittest

from kanban import keys
from kanban import table_refresh
from kanban.presenters import master

SAME_AS_ONE = [1, 1.0, "1", "1.0", "１", " 1 ", "+1", "01", "1.00"]


class KeysTest(unittest.TestCase):
    def test_同じ看板の書き方はすべて同じ形になる(self):
        for value in SAME_AS_ONE:
            with self.subTest(value=value):
                self.assertEqual(keys.normalize(value), "1")

    def test_番号でない管理番号は空白だけ落とす(self):
        self.assertEqual(keys.normalize(" A-12 "), "A-12")
        self.assertEqual(keys.normalize("ＡＢ１"), "AB1")
        self.assertEqual(keys.normalize(None), "")

    def test_小数は数として比べる(self):
        self.assertTrue(keys.same("1.50", 1.5))
        self.assertFalse(keys.same("1.5", "2"))
        self.assertFalse(keys.same("A", "B"))

    def test_比べる場所が同じ答えを出す(self):
        """中身の入れ替え・マスタ管理(同じか・あるか)が食い違わない。"""
        for value in SAME_AS_ONE:
            with self.subTest(value=value):
                self.assertEqual(table_refresh._key(value), keys.normalize(1))
                self.assertTrue(master._same_key(value, 1))
                self.assertTrue(master._has_key([{"管理番号": value}], "管理番号", "1"))


if __name__ == "__main__":
    unittest.main()
