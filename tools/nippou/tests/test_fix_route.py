"""直せと言われたページへ、行く道がある

    2026年9月15日 L-1 1直 1ページ 1行目 ロット H573E52:
    梱包数 20 が検入枚数 15 を超えています
    …
    先に次を直してください:
    ・2026年9月15日 L-1 1直 1ページ 1行目 …

    直してほしいといいつつないから直せないんですが

2つ欠けていました:

    1. 断りの中に**開く道**が無い(並べるだけ)
    2. 記録を見るの一覧は「保存の新しい順に20直」── 取り込みで同じ
       時刻の紙が何十枚も入ると、**古い未送信の直が溢れて出てこない**

名指しで断っておいて、そこへ行けないのでは直しようがありません。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.models import DetailRecord, HeaderRecord

try:
    from tests._web import WebTestCase
    HAS_FLASK = True
except Exception:                                 # noqa: BLE001
    HAS_FLASK = False
    WebTestCase = object                          # type: ignore
SKIP = "Flask が入っていません"

ROOT = Path(__file__).resolve().parent.parent
ENTRY_JS = (ROOT / "app/static/js/views/entry.js").read_text(encoding="utf-8")


@unittest.skipUnless(HAS_FLASK, SKIP)
class RecordListTests(WebTestCase):
    """記録を見る ── **未送信は古くても必ず出す。**"""

    def setUp(self) -> None:
        super().setUp()
        self.post("/api/entry/line", {"line": "L-1"})

    def save(self, report_date: str, shift: str = "1直", *,
             synced: bool = True, page: int = 1, line: str = "L-1",
             saved_at: str = "") -> None:
        key = dict(report_date=report_date, line=line, shift=shift, page=page)
        repo = self.repo()
        repo.save(HeaderRecord(**key, worker="山田"),
                  [DetailRecord(**key, row_no=1, lot="A1", ken="10")])
        if synced:
            repo.mark_synced((report_date, line, shift, page))
        if saved_at:
            repo.conn.execute(
                "UPDATE daily_header SET saved_at=? WHERE report_date=?"
                " AND line=? AND shift=? AND page=?",
                (saved_at, report_date, line, shift, page))
            repo.conn.commit()

    def rows(self) -> list[dict]:
        """一覧の中身。**アプリの文脈の中で**呼ぶ(DBはそこから引く)。"""
        from app.routes.printing import _recent_shifts
        with self.app.test_request_context(headers={"Host": "127.0.0.1"}):
            return _recent_shifts()

    def test_古い未送信でも一覧に出る(self) -> None:
        """**ここが本題。** 新しい保存が20直より多くても溢れさせない。"""
        self.save("2026年9月15日", synced=False, saved_at="2026-09-15T07:00:00")
        for day in range(1, 26):                  # 済を25直ぶん(新しい)
            self.save(f"2026年10月{day}日",
                      saved_at=f"2026-10-{day:02d}T07:00:00")
        keys = [(r["report_date"], r["shift"]) for r in self.rows()]
        self.assertIn(("2026年9月15日", "1直"), keys)

    def test_未送信が先に並ぶ(self) -> None:
        """片付ける順です。"""
        self.save("2026年9月15日", synced=False, saved_at="2026-09-15T07:00:00")
        self.save("2026年10月1日", saved_at="2026-10-01T07:00:00")
        first = self.rows()[0]
        self.assertEqual(first["report_date"], "2026年9月15日")
        self.assertFalse(first["synced"])

    def test_済は数を切る(self) -> None:
        """**読みすぎない歯止め**は「済」のほうに残します。"""
        for day in range(1, 26):
            self.save(f"2026年10月{day}日",
                      saved_at=f"2026-10-{day:02d}T07:00:00")
        rows = self.rows()
        self.assertEqual(len(rows), 20)
        self.assertTrue(all(r["synced"] for r in rows))

    def test_ページは直ごとにまとまる(self) -> None:
        """2つの出どころ(未送信・保存済み)が重なっても二重に出さない。"""
        self.save("2026年9月15日", page=1, synced=False)
        self.save("2026年9月15日", page=2, synced=False)
        rows = [r for r in self.rows() if r["report_date"] == "2026年9月15日"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["pages"], [1, 2])

    def test_1ページでも残っていれば未(self) -> None:
        self.save("2026年9月15日", page=1, synced=True)
        self.save("2026年9月15日", page=2, synced=False)
        row = [r for r in self.rows() if r["report_date"] == "2026年9月15日"][0]
        self.assertFalse(row["synced"])
        self.assertEqual(row["pages"], [1, 2])

    def test_画面にも並びの決まりが書いてある(self) -> None:
        html = self.get("/records").get_data(as_text=True)
        self.assertIn("共有へ未送信の直は、古くても必ず出します", html)


@unittest.skipUnless(HAS_FLASK, SKIP)
class RefusalRouteTests(WebTestCase):
    """断り ── **そこへ行くボタンを一緒に返す。**"""

    KEY = dict(report_date="2026年9月15日", line="L-1", shift="1直", page=1)

    def setUp(self) -> None:
        super().setUp()
        self.post("/api/entry/line", {"line": "L-1"})
        # 梱包数 20 が検入枚数 15 を超えている ── 「設備移動のため保存」
        # では通せない間違い
        self.repo().save(
            HeaderRecord(**self.KEY, worker="前の人"),
            [DetailRecord(**self.KEY, row_no=1, lot="H573E52", ken="15",
                          mai="10", tut="2", kz="08", kh="00",
                          sz="09", sh="00")])

    def push(self):
        return self.post("/api/settings/push", {})

    def test_断られる(self) -> None:
        self.assertEqual(self.push().status_code, 422)

    def test_通せないものに行き先が付く(self) -> None:
        """**ここが本題。** `at` が無いと画面は開くボタンを出せません。"""
        body = self.push().get_json()
        blockers = body["skip_blockers"]
        self.assertTrue(blockers, "通せないものが出ていない")
        at = blockers[0]["at"]
        self.assertEqual(at["report_date"], "2026年9月15日")
        self.assertEqual(at["line"], "L-1")
        self.assertEqual(at["shift"], "1直")
        self.assertEqual(at["page"], 1)

    def test_どの直の話かは文にも残す(self) -> None:
        body = self.push().get_json()
        self.assertIn("2026年9月15日 L-1 1直", body["skip_blockers"][0]["where"])

    def test_画面は同じ形で描く(self) -> None:
        """並べるだけの `<li>` に戻さない(開くボタンが消えます)。"""
        found = re.search(r"const list = document\.getElementById"
                          r"\(\"entry-skip-blockers\"\);(.*?)\n  }",
                          ENTRY_JS, re.S)
        self.assertIsNotNone(found)
        self.assertIn("findingItem", found.group(1))

    def test_空の紙では開くのをやめない(self) -> None:
        """**いちばん深いところ。**

        開くボタンは移る前に打ちかけを手元へ置きます。ところが1行も
        打っていない紙は保存しない(空の紙を作らない)ので、そこで
        断られて**開くのをやめていました** ── 直しに行こうとするのは
        たいてい画面を開いた直後(空のまま)なので、いちばん必要なときに
        だけ動かない、という形でした。
        """
        # 中身は `placeDraft`(v4.24.0 ── 置けたか・直の変わり目・ページ違いを分けて返す)
        found = re.search(r"async function placeDraft\([^)]*\) \{(.*?)\n\}",
                          ENTRY_JS, re.S)
        self.assertIsNotNone(found)
        body = found.group(1)
        self.assertIn('err.code === "empty_sheet"', body)
        self.assertIn("return { ok: true }", body)

    def test_管理者モードの切り替え先まで書く(self) -> None:
        """断られた人が次にすることが書いていないと、そこで止まります。"""
        res = self.post("/api/settings/recall",
                        {**self.KEY, "shift": "2直"})
        self.assertEqual(res.status_code, 403)
        self.assertIn("設定・管理者", res.get_json()["error"]["message"])


@unittest.skipUnless(HAS_FLASK, SKIP)
class EmptySheetTests(WebTestCase):
    """空の紙の断りに、**画面が見分けられる印**が付いているか。"""

    def setUp(self) -> None:
        super().setUp()
        self.post("/api/entry/line", {"line": "L-1"})

    def test_印が付いている(self) -> None:
        res = self.post("/api/entry/save",
                        {"rows": {}, "header": {}, "checks": {}})
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"], "empty_sheet")


if __name__ == "__main__":
    unittest.main()
