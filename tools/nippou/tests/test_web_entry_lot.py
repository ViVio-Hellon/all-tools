"""ロット番号を打つと行が埋まる ── 画面まで通して

【なにが抜けていたか】
VBA の日報入力は、LOT 欄に7桁そろった時点で `SQLiteLot検索` を呼び、
SIKALOT → SIKAHIKI → SIKAODR を辿って**その行のほとんどを
埋めて**いました。Web版にはこれが無く、材・調質も寸法も合紙もＶＣも
全部手打ちでした。

ここは3ファイルを本物と同じ形(テーブル名は3つとも「仕掛」、列名は
半角カナ)で作って、画面のAPIまで通します。
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HEADERS, WebTestCase  # noqa: E402

# 3ファイルとも中のテーブル名は「仕掛」。列名は半角カナ(元がホスト系)
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


class LotLookupTests(WebTestCase):
    """3ファイルを辿って行を埋める。"""

    #: 引当。既定は1件だけ(選ばせない)
    hiki_rows = (("H5422S0", "AB2234", 100, "", "60717001"),)
    lot_course = ""
    odr_export = ""
    odr_interleaf = "1"

    def setUp(self) -> None:
        super().setUp()
        self.ref = self.tmp / "ref"
        self.ref.mkdir(exist_ok=True)
        self._make_masters()
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

    def _make_masters(self) -> None:
        self._write("SIKALOT.sqlite3", LOT_COLUMNS, [(
            "H5422S0", "AB2234", "K1", "52S", "R",
            20.0, 1528.0, 3053.0, "H283", "JISNﾌﾗﾂﾄANF",
            self.lot_course, "取引先", "ﾅﾒｶﾜｱﾙﾐ(ｶ", "送り先",
            "9", "0", "C1",
        )])
        self._write("SIKAHIKI.sqlite3", HIKI_COLUMNS, self.hiki_rows)
        self._write("SIKAODR.sqlite3", ODR_COLUMNS, [
            ("AB2234", "VE-20NF", "VE-20NF", self.odr_interleaf, 250.04314,
             "取引先", "納入先", "送り先", "1P1186", self.odr_export),
            ("AB1234", "V325NW", "", "0", 1626.87679,
             "取引先", "納入先", "送り先", "1P0001", ""),
        ])

    def lookup(self, lot_no: str, row: int = 1, hiki_no: str = ""):
        return self.post("/api/entry/lot", {
            "rows": {str(row): {"LOT": lot_no}}, "row": row,
            "hiki_no": hiki_no,
        }).get_json()

    # -- 埋まるか ----------------------------------------------------
    def test_紙と同じ文字で埋まる(self) -> None:
        body = self.lookup("H5422S0")
        self.assertEqual(body["lot"]["state"], "filled")
        row = body["rows"]["1"]
        self.assertEqual(row["ZAI"], "52S- R")
        self.assertEqual(row["SIZ"], "20.000×1528.0×3053.0")
        self.assertEqual(row["KEN"], "9")
        self.assertEqual(row["VC"], "A:VE-20NF_B:VE-20NF")
        self.assertEqual(row["UNI"], "250.04314")

    def test_合紙は受注のものを使う(self) -> None:
        """現場は「SIKAODR の合紙を参照」。ロット側は "0" を入れてある
        ので、受注側("1")が勝てば「有」になる。"""
        self.assertEqual(self.lookup("H5422S0")["rows"]["1"]["AI"], "有")

    def test_印刷範囲外の6つも埋まる(self) -> None:
        row = self.lookup("H5422S0")["rows"]["1"]
        self.assertEqual(row["others1"], "H283")
        self.assertEqual(row["others2"], "JISNﾌﾗﾂﾄANF")
        self.assertEqual(row["others3"], "ﾅﾒｶﾜｱﾙﾐ(ｶ")
        self.assertEqual(row["others4"], "1P1186")

    def test_使った引当を持ち帰る(self) -> None:
        body = self.lookup("H5422S0")
        self.assertEqual(body["lot"]["order_no"], "AB2234")
        self.assertEqual(body["lot"]["hiki_no"], "60717001")
        self.assertEqual(body["rows"]["1"]["hiki_no"], "60717001")

    def test_保存すると引当番号も残る(self) -> None:
        """**この端末の中だけ**。共有へは送らない(紙にも欄が無い)。"""
        found = self.lookup("H5422S0")
        self.post("/api/entry/save", {"rows": found["rows"]})
        _, details = self.repo().load(*self.repo().list_keys()[0])
        self.assertEqual(details[0].hiki_no, "60717001")
        self.assertEqual(details[0].others1, "H283")

    def test_画面が印刷範囲外の欄を持ち回る(self) -> None:
        """**画面に置き場所が無いと、次に送るときに消える。**

        引いた値はサーバから降りてくるが、ブラウザが集めるのは
        `[data-family]` の欄だけ。欄そのものが無ければ、次の送信で
        空に上書きされ、納入先も包装仕様書Noも保存まで届かない。
        """
        body = self.get("/").get_data(as_text=True)
        for family in ("others1", "others2", "others3", "others4",
                       "others5", "others6", "hiki_no"):
            self.assertIn(f'data-family="{family}" data-row="1"', body, family)

    def test_引いた値が画面に残る(self) -> None:
        found = self.lookup("H5422S0")
        self.post("/api/entry/save", {"rows": found["rows"]})
        body = self.get("/").get_data(as_text=True)
        # 隠してあっても、値は行に付いている(読み直しても消えない)
        self.assertIn("ﾅﾒｶﾜｱﾙﾐ(ｶ", body)
        self.assertIn("1P1186", body)

    # -- 引けないとき ------------------------------------------------
    def test_7桁に足りなければ黙って待つ(self) -> None:
        """打っている途中に毎回断られると、打ち終われない。"""
        body = self.lookup("H542")
        self.assertEqual(body["lot"]["state"], "short")
        self.assertEqual(body["rows"]["1"]["ZAI"], "")

    def test_ロットが無ければ理由を出して手入力に任せる(self) -> None:
        body = self.lookup("Z999999")
        self.assertEqual(body["lot"]["state"], "no_lot")
        self.assertIn("SIKALOT", body["lot"]["message"])
        # **打ったロット番号は消さない**(そのあと手で埋められる)
        self.assertEqual(body["rows"]["1"]["LOT"], "Z999999")

    def test_行が変なら400(self) -> None:
        res = self.post("/api/entry/lot", {"row": 99})
        self.assertEqual(res.status_code, 400)


class NoAllocationTests(LotLookupTests):
    """引当が無いと、そこから先へ進めない。"""

    hiki_rows = ()

    def test_引当が無ければVCも単重も出ないと言う(self) -> None:
        body = self.lookup("H5422S0")
        self.assertEqual(body["lot"]["state"], "no_hiki")
        self.assertIn("SIKAHIKI", body["lot"]["message"])
        self.assertIn("ＶＣ", body["lot"]["message"])

    # 親の「埋まるか」系は引当が無いので通らない。ここでは見ない
    test_紙と同じ文字で埋まる = None
    test_合紙は受注のものを使う = None
    test_印刷範囲外の6つも埋まる = None
    test_使った引当を持ち帰る = None
    test_保存すると引当番号も残る = None
    test_引いた値が画面に残る = None


class ChooseAllocationTests(LotLookupTests):
    """引当が複数あるときは選ばせる(**VBAには無い**)。

    VBA は最初の1件を黙って使っていた。1つのロットが複数の受注に
    引き当てられていると、拾った受注の ＶＣ・単重・合紙が実際の相手と
    食い違うが、押した人には見分けが付かない。
    """

    hiki_rows = (
        ("H5422S0", "AB2234", 60, "", "60717001"),
        ("H5422S0", "AB1234", 40, "", "60717002"),
    )

    def test_複数なら選ばせる(self) -> None:
        body = self.lookup("H5422S0")
        self.assertEqual(body["lot"]["state"], "choose")
        self.assertEqual([c["hiki_no"] for c in body["lot"]["choices"]],
                         ["60717001", "60717002"])

    def test_選ぶまで何も書き換えない(self) -> None:
        """選び直したとき、どこまでが前の引当のものか分からなくなる。"""
        body = self.lookup("H5422S0")
        self.assertEqual(body["rows"]["1"]["ZAI"], "")

    def test_選べばその引当で埋まる(self) -> None:
        body = self.lookup("H5422S0", hiki_no="60717002")
        self.assertEqual(body["lot"]["state"], "filled")
        self.assertEqual(body["lot"]["order_no"], "AB1234")
        # 受注が変われば VC も単重も変わる ── そこが選ばせる理由
        self.assertEqual(body["rows"]["1"]["VC"], "A:V325NW")
        self.assertEqual(body["rows"]["1"]["UNI"], "1626.87679")

    def test_選んだ引当が消えていたら選ばせ直す(self) -> None:
        body = self.lookup("H5422S0", hiki_no="99999999")
        self.assertEqual(body["lot"]["state"], "choose")

    # 1件前提のものは見ない
    test_使った引当を持ち帰る = None
    test_保存すると引当番号も残る = None
    test_紙と同じ文字で埋まる = None
    test_合紙は受注のものを使う = None
    test_印刷範囲外の6つも埋まる = None
    test_引いた値が画面に残る = None


class AutoMarkTests(LotLookupTests):
    """etc の印が自動で立つか。"""

    lot_course = "A-GCT-B"
    odr_export = "1"

    def test_設備コースで外注が立つ(self) -> None:
        body = self.lookup("H5422S0")
        self.assertIn("outsource", body["lot"]["auto_marks"])
        self.assertIn("外注出荷", body["rows"]["1"]["ET"])

    def test_輸出区分でEXが立つ(self) -> None:
        body = self.lookup("H5422S0")
        self.assertIn("ex", body["lot"]["auto_marks"])
        self.assertIn("EX", body["rows"]["1"]["ET"])

    def test_立った印は画面にも返る(self) -> None:
        """ボタンは状態を持たない。etcの文字から読み直す。"""
        body = self.lookup("H5422S0")
        self.assertEqual(sorted(body["marks"]["1"]), ["ex", "outsource"])


class BoxFinalTests(LotLookupTests):
    """検入枚数は BOX最終実績_枚本数、BOXコースの寸法は BOX最終実績の寸法 (v4.16.0)。

        検入枚数が一切入ってきません / sikalot の BOX最終実績_枚本数を参照にしてください
        設計_設備ｺｰｽに GCT GFS GSS のどれかが含まれていた場合
        BOX最終実績_板厚、BOX最終実績_板幅、BOX最終実績_板丈 が寸法になるようにしてください

    現物の SIKALOT の形(BOX実績_* と BOX最終実績_* の両方がある)。1件検索が読む先頭行の
    BOX実績_枚本数は1工程目の「1」で、最終実績が本当の枚数。
    """

    lot_course = "GSS-A"
    final_sheets = "2874"
    final_dims = (8.0, 1250.0, 2500.0)

    def _make_masters(self) -> None:
        super()._make_masters()
        conn = sqlite3.connect(str(self.ref / "SIKALOT.sqlite3"))
        with conn:
            for name in ("BOX実績_板厚", "BOX実績_板幅", "BOX実績_板丈",
                         "BOX最終実績_枚本数", "BOX最終実績_板厚", "BOX最終実績_板幅",
                         "BOX最終実績_板丈"):
                conn.execute(f'ALTER TABLE "仕掛" ADD COLUMN "{name}"')
            conn.execute('UPDATE "仕掛" SET "BOX実績_枚本数" = 1, "BOX実績_板厚" = 30.0,'
                         ' "BOX実績_板幅" = 1600.0, "BOX実績_板丈" = 3200.0,'
                         ' "BOX最終実績_枚本数" = ?, "BOX最終実績_板厚" = ?,'
                         ' "BOX最終実績_板幅" = ?, "BOX最終実績_板丈" = ?',
                         (self.final_sheets, *self.final_dims))
        conn.close()

    def test_紙と同じ文字で埋まる(self) -> None:
        row = self.lookup("H5422S0")["rows"]["1"]
        self.assertEqual(row["KEN"], "2874")                       # 最終実績(1工程目の 1 ではない)
        self.assertEqual(row["SIZ"], "8.000×1250.0×2500.0")        # BOXコースは最終実績の寸法


class BoxFinalRealNumberTests(BoxFinalTests):
    """**検入枚数は整数**(v4.18.0)── 写しの列が実数で `2874.0` と入っていても。

        検入枚数は整数です
    """

    final_sheets = 2874.0

    def test_紙と同じ文字で埋まる(self) -> None:
        row = self.lookup("H5422S0")["rows"]["1"]
        self.assertEqual(row["KEN"], "2874")


class BoxFinalOtherCourseTests(BoxFinalTests):
    """BOXコースでなければ、寸法は製造の寸法のまま(検入枚数は最終実績)。"""

    lot_course = "HOT-NORMAL"

    def test_紙と同じ文字で埋まる(self) -> None:
        row = self.lookup("H5422S0")["rows"]["1"]
        self.assertEqual(row["KEN"], "2874")
        self.assertEqual(row["SIZ"], "20.000×1528.0×3053.0")


class BoxFinalEmptyTests(BoxFinalTests):
    """最終実績が空・0 なら BOX実績へ(古い形式の写し・最終工程がまだ無い)。"""

    final_sheets = "0"
    final_dims = (None, None, None)

    def test_紙と同じ文字で埋まる(self) -> None:
        row = self.lookup("H5422S0")["rows"]["1"]
        self.assertEqual(row["KEN"], "1")
        self.assertEqual(row["SIZ"], "30.000×1600.0×3200.0")       # BOX実績の寸法


class MarkToggleTests(WebTestCase):
    """etc の押しボタン(VBA SPCommand〜MICommand)。"""

    def toggle(self, key: str, etc: str = "", row: int = 1):
        return self.post("/api/entry/mark", {
            "rows": {str(row): {"ET": etc}}, "row": row, "key": key})

    def test_押すと書き足す(self) -> None:
        body = self.toggle("store").get_json()
        self.assertEqual(body["rows"]["1"]["ET"], "ストア")
        self.assertEqual(body["marks"]["1"], ["store"])

    def test_もう一度押すと消える(self) -> None:
        body = self.toggle("store", "ストア").get_json()
        self.assertEqual(body["rows"]["1"]["ET"], "")
        self.assertEqual(body["marks"]["1"], [])

    def test_ほかの印は残る(self) -> None:
        body = self.toggle("ex", "外注出荷 EX").get_json()
        self.assertEqual(body["rows"]["1"]["ET"], "外注出荷")

    def test_知らない印は400(self) -> None:
        self.assertEqual(self.toggle("nope").status_code, 400)

    def test_行が変なら400(self) -> None:
        res = self.post("/api/entry/mark", {"row": 0, "key": "store"})
        self.assertEqual(res.status_code, 400)

    def test_ボタンは画面に6つ出る(self) -> None:
        html = self.client.get("/", headers=HEADERS).get_data(as_text=True)
        for label in ("ｽﾄｱ", "耳付", "外注", "EX", "反転", "ｽﾎﾟｯﾄ"):
            with self.subTest(label=label):
                self.assertIn(label, html)
        self.assertIn('data-mark="outsource"', html)

    def test_最初の印が画面に載っている(self) -> None:
        """**最初の描画をJSに任せない。**

        これが無いと、画面を開いた直後は「どの印が付いているか」を画面が
        知らず、行を選んでもボタンが点きません(サーバへ1度投げるまで
        分からない)。保存済みの行を開き直したときに実際に起きました。
        """
        self.post("/api/entry/save", {"rows": {"1": {"LOT": "A1", "ET": "ストア EX"}}})
        html = self.client.get("/", headers=HEADERS).get_data(as_text=True)
        self.assertIn('id="entry-marks"', html)
        start = html.index('id="entry-marks"')
        block = html[start:start + 400]
        self.assertIn("store", block)
        self.assertIn("ex", block)


if __name__ == "__main__":
    unittest.main()
