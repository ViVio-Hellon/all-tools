"""アプリは tkinter に触らない

【なぜ要るのか】
**表示環境の無いPCでも動く**ことの保証。開発機に tkinter が入っていると、
うっかり掴んでも手元では動いてしまい、置いた先で初めて落ちる。ここで
固定しておけば、`import tkinter` を足した時点で落ちる。

もとは tkinter 版を消せる(消しても Web版が動く)ことを確かめるための
試験だったが、消したあとも**「二度と戻さない」ための網**として残す。

【やり方】
子プロセスで `tkinter` の import を禁止したうえで、アプリが使うものを
片端から読み込む。1つでも掴んでいれば ImportError で落ちる。
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

try:
    import flask  # noqa: F401

    HAS_WEB = True
except ImportError:                              # pragma: no cover
    HAS_WEB = False

_SKIP = "Flask が入っていないためスキップ (pip install -r requirements.txt)"

# tkinter を掴んでいたら落ちる形で読み込むモジュール。
# **Web版が通る道を全部**挙げる(routes → presenters → logic → db)
WEB_MODULES = (
    "app",
    "app.shell",
    "app.routes.entry", "app.routes.graph", "app.routes.gw",
    "app.routes.health", "app.routes.pending", "app.routes.printing",
    "app.routes.settings", "app.routes.staff",
    "nippou.presenters.entry",
    "nippou.work_context",
    "nippou.logic.aggregation", "nippou.logic.calculations",
    "nippou.logic.exclusions", "nippou.logic.gw_calculation",
    "nippou.logic.navigation", "nippou.logic.shift", "nippou.logic.sound",
    "nippou.logic.staff", "nippou.logic.stop_reason", "nippou.logic.validation",
    "nippou.db.repository", "nippou.db.connection",
    "nippou.access_bridge.importer", "nippou.access_bridge.pusher",
    "nippou.access_bridge.gw_master", "nippou.access_bridge.staff_master",
    "nippou.access_bridge.stop_master", "nippou.access_bridge.odbc_backend",
    "nippou.reporting.csv_export", "nippou.reporting.print_format",
)

# 起動基盤。ここが tkinter を掴むと、表示環境の無いPCで起動できない
LAUNCH_MODULES = ("launch_guard", "process_manager", "server", "start_app",
                  "boot_server", "nippou.boot_screen", "nippou.app_config")

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
        cwd=str(_ROOT), capture_output=True, text=True, timeout=120)


@unittest.skipUnless(HAS_WEB, _SKIP)
class WebIsIndependentTests(unittest.TestCase):
    def test_Web版はtkinterを掴まない(self) -> None:
        """画面・API・ロジック層のどこも tkinter を掴まないこと。"""
        result = _import_without_tkinter(WEB_MODULES)
        self.assertEqual(result.returncode, 0,
                         f"Web版が tkinter を掴んでいます:\n{result.stderr}")

    def test_起動基盤もtkinterを掴まない(self) -> None:
        """表示環境の無いPCでも起動できることの保証。"""
        result = _import_without_tkinter(LAUNCH_MODULES)
        self.assertEqual(result.returncode, 0,
                         f"起動基盤が tkinter を掴んでいます:\n{result.stderr}")

    def test_禁止の仕掛けそのものが効いている(self) -> None:
        """止められていなければ、上の2つは何も試していないことになる。"""
        result = _import_without_tkinter(("tkinter",))
        self.assertNotEqual(result.returncode, 0,
                            "tkinter を止められていません")


class NoTkinterInSourceTests(unittest.TestCase):
    """ソースに `import tkinter` が残っていないこと。

    上の import 試験は「読み込む道」だけを見るので、どこからも呼ばれて
    いないファイルに書き足された場合は素通りする。文字列でも見ておく。
    """

    def test_ソースにtkinterの取り込みが無い(self) -> None:
        offenders = []
        for path in _ROOT.rglob("*.py"):
            parts = set(path.parts)
            if parts & {".git", "__pycache__", "venv", ".venv"}:
                continue
            if path.name == Path(__file__).name:
                continue          # この試験自身は名前を書いてよい
            text = path.read_text(encoding="utf-8", errors="replace")
            if "import tkinter" in text:
                offenders.append(str(path.relative_to(_ROOT)))
        self.assertEqual(offenders, [],
                         f"tkinter を取り込んでいるファイルがあります: {offenders}")


if __name__ == "__main__":                       # pragma: no cover
    unittest.main()
