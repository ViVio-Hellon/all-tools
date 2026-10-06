"""欄ごとの入力制限 (VBA ``_KeyPress`` + ``MoveText`` の入口側)

【この規則がどこから来たか】
VBA は2か所に分けて持っていました。

    ``LOT*_KeyPress``   打った文字そのものを弾く
                        (0-9 / A-Z / a-z だけ通し、a-z は大文字へ変換)
    ``MoveText``        打ち終わってから、数字でない欄を空にする。
                        欄ごとに決まった長さ(``Mcount``)まで入ったら
                        次の欄へ飛ばす

**「数字のみ」と「2桁入力」はこの2つの合わせ技**でした。開始時・開始分・
終了時・終了分は 2 文字入ったら次へ飛び、作業人数は 1 文字、停止時間は
3 文字。だから現場は「08」「50」と打てば指を動かさずに次へ進めます。

【なぜ規則をここに置くのか】
規則を画面(JavaScript)に書くと、`logic/navigation.move_text` が持って
いる同じ規則と 2 か所に分かれます。分かれた日から、片方だけ直された
ものが少しずつずれていきます。

    ここ(Python) … どの欄に何が打てて、何文字で次へ行くかを**決める**
    画面(JS)     … 決まったものを欄の属性として受け取り、**そのとおりに
                    振る舞う**(打てない文字を弾く・長さが来たら次へ)

`FOCUS_CHAIN` / `TEXT_LIKE_FAMILIES`(`constants.py`)が唯一の出どころで、
ここはそれを画面が読める形に翻訳するだけです。

【打ち終わってからの見張りは、これとは別に残す】
画面側の制限は**親切**であって、関門ではありません。貼り付け・IME・
古いブラウザ・画面を触らない経路(API を直に叩く)では通り抜けます。
だから欄を離れたときの `move_text` による判定は今までどおり効かせます
── 画面で弾くのは「打っている最中に気づける」ようにするためです。
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Optional

from .. import constants

# 文字の種類。**画面はこの名前だけを見る**(正規表現を画面に持たせない
# ── 持たせると、規則が Python と JS の 2 か所に分かれる)
CHARSET_DIGITS = "digits"        # 数字。VBA `IsNumeric` に合わせて - と . も通す
CHARSET_ALNUM_UPPER = "alnum_upper"   # 英数字。小文字は大文字へ変換する
CHARSET_ANY = "any"              # 制限なし

# 「次の欄へ飛ぶまでの長さ」が実質「制限なし」を意味する値。
# VBA の既定値 `Mcount = 20` がこれで、LOT に 20 文字打つ人はいない
# ── つまり「飛ばない」という意味でこの数字が置かれている
NO_ADVANCE_LEN = 20

# ロット番号の桁数。**7桁ちょうど**です。
#
# 現物(`LS4LOT` の仕掛)を見ると `N7131T0` `L715C50` `N8105G0` と、
# どれも7文字の英数字でした。VBA の `LOT*_KeyPress` は文字の種類だけを
# 絞っていて長さは見ておらず、`SQLiteLot検索` が「7桁そろったら引く」
# という作りだったので、**8桁打てること自体が間違い**です。
#
# **飛び先は付けません。** 7桁で勝手に次の欄へ行くと、打ち直しに
# 戻れません(ロットは打ち間違いに気づいて直すことがいちばん多い欄)。
# 埋まったらロット検索が走って行が埋まるので、そこで自然に手が止まります。
LOT_LENGTH = 7


@dataclass(frozen=True)
class InputRule:
    """欄1つぶんの入力制限。**画面へはこの形で渡す。**"""

    family: str
    charset: str
    #: この長さまで入ったら次の欄へ飛ぶ(`Mcount`)
    advance_at: int
    #: 打ち込める上限。飛ぶ長さと同じにする ── 飛んだあとに戻って
    #: 3 文字目を足せてしまうと、「2桁」の約束が崩れる
    max_length: Optional[int]
    #: 次の欄(飛び先)。終端の THT は None
    next_family: Optional[str]
    #: 小文字を大文字へ直すか
    uppercase: bool

    @property
    def advances(self) -> bool:
        return self.next_family is not None and self.advance_at < NO_ADVANCE_LEN


def _charset(family: str) -> str:
    # LOT だけ `KeyPress` で英数字に絞っていた(`LOT*_KeyPress`)。
    # LOTNO と GW の ComboBox1 も同じ規則
    if family == "LOT":
        return CHARSET_ALNUM_UPPER
    # `MoveText` の 2 つめの Select Case。ここに**無い**欄は
    # 「数字でなければ空にする」= 数字のみ
    if family in constants.TEXT_LIKE_FAMILIES:
        return CHARSET_ANY
    return CHARSET_DIGITS


def rule(family: str) -> Optional[InputRule]:
    """その欄の入力制限。知らない欄なら None。"""
    chain = constants.FOCUS_CHAIN.get(family)
    if chain is None:
        return None
    next_family, advance_at = chain
    charset = _charset(family)
    # 上限を付けるのは**短い欄だけ**。20 は「飛ばない」の意味なので、
    # そこに上限を入れると LOT が 20 文字で打ち止めになる
    max_length = advance_at if advance_at < NO_ADVANCE_LEN else None
    if family == "LOT":
        # ロットだけは飛ばないが上限はある(`LOT_LENGTH` の説明)
        max_length = LOT_LENGTH
    return InputRule(
        family=family,
        charset=charset,
        advance_at=advance_at,
        max_length=max_length,
        next_family=next_family,
        uppercase=charset == CHARSET_ALNUM_UPPER,
    )


def as_attributes(family: str) -> dict[str, str]:
    """画面がそのまま `<input>` に付けられる形。

    **属性の名前を決めるのもここ。** テンプレートが自分で組み立てると、
    規則を足したときに直す場所が増える。
    """
    found = rule(family)
    if found is None:
        return {}
    attrs = {"data-charset": found.charset}
    if found.max_length is not None:
        attrs["maxlength"] = str(found.max_length)
    if found.advances:
        attrs["data-advance-at"] = str(found.advance_at)
        attrs["data-next"] = str(found.next_family)
    if found.charset == CHARSET_DIGITS:
        # 携帯・タブレットで数字の並びを出す。**制限ではなく手助け**
        attrs["inputmode"] = "numeric"

    # --- 英数字の欄では、かな入力に切り替わっていても困らないように ---
    #
    # 【「LOTNOに入った時点でIMEを切ってほしい」について】
    # **ブラウザから IME を確実に切る手立ては、いまのところありません。**
    # `ime-mode` は CSS の仕様から外れ、Chromium(Edge も中身は同じ)は
    # 読み飛ばします ── 付けても何も起きません。
    #
    # 代わりに「切れていなくても困らない」ようにします:
    #
    #   ・`lang="en"` … この欄は英語だ、という手がかり。IME によっては
    #     これで直接入力に寄ります(効く保証はありません)
    #   ・打った先から半角・大文字へ直す(`entry.js` の `tidy`)。
    #     `Ｎ７１３１Ｔ０` と打っても、確定した瞬間に `N7131T0` になります
    #   ・送られてきた値もサーバで同じように直す(`normalize`)
    #
    # つまり**IMEが切れていることに頼らない**作りにしてあります。
    #
    # `inputmode="latin"` は付けません ── 仕様から外れた値で、いまの
    # ブラウザは読み飛ばします。**効かないものを置くと、効いているつもりで
    # 次の人が数えます。**
    if found.charset == CHARSET_ALNUM_UPPER:
        attrs["lang"] = "en"

    # --- 予測・履歴の一覧を出させない ---
    #
    # 【なぜ切るのか】
    # ブラウザは一度打った値を覚えていて、次に同じ欄へ入ると**その一覧を
    # 欄の下に落とします**。12行の表では、その一覧が下の行を覆います ──
    # 次に打つ欄が見えなくなり、Enter を押すと一覧のほうが選ばれます。
    #
    # ここに並ぶのは形の決まった値(LOTNO・時・分・枚数)ばかりで、
    # **候補から選んで嬉しい欄が1つもありません。** 打つほうが速い。
    #
    # `ime-mode` は CSS 側(`components.css`)。ここは欄の属性だけ。
    attrs["autocomplete"] = "off"
    attrs["autocorrect"] = "off"
    attrs["autocapitalize"] = "off"
    attrs["spellcheck"] = "false"

    # --- 開始・終了の時/分は、ダブルクリックでいまの時刻 ---
    #
    # 「普通に入力も可能だが、ダブルクリックで2つのテキストボックスを簡単に
    # 埋めれる」。どの欄が埋められるかは**ここが決めます**(`work_time.
    # STAMP_FIELDS`)。画面は `data-stamp` の付いた欄だけを聞きます
    #
    # 説明は `title` ではなく、マウスを乗せると出る簡易説明(v4.7.0
    # `input_shortcuts.HINTS` → `data-hint`)に書きます ── 両方付けると、
    # ブラウザの説明と2つ重なって出ます
    from .work_time import STAMP_FIELDS
    for which, families in STAMP_FIELDS.items():
        if family in families:
            attrs["data-stamp"] = which
    return attrs


def allows(family: str, text: str) -> bool:
    """その欄にその文字列を打てるか。**画面と同じ規則をサーバでも見る。**

    画面の制限は貼り付けや IME で通り抜けるので、送られてきた値も
    ここで見ます(断るのではなく、`move_text` が空にします)。
    """
    found = rule(family)
    if found is None or text == "":
        return True
    if found.charset == CHARSET_ALNUM_UPPER:
        return all(c.isascii() and c.isalnum() for c in text)
    if found.charset == CHARSET_DIGITS:
        from .numeric import is_numeric

        return is_numeric(text)
    return True


def normalize(family: str, text: str) -> str:
    """打たれた文字を、その欄の形に直す。**捨てずに直す。**

    ロットは半角の英数字大文字で持ちます(現物がそう)。ただし
    **全角で打たれても弾きません** ── IME が全角のままだった、貼り付けた
    元が全角だった、はどちらも現場で起きます。弾くと「打てているのに
    入らない」になるので、`Ｎ７１３１Ｔ０` は `N7131T0` へ直します
    (NFKC。VBA `LOT*_KeyPress` の「小文字は大文字へ」と同じ考え方で、
    直せるものは直してから受ける)。

    そのうえで**上限で切ります**。7桁の欄に8文字目が残ると、ロット検索が
    当たらないまま保存まで通ってしまいます。
    """
    found = rule(family)
    if found is None or not text:
        return text
    out = text
    if found.charset == CHARSET_ALNUM_UPPER:
        out = unicodedata.normalize("NFKC", out).upper()
        out = "".join(c for c in out if c.isascii() and c.isalnum())
    if found.max_length is not None:
        out = out[:found.max_length]
    return out
