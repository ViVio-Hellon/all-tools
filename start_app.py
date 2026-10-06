#!/usr/bin/env python3
"""統合ツールのブラウザ版(予備)の起動開始点

    Start.vbs(通常)/ start.bat(診断)
        └─ start_app.py            ← ここ
             ├─ 実行環境の確認      (Python の版・Flask と waitress・書き込み権限)
             ├─ 同時起動の判定      (デスクトップ版が動いていれば止まる。`instance_guard`)
             ├─ 多重起動の判定      (ブラウザ版がもう動いていれば、そこへつなぐ)
             ├─ 待ち受け            (127.0.0.1:8700〜。waitress)
             └─ ブラウザを開く      (大きなタブの画面)

各ツールは、大きなタブを開いたときに、そのツールのブラウザ版を起こす(`portal/browser_tools.py`)。
**デスクトップ版(`統合ツール.exe`)が本来の入口。** ブラウザ版は exe が使えないときの予備。
ブラウザ版とデスクトップ版は同時に動かない(後から開いたほうが止まる)。

使い方:

    python start_app.py               ブラウザで開く
    python start_app.py --no-browser  ブラウザを開かない(検証用)
    python start_app.py --check       環境の確認だけして終わる(診断用)
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Optional

APP_ROOT = Path(__file__).resolve().parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

MIN_PYTHON = (3, 9)
REQUIRED_PACKAGES = (("flask", "Flask"), ("waitress", "waitress"))

#: 画面(大きなタブのページ)からの心拍が途絶えて、終わるまでの時間(秒)
IDLE_SEC = 90
#: 最初の心拍を待つ上限(秒)。ブラウザが開かなかったときに残り続けない
FIRST_CONTACT_SEC = 300

LOCK_NAME = "portal.lock"

DESKTOP_RUNNING = "統合ツールはデスクトップ版(統合ツール.exe の窓)で動いています"
DESKTOP_HINT = ("開いている窓をお使いください。ブラウザ版で開くときは、"
                "窓の「終了」で閉じてからにしてください。")


class StartupError(RuntimeError):
    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


def console_python() -> str:
    name = sys.executable.replace("\\", "/").rsplit("/", 1)[-1]
    if name.lower().startswith("pythonw"):
        return "python" + name[len("pythonw"):]
    return name


def run_environment_checks() -> None:
    if sys.version_info < MIN_PYTHON:
        raise StartupError(f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 以上が必要です(いまは {sys.version.split()[0]})",
                           "https://www.python.org/downloads/ から新しい Python を入れてください。")
    import importlib.util

    missing = [pip for module, pip in REQUIRED_PACKAGES if importlib.util.find_spec(module) is None]
    if missing:
        raise StartupError(f"必要なパッケージが入っていません: {', '.join(missing)}",
                           f"コマンドプロンプトで次を実行してください:\n"
                           f"    {console_python()} -m pip install -r requirements.txt")
    from portal import app_config

    try:
        root = app_config.ensure_local_dirs()
        probe = root / "runtime" / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise StartupError(f"作業用フォルダに書き込めません: {exc}",
                           "書き込みの権限があるか、ディスクの空きがあるか確認してください。") from None


# ------------------------------------------------------------------
# 印(ブラウザ版がどこで待ち受けているか。stop.bat と次の起動が読む)
# ------------------------------------------------------------------
def lock_path() -> Path:
    from portal import app_config

    return app_config.local_dir("runtime") / LOCK_NAME


def read_lock() -> Optional[dict]:
    try:
        data = json.loads(lock_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def write_lock(port: int, token: str) -> None:
    from portal import app_config

    data = {"app_id": app_config.app_id(), "pid": os.getpid(), "port": port,
            "url": f"http://127.0.0.1:{port}/", "token": token, "started_at": time.time(),
            "version": app_config.version(), "app_root": str(APP_ROOT), "python": sys.executable}
    path = lock_path()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def remove_lock() -> None:
    data = read_lock()
    if data and int(data.get("pid") or 0) == os.getpid():
        try:
            lock_path().unlink()
        except OSError:
            pass


def port_free(port: int, host: str) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


# ------------------------------------------------------------------
# 起動
# ------------------------------------------------------------------
def start(*, open_browser: bool = True) -> int:
    from portal import app_config, catalog, instance_guard, web
    from portal.browser_tools import BrowserTools
    from portal.logging_utils import get_logger
    from portal.rights_store import store

    log = get_logger("start")
    log.info("=" * 50)
    log.info("ブラウザ版を起動します: pid=%s Python %s", os.getpid(), sys.version.split()[0])

    # --- デスクトップ版と同時に動かさない(後から開いたほうが止まる)---
    claim = instance_guard.claim(instance_guard.BROWSER)
    if not claim.ok:
        if claim.running == instance_guard.DESKTOP:
            log.warning("デスクトップ版が動いているので、ブラウザ版は起動しません")
            instance_guard.bring_desktop_to_front()
            raise StartupError(DESKTOP_RUNNING, DESKTOP_HINT)
        # ブラウザ版がもう動いている。そこへつなぐ
        lock = read_lock()
        if lock and lock.get("url"):
            log.info("ブラウザ版がもう動いています。つなぎます: %s", lock.get("url"))
            if open_browser:
                webbrowser.open(lock["url"])
            else:
                print(f"もう動いています: {lock['url']}")
            return 0
        raise StartupError("統合ツールのブラウザ版がもう動いています",
                           "開いているブラウザのタブをお使いください。")

    host = app_config.host()
    port = next((p for p in app_config.port_candidates() if port_free(p, host)), None)
    if port is None:
        raise StartupError(f"使えるポートがありません(試した番号: {app_config.port_candidates()})",
                           "config/app.json の port を変えるか、そのアプリを終了してください。")

    token = secrets.token_urlsafe(24)
    app = web.create_app(token=token, bridge=False)
    tools = BrowserTools(catalog.load())
    web.set_browser_tools(tools)
    store.sync_in_background()

    from waitress.server import create_server

    server = create_server(app, host=host, port=port, threads=8, ident=app_config.app_id())
    stopped = threading.Event()

    def stop() -> None:
        if stopped.is_set():
            return
        stopped.set()
        log.info("停止します")
        try:
            dispatcher = getattr(server, "task_dispatcher", None)
            if dispatcher is not None:
                dispatcher.shutdown(timeout=3)
            server.close()
            from waitress import wasyncore

            table = getattr(server, "_map", None) or getattr(server, "map", None)
            if table:
                wasyncore.close_all(table, ignore_all=True)
        except Exception as exc:                  # noqa: BLE001 - 停止は best effort
            log.warning("停止でエラー: %s", exc)

    web.set_shutdown_hook(stop)
    write_lock(port, token)
    thread = threading.Thread(target=server.run, name="http", daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}/"
    log.info("待ち受け開始: %s", url)
    if open_browser:
        webbrowser.open(url)
    else:
        print(f"起動しました: {url}")

    # --- 画面が居なくなったら終わる(心拍)---
    started = time.monotonic()
    try:
        while thread.is_alive() and not stopped.is_set():
            time.sleep(1)
            beat = web.last_beat()
            contacted = beat > started
            idle = time.monotonic() - (beat if contacted else started)
            if (contacted and idle > IDLE_SEC) or (not contacted and idle > FIRST_CONTACT_SEC):
                log.info("画面が居なくなったので終わります(%.0f秒 心拍がありません)", idle)
                tools.stop_all(force=False)
                stop()
        thread.join(timeout=6)
    finally:
        remove_lock()
        claim.release()
        log.info("終了しました")
    return 0


def report_failure(error: StartupError, *, open_browser: bool) -> None:
    """起動できない理由を見せる。pythonw では標準エラーが無いので、Windows の窓で出す。"""
    text = f"{error}\n\n{error.hint}".strip()
    try:
        from portal.logging_utils import get_logger
        get_logger("start").error("起動に失敗: %s / %s", error, error.hint)
    except Exception:                             # noqa: BLE001
        pass
    if sys.stderr is not None:
        print(text, file=sys.stderr)
    if os.name == "nt" and open_browser:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, "統合ツール", 0x40)
        except Exception:                         # noqa: BLE001
            pass


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="統合ツールのブラウザ版(予備)を起動する")
    parser.add_argument("--no-browser", action="store_true", help="ブラウザを開かない(検証用)")
    parser.add_argument("--check", action="store_true", help="実行環境の確認だけして終わる(診断用)")
    args = parser.parse_args(argv)
    open_browser = not args.no_browser
    try:
        run_environment_checks()
        if args.check:
            from portal import app_config
            print("実行環境の確認: 問題ありません\n")
            print(app_config.describe())
            return 0
        return start(open_browser=open_browser)
    except StartupError as exc:
        report_failure(exc, open_browser=open_browser)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
