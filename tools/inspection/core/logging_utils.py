"""ログ出力

- 出力先: 既定はローカル領域の `logs/` 配下。**設定でフォルダを指定できる**
  (設定画面の「ログ」タブ・配布設定)。共有フォルダを指定すれば、各ラインの
  ログが1か所に集まり、管理者の席から後追いできる
- **置き場所はいつも `<出力先>/inspection/`**(ツールID のフォルダ。無ければ作る)。
  共有フォルダにはほかのツール(日報・看板…)のログも並ぶので、フォルダでも
  ファイル名でも点検表のものと分かるようにする(取り出したあとも開かずに分かる)
- ファイル名(**名前の作り方はここ1か所**)。必ず `inspection_` で始める

      ローカル           inspection/inspection_YYYYMMDD.log
                         inspection/inspection_events_YYYYMMDD.jsonl
      指定したフォルダ   inspection/inspection_YYYYMMDD_<PC>_<利用者>.log
                         inspection/inspection_events_YYYYMMDD_<PC>_<利用者>.jsonl

  指定したフォルダは複数の端末が共有しうるので、端末と利用者を名前に入れて
  **互いのファイルに書かない**(取り合い・混在を起こさない)
- **VER2.4.5 までの置き場所・名前も読む**(`<出力先>/inspection_….log`・
  `<出力先>/events_….jsonl`)。なぜなぜの検索・一覧と保管日数の片付けが、
  更新前のファイルを見失わないため(移しはしない)。前の場所はほかのツールの
  ファイルと同じ階層なので、**点検表が書いた形の名前と一致するものだけ**を扱う
  (`LEGACY_NAME`)。ほかのツールのファイルは読まない・消さない
- 指定したフォルダに書けないとき(共有が切れた・権限が無い)は、**ローカルへ
  退避して書き続ける**。理由はログと設定画面に出す。5分ごとに戻れるか試す
- 古いログは `log_keep_days`(既定 90 日)で消す。**消すのは自分の端末・自分の
  利用者のファイルだけ**(共有フォルダのほかの端末のものには触れない)
- コンソールがあるとき(start.bat)は同時にコンソールへも出す
- ロガー名で出どころを分ける。「起動前の失敗」「Webサーバの失敗」
  「業務処理の失敗」を、1つのファイルの中で名前によって区別できる

    inspection.launcher      起動入口(環境確認・多重起動・ポート)
    inspection.launch_guard  多重起動の判定
    inspection.server        Webサーバ(待ち受け・停止)
    inspection.idle_exit     自動終了の見張り(表/裏・スリープ・閉じた)
    inspection.app.*         業務(フォルダ検索・プレビュー・印刷・Excel)
    inspection.event         なぜなぜ分析用の出来事(`core/event_log.py` の写し)

設定は Flask より前に要るので、`user_settings.json` を**ここで直接読む**
(`app/` を読み込まない)。
"""
from __future__ import annotations

import getpass
import json
import logging
import os
import platform
import re
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import app_config

ROOT_LOGGER = "inspection"
_configured = False

# user_settings.json の鍵(設定画面・配布設定と同じ)
KEY_DIR = "log_dir"
KEY_KEEP_DAYS = "log_keep_days"
DEFAULT_KEEP_DAYS = 90
MIN_KEEP_DAYS, MAX_KEEP_DAYS = 7, 3650
RETRY_SEC = 300.0                     # 退避中、指定のフォルダへ戻れるか試す間隔
TOOL_ID = "inspection"                # ログの置き場所(`<出力先>/inspection/`)と名前の頭


# ------------------------------------------------------------------
# 端末の名前
# ------------------------------------------------------------------
def _safe(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]+", "-", text or "").strip("-") or "unknown"


def pc_name() -> str:
    return os.environ.get("COMPUTERNAME") or platform.node() or "unknown"


def user_name() -> str:
    try:
        return getpass.getuser()
    except Exception:                                 # noqa: BLE001
        return os.environ.get("USERNAME") or "unknown"


def owner_tag() -> str:
    """ファイル名に入れる「どの端末の誰か」。"""
    return f"{_safe(pc_name())}_{_safe(user_name())}"


# ------------------------------------------------------------------
# 設定(user_settings.json を直接読む)
# ------------------------------------------------------------------
def settings_path() -> Path:
    return app_config.local_dir("data") / "user_settings.json"


def read_settings() -> Dict[str, Any]:
    try:
        with open(settings_path(), "r", encoding="utf-8") as fp:
            data = json.load(fp)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def normalize_dir(value: Any) -> str:
    text = str(value or "").strip().strip('"').strip()
    return os.path.normpath(text) if text else ""


def keep_days(value: Any) -> int:
    try:
        days = int(value)
    except (TypeError, ValueError):
        return DEFAULT_KEEP_DAYS
    return max(MIN_KEEP_DAYS, min(MAX_KEEP_DAYS, days))


def tool_dir(base: Any) -> Path:
    """出力先の中の、点検表のログを置くフォルダ(`<出力先>/inspection/`)。"""
    return Path(base) / TOOL_ID


def check_dir(path: str) -> str:
    """そのフォルダにログを書けなければ理由。書ければ空文字。

    実際に書くのはその下の `inspection/` なので、そこを作って書けるか試す。
    """
    if not os.path.isabs(path):
        return "フルパス(ドライブ名や \\\\サーバ名 から)で指定してください"
    try:
        folder = str(tool_dir(path))
        os.makedirs(folder, exist_ok=True)
        probe = os.path.join(folder, f".write-test-{owner_tag()}-{os.getpid()}")
        with open(probe, "w", encoding="utf-8") as fp:
            fp.write("ok")
        os.remove(probe)
    except OSError as exc:
        return f"{exc.strerror or exc} ({getattr(exc, 'winerror', None) or exc.errno})"
    return ""


# ------------------------------------------------------------------
# いまの書き出し先
# ------------------------------------------------------------------
class _Target:
    """指定・実際の書き出し先・退避の理由。"""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.requested = ""              # 設定で指定したフォルダ(空 = 既定のローカル)
        # 実際の出力先(指定のフォルダか、ローカル)。書くのはその下の `inspection/`
        self.dir: Optional[Path] = None
        self.fallback_reason = ""        # 指定に書けず、ローカルへ退避している理由
        self.retry_at = 0.0
        self.keep_days = DEFAULT_KEEP_DAYS

    def local(self) -> Path:
        return app_config.local_dir("logs")

    def resolve(self, requested: str) -> None:
        with self.lock:
            self.requested = normalize_dir(requested)
            self.fallback_reason = ""
            if self.requested:
                problem = check_dir(self.requested)
                if not problem:
                    self.dir = Path(self.requested)
                    return
                self.fallback_reason = problem
                self.retry_at = time.monotonic() + RETRY_SEC
            self.dir = self.local()
            tool_dir(self.dir).mkdir(parents=True, exist_ok=True)

    def current(self) -> Path:
        with self.lock:
            if self.dir is None:
                settings = read_settings()
                self.keep_days = keep_days(settings.get(KEY_KEEP_DAYS))
                self.resolve(str(settings.get(KEY_DIR) or ""))
            elif self.fallback_reason and time.monotonic() >= self.retry_at:
                # 退避中。指定のフォルダへ戻れるか試す(共有が戻った)
                if not check_dir(self.requested):
                    self.dir = Path(self.requested)
                    self.fallback_reason = ""
                    _recovered.set()
                else:
                    self.retry_at = time.monotonic() + RETRY_SEC
            return self.dir  # type: ignore[return-value]

    def fall_back(self, reason: str) -> bool:
        """書いている途中で書けなくなった。ローカルへ移れたら True。"""
        with self.lock:
            if not self.requested or self.dir == self.local():
                return False
            self.fallback_reason = reason
            self.retry_at = time.monotonic() + RETRY_SEC
            self.dir = self.local()
            tool_dir(self.dir).mkdir(parents=True, exist_ok=True)
            return True

    def is_local(self) -> bool:
        return self.current() == self.local()


_target = _Target()
_recovered = threading.Event()


def log_dir() -> Path:
    """いまログを書いているフォルダ(`<出力先>/inspection/`。退避中ならローカルの)。"""
    return tool_dir(_target.current())


def _stem(kind: str, day: date, local: bool) -> str:
    base = f"{TOOL_ID}_{day:%Y%m%d}" if kind == "log" else f"{TOOL_ID}_{kind}_{day:%Y%m%d}"
    return base if local else f"{base}_{owner_tag()}"


def log_path_for(day: date) -> Path:
    """その日のログファイル。**名前の作り方はここ1か所**。"""
    folder = log_dir()
    return folder / (_stem("log", day, _target.is_local()) + ".log")


def events_path_for(day: date) -> Path:
    folder = log_dir()
    return folder / (_stem("events", day, _target.is_local()) + ".jsonl")


# ------------------------------------------------------------------
# 名前を読む(片付け・なぜなぜの読み出しが使う)
# ------------------------------------------------------------------
# いまの名前: inspection_YYYYMMDD[_<PC>_<利用者>].log / inspection_events_YYYYMMDD[_…].jsonl
OWN_NAME = re.compile(r"^inspection_(?:(events)_)?(\d{8})(?:_([0-9A-Za-z._-]+))?\.(log|jsonl)$")
# VER2.4.5 までの名前(出力先の直下): inspection_YYYYMMDD[_…].log / events_YYYYMMDD[_…].jsonl。
# **出力先の直下はほかのツールのファイルと同じ階層**なので、点検表が書いた形と
# 一字一句合うものだけ(種類と拡張子の組・8けたの日付・端末名の文字)
LEGACY_NAME = re.compile(r"^(inspection|events)_(\d{8})(?:_([0-9A-Za-z._-]+))?\.(log|jsonl)$")


def parse_name(name: str, legacy: bool = False) -> Optional[Dict[str, Any]]:
    """点検表のログの名前なら {kind: "log"|"events", day: "YYYYMMDD", owner: "<PC>_<利用者>"|None}。

    違えば None(ほかのツールのファイル・メモなど)。
    """
    m = (LEGACY_NAME if legacy else OWN_NAME).match(name)
    if not m:
        return None
    if legacy:
        kind = "log" if m.group(1) == "inspection" else "events"
    else:
        kind = "events" if m.group(1) else "log"
    if m.group(4) != ("jsonl" if kind == "events" else "log"):
        return None                                 # 種類と拡張子の組が点検表の書き方と違う
    try:
        datetime.strptime(m.group(2), "%Y%m%d")
    except ValueError:
        return None
    owner = m.group(3)
    if owner is not None and "_" not in owner:
        return None                                 # 点検表は「<PC>_<利用者>」の形でしか付けない
    return {"kind": kind, "day": m.group(2), "owner": owner}


def log_folders() -> List[Tuple[Path, bool, bool]]:
    """点検表のログがありうるフォルダ: (フォルダ, ローカルか, 前の置き場所か)。

    いまの置き場所(`<出力先>/inspection/`)と、VER2.4.5 までの置き場所(出力先の直下)。
    指定のフォルダに書いているときは、退避していた期間のローカルも見る。
    """
    current = _target.current()
    local = _target.local()
    bases = [(current, current == local)]
    if current != local:
        bases.append((local, True))
    out: List[Tuple[Path, bool, bool]] = []
    for base, is_local in bases:
        out.append((tool_dir(base), is_local, False))
    for base, is_local in bases:
        out.append((base, is_local, True))
    return out


def own_files(kind: str = "", *, own_only: bool = True) -> List[Tuple[Path, Dict[str, Any]]]:
    """点検表のログファイル(いまの名前と前の名前)と、名前から読んだもの。

    `own_only` … この端末・この利用者のものだけ(ローカルの名前の無いものは自分のもの)。
    外すと、指定のフォルダ(共有)にあるほかの端末の**点検表の**ファイルも返す。
    """
    out: List[Tuple[Path, Dict[str, Any]]] = []
    seen = set()
    me = owner_tag()
    for folder, local, legacy in log_folders():
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            info = parse_name(name, legacy)
            if not info or (kind and info["kind"] != kind):
                continue
            owner = info["owner"]
            if local:
                if owner not in (None, me):
                    continue
            elif owner is None or (own_only and owner != me):
                continue                            # 指定のフォルダの名前には必ず端末が入る
            path = Path(folder) / name
            if not path.is_file() or str(path) in seen:
                continue
            seen.add(str(path))
            out.append((path, info))
    return out


def status() -> Dict[str, Any]:
    """設定画面・診断に出すもの。"""
    current = log_dir()
    return {
        "requested": _target.requested,
        "dir": str(current),
        "local_dir": str(tool_dir(_target.local())),
        "visible_local_dir": str(tool_dir(app_config.visible_local_root() / "logs")),
        "is_local": _target.is_local(),
        "fallback_reason": _target.fallback_reason,
        "keep_days": _target.keep_days,
        "owner": owner_tag(),
        "file": str(log_path_for(date.today())),
        "events_file": str(events_path_for(date.today())),
    }


def apply_settings(requested: Optional[str], days: Any = None) -> Dict[str, Any]:
    """設定が変わったとき。**再起動せずに**書き出し先を替える。

    前の書き出し先には「ここから先は○○へ」、新しいほうには「前は○○」を残す
    (どちらから辿っても続きが分かる)。
    """
    log = get_logger("logging")
    before = str(log_dir())
    with _target.lock:
        if days is not None:
            _target.keep_days = keep_days(days)
        new_requested = normalize_dir(requested)
        if new_requested == _target.requested and not _target.fallback_reason:
            return status()
        log.info("ログの保存先を替えます: %s → %s", before, new_requested or "この PC のローカル(既定)")
        _target.resolve(new_requested)
    _reopen_files()
    after = str(log_dir())
    log.info("ログの保存先: %s (前: %s)", after, before)
    if _target.fallback_reason:
        log.warning("ログの保存先 %s に書けないため、この PC のローカルに残します: %s",
                    _target.requested, _target.fallback_reason)
    return status()


def _reopen_files() -> None:
    for handler in logging.getLogger(ROOT_LOGGER).handlers:
        if isinstance(handler, DailyFileHandler):
            handler.retarget()


# ------------------------------------------------------------------
# ファイルへの書き出し
# ------------------------------------------------------------------
class DailyFileHandler(logging.FileHandler):
    """日付が変わったら、その日のファイルへ書き換える。書き出し先の変更にも付いていく。

    **開いたままの端末のため。** 書き出し先を起動時に1度だけ決めると、
    夜勤をまたいだ端末は翌日ぶんを前日のファイルに書き続ける。
    """

    def __init__(self, day: date, **kwargs) -> None:
        self._day = day
        super().__init__(log_path_for(day), delay=True, **kwargs)

    def retarget(self) -> None:
        self.acquire()
        try:
            self.close()
            self.baseFilename = str(log_path_for(self._day))
            self.stream = None                    # 次の emit で開き直す
        finally:
            self.release()

    def emit(self, record: logging.LogRecord) -> None:
        today = date.today()
        if today != self._day or _recovered.is_set():
            self._day = today
            _recovered.clear()
            self.retarget()
        elif self.baseFilename != str(log_path_for(today)):
            self.retarget()                      # 退避した・戻った
        # **開くところは標準の emit の守りの外。** 共有が切れて開けないと、ログを
        # 書こうとした業務の処理(印刷など)に例外が飛ぶ。ここで受けて退避する
        if self.stream is None:
            try:
                self.stream = self._open()
            except OSError:
                self.handleError(record)
                return
        super().emit(record)

    def handleError(self, record: logging.LogRecord) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, OSError) and _target.fall_back(
                f"{exc.strerror or exc} ({getattr(exc, 'winerror', None) or exc.errno})"):
            # 指定のフォルダ(共有)に書けなくなった。ローカルへ移って書き直す
            self.retarget()
            try:
                logging.FileHandler.emit(self, logging.makeLogRecord({
                    **record.__dict__, "levelno": logging.WARNING, "levelname": "WARNING",
                    "msg": "ログの保存先 %s に書けなくなったため、この PC のローカルに切り替えました: %s",
                    "args": (_target.requested, _target.fallback_reason)}))
                logging.FileHandler.emit(self, record)
            except Exception:                     # noqa: BLE001
                super().handleError(record)
            return
        super().handleError(record)


def configure_logging() -> None:
    """一度だけ呼ぶ。二重呼び出しは無害(冪等)。"""
    global _configured
    if _configured:
        return
    log_dir().mkdir(parents=True, exist_ok=True)
    root = logging.getLogger(ROOT_LOGGER)
    root.setLevel(logging.DEBUG)
    root.propagate = False
    formatter = logging.Formatter(
        "%(asctime)s | %(name)s | %(levelname)s | pid=%(process)d | %(message)s",
        datefmt="%Y/%m/%d %H:%M:%S")
    file_handler = DailyFileHandler(date.today(), encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    # **`pythonw.exe` では `sys.stderr` が `None`**(Start.vbs は pythonw で起動する)。
    # そのとき StreamHandler を付けると、1行ごとに誰にも見えない例外が起きる
    if _has_console():
        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        console.setFormatter(formatter)
        root.addHandler(console)
    _configured = True
    if _target.fallback_reason:
        root.warning("ログの保存先 %s に書けないため、この PC のローカルに残します: %s",
                     _target.requested, _target.fallback_reason)


def _has_console() -> bool:
    return getattr(sys, "stderr", None) is not None and hasattr(sys.stderr, "write")


def get_logger(name: str = "") -> logging.Logger:
    """モジュール別ロガー(未初期化なら自動で初期化する)。"""
    configure_logging()
    return logging.getLogger(f"{ROOT_LOGGER}.{name}" if name else ROOT_LOGGER)


def silence_console() -> None:
    """コンソールへの出力だけを止める(試験用。ファイルへの出力は残す)。"""
    logger = logging.getLogger(ROOT_LOGGER)
    for handler in list(logger.handlers):
        if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
            logger.removeHandler(handler)


# ------------------------------------------------------------------
# 古いログの片付け
# ------------------------------------------------------------------
def prune(now: Optional[datetime] = None) -> List[str]:
    """保存日数を過ぎた**自分の**ログを消す。消したファイル名を返す。

    ローカルは全部自分のもの。指定したフォルダ(共有)では、名前に自分の
    端末・利用者が入っているものだけ。いまの置き場所(`inspection/`)と前の置き場所
    (出力先の直下・VER2.4.5 までの名前)の両方を見る。**点検表の名前の形と合わない
    ファイル(ほかのツールのログ・メモ)には触れない**(`own_files`)。
    """
    limit = ((now or datetime.now()) - timedelta(days=_target.keep_days)).strftime("%Y%m%d")
    removed: List[str] = []
    for path, info in own_files(own_only=True):
        if info["day"] >= limit:
            continue
        try:
            os.remove(path)
            removed.append(path.name)
        except OSError:
            pass
    return removed


def reset_for_tests() -> None:
    """試験用: 書き出し先を読み直させる。"""
    with _target.lock:
        _target.dir = None
        _target.requested = ""
        _target.fallback_reason = ""
        _target.keep_days = DEFAULT_KEEP_DAYS
    _reopen_files()
