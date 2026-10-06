"""看板履歴のファイル(``看板履歴.sqlite3``)

看板を出した・発送した・届いた、の出来事(:mod:`kanban.domain.events`)を置く
**全端末で共有するファイル**。看板集計(:mod:`kanban.presenters.stats`)はここを読む。

【なぜ看板マスタと分けるのか】
以前は看板マスタ(共有DB)の中の ``看板履歴`` 表に書いていた。記録は増え続ける
一方で、看板マスタは Access から変換し直したり、中身を入れ替えたりする相手。
記録を別のファイルにしておけば、看板マスタの扱いと関係なく残り、置き場所も
別に決められる(既定は看板マスタと同じフォルダ)。

【前の置き場所の記録】
看板マスタの ``看板履歴`` に残っている行は、起動するたびに**まだ無い行だけ**
このファイルへ写す(:func:`copy_from_shared`。ID で見分けるので 2 回入らない)。
入れ替えの途中で古い版の端末が看板マスタへ書いた分も、次に誰かが起動したときに
こちらへ来る。**看板マスタの表は消さない**(戻したくなったときの控え)。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .. import applog, config
from .shared import SharedDb, SharedDbError, _is_usable_sqlite, kanban_tables_in

TABLE = "看板履歴"


class HistoryDb(SharedDb):
    """看板履歴.sqlite3。読み方・書き方は共有DBと同じ(読むときは手元へ写す)。"""

    role = "history"

    def describe(self) -> str:
        return f"看板履歴: {self.path}"


def open_db(cfg: config.Config) -> HistoryDb:
    return HistoryDb(cfg.resolved_history_db_path(), busy_timeout_ms=cfg.busy_timeout_ms,
                     max_retry=cfg.max_retry)


def path_problem(path: str) -> str:
    """その場所を看板履歴の置き場所にしてよいか。よければ空文字。

    **まだ無いファイルは通す**(フォルダがあれば、そこに作る)。あるファイルなら、
    看板履歴の表だけ(か空)のものに限る ── 看板マスタや梱包資材マスタを指すと、
    そこへ記録を書き込んでしまう。
    """
    text = str(path or "").strip()
    if not text:
        return "場所が空です。"
    target = Path(text)
    try:
        if not target.exists():
            if not target.parent.is_dir():
                return f"フォルダが見つかりません: {target.parent}"
            if target.suffix.lower() not in (".sqlite3", ".sqlite", ".db"):
                return f"sqlite3 のファイル名にしてください(例: {config.HISTORY_DB_NAME}): {target}"
            return ""
        if not target.is_file():
            return f"ファイルではありません: {target}"
    except OSError as exc:
        return f"その場所を確かめられません: {target}({exc})"
    if not _is_usable_sqlite(target):
        return f"sqlite3 として開けません: {target}"
    try:
        kanban, others = kanban_tables_in(target)
    except Exception as exc:  # noqa: BLE001
        return f"中の表を確かめられません: {target}({exc})"
    if kanban:
        return (f"看板マスタ(看板_LVC などがあるファイル)です: {target}\n"
                f"看板履歴は別のファイル({config.HISTORY_DB_NAME})に置きます。")
    extra = [n for n in others if n != TABLE]
    if extra:
        shown = "、".join(extra[:5]) + (" …" if len(extra) > 5 else "")
        return (f"看板履歴のほかに表が入っているファイルです(梱包資材マスタなど、別のツールの"
                f"ファイルではありませんか): {target}(入っている表: {shown})")
    return ""


def create_if_missing(db: HistoryDb) -> bool:
    """ファイルが無ければ作る(**フォルダがあるときだけ**)。作ったら True。"""
    if db.exists():
        return False
    target = Path(db.path)
    if not target.parent.is_dir():
        raise SharedDbError(f"看板履歴のフォルダが見つかりません: {target.parent}")
    conn = sqlite3.connect(str(target))
    try:
        # 空のファイルは sqlite3 として開けない(ファイル見出しが無い)ので、ここで表まで作る
        from .sync import HISTORY_COLUMNS

        columns = ", ".join(f"[{c}] TEXT NOT NULL DEFAULT ''" for c, _ in HISTORY_COLUMNS)
        conn.execute(f"CREATE TABLE IF NOT EXISTS [{TABLE}] ({columns})")
        conn.execute(f"CREATE INDEX IF NOT EXISTS [idx_看板履歴_ID] ON [{TABLE}] ([ID])")
        conn.commit()
    finally:
        conn.close()
    applog.info("看板履歴のファイルを作りました: %s", target)
    return True


def copy_from_shared(shared: SharedDb, history: HistoryDb) -> int:
    """看板マスタの ``看板履歴`` にあって、看板履歴.sqlite3 に**まだ無い行だけ**写す。写した件数。

    看板マスタは手元の写しから読む(共有ファイルを直接開かない)。看板マスタの表は消さない。
    置き場所を変えたときの「前の看板履歴.sqlite3 → 新しい場所」にも使う(``shared`` に前のファイル)。
    """
    from .sync import HISTORY_COLUMNS

    try:
        have = shared.column_names(TABLE)
    except SharedDbError:
        return 0
    if not have or "ID" not in have:
        return 0
    source = shared.local_copy()
    columns = [c for c, _ in HISTORY_COLUMNS if c in have]
    names = ", ".join(f"[{c}]" for c in columns)
    picks = ", ".join(f"COALESCE(s.[{c}], '')" for c in columns)
    conn = sqlite3.connect(history.path, timeout=history.busy_timeout_ms / 1000)
    try:
        conn.execute("ATTACH DATABASE ? AS src", (str(source),))
        cursor = conn.execute(
            f"INSERT INTO [{TABLE}] ({names}) SELECT {picks} FROM src.[{TABLE}] s"
            f" WHERE COALESCE(s.[ID], '') <> ''"
            f" AND NOT EXISTS (SELECT 1 FROM [{TABLE}] h WHERE h.[ID] = s.[ID])")
        copied = max(0, cursor.rowcount)
        conn.commit()
        conn.execute("DETACH DATABASE src")
    except sqlite3.Error as exc:
        raise SharedDbError(f"看板履歴を写せませんでした({shared.path} → {history.path}): {exc}") from exc
    finally:
        conn.close()
    if copied:
        history.forget_copy()
        applog.info("看板履歴を %d 件写しました: %s → %s", copied, shared.path, history.path)
    return copied


def row_count(db: HistoryDb) -> int | None:
    """行数(読めなければ None)。設定画面の「状態」に出す。"""
    try:
        rows = db.select(f"SELECT COUNT(*) AS n FROM [{TABLE}]")
    except SharedDbError:
        return None
    return int(rows[0]["n"]) if rows else 0
