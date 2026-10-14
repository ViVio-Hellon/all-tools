"""中身の無い紙 ── **残っているものを、見えるようにして消せるようにする**

    残っているのか見えなければ消せないですよね

v3.61.2 より前は、**作業者を選んだだけで1行も打っていない紙が保存されて
いました**(自動保存が走るため)。作らないようにはしましたが、**すでに
できてしまったぶんは残ったまま**です。残ったままだと:

    共有へ未送信 ◯直ぶん に数えられる
    紙の束に空の紙が並ぶ
    直が終わると「まだ共有へ渡していません」と全画面が出る
    ところが休憩60分が無いので共有へは出せない ── 片付かない

**数が出るだけでは消せません。** どの日の・どの直の・どのページなのかが
見えて、そこから消せて初めて片付きます。

【何を「中身が無い」と見るか】
12行の欄を**ぜんぶ**見て、1文字も入っていないものだけです。作業者は
見ません ── 作業者を選んだだけでは、まだ何もしていないので(保存の側の
`presenters/entry.sheet_has_anything` と同じ見方)。

停止だけの行も、理由だけの行も**中身**です。「12行のうち何行使ったか」
(`used_rows`)とは別の問いなので、そちらは使いません。

【1行目の開始だけなら空 (v4.35.2)】

    開始だけのページは(転記された開始)空と同じ扱いでよいです

2ページ目からは、前のページの最後の終了が1行目の開始に入って描かれます
(`navigation.carry_across_pages`)。何も打たずにページを切り替えると、
移る前の打ちかけ(`saveDraft`)でその開始だけが書かれ、**空のページなのに
「中身の無い紙」に数えられず、片付けられませんでした。**

なので**1行目の開始(時・分)だけ・ほかは空**のページは空と見ます。人が
1行目の開始だけを打ったのか、前のページから写ったのかは見分けられません
── 見分けずに空とします。開始だけの行は「使った行」にも数えない
(`pages.row_counts`)のと同じ向きで、迷ったら空です。2行目から下の開始
だけ・1行目の終了だけは写ってこない形なので、これまでどおり中身です。

**「空か」の判断はここ1か所です。** 片付け(`services/empty_pages`)・
空の紙を作らない保存の関門(`presenters/entry.sheet_has_anything`)・
「この直をチェック」の画面のページ(`services/shift_check`)・集計の
ロット行(`packing_agg.build_rows`)は、みなここを通ります。

【共有へ渡したものは消しません】
手元から消しても、共有の日報管理には残ります ── 片方だけ消すと、
どちらが本当か分からなくなります。**一覧には出して、消せない理由を
添えます。**
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence
from . import line_names

#: 行が持つ欄。**紙に出ない欄も見ます** ── 中身は中身なので
_SKIP = {"report_date", "line", "shift", "page", "row_no"}


#: 前のページから写ってくる欄(1行目の開始 ── v4.35.2)。**これだけなら空**
CARRIED_ROW = 1
CARRIED_FIELDS = frozenset({"kz", "kh"})


def _filled(values: Mapping[str, Any]) -> set[str]:
    """その行で何か入っている欄の名前(小文字)。画面の `KZ` も DB の `kz` も同じに見る。"""
    return {str(name).lower() for name, value in values.items()
            if name not in _SKIP and str(value or "").strip()}


def row_is_empty(detail: Any) -> bool:
    """その行に1文字も入っていないか。"""
    return not _filled(vars(detail))


def rows_are_empty(rows: Iterable[tuple[int, Mapping[str, Any]]]) -> bool:
    """(行番号, 欄 → 値) の並びから、その紙を空と見るか。

    空と見るのは、どの行にも1文字も無いか、**1行目の開始だけ**のとき
    (前のページから写る形。上の【1行目の開始だけなら空】)。画面の12行
    (`GridState`)も、DBの明細(`DetailRecord`)も、ここで決めます。
    """
    for row_no, values in rows:
        filled = _filled(values)
        if not filled:
            continue
        if int(row_no or 0) == CARRIED_ROW and filled <= CARRIED_FIELDS:
            continue                    # 1行目の開始だけ(写っただけ・開始だけ)
        return False
    return True


def is_empty(details: Iterable[Any]) -> bool:
    """その紙を空と見るか。**行が1つも無いのも空です。**

    1文字も入っていない紙のほか、**1行目の開始だけ**の紙も空です(v4.35.2)。
    """
    return rows_are_empty((d.row_no, vars(d)) for d in details)


@dataclass(frozen=True)
class Page:
    """中身の無い紙、1枚。"""

    report_date: str = ""
    line: str = ""
    shift: str = ""
    page: int = 1
    worker: str = ""
    saved_at: str = ""
    #: 共有へ渡してあるか。**渡してあるものは消しません**
    synced: bool = False

    @property
    def key_text(self) -> str:
        return f"{self.report_date} {line_names.label(self.line)} {self.shift} 第{self.page}ページ"

    @property
    def can_delete(self) -> bool:
        return not self.synced

    @property
    def note(self) -> str:
        """消せないときの理由。消せるときは空。"""
        if self.synced:
            return ("共有へ渡してあるので、ここからは消せません"
                    "(手元だけ消すと、どちらが本当か分からなくなります)")
        return ""

    def as_dict(self) -> dict[str, Any]:
        return {"report_date": self.report_date, "line": self.line,
                "shift": self.shift, "page": self.page, "worker": self.worker,
                "saved_at": self.saved_at, "synced": self.synced,
                "key_text": self.key_text, "can_delete": self.can_delete,
                "note": self.note}


@dataclass(frozen=True)
class Found:
    """一覧ぜんぶ。**画面はこれを写すだけ。**"""

    pages: tuple[Page, ...] = ()

    @property
    def count(self) -> int:
        return len(self.pages)

    @property
    def deletable(self) -> int:
        return sum(1 for p in self.pages if p.can_delete)

    @property
    def headline(self) -> str:
        if not self.pages:
            return "中身の無いページはありません"
        head = f"中身の無いページが {self.count}枚 あります"
        if self.deletable < self.count:
            head += (f"(そのうち {self.deletable}枚 が消せます"
                     f" ── 残りは共有へ渡してあります)")
        return head

    @property
    def note(self) -> str:
        if not self.pages:
            return ""
        # **画面にそのまま出る字です。** 飾り(アスタリスク)は書きません
        return ("1行も打っていないページです(前のページから写った1行目の開始"
                "だけのページも含みます)。共有へ未送信に数えられ、直の"
                "終わりに「まだ共有へ渡していません」と出る元になります。"
                "打ってあるページはこの一覧に出ません。")

    def as_dict(self) -> dict[str, Any]:
        return {"pages": [p.as_dict() for p in self.pages],
                "count": self.count, "deletable": self.deletable,
                "headline": self.headline, "note": self.note}


def build(pages: Sequence[Page]) -> Found:
    """古いものから並べる ── **片付ける順**です。"""
    return Found(pages=tuple(sorted(
        pages, key=lambda p: (p.report_date, p.line, p.shift, p.page))))
