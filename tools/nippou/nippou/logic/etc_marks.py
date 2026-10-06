"""etc欄の押しボタン (VBA `SPCommand` / `STCommand` / `GACommand` /
`HACommand` / `EXCommand` / `MICommand` と `CommandLook_For`)

紙の「etc 反転・EX etc」欄(O列)には、その行で何をしたかを書き足します。
VBA はそれを**6つの押しボタン**で入れていました:

    ｽﾄｱ   → "ストア"
    耳付  → "耳付き"
    外注  → "外注出荷"
    EX    → "EX"
    反転  → "反転作業"
    ｽﾎﾟｯﾄ → "スポット有"

【押しボタンは覚えていない】
VBA の押しボタンは色(`BackColor`)で押されているかを表していましたが、
**値そのものは etc 欄の文字列です。** 別の行へ移ったときは
`CommandLook_For` が etc 欄の文字を読み直してボタンを塗り直します
── つまり「押した状態」はどこにも保存されておらず、**etc の文字が
唯一の出どころ**でした。

ここも同じにします。ボタンは etc の文字を書き足す・消すだけで、
状態は持ちません。持たせると、過去データを開いたときやページを移ったときに
ボタンと etc の中身が食い違います。

【自動で押されるもの】
    EX   … 受注番号の3桁目が "1"(VBA `If Mid(NowOder, 3, 1) = 1`)
           または SIKAODR の EX_輸出区分が "1"
    外注 … SIKALOT の 設計_設備ｺｰｽ に GFS / GCT / GSS が含まれる

前者は VBA にもありました。後者は**VBAには無い**追加で、現場の判断
(BOXコースを通るものは外注出荷になる)を自動化したものです。
残り4つ(ｽﾄｱ・耳付・反転・ｽﾎﾟｯﾄ)は作業者が任意で押します ── 元データから
決められる事実が無いので、勝手に押しません。
"""
from __future__ import annotations

from dataclasses import dataclass

# 外注と判定する設備コース。**部分一致**(VBA `G_COURSES` と同じ並び)。
# python-web-tools の `lot_service.BOX_COURSES` と同じ3つで、あちらは
# BOX実績寸法へ差し替える判定に使っている
OUTSOURCE_COURSES: tuple[str, ...] = ("GFS", "GCT", "GSS")

# EX と判定する 受注番号 の桁(1始まり)とその値
EX_ORDER_POSITION = 3
EX_ORDER_VALUE = "1"

# EX_輸出区分 が輸出を表す値
EX_EXPORT_VALUE = "1"


@dataclass(frozen=True)
class Mark:
    """押しボタン1つ。"""

    key: str        # 画面とAPIで使う識別子(ASCII)
    label: str      # ボタンに出す文字
    text: str       # etc欄へ書き足す文字。**これが本体**
    auto: bool = False   # 元データから自動で押せるか
    note: str = ""       # なぜ自動で押されるか(押した覚えが無い人へ)


# 並びは写真のボタンの並び(左上から)。
MARKS: tuple[Mark, ...] = (
    Mark("store", "ｽﾄｱ", "ストア"),
    Mark("ear", "耳付", "耳付き"),
    Mark("outsource", "外注", "外注出荷", auto=True,
         note="設計_設備ｺｰｽが " + "/".join(OUTSOURCE_COURSES) + " のいずれかを含む"),
    Mark("ex", "EX", "EX", auto=True,
         note="受注番号の3桁目が1、または EX_輸出区分が1"),
    Mark("flip", "反転", "反転作業"),
    Mark("spot", "ｽﾎﾟｯﾄ", "スポット有"),
)

BY_KEY = {m.key: m for m in MARKS}

# **長いものから先に見る。** "EX" は "反転作業" などには含まれないが、
# 短い印が長い印の一部に入る組み合わせが将来できたとき、短いほうを
# 先に消すと長いほうが壊れる
_BY_LENGTH = tuple(sorted(MARKS, key=lambda m: len(m.text), reverse=True))


def active(etc: str) -> list[str]:
    """いま押されている印(VBA `CommandLook_For`)。

    **etc欄の文字を読むだけ。** 別の行へ移るたび、過去データを開くたび、
    ここを通してボタンを塗り直します ── 状態を持つと必ず食い違います。
    """
    text = etc or ""
    return [m.key for m in MARKS if m.text in text]


def add(etc: str, key: str) -> str:
    """印を1つ足す。すでにあれば何もしない。

    VBA は `ET & " " & "ストア"` と**必ず空白を挟んで**後ろに足していた
    ので、同じにします(紙に出る文字がVBA版と変わらない)。
    """
    mark = BY_KEY.get(key)
    if mark is None or mark.text in (etc or ""):
        return etc or ""
    current = (etc or "").rstrip()
    return f"{current} {mark.text}".strip() if current else mark.text


def remove(etc: str, key: str) -> str:
    """印を1つ消す。

    VBA は消したあと `Replace(..., " ", "")` で**空白を全部消して**
    いました。「スポット有 ストア」から片方を消すと残りがくっついて
    しまうので、ここでは**その印だけを抜いて、空白を1つに詰めます。**
    紙に出るのは残った印だけで、そこは同じです。
    """
    mark = BY_KEY.get(key)
    if mark is None:
        return etc or ""
    return _tidy((etc or "").replace(mark.text, " "))


def toggle(etc: str, key: str) -> str:
    """押すたびに入り切りする(VBA の `BackColor` 分岐)。"""
    return remove(etc, key) if key in active(etc) else add(etc, key)


def _tidy(text: str) -> str:
    """空白の連続を1つに詰め、前後を落とす。

    **改行も空白として詰めます。** 画面の etc 欄は1行の `<input>` で、
    改行を入れてもブラウザが落としてしまうため(包装仕様の注意は
    ` / ` で区切ります ── `logic/pack_note.SEPARATOR`)。
    """
    return " ".join((text or "").split())


# ------------------------------------------------------------------
# 自動で押す判定
# ------------------------------------------------------------------
def is_ex_order(order_no: str) -> bool:
    """受注番号の3桁目が "1"(VBA `If Mid(NowOder, 3, 1) = 1`)。

    3桁に満たない受注番号は判定しません(VBA の `Mid` は空を返し、
    `= 1` が成立しない)。
    """
    text = (order_no or "").strip()
    if len(text) < EX_ORDER_POSITION:
        return False
    return text[EX_ORDER_POSITION - 1] == EX_ORDER_VALUE


def is_ex_export(export_flag: str) -> bool:
    """SIKAODR の EX_輸出区分 が "1"。

    python-web-tools(`lot_service._load_odr`)は**空でなければ輸出**と
    見ています。この列に入るのが "" と "1" だけなら、どちらの読み方でも
    同じ答えになります。ここは現場の言う「1のとき」に合わせます ──
    別の値が入り始めたときに、勝手に輸出扱いしないため。
    """
    return (export_flag or "").strip() == EX_EXPORT_VALUE


def is_outsourced(course: str) -> bool:
    """設計_設備ｺｰｽ に GFS / GCT / GSS のいずれかが含まれる。**部分一致**。"""
    text = course or ""
    return any(c in text for c in OUTSOURCE_COURSES)


def auto_keys(*, order_no: str = "", export_flag: str = "",
              course: str = "") -> list[str]:
    """元データから自動で押せる印。押す順は `MARKS` の並び。"""
    found = []
    if is_outsourced(course):
        found.append("outsource")
    if is_ex_order(order_no) or is_ex_export(export_flag):
        found.append("ex")
    return [m.key for m in MARKS if m.key in found]


def apply_auto(etc: str, keys: list[str]) -> str:
    """自動で決まった印をまとめて足す。**消しはしません。**

    作業者が消した印を次の計算で勝手に戻すと、消せない印になります。
    自動で押すのはロット番号を入れ直したときだけ(そのとき etc は
    まっさらになる)なので、ここは足すだけで足ります。
    """
    text = etc or ""
    for key in keys:
        text = add(text, key)
    return text


def choices() -> list[dict[str, object]]:
    """画面へ渡す形。**文言もサーバが持つ。**"""
    return [{"key": m.key, "label": m.label, "text": m.text,
             "auto": m.auto, "note": m.note} for m in MARKS]
