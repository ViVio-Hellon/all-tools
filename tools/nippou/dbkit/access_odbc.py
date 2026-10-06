"""Accessファイル(.accdb/.mdb)への接続 (pip不要・Windows標準DLLのみ)

VBA版は ACE OLEDB でAccessに直接つないでいた。Python版も同じ端末で
同じファイルを読み書きしたいが、`pyodbc` は導入できない制約がある。

そこで **Windowsに標準で入っている `odbc32.dll` を `ctypes` から直接呼ぶ**。
追加インストールは何も要らない。必要なのは「Accessのドライバが入っている
こと」だけで、これは元のVBAツールが動いていた端末なら必ず入っている
(Access本体、または無償の Microsoft Access Database Engine)。

    conn = connect(Path("梱包資材マスタ.accdb"))
    for row in conn.query("SELECT 幅, 丈 FROM PalletMaster"):
        ...
    conn.execute("INSERT INTO ...")
    conn.close()

【Windows以外では使えない】
開発・検証はLinuxで行っているため、この経路は`is_available()`が False を
返して使われない。Linux側は `mdbtools`(`mdb-export`)を使う
(`data_sync.py` が環境を見て自動的に振り分ける)。

【SQLの組み立てについて】
`SQLBindParameter` によるパラメータ化は型ごとのバインドが必要で、
実機でしか検証できない箇所が増える。ここでは**値をエスケープして
SQL文に埋め込む**方式にし、エスケープを1か所(`quote`)に集約して
テストで固めている。Accessの文字列リテラルはシングルクォートを
2つ重ねてエスケープする。
"""
from __future__ import annotations

import ctypes
import sys
import time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

from .logging_utils import get_logger

log = get_logger("access_odbc")

# ------------------------------------------------------------------
# ODBC の定数 (sql.h / sqlext.h より。値はODBC仕様で固定)
# ------------------------------------------------------------------
SQL_HANDLE_ENV = 1
SQL_HANDLE_DBC = 2
SQL_HANDLE_STMT = 3
SQL_ATTR_ODBC_VERSION = 200
SQL_OV_ODBC3 = 3
SQL_NULL_HANDLE = 0
SQL_SUCCESS = 0
SQL_SUCCESS_WITH_INFO = 1
SQL_NO_DATA = 100
SQL_ERROR = -1
SQL_INVALID_HANDLE = -2
SQL_DRIVER_NOCOMPLETE = 0
SQL_C_WCHAR = -8
SQL_NULL_DATA = -1
SQL_NTS = -3
SQL_FETCH_NEXT = 1
SQL_FETCH_FIRST = 2
SQL_ATTR_AUTOCOMMIT = 102
SQL_AUTOCOMMIT_OFF = 0
SQL_AUTOCOMMIT_ON = 1
SQL_COMMIT = 0
SQL_ROLLBACK = 1

# 1セルあたりの取得バッファ(文字数)。長いメモ欄も想定して余裕を持たせる
_CELL_CHARS = 4096

# ロック競合時の既定リトライ回数/待機秒数。呼び出し側の都合(業務側の
# config 等)には依存させず、このモジュール単体で完結させる
# (他のVBA移行プロジェクトへそのまま持っていけるようにするため)
DEFAULT_MAX_RETRY = 3
DEFAULT_RETRY_WAIT_SEC = 1.0

# Accessドライバ名の候補(新しいものから順に試す)
DRIVER_NAMES = (
    "Microsoft Access Driver (*.mdb, *.accdb)",
    "Microsoft Access Driver (*.mdb)",
)


class AccessError(RuntimeError):
    """Access接続まわりの失敗。原因を日本語で持つ。"""


# ロック競合と読めるドライバメッセージの断片(VBA `IsLockError` の移植)。
#
# VBA版は ADO のエラー番号(3218/3260/3261 など)で判定していたが、ODBC
# 経由だとドライバがメッセージに丸めてしまい番号が取れない。VBA側も
# 「番号で拾えないときは文字列に lock / ロック が含まれるかを見る」
# フォールバックを持っていたので、こちらはその方式に寄せる。
LOCK_ERROR_HINTS = (
    "lock",              # could not lock table / record is locked ...
    "ロック",
    "in use by another",
    "他のユーザー",
    "使用中",
    "deadlock",
)


def is_lock_error(message: str) -> bool:
    """そのエラーがロック競合か(=待てば通る見込みがあるか)。

    競合以外のエラー(列が無い・型が違う等)まで再試行すると、同じ失敗を
    繰り返して遅くなるだけなので、ここで区別する。
    """
    lowered = message.lower()
    return any(hint.lower() in lowered for hint in LOCK_ERROR_HINTS)


# 一意インデックス違反(=同じ送信IDで前回すでに届いていた)と読める
# ドライバメッセージの断片。`outbox_sync` の再送判定で使う
DUPLICATE_ERROR_HINTS = (
    "duplicate",
    "重複",
)


def is_duplicate_error(message: str) -> bool:
    """一意インデックス違反(=前回の送信が既にAccessへ届いていた)か。

    `outbox_sync` の送信ID方式は、再送で同じ値をもう一度INSERTしようと
    することがある(予約の期限切れ拾い直し等)。それをAccess側の一意
    インデックスが弾いたときは失敗ではなく「既に成功していた」なので、
    区別できるようにしておく。
    """
    lowered = message.lower()
    return any(hint.lower() in lowered for hint in DUPLICATE_ERROR_HINTS)


# DDL(列追加・インデックス作成)が「もう存在する」ために失敗したと
# 読めるドライバメッセージの断片。`outbox_sync` のスキーマ準備で使う
ALREADY_EXISTS_HINTS = (
    "already",
    "既に",
    "既存",
)


def is_already_exists_error(message: str) -> bool:
    """列/インデックスの作成失敗が「もう存在するから」かどうか。

    複数端末が同時に同じALTER TABLEを試みることもあるので、これも
    失敗として扱わず「準備はできている」とみなす。
    """
    lowered = message.lower()
    return any(hint.lower() in lowered for hint in ALREADY_EXISTS_HINTS)


def is_available() -> bool:
    """この環境でODBC経由のAccess接続が使えるか。"""
    return sys.platform == "win32"


def _odbc():
    if not is_available():
        raise AccessError(
            "ODBC経由のAccess接続はWindowsでのみ使えます。"
            "この環境では mdbtools を使ってください。")
    try:
        return ctypes.windll.odbc32       # type: ignore[attr-defined]  # Windows専用
    except (AttributeError, OSError) as exc:      # pragma: no cover - 環境依存
        raise AccessError(f"odbc32.dll を読み込めませんでした: {exc}") from exc


def _ok(ret: int) -> bool:
    return ret in (SQL_SUCCESS, SQL_SUCCESS_WITH_INFO)


def _diagnostics(handle_type: int, handle: int) -> str:
    """SQLGetDiagRecW でドライバ側のエラーメッセージを取り出す。"""
    odbc = _odbc()
    messages: list[str] = []
    for rec in range(1, 6):
        state = ctypes.create_unicode_buffer(6)
        native = ctypes.c_long()
        text = ctypes.create_unicode_buffer(1024)
        text_len = ctypes.c_short()
        ret = odbc.SQLGetDiagRecW(
            ctypes.c_short(handle_type), ctypes.c_void_p(handle), ctypes.c_short(rec),
            state, ctypes.byref(native), text, ctypes.c_short(1024),
            ctypes.byref(text_len))
        if not _ok(ret):
            break
        messages.append(f"[{state.value}] {text.value}")
    return " / ".join(messages) or "(詳細なし)"


# ------------------------------------------------------------------
# SQLリテラルの組み立て
# ------------------------------------------------------------------
def quote(value: Any) -> str:
    """値をAccessのSQLリテラルにする。

    - None            -> NULL
    - 真偽            -> True / False
    - 数値            -> そのまま
    - 日付/日時       -> #yyyy-mm-dd hh:nn:ss#
    - それ以外(文字列) -> 'xxx' (シングルクォートは2つ重ねる)

    改行やタブはリテラルに入れると壊れるので、Accessの文字列連結で
    Chr()に逃がす……という手もあるが、この移植で書き戻す列に
    改行は入らないため、単純に空白へ寄せる(入っていたらログに残す)。
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, datetime):
        return f"#{value:%Y-%m-%d %H:%M:%S}#"
    if isinstance(value, date):
        return f"#{value:%Y-%m-%d}#"

    text = str(value)
    if "\n" in text or "\r" in text or "\t" in text:
        log.warning("SQLリテラルに改行/タブが含まれていたので空白に置換します: %r", text)
        text = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ").replace("\t", " ")
    return "'" + text.replace("'", "''") + "'"


def quote_identifier(name: str) -> str:
    """テーブル名・列名を角かっこで囲む(日本語列名・空白入り対策)。"""
    if "]" in name:
        raise AccessError(f"識別子に ] は使えません: {name}")
    return f"[{name}]"


def build_insert(table: str, values: dict[str, Any]) -> str:
    cols = ", ".join(quote_identifier(c) for c in values)
    vals = ", ".join(quote(v) for v in values.values())
    return f"INSERT INTO {quote_identifier(table)} ({cols}) VALUES ({vals})"


def build_update(table: str, values: dict[str, Any], where: dict[str, Any]) -> str:
    sets = ", ".join(f"{quote_identifier(c)} = {quote(v)}" for c, v in values.items())
    conds = " AND ".join(f"{quote_identifier(c)} = {quote(v)}" for c, v in where.items())
    sql = f"UPDATE {quote_identifier(table)} SET {sets}"
    if conds:
        sql += f" WHERE {conds}"
    return sql


def connection_string(path: Path, *, driver: Optional[str] = None) -> str:
    """AccessのODBC接続文字列。DSN登録は不要(DSN-less接続)。"""
    drv = driver or DRIVER_NAMES[0]
    return f"Driver={{{drv}}};DBQ={path};"


# ------------------------------------------------------------------
# 接続
# ------------------------------------------------------------------
class AccessConnection:
    """Accessファイル1つへの接続。`with` でも使える。"""

    def __init__(self, path: Path, *, driver: Optional[str] = None,
                 read_only: bool = False) -> None:
        self.path = Path(path)
        self.read_only = read_only
        self._odbc = _odbc()
        self._env = ctypes.c_void_p()
        self._dbc = ctypes.c_void_p()
        self._connect(driver)

    # -- 接続/切断 --------------------------------------------------
    def _connect(self, driver: Optional[str]) -> None:
        if not self.path.exists():
            raise AccessError(f"Accessファイルが見つかりません: {self.path}")

        odbc = self._odbc
        if not _ok(odbc.SQLAllocHandle(SQL_HANDLE_ENV, None, ctypes.byref(self._env))):
            raise AccessError("ODBC環境ハンドルを確保できませんでした")
        odbc.SQLSetEnvAttr(self._env, SQL_ATTR_ODBC_VERSION,
                           ctypes.c_void_p(SQL_OV_ODBC3), 0)
        if not _ok(odbc.SQLAllocHandle(SQL_HANDLE_DBC, self._env, ctypes.byref(self._dbc))):
            raise AccessError("ODBC接続ハンドルを確保できませんでした")

        candidates = [driver] if driver else list(DRIVER_NAMES)
        last = ""
        for name in candidates:
            conn_str = connection_string(self.path, driver=name)
            out = ctypes.create_unicode_buffer(1024)
            out_len = ctypes.c_short()
            ret = odbc.SQLDriverConnectW(
                self._dbc, None, ctypes.c_wchar_p(conn_str), SQL_NTS,
                out, ctypes.c_short(1024), ctypes.byref(out_len),
                ctypes.c_ushort(SQL_DRIVER_NOCOMPLETE))
            if _ok(ret):
                log.info("Access接続: %s (driver=%s)", self.path.name, name)
                return
            last = _diagnostics(SQL_HANDLE_DBC, self._dbc.value or 0)

        self.close()
        raise AccessError(
            f"Accessに接続できませんでした: {self.path.name}\n{last}\n"
            "Accessのドライバが入っていない可能性があります。Access本体、または"
            "「Microsoft Access Database Engine 再頒布可能コンポーネント」(無償)"
            "を入れてください。"
            "なお64bitのPythonからは64bit版のドライバが必要です。")

    def close(self) -> None:
        odbc = self._odbc
        if self._dbc:
            odbc.SQLDisconnect(self._dbc)
            odbc.SQLFreeHandle(SQL_HANDLE_DBC, self._dbc)
            self._dbc = ctypes.c_void_p()
        if self._env:
            odbc.SQLFreeHandle(SQL_HANDLE_ENV, self._env)
            self._env = ctypes.c_void_p()

    def __enter__(self) -> "AccessConnection":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- 問い合わせ -------------------------------------------------
    def query(self, sql: str, *,
              max_retry: Optional[int] = None) -> list[dict[str, str]]:
        """SELECTを実行して、列名→文字列 の辞書のリストを返す。

        値はすべて文字列で受け取る(SQL_C_WCHAR)。型変換は取り込み側の
        変換関数(`to_int`/`to_real`等)に任せる。mdbtools経由の
        CSVエクスポートと同じ扱いになるので、後段を共通化できる。

        読み取りも書き込みと同じくロック競合で失敗しうる(他端末が
        書いている最中に読むと弾かれる)。VBA側も読み取りに再試行を
        入れていたので、こちらも同じ扱いにする。
        """
        retry_limit = DEFAULT_MAX_RETRY if max_retry is None else max_retry
        attempt = 0
        while True:
            try:
                return self._query_once(sql)
            except AccessError as exc:
                attempt += 1
                if not is_lock_error(str(exc)) or attempt > retry_limit:
                    raise
                wait = DEFAULT_RETRY_WAIT_SEC * attempt
                log.warning("読み取りがロック競合。%.1f秒待って再試行します (%s回目)",
                            wait, attempt)
                time.sleep(wait)

    def _query_once(self, sql: str) -> list[dict[str, str]]:
        return list(self.iter_query(sql))

    def iter_query(self, sql: str) -> Iterator[dict[str, str]]:
        odbc = self._odbc
        stmt = ctypes.c_void_p()
        if not _ok(odbc.SQLAllocHandle(SQL_HANDLE_STMT, self._dbc, ctypes.byref(stmt))):
            raise AccessError("ODBCステートメントを確保できませんでした")
        try:
            ret = odbc.SQLExecDirectW(stmt, ctypes.c_wchar_p(sql), SQL_NTS)
            if not _ok(ret):
                raise AccessError(
                    f"SQLの実行に失敗しました: {_diagnostics(SQL_HANDLE_STMT, stmt.value or 0)}"
                    f"\nSQL: {sql}")

            col_count = ctypes.c_short()
            odbc.SQLNumResultCols(stmt, ctypes.byref(col_count))
            names = [self._column_name(stmt, i) for i in range(1, col_count.value + 1)]

            while True:
                ret = odbc.SQLFetch(stmt)
                if ret == SQL_NO_DATA:
                    break
                if not _ok(ret):
                    raise AccessError(
                        "行の取得に失敗しました: "
                        f"{_diagnostics(SQL_HANDLE_STMT, stmt.value or 0)}")
                yield {names[i]: self._cell(stmt, i + 1) for i in range(col_count.value)}
        finally:
            odbc.SQLFreeHandle(SQL_HANDLE_STMT, stmt)

    def _column_name(self, stmt, index: int) -> str:
        buf = ctypes.create_unicode_buffer(256)
        name_len = ctypes.c_short()
        data_type = ctypes.c_short()
        size = ctypes.c_size_t()
        digits = ctypes.c_short()
        nullable = ctypes.c_short()
        self._odbc.SQLDescribeColW(
            stmt, ctypes.c_ushort(index), buf, ctypes.c_short(256),
            ctypes.byref(name_len), ctypes.byref(data_type), ctypes.byref(size),
            ctypes.byref(digits), ctypes.byref(nullable))
        return buf.value

    def _cell(self, stmt, index: int) -> str:
        buf = ctypes.create_unicode_buffer(_CELL_CHARS)
        indicator = ctypes.c_ssize_t()
        ret = self._odbc.SQLGetData(
            stmt, ctypes.c_ushort(index), ctypes.c_short(SQL_C_WCHAR),
            buf, ctypes.c_ssize_t(_CELL_CHARS * 2), ctypes.byref(indicator))
        if not _ok(ret) or indicator.value == SQL_NULL_DATA:
            return ""
        return buf.value

    # -- 更新 -------------------------------------------------------
    def execute(self, sql: str, *, max_retry: Optional[int] = None) -> int:
        """INSERT/UPDATE/DELETEを実行して、影響行数を返す。

        Accessは共有フォルダに置いて複数端末から使うので、他の端末が
        書いている瞬間に当たるとロック競合で失敗する。VBA
        `ExecuteSQLWithRetry` と同じく、ロック競合のときだけ
        少し待って再試行する(それ以外のエラーは即座に投げる)。
        """
        if self.read_only:
            raise AccessError("読み取り専用で開いた接続では更新できません")
        retry_limit = DEFAULT_MAX_RETRY if max_retry is None else max_retry
        attempt = 0
        while True:
            try:
                return self._execute_once(sql)
            except AccessError as exc:
                attempt += 1
                if not is_lock_error(str(exc)) or attempt > retry_limit:
                    raise
                wait = DEFAULT_RETRY_WAIT_SEC * attempt
                log.warning("ロック競合のため %.1f秒待って再試行します (%s回目)",
                            wait, attempt)
                time.sleep(wait)

    def _execute_once(self, sql: str) -> int:
        odbc = self._odbc
        stmt = ctypes.c_void_p()
        if not _ok(odbc.SQLAllocHandle(SQL_HANDLE_STMT, self._dbc, ctypes.byref(stmt))):
            raise AccessError("ODBCステートメントを確保できませんでした")
        try:
            ret = odbc.SQLExecDirectW(stmt, ctypes.c_wchar_p(sql), SQL_NTS)
            if not _ok(ret):
                raise AccessError(
                    f"更新に失敗しました: {_diagnostics(SQL_HANDLE_STMT, stmt.value or 0)}"
                    f"\nSQL: {sql}")
            rows = ctypes.c_ssize_t()
            odbc.SQLRowCount(stmt, ctypes.byref(rows))
            return int(rows.value)
        finally:
            odbc.SQLFreeHandle(SQL_HANDLE_STMT, stmt)

    def insert(self, table: str, values: dict[str, Any]) -> int:
        return self.execute(build_insert(table, values))

    def update(self, table: str, values: dict[str, Any], where: dict[str, Any]) -> int:
        return self.execute(build_update(table, values, where))

    # -- トランザクション --------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator["AccessConnection"]:
        """複数のSQL文を1つのAccessトランザクションにまとめる。

        VBA/VBScript側の ``BeginTrans`` / ``CommitTrans`` / ``RollbackTrans``
        に相当する。既定では ODBC接続は自動コミットなので、この間だけ
        ``SQL_ATTR_AUTOCOMMIT`` を切ってまとめてコミットし、ブロック内で
        例外が起きたらロールバックする。DELETE+複数INSERTのような
        「途中で失敗したら全部無かったことにしたい」処理はこれで囲むこと。

        呼び出し中は必ず後始末(自動コミットを元に戻す)を行うため、
        with文を抜けたあとは通常どおり1文ごとに自動コミットされる状態に戻る。
        """
        odbc = self._odbc
        ret = odbc.SQLSetConnectAttr(
            self._dbc, SQL_ATTR_AUTOCOMMIT, ctypes.c_void_p(SQL_AUTOCOMMIT_OFF), 0)
        if not _ok(ret):
            raise AccessError(
                f"トランザクションを開始できませんでした: "
                f"{_diagnostics(SQL_HANDLE_DBC, self._dbc.value or 0)}")
        try:
            yield self
        except BaseException:
            odbc.SQLEndTran(SQL_HANDLE_DBC, self._dbc, SQL_ROLLBACK)
            raise
        else:
            ret = odbc.SQLEndTran(SQL_HANDLE_DBC, self._dbc, SQL_COMMIT)
            if not _ok(ret):
                odbc.SQLEndTran(SQL_HANDLE_DBC, self._dbc, SQL_ROLLBACK)
                raise AccessError(
                    f"コミットに失敗しました(ロールバックしました): "
                    f"{_diagnostics(SQL_HANDLE_DBC, self._dbc.value or 0)}")
        finally:
            odbc.SQLSetConnectAttr(
                self._dbc, SQL_ATTR_AUTOCOMMIT, ctypes.c_void_p(SQL_AUTOCOMMIT_ON), 0)

    def table_names(self) -> list[str]:
        """ユーザーテーブルの一覧(システムテーブルは除く)。"""
        odbc = self._odbc
        stmt = ctypes.c_void_p()
        odbc.SQLAllocHandle(SQL_HANDLE_STMT, self._dbc, ctypes.byref(stmt))
        try:
            odbc.SQLTablesW(stmt, None, 0, None, 0, None, 0,
                            ctypes.c_wchar_p("TABLE"), SQL_NTS)
            names: list[str] = []
            while odbc.SQLFetch(stmt) != SQL_NO_DATA:
                names.append(self._cell(stmt, 3))   # 3列目がテーブル名
            return names
        finally:
            odbc.SQLFreeHandle(SQL_HANDLE_STMT, stmt)


def connect(path: Path, *, driver: Optional[str] = None,
            read_only: bool = False) -> AccessConnection:
    return AccessConnection(path, driver=driver, read_only=read_only)


# ------------------------------------------------------------------
# 診断 (現場の端末で「使えるか」を確かめるため)
# ------------------------------------------------------------------
def installed_drivers() -> list[str]:
    """インストール済みのODBCドライバ名を列挙する。"""
    odbc = _odbc()
    env = ctypes.c_void_p()
    if not _ok(odbc.SQLAllocHandle(SQL_HANDLE_ENV, None, ctypes.byref(env))):
        raise AccessError("ODBC環境ハンドルを確保できませんでした")
    try:
        odbc.SQLSetEnvAttr(env, SQL_ATTR_ODBC_VERSION, ctypes.c_void_p(SQL_OV_ODBC3), 0)
        names: list[str] = []
        direction = SQL_FETCH_FIRST
        while True:
            desc = ctypes.create_unicode_buffer(256)
            desc_len = ctypes.c_short()
            attrs = ctypes.create_unicode_buffer(1024)
            attrs_len = ctypes.c_short()
            ret = odbc.SQLDriversW(
                env, ctypes.c_ushort(direction), desc, ctypes.c_short(256),
                ctypes.byref(desc_len), attrs, ctypes.c_short(1024),
                ctypes.byref(attrs_len))
            if ret == SQL_NO_DATA or not _ok(ret):
                break
            names.append(desc.value)
            direction = SQL_FETCH_NEXT
        return names
    finally:
        odbc.SQLFreeHandle(SQL_HANDLE_ENV, env)


def find_access_driver() -> Optional[str]:
    """使えるAccessドライバ名を返す。無ければ None。"""
    try:
        installed = installed_drivers()
    except AccessError:
        return None
    for name in DRIVER_NAMES:
        if name in installed:
            return name
    # 名前が少し違う版もあるので "Access" を含むものを拾う
    for name in installed:
        if "Access" in name:
            return name
    return None


def diagnose(paths: Sequence[Path] = ()) -> str:
    """現場の端末で実行してもらう診断。結果を文字列で返す。"""
    lines = [
        "=== Access接続の診断 ===",
        f"Python: {sys.version.split()[0]} ({8 * ctypes.sizeof(ctypes.c_void_p)}bit)",
        f"OS: {sys.platform}",
    ]
    if not is_available():
        lines.append("→ Windowsではないため、ODBC経由の接続は使えません。")
        lines.append("  (Linux/WSLでは mdbtools を使ってください)")
        return "\n".join(lines)

    try:
        drivers = installed_drivers()
    except AccessError as exc:
        lines.append(f"→ ドライバ一覧を取得できませんでした: {exc}")
        return "\n".join(lines)

    lines.append(f"インストール済みODBCドライバ: {len(drivers)}件")
    for name in drivers:
        mark = " ← これを使います" if name in DRIVER_NAMES else ""
        lines.append(f"  - {name}{mark}")

    driver = find_access_driver()
    if driver is None:
        lines += [
            "",
            "→ Accessのドライバが見つかりません。次のどちらかを入れてください:",
            "   ・Microsoft Access 本体",
            "   ・Microsoft Access Database Engine 再頒布可能コンポーネント(無償)",
            "  Pythonが64bitなら64bit版、32bitなら32bit版が必要です。",
        ]
        return "\n".join(lines)

    lines.append(f"\n使用するドライバ: {driver}")
    for path in paths:
        lines.append(f"\n--- {path} ---")
        if not Path(path).exists():
            lines.append("  ファイルがありません")
            continue
        try:
            with connect(Path(path), driver=driver, read_only=True) as conn:
                tables = conn.table_names()
                lines.append(f"  接続OK / テーブル {len(tables)}件")
                lines.append(f"  {', '.join(tables[:12])}"
                             + (" ..." if len(tables) > 12 else ""))
        except AccessError as exc:
            lines.append(f"  接続できませんでした: {exc}")
    return "\n".join(lines)


if __name__ == "__main__":   # pragma: no cover - 手動診断用
    targets = [Path(a) for a in sys.argv[1:]]
    print(diagnose(targets))
