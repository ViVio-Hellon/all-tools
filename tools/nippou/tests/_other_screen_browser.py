"""日報入力**以外の画面**を本物のブラウザで動かす試験の土台(段階1)。

日報入力の土台 `EntryBrowserCase`(`_entry_browser.py`)をそのまま使い、開き方だけ足す:

    - `open_screen(path, ready)` … 画面を開き、`ready` の要素が出るまで待つ(`#grid-body` は待たない)
    - `admin()` / `unlock()` … 管理者モード・マスタの鍵を**サーバに**開けておく(Python の試験と同じ道)
    - `settle_review()` … 終わったまま残る直があると確認の画面へ連れて行かれるので、先に済ませる
    - `posts(prefix)` … 送った要求のうち道が `prefix` で始まるものの本文

使う試験: `test_vc_browser`・`test_gw_browser`・`test_master_browser`・`test_settings_browser`・
`test_records_browser`・`test_push_browser`。
"""
from __future__ import annotations

from tests._entry_browser import ORIGIN, EntryBrowserCase

__all__ = ["ORIGIN", "ScreenBrowserCase"]


class ScreenBrowserCase(EntryBrowserCase):
    def open_screen(self, path: str, ready: str = "main#main", *, settle: int = 500):
        page = self.watch(self.ctx.new_page())
        page.goto(f"{ORIGIN}{path}", wait_until="load")
        page.wait_for_selector(ready)
        page.wait_for_timeout(settle)                 # 画面のモジュールが繋がるまで
        return page

    def admin(self) -> None:
        res = self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        self.assertEqual(res.status_code, 200, res.get_json())

    def unlock(self) -> None:
        res = self.post("/api/master/unlock", {"enable": True, "password": "nisk"})
        self.assertEqual(res.status_code, 200, res.get_json())

    def settle_review(self) -> None:
        """直の実績の確認(`shift_end.js`)へ連れて行かれないように、先に確認を済ませる。"""
        for _ in range(3):
            state = self.get("/api/graph/review").get_json()
            if not state.get("due"):
                break
            self.post("/api/graph/review/ack", {"stage": state.get("stage", "")})

    def posts(self, prefix: str) -> list[dict]:
        return [body for path, body in self.sent if path.split("?")[0].startswith(prefix)]

    def paths(self, since: int = 0) -> list[str]:
        return [path for path, _ in self.sent[since:]]
