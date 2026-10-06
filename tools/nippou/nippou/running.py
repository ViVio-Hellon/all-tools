"""いま走っている、途中で止めてはいけない処理

【なぜ要るのか】
このアプリを終わらせる道は2つあります。

    利用者が押す       … `POST /api/shutdown`
    誰も見ていない     … `idle_exit`(画面を閉じた / 心拍が途切れた)

どちらも、**Accessへの書き戻しの途中で効いてはいけません。** 途中で
落ちると、ヘッダだけ送れて明細が送れていない状態が**共有のAccessに**
残ります。手元のSQLiteと違って、そこは他のラインも読む場所です。

【判断は1か所に置く】
「いま止めてよいか」を2か所で別々に決めると、片方だけ直した状態を
作ってしまいます。ここが唯一の出どころです。

【なぜ数を数えるのか】
waitress はスレッドプールで動くので、同じ種類の処理が重なることが
あります(押し間違い・二重送信)。真偽値だと、先に終わったほうが
「もう空いた」と言ってしまいます。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager

_lock = threading.Lock()
_counts: dict[str, int] = {}


@contextmanager
def running(label: str):
    """そのあいだ「止めてはいけない」と印を立てる。

        with running("Accessへの反映"):
            pusher.push_pending(...)

    **例外が出ても必ず下ろす。** 下りないと、そのプロセスは二度と
    自動で終われなくなり、次の起動が古いプロセスに合流し続けます。
    """
    with _lock:
        _counts[label] = _counts.get(label, 0) + 1
    try:
        yield
    finally:
        with _lock:
            left = _counts.get(label, 1) - 1
            if left > 0:
                _counts[label] = left
            else:
                _counts.pop(label, None)


def labels() -> list[str]:
    """いま走っているものの名前。画面と `/api/shutdown` に出す。"""
    with _lock:
        return sorted(_counts)


def busy() -> bool:
    """止めてはいけないものがあるか。"""
    with _lock:
        return bool(_counts)


def reset() -> None:
    """テスト用。"""
    with _lock:
        _counts.clear()
