"""この端末だけに残すファイル / 複数のPCで共有するファイル (v3.95.0)

    ローカルに保存してそのPCで引き継いで使用するものは設定部にそういう
    ファイルがあるということを明記しておいてください
    (複数PCで共有するものと該当PCで引き継ぐものは違いますよね)

設定・管理者 →「この端末と配布」の面に2つを並べ、参照設定の面からもそこへ
案内する(`presenters/storage.py`)。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class StoragePlacesTests(WebTestCase):
    def view(self) -> dict:
        from nippou.presenters import storage
        return storage.storage_view()

    def by_label(self, items: list[dict]) -> dict[str, dict]:
        return {i["label"]: i for i in items}

    def test_local_files_are_listed_with_their_real_places(self) -> None:
        from nippou import config
        from nippou.config import SETTINGS
        local = self.by_label(self.view()["local"])
        self.assertEqual(local["手元の日報"]["path"], str(SETTINGS.sqlite_path))
        self.assertTrue(local["手元の日報"]["important"])      # 消すと戻らない
        self.assertIn("共有へ保存", local["手元の日報"]["note"])
        self.assertIn("LocalBackup", local["手元の日報"]["note"])   # v4.12.0 控えにも書く
        self.assertEqual(local["この端末の設定"]["path"], str(config.USER_CONFIG_PATH))
        for label in ("参照マスタの写し", "VC計算マスタの控え", "ログ", "多重起動の印",
                      "集計CSV・印刷用HTML(既定の出力先)", "配られた音声"):
            self.assertIn(label, local)

    def test_local_files_stay_inside_this_pc(self) -> None:
        """この端末だけのものは、**この試験の一時フォルダ(=ローカル領域)の中**にある。"""
        for item in self.view()["local"]:
            with self.subTest(label=item["label"]):
                Path(item["path"]).resolve().relative_to(self.tmp.resolve())

    def test_shared_files_follow_the_reference_settings(self) -> None:
        from nippou.config import SETTINGS
        shared = self.by_label(self.view()["shared"])
        self.assertEqual(shared["日報データ(共有)"]["path"], str(SETTINGS.access_db_path))
        self.assertEqual(shared["VC計算マスタ"]["path"], str(SETTINGS.vc_master_path))
        self.assertEqual(shared["停止内訳"]["path"], str(SETTINGS.stop_reason_csv_path))
        # 参照用マスタは置き場所ごとに3行(v4.16.0)。空なら参照用マスタと同じフォルダ
        self.assertNotIn("参照用マスタ", shared)
        for label, setting, path in (
                ("仕掛ロット・引当・受注", "仕掛ロット・引当・受注の置き場所", SETTINGS.wip_master_dir),
                ("梱包資材マスタ", "梱包資材マスタの置き場所", SETTINGS.material_master_dir),
                ("伝送用ファイル", "伝送用ファイルの置き場所", SETTINGS.transmission_master_dir)):
            with self.subTest(label=label):
                self.assertEqual(shared[label]["note"], setting)
                self.assertEqual(shared[label]["path"], str(path))
                self.assertEqual(path, SETTINGS.gw_reference_dir)
                self.assertFalse(shared[label]["local_now"])

    def test_output_left_on_this_pc_is_flagged(self) -> None:
        """集計CSVの出力先を決めていなければ、共有のつもりが**この端末の中**。"""
        from nippou import config, user_settings
        view = self.view()
        self.assertTrue(self.by_label(view["shared"])["集計CSV・印刷用HTML"]["local_now"])
        shared_dir = self.tmp / "shared"
        shared_dir.mkdir()
        user_settings.save_many({config.KEY_REPORT_OUT_DIR: str(shared_dir)})
        view = self.view()
        self.assertFalse(self.by_label(view["shared"])["集計CSV・印刷用HTML"]["local_now"])
        local = self.by_label(view["local"])["集計CSV・印刷用HTML(既定の出力先)"]
        self.assertIn("いまはここへは出していません", local["note"])

    def test_looking_does_not_create_anything(self) -> None:
        before = sorted(p.relative_to(self.tmp) for p in self.tmp.rglob("*"))
        self.view()
        after = sorted(p.relative_to(self.tmp) for p in self.tmp.rglob("*"))
        self.assertEqual(before, after)

    def test_settings_screen_says_it(self) -> None:
        html = self.get("/settings").get_data(as_text=True)
        self.assertIn('id="storage"', html)
        self.assertIn("この端末だけに残すもの", html)
        self.assertIn("複数のPCで共有するもの", html)
        self.assertIn("nippou_local.sqlite3", html)
        self.assertIn("消すと戻りません", html)
        # 参照設定の面からも案内する
        paths = html[html.index('data-panel="paths"'):]
        self.assertIn('id="paths-shared-note"', paths)
        self.assertIn('data-goto-tab="terminal"', paths)

    def test_tab_note_mentions_it(self) -> None:
        from nippou.presenters import settings as view
        terminal = next(t for t in view.TABS if t.key == "terminal")
        self.assertIn("この端末に残すファイル", terminal.note)


if __name__ == "__main__":
    unittest.main()
