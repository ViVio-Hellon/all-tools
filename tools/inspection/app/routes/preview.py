"""プレビュー (VBA版 UFPreview.GeneratePreview)

- `POST /api/preview`             … {id, force} の点検表の画像を用意する
- `GET  /api/preview/image/<key>` … 画像そのもの(`<img src>` なので `?t=` で認証)
- `POST /api/excel/warm`          … Excel を先に起動しておく(点検表を選び始めたとき)
"""
from __future__ import annotations

from flask import Blueprint, Response, jsonify, request

from app.errors import ApiError

from .. import SCREEN_HEADER
from .. import current_business as business

bp = Blueprint("preview", __name__)


@bp.post("/api/preview")
def make_preview():
    body = request.get_json(silent=True) or {}
    item_id = str(body.get("id") or "").strip()
    if not item_id:
        raise ApiError(400, "NO_ITEM", "プレビューする点検表を選んでください。")
    return jsonify(business().preview.get_preview(
        item_id, force=bool(body.get("force")), screen_id=request.headers.get(SCREEN_HEADER, "")))


@bp.get("/api/preview/image/<key>")
def preview_image(key: str):
    data, mime = business().preview.get_image(key)
    return Response(data, mimetype=mime)


@bp.post("/api/excel/warm")
def warm_excel():
    """選び始めたら Excel を裏で起動しておく。プレビュー・印刷を押したときに待たせない。"""
    return jsonify({"ok": True, "started": business().excel.warm_up()}), 202
