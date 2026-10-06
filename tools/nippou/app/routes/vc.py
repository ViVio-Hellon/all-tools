"""VC長さ計算 ── レールの「VC長さ計算」(vc-calculator の移植)

    GET  /vc                   画面(面 = 計算 / 早見表 / コイル・平板)
    GET  /api/vc/state         品種・定尺・マスタの状態
    POST /api/vc/select        品種を選んだ(VBA `List選択`)
    POST /api/vc/inside        内径の選択肢を押した(`Big_Click` / `Small_Click`)
    POST /api/vc/run           計算(`CommandButton1_Click`)
    GET  /api/vc/quick         早見表(`UFquick`)
    POST /api/vc/quick-grid    早見表のマスをまとめて作る(管理者)
    GET  /api/vc/settings      設定の面(マスタの置き場所・どの名前を読んでいるか・早見表の品種)

早見表の品種を1回で足す・消す・切り替える(管理者。v3.97.0):

    POST /api/vc/quick-block          品種(枠)を足す ── VC品種・枠・マスを1つの取引で
    POST /api/vc/quick-block/delete   枠をマスごと消す
    POST /api/vc/quick-block/source   長さの出し方(式 / 固定値)を変える
    POST /api/vc/quick-cells/delete   枠の中の行(内径)・列(肉厚)を消す
    POST /api/vc/product/delete       VC品種を、使っている枠・マス・内径の選択肢ごと消す

置き場所(フォルダ)を変えるのは参照設定と同じ口(`POST /api/settings/paths`
の `vc_master_dir`)── 鍵も同じ管理者パスワードです。

**判断はサーバ。** 画面は欄の文字を送り、返ってきた欄と結果を描くだけです。
断ったときも同じ形(状態一式)で返し、理由は `error.code` が運びます。

マスタを直すのは 設定・管理者 →「マスタ」→ マスタ管理 の「VC計算マスタ」です
(ほかの参照マスタと同じ場所・同じ鍵)。
"""
from __future__ import annotations

from typing import Any, Optional

from flask import Blueprint, jsonify, render_template, request

from nippou import admin_password, work_context
from nippou.logging_setup import get_logger, log_button_click
from nippou.presenters import vc as view
from nippou.vc import grid, masters
from nippou.vc.calc import Fields, calculate, select_product
from nippou.vc.masters import Snapshot
from nippou.vc.vba_compat import parse_number, vba_format_fixed

from .. import error_body, shell

log = get_logger("app.routes.vc")

bp = Blueprint("vc", __name__)


#: 早見表だけの別窓(v4.16.0)。`/vc?window=quick`
QUICK_WINDOW = "quick"


@bp.get("/vc")
def index():
    """VC長さ計算。`?window=quick` なら**早見表だけの別窓**(v4.16.0)。

        別で開くのはよいのだが、早見表だけにしておかないと別で開いたもので
        日報に戻れてしまう

    別窓には帯もレールも出しません(F キーで画面を移る道もレールから来るので、
    一緒に無くなる)。面の札も出さず、早見表の面だけを開きます。
    """
    ctx = work_context.get_context()
    from .entry import current_calculator
    bare = request.args.get("window") == QUICK_WINDOW
    return render_template(
        "vc.html",
        tabs=view.tabs(), default_tab="quick" if bare else view.DEFAULT_TAB,
        place=view.place_view(), tones=grid.tones(), bare=bare,
        **shell.shell_context("vc", ribbon=ctx.ribbon(current_calculator())))


# ------------------------------------------------------------------
# 計算
# ------------------------------------------------------------------
def _fields(body: dict) -> Optional[Fields]:
    raw = body.get("fields")
    if raw is not None and not isinstance(raw, dict):
        return None
    return Fields.from_dict(raw or {})


def _bad_fields():
    return jsonify(error_body("bad_request", "fields の形が違います。")), 400


def _reply(snap: Snapshot, *, fields: Fields, product: Optional[str],
           status: int = 200, **extra: Any):
    body = {"state": view.state(snap), "fields": fields.to_dict(), "product": product}
    body.update(extra)
    return jsonify(body), status


@bp.get("/api/vc/state")
def state():
    return jsonify({"state": view.state(masters.current())})


@bp.post("/api/vc/select")
def select():
    """品種を選んだ(`VCList_Click` → `List選択`)。"""
    body = request.get_json(silent=True) or {}
    fields = _fields(body)
    if fields is None:
        return _bad_fields()
    name = str(body.get("product") or "")
    snap = masters.current()
    product = snap.product(name)
    if product is None:
        return _reply(snap, fields=fields, product=None, status=422, error={
            "code": view.REFUSE_NO_PRODUCT,
            "message": f"「{name}」はいまのマスタにありません。一覧を読み直しました。"})
    new = select_product(fields, vcatu=product.vcatu,
                         inside=None if product.chooses_inside else product.inside)
    return _reply(snap, fields=new, product=product.name, cleared=True)


@bp.post("/api/vc/inside")
def choose_inside():
    """内径の選択肢を押した(`Big_Click` / `Small_Click`)。"""
    body = request.get_json(silent=True) or {}
    fields = _fields(body)
    if fields is None:
        return _bad_fields()
    name = str(body.get("product") or "")
    wanted = parse_number(str(body.get("inside") or ""))
    snap = masters.current()
    product = snap.product(name)
    choice = None
    if product is not None and wanted is not None:
        choice = next((c for c in product.choices
                       if vba_format_fixed(c.inside, 1) == vba_format_fixed(wanted, 1)),
                      None)
    if choice is None:
        return _reply(snap, fields=fields, product=product.name if product else None,
                      status=422, error={
                          "code": view.REFUSE_NO_CHOICE,
                          "message": "その内径はいまのマスタの選択肢にありません。"
                                     "一覧を読み直しました。"})
    fields.inside = vba_format_fixed(choice.inside, 1)
    return _reply(snap, fields=fields, product=product.name,
                  choice=vba_format_fixed(choice.inside, 1))


@bp.post("/api/vc/run")
def run():
    """計算ボタン(`CommandButton1_Click`)。"""
    body = request.get_json(silent=True) or {}
    fields = _fields(body)
    if fields is None:
        return _bad_fields()
    snap = masters.current()
    result = calculate(fields, snap.sheets, reverse=snap.reverse_enabled())
    payload: dict[str, Any] = {"result": result.to_dict()}
    status = 200
    if not result.ran:
        status = 422
        payload["error"] = {"code": result.reason, "message": result.message}
    product = str(body.get("product") or "") or None
    return _reply(snap, fields=result.fields, product=product, status=status, **payload)


# ------------------------------------------------------------------
# 早見表
# ------------------------------------------------------------------
def _unlocked() -> bool:
    """マスタを直せる状態か。**マスタ管理と同じ鍵**(`routes/master._unlocked`)。"""
    ctx = work_context.get_context()
    return ctx.master_edit or ctx.admin


def _quick_body(message: str = "") -> dict[str, Any]:
    snap = masters.current()
    body = view.quick(snap)
    body["grid"] = {"can_edit": _unlocked(), "blocks": [], "products": [],
                    "message": message}
    if snap.source == "db":
        try:
            body["grid"]["blocks"] = grid.quick_blocks(masters.master_path())
            # 計算品種の無い枠の長さを式で出すとき、VC厚 を借りる品種の選択肢
            body["grid"]["products"] = grid.products(masters.master_path())
        except Exception as exc:                  # noqa: BLE001 - 表は出す
            log.warning("早見表の枠の一覧を読めませんでした: %s", exc)
    return body


@bp.get("/api/vc/quick")
def quick():
    return jsonify(_quick_body())


@bp.get("/api/vc/settings")
def settings():
    """設定の面。**マスタの置き場所と、2つの名前のどちらを読んでいるか**
    (`presenters/vc.place_view`)と、早見表のマスを作る枠の一覧。"""
    body = _quick_body()
    return jsonify({"place": view.place_view(), "grid": body["grid"],
                    "master": body["master"]})


def _need_key(body: dict, what: str):
    """管理者の鍵。**マスタ管理と同じ鍵**か、その場の管理者パスワード。"""
    if _unlocked() or admin_password.verify(str(body.get("password", ""))):
        return None
    return jsonify(error_body(
        "need_password",
        f"{what}には管理者パスワードが要ります"
        "(設定・管理者の「鍵を開ける」でも開けられます)。")), 403


def _grid_write(body: dict, what: str, button: str, run):
    """早見表を書く口の共通の形。鍵 → マスタに届くか → 書く → 状態一式で返す。"""
    denied = _need_key(body, what)
    if denied is not None:
        return denied
    # **無ければ作ってから書く。** 初めての操作がこれのこともある(開いてすぐ)
    snap = masters.current()
    if snap.source != "db":
        reply = _quick_body()
        reply["error"] = {"code": grid.REFUSE_NO_SOURCE,
                          "message": f"マスタに書けないので{what}ません。{snap.problem}"}
        return jsonify(reply), 422
    result = run(masters.master_path())
    log_button_click(button, extra=result.message[:120])
    reply = _quick_body(result.message if result.ok else "")
    if result.ok:
        reply["message"] = result.message
        return jsonify(reply)
    reply["error"] = {"code": result.reason, "message": result.message}
    status = {grid.REFUSE_BAD_VALUE: 400, grid.REFUSE_LOCKED: 409}.get(result.reason, 422)
    return jsonify(reply), status


@bp.post("/api/vc/quick-grid")
def quick_grid():
    """早見表のマスをまとめて作る。**管理者だけ**(マスタ管理と同じ鍵)。"""
    body = request.get_json(silent=True) or {}
    # 計算品種の無い枠の長さ: VC厚 を1つ決めれば式で出して入れる(`grid.LENGTH_*`)
    return _grid_write(body, "早見表のマスを足す", "vc_quick_grid", lambda path: grid.make_grid(
        path, str(body.get("block", "")),
        str(body.get("insides", "")), str(body.get("thicknesses", "")),
        length_mode=str(body.get("length", grid.LENGTH_NONE)),
        product=str(body.get("product", "")),
        vcatu_text=str(body.get("vcatu", "")),
        fill_empty=body.get("fill_empty", True) is not False,
        overwrite=body.get("overwrite") is True))


@bp.post("/api/vc/quick-block")
def quick_block_add():
    """早見表に品種(枠)を足す。VC品種(新しくも可)・枠・マスを1回で。"""
    body = request.get_json(silent=True) or {}
    return _grid_write(body, "早見表に品種を足す", "vc_quick_block_add", lambda path: grid.add_block(
        path, product=str(body.get("product", "")),
        new_product=str(body.get("new_product", "")),
        new_vcatu=str(body.get("new_vcatu", "")),
        new_vendor=str(body.get("new_vendor", "")),
        block_name=str(body.get("block", "")),
        formula=body.get("length", "formula") != "fixed",
        insides_text=str(body.get("insides", "")),
        thicknesses_text=str(body.get("thicknesses", "")),
        tone=str(body.get("tone", ""))))


@bp.post("/api/vc/quick-block/delete")
def quick_block_delete():
    """早見表の枠をマスごと消す(VC品種は残す)。"""
    body = request.get_json(silent=True) or {}
    return _grid_write(body, "早見表の枠を消す", "vc_quick_block_delete",
                       lambda path: grid.delete_block(path, str(body.get("block", ""))))


@bp.post("/api/vc/quick-block/source")
def quick_block_source():
    """長さの出し方を変える。`product` が空なら固定値、あれば その VC品種 の式。"""
    body = request.get_json(silent=True) or {}
    return _grid_write(body, "長さの出し方を変える", "vc_quick_block_source",
                       lambda path: grid.set_source(path, str(body.get("block", "")),
                                                    str(body.get("product", ""))))


@bp.post("/api/vc/quick-cells/delete")
def quick_cells_delete():
    """枠の中の行(内径)・列(肉厚)を消す。"""
    body = request.get_json(silent=True) or {}
    return _grid_write(body, "早見表のマスを消す", "vc_quick_cells_delete",
                       lambda path: grid.delete_cells(path, str(body.get("block", "")),
                                                      str(body.get("insides", "")),
                                                      str(body.get("thicknesses", ""))))


@bp.post("/api/vc/product/delete")
def product_delete():
    """VC品種を、使っている早見表の枠・マス・内径の選択肢ごと消す。"""
    body = request.get_json(silent=True) or {}
    return _grid_write(body, "VC品種を消す", "vc_product_delete",
                       lambda path: grid.delete_product(path, str(body.get("product", ""))))
