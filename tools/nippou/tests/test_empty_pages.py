"""中身の無い紙 ── 見えるようにして、消せるようにする

    残っているのか見えなければ消せないですよね

v3.61.2 で「1行も打っていない紙は作らない」ようにしましたが、**それ以前に
できてしまったぶんは残ったまま**です。数が出るだけでは消せないので、
一覧に出して、そこから消せるようにしました。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.models import DetailRecord, HeaderRecord
from nippou.logic import empty_pages as logic

try:
    from tests._web import WebTestCase
    HAS_FLASK = True
except Exception:                                 # noqa: BLE001
    HAS_FLASK = False
    WebTestCase = object                          # type: ignore
SKIP = "Flask が入っていません"

KEY = dict(report_date="2026年9月17日", line="L-1", shift="1直", page=1)


def blank_rows(n: int = 12) -> list[DetailRecord]:
    return [DetailRecord(**KEY, row_no=i) for i in range(1, n + 1)]


class EmptyTests(unittest.TestCase):
    """**何を「中身が無い」と見るか。**"""

    def test_12行とも空なら空(self) -> None:
        self.assertTrue(logic.is_empty(blank_rows()))

    def test_行が1つも無くても空(self) -> None:
        self.assertTrue(logic.is_empty([]))

    def test_ロットが1つあれば空ではない(self) -> None:
        rows = blank_rows()
        rows[0] = DetailRecord(**KEY, row_no=1, lot="N7131T0")
        self.assertFalse(logic.is_empty(rows))

    def test_停止だけでも空ではない(self) -> None:
        """停止を1つ入れただけの行も中身です。"""
        rows = blank_rows()
        rows[2] = DetailRecord(**KEY, row_no=3, s="0", th="60")
        self.assertFalse(logic.is_empty(rows))

    def test_理由だけでも空ではない(self) -> None:
        rows = blank_rows()
        rows[4] = DetailRecord(**KEY, row_no=5, reason="棚卸し準備")
        self.assertFalse(logic.is_empty(rows))

    def test_紙に出ない欄でも空ではない(self) -> None:
        """印刷範囲外の欄も中身は中身です。"""
        rows = blank_rows()
        rows[0] = DetailRecord(**KEY, row_no=1, others1="控え")
        self.assertFalse(logic.is_empty(rows))

    def test_空白だけは空(self) -> None:
        rows = blank_rows()
        rows[0] = DetailRecord(**KEY, row_no=1, lot="   ")
        self.assertTrue(logic.is_empty(rows))


class ViewTests(unittest.TestCase):
    def page(self, **over):
        base = dict(report_date="2026年9月17日", line="L-1", shift="1直", page=1)
        base.update(over)
        return logic.Page(**base)

    def test_古いものから並ぶ(self) -> None:
        found = logic.build([self.page(report_date="2026年9月18日"),
                             self.page(report_date="2026年9月16日"),
                             self.page(report_date="2026年9月17日")])
        self.assertEqual([p.report_date for p in found.pages],
                         ["2026年9月16日", "2026年9月17日", "2026年9月18日"])

    def test_無ければそう言う(self) -> None:
        self.assertEqual(logic.build([]).headline, "中身の無いページはありません")

    def test_共有へ渡したものは消せない(self) -> None:
        got = self.page(synced=True)
        self.assertFalse(got.can_delete)
        self.assertIn("共有へ渡してある", got.note)

    def test_消せる枚数が見出しに出る(self) -> None:
        found = logic.build([self.page(), self.page(page=2, synced=True)])
        self.assertIn("2枚", found.headline)
        self.assertIn("1枚 が消せます", found.headline)
        self.assertEqual(found.deletable, 1)

    def test_全部消せるときは余計なことを書かない(self) -> None:
        found = logic.build([self.page(), self.page(page=2)])
        self.assertNotIn("が消せます", found.headline)

    def test_飾りを画面に出さない(self) -> None:
        """`**…**` はそのまま字で出ます。"""
        found = logic.build([self.page(), self.page(page=2, synced=True)])
        self.assertNotIn("**", found.headline)
        self.assertNotIn("**", found.note)
        for p in found.pages:
            self.assertNotIn("**", p.note)


@unittest.skipUnless(HAS_FLASK, SKIP)
class ScreenTests(WebTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.post("/api/entry/line", {"line": "L-1"})

    def make_empty(self, **over):
        key = {**KEY, **over}
        self.repo().save(HeaderRecord(**key, worker="山田"),
                         [DetailRecord(**key, row_no=i) for i in range(1, 13)])
        return key

    def make_filled(self, **over):
        key = {**KEY, **over}
        self.repo().save(
            HeaderRecord(**key, worker="佐藤"),
            [DetailRecord(**key, row_no=1, lot="N7131T0", ken="10")])
        return key

    def be_admin(self):
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})

    def test_一覧に出る(self) -> None:
        self.make_empty()
        body = self.get("/api/settings/empty-pages").get_json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["pages"][0]["shift"], "1直")

    def test_打ってある紙は出ない(self) -> None:
        self.make_filled(shift="2直")
        body = self.get("/api/settings/empty-pages").get_json()
        self.assertEqual(body["count"], 0)

    def test_見るだけなら管理者でなくてよい(self) -> None:
        self.assertEqual(self.get("/api/settings/empty-pages").status_code, 200)

    def test_消すのは管理者だけ(self) -> None:
        self.make_empty()
        res = self.post("/api/settings/empty-pages/delete", KEY)
        self.assertEqual(res.status_code, 403)
        self.assertEqual(len(self.repo().list_keys()), 1, "消えています")

    def test_管理者なら消せる(self) -> None:
        self.make_empty()
        self.be_admin()
        res = self.post("/api/settings/empty-pages/delete", KEY)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["removed"])
        self.assertEqual(self.repo().list_keys(), [])
        # 消したあとの一覧も一緒に返る(押したら画面が塗り替わる)
        self.assertEqual(res.get_json()["count"], 0)

    def test_打ってある紙は消せない(self) -> None:
        """**押す瞬間にもう一度確かめます。** 画面の言うことは当てにしない。"""
        key = self.make_filled(shift="2直")
        self.be_admin()
        res = self.post("/api/settings/empty-pages/delete", key)
        self.assertEqual(res.status_code, 422)
        self.assertIn("打ってあるもの", res.get_json()["message"])
        self.assertEqual(len(self.repo().list_keys()), 1)

    def test_共有へ渡した紙は消せない(self) -> None:
        self.make_empty()
        repo = self.repo()
        repo.conn.execute(
            "UPDATE daily_header SET synced_at='2026-09-17 07:10:00', dirty=0 "
            "WHERE report_date=? AND line=? AND shift=? AND page=?",
            (KEY["report_date"], KEY["line"], KEY["shift"], KEY["page"]))
        repo.conn.commit()
        self.be_admin()
        res = self.post("/api/settings/empty-pages/delete", KEY)
        self.assertEqual(res.status_code, 422)
        self.assertIn("共有へ渡してある", res.get_json()["message"])
        self.assertEqual(len(repo.list_keys()), 1)

    def test_どの紙か分からなければ断る(self) -> None:
        self.be_admin()
        res = self.post("/api/settings/empty-pages/delete", {})
        self.assertEqual(res.status_code, 400)

    def test_もう無い紙は無いと言う(self) -> None:
        self.be_admin()
        res = self.post("/api/settings/empty-pages/delete", KEY)
        self.assertEqual(res.status_code, 422)
        self.assertIn("見つかりません", res.get_json()["message"])

    def test_設定画面に置き場所がある(self) -> None:
        html = self.get("/settings").get_data(as_text=True)
        self.assertIn('id="empty-pages-find"', html)
        self.assertIn("中身の無いページ", html)

    def test_消したら未送信からも減る(self) -> None:
        """**ここが本題。** 共有へ未送信に数えられていたものが消えます。"""
        self.make_empty()
        self.assertEqual(len(self.repo().pending_sync_headers()), 1)
        self.be_admin()
        self.post("/api/settings/empty-pages/delete", KEY)
        self.assertEqual(self.repo().pending_sync_headers(), [])


if __name__ == "__main__":
    unittest.main()
