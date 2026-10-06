"""前の直に一切の入力が無いときの警告

【なぜ要るのか ── 保存忘れは、タイマーでは防ぎきれない】
直の終わりの催促(`logic/shift.py`)も、残り5分の自動確定
(`services/shift_close.py`)も、**画面が開いていなければ動きません**。
VBA も同じで、フォームを閉じていた直はタイマーが一度も回りませんでした。
しかも「打った人が保存し忘れた」だけでなく「そもそも一行も打たなかった」
場合は、催促する相手すら居ません。

つまりタイマーは**その直の中でしか効かない**。抜けたぶんは、次の直が
気づくしかありません ── そこでこの警告です:

    **前の直に1ページも保存が無ければ、次の直の人に一度だけ知らせる。**

直っているわけではありません(利用者の言うとおり「分かったところで」
です)。ただ、**気づく人が居る時点**まで持ち込めます ── その日のうち
なら、前の直の人に聞いて入れ直せます。翌月の集計で気づくのとは違います。

【一度だけ、静かに】
出すのは1回(`WarnGate` がプロセスで覚えます)。毎回出す作りにすると、
**読まずに閉じる癖**がついて、本当に抜けた日にも気づけません。音も
鳴らしません ── これは「いま何かしろ」ではなく「そういえば」の話です。

【空振りさせない】
そのラインが**そもそもその直を回していない**なら、無いのが普通です。
毎朝「3直が空です」と出すのは、狼少年にしかなりません。だから
**最近その直で保存された実績があるときだけ**出します
(`seen_recently`)。判断に使う事実は呼び出し側が集めます。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from ..logging_setup import get_logger
from . import line_names

log = get_logger("prev_shift")

#: いまの直 → (その前の直, 報告日のずれ(日))
#:
#: 3直は 22:00→翌08:00 で、報告日は**始めた日**(`TodayCheck`)。なので
#: 1直から見た前の直は「昨日の3直」になります。日勤(08:00-18:00)は
#: 1直・2直の代わりに置く直なので、その前も昨日の3直です。
PREVIOUS: dict[str, tuple[str, int]] = {
    "1直": ("3直", -1),
    "2直": ("1直", 0),
    "3直": ("2直", 0),
    "日勤": ("3直", -1),
}

#: 「そのラインはその直を回している」と見なす遡り(日)
SEEN_WITHIN_DAYS = 14


@dataclass(frozen=True)
class EmptyPrevShift:
    """前の直が空かどうかと、その理由。**画面はこれを写すだけ。**"""

    warn: bool = False
    #: 前の直 (報告日, 直)
    report_date: str = ""
    shift: str = ""
    line: str = ""
    message: str = ""
    reason: str = ""

    def as_dict(self) -> dict:
        return {"warn": self.warn, "report_date": self.report_date,
                "shift": self.shift, "line": self.line,
                "message": self.message, "reason": self.reason}


class WarnGate:
    """「もう知らせたか」を覚える。

    鍵は (報告日, ライン, 直) ── **いまの直**のほうです。直が変われば
    別の鍵になるので、自然に忘れます。
    """

    def __init__(self) -> None:
        self._done: set[tuple[str, str, str]] = set()
        self._lock = threading.Lock()

    def mark(self, report_date: str, line: str, shift: str) -> None:
        with self._lock:
            self._done.add((report_date, line, shift))
        log.info("前直の警告を知らせ済みにしました: %s %s %s",
                 report_date, line, shift)

    def told(self, report_date: str, line: str, shift: str) -> bool:
        with self._lock:
            return (report_date, line, shift) in self._done

    def forget(self) -> None:
        with self._lock:
            self._done.clear()


_gate = WarnGate()


def gate() -> WarnGate:
    return _gate


def reset() -> None:
    """テスト用。"""
    _gate.forget()


def previous_of(shift: str, business_date: date
                ) -> Optional[tuple[str, date]]:
    """その直の1つ前は、どの日のどの直か。知らない直なら None。"""
    found = PREVIOUS.get(shift)
    if found is None:
        return None
    prev_shift, day_offset = found
    return prev_shift, business_date + timedelta(days=day_offset)


def seen_since(business_date: date, days: int = SEEN_WITHIN_DAYS) -> date:
    """「最近」の境目。"""
    return business_date - timedelta(days=days)


def evaluate(*, report_date: str, line: str, shift: str,
             prev_report_date: str, prev_shift: str,
             prev_has_data: bool, seen_recently: bool,
             recall_mode: bool = False) -> EmptyPrevShift:
    """前の直の空を知らせるべきか。

    黙る場面が4つあります。どれも**出すほうが害になる**もの:

    1. **もう知らせた。** 毎回出すと読まずに閉じる癖がつく
    2. **呼出モード中。** 過去のデータを直している最中なので、
       「前の直」の話は当てはまらない
    3. **前の直に保存がある。** 知らせることが無い
    4. **そのラインはその直を回していない。** 無いのが普通なので、
       毎回出せば狼少年になる
    """
    base = EmptyPrevShift(report_date=prev_report_date, shift=prev_shift,
                          line=line)

    if _gate.told(report_date, line, shift):
        return base
    if recall_mode:
        return base
    if prev_has_data:
        return base
    if not seen_recently:
        return base

    # 文言はそのまま画面に出ます。**飾り記号を入れない** ── 太字に
    # したいところは画面側(`entry.html`)が要素で分けています
    message = (f"前の直({prev_report_date} {prev_shift} / {line_names.label(line)})に、"
               "保存された日報が1ページもありません。")
    # 前は「その直を呼び出せば保存できます」と案内していましたが、呼び出しは
    # 保存のある直しか開けません ── **丸ごと抜けた直**では行き止まりでした。
    # 抜けた直を作るのは「この直を後から作る」(v4.0.0 `logic/backfill.py`)
    reason = ("打っていない直だったのなら、そのままで結構です。"
              "入れ忘れ・保存し忘れなら、後から作れます ── 「記録を見る」の"
              "「日付を指定して見る」でその直を選び、「この直を後から作る」を"
              "押してください(管理者モード)。「後日作成」の印が残ります。")
    return EmptyPrevShift(warn=True, report_date=prev_report_date,
                          shift=prev_shift, line=line,
                          message=message, reason=reason)
