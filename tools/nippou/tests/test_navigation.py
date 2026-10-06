"""4つの動線 ── 保存 / 呼び出し / 閲覧 / グラフ

VBA はボタン名が物(「日報DB」「graphF」)で、押すと何が起きるかが名前
から読めませんでした。しかも「過去の直を開く」が管理者タブの中に埋まり、
「見る」は印刷シートと集計シートに分かれていました。

ここで守るのは、**動詞ごとに行き先が1つ**であることです:

    保存する(手元)  日報入力の「保存(確定)」
    保存する(共有)  設定・管理者の「共有へ保存」
    呼び出す        記録を見る
    見る            記録を見る(紙) / 集計管理(表) / 集計・グラフ(グラフ)

そして**どこに居ても他の行き先が分かる**こと ── 探して回らせない。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class RailTests(unittest.TestCase):
    """レール(左の並び)。中身は素のデータだが、`app.shell` が Flask を
    読むのでここでもスキップの対象にする。"""

    def test_名前と説明が動詞でそろう(self) -> None:
        from app import shell

        notes = {key: note for key, _label, _url, note in shell.NAV}
        self.assertIn("保存", notes["entry"])
        self.assertIn("呼び出す", notes["records"])
        self.assertIn("グラフ", notes["graph"])
        self.assertIn("表", notes["agg"])
        self.assertIn("共有へ保存", notes["settings"])

    def test_記録が独立している(self) -> None:
        """設定・管理者の中に埋めない。"""
        from app import shell

        keys = [key for key, *_ in shell.NAV]
        self.assertIn("records", keys)
        self.assertIn("records", shell.READY_SCREENS)

    def test_記録は見るものの手前に来る(self) -> None:
        """入力 → 記録 → 集計 の順。設定はいちばん後ろ。"""
        from app import shell

        keys = [key for key, *_ in shell.NAV]
        self.assertLess(keys.index("entry"), keys.index("records"))
        self.assertLess(keys.index("records"), keys.index("graph"))
        self.assertEqual(keys[-1], "settings")

    def test_全部の画面が作ってある(self) -> None:
        from app import shell

        for key, *_ in shell.NAV:
            self.assertIn(key, shell.READY_SCREENS, key)


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class ScreenTests(WebTestCase):
    """実際の画面。**言葉が画面に出ているか。**"""

    def save(self, day="2026年8月3日", line="L-1", shift="1直") -> None:
        from nippou.db.models import DetailRecord, HeaderRecord

        self.repo().save(
            HeaderRecord(report_date=day, line=line, shift=shift, page=1,
                         worker="山田"),
            [DetailRecord(report_date=day, line=line, shift=shift, page=1,
                          row_no=1, lot="A1234", wei="1000", con="10")])

    def test_記録に4つの動線の地図がある(self) -> None:
        """迷ったらこの画面に来れば分かる、を作る。"""
        html = self.get("/records").get_data(as_text=True)
        for word in ("やりたいことは、どの画面か", "保存する", "呼び出す",
                     "見る", "日報入力", "集計管理", "集計・グラフ"):
            self.assertIn(word, html, word)

    def test_見ると呼び出すの違いが書いてある(self) -> None:
        """**いちばん紛らわしいのがここ。**"""
        html = self.get("/records").get_data(as_text=True)
        self.assertIn("「見る」と「呼び出す」は別のこと", html)
        self.assertIn("読むだけ", html)

    def test_見かたの帯が3画面に出る(self) -> None:
        """紙・表・グラフ。どれか1つに入り込ませない。"""
        for path in ("/records", "/agg", "/graph"):
            html = self.get(path).get_data(as_text=True)
            self.assertIn("views-switch", html, path)
            self.assertIn("見かた:", html, path)

    def test_見かたの帯はいまの場所を太字にする(self) -> None:
        cases = {"/records": "紙(この画面)", "/agg": "表(この画面)",
                 "/graph": "グラフ(この画面)"}
        for path, here in cases.items():
            html = self.get(path).get_data(as_text=True)
            self.assertIn(f"<b>{here}</b>", html, path)

    def test_設定から呼び出しの表が消えている(self) -> None:
        """日々使うものを、めったに触らない設定に置かない。"""
        html = self.get("/settings").get_data(as_text=True)
        self.assertNotIn("DB保存済み一覧", html)
        self.assertNotIn("報告日順の一覧", html)
        # 行き先は示す ── 消して黙るのがいちばん悪い
        self.assertIn("/records", html)

    def test_設定に残るのは復旧だけ(self) -> None:
        """手元が壊れたときの直し方は、日々の操作ではない。"""
        html = self.get("/settings").get_data(as_text=True)
        self.assertIn("当直をDBから復旧", html)
        self.assertIn('id="restore-shift"', html)

    def test_呼び出しは管理者でなければ押せない(self) -> None:
        self.save()
        html = self.get("/records").get_data(as_text=True)
        self.assertIn("管理者モードにすると使えます", html)
        self.assertIn("「紙を見る」は誰でも押せます", html)

    def test_紙を見るのは管理者でなくても押せる(self) -> None:
        """読むだけなので止める理由がない。"""
        self.save()
        html = self.get("/records").get_data(as_text=True)
        row = html[html.index("保存した直"):]
        self.assertIn("data-open-all", row)
        # 「紙を見る」のボタンに disabled が付いていないこと
        head = row[row.index("data-open-all"):]
        self.assertNotIn("disabled", head[:head.index("</button>")])

    def test_いまの直から3つの見方へ行ける(self) -> None:
        from nippou import work_context

        from app.routes.entry import build_shift_calculator

        ctx = work_context.get_context()
        day, line, shift = ctx.current_key(
            build_shift_calculator(self.repo().get_shift_times()))
        self.save(day, line, shift)
        html = self.get("/records").get_data(as_text=True)
        self.assertIn("紙を見る(全ページ)", html)
        self.assertIn("表で見る", html)
        self.assertIn("グラフで見る", html)

    def test_一覧に保存日時と共有の状態が出る(self) -> None:
        """**忘れられるのは共有への保存のほう。**"""
        self.save()
        html = self.get("/records").get_data(as_text=True)
        self.assertIn("最後の保存", html)
        self.assertIn("共有へ", html)

    def test_保存日時は月日と時分にする(self) -> None:
        """`2026-09-11T12:36:23` は表の1列には長すぎる。"""
        from app.routes.printing import _when

        self.assertEqual(_when("2026-09-11T12:36:23"), "09-11 12:36")
        self.assertEqual(_when("2026-09-11 12:36:23"), "09-11 12:36")
        # 読めない値は勝手に消さない
        self.assertEqual(_when("いつか"), "いつか")
        self.assertEqual(_when(None), "")

    def test_共有へ未保存ならレールに数が出る(self) -> None:
        """どの画面に居ても、やり残しが見える。"""
        self.save()
        for path in ("/", "/records", "/graph", "/agg"):
            html = self.get(path).get_data(as_text=True)
            # 「3直」が第3直に読めたので「◯直ぶん」に。どこへ行って
            # いないのかも書く(手元には保存済み)
            self.assertIn("共有へ未送信 1直ぶん", html, path)

    def test_共有へ渡したらバッジは消える(self) -> None:
        """**バッジだけを見る。**

        ここは以前ページ全体から「未保存」を探していました。画面の
        説明文に同じ言葉が出てくると落ちます ── 落ちても、バッジが
        消えていないことを意味しません。見るのは `shell._badges` が
        出す形(`未保存 <数>`)そのものにします。
        """
        import re

        self.save()
        self.repo().mark_synced(("2026年8月3日", "L-1", "1直", 1))
        html = self.get("/").get_data(as_text=True)
        self.assertIsNone(re.search(r"未保存\s*\d", html))


if __name__ == "__main__":
    unittest.main()
