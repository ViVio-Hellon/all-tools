"""仕掛のファイルは **sqlite3 だけを読む**(Access の .accdb は読まない)

    SIKALOT は Access を読んでるの？全部 sqlite を読め

仕掛ロット・引当・受注(SIKALOT / SIKAHIKI / SIKAODR)とコイル割り数(LS4LOT)は、設定の名前が
`.accdb` でも**同じ名前の `.sqlite3`** を読みます。sqlite3 が無い・読めないときは
「SIKALOT.sqlite3 がありません/を読めません」と言い、Access の道(cscript + ACE・ODBC)へは行きません。
第1パス → 第2パスの順は今までどおり。

前は:
    - 第2パス・コイル割り数の名前を `.accdb` にすると Access の道で読んでいた
    - 同じ名前の `.sqlite3` を確かめる瞬間に共有へ届かないと(`_pick_source` が OSError)、設定の名前
      (`.accdb`)のまま返して Access の道へ入っていた
    - 第1パスに SIKALOT.sqlite3 が無い・届かないのを「SIKALOT に ロット … がありません」(=ロットが無い)
      と言っていた(読めていないのに「無い」)
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import WebTestCase  # noqa: E402
from tests.test_web_entry_lot import HIKI_COLUMNS, LOT_COLUMNS, ODR_COLUMNS  # noqa: E402
from tests.test_wip_second_dir import HIKI, ODR, _lot  # noqa: E402
from tests import test_wip_second_dir as _second  # noqa: E402


class WipSqliteOnlyTests(WebTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.first = self.tmp / "仕掛1"
        self.second = self.tmp / "仕掛2"
        self.first.mkdir()
        self.second.mkdir()

    # 仕掛の形で書く・3つ置く・日報入力で引く(`test_wip_second_dir` と同じ小道具)
    write = _second.SecondWipDirTests.write
    all_three = _second.SecondWipDirTests.all_three
    lookup = _second.SecondWipDirTests.lookup

    def use(self, **values: str) -> None:
        body = {"wip_master_dir": str(self.first), "password": "nisk", **values}
        res = self.post("/api/settings/paths", body)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))

    def no_access(self):
        """Access の道(VBScript・ODBC)へ入ったら記録する。**入ってはいけない。**"""
        from nippou.access_bridge import importer

        called: list[str] = []

        def refuse(*args, **kwargs):
            called.append("vbscript")
            raise AssertionError("仕掛を Access の道で読もうとしました")

        mocked = patch.object(importer, "run_with_retry", side_effect=refuse)
        mocked.start()
        self.addCleanup(mocked.stop)
        return called

    @staticmethod
    def fake_accdb(path: Path) -> None:
        path.write_bytes(b"\x00\x01\x00\x00Standard ACE DB" + b"\0" * 64)

    # -- 名前が .accdb でも .sqlite3 を読む -----------------------------
    def test_第2パスの名前がaccdbでも同じ名前のsqlite3を読む(self) -> None:
        called = self.no_access()
        self.all_three(self.second)
        self.write(self.second, "SIKALOT2.sqlite3", LOT_COLUMNS, [_lot("H5422S0")])
        self.fake_accdb(self.second / "SIKALOT2.accdb")
        self.use(wip_master_dir_2=str(self.second), wip_master_2_lot_file="SIKALOT2.accdb")
        from nippou.config import SETTINGS

        self.assertEqual(SETTINGS.gw_lot_master_paths[-1], self.second / "SIKALOT2.sqlite3")
        body = self.lookup()
        self.assertEqual(body["lot"]["state"], "filled", body)
        self.assertEqual(body["rows"]["1"]["UNI"], "250.04314")
        self.assertEqual(called, [])

    def test_accdbしか無ければsqlite3がありませんと言いAccessへ行かない(self) -> None:
        called = self.no_access()
        self.fake_accdb(self.first / "SIKALOT.accdb")
        self.write(self.first, "SIKAHIKI.sqlite3", HIKI_COLUMNS, [HIKI])
        self.write(self.first, "SIKAODR.sqlite3", ODR_COLUMNS, [ODR])
        self.fake_accdb(self.second / "SIKALOT2.accdb")
        self.use(wip_master_dir_2=str(self.second), wip_master_2_lot_file="SIKALOT2.accdb")
        body = self.lookup()
        self.assertEqual(body["lot"]["state"], "unreadable", body)
        self.assertIn("SIKALOT.sqlite3", body["lot"]["message"])
        self.assertIn("SIKALOT2.sqlite3", body["lot"]["message"])
        self.assertNotIn("ロット H5422S0 がありません", body["lot"]["message"])
        self.assertEqual(called, [])

    def test_コイル割り数の名前がaccdbでもsqlite3を読む(self) -> None:
        self.use(coil_master_file="LS4LOT.accdb")
        from nippou.config import SETTINGS

        for path in SETTINGS.gw_coil_master_paths:
            self.assertIn(path.suffix, (".sqlite3", ".db"), SETTINGS.gw_coil_master_paths)
        self.assertEqual(SETTINGS.gw_coil_master_paths[0], self.first / "LS4LOT.sqlite3")

    def test_sqlite3を確かめる瞬間に届かなくてもaccdbの名前を返さない(self) -> None:
        self.write(self.second, "SIKALOT2.sqlite3", LOT_COLUMNS, [_lot("H5422S0")])
        self.use(wip_master_dir_2=str(self.second), wip_master_2_lot_file="SIKALOT2.accdb")
        from nippou.config import SETTINGS

        real_stat = os.stat
        share = str(self.second)

        def denied(path, *args, **kwargs):
            if str(os.fspath(path)).startswith(share):
                raise PermissionError(13, "共有に届きません(試験)", str(path))
            return real_stat(path, *args, **kwargs)

        with patch("os.stat", side_effect=denied):
            paths = SETTINGS.gw_lot_master_paths
        self.assertEqual(paths[-1], self.second / "SIKALOT2.sqlite3")

    # -- 無い・届かないを「ロットが無い」と言わない -----------------------
    def test_第1パスにSIKALOTのsqlite3が無ければありませんと言う(self) -> None:
        self.write(self.first, "SIKAHIKI.sqlite3", HIKI_COLUMNS, [HIKI])
        self.write(self.first, "SIKAODR.sqlite3", ODR_COLUMNS, [ODR])
        self.use()
        body = self.lookup()
        self.assertEqual(body["lot"]["state"], "unreadable", body)
        self.assertIn("SIKALOT.sqlite3 がありません", body["lot"]["message"])

    def test_第1パスに一度届かなくても次は引ける(self) -> None:
        self.all_three(self.first)
        self.use()
        real_stat = os.stat
        share = str(self.first)

        def unreachable(path, *args, **kwargs):
            if str(os.fspath(path)).startswith(share):
                raise FileNotFoundError(2, "ネットワーク パスが見つかりません(試験)", str(path))
            return real_stat(path, *args, **kwargs)

        with patch("os.stat", side_effect=unreachable):
            body = self.lookup()
        self.assertEqual(body["lot"]["state"], "unreadable", body)
        self.assertIn("SIKALOT.sqlite3", body["lot"]["message"])
        body = self.lookup()                          # 届くようになった。何も覚えていない
        self.assertEqual(body["lot"]["state"], "filled", body)
        self.assertEqual(body["rows"]["1"]["UNI"], "250.04314")

    def test_GW計算でもsqlite3が無ければ落ちずにそう言う(self) -> None:
        self.use()
        res = self.post("/api/gw/lot", {"lot_no": "H5422S0"})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        body = res.get_json()
        self.assertFalse(body["found"])
        self.assertIn("SIKALOT.sqlite3", body["message"])

    # -- 設定画面は実際に読むファイル名を出す -----------------------------
    def test_設定画面の第1パス第2パスはsqlite3の名前を出す(self) -> None:
        self.use(wip_master_dir_2=str(self.second), wip_master_2_lot_file="SIKALOT2.accdb",
                 coil_master_file="LS4LOT.accdb")
        from nippou.presenters import settings as presenter

        files = {f.key: f for f in presenter.file_views()}
        for key in ("lot", "hiki", "order", "coil"):
            self.assertTrue(files[key].path.endswith(".sqlite3"), files[key].path)
            self.assertEqual(files[key].kind, "sqlite3")
        paths = {p.key: p for p in presenter.path_views()}
        self.assertEqual(paths["wip_master_2_lot_file"].resolved,
                         str(self.second / "SIKALOT2.sqlite3"))
        self.assertEqual(paths["coil_master_file"].resolved, str(self.first / "LS4LOT.sqlite3"))


if __name__ == "__main__":
    unittest.main()
