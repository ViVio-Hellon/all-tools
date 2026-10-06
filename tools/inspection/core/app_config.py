"""アプリ固有値の唯一の出どころ (`config/app.json` の読み取り)

基盤仕様書 5.2 が、アプリごとに決める値(アプリケーションID・表示名・使用ポート・
ローカル保存フォルダー名・監視レベル)を**複数ファイルへ直接書き散らさず、
設定または共通定数へ集約する**ことを求めている。このモジュールがその集約点。

【業務の設定との違い】
- `config/app.json`        … アプリという「入れ物」の値(ID・ポート・ローカル領域)
- `config/inspection.json` … 業務の既定値(点検表の置き場所・Excel の扱い)

起動基盤(`start_app.py` / `launch_guard.py` / `server.py`)はこちらだけを見る。
業務コードはこちらを見ない。これが基盤仕様書 2.5「起動処理とアプリ本体の分離」。

【なぜ JSON か】
`tomllib` は Python 3.11 以降にしか無い。依存を Flask と waitress の2つに
抑えるため JSON にした。JSON はコメントを書けないので、各キーの説明は
このモジュールに置く。

【ローカル領域】(基盤仕様書 2.7 / 4.6)
ログ・キャッシュ・実行時情報は、共有配置されうるアプリ本体側ではなく
利用者ごとのローカル領域へ置く。Windows は `%LOCALAPPDATA%`、
それ以外は `~/.local/share`(XDG)。

    %LOCALAPPDATA%\\InspectionSheetPrint\\
      runtime\\  … ロック(PID・ポート・トークン)・起動した Excel の記録
      logs\\     … 日ごとのログ
      pycache\\  … PYTHONPYCACHEPREFIX の向き先
      cache\\    … プレビュー画像
      work\\     … Excel 処理の一時ファイル・起動エラー画面
      backup\\   … 利用者設定の変更前の写し
      data\\     … 利用者設定(user_settings.json)。**消してはいけない**もの
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Optional

# このファイルの2つ上 = リポジトリのルート(= 基盤仕様書のいう ApplicationRoot)
APP_ROOT = Path(__file__).resolve().parent.parent

# 環境変数で差し替えられるようにしておくと、検証時に本番設定を書き換えずに試せる
CONFIG_PATH = Path(os.environ.get(
    "INSPECTION_APP_CONFIG", str(APP_ROOT / "config" / "app.json")))

# `config/app.json` が読めないときに使う値。
# 設定ファイルが壊れていても「起動はして、画面に理由を出す」ほうが、
# 起動そのものが失敗するより調査しやすい
_FALLBACK: dict[str, Any] = {
    "app_id": "nlm.inspection-sheet-print",
    "display_name": "点検表 選択・印刷",
    "version": "0.0.0",
    "monitor_level": 2,
    "local_dir_name": "InspectionSheetPrint",
    "server": {
        "host": "127.0.0.1",
        "port": 8733,
        "port_retry": 3,
    },
    "monitoring": {
        "health_poll_seconds": 15,
        "job_poll_ms": 500,
    },
}

_cache: Optional[dict[str, Any]] = None
_load_error: str = ""


def load(*, force: bool = False) -> dict[str, Any]:
    """`config/app.json` を読む。壊れていても例外を投げず既定値へ落とす。"""
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
    if isinstance(value, dict):
        return {k: _copy(v) for k, v in value.items()}
    return value


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """既定値に設定ファイルの値をかぶせる。キーが1つ足りなくても起動できる。"""
    result = _copy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = _copy(value)
    return result


# ------------------------------------------------------------------
# アプリの識別
# ------------------------------------------------------------------
def app_id() -> str:
    """起動確認APIで照合する識別子(基盤仕様書 2.3)。

    同じポートを別のアプリが使っている場合に、誤って「起動成功」と
    判定しないためのもの。HTTP が返るかどうかだけでは判別できない。
    """
    return str(load()["app_id"])


def display_name() -> str:
    return str(load()["display_name"])


# 版は「メジャー.マイナー.パッチ」の3つの数字だけ。
# 端末はフォルダごとコピーして配るので、いまどれが入っているかを
# 答えられる番号が要る。出どころは `config/app.json` の `version` ただ1つ
_VERSION_FORM = re.compile(r"^\d+\.\d+\.\d+$")
VERSION_PREFIX = "VER"


def version() -> str:
    return str(load()["version"])


def version_label() -> str:
    """帯のバッジに出す形。例 `VER1.0.0`。"""
    return f"{VERSION_PREFIX}{version()}"


_disk_version: tuple[float, str] = (0.0, "")


def version_on_disk() -> str:
    """いま `config/app.json` に書いてある版(読み込んだあとに入れ替えられたか)。

    アプリ本体は共有フォルダに置いて各ラインのPCから起動することがある。
    動いている途中で新しい版が置かれても、そのPCの動作中のアプリは古いまま
    なので、画面に「起動し直してください」を出すために見る。更新時刻が
    変わったときだけ読み直す(15秒ごとの問い合わせで共有フォルダを叩かない)。
    """
    global _disk_version
    try:
        mtime = CONFIG_PATH.stat().st_mtime
        if mtime != _disk_version[0]:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            _disk_version = (mtime, str(raw.get("version", "")))
        return _disk_version[1] or version()
    except (OSError, ValueError, AttributeError):
        return version()                            # 読めなければ「変わっていない」とみなす


def version_problem() -> str:
    text = version()
    if _VERSION_FORM.match(text):
        return ""
    return (f"版の書き方が違います: {text!r}。"
            "config/app.json の version は「1.0.0」のように数字3つで書いてください。")


def monitor_level() -> int:
    """基盤仕様書 3章の監視レベル。本アプリは2(印刷という数十秒の処理あり)。"""
    return int(load()["monitor_level"])


# ------------------------------------------------------------------
# サーバ
# ------------------------------------------------------------------
def host() -> str:
    """待ち受けアドレス。`127.0.0.1` 固定にすることで LAN から到達できなくなり、
    Windows のファイアウォール警告も出ない。"""
    return str(load()["server"]["host"])


def port() -> int:
    return int(load()["server"]["port"])


def port_candidates() -> list[int]:
    """使用中だったときに順に試すポート。

    同じアプリが動いている場合はロックファイルで判定してブラウザだけ開く
    (基盤仕様書 2.4)。**別のアプリ**がそのポートを使っている場合は、
    ここの候補で回避する。梱包資材総合ツール(8713～/8723～)とは重ならない
    番号にしてある。
    """
    base = port()
    retry = int(load()["server"]["port_retry"])
    return [base + i for i in range(retry + 1)]


# ------------------------------------------------------------------
# 監視
# ------------------------------------------------------------------
def health_poll_seconds() -> int:
    """ブラウザが生存確認を送る間隔(基盤仕様書 2.9)。"""
    return int(load()["monitoring"]["health_poll_seconds"])


def job_poll_ms() -> int:
    """印刷の進み具合を見に行く間隔。"""
    return int(load()["monitoring"]["job_poll_ms"])


# ------------------------------------------------------------------
# ユーザー別ローカル領域 (基盤仕様書 2.7 / 4.6)
# ------------------------------------------------------------------
def local_root() -> Path:
    """`%LOCALAPPDATA%\\<local_dir_name>`(非Windowsは XDG 相当)。

    環境変数 `INSPECTION_LOCAL_DIR` で丸ごと差し替えられる。
    検証時に本番の領域を汚さずに試すための逃げ道。
    """
    override = os.environ.get("INSPECTION_LOCAL_DIR")
    if override and override.strip():
        return Path(override.strip())
    name = str(load()["local_dir_name"])
    base = os.environ.get("LOCALAPPDATA")
    if base:                                   # Windows
        return Path(base) / name
    xdg = os.environ.get("XDG_DATA_HOME")      # Linux/macOS(開発機とCI用)
    if xdg:
        return Path(xdg) / name
    return Path.home() / ".local" / "share" / name


# デスクトップ版(Tauri)の窓が画面を読み込む宛先のホスト名。外枠
# (`src-tauri/src/main.rs` の `SCHEME`)と揃える。Windows の WebView2 は
# `http://app.localhost/`、ほかの OS は `app://localhost/` になる
BRIDGE_HOSTS = ("app.localhost", "localhost")


LOCAL_SUBDIRS = ("runtime", "logs", "pycache", "cache", "work", "backup", "data")


def local_dir(name: str) -> Path:
    if name not in LOCAL_SUBDIRS:
        raise ValueError(
            f"未知のローカル領域: {name!r} (使えるのは {', '.join(LOCAL_SUBDIRS)})")
    return local_root() / name


# ------------------------------------------------------------------
# Microsoft Store 版の Python
# ------------------------------------------------------------------
# Store 版の Python は %LOCALAPPDATA% / %TEMP% への書き込みを、自分専用の控え
#   %LOCALAPPDATA%\Packages\<パッケージ名>\LocalCache\Local\...
# へ振り替える(Python の公式文書「Known issues」)。Python 自身には元の場所に
# 見えるが、**ほかのプログラム(cscript・Excel・エクスプローラー・ブラウザ)からは
# 見えない**。ラインPCで Excel 連携が「パスが見つかりません」(0x4C)になった。
# Excel との受け渡しはファイルをやめて標準入出力にしたので業務は動く。ここは
# 「ログはどこにあるか」を正しく案内するためのもの。
_STORE_FAMILY = re.compile(
    r"\\WindowsApps\\(PythonSoftwareFoundation\.Python\.[\d.]+)_(?:[^\\]*__)?([a-z0-9]{13})(?:\\|$)",
    re.IGNORECASE)


def store_python_family(paths: Optional[list[str]] = None) -> str:
    """Store 版の Python ならパッケージのファミリ名。そうでなければ空文字。"""
    import sys
    candidates = paths if paths is not None else [
        sys.executable, getattr(sys, "_base_executable", ""), sys.prefix]
    for text in candidates:
        m = _STORE_FAMILY.search(text or "")
        if m:
            return f"{m.group(1)}_{m.group(2)}"
    return ""


def visible_local_root() -> Path:
    """エクスプローラーから見たローカル領域(案内用)。

    Store 版の Python では振り替え先、それ以外は `local_root()` と同じ。
    """
    family = store_python_family()
    base = os.environ.get("LOCALAPPDATA")
    if not family or not base or os.environ.get("INSPECTION_LOCAL_DIR"):
        return local_root()
    root = local_root()
    try:
        rel = root.relative_to(base)
    except ValueError:
        return root
    return Path(base) / "Packages" / family / "LocalCache" / "Local" / rel


def ensure_local_dirs() -> Path:
    root = local_root()
    for name in LOCAL_SUBDIRS:
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def describe() -> str:
    """診断用の1枚。`start.bat` とログの先頭に出す(基盤仕様書 2.6)。"""
    lines = [
        f"アプリID      : {app_id()}",
        f"表示名        : {display_name()}",
        f"バージョン    : {version()}",
        f"監視レベル    : {monitor_level()}",
        f"設定ファイル  : {CONFIG_PATH}",
        f"アプリ本体    : {APP_ROOT}",
        f"ローカル領域  : {local_root()}",
        *([f"【注意】Microsoft Store 版の Python です({store_python_family()})。",
           f"        ログなどはエクスプローラーでは次の場所に見えます: {visible_local_root()}"]
          if store_python_family() else []),
        f"ポート        : {port()}  候補 {port_candidates()}",
    ]
    if _load_error:
        lines.append(f"【注意】{_load_error} — 既定値で動作しています")
    problem = version_problem()
    if problem:
        lines.append(f"【注意】{problem}")
    return "\n".join(lines)


if __name__ == "__main__":       # python -m core.app_config で確認できる
    print(describe())
