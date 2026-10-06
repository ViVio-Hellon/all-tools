"""用途名負荷係数算出マスタの参照 (VBA ``負荷係数辞書取得``)

伝送用ファイルの `用途名負荷係数算出` から **用途コード → 換算係数** を
引きます。コイル(丈=0)のラインでは、ロット1本を「1ロット」と数えずに
この係数で重み付けするので、係数処理ﾛｯﾄ数の出どころがここです。

【どの列を読むか】
実ファイルの列は12ありますが、VBA が実際に使っていたのは2つだけです:

    用途コード                    … 突き合わせる鍵(例 H283)
    PENAを1とした場合の換算係数    … 書き込む値(例 1.32257358137498)

`負荷係数` と `係数丸め` の列も同じ表にありますが、**VBA は読み込む
だけで使っていません**(`負荷C` を求めておいて一度も参照しない)。
ここでも使いません ── 使っていなかったものを勝手に採ると、出てくる
数字が現場の記録と合わなくなります。

読めなければ**空で返します**。係数が出ないだけで、日報の入力そのものは
止めません(VBA も `IsEmpty` のときは係数1で通していました)。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..config import SETTINGS
from ..logging_setup import get_logger
from ..logic.numeric import is_numeric, to_float
from .importer import import_table
from .runner import ScriptRunner

_logger = get_logger("access_bridge.factor_master")

#: 表の名前。伝送用ファイルの中にある(停止理由内訳・時間用と同居)
TABLE_NAME = "用途名負荷係数算出"

#: 鍵の列と、値の列。**VBA が見ていた2つだけ**
COLUMN_USAGE_CODE = "用途コード"
COLUMN_FACTOR = "PENAを1とした場合の換算係数"


def load_factors(
    master_path: Optional[Path] = None,
    runner: Optional[ScriptRunner] = None,
) -> dict[str, float]:
    """用途コード → 換算係数。**読めなければ空。**

    同じ用途コードが2行あるときは**先に出てきたほう**を採ります
    (VBA `If Not dic.Exists(key) Then dic.Add`)。
    """
    master_path = master_path or SETTINGS.transmission_master_path
    result = import_table(master_path, TABLE_NAME, runner=runner)
    if not result.success:
        _logger.warning("用途名負荷係数算出を読めませんでした error=%s", result.error)
        return {}

    factors: dict[str, float] = {}
    for row in result.rows:
        code = (row.get(COLUMN_USAGE_CODE) or "").strip()
        raw = (row.get(COLUMN_FACTOR) or "").strip()
        if not code or code in factors or not is_numeric(raw):
            continue
        factors[code] = to_float(raw)
    return factors
