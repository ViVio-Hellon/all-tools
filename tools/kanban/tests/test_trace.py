"""記録(後追い・なぜなぜ分析。kanban/trace.py)"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

from kanban import applog, config, trace

from test_web_app import RouteTestBase


class TraceFixture(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.logdir = Path(tempfile.mkdtemp(prefix="kanban_trace_"))
        self.addCleanup(shutil.rmtree, self.logdir, True)
        saved = (trace._dir, dict(trace._context), trace._state_provider)

        def restore():
            applog.close()
            trace._dir, trace._state_provider = saved[0], saved[2]
            trace._context.clear()
            trace._context.update(saved[1])

        self.addCleanup(restore)
        applog.initialize(str(self.logdir), line_name="LVC")
        trace._repeats.clear()
        trace.set_context(pc="PC-ONE", login="yamada", mode="site", line="LVC", version="VER9")

    def records(self, **kw):
        q = trace.Query(since=date.today() - timedelta(days=1), until=date.today(),
                        kinds=frozenset(trace.KIND_LABEL), **kw)
        return trace.search(q)


class WriteReadTest(TraceFixture):
    def test_a_record_carries_who_where_and_when(self):
        ref = trace.write("refused", "パスが空です", level=trace.LEVEL_WARNING, where="POST /api/x",
                          request={"path": "/api/x", "body": {"path": ""}},
                          response={"status": 400, "code": "bad_path", "message": "パスが空です"})
        r = self.records()[0]
        self.assertEqual(r["ref"], ref)
        for key, value in (("pc", "PC-ONE"), ("login", "yamada"), ("mode", "site"), ("line", "LVC"),
                           ("version", "VER9")):
            self.assertEqual(r[key], value)
        files = list((self.logdir / "KanbanSystem" / "記録").rglob("*.jsonl"))
        self.assertEqual([f.name for f in files], [f"PC-ONE_{date.today():%Y%m%d}.jsonl"])

    def test_logged_exceptions_keep_the_chain_of_causes(self):
        """``log.exception`` がそのまま、原因の連鎖つきの記録になる(なぜなぜの材料)。"""
        trace.set_state_provider(lambda: {"未反映": 3})
        try:
            try:
                {}["看板_LVC"]
            except KeyError as inner:
                raise RuntimeError("看板の表が読めません") from inner
        except RuntimeError:
            applog.get_logger("test").exception("取り込みに失敗")
        r = [x for x in self.records() if x["kind"] == "error"][0]
        chain = r["exception"]["chain"]
        self.assertEqual([c["type"] for c in chain], ["RuntimeError", "KeyError"])
        self.assertIn("test_trace.py", chain[0]["at"])
        self.assertEqual(r["state"], {"未反映": 3})
        self.assertIn("Traceback", r["exception"]["traceback"])
        # テキストのログにも重さと同じ記録番号
        text = applog.log_path().read_text(encoding="utf-8")
        self.assertIn(f"| ERROR   | kanban.test | 取り込みに失敗  [記録 {r['ref']}]", text)

    def test_warnings_from_background_work_are_recorded(self):
        applog.warning("共有DBに届きません: %s", r"\\server\share")
        r = self.records()[0]
        self.assertEqual((r["kind"], r["level"]), ("warning", "WARNING"))
        self.assertIn("共有DBに届きません", r["what"])

    def test_where_is_the_caller_not_the_log_helper(self):
        applog.warning("届きません")
        self.assertIn("test_trace.py", self.records()[0]["where"])

    def test_the_same_background_warning_is_thinned_out(self):
        """取り込みのたびに出る同じ警告で記録を埋めない。間引いた数は次の 1 件に添える。"""
        def import_once():   # 取り込みの周期と同じく、同じ場所から同じ警告
            applog.warning("共有DBに届きません")

        for _ in range(5):
            import_once()
        self.assertEqual(len(self.records()), 1)
        for key in trace._repeats:
            trace._repeats[key][0] -= trace.REPEAT_SEC + 1
        import_once()
        rows = self.records()
        self.assertEqual(len(rows), 2)
        noted = [r["extra"].get("前の記録からの同じ警告", "") for r in rows]
        self.assertIn("4 回(書かずに数えた)", noted)

    def test_info_is_not_recorded(self):
        applog.info("ふつうのこと")
        self.assertEqual(self.records(), [])

    def test_passwords_are_never_recorded(self):
        cleaned = trace.clean_input({"password": "秘密", "password_confirm": "秘密", "new_password": "x",
                                     "path": "a" * 400, "nested": {"current_password": "y"}})
        self.assertEqual(cleaned["password"], "(伏せました)")
        self.assertEqual(cleaned["nested"]["current_password"], "(伏せました)")
        self.assertNotIn("秘密", json.dumps(cleaned, ensure_ascii=False))
        self.assertTrue(cleaned["path"].endswith("(400 文字)"))

    def test_find_by_ref_with_the_operations_before_it(self):
        trace.write("operation", "POST /api/board/order", where="POST /api/board/order")
        trace.write("operation", "POST /api/board/ship", where="POST /api/board/ship")
        ref = trace.write("error", "書き戻しに失敗", level=trace.LEVEL_ERROR,
                          exception=trace.describe_exception(ValueError("x")))
        found = trace.find(ref)
        self.assertEqual(found["record"]["ref"], ref)
        self.assertEqual([t["what"] for t in found["trail"]],
                         ["POST /api/board/order", "POST /api/board/ship"])
        sheet = trace.why_sheet(found)
        for part in ("なぜなぜ分析シート", ref, "PC-ONE", "なぜ1: 書き戻しに失敗", "なぜ5:", "【真因】",
                     "【対策】", "POST /api/board/ship", "ValueError: x"):
            self.assertIn(part, sheet)

    def test_search_filters(self):
        trace.write("refused", "断った", level=trace.LEVEL_WARNING)
        trace.write("operation", "押した")
        trace.set_context(pc="PC-TWO")
        trace.write("error", "べつの端末で", level=trace.LEVEL_ERROR)
        q = dict(since=date.today(), until=date.today())
        self.assertEqual(len(trace.search(trace.Query(**q))), 2, "操作は既定で出さない")
        self.assertEqual([r["what"] for r in trace.search(trace.Query(**q, pc="PC-ONE"))], ["断った"])
        self.assertEqual([r["what"] for r in trace.search(trace.Query(**q, text="べつ"))], ["べつの端末で"])
        self.assertEqual(trace.pcs(), ["PC-ONE", "PC-TWO"])

    def test_prune_only_this_pcs_old_files(self):
        root = trace.directory()
        old = (date.today() - timedelta(days=trace.KEEP_DAYS + 10))
        folder = root / old.strftime("%Y%m")
        folder.mkdir(parents=True)
        mine = folder / f"PC-ONE_{old:%Y%m%d}.jsonl"
        other = folder / f"PC-TWO_{old:%Y%m%d}.jsonl"
        mine.write_text("{}\n", encoding="utf-8")
        other.write_text("{}\n", encoding="utf-8")
        self.assertEqual(trace.prune(), 1)
        self.assertFalse(mine.exists())
        self.assertTrue(other.exists(), "ほかの PC の記録を消した")

    def test_reinitialize_moves_the_logs(self):
        new = self.logdir / "共有" / "ログ"
        new.mkdir(parents=True)
        applog.reinitialize(str(new))
        self.assertTrue(str(applog.log_path()).startswith(str(new)))
        ref = trace.write("refused", "移ったあと", level=trace.LEVEL_WARNING)
        self.assertTrue(list((new / "KanbanSystem" / "記録").rglob("*.jsonl")))
        self.assertEqual(trace.find(ref)["record"]["what"], "移ったあと")


class RouteTraceTest(RouteTestBase):
    """画面の操作が記録になり、エラーの応答に記録番号が付く。"""

    def setUp(self) -> None:
        self.logdir = Path(tempfile.mkdtemp(prefix="kanban_trace_routes_"))
        super().setUp()
        self.addCleanup(shutil.rmtree, self.logdir, True)
        saved = (trace._dir, dict(trace._context))

        def restore():
            applog.close()
            trace._dir = saved[0]
            trace._context.clear()
            trace._context.update(saved[1])

        self.addCleanup(restore)
        applog.initialize(str(self.logdir), line_name="LVC")
        trace.set_context(pc="PC-ONE", login="yamada", mode="site", line="LVC")
        (self.dir / "config.json").write_text(json.dumps({
            "admin_password_hash": config.hash_password("秘密"), "line": "LVC",
            "log_dir": str(self.logdir)}), encoding="utf-8")

        def boom():
            try:
                int("十")
            except ValueError as exc:
                raise RuntimeError("数に直せませんでした") from exc

        self.app.add_url_rule("/api/test-boom", "boom", boom, methods=["POST"])

    def search(self, kinds="error,warning,refused,client,operation"):
        return self.get(f"/api/trace?kinds={kinds}&pc=@me").json["records"]

    def test_refusal_has_a_ref_and_is_recorded_without_the_password(self):
        res = self.post("/api/line", {"line": "LS", "password": "ちがう"})
        self.assertEqual(res.status_code, 401)
        ref = res.json["error"]["ref"]
        self.assertTrue(ref.startswith("R-"))
        self.assertEqual(res.headers["X-Record-Ref"], ref)
        found = self.get(f"/api/trace/item?ref={ref}").json
        r = found["record"]
        self.assertEqual((r["kind"], r["response"]["code"]), ("refused", "bad_password"))
        self.assertEqual(r["request"]["body"]["password"], "(伏せました)")
        self.assertNotIn("ちがう", json.dumps(found, ensure_ascii=False))

    def test_unexpected_error_tells_the_ref_and_keeps_the_causes(self):
        res = self.post("/api/test-boom", {"x": 1})
        self.assertEqual(res.status_code, 500)
        ref = res.json["error"]["ref"]
        self.assertIn(ref, res.json["error"]["message"])
        found = self.get(f"/api/trace/item?ref={ref}").json
        self.assertEqual([c["type"] for c in found["record"]["exception"]["chain"]],
                         ["RuntimeError", "ValueError"])
        self.assertEqual(found["record"]["request"]["path"], "/api/test-boom")
        self.assertIn("なぜ1", found["sheet"])
        kinds = [r["kind"] for r in self.search() if r["ref"] == ref]
        self.assertEqual(kinds, ["error"], "500 を断った操作としても二重に残した")

    def test_successful_operations_leave_footprints(self):
        res = self.post("/api/line", {"line": "LS", "password": "秘密"})
        self.assertEqual(res.status_code, 200)
        ops = [r for r in self.search("operation") if r["path"] == "/api/line"]
        self.assertEqual(len(ops), 1)

    def test_client_errors_are_recorded(self):
        res = self.post("/api/client-log", {"message": "x is undefined", "source": "/static/js/views/board.js",
                                            "line": 120, "col": 5, "stack": "at render", "page": "/board"})
        self.assertTrue(res.json["ok"])
        r = [x for x in self.search("client")][0]
        self.assertEqual((r["kind_label"], r["what"]), ("画面のエラー", "x is undefined"))

    def test_csv_of_records(self):
        self.post("/api/line", {"line": "LS", "password": "ちがう"})
        res = self.get("/api/trace/csv?kinds=refused&pc=@me")
        text = res.get_data().decode("utf-8-sig")
        self.assertTrue(text.startswith("記録番号,日時,種類"))
        self.assertIn("bad_password", text)

    def test_log_dir_needs_the_password_and_switches_now(self):
        new = self.logdir / "共有フォルダ"
        new.mkdir()
        self.assertEqual(self.post("/api/log-dir", {"path": str(new)}).status_code, 401)
        self.assertEqual(self.post("/api/log-dir", {"path": str(self.logdir / "無い"),
                                                    "password": "秘密"}).status_code, 400)
        res = self.post("/api/log-dir", {"path": str(new), "password": "秘密"})
        self.assertEqual(res.status_code, 200, res.json)
        self.assertTrue(res.json["text_log"].startswith(str(new)))
        self.assertEqual(config.load_config(str(self.dir / "config.json")).log_dir, str(new))
        self.post("/api/line", {"line": "LS", "password": "ちがう"})
        self.assertTrue(list((new / "KanbanSystem" / "記録").rglob("*.jsonl")), "新しい場所に書いていない")

    def test_settings_page_has_the_records_tab(self):
        html = self.get("/settings").get_data(as_text=True)
        for part in ('id="tab-trace"', 'id="trace-sheet"', 'id="log-dir-card"', "記録番号で開く"):
            self.assertIn(part, html)


if __name__ == "__main__":
    unittest.main()
