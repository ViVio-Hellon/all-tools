"""直したあとの集計と集計CSV (v4.46.3)

    管理者モードで 記録を見る → ほかのPCの日報(控え) → 呼び出して直す で前日分を修正した。
    印刷は該当ファイル更新をしていたが、集計ファイルは更新していない
    修正分の集計や集計CSVは扱いを失敗すると大惨事なので確実な精査をしてください

直す道ごとに揃えることがばらばらでした。どの道でも:

  - その直の集計を作り直す(ページが無くなった直は集計を消す)
  - その日の集計CSV(出力先の3本)を、**その日の直ぜんぶで**書き直す
  - **ほかのラインの日は書かない**(手元にその日の直が全部はない。一部の中身で上書きすると、
    そのラインの端末が書いた直が消える)。そのラインの端末が直しを取り込んだときに書き直す
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import WebTestCase  # noqa: E402


class CorrectionCase(WebTestCase):
    def setUp(self) -> None:
        super().setUp()
        from nippou import config, user_settings

        self.out = self.tmp / "出力先"
        self.out.mkdir()
        user_settings.save_many({config.KEY_REPORT_OUT_DIR: str(self.out)})
        self.post("/api/entry/line", {"line": "L-1"})
        self.yesterday = self.day(-1)

    def repo(self):
        from nippou.config import SETTINGS
        from nippou.db.connection import connect
        from nippou.db.repository import NippouRepository
        conn = connect(SETTINGS.sqlite_path)
        self.addCleanup(conn.close)
        return NippouRepository(conn)

    def reports(self, line: str) -> list:
        from nippou.logic.shift import parse_business_date

        day = parse_business_date(self.yesterday)
        return self.repo().packing_reports_between(day, day, line)

    def day(self, offset: int) -> str:
        from app.routes.entry import build_shift_calculator
        from nippou.services.nippou_service import format_business_date
        calc = build_shift_calculator(self.repo().get_shift_times())
        return format_business_date(calc.today_check(datetime.now(), False)
                                    + timedelta(days=offset))

    def admin(self) -> None:
        from nippou.config import SETTINGS

        self.post("/api/settings/admin", {"enable": True, "password": SETTINGS.admin_password})

    def put(self, line: str, shift: str, lot: str, *, day: str = "") -> None:
        """ページを1枚、手元に直に置く(ほかの PC が書いた直の代わり)。"""
        from nippou.db.models import DetailRecord, HeaderRecord

        key = dict(report_date=day or self.yesterday, line=line, shift=shift, page=1)
        self.repo().save(HeaderRecord(**key, worker="山田"),
                         [DetailRecord(**key, row_no=1, lot=lot, ken="10", con="10",
                                       kz="8", kh="00", sz="9", sh="00")])

    def csv_text(self, line: str, day: str = "") -> str:
        """その日・そのラインの集計CSV 3本をつないだ字(無ければ空)。"""
        from nippou.reporting import csv_export

        paths = csv_export.daily_output_paths(self.out, day or self.yesterday, line)
        return "".join(p.read_text(encoding="utf-8-sig") for p in paths.values() if p.exists())

    def recall(self, line: str, shift: str) -> None:
        res = self.post("/api/settings/recall", {"report_date": self.yesterday, "line": line,
                                                 "shift": shift, "page": 1})
        self.assertEqual(res.status_code, 200, res.get_json())

    def edit(self, lot: str, *, draft: bool = False) -> dict:
        """`draft` は画面を移る・閉じる前に1度だけ送る打ちかけ(自動保存の間引きに当たらない)。"""
        body = {"rows": {"1": {"LOT": lot, "KEN": "10", "CON": "10",
                               "KZ": "08", "KH": "00", "SZ": "09", "SH": "00"}},
                "header": {"worker": "山田"}, "checks": {}}
        if draft:
            body["draft"] = True
        res = self.post("/api/entry/save", body)
        self.assertEqual(res.status_code, 200, res.get_json())
        return res.get_json()


class RecallTests(CorrectionCase):
    """記録を見るで呼び出して直す。"""

    def test_自動保存だけで呼び出しをやめても_集計CSVは直した中身になる(self) -> None:
        """「保存(確定)」を押さずに「呼び出しをやめて、いまの直に戻る」(行を送らない)で抜けた。"""
        self.put("L-1", "1直", "OLD0001")
        self.put("L-1", "2直", "KEEP002")
        self.admin()
        self.recall("L-1", "1直")
        self.edit("NEW0001", draft=True)           # 打ちかけ(押していない保存)
        text = self.csv_text("L-1")
        self.assertIn("NEW0001", text, "呼び出した過去の直は、押していない保存でも集計CSVを書き直す")
        self.assertNotIn("OLD0001", text)
        self.assertIn("KEEP002", text, "その日のほかの直も入ったまま(直ぜんぶで書く)")
        res = self.post("/api/settings/back", {})
        self.assertEqual(res.status_code, 200)
        self.assertIn("NEW0001", self.csv_text("L-1"))

    def test_戻るだけでも直した直の集計とCSVを揃える(self) -> None:
        """前に作ったCSVが古いまま残っていても、抜けるときに直ぜんぶで書き直す。"""
        self.put("L-1", "1直", "OLD0001")
        self.admin()
        self.recall("L-1", "1直")
        # 画面を通さずに直した(取り込み・別の窓など)── 抜けるときに揃うか
        self.put("L-1", "1直", "NEW0002")
        body = self.post("/api/settings/back", {}).get_json()
        self.assertIn("NEW0002", self.csv_text("L-1"))
        self.assertTrue(any("書き直しました" in n for n in body["settled"]), body)
        self.assertEqual(len(self.reports("L-1")), 1)

    def test_ほかのラインの日の集計CSVは書かない_そのラインの端末が書いたものを消さない(self) -> None:
        """管理者のPCの手元にあるほかのラインの前日は、呼び出した直だけ。"""
        from nippou.reporting import csv_export

        self.put("HVC", "2直", "HVC0002")
        theirs = csv_export.daily_output_paths(self.out, self.yesterday, "HVC")
        for path in theirs.values():               # HVC の端末が書いた、1直〜3直ぜんぶの3本
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("HVCの端末が書いた中身(1直・2直・3直)", encoding="utf-8")
        self.admin()
        self.recall("HVC", "2直")
        body = self.edit("HVC9999")
        self.assertFalse(body["export"]["ok"])
        self.assertIn("HVC の集計CSVは、HVC のPCが自動で更新します", body["export"]["message"])
        self.edit("HVC9998", draft=True)
        back = self.post("/api/settings/back", {}).get_json()
        self.assertTrue(any("のPCが自動で更新します" in n for n in back["settled"]), back)
        for path in theirs.values():
            self.assertEqual(path.read_text(encoding="utf-8"), "HVCの端末が書いた中身(1直・2直・3直)",
                             f"{path.name} をこの端末の一部の中身で上書きした")
        # 直したページ自体は保存されている(送る待ち)
        _, details = self.repo().load(self.yesterday, "HVC", "2直", 1)
        self.assertEqual(details[0].lot, "HVC9998")


class RecalledPushTests(CorrectionCase):
    """呼出中の「直した直を共有へ送る」(v4.46.5) ──「共有保存ができませんけどいいんですか？」"""

    def share_db(self):
        from nippou import config, user_settings

        shared = self.tmp / "共有"
        shared.mkdir(exist_ok=True)
        user_settings.save_many({config.KEY_ACCESS_DIR: str(shared)})
        return shared / "日報データ.sqlite3"

    def test_呼び出している直だけを共有へ送れる_ほかの直は送らない(self) -> None:
        import sqlite3

        db = self.share_db()
        self.put("HVC", "2直", "HVC0002")
        self.put("L-1", "3直", "OTHER03")                  # 呼び出していない未送信の直
        self.admin()
        self.recall("HVC", "2直")
        html = self.get("/").get_data(as_text=True)
        self.assertIn("直した直を共有へ送る", html)
        self.edit("HVC9999")
        res = self.post("/api/settings/push", {"scope": "recalled"})
        # 直の途中までの試しの中身なので、最終時間と休憩で断られる(関門は同じ)。管理者は
        # 「設備移動のため保存」で通せる
        self.assertEqual(res.status_code, 422)
        res = self.post("/api/settings/push", {"scope": "recalled", "skip": "設備移動のため保存"})
        self.assertEqual(res.status_code, 200, res.get_json())
        body = res.get_json()
        self.assertIn("HVC の集計CSVは、HVC のPCが自動で更新します", body["message"])
        conn = sqlite3.connect(db)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            got = conn.execute('SELECT "直" FROM "T_日報明細_HVC"').fetchall()
        finally:
            conn.close()
        self.assertEqual({r[0] for r in got}, {"2直"})
        self.assertNotIn("T_日報明細_L-1", tables, "呼び出していない直は送らない")
        # もう送ってある
        again = self.post("/api/settings/push", {"scope": "recalled",
                                                 "skip": "設備移動のため保存"}).get_json()
        self.assertIn("共有へ送ってあります", again["message"])

    def test_ふだんの共有へ保存は呼出中は今までどおり断る(self) -> None:
        self.put("HVC", "2直", "HVC0002")
        self.admin()
        self.recall("HVC", "2直")
        res = self.post("/api/settings/push", {"scope": "current"})
        self.assertEqual(res.status_code, 422)


class SettleTests(CorrectionCase):
    """直す道が通る `day_csv.settle` と、集計の作り直し。"""

    def test_ページが無くなった直の集計は消す(self) -> None:
        from nippou.services import summary

        self.put("L-1", "2直", "GONE002")
        repo = self.repo()
        summary.refresh_shift(repo, self.yesterday, "L-1", "2直")
        self.assertEqual(len(self.reports("L-1")), 1)
        with repo.conn:
            repo.conn.execute("DELETE FROM daily_detail")
            repo.conn.execute("DELETE FROM daily_header")
        summary.refresh_shift(repo, self.yesterday, "L-1", "2直")
        self.assertEqual(self.reports("L-1"), [],
                         "ページの無い直の集計が残ると、グラフ・CSV・月の書き出しに数えられ続ける")

    def test_控えから直しを取り込んだ自分のラインの日は集計CSVを書き直す(self) -> None:
        from nippou.services import local_backup

        self.put("L-1", "1直", "PULL001")
        local_backup._settle_pulled(self.repo(), {(self.yesterday, "L-1", "1直")})
        self.assertIn("PULL001", self.csv_text("L-1"))

    def test_控えから取り込んでもほかのラインの日は書かない(self) -> None:
        from nippou.services import local_backup

        self.put("HVC", "1直", "PULL009")
        local_backup._settle_pulled(self.repo(), {(self.yesterday, "HVC", "1直")})
        self.assertEqual(self.csv_text("HVC"), "")

    def test_直を消すと集計CSVからも消える(self) -> None:
        from nippou.services import shift_removal

        self.put("L-1", "1直", "STAY001")
        self.put("L-1", "3直", "DROP003")
        repo = self.repo()
        shift_removal._settle_shifts(repo, {(self.yesterday, "L-1", "3直")})
        self.assertIn("DROP003", self.csv_text("L-1"))
        with repo.conn:
            repo.conn.execute("DELETE FROM daily_detail WHERE shift='3直'")
            repo.conn.execute("DELETE FROM daily_header WHERE shift='3直'")
        shift_removal._settle_shifts(repo, {(self.yesterday, "L-1", "3直")})
        text = self.csv_text("L-1")
        self.assertNotIn("DROP003", text)
        self.assertIn("STAY001", text)

    def test_2つ目の出力先へもほかのラインの日は出さない(self) -> None:
        from nippou import config, user_settings
        from nippou.services import second_output

        second = self.tmp / "2つ目"
        second.mkdir()
        user_settings.save_many({config.KEY_REPORT_OUT_DIR2: str(second)})
        self.put("HVC", "1直", "SEND001")
        self.put("L-1", "1直", "SEND002")
        repo = self.repo()
        for line in ("HVC", "L-1"):
            repo.mark_synced((self.yesterday, line, "1直", 1))
        out = second_output.after_push(repo, [(self.yesterday, "HVC", "1直", 1),
                                              (self.yesterday, "L-1", "1直", 1)])
        self.assertEqual(out.others, [f"{self.yesterday} HVC"])
        self.assertIn("そのラインのPCが自動で更新します", out.text("集計CSV"))
        self.assertFalse(list((second / "HVC").rglob("*.csv")))
        self.assertTrue(list((second / "L-1").rglob("*.csv")))


class AllStopTests(CorrectionCase):
    def test_全停入力でも集計と集計CSVを作り直す(self) -> None:
        from nippou.services import day_csv

        self.put("L-1", "1直", "BEFORE1")
        notes = day_csv.settle(self.repo(), {(self.yesterday, "L-1", "1直")})
        self.assertTrue(any("書き直しました" in n for n in notes), notes)
        self.assertIn("BEFORE1", self.csv_text("L-1"))


if __name__ == "__main__":
    unittest.main()
