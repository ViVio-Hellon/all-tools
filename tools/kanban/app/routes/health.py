"""起動確認と停止 (基盤仕様書 2.3 / 2.8 / 2.9)

``/api/health`` は3か所から呼ばれる:

1. **起動待機画面** … 準備が終わったかを見て、本体の画面へ移る
2. **launch_guard** … そのポートに居るのが自分と同じアプリかを確かめる
3. **開いている画面** … 定期的に叩いて、落ちていないかを見る(2.9)

3つとも**トークンを持たない状態でも呼べる**必要があるので、この経路だけは
素通しにしてある(``app/__init__.py`` の ``TOKEN_EXEMPT_PATHS``)。返すのは
識別情報と状態だけで、業務データは一切含めない。
"""

from __future__ import annotations

import os
import threading
import time
from typing import Callable

from flask import Blueprint, current_app, jsonify, request

from kanban.applog import get_logger

log = get_logger("app.routes.health")

bp = Blueprint("health", __name__)

# サーバの止め方。``server.py`` が起動時に注入する。ルートが waitress を
# 直接知らないようにするための注入点
_shutdown_hook: Callable[[], None] | None = None

# 「いま止めてよいか」を答える関数。起動側が注入する。取り込み・書き戻しの
# 途中なら False を返し、その理由も添える
_busy_check: Callable[[], str] | None = None


def set_shutdown_hook(hook: Callable[[], None]) -> None:
    global _shutdown_hook
    _shutdown_hook = hook


def set_busy_check(check: Callable[[], str]) -> None:
    """処理中かどうかを答える関数を登録する。

    戻り値は「止められない理由」。空文字なら止めてよい。
    """
    global _busy_check
    _busy_check = check


def _busy_reason() -> str:
    if _busy_check is None:
        return ""
    try:
        return _busy_check() or ""
    except Exception:  # noqa: BLE001 - 判定できないなら止めてよいことにする
        log.exception("処理中かどうかの判定でエラー")
        return ""


@bp.get("/api/health")
def health():
    """このアプリの識別情報と準備状態。

    ``app_id`` を返すのが要点(基盤仕様書 2.3)。同じポートを別のアプリが
    使っている場合に、単に HTTP が返ることだけで「起動成功」と判定しない
    ようにするため。
    """
    conf = current_app.config
    return jsonify(
        {
            "app_id": conf["APP_ID"],
            "display_name": conf["DISPLAY_NAME"],
            "version": conf["VERSION"],
            "mode": conf["MODE"],
            "line": conf["LINE"],
            "port": conf["PORT"],
            "pid": os.getpid(),
            "ready": bool(conf["READY"]),
            "stage": conf["STAGE"],
            "stage_key": conf["STAGE_KEY"],
            "startup_error": conf["STARTUP_ERROR"],
            "uptime_sec": round(time.time() - conf["STARTED_AT"], 1),
            "busy": _busy_reason(),
        }
    )


@bp.post("/api/alive")
def alive():
    """画面が生きている合図(心拍)。**このアプリに窓は無い。**

    見えているのはブラウザのタブだけなので、タブを閉じたら終わったつもりに
    なります。ところが ``pythonw.exe`` は動いたまま、しかもウィンドウを持た
    ないのでタスクマネージャーの「アプリ」にも出てきません。次に起動すると
    「すでに起動しています」と言われ、**閉じたのに開けない**状態になります。

    この経路は 2 つを兼ねます:

    1. **生きている合図**(:mod:`kanban.idle_exit`)。``?leaving=1`` は
       タブを閉じたときの合図、``?hidden=1`` は裏に回ったときの合図
       (どちらも ``sendBeacon``)。裏に回った画面は、ブラウザに心拍を
       間引かれても閉じたとは見なさない
    2. **どの画面が持ち主か**(:mod:`kanban.screen`)。``?screen=`` で
       名乗り、返事の ``active`` が ``False`` なら、その画面は操作できない。
       ``?take=1`` で持ち主を自分へ移す(「こちらの画面を使う」)

    2 つを分けなかったのは、**同じ一覧から答えないと食い違う**からです。
    別々に数えると、片方が「まだ開いている」、もう片方が「誰も居ない」に
    なります。
    """
    from kanban import idle_exit, screen

    sid = request.args.get("screen", "")
    _note_desktop(request.args.get("desktop") == "1")
    watch = idle_exit.get()
    # 見張りは ``--no-browser`` だと立てない。**画面の数え上げはそれでも要る**
    # ので、一覧は見張りの有無と切り離して持てるようにしてある。見張りが
    # 居るときは**その見張りが見ている一覧**を使う ── 2 つ持つと食い違う
    screens = watch.screens if watch is not None else screen.get()

    if request.args.get("leaving") == "1":
        if watch is not None:
            watch.leaving(sid)
        else:
            screens.leave(sid)
        return jsonify({"ok": True, "watching": watch is not None})

    if request.args.get("take") == "1":
        # **持ち主が移ったことは記録に残す。** 「押したのに効かなかった」
        # と言われたとき、もう1枚に取られていたのかどうかを後から追えるように
        log.info("画面の持ち主が移りました: %s", sid or "(名乗りなし)")
        claim = screens.take_over(sid)
    else:
        # ``hidden=1`` … 裏に回っている(裏に回るときの合図と、裏で間引かれ
        # ながら届く心拍)。心拍が途切れても「閉じた」と読まない
        # ``replaces=`` … ブラウザが捨てて読み込み直したタブの、前の名札
        claim = screens.beat(
            sid,
            hidden=request.args.get("hidden") == "1",
            replaces=request.args.get("replaces", ""),
        )
    return jsonify(
        {
            "ok": True,
            "watching": watch is not None,
            # この画面は操作してよいか。**False なら画面が覆いを出す**
            "active": claim.active,
            "others": claim.others,
            "held_sec": claim.held_sec,
        }
    )


def _note_desktop(named: bool) -> None:
    """デスクトップ版の窓から最初に届いた心拍で、**外枠の機能が画面に届いているか**を 1 度だけ残す。

    帳票の窓・保存ダイアログ・モードの切り替えは、画面の ``window.__TAURI__`` を通して
    外枠(Rust)に頼む。届いていなければそれらが動かないので、ログで分かるようにする。
    """
    conf = current_app.config
    if not conf.get("BRIDGE") or conf.get("DESKTOP_NOTED"):
        return
    conf["DESKTOP_NOTED"] = True
    if named:
        log.info("デスクトップ版の窓から画面がつながりました(外枠の機能を使えます)")
    else:
        log.warning("デスクトップ版なのに、画面から外枠の機能が見えません"
                    "(帳票の窓・保存ダイアログ・モードの切り替えが動きません)")


@bp.post("/api/shutdown")
def shutdown():
    """バックエンドを止める(基盤仕様書 2.8)。

    **処理の途中なら止めない。** Access への書き戻しの最中に落とすと、
    ローカルの ``dirty`` が立ったまま Access には一部だけ入った状態になり、
    どこまで送れたのかが分からなくなる。``force`` を付けられたときだけ、
    それを承知で止める。

    画面の「終了」ボタンと ``stop.bat`` の両方からここへ来る。
    """
    body = request.get_json(silent=True) or {}
    force = bool(body.get("force"))

    reason = _busy_reason()
    if reason and not force:
        log.info("処理中のため停止しません: %s", reason)
        return (
            jsonify(
                {
                    "stopped": False,
                    "busy": reason,
                    "error": {"code": "busy", "message": reason},
                }
            ),
            409,
        )

    if _shutdown_hook is None:
        log.warning("停止の手段が登録されていません")
        return (
            jsonify(
                {
                    "stopped": False,
                    "error": {"code": "no_hook", "message": "停止できませんでした"},
                }
            ),
            500,
        )

    log.info("停止要求を受け付けました (force=%s)", force)

    # **応答を返してから閉じる。** 先に閉じると、この要求自身の応答が
    # 返らず、押した側には「失敗した」ように見える
    def _later() -> None:
        time.sleep(0.3)
        try:
            _shutdown_hook()
        except Exception:  # noqa: BLE001 - 停止は best effort
            log.exception("停止の実行でエラー")

    threading.Thread(target=_later, name="shutdown", daemon=True).start()
    return jsonify({"stopped": True, "forced": force})
