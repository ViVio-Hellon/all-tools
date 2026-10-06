"""統合ツール固有の値(`config/app.json`)と、置き場所の決め方

置き場所は**外枠(Rust)の `src-tauri/src/places.rs` と同じ決め方**にする ── ずれると、
外枠が案内するログの場所と、ここが書いている場所が食い違う。

    手元の領域  %LOCALAPPDATA%\\AllTools\\   (`ALLTOOLS_LOCAL_DIR` で変えられる)
      ├ data\\     利用者の設定(`user_settings.json`)・タブ表示権限の手元の写し
      ├ logs\\     入口のログ・外枠の記録(`desktop_shell.log`)
      └ runtime\\  錠(Windows 以外)・ブラウザ版の印

Store 版の Python では `%LOCALAPPDATA%` への書き込みが Python 専用の場所へ
振り替えられる。ここ(Python)が書くものは Python からはいつも同じ場所に見える。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

APP_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("ALLTOOLS_APP_CONFIG", str(APP_ROOT / "config" / "app.json")))

_DEFAULTS: dict[str, Any] = {
    "app_id": "nlm.all-tools",
    "display_name": "統合ツール",
    "version": "0.0.0",
    "local_dir_name": "AllTools",
    "server": {"host": "127.0.0.1", "port": 8700, "port_retry": 9},
    "monitoring": {"health_poll_seconds": 15, "status_poll_ms": 2000},
}

#: デスクトップ版の窓が読む宛先の名前。外枠がツールへ渡すときに、Host を
#: 単体のデスクトップ版と同じこの名前に揃える(`src-tauri/src/relay.rs`)
BRIDGE_HOSTS = ("app.localhost", "localhost")

LOCAL_SUBDIRS = ("data", "logs", "runtime", "work")

_cache: Optional[dict[str, Any]] = None
_error = ""


def load() -> dict[str, Any]:
    """`config/app.json` を読む。読めなければ既定値(理由は `load_error()`)。"""
    global _cache, _error
    if _cache is None:
        data = json.loads(json.dumps(_DEFAULTS))
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            for key, value in raw.items():
                if isinstance(value, dict) and isinstance(data.get(key), dict):
                    data[key].update(value)
                else:
                    data[key] = value
        except (OSError, ValueError) as exc:
            _error = f"{CONFIG_PATH} を読めません: {exc}"
        _cache = data
    return _cache


def reset() -> None:
    """読み直す(試験用)。"""
    global _cache, _error
    _cache = None
    _error = ""


def load_error() -> str:
    load()
    return _error


def app_id() -> str:
    return str(load()["app_id"])


def display_name() -> str:
    return str(load()["display_name"])


def version() -> str:
    return str(load()["version"])


def host() -> str:
    return str(load()["server"]["host"])


def port_candidates() -> list[int]:
    server = load()["server"]
    base = int(server.get("port", 8700))
    retry = max(0, int(server.get("port_retry", 0)))
    return [base + i for i in range(retry + 1)]


def health_poll_ms() -> int:
    return int(load()["monitoring"].get("health_poll_seconds", 15)) * 1000


def status_poll_ms() -> int:
    return int(load()["monitoring"].get("status_poll_ms", 2000))


# ------------------------------------------------------------------
# 手元の領域
# ------------------------------------------------------------------
def local_root() -> Path:
    """この端末・この利用者の作業領域(`places.rs` の `local_root` と同じ決め方)。"""
    custom = os.environ.get("ALLTOOLS_LOCAL_DIR", "").strip()
    if custom:
        return Path(custom)
    name = str(load().get("local_dir_name") or "AllTools")
    for env in ("LOCALAPPDATA", "XDG_DATA_HOME"):
        base = os.environ.get(env, "").strip()
        if base:
            return Path(base) / name
    return Path.home() / ".local" / "share" / name


def local_dir(name: str) -> Path:
    path = local_root() / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_local_dirs() -> Path:
    root = local_root()
    for name in LOCAL_SUBDIRS:
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def is_desktop() -> bool:
    """統合ツールの窓(外枠)から起こされたか。"""
    return os.environ.get("ALLTOOLS_SHELL", "") == "1"


def origin_of(scheme: str) -> str:
    """デスクトップ版で、そのツールの画面の宛先(`catalog.rs` の `origin` と同じ)。"""
    if sys.platform == "win32":
        return f"http://{scheme}.localhost"
    return f"{scheme}://localhost"


def describe() -> str:
    """診断用(start.bat --check)。"""
    lines = [
        f"アプリ      : {display_name()} {version()} ({app_id()})",
        f"アプリの場所: {APP_ROOT}",
        f"手元の領域  : {local_root()}",
        f"Python      : {sys.version.split()[0]} ({sys.executable})",
    ]
    if load_error():
        lines.append(f"設定        : {load_error()}(既定値で動きます)")
    return "\n".join(lines)
