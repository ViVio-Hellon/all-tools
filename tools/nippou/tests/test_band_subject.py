"""帯の主語と、畳んだ説明

【2つの帯が言い合っているように読めた】

    上には赤文字でダメなところが書いてあって
    すぐ下には直すところはありませんって出てる
    …
    前の直が共有へ出ていません…直すところはありません…：
    すぐ上に直すところを書いてるのにどういうつもりだよ

言い合っていたのではありません。**上の赤い帯は前の直のこと**で、
**下の帯はいまの直のこと**でした。どちらにも主語が無かったので、
続きとして読めます ── 続きとして読めば、確かに矛盾します。

帯の頭に「前の直 9月12日 1直 のこと」「いまの直 9月12日 3直 のこと」
と置いて、左の線と札の色も分けます。

【長い説明は読まれない】

    この手の情報は関係ある部分にマウスムーブで見れるようにしたほうが
    良い 個人的には書いてあれば読むけどそうでない人のほうが多いよ
    (文字多いな...無視しようってなるらしい)

1行だけ残して、残りはマウスを載せたとき(`:hover`)か、キーボードで
進んだとき(`:focus`)に出します ── JS も依存も増やしません。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import entry_findings, handover, sheet_strip, shift

try:
    from tests._web import WebTestCase
    HAS_FLASK = True
except Exception:                                 # noqa: BLE001
    HAS_FLASK = False
    WebTestCase = object                          # type: ignore
SKIP = "Flask が入っていません"

ROOT = Path(__file__).resolve().parent.parent
ENTRY_HTML = (ROOT / "app/templates/entry.html").read_text(encoding="utf-8")
ENTRY_JS = (ROOT / "app/static/js/views/entry.js").read_text(encoding="utf-8")
CSS = (ROOT / "app/static/css/components.css").read_text(encoding="utf-8")

#: 主語に使う日付。**年は落とします**(帯が長くなるだけなので)
DATE = "2026年9月12日"


def left(shift_name: str = "1直", **over) -> handover.Left:
    base = dict(report_date=DATE, line="L-1", shift=shift_name)
    base.update(over)
    return handover.Left(**base)


class ShortDateTests(unittest.TestCase):
    """**書き方は1か所**(`logic/shift`)。札と帯で違う言い方をしない。"""

    def test_年を落とす(self) -> None:
        self.assertEqual(shift.short_date(DATE), "9月12日")

    def test_読めない形は空(self) -> None:
        self.assertEqual(shift.short_date("きのう"), "")
        self.assertEqual(shift.short_date(""), "")

    def test_紙の束も同じものを使う(self) -> None:
        """**2つ持たない。** 片方だけ直すと、同じ日だと読めなくなります。"""
        self.assertIs(sheet_strip.short_date, shift.short_date)

    def test_日付と直を1つにする(self) -> None:
        self.assertEqual(shift.about_shift(DATE, "3直"), "9月12日 3直")

    def test_片方しか無ければあるほうだけ(self) -> None:
        self.assertEqual(shift.about_shift("", "3直"), "3直")
        self.assertEqual(shift.about_shift(DATE, ""), "9月12日")
        self.assertEqual(shift.about_shift("", ""), "")


class PrevBandTests(unittest.TestCase):
    """前の直の帯 ── **どの直のことか**を頭に置く。"""

    def test_主語が出る(self) -> None:
        gate = handover.decide([left()], started=False)
        self.assertEqual(gate.about, "9月12日 1直")
        self.assertEqual(gate.as_dict()["who"], "前の直")

    def test_2直ぶん以上はまとめる(self) -> None:
        """**全部並べない** ── 主語のほうが長くなります。"""
        gate = handover.decide([left("1直"), left("2直")], started=False)
        self.assertEqual(gate.about, "9月12日 1直 ほか1直")

    def test_残っていなければ空(self) -> None:
        self.assertEqual(handover.decide([], started=False).about, "")

    def test_綺麗なときも主語を書く(self) -> None:
        """**ここが本題。** 「直すところはありません」が、すぐ下の
        「いまの直」の帯と言い合っているように読めていました。
        """
        gate = handover.decide([left()], started=False)
        self.assertIn("前の直に直すところはありません", gate.message)

    def test_ボタンの意味が付いてくる(self) -> None:
        """「直せないまま引き継ぐ…ボタンの意味は？」への答え。"""
        body = handover.decide([left()], started=False).as_dict()
        self.assertIn("1ページでも保存したあとは", body["why_more"])
        self.assertIn("共有へ保存する", body["why_more"])
        self.assertIn("直せないまま引き継ぐ", body["why_more"])
        self.assertTrue(body["why_line"])

    def test_飾りを画面に出さない(self) -> None:
        body = handover.decide([left()], started=False).as_dict()
        for key in ("about", "who", "why_line", "why_more", "message"):
            self.assertNotIn("**", str(body[key]), key)


class NowBandTests(unittest.TestCase):
    """いまの直の帯 ── **上の帯と対にして**読ませる。"""

    def found(self, *, at_end: bool = False):
        item = {"where": "1行目", "message": "梱包数", "at_shift_end": at_end}
        return entry_findings.split([item], report_date=DATE, shift="3直")

    def test_主語が出る(self) -> None:
        found = self.found()
        self.assertEqual(found.about, "9月12日 3直")
        self.assertEqual(found.as_dict()["who"], "いまの直")

    def test_確かめていなければ主語も出さない(self) -> None:
        self.assertEqual(entry_findings.none().about, "")

    def test_見出しがいまの直だと言い切る(self) -> None:
        """「この直」では、上の帯の前の直と読めます。"""
        self.assertIn("いまの直に、直すところが", self.found().headline)
        self.assertIn("いまの直は、ここまで",
                      self.found(at_end=True).headline)
        self.assertIn("いまの直に直すところはありません",
                      entry_findings.split([], report_date=DATE,
                                           shift="3直").headline)

    def test_上と下で主語が違う(self) -> None:
        """**対になっていること。** 同じ札だと分かれません。"""
        self.assertNotEqual(handover.WHO, entry_findings.WHO)


@unittest.skipUnless(HAS_FLASK, SKIP)
class BandScreenTests(WebTestCase):
    """画面 ── 札と日付の置き場があり、**塗り直しでも消えない**。"""

    def test_2つの帯に主語の置き場がある(self) -> None:
        html = self.get("/").get_data(as_text=True)
        self.assertIn('id="handover-about"', html)
        self.assertIn('id="shift-findings-about"', html)

    def test_形でも分ける(self) -> None:
        """色だけだと、隣どうしで「同じ話の続き」に見えます。"""
        html = self.get("/").get_data(as_text=True)
        self.assertIn("band--prev", html)
        self.assertIn("band--now", html)
        self.assertIn("band--prev", CSS)
        self.assertIn("band--now", CSS)

    def test_前の直が先(self) -> None:
        """**順番は 前の直 → いまの直。** 片付ける順です。"""
        html = self.get("/").get_data(as_text=True)
        self.assertLess(html.index('id="handover"'),
                        html.index('id="shift-findings"'))

    def test_塗り直しでも主語を入れる(self) -> None:
        """JS が帯を塗り直したときに、札だけ前のまま残らないこと。"""
        self.assertIn('getElementById("handover-about")', ENTRY_JS)
        self.assertIn('getElementById("shift-findings-about")', ENTRY_JS)

    def test_応答にも主語が乗る(self) -> None:
        body = self.post("/api/entry/state", {
            "rows": {}, "header": {}, "checks": {}}).get_json()
        self.assertIn("who", body["handover"])
        self.assertIn("about", body["shift_findings"])


class FoldedTextTests(unittest.TestCase):
    """畳んだ説明 ── **1行 + 印 + 残り**の3つで1組。"""

    #: 畳んだ説明の開き(`tip--up` 付きも拾う)
    OPEN = r'<(?:p|span) class="lead tip[^"]*"[^>]*>'

    def test_畳んだ説明がある(self) -> None:
        self.assertGreaterEqual(len(re.findall(self.OPEN, ENTRY_HTML)), 6)

    def test_1行と印と残りが揃っている(self) -> None:
        """印が無いと、**続きがあることに気づかれません**。"""
        for block in re.findall(r'class="lead tip[^"]*"(.*?)(?=</p>|\n<[a-z])',
                                ENTRY_HTML, re.S):
            self.assertIn("tip__line", block)
            self.assertIn("tip__mark", block)
            self.assertIn("tip__more", block)

    def test_残りは載せたときだけ出す(self) -> None:
        self.assertIn(".tip__more{ display:none; }", CSS)
        self.assertIn(".tip:hover .tip__more", CSS)
        self.assertIn(".tip:focus .tip__more", CSS)
        self.assertIn(".tip:focus-within .tip__more", CSS)

    def test_下の行を動かさない(self) -> None:
        """**浮かせて出します。** 押そうとしたボタンが手元で動かない。"""
        found = re.search(r"\.tip:focus-within \.tip__more\{(.*?)\}", CSS, re.S)
        self.assertIsNotNone(found)
        self.assertIn("position:absolute", found.group(1))

    def test_キーボードでも開く(self) -> None:
        """マウスが無い人にも開けること(`tabindex` が要ります)。"""
        for block in re.findall(self.OPEN, ENTRY_HTML):
            self.assertIn('tabindex="0"', block)

    def test_JSを増やしていない(self) -> None:
        """**CSS で足ります。** 出す・消すの判断が要らないので。"""
        self.assertNotIn("tip__more", ENTRY_JS)


@unittest.skipUnless(HAS_FLASK, SKIP)
class FoldedScreenTests(WebTestCase):
    """描いたあとの姿 ── **1行目が本当に1行**になっているか。"""

    def lines(self) -> list[str]:
        html = self.get("/").get_data(as_text=True)
        found = re.findall(r'class="tip__line"[^>]*>(.*?)</span>', html, re.S)
        self.assertTrue(found, "畳んだ説明が描かれていない")
        return [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", t)).strip()
                for t in found]

    def test_1行目は短い(self) -> None:
        """長い1行目は、畳んでいないのと同じです。"""
        for text in self.lines():
            self.assertLessEqual(len(text), 60, text)

    def test_1行目だけで意味が通る(self) -> None:
        """**畳んだままでも読める**こと(「…」で切らない)。"""
        for text in self.lines():
            self.assertNotIn("…", text)
            self.assertGreater(len(text), 8, text)

    def test_残りも描いてある(self) -> None:
        """出すのはブラウザ ── 中身はサーバが描いておきます。"""
        html = self.get("/").get_data(as_text=True)
        self.assertGreaterEqual(html.count('class="tip__more"'), 6)


if __name__ == "__main__":
    unittest.main()
