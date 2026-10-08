"""入口の画面と API(Flask)

デスクトップ版でもブラウザ版でも同じものを使う:

    デスクトップ版  外枠(Rust)が `portal://localhost/`(Windows は `http://portal.localhost/`)で
                    受けた要求を、`bridge.py` 経由でここへ渡す(ポートは使わない)
    ブラウザ版      `start_app.py` が 127.0.0.1 で待ち受け(8700〜)、ブラウザが開く

画面は1枚(`templates/shell.html`): 上に大きなタブ、下に各ツールの画面(iframe)と
大設定。ツールの画面の宛先は、デスクトップ版は外枠の独自の宛先、ブラウザ版は
各ツールのブラウザ版(ここが起こす。`browser_tools`)。

守り(各ツールと同じ):
- Host は 127.0.0.1 / localhost(デスクトップ版は外枠の宛先も)だけ
- `/api/*` は同じ画面からだけ(Fetch Metadata)・起動トークン(`X-Tool-Token`)が要る
- CORS の見出しは返さない。この画面を枠に入れてよいのは自分だけ
"""
from __future__ import annotations

import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from flask import Flask, jsonify, render_template, request

from . import admin_password, app_config, catalog as catalog_mod, distribution, identity as identity_mod
from . import shared_db, tab_rights
from .logging_utils import get_logger, log_dir
from .rights_store import store

log = get_logger("web")

PORTAL_DIR = Path(__file__).resolve().parent

TOKEN_EXEMPT = frozenset({"/api/health", "/api/alive"})

# 止める前に訊くための鉤(サーバが差し込む)。`None` なら止められない
_shutdown_hook: Optional[Callable[[], None]] = None
# ブラウザ版の各ツール(ブラウザ版のときだけサーバが差し込む)
_browser_tools = None
# 心拍(ブラウザ版の自動終了に使う)
_last_beat = time.monotonic()
_beat_lock = threading.Lock()


def set_shutdown_hook(hook: Optional[Callable[[], None]]) -> None:
    global _shutdown_hook
    _shutdown_hook = hook


def set_browser_tools(manager) -> None:
    global _browser_tools
    _browser_tools = manager


def last_beat() -> float:
    with _beat_lock:
        return _last_beat


def _error(code: str, message: str, status: int):
    return jsonify({"ok": False, "error": {"code": code, "message": message}}), status


def create_app(*, token: Optional[str] = None, bridge: bool = False) -> Flask:
    app = Flask(__name__, template_folder=str(PORTAL_DIR / "templates"),
                static_folder=str(PORTAL_DIR / "static"), static_url_path="/static")
    app.config.update(TOKEN=token or secrets.token_urlsafe(24), BRIDGE=bridge,
                      JSON_AS_ASCII=False)
    app.json.ensure_ascii = False
    catalog = catalog_mod.load()

    # ------------------------------------------------------------------
    # 守り
    # ------------------------------------------------------------------
    @app.before_request
    def _check():                                 # noqa: ANN202 - Flask のフック
        host = (request.host or "").split(":")[0]
        allowed = ("127.0.0.1", "localhost")
        if app.config.get("BRIDGE"):
            allowed = allowed + app_config.BRIDGE_HOSTS
        if host not in allowed:
            log.warning("Host不一致で拒否: %s", request.host)
            return _error("bad_host", "このアドレスからは利用できません", 400)
        if not request.path.startswith("/api/"):
            return None
        site = request.headers.get("Sec-Fetch-Site")
        if site and site not in ("same-origin", "none"):
            log.warning("別のページからの要求を拒否: %s %s", site, request.path)
            return _error("cross_origin", "別のページからは利用できません", 403)
        if request.path in TOKEN_EXEMPT:
            return None
        supplied = request.headers.get("X-Tool-Token") or request.args.get("t", "")
        if not secrets.compare_digest(supplied.encode("utf-8"), app.config["TOKEN"].encode("utf-8")):
            return _error("bad_token", "この画面は無効になりました。統合ツールを開き直してください", 401)
        return None

    @app.after_request
    def _headers(response):                       # noqa: ANN202 - Flask のフック
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "frame-ancestors 'self'"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.path.startswith("/static/"):
            response.headers["Cache-Control"] = ("public, max-age=604800, immutable"
                                                 if request.args.get("v") else "no-cache")
        else:
            response.headers["Cache-Control"] = "no-store"
        return response

    # ------------------------------------------------------------------
    # 画面
    # ------------------------------------------------------------------
    @app.get("/")
    def shell():                                  # noqa: ANN202
        me = identity_mod.current()
        edition = "desktop" if app.config["BRIDGE"] else "browser"
        tools = [{**t.to_dict(), "origin": app_config.origin_of(t.scheme) if app.config["BRIDGE"] else ""}
                 for t in catalog.tools]
        return render_template(
            "shell.html", name=app_config.display_name(), version=app_config.version(),
            edition=edition, identity=me, shell={
                "token": app.config["TOKEN"], "edition": edition,
                "version": app_config.version(), "name": app_config.display_name(),
                "tools": tools, "statusPollMs": app_config.status_poll_ms(),
                "healthPollMs": app_config.health_poll_ms(),
            })

    # ------------------------------------------------------------------
    # 状態
    # ------------------------------------------------------------------
    @app.get("/api/health")
    def health():                                 # noqa: ANN202
        import os

        return jsonify({"app_id": app_config.app_id(), "version": app_config.version(),
                        "edition": "desktop" if app.config["BRIDGE"] else "browser",
                        "pid": os.getpid(), "ready": True})

    @app.post("/api/alive")
    def alive():                                  # noqa: ANN202
        global _last_beat
        with _beat_lock:
            _last_beat = time.monotonic()
        return jsonify({"ok": True})

    @app.post("/api/log/client")
    def client_log():                             # noqa: ANN202
        """大きなタブの画面で起きたこと(画面のエラー・各タブの画面が出た)を記録に残す。

        画面(WebView)の中のことは、ここへ送らないと後から追えない。
        """
        body = request.get_json(silent=True) or {}
        text = str(body.get("message", "")).replace("\n", " ")[:2000]
        where = str(body.get("where", ""))[:300]
        suffix = f"({where})" if where else ""
        if body.get("level") == "error":
            log.error("画面のエラー: %s%s", text, suffix)
        else:
            log.info("画面: %s%s", text, suffix)
        return jsonify({"ok": True})

    @app.get("/api/tabs")
    def tabs():                                   # noqa: ANN202
        """この端末に出すタブ。初めての起動で写しが無ければ、少しだけ読むのを待つ。"""
        store.maybe_recheck()
        store.wait_first(shared_db.REACH_TIMEOUT_SEC + 2)
        me = identity_mod.current()
        decision = store.decide(me, catalog)
        return jsonify({"ok": True, "identity": me.to_dict(),
                        "tabs": [catalog.by_id(t).to_dict() for t in decision.tabs],
                        "default_tab": decision.default_tab,
                        "decision": decision.to_dict(catalog)})

    @app.get("/api/settings")
    def settings_view():                          # noqa: ANN202
        return jsonify(_settings_view(catalog))

    # ------------------------------------------------------------------
    # 管理者の認証
    # ------------------------------------------------------------------
    @app.post("/api/settings/auth")
    def auth():                                   # noqa: ANN202
        body = request.get_json(silent=True) or {}
        if body.get("lock"):
            admin_password.session.lock()
            return jsonify({"ok": True, "admin": False})
        if not admin_password.session.unlock(str(body.get("password", ""))):
            return _error("bad_password", "パスワードが違います", 403)
        return jsonify({"ok": True, "admin": True})

    @app.post("/api/settings/password")
    def change_password():                        # noqa: ANN202
        body = request.get_json(silent=True) or {}
        result = admin_password.change(str(body.get("current", "")), str(body.get("new", "")),
                                       str(body.get("confirm", "")))
        if not result.ok:
            return _error(result.reason, result.message, 422)
        return jsonify({"ok": True, "message": result.message})

    def need_admin():
        if not admin_password.session.is_open():
            return _error("locked", "管理者パスワードで鍵を開けてください", 403)
        return None

    # ------------------------------------------------------------------
    # 共有の DB の置き場所
    # ------------------------------------------------------------------
    @app.post("/api/settings/location")
    def change_location():                        # noqa: ANN202
        refused = need_admin()
        if refused:
            return refused
        from . import user_settings

        body = request.get_json(silent=True) or {}
        folder = str(body.get("folder", "")).strip()
        name = str(body.get("name", "")).strip()
        if name and ("/" in name or "\\" in name):
            return _error("bad_value", "ファイル名にフォルダを入れないでください", 400)
        user_settings.update({user_settings.KEY_SHARED_DIR: folder or None,
                              user_settings.KEY_SHARED_NAME: name or None})
        log.info("共有の DB の置き場所を変えました: %s", shared_db.location().path)
        result = store.sync(force=True)
        return jsonify({"ok": True, "sync": result.to_dict(), **_settings_view(catalog)})

    # ------------------------------------------------------------------
    # 配布設定(置き場所・管理者パスワードを、配った先の端末で使う)
    # ------------------------------------------------------------------
    def distribution_action(action: Callable[[], Any]):
        refused = need_admin()
        if refused:
            return refused
        result = action()
        if not result.ok:
            return _error(result.reason or "failed", result.message, 422)
        store.sync_in_background()
        return jsonify({"ok": True, "message": result.message, **_settings_view(catalog)})

    @app.post("/api/distribution/export")
    def distribution_export():                    # noqa: ANN202
        return distribution_action(distribution.export)

    @app.post("/api/distribution/remove")
    def distribution_remove():                    # noqa: ANN202
        return distribution_action(distribution.remove)

    @app.post("/api/distribution/reapply")
    def distribution_reapply():                   # noqa: ANN202
        return distribution_action(distribution.reapply)

    # ------------------------------------------------------------------
    # タブ表示権限
    # ------------------------------------------------------------------
    @app.post("/api/rights/sync")
    def rights_sync():                            # noqa: ANN202
        result = store.sync(force=True)
        return jsonify({"ok": True, "sync": result.to_dict(), **_settings_view(catalog)})

    def write(action: Callable[[Path], Any], done: str):
        refused = need_admin()
        if refused:
            return refused
        path = shared_db.location().path
        if not shared_db.reachable(path):
            return _error("no_source", f"共有の DB に届きません: {path}", 422)
        try:
            action(path)
        except shared_db.SourceError as exc:
            status = {"stale_row": 409, "no_row": 409, "no_table": 409}.get(exc.code, 422)
            if status != 409:
                return _error(exc.code, str(exc), status)
            # **断ったら、手元の写しも読み直してから返す。** 写しが古いままだと、画面は
            # 読み直しても古い行(古い was)を出し、何度「保存する」を押しても 409 になっていた。
            # 読み直した表を一緒に返す(画面は打った値を残したまま、行の「いま」だけ差し替える)
            store.sync(force=True)
            body = {**_settings_view(catalog), "ok": False,
                    "error": {"code": exc.code, "message": str(exc)}}
            return jsonify(body), status
        store.sync(force=True)
        log.info("%s(%s)", done, identity_mod.current().label())
        return jsonify({"ok": True, "message": done, **_settings_view(catalog)})

    @app.post("/api/rights/create-table")
    def rights_create():                          # noqa: ANN202
        return write(lambda path: shared_db.create_table(path), "タブ表示権限の表を作りました")

    def values_from(body: dict, previous: Optional[str] = None) -> dict:
        """画面の値 → 表の値。`previous` は直す行の、読んだときの表示タブ(足すときは None)。"""
        tab_ids = [t for t in body.get("tab_ids", []) if catalog.by_id(str(t))]
        if "tab_ids" not in body:
            tabs = str(body.get("tabs", ""))
        elif previous is None:
            tabs = tab_rights.canonical_tabs(tab_ids, catalog)
        else:
            # チェックを変えていなければ表の文字のまま(知らない語・名前の並びを消さない)
            tabs = tab_rights.edited_tabs(previous, tab_ids, catalog)
        default = str(body.get("default_tab", "") or "").strip()
        default_tool = catalog.by_id(default) or catalog.find(default)
        return {"ログインID": str(body.get("login_id", "")).strip(),
                "PC名": str(body.get("pc_name", "")).strip(),
                "表示タブ": tabs,
                # この版が知らない既定タブ(新しいツール)は、書かれていたまま残す。
                # 以前は備考を直しただけで空になっていた
                "既定タブ": default_tool.title if default_tool else default,
                "有効": bool(body.get("enabled", True)),
                "備考": str(body.get("note", "")).strip()}

    def check_values(values: dict):
        if not values["ログインID"] and not values["PC名"]:
            return _error("bad_value", "ログインID か PC名 の少なくとも一方を入れてください"
                                       "(両方空の行は全員に効いてしまうので使えません)", 400)
        return None

    @app.post("/api/rights/add")
    def rights_add():                             # noqa: ANN202
        values = values_from(request.get_json(silent=True) or {})
        bad = check_values(values)
        if bad:
            return bad
        return write(lambda path: shared_db.insert(path, values), "タブ表示権限に1行足しました")

    @app.post("/api/rights/save")
    def rights_save():                            # noqa: ANN202
        body = request.get_json(silent=True) or {}
        raw_was = body.get("was") if isinstance(body.get("was"), dict) else {}
        values = values_from(body, previous=str(raw_was.get("tabs", "") or ""))
        bad = check_values(values)
        if bad:
            return bad
        try:
            key = int(body.get("key"))
        except (TypeError, ValueError):
            return _error("bad_value", "どの行かが分かりません", 400)
        was = _was(body.get("was") or {})
        return write(lambda path: shared_db.update(path, key, values, was), "タブ表示権限を直しました")

    @app.post("/api/rights/delete")
    def rights_delete():                          # noqa: ANN202
        body = request.get_json(silent=True) or {}
        try:
            key = int(body.get("key"))
        except (TypeError, ValueError):
            return _error("bad_value", "どの行かが分かりません", 400)
        was = _was(body.get("was") or {})
        return write(lambda path: shared_db.delete(path, key, was), "タブ表示権限から1行消しました")

    # ------------------------------------------------------------------
    # ブラウザ版: 各ツールのブラウザ版を起こす
    # ------------------------------------------------------------------
    @app.post("/api/tools/<tool_id>/open")
    def open_tool(tool_id: str):                  # noqa: ANN202
        if app.config["BRIDGE"] or _browser_tools is None:
            return _error("not_browser", "ブラウザ版だけの操作です", 400)
        tool = catalog.by_id(tool_id)
        if tool is None:
            return _error("no_tool", "そのツールはありません", 404)
        decision = store.decide(identity_mod.current(), catalog)
        if tool.id not in decision.tabs:
            return _error("not_allowed", "この端末では、このツールのタブは出していません", 403)
        try:
            url = _browser_tools.open(tool)
        except Exception as exc:                  # noqa: BLE001 - 理由を画面へ
            log.exception("%s のブラウザ版を起こせませんでした", tool.name)
            return _error("start_failed", str(exc), 502)
        return jsonify({"ok": True, "url": url})

    @app.get("/api/tools/status")
    def tools_status():                           # noqa: ANN202
        """ブラウザ版: 各ツールのブラウザ版が動いているか(`ids=a,b` で絞る)。"""
        if _browser_tools is None:
            return jsonify({"ok": True, "tools": []})
        raw = request.args.get("ids", "")
        ids = [i for i in raw.split(",") if catalog.by_id(i)] if raw else None
        return jsonify({"ok": True, "tools": _browser_tools.status(ids)})

    # ------------------------------------------------------------------
    # 終わる
    # ------------------------------------------------------------------
    @app.post("/api/shutdown")
    def shutdown():                               # noqa: ANN202
        """止める(デスクトップ版は外枠から、ブラウザ版は「終了」と stop.bat から)。

        `check` は訊くだけ。ブラウザ版は、先に各ツールのブラウザ版に「終わってよいか」を
        訊き、途中の処理があれば 409 で理由を返す(`force` で止める)。
        """
        body = request.get_json(silent=True) or {}
        force = bool(body.get("force"))
        if _browser_tools is not None and not force:
            busy = _browser_tools.busy()
            if busy:
                # ツールの断りがもう問いかけで終わっていれば重ねて訊かない
                # (「中断して終了しますか? それでも終了しますか?」になっていた)
                asked = all(b.rstrip().endswith(("?", "？")) for b in busy)
                message = "\n".join(busy) + ("" if asked else "\n\nそれでも終了しますか?")
                return jsonify({"stopped": False, "reason": "busy", "running": busy,
                                "message": message}), 409
        if body.get("check"):
            return jsonify({"stopped": False, "can_stop": True})
        if _shutdown_hook is None:
            return _error("no_hook", "このプロセスは停止操作に対応していません", 501)
        log.info("停止要求を受け付けました (force=%s)", force)

        def later() -> None:
            if _browser_tools is not None:
                _browser_tools.stop_all(force=force)
            _shutdown_hook()

        threading.Timer(0.4, later).start()
        return jsonify({"stopped": True, "message": "終了します"})

    return app


def _was(raw: dict) -> dict:
    """画面が読んだときの行(書く前に、同じ行のままか確かめる)。"""
    keys = {"login_id": "ログインID", "pc_name": "PC名", "tabs": "表示タブ",
            "default_tab": "既定タブ", "enabled": "有効", "note": "備考"}
    out = {}
    for key, column in keys.items():
        if key in raw:
            value = raw[key]
            out[column] = (1 if value else 0) if key == "enabled" else value
    return out


def _settings_view(catalog) -> dict:
    """大設定の画面に出すもの全部。"""
    me = identity_mod.current()
    snap = store.snapshot()
    rules = [tab_rights.Rule.from_row(r) for r in snap.rows]
    decision = store.decide(me, catalog)
    loc = shared_db.location()
    set_folder, set_name = shared_db.configured()
    fallback_folder, fallback_name = shared_db.fallback()
    tools = []
    for tool in catalog.tools:
        tools.append({**tool.to_dict(), "version": tool.version(), "dir": str(tool.dir),
                      "local": str(tool.local_root()), "logs": str(tool.local_root() / "logs")})
    return {
        "ok": True,
        "identity": me.to_dict(),
        "decision": decision.to_dict(catalog),
        "admin": admin_password.session.peek(),
        "password_custom": admin_password.is_custom(),
        # `folder` / `name` は効いている場所。欄に入れるのは**大設定で決めた値**(`set_*`)だけ
        # ── 効いている既定(環境変数・既定)を欄に入れると、「変える」を押したときに
        # 既定が大設定の値として書き込まれ、配布設定にも乗っていた
        "source": {**loc.to_dict(), "default_folder": fallback_folder,
                   "default_name": fallback_name, "set_folder": set_folder, "set_name": set_name,
                   "table": tab_rights.TABLE,
                   "state": snap.state, "imported_at": snap.imported_at,
                   "checked_at": snap.checked_at, "error": snap.last_error,
                   "cached_from": snap.source},
        "rules": [r.to_dict(catalog) for r in rules],
        "problems": tab_rights.problems(rules, catalog),
        "catalog": [t.to_dict() for t in catalog.tools],
        "tools": tools,
        "distribution": distribution.summary(),
        "app": {"name": app_config.display_name(), "version": app_config.version(),
                "root": str(app_config.APP_ROOT), "local": str(app_config.local_root()),
                "logs": str(log_dir())},
    }
