"""Access の数を**違う数にせずに**読む(単精度・十進型)

python-web-tools で、取り込みが数を丸めて違う数を DB へ書いていた。同じことが起きないか調べ、
読み取り部品で 2 つ見つかった:

- 単精度(Single): そのまま float にすると 2.9 が 2.9000000953674316 になり、その値で sqlite3 へ
  書いていた(Access では 2.9 と見えている)
- 十進型(Decimal): 小数点の位置を列の定義ではなく行の 2 バイト目から取り、整数も違う位置から
  読んでいたので、2971.5 が桁違いの数になっていた(Jackcess・access_parser の読み方に合わせた)
"""
from __future__ import annotations

import struct
import unittest

from calendar_app.accdb.reader import _decode_numeric, _decode_single


def single(value: float) -> bytes:
    return struct.pack("<f", value)


def decimal_bytes(integer: int, negative: bool = False) -> bytes:
    """十進型の 17 バイト: 符号 1 バイト + 4 バイトの整数 4 つ(大きい桁から、各々は小さい側から)。"""
    words = [(integer >> shift) & 0xFFFFFFFF for shift in (96, 64, 32, 0)]
    return struct.pack("<BIIII", 0x80 if negative else 0, *words)


class SingleTest(unittest.TestCase):
    def test_Accessが見せるのと同じ数(self) -> None:
        for value in (2.9, 2971.5, 0.1, 1528.0, 8.0, -3.75, 100.01725, 0.0):
            with self.subTest(value=value):
                self.assertEqual(_decode_single(single(value)), value)

    def test_単精度で表せない桁は単精度の値のまま(self) -> None:
        got = _decode_single(single(123456789.0))
        self.assertEqual(struct.pack("<f", got), single(123456789.0), "別の 4 バイトになった")


class DecimalTest(unittest.TestCase):
    def test_小数点の位置は列の定義から(self) -> None:
        self.assertEqual(_decode_numeric(decimal_bytes(29715), scale=1), 2971.5)
        self.assertEqual(_decode_numeric(decimal_bytes(100017250), scale=6), 100.01725)
        self.assertEqual(_decode_numeric(decimal_bytes(2971), scale=0), 2971)
        self.assertIsInstance(_decode_numeric(decimal_bytes(2971), scale=0), int)

    def test_負の数と大きな桁(self) -> None:
        self.assertEqual(_decode_numeric(decimal_bytes(375, negative=True), scale=2), -3.75)
        big = 12345678901234567890
        self.assertEqual(_decode_numeric(decimal_bytes(big), scale=0), big)

    def test_短すぎれば読まない(self) -> None:
        self.assertIsNone(_decode_numeric(b"\x00" * 16, scale=0))


if __name__ == "__main__":
    unittest.main()
