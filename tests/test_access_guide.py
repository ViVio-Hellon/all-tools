"""大設定の「アクセス権限の早見表」(`portal/access_guide.py`)が、各ツールの本当の決まりと食い違わない

早見表は説明だけで、判断は各ツールの中にある。ツールの決まり(読む権限・ライン名)を変えたのに
早見表を直し忘れると、大設定が嘘を言う ── ここで各ツールの値と突き合わせる。

ツールはどれも別の Python パッケージなので、1 つずつ別のプロセスで値を取り出す。
"""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from portal import access_guide

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"


def _run(tool: str, code: str):
    done = subprocess.run([sys.executable, "-c", code], cwd=str(TOOLS / tool),
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    if done.returncode != 0:
        raise AssertionError(f"{tool}: {done.stderr[-800:]}")
    return json.loads(done.stdout.strip().splitlines()[-1])


class LineNamesTests(unittest.TestCase):
    def test_日報のライン名(self) -> None:
        names = _run("nippou", "import json; from nippou import constants; print(json.dumps(list(constants.LINE_NAMES)))")
        self.assertEqual(list(access_guide.LINE_NAMES["nippou"]), names)

    def test_看板のライン名(self) -> None:
        names = _run("kanban", "import json; from kanban import line_names as l; "
                               "print(json.dumps([d.official for d in l.DEFINITIONS]))")
        self.assertEqual(list(access_guide.LINE_NAMES["kanban"]), names)

    def test_カレンダーのライン名(self) -> None:
        names = _run("calendar", "import json; from calendar_app import config; "
                                 "print(json.dumps(list(config.ALL_LINE_NAMES)))")
        self.assertEqual(list(access_guide.LINE_NAMES["calendar"]), names)


class PermissionTests(unittest.TestCase):
    """早見表に「効く」と書いた値を、そのツールが本当に読むか。"""

    def cells(self, value: str) -> dict[str, str]:
        return next(cells for v, _note, cells in access_guide.ROWS if v == value)

    def test_日報はAdministratorとmode_fullaccessを読む(self) -> None:
        got = _run("nippou", "import json; from nippou.logic import access_rights as a; "
                             "print(json.dumps([a.ADMINISTRATOR, a.FULL_ACCESS]))")
        self.assertEqual(got, ["Administrator", "mode:fullaccess"])
        self.assertNotEqual(self.cells("Administrator")["nippou"], access_guide.NOT_READ)
        self.assertNotEqual(self.cells("mode:fullaccess")["nippou"], access_guide.NOT_READ)

    def test_看板はmode_fieldとmode_materialとAdministratorを読む(self) -> None:
        got = _run("kanban", "import json; from kanban import access_control as a; "
                             "print(json.dumps([a.PERM_FIELD, a.PERM_MATERIAL, a.ADMINISTRATOR, sorted(a.FALLBACK)]))")
        self.assertEqual(got[:3], ["mode:field", "mode:material", "Administrator"])
        self.assertEqual(got[3], ["mode:field"], "当てはまる行が無ければ現場モードだけ")
        self.assertIn("現場モードだけ", access_guide.NO_ROW["kanban"])
        for value in ("mode:field", "mode:material", "Administrator"):
            self.assertNotEqual(self.cells(value)["kanban"], access_guide.NOT_READ)

    def test_読まないと書いたツールは本当にその値を持っていない(self) -> None:
        sources = {
            "nippou": (TOOLS / "nippou" / "nippou" / "logic" / "access_rights.py"),
            "calendar": (TOOLS / "calendar" / "calendar_app" / "access_control.py"),
        }
        code_of = {tool: path.read_text(encoding="utf-8") for tool, path in sources.items()}
        # 日報は mode:field / mode:material を判断に使わない(説明の文に出るだけ)
        self.assertNotIn('"mode:field"', code_of["nippou"])
        self.assertNotIn('"mode:material"', code_of["nippou"])
        # カレンダーは Administrator も mode:fullaccess も読まない
        self.assertNotIn("Administrator", code_of["calendar"])
        self.assertNotIn("fullaccess", code_of["calendar"])
        # 点検表はアクセス権限を読まない
        found = [p for p in (TOOLS / "inspection").rglob("*.py")
                 if "tests" not in p.parts and "アクセス権限" in p.read_text(encoding="utf-8")]
        self.assertEqual(found, [])

    def test_表の形(self) -> None:
        view = access_guide.view()
        self.assertEqual([t["id"] for t in view["tools"]], ["nippou", "kanban", "calendar", "inspection"])
        for row in view["rows"]:
            self.assertEqual(len(row["cells"]), 4, row["value"])
        self.assertEqual(len(view["no_row"]), 4)


if __name__ == "__main__":
    unittest.main()
