"""直を消す・テストモード(設定・管理者の「取り込みと直し」/「この端末と配布」)

    GET  /api/test-mode                    テストモードのいま
    POST /api/test-mode                    入る(管理者パスワード)/ 抜ける
    GET  /api/settings/removal             手元のページの一覧(テスト / 中身なし / 指定して)
    POST /api/settings/removal/delete      選んだページを手元から消す(**管理者モードだけ**)
    POST /api/settings/removal/shared      消したページを共有からも消す / 手元だけにする(**管理者モードだけ**)

判断はどれもサービスが持ちます(`nippou/test_mode`・`services/shift_removal`)。ここは要求を
写して、断りの種類を HTTP に写すだけです。
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from nippou import admin_password, test_mode, user_settings, work_context
from nippou.logging_setup import get_logger, log_button_click

from .. import error_body, get_repo

log = get_logger("app.routes.removal")

bp = Blueprint("removal", __name__)


# ----------------------------------------------------------------------
# テストモード
# ----------------------------------------------------------------------
@bp.get("/api/test-mode")
def test_mode_state():
    return jsonify(test_mode.state().as_dict())


@bp.post("/api/test-mode")
def test_mode_switch():
    """入る(`enable: true` と管理者パスワード)/ 抜ける(`enable: false`。パスワードは要らない)。"""
    payload = request.get_json(silent=True) or {}
    if not payload.get("enable"):
        state = test_mode.leave(why="抜ける釦")
        log_button_click("test_mode_off")
        body = state.as_dict()
        body["message"] = "テストモードを抜けました(これから作る直は普通の直です)"
        return jsonify(body)
    password = str(payload.get("password", ""))
    try:
        ok = admin_password.verify(password)
    except user_settings.SettingsUnreadable as exc:
        log.warning("テストモードを断りました(設定ファイルを読めない): %s", exc)
        return jsonify(error_body(admin_password.REFUSE_UNREADABLE,
                                  admin_password.unreadable_message(exc))), 503
    if not ok:
        log.warning("テストモードの認証に失敗しました")
        return jsonify(error_body("bad_password", "パスワードが違います", field="password")), 403
    state = test_mode.enter()
    log_button_click("test_mode_on")
    body = state.as_dict()
    body["message"] = test_mode.BAND_TEXT + " に入りました"
    return jsonify(body)


# ----------------------------------------------------------------------
# 直を消す
# ----------------------------------------------------------------------
def _not_admin():
    return jsonify(error_body("not_admin", "ページを消すのは管理者モードでのみ可能です")), 403


@bp.get("/api/settings/removal")
def removal_list():
    """手元のページの一覧。**見るだけなので鍵は要りません**(消すのは管理者モードだけ)。"""
    from nippou.services import shift_removal

    body = shift_removal.find(
        get_repo(), kind=str(request.args.get("kind") or "all"),
        day=str(request.args.get("day") or "").strip(),
        line=str(request.args.get("line") or "").strip(),
        shift=str(request.args.get("shift") or "").strip())
    body["admin"] = work_context.get_context().admin
    return jsonify(body)


@bp.post("/api/settings/removal/delete")
def removal_delete():
    """選んだページを手元から消す。消す直前に、一覧に出したときと同じかをサーバが確かめる。"""
    from nippou.services import shift_removal

    if not work_context.get_context().admin:
        return _not_admin()
    payload = request.get_json(silent=True) or {}
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        return jsonify(error_body("bad_input", "消すページを選んでください")), 400
    log_button_click("pages_remove", extra=f"{len(items)}ページ")
    result = shift_removal.remove(get_repo(), items)
    body = result.as_dict()
    if result.error:
        return jsonify(error_body("not_kept", result.error)), 500
    # **何も消せなかったら 422**(形は正しいが、打たれていた・もう無い)
    return jsonify(body), (200 if result.removed else 422)


@bp.post("/api/settings/removal/shared")
def removal_shared():
    """「共有の日報データからも消しますか？」の答え。`remove: true` で共有からも消す。"""
    from nippou.services import shift_removal

    if not work_context.get_context().admin:
        return _not_admin()
    payload = request.get_json(silent=True) or {}
    ids = [str(i) for i in (payload.get("ids") or []) if str(i).strip()]
    if not ids:
        return jsonify(error_body("bad_input", "どの記録かが分かりません")), 400
    if not payload.get("remove"):
        log_button_click("pages_remove_local_only", extra=f"{len(ids)}件")
        kept = shift_removal.keep_local(get_repo(), ids)
        return jsonify({"local": kept, "message": f"共有からは消しませんでした(手元だけ消しました: {kept}ページ)",
                        "open": shift_removal.open_removals(get_repo())})
    log_button_click("pages_remove_shared", extra=f"{len(ids)}件")
    outcome = shift_removal.remove_shared(get_repo(), ids)
    body = outcome.as_dict()
    body["open"] = shift_removal.open_removals(get_repo())
    # **共有に届かなかったら 503**(手元は消したまま。共有はまだ、として残した)
    return jsonify(body), (503 if outcome.pending else 200)
