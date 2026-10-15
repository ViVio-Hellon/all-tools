"""プレビュー (VBA版 UFPreview.GeneratePreview)

- `POST /api/preview`             … {id, force, image} の点検表の画像(Word は PDF)を用意する。
                                    image = true なら Word も画像(PDF を出せない画面から)
- `GET  /api/preview/image/<key>` … 画像そのもの(`<img src>` なので `?t=` で認証)
- `GET  /api/preview/pdf/<key>`   … Word の点検表の PDF(枠の src なので `?t=` で認証)。
                                    **画面の中に出す**(Content-Disposition: inline。ダウンロードにしない)
- `POST /api/excel/warm`          … Excel を先に起動しておく(点検表を選び始めたとき)
"""
from __future__ import annotations

from urllib.parse import quote

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
        item_id, force=bool(body.get("force")), screen_id=request.headers.get(SCREEN_HEADER, ""),
        as_image=bool(body.get("image"))))


@bp.get("/api/preview/image/<key>")
def preview_image(key: str):
    data, mime = business().preview.get_image(key)
    return Response(data, mimetype=mime)


@bp.get("/api/preview/pdf/<key>")
def preview_pdf(key: str):
    """Word の点検表の PDF。**inline**(ブラウザ・WebView2 が枠の中に出す。保存の窓を出さない)。"""
    data, name = business().preview.get_pdf(key)
    response = Response(data, mimetype="application/pdf")
    # 名前は PDF 表示の題に出る。日本語は filename*(RFC 5987)、filename は ASCII の控え
    response.headers["Content-Disposition"] = (
        f"inline; filename=\"preview.pdf\"; filename*=UTF-8''{quote(name + '.pdf')}")
    return response


@bp.post("/api/excel/warm")
def warm_excel():
    """選び始めたら Excel を裏で起動しておく。プレビュー・印刷を押したときに待たせない。"""
    return jsonify({"ok": True, "started": business().excel.warm_up()}), 202
