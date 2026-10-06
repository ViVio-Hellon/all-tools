"""停止内訳をCSVで持つ (伝送用ファイル `作業停止時間内訳_1/_2/_3` の代わり)

【なぜCSVなのか】
停止理由の一覧は伝送用ファイル(共有の sqlite3)の3つの表にあります。
直すにはマスタ管理の鍵(管理者パスワード)が要り、現場の班長が
「この理由を足したい」ときに頼む相手が要りました ── 利用者の言葉で
「停止内訳も管理者以外が触れるようにしたい」。

ライン毎目標(`logic/line_target`)と同じく、**メモ帳で直せるCSV**を
置けば、そちらを読みます。

【CSVがあればCSV。無ければマスタ】
ライン毎目標は「ラインごとに重ねる」でしたが、停止内訳は**重ねません。**
重ねると「CSVから消した理由が、マスタから戻ってくる」になり、CSVを
書き換えても一覧から消せません。CSVに書いてある分類は**CSVだけ**を
使います。

ただし分類(表)ごとです。CSVに `1` の行しか無ければ、`2` と `3` は
マスタのまま ── 1つの分類だけ書き換えたいときに、残り2つを書き写す
手間を掛けさせないためと、書き写し損ねて一覧が空になるのを避けるため
(`pick`)。見本は3つとも書き出すので、ふつうは3つともCSVになります。

CSV の書き方(`as_csv` が書く形。寛容に読みます):

    # 行頭 # は覚え書き
    分類,内訳番号,内訳,備考
    1,0,休憩食事,
    2,イ,突発停止(機械),
    3,A,段取り変更,

- 分類は `1` / `2` / `3`(`作業停止時間内訳_1` の `_1`)。表の名前や
  「設備停止」「突発・待ち」のような呼び名でも読みます
- 内訳番号(記号)は**書いたとおりに**使います(全角半角も変えません)
  ── 日報に残っている記号と1文字でも違うと、昔の日報の停止が
  「内訳にない記号」になるので
- 区切りは「,」(Excel で保存したもの)でもタブでもよい

**読めない行は黙って捨てません。** 捨てると一覧から理由がひっそり
消えます。`problems` に行番号ごと入れて、設定画面に出します。
"""
from __future__ import annotations

import csv
import io
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .aggregation import classify_stop_code

#: 分類。**並びは作業停止時間内訳_1 → _2 → _3**(紙とマスタの並び)。
#:
#:   (番号, 画面の分類名, 表の名前, 人の呼び名, 記号の文字の種類)
#:
#: 画面の分類名は `access_bridge/stop_master.CATEGORY_TABLES` と同じもの。
#: 記号の文字の種類は集計の分け方(VBA `CheckCharType`)です ── 数字の
#: 記号は管理ロス、片仮名は突発・待ち、英字はハンドリングに数えます。
CATEGORIES: tuple[tuple[str, str, str, str, str], ...] = (
    ("1", "設備停止", "作業停止時間内訳_1", "管理ロス", "数値"),
    ("2", "不稼働", "作業停止時間内訳_2", "突発・待ち", "カタカナ"),
    ("3", "ハンドリング", "作業停止時間内訳_3", "ハンドリング", "アルファベット"),
)

#: 記号の文字の種類を、人に見せる言葉で
CHAR_TYPE_WORDS = {"数値": "数字", "カタカナ": "片仮名", "アルファベット": "英字"}

#: 見出しの行(読むときは飛ばす)
HEADER = ("分類", "内訳番号", "内訳", "備考")

#: 覚え書きの行頭
COMMENT_MARKS = ("#", "//", "'")

#: 出どころの呼び名。**画面にそのまま出ます**
FROM_CSV = "CSV"
FROM_MASTER = "マスタ"

#: 困りごとの重さ。`skip` はその行を使わない、`warn` は使うが言う
SKIP = "skip"
WARN = "warn"


def _fold(text: str) -> str:
    """分類の呼び名を見比べる形。全角半角と空白を吸収する。"""
    folded = unicodedata.normalize("NFKC", str(text or "")).strip()
    return "".join(folded.split()).lower()


#: 分類として読む書き方 → 画面の分類名
_CATEGORY_NAMES: dict[str, str] = {}
for _no, _name, _table, _call, _kind in CATEGORIES:
    for _alias in (_no, f"_{_no}", f"内訳{_no}", f"内訳_{_no}", _name, _table,
                   _call):
        _CATEGORY_NAMES[_fold(_alias)] = _name
# 呼び名の揺れ(マスタ管理・集計の画面での呼び方)
for _alias, _name in (("管理ロス停止", "設備停止"), ("管理ロス設備停止", "設備停止"),
                      ("突発", "不稼働"), ("突発停止", "不稼働"), ("待ち", "不稼働"),
                      ("段取り・突発停止", "不稼働"),
                      ("ハンドリング停止", "ハンドリング")):
    _CATEGORY_NAMES[_fold(_alias)] = _name

CATEGORY_NAMES: tuple[str, ...] = tuple(name for _, name, *_ in CATEGORIES)
_NUMBER_OF = {name: no for no, name, *_ in CATEGORIES}
_TABLE_OF = {name: table for _, name, table, *_ in CATEGORIES}
_CALL_OF = {name: call for _, name, _, call, _kind in CATEGORIES}
_KIND_OF = {name: kind for _, name, _, _, kind in CATEGORIES}


def category_of(text: str) -> Optional[str]:
    """分類の書き方 → 画面の分類名。読めなければ None。"""
    return _CATEGORY_NAMES.get(_fold(text))


def number_of(category: str) -> str:
    return _NUMBER_OF.get(category, "")


def table_of(category: str) -> str:
    return _TABLE_OF.get(category, "")


def call_of(category: str) -> str:
    """人の呼び名(管理ロス / 突発・待ち / ハンドリング)。"""
    return _CALL_OF.get(category, category)


def kind_of(category: str) -> str:
    """その分類の記号に期待する文字の種類(`classify_stop_code` の戻り値)。"""
    return _KIND_OF.get(category, "")


@dataclass(frozen=True)
class Reason:
    """停止理由1つ。`内訳番号`(記号)と `内訳`(名前)。"""

    code: str
    label: str
    note: str = ""


@dataclass(frozen=True)
class Problem:
    """困った1行。**行番号まで出す**(直す場所が分かるように)。"""

    line_no: int
    text: str
    reason: str
    #: `SKIP`(その行は使わない)/ `WARN`(使うが気をつけてほしい)
    level: str = SKIP

    def as_dict(self) -> dict:
        return {"line_no": self.line_no, "text": self.text,
                "reason": self.reason, "level": self.level}


@dataclass
class Parsed:
    """CSVから読めた停止内訳。**分類ごと**、書いてあった順。"""

    values: dict[str, list[Reason]] = field(default_factory=dict)
    problems: list[Problem] = field(default_factory=list)
    #: どこから読んだか。画面に出す
    source: str = ""

    def of(self, category: str) -> list[Reason]:
        return list(self.values.get(category, []))

    @property
    def count(self) -> int:
        return sum(len(v) for v in self.values.values())

    def categories(self) -> list[str]:
        """行が1つでもある分類(並びは `CATEGORIES`)。"""
        return [name for name in CATEGORY_NAMES if self.values.get(name)]


def _cells(line: str) -> list[str]:
    """1行を欄に分ける。**「,」を先に見る**(Excel で保存したCSV)。

    引用符(`"休憩,食事"`)も読みます。「,」が無くタブがあればタブで。
    """
    delimiter = "," if "," in line or "\t" not in line else "\t"
    try:
        row = next(csv.reader([line], delimiter=delimiter))
    except (csv.Error, StopIteration):
        row = line.split(delimiter)
    return [cell.strip() for cell in row]


def _is_header(cells: list[str]) -> bool:
    return bool(cells) and _fold(cells[0]) in {_fold("分類"), _fold("表")}


def parse(text: str, *, source: str = "") -> Parsed:
    """CSVの文字を読む。**読めない行は `problems` へ**(捨てない)。"""
    found = Parsed(source=source)
    seen: dict[str, tuple[int, str]] = {}      # 記号 → (行番号, 分類)
    for line_no, raw in enumerate(io.StringIO(text), start=1):
        line = raw.rstrip("\r\n").lstrip("﻿")
        stripped = line.strip()
        if not stripped or stripped.startswith(COMMENT_MARKS):
            continue
        cells = _cells(line)
        if _is_header(cells):
            continue
        if not any(cells):                       # 「,,,」だけの行(Excel の空行)
            continue
        if len(cells) < 3:
            found.problems.append(Problem(
                line_no, stripped,
                "欄が足りません(分類,内訳番号,内訳 の3つが要ります)"))
            continue
        category = category_of(cells[0])
        code, label = cells[1], cells[2]
        note = cells[3] if len(cells) > 3 else ""
        if category is None:
            found.problems.append(Problem(
                line_no, stripped,
                f"分類「{cells[0]}」が分かりません(1 / 2 / 3 のどれかにしてください)"))
            continue
        if not code:
            found.problems.append(Problem(
                line_no, stripped, "内訳番号(記号)がありません。記号の無い理由は選べません"))
            continue
        if not label:
            found.problems.append(Problem(
                line_no, stripped, "内訳(理由の名前)がありません"))
            continue
        if code in seen:
            first_line, first_category = seen[code]
            found.problems.append(Problem(
                line_no, stripped,
                f"記号「{code}」は {first_line}行目({call_of(first_category)})と"
                "同じです。記号は分類をまたいで1つずつにしてください(この行は使いません)"))
            continue
        seen[code] = (line_no, category)
        expected = kind_of(category)
        actual = classify_stop_code(code)
        if expected and actual != expected:
            # **使いますが、言います。** 一覧のどこに出すかは分類で決まりますが、
            # 集計(管理ロス / 突発・待ち / ハンドリング)は記号の文字で分けます
            # (VBA `CheckCharType`)── 数字の記号を 2 に書くと、選ぶときは
            # 突発・待ちの中にあるのに、集計では管理ロスに数えられます
            want = CHAR_TYPE_WORDS.get(expected, expected)
            found.problems.append(Problem(
                line_no, stripped,
                f"{call_of(category)}の記号は{want}です。「{code}」は集計で"
                f"{_counted_as(actual)}に数えられます", WARN))
        found.values.setdefault(category, []).append(Reason(code, label, note))
    return found


def _counted_as(kind: str) -> str:
    for name in CATEGORY_NAMES:
        if kind_of(name) == kind:
            return call_of(name)
    return "その他(どの停止にも入らない)"


# ------------------------------------------------------------------
# マスタと合わせる
# ------------------------------------------------------------------
@dataclass
class Picked:
    """実際に使う停止内訳と、分類ごとの出どころ。"""

    values: dict[str, list[Reason]] = field(default_factory=dict)
    #: 分類 → `FROM_CSV` / `FROM_MASTER`(どちらにも無ければ空)
    origins: dict[str, str] = field(default_factory=dict)


def pick(from_csv: Parsed, from_master: dict[str, list[Reason]]) -> Picked:
    """**分類ごとに、CSVに行があればCSV、無ければマスタ。** 重ねません。

    重ねると「CSVから消した理由がマスタから戻ってくる」ので、CSVで
    一覧を減らせなくなります(モジュールの説明)。
    """
    out = Picked()
    for name in CATEGORY_NAMES:
        rows = from_csv.of(name)
        if rows:
            out.values[name] = rows
            out.origins[name] = FROM_CSV
            continue
        master = list(from_master.get(name, []))
        out.values[name] = master
        out.origins[name] = FROM_MASTER if master else ""
    return out


# ------------------------------------------------------------------
# 書く
# ------------------------------------------------------------------
NOTE = (
    "停止内訳(停止理由の一覧)。日報入力の停止理由・全停入力・集計の名前に使います。",
    "このファイルをメモ帳で直せば、次に日報入力を開いたときから効きます。",
    "書き方: 分類,内訳番号,内訳,備考   行頭 # は覚え書き。",
    "分類 … 1=管理ロス(記号は数字) 2=突発・待ち(記号は片仮名) 3=ハンドリング(記号は英字)",
    "内訳番号 … 日報に残る記号。昔の日報と同じ記号を使ってください"
    "(変えると昔の日報の停止が「内訳にない記号」になります)",
    "分類ごとに、ここに行があればここを使い、1行も無い分類は伝送用ファイルの表を使います。",
)


def _cell(text: str) -> str:
    """欄の文字。「,」や引用符を含むときだけ囲む(Excel と同じ)。"""
    text = str(text or "")
    if any(ch in text for ch in (",", '"', "\n", "\r")):
        return '"' + text.replace('"', '""') + '"'
    return text


def as_csv(values: dict[str, Iterable[Reason]], *,
           note: Iterable[str] = NOTE) -> str:
    """CSVの文字(改行は CRLF。メモ帳で崩れない)。並びは `CATEGORIES`。"""
    lines = [f"# {text}" for text in note]
    lines.append(",".join(HEADER))
    for name in CATEGORY_NAMES:
        for reason in values.get(name, []):
            lines.append(",".join(_cell(v) for v in (
                number_of(name), reason.code, reason.label, reason.note)))
    return "\r\n".join(lines) + "\r\n"


def diff(before: dict[str, list[Reason]], after: dict[str, list[Reason]]
         ) -> dict[str, list[dict[str, str]]]:
    """いまの一覧 → 新しい一覧で、**何が増え・消え・名前が変わるか。**

    書き換える前に見せるためのものです。消える記号は、昔の日報に
    残っていれば「内訳にない記号」になります。
    """
    def flat(values: dict[str, list[Reason]]) -> dict[str, tuple[str, str]]:
        return {r.code: (name, r.label)
                for name in CATEGORY_NAMES for r in values.get(name, [])}

    old, new = flat(before), flat(after)
    added = [{"code": c, "label": new[c][1], "category": call_of(new[c][0])}
             for c in new if c not in old]
    removed = [{"code": c, "label": old[c][1], "category": call_of(old[c][0])}
               for c in old if c not in new]
    renamed = [{"code": c, "before": old[c][1], "label": new[c][1],
                "category": call_of(new[c][0])}
               for c in new if c in old and old[c][1] != new[c][1]]
    moved = [{"code": c, "label": new[c][1], "before": call_of(old[c][0]),
              "category": call_of(new[c][0])}
             for c in new if c in old and old[c][0] != new[c][0]]
    return {"added": added, "removed": removed, "renamed": renamed,
            "moved": moved}
