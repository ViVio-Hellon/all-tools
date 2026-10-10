"""アプリ固有値の唯一の出どころ (``config/app.json`` の読み取り)

社内「汎用Webアプリ作成 基盤仕様書」5.2 が、アプリごとに決める値
(アプリケーションID・表示名・使用ポート・ローカル保存フォルダー名・監視レベル)を
**複数ファイルへ直接書き散らさず、設定または共通定数へ集約する**ことを求めている。
このモジュールがその集約点になる。

【``config.py`` との違い】
- ``config.py``     … 業務の定数(テーブル名・区分・ライン名・表示書式)
- ``app_config.py`` … アプリという「入れ物」の値(ID・ポート・ローカル領域)

起動基盤(``start_app.py`` / ``launch_guard.py`` / ``server.py``)はこちらだけを見る。
業務コードはこちらを見ない。この分離が基盤仕様書 2.5「起動処理とアプリ本体の分離」
にあたる。

**このモジュールは業務モジュールを一切読み込まない。** 待機画面より前に
動くため、重い import を1つも増やさない(基盤仕様書 2.2)。

【なぜ TOML ではなく JSON か】
``tomllib`` は Python 3.11 以降の標準ライブラリで、3.9/3.10 では追加パッケージが要る。
依存を Flask と waitress の2つに抑える方針を崩さないため JSON にした。
JSON はコメントを書けないので、各キーの説明はこのモジュールに置いてある。

【ローカル領域】(基盤仕様書 2.7 / 4.6)
ログ・キャッシュ・実行時情報・DBは、共有配置されうるアプリ本体側ではなく
利用者ごとのローカル領域へ置く。Windows は ``%LOCALAPPDATA%``、
それ以外は ``~/.local/share``(XDG)を使う。

    %LOCALAPPDATA%\\LineCalendar\\
      runtime\\  … PID・ポート・状態(停止後は消してよい)
      logs\\     … launcher.log / app.log
      pycache\\  … PYTHONPYCACHEPREFIX の向き先
      cache\\    … 再取得できる高速化用データ
      work\\     … 印刷用HTMLなどの一時ファイル
      backup\\   … DBのバックアップ
      data\\     … calendar.db / settings.json など**消してはいけない**もの

``data`` は基盤仕様書の一覧には無い追加。仕様書 2.7 の目的は
「アプリ本体と、実行中に変化するファイルを分ける」ことであり、DBと利用者設定は
まさにそれにあたる。``cache``/``work`` と違って消せないので独立した名前にしている。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Optional

# このファイルの2つ上 = リポジトリのルート(= 基盤仕様書のいう ApplicationRoot)
APP_ROOT = Path(__file__).resolve().parent.parent

# アプリ固有値の置き場所。環境変数で差し替えられるようにしておくと、
# 検証時に本番設定を書き換えずに試せる
CONFIG_PATH = Path(os.environ.get(
    "CALENDAR_APP_CONFIG", str(APP_ROOT / "config" / "app.json")))

# ``config/app.json`` が読めないときに使う値。
# 設定ファイルが壊れていても「起動はして、画面に理由を出す」ほうが、
# 起動そのものが失敗するより調査しやすい(基盤仕様書 ステップ5)。
_FALLBACK: dict[str, Any] = {
    "app_id": "nlm.line-calendar",
    "display_name": "ライン管理カレンダー",
    "version": "0.0.0",
    "monitor_level": 1,
    "local_dir_name": "LineCalendar",
    "server": {
        "host": "127.0.0.1",
        "port": 8730,
        "port_retry": 3,
    },
    "monitoring": {
        "health_poll_seconds": 15,
        "sync_poll_ms": 1000,
    },
}

# 読み込み結果のキャッシュ。設定は起動中に変わらない
_cache: Optional[dict[str, Any]] = None
# 既定値へ落ちた理由(画面やログに出して原因を追えるようにする)
_load_error: str = ""


def load(*, force: bool = False) -> dict[str, Any]:
    """``config/app.json`` を読む。壊れていても例外を投げず既定値へ落とす。"""
    global _cache, _load_error
    if _cache is not None and not force:
        return _cache

    _load_error = ""
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("トップレベルがオブジェクトではありません")
        _cache = _merge(_FALLBACK, raw)
    except FileNotFoundError:
        _load_error = f"設定ファイルがありません: {CONFIG_PATH}"
        _cache = _copy(_FALLBACK)
    except Exception as exc:  # noqa: BLE001 - 理由を残して既定値で続行する
        _load_error = f"設定ファイルを読めませんでした ({CONFIG_PATH}): {exc}"
        _cache = _copy(_FALLBACK)
    return _cache


def load_error() -> str:
    """既定値へ落ちた場合の理由。正常なら空文字。"""
    load()
    return _load_error


def _copy(value: Any) -> Any:
    """入れ子の dict を実体コピーする(既定値を書き換えさせない)。"""
    if isinstance(value, dict):
        return {k: _copy(v) for k, v in value.items()}
    return value


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """既定値に設定ファイルの値をかぶせる。

    設定ファイルに書き忘れたキーがあっても既定値で埋まるので、
    キーが1つ足りないだけで起動できなくなることがない。
    """
    result = _copy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = _copy(value)
    return result


# ---------------------------------------------------------------------------
# アプリの識別
# ---------------------------------------------------------------------------
def app_id() -> str:
    """起動確認APIで照合する識別子(基盤仕様書 2.3)。

    同じポートを別のアプリが使っている場合に、誤って「起動成功」と
    判定しないためのもの。単にHTTPが返るかどうかでは判別できない。
    """
    return str(load()["app_id"])


def display_name() -> str:
    return str(load()["display_name"])


# 版の書き方。**メジャー.マイナー.パッチ の3つの数字だけ**。
#
# 端末はフォルダごとコピーして配るので、「いまどれが入っているか」を
# 聞かれたときに答えられる番号が要る。番号は ``config/app.json`` の
# ``version`` ただ1つが出どころで、画面(帯のバッジ)・``/api/health``・
# 設定画面はすべてここを読む。
_VERSION_FORM = re.compile(r"^\d+\.\d+\.\d+$")

# 画面に出すときの前置き。「3.0.0」だけだと何の番号か分からない
VERSION_PREFIX = "VER"


def version() -> str:
    return str(load()["version"])


def version_label() -> str:
    """帯のバッジに出す形。例 ``VER3.0.0``。"""
    return f"{VERSION_PREFIX}{version()}"


def version_problem() -> str:
    """版の書き方がおかしければ理由。正しければ空文字。

    番号が読めない形だと「どれが新しいのか」を並べて比べられなくなる。
    起動は止めず(版が読めなくても業務はできる)、設定画面に出す。
    """
    text = version()
    if _VERSION_FORM.match(text):
        return ""
    return (f"版の書き方が違います: {text!r}。"
            "config/app.json の version は「3.0.0」のように"
            "数字3つで書いてください。")


def monitor_level() -> int:
    """基盤仕様書 3章の監視レベル。本アプリは1(通常の操作アプリ)。

    カレンダーは入力・閲覧が主で、ブラウザを閉じたあとに続ける処理を持たない。
    取り込み元との同期は数秒で終わり、送れなければ送信待ちに残って次回やり直す
    (``calendar_app/sync/autosync.py``)ため、常駐の見張りは要らない。
    """
    return int(load()["monitor_level"])


# ---------------------------------------------------------------------------
# サーバ
# ---------------------------------------------------------------------------
def host() -> str:
    """待ち受けアドレス。``127.0.0.1`` 固定にすることでLANから到達できなくなり、
    Windowsのファイアウォール警告も出ない。"""
    return str(load()["server"]["host"])


# デスクトップ版(日報複合ツールの窓)で、外枠がこのツールへ要求を渡すときのホスト名。
# 日報複合ツールの外枠(``../../src-tauri/src/relay.rs`` の ``TOOL_HOST``)と揃える
# (画面の宛先は ``calendar://localhost/``、Windows は ``http://calendar.localhost/``。
# 外枠が単体のデスクトップ版のときと同じ ``app.localhost`` に読み替えて渡す)
BRIDGE_HOSTS = ("app.localhost", "localhost")


def port() -> int:
    return int(load()["server"]["port"])


def port_candidates() -> list[int]:
    """使用中だったときに順に試すポート。

    基盤仕様書 2.4 は多重起動の防止を求めているが、**別のアプリ**が
    そのポートを使っている場合もある。前者はロックファイルで判定して
    ブラウザだけ開き、後者はここの候補で回避する。
    """
    retry = int(load()["server"]["port_retry"])
    base = port()
    return [base + i for i in range(retry + 1)]


# ---------------------------------------------------------------------------
# 監視
# ---------------------------------------------------------------------------
def health_poll_seconds() -> int:
    """ブラウザが生存確認を送る間隔(基盤仕様書 2.9)。"""
    return int(load()["monitoring"]["health_poll_seconds"])


def sync_poll_ms() -> int:
    """同期状態を画面が問い合わせる間隔。"""
    return int(load()["monitoring"]["sync_poll_ms"])


# ---------------------------------------------------------------------------
# ユーザー別ローカル領域 (基盤仕様書 2.7 / 4.6)
# ---------------------------------------------------------------------------
def local_root() -> Path:
    """``%LOCALAPPDATA%\\<local_dir_name>``(非Windowsは XDG 相当)。

    環境変数 ``CALENDAR_LOCAL_DIR`` で丸ごと差し替えられる。
    検証時に本番の領域を汚さずに試すための逃げ道。
    """
    override = os.environ.get("CALENDAR_LOCAL_DIR")
    if override and override.strip():
        return Path(override.strip())

    name = str(load()["local_dir_name"])
    base = os.environ.get("LOCALAPPDATA")
    if base:                                   # Windows
        return Path(base) / name
    # Linux/macOS。XDGの慣習に従う(開発機とCI用)
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / name
    return Path.home() / ".local" / "share" / name


# 領域の名前。実体の作成は ``ensure_local_dirs()`` で行う
LOCAL_SUBDIRS = ("runtime", "logs", "pycache", "cache", "work", "backup", "data")


def local_dir(name: str) -> Path:
    """ローカル領域の中のフォルダを1つ取る。"""
    if name not in LOCAL_SUBDIRS:
        raise ValueError(
            f"未知のローカル領域: {name!r} (使えるのは {', '.join(LOCAL_SUBDIRS)})")
    return local_root() / name


def ensure_local_dirs() -> Path:
    """ローカル領域を作る。既にあれば何もしない。"""
    root = local_root()
    for name in LOCAL_SUBDIRS:
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def describe() -> str:
    """診断用の1枚。``start.bat --check`` とログの先頭に出す(基盤仕様書 2.6)。"""
    lines = [
        f"アプリID      : {app_id()}",
        f"表示名        : {display_name()}",
        f"バージョン    : {version()}",
        f"監視レベル    : {monitor_level()}",
        f"設定ファイル  : {CONFIG_PATH}",
        f"アプリ本体    : {APP_ROOT}",
        f"ローカル領域  : {local_root()}",
        f"ポート        : {port()}  候補 {port_candidates()}",
    ]
    if _load_error:
        lines.append(f"【注意】{_load_error} — 既定値で動作しています")
    problem = version_problem()
    if problem:
        lines.append(f"【注意】{problem}")
    return "\n".join(lines)


if __name__ == "__main__":       # python -m calendar_app.app_config で確認できる
    print(describe())
