#!/usr/bin/env python3
"""日報複合ツールのブラウザ版(予備)の起動開始点

    Start.vbs(通常)/ start.bat(診断)
        └─ start_app.py            ← ここ
             ├─ 実行環境の確認      (Python の版・Flask と waitress・書き込み権限)
             ├─ 同時起動の判定      (デスクトップ版が動いていれば止まる。`instance_guard`)
             ├─ 多重起動の判定      (ブラウザ版がもう動いていれば、そこへつなぐ)
             ├─ 待ち受け            (127.0.0.1:8700〜。waitress)
             └─ ブラウザを開く      (大きなタブの画面)

各ツールは、大きなタブを開いたときに、そのツールのブラウザ版を起こす(`portal/browser_tools.py`)。
**デスクトップ版(`日報複合ツール.exe`)が本来の入口。** ブラウザ版は exe が使えないときの予備。
ブラウザ版とデスクトップ版は同時に動かない(後から開いたほうが止まる)。

使い方:

    python start_app.py               ブラウザで開く
    python start_app.py --no-browser  ブラウザを開かない(検証用)
    python start_app.py --check       環境の確認だけして終わる(診断用)
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Optional

APP_ROOT = Path(__file__).resolve().parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

# **一式のフォルダに `__pycache__` を書かない**(共有サーバーに置くと全部の PC が同じ所へ書き、
# 更新の上書きとぶつかる)。置き場所(手元の領域の pycache)を決める import でも書かせない
_WROTE_BYTECODE = sys.dont_write_bytecode
sys.dont_write_bytecode = True
from portal import app_config as _app_config  # noqa: E402

_app_config.bytecode_to_local()
if sys.pycache_prefix:
    sys.dont_write_bytecode = _WROTE_BYTECODE

MIN_PYTHON = (3, 9)
REQUIRED_PACKAGES = (("flask", "Flask"), ("waitress", "waitress"))

#: 画面(大きなタブのページ)からの心拍が途絶えて、終わるまでの時間(秒)
IDLE_SEC = 90
#: 最初の心拍を待つ上限(秒)の既定。ブラウザが開かなかったときに残り続けない。
#: **大設定で分単位に変えられる**(`portal.user_settings.first_contact_min`。起動のときに読む)
FIRST_CONTACT_SEC = 300

#: 見張りの1回(1秒のはず)がこれより長く空いたら、そのあいだ PC ごと眠っていた
#: (スリープ・休止)とみなし、数え直す(秒)。各ツールの見張りと同じ考え
WAKE_GAP_SEC = 30

LOCK_NAME = "portal.lock"

#: 印の本人(起動時刻まで合う前回の入口)が動いているのに答えないとき、答えるのを待つ上限(秒)。
#: 過ぎたら本人を終わらせてから起動し直す(`portal/proc_identity.py`)
NO_ANSWER_WAIT_SEC = 5.0
#: 前回の入口を終わらせたあと、錠(名前付きミューテックス)が空くのを待つ上限(秒)
RECLAIM_WAIT_SEC = 5.0
#: 錠は握られているのに印が前回のもの(起動の途中)のとき、新しい印を待つ上限(秒)
STARTING_WAIT_SEC = 8.0

#: 残っていた印を片付けたときのログ(stop.bat・起動。各ツールも同じ言葉)
CLEANED = "片付けました。Start.vbs で起動できます"

DESKTOP_RUNNING = "日報複合ツールはデスクトップ版(日報複合ツール.exe の窓)で動いています"
DESKTOP_HINT = ("開いている窓をお使いください。ブラウザ版で開くときは、"
                "窓の「終了」で閉じてからにしてください。")


class StartupError(RuntimeError):
    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


def console_python() -> str:
    name = sys.executable.replace("\\", "/").rsplit("/", 1)[-1]
    if name.lower().startswith("pythonw"):
        return "python" + name[len("pythonw"):]
    return name


def run_environment_checks() -> None:
    if sys.version_info < MIN_PYTHON:
        raise StartupError(f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 以上が必要です(いまは {sys.version.split()[0]})",
                           "https://www.python.org/downloads/ から新しい Python を入れてください。")
    import importlib.util

    missing = [pip for module, pip in REQUIRED_PACKAGES if importlib.util.find_spec(module) is None]
    if missing:
        raise StartupError(f"必要なパッケージが入っていません: {', '.join(missing)}",
                           f"コマンドプロンプトで次を実行してください:\n"
                           f"    {console_python()} -m pip install -r requirements.txt")
    from portal import app_config

    try:
        root = app_config.ensure_local_dirs()
        probe = root / "runtime" / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise StartupError(f"作業用フォルダに書き込めません: {exc}",
                           "書き込みの権限があるか、ディスクの空きがあるか確認してください。") from None


# ------------------------------------------------------------------
# 印(ブラウザ版がどこで待ち受けているか。stop.bat と次の起動が読む)
# ------------------------------------------------------------------
def lock_path() -> Path:
    from portal import app_config

    return app_config.local_dir("runtime") / LOCK_NAME


def read_lock() -> Optional[dict]:
    """読めない(壊れている・書きかけ・掴まれている)ときも None。**片付けには使わない**
    (片付けは中身を読めた印だけ。`remove_lock_if`)。"""
    try:
        data = json.loads(lock_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def lock_owner(lock: Optional[dict]) -> str:
    """印の PID が、印を書いた入口のままか(`portal.proc_identity.owner`)。

    **PID は使い回される。** PC を終了・再起動したあとに残った印の PID を、まったく別の
    プログラムが持っていることがある。起動時刻まで合うときだけ本人(`SAME`)。
    """
    from portal import browser_tools, proc_identity

    if not lock:
        return proc_identity.GONE
    return proc_identity.owner(int(lock.get("pid") or 0), float(lock.get("process_started") or 0),
                               proc_identity.file_mtime(lock_path()),
                               lambda pid: browser_tools.is_alive(pid))


def remove_lock_if(lock: dict) -> bool:
    """読んだ印が**まだそのまま**なら消す(読んでから消すまでに新しい入口が書いた印を消さない)。"""
    now = read_lock()
    if not now or now.get("pid") != lock.get("pid") or now.get("token") != lock.get("token"):
        return False
    try:
        lock_path().unlink()
    except OSError:
        return False
    return True


def write_lock(port: int, token: str) -> None:
    from portal import app_config, proc_identity

    # 起動時刻(`process_started`)も書く。PID だけでは、再起動のあとに同じ番号を持った別の
    # プログラムを「動いている」と取り違える(`lock_owner`)
    data = {"app_id": app_config.app_id(), "pid": os.getpid(), "port": port,
            "url": f"http://127.0.0.1:{port}/", "token": token, "started_at": time.time(),
            "process_started": proc_identity.my_started(),
            "version": app_config.version(), "app_root": str(APP_ROOT), "python": sys.executable}
    path = lock_path()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def remove_lock() -> None:
    data = read_lock()
    if data and int(data.get("pid") or 0) == os.getpid():
        try:
            lock_path().unlink()
        except OSError:
            pass


def port_free(port: int, host: str) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _answers(lock: dict, wait_sec: float) -> bool:
    """入口が `/api/health` に自分(app_id)と答えるか。`wait_sec` 秒まで待つ。"""
    from portal import app_config
    from portal.browser_tools import _request

    deadline = time.monotonic() + wait_sec
    while True:
        try:
            status, body = _request(f"http://127.0.0.1:{int(lock.get('port') or 0)}/api/health", timeout=1.5)
        except (OSError, ValueError):
            status, body = 0, {}
        if status == 200 and isinstance(body, dict) and body.get("app_id") == app_config.app_id():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.3)


def _connect(log, lock: dict, *, open_browser: bool) -> None:
    log.info("ブラウザ版がもう動いています。つなぎます: %s", lock.get("url"))
    if open_browser:
        webbrowser.open(lock["url"])
    else:
        print(f"もう動いています: {lock['url']}")


def _join_or_take_over(log, *, open_browser: bool):
    """錠(名前付きミューテックス)は別のブラウザ版の入口が握っている。つなぐか、立て直す。

    戻り値: つないだ(=起動しない)なら None、立て直して錠を握れたらその `Claim`。

    - 印の本人(PID と起動時刻が合う)が答える → つなぐ
    - 本人が `NO_ANSWER_WAIT_SEC` 秒たっても答えない → **本人だけ**を終わらせて、錠を取り直して起動する
    - 印が前回のもの(PID が居ない・別のプログラムが同じ番号を持っている)→ 錠を握っているのは
      起動の途中の入口。新しい印が出るのを少し待ってつなぐ
    """
    from portal import instance_guard, proc_identity

    deadline = time.monotonic() + STARTING_WAIT_SEC
    said = False
    while True:
        lock = read_lock()
        state = lock_owner(lock)
        if lock and lock.get("url") and state == proc_identity.UNKNOWN:
            _connect(log, lock, open_browser=open_browser)    # 確かめられない環境は今までどおり
            return None
        if lock and lock.get("url") and state == proc_identity.SAME:
            if _answers(lock, NO_ANSWER_WAIT_SEC):
                _connect(log, lock, open_browser=open_browser)
                return None
            pid = int(lock.get("pid") or 0)
            log.warning("前回のブラウザ版の入口(pid=%s)が %.0f秒たっても応答しません。終わらせて起動し直します",
                        pid, NO_ANSWER_WAIT_SEC)
            if not proc_identity.terminate(pid, float(lock.get("process_started") or 0),
                                           proc_identity.file_mtime(lock_path())):
                raise StartupError("日報複合ツールのブラウザ版(前回の起動)が応答しません",
                                   "stop.bat --force で止めてから、もう一度開いてください。")
            remove_lock_if(lock)
            log.info("前回の入口を終わらせました。%s", CLEANED)
            until = time.monotonic() + RECLAIM_WAIT_SEC
            while True:
                claim = instance_guard.claim(instance_guard.BROWSER)
                if claim.ok:
                    return claim
                if time.monotonic() >= until:
                    break
                time.sleep(0.2)
            raise StartupError("日報複合ツールのブラウザ版がもう動いています",
                               "開いているブラウザのタブをお使いください。")
        if lock and state in (proc_identity.GONE, proc_identity.OTHER) and not said:
            said = True
            log.info("残っていた印は前回の起動のものです(pid=%s は%s)。起動の途中の入口を待ちます",
                     lock.get("pid"), "居ません" if state == proc_identity.GONE else "別のプログラムです")
        if time.monotonic() >= deadline:
            raise StartupError("日報複合ツールのブラウザ版がもう動いています",
                               "開いているブラウザのタブをお使いください。")
        time.sleep(0.3)


# ------------------------------------------------------------------
# 心拍の見張り(画面が居なくなったら終わる)
# ------------------------------------------------------------------
class IdleWatch:
    """画面(大きなタブのページ)の心拍が途絶えたかを数える。**眠っていたあいだは数えない。**

    PC がスリープすると、ブラウザも一緒に眠るので心拍は来ない。Windows の
    `time.monotonic()` は眠っているあいだも進むので、フタを開けた瞬間に「90秒 心拍が
    ありません」と判定し、ブラウザが心拍を送るより先に**入口が全ツールを止めていた**
    (各ツールは自分の見張りで眠りを差し引いていたのに、入口が先に止めてしまう)。
    誰も閉じていない。

    見張りの1回(1秒のはず)が `WAKE_GAP_SEC` より空いたら眠っていたとみなし、
    そこから数え直す(起きたブラウザが心拍を送る間を与える)。

    渡す時刻は `portal.awake_clock`(止まっていた分を差し引く時計。日報と同じ)。
    差し引いた時計では見張りの1回は空かない(起きた直後の心拍の無かった秒数は、
    眠る前の続き)。ここでの数え直しは、ただの monotonic を渡されたときの備え。
    """

    def __init__(self, started: float, *, idle_sec: float = IDLE_SEC,
                 first_contact_sec: float = FIRST_CONTACT_SEC, wake_gap_sec: float = WAKE_GAP_SEC) -> None:
        self.started = started
        self.idle_sec = idle_sec
        self.first_contact_sec = first_contact_sec
        self.wake_gap_sec = wake_gap_sec
        self.last_tick = started
        self.resumed = started          # 数え始め(起動・眠りから戻ったとき)
        self.slept = 0                  # 眠りから戻った回数(記録用)

    def tick(self, now: float, beat: float) -> Optional[float]:
        """見張りの1回。終わるべきなら、心拍の無かった秒数を返す。"""
        if now - self.last_tick > self.wake_gap_sec:
            self.resumed = now
            self.slept += 1
        self.last_tick = now
        contacted = beat > self.started
        since = max(beat if contacted else self.started, self.resumed)
        idle = now - since
        if (contacted and idle > self.idle_sec) or (not contacted and idle > self.first_contact_sec):
            return idle
        return None


# ------------------------------------------------------------------
# 起動
# ------------------------------------------------------------------
def start(*, open_browser: bool = True) -> int:
    from portal import app_config, catalog, instance_guard, web
    from portal.browser_tools import BrowserTools
    from portal.logging_utils import get_logger
    from portal.rights_store import store

    log = get_logger("start")
    log.info("=" * 50)
    log.info("ブラウザ版を起動します: pid=%s Python %s", os.getpid(), sys.version.split()[0])

    # --- デスクトップ版と同時に動かさない(後から開いたほうが止まる)---
    claim = instance_guard.claim(instance_guard.BROWSER)
    if not claim.ok:
        if claim.running == instance_guard.DESKTOP:
            log.warning("デスクトップ版が動いているので、ブラウザ版は起動しません")
            instance_guard.bring_desktop_to_front()
            raise StartupError(DESKTOP_RUNNING, DESKTOP_HINT)
        # ブラウザ版がもう動いている。そこへつなぐ(答えない前回の入口なら終わらせて起動し直す)
        claim = _join_or_take_over(log, open_browser=open_browser)
        if claim is None:
            return 0

    host = app_config.host()
    port = next((p for p in app_config.port_candidates() if port_free(p, host)), None)
    if port is None:
        raise StartupError(f"使えるポートがありません(試した番号: {app_config.port_candidates()})",
                           "config/app.json の port を変えるか、そのアプリを終了してください。")

    # 配布設定(置き場所・管理者パスワード)を、共有の DB を読みに行く前に
    from portal import distribution
    distribution.apply_on_start()

    token = secrets.token_urlsafe(24)
    app = web.create_app(token=token, bridge=False)
    tools = BrowserTools(catalog.load())
    web.set_browser_tools(tools)
    store.sync_in_background()

    from waitress.server import create_server

    server = create_server(app, host=host, port=port, threads=8, ident=app_config.app_id())
    stopped = threading.Event()

    def stop() -> None:
        if stopped.is_set():
            return
        stopped.set()
        log.info("停止します")
        try:
            dispatcher = getattr(server, "task_dispatcher", None)
            if dispatcher is not None:
                dispatcher.shutdown(timeout=3)
            server.close()
            from waitress import wasyncore

            table = getattr(server, "_map", None) or getattr(server, "map", None)
            if table:
                wasyncore.close_all(table, ignore_all=True)
        except Exception as exc:                  # noqa: BLE001 - 停止は best effort
            log.warning("停止でエラー: %s", exc)

    web.set_shutdown_hook(stop)
    write_lock(port, token)
    thread = threading.Thread(target=server.run, name="http", daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}/"
    log.info("待ち受け開始: %s", url)
    if open_browser:
        webbrowser.open(url)
    else:
        print(f"起動しました: {url}")

    # --- 画面が居なくなったら終わる(心拍)---
    # 時刻は**スリープを数えない時計**(日報と同じ `awake_clock`)。起きた瞬間に壁の時計が
    # まだ進んでいなくても、止まっていた分を差し引く
    from portal import awake_clock, user_settings
    clock = awake_clock.start()
    wait_min = user_settings.first_contact_min()
    log.info("最初の画面を最大 %d 分待ちます(大設定で変えられます)", wait_min)
    watch = IdleWatch(clock.now(), first_contact_sec=wait_min * 60)
    try:
        while thread.is_alive() and not stopped.is_set():
            time.sleep(1)
            slept = watch.slept
            now = clock.now()
            # 裏に回った画面があるあいだは、心拍が途切れても生きているものとして数える
            idle = watch.tick(now, now if web.in_background() else web.last_beat())
            if watch.slept != slept:
                log.info("見張りが止まっていました(スリープ・休止から戻った)。心拍は数え直します")
            if idle is not None:
                log.info("画面が居なくなったので終わります(%.0f秒 心拍がありません)", idle)
                tools.stop_all(force=False)
                stop()
        thread.join(timeout=6)
    finally:
        remove_lock()
        claim.release()
        log.info("終了しました")
    return 0


def report_failure(error: StartupError, *, open_browser: bool) -> None:
    """起動できない理由を見せる。pythonw では標準エラーが無いので、Windows の窓で出す。"""
    text = f"{error}\n\n{error.hint}".strip()
    try:
        from portal.logging_utils import get_logger
        get_logger("start").error("起動に失敗: %s / %s", error, error.hint)
    except Exception:                             # noqa: BLE001
        pass
    if sys.stderr is not None:
        print(text, file=sys.stderr)
    if os.name == "nt" and open_browser:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, "日報複合ツール", 0x40)
        except Exception:                         # noqa: BLE001
            pass


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="日報複合ツールのブラウザ版(予備)を起動する")
    parser.add_argument("--no-browser", action="store_true", help="ブラウザを開かない(検証用)")
    parser.add_argument("--check", action="store_true", help="実行環境の確認だけして終わる(診断用)")
    args = parser.parse_args(argv)
    open_browser = not args.no_browser
    try:
        run_environment_checks()
        if args.check:
            from portal import app_config
            print("実行環境の確認: 問題ありません\n")
            print(app_config.describe())
            return 0
        return start(open_browser=open_browser)
    except StartupError as exc:
        report_failure(exc, open_browser=open_browser)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
