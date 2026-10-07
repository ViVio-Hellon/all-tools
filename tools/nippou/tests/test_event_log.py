"""出来事の記録 ── エラーの後追いとなぜなぜ分析 (v4.1.0)

    エラー等の後追いが現状できないと感じている
    ログを残しなぜなぜで分析できるようにしておいてほしい
    ログ出力は設定でパス指定できるようにする

縛るもの:
    - 予期しないエラーに**番号**が付き、画面の文言にも同じ番号が出る
    - 番号から1件に辿れて、**なぜなぜの段**(起きたこと・直接の原因・状態・
      それまでの経過)が揃っている
    - 送った中身は残すが、**合言葉は伏せる**
    - 断り(4xx)は**画面に出た文言ごと**残る
    - 見張り(心拍・1分ごと)は残さない ── 経過が読めなくなるので
    - `log.warning` / `log.exception` で書かれた記録も出来事に入る
    - ログの置き場所は設定で変えられ、**書けなければ手元へ**書く(止めない)
"""
from __future__ import annotations

import json
import logging
import random
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import event_log as rule  # noqa: E402
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402


def _rec(kind, at, **kw):
    return rule.make(kind, at=at, terminal=kw.pop("terminal", "PC-1"), **kw)


class LogicTests(unittest.TestCase):
    """純粋な部分(`logic/event_log.py`)。"""

    def test_番号は種類と日時と見間違えない3文字(self) -> None:
        at = datetime(2026, 10, 1, 12, 53, 7)
        made = {rule.new_id(rule.KIND_ERROR, at, random.Random(i)) for i in range(200)}
        for eid in made:
            self.assertRegex(eid, r"^E1001-1253-[2-9A-Z]{3}$")
            for ch in eid[-3:]:
                self.assertNotIn(ch, "01OILSB58")
        self.assertGreater(len(made), 150)
        self.assertTrue(rule.new_id(rule.KIND_CLIENT, at).startswith("C1001-"))

    def test_同じ1分に同じ番号を2度出さない(self) -> None:
        """3文字はくじ。1分に30件出すと2%ほどの割合で重なり、一覧(同じ番号は1件に
        まとめる)から別のエラーが1件消えていた。**同じくじの目が出ても引き直す。**"""
        at = datetime(2026, 10, 2, 9, 15)
        first = rule.new_id(rule.KIND_ERROR, at, random.Random(7))
        again = rule.new_id(rule.KIND_ERROR, at, random.Random(7))   # 同じ目が出るくじ
        self.assertNotEqual(first, again)
        made = [rule.new_id(rule.KIND_CLIENT, at) for _ in range(500)]
        self.assertEqual(len(set(made)), len(made))
        # 分が替われば、前の分の番号は覚えておかない
        later = rule.new_id(rule.KIND_ERROR, datetime(2026, 10, 2, 9, 16), random.Random(7))
        self.assertTrue(later.startswith("E1002-0916-"))

    def test_送った中身は平らにして空と合言葉を落とす(self) -> None:
        got = rule.summarize_input({
            "rows": {"1": {"LOT": "B123456", "CON": ""}, "2": {}},
            "header": {"worker": "山田"},
            "password": "nisk", "admin_password": "x", "token": "t",
        })
        self.assertEqual(got["rows.1.LOT"], "B123456")
        self.assertEqual(got["header.worker"], "山田")
        self.assertNotIn("rows.1.CON", got)
        for key in ("password", "admin_password", "token"):
            self.assertEqual(got[key], "***")
        self.assertNotIn("nisk", json.dumps(got, ensure_ascii=False))

    def test_中身が多すぎれば数だけ言う(self) -> None:
        got = rule.summarize_input({f"k{i}": "x" * 200 for i in range(60)})
        self.assertLessEqual(len(got), rule.INPUT_MAX_ITEMS + 1)
        self.assertIn("ほか 20 項目", got["…"])
        self.assertTrue(got["k0"].endswith("…"))

    def test_コードの場所はこのツールのいちばん深い所(self) -> None:
        frames = [("/app/nippou/routes/x.py", 10, "save"),
                  ("/app/nippou/services/summary.py", 42, "refresh"),
                  ("/venv/lib/site-packages/flask/app.py", 900, "dispatch")]
        self.assertEqual(rule.where_in_code(frames, ["/app"]),
                         "nippou/services/summary.py:42 (refresh)")
        self.assertIn("flask/app.py:900",
                      rule.where_in_code(frames[2:], ["/app"]))
        self.assertEqual(rule.where_in_code([], ["/app"]), "")

    def test_操作の呼び名(self) -> None:
        self.assertEqual(rule.label_of("GET", "/"), "日報入力を開いた")
        self.assertEqual(rule.label_of("POST", "/api/entry/save"), "日報: 保存(確定)")
        self.assertEqual(rule.label_of("POST", "/api/settings/zzz"), "設定の操作")
        self.assertEqual(rule.label_of("GET", "/unknown"), "GET /unknown")
        self.assertIn("打ちかけ", rule.request_label("POST", "/api/entry/save",
                                                    {"draft": True}))

    def test_残すもの残さないもの(self) -> None:
        ok = rule.should_record
        self.assertTrue(ok("POST", "/api/entry/save", 200))
        self.assertTrue(ok("GET", "/settings", 200))
        self.assertFalse(ok("GET", "/api/shift/key", 200))          # 読むだけ
        self.assertFalse(ok("POST", "/api/tab/ping", 200))          # 心拍
        self.assertFalse(ok("POST", "/api/entry/close", 200, quiet=True))  # 見張り
        self.assertFalse(ok("GET", "/sv/abc/js/app.js", 200))       # 静的
        self.assertFalse(ok("POST", "/api/log/event/x/analysis", 200))
        # **断りとエラーは全部残す**(見張りでも)
        self.assertTrue(ok("GET", "/api/shift/key", 500))
        self.assertTrue(ok("POST", "/api/tab/ping", 409, quiet=True))
        self.assertFalse(ok("GET", "/favicon.ico", 404))

    def test_経過は同じタブの前30分とその後5分(self) -> None:
        t = datetime(2026, 10, 1, 12, 0)
        target = _rec(rule.KIND_ERROR, t, tab="A", id="E1")
        records = [
            _rec(rule.KIND_OP, t - timedelta(minutes=40), tab="A", id="old"),
            _rec(rule.KIND_OP, t - timedelta(minutes=10), tab="A", id="a1"),
            _rec(rule.KIND_OP, t - timedelta(minutes=5), tab="B", id="other-tab"),
            _rec(rule.KIND_WARN, t - timedelta(minutes=2), id="no-tab"),
            _rec(rule.KIND_OP, t - timedelta(minutes=1), tab="A", id="a2",
                 terminal="PC-2"),
            target,
            _rec(rule.KIND_OP, t + timedelta(minutes=2), tab="A", id="after"),
            _rec(rule.KIND_OP, t + timedelta(minutes=9), tab="A", id="late"),
        ]
        got = [r["id"] for r in rule.timeline(records, target)]
        self.assertEqual(got, ["a1", "no-tab", "E1", "after"])

    def test_一覧は種類で絞って新しい順(self) -> None:
        t = datetime(2026, 10, 1, 12, 0)
        records = [_rec(rule.KIND_OP, t, id="o"),
                   _rec(rule.KIND_ERROR, t - timedelta(minutes=1), id="e-old",
                        message="保存できません"),
                   _rec(rule.KIND_ERROR, t, id="e-new", message="集計"),
                   _rec(rule.KIND_REFUSED, t, id="r", terminal="PC-2")]
        self.assertEqual([r["id"] for r in rule.select(records)], ["e-new", "e-old"])
        self.assertEqual(len(rule.select(records, scope="problems")), 3)
        self.assertEqual(len(rule.select(records, scope="all")), 4)
        self.assertEqual([r["id"] for r in rule.select(records, scope="all",
                                                       terminal="PC-2")], ["r"])
        self.assertEqual([r["id"] for r in rule.select(records, text="保存")], ["e-old"])

    def test_なぜなぜシートは記録の段と人の段に分かれる(self) -> None:
        t = datetime(2026, 10, 1, 12, 0)
        target = _rec(rule.KIND_ERROR, t, id="E1", screen="/", action="POST /api/entry/save",
                      message="思わぬエラー", cause="KeyError: 'LOT'",
                      where="nippou/x.py:3 (f)", key="2026年10月1日 L-1 1直", line="L-1")
        sheet = rule.sheet(target, [target], {"whys": ["a"], "status": "doing"})
        labels = [f["label"] for f in sheet["facts"]]
        for want in ("番号", "いつ", "どの端末", "どの画面", "何をした", "何と出た"):
            self.assertIn(want, labels)
        self.assertEqual(sheet["cause"][0]["value"], "KeyError: 'LOT'")
        self.assertIn("2026年10月1日 L-1 1直", [s["value"] for s in sheet["state"]])
        self.assertTrue(sheet["steps"][0]["is_target"])
        self.assertIn("KeyError", sheet["hint"])
        self.assertEqual(sheet["analysis"]["status_label"], "対策中")

    def test_書いてもらったなぜなぜは5段に揃える(self) -> None:
        got = rule.clean_analysis({"whys": ["1", "2", "3", "4", "5", "6"],
                                   "status": "変な値", "measure": "x" * 999})
        self.assertEqual(len(got["whys"]), rule.WHY_COUNT)
        self.assertEqual(got["status"], rule.STATUS_OPEN)
        self.assertEqual(len(got["measure"]), rule.NOTE_MAX)
        self.assertEqual(rule.clean_analysis({})["whys"], [""] * rule.WHY_COUNT)

    def test_CSVの行は見出しと同じ数(self) -> None:
        rec = _rec(rule.KIND_ERROR, datetime(2026, 10, 1), id="E1",
                   input={"rows.1.LOT": "B1"})
        self.assertEqual(len(rule.csv_row(rec)), len(rule.CSV_HEADER))
        row = rule.csv_row(rec, {"whys": ["なぜ"], "status": "done"})
        self.assertIn("対策済", row)
        self.assertIn("rows.1.LOT=B1", row)


class ServiceTests(unittest.TestCase):
    """書く・読む(`services/event_log.py`)。**一時フォルダの外へ書かない。**"""

    def setUp(self) -> None:
        from nippou.services import event_log

        self.ev = event_log
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._before = list(logging.getLogger().handlers)
        event_log.install(log_dir=self.dir)

    def tearDown(self) -> None:
        self.ev.flush()
        root = logging.getLogger()
        for handler in list(root.handlers):
            if handler not in self._before:
                root.removeHandler(handler)
        self.ev._override["dir"] = None
        self._tmp.cleanup()

    def test_端末ごと日ごとのファイルに書いて読める(self) -> None:
        now = datetime.now()
        made = self.ev.record(rule.make(rule.KIND_ERROR, at=now,
                                        terminal=self.ev.terminal_name(), message="x"))
        self.ev.flush()
        files = list(self.dir.glob("出来事_*.jsonl"))
        self.assertEqual(len(files), 1)
        self.assertIn(now.strftime("%Y-%m-%d"), files[0].name)
        found, records = self.ev.find(made["id"])
        self.assertEqual(found["message"], "x")
        self.assertEqual(len(records), 1)

    def test_警告とエラーの記録も出来事に入る_同じ警告は1分に1度(self) -> None:
        log = logging.getLogger("nippou.services.demo")
        for _ in range(3):
            log.warning("マスタを読めませんでした: %s", "SIKALOT")
        try:
            raise ValueError("こわれた")
        except ValueError:
            log.exception("集計を作れませんでした")
        logging.getLogger("waitress.queue").warning("Task queue depth is 1")
        self.ev.flush()
        recs = self.ev.load(date.today(), date.today())
        warns = [r for r in recs if r["kind"] == rule.KIND_WARN]
        errors = [r for r in recs if r["kind"] == rule.KIND_ERROR]
        self.assertEqual(len(warns), 1)
        self.assertEqual(warns[0]["message"], "マスタを読めませんでした: SIKALOT")
        self.assertIn("test_event_log.py", warns[0]["where"])
        self.assertEqual(len(errors), 1)
        self.assertIn("ValueError: こわれた", errors[0]["cause"])
        self.assertIn("Traceback", errors[0]["trace"])
        self.assertFalse(any("Task queue" in r["message"] for r in recs))

    def test_番号を持つ記録は二重に残さない(self) -> None:
        logging.getLogger("nippou.x").error("[E1] すでに残した", extra={"event_id": "E1"})
        self.ev.flush()
        self.assertEqual(self.ev.load(date.today(), date.today()), [])

    def test_書けない先なら手元へ書いて理由を言う(self) -> None:
        blocker = self.dir / "ファイル"
        blocker.write_text("x", encoding="utf-8")
        local = self.dir / "手元"
        self.ev._override["dir"] = None
        orig_conf, orig_def = self.ev.configured_dir, self.ev.default_dir
        self.ev.configured_dir = lambda: blocker / "logs"       # ファイルの下は作れない
        self.ev.default_dir = lambda: local
        try:
            self.ev.record(rule.make(rule.KIND_ERROR, at=datetime.now(),
                                     terminal="PC-1", message="m"))
            self.ev.flush()
            self.assertEqual(len(list(local.glob("出来事_*.jsonl"))), 1)
            self.assertIn("書けません", self.ev.status()["fallback"])
        finally:
            self.ev.configured_dir, self.ev.default_dir = orig_conf, orig_def
            self.ev._fallback.update(reason="", since=0.0, dir="")

    def test_古いものは自分の端末のぶんだけ消す(self) -> None:
        me = self.ev._safe(self.ev.terminal_name())
        old = date.today() - timedelta(days=self.ev.KEEP_DAYS + 3)
        mine = self.dir / f"出来事_{old.isoformat()}_{me}.jsonl"
        other = self.dir / f"出来事_{old.isoformat()}_ほかの端末.jsonl"
        recent = self.dir / f"出来事_{date.today().isoformat()}_{me}.jsonl"
        for path in (mine, other, recent):
            path.write_text("{}\n", encoding="utf-8")
        self.assertEqual(self.ev.purge(), 1)
        self.assertFalse(mine.exists())
        self.assertTrue(other.exists())
        self.assertTrue(recent.exists())

    def test_なぜなぜは新しいものが勝つ_全端末を読む(self) -> None:
        (self.dir / "なぜなぜ_ほかの端末.jsonl").write_text(
            json.dumps({"id": "E1", "at": "2026-01-01T00:00:00", "whys": ["古い"]},
                       ensure_ascii=False) + "\n", encoding="utf-8")
        self.ev.save_analysis("E1", {"whys": ["新しい"], "status": "done"})
        got = self.ev.analyses()["E1"]
        self.assertEqual(got["whys"][0], "新しい")
        self.assertEqual(got["status"], "done")

    def test_壊れた行は飛ばす(self) -> None:
        path = self.dir / f"出来事_{date.today().isoformat()}_x.jsonl"
        path.write_text('{"id": "a", "kind": "op"}\n{"id": "b", "kin\n',
                        encoding="utf-8")
        self.assertEqual([r["id"] for r in self.ev.load(date.today(), date.today())], ["a"])


class LoggingSetupTests(unittest.TestCase):
    """`nippou.log` の書き先。**書けなくても起動を止めない。**"""

    def setUp(self) -> None:
        self._before = list(logging.getLogger().handlers)
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        from nippou.services import event_log

        event_log.flush()
        root = logging.getLogger()
        for handler in list(root.handlers):
            if handler not in self._before:
                root.removeHandler(handler)
                handler.close()
        event_log._override["dir"] = None
        self._tmp.cleanup()

    def test_書けない先なら手元のlogsへ書く(self) -> None:
        import os

        from nippou import logging_setup

        blocker = self.dir / "ファイル"
        blocker.write_text("x", encoding="utf-8")
        os.environ["NIPPOU_APP_DIR"] = str(self.dir / "app")
        try:
            logging_setup.init_logging(blocker / "logs")
            status = logging_setup.log_status()
            self.assertIn("書けません", status["problem"])
            self.assertEqual(Path(status["path"]).parent, self.dir / "app" / "logs")
        finally:
            os.environ.pop("NIPPOU_APP_DIR", None)


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class WebTests(WebTestCase):
    """拾う口(`app/event_capture.py`)とログの面(`app/routes/logs.py`)。"""

    def setUp(self) -> None:
        super().setUp()
        from nippou.logging_setup import get_logger

        def boom():
            get_logger("services.demo").warning("試しの警告")
            return {}["無い鍵"]

        self.app.add_url_rule("/api/test/boom", "boom", boom, methods=["POST"])
        self.app.add_url_rule("/test/page-boom", "page_boom", boom)

    def events(self, scope="all") -> list[dict]:
        from nippou.services import event_log

        event_log.flush()
        return self.get(f"/api/log/events?scope={scope}").get_json()["rows"]

    def test_予期しないエラーに番号_画面の文言にも同じ番号(self) -> None:
        res = self.post("/api/test/boom", {"rows": {"1": {"LOT": "B123456"}},
                                           "password": "nisk"})
        self.assertEqual(res.status_code, 500)
        err = res.get_json()["error"]
        self.assertEqual(err["code"], "internal")
        self.assertRegex(err["event_id"], r"^E\d{4}-\d{4}-[2-9A-Z]{3}$")
        self.assertIn(err["event_id"], err["message"])
        self.assertIn("管理者", err["message"])

        rows = self.events("errors")
        self.assertEqual([r["id"] for r in rows], [err["event_id"]])

        body = self.get(f"/api/log/event/{err['event_id']}").get_json()
        sheet = body["sheet"]
        cause = {c["label"]: c["value"] for c in sheet["cause"]}
        self.assertIn("KeyError", cause["例外"])
        self.assertEqual(cause["応答"], "500")
        self.assertIn("boom", cause["コードの場所"])
        self.assertEqual(sheet["input"]["rows.1.LOT"], "B123456")
        self.assertEqual(sheet["input"]["password"], "***")
        self.assertIn("Traceback", sheet["trace"])
        self.assertNotIn("nisk", json.dumps(body, ensure_ascii=False))

    def test_画面で起きたらHTMLで番号を出す(self) -> None:
        res = self.get("/test/page-boom")
        self.assertEqual(res.status_code, 500)
        html = res.get_data(as_text=True)
        self.assertIn("思わぬエラーが起きました", html)
        self.assertRegex(html, r"E\d{4}-\d{4}-[2-9A-Z]{3}")
        self.assertIn("/settings?tab=logs", html)

    def test_断りは画面に出た文言ごと残る(self) -> None:
        res = self.post("/api/settings/backfill",
                        {"report_date": "2026年9月30日", "line": "L-1", "shift": "1直"})
        self.assertEqual(res.status_code, 403)
        refused = [r for r in self.events("problems") if r["kind"] == "refused"]
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0]["label"], "過去の直を後から作る")
        self.assertIn("管理者モードが要ります", refused[0]["message"])

    def test_操作は残す_見張りと読むだけは残さない(self) -> None:
        self.post("/api/entry/state", {"rows": {"1": {"LOT": "B1"}}})
        self.client.post("/api/entry/state", json={},
                         headers={"Host": "127.0.0.1", "X-Tool-Token": "test-token",
                                  "X-Quiet": "1"})
        self.get("/api/shift/key")
        self.post("/api/tab/ping", {})
        labels = [r["label"] for r in self.events("all")]
        self.assertEqual(labels.count("日報: 欄を確かめる"), 1)
        self.assertNotIn("GET /api/shift/key", labels)

    def test_どの画面からどのタブで_開いていた直も残る(self) -> None:
        from nippou import work_context
        from nippou.services.nippou_service import RecallState

        work_context.get_context().recall = RecallState(
            active=True, report_date="2026年9月30日", line="L-1", shift="1直", page=2)
        self.client.post("/api/test/boom", json={},
                         headers={"Host": "127.0.0.1", "X-Tool-Token": "test-token",
                                  "X-Screen": "/records", "X-Tab": "tab-1"})
        rows = self.events("errors")
        sheet = self.get(f"/api/log/event/{rows[0]['id']}").get_json()["sheet"]
        facts = {f["label"]: f["value"] for f in sheet["facts"]}
        state = {s["label"]: s["value"] for s in sheet["state"]}
        self.assertEqual(facts["どの画面"], "/records")
        self.assertEqual(state["開いていた直"], "2026年9月30日 L-1 1直 ページ2")
        self.assertEqual(state["呼び出し中"], "はい")

    def test_経過に前の操作が並ぶ(self) -> None:
        headers = {"Host": "127.0.0.1", "X-Tool-Token": "test-token", "X-Tab": "T"}
        self.client.post("/api/entry/state", json={}, headers=headers)
        self.client.post("/api/test/boom", json={}, headers=headers)
        err = [r for r in self.events("errors")][0]
        steps = self.get(f"/api/log/event/{err['id']}").get_json()["sheet"]["steps"]
        self.assertIn("日報: 欄を確かめる", steps[0]["text"])
        self.assertTrue(steps[-1]["is_target"])

    def test_知らない番号は404(self) -> None:
        res = self.get("/api/log/event/E0101-0000-AAA")
        self.assertEqual(res.status_code, 404)
        self.assertIn("見つかりません", res.get_json()["error"]["message"])

    def test_なぜなぜを残すと一覧に状態が出て_面の見出しの数が減る(self) -> None:
        eid = self.post("/api/test/boom", {}).get_json()["error"]["event_id"]
        html = self.get("/settings").get_data(as_text=True)
        self.assertIn("エラー1件", html)
        res = self.post(f"/api/log/event/{eid}/analysis",
                        {"whys": ["鍵が無かった", "マスタが空だった"],
                         "measure": "空なら止める", "status": "done", "by": "山田"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["sheet"]["analysis"]["whys"][1], "マスタが空だった")
        rows = self.events("errors")
        self.assertEqual(rows[0]["status_label"], "対策済")
        html = self.get("/settings").get_data(as_text=True)
        self.assertNotIn("エラー1件", html)

    def test_CSVに出す(self) -> None:
        self.post("/api/test/boom", {})
        today = date.today().isoformat()
        res = self.post("/api/log/csv", {"start": today, "end": today, "scope": "errors"})
        self.assertEqual(res.status_code, 200)
        path = Path(res.get_json()["path"])
        self.assertTrue(str(path).startswith(str(self.tmp)))
        text = path.read_text(encoding="utf-8-sig")
        self.assertTrue(text.startswith("番号,日時,種類"))
        self.assertIn("なぜ5", text.splitlines()[0])
        self.assertEqual(len(text.strip().splitlines()), 2)
        res = self.post("/api/log/csv", {"start": "2001-01-01", "end": "2001-01-02"})
        self.assertEqual(res.status_code, 400)

    def test_画面のエラーを受け取って番号を返す(self) -> None:
        res = self.post("/api/log/client", {
            "message": "TypeError: x is undefined", "where": "/sv/a/js/views/entry.js:12:3",
            "stack": "at paint", "screen": "/", "crumbs": ["13:00:01 押した: 保存(確定) #save"],
            "kind": "error"})
        eid = res.get_json()["id"]
        self.assertTrue(eid.startswith("C"))
        sheet = self.get(f"/api/log/event/{eid}").get_json()["sheet"]
        self.assertEqual(sheet["crumbs"], ["13:00:01 押した: 保存(確定) #save"])
        self.assertEqual(self.events("errors")[0]["kind_label"], "画面のエラー")

    def test_画面のエラーは1分に30件まで(self) -> None:
        from app.routes import logs

        ids = [self.post("/api/log/client", {"message": f"e{i}"}).get_json()
               for i in range(logs.CLIENT_PER_MINUTE + 3)]
        self.assertTrue(ids[-1].get("skipped"))
        self.assertEqual(len(self.events("errors")), logs.CLIENT_PER_MINUTE)

    def test_ログの置き場所を設定で変えるとそこへ書く(self) -> None:
        root = logging.getLogger()
        before = list(root.handlers)

        def restore() -> None:
            for handler in list(root.handlers):
                if handler not in before:
                    root.removeHandler(handler)
                    handler.close()
        self.addCleanup(restore)

        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        shared = self.tmp / "共有" / "ログ"
        res = self.post("/api/settings/paths", {"log_dir": str(shared)})
        self.assertEqual(res.status_code, 200)
        self.assertIn(str(shared), res.get_json()["message"])
        self.post("/api/test/boom", {})
        from nippou.services import event_log
        event_log.flush()
        self.assertTrue(list(shared.glob("出来事_*.jsonl")))
        self.assertTrue((shared / "nippou.log").exists())
        status = self.get("/api/log/events").get_json()["status"]
        self.assertEqual(status["dir"], str(shared))
        moved = [r for r in self.events("all") if r["label"] == "ログの出力パスを変えた"]
        self.assertEqual(len(moved), 1)
        self.assertIn(str(shared), moved[0]["message"])

    def test_書けない置き場所なら手元へ書くと言う(self) -> None:
        root = logging.getLogger()
        before = list(root.handlers)

        def restore() -> None:
            for handler in list(root.handlers):
                if handler not in before:
                    root.removeHandler(handler)
                    handler.close()
        self.addCleanup(restore)

        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        blocker = self.tmp / "ファイル"
        blocker.write_text("x", encoding="utf-8")
        res = self.post("/api/settings/paths", {"log_dir": str(blocker / "logs")})
        self.assertEqual(res.status_code, 200)
        self.assertIn("この端末の logs へ書いています", res.get_json()["message"])

    def test_設定画面にログの面と置き場所の欄(self) -> None:
        html = self.get("/settings").get_data(as_text=True)
        self.assertIn('id="panel-logs"', html)
        self.assertIn('id="log-rows"', html)
        self.assertIn('id="ls-whys"', html)
        self.assertIn("js/views/logs.js", html)
        self.assertIn("ログの出力パス", html)
        from nippou import config, distribution
        self.assertIn(config.KEY_LOG_DIR, distribution.ITEM_KEYS)

    def test_画面は画面名とタブを名乗り_エラーを届ける(self) -> None:
        static = Path(__file__).resolve().parent.parent / "app" / "static" / "js"
        api = (static / "api.js").read_text(encoding="utf-8")
        self.assertIn('"X-Screen": location.pathname', api)
        self.assertIn('"X-Quiet"', api)
        errors = (static / "errors.js").read_text(encoding="utf-8")
        self.assertIn("/api/log/client", errors)
        self.assertIn("unhandledrejection", errors)
        app_js = (static / "app.js").read_text(encoding="utf-8")
        self.assertIn("startErrorReport()", app_js)


if __name__ == "__main__":
    unittest.main()
