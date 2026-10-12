"""集計管理・集計・グラフで**どのラインを見るか** (v4.29.0)

    集計管理:アクセス権限がAdministratorの場合はどのラインの集計も見れるようにしてください
    ただし混ぜないでください あくまでラインごとに見れるようにするだけです
    表示 の隣に ライン を追加しAdministratorの場合だけ表示し
    コンボボックスでライン選択できるようにし表示データもそれに追従させてください

【誰に出すか】
アクセス権限の表で **Administrator** の PC だけ(`access_rights.is_administrator`)。
それ以外の PC には選ぶ欄を出さず、名指しされても**この端末のラインのまま**読みます
(画面が古い・手で叩かれた、でも他のラインは見えない)。

【どこから読むか】
    この端末のライン   手元の作業用DB(これまでどおり)
    ほかのライン       そのラインの控え(LocalBackup。`services/local_backup`)

控えに入っているのは日報の見出しと明細だけで、集計(`packing_report`)はありません。
控えは共有のフォルダにあり、そのラインの PC も書くので、**ここからは書きません** ──
控えを**メモリーへ写して**、集計はそのメモリーの上で作ります(読み終われば消える)。

**混ぜません。** 1 度に読むのは 1 ラインだけで、ほかのラインの行は数えません
(控えはラインごとに別のファイル。読むときもラインで絞る)。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator, Optional

from ..db.repository import NippouRepository
from ..db.schema import ensure_schema
from ..logging_setup import get_logger
from ..logic import line_names

log = get_logger("services.line_view")


class NoBackup(Exception):
    """そのラインの控えが無い・読めない(画面に理由を出す)。"""


def allowed() -> bool:
    """ラインを選べるか(アクセス権限の表で Administrator の PC だけ)。"""
    from . import access_rights

    return access_rights.is_administrator()


@dataclass
class Choices:
    """選ぶ欄に出すもの。**Administrator でなければ出さない。**"""

    shown: bool = False
    line: str = ""
    lines: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"shown": self.shown, "line": self.line,
                "lines": [{"code": ln, "label": line_names.label(ln)} for ln in self.lines]}


def choices(own_line: str) -> Choices:
    """この端末のラインと、控えのあるライン(ラインの並びで。この端末のラインは必ず入れる)。"""
    if not allowed():
        return Choices(shown=False, line=own_line)
    from . import local_backup

    try:
        found = local_backup.lines_available()
    except Exception:                             # noqa: BLE001 - 届かなければこの端末だけ
        log.exception("控えのあるラインを読めませんでした")
        found = []
    lines = list(found)
    if own_line and own_line not in lines:
        lines.insert(0, own_line)
    return Choices(shown=True, line=own_line, lines=lines)


def resolve(asked: object, own_line: str) -> str:
    """見るライン。**Administrator でなければ、何を言われてもこの端末のライン。**"""
    text = str(asked or "").strip()
    if not text or not allowed():
        return own_line
    return text


@contextmanager
def repo_for(line: str, own_line: str, own_repo: NippouRepository
             ) -> Iterator[NippouRepository]:
    """そのラインを読むリポジトリ。この端末のラインなら手元、ほかは控えの写し(メモリー)。"""
    if line == own_line:
        yield own_repo
        return
    from . import local_backup

    path = local_backup.path_for(line)
    mem = sqlite3.connect(":memory:")
    try:
        try:
            with local_backup.reader(line) as backup:
                if backup is None:
                    raise NoBackup(f"{line_names.label(line)} の控えがありません: {path}")
                backup.conn.backup(mem)
        except NoBackup:
            raise
        except Exception as exc:                  # noqa: BLE001 - 理由を出す
            log.exception("%s の控えを開けませんでした: %s", line, path)
            raise NoBackup(f"{line_names.label(line)} の控えを開けませんでした: {path}"
                           f"({exc})") from exc
        mem.row_factory = sqlite3.Row
        mem.execute("PRAGMA foreign_keys=ON")
        ensure_schema(mem)
        yield NippouRepository(mem)
    finally:
        mem.close()
