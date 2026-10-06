"""起動確認・生存監視・停止 (基盤仕様書 2.3 / 2.8 / 2.9)

- `GET  /api/health`  … 起動確認と生存監視。**トークン不要**
- `POST /api/shutdown`… 安全な停止。トークン必須

`GET /` はアプリの入口だが、準備中なら起動待機画面を出す必要があるので
ここが持つ(`entry.py` ではない)。
"""
from __future__ import annotations

import os
import threading
import time
from typing import Optional

from flask import Blueprint, current_app, jsonify, request

from nippou import app_config, awake_clock, idle_exit, running
from nippou.logging_setup import get_logger

from .. import shell

log = get_logger("app.routes.health")

bp = Blueprint("health", __name__)

# 停止要求を受けてから実際に落とすまでの猶予(秒)。応答を返しきる前に
# サーバを止めると、利用者側は「押したのに何も起きなかった」ように見える
SHUTDOWN_DELAY_SEC = 0.4

# `POST /api/shutdown` が呼ばれたときに実行する後始末。
# `server.py` が起動時に差し込む(ここが直接 waitress を知らないようにする)
_shutdown_hook = None


def set_shutdown_hook(func) -> None:
    """サーバの止め方を登録する。"""
    global _shutdown_hook
    _shutdown_hook = func


@bp.get("/api/health")
def health():
    """起動確認と生存監視。

    **`app_id` を返すのが要点**。同じポートを別のアプリが使っていても、
    HTTPが返るだけでは「自分と同じアプリが起動している」とは言えない
    (基盤仕様書 2.3)。多重起動の判定はこの値の一致で行う。

    業務データは含めないので、トークン無しで答えてよい。
    """
    config = current_app.config
    return jsonify({
        "app_id": config["APP_ID"],
        "display_name": config["DISPLAY_NAME"],
        "version": config["VERSION"],
        # 静的ファイルの印。**版は上げ忘れると同じ値のまま入れ替わる**ので、
        # どの build が動いているかはこちらでしか分からない。画面を開かずに
        # 確かめられるよう、ここにも出す
        "stamp": config.get("STATIC_STAMP", config["VERSION"]),
        # **どのフォルダの、どの版が動いているか。**
        # 「入れ替えたのに古いまま」を調べるとき、これが無いと端末に行って
        # 確かめることになる(古いプロセスが残っているのか、別のフォルダを
        # 起動しているのかが区別できない)
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
        "home_url": shell.HOME_URL,
        # いま走っているものの一言。**起動待機画面はこれを読む**
        "job": _running_note(),
        # 居るとみなしている画面の数と、最後にスリープから戻ったとき。
        # 「裏に回したら落ちた」を調べるとき、端末に行かずに確かめられる
        "screens": _screens(),
    })


def _screens() -> Optional[dict]:
    watch = idle_exit.get()
    if watch is None:
        return None
    resumed = awake_clock.get().last_resume
    return {**watch.screens(),
            "resumed": resumed.as_dict() if resumed else None}


def _running_note() -> Optional[dict]:
    """走っている処理の、待機画面に出すぶんだけ。"""
    names = running.labels()
    return {"label": "、".join(names)} if names else None


@bp.post("/api/alive")
def alive():
    """画面が生きていることの心拍(基盤仕様書 2.8「自動終了」)。

    **トークンは要らない。** 返すのは「受け取った」だけで、業務データは
    1つも含まない。心拍にトークンを要求すると、トークンが切れた画面が
    黙って死んだ扱いになり、開いたままのタブの下でプロセスが落ちる。

    本文(どれも省いてよい。省いた古い画面は1枚ぶんとして数える):

        tab      … タブの名札(`api.js` の tabId)。**タブごとに数える** ──
                   2枚のうち1枚を閉じても、アプリごとは終わらない
        page     … 読み込みごとのページ番号。閉じたページから遅れて着いた
                   合図(裏に回った・送りかけの心拍)で生き返らせない
        state    … "visible" / "hidden"。hidden は裏に回った合図で、以後は
                   **心拍が止まっても居るものとみなす**(ブラウザが見えて
                   いないタブのタイマーを間引く・止めるため)
        leaving  … タブを閉じた合図(`sendBeacon`)。最後の1枚なら猶予の
                   あとで終わるが、そのあいだに心拍が戻れば取り消される
    """
    watch = idle_exit.get()
    if watch is None:
        # 見張りを立てていない(テスト・`--check`)。**答えは返す** ──
        # 画面側に「送っても無駄」と思わせない
        return jsonify({"ok": True, "watching": False})

    body = request.get_json(silent=True) or {}
    tab = str(body.get("tab", "") or "").strip()[:80]
    page = str(body.get("page", "") or "").strip()[:80]
    if body.get("leaving"):
        watch.leaving(tab, page=page)
    else:
        watch.beat(tab, hidden=body.get("state") == "hidden", page=page)
    return jsonify({"ok": True, "watching": True})


@bp.post("/api/shutdown")
def shutdown():
    """安全な停止(基盤仕様書 2.8)。

    実行中の長時間処理があるときは、**止めずに理由を返す**。利用者に
    「中断して終了 / 完了を待つ / やめる」を選ばせるため。`force=true` を
    付けて呼び直すと中断して落とす。
    """
    body = request.get_json(silent=True) or {}
    force = bool(body.get("force"))
    running = _running_jobs()
    if running and not force:
        return jsonify({
            "stopped": False,
            "reason": "busy",
            "running": running,
            "message": "実行中の処理があります。中断して終了しますか?",
        }), 409
    # `check` は**訊くだけ**(止めない)。統合ツールの外枠が、窓の × で全ツールに
    # 「終わってよいか」を先に訊いてから、まとめて止めるために使う。1つでも
    # 処理の途中なら、ほかのツールも止めずに確認を出す(先に止めてしまわない)
    if body.get("check"):
        return jsonify({"stopped": False, "can_stop": True})

    if _shutdown_hook is None:
        # サーバ抜きで組み立てた場合(テストなど)
        return jsonify({"stopped": False, "reason": "no_hook",
                        "message": "このプロセスは停止操作に対応していません"}), 501

    log.info("停止要求を受け付けました (force=%s)", force)
    threading.Timer(SHUTDOWN_DELAY_SEC, _shutdown_hook).start()
    return jsonify({"stopped": True, "message": "終了します"})


def _running_jobs() -> list[str]:
    """いま走っている、途中で止めてはいけない処理の名前。

    **判断は `nippou/running.py` ただ1つ**。利用者が押した停止も、
    誰も見ていないことによる停止(`idle_exit`)も、同じものを見る。
    """
    return running.labels()
