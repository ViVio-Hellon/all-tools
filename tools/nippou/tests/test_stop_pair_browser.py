"""日報入力: 作業停止は記号と時間を組で (v4.46.5)

    作業停止で停止理由選択後時間を入れなくても進めれますけど？

前は保存(確定)の関門だけで、打っている最中は記号を選んで時間を打たないまま、ほかの欄・
次の行へ進めた。組の外へ移ろうとしたら足りないほうの欄へ戻し、行に印を付け、保存も断る。
本物のブラウザで確かめる(無い環境では飛ばす)。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HAS_FLASK, SKIP_REASON  # noqa: E402
from tests import test_reason_popup_browser as reason_popup  # noqa: E402


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class StopPairBrowserTests(reason_popup.ReasonPopupBrowserTests):
    # 親の試験はここでは流さない(親のファイルで流れる)
    test_やめたあとでもほかの行でその他を選べば聞く = None
    test_同じ行で選び直せばまた聞く = None

    def fill_times(self, page, row: int) -> None:
        for fam, value in (("KZ", "08"), ("KH", "00"), ("SZ", "10"), ("SH", "00")):
            page.fill(f"#{fam}{row}", value)
            page.dispatch_event(f"#{fam}{row}", "change")

    def focused(self, page) -> str:
        return page.evaluate("document.activeElement?.id || ''")

    def test_記号を選んで時間を打たずにほかの欄へ行くと時間の欄へ戻る(self) -> None:
        page = self.open_entry()
        page.fill("#LOT1", "H5422S0")
        self.fill_times(page, 1)
        page.select_option("#S1", "イ")
        page.focus("#TH1")                                     # 組の中は行き来できる
        self.assertEqual(self.focused(page), "TH1")
        page.focus("#LOT2")                                    # 時間を打たずに次の行へ
        page.wait_for_function("document.activeElement?.id === 'TH1'", timeout=3000)
        self.assertIn("1行目：作業停止①の時間", page.inner_text("body"))
        # 行に印(保存(確定)を押す前に、直すところが見える)。保存の断りは `test_work_time` で見る
        page.wait_for_selector('#grid-body tr[data-row="1"][data-bad="1"]', timeout=5000)
        self.assertIn("作業停止①「イ」の時間が入っていません",
                      page.get_attribute('#grid-body tr[data-row="1"]', "title") or "")
        self.assertFalse(page.is_hidden("#bad-note"), "「直すところが1行あります」を出す")
        # 時間を打てば進める・印も消える
        page.fill("#TH1", "10")
        page.dispatch_event("#TH1", "change")
        page.focus("#LOT2")
        page.wait_for_timeout(400)
        self.assertEqual(self.focused(page), "LOT2")
        page.wait_for_selector('#grid-body tr[data-row="1"]:not([data-bad])', timeout=5000)
        self.assertEqual(self.errors, [])

    def test_時間だけで記号が無ければ記号の欄へ戻る_記号を消せば進める(self) -> None:
        page = self.open_entry()
        page.fill("#LOT1", "H5422S0")
        self.fill_times(page, 1)
        page.focus("#TH1")
        page.fill("#TH1", "15")
        page.focus("#LOT2")
        page.wait_for_function("document.activeElement?.id === 'S1'", timeout=3000)
        # 停止でなかった → 時間を消せば進める
        page.fill("#TH1", "")
        page.dispatch_event("#TH1", "change")
        page.focus("#LOT2")
        page.wait_for_timeout(400)
        self.assertEqual(self.focused(page), "LOT2")
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
