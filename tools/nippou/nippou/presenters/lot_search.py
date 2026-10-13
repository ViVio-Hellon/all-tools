"""記録を見る「ロットを探す」 (v4.33.0)

    集計機能は充実してきたが、履歴機能としてはどうですか?
    期間を絞ってロットNo検索したい場合に何を検索したらいいですか?

    「ロットを探す」を作ってください。CSV に出す機能も付けます

【いままでの探し方は4手だった】
専用の探し方が無く、集計管理 → 期間 → ロット一覧 → Ctrl+F → 作業日・直・
ページを読んで記録を見る → 日付を指定して見る、と画面をまたいで4手かかって
いました。ここは**期間とロット№(頭の数桁だけでも)を入れて1回**で、当たった
行から「紙を見る」「下見」「呼び出す」(管理者)へそのまま行けるようにします。

【出どころは保存した日報の明細(`daily_header` / `daily_detail`)】
集計(`packing_report_detail`)ではなく、打ったほうから読みます。理由は5つ:

    1. 「紙を見る」「下見」「呼び出す」の鍵は (報告日, ライン, 直, ページ)。
       明細はちょうどその鍵で持っていて、当たった行がそのまま押す先になる
       (集計は作業日・直のあとに `report_id` を挟むので、引き直しが要る)
    2. **作業者はページごと**(`daily_header.worker`)。集計の直の行は1ページ目の
       名前だけなので、途中で交代した直だと別の人が出る
    3. 打ちかけ: 自動保存・ページを移る前に置いた打ちかけは明細に入っているので
       **出ます**。集計は「読むときにまだ無ければ作る」(`summary.fill_missing`)
       ので、探すだけで書き込みが起きる ── 明細から読めば何も書きません。
       画面にだけあって**まだ置かれていない行は、どちらにも無いので出ません**
       (画面にそう書く)
    4. 控え(LocalBackup)にあるのは日報の見出しと明細だけ(集計は無い)。共有の
       日報データも同じ見出しと明細の形(VBA の行もある)。どこから読んでも同じ形
    5. 出る字が**打ったまま**(紙と同じ)。集計は値なので「08」が 8 になる

【探す範囲 ── 共有の日報データ + この端末(探す範囲を広げた)】

    ロットを探す 探す範囲: 保存した日報の行です …
    ローカル保存した範囲だけですか?
    んーLOT検索用にDB作ってもいいぐらいだと思ったんですが....

最初はこの端末の手元の DB だけでした。全ライン・全 PC の行は**共有の日報データ**
(各 PC が「共有へ保存」で送った `T_日報ヘッダー_<ライン>` / `T_日報明細_<ライン>`)
にもう集まっているので、検索用の DB は作らず、そこを読みます(`services/shared_reports`。
読むだけ・時間と件数に上限):

    共有の日報データ   全 PC が送った行                         「どこから」= 共有
    この端末の手元     まだ送っていない行(打ちかけ・直したて)    = この端末・未送信

**同じページ(報告日・ライン・直・ページ)が両方にあれば手元を採ります。** 手元のほうが
新しい(送ったあとに直した・打ち足した)ので。行ではなくページで決めるのは、手元で
行を消した・ロット№を打ち直したページの古い行が、共有から出てこないようにするためです。

共有を読めないとき(Access・まだ無い・届かない・時間切れ)は**探すのをやめず**、
これまでどおり手元と控えで探し、そのわけを画面に出します(`notice`)。控えを読むのは
ほかのラインで、Administrator の PC か管理者モードのときだけ(記録を見るの控えと同じ決まり)。
ほかの PC がまだ送っていない行は、共有を読めたときは出ません(その PC が送れば出る)。

【一致のしかた】
**前方一致**(頭の数桁)。打った字も、保存してある字も、日報入力のロット欄と
同じ直し方(`input_rules.normalize("LOT", …)` ── 全角→半角・小文字→大文字・
空白などを落とす・7桁まで)をしてから比べます。`ｎ７１` でも ` N71 ` でも
`N7131T0` に当たります。**空欄は断ります** ── 期間の全部を出すと、上限に
当たって何も分からないまま「多すぎる」と言うだけになるので。

【期間の既定は直近3か月(2か月前の月初〜当日)】
集計管理の既定(月初〜当日)ではなく広めにします。ロットを探しに来るのは
「先月流したあのロット」を聞かれたときが多く、月初〜当日だと**月の頭の数日は
先月のぶんが1行も入りません**。かといって1年にすると、頭の2〜3桁では上限に
当たりやすい ── 3か月なら、ふつうは1画面に収まります。広げたければ欄で変えます。

【上限と並び】
**作業日 → 直(1直・2直・3直・日勤の決まった順)→ ページ → 行**。紙をめくる順で、
集計管理のロット一覧と同じです。直をまたいだロットは、またいだ直の行が全部
続いて出ます。上限は `LIMIT` 行。超えたら**先の `LIMIT` 行だけ**出し、
「多すぎるので絞ってください」と言います(CSV も画面と同じ行)。
共有から読むのは `shared_reports.CAP` 行・`shared_reports.SECONDS` 秒まで(超えたら
そこでやめて、そう言います ── 画面を待たせすぎない)。

【ライン ── 見るのは誰でも、直すのは今の決まりのまま】
共有には全ラインがあるので、ラインの欄は**全員に出し**ます。既定はこの端末のライン、
選べるのは定義のライン(`logic/line_names`)と「全ライン」。表に「ライン」の列があるので、
全ラインで混ざっても読めます。

ほかのラインを**見る**(探す・紙・下見)のは誰でもできます。読むだけで、何も書きません。
共有の日報データは全ラインの PC が同じファイルに書いていて、共有保存の履歴
(`services/push_history`)もほかのラインの行を誰にでも見せています。

**直す(呼び出す)のは今の決まりのまま**です: 管理者モードか、アクセス権限の表で
Administrator の PC(`ctx.editor`)だけ。行き先はこの端末のラインで手元にあるページなら
手元から、それ以外(ほかのライン・手元に無いページ)は控えから入れて開きます。
ほかのラインの控えを読むのも今のまま `ctx.editor` だけです。

集計管理のラインの欄(`services/line_view` ── Administrator の PC だけ)とは決まりが
違います。あちらは集計を控えから作り直して見せるもの、こちらは共有の行を読むだけです。

**判断はここだけ**で、画面(`views/records.js`)は受け取った行とボタンを並べるだけです。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from ..db.models import DetailRecord, HeaderRecord
from ..db.repository import NippouRepository
from ..logging_setup import get_logger
from ..logic import input_rules, line_names
from ..logic.aggregation import shift_sort_key
from ..logic.shift import parse_business_date
from ..services import shared_reports
from .agg_admin import Table

log = get_logger("presenters.lot_search")

#: 画面に出す行の上限。**1画面で目で追える数**にしてあります ── 3か月で頭の
#: 3〜4桁なら、ふつうは数十行。これを超えるのは桁が足りないときです
LIMIT = 200

#: 既定の期間(か月)。いまの月を入れて数える(10月13日なら 8月1日〜10月13日)
DEFAULT_MONTHS = 3

#: 「呼び出す」の行き先。**この端末のラインで手元にあるページは手元から、ほかは控えから**
#: (控えの直は手元へ入れてから開く。どちらも記録を見るの既存の道)
RECALL_LOCAL = "/api/settings/recall"
RECALL_BACKUP = "/api/records/backup/open"

#: ラインの欄の「全ライン」
ALL_LINES = "*"
ALL_LABEL = "全ライン"

#: 「どこから」の字。**共有 / この端末・未送信** が基本で、共有を読めなかったときだけ
#: 手元の送り済みと控えが出ます
SOURCE_SHARED = "共有"
SOURCE_UNSENT = "この端末・未送信"
SOURCE_LOCAL = "この端末"
SOURCE_BACKUP = "控え"
SOURCE_BACKUP_UNSENT = "控え・未送信"

#: 表の列。(鍵, 見出し, 数字の列か)。見出しは集計管理のロット一覧と同じ言葉
COLUMNS: tuple[tuple[str, str, bool], ...] = (
    ("report_date", "作業日", False),
    ("line", "ライン", False),
    ("shift", "直", False),
    ("page", "ページ", True),
    ("row_no", "行", True),
    ("lot", "ロット№", False),
    ("zai", "材・調質", False),
    ("siz", "厚×幅×丈", False),
    ("ken", "検入枚数", True),
    ("start", "開始", False),
    ("end", "終了", False),
    ("con", "実績枚数", True),
    ("wei", "実績重量", True),
    ("worker", "作業者", False),
    ("others2", "用途名", False),
    ("others3", "納入先", False),
    # どこから読んだ行か(もとは「共有へ」済/未)。**未送信は打ちかけ・直したてのこともある**
    ("source", "どこから", False),
)

TITLE = "ロットを探す"


class BadRequest(ValueError):
    """画面に返す断り。`field` は直してほしい欄。"""

    def __init__(self, message: str, field: str = "", status: int = 422) -> None:
        super().__init__(message)
        self.field = field
        self.status = status


def default_span(today: date) -> tuple[date, date]:
    """既定の期間。**(DEFAULT_MONTHS - 1) か月前の月初〜当日**(理由は冒頭)。"""
    first = today.replace(day=1)
    for _ in range(DEFAULT_MONTHS - 1):
        first = (first - timedelta(days=1)).replace(day=1)
    return first, today


def tidy_lot(text: object) -> str:
    """ロット№を比べる形に。**日報入力のロット欄と同じ直し方**(`input_rules.normalize`)。"""
    return input_rules.normalize("LOT", str(text or "").strip())


def known_lines(own_line: str) -> list[str]:
    """選べるライン。**定義の表の順**で、この端末のラインが表に無ければ先に足す。"""
    lines = line_names.codes()
    if own_line and own_line not in lines:
        lines.insert(0, own_line)
    return lines


def line_label(line: str) -> str:
    return ALL_LABEL if line == ALL_LINES else line_names.label(line)


def pick_line(asked: object, own_line: str) -> str:
    """探すライン。空ならこの端末のライン。**欄に無い名前は断る**(黙ってほかを探さない)。"""
    text = str(asked or "").strip()
    if not text:
        return own_line
    if text == ALL_LINES or text in known_lines(own_line):
        return text
    raise BadRequest(f"ライン「{text}」は選べません。欄の中から選んでください", "line")


def _date(value: object, name: str) -> date:
    text = str(value or "").strip()
    if not text:
        raise BadRequest("開始日と終了日を入れてください", name, 400)
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise BadRequest("日付は yyyy-mm-dd の形式で入れてください", name, 400) from exc


@dataclass(frozen=True)
class LotQuery:
    """探す条件。**作るときに確かめる**(おかしければ `BadRequest`)。"""

    lot: str
    start: date
    end: date
    #: 1つのラインか `ALL_LINES`(`pick_line` で決めたもの)
    line: str

    @classmethod
    def of(cls, payload: dict, line: str) -> "LotQuery":
        """画面から来た条件。`line` は `pick_line` で決めたもの(ここでは選ばない)。"""
        raw = str(payload.get("lot") or "").strip()
        lot = tidy_lot(raw)
        if not raw:
            raise BadRequest("ロット№を入れてください(頭の数桁だけでも探せます)", "lot")
        if not lot:
            raise BadRequest("ロット№は英数字で入れてください(全角でもかまいません)", "lot")
        start, end = _date(payload.get("start"), "start"), _date(payload.get("end"), "end")
        if start > end:
            raise BadRequest("開始日は終了日以前にしてください", "start")
        return cls(lot=lot, start=start, end=end, line=line)

    @property
    def all_lines(self) -> bool:
        return self.line == ALL_LINES

    def lines(self, own_line: str) -> list[str]:
        """読むライン。1つか、定義の全部。"""
        return known_lines(own_line) if self.all_lines else [self.line]

    @property
    def line_label(self) -> str:
        return line_label(self.line)

    def note(self) -> str:
        return (f"{self.start.isoformat()} 〜 {self.end.isoformat()} / "
                f"{self.line_label} / ロット№ {self.lot}…")

    def matches(self, text: object) -> bool:
        return tidy_lot(text).startswith(self.lot)


def _hhmm(hour: str, minute: str) -> str:
    """時・分。**打っていなければ空**。数でなければ打ったまま並べる。"""
    hour, minute = (hour or "").strip(), (minute or "").strip()
    if not hour and not minute:
        return ""
    try:
        return f"{int(hour or 0)}:{int(minute or 0):02d}"
    except ValueError:
        return f"{hour}:{minute}"


@dataclass(frozen=True)
class Found:
    """当たった1行と、どこから読んだか。"""

    header: HeaderRecord
    detail: DetailRecord
    source: str
    #: 手元にあるページか(「呼び出す」の行き先を決める)
    local: bool = False


def _cell(found: Found, name: str) -> str:
    header, detail = found.header, found.detail
    if name == "line":
        return line_names.label(header.line)
    if name == "worker":
        return header.worker
    if name == "source":
        return found.source
    if name == "start":
        return _hhmm(detail.kz, detail.kh)
    if name == "end":
        return _hhmm(detail.sz, detail.sh)
    return str(getattr(detail, name, "") or "")


def _sort_key(found: Found) -> tuple:
    """**作業日 → 直 → ページ → 行**(紙をめくる順。`LotRow.sort_key` と同じ)。

    全ラインのときは同じ直のなかでラインの並び(定義の表の順)。
    """
    header, detail = found.header, found.detail
    order = {code: i for i, code in enumerate(line_names.codes())}
    return (parse_business_date(header.report_date) or date.min,
            *shift_sort_key(header.shift), order.get(header.line, len(order)), header.line,
            int(header.page), int(detail.row_no))


# ----------------------------------------------------------------------
# 集める ── 共有 + 手元(共有を読めなければ 手元 + 控え)
# ----------------------------------------------------------------------
@dataclass
class Gathered:
    found: list[Found] = field(default_factory=list)
    shared: Optional[shared_reports.Readout] = None
    #: 共有を読めなかったときに控えを読んだライン・控えが無い(開けない)ライン
    backup_read: list[str] = field(default_factory=list)
    backup_missing: list[str] = field(default_factory=list)
    #: 共有を読めなかったとき、控えを読ませなかった(ほかのラインで、管理者でない)
    backup_denied: bool = False


def _backup_rows(query: LotQuery, line: str, gathered: Gathered
                 ) -> list[tuple[HeaderRecord, DetailRecord]]:
    """そのラインの控えの当たり。**読むだけ**(`local_backup.reader`)。無ければ空。"""
    from ..services import local_backup

    try:
        with local_backup.reader(line) as backup:
            if backup is None:
                gathered.backup_missing.append(line)
                return []
            rows = [(h, d) for h, d in backup.lot_rows_between(query.start, query.end, line)
                    if h.line == line and query.matches(d.lot)]
    except Exception:                             # noqa: BLE001 - そのラインの控えが読めないだけ
        log.exception("%s の控えを読めませんでした(ロットを探す)", line)
        gathered.backup_missing.append(line)
        return []
    gathered.backup_read.append(line)
    return rows


def gather(repo: NippouRepository, query: LotQuery, *, own_line: str, editor: bool,
           shared_path: Optional[Path] = None) -> Gathered:
    """探す元を集める。**同じページは手元が先**(理由は冒頭)。"""
    lines = query.lines(own_line)
    gathered = Gathered()
    local_pages: set[tuple[str, str, str, int]] = set()
    local: list[tuple[HeaderRecord, DetailRecord]] = []
    for line in lines:
        local_pages.update(repo.headers_between(query.start, query.end, line))
        local.extend((h, d) for h, d in repo.lot_rows_between(query.start, query.end, line)
                     if h.line == line and query.matches(d.lot))

    shared = shared_reports.lot_rows(lines, query.start, query.end, query.matches,
                                     path=shared_path)
    gathered.shared = shared
    shared_pages = {h.key() for h, _ in shared.rows}

    def local_source(header: HeaderRecord) -> str:
        if header.dirty:
            return SOURCE_UNSENT
        return SOURCE_SHARED if header.key() in shared_pages else SOURCE_LOCAL

    gathered.found = [Found(h, d, local_source(h), local=True) for h, d in local]
    if shared.ok:
        gathered.found += [Found(h, d, SOURCE_SHARED) for h, d in shared.rows
                           if h.key() not in local_pages]
        return gathered

    # 共有を読めない ── これまでどおり、ほかのラインは控えから(読めるのは editor だけ)
    for line in lines:
        if line == own_line:
            continue
        if not editor:
            gathered.backup_denied = True
            continue
        gathered.found += [
            Found(h, d, SOURCE_BACKUP_UNSENT if h.dirty else SOURCE_BACKUP)
            for h, d in _backup_rows(query, line, gathered) if h.key() not in local_pages]
    return gathered


@dataclass
class LotSearch:
    """探した結果1画面ぶん。**画面はこれを写すだけ。**"""

    query: LotQuery
    table: Table
    #: 表の行と同じ並びの、押す先(報告日・ライン・直・ページ・行)
    hits: list[dict[str, Any]] = field(default_factory=list)
    #: 当たった行の数(上限で切る前)
    total: int = 0
    summary: str = ""
    #: 多すぎるとき・何も無いときの一言
    warning: str = ""
    #: 共有を読めなかったとき・控えを読めなかったときの断り(探すのはやめていない)
    notice: str = ""
    #: どこを探したか(いつも出す1行)
    scope: str = ""
    #: 共有を読んだ結果(`shared_reports` の状態)
    shared: str = ""
    #: 「呼び出す」を出すか(`ctx.editor`)と、選んだラインの行き先(行ごとの行き先は `hits`)
    recall_allowed: bool = False
    recall_path: str = RECALL_LOCAL
    #: 探したときの上限(切った数)
    limit: int = LIMIT
    generated_at: str = ""

    @property
    def too_many(self) -> bool:
        return self.total > self.limit

    def as_dict(self) -> dict[str, Any]:
        return {"lot": self.query.lot, "line": self.query.line,
                "line_label": self.query.line_label, "all_lines": self.query.all_lines,
                "start": self.query.start.isoformat(), "end": self.query.end.isoformat(),
                "table": self.table.as_dict(), "hits": self.hits,
                "total": self.total, "too_many": self.too_many, "limit": self.limit,
                "summary": self.summary, "warning": self.warning,
                "notice": self.notice, "scope": self.scope, "shared": self.shared,
                "recall": {"allowed": self.recall_allowed, "path": self.recall_path},
                "generated_at": self.generated_at}


def _words(gathered: Gathered) -> tuple[str, str]:
    """(どこを探したか, 断り)。"""
    shared = gathered.shared
    if shared is not None and shared.ok:
        return ("探した範囲: 共有の日報データ(各 PC が「共有へ保存」で送った行)と、"
                "この端末のまだ送っていない行(打ちかけを含む)", "")
    what = "この端末と控えの分だけ" if gathered.backup_read else "この端末の分だけ"
    notes = [f"{shared.reason() if shared else ''}、{what}探しました"]
    if shared is not None and shared.detail:
        notes[0] += f"(詳しく: {shared.detail})"
    if gathered.backup_missing:
        names = "・".join(line_names.label(ln) for ln in gathered.backup_missing)
        notes.append(f"控えが無い・開けないライン: {names}")
    if gathered.backup_denied:
        notes.append("ほかのラインの控えは、アクセス権限の表で Administrator の PC か"
                     "管理者モードのときだけ読みます")
    scope = "探した範囲: この端末に保存した行" + (
        "と、ほかのラインの控え" if gathered.backup_read else "")
    return scope, "\n".join(notes)


def _recall_path(found: Found, own_line: str) -> str:
    """この端末のラインで手元にあるページは手元から、ほかは控えから入れて開く。"""
    return RECALL_LOCAL if (found.local and found.header.line == own_line) else RECALL_BACKUP


def search(repo: NippouRepository, query: LotQuery, *, own_line: str, editor: bool,
           shared_path: Optional[Path] = None) -> LotSearch:
    """探す。`repo` はこの端末の手元。共有・控えはここから読みに行く(読むだけ)。"""
    gathered = gather(repo, query, own_line=own_line, editor=editor, shared_path=shared_path)
    found = sorted(gathered.found, key=_sort_key)
    shown = found[:LIMIT]

    table = Table(
        key="lot_search", title=TITLE, note=query.note(),
        columns=[label for _, label, _ in COLUMNS],
        numeric=[numeric for _, _, numeric in COLUMNS],
        rows=[[_cell(f, name) for name, _, _ in COLUMNS] for f in shown],
        empty=f"{query.lot} で始まるロットの行は、この期間にありません")
    hits = [{"report_date": f.header.report_date, "line": f.header.line,
             "shift": f.header.shift, "page": int(f.header.page),
             "row_no": int(f.detail.row_no), "lot": f.detail.lot, "source": f.source,
             "recall_path": _recall_path(f, own_line),
             # どの直・ページの何行目か。ボタンの説明と読み上げに使う
             "where": (f"{f.header.report_date} {line_names.label(f.header.line)} "
                       f"{f.header.shift} {f.header.page}ページ {f.detail.row_no}行目")}
            for f in shown]

    total = len(found)
    shifts = {(f.header.report_date, f.header.line, f.header.shift) for f in found}
    lots = {tidy_lot(f.detail.lot) for f in found}
    lines = {f.header.line for f in found}
    truncated = bool(gathered.shared and gathered.shared.truncated)
    if total:
        count = f"{total}行以上" if truncated else f"{total}行"
        spread = f"{len(lots)}ロット・{len(shifts)}直"
        if query.all_lines:
            spread += f"・{len(lines)}ライン"
        summary = f"{query.lot} で始まるロット: {count}({spread})"
    else:
        summary = ""
    warnings = []
    if truncated:
        warnings.append(f"共有で当たった行が {shared_reports.CAP}行を超えたので、そこで読むのを"
                        "やめました(出ている行が期間の頭からとは限りません)。"
                        "ロット№の桁を増やすか、期間を絞ってください")
    elif total > LIMIT:
        warnings.append(f"当たった行が {total}行 あり、多すぎるので先の {LIMIT}行だけ出しています。"
                        "ロット№の桁を増やすか、期間を絞ってください")
    elif not total:
        wider = "" if query.all_lines else "、ラインを「全ライン」に"
        warnings.append(f"{query.lot} で始まるロットの行は、{query.note()} にはありません"
                        f"(期間を広げる{wider}か、ロット№の桁を減らしてください)")
    scope, notice = _words(gathered)
    return LotSearch(
        query=query, table=table, hits=hits, total=total, summary=summary,
        warning="\n".join(warnings), notice=notice, scope=scope,
        shared=gathered.shared.status if gathered.shared else "",
        recall_allowed=bool(editor),
        recall_path=RECALL_LOCAL if query.line == own_line else RECALL_BACKUP,
        limit=LIMIT, generated_at=datetime.now().strftime("%H:%M"))


def screen(today: date, own_line: str) -> dict[str, Any]:
    """画面を開いたときのカードの中身(既定の期間・上限・ロット欄の属性・ラインの欄)。"""
    start, end = default_span(today)
    return {"start": start.isoformat(), "end": end.isoformat(), "limit": LIMIT,
            "months": DEFAULT_MONTHS, "line": own_line,
            # ラインの欄は**全員に**。既定はこの端末のライン、最後に「全ライン」
            "lines": ([{"code": ln, "label": line_names.label(ln)}
                       for ln in known_lines(own_line)]
                      + [{"code": ALL_LINES, "label": ALL_LABEL}]),
            # 日報入力のロット欄と同じ制限(英数字・7桁・IME に頼らない)
            "attrs": input_rules.as_attributes("LOT")}


def empty_message() -> str:
    """CSV を押したのに当たりが無いとき。**空のファイルは書きません。**"""
    return "当たった行が無いので、CSVは書きませんでした"
