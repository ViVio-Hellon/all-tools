#!/usr/bin/env python3
"""デスクトップ版(exe)を本当に起動して確かめる(GitHub Actions の Windows でも流す)

確かめること:
    0. 画面からの操作が守り(別のページ・知らない宛先)に断られていない
    1. 窓の画面(WebView)が Python から届き、画面の JS が動く
       ── 動作ログに `操作 POST /api/screen/claim → 200` の行が出る
          (カレンダー画面の JS が名乗りを送った = WebView → 外枠(Rust)→ Python →
           外枠 → WebView が1周し、さらに画面から操作が届いた証拠)
    2. **どのプロセスもポートで待ち受けていない**(exe・Python・WebView の子プロセス)
    3. exe を止めると Python も終わる(取り残さない)

使い方:
    python tools/desktop_smoke.py --exe src-tauri/target/release/LineCalendar.exe
    (Linux では DISPLAY が要る。例: xvfb-run python tools/desktop_smoke.py --exe ...)

梱包資材総合ツール(python-web-tools)の ``scripts/desktop_smoke.py`` と同じ作り。

作業用のフォルダ(DB・設定・ログ)は一時フォルダに作る。本番の領域は触らない。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WINDOWS = os.name == "nt"


def children(pid: int) -> list[int]:
    """pid の子孫(孫も)。"""
    if WINDOWS:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | ForEach-Object { \"$($_.ProcessId) $($_.ParentProcessId)\" }"],
            capture_output=True, text=True, errors="replace").stdout
        pairs = [tuple(map(int, line.split())) for line in out.splitlines() if line.strip()]
    else:
        pairs = []
        for name in os.listdir("/proc"):
            if name.isdigit():
                try:
                    stat = Path(f"/proc/{name}/stat").read_text()
                    pairs.append((int(name), int(stat.rsplit(")", 1)[1].split()[1])))
                except (OSError, ValueError, IndexError):
                    pass
    found, frontier = [], [pid]
    while frontier:
        parent = frontier.pop()
        for child, ppid in pairs:
            if ppid == parent and child not in found:
                found.append(child)
                frontier.append(child)
    return found


def listening_ports(pids: set[int]) -> dict[int, list[int]]:
    """その pid たちが待ち受けている TCP ポート。"""
    result: dict[int, list[int]] = {}
    if WINDOWS:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, errors="replace").stdout
        out += subprocess.run(["netstat", "-ano", "-p", "TCPv6"], capture_output=True, text=True, errors="replace").stdout
        for line in out.splitlines():
            cols = line.split()
            if len(cols) >= 5 and cols[3].upper() == "LISTENING" and cols[4].isdigit():
                pid = int(cols[4])
                if pid in pids:
                    result.setdefault(pid, []).append(int(cols[1].rsplit(":", 1)[1]))
        return result
    inodes: dict[str, int] = {}
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        if os.path.exists(name):
            for line in Path(name).read_text().splitlines()[1:]:
                cols = line.split()
                if cols[3] == "0A":
                    inodes[cols[9]] = int(cols[1].split(":")[1], 16)
    for pid in pids:
        try:
            fds = os.listdir(f"/proc/{pid}/fd")
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(f"/proc/{pid}/fd/{fd}")
            except OSError:
                continue
            if target.startswith("socket:[") and target[8:-1] in inodes:
                result.setdefault(pid, []).append(inodes[target[8:-1]])
    return result


def alive(pid: int) -> bool:
    if WINDOWS:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, errors="replace").stdout
        return str(pid) in out
    return os.path.exists(f"/proc/{pid}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", required=True)
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()

    work = Path(tempfile.mkdtemp(prefix="desktop_smoke_"))
    env = dict(os.environ,
               CALENDAR_ROOT=str(ROOT),
               CALENDAR_HOME=str(work / "home"),
               CALENDAR_LOCAL_DIR=str(work / "local"),
               CALENDAR_PC_NAME="SMOKE-PC")
    if os.environ.get("SMOKE_PYTHON"):
        env["CALENDAR_PYTHON"] = os.environ["SMOKE_PYTHON"]
    started = time.monotonic()
    app = subprocess.Popen([args.exe], env=env, cwd=str(ROOT))
    print(f"起動しました: pid={app.pid} 作業={work}", flush=True)

    ok = True
    footprint = re.compile(r"操作 POST /api/screen/claim → 200")
    seen = ""
    while time.monotonic() - started < args.timeout:
        if app.poll() is not None:
            print(f"[NG] exe が先に終わりました(終了コード {app.returncode})")
            ok = False
            break
        logs = sorted((work / "local" / "logs").glob("calendar_*.log"))
        text = logs[-1].read_text(encoding="utf-8", errors="replace") if logs else ""
        match = footprint.search(text)
        if match:
            seen = match.group(0)
            break
        time.sleep(0.5)
    if seen:
        print(f"[OK] 画面が Python から届きました({time.monotonic() - started:.1f}秒): {seen}")
        # **守りに断られていないか。** 外枠が渡す Host と画面の Origin が食い違うと、
        # 画面は出るのに操作がすべて「別のページ」として断られる(Windows で起きた)
        logs = sorted((work / "local" / "logs").glob("calendar_*.log"))
        text = logs[-1].read_text(encoding="utf-8", errors="replace") if logs else ""
        refused = [line for line in text.splitlines() if "bad_origin" in line or "bad_host" in line]
        if refused:
            print("[NG] 画面からの操作が守りに断られています:")
            print("\n".join(refused[:5]))
            ok = False
        else:
            print("[OK] 画面からの操作は断られていません(Origin と Host が合っている)")
    elif ok:
        print("[NG] 時間内に画面が届きませんでした。ログの末尾:")
        logs = sorted((work / "local" / "logs").glob("*.log"))
        for log in logs:
            print(f"--- {log.name}")
            print("\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-30:]))
        ok = False

    if app.poll() is None:
        tree = {app.pid, *children(app.pid)}
        ports = listening_ports(tree)
        if ports:
            print(f"[NG] 待ち受けているプロセスがあります: {ports}")
            ok = False
        else:
            print(f"[OK] 待ち受けはありません(調べたプロセス {len(tree)} 個)")
        pythons = [pid for pid in tree if pid != app.pid]
        app.terminate()
        app.wait(timeout=30)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and any(alive(p) for p in pythons):
            time.sleep(0.5)
        left = [p for p in pythons if alive(p)]
        if left:
            print(f"[NG] exe を止めても残ったプロセスがあります: {left}")
            ok = False
        else:
            print("[OK] exe を止めたら子プロセス(Python など)も終わりました")
    print("結果:", "OK" if ok else "NG")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
