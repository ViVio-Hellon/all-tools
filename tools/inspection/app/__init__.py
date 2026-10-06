"""Flask アプリの組み立て(点検表 選択・印刷)

社内「汎用Webアプリ作成 基盤仕様書」と python-web-tools(梱包資材総合ツール)
と同じ構成。点検表システム固有の機能だけをこのパッケージに置く。

    ブラウザ → routes(URL・APIの受付)→ services(業務)→ repositories(保存)

起動・停止・監視は `start_app.py` / `server.py` / `core/`(共通基盤)。
"""
from __future__ import annotations

import os
import secrets
import time
from pathlib import Path
from typing import Any, Dict, Optional

from flask import Flask, g, jsonify, request

from app.errors import ApiError, error_body
from core import app_config, event_log, idle_exit
from core.logging_utils import get_logger

log = get_logger("app")

APP_DIR = Path(__file__).resolve().parent

# 起動トークンと同一オリジン確認を要求する経路。**業務データを返す経路はここに入れる**
TOKEN_REQUIRED_PREFIXES = ("/api/",)

# `/api/*` のうち、起動トークンを要求しないもの。
#   /api/health … まだトークンを知らない相手(多重起動の判定・待機画面)が問い合わせる。
#                 返すのは識別情報と状態だけで、業務データは含めない
#   /api/alive  … 心拍。トークンを要求すると、トークンが切れた画面が黙って
#                 死んだ扱いになり、開いているのに終了してしまう
TOKEN_EXEMPT_PATHS = frozenset({"/api/health", "/api/alive"})

# 画面の番号を運ぶヘッダ(自動終了の見張りが「どの画面からか」を知るため)
SCREEN_HEADER = "X-Screen-Id"


def create_app(*, token: Optional[str] = None, port: Optional[int] = None,
               options: Optional[Dict[str, Any]] = None, business: Any = None) -> Flask:
    """アプリを1つ組み立てる。

    `token` を省略すると起動ごとに新しく作る。試験からは固定値を渡せる。
    `business` を渡すと業務の入れ物を作り直さない(試験用)。
    """
    from app.business import InspectionBusiness

    app = Flask(__name__, template_folder=str(APP_DIR / "templates"),
                static_folder=str(APP_DIR / "static"))
    app.config.update(
        TOKEN=token or secrets.token_urlsafe(32),
        # デスクトップ版はポートを使わない(0)。省略したときだけ設定の値
        PORT=app_config.port() if port is None else port,
        APP_ID=app_config.app_id(),
        VERSION=app_config.version(),
        DISPLAY_NAME=app_config.display_name(),
        STARTED_AT=time.time(),
        # 起動直後は準備中。点検表フォルダの確認が終わってから True にする
        READY=False,
        STAGE="アプリを準備中",
        STAGE_KEY="prepare",
        STARTUP_ERROR="",
        SECRET_KEY=secrets.token_hex(16),
        JSON_AS_ASCII=False,
    )
    app.json.ensure_ascii = False  # type: ignore[attr-defined]
    app.extensions["inspection"] = business or InspectionBusiness(options)
    from app import why_hints
    event_log.set_hints(why_hints.hint)       # 記録の「なぜ」に、種類の説明と確かめることを添える

    _register_security(app)
    _register_presence(app)
    _register_static_version(app)
    _register_routes(app)
    log.info("create_app: port=%s 模擬=%s", app.config["PORT"],
             bool(app.extensions["inspection"].demo))
    return app


def current_business() -> Any:
    """いまのアプリの業務の入れ物。ルートの中から呼ぶ。"""
    from flask import current_app
    return current_app.extensions["inspection"]


# ------------------------------------------------------------------
# セキュリティ(python-web-tools 設計書 §3 と同じ)
# ------------------------------------------------------------------
def op_name(path: str) -> str:
    """出来事の記録に使う操作の名前。`/api/print/cancel` → `print.cancel`。"""
    name = path[len("/api/"):] if path.startswith("/api/") else path.strip("/")
    name = name.split("/image/")[0]               # 画像のキーは名前に入れない
    return name.replace("/", ".") or "page"


# 記録しない断り(画面がすぐ次の要求を出していて、失敗ではないもの)
QUIET_CODES = frozenset({"SUPERSEDED"})


def _refuse(status: int, code: str, message: str, detail: str = ""):
    """業務の手前で断る(Host・別オリジン・トークン)。**記録してから**返す。"""
    ref = getattr(g, "ref", "")
    event_log.record(op_name(request.path), event_log.REFUSED, ref=ref, code=code, message=message,
                     detail=detail, status=status, method=request.method, path=request.path)
    return jsonify(error_body(code, message, ref=ref)), status


def _register_security(app: Flask) -> None:
    @app.before_request
    def _assign_ref():                          # noqa: ANN202 - Flaskのフック
        """要求ごとに問い合わせ番号を振る。業務の記録・断りの文・応答の見出しに同じ番号が入る。"""
        g.ref = event_log.new_ref()
        g.ref_tokens = (event_log.current_ref.set(g.ref),
                        event_log.current_screen.set(request.headers.get(SCREEN_HEADER, "")))

    @app.teardown_request
    def _clear_ref(_exc):                       # noqa: ANN202 - Flaskのフック
        tokens = getattr(g, "ref_tokens", None)
        if tokens:
            event_log.current_ref.reset(tokens[0])
            event_log.current_screen.reset(tokens[1])

    @app.before_request
    def _check_request():                       # noqa: ANN202 - Flaskのフック
        # --- Host 検証(DNSリバインディング対策) ---
        host = (request.host or "").split(":")[0]
        allowed = ("127.0.0.1", "localhost")
        if app.config.get("BRIDGE"):
            # デスクトップ版: 外枠(Tauri)の窓の宛先。TCP を通らないので
            # リバインディングの心配は無いが、知らない宛先は今までどおり断る
            allowed = allowed + app_config.BRIDGE_HOSTS
        if host not in allowed:
            log.warning("Host不一致で拒否: %s", request.host)
            return _refuse(400, "bad_host", "このアドレスからは利用できません", request.host or "")
        if not any(request.path.startswith(p) for p in TOKEN_REQUIRED_PREFIXES):
            return None
        # --- 同一オリジンの確認(Fetch Metadata) ---
        fetch_site = request.headers.get("Sec-Fetch-Site")
        if fetch_site and fetch_site not in ("same-origin", "none"):
            log.warning("別オリジンからの要求を拒否: %s %s", fetch_site, request.path)
            return _refuse(403, "cross_origin", "別のページからは利用できません",
                           f"Sec-Fetch-Site={fetch_site} Origin={request.headers.get('Origin', '')}")
        # --- 起動トークン ---
        if request.path in TOKEN_EXEMPT_PATHS:
            return None
        supplied = request.headers.get("X-Tool-Token") or request.args.get("t", "")
        # バイト列で比べる(str で非ASCIIを渡すと TypeError で 500 になる)
        if not secrets.compare_digest(supplied.encode("utf-8"), app.config["TOKEN"].encode("utf-8")):
            log.warning("トークン不一致で拒否: %s", request.path)
            return _refuse(401, "bad_token", "この画面は無効になりました。アプリを開き直してください",
                           "起動し直す前に開いていた画面か、ほかのページからの要求")
        return None

    @app.after_request
    def _headers(response):                     # noqa: ANN202 - Flaskのフック
        # CORS ヘッダは**一切返さない**(返さないことが対策)
        response.headers["X-Content-Type-Options"] = "nosniff"
        # 枠の中に出してよいのは、自分と、この PC の統合ツールの入口だけ
        # (`X-Frame-Options: SAMEORIGIN` の代わり。統合ツールの大きなタブから出すため)
        response.headers["Content-Security-Policy"] = app_config.FRAME_ANCESTORS
        response.headers["Referrer-Policy"] = "no-referrer"
        if getattr(g, "ref", ""):
            response.headers["X-Request-Ref"] = g.ref
        _apply_cache_policy(response)
        return response

    @app.errorhandler(ApiError)
    def _api_error(exc: ApiError):              # noqa: ANN202 - Flaskのフック
        ref = getattr(g, "ref", "")
        if exc.code not in QUIET_CODES:
            event_log.record(op_name(request.path), event_log.NG if exc.status >= 500 else event_log.REFUSED,
                             ref=ref, code=exc.code, message=exc.message, detail=exc.detail or "",
                             status=exc.status, method=request.method, path=request.path, **exc.context)
        return jsonify(exc.body(ref)), exc.status

    @app.errorhandler(404)
    def _not_found(_e):                         # noqa: ANN202 - Flaskのフック
        return jsonify(error_body("not_found", "ページが見つかりません", ref=getattr(g, "ref", ""))), 404

    @app.errorhandler(500)
    def _server_error(e):                       # noqa: ANN202 - Flaskのフック
        ref = getattr(g, "ref", "")
        original = getattr(e, "original_exception", None) or e
        log.exception("予期しないエラー [ref=%s]: %s %s", ref, request.method, request.path)
        event_log.record(op_name(request.path), event_log.NG, ref=ref, code="server_error",
                         message="処理中に問題が起きました", detail=_where(original),
                         status=500, method=request.method, path=request.path)
        return jsonify(error_body("server_error",
                                  "処理中に問題が起きました。ログを確認してください", ref=ref)), 500


def _where(exc: BaseException) -> str:
    """例外の種類・文・起きた場所(なぜなぜの「その奥」)。全文はテキストのログにある。"""
    import traceback
    frames = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
    place = ""
    if frames:
        last = frames[-1]
        place = f" ({os.path.basename(last.filename)}:{last.lineno} {last.name})"
    return f"{type(exc).__name__}: {exc}{place}"


STATIC_MAX_AGE = 7 * 24 * 60 * 60


def _apply_cache_policy(response) -> None:
    """版が URL に入っている静的ファイルだけ長く控える。それ以外は控えない。

    「アプリを入れ替えたのに古いまま」を起こさないため(python-web-tools と同じ)。
    """
    if request.path.startswith("/static/"):
        if request.args.get("v"):
            response.headers["Cache-Control"] = f"public, max-age={STATIC_MAX_AGE}, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"
        return
    if request.path.startswith("/api/preview/image/"):
        # 画像の名前(キー)は中身が変われば変わるので、控えてよい
        response.headers["Cache-Control"] = "private, max-age=86400"
        return
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"


def _register_presence(app: Flask) -> None:
    @app.before_request
    def _note_someone_is_here():                # noqa: ANN202 - Flaskのフック
        """**要求が来ている = 誰かが見ている。** 心拍の届き方に頼らず在席の合図にする。

        `/api/alive` だけは除く ── 「閉じました」も同じ口で受けるので、
        ここで先に取り消すと閉じたことが伝わらない。

        **画面の番号が無い `/api/health` も除く。** それは人ではなく見張り
        (2回目の起動の判定・`stop.bat --status`・外からの死活確認)で、
        数えてしまうと、画面を閉じても見張りが問い合わせるあいだ終わらない。
        画面からの問い合わせは番号を付けてくる(`api.js`)ので、そちらは数える。
        """
        if request.path == "/api/alive":
            return
        screen = request.headers.get(SCREEN_HEADER, "")
        if request.path == "/api/health" and not screen:
            return
        watch = idle_exit.get()
        if watch is not None:
            watch.beat(screen)


def _register_static_version(app: Flask) -> None:
    """CSS/JS の URL に版を付ける。入れ替えたら必ず取り直させる。"""
    stamp = app.config["VERSION"]

    @app.url_defaults
    def _stamp(endpoint, values):               # noqa: ANN202 - Flaskのフック
        if endpoint == "static" and "v" not in values:
            values["v"] = stamp


def _register_routes(app: Flask) -> None:
    from .routes import health, inspection, logs, preview, printing, settings

    app.register_blueprint(health.bp)
    app.register_blueprint(logs.bp)
    app.register_blueprint(inspection.bp)
    app.register_blueprint(preview.bp)
    app.register_blueprint(printing.bp)
    app.register_blueprint(settings.bp)
