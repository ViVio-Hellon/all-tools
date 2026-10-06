"""印刷 (tkinter 版の ``btnPrint`` 系)

【Web 版で単純になったところ】
tkinter 版は帳票 HTML を一時ファイルへ書き、既定のブラウザで開いていた
(``printing.write_document`` → ``open_document``)。Web 版はもうブラウザの
中に居るので、**URL を1つ開くだけ**でよい。一時ファイルも、その後始末も
要らなくなった。

帳票の中身を決めるのは :mod:`kanban.domain.printing` のまま。どの行を載せる
かは業務判断なので、ここでは触らない。

``/report/*`` は起動トークンを要求する(``app/__init__.py`` の
``TOKEN_REQUIRED_PREFIXES``)。帳票には資材名・サイズ・数量が丸ごと入るため、
画面より緩い口を作らない。
"""

from __future__ import annotations

from flask import Blueprint, Response, abort, current_app, jsonify, request

from kanban import config
from kanban.applog import get_logger
from kanban.domain import printing

from .. import current_mode, get_service
from .board import visible_lines

log = get_logger("app.routes.report")

bp = Blueprint("report", __name__)

#: 帳票の種類 → (組み立てる関数, 表題)
KINDS = {
    "site": (printing.build_site_document, "注文票"),
    "warehouse": (printing.build_warehouse_document, "発送明細"),
    "non_permanent": (printing.build_non_permanent_document, "非常設品リスト"),
}

#: モードごとに出せる帳票。現場は注文票、倉庫は発送明細と非常設品リスト。
#: tkinter 版のボタン配置と同じ(倉庫参照モードには印刷ボタンが無かった)
MODE_KINDS = {
    config.MODE_SITE: ("site",),
    config.MODE_WAREHOUSE: ("warehouse", "non_permanent"),
    config.MODE_WAREHOUSE_VIEW: (),
}


@bp.get("/api/report/available")
def available():
    """このモード・このラインで出せる帳票と、その件数。

    **押す前に何件出るかを見せる。** 0 件の帳票を開くと白紙が出て、
    「壊れている」と受け取られる(tkinter 版はダイアログで知らせていた)。
    """
    line = request.args.get("line", "")
    lines = visible_lines()
    if line not in lines:
        line = lines[0] if lines else ""
    if not line:
        return jsonify({"line": "", "reports": []})

    items = get_service().items(line)
    out = []
    for kind in MODE_KINDS.get(current_mode(), ()):
        build, label = KINDS[kind]
        doc = build(line, items)
        out.append(
            {
                "kind": kind,
                "label": label,
                "count": doc.data_row_count,
                "url": f"/report/{kind}?line={line}",
            }
        )
    return jsonify({"line": line, "reports": out})


@bp.get("/report/<kind>")
def render(kind: str):
    """帳票そのもの(印刷用 HTML)。新しいタブで開く。"""
    if kind not in KINDS:
        abort(404)
    if kind not in MODE_KINDS.get(current_mode(), ()):
        log.info("このモードでは出せない帳票です: %s (%s)", kind, current_mode())
        abort(404)

    line = request.args.get("line", "")
    if line not in visible_lines():
        abort(404)

    build, label = KINDS[kind]
    doc = build(line, get_service().items(line))
    log.info("帳票を作りました: %s line=%s 明細=%d", kind, line, doc.data_row_count)

    if doc.data_row_count == 0:
        # 白紙を出さない。**何も無いことを言う**
        return Response(_empty_page(label, line), mimetype="text/html; charset=utf-8")

    # ``auto_print`` は設定に従う。開いた瞬間に印刷ダイアログを出すかどうか。
    #
    # **この起動が読んでいる設定ファイルを読む。** 既定の場所を決め打つと、
    # ``--config`` で別のファイルを指して起動した端末(検証環境・並行運用)で
    # だけ設定が効かない、という追いにくい食い違いになる
    cfg = config.load_config(current_app.config.get("CONFIG_PATH") or None)
    return Response(
        printing.render_html(doc, auto_print=cfg.auto_print),
        mimetype="text/html; charset=utf-8",
    )


_EMPTY = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<title>{label} — {line}</title>
<style>
  body {{ margin:0; min-height:100vh; display:grid; place-items:center;
         font-family:"Meiryo UI","Yu Gothic UI",system-ui,sans-serif;
         background:#f4f6f8; color:#1b1f23; }}
  .box {{ text-align:center; padding:2rem 3rem; background:#fff;
          border:1px solid #d7dde3; border-radius:10px; }}
  h1 {{ font-size:1.05rem; margin:0 0 .4rem; }}
  p {{ margin:0; color:#5b6672; font-size:.9rem; }}
</style></head>
<body><div class="box">
  <h1>{line} — {label}</h1>
  <p>印刷する明細はありません。</p>
</div></body></html>
"""


def _empty_page(label: str, line: str) -> str:
    import html

    return _EMPTY.format(
        label=html.escape(label), line=html.escape(config.display_name(line))
    )
