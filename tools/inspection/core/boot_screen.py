"""起動待機画面 (基盤仕様書 2.2 / 2.3)

【なぜ独立したモジュールなのか】
この画面は **Flask が読み込まれる前に**出さなければなりません。
起動でいちばん時間を食うのは Flask とアプリ本体の import で、
そのあとに待機画面を出していたら「待たせるために見せるもの」を
待たせていることになります。

そこでここは **標準ライブラリだけ**で組み、`boot_server` が最小の
WSGI で先に出せるようにしてあります。本体が組み上がったあとも
同じ関数を Flask 側の `/` が呼ぶので、**骨格は `loading.html` の1枚**です。

【1往復で出す】
外部への要求(CSS・画像・書体)を1つも出しません。色は
`app/static/css/tokens.css` を読んで**そのまま埋め込みます**。
"""
from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATE = _ROOT / "loading.html"
_TOKENS = _ROOT / "app" / "static" / "css" / "tokens.css"

# `tokens.css` を読めなかったときの控え。帯の色だけを持つ
_FALLBACK_TOKENS = """
:root{
  --bar-a:#153247; --bar-b:#1e6079; --bar-ink:#ffffff; --bar-muted:#c3dbe4;
  --bar-face:#ffffff14; --bar-edge:#ffffff33; --bar-focus:#7fe3ea;
  --bar-ok:#8ff0c4; --bar-alert:#ffc4bf;
  --sans:"Yu Gothic UI","Meiryo UI",system-ui,sans-serif;
  --mono:ui-monospace,Consolas,"Courier New",monospace;
}
"""

# 段。**4つだけ**(基盤仕様書 2.2)。進捗率を正確に出すことではなく、
# 無反応に見える時間をなくすのが目的
STEPS = (
    ("env", "実行環境を確認"),
    ("prepare", "アプリを準備"),
    ("scan", "点検表フォルダ"),
    ("done", "完了"),
)

_cache: dict = {}


def _read(path: Path, fallback: str) -> str:
    key = str(path)
    if key not in _cache:
        try:
            _cache[key] = path.read_text(encoding="utf-8")
        except OSError:
            _cache[key] = fallback
    return _cache[key]


def tokens_css() -> str:
    return _read(_TOKENS, _FALLBACK_TOKENS)


def render(*, display_name: str, version_label: str, token: str, app_id: str,
           poll_ms: int, home_url: str, log_dir: Optional[str] = None) -> str:
    """待機画面のHTML。呼ぶ側の都合を持たないので、起動サーバと Flask の両方が同じものを出せる。"""
    template = _read(_TEMPLATE, "<!doctype html><title>{{DISPLAY_NAME}}</title>"
                                "<p>起動しています...</p><script>{{BOOT_OPTIONS}}</script>")
    steps = "".join(
        f'<li data-step="{key}" data-state="wait"><span class="tick"></span><span>{label}</span></li>'
        for key, label in STEPS)
    options = json.dumps({"pollMs": int(poll_ms), "appId": app_id, "token": token, "home": home_url},
                         ensure_ascii=False).replace("<", "\\u003c")
    if log_dir is None:
        try:
            from . import logging_utils
            log_dir = str(logging_utils.log_dir())
        except Exception:  # noqa: BLE001 - 待機画面は必ず出す
            log_dir = "%LOCALAPPDATA%\\InspectionSheetPrint\\logs"
    values = {
        "{{TOKENS_CSS}}": tokens_css(),
        "{{DISPLAY_NAME}}": html.escape(display_name),
        "{{VERSION_LABEL}}": html.escape(version_label),
        "{{STEPS}}": steps,
        "{{LOG_DIR}}": html.escape(log_dir),
        "{{BOOT_OPTIONS}}": options,
    }
    for key, value in values.items():
        template = template.replace(key, value)
    return template
