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
画面を動かすテストはこの一式では走らせられない(ブラウザが要る)ので、
**呼び出し側の形**を見ます: 画面の状態を変える口を叩いたら、その
すぐあとで塗り直しているか。

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


class KnownCasesTests(unittest.TestCase):
    """**実際に出た3件**が、ちゃんと塗り直す形になっていること。

    一般の走査だけだと、直したはずの場所が別の形で戻っても気づけません。
    出たものは名指しで押さえます。
    """

    def source(self, name: str) -> str:
        return (JS / name).read_text(encoding="utf-8")

    def test_直の確定も拾いも塗り直す(self) -> None:
        """`ran` のときだけ・`swept` のときだけ、に**分かれていない**こと。

        片方だけ塗り直すと、同じ症状が残る側と消える側ができます。
        """
        text = self.source("shift_end.js")
        self.assertIn("refreshUnlessTyping()", text)
        # 条件つきで呼んでいないか(`if (swept.length) refresh...` に戻す
        # と、残り5分の確定のほうが古いまま残ります)
        self.assertNotIn("if (swept.length) refreshUnlessTyping()", text)

    def test_打っている最中は塗り直さない(self) -> None:
        """塗り直しは本文を差し替えるので、入力中の欄は消えます。"""
        text = self.source("shift_end.js")
        self.assertIn("INPUT", text)
        self.assertIn("TEXTAREA", text)

    def test_共有へ保存したら設定画面を塗り直す(self) -> None:
        text = self.source("views/settings.js")
        head = text.index('api.post("/api/settings/push"')
        self.assertIn("refresh()", text[head:head + 900])

    def test_前の直を引き継いだら塗り直す(self) -> None:
        text = self.source("views/entry.js")
        head = text.index('api.post("/api/entry/handover"')
        self.assertIn("refresh()", text[head:head + 700])


if __name__ == "__main__":
    unittest.main()
