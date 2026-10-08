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
        self.assertIn("saveDraft()", keep[:400])
        self.assertIn("confirm(", keep[:600], "置けなかったときに黙って捨てる")


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
                      start[:900])

    def test_保存は1本ずつ_中身は送る瞬間に集める(self) -> None:
        """古い自動保存が、新しい保存のあとに着いて上書きしていた。"""
        entry = read("views/entry.js")
        self.assertEqual(entry.count('api.post("/api/entry/save"'), 3)
        for fn in ("async function maybeAutosave", "async function saveNow",
                   "async function saveDraft"):
            body = entry[entry.index(fn):]
            self.assertIn("inOrder(() => {", body[:1200], fn)


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
