"""入力の近道 (v4.7.0)

    入力が短縮されるような便利機能は何かないですかね？
    → 全部入れてください / マウスムーブで簡易説明が出るようにしてください

実物の日報(13ファイル・40ページ)を数えて、**人が毎回打っているもの**を
減らします:

    同じロットの続きの行          186行(ロット№を打った行は86行)
      個装単位 枚数が上の行と同じ  154/186行(83%)
      梱包単位 包数が「1」         274/274行(すべて)
    停止記号のよく使う7つ          186/205件(91%)
    最後の行の終了が直の終わり     29/40ページ(72%)

ここが持つのは**決めること**だけです(画面は `views/entry.js`、配線は
`app/routes/entry.py`)。

    copy_above        枚数欄のダブルクリック → 上の行と同じ枚数・包数
    default_packs     包数が空のまま欄を離れた → 1
    frequent_codes    停止記号の一覧の上に「よく使う」
    shift_end         「直の終わり」で入れる時刻
    enter_targets     Enter / Shift+Enter で移る先
    hint              欄にマウスを乗せたときの簡易説明

【打っていないものが勝手に入らないように】
どれも**押した・離れた**ときにだけ値が入ります。黙って先回りして埋める
ものは作りません ── 打っていない値が入るのが、いちばん見直しで気づけない
間違いなので(停止理由が触っただけで入っていたとき、そうでした)。
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional

from .. import constants

#: 上の行から写す欄(個装単位 枚数・梱包単位 包数)
COPY_FAMILIES: tuple[str, ...] = ("MAI", "TUT")

#: 包数が空のまま離れたときに入れる値。実物は 274/274 行が 1 でした
DEFAULT_PACKS = "1"

#: 「よく使う」に並べる数。実物は上位7つで 91%
FREQUENT_LIMIT = 7

#: 「よく使う」に入れるのは、この回数以上使ったものだけ(1回きりは並べない)
FREQUENT_MIN_COUNT = 2


# ----------------------------------------------------------------------
# 上の行と同じ(枚数欄のダブルクリック)
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class CopyAbove:
    """「上の行と同じ」の答え。`ok` でなければ何も変えない。"""

    ok: bool
    row: int
    from_row: int = 0
    values: dict[str, str] = field(default_factory=dict)
    message: str = ""

    def as_dict(self) -> dict:
        return {"ok": self.ok, "row": self.row, "from_row": self.from_row,
                "values": dict(self.values), "message": self.message}


def copy_above(rows: Mapping[int, Mapping[str, str]], row: int) -> CopyAbove:
    """その行の上で、**いちばん近い枚数・包数の入った行**から写す。

    同じロットを分けて打つとき、続きの行は枚数も包数も上と同じことが
    ほとんどです(実物は 83% / 100%)。真上が停止だけの行(全停など)の
    ことがあるので、空の行は飛ばして上へさかのぼります。

    **押したときだけ**写します。入っている値も上書きします(押した、
    ということは写したい、ということなので)。
    """
    if not 1 < row <= constants.ROW_COUNT:
        return CopyAbove(False, row, message="上に行がありません")
    for above in range(row - 1, 0, -1):
        values = rows.get(above) or {}
        found = {f: str(values.get(f) or "").strip() for f in COPY_FAMILIES}
        if any(found.values()):
            mai, tut = found["MAI"], found["TUT"]
            text = " / ".join(filter(None, [
                f"枚数 {mai}" if mai else "", f"包数 {tut}" if tut else ""]))
            return CopyAbove(True, row, above, found,
                             f"{row}行目に {above}行目と同じ {text} を入れました")
    return CopyAbove(False, row,
                     message=f"{row}行目より上に、枚数の入った行がありません")


# ----------------------------------------------------------------------
# 包数が空なら 1
# ----------------------------------------------------------------------
def default_packs(values: Mapping[str, str], changed: str) -> Optional[str]:
    """枚数か包数の欄を離れたとき、**包数が空なら 1** を返す。入れないなら None。

    実績合計 枚数は「枚数 × 包数」なので、包数が空だと枚数も重量も
    出ません(`calculations.package_total`)。実物は全部の行が 1 でした。

    入れるのは**枚数が入っていて、包数が空**のときだけ。枚数が空の行
    (停止だけの行)には入れません ── 打っていない行に 1 だけ残ります。
    """
    if changed not in COPY_FAMILIES:
        return None
    mai = str(values.get("MAI") or "").strip()
    tut = str(values.get("TUT") or "").strip()
    if not mai or tut:
        return None
    return DEFAULT_PACKS


# ----------------------------------------------------------------------
# 停止記号の「よく使う」
# ----------------------------------------------------------------------
def frequent_codes(used: Iterable[str], available: Iterable[str], *,
                   limit: int = FREQUENT_LIMIT,
                   min_count: int = FREQUENT_MIN_COUNT) -> list[str]:
    """使った記号を数えて、**多い順**に `limit` まで。

    `used` はそのラインで最近保存した行の記号(①②③ぜんぶ)。一覧に
    無い記号(マスタから消えたもの)は並べません ── 選べない記号を
    「よく使う」に出しても押せないので。同じ回数なら、先に使ったほう
    (新しい保存から読んでいれば、より最近のほう)を上にします。
    """
    allowed = set(available)
    counts = Counter(c for c in (str(u or "").strip() for u in used)
                     if c and c in allowed)
    first_seen: dict[str, int] = {}
    for i, code in enumerate(str(u or "").strip() for u in used):
        first_seen.setdefault(code, i)
    ranked = sorted((c for c, n in counts.items() if n >= min_count),
                    key=lambda c: (-counts[c], first_seen[c]))
    return ranked[:max(0, limit)]


# ----------------------------------------------------------------------
# 直の終わり
# ----------------------------------------------------------------------
def shift_end(shift: str,
              shift_end_times: Mapping[str, tuple[str, str]]) -> Optional[tuple[str, str]]:
    """その直の終わりの時刻(時, 分)。分からなければ None。

    **丸めはしません。** 残業で 23:20 や 23:30 に終わった行が実物に
    あったので、いまの時刻を直の終わりに寄せることはせず、押したときに
    だけ直の終わりを入れます。
    """
    found = shift_end_times.get(shift)
    if not found:
        return None
    hour, minute = (str(found[0]).strip(), str(found[1]).strip())
    if not hour.isdigit() or not minute.isdigit():
        return None
    return hour.zfill(2), minute.zfill(2)


# ----------------------------------------------------------------------
# Enter / Shift+Enter
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class EnterTarget:
    """Enter で行く先と、Shift+Enter で戻る先。行は**ずらす数**で持つ。"""

    next_family: str
    next_row: int        # 0 = 同じ行 / 1 = 次の行
    prev_family: str
    prev_row: int        # 0 = 同じ行 / -1 = 前の行


def _chain() -> list[str]:
    """打つ順(`FOCUS_CHAIN`)。2桁で次へ飛ぶのと**同じ道**を通る。"""
    order = ["LOT"]
    while True:
        nxt = constants.FOCUS_CHAIN.get(order[-1], (None, 0))[0]
        if not nxt or nxt in order:
            return order
        order.append(nxt)


def enter_targets(screen_order: Iterable[str]) -> dict[str, EnterTarget]:
    """画面の列の並びから、欄ごとの行く先・戻る先を決める。

    行く先は**打つ順**(`FOCUS_CHAIN`、2桁で次へ飛ぶのと同じ道)。その道に
    無い欄(合紙・実績合計・単重)からは、右にある次の欄へ進みます。
    行の最後(作業停止③の時間)の次は、次の行のロット№です。
    """
    chain = _chain()
    on_chain = set(chain)
    order = list(screen_order)
    out: dict[str, EnterTarget] = {}
    for i, family in enumerate(order):
        if family in on_chain:
            at = chain.index(family)
            nxt = (chain[at + 1], 0) if at + 1 < len(chain) else (chain[0], 1)
            prv = (chain[at - 1], 0) if at > 0 else (chain[-1], -1)
        else:
            right = [f for f in order[i + 1:] if f in on_chain]
            left = [f for f in order[:i] if f in on_chain]
            nxt = (right[0], 0) if right else (chain[0], 1)
            prv = (left[-1], 0) if left else (chain[-1], -1)
        out[family] = EnterTarget(nxt[0], nxt[1], prv[0], prv[1])
    return out


def enter_attributes(target: Optional[EnterTarget]) -> dict[str, str]:
    """`<input>` にそのまま付けられる形。"""
    if target is None:
        return {}
    return {"data-enter-next": target.next_family,
            "data-enter-next-row": str(target.next_row),
            "data-enter-prev": target.prev_family,
            "data-enter-prev-row": str(target.prev_row)}


# ----------------------------------------------------------------------
# マウスを乗せたときの簡易説明
# ----------------------------------------------------------------------
_STOP_CODE = ("停止の記号。上の「よく使う」に、このラインでよく使う記号が"
              "並びます。Delete で(停止なし)に戻ります")
_STOP_TIME = "止まっていた時間(分)。3桁打つと次の欄へ進みます"

#: 欄の簡易説明。**1〜2行で言い切る**(長い説明は画面の「くわしく」に)
HINTS: dict[str, str] = {
    "LOT": "ロット№を7桁打つと、材・調質・寸法・検入枚数などが自動で埋まります",
    "ZAI": "材・調質。ロット№から自動で入ります",
    "SIZ": "厚×幅×丈。ロット№から自動で入ります",
    "KEN": "検入枚数。ロット№から自動で入ります",
    "KZ": ("開始の時。ダブルクリックでいまの時刻を時と分に入れます"
           "(分は 0 か 5 に丸めます)。2桁で次の欄へ"),
    "KH": ("開始の分。ダブルクリックでいまの時刻を時と分に入れます"
           "(分は 0 か 5 に丸めます)。2桁で次の欄へ"),
    "SZ": ("終了の時。ダブルクリックでいまの時刻(分は 0 か 5 に丸めます)、"
           "下の「直の終わり」で直の終わりの時刻。終了は次の行の開始へ写ります"),
    "SH": ("終了の分。ダブルクリックでいまの時刻(分は 0 か 5 に丸めます)、"
           "下の「直の終わり」で直の終わりの時刻。終了は次の行の開始へ写ります"),
    "HIT": "作業人数。1桁打つと次の欄へ進みます",
    "AI": "合紙。ロット№から自動で選ばれます",
    "MAI": "個装単位 枚数。ダブルクリックで上の行と同じ枚数・包数が入ります",
    "TUT": ("梱包単位 包数。空のまま欄を離れると 1 が入ります。"
            "ダブルクリックで上の行と同じ枚数・包数"),
    "VC": "ＶＣ種別。ロット№から自動で入ります",
    "ET": "etc。表の下の押しボタン(ｽﾄｱ・耳付 など)でも入ります",
    "S": _STOP_CODE, "SS": _STOP_CODE, "STH": _STOP_CODE,
    "TH": _STOP_TIME, "THS": _STOP_TIME, "THT": _STOP_TIME,
    "CON": "実績合計 枚数 = 個装単位 枚数 × 梱包単位 包数(自動)",
    "WEI": "実績合計 重量 = 枚数 × 単重(自動)。量った重量を打つこともできます",
    "TIM": "作業時間(分) = 終了 − 開始 − 停止(自動。紙には載りません)",
    "UNI": "単重。ロット№から入ります。分けて打った行は上の行の単重を使います",
}


def hint(family: str) -> str:
    """その欄の簡易説明。無ければ空。"""
    return HINTS.get(family, "")
