"""Flask アプリの組み立て

社内「汎用Webアプリ作成 基盤仕様書」に沿った構成。起動・停止・監視の
詳細は docs/設計.md を参照。

【tkinter版との対応】
tkinter版の `ui/app.py`(メイン画面)+ 各 Toplevel ウィンドウが、ここでは
`app/routes/*` の画面になる。**業務ロジック(`nippou/logic`・`nippou/db`・
`nippou/access_bridge`・`nippou/reporting`)はそのまま引き継ぐ** ── 変わる
のは描画だけ。

【並行アクセスの前提は変えない】
ラインごとに1台のPCで1プロセス、待ち受けは 127.0.0.1 のみ
(`app_config.host()`)。あるラインの異常が他ラインを巻き込まない、という
tkinter版からの最優先要件はこの配置で保たれる。
"""
from __future__ import annotations

import hashlib
import secrets
import threading
import time
from pathlib import Path
from typing import Optional

from flask import Flask, g, jsonify, request, send_from_directory

from nippou import app_config
from nippou.db import connection as db_connection
from nippou.logging_setup import get_logger

log = get_logger("app")

APP_DIR = Path(__file__).resolve().parent

# 起動トークンと同一オリジン確認を要求する経路。
#
# **業務データを返す経路はここに入れる。** 画面のHTML(`/graph` 等)を
# 入れないのは、ブラウザのアドレス欄から開く経路だから。中身は空の器で、
# 業務データは `/api/*` から取る。
TOKEN_REQUIRED_PREFIXES = ("/api/", "/report/", "/sound/")

# `/api/*` のうち、起動トークンを要求しないもの。
#
# `/api/health` を素通しにするのは、**まだトークンを知らない相手**が
# 正当に問い合わせる場面があるため:
#   - 多重起動の判定(基盤仕様書 2.4)。後から起動したプロセスは、
#     そのポートに居るのが自分と同じアプリかどうかを知る必要がある
#   - 起動待機画面が、アプリの準備が終わったかを確かめる(同 2.3)
#
# `/api/alive`(心拍)も素通しにする。理由は2つあり、どちらも決定的:
#   - 閉じる合図は `navigator.sendBeacon` で送る。**あれはヘッダを一切
#     付けられない**ので、トークンを要求すると閉じたことが届かない
#   - トークンが切れた画面が黙って死んだ扱いになる。タブは開いたままなのに
#     その下でプロセスが落ちる、という一番たちの悪い形になる
#
# どちらも返すのは識別情報と「受け取った」だけで、業務データは一切含めない。
TOKEN_EXEMPT_PATHS = frozenset({"/api/health", "/api/alive"})


def create_app(*, token: Optional[str] = None,
               port: Optional[int] = None) -> Flask:
    """アプリを1つ組み立てる。

    `token` を省略すると起動ごとに新しく作る。テストからは固定値を渡せる。
    """
    # **静的ファイルは自前で配ります**(`static_folder=None`)。
    # URLに版を「道の一部として」入れるためです ── `_register_static_version`
    # に理由を書いてあります。
    app = Flask(__name__,
                template_folder=str(APP_DIR / "templates"),
                static_folder=None)
    app.config["STATIC_DIR"] = str(APP_DIR / "static")

    app.config.update(
        # 起動ごとの合言葉。同じPC上の別プロセスや、利用者が偶然開いた
        # 外部のWebページから叩かれないようにする
        TOKEN=token or secrets.token_urlsafe(32),
        PORT=port or app_config.port(),
        APP_ID=app_config.app_id(),
        VERSION=app_config.version(),
        DISPLAY_NAME=app_config.display_name(),
        STARTED_AT=time.time(),
        # 起動直後は準備中。重い初期化が終わってから True にする。
        # 起動待機画面はこれを見て切り替える
        READY=False,
        STAGE="アプリを準備中",
        STAGE_KEY="prepare",
        STARTUP_ERROR="",
        # セッションクッキーは使わないが、Flask の secret_key は
        # flash などが暗黙に要求することがあるので入れておく
        SECRET_KEY=secrets.token_hex(16),
    )

    # **いちばん先に。** 要求の時間を測り始め、断りもエラーも最後に見る
    # (Flask は後から付けた after_request から先に呼ぶ)。予期しないエラーに
    # 番号を付けて画面に出すのもここ(`app/event_capture.py`)
    from . import event_capture
    event_capture.register(app)
    _register_security(app)
    _register_boot_gate(app)
    # **書き込みのロックを取る前に断る。** 打てないタブの要求で
    # 順番待ちの列を伸ばしても、どのみち断るので意味がない
    _register_tab_guard(app)
    _register_db(app)
    # **DBの次に置く。** 直を引くのに時間マスタを読むので、接続を用意する
    # フックより後でなければならない
    _register_rollover(app)
    _register_static_version(app)
    _register_line_labels(app)
    _register_routes(app)

    log.info("create_app: port=%s version=%s", app.config["PORT"],
             app.config["VERSION"])
    return app


# ------------------------------------------------------------------
# セキュリティ
# ------------------------------------------------------------------
def _register_security(app: Flask) -> None:
    @app.before_request
    def _check_request():                       # noqa: ANN202 - Flaskのフック
        # --- Host 検証 (DNSリバインディング対策) ---
        # 攻撃者のドメインを 127.0.0.1 に向けられても、Hostヘッダが
        # 一致しないので弾ける
        host = (request.host or "").split(":")[0]
        allowed = ("127.0.0.1", "localhost")
        if app.config.get("BRIDGE"):
            # デスクトップ版: 外枠(Tauri)の窓の宛先。TCP を通らないので
            # リバインディングの心配は無いが、知らない宛先は今までどおり断る
            allowed = allowed + app_config.BRIDGE_HOSTS
        if host not in allowed:
            log.warning("Host不一致で拒否: %s", request.host)
            return jsonify(error_body("bad_host", "このアドレスからは利用できません")), 400

        if not any(request.path.startswith(p) for p in TOKEN_REQUIRED_PREFIXES):
            return None

        # --- 同一オリジンの確認 ---
        # ブラウザが付ける Fetch Metadata。付いていない場合(古い
        # クライアント・curl)は素通しし、トークンで守る
        fetch_site = request.headers.get("Sec-Fetch-Site")
        if fetch_site and fetch_site not in ("same-origin", "none"):
            log.warning("別オリジンからの要求を拒否: %s %s", fetch_site, request.path)
            return jsonify(error_body("cross_origin", "別のページからは利用できません")), 403

        # --- 起動トークン ---
        if request.path in TOKEN_EXEMPT_PATHS:
            return None
        supplied = (request.headers.get("X-Tool-Token")
                    or request.args.get("t", ""))
        # バイト列で比べる。`compare_digest` に str を渡すと非ASCIIで
        # TypeError になり、**500 を返してしまう**(送られた値は誰にでも
        # 決められるので、素直に 401 を返さなければならない)
        if not secrets.compare_digest(supplied.encode("utf-8"),
                                      app.config["TOKEN"].encode("utf-8")):
            log.warning("トークン不一致で拒否: %s", request.path)
            return jsonify(error_body(
                "bad_token",
                "この画面は無効になりました。アプリを開き直してください")), 401
        return None

    @app.after_request
    def _headers(response):                     # noqa: ANN202 - Flaskのフック
        # CORS ヘッダは**一切返さない**(返さないことが対策)
        response.headers["X-Content-Type-Options"] = "nosniff"
        # 枠の中に出してよいのは、自分と、この PC の統合ツールの入口だけ
        # (`X-Frame-Options: SAMEORIGIN` の代わり。統合ツールの大きなタブから出すため)
        response.headers["Content-Security-Policy"] = app_config.FRAME_ANCESTORS
        response.headers["Referrer-Policy"] = "no-referrer"
        _apply_cache_policy(response)
        return response


# 静的ファイルを控えておいてよい期間(秒)。URLに版が入っているので、
# 入れ替えれば URL が変わり、**必ず取り直される**
STATIC_MAX_AGE = 7 * 24 * 60 * 60


def _apply_cache_policy(response) -> None:
    """何を控えてよくて、何を控えてはいけないか。

    **どれも控えさせません。** 相手は同じPCの中(127.0.0.1)なので、
    毎回確かめに来ても実質ただです。`no-cache` は「使う前に必ず聞く」
    で、中身が変わっていなければ 304 が返るだけです。

    【なぜ長く控えさせるのをやめたのか ── 実際に壊れました】
    以前は「版がURLに入っているものは長く控えてよい」でした。その前提が
    **成り立っていませんでした。**

        <script src="…/js/views/settings.js?v=3.35.0-abc">   ← 版が付く
        import { api } from "../api.js";                      ← **付かない**

    版が付くのは**入口だけ**です。入口が読み込む先(`api.js` / `nav.js` /
    `tabs.js` …)は、モジュールの中に書いた相対の道でそのまま取りに行くので
    URLが変わりません。つまり:

        画面(settings.js)は新しい ＋ 道具(api.js)は1週間前の控え

    という食い違いが起きます。実際に `api.send is not a function`
    (新しい画面が、古い道具に無い関数を呼ぶ)で止まりました。その前の
    「入れ替えたら全画面のボタンが効かない」も同じ根です。

    【`no-cache` にしただけでは、届きませんでした】
    ここを `no-cache` に変えても**直りませんでした。** それ以前に配った
    控えには `immutable` が付いていて、`immutable` は「期限まで
    問い合わせるな」という意味だからです ── こちらが方針を変えても、
    **すでに配ってしまった控えは1週間効き続けます。**

    届かせるにはURLそのものを変えるしかありません。版を**道の一部**に
    入れました(`/sv/<印>/js/api.js`)。相対の import が同じ版へ解決される
    ので、一式まるごと新しい道になります(`_register_static_version`)。
    そちらは中身が変われば必ずURLが変わるので、**長く控えさせて安全**です。
    """
    if request.path.startswith("/sv/"):
        # 版が道に入っている。**中身が変わればURLが変わる**ので、
        # 長く控えてよい ── ここだけは `immutable` を付けてよい場所
        response.headers["Cache-Control"] = (
            f"public, max-age={STATIC_MAX_AGE}, immutable")
        return
    if request.path.startswith("/static/"):
        # 昔のURL。**すでに配った控えを持っている画面**がここへ来ます。
        # 使う前に必ず聞かせる(中身が同じなら 304 で終わる)
        response.headers["Cache-Control"] = "no-cache"
        return
    response.headers["Cache-Control"] = "no-store"
    # 発見的キャッシュを使う古いブラウザ向け
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"


def error_body(code: str, message: str, field: str = "") -> dict:
    """エラー応答の形。**文言はサーバが持つ。**"""
    body = {"code": code, "message": message}
    if field:
        body["field"] = field
    return {"error": body}


# ------------------------------------------------------------------
# 準備が終わるまでの門 (基盤仕様書 2.3)
# ------------------------------------------------------------------
def _register_boot_gate(app: Flask) -> None:
    """準備が終わるまで、画面の要求には起動待機画面を返す。

    基盤仕様書 2.3 は「ブラウザーだけが先に開き、接続エラーや未完成の
    画面が表示されることを防ぐ」ことを求めている。**サーバ自身が待機画面を
    出す**ことで、起動直後でも必ず何かが表示される状態を作る。

    `/api/*` と `/static/*` は通す ── 待機画面自身が `/api/health` を
    見に行くため。
    """

    @app.before_request
    def _boot_gate():                           # noqa: ANN202 - Flaskのフック
        if app.config["READY"]:
            return None
        if request.path.startswith(("/api/", "/static/")):
            return None

        # **骨格は `boot_screen` に1つだけ。** 起動サーバ(Flask が
        # 読み込まれる前に出すほう)と同じものを出す ── 2枚持つと、
        # 直したほうと直していないほうが場面によって出る
        from nippou import boot_screen

        return boot_screen.render(
            display_name=app.config["DISPLAY_NAME"],
            # 起動中も版が読める。帯がまだ出ていない場面なので、ここに
            # 出さないと「どれが入っているか」を確かめる先が無くなる
            version_label=app_config.version_label(),
            token=app.config["TOKEN"],
            app_id=app.config["APP_ID"],
            poll_ms=app_config.job_poll_ms(),
            home_url=request.path,
            log_dir=str(app_config.local_dir("logs")),
        )


# ------------------------------------------------------------------
# 直の変わり目 (VBA の `Unload UFdaily` に当たるもの)
# ------------------------------------------------------------------
# 見に行かない道。**心拍と静的ファイルは素通しする** ── 20秒ごとに
# 走る道でDBを開くと、何もしていない夜のあいだも開け閉てを続ける
ROLLOVER_SKIP = ("/static/", "/api/alive", "/api/health", "/sound/")


def _register_rollover(app: Flask) -> None:
    """直が変わっていたら、その直だけのものを落とす。

    VBA は保存処理の最後で ``Unload UFdaily`` し、フォームが持っていた
    ``chkAdminMode`` はそこで必ず消えていました。Web版の画面は
    開きっぱなしにできるので、**要求のたびに時刻で確かめます**
    (`work_context.WorkContext.roll_over`)。

    押さなくても効くのが要点です ── 1分ごとの見張り(`/api/shift/key`)が
    この道を通るので、誰も触っていなくても、変わり目から1分以内に
    管理者モードは落ちます。
    """

    @app.before_request
    def _roll_over():                           # noqa: ANN202 - Flaskのフック
        if not app.config["READY"]:
            return None
        if request.path.startswith(ROLLOVER_SKIP):
            return None
        try:
            from nippou import work_context

            from .routes.entry import current_calculator

            note = work_context.get_context().roll_over(current_calculator())
        except Exception:                       # noqa: BLE001 - 要求は通す
            # **ここで落ちても画面は出す。** 直の見分けが付かないことと、
            # 日報が打てないことは別です
            log.exception("直の変わり目を確かめられませんでした")
            return None
        if note:
            log.info("%s", note)
        return None


# ------------------------------------------------------------------
# DB接続
# ------------------------------------------------------------------
# **書く要求は1つずつ通す。**
#
# waitress はスレッドプールで動くので、要求は同時に走る。読むだけなら
# WAL があるので困らないが、書くほうが重なると次のことが起きる:
#
#   ・同じキー(報告日+ライン+直+ページ)を2か所から書くと、後から書いた
#     ほうが黙って勝つ
#   ・sqlite3 の書き込みロックに当たると `database is locked` で断られる
#
# **通し方は単純でよい** ── この道具は1台のPCを1人が使う前提なので、
# 書く要求が重なるのは押し間違いか二重送信で、待たせても誰も困らない。
_WRITE_LOCK = threading.RLock()

# 直列化しないもの。**待たせてはいけない**種類の POST。前方一致で見る
#
# **DBに触らないものだけを並べます。** 3つとも覚えているのはプロセスの
# 中(進み具合・生存・タブ)で、待たせても速くならないどころか害があります:
#
#   /api/jobs  … 進み具合。待たせると、待っている当人の様子が止まる
#   /api/alive … 生存。90秒届かないとアプリが自分で終わる(`idle_exit`)
#   /api/tab   … 心拍。15秒届かないと「閉じられた」とみなされる
#                (`logic/tab_lock.LOST_AFTER_SEC`)
#
# **`/api/tab` を入れ忘れていました。** 取り込みのような長い POST は、
# 終わるまでこの錠を持ったままです ── そのあいだ心拍が通らず、日報入力を
# 2枚開いていると、取り込みが明けた拍子に**打てる側が別のタブへ移ります**
# (先に心拍が通ったほうが引き継ぐ)。打っていた人の画面が、押してもいない
# のに「見るだけ」になります。
_NO_LOCK_PREFIXES = ("/api/jobs", "/api/alive", "/api/tab")


def _is_write(req) -> bool:
    """その要求は「書く」か。**方法だけで決める** ── 経路ごとの表を
    持つと、画面を1つ足すたびに更新が要り、忘れたぶんだけ穴が開く。
    """
    if req.method in ("GET", "HEAD", "OPTIONS"):
        return False
    return not req.path.startswith(_NO_LOCK_PREFIXES)


# ------------------------------------------------------------------
# 打てるタブは1枚 (`nippou/logic/tab_lock.py`)
# ------------------------------------------------------------------
# 日報を書き換える口。**ここは「いま打てるタブ」からしか通しません。**
#
# タブは指1本で増えます。2枚開くとどちらにも打ててしまい、保存すると
# 後から押したほうが勝つ ── 先に打った行は黙って消えます。アプリを
# 2つ起動したときと同じ壊れ方で、こちらのほうがずっと起きやすい。
#
# **見張るのは日報の入力だけです。** グラフや集計を2枚目で開くのは
# ふつうの使い方なので、そこまで止めると邪魔になるだけです。
_TAB_GUARDED_PREFIXES = ("/api/entry/", "/api/staff/", "/api/formstop/")

# 上の外にあっても、**打つ画面の行き先を変える・行を書く**口。
#
# 開いている直・ページ・呼び出しは**アプリに1つ**です(`work_context`)。
# 見るだけのタブの「記録を見る」から呼び出したりページを移ったりすると、
# 打てるタブの行き先まで黙って動き、そちらの次の自動保存が**呼び出した
# ページへ書かれます。** 打てるタブからだけ通します(v3.91.0)。
_TAB_GUARDED_PATHS = (
    "/api/settings/recall",         # 過去の直を呼び出す
    "/api/settings/page",           # ページを移る
    "/api/settings/back",           # いまの直へ戻る(直していた行を保存する)
    "/api/settings/restore-shift",  # 共有から当直を戻す(行を書き直す)
    "/api/settings/import/apply",   # 過去の日報を取り込む(行を書く)
)

# 上の中でも、**読むだけ / 計算するだけ**の口。ここを止めると、見るだけの
# タブで画面が組み立てられなくなります(値が出ない・ロットが引けない)
_TAB_READ_ONLY = (
    "/api/entry/state",     # 打った値から決まる値を返すだけ
    "/api/entry/lot",       # ロットを引くだけ
    "/api/entry/check",     # 画面の中の排他。DBは触らない
    "/api/entry/verify",    # 確かめるだけ(止めるのは共有へ保存の側)
    "/api/staff/members",
    "/api/formstop/reasons",
)


def _tab_guarded(req) -> bool:
    """その要求は「いま打てるタブ」からでなければ通さないか。"""
    if req.method in ("GET", "HEAD", "OPTIONS"):
        return False
    if req.path in _TAB_GUARDED_PATHS:
        return True
    if not req.path.startswith(_TAB_GUARDED_PREFIXES):
        return False
    return req.path not in _TAB_READ_ONLY


def _carries_rows(req) -> bool:
    """その要求は、画面の表の中身(`rows`)を送ってきたか。"""
    body = req.get_json(silent=True)
    return isinstance(body, dict) and bool(body.get("rows"))


def _register_tab_guard(app: Flask) -> None:
    """**画面を無効にするだけでは足りない。**

    無効にしたのは見た目で、要求そのものは止まっていません ── 戻る/進む、
    開きっぱなしの古いタブ、二重送信は、どれも無効化をすり抜けます。
    最後にここで見ます。
    """

    @app.before_request
    def _one_tab_may_write():                   # noqa: ANN202 - Flaskのフック
        if not _tab_guarded(request):
            return None
        from nippou import awake_clock
        from nippou.logic import tab_lock

        from .routes.tab import tab_token

        token = tab_token()
        desk = tab_lock.get_desk()
        now = awake_clock.now()
        # 時刻は心拍の口と同じもの(スリープを数えない)。**片方だけ
        # monotonic だと、起きた直後にここだけ権利を外して断る**
        if desk.may_edit(token, now=now):
            if not (_carries_rows(request) and desk.stale(token, now=now)):
                return None
            # **打てる側でも、表が古ければ書かせない。** 引き継いだ
            # タブの表は、前のタブが最後に書いた行を知りません
            # (`logic/tab_lock.py`「引き継いだタブの画面は古い」)。
            # 見るのは**表の中身を送ってくる要求だけ** ── 記録の画面からの
            # 呼び出しは表を持っていないので、古いも新しいもありません
            log.warning("古い画面からの書き込みを断りました: %s", request.path)
            return jsonify(error_body(
                "stale_tab",
                "この画面を開いたあとに、別のタブで保存されています。"
                "上書きしないよう、この画面を読み直します")), 409
        log.warning("打てないタブからの書き込みを断りました: %s", request.path)
        # 409(ぶつかった)。**400 ではない** ── 送られたものは正しく、
        # いまこのタブが打てる側でない、というだけ
        return jsonify(error_body(
            "other_tab",
            "この日報は、もう1つのタブで開いています。"
            "打てるのは1つだけです ── 打ちたいときは、この画面の"
            "「このタブで入力する」を押してください")), 409

    @app.after_request
    def _note_write(response):                  # noqa: ANN202 - Flaskのフック
        # **通った書き込みだけ**を覚える。断られたもの(4xx)は書いていない
        if _tab_guarded(request) and response.status_code < 400:
            from nippou import awake_clock
            from nippou.logic import tab_lock

            from .routes.tab import tab_token
            tab_lock.get_desk().note_write(tab_token(), now=awake_clock.now())
        return response


def _register_line_labels(app: Flask) -> None:
    """ラインを画面に出す字(v4.12.5、`logic/line_names.label`)。

    v4.13.0 から値(キー)も正規の呼び名なので、することは中板の「中板4」と、v4.12 までの
    名前(`LS`)が来たときに正規で出すことだけです。テンプレートは `{{ line | line_label }}`、
    JS は `<body data-line-labels>` の表を `static/js/line_label.js` で引きます。
    """
    import json

    from nippou.logic import line_names

    app.jinja_env.filters["line_label"] = line_names.label
    table = json.dumps(line_names.labels(), ensure_ascii=False)

    @app.context_processor
    def _line_labels():                         # noqa: ANN202 - Flaskのフック
        return {"line_labels_json": table}


def _register_db(app: Flask) -> None:
    """リクエストごとに1本開いて、終わったら閉じる。

    waitress はスレッドプールで動くため、`sqlite3` の接続をプロセス全体で
    共有できない(既定で `check_same_thread=True`)。WALは設定済みなので
    読み取りは書き込みとぶつからない。
    """

    @app.before_request
    def _take_write_lock():                     # noqa: ANN202 - Flaskのフック
        if _is_write(request):
            _WRITE_LOCK.acquire()
            g.holds_write_lock = True

    @app.teardown_request
    def _release_write_lock(_exc):              # noqa: ANN202 - Flaskのフック
        # **接続を閉じるより先に放す。** `teardown_request` は
        # `teardown_appcontext` より前に呼ばれる
        if g.pop("holds_write_lock", False):
            _WRITE_LOCK.release()

    @app.after_request
    def _backup_after_response(response):      # noqa: ANN202 - Flaskのフック
        """このリクエストで日報を書いたら、**応答を返したあとに**控えへ写す(v4.12.0)。

        控え(LocalBackup)は共有のフォルダにあることが多く、遅い・届かない
        ことがあります。写すのを応答の前にすると、そのあいだ打っている人が
        待たされるので、返し終わってから写します(`services/local_backup`)。
        """
        repo = g.get("repo")
        if repo is not None and getattr(repo, "backup_queued", False):
            from nippou.services import local_backup

            response.call_on_close(local_backup.flush_soon)
        return response

    @app.teardown_appcontext
    def _close_db(_exc):                        # noqa: ANN202 - Flaskのフック
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()


def static_stamp(static_folder: Optional[str], version: str) -> str:
    """静的ファイルの URL に付ける印。**中身が変われば必ず変わる。**

    `static/` の下を歩いて、**中身そのもの**から短い指紋を作り、
    `3.1.0-a1b2c3d4` の形にします。読めないときは版だけを返します
    (印が付かないより、粗くても付いたほうがよい)。

    更新時刻ではなく中身を読むのは、**同じものを入れ直したときに印を
    変えないため**です。配布はフォルダごとコピーなので更新時刻は毎回
    変わりますが、中身が同じなら控えをそのまま使えたほうがよい。

    歩くのは起動時の1回だけで、対象は数十ファイル・数百KBです。
    """
    if not static_folder:
        return version
    root = Path(static_folder)
    # 置き場所ごと無い/読めないときは版だけ。**指紋を取ったふりをしない**
    if not root.is_dir():
        return version
    digest = hashlib.sha256()
    try:
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            digest.update(str(path.relative_to(root)).encode("utf-8"))
            digest.update(path.read_bytes())
    except OSError:                               # 読めない場所に置かれている
        return version
    return f"{version}-{digest.hexdigest()[:8]}"


def _register_static_version(app: Flask) -> None:
    """CSS/JS の URL に版を付ける。

    **入れ替えたら必ず取り直させる。** 配布はフォルダごとコピーなので、
    ファイル名は中身が変わっても変わりません。ブラウザは同じURLの控えを
    持っているうえ、静的ファイルには `immutable` を付けて1週間控えさせて
    いるので、**URLが変わらないかぎり取りに来ません。**

    【なぜ版だけでは足りなかったか】
    以前はここに `config/app.json` の版をそのまま使っていました。
    「上げ忘れなければ必ず変わる」── その上げ忘れが起きました。
    画面の作りを直したのに版が `3.0.0` のままだったので、利用者の
    ブラウザは**1週間ぶん古い JS を出し続け**、新しいHTMLと噛み合わず
    **どの画面もボタンが全部効かない**状態になりました。

    人が覚えていることに頼る作りだったのが原因なので、**ファイルの中身
    から印を作ります。** CSS や JS を1文字でも直せば印が変わり、URLが
    変わり、必ず取り直されます。版も頭に残すので、どの版が動いているかは
    URLを見れば分かります。
    """
    static_dir = app.config["STATIC_DIR"]
    stamp = static_stamp(static_dir, app.config["VERSION"])
    app.config["STATIC_STAMP"] = stamp
    log.info("静的ファイルの印: %s", stamp)

    # ------------------------------------------------------------------
    # 版は **道の一部**。`?v=` では届きませんでした
    #
    # 【`?v=` だけでは、なぜ足りなかったか ── 2度同じところで止まりました】
    # 版が付くのは、サーバがHTMLに書いたURL(入口)だけです。その中の
    #
    #     import { api } from "../api.js";
    #
    # は**文字列のまま**で、ブラウザは `/static/js/api.js` を引きます ──
    # ここに版は付きません。入口だけ新しくなって中身が古いまま繋がり、
    # `api.send is not a function` で止まります。
    #
    # 一度これを `Cache-Control: no-cache` で直そうとしました。**直りません
    # でした。** それ以前に配った控えには `immutable` が付いており、
    # `immutable` は「期限まで問い合わせるな」という意味だからです ──
    # こちらが方針を変えても、**すでに配ってしまった控えは1週間効き続け
    # ます。** 新しい方針は次に取りに来たときにしか効きません。
    #
    # 届かせるには**URLそのものを変える**しかありません。版を道に入れると、
    #
    #     /sv/3.40.0-abcd/js/views/settings.js
    #       └ import "../api.js" → /sv/3.40.0-abcd/js/api.js
    #
    # 相対の import が**勝手に同じ版へ解決されます。** 中身を1文字でも
    # 直せば印が変わり、一式まるごと新しい道になるので、
    # 「入口は新しいのに中身が古い」が原理的に起きません。
    # ------------------------------------------------------------------
    @app.route("/sv/<stamp>/<path:filename>", endpoint="static")
    def _versioned_static(stamp, filename):     # noqa: ANN202 - Flaskのフック
        return send_from_directory(static_dir, filename)

    # 昔のURLも配り続けます。**すでに配った控えを持っている画面**が
    # `/static/js/api.js` を引きに来るので、ここで断ると真っ白になります
    @app.route("/static/<path:filename>")
    def _legacy_static(filename):               # noqa: ANN202 - Flaskのフック
        return send_from_directory(static_dir, filename)

    @app.url_defaults
    def _stamp(endpoint, values):               # noqa: ANN202 - Flaskのフック
        if endpoint != "static" or values.get("stamp"):
            return
        # **控えを跨ぐための逃げ道。**
        #
        # 起動が終わらなかったとき、画面は `?_r=<いまの時刻>` を付けて
        # 自分を取り直します(`base.html` の見張り)。その1回だけは
        # 静的ファイルの印も変えて、**ブラウザが必ず取りに来る**URLに
        # します ── 版の印が同じままだと、壊れた控えをもう一度使われて
        # 同じところで止まります。
        retry = request.args.get("_r", "") if request else ""
        values["stamp"] = f"{stamp}-r{retry}" if retry.isdigit() else stamp


def get_db():
    """このリクエスト用のDB接続。`app` の外からは呼ばない。"""
    from nippou.config import SETTINGS

    if "db" not in g:
        g.db = db_connection.connect(SETTINGS.sqlite_path)
    return g.db


def get_repo():
    """このリクエスト用のリポジトリ。"""
    from nippou.db.repository import NippouRepository

    if "repo" not in g:
        g.repo = NippouRepository(get_db())
    return g.repo


# ------------------------------------------------------------------
# ルーティング
# ------------------------------------------------------------------
def _register_routes(app: Flask) -> None:
    from .routes import (agg, entry, graph, gw, health, logs, master, pending,
                         printing, settings, sound, staff, standard_time, tab, vc)

    app.register_blueprint(health.bp)
    # 打てるタブを1枚に絞る口。**入力より先に登録する**(画面が開いた
    # 直後に叩くので、どの画面より早く用意されていてよい)
    app.register_blueprint(tab.bp)
    app.register_blueprint(entry.bp)
    # 人員(作業者選択)と全停入力。どちらも日報入力画面のモーダルから使う
    # ので、レールには出ない(`shell.NAV` を参照)
    app.register_blueprint(staff.bp)
    app.register_blueprint(gw.bp)
    app.register_blueprint(graph.bp)
    # 集計管理。VBA の「集計シート」を人が読む場所にしたもの
    app.register_blueprint(agg.bp)
    # 標準作業時間。条件×班の標準と、同じ条件の作業(作業時間・標準との差)
    app.register_blueprint(standard_time.bp)
    # VC長さ計算(vc-calculator の移植)。計算・早見表・コイル/平板
    app.register_blueprint(vc.bp)
    app.register_blueprint(printing.bp)
    app.register_blueprint(settings.bp)
    # マスタ管理(表を見る/直す)。設定画面の面から使う ── sqlite3 は
    # 現場のPCに開く道具が無い前提なので、このツールの中で完結させる
    app.register_blueprint(master.bp)
    # 音。判断は `/api/sound/due`、ファイルそのものは `/sound/<key>`
    app.register_blueprint(sound.bp)
    # ログ(エラーの一覧・なぜなぜ・画面のエラーの受け口)。設定の「ログ」の面
    app.register_blueprint(logs.bp)

    # レールに出ているのに中身がまだ無い画面へ、「準備中」の案内を置く。
    # **業務の画面を全部登録したあと**に呼ぶ(すでに実装済みのものを
    # 上書きしないよう、`shell.READY_SCREENS` を唯一の出どころにしている)
    pending.register(app)

    @app.errorhandler(404)
    def _not_found(_e):                         # noqa: ANN202 - Flaskのフック
        if request.path.startswith("/api/"):
            return jsonify(error_body("not_found", "その操作はありません")), 404
        return jsonify(error_body("not_found", "ページが見つかりません")), 404
