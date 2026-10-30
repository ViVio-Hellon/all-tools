"""日報データ.accdb から写す ── VBA が書いている Access の日報を、共有の 日報データ.sqlite3 へ(v4.36.0)

    「日報データ.accdb から写す」ボタンを作ってよいか → お願いします
    共有の表の列名「頁」と「ページ」のずれを直してよいか → 直してください
    VBA の日報をまだしばらく使い続けるか → 使います

【どこから・どこへ】
写す元は VBA が書いている `日報データ.accdb`(既定は共有の日報データと同じフォルダ。
画面で選べる)。写す先は共有の日報データ(設定の「共有の日報管理のパス」── 「共有へ
保存」と同じファイル)で、**sqlite3 のときだけ**です。写す元には**書きません**(読むだけ)。

    T_日報ヘッダー_<ライン> / T_日報明細_<ライン>  →  同じ名前の表(ラインは正規の呼び名)

【読み方 ── ACE を使わない】
ライン PC の Python には追加のライブラリを入れられないので、Access のファイルを
**そのままバイトで読みます**(`nippou/accdb` ── 看板の読み取りの写し)。読めない形は
理由を言って断ります(`check_format`):

    古い形(Access 97 までの .mdb)・暗号化(パスワード付き)・壊れている・Access ではない

**読めた行数が表の定義の行数と違う表は写しません**(看板 3.5.2 と同じ守り。読み落とした
ページを「Access に無い」と取り違えないため)。そのラインはまるごと写しません。

【写し方】
    ・列の名前の読み替え: `頁` → 写す先の表の列(`頁` か `ページ`。`sqlite_backend.page_column`)
    ・ライン名を正規へ(`line_names.upgrade`。`LS` の表は `機側` の表へ)
    ・作業停止④⑤(S4〜TH5)は Access の表に無いので空
    ・保存日時は ISO の形(`2026-07-22T12:31:42`。このツールが共有へ保存するときと同じ形)
    ・写す先に表が無ければ作る(ページの列は VBA と同じ `頁`、主キーは「共有へ保存」と同じ)
    ・知らないライン(`予備PC` など、このツールに無いライン)は写さず、一覧で知らせる

【値は打ったままの字・重なりを作らない】

    .accdb から写す はほかのツールでもある機能ですが、数値の丸め方が間違っていたり、
    重複が発生したり不具合が多数出ました 注意してください

    ・明細の値は Access でも文字。**数に直さない・丸めない・空白も削らない**(`_values`)。
      揃えるのはページの鍵(報告日・直の書き方 ── `logic/accdb_copy.normal_date`)だけ
    ・Access の表には主キーが無いので、同じページが2つあることがある(前の名前の表と正規の
      表の両方にある、報告日の書き方違い、も同じ)。**まったく同じなら1つ**にし、違えば
      **保存日時がいちばん新しいほう**(VBA は保存し直すと保存日時を入れ直すので、最後に
      保存した中身)。いちばん新しいのが2つ・保存日時を読めない・同じ行番号の明細が違う、
      のときはどちらが正しいか決められないので、**そのページは写さずに一覧で出す**
    ・書いたら**読み直して、Access の値と型まで1文字ずつ比べる**(`compare`)。合わなければ
      そのラインを書く前に戻す。確定したあとも開き直してもう一度比べる(`_verify_after`)

【写し直し ── 何度押しても同じ】
ページ(報告日・ライン・直・ページ)ごとに `logic/accdb_copy.decide` で決めます:
無いページは足す、両方にあるページは保存日時の新しいほうを残す、このツールが書いた
ページは古い Access の中身で上書き・消去しない、Access から消えたページは消さない。

【書く前・書くとき】
    ・下見(`survey`)は写す先を開かず、写しを読みます(`source_db` ── コンバートと同じ)
    ・実行(`run`)は写す先を開き、**`BEGIN IMMEDIATE` でロックを取ってから**読み直して
      決めます。ほかの PC が書いている最中なら `BUSY_TIMEOUT_MS` だけ待ち、それでも
      取れなければ断ります(何も書いていない)
    ・書くことがあれば、書く前に写す先の控えを取ります(写す先の隣の `backup` フォルダ。
      ロックを持ったまま写すので、写している間に誰も書きません)。控えを取れなければ
      写しません
    ・**1ラインずつ**区切り(SAVEPOINT)を置き、書いたページの見出し・明細の数を Access と
      突き合わせます。合わなければそのラインだけ取り消します(ほかのラインは進む)

【重さ】
押したときだけ動きます。本番の写し(5ライン 1,028 ページ・明細 12,336 行)で、読むのに
0.5 秒、写すのに 0.8 秒、2回目(全部そのまま)は 0.3 秒でした(ローカルのディスク)。
"""
from __future__ import annotations

import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ..access_bridge import pusher, sqlite_backend as backend
from ..config import SETTINGS
from ..logging_setup import get_logger
from ..logic import accdb_copy as rule
from ..logic import line_names
from ..logic import line_rename as rename_rule

log = get_logger("services.accdb_copy")

HEADER_PREFIX = "T_日報ヘッダー_"
DETAIL_PREFIX = "T_日報明細_"

#: 控えを残す数(古いものから消す)
BACKUP_KEEP = 10
BACKUP_DIR = "backup"

#: Access が自分のために持っている表(添付ファイルの `f_…_Data` など)
_ACCESS_INTERNAL = re.compile(r"^(MSys|~|f_[0-9A-Fa-f]{32}_)")


# ==================================================================
# 結果
# ==================================================================
@dataclass
class LineResult:
    """1つのラインの結果。"""

    line: str
    tables: list[str] = field(default_factory=list)       # Access の表
    source_pages: int = 0
    source_rows: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    checked: str = ""
    #: 写さなかった・書けなかった理由(空なら写した / 写せる)
    error: str = ""
    notes: list[str] = field(default_factory=list)
    #: 重なっていて写さなかったページ(「2026年7月22日 L-1 1直 1ページ: わけ」)
    skipped: list[str] = field(default_factory=list)
    #: 書いたページ(確定したあとに読み直して比べる。画面には出さない)
    written: list = field(default_factory=list, repr=False)

    def as_dict(self) -> dict[str, Any]:
        return {"line": self.line, "label": line_names.label(self.line),
                "tables": list(self.tables), "source_pages": self.source_pages,
                "source_rows": self.source_rows, "counts": dict(self.counts),
                "checked": self.checked, "error": self.error, "notes": list(self.notes),
                "skipped": list(self.skipped)}


@dataclass
class Unknown:
    """このツールに無いライン(写さない)。"""

    line: str
    tables: list[str] = field(default_factory=list)
    pages: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"line": self.line, "tables": list(self.tables), "pages": self.pages}


@dataclass
class Report:
    """1回ぶんの結果(下見 / 実行)。"""

    dry_run: bool
    source: str = ""
    dest: str = ""
    lines: list[LineResult] = field(default_factory=list)
    unknown: list[Unknown] = field(default_factory=list)
    #: 日報の表ではない表(見ていない)
    other_tables: list[str] = field(default_factory=list)
    error: str = ""
    #: 取った控え(実行して、書くことがあったとき)
    backup: str = ""
    at: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and not any(line.error for line in self.lines)

    def total(self, kind: str) -> int:
        return sum(line.counts.get(kind, 0) for line in self.lines)

    def message(self) -> str:
        """1行で言う(画面の知らせ)。"""
        if self.error:
            return self.error
        head = "下見しました(まだ写していません)" if self.dry_run else "写しました"
        verb = "足す" if self.dry_run else "足した"
        renew = "新しくする" if self.dry_run else "新しくした"
        parts = [f"{verb} {self.total(rule.ADD)}ページ",
                 f"{renew} {self.total(rule.UPDATE)}ページ",
                 f"そのまま {self.total(rule.SAME)}ページ"]
        if self.total(rule.KEEP):
            parts.append(f"共有を残す {self.total(rule.KEEP)}ページ")
        text = f"{head}: " + "・".join(parts)
        if self.unknown:
            text += ("。知らないライン(写さない): "
                     + "・".join(f"{u.line} {u.pages}ページ" for u in self.unknown))
        failed = [line.line for line in self.lines if line.error]
        if failed:
            text += f"。写せなかったライン: {'・'.join(failed)}"
        return text

    def as_dict(self) -> dict[str, Any]:
        return {"dry_run": self.dry_run, "ok": self.ok, "message": self.message(),
                "source": self.source, "dest": self.dest, "at": self.at,
                "error": self.error, "backup": self.backup, "kinds": list(rule.KINDS),
                "lines": [line.as_dict() for line in self.lines],
                "unknown": [u.as_dict() for u in self.unknown],
                "other_tables": list(self.other_tables)}


# ==================================================================
# 写す元(Access)を読む
# ==================================================================
_JET_MAGIC = b"\x00\x01\x00\x00"
_JET_NAMES = (b"Standard Jet DB", b"Standard ACE DB")
_SQLITE_MAGIC = b"SQLite format 3\x00"
#: ファイルの頭のうち RC4 で隠してある欄(0x18 から)。中に「暗号化の鍵」(0x3E)がある
_HEADER_KEY = bytes((0xC7, 0xDA, 0x39, 0x6B))
_PAGE_TABLE_DEF = 0x02


def _rc4(key: bytes, data: bytes) -> bytes:
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) & 0xFF
        s[i], s[j] = s[j], s[i]
    out = bytearray()
    i = j = 0
    for byte in data:
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        out.append(byte ^ s[(s[i] + s[j]) & 0xFF])
    return bytes(out)


def check_format(path: Path) -> str:
    """読めない形なら**その理由**(画面にそのまま出す)。読める形なら空。

    名前だけ変えたファイル・書きかけ・コピーの途中で空のまま、などを「読めませんでした」の
    一言で済ませないため、読む前に頭を見ます(看板 `table_refresh.sniff` と同じ考え)。
    """
    path = Path(path)
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            head = fh.read(4096 * 3)
    except OSError as exc:
        return f"{path.name} を開けませんでした({exc})。"
    where = f"{path.name}({size:,} バイト)"
    if size == 0:
        return (f"{where} は空です。書きかけか、コピーに失敗したファイルです。"
                "VBA が書いている 日報データ.accdb を選び直してください。")
    if head.startswith(_SQLITE_MAGIC):
        return (f"{where} は sqlite3 のファイルです。写す元には、VBA が書いている"
                " Access のファイル(日報データ.accdb)を選んでください。")
    if head.startswith(b"PK\x03\x04") or head.startswith(b"\xd0\xcf\x11\xe0"):
        return f"{where} の中身は Access ではなく、Excel など Office の別の形です。"
    if not (head[:4] == _JET_MAGIC and head[4:19] in _JET_NAMES):
        if not head[:32].strip(b"\x00"):
            return (f"{where} は頭が 0 ばかりで、Access のファイルとして読めません。"
                    "壊れているか、コピーの途中です。")
        return (f"{where} の中身は Access のファイルではないようです"
                f"(頭の16バイト: {head[:16].hex(' ')})。")
    if head[0x14] == 0x00:
        return (f"{where} は Access 97 までの古い形(.mdb)なので読めません。"
                "Access で開いて .accdb の形に変えてから選んでください。")
    if len(head) < 4096 * 3:
        return f"{where} は途中で切れています(壊れているか、コピーの途中です)。"
    hidden = _rc4(_HEADER_KEY, head[0x18:0x18 + 128])
    if hidden[0x3E - 0x18:0x3E - 0x18 + 4] != b"\x00\x00\x00\x00":
        return (f"{where} は暗号化されている(パスワードが付いている)ので読めません。"
                "Access でパスワードを外した写しを作ってから選んでください。")
    if head[2 * 4096] != _PAGE_TABLE_DEF:
        return (f"{where} は表の一覧のページが読めません。暗号化されている(パスワードが"
                "付いている)か、壊れています。Access で開けるか確かめ、「データベースの"
                "最適化/修復」をしてから選び直してください。")
    return ""


@dataclass
class _Table:
    name: str
    columns: list[str]
    rows: list[dict[str, Any]]
    defined: int


#: 同じファイルを何度も読まない(下見 → 実行 で 2 回読む)。姿(大きさ・更新日時)が変われば読み直す
_cache: dict[str, tuple[tuple[int, int], dict[str, _Table]]] = {}


class SourceError(Exception):
    """写す元を読めなかった。文は画面にそのまま出す。"""


def read_source(path: Path) -> dict[str, _Table]:
    """写す元の表を全部読む(Access の内部の表は入れない)。"""
    from ..accdb import reader

    path = Path(path)
    why = check_format(path)
    if why:
        raise SourceError(why)
    stat = path.stat()
    stamp = (stat.st_size, stat.st_mtime_ns)
    hit = _cache.get(str(path))
    if hit and hit[0] == stamp:
        return hit[1]
    try:
        out: dict[str, _Table] = {}
        with reader.AccdbReader(str(path)) as db:
            for name in db.table_names():
                if _ACCESS_INTERNAL.match(name):
                    continue
                table = db.read_table(name)
                out[name] = _Table(name, table.column_names(), table.rows,
                                   db.defined_rows(name))
    except Exception as exc:                      # noqa: BLE001 - 理由を画面に出す
        log.exception("写す元の Access を読めませんでした: %s", path)
        raise SourceError(f"{path.name} を読めませんでした({exc})。暗号化されているか、"
                          "壊れているかもしれません。") from exc
    _cache.clear()                                # 大きいので 1 つだけ覚える
    _cache[str(path)] = (stamp, out)
    return out


def forget() -> None:
    _cache.clear()


# ==================================================================
# ラインごとにまとめる
# ==================================================================
PageKey = rename_rule.PageKey


@dataclass
class _Page:
    """写す元の1ページ。値は**写す先へ書く形**(中の呼び名の並び ── `HEADER_COLUMNS` /
    `DETAIL_COLUMNS`)。読み直して1文字ずつ比べるのにも、この形を使う。"""

    header: Optional[tuple[Any, ...]]
    rows: dict[int, tuple[Any, ...]]
    saved: str
    table: str                                    # どの表から(見出しの表の名前)

    def same_as(self, other: "_Page") -> bool:
        return _exact(self.header) == _exact(other.header) and (
            {n: _exact(r) for n, r in self.rows.items()}
            == {n: _exact(r) for n, r in other.rows.items()})


def _exact(values: Optional[tuple[Any, ...]]) -> Optional[tuple[str, ...]]:
    """**型まで含めて**比べる形(`1` と `1.0`、`"08"` と `8` を別のものとして見る)。"""
    return None if values is None else tuple(repr(v) for v in values)


@dataclass
class _Source:
    """写す元の1ライン。"""

    line: str
    tables: list[str] = field(default_factory=list)
    pages: dict[PageKey, _Page] = field(default_factory=dict)
    #: 重なっていて写さなかったページ → わけ
    ambiguous: dict[PageKey, str] = field(default_factory=dict)
    other_rows: int = 0
    #: まったく同じ行・ページが2つあった(1つにした)
    identical: int = 0
    #: 同じページが2つあり、保存日時の新しいほうを採った
    newer_taken: int = 0
    bad_rows: int = 0
    dropped: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def saved(self) -> dict[PageKey, str]:
        return {key: page.saved for key, page in self.pages.items()}

    @property
    def rows(self) -> int:
        return sum(len(page.rows) for page in self.pages.values())


def _split(name: str) -> tuple[str, str]:
    """表の名前 → (`header` / `detail` / "", 名前のライン部分)。"""
    if name.startswith(HEADER_PREFIX):
        return "header", name[len(HEADER_PREFIX):]
    if name.startswith(DETAIL_PREFIX):
        return "detail", name[len(DETAIL_PREFIX):]
    return "", ""


def _row_key(row: dict[str, Any], page: backend.PageColumn) -> Optional[PageKey]:
    """表の1行 → ページの鍵。**報告日・直はこのツールの書き方に揃える**(重なりを作らない)。"""
    number = rule.page_number(page.value(row))
    if number is None:
        return None
    return (rule.normal_date(row.get("報告日")), rule.normal_shift(row.get("直")), number)


def _guard(table: _Table) -> str:
    """その表を写せない理由(読めた行数が表の定義と違う)。写せるなら空。"""
    if table.defined != len(table.rows):
        return (f"Access の表 {table.name} は、表の定義では {table.defined:,}行 ですが、"
                f"読めたのは {len(table.rows):,}行 です。Access で「データベースの最適化/"
                "修復」をしてから、もう一度押してください(このラインは写していません)")
    return ""


def _gather(tables: dict[str, _Table]) -> tuple[list[_Source], list[Unknown], list[str]]:
    """表をラインごとに。(このツールのライン, 知らないライン, 日報の表ではない表)。"""
    groups: dict[str, dict[str, dict[str, _Table]]] = {}
    others: list[str] = []
    for name in sorted(tables):
        kind, part = _split(name)
        if not kind:
            others.append(name)
            continue
        line = line_names.upgrade(part)
        groups.setdefault(line, {}).setdefault(part, {})[kind] = tables[name]

    sources: list[_Source] = []
    unknown: list[Unknown] = []
    for line, parts in groups.items():
        if line_names.by_official(line) is None:
            pages = set()
            names = []
            for pair in parts.values():
                for table in pair.values():
                    names.append(table.name)
                    page = backend.PageColumn(backend.page_names_of(table.columns))
                    pages |= {k for k in (_row_key(r, page) for r in table.rows) if k}
            unknown.append(Unknown(line, names, len(pages)))
            continue
        sources.append(_read_line(line, parts))
    order = {code: i for i, code in enumerate(line_names.codes())}
    sources.sort(key=lambda s: order.get(s.line, len(order)))
    return sources, unknown, others


def _values(row: dict[str, Any], columns: tuple[str, ...], line: str, key: PageKey,
            saved: str) -> tuple[Any, ...]:
    """写す元の1行 → 写す先の1行(中の呼び名の並び)。

    **値は打ったままの字**(冒頭)。揃えるのは鍵(報告日・ライン・直・ページ・行番号)と
    保存日時だけです。
    """
    out: list[Any] = []
    for column in columns:
        if column == "報告日":
            out.append(key[0])
        elif column == "ライン":
            out.append(line)
        elif column == "直":
            out.append(key[1])
        elif column == backend.PAGE:
            out.append(key[2])
        elif column == "行番号":
            out.append(rule.page_number(row.get(column)))
        elif column == "保存日時":
            out.append(saved)
        else:
            value = row.get(column)
            out.append(rule.saved_text(value) if isinstance(value, datetime) else value)
    return tuple(out)


def _read_pair(line: str, pair: dict[str, _Table], out: _Source,
               dropped: set[str]) -> dict[PageKey, _Page]:
    """同じ名前の(見出しの表, 明細の表)1組 → ページ。組の中の重なりはここで片付ける。"""
    headers: dict[PageKey, list[tuple[Any, ...]]] = {}
    details: dict[PageKey, dict[int, list[tuple[Any, ...]]]] = {}
    known = {"header": set(backend.HEADER_COLUMNS), "detail": set(backend.DETAIL_COLUMNS)}
    table_name = ""
    for kind in ("header", "detail"):
        table = pair.get(kind)
        if table is None:
            continue
        table_name = table_name or table.name
        page = backend.PageColumn(backend.page_names_of(table.columns))
        dropped |= set(table.columns) - known[kind] - set(backend.PAGE_NAMES)
        for raw in table.rows:
            if line_names.upgrade(raw.get("ライン")) != line:
                out.other_rows += 1
                continue
            key = _row_key(raw, page)
            if key is None:
                out.bad_rows += 1
                continue
            row = page.normalized(raw)
            if kind == "header":
                saved = rule.saved_text(row.get("保存日時"))
                headers.setdefault(key, []).append(
                    _values(row, backend.HEADER_COLUMNS, line, key, saved))
            else:
                number = rule.page_number(row.get("行番号"))
                if number is None:
                    out.bad_rows += 1
                    continue
                details.setdefault(key, {}).setdefault(number, []).append(
                    _values(row, backend.DETAIL_COLUMNS, line, key, ""))

    saved_at = backend.HEADER_COLUMNS.index("保存日時")
    pages: dict[PageKey, _Page] = {}
    for key in set(headers) | set(details):
        header = _pick_header(key, headers.get(key, []), saved_at, out)
        if header is False:
            continue
        rows: dict[int, tuple[Any, ...]] = {}
        for number, found in details.get(key, {}).items():
            unique = {_exact(r): r for r in found}
            out.identical += len(found) - len(unique)
            if len(unique) > 1:
                out.ambiguous[key] = (f"行番号 {number} の明細が {len(unique)} 行あり、"
                                      "中身が違います(どちらが正しいか決められません)")
                break
            rows[number] = next(iter(unique.values()))
        if key in out.ambiguous:
            continue
        pages[key] = _Page(header=header, rows=rows,
                           saved=header[saved_at] if header else "", table=table_name)
    return pages


def _pick_header(key: PageKey, found: list[tuple[Any, ...]], saved_at: int, out: _Source):
    """同じページの見出しが何行かあるとき(Access の表には主キーが無いので起こりうる)。

    まったく同じなら1つに。違えば**保存日時がいちばん新しい1行**を採る(VBA は保存し直すと
    保存日時を入れ直すので、新しいほうが最後に保存した中身)。いちばん新しいのが2行ある・
    保存日時を読めないときは、どちらが正しいか決められないので**そのページは写さない**
    (False を返す。一覧で知らせる)。見出しが無ければ None。
    """
    if not found:
        return None
    unique = list({_exact(r): r for r in found}.values())
    out.identical += len(found) - len(unique)
    if len(unique) == 1:
        return unique[0]
    times = [rule.when(r[saved_at]) for r in unique]
    if all(t is not None for t in times):
        newest = max(times)
        if times.count(newest) == 1:
            out.newer_taken += 1
            return unique[times.index(newest)]
    out.ambiguous[key] = (f"見出しが {len(unique)} 行あり、中身が違います"
                          "(保存日時でも新しいほうを決められません)")
    return False


def _read_line(line: str, parts: dict[str, dict[str, _Table]]) -> _Source:
    out = _Source(line=line, tables=[t.name for pair in parts.values() for t in pair.values()])
    for pair in parts.values():
        for table in pair.values():
            why = _guard(table)
            if why:
                out.error = why
                return out
    dropped: set[str] = set()
    # **前の名前の表(LS)と正規の表(機側)の両方に同じページがあるとき**も同じ決め方:
    # 中身が同じなら1つ、違えば保存日時の新しいほう、決められなければ写さない
    for part in sorted(parts):
        for key, page in _read_pair(line, parts[part], out, dropped).items():
            if key in out.ambiguous:
                continue
            had = out.pages.get(key)
            if had is None:
                out.pages[key] = page
                continue
            if had.same_as(page):
                out.identical += 1
                continue
            mine, theirs = rule.when(page.saved), rule.when(had.saved)
            if mine is not None and theirs is not None and mine != theirs:
                out.newer_taken += 1
                if mine > theirs:
                    out.pages[key] = page
                continue
            del out.pages[key]
            out.ambiguous[key] = (f"{had.table} と {page.table} の両方にあり、中身が違います"
                                  "(保存日時でも新しいほうを決められません)")
    out.dropped = sorted(dropped)
    return out


# ==================================================================
# 写す先(共有の日報データ)
# ==================================================================
def _tables(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def _dest_pages(conn: sqlite3.Connection, line: str,
                tables: set[str]) -> dict[rename_rule.PageKey, str]:
    """写す先のそのラインのページ → 保存日時(明細だけのページは空)。"""
    found: dict[rename_rule.PageKey, str] = {}
    header_table, detail_table = pusher.header_table_name(line), pusher.detail_table_name(line)
    if header_table in tables:
        page = backend.page_column(conn, header_table).sql()
        for date, shift, number, saved in conn.execute(
                f'SELECT "報告日", "直", {page}, "保存日時" FROM {backend.quote(header_table)}'
                ' WHERE "ライン"=?', (line,)):
            key = (rename_rule.text_of(date), rename_rule.text_of(shift),
                   rename_rule.number_of(number))
            found[key] = rename_rule.text_of(saved)
    if detail_table in tables:
        page = backend.page_column(conn, detail_table).sql()
        for date, shift, number in conn.execute(
                f'SELECT DISTINCT "報告日", "直", {page} FROM {backend.quote(detail_table)}'
                ' WHERE "ライン"=?', (line,)):
            key = (rename_rule.text_of(date), rename_rule.text_of(shift),
                   rename_rule.number_of(number))
            found.setdefault(key, "")
    return found


def _survey_line(conn: Optional[sqlite3.Connection], src: _Source,
                 tables: set[str]) -> tuple[LineResult, rule.Plan]:
    result = LineResult(line=src.line, tables=list(src.tables),
                        source_pages=len(src.saved), source_rows=src.rows)
    if src.error:
        result.error = src.error
        result.counts = {kind: 0 for kind in rule.KINDS}
        return result, rule.Plan()
    dest = _dest_pages(conn, src.line, tables) if conn is not None else {}
    plan = rule.decide(src.saved, dest)
    result.counts = plan.counts()
    if plan.count(rule.KEEP):
        result.notes.append(
            f"{plan.count(rule.KEEP)}ページは共有の日報データのほうが新しい(このツールで"
            "共有へ保存した)ので、Access の中身では上書きしません"
            + (f"(うち {plan.unknown}ページは保存日時を比べられませんでした)"
               if plan.unknown else ""))
    if src.other_rows:
        result.notes.append(f"ライン列が「{src.line}」でない行が {src.other_rows} 行あります"
                            "(写していません)")
    if src.identical:
        result.notes.append(f"まったく同じ行・ページが {src.identical} か所重なっていました"
                            "(1つにして写します)")
    if src.newer_taken:
        result.notes.append(f"同じページが2つあったものが {src.newer_taken} か所あり、"
                            "保存日時の新しいほうを写します")
    if src.ambiguous:
        result.skipped = [f"{rename_rule.describe_key(k, src.line)}: {why}"
                          for k, why in sorted(src.ambiguous.items(),
                                               key=lambda kv: rule._sort_key(kv[0]))]
        result.notes.append(f"同じページが重なっていて中身が違うので、{len(src.ambiguous)}ページは"
                            "写していません(Access で直してから、もう一度押してください)")
    if src.bad_rows:
        result.notes.append(f"ページ・行番号が数でない行が {src.bad_rows} 行あります(写していません)")
    if src.dropped:
        result.notes.append(f"このツールの表に無い列は写していません: {'・'.join(src.dropped)}")
    return result, plan


def _write_line(conn: sqlite3.Connection, src: _Source, plan: rule.Plan) -> rename_rule.Check:
    """1ラインぶんを書く(呼ぶ側が区切りを持つ)。

    書いたあと、**書いたページを読み直して、写す元と1文字ずつ(型まで)比べます**
    (`compare`)。合わなければ例外にして、呼ぶ側がこのラインを書く前に戻します。
    """
    header_table = pusher.header_table_name(src.line)
    detail_table = pusher.detail_table_name(src.line)
    head_page, detail_page = backend.ensure_tables(conn, header_table, detail_table)
    for key in plan.keys(rule.UPDATE):
        values = (key[0], src.line, key[1], key[2])
        conn.execute(f"DELETE FROM {backend.quote(detail_table)}"
                     f" WHERE {backend.key_where(detail_page)}", values)
        conn.execute(f"DELETE FROM {backend.quote(header_table)}"
                     f" WHERE {backend.key_where(head_page)}", values)
    header_sql = backend.insert_sql(header_table, backend.HEADER_COLUMNS, head_page)
    detail_sql = backend.insert_sql(detail_table, backend.DETAIL_COLUMNS, detail_page)
    written = plan.keys(rule.ADD) + plan.keys(rule.UPDATE)
    expected: dict[PageKey, tuple[bool, int]] = {}
    for key in written:
        page = src.pages[key]
        if page.header is not None:
            conn.execute(header_sql, head_page.values(backend.HEADER_COLUMNS, page.header))
        conn.executemany(detail_sql, [
            detail_page.values(backend.DETAIL_COLUMNS, row)
            for _number, row in sorted(page.rows.items())])
        expected[key] = (page.header is not None, len(page.rows))
    found = rename_rule.check(expected, _written(conn, src.line, set(written),
                                                 head_page, detail_page))
    if found.ok:
        problems = compare(conn, src, written)
        if problems:
            raise RuntimeError("書いた中身が Access と合いません("
                               + "・".join(problems[:3])
                               + (f" ほか{len(problems) - 3}件" if len(problems) > 3 else "")
                               + ")")
    return found


def compare(conn: sqlite3.Connection, src: _Source, keys: list[PageKey]) -> list[str]:
    """写す先にいまある中身を読み直し、写す元と**型まで含めて1文字ずつ**比べる。合わない所の一覧。

    看板 3.5.2 の「書いた中身を読み直して照らし合わせる」と同じ守り。数に直った(`08` → 8)・
    丸まった(`1.50` → 1.5)・空白が消えた・行が増えた(重なった)、をここで見つけます。
    確定する前(`_write_line`)と、確定したあとファイルを開き直して(`run`)の2回見ます。
    """
    header_table = pusher.header_table_name(src.line)
    detail_table = pusher.detail_table_name(src.line)
    head_page = backend.page_column(conn, header_table)
    detail_page = backend.page_column(conn, detail_table)
    head_cols = head_page.columns(backend.HEADER_COLUMNS)
    detail_cols = detail_page.columns(backend.DETAIL_COLUMNS)
    problems: list[str] = []
    for key in keys:
        page = src.pages[key]
        where = (key[0], src.line, key[1], key[2])
        name = rename_rule.describe_key(key, src.line)
        got = conn.execute(
            f"SELECT {', '.join(backend.quote(c) for c in head_cols)}"
            f" FROM {backend.quote(header_table)} WHERE {backend.key_where(head_page)}",
            where).fetchall()
        want = [] if page.header is None else [
            head_page.values(backend.HEADER_COLUMNS, page.header)]
        if [_exact(tuple(r)) for r in got] != [_exact(w) for w in want]:
            problems.append(f"{name} の見出し")
        got_rows = conn.execute(
            f"SELECT {', '.join(backend.quote(c) for c in detail_cols)}"
            f" FROM {backend.quote(detail_table)} WHERE {backend.key_where(detail_page)}",
            where).fetchall()
        mine = sorted(_exact(tuple(r)) for r in got_rows)
        theirs = sorted(_exact(detail_page.values(backend.DETAIL_COLUMNS, r))
                        for r in page.rows.values())
        if mine != theirs:
            problems.append(f"{name} の明細")
    return problems


def _written(conn: sqlite3.Connection, line: str, keys: set[PageKey],
             head_page: backend.PageColumn, detail_page: backend.PageColumn
             ) -> dict[PageKey, tuple[bool, int]]:
    """写す先にいまあるページ → (見出しがあるか, 明細の行数)。"""
    if not keys:
        return {}
    header_table = pusher.header_table_name(line)
    detail_table = pusher.detail_table_name(line)
    headers: dict[PageKey, int] = {}
    for date, shift, number in conn.execute(
            f'SELECT "報告日", "直", {head_page.sql()} FROM {backend.quote(header_table)}'
            ' WHERE "ライン"=?', (line,)):
        key = (rename_rule.text_of(date), rename_rule.text_of(shift),
               rename_rule.number_of(number))
        headers[key] = headers.get(key, 0) + 1
    rows: dict[PageKey, int] = {}
    page = detail_page.sql()
    for date, shift, number, count in conn.execute(
            f'SELECT "報告日", "直", {page}, COUNT(*) FROM {backend.quote(detail_table)}'
            f' WHERE "ライン"=? GROUP BY "報告日", "直", {page}', (line,)):
        key = (rename_rule.text_of(date), rename_rule.text_of(shift),
               rename_rule.number_of(number))
        rows[key] = rows.get(key, 0) + count
    # 見出しが2行ある(重なった)ページは「見出しがある」に数えない(合わないので取り消す)
    return {key: (headers.get(key, 0) == 1, rows.get(key, 0)) for key in keys}


# ==================================================================
# 控え
# ==================================================================
def _backup(dest: Path, now: datetime) -> Path:
    """写す先の控え(隣の `backup` フォルダ)。**ロックを持ったまま呼ぶ**(写している間に誰も書かない)。"""
    folder = dest.parent / BACKUP_DIR
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{dest.stem}_写す前_{now:%Y%m%d-%H%M%S}{dest.suffix}"
    shutil.copyfile(dest, target)
    old = sorted(folder.glob(f"{dest.stem}_写す前_*{dest.suffix}"))
    for path in old[:-BACKUP_KEEP]:
        try:
            path.unlink()
        except OSError:
            log.warning("古い控えを消せませんでした: %s", path)
    return target


# ==================================================================
# 入口
# ==================================================================
def default_source() -> Path:
    """写す元の既定。**共有の日報データと同じフォルダの 日報データ.accdb**。"""
    dest = Path(SETTINGS.access_db_path)
    return dest.parent / f"{Path(SETTINGS.access_db_name).stem}.accdb"


def _paths(source: object) -> tuple[Optional[Path], Optional[Path], str]:
    """(写す元, 写す先, 断る理由)。"""
    text = str(source or "").strip().strip('"')
    src = Path(text) if text else default_source()
    dest = Path(SETTINGS.access_db_path)
    if not pusher.is_sqlite_target(dest):
        return None, None, (f"写す先(共有の日報データ)が Access のファイルです({dest.name})。"
                            "写せるのは、共有の日報データが sqlite3 のときだけです")
    try:
        if not src.is_file():
            return None, None, f"写す元のファイルが見つかりません: {src}"
        if not dest.parent.is_dir():
            return None, None, f"共有の日報データの置き場所に届きません: {dest.parent}"
        if dest.exists() and src.resolve() == dest.resolve():
            return None, None, "写す元と写す先が同じファイルです"
    except OSError as exc:
        return None, None, f"ファイルに届きません: {exc}"
    return src, dest, ""


def _report(dry_run: bool, source: object) -> Report:
    text = str(source or "").strip().strip('"')
    return Report(dry_run=dry_run, source=text or str(default_source()),
                  dest=str(SETTINGS.access_db_path),
                  at=datetime.now().isoformat(timespec="seconds"))


def survey(source: object = "") -> Report:
    """**下見する(書かない)。** 写す先は開かず、写しを読んで数えるだけ。"""
    from .. import source_db

    report = _report(True, source)
    src_path, dest, why = _paths(source)
    if src_path is None or dest is None:
        report.error = why
        return report
    report.source = str(src_path)
    try:
        sources, report.unknown, report.other_tables = _gather(read_source(src_path))
    except SourceError as exc:
        report.error = str(exc)
        return report
    from ..db.connection import file_state

    try:
        # **写す先を確かめられなければ下見もしない**(無いとみなすと全部「足す」と出る)
        if file_state(dest):
            with source_db.open_source(dest) as conn:
                tables = _tables(conn)
                for src in sources:
                    report.lines.append(_survey_line(conn, src, tables)[0])
        else:
            for src in sources:
                report.lines.append(_survey_line(None, src, set())[0])
    except Exception as exc:                      # noqa: BLE001 - 画面に出す
        log.exception("写す先を読めませんでした(下見)")
        report.lines = []
        report.error = f"共有の日報データを読めませんでした: {exc}"
    return report


def _is_lock(exc: Exception) -> bool:
    from dbkit.sqlite_toolkit import is_lock_error

    return is_lock_error(exc)


def run(source: object = "") -> Report:
    """**写す。** ロックを取ってから読み直して決め、控えを取り、1ラインずつ書いて突き合わせる。"""
    report = _report(False, source)
    src_path, dest, why = _paths(source)
    if src_path is None or dest is None:
        report.error = why
        return report
    report.source = str(src_path)
    try:
        sources, report.unknown, report.other_tables = _gather(read_source(src_path))
    except SourceError as exc:
        report.error = str(exc)
        return report
    from ..db.connection import SharedUnreadable, file_state

    # **あるか無いかは、置き場所に届いて確かめてから**(`file_state`)。届かない・確かめられない
    # のに「無い」とみなすと、控えを取らずに**新しい空の日報データを作って**写していた
    try:
        existed = file_state(dest)
    except SharedUnreadable as exc:
        report.error = f"{exc}(何も写していません)"
        return report
    if existed:
        why = _not_sqlite(dest)
        if why:
            report.error = why
            return report
    try:
        # 作るのは、確かめたうえで無いときだけ(初めて写す)
        conn = backend.connect(dest, create=not existed)
    except sqlite3.Error as exc:
        report.error = f"{dest.name} を開けません: {exc}"
        return report
    now = datetime.now()
    try:
        conn.isolation_level = None               # BEGIN は自分で出す
        try:
            # **読むところからロックを取る。** ほかの PC が書いている最中なら待つ(busy_timeout)
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if _is_lock(exc):
                report.error = (f"ほかの PC が共有の日報データに書いている最中でした"
                                f"({backend.BUSY_TIMEOUT_MS // 1000}秒待ちました)。"
                                "何も写していません。しばらくしてからもう一度押してください")
                return report
            raise
        try:
            tables = _tables(conn)
            planned = [(src, *_survey_line(conn, src, tables)) for src in sources]
            if existed and any(plan.writes for _src, _res, plan in planned):
                try:
                    report.backup = str(_backup(dest, now))
                except OSError as exc:
                    conn.execute("ROLLBACK")
                    report.lines = [res for _src, res, _plan in planned]
                    report.error = (f"写す前の控えを取れなかったので、写していません: {exc}")
                    return report
            for number, (src, result, plan) in enumerate(planned):
                if not result.error and plan.writes:
                    mark = f"line{number}"
                    conn.execute(f"SAVEPOINT {mark}")
                    try:
                        found = _write_line(conn, src, plan)
                        result.checked = found.describe()
                        if not found.ok:
                            raise RuntimeError(f"写した数が合いません({found.describe()})")
                        conn.execute(f"RELEASE {mark}")
                        result.written = plan.keys(rule.ADD) + plan.keys(rule.UPDATE)
                    except Exception as exc:      # noqa: BLE001 - このラインだけ取り消す
                        conn.execute(f"ROLLBACK TO {mark}")
                        conn.execute(f"RELEASE {mark}")
                        log.exception("写せませんでした line=%s", src.line)
                        result.error = f"{exc}(このラインは写していません)"
                report.lines.append(result)
                log.info("日報データ.accdb から写す %s: %s %s", src.line, result.counts,
                         result.error or result.checked)
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    except sqlite3.Error as exc:
        log.exception("日報データ.accdb から写せませんでした")
        report.lines = []
        report.error = (f"ほかの PC が共有の日報データに書いている最中でした。何も写していません"
                        "(しばらくしてからもう一度押してください)" if _is_lock(exc)
                        else f"共有の日報データに書けませんでした: {exc}(何も写していません)")
    finally:
        conn.close()
    if not report.error:
        _verify_after(dest, sources, report)
    return report


def _verify_after(dest: Path, sources: list[_Source], report: Report) -> None:
    """**確定したあと、ファイルを開き直して**写したページを読み直し、写す元と比べる。

    確定の前にも比べています(`_write_line`)。開き直すのは、確定したものが本当にファイルに
    あるかを、別の接続から見るためです(看板 3.5.2 と同じ)。合わなければ、そのラインに
    控えの場所を添えて知らせます(確定したあとなので、ここでは戻せません)。
    """
    by_line = {src.line: src for src in sources}
    try:
        conn = backend.connect(dest)
    except sqlite3.Error as exc:
        log.warning("写したあとに開き直せませんでした: %s", exc)
        return
    try:
        for result in report.lines:
            src = by_line.get(result.line)
            if src is None or result.error or not result.written:
                continue
            problems = compare(conn, src, result.written)
            if problems:
                result.error = ("写したあとに読み直したら Access と合いませんでした("
                                + "・".join(problems[:3]) + ")。控え: "
                                + (report.backup or "なし"))
    finally:
        conn.close()


def _not_sqlite(path: Path) -> str:
    """写す先が sqlite3 でなければ理由(名前は .sqlite3 でも中身が違う)。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except OSError as exc:
        return f"共有の日報データを開けません: {exc}"
    if head and not head.startswith(_SQLITE_MAGIC):
        return f"共有の日報データ({path.name})の中身が sqlite3 ではありません。写していません"
    return ""
