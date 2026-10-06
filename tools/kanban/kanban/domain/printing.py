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
    """倉庫モードの非常設品リスト(発注済みの非常設品)。

    保留中の行は載せない(``build_warehouse_document`` 参照)。
    """
    name = config.display_name(line)
    rows = []
    for item in items:
        if item.is_held:
            continue
        if not item.is_ordered or not item.is_non_permanent:
            continue
        size, qty = _split_size(item.size)
        rows.append((item.material, [item.material, size, qty]))
    return PrintDocument(
        title=f"{name} (非常設品)",
        headers=["資材", "サイズ", "数"],
        rows=_with_separators(rows),
        kind=KIND_NON_PERMANENT,
        created_at=(now or datetime.now()).strftime("%Y/%m/%d %H:%M"),
        font_size_pt=25,
        highlight_title=True,
        file_stem=f"非常設_{name}",
    )


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

#: 先頭から何列を折り返してよいか(どの帳票も 資材・サイズ が先頭の 2 列)
WRAP_COLUMNS = 2

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
  table {{ border-collapse: collapse; width: 100%; max-width: 100%; table-layout: auto; }}
  th, td {{
    border: 1px solid #000;
    padding: 1mm 2mm;
    font-size: {font_size}pt;
    vertical-align: middle;
  }}
  th, td {{ white-space: nowrap; }}
  /* **長いもの(資材・サイズ)は折り返す。** 折り返さないと表が紙の幅を越えて、
     右端が印刷できない所まで出る(長い資材名・サイズで実際にそうなった)。
     短い列(数・状態・日時)は折り返さない ── 日本語はどの字の間でも改行
     できるので、許すと「発/送」「1/本」のように 1 字ずつ割れる */
  td.wrap {{ white-space: normal; overflow-wrap: anywhere; }}
  th {{ background: #c8c8c8; text-align: center; font-size: 12pt; }}
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

  th.pick, td.pick { width: 1%; text-align: center; padding: 0 1mm; background: #eef3f8; }
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
    retally();
  });

  const go = document.getElementById('go');
  if (go) go.addEventListener('click', () => {
    // 直している最中に押されても、打ちかけの文字を取りこぼさない
    if (document.activeElement) document.activeElement.blur();
    window.print();
  });

  retally();
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
    # 折り返してよい列(資材・サイズ)。それ以外は 1 行のまま
    wrap = [' class="wrap"' if i < WRAP_COLUMNS else "" for i in range(column_count)]
    pick = '<th class="pick"></th>' if editable else ""
    header_cells = "".join(f"<th>{html.escape(h)}</th>" for h in doc.headers)
    lines = [f"<table><thead><tr>{pick}{header_cells}</tr></thead><tbody>"]

    # 区切りの空行は、チェック欄のぶんも跨がせる(欄は印刷時に消える)
    span = column_count + (1 if editable else 0)
    for row in doc.rows:
        if row.separator:
            lines.append(f'<tr class="separator"><td colspan="{span}"></td></tr>')
            continue
        cells = list(row.cells) + [""] * (column_count - len(row.cells))
        if editable:
            body_cells = "".join(
                f'<td{wrap[i]} contenteditable="true" data-orig="{html.escape(str(c))}">'
                f"{html.escape(str(c))}</td>"
                for i, c in enumerate(cells)
            )
            lines.append(
                '<tr><td class="pick"><input type="checkbox" checked '
                'aria-label="この行を印刷する"></td>' + body_cells + "</tr>"
            )
        else:
            lines.append(
                "<tr>"
                + "".join(f"<td{wrap[i]}>{html.escape(str(c))}</td>" for i, c in enumerate(cells))
                + "</tr>"
            )
    lines.append("</tbody></table>")
    return "\n".join(lines)


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
