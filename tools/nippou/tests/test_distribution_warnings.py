"""配る前の札(「端末で違う」など)── ファイル名だけの欄を、利用者フォルダの下と取り違えない。

    「コイル割り数のファイル名(第1パス)」がこの端末の利用者フォルダの下(C:\\Users\\…\\tools\\nippou\\
    実績_LS4.sqlite3)を指しています ── そんな設定していないのにローカル保存していてそれを配ると
    言っているがどういう意味なのか

ファイル名の欄(「実績_LS4.sqlite3」)は、上の置き場所(\\\\サーバ\\…)と組み合わせて使う。名前だけを
日報のフォルダの中とみなして解いていたので、ツールを利用者フォルダの下(デスクトップ)に置いた端末では
「利用者フォルダの下を指しています」と出ていた。
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from nippou import app_config, config
from nippou.presenters import settings as presenter


#: 共有の道(Windows は \\サーバ\…。Linux の CI では利用者フォルダの外の絶対の道でまねる)
SHARE = r"\\nlmsrvngy03\Read" if os.name == "nt" else "/srv/Read"


class NameOnlyFieldsTests(unittest.TestCase):
    def warnings(self, values: dict) -> list[str]:
        base = {config.KEY_ACCESS_DIR: SHARE + "/日報", config.KEY_REFERENCE_DIR: SHARE + "/参照"}
        # ツールのフォルダが利用者フォルダの下にある端末(デスクトップに置いた)
        with mock.patch.object(Path, "home", return_value=Path(app_config.APP_ROOT).parent):
            return [w["text"] for w in presenter.distribution_warnings({**base, **values})
                    if w["level"] == presenter.LEVEL_WARN]

    def test_ファイル名だけの欄は利用者フォルダの下と言わない(self) -> None:
        got = self.warnings({
            config.KEY_COIL_DIR: SHARE + "/【New】実績",
            config.KEY_COIL_FILE: "実績_LS4.sqlite3",
            config.KEY_WIP2_LOT_FILE: "SIKALOT_圧縮.sqlite3",
            config.KEY_WIP2_HIKI_FILE: "SIKAHIKI.sqlite3",
            config.KEY_WIP2_ORDER_FILE: "SIKAODR.sqlite3",
            config.KEY_WIP2_COIL_FILE: "LS4LOT.sqlite3",
            config.KEY_ACCESS_DB_FILE: "日報データ.sqlite3",
        })
        self.assertEqual(got, [])

    def test_置き場所の欄が利用者フォルダの下なら今までどおり言う(self) -> None:
        local = str(Path(app_config.APP_ROOT) / "仕掛")
        got = self.warnings({config.KEY_WIP_DIR: local})
        self.assertEqual(len(got), 1, got)
        self.assertIn("利用者フォルダの下", got[0])

    def test_ファイル名の欄でもフォルダまで書いてあれば確かめる(self) -> None:
        local = str(Path(app_config.APP_ROOT) / "実績_LS4.sqlite3")
        got = self.warnings({config.KEY_COIL_FILE: local})
        self.assertEqual(len(got), 1, got)


if __name__ == "__main__":
    unittest.main()
