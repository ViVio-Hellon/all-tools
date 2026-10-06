#!/usr/bin/env python3
"""デスクトップ版(exe)を本当に起動して確かめる(GitHub Actions の Windows でも流す)

確かめること:
    1. 窓の画面(WebView)が Python から届く
       ── ログに `画面 GET / → 200(業務画面)` が出る
          (WebView → 外枠(Rust)→ Python → 外枠 → WebView が1周した証拠)
    2. **どのプロセスもポートで待ち受けていない**(exe・Python・WebView の子プロセス)
    3. デスクトップ版が動いているあいだ、ブラウザ版(start_app.py)は起動を断る
    4. exe を止めると Python も終わる(取り残さない)
    5. ブラウザ版が動いているあいだ、デスクトップ版は Python を起こさずに断る

使い方:
    python scripts/desktop_smoke.py --exe src-tauri/target/release/InspectionSheet.exe
    (Linux では DISPLAY が要る。例: xvfb-run python scripts/desktop_smoke.py --exe ...)

模擬モード(`--demo`)で動かす。作業用のフォルダ(ログ・設定)は一時フォルダに作る。
"""
from __future__ import annotations

import argparse
import os
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


def log_text(work: Path) -> str:
    logs = sorted((work / "local" / "logs").glob("inspection_*.log"))
    return "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in logs)


def wait_for(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.5)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", required=True)
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()

    work = Path(tempfile.mkdtemp(prefix="desktop_smoke_"))
    env = dict(os.environ, INSPECTION_ROOT=str(ROOT), INSPECTION_LOCAL_DIR=str(work / "local"),
               INSPECTION_DISTRIBUTION_DIR=str(work / "配布設定"), PYTHONIOENCODING="utf-8")
    if os.environ.get("SMOKE_PYTHON"):
        env["INSPECTION_PYTHON"] = os.environ["SMOKE_PYTHON"]
    ok = True

    def result(good: bool, text: str) -> None:
        nonlocal ok
        print(("[OK] " if good else "[NG] ") + text, flush=True)
        ok = ok and good

    # ---- 1〜4: デスクトップ版を起動する ----
    started = time.monotonic()
    app = subprocess.Popen([args.exe, "--demo"], env=env, cwd=str(ROOT))
    print(f"起動しました: pid={app.pid} 作業={work}", flush=True)
    served = wait_for(lambda: app.poll() is not None or "(業務画面)" in log_text(work), args.timeout)
    if app.poll() is not None:
        result(False, f"exe が先に終わりました(終了コード {app.returncode})")
    elif served:
        line = next(line for line in log_text(work).splitlines() if "(業務画面)" in line)
        result(True, f"画面が Python から届きました({time.monotonic() - started:.1f}秒): {line.split('| ')[-1]}")
    else:
        result(False, "時間内に画面が届きませんでした。ログの末尾:\n" + "\n".join(log_text(work).splitlines()[-30:]))

    if app.poll() is None:
        tree = {app.pid, *children(app.pid)}
        ports = listening_ports(tree)
        result(not ports, f"待ち受けはありません(調べたプロセス {len(tree)} 個)" if not ports
               else f"待ち受けているプロセスがあります: {ports}")

        browser = subprocess.run([sys.executable, "start_app.py", "--demo", "--no-browser"], env=env,
                                 cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=60)
        refused = browser.returncode != 0 and "デスクトップ版" in (browser.stdout + browser.stderr)
        result(refused, "デスクトップ版が動いているあいだ、ブラウザ版は起動を断りました" if refused
               else f"ブラウザ版が断りませんでした(終了コード {browser.returncode})\n{browser.stdout[-800:]}{browser.stderr[-800:]}")

        pythons = [pid for pid in tree if pid != app.pid]
        app.terminate()
        app.wait(timeout=30)
        left = [p for p in pythons if alive(p)] if not wait_for(
            lambda: not any(alive(p) for p in pythons), 20) else []
        result(not left, "exe を止めたら子プロセス(Python など)も終わりました" if not left
               else f"exe を止めても残ったプロセスがあります: {left}")

    # ---- 5: ブラウザ版が先に動いているとき ----
    browser = subprocess.Popen([sys.executable, "start_app.py", "--demo", "--no-browser"], env=env,
                               cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        up = wait_for(lambda: "待ち受け開始" in log_text(work) and log_text(work).count("待ち受け開始") >= 1, 60)
        result(up, "ブラウザ版を起動しました" if up else "ブラウザ版が起動しませんでした")
        before = log_text(work).count("外枠からの要求を受け付けます")
        second = subprocess.Popen([args.exe, "--demo"], env=env, cwd=str(ROOT))
        time.sleep(8)
        kids = children(second.pid) if second.poll() is None else []
        started_python = log_text(work).count("外枠からの要求を受け付けます") > before
        result(not started_python and not kids,
               "ブラウザ版が動いているあいだ、デスクトップ版は Python を起こさずに断りました" if not started_python
               else "デスクトップ版がブラウザ版と同時に起動しました")
        if second.poll() is None:
            second.kill()
            second.wait(timeout=30)
    finally:
        subprocess.run([sys.executable, "process_manager.py", "--force"], env=env, cwd=str(ROOT),
                       capture_output=True, timeout=60)
        try:
            browser.wait(timeout=30)
        except subprocess.TimeoutExpired:
            browser.kill()

    print("結果:", "OK" if ok else "NG")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
