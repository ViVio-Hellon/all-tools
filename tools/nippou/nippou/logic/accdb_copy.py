"""日報データ.accdb から写すときの決め方 ── ページごとに、足す・新しくする・そのまま(v4.36.0)

    「日報データ.accdb から写す」ボタンを作ってよいか → お願いします
    VBA の日報をまだしばらく使い続けるか → 使います

VBA は今後も `日報データ.accdb` に書き続けます。このツールは共有の `日報データ.sqlite3` を
読み書きします。写すボタンは**何度でも押す**ものなので、押すたびにページ(報告日・ライン・
直・ページ)ごとに次のどれかに決めます(`decide`):

    足す          写す先(共有の日報データ)に無い                         → 写す
    新しくする    両方にあり、Access のほうが保存日時が新しい(VBA で直した) → 入れ替える
    そのまま      両方にあり、保存日時が同じ(前に写したまま)               → 何もしない
    共有を残す    両方にあり、写す先のほうが新しい(このツールで共有へ保存した)、
                  または保存日時を比べられない                              → **触らない**

**このツールが書いたページを、古い Access の中身で上書き・消去しません。** 共有へ保存すると
そのときの保存日時が入るので、Access の保存日時より新しくなります(「共有を残す」)。
VBA のほうであとから直したページは、Access の保存日時のほうが新しいので入れ替えます
(コンバートの `logic/line_rename.decide` と同じ「新しいほうを残す」向き)。

**Access から消えたページは消しません。** 写す先にだけあるページは、Access に無くても
そのままです(VBA で消したのか、このツールで足したのかを、写す先からは見分けられない ──
消すと取り戻せないので、残すほうに倒します)。

**2回続けて押しても変わりません。** 写すときに Access の保存日時をそのまま(ISO の形にして)
写すので、次に押すと「そのまま」になります(`saved_text`)。

【値は打ったままの字で写す】

    .accdb から写す はほかのツールでもある機能ですが、数値の丸め方が間違っていたり、
    重複が発生したり不具合が多数出ました 注意してください

明細の値は Access でも文字(テキスト型)です。**数に直さない・丸めない・`08` を 8 に
しない・`1.50` を 1.5 にしない・全角を半角にしない・前後の空白も削らない** ── 「共有へ
保存」が文字をそのまま書くのと同じ扱いです。ページ・行番号(長整数)は整数のまま、
保存日時(日時)は秒まで(Access の日時は日数の小数なので、10:35:05 が 10:35:04.962 と
読める ── 一番近い秒に丸める。切り捨てない・時差はずらさない)。

**揃えるのはページの鍵だけ**です(`normal_date` / `normal_shift`)。報告日が `2026/7/22` と
`2026年7月22日` で書き分けられていると、同じページが2つに見えて重なるので、このツールの
書き方(`2026年7月22日`)に揃えてから比べ、その形で写します。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Mapping, Optional

from .line_rename import PageKey, number_of, text_of  # コンバートと同じページの形

ADD = "足す"
UPDATE = "新しくする"
SAME = "そのまま"
KEEP = "共有を残す"

#: 画面に出す順
KINDS: tuple[str, ...] = (ADD, UPDATE, SAME, KEEP)

#: 保存日時の読み方。Access(写したもの)は ISO、このツールは ISO、VBA の文字は `/` 区切り
_FORMATS: tuple[str, ...] = (
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M",
)


def _rounded(value: datetime) -> datetime:
    """秒に丸める。Access の日時は日数の小数なので、10:00:00 が 09:59:59.999996 で読める。"""
    if value.microsecond >= 500_000:
        value += timedelta(seconds=1)
    return value.replace(microsecond=0, tzinfo=None)


def when(value: object) -> Optional[datetime]:
    """保存日時 → 日時(秒まで)。読めなければ None。"""
    if isinstance(value, datetime):
        return _rounded(value)
    text = text_of(value).strip()
    if not text:
        return None
    for fmt in _FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return _rounded(datetime.fromisoformat(text))
    except ValueError:
        return None


def saved_text(value: object) -> str:
    """写す先へ書く保存日時。**ISO の形(秒まで)**(このツールが共有へ保存するときと同じ形)。

    読めない字はそのまま写します(捨てると、次に押したときに比べられなくなる)。
    """
    found = when(value)
    if found is not None:
        return found.isoformat(timespec="seconds")
    return text_of(value).strip()


_DATE_FORMATS: tuple[str, ...] = ("%Y/%m/%d", "%Y-%m-%d", "%Y.%m.%d")


def normal_date(value: object) -> str:
    """報告日を**このツールの書き方**(`2026年7月22日` ── 月日に0を付けない)に。

    読めない字はそのまま返します(勝手に別の日にしない)。
    """
    from datetime import date

    from .shift import parse_business_date

    def format_business_date(d: date) -> str:
        # `services/nippou_service.format_business_date` と同じ形(VBA の Format$ のまま)
        return f"{d.year}年{d.month}月{d.day}日"

    if isinstance(value, datetime):
        return format_business_date(value.date())
    if isinstance(value, date):
        return format_business_date(value)
    text = text_of(value)
    found = parse_business_date(text)
    if found is not None:
        return format_business_date(found)
    head = text.strip().replace("T", " ").split(" ")[0]
    for fmt in _DATE_FORMATS:
        try:
            return format_business_date(datetime.strptime(head, fmt).date())
        except ValueError:
            continue
    return text


def normal_shift(value: object) -> str:
    """直を**このツールの書き方**(`1直`)に。全角の `１直`・前後の空白だけ揃える。

    それ以外の字はそのまま返します(知らない直を作らない)。
    """
    import re
    import unicodedata

    text = text_of(value)
    folded = unicodedata.normalize("NFKC", text).strip()
    return folded if re.fullmatch(r"[1-3]直", folded) else text


def page_number(value: object) -> Optional[int]:
    """ページ・行番号を数に。数でなければ None(そのページは写さない)。"""
    found = number_of(value)
    return found if isinstance(found, int) and not isinstance(found, bool) else None


@dataclass
class Plan:
    """1つのラインで、ページごとに決めたこと。"""

    pages: dict[PageKey, str] = field(default_factory=dict)
    #: 「共有を残す」のうち、保存日時を比べられなかったページ
    unknown: int = 0

    def keys(self, kind: str) -> list[PageKey]:
        return sorted((k for k, v in self.pages.items() if v == kind), key=_sort_key)

    def count(self, kind: str) -> int:
        return sum(1 for v in self.pages.values() if v == kind)

    def counts(self) -> dict[str, int]:
        return {kind: self.count(kind) for kind in KINDS}

    @property
    def writes(self) -> bool:
        """写す先に書くことがあるか(足す・新しくする)。"""
        return any(self.count(kind) for kind in (ADD, UPDATE))


def _sort_key(key: PageKey) -> tuple:
    date, shift, page = key
    from .shift import parse_business_date

    day = parse_business_date(date)
    return (day.toordinal() if day else 0, date, shift,
            (0, page) if isinstance(page, int) else (1, str(page)))


def decide(source: Mapping[PageKey, str], dest: Mapping[PageKey, str]) -> Plan:
    """ページごとに決める。

    `source` Access のページ → 保存日時(`saved_text` の形。見出しが無ければ空)
    `dest`   写す先のページ → 保存日時(明細だけのページは空)

    写す先にだけあるページは、ここに出てきません(**消さない**)。
    """
    plan = Plan()
    for key, saved in source.items():
        if key not in dest:
            plan.pages[key] = ADD
            continue
        mine, theirs = when(saved), when(dest[key])
        if mine is None or theirs is None:
            # 比べられない。同じ字なら写し済み、違えば写す先を残す(上書きしない)
            if text_of(saved) == text_of(dest[key]):
                plan.pages[key] = SAME
            else:
                plan.pages[key] = KEEP
                plan.unknown += 1
        elif mine > theirs:
            plan.pages[key] = UPDATE
        elif mine == theirs:
            plan.pages[key] = SAME
        else:
            plan.pages[key] = KEEP
    return plan
