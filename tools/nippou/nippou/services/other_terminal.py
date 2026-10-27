"""同じ直を、ほかの端末でも打っている ── **知らせだけ**(v4.44.10)

    同じラインの2台目の端末で入力すると、1台目の行を知らせずに上書きする → 知らせを出す
    管理者が入力途中の直を(記録を見るなどから)呼び出すと、入力中の直しが消える → 知らせを出す

【何が起きていたか】
控え(LocalBackup)は「保存した時刻の新しいほうが正」です(`services/local_backup`)。同じ直を
2台で打つと、**あとから保存したほうのページで、先に保存したほうのページが丸ごと置き換わり**
ます(どちらの画面にも何も出ません)。共有へ保存しても同じで、送ったほうの中身になります。

    2台目(B)の端末 … 1台目(A)が 09:00 に保存した行は B の画面に無い。B が保存すると、
                      控えと共有の A の行が B の中身に置き換わる
    管理者のPC      … 控えから呼び出して直して保存しても、ラインの端末が次に保存すると、
                      直した中身はラインの端末の中身に置き換わる(**消えるのは管理者の直し**)

止めはしません(利用者の決定:「知らせを出す」)。どちらの端末で打つかを決めるのは人です。

【どこで見分けるか】
控えのページは、最後に保存した端末(PC名 `daily_header.saved_pc`)と中身の指紋を持っています。
手元のページは、**最後に控えと揃えたときの中身の指紋**(`base_hash`)を持っています(控えから
受け取った・控えへ写したときに付ける)。控えのページが

    ほかの端末の保存で、指紋が 手元のいまの中身 とも 揃えたときの中身 とも違う

なら、**この端末の知らない行がある** ── 保存すると置き換えます。時刻では比べません(端末ごとの
時計がずれていても見分けられるように)。

    1台だけで使っている            … 控えのページはこの端末の保存 → 出さない
    同じ端末で続けて打つ           … 同じ
    前の直・止まった端末から引き継ぐ … 起動のときに控えから受け取っている(揃えた指紋が同じ)→ 出さない
    前の版が書いた控え(PC名が空)   … 誰のものか分からない → 出さない

【いつ見るか】
控えは共有のフォルダにあるので、**打つたびには読みません**(欄を離れるたびに読みに行くと、
共有が遅い・届かないときに打つ手が止まる)。読むのは

    入力画面を開いたとき(`/`)・保存の前(自動保存も)・1分ごとの見張り(`/api/shift/key`)

で、見た答えを覚えておき、ほかの応答(欄を離れたとき・ロットを引いたとき)は覚えた答えを出します。
控えへ写すときに、ほかの端末の知らない中身を実際に置き換えたら、それも覚えます
(`local_backup.flush` → `note_replaced`)。

【管理者が呼び出したとき】
呼び出している(`recall`)直が、**まだ閉じていない直**(`ShiftCalculator.input_close_at` の前)で、
控えのページがほかの端末の保存なら、「いま ○○ の端末で入力中です」と出します。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from ..db.repository import this_pc
from ..logging_setup import get_logger

log = get_logger("services.other_terminal")

ShiftKey = tuple[str, str, str]

#: 覚えておく直の数(開いている直は1つか2つ。古いものから捨てる)
KEEP = 64


@dataclass(frozen=True)
class Page:
    """控えにある、ほかの端末が保存したページ。"""

    page: int
    pc: str
    saved_at: str


@dataclass
class Seen:
    """控えを読んで分かったこと(その直)。"""

    #: この端末の知らない中身のページ(保存すると置き換える)
    unknown: list[Page] = field(default_factory=list)
    #: ほかの端末が最後に保存したページ(中身が同じでも。管理者の「入力中です」用)
    latest_other: Optional[Page] = None
    at: float = 0.0


@dataclass
class Replaced:
    """この端末の中身で、ほかの端末のページを置き換えた。"""

    pc: str
    saved_at: str
    pages: set[int] = field(default_factory=set)
    at: float = 0.0


_lock = threading.Lock()
_seen: dict[ShiftKey, Seen] = {}
_replaced: dict[ShiftKey, Replaced] = {}


def reset() -> None:
    """覚えたことを忘れる(試験用)。"""
    with _lock:
        _seen.clear()
        _replaced.clear()


def _keep(store: dict, key: ShiftKey, value) -> None:
    store.pop(key, None)
    store[key] = value
    while len(store) > KEEP:
        del store[next(iter(store))]


# ----------------------------------------------------------------------
# 見分け
# ----------------------------------------------------------------------
def unknown_to_me(pc: str, theirs_hash: str, my_hash: str, my_base: str) -> bool:
    """控えのページ(`pc` が保存・指紋 `theirs_hash`)が、**この端末の知らない中身**か。"""
    if not pc or pc == this_pc() or not theirs_hash:
        return False
    return theirs_hash not in {my_hash or "", my_base or ""}


def scan(repo, report_date: str, line: str, shift: str) -> Optional[Seen]:
    """控えのその直を読み、ほかの端末のページを覚える。**読めなければ前の答えのまま。**"""
    from . import local_backup

    key = (report_date, line, shift)
    try:
        if local_backup.backing_off() or not local_backup.reachable():
            return None
        mine = {int(r[0]): (str(r[1] or ""), str(r[2] or "")) for r in repo.conn.execute(
            "SELECT page, content_hash, base_hash FROM daily_header"
            " WHERE report_date=? AND line=? AND shift=?", key)}
        seen = Seen(at=time.monotonic())
        me = this_pc()
        with local_backup.reader(line) as backup:
            if backup is not None:
                columns = {r[1] for r in backup.conn.execute('PRAGMA table_info("daily_header")')}
                if "saved_pc" in columns:
                    for r in backup.conn.execute(
                            "SELECT page, saved_pc, saved_at, content_hash FROM daily_header"
                            " WHERE report_date=? AND line=? AND shift=? ORDER BY page", key):
                        pc = str(r[1] or "")
                        if not pc or pc == me:
                            continue
                        found = Page(int(r[0]), pc, str(r[2] or ""))
                        if seen.latest_other is None or found.saved_at > seen.latest_other.saved_at:
                            seen.latest_other = found
                        my_hash, my_base = mine.get(found.page, ("", ""))
                        if unknown_to_me(pc, str(r[3] or ""), my_hash, my_base):
                            seen.unknown.append(found)
    except Exception:                             # noqa: BLE001 - 知らせが出ないだけ
        log.exception("控えでほかの端末のページを確かめられませんでした %s", key)
        return None
    if seen.unknown:
        log.info("ほかの端末が保存した、この端末の知らないページがあります %s %s", key,
                 [(p.page, p.pc, p.saved_at) for p in seen.unknown])
    with _lock:
        _keep(_seen, key, seen)
    return seen


def note_replaced(page_key: tuple, pc: str, saved_at: str) -> None:
    """控えへ写すときに、ほかの端末の知らない中身をこの端末の中身で置き換えた。"""
    key = tuple(page_key[:3])
    with _lock:
        got = _replaced.get(key)
        if got is None or got.pc != pc:
            got = Replaced(pc=pc, saved_at=saved_at)
        got.saved_at = max(got.saved_at, saved_at)
        got.pages.add(int(page_key[3]))
        got.at = time.monotonic()
        _keep(_replaced, key, got)


# ----------------------------------------------------------------------
# 画面に出す一言
# ----------------------------------------------------------------------
def _when(saved_at: str, now: datetime) -> str:
    """'2026-10-05T09:00:00' → '09:00'(今日)/ '10/4 22:30'(ほかの日)。"""
    try:
        at = datetime.fromisoformat(saved_at)
    except ValueError:
        return saved_at
    if at.date() == now.date():
        return f"{at:%H:%M}"
    return f"{at.month}/{at.day} {at:%H:%M}"


def _open_now(calc, report_date: str, shift: str, now: datetime) -> bool:
    """その直は、まだ打てる時間か(閉じる時刻の前)。"""
    from ..logic.shift import parse_business_date

    day = parse_business_date(report_date)
    if calc is None or day is None:
        return False
    close = calc.input_close_at(shift, day)
    return close is not None and now < close


def notice(report_date: str, line: str, shift: str, *, recall: bool = False,
           calc=None, now: Optional[datetime] = None) -> dict[str, Any]:
    """覚えた答えから、入力画面の帯の一言を作る(**読みには行かない**)。"""
    now = now or datetime.now()
    key = (report_date, line, shift)
    with _lock:
        seen = _seen.get(key)
        replaced = _replaced.get(key)
    hidden = {"shown": False, "kind": "", "pc": "", "at": "", "text": ""}
    if seen is not None and seen.unknown and (replaced is None or seen.at > replaced.at):
        latest = max(seen.unknown, key=lambda p: p.saved_at)
        at = _when(latest.saved_at, now)
        return {"shown": True, "kind": "conflict", "pc": latest.pc, "at": at,
                "text": (f"この直は ほかの端末({latest.pc})でも入力されています({at} に保存)。"
                         "この端末で保存すると、向こうで打った行をこの画面の中身で置き換えます。"
                         "どちらの端末で打つか、向こうの人と決めてください。")}
    if replaced is not None:
        at = _when(replaced.saved_at, now)
        return {"shown": True, "kind": "replaced", "pc": replaced.pc, "at": at,
                "text": (f"この直の、ほかの端末({replaced.pc}・{at} に保存)で打った行を、"
                         "この端末の中身で置き換えました。向こうで打った行が要るなら、"
                         "向こうの人と確かめてください。")}
    if recall and seen is not None and seen.latest_other is not None \
            and _open_now(calc, report_date, shift, now):
        other = seen.latest_other
        at = _when(other.saved_at, now)
        return {"shown": True, "kind": "busy", "pc": other.pc, "at": at,
                "text": (f"この直は いま {other.pc} の端末で入力中です(最後の保存 {at})。"
                         "ここで直して保存しても、向こうが続けて保存すると、ここで直した中身は"
                         "向こうの中身で置き換わります。直すのは直が終わってからにするか、"
                         "向こうの人に伝えてください。")}
    return hidden
