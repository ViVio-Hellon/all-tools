"""Static data tables ported verbatim from the VBA source.

Every mapping in this file is transcribed directly from a VBA literal
(``InitHdChMaps``, ``MoveText``, ``Point_Change`` in the ~22k line standard
module) rather than re-derived, so that the exclusion/navigation behaviour
matches the legacy tool exactly. See the docstring on each table for the
originating VBA Sub/Function name and line reference kept for traceability.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# HdCh1..HdCh9 / CheckBox8-13,75-80 mutual exclusion (InitHdChMaps).
#
# The 9 "HdCh" radio-like checkboxes represent a 3x3 grid of "reason
# groups"; each one, several "detail" checkboxes (CheckBox8-13 and
# CheckBox75-80) and several other HdCh boxes must be cleared when it is
# checked. CheckBox8-13/75-80 do the mirror image when clicked directly.
# ---------------------------------------------------------------------------

HDCH_EXCLUDES_HDCH: dict[str, tuple[str, ...]] = {
    "HdCh1": ("HdCh2", "HdCh3", "HdCh4", "HdCh7"),
    "HdCh2": ("HdCh1", "HdCh3", "HdCh5", "HdCh8"),
    "HdCh3": ("HdCh1", "HdCh2", "HdCh6", "HdCh9"),
    "HdCh4": ("HdCh1", "HdCh7", "HdCh5", "HdCh6"),
    "HdCh5": ("HdCh2", "HdCh8", "HdCh4", "HdCh6"),
    "HdCh6": ("HdCh3", "HdCh9", "HdCh4", "HdCh5"),
    "HdCh7": ("HdCh1", "HdCh4", "HdCh8", "HdCh9"),
    "HdCh8": ("HdCh2", "HdCh5", "HdCh7", "HdCh9"),
    "HdCh9": ("HdCh3", "HdCh6", "HdCh7", "HdCh8"),
}

HDCH_EXCLUDES_CHECKBOX: dict[str, tuple[str, ...]] = {
    "HdCh1": ("CheckBox8", "CheckBox11", "CheckBox75", "CheckBox78"),
    "HdCh2": ("CheckBox9", "CheckBox12", "CheckBox76", "CheckBox79"),
    "HdCh3": ("CheckBox10", "CheckBox13", "CheckBox77", "CheckBox80"),
    "HdCh4": ("CheckBox8", "CheckBox11", "CheckBox75", "CheckBox78"),
    "HdCh5": ("CheckBox9", "CheckBox12", "CheckBox76", "CheckBox79"),
    "HdCh6": ("CheckBox10", "CheckBox13", "CheckBox77", "CheckBox80"),
    "HdCh7": ("CheckBox8", "CheckBox11", "CheckBox75", "CheckBox78"),
    "HdCh8": ("CheckBox9", "CheckBox12", "CheckBox76", "CheckBox79"),
    "HdCh9": ("CheckBox10", "CheckBox13", "CheckBox77", "CheckBox80"),
}

CHECKBOX_EXCLUDES_HDCH: dict[str, tuple[str, ...]] = {
    "CheckBox8": ("HdCh1", "HdCh4", "HdCh7"),
    "CheckBox9": ("HdCh2", "HdCh5", "HdCh8"),
    "CheckBox10": ("HdCh3", "HdCh6", "HdCh9"),
    "CheckBox11": ("HdCh1", "HdCh4", "HdCh7"),
    "CheckBox12": ("HdCh2", "HdCh5", "HdCh8"),
    "CheckBox13": ("HdCh3", "HdCh6", "HdCh9"),
    "CheckBox75": ("HdCh1", "HdCh4", "HdCh7"),
    "CheckBox76": ("HdCh2", "HdCh5", "HdCh8"),
    "CheckBox77": ("HdCh3", "HdCh6", "HdCh9"),
    "CheckBox78": ("HdCh1", "HdCh4", "HdCh7"),
    "CheckBox79": ("HdCh2", "HdCh5", "HdCh8"),
    "CheckBox80": ("HdCh3", "HdCh6", "HdCh9"),
}

CHECKBOX_EXCLUDES_CHECKBOX: dict[str, tuple[str, ...]] = {
    "CheckBox8": ("CheckBox9", "CheckBox10", "CheckBox11", "CheckBox75", "CheckBox78"),
    "CheckBox9": ("CheckBox8", "CheckBox10", "CheckBox12", "CheckBox76", "CheckBox79"),
    "CheckBox10": ("CheckBox8", "CheckBox9", "CheckBox13", "CheckBox77", "CheckBox80"),
    "CheckBox11": ("CheckBox8", "CheckBox12", "CheckBox13", "CheckBox75", "CheckBox78"),
    "CheckBox12": ("CheckBox9", "CheckBox11", "CheckBox13", "CheckBox76", "CheckBox79"),
    "CheckBox13": ("CheckBox10", "CheckBox11", "CheckBox12", "CheckBox77", "CheckBox80"),
    "CheckBox75": ("CheckBox8", "CheckBox76", "CheckBox77", "CheckBox11", "CheckBox78"),
    "CheckBox76": ("CheckBox9", "CheckBox75", "CheckBox77", "CheckBox12", "CheckBox79"),
    "CheckBox77": ("CheckBox10", "CheckBox75", "CheckBox76", "CheckBox13", "CheckBox80"),
    "CheckBox78": ("CheckBox8", "CheckBox79", "CheckBox80", "CheckBox11", "CheckBox75"),
    "CheckBox79": ("CheckBox9", "CheckBox78", "CheckBox80", "CheckBox12", "CheckBox76"),
    "CheckBox80": ("CheckBox10", "CheckBox78", "CheckBox79", "CheckBox13", "CheckBox77"),
}

HDCH_NAMES: tuple[str, ...] = tuple(f"HdCh{i}" for i in range(1, 10))
DETAIL_CHECKBOX_NAMES: tuple[str, ...] = tuple(f"CheckBox{i}" for i in (8, 9, 10, 11, 12, 13, 75, 76, 77, 78, 79, 80))

# ---------------------------------------------------------------------------
# Focus-advance chain (MoveText). Each entry: field family -> (next family,
# required text length to trigger auto-advance). A required length of None
# means "effectively never" (VBA left Mcount at its 20-char default for
# free-text fields, which realistically never reaches 20 chars, so no
# auto-advance happens for those fields).
#
# Fields NOT in NUMERIC_ONLY_FAMILIES keep whatever text the user typed;
# fields IN it get blanked out if the current text is not numeric
# (replicates ``If Not IsNumeric(...) Then .Controls(TEX & num) = ""``).
# TH5 (作業停止⑤の時間) is the terminal family: it is never auto-focused away from.
# ---------------------------------------------------------------------------

FOCUS_CHAIN: dict[str, tuple[str | None, int]] = {
    "LOT": ("ZAI", 20),
    "ZAI": ("SIZ", 20),
    "SIZ": ("KEN", 20),
    "KEN": ("KZ", 20),
    "KZ": ("KH", 2),
    "KH": ("SZ", 2),
    "SZ": ("SH", 2),
    "SH": ("HIT", 2),
    "HIT": ("MAI", 1),
    "MAI": ("TUT", 20),
    "TUT": ("VC", 20),
    "VC": ("ET", 20),
    "ET": ("S", 20),
    "S": ("TH", 20),
    "TH": ("SS", 3),
    "SS": ("THS", 20),
    "THS": ("STH", 3),
    "STH": ("THT", 20),
    "THT": ("S4", 3),
    "S4": ("TH4", 20),
    "TH4": ("S5", 3),
    "S5": ("TH5", 20),
    "TH5": (None, 3),
}

# 作業停止①〜⑤の (記号の欄, 時間の欄)。**停止の組はここだけで決める**
# (④⑤は v4.24.0。紙と VBA は③まで)。小文字は DB の列名
STOP_SLOTS: tuple[tuple[str, str], ...] = (
    ("S", "TH"), ("SS", "THS"), ("STH", "THT"), ("S4", "TH4"), ("S5", "TH5"))
STOP_FIELD_PAIRS: tuple[tuple[str, str], ...] = tuple(
    (code.lower(), minutes.lower()) for code, minutes in STOP_SLOTS)
STOP_CODE_FAMILIES: tuple[str, ...] = tuple(code for code, _ in STOP_SLOTS)
STOP_TIME_FAMILIES: tuple[str, ...] = tuple(minutes for _, minutes in STOP_SLOTS)

# Families whose text is force-cleared when not numeric (all families
# *except* this text-like set, per MoveText's second Select Case).
TEXT_LIKE_FAMILIES: frozenset[str] = frozenset({"LOT", "SIZ", "ZAI", "VC", "ET", "S", "SS", "STH",
                                                "S4", "S5"})

# Field families highlighted/reset together by Point_Change, in the row
# entry grid (one instance per row 1..12).
ROW_FIELD_FAMILIES: tuple[str, ...] = (
    "LOT", "ZAI", "SIZ", "KEN", "KZ", "KH", "SZ", "SH", "HIT",
    "MAI", "TUT", "VC", "ET", "S", "TH", "SS", "THS", "STH", "THT",
    "S4", "TH4", "S5", "TH5",
    "CON", "WEI",
)

ROW_COUNT = 12

# ---------------------------------------------------------------------------
# Production lines (Line_AutoSelection). 中板 (丸徳) additionally exposes
# sub-selection buttons A1..A7 for its equipment number.
#
# **v4.13.0 から正規の呼び名がそのままツールの名前です**(キー・表の名前・CSV・
# フォルダ)。VBA の名前(L1 / LS / TOT / BALA / MARU)は `logic/line_names` の
# `old` にだけ残り、VBA が貯めたものを読むときに読み替えます。並びは VBA の
# Line_AutoSelection のまま(中身は `line_names.DEFINITIONS` と同じ9つ ── 試験で確かめる)
# ---------------------------------------------------------------------------
LINE_NAMES: tuple[str, ...] = ("L-1", "LVC", "HVC", "トット", "バランサー", "AIM", "機側", "NS1",
                               "中板")

#: Lines where 包み数計算 (package-count auto-calc from MAI/TUT) is active.
#: VBA `UFdaily.LS Or NS1 Or AIM`(LS = 機側)
PACKAGE_CALC_LINES: frozenset[str] = frozenset({"機側", "NS1", "AIM"})

#: Lines that are always treated as the day shift (日勤) regardless of
#: clock time, per TimeCheck/PrintCheck(VBA の TOT / BALA / MARU)
DAY_SHIFT_ALWAYS_LINES: frozenset[str] = frozenset({"トット", "バランサー", "中板"})

# ---------------------------------------------------------------------------
# 音声ファイル (音楽を流す)
#
# **ここには置かない。** 既定のファイル名は `logic/sound.SOUNDS` が持ち、
# 実際に使う名前は設定画面から変えられる(`user_settings`)。現場で音を
# 差し替えたいときに、コードを配り直さずに済ませるため。
# ---------------------------------------------------------------------------
