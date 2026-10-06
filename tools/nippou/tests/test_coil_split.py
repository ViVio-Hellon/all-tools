"""コイル縦割・横縦割 (`LS4LOT`) と、注意表記が行に入るところ

VBA `SQLiteLot検索` の `'---- コイル縦横 ----` の部分。あちらはこの
ファイルだけ `SimpleArr(… "LS4LOT.accdb" …)` と Access のまま残って
いましたが、**ほかの参照と同じく sqlite3 として扱います**。

ここで守るのは:

    ・機側 / NS1 のときだけ引くこと(コイル形状。VBA は AIM も引いていたが v4.21.0 で外した)
    ・縦割・横縦割が others5 / others6 に入ること
    ・引けなくても**行は埋まる**こと(VBA も `GoTo skipCoilErr`)
    ・包装仕様の注意が etc 欄に入り、印より**上**に並ぶこと
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.access_bridge.gw_master import CoilSplit, LotInfo, OrderInfo
from nippou.logic import lot_fill
from nippou.logic.pack_note import PackNote


def lot(**values) -> LotInfo:
    base = dict(lot_no="H8082H1", order_no="AB1234", inspection_no="K2",
                material="F52S", temper="R", thickness_mm=1.5,
                width_mm=320.0, length_mm=0.0, usage_code="H176",
                course="", customer="取引先", delivery="納入先",
                sender="送り先", usage_name="ｼﾔ-ｼ", box_sheets="9",
                interleaf_flag="1", cast_no="C2")
    base.update(values)
    return LotInfo(**base)


def order(**values) -> OrderInfo:
    base = dict(order_no="AB1234", vc_front="V325NW", vc_back="",
                has_interleaf=False, unit_weight_kg=1626.87679,
                customer="取", delivery="納", sender="送",
                pack_spec_no="1P0001", interleaf_flag="0", export_flag="")
    base.update(values)
    return OrderInfo(**base)


class LineTests(unittest.TestCase):
    def test_引くのは機側とNS1だけ(self) -> None:
        """コイル形状の2つ。「AIMはコイル割り数不要です」(v4.21.0)。"""
        for line in ("機側", "NS1"):
            self.assertTrue(lot_fill.needs_coil_split(line), line)
        for line in ("AIM", "L-1", "LVC", "HVC", "トット", "バランサー", "中板", ""):
            self.assertFalse(lot_fill.needs_coil_split(line), line)

    def test_AIMは包み数の自動計算は残る(self) -> None:
        """割り数を外しても、包み数の自動計算(VBA の同じ `If`)の集まりは別。"""
        from nippou.constants import PACKAGE_CALC_LINES

        self.assertIn("AIM", PACKAGE_CALC_LINES)
        self.assertNotIn("AIM", lot_fill.COIL_SPLIT_LINES)

    def test_AIMではLS4LOTを開かない(self) -> None:
        from nippou.services import lot_lookup

        with patch("nippou.access_bridge.gw_master.search_coil_split") as search:
            self.assertIsNone(lot_lookup._coil_split("H8082H1", "AIM"))
            search.assert_not_called()
            lot_lookup._coil_split("H8082H1", "機側")
            search.assert_called_once()


class FillTests(unittest.TestCase):
    def test_縦割と横縦割がothers5と6へ入る(self) -> None:
        fill = lot_fill.build(
            line="機側", lot=lot(), allocation=None, order=order(),
            coil=CoilSplit(lot_no="H8082H1", vertical="2", horizontal="14"))
        self.assertEqual(fill.values["others5"], "2")
        self.assertEqual(fill.values["others6"], "14")

    def test_引けなければ空のまま行は埋まる(self) -> None:
        """VBA も `GoTo skipCoilErr` で続けていた。"""
        fill = lot_fill.build(line="機側", lot=lot(), allocation=None,
                              order=order(), coil=None)
        self.assertEqual(fill.values["others5"], "")
        self.assertEqual(fill.values["others6"], "")
        self.assertEqual(fill.values["others1"], "H176")   # ほかは埋まる

    def test_紙に載らない6つがそろう(self) -> None:
        fill = lot_fill.build(
            line="機側", lot=lot(), allocation=None, order=order(),
            coil=CoilSplit(vertical="2", horizontal="14"))
        self.assertEqual(
            [fill.values[f"others{n}"] for n in range(1, 7)],
            ["H176", "ｼﾔ-ｼ", "納入先", "1P0001", "2", "14"])


class NoteInRowTests(unittest.TestCase):
    def test_注意がetc欄に入る(self) -> None:
        note = PackNote(pack_spec_no="1C1296", shape="コイル",
                        comments=["コイル巻き、反時計方向"], rows=1)
        fill = lot_fill.build(line="機側", lot=lot(), allocation=None,
                              order=order(), note=note)
        self.assertEqual(fill.values["ET"], "コイル巻き、反時計方向")
        self.assertTrue(fill.note.is_coil)

    def test_注意が複数なら区切って並ぶ(self) -> None:
        """**改行は入れません**(1行の `<input>` が落としてしまう)。"""
        note = PackNote(pack_spec_no="1C1273", shape="コイル", rows=3,
                        comments=["リプラ", "明細を添付", "巻き向き"])
        fill = lot_fill.build(line="機側", lot=lot(), allocation=None,
                              order=order(), note=note)
        self.assertEqual(fill.values["ET"], "リプラ / 明細を添付 / 巻き向き")

    def test_注意は印より上に並ぶ(self) -> None:
        """**紙に出る並びを VBA と合わせる**(注意が上、印が下)。"""
        note = PackNote(pack_spec_no="1P1118", shape="板", rows=1,
                        comments=["ｽﾌﾟﾚｰﾏｰｷﾝｸﾞ"])
        fill = lot_fill.build(
            line="L-1", lot=lot(course="GFS"), allocation=None,
            order=order(export_flag="1"), note=note)
        text = fill.values["ET"]
        self.assertTrue(text.startswith("ｽﾌﾟﾚｰﾏｰｷﾝｸﾞ"), text)
        self.assertIn("EX", text)
        self.assertIn("外注出荷", text)

    def test_注意が無ければetcは印だけ(self) -> None:
        fill = lot_fill.build(line="L-1", lot=lot(course="GFS"),
                              allocation=None, order=order())
        self.assertEqual(fill.values["ET"], "外注出荷")
        self.assertFalse(fill.note.has_note)


class MasterTests(unittest.TestCase):
    """LS4LOT を実際に読む。**1ロット1行の表。**"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.db = self.tmp / "LS4LOT.sqlite3"
        conn = sqlite3.connect(str(self.db))
        conn.execute('CREATE TABLE "仕掛" ("ﾛｯﾄ番号" TEXT, "製造材質" TEXT,'
                     ' "当工程設計_縦割数" TEXT, "当工程設計_横割数" TEXT)')
        conn.executemany('INSERT INTO "仕掛" VALUES (?,?,?,?)', [
            ("N7131T0", "3SCA", "1", "3"),
            ("L715C50", "F52S", "2", "14"),
        ])
        conn.commit()
        conn.close()

        from nippou import source_db

        self._patch = patch.object(source_db, "_COPY_DIR", self.tmp / "cache")
        self._patch.start()
        (self.tmp / "cache").mkdir()
        source_db.forget()
        self.addCleanup(self._stop)

    def _stop(self) -> None:
        from nippou import source_db

        self._patch.stop()
        source_db.forget()

    def test_縦割と横縦割を引く(self) -> None:
        from nippou.access_bridge import gw_master

        found = gw_master.search_coil_split("L715C50", master_path=self.db)
        self.assertIsNotNone(found)
        self.assertEqual(found.vertical, "2")
        self.assertEqual(found.horizontal, "14")

    def test_無ければNone(self) -> None:
        from nippou.access_bridge import gw_master

        self.assertIsNone(
            gw_master.search_coil_split("XXXXXXX", master_path=self.db))

    def test_ファイルが無くても落ちない(self) -> None:
        """**入力は止めない。** 縦横が空になるだけ。"""
        from nippou.access_bridge import gw_master

        self.assertIsNone(gw_master.search_coil_split(
            "L715C50", master_path=self.tmp / "無い.sqlite3"))

    def test_共有を開かず写しから読む(self) -> None:
        from nippou.access_bridge import gw_master

        gw_master.search_coil_split("L715C50", master_path=self.db)
        self.assertTrue(list((self.tmp / "cache").glob("*.sqlite3")),
                        "写しがありません(共有を直接開いています)")


class ServiceTests(unittest.TestCase):
    """`services/lot_lookup` が、ラインを見て引くかどうか決めること。"""

    def test_コイルでないラインでは引きに行かない(self) -> None:
        from nippou.services import lot_lookup

        with patch("nippou.access_bridge.gw_master.search_coil_split") as fake:
            self.assertIsNone(lot_lookup._coil_split("H8082H1", "L-1"))
        fake.assert_not_called()

    def test_コイルのラインでは引きに行く(self) -> None:
        from nippou.services import lot_lookup

        with patch("nippou.access_bridge.gw_master.search_coil_split") as fake:
            fake.return_value = CoilSplit(vertical="2", horizontal="14")
            found = lot_lookup._coil_split("H8082H1", "機側")
        self.assertEqual(found.vertical, "2")

    def test_読めなくても止めない(self) -> None:
        from nippou.services import lot_lookup

        with patch("nippou.access_bridge.gw_master.search_coil_split",
                   side_effect=RuntimeError("想定外")):
            self.assertIsNone(lot_lookup._coil_split("H8082H1", "機側"))

    def test_包装仕様NOが無ければ注意も引かない(self) -> None:
        from nippou.services import lot_lookup

        with patch("nippou.access_bridge.pack_note_master.load_notes") as fake:
            note = lot_lookup._pack_note(lot(), order(pack_spec_no=""))
        fake.assert_not_called()
        self.assertFalse(note.has_note)

    def test_受注が引けていなければ注意も引かない(self) -> None:
        from nippou.services import lot_lookup

        with patch("nippou.access_bridge.pack_note_master.load_notes") as fake:
            note = lot_lookup._pack_note(lot(), None)
        fake.assert_not_called()
        self.assertFalse(note.found)

    def test_取引先と納入先は受注のほうを先に見る(self) -> None:
        """**引当で決まった相手**。ロット側は見込みが入ることがある。"""
        from nippou.logic.pack_note import NoteRow
        from nippou.services import lot_lookup

        rows = [NoteRow(pack_spec_no="1P0001", flag="取引先",
                        customer="受注の取引先", comment="出るはず")]
        with patch("nippou.access_bridge.pack_note_master.load_notes",
                   return_value=rows):
            note = lot_lookup._pack_note(
                lot(customer="ロットの取引先"), order(customer="受注の取引先"))
        self.assertEqual(note.comments, ["出るはず"])

    def test_受注が空ならロット側で補う(self) -> None:
        from nippou.logic.pack_note import NoteRow
        from nippou.services import lot_lookup

        rows = [NoteRow(pack_spec_no="1P0001", flag="取引先",
                        customer="ロットの取引先", comment="出るはず")]
        with patch("nippou.access_bridge.pack_note_master.load_notes",
                   return_value=rows):
            note = lot_lookup._pack_note(
                lot(customer="ロットの取引先"), order(customer=""))
        self.assertEqual(note.comments, ["出るはず"])


if __name__ == "__main__":
    unittest.main()
