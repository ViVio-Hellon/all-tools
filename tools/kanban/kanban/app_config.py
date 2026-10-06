"""アプリ固有値の唯一の出どころ (``config/app.json`` の読み取り)

社内「汎用Webアプリ作成 基盤仕様書」5.2 が、アプリごとに決める値
(アプリケーションID・表示名・使用ポート・ローカル保存フォルダー名・
監視レベル)を **複数ファイルへ直接書き散らさず、設定または共通定数へ
集約する**ことを求めている。このモジュールがその集約点になる。

【``config.py`` との違い】

* ``config.py``     … 業務の定数(テーブル名・Access の置き場所・ライン定義)
* ``app_config.py`` … アプリという「入れ物」の値(ID・ポート・ローカル領域)

起動基盤(``start_app.py`` / ``launch_guard.py`` / ``server.py``)は
こちらだけを見る。業務コードはこちらを見ない。この分離が基盤仕様書 2.5
「起動処理とアプリ本体の分離」にあたる。

【なぜ TOML ではなく JSON か】
``tomllib`` は Python 3.11 以降の標準ライブラリで、3.9/3.10 では追加
パッケージが要る。依存を Flask と waitress の2つに抑える方針を守るため
JSON にした。JSON はコメントを書けないので、各キーの説明はこのモジュール
に置いてある。

【ローカル領域】(基盤仕様書 2.7 / 4.6)
ログ・キャッシュ・実行時情報・SQLite は、共有配置されうるアプリ本体側では
なく利用者ごとのローカル領域へ置く。Windows は ``%LOCALAPPDATA%``、
それ以外は ``~/.local/share``(XDG)を使う。

    %LOCALAPPDATA%\\KanbanSystem\\
      runtime\\ … PID・ポート・状態(停止後は消してよい)
      logs\\    … launcher.log / guard.log / DebugLog
      pycache\\ … PYTHONPYCACHEPREFIX の向き先
      cache\\   … 再取得できる高速化用データ
      work\\    … 印刷用の一時 HTML など
      backup\\  … SQLite のバックアップ
      data\\    … kanban.sqlite3 / config.json など**消してはいけない**もの

``data`` は基盤仕様書の一覧には無い追加。仕様書 2.7 の目的は「アプリ本体と、
実行中に変化するファイルを分ける」ことであり、ローカル SQLite と利用者設定
はまさにそれにあたる。``cache``/``work`` と違って消せないので、独立した
名前にしている。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

# このファイルの2つ上 = リポジトリのルート(= 基盤仕様書のいう ApplicationRoot)
APP_ROOT = Path(__file__).resolve().parent.parent

# アプリ固有値の置き場所。環境変数で差し替えられるようにしておくと、
# 検証時に本番設定を書き換えずに試せる
CONFIG_PATH = Path(
    os.environ.get("KANBAN_APP_CONFIG", str(APP_ROOT / "config" / "app.json"))
)

# モードの定義は業務側(``config.py``)が持つ。ここはポートを引くために
# 名前を借りるだけで、モードの定義を二重に持たない
from . import config as _business_config  # noqa: E402  (定数より先に読む必要がある)

MODE_KEYS = _business_config.ALL_MODES

# ``config/app.json`` が読めないときに使う値。
# 設定ファイルが壊れていても「起動はして、画面に理由を出す」ほうが、
# 起動そのものが失敗するより調査しやすい(基盤仕様書 ステップ5)。
_FALLBACK: dict[str, Any] = {
    "app_id": "nlm.kanban-system",
    "display_name": "資材発注看板システム",
    "version": "0.0.0",
    "monitor_level": 2,
    "local_dir_name": "KanbanSystem",
    "server": {
        "host": "127.0.0.1",
        "port_retry": 3,
        "roles": {
            _business_config.MODE_SITE: {"port": 8741},
            _business_config.MODE_WAREHOUSE: {"port": 8751},
            _business_config.MODE_WAREHOUSE_VIEW: {"port": 8761},
        },
    },
    "monitoring": {
        "health_poll_seconds": 15,
        "job_poll_ms": 300,
    },
}

# 読み込み結果のキャッシュ。設定は起動中に変わらない
_cache: dict[str, Any] | None = None
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

    設定ファイルに書き忘れたキーがあっても既定値で埋まるので、キーが1つ
    足りないだけで起動できなくなることがない。
    """
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
    """起動確認 API で照合する識別子(基盤仕様書 2.3)。

    同じポートを別のアプリが使っている場合に、誤って「起動成功」と判定
    しないためのもの。単に HTTP が返るかどうかでは判別できない。
    """
    return str(load()["app_id"])


def display_name() -> str:
    return str(load()["display_name"])


# 版の書き方。**メジャー.マイナー.パッチ の3つの数字だけ**。
#
# 現場の端末はフォルダごとコピーして配るので、「いまどれが入っているか」を
# 聞かれたときに答えられる番号が要る。番号は ``config/app.json`` の
# ``version`` ただ1つが出どころで、画面(帯のバッジ)・``/api/health``・
# 設定画面はすべてここを読む。
#
#   メジャー … 現場の手順が変わる(操作を覚え直す必要がある)
#   マイナー … できることが増える。手順はそのまま
#   パッチ   … 直しただけ。見た目も手順も変わらない
_VERSION_FORM = re.compile(r"^\d+\.\d+\.\d+$")

#: 画面に出すときの前置き。「3.0.0」だけだと何の番号か分からない
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
    return (
        f"版の書き方が違います: {text!r}。"
        "config/app.json の version は「3.0.0」のように数字3つで書いてください。"
    )


def monitor_level() -> int:
    """基盤仕様書 3章の監視レベル。

    本アプリは 2(長時間処理あり)。Access への取り込み・書き戻しが
    数十秒かかることがあり、その間ブラウザを閉じても処理は続く。
    自動実行(定期取り込み)は持つが、ブラウザが無い状態での常駐運転は
    しないのでレベル3ではない。
    """
    return int(load()["monitor_level"])


# ------------------------------------------------------------------
# サーバ
# ------------------------------------------------------------------
def host() -> str:
    """待ち受けアドレス。

    ``127.0.0.1`` 固定にすることで LAN から到達できなくなり、Windows の
    ファイアウォール警告も出ない。
    """
    return str(load()["server"]["host"])


#: デスクトップ版(Rust/Tauri)の窓が読む宛先の名前。**TCP を通らない**
#: (外枠が受けて Python の標準入力へ渡す。``bridge.py``)。
#: 外枠の ``SCHEME``(``src-tauri/src/main.rs``)と揃える
BRIDGE_HOSTS = ("app.localhost", "localhost")


#: ブラウザ版(予備)で、この画面を**枠の中に出してよい**相手。統合ツールの
#: 大きなタブ(入口のページ。この PC の 127.0.0.1 の別の番号)から出すため。
#: ほかのサイトからは出させない(クリックの乗っ取り対策。`X-Frame-Options` の代わり)。
#: デスクトップ版(統合ツールの窓)では、外枠がこの見出しを外して渡す
FRAME_ANCESTORS = "frame-ancestors 'self' http://127.0.0.1:* http://localhost:*"


def _mode_conf(mode: str) -> dict[str, Any]:
    table = load()["server"]["roles"]
    if mode in table:
        return table[mode]
    raise ValueError(f"未知のモード: {mode!r} (使えるのは {', '.join(MODE_KEYS)})")


def port(mode: str = _business_config.MODE_SITE) -> int:
    return int(_mode_conf(mode)["port"])


def port_candidates(mode: str = _business_config.MODE_SITE) -> list[int]:
    """使用中だったときに順に試すポート。

    基盤仕様書 2.4 は多重起動の防止を求めているが、**別のアプリ**がその
    ポートを使っている場合もある。前者はロックファイルで判定してブラウザ
    だけ開き、後者はここの候補で回避する。
    """
    base = port(mode)
    retry = int(load()["server"]["port_retry"])
    return [base + i for i in range(retry + 1)]


def port_range_conflicts() -> list[str]:
    """モードごとの候補ポートが重なっていないかを調べる。

    重なっていると事故になる。現場が繰り上がって倉庫のポートを使って
    しまうと、倉庫モードを起動できない。利用者からは「倉庫モードが開けない」
    としか見えず原因が分からない。

    ``port`` と ``port_retry`` は設定ファイルで変えられるので、変更のたびに
    機械が検査できるようにここに置く(テストが呼ぶ)。
    """
    problems: list[str] = []
    ranges = {mode: set(port_candidates(mode)) for mode in MODE_KEYS}
    checked: set[frozenset[str]] = set()
    for a in MODE_KEYS:
        for b in MODE_KEYS:
            if a == b or frozenset((a, b)) in checked:
                continue
            checked.add(frozenset((a, b)))
            overlap = sorted(ranges[a] & ranges[b])
            if overlap:
                problems.append(
                    f"{a} と {b} の候補ポートが重なっています: {overlap}"
                    f" ({a}={sorted(ranges[a])} / {b}={sorted(ranges[b])})"
                )
    return problems


# ------------------------------------------------------------------
# 監視
# ------------------------------------------------------------------
def health_poll_seconds() -> int:
    """ブラウザが生存確認を送る間隔(基盤仕様書 2.9)。"""
    return int(load()["monitoring"]["health_poll_seconds"])


def job_poll_ms() -> int:
    """取り込み進捗のポーリング間隔。"""
    return int(load()["monitoring"]["job_poll_ms"])


# ------------------------------------------------------------------
# ユーザー別ローカル領域 (基盤仕様書 2.7 / 4.6)
# ------------------------------------------------------------------
def local_root() -> Path:
    """``%LOCALAPPDATA%\\<local_dir_name>``(非 Windows は XDG 相当)。

    環境変数 ``KANBAN_LOCAL_DIR`` で丸ごと差し替えられる。検証時に本番の
    領域を汚さずに試すための逃げ道。
    """
    override = os.environ.get("KANBAN_LOCAL_DIR")
    if override and override.strip():
        return Path(override.strip())

    name = str(load()["local_dir_name"])
    base = os.environ.get("LOCALAPPDATA")
    if base:  # Windows
        return Path(base) / name
    # Linux/macOS。XDG の慣習に従う(開発機と CI 用)
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / name
    return Path.home() / ".local" / "share" / name


#: 領域の名前。実体の作成は :func:`ensure_local_dirs` で行う
LOCAL_SUBDIRS = ("runtime", "logs", "pycache", "cache", "work", "backup", "data", "export")


def local_dir(name: str) -> Path:
    """ローカル領域の中のフォルダを1つ取る。"""
    if name not in LOCAL_SUBDIRS:
        raise ValueError(
            f"未知のローカル領域: {name!r} (使えるのは {', '.join(LOCAL_SUBDIRS)})"
        )
    return local_root() / name


def ensure_local_dirs() -> Path:
    """ローカル領域を作る。既にあれば何もしない。"""
    root = local_root()
    for name in LOCAL_SUBDIRS:
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def describe() -> str:
    """診断用の1枚。``start.bat`` とログの先頭に出す(基盤仕様書 2.6)。"""
    lines = [
        f"アプリID      : {app_id()}",
        f"表示名        : {display_name()}",
        f"バージョン    : {version()}",
        f"監視レベル    : {monitor_level()}",
        f"設定ファイル  : {CONFIG_PATH}",
        f"アプリ本体    : {APP_ROOT}",
        f"ローカル領域  : {local_root()}",
    ]
    for mode in MODE_KEYS:
        label = _business_config.mode_display_name(mode)
        lines.append(f"ポート({label}): {port(mode)}  候補 {port_candidates(mode)}")
    if _load_error:
        lines.append(f"【注意】{_load_error} — 既定値で動作しています")
    for problem in port_range_conflicts():
        lines.append(f"【注意】{problem}")
    return "\n".join(lines)


if __name__ == "__main__":  # python -m kanban.app_config で確認できる
    print(describe())
