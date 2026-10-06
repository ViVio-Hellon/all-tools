"""紙の余白 ── **文字・罫線・バーを、紙の端から5mm以上内側に置く**

    印刷プレビューでは収まっているが印刷すると見切れています
    プリンターは紙の縁から約4mmの所には印刷できません。
    プレビューは紙の端まで描くので、画面では収まって見えるだけかと。
    文字・罫線・バーをすべて紙の端から5mm以上内側に置いてください。

【なぜ `@page` の余白に任せないのか】
余白を `@page { margin }` で決めていました。ところがブラウザの印刷画面で
「余白: なし」を選ぶと、**`@page` の余白は無視されて紙の端から描きます**
(Chrome / Edge はその選択を次の印刷でも覚えています)。プレビューは紙の
端まで描けるので収まって見え、プリンターは端から約4mmを刷れないので
見切れます。

そこで、**余白を紙の中身そのものに持たせます**:

    @page の余白          0(印刷画面の「既定」でも「なし」でも同じ始まり)
    紙1枚ぶんの箱の内側  INSET_MM(= 7mm)の余白

こうすると、印刷画面の余白が「既定」でも「なし」でも中身は紙の端から
7mm 内側です(「最小」ならプリンターの最小余白にさらに 7mm 足されます)。

【縮めさせない】
中身が紙より広いと、ブラウザは紙の幅に合わせて**全体を縮めます**。
余白も一緒に縮むので、7mm が 5mm を割ることがあります。紙1枚ぶんの箱は
**紙の大きさに収まる**ように作ります(`tests/test_paper_margins.py` が
印刷の見た目で測って確かめます)。

5mm ちょうどではなく 7mm なのは、プリンターの刷れない幅(約4mm)に
紙送りのずれを見込んだぶんです。
"""
from __future__ import annotations

import html

#: 紙の端から中身までの余白(mm)。**5mm 以上**(プリンターは端から約4mmを刷れない)
INSET_MM = 7

#: これより内側に置くこと(mm)。試験はこの値で測る
MIN_INSET_MM = 5


def page_css(size: str, unit: str) -> str:
    """印刷のときの紙と余白。`unit` は紙1枚ぶんの箱のセレクタ。

    画面で見ているときは何も変えません(`@media print` の中だけ)。
    """
    return f"""
/* 紙の余白(`nippou/reporting/paper.py`)。**@page の余白に任せない** ──
   印刷画面で「余白: なし」を選ばれると無視され、紙の端から描いてしまう */
@page {{ size: {size}; margin: 0; }}
@media print {{
  html, body {{ margin: 0 !important; padding: 0 !important; }}
  {unit} {{ box-sizing: border-box; padding: {INSET_MM}mm; }}
}}
"""


# ----------------------------------------------------------------------
# 「印刷する」の帯 (v4.6.0)
#
#     すべての印刷プレビュー画面に 印刷する ボタンがあるかのチェックと
#     動作チェック願います
#
# 紙の窓は2つあります ── 日報の紙(`print_format.py`)と梱包資材重量計算の紙
# (`gw_print.py`)。帯を付けていたのは GW だけで、**日報の紙には刷るボタンが
# ありませんでした**(Ctrl+P を知らないと刷れない)。同じ帯を2つの紙で使う
# よう、ここに1つだけ置きます。
#
# **帯は紙に出ません**(`@media print`)。画面で見ているあいだは上に貼り付いて、
# 直ぶんの長い紙を下まで送っても押せます。
# ----------------------------------------------------------------------
#: 帯のボタンの字。**紙の窓どうしでそろえる**(試験もこれを見る)
PRINT_LABEL = "印刷する"

TOOLBAR_CSS = """
/* 画面で見ているときだけの帯(`nippou/reporting/paper.py`)。**紙には出さない** */
.toolbar { display: flex; gap: 8px; align-items: center; margin-bottom: 10px;
           padding: 6px 8px; background: #f4f4f4; border: 1px solid #ccc;
           position: sticky; top: 0; z-index: 1; }
.toolbar button { font: inherit; font-size: 14px; padding: 4px 16px; cursor: pointer; }
.toolbar .hint { font-size: 11px; color: #555; }
@media print { .toolbar { display: none !important; } }
"""


def toolbar_html(hint: str) -> str:
    """紙の上の帯。**押すとブラウザの印刷の画面が出る**(Ctrl+P と同じ)。

    外の JS を読まない形にしてあります ── 紙は「当直をDBから復旧」で
    ファイルにも書き出され、ブラウザで直に開かれる(`file://`)ことがあるので。
    """
    return (f'<div class="toolbar">\n'
            f'  <button type="button" onclick="window.print()">{PRINT_LABEL}</button>\n'
            f'  <span class="hint">{html.escape(hint)}</span>\n'
            f'</div>')
