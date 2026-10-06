"""残りの画面(Web版)のテスト: GW計算 / 人員 / 全停 / 集計グラフ / 印刷 / 設定

Access への実接続はこの環境では行えないので、**取り込みが失敗しても
画面が落ちない**ことを中心に確かめる(「他ラインを落とさない」という
tkinter版からの最優先要件は、Web版でも同じ形で守る)。
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._gw_master import GwWebTestCase
from tests._web import WebTestCase


def _header(**overrides):
    from nippou.db.models import HeaderRecord

    base = dict(report_date="2026年8月3日", line="L-1", shift="1直", page=1,
                worker="山田", count="10", weight_kg="1000")
    base.update(overrides)
    return HeaderRecord(**base)


class GwTests(GwWebTestCase):
    def test_画面が出る(self) -> None:
        res = self.get("/gw")
        self.assertEqual(res.status_code, 200)
        self.assertIn("梱包資材重量計算", res.get_data(as_text=True))

    #: 通る入力ひと揃い。**必須の欄が全部入っている**
    #: (VBA `CommandButton2_Click` の計算前チェックを通る組み合わせ)
    OK = {
        "thickness_mm": "1.0", "width_mm": "1000", "length_mm": "2000",
        "count": "10", "vertical_bands": "2", "horizontal_bands": "2",
        "pack_height_mm": "100", "pallet_weight_kg": "20",
    }

    def test_資材重量マスタが読めなければ断る(self) -> None:
        """**0kgで計算を続けない。** パレット重量だけの軽いGWになります。

        VBA も「資材重量なし」で止めていました。
        """
        (self.tmp / "ref" / "梱包資材マスタ.sqlite3").unlink()
        from nippou import source_db
        source_db.forget()
        res = self.post("/api/gw/calculate", dict(self.OK))
        self.assertEqual(res.status_code, 422)
        self.assertIn("資材重量なし", res.get_json()["error"]["message"])

    def test_選んだバンドがマスタに無ければ断る(self) -> None:
        from tests import _gw_master
        _gw_master.write(self.tmp / "ref", skip=("PETバンド",))
        from nippou import source_db
        source_db.forget()
        res = self.post("/api/gw/calculate",
                        {**self.OK, "band_kind": "PETバンド"})
        self.assertEqual(res.status_code, 422)
        problems = res.get_json()["problems"]
        self.assertEqual([p["field"] for p in problems], ["band_kind"])
        self.assertIn("PETバンド", problems[0]["message"])

    def test_使わない資材ならマスタに無くても通る(self) -> None:
        from tests import _gw_master
        _gw_master.write(self.tmp / "ref", skip=("ハードボード",))
        from nippou import source_db
        source_db.forget()
        flags = {name: True for name in (
            "dunplate", "outer_paper", "interleaf", "band", "poly_sheet",
            "angle", "hardboard")}
        on = self.post("/api/gw/calculate", {**self.OK, "flags": flags})
        self.assertEqual(on.status_code, 422)
        off = self.post("/api/gw/calculate",
                        {**self.OK, "flags": {**flags, "hardboard": False}})
        self.assertEqual(off.status_code, 200)

    def test_計算できる(self) -> None:
        res = self.post("/api/gw/calculate", dict(self.OK))
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(body["stack_count"], 10)
        self.assertIn("weights", body)
        # パレット重量は風袋に必ず乗る
        self.assertGreaterEqual(body["tare_weight"], 20)

    def test_梱包高さが不正なら422で理由を返す(self) -> None:
        # 形は正しいが業務として計算できない(梱包高さ <= 板厚×枚数)
        res = self.post("/api/gw/calculate", {**self.OK, "pack_height_mm": "5"})
        self.assertEqual(res.status_code, 422)
        self.assertIn("message", res.get_json()["error"])

    def test_断りは全部まとめて返す(self) -> None:
        """VBA は1つずつ MsgBox で止めていたので、3か所間違っていると
        3回押し直すことになった。**全部返して押し直しを1回にする。**"""
        res = self.post("/api/gw/calculate", {})
        self.assertEqual(res.status_code, 422)
        problems = res.get_json()["problems"]
        fields = {p["field"] for p in problems}
        self.assertIn("thickness_mm", fields)
        self.assertIn("pallet_weight_kg", fields)
        self.assertGreater(len(problems), 3)

    def test_横バンド1本は断る(self) -> None:
        # VBA `Me.YOBA <= 1`。1本では梱包にならない
        res = self.post("/api/gw/calculate", {**self.OK, "horizontal_bands": "1"})
        self.assertEqual(res.status_code, 422)

    def test_EX2方向は横バンド4本(self) -> None:
        """縦バンドが1本(=EX2方向)なら横は4本でなければ計算させない。"""
        res = self.post("/api/gw/calculate",
                        {**self.OK, "vertical_bands": "1", "horizontal_bands": "2"})
        self.assertEqual(res.status_code, 422)
        self.assertIn("EX2", res.get_json()["error"]["message"])

    def test_EX2方向でも板幅1220から1300は通る(self) -> None:
        res = self.post("/api/gw/calculate",
                        {**self.OK, "vertical_bands": "1", "horizontal_bands": "2",
                         "width_mm": "1250"})
        self.assertEqual(res.status_code, 200)

    def test_EX2方向でも包装仕様7P0106は通る(self) -> None:
        res = self.post("/api/gw/calculate",
                        {**self.OK, "vertical_bands": "1", "horizontal_bands": "2",
                         "pack_spec_no": "7P0106"})
        self.assertEqual(res.status_code, 200)

    def test_積合せ有りで重量が空なら断る(self) -> None:
        # 言うだけで通すと、積合せぶんが抜けたGWが出る
        res = self.post("/api/gw/calculate",
                        {**self.OK, "use_combined_load": True})
        self.assertEqual(res.status_code, 422)
        self.assertIn("積合せ", res.get_json()["error"]["message"])

    def test_積合せは有りのときだけ足す(self) -> None:
        off = self.post("/api/gw/calculate",
                        {**self.OK, "combined_load_kg": "99"}).get_json()
        on = self.post("/api/gw/calculate",
                       {**self.OK, "use_combined_load": True,
                        "combined_load_kg": "99"}).get_json()
        self.assertAlmostEqual(on["tare_weight"], off["tare_weight"] + 99, places=2)

    def test_VCを使うのに品名が空なら断る(self) -> None:
        res = self.post("/api/gw/calculate",
                        {**self.OK, "use_vc": True, "vc_side_a": True})
        self.assertEqual(res.status_code, 422)
        self.assertIn("VC", res.get_json()["error"]["message"])

    def test_梱包数ごとの重量表が返る(self) -> None:
        """1梱包ぶんを掛けた表(VBA `RangePaste` → メイン C25:L124)。"""
        body = self.post("/api/gw/calculate", dict(self.OK)).get_json()
        table = body["per_pack"]
        self.assertEqual(len(table["rows"]), 100)
        self.assertEqual(len(table["columns"]), 10)
        self.assertEqual(table["columns"][-1], "風袋総重量")
        # N梱包ぶん = 1梱包ぶん × N。**表の数字は1行ずつ 0.01 に丸めて
        # 返す**ので、丸めたあとの1行目を3倍すると最大 0.02 ずれます
        # (以前はマスタが無く資材が全部0で、ずれようがありませんでした)
        self.assertAlmostEqual(table["rows"][2][-1], table["rows"][0][-1] * 3,
                               delta=0.02)
        # パレット重量は1梱包ごとに乗る
        self.assertAlmostEqual(table["rows"][0][-1], body["tare_weight"], places=2)

    def test_Aインプット重量が空ならGWを出さない(self) -> None:
        # VBAは GW欄そのものを隠していた。ここでは null で表す
        res = self.post("/api/gw/calculate", dict(self.OK))
        self.assertIsNone(res.get_json()["gross_weight"])

    def test_Aインプット重量があればGWが出る(self) -> None:
        res = self.post("/api/gw/calculate",
                        {**self.OK, "input_weight_kg": "500"})
        body = res.get_json()
        self.assertIsNotNone(body["gross_weight"])
        self.assertAlmostEqual(body["gross_weight"], body["tare_weight"] + 500, places=2)

    def test_積み形態2山なら積み枚数が半分に切り上がる(self) -> None:
        res = self.post("/api/gw/calculate",
                        {**self.OK, "count": "11", "stack_pattern": "2"})
        self.assertEqual(res.get_json()["stack_count"], 6)

    def test_LotNoが空なら400(self) -> None:
        res = self.post("/api/gw/lot", {"lot_no": ""})
        self.assertEqual(res.status_code, 400)

    def test_見つからないのは失敗ではない(self) -> None:
        # VBA も「データなし 手入力よろしく」と案内して続行させていた
        res = self.post("/api/gw/lot", {"lot_no": "NOSUCH"})
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertFalse(body["found"])
        self.assertIn("手入力", body["message"])

    def test_オーダーを引くと梱包仕様まで決まる(self) -> None:
        """VBA は `Hiki展開` の末尾で VC選択/合紙選択/バンド選択 を呼び、
        その場でチェックを付け直していた。**同じことをサーバがする。**"""
        from nippou.access_bridge.gw_master import OrderInfo

        order = OrderInfo(
            order_no="O1", vc_front="VC-A", vc_back="", has_interleaf=True,
            unit_weight_kg=1.5, customer="ﾅﾒｶﾜｱﾙﾐ(ｶ",
            delivery="KIZAN CHUANFU", sender="",
            pack_spec_no="7P0106", interleaf_flag="1")
        with patch("app.routes.gw.gw_master.search_order", return_value=order):
            body = self.post("/api/gw/order", {"order_no": "O1"}).get_json()

        self.assertTrue(body["found"])
        auto = body["auto"]
        self.assertTrue(auto["use_vc"])
        self.assertTrue(auto["vc_side_a"])
        self.assertFalse(auto["vc_side_b"])
        self.assertEqual(auto["vc_name_a"], "VC-A")
        self.assertEqual(auto["band_kind"], "PETバンド")
        self.assertEqual(auto["vertical_bands"], 1)
        # **どれが自動で入ったのか**も返す(画面が印を付けるため)
        self.assertIn("vc", auto["decided"])
        self.assertIn("band_kind", auto["decided"])

    def test_オーダーに包装仕様が入っていれば返る(self) -> None:
        # EX2方向チェックの例外に使うので、画面まで届く必要がある
        from nippou.access_bridge.gw_master import OrderInfo

        order = OrderInfo(order_no="O1", vc_front="", vc_back="",
                          has_interleaf=False, unit_weight_kg=0,
                          customer="ｺｸﾅｲ(ｶ", delivery="ｱｲﾁ", sender="",
                          pack_spec_no="7P0106")
        with patch("app.routes.gw.gw_master.search_order", return_value=order):
            body = self.post("/api/gw/order", {"order_no": "O1"}).get_json()
        self.assertEqual(body["order"]["pack_spec_no"], "7P0106")

    def test_オーダーが無くても落ちない(self) -> None:
        res = self.post("/api/gw/order", {"order_no": "NOSUCH"})
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.get_json()["found"])


class StaffTests(WebTestCase):
    def test_名簿が取れなくても画面は答える(self) -> None:
        # Access に繋がらない環境でも、名前は手入力できる
        res = self.get("/api/staff/members")
        self.assertEqual(res.status_code, 200)
        self.assertIn("teams", res.get_json())

    def test_名簿取得が例外でも落ちない(self) -> None:
        with patch("app.routes.staff.staff_master.load_staff_members",
                   side_effect=RuntimeError("想定外")):
            res = self.get("/api/staff/members")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["total"], 0)

    def test_選んだ名前が1つの文字列になる(self) -> None:
        res = self.post("/api/staff/apply", {"names": ["山田", "佐藤"]})
        self.assertEqual(res.status_code, 200)
        worker = res.get_json()["worker"]
        self.assertIn("山田", worker)
        self.assertIn("佐藤", worker)

    def test_誰も選ばなければ422(self) -> None:
        res = self.post("/api/staff/apply", {"names": []})
        self.assertEqual(res.status_code, 422)


class FormstopTests(WebTestCase):
    def test_理由が取れなくても画面は答える(self) -> None:
        res = self.get("/api/formstop/reasons")
        self.assertEqual(res.status_code, 200)
        self.assertIn("categories", res.get_json())

    def test_理由を選ばなければ422(self) -> None:
        res = self.post("/api/formstop/execute", {"reason": ""})
        self.assertEqual(res.status_code, 422)

    def test_記号が分からなければ断る(self) -> None:
        """**名前だけでは入れません。**

        欄に入るのは内訳マスタの記号です。名前("ﾌｫｰｸ待ち")を入れると
        保存前チェックが「停止内訳にない記号です」で断り、**そのページは
        以後どうやっても保存できなく**なります(消した形を保存するところで
        同じ断りに当たるため)。全停は直に1回きりなのでやり直しもできず、
        その直がまるごと手詰まりになります。
        """
        res = self.post("/api/formstop/execute",
                        {"reason": "ﾌｫｰｸ待ち", "worker": "山田"})
        self.assertEqual(res.status_code, 422)
        self.assertIn("記号", res.get_json()["error"]["message"])

    def test_全停入力は新しいページとして保存される(self) -> None:
        res = self.post("/api/formstop/execute",
                        {"reason": "テスト停止", "code": "ニ",
                         "worker": "山田"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["page"], 1)

        keys = self.repo().list_keys()
        self.assertEqual(len(keys), 1)

        # 1行目に直の開始〜終了と**記号**が入っていること
        report_date, line, shift, page = keys[0]
        _, details = self.repo().load(report_date, line, shift, page)
        row1 = details[0]
        self.assertEqual(row1.s, "ニ")          # 名前ではなく記号
        self.assertTrue(row1.kz)
        self.assertTrue(row1.th)

    def test_2回目は断る(self) -> None:
        """**直に1回きり。** 2回押せば全停が2つ並んだ日報ができてしまう。"""
        self.post("/api/formstop/execute",
                  {"reason": "1回目", "code": "ニ", "worker": "山田"})
        res = self.post("/api/formstop/execute",
                        {"reason": "2回目", "code": "リ", "worker": "山田"})
        self.assertEqual(res.status_code, 422)
        body = res.get_json()
        # どのページに入っているかまで言う(直しに行けるように)
        self.assertEqual(body["page"], 1)
        self.assertIn("第1ページ", body["error"]["message"])
        # ページは増えていない
        self.assertEqual(len(self.repo().list_keys()), 1)


class GraphTests(WebTestCase):
    def test_画面が出る(self) -> None:
        res = self.get("/graph")
        self.assertEqual(res.status_code, 200)
        self.assertIn("集計・グラフ", res.get_data(as_text=True))

    def test_期間を指定して推移が取れる(self) -> None:
        res = self.post("/api/graph/history",
                        {"start": "2026-08-01", "end": "2026-08-31"})
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(len(body["series"]), 3)
        self.assertIn("labels", body)

    def test_日付の形が違えば400(self) -> None:
        res = self.post("/api/graph/history", {"start": "not-a-date", "end": "x"})
        self.assertEqual(res.status_code, 400)

    def test_開始が終了より後なら422(self) -> None:
        res = self.post("/api/graph/history",
                        {"start": "2026-08-31", "end": "2026-08-01"})
        self.assertEqual(res.status_code, 422)

    def test_集計CSVを書き出せる(self) -> None:
        from pathlib import Path

        self.repo().save(_header(), [])
        res = self.post("/api/graph/csv", {"report_date": "2026年8月3日"})
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(body["count"], 1)
        self.assertTrue(Path(body["path"]).exists())

    def test_明細CSVも一緒に出る(self) -> None:
        """VBAの集計シートは「上の帯」と「明細の表」の2つだった。

        紙に載らない用途コード・納入先などは、明細のほうにしか出ない。
        """
        from pathlib import Path

        from nippou.db.models import DetailRecord

        header = _header()
        self.repo().save(header, [DetailRecord(
            report_date=header.report_date, line=header.line,
            shift=header.shift, page=1, row_no=1, lot="H5422S0",
            others3="ﾅﾒｶﾜｱﾙﾐ(ｶ")])
        body = self.post("/api/graph/csv",
                         {"report_date": "2026年8月3日"}).get_json()
        self.assertEqual(body["detail_count"], 1)
        path = Path(body["detail_path"])
        self.assertTrue(path.exists())
        text = path.read_text(encoding="utf-8-sig")
        self.assertIn("納入先", text.splitlines()[0])
        self.assertIn("ﾅﾒｶﾜｱﾙﾐ(ｶ", text.splitlines()[1])

    def test_データが無くてもCSVは出る(self) -> None:
        """**1日を名指ししたときは、空でも書く。**

        「その日を出せ」と言われて何も出ないと、書き出しに失敗したのか
        もともと空なのかが分かりません。
        """
        res = self.post("/api/graph/csv", {})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["count"], 0)

    def test_画面の期間ぶんを出す(self) -> None:
        """**押したときに見ている期間ぶん**を出す。

        以前はこのボタンが画面の開始日・終了日を見ておらず、押すと今日の
        ぶんだけが出ていました ── 期間を決めて見ているのにその期間の
        CSVが出せないと、過去のぶん(取り込んだ日など)を出す手立てが
        月まるごとしかありません。
        """
        from pathlib import Path

        for day in ("2026年8月3日", "2026年8月5日"):
            self.repo().save(_header(report_date=day), [])
        body = self.post("/api/graph/csv",
                         {"start": "2026-08-01", "end": "2026-08-31"}).get_json()
        self.assertEqual(body["day_count"], 2)
        # 1日3本(計算内容は年月のフォルダに1つ。v4.2.0)
        self.assertEqual(body["file_count"], 6)
        folders = sorted(Path(d["dir"]).name for d in body["days"])
        self.assertEqual(folders, ["03", "05"])
        # **操業していない日は書かない。** 空のCSVが並ぶと
        # 「出し忘れ」と「休み」が見分けられなくなる
        for day in body["days"]:
            self.assertTrue(Path(day["dir"]).is_dir())

    def test_期間に何も無ければそう言う(self) -> None:
        body = self.post("/api/graph/csv",
                         {"start": "2020-01-01", "end": "2020-01-31"}).get_json()
        self.assertEqual(body["day_count"], 0)
        self.assertIn("出せるものがありません", body["message"])

    def test_CSVも開始が終了より後なら422(self) -> None:
        res = self.post("/api/graph/csv",
                        {"start": "2026-08-31", "end": "2026-08-01"})
        self.assertEqual(res.status_code, 422)

    def test_期間を変えたらタイルの札も塗り直す(self) -> None:
        """**新しい数字に古い期間の札が付いたまま**にしない。

        タイルの見出しには「いつのぶんか」が出ています(`2026-08-01 〜
        2026-08-31` / `本日`)。数字だけ入れ替えると、期間を変えたことに
        画面から気づけません ── サーバは正しい札を返しているので、
        塗り忘れているかどうかは字面で見張ります。
        """
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        view = (root / "app/static/js/views/graph.js").read_text(encoding="utf-8")
        self.assertIn(".tile__note", view)

        # サーバ側が期間の札を返していること(画面が写す元)
        body = self.post("/api/graph/dashboard",
                         {"start": "2026-08-01", "end": "2026-08-31"}).get_json()
        note = next(t["note"] for t in body["tiles"]
                    if t["key"] == "cumulative_count")
        self.assertIn("2026-08-01", note)
        self.assertIn("2026-08-31", note)


class PrintTests(WebTestCase):
    def test_画面が出る(self) -> None:
        res = self.get("/records")
        self.assertEqual(res.status_code, 200)
        self.assertIn("記録を見る", res.get_data(as_text=True))

    def test_むかしの印刷画面は記録へ送る(self) -> None:
        """リンクやお気に入りを壊さない。404 は「壊れた」に見える。"""
        res = self.get("/print")
        self.assertEqual(res.status_code, 302)
        self.assertIn("/records", res.headers["Location"])

    def test_無いものを読むと見つからないと言う(self) -> None:
        res = self.post("/api/print/load", {
            "report_date": "2026年8月3日", "line": "L-1", "shift": "1直", "page": 1})
        self.assertFalse(res.get_json()["found"])

    def test_保存済みを読むと下見が出る(self) -> None:
        from nippou.db.models import DetailRecord

        self.repo().save(_header(), [
            DetailRecord(report_date="2026年8月3日", line="L-1", shift="1直",
                         page=1, row_no=1, lot="LOT1", zai="A"),
        ])
        res = self.post("/api/print/load", {
            "report_date": "2026年8月3日", "line": "L-1", "shift": "1直", "page": 1})
        body = res.get_json()
        self.assertTrue(body["found"])
        self.assertEqual(body["worker"], "山田")
        self.assertEqual(body["rows"][0]["lot"], "LOT1")

    def test_印刷用HTMLが出る(self) -> None:
        from nippou.db.models import DetailRecord

        self.repo().save(_header(), [
            DetailRecord(report_date="2026年8月3日", line="L-1", shift="1直",
                         page=1, row_no=1, lot="LOT1"),
        ])
        res = self.get("/report/nippou?report_date=2026年8月3日&line=L-1"
                       "&shift=1直&page=1")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn("LOT1", html)
        self.assertIn("山田", html)

    def test_印刷用HTMLもトークンが要る(self) -> None:
        # 業務データがそのまま載るので、画面のHTMLと同じ扱いにはしない
        res = self.client.get("/report/nippou?report_date=x&line=L-1&shift=1直",
                              headers={"Host": "127.0.0.1"})
        self.assertEqual(res.status_code, 401)

    def test_無いものを刷ろうとすると404(self) -> None:
        res = self.get("/report/nippou?report_date=なし&line=L-1&shift=1直&page=1")
        self.assertEqual(res.status_code, 404)


class SettingsTests(WebTestCase):
    def test_画面が出る(self) -> None:
        res = self.get("/settings")
        self.assertEqual(res.status_code, 200)
        self.assertIn("設定・管理者", res.get_data(as_text=True))

    def test_パスワードが違えば403(self) -> None:
        res = self.post("/api/settings/admin", {"enable": True, "password": "wrong"})
        self.assertEqual(res.status_code, 403)

    def test_正しいパスワードで管理者モードになる(self) -> None:
        from nippou.config import SETTINGS

        res = self.post("/api/settings/admin",
                        {"enable": True, "password": SETTINGS.admin_password})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["admin"])

    def test_管理者でなければ過去データを開けない(self) -> None:
        res = self.post("/api/settings/recall", {
            "report_date": "2026年8月3日", "line": "L-1", "shift": "1直", "page": 1})
        self.assertEqual(res.status_code, 403)

    def test_管理者でも無いデータは開けない(self) -> None:
        from nippou.config import SETTINGS

        self.post("/api/settings/admin",
                  {"enable": True, "password": SETTINGS.admin_password})
        res = self.post("/api/settings/recall", {
            "report_date": "2026年9月9日", "line": "L-1", "shift": "1直", "page": 1})
        self.assertEqual(res.status_code, 404)

    def test_管理者なら過去データを開ける(self) -> None:
        from nippou.config import SETTINGS

        self.repo().save(_header(), [])
        self.post("/api/settings/admin",
                  {"enable": True, "password": SETTINGS.admin_password})
        res = self.post("/api/settings/recall", {
            "report_date": "2026年8月3日", "line": "L-1", "shift": "1直", "page": 1})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["next"], "/")

    def test_Access反映が例外でも自ラインを落とさない(self) -> None:
        # tkinter版 `_push_to_access` と同じ最終防衛ライン
        with patch("app.routes.settings.pusher.push_pending",
                   side_effect=RuntimeError("想定外")):
            res = self.post("/api/settings/push", {})
        self.assertEqual(res.status_code, 500)
        self.assertIn("予期しないエラー", res.get_json()["error"]["message"])

    def test_Access反映は繋がらなくても応答を返す(self) -> None:
        res = self.post("/api/settings/push", {})
        self.assertIn(res.status_code, (200, 500))

    def test_時間マスタ取り込みが例外でも落ちない(self) -> None:
        with patch("nippou.access_bridge.importer.import_shift_times",
                   side_effect=RuntimeError("想定外")):
            res = self.post("/api/settings/sync-shift", {})
        self.assertEqual(res.status_code, 500)

    def test_時間マスタが取れなければ502(self) -> None:
        with patch("nippou.access_bridge.importer.import_shift_times",
                   return_value=None):
            res = self.post("/api/settings/sync-shift", {})
        self.assertEqual(res.status_code, 502)

    def test_時間マスタを取り込むと保存される(self) -> None:
        with patch("nippou.access_bridge.importer.import_shift_times",
                   return_value={"1": ("08:00", "17:00")}):
            res = self.post("/api/settings/sync-shift", {})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.repo().get_shift_times()["1"], ("08:00", "17:00"))


if __name__ == "__main__":
    unittest.main()
