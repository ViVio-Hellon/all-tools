"""サーバ側のフォルダ参照(VBA版 `SelectFolderDialog` の置き換え)

画面の「フォルダ設定」で、点検表ルートフォルダを選ぶために
**このPCから見えるフォルダ**を一覧する(python-web-tools の
`presenters/fs_browse.py` と同じ考え方)。

【なぜ tkinter のダイアログにしないか】
ブラウザのファイル選択はクライアント側のパスしか返さない。アプリが読むのは
このPCから見たパスなので、サーバで一覧して選ばせる。tkinter のダイアログは
ブラウザの裏に隠れる・表示環境の無い端末で落ちる、ので使わない
(`tests/test_no_tkinter.py` が見張っている)。

【何を返すか / 返さないか】
- **フォルダの名前**と、そこにある **Excel ファイルの名前**だけ
- ファイルの中身は返さない。読み取り口をここに作らない
- 件数に上限を置く(共有フォルダには数千の項目があることがある)
守りは `app/__init__.py` の 127.0.0.1 バインド + 起動トークン + Host検証 +
同一オリジン確認が担う。
"""
from __future__ import annotations

import string
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Sequence

MAX_ENTRIES = 300


@dataclass
class Entry:
    name: str
    path: str


@dataclass
class BrowseView:
    path: str = ""
    parent: str = ""
    exists: bool = False
    readable: bool = False
    message: str = ""
    dirs: List[Entry] = field(default_factory=list)
    excel_files: List[str] = field(default_factory=list)
    truncated: bool = False
    roots: List[Entry] = field(default_factory=list)


def browse(path_text: str, extensions: Sequence[str]) -> BrowseView:
    view = BrowseView(path=path_text, roots=_roots())
    text = (path_text or "").strip().strip('"')
    if not text:
        view.message = "フォルダを選んでください。"
        return view
    path = Path(text)
    try:
        if path.exists() and not path.is_dir():
            path = path.parent
    except OSError:
        pass
    view.path = str(path)
    view.parent = str(path.parent) if path.parent != path else ""
    try:
        exists = path.exists()
    except OSError:
        exists = False
    if not exists:
        view.message = "そのフォルダはこのPCから見えません。"
        return view
    view.exists = True
    exts = {("." + e.lower().lstrip(".")) for e in extensions}
    try:
        entries = sorted(path.iterdir(), key=lambda p: p.name.lower())
    except OSError as exc:
        view.message = f"このフォルダを読めません({exc.strerror or exc})。"
        return view
    view.readable = True
    for entry in entries:
        if len(view.dirs) + len(view.excel_files) >= MAX_ENTRIES:
            view.truncated = True
            break
        try:
            is_dir = entry.is_dir()
        except OSError:
            continue
        if is_dir:
            view.dirs.append(Entry(entry.name, str(entry)))
        elif entry.suffix.lower() in exts and not entry.name.startswith("~$"):
            view.excel_files.append(entry.name)
    if not view.dirs and not view.excel_files:
        view.message = "このフォルダは空です。"
    if view.truncated:
        view.message += f"(先頭 {MAX_ENTRIES} 件まで表示しています)"
    return view


def _roots() -> List[Entry]:
    """一覧の出発点。Windows は接続されているドライブ、それ以外は `/` とホーム。"""
    roots: List[Entry] = []
    if sys.platform.startswith("win"):
        for letter in string.ascii_uppercase:
            drive = Path(f"{letter}:\\")
            try:
                if drive.exists():
                    roots.append(Entry(f"{letter}:", str(drive)))
            except OSError:
                continue
    else:
        for candidate in (Path("/"), Path.home()):
            roots.append(Entry(str(candidate), str(candidate)))
    return roots


def to_dict(view: BrowseView) -> Dict[str, Any]:
    return {
        "path": view.path,
        "parent": view.parent,
        "exists": view.exists,
        "readable": view.readable,
        "message": view.message,
        "dirs": [{"name": e.name, "path": e.path} for e in view.dirs],
        "excel_files": view.excel_files[:20],
        "excel_count": len(view.excel_files),
        "truncated": view.truncated,
        "roots": [{"name": e.name, "path": e.path} for e in view.roots],
    }
