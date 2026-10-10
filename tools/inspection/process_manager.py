#!/usr/bin/env python3
"""対象プロセスの確認と安全な停止 (基盤仕様書 2.8)

**このアプリだけを止める。** 同じPCで動く別のPythonアプリや、利用者が
自分で開いている Excel を巻き添えにしないことが、このモジュールの目的。

順序は仕様書のとおり(python-web-tools の `process_manager.py` と同じ):

    1. まずアプリ自身へ正常終了を要求する (`POST /api/shutdown`)
    2. 応答しないときだけ、記録したPIDを使う
    3. 落とす前に、そのPIDが**本当にこのアプリか**を確かめる
       (プロセス作成時刻の一致。取れなければコマンドラインの照合)
    4. `python.exe` をプロセス名だけで一括終了することはしない

印刷中は既定では止めずに知らせる。中断してよいかは利用者が決める
(`--force` で中断する)。最後に、このアプリが起動した Excel が
残っていれば、記録した PID だけを片付ける。

使い方:

    python process_manager.py            止める(stop.bat)
    python process_manager.py --force    印刷中でも中断して止める
    python process_manager.py --status   状態を見るだけ

**デスクトップ版(日報複合ツールの窓の「点検表」)は止めない。** ポートで待ち受けていないので
外から終了を頼む道が無く、窓の確認(印刷中です…)を飛ばして止めることになる。
動いていれば、窓で閉じるよう案内し、窓を前に出す。
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import urllib.error
from pathlib import Path
from typing import Optional

APP_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_ROOT))
# 停止は短い処理なので Python のキャッシュを書かない。アプリ本体を共有フォルダに
# 置いたとき、各ラインのPCが __pycache__ を書きに行かないようにする
sys.dont_write_bytecode = True

from core import app_config, process_tracking  # noqa: E402
from core.logging_utils import get_logger  # noqa: E402

log = get_logger("process_manager")

# 正常終了を要求したあと、実際に落ちるのを待つ上限(秒)
GRACEFUL_WAIT_SEC = 8.0
# PIDで落としたあと、消えるのを待つ上限(秒)
FORCE_WAIT_SEC = 5.0


class StopResult:
    """止められたか、どうやって止めたか。"""

    def __init__(self) -> None:
        self.stopped = False
        self.method = ""
        self.message = ""
        self.busy_jobs: list[str] = []

    def __str__(self) -> str:
        mark = "済" if self.stopped else "--"
        return f"[{mark}] {self.message}"


# ------------------------------------------------------------------
# 状態
# ------------------------------------------------------------------
def status() -> Optional[dict]:
    """動いているか。動いていれば `/api/health` の中身。"""
    import launch_guard

    info = launch_guard.read_lock()
    if info is None:
        return None
    health = launch_guard.probe_health(info.port)
    if not launch_guard.is_our_app(health):
        return None
    health["_lock"] = {"pid": info.pid, "port": info.port,
                       "started": info.started_text, "app_root": info.app_root}
    return health


# ------------------------------------------------------------------
# 停止
# ------------------------------------------------------------------
def stop(*, force: bool = False) -> StopResult:
    import launch_guard

    result = StopResult()
    info = launch_guard.read_lock()
    if info is None:
        result.stopped = True
        result.method = "none"
        result.message = "起動していません"
        return result

    health = launch_guard.probe_health(info.port)
    if not launch_guard.is_our_app(health):
        # 応答しない、または別のアプリ。ロックだけ残っている状態
        if launch_guard.is_process_alive(info.pid):
            return _stop_by_pid(info, result, force=force)
        launch_guard.remove_lock()
        result.stopped = True
        result.method = "stale-lock"
        result.message = "動いていませんでした(残っていたロックを片付けました)"
        return result

    # --- 1. 正常終了を要求する ---
    asked = _request_shutdown(info.port, info.token, force=force)
    if asked.get("busy"):
        result.busy_jobs = asked.get("running", [])
        result.message = (f"実行中の処理があります: {', '.join(result.busy_jobs)}\n"
                          f"    中断して止めるには stop.bat --force を実行してください")
        return result

    if asked.get("ok") and _wait_gone(info.port, GRACEFUL_WAIT_SEC):
        launch_guard.remove_lock()
        result.stopped = True
        result.method = "graceful"
        result.message = "正常に終了しました"
        return result

    # --- 2. 応答しないときだけPIDを使う ---
    return _stop_by_pid(info, result, force=force)


def _request_shutdown(port: int, token: str, *, force: bool) -> dict:
    """`POST /api/shutdown` で正常終了を要求する(基盤仕様書 2.8 の1段目)。"""
    import launch_guard

    url = f"http://127.0.0.1:{port}/api/shutdown"
    body = json.dumps({"force": force}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Tool-Token"] = token
    try:
        # プロキシを経由しない送信口(`launch_guard.local_request`)
        with launch_guard.local_request(url, timeout=5, data=body,
                                        method="POST", headers=headers) as res:
            payload = json.loads(res.read().decode("utf-8"))
            return {"ok": bool(payload.get("stopped"))}
    except urllib.error.HTTPError as exc:
        if exc.code == 409:                        # 印刷中
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except Exception:                      # noqa: BLE001
                payload = {}
            return {"ok": False, "busy": True,
                    "running": payload.get("running", ["(不明)"])}
        log.info("停止要求は %s で拒否されました。PIDで止めます", exc.code)
        return {"ok": False}
    except (urllib.error.URLError, OSError) as exc:
        log.info("停止要求を送れませんでした (%s)。PIDで止めます", exc)
        return {"ok": False}


def _stop_by_pid(info, result: StopResult, *, force: bool) -> StopResult:
    """PIDで止める。**落とす前に本当にこのアプリかを確かめる。**"""
    import launch_guard

    if not launch_guard.is_process_alive(info.pid):
        launch_guard.remove_lock()
        result.stopped = True
        result.method = "already-gone"
        result.message = "すでに終了していました"
        return result

    if not _looks_like_our_process(info):
        result.message = (
            f"pid {info.pid} はこのアプリではないようなので止めません。\n"
            f"    手動で確認してください(ロック: {launch_guard.lock_path()})")
        log.warning("pid=%s の照合に失敗したため停止しません", info.pid)
        return result

    log.info("pid=%s を停止します", info.pid)
    if info.create_time:
        # 作成時刻まで一致したプロセスだけを止める(Windows では TerminateProcess)
        if process_tracking.terminate_verified(info.pid, info.create_time, None, FORCE_WAIT_SEC):
            launch_guard.remove_lock()
            result.stopped = True
            result.method = "terminate"
            result.message = "終了しました(応答が無かったため、このアプリのプロセスを止めました)"
            return result
    try:
        os.kill(info.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError) as exc:
        result.message = f"停止できませんでした: {exc}"
        return result

    if _wait_pid_gone(info.pid, FORCE_WAIT_SEC):
        launch_guard.remove_lock()
        result.stopped = True
        result.method = "sigterm"
        result.message = "終了しました"
        return result

    if not force:
        result.message = ("終了要求に応じません。"
                          "強制的に止めるには stop.bat --force を実行してください")
        return result

    try:
        os.kill(info.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    except OSError as exc:
        result.message = f"強制終了できませんでした: {exc}"
        return result
    launch_guard.remove_lock()
    result.stopped = True
    result.method = "sigkill"
    result.message = "強制終了しました"
    return result


def _looks_like_our_process(info) -> bool:
    """そのPIDが本当にこのアプリか。

    1. **プロセス作成時刻**がロックに記録したものと一致すれば確実に本人
       (PID が使い回されていれば作成時刻が違う)
    2. 取れなければ、コマンドラインに**このアプリの置き場所**が入っているか
    どちらでも確かめられないときは False(止めずに知らせる)。
    """
    import launch_guard

    if info.create_time:
        return process_tracking.is_same_process(info.pid, info.create_time)
    cmdline = launch_guard.process_command_line(info.pid)
    if not cmdline:
        log.warning("pid=%s のコマンドラインを取得できませんでした", info.pid)
        return False
    root = (info.app_root or str(app_config.APP_ROOT)).replace("\\", "/")
    normalized = cmdline.replace("\\", "/")
    if root and root in normalized:
        return True
    return "start_app.py" in normalized


def _wait_gone(port: int, timeout: float) -> bool:
    import launch_guard

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if launch_guard.probe_health(port, timeout=0.3) is None:
            return True
        time.sleep(0.2)
    return False


def _wait_pid_gone(pid: int, timeout: float) -> bool:
    import launch_guard

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not launch_guard.is_process_alive(pid):
            return True
        time.sleep(0.2)
    return False


def cleanup_excel(reason: str) -> int:
    """このアプリが起動した Excel / cscript が残っていれば、記録した PID だけ止める。"""
    try:
        return process_tracking.cleanup_leftovers(reason, log)
    except Exception as exc:                      # noqa: BLE001 - 停止の最後で落ちない
        log.warning("Excel の後片付けでエラー: %s", exc)
        return 0


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------
DESKTOP_MESSAGE = ("点検表はデスクトップ版(日報複合ツールの窓)で動いています。"
                   "日報複合ツールの窓の × か、画面の「終了」で閉じてください(stop.bat はブラウザ版だけを止めます)")


def describe_running() -> str:
    """いま動いているほう(`core/instance_guard.py` の錠で見る)。"""
    from core import instance_guard
    kind = instance_guard.running()
    return instance_guard.LABELS.get(kind, "") or "なし"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="点検表 選択・印刷を安全に停止する")
    parser.add_argument("--force", action="store_true", help="印刷中でも中断して止める")
    parser.add_argument("--status", action="store_true", help="状態を見るだけ")
    args = parser.parse_args(argv)

    from core import instance_guard
    desktop = instance_guard.running() == instance_guard.DESKTOP
    if args.status:
        if desktop:
            print("[稼働] デスクトップ版(ポートは使っていません。窓で操作・終了します)")
            return 0
        health = status()
        if health is None:
            print("[--] 起動していません")
        else:
            lock = health["_lock"]
            print(f"[稼働] 版={health.get('version', '?')} pid={lock['pid']} port={lock['port']} "
                  f"ready={health['ready']} 起動={lock['started']}")
        return 0

    if desktop:
        print("[--] " + DESKTOP_MESSAGE)
        instance_guard.bring_desktop_to_front()
        return 1

    result = stop(force=args.force)
    print(result)
    if result.stopped:
        cleaned = cleanup_excel("stop.bat による停止")
        if cleaned:
            print(f"     残っていた Excel を {cleaned} 件終了しました")
    return 0 if result.stopped else 1


if __name__ == "__main__":
    raise SystemExit(main())
