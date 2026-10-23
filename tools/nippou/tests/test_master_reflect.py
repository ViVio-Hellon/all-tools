"""マスタを直したら、次の計算・表示から新しい値になるか (v4.44.3)

【利用者から】

    マスタを直しても前の中身のまま動く・表示する事がないかのチェックをしてください。
    例：資材重量 変更後 前の重量のまま計算

サーバ側は、どのマスタも**要求のたびに読み直します**(共有のファイルは手元の写しを読み、
写しは元の大きさか更新時刻が変われば作り直す ── `source_db.local_copy`)。それが崩れて
いないことを、直し方の2つの場面で確かめます:

    (a) この端末のマスタ管理で直す(`/api/master/row/save`。書いたら写しの控えを捨てる)
    (b) ほかのPCが共有のマスタを直す(このツールの道を通らない)

画面の側(開いたままの画面の欄・窓の一覧)は `test_master_reflect_browser`。直の時刻は
`test_shift_master_reflect`(手元のDBへ写すので、30秒ごとに更新時刻を見る)。
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._gw_master import GwWebTestCase  # noqa: E402

#: GW計算の入力(資材は全部使う・VCは上面だけ)
GW_INPUT = {
    "lot_no": "N7131T0", "order_no": "OD-1",
    "thickness_mm": "1.99", "width_mm": "81", "length_mm": "1000",
    "count": "40", "vertical_bands": "2", "horizontal_bands": "2",
    "pack_height_mm": "200", "pallet_weight_kg": "18",
    "stack_pattern": "1", "band_kind": "シール無", "input_weight_kg": "1200",
    "use_vc": True, "vc_side_a": True, "vc_side_b": False, "vc_name_a": "テストVC",
    "flags": {name: True for name in ("dunplate", "outer_paper", "interleaf", "band",
                                       "poly_sheet", "angle", "hardboard")},
}


class GwMaterialMasterReflectTests(GwWebTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.master = self.tmp / "ref" / "梱包資材マスタ.sqlite3"

    def calc(self) -> dict:
        res = self.post("/api/gw/calculate", GW_INPUT)
        self.assertEqual(res.status_code, 200, res.get_json())
        return res.get_json()

    def rowid(self, table: str, column: str, name: str) -> int:
        conn = sqlite3.connect(str(self.master))
        try:
            return conn.execute(f'SELECT rowid FROM "{table}" WHERE "{column}" = ?', (name,)).fetchone()[0]
        finally:
            conn.close()

    def edit_here(self, table: str, column: str, name: str, values: dict) -> None:
        self.assertEqual(self.post("/api/master/unlock",
                                   {"enable": True, "password": "nisk"}).status_code, 200)
        res = self.post("/api/master/row/save", {"file": "material", "table": table,
                                                  "key": self.rowid(table, column, name),
                                                  "values": values})
        self.assertEqual(res.status_code, 200, res.get_json())

    def other_pc_edits(self, table: str, column: str, name: str, values: dict) -> None:
        conn = sqlite3.connect(str(self.master))
        with conn:
            for key, value in values.items():
                conn.execute(f'UPDATE "{table}" SET "{key}" = ? WHERE "{column}" = ?', (value, name))
        conn.close()

    def test_この端末で資材重量を直すと_次の計算から新しい重量(self) -> None:
        before = self.calc()
        self.edit_here("資材重量", "梱包資材名", "ダンプレート", {"単位質量": "0.5"})
        after = self.calc()
        # 0.250 → 0.5 で、ダンプレートの重量は2倍(画面の数字は小数2桁に丸めてある)
        self.assertGreater(after["weights"]["ダンプレート"], before["weights"]["ダンプレート"])
        self.assertAlmostEqual(after["weights"]["ダンプレート"], before["weights"]["ダンプレート"] * 2,
                               delta=0.02)
        self.assertGreater(after["gross_weight"], before["gross_weight"])
        # 記録にも、新しい値で計算したことが残る
        self.assertEqual(after["record"]["material"]["path"], str(self.master))

    def test_ほかのPCが資材重量とVC重量を直すと_次の計算から新しい重量(self) -> None:
        before = self.calc()
        self.other_pc_edits("資材重量", "梱包資材名", "ハードボード", {"係数": "2"})
        self.other_pc_edits("VC重量", "品名", "テストVC", {"単位質量": "0.1"})
        after = self.calc()
        for name in ("ハードボード", "VCフィルム"):
            self.assertGreater(after["weights"][name], before["weights"][name], name)
            self.assertAlmostEqual(after["weights"][name], before["weights"][name] * 2, delta=0.02)

    def test_計算したあとに直されても_紙は新しい重量で出す(self) -> None:
        """紙(`/report/gw`)は入力から計算し直す ── 画面で前に押した数字を写さない。"""
        from urllib.parse import urlencode

        self.calc()
        self.other_pc_edits("資材重量", "梱包資材名", "ダンプレート", {"単位質量": "0.5"})
        fresh = self.calc()
        query = {k: v for k, v in GW_INPUT.items() if k != "flags"}
        query["flags"] = ",".join(GW_INPUT["flags"])
        paper = self.get(f"/report/gw?{urlencode(query)}").get_data(as_text=True)
        self.assertIn(f'{fresh["material_total"]:.2f}', paper)
        self.assertIn(f'{fresh["weights"]["ダンプレート"]:.2f}', paper)

    def test_計算の答えの印は_マスタを直すと変わる(self) -> None:
        """開いている画面は、答えの `master_stamp` と `/api/gw/master` を見比べて計算し直す。"""
        import os

        first = self.calc()["master_stamp"]
        self.assertTrue(first)
        self.assertEqual(self.get("/api/gw/master").get_json()["stamp"], first)
        self.other_pc_edits("資材重量", "梱包資材名", "ダンプレート", {"単位質量": "0.5"})
        st = self.master.stat()
        os.utime(self.master, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))   # 同じ刻みでも変わるように
        now = self.get("/api/gw/master").get_json()["stamp"]
        self.assertNotEqual(now, first)
        self.assertEqual(self.calc()["master_stamp"], now)

    def test_VCの品名を足すと_次に開いた画面の選ぶ欄に出る(self) -> None:
        self.assertNotIn("新VC", self.get("/gw").get_data(as_text=True))
        conn = sqlite3.connect(str(self.master))
        with conn:
            conn.execute('INSERT INTO "VC重量" VALUES ("9", "新VC", "0.07", "1")')
        conn.close()
        self.assertIn('<option value="新VC">', self.get("/gw").get_data(as_text=True))


class StaffAndStopReasonReflectTests(GwWebTestCase):
    """作業者の名簿・停止理由は、窓を開くたびに引く口(`/api/staff/members`・`/api/formstop/reasons`)。"""

    def setUp(self) -> None:
        super().setUp()
        ref = self.tmp / "ref"
        conn = sqlite3.connect(str(ref / "梱包資材マスタ.sqlite3"))
        with conn:
            conn.execute('CREATE TABLE "班員名簿" ("管理番号", "苗字", "班", "名前", "読み", "担当ライン")')
            conn.execute('INSERT INTO "班員名簿" VALUES (1, "山田", "A", "山田", "やまだ", "L-1")')
        conn.close()
        self.transmission = ref / "伝送用ファイル.sqlite3"
        conn = sqlite3.connect(str(self.transmission))
        with conn:
            for table in ("作業停止時間内訳_1", "作業停止時間内訳_2", "作業停止時間内訳_3"):
                conn.execute(f'CREATE TABLE "{table}" ("内訳", "内訳番号")')
            conn.execute('INSERT INTO "作業停止時間内訳_1" VALUES ("段取り", "1")')
        conn.close()

    def names(self) -> list[str]:
        teams = self.get("/api/staff/members").get_json()["teams"]
        return sorted(n for t in teams for n in t["names"])

    def codes(self) -> list[str]:
        cats = self.get("/api/formstop/reasons").get_json()["categories"]
        return sorted(r["code"] for c in cats for r in c["reasons"])

    def test_ほかのPCが直すと_次に引いたときから新しい一覧(self) -> None:
        self.assertEqual(self.names(), ["山田"])
        self.assertEqual(self.codes(), ["1"])
        conn = sqlite3.connect(str(self.tmp / "ref" / "梱包資材マスタ.sqlite3"))
        with conn:
            conn.execute('UPDATE "班員名簿" SET "名前" = "山田太郎"')
        conn.close()
        conn = sqlite3.connect(str(self.transmission))
        with conn:
            conn.execute('UPDATE "作業停止時間内訳_1" SET "内訳番号" = "3"')
        conn.close()
        self.assertEqual(self.names(), ["山田太郎"])
        self.assertEqual(self.codes(), ["3"])
        # 日報入力の停止理由の選ぶ欄(画面を開いたときに描く)も新しい一覧
        self.assertIn('<option value="3"', self.get("/").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
