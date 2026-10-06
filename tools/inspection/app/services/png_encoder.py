"""PNG 書き出し（標準ライブラリの zlib / struct のみ使用）。"""
from __future__ import annotations

import struct
import zlib

_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def encode_rgb(width: int, height: int, rgb: bytes, level: int = 6) -> bytes:
    """RGB 24bit（上の行から順）の画素列を PNG にする。"""
    if width <= 0 or height <= 0:
        raise ValueError("画像サイズが不正です")
    stride = width * 3
    if len(rgb) < stride * height:
        raise ValueError("画素データが不足しています")
    view = memoryview(rgb)
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # フィルタなし
        raw += view[y * stride:(y + 1) * stride]
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return _SIGNATURE + _chunk(b"IHDR", header) + _chunk(b"IDAT", zlib.compress(bytes(raw), level)) \
        + _chunk(b"IEND", b"")


def bgrx_to_rgb(data: bytes, width: int, height: int) -> bytes:
    """Windows の 32bit DIB（B,G,R,X の順）を RGB に並べ替える。"""
    count = width * height
    src = memoryview(data)[:count * 4]
    rgb = bytearray(count * 3)
    rgb[0::3] = src[2::4]
    rgb[1::3] = src[1::4]
    rgb[2::3] = src[0::4]
    return bytes(rgb)


def read_png_size(data: bytes) -> tuple:
    if data[:8] != _SIGNATURE:
        raise ValueError("PNG ではありません")
    return struct.unpack(">II", data[16:24])
