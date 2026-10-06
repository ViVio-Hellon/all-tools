"""出来事の記録を**書く・読む** (`logic/event_log.py` の形で)

【置き場所】
設定の「ログの出力パス」(`config.KEY_LOG_DIR`)。決めていなければこの端末の
`logs`。ファイルは**端末ごと・日ごと**に分けます:

    出来事_2026-10-01_PC-L1.jsonl     1行が1件(JSON)
    なぜなぜ_PC-L1.jsonl              書いてもらったなぜなぜ(1行が1回の保存)

共有のフォルダを指しても、**ほかの端末と同じファイルに書きません**
(名前に端末が入る)。読むときはフォルダの中を全部読むので、どの端末で
起きたことも1つの一覧に並びます。

【書けないとき ── 記録のせいで止めない】
共有に届かないときに要求ごとに待たされると、**日報が打てなくなります。**
そこで書くのは裏の1本のスレッドで、要求は列に置くだけで戻ります。
書けなければ手元の `logs` へ書き、5分たったらもう一度設定の先を試します
(手元へ書いたあいだのぶんも、一覧には並びます)。

【古いもの】
`KEEP_DAYS` 日より前の**自分の端末のぶん**だけ消します(起動のとき)。
ほかの端末のファイルには触りません。
"""
from __future__ import annotations

import atexit
import json
import logging
import queue
import re
import threading
import time
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from ..logic import event_log as rule

#: 残す日数。**半年**(同じエラーがまた起きたか、を見られる長さ)
KEEP_DAYS = 180
#: 設定の先に書けなかったあと、もう一度試すまで(秒)
RETRY_SECONDS = 300
#: 同じ警告を続けて残さない間(秒)。マスタが読めないと要求ごとに出るので
REPEAT_SECONDS = 60
#: 一覧を作るときに読む最大の件数(古いほうから捨てる)
READ_LIMIT = 50_000

EVENT_PREFIX = "出来事_"
NOTE_PREFIX = "なぜなぜ_"
EXPORT_DIR = "書き出し"

_lock = threading.Lock()
_queue: "queue.Queue[tuple[str, Any]]" = queue.Queue()
_thread: Optional[threading.Thread] = None
_fallback: dict[str, Any] = {"reason": "", "since": 0.0, "dir": ""}
_context: Optional[Callable[[], dict]] = None


# ------------------------------------------------------------------
# 端末・置き場所
# ------------------------------------------------------------------
def terminal_name() -> str:
    from .push_history import terminal_name as name
    return name() or "この端末"


def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("_") or "端末"


#: 呼ぶ側が書き先を名指ししたとき(`init_logging(log_dir)`。試験が一時
#: フォルダへ閉じ込めるのに使う)。None なら設定に従う
_override: dict[str, Optional[Path]] = {"dir": None}


def configured_dir() -> Path:
    if _override["dir"] is not None:
        return _override["dir"]
    from ..config import SETTINGS
    return SETTINGS.log_dir


def default_dir() -> Path:
    if _override["dir"] is not None:
        return _override["dir"]                   # 名指しされた先から出ない
    from ..config import SETTINGS
    return SETTINGS.default_log_dir


def read_dirs() -> list[Path]:
    """読むフォルダ。**設定の先と、手元の逃げ先の両方**(同じなら1つ)。"""
    dirs = [configured_dir()]
    local = default_dir()
    if local != dirs[0]:
        dirs.append(local)
    return dirs


def status() -> dict[str, Any]:
    """いまどこへ書いているか。**書けていないなら、そう言う。**"""
    want = configured_dir()
    fallback = _fallback["reason"]
    return {
        "dir": str(want),
        "default_dir": str(default_dir()),
        "is_default": want == default_dir(),
        "writing_to": _fallback["dir"] or str(want),
        "fallback": fallback,
        "keep_days": KEEP_DAYS,
        "terminal": terminal_name(),
    }


# ------------------------------------------------------------------
# 書く
# ------------------------------------------------------------------
def set_context_provider(provider: Optional[Callable[[], dict]]) -> None:
    """**要求の最中なら**、どの画面・どのタブ・どの直かを返す口(`app` が置く)。"""
    global _context
    _context = provider


def context() -> dict:
    if _context is None:
        return {}
    try:
        return _context() or {}
    except Exception:                             # noqa: BLE001 - 記録は止めない
        return {}


def record(record: dict) -> dict:
    """1件を列に置く。**すぐ戻る**(書くのは裏のスレッド)。

    書く先は**置いた時点で決めます** ── 書く時点で決めると、そのあいだに
    設定が変わった(試験なら一時フォルダが片付いた)ときに別の所へ書きます。
    """
    at = rule.parse_at(record.get("at", "")) or datetime.now()
    name = f"{EVENT_PREFIX}{at:%Y-%m-%d}_{_safe(record.get('terminal') or terminal_name())}.jsonl"
    _put(name, record)
    return record


def _put(name: str, item: dict) -> None:
    _ensure_thread()
    _queue.put((name, item, configured_dir(), default_dir()))


def note(message: str, *, kind: str = rule.KIND_INFO, **fields: Any) -> dict:
    """残しておく出来事(起動・設定の変更・共有へ保存の結果 など)。"""
    now = datetime.now()
    base = {"terminal": terminal_name(), "version": _version()}
    base.update(context())
    base.update(fields)
    return record(rule.make(kind, at=now, message=message, **base))


def save_analysis(event_id: str, body: dict) -> dict:
    """なぜなぜを残す。**上書きせず足していく**(読むときは新しいものが勝つ)。"""
    note_body = rule.clean_analysis(body)
    note_body.update({"id": event_id,
                      "at": datetime.now().isoformat(timespec="seconds"),
                      "terminal": terminal_name()})
    _put(f"{NOTE_PREFIX}{_safe(terminal_name())}.jsonl", note_body)
    flush()
    return note_body


def flush(timeout: float = 5.0) -> bool:
    """列が空になるまで待つ(試験と終わり際)。"""
    if _thread is None:
        return True
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if _queue.unfinished_tasks == 0:
            return True
        time.sleep(0.01)
    return False


def _ensure_thread() -> None:
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=_drain, name="event-log", daemon=True)
        _thread.start()


def _drain() -> None:
    while True:
        name, item, want, local = _queue.get()
        try:
            _append(name, item, want, local)
        except Exception:                         # noqa: BLE001 - 書き手は止めない
            pass
        finally:
            _queue.task_done()


def _append(name: str, item: dict, want: Path, local: Path) -> None:
    line = json.dumps(item, ensure_ascii=False, default=str) + "\n"
    if want != local:
        # 書けなかった直後は5分のあいだ手元へ(毎回待たされないように)
        stuck = (_fallback["reason"]
                 and time.monotonic() - _fallback["since"] < RETRY_SECONDS)
        if not stuck:
            try:
                _write(want / name, line)
                if _fallback["reason"]:
                    _fallback.update(reason="", since=0.0, dir="")
                return
            except OSError as exc:
                _fallback.update(reason=f"{want} に書けません({exc})",
                                 since=time.monotonic(), dir=str(local))
    _write(local / name, line)


def reset() -> None:
    """置き場所を変えたとき。**書けなかった印を消して、すぐ新しい先を試す。**"""
    flush(2.0)
    _fallback.update(reason="", since=0.0, dir="")


def _write(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)


atexit.register(flush, 2.0)


# ------------------------------------------------------------------
# 記録(logging)から拾う ── `log.exception(...)` を書いてある所は全部入る
# ------------------------------------------------------------------
#: 拾わない記録。起動の記録は別のファイル、サーバの待ち行列は日常の話
_SKIP_LOGGERS = ("nippou.launch", "waitress", "werkzeug", "urllib3")


class EventHandler(logging.Handler):
    """WARNING 以上の記録を、出来事として残す。

    **すでに出来事として残したもの**(`event_id` を持つ記録)は二重に
    残しません。同じ警告が続くときは1分に1度だけ残し、間引いた数を
    次の1件に添えます(マスタが読めないと要求ごとに出るので)。
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self._seen: dict[tuple, list] = {}

    def emit(self, rec: logging.LogRecord) -> None:   # noqa: D401 - logging の口
        try:
            if getattr(rec, "event_id", None):
                return
            if rec.name.startswith(_SKIP_LOGGERS):
                return
            key = (rec.name, str(rec.msg), rec.levelno)
            now = time.monotonic()
            seen = self._seen.get(key)
            if seen is not None and now - seen[0] < REPEAT_SECONDS:
                seen[1] += 1
                return
            skipped = seen[1] if seen else 0
            self._seen[key] = [now, 0]
            if len(self._seen) > 500:
                self._seen.clear()
            record(from_log_record(rec, skipped=skipped))
        except Exception:                         # noqa: BLE001 - 記録で落とさない
            self.handleError(rec)


def from_log_record(rec: logging.LogRecord, *, skipped: int = 0) -> dict:
    kind = rule.KIND_ERROR if rec.levelno >= logging.ERROR else rule.KIND_WARN
    fields: dict[str, Any] = {"terminal": terminal_name(), "version": _version(),
                              "logger": rec.name}
    fields.update(context())
    if rec.exc_info and rec.exc_info[0] is not None:
        fields.update(describe_exception(rec.exc_info[1]))
    else:
        fields["where"] = f"{_short(rec.pathname)}:{rec.lineno} ({rec.funcName})"
    if skipped:
        fields["count"] = f"前の1分に同じものがほか {skipped} 件"
    return rule.make(kind, at=datetime.fromtimestamp(rec.created),
                     message=rec.getMessage(), **fields)


def describe_exception(exc: BaseException) -> dict[str, str]:
    """例外を**なぜなぜの2段目**の形にする(種類と文・コードの場所・通り道)。"""
    frames = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
    roots = [str(_app_root())]
    where = rule.where_in_code([(f.filename, f.lineno, f.name) for f in frames], roots)
    trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return {"cause": f"{type(exc).__name__}: {exc}", "where": where,
            "trace": trace.replace(str(_app_root()), "")}


def _app_root() -> Path:
    from .. import app_config
    return app_config.APP_ROOT


def _short(path: str) -> str:
    root = str(_app_root())
    text = str(path)
    return text[len(root):].lstrip("\\/") if text.startswith(root) else Path(text).name


def _version() -> str:
    try:
        from .. import app_config
        return app_config.version()
    except Exception:                             # noqa: BLE001
        return ""


_handler: Optional[EventHandler] = None


def install(log_dir: Optional[Path] = None) -> None:
    """記録(logging)へ拾う口を付け、古いぶんを片付ける。**何度呼んでも1つ。**

    `log_dir` を渡すと、出来事もそこへ書きます(渡さなければ設定に従う)。
    """
    global _handler
    _override["dir"] = Path(log_dir) if log_dir is not None else None
    root = logging.getLogger()
    if _handler is None or _handler not in root.handlers:
        _handler = EventHandler()
        root.addHandler(_handler)
    try:
        purge()
    except Exception:                             # noqa: BLE001 - 片付けで止めない
        pass


def purge(today: Optional[date] = None, keep_days: int = KEEP_DAYS) -> int:
    """`keep_days` 日より前の**自分の端末のぶん**を消す。消した数を返す。"""
    today = today or date.today()
    limit = today - timedelta(days=keep_days)
    mine = _safe(terminal_name())
    removed = 0
    for folder in read_dirs():
        try:
            files = list(folder.glob(f"{EVENT_PREFIX}*_{mine}.jsonl"))
        except OSError:
            continue
        for path in files:
            day = _file_date(path)
            if day is not None and day < limit:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    continue
    return removed


# ------------------------------------------------------------------
# 読む
# ------------------------------------------------------------------
def _file_date(path: Path) -> Optional[date]:
    m = re.match(rf"{EVENT_PREFIX}(\d{{4}}-\d{{2}}-\d{{2}})_", path.name)
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(1))
    except ValueError:
        return None


def files_between(start: date, end: date) -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()
    for folder in read_dirs():
        try:
            found = sorted(folder.glob(f"{EVENT_PREFIX}*.jsonl"))
        except OSError:
            continue
        for path in found:
            day = _file_date(path)
            if day is None or day < start or day > end:
                continue
            key = str(path.resolve()) if path.exists() else str(path)
            if key in seen:
                continue
            seen.add(key)
            out.append(path)
    return out


def _read_lines(paths: Iterable[Path]) -> list[dict]:
    out: list[dict] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue                          # 書きかけで切れた行
            if isinstance(item, dict):
                out.append(item)
    return out[-READ_LIMIT:]


def load(start: date, end: date) -> list[dict]:
    """期間の出来事ぜんぶ(全端末)。**同じ番号は1件にする**(逃げ先と二重のとき)。"""
    flush(1.0)
    seen: set[str] = set()
    out = []
    for item in _read_lines(files_between(start, end)):
        key = str(item.get("id", ""))
        if key and key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def find(event_id: str, *, around: Optional[date] = None) -> tuple[Optional[dict], list[dict]]:
    """番号の1件と、**その日(と前後1日)の出来事**(経過を作るため)。"""
    day = around or _date_from_id(event_id)
    if day is None:
        return None, []
    records = load(day - timedelta(days=1), day + timedelta(days=1))
    for item in records:
        if item.get("id") == event_id:
            return item, records
    return None, records


def _date_from_id(event_id: str) -> Optional[date]:
    """番号の月日から日を引く(年は今年。先の日付なら去年)。"""
    m = re.match(r"^[A-Z](\d{2})(\d{2})-", str(event_id))
    if not m:
        return None
    today = date.today()
    try:
        day = date(today.year, int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None
    return day if day <= today + timedelta(days=1) else day.replace(year=today.year - 1)


def analyses() -> dict[str, dict]:
    """書いてもらったなぜなぜ。**番号ごとに新しいもの**(全端末)。"""
    flush(1.0)
    paths: list[Path] = []
    for folder in read_dirs():
        try:
            paths.extend(sorted(folder.glob(f"{NOTE_PREFIX}*.jsonl")))
        except OSError:
            continue
    latest: dict[str, dict] = {}
    for item in _read_lines(paths):
        key = str(item.get("id", ""))
        if not key:
            continue
        if key not in latest or str(item.get("at", "")) >= str(latest[key].get("at", "")):
            latest[key] = item
    return latest


def open_errors(days: int = 2, *, today: Optional[date] = None) -> int:
    """**まだなぜなぜを済ませていない**エラーの数(この `days` 日)。面の見出しに出す。"""
    today = today or date.today()
    records = load(today - timedelta(days=days - 1), today)
    notes = analyses()
    return sum(1 for r in records
               if r.get("kind") in rule.SCOPES["errors"]
               and (notes.get(str(r.get("id", ""))) or {}).get("status") != rule.STATUS_DONE)


def terminals(records: Iterable[dict]) -> list[str]:
    return sorted({str(r.get("terminal", "")) for r in records if r.get("terminal")})


# ------------------------------------------------------------------
# CSV
# ------------------------------------------------------------------
def export_csv(records: list[dict], notes: dict[str, dict],
               *, now: Optional[datetime] = None) -> Path:
    """一覧をCSVに出す(Excel で開ける UTF-8 BOM 付き)。出した道を返す。"""
    import csv
    import io

    now = now or datetime.now()
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(rule.CSV_HEADER)
    for rec in records:
        writer.writerow(rule.csv_row(rec, notes.get(str(rec.get("id", "")))))
    name = f"エラー一覧_{now:%Y%m%d_%H%M%S}.csv"
    for folder in (configured_dir(), default_dir()):
        path = folder / EXPORT_DIR / name
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(buf.getvalue(), encoding="utf-8-sig")
            return path
        except OSError:
            continue
    raise OSError("CSVを書けるフォルダがありません")
