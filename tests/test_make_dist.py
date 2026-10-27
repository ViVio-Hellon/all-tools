"""日報複合ツール一式の配布用フォルダ(`scripts/make_dist.py`)

- 配るものだけを写す(tests・src-tauri・__pycache__・.git は入れない)
- 各ツールは、そのツールの make_dist の決まりのまま `tools\\<ツール>\\` へ
- デスクトップ版の exe は、あれば 日報複合ツール.exe の名前で直下に入れる
- 直下に何かを足したら、配るかどうかを決めさせる(一覧と食い違えば落ちる)
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Callable, Optional

import tests  # noqa: F401  (一時フォルダへ向ける)

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("alltools_make_dist", ROOT / "scripts" / "make_dist.py")
make_dist = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(make_dist)


class BuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="alltools_dist_"))
        exe = cls.tmp / "AllTools.exe"
        exe.write_bytes(b"MZ-fake")
        cls.out, cls.lines = make_dist.build(cls.tmp / "dist", exe=exe)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_入口と4ツールが入る(self) -> None:
        for name in make_dist.INCLUDE:
            self.assertTrue((self.out / name).exists(), name)
        for tool in ("nippou", "kanban", "calendar", "inspection"):
            with self.subTest(tool=tool):
                for name in ("bridge.py", "start_app.py", "config/app.json", "app/static"):
                    self.assertTrue((self.out / "tools" / tool / name).exists(), name)
                self.assertFalse((self.out / "tools" / tool / "配布メモ.txt").exists(),
                                 "ツールの配布メモは一式の配布メモにまとめる")

    def test_ショートカットを作るものが入り_配布メモに書く(self) -> None:
        self.assertTrue((self.out / "scripts" / "make_shortcuts.vbs").is_file())
        memo = (self.out / "配布メモ.txt").read_text(encoding="utf-8-sig")
        self.assertIn(r"scripts\make_shortcuts.vbs", memo)

    def test_exeは配る名前で直下に入る(self) -> None:
        self.assertEqual((self.out / make_dist.EXE_NAME).read_bytes(), b"MZ-fake")
        memo = (self.out / "配布メモ.txt").read_text(encoding="utf-8-sig")
        self.assertIn("デスクトップ版を入れました", memo)
        self.assertIn("[看板] VER", memo)
        self.assertIn("\r\n", (self.out / "配布メモ.txt").read_bytes().decode("utf-8-sig"))

    def test_配ってはいけないものが入らない(self) -> None:
        for pattern in ("tests", "src-tauri", "__pycache__", "*.pyc", ".git*", "*.sqlite3"):
            with self.subTest(pattern=pattern):
                self.assertEqual(list(self.out.rglob(pattern)), [])
        for name in make_dist.FORBIDDEN:
            self.assertFalse((self.out / name).exists(), name)


class RuleTests(unittest.TestCase):
    def test_exeが無ければブラウザ版で動くと書く(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out, lines = make_dist.build(Path(tmp) / "dist", exe=Path(tmp) / "無い.exe")
            self.assertFalse((out / make_dist.EXE_NAME).exists())
            self.assertIn("入っていません", "\n".join(lines))

    def test_一式の中には作らない_中身のある場所は作り直すと言われたときだけ(self) -> None:
        with self.assertRaises(SystemExit):
            make_dist.build(ROOT / "dist")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "dist"
            out.mkdir()
            (out / "前の.txt").write_text("x", encoding="utf-8")
            with self.assertRaises(SystemExit):
                make_dist.build(out)
            self.assertTrue((out / "前の.txt").exists())

    def test_直下の一覧と食い違わない(self) -> None:
        """直下に何かを足したら、**配るかどうかを決めさせる**(INCLUDE か DEV_ONLY へ)。"""
        done = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True,
                              encoding="utf-8", check=True)
        tracked = {line.split("/")[0].strip('"') for line in done.stdout.splitlines()}
        tracked = {t for t in tracked if t}
        undecided = tracked - set(make_dist.INCLUDE) - set(make_dist.DEV_ONLY)
        self.assertEqual(undecided, set(), "配るかどうか決まっていない")
        self.assertEqual(set(make_dist.INCLUDE) & set(make_dist.DEV_ONLY), set())

    def test_ツールの一覧と食い違わない(self) -> None:
        import json
        tools = json.loads((ROOT / "config" / "tools.json").read_text(encoding="utf-8"))["tools"]
        for tool in tools:
            with self.subTest(tool=tool["id"]):
                self.assertTrue(tool["id"] in make_dist.TOOL_BUILDERS or tool["id"] in make_dist.GENERIC_INCLUDE)

    def test_make_distを持たないツールの一覧はそのツールの直下と食い違わない(self) -> None:
        for tool_id, include in make_dist.GENERIC_INCLUDE.items():
            done = subprocess.run(["git", "ls-files", f"tools/{tool_id}"], cwd=ROOT, capture_output=True,
                                  text=True, encoding="utf-8", check=True)
            tracked = {line.split("/")[2].strip('"') for line in done.stdout.splitlines() if line.count("/") >= 2}
            dev_only = {"tests", ".gitignore", ".gitattributes"}
            with self.subTest(tool=tool_id):
                self.assertEqual(set(include), tracked - dev_only)


# ------------------------------------------------------------------
# 配った先で、大設定と4ツールが配布設定を読み込む(作る → 配る → 起動する を通しで)
# ------------------------------------------------------------------
#: 試験の子のプロセスに持ち込まない環境変数の頭(この試験の差し替え・ほかの試験の差し替え)
_TOOL_ENV_PREFIXES = ("ALLTOOLS_", "NIPPOU_", "KANBAN_", "CALENDAR_", "INSPECTION_")

#: 一式を写すときに外すもの(開発にしか使わない・作ったもの・**本物の配布設定**)
_COPY_SKIP = {".git", "src-tauri", "tests", "__pycache__", "logs", "配布設定", "配布設定.作成中",
              ".github", ".pytest_cache"}


class _Delivered(unittest.TestCase):
    """一式を一時フォルダへ写し、配布設定を用意して(``prepare``)make_dist で配布用フォルダを作る。

    端末(利用者ごとの領域)を作る・入口と4ツールを外枠と同じ形で起こす・読み込んだかを見る、の道具。
    試験は持たない(下の2つが使う)。
    """

    @classmethod
    def prepare(cls, source: Path) -> None:
        """写した一式(``source``)に、配る元で書き出した配布設定を置く。"""
        raise NotImplementedError

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="alltools_delivered_"))
        source = cls.tmp / "一式"

        def ignore(directory: str, names: list[str]) -> set[str]:
            return {n for n in names if n in _COPY_SKIP or n.endswith((".pyc", ".sqlite3"))}

        shutil.copytree(ROOT, source, ignore=ignore)
        # 点検表は Windows では利用者ごとの名前付きの錠(app_id から作る)で同時起動を見る。
        # 試験を流す PC で本物の点検表が動いていても取り合わないよう、写しの名前を変えておく
        app_json = source / "tools" / "inspection" / "config" / "app.json"
        conf = json.loads(app_json.read_text(encoding="utf-8-sig"))
        conf["app_id"] = f"{conf.get('app_id', 'inspection')}.delivered-test"
        app_json.write_text(json.dumps(conf, ensure_ascii=False, indent=2), encoding="utf-8")
        share = cls.tmp / "共有"
        cls.expected = {
            "portal": str(share / "大設定"),
            "nippou": str(share / "日報"),
            "kanban": str(share / "看板" / "看板マスタ.sqlite3"),
            "calendar": str(share / "カレンダー"),
            "inspection": str(share / "点検表"),
        }
        cls.prepare(source)
        cls.dist, cls.lines = make_dist.build(cls.tmp / "配った一式", exe=cls.tmp / "無い.exe", root=source)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # --- 端末 -------------------------------------------------------
    @classmethod
    def terminal(cls, name: str, root: Optional[Path] = None, *, shell_like: bool = False) -> dict[str, str]:
        """何も設定していない端末(利用者ごとの領域を一時フォルダへ)。``root`` は一式の場所(既定は配った一式)。

        ``shell_like`` なら、外枠(日報複合ツール.exe)が渡すのと同じく ``LOCALAPPDATA`` / ``APPDATA``
        だけを差し替え、各ツールの手元の領域の指定(``<頭>_LOCAL_DIR`` など)は渡さない。
        """
        base = cls.tmp / "端末" / name
        env = {k: v for k, v in os.environ.items() if not k.startswith(_TOOL_ENV_PREFIXES)}
        env.update({
            "LOCALAPPDATA": str(base / "Local"), "APPDATA": str(base / "Roaming"),
            "XDG_DATA_HOME": str(base / "Local"),
            "ALLTOOLS_LOG_QUIET": "1", "ALLTOOLS_LOGIN_ID": "tester", "ALLTOOLS_PC_NAME": "DELIVERED-PC",
            "ALLTOOLS_SHELL": "1", "ALLTOOLS_ROOT": str(root or cls.dist),
            "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1",
        })
        if not shell_like:
            env.update({
                "ALLTOOLS_LOCAL_DIR": str(base / "AllTools"),
                "NIPPOU_LOCAL_DIR": str(base / "NippouTool"),
                "KANBAN_LOCAL_DIR": str(base / "KanbanSystem"),
                "KANBAN_CONFIG": str(base / "Roaming" / "KanbanSystem" / "config.json"),
                "CALENDAR_LOCAL_DIR": str(base / "LineCalendar"),
                "INSPECTION_LOCAL_DIR": str(base / "InspectionSheetPrint"),
            })
        return env

    @staticmethod
    def settings_file(env: dict[str, str], tool: str) -> tuple[Path, str]:
        """その端末の設定ファイルと、配布設定の項目(試験で見る1つ)の鍵。

        手元の領域の指定が無ければ(``shell_like``)、各ツールが決めるのと同じ
        ``%LOCALAPPDATA%\\<名前>`` / ``%APPDATA%\\KanbanSystem``。
        """
        local = Path(env["LOCALAPPDATA"])

        def root(var: str, name: str) -> Path:
            return Path(env[var]) if env.get(var) else local / name

        return {
            "portal": (root("ALLTOOLS_LOCAL_DIR", "AllTools") / "data" / "user_settings.json", "shared_db_dir"),
            "nippou": (root("NIPPOU_LOCAL_DIR", "NippouTool") / "runtime" / "user_config.json", "access_dir"),
            "kanban": (Path(env["KANBAN_CONFIG"]) if env.get("KANBAN_CONFIG")
                       else Path(env["APPDATA"]) / "KanbanSystem" / "config.json", "shared_db_path"),
            "calendar": (root("CALENDAR_LOCAL_DIR", "LineCalendar") / "data" / "settings.json", "保存用DBフォルダ"),
            "inspection": (root("INSPECTION_LOCAL_DIR", "InspectionSheetPrint") / "data" / "user_settings.json",
                           "root_folder"),
        }[tool]

    @classmethod
    def stored_values(cls, env: dict[str, str], tool: str) -> dict:
        """その端末の設定ファイルの中身(無い・読めなければ空)。"""
        path, _key = cls.settings_file(env, tool)
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    @classmethod
    def stored(cls, env: dict[str, str], tool: str) -> Optional[str]:
        """その端末の設定ファイルに入っている、配布設定の項目の値(無ければ None)。"""
        path, key = cls.settings_file(env, tool)
        try:
            return json.loads(path.read_text(encoding="utf-8-sig")).get(key)
        except (OSError, ValueError):
            return None

    def start_all(self, env: dict[str, str], done: Callable[[str], bool], limit: float = 90.0) -> dict[str, str]:
        """入口と4ツールを外枠と同じ形(bridge.py・標準入出力)で起こし、``done`` になるまで待って閉じる。

        戻り値は、うまくいかなかったときに見せる各プロセスの標準エラーの終わり。
        """
        prefixes = {"portal": "ALLTOOLS", "nippou": "NIPPOU", "kanban": "KANBAN",
                    "calendar": "CALENDAR", "inspection": "INSPECTION"}
        procs: dict[str, subprocess.Popen] = {}
        errs: dict[str, Path] = {}
        started: dict[str, threading.Event] = {}
        watchers: list[threading.Thread] = []
        for tool, prefix in prefixes.items():
            folder = self.dist if tool == "portal" else self.dist / "tools" / tool
            child = dict(env, ALLTOOLS_TOOL=tool, **{f"{prefix}_TOKEN": "delivered"})
            if tool == "inspection":
                child["INSPECTION_DEMO"] = "0"
            errs[tool] = self.tmp / f"{tool}-{id(env)}-{time.monotonic_ns()}.err"
            with errs[tool].open("wb") as err:
                procs[tool] = subprocess.Popen([sys.executable, "bridge.py"], cwd=folder, env=child,
                                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err)
            started[tool] = threading.Event()

            def watch(stream, flag=started[tool]) -> None:
                for line in stream:
                    if b'"started"' in line:
                        flag.set()

            watchers.append(threading.Thread(target=watch, args=(procs[tool].stdout,), daemon=True))
            watchers[-1].start()
        try:
            deadline = time.monotonic() + limit
            while time.monotonic() < deadline:
                if all(done(tool) for tool in procs) and all(f.is_set() for f in started.values()):
                    break
                if any(p.poll() is not None for p in procs.values()):
                    break
                time.sleep(0.25)
        finally:
            for proc in procs.values():
                try:
                    proc.stdin.close()            # 外枠が閉じた = 終わってよい
                except OSError:
                    pass
            for proc in procs.values():
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            for thread in watchers:
                thread.join(timeout=5)
            for proc in procs.values():
                proc.stdout.close()
        return {tool: errs[tool].read_text(encoding="utf-8", errors="replace")[-1500:] for tool in procs}

    def assert_read(self, env: dict[str, str], tails: dict[str, str]) -> None:
        for tool, value in self.expected.items():
            with self.subTest(tool=tool):
                self.assertEqual(self.stored(env, tool), value,
                                 f"{tool} が配布設定を読み込んでいない。\n--- 標準エラーの終わり ---\n{tails[tool]}")


class DeliveredTerminalTests(_Delivered):
    """配布設定を書き出した一式を make_dist で作り、**配った先の端末**で起動して読み込まれるか。

        配布設定し配布設定フォルダを入れて別PCに持ち込んだが全ツールにおいて何も反映されていない

    1か所でも食い違う(make_dist が写さない・ツールが別の場所を見る・起動のどこかが先に既定値を
    書いてしまい「その端末にすでにある」になる)と、配った先では**黙って**何も読まれない。
    外枠(日報複合ツール.exe)と同じく、各ツールの bridge.py を子として起こして確かめる。
    """

    @classmethod
    def prepare(cls, source: Path) -> None:
        # 配る元で書き出したもの(各ツールの設定画面・大設定の「配布設定を書き出す」と同じ形・同じ場所)
        bundles = {
            source / "配布設定" / "日報複合ツール.json": {"shared_db_dir": cls.expected["portal"]},
            source / "tools" / "nippou" / "配布設定" / "設定.json": {"access_dir": cls.expected["nippou"]},
            source / "tools" / "kanban" / "配布設定" / "設定.json": {"shared_db_path": cls.expected["kanban"]},
            source / "tools" / "calendar" / "配布設定" / "設定.json": {"保存用DBフォルダ": cls.expected["calendar"]},
            source / "tools" / "inspection" / "配布設定" / "設定.json": {"root_folder": cls.expected["inspection"]},
        }
        for path, settings in bundles.items():
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"format": 1, "created_at": "2026/10/13 08:00:00",
                                        "created_on": "配る元", "settings": settings},
                                       ensure_ascii=False, indent=2), encoding="utf-8")

    # --- 試験 -------------------------------------------------------
    def test_配布用フォルダに大設定と4ツールの配布設定が入る(self) -> None:
        self.assertTrue((self.dist / "配布設定" / "日報複合ツール.json").is_file())
        for tool in ("nippou", "kanban", "calendar", "inspection"):
            with self.subTest(tool=tool):
                self.assertTrue((self.dist / "tools" / tool / "配布設定" / "設定.json").is_file())
        memo = (self.dist / "配布メモ.txt").read_text(encoding="utf-8-sig")
        self.assertIn("大設定の配布設定を入れました", memo)
        self.assertNotIn("配布設定は入れていません", memo)

    def test_何も設定していない端末で起動すると全部読み込む(self) -> None:
        env = self.terminal("新しい端末")
        tails = self.start_all(env, lambda tool: self.stored(env, tool) == self.expected[tool])
        self.assert_read(env, tails)

    def test_配布設定を置く前に1度起動した端末でも読み込む(self) -> None:
        """先に一式だけ置いて起動してみた端末。そのとき既定値が書かれていると「すでにある」で読まれない。"""
        env = self.terminal("先に起動した端末")
        hidden = self.tmp / "預かり"
        folders = [self.dist / "配布設定"] + [self.dist / "tools" / t / "配布設定"
                                              for t in ("nippou", "kanban", "calendar", "inspection")]
        for folder in folders:
            target = hidden / folder.relative_to(self.dist)
            target.parent.mkdir(parents=True, exist_ok=True)
            folder.rename(target)
        try:
            settled = time.monotonic() + 8.0
            self.start_all(env, lambda tool: time.monotonic() > settled)
        finally:
            for folder in folders:
                (hidden / folder.relative_to(self.dist)).rename(folder)
        for tool in self.expected:
            self.assertIsNone(self.stored(env, tool), f"{tool}: 配布設定が無いのに値が入った")
        tails = self.start_all(env, lambda tool: self.stored(env, tool) == self.expected[tool])
        self.assert_read(env, tails)


class ExportAllDeliveredTests(_Delivered):
    """大設定の「4ツールの分もまとめて書き出す」→ make_dist → 配った先で5つとも読む、を通しで。

        大設定の「配布設定を書き出す」だけ押して、各ツールの設定画面の配布設定を書き出して
        いなかったため、配った先で4ツールとも何も反映されなかった

    配る元の端末で入口・4ツールの設定をしておき、写した一式で入口の ``export_all`` を
    (入口のプロセスと同じく)そのまま呼ぶ。各ツールは、そのツールのフォルダの
    ``export_distribution.py`` が子として書き出す。
    """

    @classmethod
    def prepare(cls, source: Path) -> None:
        env = cls.terminal("配る元", root=source)
        # 看板は管理者パスワードを入れずに配ると、書き出した直後の確認で断る(最初に押した人が決められるため)
        hashed = subprocess.run([sys.executable, "-c", "from kanban import config; print(config.hash_password('line2026'))"],
                                cwd=source / "tools" / "kanban", env=env, capture_output=True, text=True,
                                encoding="utf-8", check=True).stdout.strip()
        for tool, value in cls.expected.items():
            path, key = cls.settings_file(env, tool)
            values = {key: value}
            if tool == "kanban":
                values["admin_password_hash"] = hashed
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
        done = subprocess.run(
            [sys.executable, "-c", "import json, sys; from portal import distribution; "
                                   "sys.stdout.write(json.dumps(distribution.export_all(), ensure_ascii=False))"],
            cwd=source, env=env, capture_output=True, timeout=300)
        cls.outcome = json.loads(done.stdout.decode("utf-8") or "{}") if done.returncode == 0 else {
            "ok": False, "results": [], "error": done.stderr.decode("utf-8", errors="replace")[-1500:]}

    def test_まとめて書き出すと5つとも書き出したと出る(self) -> None:
        self.assertEqual([r["id"] for r in self.outcome.get("results", [])],
                         ["portal", "nippou", "kanban", "calendar", "inspection"], self.outcome.get("error"))
        for result in self.outcome["results"]:
            with self.subTest(tool=result["id"]):
                self.assertEqual(result["state"], "done", result["line"])
        self.assertTrue(self.outcome["ok"])

    def test_配布用フォルダに大設定と4ツールの配布設定が入る(self) -> None:
        self.assertTrue((self.dist / "配布設定" / "日報複合ツール.json").is_file())
        for tool in ("nippou", "kanban", "calendar", "inspection"):
            with self.subTest(tool=tool):
                self.assertTrue((self.dist / "tools" / tool / "配布設定" / "設定.json").is_file())
                self.assertTrue((self.dist / "tools" / tool / "export_distribution.py").is_file(),
                                "配った先の大設定からも、まとめて書き出せる")

    def test_配った先の端末で起動すると5つとも読み込む(self) -> None:
        env = self.terminal("配った先")
        tails = self.start_all(env, lambda tool: self.stored(env, tool) == self.expected[tool])
        self.assert_read(env, tails)


class _FromSource(_Delivered):
    """配る元の端末で各ツールの参照設定を決め、大設定の「4ツールの分もまとめて書き出す」で書き出す道具。

    端末は外枠(日報複合ツール.exe)が渡すのと同じ環境(``LOCALAPPDATA`` / ``APPDATA`` だけ)。
    試験は持たない(下の2つが使う)。
    """

    #: 見る参照設定(ツールごとに複数)。値は {"1回目" / "2回目"} で配る元を変える
    KEYS = {
        "portal": ("shared_db_dir",),
        "nippou": ("gw_reference_dir", "access_dir", "report_output_dir"),
        "kanban": ("shared_db_path", "history_db_path"),
        "calendar": ("保存用DBフォルダ", "マスタDBフォルダ"),
        "inspection": ("root_folder",),
    }

    @classmethod
    def values(cls, round_name: str) -> dict[str, dict[str, str]]:
        share = cls.tmp / "共有" / round_name
        out: dict[str, dict[str, str]] = {}
        for tool, keys in cls.KEYS.items():
            out[tool] = {}
            for key in keys:
                name = "看板マスタ.sqlite3" if key == "shared_db_path" else (
                    "看板履歴.sqlite3" if key == "history_db_path" else "")
                folder = share / tool / key
                out[tool][key] = str(folder / name) if name else str(folder)
        return out

    @classmethod
    def configure_source(cls, round_name: str) -> None:
        """配る元の端末の設定を変える(各ツールの設定画面で決めたのと同じ、端末の設定ファイル)。"""
        for tool, values in cls.values(round_name).items():
            path, _key = cls.settings_file(cls.pc1, tool)
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                data = {}
            data.update(values)
            if tool == "kanban":
                data.setdefault("admin_password_hash", cls.kanban_hash)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def export_all(cls) -> dict:
        done = subprocess.run(
            [sys.executable, "-c", "import json, sys; from portal import distribution; "
                                   "sys.stdout.write(json.dumps(distribution.export_all(), ensure_ascii=False))"],
            cwd=cls.source, env=cls.pc1, capture_output=True, timeout=300)
        if done.returncode != 0:
            return {"ok": False, "results": [], "error": done.stderr.decode("utf-8", errors="replace")[-1500:]}
        return json.loads(done.stdout.decode("utf-8") or "{}")

    @classmethod
    def start_source(cls, source: Path) -> None:
        cls.source = source
        cls.pc1 = cls.terminal("配る元", root=source, shell_like=True)
        cls.kanban_hash = subprocess.run(
            [sys.executable, "-c", "from kanban import config; print(config.hash_password('line2026'))"],
            cwd=source / "tools" / "kanban", env=cls.pc1, capture_output=True, text=True,
            encoding="utf-8", check=True).stdout.strip()

    def wait_for(self, env: dict[str, str], expected: dict[str, dict[str, str]]) -> Callable[[str], bool]:
        return lambda tool: all(self.stored_values(env, tool).get(k) == v for k, v in expected[tool].items())

    def assert_values(self, env: dict[str, str], expected: dict[str, dict[str, str]],
                      tails: dict[str, str], when: str) -> None:
        for tool, values in expected.items():
            stored = self.stored_values(env, tool)
            for key, value in values.items():
                with self.subTest(when=when, tool=tool, key=key):
                    self.assertEqual(stored.get(key), value,
                                     f"{when}: {tool} の {key} が配布設定の値になっていない。\n"
                                     f"--- 標準エラーの終わり ---\n{tails[tool]}")


class CombinedFileDeliveredTests(_FromSource):
    """**大設定の1つのファイルだけ**で4ツールに届くか(各ツールのフォルダの 設定.json を消して配る)。

        日報・看板・カレンダーの参照設定は大設定のファイルに入らず、各ツールのフォルダの別ファイルに入る
        ──「そりゃぁ反映しないわな」「両方やってください」

    配る元で「4ツールの分もまとめて書き出す」→ 各ツールのフォルダの 設定.json を消す → make_dist →
    配った先(外枠と同じ道: 各ツールの bridge.py)で4ツールの参照設定が配布設定の値になる。
    両方あるときは、作った時刻が新しいほうを使う(新しいフォルダのもの・古いフォルダのもの)。
    ツールのフォルダだけの道(単品で配る道)は ``DeliveredTerminalTests``。
    """

    TOOLS = ("nippou", "kanban", "calendar", "inspection")

    @classmethod
    def prepare(cls, source: Path) -> None:
        cls.start_source(source)
        cls.configure_source("1回目")
        cls.outcome = cls.export_all()
        for tool in cls.TOOLS:
            (source / "tools" / tool / "配布設定" / "設定.json").unlink()

    def tool_file(self, root: Path, tool: str) -> Path:
        return root / "tools" / tool / "配布設定" / "設定.json"

    def test_まとめたファイルに4ツールの分が入り_ツールのフォルダに無くても配った先で読む(self) -> None:
        self.assertTrue(self.outcome.get("ok"), self.outcome)
        data = json.loads((self.dist / "配布設定" / "日報複合ツール.json").read_text(encoding="utf-8-sig"))
        expected = self.values("1回目")
        for tool in self.TOOLS:
            with self.subTest(tool=tool):
                self.assertFalse(self.tool_file(self.dist, tool).exists(), "ツールのフォルダには無い")
                self.assertEqual({k: data["tools"][tool]["settings"].get(k) for k in expected[tool]}, expected[tool])
        self.assertIn("4ツールの分(", "\n".join(self.lines), "配布メモに4ツールの分が入っていると書く")
        env = self.terminal("配った先", shell_like=True)
        tails = self.start_all(env, self.wait_for(env, expected))
        self.assert_values(env, expected, tails, "まとめたファイルだけ")

    def test_両方あれば作った時刻が新しいほうを使う(self) -> None:
        """カレンダーはあとで設定画面から書き出し直した(フォルダが新しい)。点検表はフォルダに古いものが残っていた。"""
        out = self.tmp / "両方ある一式"
        if out.exists():
            shutil.rmtree(out)
        shutil.copytree(self.dist, out)
        expected = self.values("1回目")
        newer = {"保存用DBフォルダ": str(self.tmp / "フォルダの新しい値"),
                 "マスタDBフォルダ": str(self.tmp / "フォルダの新しいマスタ")}
        self.tool_file(out, "calendar").write_text(json.dumps(
            {"format": 1, "created_at": "2999/01/01 00:00:00", "created_on": "配る元", "settings": newer},
            ensure_ascii=False), encoding="utf-8")
        self.tool_file(out, "inspection").write_text(json.dumps(
            {"format": 1, "created_at": "2000-01-01 00:00:00", "created_on": "配る元",
             "settings": {"root_folder": str(self.tmp / "フォルダの古い値")}}, ensure_ascii=False), encoding="utf-8")
        expected["calendar"] = newer
        env = self.terminal("両方ある端末", root=out, shell_like=True)
        saved, type(self).dist = self.dist, out
        try:
            tails = self.start_all(env, self.wait_for(env, expected))
        finally:
            type(self).dist = saved
        self.assert_values(env, expected, tails, "両方ある")


class KeptLineAndSoundsDeliveredTests(_FromSource):
    """前に各ツールの設定画面で入れていた**ラインと日報の音声ファイル**が、まとめて書き出したあとも残り、
    配った先に届くか(通しで)。

        まとめて書き出したら、日報の設定画面で入れていた音声ファイル(配布設定\\音)とラインが消えた
        (既定の選び方で各ツールの配布設定を丸ごと書き直していた)→「音声ファイルとラインは残して書き出す」

    配る元: 各ツールの参照設定を決め、日報・看板・カレンダーの設定画面でラインを(日報は音声ファイルも)
    選んで書き出す → この端末のラインを変える(残すのは前に入れていた値)→ 大設定の「4ツールの分も
    まとめて書き出す」→ make_dist → 配った先(外枠と同じ道: 各ツールの bridge.py)でラインと音が届く。
    ツールのフォルダの 設定.json が無く、まとめたファイルの tools だけでも届く。
    """

    #: ツール → (この端末の設定ファイルの鍵, 前に入れたライン, そのあと配る元で変えたライン, 結果の一言)
    LINES = {
        "nippou": ("terminal_line", "L-1", "L-9", "音声ファイル 1 個・ライン(L-1)はそのまま残しました"),
        "kanban": ("line", "L-2", "L-8", "担当ライン(L-2)はそのまま残しました"),
        "calendar": ("PCライン", "L-3", "L-7", "ライン(L-3)はそのまま残しました"),
    }
    #: 設定画面で選んで書き出す(既定の選び方 + ライン。日報は音声ファイルも)
    SCREEN = {
        "nippou": ("from nippou import distribution as d; "
                   "r = d.export('', [i.key for i in d.ITEMS if i.default] + [d.ITEM_LINE], [d.FILE_SOUNDS], admin=True)"),
        "kanban": "from kanban import distribution as d; r = d.export([k for k, _, x in d.ITEMS if x] + ['line'])",
        "calendar": ("from calendar_app import distribution as d; "
                     "r = d.export('', [k for k, _, x in d.ITEMS if x] + ['PCライン'], admin=True)"),
    }
    SOUND = b"RIFF-kept-sound"

    @classmethod
    def set_lines(cls, which: int) -> None:
        for tool, spec in cls.LINES.items():
            path, _key = cls.settings_file(cls.pc1, tool)
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            data[spec[0]] = spec[which]
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def prepare(cls, source: Path) -> None:
        cls.start_source(source)
        cls.configure_source("1回目")
        # 日報: この端末で鳴らしている音声ファイル(1つ)
        sounds = cls.tmp / "配る元の音"
        sounds.mkdir()
        cls.sound_name = subprocess.run(
            [sys.executable, "-c", "from nippou.logic.sound import SOUNDS; "
                                   "print(next(s.default_file for s in SOUNDS if s.default_file))"],
            cwd=source / "tools" / "nippou", env=cls.pc1, capture_output=True, text=True, encoding="utf-8",
            check=True).stdout.strip()
        (sounds / cls.sound_name).write_bytes(cls.SOUND)
        path, _key = cls.settings_file(cls.pc1, "nippou")
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        data["sound_dir"] = str(sounds)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        cls.set_lines(1)
        cls.screen_errors = {}
        for tool, code in cls.SCREEN.items():
            done = subprocess.run([sys.executable, "-c", code + "; import sys; sys.exit(0 if r.ok else r.message)"],
                                  cwd=source / "tools" / tool, env=cls.pc1, capture_output=True, timeout=120)
            if done.returncode:
                cls.screen_errors[tool] = done.stderr.decode("utf-8", errors="replace")[-1500:]
        cls.set_lines(2)                     # そのあと配る元の端末でラインを変えた
        cls.outcome = cls.export_all()

    def tool_settings(self, root: Path, tool: str) -> dict:
        return json.loads((root / "tools" / tool / "配布設定" / "設定.json").read_text(encoding="utf-8-sig"))["settings"]

    def test_まとめて書き出してもラインと音声ファイルが残り_結果の1行に出る(self) -> None:
        self.assertEqual(self.screen_errors, {}, "設定画面と同じ書き出しができていない")
        self.assertTrue(self.outcome.get("ok"), self.outcome)
        lines = {r["id"]: r["line"] for r in self.outcome["results"]}
        whole = json.loads((self.dist / "配布設定" / "日報複合ツール.json").read_text(encoding="utf-8-sig"))
        for tool, (key, before, _after, said) in self.LINES.items():
            with self.subTest(tool=tool):
                self.assertEqual(self.tool_settings(self.dist, tool).get(key), before,
                                 "ツールのフォルダの 設定.json に、前に入れていたラインが値のまま残る")
                self.assertEqual(whole["tools"][tool]["settings"].get(key), before, "まとめたファイルの tools にも入る")
                self.assertIn(said, lines[tool])
        self.assertNotIn("そのまま残しました", lines["inspection"], "点検表にはラインが無い")
        kept = self.dist / "tools" / "nippou" / "配布設定" / "音"
        self.assertEqual([(p.name, p.read_bytes()) for p in kept.iterdir()], [(self.sound_name, self.SOUND)],
                         "日報の音声ファイルは消さずに残し、配布用フォルダにも入る")
        self.assertNotIn("sound_dir", self.tool_settings(self.dist, "nippou"), "音を残すので音の置き場所は入れない")
        self.assertFalse((self.dist / "配布設定" / "音").exists(), "まとめたファイルの側には音は入らない")
        self.assertNotIn("sound_dir", whole["tools"]["nippou"]["settings"])

    def deliver(self, root: Path, name: str) -> None:
        expected = self.values("1回目")
        for tool, (key, before, _after, _said) in self.LINES.items():
            expected[tool] = {**expected[tool], key: before}
        env = self.terminal(name, root=root, shell_like=True)
        sound = Path(env["LOCALAPPDATA"]) / "NippouTool" / "data" / "音声" / self.sound_name
        wait = self.wait_for(env, expected)
        saved, type(self).dist = self.dist, root
        try:
            tails = self.start_all(env, lambda tool: wait(tool) and (tool != "nippou" or sound.is_file()))
        finally:
            type(self).dist = saved
        self.assert_values(env, expected, tails, name)
        self.assertTrue(sound.is_file(), f"日報の音声ファイルが配った先の端末へ写っていない\n{tails['nippou']}")
        self.assertEqual(sound.read_bytes(), self.SOUND)
        stored = self.stored_values(env, "nippou")
        self.assertEqual(Path(stored.get("sound_dir", "")), sound.parent, "写した音で鳴る(音の置き場所はこの端末の中)")

    def test_配った先の端末にラインと音声ファイルが届く(self) -> None:
        self.assertTrue(self.outcome.get("ok"), self.outcome)
        self.deliver(self.dist, "配った先")

    def test_ツールのフォルダの設定が無くても_まとめたファイルからラインが届き_音は日報のフォルダから届く(self) -> None:
        self.assertTrue(self.outcome.get("ok"), self.outcome)
        out = self.tmp / "まとめたファイルだけの一式"
        if out.exists():
            shutil.rmtree(out)
        shutil.copytree(self.dist, out)
        for tool in ("nippou", "kanban", "calendar", "inspection"):
            (out / "tools" / tool / "配布設定" / "設定.json").unlink()
        self.deliver(out, "まとめたファイルだけの端末")


class RedistributedTerminalTests(_FromSource):
    """**配り直し**を通しで: 配る元で参照設定を変える → 書き出す → make_dist → 配った先(外枠と同じ道)。

        日報複合ツール VER1.5.64。日報、看板、カレンダーの配布設定の参照設定が生きていないのでは?
        違うPCでやってみたが参照設定が初期値になっていた。大設定のタブ表示権限は読めていた

    配った先の端末が**前に一度配布設定を読み込んでいる**と、前は読み込んだ値も「その端末にすでに
    ある」と数えていたので、参照先を変えて配り直しても前の値のまま動いていた(まっさらな端末は
    読む)。配布設定から読み込んだ値のままの項目は新しい値にし、**その端末で直した値は残す**。

    端末は外枠(日報複合ツール.exe)が渡すのと同じ環境(``LOCALAPPDATA`` / ``APPDATA`` だけ。
    各ツールの手元の領域の指定は渡さない)で、各ツールの ``bridge.py`` を子として起こす。
    書き出しは現場と同じ大設定の「4ツールの分もまとめて書き出す」(``export_all``)。
    """

    #: 配った先の端末で、1回目を読み込んだあとに利用者が直す項目(配り直しても戻さない)
    CHANGED_HERE = {"nippou": "report_output_dir", "calendar": "マスタDBフォルダ"}

    @classmethod
    def prepare(cls, source: Path) -> None:
        cls.start_source(source)
        cls.configure_source("1回目")
        cls.first_export = cls.export_all()

    def test_配り直すと前に読み込んだ端末も新しい参照設定になる_直した値は残す(self) -> None:
        self.assertTrue(self.first_export.get("ok"), self.first_export)
        first = self.values("1回目")
        env = self.terminal("配った先", shell_like=True)
        tails = self.start_all(env, self.wait_for(env, first))
        self.assert_values(env, first, tails, "1回目")

        # 配った先の利用者が、この端末で直す(配布設定と関わりなく決めた値)
        mine: dict[str, str] = {}
        for tool, key in self.CHANGED_HERE.items():
            path, _key = self.settings_file(env, tool)
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            mine[tool] = data[key] = str(self.tmp / "この端末" / tool)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        # 配る元で参照設定を変えて、まとめて書き出し直し、配布用フォルダを作り直して配る(上書き)
        self.configure_source("2回目")
        again = self.export_all()
        self.assertTrue(again.get("ok"), again)
        make_dist.build(self.dist, force=True, exe=self.tmp / "無い.exe", root=self.source)

        second = self.values("2回目")
        expected = {tool: {k: (mine[tool] if self.CHANGED_HERE.get(tool) == k else v) for k, v in values.items()}
                    for tool, values in second.items()}
        tails = self.start_all(env, self.wait_for(env, expected))
        self.assert_values(env, expected, tails, "配り直したあと")


if __name__ == "__main__":
    unittest.main()
