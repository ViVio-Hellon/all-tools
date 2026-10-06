"""ライン毎の目標枚数と、45度線 (VBA ``目標値抜き取り`` + ``グラフ挿入``)

【VBAは何をしていたか】
`目標値抜き取り(line)` が 梱包資材マスタ「ライン毎目標」を
`[ライン]='<ライン>'` で引き、その「目標」を4つの変数に入れていました:

    累積目標 = 目標   累積上昇 = 目標      ' 累積は 目標×k の直線
    直枚目標 = 目標   直枚上昇 = 0         ' 直枚数は 目標 で横ばい

`グラフ挿入` は `Targetline` が ON で 累積目標 ≠ 0 のときだけ、赤い
折れ線「目標値」を足していました。累積のほうが**原点から一定の傾きで
伸びる線** ── これが45度線管理です。

【目標は「1日あたりの枚数」】
累積が 目標×k で伸びるので、目標は1日ぶんの枚数です。直枚数(枚)の
グラフには**同じ値を横ばいで**引きます ── VBA がそうしていたので
合わせます(1直あたりに割りません)。

【値の出どころは2つ。CSVが勝つ】
目標は**マスタとCSVの両方**から読みます。同じラインが両方にあれば
**CSVを採ります**(`merge`)── マスタを直すには Access なりツールなりを
開くことになり、「目標だけ変えたい」には重すぎるからです。CSVなら
メモ帳で開けます。

勝ち負けは**ラインごと**です。ファイルごとにすると、CSVに1行書いた
瞬間に他のラインの目標が消えます ── 直したい1行だけ書けば済む、が
「CSVのほうが簡単」の中身なので、そこを壊しません。

CSV の書き方(寛容に読みます):

    # 行頭 # は覚え書き。空行は飛ばす
    L-1,12
    LVC,119
    機側  100        ← タブでも空白でもよい
    ＮＳ１，１０      ← 全角も読む
    LS,100           ← v4.12 までの名前(VBA の名前)で書いたCSVも読む(v4.13.0)

**読めない行は黙って捨てません。** 捨てると目標線がひっそり消えて、
消えたことに気づけません。`problems` に入れて画面へ出します。
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Optional

#: 区切りとして認める文字。現場が何で区切っても読めるように
SEPARATORS = (",", "\t", ";")

#: 覚え書きの行頭
COMMENT_MARKS = ("#", "//", "'")


@dataclass(frozen=True)
class Problem:
    """読めなかった1行。**行番号まで出す**(直す場所が分かるように)。"""

    line_no: int
    text: str
    reason: str

    def as_dict(self) -> dict:
        return {"line_no": self.line_no, "text": self.text,
                "reason": self.reason}


#: 出どころの呼び名。**画面にそのまま出ます。**
FROM_CSV = "CSV"
FROM_MASTER = "マスタ"


@dataclass
class Targets:
    """ライン毎の目標枚数(1日あたり)と、読むときに困ったこと。"""

    values: dict[str, float] = field(default_factory=dict)
    problems: list[Problem] = field(default_factory=list)
    #: どこから読んだか。画面に出す
    source: str = ""
    #: ライン名 → その値の出どころ(`FROM_CSV` / `FROM_MASTER`)。
    #:
    #: **どちらが勝ったのかを画面に出すため**です。2つの出どころを
    #: 混ぜて読むので、これが無いと「マスタを直したのに変わらない」
    #: (CSVが勝っている)が黙って起きます ── 混ぜること自体は求められた
    #: 動きなので、隠さずに見せて解きます。
    origins: dict[str, str] = field(default_factory=dict)

    def of(self, line: str) -> Optional[float]:
        """そのラインの目標。無ければ None(=目標線を引かない)。"""
        found = self.values.get(normalize_line(line))
        return found if found else None

    def origin_of(self, line: str) -> str:
        """そのラインの値が、どちらから来たか。無ければ空。"""
        return self.origins.get(normalize_line(line), "")

    def as_dict(self) -> dict:
        return {"values": dict(self.values),
                "problems": [p.as_dict() for p in self.problems],
                "source": self.source,
                "origins": dict(self.origins)}


def merge(base: Targets, top: Targets, *,
          base_name: str, top_name: str) -> Targets:
    """2つの出どころを**ラインごとに**重ねる。`top` が勝つ。

    【なぜ「ファイルごと」ではなく「ラインごと」なのか】
    マスタには全ラインぶんが入っています。今月 L1 の目標だけ上げたい、
    というときに**ファイルごと**の勝ち負けにすると、CSVに L1 の1行を
    書いただけで**他のラインの目標が全部消えます** ── 使うには全部を
    書き写すことになり、「CSVのほうが直しやすい」という利点が消えます。

    ラインごとに重ねれば、直したい1行だけ書けば済みます。

    困りごと(`problems`)は両方ぶんを残します ── どちらが読めなかったの
    かは、片方が読めていても知りたいことです。
    """
    out = Targets(values=dict(base.values), source=top.source or base.source)
    out.origins = {name: base_name for name in base.values}
    for name, value in top.values.items():
        out.values[name] = value
        out.origins[name] = top_name
    out.problems = list(base.problems) + list(top.problems)
    return out


def normalize_line(text: str) -> str:
    """ライン名を突き合わせる形に。

    全角・半角、大文字小文字、前後の空白、`L-1` の区切り記号を吸収します
    ── **CSV の「L-1」とツールの「L-1」が書き方の揺れで食い違って線が消える**
    のがいちばん困るので、揺れでは落ちないようにします。

    **ツールのラインに当たれば、正規の呼び名で返します**(v4.13.0。`l-1` `L 1` `L1` → `L-1`、
    `LS` → `機側`)── 画面の下見・設定の表に出る名前がこの形なので、ツールの名前と同じ綴りに
    します。v4.12 までのCSVはツールの名前 = VBA の名前(`LS` `TOT`)で書いていたので、
    そのまま効きます。当たらない名前は揺れを落とした形のまま(`予備PC` → `予備PC`)。
    マスタ(ライン毎目標)は正規だけを読みます(`services/targets`)。
    """
    from . import line_names

    folded = unicodedata.normalize("NFKC", str(text or "")).strip()
    upgraded = line_names.upgrade(folded)
    if upgraded in line_names.codes():
        return upgraded
    loose = _fold(folded)
    for d in line_names.DEFINITIONS:
        if loose in (_fold(d.official), d.old):
            return d.official
    return loose


def _fold(text: str) -> str:
    """大文字にして、区切り記号と空白を落とす(`l-1` → `L1`)。"""
    folded = unicodedata.normalize("NFKC", str(text or "")).strip().upper()
    for mark in ("-", "_", " ", "　", "．", "."):
        folded = folded.replace(mark, "")
    return folded


def to_number(text: str) -> Optional[float]:
    """目標の数。読めなければ None。全角数字も読みます。"""
    folded = unicodedata.normalize("NFKC", str(text or "")).strip()
    folded = folded.replace(",", "").replace("　", "")
    if not folded:
        return None
    try:
        return float(folded)
    except ValueError:
        return None


def _split(line: str) -> list[str]:
    """1行を「ライン名」と「目標」に割る。**全角で打たれても割る。**

    先に NFKC を通すのは、`ＬＶＣ，１１９` のような全角のまま打たれた行を
    読むためです ── 全角カンマを区切りと見ないと、その行は丸ごと
    「形になっていません」になって、目標が黙って1つ減ります。
    割るためだけに使う形なので、画面に出す文字はもとのままです。
    """
    folded = unicodedata.normalize("NFKC", line)
    for sep in SEPARATORS:
        if sep in folded:
            return [part.strip() for part in folded.split(sep)]
    # 区切り記号が無ければ空白で割る(「LS  100」のような書き方)
    return folded.split()


def parse(text: str, *, source: str = "") -> Targets:
    """CSVの中身を読む。**読めない行は捨てずに数える。**

    同じラインが2回出てきたら**後の行を採ります**(書き足して直す人が
    いるので)。ただし「2回出ている」ことは `problems` に残します ──
    上の行を直したつもりで下が効いている、が黙って起きないように。
    """
    targets = Targets(source=source)
    seen: dict[str, int] = {}

    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip().lstrip("﻿")          # BOM を落とす
        if not line:
            continue
        if any(line.startswith(mark) for mark in COMMENT_MARKS):
            continue

        parts = [p for p in _split(line) if p != ""]
        if len(parts) < 2:
            targets.problems.append(Problem(
                number, line, "「ライン,目標」の形になっていません"))
            continue

        name = normalize_line(parts[0])
        if not name:
            targets.problems.append(Problem(number, line, "ライン名が空です"))
            continue

        value = to_number(parts[1])
        if value is None:
            targets.problems.append(Problem(
                number, line, f"目標「{parts[1]}」が数字として読めません"))
            continue
        if value < 0:
            targets.problems.append(Problem(
                number, line, "目標がマイナスです"))
            continue

        if name in seen:
            targets.problems.append(Problem(
                number, line,
                f"{parts[0]} は {seen[name]}行目にもあります"
                "(この行の値を使います)"))
        seen[name] = number
        targets.values[name] = value

    return targets


def from_rows(rows: Iterable[dict], *, line_key: str = "ライン",
              target_key: str = "目標", source: str = "") -> Targets:
    """マスタ「ライン毎目標」の行から作る(**取り込みの下ごしらえ**)。

    CSV と同じ通り道に載せるので、名前の揺れ(`L-1` / `ﾄｯﾄ`)も
    `normalize_line` が吸収します。
    """
    lines = []
    for row in rows:
        name = str(row.get(line_key, "")).strip()
        value = str(row.get(target_key, "")).strip()
        if name or value:
            lines.append(f"{name},{value}")
    return parse("\n".join(lines), source=source)


def as_csv(targets: Targets, *, order: Iterable[str] = (),
           note: str = "") -> str:
    """CSV の文字に戻す(取り込みで書き出すときに使う)。

    `order` を渡すと**その並び**で出します ── ツールのライン順に
    そろえておくと、目で追って直しやすい。
    """
    out: list[str] = []
    if note:
        out += [f"# {row}" for row in note.splitlines()]
    written: set[str] = set()
    for name in order:
        key = normalize_line(name)
        if key in targets.values:
            out.append(f"{name},{_plain(targets.values[key])}")
            written.add(key)
    for key, value in targets.values.items():
        if key not in written:
            out.append(f"{key},{_plain(value)}")
    return "\n".join(out) + "\n"


def _plain(value: float) -> str:
    return str(int(value)) if value == int(value) else str(value)


# ==================================================================
# 45度線
# ==================================================================
def cumulative_line(target: Optional[float], points: int) -> list[float]:
    """累積枚数に重ねる目標。**原点から一定の傾きで伸びる線。**

    VBA の ``targetData(k) = 累積目標 + (k-1) * 累積上昇`` で、
    どちらも「目標」なので k 日目は 目標×k になります。
    """
    if not target or points <= 0:
        return []
    return [float(target) * (k + 1) for k in range(points)]


def flat_line(target: Optional[float], points: int) -> list[float]:
    """直枚数に重ねる目標。**同じ値で横ばい**(VBA は 直枚上昇 = 0)。"""
    if not target or points <= 0:
        return []
    return [float(target)] * points
