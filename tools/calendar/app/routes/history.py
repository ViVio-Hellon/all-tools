"""履歴表示 ── 過去の月を、編集できない形で見せる

    GET /history                  画面の器
    GET /api/history              選べる月の一覧と、1か月ぶん(?year=&month=)
    GET /api/history/day/<日付>   1日の登録内容(読むだけ)

**書き込む道は1つも置きません。** カレンダー画面で前月へ戻れば過去の月も
見えますが、そこは登録・削除ができる画面です。昔の月を調べている途中の
押し間違いで過去の記録が変わらないよう、見るだけの画面を分けています
(判断は ``calendar_app/presenters/history.py``)。
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, jsonify, render_template, request

from calendar_app.logging_utils import get_logger
from calendar_app.presenters import calendar as calendar_presenter
from calendar_app.presenters import history as presenter

from .. import shell
from . import get_db

log = get_logger("app.routes.history")

bp = Blueprint("history", __name__)


@bp.get("/history")
def page():
    return render_template("history.html", **shell.shell_context("history"))


@bp.get("/api/history")
def month():
    """選べる月の一覧と、その中の1か月。

    年月を省略したら**先月**。選べない月(今月以降・登録より前)は断る ──
    **一覧に無いものは選べない。**
    """
    conn = get_db()
    listed = presenter.choices(conn)
    payload: dict[str, Any] = {"choices": listed, "month": None}
    if listed["latest"] is None:
        return jsonify(payload)                   # 過去の登録が無い

    try:
        year = int(request.args.get("year", listed["latest"]["year"]))
        month_no = int(request.args.get("month", listed["latest"]["month"]))
    except (TypeError, ValueError):
        return _refuse("bad_request", "年月の指定が正しくありません。", 400)
    if not 1 <= month_no <= 12 or not presenter.is_selectable(conn, year, month_no):
        return _refuse(presenter.REFUSE_NOT_LISTED,
                       f"{year}年{month_no}月は履歴として選べません"
                       "(選べるのは、登録がある最も古い月から先月までです)。")

    payload["month"] = presenter.month_dict(conn, year, month_no)
    return jsonify(payload)


@bp.get("/api/history/day/<path:day>")
def day(day: str):
    """1日の登録内容。**削除に使う印は返さない**(読むだけ)。"""
    try:
        parsed = calendar_presenter.parse_date(day)
    except (ValueError, AttributeError):
        return _refuse("bad_request", "日付の形式が正しくありません。", 400)
    if not presenter.is_selectable(get_db(), parsed.year, parsed.month):
        return _refuse(presenter.REFUSE_NOT_LISTED,
                       "その日は履歴として選べません。")
    return jsonify(presenter.day_dict(get_db(), parsed))


def _refuse(code: str, message: str, status: int = 422):
    return jsonify({"error": {"code": code, "message": message}}), status
