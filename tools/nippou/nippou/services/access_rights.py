"""アクセス権限の表を読んで、この端末に当てる (v4.12.0)

判断は `logic/access_rights.py`。ここは**表を探して読む**のと、**当てる**だけ。

【どこの表か】
マスタ管理に出ているファイルのうち、現場が持っている3つ(伝送用ファイル・
梱包資材マスタ・VC計算マスタ)を順に見て、最初に「アクセス権限」の表が
あったものを読みます。表の名前は全角半角・前後の空白を問いません。
**現物は梱包資材マスタ**にありました(班員名簿と同じファイル、v4.12.1)。

【いつ当てるか】
**起動のとき**(`start_app._initialize`)と、設定の画面で「読み直す」を
押したとき。表のラインが前に当てたものから変わっていれば、この端末の
ラインにします(`logic/access_rights.should_apply`)。管理者パスワードで
変えたラインは、表を書き換えるまでそのままです。

**読めなくても何も止めません。** 表が無い・行が無いときは、これまでどおり
(設定・管理者でラインを選ぶ)動きます。
"""
from __future__ import annotations

import getpass
import os
import socket
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from ..config import SETTINGS
from ..logging_setup import get_logger
from ..logic import access_rights as logic
from ..logic import line_names

log = get_logger("services.access_rights")

#: 表を探すファイル (鍵, 名前, 道)。**この順に見る**
CANDIDATES: tuple[tuple[str, str, Callable[[], Path]], ...] = (
    ("transmission", "伝送用ファイル", lambda: SETTINGS.transmission_master_path),
    ("material", "梱包資材マスタ", lambda: SETTINGS.gw_material_master_path),
    ("vc", "VC計算マスタ", lambda: SETTINGS.vc_master_path),
)


def identity() -> logic.Identity:
    """このPCの名前(ログイン名・PC名・Office のユーザー名)。"""
    try:
        login = os.environ.get("USERNAME") or getpass.getuser()
    except Exception:                             # noqa: BLE001 - 名前が分からないだけ
        login = ""
    pc = os.environ.get("COMPUTERNAME") or socket.gethostname() or ""
    # Office のユーザー名は VBA の `Application.UserName` と同じもの
    # (表のログインIDがこれで書かれていることもあるので、ログイン名と同じに扱う)
    return logic.Identity(login=login or "", pc=pc, office=_office_user_name())


def _office_user_name() -> str:
    if not sys.platform.startswith("win"):
        return ""
    try:                                          # pragma: no cover - Windows だけ
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Office\Common\UserInfo") as key:
            return str(winreg.QueryValueEx(key, "UserName")[0] or "")
    except Exception:                             # noqa: BLE001 - 無ければ使わない
        return ""


@dataclass
class Status:
    """読んだ結果。設定の画面と帯がこれを出す。"""

    decision: logic.Decision = field(default_factory=logic.Decision)
    identity: logic.Identity = field(default_factory=logic.Identity)
    #: どのファイルの表を読んだか(読めなければ空)
    source: str = ""
    #: 表が見つからない・読めないときの理由
    problem: str = ""
    #: 当てたときの知らせ(当てなければ空)
    applied: str = ""
    #: 表ぜんぶの点検(気になる行。`logic.inspect`)── このPCの行に限らない
    findings: list = field(default_factory=list)
    #: 点検した行の数(読めなければ 0)
    checked: int = 0

    @property
    def admin(self) -> bool:
        return self.decision.admin

    def as_dict(self) -> dict[str, Any]:
        d = self.decision
        return {
            "admin": d.admin, "full_access": d.full_access, "all_tabs": all_tabs(self),
            "line": d.line or "", "number": d.number, "lines": list(d.lines),
            "rows": [r.describe() for r in d.rows], "ignored": list(d.ignored),
            "identity": self.identity.describe(), "source": self.source,
            "problem": self.problem or d.problem, "summary": self.summary(),
            "applied": self.applied,
            "findings": [f.describe() for f in self.findings], "checked": self.checked,
        }

    def summary(self) -> str:
        if self.problem:
            return self.problem
        return self.decision.summary()


_lock = threading.Lock()
_status: Optional[Status] = None


def current() -> Status:
    """いちばん新しく読んだ結果(まだ読んでいなければ、空の結果)。"""
    # 読む前でも**このPCの名前は出す**(表に何を書けばよいかが分かるように)
    return _status or Status(identity=identity(),
                             problem="アクセス権限はまだ読んでいません(「読み直す」で読みます)")


def gate_reason() -> str:
    """ラインが決まっていない端末に添える一言(表を読んだのに決まらなかったとき)。"""
    if _status is None:
        return ""
    if _status.decision.line:
        return ""
    return f"アクセス権限の表では決まりませんでした: {_status.summary()}"


def is_administrator() -> bool:
    """このPCが表で Administrator か(読めていなければ False)。"""
    return bool(_status and _status.admin)


def all_tabs(status: Optional["Status"] = None) -> bool:
    """左のタブを全部(1〜8)出すか(v4.25.0)。

        マスタ：アクセス権限の追加 mode:fullaccess
        mode:fullaccessであれば現状の通り1～8まで全表示
        mode:fullaccessがついていない場合 1,2,3,5,8だけの表示

    このPCの行に `mode:fullaccess`(か Administrator)があれば全部。**表を読めた
    のに無ければ 1・2・3・5・8 だけ**(このPCの行が無いときも)。

    表が無い・読めない・まだ読んでいないときは**これまでどおり全部出す** ── 共有に
    届かない日に、管理の人の画面から「記録を見る」などが消えないように。
    """
    status = status if status is not None else _status
    if status is None or status.checked == 0:
        return True
    return status.decision.full_access or status.decision.admin


def reset() -> None:
    """読んだ結果を忘れる(テスト用)。"""
    global _status
    with _lock:
        _status = None


def _find_table(path: Path) -> Optional[str]:
    """そのファイルの「アクセス権限」の表の名前(無ければ None)。"""
    from .. import source_db

    want = logic.fold(logic.TABLE_NAME)
    if Path(path).suffix.lower() in source_db.SUFFIXES:
        try:
            names = source_db.list_tables(Path(path))
        except Exception:                         # noqa: BLE001 - 読めないファイルは飛ばす
            log.exception("表の一覧を読めませんでした: %s", path)
            return None
        return next((n for n in names if logic.fold(n) == want), None)
    return logic.TABLE_NAME                       # Access は名前で取りに行く


def read() -> tuple[list[dict[str, str]], str, str]:
    """(行, どこから読んだか, 読めなかった理由)。"""
    from ..access_bridge.importer import import_table

    looked: list[str] = []
    for _, label, path_of in CANDIDATES:
        try:
            path = Path(path_of())
        except Exception:                         # noqa: BLE001 - 設定が壊れていても次へ
            continue
        if not path.exists():
            continue
        looked.append(label)
        table = _find_table(path)
        if table is None:
            continue
        result = import_table(path, table)
        if result.success:
            return list(result.rows), f"{label} の {table}", ""
        log.info("アクセス権限を読めませんでした(%s): %s", label, result.error)
    where = "・".join(looked) if looked else "マスタのファイル"
    return [], "", f"{where} に「{logic.TABLE_NAME}」の表がありません(または読めません)"


def load() -> Status:
    """表を読んで判断する(当てはしない)。**結果を覚える。**"""
    global _status
    me = identity()
    try:
        rows, source, problem = read()
    except Exception as exc:                      # noqa: BLE001 - 読めなくても動く
        log.exception("アクセス権限の読み込みで予期しないエラー")
        rows, source, problem = [], "", f"アクセス権限を読めませんでした: {exc}"
    decision = logic.decide(rows, me) if rows else logic.Decision()
    findings = logic.inspect(rows) if rows else []
    status = Status(decision=decision, identity=me, source=source, problem=problem,
                    findings=findings, checked=len(rows))
    if findings:
        # 起動のたびに記録へ残す(ログの面で、どのPCでも同じ指摘が見える)
        log.warning("アクセス権限の表に気になる行が%d行あります: %s", len(findings),
                    " / ".join(f.describe() for f in findings))
    with _lock:
        _status = status
    return status


def apply(ctx=None) -> Status:
    """読んで、**この端末に当てる**(ラインは表の値が変わったときだけ)。"""
    from .. import config, user_settings, work_context

    status = load()
    decision = status.decision
    line, number, key = decision.line, decision.number, decision.applied_key
    # v4.12 までに当てた印は VBA の名前(`MARU:3`)── 正規へ揃えて比べる(v4.13.0)
    last = line_names.upgrade_key(user_settings.get(config.KEY_ACCESS_LINE_APPLIED) or "")
    if logic.should_apply(key, last):
        ctx = ctx or work_context.get_context()
        if ctx.recall.active:
            # 過去の紙を開いているあいだは動かさない(戻る先が変わるだけにする)
            ctx.terminal_line, ctx.terminal_maru_sub = line, number
            remembered = user_settings.save_many({config.KEY_TERMINAL_LINE: line,
                                                  config.KEY_TERMINAL_MARU: number})
        else:
            remembered = ctx.set_terminal_line(line, number)
        status.applied = (f"アクセス権限の表({status.source})から、この端末のラインを "
                          f"{decision.line_text()} にしました")
        # **覚えられたときだけ「当てた」と記す**(覚えられなければ次の起動で当て直す)
        if remembered and user_settings.save_many({config.KEY_ACCESS_LINE_APPLIED: key}):
            pass
        else:
            status.applied += "(覚えられませんでした。次の起動でもう一度当てます)"
        log.info("%s", status.applied)
    elif status.decision.found_row or status.problem or status.decision.problem:
        log.info("アクセス権限: %s(%s)", status.summary(), status.identity.describe())
    return status
