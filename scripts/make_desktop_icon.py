#!/usr/bin/env python3
"""デスクトップ版(日報複合ツール.exe)のアイコンを作る

窓・タスクバー・exe に出るアイコン。濃い紺の角丸に、4つのツールの色の札を 2×2 に並べる
(日報=青・看板=橙・カレンダー=緑・点検表=赤茶。大きなタブの印と同じ色)。
文字を使わないので、フォントが要らない。作り直すときだけ流す(作ったものはリポジトリに入れてある)。

    python scripts/make_desktop_icon.py

要るもの: Pillow(作るときだけ。アプリの実行には要らない)。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "src-tauri" / "icons"
NAVY = (21, 50, 71, 255)
COLORS = ((38, 90, 160, 255), (180, 83, 9, 255), (45, 105, 64, 255), (146, 61, 35, 255))


def draw(size: int):
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    pen = ImageDraw.Draw(image)
    pen.rounded_rectangle((0, 0, size - 1, size - 1), radius=size // 5, fill=NAVY)
    pad = size * 0.16
    gap = size * 0.07
    cell = (size - 2 * pad - gap) / 2
    for i, color in enumerate(COLORS):
        x = pad + (i % 2) * (cell + gap)
        y = pad + (i // 2) * (cell + gap)
        pen.rounded_rectangle((x, y, x + cell, y + cell), radius=max(1, int(cell * 0.18)), fill=color)
    return image


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    big = draw(512)
    big.save(OUT / "icon.png")
    for size in (32, 128):
        draw(size).save(OUT / f"{size}x{size}.png")
    big.save(OUT / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"作りました: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
