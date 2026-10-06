"""梱包資材重量計算の印刷 (VBA ``印刷()`` / ``印刷2()``)

【VBAは印刷範囲だけ違う2つの Sub だった】

    印刷()   $B$4:$L$34   A4 横   計算結果 + 頭10梱包
    印刷2()  $B$22:$L$74  A4 縦   梱包数ごとの重量表(50梱包まで)

【いまは紙1種類。表は刷らず、梱包数を聞く】

*v3.21.0 でいったん「2つから選ぶ」形にしたものを、v3.22.0 で作り直した
ところです。* 100行の表を刷っても読むのは「いま作る梱包数」の行1つで、
残りの99行は探す手間になります。押す前に梱包数を聞いて、**その行だけ**を
計算結果の紙に載せます。

ここで守るのは:
    1. 梱包数を入れると、その梱包数ぶんの資材重量が一段入る
    2. 1梱包なら段が出ない(上の数字と同じものを2度出さない)
    3. 紙の数字は**画面と同じ計算から**出る(`_compute` / `per_pack_table`)
    4. 刷れないときに**白紙を出さない**(窓に理由を字で出す)
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.reporting import gw_print
from tests._gw_master import GwWebTestCase
from tests._web import WebTestCase

#: 計算が通る入力ひとそろい。**紙の鍵**でもある(計算結果は保存しない)
INPUTS = {
    "lot_no": "N7131T0", "order_no": "OD-1",
    "thickness_mm": "1.99", "width_mm": "81", "length_mm": "1000",
    "count": "40", "vertical_bands": "2", "horizontal_bands": "2",
    "pack_height_mm": "200", "pallet_weight_kg": "18",
    "stack_pattern": "1", "band_kind": "シール無",
    "input_weight_kg": "1200",
    "flags": "dunplate,outer_paper,band,poly_sheet,angle,hardboard",
}


def _query(**extra) -> str:
    from urllib.parse import urlencode

    values = dict(INPUTS)
    values.update({k: v for k, v in extra.items() if v is not None})
    return urlencode(values)


def _data(packs: int = 1) -> gw_print.GwPrintData:
    row = gw_print.row
    pack_rows = ([] if packs <= 1 else
                 [row("ダンプレート", 1.5 * packs, "kg"),
                  row("資材計", 3.0 * packs, "kg", strong=True)])
    return gw_print.GwPrintData(
        product=[row("LotNo", "N7131T0", digits=None)],
        dimensions=[row("板厚", 1.99, "mm")],
        materials=[row("ダンプレート", 1.5, "kg"),
                   row("合紙", None, used=False)],
        totals=[row("梱包資材重量", 3.0, "kg", strong=True)],
        pack_count=packs, pack_rows=pack_rows,
        subject="LotNo N7131T0")


class PackCountTests(unittest.TestCase):
    """梱包数の読み取り。**打ち間違いで紙を止めない。**"""

    def test_ふつうの数(self) -> None:
        self.assertEqual(gw_print.normalize_packs("12"), 12)

    def test_空なら1梱包(self) -> None:
        """1梱包なら「◯梱包ぶん」の段が出ないだけで、紙は出る。"""
        self.assertEqual(gw_print.normalize_packs(""), 1)
        self.assertEqual(gw_print.normalize_packs(None), 1)

    def test_0や負は1梱包(self) -> None:
        self.assertEqual(gw_print.normalize_packs("0"), 1)
        self.assertEqual(gw_print.normalize_packs("-5"), 1)

    def test_文字なら1梱包(self) -> None:
        """計算そのものは梱包数に左右されないので、ここで断る意味がない。"""
        self.assertEqual(gw_print.normalize_packs("あ"), 1)

    def test_上限で丸める(self) -> None:
        """VBAの表と同じ100梱包まで ── その先は確かめる相手がいない。"""
        self.assertEqual(gw_print.normalize_packs("999"), gw_print.MAX_PACKS)
        self.assertEqual(gw_print.MAX_PACKS, 100)

    def test_桁が増えても丸める(self) -> None:
        """**「100000 と入れられたら」への答え。** 何桁でも上限で止まる。"""
        for text in ("100000", "99999999999", "1e9", "  1000000  "):
            self.assertEqual(gw_print.normalize_packs(text),
                             gw_print.MAX_PACKS, text)

    def test_小数は切り捨てる(self) -> None:
        self.assertEqual(gw_print.normalize_packs("12.7"), 12)


class BuildTests(unittest.TestCase):
    """紙の組み立て。"""

    def test_A4横1枚(self) -> None:
        """VBA `印刷()` と同じ向き。"""
        html = gw_print.build_html(_data())
        self.assertIn("size: A4 landscape", html)

    def test_見出しはVBAと同じ文言(self) -> None:
        self.assertIn("梱包資材重量計算結果", gw_print.build_html(_data()))

    def test_1梱包なら段が出ない(self) -> None:
        """上の「使う資材と重量」と同じ数字になるので、2度出さない。"""
        html = gw_print.build_html(_data(1))
        self.assertNotIn("梱包ぶんの資材重量", html)

    def test_2以上なら段が出る(self) -> None:
        html = gw_print.build_html(_data(12))
        self.assertIn("12梱包ぶんの資材重量", html)
        self.assertIn("18.00", html)          # 1.5 × 12
        self.assertIn("36.00", html)          # 3.0 × 12

    def test_1梱包ぶんも残る(self) -> None:
        """**計算結果そのものは1梱包ぶん。** 掛けた数だけ出すと、画面の
        数字と突き合わせられなくなる。"""
        html = gw_print.build_html(_data(12))
        self.assertIn("使う資材と重量(1梱包ぶん)", html)
        self.assertIn("合計(1梱包ぶん)", html)

    def test_掛けないものを断り書きする(self) -> None:
        """積合せ・Aインプット・GWは梱包の数で増えない(VBAの表にも無い)。"""
        html = gw_print.build_html(_data(12))
        self.assertIn("積合せ重量・Aインプット重量・GWは", html)

    def test_表は刷らない(self) -> None:
        """**ここが v3.21.0 からの変更点。** 1〜100の表は画面に残す。"""
        html = gw_print.build_html(_data(12))
        self.assertNotIn("1〜100梱包", html)
        self.assertNotIn("per-pack", html)

    def test_使わない資材の行は消さない(self) -> None:
        """消すと「忘れたのか、使わないのか」が紙から読めなくなる。"""
        html = gw_print.build_html(_data())
        self.assertIn("合紙", html)
        self.assertIn('class="unused"', html)

    def test_題にロットが入る(self) -> None:
        """何枚も刷ったあと、どれがどのロットの紙か分かるように。"""
        title = re.search(r"<title>(.*?)</title>",
                          gw_print.build_html(_data())).group(1)
        self.assertIn("N7131T0", title)

    def test_刷れない理由を紙に出す(self) -> None:
        """新しい窓で開く経路なので、断りをJSONで返すと白紙が出るだけ。"""
        html = gw_print.build_missing_html("板厚の入力が不正です。")
        self.assertIn("板厚の入力が不正です。", html)


class RowTests(unittest.TestCase):
    """行を作る助け。"""

    def test_数は桁をそろえる(self) -> None:
        self.assertEqual(gw_print.row("重量", 1.5, "kg").value, "1.50")

    def test_数に見える文字は丸めない(self) -> None:
        """ロット番号を 7131.00 にしない。"""
        self.assertEqual(
            gw_print.row("LotNo", "7131000", digits=None).value, "7131000")

    def test_無いものは棒で出す(self) -> None:
        self.assertEqual(gw_print.row("合紙", None).value, "－")


class RouteTests(GwWebTestCase):
    """`GET /report/gw`。**日報の紙と同じ道**(新しい窓で開いて Ctrl+P)。"""

    def paper(self, **extra):
        return self.get(f"/report/gw?{_query(**extra)}")

    def test_紙が出る(self) -> None:
        res = self.paper(packs="1")
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/html", res.headers["Content-Type"])
        self.assertIn("梱包資材重量計算結果", res.get_data(as_text=True))

    def test_印刷から開いたら印刷の画面を出す(self) -> None:
        """Ctrl+P を知らなくても刷れるように。"""
        body = self.paper(print="1").get_data(as_text=True)
        self.assertIn("window.print()", body.split("</div>")[-1])

    def test_開いただけなら勝手に印刷しない(self) -> None:
        body = self.paper().get_data(as_text=True)
        self.assertNotIn("setTimeout", body)

    def test_紙の上に印刷するボタン(self) -> None:
        """印刷の画面を閉じてしまっても、ここから刷れる。**紙には出ない。**"""
        body = self.paper().get_data(as_text=True)
        self.assertIn('onclick="window.print()">印刷する</button>', body)
        # 帯は日報の紙と共通(`reporting/paper.py`)。紙では消える
        self.assertIn("@media print { .toolbar { display: none !important; } }", body)
        self.assertIn(".generated-at { display: none; }", body)

    def test_梱包数を入れるとその段が出る(self) -> None:
        body = self.paper(packs="12").get_data(as_text=True)
        self.assertIn("12梱包ぶんの資材重量", body)

    def test_梱包数が無ければ段は出ない(self) -> None:
        """既定は1梱包 ── 計算結果だけの紙になる。"""
        body = self.paper().get_data(as_text=True)
        self.assertNotIn("梱包ぶんの資材重量", body)

    def test_打ち間違いでも紙は出る(self) -> None:
        """「あ」と入っていても止めない。1梱包として出す。"""
        res = self.paper(packs="あ")
        self.assertEqual(res.status_code, 200)
        self.assertNotIn("梱包ぶんの資材重量", res.get_data(as_text=True))

    def test_桁の多い梱包数でも上限で止まる(self) -> None:
        """**画面を通さず URL を直に叩かれても止まる。**

        画面の欄は3桁までですが、紙は GET なので URL を打てば何桁でも
        送れます。関門はサーバ側(`normalize_packs`)にあります。
        """
        for text in ("100000", "99999999999"):
            body = self.paper(packs=text).get_data(as_text=True)
            self.assertIn(f"{gw_print.MAX_PACKS}梱包ぶんの資材重量", body, text)
            # **紙も膨らまない** ── 1行しか出ないので大きさは変わらない
            self.assertLess(len(body), 20000, text)

    def test_計算できない入力は理由を出す(self) -> None:
        res = self.get("/report/gw?packs=5&thickness_mm=0&width_mm=81"
                       "&length_mm=1000&count=40&pack_height_mm=200")
        body = res.get_data(as_text=True)
        self.assertIn("板厚", body)
        self.assertNotIn("梱包ぶんの資材重量", body)

    def test_トークンが無ければ401(self) -> None:
        """業務データがそのまま載るので、画面のHTMLと同じ扱いにはしない。"""
        res = self.client.get(f"/report/gw?{_query(packs='1')}",
                              headers={"Host": "127.0.0.1"})
        self.assertEqual(res.status_code, 401)

    def test_製品情報が紙に載る(self) -> None:
        body = self.paper(material="A5052").get_data(as_text=True)
        self.assertIn("N7131T0", body)
        self.assertIn("A5052", body)

    def test_使わない資材はチェックのとおり(self) -> None:
        """`flags` に無い資材は紙でも「使わない」。"""
        body = self.paper(flags="dunplate").get_data(as_text=True)
        rows = re.findall(
            r'<tr class="unused"><td class="label">(.*?)<', body)
        self.assertIn("外装紙", rows)
        self.assertNotIn("ダンプレート", rows)

    def test_梱包数ぶんの段でも使わない資材は薄い(self) -> None:
        """上で「－」なのに下で「0.00」だと、同じ紙の中で言うことが違う。"""
        body = self.paper(packs="12", flags="dunplate").get_data(as_text=True)
        packs = body.split("梱包ぶんの資材重量", 1)[1]
        rows = re.findall(r'<tr class="unused"><td class="label">(.*?)<', packs)
        self.assertIn("外装紙", rows)
        self.assertIn("VCフィルム", rows)
        self.assertNotIn("ダンプレート", rows)
        # 合計の2つは常に出す(使う/使わないの話ではない)
        self.assertNotIn("資材計", rows)


class TruthyTests(GwWebTestCase):
    """クエリの真偽値。**`bool("0")` は True** なので、中身を見る。"""

    def _vc_unused(self, **extra) -> bool:
        body = self.get(f"/report/gw?{_query(**extra)}").get_data(as_text=True)
        return bool(re.search(
            r'<tr class="unused"><td class="label">VCフィルム', body))

    def test_0なら使わない(self) -> None:
        """ここを素通りさせると「VCを使わない」で開いた紙にVCが乗る。"""
        self.assertTrue(self._vc_unused(use_vc="0"))

    def test_falseでも使わない(self) -> None:
        self.assertTrue(self._vc_unused(use_vc="false"))

    def test_指定が無ければ使わない(self) -> None:
        self.assertTrue(self._vc_unused())


class ScreenTests(GwWebTestCase):
    """画面に梱包数の欄と下見ボタンがあるか。"""

    def test_梱包数を入れられる(self) -> None:
        body = self.get("/gw").get_data(as_text=True)
        self.assertIn('id="print-packs"', body)
        self.assertIn("梱包数", body)

    def test_計算前は押せない(self) -> None:
        """計算していない紙は空欄だらけ。**刷ってから気づくことになる。**"""
        body = self.get("/gw").get_data(as_text=True)
        self.assertRegex(body, r'id="print-gw"[^>]*disabled')

    def test_かたちの選択はもう無い(self) -> None:
        """**v3.21.0 の2択は外した。** 残っていると押しても何も変わらない。"""
        body = self.get("/gw").get_data(as_text=True)
        self.assertNotIn("data-print-format", body)

    def test_印刷はボタンそのもの(self) -> None:
        """「印刷と書いてあるだけでボタンにもなっていません」(v3.81.0)。

        以前は「印刷」が見出しの字で、押すボタンは灰色の「下見を開く」でした。
        """
        body = self.get("/gw").get_data(as_text=True)
        self.assertRegex(body, r'<button[^>]*id="print-gw"[^>]*>印刷</button>')
        self.assertNotIn("下見を開く", body)
        self.assertNotIn("gw-print__head", body)

    def test_押せない理由を字で出す(self) -> None:
        """吹き出し(title)だけだと、マウスを載せない人には読めない。"""
        body = self.get("/gw").get_data(as_text=True)
        self.assertIn('id="print-why"', body)
        self.assertIn("先に「計算」を押すと印刷できます", body)
        js = (Path(__file__).resolve().parent.parent / "app" / "static" / "js"
              / "views" / "gw.js").read_text(encoding="utf-8")
        self.assertIn('byId("print-why")', js)
        # 押したら、紙の窓がそのまま印刷の画面を出す
        self.assertIn('params.append("print", "1")', js)

    def test_内訳はコピーと言う(self) -> None:
        """「内訳を写すってどうなるんですか？」── 写す=クリップボードへのコピー。"""
        body = self.get("/gw").get_data(as_text=True)
        self.assertIn(">内訳をコピー</button>", body)
        self.assertNotIn("内訳を写す", body)


class SharedCalculationTests(GwWebTestCase):
    """**画面と紙が同じ計算を通る。** 数字が食い違う余地を作らない。"""

    def rates(self):
        """資材重量マスタの代わり。**0でない数字**で見ないと意味がない。

        マスタが読めない環境では全部0になり、「画面も紙も0」で一致して
        しまいます ── それでは同じ計算を通っている証しになりません。
        """
        from unittest.mock import patch

        from nippou.logic import gw_calculation as gw

        rate = gw.MaterialRate(unit_mass=2.5, coefficient=1.2)
        return patch("app.routes.gw._rates",
                     return_value=gw.MaterialRates(
                         dunplate=rate, outer_paper=rate, interleaf=rate,
                         band=rate, poly_sheet=rate, angle=rate,
                         hardboard=rate))

    def payload(self) -> dict:
        """`/api/gw/calculate` へ送る形。**クエリと同じ中身にする。**

        資材のチェックは、クエリでは ON のものを並べ、JSON では真偽値で
        渡します。ここがずれていると「画面と紙で数字が違う」に見えますが、
        実際は違う入力を比べているだけです(実際に一度ずれました)。
        """
        body = {k: v for k, v in INPUTS.items() if k != "flags"}
        used = set(INPUTS["flags"].split(","))
        body["flags"] = {name: name in used for name in (
            "dunplate", "outer_paper", "interleaf", "band", "poly_sheet",
            "angle", "hardboard")}
        return body

    def test_紙の数字は画面の数字と同じ(self) -> None:
        with self.rates():
            screen = self.post("/api/gw/calculate", self.payload()).get_json()
            body = self.get(
                f"/report/gw?{_query(packs='1')}").get_data(as_text=True)

        # 0で一致した、では確かめたことにならない
        self.assertGreater(screen["material_total"], 0)
        self.assertIn(f'{screen["tare_weight"]:.2f}', body)
        self.assertIn(f'{screen["material_total"]:.2f}', body)
        self.assertIn(f'{screen["gross_weight"]:.2f}', body)
        self.assertIn(f'{screen["weights"]["ダンプレート"]:.2f}', body)

    def test_梱包数ぶんの段も同じ表から(self) -> None:
        """**画面の表の12行目と、紙の「12梱包ぶん」が同じ数字。**

        掛け算を紙の側で書き直すと、画面の表と食い違う余地ができます。
        """
        with self.rates():
            screen = self.post("/api/gw/calculate", self.payload()).get_json()
            body = self.get(
                f"/report/gw?{_query(packs='12')}").get_data(as_text=True)

        row12 = screen["per_pack"]["rows"][11]     # 表の12梱包の行
        self.assertGreater(row12[0], 0)
        for value in row12:
            self.assertIn(f"{value:.2f}", body)

    def test_計算も印刷も同じ断りを出す(self) -> None:
        bad = {"thickness_mm": "0", "width_mm": "81", "length_mm": "1000",
               "count": "40", "pack_height_mm": "200"}
        res = self.post("/api/gw/calculate", bad)
        self.assertEqual(res.status_code, 422)
        message = res.get_json()["error"]["message"]

        from urllib.parse import urlencode
        paper = self.get(
            f"/report/gw?packs=1&{urlencode(bad)}").get_data(as_text=True)
        self.assertIn(message, paper)


if __name__ == "__main__":
    unittest.main()
