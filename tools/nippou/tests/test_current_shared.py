"""どの画面からも使う「いまの直」の組み立ては `app/current.py` に(段階2・案3)

前は日報入力の道(`app/routes/entry.py`)の中にあり、ほかの画面の道(設定・印刷・グラフ・集計・
梱包資材・VC・音・標準時間・作業者)が 17 か所から `from .entry import current_calculator` の
ように**画面の道から借りて**いました。日報入力の道を直すと、関係の無い画面まで巻き込みます。

動きは変えません(同じ関数をそのまま移す)。日報入力の道からも同じ名前で使えるままにします。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SHARED = ("build_shift_calculator", "current_calculator", "build_service", "ribbon_now",
          "current_flow", "time_split_enabled", "current_target")


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class SharedTests(WebTestCase):
    def test_日報入力の道からも同じものが使える(self) -> None:
        from app import current
        from app.routes import entry

        for name in SHARED:
            with self.subTest(name=name):
                self.assertIs(getattr(entry, name if name != "current_target" else "_target"),
                              getattr(current, name))

    def test_いまの直の計算機と帯(self) -> None:
        from app import current
        from nippou import work_context
        from nippou.logic.shift import ShiftCalculator

        with self.app.test_request_context("/"):
            calc = current.current_calculator()
            self.assertIsInstance(calc, ShiftCalculator)
            ctx = work_context.get_context()
            key = current.current_target(ctx, calc)
            self.assertEqual(len(key), 4)
            self.assertEqual(key[3], 1)                         # まだ何も無ければ 1 ページ目
            ribbon = current.ribbon_now(ctx, calc)
            self.assertIsInstance(ribbon, dict)
            self.assertFalse(current.time_split_enabled())       # 既定はオフ


class NoBorrowTests(unittest.TestCase):
    def test_ほかの画面の道は日報入力の道から借りない(self) -> None:
        offenders = []
        for path in sorted((ROOT / "app").rglob("*.py")):
            if path.name == "entry.py" and path.parent.name == "routes":
                continue
            text = path.read_text(encoding="utf-8")
            for found in re.finditer(r"from \.(?:routes\.)?entry import ([^\n]+)", text):
                names = {n.strip() for n in found.group(1).split(",")}
                if names & set(SHARED):
                    offenders.append(f"{path.relative_to(ROOT)}: {found.group(0)}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
