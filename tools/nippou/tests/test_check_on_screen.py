"""「この直をチェック」と帯は、画面のページを**打ったとおりの中身**で見る (v4.18.0)

    * 直の終わりまでに 0分しか休憩の入力がありません。60分必要です
    入れてるんだけどずっと出てますね, 壊れてますね(写真: 0 休憩食事 / 60)

【何が起きていたか】
確かめは**保存済みの中身だけ**を読んでいました。打った休憩は「保存(確定)」を
押すまで届かず、過去データ(他の直)を開いているあいだは自動保存もしないので、
いつまでも「0分」のままでした。

【約束】
    ・画面に出ているページは画面の中身で、ほかのページは保存済みの中身で見る
    ・まだ保存していない中身で見たときは、結果にそう添える(共有へ出るのは保存してから)
    ・画面が別の直のページを開いたままなら、画面の中身は混ぜない
    ・共有へ保存・直の確定は、これまでどおり保存済みの中身だけ
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.models import DetailRecord, HeaderRecord  # noqa: E402
from nippou.logic import save_checks  # noqa: E402
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402

WORKER = "青木 石井"            # 架空の名前
WORK = {"LOT": "A123450", "KEN": "100", "MAI": "10", "TUT": "1",
        "KZ": "08", "KH": "00", "SZ": "09", "SH": "00"}
REST = {"KZ": "09", "KH": "00", "SZ": "10", "SH": "00", "S": "0", "TH": "60"}


def codes(body) -> set[str]:
    return {f["code"] for f in body["findings"]}


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class VerifyTests(WebTestCase):
    def setUp(self) -> None:
        super().setUp()
        res = self.post("/api/entry/save", {"rows": {"1": WORK}, "header": {"worker": WORKER}})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.saved = res.get_json()          # 画面に描かれる中身(計算した欄も入る)

    def verify(self, rows=None, **extra):
        payload = {"header": {"worker": WORKER}, **extra}
        if rows is not None:
            payload["rows"] = rows
        return self.post("/api/entry/verify", payload).get_json()

    def test_打った休憩を数える(self) -> None:
        body = self.verify({"1": WORK, "2": REST})
        self.assertNotIn(save_checks.SHORT_BREAK, codes(body))
        self.assertTrue(body["unsaved"])
        self.assertIn("まだ保存していない中身", body["message"])
        self.assertIn("「保存(確定)」を押すまで、共有へは出ません", body["message"])

    def test_画面を送らなければ保存済みだけ(self) -> None:
        body = self.verify()
        self.assertIn(save_checks.SHORT_BREAK, codes(body))
        self.assertFalse(body["unsaved"])
        self.assertNotIn("まだ保存していない", body["message"])

    def test_保存済みと同じなら_未保存とは言わない(self) -> None:
        body = self.post("/api/entry/verify", {"rows": self.saved["rows"],
                                               "header": self.saved["header"]}).get_json()
        self.assertFalse(body["unsaved"])
        self.assertIn(save_checks.SHORT_BREAK, codes(body))

    def test_保存すれば保存済みだけでも通る(self) -> None:
        res = self.post("/api/entry/save", {"rows": {"1": WORK, "2": REST},
                                            "header": {"worker": WORKER}})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertNotIn(save_checks.SHORT_BREAK, codes(self.verify()))

    def test_別の直を開いたままの画面は混ぜない(self) -> None:
        body = self.verify({"1": WORK, "2": REST},
                           opened={"report_date": "2000年1月1日", "shift": "1直", "page": 1})
        self.assertIn(save_checks.SHORT_BREAK, codes(body))
        self.assertFalse(body["unsaved"])

    def test_帯も画面の中身で見る(self) -> None:
        body = self.post("/api/entry/state", {"rows": {"1": WORK, "2": REST},
                                              "header": {"worker": WORKER}}).get_json()
        found = body["shift_findings"]
        self.assertTrue(found["counted"])
        self.assertNotIn(save_checks.SHORT_BREAK,
                         [f["code"] for f in found["now"] + found["at_end"]])

    def test_空の画面なら保存済みで見る(self) -> None:
        """中身の無い画面で、保存済みのページを消したことにしない。"""
        body = self.verify({})
        self.assertIn(save_checks.SHORT_BREAK, codes(body))
        self.assertFalse(body["unsaved"])
        self.assertEqual(body["rows"], 12)

    def test_画面はチェックに中身を送る(self) -> None:
        js = (Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "views"
              / "entry.js").read_text(encoding="utf-8")
        self.assertIn('api.post("/api/entry/verify", collect())', js)


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class CoefficientLineTests(WebTestCase):
    """機側・NS1 は保存のときに係数処理ﾛｯﾄ数(画面に無い欄)が入る。それでも
    保存した直後に「まだ保存していない」とは言わない。"""

    terminal_line = "機側"

    def test_保存した直後は未保存と言わない(self) -> None:
        saved = self.post("/api/entry/save", {"rows": {"1": WORK},
                                              "header": {"worker": WORKER}}).get_json()
        _, rows = self.repo().load(*self.repo().list_keys()[0])
        self.assertEqual(rows[0].keisu, "1.0")            # 画面には無い欄
        body = self.post("/api/entry/verify", {"rows": saved["rows"],
                                               "header": saved["header"]}).get_json()
        self.assertIn("機側", body["message"])
        self.assertFalse(body["unsaved"])


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class ServiceTests(WebTestCase):
    """`shift_check.run(screen=…)`。**他の直(過去データ)でも同じ**に効く。"""

    KEY = ("2026年10月5日", "AIM", "1直")

    def setUp(self) -> None:
        super().setUp()
        from nippou.config import SETTINGS
        from nippou.db.connection import connect
        from nippou.db.repository import NippouRepository

        self.repo = NippouRepository(connect(SETTINGS.sqlite_path))
        self.addCleanup(self.repo.conn.close)   # 開いたままでは、Windows で一時フォルダを消せない
        day, line, shift = self.KEY
        self.header = HeaderRecord(report_date=day, line=line, shift=shift, page=1,
                                   worker=WORKER)
        self.work = DetailRecord(report_date=day, line=line, shift=shift, page=1, row_no=1,
                                 lot="A123450", ken="100", mai="10", tut="1",
                                 kz="08", kh="00", sz="09", sh="00")
        self.repo.save(self.header, [self.work])
        self.repo.conn.commit()

    def run_check(self, screen=None):
        from nippou.services import shift_check

        return shift_check.run(self.repo, *self.KEY, codes=[], screen=screen)

    def test_画面のページで差し替える(self) -> None:
        from nippou.services import shift_check

        day, line, shift = self.KEY
        rest = DetailRecord(report_date=day, line=line, shift=shift, page=1, row_no=2,
                            kz="09", kh="00", sz="10", sh="00", s="０", th="60")   # 全角でも
        before = self.run_check()
        self.assertIn(save_checks.SHORT_BREAK, {f.code for f in before.findings})
        after = self.run_check(shift_check.ScreenPage(page=1, header=self.header,
                                                      details=[self.work, rest]))
        self.assertNotIn(save_checks.SHORT_BREAK, {f.code for f in after.findings})
        self.assertTrue(after.unsaved)
        self.assertEqual(after.rows, 2)                   # 保存済みのページ1は読まない(二重に数えない)

    def test_共有へ保存の確かめは保存済みだけ(self) -> None:
        """`run_pending`(共有へ保存)は画面を渡さない ── 出ていくのは保存済みの中身。"""
        import inspect

        from nippou.services import shift_check

        self.assertNotIn("screen", inspect.signature(shift_check.run_pending).parameters)
        self.assertNotIn("screen=", inspect.getsource(shift_check.run_pending))


if __name__ == "__main__":
    unittest.main()
