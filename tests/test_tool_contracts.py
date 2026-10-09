"""4 ツール共通の約束 ── 統合ツールの窓(Rust)とランチャーが当てにしている答え方

窓の × は、まず全ツールに ``POST /api/shutdown {"check": true}`` で「終わってよいか」を訊き
(``src-tauri/src/closing.rs``)、途中の処理があれば 409 の理由を一つの確認にまとめ、そのあと
``{"force": ...}`` で止める。ランチャー・stop.bat は ``/api/health`` の ``app_id``・``ready``・
``version``・``pid`` を見る。**この答え方が 1 つのツールだけずれると、そのツールだけ確認が出ない・
勝手に止まる・止まらない**。ずれた日に落ちるよう、4 ツールを同じ試験に通す。

ツールはどれも ``app`` という名前のパッケージを持つので、1 つずつ別のプロセスで、そのツールの
本物の ``app/routes/health.py`` だけを載せた Flask で確かめる(画面や DB は要らない)。
"""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"

#: ツールごとの違い: 設定の読み方と、「途中の処理がある」にする方法
TOOL_SETUP = {
    "nippou": {
        "app_config": "from nippou import app_config",
        "busy": "mock.patch.object(health, '_running_jobs', return_value=['取り込み'])",
        "extra_config": {},
    },
    "kanban": {
        "app_config": "from kanban import app_config",
        "busy": "mock.patch.object(health, '_busy_check', lambda: '書き戻しの途中です')",
        "extra_config": {"MODE": "site", "LINE": "LVC"},
    },
    "calendar": {
        "app_config": "from calendar_app import app_config",
        "busy": "mock.patch.object(health, '_running_jobs', return_value=['取り込み元との同期'])",
        "extra_config": {},
    },
    "inspection": {
        "app_config": "from core import app_config",
        "busy": "mock.patch.object(health, 'business', return_value=_Biz(['印刷']))",
        "extra_config": {},
    },
}

DRIVER = r'''
import json, sys, threading, time
from unittest import mock
sys.path.insert(0, ".")
from flask import Flask
{app_config}
from app.routes import health

class _Biz:
    """点検表の business() の代わり(印刷・プレビュー・検索の状態だけ)。"""
    def __init__(self, labels):
        self.labels = labels
        self.printing = mock.Mock(status=lambda: {{"active": bool(labels), "message": "", "current_index": 0, "total": 0}})
        self.excel = mock.Mock(activity="")
        self.inspection = mock.Mock(status=lambda: {{"state": "done"}})
    def busy_labels(self):
        return list(self.labels)

app = Flask("contract")
app.config.update(APP_ID=app_config.app_id(), DISPLAY_NAME="試験", VERSION=app_config.version(), PORT=8700,
                  READY=True, STAGE="準備完了", STAGE_KEY="ready", STARTUP_ERROR="", STARTED_AT=time.time(),
                  **{extra})
app.register_blueprint(health.bp)
client = app.test_client()
out = {{"app_id": app_config.app_id(), "version": app_config.version()}}
stopped = threading.Event()
for name in ("SHUTDOWN_DELAY_SEC",):
    if hasattr(health, name):
        setattr(health, name, 0.01)

def call(body):
    res = client.post("/api/shutdown", json=body)
    return [res.status_code, res.get_json(silent=True)]

idle = mock.patch.object(health, "business", return_value=_Biz([])) if hasattr(health, "business") else mock.patch("builtins.id")
with idle:
    res = client.get("/api/health")
    out["health"] = [res.status_code, res.get_json(silent=True)]
    health.set_shutdown_hook(None) if hasattr(health, "set_shutdown_hook") else None
    health._shutdown_hook = None
    out["no_hook"] = call({{}})
    health.set_shutdown_hook(stopped.set)
    out["check_idle"] = call({{"check": True}})
    time.sleep(0.5)
    out["check_idle_stopped"] = stopped.is_set()
with {busy}:
    out["check_busy"] = call({{"check": True}})
    out["stop_busy"] = call({{}})
    time.sleep(0.5)
    out["busy_stopped"] = stopped.is_set()
    out["force_busy"] = call({{"force": True}})
    stopped.wait(3)
    out["force_stopped"] = stopped.is_set()
print("CONTRACT" + json.dumps(out, ensure_ascii=False))
'''


def busy_reason(body: dict) -> str:
    """``closing.rs::busy_reason`` と同じ読み方(窓の確認に出す文)。"""
    running = body.get("running")
    if isinstance(running, list) and running:
        return "・".join(str(x) for x in running)
    if isinstance(body.get("busy"), str) and body["busy"]:
        return body["busy"]
    error = body.get("error")
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])
    if body.get("message"):
        return str(body["message"])
    return "実行中の処理があります"


def run_contract(tool: str) -> dict:
    setup = TOOL_SETUP[tool]
    code = DRIVER.format(app_config=setup["app_config"], busy=setup["busy"],
                         extra=repr(setup["extra_config"]))
    done = subprocess.run([sys.executable, "-c", code], cwd=str(TOOLS / tool),
                          capture_output=True, text=True, encoding="utf-8", timeout=120)
    line = next((l for l in done.stdout.splitlines() if l.startswith("CONTRACT")), "")
    if not line:
        raise AssertionError(f"{tool} の確かめが動きませんでした:\n{done.stderr[-3000:]}")
    return json.loads(line[len("CONTRACT"):])


class ToolContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.results = {tool: run_contract(tool) for tool in TOOL_SETUP}

    def test_健康診断はランチャーと窓が見る項目を返す(self) -> None:
        for tool, out in self.results.items():
            with self.subTest(tool=tool):
                status, body = out["health"]
                self.assertEqual(status, 200)
                for key in ("app_id", "display_name", "version", "pid", "port", "ready", "stage",
                            "stage_key", "startup_error"):
                    self.assertIn(key, body, f"{tool} の /api/health に {key} がありません")
                self.assertEqual(body["app_id"], out["app_id"])
                self.assertEqual(body["version"], out["version"])
                self.assertIs(body["ready"], True)

    def test_訊くだけなら止めない(self) -> None:
        for tool, out in self.results.items():
            with self.subTest(tool=tool):
                status, body = out["check_idle"]
                self.assertEqual(status, 200)
                self.assertIs(body.get("stopped"), False)
                self.assertIs(body.get("can_stop"), True)
                self.assertFalse(out["check_idle_stopped"], f"{tool} は訊いただけで止まった")

    def test_途中の処理があれば409と理由を返し_止めない(self) -> None:
        for tool, out in self.results.items():
            with self.subTest(tool=tool):
                for key in ("check_busy", "stop_busy"):
                    status, body = out[key]
                    self.assertEqual(status, 409, f"{tool} {key}")
                    self.assertIs(body.get("stopped"), False)
                    self.assertEqual(body.get("reason"), "busy", f"{tool} は reason=busy を返さない")
                    self.assertTrue(body.get("running"), f"{tool} は running(何をしているか)を返さない")
                    self.assertNotEqual(busy_reason(body), "実行中の処理があります", f"{tool} の理由が窓に出ない")
                self.assertFalse(out["busy_stopped"], f"{tool} は途中の処理があるのに止まった")

    def test_強制なら途中でも止める(self) -> None:
        for tool, out in self.results.items():
            with self.subTest(tool=tool):
                status, body = out["force_busy"]
                self.assertEqual(status, 200)
                self.assertIs(body.get("stopped"), True)
                self.assertTrue(out["force_stopped"], f"{tool} は強制でも止まらない")

    def test_止める手段が無ければ501(self) -> None:
        for tool, out in self.results.items():
            with self.subTest(tool=tool):
                status, body = out["no_hook"]
                self.assertEqual(status, 501, f"{tool} は止める手段が無いときに {status} を返す")
                self.assertIs(body.get("stopped"), False)


if __name__ == "__main__":
    unittest.main()
