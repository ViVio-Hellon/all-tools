"""版がどこに出るか

【なぜ試験にするか】
端末はフォルダごとコピーして配るので、**その端末に入っているのはどれか**を
電話で聞かれます。聞かれる場面は決まっていて、

* 使っているとき   … 帯(どの画面でも消えない)
* 紙を渡されたとき … 印刷した1枚の右下
* 起動できないとき … 起動エラーの画面 / 起動待機画面
* ログだけ渡されたとき … 1枚目

このどれかが欠けると、そこだけ「バージョンは分かりません」になります。
出どころは ``config/app.json`` ただ1つで、増やさないことも一緒に見ます。
"""

from __future__ import annotations

import datetime as _dt
import unittest

from . import _web
from calendar_app import app_config, db, printing
from calendar_app.presenters import calendar as presenter


class RibbonTests(unittest.TestCase):
    """使っているとき ── 帯。**どの画面でも消えない。**"""

    def setUp(self) -> None:
        _web.reset_sync()
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()

    def page(self, path: str) -> str:
        return self.client.get(f"{path}?t={_web.TOKEN}").get_data(as_text=True)

    def test_カレンダーに出る(self) -> None:
        self.assertIn(app_config.version_label(), self.page("/calendar"))

    def test_設定にも出る(self) -> None:
        self.assertIn(app_config.version_label(), self.page("/settings"))

    def test_読める大きさで出す(self) -> None:
        """出しても読めなければ、探させることになる。"""
        from pathlib import Path

        css = (Path(__file__).resolve().parent.parent
               / "app/static/css/layout.css").read_text(encoding="utf-8")
        block = css.split(".ver{", 1)[1].split("}", 1)[0]
        size = int(block.split("font-size:", 1)[1].split("px", 1)[0])
        self.assertGreaterEqual(size, 12)


class PrintTests(unittest.TestCase):
    """紙を渡されたとき ── 印刷した1枚。"""

    def render(self) -> str:
        conn = db.connect(":memory:")
        self.addCleanup(conn.close)
        from calendar_app.repository import Repository

        start = presenter.grid_start(2026, 9)
        records = Repository(conn).get_month_records(
            start, start + _dt.timedelta(days=41))
        return printing.render_month_document(
            year=2026, month=9, start=start, records=records, my_line="L-1")

    def test_紙にも版が残る(self) -> None:
        self.assertIn(app_config.version_label(), self.render())

    def test_出力日時と並べて出す(self) -> None:
        """右下の1行にまとめる ── 表の中に混ぜない。"""
        footer = [line for line in self.render().splitlines()
                  if 'class="footer"' in line]
        self.assertTrue(footer)
        self.assertIn(app_config.version_label(), footer[0])


class StartupTests(unittest.TestCase):
    """起動できないとき ── そこがいちばん聞かれる。"""

    def test_起動エラーの画面に出る(self) -> None:
        import start_app

        path = start_app._write_error_page("だめでした", "こうしてください", "/logs")
        self.addCleanup(path.unlink, True)
        self.assertIn(app_config.version_label(),
                      path.read_text(encoding="utf-8"))

    def test_起動待機画面に出る(self) -> None:
        from calendar_app import boot_screen

        html = boot_screen.render(
            display_name="テストアプリ", version_label=app_config.version_label(),
            token="tok", app_id="nlm.test", poll_ms=500,
            home_url="/calendar", log_dir="C:/logs")
        self.assertIn(app_config.version_label(), html)

    def test_起動ログの1枚目に出る(self) -> None:
        """ログだけ渡されたときに、どの版が書いたものか分かるように。"""
        import logging

        import start_app

        seen: list[str] = []

        class Catch(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                seen.append(record.getMessage())

        logger = start_app.log()
        handler = Catch()
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)

        start_app.log_environment()
        self.assertTrue(any(app_config.version_label() in line for line in seen),
                        seen)


class SourceTests(unittest.TestCase):
    def test_出どころは1つ(self) -> None:
        """画面・紙・ログ・API が別々の値を持たないこと。"""
        self.assertEqual(app_config.version_label(),
                         app_config.VERSION_PREFIX + app_config.version())

    def test_健康確認と同じ値(self) -> None:
        _web.reset_sync()
        self.conn = _web.bind_db(self)
        body = _web.json_of(_web.make_client().get("/api/health"))
        self.assertEqual(body["version"], app_config.version())


if __name__ == "__main__":
    unittest.main()
