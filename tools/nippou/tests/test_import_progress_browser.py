"""過去データの取り込み: 進み具合の棒を本物のブラウザで (段階1)

前は `settings.js` の字を探して「`watchProgress(true)` と `finally` の `watchProgress(false)` が
あること」「`paintProgress` が `now.headline` を使うこと」を見ていました(`test_progress` の
`test_押したら見に行き_終わったら止める`・`test_数は画面で数えない`)。ここでは取り込みの答えを
止めて、そのあいだの画面を見ます:

    - 押したら(ファイルを渡したら)すぐ棒を出し、サーバの数と言葉をそのまま出す
    - 終われば棒を下ろす。**断られても下ろす**(棒が出たままにならない)
    - ファイルを渡したとき(`sendFiles`)も、道を打って入れるとき(`applyImport`)も同じ
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._screen_browser import ScreenBrowserCase  # noqa: E402

#: サーバが返す進み具合(画面はこれを写すだけ。数え直さない ── だから割合は件数と
#: わざと合わせていない。画面が 120/340 から数え直すと 35 になって分かる)
RUNNING = {"running": True, "percent": 40, "total": 340, "done": 120,
           "headline": "書き込み 120/340ページ(試験)", "note": "2026年8月3日 L-1 1直 1ページ"}


class ImportProgressTests(ScreenBrowserCase):
    def setUp(self) -> None:
        super().setUp()
        res = self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True)[:200])
        self.answers["/api/settings/import/progress"] = (200, RUNNING)

    def box_hidden(self, page) -> bool:
        return page.evaluate("document.getElementById('import-progress').hidden")

    def watch_running(self, page) -> None:
        """取り込みの答えを止めているあいだ、棒がサーバの数と言葉で出ていること。"""
        self.wait_held(page, 1)
        self.assertFalse(self.box_hidden(page), "押したのに棒が出ていません")
        page.wait_for_function(
            f"document.getElementById('import-progress-head').textContent === {RUNNING['headline']!r}",
            timeout=3000)
        self.assertEqual(page.text_content("#import-progress-note"), RUNNING["note"])
        self.assertEqual(page.evaluate("document.getElementById('import-progress-bar').value"), 40)

    def test_取り込むあいだは棒にサーバの数を出し_終われば断られても下ろす(self) -> None:
        page = self.open_screen("/settings?tab=data")
        self.assertTrue(self.box_hidden(page))

        with self.subTest("ファイルを渡したとき(下見)・断られても下ろす"):
            self.hold = ("/api/settings/import/upload",)
            page.set_input_files("#import-files", files=[{
                "name": "過去.csv", "mimeType": "text/csv", "buffer": "見出しだけ\n".encode("utf-8")}])
            self.watch_running(page)
            self.hold = ()
            self.refuse["/api/settings/import/upload"] = 422
            self._route(self.held.pop())                     # 断りを返す
            page.wait_for_function("document.getElementById('import-progress').hidden", timeout=3000)

        with self.subTest("道を打って入れるとき・断られても下ろす"):
            # (入れられたときは画面ごと描き直すので、棒はそれでも消える。断られたときは
            # 描き直さないので、下ろし忘れると出たまま残る)
            page = self.open_screen("/settings?tab=data")      # 渡したファイルを持たない画面で
            past = self.tmp / "過去.csv"
            past.write_text("日付,ライン,直,ロット№\n2026年8月3日,L-1,1直,B\n", encoding="utf-8-sig")
            page.fill("#import-path", str(past))
            # 道を打ったときは、1回目は下見を出すだけ(入れない)。下見が出てから入れる
            page.click("#import-go")
            page.wait_for_selector("#import-plan table", timeout=8000)
            self.hold = ("/api/settings/import/apply",)
            page.click("#import-go")
            self.watch_running(page)
            self.hold = ()
            self.answers["/api/settings/import/apply"] = (422, {
                "error": {"code": "test", "message": "読めません(試験)"},
                "ok": False, "message": "読めません(試験)"})
            self._route(self.held.pop())
            page.wait_for_selector("text=読めません(試験)", timeout=3000)
            page.wait_for_function("document.getElementById('import-progress').hidden", timeout=3000)


if __name__ == "__main__":
    unittest.main()
