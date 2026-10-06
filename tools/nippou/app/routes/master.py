"""マスタ管理 (`/api/master/*`) ── 表を見る / 直す

    GET  /api/master/browse       ファイル・表の一覧と、選んだ表の中身
    POST /api/master/unlock       編集の鍵を開ける / 閉じる(管理者パスワード)
    POST /api/master/row/save     1行を書き換える
    POST /api/master/row/add      1行足す
    POST /api/master/row/delete   1行消す
    POST /api/master/column/add   列を1つ足す(v4.14.0。直せる表だけ・戻せない)

【なぜこの画面が要るのか】
相手は **sqlite3** です。Access なら現場のPCで開いて直せましたが、
sqlite3 は**開くための道具が入っていない前提**で考えるほかありません
(テキストエディタでも開けないバイナリ形式です)。確かめる場所と直す場所を、
このツールの中に持ちます。

【見るのは誰でも、直すのはパスワードを入れた人だけ】
参照マスタは他のラインも同じものを見ています。押し間違いが全員に届くので、
**書くほうだけ**に関門を置きます。見えることと直せることは別の話なので、
見るほう(`browse`)はいつでも通します。

判断は `nippou/master_admin.py` が持ち、ここは断りの種類を HTTP に
写すだけです。
"""
from __future__ import annotations

from typing import Any

from flask import Blueprint, jsonify, request

from nippou import admin_password, job_progress, master_admin, work_context
from nippou.logging_setup import get_logger, log_button_click
from nippou.logic import progress
from nippou.presenters import master as master_presenter

from .. import error_body, get_repo

log = get_logger("app.routes.master")

bp = Blueprint("master", __name__)

# 断りの種類を HTTP に写す。
#   400 … 入力の形が違う。**サーバの状態は動いていない**
#   403 … 許されていない
#   404 … その相手が無い
#   409 … 先を越された(見ていた行がもう無い)
#   422 … 業務としての断り
STATUS = {
    master_admin.REFUSE_BAD_VALUE: 400,
    master_admin.REFUSE_LOCKED: 403,
    master_admin.REFUSE_NOT_EDITABLE: 422,
    master_admin.REFUSE_NO_FILE: 404,
    master_admin.REFUSE_NO_TABLE: 404,
    master_admin.REFUSE_NO_ROW: 409,
    master_admin.REFUSE_WRITE_FAILED: 422,
}


def _unlocked() -> bool:
    """直せる状態か。

    **管理者モードも鍵と見なします。** 開ける合言葉はどちらも同じ
    管理者パスワードで、2つ目の合言葉があるわけではありません。
    面の上で鍵を開けたのに、その面のボタンが「パスワードが要ります」と
    断ると、押した人には**効いていない**としか見えません。
    どちらも直が変われば閉じます(`work_context`)。
    """
    ctx = work_context.get_context()
    return ctx.master_edit or ctx.admin


@bp.get("/api/master/browse")
def browse():
    """ファイル・表の一覧と、選んだ表の中身。**いつでも通す。**"""
    view = master_presenter.browse(
        file=request.args.get("file", ""),
        table=request.args.get("table", ""),
        query=request.args.get("q", ""),
        sort=request.args.get("sort", ""),
        sort_dir=request.args.get("sort_dir", "asc"),
        unlocked=_unlocked())
    return jsonify(master_presenter.to_dict(view))


@bp.post("/api/master/unlock")
def unlock():
    """編集を開ける / 閉じる。

    **合言葉は管理者パスワードと同じ**です。このツールに合言葉は1つしか
    無く、2つ目を作ると現場は両方を紙に貼ります。
    """
    payload = request.get_json(silent=True) or {}
    ctx = work_context.get_context()

    if not payload.get("enable"):
        ctx.master_edit = False
        return jsonify({"unlocked": False, "message": "編集を閉じました"})

    password = str(payload.get("password", ""))
    if not admin_password.verify(password):
        log.warning("マスタ編集の認証に失敗しました")
        return jsonify(error_body("bad_password", "パスワードが違います",
                                  field="password")), 403
    ctx.master_edit = True
    log_button_click("master_edit_on")
    return jsonify({"unlocked": True,
                    "message": "編集を許可しました。直したら共有に届きます。"})


@bp.post("/api/master/row/save")
def save_row():
    """1行を書き換える。`{"file":…, "table":…, "key":…, "values":{…}}`"""
    body = request.get_json(silent=True) or {}
    return _run(body, lambda: master_admin.save_row(
        _file(body), _table(body), body.get("key"), _values(body),
        unlocked=_unlocked()))


@bp.post("/api/master/row/add")
def add_row():
    """1行足す。`{"file":…, "table":…, "values":{…}}`"""
    body = request.get_json(silent=True) or {}
    return _run(body, lambda: master_admin.add_row(
        _file(body), _table(body), _values(body), unlocked=_unlocked()))


@bp.post("/api/master/row/delete")
def delete_row():
    """1行消す。`{"file":…, "table":…, "key":…}`"""
    body = request.get_json(silent=True) or {}
    return _run(body, lambda: master_admin.delete_row(
        _file(body), _table(body), body.get("key"),
        unlocked=_unlocked()))


@bp.post("/api/master/column/add")
def add_column():
    """列を1つ足す。`{"file":…, "table":…, "name":…, "kind":…, "initial":…}`

    足せるのはマスタ管理で直せる表だけ(`master_admin.add_column_why`)。列は消せないので、
    鍵(管理者パスワード)を通す。確認は画面が押す前に1回出す。
    """
    body = request.get_json(silent=True) or {}
    log_button_click("master_add_column",
                     extra=f"{_file(body)} / {_table(body)} / {body.get('name', '')}")
    return _run(body, lambda: master_admin.add_column(
        _file(body), _table(body), body.get("name", ""), body.get("kind", "text"),
        body.get("initial", ""), unlocked=_unlocked()))


def _run(body: dict, write):
    """書いて、読み直して返す。**あいだの進み具合を置く**(`job_progress`)。

        保存処理にもプログレスを表示し進捗がわかるようにしてください

    書く先は共有の元のファイルで、書いたあとは写しを取り直して表を
    読み直します。共有が遅い日はここで待たされるので、いまどちらの段か
    ([1/2] 書いている / [2/2] 読み直している)を画面へ出します。
    """
    where = f"{_file(body)} / {_table(body)}"
    with job_progress.watching(job=progress.JOB_MASTER):
        job_progress.step(phase=progress.PHASE_MASTER_WRITE, label=where)
        result = write()
        job_progress.step(phase=progress.PHASE_RELOAD, label=where)
        return _write(result, body)


def _file(body: dict) -> str:
    return str(body.get("file", ""))


def _table(body: dict) -> str:
    return str(body.get("table", ""))


def _values(body: dict) -> dict:
    values = body.get("values")
    return values if isinstance(values, dict) else {}


def _write(result: master_admin.Result, body: dict):
    """書いたあとは**まるごとの状態**を返す。

    断ったときも同じ形で返します ── 画面は「何が起きたか」と「いま
    どうなっているか」を1回で受け取れます。断りの理由は `error.code` が
    運び、画面は文言から推し量りません。

    絞り込み(`q`)と並び替え(`sort`)も送り返してもらって保ちます ──
    行を直すたびに並びが既定へ戻ると、並べ替えて探した続きの行を、
    また並べ替え直すことになります。

    書けたら、手元へ写してある値も読み直します(`master_admin.after_write`
    ── いまは直の時刻だけ)。**一覧を描く前に**済ませるので、返す状態は
    読み直したあとのものです。
    """
    result = master_admin.after_write(result, _file(body), _table(body),
                                      get_repo())
    view = master_presenter.browse(
        file=_file(body), table=_table(body), query=str(body.get("q", "")),
        sort=str(body.get("sort", "")),
        sort_dir=str(body.get("sort_dir", "asc")),
        unlocked=_unlocked(),
        message=result.message if result.ok else "")
    payload: dict[str, Any] = master_presenter.to_dict(view)
    if result.shift_times is not None:
        payload["shift_times"] = result.shift_times
    if result.ok:
        return jsonify(payload)
    payload["error"] = {"code": result.reason, "message": result.message}
    return jsonify(payload), STATUS.get(result.reason, 422)
