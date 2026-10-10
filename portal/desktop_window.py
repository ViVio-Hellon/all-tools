"""デスクトップ版(日報複合ツール.exe)の窓を外から見つけて、「閉じて」と頼む(Windows だけ)

業務ツール統合ランチャーの終了の入口(`launcher_stop.bat`)から使う。窓に `WM_CLOSE` を
送るのは **窓の × を押したのと同じ** ── 外枠が各ツールの画面に打ちかけを置いてもらい、
全ツールに「終わってよいか」を訊き、途中の処理があれば1つの確認を出してから閉じる。
プロセスを落とすのではない。

見つけるのは、実行ファイルの名前が日報複合ツールの exe(`instance_guard.DESKTOP_EXES`)で、
**持ち主の無い**(確認の窓は大きなタブの窓が持ち主)、題名が「日報複合ツール」で始まる窓
(大きなタブの窓)だけ。題名は画面の題名に合わせて「日報複合ツール — 日報」のように変わる
(外枠が `on_document_title_changed` で写す)。別窓(印刷など。題名はツールの名前)にも送らない。
標準ライブラリ(ctypes)だけで書く。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

WM_CLOSE = 0x0010
GW_OWNER = 4
SW_RESTORE = 9
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _image_name(pid: int) -> str:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                               ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if not k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return Path(buf.value).name
    finally:
        k32.CloseHandle(handle)


def main_windows(exe_names: tuple[str, ...], title_prefix: str) -> Optional[list[int]]:
    """大きなタブの窓(見えているもの)。Windows でなければ None(確かめられない)。"""
    if os.name != "nt":
        return None
    return [hwnd for hwnd, title, owned, visible in exe_windows(exe_names)
            if visible and not owned and title.startswith(title_prefix)]


def exe_windows(exe_names: tuple[str, ...]) -> list[tuple[int, str, bool, bool]]:
    """日報複合ツールの exe の、題名のある窓すべて(窓, 題名, 持ち主がいるか, 見えているか)。Windows だけ。"""
    import ctypes
    from ctypes import wintypes

    user32 = _user32()
    wanted = {name.lower() for name in exe_names}
    found: list[tuple[int, str, bool, bool]] = []
    names: dict[int, str] = {}

    @_ENUM_PROC
    def visit(hwnd, _lparam):                     # noqa: ANN001
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in names:
            names[pid.value] = _image_name(pid.value).lower()
        if names[pid.value] in wanted:
            found.append((int(hwnd), buf.value, bool(user32.GetWindow(hwnd, GW_OWNER)),
                          bool(user32.IsWindowVisible(hwnd))))
        return True

    user32.EnumWindows(visit, 0)
    return found


_ENUM_PROC = None


def _user32():
    """user32 の関数。**引数の型を宣言する**(64 ビットでハンドルを int に縮めない)。"""
    global _ENUM_PROC
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    if _ENUM_PROC is None:
        _ENUM_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [_ENUM_PROC, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    for name in ("IsWindowVisible", "IsIconic", "SetForegroundWindow"):
        getattr(user32, name).argtypes = [wintypes.HWND]
        getattr(user32, name).restype = wintypes.BOOL
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    return user32


def ask_to_close(hwnds: list[int]) -> int:
    """窓に「閉じて」と頼む(× と同じ)。確認が見えるよう前に出す。頼めた数を返す。"""
    if os.name != "nt" or not hwnds:
        return 0
    user32 = _user32()
    asked = 0
    for hwnd in hwnds:
        try:
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, SW_RESTORE)
            user32.SetForegroundWindow(hwnd)      # 許されなければ何もしない(確認は窓の上に出る)
        except OSError:
            pass
        if user32.PostMessageW(hwnd, WM_CLOSE, 0, 0):
            asked += 1
    return asked
