"""共有保存の履歴 ── **各直で「共有へ保存」を押した時刻と担当者を残す**

    各直で共有保存を押したタイミングと担当者を履歴として残してください
    CSV出力とグラフ出力できるようにもしてください
    日報データ.sqlite3に列追加で保存がいい？列がなければ自動追加してください

【どこに残すか】

    手元  nippou_local.sqlite3 の push_history   押すたびに必ず書く
    共有  日報データ.sqlite3 の T_共有保存履歴     手元の行を写す(無ければ表ごと作る)

**既存の表(`T_日報ヘッダー_<ライン>` など)に列は足しません。** 理由は2つ:

    1. 列だと「最後に押した1回」しか持てない。押すたびに上書きされて、
       履歴になりません(同じ直で3回押したら、残るのは3回目だけ)
    2. 日報の表は Access 版の VBA も同じ名前で読み書きします。列が
       増えると、列名を書かずに INSERT している側や、列の並びで読んで
       いる側が壊れます

そこで**履歴専用の表を1つ**足します。無ければ作り、列が足りなければ
足します(`ensure_shared_table`)── 「列がなければ自動追加」はこの表で
守ります。全ラインが同じ表に書くので、ツールが無くても
`SELECT * FROM T_共有保存履歴 WHERE ライン='L-1'` で読めます。

**手元にも残すのは、共有に届かないときのため**です。共有が落ちていて
送れなかった、という記録こそ残したいのに、それを共有にしか書かない
のでは残せません。写し終えていない行(`shared=0`)は、次の「共有へ保存」
でまとめて写します。

【1回押すと何行になるか】
送った**直ごとに1行**。1回で2つの直(前の直の残り + いまの直)を送れば
2行です。送るものが無かった・関門で止めた、も残します ── 「押した」こと
自体が記録したいことなので。

    結果           いつ
    送れた         その直のページをぜんぶ共有へ送れた
    一部送れなかった  送れたページと送れなかったページがある
    送れなかった   1ページも送れなかった(共有が開けない・ロック など)
    関門で止めた   チェックに引っかかって送らなかった(直すところあり)
    送るもの無し   未送信が無かった(いまの直で1行)

**書き先が Access のときは共有へ写しません**(手元にだけ残す)。集計の
表と同じ決まりで、Access 側に表を増やさないためです。
"""
from __future__ import annotations

import socket
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from ..db.repository import NippouRepository
from ..logging_setup import get_logger

log = get_logger("services.push_history")

#: 共有の日報データ.sqlite3 に置く表(全ライン共通)
SHARED_TABLE = "T_共有保存履歴"

#: 共有の表の列(手元の列名 → 共有の列名)。**並びもこのまま作ります**
SHARED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("id", "ID"),
    ("pressed_at", "押した日時"),
    ("report_date", "報告日"),
    ("line", "ライン"),
    ("shift", "直"),
    ("pages", "ページ数"),
    ("worker", "担当者"),
    ("result", "結果"),
    ("failed_pages", "送れなかったページ数"),
    ("detail", "理由"),
    ("pressed_key", "押したときの直"),
    ("pressed_by", "押した人"),
    ("via", "通し方"),
    ("terminal", "端末"),
)

SENT = "送れた"
PARTIAL = "一部送れなかった"
FAILED = "送れなかった"
BLOCKED = "関門で止めた"
NOTHING = "送るもの無し"

#: 押したあと、その直の中身が共有に出ているか(グラフの塗りに使う)
REACHED = (SENT, NOTHING)


def terminal_name() -> str:
    try:
        return socket.gethostname()
    except OSError:                                # pragma: no cover - 名前が引けない端末
        return ""


@dataclass
class Pressed:
    """押したときの様子(送った直に関係なく同じ)。"""

    at: str                                  # ISO(秒まで)
    key_text: str = ""                       # 押したときの直(「2026年9月25日 2直」)
    by: str = ""                             # その直の担当者
    via: str = ""                            # 逃げ道の一文で通したとき
    terminal: str = field(default_factory=terminal_name)

    @classmethod
    def now(cls, repo: NippouRepository, current: tuple[str, str, str],
            *, via: str = "", at: Optional[datetime] = None,
            by: str = "") -> "Pressed":
        """押した人は、**押した画面に出ていた作業者**(`by`)。

        日報入力の「共有へ保存」は、そのとき欄に入っている作業者を
        添えて送ってきます。添えていない(設定画面・確認画面から押した)
        ときは、いまの直に保存してある作業者名で埋めます。
        """
        report_date, line, shift = current
        name = " ".join(str(by or "").split())[:40]
        if not name and report_date:
            name = "・".join(repo.shift_workers(report_date, line, shift))
        return cls(at=(at or datetime.now()).isoformat(timespec="seconds"),
                   key_text=f"{report_date} {shift}".strip(),
                   by=name, via=via)


def _row(pressed: Pressed, *, report_date: str, line: str, shift: str,
         pages: int, worker: str, result: str, failed_pages: int = 0,
         detail: str = "") -> dict:
    return {"id": uuid.uuid4().hex, "pressed_at": pressed.at,
            "report_date": report_date, "line": line, "shift": shift,
            "pages": pages, "worker": worker, "result": result,
            "failed_pages": failed_pages, "detail": detail,
            "pressed_key": pressed.key_text, "pressed_by": pressed.by,
            "via": pressed.via, "terminal": pressed.terminal}


def _worker(repo: NippouRepository, report_date: str, line: str, shift: str) -> str:
    return "・".join(repo.shift_workers(report_date, line, shift))


def _backfill_note(repo: NippouRepository, report_date: str, line: str,
                   shift: str) -> str:
    """その直に後から作ったページがあれば「後日作成(…)」の一文(v4.0.0)。"""
    from ..logic import backfill as backfill_rule
    try:
        found = repo.backfills(report_date, line, shift)
    except Exception:                             # noqa: BLE001 - 一文が出ないだけ
        return ""
    if not found:
        return ""
    pages = "・".join(str(key[3]) for key in sorted(found))
    first = found[sorted(found)[0]]
    return f"ページ{pages}は{backfill_rule.stamp_text(first)}"


def record_push(repo: NippouRepository, pressed: Pressed, summary,
                current: tuple[str, str, str]) -> list[dict]:
    """送り終えたところで残す。**送った直ごとに1行。**"""
    by_shift: dict[tuple[str, str, str], dict] = {}
    for key in summary.succeeded:
        slot = by_shift.setdefault(tuple(key[:3]), {"ok": 0, "ng": 0, "why": []})
        slot["ok"] += 1
    for outcome in summary.failed:
        slot = by_shift.setdefault(tuple(outcome.key[:3]), {"ok": 0, "ng": 0, "why": []})
        slot["ng"] += 1
        error = outcome.error
        why = str(getattr(error, "message", "") or error or "").strip()
        if why and why not in slot["why"]:
            slot["why"].append(why)

    rows = []
    for (report_date, line, shift), slot in sorted(by_shift.items()):
        if slot["ng"] == 0:
            result = SENT
        elif slot["ok"] == 0:
            result = FAILED
        else:
            result = PARTIAL
        # 後から作ったページを含む直は**そう書く**(共有の履歴を見る人にも分かる)
        made_later = _backfill_note(repo, report_date, line, shift)
        rows.append(_row(pressed, report_date=report_date, line=line, shift=shift,
                         pages=slot["ok"] + slot["ng"],
                         worker=_worker(repo, report_date, line, shift),
                         result=result, failed_pages=slot["ng"],
                         detail=" / ".join([*([made_later] if made_later else []),
                                            *slot["why"]])))
    if not rows:
        # 送るものが無かった。**押したことは残す**(いまの直で1行)
        report_date, line, shift = current
        rows.append(_row(pressed, report_date=report_date, line=line, shift=shift,
                         pages=0, worker=_worker(repo, report_date, line, shift),
                         result=NOTHING))
    repo.add_push_history(rows)
    return rows


def record_blocked(repo: NippouRepository, pressed: Pressed,
                   reports: Iterable) -> list[dict]:
    """関門で止めたときも残す。**止められた直ごとに1行。**"""
    rows = []
    for report in reports:
        if report.ok:
            continue
        first = report.findings[0].message if report.findings else ""
        more = f"(ほか{len(report.findings) - 1}件)" if len(report.findings) > 1 else ""
        rows.append(_row(pressed, report_date=report.report_date, line=report.line,
                         shift=report.shift, pages=report.pages,
                         worker=_worker(repo, report.report_date, report.line,
                                        report.shift),
                         result=BLOCKED, detail=f"{first}{more}"))
    repo.add_push_history(rows)
    return rows


# ------------------------------------------------------------------
# 共有の T_共有保存履歴
# ------------------------------------------------------------------
def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def ensure_shared_table(conn: sqlite3.Connection) -> list[str]:
    """表が無ければ作り、**列が足りなければ足す。** 足した列の名前を返す。

    列を足すのは、この先この表に列を増やしたとき、先に作られた共有の
    表でも書けるようにするためです(`ALTER TABLE … ADD COLUMN`。既存の行は
    空欄のまま残ります)。
    """
    names = [shared for _, shared in SHARED_COLUMNS]
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {_quote(SHARED_TABLE)} ("
        + ", ".join(_quote(n) for n in names)
        + f", PRIMARY KEY ({_quote('ID')}))")
    have = {r[1] for r in conn.execute(f"PRAGMA table_info({_quote(SHARED_TABLE)})")}
    added = [n for n in names if n not in have]
    for name in added:
        conn.execute(f"ALTER TABLE {_quote(SHARED_TABLE)} ADD COLUMN {_quote(name)}")
    if added:
        log.info("%s に列を足しました: %s", SHARED_TABLE, ", ".join(added))
    return added


def share(repo: NippouRepository, db_path: Path) -> int:
    """手元の、まだ写していない行を共有へ写す。写した数を返す。

    **失敗しても何も止めない**(共有への保存の結果は別に返っている)。
    写せなかった行は `shared=0` のまま残り、次の「共有へ保存」で写します。
    """
    from ..access_bridge import pusher, sqlite_backend

    if not pusher.is_sqlite_target(db_path):
        return 0
    if not Path(db_path).exists():
        # **ファイルを作らない。** 開くだけで空のDBができてしまう ── 共有に
        # 届いていないのに、手元や見当違いの場所に日報データができる
        return 0
    rows = repo.push_history(unshared_only=True)
    if not rows:
        return 0
    cols = [shared for _, shared in SHARED_COLUMNS]
    try:
        conn = sqlite_backend._connect(Path(db_path))
    except sqlite3.Error as exc:
        log.warning("共有保存の履歴を写せません(%s を開けない): %s", db_path, exc)
        return 0
    try:
        with conn:
            ensure_shared_table(conn)
            conn.executemany(
                f"INSERT OR REPLACE INTO {_quote(SHARED_TABLE)} "
                f"({', '.join(_quote(c) for c in cols)}) "
                f"VALUES ({', '.join('?' for _ in cols)})",
                [tuple(row[local] for local, _ in SHARED_COLUMNS) for row in rows])
    except sqlite3.Error as exc:
        log.warning("共有保存の履歴を写せませんでした(次の保存で写します): %s", exc)
        return 0
    finally:
        conn.close()
    repo.mark_push_history_shared([row["id"] for row in rows])
    return len(rows)


# ------------------------------------------------------------------
# 読む(CSV・グラフ・表)
# ------------------------------------------------------------------
@dataclass
class Readout:
    rows: list[dict]
    #: どこから読んだか(画面に出す一言)
    source: str
    #: 共有を読めなかったとき、その理由
    warning: str = ""


def _read_shared(db_path: Path) -> list[dict]:
    """共有の表を手元の列名で読む。表が無ければ空。"""
    from ..access_bridge import sqlite_backend

    if not Path(db_path).exists():
        return []
    conn = sqlite_backend._connect(Path(db_path))
    try:
        conn.row_factory = sqlite3.Row
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (SHARED_TABLE,)).fetchone()
        if not exists:
            return []
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({_quote(SHARED_TABLE)})")}
        out = []
        for r in conn.execute(f"SELECT * FROM {_quote(SHARED_TABLE)} ORDER BY rowid"):
            out.append({local: (r[shared] if shared in have else "")
                        for local, shared in SHARED_COLUMNS})
        return out
    finally:
        conn.close()


def read(repo: NippouRepository, db_path: Optional[Path]) -> Readout:
    """履歴をぜんぶ読む。**共有の表 + 手元のまだ写していない行。**

    共有にはほかの端末(ほかのライン)の行も入っています。共有を読めない
    ときや書き先が Access のときは、この端末の記録だけを返します。
    """
    from ..access_bridge import pusher

    local = repo.push_history()
    if db_path is None or not pusher.is_sqlite_target(db_path):
        return Readout(local, "この端末の記録(書き先が Access のため共有には写していません)")
    if not Path(db_path).exists():
        return Readout(local, f"この端末の記録だけ(共有の {Path(db_path).name} が"
                              "まだ無い・見つからないため)")
    try:
        shared = _read_shared(db_path)
    except sqlite3.Error as exc:
        return Readout(local, "この端末の記録だけ",
                       warning=f"共有の {SHARED_TABLE} を読めませんでした: {exc}")
    merged = {row["id"]: row for row in shared}
    for row in local:
        merged.setdefault(row["id"], row)      # 写していない行はこちらにだけある
    # 押した時刻の順。同じ秒のものは書いた順のまま(並べ替えは安定)
    rows = sorted(merged.values(), key=lambda r: str(r["pressed_at"]))
    return Readout(rows, f"共有の {SHARED_TABLE}(全端末) + この端末のまだ写していない行")
