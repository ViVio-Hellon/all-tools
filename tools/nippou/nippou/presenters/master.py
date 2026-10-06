"""マスタ管理の面に出すもの

判断は `nippou/master_admin.py` が済ませてあり、ここは**並べ方と言葉**
だけを持ちます。

【直したあとも、まるごとの状態を返す】
「保存しました」だけを返すと、**本当に入ったかどうか**を画面が別に
確かめに行くことになります。書いたあとも面ぜんぶを返すので、画面は
「何が起きたか」と「いまどうなっているか」を1回で受け取れます。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .. import master_admin


@dataclass
class MasterViewModel:
    #: 編集が開いているか(管理者パスワードを入れたか)
    unlocked: bool = False
    files: list[master_admin.FileInfo] = field(default_factory=list)
    #: 左に出す一覧(ファイル → その中の表)。**選ぶ前に何があるか読める**
    catalog: list[master_admin.CatalogGroup] = field(default_factory=list)
    file: str = ""
    table: str = ""
    query: str = ""
    tables: list[dict[str, str]] = field(default_factory=list)
    columns: list[master_admin.Column] = field(default_factory=list)
    page: Optional[master_admin.Page] = None
    message: str = ""


def browse(*, file: str = "", table: str = "", query: str = "",
           sort: str = "", sort_dir: str = "asc", unlocked: bool = False,
           message: str = "") -> MasterViewModel:
    """面を開いたとき / 直したあとに返す、まるごとの状態。"""
    view = MasterViewModel(unlocked=unlocked, message=message)
    view.files = master_admin.files()
    # **同じ調べものを2回しない。** ファイルの状態は上で読んである
    view.catalog = master_admin.catalog(view.files)
    view.file = _pick_file(view.files, file)
    view.query = query or ""
    if not view.file:
        return view

    target = next((f for f in view.files if f.key == view.file), None)
    view.tables = master_admin.table_notes(target.tables if target else [])
    view.page = master_admin.page(view.file, table, query=view.query,
                                  sort=sort, sort_dir=sort_dir,
                                  unlocked=unlocked)
    view.table = view.page.table
    if view.page.editable and view.table and target is not None:
        from pathlib import Path

        view.columns = master_admin.columns(Path(target.path), view.table)
    return view


def _pick_file(files: list[master_admin.FileInfo], wanted: str) -> str:
    """出すファイルを決める。

    指定が無い / 無い名前なら**直せるものの先頭**にします ── 空の面を
    出して「自分で選べ」とするより、いちばん触るものを開いておくほうが
    早い。直せるものが1つも無ければ先頭。
    """
    keys = [f.key for f in files]
    if wanted and wanted in keys:
        return wanted
    editable = [f.key for f in files if f.editable]
    if editable:
        return editable[0]
    return keys[0] if keys else ""


def to_dict(view: MasterViewModel) -> dict[str, Any]:
    return {
        "unlocked": view.unlocked,
        "files": [f.to_dict() for f in view.files],
        "catalog": [g.to_dict() for g in view.catalog],
        "file": view.file,
        "table": view.table,
        "query": view.query,
        "tables": view.tables,
        "columns": [c.to_dict() for c in view.columns],
        "page": view.page.to_dict() if view.page else None,
        "row_key": master_admin.ROW_KEY,
        "message": view.message,
    }
