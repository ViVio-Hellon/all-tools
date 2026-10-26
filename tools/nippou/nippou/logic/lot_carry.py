"""前の直から続くロット ── **検入枚数を、残りの枚数にするか訊く** (v4.41.0)

    1直に
    LOTA　20枚　　5枚　１包
    　　　　　　　5枚　１包
    　　　　　　　5枚　１包

    2直に
    LOTAを入力した場合に　1直で15枚作業しています　と出し　検入数を変えますか？　と聞いてほしい
    (このケースだと5枚になる)

ロット番号を引くと、検入枚数(KEN)には仕掛ロットの枚数(そのロットの全部)が入ります。
前の直で同じロットを途中まで作業していると、この直で作業するのは**残りの枚数**です。

【数える直】
いまの直の**1つ前の直から遡って、そのロットを作業していた直が続くかぎり**数えます
(3直 → 翌1直 → 翌2直 と続けば、2直では 3直と1直の両方)。そのロットが無い直に当たったら
止めます(間の空いた日の同じ番号は、別の作業として数えない)。遡るのは1日ぶん(3直まで)。

【数える枚数】
その直の全ページの、同じロット番号の行の **実績合計 枚数(CON = 個装単位 枚数 × 梱包単位 包数)**。
CON が空の行は MAI × TUT で数えます。

【訊く・訊かない】
- 作業した枚数が 0 → 何もしない
- 検入枚数が数で、作業した枚数がそれより少ない → 訊く(はい → 検入枚数 − 作業した枚数)
- 作業した枚数が検入枚数以上 → 訊かずに知らせる(残りが 0 以下になるので、変える先が無い)

**決めるのはここ、訊いて書き換えるのは画面**(はい のときだけ。いいえ なら仕掛ロットの枚数のまま)。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable, Optional

from .numeric import is_numeric, to_float
from .prev_shift import previous_of

#: 遡る直の数(1日ぶん)
MAX_SHIFTS = 3


@dataclass(frozen=True)
class Worked:
    """前の直で、そのロットを作業した枚数。"""

    shift: str
    day: date
    sheets: int


def _num(value: object) -> float:
    text = str(value or "").strip()
    return to_float(text) if is_numeric(text) else 0.0


def sheets_of(details: Iterable, lot_no: str) -> int:
    """その直の行のうち、同じロット番号の行の枚数の合計(CON、空なら MAI × TUT)。

    **ロット№が空の行は、上の行のロットの続き**です(紙の書き方。上の例の2・3行目)。
    1ページに同じロット№は2度打てない(保存で「LOTが重複しています」と断る)ので、
    続きの行を数えないと、分けて打ったロットは最初の1行ぶんしか数えられません
    (15枚のところを「5枚作業しています」と訊いていました)。数え方は梱包数の確かめ
    (`save_checks.check_pack_count`、VBA の `Kara`)と同じで、ページをまたいでも続きます。
    """
    lot_no = (lot_no or "").strip()
    total = 0.0
    current = ""
    ordered = sorted(details, key=lambda d: (int(getattr(d, "page", 0) or 0),
                                             int(getattr(d, "row_no", 0) or 0)))
    for d in ordered:
        mine = (getattr(d, "lot", "") or "").strip()
        if mine:
            current = mine
        if not lot_no or current != lot_no:
            continue
        con = _num(getattr(d, "con", ""))
        total += con if con else _num(getattr(d, "mai", "")) * _num(getattr(d, "tut", ""))
    return int(round(total))


def walk_back(shift: str, day: date, details_of, lot_no: str,
              max_shifts: int = MAX_SHIFTS) -> list[Worked]:
    """1つ前の直から遡り、そのロットを作業していた直が続くかぎり集める(古い順)。

    ``details_of(shift, day)`` はその直の全ページの行を返す(保存が無ければ空)。
    """
    found: list[Worked] = []
    here = (shift, day)
    for _ in range(max_shifts):
        prev = previous_of(*here)
        if prev is None:
            break
        sheets = sheets_of(details_of(*prev), lot_no)
        if sheets <= 0:
            break
        found.append(Worked(prev[0], prev[1], sheets))
        here = prev
    return list(reversed(found))


def _where(worked: list[Worked], today: date) -> str:
    """「1直」「3直(10/20)・1直」── いまの直と日が違う直には日を添える。"""
    parts = []
    for w in worked:
        parts.append(w.shift if w.day == today else f"{w.shift}({w.day.month}/{w.day.day})")
    return "・".join(parts)


def decide(*, row: int, lot_no: str, ken: str, worked: list[Worked],
           today: date) -> Optional[dict]:
    """訊くか・知らせるか。何もしないなら None。**画面はこれを写すだけ。**"""
    total = sum(w.sheets for w in worked)
    if total <= 0:
        return None
    where = _where(worked, today)
    said = f"{where}で{total}枚作業しています"
    if len(worked) > 1:
        said = (f"{where}で、合わせて{total}枚作業しています"
                f"({'・'.join(f'{w.shift} {w.sheets}枚' for w in worked)})")
    current = _num(ken)
    out = {"row": row, "lot_no": lot_no, "worked": total, "where": where, "ken": ken}
    if not current or total >= current:
        out.update(ask=False, message=f"{row}行目 ロット {lot_no}: {said}。"
                                      + (f"検入枚数({ken})以上です。" if current else ""))
        return out
    left = int(round(current - total))
    out.update(ask=True, next=str(left),
               message=f"{row}行目 ロット {lot_no}: {said}。\n"
                       f"検入数を変えますか?({ken}枚 → {left}枚)")
    return out
