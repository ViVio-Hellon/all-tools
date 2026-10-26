"""外枠(日報複合ツール.exe)から入口と4ツールを起こす・切り替える・終える流れを通しで

外枠(Rust、`src-tauri/src/bridge.rs`・`shell.rs`)は、入口とツールごとに別の Python
(`bridge.py`)を子として起こし、標準入出力で要求を渡す。ここでは**外枠と同じ道**
(作業フォルダ = そのツールのフォルダ・環境変数・標準入出力の受け渡し)で5つの
`bridge.py` を起こし、外枠の代わりをする:

    起動 → 「受け付けを始めた」(started) → 画面と要求の行き来 → 「終わってよいか」を訊く
    → 終わってもらう → 「終了してよい」(quit) → 標準入力を閉じる → 自分で終わる

あわせて、外枠が当てにしている決まりを確かめる:

- どの Python もポートで待ち受けない
- 外枠が動いているあいだ、各ツールの単体のブラウザ版(`tools/<ツール>/start_app.py`)は
  起動しない(「日報複合ツールの窓」で動いていると言う)。同じツールの2つ目の Python は
  理由を言って止まる(1つ目は動き続ける)
- 起動できないとき(Python の部品が無い・設定が壊れている・共有が見えない)、**黙って
  終わらない**。黙って終わると、外枠は「Python が見つかりません」と的外れな理由を出す
  (`bridge.rs` の `start`: 1行も言わずに終わった Python は「使えない」として次を試す)
- 失敗の画面(`pages::failure`)に出す「ログの場所」に、その失敗が書いてある
- 配布用フォルダ(`scripts/make_dist.py`)に、端末ごとのもの(ログ・Excel の一時ファイル・
  手元の設定)が紛れない

作業用のフォルダ・環境変数は起動確認(`scripts/desktop_smoke.py` の `make_work`)と同じものを
使う(起動確認と食い違わないように)。本番の領域は触らない。**時間には頼らず、状態を待つ**
(Windows の CI は遅い)。
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
from typing import Optional

import tests  # noqa: F401  (一時フォルダへ向ける)

from portal import bridge_proto

ROOT = Path(__file__).resolve().parent.parent
WINDOWS = os.name == "nt"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


smoke = _load("alltools_desktop_smoke", ROOT / "scripts" / "desktop_smoke.py")

CATALOG = json.loads((ROOT / "config" / "tools.json").read_text(encoding="utf-8-sig"))
PORTAL = CATALOG["portal"]
TOOLS = CATALOG["tools"]
ENTRIES = [PORTAL, *TOOLS]

#: 状態を待つ上限(これを過ぎたら落とす)。時間を計るためではなく、止まったまま待たないため
WAIT = 120.0

#: 外枠の中で動いている印(錠)を、単体のブラウザ版がどう見るか(ツールごとの書き方)。
#: 点検表は入口と同じ作りの `core/instance_guard.py`、ほかは `launch_guard.desktop_running`
DESKTOP_HELD = {
    "inspection": "from core import instance_guard as g; print('DESKTOP', g.running() == g.DESKTOP)",
}


def tool_dir(entry: dict) -> Path:
    return ROOT / (entry.get("dir") or ".")


def shell_env(entry: dict, work_env: dict, token: str) -> dict:
    """外枠がその Python に渡す環境(`shell.rs` の `Shell::new` と `bridge.rs` の `spawn`)。"""
    env = dict(os.environ, **work_env)
    env[f"{entry['env_prefix']}_TOKEN"] = token
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("PYTHONUTF8", None)                   # UTF-8 モードにしない(`bridge.rs` の説明)
    env["ALLTOOLS_SHELL"] = "1"
    env["ALLTOOLS_TOOL"] = entry["id"]
    env["ALLTOOLS_ROOT"] = str(ROOT)
    if entry.get("demo_env"):
        env[entry["demo_env"]] = "1"              # 起動確認と同じ `--demo`(点検表は模擬の点検表)
    return env


class Child:
    """外枠から見た1つの Python(`bridge.rs` の `Bridge` の代わり)。"""

    def __init__(self, entry: dict, work_env: dict, *, python_args: tuple[str, ...] = ()) -> None:
        self.entry = entry
        self.token = f"tok-{entry['id']}"
        self.events: list[dict] = []
        self.replies: dict[int, tuple[dict, bytes]] = {}
        self.err: list[str] = []
        self.cond = threading.Condition()
        self.next_id = 1
        self.closed_at: Optional[float] = None
        self.proc = subprocess.Popen(
            [sys.executable, *python_args, str(tool_dir(entry) / "bridge.py")],
            cwd=str(tool_dir(entry)), env=shell_env(entry, work_env, self.token),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        threading.Thread(target=self._drain, daemon=True).start()
        threading.Thread(target=self._read, daemon=True).start()

    # --- 読む側(`read_loop`) ---
    def _drain(self) -> None:
        for line in self.proc.stderr:
            self.err.append(line.decode("utf-8", "replace").rstrip())
            del self.err[:-200]

    def _read(self) -> None:
        while True:
            try:
                frame = bridge_proto.read_frame(self.proc.stdout)
            except (ValueError, EOFError, OSError):
                frame = None
            with self.cond:
                if frame is None:
                    self.events.append({"event": "<eof>"})
                    self.cond.notify_all()
                    return
                head, body = frame
                if "event" in head:
                    self.events.append(head)
                else:
                    self.replies[head.get("id")] = (head, body)
                self.cond.notify_all()

    def tail(self) -> str:
        return "\n".join(self.err[-25:])

    def first_word(self) -> dict:
        """1行目(started / fatal / 何も言わずに終わった `<eof>`)。"""
        deadline = time.monotonic() + WAIT
        with self.cond:
            while not self.events and time.monotonic() < deadline:
                self.cond.wait(0.2)
            if not self.events:
                raise AssertionError(f"{self.entry['id']}: 1行目が来ません\n{self.tail()}")
            return self.events[0]

    def wait_event(self, name: str) -> dict:
        deadline = time.monotonic() + WAIT
        with self.cond:
            while time.monotonic() < deadline:
                for event in self.events:
                    if event["event"] == name:
                        return event
                if any(e["event"] == "<eof>" for e in self.events):
                    break
                self.cond.wait(0.2)
        raise AssertionError(f"{self.entry['id']}: {name} が来ません: {self.events}\n{self.tail()}")

    # --- 渡す側(`call_with`) ---
    def call(self, method: str, path: str, *, body: bytes = b"", headers=()) -> tuple[int, dict, bytes]:
        with self.cond:
            rid = self.next_id
            self.next_id += 1
        # 外枠が付けるもの: 合言葉・Host(単体のデスクトップ版の窓と同じ `app.localhost`)
        hdrs = [["Host", "app.localhost"], ["X-Tool-Token", self.token], *[list(h) for h in headers]]
        head = {"id": rid, "method": method, "path": path, "query": "", "headers": hdrs, "len": len(body)}
        self.proc.stdin.write(json.dumps(head, ensure_ascii=False).encode("utf-8") + b"\n" + body)
        self.proc.stdin.flush()
        deadline = time.monotonic() + WAIT
        with self.cond:
            while rid not in self.replies and time.monotonic() < deadline:
                self.cond.wait(0.2)
            if rid not in self.replies:
                raise AssertionError(f"{self.entry['id']}: {method} {path} に答えません\n{self.tail()}")
            reply, data = self.replies.pop(rid)
        return reply["status"], {k.lower(): v for k, v in reply["headers"]}, data

    def page(self, path: str = "/") -> tuple[int, dict, bytes]:
        """大きなタブの枠に画面を読み込む(外枠は Sec-Fetch-Site を「利用者が開いた」に直して渡す)。"""
        return self.call("GET", path, headers=[("Accept", "text/html"), ("Sec-Fetch-Mode", "navigate"),
                                               ("Sec-Fetch-Site", "none"), ("Sec-Fetch-Dest", "iframe")])

    def post_json(self, path: str, data: dict) -> tuple[int, dict]:
        status, _headers, body = self.call("POST", path, body=json.dumps(data).encode("utf-8"),
                                           headers=[("Content-Type", "application/json"),
                                                    ("Accept", "application/json")])
        try:
            return status, json.loads(body.decode("utf-8") or "null")
        except ValueError:
            return status, {"raw": body[:300].decode("utf-8", "replace")}

    def close(self) -> None:
        """標準入力を閉じる(外枠の `shutdown`: 「もう要求は来ない」)。"""
        if self.closed_at is None:
            self.closed_at = time.monotonic()
            try:
                self.proc.stdin.close()
            except OSError:
                pass

    def wait_exit(self) -> tuple[int, float]:
        self.close()
        code = self.proc.wait(timeout=WAIT)
        return code, time.monotonic() - (self.closed_at or time.monotonic())

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except OSError:
                pass


def new_work(prefix: str) -> tuple[Path, dict]:
    work = Path(tempfile.mkdtemp(prefix=prefix, dir=tests.BASE))
    return work, smoke.make_work(work)


def start_all(entries, work_env: dict, **kwargs) -> dict[str, Child]:
    """並べて起こす(外枠は開いたタブのツールを並べて起こす)。"""
    return {entry["id"]: Child(entry, work_env, **kwargs) for entry in entries}


def run_tool_script(entry: dict, work_env: dict, *args: str) -> subprocess.CompletedProcess:
    """そのツールの単体のブラウザ版(`start_app.py`)。"""
    env = dict(os.environ, **work_env)
    return subprocess.run([sys.executable, str(tool_dir(entry) / "start_app.py"), *args],
                          cwd=str(tool_dir(entry)), env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=WAIT)


def close_like_shell(case: unittest.TestCase, children: dict[str, "Child"]) -> None:
    """外枠の終わり方(`closing.rs`): 訊く → 終わってもらう → quit を待つ → 標準入力を閉じる。

    標準入力を閉じたあと、ツールごとの上限(`exit_grace_sec`)を過ぎると外枠は止める
    (最後の書き戻しが消える)。上限の内に自分で終わること。
    """
    for tool, child in children.items():
        with case.subTest(tool=tool, step="訊くだけ"):
            status, body = child.post_json("/api/shutdown", {"check": True})
            case.assertEqual(status, 200, body)
            case.assertIs(body.get("stopped"), False, "訊いただけで止まった")
            case.assertIsNone(child.proc.poll())
    for tool, child in children.items():
        with case.subTest(tool=tool, step="終わってもらう"):
            status, body = child.post_json("/api/shutdown", {"force": False})
            case.assertEqual(status, 200, body)
    for tool, child in children.items():
        with case.subTest(tool=tool, step="終了してよい"):
            child.wait_event("quit")
    grace = {e["id"]: float(e.get("exit_grace_sec", 10)) for e in ENTRIES}
    for child in children.values():
        child.close()
    for tool, child in children.items():
        with case.subTest(tool=tool, step="閉じたら終わる"):
            code, took = child.wait_exit()
            case.assertEqual(code, 0, child.tail())
            if not WINDOWS:   # Windows の CI は遅い(本物の exe の起動確認が終わり方を通しで見る)
                case.assertLess(took, grace[tool], f"{tool}: 閉じてから {took:.1f}秒(上限 {grace[tool]}秒)")


# ==================================================================
# 通し: 起動 → 行き来 → 取り合い → 終わり方
# ==================================================================
class ShellFlowTests(unittest.TestCase):
    """5つの Python を外枠と同じ道で起こし、順に確かめる(名前の数字の順に流れる)。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.work, cls.env = new_work("flow_shell_")
        cls.children = start_all(ENTRIES, cls.env)
        cls.first = {tool: child.first_word() for tool, child in cls.children.items()}

    @classmethod
    def tearDownClass(cls) -> None:
        for child in cls.children.values():
            child.kill()

    def child(self, tool: str) -> Child:
        child = self.children[tool]
        if self.first[tool].get("event") != "started":
            self.skipTest(f"{tool} が起動していません: {self.first[tool]}\n{child.tail()}")
        return child

    def test_1_入口と4ツールが受け付けを始め_画面を返す(self) -> None:
        for entry in ENTRIES:
            tool = entry["id"]
            with self.subTest(tool=tool):
                child = self.children[tool]
                self.assertEqual(self.first[tool].get("event"), "started",
                                 f"{tool}: 1行目が started ではない: {self.first[tool]}\n{child.tail()}")
                status, headers, body = child.page("/")
                self.assertIn(status, (200, 302), body[:300])
                if status == 200:
                    self.assertTrue(headers.get("content-type", "").startswith("text/html"), headers)
                    self.assertIn(b"<html", body[:2000].lower())

    def test_2_版はconfigと揃い_健康診断に出る(self) -> None:
        """外枠の題名(`config/app.json`)・Cargo・tauri.conf と、4ツールの版(各ツールの config)。"""
        app = json.loads((ROOT / "config" / "app.json").read_text(encoding="utf-8-sig"))
        for entry in ENTRIES:
            tool = entry["id"]
            with self.subTest(tool=tool):
                conf = json.loads((tool_dir(entry) / "config" / "app.json").read_text(encoding="utf-8-sig"))
                status, _h, body = self.child(tool).call("GET", "/api/health",
                                                          headers=[("Accept", "application/json")])
                self.assertEqual(status, 200, body[:300])
                health = json.loads(body)
                self.assertEqual(health["app_id"], conf["app_id"])
                self.assertEqual(health["version"], conf["version"])
        self.assertEqual(json.loads((ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))["version"],
                         app["version"])

    def test_3_どのPythonもポートで待ち受けない(self) -> None:
        pids = {c.proc.pid for c in self.children.values() if c.proc.poll() is None}
        self.assertEqual(len(pids), len(ENTRIES), "どれかが先に終わっている")
        self.assertEqual(smoke.listening_ports(pids), {})

    def test_4_外枠が動いていれば単体のブラウザ版は起動しない(self) -> None:
        """各ツールの Python が握る錠(`runtime/desktop.lock`)を、単体のブラウザ版が見て止まる。"""
        for entry in TOOLS:
            with self.subTest(tool=entry["id"]):
                self.child(entry["id"])
                # 起動しないで終わる(待ち受けを始めない)。終了コードはツールの決まり
                # (看板は「すでに動いている」と同じ 0、ほかは 0 以外)
                done = run_tool_script(entry, self.env, "--no-browser")
                said = done.stdout + done.stderr
                self.assertIn("日報複合ツールの窓", said, said[-800:])
                self.assertNotIn("起動しました", said, said[-800:])

    def test_5_同じツールの2つ目は理由を言って止まり_1つ目は動き続ける(self) -> None:
        """外枠の立て直しの重なり・錠を使えない環境で外枠が2つ動いた、のどちらでも
        **同じ手元の DB へ2つの Python が書かない**。2つ目は黙らずに理由を言う(失敗の画面に出る)。"""
        for entry in TOOLS:
            tool = entry["id"]
            with self.subTest(tool=tool):
                first = self.child(tool)
                second = Child(entry, self.env)
                try:
                    word = second.first_word()
                    self.assertEqual(word.get("event"), "fatal", f"{word}\n{second.tail()}")
                    self.assertTrue(word.get("message"), word)
                    self.assertNotEqual(second.proc.wait(timeout=WAIT), None)
                finally:
                    second.kill()
                status, _h, body = first.call("GET", "/api/health")
                self.assertEqual(status, 200, body[:300])

    def test_9_終わってよいかを訊き_終わってもらうと終了してよいと言い_閉じれば自分で終わる(self) -> None:
        close_like_shell(self, {e["id"]: self.child(e["id"]) for e in ENTRIES})
        # 終わったら錠は外れている: 単体のブラウザ版が見る錠に「外枠が居る」と出ない
        for entry in TOOLS:
            with self.subTest(tool=entry["id"], step="錠が外れた"):
                code = "import sys; sys.path.insert(0, '.'); " + DESKTOP_HELD.get(
                    entry["id"], "import launch_guard as g; print('DESKTOP', bool(g.desktop_running()))")
                done = subprocess.run([sys.executable, "-c", code], cwd=str(tool_dir(entry)),
                                      env=dict(os.environ, **self.env), capture_output=True, text=True,
                                      encoding="utf-8", errors="replace", timeout=WAIT)
                self.assertIn("DESKTOP False", done.stdout, done.stdout[-500:] + done.stderr[-1500:])
        # 「もう一度開く」(外枠は前の Python が終わってから次を起こす): 同じ手元の領域で、また始まる
        again = start_all(ENTRIES, self.env)
        try:
            for tool, child in again.items():
                with self.subTest(tool=tool, step="もう一度開く"):
                    self.assertEqual(child.first_word().get("event"), "started", child.tail())
            close_like_shell(self, again)
        finally:
            for child in again.values():
                child.kill()


class CancelCloseTests(unittest.TestCase):
    """あるツールの「終了」のあと、ほかのツールの確認で「やめる」を選んだ(終わり方をやめた)。

    「終了してよい」を言ったツールの Python は、標準入力が閉じるまで画面に答える
    (看板は閉じるまで待ち、日報・点検表は 6 秒で自分で終わる)。外枠が終えずにそのタブを読み直すと、ツールの
    画面がふつうに出て、すぐ裏の Python が居なくなる。**終えてから読み直す**
    (動きは Rust の試験 `closing.rs` の `やめたときは止まったツールのpythonを終えてから読み直す`)。
    """

    def test_終わり方をやめたときは_止まったツールを終えてから読み直す(self) -> None:
        closing = (ROOT / "src-tauri" / "src" / "closing.rs").read_text(encoding="utf-8")
        run = closing[closing.index("fn run("):closing.index("#[cfg(test)]")]
        self.assertNotIn("reloadTool", run, "run の中で直に読み直さない(end_then_reload を通す)")
        self.assertEqual(run.count("end_then_reload(shell, "), 4, "やめる道は4つ")
        ends = closing[closing.index("fn end_then_reload("):closing.index("fn end_stopped(")]
        self.assertLess(ends.index("end_stopped("), ends.index("reloadTool"), "終えてから読み直す")


class SendBeforeExitTests(unittest.TestCase):
    """未送信を送り切ってから閉じる: ツールが送るのに使う上限が、外枠の待ちの内に収まる。

    外枠は「終了してよい」を `quit_wait_sec` まで待ち、標準入力を閉じたあとは `exit_grace_sec`
    まで待って止める(`closing.rs`・`bridge.rs`)。ツールの送る上限のほうが長いと、送っている
    途中で止められる(送れなかった分は手元に残り、次に開いたときに送る)。
    """

    def seconds(self, path: str, name: str) -> float:
        import re
        text = (ROOT / "tools" / path).read_text(encoding="utf-8")
        found = re.search(rf"^\s*{name}\s*=\s*([0-9.]+)", text, re.M)
        self.assertIsNotNone(found, f"{path} に {name} がありません")
        return float(found.group(1))

    def test_送る上限は外枠の待ちより短い(self) -> None:
        conf = {t["id"]: t for t in TOOLS}
        # カレンダー: 「終了」で送り切ってから quit / 外枠が先に閉じたら送れるだけ送る
        self.assertLess(self.seconds("calendar/calendar_app/sync_service.py", "EXIT_SEND_SEC"),
                        conf["calendar"]["quit_wait_sec"])
        self.assertLess(self.seconds("calendar/bridge.py", "EOF_SEND_SEC"), conf["calendar"]["exit_grace_sec"])
        # 日報・点検表: 止めると頼まれてから、待ち受けの輪が終わるのを待つ上限(過ぎたら自分で落ちる)
        for tool in ("nippou", "inspection"):
            with self.subTest(tool=tool):
                self.assertLess(self.seconds(f"{tool}/start_app.py", "EXIT_WAIT_SEC"), conf[tool]["exit_grace_sec"])


class LongOpenTests(unittest.TestCase):
    """長く開いたまま(心拍が途切れる・画面が裏に回る・眠ったタブ・PC のスリープ)。

    外枠の中では、入口も4ツールも**心拍では終わらない**(窓を閉じたら外枠が終わらせる)。
    心拍で終わると、裏に回った・眠ったタブのツールだけが居なくなり、戻ると「処理が止まりました」
    になる。ブラウザ版の心拍の決まり(裏に回ったら数えない・スリープから戻ったら数え直す)は
    `test_start_app.py` と各ツールの試験。
    """

    def test_外枠の中では心拍で終わらない(self) -> None:
        for entry in TOOLS:
            with self.subTest(tool=entry["id"]):
                src = (tool_dir(entry) / "start_app.py").read_text(encoding="utf-8")
                at = src.index("def start_bridge(")
                body = src[at:src.find("\ndef ", at + 5)]
                self.assertIn("watch_idle=False", body)
        # 入口の bridge.py は、心拍の見張りを持つ start_app.py を使わない
        self.assertNotIn("start_app", (ROOT / "bridge.py").read_text(encoding="utf-8"))


#: 待ち受けを本体へ引き継ぐ瞬間に、本体がもう「外枠の中」の設定(`BRIDGE`)を持っているか。
#: ツールごとに `bridge.py` の `BridgeServer` を作り、引き継ぐ口(`wsgi.install`)で本体の設定を見る
HANDOVER_DRIVER = r'''
import io, json, sys
sys.path.insert(0, ".")
import bridge
cls = bridge.make_server_class()
writer, reader = bridge.FrameWriter(io.BytesIO()), io.BytesIO()
args = {ARGS}
srv = cls(*args)
seen = {}
handover = srv.wsgi
real = handover.install
def record(app):
    seen["BRIDGE"] = bool(app.config.get("BRIDGE"))
    real(app)
handover.install = record
srv.build()
seen["after"] = bool(srv.app.config.get("BRIDGE"))
print("HANDOVER" + json.dumps(seen))
'''

#: `BridgeServer` の作り方(ツールごとに引数が違う)
HANDOVER_ARGS = {
    "nippou": '("tok", writer, reader)',
    "kanban": '("site", "tok", writer, reader)',
    "calendar": '("tok", writer, reader)',
    "inspection": '("tok", {}, writer, reader)',
}


class HandoverTests(unittest.TestCase):
    """起動の直後(待機画面 → 本体へ引き継いだ瞬間)に届いた画面の要求を断らない。

    本体は Host を確かめる(外枠の宛先 `app.localhost` は `BRIDGE` のときだけ通す)。
    看板・カレンダー・点検表は**引き継いでから** `BRIDGE` を入れていたので、その一瞬に届いた
    要求が「このアドレスからは利用できません」(403)で断られ、タブにそのまま出た
    (この試験を作るときに、カレンダーのタブで1度出た)。日報は前に直してあった。
    """

    def test_本体へ引き継ぐときには外枠の中の設定が入っている(self) -> None:
        _work, env = new_work("flow_handover_")
        for entry in TOOLS:
            tool = entry["id"]
            with self.subTest(tool=tool):
                code = HANDOVER_DRIVER.replace("{ARGS}", HANDOVER_ARGS[tool])
                done = subprocess.run([sys.executable, "-c", code], cwd=str(tool_dir(entry)),
                                      env=shell_env(entry, env, "tok"), capture_output=True, text=True,
                                      encoding="utf-8", errors="replace", timeout=WAIT)
                line = next((l for l in done.stdout.splitlines() if l.startswith("HANDOVER")), "")
                self.assertTrue(line, done.stdout[-800:] + done.stderr[-2000:])
                seen = json.loads(line[len("HANDOVER"):])
                self.assertIs(seen.get("BRIDGE"), True, f"{tool}: 引き継いだときに BRIDGE が無い")
                self.assertIs(seen.get("after"), True)


# ==================================================================
# 起動できないとき: 黙って終わらず、理由とログの場所を言う
# ==================================================================
def find_text(folder: str, needle: str) -> list[Path]:
    """`folder` の下(子のフォルダも)で `needle` を含むファイル。"""
    base = Path(folder)
    if not folder or not base.is_dir():
        return []
    found = []
    for path in base.rglob("*"):
        if path.is_file() and path.suffix.lower() in (".log", ".txt", ".jsonl"):
            try:
                if needle in path.read_text(encoding="utf-8", errors="replace"):
                    found.append(path)
            except OSError:
                pass
    return found


class FailureTests(unittest.TestCase):
    def test_部品の無いPythonでも黙らずに入れ方を言う(self) -> None:
        """Flask などを入れていない Python(`-S` で site-packages を見せない)。

        1行も言わずに終わると、外枠は次の Python を試し、最後は「Python が見つかりません」と
        的外れな理由を出す。**部品が無いと言い、入れ方(pip)を添える**こと。
        """
        _work, env = new_work("flow_nosite_")
        children = start_all(ENTRIES, env, python_args=("-S",))
        try:
            for tool, child in children.items():
                with self.subTest(tool=tool):
                    word = child.first_word()
                    self.assertEqual(word.get("event"), "fatal", f"{word}\n{child.tail()}")
                    self.assertIn("Flask", word.get("message", ""), word)
                    self.assertIn("pip install", word.get("hint", ""), word)
                    self.assertNotEqual(child.proc.wait(timeout=WAIT), 0)
        finally:
            for child in children.values():
                child.kill()

    def test_失敗の画面のログの場所に起動の失敗が書いてある(self) -> None:
        """外枠は Python が言った `log_dir` を失敗の画面の「ログの場所」に出す。そこに無いと、
        現場は別の場所を探すことになる(日報は v4.44.5 で起動の記録をログの出力パスへ移した)。

        看板と入口は起動の失敗を記録に残さない(画面に理由が出るだけ)ので、ここでは見ない。
        """
        _work, env = new_work("flow_logplace_")
        targets = [e for e in TOOLS if e["id"] in ("nippou", "calendar", "inspection")]
        children = start_all(targets, env, python_args=("-S",))
        try:
            for tool, child in children.items():
                with self.subTest(tool=tool):
                    word = child.first_word()
                    self.assertEqual(word.get("event"), "fatal", word)
                    child.proc.wait(timeout=WAIT)
                    found = find_text(word.get("log_dir", ""), word["message"])
                    self.assertTrue(found, f"{tool}: 案内した場所({word.get('log_dir')})に"
                                           f"「{word['message']}」の記録がありません")
        finally:
            for child in children.values():
                child.kill()

    def test_共有が見えなくても起動する(self) -> None:
        """共有のフォルダ(タブ表示権限の DB・看板の共有の DB)に届かない朝。起動を止めない。"""
        work, env = new_work("flow_noshare_")
        shutil.rmtree(work / "share")
        children = start_all(ENTRIES, env)
        try:
            for tool, child in children.items():
                with self.subTest(tool=tool):
                    word = child.first_word()
                    self.assertEqual(word.get("event"), "started", f"{word}\n{child.tail()}")
                    status, _h, body = child.page("/")
                    self.assertIn(status, (200, 302), body[:300])
            # 送れない(共有に届かない)ままでも、訊かれて終わり、閉じれば上限の内に終わる
            close_like_shell(self, children)
        finally:
            for child in children.values():
                child.kill()

    def test_手元の設定が壊れていても黙って終わらない(self) -> None:
        """1度起動して手元にできた設定(JSON)を全部壊してから、もう一度起こす。

        起動する(既定で動く)か、理由を言って止まる(失敗の画面に出る)のどちらか。
        何も言わずに終わる・答えずに止まるのはだめ。
        """
        work, env = new_work("flow_broken_")
        first = start_all(ENTRIES, env)
        try:
            for tool, child in first.items():
                self.assertEqual(child.first_word().get("event"), "started", f"{tool}\n{child.tail()}")
                child.page("/")
        finally:
            for child in first.values():
                child.close()
            for child in first.values():
                try:
                    child.wait_exit()
                finally:
                    child.kill()
        broken = []
        for path in work.rglob("*.json"):
            if "share" in path.relative_to(work).parts:
                continue
            path.write_text("{ 壊れた", encoding="utf-8")
            broken.append(path)
        self.assertTrue(broken, "手元に設定ができていない")
        again = start_all(ENTRIES, env)
        try:
            for tool, child in again.items():
                with self.subTest(tool=tool):
                    word = child.first_word()
                    self.assertIn(word.get("event"), ("started", "fatal"), f"{word}\n{child.tail()}")
                    if word["event"] == "fatal":
                        self.assertTrue(word.get("message"), word)
                    else:
                        status, _h, body = child.page("/")
                        self.assertIn(status, (200, 302), body[:300])
        finally:
            for child in again.values():
                child.kill()


# ==================================================================
# 配布用フォルダ: 端末ごとのものが紛れない
# ==================================================================
#: 一式の写しに置く「端末ごとのもの」(git に入らないもの。入口と、make_dist を持たない点検表は
#: 一式の make_dist が写す)。Excel・Word で開いたときにできる `~$` の一時ファイル・ログ
LOCAL_LEFTOVERS = (
    "docs/~$手順.docx",
    "portal/static/crash.log",
    "scripts/make_dist.log",
    "tools/inspection/data/~$点検表.xlsx",
    "tools/inspection/data/print.log",
    "tools/inspection/core/debug.log",
)


class DistFlowTests(unittest.TestCase):
    def test_端末ごとのログとExcelの一時ファイルは配らない(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git がありません")
        tmp = Path(tempfile.mkdtemp(prefix="flow_dist_", dir=tests.BASE))
        root = tmp / "一式"
        listed = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
        for name in filter(None, listed.decode("utf-8").split("\0")):
            src = ROOT / name
            if src.is_file():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, root / name)
        for name in LOCAL_LEFTOVERS:
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text("この端末のもの", encoding="utf-8")
        make_dist = _load("alltools_make_dist_flow", root / "scripts" / "make_dist.py")
        out, _lines = make_dist.build(tmp / "dist", exe=None, root=root)
        leaked = [name for name in LOCAL_LEFTOVERS if (out / name).exists()]
        self.assertEqual(leaked, [], "端末ごとのものが配布用フォルダに入った")
        self.assertTrue((out / "tools" / "inspection" / "data" / "README.md").is_file(), "配るものは入る")


if __name__ == "__main__":
    unittest.main()
