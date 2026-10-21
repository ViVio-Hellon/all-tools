"""仕掛パス(v4.40.0): **第1パスは LS4LOT を分ける・名前も決める、第2パスは4つとも名前を決める**

    第1パス
      SIKALOT.sqlite3 / SIKAHIKI.sqlite3 / SIKAODR.sqlite3 用のパス
      LS4LOT.sqlite3 用のパス(SIKALOT / SIKAHIKI / SIKAODR 用とは分ける)。ファイル名の変更も認める
    第2パス
      SIKALOT / SIKAHIKI / SIKAODR / LS4LOT 用のパス。全てファイル名の変更も認める
    第1パス・第2パス含めて、読むときはローカルに保存してから読む

LS4LOT の置き場所が空なら仕掛ロット・引当・受注の第1パスと同じ(v4.39 までと同じ)。
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import WebTestCase  # noqa: E402
from tests.test_web_entry_lot import HIKI_COLUMNS, LOT_COLUMNS, ODR_COLUMNS  # noqa: E402
from tests.test_wip_second_dir import HIKI, ODR, _lot  # noqa: E402

COIL_COLUMNS = ("ﾛｯﾄ番号", "製造材質", "当工程設計_縦割数", "当工程設計_横割数")
#: 名前が LS4LOT でないファイル(v4.40.1)。BOX実績_作業開始_日付 が今の年月の行の BOX設計_縦割数・横割数
BOX_COIL_COLUMNS = ("ﾛｯﾄ番号", "BOX実績_作業開始_日付", "BOX設計_縦割数", "BOX設計_横割数")


class WipPathsTests(WebTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.first = self.tmp / "仕掛1"
        self.coil = self.tmp / "コイル1"
        self.second = self.tmp / "仕掛2"
        self.material = self.tmp / "梱包資材"
        for folder in (self.first, self.coil, self.second, self.material):
            folder.mkdir()
        from nippou import source_db

        source_db.forget()
        self.addCleanup(source_db.forget)

    # -- 小道具 ----------------------------------------------------------
    def use(self, **values: str) -> None:
        body = {"wip_master_dir": str(self.first), "material_master_dir": str(self.material),
                "password": "nisk", **values}
        res = self.post("/api/settings/paths", body)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))

    @staticmethod
    def write(path: Path, columns, rows) -> None:
        conn = sqlite3.connect(str(path))
        with conn:
            cols = ", ".join(f'"{c}"' for c in columns)
            marks = ", ".join("?" for _ in columns)
            conn.execute(f'CREATE TABLE "仕掛" ({cols})')
            conn.executemany(f'INSERT INTO "仕掛" VALUES ({marks})', rows)
        conn.close()

    def coil_file(self, path: Path, vertical: str = "2") -> None:
        """LS4LOT の形(名前が LS4LOT のファイルに置く)。"""
        self.write(path, COIL_COLUMNS, [("H5422S0", "F52S", vertical, "14")])

    def box_coil_file(self, path: Path, vertical: str = "2", *, when: str = "") -> None:
        """名前が LS4LOT でないファイルの形。作業開始は既定で今の年月。"""
        from datetime import datetime

        when = when or datetime.now().strftime("%Y-%m-01 00:01:56")
        self.write(path, BOX_COIL_COLUMNS, [("H5422S0", when, vertical, "14")])

    @staticmethod
    def coil_split(lot_no: str = "H5422S0"):
        from nippou.access_bridge import gw_master

        return gw_master.search_coil_split(lot_no)

    def settings(self):
        from nippou.config import SETTINGS

        return SETTINGS

    # -- 第1パス: LS4LOT を分ける ---------------------------------------
    def test_空ならLS4LOTは仕掛と同じ置き場所と名前(self) -> None:
        """v4.39 までと同じ。"""
        self.use()
        s = self.settings()
        self.assertEqual(s.coil_master_dir, self.first)
        self.assertEqual(s.gw_coil_master_paths[0], self.first / "LS4LOT.sqlite3")
        self.assertEqual(s.gw_lot_master_path, self.first / "SIKALOT.sqlite3")

    def test_LS4LOTだけ別の置き場所にできる(self) -> None:
        self.use(coil_master_dir=str(self.coil))
        s = self.settings()
        self.assertEqual(s.gw_coil_master_paths[0], self.coil / "LS4LOT.sqlite3")
        # 仕掛ロット・引当・受注は第1パスのまま
        self.assertEqual(s.gw_lot_master_path, self.first / "SIKALOT.sqlite3")
        self.assertEqual(s.gw_hiki_master_path, self.first / "SIKAHIKI.sqlite3")
        self.assertEqual(s.gw_order_master_path, self.first / "SIKAODR.sqlite3")
        self.coil_file(self.coil / "LS4LOT.sqlite3")
        self.coil_file(self.first / "LS4LOT.sqlite3", vertical="9")   # 仕掛の場所のは見ない
        self.assertEqual(self.coil_split().vertical, "2")

    def test_第1パスのLS4LOTは名前を変えられる(self) -> None:
        self.use(coil_master_dir=str(self.coil), coil_master_file="LS4LOT_機側.sqlite3")
        self.assertEqual(self.settings().gw_coil_master_paths[0], self.coil / "LS4LOT_機側.sqlite3")
        self.box_coil_file(self.coil / "LS4LOT_機側.sqlite3", vertical="3")   # 名前が LS4LOT でない
        self.assertEqual(self.coil_split().vertical, "3")

    def test_第1パスの仕掛ロット引当受注は名前を変えられない(self) -> None:
        """名前を決められるのは第1パスでは LS4LOT だけ(SIKALOT / SIKAHIKI / SIKAODR は決まった名前)。"""
        from nippou import config

        self.assertNotIn("wip_master_lot_file", config.PATH_KEYS)
        self.assertIn(config.KEY_COIL_FILE, config.PATH_KEYS)

    # -- 第2パス: 4つとも名前を決める -----------------------------------
    def test_第2パスは4つとも名前を決められる(self) -> None:
        self.use(wip_master_dir_2=str(self.second),
                 wip_master_2_lot_file="SIKALOT2.sqlite3",
                 wip_master_2_hiki_file="SIKAHIKI2.sqlite3",
                 wip_master_2_order_file="SIKAODR2.sqlite3",
                 wip_master_2_coil_file="LS4LOT2.sqlite3")
        s = self.settings()
        self.assertEqual(s.gw_lot_master_paths[1], self.second / "SIKALOT2.sqlite3")
        self.assertEqual(s.gw_hiki_master_paths[1], self.second / "SIKAHIKI2.sqlite3")
        self.assertEqual(s.gw_order_master_paths[1], self.second / "SIKAODR2.sqlite3")
        self.assertEqual(s.gw_coil_master_paths[1], self.second / "LS4LOT2.sqlite3")
        # 実際に引く: 第1パスには何も無い → 第2パスの名前を変えた4つで埋まる
        self.write(self.second / "SIKALOT2.sqlite3", LOT_COLUMNS, [_lot("H5422S0")])
        self.write(self.second / "SIKAHIKI2.sqlite3", HIKI_COLUMNS, [HIKI])
        self.write(self.second / "SIKAODR2.sqlite3", ODR_COLUMNS, [ODR])
        self.box_coil_file(self.second / "LS4LOT2.sqlite3", vertical="4")   # 名前が LS4LOT でない
        body = self.post("/api/entry/lot", {"rows": {"1": {"LOT": "H5422S0"}}, "row": 1,
                                            "hiki_no": ""}).get_json()
        self.assertEqual(body["lot"]["state"], "filled", body)
        self.assertEqual(body["rows"]["1"]["VC"], "A:VE-20NF_B:VE-20NF")
        self.assertEqual(self.coil_split().vertical, "4")

    def test_第2パスのLS4LOTの名前が空なら第1パスと同じ名前(self) -> None:
        self.use(coil_master_file="LS4LOT_機側.sqlite3", wip_master_dir_2=str(self.second))
        self.assertEqual(self.settings().gw_coil_master_paths[1], self.second / "LS4LOT_機側.sqlite3")

    def test_LS4LOTを探す順は_第1_第2_前の置き場所(self) -> None:
        self.use(coil_master_dir=str(self.coil), wip_master_dir_2=str(self.second),
                 wip_master_2_coil_file="LS4LOT2.sqlite3")
        self.assertEqual(self.settings().gw_coil_master_paths,
                         [self.coil / "LS4LOT.sqlite3", self.second / "LS4LOT2.sqlite3",
                          self.material / "LS4LOT.sqlite3"])
        self.coil_file(self.material / "LS4LOT.sqlite3", vertical="7")   # 前の置き場所だけにある
        self.assertEqual(self.coil_split().vertical, "7")

    # -- 名前で見る列を変える(v4.40.1) ---------------------------------
    def test_名前がLS4LOTなら当工程設計の割数(self) -> None:
        self.use()
        path = self.first / "LS4LOT.sqlite3"
        self.write(path, ("ﾛｯﾄ番号", "当工程設計_縦割数", "当工程設計_横割数", "BOX設計_縦割数",
                          "BOX実績_作業開始_日付"),
                   [("H5422S0", "2", "14", "99", "2001-01-01 00:00:00")])
        found = self.coil_split()
        self.assertEqual((found.vertical, found.horizontal), ("2", "14"))   # 日付も BOX設計 も見ない

    def test_名前がLS4LOTでなければ今の年月のBOX設計の割数(self) -> None:
        from datetime import datetime
        from nippou.access_bridge import gw_master

        self.use(coil_master_file="SIKALOT_コイル.sqlite3")
        self.write(self.first / "SIKALOT_コイル.sqlite3", BOX_COIL_COLUMNS, [
            ("H5422S0", "2026-09-30 23:59:59", "8", "80"),    # 先月は拾わない
            ("H5422S0", "2026-10-02 08:00:00", "3", "30"),
            ("H5422S0", "2026-10-21 00:01:56", "5", "50"),    # 今月でいちばん新しい
            ("H5422S0", "2025-10-21 00:01:56", "9", "90"),    # 去年の同じ月も拾わない
        ])
        found = gw_master.search_coil_split("H5422S0", now=datetime(2026, 10, 21, 9, 0))
        self.assertEqual((found.vertical, found.horizontal), ("5", "50"))

    def test_今の年月の行が無ければ次の置き場所を見る(self) -> None:
        from datetime import datetime
        from nippou.access_bridge import gw_master

        self.use(coil_master_file="SIKALOT_コイル.sqlite3", wip_master_dir_2=str(self.second))
        self.write(self.first / "SIKALOT_コイル.sqlite3", BOX_COIL_COLUMNS,
                   [("H5422S0", "2026-09-30 23:59:59", "8", "80")])            # 先月だけ
        self.write(self.second / "SIKALOT_コイル.sqlite3", BOX_COIL_COLUMNS, [])  # 2つ目は空
        self.box_coil_file(self.material / "SIKALOT_コイル.sqlite3", vertical="6",
                           when="2026/10/05 07:00:00")                       # `/` の日付も読む
        found = gw_master.search_coil_split("H5422S0", now=datetime(2026, 10, 21))
        self.assertEqual(found.vertical, "6")

    def test_今の年月の行がどこにも無ければNone(self) -> None:
        from datetime import datetime
        from nippou.access_bridge import gw_master

        self.use(coil_master_file="SIKALOT_コイル.sqlite3")
        self.write(self.first / "SIKALOT_コイル.sqlite3", BOX_COIL_COLUMNS,
                   [("H5422S0", "2026-09-30 23:59:59", "8", "80")])
        self.assertIsNone(gw_master.search_coil_split("H5422S0", now=datetime(2026, 10, 1)))

    def test_年月の読み方(self) -> None:
        from nippou.access_bridge.gw_master import is_ls4lot_name, year_month

        self.assertEqual(year_month("2026-10-21 00:01:56"), "2026-10")
        self.assertEqual(year_month("2026/9/3 7:00"), "2026-09")
        self.assertEqual(year_month(""), "")
        self.assertEqual(year_month(None), "")
        self.assertTrue(is_ls4lot_name(Path("LS4LOT.sqlite3")))
        self.assertTrue(is_ls4lot_name(Path("ls4lot.accdb")))
        self.assertFalse(is_ls4lot_name(Path("LS4LOT2.sqlite3")))
        self.assertFalse(is_ls4lot_name(Path("LS4LOT_機側.sqlite3")))

    # -- 画面 ------------------------------------------------------------
    def test_設定の画面に第1パスと第2パスの欄が出る(self) -> None:
        self.use(coil_master_dir=str(self.coil), coil_master_file="LS4LOT_機側.sqlite3",
                 wip_master_dir_2=str(self.second), wip_master_2_coil_file="LS4LOT2.sqlite3")
        paths = {v["key"]: v for v in self.get("/api/settings/state").get_json()["paths"]}
        for key in ("wip_master_dir", "coil_master_dir", "coil_master_file", "wip_master_dir_2",
                    "wip_master_2_lot_file", "wip_master_2_hiki_file", "wip_master_2_order_file",
                    "wip_master_2_coil_file"):
            self.assertIn(key, paths)
        self.assertEqual(paths["coil_master_file"]["value"], "LS4LOT_機側.sqlite3")
        self.assertEqual(paths["coil_master_file"]["resolved"], str(self.coil / "LS4LOT_機側.sqlite3"))
        self.assertEqual(paths["wip_master_2_coil_file"]["resolved"], str(self.second / "LS4LOT2.sqlite3"))

    def test_ファイル名の欄にフォルダは書けない(self) -> None:
        res = self.post("/api/settings/paths", {"coil_master_file": r"\\server\共有\LS4LOT.sqlite3",
                                                "password": "nisk"})
        self.assertEqual(res.status_code, 400)

    # -- ローカルに保存してから読む --------------------------------------
    def test_第1パス第2パスとも_共有のファイルを直接開かない(self) -> None:
        """仕掛の4つを第1パス・第2パスの両方から引いて、**sqlite3 で開いたのは手元の写しだけ**。"""
        self.use(coil_master_dir=str(self.coil), wip_master_dir_2=str(self.second),
                 wip_master_2_lot_file="SIKALOT2.sqlite3")
        # 第1パス: ロットだけ別のロット(→ 第2パスまで見に行く)、引当・受注・LS4LOT は第1パス
        self.write(self.first / "SIKALOT.sqlite3", LOT_COLUMNS, [_lot("H9999S0")])
        self.write(self.first / "SIKAHIKI.sqlite3", HIKI_COLUMNS, [HIKI])
        self.write(self.first / "SIKAODR.sqlite3", ODR_COLUMNS, [ODR])
        self.write(self.second / "SIKALOT2.sqlite3", LOT_COLUMNS, [_lot("H5422S0")])
        self.coil_file(self.coil / "LS4LOT.sqlite3")
        shares = (self.first, self.coil, self.second)
        opened: list[str] = []
        real_connect = sqlite3.connect

        def spy(target, *args, **kwargs):
            opened.append(str(target))
            return real_connect(target, *args, **kwargs)

        with patch("sqlite3.connect", side_effect=spy):
            body = self.post("/api/entry/lot", {"rows": {"1": {"LOT": "H5422S0"}}, "row": 1,
                                                "hiki_no": ""}).get_json()
            found = self.coil_split()
        self.assertEqual(body["lot"]["state"], "filled", body)
        self.assertIsNotNone(found)
        direct = [p for p in opened if any(str(share) in p for share in shares)
                  and "/cache/" not in p.replace("\\", "/")]
        self.assertEqual(direct, [], "共有のファイルを直接開いています")
        from nippou import source_db

        copied = {Path(k).name for k in source_db._COPIES}
        for name in ("SIKALOT.sqlite3", "SIKAHIKI.sqlite3", "SIKAODR.sqlite3", "SIKALOT2.sqlite3",
                     "LS4LOT.sqlite3"):
            self.assertIn(name, copied, f"{name} を写していません")

    def test_Accessの名前でも写してから読む(self) -> None:
        """ファイル名を決められるので `.accdb` も通る。**Access の道にも写しを渡す。**"""
        self.use(wip_master_dir_2=str(self.second), wip_master_2_lot_file="SIKALOT2.accdb")
        share = self.second / "SIKALOT2.accdb"
        share.write_bytes(b"Standard ACE DB" + b"\0" * 64)
        seen: list[Path] = []

        from nippou.access_bridge import gw_master
        from nippou.access_bridge.importer import ImportResult

        def fake_import(path, *args, **kwargs):
            seen.append(Path(path))
            return ImportResult(success=True, rows=[])

        with patch.object(gw_master, "import_table", side_effect=fake_import):
            gw_master.search_lot("H5422S0")
        accdb = [p for p in seen if p.suffix == ".accdb"]
        self.assertTrue(accdb, seen)
        from nippou import source_db

        for path in accdb:
            self.assertNotEqual(path, share)
            self.assertIn(source_db.copy_dir(), path.parents)
        self.assertEqual(accdb[0].read_bytes(), share.read_bytes())


if __name__ == "__main__":
    unittest.main()
