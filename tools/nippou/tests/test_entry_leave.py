"""日報入力: 打ちかけを置いてから出る・遅れて返った答えで打った値を戻さない

通しの点検で見つかったもの:

* 行を打って1分以内に「記録を見る」へ行って戻る/「終了」を押すと、最後の
  自動保存(1分に1回)のあとに打った行が消えていた(途中で切れた行が残る)
* 時の欄に「18」と打ち終えて分へ移った直後、「1」のときに頼んだ答えが返って
  「1」へ戻り、そのまま自動保存されていた

どちらも画面(JS)の動きなので、ここでは仕組みが入っていることを確かめる
(画面で確かめた手順は tests の外 ── 変更の説明にある)。
"""
from __future__ import annotations

import unittest
from pathlib import Path

JS = Path(__file__).resolve().parent.parent / "app" / "static" / "js"


def read(name: str) -> str:
    return (JS / name).read_text(encoding="utf-8")


class LeaveTests(unittest.TestCase):
    def test_画面を移る前に確かめる(self) -> None:
        nav = read("nav.js")
        self.assertIn("export function beforeLeave", nav)
        go = nav[nav.index("export async function go("):]
        self.assertIn("await leaving()", go[:400], "移る前に確かめていない")

    def test_終了の前にも確かめる(self) -> None:
        app = read("app.js")
        quit_ = app[app.index("function wireQuit("):]
        self.assertLess(quit_.index("nav.leaving()"), quit_.index('"/api/shutdown"'))

    def test_日報入力は打ちかけを置いてから出る(self) -> None:
        entry = read("views/entry.js")
        self.assertIn("beforeLeave(keepTyped)", entry)
        self.assertIn('window.addEventListener("pagehide"', entry)
        keep = entry[entry.index("async function keepTyped()"):]
        self.assertIn("placeDraft()", keep[:400])
        self.assertIn("confirm(", keep[:1200], "置けなかったときに黙って捨てる")


class CloseTests(unittest.TestCase):
    """統合ツールの窓の × ・「終了」でも打ちかけを置く(外枠が頼んでくる)。"""

    def test_外枠の頼みに答える(self) -> None:
        app = read("app.js")
        hook = app[app.index('"alltools:before-close"'):]
        self.assertIn("event.source !== window.parent", app)
        self.assertIn('reply("alltools:before-close-ack")', hook[:600])
        self.assertIn("await nav.leaving()", hook[:800])
        self.assertIn('reply("alltools:before-close-done", { ok })', hook[:900])

    def test_どの欄でも打ったら数える(self) -> None:
        """材・寸法のような自由な欄が数えられず、閉じる前に置かれなかった。"""
        entry = read("views/entry.js")
        start = entry[entry.index("export function start()"):]
        self.assertIn('"[data-row][data-family], [data-header], [data-check]")) touched(el)',
                      start[:2400])

    def test_保存は1本ずつ_中身は送る瞬間に集める(self) -> None:
        """古い自動保存が、新しい保存のあとに着いて上書きしていた。"""
        entry = read("views/entry.js")
        self.assertEqual(entry.count('api.post("/api/entry/save"'), 3)
        for fn in ("async function maybeAutosave", "async function saveNow",
                   "async function saveDraft"):
            body = entry[entry.index(fn):]
            self.assertIn("inOrder(() => {", body[:1200], fn)


class PageOverwriteTests(unittest.TestCase):
    """v4.24.0: 画面に出ていないページへ、画面の中身を書かない(画面の側の備え)。

    断るのはサーバ(`entry.screen_mismatch`、`tests/test_page_mismatch.py`)。
    画面は、済んだ操作のあとに「書いたもの」にしてから移り、引き直したあとの
    画面へ前の画面の答えを塗らない。
    """

    def test_打ちかけは中身で見る(self) -> None:
        entry = read("views/entry.js")
        self.assertIn("const unsaved = () => snap() !== lastSavedSnap;", entry)
        self.assertNotIn("savedSeq", entry, "数えるやり方が残っている")

    def test_引き直したあとの画面へ前の答えを塗らない(self) -> None:
        entry = read("views/entry.js")
        start = entry[entry.index("export function start()"):]
        self.assertIn("gen += 1;", start[:300])
        for fn in ("async function settle(", "async function lookupLot(",
                   "async function stampTime(", "async function copyAbove(",
                   "async function stampShiftEnd(", "async function askWeight(",
                   "async function saveNow(", "async function placeDraft("):
            body = entry[entry.index(fn):]
            self.assertIn("if (!live(mine))", body[:1400], fn)

    def test_画面を出たら要求を取り消す(self) -> None:
        entry = read("views/entry.js")
        self.assertIn("signal: pageSignal()", entry)
        api = read("api.js")
        self.assertIn("...(signal ? { signal } : {})", api)

    def test_済んだ操作のあとは書いたものにしてから移る(self) -> None:
        entry = read("views/entry.js")
        newpage = entry[entry.index('api.post("/api/entry/newpage"'):]
        self.assertLess(newpage.index("markSaved();"), newpage.index("refresh();"))
        back = entry[entry.index("async function backToCurrent()"):]
        self.assertLess(back.index("markSaved();"), back.index("location.href"))
        modals = read("views/modals.js")
        run = modals[modals.index('"formstop-run"'):]
        self.assertLess(run.index("await leaving()"), run.index('"/api/formstop/execute"'))
        self.assertLess(run.index("markSaved();"), run.index("location.href"))

    def test_帯の閉じるは置いてから戻る(self) -> None:
        app = read("app.js")
        back = app[app.index("    back: async"):]
        self.assertLess(back.index("await nav.leaving()"), back.index('"/api/settings/back"'))
        off = app[app.index("    adminOff: async"):]
        self.assertLess(off.index("await nav.leaving()"), off.index('"/api/settings/admin"'))
        self.assertIn("window.__alltoolsHandlesClose = true;", app)

    def test_見張りは打ちかけごと連れて行かない(self) -> None:
        watch = read("shift_end.js")
        review = watch[watch.index("async function reviewCheck()"):]
        self.assertNotIn("location.href = REVIEW_URL", review)
        self.assertIn("await go(REVIEW_URL)", review)
        self.assertIn("typedHere()", review)
        refresh = watch[watch.index("function refreshUnlessTyping()"):]
        self.assertIn("!savesTyped() && typedHere()", refresh[:900])

    def test_押したボタンは押した直後だけ待ちにする(self) -> None:
        busy = read("busy.js")
        self.assertIn("setTimeout(drop, PRESS_MS)", busy)
        self.assertIn('addEventListener("keydown", drop, true)', busy)

    def test_共有へ保存の前に打ちかけを置く(self) -> None:
        entry = read("views/entry.js")
        push = entry[entry.index("async function pushShared("):]
        self.assertLess(push.index("saveDraft()"), push.index('"/api/settings/push"'))


class OtherScreensTests(unittest.TestCase):
    """v4.24.0: 梱包資材・VC計算・マスタ・設定の「打った値が戻る」。"""

    def test_梱包資材は古いロットの答えを捨てる(self) -> None:
        gw = read("views/gw.js")
        lot = gw[gw.index("async function lookupLot()"):]
        self.assertIn("if (mine !== lotSeq || current() !== lotNo) return;", lot)
        self.assertIn("}, since);", lot, "引いているあいだに打った寸法を戻す")
        calc = gw[gw.index("async function calculate()"):]
        self.assertIn("if (mine !== calcSeq) return;", calc[:400])
        self.assertIn('"print-packs", "per-pack-rows"', gw)

    def test_VC計算は2度押しで2回書かない(self) -> None:
        vc = read("views/vc.js")
        send = vc[vc.index("async function send(url, payload)"):]
        self.assertIn("if (sending) return null;", send[:200])
        self.assertIn('const DRAFT_KEY = "vc:draft";', vc)

    def test_マスタは直した列と開いたときの値を送る(self) -> None:
        master = read("views/master.js")
        self.assertIn("{ key: editing[state.row_key], values, original }", master)
        write = master[master.index("async function writeRow("):]
        self.assertIn("if (writing) return;", write[:200])

    def test_設定は描き直しで打ちかけを消さない(self) -> None:
        settings = read("views/settings.js")
        self.assertNotIn("\n      refresh();", settings)
        self.assertIn("beforeLeave(keepOrAsk);", settings)


class StaleReplyTests(unittest.TestCase):
    def test_頼んだあとに打った欄は書き換えない(self) -> None:
        entry = read("views/entry.js")
        self.assertIn("function paint(view, { forceRow = 0, since = null } = {})", entry)
        self.assertIn('Number(el.dataset.editAt || 0) > since', entry)
        settle = entry[entry.index("async function settle("):]
        self.assertIn("paint(body, { since })", settle[:400])
        lookup = entry[entry.index("async function lookupLot("):]
        self.assertIn("since })", lookup[:600])


if __name__ == "__main__":
    unittest.main()
