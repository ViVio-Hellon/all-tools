"""出来事の記録 ── **あとから「なぜ」を辿れる形に残す**

    エラー等の後追いが現状できないと感じている
    ログを残しなぜなぜで分析できるようにしておいてほしい

【前は何が足りなかったのか】
記録(`nippou.log`)は1行の文だけでした。

    2026-10-01 12:53:01  ERROR  nippou.services.summary  集計を作れませんでした

これでは**なぜなぜの1段目で止まります。** 誰が・どの画面で・何を押して・
どの直を開いていて・その前に何をしていたのかが残っていないからです。
予期しないエラーは画面に「通信に失敗しました (HTTP 500)」と出るだけで、
**記録のどの行がそのエラーなのかも結び付きませんでした。**

【何を残すか ── なぜなぜの段に合わせる】

    起きたこと      … いつ・どの端末・どの画面・何をした(操作)・何と出た
    直接の原因      … 例外の種類と文・**コードのどこで**(ファイル:行)
    そのときの状態  … ライン・開いていた直・呼び出し中か・管理者か・版
    それまでの経過  … 同じタブで、その前に何をしていたか(操作の並び)

1件ずつに**番号**を付けます(`E1001-1253-7KX`)。画面のエラーにも同じ番号を
出すので、「この番号のエラーが出た」と言ってもらえば記録の1件に辿れます。

【この層の約束】
ここは**純粋**です。ファイルも時計も触りません。記録の形・入力の要約・
番号の作り方・経過の拾い方・なぜなぜシートの組み立てだけを持ちます。
書く・読むのは `services/event_log.py`、拾う口は `app/__init__.py`。
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional, Sequence

# ------------------------------------------------------------------
# 種類 ── **一覧で絞り込む単位**。文言から推し量らない
# ------------------------------------------------------------------
KIND_ERROR = "error"       # 予期しないエラー(サーバ)。番号を画面に出す
KIND_CLIENT = "client"     # 画面(ブラウザ)のエラー
KIND_WARN = "warn"         # 記録に残った警告(読めない・書けない など)
KIND_REFUSED = "refused"   # 業務としての断り(4xx)。**なぜ保存できなかったか**
KIND_OP = "op"             # 操作(押した・開いた)。経過を辿るためのもの
KIND_INFO = "info"         # 起動・設定の変更など、残しておく出来事

KIND_LABELS = {
    KIND_ERROR: "エラー",
    KIND_CLIENT: "画面のエラー",
    KIND_WARN: "警告",
    KIND_REFUSED: "断り",
    KIND_OP: "操作",
    KIND_INFO: "記録",
}

#: 番号の頭の1文字。**読み上げても取り違えない**ように種類で分ける
_PREFIX = {KIND_ERROR: "E", KIND_CLIENT: "C", KIND_WARN: "W",
           KIND_REFUSED: "R", KIND_OP: "O", KIND_INFO: "I"}

#: 番号の末尾に使う文字。**0/O・1/I/L・5/S・8/B のように見間違えるものを除く**
#: ── 電話や手書きで伝わる番号にするため
_ALPHABET = "234679ACDEFGHJKMNPQRTUVWXYZ"

#: 一覧の「見る範囲」の選択肢。**エラーだけ**が既定(探しに来た人が見たいもの)
SCOPES = {
    "errors": (KIND_ERROR, KIND_CLIENT),
    "problems": (KIND_ERROR, KIND_CLIENT, KIND_WARN, KIND_REFUSED),
    "all": tuple(KIND_LABELS),
}
SCOPE_LABELS = {
    "errors": "エラーだけ",
    "problems": "エラー・警告・断り",
    "all": "操作も全部",
}
DEFAULT_SCOPE = "errors"

#: なぜなぜの状態
STATUS_OPEN = "open"
STATUS_DOING = "doing"
STATUS_DONE = "done"
STATUS_LABELS = {STATUS_OPEN: "未着手", STATUS_DOING: "対策中",
                 STATUS_DONE: "対策済"}

#: なぜを書く欄の数(なぜなぜ分析は5回が目安)
WHY_COUNT = 5
#: 1つの欄の長さ
NOTE_MAX = 400

#: 入力の要約で、**値を残さない**鍵(含んでいれば伏せる)
SECRET_WORDS = ("password", "passwd", "token", "secret", "合言葉", "パスワード")
#: 要約に残す項目の数と、1つの値の長さ
INPUT_MAX_ITEMS = 40
INPUT_MAX_LEN = 80
MESSAGE_MAX = 500
TRACE_MAX = 8000


# ------------------------------------------------------------------
# 番号
# ------------------------------------------------------------------
def new_id(kind: str, now: datetime, rng: Optional[random.Random] = None) -> str:
    """出来事の番号。``E1001-1253-7KX`` の形(種類・月日・時分・3文字)。

    **日付と時刻を入れる**のは、番号だけ聞いても「いつのことか」が
    分かるようにするためです。同じ1分に何件あっても、末尾の3文字で
    見分けます(27 × 27 × 27 ≒ 2万)。
    """
    pick = (rng or random).choice
    tail = "".join(pick(_ALPHABET) for _ in range(3))
    return f"{_PREFIX.get(kind, 'X')}{now:%m%d}-{now:%H%M}-{tail}"


def kind_of_status(status: int) -> str:
    """応答の番号から種類を決める。"""
    if status >= 500:
        return KIND_ERROR
    if status >= 400:
        return KIND_REFUSED
    return KIND_OP


# ------------------------------------------------------------------
# 入力の要約 ── **何を送ったか**を残す。ただし伏せるものは伏せる
# ------------------------------------------------------------------
def is_secret(key: str) -> bool:
    low = str(key).lower()
    return any(word in low for word in SECRET_WORDS)


def summarize_input(data: Any, *, max_items: int = INPUT_MAX_ITEMS,
                    max_len: int = INPUT_MAX_LEN) -> dict[str, str]:
    """送られた中身を、**「鍵の道: 値」の平たい形**にする。

    日報の12行は ``rows.1.LOT: B123456`` のようになります。**空の欄は
    残しません**(12行 × 30欄の空欄で埋まると、打った値が読めない)。
    合言葉など伏せるべき鍵は ``***`` にします。長い値は切ります。
    """
    out: dict[str, str] = {}
    overflow = 0

    def put(path: str, value: Any) -> None:
        nonlocal overflow
        if len(out) >= max_items:
            overflow += 1
            return
        text = " ".join(str(value).split())
        if len(text) > max_len:
            text = text[:max_len] + "…"
        out[path] = text

    def walk(value: Any, path: str, depth: int) -> None:
        if path and is_secret(path.rsplit(".", 1)[-1]):
            if value not in (None, ""):
                put(path, "***")
            return
        if isinstance(value, dict):
            if depth >= 4:
                put(path, f"({len(value)}項目)")
                return
            for key, inner in value.items():
                walk(inner, f"{path}.{key}" if path else str(key), depth + 1)
            return
        if isinstance(value, (list, tuple)):
            if not value:
                return
            if depth >= 4 or len(value) > 12:
                put(path, f"({len(value)}件)")
                return
            for i, inner in enumerate(value):
                walk(inner, f"{path}[{i}]", depth + 1)
            return
        if value is None or (isinstance(value, str) and not value.strip()):
            return
        put(path or "値", value)

    walk(data, "", 0)
    if overflow:
        out["…"] = f"ほか {overflow} 項目"
    return out


def clip(text: Any, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + "…"


# ------------------------------------------------------------------
# コードのどこで起きたか
# ------------------------------------------------------------------
def where_in_code(frames: Sequence[tuple[str, int, str]],
                  roots: Sequence[str]) -> str:
    """例外の通り道から、**このツールのコードの中でいちばん深い所**を1つ。

    `frames` は (ファイル, 行, 関数) の並び(浅い → 深い)。Python や
    Flask の中で起きたエラーでも、**こちらのどの行が呼んだのか**が
    なぜなぜの2段目になります。こちらのコードに1つも無ければ、
    いちばん深い所を返します。
    """
    def norm(path: str) -> str:
        return str(path).replace("\\", "/")

    keys = [norm(r).rstrip("/") + "/" for r in roots if r]
    ours = [(f, n, fn) for f, n, fn in frames
            if any(norm(f).startswith(k) for k in keys)
            and "/site-packages/" not in norm(f)]
    pick = ours[-1] if ours else (frames[-1] if frames else None)
    if pick is None:
        return ""
    path, line, func = pick
    shown = norm(path)
    for key in keys:
        if shown.startswith(key):
            shown = shown[len(key):]
            break
    return f"{shown}:{line} ({func})"


# ------------------------------------------------------------------
# 操作の呼び名 ── 経過を**人が読める言葉**で並べる
# ------------------------------------------------------------------
#: (方法, 道の頭) → 呼び名。**長い頭ほど先に当てる**(`label_of`)
ACTION_LABELS: dict[tuple[str, str], str] = {
    ("GET", "/"): "日報入力を開いた",
    ("GET", "/gw"): "梱包資材重量計算を開いた",
    ("GET", "/vc"): "VC長さ計算を開いた",
    ("GET", "/records"): "記録を見るを開いた",
    ("GET", "/graph/review"): "直の実績の確認を開いた",
    ("GET", "/graph"): "集計・グラフを開いた",
    ("GET", "/agg"): "集計管理を開いた",
    ("GET", "/standard-time"): "標準作業時間を開いた",
    ("GET", "/settings"): "設定・管理者を開いた",
    ("GET", "/print"): "印刷を開いた",
    ("GET", "/report/"): "紙(印刷用)を開いた",
    ("POST", "/api/entry/save"): "日報: 保存(確定)",
    ("POST", "/api/entry/state"): "日報: 欄を確かめる",
    ("POST", "/api/entry/lot"): "日報: ロットを引く",
    ("POST", "/api/entry/line"): "日報: ラインを変える",
    ("POST", "/api/entry/mark"): "日報: etc の印",
    ("POST", "/api/entry/check"): "日報: この直をチェック",
    ("POST", "/api/entry/newpage"): "日報: 次のページ発行",
    ("POST", "/api/entry/verify"): "日報: 確かめる",
    ("POST", "/api/entry/handover"): "日報: 引き継ぐ",
    ("POST", "/api/entry/start-next"): "日報: 次の直を始める",
    ("POST", "/api/entry/prev-shift-ack"): "日報: 前の直の知らせを閉じた",
    ("POST", "/api/entry/close"): "直の締め(自動)",
    ("POST", "/api/staff/apply"): "作業者を選ぶ",
    ("POST", "/api/formstop/execute"): "全停入力",
    ("POST", "/api/settings/push"): "共有へ保存",
    ("POST", "/api/settings/recall"): "過去の直を呼び出す",
    ("POST", "/api/settings/backfill"): "過去の直を後から作る",
    ("POST", "/api/settings/back"): "最新のページに戻る",
    ("POST", "/api/settings/page"): "ページを移る",
    ("POST", "/api/settings/admin"): "管理者モードの切り替え",
    ("POST", "/api/settings/admin-password"): "管理者パスワードを変える",
    ("POST", "/api/settings/paths"): "参照設定を保存",
    ("POST", "/api/settings/import/apply"): "過去の日報を取り込む",
    ("POST", "/api/settings/import/upload"): "取り込むファイルを渡す",
    ("POST", "/api/settings/import/preview"): "取り込みの下見",
    ("POST", "/api/settings/rebuild-summary"): "集計を作り直す",
    ("POST", "/api/settings/restore-shift"): "当直をDBから復旧",
    ("POST", "/api/settings/rollover"): "月替わりの書き出し",
    ("POST", "/api/settings/sync-shift"): "時間マスタを取り込む",
    ("POST", "/api/settings/"): "設定の操作",
    ("POST", "/api/gw/calculate"): "GW計算",
    ("POST", "/api/gw/"): "梱包資材重量計算の操作",
    ("POST", "/api/vc/run"): "VC長さ計算",
    ("POST", "/api/vc/"): "VC長さ計算の操作",
    ("POST", "/api/master/row/save"): "マスタの行を直す",
    ("POST", "/api/master/row/add"): "マスタに行を足す",
    ("POST", "/api/master/row/delete"): "マスタの行を消す",
    ("POST", "/api/master/"): "マスタの操作",
    ("POST", "/api/print/load"): "紙を読み込む",
    ("POST", "/api/agg/"): "集計管理の操作",
    ("POST", "/api/graph/review/ack"): "直の実績の確認を閉じた",
    ("POST", "/api/graph/"): "集計・グラフの操作",
    ("POST", "/api/standard-time/"): "標準作業時間の操作",
    ("POST", "/api/tab/take"): "このタブで打つ",
    ("POST", "/api/shutdown"): "終了",
    ("POST", "/api/log/client"): "画面のエラーを知らせた",
    ("POST", "/api/log/"): "ログの操作",
}


#: 開いた画面として**ちょうど**その道のときだけ当てるもの(頭で当てると
#: `/` がすべてに当たる)
_EXACT_PAGES = ("/", "/gw", "/vc", "/records", "/graph", "/agg", "/settings",
                "/standard-time", "/print")


def label_of(method: str, path: str) -> str:
    """操作の呼び名。知らないものは道をそのまま返します。"""
    method = (method or "").upper()
    best = ""
    label = ""
    for (m, head), text in ACTION_LABELS.items():
        if m != method:
            continue
        hit = path == head if head in _EXACT_PAGES else path.startswith(head)
        if hit and len(head) > len(best):
            best, label = head, text
    return label or f"{method} {path}".strip()


def request_label(method: str, path: str, data: Any = None) -> str:
    """要求の呼び名。**同じ道でも中身で意味が変わる**ものはここで言い分ける。"""
    if path == "/api/entry/save" and isinstance(data, dict) and data.get("draft"):
        return "日報: 打ちかけを置く(ページを移る前)"
    if path == "/api/settings/admin" and isinstance(data, dict):
        return "管理者モードにする" if data.get("enable") else "管理者モードを解く"
    return label_of(method, path)


# ------------------------------------------------------------------
# どの要求を残すか
# ------------------------------------------------------------------
#: 残さない道(心拍・見張り・進み具合)。**断りやエラーなら残します**
QUIET_PATHS = frozenset({
    "/api/health", "/api/alive", "/api/tab/ping", "/api/tab/hide",
    "/api/tab/release", "/api/tab/claim", "/api/progress",
    "/api/settings/import/progress", "/api/log/client", "/favicon.ico",
})
_FILE_PREFIXES = ("/static/", "/sv/", "/sound/")
#: 操作としては残さない頭。**ログを見る操作そのもの**は経過に要らない
_QUIET_PREFIXES = ("/api/log/",)


def should_record(method: str, path: str, status: int, *, quiet: bool = False) -> bool:
    """この要求を操作として残すか。

        残す   … 押した操作(POST)・開いた画面・**断りとエラーは全部**
        残さない … 心拍や1分ごとの見張り(`quiet`)・読むだけの問い合わせ
                   (GET /api/…)・静的ファイル

    見張りまで残すと、1直で数千件になって経過が読めなくなります。
    """
    if status >= 400:
        return not (status == 404 and path == "/favicon.ico")
    if (path.startswith(_FILE_PREFIXES) or path.startswith(_QUIET_PREFIXES)
            or path in QUIET_PATHS or quiet):
        return False
    if method.upper() == "GET" and path.startswith("/api/"):
        return False
    return method.upper() in ("GET", "POST", "PUT", "DELETE")


def key_text(*, recall: bool, report_date: str = "", line: str = "",
             shift: str = "", page: Any = None) -> str:
    """開いていた直。``2026年10月1日 L-1 1直 ページ2``。"""
    parts = [p for p in (report_date, line, shift) if p]
    if not parts:
        return ""
    text = " ".join(parts)
    if recall and page:
        text += f" ページ{page}"
    return text


# ------------------------------------------------------------------
# 1件の形
# ------------------------------------------------------------------
#: 記録の欄。**この順でCSVにも出す**(人が読む順)
FIELDS = ("id", "at", "kind", "terminal", "screen", "action", "label", "status",
          "message", "cause", "where", "line", "key", "recall", "admin", "tab",
          "request", "ms", "logger", "version", "input", "crumbs", "trace",
          "count")


def make(kind: str, *, at: datetime, id: str = "", **fields: Any) -> dict[str, Any]:
    """出来事1件。**欄が欠けていても読めるよう、空で埋める。**"""
    record: dict[str, Any] = {name: "" for name in FIELDS}
    record.update({"id": id or new_id(kind, at),
                   "at": at.isoformat(timespec="milliseconds"),
                   "kind": kind})
    for name, value in fields.items():
        if name in record and value is not None:
            record[name] = value
    if not record["label"] and record["action"]:
        method, _, path = str(record["action"]).partition(" ")
        record["label"] = label_of(method, path)
    record["message"] = clip(record["message"], MESSAGE_MAX)
    record["trace"] = clip(record["trace"], TRACE_MAX)
    return record


def parse_at(text: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None


def is_problem(record: dict) -> bool:
    return record.get("kind") in SCOPES["problems"]


# ------------------------------------------------------------------
# 絞り込み・経過
# ------------------------------------------------------------------
def select(records: Iterable[dict], *, scope: str = DEFAULT_SCOPE,
           terminal: str = "", text: str = "", limit: int = 500) -> list[dict]:
    """一覧に出すもの。**新しい順**。"""
    kinds = SCOPES.get(scope, SCOPES[DEFAULT_SCOPE])
    needle = (text or "").strip().lower()
    out = []
    for rec in records:
        if rec.get("kind") not in kinds:
            continue
        if terminal and rec.get("terminal") != terminal:
            continue
        if needle and needle not in " ".join(
                str(rec.get(k, "")) for k in ("id", "message", "cause", "label",
                                             "action", "key", "screen", "where")
        ).lower():
            continue
        out.append(rec)
    out.sort(key=lambda r: str(r.get("at", "")), reverse=True)
    return out[:limit]


def timeline(records: Iterable[dict], target: dict, *, before_minutes: int = 30,
             before_limit: int = 30, after_minutes: int = 5,
             after_limit: int = 5) -> list[dict]:
    """**それまでの経過。** 同じ端末の、その前 30分(最大30件)とその後 5分。

    同じタブの出来事があれば**タブで絞ります** ── 2枚のタブを開いて
    いると、別のタブの操作が混ざって経過が読めなくなるので。タブが
    分からない出来事(裏で動いた処理・記録に残った警告)は端末で拾います。
    """
    at = parse_at(target.get("at", ""))
    if at is None:
        return [target]
    start = at - timedelta(minutes=before_minutes)
    end = at + timedelta(minutes=after_minutes)
    tab = target.get("tab") or ""
    terminal = target.get("terminal") or ""
    before: list[tuple[datetime, dict]] = []
    after: list[tuple[datetime, dict]] = []
    for rec in records:
        if rec.get("id") == target.get("id"):
            continue
        if terminal and rec.get("terminal") not in ("", terminal):
            continue
        if tab and rec.get("tab") and rec.get("tab") != tab:
            continue
        when = parse_at(rec.get("at", ""))
        if when is None or when < start or when > end:
            continue
        (before if when <= at else after).append((when, rec))
    before.sort(key=lambda p: p[0])
    after.sort(key=lambda p: p[0])
    picked = [r for _, r in before[-before_limit:]]
    return picked + [target] + [r for _, r in after[:after_limit]]


# ------------------------------------------------------------------
# なぜなぜシート
# ------------------------------------------------------------------
def clean_note(text: Any) -> str:
    return str(text or "").strip()[:NOTE_MAX]


def clean_analysis(body: dict) -> dict[str, Any]:
    """書いてもらったなぜなぜ。**欄の数と長さをここで揃える。**"""
    whys = body.get("whys") or []
    whys = [clean_note(w) for w in list(whys)[:WHY_COUNT]]
    whys += [""] * (WHY_COUNT - len(whys))
    status = body.get("status") if body.get("status") in STATUS_LABELS else STATUS_OPEN
    return {"whys": whys, "measure": clean_note(body.get("measure")),
            "status": status, "by": clean_note(body.get("by"))[:40]}


def step_text(rec: dict) -> str:
    """経過の1行。``12:53:01 日報: 保存(確定) → 断り: 先に作業者を…``"""
    at = parse_at(rec.get("at", ""))
    when = at.strftime("%H:%M:%S") if at else ""
    what = rec.get("label") or rec.get("action") or KIND_LABELS.get(rec.get("kind"), "")
    kind = rec.get("kind")
    if kind == KIND_OP:
        tail = ""
    elif rec.get("message") or rec.get("cause"):
        tail = f" → {KIND_LABELS.get(kind, kind)}: {clip(headline(rec), 160)}"
    else:
        tail = f" → {KIND_LABELS.get(kind, kind)}"
    if kind in (KIND_WARN, KIND_INFO) and not rec.get("label"):
        what = rec.get("message", "")
        tail = f" ({KIND_LABELS.get(kind, kind)})"
    return f"{when} {what}{tail}".strip()


#: 予期しないエラーで画面に出す決まり文句の目印(`app/event_capture.user_message`)。
#: **中身を言っていない**ので、一覧と手がかりでは代わりに例外を出す
GENERIC_MARK = "エラー番号"


def is_generic(message: str) -> bool:
    return GENERIC_MARK in str(message or "")


def headline(rec: dict) -> str:
    """一覧の「何と出た」。決まり文句なら**例外のほう**を出す(何が壊れたかが読める)。"""
    message = str(rec.get("message") or "")
    if is_generic(message) and rec.get("cause"):
        return str(rec["cause"])
    return message or str(rec.get("cause") or "")


def why_hint(rec: dict) -> str:
    """なぜ1の**手がかり**(書くのは人)。記録から言えることだけを並べる。"""
    parts = []
    label = rec.get("label") or rec.get("action")
    if label:
        parts.append(f"「{label}」の途中で")
    message = rec.get("message")
    if message and not is_generic(message):
        parts.append(f"「{clip(message, 80)}」と出た。")
    if rec.get("cause"):
        parts.append(f"直接には {clip(rec['cause'], 120)}")
    if rec.get("where"):
        parts.append(f"(コードの {rec['where']})")
    return " ".join(parts)


def sheet(target: dict, steps: Sequence[dict],
          analysis: Optional[dict] = None) -> dict[str, Any]:
    """なぜなぜシート1枚。**記録から言えること**と**人が書くこと**を分けて持つ。"""
    at = parse_at(target.get("at", ""))
    facts = [
        ("番号", target.get("id", "")),
        ("いつ", at.strftime("%Y/%m/%d %H:%M:%S") if at else target.get("at", "")),
        ("種類", KIND_LABELS.get(target.get("kind"), target.get("kind", ""))),
        ("どの端末", target.get("terminal", "")),
        ("どの画面", target.get("screen", "")),
        ("何をした", target.get("label") or target.get("action", "")),
        ("何と出た", target.get("message", "")),
    ]
    cause = [
        ("例外", target.get("cause", "")),
        ("コードの場所", target.get("where", "")),
        ("記録した所", target.get("logger", "")),
        ("応答", str(target.get("status") or "")),
    ]
    state = [
        ("ライン", target.get("line", "")),
        ("開いていた直", target.get("key", "")),
        ("呼び出し中", "はい" if target.get("recall") else ""),
        ("管理者モード", "はい" if target.get("admin") else ""),
        ("版", target.get("version", "")),
    ]
    note = analysis or {}
    return {
        "id": target.get("id", ""),
        "facts": [{"label": k, "value": str(v)} for k, v in facts if v],
        "cause": [{"label": k, "value": str(v)} for k, v in cause if v],
        "state": [{"label": k, "value": str(v)} for k, v in state if v],
        "input": target.get("input") or {},
        "crumbs": target.get("crumbs") or [],
        "trace": target.get("trace", ""),
        "steps": [{"id": r.get("id", ""), "text": step_text(r),
                   "kind": r.get("kind", ""), "is_target": r.get("id") == target.get("id")}
                  for r in steps],
        "hint": why_hint(target),
        "analysis": {
            "whys": list(note.get("whys") or [""] * WHY_COUNT),
            "measure": note.get("measure", ""),
            "status": note.get("status", STATUS_OPEN),
            "status_label": STATUS_LABELS.get(note.get("status", STATUS_OPEN), ""),
            "by": note.get("by", ""),
            "at": note.get("at", ""),
            "terminal": note.get("terminal", ""),
        },
    }


# ------------------------------------------------------------------
# CSV(Excel で開く)
# ------------------------------------------------------------------
CSV_HEADER = ("番号", "日時", "種類", "端末", "画面", "操作", "応答", "何と出た",
              "例外", "コードの場所", "ライン", "開いていた直", "呼び出し中",
              "管理者モード", "版", "送った中身",
              *(f"なぜ{i}" for i in range(1, WHY_COUNT + 1)),
              "対策", "状態", "書いた人", "書いた日時")


def csv_row(rec: dict, analysis: Optional[dict] = None) -> list[str]:
    note = analysis or {}
    whys = list(note.get("whys") or [])
    whys += [""] * (WHY_COUNT - len(whys))
    at = parse_at(rec.get("at", ""))
    sent = "; ".join(f"{k}={v}" for k, v in (rec.get("input") or {}).items())
    return [
        rec.get("id", ""),
        at.strftime("%Y/%m/%d %H:%M:%S") if at else str(rec.get("at", "")),
        KIND_LABELS.get(rec.get("kind"), str(rec.get("kind", ""))),
        rec.get("terminal", ""), rec.get("screen", ""),
        rec.get("label") or rec.get("action", ""),
        str(rec.get("status") or ""), rec.get("message", ""),
        rec.get("cause", ""), rec.get("where", ""), rec.get("line", ""),
        rec.get("key", ""), "はい" if rec.get("recall") else "",
        "はい" if rec.get("admin") else "", rec.get("version", ""), sent,
        *whys[:WHY_COUNT],
        note.get("measure", ""),
        STATUS_LABELS.get(note.get("status", ""), "") if note else "",
        note.get("by", ""), note.get("at", ""),
    ]


def list_row(rec: dict, analysis: Optional[dict] = None) -> dict[str, Any]:
    """一覧の1行。**画面はこれを写すだけ。**"""
    at = parse_at(rec.get("at", ""))
    note = analysis or {}
    return {
        "id": rec.get("id", ""),
        "at": at.strftime("%m/%d %H:%M:%S") if at else str(rec.get("at", "")),
        "kind": rec.get("kind", ""),
        "kind_label": KIND_LABELS.get(rec.get("kind"), ""),
        "terminal": rec.get("terminal", ""),
        "screen": rec.get("screen", ""),
        "label": rec.get("label") or rec.get("action", ""),
        "message": clip(headline(rec), 160),
        "count": rec.get("count") or "",
        "status": note.get("status", "") if note else "",
        "status_label": STATUS_LABELS.get(note.get("status", ""), "") if note else "",
    }
