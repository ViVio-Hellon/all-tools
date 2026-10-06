"""アプリは tkinter に触らない

【なぜ要るのか】
**表示環境の無い端末でも動く**ことの保証。開発機に tkinter が入っていると、
うっかり掴んでも手元では動いてしまい、置いた先で初めて落ちる。
ここで固定しておけば、``import tkinter`` を足した時点で落ちる。

もとは tkinter 版を消せる(消しても Web版が動く)ことを確かめるための
試験だったが、消したあとも「二度と戻さない」ための網として残している。

【やり方】
子プロセスで ``tkinter`` の import を禁止したうえで、アプリが使うものを
片端から読み込む。1つでも掴んでいれば ImportError で落ちる。
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

from . import _isolation

_isolation.ensure_isolated()

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# tkinter を掴んでいたら落ちる形で読み込むモジュール。
# **Web版が通る道を全部**挙げる(routes → presenters → 業務)
WEB_MODULES = (
    "app",
    "app.shell",
    "app.routes.health", "app.routes.calendar", "app.routes.settings",
    "calendar_app.presenters.calendar", "calendar_app.presenters.settings",
    "calendar_app.presenters.master",
    "calendar_app.repository", "calendar_app.printing", "calendar_app.holiday",
    "calendar_app.sync_service", "calendar_app.idle_exit",
    "calendar_app.sync.autosync", "calendar_app.sync.business_rules",
    "calendar_app.sync.deletes",
    "calendar_app.importer", "calendar_app.sources", "calendar_app.master_admin",
    "calendar_app.admin_password", "calendar_app.terminals",
    "calendar_app.dbkit.source_db", "calendar_app.dbkit.outbox_sync",
)

# 起動基盤。ここが tkinter を掴むと、表示環境の無い端末で起動できない
LAUNCH_MODULES = ("launch_guard", "process_manager", "server", "start_app",
                  "boot_server", "calendar_app.boot_screen",
                  "calendar_app.app_config")

_SCRIPT = """
import sys

class _Blocked:
    \"\"\"tkinter を import しようとしたら、その場で落とす。\"\"\"

    def find_module(self, name, path=None):
        return self if name == "tkinter" or name.startswith("tkinter.") else None

    def find_spec(self, name, path=None, target=None):
        if name == "tkinter" or name.startswith("tkinter."):
            raise ImportError(name + " は Web版から使ってはいけません")
        return None

sys.meta_path.insert(0, _Blocked())

for name in {modules!r}:
    __import__(name)
print("OK")
"""


def _import_without_tkinter(modules: tuple[str, ...]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _SCRIPT.format(modules=modules)],
        cwd=str(_ROOT), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=120)


class WebIsIndependentTests(unittest.TestCase):
    def test_Web版はtkinterを掴まない(self) -> None:
        """画面・API・業務層のどこも tkinter を掴まないこと。"""
        result = _import_without_tkinter(WEB_MODULES)
        self.assertEqual(result.returncode, 0,
                         f"Web版が tkinter を掴んでいます:\n{result.stderr}")

    def test_起動基盤もtkinterを掴まない(self) -> None:
        """表示環境の無い端末でも起動できることの保証。"""
        result = _import_without_tkinter(LAUNCH_MODULES)
        self.assertEqual(result.returncode, 0,
                         f"起動基盤が tkinter を掴んでいます:\n{result.stderr}")

    def test_禁止の仕掛けそのものが効いている(self) -> None:
        """止められていなければ、上の2つは何も試していないことになる。"""
        result = _import_without_tkinter(("tkinter",))
        self.assertNotEqual(result.returncode, 0, "tkinter を止められていません")


class NoTkinterInSourceTests(unittest.TestCase):
    """ソースにも残っていないこと。

    import しない形(文字列・コメント)で残っていても害は無いが、
    **消したはずのものが残っている**と次に読む人が迷う。
    """

    def test_tkinterを参照するファイルが無い(self) -> None:
        offenders = []
        for path in _ROOT.rglob("*.py"):
            if any(part in {".git", "__pycache__", "tests"} for part in path.parts):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if "import tkinter" in text or "from tkinter" in text:
                offenders.append(str(path.relative_to(_ROOT)))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
