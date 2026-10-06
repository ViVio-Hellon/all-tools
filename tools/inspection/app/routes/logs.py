"""ログ(後追い・なぜなぜ分析)

- `GET  /api/logs`               … 書き出し先の状態と、この端末の最近のエラー・断り
- `POST /api/settings/logs`      … {log_dir} / {reset_log_dir} / {keep_days} 保存先・保存日数
- `GET  /api/logs/find?ref=…`    … 問い合わせ番号の出来事と、その直前に起きていたこと
- `POST /api/logs/open-folder`   … ログのフォルダをエクスプローラーで開く(Windows)
- `POST /api/client-log`         … 画面(ブラウザ)の中で起きたエラーを残す

ログの読み書きの仕組みは `core/logging_utils.py`・`core/event_log.py`。
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Deque, Dict

from flask import Blueprint, jsonify, request

from app.errors import ApiError
from core import event_log, logging_utils

from .. import SCREEN_HEADER
from .. import current_business as business

bp = Blueprint("logs", __name__)

RECENT_DAYS = 7
RECENT_LIMIT = 100


def _state(**extra):
    status = logging_utils.status()
    rows = [e for e in event_log.read(days=RECENT_DAYS, limit=2000)
            if e.get("result") in (event_log.NG, event_log.REFUSED)][:RECENT_LIMIT]
    return {"ok": True, "logs": status, "recent": rows, "recent_days": RECENT_DAYS,
            "keep_days_range": [logging_utils.MIN_KEEP_DAYS, logging_utils.MAX_KEEP_DAYS], **extra}


@bp.get("/api/logs")
def get_logs():
    return jsonify(_state())


@bp.post("/api/settings/logs")
def update_logs():
    biz = business()
    body = request.get_json(silent=True) or {}
    before = logging_utils.status()
    updates = {}
    if body.get("reset_log_dir"):
        updates[logging_utils.KEY_DIR] = None
    elif "log_dir" in body:
        folder = logging_utils.normalize_dir(body.get("log_dir"))
        if not folder:
            raise ApiError(400, "EMPTY_FOLDER", "ログの保存先を入力してください。")
        problem = logging_utils.check_dir(folder)
        if problem:
            # **保存しない。** 書けないフォルダを指定すると、黙ってローカルへ退避し続ける
            raise ApiError(422, "LOG_DIR_UNWRITABLE", "そのフォルダにはログを書けません。", f"{folder}: {problem}")
        updates[logging_utils.KEY_DIR] = folder
    if "keep_days" in body:
        try:
            days = int(body.get("keep_days"))
        except (TypeError, ValueError):
            raise ApiError(400, "BAD_KEEP_DAYS", "保存日数は数字で入力してください。")
        if not logging_utils.MIN_KEEP_DAYS <= days <= logging_utils.MAX_KEEP_DAYS:
            raise ApiError(400, "BAD_KEEP_DAYS",
                           f"保存日数は {logging_utils.MIN_KEEP_DAYS}～{logging_utils.MAX_KEEP_DAYS} 日で入力してください。")
        updates[logging_utils.KEY_KEEP_DAYS] = days
    if not updates:
        raise ApiError(400, "NOTHING_TO_SAVE", "変える項目がありません。")
    biz.settings.put_many(updates)
    after = biz.apply_log_settings()
    event_log.record("settings.logs", event_log.INFO, before_dir=before["dir"], after_dir=after["dir"],
                     keep_days=after["keep_days"], requested=after["requested"] or "(既定)")
    return jsonify(_state(message="ログの設定を保存しました。"))


@bp.get("/api/logs/find")
def find():
    ref = (request.args.get("ref") or "").strip().upper()
    if not ref:
        raise ApiError(400, "NO_REF", "問い合わせ番号を入力してください。")
    rows = event_log.read(ref=ref, limit=200)
    if not rows:
        raise ApiError(404, "REF_NOT_FOUND", f"問い合わせ番号 {ref} の記録が見つかりません。",
                       "別の端末の番号なら、その端末のログの保存先(共有フォルダ)を指定すると探せます。")
    rows.sort(key=lambda e: e.get("ts", ""))
    first = rows[0]
    before = [e for e in event_log.timeline(first) if e.get("ref") != ref]
    return jsonify({"ok": True, "ref": ref, "events": rows, "before": before})


@bp.post("/api/logs/open-folder")
def open_folder():
    folder = str(logging_utils.log_dir())
    if os.name != "nt":
        raise ApiError(501, "UNSUPPORTED", "この環境ではフォルダを開けません。", folder)
    try:
        os.startfile(folder)                        # type: ignore[attr-defined]  # noqa: S606
    except OSError as exc:
        raise ApiError(500, "OPEN_FAILED", "ログのフォルダを開けませんでした。", f"{folder}: {exc}")
    return jsonify({"ok": True, "dir": folder})


# ------------------------------------------------------------------
# 画面の中で起きたエラー
# ------------------------------------------------------------------
CLIENT_KINDS = frozenset({"js_error", "unhandled", "offline", "api_error"})
CLIENT_LIMIT = 30                 # 1つの画面から 10 分に残す上限(壊れた画面で埋めない)
CLIENT_WINDOW_SEC = 600
_client_lock = threading.Lock()
_client_seen: Dict[str, Deque[float]] = {}


@bp.post("/api/client-log")
def client_log():
    body = request.get_json(silent=True) or {}
    kind = str(body.get("kind") or "")
    if kind not in CLIENT_KINDS:
        raise ApiError(400, "BAD_KIND", "記録の種類が違います。")
    screen = request.headers.get(SCREEN_HEADER, "")
    now = time.monotonic()
    with _client_lock:
        seen = _client_seen.setdefault(screen, deque())
        while seen and now - seen[0] > CLIENT_WINDOW_SEC:
            seen.popleft()
        if len(seen) >= CLIENT_LIMIT:
            return jsonify({"ok": True, "recorded": False})
        seen.append(now)
        if len(_client_seen) > 200:
            _client_seen.pop(next(iter(_client_seen)))

    def text(key: str, limit: int = 2000) -> str:
        return str(body.get(key) or "")[:limit]

    ref = event_log.record(
        f"client.{kind}", event_log.NG, message=text("message", 500), detail=text("detail"),
        page=text("page", 200), action=text("action", 200), related_ref=text("ref", 40),
        status=body.get("status") if isinstance(body.get("status"), int) else None,
        user_agent=request.headers.get("User-Agent", "")[:200])
    return jsonify({"ok": True, "recorded": True, "ref": ref})
