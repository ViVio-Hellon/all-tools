"""この直の直すところが、**押す前から入力画面に出ているか**

【なぜ書いたか】

    設定タブ内で不備は当然確認できるとしても（管理者用）
    入力しか一般作業者は開かないので
    そこで何故ダメかが見えないと意味がないかと

7項目の結果が画面に出るのは、これまで**何かを押して断られたとき**
だけでした:

    「この直をチェック」を押した
    「保存(確定)」に断られた
    「共有へ保存」に断られた

前の直のぶんは引き継ぎの帯(`logic/handover.py`)が理由まで出したままに
しています。ところが**自分がいま打っている直**は、直の終わりに
「共有へ保存」を押して断られるまで分からず、しかも画面を塗り直すと
消えていました。

ここで確かめるのは3つです:

    1. 開いただけで、いまの直の直すところが応答に入っている
    2. 「いま直すもの」と「直の終わりまでに」が**分かれている**
    3. 確かめられなかったときに「ありません」と言わない
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import entry_findings
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase


def finding(message="梱包数が検入枚数を超えています", *, at_end=False,
            page=1, row=1, how="") -> dict:
    return {"code": "shift_end" if at_end else "over_count",
            "message": message, "how": how, "page": page, "row": row,
            "field": "", "sound": "", "skippable": at_end,
            "where": f"{page}ページ {row}行目", "at_shift_end": at_end}


# ======================================================================
# 1. 分け方 (Flask が無くても通る)
# ======================================================================
class SplitTests(unittest.TestCase):
    """**「いま直す」と「直の終わりまでに」は別の話。**"""

    def test_いまのものと終わりのものを分ける(self) -> None:
        found = entry_findings.split([finding(), finding(at_end=True)])
        self.assertEqual(len(found.now), 1)
        self.assertEqual(len(found.at_end), 1)

    def test_直の終わりのものだけなら赤くしない(self) -> None:
        """**ここが要点。** 8時に1行目を打った時点で最終時間と休憩は
        必ず足りません ── 赤にすると1行目から赤い画面になります。
        """
        found = entry_findings.split([finding(at_end=True)])
        self.assertEqual(found.level, entry_findings.LEVEL_TODO)
        self.assertIn("ここまで直すところはありません", found.headline)
        self.assertIn("直の終わりまでに", found.headline)

    def test_いま直すものがあれば赤(self) -> None:
        found = entry_findings.split([finding()])
        self.assertEqual(found.level, entry_findings.LEVEL_ERROR)
        self.assertIn("1件", found.headline)
        self.assertIn("共有へ保存できません", found.headline)

    def test_両方あれば数を両方出す(self) -> None:
        found = entry_findings.split(
            [finding(), finding(), finding(at_end=True)])
        self.assertIn("2件", found.headline)
        self.assertIn("直の終わりまでに 1件", found.headline)

    def test_何も無ければそう言う(self) -> None:
        found = entry_findings.split([])
        self.assertEqual(found.level, entry_findings.LEVEL_OK)
        self.assertIn("直すところはありません", found.headline)
        self.assertFalse(found.any_left)

    def test_確かめられなければ何も言わない(self) -> None:
        """**「ありません」と出さない。**

        1ページも保存されていないのに「直すところはありません」と出すと、
        確かめていないことを確かめたことにします ── そちらのほうが危ない。
        """
        found = entry_findings.none()
        self.assertFalse(found.counted)
        self.assertEqual(found.level, entry_findings.LEVEL_NONE)
        self.assertEqual(found.headline, "")

    def test_鍵をそのまま持ち回る(self) -> None:
        found = entry_findings.split([finding()], report_date="2026年9月17日",
                                     line="L-1", shift="2直")
        body = found.as_dict()
        self.assertEqual(body["report_date"], "2026年9月17日")
        self.assertEqual(body["line"], "L-1")
        self.assertEqual(body["shift"], "2直")

    def test_文言に印を混ぜない(self) -> None:
        for rows in ([], [finding()], [finding(at_end=True)]):
            self.assertNotIn("**", entry_findings.split(rows).headline)


# ======================================================================
# 2. 画面に出ているか (Flask が要る)
# ======================================================================
@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class EntryScreenTests(WebTestCase):
    """**一般作業者が開く画面に、何故ダメかが出ているか。**"""

    def sheet(self, **rows) -> dict:
        row = {"LOT": "1111111", "ZAI": "SPCC", "SIZ": "1.0",
               "KEN": "10", "KZ": "08", "KH": "00", "SZ": "09", "SH": "00",
               "HIT": "1", "MAI": "10", "TUT": "1"}
        row.update(rows)
        return {"rows": {"1": row}, "header": {"worker": "山田"}, "checks": {}}

    def test_保存前は数えない(self) -> None:
        """1ページも保存されていない ── 確かめようがありません。"""
        body = self.post("/api/entry/state", {
            "rows": {}, "header": {}, "checks": {}}).get_json()
        self.assertIn("shift_findings", body)
        self.assertFalse(body["shift_findings"]["counted"])

    def test_保存したら押さなくても出る(self) -> None:
        """**これが本題。** 押していないのに、応答に入っていること。"""
        saved = self.post("/api/entry/save", self.sheet()).get_json()
        self.assertTrue(saved["saved"])
        found = saved["shift_findings"]
        self.assertTrue(found["counted"])
        # 直の途中なので、最終時間と休憩は必ず残っている
        self.assertTrue(found["at_end"], "直の終わりまでのぶんが出ていない")
        self.assertEqual(found["level"], "todo")
        self.assertIn("ここまで直すところはありません", found["headline"])

    def test_開き直しても消えない(self) -> None:
        """**押して出したものは塗り直すと消えていました。** 帯は残ります。"""
        self.post("/api/entry/save", self.sheet())
        body = self.post("/api/entry/state", {
            "rows": {}, "header": {"worker": "山田"}, "checks": {}}).get_json()
        self.assertTrue(body["shift_findings"]["counted"])
        self.assertTrue(body["shift_findings"]["at_end"])

    def break_saved_row(self) -> None:
        """保存済みの行を**DBの側で**壊す(梱包数が検入枚数を超える形)。

        **保存(確定)を通しては作れません** ── そこが止める5項目の1つ
        なので。作れるのは、CSVから取り込んだぶん・「表を見る/直す」から
        書き換えたぶん・マスタの記号が後から変わったぶんです。
        **7項目がDBを読み直すのは、まさにそれを拾うため**でした。
        """
        from nippou import constants

        repo = self.repo()
        key = repo.saved_keys(constants.LINE_NAMES[0])[0]
        head, details = repo.load(key["report_date"], constants.LINE_NAMES[0],
                                  key["shift"], key["page"])
        for row in details:
            if row.lot:
                row.mai = "12"                   # 検入10枚 < 個装12枚 × 1包
        repo.save(head, details)

    def test_いま直すものは赤で出る(self) -> None:
        """梱包数が検入枚数を超えている ── 打っている途中でも間違い。"""
        self.post("/api/entry/save", self.sheet())
        self.break_saved_row()
        body = self.post("/api/entry/state", {
            "rows": {}, "header": {"worker": "山田"}, "checks": {}}).get_json()
        found = body["shift_findings"]
        self.assertEqual(found["level"], "error", found["headline"])
        self.assertTrue(found["now"])
        self.assertIn("共有へ保存できません", found["headline"])

    def test_画面に帯の置き場がある(self) -> None:
        html = self.get("/").get_data(as_text=True)
        self.assertIn('id="shift-findings"', html)
        self.assertIn('id="shift-findings-head"', html)
        self.assertIn('id="shift-findings-list"', html)

    def test_開いた最初から中身が入っている(self) -> None:
        """**応答を1つ待たせない。** 空の帯は「何も無い」と読まれます。"""
        self.post("/api/entry/save", self.sheet())
        html = self.get("/").get_data(as_text=True)
        self.assertIn('id="shift-findings-data"', html)
        head = html.index('id="shift-findings-head"')
        # 見出しはサーバが描いている(JS を待たない)
        self.assertIn("直の終わりまでに", html[head:head + 300])

    def test_いま開いているページへ行くボタンは出さない(self) -> None:
        """**目の前にあるページを「開く」と言わない。**

        見分けるのに `#sheet-key` のライン名が要ります。サーバが描いて
        いなかったころは JS が応答を待って足していたので、開いた最初の
        一瞬だけ「いま見ているページを開く」が出ていました。
        """
        html = self.get("/").get_data(as_text=True)
        head = html.index('id="sheet-key"')
        near = html[head:head + 260]
        self.assertIn("data-opened-line=", near)
        self.assertNotIn('data-opened-line=""', near)

    def test_帯は表より上にある(self) -> None:
        """**打つ前に読む場所。** 12行の下に置くと、届く前に打ち始めます。"""
        html = self.get("/").get_data(as_text=True)
        self.assertLess(html.index('id="shift-findings"'),
                        html.index('class="grid"'))

    def test_管理者でなくても出る(self) -> None:
        """**ここが肝心。** 一般作業者の画面に出ていなければ意味がない。"""
        from nippou import work_context

        self.post("/api/entry/save", self.sheet())
        self.break_saved_row()
        self.assertFalse(work_context.get_context().admin)
        body = self.post("/api/entry/state", {
            "rows": {}, "header": {"worker": "山田"}, "checks": {}}).get_json()
        self.assertTrue(body["shift_findings"]["now"])


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class MasterReadTests(WebTestCase):
    """停止記号のマスタを、**1リクエストにつき1度だけ**引いているか。

    帯を足したことで、1回の応答の中で「その他」の判定と7項目の記号
    チェックの2か所がマスタを要ります。引き先は共有の伝送用ファイルで、
    ネットワークの向こうにあることもあるので、**欄から離れるたびに
    2往復**させるわけにはいきません。
    """

    def test_応答1回につき1度(self) -> None:
        from unittest.mock import patch

        from nippou.presenters import entry as presenter

        with patch.object(presenter, "stop_choices",
                          wraps=presenter.stop_choices) as spy:
            self.post("/api/entry/state", {
                "rows": {}, "header": {"worker": "山田"}, "checks": {}})
        self.assertLessEqual(spy.call_count, 1,
                             "マスタを1回の応答で何度も引いている")


if __name__ == "__main__":
    unittest.main()
