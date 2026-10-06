"""取り込み元の sqlite3 ファイルを読み書きする

【何のためにあるか】
共有フォルダに置かれた sqlite3 ファイル(このアプリの外にあるデータ)を
扱う層です。手元の作業用DB(``config.sqlite_path()``)とは別物なので、
開き方も扱いも分けます。

    取り込み元(外)  … このモジュール。読むのが主で、書き戻しだけ書く
    作業用DB(手元)  … ``db.py``。アプリが自由に読み書きする

**以前は Access(.accdb)でした。** 読むために Jet4 のバイナリを自前で
解析し、書くために ODBC ドライバ(ACE)か cscript+ADODB が要りました。
ドライバの有無で動いたり動かなかったりする分岐が丸ごと消えています ──
標準ライブラリだけで、どの端末でも同じように読み書きできます。

【読むときは読み取り専用で開く】
``file:...?mode=ro`` で開きます。共有フォルダのファイルを、取り込みの
つもりで**うっかり書き換えない**ためです。

【開けたかどうかは、引けたかどうかで決める】
``sqlite3.connect()`` はファイルを触りません。**開けたつもりのまま
返ってきて、最初の問い合わせで初めて落ちます。** それでは「別の開き方を
試す」逃げ道が一度も動かないので、開くところで1文引いて確かめます。

【開けるまで手を変える】
``URI(読み取り専用)`` → ``素のパス`` → ``手元への写し``。
共有フォルダ(SMB)の上の sqlite3 は、WAL で作られていると**読むだけでも
開けません**(WAL は共有メモリを使い、SMB にはそれが無い)。写せば読めるので、
``-wal`` / ``-shm`` ごと写して読みます。
**書き戻しは写しへ逃がしません** ── 書いたものがどこにも残らないためです。

【共有フォルダ(SMB)での書き込みについて】
Access(Jet/ACE)はレコード単位のロックを持ち、複数端末からの同時書き込み
のために設計されていました。SQLite のロックはファイル単位で、SMB のロック
転送に頼ります。壊れ方が「エラー」ではなく「破損」になりうるぶん、
こちらのほうが慎重にする必要があります。そのための緩和策:

* **WAL にしない**(``journal_mode=DELETE``)。WAL は SMB で開けない
* **書き込みは短いトランザクションに閉じる**。共有の上でロックを
  持ち続けない(``writing()`` を参照)
* **busy_timeout とリトライ**。他の端末が書いている最中でもすぐ諦めない
* **書く前に控えを取る**(``backup()``)。壊れたときに戻せるようにする
"""

from __future__ import annotations

import atexit
import os
import shutil
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional
from urllib.parse import quote

from .logging_utils import get_logger

log = get_logger("source_db")

#: 取り込み元の拡張子。**この2つだけを探す**
SUFFIXES = (".sqlite3", ".db")

#: 共有フォルダの上でロックを待つ時間(ミリ秒)。
#: 上流が書いている最中に当たっても、すぐ諦めずに少し待つ
BUSY_TIMEOUT_MS = 10_000

#: ロック競合で待ってやり直す回数と間隔。
#: 元の VBA(``ExecuteSQLWithRetry``)と同じ考え方
DEFAULT_MAX_RETRY = 3
RETRY_WAIT_SEC = 1.0

#: 開き方の名前(診断に出す)
WAY_URI = "URI"
WAY_PLAIN = "素のパス"
WAY_COPY = "手元への写し"

#: 一緒に写すファイル。本体だけ写すと、直前の書き込みが落ちる
_SIDECARS = ("-wal", "-shm", "-journal")


class SourceError(RuntimeError):
    """取り込み元を読み書きできなかった。"""


# ---------------------------------------------------------------------------
# エラーの種別
# ---------------------------------------------------------------------------
# **文言から推し量るしかない層。** sqlite3 は例外の型を細かく分けないので、
# ここ1か所にまとめて、呼び出し側が文字列を見ないようにする
def is_lock_error(exc: BaseException) -> bool:
    """他の誰かが書いている最中。時間をおけば通る見込みがある。"""
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def is_duplicate_error(exc: BaseException) -> bool:
    """一意制約に当たった。**再送で二重に入るのを防げた**ということ。"""
    text = str(exc).lower()
    return "unique" in text or "constraint failed" in text


def is_missing_column_error(exc: BaseException) -> bool:
    """列が無い。取り込み元の形がこちらの想定と違う。"""
    text = str(exc).lower()
    return "no such column" in text or "has no column" in text


def is_missing_table_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "no such table" in text


# ---------------------------------------------------------------------------
# SQL の組み立て
# ---------------------------------------------------------------------------
# 値は**必ずプレースホルダで渡す**。Access のときは SQL リテラルを自前で
# 組み立てるしかなかった(ODBC 経由で文字列を投げていた)が、sqlite3 では
# その必要が無い ── 引用符の重ねも日付リテラルも考えなくてよくなった
def quote_identifier(name: str) -> str:
    """テーブル名・列名を ``"`` で囲む(日本語列名・空白入り対策)。"""
    if '"' in name:
        raise SourceError(f'識別子に " は使えません: {name}')
    return f'"{name}"'


def build_insert(table: str, values: dict[str, Any]) -> tuple[str, list[Any]]:
    cols = ", ".join(quote_identifier(c) for c in values)
    marks = ", ".join("?" for _ in values)
    sql = f"INSERT INTO {quote_identifier(table)} ({cols}) VALUES ({marks})"
    return sql, list(values.values())


def build_update(table: str, values: dict[str, Any],
                 where: dict[str, Any]) -> tuple[str, list[Any]]:
    if not where:
        # **WHERE の無い UPDATE は投げない。** 空の WHERE は「全行」で、
        # 取り込み元は共有のデータなので1回の取り違えで全員が困る
        raise SourceError("WHERE の無い UPDATE は実行できません")
    sets = ", ".join(f"{quote_identifier(c)} = ?" for c in values)
    conds = " AND ".join(f"{quote_identifier(c)} = ?" for c in where)
    sql = (f"UPDATE {quote_identifier(table)} SET {sets} WHERE {conds}")
    return sql, list(values.values()) + list(where.values())


def build_delete(table: str, where: dict[str, Any]) -> tuple[str, list[Any]]:
    if not where:
        raise SourceError("WHERE の無い DELETE は実行できません")
    conds = " AND ".join(f"{quote_identifier(c)} = ?" for c in where)
    return f"DELETE FROM {quote_identifier(table)} WHERE {conds}", list(where.values())


def build_delete_one(table: str, where: dict[str, Any]) -> tuple[str, list[Any]]:
    """条件に合う行を**1行だけ**消す。

    【なぜ「1行だけ」が要るのか】
    条件が一意とはかぎらない場面があります。削除の転送は、取り込み元の
    ID がまだ分からないあいだ**自然キー**で対象を指しますが、
    自然キーは本来1行を指すはずでも、同じ内容の行が2つある可能性を
    否定できません(別のラインが同じ日に同じ文面の連絡を出した、など)。

    そこで消すのは**自分が消した1件ぶんだけ**にします。1件消したかった
    ところで2件消えるのは、**取り返しがつかないうえ、消した人には
    見えません。** 残ったほうは次の転送でまた当たるので、取りこぼしても
    「消し足りない」で済みます ── 消しすぎるより、ずっとよい。

    ``DELETE ... LIMIT`` は SQLite の作りしだいで使えない
    (``SQLITE_ENABLE_UPDATE_DELETE_LIMIT`` が要る)ので、
    ``rowid`` の絞り込みで同じことをします。**どの端末でも通ります。**
    """
    if not where:
        raise SourceError("WHERE の無い DELETE は実行できません")
    name = quote_identifier(table)
    conds = " AND ".join(f"{quote_identifier(c)} = ?" for c in where)
    sql = (f"DELETE FROM {name} WHERE rowid IN "
           f"(SELECT rowid FROM {name} WHERE {conds} LIMIT 1)")
    return sql, list(where.values())


# ---------------------------------------------------------------------------
# 接続
# ---------------------------------------------------------------------------
class SourceConnection:
    """取り込み元1つ分の接続。

    面(``query`` / ``execute`` / ``insert`` / ``update``)は小さく保つ。
    ``dbkit.outbox_sync`` はこの面しか使わないので、取り込み元の種類が
    変わっても書き戻しエンジンをそのまま使い回せる
    (実際、Access から sqlite3 へ移したときエンジンは無傷だった)。
    """

    def __init__(self, conn: sqlite3.Connection, path: Path, *,
                 read_only: bool, way: str) -> None:
        self._conn = conn
        self.path = path
        self.read_only = read_only
        self.way = way

    # -- 後始末 -------------------------------------------------------
    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error as exc:              # noqa: BLE001 - 閉じるのは best effort
            log.debug("取り込み元を閉じるときにエラー: %s", exc)

    def __enter__(self) -> "SourceConnection":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- 問い合わせ ---------------------------------------------------
    def query(self, sql: str, params: Iterable[Any] = (), *,
              max_retry: Optional[int] = None) -> list[dict[str, Any]]:
        """SELECT して、列名→値 の辞書のリストを返す。

        読み取りも書き込みと同じくロック競合で失敗しうる(他端末が書いて
        いる最中に読むと弾かれる)ので、同じくリトライする。
        """
        def run() -> list[dict[str, Any]]:
            cur = self._conn.execute(sql, tuple(params))
            try:
                names = [d[0] for d in (cur.description or [])]
                return [dict(zip(names, row)) for row in cur.fetchall()]
            finally:
                cur.close()

        return self._retry(run, sql, max_retry)

    def execute(self, sql: str, params: Iterable[Any] = (), *,
                max_retry: Optional[int] = None) -> int:
        """INSERT/UPDATE/DELETE を実行して、影響行数を返す。

        **1文ごとに commit する。** 共有フォルダの上でトランザクションを
        開いたままにすると、その間ほかの端末が書けない。
        """
        if self.read_only:
            raise SourceError("読み取り専用で開いた接続では更新できません")

        def run() -> int:
            with self._conn:                      # 例外なら自動で rollback
                cur = self._conn.execute(sql, tuple(params))
                try:
                    return cur.rowcount
                finally:
                    cur.close()

        return self._retry(run, sql, max_retry)

    def insert(self, table: str, values: dict[str, Any]) -> int:
        sql, params = build_insert(table, values)
        return self.execute(sql, params)

    def update(self, table: str, values: dict[str, Any],
               where: dict[str, Any]) -> int:
        sql, params = build_update(table, values, where)
        return self.execute(sql, params)

    def delete(self, table: str, where: dict[str, Any]) -> int:
        sql, params = build_delete(table, where)
        return self.execute(sql, params)

    # -- まとめ読み ---------------------------------------------------
    @contextmanager
    def reading(self) -> Iterator["SourceConnection"]:
        """**1つの時点**を見ながら、いくつもの表を読む。

        取り込みは休み管理・削除履歴・班員名簿を続けて読みます。1文ずつ
        読むと、その合間に他の端末が書けてしまい、**表どうしが少しずれた
        組み合わせ**を取り込むことがあります(片方には有るのに、
        もう片方から消えている、など)。

        ``BEGIN`` を1つ開いておくと、最初の ``SELECT`` の時点が最後まで
        見え続けます(SQLite は読み手に一貫した眺めを見せる)。

        **そのあいだ他の端末は書けません。** 読むのは表を数えるだけで
        すぐ終わるのと、取り込みは20秒に1回なので、釣り合うと判断して
        います。開けなければ諦めて素通しします ── 一貫性のために
        取り込みそのものを止めるのは行きすぎです。
        """
        try:
            self._conn.execute("BEGIN")
        except sqlite3.Error as exc:
            log.info("まとめ読みを始められませんでした(1文ずつ読みます): %s", exc)
            yield self
            return
        try:
            yield self
        finally:
            try:
                self._conn.rollback()             # 読むだけなので戻すだけ
            except sqlite3.Error as exc:
                log.debug("まとめ読みを閉じるときにエラー: %s", exc)

    # -- まとめ書き ---------------------------------------------------
    @contextmanager
    def writing(self) -> Iterator["SourceConnection"]:
        """**短いトランザクション**を1つ開く。

        取り込みのような「まとめて入れ替える」処理で使う。1文ずつ
        commit すると共有の上で往復が増えるが、逆に長く開いたままにすると
        その間ほかの端末が書けない。**まとめる単位はここで明示する。**
        """
        if self.read_only:
            raise SourceError("読み取り専用で開いた接続では更新できません")
        try:
            with self._conn:
                yield self
        except sqlite3.Error as exc:
            raise SourceError(str(exc)) from exc

    def executescript(self, sql: str) -> None:
        if self.read_only:
            raise SourceError("読み取り専用で開いた接続では更新できません")
        with self._conn:
            self._conn.executescript(sql)

    # -- 形 -----------------------------------------------------------
    def table_names(self) -> list[str]:
        rows = self.query(
            "SELECT name FROM sqlite_master "
            "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name")
        return [str(r["name"]) for r in rows]

    def has_table(self, table: str) -> bool:
        return bool(self.query(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
            (table,)))

    def has_index(self, name: str) -> bool:
        """その名前の索引が実在するか。

        「作ってあるつもり」を覚えで済ませないために要る ── 取り込み元の
        ファイルは差し替えられうる(控えから戻す、年度で入れ替える)。
        ``CREATE ... IF NOT EXISTS`` を毎回投げるより、見るほうが軽い。
        """
        return bool(self.query(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
            (name,)))

    def columns(self, table: str) -> list[str]:
        """その表に実在する列名。無い表なら空。"""
        try:
            rows = self.query(f"PRAGMA table_info({quote_identifier(table)})")
        except SourceError:
            return []
        return [str(r["name"]) for r in rows]

    # -- 内部 ---------------------------------------------------------
    def _retry(self, run, sql: str, max_retry: Optional[int]):
        limit = DEFAULT_MAX_RETRY if max_retry is None else max_retry
        attempt = 0
        while True:
            try:
                return run()
            except sqlite3.Error as exc:
                if is_lock_error(exc) and attempt < limit:
                    attempt += 1
                    log.info("取り込み元がロック中。%s 回目の再試行まで %.0f 秒待機",
                             attempt, RETRY_WAIT_SEC * attempt)
                    time.sleep(RETRY_WAIT_SEC * attempt)
                    continue
                log.warning("取り込み元への問い合わせに失敗: %s / %s", exc, sql)
                raise SourceError(str(exc)) from exc


# ---------------------------------------------------------------------------
# 開く
# ---------------------------------------------------------------------------
def connect(path: Path | str, *, read_only: bool = True) -> SourceConnection:
    """取り込み元を開く。**開けるまで手を変える。**"""
    conn, way = _open(Path(path), read_only=read_only)
    return SourceConnection(conn, Path(path), read_only=read_only, way=way)


def _open(path: Path, *, read_only: bool,
          attempts: Optional[list[tuple[str, str]]] = None,
          ) -> tuple[sqlite3.Connection, str]:
    r"""開けるまで手を変える。

    【なぜ手を変えるのか】
    共有フォルダ(SMB)の上の sqlite3 は、置かれ方しだいで開けない。

        WAL で作られている … WAL は共有メモリ(``-shm``)を使う。SMB には
                             それが無いので、**読むだけでも開けない**
        誰かが掴んでいる   … 変換ツールや他の端末が開いたまま
        URI が通らない     … UNC・ドライブ割り当て・長いパス
    """
    if not path.exists():
        raise SourceError(f"ファイルが見つかりません: {path}")
    # 絶対の道にするだけ。**共有には問い合わせない。**
    # ``resolve()`` は実体を開いて確かめに行くので、共有の上では往復が増える
    resolved = Path(os.path.abspath(path))
    log_to = attempts if attempts is not None else []

    ways = [(WAY_URI, lambda: _try_uri(resolved, read_only=read_only)),
            (WAY_PLAIN, lambda: _try_plain(resolved, read_only=read_only))]
    if read_only:
        # 写しは**読むときだけ**。書き戻しを写しへ向けたら、書いたものが
        # どこにも残らない ── 開けないなら開けないと言うほうがまし
        ways.append((WAY_COPY, lambda: _try_copy(resolved)))

    for name, attempt in ways:
        try:
            conn = attempt()
        except (sqlite3.Error, OSError) as exc:
            log_to.append((name, str(exc)))
            continue
        log_to.append((name, ""))
        if name != WAY_URI:
            log.warning("%s は %s で開きました", path.name, name)
        return conn, name

    reasons = " / ".join(f"{n}: {why}" for n, why in log_to if why)
    raise SourceError(f"{path.name} を開けませんでした: {reasons}")


def to_uri(path: Path) -> str:
    """``file:`` の URI にする。UNC(``\\\\サーバ\\共有``)にも対応する。"""
    text = str(path).replace("\\", "/")
    if text.startswith("//"):                     # UNC
        return "file:" + quote(text, safe="/:")
    if len(text) > 1 and text[1] == ":":          # C:/... のドライブ
        return "file:/" + quote(text, safe="/:")
    return "file:" + quote(text, safe="/:")


def _try_uri(resolved: Path, *, read_only: bool) -> sqlite3.Connection:
    """``file:...?mode=ro`` で開く。ふだんはこれで通る。"""
    uri = to_uri(resolved) + ("?mode=ro" if read_only else "")
    conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
    return _prepare(conn, read_only=read_only)


def _try_plain(resolved: Path, *, read_only: bool) -> sqlite3.Connection:
    """素のパスで開く。URI が通らない環境向け(読み取り専用にはできない)。"""
    conn = sqlite3.connect(str(resolved), timeout=BUSY_TIMEOUT_MS / 1000)
    return _prepare(conn, read_only=read_only)


def _try_copy(resolved: Path) -> sqlite3.Connection:
    """手元に写してから開く。**共有の上で開けないときの最後の手。**

    WAL のファイルは共有フォルダの上では開けない(共有メモリが要るため)。
    手元のディスクなら開けるので、写して読む。``-wal`` / ``-shm`` も
    一緒に写す ── 本体だけ写すと、直前の書き込みが落ちる。

    **読み取り専用で開く。** 写しを書き換えても誰にも届かない。
    """
    copy = _copy_of(resolved)
    conn = sqlite3.connect(to_uri(copy) + "?mode=ro", uri=True,
                           timeout=BUSY_TIMEOUT_MS / 1000)
    return _prepare(conn, read_only=True)


def _prepare(conn: sqlite3.Connection, *, read_only: bool) -> sqlite3.Connection:
    """**1文引いて確かめてから返す。**

    ``sqlite3.connect()`` はファイルを触らないので、開けたつもりのまま
    返ってきて最初の問い合わせで落ちる。ここで確かめておかないと、
    「別の開き方を試す」逃げ道が一度も動かない。
    """
    try:
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        # ここで初めてファイルに触る
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        if not read_only:
            # **WAL にしない。** WAL は共有メモリを使うので、SMB 上に
            # 置いた瞬間そのファイルは誰からも開けなくなる
            mode = str(conn.execute(
                "PRAGMA journal_mode = DELETE").fetchone()[0]).lower()
            if mode != "delete":
                log.warning("journal_mode を delete にできませんでした (いま %s)", mode)
    except (sqlite3.Error, OSError):
        conn.close()
        raise
    return conn


# 写しの置き場。**同じファイルを何度も写さない**
_COPY_DIR: Optional[Path] = None
_COPIES: dict[str, tuple[tuple[float, int], Path]] = {}


def _copy_root() -> Path:
    global _COPY_DIR
    if _COPY_DIR is None:
        _COPY_DIR = Path(tempfile.mkdtemp(prefix="source_db_copy_"))
        atexit.register(shutil.rmtree, _COPY_DIR, True)
    return _COPY_DIR


def _copy_of(resolved: Path) -> Path:
    """手元に写した実体。中身が変わっていなければ写し直さない。"""
    stat = resolved.stat()
    stamp = (stat.st_mtime, stat.st_size)
    key = str(resolved)
    cached = _COPIES.get(key)
    if cached is not None and cached[0] == stamp and cached[1].exists():
        return cached[1]

    copy = _copy_root() / f"{abs(hash(key)):x}{resolved.suffix}"
    for extra in _SIDECARS:
        side = Path(str(resolved) + extra)
        target = Path(str(copy) + extra)
        if side.exists():
            shutil.copyfile(side, target)
    shutil.copyfile(resolved, copy)
    _COPIES[key] = (stamp, copy)
    log.info("共有の上で開けないので手元へ写しました: %s → %s", resolved, copy)
    return copy


# ---------------------------------------------------------------------------
# 探す
# ---------------------------------------------------------------------------
def list_source_files(directory: Optional[Path]) -> list[Path]:
    """そのフォルダにある取り込み元の候補。名前順。"""
    if directory is None or not str(directory):
        return []
    try:
        entries = sorted(Path(directory).iterdir(), key=lambda p: p.name.lower())
    except (OSError, ValueError):
        return []
    return [p for p in entries if p.is_file() and p.suffix.lower() in SUFFIXES]


def find(directory: Optional[Path], name: str) -> Optional[Path]:
    """名前で探す。**拡張子違いも同じものとして扱う**(.sqlite3 / .db)。"""
    if directory is None or not str(directory):
        return None
    stem = Path(name).stem.casefold()
    for path in list_source_files(directory):
        if path.stem.casefold() == stem:
            return path
    return None


# ---------------------------------------------------------------------------
# 開いてみる (診断)
# ---------------------------------------------------------------------------
@dataclass
class Probe:
    """開いてみた結果。**駄目だった理由まで持つ。**"""

    path: Optional[Path] = None
    ok: bool = False
    size: int = 0
    is_sqlite: bool = False
    journal: str = ""
    opened_by: str = ""
    tables: list[str] = field(default_factory=list)
    sidecars: list[str] = field(default_factory=list)
    attempts: list[tuple[str, str]] = field(default_factory=list)
    error: str = ""

    def describe(self) -> str:
        if self.path is None:
            return "見つかりません"
        if self.ok:
            text = f"開けました ({len(self.tables)}表 / {self.size:,} バイト)"
            if self.opened_by and self.opened_by != WAY_URI:
                text += f" ※{self.opened_by}で開きました"
            if self.journal and self.journal.lower() == "wal":
                text += " ※WAL のため共有では開けないことがあります"
            return text
        return f"開けません: {self.error}"

    def hint(self) -> str:
        """次に何をすればよいか。**「読めません」だけでは直せない。**"""
        if self.ok or self.path is None:
            return ""
        if not self.is_sqlite:
            return ("sqlite3 のファイルではないようです。"
                    "変換ツール (tools/convert_accdb.py) で .accdb から"
                    "作り直してください。")
        if any("-wal" in s for s in self.sidecars):
            return ("WAL で作られています。共有フォルダでは開けないので、"
                    "手元で `PRAGMA journal_mode=DELETE;` を実行してから"
                    "置き直してください。")
        return "他の端末が開いたままになっていないか確認してください。"


#: sqlite3 ファイルの先頭に必ずある印
_MAGIC = b"SQLite format 3\x00"


def probe(path: Optional[Path]) -> Probe:
    """実際に開いてみる。**駄目な理由と、次にすることまで返す。**"""
    if path is None:
        return Probe()

    result = Probe(path=Path(path))
    try:
        result.size = result.path.stat().st_size
        with open(result.path, "rb") as fh:
            result.is_sqlite = fh.read(len(_MAGIC)) == _MAGIC
    except OSError as exc:
        result.error = str(exc)
        return result

    result.sidecars = [extra for extra in _SIDECARS
                       if Path(str(result.path) + extra).exists()]

    try:
        conn, way = _open(result.path, read_only=True, attempts=result.attempts)
    except SourceError as exc:
        result.error = str(exc)
        return result

    result.opened_by = way
    try:
        source = SourceConnection(conn, result.path, read_only=True, way=way)
        result.tables = source.table_names()
        row = source.query("PRAGMA journal_mode")
        result.journal = str(row[0]["journal_mode"]) if row else ""
        result.ok = True
    except SourceError as exc:
        result.error = str(exc)
    finally:
        conn.close()
    return result


# ---------------------------------------------------------------------------
# 控え
# ---------------------------------------------------------------------------
#: 同じファイルの控えを、続けて取り直さない間隔(秒)。
#: 1行直すたびに共有のファイルを丸ごと読むので、共有への行き来が
#: 台数ぶん増える。**守りたいのは「戻せる状態がある」こと**で、
#: 編集1回ごとの世代ではない
BACKUP_MIN_INTERVAL_SEC = 300.0

#: パスごとの、最後に控えを取った時刻と、その控え
_LAST_BACKUP: dict[str, tuple[float, Path]] = {}


def backup(path: Path, folder: Path, *, keep: int = 10,
           min_interval_sec: float = BACKUP_MIN_INTERVAL_SEC) -> Optional[Path]:
    """書く前に控えを取る。取れなくても止めない。

    共有フォルダの上の SQLite は、Access と違ってロックが OS 任せなので、
    最悪の壊れ方が「破損」になりうる。**戻せる状態を持っておく**ための保険。

    【``shutil.copyfile`` では取らない】
    ファイルを丸ごと写すと、**写している最中に他の端末が書いていれば、
    途中の状態が混ざった写し**になりえます。それは「控えがあるつもりで、
    戻せない」という一番たちの悪い形です。
    ``sqlite3`` のバックアップAPIは読み取りの錠を取って写すので、
    出来上がりが必ず一貫します。**同じ理由で、写している間ほかの端末は
    書けません**が、控えを取るのは設定を直すときだけなので釣り合います。

    開けないファイル(sqlite3 ではない・壊れている)は API では写せない
    ので、そのときだけファイルの丸写しに落とします ── 中身が読めない
    ものでも、**手元に置いておくこと自体に意味がある**ためです。

    【続けて取り直さない】
    ``min_interval_sec`` のあいだは、直前の控えを使い回します。
    1行直すたびに共有を丸ごと読むと、台数ぶん共有への行き来が増えます。
    """
    key = str(path)
    now = time.monotonic()
    last = _LAST_BACKUP.get(key)
    if last is not None and now - last[0] < min_interval_sec and last[1].exists():
        log.debug("控えは %.0f 秒前に取ってあるので省きます", now - last[0])
        return last[1]

    try:
        folder.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        target = folder / f"{path.stem}_{stamp}{path.suffix}"
        # 同じ秒に2つ取ると名前がぶつかる。**前の世代を黙って潰さない**
        serial = 2
        while target.exists():
            target = folder / f"{path.stem}_{stamp}-{serial}{path.suffix}"
            serial += 1
        if not _backup_consistent(path, target):
            shutil.copyfile(path, target)
    except OSError as exc:                        # noqa: BLE001 - 控えは best effort
        log.warning("控えを取れませんでした: %s", exc)
        return None
    _LAST_BACKUP[key] = (now, target)

    # 古い控えを片付ける(共有でもローカルでも、際限なく増やさない)
    try:
        olds = sorted(folder.glob(f"{path.stem}_*{path.suffix}"))
        for extra in olds[:-keep]:
            extra.unlink()
    except OSError:
        pass
    log.info("控えを取りました: %s", target)
    return target


def _backup_consistent(path: Path, target: Path) -> bool:
    """``sqlite3`` のバックアップAPIで写す。写せたら ``True``。

    こちらは**読み取りの錠を取ってから**写すので、書き込みの途中が
    混ざりません。開けないファイルでは使えないので、その判断のために
    真偽を返します(呼ぶ側が丸写しへ落とす)。
    """
    source: Optional[sqlite3.Connection] = None
    copy: Optional[sqlite3.Connection] = None
    try:
        source = sqlite3.connect(to_uri(Path(os.path.abspath(path))) + "?mode=ro",
                                 uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
        source.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        copy = sqlite3.connect(str(target))
        source.backup(copy)
        return True
    except (sqlite3.Error, OSError) as exc:
        log.info("控えを sqlite3 の写し取りで作れませんでした(丸写しにします): %s",
                 exc)
        # 書きかけが残っていると、丸写しがその上に書けないことがある
        try:
            if target.exists():
                target.unlink()
        except OSError:
            pass
        return False
    finally:
        for conn in (copy, source):
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass


def reset_backup_memory() -> None:
    """テスト用。「さっき取った」を忘れる。"""
    _LAST_BACKUP.clear()
