"""日報入力: 1行を空にする確かめと、直っていない行の印 (v4.3.0)

    1行削除での確認が×から遠いので面倒さを感じる(写真 画面上部に出る)
    入力エラー時に該当行に×が入るが 1行削除と被っているのでわかりにくい

- 確かめはブラウザの `confirm`(画面のいちばん上に出る)をやめ、**押した ✕ の
  すぐ下**に吹き出しで出す。「空にする」が ✕ の隣にあるので続けて押せる
- 直っていない行の印は ✕ をやめて、**行番号そのものを塗った札**にする
  (行番号の右には「空にする」の ✕ があるので、そこに記号を足すと紛れる)

画面の動き(位置・Esc・外を押すと閉じる)はブラウザで確かめています
(README の v4.3.0)。ここではブラウザが無くても見られる配線を確かめます。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "app" / "static" / "js" / "views" / "entry.js"
CSS = ROOT / "app" / "static" / "css" / "components.css"


class AskNearTheButtonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.js = JS.read_text(encoding="utf-8")

    # 確かめの窓(confirm)を使わない・✕ のすぐ下に出て「空にする」に焦点・どの行かを番号とロットで言う・
    # 空の行では出さない・やめる/Esc/外/巻き戻しで閉じる は、**本物のブラウザで押して確かめます**
    # (`test_row_clear_browser`。守りを外すと落ちる)。ここは作り(CSS・HTML)だけ

    def test_吹き出しはボタンに寄せて固定で出す(self) -> None:
        css = CSS.read_text(encoding="utf-8")
        rule = css[css.index(".rowask{"):]
        rule = rule[:rule.index("}")]
        self.assertIn("position:fixed", rule)
        self.assertIn('.rowno__del[aria-expanded="true"]', css)


class BadRowMarkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.css = CSS.read_text(encoding="utf-8")

    def test_印にバツを使わない(self) -> None:
        """印に ✕ を使わない(「空にする」の ✕ と紛れる)。"""
        self.assertNotRegex(self.css, r'td\.rowno::after\{\s*content:"✕"')
        self.assertNotRegex(self.css, r'data-bad="1"\]\s*td\.rowno::after')

    def test_行番号を塗った札にする(self) -> None:
        for state, color in (('data-bad="1"', "--state-error"),
                             ('data-problem="1"', "--stop-sudden")):
            with self.subTest(state=state):
                m = re.search(r"tr\[" + re.escape(state) + r"\] td\.rowno \.rowno__n\{([^}]*)\}",
                              self.css)
                self.assertIsNotNone(m)
                self.assertIn(f"background:var({color})", m.group(1))
                self.assertIn("border-radius:999px", m.group(1))


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class TemplateTests(WebTestCase):
    def test_吹き出しは1つだけ置く(self) -> None:
        html = self.get("/").get_data(as_text=True)
        self.assertEqual(html.count('id="row-ask"'), 1)
        box = html[html.index('<div class="rowask"'):]
        box = box[:box.index("</div>")]
        self.assertIn('role="alertdialog"', box)
        self.assertIn(">空にする</button>", box)
        self.assertIn(">やめる</button>", box)
        # ✕ は12行ぶん(押されたら上の吹き出しを ✕ のそばへ動かす)
        self.assertEqual(len(re.findall(r'data-clear-row="\d+"', html)), 12)


if __name__ == "__main__":
    unittest.main()
