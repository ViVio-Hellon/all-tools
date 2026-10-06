"""サーバ側のフォルダ参照(設定画面の「参照...」)

設定画面で共有 DB の置き場所を決めるために、**サーバ(=このPC)から見える**
フォルダを一覧する。

【なぜ要るのか】
ブラウザのファイル選択ダイアログは**クライアント側**のパスしか返しません。
アプリが読むのはサーバから見たパスなので、選ばせても意味がありません
(tkinter 版は同じプロセスの中に画面があったので ``askopenfilename`` が
そのまま使えていました。Web 化でここが噛み合わなくなります)。

直接打ってもよく、打ったパスがどう見えているかをその場で返します。

【何を返すか / 返さないか】

* **フォルダの名前**と、そこにある **共有 DB ファイルの名前**だけ
* ファイルの中身は返しません。読み取り口をここに作らない

一覧そのものが目的なので「上のフォルダへ行けないようにする」たぐいの制限は
付けません ── それでは置き場所を探せません。守りは ``app/__init__.py`` の
**127.0.0.1 バインド + 起動トークン + Host 検証 + 同一オリジン確認**が担います。

代わりに、**名前しか出さない**ことと**件数に上限を置く**ことで、ここが
「サーバの中を読む窓口」にならないようにしてあります。
"""

from __future__ import annotations

import string
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import applog
from ..db.shared import SUFFIXES

log = applog

#: 1 回に返す上限。共有フォルダには数千の項目があることがあるので、
#: 全部返すと画面も JSON も重くなる
MAX_ENTRIES = 300

#: 探しているファイル。これ以外の名前は出さない
SOURCE_SUFFIXES = SUFFIXES

#: 変換前の Access ファイルも見えるようにする ── 移行の途中は「.accdb は
#: あるが .sqlite3 がまだ無い」状態になり、そこで何も見えないと
#: 「フォルダを間違えた」のか「変換がまだ」なのかが分からない
LEGACY_SUFFIXES = (".accdb", ".mdb")


@dataclass
class Entry:
    name: str
    path: str
    is_dir: bool = True
    legacy: bool = False
    """変換前の Access ファイル(選べないが、あることは見せる)。"""


@dataclass
class BrowseView:
    """いま見ているフォルダ。"""

    path: str = ""
    parent: str = ""
    """上のフォルダ(無ければ空)。"""

    exists: bool = False
    readable: bool = False
    message: str = ""
    dirs: list[Entry] = field(default_factory=list)
    files: list[Entry] = field(default_factory=list)
    truncated: bool = False
    roots: list[Entry] = field(default_factory=list)
    """一覧の出発点。Windows のドライブなど。"""

    picked: str = ""
    """ファイルを指されたとき、その名前(入れ物を開いたうえで印を付ける)。"""


def browse(path_text: str) -> BrowseView:
    """``path_text`` のフォルダを一覧する。空なら出発点だけを返す。"""
    view = BrowseView(path=path_text, roots=_roots())
    text = (path_text or "").strip()
    if not text:
        view.message = "フォルダを選んでください。"
        return view

    path = Path(text)

    # ファイルを指されたら、その入れ物を開く。「参照」で目当てのファイルを
    # 見つけたとき、いちいち親へ戻らせない
    try:
        if path.exists() and not path.is_dir():
            view.picked = path.name
            path = path.parent
    except OSError:
        pass

    view.path = str(path)
    try:
        view.parent = str(path.parent) if path.parent != path else ""
    except OSError:
        view.parent = ""

    try:
        exists = path.exists()
    except OSError as exc:
        # 切断された共有フォルダなど。存在確認そのものが失敗しうる
        view.message = f"このパスを確認できません({exc.strerror or exc})。"
        return view

    if not exists:
        view.message = "そのフォルダはこの PC から見えません。"
        return view
    view.exists = True

    if not path.is_dir():  # pragma: no cover - 上で畳んでいる
        view.message = "フォルダではありません。"
        return view

    try:
        entries = sorted(path.iterdir(), key=lambda p: p.name.lower())
    except OSError as exc:
        # 権限が無い・切断された共有フォルダなど。理由を出す
        log.info("フォルダを読めません(%s): %s", path, exc)
        view.message = f"このフォルダを読めません({exc.strerror or exc})。"
        return view

    view.readable = True
    for entry in entries:
        if len(view.dirs) + len(view.files) >= MAX_ENTRIES:
            view.truncated = True
            break
        try:
            is_dir = entry.is_dir()
        except OSError:  # 壊れたリンクなど
            continue
        if is_dir:
            view.dirs.append(Entry(name=entry.name, path=str(entry), is_dir=True))
            continue
        suffix = entry.suffix.lower()
        if suffix in SOURCE_SUFFIXES:
            view.files.append(Entry(name=entry.name, path=str(entry), is_dir=False))
        elif suffix in LEGACY_SUFFIXES:
            view.files.append(
                Entry(name=entry.name, path=str(entry), is_dir=False, legacy=True)
            )

    view.message = _summary(view)
    if view.truncated:
        view.message += f"(先頭 {MAX_ENTRIES} 件まで表示しています)"
    return view


def _summary(view: BrowseView) -> str:
    usable = [f for f in view.files if not f.legacy]
    legacy = [f for f in view.files if f.legacy]
    if usable:
        return f"共有DB {len(usable)} 件が見つかりました。"
    if legacy:
        # 移行の途中。**次に何をすればよいか**まで言う
        return (
            f"変換前の Access ファイルが {len(legacy)} 件あります。"
            "tools/accdb_to_sqlite.py で変換してください。"
        )
    if not view.dirs:
        return "このフォルダは空です。"
    return "このフォルダに共有DBはありません。"


def _roots() -> list[Entry]:
    """一覧の出発点。

    Windows は接続されているドライブ、それ以外は ``/`` とホーム。
    「どこから始めればよいか分からない」を作らない。
    """
    roots: list[Entry] = []
    if sys.platform.startswith("win"):
        for letter in string.ascii_uppercase:
            drive = Path(f"{letter}:\\")
            try:
                if drive.exists():
                    roots.append(Entry(name=f"{letter}:", path=str(drive)))
            except OSError:
                continue
    else:
        for candidate in (Path("/"), Path.home()):
            roots.append(Entry(name=str(candidate), path=str(candidate)))
    return roots


def to_dict(view: BrowseView) -> dict[str, Any]:
    return {
        "path": view.path,
        "parent": view.parent,
        "exists": view.exists,
        "readable": view.readable,
        "message": view.message,
        "dirs": [_entry(e) for e in view.dirs],
        "files": [_entry(e) for e in view.files],
        "truncated": view.truncated,
        "roots": [_entry(e) for e in view.roots],
        "picked": view.picked,
    }


def _entry(entry: Entry) -> dict[str, Any]:
    return {
        "name": entry.name,
        "path": entry.path,
        "is_dir": entry.is_dir,
        "legacy": entry.legacy,
    }
