"""PC ごとの設定 (VBA ではレジストリに保存していたもの) を JSON で保持する。

VBA 版は ``SaveSetting/GetSetting`` でレジストリの
``ライン管理カレンダー\\設定\\PCライン`` に保存していた。
Python 版では標準ライブラリのみで動かすため JSON ファイルに置き換える。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from . import config

#: レジストリのキー名に相当する設定キー
KEY_MY_LINE = "PCライン"

#: 取り込み元の置き場所 (参照パス)。**フォルダで持つ** ── 理由は
#: ``config`` の「参照パス」の説明を参照
KEY_DATA_DB_DIR = "保存用DBフォルダ"
KEY_MASTER_DB_DIR = "マスタDBフォルダ"

#: 旧名(ファイルのフルパスで持っていたころ)。**読むだけ**残す ──
#: 設定済みの端末が、更新の日に置き場所を見失わないように
KEY_ACCESS_DATA_PATH = "Access保存用DBパス"
KEY_ACCESS_MASTER_PATH = "Accessマスタ DBパス"

#: ログを書くフォルダ。空なら既定(``%LOCALAPPDATA%\\LineCalendar\\logs``)。
#: 指定したときは、その下に PC名 のフォルダを作って書く(``logging_utils``)
KEY_LOG_DIR = "ログフォルダ"

KEY_LAST_IMPORT = "最終取込日時"
#: 自動同期を行うか
KEY_AUTO_SYNC = "自動同期"
#: 自動同期の間隔 (秒)
KEY_SYNC_INTERVAL = "自動同期間隔秒"

#: 入力してから Access へ送るまでの既定の待ち時間 (秒)
DEFAULT_SYNC_INTERVAL = 20


def load() -> dict[str, Any]:
    """設定ファイル全体を読み込む。存在しない/壊れている場合は空の辞書。"""
    path = config.settings_path()
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        # 設定が壊れていてもアプリは起動できるようにする
        return {}


#: 設定ファイルの「読んで・直して・書く」を1つずつにする錠。
#: **同時に2つ保存すると、片方の値が消えていた** ── どちらも同じ古い
#: 中身を読み、自分の1項目だけ直して書くので、後に書いたほうが先の
#: 変更を古い値で上書きする(設定画面の保存と、配布設定・端末の記録が
#: 重なったときなど)。「打った値が勝手に戻ることがありました」の一因
LOCK = threading.RLock()


def save(data: dict[str, Any]) -> None:
    """設定ファイル全体を書き出す (一時ファイル経由で原子的に置き換える)。

    **一時ファイルの名前は書くたびに変える。** 以前は ``settings.tmp``
    固定で、2つの保存が重なると同じ一時ファイルへ交互に書き、途中までの
    中身や相手の中身で置き換えることがあった。
    """
    path = config.settings_path()
    with LOCK:
        fd, name = tempfile.mkstemp(prefix=path.stem + ".", suffix=".tmp",
                                    dir=str(path.parent))
        tmp = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            tmp.replace(path)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise


def get(key: str, default: Any = "") -> Any:
    return load().get(key, default)


def has_value(key: str) -> bool:
    """その項目が設定ファイルに書かれているか(既定のままか)。"""
    return key in load()


def set_value(key: str, value: Any) -> None:
    with LOCK:
        data = load()
        data[key] = value
        save(data)


def update(values: dict[str, Any]) -> None:
    """いくつかの項目を**まとめて1回で**書く(途中の状態を残さない)。"""
    with LOCK:
        data = load()
        data.update(values)
        save(data)


# ---------------------------------------------------------------------------
# ライン設定 (VBA: GetMyLine / SaveMyLine)
# ---------------------------------------------------------------------------
def get_my_line() -> str:
    """この PC に登録されているライン名。未登録なら空文字。"""
    value = get(KEY_MY_LINE, "")
    return value if isinstance(value, str) else ""


def save_my_line(line_name: str) -> None:
    """この PC のライン設定を保存する。"""
    set_value(KEY_MY_LINE, line_name)


# ---------------------------------------------------------------------------
# 自動同期
# ---------------------------------------------------------------------------
def auto_sync_enabled() -> bool:
    """自動同期を行う設定か (既定は有効)。"""
    return bool(get(KEY_AUTO_SYNC, True))


def set_auto_sync(enabled: bool) -> None:
    set_value(KEY_AUTO_SYNC, bool(enabled))


def sync_interval() -> int:
    """自動同期の間隔 (秒)。短すぎる値は 5 秒に丸める。"""
    try:
        value = int(get(KEY_SYNC_INTERVAL, DEFAULT_SYNC_INTERVAL))
    except (TypeError, ValueError):
        return DEFAULT_SYNC_INTERVAL
    return max(value, 5)


# ---------------------------------------------------------------------------
# 参照パス
# ---------------------------------------------------------------------------
def data_db_dir_setting() -> str:
    """保存用DBのフォルダ(打たれたままの文字列)。"""
    value = get(KEY_DATA_DB_DIR, "")
    return value if isinstance(value, str) else ""


def master_db_dir_setting() -> str:
    """マスタDBのフォルダ(打たれたままの文字列)。"""
    value = get(KEY_MASTER_DB_DIR, "")
    return value if isinstance(value, str) else ""


def access_data_path() -> str:
    """共有されている保存用 Access ファイルのパス。

    **いまの出どころは参照パス**(``sources.find_data_db``)で、ここは
    旧設定を読むためだけに残してある。ファイルのフルパスで持っていた
    ころに設定した端末が、更新の日に置き場所を見失わないようにするため。
    """
    value = get(KEY_ACCESS_DATA_PATH, "")
    return value if isinstance(value, str) else ""


# ---------------------------------------------------------------------------
# ログフォルダ
# ---------------------------------------------------------------------------
def log_dir_setting() -> str:
    """ログフォルダ(打たれたままの文字列)。未設定なら空文字。"""
    value = get(KEY_LOG_DIR, "")
    return value if isinstance(value, str) else ""


# ---------------------------------------------------------------------------
# 画面の見た目(ライト / ダーク)
# ---------------------------------------------------------------------------
#: 画面の明暗。**この端末だけ**の好み(夜勤で暗い背景にしたい、など)。
#: 決めていなければ**ライト**(日報複合ツールの4ツールでそろえる。「自動」は選べば効く)
KEY_THEME = "画面の見た目"
THEME_AUTO = "auto"
THEME_LIGHT = "light"
THEME_DARK = "dark"

#: 選べるもの: (値, 画面の名前)。**ここに無いものは選べない**
THEME_CHOICES: tuple[tuple[str, str], ...] = (
    (THEME_AUTO, "自動(Windows の設定に合わせる)"),
    (THEME_LIGHT, "ライト(明るい背景)"),
    (THEME_DARK, "ダーク(暗い背景)"),
)
THEME_LABELS = dict(THEME_CHOICES)


def get_theme() -> str:
    """この端末の画面の見た目。知らない値・未設定は「ライト」。"""
    value = get(KEY_THEME, THEME_LIGHT)
    return value if value in THEME_LABELS else THEME_LIGHT


def theme_is_chosen() -> bool:
    """この端末で見た目を選んだか。選んでいなければ、日報複合ツールの大設定の既定に従う。"""
    return get(KEY_THEME, None) in THEME_LABELS


def set_theme(theme: str) -> None:
    """画面の見た目を保存する。**一覧に無い値は受けない**(``ValueError``)。"""
    if theme not in THEME_LABELS:
        raise ValueError(f"知らない見た目です: {theme!r}")
    set_value(KEY_THEME, theme)
