"""対象プロセスだけを安全に停止する (基盤仕様書 2.8)

``stop.bat`` から呼ばれる。**Python をプロセス名だけで一括終了しない。**
同じ PC で別の Python アプリが動いていることがあり、巻き添えにすると
そちらの作業が消える。

止め方は3段階。上から順に試し、通ったらそこで終わる::

    1. アプリ自身へ正常終了を頼む   POST /api/shutdown
    2. 応答が無ければ記録した PID を使う(ただし中身を確かめてから)
    3. それでも残るなら、強制終了

2 の「中身を確かめてから」が要点。ロックに書いてある PID は、時間が経てば
**別のプロセスに使い回されている**ことがある。コマンドラインにこのアプリの
場所が含まれているかを見てから落とす。
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import launch_guard  # noqa: E402
from kanban import app_config, config  # noqa: E402
from kanban.applog import get_logger  # noqa: E402

log = get_logger("process_manager")

#: 正常終了を頼んでから諦めるまで(秒)
GRACEFUL_WAIT_SEC = 12.0
#: 強制終了のあと、消えたことを確かめるまで(秒)
KILL_WAIT_SEC = 5.0


class StopResult:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.was_running = False
        self.stopped = False
        self.how = ""
        self.message = ""

    def __str__(self) -> str:
        label = config.mode_display_name(self.mode)
        if not self.was_running:
            return f"{label}: 動いていません"
        if self.stopped:
            return f"{label}: 停止しました({self.how})"
        return f"{label}: 停止できませんでした — {self.message}"


def stop_mode(mode: str, *, force: bool = False) -> StopResult:
    """1 つのモードを止める。"""
    result = StopResult(mode)
    info = launch_guard.read_lock(mode)
    if info is None:
        return result

    if not launch_guard.is_process_alive(info.pid):
        log.info("ロックは残っていますが pid=%s は不在です", info.pid)
        launch_guard.remove_lock(mode)
        return result

    health = launch_guard.probe_health(info.port)
    if not launch_guard.is_our_app(health, mode):
        # PID の使い回し。**落としてはいけない**
        log.warning("pid=%s は別のプロセスです。触りません", info.pid)
        launch_guard.remove_lock(mode)
        result.message = "ロックのプロセスは別物でした(触っていません)"
        return result

    result.was_running = True

    # --- 1. 正常終了を頼む -------------------------------------------
    #
    # **殺す前に、必ずアプリ自身へ頼む。** 終了処理では最後に 1 回
    # Access へ書き戻す(``start_app._shutdown``)。プロセスを殺すとそれが
    # 走らず、押した操作が手元に取り残される。
    log.info("正常終了を要求します: mode=%s port=%s", mode, info.port)
    if launch_guard.request_shutdown(info.port, info.token):
        if _wait_gone(info.pid, GRACEFUL_WAIT_SEC):
            launch_guard.remove_lock(mode)
            result.stopped = True
            result.how = "正常終了"
            return result
        log.warning("停止要求は通りましたが、プロセスが残っています")
    elif not force:
        # 処理中で断られた(409)。**勝手に落とさない**
        busy = (health or {}).get("busy", "")
        result.message = busy or "処理中のため停止しませんでした"
        log.info("停止しません: %s", result.message)
        return result
    else:
        # 処理中だが、それでも止めてよいと言われている。
        # **もう一度アプリ自身へ頼む** ── 押し切るのと殺すのは違う。
        # ここで閉じられれば、最後の書き戻しは走る
        log.info("処理中ですが、押し切って正常終了を要求します")
        if launch_guard.request_shutdown(info.port, info.token, force=True):
            if _wait_gone(info.pid, GRACEFUL_WAIT_SEC):
                launch_guard.remove_lock(mode)
                result.stopped = True
                result.how = "正常終了(押し切り)"
                return result
            log.warning("押し切りの停止要求も通りましたが、プロセスが残っています")

    # --- 2. PID で止める(中身を確かめてから) --------------------------
    cmdline = launch_guard.process_command_line(info.pid)
    if cmdline and not _looks_like_ours(cmdline, info):
        log.warning("pid=%s の中身がこのアプリと一致しません。触りません: %s",
                    info.pid, cmdline)
        result.message = "プロセスの中身が一致しないため停止しませんでした"
        return result
    if not cmdline:
        log.warning("pid=%s のコマンドラインを取得できませんでした", info.pid)
        if not force:
            result.message = (
                "プロセスの中身を確認できませんでした。"
                "--force を付けると確認せずに停止します"
            )
            return result

    log.info("PID で停止します: pid=%s", info.pid)
    if _terminate(info.pid) and _wait_gone(info.pid, KILL_WAIT_SEC):
        launch_guard.remove_lock(mode)
        result.stopped = True
        result.how = "PID で停止"
        return result

    # --- 3. 強制終了 ---------------------------------------------------
    log.warning("強制終了します: pid=%s", info.pid)
    if _kill(info.pid) and _wait_gone(info.pid, KILL_WAIT_SEC):
        launch_guard.remove_lock(mode)
        result.stopped = True
        result.how = "強制終了"
        return result

    result.message = f"pid={info.pid} を停止できませんでした"
    return result


def _looks_like_ours(cmdline: str, info: launch_guard.LockInfo) -> bool:
    """そのコマンドラインは、このアプリのものか。

    アプリの置き場所か、起動スクリプトの名前が含まれていれば自分とみなす。
    """
    text = cmdline.replace("\\", "/").lower()
    root = (info.app_root or str(app_config.APP_ROOT)).replace("\\", "/").lower()
    if root and root in text:
        return True
    return "start_app.py" in text


def _wait_gone(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not launch_guard.is_process_alive(pid):
            return True
        time.sleep(0.2)
    return not launch_guard.is_process_alive(pid)


def _terminate(pid: int) -> bool:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid)],
                           capture_output=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            os.kill(pid, signal.SIGTERM)
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("停止に失敗しました: %s", exc)
        return False


def _kill(pid: int) -> bool:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            os.kill(pid, signal.SIGKILL)
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("強制終了に失敗しました: %s", exc)
        return False


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="process_manager",
        description="資材発注看板システムのバックエンドを停止する",
    )
    p.add_argument("--mode", choices=config.ALL_MODES,
                   help="止めるモード。省略すると全部")
    p.add_argument("--all", action="store_true",
                   help="全モードを止める(省略時と同じ。stop.bat から明示的に渡す)")
    p.add_argument("--force", action="store_true",
                   help="処理中でも止める(未反映の書き戻しが残る場合があります)")
    args = p.parse_args(argv)

    modes = [args.mode] if args.mode else list(config.ALL_MODES)
    results = [stop_mode(m, force=args.force) for m in modes]

    for r in results:
        print(r)

    if not any(r.was_running for r in results):
        print("\n動いているものはありませんでした。")
        return 0
    failed = [r for r in results if r.was_running and not r.stopped]
    if failed:
        print("\n停止できなかったものがあります。", file=sys.stderr)
        print("処理中で断られた場合は、少し待つか --force を付けてください。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
