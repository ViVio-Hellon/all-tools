"""この端末のラインが決まるまで、日報は打たせない (v4.3.0)

    この端末のラインがまだ決まっていません(上の帯のラインは「未設定」)。
    決まるまでは L-1 として保存します。：決めるまで入力させないほうがいいね

それまでは止めずに L-1 として保存していました。知らせを読まずに打ち始めると
**別のラインの日報が L-1 に混ざり**、直すにはその直を打ち直すことになります。
決めるのは1度きりなので、最初に止めます。

- 画面: 表と作業者のカードを伏せ、「ラインを決める」の関門だけを出す
  (作業者の帯・前の直の帯より先)
- サーバ: 保存・次ページ発行・全停入力を断る(画面だけで止めると、古い画面や
  別のタブから通ってしまう)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def typed() -> dict:
    return {"rows": {"1": {"LOT": "N7131T0", "KZ": "08", "KH": "00", "SZ": "10",
                           "SH": "00", "CON": "10", "WEI": "1000", "MAI": "10"}},
            "header": {"worker": "山田"}, "checks": {}}


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class LineGateTests(WebTestCase):
    terminal_line = None          # 配った先の、まだラインを決めていない端末

    def decide(self, line: str = "L-1") -> None:
        from nippou import config, user_settings

        user_settings.save(config.KEY_TERMINAL_LINE, line)

    def _keys(self, repo):
        """書かれた日報(ヘッダーの行)。**1行も無いこと**を確かめる。"""
        from nippou.db.schema import ensure_schema

        ensure_schema(repo.conn)
        return [tuple(r) for r in repo.conn.execute("SELECT * FROM daily_header")]

    # -- 画面 ----------------------------------------------------------
    def test_画面は関門だけを出す(self) -> None:
        html = self.get("/").get_data(as_text=True)
        gate = html[html.index('id="terminal-line-note"') - 80:]
        gate = gate[:gate.index("</div>\n</div>") + 12]
        self.assertNotIn("hidden", gate[:gate.index(">")])
        self.assertIn("決まるまで日報は打てません", gate)
        self.assertIn('id="start-pick-line"', gate)
        self.assertIn('href="/settings?tab=terminal"', gate)
        # 作業者の帯は出さない(ラインが先)
        start = html[html.index('id="start-shift"'):]
        self.assertIn("hidden", start[:start.index(">")])
        # 紙の題にも L-1 と出さない(帯と同じく「未設定」)
        key = html[html.index('class="sheet-key__now"'):]
        self.assertIn("／ 未設定</b>", key[:key.index("</b>") + 4])
        now = html[html.index('class="sheet-now__line"'):]
        self.assertTrue(now.startswith('class="sheet-now__line">未設定<'))

    def test_状態の応答もラインを先に言う(self) -> None:
        body = self.post("/api/entry/state", typed() | {"header": {}}).get_json()
        self.assertTrue(body["needs_line"])
        self.assertEqual(body["line_label"], "未設定")
        self.assertFalse(body["needs_worker"])
        self.assertFalse(body["handover"]["blocked"])
        self.assertIn("まだ決まっていません", body["line_note"])

    # -- サーバが断る ---------------------------------------------------
    def test_保存は409で断り_何も書かない(self) -> None:
        res = self.post("/api/entry/save", typed())
        self.assertEqual(res.status_code, 409)
        body = res.get_json()
        self.assertFalse(body["saved"])
        self.assertIn("ラインがまだ決まっていない", body["message"])
        self.assertIn("この端末のライン", body["message"])
        self.assertEqual(self._keys(self.repo()), [])

    def test_自動保存と打ちかけは黙って見送る(self) -> None:
        for flag in ("silent", "draft"):
            with self.subTest(flag=flag):
                res = self.post("/api/entry/save", typed() | {flag: True})
                self.assertEqual(res.status_code, 200)
                body = res.get_json()
                self.assertFalse(body["saved"])
                self.assertIn("ライン", body["skipped"])
        self.assertEqual(self._keys(self.repo()), [])

    def test_次ページ発行と全停入力も断る(self) -> None:
        res = self.post("/api/entry/newpage", typed())
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()["error"]["code"], "line_undecided")
        res = self.post("/api/formstop/execute",
                        {"reason": "停電", "code": "1", "worker": "山田"})
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()["error"]["code"], "line_undecided")
        self.assertEqual(self._keys(self.repo()), [])

    # -- 決めれば通る ---------------------------------------------------
    def test_決めれば関門は消えて保存できる(self) -> None:
        self.decide("L-1")
        body = self.post("/api/entry/state", typed()).get_json()
        self.assertFalse(body["needs_line"])
        self.assertEqual(body["line_note"], "")
        res = self.post("/api/entry/save", typed())
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True)[:300])
        self.assertTrue(res.get_json()["saved"])
        html = self.get("/").get_data(as_text=True)
        gate = html[html.index('id="terminal-line-note"'):]
        self.assertIn("hidden", gate[:gate.index(">")])


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class DecidedTerminalTests(WebTestCase):
    """決めてある端末(テストの既定)では、関門は出ない。"""

    def test_関門は出ない(self) -> None:
        body = self.post("/api/entry/state", typed()).get_json()
        self.assertFalse(body["needs_line"])
        self.assertEqual(self.post("/api/entry/save", typed()).status_code, 200)


class WiringTests(unittest.TestCase):
    """画面の配線(ブラウザが無くても見られるもの)。"""

    def test_表と作業者のカードを伏せる(self) -> None:
        js = (ROOT / "app" / "static" / "js" / "views" / "entry.js").read_text(encoding="utf-8")
        self.assertIn("const noLine = !!view?.needs_line;", js)
        self.assertIn('document.body.classList.toggle("needs-line", noLine);', js)
        self.assertIn('needs_line: !document.getElementById("terminal-line-note")?.hidden', js)
        css = (ROOT / "app" / "static" / "css" / "components.css").read_text(encoding="utf-8")
        self.assertIn('body.needs-line [data-step-for="worker"]', css)

    def test_文言はL_1で保存すると言わない(self) -> None:
        from nippou import work_context

        self.assertNotIn("L-1 として保存", work_context.LINE_GATE_MESSAGE)
        self.assertIn("この端末のライン", work_context.LINE_GATE_MESSAGE)


if __name__ == "__main__":
    unittest.main()
