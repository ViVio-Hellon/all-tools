"""表の中身だけを Access の最新に入れ替える(python-web-tools の ``table_bring.refresh`` の移植)

【なぜ要るのか】
Access のほうが新しいとき(Access で看板を足した・サイズを直した)、以前は
Access をまるごと変換して共有DB(看板マスタ.sqlite3)を差し替えるしかなかった。
すると、**このツールが共有DBに足した表・列・行が消える**:

    看板履歴(集計の記録)・看板コメント(倉庫 ⇔ 現場のやり取り)・それぞれの索引、
    そして Access には届いていない看板の状態(赤・緑・注文中と、その日時)

そこで **Access と共有DBの両方にある表について、選んだ表の中身だけ**を入れ替える。
表の定義(列・索引)とほかの表には触らない。

【守っていること】
- **このツールが書き込む表は入れ替えない**(看板履歴・看板コメント・Form状態管理)
- 表の定義はそのまま。**同じ名前の列だけ**を写す。Access にしか無い列は写さない(画面に出す)
- **看板の表(看板_<ライン>)は、状態の列をいまの値のまま残す。** 状態(欲・不・更新日・
  発送・倉庫確認日時・保留・注文中日時)は各端末が共有DBへ書いていて、Access には
  届いていない。管理番号で突き合わせ、資材・サイズなどは Access の最新に、状態は
  いまのままにする。Access に無い看板のうち、発注中・発送済み・注文中のものは消さずに残す
- 文字化けの疑いがある表(置き換え文字 U+FFFD を含む行がある)は入れ替えない
- 1 つの表は **消す+入れる を 1 回で確定**する。途中で落ちたら元のまま
- 書く前に共有DBを、この端末の ``backup`` フォルダへ写しておく

【Access のままでも選べる】
.accdb / .mdb は、移行の変換(``tools/accdb_to_sqlite.py``)と同じ内蔵リーダー
(:mod:`kanban.accdb.reader`)で読み、値も同じ書き方にそろえる
(:func:`kanban.accdb.reader.to_sqlite_value`)。変換済みの .sqlite3 も選べる。
"""

from __future__ import annotations

import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import applog, config, keys
from .db.shared import SharedDb, SharedDbError

ACCESS_SUFFIXES = (".accdb", ".mdb")
SQLITE_SUFFIXES = (".sqlite3", ".sqlite", ".db")
UPLOAD_SUFFIXES = ACCESS_SUFFIXES + SQLITE_SUFFIXES
#: 受け取る大きさの上限。Access の上限(2GB)より手前で止める
UPLOAD_LIMIT_BYTES = 1024 * 1024 * 1024
#: 控えを残す数(古いものから消す。共有DBの大きさ × この数だけ手元に溜まる)
BACKUP_KEEP = 10

REFUSE_NO_FILE = "no_file"
REFUSE_NO_DEST = "no_dest"
REFUSE_SAME_FILE = "same_file"
REFUSE_NOTHING = "nothing"
REFUSE_READ = "read_failed"
REFUSE_WRITE_FAILED = "write_failed"

#: 状態の列(:data:`kanban.presenters.master.STATE_COLUMNS` と同じ)
STATE_COLUMNS = (
    config.COL_WANT, config.COL_UNWANT, config.COL_ORDERED_AT, config.COL_SHIPPED,
    config.COL_CONFIRMED_AT, config.COL_HOLD, config.COL_HOLD_AT,
)
#: Access に無い看板を足すときの状態(マスタ管理で足すときと同じ ── 発注していない)
INITIAL_STATE = {c: "" for c in STATE_COLUMNS} | {config.COL_UNWANT: config.MARK_ON}

KEEPS_KANBAN_SHORT = "状態(赤・緑・注文中とその日時)はいまのまま残します"
KEEPS_KANBAN = (
    "状態の列(欲・不・更新日・発送・倉庫確認日時・保留・注文中日時)は、いまの値を残します"
    "(各端末が共有DBへ書いていて、Access には届いていないため)。"
    "Access に無い看板のうち、発注中・発送済み・注文中のものは消さずに残します"
)


def _tool_owned() -> dict[str, str]:
    """**このツールが書き込む表。** Access の中身では入れ替えない。"""
    from .db import sync

    return {
        sync.HISTORY_TABLE: "このツールが書き込む表です(看板集計の記録)",
        sync.COMMENT_TABLE: "このツールが書き込む表です(倉庫 ⇔ 現場のコメント)",
        config.TABLE_STATE: "このツールが書き込む表です(開いている端末の印)",
    }


#: Access が自分のために持っている表。変換の仕方によっては sqlite3 にも入る
_ACCESS_INTERNAL = re.compile(r"^(MSys|~|f_[0-9A-Fa-f]{32}_)")


def is_access_internal(name: str) -> bool:
    return bool(_ACCESS_INTERNAL.match(name))


def is_kanban_table(name: str) -> bool:
    return name.startswith(config.KANBAN_TABLE_PREFIX)


def is_access(path: Path) -> bool:
    return Path(path).suffix.lower() in ACCESS_SUFFIXES


# ------------------------------------------------------------------
# 読む(Access / 変換した sqlite3)
# ------------------------------------------------------------------
@dataclass
class SourceTable:
    name: str
    columns: list[str]
    rows: list[dict[str, Any]]


class SourceError(Exception):
    """選んだファイルを読めなかった。文は画面にそのまま出す。"""


#: 同じファイルを何度も読まない(中を見る → 入れ替える で 2 回読む)。姿が変われば読み直す
_cache: dict[str, tuple[tuple[int, int], dict[str, SourceTable]]] = {}


def read_source(path: Path) -> dict[str, SourceTable]:
    """選んだファイルの表を全部読む。Access の内部の表(MSys…)は入れない。"""
    path = Path(path)
    try:
        stat = path.stat()
    except OSError as exc:
        raise SourceError(f"ファイルを開けません: {path}({exc})") from exc
    stamp = (stat.st_size, stat.st_mtime_ns)
    hit = _cache.get(str(path))
    if hit and hit[0] == stamp:
        return hit[1]
    tables = _read_access(path) if is_access(path) else _read_sqlite(path)
    _cache.clear()                      # 大きいので 1 つだけ覚える
    _cache[str(path)] = (stamp, tables)
    return tables


def _read_access(path: Path) -> dict[str, SourceTable]:
    from .accdb import reader

    try:
        out: dict[str, SourceTable] = {}
        with reader.AccdbReader(str(path)) as db:
            for name in db.table_names():
                if is_access_internal(name):     # 添付ファイルの表(f_…_Data)など
                    continue
                table = db.read_table(name)
                columns = table.column_names()
                rows = [{c: reader.to_sqlite_value(r.get(c)) for c in columns} for r in table.rows]
                out[name] = SourceTable(name, columns, rows)
        return out
    except Exception as exc:  # noqa: BLE001 - リーダーの失敗は理由を画面に出す
        raise SourceError(f"Access を読めませんでした: {exc}") from exc


def _read_sqlite(path: Path) -> dict[str, SourceTable]:
    # **素のパスで開く(URI にしない)。** 共有フォルダ(\\server\…)のファイルは
    # URI にすると開けない(kanban.db.shared の接続と同じ)。書かないよう query_only にする
    try:
        conn = sqlite3.connect(str(path))
        conn.execute("PRAGMA query_only = ON")
    except sqlite3.Error as exc:
        raise SourceError(f"sqlite3 を開けません: {exc}") from exc
    conn.row_factory = sqlite3.Row
    try:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
            if not is_access_internal(r[0])]
        out = {}
        for name in names:
            cursor = conn.execute(f"SELECT * FROM {_q(name)}")
            columns = [d[0] for d in cursor.description]
            out[name] = SourceTable(name, columns, [dict(r) for r in cursor.fetchall()])
        return out
    except sqlite3.Error as exc:
        raise SourceError(f"sqlite3 を読めませんでした: {exc}") from exc
    finally:
        conn.close()


def _q(name: str) -> str:
    return "[" + str(name).replace("]", "]]") + "]"


def _suspect_rows(table: SourceTable) -> int:
    """文字化けの疑いがある行(置き換え文字 U+FFFD を含む行)の数。"""
    return sum(1 for r in table.rows if any(isinstance(v, str) and "�" in v for v in r.values()))


def _key(value: Any) -> str:
    """管理番号をそろえる(:func:`kanban.keys.normalize`)。

    共有DBの列が TEXT で Access が数値(Double)だと、入れ替えで '1.0' と書かれる。これを 1 と
    突き合わせられないと、次の入れ替えで同じ看板を足し、発注中の元の行も残して表が倍になった。
    """
    return keys.normalize(value)


# ------------------------------------------------------------------
# 中を見る(書かない)
# ------------------------------------------------------------------
@dataclass
class Candidate:
    """両方にある表 1 つ。"""

    name: str
    rows: int = 0                 # Access(選んだファイル)の行数
    current_rows: int = 0         # 共有DBのいまの行数
    columns: list[str] = field(default_factory=list)
    not_copied: list[str] = field(default_factory=list)   # 共有DBに無い列(写さない)
    suspect: int = 0
    refresh_why: str = ""         # 入れ替えられない理由(入れ替えられるなら空)
    keeps: str = ""               # 入れ替えても残すもの
    preview: str = ""             # 入れ替えると何が変わるか(看板の表: 変わる・足す・消す・残す)

    @property
    def can_refresh(self) -> bool:
        return not self.refresh_why


@dataclass
class FileFacts:
    """読んだファイル・書き込み先の姿。**どのファイルを見たのか**を画面に出すため。"""
    path: str = ""
    size: int = 0
    modified: str = ""            # 更新日時
    tables: int = 0               # 表の数(Access の内部の表は数えない)
    kanban_tables: list[str] = field(default_factory=list)   # 看板の表(看板_<ライン>)


@dataclass
class Plan:
    source: str = ""
    dest: str = ""
    ok: bool = False
    message: str = ""
    candidates: list[Candidate] = field(default_factory=list)
    only_in_source: list[str] = field(default_factory=list)
    only_in_dest: list[str] = field(default_factory=list)
    #: 両方にある表が無い・少ないときの見立て(どのファイルを選ぶべきか)。無ければ空
    hint: str = ""
    #: 名前の書き方(全角/半角・大文字/小文字・空白)だけが違う表 ``(Access, 共有DB)``。
    #: 別の表として扱う(入れ替えない)が、見比べられるように出す
    near: list[tuple[str, str]] = field(default_factory=list)
    source_facts: FileFacts = field(default_factory=FileFacts)
    dest_facts: FileFacts = field(default_factory=FileFacts)


def refresh_why(name: str, src: SourceTable, dest_columns: Sequence[str], suspect: int) -> str:
    """その表の中身を入れ替えられない理由。できるなら空。"""
    owned = _tool_owned().get(name)
    if owned:
        return owned + "。Access の中身では入れ替えません"
    if suspect:
        return f"文字化けの疑いがある行が {suspect}行 あります。Access の中身と見比べてください"
    if not [c for c in src.columns if c in dest_columns]:
        return "共有DBの表と、同じ名前の列が 1 つもありません"
    if is_kanban_table(name):
        if config.COL_KEY not in src.columns or config.COL_KEY not in dest_columns:
            return f"{config.COL_KEY} の列がありません(看板を突き合わせられません)"
        keys = [_key(r.get(config.COL_KEY)) for r in src.rows]
        if any(not k for k in keys):
            return f"{config.COL_KEY} が空の行があります"
        dup = sorted({k for k in keys if keys.count(k) > 1})
        if dup:
            return f"{config.COL_KEY} が重なっています: {', '.join(dup[:10])}"
    return ""


def _dest_path(shared: SharedDb | None) -> Path | None:
    if shared is None or not shared.exists():
        return None
    return Path(shared.path)


def _same(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return str(a) == str(b)


def _check_paths(source_path: str, shared: SharedDb | None) -> tuple[Path | None, str, str]:
    """``(読むファイル, 断る理由, 理由の種類)``。"""
    text = str(source_path or "").strip()
    if not text:
        return None, "Access のファイルを選んでください。", REFUSE_NO_FILE
    src = Path(text)
    if not src.is_file():
        return None, f"ファイルが見つかりません: {src}", REFUSE_NO_FILE
    dest = _dest_path(shared)
    if dest is None:
        return None, "共有DBに届きません。「接続先」を確かめてください。", REFUSE_NO_DEST
    if _same(src, dest):
        return None, ("選んだのは、いま使っている共有DBそのものです。"
                      "Access(.accdb)か、Access を変換した別のファイルを選んでください。"), REFUSE_SAME_FILE
    # 書き込み先が看板マスタでなければ書かない(接続先が別のツールの sqlite3 を指している)
    from .db.shared import kanban_tables_in, not_kanban_reason

    try:
        kanban, others = kanban_tables_in(dest)
    except Exception as exc:  # noqa: BLE001
        return None, f"共有DBの中を確かめられません: {exc}", REFUSE_NO_DEST
    if not kanban:
        return None, ("書き込み先の共有DBが看板マスタではありません。" + not_kanban_reason(str(dest), others)
                      + "\n「接続先」を確かめてください。"), REFUSE_NO_DEST
    return src, "", ""


def plan(source_path: str, shared: SharedDb | None) -> Plan:
    """選んだファイルの中を見る。**書かない。**"""
    out = Plan(source=str(source_path or "").strip())
    src_path, why, _reason = _check_paths(source_path, shared)
    if src_path is None:
        out.message = why
        return out
    assert shared is not None
    out.dest = shared.path
    try:
        tables = read_source(src_path)
        dest_names = set(shared.table_names())
    except (SourceError, SharedDbError) as exc:
        out.message = str(exc)
        return out
    out.source_facts = _facts(src_path, tables)
    out.dest_facts = _facts(Path(shared.path), [n for n in dest_names if not is_access_internal(n)])
    out.only_in_dest = sorted((n for n in dest_names if n not in tables and not is_access_internal(n)),
                              key=_order)
    for name in sorted(tables, key=_order):
        src = tables[name]
        if name not in dest_names:
            out.only_in_source.append(name)
            continue
        dest_cols = shared.column_names(name)
        suspect = _suspect_rows(src)
        cand = Candidate(
            name=name, rows=len(src.rows), columns=src.columns,
            current_rows=_count(shared, name),
            not_copied=[c for c in src.columns if c not in dest_cols],
            suspect=suspect,
            refresh_why=refresh_why(name, src, dest_cols, suspect),
            keeps=KEEPS_KANBAN_SHORT if is_kanban_table(name) else "",
        )
        if is_kanban_table(name) and cand.can_refresh:
            cand.preview = _preview_kanban(shared, src, dest_cols)
        out.candidates.append(cand)
    out.ok = True
    out.near = _near_names(out.only_in_source, out.only_in_dest)
    can = sum(1 for c in out.candidates if c.can_refresh)
    if out.candidates:
        out.message = (f"共有DBにもある表が {len(out.candidates)} 個あり、"
                       f"そのうち {can} 個の中身を Access の最新に入れ替えられます。")
        if out.only_in_source:
            out.message += f"(Access にだけある表 {len(out.only_in_source)} 個は扱いません)"
    else:
        out.message = (f"選んだファイル(表 {len(tables)} 個)と共有DB(表 {out.dest_facts.tables} 個)に、"
                       "同じ名前の表が 1 つもありません。何も入れ替えられません。")
    out.hint = _hint(src_path, out)
    return out


def _facts(path: Path, names: Iterable[str]) -> FileFacts:
    names = list(names)
    facts = FileFacts(path=str(path), tables=len(names),
                      kanban_tables=sorted((n for n in names if is_kanban_table(n)), key=_order))
    try:
        stat = path.stat()
        facts.size = stat.st_size
        facts.modified = datetime.fromtimestamp(stat.st_mtime).strftime("%Y/%m/%d %H:%M:%S")
    except OSError:
        pass
    return facts


def _loose(name: str) -> str:
    """名前の書き方の違い(全角/半角・大文字/小文字・空白)を無くした形。見比べるためだけに使う。"""
    import unicodedata

    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", name)).casefold()


def _near_names(only_src: Sequence[str], only_dest: Sequence[str]) -> list[tuple[str, str]]:
    dest = {}
    for name in only_dest:
        dest.setdefault(_loose(name), name)
    return [(name, dest[_loose(name)]) for name in only_src if _loose(name) in dest]


#: 梱包資材マスタ(python-web-tools のファイル)に入っている表。選んだのがこれかを見分ける
_GATEWAY_MARKS = ("アクセス権限",)


def _hint(src_path: Path, p: Plan) -> str:
    """両方にある表が無い・看板の表が入れ替えられないときに、**何が起きているか**を一言で。"""
    if p.candidates and any(is_kanban_table(c.name) for c in p.candidates):
        return ""
    src_kanban = p.source_facts.kanban_tables
    lines = []
    if not src_kanban:
        lines.append(f"選んだファイルには看板の表(看板_LVC など)が 1 つもありません: {src_path.name}")
        if "梱包資材" in src_path.stem or any(m in p.only_in_source for m in _GATEWAY_MARKS):
            lines.append("選んだのは梱包資材マスタのようです。このツールが入れ替えるのは"
                         f"看板マスタ(共有DB {Path(p.dest).name})の表だけです。")
        lines.append(f"看板マスタの Access({config.TARGET_DB_NAME})を選んでください。")
    elif p.dest_facts.kanban_tables:
        lines.append("看板の表はありますが、共有DBの看板の表と名前が合いません。"
                     f"選んだファイル: {'、'.join(src_kanban[:8])}"
                     + (" …" if len(src_kanban) > 8 else "")
                     + f" / 共有DB: {'、'.join(p.dest_facts.kanban_tables[:8])}"
                     + (" …" if len(p.dest_facts.kanban_tables) > 8 else ""))
    if p.near:
        lines.append("名前の書き方(全角/半角・大文字/小文字・空白)だけが違う表があります。"
                     "別の表として扱い、入れ替えません: "
                     + "、".join(f"「{a}」⇔「{b}」" for a, b in p.near[:8]))
    return "\n".join(lines)


def _preview_kanban(shared: SharedDb, src: SourceTable, dest_cols: list[str]) -> str:
    """入れ替えると何枚の看板がどうなるか(書かない)。"""
    try:
        current = shared.select(f"SELECT * FROM {_q(src.name)}")
    except SharedDbError:
        return ""
    key = config.COL_KEY
    by_key = _by_key(current)
    compare = [c for c in src.columns if c in dest_cols and c not in STATE_COLUMNS]
    changed = added = 0
    for s in src.rows:
        now = by_key.get(_key(s.get(key)))
        if now is None:
            added += 1
        elif any(_text(s.get(c)) != _text(now.get(c)) for c in compare):
            changed += 1
    src_keys = {_key(s.get(key)) for s in src.rows}
    gone = [r for k, r in by_key.items() if k not in src_keys]
    kept = sum(1 for r in gone if _active(r))
    doubled = len(current) - len(by_key)
    if not (changed or added or gone or doubled):
        return "Access と同じです(入れ替えても変わりません)"
    parts = [f"変わる {changed}", f"足す {added}", f"消す {len(gone) - kept}"]
    if kept:
        parts.append(f"残す(Access に無いが発注中など) {kept}")
    text = "看板: " + "・".join(parts) + " 枚"
    if doubled:
        text += f"(共有DBで同じ管理番号が重なっている {doubled} 行を 1 行にまとめます)"
    return text


def _text(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return "" if value is None else str(value).strip()


def _order(name: str) -> tuple:
    """看板の表をラインの並びで先に、ほかは名前順。"""
    codes = config.all_line_codes()
    if is_kanban_table(name):
        code = name[len(config.KANBAN_TABLE_PREFIX):]
        return (0, codes.index(code) if code in codes else len(codes), name)
    return (1, 0, name)


def _count(shared: SharedDb, name: str) -> int:
    try:
        return int(shared.select(f"SELECT COUNT(*) AS n FROM {_q(name)}")[0]["n"])
    except (SharedDbError, IndexError, KeyError):
        return 0


# ------------------------------------------------------------------
# 入れ替える
# ------------------------------------------------------------------
@dataclass
class RefreshResult:
    ok: bool
    message: str
    reason: str = ""
    refreshed: list[tuple[str, int, int]] = field(default_factory=list)   # (表, 前, 後)
    notes: list[str] = field(default_factory=list)
    backup: str = ""
    written: str = ""             # 書き込んだファイル(共有DB)
    written_at: str = ""          # そのファイルの更新日時(書いたあと)
    verified: str = ""            # 書いたあと、ファイルを直接開き直して確かめた結果


def refresh(source_path: str, shared: SharedDb | None, tables: Iterable[str],
            backup_dir: Path) -> RefreshResult:
    """選んだ表の中身を、Access(選んだファイル)の中身に入れ替える。"""
    src_path, why, reason = _check_paths(source_path, shared)
    if src_path is None:
        return RefreshResult(False, why, reason)
    assert shared is not None
    wanted = [t for t in dict.fromkeys(str(t) for t in tables) if t]
    if not wanted:
        return RefreshResult(False, "入れ替える表を選んでください。", REFUSE_NOTHING)
    try:
        source = read_source(src_path)
        dest_names = set(shared.table_names())
    except (SourceError, SharedDbError) as exc:
        return RefreshResult(False, str(exc), REFUSE_READ)

    refusals = []
    for name in wanted:
        if name not in source:
            refusals.append(f"{name}(選んだファイルに無い)")
        elif name not in dest_names:
            refusals.append(f"{name}(共有DBに無い)")
        else:
            src = source[name]
            why = refresh_why(name, src, shared.column_names(name), _suspect_rows(src))
            if why:
                refusals.append(f"{name}({why})")
    if refusals:
        return RefreshResult(False, "入れ替えられない表があります: " + "、".join(refusals)
                             + "。何も変えていません。", REFUSE_NOTHING)

    try:
        backup = _backup(Path(shared.path), backup_dir)
    except OSError as exc:
        return RefreshResult(False, f"書く前の控えを取れませんでした({exc})。何も変えていません。",
                             REFUSE_WRITE_FAILED)
    applog.info("表の中身を入れ替えます: %s → %s(控え: %s)", src_path, shared.path, backup)

    done: list[tuple[str, int, int]] = []
    notes: list[str] = []
    failed: list[str] = []
    for name in wanted:
        try:
            before, after, note = _refresh_one(shared, source[name])
        except (sqlite3.Error, SharedDbError) as exc:
            failed.append(f"{name}({exc})")
            applog.warning("表の中身を入れ替えられませんでした: %s: %s", name, exc)
            continue
        done.append((name, before, after))
        if note:
            notes.append(f"{name}: {note}")
        applog.info("表の中身を入れ替えました: %s %d→%d行 %s", name, before, after, note)

    if failed and not done:
        return RefreshResult(False, "入れ替えられませんでした: " + "、".join(failed)
                             + "。共有DBは変えていません。", REFUSE_WRITE_FAILED, backup=str(backup))
    message = ("中身を Access の最新に入れ替えました: "
               + "、".join(f"{t}({b:,}行 → {a:,}行)" for t, b, a in done) + "。")
    if failed:
        message += " ただし次は入れ替えられませんでした(元のまま): " + "、".join(failed)
    written_at, verified = _verify_written(Path(shared.path), done)
    return RefreshResult(not failed, message, "" if not failed else REFUSE_WRITE_FAILED,
                         refreshed=done, notes=notes, backup=str(backup),
                         written=shared.path, written_at=written_at, verified=verified)


def _verify_written(path: Path, done: list[tuple[str, int, int]]) -> tuple[str, str]:
    """書いたあと、**共有DBのファイルを直接開き直して**確かめる(手元の写しは使わない)。

    画面の表示は手元の写しから出るので、「表示は変わったがファイルは?」に答えられない。
    ファイルそのものの更新日時と、入れ替えた表の行数を読み直して返す。
    """
    try:
        written_at = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y/%m/%d %H:%M:%S")
    except OSError:
        written_at = ""
    try:
        conn = sqlite3.connect(str(path))
        conn.execute("PRAGMA query_only = ON")
        try:
            counts = {t: int(conn.execute(f"SELECT COUNT(*) FROM {_q(t)}").fetchone()[0]) for t, _b, _a in done}
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return written_at, f"ファイルを開き直して確かめられませんでした({exc})"
    wrong = [f"{t}(ファイルでは {counts[t]} 行)" for t, _b, a in done if counts.get(t) != a]
    if wrong:
        return written_at, "⚠ ファイルの中身が入れ替えた結果と合いません: " + "、".join(wrong)
    return written_at, ("ファイルを開き直して確かめました: "
                        + "、".join(f"{t} {counts[t]:,} 行" for t, _b, _a in done))


def _refresh_one(shared: SharedDb, src: SourceTable) -> tuple[int, int, str]:
    """1 つの表を入れ替える(1 回で確定)。``(前の行数, 後の行数, 添える一言)``。"""
    name = src.name
    with shared.transaction() as conn:
        dest_cols = [r[1] for r in conn.execute(f"PRAGMA table_info({_q(name)})")]
        common = [c for c in src.columns if c in dest_cols]
        current = [dict(r) for r in conn.execute(f"SELECT * FROM {_q(name)}")]
        before = len(current)
        note = ""
        if is_kanban_table(name):
            rows, note = _merge_kanban(src, current, common, dest_cols)
        else:
            rows = [{c: r.get(c) for c in common} for r in src.rows]
        conn.execute(f"DELETE FROM {_q(name)}")
        for row in rows:
            cols = list(row)
            conn.execute(
                f"INSERT INTO {_q(name)} ({', '.join(_q(c) for c in cols)})"
                f" VALUES ({', '.join('?' for _ in cols)})",
                [row[c] for c in cols],
            )
        after = int(conn.execute(f"SELECT COUNT(*) FROM {_q(name)}").fetchone()[0])
    return before, after, note


def _by_key(current: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """共有DBの看板を管理番号で引く。**同じ管理番号が重なっていたら 1 行にする**
    (発注中・発送済み・注文中の行を優先し、同じなら後の行)。入れ替えのたびに重なりが
    片付き、増えていかない。"""
    out: dict[str, dict[str, Any]] = {}
    for row in current:
        k = _key(row.get(config.COL_KEY))
        if k in out and _active(out[k]) and not _active(row):
            continue
        out[k] = row
    return out


def _active(row: dict[str, Any]) -> bool:
    """発注中・発送済み・注文中のどれか(消すと動いている発注を見失う)。"""
    on = config.MARK_ON
    return any(str(row.get(c) or "").strip() == on
               for c in (config.COL_WANT, config.COL_SHIPPED, config.COL_HOLD))


def _merge_kanban(src: SourceTable, current: list[dict[str, Any]], common: list[str],
                  dest_cols: list[str]) -> tuple[list[dict[str, Any]], str]:
    """看板の表: 資材・サイズなどは Access、状態はいまのまま。"""
    key = config.COL_KEY
    by_key = _by_key(current)
    doubled = len(current) - len(by_key)
    states = [c for c in STATE_COLUMNS if c in dest_cols]
    only_dest = [c for c in dest_cols if c not in common and c not in states]
    rows: list[dict[str, Any]] = []
    added = 0
    for s in src.rows:
        k = _key(s.get(key))
        now = by_key.get(k)
        row = {c: s.get(c) for c in common}
        if now is not None:
            # 状態と、共有DBにだけある列は、いまの値を残す。**管理番号もいまの書き方のまま**
            # (TEXT の列へ Access の 1.0 を書くと '1.0' になり、端末の写しでは別の看板になる)
            for c in states + only_dest:
                row[c] = now.get(c)
            row[key] = now.get(key)
        else:
            added += 1
            if isinstance(row.get(key), float) and row[key].is_integer():
                row[key] = int(row[key])
            for c in states:
                if c not in common:
                    row[c] = INITIAL_STATE[c]
        rows.append(row)
    src_keys = {_key(s.get(key)) for s in src.rows}
    kept = [r for k, r in by_key.items() if k not in src_keys and _active(r)]
    dropped = sum(1 for k, r in by_key.items() if k not in src_keys and not _active(r))
    rows.extend({c: r.get(c) for c in dest_cols} for r in kept)
    parts = ["状態はいまのまま"]
    if doubled:
        parts.append(f"共有DBで同じ管理番号が重なっていた {doubled} 行を 1 行にまとめました")
    if added:
        parts.append(f"Access で足された看板 {added} 枚を足しました")
    if dropped:
        parts.append(f"Access に無い看板 {dropped} 枚を消しました")
    if kept:
        parts.append("Access に無いが発注中・発送済み・注文中の看板 "
                     f"{len(kept)} 枚({', '.join(_key(r.get(key)) for r in kept[:10])})は残しました")
    return rows, "・".join(parts)


def _backup(dest: Path, folder: Path) -> Path:
    """書く前に共有DBを手元へ写す。戻したいときの頼り。古いものから消して数を保つ。"""
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{dest.stem}_中身を入れ替える前_{datetime.now():%Y%m%d_%H%M%S}{dest.suffix}"
    shutil.copyfile(dest, target)
    old = sorted(folder.glob(f"{dest.stem}_中身を入れ替える前_*{dest.suffix}"))
    for stale in old[:-BACKUP_KEEP]:
        try:
            stale.unlink()
        except OSError:
            pass
    return target


# ------------------------------------------------------------------
# 落としたファイルを受け取る
# ------------------------------------------------------------------
def save_upload(filename: str, stream: Any, folder: Path,
                expected: int | None = None) -> tuple[Path | None, str]:
    """ドロップされたファイルを手元の作業フォルダへ置く。``(置いた場所, 断る理由)``。

    名前はファイル名の部分だけを使う(フォルダを含む名前で外へ書かせない)。
    同じ名前が来たら置き換える ── 直して落とし直すのがふつうの使い方なので。
    ``expected``(画面が知っている大きさ)と届いた大きさが違えば置かない ──
    欠けたファイルを読んで「ファイルが小さすぎます」と言うより、届かなかったと言う
    (デスクトップ版の窓は、FormData に入れたファイルの中身を渡さないことがあった)。
    """
    name = Path(str(filename).replace("\\", "/")).name
    if Path(name).suffix.lower() not in UPLOAD_SUFFIXES:
        return None, "Access(.accdb / .mdb)か sqlite3(.sqlite3 / .db)のファイルを落としてください。"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    written = 0
    try:
        with open(target, "wb") as fh:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > UPLOAD_LIMIT_BYTES:
                    raise ValueError("大きすぎます")
                fh.write(chunk)
    except (OSError, ValueError) as exc:
        target.unlink(missing_ok=True)
        return None, f"ファイルを受け取れませんでした({exc})"
    if expected is not None and written != expected:
        target.unlink(missing_ok=True)
        applog.warning("入れ替えに使うファイルが欠けて届きました: %s (%d / %d バイト)", name, written, expected)
        return None, (f"{name} が欠けて届きました({written:,} / {expected:,} バイト)。"
                      "もう一度落とすか、「参照...」でファイルの場所を選んでください。")
    _cache.pop(str(target), None)
    applog.info("入れ替えに使うファイルを受け取りました: %s (%d バイト)", target, written)
    return target, ""


def _facts_dict(f: FileFacts) -> dict[str, Any]:
    return {"path": f.path, "name": Path(f.path).name if f.path else "", "size": f.size,
            "modified": f.modified, "tables": f.tables, "kanban_tables": f.kanban_tables}


def plan_dict(p: Plan) -> dict[str, Any]:
    return {
        "source": p.source, "dest": p.dest, "ok": p.ok, "message": p.message,
        "access": is_access(Path(p.source)) if p.source else False,
        "only_in_source": p.only_in_source, "only_in_dest": p.only_in_dest,
        "hint": p.hint, "near": [list(n) for n in p.near],
        "source_facts": _facts_dict(p.source_facts), "dest_facts": _facts_dict(p.dest_facts),
        "tables": [{"name": c.name, "rows": c.rows, "current_rows": c.current_rows,
                    "columns": c.columns, "not_copied": c.not_copied, "suspect": c.suspect,
                    "refresh_why": c.refresh_why, "can_refresh": c.can_refresh,
                    "keeps": c.keeps, "preview": c.preview} for c in p.candidates],
    }
