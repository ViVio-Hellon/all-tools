"""直の時刻を、伝送用ファイルの「時間用」から手元へ写す ── **写す道は1本**

直の時刻は上流(伝送用ファイル)が持っている値ですが、画面は毎回それを
読みに行かず、手元のDB(`shift_config`)に写したものを見ます。写すのは
次の3か所で、どれもここを通します:

    起動のとき              `start_app._read_shift_times`
    「時間マスタを取り込む」  `/api/settings/sync-shift`
    マスタ管理で時間用を直したとき  `master_admin.after_write`

3つ目が無かったころは、マスタ管理で直しても**起動し直すか取り込むまで
効きませんでした。** 直した人は「直したのに変わらない」を見て、もう一度
直しに行きます。

写し方は**揃える**です(`NippouRepository.replace_shift_times`)。
マスタから消えた直は手元からも消します。読めなかったとき(共有が落ちて
いる・表が空)は手元に触りません ── 前回読めた値のまま動き、帯が
「読めていません」と言い続けます。
"""
from __future__ import annotations

from typing import Optional

from ..access_bridge import importer
from ..config import SETTINGS
from ..db.repository import NippouRepository
from ..logging_setup import get_logger

log = get_logger("services.shift_times")


def reload(repo: NippouRepository) -> Optional[dict[str, tuple[str, str]]]:
    """マスタを読み、手元をそれに揃える。読めた中身を返す。

    **読めなければ None**(空の表も読めなかった扱い。手元は触らない)。
    予期しない例外は呼び手へ上げます ── 呼び手ごとに「起動は続ける」
    「500で返す」と扱いが違うので。
    """
    times = importer.import_shift_times(
        SETTINGS.transmission_master_path, table_name=SETTINGS.shift_time_table)
    if not times:
        log.warning("直の時間をマスタから読めませんでした: %s",
                    SETTINGS.transmission_master_path)
        return None
    repo.replace_shift_times(times)
    log.info("直の時間をマスタから写しました: %d件", len(times))
    return times


def describe(times: dict[str, tuple[str, str]]) -> str:
    """「1直 07:00〜15:00 / 2直 15:00〜22:50 …」。**直の順に並べる。**"""
    from ..logic.shift import FORM_KEYS

    names = dict(FORM_KEYS)                       # 鍵 → 直の名前(logic/shift の 1 か所)
    order = [k for k in names if k in times]
    order += sorted(k for k in times if k not in names)
    return " / ".join(
        f"{names.get(k, k)} {times[k][0] or '(空)'}〜{times[k][1] or '(空)'}"
        for k in order)
