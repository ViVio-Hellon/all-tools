"""集計用CSV出力 (VBA ``Print_All_Agg`` → ``Agg_OutPut`` / ``Aggre_Calcul``)

【VBAの「集計シート」は2つの顔を持っていた】
`Print_All_Agg` が作っていた集計シートには、**別々のものが2つ**
載っていました。

    上の帯(2〜4行目) … 直ごと・1日ぶんの合計(`Aggre_Calcul` が計算)
    表(8〜151行目)   … **その日の明細行を全部並べたもの**(`Agg_OutPut`)

下の表がなぜ要るかというと、**紙に載らない欄がそこにしか出ない**から
です。`Agg_OutPut` は印刷シートの 40〜45列(AN〜AS = 用途コード / 用途名 /
納入先 / 包装仕様書No / コイル縦割 / コイル横縦割)を拾って集計シートの
31〜34・43〜44列へ書いていました。印刷範囲の外なので**紙には出ず**、
集計シートだけが残す場所でした。

だからここも2本書きます。

    集計_<日付>_<ライン>.csv      直ごとの合計(上の帯)
    集計明細_<日付>_<ライン>.csv  その日の明細行ぜんぶ(下の表)

【出どころは「残してある集計」です (v3.29.0)】
以前はここだけ**打った明細を読んで、その場で計算し直して**いました。
グラフは残してある集計(`packing_report` ほか)を読むので、同じ数を
**2通りの道**で出していたことになります ── 数は合いますが、
「入力 → 集計 → CSV」の一直線で説明できませんでした。

いまは3本とも集計から作ります(式の説明は年月のフォルダに1つ)。

    daily_header / daily_detail     打つところ(ここだけが正)
             ↓ 保存のたびに投影する (services/summary.py)
    packing_report / _detail / _stop_detail
             ↓ 読むだけ
    画面のグラフ ・ 集計管理の表 ・ **このCSV**

読むときに集計がまだ無ければ、`summary.fill_missing` がその場で作って
から返します ── **CSVだけ空になる、が起きません。**

【そのぶん、明細CSVの見え方が少し変わります】
集計は**値**なので、打った文字そのものではありません。

    打っていない数の欄   "" のまま出していた → 0 が入るので "" に戻す
    時・分               "08" と打った文字   → 8 なので2桁に詰め直す
    重量 "1000"          1000.0            → "1000"(整数は小数点を出さない)

打った文字そのものが要るときは紙(`/report/nippou`)を見てください。

【欄の名前はどこから来るか】
`layout.py` が1か所で持っています ── 画面も紙もCSVも同じ名前になるよう
にするためで、片方だけ直して食い違うのを防ぎます。上下2段の見出しは
`layout.csv_header` が1行に畳みます(`作業停止① 記号`)。

Excelへ直接出力する案もあったが、追加ライブラリ(openpyxl等)が要る
うえレイアウトの自由度も低いため、標準の :mod:`csv` モジュールで
"Excel でそのまま開ける" CSV を書き出す方式にしている
(BOM付きUTF-8。Windows版Excelが文字化けせずに開ける)。
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .. import layout
from ..db.models import PackingReport
from ..db.repository import NippouRepository
from ..logic.aggregation import (
    ShiftAggregate,
    shift_sort_key,
    stop_kind_label,
    stop_kind_sort_key,
)
from ..logic.month_roll import safe_name
from ..logic.numeric import format_fixed
from ..logic.packing_agg import LotRow, stop_rollup
from ..logic.shift import parse_business_date

#: 直ごとの合計。**停止は3分類に分けたまま、合計も添えます** ──
#: グラフにするとき、積み上げ棒(3分類)と折れ線(停止合計)のどちらにも
#: そのまま使えるようにするためで、足し算をExcelでやり直させないため。
#:
#: 名前は現場の呼び名(`aggregation.STOP_KIND_LABELS`)です ──
#: 画面のドーナツの凡例と同じ文字にしてあります。
#: 停止3分類の列名。**1か所で作ります** ── 見出しと中身を別々に
#: 書くと、名前を直したときに片方だけ残ります
LOSS_COLUMN = f"{stop_kind_label('管理ロス停止')}(分)"
SUDDEN_COLUMN = f"{stop_kind_label('突発停止')}(分)"
HANDLING_COLUMN = f"{stop_kind_label('ハンドリング停止')}(分)"

FIELDNAMES = [
    "日付", "ライン", "直",
    "合計枚数", "合計重量(t)",
    "作業時間合計(分)", "稼働時間合計(分)", "操業時間(分)",
    LOSS_COLUMN, SUDDEN_COLUMN, HANDLING_COLUMN, "停止合計(分)",
    "稼働率(%)", "生産性(t/h)",
]

# ----------------------------------------------------------------------
# 明細のほう (VBA `Agg_OutPut` が集計シートの 8〜151行目に並べていたもの)
#
# 並びは**紙の左から右**。VBA の集計シートでは印刷範囲外の6つが
# 31〜34列と43〜44列に離れて置かれていましたが、それは集計シートの
# 空いている場所へ入れた結果で、紙(AN〜AS)では隣り合っています。
# **隣り合っているものは隣り合わせて出します。**
# ----------------------------------------------------------------------
#: 行を指す鍵
_KEY_FIELDS: tuple[str, ...] = ("日付", "ライン", "直", "ページ", "行")

#: 直に1つしかないもの。**行ごとに繰り返して入れます** ── CSVを
#: 並べ替えたり絞り込んだりしたときに、どの直の行かが分からなくなる
_HEADER_FIELDS: tuple[tuple[str, str], ...] = (
    ("worker_name", "作業者"), ("daytime_operation", "昼稼働"),
    ("reason", "作業コメント"),
)

DETAIL_FIELDNAMES: list[str] = [
    *_KEY_FIELDS,
    "係数処理ﾛｯﾄ数",
    *[layout.csv_header(c) for c in layout.COLUMNS],
    *[layout.csv_header(c) for c in layout.EXTRA_COLUMNS],
    *[label for _, label in _HEADER_FIELDS],
    # 紙にも共有の日報管理にも欄が無い、この端末だけの控え。
    # 月別の書き出しでは**最後の写し**になるので、ここにも残す
    "引当番号",
]

#: 紙の欄(`layout.COLUMNS` の family) → 残した集計(`LotRow`)の属性。
#:
#: **ここが写し戻しの全部です。** 集計を作るとき
#: (`packing_agg.build_shift_rows`)に打った欄から値を拾っているので、
#: その逆を1か所に書いておきます ── 対応を2か所に散らすと、欄が
#: 増えたときに片方だけ直して静かに落ちます。
#:
#: 作業停止①②③(S/TH/SS/THS/STH/THT)はここにありません。集計では
#: **縦に持っている**(`packing_stop_detail` が1停止1行)ので、
#: `_stop_cells` が `stop_no` を見て横へ戻します。
_LOT_FIELDS: dict[str, str] = {
    "LOT": "lot_no", "ZAI": "material_condition", "SIZ": "dimension",
    "KEN": "incoming_quantity",
    "KZ": "start_hour", "KH": "start_minute",
    "SZ": "end_hour", "SH": "end_minute",
    "HIT": "worker_count", "AI": "interleaf",
    "MAI": "packing_quantity", "TUT": "packing_package_count",
    "VC": "vc_type", "ET": "etc",
    "CON": "actual_quantity", "WEI": "actual_weight",
    "TIM": "work_time", "UNI": "unit_weight",
}

#: 印刷範囲のさらに外(紙の AN〜AS)。こちらも `LotRow` の属性へ
_EXTRA_LOT_FIELDS: dict[str, str] = {
    "others1": "purpose_code", "others2": "purpose_name",
    "others3": "delivery_destination", "others4": "packing_spec_no",
    "others5": "coil_vertical_split", "others6": "coil_horizontal_split",
}

#: 時・分の欄。**2桁に詰め直します** ── 紙は "08" で、集計は 8 です
_CLOCK_FIELDS = frozenset({"KZ", "KH", "SZ", "SH"})

#: 作業停止①②③の欄。(記号, 時間) の組を紙の並びで
_STOP_CELLS: tuple[tuple[str, str], ...] = (
    ("S", "TH"), ("SS", "THS"), ("STH", "THT"))


def _text(value: object) -> str:
    """文字の欄。`None` は空欄に。"""
    return "" if value is None else str(value)


def _number(value: object) -> str:
    """数の欄を、**打った形に近い文字**へ戻す。

    集計は値なので `1000.0` のように小数点が付きます。打った人は
    「1000」と書いたので、割り切れるときは小数点を出しません。

    **0 は空欄にします。** 打っていない欄は集計では 0 になっていて、
    そのまま出すと12行×30列が 0 で埋まります ── 打った日報を見ながら
    照らし合わせる表なので、打っていないところは空いているのが正です。
    """
    if value is None:
        return ""
    number = float(value)
    if number == 0:
        return ""
    if number == int(number):
        return str(int(number))
    return f"{number:.6f}".rstrip("0").rstrip(".")


def _clock(value: object) -> str:
    """時・分。**打っていなければ空欄、0時0分なら "00"。**

    `_number` と分けてあるのは、ここでは **0 が本当の値**だからです
    (0時00分に始めた行がある)。集計側も `None` と 0 を分けて持って
    います(`packing_agg._int`)。
    """
    if value is None or value == "":
        return ""
    return f"{int(value):02d}"


def _stop_cells(row: LotRow) -> dict[str, str]:
    """縦に持っている停止を、紙の横並び①②③へ戻す。

    記号の無い組は集計に残っていません(`build_stops`)。**空いた番号は
    空欄のまま**にします ── ②だけ書いた行を①へ詰めると、紙と
    見比べたときに合いません。
    """
    cells: dict[str, str] = {}
    by_no = {s.stop_no: s for s in row.stops}
    for index, (code_field, minutes_field) in enumerate(_STOP_CELLS, start=1):
        stop = by_no.get(index)
        cells[code_field] = _text(stop.stop_code) if stop else ""
        cells[minutes_field] = _number(stop.stop_minutes) if stop else ""
    return cells


def aggregate_row_dict(agg: ShiftAggregate) -> dict[str, str]:
    return {
        "日付": agg.report_date,
        "ライン": agg.line,
        "直": agg.shift,
        "合計枚数": format_fixed(agg.sheet_count, 0),
        "合計重量(t)": format_fixed(agg.weight_ton, 2),
        "作業時間合計(分)": format_fixed(agg.work_minutes, 0),
        "稼働時間合計(分)": format_fixed(agg.operating_minutes, 0),
        # 1440分 − 管理ロスだけ(VBA `Opetional`)。稼働時間とは引くものが
        # 違うので、**並べて置かないと取り違えます**
        "操業時間(分)": format_fixed(agg.operational_minutes, 0),
        LOSS_COLUMN: format_fixed(agg.management_loss_minutes, 0),
        SUDDEN_COLUMN: format_fixed(agg.unplanned_stop_minutes, 0),
        HANDLING_COLUMN: format_fixed(agg.handling_stop_minutes, 0),
        "停止合計(分)": format_fixed(agg.total_stop_minutes, 0),
        "稼働率(%)": format_fixed(agg.operating_rate_pct, 1),
        "生産性(t/h)": format_fixed(agg.productivity_t_per_h, 2),
    }


def build_daily_aggregate_rows(
    repo: NippouRepository, report_date: str, line: str
) -> list[ShiftAggregate]:
    """その日の直ごとの合計。**残してある集計を読むだけ。**

    以前はここで明細を読んで `aggregate_day` に掛けていました。数は
    同じですが、グラフとCSVで**別の道**を通ることになるので揃えました
    (`services/summary.day_rows`)。まだ集計の無い直は、`fill_missing`
    がその場で作ってから返します。
    """
    from ..services import summary

    return summary.day_rows(repo, report_date, line)


def write_daily_aggregate_csv(
    repo: NippouRepository, report_date: str, line: str, out_path: Path
) -> int:
    """集計用CSVをファイルへ書き出し、書き出した行数(直の数)を返す。"""
    rows = build_daily_aggregate_rows(repo, report_date, line)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for agg in rows:
            writer.writerow(aggregate_row_dict(agg))
    return len(rows)


# ----------------------------------------------------------------------
# 停止の内訳 ── **分類ごと、そのなかの項目ごと**
#
# 集計CSVには3分類の合計しか出ていませんでした。「段取り・突発停止が
# 120分」までは分かっても、それが機械の突発なのかﾌｫｰｸ待ちなのかは
# 出ません ── **手が打てるのは項目まで降りたときだけ**です。
# VBA も同じ理由で停止集計シートを別に作っていました(`Stop_Agg`)。
#
# 直を混ぜないのが肝です。日でまとめてしまうと「3直だけ長い」が
# 見えなくなり、**いちばん知りたい偏りが平らになります。**
# ----------------------------------------------------------------------
#: まとめの行か、1件ずつの行か
STOP_KIND_TOTAL = "直の合計"
STOP_KIND_ONE = "1件ずつ"

#: 紙の作業停止①②③
STOP_SLOT_LABELS = {1: "作業停止①", 2: "作業停止②", 3: "作業停止③"}

STOP_FIELDNAMES = [
    "日付", "ライン", "直", "区分", "分類", "記号", "内訳",
    "ページ", "行", "LOTNO", "停止の位置",
    "回数", "停止時間(分)", "その直の停止に占める割合(%)",
]


def stop_rows_of(lots: list[LotRow]) -> list[dict[str, str]]:
    r"""停止を**直ごと・記号ごと**に並べ、そのすぐ下に1件ずつ置く。

    出どころは残してある集計(`packing_stop_detail`)です。内訳名も分類も
    **保存した時点のものがそのまま入っている**ので、マスタを読み直す
    必要がありません ── 記号を付け替えても、去年のCSVは去年のままです。

    並びは 直 → 分類(マスタの1→2→3) → 時間の長い順。分類をまたいで
    長い順に並べると、色分けした表がまだらになって読めません。

    【まとめの下に、1件ずつを置く理由】
    以前は記号ごとの合計だけでした。「ﾌｫｰｸ待ち 45分」とまでは出るのに、
    **それがどの行で起きたのかが、このCSVからは辿れません。** 紙を
    開いて12行を目で追うか、集計明細CSVのほうを別に開いて突き合わせる
    ことになります ── 手を打つ相手(そのロット・その時間帯)に辿り着く
    のが、いちばん時間の掛かるところでした。

    そこで、まとめの行(`区分` = ``直の合計``)の**すぐ下**に、その記号の
    1件ずつ(`区分` = ``1件ずつ``)を並べます。ページ・行・LOTNO・紙の
    ①②③が入るので、**そのままその行を開けます。**

        直の合計  ﾌｫｰｸ待ち          45分  3回
        1件ずつ   ページ1 3行目  H5422S0 作業停止①  20分
        1件ずつ   ページ1 7行目  N7131T0 作業停止①  15分
        1件ずつ   ページ2 2行目  L715C50 作業停止②  10分

    足せば合計になるので、**合計だけ見たい人はまとめの行だけ拾えます**
    (Excel のフィルタで `区分` を絞れます)。
    """
    by_shift: dict[tuple[str, str, str], list[LotRow]] = {}
    for row in lots:
        by_shift.setdefault((row.work_date, row.line_name, row.shift),
                            []).append(row)

    rows: list[dict[str, str]] = []
    for key in sorted(by_shift, key=lambda k: (_date_key(k[0]), k[1],
                                               shift_sort_key(k[2]))):
        work_date, line_name, shift = key
        lots_here = by_shift[key]
        items = stop_rollup(lots_here)
        total = sum(stop.stop_minutes for stop, _ in items)
        items.sort(key=lambda pair: (stop_kind_sort_key(pair[0].stop_kind),
                                     -pair[0].stop_minutes,
                                     pair[0].stop_code))
        for stop, times in items:
            base = {
                "日付": work_date, "ライン": line_name, "直": shift,
                "分類": stop_kind_label(stop.stop_kind),
                "記号": stop.stop_code,
                "内訳": stop.stop_reason,
            }
            rows.append({
                **base,
                "区分": STOP_KIND_TOTAL,
                "ページ": "", "行": "", "LOTNO": "", "停止の位置": "",
                "回数": format_fixed(times, 0),
                "停止時間(分)": format_fixed(stop.stop_minutes, 0),
                # **その直の中での重さ。**1日の中での重さにすると、
                # 直が1つしか無い日と3つある日で読み方が変わります
                "その直の停止に占める割合(%)":
                    format_fixed(stop.stop_minutes / total * 100, 1)
                    if total > 0 else "",
            })
            rows.extend(_stop_occurrences(base, lots_here, stop.stop_code))
    return rows


def _stop_occurrences(base: dict[str, str], lots: list[LotRow],
                      code: str) -> list[dict[str, str]]:
    """その記号が**どの行で**効いたか。ページ → 行 → ①②③ の順。

    紙をめくる順です ── 読む人はページを開いて上から下へ目を落とすので、
    その順に並んでいないと、行ったり来たりしながら探すことになります。

    **割合は入れません。** 1件ぶんの割合を出すと、まとめの行の割合と
    並んだときにどちらを読むのか分からなくなります。合計に対する重さは
    まとめの行、どこで起きたかはこちら、と役目を分けます。
    """
    found: list[tuple[int, int, int, LotRow, object]] = []
    for lot in lots:
        for stop in lot.stops:
            if stop.stop_code == code:
                found.append((lot.page, lot.row_no, stop.stop_no, lot, stop))
    found.sort(key=lambda item: item[:3])
    return [{
        **base,
        "区分": STOP_KIND_ONE,
        "ページ": str(page), "行": str(row_no), "LOTNO": lot.lot_no,
        "停止の位置": STOP_SLOT_LABELS.get(stop_no, f"作業停止{stop_no}"),
        "回数": "1",
        "停止時間(分)": format_fixed(stop.stop_minutes, 0),
        "その直の停止に占める割合(%)": "",
    } for page, row_no, stop_no, lot, stop in found]


def _date_key(work_date: str):
    from datetime import date as _date

    return parse_business_date(work_date) or _date.min


def build_stop_rows(repo: NippouRepository, report_date: str,
                    line: str) -> list[dict[str, str]]:
    """その日の停止内訳。**残してある集計から。**"""
    from ..services import summary

    return stop_rows_of(summary.day_lot_rows(repo, report_date, line))


def write_daily_stop_csv(repo: NippouRepository, report_date: str,
                         line: str, out_path: Path) -> int:
    """停止内訳CSVを書き出し、書いた行数(項目の数)を返す。"""
    rows = build_stop_rows(repo, report_date, line)
    return write_stop_rows(rows, out_path)


def write_stop_rows(rows: list[dict[str, str]], out_path: Path) -> int:
    """組み立て済みの停止内訳を書き出す。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=STOP_FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return len(rows)


# ----------------------------------------------------------------------
# 計算内容 ── **その数がどこから来たか**
#
# 前は日のフォルダに `計算内容_<日付>_<ライン>.csv` を毎日出していました。
# 式と注記は毎日同じなので、v4.2.0 から**年月のフォルダに1つ**
# (`計算内容.csv`、`csv_legend.ensure_month`)にしています。式を組み立てる
# のは `logic/formula.py` の1か所だけ ── 画面の途中式と食い違いません。
# ----------------------------------------------------------------------


# ----------------------------------------------------------------------
# 明細のほう (`Agg_OutPut`)
# ----------------------------------------------------------------------
def detail_row_dict(report: Optional[PackingReport],
                    row: LotRow) -> dict[str, str]:
    """明細1行ぶん。**紙に出ない6つも入れる。**

    用途コード・用途名・納入先・包装仕様書No・コイル縦割・コイル横縦割は
    印刷範囲の外なので紙には出ません。VBA も集計シートにだけ書いて
    いました ── ここが唯一の出口です。

    `report` は直に1つしかないもの(作業者・昼稼働・作業コメント)を
    取るためだけに使います。**行ごとに繰り返して入れます** ── CSVを
    並べ替えたり絞り込んだりすると、どの直の行か分からなくなるので。
    """
    out: dict[str, str] = {
        "日付": row.work_date, "ライン": row.line_name,
        "直": row.shift, "ページ": str(row.page), "行": str(row.row_no),
        "係数処理ﾛｯﾄ数": _number(row.coefficient_lot_count),
    }
    stops = _stop_cells(row)
    for column in layout.COLUMNS:
        name = layout.csv_header(column)
        if column.family in stops:                # 作業停止①②③
            out[name] = stops[column.family]
            continue
        value = getattr(row, _LOT_FIELDS[column.family], None)
        if column.family in _CLOCK_FIELDS:
            out[name] = _clock(value)
        elif column.kind == "num" or column.kind == "calc":
            out[name] = _number(value)
        else:
            out[name] = _text(value)
    for column in layout.EXTRA_COLUMNS:
        out[layout.csv_header(column)] = _text(
            getattr(row, _EXTRA_LOT_FIELDS[column.family], ""))
    for field, label in _HEADER_FIELDS:
        out[label] = _text(getattr(report, field, "") if report else "")
    out["引当番号"] = _text(row.hiki_no)
    return out


def detail_rows_of(lots: list[LotRow],
                   reports: list[PackingReport]) -> list[dict[str, str]]:
    """ロット行を、日付→直→ページ→行の順にCSVの行へ。

    **空の行はもう入っていません。** 集計を作るときに落としてあります
    (`packing_agg.has_content`)── 12行のうち打ったのが3行なら、残り9行は
    紙の余白と同じで、並べる意味がないので。
    """
    by_key = {(r.work_date, r.line_name, r.shift): r for r in reports}
    return [detail_row_dict(by_key.get((row.work_date, row.line_name,
                                        row.shift)), row)
            for row in sorted(lots, key=lambda r: r.sort_key)]


def build_detail_rows(repo: NippouRepository, report_date: str,
                      line: str) -> list[dict[str, str]]:
    """その日の明細行。**残してある集計から。**"""
    from ..services import summary

    return detail_rows_of(summary.day_lot_rows(repo, report_date, line),
                          summary.for_date(repo, report_date, line))


def write_daily_detail_csv(
    repo: NippouRepository, report_date: str, line: str, out_path: Path
) -> int:
    """明細CSVを書き出し、書き出した行数を返す。"""
    return write_detail_rows(build_detail_rows(repo, report_date, line),
                             out_path)


def write_detail_rows(rows: list[dict[str, str]], out_path: Path) -> int:
    """組み立て済みの明細行を書き出す(月別の書き出しからも使う)。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=DETAIL_FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return len(rows)


_UNSAFE_FILENAME_CHARS = '\\/:*?"<>|'


def _name_parts(report_date: str, line: str) -> tuple[str, str]:
    parsed = parse_business_date(report_date)
    date_part = parsed.isoformat() if parsed else report_date
    safe_line = "".join(ch for ch in line
                        if ch not in _UNSAFE_FILENAME_CHARS).strip() or "Unknown"
    return date_part, safe_line


#: 出力先の中で、種類ごとに分ける名前。**同じ出力先を複数の用途で
#: 使うので、混ぜると探せません**(集計CSVと印刷用HTMLが同じ日の
#: フォルダに並ぶと、紙を探しているのにCSVが目に入ります)。
KIND_AGGREGATE = "集計"
KIND_PRINT = "印刷"


def line_dir(base_dir: Path, line: str, kind: str) -> Path:
    r"""ラインと種類で分けた根。``<指定のパス>\機側\集計``。

    【なぜラインで分けるのか】
    出力先には**共有のフォルダを指せます**(そのほうが他の人も開ける)。
    そこへ全ラインが同じ構成で書き込むと、`集計_2026-08-26_LS.csv` と
    `集計_2026-08-26_L1.csv` が同じフォルダに混ざります ── 自分の
    ラインのぶんを探すのに、名前の後ろを1件ずつ読むことになります。

    **指定パスから下はこちらで作ります。** 現場に「先にフォルダを
    作っておいてください」とは言えません(作り忘れれば出ないだけで、
    出ていないことに気づくのは後日です)。
    """
    return base_dir / safe_name(line) / kind


def dated_dir(base_dir: Path, report_date: str, line: str = "",
              kind: str = KIND_AGGREGATE) -> Path:
    r"""その日のフォルダ。``<指定のパス>\機側\集計\2026.09\12``。

    【なぜこの並びなのか】
    上から **ライン → 種類 → 年月 → 日**。探すときに絞る順です ──
    見る人はまず自分のラインを開き、次に集計か紙かを選び、それから
    月をたどります。

    【なぜ日まで分けるのか】
    1つのフォルダに出し続けると、1ラインでも**1日3本**、1か月で
    90本を超えます。ファイル名に日付は入っていますが、名前で並べても
    「先月の3日」を出すのに画面いっぱいの一覧を目で追うことになります。

    **ゼロ詰めの2桁**にしてあります(``9`` ではなく ``09``)── 名前順の
    並びが日付順になり、9月と10月が入れ替わりません。年月を
    ``2026.09`` と1つにまとめてあるのは、月別の書き出し(VBA から
    引き継いだ ``yyyy.mm``)と同じ読み方にするためです。

    日付が読めなければ日のフォルダを作らずに種類の下へ出します ──
    **置き場所が決まらないことより、出ないことのほうが困ります。**
    """
    root = line_dir(base_dir, line, kind) if line else base_dir
    parsed = parse_business_date(report_date)
    if parsed is None:
        return root
    return root / f"{parsed.year:04d}.{parsed.month:02d}" / f"{parsed.day:02d}"


def default_output_path(base_dir: Path, report_date: str, line: str) -> Path:
    r"""出力先(例: ``…\L-1\集計\2026.08\03\集計_2026-08-03_L-1.csv``)。

    フォルダで日が分かるのに**ファイル名にも日付を残します** ──
    メールに添えたり手元へ写したりした瞬間にフォルダから離れるので、
    名前だけで何の日のものか分からないと使えません。
    """
    date_part, safe_line = _name_parts(report_date, line)
    return (dated_dir(base_dir, report_date, line)
            / f"集計_{date_part}_{safe_line}.csv")


def default_detail_output_path(base_dir: Path, report_date: str, line: str) -> Path:
    """明細CSVのほう(例: ``集計明細_2026-08-03_L1.csv``)。"""
    date_part, safe_line = _name_parts(report_date, line)
    return (dated_dir(base_dir, report_date, line)
            / f"集計明細_{date_part}_{safe_line}.csv")


def default_stop_output_path(base_dir: Path, report_date: str,
                             line: str) -> Path:
    """停止内訳CSV(例: ``停止内訳_2026-08-03_L1.csv``)。"""
    date_part, safe_line = _name_parts(report_date, line)
    return (dated_dir(base_dir, report_date, line)
            / f"停止内訳_{date_part}_{safe_line}.csv")


def daily_output_paths(base_dir: Path, report_date: str,
                       line: str) -> dict[str, Path]:
    """その日に出す3本。**呼ぶ側が並びを覚えないで済むように**1か所で。

    鍵は書き出す関数と対で使うもので、画面にもログにも出ます。
    """
    return {
        "aggregate": default_output_path(base_dir, report_date, line),
        "detail": default_detail_output_path(base_dir, report_date, line),
        "stop": default_stop_output_path(base_dir, report_date, line),
    }


@dataclass(frozen=True)
class DailySet:
    """その日に書いた3本と、年月のフォルダの説明2つ。"""

    files: dict[str, tuple[Path, int]]
    #: その日のフォルダ(3本が入っている)
    folder: Path
    #: 年月のフォルダの `CSVの読み方.txt`
    legend: Path
    #: 年月のフォルダの `計算内容.csv`
    guide: Optional[Path] = None
    #: 説明を**このとき置いた**(無かった・古かった)ファイル
    placed: tuple[Path, ...] = ()

    @property
    def legend_reissued(self) -> bool:
        return bool(self.placed)

    def counts(self) -> dict[str, int]:
        return {key: count for key, (_, count) in self.files.items()}


def write_daily_set(repo: NippouRepository, report_date: str, line: str,
                    base_dir: Path) -> DailySet:
    """その日の3本をまとめて書く。

    **1本ずつ呼び分けさせません** ── 押したときと保存のあとの2か所から
    呼ぶので、片方に1本足し忘れると「ボタンでは出るのに自動では
    出ない」が起きます。

    列の説明(`CSVの読み方.txt`)と式の説明(`計算内容.csv`)は、**年月の
    フォルダに1つだけ**置きます。

        日フォルダ内に毎回入れる CSVの読み方、計算内容は年月フォルダに
        1回だけ入れてください 無ければ入れる

    毎回見に行き、**無いとき(消された・初めての月)だけ**書きます。中身が
    同じなら触りません。日付が読めず年月のフォルダが決まらないときは、
    3本と同じフォルダに置きます。
    """
    from . import csv_legend

    paths = daily_output_paths(base_dir, report_date, line)
    folder = paths["aggregate"].parent
    parsed = parse_business_date(report_date)
    month_folder = folder.parent if parsed is not None else folder
    files = {
        "aggregate": (paths["aggregate"], write_daily_aggregate_csv(
            repo, report_date, line, paths["aggregate"])),
        "detail": (paths["detail"], write_daily_detail_csv(
            repo, report_date, line, paths["detail"])),
        "stop": (paths["stop"], write_daily_stop_csv(
            repo, report_date, line, paths["stop"])),
    }
    placed = csv_legend.ensure_month(
        month_folder, parsed.year if parsed else None,
        parsed.month if parsed else None)
    return DailySet(files=files, folder=folder,
                    legend=month_folder / csv_legend.FILENAME,
                    guide=month_folder / csv_legend.GUIDE_FILENAME,
                    placed=tuple(placed))


@dataclass(frozen=True)
class Copy:
    """集計CSVの**2つ目の出力先**へ書いた結果(`config.SETTINGS.summary_csv_dirs`)。"""

    base: Path
    #: 書けたときの、開くと中身が見えるところ(日のフォルダ等)
    where: str = ""
    #: 書けなかった理由。空なら書けた
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def as_dict(self) -> dict[str, object]:
        return {"dir": str(self.base), "where": self.where,
                "ok": self.ok, "error": self.error}

    def text(self) -> str:
        """知らせに足す1句。"""
        if self.ok:
            return f"2つ目の出力先にも出しました({self.where or self.base})"
        return f"2つ目の出力先({self.base})には出せませんでした: {self.error}"


def write_copies(bases: list[Path], write) -> list[Copy]:
    """1つ目より後ろの出力先へ、**同じものを**書く。

        集計データの出力先を２つ設定したいです(使用、アクセス権の関係)

    `write(base)` は1つ目に書いたのと同じ書き方(戻り値の `str()` が
    知らせに出る場所)。**1つ落ちても1つ目は成功のまま** ── 2つ目は
    アクセス権の違うフォルダを指すことが前提なので、届かない日は普通に
    あります。そのときは落ちたことを知らせに載せて、黙りません。
    """
    from ..logging_setup import get_logger

    log = get_logger("reporting.csv_export")
    out: list[Copy] = []
    for base in bases:
        try:
            where = write(base)
        except Exception as exc:                  # noqa: BLE001 - 1つ目は書けている
            log.exception("集計CSVを2つ目の出力先へ書けませんでした: %s", base)
            out.append(Copy(base=base, error=str(exc)))
            continue
        out.append(Copy(base=base, where=str(where) if where is not None else ""))
    return out


@dataclass(frozen=True)
class PeriodSet:
    """期間ぶんの書き出し。**日ごとに1組**。"""

    days: list[tuple[str, DailySet]]
    #: 書けなかった日と、その理由。**黙って飛ばさない**
    failed: list[tuple[str, str]]
    #: 見に行ったが中身の無かった日の数(1日も操業していない日)
    skipped: int = 0

    @property
    def files(self) -> int:
        return sum(len(one.files) for _date, one in self.days)

    @property
    def folders(self) -> list[Path]:
        return [one.folder for _date, one in self.days]


def write_period_set(repo: NippouRepository, start, end, line: str,
                     base_dir: Path, *, always: str = "") -> PeriodSet:
    """期間ぶんの集計CSVを、**日ごとのフォルダへ**書く。

    【なぜ期間で出せる必要があるのか】
    日ごとの3本は、これまで**その日を保存したときにしか**出ませんでした。
    過去のぶん ── Excelから取り込んだ日や、この仕組みが入る前に保存した
    日 ── を出す手立てが、月まるごと(`month_export`)しかありません。
    グラフは期間で見るのに、その期間のCSVが出せないのは筋が通りません。

    **中身のある日だけ書きます。** 操業していない日まで空のCSVを並べると、
    フォルダを開いたときに「この日は出し忘れ」と「この日は休み」が
    見分けられなくなります。見に行って空だった日は数だけ返します。

    `always` は**名指しされた1日**です。そこだけは中身が無くても書きます
    ── 「その日を出せ」と言われて何も出ないと、書き出しに失敗したのか
    もともと空なのかが分かりません(月まるごとの書き出しが空でも書くのと
    同じ理由)。期間を指定したときは名指しではないので付けません。

    1日書けなくても止めません ── 共有へ届かない日が1つあるだけで
    残り全部が出ないほうが困ります。
    """
    from ..services import summary

    reports = summary.for_period(repo, start, end, line)
    # 並びは日付順。**集合で持つと出る順が揺れる**(どこまで出たかが読めない)
    seen: list[str] = []
    for report in reports:
        if report.work_date and report.work_date not in seen:
            seen.append(report.work_date)
    if always and always not in seen:
        seen.append(always)
    seen.sort(key=lambda text: (parse_business_date(text) or start, text))

    days: list[tuple[str, DailySet]] = []
    failed: list[tuple[str, str]] = []
    for work_date in seen:
        try:
            days.append((work_date,
                         write_daily_set(repo, work_date, line, base_dir)))
        except Exception as exc:                  # noqa: BLE001 - 残りは出す
            failed.append((work_date, str(exc)))
    return PeriodSet(days=days, failed=failed)


# ----------------------------------------------------------------------
# 集計管理の画面から書き出す
#
# 画面に出ている表を**そのまま**ファイルにします。画面と書き出しで
# 列や並びが違うと、突き合わせるときに毎回読み替えることになるので。
# ----------------------------------------------------------------------
def write_table(table, out_path: Path) -> int:
    """`presenters/agg_admin.Table` を1本のCSVにする。書いた行数を返す。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(table.columns)
        writer.writerows(table.rows)
    return len(table.rows)


def standard_time_output_path(base_dir: Path, today, line: str, *,
                              by_worker: bool = False, by_operator: bool = False) -> Path:
    """例: ``標準作業時間_2026-09-26_L-1.csv``(期間ではなく、その時点の標準)。

    作業者ごとは ``標準作業時間_作業者ごと_…``、オペレーター構成ごとは
    ``標準作業時間_オペレーター構成ごと_…``(班ごとのCSVを上書きしない)。
    """
    safe_line = "".join(ch for ch in line
                        if ch not in _UNSAFE_FILENAME_CHARS).strip() or "Unknown"
    head = ("標準作業時間_作業者ごと" if by_worker
            else "標準作業時間_オペレーター構成ごと" if by_operator else "標準作業時間")
    return base_dir / f"{head}_{today}_{safe_line}.csv"


def works_output_path(base_dir: Path, today, line: str, criteria) -> Path:
    """例: ``同条件作業_2026-09-26_L1_H176_1P0001_8×1528×3053.csv``

    OR のときは欄のあいだを「又は」でつなぎます(AND と同じ名前にしない)。
    ファイル名が長くなりすぎないよう、条件の部分は 80 文字で切ります。
    """
    word = "_" if criteria.join == "and" else "_又は_"
    parts = [("・".join(values)) for _, _, values in criteria.fields]
    cond = word.join(parts)
    safe = "".join(ch for ch in cond if ch not in _UNSAFE_FILENAME_CHARS).strip()[:80] or "-"
    safe_line = "".join(ch for ch in (line or "全ライン")
                        if ch not in _UNSAFE_FILENAME_CHARS).strip() or "-"
    return base_dir / f"同条件作業_{today}_{safe_line}_{safe}.csv"


def power_output_path(base_dir: Path, today, line: str, group_label: str) -> Path:
    """例: ``梱包力_班ごと_2026-10-03_L1.csv``"""
    safe_line = "".join(ch for ch in (line or "全ライン")
                        if ch not in _UNSAFE_FILENAME_CHARS).strip() or "-"
    safe_group = "".join(ch for ch in group_label
                         if ch not in _UNSAFE_FILENAME_CHARS).strip() or "-"
    return base_dir / f"梱包力_{safe_group}ごと_{today}_{safe_line}.csv"


def agg_output_path(base_dir: Path, key: str, start, end, line: str) -> Path:
    """例: ``集計_ロット別_2026-08-01_2026-08-31_L1.csv``"""
    names = {"days": "日別", "shifts": "直別", "by_key": "ロット別",
             "lots": "ロット一覧", "push_log": "共有保存の履歴"}
    safe_line = "".join(ch for ch in line
                        if ch not in _UNSAFE_FILENAME_CHARS).strip() or "Unknown"
    return base_dir / (f"集計_{names.get(key, key)}_{start}_{end}"
                       f"_{safe_line}.csv")
