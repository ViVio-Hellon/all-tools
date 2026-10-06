"""印刷用フォーマットのHTML生成(VBAの ``配列_OutPut`` 相当)。

追加ライブラリ(PDF生成系等)を使わない制約の中で「任意のタイミングで
印刷」を実現するため、印刷用にスタイリングしたHTMLファイルを書き出し、
既定のブラウザで開く方式にしている(呼び出し側 ``ui/print_preview_window.py``
がブラウザを開く。ここではHTML文字列の組み立てとファイル書き出しだけを
受け持つ)。ブラウザの「印刷」(Ctrl+P)でそのまま印刷できるよう、
``@media print`` で余白/改ページを調整している。

【列の並びと見出しは `nippou/layout.py` が持つ】
入力画面と別々に持っていたので、同じ欄が画面では「材質」、紙では
「材・調質」になっていました。現場の正は紙(梱包実績日報表)なので、
**紙の並びを1か所に書いて、画面も紙もそこを読みます。**

作業時間(分)と単重は紙に載りません(印刷範囲の外にある計算結果)。
`layout.PRINT_COLUMNS` がそれを外したものを返します。

【紙の頭に集計を載せる】
VBA は印刷フォーマットと集計フォーマットが**別のシート**で、紙も別々に
出ていました。ここでは残してある集計(`services/summary.py`)を、明細の
**上**に一段はさみます ── 刷った紙だけを見る人(現場に貼る、朝礼で配る)
にとって、その直がどうだったかは明細を足し算しないと分からないので。

集計は渡されたときだけ出ます。渡さなければ、これまでどおり明細だけの
紙になります。
"""
from __future__ import annotations

import html
from pathlib import Path
from typing import Optional, Sequence

from .. import constants, layout
from ..db.models import DetailRecord, HeaderRecord, PackingReport
from ..logic import line_names
from ..logic.numeric import format_fixed
from ..logic.packing_agg import StopRow
from . import paper

_STYLE = """
body { font-family: "Meiryo UI", "Yu Gothic", sans-serif; font-size: 12px; margin: 16px; }
h1 { font-size: 16px; margin: 0 0 8px; }
table { border-collapse: collapse; width: 100%; margin-top: 8px; }
th, td { border: 1px solid #444; padding: 2px 4px; text-align: center; white-space: nowrap; }
th { background: #eee; }
/* ------------------------------------------------------------------
   紙の頭 ── 実物の Excel と同じ並び

   左に 作業日・直 / 梱包ライン / 作業者名 の3行、中央に題、右に
   昼稼働(有・無)と「ヨ：その他（理由を記載）」。実物を読んで写しました
   (`2026年8月31日3直_HVC` の A1:O3)。
   ------------------------------------------------------------------ */
.head { display: flex; align-items: flex-start; gap: 12px; margin-bottom: 4px; }
/* **全幅にしない。** `table { width: 100% }` がここにも効いてしまい、
   左の3行が紙いっぱいに伸びて、題が縦書きのように潰れていました */
.head__keys { border-collapse: collapse; width: auto; margin: 0; flex: none; }
.head__keys th, .head__keys td {
  border: 1px solid #444; padding: 1px 6px; text-align: left;
  white-space: nowrap; font-size: 11px; background: #fff; font-weight: normal;
}
.head__keys th { background: #eee; }
.head__title { flex: 1 1 auto; display: flex; align-items: center;
               justify-content: center; align-self: stretch; min-width: 0; }
.head__title h1 { font-size: 16px; margin: 0; letter-spacing: 2px;
                  white-space: nowrap; }
.head__right { display: flex; gap: 8px; align-items: stretch; flex: none; }
.head__box { border: 1px solid #444; padding: 1px 6px; font-size: 11px;
             min-width: 5em; }
.head__box b { display: block; font-weight: normal; background: #eee;
               margin: -1px -6px 2px; padding: 0 6px; }
.head__box--reason { min-width: 16em; }
/* 紙の3行目にある注意書き */
.note { font-size: 11px; margin: 4px 0 0; }
.generated-at { margin-top: 12px; font-size: 10px; color: #666; }
.backfill-stamp { display: inline-block; margin: 4px 0 0; padding: 1px 8px;
  border: 1.5px solid #000; font-size: 12px; font-weight: 700; }
/* ------------------------------------------------------------------
   表の下の合計 ── 紙の21〜24行目

       S21:T21  負荷計算後      → 係数Lot数
       U21:W21  直実績合計      → ﾛｯﾄ数 / 枚数 / 重量

   **重量は Kg と T の2行**です。実物は1つのセルに改行で
   「4379.4 Kg」「4.38 T」と入っていました(W24)。
   ------------------------------------------------------------------ */
.totals { display: flex; justify-content: flex-end; margin-top: 4px; }
.totals table { width: auto; margin: 0; }
.totals th, .totals td { padding: 1px 10px; }
.totals .kg { display: block; }
.totals .ton { display: block; font-size: 11px; }

/* 紙には無い、このツールが足した集計。**紙の姿のあとに置く** */
.agg { border: 1px solid #444; margin: 12px 0 0; padding: 4px 6px; }
.agg h2 { font-size: 12px; margin: 0 0 4px; }
.agg dl { display: grid; grid-template-columns: repeat(6, auto);
          gap: 2px 16px; margin: 0; justify-content: start; }
.agg div { white-space: nowrap; }
/* 停止の並びは**折り返す**。長い直だと1行が紙の幅を超え、ブラウザが
   紙ごと縮めて、余白が紙の端から 5mm を割っていました */
.agg .stops { margin-top: 4px; font-size: 11px; white-space: normal; }
/* 1ページ = 1枚の紙。**2枚目からは改ページ** ── 直ぶんをまとめて開いても、
   刷れば紙の枚数は1ページずつに分かれる */
.sheet + .sheet { page-break-before: always; break-before: page; }
@media screen {
  /* 画面で続けて見るときだけ、紙の切れ目が分かるように */
  .sheet + .sheet { border-top: 2px dashed #999; margin-top: 24px;
                    padding-top: 16px; }
}
@media print {
  .generated-at { display: none; }
  /* **紙に収める。** 22列の表は紙の幅より広く、ブラウザが 91% ほどに
     縮めて刷っていました ── 縮めると余白も縮むので、端から 5mm を
     割ることがあります。縮めたのと同じ大きさで組んでおきます */
  body { font-size: 11px; }
  th, td { padding: 1px 2px; }
  .sheet { break-inside: avoid; }
}
""" + paper.TOOLBAR_CSS + paper.page_css("A4 landscape", ".sheet")   # 梱包実績日報表は横長の紙


def _esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


def _head_html(header: HeaderRecord) -> str:
    """紙の頭(1〜3行目)。**実物の Excel と同じ並びにする。**

    実物(`2026年8月31日3直_HVC`)の A1:O3 を読むと、こう並んでいます:

        A1 作業日・直   B1 2026年8月31日3直   E1 梱包実績日報表
        A2 梱包ライン   B2 HVC                N1 昼稼働 / N2 有・無
        A3 作業者名     B3 (氏名)             O1 ヨ：その他（理由を記載）
                        E3 ※作業停止時間は…   O2 (理由の本文)

    以前はここを「ラベル: 値」の横並び1行にしていました。**紙とは
    別物の見た目**で、刷ったものを現物と見比べる人が迷います。左の3行・
    中央の題・右の2枠、という形へ寄せました。

    **ページは右の枠に入れます。** 紙にはページ欄がありませんが(1枚=1ページなので)、
    直ぶんをまとめて開けるこちらでは、どの紙かが分からないと困ります。
    """
    keys = [
        ("作業日・直", f"{header.report_date}{header.shift}"),
        ("梱包ライン", line_names.label(header.line)),
        ("作業者名", header.worker),
    ]
    rows = "".join(
        f"<tr><th>{_esc(label)}</th><td>{_esc(value)}</td></tr>"
        for label, value in keys)
    # 昼稼働は紙では「有・無」を丸で囲む。刷ったものに手で丸を付ける
    # ことはないので、**選ばれたほうだけ**を出します
    day = _esc(header.day_shift or "無")
    return f'''<div class="head">
<table class="head__keys">{rows}</table>
<div class="head__title"><h1>{_esc(layout.SHEET_TITLE)}</h1></div>
<div class="head__right">
<div class="head__box"><b>ページ</b>{_esc(header.page)}</div>
<div class="head__box"><b>昼稼働</b>{day}</div>
<div class="head__box head__box--reason"><b>ヨ：その他（理由を記載）</b>{_esc(header.reason)}</div>
</div>
</div>'''


def _totals_html(header: HeaderRecord,
                 summary: Optional[PackingReport] = None) -> str:
    """表の下の合計(紙の21〜24行目)。

        S21:T21  負荷計算後      → 係数Lot数
        U21:W21  直実績合計      → ﾛｯﾄ数 / 枚数 / 重量

    **重量は Kg と T の2行**にします。実物は1つのセルに改行で
    「4379.4 Kg」「4.38 T」と入っていました(W24)── 現場はトンのほうで
    話すので、Kg だけだと毎回割り算することになります。

    トンは**読むための添え物**なので、数字でなければ黙って出しません
    (`calculations.tons` が空を返す)。紙も空欄のことがあります。

    【空いていたら集計から入れる】
    ﾛｯﾄ数と係数Lot数は、機側・NS1 では両方、AIM では ﾛｯﾄ数だけ
    (`logic/load_factor`)保存のたびに計算して入りますが、**それ以外は手入力**です。
    打っていなければ空のまま ── 取り込んだ過去のぶんも、CSVにこの2つが
    無いので空です。

    そのまま刷ると、**同じ紙の下にある集計には「Lot数 3」と出ているのに、
    合計欄だけ空**という紙ができます。読んだ人はどちらが本当か分かり
    ません。数えた値があるならそれを出します ── 出どころは集計と同じ
    1つで、ここで足し算をやり直しはしません。
    """
    from ..logic.calculations import tons

    lot_count = str(header.lot_count or "")
    coefficient = str(header.coefficient_lot_count or "")
    if summary is not None:
        if not lot_count.strip():
            lot_count = str(summary.total_lot_count)
        if not coefficient.strip():
            coefficient = format_fixed(summary.coefficient_lot_count, 2)

    ton = tons(str(header.weight_kg or ""))
    weight = f'<span class="kg">{_esc(header.weight_kg)} Kg</span>'
    if ton:
        weight += f'<span class="ton">{_esc(ton)} T</span>'
    return f'''<div class="totals">
<table>
<thead>
<tr><th>負荷計算後</th><th colspan="3">直実績合計</th></tr>
<tr><th>係数Lot数</th><th>ﾛｯﾄ数</th><th>枚数</th><th>重量</th></tr>
</thead>
<tbody>
<tr>
<td>{_esc(coefficient)}</td>
<td>{_esc(lot_count)}</td>
<td>{_esc(header.count)}</td>
<td>{weight}</td>
</tr>
</tbody>
</table>
</div>'''


def _summary_html(summary: Optional[PackingReport],
                  stops: Optional[Sequence[tuple[StopRow, int]]]) -> str:
    """その直の集計を一段ぶん。**渡されなければ何も出しません。**

    出すのは残してある値そのままで、ここで足し算し直しません ──
    紙とグラフと共有DBで数字が食い違わないのは、出どころが1つだから。
    丸めるのは**ここだけ**です(早く丸めると端数が積もります)。
    """
    if summary is None:
        return ""
    pairs = [
        ("枚数", format_fixed(summary.total_quantity, 0)),
        ("重量(Kg)", format_fixed(summary.total_weight, 1)),
        ("Lot数", str(summary.total_lot_count)),
        ("係数Lot数", format_fixed(summary.coefficient_lot_count, 2)),
        ("作業(分)", format_fixed(summary.work_time, 0)),
        ("管理ロス(分)", format_fixed(summary.equipment_stop_total, 0)),
        ("突発(分)", format_fixed(summary.setup_stop_total, 0)),
        ("ﾊﾝﾄﾞﾘﾝｸﾞ(分)", format_fixed(summary.handling_stop_total, 0)),
        ("稼働(分)", format_fixed(summary.operating_time, 0)),
        ("稼働率(%)", format_fixed(summary.operating_rate, 1)),
        ("生産性(t/h)", format_fixed(summary.productivity, 2)),
    ]
    body = "".join(f"<div><b>{_esc(label)}:</b> {_esc(value)}</div>"
                   for label, value in pairs)

    # 停止は**長い順**。打てる手があるのは項目まで降りたときだけ
    stops_html = ""
    if stops:
        text = " / ".join(
            f"{_esc(stop.text)} {_esc(format_fixed(stop.stop_minutes, 0))}分"
            f"×{times}" for stop, times in stops)
        stops_html = f'<div class="stops"><b>停止:</b> {text}</div>'

    note = f"{summary.pages}ページぶん" if summary.pages > 1 else ""
    title = f"{_esc(summary.shift)} の集計(紙には無い、このツールの追加)"
    if note:
        title += f"({_esc(note)})"
    return (f'<div class="agg"><h2>{title}</h2>'
            f"<dl>{body}</dl>{stops_html}</div>")


def _sheet_title(header: HeaderRecord) -> str:
    return (f"日報 {header.report_date} {line_names.label(header.line)} {header.shift} "
            f"第{header.page}ページ")


def _print_hint(pages: int) -> str:
    """帯に添える一言。**何枚出るか**を先に言う(直ぶんは紙が何枚にもなる)。"""
    close = "刷り終わったら、この窓は閉じてかまいません"
    if pages > 1:
        return f"全{pages}ページ ─ A4 横で{pages}枚に分かれて刷れます。{close}"
    return f"A4 横1枚で刷れます。{close}"


def _document(title: str, body: str, *, pages: int = 0) -> str:
    """紙1枚ぶん / 直ぶんに共通の外側。

    `pages` があれば、上に「印刷する」の帯を付けます(v4.6.0)── 以前は
    帯が無く、**Ctrl+P を知らないと刷れませんでした**(梱包資材重量計算の
    紙には付いていた)。帯は紙に出ません。刷るものが無い窓には付けません。
    """
    toolbar = paper.toolbar_html(_print_hint(pages)) + "\n" if pages else ""
    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>{_esc(title)}</title>
<style>{_STYLE}</style>
</head>
<body>
{toolbar}{body}</body>
</html>
"""


def _sheet_html(
    header: HeaderRecord,
    details: list[DetailRecord],
    *,
    generated_at: str = "",
    summary: Optional[PackingReport] = None,
    stops: Optional[Sequence[tuple[StopRow, int]]] = None,
    stamp: str = "",
) -> str:
    """紙1枚ぶんの中身(`<section class="sheet">`)。

    `stamp` は「後日作成(…)」の印(v4.0.0)。**刷った紙だけ見ても、後から
    作ったページだと分かる**ように見出しの下へ枠で出します(画面だけの印は
    紙になった時点で消えるため)。

    **`<html>` は付けません。** 直ぶんをまとめて1つの窓に出せるように、
    1枚ぶんを部品として切り出してあります。
    """
    columns = layout.PRINT_COLUMNS
    head_html = _head_html(header)

    # 上段 ── 結合される見出しだけ colspan を持ち、単独の列は2段ぶん高い
    top: list[str] = []
    index = 0
    for group, span in layout.column_groups(columns):
        column = columns[index]
        if group:
            top.append(f'<th colspan="{span}">{_esc(group)}</th>')
        else:
            top.append(f'<th rowspan="2">{_esc(column.label)}</th>')
        index += span
    # 下段 ── 上段が結合されている列の内訳
    bottom = "".join(f"<th>{_esc(c.label)}</th>" for c in columns if c.group)

    # ------------------------------------------------------------------
    # **12行、いつも出します。** 打っていない行も空のまま刷ります。
    #
    # 紙(梱包実績日報表)は12行の罫線が引かれた用紙で、打った数で行数が
    # 変わったりしません。打った行だけ出すと、
    #
    #   ・3行しか打っていない直の紙が、表の途中で切れた別物に見える
    #   ・刷ったあと手で書き足す余白が無い(現場は普通にやります)
    #   ・行番号が飛んでいるのか、そこで終わりなのかが読めない
    #
    # 無意味に見える空行は、**紙としては意味があります。**
    #
    # 打ってある行は行番号で置きます ── 歯抜け(2行目と5行目だけ)でも、
    # 紙の2行目・5行目に出ます。順に詰めると、画面と紙で行番号が
    # ずれます(理由欄が「3行目: ○○」と行番号で書かれているので、
    # ずれると指している行が変わります)。
    # ------------------------------------------------------------------
    by_row = {d.row_no: d for d in details}
    body_rows = []
    for row_no in range(1, constants.ROW_COUNT + 1):
        d = by_row.get(row_no)
        cells = "".join(
            f"<td>{_esc(getattr(d, c.field, '') if d else '')}</td>"
            for c in columns)
        body_rows.append(f"<tr><td>{row_no}</td>{cells}</tr>")
    # 12行に収まらない行があれば、**捨てずに下へ足す。** 起きないはず
    # ですが、起きたときに黙って消えるほうが困ります
    for row_no in sorted(n for n in by_row if n > constants.ROW_COUNT or n < 1):
        d = by_row[row_no]
        cells = "".join(f"<td>{_esc(getattr(d, c.field, ''))}</td>" for c in columns)
        body_rows.append(f"<tr><td>{row_no}</td>{cells}</tr>")

    title = _sheet_title(header)
    stamp_html = f'<div class="backfill-stamp">{_esc(stamp)}</div>' if stamp else ""
    return f"""<section class="sheet">
{head_html}
{stamp_html}
<div class="note">{_esc(layout.STOP_TIME_NOTE)}</div>
<table>
<thead>
<tr><th rowspan="2">行</th>{''.join(top)}</tr>
<tr>{bottom}</tr>
</thead>
<tbody>
{''.join(body_rows)}
</tbody>
</table>
{_totals_html(header, summary)}
{_summary_html(summary, stops)}
<div class="generated-at">生成日時: {_esc(generated_at)} / {_esc(title)}</div>
</section>
"""


def build_print_html(
    header: HeaderRecord,
    details: list[DetailRecord],
    *,
    generated_at: str = "",
    summary: Optional[PackingReport] = None,
    stops: Optional[Sequence[tuple[StopRow, int]]] = None,
    stamp: str = "",
) -> str:
    """port of ``配列_OutPut``: 1件(報告日+ライン+直+ページ)の印刷用HTMLを組み立てる。

    紙(梱包実績日報表)と同じ姿にする ── 見出しは2段で、
    「梱包作業時間」の下に開始・終了がぶら下がる。

    `summary` を渡すと、明細の**上**にその直の集計が一段入ります。
    渡さなければ明細だけの紙になります(VBAの印刷フォーマットと同じ姿)。
    """
    return _document(
        _sheet_title(header),
        _sheet_html(header, details, generated_at=generated_at,
                    summary=summary, stops=stops, stamp=stamp),
        pages=1)


def build_shift_html(
    pages: Sequence[tuple[HeaderRecord, list[DetailRecord]]],
    *,
    generated_at: str = "",
    summary: Optional[PackingReport] = None,
    stops: Optional[Sequence[tuple[StopRow, int]]] = None,
    stamps: Optional[dict[int, str]] = None,
) -> str:
    """**1直ぶんを1つの窓に。** ページのあいだで改ページします。

    `stamps` はページ番号 → 「後日作成(…)」の印(後から作ったページだけ)。

    紙はページごとに1枚ずつ出ますが、開く窓をページの数だけ増やすのは
    仕組みの都合で、刷る人の都合ではありません ── 3ページある直を刷るのに
    3回開いて3回 Ctrl+P、では取りこぼします。1つ開いて1回刷れば、
    紙は3枚出ます。

    集計は**どのページにも載せます**。刷ったあと紙がばらけても、1枚1枚が
    その直の数字を持っているように。
    """
    stamps = stamps or {}
    body = "".join(
        _sheet_html(header, details, generated_at=generated_at,
                    summary=summary, stops=stops,
                    stamp=stamps.get(int(header.page), ""))
        for header, details in pages)
    if not pages:
        return _document("日報", '<p class="note">該当するデータがありません。</p>')
    head = pages[0][0]
    title = (f"日報 {head.report_date} {line_names.label(head.line)} {head.shift} "
             f"全{len(pages)}ページ")
    return _document(title, body, pages=len(pages))


_UNSAFE_FILENAME_CHARS = '\\/:*?"<>|'


def write_print_html(
    header: HeaderRecord,
    details: list[DetailRecord],
    out_path: Path,
    *,
    generated_at: str = "",
    summary: Optional[PackingReport] = None,
    stops: Optional[Sequence[tuple[StopRow, int]]] = None,
) -> Path:
    """印刷用HTMLをファイルへ書き出し、書き出し先を返す。"""
    html_text = build_print_html(header, details, generated_at=generated_at,
                                 summary=summary, stops=stops)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_text, encoding="utf-8")
    return out_path


def default_output_path(base_dir: Path, header: HeaderRecord) -> Path:
    r"""出力先を組み立てる。

    ``<指定のパス>\L-1\印刷\2026.08\03\印刷_2026年8月3日_L-1_1直_1.html``

    集計CSVと**同じ並べ方**にしてあります(`csv_export.dated_dir`)──
    片方だけ別の場所に出ると、「印刷したはずのものが見つからない」と
    いうときに探す場所が2通りになります。ラインで分かれているので、
    自分のラインのフォルダだけ開けば、紙も集計も同じ日付でそろいます。
    """
    from .csv_export import KIND_PRINT, dated_dir

    safe_line = "".join(ch for ch in header.line if ch not in _UNSAFE_FILENAME_CHARS).strip() or "Unknown"
    safe_date = "".join(ch for ch in header.report_date if ch not in _UNSAFE_FILENAME_CHARS).strip()
    folder = dated_dir(base_dir, header.report_date, header.line, KIND_PRINT)
    return folder / f"印刷_{safe_date}_{safe_line}_{header.shift}_{header.page}.html"
