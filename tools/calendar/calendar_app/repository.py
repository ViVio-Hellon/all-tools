"""データアクセス層 (VBA の modData / modDB の移植)。

VBA では 1 操作ごとに Access へ ADO 接続していたが、
Python 版は SQLite への接続を 1 本使い回す。
実際の SQL 実行は ``dbkit.sqlite_toolkit`` (ロック競合リトライ付き) に任せる。

Access への反映は 2 段構え::

    追加 (休みの登録・コメント・削除履歴) -> dbkit.outbox_sync が自動検知して送る
    削除 (休みの登録の取り消し)           -> sync/deletes.py が明示的に転送する

追加側は「SQLite の業務テーブルに新しく増えた行」を outbox_sync が
自分で見つけてくれるため、このモジュールが変更履歴を明示的に記録する
必要はない。削除は行そのものが消えるため、その一手だけ ``sync/deletes.py``
に予約を残す。
"""

from __future__ import annotations

import datetime as _dt
import getpass
import platform
import sqlite3
from dataclasses import dataclass
from typing import Iterable, Sequence

from . import config, db
from .dbkit import sqlite_toolkit
from .logging_utils import debug_log
from .sync import specs as sync_specs, total_pending_count
from .sync.deletes import queue_delete

__all__ = [
    "DayRecord",
    "Worker",
    "Repository",
    "build_day_text",
    "build_day_tooltip",
    "build_day_detail",
    "comment_origin",
    "comment_when",
    "shown_time",
    "shift_neighbours",
]


def _date_key(d: _dt.date) -> str:
    """DB に保存する日付文字列 (yyyy/mm/dd)。"""
    if isinstance(d, _dt.datetime):
        d = d.date()
    return d.strftime(config.DATE_KEY_FORMAT)


def operator_name() -> str:
    """削除実行者の識別文字列 (VBA: GetOperatorName)。"""
    try:
        user = getpass.getuser()
    except Exception:
        user = "unknown"
    return f"{user}@{platform.node() or 'unknown'}"


@dataclass
class DayRecord:
    """休み管理テーブルの 1 レコード。

    VBA では 9 要素の配列 (区分, 登録内容, 識別コード, 直, 残業者, 早出者, 班, ライン, ID)
    を扱っていたが、意味が分かりにくいため名前付きの構造体にしている。
    """

    id: int
    kubun: str
    naiyou: str
    code: str
    shift: str
    overtime: str
    early: str
    group: str
    line: str
    date: _dt.date | None = None
    access_id: int | None = None
    #: 連絡を残した班 (VER4.2.4)。前の版の行は空 ── ``group`` は宛先として選んだ班
    writer_group: str = ""
    #: 保存した時刻 ``yyyy/mm/dd hh:mm:ss`` (VER4.2.4)。前の版の行は空
    registered_at: str = ""

    @property
    def is_rest(self) -> bool:
        return self.kubun == config.KUBUN_REST

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "DayRecord":
        keys = row.keys()
        raw_date = row["日付"] if "日付" in keys else None
        parsed: _dt.date | None = None
        if raw_date:
            try:
                parsed = _dt.datetime.strptime(raw_date, config.DATE_KEY_FORMAT).date()
            except ValueError:
                parsed = None
        return cls(
            id=row["ID"],
            kubun=row["区分"] or "",
            naiyou=row["登録内容"] or "",
            code=row["識別コード"] or "",
            shift=(row["直"] or "").strip(),
            overtime=(row["残業者"] or "").strip(),
            early=(row["早出者"] or "").strip(),
            group=(row["班"] or "").strip(),
            line=(row["ライン"] or "").strip(),
            date=parsed,
            access_id=row["access_id"] if "access_id" in keys else None,
            # 後から足した列。**無い表・空の行でも読める**(空のまま)
            writer_group=_text(row, keys, config.COL_WRITER_GROUP),
            registered_at=_text(row, keys, config.COL_REGISTERED_AT),
        )


def _text(row: sqlite3.Row, keys: Sequence[str], name: str) -> str:
    """列があれば前後の空白を落とした文字、無ければ空。"""
    if name not in keys:
        return ""
    return str(row[name] or "").strip()


@dataclass
class Worker:
    """班員名簿の 1 行 (VBA: Get作業者一覧 の戻り値 1 行分)。"""

    code: str  # 管理番号
    group: str  # 班
    name: str  # 名前
    line: str  # 担当ライン
    reading: str = ""  # 読み (並び替え用)

    @property
    def display(self) -> str:
        """選択リスト表示用 "ライン 班 名前"。"""
        return f"{self.line} {self.group} {self.name}".strip()

    @property
    def is_day_shift(self) -> bool:
        """3 交替に属さない (昼勤・日勤など) なら True。

        VBA: 班が A/B/C/D 以外なら直の確認を行わずそのまま登録する。
        """
        return self.group not in config.SHIFT_GROUPS


class Repository:
    """SQLite 上のカレンダーデータへのアクセス窓口。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ------------------------------------------------------------------
    # 参照系
    # ------------------------------------------------------------------
    def get_month_records(
        self, start: _dt.date, end: _dt.date
    ) -> dict[str, list[DayRecord]]:
        """表示範囲 (42 日分) を 1 回の問い合わせでまとめて取得する。

        VBA: GetMonthRecords。セルごとに毎回 DB を叩くと遅いため範囲でまとめて読む。
        戻り値は "yyyy/mm/dd" をキーとした辞書。
        """
        rows = self.conn.execute(
            f'SELECT * FROM "{config.TABLE_DATA}" '
            'WHERE "日付" >= ? AND "日付" <= ? ORDER BY "ID"',
            (_date_key(start), _date_key(end)),
        ).fetchall()

        result: dict[str, list[DayRecord]] = {}
        for row in rows:
            result.setdefault(row["日付"], []).append(DayRecord.from_row(row))
        debug_log(f"Repository.get_month_records: 取得日数={len(result)}")
        return result

    def month_counts(self, before: _dt.date, my_line: str | None = None) -> dict[str, int]:
        """``before`` より前の登録件数を月ごとに(履歴表示の月選び)。

        キーは ``yyyy/mm``。日付は ``yyyy/mm/dd`` の文字で持っているので、
        先頭7文字がそのまま年月になる。``my_line`` を渡すと、その端末のマス目に出る
        登録だけを数える(`record_shown`。VER4.2.6)。
        """
        if my_line is not None:
            counts: dict[str, int] = {}
            for row in self.conn.execute(
                    f'SELECT * FROM "{config.TABLE_DATA}" WHERE "日付" < ?', (_date_key(before),)):
                ym = str(row["日付"] or "")[:7]
                if ym and record_shown(DayRecord.from_row(row), my_line):
                    counts[ym] = counts.get(ym, 0) + 1
            return counts
        rows = self.conn.execute(
            f'SELECT substr("日付", 1, 7) AS ym, COUNT(*) AS n '
            f'FROM "{config.TABLE_DATA}" WHERE "日付" < ? GROUP BY ym',
            (_date_key(before),),
        ).fetchall()
        return {row["ym"]: int(row["n"]) for row in rows if row["ym"]}

    def get_day_records(self, d: _dt.date) -> list[DayRecord]:
        """指定日 1 日分の登録内容を取得する (VBA: GetDayRecords)。"""
        rows = self.conn.execute(
            f'SELECT * FROM "{config.TABLE_DATA}" WHERE "日付"=? ORDER BY "ID"',
            (_date_key(d),),
        ).fetchall()
        return [DayRecord.from_row(r) for r in rows]

    def exists_record(self, d: _dt.date, code: str) -> bool:
        """同一日付に同じ管理番号が登録済みか (VBA: ExistsRecord)。休みの二重登録防止。"""
        row = self.conn.execute(
            f'SELECT COUNT(*) AS cnt FROM "{config.TABLE_DATA}" '
            'WHERE "日付"=? AND "識別コード"=?',
            (_date_key(d), code),
        ).fetchone()
        return bool(row["cnt"])

    # ------------------------------------------------------------------
    # 更新系
    # ------------------------------------------------------------------
    def save_record(
        self,
        d: _dt.date,
        kubun: str,
        naiyou: str,
        code: str,
        shift: str = "",
        overtime: str = "",
        early: str = "",
        group: str = "",
        line: str = "",
        writer_group: str = "",
        registered_at: str | None = None,
    ) -> int:
        """1 件保存する (VBA: SaveRecord)。戻り値は採番された ID。

        ``writer_group`` は連絡を**残した**班、``registered_at`` は保存した時刻
        (省略すると今。VER4.2.4)。時刻は送った時刻ではなく、**打った人が
        登録を押した時刻**を残す ── 共有が落ちていて送るのが遅れても変わらない。
        """
        if registered_at is None:
            registered_at = db.now_string()
        values = {
            "日付": _date_key(d),
            "区分": db.sanitize(kubun),
            "登録内容": db.sanitize(naiyou),
            "識別コード": db.sanitize(code),
            "直": db.sanitize(shift),
            "残業者": db.sanitize(overtime),
            "早出者": db.sanitize(early),
            "班": db.sanitize(group),
            "ライン": db.sanitize(line),
            config.COL_WRITER_GROUP: db.sanitize(writer_group),
            config.COL_REGISTERED_AT: db.sanitize(registered_at),
        }

        # ここで Access への送信予約を明示する必要はない。
        # dbkit.outbox_sync が「新しく増えた行」を次の送信タイミングで
        # 自動的に見つけて送る (calendar_app/sync/specs.py 参照)。
        result = sqlite_toolkit.insert_record(
            self.conn, config.TABLE_DATA, values, caller_name="Repository.save_record"
        )
        if not result.ok:
            raise sqlite3.OperationalError(result.error or "save_record に失敗しました")
        new_id = int(result.lastrowid)

        debug_log(
            f"Repository.save_record: 日付={values['日付']} 区分={kubun} "
            f"内容={naiyou} 班={group} ライン={line} ID={new_id}"
        )
        return new_id

    def delete_records_by_ids(self, ids: Sequence[int]) -> int:
        """指定 ID を削除し、削除履歴に記録する (VBA: DeleteRecordsByIds)。

        削除履歴への追記は他の追加操作と同じく outbox_sync に任せられるが、
        「休み管理からの削除」だけは Access へ明示的に転送する必要があるため
        ``sync.deletes.queue_delete`` で予約する
        (Access にまだ届いていない行なら、予約自体を行わない)。

        戻り値は実際に削除した件数。
        """
        if not ids:
            return 0

        operator = operator_name()
        stamp = db.now_string()
        deleted = 0
        forwarded = 0

        with self.conn:
            for record_id in ids:
                row = self.conn.execute(
                    f'SELECT * FROM "{config.TABLE_DATA}" WHERE "ID"=?', (record_id,)
                ).fetchone()
                if row is None:
                    debug_log(f"Repository.delete_records_by_ids: 対象なし ID={record_id}")
                    continue

                history = {
                    "削除日時": stamp,
                    "削除実行者": operator,
                    "対象日付": row["日付"],
                    "区分": row["区分"] or "",
                    "登録内容": row["登録内容"] or "",
                    "識別コード": row["識別コード"] or "",
                    "班": row["班"] or "",
                    "ライン": row["ライン"] or "",
                }
                columns = ", ".join(f'"{c}"' for c in history)
                placeholders = ", ".join("?" for _ in history)
                # 削除履歴への追記は outbox_sync が自動的に拾うため、ここでは
                # 素直に INSERT するだけでよい
                self.conn.execute(
                    f'INSERT INTO "{config.TABLE_DEL_HISTORY}" ({columns}) '
                    f"VALUES ({placeholders})",
                    tuple(history.values()),
                )

                # Access へ転送する必要があるか (未送信のローカル専用行なら不要)
                # を先に判定してから、実際に行を消す
                if queue_delete(self.conn, sync_specs.DATA_SPEC, row, row_id=record_id):
                    forwarded += 1

                self.conn.execute(
                    f'DELETE FROM "{config.TABLE_DATA}" WHERE "ID"=?', (record_id,)
                )
                deleted += 1
                debug_log(f"Repository.delete_records_by_ids: 削除完了 ID={record_id}")

        debug_log(
            f"Repository.delete_records_by_ids: 終了 削除件数={deleted} "
            f"(Access転送予約={forwarded}) 実行者={operator}"
        )
        return deleted

    # ------------------------------------------------------------------
    # 班員名簿 (VBA: modDB)
    # ------------------------------------------------------------------
    def get_workers(self, my_line: str) -> list[Worker]:
        """PC のライン設定で絞り込んだ作業者一覧 (VBA: Get作業者一覧)。

        * 作業長          : 担当ラインが空欄でない全員
        * LVC / L-1       : 同一人物が兼務するため双方を対象にする
        * それ以外        : PC のラインと完全一致する人のみ
        並び順は ライン → 読み。
        """
        rows = self.conn.execute(
            f'SELECT * FROM "{config.TABLE_MEMBER}"'
        ).fetchall()

        workers: list[Worker] = []
        for row in rows:
            line = (row["担当ライン"] or "").strip()
            if my_line == config.LINE_SUPERVISOR:
                target = bool(line)
            elif my_line in ("LVC", "L-1"):
                target = line in ("LVC", "L-1")
            else:
                target = line == my_line
            if not target:
                continue
            workers.append(
                Worker(
                    code=str(row["管理番号"] or "").strip(),
                    group=(row["班"] or "").strip(),
                    name=(row["名前"] or "").strip(),
                    line=line,
                    reading=(row["読み"] or "").strip(),
                )
            )

        workers.sort(key=lambda w: (w.line, w.reading, w.name))
        debug_log(
            f"Repository.get_workers: PCライン={my_line} 対象件数={len(workers)}"
        )
        return workers

    def get_line_group_pairs(self) -> list[tuple[str, str]]:
        """班員名簿に実在する「ライン・班」の組み合わせ (VBA: Getライン班ペア一覧)。"""
        rows = self.conn.execute(
            f'SELECT DISTINCT "担当ライン", "班" FROM "{config.TABLE_MEMBER}" '
            'WHERE "担当ライン" <> \'\' AND "班" <> \'\' '
            'ORDER BY "担当ライン", "班"'
        ).fetchall()
        return [(r["担当ライン"].strip(), r["班"].strip()) for r in rows]

    def member_count(self) -> int:
        """班員名簿の件数。設定画面が取り込み状況として出す。"""
        row = self.conn.execute(
            f'SELECT COUNT(*) AS cnt FROM "{config.TABLE_MEMBER}"'
        ).fetchone()
        return int(row["cnt"])

    def has_workers(self) -> bool:
        """班員名簿が取り込まれているか。"""
        return self.member_count() > 0

    def pending_sync_count(self) -> int:
        """Access へ未反映の変更件数 (未送信の追加 + 未転送の削除)。"""
        return total_pending_count(self.conn)


# ---------------------------------------------------------------------------
# 表示テキストの組み立て (VBA: modData の Build* 関数群)
# ---------------------------------------------------------------------------
def build_day_text(records: Iterable[DayRecord], my_line: str) -> str:
    """カレンダーセルに出す端的な表示テキスト (VBA: BuildDayText)。

    休み     -> 「L-1:B班:杉山良宏 休」 (1 件 1 行)
    コメント -> 「L-1:B班 コメント有」  (同じライン+班のコメントは 1 行にまとめる。
                班は**残した班**。前の版の行は「L-1:(班なし) コメント有」)

    PC のライン設定に応じて表示を絞り込む::

        作業長             : 休み・コメントとも全ライン表示
        コイル             : 休みはコイルのみ、コメントはコイル宛のみ
        コイル以外の各ライン : 休みはコイル以外全部、コメントは自分のライン宛のみ
    """
    lines: list[str] = []
    comment_heads: list[str] = []  # 重複を除きつつ登場順を保つ

    for rec in records:
        if rec.is_rest:
            if not record_shown(rec, my_line):
                continue
            head = f"{rec.line}:" if rec.line else ""
            if rec.group:
                head += f"{rec.group}班:"
            lines.append(f"{head}{rec.naiyou} 休")

        elif rec.kubun == config.KUBUN_OTHER:
            if not _comment_shown(rec, my_line):
                continue
            # 班は**残した班**(VER4.2.4)。前の版の行は宛先の班しか無いので、
            # 「からの連絡」と読ませないよう (班なし) にする
            head = f"{rec.line}:" if rec.line else ""
            head += f"{rec.writer_group}班" if rec.writer_group else NO_GROUP
            if head not in comment_heads:
                comment_heads.append(head)

    # コメントはライン+班ごとに「コメント有」だけを出す (詳細は内容閲覧で確認する)
    lines.extend(f"{head} コメント有" for head in comment_heads)
    return "\n".join(lines)


#: 連絡を残した班が分からない(前の版の)行に出す札
NO_GROUP = "(班なし)"
#: 保存した時刻が無い(前の版の)行に出す文
NO_TIME = "時刻の記録なし"


def _comment_shown(rec: DayRecord, my_line: str) -> bool:
    """その連絡をこの端末のマス目に出すか。作業長は全ライン、ほかは自分のライン宛だけ。"""
    return my_line == config.LINE_SUPERVISOR or rec.line == my_line


def record_shown(rec: DayRecord, my_line: str) -> bool:
    """その登録をこの端末に見せるか。**マス目・マウスを乗せたとき・内容閲覧・削除・印刷の決まりは1つ**(VER4.2.6)。

        作業長             : 休み・連絡とも全ライン
        コイル             : 休みはコイルだけ、連絡はコイル宛だけ
        コイル以外の各ライン : 休みはコイル以外全部、連絡は自分のライン宛だけ

    前はマス目と印刷だけがこれで絞り、マウスを乗せたときの休み・内容閲覧・削除の選択肢は全ラインを
    出していた(マス目に無いほかのラインの登録が見え、消せた)。
    """
    if rec.is_rest:
        if my_line == config.LINE_SUPERVISOR:
            return True
        if my_line == config.LINE_COIL:
            return rec.line == config.LINE_COIL
        return rec.line != config.LINE_COIL
    return _comment_shown(rec, my_line)


def comment_origin(rec: DayRecord) -> str:
    """連絡の見出し「L-1 B班からの連絡」(VER4.2.4)。

    **班は宛先ではなく、残した班。** 前の版の行は ``班`` に宛先として選んだ班
    しか無いので、それを「からの連絡」と出すと逆の意味に読める。書いた班が
    残っていない行は「L-1 連絡(班なし)」にする。
    """
    line = rec.line or "(所属不明)"
    if rec.writer_group:
        return f"{line} {rec.writer_group}班からの連絡"
    return f"{line} 連絡{NO_GROUP}"


def shown_time(stamp: str) -> str:
    """保存した時刻を画面に出す形「2026/10/21 14:05」に。秒は落とす。無ければ空。

    ``yyyy/mm/dd hh:mm:ss`` でないもの(別の手段で入った行)は、読める範囲だけ出す。
    """
    text = (stamp or "").strip()
    if not text:
        return ""
    for form in (config.DATETIME_FORMAT, "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                 "%Y/%m/%d %H:%M"):
        try:
            return _dt.datetime.strptime(text, form).strftime("%Y/%m/%d %H:%M")
        except ValueError:
            continue
    return text


def comment_when(rec: DayRecord) -> str:
    """連絡を残した時刻。前の版の行は「時刻の記録なし」。"""
    return shown_time(rec.registered_at) or NO_TIME


def shift_neighbours(shift: str) -> tuple[int, int] | None:
    """直番号から前直・次直を求める (1→3/2, 2→1/3, 3→2/1)。

    **繋ぎが誰の代わりなのかは、この2つで決まります。**
    休んだ直の**前の直が残業で**、**次の直が早出で**繋ぎます。
    閲覧・ツールチップ・登録の画面が同じ言い方をするよう、
    求め方はここ1つに置いてあります。
    """
    try:
        num = int(shift)
    except (TypeError, ValueError):
        return None
    prev = num - 1 if num - 1 >= 1 else 3
    nxt = num + 1 if num + 1 <= 3 else 1
    return prev, nxt


def build_day_tooltip(records: Sequence[DayRecord], my_line: str | None = None) -> str:
    """マスにマウスを乗せたときの文字列 (VBA: BuildDayTooltip)。

    * 休み … 残業/早出繋ぎ。複数人の休みがある場合は区切り線と番号を入れて見分けやすくする
    * 連絡 … どの班からといつ「L-1 B班からの連絡　2026/10/21 14:05」(VER4.2.4)。
      **マスに出すものと同じものだけ**(休みも連絡も ``record_shown`` で絞る。VER4.2.6。``None`` なら絞らない)
    """
    shown = [r for r in records if my_line is None or record_shown(r, my_line)]
    targets = [r for r in shown if r.is_rest and r.shift]
    comments = [r for r in shown if r.kubun == config.KUBUN_OTHER]
    if not targets and not comments:
        return ""

    blocks: list[str] = []
    for i, rec in enumerate(targets, start=1):
        neighbours = shift_neighbours(rec.shift)
        if neighbours is None:
            continue
        prev, nxt = neighbours
        overtime = rec.overtime or config.UNREGISTERED
        early = rec.early or config.UNREGISTERED

        head = f"{rec.line}:" if rec.line else ""
        if rec.group:
            head += f"{rec.group}班:"
        head += rec.naiyou
        # 複数件ある場合のみ番号を振り、どの休みの繋ぎ情報か分かるようにする
        if len(targets) > 1:
            head = f"[{i}] {head}"

        blocks.append(f"{head}\n  {prev}直残業　{overtime}\n  {nxt}直早出　{early}")

    if comments:
        blocks.append("\n".join(f"{comment_origin(rec)}　{comment_when(rec)}"
                                for rec in comments))

    return ("\n" + "-" * 20 + "\n").join(blocks)


def build_day_detail(records: Sequence[DayRecord]) -> str:
    """内容閲覧フォーム用の詳細テキスト (VBA: BuildDayDetail)。"""
    if not records:
        return "登録はありません。"

    blocks: list[str] = []
    for rec in records:
        if rec.is_rest:
            head = "[休み] "
            if rec.line:
                head += f"{rec.line} "
            if rec.group:
                head += f"{rec.group}班 "
            head += rec.naiyou
            if rec.shift:
                head += f" ({rec.shift}直)"

            neighbours = shift_neighbours(rec.shift) if rec.shift else None
            if neighbours:
                prev, nxt = neighbours
                overtime = rec.overtime or config.UNREGISTERED
                early = rec.early or config.UNREGISTERED
                head += f"\n  {prev}直残業: {overtime}\n  {nxt}直早出: {early}"
            blocks.append(head)
        else:
            # どの班からといつ (VER4.2.4)。前の版の行は宛先の班を出さない
            blocks.append(f"[連絡] {comment_origin(rec)}　{comment_when(rec)}"
                          f"\n  {rec.naiyou}")

    return ("\n" + "-" * 30 + "\n").join(blocks)
