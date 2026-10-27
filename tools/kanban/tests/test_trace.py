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
        files = list((self.logdir / "kanban" / "記録").rglob("*.jsonl"))
        self.assertEqual([f.name for f in files], [f"kanban_記録_PC-ONE_{date.today():%Y%m%d}.jsonl"])

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
        mine = folder / f"kanban_記録_PC-ONE_{old:%Y%m%d}.jsonl"
        other = folder / f"kanban_記録_PC-TWO_{old:%Y%m%d}.jsonl"
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
        self.assertTrue(list((new / "kanban" / "記録").rglob("kanban_記録_*.jsonl")))
        self.assertEqual(trace.find(ref)["record"]["what"], "移ったあと")


def _old_record(logdir: Path, pc: str, at: datetime, what: str, ref: str, kind: str = "error") -> Path:
    """VER3.5.9 までの形(``KanbanSystem/記録/<年月>/<PC名>_<年月日>.jsonl``)で 1 件置く。"""
    path = logdir / "KanbanSystem" / "記録" / f"{at:%Y%m}" / f"{pc}_{at:%Y%m%d}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ref": ref, "at": f"{at:%Y/%m/%d %H:%M:%S}.000", "level": "ERROR", "kind": kind,
           "where": "前の版", "what": what, "pc": pc, "login": "old", "mode": "site", "line": "LVC",
           "version": "VER3.5.9", "pid": 1}
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return path


#: ほかのツールのファイル(共有フォルダに並ぶもの)。看板は読まない・消さない
def _other_tools_files(logdir: Path, day: date) -> list[Path]:
    month = f"{day:%Y%m}"
    rec = json.dumps({"ref": "E-0101-000000-zzzz", "at": f"{day:%Y/%m/%d} 00:00:00.000", "level": "ERROR",
                      "kind": "error", "what": "ほかのツールの記録", "pc": "PC-ONE"}, ensure_ascii=False) + "\n"
    paths = [
        logdir / f"dbviewer_PC-ONE_{day:%Y%m%d}.log",
        logdir / "nippou.log",
        logdir / f"出来事_{day:%Y%m%d}_PC-ONE.jsonl",
        logdir / f"記録_PC-ONE_{day:%Y%m%d}.jsonl",
        logdir / "nippou" / f"nippou_記録_PC-ONE_{day:%Y%m%d}.jsonl",
        logdir / "kanban" / "記録" / month / f"dbviewer_PC-ONE_{day:%Y%m%d}.log",
        logdir / "kanban" / "記録" / month / f"記録_PC-ONE_{day:%Y%m%d}.jsonl",
        logdir / "kanban" / "記録" / month / f"nippou_記録_PC-ONE_{day:%Y%m%d}.jsonl",
        logdir / "KanbanSystem" / "記録" / month / f"nippou_記録_PC-ONE_{day:%Y%m%d}.jsonl",
        logdir / "KanbanSystem" / "記録" / month / f"dbviewer_PC-ONE_{day:%Y%m%d}.log",
    ]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rec, encoding="utf-8")
    return paths


class LayoutTest(TraceFixture):
    """**ツール名のフォルダ(kanban)の下に、名前がツール名(kanban_)で始まるファイルで書く**(VER3.5.10)。

    共有フォルダにはほかのツールのログも同じ階層に並ぶ(``dbviewer_PC_日付.log``・``nippou.log``・
    ``記録_PC_日付.jsonl`` …)。取り出して手元に置いても、開かずにどのツールのものかわかるように。
    前の版の場所(``KanbanSystem/…``)の記録も読み、消すのは看板自身のファイルだけ。
    """

    def written_files(self, base: Path) -> list[Path]:
        return [p for p in base.rglob("*") if p.is_file()]

    def test_logs_and_records_go_under_the_tool_folder_with_the_tool_name(self):
        log = applog.log_path()
        self.assertEqual(log.parent, self.logdir / "kanban" / "LVC")
        self.assertRegex(log.name, r"^kanban_DebugLog_\d{8}_\d{6}_.+\.txt$")
        self.assertTrue(log.stem.endswith("_" + applog.pc_name()), "名前に PC 名が無い(同じ秒に開いた 2 台が混ざる)")
        self.assertEqual(trace.directory(), self.logdir / "kanban" / "記録")
        today = datetime.now()
        trace.write("refused", "断った", level=trace.LEVEL_WARNING)
        applog.warning("届きません")
        self.assertTrue((self.logdir / "kanban" / "記録" / f"{today:%Y%m}"
                         / f"kanban_記録_PC-ONE_{today:%Y%m%d}.jsonl").is_file())
        files = self.written_files(self.logdir)
        self.assertTrue(files)
        for f in files:
            with self.subTest(file=str(f.relative_to(self.logdir))):
                self.assertEqual(f.relative_to(self.logdir).parts[0], "kanban", "ツール名のフォルダの外に書いた")
                self.assertTrue(f.name.startswith("kanban_"), "名前がツール名で始まらない")
        self.assertFalse((self.logdir / "KanbanSystem").exists(), "前の形の場所を作った")

    def test_default_place_has_the_same_shape(self):
        """出力先を設定していない(この端末の logs)ときも同じ形。"""
        local = self.logdir / "ローカル領域"
        with mock.patch.dict(os.environ, {"KANBAN_LOCAL_DIR": str(local)}):
            cfg = config.Config()
            self.assertEqual(cfg.log_dir, "")
            path = applog.initialize(cfg.resolved_log_dir(), line_name="LS")
            self.assertEqual(trace.root_for(cfg.resolved_log_dir()), local / "logs" / "kanban" / "記録")
        self.assertEqual(path.parent, local / "logs" / "kanban" / "LS")
        self.assertTrue(path.name.startswith("kanban_DebugLog_"))
        self.assertEqual(trace.directory(), local / "logs" / "kanban" / "記録")

    def test_fallback_when_the_place_cannot_be_written_has_the_same_shape(self):
        """書けないときの代わりの場所(一時フォルダ)も ``<一時フォルダ>/kanban/<ライン>/kanban_…``。"""
        blocked = self.logdir / "ファイルなのでフォルダを作れない"
        blocked.write_text("", encoding="utf-8")
        tmp = self.logdir / "一時"
        tmp.mkdir()
        with mock.patch("tempfile.gettempdir", return_value=str(tmp)):
            path = applog.initialize(str(blocked), line_name="LVC")
        self.assertEqual(path.parent, tmp / "kanban" / "LVC")
        self.assertTrue(path.name.startswith("kanban_DebugLog_"))
        self.assertEqual(trace.directory(), tmp / "kanban" / "記録")
        self.assertEqual([p.name for p in tmp.iterdir()], ["kanban"])

    def test_changing_the_place_keeps_the_shape(self):
        new = self.logdir / "共有" / "ログ"
        new.mkdir(parents=True)
        path = applog.reinitialize(str(new))
        self.assertEqual(path.parent, new / "kanban" / "LVC")
        self.assertTrue(path.name.startswith("kanban_DebugLog_"))
        trace.write("refused", "移ったあと", level=trace.LEVEL_WARNING)
        for f in self.written_files(new):
            self.assertEqual(f.relative_to(new).parts[0], "kanban")
            self.assertTrue(f.name.startswith("kanban_"), f)

    def test_records_written_by_the_previous_version_are_still_found(self):
        """更新前に ``KanbanSystem/記録/<年月>/<PC名>_<年月日>.jsonl`` へ書いた記録も探せる・引ける・なぜなぜが出る。"""
        now = datetime.now()
        _old_record(self.logdir, "PC-OLD", now, "前の版で起きた", "E-0101-000000-aaaa")
        _old_record(self.logdir, "PC-ONE", now, "この端末の前の版で", "E-0101-000000-bbbb")
        trace.write("error", "新しい版で", level=trace.LEVEL_ERROR)
        whats = sorted(r["what"] for r in self.records())
        self.assertEqual(whats, ["この端末の前の版で", "前の版で起きた", "新しい版で"])
        self.assertEqual([r["what"] for r in self.records(pc="PC-OLD")], ["前の版で起きた"])
        self.assertEqual(sorted(r["what"] for r in self.records(pc="PC-ONE")), ["この端末の前の版で", "新しい版で"])
        self.assertEqual(trace.pcs(), ["PC-OLD", "PC-ONE"])
        found = trace.find("E-0101-000000-aaaa")
        self.assertIsNotNone(found, "前の版の記録を記録番号で引けない")
        self.assertIn("KanbanSystem", found["file"])
        self.assertIn("なぜ1: 前の版で起きた", trace.why_sheet(found))
        # 前の場所には書き足さない
        self.assertEqual(len(list((self.logdir / "KanbanSystem").rglob("*.jsonl"))), 2)

    def test_other_tools_files_are_neither_read_nor_removed(self):
        today = date.today()
        old = today - timedelta(days=trace.KEEP_DAYS + 10)
        others = _other_tools_files(self.logdir, today) + _other_tools_files(self.logdir, old)
        self.assertEqual(self.records(), [], "ほかのツールのファイルを看板の記録として読んだ")
        self.assertEqual(trace.pcs(), [])
        self.assertIsNone(trace.find("E-0101-000000-zzzz"))
        self.assertEqual(trace.prune(), 0)
        for path in others:
            self.assertTrue(path.exists(), f"ほかのツールのファイルを消した: {path}")

    def test_prune_removes_only_this_pcs_old_kanban_records_in_both_places(self):
        old = datetime.now() - timedelta(days=trace.KEEP_DAYS + 10)
        root = trace.directory()
        new_mine = root / f"{old:%Y%m}" / f"kanban_記録_PC-ONE_{old:%Y%m%d}.jsonl"
        new_other = root / f"{old:%Y%m}" / f"kanban_記録_PC-TWO_{old:%Y%m%d}.jsonl"
        # 名前が前方一致するだけの、ほかの端末(PC-ONE_X)の分
        new_similar = root / f"{old:%Y%m}" / f"kanban_記録_PC-ONE_X_{old:%Y%m%d}.jsonl"
        for f in (new_mine, new_other, new_similar):
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("{}\n", encoding="utf-8")
        old_mine = _old_record(self.logdir, "PC-ONE", old, "古い", "E-0101-000000-cccc")
        old_other = _old_record(self.logdir, "PC-TWO", old, "古い", "E-0101-000000-dddd")
        old_similar = _old_record(self.logdir, "PC-ONE_X", old, "古い", "E-0101-000000-eeee")
        recent = _old_record(self.logdir, "PC-ONE", datetime.now(), "新しい", "E-0101-000000-ffff")
        others = _other_tools_files(self.logdir, old.date())
        self.assertEqual(trace.prune(), 2)
        self.assertFalse(new_mine.exists())
        self.assertFalse(old_mine.exists(), "前の場所の古い記録を片付けていない")
        for f in (new_other, new_similar, old_other, old_similar, recent, *others):
            self.assertTrue(f.exists(), f"消してはいけないものを消した: {f}")


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
        self.assertTrue(list((new / "kanban" / "記録").rglob("kanban_記録_*.jsonl")), "新しい場所に書いていない")
        self.assertFalse((new / "KanbanSystem").exists(), "前の形の場所に書いた")

    def test_records_tab_reads_the_previous_versions_records(self):
        """設定の「記録」タブ(``/api/trace``・``/api/trace/item``)が、更新前の場所の記録も出す。"""
        now = datetime.now()
        _old_record(self.logdir, "PC-ONE", now, "前の版で起きた", "E-0101-000000-aaaa")
        _other_tools_files(self.logdir, now.date())
        self.post("/api/line", {"line": "LS", "password": "ちがう"})
        rows = self.search()
        whats = [r["what"] for r in rows]
        self.assertIn("前の版で起きた", whats)
        self.assertNotIn("ほかのツールの記録", whats)
        found = self.get("/api/trace/item?ref=E-0101-000000-aaaa")
        self.assertEqual(found.status_code, 200)
        self.assertIn("なぜ1: 前の版で起きた", found.json["sheet"])
        state = self.get("/api/log-dir").json
        self.assertEqual(state["records"], str(self.logdir / "kanban" / "記録"))
        self.assertEqual(state["records_old"], str(self.logdir / "KanbanSystem" / "記録"))

    # 設定画面の「記録」の面(開くと探す・行を押すか記録番号を入れるとなぜなぜ分析シートの下書きが出る)は、
    # 字を探すのではなく**本物のブラウザで押して確かめます**(`test_settings_cards_browser.TraceTabTest`)。
    # 記録の置き場所の欄(log-dir-card)は `test_settings_places_browser` で変えて確かめます。


if __name__ == "__main__":
    unittest.main()
