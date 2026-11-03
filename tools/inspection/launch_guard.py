"""多重起動の防止とインスタンス管理 (基盤仕様書 2.4)

python-web-tools の `launch_guard.py` と同じ仕組み(この道具はモードが1つ)。

同じアプリが二重に起動すると、ポート競合・二重印刷・設定ファイル競合が
起きる。そこで起動ごとに**ロックファイル**(PID・ポート・起動時刻・トークン)を
書き、次の起動はそれを見て判断する。

    生きている同じアプリが居る → 新しく起動せず、ブラウザだけ開く
    死んだロックが残っている   → 消して続行する(記録は残す)

「生きているか」の判定は2段構えにする:

    1. PIDのプロセスが存在するか
    2. そのポートの `/api/health` が**同じ app_id** を返すか

1だけでは足りない。PIDは使い回されるので、無関係なプロセスが同じ番号を
持っていることがある。2だけでも足りない。別のアプリがHTTPを返している
場合があるので、`app_id` の照合が要る(基盤仕様書 2.3)。

このモジュールは Flask に依存しない。起動の判断だけを持つので、
サーバが立たない状況でも動く。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from core import app_config
from core.logging_utils import get_logger

#: Windows のコマンド(tasklist・wmic)の出力の文字コード。**コンソールの文字コード(oem)で読む。**
#: 日本語の Windows では Shift-JIS(「情報: 指定された条件に一致するタスクは…」)。Python の
#: 既定(UTF-8 モードのとき UTF-8)で読むと、読めずに落ちて起動が止まっていた。読めない字は置き換える
CONSOLE_ENCODING = "oem" if os.name == "nt" else None

log = get_logger("launch_guard")

# `/api/health` を叩くときの待ち時間(秒)。ローカルなので短くてよい。
# 長いと、死んだロックの掃除に毎回この分だけ待たされる
HEALTH_TIMEOUT_SEC = 1.5


@dataclass
class LockInfo:
    """`runtime/app.lock` の中身。"""

    app_id: str
    pid: int
    port: int
    url: str
    started_at: float
    python: str = ""
    app_root: str = ""
    # 起動トークン。`stop.bat` が「正常終了を要求する」ために要る
    # (基盤仕様書 2.8 は、PIDで落とす前にまずアプリ自身へ頼むことを
    # 求めている)。ロックは利用者ごとのローカル領域にあり、
    # 読めるのは同じ利用者だけ。そこまで入れる相手はプロセスを直接
    # 落とせるので、ここに置いても守りの強さは変わらない。
    # 逆に、悪意あるWebページはローカルのファイルを読めないため、
    # トークンによる防御(別オリジンからの操作を弾く)はそのまま効く。
    token: str = ""
    # プロセスの作成時刻(Windows は FILETIME)。**PID は使い回される**ので、
    # 停止前に「本当にこのアプリか」を確かめるのに使う。`wmic` は
    # Windows 11 24H2 で無くなったため、コマンドラインの照合だけに頼らない
    create_time: int = 0
    # プロセスの起動した時刻(エポック秒。`create_time` と同じものを時刻にした値)。日報複合ツールの
    # 入口(`portal/browser_tools.py`)も各ツールの印をこれで見る。0 は前の版の印(`lock_owner`)
    process_started: float = 0.0

    @property
    def started_text(self) -> str:
        return time.strftime("%Y/%m/%d %H:%M:%S", time.localtime(self.started_at))


def lock_path() -> Path:
    return app_config.local_dir("runtime") / "app.lock"


# ------------------------------------------------------------------
# ロックファイル
# ------------------------------------------------------------------
def write_lock(info: LockInfo) -> Path:
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(info), ensure_ascii=False, indent=2),
                    encoding="utf-8")
    # トークンを含むので、本人だけが読める権限にする。
    # Windows には chmod が効かないが、`%LOCALAPPDATA%` 自体が
    # 利用者ごとのフォルダなので実質同じ扱いになる
    try:
        os.chmod(path, 0o600)
    except OSError as exc:                        # noqa: BLE001 - 権限設定は best effort
        log.debug("ロックの権限を変更できませんでした: %s", exc)
    log.info("ロックを書きました: %s (pid=%s port=%s)", path, info.pid, info.port)
    return path


def read_lock() -> Optional[LockInfo]:
    """壊れていれば `None`。読めないことを理由に起動を止めない。"""
    path = lock_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return LockInfo(**{k: raw[k] for k in LockInfo.__dataclass_fields__ if k in raw})
    except FileNotFoundError:
        return None
    except Exception as exc:                      # noqa: BLE001 - 壊れたロックは無視する
        log.warning("ロックを読めませんでした (%s): %s", path, exc)
        return None


def remove_lock() -> None:
    try:
        lock_path().unlink()
        log.info("ロックを消しました")
    except FileNotFoundError:
        pass
    except OSError as exc:                        # noqa: BLE001
        log.warning("ロックを消せませんでした: %s", exc)


def release_lock() -> None:
    """**自分が取ったロックだけ**を片付ける。

    終わるときに無条件で消すと、入れ替えのときに困る ── 古いほうが終わる頃には
    新しいほうがもうロックを取っているので、古いほうの後始末が新しいほうのロックを
    消し、次の起動で「誰も居ない」と判定されて 2 つ目が立つ(日報・カレンダーで直した不具合)。
    """
    info = read_lock()
    if info is None:
        return
    if info.pid != os.getpid():
        log.info("自分のロックではないので残します (pid=%s)", info.pid)
        return
    remove_lock()


def update_lock(info: LockInfo) -> None:
    """取ったロックへ、決まったポートとトークンを書く。**自分のロックでなければ書かない。**

    待ち受けの確認(最大 15 秒)のあいだに、古い起動の後始末や別の起動がロックを
    差し替えていたら、上書きすると動いているほうの居場所が消える。
    """
    current = read_lock()
    if current is not None and current.pid != os.getpid():
        log.warning("自分のロックではないので書き換えません (pid=%s)", current.pid)
        return
    write_lock(info)


# 起動中(まだポートが決まっていない)ロックの `port`
STARTING_PORT = 0

# 起動中のロックを見つけたとき、ポートが入るのを待つ上限(秒)。
# 待ち受けの確認(`start_app.LISTEN_TIMEOUT_SEC`)より長くしておく ──
# 短いと、立ち上がりかけの1つ目を「死んでいる」と誤って追い出す
STARTING_WAIT_SEC = 20.0


def try_acquire() -> bool:
    """**割り込まれない形で**ロックを取る。取れたら `True`。

    起動の判定とロックの書き込みが離れていると、そのあいだに始まった
    2つ目が「ロックなし」を見て一緒に立ち上がる ── 実際に起きていた
    多重起動はこれで、待ち受けの確認に最大15秒かかるあいだ、窓が
    開きっぱなしだった。

    ここでは `O_CREAT | O_EXCL` で作る。**あるかどうかを見てから作る**
    のではなく、作れたかどうかで決めるので、2つ目は必ず失敗する
    (OSがひとつの操作として面倒を見る。Windowsでも同じ)。

    この時点ではポートも token もまだ無いので `port=0` で書いておく。
    決まったら `write_lock` が同じファイルを上書きする。
    """
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    info = LockInfo(
        app_id=app_config.app_id(), pid=os.getpid(),
        port=STARTING_PORT, url="", started_at=time.time(),
        python=sys.executable, app_root=str(app_config.APP_ROOT),
        create_time=_my_create_time(), process_started=my_started())
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    except OSError as exc:                        # noqa: BLE001
        # 作れない事情(権限など)で起動そのものを止めない。
        # 多重起動の守りは弱まるが、動かないよりはよい
        log.warning("ロックを作れませんでした (%s): %s", path, exc)
        return True
    with os.fdopen(fd, "w", encoding="utf-8") as fp:
        json.dump(asdict(info), fp, ensure_ascii=False, indent=2)
    log.info("ロックを取りました: %s (pid=%s 起動中)", path, info.pid)
    return True


# ------------------------------------------------------------------
# プロセスの生死
# ------------------------------------------------------------------
def is_process_alive(pid: int) -> bool:
    """そのPIDのプロセスが存在するか。中身までは見ない。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        return _is_alive_windows(pid)
    try:
        os.kill(pid, 0)          # シグナル0は存在確認だけ
    except ProcessLookupError:
        return False
    except PermissionError:
        return True              # 別ユーザーのプロセス = 生きている
    return True


def _is_alive_windows(pid: int) -> bool:
    """Windows では `tasklist` で確認する。

    `OpenProcess` を ctypes で叩く手もあるが、権限やハンドルの後始末を
    誤ると別の不具合を招く。起動時に1回だけの判定なので、
    外部コマンドの数十msは問題にならない。
    """
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, encoding=CONSOLE_ENCODING, errors="replace", timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("tasklist を実行できませんでした: %s", exc)
        return True              # 分からないときは「生きている」に倒す
    return f'"{pid}"' in (out.stdout or "")


def process_command_line(pid: int) -> str:
    """そのPIDが何を実行しているか。**停止前の確認に使う**。

    基盤仕様書 2.8 は「Pythonをプロセス名だけで一括終了しない」ことを
    求めている。無関係なPythonアプリを巻き添えにしないため、
    落とす前にコマンドラインとアプリの場所を照合する。
    取れなければ空文字(呼び出し側は安全側に倒して落とさない)。
    """
    if pid <= 0:
        return ""
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["wmic", "process", "where", f"ProcessId={pid}", "get",
                 "CommandLine", "/format:list"],
                capture_output=True, text=True, encoding=CONSOLE_ENCODING, errors="replace", timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            for line in (out.stdout or "").splitlines():
                if line.startswith("CommandLine="):
                    return line.split("=", 1)[1].strip()
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("wmic を実行できませんでした: %s", exc)
        return ""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except OSError:
        return ""


# ------------------------------------------------------------------
# 印の PID が、印を書いた本人のままか(PID の使い回し)
# ------------------------------------------------------------------
# 印には PID を書き、「その PID が生きていれば動いている」と見ていた。**Windows は PID を
# 使い回す。** 動いたまま PC を終了・再起動すると印が残り、再起動のあとで**まったく別の
# プログラムが同じ番号を持つ**ことがある。すると「起動中」と見続けて画面に接続できず、
# stop.bat も「接続できない」で終わって印を消さなかった(別のツールで見つかった不具合)。
#
#     印に書くもの   PID と、そのプロセスの**起動した時刻**(`process_started`。エポック秒)
#     本人とみなす   PID が生きていて、起動時刻が印のものと合うときだけ
#     前の版の印     起動時刻が無い。「印を書いた時刻(ファイルの更新時刻)より後に起動した
#                    プロセスは、書いた本人ではない」とみなす(本人は書く前に起動している)
#
# 本人が `NO_ANSWER_WAIT_SEC` 秒たっても答えないときは、**本人と確かめたうえで**終わらせて
# 起動し直す(stop.bat も同じ)。関係のないプログラムは終わらせない。
# 日報複合ツールの入口(`portal/proc_identity.py`)と同じ考え。直すときは揃える。

#: 起動時刻を比べるときの許し(秒)。Windows は同じ値が返る。Linux は btime が秒単位
START_TOLERANCE_SEC = 2.0
#: 印の本人が答えないとき、答えるのを待つ上限(秒)。過ぎたら本人を終わらせる
NO_ANSWER_WAIT_SEC = 5.0
#: 残っていた印を片付けたときのログ(stop.bat・起動。日報複合ツールの入口と同じ言葉)
CLEANED = "片付けました。Start.vbs で起動できます"

OWNER_GONE, OWNER_SAME, OWNER_OTHER, OWNER_UNKNOWN = "gone", "same", "other", "unknown"

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    _k32.GetProcessTimes.restype = wintypes.BOOL
    _k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _k32.WaitForSingleObject.restype = wintypes.DWORD
    _k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.TerminateProcess.restype = wintypes.BOOL
    _QUERY = 0x1000                 # PROCESS_QUERY_LIMITED_INFORMATION
    _TERMINATE = 0x0001
    _SYNCHRONIZE = 0x00100000

    def _handle_started(handle) -> Optional[float]:
        if _k32.WaitForSingleObject(handle, 0) == 0:          # もう終わっている
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not _k32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        return filetime_to_epoch((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)

    def _started(pid: int) -> Optional[float]:
        handle = _k32.OpenProcess(_QUERY | _SYNCHRONIZE, False, int(pid))
        if not handle:
            return None             # 居ない・開けない(同じ利用者のツールなら必ず開ける)
        try:
            return _handle_started(handle)
        finally:
            _k32.CloseHandle(handle)

    def _terminate(pid: int, matches, wait_sec: float) -> bool:
        """開いた握りで起動時刻を確かめてから止める(確かめてから止めるまでに PID が入れ替わらない)。"""
        handle = _k32.OpenProcess(_TERMINATE | _QUERY | _SYNCHRONIZE, False, int(pid))
        if not handle:
            return False
        try:
            started = _handle_started(handle)
            if started is None:
                return True
            if not matches(started):
                return False
            _k32.TerminateProcess(handle, 1)
            return _k32.WaitForSingleObject(handle, int(max(0.0, wait_sec) * 1000)) == 0
        finally:
            _k32.CloseHandle(handle)

else:
    def _boot_time() -> Optional[float]:
        try:
            with open("/proc/stat", encoding="ascii") as fh:
                for line in fh:
                    if line.startswith("btime "):
                        return float(line.split()[1])
        except (OSError, ValueError):
            pass
        return None

    def _started(pid: int) -> Optional[float]:
        try:
            with open(f"/proc/{int(pid)}/stat", encoding="utf-8", errors="replace") as fh:
                stat = fh.read()
        except OSError:
            return None
        fields = stat[stat.rfind(")") + 2:].split()     # 名前に空白があっても崩れない
        boot = _boot_time()
        if len(fields) < 20 or fields[0] == "Z" or boot is None:
            return None
        try:
            return boot + int(fields[19]) / os.sysconf("SC_CLK_TCK")
        except (ValueError, OSError):
            return None

    def _terminate(pid: int, matches, wait_sec: float) -> bool:
        import signal

        started = _started(pid)
        if started is None:
            return True
        if not matches(started):
            return False
        for sig in (signal.SIGTERM, getattr(signal, "SIGKILL", signal.SIGTERM)):
            try:
                os.kill(int(pid), sig)
            except ProcessLookupError:
                return True
            except OSError:
                return False
            deadline = time.monotonic() + max(0.0, wait_sec) / 2
            while time.monotonic() < deadline:
                if _started(pid) is None:
                    return True
                time.sleep(0.1)
        return _started(pid) is None


def filetime_to_epoch(filetime: int) -> float:
    """Windows の FILETIME(1601年からの 100ns)をエポック秒にする。"""
    return filetime / 10_000_000 - 11_644_473_600


def process_started(pid: int) -> Optional[float]:
    """その PID のプロセスが起動した時刻(エポック秒)。居ない・確かめられないときは None。"""
    if not pid or int(pid) <= 0:
        return None
    try:
        return _started(int(pid))
    except Exception:                             # noqa: BLE001 - 確かめられない
        return None


def my_started() -> float:
    """自分の起動時刻(印に書く)。確かめられない環境では 0(前の版の印と同じ扱いになる)。"""
    return process_started(os.getpid()) or 0.0


def _start_matches(recorded: float, written: Optional[float]):
    def matches(started: float) -> Optional[bool]:
        if recorded:
            return abs(started - float(recorded)) <= START_TOLERANCE_SEC
        if written is None:
            return None
        return started <= float(written) + START_TOLERANCE_SEC   # 本人は印を書く前に起動している
    return matches


def owner_state(pid: int, recorded: float = 0.0, written: Optional[float] = None) -> str:
    """印の PID が書いた本人か。

    `OWNER_GONE` 居ない / `OWNER_SAME` 本人 / `OWNER_OTHER` 別のプロセス(PID の使い回し) /
    `OWNER_UNKNOWN` 確かめられない(呼び出し側は今までどおり PID の生き死にだけで決める)。
    `recorded` は印の起動時刻(無ければ 0)、`written` は印の更新時刻(読めなければ None)。
    """
    if int(pid or 0) == os.getpid():
        return OWNER_UNKNOWN                      # 自分自身の印(起動時刻で見分けることではない)
    if not is_process_alive(int(pid or 0)):
        return OWNER_GONE
    started = process_started(pid)
    if started is None:
        # 自分のは取れるのに相手のが取れない = 居ない・別の利用者・守られたプロセス。
        # 同じ利用者のツールなら必ず取れるので、書いた本人ではない
        return OWNER_OTHER if process_started(os.getpid()) is not None else OWNER_UNKNOWN
    verdict = _start_matches(recorded, written)(started)
    if verdict is None:
        return OWNER_UNKNOWN
    return OWNER_SAME if verdict else OWNER_OTHER


def terminate_owner(pid: int, recorded: float = 0.0, written: Optional[float] = None,
                    wait_sec: float = 5.0) -> bool:
    """**本人と確かめたプロセスだけ**を止める。止まった(居なくなった)ら True。"""
    if int(pid or 0) == os.getpid():
        return False                              # 自分は止めない
    check = _start_matches(recorded, written)
    try:
        return _terminate(int(pid), lambda started: bool(check(started)), wait_sec)
    except Exception:                             # noqa: BLE001
        return False


def file_mtime(path) -> Optional[float]:
    """印の更新時刻(前の版の印の判定に使う)。読めなければ None。"""
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def lock_owner(info: LockInfo) -> str:
    """印(`app.lock`)の PID が、印を書いた本人のままか(`owner_state`)。

    前の版の点検表の印は作成時刻(`create_time`)だけを持つ。それがあれば作成時刻で比べる。
    """
    if not info.process_started and info.create_time:
        if not is_process_alive(info.pid):
            return OWNER_GONE
        from core.process_tracking import is_same_process
        return OWNER_SAME if is_same_process(info.pid, info.create_time) else OWNER_OTHER
    return owner_state(info.pid, info.process_started, file_mtime(lock_path()))


def remove_lock_if(info: LockInfo) -> bool:
    """読んだ印が**まだそのまま**なら消す。

    読んでから消すまでに、新しく起動したほうが印を取っていたら消さない。**読めない印も
    消さない**(1.5.79〜1.5.81 の決まり: 読めないのを「無い・壊れている」と取り違えない)。
    """
    now = read_lock()
    if now is None or now.pid != info.pid or now.started_at != info.started_at:
        return False
    remove_lock()
    return True


def _wait_answer(info: LockInfo) -> Optional[dict]:
    """印の本人が答えるのを `NO_ANSWER_WAIT_SEC` 秒まで待つ。答えたら `/api/health` の中身。"""
    deadline = time.monotonic() + NO_ANSWER_WAIT_SEC
    while True:
        health = probe_health(info.port) if info.port else None
        if is_our_app(health):
            return health
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.3)


def _end_unanswered(info: LockInfo) -> bool:
    """印の本人(PID と起動時刻が合う)が答えない。**本人だけ**を終わらせ、印を片付ける。"""
    log.warning("pid=%s (port=%s) が応答しません。終わらせて起動し直します", info.pid, info.port)
    if not terminate_owner(info.pid, info.process_started, file_mtime(lock_path())):
        log.warning("pid=%s を終わらせられませんでした", info.pid)
        return False
    remove_lock_if(info)
    log.info("応答しない前の起動を終わらせました。%s", CLEANED)
    return True


# ------------------------------------------------------------------
# 起動確認API
# ------------------------------------------------------------------
# 自分自身(127.0.0.1)への通信に**プロキシを通さない**ための送信口。
#
# `urllib.request.urlopen()` の既定は、環境変数 `HTTP_PROXY` や
# Windowsのインターネット設定からプロキシを拾う。社内PCではたいてい
# プロキシが入っており、除外一覧に `127.0.0.1` が無いと、**自分自身への
# 通信までプロキシへ送られて失敗する**。
#
# 実際に踏んだ: サーバは待ち受けを始めているのに `/api/health` が
# 返らず、15秒待って「サーバを起動できませんでした」になった。
# ループバックにプロキシを挟む理由は無いので、ここで明示的に外す。
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def local_request(url: str, *, timeout: float, data: Optional[bytes] = None,
                  method: Optional[str] = None,
                  headers: Optional[dict] = None):
    """127.0.0.1 への要求。プロキシを経由しない。

    このアプリが外に出す通信はこれだけなので、送信口を1つに集約しておく。
    """
    request = urllib.request.Request(url, data=data, method=method,
                                     headers=headers or {})
    return _LOCAL_OPENER.open(request, timeout=timeout)


def probe_health(port: int, *, timeout: float = HEALTH_TIMEOUT_SEC) -> Optional[dict]:
    """`GET /api/health` を叩く。応答しなければ `None`。"""
    url = f"http://127.0.0.1:{port}/api/health"
    try:
        with local_request(url, timeout=timeout) as res:
            return json.loads(res.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None


def is_port_accepting(port: int, *, timeout: float = 0.5) -> bool:
    """TCPで繋がるか。HTTPまでは見ない。

    「待ち受けていない」のか「待ち受けてはいるが応答が返らない」のかを
    分けるために使う。後者はプロキシやセキュリティ製品が挟まっている
    ことが多く、直し方がまったく違う。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def proxy_settings() -> dict[str, str]:
    """いま効いているプロキシ設定。原因調査のためだけに使う。"""
    try:
        return {k: v for k, v in urllib.request.getproxies().items()
                if k in ("http", "https")}
    except Exception:                    # noqa: BLE001 - 調査用なので握る
        return {}


def is_our_app(health: Optional[dict]) -> bool:
    """その応答が**自分と同じアプリ**か。

    `app_id` を見ないと、たまたま同じポートを使っている別のアプリを
    自分だと誤認する(基盤仕様書 2.3)。
    """
    if not health:
        return False
    return health.get("app_id") == app_config.app_id()


# ------------------------------------------------------------------
# 既存インスタンスの判定
# ------------------------------------------------------------------
@dataclass
class GuardResult:
    """起動してよいか。"""

    should_start: bool
    url: str = ""
    existing: Optional[LockInfo] = None
    reason: str = ""
    # 動いているのが**古い版**だった場合、その版。合流してはいけない
    stale_version: str = ""


# 古い版が動いていたとき、終わるのを待つ上限(秒)
REPLACE_WAIT_SEC = 10.0


def check_existing() -> GuardResult:
    """すでに同じアプリが動いていないか調べる。

    動いていれば `should_start=False` と、開くべきURLを返す。

    **ただし版が違えば合流しません。** 入れ替えたのに古いプロセスが
    残っていると、ここが「すでに起動しています」と答えて古いほうの
    ブラウザを開き、**新しい版がいつまでも動きません**(画面の版バッジも
    古いままになる)。版が違うときは古いほうを終わらせて、こちらで
    立て直します。
    """
    info = read_lock()
    if info is None:
        return GuardResult(True, reason="ロックなし")

    state = lock_owner(info)
    if state == OWNER_GONE:
        log.info("死んだロックを掃除します (pid=%s は不在)", info.pid)
        if remove_lock_if(info):
            log.info(CLEANED)
        return GuardResult(True, reason=f"ロックは残っていたがpid {info.pid} は不在")
    if state == OWNER_OTHER:
        # **PID の使い回し。** PC の終了・再起動で残った印の番号を、別のプログラムが持っている
        log.info("残っていた印を片付けます (pid=%s は別のプロセスです。起動時刻が合いません)", info.pid)
        if remove_lock_if(info):
            log.info(CLEANED)
        return GuardResult(True, reason="ロックのプロセスは別物だった")

    if info.port == STARTING_PORT:
        return _wait_for_starting(info)

    health = probe_health(info.port)
    if not is_our_app(health) and state == OWNER_SAME:
        # 起動時刻まで合った本人が答えない。少し待って、答えなければ本人だけを終わらせる
        health = _wait_answer(info)
        if health is None:
            _end_unanswered(info)
            return GuardResult(True, reason="前の起動が応答しないので終わらせました")
    if not is_our_app(health):
        # プロセスは居るがアプリではない(PIDの使い回し、または
        # 起動途中で落ちた)。ロックを消して新しく起動する
        log.info("pid=%s は生きているが port %s は自分ではない", info.pid, info.port)
        remove_lock()
        return GuardResult(True, reason="ロックのプロセスは別物だった")

    running = str((health or {}).get("version", ""))
    mine = app_config.version()
    if running and running != mine:
        return _replace_stale(info, running, mine)

    log.info("すでに起動しています (pid=%s port=%s 版=%s)",
             info.pid, info.port, running or "(不明)")
    return GuardResult(False, url=info.url, existing=info,
                       reason="同じアプリが起動中")


def _wait_for_starting(info: "LockInfo") -> GuardResult:
    """1つ目がまだ立ち上がっている途中。**待ってから合流する。**

    起動には時間がかかる(共有フォルダの点検表の確認もある)ので、
    利用者は「反応が無い」と思ってもう一度押す ── 多重起動のいちばん
    多い入口がこれ。ここで待って同じ画面へ合流させる。

    立ち上がりきらないまま `STARTING_WAIT_SEC` を過ぎたら、1つ目は
    立ち上がりに失敗したものとして掃除する(そうしないと、失敗した
    ロックが残るかぎり二度と起動できなくなる)。
    """
    log.info("1つ目が起動中です (pid=%s)。ポートが決まるのを待ちます", info.pid)
    deadline = time.monotonic() + STARTING_WAIT_SEC
    while time.monotonic() < deadline:
        time.sleep(0.3)
        now = read_lock()
        if now is None:
            return GuardResult(True, reason="起動中だったロックが消えました")
        if now.port != STARTING_PORT:
            log.info("1つ目が立ち上がりました。合流します: %s", now.url)
            return GuardResult(False, url=now.url, existing=now,
                               reason="起動中だった1つ目に合流します")
        if not is_process_alive(now.pid):
            remove_lock()
            return GuardResult(True, reason="起動中のプロセスが居なくなりました")

    log.warning("1つ目が %.0f秒 で立ち上がりませんでした。掃除して起動します",
                STARTING_WAIT_SEC)
    now = read_lock()
    if now is not None and lock_owner(now) == OWNER_SAME and _end_unanswered(now):
        # 立ち上がりきらずに残った1つ目(起動時刻まで合う本人)を終わらせた。残すと2つになる
        return GuardResult(True, reason="起動中のまま応答がないので終わらせました")
    remove_lock()
    return GuardResult(True, reason="起動中のまま応答がないので引き継ぎます")


def _replace_stale(info: "LockInfo", running: str, mine: str) -> GuardResult:
    """動いているのが違う版。**終わらせてから立て直す。**

    処理の途中(印刷など)なら終わらせません ── 途中で止めるほうが害が
    大きいので、そのときは合流して、次の起動に任せます。
    """
    log.warning("動いているのは別の版です(動作中=%s / ファイル=%s)。"
                "古いほうを終わらせます", running, mine)
    if not request_shutdown(info.port, info.token):
        log.warning("古い版を終わらせられませんでした。合流します")
        return GuardResult(False, url=info.url, existing=info,
                           stale_version=running,
                           reason=f"別の版({running})が動いていますが止められません")

    deadline = time.monotonic() + REPLACE_WAIT_SEC
    while time.monotonic() < deadline:
        if probe_health(info.port, timeout=0.3) is None:
            remove_lock()
            log.info("古い版(%s)を終わらせました。%s で立て直します", running, mine)
            return GuardResult(True, stale_version=running,
                               reason=f"別の版({running})を終わらせました")
        time.sleep(0.2)

    log.warning("古い版が %.0f秒 で終わりませんでした。合流します", REPLACE_WAIT_SEC)
    return GuardResult(False, url=info.url, existing=info,
                       stale_version=running,
                       reason=f"別の版({running})が動いています")


def request_shutdown(port: int, token: str) -> bool:
    """`POST /api/shutdown` を送る。受け付けられたら `True`。

    **`force` は付けません。** 印刷の途中なら 409 が返り、
    そのときは止めません。
    `process_manager` にも同じ処理がありますが、あちらは停止そのものが
    仕事で、こちらは**起動の途中で古い版をどけるだけ**です。
    こちらから `process_manager` を読むと、起動の入口が停止側の
    モジュールに依存します(待機画面より前なので import を増やさない)。
    """
    body = json.dumps({"force": False}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Tool-Token"] = token
    try:
        with local_request(f"http://{app_config.host()}:{port}/api/shutdown",
                           timeout=5, data=body, method="POST",
                           headers=headers) as res:
            return bool(json.loads(res.read().decode("utf-8")).get("stopped"))
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            log.info("古い版は処理の途中なので止めません")
        else:
            log.info("古い版の停止要求が %s で断られました", exc.code)
        return False
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.info("古い版へ停止要求を送れませんでした: %s", exc)
        return False


# ------------------------------------------------------------------
# ポート
# ------------------------------------------------------------------
def is_port_free(port: int, host: str = "") -> bool:
    return not port_problem(port, host)


def port_problem(port: int, host: str = "") -> str:
    """そのポートで待ち受けを開始できなければ、その理由。できれば空文字。

    **waitress の bind と同じ条件で**確かめる。

    - Windows: `SO_REUSEADDR` を**付けない**。Windows では付けると、ほかの
      アプリが待ち受け中のポートまで「空いている」と判定してしまう。
      使えなかった理由(OS のエラー番号)はログに残す
    - それ以外: waitress は `SO_REUSEADDR` を付けて bind する。確かめる側も
      付ける。付けないと、再起動の直後(TIME_WAIT のあいだ)は前と同じ
      ポートを「使用中」と見て次の番号へずれ、**開いたままの画面が前の
      番号を見続けてつながり直せない**。待ち受け中のポートは付けても断られる。
    """
    host = host or app_config.host()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if os.name != "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError as exc:
            # 理由を残す。Windows の 10048 = ほかが使用中、10013 = OS が予約して
            # いる番号(Hyper-V などの除外範囲。`netsh interface ipv4 show
            # excludedportrange protocol=tcp` で見える)。10013 なら毎回ずれる
            return f"{getattr(exc, 'winerror', None) or exc.errno}: {exc.strerror or exc}"
    return ""


# 直近の pick_port で飛ばした番号と理由(起動の記録に残す)
SKIPPED_PORTS: list = []


def pick_port() -> Optional[int]:
    """使えるポートを1つ選ぶ。全部塞がっていれば `None`。"""
    SKIPPED_PORTS.clear()
    for port in app_config.port_candidates():
        problem = port_problem(port)
        if not problem:
            return port
        log.info("ポート %s は使用中です(%s)", port, problem)
        SKIPPED_PORTS.append(f"{port}: {problem}")
    return None


# ------------------------------------------------------------------
# 起動時の記録
# ------------------------------------------------------------------
def build_lock_info(port: int, token: str = "") -> LockInfo:
    return LockInfo(
        app_id=app_config.app_id(),
        pid=os.getpid(),
        port=port,
        url=f"http://{app_config.host()}:{port}/",
        started_at=time.time(),
        python=sys.executable,
        app_root=str(app_config.APP_ROOT),
        token=token,
        create_time=_my_create_time(),
        process_started=my_started(),
    )


def _my_create_time() -> int:
    from core.process_tracking import current_identity
    return int(current_identity().create_time or 0)
