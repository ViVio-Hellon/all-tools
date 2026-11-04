"""印刷用の帳票生成。

VBA 版は Excel のシートを作って ``PrintOut`` していた。Python 標準ライブラリ
だけで同じことをするため、印刷用の HTML を生成して既定のブラウザで開く。
用紙幅に合わせる設定(``@page`` / ``FitToPagesWide``相当)と、資材が変わる
ところで空行を入れる体裁は元の Excel 出力に合わせている。

``auto_print`` を有効にすると、開いた直後に印刷ダイアログを表示する。

【出す前に直せる】
**帳票は人へ渡す紙です。** 渡す直前に「この数だけ違う」「この資材名は
現場の呼び方に直したい」と気付くことがあり、VBA 版・Excel 版ではその場で
セルを打ち替えてから印刷できていました。Web 版で HTML を出しっぱなしに
すると、その手が無くなります ── 直すには看板そのものを触るしかなくなり、
**紙を直したいだけなのに実データを動かす**ことになります。

そこで、確認の画面(= 帳票そのもの)の各マスを打ち替えられるようにして
あります。

* 打ち替えても**共有DBには一切書きません。** ここで直すのは紙だけです
* 載せたくない行は外せます(印刷には出ません)
* 「元に戻す」で、システムが出した値へ戻せます

直した跡は画面では色が付きますが、**印刷には出ません**(紙に編集の跡が
残ると、受け取った側が「これは何の印か」と迷う)。
"""

from __future__ import annotations

import html
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

from .. import applog, config
from .models import KanbanItem

#: 現場モードの印刷(発注済みかつ未発送)
KIND_SITE = "site"
#: 倉庫モードの印刷(発注済みかつ発送済み)
KIND_WAREHOUSE = "warehouse"
#: 倉庫モードの非常設品リスト
KIND_NON_PERMANENT = "non_permanent"


@dataclass
class PrintRow:
    """帳票の 1 行。``separator`` が True なら資材の区切り(空行)。"""

    cells: list[str] = field(default_factory=list)
    separator: bool = False


@dataclass
class PrintDocument:
    """1 枚の帳票。"""

    title: str
    headers: list[str]
    rows: list[PrintRow]
    kind: str = KIND_SITE
    created_at: str = ""
    font_size_pt: int = 22
    highlight_title: bool = False
    file_stem: str = "print"

    @property
    def data_row_count(self) -> int:
        return sum(1 for r in self.rows if not r.separator)


def _split_size(raw_size: str) -> tuple[str, str]:
    """``"2200 × 50M : 3本"`` を ``("2200 × 50M", "3本")`` に分ける。

    VBA と同じく ``:`` が無い場合はサイズを空にし、全体を「数」側へ入れる。
    """
    text = (raw_size or "").strip()
    if ":" in text:
        left, right = text.split(":", 1)
        return left.strip(), right.strip()
    return "", text


def _with_separators(
    rows: Iterable[tuple[str, list[str]]]
) -> list[PrintRow]:
    """資材名が変わるところに空行を挟む(Excel 版と同じ体裁)。"""
    out: list[PrintRow] = []
    previous = ""
    for material, cells in rows:
        if previous and previous != material:
            out.append(PrintRow(separator=True))
        out.append(PrintRow(cells=cells))
        previous = material
    return out


def build_site_document(
    line: str, items: Sequence[KanbanItem], now: datetime | None = None
) -> PrintDocument:
    """現場モードの注文票。

    対象は「発注済み(赤)かつ未発送(緑でない)かつ注文中でない」。赤くて
    緑のものは既に届いている/発送済みなので印刷しない。注文中(倉庫対応中)
    の行も対象外(VBA と同じ条件)。
    """
    name = config.display_name(line)
    rows = [
        (
            item.material,
            [item.material, item.size, "注文", item.ordered_at],
        )
        for item in items
        if item.is_ordered and not item.is_shipped and not item.is_held
    ]
    return PrintDocument(
        title=name,
        headers=["資材", "サイズ", "状態", "更新日時"],
        rows=_with_separators(rows),
        kind=KIND_SITE,
        created_at=(now or datetime.now()).strftime("%Y/%m/%d %H:%M"),
        font_size_pt=15,
        file_stem=f"資材発注_{name}",
    )


def build_warehouse_document(
    line: str, items: Sequence[KanbanItem], now: datetime | None = None
) -> PrintDocument:
    """倉庫モードの発送明細(発注済みかつ発送済み)。

    注文中(``保留 = 〇``)の行は一切載せない(VBA ``PrintLine`` と同じ)。

    **一括発送を通しても、注文中のまま発送済みになる行はできません。**
    VBA の ``ExecuteSQLBatchShip`` は保留列を触らなかったので、緑が付いて
    いるのに黄も点いたままの行が実際に作れました ── そうなると、この明細
    からその行だけが黙って抜け落ちます(発送したのに載らない)。いまは
    一括発送も発送ボタン 1 つぶんと同じく注文中を解除します
    (:func:`kanban.domain.models.batch_ship_changes`)。
    """
    name = config.display_name(line)
    rows = []
    for item in items:
        if item.is_held:
            continue
        if not (item.is_ordered and item.is_shipped):
            continue
        size, qty = _split_size(item.size)
        rows.append((item.material, [item.material, size, qty, "発送"]))
    return PrintDocument(
        title=f"{name} (倉庫発送明細)",
        headers=["資材", "サイズ", "数", "状態"],
        rows=_with_separators(rows),
        kind=KIND_WAREHOUSE,
        created_at=(now or datetime.now()).strftime("%Y/%m/%d %H:%M"),
        font_size_pt=22,
        file_stem=f"資材発送リスト_{name}",
    )


def build_non_permanent_document(
    line: str, items: Sequence[KanbanItem], now: datetime | None = None
) -> PrintDocument:
    """倉庫モードの非常設品リスト(発注済みの非常設品と、**いまの状態**)。

    状態は盤の色と同じ言葉で出す(VER3.5.3。以前は載るだけで状態が分からなかった):

    * 赤だけ(発注だけ) … ``受付``
    * 緑(発送)          … ``発送``
    * 黄(注文中)        … ``注文中``(以前は載せなかった。注文して待っている非常設品こそ
      確かめたい)
    """
    name = config.display_name(line)
    rows = []
    for item in items:
        if not item.is_ordered or not item.is_non_permanent:
            continue
        size, qty = _split_size(item.size)
        rows.append((item.material, [item.material, size, qty, non_permanent_state(item)]))
    return PrintDocument(
        title=f"{name} (非常設品)",
        headers=["資材", "サイズ", "数", "状態"],
        rows=_with_separators(rows),
        kind=KIND_NON_PERMANENT,
        created_at=(now or datetime.now()).strftime("%Y/%m/%d %H:%M"),
        font_size_pt=25,
        highlight_title=True,
        file_stem=f"非常設_{name}",
    )


def non_permanent_state(item: KanbanItem) -> str:
    """非常設品リストの「状態」。注文中(黄)> 発送(緑)> 受付(赤だけ)の順に見る。"""
    if item.is_held:
        return "注文中"
    if item.is_shipped:
        return "発送"
    return "受付"


#: 紙の端から、文字・罫線・色の帯(見出しの灰色など)を置かない幅(mm)。
#:
#: **プリンターは紙の縁から 4mm ほどには印刷できません。** 以前は左右と下の
#: 余白が 0 で(``@page { margin: 8mm 0 0 0 }``)、横は本文の余白 6mm だけ
#: ──しかもマスが折り返さないので、長い資材名があると表が紙の幅を越え、
#: 右端は **0mm** まで描いていました(PDF にして測った値)。画面のプレビューは
#: 紙の端まで描くので、収まって見えるだけでした。
#:
#: 余白は ``@page`` で取ります ── 本文の余白は 1 ページ目の上と最後のページの
#: 下にしか効かず、2 ページ目以降の上や途中のページの下が紙の端まで来るため。
#: どの辺も 5mm 以上内側になるよう、少し足して取ります。
PAGE_MARGIN_MM = {"top": 8, "right": 7, "bottom": 8, "left": 7}

#: 紙(A4 縦)の幅(mm)
PAPER_WIDTH_MM = 210
#: 表を置ける幅(mm)。紙の幅から左右の余白を引いたもの
PRINTABLE_WIDTH_MM = PAPER_WIDTH_MM - PAGE_MARGIN_MM["left"] - PAGE_MARGIN_MM["right"]
#: 表の幅(mm)。置ける幅より 1mm 狭くする ── ぴったりにすると、右端の罫線が
#: 紙の余白の境で切れて印刷されなかった(Chromium の PDF で見た)
TABLE_WIDTH_MM = PRINTABLE_WIDTH_MM - 1

#: 先頭の何列が「長いもの」か(どの帳票も 資材・サイズ が先頭の 2 列)。
#: 余った幅はこの列に回し、短い列(数・状態・日時)は中身の幅のまま狭くする
WIDE_COLUMNS = 2

#: 欄に収めるために縮めてよい文字の大きさの下限(pt)。紙で読める大きさに留める
MIN_FONT_PT = 7

#: 見出しの文字の大きさ(pt)
HEADER_FONT_PT = 12

#: マスの左右の余白と罫線(mm)。CSS の ``padding: 1mm 2mm`` と 1px の罫線
_CELL_PAD_MM = 2 * 2 + 0.6

#: 1pt は何 mm か
_PT_MM = 25.4 / 72

#: 画面で見せる「この行を印刷する」チェック欄の幅(mm)。紙には出ない
PICK_WIDTH_MM = 8

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  @page {{ size: A4 portrait; margin: {page_margin}; }}
  body {{
    font-family: "Meiryo UI", "Yu Gothic UI", "MS PGothic", sans-serif;
    margin: 0;
  }}
  /* 画面では紙の余白が無いので、同じだけ空けて見せる */
  @media screen {{ body {{ margin: {page_margin}; }} }}
  h1 {{ font-size: 14pt; margin: 0 0 2mm 0; {title_color} }}
  .created {{ font-size: 12pt; margin: 0 0 3mm 0; }}
  /* **表は {printable}mm(紙の印刷できる幅より 1mm 狭い)に、列の幅を決めて描く。**
     列の幅は中身に合わせて配る(資材・サイズは広く、数・状態は狭く。:func:`column_widths`)。
     画面ではチェック欄のぶんだけ広い(チェック欄は紙には出ない) */
  table {{
    border-collapse: collapse; table-layout: fixed;
    width: {printable}mm;
  }}
  @media screen {{ table.editable {{ width: {screen_width}mm; }} }}
  th, td {{
    /* 幅は罫線と余白込み(見出しに書いた幅 = 紙の上の欄の幅) */
    box-sizing: border-box;
    border: 1px solid #000;
    padding: 1mm 2mm;
    font-size: {font_size}pt;
    vertical-align: middle;
  }}
  /* **どのマスも 1 行で出す**(VER3.6.2)。以前は資材・サイズを折り返していたので、
     「1030 × 1520」「ハードボード(大板棚)」が 2 行に割れた(現場の声)。
     入りきらないマスは、印刷の前に**文字を小さくして欄に収める**(下のスクリプト。
     下限 {min_font}pt)。それでも入らないときだけ末尾を「…」にする(隣の欄へはみ出さない) */
  th, td {{ white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  th {{ background: #c8c8c8; text-align: center; font-size: {header_font}pt; }}
  tr.separator td {{ border: none; height: 3mm; padding: 0; }}
  .empty {{ font-size: 14pt; padding: 6mm 0; }}
  @media print {{ .no-print {{ display: none; }} }}
{editor_css}
</style>
</head>
<body>
<h1>{title}</h1>
<p class="created">作成日時: {created_at}</p>
{toolbar}
{body}
<p class="no-print" style="font-size:10pt;color:#555">
  ※上の「🖨 印刷する」(または Ctrl+P)で印刷します。この行は紙には出ません。
</p>
{fit_script}
{script}
</body>
</html>
"""

#: 出す前に直すための見た目。**印刷には一切出さない。**
#:
#: 紙に編集の跡(色・点線・チェック欄)が残ると、受け取った側が
#: 「これは何の印か」と迷う。画面でだけ分かるようにする。
_EDITOR_CSS = """
  /* --- 出す前に直す(画面だけ) --- */
  .tools {
    display: flex; align-items: center; gap: .5rem; flex-wrap: wrap;
    margin: 0 0 3mm; padding: .5rem .7rem;
    background: #eef3f8; border: 1px solid #d7dde3; border-radius: 6px;
    font-size: 11pt;
  }
  .tools button {
    font: inherit; padding: .25rem .8rem; border-radius: 4px;
    border: 1px solid #b9c3cc; background: #fff; cursor: pointer;
  }
  .tools button.go { background: #0f6fc5; border-color: #0f6fc5; color: #fff; font-weight: 700; }
  .tools .hint { color: #5b6672; font-size: 10pt; }
  .tools .n { font-weight: 700; }

  th.pick, td.pick { text-align: center; padding: 0 1mm; background: #eef3f8; overflow: visible; }
  td[contenteditable] { cursor: text; }
  td[contenteditable]:hover { background: #eef5ff; }
  td[contenteditable]:focus { background: #fff6d9; outline: 2px solid #0f6fc5; outline-offset: -2px; }
  td.edited { background: #fff6d9; }
  tr.dropped td { color: #9aa4ad; text-decoration: line-through; }

  @media print {
    .tools, th.pick, td.pick { display: none !important; }
    /* 外した行は紙に出さない */
    tr.dropped { display: none !important; }
    /* 直した跡は紙に残さない */
    td.edited, td[contenteditable] {
      background: transparent !important; outline: none !important;
    }
  }
"""

#: 出す前に直すための操作。**共有DBへは何も送らない。**
#:
#: ここで直すのは紙だけなので、通信は 1 度も起きない(``fetch`` も
#: ``form`` も無い)。実データを触っていないことが、コードを見て分かる
#: ようにしてある。
_EDITOR_JS = """
<script>
(() => {
  const rows = () => [...document.querySelectorAll('tbody tr:not(.separator)')];
  const count = document.getElementById('n');

  const retally = () => {
    if (count) count.textContent = rows().filter((r) => !r.classList.contains('dropped')).length;
  };

  for (const td of document.querySelectorAll('td[contenteditable]')) {
    // 打ち替えたことが画面で分かるように(紙には出さない)
    td.addEventListener('input', () => {
      td.classList.toggle('edited', td.textContent !== td.dataset.orig);
      // 打ち替えて長くなっても 1 行に収める
      if (window.fitCells) window.fitCells(td);
    });
    // 貼り付けは**文字だけ**にする。書式ごと入ると表が崩れる
    td.addEventListener('paste', (ev) => {
      ev.preventDefault();
      const text = (ev.clipboardData || window.clipboardData).getData('text');
      document.execCommand('insertText', false, text.replace(/\\s+/g, ' '));
    });
    // Enter で改行を作らない(1 マスは 1 行)
    td.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') { ev.preventDefault(); td.blur(); }
    });
  }

  for (const box of document.querySelectorAll('.pick input')) {
    box.addEventListener('change', () => {
      box.closest('tr').classList.toggle('dropped', !box.checked);
      retally();
    });
  }

  const reset = document.getElementById('reset');
  if (reset) reset.addEventListener('click', () => {
    for (const td of document.querySelectorAll('td[contenteditable]')) {
      td.textContent = td.dataset.orig;
      td.classList.remove('edited');
    }
    for (const box of document.querySelectorAll('.pick input')) {
      box.checked = true;
      box.closest('tr').classList.remove('dropped');
    }
    if (window.fitCells) window.fitCells();
    retally();
  });

  const go = document.getElementById('go');
  if (go) go.addEventListener('click', () => {
    // 直している最中に押されても、打ちかけの文字を取りこぼさない
    if (document.activeElement) document.activeElement.blur();
    if (window.fitCells) window.fitCells();
    window.print();
  });

  retally();
})();
</script>
"""

#: **どのマスも 1 行に収める**(VER3.6.2)。入りきらないマスだけ、欄に収まるまで
#: 文字を小さくする(下限 ``MIN_FONT_PT``)。
#:
#: 列の幅は mm で決めてあり、画面でも紙でも同じなので、画面で測って縮めた大きさが
#: そのまま紙に出る。開いたとき・印刷の直前(``beforeprint``)・マスを打ち替えたときに測る。
#: 編集できない帳票にも付ける(直せなくても 1 行に収める)
_FIT_JS = """
<script>
(() => {
  const MIN_PX = %(min_px).2f;
  // 紙に描くときは字の幅が画面よりわずかに広くなる(字の位置を画素にそろえる)。
  // ぴったりに合わせると紙で末尾が欠けるので、欄の 96%% までに収める
  const ROOM = 0.96;
  const range = document.createRange();
  const textWidth = (cell) => { range.selectNodeContents(cell); return range.getBoundingClientRect().width; };
  const room = (cell) => {
    const cs = getComputedStyle(cell);
    return (cell.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight)) * ROOM;
  };
  const fit = (cell) => {
    cell.style.fontSize = '';
    let px = parseFloat(getComputedStyle(cell).fontSize);
    const limit = room(cell);
    // 0.5px ずつ。入りきるか、下限まで
    while (textWidth(cell) > limit && px > MIN_PX) {
      px = Math.max(MIN_PX, px - 0.5);
      cell.style.fontSize = px + 'px';
    }
    cell.classList.toggle('shrunk', cell.style.fontSize !== '');
  };
  window.fitCells = (only) => {
    const cells = only ? [only] : document.querySelectorAll('table.report th:not(.pick), table.report td:not(.pick)');
    for (const cell of cells) if (!cell.closest('tr.separator')) fit(cell);
  };
  window.fitCells();
  window.addEventListener('beforeprint', () => window.fitCells());
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => window.fitCells());
})();
</script>
"""

_TOOLBAR = """
<div class="tools no-print">
  <button type="button" id="go" class="go">🖨 印刷する</button>
  <button type="button" id="reset">元に戻す</button>
  <span>印刷する行: <span class="n" id="n">0</span></span>
  <span class="hint">
    マスを押すと直せます(紙だけ。看板のデータは変わりません)。
    左のチェックを外した行は印刷されません。
  </span>
</div>
"""


def render_html(
    doc: PrintDocument, auto_print: bool = False, editable: bool = True
) -> str:
    """帳票を印刷用 HTML に変換する。

    ``editable`` が真なら、**出す前に各マスを直せる**画面になります
    (既定)。直した内容は紙にしか効かず、共有DBへは書きません。
    偽にすると、直せない・そのまま刷るだけの帳票になります。

    ``auto_print`` は**開いた直後に印刷ダイアログを出す**設定です
    (``config.auto_print``)。直したい人は、そのダイアログを閉じてから
    直して「印刷する」を押せます ── 今までどおり押さずに刷りたい人の
    手数を増やさないため、ここは変えていません。
    """
    if doc.data_row_count == 0:
        # 直すものが無いので、道具立ても出さない
        body = '<p class="empty">対象の明細はありません。</p>'
        editable = False
    else:
        body = _render_table(doc, editable)

    scripts = []
    if editable:
        scripts.append(_EDITOR_JS)
    if auto_print:
        scripts.append(
            "<script>window.addEventListener('load',()=>window.print());</script>"
        )

    margin = PAGE_MARGIN_MM
    return _HTML_TEMPLATE.format(
        page_margin=f"{margin['top']}mm {margin['right']}mm {margin['bottom']}mm {margin['left']}mm",
        printable=_mm(TABLE_WIDTH_MM),
        screen_width=_mm(TABLE_WIDTH_MM + PICK_WIDTH_MM),
        min_font=MIN_FONT_PT,
        header_font=HEADER_FONT_PT,
        fit_script=_FIT_JS % {"min_px": MIN_FONT_PT * 96 / 72} if doc.data_row_count else "",
        title=html.escape(doc.title),
        created_at=html.escape(doc.created_at),
        font_size=doc.font_size_pt,
        title_color="color:#c83200;" if doc.highlight_title else "",
        editor_css=_EDITOR_CSS if editable else "",
        toolbar=_TOOLBAR if editable else "",
        body=body,
        script="\n".join(scripts),
    )


def _render_table(doc: PrintDocument, editable: bool) -> str:
    """明細の表。

    ``editable`` のときは各マスを ``contenteditable`` にし、元の値を
    ``data-orig`` に控えます ── 「元に戻す」はこれを書き戻すだけなので、
    サーバへ訊き直しません(直している最中に共有DBが変わっても、手元の
    紙が勝手に書き換わらない)。
    """
    column_count = len(doc.headers)
    # どのマスも 1 行。列の幅は中身に合わせて配る(見出しの行に書く = 表の列の幅になる)
    widths = column_widths(doc)
    pick = f'<th class="pick" style="width:{_mm(PICK_WIDTH_MM)}mm"></th>' if editable else ""
    header_cells = "".join(
        f'<th style="width:{_mm(w)}mm">{html.escape(h)}</th>' for h, w in zip(doc.headers, widths)
    )
    table_class = "report editable" if editable else "report"
    lines = [f'<table class="{table_class}"><thead><tr>{pick}{header_cells}</tr></thead><tbody>']

    # 区切りの空行は、チェック欄のぶんも跨がせる(欄は印刷時に消える)
    span = column_count + (1 if editable else 0)
    for row in doc.rows:
        if row.separator:
            lines.append(f'<tr class="separator"><td colspan="{span}"></td></tr>')
            continue
        cells = list(row.cells) + [""] * (column_count - len(row.cells))
        if editable:
            body_cells = "".join(
                f'<td contenteditable="true" data-orig="{html.escape(str(c))}">'
                f"{html.escape(str(c))}</td>"
                for c in cells
            )
            lines.append(
                '<tr><td class="pick"><input type="checkbox" checked '
                'aria-label="この行を印刷する"></td>' + body_cells + "</tr>"
            )
        else:
            lines.append(
                "<tr>"
                + "".join(f"<td>{html.escape(str(c))}</td>" for c in cells)
                + "</tr>"
            )
    lines.append("</tbody></table>")
    return "\n".join(lines)


def _mm(value: float) -> str:
    """CSS に書く mm の値(小数 2 桁まで。末尾の 0 は落とす)。"""
    return f"{value:.2f}".rstrip("0").rstrip(".")


def text_width_em(text: str) -> float:
    """文字列の幅の見積もり(文字の大きさ = 1 として)。

    全角(日本語・「×」など)は 1、半角の英数字は 0.55、空白は 0.3 と見る。
    **見積もりは列の幅を配るのに使うだけ。** 実際に入りきらないマスは、ブラウザが
    測って文字を小さくする(``_FIT_JS``)。
    """
    width = 0.0
    for ch in text:
        if ch == " ":
            width += 0.3
        elif ord(ch) < 0x80:
            width += 0.55
        else:
            width += 1.0
    return width


def column_widths(doc: PrintDocument) -> list[float]:
    """各列の幅(mm)。足すと :data:`TABLE_WIDTH_MM` ちょうどになる。

    * 列ごとに、いちばん長いマス(と見出し)が 1 行に入る幅を見積もる
    * 短い列(数・状態・日時)は**その幅のまま**(狭く)
    * 残りを長い列(資材・サイズ)に、見積もった幅の割合で配る。入りきらない
      ときも同じ割合で分け、入りきらないマスは文字を小さくして収める
    """
    count = len(doc.headers)
    if not count:
        return []
    need = []
    for i, header in enumerate(doc.headers):
        widest = text_width_em(header) * HEADER_FONT_PT
        for row in doc.rows:
            if row.separator or i >= len(row.cells):
                continue
            widest = max(widest, text_width_em(str(row.cells[i])) * doc.font_size_pt)
        need.append(widest * _PT_MM + _CELL_PAD_MM)

    wide = list(range(min(WIDE_COLUMNS, count)))
    narrow = [i for i in range(count) if i not in wide]
    total = float(TABLE_WIDTH_MM)
    widths = list(need)
    # 短い列だけで紙の半分を越えるなら、短い列も割合で縮める(普通は起きない)
    narrow_sum = sum(need[i] for i in narrow)
    if narrow and narrow_sum > total / 2:
        for i in narrow:
            widths[i] = need[i] * (total / 2) / narrow_sum
        narrow_sum = total / 2
    rest = total - narrow_sum
    wide_need = sum(need[i] for i in wide) or 1.0
    for i in wide:
        widths[i] = rest * need[i] / wide_need
    if not wide:
        scale = total / sum(widths)
        widths = [w * scale for w in widths]
    # 丸めの端数は最後の列で吸収して、合計をちょうどにする
    widths = [round(w, 2) for w in widths]
    widths[-1] = round(total - sum(widths[:-1]), 2)
    return widths


def output_dir(base: str | os.PathLike[str] | None = None) -> Path:
    """帳票の出力先。既定は一時フォルダ配下。"""
    if base:
        path = Path(base)
    else:
        path = Path(tempfile.gettempdir()) / config.APP_NAME / "print"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_document(
    doc: PrintDocument,
    base_dir: str | os.PathLike[str] | None = None,
    auto_print: bool = False,
) -> Path:
    """帳票 HTML をファイルへ書き出す。"""
    directory = output_dir(base_dir)
    stem = _safe_stem(doc.file_stem)
    path = directory / f"{stem}_{datetime.now():%Y%m%d_%H%M%S}.html"
    path.write_text(render_html(doc, auto_print=auto_print), encoding="utf-8")
    applog.info("printing: 帳票を作成 %s (%d 行)", path, doc.data_row_count)
    return path


def _safe_stem(stem: str) -> str:
    out = []
    for ch in stem:
        out.append("_" if ch in '\\/:*?"<>| ' else ch)
    return "".join(out) or "print"


def open_document(path: str | os.PathLike[str]) -> bool:
    """既定のアプリケーション(ブラウザ)でファイルを開く。"""
    target = str(path)
    try:
        if os.name == "nt":
            os.startfile(target)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["xdg-open", target])
        return True
    except Exception as exc:
        applog.warning("printing: ファイルを開けませんでした %s (%s)", target, exc)
        return False


def print_document(
    doc: PrintDocument,
    base_dir: str | os.PathLike[str] | None = None,
    auto_print: bool = False,
    open_after: bool = True,
) -> Path:
    """帳票を書き出して開く。"""
    path = write_document(doc, base_dir=base_dir, auto_print=auto_print)
    if open_after:
        open_document(path)
    return path
