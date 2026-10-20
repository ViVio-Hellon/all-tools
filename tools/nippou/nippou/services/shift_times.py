"""直の時刻を、伝送用ファイルの「時間用」から手元へ写す ── **写す道は1本**

直の時刻は上流(伝送用ファイル)が持っている値ですが、画面は毎回それを
読みに行かず、手元のDB(`shift_config`)に写したものを見ます。写すのは
次の3か所で、どれもここを通します:

    起動のとき              `start_app._read_shift_times`
    「時間マスタを取り込む」  `/api/settings/sync-shift`
    マスタ管理で時間用を直したとき  `master_admin.after_write`
    **ほかのPCがマスタを直したとき**  `refresh_if_changed`(v4.39.1)

3つ目が無かったころは、マスタ管理で直しても**起動し直すか取り込むまで
効きませんでした。** 直した人は「直したのに変わらない」を見て、もう一度
直しに行きます。

写し方は**揃える**です(`NippouRepository.replace_shift_times`)。
マスタから消えた直は手元からも消します。読めなかったとき(共有が落ちて
いる・表が空)は手元に触りません ── 前回読めた値のまま動き、帯が
「読めていません」と言い続けます。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Optional

from ..access_bridge import importer
from ..config import SETTINGS
from ..db.repository import NippouRepository
from ..logging_setup import get_logger

log = get_logger("services.shift_times")


#: マスタのファイルを最後に読んだときの (更新時刻, 大きさ)。ファイルごと
_seen: dict[str, tuple[int, int]] = {}
#: 変わったかを見るのは、これより間を空けて(秒)。画面の1分の見張りで足りる
CHECK_EVERY = 30.0
#: 最後に見に行ったとき(`time.monotonic`)。**間を空けるかの判断は設定もファイルも読まずに**
#: (v4.39.2。前は毎回の要求で設定ファイルを読み、マスタの場所を確かめてから間を見ていた)
_last_check: Optional[float] = None
_lock = threading.Lock()


def _stamp(path: Path) -> Optional[tuple[int, int]]:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def reset() -> None:
    """覚えている更新時刻を忘れる(試験用)。"""
    global _last_check
    with _lock:
        _seen.clear()
        _last_check = None


def refresh_if_changed(repo, *, now: Optional[float] = None,
                       guard=None) -> Optional[dict[str, tuple[str, str]]]:
    """**ほかのPCがマスタの時間用を直したら、この端末にも効かせる**(v4.39.1)。

        直の開始時間終了時間をマスタで修正後即座に反映するかのチェックをしてください

    マスタ管理で直したPCは、その場で読み直します(`master_admin.after_write`)。
    ところがほかのPCは**起動し直すか「時間マスタを取り込む」を押すまで前の時刻のまま**
    でした ── 1直の終わりを 15:30 に延ばしても、ほかのPCは 15:00 で「見るだけ」に
    なり、催促も自動確定も最終時間の確かめも 15:00 で動きます。

    画面の要求のついでに(`app._register_shift_master_refresh`。30秒に1回まで)、
    マスタのファイルの更新時刻と大きさを見て、**前に読んだときと違えば読み直します**。
    見るのはファイルの属性だけなので、変わっていなければ中身は読みません。
    読み直したら中身を、変わっていない・見なかったときは None を返します。

    `guard` は手元のDBへ写すあいだ持つロック(書き込みのロック)。**少しだけ待って取れ
    なければ今回は見送り**、次の要求でまた見ます(共有へ保存のような長い書き込みの
    あいだ、画面の要求を待たせない)。

    `repo` は手元のDB、または**それを返す関数**(読み直すときだけ開く。v4.39.2)。
    """
    global _last_check
    t = time.monotonic() if now is None else now
    with _lock:
        if _last_check is not None and t - _last_check < CHECK_EVERY:
            return None
        _last_check = t
    path = SETTINGS.transmission_master_path
    key = str(path)
    stamp = _stamp(path)
    if stamp is None or _seen.get(key) == stamp:
        return None
    if guard is not None and not guard.acquire(timeout=0.5):
        with _lock:
            _last_check = None                    # 次の要求でもう一度
        return None
    try:
        times = reload(repo() if callable(repo) else repo)
    finally:
        if guard is not None:
            guard.release()
    # 読めなかったときも覚える(壊れたファイルを30秒ごとに読みに行かない。次に変わったら読む)
    with _lock:
        _seen[key] = stamp
    if times is not None:
        log.info("マスタの時間用が変わっていたので読み直しました: %s", describe(times))
    return times


def reload(repo: NippouRepository) -> Optional[dict[str, tuple[str, str]]]:
    """マスタを読み、手元をそれに揃える。読めた中身を返す。

    **読めなければ None**(空の表も読めなかった扱い。手元は触らない)。
    予期しない例外は呼び手へ上げます ── 呼び手ごとに「起動は続ける」
    「500で返す」と扱いが違うので。
    """
    path = SETTINGS.transmission_master_path
    stamp = _stamp(path)
    times = importer.import_shift_times(
        path, table_name=SETTINGS.shift_time_table)
    # 読んだときの更新時刻を覚える(`refresh_if_changed` が同じものを読み直さない)
    if stamp is not None:
        with _lock:
            _seen[str(path)] = stamp
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
