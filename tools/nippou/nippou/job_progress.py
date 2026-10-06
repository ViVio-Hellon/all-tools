"""いま走っている仕事(取り込み・共有へ保存・マスタを直す)の、進み具合(プロセスに1つ)

    件数が多いときはプログレス表示させてください

取り込みは1回の要求で最後までやります(**途中で切らない** ── 途中まで
入った日報を残さないため)。何百ページもあると何十秒もかかるので、
そのあいだ画面には何も返せません。

そこで、**進み具合だけ別に置いて**、画面が見に来られるようにします:

    POST /api/settings/import/upload   … 取り込み本体(終わるまで返らない)
    GET  /api/settings/import/progress … いまどこか(何度でも聞ける)

waitress はスレッドプールで動く(`server.py` の `threads=8`)ので、
取り込みが1本のスレッドを占めていても、進み具合は別のスレッドが返せます。

【なぜ「1つ」でよいのか】
取り込めるのは管理者だけで、しかもこの端末は**1人が1画面**で使います
(多重起動は `logic/single_tab` が止めます)。同時に2つ走ることを
考えるより、**走っているものを1つだけ正しく出す**ほうが確かです。

【この層の約束】
言葉を作るのは `logic/progress.py`(純粋)。ここは**数を持つだけ**です。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator, Optional

from .logic.progress import JOB_IMPORT, Progress

_lock = threading.Lock()
_now: Optional[Progress] = None


def snapshot() -> Progress:
    """いまの進み具合。走っていなければ「走っていない」を返します。"""
    with _lock:
        return _now or Progress()


def start(*, file_count: int = 1, job: str = JOB_IMPORT) -> None:
    """仕事の始まり。**画面が見に来る前に立てておきます。**

    `job` は何の仕事か(`logic/progress.JOB_*`)── 取り込み・共有へ保存・
    マスタを直す。段の並びと見出しが変わります。
    """
    with _lock:
        global _now
        _now = Progress(running=True, job=job,
                        file_count=max(1, int(file_count)))


def step(*, phase: str = "", done: Optional[int] = None,
         total: Optional[int] = None, label: Optional[str] = None,
         file_no: Optional[int] = None,
         file_name: Optional[str] = None) -> None:
    """1つ進んだ。**渡さなかったものは、そのまま残します。**

    段が変わったとき(`phase` だけ渡す)は、数を0から数え直します ──
    前の段の「340/340」が次の段の頭に残ると、終わったように見えます。
    """
    with _lock:
        global _now
        if _now is None or not _now.running:
            return                                # 始まっていない(何もしない)
        changed = bool(phase) and phase != _now.phase
        _now = Progress(
            running=True,
            job=_now.job,
            phase=phase or _now.phase,
            done=(0 if changed and done is None else
                  (_now.done if done is None else int(done))),
            total=(0 if changed and total is None else
                   (_now.total if total is None else int(total))),
            label=("" if changed and label is None else
                   (_now.label if label is None else str(label))),
            file_no=_now.file_no if file_no is None else int(file_no),
            file_count=_now.file_count,
            file_name=_now.file_name if file_name is None else str(file_name),
        )


def finish() -> None:
    """終わり。**必ず下ろします**(下りないと棒が出たままになります)。"""
    with _lock:
        global _now
        _now = None


@contextmanager
def watching(*, file_count: int = 1, job: str = JOB_IMPORT) -> Iterator[None]:
    """そのあいだ進み具合を出す。例外が出ても必ず下ろす。

        with job_progress.watching(file_count=3):
            ...取り込み...
        with job_progress.watching(job=progress.JOB_PUSH):
            ...共有へ保存...
    """
    start(file_count=file_count, job=job)
    try:
        yield
    finally:
        finish()


def reset() -> None:
    """テスト用。プロセスに1つの状態を戻します。"""
    finish()
