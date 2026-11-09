"""案内表示 (`/api/notice/*`。v4.47.0)

    GET  /api/notice/due    いま出す案内(無ければ notice=null)。画面の1分ごとの見張りが聞く
    POST /api/notice/ack    押した(名前を選んで「点検済み」など)。押すまで画面は閉じない

決まりは `logic/guide_notice.py`、表と記録は `services/guide_notice.py`。比べるラインは
**この端末のライン設定**(管理者がほかのラインを見ていても、その PC の持ち場の案内を出す)。
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from nippou import work_context
from nippou.logging_setup import get_logger, log_button_click
from nippou.services import guide_notice

from .. import error_body, get_repo

log = get_logger("app.routes.notice")

bp = Blueprint("notice", __name__)


def _line(ctx) -> str:
    return ctx.terminal_line if work_context.terminal_line_decided() else ""


@bp.get("/api/notice/due")
def due():
    from ..current import current_calculator

    ctx = work_context.get_context()
    try:
        got = guide_notice.due_now(get_repo(), current_calculator(), _line(ctx),
                                   force_day_shift=ctx.force_day_shift())
    except Exception:                             # noqa: BLE001 - 案内が出ないだけ
        log.exception("案内表示を確かめられませんでした")
        return jsonify({"notice": None})
    return jsonify(got.as_dict())


@bp.post("/api/notice/ack")
def ack():
    """押した。**いま出している案内と同じ直**のときだけ受け付ける(古い画面の押し直しで、
    次の直の案内を押したことにしない)。名前を選ぶ案内は名前が要る。"""
    from ..current import current_calculator

    ctx = work_context.get_context()
    payload = request.get_json(silent=True) or {}
    notice_id = str(payload.get("id", "")).strip()
    worker = " ".join(str(payload.get("worker", "")).split())
    got = guide_notice.due_now(get_repo(), current_calculator(), _line(ctx),
                               force_day_shift=ctx.force_day_shift())
    notice = guide_notice.find(notice_id)
    if notice is None:
        return jsonify(error_body("no_notice", "その案内はもうありません")), 404
    key = (str(payload.get("report_date", "")), str(payload.get("shift", "")))
    if key != (got.report_date, got.shift):
        return jsonify(error_body("moved", "直が変わりました。画面の案内を出し直します")), 409
    if notice.pick_worker and not worker:
        return jsonify(error_body("no_worker", "押した人の名前を選んでください", field="worker")), 422
    result = guide_notice.record(get_repo(), notice, report_date=got.report_date,
                                 line=got.line, shift=got.shift, worker=worker)
    log_button_click("guide_notice_ack", line=got.line,
                     extra=f"{notice.id} {got.report_date}/{got.shift} {worker}")
    return jsonify({"ok": result.ok, "message": result.message, "path": result.shared_path})
