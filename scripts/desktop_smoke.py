#!/usr/bin/env python3
"""デスクトップ版(統合ツール.exe)を本当に起動して確かめる(GitHub Actions の Windows でも流す)

確かめること:
    1. 大きなタブの画面が入口の Python から届き、画面の JS が動く
       ── 入口のログに「大きなタブの画面がつながりました(デスクトップ版)」が出る
    2. **4つのツールの画面がそれぞれの Python から届く**(大きなタブの枠の中)
       ── 「<タブ> の画面が出ました: <ツールの画面の題名>」が4つ出る。ツールの待機画面
          (「…を起動しています」)ではなく、本体の画面の題名が出るまで待つ
          (ツールの画面に差し込んだ台本が、大きなタブの画面へ知らせた証拠。
           外枠(Rust)→ ツールの Python → 外枠 → 枠 → 大きなタブの画面 → 入口の Python が1周した)
    3. **どのプロセスもポートで待ち受けていない**(exe・5つの Python・WebView の子プロセス)
    4. ブラウザ版は起動しない(後から開いたほうが止まる): 統合ツールのブラウザ版と、
       各ツールのブラウザ版(tools/<ツール>/start_app.py)
    5. exe を止めると Python も終わる(取り残さない)

使い方:
    python scripts/desktop_smoke.py --exe src-tauri/target/release/AllTools.exe
    (Linux では DISPLAY が要る。例: xvfb-run -a python scripts/desktop_smoke.py --exe ...)

作業用のフォルダ(手元の領域・共有の DB の代わり・各ツールの設定)は一時フォルダに作る。
本番の領域は触らない。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WINDOWS = os.name == "nt"
TABS = {"nippou": "日報", "kanban": "看板", "calendar": "カレンダー", "inspection": "点検表"}


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


def command_line(pid: int) -> str:
    if WINDOWS:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                             capture_output=True, text=True).stdout
        return out.strip()
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return ""


def alive(pid: int) -> bool:
    if WINDOWS:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    return os.path.exists(f"/proc/{pid}")


def make_work(work: Path) -> dict[str, str]:
    """作業用のフォルダと、各ツールへ渡す環境変数。本番の共有フォルダ・手元の領域は見ない。"""
    share = work / "share"
    share.mkdir(parents=True)
    # 看板の共有の代わり(看板マスタに L1 の看板を 2 枚)
    cols = ("[管理番号] NUMERIC, [資材] TEXT, [サイズ] TEXT, [欲] TEXT, [不] TEXT, [更新日] DATETIME,"
            " [発送] TEXT, [倉庫確認日時] DATETIME, [常設品] TEXT, [保留] TEXT, [注文中日時] DATETIME")
    db = sqlite3.connect(str(share / "看板マスタ.sqlite3"))
    db.execute(f"CREATE TABLE [看板_L1] ({cols})")
    db.executemany("INSERT INTO [看板_L1] VALUES (?, ?, ?, '', '〇', '', '', '', '〇', '', '')",
                   [(1, "外装紙", "2200"), (2, "テープ", "50")])
    db.execute("CREATE TABLE [Form状態管理] ([ライン名] TEXT, [状態] TEXT, [更新日時] DATETIME, [ホスト名] TEXT)")
    db.commit()
    db.close()
    kanban_settings = work / "kanban" / "settings"
    kanban_settings.mkdir(parents=True)
    (kanban_settings / "config.json").write_text(json.dumps({
        "shared_db_path": str(share / "看板マスタ.sqlite3"),
        "sqlite_path": str(work / "kanban" / "local" / "kanban.sqlite3"),
        "log_dir": str(work / "kanban" / "logs"), "line": "L1",
    }), encoding="utf-8")
    env = {
        "ALLTOOLS_ROOT": str(ROOT),
        "ALLTOOLS_LOCAL_DIR": str(work / "portal"),
        # タブ表示権限の共有の DB は空(表が無い)→ 全部のタブが出る
        "ALLTOOLS_SHARED_DB_DIR": str(share),
        "ALLTOOLS_LOGIN_ID": "smoke", "ALLTOOLS_PC_NAME": "SMOKE-PC",
        "NIPPOU_LOCAL_DIR": str(work / "nippou" / "local"),
        "NIPPOU_APP_DIR": str(work / "nippou" / "app"),
        "NIPPOU_DIST_DIR": str(work / "nippou" / "配布設定"),
        "KANBAN_LOCAL_DIR": str(work / "kanban" / "local"),
        "KANBAN_SETTINGS_DIR": str(kanban_settings),
        "KANBAN_CONFIG": str(kanban_settings / "config.json"),
        "KANBAN_DISTRIBUTION_DIR": str(work / "kanban" / "配布設定"),
        "KANBAN_LOGIN_ID": "smoke", "KANBAN_PC_NAME": "SMOKE-PC",
        "CALENDAR_HOME": str(work / "calendar" / "home"),
        "CALENDAR_LOCAL_DIR": str(work / "calendar" / "local"),
        "CALENDAR_PC_NAME": "SMOKE-PC",
        "INSPECTION_LOCAL_DIR": str(work / "inspection" / "local"),
        "INSPECTION_DISTRIBUTION_DIR": str(work / "inspection" / "配布設定"),
        "PYTHONIOENCODING": "utf-8",
    }
    if os.environ.get("SMOKE_PYTHON"):
        env["ALLTOOLS_PYTHON"] = os.environ["SMOKE_PYTHON"]
    return env


def portal_log(work: Path) -> str:
    return "".join(p.read_text(encoding="utf-8", errors="replace")
                   for p in sorted((work / "portal" / "logs").glob("portal_*.log")))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", required=True)
    parser.add_argument("--timeout", type=float, default=240)
    args = parser.parse_args()

    work = Path(tempfile.mkdtemp(prefix="alltools_smoke_"))
    env = dict(os.environ, **make_work(work))
    ok = True

    def result(good: bool, text: str) -> None:
        nonlocal ok
        print(("[OK] " if good else "[NG] ") + text, flush=True)
        ok = ok and good

    # ---- 1・2: 起動して、大きなタブと4つのツールの画面が出るのを待つ ----
    started = time.monotonic()
    # --demo: 点検表は模擬の点検表で動く(共有フォルダの Excel を見に行かない)
    app = subprocess.Popen([str(Path(args.exe).resolve()), "--demo"], env=env, cwd=str(ROOT))
    print(f"起動しました: pid={app.pid} 作業={work}", flush=True)
    joined = re.compile(r"大きなタブの画面がつながりました\(デスクトップ版\): (\S+)")
    shown = {tool: re.compile(rf"画面: {title} の画面が出ました: (.*?)\(") for tool, title in TABS.items()}
    seen: dict[str, str] = {}
    text = ""
    while time.monotonic() - started < args.timeout:
        if app.poll() is not None:
            result(False, f"exe が先に終わりました(終了コード {app.returncode})")
            break
        text = portal_log(work)
        match = joined.search(text)
        if match:
            seen.setdefault("shell", match.group(1))
        for tool, pattern in shown.items():
            for found in pattern.finditer(text):
                title = found.group(1).strip()
                if "起動しています" not in title:      # ツールの待機画面ではなく本体
                    seen.setdefault(tool, title or "(題名なし)")
        if len(seen) == len(TABS) + 1:
            break
        time.sleep(0.5)
    took = time.monotonic() - started
    result("shell" in seen, f"大きなタブの画面がつながりました({took:.1f}秒): {seen.get('shell', '')}")
    for tool, title in TABS.items():
        result(tool in seen, f"{title} の画面が出ました: {seen.get(tool, '(出ていません)')}")
    if len(seen) < len(TABS) + 1:
        print("--- 入口のログの末尾")
        print("\n".join(text.splitlines()[-30:]))
        shell_log = work / "portal" / "logs" / "desktop_shell.log"
        if shell_log.is_file():
            print("--- 外枠の記録")
            print(shell_log.read_text(encoding="utf-8", errors="replace")[-3000:])

    if app.poll() is None:
        # ---- 3: 待ち受けが無い ----
        tree = {app.pid, *children(app.pid)}
        # py ランチャ経由だと py.exe と python.exe の2つずつ見える。少なくとも5つ
        pythons = [pid for pid in tree if "bridge.py" in command_line(pid)]
        result(len(pythons) >= len(TABS) + 1,
               f"入口と4つのツールの Python が動いています({len(pythons)} 個)")
        ports = listening_ports(tree)
        result(not ports, f"待ち受けはありません(調べたプロセス {len(tree)} 個)" if not ports
               else f"待ち受けているプロセスがあります: {ports}")

        # ---- 4: ブラウザ版は起動しない(後から開いたほうが止まる) ----
        done = subprocess.run([sys.executable, str(ROOT / "start_app.py"), "--no-browser"],
                              env=env, cwd=str(ROOT), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120)
        result(done.returncode != 0 and "デスクトップ版" in done.stdout + done.stderr,
               "統合ツールのブラウザ版は起動しません(デスクトップ版が動いている)")
        for tool, title in TABS.items():
            tool_dir = ROOT / "tools" / tool
            done = subprocess.run([sys.executable, str(tool_dir / "start_app.py"), "--no-browser"],
                                  env=env, cwd=str(tool_dir), capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=120)
            said = done.stdout + done.stderr
            result("統合ツールの窓" in said, f"{title}のブラウザ版は起動しません"
                   + ("" if "統合ツールの窓" in said else f": {said[-300:]}"))

        # ---- 5: exe を止めると Python も終わる ----
        others = [pid for pid in tree if pid != app.pid]
        app.terminate()
        app.wait(timeout=30)
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline and any(alive(p) for p in others):
            time.sleep(0.5)
        left = [p for p in others if alive(p)]
        result(not left, "exe を止めたら子プロセス(Python など)も終わりました" if not left
               else f"exe を止めても残ったプロセスがあります: {left}")
    print("結果:", "OK" if ok else "NG")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
