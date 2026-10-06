"""全停入力（VBAの ``全停入力()``、標準モジュール~5273行）のコアロジック。

「直全体を通して設備が完全停止していた」ことを表す特殊な一括入力 ---
日報の1行目(row1)だけを使って、その直の開始〜終了時刻・停止理由・
実働分をまとめて設定する。``GetAdjustedMinutesDiff``（~16493行）による
実働分計算（日跨ぎ対応）を純粋関数として移植した。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time


@dataclass(frozen=True)
class AllStopEntry:
    """全停時に日報1行目(row1)へ書き込む内容。"""

    kz: str  # 開始時
    kh: str  # 開始分
    sz: str  # 終了時
    sh: str  # 終了分
    s: str  # 作業停止①の**記号**(内訳番号)。名前ではない
    th: str  # 実働分(TH1)


def working_minutes(kz: str, kh: str, sz: str, sh: str, subtract_minutes: int = 0) -> int:
    """port of ``GetAdjustedMinutesDiff``: 開始/終了時刻(時分)から実働分を
    計算する。終了 < 開始 の場合は日跨ぎとみなし +1日する
    （3直が夜〜翌朝にまたがるケース向け）。不正な入力なら VBA と同じく
    -1 を返す。
    """
    try:
        start = time(int(kz), int(kh))
        end = time(int(sz), int(sh))
    except (ValueError, TypeError):
        return -1

    start_minutes = start.hour * 60 + start.minute
    end_minutes = end.hour * 60 + end.minute
    if end_minutes < start_minutes:
        end_minutes += 24 * 60  # 跨日対応

    return end_minutes - start_minutes - subtract_minutes


def build_all_stop_entry(shift_start: time, shift_end: time,
                        reason_code: str) -> AllStopEntry:
    """port of ``全停入力()`` の row1 組み立て部分。直の開始・終了時刻
    (``ShiftCalculator.current_shift_start_time``/``_end_time``)と選んだ
    停止理由の**記号**から、日報1行目に書き込む値一式を作る。

    【**記号**であって、名前ではありません】
    ここには長らく「ﾌｫｰｸ待ち」のような**名前**が入っていました。作業停止①の
    欄が持つのは内訳マスタの**内訳番号**(「ニ」など)で、名前ではありません。

    名前を書くと、こうなります:

        1. 保存前チェックが「停止内訳にない記号です」と断る
        2. **そのページは以後どうやっても保存できない** ── 消そうにも、
           消した形を保存するところで同じ断りに当たる
        3. 全停は直に1回きりなので、やり直しもできない

    実際にそうなりました。**記号を書けば、この3つとも起きません。**
    """
    kz = f"{shift_start.hour:02d}"
    kh = f"{shift_start.minute:02d}"
    sz = f"{shift_end.hour:02d}"
    sh = f"{shift_end.minute:02d}"
    th = str(working_minutes(kz, kh, sz, sh))
    return AllStopEntry(kz=kz, kh=kh, sz=sz, sh=sh, s=reason_code, th=th)
