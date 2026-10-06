"""紙の束 ── 前の直の紙が残っていることを、画面から読めるようにする

【なぜ書いたか】

    直終わりに保存が済み　次直が登録すると前直分はどうなりますか？
    消えるのですか？
    VBAのときはシートは残っているし発行すればシートが切り替わって
    まっさらになるので視覚的に変わった、新規だ　とわかるのですが
    本ツールはそこがわかりにくく　終わっているのか　終わっていないのか
    切り替わったのかそうでないのか　ここが不明瞭

**消えません。** それはこの `消えないこと` のテストで押さえます ──
次の直が保存しても、前の直の紙がそのまま残っていること。

そのうえで、**残っていることが画面から読めるか**を見ます。VBA には
シートのタブが並んでいて、束が見えていました。こちらの画面は
「2直 1ページ目 / 全1ページ」しか出しておらず、しかもこの「全1ページ」は
いまの直のぶんしか数えていません。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import sheet_strip
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase

DAY = "2026年9月17日"


def saved(shift: str, page: int, *, synced: bool = False,
          report_date: str = DAY, line: str = "L-1") -> dict:
    return {"report_date": report_date, "line": line, "shift": shift,
            "page": page, "synced": synced}


def build(rows, **kw):
    kw.setdefault("report_date", DAY)
    kw.setdefault("line", "L-1")
    return sheet_strip.build(rows, **kw)


def labels(strip) -> list[str]:
    return [s.label for s in strip.sheets]


# ======================================================================
# 1. 並べ方 (Flask が無くても通る)
# ======================================================================
class OrderTests(unittest.TestCase):
    """**直 → ページ の順。** 保存した順ではありません。"""

    def test_直の順に並ぶ(self) -> None:
        strip = build([saved("2直", 1), saved("1直", 1), saved("3直", 1)])
        self.assertEqual(labels(strip), ["9月17日 1直 1ページ目", "9月17日 2直 1ページ目", "9月17日 3直 1ページ目"])

    def test_同じ直はページの順(self) -> None:
        strip = build([saved("1直", 3), saved("1直", 1), saved("1直", 2)])
        self.assertEqual(labels(strip),
                         ["9月17日 1直 1ページ目", "9月17日 1直 2ページ目", "9月17日 1直 3ページ目"])

    def test_知らない直は後ろ(self) -> None:
        """マスタが増えても並びが崩れないように。"""
        strip = build([saved("4直", 1), saved("1直", 1)])
        self.assertEqual(labels(strip)[0], "9月17日 1直 1ページ目")

    def test_別の日は混ぜない(self) -> None:
        strip = build([saved("1直", 1), saved("1直", 1, report_date="2026年9月16日")])
        self.assertEqual(len(strip.sheets), 1)

    def test_別のラインも混ぜない(self) -> None:
        """紙はライン1本ぶんです ── 隣のラインの紙は別の束。"""
        strip = build([saved("1直", 1), saved("1直", 2, line="LVC")])
        self.assertEqual(labels(strip), ["9月17日 1直 1ページ目"])

    def test_壊れた行は飛ばす(self) -> None:
        """**読めない行があっても束は出す。** 束のために画面を止めない。"""
        strip = build([{"report_date": DAY, "line": "L-1", "shift": "", "page": 1},
                       {"report_date": DAY, "line": "L-1", "shift": "1直", "page": 0},
                       {"report_date": DAY, "line": "L-1", "shift": "1直",
                        "page": "ページ"},
                       saved("1直", 1)])
        self.assertEqual(labels(strip), ["9月17日 1直 1ページ目"])


class NowTests(unittest.TestCase):
    """**いま開いている紙。** ここが動くことが「切り替わった」の合図。"""

    def test_いまの紙に印がつく(self) -> None:
        strip = build([saved("1直", 1), saved("2直", 1)],
                      current_shift="2直", current_page=1)
        now = [s.label for s in strip.sheets if s.now]
        self.assertEqual(now, ["9月17日 2直 1ページ目"])

    def test_いまの紙は1枚だけ(self) -> None:
        strip = build([saved("1直", 1), saved("1直", 2)],
                      current_shift="1直", current_page=2)
        self.assertEqual(sum(1 for s in strip.sheets if s.now), 1)

    def test_発行したての紙も束に出る(self) -> None:
        """**ここが抜けると、切り替わった直後だけ姿が消えます。**

        次ページを出した直後・直が変わった直後は、まだ1度も保存して
        いません。束から消えていたら「新しい紙に移った」が見えません。
        """
        strip = build([saved("1直", 1)], current_shift="2直", current_page=1)
        self.assertEqual(labels(strip), ["9月17日 1直 1ページ目", "9月17日 2直 1ページ目"])
        new = strip.sheets[1]
        self.assertTrue(new.now)
        self.assertFalse(new.saved)
        self.assertEqual(new.mark, sheet_strip.MARKS[sheet_strip.STATE_NEW])

    def test_保存済みなら新しい紙とは言わない(self) -> None:
        strip = build([saved("2直", 1)], current_shift="2直", current_page=1)
        self.assertTrue(strip.sheets[0].saved)
        self.assertEqual(strip.sheets[0].state, sheet_strip.STATE_PENDING)

    def test_いまの紙が無くても束は出る(self) -> None:
        """記録を見るから過去を開いているときなど。"""
        strip = build([saved("1直", 1)])
        self.assertEqual(len(strip.sheets), 1)
        self.assertFalse(strip.sheets[0].now)


class StateTests(unittest.TestCase):
    """**終わっているのか、終わっていないのか。** 字で書きます。"""

    def test_共有へ出ていれば済(self) -> None:
        strip = build([saved("1直", 1, synced=True)])
        self.assertEqual(strip.sheets[0].state, sheet_strip.STATE_SYNCED)
        self.assertEqual(strip.sheets[0].mark, "共有済")

    def test_手元だけなら未送信(self) -> None:
        strip = build([saved("1直", 1)])
        self.assertEqual(strip.sheets[0].mark, "未送信")

    def test_記号にしない(self) -> None:
        """**「済」「未」だけでは何が済んだのか読めません。**

        停止記号で同じことを言われました(「停止内訳にない記号です」)。
        """
        for mark in sheet_strip.MARKS.values():
            self.assertGreaterEqual(len(mark), 3, f"{mark} は短すぎる")


class OpenTests(unittest.TestCase):
    """押したときの行き先。**同じ直は切り替え、別の直は読むだけ。**"""

    def strip(self):
        return build([saved("1直", 1), saved("2直", 1), saved("2直", 2)],
                     current_shift="2直", current_page=1).as_dict()

    def sheet(self, label):
        for s in self.strip()["sheets"]:
            if s["label"] == label:
                return s
        raise AssertionError(f"{label} が束にない")

    def test_いまの紙は押しても動かない(self) -> None:
        self.assertEqual(self.sheet("9月17日 2直 1ページ目")["open_as"],
                         sheet_strip.OPEN_NOTHING)

    def test_同じ直の別ページはその場で切り替え(self) -> None:
        """自分がさっき打った紙です ── 管理者モードは要りません。"""
        self.assertEqual(self.sheet("9月17日 2直 2ページ目")["open_as"],
                         sheet_strip.OPEN_PAGE)

    def test_別の直は読むだけ(self) -> None:
        """**ここから書き換えられるようにしません。**

        直すのは「記録を見る」から呼び出す道(管理者モード)です ──
        「修正を誰でも出来るようにするのは運用上出来ない」。
        """
        self.assertEqual(self.sheet("9月17日 1直 1ページ目")["open_as"],
                         sheet_strip.OPEN_PAPER)

    def test_押すと何が起きるかを札に書く(self) -> None:
        self.assertIn("読めます", self.sheet("9月17日 1直 1ページ目")["note"])
        self.assertIn("切り替わります", self.sheet("9月17日 2直 2ページ目")["note"])


class SummaryTests(unittest.TestCase):
    """束の上の一行。**「消えていない」を言葉でも言う。**"""

    def test_1枚だけならそう言う(self) -> None:
        strip = build([], current_shift="1直", current_page=1)
        self.assertIn("1枚だけ", strip.summary)

    def test_前の直のぶんが残っていると言う(self) -> None:
        strip = build([saved("1直", 1, synced=True)],
                      current_shift="2直", current_page=1)
        self.assertIn("2枚", strip.summary)
        self.assertIn("残っています", strip.summary)

    def test_同じ直の2枚目に前の直とは書かない(self) -> None:
        """**嘘を書かない。** 別の直が1枚も無いのに「別の直のぶんも
        残っています」と書くと、読んだ人は前の直を探しにいきます。
        """
        strip = build([saved("1直", 1)], current_shift="1直", current_page=2)
        self.assertNotIn("別の直", strip.summary)
        self.assertIn("切り替わります", strip.summary)

    def test_未送信の数まで出す(self) -> None:
        strip = build([saved("1直", 1), saved("1直", 2)],
                      current_shift="2直", current_page=1)
        self.assertIn("2枚は共有へ未送信", strip.summary)

    def test_束が空なら何も言わない(self) -> None:
        self.assertEqual(build([]).summary, "")

    def test_文言に印を混ぜない(self) -> None:
        strip = build([saved("1直", 1)], current_shift="2直", current_page=1)
        self.assertNotIn("**", strip.summary)
        for sheet in strip.as_dict()["sheets"]:
            self.assertNotIn("**", sheet["note"])


# ======================================================================
# 2. 実際に消えないこと・画面に出ること (Flask が要る)
# ======================================================================
@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class SurvivesTests(WebTestCase):
    """**次の直が保存しても、前の直の紙は消えません。**

    これが本題です。束はそれを見せるための道具で、消えないことそのものは
    保存の鍵(報告日・ライン・直・ページ)が別であることから来ます。
    """

    def sheet(self, lot: str, worker: str) -> dict:
        return {"rows": {"1": {"LOT": lot, "ZAI": "SPCC", "SIZ": "1.0",
                               "KEN": "10", "KZ": "08", "KH": "00",
                               "SZ": "09", "SH": "00", "HIT": "1",
                               "MAI": "10", "TUT": "1"}},
                "header": {"worker": worker}, "checks": {}}

    def test_前の直の12行が残っている(self) -> None:
        """**次の直が保存しても、前の直の紙には触りません。**

        保存の鍵は 報告日・ライン・直・ページ の4つで、`repository.save`
        の DELETE もその4つが一致する紙にしか当たりません。時計が何直を
        指していても同じなので、**いまの直と別の直**で確かめます。
        """
        from nippou import constants
        from nippou.logic.shift import SHIFT_NAMES

        line = constants.LINE_NAMES[0]
        first = self.post("/api/entry/save",
                          self.sheet("1111111", "前の直の山田")).get_json()
        day, shift = first["report_date"], first["shift"]

        repo = self.repo()
        head, details = repo.load(day, line, shift, 1)
        self.assertEqual(head.worker, "前の直の山田")
        self.assertEqual([d.lot for d in details if d.lot], ["1111111"])

        # **別の直の紙として、もう1枚保存する**
        other = next(n for n in SHIFT_NAMES if n != shift)
        repo.save(*self._as_other_shift(head, details, other, "2222222",
                                        "次の直の田中"))

        # 前の直のぶんはそのまま。束にも2枚そろう
        again, kept = repo.load(day, line, shift, 1)
        self.assertEqual(again.worker, "前の直の山田")
        self.assertEqual([d.lot for d in kept if d.lot], ["1111111"])
        pages = {(r["shift"], r["page"]) for r in repo.saved_keys(line)}
        self.assertIn((shift, 1), pages)
        self.assertIn((other, 1), pages)

    def _as_other_shift(self, head, details, shift, lot, worker):
        import copy

        head2 = copy.deepcopy(head)
        head2.shift, head2.worker = shift, worker
        rows = []
        for d in copy.deepcopy(details):
            d.shift = shift
            if d.lot:
                d.lot = lot
            rows.append(d)
        return head2, rows


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class StripRouteTests(WebTestCase):
    """束が応答にも画面にも出ているか。"""

    def state(self, worker: str = "山田") -> dict:
        return self.post("/api/entry/state", {
            "rows": {}, "header": {"worker": worker}, "checks": {}}).get_json()

    def test_応答に束が入っている(self) -> None:
        body = self.state()
        self.assertIn("sheets", body)
        for key in ("report_date", "line", "count", "summary", "sheets"):
            self.assertIn(key, body["sheets"])

    def test_いまの紙が束に出ている(self) -> None:
        """**まだ1行も保存していなくても出ます**(発行したての紙)。"""
        body = self.state()
        now = [s for s in body["sheets"]["sheets"] if s["now"]]
        self.assertEqual(len(now), 1)
        self.assertEqual(now[0]["shift"], body["shift"])
        self.assertEqual(now[0]["page"], body["page"])

    def test_画面に束の置き場がある(self) -> None:
        html = self.get("/").get_data(as_text=True)
        self.assertIn('id="sheets"', html)
        self.assertIn('id="sheets-row"', html)
        self.assertIn('id="sheets-note"', html)

    def test_束は表のすぐ上にある(self) -> None:
        """**打っているあいだ映っているのは表だけ。** 上のカードに置くと
        見えません(`sheet-now` と同じ理由)。
        """
        html = self.get("/").get_data(as_text=True)
        self.assertLess(html.index('id="sheets"'), html.index('id="sheet-now"'))
        self.assertLess(html.index('id="sheet-now"'), html.index('class="grid"'))

    def test_束が自分で鍵を持っている(self) -> None:
        """**隣から拾わない。**

        日付とラインを `#sheet-key` から拾っていたら、サーバが描いた
        時点ではライン名が入っておらず(あれは応答が来てから JS が
        足します)、開いた直後に札を押すと空の紙が開きました。
        押し先が要る値は、押す部品の側に置きます。
        """
        html = self.get("/").get_data(as_text=True)
        head = html.index('id="sheets"')
        near = html[head - 200:head + 200]
        self.assertIn("data-sheets-date=", near)
        self.assertIn("data-sheets-line=", near)
        # 空で描かれていない ── 空なら紙が開きません
        self.assertNotIn('data-sheets-line=""', near)

    def test_読み上げにも押したあとが分かる(self) -> None:
        body = self.state()
        for sheet in body["sheets"]["sheets"]:
            self.assertTrue(sheet["note"], "札に一行が無い")


if __name__ == "__main__":
    unittest.main()
