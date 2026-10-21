"""日報入力: **打ちかけを置いてから出る**・**保存は1本ずつ** (本物のブラウザ。段階1)

前は字を探して「仕組みが入っていること」だけを見ていました(`test_entry_leave` の一部)。
ここでは本当に打って、出て、戻って確かめます:

    - 記録を見るへ移って戻っても、打った欄が残る(移る前に打ちかけを置く `keepTyped`)
    - 材のような自由な欄でも同じ(どの欄でも打ったら数える `touched`)
    - 保存は1本ずつ・中身は送る瞬間に集める(`inOrder` ── 古い保存があとから着いて上書きしない)
    - 日報複合ツールの窓の × ・終了: 外枠の頼み(`alltools:before-close`)に「受けた」「済んだ」を
      返し、そのあいだに打ちかけを置く。外枠でない相手からの頼みには答えない
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._entry_browser import ORIGIN, EntryBrowserCase  # noqa: E402

#: 日報入力を埋め込む親(日報複合ツールの外枠の代わり)。頼みを送り、返事を `window.__replies` へ
FRAME_PAGE = """<!doctype html><meta charset="utf-8"><title>外枠</title>
<iframe id="tool" src="/" style="width:1400px;height:860px"></iframe>
<script>
  window.__replies = [];
  window.addEventListener("message", (e) => { if (e.data && e.data.type) window.__replies.push(e.data); });
  window.askClose = (seq) => document.getElementById("tool").contentWindow
      .postMessage({ type: "alltools:before-close", seq }, "*");
</script>"""


class LeaveTests(EntryBrowserCase):
    def test_記録を見るへ移って戻っても打った欄が残る(self) -> None:
        page = self.open_entry()
        self.type_in(page, "HIT1", "3")
        self.type_in(page, "ZAI1", "52S- R")            # 自由な欄(材)も数える
        before = len(self.saves(draft=True))
        page.click('nav.rail a[href="/records"]')       # 自動保存(1分)を待たずに移る
        page.wait_for_function("location.pathname === '/records'", timeout=8000)
        drafts = self.saves(draft=True)[before:]
        self.assertTrue(drafts, "移る前に打ちかけを置いていません")
        row = drafts[-1].get("rows", {}).get("1", {})
        self.assertEqual((row.get("HIT"), row.get("ZAI")), ("3", "52S- R"))
        page.click('nav.rail a[href="/"]')
        page.wait_for_selector("#grid-body")
        page.wait_for_timeout(800)
        self.assertEqual(self.value(page, "HIT1"), "3")
        self.assertEqual(self.value(page, "ZAI1"), "52S- R")
        self.assertEqual(self.errors, [])

    def test_打ちかけを置けなければ黙って捨てずに訊く(self) -> None:
        page = self.open_entry()
        self.type_in(page, "HIT1", "3")
        self.refuse["/api/entry/save"] = 500           # 置けない
        page.click('nav.rail a[href="/records"]')
        page.wait_for_function("location.pathname === '/records'", timeout=8000)  # 「はい」で移る
        self.assertTrue(any("保存していない行は消えます" in m for m in self.dialogs), self.dialogs)

    def test_共有へ保存の前に打ちかけを置く(self) -> None:
        page = self.open_entry()
        self.type_in(page, "HIT1", "3")
        mark = len(self.sent)
        page.click("#push-shared")
        is_push = lambda p: p.split("?")[0] == "/api/settings/push"      # 下見(/plan)ではなく送る要求
        self.wait_until(page, lambda: any(is_push(p) for p, _ in self.sent[mark:]), "共有へ保存の要求")
        paths = [p for p, _ in self.sent[mark:]]
        draft_at = next((i for i, (p, b) in enumerate(self.sent[mark:])
                         if p.startswith("/api/entry/save") and b.get("draft")), None)
        push_at = next(i for i, p in enumerate(paths) if is_push(p))
        self.assertIsNotNone(draft_at, f"打ちかけを置いていません: {paths}")
        self.assertLess(draft_at, push_at, "共有へ送ってから置いています")
        self.assertEqual(self.sent[mark:][draft_at][1]["rows"]["1"].get("HIT"), "3")

    def test_保存は1本ずつ_中身は送る瞬間に集める(self) -> None:
        page = self.open_entry()
        self.type_in(page, "MAI1", "10")
        page.locator("#MAI1").press("Tab")
        page.wait_for_timeout(600)
        self.hold = ("/api/entry/save",)
        page.click("#save")                             # 1本目を止める
        self.wait_held(page, 1)
        self.type_in(page, "HIT1", "7")                 # 1本目を待つあいだに打つ
        page.locator("#HIT1").press("Tab")
        page.wait_for_timeout(300)
        # 2本目: 保存の答えを待つあいだに画面を移る(打ちかけを置く `placeDraft`)。
        # 保存のボタンは答えを待つあいだ押せないので、重なるのはこの道
        page.click('nav.rail a[href="/records"]')
        page.wait_for_timeout(1200)
        self.assertEqual(len(self.held), 1, "1本目が返る前に2本目が出ています(追い越して上書きし得る)")
        self.release(page)                              # 1本目を返すと、2本目が出る
        self.wait_held(page, 1)
        import json

        # 2本目は欄を離れたあとの自動保存か、移る前の打ちかけ(どちらも同じ列に並ぶ)
        second = json.loads(self.held[0].request.post_data or "{}")
        self.assertTrue(second.get("draft") or second.get("silent"), second.keys())
        self.assertEqual(second["rows"]["1"].get("HIT"), "7", "2本目が送る瞬間の中身を持っていません")
        self.hold = ()
        while self.held:
            self.release(page)
        page.wait_for_function("location.pathname === '/records'", timeout=8000)
        self.assertEqual(self.errors, [])


class WindowCloseTests(EntryBrowserCase):
    """日報複合ツールの窓の × ・終了。**外枠が頼み、日報が打ちかけを置いてから「済んだ」を返す。**"""

    def open_framed(self):
        self.pages["/__frame"] = FRAME_PAGE
        page = self.watch(self.ctx.new_page())
        page.goto(f"{ORIGIN}/__frame", wait_until="load")
        frame = page.frame_locator("#tool")
        frame.locator("#grid-body").wait_for()
        tool = page.frames[1]
        tool.wait_for_timeout(600)
        self.name_worker(tool)
        return page, tool

    def test_窓を閉じる前に打ちかけを置いてから済んだと返す(self) -> None:
        page, tool = self.open_framed()
        self.type_in(tool, "HIT1", "4")
        before = len(self.saves(draft=True))
        page.evaluate("askClose(7)")
        page.wait_for_function(
            "window.__replies.some(r => r.type === 'alltools:before-close-done' && r.seq === 7)",
            timeout=8000)
        replies = page.evaluate("window.__replies")
        kinds = [r["type"] for r in replies if r.get("seq") == 7]
        self.assertEqual(kinds[0], "alltools:before-close-ack")      # すぐ「受けた」
        done = next(r for r in replies if r["type"] == "alltools:before-close-done")
        self.assertTrue(done["ok"])
        drafts = self.saves(draft=True)[before:]
        self.assertTrue(drafts, "閉じる前に打ちかけを置いていません")
        self.assertEqual(drafts[-1]["rows"]["1"].get("HIT"), "4")
        self.assertEqual(self.errors, [])

    def close_once(self, page, seq: int) -> None:
        page.evaluate(f"askClose({seq})")
        page.wait_for_function(
            f"window.__replies.some(r => r.type === 'alltools:before-close-done' && r.seq === {seq})",
            timeout=8000)

    def test_打ちかけは中身で見る_打って戻したら置き直さない(self) -> None:
        """打った回数ではなく**中身**で見る(`unsaved = snap() !== lastSavedSnap`)。"""
        page, tool = self.open_framed()
        self.type_in(tool, "HIT1", "3")
        self.close_once(page, 1)                      # 置く(ここで「書いたもの」になる)
        self.assertEqual(self.saves(draft=True)[-1]["rows"]["1"].get("HIT"), "3")
        self.type_in(tool, "HIT1", "4")
        self.type_in(tool, "HIT1", "3")               # 打ったが、中身は置いたときと同じ
        before = len(self.saves(draft=True))
        self.close_once(page, 2)
        self.assertEqual(self.saves(draft=True)[before:], [], "中身が同じなのに置き直しています")
        self.type_in(tool, "HIT1", "5")               # 中身が変われば置く
        self.close_once(page, 3)
        self.assertEqual(self.saves(draft=True)[-1]["rows"]["1"].get("HIT"), "5")

    def test_外枠でない相手からの頼みには答えない(self) -> None:
        page = self.open_entry()                        # 埋め込まれていない(親が自分)
        page.evaluate("""() => {
            window.__seen = [];
            window.addEventListener("message", (e) => window.__seen.push(e.data && e.data.type));
            window.postMessage({ type: "alltools:before-close", seq: 1 }, "*");
        }""")
        page.wait_for_timeout(800)
        seen = page.evaluate("window.__seen")
        self.assertNotIn("alltools:before-close-ack", seen)
        self.assertNotIn("alltools:before-close-done", seen)


if __name__ == "__main__":
    unittest.main()
