"""画面URLとAPIの受付

Blueprint は**入力の形だけ**を見て、判断は presenters / repository へ渡す
(``docs/設計.md`` §1)。

``get_db`` をここに置いてあるのは、テストが1か所を差し替えるだけで
全ルートの接続を握れるようにするため(``tests/_web.py``)。
"""

from __future__ import annotations

from .. import get_db  # noqa: F401  (ルートはここから引く)

__all__ = ["get_db"]
