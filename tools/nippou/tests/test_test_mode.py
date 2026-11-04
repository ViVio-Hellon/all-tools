"""テストモード ── 試しに打った直を「テストデータ」の印付きで残す

    テストモード: パスワード認証(管理者パスワード)で入る。テストモードのあいだに作った直は
    「テストデータ」の印付きで保存し、共有の日報データへも印付きで送る

確かめること:

    - 入るには管理者パスワードが要る・抜けるのは要らない・入り切りが出来事の記録に残る
    - 起動し直したら切れている(持ち越さない)
    - 印は直の単位: テストモードで作った直は印付き、前からある直は普通のまま、
      抜けたあとに同じ直へ打っても印は落ちない(2ページ目・保存し直し・係数の書き戻し)
    - テストの直は集計・グラフ・CSV・月の書き出し・内訳番号99・紙の頭の集計・標準作業時間に入らない
    - 共有の日報データへは「テスト」の列に 1 を付けて送る(列は後ろに足す。テストの直が
      無ければ足さない)。書き先が Access で列が無ければ、テストのページは送らない
    - 画面: 帯が出る・記録を見るの一覧に印
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402

DAY = "2026年8月3日"
DAY_ISO = date(2026, 8, 3)


def rows(key: dict, n: int = 2, *, lot: str = "A") -> list:
    from nippou.db.models import DetailRecord

    return [DetailRecord(**key, row_no=i, lot=f"{lot}{i}", ken="10", con="10", wei="12.5",
                         kz="8", kh="00", sz="9", sh="00") for i in range(1, n + 1)]


def key_of(shift: str = "1直", page: int = 1, day: str = DAY, line: str = "L-1") -> dict:
    return dict(report_date=day, line=line, shift=shift, page=page)


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class TestModeCase(WebTestCase):
    def password(self) -> str:
        from nippou.config import SETTINGS

        return SETTINGS.admin_password

    def enter(self):
        res = self.post("/api/test-mode", {"enable": True, "password": self.password()})
        self.assertEqual(res.status_code, 200, res.get_json())
        return res

    def leave(self):
        res = self.post("/api/test-mode", {"enable": False})
        self.assertEqual(res.status_code, 200, res.get_json())
        return res

    def save(self, key: dict, *, worker: str = "山田", n: int = 2, lot: str = "A"):
        from nippou.db.models import HeaderRecord

        self.repo().save(HeaderRecord(**key, worker=worker), rows(key, n, lot=lot))

    def mark(self, key: dict) -> int:
        row = self.repo().conn.execute(
            "SELECT test_mark FROM daily_header WHERE report_date=? AND line=? AND shift=? AND page=?",
            (key["report_date"], key["line"], key["shift"], key["page"])).fetchone()
        return int(row[0])


class SwitchTests(TestModeCase):
    def test_入るには管理者パスワードが要る(self) -> None:
        res = self.post("/api/test-mode", {"enable": True, "password": "ちがう"})
        self.assertEqual(res.status_code, 403)
        self.assertFalse(self.get("/api/test-mode").get_json()["active"])
        self.enter()
        body = self.get("/api/test-mode").get_json()
        self.assertTrue(body["active"])
        self.assertIn("テストモード(この間に保存した直はテストデータです)", body["band"])

    def test_抜けるのにパスワードは要らない(self) -> None:
        self.enter()
        self.leave()
        self.assertFalse(self.get("/api/test-mode").get_json()["active"])

    def test_入り切りが出来事の記録に残る(self) -> None:
        from nippou.services import event_log

        self.enter()
        self.leave()
        event_log.flush(5.0)
        found = [r for r in event_log.load(date.today(), date.today())
                 if r.get("action") in ("test_mode_on", "test_mode_off")]
        self.assertEqual([r["action"] for r in found], ["test_mode_on", "test_mode_off"])
        # 誰が(PC名・ログインID)
        self.assertIn("PC ", found[0]["where"])
        self.assertIn("ログインID", found[0]["where"])

    def test_起動し直したら切れている(self) -> None:
        """状態はプロセスの中だけ。設定ファイルにも作業用DBにも残さない。"""
        self.enter()
        from nippou import config, user_settings

        raw = config.USER_CONFIG_PATH.read_text(encoding="utf-8") if config.USER_CONFIG_PATH.exists() else ""
        self.assertNotIn("test_mode", raw)
        # 起動し直し = モジュールを読み直してアプリを作り直す
        for name in [m for m in sys.modules if m.startswith(("nippou", "app"))]:
            del sys.modules[name]
        from app import create_app
        from tests._web import TOKEN

        again = create_app(token=TOKEN, port=8733)
        again.config["READY"] = True
        self.client = again.test_client()
        self.assertFalse(self.get("/api/test-mode").get_json()["active"])
        del user_settings

    def test_管理者モードの直の変わり目では切れない(self) -> None:
        """管理者モードは直の変わり目で落ちるが、テストモードは抜けるか起動し直すまで続く。"""
        from nippou import test_mode, work_context

        self.enter()
        work_context.get_context().admin = True
        work_context.get_context().seen_key = ("L-1", DAY, "1直")
        self.assertTrue(test_mode.is_active())


class MarkTests(TestModeCase):
    def test_テストモードで作った直に印が付く(self) -> None:
        self.enter()
        self.save(key_of("1直"))
        self.assertEqual(self.mark(key_of("1直")), 1)
        self.assertTrue(self.repo().is_test_shift(DAY, "L-1", "1直"))

    def test_テストモードに入る前からある直は普通のまま(self) -> None:
        self.save(key_of("1直"))
        self.enter()
        self.save(key_of("1直"), lot="B")                       # 同じページを直す
        self.save(key_of("1直", page=2))                        # 同じ直の2ページ目
        self.assertEqual(self.mark(key_of("1直")), 0)
        self.assertEqual(self.mark(key_of("1直", page=2)), 0)

    def test_抜けたあとも同じ直の印は落ちない(self) -> None:
        self.enter()
        self.save(key_of("2直"))
        self.leave()
        self.save(key_of("2直"), lot="C")                       # 自動保存・確定で保存し直す
        self.save(key_of("2直", page=2))                        # 直の中の新しいページ
        self.assertEqual(self.mark(key_of("2直")), 1)
        self.assertEqual(self.mark(key_of("2直", page=2)), 1)
        # 抜けたあとに作る直は普通の直
        self.save(key_of("3直"))
        self.assertEqual(self.mark(key_of("3直")), 0)

    def test_画面の保存の道でも印が付く(self) -> None:
        """日報入力の保存(`/api/entry/save` → `page_writer` → `repository.save`)を通しても付く。"""
        from nippou.services import page_writer
        from nippou.db.models import HeaderRecord

        self.enter()
        key = key_of("1直", day="2026年8月4日")
        page_writer.save_page(self.repo(), HeaderRecord(**key, worker="山田"), rows(key),
                              by_screen=True)
        self.assertEqual(self.mark(key), 1)

    def test_読んだ見出しにも印がある(self) -> None:
        self.enter()
        self.save(key_of("1直"))
        header, _ = self.repo().load(DAY, "L-1", "1直", 1)
        self.assertTrue(header.test)
        self.assertEqual(self.repo().test_shift_keys("L-1"), {(DAY, "L-1", "1直")})

    def test_控えへ写しても印が残る(self) -> None:
        """控え(LocalBackup)は手元と同じ形。印の列も写る(別のPCが戻しても印付き)。"""
        from nippou import config, user_settings
        from nippou.services import local_backup as lb

        share = self.tmp / "共有"
        share.mkdir()
        user_settings.save_many({config.KEY_ACCESS_DIR: str(share)})
        lb.reset()
        self.addCleanup(lb.reset)
        self.enter()
        self.save(key_of("1直"))
        repo = self.repo()
        lb.flush(repo, force=True)
        with lb.reader("L-1") as backup:
            got = backup.conn.execute("SELECT test_mark FROM daily_header").fetchone()[0]
        self.assertEqual(got, 1)


class ExcludedTests(TestModeCase):
    """**テストの直は数えない。** 本番の直と同じ日に並べて、本番の分だけが出ることを見る。"""

    def setUp(self) -> None:
        super().setUp()
        self.save(key_of("1直"), n=2)                           # 本番 20枚
        self.enter()
        self.save(key_of("2直"), n=3, lot="T")                  # テスト 30枚
        self.save(key_of("2直", day="2026年9月1日"), n=1, lot="U")   # テストだけの月
        self.leave()

    def test_集計を作らない(self) -> None:
        from nippou.services import summary

        repo = self.repo()
        summary.refresh_shift(repo, DAY, "L-1", "2直")
        self.assertIsNone(repo.load_packing_report(DAY, "L-1", "2直"))
        rows_ = summary.day_rows(repo, DAY, "L-1")
        self.assertEqual([r.shift for r in rows_], ["1直"])
        self.assertEqual(rows_[0].sheet_count, 20)

    def test_期間のグラフと停止とロット行に入らない(self) -> None:
        from nippou.services import summary

        repo = self.repo()
        start, end = DAY_ISO, date(2026, 9, 30)
        self.assertEqual(sum(r.sheet_count for r in summary.period_rows(repo, start, end)), 20)
        self.assertEqual({r.lot_no for r in summary.lot_rows(repo, start, end)}, {"A1", "A2"})
        self.assertEqual(summary.rebuild(repo, start, end).shifts, 1)

    def test_グラフの画面にも出ない(self) -> None:
        body = self.post("/api/graph/history", {"start": "2026-08-01", "end": "2026-08-31"})
        self.assertEqual(body.status_code, 200, body.get_data(as_text=True)[:300])
        self.assertNotIn("T1", body.get_data(as_text=True))

    def test_集計CSVに入らない(self) -> None:
        from nippou.reporting import csv_export

        repo = self.repo()
        shifts = [r.shift for r in csv_export.build_daily_aggregate_rows(repo, DAY, "L-1")]
        self.assertEqual(shifts, ["1直"])
        lots = repr(csv_export.build_detail_rows(repo, DAY, "L-1"))
        self.assertIn("A1", lots)
        self.assertNotIn("T1", lots)

    def test_月の書き出しに入らない(self) -> None:
        repo = self.repo()
        self.assertEqual(repo.month_keys(), [(2026, 8)], "テストだけの月を書き出そうとしています")
        found = repo.month_headers(2026, 8)
        self.assertEqual({h.shift for h, _ in found}, {"1直"})
        before = repo.month_fingerprints()
        self.save(key_of("2直"), n=3, lot="X")                  # テストの直を直しても
        self.assertEqual(repo.month_fingerprints(), before, "月の書き出しをやり直そうとしています")
        self.assertFalse(repo.month_has_pending(2026, 9))

    def test_月の書き出しのファイルに入らない(self) -> None:
        from nippou.reporting import month_export

        made = month_export.write_month(self.repo(), year=2026, month=8, line="L-1",
                                        base_dir=self.tmp / "月")
        self.assertEqual(made.shifts, 1)
        text = "".join(p.read_text(encoding="utf-8-sig") for p in made.files)
        self.assertIn("A1", text)
        self.assertNotIn("T1", text)

    def test_内訳番号99の時間に入らない(self) -> None:
        repo = self.repo()
        found = repo.list_headers_between(DAY_ISO, DAY_ISO, line="L-1")
        self.assertEqual({h.shift for h, _ in found}, {"1直"})
        self.assertEqual(len(repo.list_headers_between(DAY_ISO, DAY_ISO, line="L-1",
                                                        include_test=True)), 2)

    def test_係数の前の直に数えない(self) -> None:
        from nippou.services import load_factor

        pages = load_factor.previous_pages(self.repo(), DAY, "L-1", "3直")
        self.assertEqual([[d.lot for d in p] for p in pages], [["A1", "A2"]])

    def test_紙にテストの印が付き出力先へは残さない(self) -> None:
        from nippou.services import paper_files

        repo = self.repo()
        stamps = paper_files.backfill_stamps(repo, DAY, "L-1", "2直")
        self.assertIn("テストデータ", stamps[1])
        self.assertEqual(paper_files.backfill_stamps(repo, DAY, "L-1", "1直"), {})
        got = paper_files.save_shift(repo, DAY, "L-1", "2直")
        self.assertEqual(got["files"], [])
        self.assertIn("テストの直", paper_files.saved_note(got))
        self.assertEqual(paper_files.aggregate_of(repo, DAY, "L-1", "2直"), {})

    def test_紙の頭の本日計に入らない(self) -> None:
        from nippou.services import paper_files

        got = paper_files.aggregate_of(self.repo(), DAY, "L-1", "1直")
        self.assertEqual(got["summary"].total_quantity, 20)


class SharedMarkTests(TestModeCase):
    """共有の日報データ(sqlite3)へは「テスト」の列に印を付けて送る。"""

    def push(self) -> Path:
        from nippou.access_bridge import pusher
        from nippou.services import summary as summaries

        shared = self.tmp / "共有" / "日報データ.sqlite3"
        shared.parent.mkdir(exist_ok=True)
        repo = self.repo()
        summaries.fill_missing(repo, date(2026, 1, 1), date(2026, 12, 31))   # 保存の道が作る集計
        summary = pusher.push_pending(repo, shared)
        self.assertEqual(summary.failed, [])
        return shared

    def columns(self, shared: Path, table: str) -> list[str]:
        conn = sqlite3.connect(str(shared))
        try:
            return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
        finally:
            conn.close()

    def test_テストの直が無ければ列を足さない(self) -> None:
        self.save(key_of("1直"))
        shared = self.push()
        self.assertNotIn("テスト", self.columns(shared, "T_日報ヘッダー_L-1"))

    def test_テストの直は印付きで送り普通の直は空(self) -> None:
        self.save(key_of("1直"))
        self.push()                                             # 列の無い表が先にある
        self.enter()
        self.save(key_of("2直"))
        shared = self.push()
        cols = self.columns(shared, "T_日報ヘッダー_L-1")
        self.assertEqual(cols[-1], "テスト", "列は後ろに足す")
        self.assertEqual(cols[:12], ["報告日", "ライン", "直", "頁", "担当者", "昼勤", "枚数",
                                     "重量Kg", "Lot数", "係数Lot数", "理由", "保存日時"],
                         "前からある列の並びを変えています")
        conn = sqlite3.connect(str(shared))
        try:
            got = dict(conn.execute('SELECT "直", "テスト" FROM "T_日報ヘッダー_L-1"').fetchall())
            summaries = [r[0] for r in conn.execute('SELECT "直" FROM "T_日報集計_L-1"')]
        finally:
            conn.close()
        self.assertEqual(got, {"1直": None, "2直": "1"})
        self.assertEqual(summaries, ["1直"], "テストの直の集計を共有へ出しています")

    def test_共有から読んだ行にも印(self) -> None:
        from nippou.services import shared_reports

        self.save(key_of("1直"))
        self.enter()
        self.save(key_of("2直"))
        shared = self.push()
        test = shared_reports.shift_pages(DAY, "L-1", "2直", path=shared)
        plain = shared_reports.shift_pages(DAY, "L-1", "1直", path=shared)
        self.assertTrue(test.pages and test.pages[0][0].test, test.status)
        self.assertFalse(plain.pages[0][0].test)

    def test_Accessで列が無ければテストのページは送らない(self) -> None:
        from nippou.access_bridge import pusher

        self.enter()
        self.save(key_of("2直"))
        repo = self.repo()
        header = repo.pending_sync_headers()[0]
        summary = pusher.PushSummary()
        vba = pusher.AccessTable(("報告日", "ライン", "直", "頁", "担当者"))
        with patch.object(pusher, "access_table", return_value=vba), \
                patch.object(pusher, "access_unread", return_value=""):
            pusher._push_one(repo, Path("日報データ.accdb"), header, None, summary)
        self.assertEqual(summary.succeeded, [])
        self.assertIn("テストの印の列", summary.failed[0].error.message)
        self.assertTrue(repo.pending_sync_headers(), "送っていないのに送り済みにしています")

    def test_Accessで列があれば印を書く(self) -> None:
        from nippou.access_bridge import pusher
        from nippou.db.models import HeaderRecord

        table = pusher.AccessTable(("報告日", "ライン", "直", "頁", "担当者", "テスト"))
        sql = pusher.build_header_statements(HeaderRecord(**key_of(), test=True), table)[1]
        self.assertIn("[テスト]", sql)
        self.assertIn("'1'", sql)
        plain = pusher.build_header_statements(HeaderRecord(**key_of()), table)[1]
        self.assertIn("NULL", plain)
        vba = pusher.build_header_statements(HeaderRecord(**key_of(), test=True))[1]
        self.assertNotIn("テスト", vba, "列を見られなかった表へ印を書いています")


class ScreenTests(TestModeCase):
    def test_テストモード中はどの画面にも帯が出る(self) -> None:
        for path in ("/", "/settings", "/records", "/graph"):
            with self.subTest(path=path):
                self.assertNotIn('id="test-band"', self.get(path).get_data(as_text=True))
        self.enter()
        for path in ("/", "/settings", "/records", "/graph"):
            with self.subTest(path=path):
                html = self.get(path).get_data(as_text=True)
                self.assertIn('id="test-band"', html)
                self.assertIn("テストモード(この間に保存した直はテストデータです)", html)
                self.assertIn('id="test-band-leave"', html)

    def test_記録を見るの一覧に印(self) -> None:
        self.save(key_of("1直"))
        self.enter()
        self.save(key_of("2直"))
        html = self.get("/records").get_data(as_text=True)
        self.assertEqual(html.count("data-test-mark"), 1)


if __name__ == "__main__":
    unittest.main()
