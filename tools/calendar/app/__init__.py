"""Flask アプリの組み立て

社内「汎用Webアプリ作成 基盤仕様書」に沿った構成。
起動・停止・監視の詳細は ``docs/設計.md`` の §2。

【守り】
待ち受けは ``127.0.0.1`` だけなので、LAN からは届かない。
デスクトップ版(``bridge.py``)は待ち受けもしない(標準入出力で外枠から受ける)。
守りはどちらでも同じものを通す。それでも
**同じPCで開いている別のWebページ**は要求を投げられるので、3つで守る:

1. **起動トークン** — 起動ごとに作る秘密。``X-Tool-Token`` ヘッダか
   ``?t=`` で示す。知っているのは、このプロセスが開いたブラウザだけ
2. **Host の確認** — ``127.0.0.1``/``localhost`` 以外の名前で来たものは断る
   (DNS リバインディング対策)
3. **同一オリジンの確認** — 別のページから投げられた更新要求を弾く

業務データを返す経路は必ず ``TOKEN_REQUIRED_PREFIXES`` に入れる。
画面のHTML(``/calendar`` など)を入れないのは、ブラウザのアドレス欄から
開く経路だから。中身は空の器で、業務データは ``/api/*`` から取る。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from flask import Flask, g, jsonify, request

from calendar_app import app_config, db
from calendar_app.logging_utils import get_logger

log = get_logger("app")

APP_DIR = Path(__file__).resolve().parent

#: 起動トークンと同一オリジン確認を要求する経路。
#: **業務データを返す経路はここに入れる。**
TOKEN_REQUIRED_PREFIXES = ("/api/", "/print")

#: ``/api/*`` のうち、起動トークンを要求しないもの。
#:
#: ``/api/health`` を素通しにするのは、**まだトークンを知らない相手**が
#: 正当に問い合わせる場面があるため:
#:   - 多重起動の判定(基盤仕様書 2.4)。後から起動したプロセスは、
#:     そのポートに居るのが自分と同じアプリかどうかを知る必要がある
#:   - 起動待機画面が、アプリの準備が終わったかを確かめる(同 2.3)
#: 返すのは識別情報と状態だけで、業務データは一切含めない。
#:
#: ``/api/alive``(心拍)も素通しにする。返すのは「受け取った」だけ。
#: トークンを要求すると、**トークンが切れた画面が黙って死んだ扱いになり**、
#: 開いているのに終了してしまう。
TOKEN_EXEMPT_PATHS = frozenset({"/api/health", "/api/alive"})

#: **状態を変える要求のうち、いま使ってよい画面からのものだけ通す**
#: (``calendar_app/screen_lock.py``)。同じ端末でタブを2枚開くと、
#: どちらでも登録できて、どちらも「自分が最新」の顔をする。
#: 画面側でも入口を塞ぐが、**断るのはここ** ── 画面の作りに関係なく、
#: 使ってよい画面以外からは書けないようにしておく。
#:
#: 除くもの:
#:   /api/screen/claim … これ自体が名乗りの入口
#:   /api/alive        … 心拍。外れた画面も送ってくる(そう返すために要る)
#:   /api/shutdown     … 停止。``stop.bat`` は画面を持たない
#:   /api/client-log   … 画面で起きたエラーの報告。**裏に回った画面の
#:                       エラーこそ取りこぼしたくない**
SCREEN_EXEMPT_PATHS = frozenset({"/api/screen/claim", "/api/alive",
                                 "/api/shutdown", "/api/client-log"})

#: 受け付けるホスト名。DNS リバインディング対策(上の 2 番)
ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})


def create_app(*, token: Optional[str] = None,
               port: Optional[int] = None) -> Flask:
    """アプリを1つ組み立てる。

    ``token`` を省略すると起動ごとに新しく作る。テストからは固定値を渡せる。
    """
    import secrets

    app = Flask(__name__, static_folder=str(APP_DIR / "static"),
                template_folder=str(APP_DIR / "templates"))

    app.config.update(
        APP_ID=app_config.app_id(),
        DISPLAY_NAME=app_config.display_name(),
        VERSION=app_config.version(),
        TOKEN=token or secrets.token_urlsafe(24),
        PORT=port or app_config.port(),
        # 起動の段。``server.AppServer`` が書き換える(基盤仕様書 2.2)
        READY=False,
        STAGE="アプリを準備中",
        STAGE_KEY="prepare",
        STARTUP_ERROR="",
        STARTED_AT=time.time(),
        # Jinja に余計な空行を残さない
        TEMPLATES_AUTO_RELOAD=False,
    )
    app.jinja_env.trim_blocks = True
    app.jinja_env.lstrip_blocks = True

    # **守りより先に。** 守りが断った要求も、記録には残す
    from . import tracing

    tracing.install(app)
    _install_guards(app)
    _install_db(app)
    _register_routes(app)
    return app


# ---------------------------------------------------------------------------
# 守り
# ---------------------------------------------------------------------------
def _install_guards(app: Flask) -> None:
    @app.before_request
    def _guard():                                  # noqa: ANN202
        path = request.path

        # --- 2. Host の確認 ---
        # ``Host:`` が別の名前だと、外部のDNSが 127.0.0.1 を指して
        # このアプリへ届く(DNS リバインディング)。名前で弾く
        host = (request.host or "").rsplit(":", 1)[0]
        allowed = ALLOWED_HOSTS
        if app.config.get("BRIDGE"):
            # デスクトップ版: 外枠(Tauri)の窓の宛先。TCP を通らないので
            # リバインディングの心配は無いが、知らない宛先は今までどおり断る
            allowed = allowed | frozenset(app_config.BRIDGE_HOSTS)
        if host and host not in allowed:
            log.warning("知らない Host で要求が来ました: %r", request.host)
            return jsonify({"error": {
                "code": "bad_host",
                "message": "このアドレスからは利用できません。"}}), 403

        if not path.startswith(TOKEN_REQUIRED_PREFIXES):
            return None
        if path in TOKEN_EXEMPT_PATHS:
            return None

        # --- 1. 起動トークン ---
        supplied = request.headers.get("X-Tool-Token") or request.args.get("t", "")
        if supplied != app.config["TOKEN"]:
            return jsonify({"error": {
                "code": "bad_token",
                "message": "この画面は無効になりました。アプリを開き直してください。"}}), 403

        # --- 3. 同一オリジンの確認 ---
        # 状態を変える要求だけ見る。GET は画面の読み込みでも来るため
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("Origin") or request.headers.get("Referer")
            if origin and not _same_origin(origin, request.host):
                log.warning("別オリジンからの更新要求を断りました: %r", origin)
                return jsonify({"error": {
                    "code": "bad_origin",
                    "message": "別のページからは操作できません。"}}), 403

            # --- 4. いま使ってよい画面か ---
            # **2枚目のタブからは書かせない。** 画面側でも塞ぐが、
            # 守りはここに置く(§ SCREEN_EXEMPT_PATHS)
            if path not in SCREEN_EXEMPT_PATHS:
                from calendar_app import screen_lock

                screen_id = request.headers.get("X-Screen-Id", "")
                if not screen_lock.get().is_active(screen_id):
                    log.warning("使っていない画面からの更新要求を断りました")
                    return jsonify({"error": {
                        "code": screen_lock.REFUSE_OTHER_SCREEN,
                        "message": "この画面は別の画面に切り替わっています。"
                                   "使う画面で操作してください。"}}), 409
        return None

    @app.after_request
    def _headers(response):                        # noqa: ANN202
        # 業務データを載せた応答を残さない。戻るボタンで古い内容が出るのを防ぐ
        if request.path.startswith(TOKEN_REQUIRED_PREFIXES):
            response.headers["Cache-Control"] = "no-store"
        # トークンをクエリに載せる経路があるので、外へ持ち出させない
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response


def _same_origin(origin: str, host: str) -> bool:
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return bool(parsed.netloc) and parsed.netloc == host


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------
def _install_db(app: Flask) -> None:
    """要求ごとに接続を開いて、終わりに閉じる。

    ``sqlite3`` の接続は**スレッドをまたげない**。waitress は要求ごとに
    別のスレッドで処理するので、プロセスに1つ持ち回すことはできない。
    """

    @app.teardown_appcontext
    def _close(_exc):                              # noqa: ANN202
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()


def get_db() -> sqlite3.Connection:
    """いまの要求のDB接続。**テストはここを差し替える**(``tests/_web.py``)。"""
    if "db" not in g:
        g.db = db.connect()
    return g.db


# ---------------------------------------------------------------------------
# 経路
# ---------------------------------------------------------------------------
def _register_routes(app: Flask) -> None:
    from .routes import calendar as calendar_routes
    from .routes import diagnostics as diagnostics_routes
    from .routes import health as health_routes
    from .routes import history as history_routes
    from .routes import master as master_routes
    from .routes import screen as screen_routes
    from .routes import settings as settings_routes

    app.register_blueprint(health_routes.bp)
    app.register_blueprint(calendar_routes.bp)
    app.register_blueprint(history_routes.bp)
    app.register_blueprint(settings_routes.bp)
    app.register_blueprint(master_routes.bp)
    app.register_blueprint(screen_routes.bp)
    app.register_blueprint(diagnostics_routes.bp)
