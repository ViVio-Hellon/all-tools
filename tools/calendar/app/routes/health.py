"""起動確認・生存監視・停止 (基盤仕様書 2.3 / 2.8 / 2.9)

- ``GET  /``             … 準備中なら起動待機画面、終わっていればアプリ本体へ
- ``GET  /api/health``   … 起動確認と生存監視。**トークン不要**
- ``POST /api/alive``    … 画面が生きていることの心拍。**トークン不要**
- ``POST /api/shutdown`` … 安全な停止。トークン必須
"""

from __future__ import annotations

import os
import threading
import time

from flask import Blueprint, current_app, jsonify, redirect, request

from calendar_app import app_config, logging_utils
from calendar_app.logging_utils import get_logger

from .. import shell

log = get_logger("app.routes.health")

bp = Blueprint("health", __name__)

# 画面が「見えていない」ときに送ってくる ``state``。``frozen`` は凍らされる
# 直前(Page Lifecycle の ``freeze``)
HIDDEN_STATES = ("hidden", "frozen")

# 停止要求を受けてから実際に落とすまでの猶予(秒)。
# 応答を返しきる前にサーバを止めると、利用者側は「押したのに何も
# 起きなかった」ように見えるため、少し待ってから落とす。
SHUTDOWN_DELAY_SEC = 0.4

# ``POST /api/shutdown`` が呼ばれたときに実行する後始末。
# ``server.py`` が起動時に差し込む(ここが直接 waitress を知らないようにする)。
_shutdown_hook = None


def set_shutdown_hook(func) -> None:
    """サーバの止め方を登録する。"""
    global _shutdown_hook
    _shutdown_hook = func


@bp.get("/")
def index():
    """準備が終わるまでは起動待機画面を出す。

    基盤仕様書 2.3 は「ブラウザーだけが先に開き、接続エラーや未完成の
    画面が表示されることを防ぐ」ことを求めている。サーバ自身が待機画面を
    出すことで、**起動直後でも必ず何かが表示される**状態を作る。
    """
    if not current_app.config["READY"]:
        # **骨格は ``boot_screen`` に1つだけ。** 起動サーバ(Flask が
        # 読み込まれる前に出すほう)と同じものを出す ── 2枚持つと、
        # 直したほうと直していないほうが場面によって出る
        from calendar_app import boot_screen

        return boot_screen.render(
            display_name=current_app.config["DISPLAY_NAME"],
            # 起動中も版が読める。帯がまだ出ていない場面なので、
            # ここに出さないと「どれが入っているか」を確かめる先が無くなる
            version_label=app_config.version_label(),
            token=current_app.config["TOKEN"],
            app_id=current_app.config["APP_ID"],
            poll_ms=app_config.sync_poll_ms(),
            home_url=shell.home_url(),
            log_dir=logging_utils.current_folder(),
        )
    # 準備ができたら業務画面へ送る。
    # ここで止まる画面を出すと、``Start.vbs`` から起動した利用者が
    # **アプリに入れない**(レールが無いので行き先が分からない)
    return redirect(shell.home_url(), code=302)


@bp.get("/api/health")
def health():
    """起動確認と生存監視。

    **``app_id`` を返すのが要点**。同じポートを別のアプリが使っていても、
    HTTPが返るだけでは「自分と同じアプリが起動している」とは言えない
    (基盤仕様書 2.3)。多重起動の判定はこの値の一致で行う。

    業務データは含めないので、トークン無しで答えてよい。
    """
    config = current_app.config
    return jsonify({
        "app_id": config["APP_ID"],
        "display_name": config["DISPLAY_NAME"],
        "version": config["VERSION"],
        # **どのフォルダの、どの版が動いているか。**
        # 「入れ替えたのに古いまま」を調べるとき、これが無いと端末に
        # 行って確かめることになる(古いプロセスが残っているのか、
        # 別のフォルダを起動しているのかが区別できない)
        "app_root": str(app_config.APP_ROOT),
        "port": config["PORT"],
        "pid": os.getpid(),
        "ready": bool(config["READY"]),
        "stage": config["STAGE"],
        # どの段に居るか。**待機画面が段を組み立て直さない**ための鍵
        # (文言だけ渡すと、画面側が文字列から段を推し量ることになる)
        "stage_key": config.get("STAGE_KEY", "prepare"),
        "startup_error": config["STARTUP_ERROR"],
        "uptime_sec": round(time.time() - config["STARTED_AT"], 1),
    })


@bp.post("/api/alive")
def alive():
    """画面が生きていることの心拍(基盤仕様書 2.8)。

    **トークンは要らない。** 返すのは「受け取った」だけで、業務データは
    1つも含まない。心拍にトークンを要求すると、トークンが切れた画面が
    黙って死んだ扱いになる。

    ``leaving=true`` はタブを閉じた合図(``sendBeacon``)。猶予のあとで
    終わるが、そのあいだに心拍が戻れば取り消される。

    ``state`` は画面が見えているか(``visible`` / ``hidden``)。**裏に回る
    瞬間に ``hidden`` が届く**(``sendBeacon``)。裏の画面はブラウザに
    タイマーを間引かれ・凍らされて心拍が止まるので、そのあいだは
    自動終了もタブの空き判定もしない(``idle_exit`` / ``screen_lock``)。

    **いま使ってよい画面かどうかも一緒に返す**(``active``)。
    別の画面に取って代わられた画面は、ここで初めてそれを知る ──
    黙って古い表示を続けさせない(基盤仕様書 2.9)。
    """
    from calendar_app import idle_exit, screen_lock

    from .. import _same_origin

    # **別のサイトのページからの心拍は数えない。** 心拍はトークン無しで
    # 受ける(切れた画面を黙って死なせない)ので、同じブラウザで開いている
    # 他のサイトのタブからも ``sendBeacon`` で届きうる。数えると、誰も
    # 見ていないのに終わらない・閉じたのに取り消される、が外から起こせる。
    # 送り元の書いていない要求(古いブラウザ・道具)は今までどおり受ける
    origin = request.headers.get("Origin", "")
    if origin and origin != "null" and not _same_origin(origin, request.host):
        log.warning("別のサイトからの心拍を無視しました: %r", origin)
        return jsonify({"ok": False, "error": {
            "code": "bad_origin", "message": "別のページからの心拍は受けません。"}}), 403

    body = request.get_json(silent=True) or {}
    screen_id = str(body.get("screen_id", ""))
    leaving = bool(body.get("leaving"))
    hidden = str(body.get("state", "")) in HIDDEN_STATES

    # 画面が閉じたなら、次の画面がすぐ使えるように空ける
    if leaving and screen_id:
        screen_lock.get().release(screen_id)
        active = True
    else:
        active = screen_lock.get().beat(screen_id, hidden=hidden)

    watch = idle_exit.get()
    if watch is None:
        return jsonify({"ok": True, "watching": False, "active": active})

    if leaving:
        watch.leaving(screen_id)
    else:
        # **取って代わられた画面の「裏に回った」は数えない。** 数えると、
        # 使われていない画面のせいでいつまでも終わらない
        watch.beat(screen_id, hidden=hidden and active)
    return jsonify({"ok": True, "watching": True, "active": active})


@bp.post("/api/shutdown")
def shutdown():
    """安全な停止(基盤仕様書 2.8)。

    取り込み元への送信中は、**止めずに理由を返す**。利用者に
    「中断して終了 / 完了を待つ / やめる」を選ばせるため。
    ``force=true`` を付けて呼び直すと中断して落とす。
    """
    force = bool((request.get_json(silent=True) or {}).get("force"))
    running = _running_jobs()
    if running and not force:
        return jsonify({
            "stopped": False,
            "reason": "busy",
            "running": running,
            "message": "取り込み元と同期しています。中断して終了しますか?",
        }), 409

    if _shutdown_hook is None:
        # サーバ抜きで組み立てた場合(テストなど)
        return jsonify({"stopped": False, "reason": "no_hook",
                        "message": "このプロセスは停止操作に対応していません"}), 501

    log.info("停止要求を受け付けました (force=%s)", force)
    threading.Timer(SHUTDOWN_DELAY_SEC, _shutdown_hook).start()
    return jsonify({"stopped": True, "message": "終了します"})


def _running_jobs() -> list[str]:
    """いま走っている処理の名前。

    送信の途中で落とすと、取り込み元へ届いたかどうかが分からないまま終わる。
    止める前に必ずここを見る(基盤仕様書 2.8)。
    """
    from calendar_app import sync_service

    return sync_service.get_service().busy_labels()
