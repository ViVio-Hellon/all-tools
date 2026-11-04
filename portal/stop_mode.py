"""管理者以外停止モードの、日報複合ツールの入口(portal)の受け持ち

中身(お願い・見張り・控え)は `portal/remote_stop.py`(4ツールと同じ中身)。ここでするのは:

- 置き場所を**大設定の「共有DBの置き場所」**のフォルダにする(`shared_db.location`)
- お願いを出せるかの判定: 管理者パスワード **かつ** この端末のアクセス権限に `Administrator`
  (共有DB = 梱包資材マスタの `アクセス権限` の表。python-web-tools・日報と同じ表と同じ読み方)
- 入口の見張り: 中のツール(大きなタブの4ツール)が止めてよいと答えるのを待ち、
  画面に打ちかけを置いてもらってから、まとめて閉じる
- 起動: お願いが出ていれば起動しない(お願いを出した PC は起動する)
"""
from __future__ import annotations

import re
import sqlite3
import time
import unicodedata
from pathlib import Path
from typing import Callable, Optional

from . import app_config, remote_stop, shared_db
from .logging_utils import get_logger

log = get_logger("remote_stop")

#: アクセス権限の表(python-web-tools の `access_control.TABLE`、日報の `logic/access_rights.TABLE_NAME`)
ACCESS_TABLE = "アクセス権限"
ADMINISTRATOR = "Administrator"
LOGIN_COLUMNS = frozenset({"ログインid", "ログイン名", "ログオンid", "ユーザーid", "ユーザー名",
                           "ユーザid", "ユーザ名", "userid", "username", "login", "loginid"})
PC_COLUMNS = frozenset({"pc名", "pc", "コンピュータ名", "コンピューター名", "端末名", "端末",
                        "computername", "hostname", "pcname"})
RIGHTS_COLUMN = "権限"
VALID_COLUMN = "有効"
_OFF = frozenset({"0", "false", "no", "off", "いいえ", "無効", "×", "✕", "x", "n"})
_SEPARATORS = re.compile(r"[,、/／・;；]+")


def _root() -> Path:
    try:
        return Path(shared_db.location(strict=True).folder)
    except shared_db.SourceError as exc:
        raise remote_stop.Unreadable(str(exc)) from None


def configure() -> None:
    remote_stop.configure(app=app_config.display_name(), app_id="portal", version=app_config.version(),
                          local_root=app_config.local_root(), root=_root, bundle_root=app_config.APP_ROOT,
                          hosted=False, logger=log)


# ------------------------------------------------------------------
# Administrator
# ------------------------------------------------------------------
def fold(text: object) -> str:
    return unicodedata.normalize("NFKC", "" if text is None else str(text)).strip().casefold()


def _same(cell: object, mine: str) -> bool:
    """ログインID・PC名の欄が自分と同じか(`DOMAIN\\user` は `user` でも当たる)。"""
    value = fold(cell)
    want = fold(mine)
    if not value or not want:
        return False
    return value == want or value.rsplit("\\", 1)[-1] == want.rsplit("\\", 1)[-1]


def administrator_rows(rows: list, columns: list, pc: str, login: str) -> list:
    """この端末(ログインID と PC名の両方。空の欄は問わない)の行のうち、権限に Administrator があるもの。"""
    login_col = next((c for c in columns if fold(c) in LOGIN_COLUMNS), None)
    pc_col = next((c for c in columns if fold(c) in PC_COLUMNS), None)
    rights_col = next((c for c in columns if fold(c) == fold(RIGHTS_COLUMN)), None)
    valid_col = next((c for c in columns if fold(c) == fold(VALID_COLUMN)), None)
    if rights_col is None or (login_col is None and pc_col is None):
        return []
    found = []
    for row in rows:
        if valid_col is not None and fold(row.get(valid_col)) in _OFF:
            continue
        lv = fold(row.get(login_col)) if login_col else ""
        pv = fold(row.get(pc_col)) if pc_col else ""
        if not lv and not pv:
            continue                              # 誰の行にもならない
        if lv and not _same(lv, login):
            continue
        if pv and not _same(pv, pc):
            continue
        tokens = [t.strip() for t in _SEPARATORS.split(str(row.get(rights_col) or "")) if t.strip()]
        if any(fold(t) == fold(ADMINISTRATOR) for t in tokens):
            found.append(row)
    return found


def administrator() -> tuple:
    """(この端末が Administrator か, 理由)。共有DBの `アクセス権限` の表を読む。**読めなければ False**。"""
    pc, login = remote_stop.identity()
    try:
        path = shared_db.location(strict=True).path
        conn = shared_db._connect(path, read_only=True)
    except shared_db.SourceError as exc:
        return False, f"共有DBを読めないので、アクセス権限を確かめられません: {exc}"
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        table = next((t for t in tables if fold(t) == fold(ACCESS_TABLE)), None)
        if table is None:
            return False, f"共有DB({path.name})に「{ACCESS_TABLE}」の表がありません"
        quoted = '"' + table.replace('"', '""') + '"'
        cursor = conn.execute(f"SELECT * FROM {quoted}")
        columns = [d[0] for d in cursor.description]
        rows = [dict(zip(columns, r)) for r in cursor.fetchall()]
    except sqlite3.Error as exc:
        return False, f"アクセス権限の表を読めません: {exc}"
    finally:
        conn.close()
    if administrator_rows(rows, columns, pc, login):
        return True, ""
    return False, (f"この端末({pc} / {login})のアクセス権限に Administrator がありません"
                   f"(共有DBの「{ACCESS_TABLE}」の表の、ログインID・PC名がこの端末の行の「権限」)")


def _password_ok(password: str) -> bool:
    from . import admin_password
    return bool(password) and admin_password.verify(str(password))


def issue(password: str, reason: str = "") -> remote_stop.Result:
    ok = _password_ok(password)
    admin = administrator()[0] if ok else False
    return remote_stop.issue(ok, admin, reason)


def clear(password: str) -> remote_stop.Result:
    ok = _password_ok(password)
    admin = administrator()[0] if ok else False
    return remote_stop.clear(ok, admin)


_admin_cache: dict = {"at": 0.0, "value": (False, "")}
ADMIN_CACHE_SEC = 30.0


def administrator_cached() -> tuple:
    """画面の見張り向け(30秒は読み直さない)。"""
    now = time.monotonic()
    if now - _admin_cache["at"] > ADMIN_CACHE_SEC:
        _admin_cache["value"] = administrator()
        _admin_cache["at"] = now
    return _admin_cache["value"]


def state(*, full: Optional[bool] = None) -> dict:
    """大設定の欄に出すもの。各 PC の知らせを読むのは **Administrator の端末だけ**(ほかはお願いだけ)。"""
    admin, why = administrator_cached()
    view = remote_stop.overview(full=admin if full is None else full).to_dict()
    view["administrator"] = admin
    view["administrator_why"] = why
    return view


# ------------------------------------------------------------------
# 起動
# ------------------------------------------------------------------
def startup_block() -> Optional[str]:
    """起動してよいか(止めるなら理由)。判定で起動を止めない。"""
    try:
        configure()
        return remote_stop.startup_block()
    except Exception as exc:                      # noqa: BLE001
        log.warning("管理者以外停止のお願いを確かめられませんでした: %s", exc)
        return None


def split_block(text: str) -> tuple:
    """起動の知らせ向けに (1行目, 残り)。"""
    head, _, rest = (text or "").partition("\n")
    return head, rest.strip()


# ------------------------------------------------------------------
# 入口の見張り
# ------------------------------------------------------------------
def screens_flushed() -> None:
    """画面が「打ちかけを置きました」と答えた(大きなタブの画面)。待たずに閉じる。"""
    watcher = remote_stop.get()
    if watcher is not None:
        watcher.hurry()


def install(stop: Callable[[], None], *, desktop: bool) -> Optional[remote_stop.Watcher]:
    """入口の見張りを立てる。`stop` は確認なしで全部を閉じる道(ブラウザ版: 各ツールに強制で終わって
    もらってから入口を止める / デスクトップ版: 外枠に「確認なしで閉じて」と頼む)。"""
    try:
        configure()
        for note in remote_stop.restore_keeps():
            remote_stop.user_note(note)

        def hold() -> bool:
            watcher = remote_stop.get()
            rid = ((watcher.request if watcher else None) or {}).get("id", "")
            return remote_stop.hosted_left(rid)[0] if rid else False

        def left() -> list:
            watcher = remote_stop.get()
            rid = ((watcher.request if watcher else None) or {}).get("id", "")
            return remote_stop.hosted_left(rid)[1] if rid else []

        def close() -> None:
            # 見張りは、画面が打ちかけを置く間(FLUSH_SEC。置いたと答えれば待たない)のあとで呼ぶ
            log.warning("管理者以外停止: 日報複合ツールを閉じます(%s)", "デスクトップ版" if desktop else "ブラウザ版")
            stop()

        def force_ok() -> bool:
            watcher = remote_stop.get()
            rid = ((watcher.request if watcher else None) or {}).get("id", "")
            return not (rid and remote_stop.hosted_keep_failed(rid))

        return remote_stop.install(close, left, kind="desktop" if desktop else "browser",
                                   extra_sec=remote_stop.HOST_EXTRA_SEC, hold=hold, force_ok=force_ok)
    except Exception as exc:                      # noqa: BLE001 - 見張りが立たなくても起動は続ける
        log.warning("管理者以外停止の見張りを立てられませんでした: %s", exc)
        return None


def leave() -> None:
    try:
        remote_stop.leave()
    except Exception:                             # noqa: BLE001 - 終わるときの後始末で止まらない
        pass
