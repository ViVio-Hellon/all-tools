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
    4. 控え(LocalBackup)にあるのは日報の見出しと明細だけ(集計は無い)。
       ほかのラインを探すときも、そのまま同じ読み方ができる
    5. 出る字が**打ったまま**(紙と同じ)。集計は値なので「08」が 8 になる

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

【ラインは混ぜない】
探すのは1度に1ライン。どのラインを読むか(Administrator の PC だけ選べる・
ほかのラインは控えから・控えには書かない)は集計管理と同じ
`services/line_view` が決めます。ここは渡されたリポジトリをそのラインで読むだけ。

**判断はここだけ**で、画面(`views/records.js`)は受け取った行とボタンを並べるだけです。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from ..db.models import DetailRecord, HeaderRecord
from ..db.repository import NippouRepository
from ..logic import input_rules, line_names
from ..logic.aggregation import shift_sort_key
from ..logic.shift import parse_business_date
from .agg_admin import Table

#: 画面に出す行の上限。**1画面で目で追える数**にしてあります ── 3か月で頭の
#: 3〜4桁なら、ふつうは数十行。これを超えるのは桁が足りないときです
LIMIT = 200

#: 既定の期間(か月)。いまの月を入れて数える(10月13日なら 8月1日〜10月13日)
DEFAULT_MONTHS = 3

#: 「呼び出す」の行き先。**この端末のラインは手元から、ほかのラインは控えから**
#: (控えの直は手元へ入れてから開く。どちらも記録を見るの既存の道)
RECALL_LOCAL = "/api/settings/recall"
RECALL_BACKUP = "/api/records/backup/open"

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
    # 共有へ渡したページか。**未は打ちかけ・直したてのこともある**
    ("synced", "共有へ", False),
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
    line: str

    @classmethod
    def of(cls, payload: dict, line: str) -> "LotQuery":
        """画面から来た条件。`line` は `line_view.resolve` で決めたもの(ここでは選ばない)。"""
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

    def note(self) -> str:
        return (f"{self.start.isoformat()} 〜 {self.end.isoformat()} / "
                f"{line_names.label(self.line)} / ロット№ {self.lot}…")


def _hhmm(hour: str, minute: str) -> str:
    """時・分。**打っていなければ空**。数でなければ打ったまま並べる。"""
    hour, minute = (hour or "").strip(), (minute or "").strip()
    if not hour and not minute:
        return ""
    try:
        return f"{int(hour or 0)}:{int(minute or 0):02d}"
    except ValueError:
        return f"{hour}:{minute}"


def _cell(header: HeaderRecord, detail: DetailRecord, name: str) -> str:
    if name == "line":
        return line_names.label(header.line)
    if name == "worker":
        return header.worker
    if name == "synced":
        return "未" if header.dirty else "済"
    if name == "start":
        return _hhmm(detail.kz, detail.kh)
    if name == "end":
        return _hhmm(detail.sz, detail.sh)
    return str(getattr(detail, name, "") or "")


def _sort_key(item: tuple[HeaderRecord, DetailRecord]) -> tuple:
    """**作業日 → 直 → ページ → 行**(紙をめくる順。`LotRow.sort_key` と同じ)。"""
    header, detail = item
    return (parse_business_date(header.report_date) or date.min,
            *shift_sort_key(header.shift), int(header.page), int(detail.row_no))


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
    #: 「呼び出す」を出すか(`ctx.editor`)と、その行き先
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
                "start": self.query.start.isoformat(), "end": self.query.end.isoformat(),
                "table": self.table.as_dict(), "hits": self.hits,
                "total": self.total, "too_many": self.too_many, "limit": self.limit,
                "summary": self.summary, "warning": self.warning,
                "recall": {"allowed": self.recall_allowed, "path": self.recall_path},
                "generated_at": self.generated_at}


def search(repo: NippouRepository, query: LotQuery, *, own_line: str,
           editor: bool) -> LotSearch:
    """探す。`repo` は `line_view.repo_for` が渡したもの(そのラインの手元か控えの写し)。"""
    found = [(header, detail)
             for header, detail in repo.lot_rows_between(query.start, query.end, query.line)
             if header.line == query.line and tidy_lot(detail.lot).startswith(query.lot)]
    found.sort(key=_sort_key)
    shown = found[:LIMIT]

    table = Table(
        key="lot_search", title=TITLE, note=query.note(),
        columns=[label for _, label, _ in COLUMNS],
        numeric=[numeric for _, _, numeric in COLUMNS],
        rows=[[_cell(h, d, name) for name, _, _ in COLUMNS] for h, d in shown],
        empty=f"{query.lot} で始まるロットの行は、この期間にありません")
    hits = [{"report_date": h.report_date, "line": h.line, "shift": h.shift,
             "page": int(h.page), "row_no": int(d.row_no), "lot": d.lot,
             # どの直・ページの何行目か。ボタンの説明と読み上げに使う
             "where": f"{h.report_date} {h.shift} {h.page}ページ {d.row_no}行目"}
            for h, d in shown]

    total = len(found)
    shifts = {(h.report_date, h.shift) for h, _ in found}
    lots = {tidy_lot(d.lot) for _, d in found}
    if total:
        summary = (f"{query.lot} で始まるロット: {total}行"
                   f"({len(lots)}ロット・{len(shifts)}直)")
    else:
        summary = ""
    warning = ""
    if total > LIMIT:
        warning = (f"当たった行が {total}行 あり、多すぎるので先の {LIMIT}行だけ出しています。"
                   "ロット№の桁を増やすか、期間を絞ってください")
    elif not total:
        warning = (f"{query.lot} で始まるロットの行は、{query.note()} にはありません"
                   "(探すのは保存してある行です。期間を広げるか、ロット№の桁を減らしてください)")
    return LotSearch(
        query=query, table=table, hits=hits, total=total, summary=summary, warning=warning,
        recall_allowed=bool(editor),
        recall_path=RECALL_LOCAL if query.line == own_line else RECALL_BACKUP,
        limit=LIMIT, generated_at=datetime.now().strftime("%H:%M"))


def screen(today: date, own_line: str) -> dict[str, Any]:
    """画面を開いたときのカードの中身(既定の期間・上限・ロット欄の属性)。"""
    start, end = default_span(today)
    return {"start": start.isoformat(), "end": end.isoformat(), "limit": LIMIT,
            "months": DEFAULT_MONTHS, "line": own_line,
            # 日報入力のロット欄と同じ制限(英数字・7桁・IME に頼らない)
            "attrs": input_rules.as_attributes("LOT")}


def empty_message() -> str:
    """CSV を押したのに当たりが無いとき。**空のファイルは書きません。**"""
    return "当たった行が無いので、CSVは書きませんでした"

