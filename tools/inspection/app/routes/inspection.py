"""点検表一覧 (VBA版 GetWorkbooksFromFolder / CreateCheckBoxes)

- `GET  /api/inventory`         … 一覧。`?wait=秒` で検索の終わりを少し待つ
- `POST /api/inventory/refresh` … 再読込(フォルダを検索し直す)
- `GET  /api/inventory/status`  … 検索の進み具合
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from .. import current_business as business

bp = Blueprint("inspection", __name__)


@bp.get("/api/inventory")
def inventory():
    biz = business()
    try:
        wait = float(request.args.get("wait", "0") or 0)
    except ValueError:
        wait = 0.0
    # 一覧を出せるなら待たない(前回の控え・再読込の途中)。画面は出ている一覧を
    # そのまま見せて、確認が終わったら替える
    if wait > 0 and biz.inspection.inventory() is None:
        biz.inspection.wait(min(wait, 10.0))
    inv = biz.inspection.inventory()
    return jsonify({"ok": True, "scan": biz.inspection.status(), "settings": biz.settings.to_dict(),
                    "inventory": inv.to_dict() if inv is not None else None})


@bp.post("/api/inventory/refresh")
def refresh():
    biz = business()
    started = biz.inspection.start_scan("画面の再読込")
    return jsonify({"ok": True, "started": started, "scan": biz.inspection.status()})


@bp.get("/api/inventory/status")
def status():
    return jsonify({"ok": True, "scan": business().inspection.status()})
