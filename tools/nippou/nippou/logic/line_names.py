"""ライン名の定義 ── 正規の呼び名がこのツールの名前(v4.13.0)。前(VBA の名前)は読み替えだけ

    しっかり定義しましょうか
    正規  前
    L-1   L1
    LVC   LVC
    HVC   HVC
    機側  LS
    NS1   NS1
    AIM   AIM
    トット TOT
    バランサー BALA
    中板  ラインNO とセット MARU
    「コイル」はこのツールにはないラインです
    AIM・LS・NS1・MARU は全部別のラインですよ

【v4.13.0 から、正規の呼び名がそのままツールの名前】
v4.12.5 までは、中では前(VBA の名前 `L1` `LS`…)をキーにして、画面と紙だけを
正規の呼び名にしていました。

    これを機にそこも変えませんか？ 今後の混乱のもとになりますし
    まずこのツールは運用していません … このデータをコンバートする仕組みだけで済む

そのとおりなので、**キー・DB・共有の表の名前・CSV・フォルダ名まで正規の呼び名**
にしました(`T_日報ヘッダー_機側`、`集計_2026-10-02_L-1.csv`)。`code` は
いまも残していますが、中身は正規の呼び名と同じです。

**前(`old`)が出てくるのは、VBA が貯めたものを読むときだけ**です:

    共有の旧名の表(T_日報ヘッダー_LS …)  → `services/line_rename` が写す(コンバート)
    VBA・変換器が書き出した CSV(ライン列が LS)→ 取り込むときに `upgrade` で読み替え
    v4.12 までの端末の設定・手元のDB        → 読むとき・起動のときに `upgrade`

LVC・HVC・NS1・AIM は前と正規が同じ字なので、表の名前も中身も変わりません
(`renamed()` に入らない ── 写すものがありません)。

【マスタは正規の呼び名だけを読む(v4.12.2)】
    マスタに L1 と書いても L-1 と読んでくれるということです？ ｌ―１ でも？
    あまり良くないですよねぇ

マスタ(アクセス権限・ライン毎目標)は**この表の正規の呼び名だけ**を読みます。
`L1`(前の名前)・`l-1`・`L―1`・`L 1` は読みません ── 読まなかったことは画面に
出し、正規の書き方を添えます(`hint`)。前の名前を読み替えるのは、上の
「VBA が貯めたもの」だけです(マスタは人が直せるので、直してもらいます)。

**揃えるのは文字の幅だけ**です(全角/半角。`ﾄｯﾄ` = `トット`、`ＬＶＣ` = `LVC`)。
現物の「ライン毎目標」が `ﾄｯﾄ` `ﾊﾞﾗﾝｻｰ` と半角で書いてあるためで、同じ字の
幅違いは書き間違いではないので。大文字小文字・記号(- と ― など)・空白は揃えません。

【中板は、ラインNOとセット】
中板(丸徳)は設備番号 1〜7 を持つ唯一のラインです。マスタでは
**`中板3` のように、中板のすぐ後ろにラインNO を続けて**書きます(v4.12.3 で
この1つの書き方に決めました)。`中板 3` `中板-3` は読みません。
ラインNO が無い・1〜7 でないときは、どの設備か分からないので決めません。
ラインNO は表の名前には入れません(`T_日報ヘッダー_中板` の1つ。VBA の MARU と同じ持ち方)。

【間違いに気づく(`check`)】
正規しか読まないので、書き間違いは**黙って読まれない**だけになります。それでは
気づけないので、「ラインのつもりで書いたのに読めない値」を見分けて言います:

    L1・l-1・ｌ―１・LS・TOT …  正規でない書き方(正規を添える)
    中板・中板9・中板 3 …        中板の書き方違い
    mode:field・作業長・コイル … ラインではない(言わない。スルー)

【無いもの】
`コイル` はこのツールのラインではありません(AIM・機側・NS1・中板はそれぞれ
別のライン)。表に無い呼び名は読みません。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional



@dataclass(frozen=True)
class LineName:
    """1つのライン。"""

    official: str                  # 正規(マスタ・現場の呼び名 = このツールの名前)
    old: str                       # 前(VBA の名前。旧名の表・CSV を読むときだけ)
    numbered: bool = False         # ラインNO とセット(中板)

    @property
    def code(self) -> str:
        """このツールの名前(v4.13.0 から正規の呼び名と同じ)。"""
        return self.official

    @property
    def renamed(self) -> bool:
        """前と正規で字が違うか(違うラインだけ、旧名の表を写す)。"""
        return self.old != self.official


#: **定義の表。** ツールの全ラインがちょうど1回ずつ出てきます(試験で確かめる)。
#: 並びは選ぶ欄の並び(L-1・LVC・HVC・機側・NS1・AIM…)
DEFINITIONS: tuple[LineName, ...] = (
    LineName("L-1", old="L1"),
    LineName("LVC", old="LVC"),
    LineName("HVC", old="HVC"),
    LineName("機側", old="LS"),
    LineName("NS1", old="NS1"),
    LineName("AIM", old="AIM"),
    LineName("トット", old="TOT"),
    LineName("バランサー", old="BALA"),
    LineName("中板", old="MARU", numbered=True),
)

#: 中板のラインNO(= MARU の設備番号)
NUMBERS: tuple[str, ...] = tuple(str(i) for i in range(1, 8))


def same(text: object) -> str:
    """比べる形。**文字の幅と前後の空白だけ**を揃える(大文字小文字・記号はそのまま)。"""
    return unicodedata.normalize("NFKC", "" if text is None else str(text)).strip()


_BY_OFFICIAL = {same(d.official): d for d in DEFINITIONS}
_BY_CODE = {d.code: d for d in DEFINITIONS}
_BY_OLD = {d.old: d for d in DEFINITIONS}


def by_official(text: object) -> Optional[LineName]:
    """正規の呼び名 → 定義。正規でなければ None(前の名前 `L1` `LS` も None)。"""
    return _BY_OFFICIAL.get(same(text))


def by_old(text: object) -> Optional[LineName]:
    """前(VBA の名前)→ 定義。**字のとおり**(幅だけ揃える)。前でなければ None。"""
    return _BY_OLD.get(same(text))


def upgrade(text: object) -> str:
    """**VBA が貯めたもの・v4.12 までの値を読むとき**の読み替え。前 → 正規。

    `LS` → `機側`、`MARU` → `中板`。正規の呼び名は正規の字に揃えて返し
    (`ﾄｯﾄ` → `トット`)、どちらでもない値はそのまま返します(「未設定」など)。
    **マスタには使いません**(マスタは正規だけを読む ── `read`)。
    """
    raw = "" if text is None else str(text)
    found = by_old(raw) or by_official(raw)
    return found.official if found else raw


def upgrade_key(text: object) -> str:
    """`MARU:3` / `L1` の形(アクセス権限を前に当てた印)を正規へ。"""
    raw = "" if text is None else str(text)
    head, sep, tail = raw.partition(":")
    return f"{upgrade(head)}{sep}{tail}"


def official_of(code: str) -> str:
    """ツールの名前・前の名前 → 正規の呼び名(無ければそのまま)。"""
    return upgrade(code)


def label(code: object, number: object = "") -> str:
    """**画面と紙に出す呼び名**。中板は設備番号があれば「中板4」。

    v4.13.0 からツールの名前が正規の呼び名なので、ほぼそのまま返します。
    前の名前(`LS`)が来ても正規で出します(v4.12 までに作った手元のデータ)。
    知らない値はそのまま返します(「未設定」「—」もそのまま)。
    """
    text = "" if code is None else str(code)
    found = _BY_CODE.get(text) or _BY_OLD.get(text)
    if found is None:
        return text
    numbered = str(number or "").strip()
    return f"{found.official}{numbered}" if (found.numbered and numbered) else found.official


def codes() -> list[str]:
    """ツールの名前を**定義の表の順**で(選ぶ欄の並び。L-1・LVC・HVC・機側・NS1・AIM…)。"""
    return [d.code for d in DEFINITIONS]


def renamed() -> list[LineName]:
    """前と正規で字が違うライン(L-1・機側・トット・バランサー・中板)。"""
    return [d for d in DEFINITIONS if d.renamed]


def labels() -> dict[str, str]:
    """名前 → 見せる字(画面の JS が使う。`base.html` の `data-line-labels`)。

    前の名前も正規へ引けるように入れておきます(v4.12 までの手元のデータ)。
    """
    table = {d.old: d.official for d in DEFINITIONS}
    table.update({d.code: d.official for d in DEFINITIONS})
    return table


def to_code(text: object) -> Optional[str]:
    """マスタの呼び名 → ツールの名前(正規の字に揃える)。`中板` `中板3` も 中板。**番号は見ない**。"""
    found = read(text)
    return found.code if found.code else None


@dataclass(frozen=True)
class Reading:
    """1つの呼び名を読んだ結果。"""

    code: str = ""                 # ツールの名前(= 正規の呼び名。読めなければ空)
    number: str = ""               # 中板のラインNO(1〜7。無ければ空)
    problem: str = ""              # 読めたが決められない理由(ラインNO が無い など)


#: `中板3` の形(名前のすぐ後ろに数字。**空白や記号を挟んだものは読まない**)
_INLINE = re.compile(r"^(?P<name>[^\s\d\W]+)(?P<number>\d+)$")


def read(text: object) -> Reading:
    """呼び名を読む。正規でなければ空の Reading。"""
    found = by_official(text)
    written = ""
    if found is None:
        # `中板3` ── ラインNO とセットのラインだけ、数字を続けて書ける
        match = _INLINE.match(same(text))
        if match:
            head = by_official(match.group("name"))
            if head is not None and head.numbered:
                found, written = head, match.group("number")
    if found is None:
        return Reading()
    if not found.numbered:
        return Reading(code=found.code)
    given = written
    if not given:
        return Reading(code=found.code,
                       problem=f"{found.official} はラインNO(1〜7)とセットで書いてください")
    if given not in NUMBERS:
        return Reading(code=found.code,
                       problem=f"{found.official} のラインNO「{given}」は 1〜7 ではありません")
    return Reading(code=found.code, number=given)


def hint(text: object) -> str:
    """読めなかった呼び名に添える一言(正規の書き方が分かるときだけ)。"""
    value = same(text)
    found = _BY_OLD.get(value) or _BY_OLD.get(value.upper())
    if found is None:
        # `l-1` `L―1` など: 記号と大文字小文字を外せば正規か前の名前に当たる
        loose = re.sub(r"[\W_]+", "", value).upper()
        found = next((d for d in DEFINITIONS
                      if loose in (re.sub(r"[\W_]+", "", same(d.official)).upper(), d.old)),
                     None)
    if found is None:
        return ""
    return f"正規は「{found.official}」"


def check(text: object) -> str:
    """**ラインのつもりで書いたのに読めない値**なら、その理由。読める・ラインでない値は空。

    正規しか読まないので、書き間違いは黙って読まれません。それに気づくための一言です
    (マスタ管理で直すとき・設定の画面の点検・ライン毎目標の取り込み)。
    """
    value = same(text)
    if not value:
        return ""
    written = str(text).strip()                   # 言うときは**書いてあるまま**
    found = read(value)
    if found.code:
        return found.problem                      # 中板 だけ / 中板9
    clue = hint(value)
    if clue:
        return f"「{written}」は読みません({clue})"
    for d in DEFINITIONS:
        if d.numbered and value.startswith(same(d.official)):
            return (f"「{written}」は読みません({d.official}は「{d.official}3」のように"
                    "ラインNO を続けて書きます)")
    return ""                                     # ラインではない値(mode:field など)


def table_rows() -> list[dict[str, str]]:
    """画面・README に出す形(正規 / 前)。"""
    return [{"official": (f"{d.official}1〜{d.official}7(ラインNO を続けて)" if d.numbered
                          else d.official),
             "old": d.old} for d in DEFINITIONS]

