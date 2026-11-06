"""日報入力データの控え ── LocalBackup (v4.12.0)

    日報入力データがローカル保存とのことですが指定パスにも保存できるように
    (ローカルと両方)ローカルになければパスを見る / フォルダ名は LocalBackup
    権限 が Administrator のユーザーは LocalBackup すべてのラインデータを
    記録を見る で参照でき編集もできる(この機能がないと管理者が現地に行く必要がある)

【置き場所】

    <控えの置き場所>\\LocalBackup\\<ライン名>\\日報入力データ.sqlite3

控えの置き場所は設定・参照設定の「日報入力データの控えの置き場所」。空なら
**共有の日報データと同じフォルダ**です(どのPCからも届く場所でないと、管理者が
別のPCから直せないので)。中の形は手元のDBと同じで、入れるのは日報の
見出しと明細(`daily_header` / `daily_detail`)だけです。

【いつ写すか】
手元へ保存したとき(自動保存・打ちかけも)・共有へ送れたとき・紙を消したとき。
リポジトリが**保存と同じ取引で**写す待ち(`backup_pending`)に入れ、要求の
**応答を返したあと**に写します(`app/__init__._register_db`)── 共有のフォルダが
遅い・届かないときに、打っている人を待たせないため。届かなければ待ちに残り、
次の保存か起動で写ります(しばらく届かなければ、`RETRY_AFTER_SEC` は試さない)。

【どちらが新しいか】
同じページが手元と控えの両方にあるときは、**保存した時刻(`saved_at`)が新しい
ほう**を正とします。管理者が別のPCで直したぶんは控えのほうが新しいので:

    写すとき(手元 → 控え) … 控えのほうが新しければ上書きせず、手元へ戻す
    起動のとき(控え → 手元)… 手元に無い・控えのほうが新しいページを戻す

**控えの置き場所(親のフォルダ)が無ければ作りません。** 届かない共有を
「無い」と取り違えて、手元のどこかに作ってしまわないように。`LocalBackup` と
ライン名のフォルダは、親があれば作ります。

**ここで何が起きても、手元への保存は成功のまま**です(写す待ちに残るだけ)。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import nullcontext as _no_lock
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

from ..config import SETTINGS
from ..db.repository import NippouRepository
from ..db.schema import ensure_schema
from ..logging_setup import get_logger
from ..logic import line_names

log = get_logger("services.local_backup")

PageKey = tuple[str, str, str, int]
ShiftKey = tuple[str, str, str]

#: 写す表(見出しが先 ── 明細は見出しを外部キーで指す)
HEADER = "daily_header"
DETAIL = "daily_detail"
_WHERE = "report_date=? AND line=? AND shift=? AND page=?"

#: 届かなかったあと、次に試すまでの秒数(自動保存のたびに待たされないように)
RETRY_AFTER_SEC = 60.0

_flush_lock = threading.Lock()
_retry_at = 0.0

#: **この端末の画面でいま打っているページ**と、最後に書いた時刻(v4.24.0)。
#:
#: 写すときに「控えのほうが新しい」ページを手元へ戻しますが、それが**画面に
#: 開いて打っている最中のページ**だと、画面の知らないうちに中身が入れ替わり、
#: 次の自動保存が戻した中身を(または戻した中身を画面が)黙って上書きします。
#: 打っているあいだはそのページを戻さず、写す待ちに残します(離れたあとの
#: 写しで、新しいほうに揃います)。
#:
#: 「新しい」は**各PCの時計**で決めています(`saved_at`)。別のPCの時計が進んで
#: いると、古い中身が新しく見えます ── ここで守れるのは画面に開いている
#: ページだけで、ほかのページは時計がずれていれば取り違えます(ログに残します)。
EDITING_HOLD_SEC = 600.0
_editing: Optional[tuple[PageKey, float]] = None


def note_editing(key: PageKey) -> None:
    """画面がこのページを打っている(保存した)。`flush` はしばらく戻さない。"""
    global _editing
    _editing = (tuple(key), time.monotonic())


def editing() -> Optional[PageKey]:
    """いま打っているページ(最後に書いてから `EDITING_HOLD_SEC` のあいだ)。"""
    if _editing is None:
        return None
    key, at = _editing
    return key if time.monotonic() - at < EDITING_HOLD_SEC else None


def _safe(line: str) -> str:
    from ..access_bridge.pusher import safe_line_name

    return safe_line_name(line)


def root() -> Path:
    return SETTINGS.local_backup_root


def path_for(line: str) -> Path:
    """そのラインの控えのファイル。"""
    return root() / _safe(line) / SETTINGS.local_backup_filename


def reachable() -> bool:
    """控えの置き場所(親のフォルダ)に届くか。"""
    try:
        return SETTINGS.local_backup_base.is_dir()
    except OSError:
        return False


def backing_off() -> bool:
    """少し前に控えの置き場所へ届かなかった(しばらくは読みにも行かない)。"""
    return time.monotonic() < _retry_at


def lines_available() -> list[str]:
    """控えのあるライン(ラインの並びで)。届かなければ空。"""
    try:
        if not root().is_dir():
            return []
        found = {p.parent.name for p in root().glob(f"*/{SETTINGS.local_backup_filename}")}
    except OSError:
        return []
    # 並びは定義の表の順(L-1・LVC・HVC・機側…)
    known = [ln for ln in line_names.codes() if _safe(ln) in found]
    return known + sorted(found - {_safe(ln) for ln in known})


# ----------------------------------------------------------------------
# 開く
# ----------------------------------------------------------------------
def _open_write(path: Path) -> sqlite3.Connection:
    """書くために開く。**WAL にしない**(共有のフォルダでは働かないことがある)。

    **控えのファイルを新しく作るのは、置き場所に届いて本当に無いと確かめたときだけ**
    (`db.connection.open_rw`。`LocalBackup` とライン名のフォルダは控えの置き場所の下なら
    作る)。一瞬見えなかった・権限・0バイトのときは作らずに `SharedUnreadable`(写す待ちに
    残し、次の回にやり直す)。以前は開くだけで、見えなかった控えの場所に空の控えを作っていた。
    """
    from ..db.connection import open_rw

    conn = open_rw(path, create=True, base=SETTINGS.local_backup_base, timeout=10)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA foreign_keys=ON")
        ensure_schema(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def _exists(path: Path) -> bool:
    """そのラインの控えが**ある**か・**確かめたうえで無い**か。確かめられなければ `SharedUnreadable`。"""
    from ..db.connection import file_state

    return file_state(path, base=SETTINGS.local_backup_base)


@contextmanager
def reader(line: str) -> Iterator[Optional[NippouRepository]]:
    """そのラインの控えを**読むだけ**で開く。**確かめたうえで無ければ** None。

    届かない・権限・見えているのに開けないときは `SharedUnreadable`(sqlite3 のエラー)。
    「無い」と同じにすると、起動のときに手元の全ページを「控えに無い」として写す待ちに
    入れ、記録を見るでは「控えがありません」と出ていた。
    """
    path = path_for(line)
    if not _exists(path):
        yield None
        return
    # 共有(UNC)の上でも開ける形で(`db.connection.readonly_uri`)。以前は
    # `as_posix()` で `file://サーバ/…` になり「invalid uri authority」で断られていた
    from ..db.connection import connect_readonly

    conn = connect_readonly(path, timeout=10)
    try:
        yield NippouRepository(conn)
    finally:
        conn.close()


# ----------------------------------------------------------------------
# 1ページを写す
# ----------------------------------------------------------------------
def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def _stamp(conn: sqlite3.Connection, key: PageKey) -> Optional[tuple[str, str]]:
    """(保存した時刻, 指紋)。そのページが無ければ None。"""
    row = conn.execute(f"SELECT saved_at, content_hash FROM {HEADER} WHERE {_WHERE}",
                       key).fetchone()
    return None if row is None else (str(row[0] or ""), str(row[1] or ""))


def _newer(theirs: Optional[tuple[str, str]], mine: Optional[tuple[str, str]]) -> bool:
    """`theirs` が `mine` より新しい別の中身か(同じ時刻なら新しくない)。"""
    if theirs is None:
        return False
    if mine is None:
        return True
    return theirs[0] > mine[0] and theirs[1] != mine[1]


def _sent_same(sent: Optional[tuple[str, str]], mine: Optional[tuple[str, str]]) -> bool:
    """控えで共有へ済のページ(`sent` = 送った時刻と指紋)が、手元のいまの中身と同じか。"""
    return bool(sent and mine and mine[1] and sent[1] == mine[1])


def _base_of(conn: sqlite3.Connection, key: PageKey) -> str:
    """手元のそのページが**最後に控えと揃ったときの中身の指紋**(分からなければ空)。"""
    row = conn.execute(f"SELECT base_hash FROM {HEADER} WHERE {_WHERE}", key).fetchone()
    return str(row[0] or "") if row else ""


def _settle_base(conn: sqlite3.Connection, key: PageKey) -> None:
    """手元のそのページは**いま控えと同じ中身**、と覚える(`daily_header.base_hash`)。

    控えから戻した・控えへ写したあとに呼びます(取引は呼ぶ側)。ほかの端末の知らせ
    (`services/other_terminal`)は、控えの中身がこの指紋とも手元のいまの中身とも違う
    ときだけ出します ── 自分が受け取った・写した中身では知らせません。
    """
    conn.execute(f"UPDATE {HEADER} SET base_hash=content_hash WHERE {_WHERE}", key)


def _owner(conn: sqlite3.Connection, key: PageKey) -> Optional[tuple[str, str, str]]:
    """控えのそのページを最後に保存した端末: (PC名, 保存した時刻, 指紋)。分からなければ None。"""
    if "saved_pc" not in _columns(conn, HEADER):
        return None                               # 前の版が作った控え(誰のものか分からない)
    row = conn.execute(f"SELECT saved_pc, saved_at, content_hash FROM {HEADER} WHERE {_WHERE}",
                       key).fetchone()
    if row is None or not row[0]:
        return None
    return str(row[0]), str(row[1] or ""), str(row[2] or "")


def _base(conn: sqlite3.Connection, key: PageKey) -> str:
    row = conn.execute(f"SELECT base_hash FROM {HEADER} WHERE {_WHERE}", key).fetchone()
    return str(row[0] or "") if row is not None else ""


def _copy_page(src: sqlite3.Connection, dst: sqlite3.Connection, key: PageKey) -> bool:
    """`src` のそのページで `dst` を置き換える(取引は呼ぶ側)。`src` に無ければ消す。

    列は**両方にある列だけ**を写します(版の違うDBどうしでも写せるように)。
    """
    dst.execute(f"DELETE FROM {DETAIL} WHERE {_WHERE}", key)
    dst.execute(f"DELETE FROM {HEADER} WHERE {_WHERE}", key)
    for table in (HEADER, DETAIL):
        theirs = set(_columns(dst, table))
        cols = [c for c in _columns(src, table) if c in theirs]
        names = ", ".join(f'"{c}"' for c in cols)
        rows = src.execute(f"SELECT {names} FROM {table} WHERE {_WHERE}", key).fetchall()
        if table == HEADER and not rows:
            return False                          # 手元に無い = 消した
        dst.executemany(
            f"INSERT INTO {table} ({names}) VALUES ({', '.join('?' for _ in cols)})",
            [tuple(r) for r in rows])
    return True


@dataclass
class Result:
    """写した・戻した結果。"""

    written: int = 0                # 控えへ写したページ
    removed: int = 0                # 控えから消したページ
    pulled: int = 0                 # 控えから手元へ戻したページ
    pending: int = 0                # まだ写せていないページ
    #: ほかのPCが同じ中身を共有へ送っていたので、送った印だけ揃えたページ(起動のとき)
    adopted: int = 0
    error: str = ""
    shifts: list[ShiftKey] = field(default_factory=list)   # 戻した直(集計を作り直す)

    @property
    def message(self) -> str:
        if self.error:
            return f"控え(LocalBackup): {self.error}"
        parts = []
        if self.written:
            parts.append(f"{self.written}ページを写しました")
        if self.removed:
            parts.append(f"{self.removed}ページを消しました")
        if self.pulled:
            parts.append(f"{self.pulled}ページを手元へ戻しました")
        if self.adopted:
            parts.append(f"ほかのPCが共有へ送っていた{self.adopted}ページを送り済みにしました")
        return "控え(LocalBackup): " + ("・".join(parts) if parts else "変わりありません")

    def as_dict(self) -> dict[str, Any]:
        return {"written": self.written, "removed": self.removed, "pulled": self.pulled,
                "pending": self.pending, "error": self.error, "message": self.message}


def _refresh(repo: NippouRepository, shifts: set[ShiftKey]) -> None:
    """戻した直の集計を作り直す(明細が正。集計はそこから何度でも作れる)。"""
    from . import summary

    for report_date, line, shift in sorted(shifts):
        try:
            summary.refresh_shift(repo, report_date, line, shift)
        except Exception:                         # noqa: BLE001 - 明細は戻っている
            log.exception("戻した直の集計を作り直せませんでした %s/%s/%s",
                          report_date, line, shift)


def _settle_pulled(repo: NippouRepository, shifts: set[ShiftKey],
                   csv: Optional[set[ShiftKey]] = None) -> None:
    """控えから戻した・消した直の集計を作り直し、**この端末のラインの日は集計CSVも書き直す**(v4.46.3)。

    管理者のPCでこのラインの日報を直すと、直しは控え経由でこの端末に戻ってきます。前は集計だけ
    作り直して集計CSVはそのままだったので、出力先のCSVは直す前の中身のままでした。手元に
    その日の直が全部あるのはこの端末なので、ここで直ぜんぶで書き直します(`day_csv.settle`。
    ほかのラインの日は書かない)。負荷係数は直した側で数え直してあるので、ここでは触りません。
    """
    if not shifts:
        return
    from . import day_csv, second_output

    csv = set(shifts) if csv is None else set(csv) & set(shifts)
    _refresh(repo, set(shifts) - csv)                      # 集計だけ
    if not csv:
        return
    for note in day_csv.settle(repo, csv, factors=False):
        log.info("控えから戻した直: %s", note)
    # 2つ目の出力先も(共有へ送り済みの中身なら)。直した側が先に共有へ送っていると、この端末は
    # その直を送り直さないので、共有へ保存の道では2つ目が書き直されない
    try:
        with day_csv._lock if day_csv._lock is not None else _no_lock():
            out = second_output.after_pull(repo, {(d, ln) for d, ln, _sh in csv})
            if out.copies or out.held:
                log.info("控えから戻した直(2つ目): %s", out.text("集計CSV"))
    except Exception:                                      # noqa: BLE001 - 1つ目は書けている
        log.exception("控えから戻した直の2つ目の出力先へ書けませんでした")


def _base_missing() -> str:
    return (f"控えの置き場所 {SETTINGS.local_backup_base} に届きません"
            "(届くようになったら写します)")


#: ほかで写している最中のときの一言(`flush`)
BUSY = "ほかで写しています"
#: 要求のあとに写すとき(`flush_soon`)、ほかで写し終えるのを待つ上限(秒)
FLUSH_SOON_WAIT_SEC = 30.0


def flush(repo: NippouRepository, *, force: bool = False, wait: float = 0.0) -> Result:
    """写す待ちのページを控えへ写す。**同時には1つだけ走る。**

    `wait` 秒まで、ほかで写し終えるのを待つ(0 なら待たずに `BUSY` で戻る)。
    """
    global _retry_at
    got = _flush_lock.acquire(timeout=wait) if wait > 0 else _flush_lock.acquire(blocking=False)
    if not got:
        return Result(pending=len(repo.backup_pending()), error=BUSY)
    try:
        # 入れた時刻も読んでおく ── 写している最中に入れ直されたページ(共有へ保存して
        # 送り済みになった など)の待ちを、写し終えたときに消さないため
        stamped = repo.backup_pending_stamped()
        stamps = dict(stamped)
        pending = [key for key, _ in stamped]
        result = Result(pending=len(pending))
        if not pending:
            return result
        if not force and time.monotonic() < _retry_at:
            result.error = _base_missing()
            return result
        if not reachable():
            _retry_at = time.monotonic() + RETRY_AFTER_SEC
            result.error = _base_missing()
            return result
        by_line: dict[str, list[PageKey]] = {}
        for key in pending:
            by_line.setdefault(key[1], []).append(key)
        pulled: set[ShiftKey] = set()
        open_now = editing()
        for line, keys in by_line.items():
            try:
                dst = _open_write(path_for(line))
            except (OSError, sqlite3.Error) as exc:
                log.warning("控えを開けませんでした %s: %s", path_for(line), exc)
                result.error = f"{path_for(line)} を開けません: {exc}"
                continue
            try:
                done: list[PageKey] = []
                written: list[PageKey] = []
                with dst:                         # 1ラインぶんを1つの取引で
                    for key in keys:
                        mine, theirs = _stamp(repo.conn, key), _stamp(dst, key)
                        # **控えが、手元が最後に揃ったときのまま**(`base_hash`)なら、変わったのは
                        # 手元だけ ── 時計に関係なく手元を写す(v4.46.3。管理者のPCの時計が遅れて
                        # いると、直した中身を「控えのほうが新しい」と取り違え、古い中身で手元の
                        # 直しを上書きしていた。共有へは直したものを送ったあとでも)
                        if (mine is not None and theirs is not None
                                and _base_of(repo.conn, key) == theirs[1] != mine[1]):
                            theirs = (min(theirs[0], mine[0]), theirs[1])
                        # 手元に無い = このPCで消した(待ちに入れたのは消したとき)。
                        # **新しさを比べるのは両方にあるときだけ**
                        if mine is not None and _newer(theirs, mine) and key == open_now:
                            # **画面で打っている最中のページは戻さない**(待ちに残す)
                            log.warning("控えのほうが新しいページですが、この画面で打って"
                                        "いる最中なので戻しません(離れたあとに揃えます。"
                                        "別のPCの時計がずれていると、古い中身が新しく見える"
                                        "ことがあります) key=%s 控え=%s 手元=%s",
                                        key, theirs, mine)
                            continue
                        if mine is not None and _newer(theirs, mine):
                            # 控えのほうが新しい(別のPCで直された)── 上書きせず、手元へ戻す
                            with repo.conn:
                                _copy_page(dst, repo.conn, key)
                                _settle_base(repo.conn, key)
                            result.pulled += 1
                            pulled.add(key[:3])
                        else:
                            if mine is not None:
                                _note_replacing(dst, repo.conn, key, mine)
                            if _copy_page(repo.conn, dst, key):
                                result.written += 1
                                written.append(key)
                            elif theirs is not None:
                                result.removed += 1
                                # 消した記録も控えへ(同じラインのほかの PC が、消したときと
                                # 同じ中身のページを控えへ戻さないように ── `_pull`)
                                _copy_removals(repo.conn, dst, key)
                        done.append(key)
                # 控えへ写せた(取引が通った)ページは、控えと同じ中身になった
                with repo.conn:
                    for key in written:
                        _settle_base(repo.conn, key)
                repo.clear_backup_pending(done, stamps)
            except (OSError, sqlite3.Error) as exc:
                log.warning("控えへ写せませんでした %s: %s", line, exc)
                result.error = f"{line_names.label(line)} の控えへ写せませんでした: {exc}"
            finally:
                dst.close()
        _settle_pulled(repo, pulled)
        result.shifts = sorted(pulled)
        result.pending = len(repo.backup_pending())
        if not result.error:
            _retry_at = 0.0
        return result
    finally:
        _flush_lock.release()


def _copy_removals(src: sqlite3.Connection, dst: sqlite3.Connection, key: PageKey) -> None:
    """そのページを消した記録(`page_removal`)を写す(取引は呼ぶ側)。写せなくても止めない。"""
    try:
        cols = [c for c in _columns(src, "page_removal") if c in set(_columns(dst, "page_removal"))]
        if not cols:
            return
        names = ", ".join(f'"{c}"' for c in cols)
        rows = src.execute(f"SELECT {names} FROM page_removal WHERE {_WHERE}", key).fetchall()
        dst.executemany(
            f"INSERT OR REPLACE INTO page_removal ({names}) VALUES ({', '.join('?' for _ in cols)})",
            [tuple(r) for r in rows])
    except sqlite3.Error:
        log.exception("消した記録を控えへ写せませんでした key=%s", key)


def _removed_there(backup: Optional[NippouRepository], line: str) -> dict[PageKey, set[str]]:
    """控えにある「消した記録」: ページ → 消したときの中身の指紋。前の版の控え(表が無い)は空。"""
    if backup is None:
        return {}
    try:
        rows = backup.conn.execute(
            "SELECT report_date, line, shift, page, content_hash FROM page_removal WHERE line=?",
            (line,)).fetchall()
    except sqlite3.Error:
        return {}
    out: dict[PageKey, set[str]] = {}
    for r in rows:
        out.setdefault((r[0], r[1], r[2], int(r[3])), set()).add(str(r[4] or ""))
    return out


def _note_replacing(dst: sqlite3.Connection, local: sqlite3.Connection, key: PageKey,
                    mine: tuple[str, str]) -> None:
    """控えのそのページが**ほかの端末が保存した、この端末の知らない中身**なら、
    この端末の中身で置き換えることを覚える(画面の知らせ `services/other_terminal`)。

    止めはしません(知らせだけ)。知らせを作れなくても写すのはそのまま。
    """
    try:
        from . import other_terminal

        owner = _owner(dst, key)
        if owner is None:
            return
        pc, saved_at, theirs_hash = owner
        if other_terminal.unknown_to_me(pc, theirs_hash, mine[1], _base(local, key)):
            log.warning("ほかの端末(%s・%s に保存)の控えのページを、この端末の中身で"
                        "置き換えます key=%s", pc, saved_at, key)
            other_terminal.note_replaced(key, pc, saved_at)
    except Exception:                             # noqa: BLE001 - 写すのは止めない
        log.exception("ほかの端末のページかを確かめられませんでした key=%s", key)


def flush_soon() -> None:
    """要求の応答を返したあとに写す(`call_on_close` から)。**自分の接続で。**"""
    from ..db.connection import connect

    try:
        conn = connect(SETTINGS.sqlite_path)
    except Exception:                             # noqa: BLE001 - 次の機会に写す
        log.exception("控えへ写すために手元のDBを開けませんでした")
        return
    try:
        # **ほかで写している最中なら、終わるのを待ってから写す。** 写している最中に入れ直した待ち
        # (共有へ保存して送り済みになった など)は消さずに残すが、そのとき待たずに戻ると、
        # 残った待ちを写す人がいなくなり、次に何か保存するまで控えが「未送信」のままだった
        result = flush(NippouRepository(conn), wait=FLUSH_SOON_WAIT_SEC)
        if result.written or result.pulled or result.removed:
            log.info("%s", result.message)
    except Exception:                             # noqa: BLE001 - 次の機会に写す
        log.exception("控えへ写す途中で予期しないエラー")
    finally:
        conn.close()


def reset() -> None:
    """届かなかった記憶を忘れる(テスト用)。"""
    global _retry_at, _editing, _pulled_at
    _pulled_at = 0.0
    _retry_at = 0.0
    _editing = None


# ----------------------------------------------------------------------
# 起動のとき: 控え ⇄ 手元(この端末のライン)
# ----------------------------------------------------------------------
#: 動いているあいだに控えを見に行く間隔(秒)。管理者のPCが直したこのラインの日報を取り込む
PULL_EVERY_SEC = 300.0
_pulled_at = 0.0


def pull_if_due(repo: NippouRepository, line: str) -> Optional[Result]:
    """**動いているあいだも**、ときどき控えを見て、ほかのPC(管理者)が直したページを取り込む(v4.46.3)。

    前は起動のときだけ見ていたので、ラインの端末が起動したままだと、管理者が直した前日分は
    次に起動するまでこの端末に入らず、その日の集計CSVも直す前のまま残っていました。
    取り込んだら集計と集計CSVを書き直します(`_settle_pulled`)。画面で打っているページは戻しません。
    """
    global _pulled_at
    now = time.monotonic()
    if _pulled_at and now - _pulled_at < PULL_EVERY_SEC:
        return None
    _pulled_at = now
    result = pull(repo, line)
    if result.pulled or result.removed:
        log.info("控えから取り込みました(動いているあいだ): %s", result.message)
    return result


def pull(repo: NippouRepository, line: str) -> Result:
    """そのラインの控えと手元を**新しいほうに揃える**準備をする。

        控えにだけある / 控えのほうが新しい  → 手元へ戻す(ここで)
        手元にだけある / 手元のほうが新しい  → 写す待ちに入れる(`flush` が写す)

    2つ目は、この仕組みが入る前に打ったぶんも控えに入れるためです。
    """
    result = Result()
    if not reachable():
        result.error = _base_missing()
        return result
    try:
        return _pull(repo, line, result)
    except sqlite3.Error as exc:
        # **控えを読めなかった。** 「控えに無い」とみなして全ページを写す待ちに入れない
        # (戻すものも分からない)。次の起動・保存のときにやり直す
        log.warning("控えを読めませんでした(手元はそのまま。次の回にやり直します) %s: %s",
                    path_for(line), exc)
        result.error = f"{path_for(line)} を読めませんでした: {exc}"
        result.pending = len(repo.backup_pending())
        return result


def _pull(repo: NippouRepository, line: str, result: Result) -> Result:
    pulled: set[ShiftKey] = set()
    replaced: set[ShiftKey] = set()
    to_send: list[PageKey] = []
    with reader(line) as backup:
        theirs: dict[PageKey, tuple[str, str]] = {}
        #: 控えで「共有へ済」のページ → (送った時刻, 送ったときの指紋)
        sent_there: dict[PageKey, tuple[str, str]] = {}
        if backup is not None:
            for r in backup.conn.execute(
                    f"SELECT report_date, line, shift, page, saved_at, content_hash,"
                    f" dirty, synced_at, synced_hash FROM {HEADER} WHERE line=?", (line,)):
                key = (r[0], r[1], r[2], int(r[3]))
                theirs[key] = (str(r[4] or ""), str(r[5] or ""))
                if not r[6] and r[8]:
                    sent_there[key] = (str(r[7] or ""), str(r[8]))
        mine = {(r[0], r[1], r[2], int(r[3])): (str(r[4] or ""), str(r[5] or ""))
                for r in repo.conn.execute(
                    f"SELECT report_date, line, shift, page, saved_at, content_hash"
                    f" FROM {HEADER} WHERE line=?", (line,))}
        # 手元が**最後に控えと揃ったときの中身**(`base_hash`)。手元がそこから変わっていないのに
        # 控えが変わっていれば、時計に関係なく控えのほうが新しい(v4.46.3。管理者のPCの時計が
        # 遅れていると、直した時刻が手元より古く見え、取り込まずに古い中身で控えを上書きしていた)
        base = {(r[0], r[1], r[2], int(r[3])): str(r[4] or "")
                for r in repo.conn.execute(
                    f"SELECT report_date, line, shift, page, base_hash FROM {HEADER} WHERE line=?",
                    (line,))}

        def moved_there(key: PageKey) -> bool:
            """手元はそのまま・控えだけ変わった(どちらの時計にも頼らない)。"""
            known = base.get(key, "")
            return bool(known and key in mine and key in theirs
                        and mine[key][1] == known and theirs[key][1] != known)

        unsent_here = {(r[0], r[1], r[2], int(r[3])) for r in repo.conn.execute(
            f"SELECT report_date, line, shift, page FROM {HEADER} WHERE line=? AND dirty=1",
            (line,))}
        # **この端末で消して、まだ控えから消せていないページ**(写す待ちに残っていて、手元に無い)。
        # 控えから戻すと、消したページが起動のたびに生き返る(「直を消す」・中身の無いページ)
        waiting = set(repo.backup_pending())
        removed_there = _removed_there(backup, line)
        typing = editing()
        for key, stamp in theirs.items():
            if key not in mine and key in waiting:
                continue
            if key == typing:
                continue                          # 画面で打っているページは戻さない(`flush` と同じ)
            if _newer(stamp, mine.get(key)) or moved_there(key):
                if key in mine:
                    replaced.add(key[:3])         # 手元にあったページを、控えの新しいほうで置き換えた
                with repo.conn:
                    _copy_page(backup.conn, repo.conn, key)
                    _settle_base(repo.conn, key)
                result.pulled += 1
                pulled.add(key[:3])
            elif key in unsent_here and _sent_same(sent_there.get(key), mine.get(key)):
                # **同じ中身を、ほかのPCが共有へ送ってくれていた**(この端末が止まって
                # いるあいだに、次の直の端末が控えから受け取って送った)。送った印だけ
                # 揃えます ── 揃えないと、送り済みの直がこの端末では「共有へ出ていません」
                # の赤い帯になり、次の直の入力まで止めます
                with repo.conn:
                    repo.conn.execute(
                        f"UPDATE {HEADER} SET dirty=0, synced_at=?, synced_hash=?"
                        f" WHERE {_WHERE} AND dirty=1 AND content_hash=?",
                        (*sent_there[key], *key, sent_there[key][1]))
                result.adopted += 1
        gone: list[PageKey] = []
        for key, stamp in mine.items():
            if key not in theirs and stamp[1] and stamp[1] in removed_there.get(key, set()):
                # **ほかの PC で消したページ**(控えに消した記録があり、中身も消したときと同じ)。
                # 控えへ戻さず、この端末からも消す(戻すと、消した PC の起動でまた生き返る)
                gone.append(key)
                continue
            if key not in theirs or (_newer(stamp, theirs[key]) and not moved_there(key)):
                to_send.append(key)
        for key in gone:
            _drop_removed(repo, key, backup)
            result.removed += 1
    repo.queue_backup(to_send)
    # 集計CSVは**置き換えた・消した直の日だけ**書き直す(手元が空の端末の初めての起動で、
    # 何年ぶんもの日を共有のフォルダへ書き直さない。新しく入っただけの直は前に出したCSVのまま)
    _settle_pulled(repo, pulled | {k[:3] for k in gone},
                   csv=replaced | {k[:3] for k in gone})
    result.shifts = sorted(pulled)
    result.pending = len(repo.backup_pending())
    return result


def _drop_removed(repo: NippouRepository, key: PageKey, backup: Optional[NippouRepository]) -> None:
    """ほかの PC で消したページを、この端末からも消す(消した記録も写して残す)。"""
    with repo.conn:
        repo.conn.execute(f"DELETE FROM {DETAIL} WHERE {_WHERE}", key)
        repo.conn.execute(f"DELETE FROM {HEADER} WHERE {_WHERE}", key)
        if backup is not None:
            _copy_removals(backup.conn, repo.conn, key)
    log.warning("ほかの PC で消したページなので、この端末からも消しました key=%s", key)


def _refresh_after_drop(repo: NippouRepository, shifts: set[ShiftKey]) -> None:
    """消したページの直の集計を作り直す。ページが残っていなければ集計も消す。"""
    from . import summary

    for report_date, line, shift in sorted(shifts):
        try:
            if repo.saved_pages(report_date, line, shift):
                summary.refresh_shift(repo, report_date, line, shift)
            else:
                repo.delete_packing_report(report_date, line, shift)
        except Exception:                         # noqa: BLE001 - 消すのは済んでいる
            log.exception("消した直の集計を作り直せませんでした %s/%s/%s",
                          report_date, line, shift)


# ----------------------------------------------------------------------
# 記録を見る: 控えの直を並べる / 手元へ入れる
# ----------------------------------------------------------------------
def list_shifts(line: str, limit: int = 60) -> list[dict[str, Any]]:
    """そのラインの控えにある直(新しい保存が先)。**直の単位で**、ページは添え物。"""
    from ..logic.shift import parse_business_date

    with reader(line) as backup:
        if backup is None:
            return []
        test = ", test_mark" if "test_mark" in _columns(backup.conn, HEADER) else ", 0 AS test_mark"
        rows = backup.conn.execute(
            f"SELECT report_date, line, shift, page, saved_at, dirty, worker{test}"
            f" FROM {HEADER} WHERE line=?", (line,)).fetchall()
    found: dict[ShiftKey, dict[str, Any]] = {}
    for r in rows:
        key = (r["report_date"], r["line"], r["shift"])
        got = found.setdefault(key, {"report_date": key[0], "line": key[1], "shift": key[2],
                                     "pages": [], "saved_at_raw": "", "synced": True,
                                     "workers": [], "test": False})
        got["test"] = got["test"] or bool(r["test_mark"])
        got["pages"].append(int(r["page"]))
        got["saved_at_raw"] = max(got["saved_at_raw"], str(r["saved_at"] or ""))
        got["synced"] = got["synced"] and not r["dirty"]
        for name in str(r["worker"] or "").split():
            if name not in got["workers"]:
                got["workers"].append(name)
    shifts = list(found.values())
    for s in shifts:
        s["pages"].sort()
        raw = s["saved_at_raw"]
        s["saved_at"] = f"{raw[5:10]} {raw[11:16]}" if len(raw) >= 16 else raw
        day = parse_business_date(s["report_date"])
        s["day"] = day.isoformat() if day else ""
    shifts.sort(key=lambda s: (s["day"], s["saved_at_raw"]), reverse=True)
    return shifts[:limit]


def import_shift(repo: NippouRepository, report_date: str, line: str, shift: str) -> Result:
    """控えのその直(全ページ)を手元へ入れる。**手元のほうが新しいページは触らない。**"""
    result = Result()
    with reader(line) as backup:
        if backup is None:
            result.error = f"{line} の控えがありません({path_for(line)})"
            return result
        pages = backup.saved_pages(report_date, line, shift)
        for page in pages:
            key = (report_date, line, shift, int(page))
            if _newer(_stamp(backup.conn, key), _stamp(repo.conn, key)):
                with repo.conn:
                    _copy_page(backup.conn, repo.conn, key)
                    _settle_base(repo.conn, key)
                result.pulled += 1
    if result.pulled:
        _refresh(repo, {(report_date, line, shift)})
        result.shifts = [(report_date, line, shift)]
    return result


def pending_count(repo: NippouRepository) -> int:
    try:
        return len(repo.backup_pending())
    except sqlite3.Error:
        return 0


def status(repo: Optional[NippouRepository] = None) -> dict[str, Any]:
    """設定の画面に出す1かたまり。"""
    return {"base": str(SETTINGS.local_backup_base), "root": str(root()),
            "reachable": reachable(),
            "pending": pending_count(repo) if repo is not None else 0}
