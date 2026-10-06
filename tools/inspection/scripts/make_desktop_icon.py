#!/usr/bin/env python3
"""デスクトップ版(Tauri)のアイコンを作る

窓・タスクバー・exe に出るアイコン。帯と同じ濃紺〜ティールの角丸に白の「点」。
作り直すときだけ流す(作ったものはリポジトリに入れてある)。

    python scripts/make_desktop_icon.py

要るもの: Pillow と日本語のフォント(既定は IPAゴシック。`--font` で指定)。
**アプリの動作には要らない**(ラインPCに Pillow を入れる必要は無い)。
"""
from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "src-tauri" / "icons"
TOP = (21, 50, 71)       # 帯の濃紺(tokens.css --bar-a)
BOTTOM = (30, 96, 121)   # 帯のティール(--bar-b)


def draw(size: int, font_path: str):
    from PIL import Image, ImageDraw, ImageFont

    fill = Image.new("RGBA", (size, size))
    for y in range(size):
        t = y / max(1, size - 1)
        color = tuple(int(a + (b - a) * t) for a, b in zip(TOP, BOTTOM)) + (255,)
        ImageDraw.Draw(fill).line([(0, y), (size, y)], fill=color)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=size // 6, fill=255)
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    image.paste(fill, (0, 0), mask)
    font = ImageFont.truetype(font_path, int(size * 0.66))
    ImageDraw.Draw(image).text((size / 2, size / 2), "点", font=font, fill="white", anchor="mm")
    return image


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", default="/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    big = draw(512, args.font)
    big.save(OUT / "icon.png")
    for size in (32, 128):
        draw(size, args.font).save(OUT / f"{size}x{size}.png")
    big.save(OUT / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"作りました: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
