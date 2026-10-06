"""外枠(帯とレール)に渡す値

**画面の骨格が読むものは1か所で作る。** 帯に出す事実・レールの並び・
版の表示を各テンプレートが自前で組むと、画面ごとに食い違う。

レールは3つだけ:

    1. カレンダー … 登録と閲覧。ここが主
    2. 履歴       … 過去の月を見るだけ(編集できない)
    3. 設定       … この端末の設定と、取り込み元との同期

tkinter 版では [ライン設定] [同期設定] [取り込み] [今すぐ同期] が
カレンダー上部のボタンに並んでいた。**毎日使うもの(日付を押す)と、
最初に1度決めるもの(ライン設定)が同じ高さに並んでいた**ので、設定側を
別の画面へ移した。
"""

from __future__ import annotations

import os

from dataclasses import dataclass
from typing import Any

from calendar_app import app_config, idle_exit, settings as user_settings
from calendar_app.presenters import settings as settings_presenter


@dataclass
class NavItem:
    """レールの1項目。"""

    key: str
    label: str
    url: str
    note: str = ""          # 名前だけでは何をする画面か分からないため
    badge: str = ""
    badge_kind: str = ""


def home_url() -> str:
    """準備が終わったあとに開く画面。"""
    return "/calendar"


def nav(active: str) -> list[NavItem]:
    """レール。**送信待ちがあれば設定にバッジを出す。**

    黙って溜めない ── 送れていないことは、押さなくても見えるところに出す
    (基盤仕様書 2.9)。
    """
    sync = settings_presenter.sync_dict()
    badge, kind = "", ""
    if sync["offline"]:
        badge, kind = "!", "alert"
    elif sync["pending"]:
        badge, kind = str(sync["pending"]), "wait"

    return [
        NavItem("calendar", "カレンダー", "/calendar",
                "休み・連絡の登録と閲覧"),
        # 過去の月を見るだけの画面。**書き込む道を持たない**(routes/history.py)
        NavItem("history", "履歴", "/history", "過去の月を見る(編集不可)"),
        NavItem("settings", "設定", "/settings",
                "ライン設定・取り込み元との同期", badge=badge, badge_kind=kind),
    ]


def shell_context(active: str) -> dict[str, Any]:
    """テンプレートの外枠へ渡す値ぜんぶ。"""
    from flask import current_app

    return {
        "display_name": current_app.config["DISPLAY_NAME"],
        "version_label": app_config.version_label(),
        "token": current_app.config["TOKEN"],
        # **この画面を出したのがどのプロセスか**を持たせる。起動し直されると
        # トークンが変わるので、古いタブは読み込み直さないと何も通らない
        # (health.js が見比べる)
        "version": app_config.version(),
        "pid": os.getpid(),
        "my_line": user_settings.get_my_line() or "未設定",
        "nav": nav(active),
        "active": active,
        "health_poll_ms": app_config.health_poll_seconds() * 1000,
        "sync_poll_ms": app_config.sync_poll_ms(),
        # 心拍の間隔。**出どころは idle_exit ただ1つ**(見張る側と送る側が
        # 別々に持つと、片方だけ変えたときに途切れと誤る)
        "heartbeat_ms": idle_exit.HEARTBEAT_MS,
        "favicon": _favicon(),
        # 画面の見た目(ライト / ダーク / 自動)。**HTML を返す時点で決める**
        # ── 画面の JS が読んでから切り替えると、開くたびに一瞬白く光る
        "theme": user_settings.get_theme(),
    }


def _favicon() -> str:
    """タブのアイコン。

    外部ファイルにしないのは要求を1本減らすため。カレンダーだと分かる形に
    しておくと、タブの文字が切れても見分けられる。
    """
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        '<rect width="32" height="32" rx="6" fill="#2f5d8a"/>'
        '<rect x="6" y="9" width="20" height="16" rx="2" fill="#ffffff"/>'
        '<rect x="6" y="9" width="20" height="4" rx="2" fill="#d24b45"/>'
        '<rect x="9" y="16" width="4" height="4" fill="#2f5d8a"/>'
        '</svg>'
    )
