"""案内表示 ── 条件に合ったら画面いっぱいに出す案内(**日報複合ツール全体の機能** ── 1.5.104)

    ライン設定が機側の時に限り 2直の始まりに「計量器の点検を行ってください 点検済みですか？」を
    大きく表示したい。ただ汎用的に使いたいので テーブル:案内表示 の作成。
    テーブル内に案内を出す条件、出すタイミング、大きさをいれたい(ヒットで表示)

    日報をアクセス権限で表示していないラインにたいして何も表示できなくなっている
    日報の機能ではなく all-tools 全体の機能として扱って。大設定で案内表示マスタの編集をできるように

1.5.103 までは日報の中の機能で、日報のタブを出していない PC には出せなかった。いまは**入口(大きなタブ
の画面)**が出す ── どのツールのタブを出していても、出していなくても出る。

**決めるのはここだけ**(表の1行をどう読むか・いま出すか)。読む・覚える・書くは `guide_store.py`、
出すのは入口の画面(`static/js/guide_notice.js`)、直すのは大設定(`static/js/settings.js`)。

【表「案内表示」の列】(**共有の DB**(タブ表示権限と同じ。既定 梱包資材マスタ)。大設定で直す)

    ID          案内の名前(押した記録に残る。空なら行の番号)
    有効        1 で出す・0 で出さない(空は出す)
    ライン      空なら全ライン。「機側,NS1」のように並べてもよい(この端末のライン設定と比べる)
    直          空なら全部の直。「2直」「1直,3直」
    曜日        空なら毎日。「月」「月,水,金」「平日」(月〜金)「土日」。**報告日の曜日**で見る
                (3直は始めた日の曜日 ── 月曜の3直は火曜の朝まで「月」)
    タイミング  直の始まり(空も同じ)/ 時刻 / 直の終わり前
    時刻        「時刻」のとき 14:30 の形(その直の中のその時刻になったら)
    分          「直の終わり前」のとき、終わりの何分前から
    表示文      大きく出す文(改行してよい)
    確認の文    その下に出す問い(例: 点検済みですか？)
    ボタン      押すボタンの字(空なら「確認しました」)
    大きさ      大(画面いっぱい・空も同じ)/ 中(真ん中の窓)/ 小(上の帯)
    色          注意(黄・空も同じ)/ 警告(赤)/ 案内(青)
    名前を選ぶ  1 で押した人の名前を選ばせる(空も同じ)/ 0 で選ばせない
    過ぎても出す 1 でその直に押されなければ、あとの直で出す(空も同じ)/ 0 で出さない
    開始日      この日から出す(空ならずっと前から)。2026-11-09 の形
    終了日      この日まで出す(空ならずっと先まで)
    並び        いくつも当たったときに先に出す順(小さいほど先)

【いつ出すか】**1つの直で、押すまで1回。** 押すまで閉じない。押した記録はその直・その案内で
1つ(`guide_store` が覚える)。直の時間に入ったあと最初の見張り(1分ごと)で出す ──
直の途中で PC を立ち上げても、まだ押していなければ出る。

【押されなかったら、あとの直で出す(v4.47.2)】

    2直を動かしていない から3直で出さないのは違う。動かしていないのであれば3直でやるのが正
    (2直で誰も確認していない、だから3直が代わりにやる必要がある)

「過ぎても出す」(空は出す)の案内は、その直(例: 2直)のあいだに押されないまま直が終わると、**次に
同じ直が始まるまで、あとの直(3直・翌日の1直…)で出す**。出すのはいちばん近い回のぶんだけ。押した
記録は元の直(2直)のぶんとして残り、押した直(3直)も残す。押したかは日報の保存では見ない(日報は
管理者があとから作ることもある)── **押した記録そのもの**で決める(`guide_store.acks`)。
直を決めていない案内(直が空)は、どの直にも自分の回があるので、あとの直へは回さない。
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable, Optional

TABLE = "案内表示"

#: 表を作るときの列と型(sqlite3)。並びは画面(マスタ管理)の並び
COLUMNS: tuple[tuple[str, str], ...] = (
    ("ID", "TEXT"), ("有効", "INTEGER"), ("ライン", "TEXT"), ("直", "TEXT"), ("曜日", "TEXT"),
    ("タイミング", "TEXT"), ("時刻", "TEXT"), ("分", "INTEGER"),
    ("表示文", "TEXT"), ("確認の文", "TEXT"), ("ボタン", "TEXT"),
    ("大きさ", "TEXT"), ("色", "TEXT"), ("名前を選ぶ", "INTEGER"), ("過ぎても出す", "INTEGER"),
    ("開始日", "TEXT"), ("終了日", "TEXT"), ("並び", "INTEGER"),
)

#: 表を作ったときに入れておく1行(頼まれた案内そのもの)
FIRST_ROW: dict[str, Any] = {
    "ID": "計量器の点検", "有効": 1, "ライン": "機側", "直": "2直", "曜日": "",
    "タイミング": "直の始まり", "時刻": "", "分": None,
    "表示文": "計量器の点検を行ってください", "確認の文": "点検済みですか？",
    "ボタン": "点検済み", "大きさ": "大", "色": "注意", "名前を選ぶ": 1, "過ぎても出す": 1,
    "開始日": "", "終了日": "", "並び": 1,
}

#: マスタ管理で行を直すときに出す、列ごとの説明と選べる値(v4.47.2)。
#:
#:     案内表示の色って注意以外に何が設定できるんですか？ 設定時に教えるようにしてください
#:
#: (説明, 選べる値)。選べる値がある列は、それ以外を入れると保存で断る(`problem_of`)
COLUMN_HINTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "ID": ("案内の名前。押した記録に残ります(空なら行の番号)", ()),
    "有効": ("1 で出す・0 で出さない(空は出す)", ("1", "0")),
    "ライン": ("空なら全ライン。「機側,NS1」のように , で並べてもよい", ()),
    "直": ("空なら全部の直。「2直」「1直,3直」のように", ()),
    "曜日": ("空なら毎日。「月」「月,水,金」「平日」(月〜金)「土日」のように。報告日の曜日で見る"
           "(3直は始めた日 ── 月曜の3直は翌朝まで「月」)", ()),
    "タイミング": ("直の始まり(空も同じ)/ 時刻(「時刻」の欄の時刻から)/ 直の終わり前(「分」の欄の分前から)",
               ("直の始まり", "時刻", "直の終わり前")),
    "時刻": ("タイミングが「時刻」のとき。14:30 の形", ()),
    "分": ("タイミングが「直の終わり前」のとき。終わりの何分前から", ()),
    "表示文": ("大きく出す文", ()),
    "確認の文": ("その下に出す問い(例: 点検済みですか？)", ()),
    "ボタン": ("押すボタンの字(空なら「確認しました」)", ()),
    "大きさ": ("大 = 画面いっぱい(空も同じ)/ 中 = 真ん中の窓 / 小 = 上の帯", ("大", "中", "小")),
    "色": ("注意 = 黄(空も同じ)/ 警告 = 赤 / 案内 = 青", ("注意", "警告", "案内")),
    "名前を選ぶ": ("1 で押す人の名前を選ばせる(空も同じ)・0 で選ばせない", ("1", "0")),
    "過ぎても出す": ("1 でその直に押されなければ、次に同じ直が始まるまであとの直で出す(空も同じ)・0 で出さない",
                ("1", "0")),
    "開始日": ("この日から出す(空ならずっと前から)。2026-11-09 の形", ()),
    "終了日": ("この日まで出す(空ならずっと先まで)。2026-11-09 の形", ()),
    "並び": ("いくつも当たったときに先に出す順(小さいほど先)", ()),
}


def problem_of(values: dict[str, Any]) -> str:
    """マスタ管理で書く前の確かめ。**選べる値以外・読めない形**を、選べる値を添えて断る。"""
    for name, value in values.items():
        text = _text(value)
        if not text:
            continue
        hint, choices = COLUMN_HINTS.get(name, ("", ()))
        if choices and text not in choices:
            return f"「{name}」は {' ・ '.join(choices)} のどれかで入れてください({hint})。"
        if name == "時刻" and _time(text) is None:
            return "「時刻」は 14:30 のように 時:分 で入れてください。"
        if name in ("開始日", "終了日") and _date(text) is None:
            return f"「{name}」は 2026-11-09 の形で入れてください。"
        if name == "曜日" and _weekdays(text) is None:
            return ("「曜日」は 月 火 水 木 金 土 日 を , で並べるか、平日・土日・毎日 で入れてください"
                    "(例: 月,水,金)。")
        if name in ("分", "並び") and _int(text) is None:
            return f"「{name}」は数で入れてください。"
    return ""


TIMING_START = "直の始まり"
TIMING_AT = "時刻"
TIMING_BEFORE_END = "直の終わり前"
TIMINGS = (TIMING_START, TIMING_AT, TIMING_BEFORE_END)

SIZE_LARGE, SIZE_MEDIUM, SIZE_SMALL = "大", "中", "小"
COLOR_CAUTION, COLOR_WARNING, COLOR_INFO = "注意", "警告", "案内"

DEFAULT_BUTTON = "確認しました"


@dataclass(frozen=True)
class Notice:
    id: str
    text: str
    question: str = ""
    button: str = DEFAULT_BUTTON
    lines: frozenset[str] = frozenset()
    shifts: frozenset[str] = frozenset()
    #: 出す曜日(0=月)。空なら毎日
    weekdays: frozenset[int] = frozenset()
    timing: str = TIMING_START
    at: Optional[time] = None
    minutes: Optional[int] = None
    size: str = SIZE_LARGE
    color: str = COLOR_CAUTION
    pick_worker: bool = True
    #: 押されなかったら、あとの直で出す
    carry: bool = True
    start: Optional[date] = None
    end: Optional[date] = None
    order: int = 0
    #: 読めなかった欄(この行は出さない。マスタ管理で直してもらう)
    problems: tuple[str, ...] = field(default=())

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text, "question": self.question,
                "button": self.button, "size": self.size, "color": self.color,
                "pick_worker": self.pick_worker}


def _text(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value if value is not None else "")).strip()


def _list(value: Any) -> frozenset[str]:
    text = _text(value)
    for sep in ("、", "・", "/", ";", " "):
        text = text.replace(sep, ",")
    return frozenset(p.strip() for p in text.split(",") if p.strip())


def _flag(value: Any, default: bool) -> bool:
    text = _text(value)
    if not text:
        return default
    return text not in ("0", "いいえ", "無効", "false", "False", "×", "なし")


def _int(value: Any) -> Optional[int]:
    text = _text(value)
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _time(value: Any) -> Optional[time]:
    text = _text(value).replace("時", ":").replace("分", "")
    if not text:
        return None
    try:
        hour, _, minute = text.partition(":")
        return time(int(hour), int(minute or 0))
    except ValueError:
        return None


def _date(value: Any) -> Optional[date]:
    text = _text(value).replace("/", "-").replace(".", "-")
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(text[:10] if fmt == "%Y-%m-%d" else text, fmt).date()
        except ValueError:
            continue
    return None


WEEKDAYS = "月火水木金土日"
#: まとめた言い方 → 曜日(0=月)
WEEKDAY_GROUPS = {"平日": (0, 1, 2, 3, 4), "土日": (5, 6), "毎日": tuple(range(7))}


def _weekdays(value: Any) -> Optional[frozenset[int]]:
    """「月,水,金」「平日」「月曜」「月～金」→ 曜日(0=月)。空は空(毎日)。読めなければ None。"""
    text = _text(value)
    if not text:
        return frozenset()
    out: set[int] = set()
    for part in _list(text):
        part = part.replace("曜日", "").replace("曜", "")
        if part in WEEKDAY_GROUPS:
            out.update(WEEKDAY_GROUPS[part])
            continue
        for sep in ("～", "~", "-", "〜"):
            if sep in part:
                a, _, b = part.partition(sep)
                if a in WEEKDAYS and b in WEEKDAYS and a and b:
                    i, j = WEEKDAYS.index(a), WEEKDAYS.index(b)
                    out.update(range(i, j + 1) if i <= j else [*range(i, 7), *range(0, j + 1)])
                    break
                return None
        else:
            if len(part) >= 1 and all(ch in WEEKDAYS for ch in part):
                out.update(WEEKDAYS.index(ch) for ch in part)   # 「月水金」も
            else:
                return None
    return frozenset(out)


def _shift(name: str) -> str:
    """「2」「２直」→「2直」。"""
    name = _text(name)
    return f"{name}直" if name.isdigit() else name


def parse(rows: Iterable[dict[str, Any]]) -> list[Notice]:
    """表の行 → 案内。**有効でない行は入れない。** 読めない欄がある行は `problems` 付きで返す
    (`due` は出さない。設定の画面などで知らせる)。"""
    out: list[Notice] = []
    for index, row in enumerate(rows, start=1):
        get = {(_text(k)): v for k, v in row.items()}
        if not _flag(get.get("有効"), True):
            continue
        text = str(get.get("表示文") or "").strip()
        problems: list[str] = []
        if not text:
            problems.append("表示文が空です")
        timing = _text(get.get("タイミング")) or TIMING_START
        if timing not in TIMINGS:
            problems.append(f"タイミング「{timing}」は使えません({' / '.join(TIMINGS)})")
        at = _time(get.get("時刻"))
        minutes = _int(get.get("分"))
        if timing == TIMING_AT and at is None:
            problems.append("タイミングが「時刻」なのに、時刻が 14:30 の形で入っていません")
        if timing == TIMING_BEFORE_END and (minutes is None or minutes < 0):
            problems.append("タイミングが「直の終わり前」なのに、分が入っていません")
        start, end = get.get("開始日"), get.get("終了日")
        weekdays = _weekdays(get.get("曜日"))
        if weekdays is None:
            problems.append("曜日が読めません(月,水,金 / 平日 / 土日 の形)")
            weekdays = frozenset()
        if _text(start) and _date(start) is None:
            problems.append("開始日が 2026-11-09 の形ではありません")
        if _text(end) and _date(end) is None:
            problems.append("終了日が 2026-11-09 の形ではありません")
        size = _text(get.get("大きさ")) or SIZE_LARGE
        color = _text(get.get("色")) or COLOR_CAUTION
        out.append(Notice(
            id=_text(get.get("ID")) or f"{index}行目",
            text=text,
            question=str(get.get("確認の文") or "").strip(),
            button=_text(get.get("ボタン")) or DEFAULT_BUTTON,
            lines=_list(get.get("ライン")),
            shifts=frozenset(_shift(s) for s in _list(get.get("直"))),
            weekdays=weekdays,
            timing=timing, at=at, minutes=minutes,
            size=size if size in (SIZE_LARGE, SIZE_MEDIUM, SIZE_SMALL) else SIZE_LARGE,
            color=color if color in (COLOR_CAUTION, COLOR_WARNING, COLOR_INFO) else COLOR_CAUTION,
            pick_worker=_flag(get.get("名前を選ぶ"), True),
            carry=_flag(get.get("過ぎても出す"), True),
            start=_date(start), end=_date(end),
            order=_int(get.get("並び")) or 0,
            problems=tuple(problems)))
    return out


# ----------------------------------------------------------------------
# 直の1回ぶん(v4.47.2)── いまの直と、過ぎた直をまとめて見る
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class Slot:
    """直の1回ぶん(報告日・直・始まり・終わり)。"""
    report_date: str
    business: date
    shift: str
    start: datetime
    end: datetime

    @property
    def key(self) -> tuple[str, str]:
        return (self.report_date, self.shift)


def applies(notice: Notice, line: str, slot: Slot) -> bool:
    """その直のその回に、この案内を出す決まりか(押したかは見ない)。"""
    return (not notice.problems and bool(line)
            and (not notice.lines or line in notice.lines)
            and (not notice.shifts or slot.shift in notice.shifts)
            and (not notice.weekdays or slot.business.weekday() in notice.weekdays)
            and (notice.start is None or slot.business >= notice.start)
            and (notice.end is None or slot.business <= notice.end))


def point(notice: Notice, slot: Slot) -> datetime:
    """その回で出し始める時刻。"""
    if notice.timing == TIMING_AT and notice.at is not None:
        at = datetime.combine(slot.start.date(), notice.at)
        return at + timedelta(days=1) if at < slot.start else at
    if notice.timing == TIMING_BEFORE_END and notice.minutes is not None:
        return slot.end - timedelta(minutes=notice.minutes)
    return slot.start


@dataclass(frozen=True)
class Hit:
    notice: Notice
    slot: Slot
    #: 過ぎた直のぶんを、いまの直で出している
    carried: bool = False


def _known(notice: Notice, slot: Slot, since: Optional[dict[str, datetime]]) -> bool:
    """その回のうちに、この案内はもうあったか(**案内を作る前の回は数えない** ── v4.47.4)。"""
    first = (since or {}).get(notice.id)
    return first is None or first < slot.end


def due_all(notices: Iterable[Notice], *, line: str, now: datetime, slots: Iterable[Slot],
            acked: Iterable[tuple[str, str, str]] = (),
            since: Optional[dict[str, datetime]] = None) -> list[Hit]:
    """いま出す案内。`slots` はいまの前後の直の回(`shift_slots`)、`acked` は押した (案内ID, 報告日, 直)。

    案内ごとに見る:
        その案内の直の回のうち、いまがその中にある回 → その回のぶん(時刻が来ていて、押していなければ)
        いまがどの回の中でもない(「過ぎても出す」・直を決めてある案内だけ)
            → いちばん近い**過ぎた**回のぶん(押されていなければ。案内を作る前の回は数えない)
    過ぎたぶんを先に出す(古いほうから片付ける)。
    """
    done = set(acked)
    slots = list(slots)
    hits: list[Hit] = []
    for notice in notices:
        names = notice.shifts or MAIN_SHIFTS
        mine = [s for s in slots if s.shift in names and applies(notice, line, s)]
        now_in = [s for s in mine if s.start <= now < s.end]
        if now_in:
            slot = max(now_in, key=lambda s: s.start)
            if point(notice, slot) <= now and (notice.id, *slot.key) not in done:
                hits.append(Hit(notice, slot))
            continue
        if not notice.carry or not notice.shifts:
            continue
        past = sorted((s for s in mine if s.end <= now and _known(notice, s, since)),
                      key=lambda s: s.start)
        if past and (notice.id, *past[-1].key) not in done:
            hits.append(Hit(notice, past[-1], carried=True))
    return sorted(hits, key=lambda h: (not h.carried, h.notice.order, h.slot.start, h.notice.id))


def windows(notices: Iterable[Notice], *, line: str, now: datetime,
            slots: Iterable[Slot], since: Optional[dict[str, datetime]] = None
            ) -> list[tuple[Notice, Slot]]:
    """その期間に出した(出すはずだった)案内の回。押されていない一覧のもと(新しい回が先)。
    案内を作る前の回は入れない(`since`)。"""
    out = [(n, s) for s in slots for n in notices
           if applies(n, line, s) and s.start <= now and point(n, s) <= now
           and _known(n, s, since)]
    return sorted(out, key=lambda x: (x[1].start, x[0].order), reverse=True)


# ----------------------------------------------------------------------
# 直の時刻(伝送用ファイルの「時間用」。読めなければ既定)
# ----------------------------------------------------------------------
#: 直を決めていない案内が見る直(日勤は1直・2直と重なるので入れない)
MAIN_SHIFTS = frozenset({"1直", "2直", "3直"})
#: 「時間用」の 直 の欄 → 直の名前
SHIFT_KEYS = {"1": "1直", "2": "2直", "3": "3直", "昼": "日勤"}
#: 既定の直の時刻(日報の既定と同じ)
DEFAULT_TIMES: dict[str, tuple[time, time]] = {
    "1直": (time(7, 0), time(15, 0)), "2直": (time(15, 0), time(22, 50)),
    "3直": (time(22, 50), time(7, 0)), "日勤": (time(7, 0), time(17, 0)),
}


def shift_times(rows: Iterable[dict[str, Any]]) -> dict[str, tuple[time, time]]:
    """「時間用」の行(直・開始・終了)→ 直の時刻。読めない直は既定のまま。"""
    out = dict(DEFAULT_TIMES)
    for row in rows:
        get = {_text(k): v for k, v in row.items()}
        name = SHIFT_KEYS.get(_text(get.get("直")))
        start, end = _time(get.get("開始")), _time(get.get("終了"))
        if name and start is not None and end is not None:
            out[name] = (start, end)
    return out


def report_date(day: date) -> str:
    """日報の報告日の書き方(2026年11月9日)。押した記録は日報と同じ字で残す。"""
    return f"{day.year}年{day.month}月{day.day}日"


def shift_slots(times: dict[str, tuple[time, time]], now: datetime, *, back_days: int = 8,
                ahead_days: int = 1) -> list[Slot]:
    """いまの前後の直の回。報告日は**始めた日**(3直は日をまたいでも始めた日 ── 日報と同じ)。"""
    out: list[Slot] = []
    for offset in range(-back_days, ahead_days + 1):
        day = now.date() + timedelta(days=offset)
        for name, (start_t, end_t) in times.items():
            start = datetime.combine(day, start_t)
            end = datetime.combine(day, end_t)
            if end <= start:
                end += timedelta(days=1)
            out.append(Slot(report_date(day), day, name, start, end))
    return out

