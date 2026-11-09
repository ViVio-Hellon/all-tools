"""案内表示 (v4.47.0)

    ライン設定が機側の時に限り 2直の始まりに「計量器の点検を行ってください 点検済みですか？」を
    大きく表示したい。テーブル:案内表示 に条件・タイミング・大きさ(ヒットで表示)
    1. 直の時間に入ったあと最初の見張りで出す 2. 押した記録が要る(作業者は案内の中で選ぶ)
    3. 押すまで閉じない
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import guide_notice as rule  # noqa: E402
from tests._web import WebTestCase  # noqa: E402

DAY = date(2026, 11, 9)
START = datetime(2026, 11, 9, 15, 0)
END = datetime(2026, 11, 9, 22, 50)


def row(**kw):
    base = dict(rule.FIRST_ROW)
    base.update(kw)
    return base


def due(rows, *, line="機側", shift="2直", now=datetime(2026, 11, 9, 15, 1), acked=()):
    return rule.due(rule.parse(rows), line=line, shift=shift, business_date=DAY, now=now,
                    shift_start=START, shift_end=END, acked=acked)


class RuleTests(unittest.TestCase):
    def test_機側の2直の始まりに出る_ほかのライン_ほかの直では出ない(self) -> None:
        self.assertEqual([n.id for n in due([row()])], ["計量器の点検"])
        self.assertEqual(due([row()], line="L-1"), [])
        self.assertEqual(due([row()], shift="1直"), [])

    def test_直の途中でも押すまで出る_押したら出ない(self) -> None:
        self.assertTrue(due([row()], now=datetime(2026, 11, 9, 20, 0)), "直の途中に立ち上げても出す")
        self.assertEqual(due([row()], acked=["計量器の点検"]), [])

    def test_直の外では出ない(self) -> None:
        self.assertEqual(due([row()], now=datetime(2026, 11, 9, 23, 0)), [])

    def test_空のラインと直は全部(self) -> None:
        self.assertTrue(due([row(ライン="", 直="")], line="L-1", shift="1直"))
        self.assertTrue(due([row(ライン="L-1,機側", 直="2")], line="機側"))

    def test_無効の行は出ない(self) -> None:
        self.assertEqual(due([row(有効=0)]), [])

    def test_時刻と直の終わり前(self) -> None:
        at = row(タイミング="時刻", 時刻="18:00")
        self.assertEqual(due([at], now=datetime(2026, 11, 9, 17, 59)), [])
        self.assertTrue(due([at], now=datetime(2026, 11, 9, 18, 0)))
        before = row(タイミング="直の終わり前", 分=30)
        self.assertEqual(due([before], now=datetime(2026, 11, 9, 22, 19)), [])
        self.assertTrue(due([before], now=datetime(2026, 11, 9, 22, 20)))

    def test_期間(self) -> None:
        self.assertEqual(due([row(開始日="2026-11-10")]), [])
        self.assertEqual(due([row(終了日="2026-11-08")]), [])
        self.assertTrue(due([row(開始日="2026/11/09", 終了日="2026-11-09")]))

    def test_読めない欄のある行は出さず_わけを持つ(self) -> None:
        bad = rule.parse([row(タイミング="時刻", 時刻="")])
        self.assertTrue(bad[0].problems)
        self.assertEqual(due([row(タイミング="時刻", 時刻="")]), [])

    def test_並び順と大きさ(self) -> None:
        got = due([row(ID="B", 並び=2), row(ID="A", 並び=1, 大きさ="小", 色="警告")])
        self.assertEqual([n.id for n in got], ["A", "B"])
        self.assertEqual((got[0].size, got[0].color), ("小", "警告"))


class AccessTableTests(unittest.TestCase):
    """伝送用ファイルが Access(.accdb)のときも、表が無ければ作る(共有へ保存と同じ VBScript の道)。"""

    def setUp(self) -> None:
        import tempfile

        from nippou.services import guide_notice

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "伝送用ファイル.accdb"
        self.path.write_bytes(b"not really access")
        guide_notice.reset()
        self.addCleanup(guide_notice.reset)

    def run_with(self, *, exists: bool, now: float = 1000.0):
        from unittest import mock

        from nippou.access_bridge.runner import ScriptResult
        from nippou.services import guide_notice

        scripts: list[str] = []

        class Runner:
            def run(self, text):
                scripts.append(text)
                return ScriptResult(success=True, stdout="OK:2", return_code=0)

        found = mock.Mock(success=exists, rows=[], error=None)
        with mock.patch("nippou.access_bridge.importer.import_table", return_value=found):
            made = guide_notice.ensure_access_table(self.path, runner=Runner(), now=now)
        return made, scripts

    def test_無ければ表を作り_頼まれた1行を入れる(self) -> None:
        made, scripts = self.run_with(exists=False)
        self.assertTrue(made)
        self.assertEqual(len(scripts), 1)
        self.assertIn("CREATE TABLE [案内表示]", scripts[0])
        self.assertIn("[表示文] MEMO", scripts[0])
        self.assertIn("'計量器の点検を行ってください'", scripts[0])
        self.assertIn("'機側'", scripts[0])

    def test_あれば作らない(self) -> None:
        made, scripts = self.run_with(exists=True)
        self.assertFalse(made)
        self.assertEqual(scripts, [])

    def test_確かめるのは10分に1回まで(self) -> None:
        self.run_with(exists=True, now=1000.0)
        made, scripts = self.run_with(exists=False, now=1300.0)
        self.assertFalse(made)
        self.assertEqual(scripts, [], "10分たつまでは確かめない(cscript を毎回走らせない)")
        made, scripts = self.run_with(exists=False, now=1700.0)
        self.assertTrue(made)


class NoticeWebTests(WebTestCase):
    terminal_line = "機側"

    def setUp(self) -> None:
        super().setUp()
        from nippou import config, user_settings
        from nippou.config import SETTINGS
        from nippou.services import guide_notice

        self.out = self.tmp / "出力先"
        user_settings.save_many({config.KEY_REPORT_OUT_DIR: str(self.out)})
        self.master = SETTINGS.transmission_master_path
        self.master.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.master)
        conn.execute("CREATE TABLE 時間用 (直 TEXT, 開始 TEXT, 終了 TEXT)")
        conn.commit()
        conn.close()
        guide_notice.reset()
        self.addCleanup(guide_notice.reset)

    def at(self, when: datetime):
        """時計をその時刻に(案内の判定と押した記録だけ)。"""
        from unittest import mock

        from nippou.services import guide_notice

        real = guide_notice.due_now

        def fixed(repo, calc, line, now=None, **kw):
            return real(repo, calc, line, when, **kw)

        patcher = mock.patch.object(guide_notice, "due_now", side_effect=fixed)
        patcher.start()
        self.addCleanup(patcher.stop)

    def table_rows(self):
        conn = sqlite3.connect(self.master)
        try:
            return conn.execute(f'SELECT "ID", "ライン", "直" FROM "{rule.TABLE}"').fetchall()
        finally:
            conn.close()

    def test_表が無ければ作り_頼まれた案内を入れる_2直の始まりに出る(self) -> None:
        self.at(datetime(2026, 11, 9, 15, 1))
        body = self.get("/api/notice/due").get_json()
        self.assertEqual(self.table_rows(), [("計量器の点検", "機側", "2直")])
        self.assertEqual(body["notice"]["text"], "計量器の点検を行ってください")
        self.assertEqual(body["notice"]["question"], "点検済みですか？")
        self.assertEqual(body["notice"]["button"], "点検済み")
        self.assertEqual(body["notice"]["size"], "大")
        self.assertTrue(body["notice"]["pick_worker"])
        self.assertEqual((body["line"], body["shift"]), ("機側", "2直"))

    def test_1直では出ない(self) -> None:
        self.at(datetime(2026, 11, 9, 9, 0))
        self.assertIsNone(self.get("/api/notice/due").get_json()["notice"])

    def test_名前が無ければ押せない_押したら記録してその直は出ない(self) -> None:
        from nippou.services import guide_notice

        self.at(datetime(2026, 11, 9, 15, 1))
        body = self.get("/api/notice/due").get_json()
        ack = {"id": "計量器の点検", "report_date": body["report_date"], "shift": "2直"}
        res = self.post("/api/notice/ack", ack)
        self.assertEqual(res.status_code, 422, "名前を選ぶまで押せない")
        res = self.post("/api/notice/ack", {**ack, "worker": "山田"})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertIsNone(self.get("/api/notice/due").get_json()["notice"], "押したらその直は出ない")
        # 記録: この端末・共有のCSV
        repo = self.repo()
        got = guide_notice.history(repo)
        self.assertEqual((got[0]["notice_id"], got[0]["worker"], got[0]["shift"]),
                         ("計量器の点検", "山田", "2直"))
        csvs = list((self.out / "機側" / "案内の確認").glob("案内の確認_*.csv"))
        self.assertEqual(len(csvs), 1)
        text = csvs[0].read_text(encoding="utf-8-sig")
        self.assertIn("報告日,直,ライン,案内,押した人,押した時刻,PC", text)
        self.assertIn("2直,機側,計量器の点検,山田", text)

    def test_直が変わったあとの押し直しは断る(self) -> None:
        self.at(datetime(2026, 11, 9, 15, 1))
        body = self.get("/api/notice/due").get_json()
        res = self.post("/api/notice/ack", {"id": "計量器の点検", "worker": "山田",
                                            "report_date": body["report_date"], "shift": "1直"})
        self.assertEqual(res.status_code, 409)

    def test_共有に書けなければ覚えて次の見張りで書く(self) -> None:
        from nippou.services import guide_notice

        self.at(datetime(2026, 11, 9, 15, 1))
        body = self.get("/api/notice/due").get_json()
        self.out.parent.mkdir(parents=True, exist_ok=True)
        self.out.write_text("ファイルなのでフォルダを作れない", encoding="utf-8")
        res = self.post("/api/notice/ack", {"id": "計量器の点検", "worker": "山田",
                                            "report_date": body["report_date"], "shift": "2直"})
        self.assertIn("次の見張りで書きます", res.get_json()["message"])
        self.assertEqual(guide_notice.history(self.repo())[0]["shared"], 0)
        self.out.unlink()
        self.get("/api/notice/due")
        self.assertEqual(guide_notice.history(self.repo())[0]["shared"], 1)

    def test_ラインを決めていない端末には出さない(self) -> None:
        from nippou import config, user_settings, work_context

        user_settings.save(config.KEY_TERMINAL_LINE, "")
        work_context.reset()
        self.at(datetime(2026, 11, 9, 15, 1))
        self.assertIsNone(self.get("/api/notice/due").get_json()["notice"])


if __name__ == "__main__":
    unittest.main()
