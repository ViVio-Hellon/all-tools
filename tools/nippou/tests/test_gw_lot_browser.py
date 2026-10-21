"""梱包資材: **古いロットの答えで、打った値を戻さない・梱包数は戻ってきても残る** (本物のブラウザ。段階1)

前は字を探していました(`test_entry_leave` の `OtherScreensTests` の一部)。ここでは LotNo を
本当に打ち、ロットの引き(`/api/gw/lot`)の答えを**わざと止めて**、順番を入れ替えて返します:

    - 新しいロットを頼んだあとに、古いロットの答えが届いても塗らない
    - 欄のロットを打ち直していたら(まだ7桁でない)、答えを塗らない
    - 引いているあいだに打った寸法は、打った値のまま
    - 梱包数(紙)・1梱包ごとの表の行数は、ほかの画面へ行って戻っても残る(前は1に戻った)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._entry_browser import ORIGIN  # noqa: E402
from tests.test_entry_shell_browser import ShellCase  # noqa: E402


def lot_answer(thickness: str) -> dict:
    """ロットの答え(寸法だけ違う)。オーダーは決まらない形にしておく。"""
    return {"found": True, "message": "", "allocations": [], "order": None, "auto": {},
            "size": {"thickness_mm": thickness, "width_mm": "1000", "length_mm": "2000"},
            "g_course": None, "lot": {}}


class GwLotTests(ShellCase):
    def open_gw(self):
        page = self.watch(self.ctx.new_page())
        page.goto(f"{ORIGIN}/gw", wait_until="load")
        page.wait_for_selector("#lot-no")
        page.wait_for_timeout(500)
        return page

    def ask(self, page, lot: str, count: int) -> None:
        """LotNo を打つ(7桁そろった時点で引く)。止めた引きが `count` 本になるまで待つ。"""
        self.type_in(page, "lot-no", lot)
        self.wait_held(page, count)

    def thickness(self, page) -> str:
        return self.value(page, "dim-thickness_mm")

    def test_新しいロットを頼んだあとに届いた古い答えは塗らない(self) -> None:
        page = self.open_gw()
        self.hold = ("/api/gw/lot",)
        self.ask(page, "N7131T0", 1)                   # 1本目(A)
        self.ask(page, "N7132T0", 2)                   # 2本目(B)
        self.ask(page, "N7131T0", 3)                   # 3本目(A をもう一度。欄は A)
        self.fulfill(page, lot_answer("2.0"), index=2)  # 新しいほうが先に返る
        self.assertEqual(self.thickness(page), "2.0")
        self.fulfill(page, lot_answer("9.9"), index=0)  # 古い A があとから届く(欄は同じ A)
        self.fulfill(page, lot_answer("7.7"), index=0)  # B も届く(欄はもう B ではない)
        self.assertEqual(self.thickness(page), "2.0", "古いロットの答えで寸法が戻りました")
        self.assertEqual(self.errors, [])

    def test_ロットを打ち直していたら答えを塗らない(self) -> None:
        page = self.open_gw()
        self.hold = ("/api/gw/lot",)
        self.ask(page, "N7131T0", 1)
        page.locator("#lot-no").press("Backspace")     # 打ち直しはじめ(6桁。まだ引かない)
        self.fulfill(page, lot_answer("9.9"))
        self.assertEqual(self.thickness(page), "", "欄に無いロットの寸法を入れました")

    def test_引いているあいだに打った寸法は戻さない(self) -> None:
        page = self.open_gw()
        self.hold = ("/api/gw/lot",)
        self.ask(page, "N7131T0", 1)
        self.type_in(page, "dim-thickness_mm", "3.5")
        self.fulfill(page, lot_answer("9.9"))
        self.assertEqual(self.thickness(page), "3.5")
        self.assertEqual(self.value(page, "dim-width_mm"), "1000")   # 打っていない欄は入る

    def test_梱包数と表の行数はほかの画面へ行って戻っても残る(self) -> None:
        page = self.open_gw()
        self.type_in(page, "print-packs", "3")
        page.select_option("#per-pack-rows", "50", force=True)    # 計算するまで隠れている表の行数
        page.click('nav.rail a[href="/records"]')
        page.wait_for_function("location.pathname === '/records'", timeout=8000)
        page.click('nav.rail a[href="/gw"]')
        page.wait_for_selector("#lot-no")
        page.wait_for_timeout(500)
        self.assertEqual(self.value(page, "print-packs"), "3")
        self.assertEqual(self.value(page, "per-pack-rows"), "50")


if __name__ == "__main__":
    unittest.main()
