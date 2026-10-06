"""人員フォーム(UF登録人員)用の班員名簿マスタ参照。

VBAの ``人員()``（標準モジュール~4758行）が
``PATH_AIM_参照 & "梱包資材マスタ.accdb"`` の「班員名簿」テーブルから
読み込んでいた処理を移植したもの。GW計算フォームの資材重量/VC重量マスタ
と同じAccessファイルに同居している（提出データで確認済み: 列は
管理番号/苗字/班/名前/読み/担当ライン）。参照専用（読み取りのみ）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..config import SETTINGS
from ..logging_setup import get_logger
from ..logic.staff import StaffMember
from .importer import import_table
from .runner import ScriptRunner

_logger = get_logger("access_bridge.staff_master")


def load_staff_members(
    master_path: Optional[Path] = None,
    runner: Optional[ScriptRunner] = None,
) -> list[StaffMember]:
    """port of ``人員()`` の名簿読み込み部分。"""
    master_path = master_path or SETTINGS.gw_material_master_path
    result = import_table(master_path, SETTINGS.staff_master_table, runner=runner)
    if not result.success:
        _logger.warning("班員名簿の取得に失敗しました error=%s", result.error)
        return []

    members: list[StaffMember] = []
    for row in result.rows:
        name = row.get("名前", "").strip()
        if not name:
            continue
        members.append(
            StaffMember(name=name, team=row.get("班", "").strip(), reading=row.get("読み", "").strip(),
                        # 現場が足した列(v4.15.0)。無い名簿では空のまま
                        operator=row.get(SETTINGS.staff_operator_column, "").strip())
        )
    return members
