"""アクセス権限 ── この端末で、どのモードを使えるか

Windows の**ログインID**と**PC名**を条件に、梱包資材マスタ(``梱包資材マスタ.sqlite3``)の
``アクセス権限`` 表から、その人・その端末が使えるモードを引く。

【なぜ要るのか】
これまでモードの切り替えは**管理者パスワードだけ**が関門だった。パスワードは
教え合える。守っていたのは「誰か」ではなく「パスワードを知っているか」だった。
ログインIDとPC名は Windows が持っている事実で、利用者が画面から変えられない。

【python-web-tools(梱包資材総合ツール)と同じ表・同じ読み方】
``アクセス権限`` は python-web-tools がすでに持っている表で、**ほかのツールも使う**。
1 行 = 1 つの許可で、``権限`` 列にはこのツールの知らない文字列も入る。

    ログインID      PC名             権限            有効
    --------------  ---------------  --------------  ----
    yamada          (空)             mode:material   1   … 山田はどのPCでも倉庫(資材)
    (空)            NLM-NGY-240638   mode:field      1   … この端末は誰でも現場
    suzuki          NLM-NGY-240638   mode:material   1   … 鈴木がこの端末のときだけ

このツールが読むのは次の 2 つだけ。**ほかの文字列は、そのままにして使わない**
(ほかのツールの権限なので、打ち間違い扱いもしない)。

    mode:field     … 現場モード
    mode:material  … 資材モード(このツールでは**倉庫モード**)

倉庫参照モードは何も変えられないので、権限は問わない。

読み方は python-web-tools の ``access_control`` と同じにしてある ── 同じ 1 行が
ツールによって違う意味になると、書いた人は結果を説明できない:

* 空欄は「問わない」。**ログインIDもPC名も空の行は誰にも効かない**(全員への許可に
  なり、権限を設ける意味が消えるため)
* 大文字小文字は区別しない(Windows のIDとPC名がそうなので)
* ``有効`` は空欄なら有効、``0`` / ``false`` / ``×`` などなら無効
* 当てはまる行の**和**を取る
* **当てはまる行が無ければ現場モードだけ**(締め出さず、強い権限も渡さない)

【ライン】(:mod:`kanban.line_names`。日報と同じ定義・同じ読み方)
``権限`` 列には**ライン名**(L-1・LVC・HVC・機側・NS1・AIM…の**正規の呼び名**)も入る。この端末の
行にライン名がちょうど 1 つあれば、それを**担当ライン**にする(:func:`line_to_apply`)。
``L1`` ``LS`` ``l-1`` ``コイル`` などは読まず、設定の画面に「読まなかった権限: L1(正規は「L-1」)」と
正しい書き方を添えて出す。違うラインが 2 つ以上あれば、どちらか分からないので決めない。
****起動するたびに表のラインにする**(:func:`line_at_startup`)。管理者パスワードで変えた担当ラインは
その起動のあいだだけ保つ(起動中の読み直しは、表のラインが前に当てたものから変わったときだけ当てる。
:func:`line_to_apply`)。次も変えたままにしたいなら、マスタ(アクセス権限)を書き換える。

【届かないとき】
梱包資材マスタは共有フォルダにある。届かないたびに現場モードへ落とすと、倉庫の端末が
倉庫として開けなくなるので、**前回読めた内容を手元に覚えておき**、届かないときは
それで判断する(:func:`grant_for`)。
"""

from __future__ import annotations

import difflib
import getpass
import json
import os
import platform
import threading
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from . import applog, config
from .db.shared import SharedDb, SharedDbError

TABLE = "アクセス権限"

#: 無いときに作る表。**実物の梱包資材マスタと同じ定義**(python-web-tools が読める形)
DDL = (
    'CREATE TABLE "アクセス権限" ("管理番号" INTEGER PRIMARY KEY, "ログインID" TEXT DEFAULT \'\','
    ' "PC名" TEXT DEFAULT \'\', "権限" TEXT NOT NULL, "有効" INTEGER DEFAULT 1, "備考" TEXT DEFAULT \'\')'
)

COL_NO, COL_LOGIN, COL_PC, COL_PERMISSION, COL_ENABLED, COL_NOTE = (
    "管理番号", "ログインID", "PC名", "権限", "有効", "備考")

PERM_FIELD = "mode:field"
PERM_MATERIAL = "mode:material"

#: モード → 要る権限。**倉庫参照は問わない**(何も変えられない)
MODE_PERMISSION = {
    config.MODE_SITE: PERM_FIELD,
    config.MODE_WAREHOUSE: PERM_MATERIAL,
}

#: このツールが読む権限と、画面の言葉
PERMISSION_LABEL = {
    PERM_FIELD: "現場モード",
    PERM_MATERIAL: "資材モード(このツールでは倉庫モード)",
}
KNOWN = frozenset(PERMISSION_LABEL)

#: **管理者。現場・倉庫の両方を使える**(切り替えられる)。日報と同じ書き方・同じ読み方:
#: 権限の欄の 1 つとして書き、全角半角・大文字小文字・前後の空白は問わない
#: (``Administrator`` = ``administrator`` = ``ＡＤＭＩＮＩＳＴＲＡＴＯＲ``)
ADMINISTRATOR = "Administrator"
ADMIN_CODES = frozenset({PERM_FIELD, PERM_MATERIAL})

#: 当てはまる行が無いときに渡すもの。**いちばん狭い現場モードだけ**
FALLBACK = frozenset({PERM_FIELD})

#: 検証用に身元を差し替える。本番では設定しない
ENV_LOGIN = "KANBAN_LOGIN_ID"
ENV_HOST = "KANBAN_PC_NAME"

#: 起動時に梱包資材マスタを待つ上限(秒)。届かない共有フォルダは長く待たされる
READ_TIMEOUT_SEC = 8.0

_CACHE_KEY = "access_rules"
#: 表から最後に当てた担当ライン(前と同じなら当て直さない)
_APPLIED_LINE_KEY = "access_line_applied"


# ------------------------------------------------------------------
# 身元
# ------------------------------------------------------------------
@dataclass(frozen=True)
class Identity:
    login_id: str
    pc_name: str

    def label(self) -> str:
        return f"{self.login_id or '(不明)'} @ {self.pc_name or '(不明)'}"


def _clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def is_administrator(value: Any) -> bool:
    """``Administrator`` か(全角半角・大文字小文字・前後の空白は問わない。日報と同じ)。"""
    return unicodedata.normalize("NFKC", _clean(value)).casefold() == ADMINISTRATOR.casefold()


def has_administrator(permission: Any) -> bool:
    """権限の欄に ``Administrator`` があるか(「Administrator、L-1」のように並べて書いてもよい)。"""
    from . import line_names

    return any(is_administrator(t) for t in line_names.tokens(permission))


def current_identity() -> Identity:
    """ログインIDとPC名。Windows は ``USERNAME`` / ``COMPUTERNAME`` を必ず持っている。"""
    login = _clean(os.environ.get(ENV_LOGIN)) or _clean(os.environ.get("USERNAME"))
    if not login:
        try:
            login = _clean(getpass.getuser())
        except Exception:  # noqa: BLE001 - 環境によっては落ちる
            login = ""
    host = _clean(os.environ.get(ENV_HOST)) or _clean(os.environ.get("COMPUTERNAME"))
    if not host:
        host = _clean(platform.node())
    return Identity(login_id=login, pc_name=host)


def _same(a: str, b: str) -> bool:
    """Windows のIDとPC名は**大文字小文字を区別しない**。"""
    return a.casefold() == b.casefold()


def parse_enabled(value: Any) -> bool:
    """``有効`` 列。空欄は有効、はっきり無効と書いたものだけ無効(python-web-tools と同じ)。"""
    text = _clean(value).casefold()
    return text not in ("0", "false", "no", "n", "×", "x", "無効", "off")


# ------------------------------------------------------------------
# 表の 1 行
# ------------------------------------------------------------------
@dataclass
class Rule:
    no: Any = None
    login_id: str = ""
    pc_name: str = ""
    permission: str = ""
    enabled: bool = True
    note: str = ""

    def has_condition(self) -> bool:
        return bool(self.login_id or self.pc_name)

    def matches(self, identity: Identity) -> bool:
        if not self.enabled or not self.has_condition():
            return False
        if self.login_id and not _same(self.login_id, identity.login_id):
            return False
        if self.pc_name and not _same(self.pc_name, identity.pc_name):
            return False
        return True

    def condition_label(self) -> str:
        parts = []
        if self.login_id:
            parts.append(f"ID {self.login_id}")
        if self.pc_name:
            parts.append(f"PC {self.pc_name}")
        return " かつ ".join(parts) if parts else "(条件なし ── 効きません)"

    def meaning(self) -> str:
        """このツールでの意味。ライン名なら担当ライン、ほかのツールの権限は、そう言う。"""
        if self.permission in PERMISSION_LABEL:
            return PERMISSION_LABEL[self.permission]
        from . import line_names

        parts = []
        for token in line_names.tokens(self.permission):
            if is_administrator(token):
                parts.append("管理者(現場モード・倉庫モードの両方)")
                continue
            d = line_names.by_official(token)
            if d is not None:
                parts.append(f"担当ライン「{d.official}」" + ("" if line_names.supported(d) else "(このツールに看板なし)"))
                continue
            clue = line_names.hint(token)
            parts.append(f"{token}: 読みません({clue})" if clue else f"{token}: ほかのツールの権限")
        return " / ".join(parts) if parts else "ほかのツールの権限(このツールでは使いません)"

    def to_dict(self) -> dict[str, Any]:
        return {"no": self.no, "login_id": self.login_id, "pc_name": self.pc_name,
                "permission": self.permission, "enabled": self.enabled, "note": self.note,
                "condition": self.condition_label(), "meaning": self.meaning(),
                "known": self.permission in KNOWN or has_administrator(self.permission)
                or "担当ライン" in self.meaning()}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Rule":
        return cls(no=d.get("no"), login_id=_clean(d.get("login_id")), pc_name=_clean(d.get("pc_name")),
                   permission=_clean(d.get("permission")), enabled=bool(d.get("enabled", True)),
                   note=_clean(d.get("note")))


# ------------------------------------------------------------------
# この端末が持っているもの
# ------------------------------------------------------------------
@dataclass
class Grant:
    identity: Identity
    codes: frozenset[str] = FALLBACK
    matched: list[Rule] = field(default_factory=list)
    has_master: bool = False
    """アクセス権限の表に行があったか(無ければ全員が現場モードだけ)。"""

    reason: str = ""
    """既定(現場モードだけ)へ落ちた理由・前回の内容で判断している理由。"""

    path: str = ""
    source: str = ""
    """``fresh``(いま読んだ)/ ``cache``(届かないので前回の内容)/ ``none``。"""

    read_at: str = ""

    line: str = ""
    """表から決まった担当ライン(このツールのコード。決まらなければ空)。"""
    lines_found: tuple[str, ...] = ()
    """この端末の行に書いてあったライン(正規の呼び名)。"""
    line_problem: str = ""
    """ラインを決めなかった理由(2 つある・このツールに無いライン)。"""
    ignored: tuple[str, ...] = ()
    """読まなかった権限(このツールのモードでもラインでもない。正しい書き方が分かれば添える)。"""
    admin: bool = False
    """この端末の行に ``Administrator`` がある(現場・倉庫の両方を使える)。"""
    direct: frozenset[str] = frozenset()
    """``mode:field`` / ``mode:material`` として書いてあった権限(Administrator の分を除く)。"""

    def via(self, mode: str) -> str:
        """そのモードを使える理由になった権限(画面の「使えます(…)」)。"""
        need = MODE_PERMISSION.get(mode, "")
        if not need or need in self.direct or not self.admin:
            return need
        return ADMINISTRATOR

    def allows(self, mode: str) -> bool:
        need = MODE_PERMISSION.get(mode)
        return True if need is None else need in self.codes

    def allowed_modes(self) -> list[str]:
        return [m for m in config.ALL_MODES if self.allows(m)]

    def startup_mode(self, requested: str) -> str:
        """開くモード。頼まれたものが使えればそれ、だめなら**狭いほうから**(現場 → 倉庫 → 倉庫参照)。"""
        if self.allows(requested):
            return requested
        for mode in (config.MODE_SITE, config.MODE_WAREHOUSE, config.MODE_WAREHOUSE_VIEW):
            if self.allows(mode):
                return mode
        return config.MODE_WAREHOUSE_VIEW

    def how_to_allow(self, mode: str) -> str:
        """そのモードを使えるようにするには、どの行を足せばよいか(画面にそのまま出す)。"""
        need = MODE_PERMISSION.get(mode)
        if need is None:
            return ""
        return (f"{config.mode_display_name(mode)}を使うには、梱包資材マスタの「{TABLE}」に次の行を"
                f"足してください(設定 → マスタ管理 → {TABLE}): "
                f"ログインID = {self.identity.login_id or '(空でPC名だけでもよい)'} / "
                f"PC名 = {self.identity.pc_name or '(空でIDだけでもよい)'} / 権限 = {need} / 有効 = 1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "login_id": self.identity.login_id, "pc_name": self.identity.pc_name,
            "label": self.identity.label(),
            "codes": sorted(self.codes),
            "matched": [r.to_dict() for r in self.matched],
            "has_master": self.has_master, "reason": self.reason,
            "path": self.path, "source": self.source, "read_at": self.read_at,
            "allowed_modes": self.allowed_modes(),
            "line": self.line, "line_label": config.display_name(self.line) if self.line else "",
            "lines_found": list(self.lines_found), "line_problem": self.line_problem,
            "ignored": list(self.ignored), "admin": self.admin,
            "modes": [
                {"key": m, "label": config.mode_display_name(m), "allowed": self.allows(m),
                 "permission": MODE_PERMISSION.get(m, ""), "via": self.via(m) if self.allows(m) else "",
                 "how": "" if self.allows(m) else self.how_to_allow(m)}
                for m in config.ALL_MODES
            ],
        }


def resolve(rules: Sequence[Rule] | None, identity: Identity | None = None) -> Grant:
    """当てはまる行の**和**。このツールの権限が 1 つも無ければ現場モードだけ。ラインも決める。"""
    grant = _resolve_modes(rules, identity or current_identity())
    _decide_line(grant)
    return grant


def _decide_line(grant: Grant) -> None:
    """この端末の行の ``権限`` からラインを決める(:mod:`kanban.line_names`)。"""
    from . import line_names

    found: list = []
    ignored: list[str] = []
    for rule in grant.matched:
        if rule.permission in KNOWN:
            continue                        # このツールのモード(読んでいる)
        for token in line_names.tokens(rule.permission):
            if token in KNOWN or is_administrator(token):
                continue
            d = line_names.by_official(token)
            if d is not None:
                if d not in found:
                    found.append(d)
                continue
            clue = line_names.hint(token)
            text = f"{token}({clue})" if clue else token
            if text not in ignored:
                ignored.append(text)        # それ以外の文字列はスルー(書き方が分かれば添える)
    grant.lines_found = tuple(d.official for d in found)
    grant.ignored = tuple(ignored)
    # トットとバランサーはこのツールでは同じライン(大板小板)。**ラインが違うか**で数える
    codes = list(dict.fromkeys(d.code for d in found))
    if len(codes) > 1:
        grant.line_problem = (f"この端末の行にラインが {len(codes)} つあります({'・'.join(grant.lines_found)})。"
                              "どちらか分からないので、担当ラインは決めません")
    elif found and not line_names.supported(found[0]):
        grant.line_problem = f"{found[0].official} はこのツールに看板が無いラインなので、担当ラインにはしません"
    elif found:
        grant.line = found[0].code


def line_at_startup(grant: Grant) -> str:
    """起動するときに当てる担当ライン。**表で決まれば毎回それ**(読めなかったときは当てない)。

    管理者パスワードで変えた担当ラインは、その起動のあいだだけ。次に起動すると表のラインに戻る
    ── 変えたままにしたいなら、マスタ(アクセス権限)を書き換える。
    """
    if not grant.line or grant.source == "none":
        return ""
    return grant.line


def line_to_apply(grant: Grant, store: Any) -> str:
    """**起動しているあいだ**(設定の「読み直す」)に当てる担当ライン。表のラインが前に当てたもの
    から変わったときだけ(変わらなければ空)── 管理者パスワードで変えた担当ラインを、画面を開き
    直しただけで戻さないため。"""
    if not grant.line or grant.source == "none":
        return ""
    try:
        before = store.get_meta(_APPLIED_LINE_KEY, "") if store is not None else ""
    except Exception:  # noqa: BLE001
        before = ""
    return grant.line if grant.line != before else ""


def mark_line_applied(store: Any, code: str) -> None:
    if store is None:
        return
    try:
        store.set_meta(_APPLIED_LINE_KEY, code)
    except Exception:  # noqa: BLE001 - 覚えられなくても次にまた当てるだけ
        applog.exception("当てた担当ラインを覚えられませんでした")


def _resolve_modes(rules: Sequence[Rule] | None, identity: Identity) -> Grant:
    if not rules:
        return Grant(identity=identity, codes=FALLBACK, has_master=False,
                     reason=f"{TABLE} にまだ行がありません(どの端末も現場モードだけになります)")
    matched = [r for r in rules if r.matches(identity)]
    direct = frozenset(r.permission for r in matched if r.permission in KNOWN)
    admin = any(has_administrator(r.permission) for r in matched)
    codes = direct | (ADMIN_CODES if admin else frozenset())
    if not codes:
        why = (f"{TABLE} に {identity.label()} の、このツールのモードの行がありません"
               if matched else f"{TABLE} に {identity.label()} の登録がありません")
        return Grant(identity=identity, codes=FALLBACK, matched=matched, has_master=True,
                     reason=why + "(現場モードだけ使えます)")
    return Grant(identity=identity, codes=codes, matched=matched, has_master=True,
                 admin=admin, direct=direct)


def problems(rules: Sequence[Rule]) -> list[str]:
    """書いた人の意図どおりに効かない行。**ほかのツールの権限は問題にしない。**"""
    out: list[str] = []
    blank = [r for r in rules if r.permission and not r.has_condition()]
    if blank:
        me = current_identity()
        out.append(f"ログインID も PC名 も空の行が {len(blank)} 件あります(全員への許可になるので効きません)。"
                   f"どちらか一方でも埋めれば効きます ── この端末なら ログインID = {me.login_id} / "
                   f"PC名 = {me.pc_name}")
    typos = []
    for r in rules:
        near = near_known(r.permission)
        if near:
            typos.append(f"{COL_NO} {r.no}: {r.permission} → {near} のことですか?")
    if typos:
        out.append("このツールの権限の打ち間違いかもしれません(効いていません): " + " / ".join(typos))
    out.extend(_line_problems(rules))
    return out


def _line_problems(rules: Sequence[Rule]) -> list[str]:
    """ライン名の書き間違い(表の全部の行)と、同じ ID・PC名 にラインが 2 つある行。"""
    from . import line_names

    out: list[str] = []
    wrong = []
    by_pair: dict[tuple[str, str], set[str]] = {}
    shown: dict[tuple[str, str], tuple[str, str]] = {}
    for r in rules:
        if not r.enabled or r.permission in KNOWN:
            continue
        for token in line_names.tokens(r.permission):
            mistake = line_names.check(token)
            if mistake:
                wrong.append(f"{COL_NO} {r.no}: {mistake}")
            d = line_names.by_official(token)
            if d is not None and r.has_condition():
                pair = (r.login_id.casefold(), r.pc_name.casefold())
                by_pair.setdefault(pair, {}).setdefault(d.code, d.official)   # トット・バランサーは同じライン
                shown.setdefault(pair, (r.login_id, r.pc_name))
    if wrong:
        out.append("ライン名として読まない書き方があります(担当ラインになりません): " + " / ".join(wrong))
    twice = [f"{shown[pair][0] or '(空)'} / {shown[pair][1] or '(空)'}: {'・'.join(sorted(names.values()))}"
             for pair, names in by_pair.items() if len(names) > 1]
    if twice:
        out.append("同じ ログインID・PC名 にラインが 2 つ以上あります(担当ラインを決めません): " + " / ".join(twice))
    return out


def near_known(code: str) -> str:
    """このツールの権限の**打ち間違いらしい**なら、その正しい綴り。違えば空文字。

    ``Mode:Field`` や ``mode:feild`` は効かない(完全一致で読む)。ほかのツールの
    権限(``master:edit`` など)は似ていないので、ここには引っかからない。
    """
    code = _clean(code)
    if not code or code in KNOWN:
        return ""
    near = difflib.get_close_matches(code.casefold(), sorted(KNOWN), n=1, cutoff=0.8)
    return near[0] if near else ""


# ------------------------------------------------------------------
# 梱包資材マスタを読む・表を用意する
# ------------------------------------------------------------------
class AccessDb(SharedDb):
    """梱包資材マスタ。**見せる・直すのはアクセス権限の表だけ**(ほかの表は python-web-tools のもの)。"""

    role = "access"

    def all_table_names(self) -> list[str]:
        return super().table_names()

    def table_names(self) -> list[str]:
        return [n for n in self.all_table_names() if n == TABLE]


def open_db(cfg: config.Config) -> AccessDb:
    return AccessDb(cfg.resolved_access_db_path(), busy_timeout_ms=cfg.busy_timeout_ms,
                    max_retry=cfg.max_retry)


class Unavailable(Exception):
    """梱包資材マスタを読めなかった。文は画面にそのまま出す。"""


def path_problem(path: str) -> str:
    """その場所をアクセス権限の置き場所にしてよいか。よければ空文字。"""
    from .db.shared import _is_usable_sqlite, kanban_tables_in

    target = Path(str(path or "").strip())
    if not str(target).strip() or str(target) == ".":
        return "場所が空です。"
    try:
        if not target.exists():
            return f"その場所が見つかりません: {target}"
        if not target.is_file():
            return f"ファイルではありません: {target}"
    except OSError as exc:
        return f"その場所を確かめられません: {target}({exc})"
    if not _is_usable_sqlite(target):
        return f"sqlite3 として開けません: {target}"
    try:
        kanban, _others = kanban_tables_in(target)
    except Exception as exc:  # noqa: BLE001
        return f"中の表を確かめられません: {target}({exc})"
    if kanban:
        return (f"看板マスタ(看板_LVC などがあるファイル)です: {target}\n"
                "アクセス権限は梱包資材マスタに置きます(ほかのツールと同じ表を使うため)。")
    return ""


def ensure_table(db: AccessDb) -> tuple[bool, str]:
    """``アクセス権限`` が無ければ作る。``(使えるか, 一言)``。**ほかの表には触らない。**"""
    why = path_problem(db.path)
    if why:
        return False, why
    try:
        if TABLE in db.all_table_names():
            return True, ""
    except SharedDbError as exc:
        return False, str(exc)
    if not db.can_write:
        return False, f"{db.path} に書けないので {TABLE} を作れません"
    results = db.execute([(DDL, [])])
    if not results or not results[0].ok:
        return False, f"{TABLE} を作れませんでした: {results[0].error_message if results else ''}"
    applog.info("梱包資材マスタに %s を作りました: %s", TABLE, db.path)
    return True, f"梱包資材マスタに {TABLE} の表を作りました"


def read_rules(db: AccessDb) -> tuple[list[Rule], bool]:
    """``(行, 表があるか)``。梱包資材マスタに届かなければ :class:`Unavailable`。"""
    if not db.exists():
        raise Unavailable(f"梱包資材マスタが見つかりません: {db.path}")
    try:
        if TABLE not in db.all_table_names():
            return [], False
        rows = db.select(
            f'SELECT "{COL_NO}" AS no, "{COL_LOGIN}" AS login_id, "{COL_PC}" AS pc_name,'
            f' "{COL_PERMISSION}" AS permission, "{COL_ENABLED}" AS enabled, "{COL_NOTE}" AS note'
            f' FROM "{TABLE}" ORDER BY "{COL_NO}"')
    except SharedDbError as exc:
        raise Unavailable(f"梱包資材マスタを読めません: {exc}") from exc
    return [Rule(no=r["no"], login_id=_clean(r["login_id"]), pc_name=_clean(r["pc_name"]),
                 permission=_clean(r["permission"]), enabled=parse_enabled(r["enabled"]),
                 note=_clean(r["note"])) for r in rows], True


def _with_timeout(fn: Callable[[], Any], seconds: float) -> Any:
    """届かない共有フォルダで起動を止めないよう、待つ時間に上限を置く。"""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - 呼んだ側へ渡す
            box["error"] = exc

    worker = threading.Thread(target=run, name="access-read", daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        raise Unavailable(f"梱包資材マスタを {seconds:.0f} 秒待っても読めませんでした")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def grant_for(cfg: config.Config, store: Any = None, *, identity: Identity | None = None,
              timeout: float = READ_TIMEOUT_SEC, db: AccessDb | None = None) -> Grant:
    """この端末の権限。**読めたら覚え、読めなければ前回の内容で判断する。**"""
    db = db or open_db(cfg)
    identity = identity or current_identity()
    now = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
    try:
        rules, _has_table = _with_timeout(lambda: read_rules(db), timeout)
    except Exception as exc:  # noqa: BLE001 - 読めないときは前回の内容へ
        cached = _load_cache(store, db.path)
        if cached is not None:
            grant = resolve(cached[0], identity)
            grant.path, grant.source, grant.read_at = db.path, "cache", cached[1]
            note = f"梱包資材マスタを読めないので、前回読めた内容({cached[1]})で判断しています: {exc}"
            grant.reason = note if not grant.reason else f"{note} / {grant.reason}"
            return grant
        grant = resolve([], identity)
        grant.path, grant.source = db.path, "none"
        grant.reason = f"{exc}(読めないので現場モードだけ使えます)"
        return grant
    _save_cache(store, db.path, rules, now)
    grant = resolve(rules, identity)
    grant.path, grant.source, grant.read_at = db.path, "fresh", now
    return grant


def _save_cache(store: Any, path: str, rules: Iterable[Rule], at: str) -> None:
    if store is None:
        return
    try:
        store.set_meta(_CACHE_KEY, json.dumps(
            {"path": path, "at": at, "rules": [r.to_dict() for r in rules]}, ensure_ascii=False))
    except Exception:  # noqa: BLE001 - 覚えられなくても判断はできる
        applog.exception("アクセス権限を手元に覚えられませんでした")


def _load_cache(store: Any, path: str) -> tuple[list[Rule], str] | None:
    """前回読めた内容。**同じ置き場所のものだけ**(置き場所を変えたら前のは使わない)。"""
    if store is None:
        return None
    try:
        data = json.loads(store.get_meta(_CACHE_KEY, "") or "null")
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("path") != path:
        return None
    return [Rule.from_dict(d) for d in data.get("rules", [])], str(data.get("at", ""))
