"""ログ出力。VBA の ``DebugLog`` / ``UserLog`` に相当する。

VBA 版は ``<basePath>\\<ブック名>\\<ライン名>\\DebugLog_yyyymmdd_hhmmss.txt`` に
追記していた。ここでも同じ階層構造を維持し、ライン名が取得できない場合は
``Unknown`` フォルダに出す(従来仕様を踏襲)。

VBA 版はログ 1 行ごとにファイルを開き直していたため書き込みが遅かった。
Python 版では ``logging`` のハンドラを 1 つ保持して使い回す。
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable

LOGGER_NAME = "kanban"
#: dbkit(汎用DB層)側のログもこのアプリの DebugLog に合流させる。
#: dbkit は kanban を知らない(他プロジェクトでも使い回すため)ので、
#: 各自の logging.getLogger("dbkit") へ handler を共有する側で束ねる。
_DBKIT_LOGGER_NAME = "dbkit"
_UNKNOWN_LINE = "Unknown"

_logger: logging.Logger | None = None
_log_path: Path | None = None
_user_sinks: list[Callable[[str], None]] = []
#: いまの出力先(設定で置き場所を変えたとき、同じ名前で開き直すため)
_settings: dict[str, object] = {}


def initialize(
    log_dir: str | os.PathLike[str],
    line_name: str = "",
    app_name: str = "KanbanSystem",
    echo_to_stderr: bool = False,
) -> Path | None:
    """ログファイルを初期化する(VBA の ``InitializeDebugLog``)。

    戻り値は作成したログファイルのパス。ネットワーク共有に書けない場合は
    一時フォルダへフォールバックし、それも失敗した場合は None を返す。
    """
    global _logger, _log_path
    from . import trace

    _settings.update(log_dir=str(log_dir), line_name=line_name, app_name=app_name,
                     echo_to_stderr=echo_to_stderr)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    line = _sanitize(line_name) or _UNKNOWN_LINE
    # **重さ(INFO / WARNING / ERROR)と、どこが書いたか**を 1 行ごとに残す。警告以上には
    # 記録番号を付ける(記録 ``記録/*.jsonl`` の同じ番号の 1 件に、原因の連鎖まで入っている)
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s%(trace_tail)s",
        datefmt="%Y/%m/%d %H:%M:%S",
    )
    ref_filter = trace.RefFilter()

    path = None
    for base in _candidate_dirs(log_dir, app_name, line):
        try:
            base.mkdir(parents=True, exist_ok=True)
            candidate = base / f"DebugLog_{datetime.now():%Y%m%d_%H%M%S}.txt"
            handler = logging.FileHandler(candidate, encoding="utf-8")
            handler.setFormatter(formatter)
            handler.addFilter(ref_filter)
            logger.addHandler(handler)
            path = candidate
            # 記録(後追い・なぜなぜ用)も同じ置き場所の下へ
            trace.configure(base.parent.parent)
            break
        except OSError:
            continue

    if echo_to_stderr:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        stream.addFilter(ref_filter)
        logger.addHandler(stream)

    # 警告以上を記録へ(原因の連鎖・要求・状態つき)
    record_handler = trace.Handler()
    record_handler.addFilter(ref_filter)
    logger.addHandler(record_handler)

    if not logger.handlers:
        logger.addHandler(logging.NullHandler())

    dbkit_logger = logging.getLogger(_DBKIT_LOGGER_NAME)
    dbkit_logger.setLevel(logging.DEBUG)
    dbkit_logger.propagate = False
    for handler in list(dbkit_logger.handlers):
        dbkit_logger.removeHandler(handler)
    for handler in logger.handlers:
        dbkit_logger.addHandler(handler)

    _logger = logger
    _log_path = path

    if path is not None:
        logger.info("=========================================")
        logger.info("デバッグログ開始")
        # **どの版が書いたログかを、ログ自身に残す。**
        # 送られてきたログだけを見て調べることになるので、ここに無いと
        # 「その不具合はもう直っている版では？」を確かめる手がなくなる。
        # 版が読めなくてもログは書く(調べる手段を先に潰さない)
        logger.info("バージョン: %s", _version_text())
        logger.info("ライン名: %s", line)
        logger.info("ログファイル: %s", path)
        logger.info("=========================================")
    return path


def reinitialize(log_dir: str | os.PathLike[str]) -> Path | None:
    """置き場所を変えて開き直す(設定画面でログの置き場所を変えたとき。アプリは止めない)。"""
    old = _log_path
    if _logger is not None and old is not None:
        _logger.info("ログの置き場所を変えます: %s", log_dir)
    path = initialize(log_dir, line_name=str(_settings.get("line_name", "")),
                      app_name=str(_settings.get("app_name", "KanbanSystem")),
                      echo_to_stderr=bool(_settings.get("echo_to_stderr", False)))
    if _logger is not None and old is not None:
        _logger.info("前のログ: %s", old)
    return path


def current_dir() -> str:
    """いまの置き場所(設定どおり。書けずに一時フォルダへ逃げたときは実際の場所を :func:`log_path` で)。"""
    return str(_settings.get("log_dir", ""))


def _version_text() -> str:
    """版の表示。読めなければ理由を添える(黙って空にしない)。"""
    try:
        from . import app_config

        return app_config.version_label()
    except Exception as exc:  # noqa: BLE001 - ログのためにアプリを止めない
        return f"(取得できません: {exc})"


def _candidate_dirs(
    log_dir: str | os.PathLike[str], app_name: str, line: str
) -> list[Path]:
    """書き込み先の候補(優先順)。"""
    import tempfile

    primary = Path(log_dir) / app_name / line
    fallback = Path(tempfile.gettempdir()) / app_name / line
    return [primary, fallback]


def _sanitize(value: str) -> str:
    """制御文字・全角空白・パス区切りを除去する。

    VBA 版の ``SanitizeRegistryValue`` と同じ意図。見えない文字が混ざると
    「ラインによってフォルダが作られない」原因になる。
    """
    out = []
    for ch in value or "":
        code = ord(ch)
        if code < 32 or code == 127 or code == 0x3000:
            continue
        if ch in '\\/:*?"<>|':
            continue
        out.append(ch)
    return "".join(out).strip()


def logger() -> logging.Logger:
    """ロガーを返す(未初期化ならフォールバックを構成する)。"""
    global _logger
    if _logger is None:
        log = logging.getLogger(LOGGER_NAME)
        log.addHandler(logging.NullHandler())
        log.propagate = False
        _logger = log
    return _logger


def log_path() -> Path | None:
    return _log_path


def get_logger(name: str) -> logging.Logger:
    """区分名つきのロガーを返す(起動基盤が使う)。

    ``kanban`` ロガーの子として作るので、出力先・書式は :func:`initialize`
    が構成したものをそのまま使う。基盤仕様書 2.6 が求める「起動前に失敗
    したのか / Web サーバが失敗したのか / 業務処理で失敗したのか」の区別は、
    ファイルを分けるのではなく **この名前** で行う(1 つのログを時系列で
    読めるほうが、起動の失敗を追うには都合がよい)。

    ``launch_guard`` のようにアプリ本体より先に動くモジュールからも
    呼ばれるため、まだ :func:`initialize` されていなくても失敗しない
    (:func:`logger` が NullHandler を付けたフォールバックを返す)。
    """
    logger()  # 親ロガーの構成を保証してから子を作る
    return logging.getLogger(f"{LOGGER_NAME}.{name}")


def debug(message: str, *args: object) -> None:
    """VBA の ``DebugLog`` 相当。"""
    logger().debug(message, *args, stacklevel=2)


def info(message: str, *args: object) -> None:
    logger().info(message, *args, stacklevel=2)


def warning(message: str, *args: object) -> None:
    logger().warning(message, *args, stacklevel=2)


def error(message: str, *args: object) -> None:
    logger().error(message, *args, stacklevel=2)


def exception(message: str, *args: object) -> None:
    logger().exception(message, *args, stacklevel=2)


def close() -> None:
    """ログを閉じる(VBA の ``CloseDebugLog``)。"""
    global _logger, _log_path
    if _logger is None:
        return
    if _log_path is not None:
        _logger.info("=========================================")
        _logger.info("デバッグログ終了")
        _logger.info("=========================================")

    # **dbkit 側からも外してから閉じる。**
    #
    # `initialize` は同じハンドラの**実体**を dbkit のロガーにも付けている。
    # こちらから外すだけだと、閉じたハンドラが向こうに残り、以後 dbkit が
    # 1 行でも書こうとした瞬間に「閉じたファイルへの操作」で落ちる
    # (終了処理と入れ違いに動いているスレッドが踏みうる)。
    dbkit_logger = logging.getLogger(_DBKIT_LOGGER_NAME)
    for handler in list(_logger.handlers):
        _logger.removeHandler(handler)
        dbkit_logger.removeHandler(handler)
        handler.close()
    # 付け替えの取りこぼしがあっても、閉じたものを残さない
    for handler in list(dbkit_logger.handlers):
        dbkit_logger.removeHandler(handler)
    dbkit_logger.addHandler(logging.NullHandler())

    _logger = None
    _log_path = None


# --- ユーザー向けログ(画面表示) --------------------------------------------
def add_user_sink(sink: Callable[[str], None]) -> None:
    """ユーザー向けログの出力先(画面ウィジェット等)を登録する。"""
    if sink not in _user_sinks:
        _user_sinks.append(sink)


def remove_user_sink(sink: Callable[[str], None]) -> None:
    if sink in _user_sinks:
        _user_sinks.remove(sink)


def user(message: str, separator: bool = False) -> None:
    """VBA の ``UserLog`` 相当。デバッグログにも同時に記録する。"""
    info(message)
    text = f"──── {message} ────" if separator else message
    for sink in list(_user_sinks):
        try:
            sink(text)
        except Exception:  # 画面表示の失敗で業務を止めない
            pass
