"""マスタ管理 (``/api/master/*``)

設定画面の「マスタ管理」の節が使う。画面そのものは ``settings.html`` の
中にあり、ここは中身の出し入れだけを持つ。

【書き先は取り込み元】
直すのは共有フォルダの取り込み元(マスタDB)で、手元の SQLite ではない。
手元は総入れ替えで取り込まれるので、直しても次の取り込みで消える。
理由と手順は ``calendar_app/master_admin.py`` に書いてある。

【パスが無ければ直せない】
参照パスが未設定・ファイルが見つからない・開けない、の3つは
すべて ``no_source`` で断る。**見るだけ**は通す ── 中身を確かめられる
ことと、書き換えられることは別の話。

【見るだけの表もここに並ぶ】
端末一覧(どのPCがどのラインか)は**書けない表**で、直そうとすると
``not_editable`` で断る。直すのはその端末の設定画面で、管理者パスワードが
要る ── 一覧から直せてしまうと、その端末の人には何も起きていないように
見えたまま表示だけが変わる。
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from calendar_app import admin_password, config, master_admin
from calendar_app.logging_utils import get_logger
from calendar_app.presenters import master as presenter

from . import get_db

log = get_logger("app.routes.master")

bp = Blueprint("master", __name__)

#: 断りの種別を HTTP に写す(``docs/設計.md`` §1)。
#:   400 … 入力の形が違う。**サーバの状態は動いていない**
#:   409 … 先を越された(見ていた行がもう無い / もうある)
#:   422 … 業務としての断り
STATUS = {
    master_admin.REFUSE_BAD_VALUE: 400,
    master_admin.REFUSE_NO_ROW: 409,
    master_admin.REFUSE_ALREADY: 409,
    # 形は正しく、**別の誰かが先に直した**(設計 §1)
    master_admin.REFUSE_STALE: 409,
    master_admin.REFUSE_NO_SOURCE: 422,
    master_admin.REFUSE_NOT_EDITABLE: 422,
    master_admin.REFUSE_WRITE_FAILED: 422,
    # 管理者パスワードの関門。**403** ── 形は正しく、この端末では
    # まだできない(設計 §8.2)
    admin_password.REFUSE_NEED_PASSWORD: 403,
    admin_password.REFUSE_WRONG: 403,
}


@bp.get("/api/master")
def read():
    """表の中身と、直せるかどうか。"""
    table = request.args.get("table", config.TABLE_MEMBER)
    return jsonify(presenter.page_dict(master_admin.page(
        table, query=request.args.get("q", ""),
        sort=request.args.get("sort", ""),
        desc=request.args.get("desc", "") in ("1", "true"))))


@bp.post("/api/master/save")
def save():
    """1行を直す。"""
    body = request.get_json(silent=True) or {}
    table = str(body.get("table", config.TABLE_MEMBER))
    key = str(body.get("key", ""))
    values = body.get("values")
    if not isinstance(values, dict):
        return _bad_request("直す内容が指定されていません。")
    # **開いたときの中身**。これがあるあいだに別の端末が直していれば断る
    # (``master_admin.save_row`` の説明)。無ければこれまでどおり素通し
    expected = body.get("expected")
    return _finish(table, master_admin.save_row(
        get_db(), table, key, values,
        expected if isinstance(expected, dict) else None,
        str(body.get("password", ""))))


@bp.post("/api/master/add")
def add():
    """1行を足す。"""
    body = request.get_json(silent=True) or {}
    table = str(body.get("table", config.TABLE_MEMBER))
    values = body.get("values")
    if not isinstance(values, dict):
        return _bad_request("追加する内容が指定されていません。")
    return _finish(table, master_admin.add_row(
        get_db(), table, values, str(body.get("password", ""))))


@bp.post("/api/master/delete")
def delete():
    """1行を消す。"""
    body = request.get_json(silent=True) or {}
    table = str(body.get("table", config.TABLE_MEMBER))
    key = str(body.get("key", ""))
    return _finish(table, master_admin.delete_row(
        get_db(), table, key, str(body.get("password", ""))))


# ---------------------------------------------------------------------------
def _finish(table: str, result: master_admin.Result):
    """結果に**更新後の一覧一式**を添えて返す。

    画面は返ってきたものを描き直すだけでよい(差分を当てない)。
    断ったときも一覧を返す ── 直せなかった理由を読みながら、
    いまの中身を確かめられるようにするため。
    """
    # **見ていた並びのまま返す**(絞り込み・並べ替え)。直すたびに既定の
    # 並びへ戻ると、いま直した行がどこへ行ったか探すことになる
    view = (request.get_json(silent=True) or {}).get("view") or {}
    if not isinstance(view, dict):
        view = {}
    payload = presenter.page_dict(master_admin.page(
        table, query=str(view.get("q", "")), sort=str(view.get("sort", "")),
        desc=bool(view.get("desc"))))
    payload["message"] = result.message
    if result.ok:
        log.info("マスタを直しました: %s / %s", table, result.message)
        return jsonify(payload)
    payload["error"] = {"code": result.reason, "message": result.message}
    return jsonify(payload), STATUS.get(result.reason, 422)


def _bad_request(message: str):
    return jsonify({"error": {"code": "bad_request", "message": message}}), 400
