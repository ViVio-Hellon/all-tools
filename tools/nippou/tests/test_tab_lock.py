"""打てるタブは1枚 ── **2枚に打たせない** (`nippou/logic/tab_lock.py`)

【なぜ機械で見るのか】
アプリを2つ起動する話は前に直しました。**タブはもっと簡単に増えます** ──
指1本です。そして壊れ方は同じです。

    タブA: 1〜6行目を打って保存      → DBには6行
    タブB: (Aを開く前の画面のまま)
           7行目だけ打って保存       → Bが持っていた形で上書き
                                      → **Aの6行が消える**

どちらのタブも正常に見えています。気づくのは翌日「打ったはずの直が
無い」という形 ── **壊れたことが見えない不具合**なので、人が触って
確かめることを当てにできません。

ここで押さえるのは3つです。

    1. 2枚目は**見るだけ**になる。1枚目の権利は勝手に動かない
    2. 打てない側からの**書き込みはサーバが断る**(画面の無効化は見た目)
    3. 1枚目が閉じたら、2枚目は**自分で押さなくても**打てるようになる
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import tab_lock  # noqa: E402
from tests._web import HEADERS, WebTestCase  # noqa: E402


class DeskTests(unittest.TestCase):
    """机(`TabDesk`)そのもの。時刻は渡すので、待たずに試せる。"""

    def setUp(self) -> None:
        self.desk = tab_lock.TabDesk()

    def test_1枚目は打てる(self) -> None:
        self.assertTrue(self.desk.claim("A", now=0.0).may_edit)

    def test_2枚目は見るだけ(self) -> None:
        """**ここが本体。** 開くのは止めないが、打たせない。"""
        self.desk.claim("A", now=0.0)
        verdict = self.desk.claim("B", now=1.0)
        self.assertFalse(verdict.may_edit)
        self.assertEqual(verdict.role, tab_lock.ROLE_VIEWER)

    def test_2枚目が開いても1枚目は打てるまま(self) -> None:
        """後から開いたほうに権利が移ると、**打っている最中に足元が
        崩れます。** 移すのは押したときだけ。"""
        self.desk.claim("A", now=0.0)
        self.desk.claim("B", now=1.0)
        self.assertTrue(self.desk.ping("A", now=2.0).may_edit)

    def test_何枚開いているかを返す(self) -> None:
        self.desk.claim("A", now=0.0)
        self.desk.claim("B", now=0.0)
        self.desk.claim("C", now=0.0)
        self.assertEqual(self.desk.claim("A", now=0.0).others, 2)

    def test_押せば奪える(self) -> None:
        """「このタブで入力する」。**押したときだけ動く。**"""
        self.desk.claim("A", now=0.0)
        self.desk.claim("B", now=1.0)
        self.assertTrue(self.desk.take("B", now=2.0).may_edit)
        self.assertFalse(self.desk.ping("A", now=3.0).may_edit)

    def test_閉じたら次が打てる(self) -> None:
        """ふつうに閉じたときは、その場で分かる ── 20秒黙って待たせない。"""
        self.desk.claim("A", now=0.0)
        self.desk.claim("B", now=1.0)
        self.desk.release("A", now=2.0)
        self.assertTrue(self.desk.ping("B", now=3.0).may_edit)

    def test_落ちたら心拍が途切れて次が打てる(self) -> None:
        """閉じる合図が届かないこともあります(落ちた・電源が切れた)。

        **待つのは心拍3回ぶん。** 1回や2回の取りこぼし(重い保存の最中
        など)で権利が動くと、打っている人の足元が崩れます。
        """
        self.desk.claim("A", now=0.0)
        self.desk.claim("B", now=0.0)
        # まだ待つ
        late = tab_lock.LOST_AFTER_SEC - 0.1
        self.assertFalse(self.desk.ping("B", now=late).may_edit)
        # ここで諦める
        self.assertTrue(
            self.desk.ping("B", now=tab_lock.LOST_AFTER_SEC + 1).may_edit)

    def test_心拍が続くかぎり取られない(self) -> None:
        self.desk.claim("A", now=0.0)
        self.desk.claim("B", now=0.0)
        for step in range(1, 20):
            now = step * tab_lock.HEARTBEAT_SEC
            self.desk.ping("A", now=now)
            self.assertFalse(self.desk.ping("B", now=now).may_edit,
                             f"{now}秒で取られました")

    def test_知らないタブの心拍は開いたものとして扱う(self) -> None:
        """アプリを立て直したあとの画面がここへ来ます。**断らない** ──
        断ると、理由の分からないまま打てなくなります。"""
        self.assertTrue(self.desk.ping("A", now=0.0).may_edit)

    def test_誰も居なければ書いてよい(self) -> None:
        """心拍より先に保存が飛ぶことがあります(打ち終わって即保存)。
        そこで断ると、**打った分が消えます。**"""
        self.assertTrue(self.desk.may_edit("A", now=0.0))

    def test_打てない側は書けない(self) -> None:
        self.desk.claim("A", now=0.0)
        self.desk.claim("B", now=0.0)
        self.assertTrue(self.desk.may_edit("A", now=1.0))
        self.assertFalse(self.desk.may_edit("B", now=1.0))


class TabApiTests(WebTestCase):
    """画面との受け渡し。"""

    def setUp(self) -> None:
        super().setUp()
        tab_lock.reset_desk()
        self.addCleanup(tab_lock.reset_desk)

    def as_tab(self, name: str) -> dict:
        return {**HEADERS, "X-Tab": name}

    def post(self, path: str, tab: str, data=None):
        return self.client.post(path, headers=self.as_tab(tab),
                                json=data or {})

    def test_名乗れば役が返る(self) -> None:
        body = self.post("/api/tab/claim", "A").get_json()
        self.assertEqual(body["role"], "editor")
        self.assertTrue(body["may_edit"])

    def test_2枚目は見るだけで返る(self) -> None:
        self.post("/api/tab/claim", "A")
        body = self.post("/api/tab/claim", "B").get_json()
        self.assertFalse(body["may_edit"])
        self.assertEqual(body["others"], 1)

    def test_心拍の間隔も渡す(self) -> None:
        """**画面が勝手に決めない。** 見送る長さと組で意味があるので。"""
        body = self.post("/api/tab/claim", "A").get_json()
        self.assertEqual(body["heartbeat_sec"], tab_lock.HEARTBEAT_SEC)

    def test_名札が無ければ断る(self) -> None:
        res = self.client.post("/api/tab/claim", headers=HEADERS, json={})
        self.assertEqual(res.status_code, 400)

    def test_閉じたことは本文でも受ける(self) -> None:
        """`sendBeacon` は**ヘッダを一切付けられない。**

        ヘッダだけを見ていると、閉じたことが永遠に届きません ──
        次のタブは心拍が途切れるまで(15秒)打てないままになります。
        """
        self.post("/api/tab/claim", "A")
        self.post("/api/tab/claim", "B")
        res = self.client.post("/api/tab/release", headers=HEADERS,
                               json={"tab": "A"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(self.post("/api/tab/ping", "B").get_json()["may_edit"])


class WriteGuardTests(WebTestCase):
    """**画面を無効にするだけでは足りない。**

    無効にしたのは見た目で、要求そのものは止まっていません ── 戻る/進む、
    開きっぱなしの古いタブ、二重送信は、どれも無効化をすり抜けます。
    """

    def setUp(self) -> None:
        super().setUp()
        tab_lock.reset_desk()
        self.addCleanup(tab_lock.reset_desk)

    def as_tab(self, name: str) -> dict:
        return {**HEADERS, "X-Tab": name}

    def two_tabs(self) -> None:
        self.client.post("/api/tab/claim", headers=self.as_tab("A"), json={})
        self.client.post("/api/tab/claim", headers=self.as_tab("B"), json={})

    def test_打てない側の保存は断る(self) -> None:
        """**ここが本体。** 断らないと、先に打ったぶんが上書きされます。"""
        self.two_tabs()
        res = self.client.post("/api/entry/save", headers=self.as_tab("B"),
                               json={})
        # 409(ぶつかった)。送られたものは正しく、いま打てる側でないだけ
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()["error"]["code"], "other_tab")

    def test_断り文には何をすればよいかを書く(self) -> None:
        self.two_tabs()
        res = self.client.post("/api/entry/save", headers=self.as_tab("B"),
                               json={})
        message = res.get_json()["error"]["message"]
        self.assertIn("もう1つのタブ", message)
        self.assertIn("このタブで入力する", message)

    def test_打てる側は通る(self) -> None:
        self.two_tabs()
        res = self.client.post("/api/entry/save", headers=self.as_tab("A"),
                               json={})
        self.assertNotEqual(res.status_code, 409)

    def test_奪えば通るようになる(self) -> None:
        self.two_tabs()
        self.client.post("/api/tab/take", headers=self.as_tab("B"), json={})
        res = self.client.post("/api/entry/save", headers=self.as_tab("B"),
                               json={})
        self.assertNotEqual(res.status_code, 409)

    def test_読むだけの口は止めない(self) -> None:
        """見るだけのタブでも**画面は組み立てられる**必要があります ──
        値が出ない・ロットが引けないと、見るためにも使えません。"""
        self.two_tabs()
        for path in ("/api/entry/state", "/api/entry/lot",
                     "/api/entry/check", "/api/entry/verify"):
            with self.subTest(path=path):
                res = self.client.post(path, headers=self.as_tab("B"),
                                       json={})
                self.assertNotEqual(res.status_code, 409, path)

    def test_日報以外は取り合わない(self) -> None:
        """グラフや集計を2枚目で開くのは**ふつうの使い方**です。
        そこまで止めると邪魔になるだけ。"""
        self.two_tabs()
        res = self.client.post("/api/gw/lot", headers=self.as_tab("B"),
                               json={"lot_no": "H5422S0"})
        self.assertNotEqual(res.status_code, 409)

    def test_1枚だけなら当然通る(self) -> None:
        self.client.post("/api/tab/claim", headers=self.as_tab("A"), json={})
        res = self.client.post("/api/entry/save", headers=self.as_tab("A"),
                               json={})
        self.assertNotEqual(res.status_code, 409)

    def test_誰も名乗っていなければ通す(self) -> None:
        """心拍より先に保存が飛ぶことがある(打ち終わって即保存)。
        **そこで断ると、打った分が消えます。**"""
        res = self.client.post("/api/entry/save", headers=self.as_tab("A"),
                               json={})
        self.assertNotEqual(res.status_code, 409)


class ScreenTests(WebTestCase):
    """画面の側。"""

    def test_日報入力だけが取り合う(self) -> None:
        """打つ画面はここだけ。**グラフまで取り合わない。**"""
        root = Path(__file__).resolve().parent.parent
        entry = (root / "app" / "static" / "js" / "views"
                 / "entry.js").read_text(encoding="utf-8")
        self.assertIn("tab_lock.js", entry)
        self.assertIn("tabLock.start()", entry)
        for name in ("graph", "agg", "gw"):
            with self.subTest(name=name):
                text = (root / "app" / "static" / "js" / "views"
                        / f"{name}.js").read_text(encoding="utf-8")
                self.assertNotIn("tab_lock.js", text)

    def test_2枚目は1枚に差し替わる(self) -> None:
        """**帯では足りませんでした。**

        薄くして触れなくしても、12行の表も保存の並びもそこにあるので、
        画面としては日報入力のままです ── どちらのタブで打っているのかは、
        結局その帯を読まないと分かりません。
        「2枚目を開いた時点で『別のタブで開いています』の1枚に差し替える」。
        """
        root = Path(__file__).resolve().parent.parent
        js = (root / "app" / "static" / "js"
              / "tab_lock.js").read_text(encoding="utf-8")
        self.assertIn("別のタブで開いています", js)
        self.assertIn("このタブで入力する", js)
        # 本体を隠すのは CSS。JS で `display` を1つずつ触らない
        # (戻すときに元の値が分からなくなる)
        self.assertNotIn(".style.display", js)
        css = (root / "app" / "static" / "css"
               / "components.css").read_text(encoding="utf-8")
        self.assertIn("body.is-viewer #main > *:not(#tab-lock){ display:none; }",
                      css)

    def test_隠れていても押せなくしておく(self) -> None:
        """隠すのは見た目の話。焦点はキーボードで入り込めます。"""
        root = Path(__file__).resolve().parent.parent
        js = (root / "app" / "static" / "js"
              / "tab_lock.js").read_text(encoding="utf-8")
        self.assertIn("el.disabled = true", js)
        # **触るのは `#main` の中だけ。** 帯の「終了」や左の並びまで
        # 灰色にすると、このタブが壊れたように見えます
        self.assertIn('document.getElementById("main")', js)

    def test_他の画面までは差し替えない(self) -> None:
        """記録・グラフ・集計は2枚目でも見られます。

        差し替えたまま画面を移ると、そちらが真っ白になります ──
        取り合っているのは「打つ画面」だけです。
        """
        root = Path(__file__).resolve().parent.parent
        js = (root / "app" / "static" / "js"
              / "tab_lock.js").read_text(encoding="utf-8")
        leave = js[js.index("onLeave(() => {"):]
        self.assertIn("unpaint();", leave[:leave.index("});")])
        body = js[js.index("function unpaint()"):]
        body = body[:body.index("\n}")]
        self.assertIn('classList.remove("is-viewer")', body)
        # **心拍は止めない。** 止めると、グラフを見に行って戻ってきた
        # だけで打つ権利を落とします
        self.assertNotIn("clearInterval", body)

    def test_名札は全部の要求に付く(self) -> None:
        """書き込みの口が見るので、**付け忘れる場所を作らない。**"""
        root = Path(__file__).resolve().parent.parent
        text = (root / "app" / "static" / "js"
                / "api.js").read_text(encoding="utf-8")
        self.assertIn('"X-Tab"', text)
        # タブごとに別で、読み直しでは消えない寿命
        self.assertIn("sessionStorage", text)


if __name__ == "__main__":
    unittest.main()
