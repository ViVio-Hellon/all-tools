"""アプリケーション共通の定数と、データ保存先の解決。

VBA 版の modMain / modConst に相当する。
Access のテーブル名・列名はそのまま SQLite 側でも使用し、
移行前後でデータの意味が変わらないようにしている。
"""

from __future__ import annotations

import os
from pathlib import Path

from .dbkit import source_db

# ---------------------------------------------------------------------------
# テーブル名 (Access 側と同一)
# ---------------------------------------------------------------------------
#: 休み・連絡の保存先 (VBA: TABLE_DATA)
TABLE_DATA = "休み管理"
#: 削除履歴 (VBA: TABLE_DEL_HISTORY)
TABLE_DEL_HISTORY = "削除履歴"
#: 作業者名簿 (VBA: TABLE_MEMBER。マスタ DB 側にある読み取り専用テーブル)
TABLE_MEMBER = "班員名簿"

#: 取り込み元のファイル名 (VBA: DB_FILE_DATA / DB_FILE_MASTER)。
#: **拡張子違いも同じものとして扱う** ので、.sqlite3 / .db のどちらでもよい
#: (``dbkit.source_db.find``)。
SOURCE_FILE_DATA = "連絡帳.sqlite3"
SOURCE_FILE_MASTER = "梱包資材マスタ.sqlite3"

#: 変換前の Access ファイル名。**変換ツール(tools/convert_accdb.py)だけ**
#: が使う。アプリ本体はもう .accdb を読まない
ACCESS_FILE_DATA = "連絡帳.accdb"
ACCESS_FILE_MASTER = "梱包資材マスタ.accdb"

# ---------------------------------------------------------------------------
# 区分 (休み管理.区分 に入る値)
# ---------------------------------------------------------------------------
KUBUN_REST = "休み"
KUBUN_OTHER = "その他"

#: 3 交替勤務の班。ここに該当しない班(昼勤・日勤など)は直の確認を行わない。
SHIFT_GROUPS = ("A", "B", "C", "D")

#: 繋ぎ担当者が未選択だった場合に保存する文字列
UNREGISTERED = "未登録"

# ---------------------------------------------------------------------------
# ライン設定
# ---------------------------------------------------------------------------
#: 選択可能な全ライン名 (VBA: GetAllLineNames)
ALL_LINE_NAMES = (
    "コイル",
    "LVC",
    "HVC",
    "機側",
    "L-1",
    "AIM",
    "NS1",
    "大板小板",
    "作業長",
)

#: 作業者選択画面でのライン表示順 (VBA: GetLineOrder)
LINE_ORDER = ("作業長", "コイル", "HVC", "機側", "LVC", "L-1")

#: 同一人物が兼務するため 1 グループとして表示するライン (VBA: BuildLineGroupsForData)
MERGED_LINE_GROUPS = (("LVC", "L-1"),)

#: 全ラインの休みを閲覧できる特別なライン設定
LINE_SUPERVISOR = "作業長"
#: 他ラインとは分離して表示するライン
LINE_COIL = "コイル"

# ---------------------------------------------------------------------------
# 表示書式
# ---------------------------------------------------------------------------
DATE_KEY_FORMAT = "%Y/%m/%d"
DATETIME_FORMAT = "%Y/%m/%d %H:%M:%S"

FONT_NAME = "Meiryo"
WEEKDAY_LABELS = ("日", "月", "火", "水", "木", "金", "土")

APP_TITLE = "ライン管理カレンダー"


# ---------------------------------------------------------------------------
# 管理者パスワード (設定を守る関門の既定値)
# ---------------------------------------------------------------------------
# **一度も変えていない端末で通る値。** 端末ごとに変えられるようにしてあり
# (``admin_password.change``)、変えた端末はそちらが優先される。
#
# ここに平文があることに意味を持たせないでください ── これは
# 「誤って押されない」ためのUIガードの初期値で、アクセス制御ではありません
# (``calendar_app/admin_password.py`` の説明)。配布のしかたを変えたい
# ときは環境変数で上書きできます。
ADMIN_PASSWORD = os.environ.get("CALENDAR_ADMIN_PASSWORD", "nisk")


# ---------------------------------------------------------------------------
# 取り込み元の置き場所 (参照パス)
# ---------------------------------------------------------------------------
# **フォルダで持ち、ファイルはその中から探す。** ファイルのフルパスを
# 設定させると、上流がファイル名を変えただけで動かなくなり、現場からは
# 「急に読めなくなった」としか見えない。フォルダなら名前が変わっても
# 拾えるし、保存用とマスタが同じ共有に並んでいる普通の置き方にも合う。
#
# 実際に使うパスは ``data_db_dir()`` / ``master_db_dir()`` で取る。
# 設定が無いときの既定は ``default_data_db_dir()`` などで取る(環境変数)。
#
# 配る前に決めておきたいときは、設定画面の「配布設定」で書き出す
# (``calendar_app/distribution.py``)。配った先では起動したときに
# **端末の設定へ読み込む**ので、ここを通らない。


def default_data_db_dir() -> str:
    """端末で決めていないときの保存用DBのフォルダ(打たれたままの文字列)。

    環境変数が立っていればそれ ── PC ごとに違う置き方をしたいときの
    逃げ道として残してある。
    """
    return os.environ.get("CALENDAR_DATA_DB_DIR", "").strip()


def default_master_db_dir() -> str:
    """端末で決めていないときのマスタDBのフォルダ。"""
    return os.environ.get("CALENDAR_MASTER_DB_DIR", "").strip()


#: 取り込み元として認める拡張子。出どころは ``dbkit.source_db`` ただ1つ
SOURCE_SUFFIXES = source_db.SUFFIXES


def resolve_dir(text: str) -> Path:
    r"""打たれた道を、**実際に見に行く道**にする。

    2通りの書き方を受ける。

        絶対  ``\\サーバ\共有\…`` / ``C:\data\…`` / ``/mnt/share/…``
              打たれたまま使う
        相対  ``data\src`` / ``..\共有`` / ``src``
              **アプリのフォルダから**たどる

    相対を「いまの作業フォルダ」から見ないのが要点。作業フォルダは
    どこから起動したかで変わるので、同じ設定でも端末ごとに違う場所を
    指すことになる。アプリのフォルダなら、フォルダごとコピーして配る
    運用でも、写しの中の同じ場所を指し続ける。

    (``~`` は利用者のフォルダに開く。共有に届かない端末で、手元の
     写しを指すのに使える)
    """
    trimmed = (text or "").strip().strip('"')
    if not trimmed:
        raise ValueError("パスが空です")
    path = Path(trimmed).expanduser()
    if path.is_absolute() or trimmed.startswith("\\\\"):
        return path
    # このファイルの1つ上 = アプリのフォルダ
    return (Path(__file__).resolve().parent.parent / path).resolve()


def _configured_dir(key: str, fallback: str) -> Path:
    """設定画面の値を優先し、無ければ既定。

    ``settings`` を遅延 import するのは、``config`` を先に読むモジュールとの
    循環参照を避けるため。
    """
    from . import settings

    configured = settings.get(key, "")
    if isinstance(configured, str) and configured.strip():
        try:
            return resolve_dir(configured)
        except ValueError:
            pass
    return resolve_dir(fallback) if fallback else Path()


def data_db_dir() -> Path:
    """保存用DB (連絡帳.sqlite3) が置いてあるフォルダ。"""
    from . import settings

    return _configured_dir(settings.KEY_DATA_DB_DIR, default_data_db_dir())


def master_db_dir() -> Path:
    """マスタDB (梱包資材マスタ.sqlite3) が置いてあるフォルダ。

    保存用と同じ共有に並んでいることも多いので、既定は分けずに
    「設定されていなければ保存用と同じ場所を見る」にしてある
    (``sources.find_master_db``)。
    """
    from . import settings

    return _configured_dir(settings.KEY_MASTER_DB_DIR, default_master_db_dir())


def data_db_dir_text() -> str:
    """いま効いている保存用DBのフォルダを、**打たれたままの文字で**。

    端末で保存したものがあればそれ、無ければ既定(環境変数)。
    端末一覧に載せるのはこちら ── 保存したものだけを載せると、既定で
    動いている端末が「未設定」に見える。
    """
    from . import settings

    return settings.data_db_dir_setting() or default_data_db_dir()


def master_db_dir_text() -> str:
    """いま効いているマスタDBのフォルダを、打たれたままの文字で。"""
    from . import settings

    return settings.master_db_dir_setting() or default_master_db_dir()


# ---------------------------------------------------------------------------
# 保存先ディレクトリ
# ---------------------------------------------------------------------------
def app_home() -> Path:
    """アプリのデータディレクトリを返す。

    既定は**利用者ごとのローカル領域**の ``data`` (基盤仕様書 2.7 / 4.6)。
    Windows では ``%LOCALAPPDATA%\\LineCalendar\\data``。
    アプリ本体は共有フォルダに置かれることがあるため、実行中に変化する
    ファイル(DB・設定・ログ)は本体側へ置かない。

    環境変数 ``CALENDAR_HOME`` で丸ごと差し替え可能 (テスト向け)。
    """
    env = os.environ.get("CALENDAR_HOME")
    if env:
        base = Path(env).expanduser()
    else:
        from . import app_config

        base = app_config.local_dir("data")
    base.mkdir(parents=True, exist_ok=True)
    return base


def sqlite_path() -> Path:
    """取り込み先の SQLite ファイルパス。"""
    return app_home() / "calendar.db"


def settings_path() -> Path:
    """PC ごとの設定ファイル (VBA ではレジストリに保存していたもの)。"""
    return app_home() / "settings.json"


def log_dir() -> Path:
    """デバッグログの出力先 (VBA: DebugLog の出力フォルダ)。

    実際の出力は ``logging_utils`` がローカル領域の ``logs`` へ行う。
    ここは ``CALENDAR_HOME`` を立てたときに追随するための入口。
    """
    d = app_home() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sync_dir() -> Path:
    """Access へ反映するための出力先。"""
    d = app_home() / "sync"
    d.mkdir(parents=True, exist_ok=True)
    return d
