"""設定画面の面(タブ)と、鍵の見え方

【なぜ面で分けたのか】
14枚のカードが1本に積まれていて、「機能を確かめたいのに、どこに何が
あるか分からない」状態でした。**縦に積んだものは、下にあるほど無いもの
として扱われます** ── スクロールしないと見えないということは、見えて
いないあいだは思い出せないということです。

【ここで守っているもの】
1. 面の**並び・見出し・鍵の要否はサーバが決める**(`TABS`)。画面に
   べた書きすると、片方だけ直したときに食い違う
2. **置き場所は、それを使う機能と同じ面にある。** 「音の置き場所」が
   別の面にあると、ファイル名だけ直して帰ることになる
3. **鍵が要る面は、開く前にそう言う。** 押してから断られるのは手戻り
4. **鍵は1つだけ。** 同じ合言葉の入力欄が画面に2つあると、どちらを
   使うのか分からない
5. 「見つかりません」は**何が**見つからないのかまで言う
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.presenters import settings as settings_presenter
from tests._web import WebTestCase


class TabSpecTests(unittest.TestCase):
    """面の決まりごと。**画面を動かさなくても見張れるところ。**"""

    def test_面のキーは重ならない(self) -> None:
        keys = [t.key for t in settings_presenter.TABS]
        self.assertEqual(len(keys), len(set(keys)))

    def test_既定の面は実在する(self) -> None:
        keys = {t.key for t in settings_presenter.TABS}
        self.assertIn(settings_presenter.DEFAULT_TAB, keys)

    def test_多すぎない(self) -> None:
        """**面が多いと、探す手間がタブの中へ移るだけ。**

        「ただちょっと多すぎるな」と言われた14枚を分けたので、面の数を
        14に近づけたら分けた意味がありません。

        v4.1.0 で「ログ」を足して7枚にしました。困ったときに開く面なので、
        ほかの面の奥に置くと**探せない**(分ける前の困りごとの再来)── 面を
        1枚足すほうを選びました。これより増やすときは、まとめ直しを先に。
        """
        self.assertLessEqual(len(settings_presenter.TABS), 7)

    def test_どのパスもどこかの面に置かれている(self) -> None:
        """置き場所の欄が、どの面にも無いと**画面から消えます。**"""
        from nippou import config

        missing = [k for k in config.PATH_KEYS
                   if k not in settings_presenter.PATH_TAB]
        self.assertEqual(missing, [], f"面の決まっていない欄: {missing}")

    def test_面のキーは実在する面を指す(self) -> None:
        keys = {t.key for t in settings_presenter.TABS}
        for mapping in (settings_presenter.PATH_TAB, settings_presenter.FILE_TAB):
            for setting_key, tab in mapping.items():
                with self.subTest(key=setting_key):
                    self.assertIn(tab, keys)

    def test_短い名前は本物の設定キーを指す(self) -> None:
        from nippou import config

        for name, key in settings_presenter.PATH_KEY_NAMES.items():
            with self.subTest(name=name):
                self.assertIn(key, config.PATH_KEYS)

    def test_鍵の説明に飾りを書かない(self) -> None:
        """`lock` は**そのまま画面に出る文字**です。"""
        for tab in settings_presenter.TABS:
            with self.subTest(tab=tab.key):
                self.assertNotIn("**", tab.lock)
                self.assertNotIn("**", tab.note)


class TabScreenTests(WebTestCase):
    """画面に面が出ているか。"""

    def html(self) -> str:
        return self.get("/settings").get_data(as_text=True)

    def test_全部の面が出る(self) -> None:
        html = self.html()
        for tab in settings_presenter.TABS:
            with self.subTest(tab=tab.key):
                self.assertIn(f'data-key="{tab.key}"', html)
                self.assertIn(f'id="panel-{tab.key}"', html)
                self.assertIn(tab.label, html)

    def test_見出しのn件の中身をその面に並べる(self) -> None:
        """画面に出していない欄(共有の日報管理のファイル名など)の困りごとも数えるので、
        中身を見せないと「何もないのに 1件」になる。"""
        import re
        html = self.html()
        badge = re.search(r'data-key="paths".*?<span class="tab__badge"[^>]*>(\d+)件</span>', html, re.S)
        self.assertIsNotNone(badge)
        self.assertIn('id="paths-problems"', html)
        self.assertIn(f"見出しの「{badge.group(1)}件」は次のとおりです", html, "見出しの数と中身が合う")
        self.assertIn('title="共有の日報管理のパス', html, "見出しの印にも中身を添える")

    def test_使っていない欄は困りごとに数えない(self) -> None:
        """仕掛の2つ目は空なら使わない。数えていたので、何も無いのに「1件」と出ていた。"""
        html = self.html()
        box = html[html.index('id="paths-problems"'):]
        box = box[:box.index("</div>")]
        self.assertNotIn("2つ目", box)

    def test_表の行を直す窓は欄を広くとる(self) -> None:
        self.assertIn('class="stack-fields stack-fields--wide" id="row-fields"', self.html())
        css = (Path(__file__).resolve().parent.parent / "app" / "static" / "css" / "components.css"
               ).read_text(encoding="utf-8")
        self.assertIn(".stack-fields--wide .stack-field{", css)

    def test_既定の面に印が付く(self) -> None:
        self.assertIn('data-default="1"', self.html())

    def test_パスは参照設定の面だけにある(self) -> None:
        """**同じ種類の操作は、同じ1か所で。**

        一度は「置き場は、それを使う機能と同じ面へ」で散らしました。
        触ってもらうと逆でした ── 面ごとに保存ボタンがあり、欄の並びも
        揃っていないので、「設定の仕方が全部違う」状態です。パスを
        決めるのは据え付けのときの1つの作業なので、場所も1つにします。
        """
        html = self.html()
        panel = self._panel(html, "paths")
        for key in settings_presenter.SHOWN_PATH_KEYS:
            with self.subTest(key=key):
                self.assertIn(f'id="path-{key}"', panel)
        # ほかの面には欄を置かない(2か所にあると、どちらが効くか分からない)
        for other in ("output", "master", "sound", "data", "terminal"):
            with self.subTest(tab=other):
                self.assertNotIn('data-path-key=', self._panel(html, other))

    def test_フォルダだけを出す(self) -> None:
        """「ファイル名までの入力は不要」── 出すのはフォルダだけ。

        **打たせないのは名前であって、置き場所ではありません。**
        ライン毎目標のCSVは置き場所まで画面から消していたため、
        参照用マスタと同じフォルダにしか置けませんでした
        ── いまはフォルダの欄だけ出し、名前はこちらで付けます
        (`FIXED_NAME_KEYS`)。
        """
        html = self.html()
        from nippou import config
        from nippou.presenters import settings as view

        # 日報データのファイル名は出さない(既定で足りる)
        self.assertNotIn(f'id="path-{config.KEY_ACCESS_DB_FILE}"', html)
        # ライン毎目標は**フォルダとして**出す
        self.assertIn(f'id="path-{config.KEY_LINE_TARGET_FILE}"', html)
        row = {p.key: p for p in view.path_views()}[config.KEY_LINE_TARGET_FILE]
        self.assertFalse(row.is_file, "名前を打つ欄になっています")

        # 設定そのものは残っている(値が入っていれば効く・APIも受ける)
        keys = {v["key"] for v in
                self.get("/api/settings/state").get_json()["paths"]}
        self.assertIn(config.KEY_ACCESS_DB_FILE, keys)

    def test_音のファイル名だけはその面にある(self) -> None:
        """鳴らすものを選ぶのは設定ではなく好みなので、音の面に置く。"""
        panel = self._panel(self.html(), "sound")
        self.assertIn('id="save-sounds"', panel)
        self.assertIn("data-sound-file", panel)

    def test_欄ごとに保存できる(self) -> None:
        """**まとめて1つのボタンにしない。**

        どこまでが保存の対象なのかが読めず、押すたびに他の欄まで
        巻き込みます。「それぞれに保存ボタンが必要」。
        """
        panel = self._panel(self.html(), "paths")
        rows = panel.count("data-path-key=")
        self.assertEqual(panel.count("data-save-path>"), rows)
        # 打ちかけか保存済みかを出す場所も、欄ごとにある
        self.assertEqual(panel.count("data-path-state"), rows)

    def test_読み直せる(self) -> None:
        """**最初に失敗したままにしない。**

        「あります / ありません」は画面を描いたときの結果です。共有が
        後から繋がったときに確かめる先が要ります。
        """
        self.assertIn('id="paths-recheck"', self._panel(self.html(), "paths"))
        res = self.post("/api/settings/paths/recheck", {})
        self.assertEqual(res.status_code, 200)
        self.assertIn("読み直しました", res.get_json()["message"])

    def test_読み直すと手元の写しを捨てる(self) -> None:
        """捨てないと、繋がる前に作った古い写しをそのまま読みます。"""
        from unittest import mock

        with mock.patch("nippou.source_db.forget") as forget:
            self.post("/api/settings/paths/recheck", {})
        forget.assert_called_once()

    def test_使う面からは参照設定へ案内する(self) -> None:
        """パスが別の面にあるので、**どこで決めるか**を書いておく。"""
        html = self.html()
        for tab in ("output", "master", "sound"):
            with self.subTest(tab=tab):
                self.assertIn('data-goto-tab="paths"', self._panel(html, tab))

    def test_取り込みと復旧と作り直しが同じ面にある(self) -> None:
        panel = self._panel(self.html(), "data")
        self.assertIn('id="import"', panel)
        self.assertIn('id="restore-shift"', panel)
        self.assertIn('id="rebuild-summary"', panel)

    def test_面の部品を読んでいる(self) -> None:
        """面を動かすのは共有の部品(`static/js/tabs.js`)。

        **画面ごとに書き直さない。** タブは矢印キーで回れること・
        選んだ面を覚えること・外からの名指し(`?tab=`)を優先することまで
        決まりがあるので、2つ目を作ると片方だけ崩れます。
        """
        root = Path(__file__).resolve().parent.parent
        self.assertTrue((root / "app/static/js/tabs.js").is_file())
        view = (root / "app/static/js/views/settings.js").read_text(
            encoding="utf-8")
        self.assertIn('from "../tabs.js"', view)
        self.assertIn("attachAll()", view)

    def _panel(self, html: str, key: str) -> str:
        """その面の中身だけを切り出す。**面をまたいだ当たりを数えない。**

        面の中にも面がある(マスタ)ので、外側だけに付けた
        `data-panel` で切ります。
        """
        start = html.index(f'data-panel="{key}"')
        rest = html[start:]
        end = rest.find("data-panel=", 1)
        return rest if end < 0 else rest[:end]


class LockBarTests(WebTestCase):
    """鍵の帯。**開く前に、何に要るのかが読める。**"""

    def html(self) -> str:
        return self.get("/settings").get_data(as_text=True)

    def test_鍵が要る面には帯が出る(self) -> None:
        html = self.html()
        for tab in settings_presenter.TABS:
            with self.subTest(tab=tab.key):
                if tab.lock:
                    self.assertIn(f'data-lock="{tab.key}"', html)
                    self.assertIn(f'id="lock-pw-{tab.key}"', html)
                else:
                    self.assertNotIn(f'data-lock="{tab.key}"', html)

    def test_何に要るのかを書いてある(self) -> None:
        html = self.html()
        for tab in settings_presenter.TABS:
            if tab.lock:
                with self.subTest(tab=tab.key):
                    self.assertIn(tab.lock, html)

    def test_鍵は全部同じものと書いてある(self) -> None:
        """合言葉が2つあると、現場は両方を紙に貼ります。"""
        self.assertIn("どの面でも同じもの", self.html())

    def test_開ければ開いていると出る(self) -> None:
        self.assertIn("鍵がかかっています", self.html())
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        html = self.html()
        self.assertIn("鍵が開いています", html)
        self.assertNotIn("鍵がかかっています", html)
        # 開いたら合言葉の欄は出さない(入れる必要が無いので)
        self.assertNotIn('id="lock-pw-master"', html)

    def test_守る欄は鍵が閉まっていれば打てない(self) -> None:
        """打てるのに保存で断られると、効いていないとしか見えない。"""
        html = self.html()
        head = html[html.index('id="path-gw_reference_dir"'):][:400]
        self.assertIn("readonly", head)

    def test_鍵を開ければ打てる(self) -> None:
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        html = self.html()
        head = html[html.index('id="path-gw_reference_dir"'):][:400]
        self.assertNotIn("readonly", head)

    def test_鍵が開いていればパスワード無しで保存できる(self) -> None:
        """面の上で鍵を開けたのに、その面のボタンが断るのはおかしい。"""
        target = self.tmp / "ref2"
        target.mkdir()
        res = self.post("/api/settings/paths", {"gw_reference_dir": str(target)})
        self.assertEqual(res.status_code, 403)     # まだ鍵が閉まっている

        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        res = self.post("/api/settings/paths", {"gw_reference_dir": str(target)})
        self.assertEqual(res.status_code, 200)


class WordingTests(WebTestCase):
    """**何が見つからないのかを言う。**

    「見つかりません」とだけ出していたので、フォルダのことなのか
    ファイルのことなのか読めませんでした。「辿る…」も、押すと何が
    出るのか分かりません。
    """

    def html(self) -> str:
        return self.get("/settings").get_data(as_text=True)

    def test_何が無いのかまで言う(self) -> None:
        html = self.html()
        self.assertNotIn(">見つかりません<", html)
        self.assertIn("フォルダがありません", html)

    def test_欄ごとに探しているものが違う(self) -> None:
        views = {v["key"]: v for v in
                 self.get("/api/settings/state").get_json()["paths"]}
        self.assertEqual(views["gw_reference_dir"]["looking_for"], "フォルダ")
        # ファイル名の欄が見ているのは**置き場所**(ファイルは初回に作る)
        self.assertEqual(views["access_db_filename"]["looking_for"],
                         "置き場所のフォルダ")
        # 目標CSVは**そのファイル**が無ければ目標線が出ない
        self.assertEqual(views["line_target_file"]["looking_for"], "ファイル")

    def test_押すと何が出るのかを書いてある(self) -> None:
        html = self.html()
        self.assertNotIn("辿る…", html)
        self.assertIn("フォルダを選ぶ…", html)


class TabBadgeTests(WebTestCase):
    """面の見出しに出す印。**隠したせいで気づけなくならないように。**"""

    def test_困りごとのある面に数が出る(self) -> None:
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        self.post("/api/settings/paths",
                  {"gw_reference_dir": str(self.tmp / "どこにも無い")})
        html = self.get("/settings").get_data(as_text=True)
        head = html[html.index('data-key="master"'):][:400]
        self.assertIn('data-level="ng"', head)

    def test_印と下の文言は同じ出どころ(self) -> None:
        """見出しの数と、並ぶ文言が食い違うと、どちらを信じるか分からない。"""
        state = settings_presenter.to_dict()
        counted = sum(int(b["text"].rstrip("件"))
                      for b in state["tab_badges"].values())
        self.assertEqual(counted, len(state["problems"]))


if __name__ == "__main__":
    unittest.main()
