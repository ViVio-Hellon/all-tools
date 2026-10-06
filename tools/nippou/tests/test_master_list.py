"""マスタ管理: 表の一覧は、それだけで流す (v3.94.0)

    テーブル名だけでスクロールしたいで現状だと全体でスクロールして
    選んで上に戻ってテーブル確認です

一覧(表が40近く)をそのまま伸ばしていたので、下のほうの表を選ぶには作業面
ごと流すしかなく、選ぶと中身(右)は画面の上に置いてけぼりでした。本物の
ブラウザでの確かめは scratchpad の確認で済ませ、ここは**その作りが消えて
いないか**を見ます。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
CSS = (ROOT / "app/static/css/components.css").read_text(encoding="utf-8")
JS = (ROOT / "app/static/js/views/master.js").read_text(encoding="utf-8")


class ListScrollTests(unittest.TestCase):
    def test_list_has_its_own_scroll_on_wide_screens(self) -> None:
        block = CSS[CSS.index("テーブル名だけでスクロールしたい"):]
        block = block[:block.index("}\n}") + 3]
        self.assertIn(".masterwrap > aside{ position:sticky; top:0; }", block)
        self.assertRegex(block, r"\.mlist\{[^}]*max-height:calc\(100vh")
        self.assertRegex(block, r"\.mlist\{[^}]*overflow-y:auto")

    def test_list_position_survives_repaint(self) -> None:
        """描き直すと中身が一度空になり、流れた位置が頭へ戻る ── 残す。"""
        self.assertIn("const kept = host.scrollTop;", JS)
        self.assertIn("host.scrollTop = kept;", JS)
        self.assertIn("keepInList(", JS)

    def test_choosing_a_table_reveals_its_head(self) -> None:
        self.assertRegex(JS, re.compile(r"await load\(\{ file: group\.file.*?\}\);\s*revealTable\(\);", re.S))


if __name__ == "__main__":
    unittest.main()
