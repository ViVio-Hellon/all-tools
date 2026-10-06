"""この直の直すところを、**押す前から入力画面に出す**

【なぜ要るのか】

    設定タブ内で不備は当然確認できるとしても（管理者用）
    入力しか一般作業者は開かないので
    そこで何故ダメかが見えないと意味がないかと

そのとおりでした。7項目の結果が入力画面に出るのは、**何かを押して
断られたとき**だけです:

    「この直をチェック」を押した
    「保存(確定)」に断られた
    「共有へ保存」に断られた

前の直のぶんは引き継ぎの帯(`logic/handover.py`)が理由まで出したまま
にしています。ところが**自分がいま打っている直**は、直の終わりに
「共有へ保存」を押して断られるまで分かりません。しかも画面を塗り直すと
消えます ── 打ち続けているあいだ、何がひっかかるのかは画面のどこにも
無い、という形でした。

そこで、いまの直のぶんを**応答のたびに**返して、帯として置いたままに
します。押す前から読めます。

【出すからには、色を分ける】
7項目のうち2つ ── 最終時間(`SHIFT_END`)と休憩(`SHORT_BREAK`)── は
**直が終わっていなければ必ず足りません**(`save_checks.AT_SHIFT_END`)。
8時に1行目を打った人に、これを赤で出しては意味がありません。1行目から
赤い画面は、そのうち誰も読まなくなります。

    いま直すもの     … 梱包数・停止記号・開始と終了が同じ、など
                        **いま入っている値が、それだけで間違っている**
    直の終わりまでに … 最終時間・休憩
                        **まだ足りないだけ**。終わりまでに埋まればよい

【この層の約束】
ここは**純粋**。DBも時計も触らず、渡された結果を分けて言葉にするだけ。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .shift import about_shift

#: 帯の主語。**画面の見出しに出る字**(`app/templates/entry.html`)。
#: 上の帯(`logic/handover.WHO` = 前の直)と**対にして**読ませます。
WHO = "いまの直"

#: 帯の強さ。画面はこれで色を選ぶ(`msg--error` / `msg--todo` / `msg--ok`)
LEVEL_NONE = ""        # 出さない(まだ1ページも保存されていない)
LEVEL_OK = "ok"        # 直すところは無い
LEVEL_TODO = "todo"    # 残りは「直の終わりまでに」だけ
LEVEL_ERROR = "error"  # いま直すものがある


@dataclass(frozen=True)
class Standing:
    """いまの直が、共有へ出せる形になっているか。

    `now` と `at_end` の中身は `save_checks.Finding.as_dict()` の形の
    そのままです ── 画面は「この直をチェック」の結果と**同じ形で**
    出すので、混ぜても並べても同じに見えます。
    """

    #: 確かめられたか。1ページも保存されていなければ False(帯は出さない)
    counted: bool = False
    report_date: str = ""
    line: str = ""
    shift: str = ""
    #: いま直すもの
    now: tuple[dict, ...] = field(default_factory=tuple)
    #: 直の終わりまでに埋めるもの
    at_end: tuple[dict, ...] = field(default_factory=tuple)

    @property
    def level(self) -> str:
        if not self.counted:
            return LEVEL_NONE
        if self.now:
            return LEVEL_ERROR
        return LEVEL_TODO if self.at_end else LEVEL_OK

    @property
    def about(self) -> str:
        """**この帯はどの直のことか**(「9月12日 3直」)。

            上には赤文字でダメなところが書いてあって
            すぐ下には直すところはありませんって出てる

        上の赤い帯は**前の直**のこと、この帯は**いまの直**のことでした。
        どちらも主語を書いていなかったので、**同じ直について言い合って
        いる**ように読めます。主語を付けます。
        """
        return about_shift(self.report_date, self.shift) if self.counted else ""

    @property
    def headline(self) -> str:
        """帯の1行目。**何件あるかと、その意味まで。**

        主語(`about`)は見出しに出るので、ここでは「いまの直」と
        言い切ります ── 「この直」だと、上の帯の前の直と読めます。
        """
        if not self.counted:
            return ""
        if self.now:
            head = f"いまの直に、直すところが {len(self.now)}件 あります"
            if self.at_end:
                head += f"(ほかに、直の終わりまでに {len(self.at_end)}件)"
            return head + " ── このままでは共有へ保存できません。"
        if self.at_end:
            return (f"いまの直は、ここまで直すところはありません。"
                    f"直の終わりまでに埋めるものが {len(self.at_end)}件 あります。")
        return "いまの直に直すところはありません。このまま共有へ保存できます。"

    @property
    def any_left(self) -> bool:
        return bool(self.now or self.at_end)

    def as_dict(self) -> dict:
        return {"counted": self.counted, "level": self.level,
                "headline": self.headline, "any_left": self.any_left,
                "about": self.about, "who": WHO,
                "report_date": self.report_date, "line": self.line,
                "shift": self.shift,
                "now": list(self.now), "at_end": list(self.at_end)}


def split(findings: Iterable[dict], *, counted: bool = True,
          report_date: str = "", line: str = "", shift: str = "") -> Standing:
    """`shift_check` の結果を、**いま直すもの**と**終わりまでに**に分ける。

    受けるのは `Finding.as_dict()` の形(`at_shift_end` を持つ辞書)。
    判断そのものは `save_checks.AT_SHIFT_END` が持っていて、ここは
    その印で振り分けるだけです ── **同じことを2か所で決めない**ため。
    """
    now: list[dict] = []
    at_end: list[dict] = []
    for item in findings or ():
        if not isinstance(item, dict):             # pragma: no cover - 念のため
            continue
        (at_end if item.get("at_shift_end") else now).append(item)
    return Standing(counted=bool(counted), report_date=report_date,
                    line=line, shift=shift,
                    now=tuple(now), at_end=tuple(at_end))


def none() -> Standing:
    """**確かめられなかった。** 帯は出しません。

    1ページも保存されていないとき・読めなかったときに返します ──
    「直すところはありません」と出してしまうと、**確かめていないことを
    確かめたことにする**ので、そちらのほうが危ない。
    """
    return Standing()
