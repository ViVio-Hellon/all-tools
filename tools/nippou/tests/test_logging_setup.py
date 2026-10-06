import logging
import logging.handlers
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dbkit.logging_utils import get_logger as dbkit_get_logger
from nippou.logging_setup import init_logging


class InitLoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._root_handlers_before = list(logging.getLogger().handlers)
        self._tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        root = logging.getLogger()
        for handler in list(root.handlers):
            if handler not in self._root_handlers_before:
                root.removeHandler(handler)
                handler.close()
        self._tmpdir.cleanup()

    def test_init_logging_creates_log_file(self) -> None:
        log_dir = Path(self._tmpdir.name)
        init_logging(log_dir)
        self.assertTrue((log_dir / "nippou.log").exists())

    def test_dbkit_logs_land_in_the_same_file_as_nippou_logs(self) -> None:
        log_dir = Path(self._tmpdir.name)
        init_logging(log_dir)

        dbkit_get_logger("sqlite_toolkit").info("dbkit実行ログ")
        logging.getLogger("nippou.ui").info("nippouログ")

        content = (log_dir / "nippou.log").read_text(encoding="utf-8")
        self.assertIn("dbkit.sqlite_toolkit", content)
        self.assertIn("dbkit実行ログ", content)
        self.assertIn("nippou.ui", content)
        self.assertIn("nippouログ", content)

    def test_calling_init_logging_twice_does_not_duplicate_handler(self) -> None:
        log_dir = Path(self._tmpdir.name)
        init_logging(log_dir)
        init_logging(log_dir)

        matching = [
            h for h in logging.getLogger().handlers
            if isinstance(h, logging.handlers.RotatingFileHandler)
            and getattr(h, "_nippou_log_path", None) == log_dir / "nippou.log"
        ]
        self.assertEqual(len(matching), 1)


if __name__ == "__main__":
    unittest.main()
