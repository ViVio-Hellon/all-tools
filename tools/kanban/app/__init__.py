"""Flask アプリの組み立て

社内「汎用Webアプリ作成 基盤仕様書」に沿った構成。起動・停止・監視の詳細は
``docs/移行仕様.md``。

【モードと操作範囲】
モードは **現場 / 倉庫 / 倉庫参照** の3つ(:mod:`kanban.config`)。tkinter 版は
モードごとに別のウィンドウを開いていた。Web 版もモードごとに**別ポート・
別ロック**で起動する ── 同じ考え方をそのまま持ち込んでいる。
ただし **1 台の PC で動くのは 1 つだけ**(:mod:`launch_guard`)。別のモードが
動いていれば、それを終わらせてから立て直す。

操作できる範囲はモードで決まる(:mod:`kanban.presenters.board` の
``BoardPolicy``)。守りは2段構え:

1. **そのモードに無い操作は登録しない。** 倉庫参照モードのプロセスには、
   状態を変える API が **存在しない**(404)。隠すのではなく、無い
2. **登録されていても、要求ごとにモードを確かめる。** 現場モードで
   注文中を動かそうとすれば 403

1 だけで足りそうに見えるが、モードは設定画面から切り替えられる
(:func:`set_mode`)。切り替えた直後は登録済みのエンドポイントが残るので、
2 が要る。
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from pathlib import Path

from flask import Flask, current_app, g, jsonify, request

from kanban import app_config, config
from kanban.applog import get_logger
from kanban.db.store import Store
from kanban.domain.service import KanbanService

log = get_logger("app")

APP_DIR = Path(__file__).resolve().parent

# 起動トークンと同一オリジン確認を要求する経路。
#
# **業務データを返す経路はここに入れる。** ``/api/*`` だけでなく
# ``/report/*``(印刷用 HTML)も入れる ── 帳票には資材名・サイズ・数量が
# 丸ごと入るため、素通しにすると画面より緩い口ができてしまう。
#
# 画面の HTML(``/site`` 等)を入れないのは、ブラウザのアドレス欄から開く
# 経路だから。中身は空の器で、業務データは ``/api/*`` から取る。
TOKEN_REQUIRED_PREFIXES = ("/api/", "/report/")

# ``/api/*`` のうち、起動トークンを要求しないもの。
#
# ``/api/health`` を素通しにするのは、**まだトークンを知らない相手**が正当に
# 問い合わせる場面があるため:
#   - 多重起動の判定(基盤仕様書 2.4)。後から起動したプロセスは、そのポートに
#     居るのが自分と同じアプリかどうかを知る必要がある
#   - 起動待機画面が、アプリの準備が終わったかを確かめる(同 2.3)
# 返すのは識別情報と状態だけで、業務データは一切含めない。
#
# ``/api/alive``(心拍)も素通しにする。返すのは「受け取った」だけで業務
# データを含まない。**トークンを要求すると、トークンが切れた画面が黙って
# 死んだ扱いになり、開いているのに終了してしまう**(:mod:`kanban.idle_exit`)。
TOKEN_EXEMPT_PATHS = frozenset({"/api/health", "/api/alive"})


def create_app(
    mode: str = config.MODE_SITE,
    *,
    token: str | None = None,
    port: int | None = None,
    store: Store | None = None,
    line: str = "",
    on_manual_refresh=None,
    on_reconnect=None,
    on_changed=None,
) -> Flask:
    """アプリを1つ組み立てる。

    ``store`` を渡すと、その接続をそのまま使う(起動側が既に開いている
    ものを引き継ぐ)。省略するとテスト用にメモリ上へ作る。

    ``line`` は現場モードの担当ライン。倉庫・倉庫参照では使わない。
    """
    if mode not in config.ALL_MODES:
        raise ValueError(f"未知のモード: {mode!r} (使えるのは {', '.join(config.ALL_MODES)})")

    app = Flask(
        __name__,
        template_folder=str(APP_DIR / "templates"),
        static_folder=str(APP_DIR / "static"),
    )

    app.config.update(
        MODE=mode,
        LINE=line,
        # 起動ごとの合言葉。同じ PC 上の別プロセスや、利用者が偶然開いた
        # 外部の Web ページから叩かれないようにする
        TOKEN=token or secrets.token_urlsafe(32),
        # 0 はデスクトップ版(待ち受けない。bridge.py)
        PORT=app_config.port(mode) if port is None else port,
        APP_ID=app_config.app_id(),
        VERSION=app_config.version(),
        DISPLAY_NAME=app_config.display_name(),
        STARTED_AT=time.time(),
        # 起動直後は準備中。重い初期化(スキーマ適用・共有DBからの取り込み)が
        # 終わってから True にする。起動待機画面はこれを見て切り替える
        READY=False,
        STAGE="アプリを準備中",
        STAGE_KEY="prepare",
        STARTUP_ERROR="",
        STORE=store,
        # 「共有DBから今すぐ取り込む」を呼ぶための関数。起動側が注入する
        MANUAL_REFRESH=on_manual_refresh,
        # 接続先を差し替えて取り込み直す関数。**開き直しを要らなくする**
        # ためのもので、これが無いと接続先を変えても反映されない
        RECONNECT=on_reconnect,
        # ボタンで状態が変わったときに呼ぶ関数(**押した直後に書き戻す**)。
        # 無いと次の書き戻しの周期(既定 60 秒)まで共有DBへ届かず、他の端末は
        # その間ずっと古い状態を見る
        ON_CHANGED=on_changed,
        # 定期取り込みの間隔。**「取り込めていない」の判定に要る** ──
        # これが分からないと、どれだけ古ければ異常なのかを決められない。
        # 0 なら定期取り込みをしない端末なので、古さを言っても直しようがない
        IMPORT_INTERVAL_SEC=0,
        # Flask の flash などが暗黙に要求することがあるので入れておく
        SECRET_KEY=secrets.token_hex(16),
    )

    _register_trace(app)       # いちばん先に: 断られた要求にも記録番号を振る
    _register_security(app)
    _register_screen_guard(app)
    _register_db(app)
    _register_static_version(app)
    _register_routes(app)

    log.info("create_app: mode=%s port=%s line=%s", mode,
             app.config["PORT"] or "なし(デスクトップ版)", line or "-")
    return app


# ------------------------------------------------------------------
# モード
# ------------------------------------------------------------------
def current_mode() -> str:
    from flask import current_app

    return current_app.config["MODE"]


def current_line() -> str:
    """現場モードの担当ライン。"""
    from flask import current_app

    return current_app.config["LINE"]


def require_mode(*allowed: str):
    """そのモードでなければ 403 を返す ``before_request`` を作る。

    エンドポイントの登録可否(起動時に決まる)とは別の話。モードは設定画面
    から切り替えられるので、**要求のたびに**確かめる必要がある。
    """

    def _check():
        if current_mode() not in allowed:
            labels = " / ".join(config.mode_display_name(m) for m in allowed)
            log.info("モード違いで断りました: %s (いま %s)", request.path, current_mode())
            return (
                jsonify(
                    _error(
                        "wrong_mode",
                        f"この操作は{labels}でのみ行えます。設定からモードを切り替えてください。",
                    )
                ),
                403,
            )
        return None

    return _check


# ------------------------------------------------------------------
# セキュリティ
# ------------------------------------------------------------------
def _register_security(app: Flask) -> None:
    @app.before_request
    def _check_request():  # noqa: ANN202 - Flask のフック
        # --- Host 検証 (DNS リバインディング対策) ---
        # 攻撃者のドメインを 127.0.0.1 に向けられても、Host ヘッダが
        # 一致しないので弾ける
        host = (request.host or "").split(":")[0]
        allowed = ("127.0.0.1", "localhost")
        if app.config.get("BRIDGE"):
            # デスクトップ版: 外枠(Tauri)の窓の宛先。TCP を通らないので
            # リバインディングの心配は無いが、知らない宛先は今までどおり断る
            allowed = allowed + app_config.BRIDGE_HOSTS
        if host not in allowed:
            log.warning("Host不一致で拒否: %s", request.host)
            return jsonify(_error("bad_host", "このアドレスからは利用できません")), 400

        if not any(request.path.startswith(p) for p in TOKEN_REQUIRED_PREFIXES):
            return None

        # --- 同一オリジンの確認 ---
        # ブラウザが付ける Fetch Metadata。付いていない場合(古いクライアント・
        # curl)は素通しし、トークンで守る
        fetch_site = request.headers.get("Sec-Fetch-Site")
        if fetch_site and fetch_site not in ("same-origin", "none"):
            log.warning("別オリジンからの要求を拒否: %s %s", fetch_site, request.path)
            return jsonify(_error("cross_origin", "別のページからは利用できません")), 403

        # --- 起動トークン ---
        if request.path in TOKEN_EXEMPT_PATHS:
            return None
        supplied = request.headers.get("X-Tool-Token") or request.args.get("t", "")
        # バイト列で比べる。``compare_digest`` に str を渡すと非 ASCII で
        # TypeError になり、**500 を返してしまう**(送られた値は誰にでも
        # 決められるので、素直に 401 を返さなければならない)
        if not secrets.compare_digest(
            supplied.encode("utf-8"), app.config["TOKEN"].encode("utf-8")
        ):
            log.warning("トークン不一致で拒否: %s", request.path)
            return (
                jsonify(
                    _error("bad_token", "この画面は無効になりました。アプリを開き直してください")
                ),
                401,
            )
        return None

    @app.after_request
    def _headers(response):  # noqa: ANN202 - Flask のフック
        # CORS ヘッダは**一切返さない**(返さないことが対策)
        response.headers["X-Content-Type-Options"] = "nosniff"
        # 枠の中に出してよいのは、自分と、この PC の日報複合ツールの入口だけ
        # (`X-Frame-Options: SAMEORIGIN` の代わり。日報複合ツールの大きなタブから出すため)
        response.headers["Content-Security-Policy"] = app_config.FRAME_ANCESTORS
        response.headers["Referrer-Policy"] = "no-referrer"
        _apply_cache_policy(response)
        return response


#: 静的ファイルを控えておいてよい期間(秒)。URL に中身の指紋が入っているので、
#: 入れ替えれば URL が変わり、**必ず取り直される**
STATIC_MAX_AGE = 7 * 24 * 60 * 60


def _apply_cache_policy(response) -> None:
    """何を控えてよくて、何を控えてはいけないか。

    分け方は1行で言える:

    * **版が URL に入っているもの(静的ファイル)は、長く控えてよい。**
      入れ替えれば URL が変わるので、古いものが出ることはない
    * **それ以外は控えない。** 画面の HTML も、API の応答も、いま作った
      ものを毎回渡す

    指示の無い HTML はブラウザが自分の判断で控える(発見的キャッシュ・
    戻る操作)。看板は**いまの状態を見るための画面**なので、古い盤面が
    出るのは事故そのものになる。

    【長く控えてよいのは、いまの指紋が付いた URL だけ】
    以前は ``/static/`` を**すべて** 7 日・``immutable`` で渡していました。
    ところが付けていた印は ``config/app.json`` の版で、**中身を直しても版を
    上げなければ URL が変わらない** ── 実際、アプリを入れ替えたのに設定画面の
    JS だけ古いまま残り、新しいサーバに古い画面で送って断られました
    (看板の状態の欄を読むだけにした直後、「不」が空で送られた)。
    さらに JS の中の ``import './api.js'`` には印そのものが付かないので、
    入口だけ新しくて中身が古い、も起きえます。

    * 印は**中身の指紋**にする(:func:`_static_stamp`)。1 文字でも変われば変わる
    * JS どうしの読み込みにも同じ印を付ける(``base.html`` の import map)
    * **いまの指紋が付いていないもの**は ``no-cache``(使う前に必ず確かめる)。
      ETag が付くので、中身が同じなら 304 が返るだけ
    """
    if request.path.startswith("/static/"):
        if request.args.get("v") == current_app.config.get("STATIC_STAMP"):
            response.headers["Cache-Control"] = f"public, max-age={STATIC_MAX_AGE}, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"
        return
    response.headers["Cache-Control"] = "no-store"
    # 発見的キャッシュを使う古いブラウザ向け。``no-store`` を読まない実装でも、
    # この2つがあれば控えない
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"


# ------------------------------------------------------------------
# 記録(kanban/trace.py) ── 後追い・なぜなぜ分析のため
# ------------------------------------------------------------------
#: 記録しない経路(数秒ごとに来る・記録そのものの口)
_TRACE_SKIP = ("/api/alive", "/api/health", "/api/client-log", "/api/trace")

#: 断ったうちで「ふつうの流れ」のもの(パスワードを訊く)。操作の足跡として残し、
#: 断った操作の一覧には出さない
_TRACE_NORMAL_CODES = frozenset({"locked", "need_password_setup"})


def _register_trace(app: Flask) -> None:
    import json as _json
    import time

    from kanban import trace

    @app.before_request
    def _trace_start():  # noqa: ANN202 - Flask のフック
        g.trace_ref = trace.new_ref("R")
        g.trace_t0 = time.monotonic()

    @app.after_request
    def _trace_finish(response):  # noqa: ANN202 - Flask のフック
        """断った・通した操作を記録に残し、**エラーの応答に記録番号を付ける。**

        画面はエラーの文に記録番号を添えて出す。番号を言ってもらえば、記録の 1 件
        (入力・原因の連鎖・直前の操作)にたどり着ける。
        """
        path = request.path
        if not path.startswith("/api/") or path.startswith(_TRACE_SKIP):
            return response
        ref = g.get("trace_ref", "")
        status = response.status_code
        ms = int((time.monotonic() - g.get("trace_t0", time.monotonic())) * 1000)
        try:
            if status >= 400:
                response.headers["X-Record-Ref"] = ref
                info: dict = {}
                if response.is_json:
                    data = response.get_json(silent=True)
                    err = data.get("error") if isinstance(data, dict) else None
                    if isinstance(err, dict):
                        info = {"code": err.get("code", ""), "message": err.get("message", "")}
                        err.setdefault("ref", ref)
                        response.set_data(_json.dumps(data, ensure_ascii=False))
                if status >= 500:
                    return response   # エラーとして記録済み(_unexpected の log.exception)
                normal = info.get("code") in _TRACE_NORMAL_CODES
                trace.write(
                    "operation" if normal else "refused",
                    info.get("message") or f"{status}",
                    level=trace.LEVEL_INFO if normal else trace.LEVEL_WARNING,
                    ref=ref, where=f"{request.method} {path}", request=trace.current_request(),
                    response={"status": status, **info}, extra={"ms": ms},
                )
            elif request.method == "POST":
                trace.write("operation", f"{request.method} {path}", ref=ref,
                            where=f"{request.method} {path}", request=trace.current_request(),
                            response={"status": status}, extra={"ms": ms})
        except Exception:  # noqa: BLE001 - 記録のために応答を壊さない
            log.debug("記録に書けませんでした", exc_info=True)
        return response


def _error(code: str, message: str, field: str = "") -> dict:
    """エラー応答の形。文言はサーバが持つ。"""
    body = {"code": code, "message": message}
    if field:
        body["field"] = field
    return {"error": body}


# ------------------------------------------------------------------
# 画面の持ち主 (基盤仕様書 2.4 を、プロセスではなく画面に当てはめたもの)
# ------------------------------------------------------------------
# **プロセスが1つでも、タブは何枚でも繋がる。** :mod:`launch_guard` が防ぐ
# のは ``pythonw.exe`` の二重起動で、そこは守れている。だがこのアプリに窓は
# 無く、見えているのはブラウザのタブなので、**同じプロセスに 2 枚繋いだ時点で
# 両方から押せてしまう** ── 同じ看板を 2 画面で取り合う形になる。
#
# 画面は :mod:`kanban.screen` で名札を持ち、持ち主は 1 枚だけ。ここでは
# **持ち主でない画面からの「変える操作」を断る。** 画面側の覆いだけに任せると、
# 覆いが出るまでの数秒や、覆いを閉じられた場合に素通しになる。
#
# 読むのは断らない。古い盤を見ていること自体は害が無く、むしろ「もう 1 枚で
# 何が起きたか」が見えたほうがよい。
_SCREEN_EXEMPT_PATHS = frozenset(
    {
        "/api/health",
        "/api/alive",
        # **終了はどの画面からでも通す。** 使えない画面に閉じる手段まで
        # 無いと、出口が見つからない(``stop.bat`` もここを叩く)
        "/api/shutdown",
        # 画面のエラーの知らせ。持ち主でない画面で起きたエラーも記録に残す
        "/api/client-log",
    }
)


def _is_screen_guarded(req) -> bool:
    """その要求を「持ち主かどうか」で断ってよいか。"""
    if req.method in ("GET", "HEAD", "OPTIONS"):
        return False
    return req.path.startswith("/api/") and req.path not in _SCREEN_EXEMPT_PATHS


def _register_screen_guard(app: Flask) -> None:
    @app.before_request
    def _check_screen():  # noqa: ANN202 - Flask のフック
        if not _is_screen_guarded(request):
            return None
        sid = request.headers.get("X-Screen", "")
        if not sid:
            # **名乗らない相手は断らない。** ``stop.bat`` や試験、手元の
            # ``curl`` を巻き添えにしない。ここは事故を防ぐための仕切りで
            # あって、守りはトークンと Host 検証が受け持っている
            return None
        from kanban import screen

        if screen.get().is_holder(sid):
            return None
        log.info("持ち主でない画面からの操作を断りました: %s", request.path)
        return (
            jsonify(
                _error(
                    "not_active",
                    "この画面では操作できません。このアプリは別のタブで開いています。",
                )
            ),
            423,
        )


# ------------------------------------------------------------------
# ストア(ローカル SQLite)
# ------------------------------------------------------------------
# **書く要求は1つずつ通す。**
#
# waitress はスレッドプールで動くので、要求は同時に走る。読むだけなら WAL が
# あるので困らないが、書くほうが重なると次のことが起きる:
#
#   ・同じ行を2か所から書くと、``rev`` の楽観ロックが空振りして片方が
#     「他の端末が更新しました」になる(実際には同じ端末の二重送信)
#   ・sqlite3 の書き込みロックに当たると ``database is locked`` で断られる
#
# **通し方は単純でよい** ── この道具は1台の PC を1人が使う前提なので、書く
# 要求が重なるのは押し間違いか二重送信で、待たせても誰も困らない。
_WRITE_LOCK = threading.RLock()

# 直列化しないもの。**待たせてはいけない**種類の POST。前方一致で見る
# ``/api/alive`` は 20 秒ごとに来る。書き込みロックの後ろで待たせると、
# 心拍が遅れて**開いているのに自動終了する**
_NO_LOCK_PREFIXES = ("/api/health", "/api/alive", "/api/shutdown", "/api/mode", "/api/client-log")


def _is_write(req) -> bool:
    """その要求は「書く」か。**方法だけで決める** ── 経路ごとの表を持つと、
    画面を1つ足すたびに更新が要り、忘れたぶんだけ穴が開く。
    """
    if req.method in ("GET", "HEAD", "OPTIONS"):
        return False
    return not req.path.startswith(_NO_LOCK_PREFIXES)


def _register_db(app: Flask) -> None:
    @app.before_request
    def _take_write_lock():  # noqa: ANN202 - Flask のフック
        if _is_write(request):
            _WRITE_LOCK.acquire()
            g.holds_write_lock = True

    @app.teardown_request
    def _release_write_lock(_exc):  # noqa: ANN202 - Flask のフック
        if g.pop("holds_write_lock", False):
            _WRITE_LOCK.release()


def get_store() -> Store:
    """このプロセスのローカル SQLite。

    tkinter 版と同じく **プロセスに1つ**。``Store`` は内部でスレッドごとの
    接続を持つので、waitress のスレッドプールから呼んでも安全
    (``check_same_thread`` に当たらない)。
    """
    from flask import current_app

    store = current_app.config["STORE"]
    if store is None:
        raise RuntimeError("ストアが設定されていません(create_app に store を渡してください)")
    return store


def get_service() -> KanbanService:
    """このリクエスト用のサービス。"""
    if "service" not in g:
        g.service = KanbanService(get_store(), on_changed=current_app.config.get("ON_CHANGED"))
    return g.service


def _static_stamp(folder: Path) -> str:
    """静的ファイル一式の**中身の指紋**。1 ファイルでも 1 文字でも変われば変わる。

    版(``config/app.json``)を印にしていたころは、版を上げずに入れ替えると
    URL が変わらず、ブラウザが 7 日間古い JS を使い続けました。
    """
    digest = hashlib.sha1()
    for path in sorted(folder.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(folder).as_posix().encode("utf-8"))
            try:
                digest.update(path.read_bytes())
            except OSError:
                digest.update(b"?")
    return digest.hexdigest()[:12]


def _register_static_version(app: Flask) -> None:
    """CSS/JS の URL に中身の指紋を付ける。

    **入れ替えたら必ず取り直させる。** 配布はフォルダごとコピーなので、
    ファイル名は版が上がっても変わらない。ブラウザは同じ URL の控えを持って
    いるので、``Cache-Control`` を無視する場面(戻る操作・オフライン復帰・
    企業のプロキシ)では**古い JS・CSS が出続ける**。

    テンプレートが名指しするファイル(``url_for('static', ...)``)には
    ``?v=<指紋>`` が付きます。JS の中の ``import './api.js'`` には付かない
    ので、``base.html`` の **import map** で同じ印付きの URL へ振り替えます
    (:func:`_import_map`)。
    """
    folder = Path(app.static_folder)
    stamp = _static_stamp(folder)
    app.config["STATIC_STAMP"] = stamp
    import_map = _import_map(folder, app.static_url_path, stamp)

    @app.url_defaults
    def _stamp(endpoint, values):  # noqa: ANN202 - Flask のフック
        if endpoint == "static" and "v" not in values:
            values["v"] = stamp

    @app.context_processor
    def _inject_import_map():  # noqa: ANN202 - Flask のフック
        return {"import_map": import_map}


def _import_map(folder: Path, url_path: str, stamp: str) -> dict:
    """JS どうしの読み込みを、印付きの URL へ振り替える表。

    ``views/board.js`` の ``import { api } from '../api.js'`` は
    ``/static/js/api.js`` を取りに行く(印が付かない)。import map はこれを
    ``/static/js/api.js?v=<指紋>`` に読み替えさせる。**同じ URL に揃うので、
    モジュールが 2 回読み込まれることもない**(テンプレートが名指しする
    ``url_for`` の URL と同じ形)。
    """
    imports = {}
    for path in sorted((folder / "js").rglob("*.js")):
        rel = path.relative_to(folder).as_posix()
        imports[f"{url_path}/{rel}"] = f"{url_path}/{rel}?v={stamp}"
    return {"imports": imports}


# ------------------------------------------------------------------
# ルーティング
# ------------------------------------------------------------------
def _register_routes(app: Flask) -> None:
    from .routes import board, health, report, settings
    from .routes import trace as trace_routes

    app.register_blueprint(health.bp)
    app.register_blueprint(trace_routes.bp)
    # 設定画面は**どのモードでも**開ける。取り込みと書き戻し、そして
    # 「いまどのモードで動いているか」を確かめる場所になる
    app.register_blueprint(settings.bp)
    # 看板そのもの。読むだけの経路はどのモードでも登録する
    app.register_blueprint(board.bp)
    app.register_blueprint(report.bp)

    # 状態を変える経路も**常に登録する**。モードは設定画面から切り替えられる
    # ので、起動時のモードで登録を決めると、切り替えた直後に「あるはずの操作が
    # 404」になる。守りは要求ごとのモード確認で行う(各エンドポイントの先頭と
    # ``presenters.board.policy_for``)。倉庫参照モードでは ``BoardPolicy`` が
    # すべて False を返すので、どのボタンも通らない。
    app.register_blueprint(board.write_bp)

    @app.errorhandler(404)
    def _not_found(_e):  # noqa: ANN202 - Flask のフック
        if request.path.startswith("/api/"):
            return jsonify(_error("not_found", "その操作はこのモードでは使えません")), 404
        return jsonify(_error("not_found", "ページが見つかりません")), 404

    @app.errorhandler(Exception)
    def _unexpected(exc):  # noqa: ANN202 - Flask のフック
        """想定していない例外。**理由を画面へ返し、経緯を記録に残す。**

        これが無いと、画面には「通信に失敗しました (500)」としか出ず、しかも
        例外の経緯は waitress 側に流れて**このアプリの記録(DebugLog)に
        残りません**。マスタに行を足すと 500 になる不具合(共有フォルダへの
        書き込みが URI の形で断られていた)は、まさにこれで原因が見えなかった。
        """
        from werkzeug.exceptions import HTTPException

        if isinstance(exc, HTTPException):
            return exc  # 404 や 405 は Flask の扱いのまま
        log.exception("想定外のエラー: %s %s", request.method, request.path)
        ref = g.get("trace_ref", "")
        if request.path.startswith("/api/"):
            return (
                jsonify(_error(
                    "internal",
                    f"想定外のエラーで処理できませんでした: {type(exc).__name__}: {exc}"
                    f"(設定 →「記録」で 記録番号 {ref} を開くと、原因の連鎖と直前の操作が見られます)",
                )),
                500,
            )
        return (f"想定外のエラーが起きました(記録番号 {ref})。"
                "設定 →「記録」で、その番号の記録を確かめてください。"), 500
