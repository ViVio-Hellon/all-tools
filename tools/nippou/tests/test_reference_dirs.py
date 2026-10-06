"""参照用マスタの置き場所を3つに分ける (v4.16.0)

    仕掛ロット・仕掛引当・仕掛受注 と 梱包資材マスタ・コイル割り数(LS4LOT) と
    伝送用ファイル は別の場所におくので別に設定できるようにしてください

【約束】
    ・3つとも**空なら参照用マスタと同じフォルダ**(これまでどおり)
    ・設定すれば、そのファイルだけがそこを見る(ほかは動かない)
    ・参照設定の面に欄が出る・パスワードで守る・配布設定に入れられる
    ・ファイルの状態(マスタ管理)は、どの設定から来たかを言う
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class ReferenceDirTests(WebTestCase):
    def setUp(self) -> None:
        super().setUp()
        from nippou import config

        self.config = config
        self.ref = self.tmp / "ref"
        for name in ("ref", "wip", "material", "transmission"):
            (self.tmp / name).mkdir(exist_ok=True)

    def paths(self) -> dict[str, Path]:
        from nippou.config import SETTINGS

        return {"lot": SETTINGS.gw_lot_master_path.parent,
                "hiki": SETTINGS.gw_hiki_master_path.parent,
                "order": SETTINGS.gw_order_master_path.parent,
                "material": SETTINGS.gw_material_master_path.parent,
                "coil": SETTINGS.gw_coil_master_path.parent,
                "transmission": SETTINGS.transmission_master_path.parent,
                "stop": SETTINGS.stop_reason_master_path.parent}

    def test_空なら参照用マスタと同じフォルダ(self) -> None:
        self.post("/api/settings/paths", {"gw_reference_dir": str(self.ref), "password": "nisk"})
        self.assertEqual(set(self.paths().values()), {self.ref})

    def test_3つを別々に(self) -> None:
        c = self.config
        res = self.post("/api/settings/paths", {
            "gw_reference_dir": str(self.ref),
            c.KEY_WIP_DIR: str(self.tmp / "wip"),
            c.KEY_MATERIAL_DIR: str(self.tmp / "material"),
            c.KEY_TRANSMISSION_DIR: str(self.tmp / "transmission"),
            "password": "nisk"})
        self.assertEqual(res.status_code, 200, res.get_json())
        got = self.paths()
        self.assertEqual({got["lot"], got["hiki"], got["order"]}, {self.tmp / "wip"})
        self.assertEqual({got["material"], got["coil"]}, {self.tmp / "material"})
        self.assertEqual({got["transmission"], got["stop"]}, {self.tmp / "transmission"})

    def test_1つだけ変えればほかは参照用マスタのまま(self) -> None:
        c = self.config
        self.post("/api/settings/paths", {"gw_reference_dir": str(self.ref),
                                          c.KEY_WIP_DIR: str(self.tmp / "wip"),
                                          "password": "nisk"})
        got = self.paths()
        self.assertEqual(got["lot"], self.tmp / "wip")
        self.assertEqual(got["material"], self.ref)
        self.assertEqual(got["transmission"], self.ref)

    def test_パスワードが要る(self) -> None:
        res = self.post("/api/settings/paths", {self.config.KEY_WIP_DIR: str(self.tmp / "wip")})
        self.assertNotEqual(res.status_code, 200)
        self.assertNotEqual(self.paths()["lot"], self.tmp / "wip")

    def test_参照設定の面と_ファイルの出どころ(self) -> None:
        from nippou.presenters import settings as presenter

        page = self.get("/settings?tab=paths").get_data(as_text=True)
        for label in ("仕掛ロット・引当・受注の置き場所", "梱包資材マスタの置き場所",
                      "伝送用ファイルの置き場所"):
            self.assertIn(label, page)
        sources = {f.key: f.source_key for f in presenter.file_views()}
        c = self.config
        self.assertEqual((sources["lot"], sources["hiki"], sources["order"]),
                         (c.KEY_WIP_DIR,) * 3)
        self.assertEqual((sources["material"], sources["coil"]), (c.KEY_MATERIAL_DIR,) * 2)
        self.assertEqual(sources["transmission"], c.KEY_TRANSMISSION_DIR)

    def test_配布設定に入れられる(self) -> None:
        from nippou import distribution

        keys = {item.key for item in distribution.ITEMS}
        c = self.config
        self.assertLessEqual({c.KEY_WIP_DIR, c.KEY_MATERIAL_DIR, c.KEY_TRANSMISSION_DIR}, keys)


if __name__ == "__main__":
    unittest.main()
