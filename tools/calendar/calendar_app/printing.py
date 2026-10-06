"""印刷用の出力 (VBA の btnPrint_Click の代替)。

VBA 版は「印刷用」ワークシートを作って A4 横 1 ページに収め、
``PrintPreview`` から印刷/PDF 出力させていた。
Python 版は Excel に依存できないため、同じ体裁の HTML を生成して
ブラウザに表示させる。ブラウザの印刷ダイアログから印刷/PDF 保存ができる。
"""

from __future__ import annotations

import datetime as _dt
import html
from pathlib import Path

from . import config
from .holiday import holiday_name
from .repository import DayRecord, build_day_text


def _version_label() -> str:
    """紙に出す版。**出どころは ``config/app.json`` ただ1つ。**

    読めなくても印刷は止めない ── 版が分からないことより、
    その日のカレンダーが出ないことのほうが困る。
    """
    try:
        from . import app_config

        return app_config.version_label()
    except Exception:                             # noqa: BLE001 - 印刷を止めない
        return ""

__all__ = ["render_month_document", "render_month_html"]

# ---------------------------------------------------------------------------
# 紙の上の置き場所
# ---------------------------------------------------------------------------
# **プリンターは紙の縁から約4mmには印刷できません。** ブラウザの印刷
# プレビューは紙の端まで描くので、画面では収まって見えても、紙では縁が
# 欠けます。そこで、**文字・罫線・色帯をすべて紙の端から SAFE_MM 以上
# 内側に置きます。**
#
# 余白は**ページの内側に自分で取ります**(``@page`` の margin は 0)。
# ``@page`` の余白に頼ると、印刷ダイアログで「余白: なし」「最小」を選ばれた
# ときに消えるか変わるためです。紙1枚ぶんの台紙(``.sheet``)を置き、
# その padding を安全な余白にします。
#
# **5mm ちょうどにしない。** 刷れない幅は機種と給紙のずれで前後するので、
# 4mm に余裕を足して SAFE_MM とします。
PAPER_W_MM = 297          # A4 横
PAPER_H_MM = 210
SAFE_MM = 8               # 紙の端から、描いてよい所まで(5mm 以上の決まりに余裕)
#: 台紙の高さ。**紙より少し低くする。** ダイアログで余白を足されると
#: (「最小」「カスタム」)、Chrome は幅が収まるよう全体を縮めるが、
#: 縮めても高さが余白ぶん足りないと2枚目(白紙)ができる。片側10mm まで
#: 足されても1枚に収まる高さにしてある: 203 × (297-20)/297 ≦ 210-20
SHEET_H_MM = 203
#: 1週の行の高さ(最低)。6週が台紙に収まる高さ。**行ごとに揃える** ──
#: 余った高さを表に配ると、空の週(翌月だけの週)が細く潰れる
ROW_MM = 27

_CSS = f"""
@page {{ size: A4 landscape; margin: 0; }}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; background: #fff; }}
body {{
    font-family: "Meiryo", "Hiragino Kaku Gothic ProN", sans-serif;
    color: #000;
    /* 色帯(曜日の見出し・土日祝の色)を**プレビューのとおりに刷る**。
       指定しないと「背景のグラフィック」を入れない限り白く抜け、
       白い文字の曜日が読めなくなる */
    -webkit-print-color-adjust: exact; print-color-adjust: exact;
}}
/* 紙1枚ぶんの台紙。**padding が安全な余白**(ここより外には何も描かない) */
.sheet {{
    width: {PAPER_W_MM}mm; height: {SHEET_H_MM}mm;
    padding: {SAFE_MM}mm; overflow: hidden;
}}
/* 中身。多すぎる月は、台紙の中に収まるよう縮める(下のスクリプト) */
.content {{ height: 100%; }}
h1 {{ font-size: 16pt; text-align: center; margin: 0 0 2mm; }}
table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
th, td {{ border: 1px solid #808080; vertical-align: top; }}
th {{ font-size: 10pt; color: #fff; background: #465A78; padding: 3px 0; text-align: center; }}
th.sun {{ background: #AA3C3C; }}
th.sat {{ background: #3C5AAA; }}
td {{ height: {ROW_MM}mm; padding: 3px 4px; font-size: 8.5pt; line-height: 1.35; }}
td.out {{ background: #EBEBEB; color: #A0A0A0; }}
td.holiday {{ background: #FFE0E0; }}
td.sat {{ background: #DCECFF; }}
.daynum {{ font-weight: bold; font-size: 10pt; }}
.holiday-name {{ font-size: 7.5pt; color: #A00000; margin-left: 2px; }}
.body {{ margin-top: 2px; white-space: pre-wrap; word-break: break-all; }}
.footer {{ margin-top: 1.5mm; font-size: 8pt; color: #555; text-align: right; }}
/* 画面で見るときだけ: 紙の外を灰色にして、台紙と安全な余白を見せる */
@media screen {{
    html {{ background: #d9dde1; }}
    body {{ background: transparent; padding: 12px; }}
    .sheet {{ background: #fff; margin: 0 auto; box-shadow: 0 1px 4px #0003;
              outline: 1px dashed #c4d0dc; outline-offset: -{SAFE_MM}mm; }}
}}
@media print {{
    .noprint {{ display: none; }}
}}
.noprint {{
    width: {PAPER_W_MM}mm; margin: 0 auto 8px; padding: 6px 10px; background: #f0f4f8;
    border: 1px solid #c4d0dc; font-size: 9pt; text-align: center; line-height: 1.6;
}}
/* 印刷する・閉じる。**紙には出ない**(.noprint の中) */
.print-actions {{ display: flex; justify-content: center; gap: 8px; margin-bottom: 4px; }}
.print-actions button {{
    font: inherit; font-size: 11pt; font-weight: bold; cursor: pointer;
    padding: 6px 22px; border-radius: 4px; border: 1px solid #465A78;
    background: #fff; color: #233046;
}}
.print-actions button.primary {{ background: #465A78; color: #fff; }}
.print-actions button:hover {{ filter: brightness(1.08); }}
.print-actions button:focus-visible {{ outline: 2px solid #233046; outline-offset: 2px; }}
.print-actions button:disabled {{ opacity: .6; cursor: default; }}
.close-note {{ color: #7a2e2e; }}
"""

# 多すぎる月を台紙に収める。**切り捨てない**(黙って消えるほうが困る)。
# 行の高さ(ROW_MM)で収まればそのまま、中身が多くて行が伸び、台紙に
# 収まらなければ全体を縮める
_FIT_JS = """
(function () {
  function fit() {
    var content = document.querySelector('.content');
    var sheet = document.querySelector('.sheet');
    if (!content || !sheet) return;
    content.style.zoom = 1;
    content.style.height = 'auto';
    var style = getComputedStyle(sheet);
    var room = sheet.clientHeight - parseFloat(style.paddingTop)
             - parseFloat(style.paddingBottom);
    var need = content.scrollHeight;
    if (need <= room) {
      content.style.height = '100%';
      return;
    }
    // 中身だけを縮める。**枠(.content)の高さは台紙のまま** ── 割合の高さは
    // zoom で縮まないので、割り戻すと枠ごと台紙からはみ出す
    content.style.height = '100%';
    var scale = Math.max(0.4, room / need);
    // 文字は縮めても行の高さがきっちり比例しない(端数の丸め)。
    // **縮めたあとの実際の位置を測り直し**、はみ出していればもう少し縮める
    for (var i = 0; i < 6; i += 1) {
      content.style.zoom = scale;
      var top = content.getBoundingClientRect().top;
      var limit = sheet.getBoundingClientRect().bottom - parseFloat(style.paddingBottom);
      var last = content.lastElementChild.getBoundingClientRect().bottom;
      if (last <= limit || scale <= 0.4) break;
      // はみ出した割合だけ縮める(少しだけ余分に)
      scale = Math.max(0.4, scale * (limit - top) / (last - top) * 0.995);
    }
  }
  fit();
  window.addEventListener('beforeprint', fit);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(fit);
})();
"""

# 印刷する・閉じる。**Ctrl+P を知らない人でも印刷できるように**ボタンで出す。
# 押したら、文字の幅で行が変わっても収まるよう測り直してから(beforeprint で
# fit が走る)印刷ダイアログを開く。
# 閉じるは、カレンダー/履歴の [印刷] が開いたタブなら閉じられる。
# デスクトップ版(Tauri)は外枠が開いた窓なので、外枠に閉じてもらう。
# アドレス欄から直接開いたタブはブラウザが閉じさせないので、そう伝える
_ACTIONS_JS = """
(function () {
  var printButton = document.getElementById('do-print');
  var closeButton = document.getElementById('do-close');
  if (printButton) {
    printButton.addEventListener('click', function () { window.print(); });
    printButton.focus();
  }
  if (closeButton) {
    closeButton.addEventListener('click', function () {
      // デスクトップ版は外枠が開いた窓なので、外枠に閉じてもらう
      var desk = window.__TAURI__ && window.__TAURI__.core;
      if (desk) { desk.invoke('close_window'); return; }
      window.close();
      setTimeout(function () {
        if (window.closed) return;
        var note = document.getElementById('close-note');
        if (note) {
          note.hidden = false;
          note.textContent = 'このタブは自動では閉じられません。ブラウザのタブの×で閉じてください。';
        }
      }, 300);
    });
  }
})();
"""


def render_month_document(
    *,
    year: int,
    month: int,
    start: _dt.date,
    records: dict[str, list[DayRecord]],
    my_line: str,
) -> str:
    """表示中の月を印刷用 HTML(1枚)として組み立てて返す。

    セルの配置・色分けは画面表示と同じ規則にそろえている。

    Web 版はこれを ``GET /print`` がそのまま返すので、**ファイルを経由しない**。
    ファイルとして残したい場合(検証・控え)は ``render_month_html()`` を使う。
    """
    rows: list[str] = []
    for week in range(6):
        cells: list[str] = []
        for column in range(7):
            current = start + _dt.timedelta(days=week * 7 + column)
            in_month = current.month == month
            holiday = holiday_name(current)
            key = current.strftime(config.DATE_KEY_FORMAT)

            classes: list[str] = []
            if not in_month:
                classes.append("out")
            elif holiday or current.weekday() == 6:
                classes.append("holiday")
            elif current.weekday() == 5:
                classes.append("sat")

            if in_month:
                text = build_day_text(records.get(key, []), my_line)
                inner = f'<span class="daynum">{current.day}</span>'
                if holiday:
                    inner += f'<span class="holiday-name">{html.escape(holiday)}</span>'
                if text:
                    inner += f'<div class="body">{html.escape(text)}</div>'
            else:
                # 前後月は日付も出さずグレーアウトのみ (VBA と同じ)
                inner = ""

            class_attr = f' class="{" ".join(classes)}"' if classes else ""
            cells.append(f"<td{class_attr}>{inner}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")

    headers = []
    for column, name in enumerate(config.WEEKDAY_LABELS):
        cls = " class=\"sun\"" if column == 0 else (" class=\"sat\"" if column == 6 else "")
        headers.append(f"<th{cls}>{name}</th>")

    line_note = f"　ライン設定: {html.escape(my_line)}" if my_line else ""
    generated = _dt.datetime.now().strftime(config.DATETIME_FORMAT)
    # **紙にも版を残す。** 印刷物は手渡しで回るので、あとから
    # 「どの版で出したものか」を聞かれても、紙だけで答えられるようにする
    version = html.escape(_version_label())

    document = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>{year}年{month}月 {html.escape(config.APP_TITLE)}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="noprint">
  <div class="print-actions">
    <button type="button" class="primary" id="do-print">印刷する</button>
    <button type="button" id="do-close">閉じる</button>
  </div>
  <div class="close-note" id="close-note" role="status" hidden></div>
  [印刷する] (または Ctrl+P) で A4 横 1 ページに印刷できます。
  PDF として保存する場合は、印刷先に「PDF に保存」を選んでください。<br>
  余白は「既定」のままで、紙の端から {SAFE_MM}mm 内側に収まります
  (点線の内側だけに描いています)。倍率は変えないでください。
</div>
<div class="sheet"><div class="content">
<h1>{year}年 {month}月　{html.escape(config.APP_TITLE)}</h1>
<table>
  <thead><tr>{"".join(headers)}</tr></thead>
  <tbody>
{chr(10).join("    " + row for row in rows)}
  </tbody>
</table>
<div class="footer">出力日時: {generated}{line_note}　{version}</div>
</div></div>
<script>{_FIT_JS}</script>
<script>{_ACTIONS_JS}</script>
</body>
</html>
"""
    return document


def render_month_html(
    *,
    year: int,
    month: int,
    start: _dt.date,
    records: dict[str, list[DayRecord]],
    my_line: str,
    output_dir: Path | None = None,
) -> Path:
    """印刷用 HTML をファイルへ書き出し、そのパスを返す。

    Web 版の画面はファイルを経由しない(``GET /print`` が直接返す)。
    これは控えを残したいときと、テストから中身を確かめるための入口。
    """
    document = render_month_document(
        year=year, month=month, start=start, records=records, my_line=my_line)
    folder = output_dir or (config.app_home() / "print")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"calendar_{year}{month:02d}.html"
    path.write_text(document, encoding="utf-8")
    return path
