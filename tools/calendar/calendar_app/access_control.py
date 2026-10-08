"""アクセス権限 ── この端末が使えるラインを、ログインID と PC名 で決める

【なぜ要るのか】
これまでライン設定は**管理者パスワードだけ**で守っていました。パスワードは
教え合えるので、知っている人ならどの端末でもどのラインにでも変えられます。
守っていたのは「誰か」ではなく「合言葉を知っているか」です。

ここでは**誰か(どの端末か)**で決めます。ログインID と PC名 は Windows が
持っている事実で、利用者が画面から変えられません。

【表は他のツールと共用】
``梱包資材マスタ.sqlite3`` の ``アクセス権限`` 表は、梱包資材総合ツール
(python-web-tools)も使っています。列の形も読み方もそちらに揃えます。

    管理番号  ログインID  PC名        権限            有効  備考
    --------  ----------  ----------  --------------  ----  ----
    1         yamada      (空)        コイル          1           … 山田はどのPCでもコイル
    2         (空)        NLM-PC-042  作業長          1           … この端末は誰でも作業長
    3         suzuki      NLM-PC-042  mode:material   1           … (別ツールの権限)

* 空欄は「問わない」。**両方が空の行は誰にも効かない**(全員への許可に
  なり、権限を設ける意味が消えるため。別ツールと同じ決まり)
* 大文字小文字は区別しない(Windows のIDとPC名がそうなので)
* **このツールが読むのは、権限がライン名の行だけ。** ``mode:field`` の
  ような他ツールの値は読み飛ばします(消したり直したりもしない)
* 当たる行が複数あれば**和**(そのどれか)

【表で決まった端末は、そのラインに固定】
当たる行が1つなら、その端末は**そのラインに固定**です(画面では選べない)。
複数あれば、その中でだけ選べます。**切り替えにはいつでも管理者パスワード**
が要ります ── 権限は「使ってよいライン」であって、「勝手に変えてよい」
ではないため。表に無いラインは、パスワードを知っていても選べません。

【当たる行が無いとき】
**今までどおり、管理者パスワードで**どのラインにも切り替えられます。 表をまだ作っていない・
この端末を登録し忘れた、で全員が締め出されると仕事が止まるためです。
画面には、登録に使う値(この端末のログインID と PC名)を出します。
"""

from __future__ import annotations

import difflib
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Optional

from . import config
from .logging_utils import get_logger

log = get_logger("access_control")

TABLE = "アクセス権限"

#: 取り込み元(梱包資材マスタ)に表が無いときに作る形。**別ツールと同じ**
#: (手元で作った形が違うと、別ツールが読めなくなる)
SOURCE_SCHEMA = (
    f'CREATE TABLE IF NOT EXISTS "{TABLE}" ('
    '"管理番号" INTEGER PRIMARY KEY, '
    '"ログインID" TEXT DEFAULT \'\', '
    '"PC名" TEXT DEFAULT \'\', '
    '"権限" TEXT NOT NULL, '
    '"有効" INTEGER DEFAULT 1, '
    '"備考" TEXT DEFAULT \'\')'
)

COLUMNS = ("管理番号", "ログインID", "PC名", "権限", "有効", "備考")

#: 別ツールが使っている権限の例(マスタ管理の入力候補に出す)
OTHER_TOOL_CODES = ("mode:field", "mode:material")


def line_codes() -> tuple[str, ...]:
    """このツールが権限として読む値 = ライン名。"""
    return tuple(config.ALL_LINE_NAMES)


def is_line(code: str) -> bool:
    return code in line_codes()


# ---------------------------------------------------------------------------
# 1行
# ---------------------------------------------------------------------------
@dataclass
class Rule:
    """表の1行。空欄は「問わない」。"""

    login_id: str = ""
    pc_name: str = ""
    permission: str = ""
    enabled: bool = True
    note: str = ""

    def has_condition(self) -> bool:
        return bool(self.login_id or self.pc_name)

    def matches(self, login_id: str, pc_name: str) -> bool:
        # 条件が1つも無い行は全員に効いてしまう。書かれていても効かせない
        if not self.enabled or not self.has_condition():
            return False
        if self.login_id and self.login_id.casefold() != login_id.casefold():
            return False
        if self.pc_name and self.pc_name.casefold() != pc_name.casefold():
            return False
        return True


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _enabled(value: Any) -> bool:
    """``有効``。空・NULL は有効(列の既定が 1 のため)。"""
    text = _text(value)
    if text == "":
        return True
    return text not in ("0", "False", "false", "無効")


def load_rules(conn: sqlite3.Connection) -> list[Rule]:
    """手元の写しを読む。表が無ければ空(まだ取り込んでいない)。"""
    try:
        rows = conn.execute(
            f'SELECT "ログインID", "PC名", "権限", "有効", "備考" '
            f'FROM "{TABLE}" ORDER BY "管理番号"').fetchall()
    except sqlite3.Error:
        return []
    return [Rule(login_id=_text(r[0]), pc_name=_text(r[1]),
                 permission=_text(r[2]), enabled=_enabled(r[3]),
                 note=_text(r[4]))
            for r in rows]


# ---------------------------------------------------------------------------
# この端末が使えるライン
# ---------------------------------------------------------------------------
@dataclass
class Grant:
    """この端末(ログインID と PC名)が使えるライン。"""

    login_id: str
    pc_name: str
    #: 使えるライン(``LINE_ORDER`` の順)。**空なら表で決まっていない**
    lines: tuple[str, ...] = ()
    #: 表が手元にあるか(1行でも取り込めたか)
    has_table: bool = False
    #: 当たった行の数(ライン以外の権限も含む)
    matched: int = 0

    @property
    def managed(self) -> bool:
        """表でラインが決まっているか。"""
        return bool(self.lines)

    @property
    def fixed(self) -> bool:
        """**ライン固定**か(表で1つに決まっている)。画面は選べなくする。"""
        return len(self.lines) == 1

    def allows(self, line: str) -> bool:
        return line in self.lines

    def label(self) -> str:
        return f"{self.login_id or '(不明)'} @ {self.pc_name or '(不明)'}"

    def explain(self) -> str:
        """設定画面に出す一文。**どうすれば変わるか**まで言う。"""
        if self.fixed:
            return (f"この端末({self.label()})は、アクセス権限で"
                    f"「{self.lines[0]}」に固定されています。"
                    "ほかのラインにするには、マスタ管理の「アクセス権限」を"
                    "直してください(管理者パスワードが要ります)。")
        if self.managed:
            return (f"この端末({self.label()})は、アクセス権限で"
                    f"「{'・'.join(self.lines)}」のどれかに限られます。"
                    "この中で切り替えるにも管理者パスワードが要ります。"
                    "ほかのラインは選べません。")
        head = ("アクセス権限の表がまだ取り込まれていません。" if not self.has_table
                else f"アクセス権限にこの端末({self.label()})のラインの登録がありません。")
        return (head + "いまは管理者パスワードで切り替えます。"
                "マスタ管理の「アクセス権限」に、ログインID = "
                f"{self.login_id or '(空でPC名だけでもよい)'} / PC名 = "
                f"{self.pc_name or '(空でIDだけでもよい)'} / 権限 = ライン名 "
                "の行を足すと、この端末はそのラインに固定されます。")

    def to_dict(self) -> dict[str, Any]:
        return {"login_id": self.login_id, "pc_name": self.pc_name,
                "label": self.label(), "lines": list(self.lines),
                "managed": self.managed, "fixed": self.fixed,
                "has_table": self.has_table,
                "explain": self.explain()}


def resolve(conn: sqlite3.Connection, login_id: Optional[str] = None,
            pc_name: Optional[str] = None) -> Grant:
    """この端末が使えるライン。**判断はここだけ。**"""
    if login_id is None or pc_name is None:
        from . import terminals

        who = terminals.identity()
        login_id = who.login_id if login_id is None else login_id
        pc_name = who.pc_name if pc_name is None else pc_name
    rules = load_rules(conn)
    matched = [r for r in rules if r.matches(login_id, pc_name)]
    granted = {r.permission for r in matched if is_line(r.permission)}
    lines = tuple(name for name in _ordered_lines() if name in granted)
    return Grant(login_id=login_id, pc_name=pc_name, lines=lines,
                 has_table=bool(rules) or _has_table(conn), matched=len(matched))


def _ordered_lines() -> tuple[str, ...]:
    order = [name for name in config.LINE_ORDER if name in config.ALL_LINE_NAMES]
    order += [name for name in config.ALL_LINE_NAMES if name not in order]
    return tuple(order)


def _has_table(conn: sqlite3.Connection) -> bool:
    try:
        return bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (TABLE,)).fetchone())
    except sqlite3.Error:
        return False


def problems(conn: sqlite3.Connection) -> list[str]:
    """表の中で、書いた人の意図どおりに効かない行(マスタ管理に出す)。

    **他ツールの値は問題にしない**(``mode:field`` など)。ラインの
    打ち間違いらしいもの(``ｺｲﾙ`` ``コイル ``)だけを指摘する ──
    共用の表なので、知らない値を全部「おかしい」と言うと別ツールの
    正しい行まで疑わせてしまう。
    """
    rules = load_rules(conn)
    out: list[str] = []
    lines = line_codes()
    near = []
    for code in sorted({r.permission for r in rules if r.permission}):
        if is_line(code) or ":" in code:
            continue
        # 半角カナ・全角英数(``ｺｲﾙ`` ``ＨＶＣ``)は**そろえてから**比べる。
        # 文字の種類が違うと、似ているとすら判定されない
        folded = unicodedata.normalize("NFKC", code).strip()
        guess = ([folded] if is_line(folded) else
                 difflib.get_close_matches(folded, lines, n=1, cutoff=0.5))
        if guess:
            near.append(f"{code} → {guess[0]} のことですか?")
    if near:
        out.append("ライン名に近いが一致しない権限があります(このツールでは効きません): "
                   + ", ".join(near))
    blank = sum(1 for r in rules if is_line(r.permission) and not r.has_condition())
    if blank:
        out.append(f"ログインID も PC名 も空のライン権限が {blank} 件あります"
                   "(全員への許可になるので効かせていません)。")
    return out


# ---------------------------------------------------------------------------
# 取り込み元(梱包資材マスタ)に表を作る / 手元へ取り込む
# ---------------------------------------------------------------------------
def ensure_table(source) -> bool:
    """取り込み元に表が無ければ作る。作ったら ``True``。

    **形は別ツールと同じ**(``SOURCE_SCHEMA``)。既にあれば何もしない ──
    別ツールが作った表の形を、こちらから変えない。
    """
    if source.has_table(TABLE):
        return False
    source.execute(SOURCE_SCHEMA)
    log.info("取り込み元に %s 表を作りました", TABLE)
    return True


def import_rows(conn: sqlite3.Connection, source) -> int:
    """取り込み元の表を手元へ写す(``importer`` の表ごとの取り込み)。

    **全部の行を写す**(他ツールの値も)。マスタ管理の画面は取り込み元を
    直に読むが、手元の写しも同じ中身にしておくと、調べるときに迷わない。
    """
    return import_row_list(conn, source.query(f'SELECT * FROM "{TABLE}"'))


def import_row_list(conn: sqlite3.Connection, rows) -> int:
    """読み終えた行を手元へ写す。**取り込み元はもう閉じていてよい。**

    取り込みは、先に取り込み元を読み終えてから手元の錠を取る
    (``importer.import_source``)。読んでいるあいだ手元を塞がないため。
    """
    conn.execute(f'DELETE FROM "{TABLE}"')
    count = 0
    for row in rows:
        permission = _text(row.get("権限"))
        if not permission:
            continue
        conn.execute(
            f'INSERT INTO "{TABLE}" ("管理番号","ログインID","PC名","権限","有効","備考") '
            'VALUES (?,?,?,?,?,?)',
            (row.get("管理番号"), _text(row.get("ログインID")),
             _text(row.get("PC名")), permission,
             1 if _enabled(row.get("有効")) else 0, _text(row.get("備考"))))
        count += 1
    return count


# ---------------------------------------------------------------------------
# 起動したとき
# ---------------------------------------------------------------------------
@dataclass
class Applied:
    changed: bool = False
    line: str = ""
    before: str = ""
    reasons: list[str] = field(default_factory=list)


def enforce(conn: sqlite3.Connection) -> Applied:
    """表でラインが決まっていて、いまのラインがその中に無ければ合わせる。

    呼ぶのは**起動したとき**と、**同期で表を取り込み直したとき**
    (``sync/autosync.py``)。後者があるので、マスタ管理で行を直せば、
    開いたままの端末も次の同期で合う ── 閉じて開き直させない。

    **表が勝つ。** 予約(端末一覧の「次のライン」)で表に無いラインが
    入っても、ここで表のラインに戻す ── 管理の出どころは表1つにする。
    表で決まっていない端末には何もしない(今までどおり)。
    いまのラインが表の中にあれば、それも変えない(利用者が選んだもの)。
    """
    from . import settings as user_settings

    grant = resolve(conn)
    current = user_settings.get_my_line()
    if not grant.managed or grant.allows(current):
        return Applied(line=current, before=current)
    line = grant.lines[0]
    user_settings.save_my_line(line)
    log.info("アクセス権限に合わせてラインを変えました: %s → %s (%s)",
             current or "(未設定)", line, grant.label())
    return Applied(changed=True, line=line, before=current)
