"""Windows クリップボードの画像を PNG として取得する（ctypes のみ使用）。

VBA版 UFPreview.CreatePictureFromClipboard と同じく、
    1. 拡張メタファイル（CF_ENHMETAFILE, 高解像度）を優先
    2. 無ければビットマップ（CF_BITMAP）
を取得する。拡張メタファイルは拡大率を掛けて描画するため、文字が鮮明になる。

Excel の CopyPicture は「遅延レンダリング」でクリップボードへ登録することがあるため、
この処理は Excel がまだ起動している間（VBScript 側が待機している間）に呼ぶ。
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional, Tuple

from app.services.png_encoder import bgrx_to_rgb, encode_rgb

IS_WINDOWS = os.name == "nt"
CF_BITMAP = 2
CF_DIB = 8
CF_ENHMETAFILE = 14
DIB_RGB_COLORS = 0
WHITENESS = 0x00FF0062
MAX_TOTAL_PIXELS = 40_000_000


class ClipboardImageError(Exception):
    pass


class ClipboardOwnerError(ClipboardImageError):
    """クリップボードの中身が、こちらの Excel が置いたものではない。

    ほかのアプリ(同じ PC で別の作業をしている人の Ctrl+C など)が、
    コピーと読み取りのあいだに書き換えた。そのまま読むと**別のものを
    点検表のプレビューとして出してしまう**ので、読まずにコピーし直させる。
    """


if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

    class RECT(ctypes.Structure):
        _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                    ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

    class SIZEL(ctypes.Structure):
        _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]

    class ENHMETAHEADER(ctypes.Structure):
        _fields_ = [
            ("iType", wintypes.DWORD), ("nSize", wintypes.DWORD),
            ("rclBounds", RECT), ("rclFrame", RECT),
            ("dSignature", wintypes.DWORD), ("nVersion", wintypes.DWORD),
            ("nBytes", wintypes.DWORD), ("nRecords", wintypes.DWORD),
            ("nHandles", wintypes.WORD), ("sReserved", wintypes.WORD),
            ("nDescription", wintypes.DWORD), ("offDescription", wintypes.DWORD),
            ("nPalEntries", wintypes.DWORD), ("szlDevice", SIZEL), ("szlMillimeters", SIZEL),
            ("cbPixelFormat", wintypes.DWORD), ("offPixelFormat", wintypes.DWORD),
            ("bOpenGL", wintypes.DWORD), ("szlMicrometers", SIZEL),
        ]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

    class BITMAP(ctypes.Structure):
        _fields_ = [
            ("bmType", wintypes.LONG), ("bmWidth", wintypes.LONG), ("bmHeight", wintypes.LONG),
            ("bmWidthBytes", wintypes.LONG), ("bmPlanes", wintypes.WORD),
            ("bmBitsPixel", wintypes.WORD), ("bmBits", ctypes.c_void_p),
        ]

    def _sig(func: Any, argtypes: list, restype: Any) -> None:
        func.argtypes = argtypes
        func.restype = restype

    _sig(_user32.OpenClipboard, [wintypes.HWND], wintypes.BOOL)
    _sig(_user32.CloseClipboard, [], wintypes.BOOL)
    _sig(_user32.IsClipboardFormatAvailable, [wintypes.UINT], wintypes.BOOL)
    _sig(_user32.GetClipboardData, [wintypes.UINT], wintypes.HANDLE)
    _sig(_user32.GetClipboardSequenceNumber, [], wintypes.DWORD)
    _sig(_user32.GetClipboardOwner, [], wintypes.HWND)
    _sig(_user32.GetWindowThreadProcessId, [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD)
    _sig(_user32.GetDC, [wintypes.HWND], wintypes.HDC)
    _sig(_user32.ReleaseDC, [wintypes.HWND, wintypes.HDC], ctypes.c_int)
    _sig(_gdi32.CopyEnhMetaFileW, [wintypes.HANDLE, wintypes.LPCWSTR], wintypes.HANDLE)
    _sig(_gdi32.DeleteEnhMetaFile, [wintypes.HANDLE], wintypes.BOOL)
    _sig(_gdi32.GetEnhMetaFileHeader, [wintypes.HANDLE, wintypes.UINT, ctypes.c_void_p], wintypes.UINT)
    _sig(_gdi32.CreateCompatibleDC, [wintypes.HDC], wintypes.HDC)
    _sig(_gdi32.DeleteDC, [wintypes.HDC], wintypes.BOOL)
    _sig(_gdi32.CreateDIBSection, [wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD],
         wintypes.HBITMAP)
    _sig(_gdi32.SelectObject, [wintypes.HDC, wintypes.HGDIOBJ], wintypes.HGDIOBJ)
    _sig(_gdi32.DeleteObject, [wintypes.HGDIOBJ], wintypes.BOOL)
    _sig(_gdi32.PatBlt, [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         wintypes.DWORD], wintypes.BOOL)
    _sig(_gdi32.PlayEnhMetaFile, [wintypes.HDC, wintypes.HANDLE, ctypes.POINTER(RECT)], wintypes.BOOL)
    _sig(_gdi32.GdiFlush, [], wintypes.BOOL)
    _sig(_gdi32.GetObjectW, [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p], ctypes.c_int)
    _sig(_gdi32.GetDIBits, [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                            ctypes.POINTER(BITMAPINFO), wintypes.UINT], ctypes.c_int)

    def _bitmap_info(width: int, height: int) -> "BITMAPINFO":
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = width
        bmi.bmiHeader.biHeight = -height  # 上から下へ
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0  # BI_RGB
        return bmi

    def _open_clipboard(retries: int = 30, delay: float = 0.1) -> None:
        for _ in range(retries):
            if _user32.OpenClipboard(None):
                return
            time.sleep(delay)
        raise ClipboardImageError("クリップボードを開けませんでした（他のアプリが使用中の可能性があります）")

    def sequence_number() -> Optional[int]:
        return int(_user32.GetClipboardSequenceNumber())

    def owner_pid() -> Optional[int]:
        """いまクリップボードに置いたプロセス。分からなければ None。"""
        hwnd = _user32.GetClipboardOwner()
        if not hwnd:
            return None
        pid = wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value) or None

    def _fit(width: float, height: float, scale: float, max_pixels: int) -> Tuple[int, int]:
        w, h = width * scale, height * scale
        longest = max(w, h)
        if longest > max_pixels:
            ratio = max_pixels / longest
            w, h = w * ratio, h * ratio
        if w * h > MAX_TOTAL_PIXELS:
            ratio = (MAX_TOTAL_PIXELS / (w * h)) ** 0.5
            w, h = w * ratio, h * ratio
        return max(1, int(round(w))), max(1, int(round(h)))

    def _read_emf(scale: float, max_pixels: int) -> Tuple[bytes, Dict[str, Any]]:
        _open_clipboard()
        try:
            handle = _user32.GetClipboardData(CF_ENHMETAFILE)
            if not handle:
                raise ClipboardImageError("拡張メタファイルを取得できませんでした")
            hemf = _gdi32.CopyEnhMetaFileW(handle, None)  # クリップボード所有のハンドルは複製して使う
            if not hemf:
                raise ClipboardImageError("拡張メタファイルを複製できませんでした")
        finally:
            _user32.CloseClipboard()
        try:
            header = ENHMETAHEADER()
            _gdi32.GetEnhMetaFileHeader(hemf, ctypes.sizeof(header), ctypes.byref(header))
            frame = header.rclFrame  # 0.01mm 単位
            width_mm100 = frame.right - frame.left
            height_mm100 = frame.bottom - frame.top
            if width_mm100 > 0 and height_mm100 > 0:
                base_w, base_h = width_mm100 * 96.0 / 2540.0, height_mm100 * 96.0 / 2540.0
            else:
                bounds = header.rclBounds
                base_w, base_h = bounds.right - bounds.left + 1, bounds.bottom - bounds.top + 1
            if base_w <= 0 or base_h <= 0:
                raise ClipboardImageError("画像サイズを判定できませんでした")
            width, height = _fit(base_w, base_h, scale, max_pixels)
            data, complete = _render_emf(hemf, width, height)
        finally:
            _gdi32.DeleteEnhMetaFile(hemf)
        png = encode_rgb(width, height, bgrx_to_rgb(data, width, height))
        return png, {"format": "emf", "width": width, "height": height, "complete": complete}

    def _render_emf(hemf: Any, width: int, height: int) -> Tuple[bytes, bool]:
        screen_dc = _user32.GetDC(None)
        mem_dc = _gdi32.CreateCompatibleDC(screen_dc)
        bits = ctypes.c_void_p()
        bmi = _bitmap_info(width, height)
        hbmp = _gdi32.CreateDIBSection(screen_dc, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
        if not hbmp or not bits.value:
            _gdi32.DeleteDC(mem_dc)
            _user32.ReleaseDC(None, screen_dc)
            raise ClipboardImageError("描画用のメモリを確保できませんでした")
        old = _gdi32.SelectObject(mem_dc, hbmp)
        try:
            _gdi32.PatBlt(mem_dc, 0, 0, width, height, WHITENESS)
            rect = RECT(0, 0, width, height)
            complete = bool(_gdi32.PlayEnhMetaFile(mem_dc, hemf, ctypes.byref(rect)))
            _gdi32.GdiFlush()
            data = ctypes.string_at(bits.value, width * height * 4)
        finally:
            _gdi32.SelectObject(mem_dc, old)
            _gdi32.DeleteObject(hbmp)
            _gdi32.DeleteDC(mem_dc)
            _user32.ReleaseDC(None, screen_dc)
        return data, complete

    def _read_bitmap() -> Tuple[bytes, Dict[str, Any]]:
        _open_clipboard()
        try:
            hbmp = _user32.GetClipboardData(CF_BITMAP)
            if not hbmp:
                raise ClipboardImageError("ビットマップを取得できませんでした")
            bm = BITMAP()
            if not _gdi32.GetObjectW(hbmp, ctypes.sizeof(BITMAP), ctypes.byref(bm)):
                raise ClipboardImageError("ビットマップ情報を取得できませんでした")
            width, height = int(bm.bmWidth), abs(int(bm.bmHeight))
            if width <= 0 or height <= 0 or width * height > MAX_TOTAL_PIXELS:
                raise ClipboardImageError(f"ビットマップのサイズが不正です（{width}×{height}）")
            bmi = _bitmap_info(width, height)
            buf = ctypes.create_string_buffer(width * height * 4)
            screen_dc = _user32.GetDC(None)
            try:
                lines = _gdi32.GetDIBits(screen_dc, hbmp, 0, height, buf, ctypes.byref(bmi), DIB_RGB_COLORS)
            finally:
                _user32.ReleaseDC(None, screen_dc)
            if lines != height:
                raise ClipboardImageError("ビットマップの画素を読み取れませんでした")
            data = buf.raw
        finally:
            _user32.CloseClipboard()
        png = encode_rgb(width, height, bgrx_to_rgb(data, width, height))
        return png, {"format": "bitmap", "width": width, "height": height, "complete": True}

    def read_image_png(scale: float = 2.0, max_pixels: int = 3200, previous_sequence: Optional[int] = None,
                       retries: int = 15, delay: float = 0.2,
                       expected_owner: Optional[int] = None) -> Tuple[bytes, Dict[str, Any]]:
        """クリップボードの画像を PNG で返す。previous_sequence と同じ間は「まだコピーされていない」とみなす。

        `expected_owner` を渡すと、そのプロセス(こちらの Excel)が置いたものかを確かめる。
        """
        last_error: Optional[Exception] = None
        for _ in range(max(1, retries)):
            if previous_sequence is not None and sequence_number() == previous_sequence:
                last_error = ClipboardImageError("クリップボードが更新されていません（コピー失敗の可能性）")
                time.sleep(delay)
                continue
            if expected_owner:
                owner = owner_pid()
                if owner is not None and owner != expected_owner:
                    raise ClipboardOwnerError(
                        f"ほかのアプリがクリップボードを使いました(pid={owner})")
            has_emf = bool(_user32.IsClipboardFormatAvailable(CF_ENHMETAFILE))
            has_bmp = bool(_user32.IsClipboardFormatAvailable(CF_BITMAP)) or \
                bool(_user32.IsClipboardFormatAvailable(CF_DIB))
            if not has_emf and not has_bmp:
                last_error = ClipboardImageError("クリップボードに画像データがありません")
                time.sleep(delay)
                continue
            try:
                if has_emf:
                    return _read_emf(scale, max_pixels)
                return _read_bitmap()
            except ClipboardImageError as exc:
                last_error = exc
                time.sleep(delay)
        raise last_error or ClipboardImageError("クリップボードから画像を取得できませんでした")

else:
    def sequence_number() -> Optional[int]:
        return None

    def owner_pid() -> Optional[int]:
        return None

    def read_image_png(scale: float = 2.0, max_pixels: int = 3200, previous_sequence: Optional[int] = None,
                       retries: int = 15, delay: float = 0.2,
                       expected_owner: Optional[int] = None) -> Tuple[bytes, Dict[str, Any]]:
        raise ClipboardImageError("クリップボードからの画像取得は Windows でのみ使用できます")
