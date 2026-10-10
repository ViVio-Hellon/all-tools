#!/usr/bin/env python3
"""日報複合ツールのブラウザ版を止める(`stop.bat` から)・ランチャーの入口

    python process_manager.py            ブラウザ版(入口と各ツール)に終わってもらう
    python process_manager.py --force    処理の途中でも止める
    python process_manager.py --status   いま何が動いているかを出す
    python process_manager.py --check    起動確認(launcher_check.bat)。0 使える / 2 準備中 / 1 動いていない
    python process_manager.py --launcher 終了の入口(launcher_stop.bat)。デスクトップ版も閉じる

`stop.bat` は**デスクトップ版(日報複合ツール.exe の窓)を止めない。** 窓の × か「終了」で閉じる
(閉じるときに全ツールの「終わってよいか」を訊くため)。動いていれば窓を前に出す。

業務ツール統合ランチャーの終了の入口(`--launcher`)は、デスクトップ版なら**窓に「閉じて」と
頼む**(× と同じ流れ。プロセスは落とさない)。ブラウザ版なら `stop.bat` と同じ。
ランチャーは最後の1行を理由として利用者に見せるので、理由は最後に出す。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
from pathlib import Path
from typing import Optional

APP_ROOT = Path(__file__).resolve().parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

# 画面に打ちかけを置いてもらうのを待つ上限(秒)。ランチャーは stop.bat を 20 秒で見切る
ASKING_WAIT_SEC = 15.0

# 終了の入口: デスクトップ版の窓が閉じるのを待つ上限(秒)。ランチャーは入口を 20 秒で見切る
DESKTOP_CLOSE_WAIT_SEC = 16.0

DESKTOP_MESSAGE = ("デスクトップ版(日報複合ツール.exe の窓)が動いています。"
                   "窓の × か「終了」で閉じてください(stop.bat では止めません)。")


def describe() -> list[str]:
    from portal import catalog, instance_guard
    from portal.browser_tools import find_running

    import start_app

    lines = []
    kind = instance_guard.running()
    if kind == instance_guard.DESKTOP:
        lines.append("[稼働] デスクトップ版(日報複合ツール.exe)")
    lock = start_app.read_lock()
    if kind == instance_guard.BROWSER and lock:
        lines.append(f"[稼働] ブラウザ版の入口 pid={lock.get('pid')} {lock.get('url')}")
    for tool in catalog.load().tools:
        running = find_running(tool)
        if running is not None:
            lines.append(f"[稼働] {tool.name}(ブラウザ版) pid={running.pid} port={running.port}")
    return lines or ["動いているものはありません"]


def stop(*, force: bool = False, hints: bool = True) -> int:
    """ブラウザ版を止める。0 止めた / 1 デスクトップ版が動いている / 2 止めなかった(理由を出す)。

    `hints` は stop.bat の案内(「stop.bat --force」)。ランチャーの入口では出さない
    (最後の1行が理由として利用者に見えるため)。
    """
    from portal import catalog, instance_guard
    from portal.browser_tools import BrowserTools, _request, is_alive

    import start_app

    if instance_guard.running() == instance_guard.DESKTOP:
        print(DESKTOP_MESSAGE)
        instance_guard.bring_desktop_to_front()
        return 1
    lock = start_app.read_lock()
    code = 0
    if lock and lock.get("port") and is_alive(int(lock.get("pid") or 0)):
        base = f"http://127.0.0.1:{lock['port']}"
        try:
            status, body = _request(f"{base}/api/shutdown",
                                    data=json.dumps({"force": force}).encode("utf-8"),
                                    headers={"Content-Type": "application/json",
                                             "X-Tool-Token": str(lock.get("token") or "")}, timeout=10)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"入口に届きませんでした: {exc}")
            status, body = 0, {}
        pid = int(lock.get("pid") or 0)
        if status == 409 and body.get("reason") == "asking":
            # 開いている画面に、打ちかけを置いてから閉じるよう頼んだ。置けたら入口が自分で
            # 終わる。少しだけ待つ(ランチャーは stop.bat を 20 秒で見切る)
            print(str(body.get("message") or ""), flush=True)
            deadline = time.monotonic() + ASKING_WAIT_SEC
            while time.monotonic() < deadline and is_alive(pid):
                time.sleep(0.3)
            if is_alive(pid):
                print("まだ画面で確認中です。画面を見てください(済めば自分で終わります)")
                return 2
            status = 200
        elif status == 409:
            print("止めませんでした:\n" + str(body.get("message") or ""))
            if hints and body.get("reason") != "refused":
                print("それでも止めるときは stop.bat --force")
            return 2
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and is_alive(int(lock.get("pid") or 0)):
            time.sleep(0.3)
        print("ブラウザ版の入口に終わってもらいました")
    # 入口が居なくても、各ツールのブラウザ版が残っていれば止める
    tools = BrowserTools(catalog.load())
    left = tools.running()
    if left:
        if not force:
            busy = tools.busy()
            if busy:
                print("処理の途中のツールがあります:\n" + "\n".join(busy))
                if hints:
                    print("それでも止めるときは stop.bat --force")
                return 2
        tools.stop_all(force=force)
        print("各ツールのブラウザ版に終わってもらいました: " + "、".join(r.tool.title for r in left))
    return code


# ------------------------------------------------------------------
# 業務ツール統合ランチャーの入口(docs/ランチャー連携.md)
# ------------------------------------------------------------------
CHECK_READY, CHECK_STOPPED, CHECK_STARTING = 0, 1, 2


def _desktop_windows() -> Optional[list[int]]:
    from portal import app_config, desktop_window, instance_guard

    # 大きなタブの窓の題名は「日報複合ツール」「日報複合ツール — 日報」(画面の題名に合わせて変わる)
    return desktop_window.main_windows(instance_guard.DESKTOP_EXES, app_config.display_name())


def check() -> int:
    """起動確認(`launcher_check.bat`)。最後の1行は、起動中の窓に出る「準備の段階」。"""
    from portal import app_config, instance_guard
    from portal.browser_tools import _request, is_alive

    import start_app

    kind = instance_guard.running()
    if kind == instance_guard.DESKTOP:
        windows = _desktop_windows()
        if windows is None or windows:            # 窓を確かめられない環境は、錠を信じる
            print("デスクトップ版が動いています")
            return CHECK_READY
        print("デスクトップ版の窓を開いています")
        return CHECK_STARTING
    lock = start_app.read_lock()
    if lock and lock.get("port") and is_alive(int(lock.get("pid") or 0)):
        try:
            status, body = _request(f"http://127.0.0.1:{lock['port']}/api/health", timeout=3)
        except (urllib.error.URLError, OSError, ValueError):
            status, body = 0, {}
        if status == 200 and body.get("app_id") == app_config.app_id() and body.get("ready", True):
            print("ブラウザ版が動いています")
            return CHECK_READY
        print("ブラウザ版を準備しています")
        return CHECK_STARTING
    if kind == instance_guard.BROWSER:
        print("ブラウザ版を起動しています")
        return CHECK_STARTING
    print("動いていません")
    return CHECK_STOPPED


def launcher_stop(*, force: bool = False) -> int:
    """終了の入口(`launcher_stop.bat`)。0 終了の手続きをした / 1 止めなかった(最後の行が理由)。

    デスクトップ版は**窓に「閉じて」と頼む**(× と同じ: 打ちかけを置いてもらい、全ツールに
    訊き、途中の処理があれば1つの確認)。確認に答えるまでは閉じない ── 待ちきれなければ
    「確認しています」と返す(強制終了するかは利用者がランチャーで選ぶ。`--force` のときは
    ここでは何もせず、ランチャーの止め方に任せる)。
    """
    from portal import instance_guard

    if instance_guard.running() == instance_guard.DESKTOP:
        if force:
            print("デスクトップ版は窓を閉じて止めます(強制終了はランチャーの止め方で続けます)")
            return 1
        windows = _desktop_windows()
        if windows is None:
            print("デスクトップ版の窓を確かめられません(Windows 以外)")
            return 1
        if not windows:
            print("デスクトップ版の窓が見つかりません(窓の「終了」で閉じてください)")
            return 1
        from portal import desktop_window

        desktop_window.ask_to_close(windows)
        deadline = time.monotonic() + DESKTOP_CLOSE_WAIT_SEC
        while time.monotonic() < deadline and instance_guard.running() == instance_guard.DESKTOP:
            time.sleep(0.3)
        if instance_guard.running() != instance_guard.DESKTOP:
            print("日報複合ツールの窓を閉じました")
            return 0
        print("日報複合ツールの窓で、閉じてよいかの確認をしています(窓を見て答えてください)")
        return 1
    code = stop(force=force, hints=False)
    if code == 0:
        print("日報複合ツールを終了しました")
    return 0 if code == 0 else 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="処理の途中でも止める")
    parser.add_argument("--status", action="store_true", help="いま何が動いているかを出す")
    parser.add_argument("--check", action="store_true", help="起動確認(launcher_check.bat)")
    parser.add_argument("--launcher", action="store_true", help="終了の入口(launcher_stop.bat)")
    args = parser.parse_args(argv)
    if args.status:
        print("\n".join(describe()))
        return 0
    if args.check:
        return check()
    if args.launcher:
        return launcher_stop(force=args.force)
    return stop(force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
