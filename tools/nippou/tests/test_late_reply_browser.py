"""日報入力: **遅れて届いた答え・追い越された答えで、打った欄が戻らない** (本物のブラウザ)

「打った値が戻る・消える」は何度も直してきた不具合です(README に「消え」117回)。
画面は要求ごとに「頼んだときの打った番号」(`editSeq`)を覚え、答えを塗るときに
それより後に打った欄は塗らない(`paint(..., { since })`)。この試験は**答えをわざと
止めて**、そのあいだに打ち、止めた答えを返したり順番を入れ替えたりして確かめます。

    欄を離れた答え(`/api/entry/state`)を止めて、別の欄を打つ
    2つの答えを逆の順に返す(追い越し)
    ロット№の引き(`/api/entry/lot`)を止めて、同じ行のほかの欄を打つ
    保存(`/api/entry/save`)を止めて、打つ

サーバの時計は直の中ほどに止めます(直の残り5分は自動確定で画面が移るため)。
本物のブラウザ(Playwright)で通します。無い環境では飛ばします(CI では飛ばしません)。
"""
from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 取り次ぎ・答えを止める・時計を止める・日報入力を開くは土台にまとめた(`tests/_entry_browser.py`)
from tests._entry_browser import EntryBrowserCase  # noqa: E402


class LateReplyTests(EntryBrowserCase):
    # -- 試験 ------------------------------------------------------------
    def test_離れた答えが遅れても_そのあいだに打った欄は戻らない(self) -> None:
        page = self.open_entry()
        self.hold = ("/api/entry/state",)
        self.type_in(page, "MAI1", "10")
        page.locator("#MAI1").press("Tab")           # 離れる → 答えを止める
        self.wait_held(page, 1)
        self.type_in(page, "HIT1", "3")              # 答えを待つあいだに打つ
        self.hold = ()
        self.release(page)
        self.assertEqual(self.value(page, "HIT1"), "3")
        self.assertEqual(self.value(page, "MAI1"), "10")
        self.assertEqual(self.errors, [])

    def test_追い越した古い答えで新しい欄が戻らない(self) -> None:
        page = self.open_entry()
        self.hold = ("/api/entry/state",)
        self.type_in(page, "MAI1", "10")
        page.locator("#MAI1").press("Tab")           # 1本目(HIT1 が空のときの答え)
        self.wait_held(page, 1)
        self.type_in(page, "HIT1", "3")
        page.locator("#HIT1").press("Tab")           # 2本目
        self.wait_held(page, 2)
        self.hold = ()
        self.release(page, 1)                        # 新しいほうを先に返す
        self.release(page, 0)                        # 古いほうが後から届く
        self.assertEqual(self.value(page, "HIT1"), "3")
        self.assertEqual(self.value(page, "MAI1"), "10")
        self.assertEqual(self.errors, [])

    def test_ロットを引くあいだに同じ行に打った欄は戻らない(self) -> None:
        """ロットの答えは**焦点のある行も書き換える**(`forceRow`)。それでも後から打った欄は守る。"""
        page = self.open_entry()
        self.hold = ("/api/entry/lot",)
        self.type_in(page, "LOT1", "N7131T0")
        page.locator("#LOT1").press("Tab")
        self.wait_held(page, 1)
        self.hold = ()                               # 離れた答え(state)は通す
        self.type_in(page, "HIT1", "4")
        self.release(page)
        self.assertEqual(self.value(page, "HIT1"), "4")
        self.assertEqual(self.value(page, "LOT1"), "N7131T0")
        self.assertEqual(self.errors, [])

    def test_保存を待つあいだに打った欄は戻らない(self) -> None:
        page = self.open_entry()
        self.type_in(page, "MAI1", "10")
        page.locator("#MAI1").press("Tab")
        page.wait_for_timeout(600)                   # 離れた答えまで済ませる
        self.hold = ("/api/entry/save",)
        page.click("#save")
        deadline = time.monotonic() + 8
        while not any(json.loads(r.request.post_data or "{}").get("silent") is not True
                      for r in self.held):
            if time.monotonic() > deadline:
                self.fail("保存(確定)の要求が来ません")
            page.wait_for_timeout(50)
        self.type_in(page, "HIT1", "5")              # 保存を待つあいだに打つ
        # **焦点も外す**(打っている欄は別の守りで書き換えないので、それに頼らずに確かめる)
        page.locator("#HIT1").press("Tab")
        page.wait_for_timeout(600)                   # 離れた答え(止めていない)を塗り終える
        self.hold = ()
        while self.held:
            self.release(page)
        self.assertEqual(self.value(page, "HIT1"), "5")
        self.assertEqual(self.value(page, "MAI1"), "10")
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
