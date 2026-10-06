"""ログの書き先 ── 開けっぱなしでも日付どおりに残るか

【なぜ要るか】
このツールは朝に開いて一日中そのまま、という使い方をされる。素の
``FileHandler`` はファイル名を**起動時に1回だけ**決めるので、月初に開いて
月末まで開けていると、1か月ぶん全部が ``calendar_<開いた日>.log`` に入る。

* 日付でログを探す運用が崩れる
* 20秒ごとの同期が1日2万行ほど出すので、1つのファイルが数十MBになる

どちらも「起きてから気づく」たちのもので、気づいたときには調べたい日の
ログが巨大なファイルの中に埋まっている。
"""

from __future__ import annotations

import datetime as _dt
import logging
import tempfile
import unittest
from pathlib import Path

from . import _isolation

_isolation.ensure_isolated()

from calendar_app import logging_utils  # noqa: E402


class DailyFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.today = _dt.date.today()

    def handler(self, **kwargs) -> logging_utils.DailyFileHandler:
        h = logging_utils.DailyFileHandler(self.folder, **kwargs)
        h.setFormatter(logging.Formatter("%(message)s"))
        self.addCleanup(h.close)
        return h

    def write(self, handler, message: str, when: _dt.date) -> None:
        record = logging.LogRecord("t", logging.INFO, "", 1, message, None, None)
        record.created = _dt.datetime.combine(
            when, _dt.time(12, 0, 0)).timestamp()
        handler.emit(record)

    def name(self, when: _dt.date) -> Path:
        return self.folder / logging_utils._log_name(when)

    def test_今日のファイルに書く(self) -> None:
        handler = self.handler()
        self.write(handler, "きょう", self.today)
        self.assertIn("きょう", self.name(self.today).read_text(encoding="utf-8"))

    def test_日付が変わったら書き先も移る(self) -> None:
        """**ここが肝。** 素の FileHandler では移らない。"""
        handler = self.handler()
        tomorrow = self.today + _dt.timedelta(days=1)
        self.write(handler, "きょう", self.today)
        self.write(handler, "あす", tomorrow)

        self.assertIn("きょう", self.name(self.today).read_text(encoding="utf-8"))
        self.assertIn("あす", self.name(tomorrow).read_text(encoding="utf-8"))
        # 前の日のファイルに翌日ぶんを混ぜない
        self.assertNotIn("あす", self.name(self.today).read_text(encoding="utf-8"))

    def test_古いものは消す(self) -> None:
        """上限を置かないと、ローカル領域が静かに膨らみ続ける。"""
        old = self.name(self.today - _dt.timedelta(days=logging_utils.KEEP_DAYS + 1))
        keep = self.name(self.today - _dt.timedelta(days=1))
        for path in (old, keep):
            path.write_text("x", encoding="utf-8")

        self.handler()
        self.assertFalse(old.exists())
        self.assertTrue(keep.exists())

    def test_名前が違うものは触らない(self) -> None:
        """人が手で置いた控えを消さない。"""
        other = self.folder / "手で取った控え.log"
        other.write_text("x", encoding="utf-8")
        self.handler()
        self.assertTrue(other.exists())

    def test_日付をまたいだときも掃除する(self) -> None:
        """開けっぱなしの端末では、起動時の掃除だけでは効かない。"""
        handler = self.handler(keep_days=1)
        stale = self.name(self.today - _dt.timedelta(days=5))
        stale.write_text("x", encoding="utf-8")

        self.write(handler, "あす", self.today + _dt.timedelta(days=1))
        self.assertFalse(stale.exists())

    def test_掃除しない設定なら残す(self) -> None:
        old = self.name(self.today - _dt.timedelta(days=400))
        old.write_text("x", encoding="utf-8")
        self.handler(keep_days=0)
        self.assertTrue(old.exists())


class ConfigureTests(unittest.TestCase):
    def test_日付で切り替わるハンドラを使う(self) -> None:
        """``configure_logging`` が素の FileHandler へ戻らないように。"""
        logging_utils.configure_logging()
        root = logging.getLogger(logging_utils._ROOT_NAME)
        files = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
        self.assertTrue(files)
        for handler in files:
            self.assertIsInstance(handler, logging_utils.DailyFileHandler)


if __name__ == "__main__":
    unittest.main()
