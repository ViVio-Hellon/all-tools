"""Auto-calculation logic ported from the VBA ``単重計算`` / ``包み数計算``
Subs (standard module, ~line 18209 / 18238).

Both functions operate on a single row's worth of textbox values and
return the *new* value for the field they compute -- callers are
responsible for writing it back into the widget/model and for deciding
when to invoke them (WEI's ``Exit`` event for unit weight; MAI/TUT's
``Exit`` event for package count).
"""
from __future__ import annotations

from .numeric import format_fixed, is_numeric, to_float


def unit_weight(con: str, wei: str, uni: str, lot: str) -> str:
    """Port of ``単重計算(i)``: 単重 (unit weight) = WEI / CON.

    Faithfully reproduces the original's quirky guard: the value is only
    *computed* when CON is non-zero, UNI is currently blank, and LOT is
    non-blank. In every other reachable branch (CON/WEI not numeric, CON
    is zero, or UNI already holds a value) the field is cleared back to
    "" -- this matches the VBA source exactly, including the fact that a
    manually typed UNI gets wiped the next time this runs while CON/WEI
    are still numeric.
    """
    if not (is_numeric(con) and is_numeric(wei)):
        return ""

    con_val = to_float(con)
    if con_val != 0 and uni == "":
        if lot != "":
            wei_val = to_float(wei)
            return format_fixed(wei_val / con_val, 2)
        return uni  # unchanged (still blank)
    return ""


def package_count(mai: str, tut: str) -> str:
    """Port of ``包み数計算()``: 包み数 (package/bundle count) = MAI / TUT.

    Only meaningful for lines where the original gated the whole loop on
    ``UFdaily.LS Or UFdaily.NS1 Or UFdaily.AIM`` -- callers should check
    :data:`nippou.constants.PACKAGE_CALC_LINES` before calling this for a
    given line, matching that gate.

    ⚠ **この値はデータに残りません。** VBA ではこれが入力中の Change で
    走ったあと、出力の直前に走る ``Weight計算`` が必ず「枚数 × 包数」で
    上書きしていました(:func:`package_total`)。実データ(日報管理の
    ``T_日報明細_*`` 全ライン 2831 行)を突き合わせても、かけ算だけが
    成り立つ行が 75 行、割り算だけが成り立つ行は 0 行です。

    そのため Web 版は :func:`package_total` のほうを使います。この関数は
    **移植の記録として**残してあります ── 消すと「なぜ割り算ではないのか」
    を次に読む人が調べ直すことになります。
    """
    if not (is_numeric(mai) and is_numeric(tut)):
        return ""

    mai_val = to_float(mai)
    tut_val = to_float(tut)
    if mai_val != 0 and tut_val != 0:
        return format_fixed(mai_val / tut_val, 0)
    return ""


# ==================================================================
# 単重から作業重量を出す (VBA ``Weight計算`` / ``CalculateWeights``)
#
# 【なぜフォームを経由していたのか】
# 打った値をそのままシートへ出すのではなく、**フォームの上で計算と変換を
# 済ませてから**出していました。重量まわりはこの順です:
#
#     実績合計 枚数 = 個装単位 枚数 × 梱包単位 包数
#     単重が空の欄は、**上の行までさかのぼって**いちばん近い単重を使う
#     実績合計 重量 = 実績合計 枚数 × その単重     (すでに値があれば触らない)
#     直の合計 = 枚数の総和 / 重量の総和
#
# 【さかのぼって単重を探す理由】
# 同じロットを何行にも分けて打つとき、単重は先頭の行にしか入りません
# (`FindPreviousUnitWeight`)。2行目以降で空だからといって重量を出さない
# ようにすると、**分けて打った日だけ合計が合わなくなります。**
#
# 【すでに重量が入っていれば触らない】
# 現物を量って入れた値のほうが正しいので、計算で上書きしません
# (`If .Controls("WEI" & i) = "" Then`)。
# ==================================================================

def package_total(mai: str, tut: str) -> str:
    """実績合計 枚数 = 個装単位 枚数 × 梱包単位 包数 (``CON = MAI * TUT``)。

    VBA は `val()` を通すので、数字でない欄は 0 として扱い、結果が 0 なら
    空にします(``If .Controls("CON" & i) = "0" Then ... = ""``)。
    """
    mai_val = to_float(mai) if is_numeric(mai) else 0.0
    tut_val = to_float(tut) if is_numeric(tut) else 0.0
    total = mai_val * tut_val
    if total == 0:
        return ""
    return format_fixed(total, 0)


def previous_unit_weight(rows: dict[int, dict[str, str]], row: int) -> float:
    """その行から上へさかのぼって、いちばん近い単重 (``FindPreviousUnitWeight``)。

    自分の行も含めて見ます。見つからなければ 0(=重量を出さない)。
    """
    for candidate in range(row, 0, -1):
        text = (rows.get(candidate, {}).get("UNI") or "").strip()
        if text and is_numeric(text):
            return to_float(text)
    return 0.0


def work_weight(*, con: str, unit: float, mai: str, tut: str, wei: str) -> str:
    """実績合計 重量 = 実績合計 枚数 × 単重。

    **すでに重量が入っていれば触りません。** 現物を量った値のほうが正しい。
    枚数と包数の両方が埋まっている行だけが対象です(VBA と同じ)。
    """
    # VBA は先に "0.0" を空へ均してから見る
    current = "" if wei.strip() in ("", "0.0") else wei.strip()
    if current:
        return current
    if unit <= 0 or not mai.strip() or not tut.strip():
        return ""
    con_val = to_float(con) if is_numeric(con) else 0.0
    return format_fixed(con_val * unit, 1)


def totals(rows: dict[int, dict[str, str]]) -> tuple[str, str]:
    """直の合計(枚数, 重量)。VBA の ``AlCount`` / ``AlWeight``。

    どちらも `val()` 相当なので、空や数字でない欄は 0 として足します。
    """
    count = 0.0
    weight = 0.0
    for values in rows.values():
        con = (values.get("CON") or "").strip()
        wei = (values.get("WEI") or "").strip()
        if is_numeric(con):
            count += to_float(con)
        if is_numeric(wei):
            weight += to_float(wei)
    return (format_fixed(count, 0) if count else "",
            format_fixed(weight, 1) if weight else "")


def tons(weight_kg: str) -> str:
    """Kg をトンにした添え書き。紙の「4379.4 Kg / 4.38 T」の下の行。

    **合計そのものは Kg のまま**です。トンは読むための添え物なので、
    数字でなければ空を返して黙って消えます(紙も空欄のことがある)。
    """
    text = (weight_kg or "").strip()
    if not is_numeric(text):
        return ""
    value = to_float(text) / 1000.0
    return format_fixed(value, 2) if value else ""


# 重量を計算し直してよいか聞く欄。**この2つを変えたときだけ聞きます**
# ── 実績合計 枚数 = 個装単位 枚数 × 梱包単位 包数 なので、
# 重量の元になるのはこの2つです
WEIGHT_SOURCES = ("MAI", "TUT")


def weight_if_recalculated(rows: dict[int, dict[str, str]], row: int) -> str:
    """その行の重量を、**いま入っている値を無視して**計算し直すといくつか。

    `work_weight` は重量が入っていれば触りません(現物を量った値のほうが
    正しいため)。ここはその手前の値、「もし計算し直したら」を出します。
    """
    values = rows.get(row, {})
    return work_weight(
        con=package_total(values.get("MAI", ""), values.get("TUT", "")),
        unit=previous_unit_weight(rows, row),
        mai=values.get("MAI", ""),
        tut=values.get("TUT", ""),
        wei="")                       # **空として見る** ── ここが違い


def weight_needs_asking(rows: dict[int, dict[str, str]], row: int,
                        changed: str) -> str:
    """「重量を変更しますか？」と聞くべきか。聞くなら**新しい値**を返す。

    【VBAはここで何もしませんでした】
    VBA の `Weight計算` は、重量が入っている行を触りません。つまり
    重量が出たあとに個装単位 枚数や梱包単位 包数を直しても、**重量は
    前のままです。** 紙に「現物を量った重量」を書くことがあるので、
    勝手に上書きしない、という作りでした。

    ところが現場で多いのは「打ち間違えた枚数を直す」ほうです。直しても
    重量が動かないので、**枚数と重量が食い違ったまま保存できます** ──
    食い違いは画面に出ないので、集計まで通ります。

    かといって黙って上書きすると、量った重量が消えます。どちらも黙って
    やるには重すぎるので、**聞きます。**

    聞くのは3つが揃ったときだけ:

        1. 直したのが個装単位 枚数 か 梱包単位 包数 (`WEIGHT_SOURCES`)
        2. その行の重量に、すでに値が入っている
        3. 計算し直すと**違う値**になる

    揃わなければ空を返します(聞かない)。
    """
    if changed not in WEIGHT_SOURCES:
        return ""
    current = (rows.get(row, {}).get("WEI") or "").strip()
    if not current or current == "0.0":
        # 空なら、ふつうに `apply_weights` が入れます ── 聞く必要は無い
        return ""
    fresh = weight_if_recalculated(rows, row)
    if not fresh or fresh == current:
        return ""
    return fresh


def apply_weights(rows: dict[int, dict[str, str]],
                  *, row_count: int) -> tuple[str, str]:
    """1画面ぶんの重量計算をまとめて当てる (``CalculateWeights``)。

    `rows` を**その場で書き換え**、直の合計(枚数, 重量)を返します。
    VBA と同じく2周します ── 1周目で枚数を全部決めてから、2周目で
    単重をさかのぼるためです(1周で回すと、まだ決まっていない枚数を
    使ってしまいます)。
    """
    for row in range(1, row_count + 1):
        values = rows.setdefault(row, {})
        values["CON"] = package_total(values.get("MAI", ""), values.get("TUT", ""))

    for row in range(1, row_count + 1):
        values = rows[row]
        values["WEI"] = work_weight(
            con=values.get("CON", ""),
            unit=previous_unit_weight(rows, row),
            mai=values.get("MAI", ""),
            tut=values.get("TUT", ""),
            wei=values.get("WEI", ""))

    return totals(rows)
