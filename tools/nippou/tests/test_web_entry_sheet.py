"""日報入力の画面 ── 紙と同じ場所に、同じものを置く

【何が分からなかったか】
画面の上に 担当者・枚数・重量Kg・Lot数・係数Lot数・理由 が並んでいて、
**それが何なのかが読めませんでした。** 紙(梱包実績日報表)では別の場所に
あり、性質も違います:

    紙の1〜3行目(表の上)   作業者名 / 昼稼働 / ヨ：その他（理由を記載）
    紙の21〜26行目(表の下) 負荷計算後 / 直実績合計 ﾛｯﾄ数・枚数・重量

合計を上に置くと「まだ何も打っていないのに枚数欄がある」ように見えます。
ここは、紙と同じ並びに戻したことを守ります。

あわせて、**12行を使い切ったときの逃げ道**(VBA の「新規発行」)と、
理由を「その他」のときだけ書かせることも確かめます。
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HEADERS, WebTestCase  # noqa: E402


class SheetLayoutTests(WebTestCase):
    """合計は表の下。上は打つ前に決まるものだけ。"""

    def html(self) -> str:
        return self.client.get("/", headers=HEADERS).get_data(as_text=True)

    def test_合計は表より下に出る(self) -> None:
        """**並び順そのものが直したかったこと。** 上にあると、打つ前から
        枚数欄があるように見える。"""
        html = self.html()
        grid = html.index('id="grid-body"')
        for name in ("hd-count", "hd-weight_kg", "hd-lot_count",
                     "hd-coefficient_lot_count"):
            with self.subTest(name=name):
                self.assertGreater(html.index(f'id="{name}"'), grid,
                                   f"{name} が表より上にあります")

    def test_作業者名と理由は表より上(self) -> None:
        html = self.html()
        grid = html.index('id="grid-body"')
        self.assertLess(html.index('id="hd-worker"'), grid)
        # 理由は**行ごと**になったので、欄の入れもの(`reason-rows`)を見る
        self.assertLess(html.index('id="reason-rows"'), grid)

    def test_枚数と重量は直せない(self) -> None:
        """`Weight計算` が出す欄。打てると、合計だけを直して明細と
        食い違ったまま保存できてしまう。"""
        html = self.html()
        for name in ("hd-count", "hd-weight_kg"):
            with self.subTest(name=name):
                start = html.index(f'id="{name}"')
                tag = html[start:html.index(">", start)]
                self.assertIn("readonly", tag)

    def test_ロット数と係数は手入力のまま(self) -> None:
        """出どころ(係数処理ﾛｯﾄ数)が日報側に無いので、勝手に作らない。"""
        html = self.html()
        for name in ("hd-lot_count", "hd-coefficient_lot_count"):
            with self.subTest(name=name):
                start = html.index(f'id="{name}"')
                tag = html[start:html.index(">", start)]
                self.assertNotIn("readonly", tag)

    def test_紙の見出しで出す(self) -> None:
        html = self.html()
        self.assertIn("直実績合計", html)
        self.assertIn("作業者名(担当者)", html)

    def test_残り行数が出る(self) -> None:
        """紙は12行しかない。残りが読めないと13行目を打とうとする。"""
        html = self.html()
        self.assertIn('id="rows-left"', html)
        self.assertIn("0/12行使用(あと12行)", html)


class StepGuideTests(WebTestCase):
    """作業の順序を画面で示す(作業者 → 入力 → 保存)。"""

    def test_手順の帯が出る(self) -> None:
        html = self.client.get("/", headers=HEADERS).get_data(as_text=True)
        self.assertIn('id="steps"', html)
        for label in ("ライン", "作業者", "日報を入力", "保存(確定)"):
            with self.subTest(label=label):
                self.assertIn(label, html)

    def test_最初は作業者がいまここ(self) -> None:
        html = self.client.get("/", headers=HEADERS).get_data(as_text=True)
        start = html.index('data-step="worker"')
        self.assertIn('data-current="1"', html[start:start + 120])

    def test_担当者を入れると明細へ進む(self) -> None:
        body = self.post("/api/entry/state",
                         {"header": {"worker": "近藤雅幹"}}).get_json()
        current = [s["key"] for s in body["steps"] if s["current"]]
        self.assertEqual(current, ["rows"])


class ReasonPopupTests(WebTestCase):
    """理由は「その他」を選んだときだけ(紙の「ヨ：その他（理由を記載）」)。"""

    def _make_master(self) -> None:
        ref = self.tmp / "ref"
        ref.mkdir(exist_ok=True)
        conn = sqlite3.connect(str(ref / "伝送用ファイル.sqlite3"))
        with conn:
            for table, rows in (
                ("作業停止時間内訳_1", [("1", "休憩食事", "0"),
                                        ("2", "その他（理由を記載）", "ヨ")]),
                ("作業停止時間内訳_2", [("1", "突発停止(機械)", "イ")]),
                ("作業停止時間内訳_3", [("1", "ビニール交換", "G")]),
            ):
                conn.execute(
                    f'CREATE TABLE "{table}" ("管理番号", "内訳", "内訳番号", "備考")')
                conn.executemany(
                    f'INSERT INTO "{table}" VALUES (?, ?, ?, "")', rows)
        conn.close()
        self.post("/api/settings/paths",
                  {"gw_reference_dir": str(ref), "password": "nisk"})

    def test_選んでいなければ欄が出ない(self) -> None:
        """**欄そのものを出さない。** 読み取り専用の空欄を並べるより、
        「その他を選ぶと出る」ほうが、打つべきかどうかが分かる。"""
        self._make_master()
        html = self.client.get("/", headers=HEADERS).get_data(as_text=True)
        self.assertNotIn('data-reason="', html)
        self.assertIn('id="reason-rows"', html)

    def test_その他を選ぶと開く(self) -> None:
        self._make_master()
        body = self.post("/api/entry/state",
                         {"rows": {"4": {"SS": "ヨ"}}}).get_json()
        self.assertTrue(body["reason_open"])
        self.assertEqual(body["reason_rows"], [4])

    def test_ほかの記号では開かない(self) -> None:
        self._make_master()
        body = self.post("/api/entry/state",
                         {"rows": {"1": {"S": "0"}}}).get_json()
        self.assertFalse(body["reason_open"])
        self.assertEqual(body["reason_rows"], [])

    def test_マスタが無ければ開けたまま(self) -> None:
        """停止理由が自由入力に落ちているので、こちらだけ閉じると
        理由を書く手立てが無くなる。"""
        body = self.post("/api/entry/state", {}).get_json()
        self.assertTrue(body["reason_open"])

    def test_書いた理由は保存まで通る(self) -> None:
        self._make_master()
        self.post("/api/entry/save", {
            "rows": {"1": {"LOT": "A1"},
                     "4": {"SS": "ヨ", "reason": "棚卸し準備、点検表差し替え"}},
            "header": {},
        })
        html = self.client.get("/", headers=HEADERS).get_data(as_text=True)
        self.assertIn("棚卸し準備、点検表差し替え", html)
        # その行の欄に戻る(ページの欄ではなく)
        self.assertIn('id="reason-4"', html)


class NewPageTests(WebTestCase):
    """12行を使い切ったときの逃げ道(VBA の「新規発行」)。"""

    def _fill(self, rows: int = 12) -> dict:
        """打った紙。**最後の行は終了まで**(12行目が終了まで入って初めて
        次のページを出せる ── v4.8.0 `logic/pages.new_page_check`)。"""
        body = {str(r): {"LOT": f"H{r}22S0"} for r in range(1, rows + 1)}
        body[str(rows)].update(KZ="08", KH="00", SZ="08", SH="30")
        return {"rows": body, "header": {"worker": "近藤雅幹"}}

    def test_空のページでは断る(self) -> None:
        """押し間違いで空のページが積み上がると、ページ数だけが増える
        (`page_count` は MAX([ページ]))。"""
        res = self.post("/api/entry/newpage", {})
        self.assertEqual(res.status_code, 422)
        # 空でも12行目が空でも、断りは同じ入口(`new_page_check`)
        self.assertEqual(res.get_json()["error"]["code"], "page_not_full")

    def test_打ってあれば次のページを作る(self) -> None:
        res = self.post("/api/entry/newpage", self._fill())
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["page"], 2)

    def test_移った先は空の12行(self) -> None:
        self.post("/api/entry/newpage", self._fill())
        html = self.client.get("/", headers=HEADERS).get_data(as_text=True)
        self.assertIn("第2ページ / 全2ページ", html)
        self.assertIn("0/12行使用(あと12行)", html)
        self.assertNotIn("H122S0", html)

    def test_移る前に今のページを保存する(self) -> None:
        """**保存が先。** 先にページを進めると、いま打った12行はどこにも
        書かれないまま画面から消える。"""
        self.post("/api/entry/newpage", self._fill())
        loaded = self.repo().load(*self.repo().list_keys()[0])
        self.assertIsNotNone(loaded)
        header, details = self.repo().load(
            *[k for k in self.repo().list_keys() if k[3] == 1][0])
        self.assertEqual(details[0].lot, "H122S0")
        self.assertEqual(details[11].lot, "H1222S0")

    def test_作業者は次のページへ引き継ぐ(self) -> None:
        """同じ直の続きなので、選び直させない。"""
        self.post("/api/entry/newpage", self._fill())
        html = self.client.get("/", headers=HEADERS).get_data(as_text=True)
        self.assertIn("近藤雅幹", html)

    def test_LOT重複のままでは進めない(self) -> None:
        """重複したまま保存すると後段の集計が壊れる。保存する経路は
        どこでも同じ関門を通す。"""
        payload = self._fill()
        payload["rows"]["2"]["LOT"] = "H122S0"
        res = self.post("/api/entry/newpage", payload)
        self.assertEqual(res.status_code, 422)
        self.assertIn("H122S0", res.get_json()["message"])

    def test_呼出モード中は断る(self) -> None:
        """過去のページを直している最中に新しいページを作ると、どの直に足したのか
        押した人には分からない。"""
        self.post("/api/entry/save", self._fill())
        keys = self.repo().list_keys()
        date, line, shift, page = keys[0]
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        self.post("/api/settings/recall",
                  {"report_date": date, "line": line, "shift": shift,
                   "page": page})
        res = self.post("/api/entry/newpage", self._fill())
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"], "recall_mode")

    def test_呼出中はボタンを押せなくしておく(self) -> None:
        self.post("/api/entry/save", self._fill())
        date, line, shift, page = self.repo().list_keys()[0]
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        self.post("/api/settings/recall",
                  {"report_date": date, "line": line, "shift": shift,
                   "page": page})
        html = self.client.get("/", headers=HEADERS).get_data(as_text=True)
        start = html.index('id="new-page"')
        self.assertIn("disabled", html[start:start + 200])


if __name__ == "__main__":
    unittest.main()
