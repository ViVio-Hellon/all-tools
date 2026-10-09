"""看板の状態の列と「動いている看板か」の決まり。**ここ 1 か所で決める。**

状態の列(欲・不・更新日・発送・倉庫確認日時・保留・注文中日時)は、各端末が看板画面の
ボタンで共有DBへ書く。Access には届かない。以前はこの一覧と「発注中・発送済み・注文中か」の
判定が、マスタ管理・中身の入れ替え・画面(JS)に別々に書かれていて、1 か所だけ直すと
食い違った(状態の列を 1 つ足すのに約 12 か所を直すことになっていた)。

- :data:`STATE_COLUMNS` … 状態の列(共有DBでの列名)
- :data:`INITIAL_STATE` … 新しい看板の状態(まだ発注していない = ``不 = 〇``)
- :data:`ACTIVE_MARKS` … 動いている看板の印 ``(列, 呼び名)``。画面(JS)にも渡す
"""
from __future__ import annotations

from typing import Any, Mapping

from .. import config

STATE_COLUMNS: tuple[str, ...] = (
    config.COL_WANT,
    config.COL_UNWANT,
    config.COL_ORDERED_AT,
    config.COL_SHIPPED,
    config.COL_CONFIRMED_AT,
    config.COL_HOLD,
    config.COL_HOLD_AT,
)

#: 新しい看板の状態。**選ばせずに、これで固定する**(看板画面で発注を取り消したあとと同じ形)
INITIAL_STATE: dict[str, str] = {c: "" for c in STATE_COLUMNS} | {config.COL_UNWANT: config.MARK_ON}

#: 動いている看板の印 ``(列, 呼び名, 色)``。消すと動いている発注を見失う
ACTIVE_MARKS: tuple[tuple[str, str, str], ...] = (
    (config.COL_WANT, "発注中", "赤"),
    (config.COL_SHIPPED, "発送済み", "緑"),
    (config.COL_HOLD, "注文中", "黄"),
)
ACTIVE_COLUMNS: tuple[str, ...] = tuple(col for col, _, _ in ACTIVE_MARKS)


def _on(row: Mapping[str, Any], column: str) -> bool:
    return str(row.get(column) or "").strip() == config.MARK_ON


def active_labels(row: Mapping[str, Any], *, with_color: bool = False) -> list[str]:
    """その看板が動いている理由(``["発注中", "注文中"]``)。動いていなければ空。"""
    return [f"{name}({color})" if with_color else name
            for col, name, color in ACTIVE_MARKS if _on(row, col)]


def is_active(row: Mapping[str, Any]) -> bool:
    """発注中・発送済み・注文中のどれか。"""
    return any(_on(row, col) for col in ACTIVE_COLUMNS)


def for_screen() -> dict[str, Any]:
    """画面(JS)へ渡す形。JS に列名と印を書き写さない。"""
    return {"mark_on": config.MARK_ON,
            "active": [{"column": col, "label": name} for col, name, _ in ACTIVE_MARKS]}
