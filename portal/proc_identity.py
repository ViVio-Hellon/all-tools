"""印(`runtime/*.lock`)に書いた PID が、**書いた本人のまま**かを見分ける

印には PID を書き、次の起動・stop.bat は「その PID が生きていれば動いている」と見ていた。
**Windows は PID を使い回す。** ツールが動いたまま PC を終了・再起動すると印が残り、再起動の
あとで**まったく別のプログラムが同じ番号を持つ**ことがある。すると「動いている」と見続け、
画面に接続できないまま止まり、stop.bat も「接続できない」で終わって印を消さなかった
(別のツールで見つかった不具合。同じ作りの所を直した)。

    印に書くもの   PID と、そのプロセスの**起動した時刻**(`process_started`。エポック秒)
    本人とみなす   PID が生きていて、起動時刻が印のものと合うときだけ
    前の版の印     起動時刻が無い。「印を書いた時刻(ファイルの更新時刻)より後に起動した
                   プロセスは、書いた本人ではない」とみなす(本人は書く前に起動している)

起動時刻は Windows では `GetProcessTimes` の作成時刻(ctypes。追加の部品は使わない)、
それ以外(開発機・試験)では `/proc/<pid>/stat` の starttime と起動の時刻(btime)から出す。

各ツール(`tools/*/launch_guard.py`)にも同じ考えの写しがある(ツールは単体でも配るので、
入口のこれを読まない)。直すときは揃える。
"""
from __future__ import annotations

import os
import time
from typing import Callable, Optional

#: 起動時刻を比べるときの許し(秒)。Windows は同じ値が返る。Linux は btime が秒単位で、
#: 時計の合わせで少し動く。使い回された PID の持ち主が、本人と2秒以内に起動していることは無い
#: (本人は前の起動 = 再起動の前に起動している)
START_TOLERANCE_SEC = 2.0

GONE, SAME, OTHER, UNKNOWN = "gone", "same", "other", "unknown"

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
        if _k32.WaitForSingleObject(handle, 0) == 0:          # WAIT_OBJECT_0 = もう終わっている
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not _k32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        return filetime_to_epoch((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)

    def _started(pid: int) -> Optional[float]:
        handle = _k32.OpenProcess(_QUERY | _SYNCHRONIZE, False, int(pid))
        if not handle:
            return None             # 居ない・開けない(**同じ利用者のツールなら必ず開ける**)
        try:
            return _handle_started(handle)
        finally:
            _k32.CloseHandle(handle)

    def _terminate(pid: int, matches: Callable[[float], bool], wait_sec: float) -> bool:
        """開いた握りで起動時刻を確かめてから止める(確かめてから止めるまでに PID が入れ替わらない)。"""
        handle = _k32.OpenProcess(_TERMINATE | _QUERY | _SYNCHRONIZE, False, int(pid))
        if not handle:
            return False
        try:
            started = _handle_started(handle)
            if started is None:
                return True         # もう終わっていた
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

    def _terminate(pid: int, matches: Callable[[float], bool], wait_sec: float) -> bool:
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


def _matcher(recorded: float, written: Optional[float]) -> Callable[[float], Optional[bool]]:
    def matches(started: float) -> Optional[bool]:
        if recorded:
            return abs(started - float(recorded)) <= START_TOLERANCE_SEC
        if written is None:
            return None
        # 前の版の印: 本人は印を書く前に起動している
        return started <= float(written) + START_TOLERANCE_SEC
    return matches


def owner(pid: int, recorded: float, written: Optional[float],
          alive: Callable[[int], bool]) -> str:
    """印の PID が書いた本人か。

    `GONE` 居ない / `SAME` 本人 / `OTHER` 別のプロセス(PID の使い回し) /
    `UNKNOWN` 確かめられない(呼び出し側は今までどおり PID の生き死にだけで決める)。
    `recorded` は印の起動時刻(無ければ 0)、`written` は印の更新時刻(読めなければ None)。
    `alive` は PID が生きているか(各所の今までの確かめ方。試験で差し替える)。
    """
    if not alive(int(pid or 0)):
        return GONE
    started = process_started(pid)
    if started is None:
        # 自分の起動時刻は取れるのに、相手のが取れない = 居ない・別の利用者・守られたプロセス。
        # 同じ利用者のツールなら必ず取れるので、書いた本人ではない
        return OTHER if process_started(os.getpid()) is not None else UNKNOWN
    verdict = _matcher(recorded, written)(started)
    if verdict is None:
        return UNKNOWN
    return SAME if verdict else OTHER


def terminate(pid: int, recorded: float, written: Optional[float], wait_sec: float = 5.0) -> bool:
    """**本人と確かめたプロセスだけ**を止める。止まった(居なくなった)ら True。

    確かめられない(起動時刻も印の時刻も無い)ときは止めない(False)。
    """
    if int(pid or 0) == os.getpid():
        return False                              # 自分は止めない
    check = _matcher(recorded, written)
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
