"""共有の日報データを**読むだけ** ── ロットを探す・紙・下見

    ロットを探す 探す範囲: 保存した日報の行です(打ちかけの自動保存も含みます)。
    画面に打っただけでまだ保存されていない行は出ません。:
    ローカル保存した範囲だけですか?
    んーLOT検索用にDB作ってもいいぐらいだと思ったんですが....
    もしくは集計用CSVにLOT検索もこなせるように追加LOTNOを出しておくとか

【検索用の DB は作らない】
全ライン・全 PC の行は、もう**共有の日報データ**(`日報データ.sqlite3`。設定の
`access_db_path`)に集まっています ── 各 PC が「共有へ保存」で
`T_日報ヘッダー_<ライン>` / `T_日報明細_<ライン>` へ送っているので。検索用に
もう1つ DB を作ると、送るたびに2か所へ書くことになり、片方だけ古くなる芽を
作ります。ここはその表を**読むだけ**です(書く口を作らない)。

【開き方 ── 読むだけ・作らない】
`db.connection.connect_readonly`(`?mode=ro`)で開きます。控え(`local_backup.reader`)・
標準作業時間を読むのと同じ開き方で、**無いファイルを作らず、書けません。**
ファイルが無ければ開く前に断ります(フォルダごと見えなければ「届かない」)。

`source_db`(手元へ写して読む)にしないのは、日報データは全ラインの PC が送るたびに
大きさが変わるからです ── 写しの控えが効かず、探すたびにファイルを丸ごと写すことに
なります。ここは当たる行だけを SQL で絞って持ってきます。

【待たせすぎない ── 時間と件数】
読むのは**別の糸**で、`SECONDS` 秒で見切ります。共有のフォルダに届かないときは、
ファイルを確かめるだけで何十秒も止まることがあるので。問い合わせも同じ期限で
止めます(`set_progress_handler`)。当たった行は `CAP` 行で読むのをやめます
(`truncated`)。どちらでも呼ぶ側は**探すのをやめず**、手元のぶんで続けます。

【Access のとき】
読みません。既存の Access の読み方(VBScript + ADODB を cscript で動かし、表を CSV に
書き出して読む `access_bridge.importer`)は、ラインごとに cscript を立ち上げて表を
**丸ごと**書き出すことになります ── ロット№の比べ方(全角・小文字を揃えてから頭を
比べる)は Access の SQL では書けないので、絞れません。全ラインだと何十秒もかかり、
共有の `.laccdb` も握ります。後日作成の確かめ(`backfill`)・コンバートと同じ線引きで、
呼ぶ側が「共有が Access なので、この端末と控えの分だけ探しました」と言います。

【前の名前の表も読む】
VBA は前の名前の表(`T_日報ヘッダー_LS`)に書き続けます(`services/line_rename`)。
コンバートを待たずに見えるよう、前と正規で字が違うラインは前の表も読み、中の
「ライン」を正規へ読み替えます。同じページが両方にあれば**正規の表**を採ります
(このツールが書いたほう。コンバートの「このツールの分は残す」と同じ向き)。
ページ・行番号は数に揃えます(VBA は字で入れていることがある)。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Optional

from ..db.models import DetailRecord, HeaderRecord
from ..logging_setup import get_logger
from ..logic import line_names
from ..logic.line_rename import number_of
from ..logic.shift import parse_business_date

log = get_logger("services.shared_reports")

#: 共有を読むのに待つ秒数(開くのも問い合わせも、まとめてこの中で)
SECONDS = 8.0
#: 共有から持ってくる当たりの行の上限。**画面の上限(200行)の10倍** ──
#: ここで切れたら、どのみち「絞ってください」と言うことになる
CAP = 2000

OK = "ok"
ACCESS = "access"
MISSING = "missing"
UNREACHABLE = "unreachable"
TIMEOUT = "timeout"
ERROR = "error"

PageKey = tuple[str, str, str, int]
Page = tuple[HeaderRecord, list[DetailRecord]]

#: 明細の列(共有の名前 → 手元の名前)。**共有の列はこの名前で作られている**
#: (`access_bridge.sqlite_backend.DETAIL_COLUMNS`)。無い列は空で読む(古い表)
_DETAIL_FIELDS: tuple[tuple[str, str], ...] = tuple(
    (name, "keisu" if name == "係数" else name.lower())
    for name in ("LOT", "ZAI", "SIZ", "KEN", "KZ", "KH", "SZ", "SH", "HIT", "AI",
                 "MAI", "TUT", "VC", "ET", "S", "TH", "SS", "THS", "STH", "THT",
                 "CON", "WEI", "TIM", "UNI",
                 "Others1", "Others2", "Others3", "Others4", "Others5", "Others6", "係数",
                 "S4", "TH4", "S5", "TH5"))


class _TimedOut(Exception):
    """期限までに読み終わらなかった。"""


@dataclass
class Readout:
    """共有を読んだ結果。**読めなくても例外にしない**(呼ぶ側は手元で続ける)。"""

    status: str
    path: Path
    #: 当たった行(ロットを探す)。並びは決めていない
    rows: list[tuple[HeaderRecord, DetailRecord]] = field(default_factory=list)
    #: その直のページ(紙・下見)。ページの順
    pages: list[Page] = field(default_factory=list)
    #: `CAP` で読むのをやめた
    truncated: bool = False
    #: 読めなかったときの元の言葉(画面には「詳しく」として添える)
    detail: str = ""
    seconds: float = SECONDS

    @property
    def ok(self) -> bool:
        return self.status == OK

    def reason(self) -> str:
        """読まなかった・読めなかったわけ。**「〜ので」まで**(後ろは呼ぶ側が続ける)。"""
        name = self.path.name
        if self.status == ACCESS:
            return f"共有の日報データ({name})が Access なので"
        if self.status == MISSING:
            return f"共有の日報データ({name})がまだ無いので"
        if self.status == UNREACHABLE:
            return f"共有の日報データの置き場所({self.path.parent})に届かないので"
        if self.status == TIMEOUT:
            return f"共有の日報データを読むのに {self.seconds:g}秒を超えたので"
        if self.status == ERROR:
            return f"共有の日報データ({name})を読めなかったので"
        return ""


def default_path() -> Path:
    from ..config import SETTINGS

    return Path(SETTINGS.access_db_path)


def _text(value: object) -> str:
    """共有の値を字に(Access 経路と同じ揃え方 ── `importer._as_text`)。"""
    from ..access_bridge.importer import _as_text

    return _as_text(value)


def _tables(line: str) -> list[tuple[str, str]]:
    """そのラインの(見出しの表, 明細の表)。**正規が先**、前の名前の表があとから。"""
    from ..access_bridge import pusher

    out = [(pusher.header_table_name(line), pusher.detail_table_name(line))]
    found = line_names.by_official(line)
    if found is not None and found.renamed:
        out.append((pusher.header_table_name(found.old), pusher.detail_table_name(found.old)))
    return out


def _header(row: sqlite3.Row, names: set[str]) -> HeaderRecord:
    get = (lambda col: _text(row[col]) if col in names else "")
    saved_at = get("保存日時")
    return HeaderRecord(
        report_date=get("報告日"), line=line_names.upgrade(get("ライン")), shift=get("直"),
        page=number_of(row["ページ"]) if "ページ" in names else 1,
        worker=get("担当者"), day_shift=get("昼勤"), count=get("枚数"),
        weight_kg=get("重量Kg"), lot_count=get("Lot数"),
        coefficient_lot_count=get("係数Lot数"), reason=get("理由"),
        # 共有にあるのは送ったもの。「未送信」ではない
        saved_at=saved_at, dirty=False, synced_at=saved_at or None)


def _detail(row: sqlite3.Row, names: set[str], header: HeaderRecord) -> DetailRecord:
    kwargs = {field_name: (_text(row[col]) if col in names else "")
              for col, field_name in _DETAIL_FIELDS}
    return DetailRecord(report_date=header.report_date, line=header.line,
                        shift=header.shift, page=header.page,
                        row_no=number_of(row["行番号"]), **kwargs)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    from ..access_bridge.sqlite_backend import quote

    return {r[1] for r in conn.execute(f"PRAGMA table_info({quote(table)})")}


def _usable(value: object) -> bool:
    """ページ・行番号が数に揃ったか(揃わない行は、紙の鍵にならないので読まない)。"""
    return isinstance(value, int)


def _within(seconds: float, work: Callable[[float], object]) -> object:
    """`work(期限)` を別の糸で動かし、`seconds` 秒で見切る。"""
    deadline = time.monotonic() + seconds
    box: dict[str, object] = {}

    def run() -> None:
        try:
            box["value"] = work(deadline)
        except BaseException as exc:              # noqa: BLE001 - 呼んだ側で投げ直す
            box["error"] = exc

    worker = threading.Thread(target=run, name="shared-reports", daemon=True)
    worker.start()
    worker.join(seconds + 0.5)
    if worker.is_alive():
        # 置いていく。期限を過ぎた問い合わせは `set_progress_handler` が止める
        raise _TimedOut()
    if "error" in box:
        raise box["error"]                        # type: ignore[misc]
    return box.get("value")


def _open(path: Path, deadline: float) -> sqlite3.Connection:
    from ..db.connection import connect_readonly

    conn = connect_readonly(path, timeout=max(0.5, deadline - time.monotonic()))
    # 期限を過ぎたら問い合わせを止める(`sqlite3.OperationalError: interrupted`)
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 2000)
    return conn


def _read(path: Path, seconds: float,
          work: Callable[[sqlite3.Connection], None], readout: Readout) -> Readout:
    """共通の関門(Access・無い・届かない・時間切れ・読めない)。"""
    from ..access_bridge import pusher

    readout.seconds = seconds
    if not pusher.is_sqlite_target(path):
        readout.status = ACCESS
        return readout

    def run(deadline: float) -> None:
        try:
            exists = path.is_file()
            reachable = exists or path.parent.is_dir()
        except OSError:
            exists = reachable = False
        if not exists:
            readout.status = MISSING if reachable else UNREACHABLE
            return
        conn = _open(path, deadline)
        try:
            work(conn)
        finally:
            conn.close()
        readout.status = OK

    try:
        _within(seconds, run)
    except _TimedOut:
        log.warning("共有の日報データを %s秒で読み終われませんでした: %s", seconds, path)
        return Readout(status=TIMEOUT, path=path, seconds=seconds)
    except sqlite3.OperationalError as exc:
        if "interrupted" in str(exc).lower():
            log.warning("共有の日報データを %s秒で読み終われませんでした: %s", seconds, path)
            return Readout(status=TIMEOUT, path=path, seconds=seconds)
        log.warning("共有の日報データを読めませんでした %s: %s", path, exc)
        return Readout(status=ERROR, path=path, detail=str(exc), seconds=seconds)
    except Exception as exc:                      # noqa: BLE001 - 手元で続ける
        log.warning("共有の日報データを読めませんでした %s: %s", path, exc)
        return Readout(status=ERROR, path=path, detail=str(exc), seconds=seconds)
    return readout


# ----------------------------------------------------------------------
# ロットを探す
# ----------------------------------------------------------------------
def lot_rows(lines: list[str], start: date, end: date, match: Callable[[str], bool], *,
             path: Optional[Path] = None, seconds: Optional[float] = None,
             cap: Optional[int] = None) -> Readout:
    """期間(報告日・両端含む)の、`match` に当たるロット№の明細行と、その行のページの見出し。

    比べ方(`match`)は呼ぶ側が渡します(全角・小文字を揃えてから頭を比べるので、
    SQL の `LIKE` では書けない)。SQL の中で呼ぶので、当たらない行は持ってきません。
    """
    from ..access_bridge.sqlite_backend import quote

    path = Path(path or default_path())
    seconds = SECONDS if seconds is None else seconds
    cap = CAP if cap is None else cap
    readout = Readout(status=OK, path=path)

    def in_span(text: object) -> int:
        day = parse_business_date(_text(text))
        return 1 if day is not None and start <= day <= end else 0

    def hit(text: object) -> int:
        try:
            return 1 if match(_text(text)) else 0
        except Exception:                         # noqa: BLE001 - 比べられない字は外す
            return 0

    def work(conn: sqlite3.Connection) -> None:
        conn.create_function("in_span", 1, in_span, deterministic=True)
        conn.create_function("lot_hit", 1, hit, deterministic=True)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for line in lines:
            taken: set[PageKey] = set()          # 正規の表で見たページ
            for header_table, detail_table in _tables(line):
                if header_table not in tables or detail_table not in tables:
                    continue
                head_cols = _columns(conn, header_table)
                headers: dict[PageKey, HeaderRecord] = {}
                for r in conn.execute(f"SELECT * FROM {quote(header_table)}"
                                      ' WHERE in_span("報告日")'):
                    header = _header(r, head_cols)
                    if header.line != line or not _usable(header.page):
                        continue
                    if header.key() not in taken:
                        headers[header.key()] = header
                if not headers:
                    continue
                detail_cols = _columns(conn, detail_table)
                left = cap + 1 - len(readout.rows)
                for r in conn.execute(
                        f"SELECT * FROM {quote(detail_table)}"
                        ' WHERE in_span("報告日") AND lot_hit("LOT") LIMIT ?', (left,)):
                    key = (_text(r["報告日"]), line_names.upgrade(_text(r["ライン"])),
                           _text(r["直"]), number_of(r["ページ"]))
                    header = headers.get(key)
                    if header is None or not _usable(number_of(r["行番号"])):
                        continue
                    readout.rows.append((header, _detail(r, detail_cols, header)))
                taken.update(headers)
                if len(readout.rows) > cap:
                    readout.truncated = True
                    del readout.rows[cap:]
                    return

    return _read(path, seconds, work, readout)


# ----------------------------------------------------------------------
# 紙・下見(その直のページ)
# ----------------------------------------------------------------------
def shift_pages(report_date: str, line: str, shift: str, *,
                path: Optional[Path] = None, seconds: Optional[float] = None) -> Readout:
    """その直の全ページ(見出し + 明細)。**手元にも控えにも無い直**の紙と下見に使う。"""
    from ..access_bridge.sqlite_backend import quote

    path = Path(path or default_path())
    seconds = SECONDS if seconds is None else seconds
    readout = Readout(status=OK, path=path)

    def work(conn: sqlite3.Connection) -> None:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        found: dict[int, Page] = {}
        for header_table, detail_table in _tables(line):
            if header_table not in tables or detail_table not in tables:
                continue
            head_cols = _columns(conn, header_table)
            headers: dict[int, HeaderRecord] = {}
            for r in conn.execute(f'SELECT * FROM {quote(header_table)}'
                                  ' WHERE "報告日"=? AND "直"=?', (report_date, shift)):
                header = _header(r, head_cols)
                if header.line == line and _usable(header.page) and header.page not in found:
                    headers[header.page] = header
            if not headers:
                continue
            detail_cols = _columns(conn, detail_table)
            rows: dict[int, list[DetailRecord]] = {page: [] for page in headers}
            for r in conn.execute(f'SELECT * FROM {quote(detail_table)}'
                                  ' WHERE "報告日"=? AND "直"=?', (report_date, shift)):
                page = number_of(r["ページ"])
                if (page in headers and line_names.upgrade(_text(r["ライン"])) == line
                        and _usable(number_of(r["行番号"]))):
                    rows[page].append(_detail(r, detail_cols, headers[page]))
            for page, header in headers.items():
                found[page] = (header, sorted(rows[page], key=lambda d: d.row_no))
        readout.pages = [found[page] for page in sorted(found)]

    return _read(path, seconds, work, readout)
