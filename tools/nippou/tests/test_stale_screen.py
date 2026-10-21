"""**サーバの状態が変わったのに、画面が古い答えを出したまま**にしない

【なぜ書いたか】
この形のバグを3回続けて出しました:

    共有へ送り終えたのに「前の直が共有へ出ていません /
    この直の入力は始められません」が出たまま(v3.56.2 で修正)
    「共有へ保存: 1件」と出たすぐ横で「共有へ未送信 1直ぶん」が
    減らないまま(v3.56.3 で修正)
    残り5分の自動確定が走って共有へも送ったのに、レールの数も帯も
    古いまま(同上)

どれも同じ形です ── **画面は HTML が描かれた時点の答えを持っている**
のに、そのあとサーバ側で数や状態が変わり、誰も塗り直さない。

利用者から見ると「押す物も無いのに止まっている」「やったのにやって
いないと書いてある」で、**開き直すまで抜けられません。** 直すところが
無いのに止まっているのが一番たちが悪い ── 何を直せばよいのか
分からないからです。

【どう防ぐか】
口は増えていくので、1つずつ画面を動かして確かめるのでは足し忘れを止められません。
そこで**呼び出し側の形**を見ます(書き方の決まりとして): 画面の状態を変える口を
叩いたら、そのすぐあとで塗り直しているか。

**実際に出た件は、本物のブラウザで押して確かめます**(`test_shift_watch_browser` の
`RepaintTests`: 自動確定の確定でも拾いでも塗り直す・打っている最中は見送る・
共有へ保存したら設定画面を塗り直す・前の直を引き継いだら塗り直す)。

塗り直しと認めるのは3つです:

    refresh()        いまの画面をサーバから取り直す(レールも含む)
    location.href    別の画面へ移る
    paint(...)       応答に画面ぜんぶが入っているので、それを写す

**完全ではありません。** 行の近さで見ているだけなので、遠くに書いた
塗り直しは拾えません。それでも「足すのを忘れた」はここで止まります。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "app" / "static" / "js"

#: 叩くと**画面に出ている何かが変わる**口。
#:
#: 「共有へ未送信 ◯直ぶん」(レールと設定画面)・前の直の始末・
#: 管理者モード・ライン・開いているページ ── どれも画面が
#: 描いたきりなので、変えたら塗り直さないと食い違います。
CHANGES_SCREEN = (
    "/api/settings/push",             # 未送信の数が減る
    "/api/settings/import/apply",     # 未送信の数が増える
    "/api/settings/restore-shift",    # 同上
    "/api/entry/handover",            # 前の直の始末が変わる
    "/api/entry/close",               # 確定・拾い → 未送信の数が減る
    "/api/settings/admin",            # 鍵の開け閉め
    "/api/entry/line",                # ラインが変わる
    "/api/settings/recall",           # 開く直が変わる
    "/api/settings/page",             # 開くページが変わる
    "/api/formstop/execute",          # ページが1枚増える
)

#: 塗り直したと認める書き方。
#:
#: `refresh` で始まる呼び出しはぜんぶ認めます ── 包んだもの
#: (`refreshUnlessTyping()`)も塗り直しなので。
REPAINT = re.compile(r"\brefresh\w*\(|location\.(href|reload)|\bpaint\(")

#: 何行先まで見るか。**押した直後に書く**のが約束なので、短くてよい
WINDOW = 25


def _js_files() -> list[Path]:
    return sorted(JS.rglob("*.js"))


def _calls() -> list[tuple[Path, int, str, str]]:
    """(ファイル, 行番号, 口, そのあと WINDOW 行ぶん) を集める。"""
    found: list[tuple[Path, int, str, str]] = []
    pattern = re.compile(r'\.post\(\s*"([^"]+)"')
    for path in _js_files():
        lines = path.read_text(encoding="utf-8").splitlines()
        for n, line in enumerate(lines):
            match = pattern.search(line)
            if not match or match.group(1) not in CHANGES_SCREEN:
                continue
            after = "\n".join(lines[n:n + WINDOW])
            found.append((path, n + 1, match.group(1), after))
    return found


class RepaintAfterChangeTests(unittest.TestCase):
    """**変えたら塗り直す。** 足し忘れをここで止める。"""

    def test_口をぜんぶ見つけられている(self) -> None:
        """一覧が空振りしていないこと(名前を変えたら気づけるように)。"""
        seen = {call[2] for call in _calls()}
        missing = set(CHANGES_SCREEN) - seen
        self.assertEqual(missing, set(),
                         f"この口を叩いている画面が見つかりません: {missing}")

    def test_変えたら塗り直している(self) -> None:
        bad = []
        for path, line, url, after in _calls():
            if not REPAINT.search(after):
                bad.append(f"{path.relative_to(ROOT)}:{line} {url}")
        self.assertEqual(bad, [], "\n".join(
            ["状態を変えたのに塗り直していません(画面が古いまま残ります):",
             *bad]))


# **実際に出た3件**(直の確定も拾いも塗り直す・打っている最中は塗り直さない・共有へ保存したら
# 設定画面を塗り直す)と、前の直を引き継いだら塗り直す は、字を探すのではなく
# **本物のブラウザで見張りを回して確かめます**(`test_shift_watch_browser.RepaintTests`。
# 塗り直しを外す・`ran` だけ/`swept` だけにする・入力中の見送りを外すと落ちる)。


if __name__ == "__main__":
    unittest.main()
