"""ページを増やすときの歯止め (VBA の印刷用シート3枚ぶん)

【VBA は3ページで打ち止めだった】
ブックに印刷用シートが3枚しか無く、`日報入力` はそれを if/elseif で
並べていました:

    If  flag3 And flag2 And flag  Then z = 3   ' temp(3)
    ElseIf  flag2 And flag        Then z = 2   ' temp(2)
    ElseIf  flag                  Then         ' temp
    Else  MsgBox "新規発行してください"

つまり **12行 × 3ページ = 36ロット/直** が天井でした。業務としてそう決めた
というより、シートを3枚用意したらそうなった、という性質のものです。
37本目が出たら、VBA は打つ場所が無くなります。

【Web版は止めない。**一声かけるだけ。**】
シートという制約はもう無いので、制約だけを移植する理由がありません。
そのうえで青天井にもしません ── 押し間違いでページが増え続けても誰も
気づけないので、いつもと違うことが起きたら言います。

**止めないのが肝心です。** 直の途中で「もう打てません」になるのが
日報ツールとして一番まずい止まり方で、現場は紙に書いて後で入れ直す
ことになり、その日のデータがまるごと怪しくなります。

【全停は直に1回】
「直全体を通して設備が完全停止していた」という入力なので、同じ直に
2つあるのは形として成り立ちません。VBA は全停のときに**発行済みの
シートを消して**から書いていました(「残すと(2)がでてうざい」)。
こちらは消さずに、空のページがあればそこへ書き、既に全停が入っていれば
断ります ── 打ってあるものを黙って消さない、が優先です。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

#: VBA が用意していた印刷用シートの枚数。**上限ではなく目安**
USUAL_PAGES = 3

#: 紙1枚の行数(`constants.ROW_COUNT` と同じ。文言に出すために持つ)
ROWS_PER_PAGE = 12


def new_page_warning(latest_page: int, *,
                     usual: int = USUAL_PAGES) -> str:
    """新しいページを出す前の一声。**要らなければ空文字。**

    `latest_page` はいま開いている(=最後の)ページ。これが `usual` 以上なら、
    次に作るのは4ページ目以降です。

    **断りではありません。** 画面はこれを見せて「はい」を待つだけで、
    押されたら通します。20年間、1直で36ロットを超えたことはない、と
    いう話なので、ここに来た時点でたいていは誤操作です ── だからと
    いって打てなくすると、本当に必要だった1回で困ります。
    """
    if latest_page < usual:
        return ""
    return (f"この直はすでに{latest_page}ページあります"
            f"({usual}ページ = {usual * ROWS_PER_PAGE}行が、いつもの上限です)。\n"
            "続けて新しいページを出しますか?")


def all_stop_refusal(has_all_stop_page: Optional[int]) -> str:
    """全停入力を断るなら理由、通してよければ空文字。

    直に1回きりの入力なので、2回目は断ります ── いまは押すたびにページが
    増え、同じ直に全停が2つ並んだ日報が出来てしまいます。

    **直し方まで書きます。** 理由を選び間違えただけのときに「断られた」
    で終わると、消す手立てを探すことになります。

    「そのページを開いて直してください」だけでは足りませんでした ──
    **開き方も、消し方も書いていない**ので、「消しても直せません」
    「どうすんのこれほんとに」で止まります。押すものの名前まで書きます。
    """
    if not has_all_stop_page:
        return ""
    return (f"この直には、すでに全停入力があります(第{has_all_stop_page}ページ)。\n"
            f"理由を変えるときは、下の「第{has_all_stop_page}ページを開く」を押し、"
            "1行目の停止理由を選び直して「保存(確定)」してください。\n"
            f"全停そのものを取り消すときは、同じページの1行目を行番号の横の "
            "✕ で空にしてから「保存(確定)」すると、もう一度「全停入力」を"
            "押せるようになります。")


def is_all_stop_row(*, lot: str = "", kz: str = "", kh: str = "",
                    sz: str = "", sh: str = "", s: str = "") -> bool:
    """その行が**全停で書かれた行**か。

    全停は1行目に「直の開始〜終了・停止理由・実働分」だけを書きます
    (`logic/stop_reason.build_all_stop_entry`)。**ロットが無いのに直
    まるごとの時間と停止理由が入っている**のが目印です ── ふつうの行は
    必ずロットから始まります。

    欄の名前ではなく値で受けるのは、DBの行(`DetailRecord`)からも画面の
    入力からも同じように呼べるようにするためです。
    """
    if str(lot or "").strip():
        return False                              # 打った行(全停ではない)
    filled = all(str(v or "").strip() for v in (kz, kh, sz, sh))
    return filled and bool(str(s or "").strip())


# ----------------------------------------------------------------------
# 行がどこまで打ってあるか (v4.8.0)
#
#     日報入力でページを増やす際は12行目が埋まっていることをチェック
#
# 【12行目まで埋まってから(v3.x から)】
# 「次ページ発行」は**いまのページを確定してから**次を作ります。1行だけ
# 打って押せてしまった頃は、1行の紙が何枚も残りました ── 紙は12行の罫線が
# 引かれた用紙で、途中で切り上げて次の紙へ行くものではありません。打って
# あるページを消す道は作りたくないので(押し間違いで12行が消える)、出す
# 前に止めます。VBA も12行を使い切ったときだけ「新規発行してください」と
# 言っていました。
#
# 【v4.8.0 ── 写っただけの開始時刻で通っていた】
# 前は「12行目に何か入っていれば埋まっている」と見ていました。ところが
# **11行目の終了を打つと、その時刻が12行目の開始へ自動で写ります**
# (`navigation.same_text`)── 11行しか打っていないのに12行目が「埋まって
# いる」ことになり、次のページが出せていました(ブラウザで確かめて分かった)。
#
# そこで行の中身で見分けます:
#
#     empty       … 何も無い
#     start_only  … 開始時刻だけ(上の行の終了から写ったもの、が普通)
#     unfinished  … ロット№などはあるが、終了時刻がまだ
#     filled      … 終了時刻まで入っている(打ち終わった行)
#
# 実物の日報は、どの行も開始と終了が入っていました(261/261行)。
# ----------------------------------------------------------------------
ROW_EMPTY = "empty"
ROW_START_ONLY = "start_only"
ROW_UNFINISHED = "unfinished"
ROW_FILLED = "filled"

#: 開始時刻のほかに「打った」と言える欄(ロット・終了・梱包数量・重量)。
#: **停止の記号だけの行は数えません**(紙の行数を数えるもの。前から同じ)
_CONTENT_FAMILIES = ("LOT", "SZ", "SH", "MAI", "TUT", "WEI")


def _has(values: Mapping[str, str], family: str) -> bool:
    return bool(str(values.get(family) or "").strip())


def row_state(values: Optional[Mapping[str, str]]) -> str:
    """その行がどこまで打ってあるか(上の4つのどれか)。"""
    values = values or {}
    if _has(values, "SZ") and _has(values, "SH"):
        return ROW_FILLED
    if any(_has(values, f) for f in _CONTENT_FAMILIES):
        return ROW_UNFINISHED
    if _has(values, "KZ") or _has(values, "KH"):
        return ROW_START_ONLY
    return ROW_EMPTY


def row_counts(state_of_row: str) -> bool:
    """紙の行として数えるか。**開始時刻だけの行は数えない**(写っただけなので)。"""
    return state_of_row in (ROW_FILLED, ROW_UNFINISHED)


@dataclass(frozen=True)
class NewPageCheck:
    """「次ページ発行」を押してよいか。**断りと、確かめの一言を分けて持つ。**"""

    #: 出してよいか(12行目が終了まで入っている、かつ空のページではない)
    ready: bool
    #: 出せないときの理由(出せるなら空)
    refusal: str
    #: 使った行の数(開始時刻だけの行は数えない)
    used: int
    #: 12行目より上で、まだ空(か開始時刻だけ)の行。**断らずに確かめる**
    gaps: tuple[int, ...] = ()

    def gap_question(self) -> str:
        """空いている行があれば、押す前の一言。無ければ空。"""
        if not self.gaps:
            return ""
        rows = "・".join(f"{r}行目" for r in self.gaps)
        return (f"{rows}が空のまま、次のページへ進みます。\n"
                "空けたままでよければ OK、打つときは「キャンセル」を押してから打ってください。")


def new_page_check(rows: Mapping[int, Mapping[str, str]],
                   row_count: int = ROWS_PER_PAGE) -> NewPageCheck:
    """**12行目が終了まで入っているか**で、次のページを出してよいかを決める。

    断るのは3つ(どれも「どうすれば出せるか」まで言う):

        空のページ          … まだ1行も打っていない
        12行目が空          … 途中で次の紙へ行くと、短いページが残る
        12行目が途中まで    … 開始時刻だけ(写っただけ)・終了がまだ

    12行目より上に空いた行があるときは**断りません** ── ✕ で空にした行を
    詰めずに残すことはある(紙の行番号を変えないため)。そのかわり押す前に
    確かめます(`gap_question`)。
    """
    states = {r: row_state(rows.get(r)) for r in range(1, row_count + 1)}
    used = sum(1 for s in states.values() if row_counts(s))
    last = states[row_count]
    gaps = tuple(r for r in range(1, row_count) if not row_counts(states[r]))
    # 断りは**1行で**(v4.9.0 ── 長いと読まれない)。何をすれば出せるかで終える
    if used == 0:
        return NewPageCheck(False, "いまのページはまだ空です。打ってから出してください。", used)
    if last == ROW_EMPTY:
        return NewPageCheck(False, (
            f"{row_count}行目まで打ってから出してください(いま {used}/{row_count}行)。"),
            used, gaps)
    if last == ROW_START_ONLY:
        return NewPageCheck(False, (
            f"{row_count}行目はまだ開始時刻だけです({row_count - 1}行目の終了から写ったもの)。"
            "終了まで打つと出せます。"), used, gaps)
    if last == ROW_UNFINISHED:
        return NewPageCheck(False, (
            f"{row_count}行目の終了時刻がまだです。終了まで打つと出せます。"), used, gaps)
    return NewPageCheck(True, "", used, gaps)


def new_page_confirm(page: int, next_page: int) -> str:
    """押したときの確かめ。**1行で言う**(v4.9.0 ── 長いと読まれない。
    引き継ぐものは案内の一行に、直すところは止まったときに言う)。"""
    return f"第{page}ページを保存して、第{next_page}ページを出します。よろしいですか?"


def used_page(lots: Iterable[str]) -> bool:
    """そのページに何か打ってあるか(ロットが1つでもあれば「打ってある」)。"""
    return any(str(lot or "").strip() for lot in lots)
