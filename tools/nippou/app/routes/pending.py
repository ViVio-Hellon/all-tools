"""まだ作っていない画面の「準備中」案内

【なぜ空振りさせないのか】
レールに並べておいて押すと404になると、押した人には「壊れている」と
しか見えない。まだ無いことと壊れていることは違うので、**無いなら無いと
言う画面**を出す(黙って何も起きない状態を作らない)。

登録するのは `shell.READY_SCREENS` に挙がっていない画面だけ。既に実装
済みのものを上書きしないよう、**業務の画面を全部登録したあと**に呼ぶ。
"""
from __future__ import annotations

from flask import Flask, render_template

from .. import shell


def register(app: Flask) -> None:
    for item in shell.pending_items():
        _register_one(app, item)


def _register_one(app: Flask, item: shell.NavItem) -> None:
    plan, gains, instead = shell.PENDING_SCREENS.get(
        item.key, ("", "", ""))

    def view(_item=item, _plan=plan, _gains=gains, _instead=instead):
        return render_template("pending.html",
                               plan=_plan, gains=_gains, instead=_instead,
                               title=_item.label,
                               **shell.shell_context(_item.key))

    app.add_url_rule(item.url, endpoint=f"pending_{item.key}", view_func=view)
