"""包装仕様の注意表記のマスタ (`梱包資材マスタ` の `注意_包装仕様`)

VBA ``PackagingSpecificationNo`` が読んでいた表です。包装仕様NO で
引いて、その番号に付いている注意(コメント)を拾います。

**判定はここでしません。** どの行を出すかは `logic/pack_note.py` が
決めます ── 範囲や部分一致の決まりを、ファイルを読む層に混ぜると、
テストのたびにファイルが要るようになります。

読むのは `梱包資材マスタ`(資材重量・VC重量・班員名簿と同じファイル)。
共有の上にあるので `source_db` の写しごしに読みます。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..config import SETTINGS
from ..logging_setup import get_logger
from ..logic.pack_note import NoteRow
from . import script_gen
from .importer import import_table
from .runner import ScriptRunner

_logger = get_logger("access_bridge.pack_note_master")

#: 表の名前。VBA ``tableName = "注意_包装仕様"``。
#: 実際に読む名前は `SETTINGS.pack_note_table`(設定が唯一の出どころ)
TABLE_NAME = "注意_包装仕様"

#: 列の名前 → `NoteRow` の属性。**VBA が `Select Case` で拾っていた列**
COLUMNS: dict[str, str] = {
    "包装仕様NO": "pack_spec_no",
    "コメント": "comment",
    "備考": "remark",
    "形状": "shape",
    "フラグ": "flag",
    "用途コード": "usage_code",
    "厚下": "thickness_low",
    "厚上": "thickness_high",
    "板幅下": "width_low",
    "板幅上": "width_high",
    "板丈下": "length_low",
    "板丈上": "length_high",
    "納入先": "delivery",
    "取引先": "customer",
}


def load_notes(pack_spec_no: str, master_path: Optional[Path] = None,
               runner: Optional[ScriptRunner] = None) -> list[NoteRow]:
    """その包装仕様NOの行を**全部**返す。無ければ空。

    同じ番号に複数行あるのがふつうです(実データで56種87行)。
    どれを出すかは呼び手(`logic/pack_note.build`)が決めます。

    **読めなくても例外にしません。** 注意が出ないのは困りますが、
    マスタが読めないことで日報の入力そのものが止まるほうが困ります
    ── 記録だけ残して空を返します。
    """
    number = (pack_spec_no or "").strip()
    if not number:
        return []

    path = master_path or SETTINGS.gw_material_master_path
    sql_filter = f"[包装仕様NO]={script_gen.sql_literal(number, 'TEXT')}"
    try:
        result = import_table(path, SETTINGS.pack_note_table,
                              sql_filter=sql_filter, runner=runner)
    except Exception as exc:                      # noqa: BLE001 - 入力は止めない
        _logger.exception("注意_包装仕様 を読めませんでした 包装仕様NO=%s", number)
        del exc
        return []

    if not result.success:
        _logger.warning("注意_包装仕様 の取得に失敗しました 包装仕様NO=%s error=%s",
                        number, result.error)
        return []

    return [NoteRow(**{attr: (row.get(column, "") or "").strip()
                       for column, attr in COLUMNS.items()})
            for row in result.rows]
