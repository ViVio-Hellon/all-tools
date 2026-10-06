"""このアプリが起動した子プロセス(Excel / cscript)の記録と後片付け

【なぜ要るのか】
Excel は VBScript(`cscript.exe`)から COM で起動するため、Excel の
プロセスは cscript の子にならない。cscript を止めても Excel は残る。
非表示の Excel が残り続けると、プレビューを繰り返すたびに EXCEL.EXE が
増えていく(要件定義 13)。

そこで Excel の **PID とプロセス作成時刻** を `runtime/tracked_processes.json`
に残し、終わらなければ**その PID だけ**を止める。作成時刻まで照合するのは、
PID は使い回されるため ── 利用者が自分で開いている Excel を巻き添えにしない。

アプリの起動時・終了時・stop.bat のときにも記録を確かめ、残っていれば片付ける。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

IS_WINDOWS = os.name == "nt"


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    create_time: Optional[int]  # Windows: FILETIME(100ns), Linux: 起動からの clock tick
    exe: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"pid": self.pid, "create_time": self.create_time, "exe": self.exe}


# ======================================================================
# OS 別の実装
# ======================================================================
if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _u32 = ctypes.WinDLL("user32", use_last_error=True)

    PROCESS_TERMINATE = 0x0001
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    SYNCHRONIZE = 0x00100000
    WAIT_OBJECT_0 = 0
    WAIT_TIMEOUT = 0x102

    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    _k32.GetProcessTimes.restype = wintypes.BOOL
    _k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]
    _k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    _k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _k32.WaitForSingleObject.restype = wintypes.DWORD
    _k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.TerminateProcess.restype = wintypes.BOOL
    _u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _u32.GetWindowThreadProcessId.restype = wintypes.DWORD

    def _open(pid: int, access: int) -> Optional[int]:
        handle = _k32.OpenProcess(access, False, int(pid))
        return handle or None

    def get_process_identity(pid: int) -> Optional[ProcessIdentity]:
        handle = _open(pid, PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE)
        if not handle:
            return None
        try:
            if _k32.WaitForSingleObject(handle, 0) == WAIT_OBJECT_0:
                return None  # 既に終了している
            created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
            create_time = None
            if _k32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                    ctypes.byref(kernel), ctypes.byref(user)):
                create_time = (created.dwHighDateTime << 32) | created.dwLowDateTime
            size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            exe = buf.value if _k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)) else ""
            return ProcessIdentity(int(pid), create_time, exe)
        finally:
            _k32.CloseHandle(handle)

    def _wait_exit_raw(pid: int, timeout: float) -> bool:
        handle = _open(pid, SYNCHRONIZE)
        if not handle:
            return True
        try:
            return _k32.WaitForSingleObject(handle, int(max(0.0, timeout) * 1000)) == WAIT_OBJECT_0
        finally:
            _k32.CloseHandle(handle)

    def _terminate_raw(pid: int) -> bool:
        handle = _open(pid, PROCESS_TERMINATE | SYNCHRONIZE)
        if not handle:
            return False
        try:
            return bool(_k32.TerminateProcess(handle, 1))
        finally:
            _k32.CloseHandle(handle)

    def pid_from_hwnd(hwnd: int) -> Optional[int]:
        """ウィンドウハンドルからプロセスIDを得る（Excel の Application.Hwnd 用）。"""
        pid = wintypes.DWORD(0)
        _u32.GetWindowThreadProcessId(wintypes.HWND(int(hwnd)), ctypes.byref(pid))
        return int(pid.value) or None

else:  # ---- Windows 以外（開発・テスト用） -------------------------
    import signal

    def get_process_identity(pid: int) -> Optional[ProcessIdentity]:
        try:
            os.kill(int(pid), 0)
        except (OSError, ValueError):
            return None
        create_time = None
        exe = ""
        try:
            with open(f"/proc/{int(pid)}/stat", "r") as fp:
                stat = fp.read()
            # comm に空白が含まれても良いよう、最後の ')' 以降を分割する
            fields = stat[stat.rfind(")") + 2:].split()
            if fields and fields[0] == "Z":
                return None  # ゾンビは終了済み扱い
            create_time = int(fields[19])
            with open(f"/proc/{int(pid)}/comm", "r") as fp:
                exe = fp.read().strip()
        except (OSError, IndexError, ValueError):
            pass
        return ProcessIdentity(int(pid), create_time, exe)

    def _wait_exit_raw(pid: int, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if get_process_identity(pid) is None:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)

    def _terminate_raw(pid: int) -> bool:
        try:
            os.kill(int(pid), signal.SIGTERM)
            return True
        except OSError:
            return False

    def pid_from_hwnd(hwnd: int) -> Optional[int]:
        return None


# ======================================================================
# OS 共通の安全な操作
# ======================================================================
def current_identity() -> ProcessIdentity:
    identity = get_process_identity(os.getpid())
    return identity or ProcessIdentity(os.getpid(), None, sys.executable)


def is_same_process(pid: int, create_time: Optional[int]) -> bool:
    """PID が再利用されていないか（作成時刻まで一致するか）確認する。"""
    identity = get_process_identity(pid)
    if identity is None:
        return False
    if create_time is None or identity.create_time is None:
        return False  # 作成時刻で確認できないものは「同一」とみなさない
    return identity.create_time == create_time


def wait_for_exit(pid: int, create_time: Optional[int], timeout: float) -> bool:
    if not is_same_process(pid, create_time):
        return True
    return _wait_exit_raw(pid, timeout)


def terminate_verified(pid: int, create_time: Optional[int],
                       allowed_exe_names: Optional[List[str]] = None, wait_sec: float = 5.0) -> bool:
    """PID・作成時刻（・実行ファイル名）がすべて一致した場合だけ強制終了する。

    戻り値: 対象プロセスが存在しない（終了済み）状態になれば True。
    """
    identity = get_process_identity(pid)
    if identity is None:
        return True
    if create_time is None or identity.create_time != create_time:
        return True  # 別プロセス（PID 再利用）なので触らない
    if allowed_exe_names:
        exe_name = os.path.basename(identity.exe or "").lower()
        if exe_name and exe_name not in [n.lower() for n in allowed_exe_names]:
            return True  # 想定外の実行ファイルなので触らない
    _terminate_raw(pid)
    return _wait_exit_raw(pid, wait_sec)


class TrackedProcessRegistry:
    """このアプリが起動した子プロセス（Excel 等）を記録し、残留を確実に片付ける。"""

    def __init__(self, path: str, logger: Any = None):
        self.path = path
        self.log = logger
        self._lock = threading.Lock()

    def _load(self) -> List[Dict[str, Any]]:
        try:
            with open(self.path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save(self, entries: List[Dict[str, Any]]) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        # 書きかけのファイル名はプロセスとスレッドごとに分ける。stop.bat と
        # アプリの終了処理が同時に片付けると、同じ名前では片方が失敗していた
        tmp = f"{self.path}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(entries, fp, ensure_ascii=False, indent=1)
        for attempt in range(5):
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:                # Windows: 相手が読んでいる最中
                time.sleep(0.05 * (attempt + 1))
        os.replace(tmp, self.path)

    def add(self, identity: ProcessIdentity, label: str) -> None:
        with self._lock:
            entries = [e for e in self._load() if e.get("pid") != identity.pid]
            me = current_identity()
            entries.append({**identity.to_dict(), "label": label,
                            "registered_at": datetime.now().isoformat(timespec="seconds"),
                            "owner_pid": me.pid, "owner_create_time": me.create_time})
            self._save(entries)

    def remove(self, pid: int) -> None:
        with self._lock:
            entries = self._load()
            remaining = [e for e in entries if e.get("pid") != pid]
            if len(remaining) != len(entries):
                self._save(remaining)

    def entries(self) -> List[Dict[str, Any]]:
        with self._lock:
            return self._load()

    def cleanup(self, reason: str, allowed_exe_names: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """記録済みプロセスのうち、まだ残っているものを安全に終了する。"""
        cleaned: List[Dict[str, Any]] = []
        kept: List[Dict[str, Any]] = []
        me = os.getpid()
        with self._lock:
            entries = self._load()
            for entry in entries:
                pid, ctime = int(entry.get("pid", 0)), entry.get("create_time")
                owner, owner_ctime = int(entry.get("owner_pid", 0) or 0), entry.get("owner_create_time")
                if owner and owner != me and owner_ctime and is_same_process(owner, owner_ctime):
                    # **まだ動いているほかのアプリのもの。** 触らない(使い回し中の Excel を
                    # 別のプロセス ── 2つ目の起動・stop.bat ── が止めてしまわないように)
                    kept.append(entry)
                    continue
                if pid and is_same_process(pid, ctime):
                    ok = terminate_verified(pid, ctime, allowed_exe_names)
                    cleaned.append({**entry, "terminated": ok})
                    if self.log:
                        self.log.warning("残留プロセスを終了しました pid=%s label=%s 理由=%s 結果=%s",
                                         pid, entry.get("label"), reason, "成功" if ok else "失敗")
            self._save(kept)
        return cleaned


EXCEL_PROCESS_NAMES = ["excel.exe", "cscript.exe"]


def registry(path: Optional[str] = None, logger: Any = None) -> "TrackedProcessRegistry":
    """ローカル領域の記録ファイルを使う registry。"""
    if path is None:
        from . import app_config
        path = str(app_config.local_dir("runtime") / "tracked_processes.json")
    return TrackedProcessRegistry(path, logger)


def cleanup_leftovers(reason: str, logger: Any = None) -> int:
    """記録に残っている Excel / cscript を片付ける。片付けた数を返す。"""
    names = EXCEL_PROCESS_NAMES if IS_WINDOWS else None
    return len(registry(logger=logger).cleanup(reason, names))

