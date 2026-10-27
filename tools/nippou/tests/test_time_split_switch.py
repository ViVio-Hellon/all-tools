"""「時間を割り振る」「LOTで時間を割り振る」をそれぞれ入り切りする・配布設定で配る

【利用者から】

    時間を割り振る / LOTで時間を割り振る: 共にパスワードでオンオフ出来るようにし、配布設定できるようにしてください

    ・設定・管理者(管理者パスワードの鍵)で2つを別々に「使う / 使わない」にする
      (`config.KEY_TIME_SPLIT_SHIFT`・`KEY_TIME_SPLIT_LOT`)
    ・使わないボタンは日報入力に出さない。画面を通さずに呼ばれてもサーバが断る(`time_split_off`)
    ・書いていない端末は前の版の1つの入り切り(`KEY_TIME_SPLIT`。既定はオフ)に従う ── 版を上げただけで
      動きが変わらない
    ・配布設定の項目「「時間を割り振る」を使う」「「LOTで時間を割り振る」を使う」。配った先は書き出した元と
      同じ入り切り。配布設定から読んだ値は覚えておき、配り直せば新しい値に、その端末で直した値は残す。
      大設定のまとめたファイル(`配布設定\\日報複合ツール.json` の tools.nippou)からも読む
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402
from tests.test_distribution import DistributionTestCase  # noqa: E402

SHIFT = "time_split_shift_enabled"
LOT = "time_split_lot_enabled"


def row(lot: str) -> dict:
    return {"LOT": lot, "MAI": "10", "TUT": "1"}


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class SwitchWebTests(WebTestCase):
    def admin(self) -> None:
        self.assertEqual(self.post("/api/settings/admin", {"enable": True, "password": "nisk"}).status_code, 200)

    def page(self) -> str:
        return self.get("/").get_data(as_text=True)

    def screen(self, **extra) -> dict:
        return {"rows": {"1": row("A"), "2": row("B")}, "header": {"worker": "山田"}, **extra}

    def test_鍵は前の版の入り切りと別(self) -> None:
        from nippou import config
        self.assertEqual((config.KEY_TIME_SPLIT_SHIFT, config.KEY_TIME_SPLIT_LOT), (SHIFT, LOT))
        self.assertNotIn(config.KEY_TIME_SPLIT, (SHIFT, LOT))

    def test_入り切りには管理者パスワードが要る(self) -> None:
        res = self.post("/api/settings/time-split", {"enabled": True, "which": "lot"})
        self.assertEqual(res.status_code, 403)
        page = self.get("/settings").get_data(as_text=True)
        self.assertIn('id="time-split-choices-shift"', page)
        self.assertIn('id="time-split-choices-lot"', page)
        self.assertIn("上の鍵を開けると変えられます", page)
        self.assertNotIn('id="time-split-lot"', self.page())

    def test_それぞれ入り切りでき_切ったほうはボタンを出さずサーバも断る(self) -> None:
        self.admin()
        self.post("/api/settings/time-split", {"enabled": True, "which": "shift"})
        body = self.post("/api/settings/time-split", {"enabled": False, "which": "lot"}).get_json()
        self.assertEqual({b["which"]: b["enabled"] for b in body["buttons"]}, {"shift": True, "lot": False})
        self.assertIn("「LOTで時間を割り振る」を出さないようにしました", body["message"])
        page = self.page()
        self.assertIn('id="time-split"', page)
        self.assertNotIn('id="time-split-lot"', page)
        # 画面を通さずに呼ばれても、切ったほうは動かない
        res = self.post("/api/entry/distribute-lot", self.screen(dry_run=True, focus_row=2))
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.get_json()["error"]["code"], "time_split_off")
        self.assertIn("「LOTで時間を割り振る」はこの端末では使わない設定です", res.get_json()["error"]["message"])
        self.assertEqual(self.post("/api/entry/distribute-times", self.screen(dry_run=True)).status_code, 200)

        # 逆にする
        self.post("/api/settings/time-split", {"enabled": False, "which": "shift"})
        self.post("/api/settings/time-split", {"enabled": True, "which": "lot"})
        page = self.page()
        self.assertNotIn('id="time-split"', page)
        self.assertIn('id="time-split-lot"', page)
        res = self.post("/api/entry/distribute-times", self.screen(dry_run=True))
        self.assertEqual(res.status_code, 403)
        self.assertIn("「時間を割り振る」はこの端末では使わない設定です", res.get_json()["error"]["message"])

    def test_書いていない端末は前の版の入り切りに従う(self) -> None:
        from nippou import config, user_settings
        from app import current
        self.assertEqual((current.time_split_button("shift"), current.time_split_button("lot")), (False, False),
                         "既定はいままでどおりオフ")
        user_settings.save(config.KEY_TIME_SPLIT, True)            # 前の版でオンにしていた端末
        self.assertEqual((current.time_split_button("shift"), current.time_split_button("lot")), (True, True))
        page = self.page()
        self.assertIn('id="time-split"', page)
        self.assertIn('id="time-split-lot"', page)
        # 新しい入り切りを書けば、そちらが勝つ
        user_settings.save(LOT, False)
        self.assertEqual((current.time_split_button("shift"), current.time_split_button("lot")), (True, False))

    def test_whichが無ければ2つとも(self) -> None:
        self.admin()
        body = self.post("/api/settings/time-split", {"enabled": True}).get_json()
        self.assertTrue(body["enabled"])
        self.assertEqual([b["enabled"] for b in body["buttons"]], [True, True])
        self.assertEqual(self.post("/api/settings/time-split", {"enabled": True, "which": "x"}).status_code, 400)

    def test_配布設定の書き出す項目に出る(self) -> None:
        page = self.get("/settings").get_data(as_text=True)
        self.assertIn("「時間を割り振る」を使う", page)
        self.assertIn("「LOTで時間を割り振る」を使う", page)


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class SwitchDistributionTests(DistributionTestCase):
    """配った先で、書き出した元と同じ入り切りになる。"""

    def items(self):
        from nippou import distribution
        return [item.key for item in distribution.ITEMS if item.default]

    def buttons(self) -> tuple[bool, bool]:
        from app import current
        return current.time_split_button("shift"), current.time_split_button("lot")

    def test_既定で書き出す項目に入り_名前も付く(self) -> None:
        from nippou import distribution
        labels = {i.key: i.label for i in distribution.ITEMS if i.default}
        self.assertEqual(labels[SHIFT], "「時間を割り振る」を使う")
        self.assertEqual(labels[LOT], "「LOTで時間を割り振る」を使う")

    def test_配った先も同じ入り切り_配り直せば新しく_直した値は残す(self) -> None:
        from nippou import distribution, user_settings
        self.source.use()
        user_settings.save_many({SHIFT: True, LOT: False})
        result = distribution.export("nisk", self.items(), [])
        self.assertTrue(result.ok, result.message)
        data = self.written()
        self.assertIs(data["settings"][SHIFT], True)
        self.assertIs(data["settings"][LOT], False)
        self.assertEqual(data["labels"][SHIFT], "「時間を割り振る」を使う")
        self.assertEqual(data["labels"][LOT], "「LOTで時間を割り振る」を使う")
        shown = {c["label"]: c["value"] for c in distribution.contents(distribution.read())}
        self.assertEqual((shown["「時間を割り振る」を使う"], shown["「LOTで時間を割り振る」を使う"]), ("する", "しない"))

        # 配った先(何も決めていない端末)。起動前でも配布設定の値で動き、起動で読み込む
        self.dest.use()
        self.assertEqual(self.buttons(), (True, False))
        applied = distribution.apply_on_start().applied
        self.assertIn("「時間を割り振る」を使う", applied)
        self.assertIn("「LOTで時間を割り振る」を使う", applied)
        self.assertEqual({k: user_settings.load_all()[k] for k in (SHIFT, LOT)}, {SHIFT: True, LOT: False})
        self.assertEqual(self.buttons(), (True, False))

        # 配り直す(LOT も使う)→ 配布設定から読んだ値のままの端末は新しい値に
        self.source.use()
        user_settings.save(LOT, True)
        self.assertTrue(distribution.export("nisk", self.items(), []).ok)
        self.dest.use()
        distribution.apply_on_start()
        self.assertEqual(self.buttons(), (True, True))

        # その端末で直した値は、配り直しても残す
        user_settings.save(SHIFT, False)
        self.source.use()
        user_settings.save(LOT, False)
        self.assertTrue(distribution.export("nisk", self.items(), []).ok)
        self.dest.use()
        result = distribution.apply_on_start()
        self.assertIn("「時間を割り振る」を使う", result.kept)
        self.assertEqual(self.buttons(), (False, False))

    def test_前の版の入り切りだけの端末も_その値を書き出し配る(self) -> None:
        from nippou import config, distribution, user_settings
        self.source.use()
        user_settings.save(config.KEY_TIME_SPLIT, True)            # 前の版でオンにした端末
        self.assertTrue(distribution.export("nisk", self.items(), []).ok)
        data = self.written()
        self.assertEqual((data["settings"][SHIFT], data["settings"][LOT]), (True, True))
        self.dest.use()
        distribution.apply_on_start()
        self.assertEqual(self.buttons(), (True, True))

    def test_前の版でオンにしていた端末は_この端末の値として残す(self) -> None:
        from nippou import config, distribution, user_settings
        self.source.use()
        user_settings.save_many({SHIFT: False, LOT: False})
        self.assertTrue(distribution.export("nisk", self.items(), []).ok)
        self.dest.use()
        user_settings.save(config.KEY_TIME_SPLIT, True)
        result = distribution.apply_on_start()
        self.assertIn("「時間を割り振る」を使う", result.kept)
        self.assertEqual(self.buttons(), (True, True), "既存データがある場合はそちらが優先")
        distribution.reapply("nisk")                              # 揃えるときは読み込み直す
        self.assertEqual(self.buttons(), (False, False))

    def test_何も決めていない端末からは書き出さない(self) -> None:
        from nippou import distribution, user_settings
        self.source.use()
        user_settings.save(SHIFT, True)
        result = distribution.export("nisk", self.items(), [])
        self.assertTrue(result.ok)
        self.assertNotIn(LOT, self.written()["settings"], "既定のまま(配った先も既定で動く)")
        self.assertIn("「LOTで時間を割り振る」を使う", result.message)

    def test_大設定のまとめたファイルからも読む(self) -> None:
        from nippou import distribution, user_settings
        saved = os.environ.get("ALLTOOLS_DISTRIBUTION_DIR")
        self.addCleanup(lambda: os.environ.__setitem__("ALLTOOLS_DISTRIBUTION_DIR", saved)
                        if saved is not None else os.environ.pop("ALLTOOLS_DISTRIBUTION_DIR", None))
        whole = self.root / "一式" / "配布設定"
        os.environ["ALLTOOLS_DISTRIBUTION_DIR"] = str(whole)
        # 配る元で書き出したもの(ツールのフォルダの 設定.json)を、大設定がまとめたファイルへ入れたのと同じ形
        self.source.use()
        user_settings.save_many({SHIFT: False, LOT: True})
        self.assertTrue(distribution.export("nisk", self.items(), []).ok)
        own = self.written()
        distribution.settings_path().unlink()                     # 配った先にはまとめたファイルだけ
        whole.mkdir(parents=True)
        (whole / distribution.INTEGRATED_FILE).write_text(json.dumps(
            {"format": 1, "created_at": own["created_at"], "settings": {}, "tools": {"nippou": own}},
            ensure_ascii=False), encoding="utf-8")
        self.dest.use()
        self.assertTrue(distribution.read().integrated)
        applied = distribution.apply_on_start().applied
        self.assertIn("「LOTで時間を割り振る」を使う", applied)
        self.assertEqual(self.buttons(), (False, True))


if __name__ == "__main__":
    unittest.main()
