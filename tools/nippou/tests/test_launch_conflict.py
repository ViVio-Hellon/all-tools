"""多重起動を断るときは、居場所と止め方まで出す (`start_app`)

【なぜ機械で見るのか】
古い版が残っていて終わらせられないとき、黙って諦めると、押した人には
**「何も起きない」としか見えません。** 入れ替えたはずの新しい版は
いつまでも動かず、古い画面が開くだけです。

`Start.vbs` はコンソールを出さないので、**ブラウザに出さないと誰にも
届きません**。ここでは「何が動いているか」と「どうすれば止まるか」が
実際に書かれているかを見ます。
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import launch_guard
import start_app
from tests._launch_logs import keep_in_temp

# 起動の記録を本物の logs へ書かない(`tests/_launch_logs.py`)
keep_in_temp()


def _lock(**kwargs) -> launch_guard.LockInfo:
    values = dict(app_id="nlm.nippou-tool", pid=4321, port=8733,
                  url="http://127.0.0.1:8733/", started_at=time.time(),
                  version="2.9.0", app_root=r"\\srv\共有\日報ツール")
    values.update(kwargs)
    return launch_guard.LockInfo(**values)


class JoinTests(unittest.TestCase):
    """同じ版が動いているだけなら、**合流でよい**。"""

    def test_同じ版なら合流して0を返す(self) -> None:
        guard = launch_guard.GuardResult(
            False, url="http://127.0.0.1:8733/", existing=_lock(),
            reason="同じアプリが起動済みです")
        opened = []
        with patch.object(start_app.webbrowser, "open", opened.append):
            code = start_app._join_or_explain(guard, open_browser=True)
        self.assertEqual(code, 0)
        self.assertEqual(opened, ["http://127.0.0.1:8733/"])


class StaleTests(unittest.TestCase):
    """古い版が終わらないとき。**断るだけで終わらせない。**"""

    def setUp(self) -> None:
        self.guard = launch_guard.GuardResult(
            False, url="http://127.0.0.1:8733/", existing=_lock(),
            stale_version="2.9.0", reason="古い版が終了しませんでした")

    def test_起動しなかったことを伝える(self) -> None:
        """**0 では返さない。** 呼んだ側にも伝わる必要がある。"""
        with patch.object(start_app.webbrowser, "open"):
            code = start_app._join_or_explain(self.guard, open_browser=False)
        self.assertEqual(code, 1)

    def test_古いほうのブラウザを勝手に開かない(self) -> None:
        """開くと**古い版が動いているように見える**。案内を出す。"""
        opened = []
        with patch.object(start_app.webbrowser, "open", opened.append):
            start_app._join_or_explain(self.guard, open_browser=True)
        self.assertEqual(len(opened), 1)
        self.assertTrue(opened[0].startswith("file:"),
                        f"古いほうの画面を開いています: {opened[0]}")

    def test_コンソールにも居場所と止め方を出す(self) -> None:
        """`start.bat` から起動した人はこれを読む。"""
        text = "\n".join(start_app._stale_report(self.guard))
        for expected in ("PID 4321", "ポート 8733", "2.9.0",
                         r"\\srv\共有\日報ツール", "stop.bat"):
            self.assertIn(expected, text)

    def test_案内に何が動いているかが書いてある(self) -> None:
        page = start_app._write_conflict_page(self.guard)
        text = page.read_text(encoding="utf-8")
        for expected in ("PID 4321", "8733", "2.9.0", "日報ツール",
                         "古い版が終了しませんでした"):
            self.assertIn(expected, text)

    def test_案内に止め方が順に書いてある(self) -> None:
        text = start_app._write_conflict_page(self.guard).read_text(encoding="utf-8")
        # 上から順に、**穏やかなものから**
        self.assertLess(text.index("終了"), text.index("stop.bat --force"))
        self.assertIn("stop.bat", text)
        self.assertIn("stop.bat --force", text)

    def test_案内にこれから出す版も書いてある(self) -> None:
        """**どちらが新しいのか**が分からないと、止めてよいか判断できない。"""
        from nippou import app_config

        text = start_app._write_conflict_page(self.guard).read_text(encoding="utf-8")
        self.assertIn(app_config.version(), text)

    def test_ロックが読めなくても案内は出る(self) -> None:
        """壊れたロックを理由に、何も伝えないのが一番まずい。"""
        guard = launch_guard.GuardResult(
            False, stale_version="2.9.0", reason="古い版が終了しませんでした")
        text = start_app._write_conflict_page(guard).read_text(encoding="utf-8")
        self.assertIn("2.9.0", text)
        self.assertIn("stop.bat", text)
        # コンソール向けも落ちない
        self.assertIn("stop.bat", "\n".join(start_app._stale_report(guard)))

    def test_道に含まれる記号を素通しにしない(self) -> None:
        """道は打たれたものなので、HTMLに直接は入れない。"""
        guard = launch_guard.GuardResult(
            False, existing=_lock(app_root="<script>alert(1)</script>"),
            stale_version="2.9.0", reason="古い版が終了しませんでした")
        text = start_app._write_conflict_page(guard).read_text(encoding="utf-8")
        self.assertNotIn("<script>alert(1)</script>", text)
        self.assertIn("&lt;script&gt;", text)


if __name__ == "__main__":
    unittest.main()
