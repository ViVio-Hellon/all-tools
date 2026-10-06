"""印刷 (VBA版 PrintWorkbooksFromFolder / DoPrint)

- `POST /api/print`        … {ids, copies} を選んだ順に印刷(202で受け付け、裏で進む)
- `GET  /api/print/status` … 進み具合(画面を開き直しても戻せる)
- `POST /api/print/cancel` … 中止(いまの1件が終わったところで止まる)
- `GET  /api/printer`      … 既定のプリンター名
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from app.errors import ApiError

from .. import current_business as business

bp = Blueprint("printing", __name__)


@bp.post("/api/print")
def start_print():
    body = request.get_json(silent=True) or {}
    ids = body.get("ids")
    if not isinstance(ids, list):
        raise ApiError(400, "BAD_REQUEST", "印刷する点検表の指定が正しくありません。")
    screen = request.headers.get("X-Screen-Id", "")
    return jsonify({"ok": True, "job": business().printing.start(ids, body.get("copies", 1), screen)}), 202


@bp.get("/api/print/status")
def print_status():
    return jsonify({"ok": True, "job": business().printing.status()})


@bp.post("/api/print/cancel")
def cancel_print():
    return jsonify({"ok": True, "job": business().printing.cancel()})


@bp.get("/api/printer")
def printer():
    return jsonify({"ok": True, "printer": business().default_printer() or ""})
