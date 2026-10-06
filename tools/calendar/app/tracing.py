"""要求ごとの記録 ── 後から「何をして、なぜ断られ/落ちたか」を追えるように

エラーの後追いで困るのは、**事実が残っていない**こと。利用者に聞いても
「押したら赤いのが出た」くらいしか分からない。ここで残すのは3つ:

1. **操作** … 状態を変える要求(POST)を1行ずつ。何をした後に起きたかが分かる
2. **断り** … 400/403/409/422 と、その ``code`` と画面に出した文言。
   「登録できなかった」と言われたとき、**なぜ断ったか**がそのまま読める
3. **思わぬエラー** … 記録番号を付けて、例外の全文と入力を残す。
   画面にも同じ番号を出すので、利用者が言った番号からその行へたどれる

どの行にも要求ごとの追跡番号(``r=XXXXXX``)が付く(``logging_utils``)。
同じ番号の行を拾えば、その1回の操作で何が起きたかが揃う。

**記録が応答を壊さない。** 書けなくても、要求そのものは普通に返す。
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from flask import Flask, Response, g, jsonify, request
from werkzeug.exceptions import HTTPException

from calendar_app import logging_utils
from calendar_app.logging_utils import get_logger

log = get_logger("app.request")

#: 記録しない経路。**短い間隔で何度も来る**もの。1日に何万行も出て、
#: 肝心の操作が埋もれる(断られたときと、遅かったときは残す)
QUIET_PATHS = frozenset({"/api/alive", "/api/health", "/api/sync",
                         "/api/client-log"})

#: これより遅い応答は、読むだけの要求でも残す(ミリ秒)
SLOW_MS = 3000

#: 同じ断りが続くときにまとめる長さ(秒)。開きっぱなしの古いタブが
#: 1秒ごとに断られ続ける、のような場面でログを埋めない
REPEAT_WINDOW = 60

#: 入力を残す長さ。長い入力(CSV など)でログを膨らませない
BODY_LIMIT = 400

#: 伏せる入力。**合言葉はログに残さない**(共有フォルダに置かれうる)
_SECRET_HINTS = ("password", "pass", "パスワード", "token")

_repeat_lock = threading.Lock()
_repeats: dict[tuple[int, str, str], list[float]] = {}


def reset() -> None:
    """まとめていた断りを忘れる(試験の間で持ち越さないため)。"""
    with _repeat_lock:
        _repeats.clear()


def install(app: Flask) -> None:
    @app.before_request
    def _start():                                  # noqa: ANN202
        g.trace_token = logging_utils.set_trace("r=" + logging_utils.new_id())
        g.trace_t0 = time.monotonic()
        return None

    @app.after_request
    def _record(response):                         # noqa: ANN202
        try:
            _log_response(response)
        except Exception:                          # noqa: BLE001 - 記録で応答を壊さない
            pass
        return response

    @app.teardown_request
    def _end(_exc):                                # noqa: ANN202
        token = g.pop("trace_token", None)
        if token is not None:
            logging_utils.reset_trace(token)

    @app.errorhandler(Exception)
    def _unexpected(exc):                          # noqa: ANN202
        # 404・405 などは Flask の決まりどおりに返す(思わぬエラーではない)
        if isinstance(exc, HTTPException):
            return exc
        return unexpected_reply(exc)


def unexpected_reply(exc: BaseException):
    """思わぬエラーを記録番号付きで残し、画面にも同じ番号を返す。"""
    ref = logging_utils.new_ref()
    log.error("思わぬエラー %s %s: %s: %s\n  入力: %s",
              request.method, request.path, type(exc).__name__, exc,
              body_summary() or "(なし)", exc_info=exc, extra={"ref": ref})
    message = (f"思わぬエラーが起きました(記録番号 {ref})。"
               "もう一度やってもだめなときは、この番号を管理者に伝えてください。")
    if request.path.startswith("/api/") or request.path.startswith("/print"):
        return jsonify({"error": {"code": "internal", "message": message,
                                  "ref": ref}}), 500
    return Response(
        "<!doctype html><meta charset='utf-8'><title>エラー</title>"
        f"<p>{message}</p>", status=500, mimetype="text/html")


# ---------------------------------------------------------------------------
# 1行にまとめる
# ---------------------------------------------------------------------------
def _is_secret(key: str) -> bool:
    lowered = key.lower()
    return any(hint in lowered for hint in _SECRET_HINTS)


def _mask(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("***" if _is_secret(str(k)) and v else _mask(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_mask(v) for v in value]
    return value


def body_summary() -> str:
    """入力(JSON)とクエリを1行に。合言葉は伏せ、長いものは切る。"""
    parts: list[str] = []
    query = {k: v for k, v in request.args.items() if k != "t"}   # t は起動トークン
    if query:
        parts.append("query=" + json.dumps(_mask(query), ensure_ascii=False))
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        body = request.get_json(silent=True)
        if body not in (None, {}, []):
            parts.append("body=" + json.dumps(_mask(body), ensure_ascii=False))
    text = " ".join(parts)
    if len(text) > BODY_LIMIT:
        text = text[:BODY_LIMIT] + f"…(残り{len(text) - BODY_LIMIT}字)"
    return text


def _error_of(response: Response) -> tuple[str, str]:
    if not response.is_json:
        return "", ""
    body = response.get_json(silent=True) or {}
    info = body.get("error") if isinstance(body, dict) else None
    if not isinstance(info, dict):
        return "", ""
    return str(info.get("code", "")), str(info.get("message", ""))


def _repeated(key: tuple[int, str, str]) -> int:
    """同じ断りが続いているか。続いていれば -1、まとめて言う回数があればその数。"""
    now = time.monotonic()
    with _repeat_lock:
        last = _repeats.get(key)
        if last is not None and now - last[0] < REPEAT_WINDOW:
            last[1] += 1
            return -1
        skipped = int(last[1]) if last is not None else 0
        _repeats[key] = [now, 0]
        if len(_repeats) > 500:                    # 古いものを捨てる
            for k in [k for k, v in _repeats.items() if now - v[0] >= REPEAT_WINDOW]:
                _repeats.pop(k, None)
        return skipped


def _log_response(response: Response) -> None:
    status = response.status_code
    if status >= 500:
        return                       # ``unexpected_reply`` が記録番号付きで残している
    path = request.path
    method = request.method
    elapsed = (time.monotonic() - g.get("trace_t0", time.monotonic())) * 1000
    writing = method not in ("GET", "HEAD", "OPTIONS")

    if status >= 400:
        code, message = _error_of(response)
        skipped = _repeated((status, code, path))
        if skipped < 0:
            return
        more = f"(前の{REPEAT_WINDOW}秒に同じ断りがあと{skipped}回)" if skipped else ""
        summary = body_summary()
        log.info("断りました %s %s → %s %s: %s%s%s", method, path, status,
                 code or "-", message or "-", f" | {summary}" if summary else "",
                 more)
        return

    if writing and path not in QUIET_PATHS:
        summary = body_summary()
        log.info("操作 %s %s → %s (%.0fms)%s", method, path, status, elapsed,
                 f" | {summary}" if summary else "")
    elif elapsed >= SLOW_MS:
        log.warning("遅い応答 %s %s → %s (%.0fms)", method, path, status, elapsed)
