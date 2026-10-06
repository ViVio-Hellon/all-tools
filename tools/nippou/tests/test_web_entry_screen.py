"""日報入力の画面 ── 紙の姿・選ぶ欄・入力制限

指摘は3つでした:

    ・写真1(梱包実績日報表)のレイアウトに寄せてほしい
    ・設備停止理由は入力欄でコンボボックスから選ばせたほうがよい
    ・入力制限(数字のみ、2桁入力 etc)が何も効いていない

3つめは大半が `nav.js` の不具合(2度目に画面へ来ると listener が
付かない)でしたが、規則そのものが画面へ降りていないと、直しても
効きません ── ここは**規則が欄の属性として本当に出ているか**を見ます。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HEADERS, WebTestCase                     # noqa: E402


class PaperLayoutTests(WebTestCase):
    """紙(梱包実績日報表)と同じ見出しが出ているか。"""

    def html(self) -> str:
        res = self.client.get("/", headers=HEADERS)
        self.assertEqual(res.status_code, 200)
        return res.get_data(as_text=True)

    def test_2段の見出しが出る(self) -> None:
        html = self.html()
        for group in ("梱包作業時間", "梱包数量", "作業停止①", "作業停止②",
                      "作業停止③", "実績合計", "製品寸法", "ＶＣ種別"):
            with self.subTest(group=group):
                self.assertIn(group, html)

    def test_紙の言葉になっている(self) -> None:
        html = self.html()
        self.assertIn("材・調質", html)
        self.assertIn("個装単位 枚数", html)
        self.assertIn("梱包単位 包数", html)
        # 前の見出しは残っていない
        self.assertNotIn(">材質<", html)
        self.assertNotIn(">サイズ<", html)

    def test_LOT欄の見出しは打つ人の言葉(self) -> None:
        """**ここだけ紙と違う。**

        紙は「ロット№」ですが、現場が口にするのも、引き先
        (`LS4LOT` / `SIKALOT`)の列名も LOTNO でした。打つ欄の見出しは
        打つ人に合わせます ── 紙とCSVは「ロット№」のままです
        (`layout.Column.screen_label`)。
        """
        html = self.html()
        self.assertIn(">LOTNO<", html)
        self.assertNotIn(">ロット№<", html)

    def test_紙の注意書きが出る(self) -> None:
        self.assertIn("※作業停止時間は5分以上で時間を記入", self.html())

    def test_紙に載らない列は区別して出す(self) -> None:
        """作業時間・単重は印刷範囲の外。入力する列と同じ見た目にしない。"""
        html = self.html()
        self.assertIn("印刷範囲外", html)
        self.assertIn('class="outside"', html)

    def test_合紙の欄が出る(self) -> None:
        """DBには前からあったのに、画面に出ていなかった列。"""
        html = self.html()
        self.assertIn('id="AI1"', html)
        self.assertIn(">有<", html)
        self.assertIn(">無<", html)


class InputLimitTests(WebTestCase):
    """入力制限が欄の属性として降りているか。"""

    def html(self) -> str:
        return self.client.get("/", headers=HEADERS).get_data(as_text=True)

    def test_数字のみの欄(self) -> None:
        html = self.html()
        # 検入枚数
        self.assertIn('id="KEN1"', html)
        self.assertRegex(html, r'id="KEN1"[^>]*data-charset="digits"')

    def test_時分は2桁で次へ飛ぶ(self) -> None:
        html = self.html()
        self.assertRegex(html, r'id="KZ1"[^>]*maxlength="2"')
        self.assertRegex(html, r'id="KZ1"[^>]*data-advance-at="2"')
        self.assertRegex(html, r'id="KZ1"[^>]*data-next="KH"')

    def test_作業人数は1桁(self) -> None:
        self.assertRegex(self.html(), r'id="HIT1"[^>]*maxlength="1"')

    def test_停止時間は3桁(self) -> None:
        self.assertRegex(self.html(), r'id="TH1"[^>]*maxlength="3"')

    def test_ロットは英数字(self) -> None:
        self.assertRegex(self.html(), r'id="LOT1"[^>]*data-charset="alnum_upper"')

    def test_自由入力の欄には制限を付けない(self) -> None:
        self.assertRegex(self.html(), r'id="VC1"[^>]*data-charset="any"')


class StopReasonComboTests(WebTestCase):
    """停止理由の記号は「打つ」ではなく「選ぶ」。"""

    def _make_master(self) -> None:
        """伝送用ファイルを、実物と同じ形で置く。

        **この1件だけのフォルダへ置く。** 参照先は既定だと利用者の
        ホームなので、置きっ放しにすると他のテストへ漏れる
        """
        ref = self.tmp / "ref"
        ref.mkdir(exist_ok=True)
        path = ref / "伝送用ファイル.sqlite3"
        conn = sqlite3.connect(str(path))
        with conn:
            for table, rows in (
                ("作業停止時間内訳_1", [("1", "休憩食事", "0"), ("2", "TPM活動/清掃", "1")]),
                ("作業停止時間内訳_2", [("1", "突発停止(機械)", "イ"), ("2", "段取り（板）", "レ")]),
                ("作業停止時間内訳_3", [("1", "段取り変更", "A"), ("2", "ビニール交換", "G")]),
            ):
                conn.execute(
                    f'CREATE TABLE "{table}" ("管理番号", "内訳", "内訳番号", "備考")')
                conn.executemany(
                    f'INSERT INTO "{table}" VALUES (?, ?, ?, "")', rows)
        conn.close()
        self.post("/api/settings/paths",
                  {"gw_reference_dir": str(ref), "password": "nisk"})

    def test_マスタがあれば選ぶ欄になる(self) -> None:
        self._make_master()
        html = self.client.get("/", headers=HEADERS).get_data(as_text=True)
        self.assertIn('<select id="S1"', html)
        self.assertIn('<select id="SS1"', html)
        self.assertIn('<select id="STH1"', html)
        # 記号だけでは分からないので、名前も並べて出す
        self.assertIn("0 休憩食事", html)
        self.assertIn("レ 段取り（板）", html)
        self.assertIn("G ビニール交換", html)

    def test_分類ごとにまとめて出す(self) -> None:
        self._make_master()
        html = self.client.get("/", headers=HEADERS).get_data(as_text=True)
        for category in ("設備停止", "不稼働", "ハンドリング"):
            with self.subTest(category=category):
                self.assertIn(f'<optgroup label="{category}">', html)

    def test_マスタが無ければ自由入力に落ちる(self) -> None:
        """**画面が出ないより、打てるほうがまし。**"""
        # 参照先は既定だと利用者のホーム。**この1件だけの空フォルダ**を
        # 指してから見る(端末に何が置いてあるかに結果を左右されない)
        empty = self.tmp / "empty"
        empty.mkdir()
        self.post("/api/settings/paths",
                  {"gw_reference_dir": str(empty), "password": "nisk"})
        html = self.client.get("/", headers=HEADERS).get_data(as_text=True)
        self.assertNotIn('<select id="S1"', html)
        self.assertIn('id="S1"', html)

    def test_選んだ記号が保存まで通る(self) -> None:
        self._make_master()
        res = self.post("/api/entry/save", {
            "rows": {"1": {"LOT": "H5422S0", "S": "0", "TH": "20",
                           "KZ": "08", "KH": "00", "SZ": "10", "SH": "00",
                           "AI": "有"}},
            "header": {"worker": "近藤雅幹"},
        })
        self.assertEqual(res.status_code, 200)
        html = self.client.get("/", headers=HEADERS).get_data(as_text=True)
        # 選ばれた記号が選択済みで戻る
        self.assertRegex(html, r'<option value="0"\s*selected>0 休憩食事</option>')
        self.assertRegex(html, r'<option value="有"\s*selected>有</option>')

    def test_マスタに無い記号も消さない(self) -> None:
        """マスタが差し替わると、去年の日報の記号が一覧から消えることがある。

        **DBへ直に入れます。** `/api/entry/save` は通りません ── マスタに
        無い記号は保存前チェック(内訳チェックRun)が止めるようになった
        ためで、それが正しい振る舞いです。ここで見たいのはその先、
        「**すでに入っている**行を開いたときに一覧から消えないか」です
        (去年の日報、他の道具で入った行、マスタの差し替え)。
        """
        self._make_master()
        from nippou import work_context
        from nippou.db.models import DetailRecord, HeaderRecord

        with self.app.test_request_context(headers=HEADERS):
            ctx = work_context.get_context()
            from app.routes.entry import current_calculator
            date, line, shift = ctx.current_key(current_calculator())
        key = dict(report_date=date, line=line, shift=shift, page=1)
        self.repo().save(HeaderRecord(**key),
                         [DetailRecord(**key, row_no=1, s="Z")])

        html = self.client.get("/", headers=HEADERS).get_data(as_text=True)
        self.assertIn("Z(マスタに無い)", html)


class ExclusionScreenTests(WebTestCase):
    """裸のチェックボックス(HdCh1-9 / #8-#80)は画面から下ろした。

    値はどこからも読まれておらず(VBA でも排他の見た目だけだった)、
    停止理由は行ごとの記号として入れるので、画面に出す意味が無い。
    """

    def test_裸のチェックボックスは出さない(self) -> None:
        html = self.client.get("/", headers=HEADERS).get_data(as_text=True)
        self.assertNotIn("HdCh1", html)
        self.assertNotIn("CheckBox75", html)


class EtcButtonTests(WebTestCase):
    """etc のボタンが**1度押しで効く**こと。

    【「2回押して効く」の正体】
    行を選んでいないあいだ、ボタンを `disabled` にしていました。
    **無効のボタンは click を出しません** ── 押しても toast も音も出ず、
    何も起きません。

        1度目 … 押す → 何も起きない(無効だった)
        その後 … 表のどこかを触る → 行が決まってボタンが生きる
        2度目 … 押す → 効く

    押した人からは「2回押さないと効かない」に見えます。原因が画面に
    出ないので、なおさら分かりません。
    """

    JS = (Path(__file__).resolve().parent.parent
          / "app" / "static" / "js" / "views" / "entry.js")

    def source(self) -> str:
        return self.JS.read_text(encoding="utf-8")

    def test_押せなくしない(self) -> None:
        """**ここが本体。** 押せない状態を作らない。"""
        text = self.source()
        head = text[text.index("function paintMarks"):]
        body = head[:head.index("\n}")]
        self.assertIn("btn.disabled = false", body)
        self.assertNotIn("btn.disabled = !markRow", body)

    def test_行が決まっていなければ押したときに決める(self) -> None:
        text = self.source()
        # 押したときの処理。**塗り直すほうではなく、click を付けるほう**
        block = text[text.index('btn.addEventListener("click"'):]
        self.assertIn("rowForMarks()", block[:700])

    def test_どの行に効いたかは画面に出る(self) -> None:
        """勝手に決めますが、**決めた行は見えます。**"""
        html = self.client.get("/", headers=HEADERS).get_data(as_text=True)
        self.assertIn('id="marks-row"', html)

    def test_打ちかけの行を選ぶ(self) -> None:
        """何も入っていなければ1行目。入っていれば、その下の行。"""
        text = self.source()
        block = text[text.index("function rowForMarks"):]
        body = block[:block.index("\n}")]
        self.assertIn("last || 1", body)
        # etc欄そのものが入っているかどうかで行を選ばない(押すたびに
        # 下へずれてしまう)
        self.assertIn('=== "ET"', body)


class LotLookupTimingTests(WebTestCase):
    """LOTNO は**欄を離れなくても引く**。

    以前は離れたとき(blur)だけでした。現場の打ちかたは「LOTNOを打って、
    そのまま画面を見る」なので、離れるまで材・調質が入らず、打ったのに
    空のままに見えます。VBA は `LOT*_Change` で7桁そろった時点で
    引いていました。
    """

    JS = (Path(__file__).resolve().parent.parent
          / "app" / "static" / "js" / "views" / "entry.js")

    def source(self) -> str:
        return self.JS.read_text(encoding="utf-8")

    def test_打ち終わった時点で引く(self) -> None:
        text = self.source()
        # 打っている最中(input)と、変換が確定したとき の両方から
        for anchor in ('el.addEventListener("input"',
                       'el.addEventListener("compositionend"'):
            with self.subTest(anchor=anchor):
                block = text[text.index(anchor):][:600]
                self.assertIn("maybeLookup(el)", block)

    def test_7桁ちょうどのときだけ引く(self) -> None:
        """1〜6桁を打っているあいだに共有ファイルを6回読みに行かせない。"""
        text = self.source()
        block = text[text.index("function maybeLookup"):]
        body = block[:block.index("\n}")]
        self.assertIn("!== LOT_LENGTH", body)

    def test_同じ番号は二度引かない(self) -> None:
        """8桁目を打って消すたびに引き直さない。"""
        text = self.source()
        block = text[text.index("function maybeLookup"):]
        body = block[:block.index("\n}")]
        self.assertIn("lastLookedUp", body)


class ImeHintTests(WebTestCase):
    """英数字の欄では、かな入力に切り替わっていても困らないこと。

    **ブラウザから IME を確実に切る手立ては、いまのところありません。**
    `ime-mode` は CSS の仕様から外れ、Chromium(Edge も中身は同じ)は
    読み飛ばします。なので「切れていることに頼らない」作りにします ──
    打った先から半角・大文字へ直し、送られてきた値もサーバで直す。
    """

    def test_英語の欄だと伝える(self) -> None:
        from nippou.logic import input_rules

        attrs = input_rules.as_attributes("LOT")
        self.assertEqual(attrs["lang"], "en")

    def test_効かない値は置かない(self) -> None:
        """`inputmode="latin"` は仕様から外れた値で、いまのブラウザは
        読み飛ばします。**効かないものを置くと、効いているつもりで
        次の人が数えます。**"""
        from nippou.logic import input_rules

        self.assertNotIn("inputmode", input_rules.as_attributes("LOT"))

    def test_全角で打っても直る(self) -> None:
        """IMEが切れていなくても通るのが要点(`normalize`)。"""
        from nippou.logic import input_rules

        self.assertEqual(input_rules.normalize("LOT", "Ｎ７１３１Ｔ０"),
                         "N7131T0")


class EntryLayoutTests(WebTestCase):
    """日報入力の並び(v4.22.0)── 表の上に2枚、表の下に2枚。

        入力ページレイアウトを写真のようにできますか
        現状操作コントロールの間に説明文があったり導線が好みでないです

        表の上  … etc のボタン | 入力のこつ
        表の下  … ③のボタン   | 直実績合計
        説明    … ボタンに乗せたとき・いちばん下の「くわしく」
    """

    def html(self) -> str:
        return self.client.get("/", headers=HEADERS).get_data(as_text=True)

    def test_表の上にetcと入力のこつ(self) -> None:
        html = self.html()
        start = html.index('class="entry-panels"')
        block = html[start:html.index('class="grid-wrap"')]
        self.assertIn('id="marks"', block)
        self.assertIn('id="entry-hints"', block)
        for tip in ('紙には載りません', 'id="stamp-tip"', 'id="shortcut-tip"'):
            self.assertIn(tip, block)
        self.assertLess(html.index('id="marks"'), html.index('id="entry-hints"'))   # 左に etc
        self.assertLess(html.index('id="sheet-now"'), start)                          # 見出しの下

    def test_表の下に保存のボタンと直実績合計(self) -> None:
        html = self.html()
        start = html.index('class="entry-foot"')
        self.assertLess(html.index('class="grid-wrap"'), start)
        block = html[start:html.index('id="check-panel"')]
        self.assertIn('data-step-for="save"', block)
        self.assertIn("直実績合計", block)
        self.assertLess(block.index('id="save"'), block.index("直実績合計"))         # 左にボタン

    def test_ボタンの間に説明の文を置かない(self) -> None:
        """説明はボタンに乗せたとき(`data-hint`)と、いちばん下の「くわしく」。"""
        html = self.html()
        actions = html[html.index('class="card entry-actions"'):html.index("直実績合計")]
        self.assertNotIn('class="lead tip"', actions)
        for button in ('id="save"', 'id="check-shift"', 'id="push-shared"', 'id="print-page"'):
            with self.subTest(button=button):
                tag = actions[actions.index(button):]
                self.assertIn("data-hint=", tag[:tag.index(">")])
        # 保存の知らせはボタンの列の外(文の長さでボタンが折り返さない)
        row = actions[actions.index('data-step-for="save"'):]
        row = row[:row.index("</div>")]
        self.assertNotIn('id="save-note"', row)
        self.assertIn('id="save-note"', actions)

    def test_説明はいちばん下に畳む(self) -> None:
        html = self.html()
        notes = html[html.index('id="entry-notes"'):]
        notes = notes[:notes.index("</details>")]
        for text in ("紙が欲しいときだけ", "この端末に</b>保存します", "みんなが見るところへ",
                     "集計CSVを3本", "この直の中でだけ", "赤く残ります"):
            with self.subTest(text=text):
                self.assertIn(text, notes)
        self.assertLess(html.index('class="entry-foot"'), html.index('id="entry-notes"'))
        self.assertNotIn(" open", html[html.index('id="entry-notes"') - 40:html.index('id="entry-notes"') + 20])

    def test_狭い画面では縦に落ちる(self) -> None:
        """横に詰めると、どちらも読めない幅になります。"""
        css = (Path(__file__).resolve().parent.parent / "app" / "static"
               / "css" / "components.css").read_text(encoding="utf-8")
        self.assertRegex(css, r"@media \(max-width:\d+px\)\{\s*\.entry-panels, \.entry-foot\{ "
                              r"grid-template-columns:minmax\(0, 1fr\); \}")


class SaveButtonRowTests(WebTestCase):
    """③の並び ── **押す場面ごとに囲む**

    「③のボタンの並びがなぁ．．．　保存2つあって作業者2つ押すかなぁ？」
    と言われたところです。平らに6つ並べると「保存(確定)」と
    「共有へ保存」が隣どうしに見えて、どちらを押すのか・両方押すのかが
    読めません。押す**回数**で囲って、見出しにいつ押すのかを書きます。
    """

    def html(self) -> str:
        return self.client.get("/", headers=HEADERS).get_data(as_text=True)

    def row(self) -> str:
        html = self.html()
        start = html.index('data-step-for="save"')
        return html[start:html.index("</div>", html.index("save-note"))]

    def test_3つの囲みに分かれる(self) -> None:
        row = self.row()
        for when in ("always", "end", "maybe"):
            with self.subTest(when=when):
                self.assertIn(f'data-when="{when}"', row)

    def test_囲みの見出しがいつ押すのかを言う(self) -> None:
        row = self.row()
        self.assertIn("打つあいだ、何度でも", row)
        self.assertIn("直が終わったら1回", row)
        self.assertIn("要るときだけ", row)

    def test_保存2つは別の囲みに入る(self) -> None:
        """**隣に置かない。** 名前が似ているので、器で離します。"""
        row = self.row()
        save = row.index('id="save"')
        push = row.index('id="push-shared"')
        between = row[save:push]
        # あいだに囲みの区切りがある(同じ `btn-group__row` の中にいない)
        self.assertIn("</span>", between)
        self.assertIn('data-when="end"', between)

    def test_直の終わりの2つは矢印でつなぐ(self) -> None:
        """チェック → 共有へ保存 の順で押すことが、形でも見える。"""
        row = self.row()
        check = row.index('id="check-shift"')
        arrow = row.index("btn-group__arrow")
        push = row.index('id="push-shared"')
        self.assertLess(check, arrow)
        self.assertLess(arrow, push)

    def test_次ページ発行は保存の列にいない(self) -> None:
        """保存ではなく**紙を1枚増やす**操作なので、紙を決める上の段。"""
        html = self.html()
        self.assertLess(html.index('id="new-page"'),
                        html.index('data-step-for="save"'))
        self.assertLess(html.index('id="open-staff"'),
                        html.index('id="new-page"'))

    def test_印刷は残っている(self) -> None:
        self.assertIn('id="print-page"', self.row())


class PageWordingTests(WebTestCase):
    """「頁」は使わない ── **聞き慣れない字を画面に置かない**

    「頁ってどうも聞きなれないから　ページにかえれる？」と言われた
    ところです。読めない字が1つあるだけで、その行ぜんぶが飛ばされます。
    """

    def screens(self) -> list[tuple[str, str]]:
        return [(path, self.client.get(path, headers=HEADERS)
                 .get_data(as_text=True))
                for path in ("/", "/records", "/settings", "/graph", "/gw")]

    def test_どの画面にも頁が出ない(self) -> None:
        for path, html in self.screens():
            with self.subTest(path=path):
                self.assertNotIn("頁", html)

    def test_ページという言葉で出ている(self) -> None:
        html = self.client.get("/", headers=HEADERS).get_data(as_text=True)
        self.assertIn("ページ", html)
        self.assertIn("次ページ発行", html)

    def test_静的ファイルにも残っていない(self) -> None:
        """画面から `fetch` で差し替わる文も、同じ言葉でそろえる。"""
        root = Path(__file__).resolve().parent.parent / "app" / "static"
        for path in sorted(root.rglob("*.js")):
            with self.subTest(path=path.name):
                self.assertNotIn("頁", path.read_text(encoding="utf-8"))
