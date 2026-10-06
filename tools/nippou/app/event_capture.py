"""出来事を**拾う口** ── 要求ごとの操作・断り・予期しないエラー

    エラー等の後追いが現状できないと感じている

【ここがやること】
    1. 要求ごとに、**どの画面・どのタブ・どの直**で何をしたかを残す
       (`logic/event_log.should_record` が残すものを決める)
    2. 業務としての断り(4xx)は、**画面に出した文言ごと**残す
       ── 「なぜ保存できなかったのか」は断りの文言そのものなので
    3. 予期しないエラーに**番号**を付け、画面にも同じ番号を出す
       (前は「通信に失敗しました (HTTP 500)」とだけ出て、記録のどれが
       そのエラーなのか結び付きませんでした)
    4. 要求の最中に `log.warning` / `log.exception` で書かれた記録にも、
       同じ要求の番号・画面・直を添える(`services/event_log.EventHandler`)

**記録のせいで要求を落とさない。** ここで何が起きても握りつぶします。
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime

from flask import Flask, g, has_request_context, jsonify, request
from werkzeug.exceptions import HTTPException

from nippou.logging_setup import get_logger
from nippou.logic import event_log as rule
from nippou.services import event_log

log = get_logger("app.events")


def request_context() -> dict:
    """いまの要求の様子。**要求の外なら空**(裏で動く処理)。"""
    if not has_request_context():
        return {}
    out = {
        "screen": request.headers.get("X-Screen", "")
                  or ("" if request.path.startswith("/api/") else request.path),
        "tab": request.headers.get("X-Tab", ""),
        "request": getattr(g, "event_request", ""),
        "action": f"{request.method} {request.path}",
    }
    try:
        from nippou import work_context

        ctx = work_context.get_context()
        out["line"] = ctx.line
        out["admin"] = bool(ctx.admin)
        if ctx.recall.active:
            r = ctx.recall
            out["recall"] = True
            out["key"] = rule.key_text(recall=True, report_date=r.report_date,
                                       line=r.line, shift=r.shift, page=r.page)
        elif ctx.anchor.filled:
            a = ctx.anchor
            out["key"] = rule.key_text(recall=False, report_date=a.report_date,
                                       line=a.line, shift=a.shift)
    except Exception:                             # noqa: BLE001 - 様子が取れないだけ
        pass
    return out


def _input() -> dict:
    """送られた中身の要約(伏せ字つき)。"""
    try:
        if request.method == "GET":
            args = {k: v for k, v in request.args.items() if k != "t"}
            return rule.summarize_input(args) if args else {}
        if request.files:
            names = [f.filename for f in request.files.getlist("files")
                     or list(request.files.values())]
            data = {"files": names, **request.form.to_dict()}
            return rule.summarize_input(data)
        data = request.get_json(silent=True)
        if data is None and request.form:
            data = request.form.to_dict()
        return rule.summarize_input(data) if data else {}
    except Exception:                             # noqa: BLE001
        return {}


def _base_fields(status: int) -> dict:
    started = getattr(g, "event_started", None)
    fields = request_context()
    fields.update({
        "terminal": event_log.terminal_name(),
        "version": _version(),
        "status": status,
        "ms": int((time.monotonic() - started) * 1000) if started else "",
        "input": _input(),
    })
    try:
        data = request.get_json(silent=True) if request.method != "GET" else None
    except Exception:                             # noqa: BLE001
        data = None
    fields["label"] = rule.request_label(request.method, request.path, data)
    return fields


def _version() -> str:
    try:
        from flask import current_app
        return str(current_app.config.get("VERSION", ""))
    except Exception:                             # noqa: BLE001
        return ""


def _response_message(response) -> str:
    """断りの文言。**画面に出たものと同じ**を残す。"""
    try:
        if not response.is_json:
            return ""
        body = response.get_json(silent=True) or {}
        info = body.get("error") if isinstance(body.get("error"), dict) else {}
        return str(info.get("message") or body.get("message") or "")
    except Exception:                             # noqa: BLE001
        return ""


def user_message(event_id: str) -> str:
    return (f"思わぬエラーが起きました(エラー番号 {event_id})。もう一度押しても"
            "同じなら、この番号を管理者に伝えてください。設定・管理者の「ログ」で"
            "このときの様子を見られます。")


def register(app: Flask) -> None:
    """拾う口を付ける。**ほかのフックより先に**呼ぶ(時間を測り始めるため)。"""
    event_log.set_context_provider(request_context)

    @app.before_request
    def _event_start():                           # noqa: ANN202 - Flaskのフック
        g.event_started = time.monotonic()
        g.event_request = uuid.uuid4().hex[:8]

    @app.after_request
    def _event_record(response):                  # noqa: ANN202 - Flaskのフック
        try:
            if getattr(g, "event_done", False):
                return response
            quiet = request.headers.get("X-Quiet") == "1"
            status = response.status_code
            if not rule.should_record(request.method, request.path, status,
                                      quiet=quiet):
                return response
            kind = rule.kind_of_status(status)
            fields = _base_fields(status)
            if kind != rule.KIND_OP:
                fields["message"] = _response_message(response)
            event_log.record(rule.make(kind, at=datetime.now(), **fields))
        except Exception:                         # noqa: BLE001 - 記録で落とさない
            pass
        return response

    @app.errorhandler(Exception)
    def _unexpected(exc):                         # noqa: ANN202 - Flaskのフック
        if isinstance(exc, HTTPException):
            return exc
        now = datetime.now()
        event_id = rule.new_id(rule.KIND_ERROR, now)
        message = user_message(event_id)
        try:
            fields = _base_fields(500)
            fields.update(event_log.describe_exception(exc))
            event_log.record(rule.make(rule.KIND_ERROR, at=now, id=event_id,
                                       message=message, **fields))
        except Exception:                         # noqa: BLE001
            pass
        g.event_done = True
        # 文字の記録(nippou.log)にも番号つきで。**出来事にはもう残した**ので
        # 拾う口(EventHandler)には二重に残させない
        log.error("[%s] 予期しないエラー: %s %s", event_id, request.method,
                  request.path, exc_info=exc, extra={"event_id": event_id})
        if request.path.startswith("/api/"):
            return jsonify({"error": {"code": "internal", "message": message,
                                      "event_id": event_id}}), 500
        return (_error_page(event_id, message), 500,
                {"Content-Type": "text/html; charset=utf-8"})


def _error_page(event_id: str, message: str) -> str:
    """画面(HTML)で起きたとき。**部品に頼らず**その場で書く(壊れていても出る)。"""
    from html import escape

    return (
        "<!doctype html><html lang='ja'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>エラー</title><style>"
        ":root{--bg:#fff;--fg:#1d2433;--line:#c9ced8;--accent:#b42318}"
        "@media (prefers-color-scheme:dark){:root{--bg:#141820;--fg:#e6e9ef;"
        "--line:#3a4252;--accent:#ff8a7a}}"
        "body{background:var(--bg);color:var(--fg);font-family:sans-serif;"
        "margin:0;padding:32px 16px}main{max-width:640px;margin:0 auto}"
        "h1{font-size:20px;color:var(--accent)}code{font-size:18px;font-weight:bold;"
        "border:1px solid var(--line);padding:2px 8px;border-radius:4px}"
        "a{color:inherit}</style></head><body><main>"
        f"<h1>思わぬエラーが起きました</h1><p>エラー番号 <code>{escape(event_id)}</code></p>"
        f"<p>{escape(message)}</p>"
        "<p><a href='/'>日報入力へ戻る</a> ・ <a href='/settings?tab=logs'>ログを見る</a></p>"
        "</main></body></html>")
