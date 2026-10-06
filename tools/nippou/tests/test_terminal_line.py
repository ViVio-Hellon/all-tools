"""この端末のラインを覚える ── **起動し直しても L-1 に戻らない**

    パスの保存先、共有保存前の日報入力データはどこに保存されているのですか？
    ツール配布時に注意しないといけないことなどありますか

配布の注意を洗い出していて見つかりました。設定画面で決める
「この端末のライン」を**覚えていませんでした**(プロセスの中だけ)。

    LVC の端末で設定 → LVC で動く
    画面を閉じる → 90秒でアプリが自分で終わる(`idle_exit`)
    翌朝起動 → **L-1 に戻っている**
      → LVC の日報が L-1 のキーで保存され、共有の T_日報ヘッダー_L-1 へ送られる

VBA はレジストリに覚えていました(`SaveSetting "日報管理", "Config", "Line"`)。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import WebTestCase


class RememberTests(WebTestCase):
    """設定画面で決めたラインを、**起動し直しても**使う。"""

    terminal_line = None          # まだ決めていない端末から始める

    def admin(self) -> None:
        res = self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        self.assertEqual(res.status_code, 200)

    def restart(self):
        """アプリを起動し直したのと同じ ── プロセスの中の状態を捨てる。"""
        from nippou import work_context
        return work_context.reset()

    def saved(self) -> dict:
        from nippou import config
        path = config.USER_CONFIG_PATH
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def test_決めていなければL_1(self) -> None:
        self.assertEqual(self.restart().line, "L-1")

    def test_決めたラインで起動する(self) -> None:
        """**ここが本題。** 以前は起動し直すたびに L-1 でした。"""
        self.admin()
        res = self.post("/api/entry/line", {"line": "LVC"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["remembered"])
        self.assertIn("覚えました", res.get_json()["message"])

        ctx = self.restart()
        self.assertEqual(ctx.line, "LVC")
        self.assertEqual(ctx.terminal_line, "LVC")

    def test_丸徳は設備番号も覚える(self) -> None:
        self.admin()
        self.post("/api/entry/line", {"line": "中板", "maru_sub": "3"})
        ctx = self.restart()
        self.assertEqual((ctx.line, ctx.maru_sub), ("中板", "3"))

    def test_丸徳から移れば設備番号は消える(self) -> None:
        self.admin()
        self.post("/api/entry/line", {"line": "中板", "maru_sub": "3"})
        self.post("/api/entry/line", {"line": "機側"})
        ctx = self.restart()
        self.assertEqual((ctx.line, ctx.maru_sub), ("機側", ""))

    def test_知らないラインが書かれていたらL_1(self) -> None:
        """ファイルを手で直して打ち間違えても、**起動はする。**"""
        from nippou import config, user_settings
        user_settings.save(config.KEY_TERMINAL_LINE, "L9")
        self.assertEqual(self.restart().line, "L-1")

    def test_覚えられなければそう言う(self) -> None:
        """黙って ok にすると、翌朝また L-1 に戻ります。"""
        self.admin()
        with patch("nippou.user_settings.save_many", return_value=False):
            body = self.post("/api/entry/line", {"line": "LVC"}).get_json()
        self.assertFalse(body["remembered"])
        self.assertIn("覚えられませんでした", body["message"])

    def test_管理者でなければ変えられないし覚えない(self) -> None:
        res = self.post("/api/entry/line", {"line": "LVC"})
        self.assertEqual(res.status_code, 403)
        self.assertNotIn("terminal_line", self.saved())

    def test_設定画面に覚えているかが出る(self) -> None:
        html = self.get("/settings?tab=terminal").get_data(as_text=True)
        self.assertIn("まだ決めていません", html)
        self.admin()
        self.post("/api/entry/line", {"line": "LVC"})
        html = self.get("/settings?tab=terminal").get_data(as_text=True)
        self.assertIn("この端末に覚えています", html)


class RecallReturnTests(WebTestCase):
    """他ラインの紙を開いて戻ったら、**この端末のライン**へ戻る。

    呼び出しは `ctx.line` を開いたラインにします。以前は戻る先が無く、
    「いまの直に戻る」を押したあとも他ラインの端末として動いていました。
    """

    def setUp(self) -> None:
        super().setUp()
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        self.post("/api/entry/line", {"line": "LVC"})
        from nippou.db.models import DetailRecord, HeaderRecord
        key = dict(report_date="2026年9月1日", line="L-1", shift="1直", page=1)
        self.repo().save(HeaderRecord(**key, worker="山田"),
                         [DetailRecord(**key, row_no=1, lot="A1")])

    def ctx(self):
        from nippou import work_context
        return work_context.get_context()

    def test_呼び出しているあいだは開いたライン(self) -> None:
        res = self.post("/api/settings/recall", {
            "report_date": "2026年9月1日", "line": "L-1", "shift": "1直", "page": 1})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.ctx().line, "L-1")
        self.assertEqual(self.ctx().terminal_line, "LVC", "端末のラインまで変わった")

    def test_戻ったら端末のライン(self) -> None:
        self.post("/api/settings/recall", {
            "report_date": "2026年9月1日", "line": "L-1", "shift": "1直", "page": 1})
        self.post("/api/settings/back", {})
        self.assertEqual(self.ctx().line, "LVC")

    def test_設定画面は端末のラインを出す(self) -> None:
        """呼び出し中でも「この端末は LVC」と言う。"""
        self.post("/api/settings/recall", {
            "report_date": "2026年9月1日", "line": "L-1", "shift": "1直", "page": 1})
        from nippou.presenters import settings as presenter
        self.assertEqual(presenter.line_view()["current"], "LVC")

    def test_呼び出しは覚えない(self) -> None:
        self.post("/api/settings/recall", {
            "report_date": "2026年9月1日", "line": "L-1", "shift": "1直", "page": 1})
        from nippou import work_context
        self.assertEqual(work_context.reset().line, "LVC")


if __name__ == "__main__":
    unittest.main()
