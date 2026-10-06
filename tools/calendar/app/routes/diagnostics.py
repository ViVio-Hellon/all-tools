"""エラーの後追い (設定画面「9. ログ」)

- ``POST /api/client-log``  … 画面(ブラウザ)で起きたエラーを記録する
- ``GET  /api/logs``         … いまの書き先と、最近の記録番号の一覧
- ``GET  /api/logs/report``  … 記録番号1つぶんの「なぜなぜ分析の材料」

**画面のエラーはサーバに届かないと誰も知らない。** JS の例外は
ブラウザの開発者ツールにしか出ず、現場では「押しても何も起きない」と
しか見えない。記録番号を付けてサーバのログに残し、画面にも同じ番号を出す。
"""

from __future__ import annotations

import threading
import time

from flask import Blueprint, jsonify, request

from calendar_app import log_report, logging_utils
from calendar_app.logging_utils import get_logger

log = get_logger("client")

bp = Blueprint("diagnostics", __name__)

#: 画面からの報告を受ける上限(1分あたり)。壊れた画面が毎秒同じ例外を
#: 投げても、ログを埋めない
CLIENT_LIMIT_PER_MIN = 30
#: 同じ文言は、この秒数のあいだ1回だけ残す
CLIENT_REPEAT_SEC = 600

_FIELD_LIMITS = {"kind": 40, "message": 500, "source": 300, "page": 300,
                 "stack": 4000, "screen": 40}

_lock = threading.Lock()
_recent: list[float] = []
_seen: dict[str, float] = {}


def reset() -> None:
    """受けた報告の数と文言を忘れる(試験の間で持ち越さないため)。"""
    with _lock:
        _recent.clear()
        _seen.clear()


def _clip(body: dict, key: str) -> str:
    text = str(body.get(key, "") or "").replace("\r", "")
    limit = _FIELD_LIMITS[key]
    return text if len(text) <= limit else text[:limit] + "…"


def _accept(message: str) -> bool:
    now = time.monotonic()
    with _lock:
        while _recent and now - _recent[0] > 60:
            _recent.pop(0)
        if len(_recent) >= CLIENT_LIMIT_PER_MIN:
            return False
        last = _seen.get(message)
        if last is not None and now - last < CLIENT_REPEAT_SEC:
            return False
        _recent.append(now)
        _seen[message] = now
        if len(_seen) > 200:
            for key in [k for k, v in _seen.items() if now - v >= CLIENT_REPEAT_SEC]:
                _seen.pop(key, None)
        return True


@bp.post("/api/client-log")
def client_log():
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        body = {}
    message = _clip(body, "message") or "(文言なし)"
    if not _accept(message):
        return jsonify({"ok": False, "ref": ""})

    ref = logging_utils.new_ref()
    stack = _clip(body, "stack")
    line, col = body.get("line", ""), body.get("col", "")
    lines = [f"  種類: {_clip(body, 'kind') or 'error'}",
             f"  画面: {_clip(body, 'page') or '-'}"]
    source = _clip(body, "source")
    if source:
        lines.append(f"  場所: {source}:{line}:{col}")
    if stack:
        # **字下げして残す。** 1行目に見える形で書くと、ログを読む側が
        # 別の記録の始まりと取り違える
        lines += ["    " + s for s in stack.splitlines() if s.strip()]
    log.error("画面でエラー: %s\n%s", message, "\n".join(lines),
              extra={"ref": ref})
    return jsonify({"ok": True, "ref": ref})


@bp.get("/api/logs")
def logs():
    """いまの書き先と、最近7日の記録番号。**重いので設定画面とは別に取る。**"""
    return jsonify({
        "status": logging_utils.status(),
        "errors": log_report.recent_errors(days=7, limit=50),
        "days": 7,
    })


@bp.get("/api/logs/report")
def report():
    ref = request.args.get("ref", "").strip().upper()
    text = log_report.report(ref)
    if text is None:
        return jsonify({"error": {
            "code": "not_found",
            "message": f"記録番号 {ref or '(空)'} の記録が見つかりません。"
                       "番号を確かめてください(ログは"
                       f"{logging_utils.KEEP_DAYS}日で消えます)。"}}), 404
    return jsonify({"ref": ref, "text": text})
