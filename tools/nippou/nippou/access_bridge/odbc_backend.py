"""Access接続のODBC実装(``dbkit.access_odbc`` ベース)。

``SETTINGS.access_backend == "odbc"`` のときに、``runner.py`` +
``script_gen.py``(VBScript生成 + ``cscript.exe`` 起動)の代わりに使われる
経路。戻り値の形(``ImportResult`` / ``ScriptResult``)はVBScript経路に
合わせてあるので、呼び出し側(``importer.py`` / ``pusher.py``)の分岐は
「どちらの関数を呼ぶか」だけで済む。

SQL文の組み立て(``pusher.build_header_statements`` 等)は既存のまま
共有する -- 「①取込み/③反映で何を送るか」は業務ロジック、「Accessに
どう繋いでどう実行するか」は接続方式、という分離を保つため。

【VBScript経路との違い】
- サブプロセス起動(cscript.exe)・一時ファイルが無いぶん軽い。
- 反映(③)はADODBのBeginTrans/CommitTrans/RollbackTransの代わりに
  ``AccessConnection.transaction()``(ODBCの自動コミットOFF/SQLEndTran)
  でまとめる。ロック競合時の再試行は文単位(``AccessConnection.execute``
  が内部で行う)で、VBScript経路のようにトランザクション全体を
  作り直しての再試行ではない -- 個々の文の再試行で足りる想定だが、
  実機(Windows+Accessドライバ)での検証はまだ済んでいない。
"""
from __future__ import annotations

from pathlib import Path

from dbkit import access_odbc

from ..logging_setup import get_logger
from .errors import AccessBridgeError, ErrorKind, classify_error
from .runner import ScriptResult

_logger = get_logger("access_bridge.odbc_backend")


def _classify(exc: access_odbc.AccessError) -> AccessBridgeError:
    """``access_odbc.AccessError`` をVBScript経路と同じ ``AccessBridgeError``
    に分類し直す。ODBC側にはADOのエラー番号が無いので ``err_number`` は
    常に ``None`` (``classify_error`` はメッセージ本文だけでもロック競合等を
    判定できるので、それで代用する)。"""
    return classify_error(None, str(exc))


def import_table_odbc(accdb_path: Path, table_name: str, sql_filter: str = "") -> "ImportResult":
    """VBScript経路の ``importer.import_table`` に相当するODBC版。"""
    from .importer import ImportResult  # 遅延import: importer<->odbc_backend の循環を避ける

    where = f" WHERE {sql_filter}" if sql_filter else ""
    try:
        with access_odbc.connect(accdb_path, read_only=True) as conn:
            rows = conn.query(
                f"SELECT * FROM {access_odbc.quote_identifier(table_name)}{where}")
        _logger.info("import ok(odbc) table=%s rows=%d", table_name, len(rows))
        return ImportResult(success=True, rows=rows)
    except access_odbc.AccessError as exc:
        _logger.error("import failed(odbc) table=%s error=%s", table_name, exc)
        return ImportResult(success=False, error=_classify(exc))
    except Exception as exc:  # noqa: BLE001 - 想定外の例外で呼び出し元(他ライン)を落とさない
        _logger.exception("import中に予期しないエラー(odbc) table=%s", table_name)
        return ImportResult(success=False, error=AccessBridgeError(ErrorKind.UNKNOWN, None, str(exc)))


def push_statements_odbc(accdb_path: Path, statements: list[str]) -> ScriptResult:
    """VBScript経路の ``build_push_script`` + ``run_with_retry`` に相当する
    ODBC版。``statements`` (1キー分のDELETE+INSERT群)を1つのAccess
    トランザクションとして実行し、途中で失敗したらロールバックする。"""
    try:
        with access_odbc.connect(accdb_path) as conn:
            with conn.transaction():
                for stmt in statements:
                    conn.execute(stmt)
        return ScriptResult(success=True, rows=len(statements))
    except access_odbc.AccessError as exc:
        _logger.error("push failed(odbc) error=%s", exc)
        return ScriptResult(success=False, err_desc=str(exc), error=_classify(exc))
    except Exception as exc:  # noqa: BLE001 - 想定外の例外で呼び出し元(他ライン)を落とさない
        _logger.exception("push中に予期しないエラー(odbc)")
        return ScriptResult(
            success=False, err_desc=str(exc),
            error=AccessBridgeError(ErrorKind.UNKNOWN, None, str(exc)))
