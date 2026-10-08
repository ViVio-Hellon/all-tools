"""ブラウザ版(予備)で、各ツールのブラウザ版を起こして大きなタブに並べる

ブラウザ版は**各ツールの今までのブラウザ版をそのまま使う**(予備なので、デスクトップ版の
仕組みに頼らない)。ここがするのは:

- タブが開かれたら、そのツールのブラウザ版を起こす(`tools/<名前>/start_app.py --no-browser`)。
  すでに動いていれば、それにつなぐ
- そのツールが待ち受けている番号と起動トークンを、ツールの印(`runtime/*.lock`)から読み、
  枠に出す宛先(`http://127.0.0.1:<番号>/?t=<トークン>`)を返す
- 「終了」のとき、各ツールに「終わってよいか」を訊き、まとめて止める

各ツールのブラウザ版は、自分の画面(枠の中)からの心拍が途絶えると自分で終わる
(今までどおり)。大きなタブのページを閉じれば、入口も各ツールも終わる。

`ALLTOOLS_PORTAL=1` を渡して起こす(ツールはブラウザを自分で開かず、心拍の見張りは立てる)。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from .catalog import Catalog, Tool
from .logging_utils import get_logger, log_dir

#: Windows のコマンド(tasklist・wmic)の出力の文字コード。**コンソールの文字コード(oem)で読む。**
#: 日本語の Windows では Shift-JIS(「情報: 指定された条件に一致するタスクは…」)。Python の
#: 既定(UTF-8 モードのとき UTF-8)で読むと、読めずに落ちて起動が止まっていた。読めない字は置き換える
CONSOLE_ENCODING = "oem" if os.name == "nt" else None

log = get_logger("browser_tools")

#: ツールのブラウザ版が待ち受けを始めるまで待つ上限(秒)。日報は取り込みの準備がある
START_WAIT_SEC = 90
HEALTH_TIMEOUT_SEC = 1.5

# 自分自身(127.0.0.1)への通信に**プロキシを通さない**(社内PCはプロキシの除外が無いことがある)
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _request(url: str, *, data: Optional[bytes] = None, headers: Optional[dict] = None,
             timeout: float = HEALTH_TIMEOUT_SEC) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=data, headers=headers or {},
                                 method="POST" if data is not None else "GET")
    try:
        with _LOCAL.open(req, timeout=timeout) as res:
            return res.status, json.loads(res.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8") or "{}")
        except ValueError:
            body = {}
        return exc.code, body


def is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                                 capture_output=True, text=True, encoding=CONSOLE_ENCODING, errors="replace", timeout=5,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError):
            return True
        return f'"{pid}"' in (out.stdout or "")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Running:
    def __init__(self, tool: Tool, info: dict) -> None:
        self.tool = tool
        self.pid = int(info.get("pid") or 0)
        self.port = int(info.get("port") or 0)
        self.token = str(info.get("token") or "")

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def url(self) -> str:
        return f"{self.base}/?t={self.token}"


def find_running(tool: Tool, *, check_pid: bool = True) -> Optional[Running]:
    """そのツールのブラウザ版が動いていれば、その印(番号・トークン)。

    `check_pid=False` は PID を確かめない(Windows の tasklist を起こさない)。
    待ち受けが自分(app_id)と答えるかだけで見る。画面の見張り(数秒ごと)向け。
    """
    runtime = tool.local_root() / "runtime"
    try:
        locks = sorted(runtime.glob("*.lock"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    app_id = tool.app_id()
    for path in locks:
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue                               # OS の錠だけのファイル(中身が無い)
        if not isinstance(info, dict) or not info.get("token") or not int(info.get("port") or 0):
            continue
        if app_id and info.get("app_id") and info.get("app_id") != app_id:
            continue
        running = Running(tool, info)
        if check_pid and not is_alive(running.pid):
            continue
        try:
            status, health = _request(f"{running.base}/api/health")
        except (urllib.error.URLError, OSError, ValueError):
            continue
        if status == 200 and (not app_id or health.get("app_id") == app_id):
            return running
    return None


def has_live_lock(tool: Tool) -> bool:
    """そのツールのブラウザ版の印(`runtime/*.lock`)が残っていて、その PID が生きているか。

    待ち受けが答えない(重い処理の途中・一時停止)だけでは「終わった」と言えない。
    終わったと言えるのは、**印が消えたか、印の PID が居ない**ときだけ。
    """
    runtime = tool.local_root() / "runtime"
    try:
        locks = list(runtime.glob("*.lock"))
    except OSError:
        return False
    app_id = tool.app_id()
    for path in locks:
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(info, dict) or not info.get("token") or not int(info.get("port") or 0):
            continue
        if app_id and info.get("app_id") and info.get("app_id") != app_id:
            continue
        if is_alive(int(info.get("pid") or 0)):
            return True
    return False


class BrowserTools:
    """ブラウザ版の各ツール(起こした子と、つないだ相手)。"""

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self._lock = threading.Lock()
        self._children: dict[str, subprocess.Popen] = {}

    def _spawn(self, tool: Tool) -> subprocess.Popen:
        env = dict(os.environ, ALLTOOLS_PORTAL="1", PYTHONIOENCODING="utf-8")
        env.pop("ALLTOOLS_SHELL", None)
        out = log_dir()
        out.mkdir(parents=True, exist_ok=True)
        err = open(out / f"browser_{tool.id}.log", "ab")
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        log.info("%s のブラウザ版を起こします", tool.name)
        try:
            return subprocess.Popen([sys.executable, str(tool.dir / "start_app.py"), "--no-browser"],
                                    cwd=str(tool.dir), env=env, stdin=subprocess.DEVNULL,
                                    stdout=err, stderr=err, creationflags=flags, close_fds=True)
        finally:
            err.close()

    def open(self, tool: Tool) -> str:
        """枠に出す宛先。動いていなければ起こして、待ち受けを始めるまで待つ。"""
        running = find_running(tool)
        if running is not None:
            return running.url
        with self._lock:
            child = self._children.get(tool.id)
            if child is None or child.poll() is not None:
                child = self._spawn(tool)
                self._children[tool.id] = child
        deadline = time.monotonic() + START_WAIT_SEC
        while time.monotonic() < deadline:
            running = find_running(tool)
            if running is not None:
                log.info("%s のブラウザ版につなぎます: port=%s", tool.name, running.port)
                return running.url
            code = child.poll()
            if code is not None:
                # 同じものがもう動いていてつないだ(=0)なら、もう一度探せば見つかる
                running = find_running(tool)
                if running is not None:
                    return running.url
                raise RuntimeError(f"{tool.name}のブラウザ版が起動しませんでした(終了コード {code})。"
                                   f"{self._last_words(tool)}")
            time.sleep(0.3)
        raise RuntimeError(f"{tool.name}のブラウザ版が {START_WAIT_SEC} 秒で起動しませんでした。")

    def _last_words(self, tool: Tool) -> str:
        """起動しなかった理由(そのツールが書いた最後の数行)。"""
        try:
            text = (log_dir() / f"browser_{tool.id}.log").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        lines = [line for line in text.splitlines() if line.strip()][-4:]
        return ("\n" + "\n".join(lines)) if lines else ""

    def running(self) -> list[Running]:
        return [r for r in (find_running(t) for t in self.catalog.tools) if r is not None]

    def busy(self) -> list[str]:
        """処理の途中のツール(「終わってよいか」を訊いて、断ったもの)。"""
        found = []
        for running in self.running():
            try:
                status, body = _request(f"{running.base}/api/shutdown",
                                        data=json.dumps({"check": True}).encode("utf-8"),
                                        headers={"Content-Type": "application/json",
                                                 "X-Tool-Token": running.token}, timeout=5)
            except (urllib.error.URLError, OSError, ValueError):
                continue
            if status == 409:
                reason = body.get("message") or body.get("busy") or (body.get("error") or {}).get("message") or "処理の途中です"
                found.append(f"{running.tool.title}: {reason}")
        return found

    def stop_all(self, *, force: bool = False) -> None:
        for running in self.running():
            try:
                _request(f"{running.base}/api/shutdown",
                         data=json.dumps({"force": force}).encode("utf-8"),
                         headers={"Content-Type": "application/json", "X-Tool-Token": running.token},
                         timeout=5)
                log.info("%s のブラウザ版に終わってもらいました", running.tool.name)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                log.warning("%s のブラウザ版を止められませんでした: %s", running.tool.name, exc)

    def status(self, ids: Optional[list[str]] = None) -> list[dict]:
        """各ツールのブラウザ版が動いているか(`ids` だけ。画面の見張り用に軽く見る)。

        `running`: 待ち受けが答えた。`ended`: **印が消えた・印の PID が居ない**(本当に終わった)。
        どちらでもない(答えないが、PID は生きている)は重い処理の途中かもしれない ──
        以前は待ち受けが2回答えないだけで「終了しました」にして枠を消し、打ちかけが消えていた。
        PID を確かめる(Windows は tasklist)のは、待ち受けが答えなかったときだけ。
        """
        tools = [t for t in self.catalog.tools if ids is None or t.id in ids]
        # 自分で起こした子が終わっていれば片付ける(片付けないと、終わった子の PID が
        # ゾンビとして「生きている」ように見え、いつまでも終わったと分からない)
        with self._lock:
            for child in self._children.values():
                child.poll()
        out = []
        for t in tools:
            running = find_running(t, check_pid=False) is not None
            ended = False if running else not has_live_lock(t)
            out.append({"id": t.id, "running": running, "ended": ended})
        return out
