"""取り込み済みの過去日報を直す ── 2直に入った3直の行を3直へ戻す(`services/reshift`)

    2025年12月1日_HVC 2直であるのにかかわらず 22:50 以降があり8行目が余分
    入ってしまっているのはどうしたらいいんですか？

現場のDBそのままの形(12/1 HVC): 2直の8行目「H9461S0 22:50〜23:30 1枚」は
3直の1行目。3直は「23:30〜 ロット空欄(続きの行)」から始まっていた。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.connection import connect
from nippou.db.models import DetailRecord, HeaderRecord
from nippou.db.repository import NippouRepository
from nippou.services import reshift
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase

DAY = "2025年12月1日"


def seed(repo: NippouRepository, *, synced: bool = True, third: bool = True) -> None:
    k2 = dict(report_date=DAY, line="HVC", shift="2直", page=1)
    rows2 = [DetailRecord(**k2, row_no=1, lot="HY021T2", kz="15", kh="0", sz="16", sh="30", con="2"),
             DetailRecord(**k2, row_no=2, lot="H9461S0", kz="21", kh="45", sz="22", sh="50"),
             DetailRecord(**k2, row_no=3, lot="H9461S0", kz="22", kh="50", sz="23", sh="30", con="1")]
    repo.save(HeaderRecord(**k2, worker="山田有希 渡部伸二"), rows2)
    if synced:
        repo.mark_synced((DAY, "HVC", "2直", 1))
    if third:
        k3 = dict(report_date=DAY, line="HVC", shift="3直", page=1)
        repo.save(HeaderRecord(**k3, worker="安藤新緑 河合栄二"),
                  [DetailRecord(**k3, row_no=1, kz="23", kh="30", sz="0", sh="10", con="1")])
        if synced:
            repo.mark_synced((DAY, "HVC", "3直", 1))


class ReshiftTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.conn = connect(self.tmp / "t.sqlite3")
        self.addCleanup(self.conn.close)
        self.repo = NippouRepository(self.conn)

    def rows(self, shift):
        return [(d.row_no, d.lot, d.kz, d.kh) for d in self.repo.load(DAY, "HVC", shift, 1)[1]]

    def test_下見は日と行を出し書かない(self) -> None:
        seed(self.repo)
        fixes = reshift.plan(self.repo)
        self.assertEqual([(f.report_date, f.rows) for f in fixes],
                         [(DAY, ["2直 1ページ 3行目 H9461S0 22:50〜23:30 1枚"])])
        self.assertEqual(len(self.rows("2直")), 3, "下見で書いた")

    def test_3直の頭へ戻す(self) -> None:
        seed(self.repo)
        result = reshift.apply(self.repo, out_dir=self.tmp / "out")
        self.assertEqual((result.days, result.rows), (1, 1), result.message)
        self.assertEqual(self.rows("2直"), [(1, "HY021T2", "15", "0"), (2, "H9461S0", "21", "45")])
        self.assertEqual(self.rows("3直"), [(1, "H9461S0", "22", "50"), (2, "", "23", "30")])
        self.assertEqual(self.repo.load(DAY, "HVC", "3直", 1)[0].worker, "安藤新緑 河合栄二")
        self.assertEqual(reshift.plan(self.repo), [], "2回目も戻すものがある")

    def test_送った印を保つ(self) -> None:
        """共有へ出さない扱いだった古い日報を、直したからといって送り直さない。"""
        seed(self.repo, synced=True)
        reshift.apply(self.repo, out_dir=self.tmp / "out")
        self.assertEqual(self.repo.pending_sync_headers(), [])

    def test_未送信だった直は未送信のまま(self) -> None:
        seed(self.repo, synced=False)
        reshift.apply(self.repo, out_dir=self.tmp / "out")
        self.assertEqual(sorted(h.shift for h in self.repo.pending_sync_headers()), ["2直", "3直"])

    def test_3直が無ければ作る(self) -> None:
        seed(self.repo, third=False)
        reshift.apply(self.repo, out_dir=self.tmp / "out")
        self.assertEqual(self.rows("3直"), [(1, "H9461S0", "22", "50")])

    def test_またいだ行も3直が続きの行から始まるなら戻す(self) -> None:
        """2026/1/10: 2直は全停、3直の人が 22:25 から始めていた。"""
        k2 = dict(report_date=DAY, line="HVC", shift="2直", page=1)
        self.repo.save(HeaderRecord(**k2, worker=""),
                       [DetailRecord(**k2, row_no=1, kz="15", kh="0", sz="22", sh="50", s="2", th="470"),
                        DetailRecord(**k2, row_no=2, lot="HX460Y0", kz="22", kh="25", sz="23", sh="30", con="1")])
        k3 = dict(report_date=DAY, line="HVC", shift="3直", page=1)
        self.repo.save(HeaderRecord(**k3, worker="森川貴志 吉田秀治"),
                       [DetailRecord(**k3, row_no=1, kz="23", kh="30", sz="0", sh="0", con="1")])
        reshift.apply(self.repo, out_dir=self.tmp / "out")
        self.assertEqual(self.rows("2直"), [(1, "", "15", "0")])
        self.assertEqual(self.rows("3直")[0], (1, "HX460Y0", "22", "25"))

    def test_戻す行と同じ写しは消す(self) -> None:
        """2026/1/10 は同じ行が2直に4行入っていた(ファイルでは1行)。"""
        k2 = dict(report_date=DAY, line="HVC", shift="2直", page=1)
        same = dict(lot="HX460Y0", kz="22", kh="25", sz="23", sh="30", con="1")
        self.repo.save(HeaderRecord(**k2, worker=""),
                       [DetailRecord(**k2, row_no=1, kz="15", kh="0", sz="22", sh="50", s="2", th="470")]
                       + [DetailRecord(**k2, row_no=i, **same) for i in (2, 3, 4, 5)])
        k3 = dict(report_date=DAY, line="HVC", shift="3直", page=1)
        self.repo.save(HeaderRecord(**k3, worker="3直"),
                       [DetailRecord(**k3, row_no=1, kz="23", kh="30", sz="0", sh="0", con="1")])
        self.assertEqual(sum("重なり" in r for r in reshift.plan(self.repo)[0].rows), 3)
        reshift.apply(self.repo, out_dir=self.tmp / "out")
        self.assertEqual(self.rows("2直"), [(1, "", "15", "0")])
        self.assertEqual([r[1] for r in self.rows("3直")], ["HX460Y0", ""])

    def test_いまの作業日は触らない(self) -> None:
        seed(self.repo)
        self.assertEqual(reshift.plan(self.repo, skip_dates=(DAY,)), [])

    def test_12行を超えたらページを組み直す(self) -> None:
        seed(self.repo)
        k3 = dict(report_date=DAY, line="HVC", shift="3直", page=1)
        self.repo.save(HeaderRecord(**k3, worker="3直"),
                       [DetailRecord(**k3, row_no=i + 1, kz=str(i), kh="0", sz=str(i), sh="30")
                        for i in range(12)])
        reshift.apply(self.repo, out_dir=self.tmp / "out")
        self.assertEqual(self.repo.saved_pages(DAY, "HVC", "3直"), [1, 2])
        self.assertEqual(self.repo.load(DAY, "HVC", "3直", 2)[1][0].kz, "11")


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class ReshiftRouteTests(WebTestCase):
    def test_管理者モードだけ(self) -> None:
        self.assertEqual(self.post("/api/settings/import/reshift", {}).status_code, 403)

    def test_下見してから戻す(self) -> None:
        seed(self.repo())
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        body = self.post("/api/settings/import/reshift", {}).get_json()
        self.assertEqual((body["days"], body["rows"]), (1, 1), body)
        done = self.post("/api/settings/import/reshift", {"apply": True}).get_json()
        self.assertIn("3直へ戻しました", done["message"])
        self.assertEqual(self.post("/api/settings/import/reshift", {}).get_json()["days"], 0)

    def test_画面にボタンがある(self) -> None:
        html = self.get("/settings").get_data(as_text=True)
        for needle in ('id="reshift-preview"', 'id="reshift-go"', "3直の行を3直へ"):
            self.assertIn(needle, html)


if __name__ == "__main__":
    unittest.main()
