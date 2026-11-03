#!/usr/bin/env python3
"""日報複合ツールを止める(`stop.bat`)・ランチャーの入口(`launcher_check.bat`・`launcher_stop.bat`)

    python process_manager.py            ブラウザ版(入口と各ツール)に終わってもらう
    python process_manager.py --force    処理の途中でも止める(デスクトップ版も。途中の処理は捨てる)
    python process_manager.py --status   いま何が動いているかを出す
    python process_manager.py --check    起動確認(launcher_check.bat)。0 使える / 2 準備中 / 1 動いていない
    python process_manager.py --launcher 終了の入口(launcher_stop.bat)。デスクトップ版も閉じる

**止める入口の決まり**(業務ツール統合ランチャー 1.8.9「更新のため全 PC で止める」。docs/ランチャー連携.md):

    終了コード  0 = 止めた・もともと動いていない
                3 = 処理中なので止めなかった(画面で確認中・「閉じない」が選ばれた・まだ終わりきらない も)
                1 = そのほか止められなかった(stop.bat とデスクトップ版・窓が見つからない など)
    標準出力の **1行目が理由**(「実行中の処理があります: 日報(標準作業時間の取り込み)」)。
    ランチャーの管理者の一覧と、利用者への知らせに出る。途中の経過は標準エラーへ出す。
    ランチャーの入口(`--launcher`)は標準出力に1行だけ出す(1.7 は最後の1行を理由として見せる)

`stop.bat` は**デスクトップ版(日報複合ツール.exe の窓)を閉じない。** 窓の × か「終了」で閉じる
(閉じるときに全ツールの「終わってよいか」を訊くため)。動いていれば窓を前に出す。
`--force` のときだけ、窓に「閉じて」と頼み、閉じなければ exe(と子の Python)を止める。

業務ツール統合ランチャーの終了の入口(`--launcher`)は、デスクトップ版なら**窓に「閉じて」と
頼む**(× と同じ流れ。プロセスは落とさない)。ブラウザ版なら `stop.bat` と同じ。

**止めたと言うのは、プロセスが居なくなってから。** exe・dll・データベースを掴んだまま
「止めた」と言うと、ランチャーがツールのフォルダーを上書きするときに失敗する。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

APP_ROOT = Path(__file__).resolve().parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
# 短い処理なので Python の .py の写しを書かない(`launcher_check.bat` は何度も呼ばれる。一式を共有に
# 置くと、全部の PC が一式のフォルダの `__pycache__` へ書きに行き、更新の上書きとぶつかる)
sys.dont_write_bytecode = True

# 終了コード(止める入口の決まり)
EXIT_STOPPED, EXIT_FAILED, EXIT_BUSY = 0, 1, 3

# 画面に打ちかけを置いてもらうのを待つ上限(秒)。ランチャーは stop.bat を 20 秒で見切る
ASKING_WAIT_SEC = 15.0

# ブラウザ版の起動の途中(錠はあるが待ち受けの印がまだ)なら、印が出るのを待つ上限(秒)
STARTING_WAIT_SEC = 8.0

# ブラウザ版の入口に終わってもらってから、PID が居なくなるのを待つ上限(秒)。--force は短く
PORTAL_EXIT_WAIT_SEC = 15.0
PORTAL_FORCE_WAIT_SEC = 6.0

# 各ツールのブラウザ版に終わってもらってから、PID が居なくなるのを待つ上限(秒)
TOOL_EXIT_WAIT_SEC = 15.0
TOOL_FORCE_WAIT_SEC = 6.0

# 終了の入口: デスクトップ版の窓が閉じるのを待つ上限(秒)。ランチャーは入口を 20 秒で見切る
DESKTOP_CLOSE_WAIT_SEC = 16.0
# --force: 窓に「閉じて」と頼んでから、exe を止めるまで待つ(秒)。途中の処理が無ければ
# この間に打ちかけを置いて自分で閉じる。確認が出ていれば待たずに止めることになる
DESKTOP_FORCE_GRACE_SEC = 6.0
DESKTOP_KILL_WAIT_SEC = 8.0

DESKTOP_MESSAGE = ("デスクトップ版(日報複合ツール.exe の窓)が動いています。"
                   "窓の × か「終了」で閉じてください(stop.bat では閉じません)")
FORCE_HINT = "中断して止めるときは stop.bat --force(途中の処理は捨てます)"


@dataclass
class Outcome:
    """止めた結果。`line` は標準出力の1行目(理由・結果)。"""

    code: int
    line: str
    details: list[str] = field(default_factory=list)
    hint: str = ""          # stop.bat の案内(--force)。ランチャーの入口では出さない


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


def _progress(text: str) -> None:
    """途中の経過(標準エラー)。標準出力の1行目は理由のために空けておく。"""
    if text:
        print(text, file=sys.stderr, flush=True)


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


def emit(outcome: Outcome, *, launcher: bool = False) -> int:
    """結果を出す。**標準出力の1行目が理由。** ランチャーの入口は標準出力に1行だけ。"""
    print(one_line(outcome.line), flush=True)
    rest = list(outcome.details) + ([outcome.hint] if outcome.hint and not launcher else [])
    for line in rest:
        print(line, file=sys.stderr if launcher else sys.stdout)
    sys.stdout.flush()
    return outcome.code


def stop(*, force: bool = False, hints: bool = True) -> int:
    """`stop.bat`。結果を出して終了コードを返す(0 止めた / 3 処理中 / 1 そのほか)。

    `hints` は stop.bat の案内(「stop.bat --force」)。
    """
    outcome = run_stop(force=force)
    if not hints:
        outcome.hint = ""
    return emit(outcome)


def run_stop(*, force: bool = False) -> Outcome:
    """止める(出さずに結果を返す)。デスクトップ版は `--force` のときだけ止める。"""
    from portal import instance_guard

    if instance_guard.running() == instance_guard.DESKTOP:
        if force:
            return force_stop_desktop()
        instance_guard.bring_desktop_to_front()
        return Outcome(EXIT_FAILED, DESKTOP_MESSAGE,
                       hint="窓を閉じずに止めるときは stop.bat --force(途中の処理は捨てます)")
    return _stop_browser(force=force)


def _portal_busy_line(body: dict) -> str:
    """入口の「処理の途中」の答え(409 busy)から1行目を作る(どのツールか分かるように)。"""
    from portal.browser_tools import busy_line

    if body.get("line"):
        return str(body["line"])
    if isinstance(body.get("tools"), list) and body["tools"]:
        return busy_line(body["tools"])
    items = []
    for text in body.get("running") or []:
        title, sep, rest = str(text).partition(": ")
        items.append({"title": title, "reason": rest} if sep else {"title": "", "reason": str(text)})
    return busy_line(items) if items else "実行中の処理があります"


def _stop_browser(*, force: bool) -> Outcome:
    from portal import catalog, instance_guard
    from portal.browser_tools import BrowserTools, _request, busy_line, is_alive, terminate_tree, wait_gone

    import start_app

    details: list[str] = []
    did = False                       # 止めたものがある
    killed = False                    # PID で止めたものがある(点検表の Excel を片付ける)
    lock = start_app.read_lock()
    pid = int((lock or {}).get("pid") or 0)
    if not (lock and lock.get("port") and is_alive(pid)) and instance_guard.running() == instance_guard.BROWSER:
        # 起動の途中(錠は握ったが、まだ待ち受けの印 portal.lock を書いていない。配布設定・共有の
        # DB を読んでいる)。「動いていない」と言うと、ランチャーが起動中のフォルダーを上書きする
        deadline = time.monotonic() + STARTING_WAIT_SEC
        while time.monotonic() < deadline:
            time.sleep(0.3)
            lock = start_app.read_lock()
            pid = int((lock or {}).get("pid") or 0)
            if (lock and lock.get("port") and is_alive(pid)) or instance_guard.running() != instance_guard.BROWSER:
                break
        if not (lock and lock.get("port") and is_alive(pid)) and instance_guard.running() == instance_guard.BROWSER:
            return Outcome(EXIT_BUSY, "日報複合ツール(ブラウザ版)を起動しています(起動が済んでから止め直してください)")
    if lock and lock.get("port") and is_alive(pid):
        did = True
        base = f"http://127.0.0.1:{lock['port']}"
        try:
            status, body = _request(f"{base}/api/shutdown",
                                    data=json.dumps({"force": force}).encode("utf-8"),
                                    headers={"Content-Type": "application/json",
                                             "X-Tool-Token": str(lock.get("token") or "")}, timeout=10)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            _progress(f"入口に届きませんでした: {exc}")
            status, body = 0, {}
        body = body if isinstance(body, dict) else {}
        if status == 409 and body.get("reason") == "asking":
            # 開いている画面に、打ちかけを置いてから閉じるよう頼んだ。置けたら入口が自分で
            # 終わる。少しだけ待つ(ランチャーは stop.bat を 20 秒で見切る)
            _progress(str(body.get("message") or ""))
            deadline = time.monotonic() + ASKING_WAIT_SEC
            while time.monotonic() < deadline and is_alive(pid):
                time.sleep(0.3)
            if is_alive(pid):
                return Outcome(EXIT_BUSY, "日報複合ツールの画面で、閉じる前の確認をしています"
                                          "(画面を見て答えてください。済めば自分で終わります)",
                               details=[str(body.get("message") or "")], hint=FORCE_HINT)
        elif status == 409 and body.get("reason") == "refused":
            return Outcome(EXIT_BUSY, str(body.get("message") or "画面で「閉じない」が選ばれました"))
        elif status == 409:
            return Outcome(EXIT_BUSY, _portal_busy_line(body),
                           details=["止めませんでした:", str(body.get("message") or "")], hint=FORCE_HINT)
        elif status != 200 and not force:
            # 入口が答えない(固まっている・組み立ての途中)。黙って待ってから「止めた」と言わない
            if not _wait_pid(pid, 3.0):
                return Outcome(EXIT_FAILED, "日報複合ツールの入口が停止の頼みに答えません"
                                            f"(pid={pid})", hint=FORCE_HINT)
        if not _wait_pid(pid, PORTAL_FORCE_WAIT_SEC if force else PORTAL_EXIT_WAIT_SEC):
            if not force:
                return Outcome(EXIT_BUSY, "日報複合ツールの入口がまだ終わっていません"
                                          "(終わるのを待っています)", hint=FORCE_HINT)
            _progress(f"日報複合ツールの入口が終わらないので止めます(pid={pid})")
            terminate_tree(pid)
            killed = True
            if is_alive(pid):
                return Outcome(EXIT_FAILED, f"日報複合ツールの入口を止められませんでした(pid={pid})")
        details.append("ブラウザ版の入口に終わってもらいました")
    # 入口が居なくても、各ツールのブラウザ版が残っていれば止める
    tools = BrowserTools(catalog.load())
    left = tools.running()
    if left:
        did = True
        if not force:
            busy = tools.busy_tools() if hasattr(tools, "busy_tools") else []
            if busy:
                return Outcome(EXIT_BUSY, busy_line(busy),
                               details=["処理の途中のツールがあります:"]
                               + [f"{b['title']}: {b['message']}" for b in busy], hint=FORCE_HINT)
        tools.stop_all(force=force)
        # **終わったことを確かめてから「終わった」と言う。** 以前は頼んだだけで言っていて、
        # 本体の組み立てに失敗したツール(待機画面が止め口に答えなかった)の Python が残り、
        # フォルダを消せなかった(タスクマネージャーで Python を切るしかなかった)
        stuck = wait_gone(left, TOOL_FORCE_WAIT_SEC if force else TOOL_EXIT_WAIT_SEC)
        cut = list(stuck) if force else []
        for r in cut:
            _progress(f"{r.tool.title} が終わらないので止めます(pid={r.pid})")
            details.append(f"{r.tool.title} が終わらないので止めました(pid={r.pid})")
            terminate_tree(r.pid)
            killed = True
        if cut:
            stuck = wait_gone(stuck, 5)
        done = [r for r in left if r not in stuck and r not in cut]
        if done:
            details.append("各ツールのブラウザ版に終わってもらいました: " + "、".join(r.tool.title for r in done))
        if stuck:
            names = "、".join(f"{r.tool.title}(pid={r.pid})" for r in stuck)
            if force:
                return Outcome(EXIT_FAILED, "止められなかったツールがあります: " + names, details=details)
            return Outcome(EXIT_BUSY, "終わらなかったツールがあります: " + names
                           + "(終わるのを待っています)", details=details, hint=FORCE_HINT)
    if killed:
        cleanup_inspection_excel(details)
    if not did:
        return Outcome(EXIT_STOPPED, "日報複合ツールは動いていません")
    return Outcome(EXIT_STOPPED, "日報複合ツール(ブラウザ版)を止めました"
                   + ("(途中の処理は捨てました)" if force else ""), details=details)


def _wait_pid(pid: int, timeout: float) -> bool:
    """PID が居なくなるのを待つ。居なくなれば True。"""
    from portal.browser_tools import is_alive

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and is_alive(pid):
        time.sleep(0.3)
    return not is_alive(pid)


def _wait_desktop_gone(timeout: float) -> bool:
    from portal import instance_guard

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and instance_guard.running() == instance_guard.DESKTOP:
        time.sleep(0.3)
    return instance_guard.running() != instance_guard.DESKTOP


def force_stop_desktop() -> Outcome:
    """`--force`: デスクトップ版(外枠 = 日報複合ツール.exe と、子の入口・4ツールの Python)を止める。

    1. 窓に「閉じて」と頼む(× と同じ。途中の処理が無ければ打ちかけを置いて自分で閉じる)
    2. 閉じなければ(確認が出ている・固まっている)、exe を子ごと止める(`taskkill /T /F`)。
       途中の処理は捨てる。外枠には外から「確認なしで閉じて」と頼む道が無い(ポートを使わない)
    錠(名前付きミューテックス)は exe が終われば OS が外す。それを見て「止めた」と言う。
    """
    from portal import desktop_window, instance_guard
    from portal.browser_tools import terminate_tree

    details: list[str] = []
    windows = _desktop_windows()
    if windows:
        desktop_window.ask_to_close(windows)
        _progress("日報複合ツールの窓に「閉じて」と頼みました(閉じなければ止めます)")
        if _wait_desktop_gone(DESKTOP_FORCE_GRACE_SEC):
            return Outcome(EXIT_STOPPED, "日報複合ツールの窓を閉じました")
    pids = desktop_window.exe_pids(instance_guard.DESKTOP_EXES)
    if pids is None:
        return Outcome(EXIT_FAILED, "デスクトップ版を止められません(Windows 以外)")
    if not pids and instance_guard.running() == instance_guard.DESKTOP:
        return Outcome(EXIT_FAILED, "デスクトップ版の exe が見つかりません(錠だけが残っています)")
    for pid in pids:
        _progress(f"日報複合ツールの exe を止めます(pid={pid})")
        terminate_tree(pid)
        details.append(f"日報複合ツールの exe を止めました(pid={pid})")
    if not _wait_desktop_gone(DESKTOP_KILL_WAIT_SEC):
        return Outcome(EXIT_FAILED, "デスクトップ版を止められませんでした"
                       f"(pid={'、'.join(str(p) for p in pids) or '?'})", details=details)
    cleanup_inspection_excel(details)
    return Outcome(EXIT_STOPPED, "日報複合ツールを強制終了しました(途中の処理は捨てました)", details=details)


def cleanup_inspection_excel(details: list[str]) -> None:
    """点検表が起こした Excel(印刷の途中で止めたときに残る)を片付ける。

    ふだんは点検表が自分の終わりに片付ける。PID で止めたときは片付けが走らないので、
    点検表の `process_manager.py --cleanup-excel`(記録した PID だけ。利用者の Excel は触らない)
    に頼む。残すと点検表の帳票を掴んだままになる。
    """
    script = APP_ROOT / "tools" / "inspection" / "process_manager.py"
    if not script.is_file():
        return
    try:
        done = subprocess.run([sys.executable, str(script), "--cleanup-excel"], cwd=str(script.parent),
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              env=dict(os.environ, PYTHONIOENCODING="utf-8"), timeout=20,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        details.append(f"点検表の Excel を片付けられませんでした: {exc}")
        return
    said = (done.stdout or "").strip()
    if said:
        details.append(said.splitlines()[-1])


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
    """終了の入口(`launcher_stop.bat`)。0 止めた / 3 処理中・確認中 / 1 そのほか。標準出力は理由の1行だけ。"""
    return emit(run_launcher_stop(force=force), launcher=True)


def run_launcher_stop(*, force: bool = False) -> Outcome:
    """デスクトップ版は**窓に「閉じて」と頼む**(× と同じ: 打ちかけを置いてもらい、全ツールに
    訊き、途中の処理があれば1つの確認)。確認に答えるまでは閉じない ── 待ちきれなければ
    「確認しています」と返す(3)。`--force` は窓に頼んで、閉じなければ exe を止める
    (ランチャーの「強制終了」・「更新のため全 PC で止める」の強制終了)。
    ブラウザ版は `stop.bat` と同じ。
    """
    from portal import instance_guard

    if instance_guard.running() == instance_guard.DESKTOP:
        if force:
            return force_stop_desktop()
        windows = _desktop_windows()
        if windows is None:
            return Outcome(EXIT_FAILED, "デスクトップ版の窓を確かめられません(Windows 以外)")
        if not windows:
            return Outcome(EXIT_FAILED, "デスクトップ版の窓が見つかりません(窓の「終了」で閉じてください)")
        from portal import desktop_window

        desktop_window.ask_to_close(windows)
        if _wait_desktop_gone(DESKTOP_CLOSE_WAIT_SEC):
            return Outcome(EXIT_STOPPED, "日報複合ツールの窓を閉じました")
        return Outcome(EXIT_BUSY, "日報複合ツールの窓で、閉じてよいかの確認をしています(窓を見て答えてください)")
    return run_stop(force=force)


#: 終了の入口に渡されても読み飛ばす引数(pause を飛ばす印は bat が見る)
NOPAUSE_ARGS = ("/nopause", "--nopause", "-nopause")


def main(argv: Optional[list[str]] = None) -> int:
    console_stdout()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="処理の途中でも止める")
    parser.add_argument("--status", action="store_true", help="いま何が動いているかを出す")
    parser.add_argument("--check", action="store_true", help="起動確認(launcher_check.bat)")
    parser.add_argument("--launcher", action="store_true", help="終了の入口(launcher_stop.bat)")
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args([a for a in argv if a.lower() not in NOPAUSE_ARGS])
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
