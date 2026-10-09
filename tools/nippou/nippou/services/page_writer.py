"""日報のページを書く・消す ── **ここ 1 か所を通す。**

ページを書く経路は、画面の保存のほかに全停入力・CSV の取り込み・直の付け替え・空のページの
片付けがある。画面の 12 行を通さずに書いた(消した)ページは、そのページを開いたままの古い画面に
**上書きさせてはいけない**(自動保存が、取り込んだ行を古い中身で戻す・消したページを作り直す)。
以前はこの印(`written_aside`)を付けるのが全停入力だけで、ほかの経路は付け忘れていた
(「打った行が消える・値が戻る」の系統)。書く経路をここに通し、印の付け忘れを無くす。

- ``by_screen=True``  … 画面が出しているページを、その画面の中身で書く(保存・新しいページ・戻る前)
- ``by_screen=False`` … 画面の外から書く・消す。**印を付ける**(`app/routes/entry.screen_mismatch` が見る)
"""
from __future__ import annotations

import threading
from typing import Optional

#: 画面の外から書いた(消した)ページ ``(報告日, ライン, 直, ページ)`` → その時刻(`awake_clock`)
_WRITTEN_ASIDE: dict[tuple, float] = {}
_LOCK = threading.Lock()
#: 覚えるのは最近のぶんだけ(開いたままの画面が出しているのは最近のページ)
KEEP = 512


def note_written_aside(key: tuple) -> None:
    """画面の 12 行を通さずにページを書いた(消した)と覚える。"""
    from nippou import awake_clock

    with _LOCK:
        _WRITTEN_ASIDE.pop(tuple(key), None)
        _WRITTEN_ASIDE[tuple(key)] = awake_clock.now()
        while len(_WRITTEN_ASIDE) > KEEP:
            del _WRITTEN_ASIDE[next(iter(_WRITTEN_ASIDE))]


def written_aside_at(key: tuple) -> Optional[float]:
    """そのページを画面の外から最後に書いた時刻。書いていなければ None。"""
    with _LOCK:
        return _WRITTEN_ASIDE.get(tuple(key))


def reset() -> None:
    with _LOCK:
        _WRITTEN_ASIDE.clear()


def save_page(repo, header, details, *, by_screen: bool) -> None:
    """ページを書く。画面の外からなら印を付ける。"""
    repo.save(header, details)
    if not by_screen:
        note_written_aside((header.report_date, header.line, header.shift, header.page))


def delete_page(repo, report_date: str, line: str, shift: str, page: int, *, by_screen: bool = False):
    """ページを消す。**消したページも印を付ける**(古い画面の自動保存が作り直さないように)。"""
    removed = repo.delete_page(report_date, line, shift, page)
    if not by_screen:
        note_written_aside((report_date, line, shift, page))
    return removed
