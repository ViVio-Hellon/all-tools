"""マスタ管理: 表の一覧は、それだけで流す (v3.94.0)

    テーブル名だけでスクロールしたいで現状だと全体でスクロールして
    選んで上に戻ってテーブル確認です

一覧(表が40近く)をそのまま伸ばしていたので、下のほうの表を選ぶには作業面
ごと流すしかなく、選ぶと中身(右)は画面の上に置いてけぼりでした。

**一覧がそれだけで流れる(高さを画面に収めて中だけ流す)・狭い画面で選ぶと中身の頭へ送る
は、本物のブラウザで測って確かめます**(`test_master_browser` の ListScrollTests。守りを
外すと落ちる)。ここに残すのは、いまの並びではブラウザで区別できない備え:

    - 一覧を上に貼り付ける(sticky): 右の中身は高さを切ってあり一覧より低いので、いまは
      作業面を流しても一覧が外へ出ない。中身が伸びたときの備え
    - 描き直しても流れた位置を残す: Chromium は同じ処理の中で空にして詰め直すと位置を
      保つので、外しても区別できない(ほかのブラウザ・描き直しの間に待ちが入ったときの備え)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
CSS = (ROOT / "app/static/css/components.css").read_text(encoding="utf-8")
JS = (ROOT / "app/static/js/views/master.js").read_text(encoding="utf-8")


class ListScrollTests(unittest.TestCase):
    def test_list_sticks_on_wide_screens(self) -> None:
        block = CSS[CSS.index("テーブル名だけでスクロールしたい"):]
        block = block[:block.index("}\n}") + 3]
        self.assertIn(".masterwrap > aside{ position:sticky; top:0; }", block)

    def test_list_position_survives_repaint(self) -> None:
        """描き直すと中身が一度空になり、流れた位置が頭へ戻る ── 残す。"""
        self.assertIn("const kept = host.scrollTop;", JS)
        self.assertIn("host.scrollTop = kept;", JS)
        self.assertIn("keepInList(", JS)


if __name__ == "__main__":
    unittest.main()
