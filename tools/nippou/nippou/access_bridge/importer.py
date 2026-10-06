"""Phase ① of the architecture: Access -> CSV (via generated VBScript) ->
SQLite.

Ports the read side of ``GetRecordsArr`` / ``shiftTime`` (used to
populate ``Start1``/``End1``/... at startup) onto the new pipeline: run a
generated VBScript that dumps a table to UTF-8 CSV, then parse that CSV
with the standard :mod:`csv` module and hand rows to the caller (kept
generic so any Access table can be imported, not just the shift-time
master).
"""
from __future__ import annotations

import csv
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .. import source_db
from ..config import SETTINGS
from ..logging_setup import get_logger
from . import script_gen
from .errors import AccessBridgeError, ErrorKind
from .runner import ScriptRunner, run_with_retry

_logger = get_logger("access_bridge.importer")


def _as_text(value: object) -> str:
    """sqlite3 が返す値を、Access経路と同じ「文字列」に揃える。

    呼び出し側は ``row.get("製造板厚")`` を ``float()`` に掛けたり
    ``.strip()`` したりする ── Access経路がCSV由来で全部文字列だから。
    ここで揃えないと、取り込み元が sqlite3 になった日に
    ``AttributeError: 'float' object has no attribute 'strip'`` が
    画面のあちこちで出る。

    小数は ``1.0`` ではなく ``1`` にする。VBAが書いた整数が sqlite3 では
    ``REAL`` で入っていることがあり、そのまま文字にすると照合(用途ｺｰﾄﾞ・
    管理番号のような「数字だが文字として突き合わせる」列)が外れる。
    """
    if value is None:
        return ""
    if isinstance(value, bool):                # bool は int より先に見る
        return "1" if value else "0"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _import_sqlite(path: Path, table_name: str, sql_filter: str) -> ImportResult:
    """取り込み元が sqlite3 のとき。**共有のファイルは開かない。**

    `source_db` が手元へ写してから読む(その理由はあちらに書いてある)。

    ``sql_filter`` は Access の書き方(``[ﾛｯﾄ番号]='...'``)で渡ってくる。
    角括弧を二重引用符に直せば sqlite3 でもそのまま通る ── どちらも
    「識別子を囲む書き方」で、中身の文法は同じ。**呼び出し側が組み立てた
    文をそのまま置く**ので、値は必ず ``script_gen.sql_literal`` を
    通っていること(通っていれば引用符は畳まれている)。
    """
    where = f" WHERE {_to_sqlite_filter(sql_filter)}" if sql_filter.strip() else ""
    sql = f"SELECT * FROM {source_db.quote_identifier(table_name)}{where}"
    try:
        rows = source_db.read_query(path, sql)
    except source_db.SourceError as exc:
        _logger.warning("sqlite3 の取り込みに失敗しました table=%s: %s",
                        table_name, exc)
        return ImportResult(success=False,
                            error=AccessBridgeError(ErrorKind.UNKNOWN, None, str(exc)))
    _logger.info("import ok (sqlite3) table=%s rows=%d", table_name, len(rows))
    return ImportResult(
        success=True,
        rows=[{k: _as_text(v) for k, v in row.items()} for row in rows])


def _to_sqlite_filter(sql_filter: str) -> str:
    """Access の ``[列名]`` を sqlite3 の ``"列名"`` に直す。

    値の側は触らない ── ``'...'`` の中に ``[`` や ``]`` が入っていても、
    それは列名ではないので置き換えてはいけない。引用符の中かどうかを
    見ながら進む。
    """
    out: list[str] = []
    in_quote = False
    for char in sql_filter:
        if char == "'":
            in_quote = not in_quote
            out.append(char)
        elif not in_quote and char in "[]":
            out.append('"')
        else:
            out.append(char)
    return "".join(out)


@dataclass
class ImportResult:
    success: bool
    rows: list[dict[str, str]] = field(default_factory=list)
    error: Optional[AccessBridgeError] = None


def import_table(
    accdb_path: Path,
    table_name: str,
    sql_filter: str = "",
    runner: Optional[ScriptRunner] = None,
) -> ImportResult:
    """Access上の1テーブルを取り込む。

    「仕掛」テーブルのように数千件・100列超になりうる共有マスタも対象に
    含まれるため、呼び出し側は必要な範囲だけを ``sql_filter``
    （例: ``"[ﾛｯﾄ番号]='...'"``）で絞り込むことを強く推奨する
    （README「運用上の注意」参照）。フィルタなしの全件取得は
    Access側の応答時間・ロック保持時間を延ばし、他ラインの操作を
    間接的に待たせる原因になり得る。

    一時ファイルの作成・CSV読み込みで想定外の ``OSError``
    （ディスク容量不足など）が起きても、ここで捕捉して失敗として返す
    ---呼び出し元のライン全体がクラッシュしないようにするため。

    ``SETTINGS.access_backend == "odbc"`` の場合は VBScript/cscript.exe
    を経由せず、``dbkit.access_odbc`` による直結読み取りへ委譲する
    (``runner`` はVBScript経路専用なのでODBC経路では使われない)。

    **渡された道が sqlite3 なら、そちらへ回す**(:func:`_import_sqlite`)。
    参照用マスタは Access から sqlite3 へ移りつつあり、置き換えは
    ファイル単位で進む ── 呼び出し側が「いまどちらか」を気にせずに
    済むよう、振り分けはここ1か所で行う。
    """
    if Path(accdb_path).suffix.lower() in source_db.SUFFIXES:
        return _import_sqlite(Path(accdb_path), table_name, sql_filter)

    if SETTINGS.access_backend == "odbc":
        from .odbc_backend import import_table_odbc
        return import_table_odbc(accdb_path, table_name, sql_filter)

    try:
        fd, csv_path = tempfile.mkstemp(suffix=".csv", prefix="nippou_import_")
        os.close(fd)
    except OSError as exc:
        _logger.exception("一時CSVファイルの作成に失敗しました table=%s", table_name)
        return ImportResult(success=False, error=AccessBridgeError(ErrorKind.UNKNOWN, None, str(exc)))

    try:
        result = run_with_retry(
            lambda: script_gen.build_import_script(str(accdb_path), table_name, csv_path, sql_filter),
            runner=runner,
        )
        if not result.success:
            _logger.error("import failed table=%s error=%s", table_name, result.error)
            return ImportResult(success=False, error=result.error)

        rows: list[dict[str, str]] = []
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append({k: (v or "") for k, v in row.items()})
        _logger.info("import ok table=%s rows=%d", table_name, len(rows))
        return ImportResult(success=True, rows=rows)
    except OSError as exc:
        _logger.exception("CSVファイルの読み込みに失敗しました table=%s", table_name)
        return ImportResult(success=False, error=AccessBridgeError(ErrorKind.UNKNOWN, None, str(exc)))
    finally:
        try:
            os.unlink(csv_path)
        except OSError:
            pass


def import_shift_times(
    accdb_path: Path, table_name: str = "時間用", runner: Optional[ScriptRunner] = None
) -> Optional[dict[str, tuple[str, str]]]:
    """Convenience wrapper mirroring ``Set_Shift`` / ``shiftTime``: reads
    the 直/開始/終了 columns and returns a ``{shift_key: (start, end)}``
    map ready for ``NippouRepository.set_shift_time``."""
    result = import_table(accdb_path, table_name, runner=runner)
    if not result.success:
        return None

    times: dict[str, tuple[str, str]] = {}
    for row in result.rows:
        shift_key = row.get("直", "").strip()
        start = row.get("開始", "").strip()
        end = row.get("終了", "").strip()
        if shift_key:
            times[shift_key] = (start, end)
    return times
