"""ブラウザ版とデスクトップ版を同時に動かさない(どちらを後から開いても止まる)

ブラウザ版(`Start.vbs` / `start.bat` → `start_app.py`)とデスクトップ版
(`日報複合ツール.exe` = Rust/Tauri の外枠、`src-tauri/src/instance.rs`)は、同じ4ツールの
同じ手元のデータ・同じ共有の DB を扱う。両方が動くと、二重の書き戻し・設定の
書き合いが起きる。**後から開いたほうが止まる。**

作りは点検表(vba-inspection-sheet-python-migration)の `core/instance_guard.py` と同じ。

【錠は OS の名前付きの錠にする(ファイルにしない)】
ラインPCの Python は Microsoft Store 版で、`%LOCALAPPDATA%` に書いたファイルは
Python 専用の場所へ振り替えられる。Python が書いた錠のファイルを exe からは
見つけられない。名前付きミューテックスは振り替えられないので、どちらの言語からも
同じ名前で見える。

    Windows   名前付きミューテックス(`Local\\` = 利用者のログオンごと)。プロセスが
              終われば OS が消す
    それ以外  手元の領域の runtime/ のファイルロック(flock)。開発機・試験用

【手順】(Rust 側も同じ。食い違うと止め損ねる。`tests/test_shell_contract.py` で突き合わせる)

    1. 自分の種類の目印を作る      …(desktop / browser)
    2. 本体の錠を作る
       ├─ 作れた                  → 起動してよい(両方を終わるまで握る)
       └─ すでにあった
          ├─ 相手の種類の目印がある → 相手が動いている。止まる
          └─ 無い                  → 同じ種類が動いている(ブラウザ版はつなぐ・
                                       デスクトップ版は窓を前に出す)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, List, Optional

from . import app_config

BROWSER = "browser"
DESKTOP = "desktop"
KINDS = (BROWSER, DESKTOP)
LABELS = {BROWSER: "ブラウザ版", DESKTOP: "デスクトップ版"}

#: デスクトップ版の exe の名前(一式のフォルダの直下に置く。配るときの名前が先)。
#: 「統合ツール.exe」は 1.4.0 で名前を変える前に配ったもの(置いたままの PC でも見つける)
DESKTOP_EXES = ("日報複合ツール.exe", "AllTools.exe", "統合ツール.exe")


def base_name() -> str:
    """錠の名前の頭。`app_id` から作る(ほかのアプリと混ざらない)。"""
    app_id = app_config.app_id().strip() or "nlm.all-tools"
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in app_id)


def names() -> dict:
    base = base_name()
    return {"instance": f"{base}.instance", DESKTOP: f"{base}.desktop", BROWSER: f"{base}.browser"}


@dataclass
class Claim:
    """`claim()` の結果。`ok` なら握っている(`release()` かプロセスの終わりまで)。"""
    kind: str
    ok: bool = False
    running: str = ""                    # 取れなかったとき、動いているほうの種類
    _handles: List[Any] = field(default_factory=list, repr=False)

    @property
    def other_kind_running(self) -> bool:
        return not self.ok and self.running != "" and self.running != self.kind

    def release(self) -> None:
        while self._handles:
            _close(self._handles.pop())


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    _k32.CreateMutexW.restype = wintypes.HANDLE
    _k32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    _k32.OpenMutexW.restype = wintypes.HANDLE
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _ERROR_ALREADY_EXISTS = 183
    _SYNCHRONIZE = 0x00100000

    def _full(name: str) -> str:
        return "Local\\" + name

    def _create(name: str, exclusive: bool) -> tuple[Optional[Any], bool]:
        """作る。(握り, すでにあったか)。目印(exclusive=False)はあっても握る。"""
        handle = _k32.CreateMutexW(None, False, _full(name))
        existed = ctypes.get_last_error() == _ERROR_ALREADY_EXISTS
        if not handle:
            raise OSError(ctypes.get_last_error(), f"錠を作れません: {name}")
        if existed and exclusive:
            _k32.CloseHandle(handle)
            return None, True
        return handle, existed

    def _exists(name: str) -> bool:
        handle = _k32.OpenMutexW(_SYNCHRONIZE, False, _full(name))
        if handle:
            _k32.CloseHandle(handle)
            return True
        return False

    def _close(handle: Any) -> None:
        _k32.CloseHandle(handle)

else:
    import fcntl

    def _path(name: str):
        return app_config.local_dir("runtime") / f"{name}.lock"

    def _create(name: str, exclusive: bool) -> tuple[Optional[Any], bool]:
        fd = os.open(_path(name), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            # 本体は1人だけ(EX)。目印は同じ種類が何人でも握れる(SH)
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return None, True
        return fd, False

    def _exists(name: str) -> bool:
        """誰かが握っているか(握って確かめ、すぐ放す)。"""
        try:
            fd = os.open(_path(name), os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        finally:
            os.close(fd)                          # 閉じれば放れる
        return False

    def _close(handle: Any) -> None:
        try:
            os.close(handle)
        except OSError:
            pass


_held: List[Claim] = []                  # 握った錠(プロセスの終わりまで手放さない)


def claim(kind: str) -> Claim:
    """`kind` として動いてよいかを決め、よければ錠を握る。"""
    if kind not in KINDS:
        raise ValueError(kind)
    n = names()
    other = DESKTOP if kind == BROWSER else BROWSER
    result = Claim(kind)
    marker, _ = _create(n[kind], exclusive=False)          # 1. 自分の目印を先に
    if marker is not None:
        result._handles.append(marker)
    instance, _existed = _create(n["instance"], exclusive=True)   # 2. 本体
    if instance is not None:
        result._handles.append(instance)
        result.ok = True
        _held.append(result)
        return result
    result.running = other if _exists(n[other]) else kind
    result.release()
    return result


def running() -> str:
    """いま動いているほうの種類(どちらも動いていなければ空)。錠は取らない。"""
    n = names()
    if _exists(n[DESKTOP]):
        return DESKTOP
    if _exists(n["instance"]) or _exists(n[BROWSER]):
        return BROWSER
    return ""


def release_all() -> None:
    """試験用: 握った錠を全部放す。"""
    while _held:
        _held.pop().release()


def desktop_exe() -> Optional[str]:
    """デスクトップ版の exe(あれば)。ブラウザ版から窓を前に出してもらうのに使う。"""
    for name in DESKTOP_EXES:
        path = app_config.APP_ROOT / name
        if path.is_file():
            return str(path)
    return None


def bring_desktop_to_front() -> bool:
    """デスクトップ版の窓を前に出す。

    exe をもう一度起動すると、2つ目は開かずに1つ目の窓を前に出して終わる
    (`tauri-plugin-single-instance`)。それを使う。
    """
    exe = desktop_exe()
    if not exe or os.name != "nt":
        return False
    import subprocess
    try:
        subprocess.Popen([exe], cwd=str(app_config.APP_ROOT), close_fds=True)
    except OSError:
        return False
    return True
