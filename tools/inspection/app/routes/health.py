"""起動確認・生存監視・停止 (基盤仕様書 2.3 / 2.8 / 2.9)

- `GET  /`             … 準備中なら起動待機画面、終わっていれば点検表の画面
- `GET  /api/health`   … 起動確認と生存監視。**トークン不要**
- `POST /api/alive`    … 画面の心拍(自動終了の見張り)。**トークン不要**
- `POST /api/shutdown` … 安全な停止。トークン必須
"""
from __future__ import annotations

import os
import secrets
import threading
import time

from flask import Blueprint, current_app, jsonify, render_template, request

from core import app_config, idle_exit, logging_utils
from core.logging_utils import get_logger

from .. import current_business as business

log = get_logger("app.routes.health")

bp = Blueprint("health", __name__)

# 停止要求を受けてから実際に落とすまでの猶予(秒)。
# 応答を返しきる前に止めると「押したのに何も起きない」ように見える
SHUTDOWN_DELAY_SEC = 0.4

# `POST /api/shutdown` で呼ぶ後始末。`server.py` が起動時に差し込む
_shutdown_hook = None


def set_shutdown_hook(func) -> None:
    """サーバの止め方を登録する。"""
    global _shutdown_hook
    _shutdown_hook = func


@bp.get("/")
def index():
    """準備が終わるまでは起動待機画面、終わったら点検表の画面。

    骨格は `core.boot_screen` に1つだけ(起動サーバと同じものを出す)。
    """
    config = current_app.config
    if not config["READY"]:
        from core import boot_screen

        return boot_screen.render(
            display_name=config["DISPLAY_NAME"],
            version_label=app_config.version_label(),
            token=config["TOKEN"],
            app_id=config["APP_ID"],
            poll_ms=app_config.job_poll_ms(),
            home_url="/?t=" + config["TOKEN"],
            log_dir=str(logging_utils.log_dir()),
        )
    biz = business()
    return render_template(
        "main.html",
        token=config["TOKEN"],
        # このタブの番号。心拍に付けて送り、見張りは画面ごとに数える
        screen_id=secrets.token_hex(8),
        display_name=config["DISPLAY_NAME"],
        version_label=app_config.version_label(),
        version=app_config.version(),
        # 版の書き方がおかしければフォルダ設定の画面に出す(起動は止めない)
        version_problem=app_config.version_problem(),
        dark=biz.settings.dark_mode(),
        demo=biz.demo,
        max_copies=biz.cfg.max_copies,
        health_poll_ms=app_config.health_poll_seconds() * 1000,
        alive_poll_ms=idle_exit.HEARTBEAT_MS,
        job_poll_ms=app_config.job_poll_ms(),
    )


@bp.get("/api/health")
def health():
    """起動確認と生存監視。

    **`app_id` を返すのが要点。** 同じポートに別のアプリが居ても、HTTPが
    返るだけでは「自分と同じアプリ」とは言えない。業務データは含めない。
    """
    config = current_app.config
    return jsonify({
        "app_id": config["APP_ID"],
        "display_name": config["DISPLAY_NAME"],
        "version": config["VERSION"],
        "app_root": str(app_config.APP_ROOT),
        "port": config["PORT"],
        # どちらで動いているか(デスクトップ版はポートを使わない)
        "edition": "desktop" if config.get("BRIDGE") else "browser",
        "pid": os.getpid(),
        "ready": bool(config["READY"]),
        "stage": config["STAGE"],
        "stage_key": config.get("STAGE_KEY", "prepare"),
        "startup_error": config["STARTUP_ERROR"],
        "uptime_sec": round(time.time() - config["STARTED_AT"], 1),
        # 共有フォルダのアプリが新しい版に入れ替えられたか(画面が「起動し直して」を出す)
        "version_on_disk": app_config.version_on_disk(),
        # 走っている処理の一言(帯と待機画面が読む)
        "job": _running_note(),
    })


def _running_note():
    try:
        biz = business()
    except Exception:  # noqa: BLE001 - 見張りは止めない
        return None
    status = biz.printing.status()
    if status.get("active"):
        return {"label": "印刷", "message": status.get("message", ""),
                "current": status.get("current_index", 0), "total": status.get("total", 0)}
    if biz.excel.activity == "preview":
        return {"label": "プレビュー", "message": "プレビューを作成しています", "current": 0, "total": 0}
    scan = biz.inspection.status()
    if scan.get("state") == "scanning":
        return {"label": "フォルダ検索", "message": f"{scan.get('scanned', 0)} 件確認済み",
                "current": 0, "total": 0}
    return None


@bp.post("/api/alive")
def alive():
    """画面が生きていることの心拍(基盤仕様書 2.8「自動終了」)。

    `leaving=true` はタブを閉じた合図(`sendBeacon`)。猶予のあとで終わるが、
    そのあいだに心拍が戻れば取り消す。`visible=false` は裏に回った合図 ──
    ブラウザは裏のタブのタイマーを間引くので、そう言った画面は落とさない。
    `resumed=true` は表に戻った / スリープから戻った合図。
    """
    body = request.get_json(silent=True) or {}
    watch = idle_exit.get()
    if watch is None:
        return jsonify({"ok": True, "watching": False})
    screen = str(body.get("screen_id") or "")
    if body.get("leaving"):
        watch.leaving(screen)
    elif body.get("resumed"):
        watch.resumed(screen, gap_sec=_number(body.get("gap_ms")) / 1000,
                      visible=body.get("visible") is not False)
    elif body.get("visible") is False:
        watch.hidden(screen)
    else:
        watch.beat(screen, visible=True if body.get("visible") else None)
    return jsonify({"ok": True, "watching": True})


def _number(value) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


@bp.post("/api/shutdown")
def shutdown():
    """安全な停止(基盤仕様書 2.8)。

    印刷中は**止めずに理由を返す**。`force=true` で呼び直すと、いまの1件が
    終わったところで中止して落とす(`InspectionBusiness.on_shutdown`)。
    """
    body = request.get_json(silent=True) or {}
    force = bool(body.get("force"))
    running = business().busy_labels()
    if running and not force:
        return jsonify({
            "stopped": False,
            "reason": "busy",
            "running": running,
            "message": f"{'・'.join(running)}の途中です。中断して終了しますか?",
        }), 409
    # `check` は**訊くだけ**(止めない)。統合ツールの外枠が、窓の × で全ツールに
    # 「終わってよいか」を先に訊いてから、まとめて止めるために使う。1つでも
    # 処理の途中なら、ほかのツールも止めずに確認を出す(先に止めてしまわない)
    if body.get("check"):
        return jsonify({"stopped": False, "can_stop": True})
    if _shutdown_hook is None:
        return jsonify({"stopped": False, "reason": "no_hook",
                        "message": "このプロセスは停止操作に対応していません"}), 501
    log.info("停止要求を受け付けました (force=%s)", force)
    from core import event_log
    event_log.record("shutdown.request", event_log.INFO, force=force,
                     by="画面の「終了」" if request.headers.get("X-Screen-Id") else "stop.bat・ほかのプログラム",
                     busy=business().busy_labels() if force else None)
    threading.Timer(SHUTDOWN_DELAY_SEC, _shutdown_hook).start()
    return jsonify({"stopped": True, "message": "終了します"})
