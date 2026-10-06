r"""ログ出力 (VBA ``DebugLog`` / ``InitializeDebugLog`` 相当)

【出力先】(基盤仕様書 2.6 / 2.7)
``app_config.local_dir("logs")/calendar_YYYYMMDD.log``。
アプリ本体は共有フォルダに置かれることがあるので、**実行中に変化する
ファイルは利用者ごとのローカル領域へ**置く。

**日付が変わったら書き先も移る**(``DailyFileHandler``)。このツールは
朝に開いて一日中そのまま、という使い方をされるので、名前を起動時に1回
決めてしまうと1つのファイルに何週間ぶんも溜まる。古いものは
``KEEP_DAYS`` 日で消す。

VBA 版は共有フォルダ配下に ``<ブック名>\<ライン名>\DebugLog_*.txt`` を
作っていた。ライン名でフォルダを分けていたのは共有先で混ざらないためだが、
Python 版のログはもともと PC ごとのローカル領域にあり、**1台 = 1ライン**
なので、分ける意味が無い。代わりに1行ごとに出どころ(ロガー名)を出す。

【出どころで切り分ける】
基盤仕様書 2.6 が求めているのは「起動前に失敗したのか」「Webサーバが
失敗したのか」「業務処理で失敗したのか」を区別できることで、
ファイルを分けること自体ではない。名前で足りる:

    launcher / launch_guard / server / boot_server   起動基盤
    app.routes.*                                     Web の受け口
    repository / sync.* / importer / sources          業務処理

【置き場所を変える】(設定画面「9. ログ」)
``settings.json`` の ``ログフォルダ`` に書けば、そこへ書く。**PC名の
フォルダを1段はさむ** ── 共有フォルダを指定して全台が同じ場所へ書いても、
混ざらず、掃除で他の端末のものを消さない::

    <指定したフォルダ>\<PC名>\calendar_YYYYMMDD.log

指定先に書けないとき(フォルダが無い・ネットワークが切れた)は
**ローカル領域へ書き続ける**。ログが書けないことを理由にアプリを
止めないのと同じで、「ログが要る場面ほどネットワークが不調」だから。
切り替えたことはローカル側の先頭に1行残し、設定画面にも出す。

【後から追えるように】(なぜなぜ分析)
1行ごとに次を載せる::

    日時(ミリ秒) | 重さ | 出どころ | スレッド | 追跡 | 本文

* **追跡** … 画面からの要求1回ごとの番号 ``r=XXXXXX``、同期1回ごとの
  ``s=XXXXXX``。同じ番号の行を拾えば、その操作で何が起きたかが揃う
* **記録番号** … ERROR 以上の行には ``記録番号=E20261001-142233-K3Q`` を
  付ける。画面にも同じ番号を出すので、利用者が言った番号から
  その行へたどれる(``calendar_app/log_report.py``)

【dbkit】
``calendar_app.dbkit`` は他プロジェクトでも使う共通DB基盤なので、
既定では ``NullHandler`` しか持たない。出力先はここで初めて決まる。
"""

from __future__ import annotations

import contextvars
import logging
import os
import platform
import re
import secrets
import sys
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

_ROOT_NAME = "calendar_app"
_configured = False

#: ログを残す日数。これより古い ``calendar_*.log`` は起動時と日付が
#: 変わった時に消す。
#:
#: **開けっぱなしにされる前提で決めている。** 20秒ごとの同期が1日あたり
#: 2万行ほど出すので、1日2〜3MB になる。上限を置かないと
#: ローカル領域が静かに膨らみ続ける(控えを10世代で打ち止めているのと
#: 同じ考え方)。
KEEP_DAYS = 30

_FILE_PREFIX = "calendar_"
_FILE_SUFFIX = ".log"


#: 1行の形。``log_report`` がこの形を読むので、変えたらあちらも直す
LINE_FORMAT = ("%(asctime)s.%(msecs)03d | %(levelname)s | %(name)s | "
               "%(threadName)s | %(trace)s | %(message)s")
DATE_FORMAT = "%Y/%m/%d %H:%M:%S"

#: 記録番号を付ける重さ。画面に「記録番号 ○○ を伝えてください」と出すのはこれ以上
REF_LEVEL = logging.ERROR


def _log_name(when: date) -> str:
    return f"{_FILE_PREFIX}{when:%Y%m%d}{_FILE_SUFFIX}"


# ---------------------------------------------------------------------------
# 追跡番号と記録番号
# ---------------------------------------------------------------------------
#: 読み違えにくい字だけ(0/O・1/I/L・2/Z・5/S・8/B を外す)。
#: 電話で読み上げてもらうことがある
_ALPHABET = "ACDEFHJKMNPQRTUVWXY3479"

#: いまの処理の追跡番号。要求ごと・同期1回ごとに立てる。
#: ``ContextVar`` なので、スレッドが違えば混ざらない
_trace: contextvars.ContextVar[str] = contextvars.ContextVar("trace", default="-")


def new_id(length: int = 6) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def new_ref(now: Optional[datetime] = None) -> str:
    """記録番号。**いつのものかが番号だけで分かる**形にする。

    例 ``E20261001-142233-K3Q``。日時が入っているので、集めたログが
    何十台ぶんあっても、まず日付のファイルに絞れる。
    """
    return f"E{(now or datetime.now()):%Y%m%d-%H%M%S}-{new_id(3)}"


REF_PATTERN = re.compile(r"記録番号=(E\d{8}-\d{6}-[A-Z0-9]{3})")


def set_trace(value: str) -> contextvars.Token:
    """いまの処理に追跡番号を立てる。戻り値は ``reset_trace`` に渡す。"""
    return _trace.set(value or "-")


def reset_trace(token: contextvars.Token) -> None:
    try:
        _trace.reset(token)
    except ValueError:
        _trace.set("-")              # 別の文脈で立てたものは戻せない


def current_trace() -> str:
    return _trace.get()


class _TraceFilter(logging.Filter):
    """各行に追跡番号を、ERROR 以上には記録番号を載せる。

    **ハンドラに付ける**(ロガーではなく)。flask・waitress など
    よそのロガーの行も同じ形にそろうため。何度通っても1回だけ付ける。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "trace_done"):
            trace = getattr(record, "trace", None) or _trace.get()
            ref = getattr(record, "ref", "")
            if not ref and record.levelno >= REF_LEVEL:
                ref = new_ref(datetime.fromtimestamp(record.created))
            record.ref = ref
            record.trace = f"{trace} 記録番号={ref}" if ref else trace
            record.trace_done = True
        return True


class DailyFileHandler(logging.FileHandler):
    """日付が変わったら、その日のファイルへ書き移るハンドラ。

    **素の ``FileHandler`` では足りない。** ファイル名を起動時に1回だけ
    決めるので、朝に開いて月末まで開けっぱなし、という使い方をすると
    9月ぶん全部が ``calendar_20260901.log`` に入る。日付でログを探す運用が
    崩れるうえ、1ファイルが数十MBになる。

    ``TimedRotatingFileHandler`` を使わないのは、あちらが付ける名前
    (``calendar_20260901.log.2026-09-02``)が既にある決め事から外れるため。
    **いまのログは必ず今日の日付のファイル**、が読む側にとっていちばん
    分かりやすい。

    ``fallback`` を渡すと、``folder`` に書けないときはそちらへ書く
    (指定したログフォルダが共有で、ネットワークが切れた場合など)。
    元へ戻すのは**日付が変わったとき**と、設定を保存し直したとき
    (``reconfigure``)。1行ごとに見に行くと、共有が止まっている間
    1行ごとに数秒待たされる。
    """

    def __init__(self, folder: Path, *, keep_days: int = KEEP_DAYS,
                 fallback: Optional[Path] = None) -> None:
        self.preferred = folder
        self.fallback = fallback if fallback and fallback != folder else None
        self.folder = folder
        self.keep_days = keep_days
        self.fallback_reason = ""
        self._day = date.today()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            super().__init__(folder / _log_name(self._day), encoding="utf-8")
        except OSError as exc:
            if self.fallback is None:
                raise
            self._use_fallback(exc, init=True)
        _prune(self.folder, keep_days)

    # ------------------------------------------------------------------
    def _use_fallback(self, exc: BaseException, *, init: bool = False) -> None:
        assert self.fallback is not None
        self.fallback_reason = f"{self.preferred} に書けません: {exc}"
        self.folder = self.fallback
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / _log_name(self._day)
        if init:
            super().__init__(path, encoding="utf-8")
        else:
            self._close_stream()
            self.baseFilename = str(path)
            self.stream = self._open()
        # **切り替えたことを書き先に残す。** 後で読む人が、指定先のログが
        # 途中で途切れている理由をここで知る
        self.stream.write(
            f"{datetime.now():{DATE_FORMAT}}.000 | WARNING | {_ROOT_NAME}.log | "
            f"{threading.current_thread().name} | - | "
            f"ログフォルダに書けないので、ここへ書きます: {self.fallback_reason}\n")
        self.stream.flush()

    def _close_stream(self) -> None:
        try:
            if self.stream:
                self.stream.close()
        except OSError:
            pass
        self.stream = None  # type: ignore[assignment]

    def emit(self, record: logging.LogRecord) -> None:
        # 記録した時刻で見る(日付が変わる瞬間の1行を取りこぼさない)
        today = datetime.fromtimestamp(record.created).date()
        if today != self._day:
            self._roll(today)
        try:
            if self.stream is None:
                self.stream = self._open()
            self.stream.write(self.format(record) + self.terminator)
            self.stream.flush()
        except OSError as exc:
            # 指定先が途中で書けなくなった。**ローカルへ移って同じ行を書く**
            if self.fallback is None or self.folder == self.fallback:
                self.handleError(record)
                return
            try:
                self._use_fallback(exc)
                self.stream.write(self.format(record) + self.terminator)
                self.stream.flush()
            except OSError:
                self.handleError(record)
        except Exception:                          # noqa: BLE001 - 形の誤りなど
            self.handleError(record)

    def _roll(self, today: date) -> None:
        """書き先を今日のファイルへ移す。**失敗しても書き続ける。**

        ローカルへ逃げていたなら、ここで指定先へ戻れるか試す。
        """
        self._day = today
        target = self.preferred
        try:
            target.mkdir(parents=True, exist_ok=True)
            self._close_stream()
            self.folder = target
            self.baseFilename = str(target / _log_name(today))
            self.stream = self._open()
            self.fallback_reason = ""
        except OSError as exc:
            if self.fallback is None:
                return                  # 新しい方を開けなくても、アプリは止めない
            try:
                self._use_fallback(exc)
            except OSError:
                return
        _prune(self.folder, self.keep_days)


def _prune(folder: Path, keep_days: int) -> None:
    """古いログを消す。**消せなくても黙って進む**(ログの掃除で止めない)。"""
    if keep_days <= 0:
        return
    limit = date.today().toordinal() - keep_days
    try:
        for path in folder.glob(f"{_FILE_PREFIX}*{_FILE_SUFFIX}"):
            stamp = path.stem[len(_FILE_PREFIX):]
            try:
                when = datetime.strptime(stamp, "%Y%m%d").date()
            except ValueError:
                continue                    # 名前が違うものは触らない
            if when.toordinal() < limit:
                path.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 置き場所
# ---------------------------------------------------------------------------
#: いま付けているファイル用ハンドラ(``reconfigure`` で付け替える)
_file_handler: Optional[DailyFileHandler] = None
_setup_lock = threading.RLock()

#: よそのロガーも同じ書き先へ集める: (名前, 重さ)
#:   dbkit   … 共通DB基盤。calendar_app に依存しないので自分では書き先を持たない
#:   waitress … 待ち受け。要求の外で起きた例外・待ち行列のあふれ
#:   flask/werkzeug … 万一こちらの受け口を通らなかった例外
_FOREIGN = (("dbkit", logging.DEBUG), ("waitress", logging.WARNING),
            ("flask.app", logging.WARNING), ("werkzeug", logging.WARNING))

_INVALID_NAME = re.compile(r'[\\/:*?"<>|]+')


def default_dir() -> Path:
    """既定の置き場所(``%LOCALAPPDATA%\\LineCalendar\\logs``)。"""
    from . import app_config

    return app_config.local_dir("logs")


def _pc_name() -> str:
    try:
        from .terminals import identity

        name = identity().pc_name
    except Exception:                              # noqa: BLE001 - 取り込み途中など
        name = os.environ.get("CALENDAR_PC_NAME") or platform.node()
    return _INVALID_NAME.sub("_", (name or "").strip()) or "PC"


def configured_text() -> str:
    """設定に書いてあるログフォルダ(打たれたまま)。未設定なら空文字。"""
    try:
        from . import settings as user_settings

        return user_settings.log_dir_setting()
    except Exception:                              # noqa: BLE001 - 設定が壊れていても書く
        return ""


def folder_for(text: str) -> Path:
    """設定の文字列から、実際に書くフォルダを決める(PC名を1段はさむ)。

    空なら既定の置き場所(こちらは端末ごとのローカルなので、はさまない)。
    """
    if not (text or "").strip():
        return default_dir()
    from . import config

    return config.resolve_dir(text) / _pc_name()


def _formatter() -> logging.Formatter:
    return logging.Formatter(LINE_FORMAT, datefmt=DATE_FORMAT)


def _make_file_handler() -> Optional[DailyFileHandler]:
    fallback: Optional[Path]
    try:
        fallback = default_dir()
    except Exception:                              # noqa: BLE001
        fallback = None
    text = configured_text()
    try:
        preferred = folder_for(text) if text else fallback
    except ValueError:
        preferred = fallback
    if preferred is None:
        return None
    try:
        handler = DailyFileHandler(preferred, fallback=fallback)
    except OSError:
        return None
    handler.setFormatter(_formatter())
    handler.addFilter(_TraceFilter())
    return handler


def _loggers() -> list[logging.Logger]:
    return [logging.getLogger(_ROOT_NAME)] + [logging.getLogger(n) for n, _ in _FOREIGN]


def configure_logging(*, verbose: bool = False) -> None:
    """アプリ起動時に一度だけ呼ぶ。二重呼び出しは無害(冪等)。"""
    global _configured, _file_handler
    with _setup_lock:
        if _configured:
            return
        # 先に立てる。この下でどこかが get_logger() を呼んでも無限再帰しない
        _configured = True

        root = logging.getLogger(_ROOT_NAME)
        root.setLevel(logging.DEBUG)
        root.propagate = False
        for name, level in _FOREIGN:
            other = logging.getLogger(name)
            other.setLevel(level)
            other.propagate = False

        # ログが書けないことを理由にアプリを止めない。
        # 書けなければコンソールだけに出して続行する
        _file_handler = _make_file_handler()
        if _file_handler is not None:
            for logger in _loggers():
                logger.addHandler(_file_handler)

        # コンソールが無いときは付けない。
        # **``pythonw.exe`` では ``sys.stderr`` が ``None``**(Start.vbs は
        # 画面を出さないために pythonw を使う)。``StreamHandler()`` はそのとき
        # ``stream = None`` を抱え、1行出すたびに ``None.write`` で例外を起こす。
        # ``logging`` が握りつぶすので表には出ないが、誰にも見えない例外を
        # 出力のたびに払うことになる。
        if _has_console():
            console = logging.StreamHandler()
            console.setLevel(logging.DEBUG if verbose else logging.INFO)
            console.setFormatter(_formatter())
            console.addFilter(_TraceFilter())
            for logger in _loggers():
                logger.addHandler(console)

        _install_excepthooks()


def reconfigure() -> dict[str, Any]:
    """設定を変えたあと、書き先を付け替える(**再起動しなくても効く**)。

    書き先が変わったら、新しい書き先にも**見出し**(版・端末・ライン・
    参照パス)を残す。起動の記録は前の書き先にしか無いので、新しい
    書き先のログだけを渡された人が「どの端末の・どの設定のものか」を
    読めなくなる(``log_report`` はこの見出しを拾う)。
    """
    global _file_handler
    configure_logging()
    with _setup_lock:
        new = _make_file_handler()
        old = _file_handler
        for logger in _loggers():
            if old is not None:
                logger.removeHandler(old)
            if new is not None:
                logger.addHandler(new)
        _file_handler = new
        if old is not None:
            old.close()
        moved = (new is not None and
                 (old is None or Path(old.baseFilename) != Path(new.baseFilename)))
    if moved:
        write_header(HEADER_MOVED)
    return status()


# ---------------------------------------------------------------------------
# 見出し ── どの版の・どの端末の・どの設定のログか
# ---------------------------------------------------------------------------
#: 見出しの1行目。``log_report`` がこれを目印に「起動時の様子」を拾う
HEADER_BOOT = "起動:"
HEADER_MOVED = "見出し:"
HEADER_MARKS = (HEADER_BOOT, HEADER_MOVED)


def write_header(mark: str = HEADER_BOOT) -> None:
    """見出しを書く。**起動のたびと、書き先が変わったとき。**

    なぜなぜ分析は「その端末で何が違ったか」から始まることが多いので、
    版だけでなく端末・ライン・参照パス・ログの書き先まで残す。
    """
    from . import app_config

    log = get_logger("launcher")
    log.info("=" * 60)
    # **版を1枚目に残す。** あとからログだけを渡されたときに、
    # どの版が書いたものか分からないと読み解けない
    if mark == HEADER_BOOT:
        log.info("%s pid=%s %s", HEADER_BOOT, os.getpid(), app_config.version_label())
        log.info("Python: %s (%s)", sys.version.split()[0], sys.executable)
        log.info("アプリ本体: %s", app_config.APP_ROOT)
        log.info("ローカル領域: %s", app_config.local_root())
    else:
        log.info("%s ログの書き先が変わったので、ここにも残します pid=%s %s",
                 HEADER_MOVED, os.getpid(), app_config.version_label())
    try:
        from . import settings as user_settings
        from .terminals import identity

        who = identity()
        log.info("端末: PC名=%s ログインID=%s ライン=%s", who.pc_name,
                 who.login_id, user_settings.get_my_line() or "(未設定)")
        log.info("参照パス: 保存用DB=%s マスタDB=%s",
                 user_settings.data_db_dir_setting() or "(既定)",
                 user_settings.master_db_dir_setting() or "(既定)")
    except Exception as exc:                       # noqa: BLE001 - 記録で止めない
        log.warning("端末の情報を残せませんでした: %s", exc)
    where = status()
    log.info("ログの書き先: %s", where["folder"])
    if where["fallback_reason"]:
        log.warning("指定のログフォルダに書けません: %s", where["fallback_reason"])


def status() -> dict[str, Any]:
    """いまどこへ書いているか(設定画面と起動の記録に出す)。"""
    configure_logging()
    handler = _file_handler
    text = configured_text()
    try:
        default = str(default_dir())
    except Exception:                              # noqa: BLE001
        default = ""
    return {
        "setting": text,
        "default": default,
        "folder": str(handler.folder) if handler else "",
        "file": handler.baseFilename if handler else "",
        "fallback_reason": handler.fallback_reason if handler else
                           "ログファイルを開けませんでした(コンソールにだけ出しています)",
        "keep_days": KEEP_DAYS,
    }


def current_folder() -> str:
    """いま書いているフォルダ(エラー画面の「ログはここ」に出す)。"""
    try:
        return status()["folder"] or str(default_dir())
    except Exception:                              # noqa: BLE001 - 報告で失敗しない
        return "(ログの置き場所を特定できませんでした)"


def folders_to_read() -> list[Path]:
    """後追いで読むフォルダ。いまの書き先と既定の置き場所(逃げた先)。"""
    configure_logging()
    found: list[Path] = []
    candidates: list[Optional[Path]] = []
    handler = _file_handler
    if handler is not None:
        candidates += [handler.preferred, handler.folder]
    try:
        candidates.append(default_dir())
    except Exception:                              # noqa: BLE001
        pass
    for path in candidates:
        if path is not None and path not in found:
            found.append(path)
    return found


# ---------------------------------------------------------------------------
# どこにも捕まらなかった例外
# ---------------------------------------------------------------------------
_hooks_installed = False


def _install_excepthooks() -> None:
    """**裏のスレッドで落ちた例外を、黙って消さない。**

    ``threading.Thread`` の中で捕まらなかった例外は、既定では
    標準エラーに出るだけで、``pythonw.exe`` ではそれも消える。
    同期の見張りが黙って止まった、のように「いつから・なぜ」が
    いちばん追いにくい壊れ方になるので、記録番号付きで残す。
    """
    global _hooks_installed
    if _hooks_installed:
        return
    _hooks_installed = True
    log = logging.getLogger(f"{_ROOT_NAME}.uncaught")

    original_thread = threading.excepthook

    def thread_hook(args: Any) -> None:
        if args.exc_type is not SystemExit:
            name = args.thread.name if args.thread else "?"
            log.critical("スレッド %s が例外で止まりました: %s: %s", name,
                         args.exc_type.__name__, args.exc_value,
                         exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        original_thread(args)

    original_sys = sys.excepthook

    def sys_hook(exc_type: Any, exc: Any, tb: Any) -> None:
        if not issubclass(exc_type, KeyboardInterrupt):
            log.critical("捕まらなかった例外で終わります: %s: %s",
                         exc_type.__name__, exc, exc_info=(exc_type, exc, tb))
        original_sys(exc_type, exc, tb)

    threading.excepthook = thread_hook
    sys.excepthook = sys_hook


def _has_console() -> bool:
    """標準エラー出力に書けるか。

    ``pythonw.exe`` は ``sys.stderr`` が ``None``。リダイレクト先が閉じて
    いる場合に備えて ``write`` を持つかどうかまで見る。
    """
    stream = getattr(sys, "stderr", None)
    return stream is not None and hasattr(stream, "write")


def get_logger(name: str = "") -> logging.Logger:
    """出どころ別のロガーを取る(未初期化なら自動で初期化する)。"""
    configure_logging()
    return logging.getLogger(f"{_ROOT_NAME}.{name}" if name else _ROOT_NAME)


def debug_log(message: str) -> None:
    """VBA の ``DebugLog`` と同じ感覚で使える簡易関数。"""
    get_logger().debug(message)


def silence_console() -> None:
    """コンソールへの出力だけを止める(ファイルへの出力は残す)。

    テストや診断スクリプトのように、**そのプログラム自身の出力が主役**で
    あって業務ログが主役ではない場面のためのもの。

    ``get_logger()`` は取り込み時に呼ばれることがあり、その時点で既に
    コンソール用ハンドラが付いている。あとから ``configure_logging()`` が
    走る場合もあるので、既存のハンドラを外すだけでなく、以降の追加も弾く。
    """
    for logger in _loggers():
        for handler in list(logger.handlers):
            if _is_console(handler):
                logger.removeHandler(handler)

        if getattr(logger, "_console_silenced", False):
            continue                        # 二重に包まない(冪等)

        original_add = logger.addHandler

        def add_handler(handler: logging.Handler, _add=original_add) -> None:
            if _is_console(handler):
                return
            _add(handler)

        logger.addHandler = add_handler      # type: ignore[method-assign]
        logger._console_silenced = True      # type: ignore[attr-defined]


def _is_console(handler: logging.Handler) -> bool:
    # FileHandler は StreamHandler の派生なので先に除外する
    return (isinstance(handler, logging.StreamHandler)
            and not isinstance(handler, logging.FileHandler))
