import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.access_bridge import importer
from nippou.access_bridge.runner import ScriptResult


class FakeSuccessRunner:
    """常に成功を返すダミーrunner(実際のcscript.exeは呼ばない)。"""

    def run(self, script_text: str) -> ScriptResult:
        return ScriptResult(success=True, rows=0)


def modules():
    """`importer` をそのつど引き直す。

    **Web版のテストが `nippou.*` を読み込み直す**(`tests/_web.py`)ので、
    ファイルの先頭で掴んだモジュールは古くなることがある。古い側を
    呼びながら新しい側に `patch` を当てると、当たらないまま素通りする。
    """
    import importlib

    return importlib.import_module("nippou.access_bridge.importer")


class ImportTableResilienceTests(unittest.TestCase):
    """「①取込み」側でも、想定外の例外でライン(このアプリ)自体が
    クラッシュしないことを検証する。SIKALOT.accdb のような数千件規模の
    共有マスタを取り込む際、ディスク容量不足など環境要因のエラーが
    起きても、呼び出し元(`app/routes/settings.py`)は落ちずに失敗結果を
    受け取れる必要がある。ここが例外を投げ抜けると**サーバごと落ちて
    画面が開かなくなる**ので、500 を返して済ませられる形にしておく。
    """

    def test_temp_file_creation_failure_is_reported_not_raised(self) -> None:
        mod = modules()
        with patch.object(mod.tempfile, "mkstemp", side_effect=OSError("容量不足")):
            result = mod.import_table(Path("dummy.accdb"), "仕掛", runner=FakeSuccessRunner())
        self.assertFalse(result.success)
        self.assertIsNotNone(result.error)

    def test_csv_read_failure_is_reported_not_raised(self) -> None:
        # cscript側は成功したとみなされるが、その後のCSV読み込みで
        # 想定外のOSErrorが起きるケース(ファイルが途中で消えた等)。
        mod = modules()
        with patch.object(mod, "open", side_effect=OSError("読み込み失敗"),
                          create=True):
            result = mod.import_table(Path("dummy.accdb"), "仕掛", runner=FakeSuccessRunner())
        self.assertFalse(result.success)
        self.assertIsNotNone(result.error)

    def test_import_shift_times_returns_none_on_failure_without_raising(self) -> None:
        class AlwaysFailRunner:
            def run(self, script_text: str) -> ScriptResult:
                return ScriptResult(success=False, err_desc="接続失敗")

        result = importer.import_shift_times(Path("dummy.accdb"), runner=AlwaysFailRunner())
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
