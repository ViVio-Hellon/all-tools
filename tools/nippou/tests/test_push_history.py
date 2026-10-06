"""共有保存の履歴 ── 各直で「共有へ保存」を押した時刻と担当者 (v3.84.0)

    各直で共有保存を押したタイミングと担当者を履歴として残してください
    CSV出力とグラフ出力できるようにもしてください
    日報データ.sqlite3に列追加で保存がいい？列がなければ自動追加してください

ここで押さえるのは:

    1. 押すたびに、送った直ごとに1行(送れた・一部・送れなかった・関門で
       止めた・送るもの無し)。担当者はその直の作業者名
    2. 共有の日報データ.sqlite3 に T_共有保存履歴 を**無ければ作り、列が
       足りなければ足す**。既存の日報の表には列を足さない
    3. 共有に届かなかった行は手元に残り、次の保存で写す。ファイルは作らない
    4. 表・グラフ(直の終わりから何分後)・CSV
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.connection import connect  # noqa: E402
from nippou.db.models import DetailRecord, HeaderRecord  # noqa: E402
from nippou.db.repository import NippouRepository  # noqa: E402
from nippou.db.schema import ensure_schema  # noqa: E402
from nippou.presenters import push_log  # noqa: E402
from nippou.services import push_history as ph  # noqa: E402
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402

DAY = "2026年9月25日"
SHIFT_TIMES = {"1": ("07:00", "15:00"), "2": ("15:00", "22:50"), "3": ("22:50", "07:00")}


class RepoCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.conn = connect(self.tmp / "local.sqlite3")
        ensure_schema(self.conn)
        self.addCleanup(self.conn.close)
        self.repo = NippouRepository(self.conn)
        self.shared = self.tmp / "日報データ.sqlite3"

    def save(self, shift: str, worker: str, page: int = 1, day: str = DAY) -> tuple:
        key = dict(report_date=day, line="L-1", shift=shift, page=page)
        self.repo.save(HeaderRecord(**key, worker=worker),
                       [DetailRecord(**key, row_no=1, lot="N7131T0")])
        return (day, "L-1", shift, page)

    def pressed(self, at: str = "2026-09-25T15:07:30", key=(DAY, "L-1", "2直")):
        return ph.Pressed(at=at, key_text=f"{key[0]} {key[2]}", by="佐藤", terminal="PC-L-1")

    def summary(self, ok=(), ng=()):
        failed = [SimpleNamespace(key=k, error=SimpleNamespace(message="ロック中です"))
                  for k in ng]
        return SimpleNamespace(succeeded=list(ok), failed=failed)

    def make_shared(self) -> None:
        sqlite3.connect(self.shared).close()        # 共有のファイルがある状態


class RecordTests(RepoCase):
    """押すたびに、**送った直ごとに1行。**"""

    def test_直ごとに1行_担当者はその直の作業者(self) -> None:
        a = self.save("1直", "山田")
        b = self.save("2直", "佐藤")
        rows = ph.record_push(self.repo, self.pressed(), self.summary(ok=[a, b]),
                              (DAY, "L-1", "2直"))
        got = {(r["shift"], r["worker"], r["result"], r["pages"]) for r in rows}
        self.assertEqual(got, {("1直", "山田", ph.SENT, 1), ("2直", "佐藤", ph.SENT, 1)})
        # 押したときの様子はどの行にも同じものが入る
        self.assertEqual({r["pressed_by"] for r in rows}, {"佐藤"})
        self.assertEqual({r["pressed_at"] for r in rows}, {"2026-09-25T15:07:30"})

    def test_ページが複数なら担当者を並べる(self) -> None:
        a = self.save("1直", "山田", page=1)
        b = self.save("1直", "鈴木", page=2)
        row, = ph.record_push(self.repo, self.pressed(), self.summary(ok=[a, b]),
                              (DAY, "L-1", "1直"))
        self.assertEqual(row["worker"], "山田・鈴木")
        self.assertEqual(row["pages"], 2)

    def test_送れなかったページを数える(self) -> None:
        a = self.save("1直", "山田", page=1)
        b = self.save("1直", "山田", page=2)
        c = self.save("2直", "佐藤")
        rows = {r["shift"]: r for r in ph.record_push(
            self.repo, self.pressed(), self.summary(ok=[a], ng=[b, c]), (DAY, "L-1", "2直"))}
        self.assertEqual(rows["1直"]["result"], ph.PARTIAL)
        self.assertEqual(rows["1直"]["failed_pages"], 1)
        self.assertEqual(rows["2直"]["result"], ph.FAILED)
        self.assertEqual(rows["2直"]["detail"], "ロック中です")

    def test_送るものが無くても押したことは残す(self) -> None:
        self.save("2直", "佐藤")
        row, = ph.record_push(self.repo, self.pressed(), self.summary(),
                              (DAY, "L-1", "2直"))
        self.assertEqual((row["shift"], row["worker"], row["result"], row["pages"]),
                         ("2直", "佐藤", ph.NOTHING, 0))

    def test_関門で止めたときも残す(self) -> None:
        self.save("1直", "山田")
        finding = SimpleNamespace(message="最終時間まで入力がないのでは？")
        report = SimpleNamespace(ok=False, report_date=DAY, line="L-1", shift="1直",
                                 pages=1, findings=[finding, finding])
        row, = ph.record_blocked(self.repo, self.pressed(), [report])
        self.assertEqual(row["result"], ph.BLOCKED)
        self.assertEqual(row["worker"], "山田")
        self.assertIn("最終時間", row["detail"])
        self.assertIn("ほか1件", row["detail"])

    def test_消さない(self) -> None:
        """履歴なので、押すたびに足すだけ。"""
        self.save("2直", "佐藤")
        for _ in range(3):
            ph.record_push(self.repo, self.pressed(), self.summary(), (DAY, "L-1", "2直"))
        self.assertEqual(len(self.repo.push_history()), 3)


class PressedByTests(RepoCase):
    """押した人 ── **押した画面に出ていた作業者。**"""

    def test_画面の作業者を使う(self) -> None:
        self.save("2直", "佐藤")
        pressed = ph.Pressed.now(self.repo, (DAY, "L-1", "2直"), by=" 高橋 ")
        self.assertEqual(pressed.by, "高橋")

    def test_添えていなければ_いまの直の作業者(self) -> None:
        """設定画面・確認画面から押したとき。"""
        self.save("2直", "佐藤")
        self.assertEqual(ph.Pressed.now(self.repo, (DAY, "L-1", "2直")).by, "佐藤")


class SharedTableTests(RepoCase):
    """共有の日報データ.sqlite3 の T_共有保存履歴。"""

    def record(self) -> None:
        self.save("2直", "佐藤")
        ph.record_push(self.repo, self.pressed(), self.summary(), (DAY, "L-1", "2直"))

    def read_shared(self) -> list[dict]:
        conn = sqlite3.connect(self.shared)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(f'SELECT * FROM "{ph.SHARED_TABLE}"')]
        finally:
            conn.close()

    def test_表が無ければ作る(self) -> None:
        self.make_shared()
        self.record()
        self.assertEqual(ph.share(self.repo, self.shared), 1)
        row, = self.read_shared()
        self.assertEqual((row["担当者"], row["結果"], row["直"], row["押した人"]),
                         ("佐藤", ph.NOTHING, "2直", "佐藤"))
        self.assertEqual(self.repo.push_history(unshared_only=True), [])

    def test_列が足りなければ足す(self) -> None:
        """先に作られた表に列が無くても書ける。**既にある行は残る。**"""
        conn = sqlite3.connect(self.shared)
        conn.execute(f'CREATE TABLE "{ph.SHARED_TABLE}" ("ID", "押した日時", "報告日", '
                     '"ライン", "直", PRIMARY KEY ("ID"))')
        conn.execute(f'INSERT INTO "{ph.SHARED_TABLE}" VALUES ("old", "2026-09-01T07:00:00", '
                     '"2026年9月1日", "L-1", "1直")')
        conn.commit()
        conn.close()
        self.record()
        ph.share(self.repo, self.shared)
        rows = {r["ID"]: r for r in self.read_shared()}
        self.assertIn("old", rows)
        self.assertEqual(len(rows), 2)
        new = next(r for i, r in rows.items() if i != "old")
        self.assertEqual(new["担当者"], "佐藤")
        self.assertEqual(set(new), {shared for _, shared in ph.SHARED_COLUMNS})

    def test_日報の表には列を足さない(self) -> None:
        """Access 版の VBA も同じ表を読み書きする。列が増えると壊れる。"""
        from nippou.access_bridge import sqlite_backend
        self.make_shared()
        self.record()
        ph.share(self.repo, self.shared)
        conn = sqlite3.connect(self.shared)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        self.assertEqual(tables, {ph.SHARED_TABLE})
        self.assertNotIn("押した日時", sqlite_backend.HEADER_COLUMNS)

    def test_共有のファイルが無ければ作らない(self) -> None:
        """開くだけで空のDBができてしまう。**写さずに手元に残す。**"""
        self.record()
        self.assertEqual(ph.share(self.repo, self.shared), 0)
        self.assertFalse(self.shared.exists())
        self.assertEqual(len(self.repo.push_history(unshared_only=True)), 1)

    def test_届かなかった行は次の保存で写す(self) -> None:
        self.record()
        ph.share(self.repo, self.shared)          # 共有がまだ無い
        self.make_shared()
        self.record()
        self.assertEqual(ph.share(self.repo, self.shared), 2)
        self.assertEqual(len(self.read_shared()), 2)

    def test_書けなくても止めない(self) -> None:
        self.make_shared()
        self.record()
        with patch("nippou.access_bridge.sqlite_backend._connect",
                   side_effect=sqlite3.OperationalError("database is locked")):
            self.assertEqual(ph.share(self.repo, self.shared), 0)
        self.assertEqual(len(self.repo.push_history(unshared_only=True)), 1)

    def test_書き先がAccessなら写さない(self) -> None:
        self.record()
        self.assertEqual(ph.share(self.repo, self.tmp / "日報データ.accdb"), 0)

    def test_読むと共有とまだ写していない行を合わせる(self) -> None:
        """共有にはほかの端末の行もある。**同じ行は2つにしない。**"""
        self.make_shared()
        self.record()
        ph.share(self.repo, self.shared)
        conn = sqlite3.connect(self.shared)
        conn.execute(f'INSERT INTO "{ph.SHARED_TABLE}" ("ID", "押した日時", "報告日", '
                     '"ライン", "直", "担当者", "結果") VALUES ("other", '
                     '"2026-09-25T15:02:00", ?, "LVC", "1直", "田中", ?)', (DAY, ph.SENT))
        conn.commit()
        conn.close()
        self.record()                              # まだ写していない
        readout = ph.read(self.repo, self.shared)
        self.assertEqual(len(readout.rows), 3)
        self.assertIn("田中", {r["worker"] for r in readout.rows})


class TimingTests(unittest.TestCase):
    """直の終わりから何分後に押したか。"""

    def minutes(self, shift: str, pressed: str, day: str = DAY):
        return push_log.minutes_after_end(
            {"report_date": day, "shift": shift, "pressed_at": pressed}, SHIFT_TIMES)

    def test_終わったあと(self) -> None:
        self.assertEqual(self.minutes("1直", "2026-09-25T15:10:00"), 10)

    def test_終わる前はマイナス(self) -> None:
        self.assertEqual(self.minutes("2直", "2026-09-25T22:20:00"), -30)

    def test_3直は翌朝に終わる(self) -> None:
        """22:50〜07:00。報告日は始まった日。"""
        self.assertEqual(self.minutes("3直", "2026-09-26T07:20:00"), 20)

    def test_読めなければ出さない(self) -> None:
        self.assertIsNone(self.minutes("1直", "壊れた時刻"))


class ViewTests(RepoCase):
    """表とグラフ(`presenters/push_log`)。"""

    def seed(self) -> None:
        self.repo.replace_shift_times(SHIFT_TIMES)
        self.save("1直", "山田")
        self.save("2直", "佐藤")
        for shift, at, result in (("1直", "2026-09-25T15:12:00", ph.SENT),
                                  ("2直", "2026-09-25T22:40:00", ph.BLOCKED),
                                  ("2直", "2026-09-26T09:00:00", ph.SENT)):
            self.repo.add_push_history([{
                "id": f"{shift}{at}", "pressed_at": at, "report_date": DAY,
                "line": "L-1", "shift": shift, "pages": 1,
                "worker": "山田" if shift == "1直" else "佐藤",
                "result": result, "pressed_key": f"{DAY} {shift}"}])

    def view(self, **kw):
        kw.setdefault("line", "L-1")
        return push_log.build(self.repo, start=date(2026, 9, 1), end=date(2026, 9, 30),
                              db_path=self.tmp / "日報データ.accdb", **kw)

    def test_表は押した順_列は画面とCSVで同じ(self) -> None:
        self.seed()
        table = self.view().table
        self.assertEqual(table.columns[:6], ["押した日時", "報告日", "ライン", "直",
                                            "担当者", "結果"])
        self.assertEqual([r[3] for r in table.rows], ["1直", "2直", "2直"])
        after = table.columns.index("直の終わりとの差(分・マイナスは直内)")
        self.assertEqual([r[after] for r in table.rows], ["+12", "-10", "+610"])
        when = table.columns.index("直内か")
        self.assertEqual([r[when] for r in table.rows], ["直の後", "直内", "直の後"])
        self.assertIn("押した人", table.columns)

    def test_グラフは直の終わりから何分後(self) -> None:
        self.seed()
        chart = self.view().chart
        got = [(p["shift"], p["actual"], p["reached"], p["clipped"]) for p in chart["points"]]
        # 翌朝に押した(610分)は範囲の外。端に寄せて、本当の値を添える
        self.assertEqual(got, [("1直", 12, True, False), ("2直", -10, False, False),
                               ("2直", 610, True, True)])
        self.assertEqual(chart["points"][2]["clip_label"], "10時間後")
        self.assertEqual(chart["points"][2]["minutes"], push_log.Y_CEIL)
        # 色と形は直に付く(色だけに頼らない)
        self.assertEqual({(s["name"], s["mark"], s["shape"]) for s in chart["shifts"]},
                         {("1直", "cat-1", "circle"), ("2直", "cat-2", "square")})
        self.assertEqual(chart["y_min"] % 30, 0)          # 目盛りの切りのよいところ
        self.assertEqual(len(chart["days"]), 30)

    def test_範囲の外は端に寄せる(self) -> None:
        self.seed()
        self.repo.add_push_history([{
            "id": "late", "pressed_at": "2026-09-27T15:00:00", "report_date": DAY,
            "line": "L-1", "shift": "1直", "result": ph.SENT}])
        point = next(p for p in self.view().chart["points"] if p["actual"] > 1000)
        self.assertTrue(point["clipped"])
        self.assertEqual(point["minutes"], push_log.Y_CEIL)
        self.assertEqual(point["clip_label"], "48時間後")

    def test_ふだんの点が0の線に潰れない(self) -> None:
        """1回だけ遅い直があっても、縦の範囲はそれに引っ張られない。"""
        self.seed()
        chart = self.view().chart
        self.assertLessEqual(chart["y_max"], push_log.Y_CEIL)
        self.assertGreaterEqual(chart["y_min"], push_log.Y_FLOOR)

    def test_ラインと期間で絞る(self) -> None:
        self.seed()
        self.repo.add_push_history([{
            "id": "lvc", "pressed_at": "2026-09-25T15:00:00", "report_date": DAY,
            "line": "LVC", "shift": "1直", "result": ph.SENT},
            {"id": "aug", "pressed_at": "2026-08-31T15:00:00", "report_date": "2026年8月31日",
             "line": "L-1", "shift": "1直", "result": ph.SENT}])
        self.assertEqual(len(self.view().table.rows), 3)
        self.assertEqual(len(self.view(line=None).table.rows), 4)

    def test_数える(self) -> None:
        self.seed()
        counts = self.view().counts
        self.assertEqual(counts["押した"], 3)
        self.assertEqual(counts[ph.SENT], 2)
        self.assertEqual(counts[ph.BLOCKED], 1)
        self.assertEqual(counts["直内"], 1)
        self.assertEqual(counts["直の後"], 2)

    def test_点に載せると押した人が出る(self) -> None:
        """「履歴に押した作業者を表示させてください(マウスオーバーでいいです)」"""
        self.seed()
        self.repo.add_push_history([{
            "id": "by", "pressed_at": "2026-09-25T14:50:00", "report_date": DAY,
            "line": "L-1", "shift": "1直", "worker": "山田", "pressed_by": "高橋",
            "result": ph.SENT}])
        point = next(p for p in self.view().chart["points"] if p["pressed_by"] == "高橋")
        self.assertIn("押した人 高橋", point["tip"])
        self.assertIn("その直の担当者 山田", point["tip"])
        self.assertIn("直の終わりの10分前(直内)", point["tip"])

    def test_ふつうは直内_線の下と読む(self) -> None:
        """「共有は普通直内に押すはずです」── 題も目盛りも、過ぎる前提にしない。"""
        self.seed()
        chart = self.view().chart
        self.assertNotIn("何分後", chart["title"])
        self.assertIn("直内", chart["below"])
        self.assertLess(chart["y_min"], 0)
        self.assertLessEqual(abs(push_log.Y_CEIL), abs(push_log.Y_FLOOR),
                             "直内(下)のほうを広く取る")
        self.assertEqual(push_log.timing_of(0), "直内")
        self.assertEqual(push_log.timing_of(1), "直の後")
        self.assertEqual(push_log.when_text(-12), "直の終わりの12分前(直内)")
        self.assertEqual(push_log.when_text(5), "直が終わって5分後")


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class FlowTests(WebTestCase):
    """本物の「共有へ保存」で残るか(書き先は sqlite3)。"""

    def setUp(self) -> None:
        super().setUp()
        from nippou import config, user_settings
        self.share = self.tmp / "共有"
        self.share.mkdir()
        user_settings.save_many({config.KEY_ACCESS_DIR: str(self.share),
                                 config.KEY_REPORT_OUT_DIR: str(self.tmp / "出力")})

    def shared_rows(self) -> list[tuple]:
        from nippou.config import SETTINGS
        conn = sqlite3.connect(SETTINGS.access_db_path)
        try:
            return conn.execute(f'SELECT 直, 担当者, 結果, 通し方 FROM "{ph.SHARED_TABLE}" '
                                'ORDER BY 押した日時, 結果').fetchall()
        finally:
            conn.close()

    def push(self, **body):
        with patch("nippou.services.shift_check.run_pending", return_value=[]):
            return self.post("/api/settings/push", body)

    def test_押すと手元と共有に残る(self) -> None:
        from tests.test_auto_export import payload
        self.assertTrue(self.post("/api/entry/save", payload()).get_json()["saved"])
        self.assertEqual(self.push().status_code, 200)
        self.assertEqual(self.push().status_code, 200)          # 2回目は送るもの無し
        local = self.repo().push_history()
        self.assertEqual([r["result"] for r in local], [ph.SENT, ph.NOTHING])
        self.assertEqual({r["worker"] for r in local}, {"山田"})
        self.assertTrue(all(r["shared"] for r in local))
        self.assertEqual(sorted(r[2] for r in self.shared_rows()), sorted([ph.SENT, ph.NOTHING]))

    def test_押した人は画面の作業者(self) -> None:
        """日報入力の「共有へ保存」は、欄に出ている作業者を添えてくる。"""
        from tests.test_auto_export import payload
        self.post("/api/entry/save", payload())
        self.assertEqual(self.push(worker="高橋").status_code, 200)
        row, = self.repo().push_history()
        self.assertEqual((row["worker"], row["pressed_by"]), ("山田", "高橋"))
        js = (Path(__file__).resolve().parent.parent / "app" / "static" / "js"
              / "views" / "entry.js").read_text(encoding="utf-8")
        body = js[js.index("async function pushShared"):]
        self.assertIn('[data-header="worker"]', body[:body.index("catch (err)")])

    def test_関門で止めても残る(self) -> None:
        from tests.test_auto_export import payload
        self.post("/api/entry/save", payload())
        finding = SimpleNamespace(message="最終時間まで入力がないのでは？",
                                  skippable=True, as_dict=lambda: {"message": "x"})
        report = SimpleNamespace(ok=False, report_date="x", line="L-1", shift="1直",
                                 pages=1, rows=1, findings=[finding], skipped=[],
                                 key_text="x", as_dict=lambda: {})
        with patch("nippou.services.shift_check.run_pending", return_value=[report]), \
                patch("nippou.services.shift_check.blocked_reports", return_value=[report]), \
                patch("nippou.services.shift_check.combined_message", return_value="止めた"), \
                patch("nippou.logic.save_checks.unskippable", return_value=[]):
            res = self.post("/api/settings/push", {})
        self.assertEqual(res.status_code, 422)
        self.assertEqual([r["result"] for r in self.repo().push_history()], [ph.BLOCKED])

    def test_記録で失敗しても共有への保存は返す(self) -> None:
        from tests.test_auto_export import payload
        self.post("/api/entry/save", payload())
        with patch("nippou.services.push_history.record_push", side_effect=RuntimeError("x")):
            res = self.push()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["succeeded"], 1)

    def test_表とグラフとCSV(self) -> None:
        from tests.test_auto_export import payload
        self.post("/api/entry/save", payload())
        self.push()
        span = {"start": "2000-01-01", "end": "2099-12-31"}
        view = self.post("/api/agg/push-log", span).get_json()
        self.assertEqual(view["table"]["count"], 1)
        self.assertIn("直の終わりに対して", view["chart"]["title"])
        self.assertIn("T_共有保存履歴", view["source"])
        body = self.post("/api/agg/push-log/csv", {**span, "scope": "all"}).get_json()
        out = Path(body["file"])
        self.assertTrue(out.exists())
        self.assertTrue(str(out).startswith(str(self.tmp)))
        text = out.read_text(encoding="utf-8-sig")
        self.assertTrue(text.startswith("押した日時,報告日,ライン,直,担当者,結果"))
        self.assertIn("全ライン", out.name)

    def test_画面にカードがある(self) -> None:
        html = self.get("/agg").get_data(as_text=True)
        self.assertIn('id="push-log"', html)
        self.assertIn('href="#push-log"', html)
        root = Path(__file__).resolve().parent.parent / "app" / "static" / "js"
        self.assertIn("paintTimingChart", (root / "views" / "agg.js").read_text(encoding="utf-8"))
        self.assertIn("export function paintTimingChart",
                      (root / "chart.js").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
