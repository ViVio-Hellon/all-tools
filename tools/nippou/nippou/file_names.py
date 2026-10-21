"""書き出すファイル・フォルダの名前に使えない字を除く(1か所だけ)。

前は同じ字の並びが3つのモジュールにあり、除いて空なら何にするかも呼ぶ所ごとに
書いていました。並びを直すときにほかの所だけ古いまま残らないよう、ここに置きます。
"""
from __future__ import annotations

import re
from typing import Optional

#: Windows のファイル名に使えない字
UNSAFE_CHARS = '\\/:*?"<>|'

#: 使えない字と空白の続き(1つの `_` にまとめる所が使う)
UNSAFE_OR_SPACE = re.compile("[" + re.escape(UNSAFE_CHARS) + r"\s]+")


def safe_name(text: Optional[str], default: str = "Unknown",
              limit: Optional[int] = None) -> str:
    """使えない字を除き、前後の空白を落とす。`limit` で長さを切る。空なら `default`。"""
    cleaned = "".join(ch for ch in (text or "") if ch not in UNSAFE_CHARS).strip()
    if limit is not None:
        cleaned = cleaned[:limit]
    return cleaned or default
