"""梱包資材重量計算の画面 ── 打てば引く / オーダーNoは選ぶもの

【何が使いにくかったか】
LotNo を打ってから「LotNoで引く」を押す作りでした。**打ってから押す、を
覚えていないと進めない**のは仕組みの都合で、押す人の都合ではありません
(VBA も `LOT*_Change` で自動的に引いていました)。

オーダーNo も打つ欄でした。ロット番号が決まれば受注番号は決まっている
(SIKAHIKI)ので、打ち直させるものではありません ── VBA のフォームにも
「LotNo入力後にオーダーNoを選択してください」と刷ってあります。

ここは「ボタンが無いこと」「オーダーNoが選択肢であること」「引当が
複数のときは選ばせること」を守ります。
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HEADERS, WebTestCase  # noqa: E402

LOT_COLUMNS = (
    "ﾛｯﾄ番号", "ｵｰﾀﾞｰ番号", "検査番号", "製造材質", "製造調質",
    "製造板厚", "製造板幅", "製造板丈", "用途ｺｰﾄﾞ", "用途名",
    "設計_設備ｺｰｽ", "取引先名", "納入先名", "送り先名",
    "BOX実績_枚本数", "梱包_合紙", "鋳造番号",
)
HIKI_COLUMNS = ("ﾛｯﾄ番号", "受注番号", "引当数量", "引当調整NO", "引当番号")
ODR_COLUMNS = (
    "受注番号", "VC_表", "VC_裏", "合紙", "製品単重", "取引先名称",
    "納入先名称", "送り先名称", "包装仕様NO", "EX_輸出区分",
)


class GwScreenTests(WebTestCase):
    """画面の作り。"""

    def html(self) -> str:
        return self.client.get("/gw", headers=HEADERS).get_data(as_text=True)

    def test_引くボタンは無い(self) -> None:
        """打ってから押す、を覚えていないと進めない作りをやめた。"""
        html = self.html()
        self.assertNotIn("LotNoで引く", html)
        self.assertNotIn("オーダーNoで引く", html)
        self.assertNotIn('id="search-lot"', html)
        self.assertNotIn('id="search-order"', html)

    def test_オーダーNoは選ぶもの(self) -> None:
        """ロット番号が決まれば受注番号は決まっている。打ち直させない。"""
        html = self.html()
        start = html.index('id="order-no"')
        tag = html[max(0, start - 40):start + 40]
        self.assertIn("<select", tag)

    def test_LotNoは7桁まで(self) -> None:
        html = self.html()
        start = html.index('id="lot-no"')
        self.assertIn('maxlength="7"', html[start:html.index(">", start)])

    def test_作業の順に列が並ぶ(self) -> None:
        """製品 → 梱包の形 → 使う資材 → 結果。**一方向。**"""
        html = self.html()
        self.assertIn('class="gw-cols"', html)
        order = [html.index(x) for x in
                 ("製品情報", "梱包の形", "使う資材", ">結果<")]
        self.assertEqual(order, sorted(order), "列の並びが作業の順ではありません")

    def test_打つ欄と引いて入る欄を分ける(self) -> None:
        """混ざっていると、どれを埋めればよいのかが読めない。"""
        html = self.html()
        self.assertIn('id="product-facts"', html)
        for name in ("material", "temper", "usage_code", "course",
                     "customer", "delivery", "sender"):
            with self.subTest(name=name):
                self.assertIn(f'id="pf-{name}"', html)

    def test_結果の欄は最初から出ている(self) -> None:
        """計算するまで空欄でも、**どこに何が出るか**は先に分かる。"""
        html = self.html()
        for name in ("ダンプレート", "material_total", "tare_weight",
                     "gross_weight"):
            with self.subTest(name=name):
                self.assertIn(f'data-result="{name}"', html)


class GwLotLookupTests(WebTestCase):
    """LotNo から受注番号まで辿る。"""

    hiki_rows = (("H5422S0", "AB2234", 100, "", "60717001"),)

    def setUp(self) -> None:
        super().setUp()
        self.ref = self.tmp / "ref"
        self.ref.mkdir(exist_ok=True)
        self._write("SIKALOT.sqlite3", LOT_COLUMNS, [(
            "H5422S0", "AB2234", "K1", "52S", "R",
            20.0, 1528.0, 3053.0, "H283", "JISNﾌﾗﾂﾄANF",
            "A-GCT-B", "取引先", "ﾅﾒｶﾜｱﾙﾐ(ｶ", "送り先", "9", "0", "C1")])
        self._write("SIKAHIKI.sqlite3", HIKI_COLUMNS, self.hiki_rows)
        self._write("SIKAODR.sqlite3", ODR_COLUMNS, [
            ("AB2234", "VE-20NF", "VE-20NF", "1", 250.04314,
             "取引先", "納入先", "送り先", "1P1186", ""),
            ("AB1234", "V325NW", "", "0", 1626.87679,
             "取引先2", "納入先2", "送り先2", "1P0001", "1"),
        ])
        self.post("/api/settings/paths",
                  {"gw_reference_dir": str(self.ref), "password": "nisk"})

    def _write(self, name: str, columns, rows) -> None:
        conn = sqlite3.connect(str(self.ref / name))
        with conn:
            cols = ", ".join(f'"{c}"' for c in columns)
            marks = ", ".join("?" for _ in columns)
            conn.execute(f'CREATE TABLE "仕掛" ({cols})')
            conn.executemany(f'INSERT INTO "仕掛" VALUES ({marks})', rows)
        conn.close()

    def lookup(self, lot_no: str = "H5422S0"):
        return self.post("/api/gw/lot", {"lot_no": lot_no}).get_json()

    def test_寸法が入る(self) -> None:
        body = self.lookup()
        self.assertTrue(body["found"])
        self.assertEqual(body["lot"]["thickness_mm"], 20.0)
        self.assertEqual(body["lot"]["width_mm"], 1528.0)
        self.assertEqual(body["lot"]["length_mm"], 3053.0)

    def test_受注番号まで辿る(self) -> None:
        """**引当が唯一の橋。** ここを辿らないとＶＣも合紙も出ない。"""
        body = self.lookup()
        self.assertEqual([a["order_no"] for a in body["allocations"]],
                         ["AB2234"])

    def test_1件なら受注も自動で入る(self) -> None:
        """選ぶものが無いので、選ばせずにそこまで辿る。"""
        body = self.lookup()
        self.assertEqual(body["order"]["pack_spec_no"], "1P1186")
        self.assertEqual(body["order"]["unit_weight_kg"], 250.04314)

    def test_合紙は受注の合紙列から決まる(self) -> None:
        """現場の指示どおり SIKAODR の「合紙」を参照する。"""
        body = self.lookup()
        self.assertTrue(body["auto"]["interleaf"])
        self.assertIn("interleaf", body["auto"]["decided"])

    def test_VCも自動で決まる(self) -> None:
        body = self.lookup()
        self.assertTrue(body["auto"]["use_vc"])
        self.assertEqual(body["auto"]["vc_name_a"], "VE-20NF")

    def test_ロットが無ければ手入力に任せる(self) -> None:
        body = self.lookup("Z999999")
        self.assertFalse(body["found"])
        self.assertEqual(body["allocations"], [])
        self.assertIn("手入力", body["message"])

    def test_空なら400(self) -> None:
        self.assertEqual(self.post("/api/gw/lot", {"lot_no": ""}).status_code, 400)


class GwManyAllocationTests(GwLotLookupTests):
    """引当が複数のときは、オーダーNoを選ばせる。"""

    hiki_rows = (
        ("H5422S0", "AB2234", 60, "", "60717001"),
        ("H5422S0", "AB1234", 40, "", "60717002"),
    )

    def test_選ばせる(self) -> None:
        body = self.lookup()
        self.assertEqual([a["order_no"] for a in body["allocations"]],
                         ["AB2234", "AB1234"])
        self.assertIn("選んでください", body["message"])

    def test_選ぶまで受注側は決めない(self) -> None:
        """どれを選ぶかで ＶＣ も合紙も単重も変わる。"""
        body = self.lookup()
        self.assertNotIn("order", body)
        self.assertNotIn("auto", body)

    def test_選べばその受注で決まる(self) -> None:
        body = self.post("/api/gw/order", {"order_no": "AB1234"}).get_json()
        self.assertTrue(body["found"])
        self.assertEqual(body["order"]["pack_spec_no"], "1P0001")
        # 合紙 "0" の受注なので、合紙は付かない
        self.assertFalse(body["auto"]["interleaf"])

    def test_番号だけでは選べないので数量も並べる(self) -> None:
        body = self.lookup()
        self.assertIn("60717001", body["allocations"][0]["label"])
        self.assertIn("AB2234", body["allocations"][0]["label"])

    # 1件前提のものは見ない(ここは `test_選ばせる` が見ている)
    test_受注番号まで辿る = None
    test_1件なら受注も自動で入る = None
    test_合紙は受注の合紙列から決まる = None
    test_VCも自動で決まる = None


class GwNoAllocationTests(GwLotLookupTests):
    """引当が無いと、そこから先へ進めない。"""

    hiki_rows = ()

    def test_引当が無いと言う(self) -> None:
        body = self.lookup()
        self.assertTrue(body["found"])        # ロットはある
        self.assertEqual(body["allocations"], [])
        self.assertIn("SIKAHIKI", body["message"])
        self.assertIn("手で入れて", body["message"])

    def test_寸法だけは入る(self) -> None:
        """受注が分からなくても、ロット側の寸法は使える。"""
        self.assertEqual(self.lookup()["lot"]["thickness_mm"], 20.0)

    test_受注番号まで辿る = None
    test_1件なら受注も自動で入る = None
    test_合紙は受注の合紙列から決まる = None
    test_VCも自動で決まる = None


class LeftOverMaterialTests(WebTestCase):
    """**前のロットの資材を残さない。**

    【何が起きていたか】
    「オーダーNoがない状態でも使い資材にチェックが入った」── 見ていた
    のは**前に引いたロットの選択**でした。画面の `applyAuto` は、受注が
    決まらないとき(全角で打った・引当が無い・引当が複数で選ぶ前)に
    **何もしていません**でした。何もしないということは、前の選択が
    そのまま残るということです。

    オーダーNoの欄は空になるのに、ＶＣと合紙だけ前のロットのまま。
    そこで計算を押すと、**別のロットの梱包仕様で重量が出ます** ──
    数字は出るので、間違っていることが画面から分かりません。

    【消すのは、オーダーから来たものだけ】
    「使う資材」7つのうち、受注で決まるのは**合紙とアングルの2つ**
    だけです。残り5つ(ダンプレート・外装紙・バンド・ポリシート・
    ハードボード)はどの梱包でも使う標準の資材で、画面は最初から付いた
    状態で配られます。ここまで外すと5つぶんの重量が抜けた計算になる
    ので、**直したつもりで別の間違いを作ります。**
    """

    JS = (Path(__file__).resolve().parent.parent
          / "app" / "static" / "js" / "views" / "gw.js")

    def source(self) -> str:
        return self.JS.read_text(encoding="utf-8")

    def clear_auto(self) -> str:
        """`clearAuto` の中身だけを取り出す。"""
        text = self.source()
        start = text.index("function clearAuto()")
        return text[start:text.index("\n}", start)]

    def test_受注が決まらなければ白紙に戻す(self) -> None:
        """**ここが本体。** `if (!auto) return;` では残ります。"""
        text = self.source()
        # 引数に `since`(引いているあいだに打った寸法を守る ── v4.24.0)が付いても同じ
        head = text[text.index("function applyAuto(auto"):][:140]
        self.assertIn("clearAuto()", head,
                      "受注が無いときに前の選択が残ります")

    def test_引けなかったときにも消す(self) -> None:
        """ロットが当たらなかったとき ── ここが一番よく通る道です。"""
        text = self.source()
        block = text[text.index("if (!body.found)"):][:400]
        self.assertIn("clearAuto()", block)

    def test_引当が複数で選ぶ前にも消す(self) -> None:
        """受注が返ってこない＝まだ決まっていない。前のままにしない。"""
        self.assertIn("if (!body.order) clearAuto();", self.source())

    def test_ＶＣは外す(self) -> None:
        body = self.clear_auto()
        for name in ("use-vc", "vc-a", "vc-b", "vc-name-a", "vc-name-b"):
            with self.subTest(name=name):
                self.assertIn(name, body)

    def test_標準の資材まで外さない(self) -> None:
        """**5つぶんの重量が抜けた計算になります。**

        `[data-flag]` をまとめて外すと、ダンプレートも外装紙もバンドも
        ポリシートもハードボードも消えます ── どれも受注とは関係なく
        毎回使うものです。
        """
        body = self.clear_auto()
        self.assertNotIn("querySelectorAll", body,
                         "使う資材をまとめて外しています")
        for name in ("dunplate", "outer_paper", "band",
                     "poly_sheet", "hardboard"):
            with self.subTest(name=name):
                self.assertNotIn(name, body)

    def test_合紙と縦バンドアングルは既定へ戻す(self) -> None:
        """この2つだけが受注で決まるもの。合紙は**配られたときの姿**、
        縦バンドアングルは**縦バンドの本数どおり**(`syncAngle`)。"""
        body = self.clear_auto()
        self.assertIn("interleaf", body)
        self.assertIn("= true", body)
        self.assertIn("syncAngle()", body)

    def test_自動の印も消す(self) -> None:
        """印が残ると、人が選んだものまで自動で決まったように見えます。"""
        self.assertIn("markDecided([])", self.clear_auto())

    def test_画面は7つとも付いた状態で配られる(self) -> None:
        """「チェックが入っている」の出どころ ── **受注ではなく既定**。

        打った人が見たチェックは、ここから来ています。受注を引かなくても
        付いているのが正しい姿です(毎回使う資材だから)。
        """
        html = self.client.get("/gw", headers=HEADERS).get_data(as_text=True)
        for name in ("dunplate", "outer_paper", "interleaf", "band",
                     "poly_sheet", "angle", "hardboard"):
            with self.subTest(name=name):
                start = html.index(f'data-flag="{name}"')
                self.assertIn("checked", html[start:start + 40])


if __name__ == "__main__":
    unittest.main()


class VcNameComboTests(WebTestCase):
    """ＶＣの品名は**選ぶもの**(VBA `VCCombo設定` → ComboBox1/2)

    「表面(A)表面(B)はVBAだとコンボボックスでDBからVCデータ入れて
    選択できる」と言われたところです。打たせると、マスタに無い綴りが
    そのまま通り、計算の最後で「登録されていないVCです」と断られます
    ── そこから打ち直す場所も、正しい綴りも画面に出ていません。
    """

    def with_master(self, names) -> str:
        ref = self.tmp / "ref"
        ref.mkdir(exist_ok=True)
        conn = sqlite3.connect(str(ref / "梱包資材マスタ.sqlite3"))
        with conn:
            conn.execute('CREATE TABLE "VC重量" ("品名", "単位質量", "係数")')
            conn.executemany('INSERT INTO "VC重量" VALUES (?, ?, ?)',
                             [(n, "1.5", "1.0") for n in names])
        conn.close()
        self.post("/api/settings/paths",
                  {"gw_reference_dir": str(ref), "password": "nisk"})
        return self.client.get("/gw", headers=HEADERS).get_data(as_text=True)

    def field(self, html: str, elem_id: str) -> str:
        start = html.index(f'id="{elem_id}"')
        return html[max(0, start - 60):start + 60]

    def test_マスタの品名が選択肢になる(self) -> None:
        html = self.with_master(["VE-20NF", "V325NW"])
        for elem_id in ("vc-name-a", "vc-name-b"):
            with self.subTest(elem_id=elem_id):
                self.assertIn("<select", self.field(html, elem_id))
        for name in ("VE-20NF", "V325NW"):
            with self.subTest(name=name):
                self.assertIn(f'<option value="{name}">{name}</option>', html)

    def test_選ばない状態から始まる(self) -> None:
        """前のロットの品名が残って見えないように、空から始めます。"""
        html = self.with_master(["VE-20NF"])
        self.assertIn('<option value="">(選ぶ)</option>', html)

    def test_マスタを読めなければ打つ欄に落ちる(self) -> None:
        """**その日にGW計算ができなくなるほうが困ります。**

        一覧が空のまま選ぶ欄だけ出すと、選べる品名が1つも無い欄が
        残ります。
        """
        html = self.client.get("/gw", headers=HEADERS).get_data(as_text=True)
        for elem_id in ("vc-name-a", "vc-name-b"):
            with self.subTest(elem_id=elem_id):
                self.assertIn('type="text"', self.field(html, elem_id))
        self.assertIn("手入力", html)

    def test_読めなかった理由と直す場所を書く(self) -> None:
        html = self.client.get("/gw", headers=HEADERS).get_data(as_text=True)
        self.assertIn("ＶＣ重量マスタ", html)
        self.assertIn("参照設定", html)


class FigureButtonTests(WebTestCase):
    """梱包図を出すボタン (VBA `図形展開`)

    「図形描写出すボタンがない」と言われたところです。図そのものは
    計算すると下に出ますが、**画面の下のほうにあるので気づかれません**
    でした。VBA には押すボタンがあったので、同じものを計算の隣に置きます。
    """

    JS = (Path(__file__).resolve().parent.parent
          / "app" / "static" / "js" / "views" / "gw.js")

    def test_計算の隣にボタンがある(self) -> None:
        html = self.client.get("/gw", headers=HEADERS).get_data(as_text=True)
        self.assertIn('id="show-figure"', html)
        self.assertIn("梱包図を出す", html)
        # 「計算」と同じ並びの中(あいだに別の器が挟まっていない)
        between = html[html.index('id="calc"'):html.index('id="show-figure"')]
        self.assertNotIn("<section", between)

    def test_押せば配線されている(self) -> None:
        source = self.JS.read_text(encoding="utf-8")
        self.assertIn('byId("show-figure")?.addEventListener("click", showFigure)',
                      source)

    def test_まだ計算していなければ先に計算する(self) -> None:
        """**押したのに何も起きない、を作らない。**"""
        source = self.JS.read_text(encoding="utf-8")
        body = source[source.index("async function showFigure()"):]
        body = body[:body.index("\n}\n")]
        self.assertIn("await calculate()", body)
        self.assertIn("scrollIntoView", body)

    def test_計算が通らなくても黙って終わらない(self) -> None:
        """寸法が空のまま押すと、図の枠すら出ません。

        そこで何も言わずに終わると、ボタンが壊れているように見えます
        ── 断りの中身は「直すところ」に出ているので、そこへ送ります。
        """
        source = self.JS.read_text(encoding="utf-8")
        body = source[source.index("async function showFigure()"):]
        body = body[:body.index("\n}\n")]
        self.assertIn("calc-state", body)
        self.assertIn("calc-problems", body)
