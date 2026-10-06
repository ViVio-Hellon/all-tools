"""設定(点検表フォルダ・表示テーマ・管理者パスワード・配布設定)と診断情報

- `GET  /api/settings`    … いまの設定(管理者パスワードは「変えてあるか」だけ。値は出さない)
- `POST /api/settings`    … {root_folder} / {reset_root_folder} / {dark_mode}
- `POST /api/settings/admin-password`         … {current, new, confirm} / {current, reset}
- `POST /api/settings/distribution/export`    … {password, items}
- `POST /api/settings/distribution/reapply`   … {password}
- `POST /api/settings/distribution/remove`    … {password}
- `GET  /api/fs/list`     … フォルダ参照(VBA版 SelectFolderDialog の置き換え)
- `GET  /api/diagnostics` … 困ったときに見る情報(版・パス・Excel・プリンター)
"""
from __future__ import annotations

import os
import sys

from flask import Blueprint, current_app, jsonify, request

from app.errors import ApiError
from app.services import fs_browse
from core import app_config, logging_utils

from .. import current_business as business

bp = Blueprint("settings", __name__)


def _state(biz, **extra):
    """設定画面が描くもの一式。**パスワードの値は入れない。**"""
    from app.services import admin_password
    return {"ok": True, "settings": biz.settings.to_dict(), "demo": biz.demo,
            "admin": {"custom": biz.admin.is_custom(), "min_length": admin_password.MIN_LENGTH},
            "distribution": biz.distribution.summary(), **extra}


@bp.get("/api/settings")
def get_settings():
    return jsonify(_state(business()))


@bp.post("/api/settings")
def update_settings():
    biz = business()
    body = request.get_json(silent=True) or {}
    before_folder = biz.settings.effective_root_folder()
    rescan = False
    if body.get("reset_root_folder") or "root_folder" in body:
        if biz.demo:
            raise ApiError(422, "DEMO_MODE", "模擬モードでは点検表フォルダを変更できません。")
    if body.get("reset_root_folder"):
        biz.settings.update(reset_root_folder=True)
        rescan = True
    elif "root_folder" in body:
        folder = str(body.get("root_folder") or "").strip().strip('"')
        if not folder:
            raise ApiError(400, "EMPTY_FOLDER", "点検表フォルダを入力してください。")
        if not os.path.isdir(os.path.normpath(folder)):
            raise ApiError(422, "FOLDER_NOT_FOUND",
                           "指定されたフォルダが見つかりません。パスを確認してください。", folder)
        biz.settings.update(root_folder=folder)
        rescan = True
    if "dark_mode" in body:
        biz.settings.update(dark_mode=bool(body.get("dark_mode")))
    if rescan:
        from core import event_log
        event_log.record("settings.folder", event_log.INFO, before=before_folder,
                         after=biz.settings.effective_root_folder())
        biz.inspection.clear()
        biz.inspection.start_scan("フォルダ設定の変更")
    return jsonify(_state(biz, rescan=rescan))


# ------------------------------------------------------------------
# 管理者パスワード(`app/services/admin_password.py`)。値は保存も応答もしない
# ------------------------------------------------------------------
@bp.post("/api/settings/admin-password")
def change_admin_password():
    biz = business()
    body = request.get_json(silent=True) or {}
    if body.get("reset"):
        result = biz.admin.reset(str(body.get("current", "")))
    else:
        result = biz.admin.change(str(body.get("current", "")), str(body.get("new", "")),
                                  str(body.get("confirm", "")))
    if not result.ok:
        # 入力の形の誤り。**サーバの状態は動いていない**
        raise ApiError(400, result.reason, result.message)
    from core import event_log
    event_log.record("settings.admin_password", event_log.INFO,
                     action="既定に戻した" if body.get("reset") else "変えた")   # **値は残さない**
    return jsonify(_state(biz, message=result.message))


# ------------------------------------------------------------------
# 配布設定(`app/services/distribution.py`)。どれも管理者パスワードが要る
# ------------------------------------------------------------------
def _distribution_reply(biz, result):
    from app.services import distribution
    if not result.ok:
        status = {distribution.REFUSE_NEED_PASSWORD: 403,
                  distribution.REFUSE_FAILED: 500}.get(result.reason, 400)
        raise ApiError(status, result.reason, result.message)
    action = request.path.rsplit("/", 1)[-1]
    if action in ("export", "remove"):
        from core import event_log
        event_log.record("settings.distribution", event_log.INFO,
                         action={"export": "書き出し", "remove": "削除"}[action],
                         items=(request.get_json(silent=True) or {}).get("items"))
    if getattr(result, "logs_changed", False):
        biz.apply_log_settings()
    rescan = bool(result.root_changed) and not biz.demo
    if rescan:
        # 点検表フォルダが替わった。前のフォルダの一覧を出さない
        biz.inspection.clear()
        biz.inspection.start_scan("配布設定の読み込み")
    return jsonify(_state(biz, message=result.message, rescan=rescan))


@bp.post("/api/settings/distribution/export")
def export_distribution():
    """この端末のいまの設定を、配布設定として書き出す。"""
    biz = business()
    body = request.get_json(silent=True) or {}
    items = body.get("items")
    if not isinstance(items, list) or not all(isinstance(x, str) for x in items):
        raise ApiError(400, "bad_input", "入れる項目の形が違います。")
    return _distribution_reply(biz, biz.distribution.export(str(body.get("password", "")), items))


@bp.post("/api/settings/distribution/reapply")
def reapply_distribution():
    """置いてある配布設定を読み込み直す(この端末の値も上書き)。"""
    biz = business()
    body = request.get_json(silent=True) or {}
    return _distribution_reply(biz, biz.distribution.reapply(str(body.get("password", ""))))


@bp.post("/api/settings/distribution/remove")
def remove_distribution():
    biz = business()
    body = request.get_json(silent=True) or {}
    return _distribution_reply(biz, biz.distribution.remove(str(body.get("password", ""))))


@bp.get("/api/fs/list")
def fs_list():
    biz = business()
    path = request.args.get("path") or biz.settings.effective_root_folder()
    return jsonify(fs_browse.to_dict(fs_browse.browse(path, biz.cfg.extensions)))


@bp.get("/api/diagnostics")
def diagnostics():
    biz = business()
    config = current_app.config
    return jsonify({
        "ok": True,
        "app": {"app_id": config["APP_ID"], "version": config["VERSION"], "pid": os.getpid(),
                "python": sys.version.split()[0], "app_root": str(app_config.APP_ROOT),
                "port": config["PORT"], "started_at": config["STARTED_AT"], "demo": biz.demo,
                "edition": "desktop" if config.get("BRIDGE") else "browser"},
        "paths": {"local_root": str(app_config.local_root()),
                  "logs": logging_utils.status(),
                  "cache": str(app_config.local_dir("cache"))},
        "excel": {"backend": biz.excel.backend_name, "activity": biz.excel.activity,
                  "tracked_processes": biz.registry.entries()},
        "printer": biz.default_printer() or "",
        "scan": biz.inspection.status(),
        "settings": biz.settings.to_dict(),
    })
