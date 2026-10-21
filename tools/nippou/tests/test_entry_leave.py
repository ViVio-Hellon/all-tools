"""日報入力: 打ちかけを置いてから出る・遅れて返った答えで打った値を戻さない

通しの点検で見つかったもの:

* 行を打って1分以内に「記録を見る」へ行って戻る/「終了」を押すと、最後の
  自動保存(1分に1回)のあとに打った行が消えていた(途中で切れた行が残る)
* 時の欄に「18」と打ち終えて分へ移った直後、「1」のときに頼んだ答えが返って
  「1」へ戻り、そのまま自動保存されていた

どちらも画面(JS)の動きなので、**本物のブラウザで打って確かめます**(下の指し示し)。
ここに残っているのは、ブラウザで区別できないもの(守りが何重にもある)と、
ほかの画面(梱包資材・VC計算・マスタ・設定)の字探しです。
"""
from __future__ import annotations

import unittest
from pathlib import Path

JS = Path(__file__).resolve().parent.parent / "app" / "static" / "js"


def read(name: str) -> str:
    return (JS / name).read_text(encoding="utf-8")


# 「終了」の前にも打ちかけを置く(`/api/shutdown` より先に置く)・窓の × ・終了で打ちかけを置く
# (外枠の頼み `alltools:before-close`、外枠へ「自分で答える」と名乗る)・どの欄でも打ったら数える・
# 保存は1本ずつ(`inOrder`)・移る前に打ちかけを置く・置けなければ訊く・打ちかけは中身で見る・
# 共有へ保存の前に打ちかけを置く は、字を探すのではなく
# **本物のブラウザで打って確かめます**(`test_entry_leave_browser`・`test_entry_shell_browser` の
# `QuitTests`。守りを外すと落ちる)。


class PageOverwriteTests(unittest.TestCase):
    """v4.24.0: 画面に出ていないページへ、画面の中身を書かない(画面の側の備え)。

    断るのはサーバ(`entry.screen_mismatch`、`tests/test_page_mismatch.py`)。
    画面は、済んだ操作のあとに「書いたもの」にしてから移り、引き直したあとの
    画面へ前の画面の答えを塗らない。
    """

    def test_引き直したあとの画面へ前の答えを塗らない(self) -> None:
        """**字のまま残す。** 引き直した画面へ前の答えが塗られるのは、守りが何重にもあって
        (画面を出たら要求を取り消す `pageSignal`・サーバの `screen_mismatch`・`paint` の `since`)、
        ブラウザで動かしても、この印(`gen`/`live`)を外しただけでは見た目が変わらないため。"""
        entry = read("views/entry.js")
        start = entry[entry.index("export function start()"):]
        self.assertIn("gen += 1;", start[:300])
        for fn in ("async function settle(", "async function lookupLot(",
                   "async function stampTime(", "async function copyAbove(",
                   "async function stampShiftEnd(", "async function askWeight(",
                   "async function saveNow(", "async function placeDraft("):
            body = entry[entry.index(fn):]
            self.assertIn("if (!live(mine))", body[:1400], fn)


# 次の5つは、字を探すのではなく**本物のブラウザで押して確かめます**(守りを外すと落ちる):
#
#   画面を出たら要求を取り消す       … `test_entry_shell_browser.LeaveCancelTests`
#   済んだ操作のあとは書いたものにしてから移る(次ページ発行・最新のページに戻る・全停入力)
#                                     … `test_entry_shell_browser.DoneThenMoveTests`
#   帯の閉じるは置いてから戻る(過去データ ✕・管理者 ✕)
#                                     … `test_entry_shell_browser.ChipTests`
#   見張りは打ちかけごと連れて行かない(確認画面へ・自動確定のあとの塗り直し)
#                                     … `test_shift_watch_browser`
#   押したボタンは押した直後だけ待ちにする … `test_entry_shell_browser.BusyTests`


class OtherScreensTests(unittest.TestCase):
    """v4.24.0: 梱包資材・VC計算・マスタ・設定の「打った値が戻る」。

    梱包資材のロットの引き(古いロットの答え・打ち直したロット・引いているあいだに打った寸法)と、
    梱包数・表の行数を戻ってきても残す は、**本物のブラウザで答えを止めて確かめます**
    (`test_gw_lot_browser`。守りを外すと落ちる)。

    ここに残っているのは字のまま: 梱包資材の計算の追い越し(答えの形が大きく、試験で
    作るのが割に合わない)と、VC計算・マスタ・設定(画面の準備が重い。VC の2度押しは
    返事を待つあいだボタンも押せなくなるので、守りが2重で見た目では区別できない)。
    """

    def test_梱包資材は古い計算の答えを捨てる(self) -> None:
        gw = read("views/gw.js")
        calc = gw[gw.index("async function calculate()"):]
        self.assertIn("if (mine !== calcSeq) return;", calc[:400])

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


# 「頼んだあとに打った欄は書き換えない」(`paint(..., { since })`)は、字を探すのではなく
# **答えをわざと止めて打つ**本物のブラウザの試験で確かめます(`test_late_reply_browser`。
# 欄を離れた答えの遅れ・追い越し・ロットの引き・保存の4つ。守りを外すと4件とも落ちる)。

if __name__ == "__main__":
    unittest.main()
