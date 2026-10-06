"""日本の祝日判定 (VBA の modHoliday / ktHolidayName の移植)。

元ロジックの出典と著作権表示 (原文のまま引用) ::

    '_/  --- VB / VBA 版 ( Update: 2018/12/8 ) ---
    '_/  CopyRight(C) K.Tsunoda(AddinBox) 2001 All Rights Reserved.
    '_/  ( AddinBox  http://addinbox.sakura.ne.jp/index.htm )
    '_/  この祝日マクロは『kt関数アドイン』で使用しているものです。
    '_/  (*1)このマクロを引用するに当たっては、必ずこのコメントも
    '_/      一緒に引用する事とします。
    '_/  (*2)他サイト上で本マクロを直接引用する事は、ご遠慮願います。
    '_/      【 http://addinbox.sakura.ne.jp/holiday_logic.htm 】
    '_/      へのリンクによる紹介で対応して下さい。

VBA からの移植にあたり、判定条件・年代分岐はそのまま維持している。
"""

from __future__ import annotations

import datetime as _dt

__all__ = ["holiday_name", "is_holiday"]

# VBA の Weekday(): 1=日曜 ... 7=土曜
_SUNDAY, _MONDAY, _TUESDAY, _WEDNESDAY = 1, 2, 3, 4
_SATURDAY = 7

_HOLIDAY_LAW_START = _dt.date(1948, 7, 20)  # 祝日法施行
_SUBSTITUTE_START = _dt.date(1973, 4, 12)  # 振替休日施行日

_SHOWA_TAISO = _dt.date(1989, 2, 24)  # 昭和天皇の大喪の礼
_AKIHITO_WEDDING = _dt.date(1959, 4, 10)  # 皇太子明仁親王の結婚の儀
_NARUHITO_WEDDING = _dt.date(1993, 6, 9)  # 皇太子徳仁親王の結婚の儀
_SOKUI_HEISEI = _dt.date(1990, 11, 12)  # 即位礼正殿の儀 (平成天皇)
_HEISEI_ABDICATION = _dt.date(2019, 4, 30)  # 平成天皇の退位 (国民の休日)
_NARUHITO_ENTHRONE = _dt.date(2019, 5, 1)  # 徳仁親王の即位
_GW2019_KOKUMIN = _dt.date(2019, 5, 2)  # 2019 GW の国民の休日
_SOKUI_REIWA = _dt.date(2019, 10, 22)  # 即位礼正殿の儀 (徳仁親王)


def _vb_weekday(d: _dt.date) -> int:
    """VBA の Weekday() と同じ 1(日)〜7(土) を返す。"""
    return (d.weekday() + 1) % 7 + 1


def _nth_weekday(d: _dt.date) -> str:
    """VBA の ``(((int日 - 1) \\ 7) + 1) & Weekday(日付)`` 相当の文字列を返す。"""
    return f"{(d.day - 1) // 7 + 1}{_vb_weekday(d)}"


def _vernal_equinox_day(year: int) -> int:
    """春分日の略算 (『新こよみ便利帳』の式)。範囲外は 99。"""
    if year <= 1947:
        return 99
    if year <= 1979:
        return int(20.8357 + 0.242194 * (year - 1980) - _fix((year - 1983) / 4))
    if year <= 2099:
        return int(20.8431 + 0.242194 * (year - 1980) - _fix((year - 1980) / 4))
    if year <= 2150:
        return int(21.8510 + 0.242194 * (year - 1980) - _fix((year - 1980) / 4))
    return 99


def _autumnal_equinox_day(year: int) -> int:
    """秋分日の略算。範囲外は 99。"""
    if year <= 1947:
        return 99
    if year <= 1979:
        return int(23.2588 + 0.242194 * (year - 1980) - _fix((year - 1983) / 4))
    if year <= 2099:
        return int(23.2488 + 0.242194 * (year - 1980) - _fix((year - 1980) / 4))
    if year <= 2150:
        return int(24.2488 + 0.242194 * (year - 1980) - _fix((year - 1980) / 4))
    return 99


def _fix(value: float) -> int:
    """VBA の Fix() (0 方向への切り捨て) と同じ挙動。"""
    return int(value)


def _base_holiday(d: _dt.date) -> str:
    """振替休日を考慮しない、その日単体の祝日名を返す (VBA: prv祝日)。"""
    if d < _HOLIDAY_LAW_START:
        return ""

    y, m, day = d.year, d.month, d.day

    # -- 1月 --
    if m == 1:
        if day == 1:
            return "元日"
        if y >= 2000:
            if _nth_weekday(d) == "22":  # 第2月曜
                return "成人の日"
        elif day == 15:
            return "成人の日"
        return ""

    # -- 2月 --
    if m == 2:
        if day == 11:
            return "建国記念の日" if y >= 1967 else ""
        if day == 23:
            return "天皇誕生日" if y >= 2020 else ""
        if d == _SHOWA_TAISO:
            return "昭和天皇の大喪の礼"
        return ""

    # -- 3月 --
    if m == 3:
        return "春分の日" if day == _vernal_equinox_day(y) else ""

    # -- 4月 --
    if m == 4:
        if day == 29:
            if y >= 2007:
                return "昭和の日"
            if y >= 1989:
                return "みどりの日"
            return "天皇誕生日"  # 昭和天皇
        if d == _HEISEI_ABDICATION:
            return "国民の休日"
        if d == _AKIHITO_WEDDING:
            return "皇太子明仁親王の結婚の儀"
        return ""

    # -- 5月 --
    if m == 5:
        if day == 3:
            return "憲法記念日"
        if day == 4:
            if y >= 2007:
                return "みどりの日"
            if y >= 1986:
                # 5/4 が日曜は『只の日曜』、月曜は『憲法記念日の振替休日』(〜2006年)
                if _vb_weekday(d) > _MONDAY:
                    return "国民の休日"
            return ""
        if day == 5:
            return "こどもの日"
        if day == 6:
            if y >= 2007 and _vb_weekday(d) in (_TUESDAY, _WEDNESDAY):
                return "振替休日"  # [5/3, 5/4 が日曜]のケースのみここで判定
            return ""
        if y == 2019:
            if d == _NARUHITO_ENTHRONE:
                return "即位の日"
            if d == _GW2019_KOKUMIN:
                return "国民の休日"
        return ""

    # -- 6月 --
    if m == 6:
        return "皇太子徳仁親王の結婚の儀" if d == _NARUHITO_WEDDING else ""

    # -- 7月 --
    if m == 7:
        if y >= 2021:
            return "海の日" if _nth_weekday(d) == "32" else ""
        if y == 2020:
            # 五輪特措法により「海の日」7/23 /「スポーツの日」7/24 へ移動
            if day == 23:
                return "海の日"
            if day == 24:
                return "スポーツの日"
            return ""
        if y >= 2003:
            return "海の日" if _nth_weekday(d) == "32" else ""
        if y >= 1996:
            return "海の日" if day == 20 else ""
        return ""

    # -- 8月 --
    if m == 8:
        if y >= 2021:
            return "山の日" if day == 11 else ""
        if y == 2020:
            return "山の日" if day == 10 else ""  # 五輪特措法により移動
        if y >= 2016:
            return "山の日" if day == 11 else ""
        return ""

    # -- 9月 --
    if m == 9:
        autumn = _autumnal_equinox_day(y)
        if day == autumn:
            return "秋分の日"
        if y >= 2003:
            if _nth_weekday(d) == "32":  # 第3月曜
                return "敬老の日"
            if _vb_weekday(d) == _TUESDAY and day == autumn - 1:
                return "国民の休日"  # 火曜日 かつ 秋分日の前日
            return ""
        if y >= 1966:
            return "敬老の日" if day == 15 else ""
        return ""

    # -- 10月 --
    if m == 10:
        if y >= 2021:
            return "スポーツの日" if _nth_weekday(d) == "22" else ""
        if y == 2020:
            return ""  # 五輪特措法により 7/24 へ移動
        if y >= 2000:
            if _nth_weekday(d) == "22":
                return "体育の日"
            if d == _SOKUI_REIWA:
                return "即位礼正殿の儀"
            return ""
        if y >= 1966:
            return "体育の日" if day == 10 else ""
        return ""

    # -- 11月 --
    if m == 11:
        if day == 3:
            return "文化の日"
        if day == 23:
            return "勤労感謝の日"
        if d == _SOKUI_HEISEI:
            return "即位礼正殿の儀"
        return ""

    # -- 12月 --
    if m == 12:
        if day == 23 and 1989 <= y <= 2018:
            return "天皇誕生日"  # 平成天皇
        return ""

    return ""


def holiday_name(d: _dt.date) -> str:
    """祝日名を返す。祝日でなければ空文字 (VBA: ktHolidayName)。

    振替休日の判定 : 対象日が祝日でなく、かつ月曜日の場合のみ
    前日(日曜)が祝日かどうかを見て「振替休日」とする。
    """
    if isinstance(d, _dt.datetime):
        d = d.date()

    name = _base_holiday(d)
    if name:
        return name

    if _vb_weekday(d) == _MONDAY and d >= _SUBSTITUTE_START:
        if _base_holiday(d - _dt.timedelta(days=1)):
            return "振替休日"
    return ""


def is_holiday(d: _dt.date) -> bool:
    """祝日なら True。"""
    return bool(holiday_name(d))
