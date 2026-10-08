"""集計計算ロジック(VBAの ``Aggre_Calcul``/``Stop_Distr``/``CheckCharType``
相当、標準モジュール~12466行, ~15933行)。

日報の1行(``DetailRecord``)は最大3組の「停止理由コード・停止時間」の
ペアを持てる((s,th) / (ss,ths) / (sth,tht))。VBAの ``Stop_Distr`` は
このコードの**文字種**だけを見て、機械的に3つのバケツへ振り分けていた
(``CheckCharType`` の判定順序: 数値 → カタカナ → アルファベット)。

    数値       -> 管理ロス停止   (コード例: "1.TPM活動/清掃" 等の数字)
    カタカナ   -> 突発停止(不稼働) (コード例: "イ.突発停止(機械)" 等)
    アルファベット -> ハンドリング停止 (コード例: "A" 等)

空欄や、どの文字種にも当てはまらない(数字とカタカナが混在する等の)
コードはVBA同様に集計から除外する。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..constants import STOP_FIELD_PAIRS
from ..db.models import DetailRecord, HeaderRecord
from .numeric import is_numeric, to_float
from .shift import SHIFT_NAMES, parse_business_date

MINUTES_PER_DAY = 24 * 60  # 1440分 (1日ぶん)

# 全角カタカナ(U+30A0-30FF) / 半角カタカナ(U+FF61-FF9F)。VBAの
# IsOnlyKatakana(AscWによるコードポイント比較)をそのまま移植。
_KATAKANA_RANGES = ((0x30A0, 0x30FF), (0xFF61, 0xFF9F))


def _is_katakana_only(text: str) -> bool:
    if not text:
        return False
    return all(any(lo <= ord(ch) <= hi for lo, hi in _KATAKANA_RANGES) for ch in text)


def _is_alphabet_only(text: str) -> bool:
    """半角英字(A-Z/a-z)のみか。VBAの ``IsOnlyAlphabet`` の移植。"""
    if not text:
        return False
    return all(ch.isascii() and ch.isalpha() for ch in text)


def classify_stop_code(code: str) -> str:
    """port of ``CheckCharType``。停止理由コード1つの文字種を判定する。

    戻り値は "数値" / "カタカナ" / "アルファベット" / "空" / "その他"
    のいずれか(VBA版の日本語の戻り値をそのまま踏襲)。
    """
    text = (code or "").strip()
    if text == "":
        return "空"
    if is_numeric(text):
        return "数値"
    if _is_katakana_only(text):
        return "カタカナ"
    if _is_alphabet_only(text):
        return "アルファベット"
    return "その他"


def _minutes(value: str) -> float:
    return to_float(value) if is_numeric(value) else 0.0


@dataclass
class ShiftAggregate:
    """port of ``DayShift_Agg``/``Aggre_Calcul`` が集計シート1行分として
    書き出す項目。(報告日, ライン, 直) をキーとする。"""

    report_date: str
    line: str
    shift: str
    sheet_count: float = 0.0             # 合計（枚）
    weight_kg: float = 0.0               # 合計（重量, kg）
    work_minutes: float = 0.0            # 作業時間合計（分）
    management_loss_minutes: float = 0.0     # 管理ロス停止（分）: 数値コード
    unplanned_stop_minutes: float = 0.0      # 突発停止/不稼働（分）: カタカナコード
    handling_stop_minutes: float = 0.0       # ハンドリング停止（分）: アルファベットコード

    @property
    def weight_ton(self) -> float:
        return self.weight_kg / 1000.0

    @property
    def total_stop_minutes(self) -> float:
        return self.management_loss_minutes + self.unplanned_stop_minutes + self.handling_stop_minutes

    @property
    def operating_minutes(self) -> float:
        """稼働時間(分) = 1440分 - (管理ロス+突発+ハンドリング停止)。
        port of ``Aggre_Calcul`` の ``OpeTime``。"""
        return MINUTES_PER_DAY - self.total_stop_minutes

    @property
    def operational_minutes(self) -> float:
        """操業時間(分) = 1440分 - 管理ロス停止のみ。port of ``Opetional``。"""
        return MINUTES_PER_DAY - self.management_loss_minutes

    @property
    def operating_rate_pct(self) -> float:
        """稼働率(%) = 稼働時間 / 1440分 * 100。port of ``OpeRate``。"""
        return self.operating_minutes / MINUTES_PER_DAY * 100

    @property
    def productivity_t_per_h(self) -> float:
        """生産性(t/h) = 重量(t) / 稼働時間(h)。

        VBA版(``DayShift_Agg``)は特定セル位置に依存した簡易式
        (1直+2直の重量 / (515分-管理ロス停止)/60)だったが、直によって
        操業可能時間の前提が異なり一般化しづらいため、ここでは
        「その直の稼働時間」を分母にした素直な式に置き換えている。
        稼働時間が0以下なら計算不能として0を返す。
        """
        hours = self.operating_minutes / 60
        if hours <= 0:
            return 0.0
        return self.weight_ton / hours


_STOP_PAIRS = STOP_FIELD_PAIRS


def aggregate_shift(header: HeaderRecord, details: list[DetailRecord]) -> ShiftAggregate:
    """port of ``Aggre_Calcul``(1直・1保存単位分)。

    **枚数も重量も、明細行を足して出します。** ヘッダの合計欄
    (``header.count`` / ``header.weight_kg``)は画面が同じ行から出した
    写しなので、ふだんは同じ値になります。

    【なぜ写しのほうを使わないのか】
    食い違うのは、**表を直に書き換えたとき**です。画面を通さずに
    明細だけ直すと、合計欄は前の値のまま残ります。そのとき信じるべき
    なのは行のほうで、合計欄はその結果でしかありません。VBA の
    `Aggre_Calcul` も集計シートに並んだ**行を読んで**足していました。

    保存前チェックを「フォームの値ではなくDBを読み直して」走らせるのと
    同じ考え方です(`services/shift_check.py`)。
    """
    agg = ShiftAggregate(
        report_date=header.report_date, line=header.line, shift=header.shift)
    for d in details:
        agg.sheet_count += _minutes(d.con)
        agg.weight_kg += _minutes(d.wei)
        agg.work_minutes += _minutes(d.tim)
        for code_field, minutes_field in _STOP_PAIRS:
            code = getattr(d, code_field)
            minutes = _minutes(getattr(d, minutes_field))
            if minutes == 0:
                continue
            kind = classify_stop_code(code)
            if kind == "数値":
                agg.management_loss_minutes += minutes
            elif kind == "カタカナ":
                agg.unplanned_stop_minutes += minutes
            elif kind == "アルファベット":
                agg.handling_stop_minutes += minutes
            # "空"/"その他" は VBA と同じく集計対象外
    return agg


#: 直の並び。**紙に出る順**で、五十音でも時刻順でもない
#: (名前と並びの出どころは `logic/shift.SHIFT_NAMES`)
SHIFT_ORDER = {name: i for i, name in enumerate(SHIFT_NAMES)}

_SHIFT_ORDER = SHIFT_ORDER


def shift_sort_key(shift: str) -> tuple[int, str]:
    """並べ替えの鍵。知らない直は後ろへ回して五十音。"""
    return (SHIFT_ORDER.get(shift, 99), shift)


def day_rate_pct(rows: list["ShiftAggregate"]) -> float:
    """その日の稼働率(%)。**直ごとの平均ではなく、合計から出し直す。**

    直の平均にすると、1直だけ動いた日と3直とも動いた日が同じ数字に
    なります。止まっていた分を足し合わせてから、直の数だけ伸ばした
    1440分で割り直します。
    """
    if not rows:
        return 0.0
    stopped = sum(r.total_stop_minutes for r in rows)
    whole = float(MINUTES_PER_DAY * len(rows))
    return max(0.0, (whole - stopped) / whole * 100)


def _accumulate(existing: ShiftAggregate, addition: ShiftAggregate) -> None:
    existing.sheet_count += addition.sheet_count
    existing.weight_kg += addition.weight_kg
    existing.work_minutes += addition.work_minutes
    existing.management_loss_minutes += addition.management_loss_minutes
    existing.unplanned_stop_minutes += addition.unplanned_stop_minutes
    existing.handling_stop_minutes += addition.handling_stop_minutes


def aggregate_day(records: list[tuple[HeaderRecord, list[DetailRecord]]]) -> list[ShiftAggregate]:
    """port of ``DayShift_Agg``。同じ(報告日,ライン,直)で複数回保存(複数
    ページ)されていれば合算して1行にまとめる。並び順は 1直→2直→3直→日勤→
    その他(五十音)。"""
    totals: dict[tuple[str, str, str], ShiftAggregate] = {}
    for header, details in records:
        key = (header.report_date, header.line, header.shift)
        shift_agg = aggregate_shift(header, details)
        if key not in totals:
            totals[key] = shift_agg
        else:
            _accumulate(totals[key], shift_agg)

    return sorted(
        totals.values(),
        key=lambda a: (_SHIFT_ORDER.get(a.shift, 99), a.shift),
    )


# ======================================================================
# 停止の項目ごと (VBA ``Stop_Agg`` → ``停止グラフ挿入``)
# ======================================================================
#: 文字種から、集計上の分類名へ。`ShiftAggregate` の3つのバケツと同じ並び
STOP_KINDS: dict[str, str] = {
    "数値": "管理ロス停止",
    "カタカナ": "突発停止",
    "アルファベット": "ハンドリング停止",
}

#: 人に見せる名前。VBA の変数名(``StopM`` / ``StopH``)に添えてあった
#: 呼び名で、現場で使われているのはこちらです。
#:
#: **溜めてある文字(`STOP_KINDS` の右)とは分けてあります。**
#: `packing_stop_detail.stop_kind` には保存した時点の分類がそのまま
#: 入っていて、`packing_agg.by_key` もその文字で足しています ──
#: 右側を書き換えると、**去年保存した行が全部「その他」に落ちます。**
#: 見せる名前を変えたいときは、こちらだけを変えてください。
STOP_KIND_LABELS: dict[str, str] = {
    "管理ロス停止": "管理ロス設備停止",      # VBA StopM。数字の記号
    "突発停止": "段取り・突発停止",          # VBA StopH。カタカナの記号
    "ハンドリング停止": "ハンドリング停止",  # 英字の記号
    "その他": "その他",
}

#: 出す順。**重い順でも五十音でもなく、紙とマスタの並び**
#: (作業停止時間内訳_1 → _2 → _3)
STOP_KIND_ORDER: tuple[str, ...] = (
    "管理ロス停止", "突発停止", "ハンドリング停止", "その他")


def stop_kind_label(kind: str) -> str:
    """溜めてある分類名 → 人に見せる名前。知らない値はそのまま返す。"""
    return STOP_KIND_LABELS.get(kind, kind or "その他")


def stop_kind_sort_key(kind: str) -> int:
    """分類の並び。知らない値は最後へ。"""
    try:
        return STOP_KIND_ORDER.index(kind)
    except ValueError:
        return len(STOP_KIND_ORDER)


@dataclass
class StopItem:
    """停止1項目ぶん。VBA ``Stop_Agg`` が (項目, 回数, 時間) で出していたもの。"""

    code: str
    label: str
    kind: str
    count: int = 0
    minutes: float = 0.0

    @property
    def text(self) -> str:
        """画面に出す名前。記号だけでは何のことか分からない。"""
        return f"{self.code} {self.label}".strip() if self.label else self.code


def stop_items(records: list[tuple[HeaderRecord, list[DetailRecord]]],
               labels: dict[str, str] | None = None) -> list[StopItem]:
    """port of ``Stop_Agg``。停止の**記号ごと**に、回数と分を積む。

    【なぜ3分類の合計だけでは足りないのか】
    「突発停止が 120分」までは `ShiftAggregate` で出ますが、それが
    ﾌｫｰｸ待ちなのか機械の突発なのかは出ません。**手を打てるのは項目まで
    降りたとき**なので、VBA も停止集計シートを作って円グラフにして
    いました(`停止グラフ挿入`)。

    `labels` は記号 → 内訳名(`作業停止時間内訳_1/2/3` の「内訳」)。
    渡さなければ記号だけで出します。

    並びは**時間の長い順** ── 長いものから見るための表です。
    """
    names = labels or {}
    found: dict[str, StopItem] = {}
    for _, details in records:
        for d in details:
            for code_field, minutes_field in _STOP_PAIRS:
                code = (getattr(d, code_field) or "").strip()
                if not code:
                    continue
                kind = STOP_KINDS.get(classify_stop_code(code), "その他")
                item = found.get(code)
                if item is None:
                    item = StopItem(code=code, label=names.get(code, ""), kind=kind)
                    found[code] = item
                # **回数は「その記号が書かれた回数」。** 時間が空でも
                # 1回は1回(VBA も名前があれば数えていた)
                item.count += 1
                item.minutes += _minutes(getattr(d, minutes_field))
    return sorted(found.values(), key=lambda i: (-i.minutes, i.code))


def aggregate_by_date(records: list[tuple[HeaderRecord, list[DetailRecord]]]) -> list[ShiftAggregate]:
    """複数日にまたがる records を、暦日1日ぶんずつ(その日の全直を合算)に
    まとめる。グラフの期間推移表示(月初〜指定日のデフォルト、期間指定)で
    使う。``shift`` フィールドは合算後の目印として "全直" 固定にする。"""
    totals: dict[str, ShiftAggregate] = {}
    for header, details in records:
        key = header.report_date
        shift_agg = aggregate_shift(header, details)
        if key not in totals:
            totals[key] = ShiftAggregate(report_date=header.report_date, line=header.line, shift="全直")
        _accumulate(totals[key], shift_agg)

    return sorted(totals.values(), key=lambda a: parse_business_date(a.report_date) or date.min)
