#!/usr/bin/env python3
"""統合ツールのブラウザ版を止める(`stop.bat` から)

    python process_manager.py            ブラウザ版(入口と各ツール)に終わってもらう
    python process_manager.py --force    処理の途中でも止める
    python process_manager.py --status   いま何が動いているかを出す

**デスクトップ版(統合ツール.exe の窓)は止めない。** 窓の × か「終了」で閉じる
(閉じるときに全ツールの「終わってよいか」を訊くため)。動いていれば窓を前に出す。
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

DESKTOP_MESSAGE = ("デスクトップ版(統合ツール.exe の窓)が動いています。"
                   "窓の × か「終了」で閉じてください(stop.bat では止めません)。")


def describe() -> list[str]:
    from portal import catalog, instance_guard
    from portal.browser_tools import find_running

    import start_app

    lines = []
    kind = instance_guard.running()
    if kind == instance_guard.DESKTOP:
        lines.append("[稼働] デスクトップ版(統合ツール.exe)")
    lock = start_app.read_lock()
    if kind == instance_guard.BROWSER and lock:
        lines.append(f"[稼働] ブラウザ版の入口 pid={lock.get('pid')} {lock.get('url')}")
    for tool in catalog.load().tools:
        running = find_running(tool)
        if running is not None:
            lines.append(f"[稼働] {tool.name}(ブラウザ版) pid={running.pid} port={running.port}")
    return lines or ["動いているものはありません"]


def stop(*, force: bool = False) -> int:
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
        if status == 409:
            print("処理の途中のツールがあります:\n" + str(body.get("message") or ""))
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
                print("それでも止めるときは stop.bat --force")
                return 2
        tools.stop_all(force=force)
        print("各ツールのブラウザ版に終わってもらいました: " + "、".join(r.tool.title for r in left))
    return code


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="処理の途中でも止める")
    parser.add_argument("--status", action="store_true", help="いま何が動いているかを出す")
    args = parser.parse_args(argv)
    if args.status:
        print("\n".join(describe()))
        return 0
    return stop(force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
