#!/usr/bin/env python3
"""対象プロセスの確認と安全な停止 (基盤仕様書 2.8)

**このアプリだけを止める。** 同じPCで動く別のPythonアプリを巻き添えに
しないことが、このモジュールの唯一の目的。

順序は仕様書のとおり:

    1. まずアプリ自身へ正常終了を要求する (`POST /api/shutdown`)
    2. 応答しないときだけ、記録したPIDを使う
    3. 落とす前に、そのPIDが**本当にこのアプリか**を確かめる
    4. `python.exe` をプロセス名だけで一括終了することはしない

実行中の長時間処理(Accessへの反映など)があるときは、既定では止めずに
知らせる。中断してよいかは利用者が決める(`--force` で中断する)。

使い方:

    python process_manager.py            止める
    python process_manager.py --force    実行中の処理を中断してでも止める
    python process_manager.py --status   状態を見るだけ
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
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from nippou import app_config  # noqa: E402
from nippou.logging_setup import get_launch_logger  # noqa: E402

log = get_launch_logger("process_manager", filename="guard.log")

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
    """動いていれば `/api/health` の中身。動いていなければ `None`。"""
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
    """止める。**印に載っているものだけでなく、動いている全部。**

    印(ロック)には1つぶんしか書けません。印に載っていない残りが動いて
    いると、`stop.bat` を押しても片方しか止まらず、次の起動はそちらへ
    合流します ── 「止めたのにまだ動いている」「合流したと出るのに
    2つ動いている」が、そこから続きます。

    そこで**候補のポートを舐めて、自分と同じアプリを全部止めます**
    (`launch_guard.find_running`)。
    """
    import launch_guard

    result = StopResult()
    info = launch_guard.read_lock()
    running = launch_guard.find_running()

    if info is None and not running:
        result.stopped = True
        result.method = "none"
        result.message = "起動していません"
        return result

    # 印に載っていないものを先に片付ける。**残すと次の起動が合流します**
    extras = [r for r in running
              if info is None or r.port != info.port]
    for extra in extras:
        _stop_extra(extra, force=force)
    if info is None:
        result.stopped = True
        result.method = "scan"
        result.message = (f"印はありませんでしたが、動いていた "
                          f"{len(extras)}個を止めました")
        launch_guard.remove_lock()
        return result
    # 余分を止めたことは、**最後まで残して伝える。** ここで
    # `result.message` に入れてしまうと、この先の分岐が全部上書きする
    extra_note = (f"(印に載っていない {len(extras)}個も止めました) "
                  if extras else "")

    health = launch_guard.probe_health(info.port)
    if not launch_guard.is_our_app(health):
        # 応答しない、または別のアプリ。ロックだけ残っている状態
        if launch_guard.is_process_alive(info.pid):
            return _noted(_stop_by_pid(info, result, force=force), extra_note)
        launch_guard.remove_lock()
        result.stopped = True
        result.method = "stale-lock"
        result.message = "動いていませんでした(残っていたロックを片付けました)"
        return _noted(result, extra_note)

    # --- 1. 正常終了を要求する ---
    asked = _request_shutdown(info.port, info.token, force=force)
    if asked.get("busy"):
        result.busy_jobs = asked.get("running", [])
        result.message = (f"実行中の処理があります: {', '.join(result.busy_jobs)}\n"
                          f"    中断して止めるには --force を付けてください")
        return _noted(result, extra_note)

    if asked.get("ok") and _wait_gone(info.port, GRACEFUL_WAIT_SEC):
        launch_guard.remove_lock()
        result.stopped = True
        result.method = "graceful"
        result.message = "正常に終了しました"
        return _noted(result, extra_note)

    # --- 2. 応答しないときだけPIDを使う ---
    return _noted(_stop_by_pid(info, result, force=force), extra_note)


def _noted(result: StopResult, note: str) -> StopResult:
    """止めた余分のことを、どの結末の文にも残す。"""
    if note:
        result.message = note + result.message
    return result


def _stop_extra(extra, *, force: bool) -> None:
    """印に載っていない1つを止める。**トークンが無いのでPIDで。**

    印が無いということは起動トークンも分からないので、`/api/shutdown`
    は通りません(通ってはいけません)。落とす前に、そのPIDが本当に
    このアプリかを確かめます ── 無関係なものを巻き添えにしない。
    """
    import launch_guard

    log.warning("印に載っていないインスタンスを止めます: port=%s pid=%s",
                extra.port, extra.pid)
    if not extra.pid or not launch_guard.is_process_alive(extra.pid):
        return
    fake = launch_guard.LockInfo(
        app_id="", pid=extra.pid, port=extra.port, url=extra.url,
        started_at=0.0, app_root=extra.app_root)
    if not _looks_like_our_process(fake):
        log.warning("pid=%s の照合に失敗したため止めません", extra.pid)
        return
    try:
        os.kill(extra.pid, signal.SIGTERM)
    except OSError as exc:                        # noqa: BLE001
        log.warning("pid=%s を止められませんでした: %s", extra.pid, exc)
        return
    if _wait_pid_gone(extra.pid, FORCE_WAIT_SEC) or not force:
        return
    try:
        os.kill(extra.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    except OSError:                               # noqa: BLE001
        pass


def _request_shutdown(port: int, token: str, *, force: bool) -> dict:
    """`POST /api/shutdown` で正常終了を要求する(基盤仕様書 2.8 の1段目)。

    トークンはロックファイルから読む。ロックは利用者ごとのローカル領域に
    あるので、読める相手はそもそもプロセスを直接落とせる。
    """
    import launch_guard

    url = f"http://127.0.0.1:{port}/api/shutdown"
    body = json.dumps({"force": force}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Tool-Token"] = token
    try:
        # プロキシを経由しない送信口を使う(`launch_guard` の注釈を参照)。
        # 社内PCのプロキシ設定に 127.0.0.1 の除外が無いと、自分自身への
        # 停止要求までプロキシへ送られて届かない
        with launch_guard.local_request(url, timeout=5, data=body,
                                        method="POST", headers=headers) as res:
            payload = json.loads(res.read().decode("utf-8"))
            return {"ok": bool(payload.get("stopped"))}
    except urllib.error.HTTPError as exc:
        if exc.code == 409:                        # 実行中の処理がある
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
                          "強制的に止めるには --force を付けてください")
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

    コマンドラインに**このアプリの置き場所**が入っていることを確かめる。
    別のフォルダにある同じツールや、無関係なPythonを巻き添えにしない。
    コマンドラインが取れない環境では False にして、止めずに知らせる
    (誤って別のアプリを落とすより、止まらないほうが害が小さい)。

    【`launch_guard.looks_like_our_process` と**わざと逆に倒してある**】
    向こうは分からないときに True(= このアプリだ)へ倒します。あちらの
    判断は「起動してよいか」なので、分からないまま起動すると**2つ
    動きます**。こちらの判断は「落としてよいか」なので、分からないまま
    落とすと**別のアプリを巻き添え**にします。同じ問いに見えて、
    間違えたときの害が反対側にあるので、1つにまとめないでください。
    """
    import launch_guard

    cmdline = launch_guard.process_command_line(info.pid)
    if not cmdline:
        log.warning("pid=%s のコマンドラインを取得できませんでした", info.pid)
        return False

    root = (info.app_root or str(app_config.APP_ROOT)).replace("\\", "/")
    normalized = cmdline.replace("\\", "/")
    if root and root in normalized:
        return True
    # 起動スクリプト名でも照合する(相対パスで起動された場合)
    return "start_app.py" in normalized


def _wait_gone(port: int, timeout: float) -> bool:
    """そのポートが応答しなくなるまで待つ。"""
    import launch_guard

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if launch_guard.probe_health(port, timeout=0.5) is None:
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


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------
def _print_status() -> int:
    health = status()
    if health is None:
        print("起動していません")
        return 0
    lock = health.get("_lock", {})
    print(f"起動中: {health.get('display_name')} {health.get('version')}")
    print(f"  URL      : http://127.0.0.1:{lock.get('port')}/")
    print(f"  PID      : {lock.get('pid')}")
    print(f"  起動時刻 : {lock.get('started')}")
    print(f"  本体     : {lock.get('app_root')}")
    print(f"  準備完了 : {'はい' if health.get('ready') else 'いいえ'}"
          f" ({health.get('stage')})")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="日報管理ツールを停止する")
    parser.add_argument("--force", action="store_true",
                        help="実行中の処理を中断してでも止める")
    parser.add_argument("--status", action="store_true",
                        help="状態を見るだけ(止めない)")
    args = parser.parse_args(argv)

    if args.status:
        return _print_status()

    result = stop(force=args.force)
    print(result)
    return 0 if result.stopped else 1


if __name__ == "__main__":
    raise SystemExit(main())
