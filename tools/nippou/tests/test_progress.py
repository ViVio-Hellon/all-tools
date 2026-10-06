"""取り込みの進み具合

    件数が多いときはプログレス表示させてください

何百ページもある過去データの取り込みは何十秒もかかります。そのあいだ
画面は押したまま止まって見える ── **止まっているのか動いているのか**が
分からないと、人はもう一度押すか、閉じるかします。

取り込みは途中で切りません(途中まで入った日報を残さないため)。
そこで**進み具合だけ別に置いて**、画面が見に来られるようにしました:

    POST /api/settings/import/upload   … 本体(終わるまで返らない)
    GET  /api/settings/import/progress … いまどこか(何度でも聞ける)
"""
from __future__ import annotations

import re
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou import job_progress
from nippou.logic import progress as logic

try:
    from tests._web import WebTestCase
    HAS_FLASK = True
except Exception:                                 # noqa: BLE001
    HAS_FLASK = False
    WebTestCase = object                          # type: ignore
SKIP = "Flask が入っていません"

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_JS = (ROOT / "app/static/js/views/settings.js").read_text(
    encoding="utf-8")
SETTINGS_HTML = (ROOT / "app/templates/settings.html").read_text(
    encoding="utf-8")
CSS = (ROOT / "app/static/css/components.css").read_text(encoding="utf-8")


class WordingTests(unittest.TestCase):
    """**言葉を作るのは純ロジック。** 画面は写すだけ。"""

    def test_段といくつ目と割合を出す(self) -> None:
        now = logic.Progress(running=True, phase=logic.PHASE_WRITE,
                             done=120, total=340)
        self.assertIn("日報を入れています", now.headline)
        self.assertIn("120/340ページ", now.headline)
        self.assertIn("35%", now.headline)

    def test_何段目かを出す(self) -> None:
        """**終わったはずなのに終わらない**を避ける ── 段は4つあります。"""
        now = logic.Progress(running=True, phase=logic.PHASE_SUMMARY,
                             done=1, total=3)
        self.assertIn("[3/4]", now.headline)

    def test_総数が分からないうちは0パーセント(self) -> None:
        """**嘘の100を出さない。**"""
        now = logic.Progress(running=True, phase=logic.PHASE_READ)
        self.assertEqual(now.percent, 0)
        self.assertNotIn("%", now.headline)

    def test_走っていなければ何も言わない(self) -> None:
        self.assertEqual(logic.idle().headline, "")
        self.assertFalse(logic.idle().running)

    def test_いま触っているものを添える(self) -> None:
        now = logic.Progress(running=True, phase=logic.PHASE_WRITE,
                             label="2026年8月3日 L-1 1直 1ページ",
                             file_no=2, file_count=3, file_name="8月.xlsx")
        self.assertIn("2/3本目", now.note)
        self.assertIn("8月.xlsx", now.note)
        self.assertIn("2026年8月3日", now.note)

    def test_1本だけなら本数を言わない(self) -> None:
        now = logic.Progress(running=True, file_count=1, file_no=1,
                             file_name="8月.xlsx")
        self.assertNotIn("本目", now.note)

    def test_割合は0から100に収める(self) -> None:
        self.assertEqual(logic.Progress(done=999, total=10).percent, 100)
        self.assertEqual(logic.Progress(done=-5, total=10).percent, 0)

    def test_飾りを画面に出さない(self) -> None:
        now = logic.Progress(running=True, phase=logic.PHASE_CSV,
                             done=1, total=2, label="x")
        self.assertNotIn("**", now.headline + now.note)


class HolderTests(unittest.TestCase):
    """**数を持つのはプロセスに1つ。** 走っていなければ空を返す。"""

    def setUp(self) -> None:
        job_progress.reset()
        self.addCleanup(job_progress.reset)

    def test_始まる前は走っていない(self) -> None:
        self.assertFalse(job_progress.snapshot().running)

    def test_始めて進めて終わる(self) -> None:
        job_progress.start(file_count=2)
        self.assertTrue(job_progress.snapshot().running)
        job_progress.step(phase=logic.PHASE_WRITE, total=10)
        job_progress.step(done=4, label="1ページ")
        now = job_progress.snapshot()
        self.assertEqual((now.done, now.total), (4, 10))
        self.assertEqual(now.label, "1ページ")
        job_progress.finish()
        self.assertFalse(job_progress.snapshot().running)

    def test_段が変われば数え直す(self) -> None:
        """**前の段の「340/340」を次の段の頭に残さない。**"""
        job_progress.start()
        job_progress.step(phase=logic.PHASE_WRITE, total=340, done=340)
        job_progress.step(phase=logic.PHASE_SUMMARY)
        now = job_progress.snapshot()
        self.assertEqual((now.done, now.total), (0, 0))

    def test_始まっていなければ何も起きない(self) -> None:
        """取り込みを直接呼んだとき(テストや取り込みCLI)に落ちないこと。"""
        job_progress.step(phase=logic.PHASE_WRITE, total=5)
        self.assertFalse(job_progress.snapshot().running)

    def test_例外が出ても下ろす(self) -> None:
        with self.assertRaises(ValueError):
            with job_progress.watching(file_count=1):
                raise ValueError("途中で落ちた")
        self.assertFalse(job_progress.snapshot().running)

    def test_別のスレッドから見える(self) -> None:
        """**取り込みが1本のスレッドを占めていても、別の口が答えます。**"""
        job_progress.start()
        job_progress.step(phase=logic.PHASE_WRITE, total=7, done=3)
        seen: list[int] = []
        thread = threading.Thread(
            target=lambda: seen.append(job_progress.snapshot().done))
        thread.start()
        thread.join()
        self.assertEqual(seen, [3])


@unittest.skipUnless(HAS_FLASK, SKIP)
class ScreenTests(WebTestCase):
    """画面 ── 口があり、置き場所があり、止め忘れない。"""

    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(self.holder().reset)

    def holder(self):
        """**アプリが読み込み直したほうの**モジュールを掴む。

        `_web.WebTestCase` は1件ごとに `nippou*` を読み込み直すので、
        この本文の頭で取り込んだものは**別のモジュール**です(状態を
        持つものは、その違いがそのまま「見えない」になります)。
        """
        from nippou import job_progress as fresh
        return fresh

    def test_走っていなければ空で返る(self) -> None:
        body = self.get("/api/settings/import/progress").get_json()
        self.assertFalse(body["running"])
        self.assertEqual(body["headline"], "")

    def test_走っていれば数が返る(self) -> None:
        now = self.holder()
        now.start(file_count=1)
        now.step(phase=logic.PHASE_WRITE, total=340, done=120,
                 label="2026年8月3日 L-1 1直 1ページ")
        body = self.get("/api/settings/import/progress").get_json()
        self.assertTrue(body["running"])
        self.assertEqual(body["percent"], 35)
        self.assertIn("120/340ページ", body["headline"])
        self.assertIn("2026年8月3日", body["note"])

    def test_取り込みのあとは下りている(self) -> None:
        """**棒が出たままにならない。**"""
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        csv = self.tmp / "から.csv"
        csv.write_text("見出しだけ\n", encoding="utf-8")
        self.post("/api/settings/import/apply", {"path": str(csv)})
        self.assertFalse(
            self.get("/api/settings/import/progress").get_json()["running"])

    def test_画面に置き場所がある(self) -> None:
        html = SETTINGS_HTML
        self.assertIn('id="import-progress"', html)
        self.assertIn('id="import-progress-bar"', html)
        self.assertIn('id="import-progress-head"', html)
        self.assertIn("<progress", html)

    def test_見えるように塗ってある(self) -> None:
        self.assertIn(".job__bar", CSS)

    def test_押したら見に行き_終わったら止める(self) -> None:
        """**止め忘れると棒が残ります。**"""
        for name in ("async function applyImport", "async function sendFiles"):
            found = re.search(name + r"\([^)]*\) \{(.*?)\n\}",
                              SETTINGS_JS, re.S)
            self.assertIsNotNone(found, name)
            body = found.group(1)
            self.assertIn("watchProgress(true)", body, name)
            self.assertIn("finally", body, name)
            self.assertIn("watchProgress(false)", body, name)

    def test_数は画面で数えない(self) -> None:
        """**出どころは1つ。** 画面がページ数を数えると必ずずれます。"""
        found = re.search(r"function paintProgress\(now\) \{(.*?)\n\}",
                          SETTINGS_JS, re.S)
        self.assertIsNotNone(found)
        self.assertIn("now.headline", found.group(1))
        self.assertIn("now.percent", found.group(1))


if __name__ == "__main__":
    unittest.main()
