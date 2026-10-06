"""アクセス権限 ── マスタの表から「このPCのライン」と「管理者か」を決める (v4.12.0〜v4.12.2)

    マスタ管理に テーブル：アクセス権限 があるので カラム：権限 にライン名が
    のっているのでそれをラインに設定する仕組み(パスワードがあれば現行の通り変更可)
    カラム：権限 に Administrator ライン名(L-1、HVC...etc) それ以外の文字列はスルー

【現物の表(梱包資材マスタ、v4.12.1 で確かめた)】

    管理番号 / ログインID / PC名 / 権限 / 有効 / 備考

1行が「**このログインIDで、このPCに入ったときの権限**」です。現場の各ラインの
PCは**同じ1つのログインID**(現場の共通アカウント)で使っていて、PCごとに
行があります(HVC のPC → HVC、L-1 のPC → L-1 …)。1つのPCに行が2つ以上
あることも普通です(ライン名の行と `mode:field` の行、など)。

【どの行がこのPCか ── ログインIDとPC名の両方】
v4.12.0 は「どれかの欄がログイン名**か**PC名と同じ行」としていました。現物では
共通アカウントが12台ぶんの行に入っているので、**どのラインのPCでも全部の
ラインの行が当たり**、「ラインが2つ以上 → 決めない」になって一度も効きません
でした。いまは:

    ログインIDの列とPC名の列があれば   … **両方とも**このPCと同じ行
                                          (欄が空なら、その欄は問わない)
    どちらかの列しか無ければ           … その列が同じ行
    どちらの列も無ければ               … どれかの欄がログイン名かPC名と同じ行
                                          (列名が分からない表のための逃げ道)

比べるときは大文字小文字・全角半角を問いません(`DOMAIN\\user` は `user` でも当たる)。
「有効」の列があれば、0 / いいえ / 無効 の行は読みません。

【権限の読み方】

    Administrator   … 管理者(どのラインの控えも、記録を見るで見て直せる)
    ライン名         … このPCのライン。**正規の呼び名だけ**(`logic/line_names` の定義の表:
                       L-1・LVC・HVC・機側・NS1・AIM・トット・バランサー・中板)。
                       `L1`(VBA の名前)・`l-1` のような正規でない書き方は読まない(v4.12.2)。
                       中板はラインNO を続けて書く(`中板3`)
    それ以外         … 読まない(スルー)。現物には mode:field / mode:material /
                       作業長 / コイル があります(ほかのツールの権限・役目、
                       このツールに無いライン)

1つの欄に「,」「、」「/」「・」で並べてあれば、1つずつ読みます。

【決めないとき】
このPCの行に**違うラインが2つ以上**あると、どちらか分からないので決めません。
ラインを書いていない(Administrator・作業長だけ)ときもラインは触りません。
どちらも画面に理由を出し、**読まなかった権限も並べます**(ツールのラインに
当てはまらない呼び名に気づけるように)。

【間違いに気づく ── 表ぜんぶの点検(`inspect`、v4.12.3)】
正規しか読まないので、書き間違いは**黙って読まれない**だけです。このPCの行の
ことしか見ないと、ほかのPCの行の書き間違いには誰も気づけません。そこで表の
**全部の行**を確かめて、気になる行を言います(設定の画面・マスタ管理):

    権限がラインのつもりで読めない   L1(正規は L-1)・中板(ラインNO が無い)・中板 3
    ログインIDもPC名も空             誰の行にもならない
    同じログインID・PC名にラインが2つ どちらか分からないので決めない

mode:field・作業長・コイルのような**ラインではない値は言いません**(スルーの決まり)。
マスタ管理で直すときは、ラインのつもりで読めない値は**保存の前に断ります**
(`master_admin._access_problem`)。

**ここは純粋な判断だけ**で、表を読むのは `services/access_rights.py` です。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

from .. import constants
from . import line_names

#: 表の名前と、権限の列の名前
TABLE_NAME = "アクセス権限"
RIGHTS_COLUMN = "権限"
#: 管理者の印
ADMINISTRATOR = "Administrator"

#: ログインIDの列・PC名の列・有効の列として読む名前(`fold` した形)
LOGIN_COLUMNS = frozenset({"ログインid", "ログイン名", "ログオンid", "ユーザーid", "ユーザー名",
                           "ユーザid", "ユーザ名", "userid", "username", "login", "loginid"})
PC_COLUMNS = frozenset({"pc名", "pc", "コンピュータ名", "コンピューター名", "端末名", "端末",
                        "computername", "hostname", "pcname"})
VALID_COLUMN = "有効"
#: 「有効」の列で**無効**と読む値(`fold` した形)。空は無効にしない(書き忘れで止めない)
_OFF = frozenset({"0", "false", "no", "off", "いいえ", "無効", "×", "✕", "x", "n"})

#: 1つの欄に並べるときの区切り(**空白では区切らない** ── 「L 1」を割らない)
_SEPARATORS = re.compile(r"[,、/／・;；]+")


def fold(text: object) -> str:
    """比べるための形(全角半角・大文字小文字・前後の空白を揃える)。"""
    return unicodedata.normalize("NFKC", "" if text is None else str(text)).strip().casefold()


def line_of(value: object, lines: Iterable[str] = constants.LINE_NAMES) -> Optional[str]:
    """権限の値(正規の呼び名)→ ツールの名前(正規の字に揃える)。正規でなければ None。"""
    code = line_names.to_code(value)
    return code if code in tuple(lines) else None


def is_administrator(value: object) -> bool:
    return fold(value) == fold(ADMINISTRATOR)


def tokens(value: object) -> list[str]:
    """1つの欄に並んだ権限を1つずつ。"""
    return [t.strip() for t in _SEPARATORS.split(str(value or "")) if t.strip()]


def _column(columns: Iterable[str], names: Iterable[str]) -> Optional[str]:
    want = {fold(n) for n in names}
    return next((c for c in columns if fold(c) in want), None)


def rights_column(columns: Iterable[str]) -> Optional[str]:
    """表の列から「権限」の列を探す(全角半角・前後の空白は問わない)。"""
    return _column(columns, (RIGHTS_COLUMN,))


def is_valid(value: object) -> bool:
    """「有効」の欄。0 / いいえ / 無効 なら False(空は有効のまま)。"""
    return fold(value) not in _OFF


@dataclass(frozen=True)
class Identity:
    """このPCを指す名前。"""

    login: str = ""                # Windows のログイン名
    pc: str = ""                   # PC名(コンピュータ名)
    office: str = ""               # Office のユーザー名(VBA の Application.UserName)

    @property
    def names(self) -> tuple[tuple[str, str], ...]:
        """画面に出す形(「ログイン名 yamada / PC名 PC-01」)。"""
        found = [("ログイン名", self.login), ("PC名", self.pc)]
        if self.office:
            found.append(("Officeのユーザー名", self.office))
        return tuple(found)

    def folded(self) -> set[str]:
        return {fold(v) for _, v in self.names if fold(v)}

    @staticmethod
    def _same(cell: object, *mine: str) -> bool:
        text = fold(cell)
        if not text:
            return False
        wanted = {fold(m) for m in mine if fold(m)}
        # `DOMAIN\\user` と書いてあれば `user` でも当てる
        return text in wanted or text.rsplit("\\", 1)[-1] in wanted

    def is_login(self, cell: object) -> bool:
        return self._same(cell, self.login, self.office)

    def is_pc(self, cell: object) -> bool:
        return self._same(cell, self.pc)

    def matches(self, cell: object) -> bool:
        """その欄の値が、このPCの名前のどれかと同じか(列名が分からない表のとき)。"""
        return self._same(cell, self.login, self.pc, self.office)

    def describe(self) -> str:
        return " / ".join(f"{label} {value}" for label, value in self.names if value)


@dataclass(frozen=True)
class RightsRow:
    """このPCに当たった1行。"""

    number: int                    # 表の中で何行目か(1から)
    matched_by: str                # どの列で当たったか(「ログインID・PC名」など)
    matched_value: str
    rights: str                    # 権限の欄そのまま

    def describe(self) -> str:
        return f"{self.number}行目({self.matched_by} = {self.matched_value}、権限 = {self.rights})"


@dataclass(frozen=True)
class Decision:
    """表から決まったこと。"""

    admin: bool = False
    #: 決まったライン(決められなければ None)
    line: Optional[str] = None
    #: 中板のラインNO = 設備番号(1〜7。ほかのラインは空)
    number: str = ""
    #: このPCの行に書いてあったライン(2つ以上なら決めない。中板は「中板3」の形)
    lines: tuple[str, ...] = ()
    rows: tuple[RightsRow, ...] = ()
    #: 読まなかった(スルーした)権限の値
    ignored: tuple[str, ...] = ()
    #: 決められなかった理由(決まったなら空)
    problem: str = ""

    @property
    def found_row(self) -> bool:
        return bool(self.rows)

    @property
    def applied_key(self) -> str:
        """前に当てたものと比べる形(`中板:3` / `L-1`)。決まっていなければ空。"""
        if not self.line:
            return ""
        return f"{self.line}:{self.number}" if self.number else self.line

    def line_text(self) -> str:
        """「機側」「中板3」の形(v4.13.0 からツールの名前が正規の呼び名なので、添えるものは無い)。"""
        if not self.line:
            return ""
        return line_names.label(self.line, self.number)

    def summary(self) -> str:
        """1行で言う(帯・設定の画面)。読まなかった権限も添える。"""
        if self.problem:
            text = self.problem
        else:
            parts = [f"ライン {self.line_text()}"] if self.line else []
            if self.admin:
                parts.append(ADMINISTRATOR)
            text = "・".join(parts) if parts else "ラインの指定なし"
        if self.rows and self.ignored:
            text += f"(読まなかった権限: {'・'.join(self.ignored)})"
        return text


def _match(row: Mapping[str, object], identity: Identity, rights: str,
           login_col: Optional[str], pc_col: Optional[str]) -> Optional[tuple[str, str]]:
    """この行がこのPCのものなら (どの列で, どの値で)。違えば None。"""
    if login_col or pc_col:
        used: list[tuple[str, str]] = []
        for col, same in ((login_col, identity.is_login), (pc_col, identity.is_pc)):
            if col is None:
                continue
            cell = str(row.get(col) or "").strip()
            if not cell:
                continue                          # 空の欄は問わない
            if not same(cell):
                return None
            used.append((col, cell))
        if not used:
            return None                           # 両方空の行は誰の行でもない
        return "・".join(c for c, _ in used), " / ".join(v for _, v in used)
    for name, value in row.items():
        if name != rights and identity.matches(value):
            return name, str(value).strip()
    return None


def decide(rows: Iterable[Mapping[str, object]], identity: Identity,
           lines: Iterable[str] = constants.LINE_NAMES) -> Decision:
    """表の行と、このPCの名前から決める。"""
    rows = list(rows)
    lines = tuple(lines)
    if not rows:
        return Decision(problem=f"{TABLE_NAME} の表に行がありません")
    columns = list(rows[0].keys())
    column = rights_column(columns)
    if column is None:
        return Decision(problem=f"{TABLE_NAME} の表に「{RIGHTS_COLUMN}」の列がありません")
    if not identity.folded():
        return Decision(problem="このPCのログイン名・PC名が分かりません")
    login_col = _column(columns, LOGIN_COLUMNS)
    pc_col = _column(columns, PC_COLUMNS)
    valid_col = _column(columns, (VALID_COLUMN,))

    matched: list[RightsRow] = []
    admin = False
    found: list[tuple[str, str]] = []             # (コード, ラインNO)
    ignored: list[str] = []
    notes: list[str] = []                         # 読めたが決められない(中板にラインNO が無い など)
    for number, row in enumerate(rows, 1):
        if valid_col is not None and not is_valid(row.get(valid_col)):
            continue                              # 有効でない行は読まない
        hit = _match(row, identity, column, login_col, pc_col)
        if hit is None:
            continue
        rights = str(row.get(column) or "").strip()
        matched.append(RightsRow(number, hit[0], hit[1], rights))
        for token in tokens(rights):
            if is_administrator(token):
                admin = True
                continue
            reading = line_names.read(token)
            if reading.code and reading.code in lines:
                if reading.problem:
                    if reading.problem not in notes:
                        notes.append(reading.problem)
                elif (reading.code, reading.number) not in found:
                    found.append((reading.code, reading.number))
                continue
            # それ以外の文字列はスルー。**ラインのつもりで読めない値なら、理由を添える**
            mistake = line_names.check(token)
            hint = line_names.hint(token)
            label = (f"{token}({hint})" if hint else
                     f"{token}(中板3 のように続けて書きます)" if mistake else token)
            if label not in ignored:
                ignored.append(label)

    if not matched:
        return Decision(problem=(f"{TABLE_NAME} の表に、このPC({identity.describe()})の"
                                 "行がありません"))
    shown = tuple(f"{code}{number}" for code, number in found)
    line, number = found[0] if len(found) == 1 else (None, "")
    problem = ""
    if len(found) > 1:
        problem = (f"このPCの行にラインが{len(found)}つあります({'・'.join(shown)})。"
                   "どちらか分からないので、ラインは自動では決めません")
    elif not found and notes:
        problem = "・".join(notes) + "(ラインは決めません)"
    return Decision(admin=admin, line=line, number=number, lines=shown, rows=tuple(matched),
                    ignored=tuple(ignored), problem=problem)


@dataclass(frozen=True)
class Finding:
    """点検で気になった1行。"""

    number: int                    # 表の中で何行目か(1から)
    key: str                       # 管理番号(あれば)
    message: str

    def describe(self) -> str:
        where = f"管理番号 {self.key}" if self.key else f"{self.number}行目"
        return f"{where}: {self.message}"


def inspect(rows: Iterable[Mapping[str, object]],
            lines: Iterable[str] = constants.LINE_NAMES) -> list[Finding]:
    """表の**全部の行**を確かめる(このPCの行だけでなく)。気になる行を返す。"""
    rows = list(rows)
    if not rows:
        return []
    columns = list(rows[0].keys())
    column = rights_column(columns)
    if column is None:
        return []                                 # 表ごと読めない(decide が言う)
    lines = tuple(lines)
    login_col = _column(columns, LOGIN_COLUMNS)
    pc_col = _column(columns, PC_COLUMNS)
    valid_col = _column(columns, (VALID_COLUMN,))
    key_col = _column(columns, ("管理番号",))
    found: list[Finding] = []
    by_pair: dict[tuple[str, str], list[tuple[int, str, str]]] = {}
    for number, row in enumerate(rows, 1):
        key = str(row.get(key_col) or "").strip() if key_col else ""
        for token in tokens(row.get(column)):
            mistake = line_names.check(token)
            if mistake:
                found.append(Finding(number, key, f"権限: {mistake}"))
        if (login_col or pc_col) and not any(
                str(row.get(c) or "").strip() for c in (login_col, pc_col) if c):
            found.append(Finding(number, key,
                                 "ログインIDもPC名も空なので、どのPCの行にもなりません"))
        if valid_col is not None and not is_valid(row.get(valid_col)):
            continue
        pair = tuple(fold(row.get(c)) if c else "" for c in (login_col, pc_col))
        for token in tokens(row.get(column)):
            reading = line_names.read(token)
            if reading.code in lines and not reading.problem:
                by_pair.setdefault(pair, []).append(
                    (number, key, f"{reading.code}{reading.number}"))
    for pair, hits in by_pair.items():
        distinct = sorted({h[2] for h in hits})
        if len(distinct) > 1:
            number, key, _ = hits[0]
            where = "・".join((f"管理番号 {k}" if k else f"{n}行目") for n, k, _ in hits)
            found.append(Finding(number, key, (
                f"同じログインID・PC名にラインが{len(distinct)}つあります"
                f"({'・'.join(distinct)}、{where})── どちらか分からないので決めません")))
    found.sort(key=lambda f: f.number)
    return found


def should_apply(decided: Optional[str], last_applied: str) -> bool:
    """表のラインを、いまこの端末に当てるか。

    **表の値が前に当てたものから変わったときだけ**当てます。毎回当てると、
    管理者パスワードで変えたライン(「現行の通り変更可」)が、起動し直す
    たびに表の値へ戻ってしまいます(画面を閉じるとアプリが終わる作りなので、
    起動し直しは1日に何度もあります)。表を書き換えれば、次の起動で効きます。
    """
    return bool(decided) and decided != (last_applied or "")
