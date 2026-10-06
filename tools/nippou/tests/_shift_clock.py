"""直の時刻を、**その日の中に**置く ── 時計に依るテストの下ごしらえ

【なぜ要るのか】
直の境界を試すテストは、時計を止められないので**境界のほうを動かします。**
素直に書くと `いま ± 30分` のような窓になりますが、これは真夜中の前後で
日をまたぎます。`TimeCheck` が日をまたげるのは3直だけなので
(`nippou/logic/shift.time_check`)、またいだ窓はどの直にも当たらず、
**夜中に走らせたときだけテストが落ちます。**

    00:24 に `いま ± 30分` → 「23:54〜00:54」
      → 1直にも2直にも当たらない → 受け皿の3直へ落ちる
      → 「1直のはずが3直」で落ちる

走らせる時刻で結果が変わるテストは、そのうち誰も赤を見なくなります。
**赤は本物だけにします。**

【同じ下ごしらえを3か所に持たない】
`test_shift_boundary` `test_shift_anchor` `test_shift_closing` が、それぞれ
似て非なる窓の作り方を持っていました(そして3つとも夜中に落ちました)。
ここ1か所に寄せます。

【どう置くか】
窓は**必ずその日の中**(00:00〜23:59)に収めます。端に寄って入り切らない
ときは、**幅を縮めます** ── 直の長さを試したいのではなく、位置を試して
いるので、縮めても試したいことは変わりません。それでも作れないとき
(真夜中の1分など)は `None` を返し、呼ぶ側が**理由を言って飛ばします。**
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

#: 直の並び。`TimeCheck` の見る順とは別で、**1つ前**を決めるためのもの
ORDER = ("1", "2", "3")

#: 窓と窓のあいだに置く最小の幅。0分の窓は「無い」のと同じ
MINUTE = timedelta(minutes=1)


def hhmm(when: datetime) -> str:
    return when.strftime("%H:%M")


def day_bounds(now: datetime) -> tuple[datetime, datetime]:
    """その日の端。**23:59 まで** ── `TimeCheck` が見る上限に合わせる。"""
    return (now.replace(hour=0, minute=0, second=0, microsecond=0),
            now.replace(hour=23, minute=59, second=0, microsecond=0))


def window_around(now: datetime, *, head: int, tail: int,
                  min_width: int = 0) -> Optional[tuple[datetime, datetime]]:
    """`now` を含む窓。**日をまたがない。**

        head … 窓の始まりから `now` までの分数(「直に入って何分か」)
        tail … `now` から窓の終わりまでの分数

    端に当たったら、その側だけ詰めます。**1分は必ず前に空けます** ──
    そこへ「1つ前の直」を置くので(`chain`)、0分では置けません。
    """
    day_start, day_end = day_bounds(now)
    start = max(day_start + MINUTE, now - timedelta(minutes=head))
    end = min(day_end, now + timedelta(minutes=tail))
    if not (start < now < end):
        return None
    if min_width and (end - start) < timedelta(minutes=min_width):
        return None
    return (start, end)


def window_before(now: datetime, *, minutes_ago: int, width: int
                  ) -> Optional[tuple[datetime, datetime]]:
    """`minutes_ago` 分前に**終わった**窓。その日の中に収める。

    「その直はとっくに終わっています」を作るためのもの。終わりが今日の
    うちに無ければ(真夜中すぐ)作れません。
    """
    day_start, _ = day_bounds(now)
    end = now - timedelta(minutes=minutes_ago)
    if end <= day_start:
        return None
    start = max(day_start, end - timedelta(minutes=width))
    if start >= end:
        return None
    return (start, end)


def chain(now: datetime, key: str, *, head: int, tail: int,
          width: int = 60, min_width: int = 0, prev_min_width: int = 0
          ) -> Optional[dict[str, tuple[datetime, datetime]]]:
    """3直ぶんの窓。`now` は `key` の直の中に入り、**1つ前は終わっている。**

    置き方は3つの決まりだけです。

        1. `key` の窓は `now` を含む(`window_around`)
        2. **1つ前の直は、その真ん前**に置く ── 「たった今終わった」形。
           ここが空くと「終わった直」の話ができません
        3. 残りの1直は、後ろに置く。後ろが無ければ前へ回す。
           **どこでもよい** ── `TimeCheck` は当たり外れしか見ないので、
           1日の並び順に意味はありません(現実の直とは違うところ)

    幅は「そうしたい値」で、端では縮みます。`min_width`(いまの直)と
    `prev_min_width`(1つ前の直)を渡すと、そこまで縮んだら作れないものと
    して `None` を返します ── **打つ紙より短い直には入りません。**
    保存前チェックが「作業時間が直の規定時間を超えています」で断るので、
    直の長さは紙の長さより短くできません(`logic/work_time.shift_limit`)。
    """
    if key not in ORDER:
        raise ValueError(f"知らない直です: {key}")
    day_start, day_end = day_bounds(now)
    here = window_around(now, head=head, tail=tail, min_width=min_width)
    if here is None:
        return None
    start, end = here

    room_before = start - day_start
    if room_before < MINUTE:
        return None
    index = ORDER.index(key)
    prev = ORDER[(index - 1) % len(ORDER)]
    rest = ORDER[(index - 2) % len(ORDER)]

    prev_width = min(timedelta(minutes=width), room_before)
    if prev_min_width and prev_width < timedelta(minutes=prev_min_width):
        return None
    prev_window = (start - prev_width, start)
    windows = {key: (start, end), prev: prev_window}

    room_after = day_end - end
    if room_after >= MINUTE:
        windows[rest] = (end, end + min(timedelta(minutes=width), room_after))
    else:
        left = prev_window[0] - day_start
        if left < MINUTE:
            return None
        windows[rest] = (prev_window[0] - min(timedelta(minutes=width), left),
                         prev_window[0])
    return windows


def write(repo, windows: dict[str, tuple[datetime, datetime]], *,
          day: tuple[str, str] = ("08:00", "18:00")) -> None:
    """作った窓を時間マスタへ書く。昼勤はぶつからない位置に置いておく。"""
    for key, (start, end) in windows.items():
        repo.set_shift_time(key, hhmm(start), hhmm(end))
    repo.set_shift_time("昼", day[0], day[1])
