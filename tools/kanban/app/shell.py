"""画面の外枠に渡す値

左のレールの並びと、テンプレートが必要とする共通の値をここで作る。

**並びは作業の順序。** 現場は「見る → 印刷 → 設定」、倉庫は「ラインを選ぶ →
印刷 → 設定」。tkinter 版はボタンを横一列に並べていたが、Web 版では画面
そのものが分かれるので、行き先を縦に並べる。

モードごとに出す画面が変わる。倉庫参照モードは読むだけなので、押せる操作の
ある画面は出さない。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from flask import current_app

from kanban import app_config, config, idle_exit


@dataclass
class NavItem:
    key: str
    label: str
    url: str
    note: str = ""
    """名前の下に出す一行。何をする画面か。"""

    badge: str = ""
    badge_kind: str = "todo"
    """``todo`` | ``done``。"""


def nav_items(mode: str, badges: dict[str, tuple[str, str]] | None = None) -> list[NavItem]:
    """レールの項目。``badges`` は ``{key: (文言, 種別)}``。

    バッジは「その先に何があるか」を行く前に示すためのもの。未反映件数など。
    """
    badges = badges or {}
    source: tuple[tuple[str, str, str, str], ...]
    if mode == config.MODE_SITE:
        source = (
            ("board", "看板", "/board", "発注する・届いた資材を確認する"),
            ("settings", "設定", "/settings", "接続先とモード"),
        )
    elif mode == config.MODE_WAREHOUSE:
        source = (
            ("board", "看板", "/board", "発送する・注文中にする"),
            ("settings", "設定", "/settings", "接続先とモード"),
        )
    else:
        source = (
            ("board", "看板", "/board", "発注/発送の状況を見る"),
            ("settings", "設定", "/settings", "接続先とモード"),
        )

    items = []
    for key, label, url, note in source:
        badge, kind = badges.get(key, ("", "todo"))
        items.append(NavItem(key=key, label=label, url=url, note=note,
                             badge=badge, badge_kind=kind))
    return items


# タブのアイコン。現場は赤、倉庫は緑、倉庫参照は灰。文字は「現」「倉」「見」。
# 3つのモードを同時に開いていても、タブの並びで取り違えないようにする
# (タブの表題は幅が足りず途中で切れる)。**色と文字はモードが持つ**
_FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="6" fill="{color}"/>'
    '<text x="16" y="23" font-size="19" font-family="sans-serif"'
    ' text-anchor="middle" fill="{ink}">{mark}</text></svg>'
)

_MODE_MARKS = {
    config.MODE_SITE: ("#d94040", "#ffffff", "現"),
    config.MODE_WAREHOUSE: ("#2e9e51", "#ffffff", "倉"),
    config.MODE_WAREHOUSE_VIEW: ("#8a949e", "#ffffff", "見"),
}


def favicon(mode: str) -> str:
    """タブのアイコン(SVG そのもの)。``data:`` URL に埋めて使う。"""
    color, ink, mark = _MODE_MARKS.get(mode, _MODE_MARKS[config.MODE_SITE])
    return _FAVICON.format(color=color, ink=ink, mark=mark)


def mode_tone(mode: str) -> str:
    """帯やバッジに使う色の種別名(``tokens.css`` のトークン名)。"""
    return {
        config.MODE_SITE: "site",
        config.MODE_WAREHOUSE: "warehouse",
        config.MODE_WAREHOUSE_VIEW: "view",
    }.get(mode, "site")


def shell_context(active: str, *, badges: dict[str, tuple[str, str]] | None = None) -> dict[str, Any]:
    """``base.html`` が必要とする値一式。

    各画面のルートは ``render_template("...", **shell_context("board"))`` で使う。
    ここに集めておくことで、画面ごとに渡し忘れが起きない。
    """
    conf = current_app.config
    mode = conf["MODE"]
    return {
        "active": active,
        "nav": nav_items(mode, badges),
        "mode": mode,
        "mode_label": config.mode_display_name(mode),
        "mode_tone": mode_tone(mode),
        "read_only": mode == config.MODE_WAREHOUSE_VIEW,
        # デスクトップ版(Rust/Tauri の窓。ポートを使わない ``bridge.py``)で動いているか
        "desktop": bool(conf.get("BRIDGE")),
        "line": conf["LINE"],
        # **担当ラインは現場モードだけ。** 倉庫・倉庫参照は全ラインを見るので、
        # 設定に残っている現場の頃のラインを出すと「倉庫なのに担当 L-1」になる
        "line_label": (config.display_name(conf["LINE"])
                       if conf["LINE"] and mode == config.MODE_SITE else ""),
        "display_name": conf["DISPLAY_NAME"],
        # 帯に常時出す版。**どれが入っている端末か**を聞かれたときに、画面を
        # 見れば答えられるようにする(出どころは config/app.json)
        "version_label": app_config.version_label(),
        "favicon": favicon(mode),
        # 権限が無くて、頼まれたモードとは別のモードで開いたときの理由(start_app._apply_access)。
        # **黙って別のモードで開かない** ── 倉庫のつもりで現場の画面を触らせない
        "startup_notice": conf.get("STARTUP_NOTICE", ""),
        "token": conf["TOKEN"],
        "health_poll_ms": app_config.health_poll_seconds() * 1000,
        "board_poll_ms": max(2000, app_config.health_poll_seconds() * 1000 // 3),
        # 心拍の間隔。**出どころは idle_exit ただ1つ** ── 画面とサーバで
        # 別に決めると、片方を直しただけで自動終了が誤る
        "alive_poll_ms": idle_exit.HEARTBEAT_MS,
    }
