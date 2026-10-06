"""アプリは tkinter に触らない(python-web-tools の test_no_tkinter.py と同じ網)

VBA版の「フォルダ選択」(SelectFolderDialog)は、tkinter のダイアログでは
なく**サーバ側のフォルダ参照**(`/api/fs/list`)に置き換えた。tkinter の
ダイアログはブラウザの裏に隠れ、表示環境の無い端末では落ちるため。
ここで固定しておけば、`import tkinter` を足した時点で落ちる。
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

try:
    import flask  # noqa: F401
    HAS_WEB = True
except ImportError:                              # pragma: no cover
    HAS_WEB = False

WEB_MODULES = (
    "app", "app.business", "app.errors",
    "app.routes.health", "app.routes.inspection", "app.routes.preview",
    "app.routes.printing", "app.routes.settings",
    "app.services.inspection_service", "app.services.excel_service",
    "app.services.preview_service", "app.services.print_service",
    "app.services.settings_service", "app.services.fs_browse",
    "app.services.admin_password", "app.services.distribution",
    "app.services.clipboard_image", "app.services.png_encoder",
    "app.services.demo_data", "app.services.system_info",
)
LAUNCH_MODULES = ("launch_guard", "process_manager", "server", "boot_server", "start_app",
                  "core.idle_exit", "core.boot_screen", "core.process_tracking")

_SCRIPT = """
import sys

class _Blocked:
    def find_spec(self, name, path=None, target=None):
        if name == "tkinter" or name.startswith("tkinter."):
            raise ImportError(name + " は使ってはいけません")
        return None

sys.meta_path.insert(0, _Blocked())
for name in {modules!r}:
    __import__(name)
print("OK")
"""


def _import_without_tkinter(modules) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", _SCRIPT.format(modules=modules)],
                          cwd=str(_ROOT), capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120)


@unittest.skipUnless(HAS_WEB, "Flask が入っていないためスキップ")
class NoTkinterTests(unittest.TestCase):
    def test_画面とサービスはtkinterを掴まない(self) -> None:
        result = _import_without_tkinter(WEB_MODULES)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_起動基盤もtkinterを掴まない(self) -> None:
        result = _import_without_tkinter(LAUNCH_MODULES)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_禁止の仕掛けそのものが効いている(self) -> None:
        result = _import_without_tkinter(("tkinter",))
        self.assertNotEqual(result.returncode, 0, "tkinter を止められていません")


if __name__ == "__main__":                       # pragma: no cover
    unittest.main()
