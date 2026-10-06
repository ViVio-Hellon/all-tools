#!/usr/bin/env python3
"""デスクトップ版(exe)を本当に起動して確かめる(GitHub Actions の Windows でも流す)

確かめること:
    1. 窓の画面(WebView)が Python から届く
       ── ログに `画面 GET /board → 200` が出る
          (WebView → 外枠(Rust)→ Python → 外枠 → WebView が1周した証拠)
    2. 画面の JS が動き、外枠の機能(`window.__TAURI__`)が見えている
       ── ログに「デスクトップ版の窓から画面がつながりました」が出る
    3. **どのプロセスもポートで待ち受けていない**(exe・Python・WebView の子プロセス)
    4. exe を止めると Python も終わる(取り残さない)

使い方:
    python scripts/desktop_smoke.py --exe src-tauri/target/release/KanbanSystem.exe
    (Linux では DISPLAY が要る。例: xvfb-run python scripts/desktop_smoke.py --exe ...)

作業用のフォルダ(DB・設定・ログ)は一時フォルダに作る。本番の領域は触らない。
"""
from __future__ import annotations

import argparse
import json
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
            capture_output=True, text=True).stdout
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
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True).stdout
        out += subprocess.run(["netstat", "-ano", "-p", "TCPv6"], capture_output=True, text=True).stdout
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
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    return os.path.exists(f"/proc/{pid}")


def make_share(share: Path) -> None:
    """共有フォルダの代わり(看板マスタに L1 の看板を 2 枚)。"""
    import sqlite3

    share.mkdir(parents=True)
    cols = ("[管理番号] NUMERIC, [資材] TEXT, [サイズ] TEXT, [欲] TEXT, [不] TEXT, [更新日] DATETIME,"
            " [発送] TEXT, [倉庫確認日時] DATETIME, [常設品] TEXT, [保留] TEXT, [注文中日時] DATETIME")
    db = sqlite3.connect(str(share / "看板マスタ.sqlite3"))
    db.execute(f"CREATE TABLE [看板_L1] ({cols})")
    db.executemany("INSERT INTO [看板_L1] VALUES (?, ?, ?, '', '〇', '', '', '', '〇', '', '')",
                   [(1, "外装紙", "2200"), (2, "テープ", "50")])
    db.execute("CREATE TABLE [Form状態管理] ([ライン名] TEXT, [状態] TEXT, [更新日時] DATETIME, [ホスト名] TEXT)")
    db.commit()
    db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", required=True)
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()

    work = Path(tempfile.mkdtemp(prefix="desktop_smoke_"))
    make_share(work / "share")
    # 設定(共有DB・手元の SQLite・ログの置き場所)。担当ラインは L1
    settings = work / "settings"
    settings.mkdir()
    (settings / "config.json").write_text(json.dumps({
        "shared_db_path": str(work / "share" / "看板マスタ.sqlite3"),
        "sqlite_path": str(work / "local" / "kanban.sqlite3"),
        "log_dir": str(work / "logs"), "line": "L1",
    }), encoding="utf-8")
    env = dict(os.environ,
               KANBAN_ROOT=str(ROOT),
               KANBAN_CONFIG=str(settings / "config.json"),
               KANBAN_SETTINGS_DIR=str(settings),
               KANBAN_DISTRIBUTION_DIR=str(work / "配布設定"),
               KANBAN_LOCAL_DIR=str(work / "local"),
               KANBAN_LOGIN_ID="smoke", KANBAN_PC_NAME="SMOKE-PC")
    if os.environ.get("SMOKE_PYTHON"):
        env["KANBAN_PYTHON"] = os.environ["SMOKE_PYTHON"]
    started = time.monotonic()
    app = subprocess.Popen([args.exe], env=env, cwd=str(ROOT))
    print(f"起動しました: pid={app.pid} 作業={work}", flush=True)

    ok = True
    page = re.compile(r"画面 GET /(board|settings)\S* → 200")
    joined = "デスクトップ版の窓から画面がつながりました"
    seen = ""
    while time.monotonic() - started < args.timeout:
        if app.poll() is not None:
            print(f"[NG] exe が先に終わりました(終了コード {app.returncode})")
            ok = False
            break
        text = "".join(p.read_text(encoding="utf-8", errors="replace")
                       for p in sorted((work / "logs").rglob("DebugLog_*.txt")))
        match = page.search(text)
        if match and joined in text:
            seen = match.group(0)
            break
        time.sleep(0.5)
    if seen:
        print(f"[OK] 画面が Python から届きました({time.monotonic() - started:.1f}秒): {seen}")
        print(f"[OK] 画面の JS が動き、外枠の機能が見えています: {joined}")
    elif ok:
        print("[NG] 時間内に画面が届きませんでした。ログの末尾:")
        logs = sorted((work / "logs").rglob("DebugLog_*.txt"))
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
