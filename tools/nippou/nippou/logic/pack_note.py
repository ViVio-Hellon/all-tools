"""包装仕様の注意表記 (VBA ``PackagingSpecificationNo``)

【何のための機能か】
包装仕様NO には「この番号のときは、ふだんと違うことをする」という
決まりが付いていることがあります ── コイル巻きの向き、裸梱包、
ラテラルボー測定、スプレーマーキング、検査票の添付。

**打ち終わってから気づくと、荷を解くことになります。** だから VBA は
ロット番号を打った時点で `注意_包装仕様` を引き、その注意を etc 欄へ
書き、コイルのときは `MsgBox` で目の前に出していました。

【判定の形】
`梱包資材マスタ` の `注意_包装仕様` を **包装仕様NO で引きます**。
同じ番号に複数行あることがあり(実データで56種87行)、行ごとに
「どういうときに出すか」が `フラグ` で決まっています。

    フラグ空       いつでも出す
    用途コード     その行の用途コードと一致したとき
    寸法           厚・幅・丈が3つとも範囲に入ったとき
    納入先         引いた納入先が、その行の納入先にあたるとき
    取引先         引いた取引先が、その行の取引先にあたるとき

**フラグは重ねて書けます。** `寸法納入` なら「寸法も納入先も」、
`寸法取引` なら「寸法も取引先も」 ── 並んでいる条件を**すべて**
満たしたときだけ出します。

出す文字は `コメント`。複数あたれば**重ねます**(VBA は後ろから前へ
積んでいたので、並びもそれに合わせます)。

【除外(`≠`)は、フラグに書いていなくても効く】
`寸法取引` の行は、取引先(`ﾅﾒｶﾜｱﾙﾐ(ｶ`)の条件に加えて、納入先の欄に
**除外の並び**を持っています ── `≠K.T.N. CO. LTD,≠ﾆﾎﾝﾊﾂｼﾞﾖｳ*`。
「この取引先だが、この納入先は除く」という書き方です。

除外は**書いてあれば必ず効かせます。** フラグに `納入` と書いていない
からといって無視すると、**わざわざ「除く」と書いてある相手に注意を
出す**ことになります。

【板は静かに、コイルは前に出す】
VBA は `If arr(0) <> "板" Then MsgBox ...` ── **板のときはポップアップを
出しません**(「板***警告なし(検査側で出力)」とソースに書いてあります)。
コイルだけが目の前に出ます。ここも同じにして、`shape` を見て画面が
決めます。

【VBAから直したところ】
1つめ。複数行のときの「納入先」判定が、VBA では**どの行でも1行目の
納入先**を見ていました(`TempHiki(1, C_納入先)`。1行だけのときは
`TempHiki(1, …)` で正しいので、複数行のほうへ写したときの取り違えです)。
ここは行ごとに見ます。

2つめ。**組み合わせのフラグを VBA は見ていませんでした。**
`Select Case` にあるのは `用途コード` / `寸法` / `納入先` の3つだけで、
実データにある `寸法納入`(6行)と `寸法取引`(2行)はどれにも当たらず、
**注意が1つも出ていませんでした**(ラテラルボー測定)。条件が一致する
なら出す、に直しています。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..logging_setup import get_logger
from .numeric import is_numeric, to_float

log = get_logger("logic.pack_note")

#: 条件の種類
COND_USAGE = "用途コード"
COND_SIZE = "寸法"
COND_DELIVERY = "納入先"
COND_CUSTOMER = "取引先"

#: `フラグ` に書かれる言葉 → 条件。**長いものから**照合します
#: (`納入先` を先に見ないと `納入` で切れてしまう)
FLAG_WORDS: tuple[tuple[str, str], ...] = (
    (COND_USAGE, COND_USAGE),
    (COND_SIZE, COND_SIZE),
    (COND_DELIVERY, COND_DELIVERY), ("納入", COND_DELIVERY),
    (COND_CUSTOMER, COND_CUSTOMER), ("取引", COND_CUSTOMER),
)

#: 「この相手は除く」。`≠K.T.N. CO. LTD` のように頭に付く
NEGATE = "≠"
#: 並べるときの区切り。`≠A,≠B*`
TERM_SEPARATOR = ","
#: この欄だけのワイルドカード。`ﾆﾎﾝﾊﾂｼﾞﾖｳ*` は「で始まるところ全部」
WILDCARD = "*"


def parse_flag(flag: str) -> tuple[list[str], str]:
    """フラグ → (条件の並び, 読めなかった残り)。

    `寸法納入` は `寸法` と `納入先` の2つに分かれます。残りが出たら
    **その行は出しません** ── 知らない書き方を勝手に解釈して、意図と
    違う注意を出すほうが危ないので。
    """
    rest = (flag or "").strip()
    found: list[str] = []
    changed = True
    while rest and changed:
        changed = False
        for word, condition in FLAG_WORDS:
            if rest.startswith(word):
                if condition not in found:
                    found.append(condition)
                rest = rest[len(word):].strip()
                changed = True
                break
    return found, rest


@dataclass(frozen=True)
class Term:
    """一致式の1つぶん。`≠` が付いていれば「除く」。"""

    text: str
    negate: bool = False

    def hit(self, value: str) -> bool:
        """`value` がこの言葉にあたるか。**空なら当たりません。**

        `*` はワイルドカード。付いていなければ、**どちらかがどちらかを
        含めば**一致とします ── VBA は「行の値が引いた値を含むか」で
        見ていたので(`Like "*" & 納入先 & "*"`)、その向きも残します。
        """
        if not value or not self.text:
            return False
        if WILDCARD in self.text:
            pattern = re.escape(self.text).replace(re.escape(WILDCARD), ".*")
            return re.search(pattern, value) is not None
        return self.text in value or value in self.text


def parse_terms(expression: str) -> list[Term]:
    """`≠A,≠B*` のような並びを読む。ふつうの名前1つでも同じ形になります。"""
    out: list[Term] = []
    for part in (expression or "").split(TERM_SEPARATOR):
        text = part.strip()
        negate = text.startswith(NEGATE)
        if negate:
            text = text[len(NEGATE):].strip()
        if text:
            out.append(Term(text=text, negate=negate))
    return out


def expression_hit(expression: str, value: str) -> bool:
    """一致式にあたるか。**除くほうが強い。**

    肯定がどれか1つでもあたれば一致。肯定が1つも書かれていなければ
    (除外だけの並び)、あたったものとして扱います ── そこは除外で
    絞るための書き方だからです。
    """
    terms = parse_terms(expression)
    if any(term.hit(value) for term in terms if term.negate):
        return False
    positives = [term for term in terms if not term.negate]
    if not positives:
        return bool(terms)          # 除外だけの並び。除かれなければ一致
    return any(term.hit(value) for term in positives)


def excluded(expression: str, value: str) -> bool:
    """`≠` にあたるか。**フラグに書いていなくても効かせる**ため別に持つ。"""
    return any(term.hit(value) for term in parse_terms(expression)
               if term.negate)

#: 形状。コイルのときだけ画面の前に出す(VBA の `MsgBox`)。
#: もう一方は「板」で、そちらは名前で呼ぶ場所がありません
SHAPE_COIL = "コイル"

#: 注意が複数あるときの区切り。**改行は使えません** ── 画面の etc 欄は
#: 1行の `<input>` で、改行はブラウザが黙って落とします(`PackNote.text`)
SEPARATOR = " / "


def _val(text: str) -> float:
    """VBA の ``Val()``。数でなければ 0。

    範囲の欄には `＊`(全角アスタリスク)が入っていることがあり、VBA でも
    `Val("＊")` は 0 でした。**下限0・上限0 は「0以上0以下」**なので、
    寸法の判定はまず通りません ── そこは VBA と同じにしてあります。
    """
    s = (text or "").strip()
    return to_float(s) if is_numeric(s) else 0.0


@dataclass(frozen=True)
class NoteRow:
    """`注意_包装仕様` の1行。**読んだものをそのまま持ちます。**"""

    pack_spec_no: str
    comment: str = ""
    remark: str = ""            # 備考(判定には使わない。画面の添え書き)
    shape: str = ""             # 板 / コイル
    flag: str = ""
    usage_code: str = ""
    thickness_low: str = ""
    thickness_high: str = ""
    width_low: str = ""
    width_high: str = ""
    length_low: str = ""
    length_high: str = ""
    delivery: str = ""
    customer: str = ""

    def matches(self, *, usage_code: str, thickness: float, width: float,
                length: float, delivery: str, customer: str = "") -> bool:
        """この行の注意を出すか。

        フラグに並んでいる条件を**すべて**満たしたときだけ出します。
        読めない書き方が残っていたら出しません ── 勝手に解釈して、
        意図と違う注意を出すほうが危ないので。

        除外(`≠`)だけは、フラグに書いていなくても効かせます
        (このファイルの冒頭を参照)。
        """
        conditions, rest = parse_flag(self.flag)
        if rest:
            log.warning("注意_包装仕様: 読めないフラグなので出しません"
                        " 包装仕様NO=%s フラグ=%s 残り=%s",
                        self.pack_spec_no, self.flag, rest)
            return False

        # **「除く」と書いてある相手には出さない。** 条件より先に見る
        if excluded(self.delivery, delivery) or excluded(self.customer, customer):
            return False

        for condition in conditions:
            if not self._holds(condition, usage_code=usage_code,
                               thickness=thickness, width=width,
                               length=length, delivery=delivery,
                               customer=customer):
                return False
        return True

    def _holds(self, condition: str, *, usage_code: str, thickness: float,
               width: float, length: float, delivery: str,
               customer: str) -> bool:
        if condition == COND_USAGE:
            return bool(self.usage_code) and usage_code == self.usage_code
        if condition == COND_SIZE:
            return (_val(self.thickness_low) <= thickness <= _val(self.thickness_high)
                    and _val(self.width_low) <= width <= _val(self.width_high)
                    and _val(self.length_low) <= length <= _val(self.length_high))
        if condition == COND_DELIVERY:
            # **納入先が空のときは出さない。** 欠陥引当などで納入先が
            # 空のことがあり、部分一致にすると全部あたってしまう
            # (VBA の 2024.3.11 の直しと同じ)
            return bool(delivery) and expression_hit(self.delivery, delivery)
        if condition == COND_CUSTOMER:
            return bool(customer) and expression_hit(self.customer, customer)
        return False                                  # ここには来ない


@dataclass
class PackNote:
    """引いた注意。**無いときも形は返します。**"""

    pack_spec_no: str = ""
    shape: str = ""
    comments: list[str] = field(default_factory=list)
    #: 見つかった行の数(あたらなかったものも含む)。記録に使う
    rows: int = 0

    @property
    def found(self) -> bool:
        """その番号が表にあったか。**注意が出るかどうかとは別。**"""
        return self.rows > 0

    @property
    def has_note(self) -> bool:
        return bool(self.comments)

    @property
    def is_coil(self) -> bool:
        """コイルか。**画面の前に出すかどうかの判断はこれ。**"""
        return self.shape == SHAPE_COIL

    @property
    def text(self) -> str:
        """etc 欄へ書く文字。**区切りは ` / `。**

        VBA は空白で区切って積み、`Split` して**改行**で並べ直して
        いました(あちらの etc 欄は複数行のテキストボックス)。

        ここは改行を使いません ── 画面の etc 欄は1行の `<input>` で、
        改行を入れると**ブラウザが黙って落とします**。「リプラ」と
        「梱包毎 ｺｲﾙ明細を添付」が `リプラ梱包毎 ｺｲﾙ明細を添付` に
        なって読めなくなるので、目に見える区切りを入れます。
        """
        return SEPARATOR.join(self.comments)

    def as_dict(self) -> dict:
        return {"pack_spec_no": self.pack_spec_no, "shape": self.shape,
                "comments": self.comments, "text": self.text,
                "found": self.found, "has_note": self.has_note,
                "is_coil": self.is_coil, "message": self.message}

    @property
    def message(self) -> str:
        """画面に出す一言。**コイルは言い方を変えます。**"""
        if not self.comments:
            return ""
        head = "コイルの注意" if self.is_coil else "包装仕様の注意"
        return f"{head}({self.pack_spec_no}): " + " / ".join(self.comments)


def build(pack_spec_no: str, rows: list[NoteRow], *, usage_code: str = "",
          thickness: float = 0.0, width: float = 0.0, length: float = 0.0,
          delivery: str = "", customer: str = "") -> PackNote:
    """あたった行のコメントを集める。**DBに触りません。**

    並びは VBA と同じ ── あちらは `Kari & " " & 既にあるもの` と
    **前へ積んで**いたので、行の並びとは逆になります。紙に出る順が
    変わると「いつもと違う」と見えるので、そこも合わせます。
    """
    number = (pack_spec_no or "").strip()
    if not number or not rows:
        # 番号が無いなら**引いてもいない**。`found` を立てない
        return PackNote(pack_spec_no=number)
    note = PackNote(pack_spec_no=number, rows=len(rows))

    # 形状は1行目のもの(VBA `TempHiki(1, C_形状)`)。同じ番号で板と
    # コイルが混ざることは無い前提で、あちらもそう書いてありました
    note.shape = (rows[0].shape or "").strip()

    for row in rows:
        if not row.matches(usage_code=usage_code, thickness=thickness,
                           width=width, length=length, delivery=delivery,
                           customer=customer):
            continue
        comment = (row.comment or "").strip()
        if not comment or comment in note.comments:
            # **同じ文言は1度だけ。** 寸法の帯を分けて同じ注意を書いて
            # ある行があり(実データの `寸法納入` は厚みで2行)、両方
            # あたると「ラテラルボー測定 / ラテラルボー測定」になる
            continue
        note.comments.insert(0, comment)         # **前へ積む**(VBAと同じ)
    return note
