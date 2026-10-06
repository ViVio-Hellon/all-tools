"""記録(後追い・なぜなぜ分析)の画面の口(kanban/trace.py)

======================================== ==========
経路                                      パスワード
======================================== ==========
``POST /api/client-log``                    不要(画面で起きたエラーを知らせる)
``GET  /api/trace``                         **不要**(記録を探す・読むだけ)
``GET  /api/trace/item?ref=``               **不要**(1 件と直前の操作・なぜなぜシートの下書き)
``GET  /api/trace/csv``                     **不要**(探した記録を CSV で)
``GET  /api/log-dir``                       **不要**(いまの置き場所)
``POST /api/log-dir``                       必要(記録の置き場所を変える)
======================================== ==========

読むだけのものはパスワードを要らないことにした(マスタ・集計と同じ考え方) ──
**調べる手段を塞がない。** 中身に入力は残るが、パスワードは記録の時点で伏せてある。
"""

from __future__ import annotations

import csv
import io
import os
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from flask import Blueprint, current_app, jsonify, request

from kanban import applog, config, trace

bp = Blueprint("trace", __name__)
log = applog.get_logger("app.routes.trace")

#: 画面のエラーの知らせは 1 分に何件まで受けるか(壊れた画面が毎秒送ってきても記録を埋めない)
CLIENT_PER_MINUTE = 20
_client_lock = threading.Lock()
_client_times: list[float] = []


def _err(code: str, message: str, field: str = "") -> dict:
    body = {"code": code, "message": message}
    if field:
        body["field"] = field
    return {"error": body}


def _cfg() -> config.Config:
    return config.load_config(current_app.config.get("CONFIG_PATH") or None)


# ------------------------------------------------------------------
# 画面のエラー
# ------------------------------------------------------------------
@bp.post("/api/client-log")
def client_log():
    """画面(ブラウザ)の中で起きたエラーを記録に残す。**画面の不具合はサーバのログに出ない**ため。"""
    now = time.monotonic()
    with _client_lock:
        _client_times[:] = [t for t in _client_times if now - t < 60]
        if len(_client_times) >= CLIENT_PER_MINUTE:
            return jsonify({"ok": False, "dropped": True})
        _client_times.append(now)
    body = request.get_json(silent=True) or {}

    def text(key: str, limit: int = 2000) -> str:
        return str(body.get(key, "") or "")[:limit]

    ref = trace.write(
        "client", text("message", 1000) or "(文なし)", level=trace.LEVEL_ERROR,
        where=f"画面 {text('page', 200)}",
        exception={"chain": [{"type": text("type", 100) or "Error", "message": text("message", 1000),
                              "at": f"{text('source', 300)}:{text('line', 10)}:{text('col', 10)}"}],
                   "traceback": text("stack", 6000)},
        extra={"user_agent": request.headers.get("User-Agent", "")[:200], "action": text("action", 300)},
    )
    log.info("画面でエラー: %s (%s)", text("message", 300), ref)
    return jsonify({"ok": True, "ref": ref})


# ------------------------------------------------------------------
# 探す・読む
# ------------------------------------------------------------------
def _query(args) -> trace.Query:
    def day(key: str, default: date) -> date:
        try:
            return datetime.strptime(str(args.get(key, "")), "%Y-%m-%d").date()
        except ValueError:
            return default

    until = day("to", date.today())
    since = day("from", until - timedelta(days=6))
    if since > until:
        since, until = until, since
    kinds = frozenset(k for k in str(args.get("kinds", "")).split(",") if k in trace.KIND_LABEL)
    pc = str(args.get("pc", "")).strip()
    if pc == "@me":
        pc = str(trace._context.get("pc", ""))
    return trace.Query(since=since, until=until,
                       kinds=kinds or frozenset({"error", "warning", "refused", "client"}),
                       pc=pc, text=str(args.get("q", ""))[:200],
                       limit=max(1, min(2000, int(args.get("limit", 500) or 500))))


def _root():
    return trace.directory() or trace.root_for(_cfg().resolved_log_dir())


@bp.get("/api/trace")
def search():
    q = _query(request.args)
    root = _root()
    rows = trace.search(q, root)
    return jsonify({
        "root": str(root), "exists": root.exists(),
        "from": q.since.isoformat(), "to": q.until.isoformat(),
        "me": trace._context.get("pc", ""), "pcs": trace.pcs(root, q.since, q.until),
        "kinds": trace.KIND_LABEL, "count": len(rows), "limit": q.limit,
        "records": [_brief(r) for r in rows],
    })


def _brief(r: dict) -> dict:
    res = r.get("response") or {}
    req = r.get("request") or {}
    chain = (r.get("exception") or {}).get("chain") or []
    return {
        "ref": r.get("ref", ""), "at": r.get("at", ""), "kind": r.get("kind", ""),
        "kind_label": trace.KIND_LABEL.get(r.get("kind", ""), r.get("kind", "")),
        "level": r.get("level", ""), "pc": r.get("pc", ""), "mode": r.get("mode", ""),
        "where": r.get("where", ""), "path": req.get("path", ""),
        "what": r.get("what", ""), "shown": res.get("message", ""),
        "cause": f"{chain[-1].get('type', '')}: {chain[-1].get('message', '')}" if chain else "",
    }


@bp.get("/api/trace/item")
def item():
    ref = str(request.args.get("ref", "")).strip()
    found = trace.find(ref, _root())
    if not found:
        return jsonify(_err("not_found", f"記録番号 {ref} の記録が見つかりません"
                                         "(置き場所を変える前の記録・ほかの置き場所の記録かもしれません)")), 404
    return jsonify({
        "record": found["record"], "trail": [_brief(t) for t in found["trail"]],
        "related": [_brief(t) for t in found.get("related", [])],
        "file": found["file"], "sheet": trace.why_sheet(found),
        "kind_label": trace.KIND_LABEL.get(found["record"].get("kind", ""), ""),
    })


@bp.get("/api/trace/csv")
def search_csv():
    q = _query(request.args)
    rows = trace.to_csv_rows(trace.search(q, _root()))
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\r\n").writerows(rows)
    name = f"記録_{q.since:%Y%m%d}_{q.until:%Y%m%d}.csv"
    return current_app.response_class(
        ("﻿" + buf.getvalue()).encode("utf-8"), mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=records.csv; filename*=UTF-8''{quote(name)}"},
    )


# ------------------------------------------------------------------
# 置き場所
# ------------------------------------------------------------------
def _dir_state(cfg: config.Config) -> dict:
    path = cfg.resolved_log_dir()
    return {
        "path": path, "configured": bool(cfg.log_dir.strip()),
        "records": str(trace.root_for(path)),
        "text_log": str(applog.log_path() or ""),
        "writing_to": str(trace.directory() or ""),
        "keep_days": trace.KEEP_DAYS,
    }


@bp.get("/api/log-dir")
def log_dir_state():
    return jsonify(_dir_state(_cfg()))


@bp.post("/api/log-dir")
def set_log_dir():
    """記録(ログ)の置き場所を変える。**管理者パスワードが要る。** 空なら既定(この端末のローカル領域)。

    その場で書き先を切り替える(開き直さなくてよい)。共有フォルダを指せば、全端末の記録を
    1 か所で見られる(記録のファイルは PC ごとに分かれる)。
    """
    from .settings import _check_admin

    body = request.get_json(silent=True) or {}
    path = str(body.get("path", "")).strip()
    ok, problem = _check_admin(body)
    if not ok:
        return problem
    if path:
        folder = Path(path)
        try:
            if not folder.is_dir():
                return jsonify(_err("bad_path", f"フォルダが見つかりません: {path}", "path")), 400
            probe = folder / f".kanban_write_test_{os.getpid()}"
            probe.write_bytes(b"")
            probe.unlink()
        except OSError as exc:
            return jsonify(_err("bad_path", f"そのフォルダに書けません: {path}({exc})", "path")), 400
    cfg = _cfg()
    before = cfg.resolved_log_dir()
    cfg.log_dir = path
    try:
        config.save_config(cfg)
    except OSError as exc:
        return jsonify(_err("not_writable", f"設定を書けませんでした: {exc}")), 500
    target = cfg.resolved_log_dir()
    written = applog.reinitialize(target)
    log.info("記録の置き場所を変えました: %s → %s", before, target)
    message = f"記録の置き場所を{'既定に戻しました' if not path else '変えました'}: {target}"
    if written is None or not str(written).startswith(str(Path(target))):
        message += f"(ただし書けないので一時フォルダへ書いています: {written})"
    return jsonify({"ok": True, "message": message, **_dir_state(cfg)})
