"""直を消す ── **この PC の手元で要らないページを探して消す。共有からも消すかは選ぶ**

    そのPCの手元で不要なものを探す → 消す → 選択制「共有の日報データからも消しますか？」
    yes で共有からも消す

【探すもの(一覧に出す。どれも選べる)】

    テスト      テストモードで作った直のページ(`nippou/test_mode`)。まとめて選べる
    中身なし    1行も打っていないページ(「中身の無いページを片付ける」と同じ物差し
                `logic/empty_pages`)
    指定して    日付・ライン・直で絞った、手元に保存したページ(テストモードを作る前に
                試しに打った分など)

探すのは手元の作業用DBからです(画面の値ではない)。

【消す前にもう一度確かめる】
一覧に出したときの「保存した時刻と中身の指紋」(`stamp`)を画面から送ってもらい、消す直前に
DBの今と比べます。違えば**消しません**(一覧を出してから押すまでのあいだに誰かが打った ──
`services/empty_pages` と同じ思想)。

【消す前に控えを残す】
消すページの見出しと明細を、この PC の `%LOCALAPPDATA%\\NippouTool\\消した日報\\` に
1回ごと1つの sqlite3 で残します(手元の作業用DBと同じ形。取り消したいときはそこから戻せる)。
**残せなければ消しません。**

【手元から消す】
見出しと明細を1つの取引で(`page_writer.delete_page` ── 開いたままの古い画面が自動保存で
作り直さない印も付く)。控え(LocalBackup)からも、写すときに消えます。消した直の集計は作り
直し、ページが残らない直は集計も消します。

【共有から消す(「はい」のとき)】
その直のキー(報告日・ライン・直・ページ)の行だけを、**1回の処理(1つの取引)で**消します
(見出しと明細。ページが手元に1枚も残らない直は、共有の集計の3つの表のその直の行も)。

    共有を読めない・届かない   **消しません**(1.5.79〜1.5.81 の決まり)。理由を出し、
                               「共有はまだ」(`page_removal.shared = 'pending'`)と残して、
                               同じ面からあとで消せるようにします
    手元にまた保存された       そのページは共有から消しません(`'skipped'`)

【記録】
誰が(PC名・ログインID)いつ何を消したかを `page_removal`(消しません)と、出来事の記録
(`services/event_log`)に残します。
"""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from ..db.repository import NippouRepository
from ..logging_setup import get_logger
from ..logic import empty_pages as empty_rule
from ..logic import line_names
from ..logic.shift import parse_business_date
from . import page_writer

log = get_logger("services.shift_removal")

KIND_ALL = "all"
KIND_TEST = "test"
KIND_EMPTY = "empty"
KINDS = {KIND_ALL: "手元のページ(指定して選ぶ)", KIND_TEST: "テストデータ", KIND_EMPTY: "中身の無いページ"}
#: 「指定して」のときに並べる上限(新しい日から)。絞れば全部出る
LIST_LIMIT = 300

#: 消す前の控えを置くフォルダ(`app_config.local_root()` の下)
KEPT_DIR = "消した日報"

SHARED_UNASKED = ""
SHARED_LOCAL = "local"
SHARED_PENDING = "pending"
SHARED_DONE = "done"
SHARED_SKIPPED = "skipped"
SHARED_LABELS = {SHARED_UNASKED: "共有はまだ(答えていません)", SHARED_LOCAL: "手元だけ",
                 SHARED_PENDING: "共有はまだ(届かなかった)", SHARED_DONE: "共有からも消しました",
                 SHARED_SKIPPED: "共有からは消していません"}
#: 共有から**まだ消せていない**(あとで消せる)状態
OPEN_STATES = (SHARED_UNASKED, SHARED_PENDING)

PageKey = tuple[str, str, str, int]


def key_text(key: PageKey) -> str:
    return f"{key[0]} {line_names.label(key[1])} {key[2]} 第{key[3]}ページ"


def _stamp(saved_at: object, content_hash: object) -> str:
    return f"{saved_at or ''}|{content_hash or ''}"


# ======================================================================
# 探す
# ======================================================================
@dataclass
class Page:
    report_date: str
    line: str
    shift: str
    page: int
    worker: str = ""
    saved_at: str = ""
    rows: int = 0
    test: bool = False
    empty: bool = False
    sent: bool = False          # この端末から共有へ渡したことがある(送ったあとに直したものも)
    unsent: bool = False        # いま共有へ未送信
    stamp: str = ""

    @property
    def key(self) -> PageKey:
        return (self.report_date, self.line, self.shift, int(self.page))

    def as_dict(self) -> dict[str, Any]:
        day = parse_business_date(self.report_date)
        return {"report_date": self.report_date, "line": self.line,
                "line_label": line_names.label(self.line), "shift": self.shift,
                "page": int(self.page), "worker": self.worker,
                "saved_at": self.saved_at[:16].replace("T", " "), "rows": self.rows,
                "test": self.test, "empty": self.empty, "sent": self.sent,
                "unsent": self.unsent, "stamp": self.stamp, "key_text": key_text(self.key),
                "day": day.isoformat() if day else ""}


def _rows_of(details) -> int:
    return sum(1 for d in details if not empty_rule.row_is_empty(d))


def _sent(row: sqlite3.Row) -> bool:
    return bool(row["synced_at"] or row["synced_hash"])


def find(repo: NippouRepository, *, kind: str = KIND_ALL, day: str = "", line: str = "",
         shift: str = "") -> dict[str, Any]:
    """手元のページの一覧(新しい日から)。`kind` で テスト / 中身なし に絞る。

    `day` は `YYYY-MM-DD`、`line`・`shift` は手元の値そのもの。空なら絞らない。
    """
    kind = kind if kind in KINDS else KIND_ALL
    sql = ("SELECT report_date, line, shift, page, worker, saved_at, dirty, synced_at,"
           " synced_hash, content_hash, test_mark FROM daily_header")
    where, args = [], []
    if line:
        where.append("line=?")
        args.append(line)
    if shift:
        where.append("shift=?")
        args.append(shift)
    if kind == KIND_TEST:
        where.append("test_mark<>0")
    if where:
        sql += " WHERE " + " AND ".join(where)
    heads = repo.conn.execute(sql, args).fetchall()
    if day:
        heads = [h for h in heads if _day_of(h["report_date"]) == day]
    heads.sort(key=lambda h: (parse_business_date(h["report_date"]) or datetime.min.date(),
                              h["shift"], int(h["page"])), reverse=True)
    pages: list[Page] = []
    truncated = False
    for h in heads:
        if kind == KIND_ALL and len(pages) >= LIST_LIMIT:
            truncated = True
            break
        loaded = repo.load(h["report_date"], h["line"], h["shift"], int(h["page"]))
        if loaded is None:
            continue
        details = loaded[1]
        empty = empty_rule.is_empty(details)
        if kind == KIND_EMPTY and not empty:
            continue
        pages.append(Page(
            report_date=h["report_date"], line=h["line"], shift=h["shift"], page=int(h["page"]),
            worker=h["worker"] or "", saved_at=str(h["saved_at"] or ""), rows=_rows_of(details),
            test=bool(h["test_mark"]), empty=empty, sent=_sent(h), unsent=bool(h["dirty"]),
            stamp=_stamp(h["saved_at"], h["content_hash"])))
    counts = _counts(repo)
    if pages:
        head = f"{KINDS[kind]}: {len(pages)}ページ" + ("(多いので新しい日から先だけ)" if truncated else "")
    else:
        head = f"{KINDS[kind]}は見つかりませんでした"
    return {"kind": kind, "pages": [p.as_dict() for p in pages], "truncated": truncated,
            "limit": LIST_LIMIT, "counts": counts, "headline": head,
            "open": open_removals(repo), "choices": _choices(repo)}


def _day_of(report_date: str) -> str:
    parsed = parse_business_date(report_date)
    return parsed.isoformat() if parsed else ""


def _choices(repo: NippouRepository) -> dict[str, list[dict[str, str]]]:
    """絞る欄に出すライン・直(**手元にあるものだけ**)。"""
    from ..logic.aggregation import shift_sort_key

    lines = [r[0] for r in repo.conn.execute("SELECT DISTINCT line FROM daily_header") if r[0]]
    order = {code: i for i, code in enumerate(line_names.codes())}
    lines.sort(key=lambda ln: (order.get(ln, len(order)), ln))
    shifts = [r[0] for r in repo.conn.execute("SELECT DISTINCT shift FROM daily_header") if r[0]]
    shifts.sort(key=shift_sort_key)
    return {"lines": [{"code": ln, "label": line_names.label(ln)} for ln in lines],
            "shifts": [{"code": sh, "label": sh} for sh in shifts]}


def _counts(repo: NippouRepository) -> dict[str, int]:
    """テストのページ・中身の無いページの数(見出しに添える)。"""
    test = repo.conn.execute("SELECT COUNT(*) FROM daily_header WHERE test_mark<>0").fetchone()[0]
    total = repo.conn.execute("SELECT COUNT(*) FROM daily_header").fetchone()[0]
    return {"test": int(test or 0), "all": int(total or 0)}


def open_removals(repo: NippouRepository) -> list[dict[str, Any]]:
    """共有から**まだ消せていない**記録(答えていない・届かなかった)。"""
    out = []
    for r in repo.removals(shared=OPEN_STATES, limit=200):
        key = (r["report_date"], r["line"], r["shift"], int(r["page"]))
        out.append({"id": r["id"], "key_text": key_text(key), "removed_at":
                    str(r["removed_at"])[:16].replace("T", " "), "shared": r["shared"],
                    "state": SHARED_LABELS.get(r["shared"], r["shared"]),
                    "detail": r["shared_detail"], "test": bool(r["test_mark"])})
    return out


# ======================================================================
# 手元から消す
# ======================================================================
@dataclass
class Removed:
    removed: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    ids: list[str] = field(default_factory=list)
    kept_file: str = ""
    error: str = ""

    @property
    def message(self) -> str:
        if self.error:
            return self.error
        parts = []
        if self.removed:
            parts.append(f"手元から {len(self.removed)}ページを消しました(控え: {self.kept_file})")
        if self.skipped:
            parts.append(f"{len(self.skipped)}ページは消していません: "
                         + " / ".join(f"{s['key_text']} … {s['why']}" for s in self.skipped[:5])
                         + (f" ほか{len(self.skipped) - 5}ページ" if len(self.skipped) > 5 else ""))
        return "。".join(parts) or "消すページがありませんでした"

    def as_dict(self) -> dict[str, Any]:
        return {"removed": self.removed, "skipped": self.skipped, "ids": self.ids,
                "kept_file": self.kept_file, "error": self.error, "message": self.message,
                # 消したあとに「共有の日報データからも消しますか？」を聞くか
                "ask_shared": bool(self.ids)}


def _parse_item(item: Any) -> Optional[tuple[PageKey, str, str]]:
    """画面から来た1件 → (キー, stamp, どの一覧から)。読めなければ None。"""
    if not isinstance(item, dict):
        return None
    try:
        page = int(item.get("page", 0))
    except (TypeError, ValueError):
        return None
    key = (str(item.get("report_date", "")).strip(), str(item.get("line", "")).strip(),
           str(item.get("shift", "")).strip(), page)
    if not (key[0] and key[1] and key[2] and page > 0):
        return None
    reason = str(item.get("reason") or KIND_ALL)
    return key, str(item.get("stamp") or ""), reason if reason in KINDS else KIND_ALL


def kept_dir() -> Path:
    from .. import app_config

    return app_config.local_root() / KEPT_DIR


def _keep(repo: NippouRepository, keys: list[PageKey], now: datetime, pc: str) -> Path:
    """消す前の控え(見出しと明細)。**書けなければ例外**(消さない)。"""
    from ..db.schema import ensure_schema
    from . import local_backup

    folder = kept_dir()
    folder.mkdir(parents=True, exist_ok=True)
    safe_pc = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (pc or "PC"))
    path = folder / f"消す前_{now.strftime('%Y%m%d-%H%M%S')}_{safe_pc}.sqlite3"
    n = 1
    while path.exists():
        n += 1
        path = folder / f"消す前_{now.strftime('%Y%m%d-%H%M%S')}_{safe_pc}_{n}.sqlite3"
    conn = sqlite3.connect(str(path))
    try:
        conn.row_factory = sqlite3.Row
        ensure_schema(conn)
        with conn:
            for key in keys:
                local_backup._copy_page(repo.conn, conn, key)
        got = conn.execute("SELECT COUNT(*) FROM daily_header").fetchone()[0]
    finally:
        conn.close()
    if got != len(keys):
        raise OSError(f"控えに写せたのは {got}/{len(keys)}ページでした: {path}")
    return path


def remove(repo: NippouRepository, items: Iterable[Any], *,
           now: Optional[datetime] = None) -> Removed:
    """選んだページを手元から消す。**消す直前に、一覧に出したときと同じかを確かめる。**"""
    from .. import test_mode

    now = now or datetime.now()
    pc, login = test_mode.who()
    out = Removed()
    ok: list[tuple[PageKey, str, sqlite3.Row, int]] = []
    seen: set[PageKey] = set()
    for item in items:
        parsed = _parse_item(item)
        if parsed is None:
            continue
        key, stamp, reason = parsed
        if key in seen:
            continue
        seen.add(key)
        row = repo.conn.execute(
            "SELECT * FROM daily_header WHERE report_date=? AND line=? AND shift=? AND page=?",
            key).fetchone()
        if row is None:
            out.skipped.append({"key_text": key_text(key), "why": "もう消えています"})
            continue
        if stamp != _stamp(row["saved_at"], row["content_hash"]):
            # **一覧を出したあとに保存された**(誰かが打った)。消さない
            out.skipped.append({"key_text": key_text(key),
                                "why": "一覧を出したあとに保存されたので消していません"
                                       "(探し直してから選んでください)"})
            continue
        loaded = repo.load(*key)
        rows = _rows_of(loaded[1]) if loaded else 0
        ok.append((key, reason, row, rows))
    if not ok:
        return out

    try:
        kept = _keep(repo, [k for k, *_ in ok], now, pc)
    except Exception as exc:                      # noqa: BLE001 - 控えが無ければ消さない
        log.exception("消す前の控えを残せませんでした(消していません)")
        out.error = f"消す前の控えを残せなかったので、何も消していません: {exc}"
        return out
    out.kept_file = str(kept)

    shifts: set[tuple[str, str, str]] = set()
    for key, reason, row, rows in ok:
        if not page_writer.delete_page(repo, *key):
            out.skipped.append({"key_text": key_text(key), "why": "もう消えています"})
            continue
        record = {
            "id": uuid.uuid4().hex, "report_date": key[0], "line": key[1], "shift": key[2],
            "page": key[3], "removed_at": now.isoformat(timespec="seconds"),
            "terminal": pc, "login": login, "reason": reason, "rows": rows,
            "content_hash": row["content_hash"] or "", "test_mark": int(row["test_mark"] or 0),
            "was_sent": int(_sent(row)), "kept_file": str(kept), "shared": SHARED_UNASKED,
            "shared_at": "", "shared_detail": ""}
        repo.add_removal(record)
        out.ids.append(record["id"])
        out.removed.append({"id": record["id"], "key_text": key_text(key), "rows": rows,
                            "test": bool(row["test_mark"]), "sent": bool(record["was_sent"])})
        shifts.add(key[:3])
        log.warning("手元からページを消しました key=%s rows=%d test=%s pc=%s login=%s 控え=%s",
                    key, rows, bool(row["test_mark"]), pc, login, kept)
    _settle_shifts(repo, shifts)
    if out.removed:
        _note(f"手元から {len(out.removed)}ページを消しました: "
              + " / ".join(r["key_text"] for r in out.removed[:10])
              + (f" ほか{len(out.removed) - 10}ページ" if len(out.removed) > 10 else ""),
              action="pages_removed", pc=pc, login=login, input=f"控え: {kept}",
              pages=[r["key_text"] for r in out.removed])
    return out


def _settle_shifts(repo: NippouRepository, shifts: Iterable[tuple[str, str, str]]) -> None:
    """消した直の集計を作り直す。ページが残っていなければ集計も消す。"""
    from . import summary

    for report_date, line, shift in sorted(shifts):
        try:
            if repo.saved_pages(report_date, line, shift):
                summary.refresh_shift(repo, report_date, line, shift)
            else:
                repo.delete_packing_report(report_date, line, shift)
        except Exception:                         # noqa: BLE001 - 消せてはいる
            log.exception("消したあとの集計の作り直しでエラー %s/%s/%s", report_date, line, shift)


def _note(message: str, *, pc: str = "", login: str = "", pages: Iterable[str] = (),
          **fields: Any) -> None:
    """出来事の記録(誰が・いつ・何を)。消したページは「鍵」の欄にも並べる。"""
    from .. import test_mode

    fields.setdefault("key", " / ".join(pages))
    test_mode.note(message, pc=pc, login=login, **fields)


# ======================================================================
# 共有から消す / 手元だけにする
# ======================================================================
@dataclass
class SharedOutcome:
    done: list[str] = field(default_factory=list)       # 共有から消したページ(字)
    skipped: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)    # 届かなかった(あとで消せる)
    error: str = ""
    rows: int = 0

    @property
    def message(self) -> str:
        parts = []
        if self.done:
            parts.append(f"共有の日報データからも {len(self.done)}ページを消しました")
        if self.pending:
            parts.append(f"共有の日報データからは消せませんでした({self.error})。"
                         f"{len(self.pending)}ページは「共有はまだ」として残しています ── "
                         "共有に届くようになったら、この面の「共有からまだ消していないページ」から消せます")
        if self.skipped:
            parts.append(f"{len(self.skipped)}ページは共有から消していません(手元にまた保存されています):"
                         + " / ".join(self.skipped[:5]))
        return "。".join(parts) or "共有から消すページはありませんでした"

    def as_dict(self) -> dict[str, Any]:
        return {"done": self.done, "skipped": self.skipped, "pending": self.pending,
                "error": self.error, "rows": self.rows, "message": self.message}


def keep_local(repo: NippouRepository, ids: Iterable[str]) -> int:
    """「いいえ」── 手元だけ消した、と残す。"""
    from .. import test_mode

    records = repo.removals(ids=list(ids), shared=OPEN_STATES)
    repo.set_removal_shared([r["id"] for r in records], SHARED_LOCAL, "共有からは消さない(いいえ)")
    if records:
        pc, login = test_mode.who()
        _note(f"消したページを共有からは消さないことにしました({len(records)}ページ)",
              action="pages_removed_local_only", pc=pc, login=login,
              pages=[key_text(_key(r)) for r in records])
    return len(records)


def _key(r: dict) -> PageKey:
    return (r["report_date"], r["line"], r["shift"], int(r["page"]))


def remove_shared(repo: NippouRepository, ids: Iterable[str], *,
                  db_path: Optional[Path] = None, runner=None) -> SharedOutcome:
    """「はい」── 消したページを共有の日報データからも消す。**届かなければ消さない。**"""
    from .. import test_mode
    from ..access_bridge import pusher
    from ..config import SETTINGS

    out = SharedOutcome()
    pc, login = test_mode.who()
    records = repo.removals(ids=list(ids), shared=OPEN_STATES)
    targets = []
    for r in records:
        key = _key(r)
        if repo.load(*key) is not None:
            # 手元にまた保存された(消したあとに打った)。共有の行はそのページの今の中身の
            # はずなので消さない
            repo.set_removal_shared([r["id"]], SHARED_SKIPPED,
                                    "手元にまた保存されたので、共有からは消していません")
            out.skipped.append(key_text(key))
            continue
        targets.append((r, key))
    if not targets:
        return out

    path = Path(db_path or SETTINGS.access_db_path)
    keys = [k for _, k in targets]
    # ページが手元に1枚も残らない直は、共有の集計の行も消す
    gone_shifts = sorted({k[:3] for k in keys if not repo.saved_pages(*k[:3])})
    if pusher.is_sqlite_target(path):
        from ..access_bridge import sqlite_backend

        result = sqlite_backend.delete_pages(
            path,
            [(k, pusher.header_table_name(k[1]), pusher.detail_table_name(k[1])) for k in keys],
            [(s, pusher.summary_table_name(s[1]), pusher.agg_detail_table_name(s[1]),
              pusher.stop_detail_table_name(s[1])) for s in gone_shifts])
        ok, error = result.ok, result.error
        out.rows = result.header_rows + result.detail_rows + result.summary_rows
        detail = (f"見出し{result.header_rows}行・明細{result.detail_rows}行・"
                  f"集計{result.summary_rows}行を消しました") if ok else error
    else:
        ok, error = _remove_from_access(path, keys, runner)
        detail = "共有(Access)から消しました" if ok else error

    ids_done = [r["id"] for r, _ in targets]
    if not ok:
        repo.set_removal_shared(ids_done, SHARED_PENDING, error)
        out.pending = [key_text(k) for k in keys]
        out.error = error
        log.warning("共有から消せませんでした(手元は消したまま。共有はまだ) keys=%s: %s", keys, error)
        _note(f"消したページを共有の日報データから消せませんでした({len(keys)}ページ): {error}",
              kind="warn", action="pages_removed_shared_failed", pc=pc, login=login,
              pages=[key_text(k) for k in keys])
        return out
    repo.set_removal_shared(ids_done, SHARED_DONE, detail)
    out.done = [key_text(k) for k in keys]
    log.warning("共有からもページを消しました keys=%s pc=%s login=%s %s", keys, pc, login, detail)
    _note(f"共有の日報データからも {len(keys)}ページを消しました: "
          + " / ".join(out.done[:10]), action="pages_removed_shared", pc=pc, login=login,
          pages=out.done, cause=detail)
    _clean_standard_time(repo, gone_shifts)
    return out


def _remove_from_access(path: Path, keys: list[PageKey], runner) -> tuple[bool, str]:
    """共有が Access のとき。**全ページを1つの取引で**(VBScript / ODBC の取引)。"""
    from ..access_bridge import pusher, script_gen
    from ..access_bridge.runner import run_with_retry
    from ..config import SETTINGS

    pusher.forget_access_tables()
    unread = pusher.access_unread(path)
    if unread:
        return False, f"共有の日報データ(Access)を開けません: {unread}"
    statements: list[str] = []
    for key in keys:
        head = pusher.access_table(path, pusher.header_table_name(key[1]))
        rows = pusher.access_table(path, pusher.detail_table_name(key[1]))
        statements += pusher.build_delete_statements(key, head, rows)
    try:
        if SETTINGS.access_backend == "odbc":
            from ..access_bridge.odbc_backend import push_statements_odbc

            result = push_statements_odbc(path, statements)
        else:
            result = run_with_retry(lambda: script_gen.build_push_script(str(path), statements),
                                    runner=runner)
    except Exception as exc:                      # noqa: BLE001 - 消せなかったとして残す
        log.exception("共有(Access)から消す途中で予期しないエラー")
        return False, f"共有(Access)から消せませんでした: {exc}"
    if result.success:
        return True, ""
    return False, f"共有(Access)から消せませんでした: {result.err_desc or result.error}"


def _clean_standard_time(repo: NippouRepository, shifts: list[tuple[str, str, str]]) -> None:
    """ページの無くなった直の実績を、共有の標準作業時間からも外す(写す待ちに入れて写す)。"""
    if not shifts:
        return
    try:
        from ..config import SETTINGS
        from . import standard_time

        repo.queue_standard_time(shifts)
        standard_time.flush(repo, SETTINGS.standard_time_db_path)
    except Exception:                             # noqa: BLE001 - 次の共有へ保存で写る
        log.exception("標準作業時間から外せませんでした(次の共有へ保存で外れます)")
