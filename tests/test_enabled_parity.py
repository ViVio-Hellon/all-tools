"""アクセス権限「有効」列の読み方を、ツールのあいだで揃える (v1.5.36)

同じ共有の「アクセス権限」表を、日報・看板・カレンダー(と入口のタブ表示権限)がそれぞれ
読みます。カレンダーだけ「FALSE」「off」「×」「no」を有効と読み、同じ行で答えが割れていました。

基準は python-web-tools の `to_flag`(このリポジトリの外。変えない)── 空は有効、
「0 false no n × x 無効 off」(大文字小文字は見ない)だけ無効。
- 看板・カレンダーは基準とまったく同じ
- 日報・入口は基準より**多く無効にする**だけ(いいえ・✕・全角)。基準が無効にするものを
  有効にすることは無い(権限が広がる向きには割れない)
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_access_guide import _run  # noqa: E402

DISABLED = {"0", "false", "no", "n", "×", "x", "無効", "off"}
VALUES = [None, "", 0, 1, -1, "0", "1", "-1", "TRUE", "FALSE", "False", " false ", "off", "OFF",
          "no", "N", "×", "x", "X", "無効", "有効", "はい"]
EXTRA = ["いいえ", "✕", "０", "ＦＡＬＳＥ"]


def reference(value) -> bool:
    """python-web-tools `to_flag` と同じ読み方(基準)。"""
    return ("" if value is None else str(value).strip()).casefold() not in DISABLED


def answers(tool: str, func: str) -> list[bool]:
    code = (f"import json; {func}; vals = json.loads({json.dumps(json.dumps(VALUES + EXTRA))}); "
            "print(json.dumps([bool(f(v)) for v in vals]))")
    return _run(tool, code)


class EnabledParityTests(unittest.TestCase):
    def test_看板とカレンダーは基準と同じ(self) -> None:
        want = [reference(v) for v in VALUES + EXTRA]
        for tool, func in (("kanban", "from kanban.access_control import parse_enabled as f"),
                           ("calendar", "from calendar_app.access_control import parse_enabled as f")):
            with self.subTest(tool=tool):
                self.assertEqual(answers(tool, func), want)

    def test_日報と入口は基準が無効にするものを有効にしない(self) -> None:
        from portal.tab_rights import _flag
        got = {"nippou": answers("nippou", "from nippou.logic.access_rights import is_valid as f"),
               "portal": [bool(_flag(v)) for v in VALUES + EXTRA]}
        for tool, flags in got.items():
            for value, flag in zip(VALUES + EXTRA, flags):
                with self.subTest(tool=tool, value=value):
                    if value in VALUES:
                        self.assertEqual(flag, reference(value))
                    elif not reference(value):
                        self.assertFalse(flag)


if __name__ == "__main__":
    unittest.main()
