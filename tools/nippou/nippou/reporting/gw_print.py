"""梱包資材重量計算の印刷フォーマット (VBA ``印刷()`` / ``印刷2()``)

【VBAは2種類の紙を出していた】
`UFGW` の印刷ボタンは「メイン」シートを開いて `印刷` を呼び、標準モジュール
`GW計算` の2つの Sub が**印刷範囲だけを変えて**刷っていました。

    印刷()   PrintArea $B$4:$L$34   A4 横  Zoom 78
    印刷2()  PrintArea $B$22:$L$74  A4 縦  FitToPages 1×1

どちらもヘッダ右に「梱包資材重量計算結果」、フッタ右に「&D 印刷」。
つまり**同じシートの、違うところを切り取った2枚**です。

    $B$4:$L$34   計算結果 ── 製品情報(D6:E20) / 使う資材と重量(G5:J20)
                 に加えて、下にぶら下がる梱包数ごとの表の**頭10梱包**
    $B$22:$L$74  梱包数ごとの重量表の見出しと、そこから**50梱包ぶん**

【紙は1種類にしました ── 表は刷らず、梱包数を聞く】

*これは v3.21.0 でいったん「2種類から選ぶ」形にしたものを、v3.22.0 で
作り直したものです。* 前の形は VBA の印刷範囲をそのまま2つのかたちに
していましたが、**紙で要るのは1行だけ**でした。

100行の表を刷っても、読むのは「いま作る梱包数」の行1つです。残りの99行は
探す手間になり、紙も余計に出ます(VBA が50行で切っていたのも、1枚に
収めるためでした)。そこで押す前に**梱包数を聞いて、その行だけ**を計算
結果の紙に載せます。

    梱包数 12 → 「12梱包ぶん」の資材重量を、計算結果の下に一段

1〜100梱包の表そのものは**画面に残っています**(VBA `RangePaste` 相当)。
見比べたいときは画面で見られるので、紙から外しても失うものはありません。

【「12梱包ぶん」に何を載せるか】
VBA の表(`RangePaste`)が持っていた10項目と**同じもの**です ── 8資材 +
資材計 + 風袋総重量。積合せ重量・Aインプット重量・GW は入れません:
VBA の表にも入っておらず(梱包の数で増えるものではない)、ここで勝手に
掛け算すると、紙にだけ VBA と違う数字が出ます。

【数字はここで計算しない】
渡された値を並べるだけです。計算は `logic/gw_calculation.py` の1か所に
あり、画面に出ている数字と紙の数字が食い違う余地を作りません。
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field
from typing import Optional, Sequence

from ..logic.numeric import format_fixed
from . import paper

#: 紙の見出し。VBA `PageSetup.RightHeader` と同じ文言
TITLE = "梱包資材重量計算結果"

#: 梱包数の上限。VBA の表と同じ100梱包までにします ── それ以上は
#: `logic/gw_calculation.per_pack_table` が作らないので、紙だけ先へ
#: 行っても確かめる相手がいません
MAX_PACKS = 100


# ----------------------------------------------------------------------
# 紙に載せる値
# ----------------------------------------------------------------------
@dataclass
class Row:
    """1行。**単位まで持つ** ── 紙の上で「12.3」だけでは読めない。"""

    label: str
    value: str
    unit: str = ""
    #: 使わない資材。VBA は チェックOFF の資材の欄を空にしていた
    used: bool = True
    #: 太字で出すか(合計の行)
    strong: bool = False
    #: label の下にぶら下げる小さい字(バンドの種別・VCの品名)
    sub: str = ""


@dataclass
class GwPrintData:
    """紙1枚ぶんの中身。**ここに無いものは紙に出ません。**

    組み立てるのは呼び手(`app/routes/gw.py`)で、この型は入れ物です ──
    計算の結果をそのまま持つので、画面に出ている数字とずれません。
    """

    #: 製品情報(ロットから引いた、打たない値)
    product: list[Row] = field(default_factory=list)
    #: 梱包の形(人が打った値)
    dimensions: list[Row] = field(default_factory=list)
    #: 資材ごとの重量(**1梱包ぶん** ── 計算結果そのもの)
    materials: list[Row] = field(default_factory=list)
    #: 合計(梱包資材重量・積合せ・風袋総重量・Aインプット・GW)
    totals: list[Row] = field(default_factory=list)
    #: 刷る人が入れた梱包数。1なら「◯梱包ぶん」の段は出しません
    pack_count: int = 1
    #: その梱包数ぶんの資材重量(VBA の表の該当行)
    pack_rows: list[Row] = field(default_factory=list)
    #: 紙の題に添える鍵(LotNo など)。無ければ題だけ
    subject: str = ""


# ----------------------------------------------------------------------
# 見た目
#
# 日報の印刷(`print_format.py`)と**同じ作り**にしてあります ── 追加の
# ライブラリを使わず、印刷用にしたHTMLをブラウザで開いて Ctrl+P で刷る。
# ここが持つのは組み立てだけです。
# ----------------------------------------------------------------------
_STYLE = """
body { font-family: "Meiryo UI", "Yu Gothic", sans-serif; font-size: 12px;
       margin: 16px; }
h1 { font-size: 15px; margin: 0; }
.head { display: flex; align-items: baseline; justify-content: space-between;
        border-bottom: 2px solid #444; padding-bottom: 4px; margin-bottom: 8px; }
.head .subject { font-size: 12px; }
.cols { display: flex; gap: 16px; align-items: flex-start; }
.cols > section { flex: 1; min-width: 0; }
h2 { font-size: 12px; margin: 0 0 4px; padding: 2px 4px; background: #eee;
     border: 1px solid #444; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid #444; padding: 2px 4px; white-space: nowrap; }
th { background: #eee; text-align: center; }
td.label { text-align: left; }
td.value { text-align: right; font-family: "Consolas", monospace; }
td.unit { text-align: left; width: 3em; }
tr.strong td { font-weight: 700; background: #f4f4f4; }
/* 使わない資材。**行は消さない** ── 消すと「忘れたのか、使わないのか」が
   紙から読めなくなる(VBAはチェックOFFの欄を空にしていた) */
tr.unused td { color: #888; }
td .sub { display: block; font-size: 10px; color: #555; }
/* 「◯梱包ぶん」の段。**計算結果とひと目で分かれるように**枠を濃くする
   ── 1梱包ぶんの数字と並ぶので、混ざると取り違える */
.packs { margin-top: 10px; border: 2px solid #444; padding: 4px 6px; }
.packs h2 { border: 0; background: transparent; padding: 0;
            margin-bottom: 4px; }
.packs .note { font-size: 10px; color: #555; margin: 4px 0 0; }
.packs td.value { width: 7em; }
.generated-at { margin-top: 10px; font-size: 10px; color: #666; }
@media print {
  .generated-at { display: none; }
  /* **紙1枚に収める。** 「◯梱包ぶん」の段まで載ると、紙の端から 7mm の
     余白の内側に収まりきらず2枚目にはみ出していました(はみ出した続きには
     余白がありません)。行を少し詰めます */
  body { font-size: 11px; }
  th, td { padding: 1px 4px; }
  h2 { padding: 1px 4px; }
  .packs { margin-top: 6px; }
}
""" + paper.TOOLBAR_CSS + paper.page_css("A4 landscape", ".paper")   # VBA `印刷()` と同じ A4 横

# 画面で開いたときの帯。**印刷の画面を閉じてしまっても、ここから刷れる**
# (Ctrl+P を知らなくても)。帯そのものは紙に出ない(`@media print`)。
# 日報の紙と同じ帯です(`paper.toolbar_html`)
_TOOLBAR = paper.toolbar_html(
    "A4 横1枚で刷れます。刷り終わったら、この窓は閉じてかまいません")

# `print=1` で開いたとき。**開いたらそのまま印刷の画面を出す**
_AUTO_PRINT = """<script>
window.addEventListener("load", function () { setTimeout(function () { window.print(); }, 200); });
</script>"""


def _esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


def _rows_html(rows: Sequence[Row]) -> str:
    if not rows:
        return '<p class="note">(ありません)</p>'
    out = []
    for item in rows:
        classes = " ".join(filter(None, [
            "strong" if item.strong else "",
            "" if item.used else "unused",
        ]))
        attr = f' class="{classes}"' if classes else ""
        sub = f'<span class="sub">{_esc(item.sub)}</span>' if item.sub else ""
        out.append(
            f'<tr{attr}>'
            f'<td class="label">{_esc(item.label)}{sub}</td>'
            f'<td class="value">{_esc(item.value)}</td>'
            f'<td class="unit">{_esc(item.unit)}</td></tr>')
    return f"<table><tbody>{''.join(out)}</tbody></table>"


def _packs_html(data: GwPrintData) -> str:
    """「◯梱包ぶん」の段。**1梱包のときは出しません。**

    1梱包なら上の「使う資材と重量」と同じ数字になるので、並べても
    読むものが増えるだけです。
    """
    if data.pack_count <= 1 or not data.pack_rows:
        return ""
    half = (len(data.pack_rows) + 1) // 2
    left, right = data.pack_rows[:half], data.pack_rows[half:]
    return f"""
<div class="packs">
  <h2>{_esc(data.pack_count)}梱包ぶんの資材重量</h2>
  <div class="cols">
    <section>{_rows_html(left)}</section>
    <section>{_rows_html(right)}</section>
  </div>
  <p class="note">
    1梱包ぶんに梱包数を掛けたものです。積合せ重量・Aインプット重量・GWは
    梱包の数で増えるものではないので、ここには入れていません。
  </p>
</div>
"""


def build_html(data: GwPrintData, *, generated_at: str = "",
               auto_print: bool = False) -> str:
    """紙1枚ぶんのHTML。**左が入れたもの、右が出たもの。**

    VBA のシートも左(D〜E列)に製品情報と梱包の形、右(G〜J列)に資材ごとの
    重量と合計が並んでいました。読む向きを変えません。

    `auto_print=True` なら、開いたらそのまま印刷の画面を出します
    (画面の「印刷」から開いたとき)。どちらでも上に「印刷する」の帯が
    付きます(紙には出ません)。
    """
    subject = (f'<span class="subject">{_esc(data.subject)}</span>'
               if data.subject else "")
    title = f"{TITLE} {data.subject}".strip()
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>{_esc(title)}</title>
<style>{_STYLE}</style>
</head>
<body>
{_TOOLBAR}
<div class="paper">
<div class="head">
  <h1>{_esc(TITLE)}</h1>
  {subject}
</div>
<div class="cols">
  <section>
    <h2>製品情報</h2>
    {_rows_html(data.product)}
    <h2 style="margin-top:8px">梱包の形</h2>
    {_rows_html(data.dimensions)}
  </section>
  <section>
    <h2>使う資材と重量(1梱包ぶん)</h2>
    {_rows_html(data.materials)}
    <h2 style="margin-top:8px">合計(1梱包ぶん)</h2>
    {_rows_html(data.totals)}
  </section>
</div>
{_packs_html(data)}
<div class="generated-at">生成日時: {_esc(generated_at)}</div>
</div>
{_AUTO_PRINT if auto_print else ""}
</body>
</html>
"""


def build_missing_html(message: str) -> str:
    """刷れないときの紙。**白紙を出さない。**

    新しい窓で開く経路なので、JSONの断りを返しても押した人には何も
    見えません。なぜ刷れないのかを、その窓に字で出します。
    """
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>{_esc(TITLE)}</title>
<style>{_STYLE}</style>
</head>
<body>
<div class="head"><h1>{_esc(TITLE)}</h1></div>
<p class="note">{_esc(message)}</p>
</body>
</html>
"""


def normalize_packs(value: object) -> int:
    """梱包数を、紙に出せる数に整える。

    **打ち間違いで紙を止めません。** 空・0・文字なら1梱包(=「◯梱包ぶん」
    の段が出ないだけ)、上限を超えたら上限に丸めます ── 計算そのものは
    梱包数に左右されないので、ここで断る意味がありません。
    """
    try:
        packs = int(float(str(value).strip() or 1))
    except ValueError:
        return 1
    return max(1, min(packs, MAX_PACKS))


def row(label: str, value: object, unit: str = "", *, digits: Optional[int] = 2,
        used: bool = True, strong: bool = False, sub: str = "") -> Row:
    """行を1つ作る助け。数は桁をそろえ、文字はそのまま出します。

    `digits=None` なら数でも丸めません(ロット番号のような「数に見える
    文字」を 7131.00 にしないため)。
    """
    if digits is None or isinstance(value, str):
        text = "" if value is None else str(value)
    elif value is None:
        text = "－"
    else:
        text = format_fixed(float(value), digits)
    return Row(label=label, value=text, unit=unit, used=used, strong=strong,
               sub=sub)
