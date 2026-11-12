"""閲覧権限 ── どの端末に、どのラインの休み・連絡を見せるか(カレンダーマスタ.sqlite3)

    カレンダー設定部でどのPCがどのラインの閲覧権をもつかを設定できるようにしてほしい
    (コードを変えなくても参照を変えれるようにしたいです)
    カレンダーマスタsqlite3 として作ってください。なければ作るが、デフォルト設定値は
    プログラムに直接書いてある設定値

【置き場所】マスタDB(梱包資材マスタ.sqlite3)と同じフォルダの ``カレンダーマスタ.sqlite3``、表 ``閲覧権限``。
**無ければ作り、いままでプログラムに書いてあった決まりを3行入れます**(作った日に何も変わらない):

    ライン   休みを見るライン  連絡を見るライン  宛先を選べる
    作業長   全部              全部              1
    コイル   コイル            自分              0
    その他   コイル以外        自分              0

【1行の読み方】
    ログインID・PC名  どちらかを書けば**その端末の行**(空欄は問わない。両方空ならラインの行)
    ライン            ラインの行のとき、この端末のライン(設定のライン)。「その他」は どの行にも当たらないライン
    休み/連絡を見るライン  「全部」「自分」(この端末のライン)・ライン名・「コイル以外」のような「◯以外」を , で並べる
    宛先を選べる      1 なら、連絡を書くとき宛先のラインを選べる(作業長と同じ)
    有効              0 なら読まない

【どの行が効くか】端末の行 → この端末のラインの行 → 「その他」の行 → (表が読めなければ)プログラムの決まり。
"""
from __future__ import annotations

import sqlite3
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from . import config
from .logging_utils import get_logger

log = get_logger("view_rights")

FILE = "カレンダーマスタ.sqlite3"
TABLE = "閲覧権限"
COLUMNS = ("管理番号", "ログインID", "PC名", "ライン", "休みを見るライン", "連絡を見るライン",
           "宛先を選べる", "有効", "備考")
OTHER = "その他"
ALL = "全部"
SELF = "自分"
EXCEPT = "以外"

SCHEMA = (
    f'CREATE TABLE IF NOT EXISTS "{TABLE}" ('
    '"管理番号" INTEGER PRIMARY KEY, "ログインID" TEXT DEFAULT \'\', "PC名" TEXT DEFAULT \'\', '
    '"ライン" TEXT DEFAULT \'\', "休みを見るライン" TEXT DEFAULT \'\', "連絡を見るライン" TEXT DEFAULT \'\', '
    '"宛先を選べる" INTEGER DEFAULT 0, "有効" INTEGER DEFAULT 1, "備考" TEXT DEFAULT \'\')'
)

#: **いままでプログラムに書いてあった決まり**(表を作るときに入れる・表が読めないときに使う)
DEFAULT_ROWS: tuple[dict[str, Any], ...] = (
    {"ライン": config.LINE_SUPERVISOR, "休みを見るライン": ALL, "連絡を見るライン": ALL,
     "宛先を選べる": 1, "備考": "作業長は全ライン"},
    {"ライン": config.LINE_COIL, "休みを見るライン": config.LINE_COIL, "連絡を見るライン": SELF,
     "宛先を選べる": 0, "備考": "コイルはコイルだけ"},
    {"ライン": OTHER, "休みを見るライン": f"{config.LINE_COIL}{EXCEPT}", "連絡を見るライン": SELF,
     "宛先を選べる": 0, "備考": "ほかのラインは、休みはコイル以外・連絡は自分のライン宛"},
)


def _text(value: Any) -> str:
    return unicodedata.normalize("NFKC", "" if value is None else str(value)).strip()


def _tokens(value: Any) -> list[str]:
    text = _text(value)
    for sep in ("、", "・", "/", ";", " "):
        text = text.replace(sep, ",")
    return [t.strip() for t in text.split(",") if t.strip()]


def _enabled(value: Any) -> bool:
    from .access_control import parse_enabled

    return parse_enabled(value)


def spec_problem(value: Any) -> str:
    """見るラインの書き方が読めなければ、その理由。読めれば空。"""
    names = set(config.ALL_LINE_NAMES)
    bad = []
    for token in _tokens(value):
        base = token[:-len(EXCEPT)] if token.endswith(EXCEPT) and token != EXCEPT else token
        if base in (ALL, SELF) or base in names:
            continue
        bad.append(token)
    if bad:
        return (f"{'・'.join(bad)} は読めません。「全部」「自分」・ライン名({'・'.join(config.ALL_LINE_NAMES)})・"
                "「コイル以外」のような「◯以外」を , で並べてください。")
    return ""


def allows(spec: Any, line: str, my_line: str) -> bool:
    """見るラインの書き方 `spec` で、ライン `line` の登録を見せるか。"""
    include: set[str] = set()
    exclude: set[str] = set()
    everything = False
    for token in _tokens(spec):
        negate = token.endswith(EXCEPT) and token != EXCEPT
        name = token[:-len(EXCEPT)] if negate else token
        if name == ALL:
            if not negate:
                everything = True
            continue
        if name == SELF:
            name = my_line
        (exclude if negate else include).add(name)
    if not include and not everything and exclude:
        everything = True                         # 「コイル以外」だけ = 全部からコイルを除く
    shown = everything or line in include
    return shown and line not in exclude


@dataclass(frozen=True)
class Rights:
    """この端末の閲覧の決まり。"""

    rest: str
    comment: str
    choose: bool = False
    #: どの行から決まったか(設定画面に出す)
    origin: str = ""

    def rest_shown(self, line: str, my_line: str) -> bool:
        return allows(self.rest, line, my_line)

    def comment_shown(self, line: str, my_line: str) -> bool:
        return allows(self.comment, line, my_line)


def _row_rights(row: dict[str, Any], origin: str) -> Rights:
    flag = _text(row.get("宛先を選べる"))
    return Rights(rest=_text(row.get("休みを見るライン")), comment=_text(row.get("連絡を見るライン")),
                  choose=flag not in ("", "0", "false", "no", "無効", "×"), origin=origin)


def decide(rows: Iterable[dict[str, Any]], my_line: str, login: str = "", pc: str = "") -> Rights:
    """この端末の決まり。端末の行 → ラインの行 → その他の行 → プログラムの決まり。"""
    rows = [r for r in rows if _enabled(r.get("有効"))]
    fold = lambda v: _text(v).casefold()           # noqa: E731
    for row in rows:
        lv, pv = fold(row.get("ログインID")), fold(row.get("PC名"))
        if not (lv or pv):
            continue
        if lv and lv != fold(login):
            continue
        if pv and pv != fold(pc):
            continue
        return _row_rights(row, f"この端末の行({_text(row.get('PC名')) or _text(row.get('ログインID'))})")
    for want in (my_line, OTHER):
        for row in rows:
            if fold(row.get("ログインID")) or fold(row.get("PC名")):
                continue
            if _text(row.get("ライン")) == want and want:
                return _row_rights(row, f"ライン「{want}」の行")
    return fallback(my_line)


def fallback(my_line: str) -> Rights:
    """表が無い・読めないときの決まり(いままでプログラムに書いてあったもの)。"""
    for row in DEFAULT_ROWS:
        if row["ライン"] == my_line:
            return _row_rights(row, "プログラムの決まり")
    return _row_rights(DEFAULT_ROWS[-1], "プログラムの決まり")


# ---------------------------------------------------------------------------
# ファイル
# ---------------------------------------------------------------------------
def path() -> Optional[Path]:
    """カレンダーマスタ.sqlite3 の道(マスタDBと同じフォルダ)。フォルダが分からなければ None。"""
    from . import sources

    try:
        master = sources.find_master_db()
    except Exception:                             # noqa: BLE001 - 届かない
        return None
    # **マスタDBが見つかったときだけ**(参照パスが未設定のときに、既定のフォルダへ勝手に作らない。
    # 作ると保存用DBを探す所が「マスタ以外の sqlite3」としてこれを拾ってしまう)
    return None if master is None else master.parent / FILE


def ensure(target: Optional[Path] = None) -> bool:
    """無ければ作り、プログラムの決まりを入れる。作ったら True。表があれば触らない。"""
    target = target or path()
    if target is None or not target.parent.is_dir():
        return False
    conn = sqlite3.connect(str(target), timeout=10)
    try:
        have = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()
        if have:
            return False
        with conn:
            conn.execute(SCHEMA)
            for row in DEFAULT_ROWS:
                names = list(row)
                conn.execute(f'INSERT INTO "{TABLE}" ({", ".join(chr(34) + n + chr(34) for n in names)})'
                             f' VALUES ({", ".join("?" for _ in names)})', [row[n] for n in names])
        log.info("%s に %s を作り、いままでの決まりを入れました", target, TABLE)
        return True
    finally:
        conn.close()


#: ファイルを見に行く間(秒)。直したら数秒で効く
CHECK_EVERY = 5.0
_lock = threading.Lock()
_cache: dict[str, Any] = {}


def read_rows() -> Optional[list[dict[str, Any]]]:
    """表の行。読めなければ None(プログラムの決まりで動く)。ファイルが変わったときだけ読み直す。"""
    import time

    now = time.monotonic()
    with _lock:
        if "checked" in _cache and now - _cache["checked"] < CHECK_EVERY:
            return _cache.get("rows")
        _cache["checked"] = now                   # 1か月ぶんのマス目で何百回も呼ばれる。ファイルは数秒に1回だけ見る
    target = path()
    if target is None:
        return None
    try:
        if not target.exists():
            ensure(target)
        stat = target.stat()
    except (OSError, sqlite3.Error) as exc:
        log.warning("カレンダーマスタを確かめられませんでした: %s", exc)
        return _cache.get("rows")
    stamp = (str(target), stat.st_mtime_ns, stat.st_size)
    with _lock:
        if _cache.get("stamp") == stamp:
            return _cache.get("rows")
    try:
        conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True, timeout=5)
        try:
            conn.row_factory = sqlite3.Row
            rows = [dict(r) for r in conn.execute(f'SELECT * FROM "{TABLE}" ORDER BY "管理番号"')]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.warning("カレンダーマスタの閲覧権限を読めませんでした: %s", exc)
        return _cache.get("rows")
    with _lock:
        _cache.update(stamp=stamp, rows=rows)
    return rows


def reset() -> None:
    with _lock:
        _cache.clear()


def current(my_line: str) -> Rights:
    """この端末の閲覧の決まり(表 → 読めなければプログラムの決まり)。"""
    from .terminals import identity

    rows = read_rows()
    if rows is None:
        return fallback(my_line)
    who = identity()
    return decide(rows, my_line, who.login_id, who.pc_name)
