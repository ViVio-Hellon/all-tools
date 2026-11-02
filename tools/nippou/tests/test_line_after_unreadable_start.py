"""起動したときにこの端末の設定を読めなかった端末が、L-1 のまま動き続けない。

起動の最初の1回で設定を読めないと(共有の一瞬の不調・ロック)、`get_context` は既定の L-1 で
始め、その起動のあいだずっと L-1 のキーで動いていた(端末は HVC でも)。設定を読めるようになったら、
設定のラインに合わせる。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests._web import WebTestCase  # noqa: E402


class LineAfterUnreadableStartTests(WebTestCase):
    terminal_line = "HVC"

    def test_起動時に読めなくても_読めるようになれば端末のラインで動く(self) -> None:
        from nippou import work_context

        with patch.object(work_context, "stored_terminal_line", return_value=""):
            ctx = work_context.reset()
        self.assertEqual(ctx.line, "L-1")                 # 読めなかった間は決まっていない扱い
        self.assertEqual(work_context.get_context().line, "HVC",
                         "読めるようになっても、起動の最初に決めた L-1 のまま動いた")
        self.assertEqual(work_context.get_context().terminal_line, "HVC")

    def test_読めて決めたラインは_あとから設定を読み直して変えない(self) -> None:
        from nippou import work_context

        ctx = work_context.reset()
        self.assertEqual(ctx.line, "HVC")
        ctx.line = "AIM"                                  # 管理者が別のラインの紙を呼び出している
        self.assertEqual(work_context.get_context().line, "AIM")

    def test_呼び出し中なら戻る先だけ合わせる(self) -> None:
        from nippou import work_context

        with patch.object(work_context, "stored_terminal_line", return_value=""):
            ctx = work_context.reset()
        ctx.line = "AIM"                                  # 読めない間に別のラインの紙を呼び出した
        ctx = work_context.get_context()
        self.assertEqual(ctx.line, "AIM")
        self.assertEqual(ctx.terminal_line, "HVC")


if __name__ == "__main__":
    unittest.main()
