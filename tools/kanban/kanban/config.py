"""設定値とライン定義。

VBA 側の標準モジュールにあった定数群 (``PATH_AIM_参照`` / ``TARGET_DB_NAME`` /
``GetLineMaster`` など) を Python 側で 1 箇所に集約する。

パス等の環境依存値は JSON 設定ファイルで上書きできる。既定の探索先は

* Windows: ``%APPDATA%\\KanbanSystem\\config.json``
* それ以外: ``~/.config/kanban_system/config.json``

環境変数 ``KANBAN_CONFIG`` を指定するとそのファイルを優先して読む。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

APP_NAME = "KanbanSystem"

# --- 共有パス ------------------------------------------------------------------
#: 本番の参照用ファイル置き場。**接続先を設定していない端末はここを見ます。**
#:
#: VBA の定数を写した当初は ``\\nlmsrvngy03`` でしたが、本番は
#: ``\\nlmfangyshrd`` です(2026/09 に確認)。説明書のパス(下の
#: ``PATH_MANUAL``)は元から ``\\nlmfangyshrd`` を指していました。
#: 配布設定(``配布設定\\config.json``)で接続先を入れて配れば、ここは使われません
PATH_AIM_REFERENCE = r"\\nlmfangyshrd\各課共有\0130_日軽稲沢\梱包課\AIM\【■】_参照用ファイル"
TARGET_DB_NAME = "看板マスタ.accdb"
"""旧・Access ファイル名(移行用)。"""

TARGET_SHARED_DB_NAME = "看板マスタ.sqlite3"
"""共有 SQLite のファイル名。正式なデータの置き場所。"""

#: アクセス権限(誰がどのモードを使えるか)を持つファイル。**梱包資材マスタ**の中の
#: ``アクセス権限`` 表を、python-web-tools(梱包資材総合ツール)と共有する
#: (:mod:`kanban.access_control`)。既定は共有DBと同じフォルダのこの名前
ACCESS_DB_NAME = "梱包資材マスタ.sqlite3"

#: 看板履歴(集計のための出来事の記録)を置くファイル。看板マスタとは**別のファイル**
#: (:class:`kanban.db.history.HistoryDb`)。既定は共有DBと同じフォルダのこの名前
HISTORY_DB_NAME = "看板履歴.sqlite3"
PATH_MANUAL = (
    r"\\nlmfangyshrd\各課共有\0130_日軽稲沢\梱包課\AIM\【■】_参照用ファイル"
    r"\説明書\看板システム説明書.html"
)
PATH_DEBUG_PRINT = r"\\nlmsrvngy03\各課共有\0130_日軽稲沢\梱包課\梱包課共有\日報管理\DebugPrint"

# --- テーブル名 --------------------------------------------------------------
TABLE_STATE = "Form状態管理"
KANBAN_TABLE_PREFIX = "看板_"

# --- 看板テーブルの列名(Access 側の実列名) ----------------------------------
COL_KEY = "管理番号"
COL_MATERIAL = "資材"
COL_SIZE = "サイズ"
COL_WANT = "欲"
COL_UNWANT = "不"
COL_ORDERED_AT = "更新日"
COL_SHIPPED = "発送"
COL_CONFIRMED_AT = "倉庫確認日時"
COL_PERMANENT = "常設品"
COL_HOLD = "保留"
COL_HOLD_AT = "注文中日時"
"""「注文中」ボタン(旧・保留ボタン)を立てた日時。

列名・用途とも VBA 側の更新に合わせている。以前は自由入力の理由
テキストだったが、現在は自動記録される日時(他の ``_at`` 列と同じ
``now_string()`` 形式)。Access 側の列名も ``理由`` から ``注文中日時``
へ変わっている。
"""

#: 看板テーブルに必要な列(これが欠けているテーブルは取り込み対象外)
#: 保留/注文中日時は VBA の ``C_保留 > 0`` と同じく任意列として扱う
#: (無いテーブルがあっても取り込み自体は失敗させない。常設品と同じ方針)。
REQUIRED_COLUMNS = (
    COL_KEY,
    COL_MATERIAL,
    COL_SIZE,
    COL_WANT,
    COL_UNWANT,
    COL_ORDERED_AT,
    COL_SHIPPED,
    COL_CONFIRMED_AT,
)

#: 状態を表すマーク
MARK_ON = "〇"
MARK_OFF = ""
#: 非常設品のマーク
MARK_NON_PERMANENT = "×"

#: フォーム状態管理テーブルの状態値
STATE_OPEN = "開"
STATE_CLOSED = "閉"
#: 倉庫モードが ``Form状態管理`` に登録するライン名
WAREHOUSE_LINE_NAME = "倉庫"

#: ``Form状態管理`` の列名(``ライン名`` と ``状態`` 以外)。
#:
#: VBA 版は ``状態`` しか書いていませんでしたが、``更新日時`` を書くことで
#: 「印は立っているが、もう誰も打っていない」を見分けられます ── 異常終了で
#: 開いたままになった印を、いつまでも信じないためです。
COL_STATE_LINE = "ライン名"
COL_STATE_VALUE = "状態"
COL_STATE_UPDATED_AT = "更新日時"
COL_STATE_HOST = "ホスト名"

# --- 画面(フォーム)の種別 --------------------------------------------------
# 現場と倉庫は別々のフォームで、1 つのプロセスではどちらか一方しか開かない。
# VBA の MaterialForm / WarehouseForm / WarehouseViewForm と一対一で対応する。
MODE_SITE = "site"
"""現場モード(MaterialForm 相当)。担当ラインの発注を行う。"""

MODE_WAREHOUSE = "warehouse"
"""倉庫モード(WarehouseForm 相当)。全ラインの発送を行う。"""

MODE_WAREHOUSE_VIEW = "view"
"""倉庫参照モード(WarehouseViewForm 相当)。表示のみ。"""

ALL_MODES = (MODE_SITE, MODE_WAREHOUSE, MODE_WAREHOUSE_VIEW)

#: 設定画面・断り書きに出す表示名
MODE_DISPLAY_NAMES = {
    MODE_SITE: "現場モード",
    MODE_WAREHOUSE: "倉庫モード",
    MODE_WAREHOUSE_VIEW: "倉庫参照モード(操作不可)",
}


def mode_display_name(mode: str) -> str:
    """モード種別の表示名(未知の値はそのまま返す)。"""
    return MODE_DISPLAY_NAMES.get(mode, mode)


# --- 管理者パスワード ---------------------------------------------------------
# モード変更・接続パス変更を誰でもできないようにするための共通パスワード。
# 平文は保存せず、ソルト付きハッシュ(PBKDF2-HMAC-SHA256)だけを
# config.json に保存する(``admin_password_hash``)。
_PBKDF2_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    """管理者パスワードをソルト付きでハッシュ化する。

    戻り値は ``"<ソルトhex>$<ハッシュhex>"``。同じパスワードから同じ
    ハッシュにならないよう、呼び出しごとに新しいソルトを生成する。
    """
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ITERATIONS
    ).hex()
    return f"{salt}${digest}"


def verify_password(password: str, stored_hash: str) -> bool:
    """``hash_password`` で作ったハッシュと平文パスワードを比較する。"""
    if not stored_hash or "$" not in stored_hash:
        return False
    salt_hex, digest_hex = stored_hash.split("$", 1)
    try:
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS
    ).hex()
    return secrets.compare_digest(candidate, digest_hex)


@dataclass(frozen=True)
class LineInfo:
    """1 ラインの定義。VBA の ``GetLineMaster`` の 1 行に対応する。"""

    code: str
    display_name: str
    warehouse_target: bool = True
    """倉庫モードのタブに表示するか(VBA の ``倉庫対象``)。"""

    shipping_disabled: bool = True
    """現場モードで発送ボタンを操作させないか(VBA の ``発送無効``)。"""


#: 対応ラインのマスタ定義。ライン追加・削除はここに 1 行足すだけでよい。
LINE_MASTER: tuple[LineInfo, ...] = (
    LineInfo("LVC", "LVC"),
    LineInfo("HVC", "HVC"),
    LineInfo("LS", "機側"),
    LineInfo("L1", "L-1"),
    LineInfo("AIM", "AIM"),
    LineInfo("NS1", "NS1", warehouse_target=False),
    LineInfo("コイル", "コイル"),
    LineInfo("大板小板", "大板小板"),
)

_LINE_BY_CODE = {info.code: info for info in LINE_MASTER}


def line_info(code: str) -> LineInfo | None:
    """ライン定義を返す(未登録なら None)。"""
    return _LINE_BY_CODE.get(code)


def is_supported_line(code: str) -> bool:
    """対応ラインかどうか(VBA の ``IsSupportedLine``)。"""
    return code in _LINE_BY_CODE


def is_shipping_disabled_line(code: str) -> bool:
    """発送ボタンを無効化するラインか(VBA の ``IsShippingDisabledLine``)。"""
    info = _LINE_BY_CODE.get(code)
    return bool(info and info.shipping_disabled)


def all_line_codes() -> list[str]:
    """全ラインコード(VBA の ``GetAllLineCodes``)。"""
    return [info.code for info in LINE_MASTER]


def warehouse_line_codes() -> list[str]:
    """倉庫モードの表示対象ラインコード(VBA の ``GetWarehouseLineCodes``)。"""
    return [info.code for info in LINE_MASTER if info.warehouse_target]


def display_name(code: str) -> str:
    """画面表示用のライン名(``LS`` -> ``機側`` / ``L1`` -> ``L-1``)。"""
    info = _LINE_BY_CODE.get(code)
    return info.display_name if info else code


def table_name_for(code: str) -> str:
    """ラインコードに対応する Access のテーブル名。"""
    return f"{KANBAN_TABLE_PREFIX}{code}"


def _default_config_path() -> Path:
    override = os.environ.get("KANBAN_CONFIG")
    if override:
        return Path(override)
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / APP_NAME / "config.json"
    return Path.home() / ".config" / "kanban_system" / "config.json"


def _default_data_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / APP_NAME
    return Path.home() / ".local" / "share" / "kanban_system"


@dataclass
class Config:
    """アプリケーション全体の設定。"""

    shared_db_path: str = ""
    """取り込み元 / 書き戻し先の**共有 SQLite** ファイル。

    正式なデータの置き場所。全端末がここを読み書きする。
    """

    access_db_path: str = ""
    """アクセス権限を持つ**梱包資材マスタ**(sqlite3)。空なら共有DBと同じフォルダの
    ``梱包資材マスタ.sqlite3``(:meth:`resolved_access_db_path`)。"""

    history_db_path: str = ""
    """看板履歴を書く・読む ``看板履歴.sqlite3``(全端末で共有)。空なら共有DBと同じフォルダ
    (:meth:`resolved_history_db_path`)。"""

    csv_dir: str = ""
    """CSV を書き出すフォルダ。空ならこの端末のローカル領域の ``export``
    (:meth:`resolved_csv_dir`)。"""

    accdb_path: str = ""
    """旧・Access ファイル(移行用に残してある)。

    ``shared_db_path`` が未設定のときだけ、変換元を探す手がかりとして使う。
    運用では参照しない ── 読み書きの相手は共有 SQLite ただ 1 つ。
    """

    sqlite_path: str = ""
    """運用中に読み書きする SQLite ファイル(複数ラインで共有する)。"""

    line: str = ""
    """このパソコンが担当するラインコード(現場モードで使用)。"""

    mode: str = MODE_SITE
    """CLI ``--mode`` で明示指定されたときだけ使う、1 回限りの上書き値。

    通常の運用では、この端末が起動する画面(``site``/``warehouse``/``view``)は
    ``config.json`` ではなく SQLite(``Store.get_device_mode``)に保存される
    (初回は現場モードを既定として書き、以後は設定画面の「この端末」から)。
    ``mode`` フィールドはあくまで CLI 引数を一時的に運ぶためのもので、
    SQLite に保存された値を上書きすることはない。
    """

    admin_password_hash: str = ""
    """モード変更・接続パス変更を保護する管理者パスワードのハッシュ。

    平文は保存しない(``config.hash_password``/``verify_password`` 参照)。
    未設定(空文字)の場合、設定画面で保護された操作を初めて行おうとした
    ときにその場でパスワードを設定させる。
    """

    log_dir: str = ""
    manual_path: str = PATH_MANUAL

    # --- 同期 -----------------------------------------------------------
    # ローカル SQLite は「この端末専用の作業用の写し + 送信待ちの一時置き場」
    # であり、正式なデータは常に Access 側にある。そのため
    #   起動時   : Access -> SQLite (import_on_startup)
    #   運用中   : SQLite -> Access (export_interval_sec、操作直後にも実行)
    #   運用中   : Access -> SQLite (import_interval_sec、他端末の変更を取り込む)
    # の 3 つを回す。sqlite_path は既定でこの端末のローカルディスクを指す
    # ため、SQLite 自体を複数端末で共有するわけではない
    # (共有フォルダへ置く構成は docs/運用手順.md の「代替構成」を参照)。
    import_on_startup: bool = True
    """起動時に Access -> SQLite の取り込みを行うか。"""

    export_interval_sec: int = 60
    """SQLite -> Access の書き戻し間隔(秒)。0 で自動書き戻しを止める。"""

    mistake_minutes: int = 5
    """看板集計で**押し間違いとみなす時間**(分)。注文中をこれより早く外した・発送の無いラインで
    赤をこれより早く消したものは数えない(:mod:`kanban.presenters.stats`)。0 なら時間では除かない。"""

    import_interval_sec: int = 30
    """Access -> SQLite の再取り込み間隔(秒)。0 で自動再取り込みを止める。

    他ライン・倉庫が Access へ書き戻した内容をこの端末へ反映するための
    ポーリング。ローカル SQLite は他端末と共有していないため、これが無いと
    他端末の変更が画面に反映されるのはこの端末を再起動したときだけになる。
    """


    # --- 画面 -----------------------------------------------------------
    refresh_interval_sec: int = 5
    """ローカル SQLite の変化を画面へ反映する間隔(秒)。0 で自動更新なし。

    この端末自身の操作、および ``import_interval_sec`` による Access からの
    再取り込みの両方をこの間隔で拾って画面へ反映する。"""

    frames_per_row: int = 4
    """資材フレームを横に並べる数(VBA の ``framesPerRow``)。"""

    auto_print: bool = False
    """印刷用 HTML を開いた直後に印刷ダイアログを出すか。"""

    # --- 排他制御 -------------------------------------------------------
    busy_timeout_ms: int = 8000
    max_retry: int = 3
    """ロック競合時の再試行回数(VBA の ``MAX_RETRY``)。"""

    host_name: str = field(default_factory=lambda: socket.gethostname())

    # 読み込み元の設定ファイル(保存時に使用)
    source_path: str = ""

    def resolved_shared_db_path(self) -> str:
        """実際に読み書きする共有 SQLite のパス。"""
        if self.shared_db_path:
            return self.shared_db_path
        return str(Path(PATH_AIM_REFERENCE) / TARGET_SHARED_DB_NAME)

    def resolved_access_db_path(self) -> str:
        """アクセス権限を読む梱包資材マスタのパス。

        **既定は共有DBと同じフォルダ。** 本番では python-web-tools の既定の置き場所
        (``【■】_参照用ファイル``)と同じになり、試験環境(共有DBを
        ``Test環境`` に写した)では、そこに写した梱包資材マスタを見る ──
        本番の権限を試験で書き換えないように。
        """
        if self.access_db_path:
            return self.access_db_path
        return str(Path(self.resolved_shared_db_path()).parent / ACCESS_DB_NAME)

    def resolved_history_db_path(self) -> str:
        """看板履歴のファイル。**既定は共有DBと同じフォルダの** ``看板履歴.sqlite3``。"""
        if self.history_db_path:
            return self.history_db_path
        return str(Path(self.resolved_shared_db_path()).parent / HISTORY_DB_NAME)

    def resolved_csv_dir(self) -> str:
        """CSV の書き出し先。既定はこの端末のローカル領域の ``export``。"""
        if self.csv_dir:
            return self.csv_dir
        from . import app_config

        return str(app_config.local_dir("export"))

    def resolved_accdb_path(self) -> str:
        """旧 Access ファイルのパス(変換のときだけ使う)。"""
        if self.accdb_path:
            return self.accdb_path
        return str(Path(PATH_AIM_REFERENCE) / TARGET_DB_NAME)

    def resolved_sqlite_path(self) -> str:
        if self.sqlite_path:
            return self.sqlite_path
        return str(_default_data_dir() / "kanban.sqlite3")

    def resolved_log_dir(self) -> str:
        """記録(ログ)の置き場所。既定はこの端末のローカル領域の ``logs``(起動側と同じ)。"""
        if self.log_dir:
            return self.log_dir
        from . import app_config

        return str(app_config.local_dir("logs"))

    def to_dict(self) -> dict[str, Any]:
        data = {
            "shared_db_path": self.shared_db_path,
            "access_db_path": self.access_db_path,
            "history_db_path": self.history_db_path,
            "csv_dir": self.csv_dir,
            "accdb_path": self.accdb_path,
            "sqlite_path": self.sqlite_path,
            "line": self.line,
            "mode": self.mode,
            "admin_password_hash": self.admin_password_hash,
            "log_dir": self.log_dir,
            "manual_path": self.manual_path,
            "import_on_startup": self.import_on_startup,
            "export_interval_sec": self.export_interval_sec,
            "import_interval_sec": self.import_interval_sec,
            "mistake_minutes": self.mistake_minutes,
            "refresh_interval_sec": self.refresh_interval_sec,
            "frames_per_row": self.frames_per_row,
            "auto_print": self.auto_print,
            "busy_timeout_ms": self.busy_timeout_ms,
            "max_retry": self.max_retry,
        }
        return data


_KNOWN_KEYS = set(Config().to_dict())

#: 空欄なら「決めていない」を意味する項目。**空欄で下の段を覆い隠さない。**
#:
#: 配布設定より前の版は、端末の設定ファイルへ全項目を書き出していました
#: (未設定の接続先・パスワードも空欄のまま)。その空欄が配布設定を覆うと、
#: 配った接続先が効かず、**パスワードが未設定に戻って最初に押した人が決め
#: られる**状態になります。空欄は「無い」として読みます。
_BLANK_MEANS_UNSET = frozenset({
    "shared_db_path", "access_db_path", "history_db_path", "csv_dir", "accdb_path", "sqlite_path", "log_dir",
    "manual_path", "admin_password_hash", "line",
})

#: 端末の設定ファイルの形式。:func:`save_config` が書く目印。
#: 目印の無い**全項目入りのファイル**は、配布設定より前の版が書き出したもの
#: (:func:`_read_local`)。
LOCAL_FORMAT_KEY = "_形式"
LOCAL_FORMAT = 2

#: 旧版が必ず書いていた項目。これが全部あって目印が無ければ、旧版の書き出し。
#: (差分だけを書く今の版が、これを全部書くことはまず無い)
_LEGACY_DUMP_KEYS = frozenset({
    "import_on_startup", "frames_per_row", "busy_timeout_ms", "max_retry",
})


def _clean_values(raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """設定ファイルから読んだ値を、項目の型にそろえる。

    戻り値は ``(読んだ値, 読まなかった項目 → 理由)``。

    **手で書き換えたファイルを想定しています**(説明.txt でも案内している)。
    JSON では ``60`` と ``"60"``、``false`` と ``"false"`` を書き分けますが、
    手で直す人は区別しません。そのまま通すと ``"false"`` は**文字があるので
    「する」と読まれ**、``"60"`` は数として使うところで落ちます。
    """
    defaults = Config().to_dict()
    out: dict[str, Any] = {}
    bad: dict[str, str] = {}
    for key, value in raw.items():
        if key not in _KNOWN_KEYS:
            continue
        want = defaults[key]
        if isinstance(want, bool):
            if isinstance(value, bool):
                out[key] = value
            elif str(value).strip().lower() in ("true", "1", "する", "yes", "on"):
                out[key] = True
            elif str(value).strip().lower() in ("false", "0", "しない", "no", "off", ""):
                out[key] = False
            else:
                bad[key] = f"する/しない(true/false)で書いてください: {value!r}"
        elif isinstance(want, int):
            try:
                if isinstance(value, bool):
                    raise ValueError
                number = float(str(value).strip())
                if not number.is_integer() or number < 0:
                    raise ValueError
                out[key] = int(number)
            except ValueError:
                bad[key] = f"0 以上の整数で書いてください: {value!r}"
        elif isinstance(value, (dict, list)) or value is None:
            if value is None:
                continue
            bad[key] = f"文字で書いてください: {value!r}"
        else:
            out[key] = str(value)
    return out, bad


# ---------------------------------------------------------------------------
# 設定ファイルの置き場所
# ---------------------------------------------------------------------------
# この端末の設定(接続先・取り込み/書き戻し間隔・自動印刷・管理者パスワード・
# 担当ライン)は **``%APPDATA%\\KanbanSystem\\config.json``** に置きます。
#
# **アプリのフォルダの中には置きません。** 一時期 python-web-tools に倣って
# アプリのフォルダの ``data\\config.json`` に置いていましたが、新しい版を
# ダウンロードしてフォルダごと入れ替えると ``data\\`` が無くなり、**管理者
# パスワードも接続先も消えていました**(パスワードが「未設定」に戻り、最初に
# 押した人が決められる状態)。利用者ごとのフォルダなら、アプリを何度入れ
# 替えても残ります。
#
# 1 台で決めた設定を配るのは「配布設定」(:mod:`kanban.distribution`)。
# アプリのフォルダの直下の ``配布設定\\`` に書き出し、配った先は起動時に読む。
#
# アプリのフォルダの ``data\\config.json`` が残っていれば(その時期の版で
# 設定した端末)、この端末に無い項目だけを 1 度引き継ぎます(:func:`_migrate`)。

#: 端末ごとに違う値(配布設定では既定で配らない)
PER_PC_KEYS = frozenset({"line", "mode", "sqlite_path"})

#: 設定画面で直せる項目。画面に出すのは **Web 版で実際に効くもの**だけ ──
#: 効かない項目を並べると、直したのに何も変わらない、という一番わかりにくい
#: 状態を作るため
SHARED_FORM_KEYS = (
    "shared_db_path",
    "import_interval_sec",
    "export_interval_sec",
    "auto_print",
)

_APP_ROOT = Path(__file__).resolve().parent.parent

#: アプリのフォルダ(``start.bat`` のある場所)
APP_ROOT = _APP_ROOT

#: 引き継ぎの印(この端末の設定ファイルの付記)
_MIGRATED_FROM = "_アプリのフォルダから引き継いだ"


def settings_path() -> Path:
    """この端末の設定ファイル(``%APPDATA%\\KanbanSystem\\config.json``)。

    環境変数 ``KANBAN_CONFIG`` で差し替えられます(検証用)。
    """
    return _default_config_path()


def legacy_app_settings_path() -> Path:
    """一時期の版が使っていた、アプリのフォルダの ``data\\config.json``(引き継ぐだけ)。

    環境変数 ``KANBAN_SETTINGS_DIR`` でフォルダを差し替えられます(検証用)。
    """
    override = os.environ.get("KANBAN_SETTINGS_DIR")
    folder = Path(override) if override else _APP_ROOT / "data"
    return folder / "config.json"


#: 設定の項目を、利用者の言葉で呼ぶときの名前(画面・起動時のログ)
SETTING_LABELS = {
    "shared_db_path": "接続先(共有DB)",
    "import_interval_sec": "取り込み間隔",
    "export_interval_sec": "書き戻し間隔",
    "auto_print": "自動印刷",
    "mistake_minutes": "押し間違いとみなす時間",
    "admin_password_hash": "管理者パスワード",
    "line": "担当ライン",
}


def setting_label(key: str) -> str:
    return SETTING_LABELS.get(key, key)


@dataclass
class SettingsFileStatus:
    """この端末の設定ファイルがどうなっているか(起動時の確認・ログに出す)。"""

    path: str
    exists: bool = False
    error: str = ""
    """読めなかった理由。**壊れていても起動は止めない**(組み込みの既定で動く)。"""

    values: dict[str, Any] = field(default_factory=dict)
    """書いてある値(組み込みの既定と違うものだけが書かれている)。"""

    ignored: list[str] = field(default_factory=list)
    """書いてあったが読まなかった項目(形の違う値)。"""


def read_settings_file(path: Path | None = None) -> SettingsFileStatus:
    """設定ファイルを読む(配った先と同じ読み方)。"""
    target = Path(path) if path else settings_path()
    status = SettingsFileStatus(path=str(target))
    try:
        status.exists = target.is_file()
    except OSError as exc:
        status.error = f"設定ファイルを確かめられませんでした: {exc}"
        return status
    if not status.exists:
        return status
    raw = _read_raw_local(target)
    if raw is None:
        status.error = f"設定ファイルを読めませんでした(JSON の形が違います): {target}"
        return status
    wanted = {
        k: v for k, v in raw.items()
        if not k.startswith("_") and k != "mode"
        and not (k in _BLANK_MEANS_UNSET and not str(v or "").strip())
    }
    status.values, bad = _clean_values(wanted)
    status.ignored += [f"{setting_label(k)}({why})" for k, why in bad.items()]
    return status


def _read_raw_local(config_path: Path) -> dict[str, Any] | None:
    """端末の設定ファイルをそのまま読む。無ければ ``{}``、読めなければ ``None``。

    **BOM 付きも読みます**(``utf-8-sig``)。メモ帳で直すと BOM が付くことが
    あり、以前はそれだけで**ファイル全体を読まなかった** ── 担当ラインも
    接続先も消えたように見え、次に何かを保存した時点で本当に消えていました。
    """
    if not config_path.is_file():
        return {}
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _read_local(config_path: Path) -> dict[str, Any]:
    raw = _read_raw_local(config_path) or {}
    legacy = LOCAL_FORMAT_KEY not in raw and _LEGACY_DUMP_KEYS <= set(raw)
    builtin = Config().to_dict()
    values, _bad = _clean_values(raw)
    out: dict[str, Any] = {}
    for key, value in values.items():
        if key in _BLANK_MEANS_UNSET and not str(value).strip():
            continue
        if legacy and value == builtin.get(key):
            # **旧版の書き出し。** 全項目を書いていたので、組み込みの既定と同じ
            # 値は「この端末で選んだ」のではなく「書き出されただけ」。
            # 持っていると配布設定をすべて覆い隠す
            continue
        out[key] = value
    return out


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """設定を読む。

    ``path`` を渡さなければ(ふだん)この端末の設定ファイル
    (``%APPDATA%\\KanbanSystem\\config.json``)。``--config``・試験では渡された
    ファイルを読む。どちらも無ければ組み込みの既定値。壊れていれば読まずに
    既定で動く(起動は止めない)。
    """
    config_path = Path(path) if path else settings_path()
    if not path:
        _migrate(config_path)
    values = {k: v for k, v in _read_local(config_path).items() if k != "mode"}
    return replace(Config(), **values, source_path=str(config_path))


def _migrate(config_path: Path) -> None:
    """アプリのフォルダの ``data\\config.json`` を、この端末の設定へ 1 度だけ引き継ぐ。

    一時期の版は設定をアプリのフォルダに置いていました。その版で設定した端末では、
    パスワードや接続先がそちらにしか無いので、**この端末に無い項目だけ**を
    持ってくる(この端末で決めてある値は上書きしない)。引き継いだら印を付け、
    次からは読まない。書けなければ何もしない(読んだ値で動くことはしない ──
    次の起動でもう一度試す)。
    """
    legacy = legacy_app_settings_path()
    try:
        if not legacy.is_file():
            return
    except OSError:
        return
    raw = _read_raw_local(config_path)
    if raw is None or raw.get(_MIGRATED_FROM) == str(legacy):
        return
    old = read_settings_file(legacy).values
    have = _read_local(config_path)
    carried = {k: v for k, v in old.items() if k not in have and k != "mode"}
    try:
        _write_json(config_path, {
            LOCAL_FORMAT_KEY: LOCAL_FORMAT, **raw, **carried, _MIGRATED_FROM: str(legacy),
        })
    except OSError:
        return
    if carried:
        try:
            from .applog import get_logger

            get_logger("config").info(
                "アプリのフォルダの設定を引き継ぎました: %s(%s)",
                legacy, "、".join(setting_label(k) for k in sorted(carried)),
            )
        except Exception:  # noqa: BLE001 - 記録できなくても引き継ぎは済んでいる
            pass


def save_config(cfg: Config) -> None:
    """設定を書き出す。

    **組み込みの既定と同じ値は書きません。** 書くのは変えたぶんだけなので、
    ファイルを開けば「何を変えてあるか」がそのまま読めます。付記(``_`` で
    始まる名前。配布設定を読み込んだ日時など)は残します。
    """
    builtin = Config().to_dict()
    path = Path(cfg.source_path) if cfg.source_path else settings_path()
    values = {
        key: value for key, value in cfg.to_dict().items()
        if key != "mode"
        and value != builtin.get(key)
        and not (key in _BLANK_MEANS_UNSET and not str(value).strip())
    }
    _write_if_changed(path, {LOCAL_FORMAT_KEY: LOCAL_FORMAT, **_meta_of(path), **values})


def _meta_of(path: Path) -> dict[str, Any]:
    """ファイルにある付記(``_`` で始まる名前。形式の目印は除く)。書き直しても残す。"""
    raw = _read_raw_local(path) or {}
    return {k: v for k, v in raw.items() if k.startswith("_") and k != LOCAL_FORMAT_KEY}


def read_meta(key: str, path: Path | None = None) -> Any:
    """この端末の設定ファイルの付記を読む(配布設定を読み込んだ日時など)。"""
    return (_read_raw_local(Path(path) if path else settings_path()) or {}).get(key)


def write_meta(key: str, value: Any, path: Path | None = None) -> None:
    """この端末の設定ファイルに付記を書く。設定の値には触らない。"""
    target = Path(path) if path else settings_path()
    raw = _read_raw_local(target) or {}
    _write_json(target, {LOCAL_FORMAT_KEY: LOCAL_FORMAT, **raw, key: value})


def _write_if_changed(path: Path, values: dict[str, Any]) -> None:
    if _read_raw_local(path) == values:
        return
    _write_json(path, values)


def _write_json(path: Path, values: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if _read_raw_local(path) is None:
        # **読めないファイルを黙って上書きしない。** 手で直して壊した設定の
        # 中身は、直せば読める。残しておけば取り戻せる
        from datetime import datetime

        keep = path.with_name(f"{path.name}.読めなかった_{datetime.now():%Y%m%d%H%M%S}")
        try:
            os.replace(path, keep)
        except OSError:
            pass
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
