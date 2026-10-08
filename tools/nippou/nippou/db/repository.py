"""Local SQLite repository: the day-to-day read/write surface for the UI.

Ports the read/write half of ``NippouDB_Save`` / ``NippouDB_Load`` /
``NippouDB_GetPageCount`` (standard module ~line 13263-14120). Every
write here happens against the *local* SQLite database -- pushing to the
Access .accdb is a separate, explicit step handled by
``access_bridge/pusher.py`` using the ``dirty`` flag this repository
maintains (see :meth:`NippouRepository.pending_sync_headers`).

Lock-competition handling (retry-then-fail on "database is locked") goes
through :mod:`dbkit.sqlite_toolkit` so the classification/retry policy is
shared with the other VBA migration projects rather than reimplemented
per repository. ``save`` is the one write that touches several tables in
a single atomic transaction, which ``execute_with_retry`` (built for one
statement at a time) can't wrap directly -- so it keeps its own
:func:`~.connection.transaction` block but drives the retry loop with the
same :func:`dbkit.sqlite_toolkit.is_lock_error` classifier used
everywhere else, which the original VBA ``NippouDB_Save`` did not have.
"""
from __future__ import annotations

import hashlib
import sqlite3
import time
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

from dbkit.sqlite_toolkit import (
    DEFAULT_MAX_RETRY,
    DEFAULT_RETRY_WAIT_SEC,
    execute_with_retry,
    insert_record,
    is_lock_error,
)

from ..logic.aggregation import SHIFT_ORDER
from ..logic.fingerprint import page_fingerprint, summary_fingerprint
from ..logic.month_roll import next_month
from ..logic.shift import parse_business_date
from .connection import transaction
from .models import (
    DETAIL_FAMILIES,
    REPORT_FIELDS,
    DetailRecord,
    HeaderRecord,
    PackingDetail,
    PackingReport,
    PackingStop,
)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class NippouRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        #: このリポジトリで、控え(LocalBackup)へ写す待ちを入れたか。
        #: 入れたら要求の終わりに写しに行きます(`app/__init__._register_db`)
        self.backup_queued = False

    _BACKUP_QUEUE_SQL = ("INSERT OR REPLACE INTO backup_pending"
                         " (report_date, line, shift, page, queued_at) VALUES (?, ?, ?, ?, ?)")

    def _queue_backup_in_tx(self, key: tuple[str, str, str, int]) -> None:
        """取引の中で、控えへ写す待ちに入れる(**保存と一緒に入るか、一緒に入らないか**)。"""
        self.conn.execute(self._BACKUP_QUEUE_SQL,
                          (*key, datetime.now().isoformat(timespec="seconds")))
        self.backup_queued = True

    # ------------------------------------------------------------------
    # Header + detail save/load (NippouDB_Save / NippouDB_Load)
    # ------------------------------------------------------------------
    def save(self, header: HeaderRecord, details: list[DetailRecord]) -> None:
        """DELETE + INSERT the header and every detail row for this key
        in one transaction, exactly like ``NippouDB_Save`` did against
        Access. Marks the header dirty **only when the content actually
        changed** since it was pushed (see :mod:`nippou.logic.fingerprint`).

        This touches multiple tables in one atomic unit, so it can't be
        expressed as a single ``dbkit.sqlite_toolkit.execute_with_retry``
        call -- instead the whole transaction is retried on lock
        contention using the same classifier dbkit uses everywhere else,
        which the original VBA save routine did not have."""
        header.saved_at = _now_iso()
        key = header.key()

        # **中身が変わっていなければ、未送信に戻さない。**
        #
        #     共有へ保存 をしてから 保存(確定) を行うと
        #     共有へ未送信 1直ぶん になる
        #     差分がなければやはり 共有へ保存 を何度も求める必要がないのでは
        #
        # 指紋を1つ取るだけです(`logic/fingerprint`)── 12行35欄を並べて
        # SHA-256 を1回。この下の DELETE+INSERT 13本に比べれば無いに等しい
        content_hash = page_fingerprint(header, details)

        retry_count = 0
        while True:
            try:
                with transaction(self.conn):
                    # **消す前に読みます。** やり直し(ロック待ち)のときも
                    # 巻き戻った中身をもう一度見るので、答えが変わりません
                    before = self.conn.execute(
                        "SELECT synced_at, synced_hash FROM daily_header"
                        " WHERE report_date=? AND line=? AND shift=? AND page=?",
                        key).fetchone()
                    sent_hash = (before["synced_hash"] or "") if before else ""
                    # **共有と同じか。** 同じなら送った印もそのまま残す
                    same_as_sent = bool(sent_hash) and sent_hash == content_hash
                    header.dirty = not same_as_sent
                    synced_at = before["synced_at"] if same_as_sent else None
                    self.conn.execute(
                        "DELETE FROM daily_detail WHERE report_date=? AND line=? AND shift=? AND page=?",
                        key,
                    )
                    self.conn.execute(
                        "DELETE FROM daily_header WHERE report_date=? AND line=? AND shift=? AND page=?",
                        key,
                    )
                    self.conn.execute(
                        """
                        INSERT INTO daily_header
                            (report_date, line, shift, page, worker, day_shift, count,
                             weight_kg, lot_count, coefficient_lot_count, reason,
                             saved_at, dirty, synced_at, content_hash, synced_hash)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            header.report_date, header.line, header.shift, header.page,
                            header.worker, header.day_shift, header.count, header.weight_kg,
                            header.lot_count, header.coefficient_lot_count, header.reason,
                            header.saved_at, int(header.dirty), synced_at,
                            content_hash,
                            # **送ったときの指紋は消しません。** 直して、
                            # また元に戻したときに「同じ」と分かるように
                            sent_hash,
                        ),
                    )
                    for d in details:
                        self.conn.execute(
                            """
                            INSERT INTO daily_detail (
                                report_date, line, shift, page, row_no,
                                lot, zai, siz, ken, kz, kh, sz, sh, hit, ai,
                                mai, tut, vc, et, s, th, ss, ths, sth, tht,
                                s4, th4, s5, th5,
                                con, wei, tim, uni,
                                others1, others2, others3, others4, others5, others6, keisu,
                                reason, hiki_no, box_course
                            ) VALUES (?,?,?,?,?, ?,?,?,?,?,?,?,?,?,?, ?,?,?,?,?,?,?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?,?,?,?, ?,?,?)
                            """,
                            (
                                header.report_date, header.line, header.shift, header.page, d.row_no,
                                d.lot, d.zai, d.siz, d.ken, d.kz, d.kh, d.sz, d.sh, d.hit, d.ai,
                                d.mai, d.tut, d.vc, d.et, d.s, d.th, d.ss, d.ths, d.sth, d.tht,
                                d.s4, d.th4, d.s5, d.th5,
                                d.con, d.wei, d.tim, d.uni,
                                d.others1, d.others2, d.others3, d.others4, d.others5, d.others6, d.keisu,
                                d.reason, d.hiki_no, d.box_course,
                            ),
                        )
                    # 控え(LocalBackup)へも写す ── **同じ取引で**待ちに入れる
                    self._queue_backup_in_tx(key)
                return
            except sqlite3.Error as exc:
                if is_lock_error(exc) and retry_count < DEFAULT_MAX_RETRY:
                    retry_count += 1
                    time.sleep(DEFAULT_RETRY_WAIT_SEC * retry_count)
                    continue
                raise

    @staticmethod
    def _header_from_row(row: sqlite3.Row) -> HeaderRecord:
        return HeaderRecord(
            report_date=row["report_date"], line=row["line"], shift=row["shift"], page=row["page"],
            worker=row["worker"] or "", day_shift=row["day_shift"] or "", count=row["count"] or "",
            weight_kg=row["weight_kg"] or "", lot_count=row["lot_count"] or "",
            coefficient_lot_count=row["coefficient_lot_count"] or "", reason=row["reason"] or "",
            saved_at=row["saved_at"] or "", dirty=bool(row["dirty"]), synced_at=row["synced_at"],
        )

    def _details_for(self, report_date: str, line: str, shift: str, page: int) -> list[DetailRecord]:
        detail_rows = self.conn.execute(
            "SELECT * FROM daily_detail WHERE report_date=? AND line=? AND shift=? AND page=? ORDER BY row_no",
            (report_date, line, shift, page),
        ).fetchall()
        details = []
        for r in detail_rows:
            kwargs = {"report_date": r["report_date"], "line": r["line"], "shift": r["shift"],
                      "page": r["page"], "row_no": r["row_no"]}
            for fam in (*DETAIL_FAMILIES, "others1", "others2", "others3", "others4", "others5", "others6",
                        "keisu", "reason", "hiki_no", "box_course"):
                kwargs[fam] = r[fam] or ""
            details.append(DetailRecord(**kwargs))
        return details

    def load(
        self, report_date: str, line: str, shift: str, page: int = 1
    ) -> Optional[tuple[HeaderRecord, list[DetailRecord]]]:
        row = self.conn.execute(
            "SELECT * FROM daily_header WHERE report_date=? AND line=? AND shift=? AND page=?",
            (report_date, line, shift, page),
        ).fetchone()
        if row is None:
            return None
        header = self._header_from_row(row)
        return header, self._details_for(report_date, line, shift, page)

    def delete_page(self, report_date: str, line: str, shift: str,
                    page: int) -> bool:
        """その紙を1枚まるごと消す。消したら True、無ければ False。

        **明細と見出しを1つの取引で消します。** 片方だけ残ると、見出しの
        無い明細(どの紙のものか分からない行)や、中身の無い見出しができます。

        呼ぶのは「中身の無い紙を片付ける」だけです
        (`services/empty_pages.py`)── 打ってある紙をここから消す道は
        作っていません。消せるのは**中身が無いことを確かめたぶん**だけ、
        という関門はサービスの側が持ちます。
        """
        key = (report_date, line, shift, page)
        found = self.conn.execute(
            "SELECT 1 FROM daily_header "
            "WHERE report_date=? AND line=? AND shift=? AND page=?",
            key).fetchone()
        if found is None:
            return False

        retry_count = 0
        while True:
            try:
                with transaction(self.conn):
                    self.conn.execute(
                        "DELETE FROM daily_detail "
                        "WHERE report_date=? AND line=? AND shift=? AND page=?",
                        key)
                    self.conn.execute(
                        "DELETE FROM daily_header "
                        "WHERE report_date=? AND line=? AND shift=? AND page=?",
                        key)
                    # 控えからも消す(写す側が「手元に無い」を見て消します)
                    self._queue_backup_in_tx(key)
                return True
            except sqlite3.Error as exc:
                # 保存と同じ待ち方。**鍵の取り合いだけ待ち直します**
                if is_lock_error(exc) and retry_count < DEFAULT_MAX_RETRY:
                    retry_count += 1
                    time.sleep(DEFAULT_RETRY_WAIT_SEC * retry_count)
                    continue
                raise

    # ------------------------------------------------------------------
    # Key discovery (NippouDB_FillDBList / NippouDB_GetPageCount)
    # ------------------------------------------------------------------
    def list_keys(self, line: Optional[str] = None) -> list[tuple[str, str, str, int]]:
        """(report_date, line, shift, page) tuples, newest first --
        mirrors the descending sort ``LoadExistingSheets`` applied to its
        sheet-name list.

        ``report_date`` is stored as "yyyy年m月d日" (month/day *not*
        zero-padded, matching the original VBA ``Format$``), so a plain
        SQL ``ORDER BY report_date`` sorts wrong across month/day-count
        boundaries (e.g. "...8月30日" sorts before "...8月3日"). Sorting
        is done in Python instead, using the parsed calendar date.
        """
        if line is None:
            rows = self.conn.execute(
                "SELECT report_date, line, shift, page FROM daily_header"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT report_date, line, shift, page FROM daily_header WHERE line=?",
                (line,),
            ).fetchall()
        keys = [(r["report_date"], r["line"], r["shift"], r["page"]) for r in rows]
        keys.sort(key=lambda k: (parse_business_date(k[0]) or date.min, k[2], k[3]), reverse=True)
        return keys

    def shift_keys_between(self, start_date: date, end_date: date,
                           line: Optional[str] = None
                           ) -> list[tuple[str, str, str]]:
        """期間に保存がある (報告日, ライン, 直)。**明細は読みません。**

        集計の作り直しは「どの直があるか」だけ分かれば始められます。
        `list_headers_between` はページごとに明細を全部引いてくるので、
        ひと月ぶんを確かめるだけで何千行も読むことになります。
        """
        sql = "SELECT DISTINCT report_date, line, shift FROM daily_header"
        args: tuple = ()
        if line is not None:
            sql += " WHERE line=?"
            args = (line,)
        found: list[tuple[date, str, tuple[str, str, str]]] = []
        for row in self.conn.execute(sql, args).fetchall():
            parsed = parse_business_date(row["report_date"])
            if parsed is None or not (start_date <= parsed <= end_date):
                continue
            found.append((parsed, row["shift"],
                          (row["report_date"], row["line"], row["shift"])))
        found.sort(key=lambda item: (item[0], item[1]))
        return [key for _, _, key in found]

    def page_count(self, report_date: str, line: str, shift: str) -> int:
        """その当直のページ数 = **いちばん大きいページ番号**。

        VBA ``NippouDB_GetPageCount`` は ``MAX([ページ])`` を返します。数え上げ
        (``COUNT(*)``)ではないのが要点で、ページ1とページ3だけが保存されている
        場合に食い違います ── 数え上げだと2になり、ページ移動の上限が2に
        なって**ページ3を開けなくなります**。歯抜けは、ページ2を作ってから消した
        ときや、保存が途中で失敗したときに実際に起こります。
        """
        row = self.conn.execute(
            "SELECT MAX(page) AS p FROM daily_header"
            " WHERE report_date=? AND line=? AND shift=?",
            (report_date, line, shift),
        ).fetchone()
        return int(row["p"]) if row and row["p"] is not None else 0

    def saved_pages(self, report_date: str, line: str, shift: str) -> list[int]:
        """実際に保存されているページの番号。**歯抜けが見える。**

        `page_count` は上限しか返さないので、ページ1と3だけがあるときに
        「ページ2は無い」と示せません。画面はこれを使って、開けるページだけを
        押せるようにします。
        """
        rows = self.conn.execute(
            "SELECT page FROM daily_header"
            " WHERE report_date=? AND line=? AND shift=? ORDER BY page",
            (report_date, line, shift),
        ).fetchall()
        return [int(r["page"]) for r in rows]

    def saved_keys(self, line: Optional[str] = None, limit: int = 200
                   ) -> list[dict[str, object]]:
        """DBに保存済みのキー一覧。**保存日時の新しい順**。

        port of ``NippouDB_FillDBList``(``ORDER BY [保存日時] DESC``)。
        `list_keys` との違いは並び順と保存日時を返すことで、管理者が
        「さっき保存したもの」を探すための一覧です ── 報告日順だと、
        直したばかりの過去データが下のほうに埋もれます。
        """
        sql = ("SELECT report_date, line, shift, page, saved_at, dirty"
               " FROM daily_header")
        params: tuple = ()
        if line is not None:
            sql += " WHERE line=?"
            params = (line,)
        sql += " ORDER BY saved_at DESC LIMIT ?"
        rows = self.conn.execute(sql, params + (max(1, int(limit)),)).fetchall()
        return [{
            "report_date": r["report_date"], "line": r["line"],
            "shift": r["shift"], "page": int(r["page"]),
            "saved_at": r["saved_at"],
            # ③反映を待っているか。`dirty` は「まだ送っていない」の意味
            "synced": not bool(r["dirty"]),
        } for r in rows]

    def latest_page(self, report_date: str, line: str, shift: str) -> int:
        row = self.conn.execute(
            "SELECT MAX(page) AS p FROM daily_header WHERE report_date=? AND line=? AND shift=?",
            (report_date, line, shift),
        ).fetchone()
        return int(row["p"]) if row and row["p"] is not None else 0

    # ------------------------------------------------------------------
    # Cross-date/shift queries (集計用CSV・グラフ履歴表示で使う)
    # ------------------------------------------------------------------
    def list_headers_for_date(
        self, report_date: str, line: Optional[str] = None
    ) -> list[tuple[HeaderRecord, list[DetailRecord]]]:
        """指定した1日(``report_date`` 文字列, 完全一致)に保存されている
        全ヘッダー+明細を、直→ページの順で返す。集計用CSV・印刷プレビューの
        対象選びで使う。"""
        if line is None:
            rows = self.conn.execute(
                "SELECT * FROM daily_header WHERE report_date=? ORDER BY shift, page",
                (report_date,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM daily_header WHERE report_date=? AND line=? ORDER BY shift, page",
                (report_date, line),
            ).fetchall()
        return [
            (header, self._details_for(header.report_date, header.line, header.shift, header.page))
            for header in (self._header_from_row(r) for r in rows)
        ]

    def list_headers_between(
        self, start_date: date, end_date: date, line: Optional[str] = None
    ) -> list[tuple[HeaderRecord, list[DetailRecord]]]:
        """指定期間(両端含む)の全ヘッダー+明細を、日付→直→ページの順で返す。
        グラフの履歴表示(月初〜指定日のデフォルト表示・期間指定)で使う。

        ``report_date`` がゼロ埋めなしの文字列のため SQL側での範囲比較は
        使わず、(``line`` 指定時のみ効くWHERE以外は)一度取得してから
        Python側で日付をパースして絞り込む。"""
        if line is None:
            rows = self.conn.execute("SELECT * FROM daily_header").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM daily_header WHERE line=?", (line,)
            ).fetchall()

        matched: list[tuple[date, HeaderRecord]] = []
        for r in rows:
            header = self._header_from_row(r)
            parsed = parse_business_date(header.report_date)
            if parsed is not None and start_date <= parsed <= end_date:
                matched.append((parsed, header))
        matched.sort(key=lambda item: (item[0], item[1].shift, item[1].page))

        return [
            (header, self._details_for(header.report_date, header.line, header.shift, header.page))
            for _, header in matched
        ]

    # ------------------------------------------------------------------
    # 集計フォーマット ── **打った日報からの投影**
    #
    # VBA は `Agg_OutPut` で集計シートへ並べ直し、`Aggre_Calcul` でその
    # シートを読んで合計していました。ここは同じことを表で行います。
    #
    #   packing_report         1作業日1ライン1直
    #   packing_report_detail  1ロット1行
    #   packing_stop_detail    1停止1行(横持ち→縦持ち)
    #
    # **打つ場所は日報だけ。** こちらは保存のたびに作り直されるので、
    # 食い違ったら作り直せば必ず揃います(`services/summary.py`)。
    # ------------------------------------------------------------------
    def save_packing_report(self, report: PackingReport,
                            details: list[PackingDetail]) -> int:
        """1直ぶんを**まるごと入れ直す**。新しい `report_id` を返す。

        足し込みではなく置き換えです ── 明細を直したときに古い行が
        残っていると、枚数が二重になります。子(`packing_report_detail`
        → `packing_stop_detail`)は外部キーの `ON DELETE CASCADE` で
        一緒に消えます。

        **1直ぶんで1トランザクション。** 途中で落ちたときに、親だけ
        入って明細が無い、という形にしないためです。
        """
        now = _now_iso()
        report.updated_at = now
        key = report.key()
        # **日報と同じ決まり。** 中身が共有と同じなら、送り直しません
        # (`logic/fingerprint`)。集計は保存のたびに作り直されるので、
        # これが無いと「打ち直していないのに毎回送る」ことになります
        content_hash = summary_fingerprint(report, details)
        with transaction(self.conn):
            # 外部キーは接続ごとの設定。ここで確かめておく
            # (`connection.py` が入れているが、渡された接続でも効くように)
            self.conn.execute("PRAGMA foreign_keys = ON")
            old = self.conn.execute(
                "SELECT id, created_at, synced_at, synced_hash FROM packing_report"
                " WHERE work_date=? AND line_name=? AND shift=?", key).fetchone()
            created = old["created_at"] if old else now
            sent_hash = (old["synced_hash"] or "") if old else ""
            same_as_sent = bool(sent_hash) and sent_hash == content_hash
            report.dirty = not same_as_sent
            synced_at = old["synced_at"] if same_as_sent else None
            if old:
                self.conn.execute("DELETE FROM packing_report WHERE id=?",
                                  (old["id"],))
            # 列は `models.REPORT_FIELDS` から組み立てます。**手で並べた
            # `?` の数を数えるのをやめる**ため ── 値を1つ足したときに、
            # 列名・`?`・値の3か所を揃って直さないと静かにずれます
            cursor = self.conn.execute(
                f"""
                INSERT INTO packing_report
                    (work_date, line_name, shift, {', '.join(REPORT_FIELDS)},
                     dirty, synced_at, content_hash, synced_hash,
                     created_at, updated_at)
                VALUES (?,?,?, {', '.join('?' * len(REPORT_FIELDS))},
                        ?, ?, ?, ?, ?, ?)
                """,
                (*key, *(getattr(report, f) for f in REPORT_FIELDS),
                 int(report.dirty), synced_at, content_hash, sent_hash,
                 created, now))
            report_id = int(cursor.lastrowid)
            report.id = report_id
            report.created_at, report.updated_at = created, now

            for detail in details:
                detail.report_id = report_id
                detail.created_at = detail.created_at or now
                detail.updated_at = now
                cur = self.conn.execute(
                    """
                    INSERT INTO packing_report_detail
                        (report_id, page, row_no, lot_no, material_condition,
                         dimension, incoming_quantity, start_hour, start_minute,
                         end_hour, end_minute, worker_count, interleaf,
                         packing_quantity, packing_package_count, vc_type,
                         actual_quantity, actual_weight, work_time, unit_weight,
                         coefficient_lot_count, purpose_code, purpose_name,
                         delivery_destination, packing_spec_no, etc,
                         coil_vertical_split, coil_horizontal_split, hiki_no,
                         created_at, updated_at)
                    VALUES (?,?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?, ?,?,?,?,
                            ?,?,?, ?,?,?, ?,?,?, ?,?)
                    """,
                    (report_id, detail.page, detail.row_no, detail.lot_no,
                     detail.material_condition, detail.dimension,
                     detail.incoming_quantity, detail.start_hour,
                     detail.start_minute, detail.end_hour, detail.end_minute,
                     detail.worker_count, detail.interleaf,
                     detail.packing_quantity, detail.packing_package_count,
                     detail.vc_type, detail.actual_quantity,
                     detail.actual_weight, detail.work_time,
                     detail.unit_weight, detail.coefficient_lot_count,
                     detail.purpose_code, detail.purpose_name,
                     detail.delivery_destination, detail.packing_spec_no,
                     detail.etc, detail.coil_vertical_split,
                     detail.coil_horizontal_split, detail.hiki_no,
                     detail.created_at, now))
                detail.id = int(cur.lastrowid)
                for stop in detail.stops:
                    stop.detail_id = detail.id
                    stop.created_at = stop.created_at or now
                    stop.updated_at = now
                    self.conn.execute(
                        "INSERT INTO packing_stop_detail"
                        " (detail_id, stop_no, stop_code, stop_reason,"
                        "  stop_kind, stop_minutes, created_at, updated_at)"
                        " VALUES (?,?,?,?,?,?,?,?)",
                        (detail.id, stop.stop_no, stop.stop_code,
                         stop.stop_reason, stop.stop_kind, stop.stop_minutes,
                         stop.created_at, now))
        return report_id

    @staticmethod
    def _report_from_row(row: sqlite3.Row) -> PackingReport:
        return PackingReport(
            id=int(row["id"]), work_date=row["work_date"],
            line_name=row["line_name"], shift=row["shift"],
            worker_name=row["worker_name"] or "",
            daytime_operation=row["daytime_operation"] or "",
            reason=row["reason"] or "", pages=int(row["pages"] or 0),
            operation_time=float(row["operation_time"] or 0.0),
            work_time=float(row["work_time"] or 0.0),
            operating_time=float(row["operating_time"] or 0.0),
            operating_rate=float(row["operating_rate"] or 0.0),
            equipment_stop_total=float(row["equipment_stop_total"] or 0.0),
            setup_stop_total=float(row["setup_stop_total"] or 0.0),
            handling_stop_total=float(row["handling_stop_total"] or 0.0),
            total_lot_count=int(row["total_lot_count"] or 0),
            coefficient_lot_count=float(row["coefficient_lot_count"] or 0.0),
            total_quantity=float(row["total_quantity"] or 0.0),
            total_weight=float(row["total_weight"] or 0.0),
            productivity=float(row["productivity"] or 0.0),
            dirty=bool(row["dirty"]), synced_at=row["synced_at"],
            created_at=row["created_at"] or "", updated_at=row["updated_at"] or "")

    @staticmethod
    def _packing_detail_from_row(row: sqlite3.Row) -> PackingDetail:
        return PackingDetail(
            id=int(row["id"]), report_id=int(row["report_id"]),
            page=int(row["page"] or 1), row_no=int(row["row_no"] or 0),
            lot_no=row["lot_no"] or "",
            material_condition=row["material_condition"] or "",
            dimension=row["dimension"] or "",
            incoming_quantity=float(row["incoming_quantity"] or 0.0),
            start_hour=row["start_hour"], start_minute=row["start_minute"],
            end_hour=row["end_hour"], end_minute=row["end_minute"],
            worker_count=float(row["worker_count"] or 0.0),
            interleaf=row["interleaf"] or "",
            packing_quantity=float(row["packing_quantity"] or 0.0),
            packing_package_count=float(row["packing_package_count"] or 0.0),
            vc_type=row["vc_type"] or "",
            actual_quantity=float(row["actual_quantity"] or 0.0),
            actual_weight=float(row["actual_weight"] or 0.0),
            work_time=float(row["work_time"] or 0.0),
            unit_weight=float(row["unit_weight"] or 0.0),
            coefficient_lot_count=float(row["coefficient_lot_count"] or 0.0),
            purpose_code=row["purpose_code"] or "",
            purpose_name=row["purpose_name"] or "",
            delivery_destination=row["delivery_destination"] or "",
            packing_spec_no=row["packing_spec_no"] or "",
            etc=row["etc"] or "",
            coil_vertical_split=row["coil_vertical_split"] or "",
            coil_horizontal_split=row["coil_horizontal_split"] or "",
            hiki_no=row["hiki_no"] or "",
            created_at=row["created_at"] or "", updated_at=row["updated_at"] or "")

    def load_packing_report(self, work_date: str, line: str, shift: str
                            ) -> Optional[PackingReport]:
        row = self.conn.execute(
            "SELECT * FROM packing_report"
            " WHERE work_date=? AND line_name=? AND shift=?",
            (work_date, line, shift)).fetchone()
        return self._report_from_row(row) if row else None

    def packing_reports_between(self, start_date: date, end_date: date,
                                line: Optional[str] = None
                                ) -> list[PackingReport]:
        """期間ぶんの直の合計。並びは 日付 → 直(紙に出る順)。

        `work_date` はゼロ埋めなしの文字列なので、SQLの範囲比較ではなく
        Python 側でパースして絞ります(`list_headers_between` と同じ)。
        """
        if line is None:
            rows = self.conn.execute("SELECT * FROM packing_report").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM packing_report WHERE line_name=?",
                (line,)).fetchall()
        found: list[tuple[date, int, str, PackingReport]] = []
        for row in rows:
            record = self._report_from_row(row)
            parsed = parse_business_date(record.work_date)
            if parsed is not None and start_date <= parsed <= end_date:
                found.append((parsed, SHIFT_ORDER.get(record.shift, 99),
                              record.shift, record))
        found.sort(key=lambda item: item[:3])
        return [record for *_, record in found]

    def packing_details(self, report_id: int) -> list[PackingDetail]:
        """1直ぶんのロット行(停止つき)。並びは ページ → 行番号。"""
        details = [self._packing_detail_from_row(r) for r in self.conn.execute(
            "SELECT * FROM packing_report_detail WHERE report_id=?"
            " ORDER BY page, row_no", (report_id,)).fetchall()]
        if not details:
            return []
        by_id = {d.id: d for d in details}
        marks = ",".join("?" for _ in by_id)
        for row in self.conn.execute(
                f"SELECT * FROM packing_stop_detail WHERE detail_id IN ({marks})"
                " ORDER BY detail_id, stop_no", tuple(by_id)):
            by_id[int(row["detail_id"])].stops.append(PackingStop(
                id=int(row["id"]), detail_id=int(row["detail_id"]),
                stop_no=int(row["stop_no"] or 0),
                stop_code=row["stop_code"] or "",
                stop_reason=row["stop_reason"] or "",
                stop_kind=row["stop_kind"] or "",
                stop_minutes=float(row["stop_minutes"] or 0.0),
                created_at=row["created_at"] or "",
                updated_at=row["updated_at"] or ""))
        return details

    def packing_details_between(self, start_date: date, end_date: date,
                                line: Optional[str] = None
                                ) -> list[tuple[PackingReport, list[PackingDetail]]]:
        """期間ぶんのロット行。ロット別集計と一覧が読むもの。"""
        return [(report, self.packing_details(report.id))
                for report in self.packing_reports_between(start_date, end_date,
                                                           line=line)]

    def packing_report_keys(self) -> list[tuple[str, str, str]]:
        """集計が入っている (作業日, ライン, 直)。取りこぼし防止に使う。"""
        return [(r["work_date"], r["line_name"], r["shift"]) for r in
                self.conn.execute(
                    "SELECT work_date, line_name, shift FROM packing_report")]

    def pending_packing_reports(self) -> list[PackingReport]:
        """まだ共有へ送っていない集計。"""
        return [self._report_from_row(r) for r in self.conn.execute(
            "SELECT * FROM packing_report WHERE dirty=1").fetchall()]

    def mark_packing_report_synced(self, key: tuple[str, str, str]) -> None:
        execute_with_retry(
            self.conn,
            "UPDATE packing_report SET dirty=0, synced_at=?,"
            " synced_hash=content_hash"
            " WHERE work_date=? AND line_name=? AND shift=?",
            (_now_iso(), *key), caller_name="mark_packing_report_synced")

    # ------------------------------------------------------------------
    # 月替わりの書き出し
    #
    # **日報そのものは消しません。** VBA の `月初削除` が消していたのは
    # Excelシート(見た目の写し)で、元データは Access に残っていました。
    # このツールの日報は**値**で、年月日と直で呼び出せるもの ── 消したら
    # 二度と同じ紙を出せません。月替わりにやるのは書き出しだけです。
    # ------------------------------------------------------------------
    def month_keys(self) -> list[tuple[int, int]]:
        """入っている報告日の (年, 月)。重複なし・古い順。

        月替わりかどうかは**入っているデータから**決めます
        (`logic/month_roll.py`)。VBA もシート名の日付を見ていました。
        """
        rows = self.conn.execute("SELECT DISTINCT report_date FROM daily_header").fetchall()
        months = set()
        for r in rows:
            parsed = parse_business_date(r["report_date"])
            if parsed is not None:
                months.add((parsed.year, parsed.month))
        return sorted(months)

    def month_fingerprints(self) -> dict[tuple[int, int], str]:
        """(年, 月) ごとの、**その月の中身の指紋**。

        ページごとの指紋(`content_hash`)を鍵の順に束ねたもの。どれか
        1ページでも中身が変わる・増える・減ると変わります。同じ中身を
        保存し直しただけなら変わりません。
        """
        pages: dict[tuple[int, int], list[str]] = {}
        for r in self.conn.execute(
                "SELECT report_date, line, shift, page, content_hash FROM daily_header"):
            parsed = parse_business_date(r["report_date"])
            if parsed is None:
                continue
            pages.setdefault((parsed.year, parsed.month), []).append(
                "\t".join((r["report_date"], r["line"], r["shift"], str(r["page"]),
                           r["content_hash"] or "")))
        return {month: hashlib.sha1("\n".join(sorted(items)).encode("utf-8")).hexdigest()
                for month, items in pages.items()}

    def mark_rolled(self, year: int, month: int) -> None:
        """その月の月替わりの書き出しを済ませた(`month_rollover`)。

        **いまの中身の指紋**も一緒に覚えます。書き込みは1つずつ通る
        (`app/__init__.py` の `_WRITE_LOCK`)ので、書き出してからここまでの
        あいだに別の保存が割り込むことはありません。
        """
        fingerprint = self.month_fingerprints().get((year, month), "")
        self.conn.execute(
            "INSERT INTO month_rollover (year, month, done_at, fingerprint) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(year, month) DO UPDATE SET "
            "done_at=excluded.done_at, fingerprint=excluded.fingerprint",
            (year, month, _now_iso(), fingerprint))
        self.conn.commit()

    def settled_months(self) -> set[tuple[int, int]]:
        """書き出しを済ませ、**そのあと中身が変わっていない**月。

        手元の日報は消さないので、これが無いと同じ月を何度も書き出し、
        次の月がいつまでも書き出されません(`schema.py` の注記)。
        """
        rows = self.conn.execute(
            "SELECT year, month, fingerprint FROM month_rollover").fetchall()
        if not rows:
            return set()
        now = self.month_fingerprints()
        return {(r["year"], r["month"]) for r in rows
                if now.get((r["year"], r["month"])) == r["fingerprint"]}

    # ------------------------------------------------------------------
    # 共有保存の履歴 (`push_history`)
    # ------------------------------------------------------------------
    PUSH_HISTORY_COLUMNS = ("id", "pressed_at", "report_date", "line", "shift",
                            "pages", "worker", "result", "failed_pages", "detail",
                            "pressed_key", "pressed_by", "via", "terminal")

    def add_push_history(self, rows: list[dict]) -> None:
        """押した記録を足す。**消しも書き換えもしない**(履歴なので)。"""
        if not rows:
            return
        cols = self.PUSH_HISTORY_COLUMNS
        self.conn.executemany(
            f"INSERT OR IGNORE INTO push_history ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' for _ in cols)})",
            [tuple(row.get(c, "") if c not in ("pages", "failed_pages")
                   else int(row.get(c) or 0) for c in cols) for row in rows])
        self.conn.commit()

    def push_history(self, *, unshared_only: bool = False) -> list[dict]:
        """押した記録ぜんぶ(古い順)。`unshared_only` なら共有へ写していないものだけ。"""
        sql = "SELECT * FROM push_history"
        if unshared_only:
            sql += " WHERE shared=0"
        # 同じ秒に押した行は**書いた順**(id は乱数なので並びの鍵にしない)
        sql += " ORDER BY pressed_at, rowid"
        return [dict(r) for r in self.conn.execute(sql).fetchall()]

    def mark_push_history_shared(self, ids: list[str]) -> None:
        if not ids:
            return
        self.conn.executemany("UPDATE push_history SET shared=1 WHERE id=?",
                              [(i,) for i in ids])
        self.conn.commit()

    # -- 控え(LocalBackup)へ写す待ち (`services/local_backup`) ---------------
    def queue_backup(self, keys: Iterable[tuple[str, str, str, int]]) -> None:
        """控えへ写す待ちに入れる(ページ単位。同じページは1つ)。"""
        rows = [(*k, datetime.now().isoformat(timespec="seconds")) for k in keys]
        if not rows:
            return
        self.conn.executemany(self._BACKUP_QUEUE_SQL, rows)
        self.conn.commit()
        self.backup_queued = True

    def backup_pending(self) -> list[tuple[str, str, str, int]]:
        """控えへ写す待ちのページ(入れた順)。"""
        return [(r["report_date"], r["line"], r["shift"], int(r["page"]))
                for r in self.conn.execute(
                    "SELECT report_date, line, shift, page FROM backup_pending"
                    " ORDER BY queued_at, rowid")]

    def clear_backup_pending(self, keys: Iterable[tuple[str, str, str, int]]) -> None:
        rows = [tuple(k) for k in keys]
        if not rows:
            return
        self.conn.executemany(
            "DELETE FROM backup_pending WHERE report_date=? AND line=? AND shift=? AND page=?",
            rows)
        self.conn.commit()

    # -- 標準作業時間へ写す待ち (`services/standard_time`) ------------------
    def queue_standard_time(self, keys: Iterable[tuple[str, str, str]]) -> None:
        """写す待ちに入れる `(報告日, ライン, 直)`。同じ直は1つ。"""
        rows = [(d, ln, s, datetime.now().isoformat(timespec="seconds"))
                for d, ln, s in keys]
        if not rows:
            return
        self.conn.executemany(
            "INSERT OR IGNORE INTO standard_time_pending"
            " (work_date, line, shift, queued_at) VALUES (?, ?, ?, ?)", rows)
        self.conn.commit()

    def standard_time_pending(self) -> list[tuple[str, str, str]]:
        """写す待ちの直(入れた順)。"""
        return [(r["work_date"], r["line"], r["shift"]) for r in self.conn.execute(
            "SELECT work_date, line, shift FROM standard_time_pending"
            " ORDER BY queued_at, rowid")]

    def clear_standard_time_pending(self, keys: Iterable[tuple[str, str, str]]) -> None:
        rows = list(keys)
        if not rows:
            return
        self.conn.executemany(
            "DELETE FROM standard_time_pending WHERE work_date=? AND line=? AND shift=?",
            rows)
        self.conn.commit()

    def recent_stop_codes(self, line: str, pages: int = 60) -> list[str]:
        """そのラインで**最近保存したページ**の停止記号(①〜⑤)を、新しい順に。

        日報入力の停止記号の一覧に「よく使う」を出すため(v4.7.0
        `logic/input_shortcuts.frequent_codes`)。報告日は「2026年8月3日」の
        字なので、並べるのは保存日時で ── 直したばかりの過去の直も最近の
        ぶんに入りますが、記号の癖を見るには十分です。
        """
        rows = self.conn.execute(
            "SELECT d.s, d.ss, d.sth, d.s4, d.s5 FROM daily_detail d"
            " JOIN (SELECT report_date, line, shift, page, saved_at FROM daily_header"
            "       WHERE line=? ORDER BY saved_at DESC LIMIT ?) h"
            "   ON d.report_date=h.report_date AND d.line=h.line"
            "  AND d.shift=h.shift AND d.page=h.page"
            " ORDER BY h.saved_at DESC, d.page, d.row_no",
            (line, max(1, int(pages)))).fetchall()
        return [str(code).strip() for r in rows
                for code in (r["s"], r["ss"], r["sth"], r["s4"], r["s5"])
                if code and str(code).strip()]

    def shift_workers(self, report_date: str, line: str, shift: str) -> list[str]:
        """その直の担当者(ページごとの作業者名。重なりは1つに、ページ順)。"""
        rows = self.conn.execute(
            "SELECT worker FROM daily_header WHERE report_date=? AND line=? AND shift=?"
            " ORDER BY page", (report_date, line, shift)).fetchall()
        names: list[str] = []
        for r in rows:
            name = (r["worker"] or "").strip()
            if name and name not in names:
                names.append(name)
        return names

    def month_headers(self, year: int, month: int, line: Optional[str] = None
                      ) -> list[tuple[HeaderRecord, list[DetailRecord]]]:
        """その月ぶん(全直・全ページ)。書き出しにも、消す前の数え上げにも使う。"""
        # 月末は「次の月の1日の前日」。**12月をまたぐ足し算は
        # `month_roll` が1か所で持っています** ── ここで `month % 12 + 1`
        # と書くと、同じ繰り上がりを2か所で持つことになる
        first = date(year, month, 1)
        last = date(*next_month(year, month), 1) - timedelta(days=1)
        return self.list_headers_between(first, last, line=line)

    def month_has_pending(self, year: int, month: int) -> bool:
        """その月に、**まだ共有へ送っていない**ページが残っているか。

        書き出しの結果に添えて画面に出します ── 共有へ出ていない月を
        「片付いた」と受け取られないように。
        """
        for header, _ in self.month_headers(year, month):
            if header.dirty:
                return True
        return False

    # ------------------------------------------------------------------
    # Access sync bookkeeping
    # ------------------------------------------------------------------
    def pending_sync_headers(self) -> list[HeaderRecord]:
        rows = self.conn.execute("SELECT * FROM daily_header WHERE dirty=1").fetchall()
        return [self._header_from_row(r) for r in rows]

    def mark_synced(self, key: tuple[str, str, str, int]) -> None:
        """送れた印。**そのときの指紋も控えます。**

        次の保存はこの指紋と比べて、同じなら未送信に戻しません
        (`logic/fingerprint`)。
        """
        result = execute_with_retry(
            self.conn,
            "UPDATE daily_header SET dirty=0, synced_at=?, synced_hash=content_hash "
            "WHERE report_date=? AND line=? AND shift=? AND page=?",
            (_now_iso(), *key),
            caller_name="mark_synced",
        )
        if not result.ok:
            raise sqlite3.OperationalError(result.error or "mark_synced failed")
        # 送れた印も控えへ写す(管理者が控えを見たとき「共有へ済」と分かるように)
        try:
            with transaction(self.conn):
                self._queue_backup_in_tx(tuple(key))
        except sqlite3.Error:
            # 写す待ちに入れられなくても、送れた印は付いている(次の保存で写る)
            pass

    def log_sync(self, direction: str, table_name: str, status: str, detail: str = "") -> None:
        result = insert_record(
            self.conn, "sync_log",
            {
                "direction": direction, "table_name": table_name, "status": status,
                "detail": detail, "created_at": _now_iso(),
            },
            caller_name="log_sync",
        )
        if not result.ok:
            raise sqlite3.OperationalError(result.error or "log_sync failed")

    # ------------------------------------------------------------------
    # Shift boundary config (Set_Shift / shiftTime)
    # ------------------------------------------------------------------
    def set_shift_time(self, shift_key: str, start_time: str, end_time: str) -> None:
        result = execute_with_retry(
            self.conn,
            "INSERT INTO shift_config (shift_key, start_time, end_time, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(shift_key) DO UPDATE SET start_time=excluded.start_time, "
            "end_time=excluded.end_time, updated_at=excluded.updated_at",
            (shift_key, start_time, end_time, _now_iso()),
            caller_name="set_shift_time",
        )
        if not result.ok:
            raise sqlite3.OperationalError(result.error or "set_shift_time failed")

    def replace_shift_times(self, times: dict[str, tuple[str, str]]) -> None:
        """手元の直の時刻を、**マスタの中身に揃える。**

        `set_shift_time` を並べるだけだと、マスタから消えた直(マスタ管理で
        「昼」の行を消した等)が手元に残り、消したのに効き続けます。
        マスタに無い直は手元からも消します。

        **空なら何もしません。** 空の結果は「読めなかった」と見分けが
        つかないので、それで手元を消すと直の時刻がぜんぶ控えに戻ります。
        """
        if not times:
            return
        for key, (start, end) in times.items():
            self.set_shift_time(key, start, end)
        marks = ", ".join("?" for _ in times)
        result = execute_with_retry(
            self.conn,
            f"DELETE FROM shift_config WHERE shift_key NOT IN ({marks})",
            tuple(times),
            caller_name="replace_shift_times",
        )
        if not result.ok:
            raise sqlite3.OperationalError(result.error or "replace_shift_times failed")

    def get_shift_times(self) -> dict[str, tuple[str, str]]:
        rows = self.conn.execute("SELECT shift_key, start_time, end_time FROM shift_config").fetchall()
        return {r["shift_key"]: (r["start_time"], r["end_time"]) for r in rows}

    # ------------------------------------------------------------------
    # 直を締めた印 (VBA SavePrintStatusToRegistry / LoadPrintStatusFromRegistry)
    #
    # **「刷ったか」ではなく「締めたか」です。** VBA はレジストリに印刷
    # 時刻を入れ、それを見て催促と自動印刷を止めていました。こちらは紙を
    # 作業者が欲しいときだけ出すものにしたので、印が意味するのは
    # 「その直の保存・集計を確定した」こと ── 紙を出したかどうかとは
    # 関係ありません(刷っても印は立ちません)。
    #
    # 表と列の名前は `print_status` / `printed_at` のままです。中身の
    # 意味だけが変わったので、入っている行を捨てる理由がありません。
    # ------------------------------------------------------------------
    def mark_shift_closed(self, shift_name: str, print_date: date, line: str) -> None:
        result = execute_with_retry(
            self.conn,
            "INSERT INTO print_status (shift_name, print_date, line, printed_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(shift_name, print_date, line) DO UPDATE SET printed_at=excluded.printed_at",
            (shift_name, print_date.isoformat(), line, _now_iso()),
            caller_name="mark_shift_closed",
        )
        if not result.ok:
            raise sqlite3.OperationalError(result.error or "mark_shift_closed failed")

    def is_shift_closed(self, shift_name: str, print_date: date, line: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM print_status WHERE shift_name=? AND print_date=? AND line=?",
            (shift_name, print_date.isoformat(), line),
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # 直の引き継ぎ (`shift_handover`)
    #
    # **直せないまま次の直へ渡した、という事実だけを持ちます。** 直った
    # かどうかは `daily_header.dirty`(共有へ出たか)が持つので、ここを
    # 消したり書き換えたりはしません ── 引き継いだのは過去の出来事です。
    # ------------------------------------------------------------------
    def record_handover(self, report_date: str, line: str, shift: str, *,
                        taken_by: str, findings: str = "") -> None:
        """前の直を引き継いだことを残す。**同じ直は上書き**(押し直し)。"""
        result = execute_with_retry(
            self.conn,
            "INSERT INTO shift_handover"
            " (report_date, line, shift, taken_by, taken_at, findings)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(report_date, line, shift) DO UPDATE SET"
            "   taken_by=excluded.taken_by, taken_at=excluded.taken_at,"
            "   findings=excluded.findings",
            (report_date, line, shift, taken_by, _now_iso(), findings),
            caller_name="record_handover",
        )
        if not result.ok:
            raise sqlite3.OperationalError(result.error or "record_handover failed")

    # ------------------------------------------------------------------
    # 後日作成 (`backfill`)
    #
    # **終わった直を管理者が後から作った**事実だけを持ちます。中身は
    # `daily_header` / `daily_detail` に入るので、印を出すのは中身がある
    # 枠だけです(`backfills` が絞ります)。
    # ------------------------------------------------------------------
    def record_backfill(self, report_date: str, line: str, shift: str, page: int,
                        *, terminal: str = "", note: str = "") -> None:
        """後から作り始めたことを残す。**同じ枠は上書き**(作りかけで戻って、また作る)。"""
        result = execute_with_retry(
            self.conn,
            "INSERT INTO backfill (report_date, line, shift, page, opened_at, terminal, note)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(report_date, line, shift, page) DO UPDATE SET"
            "   opened_at=excluded.opened_at, terminal=excluded.terminal,"
            "   note=excluded.note",
            (report_date, line, shift, int(page), _now_iso(), terminal, note),
            caller_name="record_backfill",
        )
        if not result.ok:
            raise sqlite3.OperationalError(result.error or "record_backfill failed")

    def backfills(self, report_date: Optional[str] = None, line: Optional[str] = None,
                  shift: Optional[str] = None) -> dict[tuple[str, str, str, int], dict]:
        """後から作ったページ。**中身が保存されている枠だけ**(作りかけは出さない)。

        鍵は (報告日, ライン, 直, ページ)。絞る値を渡せばその直だけ。
        """
        where, args = [], []
        for column, value in (("b.report_date", report_date), ("b.line", line),
                              ("b.shift", shift)):
            if value is not None:
                where.append(f"{column}=?")
                args.append(value)
        rows = self.conn.execute(
            "SELECT b.report_date, b.line, b.shift, b.page, b.opened_at, b.terminal,"
            " b.note, h.saved_at FROM backfill b JOIN daily_header h"
            " ON h.report_date=b.report_date AND h.line=b.line AND h.shift=b.shift"
            " AND h.page=b.page" + (" WHERE " + " AND ".join(where) if where else ""),
            args).fetchall()
        return {(r["report_date"], r["line"], r["shift"], int(r["page"])): {
                    "opened_at": r["opened_at"], "terminal": r["terminal"] or "",
                    "note": r["note"] or "", "saved_at": r["saved_at"] or ""}
                for r in rows}

    def backfill_opened(self, report_date: str, line: str, shift: str,
                        page: int) -> Optional[dict]:
        """その枠を後から作り始めた記録(**中身がまだ無くても**返す)。入力画面の帯に使う。"""
        row = self.conn.execute(
            "SELECT opened_at, terminal, note FROM backfill"
            " WHERE report_date=? AND line=? AND shift=? AND page=?",
            (report_date, line, shift, int(page))).fetchone()
        if row is None:
            return None
        return {"opened_at": row["opened_at"], "terminal": row["terminal"] or "",
                "note": row["note"] or ""}

    def handover_of(self, report_date: str, line: str, shift: str) -> Optional[dict]:
        """その直の引き継ぎ記録。無ければ None。"""
        row = self.conn.execute(
            "SELECT taken_by, taken_at, findings FROM shift_handover"
            " WHERE report_date=? AND line=? AND shift=?",
            (report_date, line, shift),
        ).fetchone()
        if row is None:
            return None
        return {"taken_by": row["taken_by"], "taken_at": row["taken_at"],
                "findings": row["findings"] or ""}
