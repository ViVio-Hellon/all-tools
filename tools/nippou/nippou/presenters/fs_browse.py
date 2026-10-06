"""サーバから見えるフォルダを一覧する

【なぜ要るのか】
ブラウザのファイル選択ダイアログは**クライアント側**の道しか返しません。
このアプリは同じPCで動いているので実害は無さそうに見えますが、返るのは
`C:\\fakepath\\梱包資材マスタ.sqlite3` のような偽の道で、**フォルダは
選べません。**

参照パスは手で打っても構いませんが、共有フォルダの長い道を打ち間違えると
「見つかりません」だけが返ってきて、どこで間違えたのか分かりません。
一段ずつ辿れるようにしておくと、そこで気づけます。

【返すもの】
フォルダの名前と、そこにある**取り込み元らしいファイル**の名前だけ。
**中身は返しません** ── 読み取り口をここに作らない。

「上のフォルダへ行かせない」たぐいの制限は付けません(それでは置き場所を
探せません)。守りは `app/__init__.py` の 127.0.0.1 バインド + 起動
トークン + Host検証 + 同一オリジン確認です。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import app_config, source_db

# 1度に返す数の上限。共有フォルダには数千のファイルが入っていることがあり、
# 全部返すと画面が固まる
MAX_ENTRIES = 300


@dataclass
class BrowseView:
    path: str = ""
    exists: bool = False
    parent: str = ""
    dirs: list[str] = field(default_factory=list)
    files: list[dict] = field(default_factory=list)
    truncated: bool = False
    error: str = ""
    #: いま開いている場所を、頭から1段ずつ。**どこに居るのかを出す**
    crumbs: list[dict] = field(default_factory=list)
    #: この端末で開けるドライブ(C: / D: / 割り当てた共有)
    drives: list[dict] = field(default_factory=list)


def drives() -> list[dict]:
    """開けるドライブ。**「どのドライブを見ているか」から始められるように。**

    「フォルダから選択もできるように、どのドライブで何を開いたか
    分かるように」── 一段ずつ上へ辿るだけだと、共有(Z:)へ移りたいのに
    行き着けません。頭に並べておけば1回で移れます。

    Windows 以外(開発機)では `/` だけを返します ── 無い箱を出さない。
    """
    import string

    out: list[dict] = []
    if os.name != "nt":
        return [{"name": "/", "path": "/"}]
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        try:
            if Path(root).is_dir():
                out.append({"name": f"{letter}:", "path": root})
        except OSError:                              # 抜かれたメディア等
            continue
    return out


def _crumbs(target: Path) -> list[dict]:
    """頭から1段ずつの道。押せば途中まで戻れる。"""
    out: list[dict] = []
    parts = list(target.parts)
    for index in range(len(parts)):
        here = Path(*parts[:index + 1])
        name = parts[index].rstrip("\\/") or str(here)
        out.append({"name": name, "path": str(here)})
    return out


def browse(text: str) -> BrowseView:
    """そのフォルダの中を返す。**例外は投げない。**

    空で呼ばれたらアプリのフォルダから始めます ── 共有に届かない端末でも
    必ず何かが出るところ。
    """
    raw = (text or "").strip().strip('"')
    target = Path(raw).expanduser() if raw else app_config.APP_ROOT
    view = BrowseView(path=str(target), drives=drives(),
                      crumbs=_crumbs(target))

    try:
        view.exists = target.is_dir()
    except OSError as exc:                           # 共有に届かない
        view.error = str(exc)
        return view
    if not view.exists:
        view.error = "フォルダがありません"
        # **親は返す。** 打ち間違えたときに、一段戻って選び直せる
        view.parent = str(target.parent) if target.parent != target else ""
        return view

    view.parent = str(target.parent) if target.parent != target else ""
    try:
        entries = sorted(target.iterdir(), key=lambda p: p.name.lower())
    except OSError as exc:
        view.error = str(exc)
        return view

    for entry in entries:
        if len(view.dirs) + len(view.files) >= MAX_ENTRIES:
            view.truncated = True
            break
        try:
            if entry.is_dir():
                view.dirs.append(entry.name)
                continue
        except OSError:                              # 権限が無い等。飛ばす
            continue
        if entry.suffix.lower() in source_db.SUFFIXES + (".accdb", ".mdb"):
            try:
                size = entry.stat().st_size
            except OSError:
                size = 0
            view.files.append({"name": entry.name, "size": size})
    return view


def to_dict(view: BrowseView) -> dict[str, Any]:
    return vars(view)
