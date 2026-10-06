"""ログ ── エラーの一覧・なぜなぜシート・CSV・画面のエラーの受け口

設定・管理者の「ログ」の面から使います。**鍵は要りません** ── 見るのも、
なぜなぜを書くのも、困った人がその場でやることなので。

判断(何を一覧に出すか・経過をどこまで拾うか・シートの形)は
`nippou/logic/event_log.py`、ファイルを読むのは `nippou/services/event_log.py`。
ここは受け取って渡すだけです。
"""
from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta

from flask import Blueprint, jsonify, request

from nippou.logging_setup import get_logger, log_status
from nippou.logic import event_log as rule
from nippou.services import event_log

from .. import error_body
from ..event_capture import request_context

bp = Blueprint("logs", __name__)
log = get_logger("routes.logs")

#: 一覧の既定の期間(日)。**探しに来た人が見たいのは最近のもの**
DEFAULT_DAYS = 7
#: いちどに読む最大の期間(日)。半年ぶんを一度に読むと待たせるので
MAX_DAYS = 93

# 画面のエラーの受け口は、壊れた画面が同じエラーを毎秒送ってくることがある。
# **1分に30件まで**(それ以上は数だけ数えて捨てる)
_client_lock = threading.Lock()
_client_window: dict[str, float] = {"start": 0.0, "count": 0}
CLIENT_PER_MINUTE = 30


def _period() -> tuple[date, date]:
    """期間。読めなければ既定(今日まで7日)。長すぎれば切る。"""
    today = date.today()
    try:
        end = date.fromisoformat(request.args.get("end", "") or today.isoformat())
    except ValueError:
        end = today
    try:
        start = date.fromisoformat(request.args.get("start", "")
                                   or (end - timedelta(days=DEFAULT_DAYS - 1)).isoformat())
    except ValueError:
        start = end - timedelta(days=DEFAULT_DAYS - 1)
    if start > end:
        start, end = end, start
    if (end - start).days >= MAX_DAYS:
        start = end - timedelta(days=MAX_DAYS - 1)
    return start, end


def _filters(source) -> dict:
    scope = source.get("scope", rule.DEFAULT_SCOPE)
    return {"scope": scope if scope in rule.SCOPES else rule.DEFAULT_SCOPE,
            "terminal": str(source.get("terminal", "") or ""),
            "text": str(source.get("q", "") or "")}


def _status() -> dict:
    out = event_log.status()
    text = log_status()
    out["text_log"] = text["path"]
    out["text_problem"] = text["problem"]
    return out


@bp.get("/api/log/events")
def events():
    """一覧。**新しい順**。なぜなぜを書いたものには状態を添える。"""
    start, end = _period()
    filters = _filters(request.args)
    records = event_log.load(start, end)
    picked = rule.select(records, **filters)
    notes = event_log.analyses()
    counts = {key: sum(1 for r in records if r.get("kind") in kinds)
              for key, kinds in rule.SCOPES.items()}
    return jsonify({
        "rows": [rule.list_row(r, notes.get(str(r.get("id", "")))) for r in picked],
        "start": start.isoformat(), "end": end.isoformat(),
        "filters": filters,
        "counts": counts,
        "terminals": event_log.terminals(records),
        "status": _status(),
        "scopes": [{"key": k, "label": v} for k, v in rule.SCOPE_LABELS.items()],
    })


@bp.get("/api/log/event/<event_id>")
def event(event_id: str):
    """1件のなぜなぜシート。**それまでの経過**(同じタブの前30分)を添える。"""
    target, records = event_log.find(event_id)
    if target is None:
        return jsonify(error_body(
            "not_found", f"番号 {event_id} の記録が見つかりません"
            "(番号の打ち間違いか、置き場所を変える前の記録です)")), 404
    steps = rule.timeline(records, target)
    note = event_log.analyses().get(event_id)
    return jsonify({"sheet": rule.sheet(target, steps, note),
                    "why_count": rule.WHY_COUNT,
                    "statuses": [{"key": k, "label": v}
                                 for k, v in rule.STATUS_LABELS.items()]})


@bp.post("/api/log/event/<event_id>/analysis")
def save_analysis(event_id: str):
    """なぜなぜを残す。**ログの置き場所に**書きます(共有なら全端末から見える)。"""
    target, records = event_log.find(event_id)
    if target is None:
        return jsonify(error_body("not_found", f"番号 {event_id} の記録が見つかりません")), 404
    body = request.get_json(silent=True) or {}
    note = event_log.save_analysis(event_id, body)
    steps = rule.timeline(records, target)
    log.info("なぜなぜを残しました: %s (%s)", event_id,
             rule.STATUS_LABELS.get(note["status"], ""))
    return jsonify({"sheet": rule.sheet(target, steps, note),
                    "message": f"{event_id} のなぜなぜを残しました"})


@bp.post("/api/log/csv")
def export_csv():
    """いまの絞り込みのままCSVへ。**出した先のパスを返す**(Excel で開く)。"""
    body = request.get_json(silent=True) or {}
    try:
        start = date.fromisoformat(str(body.get("start", "")))
        end = date.fromisoformat(str(body.get("end", "")))
    except ValueError:
        end = date.today()
        start = end - timedelta(days=DEFAULT_DAYS - 1)
    if start > end:
        start, end = end, start
    records = rule.select(event_log.load(start, end), limit=100_000,
                          **_filters(body))
    if not records:
        return jsonify(error_body("empty", "この絞り込みでは1件もありません")), 400
    try:
        path = event_log.export_csv(records, event_log.analyses())
    except OSError as exc:
        return jsonify(error_body("write_failed", f"CSVを書けませんでした: {exc}")), 500
    return jsonify({"path": str(path),
                    "message": f"{len(records)}件をCSVに出しました: {path}"})


@bp.post("/api/log/client")
def client_error():
    """画面(ブラウザ)で起きたエラー。**番号を返す**(画面に出してもらう)。"""
    now = datetime.now()
    with _client_lock:
        if time.monotonic() - _client_window["start"] > 60:
            _client_window.update(start=time.monotonic(), count=0)
        _client_window["count"] += 1
        if _client_window["count"] > CLIENT_PER_MINUTE:
            return jsonify({"id": "", "skipped": True})
    body = request.get_json(silent=True) or {}
    fields = request_context()
    fields.update({
        "terminal": event_log.terminal_name(),
        "screen": str(body.get("screen") or fields.get("screen") or ""),
        "label": "画面のエラー",
        "message": rule.clip(body.get("message"), rule.MESSAGE_MAX),
        "where": rule.clip(body.get("where"), 200),
        "cause": rule.clip(body.get("kind") or "error", 40),
        "trace": rule.clip(body.get("stack"), rule.TRACE_MAX),
        "crumbs": [rule.clip(c, 120) for c in list(body.get("crumbs") or [])[-20:]],
        "version": str(body.get("version") or ""),
    })
    fields.pop("action", None)
    record = rule.make(rule.KIND_CLIENT, at=now, **fields)
    event_log.record(record)
    log.warning("[%s] 画面のエラー: %s (%s)", record["id"], record["message"],
                record["screen"], extra={"event_id": record["id"]})
    return jsonify({"id": record["id"]})
