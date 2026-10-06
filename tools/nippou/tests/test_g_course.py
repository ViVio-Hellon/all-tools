"""Gコースのロット ── GW も BOX最終実績の寸法 / Gコースだったことが分かる (v4.17.0)

    GWも同様にしてください、ただし日報入力もGWもGのコースがあったことはわかるようにしてください

【約束】
    ・Gコース(設計_設備ｺｰｽ に GCT / GFS / GSS)の寸法は BOX最終実績。日報入力・GW・
      包装仕様の注意(寸法の条件)が**同じ寸法**を見る
    ・BOX最終実績の寸法が無ければ製造の寸法のまま。そのときも Gコースだったことは言う
    ・日報入力: 引いたときに一言、寸法の欄に「G」。行に控えるので**開き直しても出る**。
      打ち直した・引けなかった・Gでないロットなら消える。控えは共有へは出ない
    ・GW: 寸法を BOX最終実績で入れ、寸法の上に一言(製造の寸法も並べる)。紙にも書く
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import g_course  # noqa: E402
from tests._gw_master import GwWebTestCase  # noqa: E402
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402
from tests.test_gw_print import INPUTS  # noqa: E402
from tests.test_web_entry_lot import HIKI_COLUMNS, LOT_COLUMNS, ODR_COLUMNS  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MADE = (20.0, 1528.0, 3053.0)
BOX = (8.0, 1250.0, 2500.0)


def lot(course: str, box=BOX, made=MADE):
    return SimpleNamespace(course=course, thickness_mm=made[0], width_mm=made[1],
                           length_mm=made[2], box_thickness_mm=box[0],
                           box_width_mm=box[1], box_length_mm=box[2])


class LogicTests(unittest.TestCase):
    def test_3つのコースを部分一致で(self) -> None:
        for course, found in (("A-GCT-B", "GCT"), ("GFS", "GFS"), ("XGSSX", "GSS"),
                              ("HOT-NORMAL", ""), ("", ""), ("G-CT", "")):
            with self.subTest(course=course):
                self.assertEqual(g_course.find_course(course), found)

    def test_Gコースは_BOX最終実績の寸法(self) -> None:
        found = g_course.from_lot(lot("GSS-A"))
        self.assertTrue(found.used_box)
        self.assertEqual(found.size, BOX)
        self.assertEqual(found.stored, "GSS")
        self.assertEqual(found.label, "Gコース(GSS)")
        self.assertEqual(found.message, "Gコース(GSS)のロットです。寸法は BOX最終実績 "
                                        "8.000×1250.0×2500.0(製造 20.000×1528.0×3053.0)")
        self.assertEqual(g_course.product_size(lot("GSS-A")), BOX)

    def test_BOX最終実績が無ければ製造の寸法_でもGコースとは言う(self) -> None:
        found = g_course.from_lot(lot("GCT", box=(0.0, 0.0, 0.0)))
        self.assertFalse(found.used_box)
        self.assertEqual(found.size, MADE)
        self.assertEqual(found.stored, "GCT/製造")
        self.assertIn("Gコース(GCT)のロットです", found.message)
        self.assertIn("製造の寸法 20.000×1528.0×3053.0 のまま", found.message)

    def test_Gでなければ製造の寸法で_何も言わない(self) -> None:
        self.assertIsNone(g_course.from_lot(lot("HOT")))
        self.assertEqual(g_course.product_size(lot("HOT")), MADE)

    def test_行の印は控えから(self) -> None:
        self.assertIsNone(g_course.row_mark(""))
        self.assertEqual(g_course.row_mark("GSS"),
                         {"text": "G", "course": "GSS",
                          "title": "Gコース(GSS)のロット ── 寸法は BOX最終実績"})
        self.assertIn("製造の寸法のまま", g_course.row_mark("GCT/製造")["title"])
        self.assertEqual(g_course.row_mark("GCT/製造")["course"], "GCT")

    def test_LotInfo_も同じ寸法(self) -> None:
        from nippou.access_bridge.gw_master import BOX_COURSES, LotInfo

        info = LotInfo(lot_no="H5422S0", order_no="", inspection_no="", material="52S",
                       temper="R", thickness_mm=MADE[0], width_mm=MADE[1], length_mm=MADE[2],
                       usage_code="", course="GFS-1", customer="", delivery="", sender="",
                       box_thickness_mm=BOX[0], box_width_mm=BOX[1], box_length_mm=BOX[2])
        self.assertEqual(info.product_size(), BOX)
        self.assertEqual(info.box_course, "GFS")
        self.assertEqual(BOX_COURSES, g_course.COURSES)

    def test_包装仕様の注意も製品の寸法で見る(self) -> None:
        """寸法の条件が BOX最終実績の寸法にだけ当たる行 → Gコースなら出る。"""
        from nippou.logic.pack_note import NoteRow
        from nippou.services import lot_lookup

        row = NoteRow(pack_spec_no="1P0001", comment="BOXの注意", shape="板", flag="寸法",
                      thickness_low="7", thickness_high="9", width_low="1200",
                      width_high="1300", length_low="2400", length_high="2600")
        order = SimpleNamespace(pack_spec_no="1P0001", delivery="", customer="")
        with mock.patch("nippou.access_bridge.pack_note_master.load_notes",
                        return_value=[row]):
            g_lot = lot("GSS-A")
            g_lot.usage_code = g_lot.delivery = g_lot.customer = ""
            self.assertTrue(lot_lookup._pack_note(g_lot, order).has_note)
            plain = lot("HOT")
            plain.usage_code = plain.delivery = plain.customer = ""
            self.assertFalse(lot_lookup._pack_note(plain, order).has_note)

    def test_控えは共有へ出ない(self) -> None:
        """指紋にも、共有の日報管理の列にも入らない(引当番号と同じ)。"""
        from nippou.access_bridge import sqlite_backend
        from nippou.logic import fingerprint

        self.assertNotIn("box_course", fingerprint.DETAIL_FIELDS)
        self.assertNotIn("box_course", [c.lower() for c in sqlite_backend.DETAIL_COLUMNS])


class _Masters:
    """SIKALOT / SIKAHIKI / SIKAODR を本物の形で(BOX最終実績の列つき)。"""

    lot_course = "GSS-A"
    final_dims: tuple = BOX

    def make_masters(self) -> None:
        self.ref = self.tmp / "ref"
        self.ref.mkdir(exist_ok=True)
        self._write("SIKALOT.sqlite3", LOT_COLUMNS + (
            "BOX最終実績_枚本数", "BOX最終実績_板厚", "BOX最終実績_板幅", "BOX最終実績_板丈"), [(
                "H5422S0", "AB2234", "K1", "52S", "R", *MADE, "H283", "JISNﾌﾗﾂﾄANF",
                self.lot_course, "取引先", "納入先", "送り先", "9", "0", "C1",
                "2874", *self.final_dims)])
        self._write("SIKAHIKI.sqlite3", HIKI_COLUMNS,
                    [("H5422S0", "AB2234", 100, "", "60717001")])
        self._write("SIKAODR.sqlite3", ODR_COLUMNS, [
            ("AB2234", "VE-20NF", "", "1", 250.04314, "取引先", "納入先", "送り先",
             "1P1186", "")])
        self.post("/api/settings/paths", {"gw_reference_dir": str(self.ref), "password": "nisk"})

    def _write(self, name: str, columns, rows) -> None:
        conn = sqlite3.connect(str(self.ref / name))
        with conn:
            cols = ", ".join(f'"{c}"' for c in columns)
            conn.execute(f'CREATE TABLE "仕掛" ({cols})')
            conn.executemany(f'INSERT INTO "仕掛" VALUES ({", ".join("?" for _ in columns)})', rows)
        conn.close()


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class EntryTests(_Masters, WebTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_masters()

    def lookup(self, lot_no: str = "H5422S0", **row):
        return self.post("/api/entry/lot", {"rows": {"1": {"LOT": lot_no, **row}},
                                            "row": 1}).get_json()

    def test_引いたときに言い_行に控え_寸法の欄に印(self) -> None:
        body = self.lookup()
        self.assertEqual(body["rows"]["1"]["SIZ"], "8.000×1250.0×2500.0")
        g = body["lot"]["g_course"]
        self.assertEqual((g["course"], g["used_box"]), ("GSS", True))
        self.assertIn("製造 20.000×1528.0×3053.0", g["message"])
        self.assertEqual(body["rows"]["1"]["box_course"], "GSS")
        self.assertEqual(body["g_marks"]["1"]["text"], "G")
        self.assertIn("Gコース(GSS)", body["g_marks"]["1"]["title"])

    def test_保存して開き直しても印が出る(self) -> None:
        found = self.lookup()
        self.post("/api/entry/save", {"rows": found["rows"]})
        _, details = self.repo().load(*self.repo().list_keys()[0])
        self.assertEqual(details[0].box_course, "GSS")
        page = self.get("/").get_data(as_text=True)
        self.assertIn('data-family="box_course" data-row="1"', page)   # 持ち回る
        start = page.index('data-gmark-row="1"')
        tag = page[start:page.index(">", start)]
        self.assertIn('title="Gコース(GSS)のロット ── 寸法は BOX最終実績"', tag)
        self.assertNotIn("hidden", tag)
        start = page.index('data-gmark-row="2"')
        self.assertIn("hidden", page[start:page.index(">", start)])

    def test_打ち直したら消える(self) -> None:
        for lot_no in ("H5422", "N9999T0", ""):          # 7桁に足りない / 無いロット / 空
            with self.subTest(lot_no=lot_no):
                body = self.lookup(lot_no, box_course="GSS")
                self.assertEqual(body["rows"]["1"]["box_course"], "")
                self.assertEqual(body["g_marks"], {})

    def test_画面が描く(self) -> None:
        js = (ROOT / "app" / "static" / "js" / "views" / "entry.js").read_text(encoding="utf-8")
        self.assertIn("paintGMarks(view.g_marks)", js)
        self.assertIn("lot.g_course.message", js)


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class EntryPlainTests(EntryTests):
    lot_course = "HOT-NORMAL"

    def test_引いたときに言い_行に控え_寸法の欄に印(self) -> None:
        body = self.lookup()
        self.assertEqual(body["rows"]["1"]["SIZ"], "20.000×1528.0×3053.0")
        self.assertIsNone(body["lot"]["g_course"])
        self.assertEqual(body["rows"]["1"]["box_course"], "")
        self.assertEqual(body["g_marks"], {})

    def test_保存して開き直しても印が出る(self) -> None:
        found = self.lookup()
        self.post("/api/entry/save", {"rows": found["rows"]})
        _, details = self.repo().load(*self.repo().list_keys()[0])
        self.assertEqual(details[0].box_course, "")


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class EntryNoBoxTests(EntryTests):
    final_dims = (None, None, None)

    def test_引いたときに言い_行に控え_寸法の欄に印(self) -> None:
        body = self.lookup()
        self.assertEqual(body["rows"]["1"]["SIZ"], "20.000×1528.0×3053.0")
        self.assertFalse(body["lot"]["g_course"]["used_box"])
        self.assertEqual(body["rows"]["1"]["box_course"], "GSS/製造")
        self.assertIn("製造の寸法のまま", body["g_marks"]["1"]["title"])

    def test_保存して開き直しても印が出る(self) -> None:
        found = self.lookup()
        self.post("/api/entry/save", {"rows": found["rows"]})
        _, details = self.repo().load(*self.repo().list_keys()[0])
        self.assertEqual(details[0].box_course, "GSS/製造")


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class GwTests(_Masters, WebTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_masters()

    def test_寸法は_BOX最終実績_と一言(self) -> None:
        body = self.post("/api/gw/lot", {"lot_no": "H5422S0"}).get_json()
        self.assertEqual(body["size"], {"thickness_mm": 8.0, "width_mm": 1250.0,
                                        "length_mm": 2500.0})
        self.assertEqual(body["g_course"]["course"], "GSS")
        self.assertIn("製造 20.000×1528.0×3053.0", body["g_course"]["message"])
        self.assertEqual(body["lot"]["thickness_mm"], 20.0)   # 引いた値そのものは残す

    def test_画面に置き場所がある(self) -> None:
        page = self.get("/gw").get_data(as_text=True)
        self.assertRegex(page, r'id="g-course-note"[^>]*hidden')
        js = (ROOT / "app" / "static" / "js" / "views" / "gw.js").read_text(encoding="utf-8")
        self.assertIn("body.size.thickness_mm", js)
        self.assertNotIn("body.lot.thickness_mm", js)        # 製造の寸法を直に入れない
        self.assertIn("paintGCourse(body.g_course)", js)
        self.assertIn('params.append("g_course", lastGCourse.stored)', js)



@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class GwPrintTests(GwWebTestCase):
    def test_紙にも書く(self) -> None:
        from urllib.parse import urlencode

        page = self.get("/report/gw?" + urlencode({**INPUTS, "g_course": "GSS"})
                        ).get_data(as_text=True)
        self.assertIn("LotNo N7131T0 / オーダーNo OD-1 / Gコース(GSS)", page)
        self.assertIn("Gコース(GSS)のロット ── 寸法は BOX最終実績", page)
        plain = self.get("/report/gw?" + urlencode(INPUTS)).get_data(as_text=True)
        self.assertNotIn("Gコース", plain)


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class GwPlainTests(GwTests):
    lot_course = "HOT-NORMAL"

    def test_寸法は_BOX最終実績_と一言(self) -> None:
        body = self.post("/api/gw/lot", {"lot_no": "H5422S0"}).get_json()
        self.assertEqual(body["size"], {"thickness_mm": 20.0, "width_mm": 1528.0,
                                        "length_mm": 3053.0})
        self.assertIsNone(body["g_course"])


if __name__ == "__main__":
    unittest.main()
