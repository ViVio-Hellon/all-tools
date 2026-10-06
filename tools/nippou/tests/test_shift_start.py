"""直の始まり・終わりの導線 ── **何をすればいいかが画面から読めること**

【なぜ書いたか】
「直替わりの導線がわからないので今の状態もわからない」「結局初めに何を
するのかルール化が決まっていない」と言われました。開けば12行が打てる
状態なので、**直が変わったことにも、まだ何もしていないことにも、
画面からは気づけません。**

決めたルールは2つです:

    始まり … **作業者を選ぶ。** 選ぶまで表は伏せる
    終わり … **その直の時間が過ぎたら、入力画面は見るだけ。**
             直すのは 設定・管理者 → 管理者モード から

作業者を始まりにしたのは、**どのみち必ず要るもの**だからです。手間が
1つ増えるわけではなく、いつもの1つ目が「始まりの合図」も兼ねます。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HEADERS, WebTestCase


class NeedsWorkerTests(WebTestCase):
    """**始まりは作業者を選ぶこと。**"""

    def view(self) -> dict:
        return self.post("/api/entry/state", {}).get_json()

    def test_作業者が空なら始まりの帯が出る(self) -> None:
        self.assertTrue(self.view()["needs_worker"])
        html = self.get("/").get_data(as_text=True)
        self.assertIn("まず作業者を選んでください", html)
        self.assertIn('id="start-shift"', html)

    def test_選べば帯は消える(self) -> None:
        body = self.post("/api/entry/state",
                         {"header": {"worker": "杉山良宏"}}).get_json()
        self.assertFalse(body["needs_worker"])

    def test_空白だけでは選んだことにしない(self) -> None:
        body = self.post("/api/entry/state",
                         {"header": {"worker": "   "}}).get_json()
        self.assertTrue(body["needs_worker"])

    def test_始まりの帯は開いた時点で出ている(self) -> None:
        """**JSを待たない。** 待つと、開いた直後の一瞬だけ表が打てます。"""
        html = self.get("/").get_data(as_text=True)
        start = html.index('id="start-shift"')
        # `hidden` が付いていない = サーバが最初から出している
        self.assertNotIn("hidden", html[start:start + 120])

    def test_見るだけの画面では出さない(self) -> None:
        """直せないものを「直せ」と言わない。"""
        from nippou import work_context
        from nippou.db.models import DetailRecord, HeaderRecord

        key = dict(report_date="2020年1月1日", line="L-1", shift="3直", page=1)
        self.repo().save(HeaderRecord(**key),
                         [DetailRecord(**key, row_no=1, lot="OLD")])
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        self.post("/api/settings/recall",
                  {"report_date": "2020年1月1日", "line": "L-1",
                   "shift": "3直", "page": 1})
        self.post("/api/settings/admin", {"enable": False})
        body = self.view()
        self.assertTrue(body["read_only"])
        self.assertFalse(body["needs_worker"])


class ReadOnlyWordingTests(WebTestCase):
    """**時間を過ぎたら、見るだけ。** 中途半端に打てる状態を残さない。"""

    def test_帯は打てないことを言い切る(self) -> None:
        html = self.get("/").get_data(as_text=True)
        self.assertIn("この直の時間は過ぎています。見るだけです。", html)
        self.assertIn("打つことも保存することもできません", html)

    def test_直し方まで書いてある(self) -> None:
        html = self.get("/").get_data(as_text=True)
        self.assertIn("管理者モード", html)
        self.assertIn("/settings?tab=terminal", html)


class SkipBoxOnEntryTests(WebTestCase):
    """**逃げ道は、断られる場所に置く。**

    断り文は「下に『設備移動のため保存』と打つと通せます」と書いて
    いましたが、打つ欄は設定画面にしかありませんでした ── **打つ欄の
    無い画面で「下に打て」と言われる**ので、打てず、次の直へも行けません。
    """

    def test_入力画面に打つ欄がある(self) -> None:
        html = self.get("/").get_data(as_text=True)
        self.assertIn('id="entry-skip-phrase"', html)
        self.assertIn('id="entry-skip-push"', html)

    def test_ふだんは隠れている(self) -> None:
        html = self.get("/").get_data(as_text=True)
        box = html.index('id="entry-skip"')
        self.assertIn("hidden", html[box:box + 80])

    def test_合言葉はサーバの文言(self) -> None:
        from nippou.services import shift_check

        html = self.get("/").get_data(as_text=True)
        self.assertIn(shift_check.SKIP_PHRASE, html)


class RowClearTests(WebTestCase):
    """**1行ずつ消せる。** 12欄を1つずつ選んで消して回るのは手間です。"""

    def test_行ごとに消すボタンがある(self) -> None:
        html = self.get("/").get_data(as_text=True)
        for row in range(1, 13):
            with self.subTest(row=row):
                self.assertIn(f'data-clear-row="{row}"', html)

    def test_何をするか書いてある(self) -> None:
        html = self.get("/").get_data(as_text=True)
        self.assertIn("1行目を空にする", html)


if __name__ == "__main__":
    unittest.main()
