"""ログから「なぜなぜ分析」の材料を拾う

エラーが起きたあとで困るのは、**「何が起きたか」の事実が集まらない**こと。
利用者が覚えているのは「押したら赤い表示が出た」くらいで、いつ・どの端末で・
何をしようとして・何が原因だったかは、ログのあちこちに散っている。

このモジュールは、記録番号(``logging_utils.new_ref``)を1つ受けて、
なぜなぜ分析を始めるのに要る事実を1枚にまとめる:

    何が起きたか      日時・端末・版・どこで・内容
    直接の原因        最後に投げられた例外と、それが起きたこちらのコードの場所
    原因のつながり    例外の連鎖(``raise ... from ...``)。根本に近い順
    同じ操作の記録    同じ追跡番号(r=/s=)の行 ── その操作で何が起きたか
    直前の流れ        その前の INFO 以上の行 ── 何をした後だったか
    起動時の様子      版・端末・ライン・参照パス・ログの書き先
    なぜなぜ          空欄の枠(ここからは人が埋める)

**判断は書かない。** 原因を推し量って書くと、読む人がそれを事実と
取り違える。ここに出すのはログにあった事実だけで、なぜを重ねるのは人。

読むのは設定画面(「9. ログ」)と ``tools/log_report.py``(集めたログを
まとめて見る)の2か所。どちらもこのモジュールを呼ぶだけにしてある。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

from . import logging_utils

# いまの形: 日時.ミリ秒 | 重さ | 出どころ | スレッド | 追跡 | 本文
_LINE = re.compile(
    r"^(?P<at>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})(?:\.(?P<ms>\d{3}))? \| "
    r"(?P<level>[A-Z]+) \| (?P<name>[^|]*?) \| (?P<thread>[^|]*?) \| "
    r"(?P<trace>[^|]*?) \| (?P<message>.*)$")
# 前の形(この版より前に書かれた行): 日時 | 出どころ | 重さ | 本文
_OLD_LINE = re.compile(
    r"^(?P<at>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}) \| (?P<name>[^|]*?) \| "
    r"(?P<level>[A-Z]+) \| (?P<message>.*)$")

_FILE_GLOB = "calendar_*.log"
_FILE_DATE = re.compile(r"calendar_(\d{8})\.log$")

#: 例外の連鎖の区切り(Python が書く決まり文句)
_CAUSE = "The above exception was the direct cause of the following exception:"
_CONTEXT = "During handling of the above exception, another exception occurred:"
_FRAME = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>.+)$')

#: 見出しの目印(``logging_utils.write_header`` の1行目)
_BOOT_MARKS = logging_utils.HEADER_MARKS

#: 直前の流れに出す行数
BEFORE_LINES = 30
#: 起動の記録を探しに戻る日数(朝起動して夕方に落ちた、なら同じ日で足りる)
BOOT_LOOKBACK_DAYS = 7


@dataclass
class Entry:
    """ログの1件(続きの行 = 例外の中身などを含む)。"""

    at: str
    level: str
    name: str
    thread: str
    trace: str
    ref: str
    message: str
    extra: list[str] = field(default_factory=list)
    path: Optional[Path] = None

    def text(self) -> str:
        head = f"{self.at} | {self.level} | {self.name} | {self.thread} | {self.trace} | {self.message}"
        return "\n".join([head] + self.extra)

    @property
    def level_no(self) -> int:
        return _LEVELS.get(self.level, 0)


_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


# ---------------------------------------------------------------------------
# 読む
# ---------------------------------------------------------------------------
def parse_lines(lines: Iterable[str], path: Optional[Path] = None) -> list[Entry]:
    entries: list[Entry] = []
    for raw in lines:
        line = raw.rstrip("\r\n")
        m = _LINE.match(line)
        if m:
            trace_col = m["trace"].strip()
            ref_match = logging_utils.REF_PATTERN.search(trace_col)
            ref = ref_match.group(1) if ref_match else ""
            trace = trace_col.split(" ")[0] if trace_col else "-"
            at = m["at"] + (f".{m['ms']}" if m["ms"] else "")
            entries.append(Entry(at, m["level"], m["name"].strip(),
                                 m["thread"].strip(), trace, ref,
                                 m["message"], path=path))
            continue
        m = _OLD_LINE.match(line)
        if m:
            entries.append(Entry(m["at"], m["level"], m["name"].strip(), "",
                                 "-", "", m["message"], path=path))
            continue
        if entries:
            entries[-1].extra.append(line)
        # 先頭の続きの行(前の日のファイルから続いたもの)は捨てる
    return entries


def read_file(path: Path) -> list[Entry]:
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            return parse_lines(fh, path)
    except OSError:
        return []


def _file_date(path: Path) -> Optional[date]:
    m = _FILE_DATE.search(path.name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d").date()
    except ValueError:
        return None


def log_files(folders: Iterable[Path], *, days: Optional[int] = None,
              recursive: bool = False) -> list[Path]:
    """日付つきのログファイル。**新しい順**。``days`` で何日前まで見るか絞る。"""
    since = date.today() - timedelta(days=days) if days else None
    seen: set[Path] = set()
    found: list[tuple[date, Path]] = []
    for folder in folders:
        try:
            paths = folder.rglob(_FILE_GLOB) if recursive else folder.glob(_FILE_GLOB)
            for path in paths:
                when = _file_date(path)
                key = path.resolve()
                if when is None or key in seen:
                    continue
                if since and when < since:
                    continue
                seen.add(key)
                found.append((when, path))
        except OSError:
            continue
    found.sort(key=lambda item: (item[0], str(item[1])), reverse=True)
    return [path for _, path in found]


def default_folders() -> list[Path]:
    return logging_utils.folders_to_read()


# ---------------------------------------------------------------------------
# 一覧
# ---------------------------------------------------------------------------
def where_of(entry: Entry) -> str:
    """どこで起きたか。利用者が「何をしていたときか」を思い出せる言い方にする。"""
    if entry.name.endswith(".client"):
        return "画面(ブラウザ)"
    if entry.name.endswith(".uncaught"):
        return "裏の処理(止まった)"
    if entry.trace.startswith("s="):
        return "同期"
    if entry.trace.startswith("r="):
        return "画面からの操作"
    if entry.thread.startswith("sync"):
        return "同期"
    return "アプリ"


def _pc_of(path: Optional[Path]) -> str:
    """ファイルの置き場所から端末名を読む(指定フォルダの下は PC名\\ の形)。"""
    if path is None:
        return ""
    parent = path.parent.name
    return "" if parent == "logs" else parent


def recent_errors(*, days: int = 7, limit: int = 50,
                  folders: Optional[list[Path]] = None,
                  recursive: bool = False) -> list[dict[str, str]]:
    """記録番号の付いた行(ERROR 以上)を新しい順に。

    数MBのファイルを7日ぶん読むので、**記録番号を含む行だけ**を先に拾う
    (続きの行は要らない ── 一覧に出すのは1行目だけ)。
    """
    rows: list[dict[str, str]] = []
    for path in log_files(folders or default_folders(), days=days,
                          recursive=recursive):
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                hits = [line for line in fh if "記録番号=" in line]
        except OSError:
            continue
        for entry in reversed(parse_lines(hits, path)):
            if not entry.ref:
                continue
            rows.append({
                "at": entry.at, "ref": entry.ref, "level": entry.level,
                "where": where_of(entry), "what": entry.message[:200],
                "pc": _pc_of(path), "file": str(path),
            })
            if len(rows) >= limit:
                return rows
    return rows


# ---------------------------------------------------------------------------
# 1件をまとめる
# ---------------------------------------------------------------------------
def _ref_date(ref: str) -> Optional[date]:
    try:
        return datetime.strptime(ref[1:9], "%Y%m%d").date()
    except ValueError:
        return None


def find(ref: str, *, folders: Optional[list[Path]] = None,
         recursive: bool = False) -> Optional[tuple[Entry, list[Entry]]]:
    """記録番号の行と、それが入っているファイルの全件を返す。"""
    ref = (ref or "").strip().upper()
    if not logging_utils.REF_PATTERN.fullmatch(f"記録番号={ref}"):
        return None
    when = _ref_date(ref)
    files = log_files(folders or default_folders(), recursive=recursive)
    # 番号の日付のファイルから探す(日付をまたいで書かれた場合に備えて前後も)
    if when is not None:
        near = {when, when - timedelta(days=1), when + timedelta(days=1)}
        files = ([p for p in files if _file_date(p) in near]
                 + [p for p in files if _file_date(p) not in near])
    for path in files:
        try:
            if ref not in path.read_text(encoding="utf-8", errors="replace"):
                continue
        except OSError:
            continue
        entries = read_file(path)
        for entry in entries:
            if entry.ref == ref:
                return entry, entries
    return None


@dataclass
class Exceptions:
    chain: list[str]          # 根本に近い順
    place: str                # 最後の例外が起きた、こちらのコードの場所


_CLIENT_PREFIX = "画面でエラー: "


def exceptions_of(entry: Entry) -> Exceptions:
    """続きの行(トレースバック)から例外の連鎖と場所を読む。

    画面(ブラウザ)のエラーは Python の形をしていないので、文言そのものが
    直接の原因、``場所:`` の行(どのJSの何行目か)が場所になる。
    """
    if entry.name.endswith(".client"):
        place = next((l.strip()[len("場所: "):] for l in entry.extra
                      if l.strip().startswith("場所: ")), "")
        message = entry.message
        if message.startswith(_CLIENT_PREFIX):
            message = message[len(_CLIENT_PREFIX):]
        return Exceptions([message], place)
    segments: list[list[str]] = [[]]
    for line in entry.extra:
        if line.strip() in (_CAUSE, _CONTEXT):
            segments.append([])
        else:
            segments[-1].append(line)

    chain: list[str] = []
    place = ""
    for segment in segments:
        frames = [m for m in (_FRAME.match(l) for l in segment) if m]
        # 例外の行 = 字下げの無い最後の行(Traceback 見出しは除く)
        heads = [l for l in segment if l and not l.startswith((" ", "\t"))
                 and not l.startswith("Traceback ")]
        if heads:
            chain.append(heads[-1].strip())
        ours = [f for f in frames if _is_ours(f["file"])]
        if ours:
            last = ours[-1]
            place = (f"{_short(last['file'])} {last['line']}行目 "
                     f"({last['func'].strip()})")
    return Exceptions(chain, place)


def _is_ours(path: str) -> bool:
    norm = path.replace("\\", "/")
    return ("/calendar_app/" in norm or "/app/routes/" in norm
            or norm.endswith(("/app/__init__.py", "/start_app.py")))


def _short(path: str) -> str:
    norm = path.replace("\\", "/")
    for marker in ("/calendar_app/", "/app/", "/tools/"):
        if marker in norm:
            return marker.strip("/") + "/" + norm.split(marker, 1)[1]
    return norm


def _boot_lines(entries: list[Entry], index: int, path: Optional[Path],
                folders: list[Path], recursive: bool) -> list[str]:
    """エラーより前の、いちばん近い起動の記録(版・端末・パス)。"""
    def pick(items: list[Entry], upto: int) -> list[str]:
        for i in range(upto - 1, -1, -1):
            if items[i].name.endswith(".launcher") and items[i].message.startswith(_BOOT_MARKS):
                block = [e for e in items[i:i + 40] if e.name.endswith(".launcher")]
                return [f"{e.at}  {e.message}" for e in block[:16]]
        return []

    found = pick(entries, index)
    if found or path is None:
        return found
    # 起動が前の日なら、そのファイルへ戻って探す
    day = _file_date(path)
    if day is None:
        return []
    for back in range(1, BOOT_LOOKBACK_DAYS + 1):
        older = path.with_name(logging_utils._log_name(day - timedelta(days=back)))
        if older.exists():
            items = read_file(older)
            found = pick(items, len(items))
            if found:
                return found
    return []


def report(ref: str, *, folders: Optional[list[Path]] = None,
           recursive: bool = False) -> Optional[str]:
    """記録番号1つぶんの「なぜなぜ分析の材料」。見つからなければ ``None``。"""
    folders = folders or default_folders()
    hit = find(ref, folders=folders, recursive=recursive)
    if hit is None:
        return None
    entry, entries = hit
    index = next(i for i, e in enumerate(entries) if e is entry)
    exc = exceptions_of(entry)

    same = [e for e in entries
            if entry.trace != "-" and e.trace == entry.trace and e is not entry]
    before = [e for e in entries[:index] if e.level_no >= 20][-BEFORE_LINES:]
    boot = _boot_lines(entries, index, entry.path, folders, recursive)
    detail = [line for line in entry.extra if not line.startswith(("Traceback", "  File", "    "))
              and line.strip() not in (_CAUSE, _CONTEXT) and line.strip()]

    out: list[str] = []
    add = out.append
    add(f"なぜなぜ分析の材料 ── 記録番号 {entry.ref}")
    add("=" * 60)
    add("※ ログにあった事実だけを並べています。原因の推測は書いていません。")
    add("")
    add("■ 何が起きたか")
    add(f"  日時     : {entry.at}")
    pc = _pc_of(entry.path)
    if pc:
        add(f"  端末     : {pc}")
    add(f"  どこで   : {where_of(entry)}"
        + (f"(追跡番号 {entry.trace})" if entry.trace != "-" else ""))
    add(f"  出どころ : {entry.name}(スレッド {entry.thread or '-'})")
    add(f"  重さ     : {entry.level}")
    add(f"  内容     : {entry.message}")
    for line in detail:
        if exc.chain and line.strip() in exc.chain:
            continue
        add(f"             {line.strip()}")
    add(f"  ファイル : {entry.path}")
    add("")
    add("■ 直接の原因(最後に起きた例外)")
    if exc.chain:
        add(f"  {exc.chain[-1]}")
        if exc.place:
            add(f"  場所: {exc.place}")
    else:
        add("  (例外の記録はありません ── 上の「内容」が全てです)")
    if len(exc.chain) > 1:
        add("")
        add("■ 原因のつながり(上ほど根本に近い)")
        for i, item in enumerate(exc.chain, 1):
            add(f"  {i}. {item}")
    add("")
    add(f"■ 同じ操作の記録({entry.trace})" if entry.trace != "-" else
        "■ 同じ操作の記録")
    if same:
        out.extend("  " + e.text().replace("\n", "\n  ") for e in same)
    else:
        add("  (ほかの行はありません)")
    add("")
    add(f"■ 直前の流れ(この前の INFO 以上 {len(before)} 行)")
    if before:
        out.extend(f"  {e.at} | {e.level} | {e.trace} | {e.message}" for e in before)
    else:
        add("  (ありません)")
    add("")
    add("■ 起動時の様子")
    if boot:
        out.extend(f"  {line}" for line in boot)
    else:
        add(f"  (起動の記録が見つかりません。{BOOT_LOOKBACK_DAYS}日より前に起動したままか、"
            "ログが消えています)")
    add("")
    add("■ 例外の全文")
    if entry.extra:
        out.extend(f"  {line}" for line in entry.extra)
    else:
        add("  (ありません)")
    add("")
    add("■ なぜなぜ(ここから下を埋めてください)")
    first = exc.chain[-1] if exc.chain else entry.message
    add(f"  起きたこと : {first}")
    for i in range(1, 6):
        add(f"  なぜ{i}     : ")
    add("  真因       : ")
    add("  対策       : ")
    add("  確かめ方   : (いつ・誰が・何を見て、効いたと判断するか)")
    return "\n".join(out)
