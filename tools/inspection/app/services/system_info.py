"""既定のプリンター名など、Windows の情報取得（ctypes のみ使用）。"""
from __future__ import annotations

import os
from typing import Optional


def default_printer() -> Optional[str]:
    """Windows の既定のプリンター名。取得できない場合は None。

    Excel は新しく起動すると既定のプリンターへ印刷するため、画面に表示して確認できるようにする。
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        winspool = ctypes.WinDLL("winspool.drv")
        func = winspool.GetDefaultPrinterW
        func.argtypes = [wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        func.restype = wintypes.BOOL
        size = wintypes.DWORD(0)
        func(None, ctypes.byref(size))
        if size.value == 0:
            return None
        buf = ctypes.create_unicode_buffer(size.value)
        if func(buf, ctypes.byref(size)):
            return buf.value or None
    except Exception:  # noqa: BLE001
        return None
    return None
