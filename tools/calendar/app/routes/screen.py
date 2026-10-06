"""画面の名乗り (``/api/screen/*``)

    POST /api/screen/claim   この画面で使わせてほしい

同じ端末でタブを2枚開かせないための入口です。判断そのものは
``calendar_app/screen_lock.py`` が持っていて、ここは受け渡しだけ。

**断りは 409。** 形は正しく、**別の誰か(別の画面)が先に使っている**という
意味なので、設計 §1 の分け方ではここになります。
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from calendar_app import screen_lock
from calendar_app.logging_utils import get_logger

log = get_logger("app.routes.screen")

bp = Blueprint("screen", __name__)


@bp.post("/api/screen/claim")
def claim():
    """この画面で使ってよいか聞く。画面を出す前に1回だけ呼ぶ。

    ``force=true`` は「この画面で使う」を押されたとき。前の画面は
    次の心拍で自分が外れたことを知る。
    """
    body = request.get_json(silent=True) or {}
    screen_id = str(body.get("screen_id", ""))
    force = bool(body.get("force"))

    result = screen_lock.get().claim(screen_id, force=force)
    payload = {
        "ok": result.ok,
        "message": result.message,
        # いつから使われているか。**断るなら理由を具体的に出す**
        "holder_since": (result.holder.since_text()
                         if result.holder is not None else ""),
    }
    if result.ok:
        return jsonify(payload)

    log.info("2枚目の画面を断りました (使用中: %s から)", payload["holder_since"])
    payload["error"] = {"code": result.reason, "message": result.message}
    return jsonify(payload), 409
