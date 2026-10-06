"""READMEの「ファイルの地図」が、本当のファイル名と合っているか

【なぜ機械で見るのか】
名前と置き場所は、聞かれるたびに人がREADMEを読んで答えるものです。
**ずれても誰も気づきません** ── 動きはコードで決まっていて、READMEは
読まれるだけなので、間違ったまま何か月も残ります。

実際「自動保存は 日報データ.sqlite3 に書くのか」と聞かれました。書きません
(手元の `nippou_local.sqlite3` です)が、それがどこにも書いてありません
でした。**書いたからには、ずれたら落ちるようにしておきます。**

ここが見るのは名前だけです。振る舞い(どれがいつ書くか)は
`test_web_entry_screen` や `test_shift_check` が見ています。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou import app_config                                  # noqa: E402
from nippou.config import SETTINGS                             # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SECTION = "## ファイルの地図"


class FileMapTests(unittest.TestCase):
    """READMEに書いた名前が、`config` の値と一致すること。"""

    @classmethod
    def setUpClass(cls) -> None:
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        cls.readme = text
        start = text.index(SECTION)
        # 次の見出しまでを「その節」とする
        end = text.index("\n## ", start + len(SECTION))
        cls.section = text[start:end]

    def test_節がある(self) -> None:
        self.assertIn(SECTION, self.readme)

    def test_手元と共有の名前(self) -> None:
        """**ここが一番よく聞かれるところ。** 2つを取り違えない。"""
        self.assertIn(SETTINGS.sqlite_filename, self.section)
        self.assertIn(SETTINGS.access_db_filename, self.section)

    def test_どちらが手元でどちらが共有かも書く(self) -> None:
        """名前が並んでいるだけでは、取り違えたままになります。"""
        local = self.section.index(SETTINGS.sqlite_filename)
        shared = self.section.index(SETTINGS.access_db_filename)
        head = self.section[:max(local, shared) + 200]
        self.assertIn("手元", head)
        self.assertIn("共有", head)

    def test_参照マスタの名前(self) -> None:
        for name in (SETTINGS.gw_lot_master_filename,
                     SETTINGS.gw_hiki_master_filename,
                     SETTINGS.gw_order_master_filename,
                     SETTINGS.gw_coil_master_filename,
                     SETTINGS.gw_material_master_filename,
                     SETTINGS.transmission_master_filename,
                     SETTINGS.line_target_filename,
                     SETTINGS.stop_reason_csv_filename,
                     SETTINGS.vc_master_filename):
            with self.subTest(name=name):
                self.assertIn(name, self.section)

    def test_自動保存の間引きの秒数(self) -> None:
        """**数字は特にずれます。** 変えたらここで落ちる。"""
        self.assertIn(f"{SETTINGS.autosave_interval_sec}秒", self.section)

    def test_この端末だけのものの置き場所(self) -> None:
        import launch_guard

        self.assertIn(launch_guard.LOCK_NAME, self.section)
        # ローカル領域の名前(実在するものだけを挙げる)
        for name in ("runtime", "cache", "logs", "work"):
            with self.subTest(name=name):
                self.assertIn(name, app_config.LOCAL_SUBDIRS)
                self.assertIn(f"{name}/", self.section)

    def test_出力先のフォルダの形(self) -> None:
        from nippou.reporting import csv_export

        self.assertIn(csv_export.KIND_AGGREGATE, self.section)
        self.assertIn(csv_export.KIND_PRINT, self.section)

    def test_自動保存が書く先を言い切っている(self) -> None:
        """「自動保存は共有へ書かない」が読み取れること。"""
        self.assertIn("「共有へ保存」だけ", self.section)
        self.assertIn("集計CSVを書きません", self.section)


if __name__ == "__main__":
    unittest.main()
