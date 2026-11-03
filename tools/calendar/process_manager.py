#!/usr/bin/env python3
"""対象プロセスの確認と安全な停止 (基盤仕様書 2.8)

**このアプリだけを止める。** 同じPCで動く別のPythonアプリを巻き添えに
しないことが、このモジュールの唯一の目的。

順序は仕様書のとおり:

    1. まずアプリ自身へ正常終了を要求する (``POST /api/shutdown``)
    2. 応答しないときだけ、記録したPIDを使う
    3. 落とす前に、そのPIDが**本当にこのアプリか**を確かめる
    4. ``python.exe`` をプロセス名だけで一括終了することはしない

Access への送信中は、既定では止めずに知らせる。中断してよいかは
利用者が決める(``--force`` で中断する)。

使い方::

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
sys.path.insert(0, str(APP_ROOT))
# 短い処理なので Python の .py の写しを書かない(一式を共有に置くと、全部の PC がツールのフォルダの
# `__pycache__` へ書きに行き、更新の上書きとぶつかる)
sys.dont_write_bytecode = True

from calendar_app import app_config  # noqa: E402
from calendar_app.logging_utils import get_logger  # noqa: E402

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
# 止める入口の決まり(業務ツール統合ランチャー 1.8.9「更新のため全 PC で止める」)
# ------------------------------------------------------------------
#: 終了コード: 0 = 止めた・動いていない / 3 = 処理中なので止めなかった / 1 = そのほか止められなかった。
#: **標準出力の1行目が理由**(「実行中の処理があります: 取り込み」)。ランチャーの管理者の一覧と、
#: 利用者への知らせに出る
EXIT_STOPPED, EXIT_FAILED, EXIT_BUSY = 0, 1, 3

#: stop.bat に渡されても読み飛ばす引数(pause を飛ばす印。bat が見る)
NOPAUSE_ARGS = ("/nopause", "--nopause", "-nopause")

# cp932 の窓で書けない字(書けずに「?」になる)を、似た字に直す
_CONSOLE_SAFE = str.maketrans({"\u2014": "\u2015", "\u2013": "-", "\u00a0": " ", "\u2212": "-"})


def one_line(text: str) -> str:
    """1行にする(改行・続く空白を1つの空白に)。cp932 で書けない字を直す。"""
    return " ".join(str(text or "").split()).translate(_CONSOLE_SAFE)


def console_stdout() -> None:
    """標準出力・標準エラーを、呼んだ窓(cmd。bat は chcp 932)の文字コードに合わせる。

    Windows の Python は、窓へ直接書くときは化けないが、**パイプ**(ランチャーが受け取る)へは
    既定の文字コードで書く。Python の UTF-8 モード(3.15 からは既定)では UTF-8 になり、
    cp932 で読むランチャーの一覧で化ける。窓の文字コード(`GetConsoleOutputCP`、無ければ cp932)
    に合わせ、書けない字は「?」にする(落ちない)。PYTHONIOENCODING を決めてあればそれに従う。
    """
    if os.name != "nt" or os.environ.get("PYTHONIOENCODING"):
        return
    encoding = "cp932"
    try:
        import codecs
        import ctypes

        page = int(ctypes.WinDLL("kernel32").GetConsoleOutputCP())
        if page:
            codecs.lookup(f"cp{page}")
            encoding = f"cp{page}"
    except (OSError, AttributeError, LookupError, ValueError):
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is None or stream.isatty():
                continue
            stream.reconfigure(encoding=encoding, errors="replace")
        except (AttributeError, ValueError, OSError):
            continue

def report(result: StopResult) -> int:
    """結果を出して終了コードを返す。**標準出力の1行目が理由**(止めなかったとき)。"""
    if result.stopped:
        lines, code = [str(result)], EXIT_STOPPED
    elif result.busy_jobs:
        lines = ["実行中の処理があります: " + "・".join(str(j) for j in result.busy_jobs),
                 "    中断して止めるには stop.bat --force を実行してください(途中の処理は捨てます)"]
        code = EXIT_BUSY
    else:
        lines = [line for line in result.message.splitlines() if line.strip()] or ["止められませんでした"]
        code = EXIT_FAILED
    print(one_line(lines[0]))
    for line in lines[1:]:
        print(line.translate(_CONSOLE_SAFE))
    return code


# ---------------------------------------------------------------------------
# 状態
# ---------------------------------------------------------------------------
def status() -> Optional[dict]:
    """動いているか。動いていれば ``/api/health`` の中身。"""
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


# ---------------------------------------------------------------------------
# 停止
# ---------------------------------------------------------------------------
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
                          f"    中断して止めるには --force を付けてください")
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
    """``POST /api/shutdown`` で正常終了を要求する(基盤仕様書 2.8 の1段目)。

    トークンはロックファイルから読む。ロックは利用者ごとのローカル領域に
    あるので、読める相手はそもそもプロセスを直接落とせる。
    """
    import launch_guard

    url = f"http://{app_config.host()}:{port}/api/shutdown"
    body = json.dumps({"force": force}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Tool-Token"] = token
    try:
        # プロキシを経由しない送信口を使う(``launch_guard`` の注釈を参照)。
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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    console_stdout()
    parser = argparse.ArgumentParser(
        description=f"{app_config.display_name()}を安全に停止する")
    parser.add_argument("--force", action="store_true",
                        help="実行中の処理を中断してでも止める")
    parser.add_argument("--status", action="store_true", help="状態を見るだけ")
    # ``stop.bat`` が余分な引数を渡しても落ちないように受けておく
    parser.add_argument("--all", action="store_true", help=argparse.SUPPRESS)
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args([a for a in argv if a.lower() not in NOPAUSE_ARGS])

    if args.status:
        health = status()
        if health is None:
            print("[--] 起動していません")
        else:
            lock = health["_lock"]
            print(f"[稼働] pid={lock['pid']} port={lock['port']} "
                  f"ready={health['ready']} 起動={lock['started']}")
        return 0

    return report(stop(force=args.force))


if __name__ == "__main__":
    raise SystemExit(main())
