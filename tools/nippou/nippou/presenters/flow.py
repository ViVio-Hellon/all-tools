"""直ひと回りの動線 ── **いまどこで、次に何をするか**

【何が足りていなかったか】
画面の名前も置き場所もそろえましたが、**順番が画面に出ていません**
でした。日報入力の帯は

    作業者 → 日報を入力 → 保存(確定)

までで終わっていて、**保存したあとに何が残っているかを誰も言わない**。
直の終わりの「確かめる」も「共有へ保存」も、覚えている人しかやらない
作業になっていました(VBA も同じで、印刷ボタンを押した人だけが
`ExecutePrintProcess` の中でチェックを通っていた)。

そこで直ひと回りを1本の帯にします:

    作業者 → 日報を入力 → 保存(この端末) → 確かめる → 共有へ保存

ラインはこの帯に入れません ── **直ひと回りの作業ではなく、据え付けの
ときに1度決めるもの**です(いま何なのかは上の帯に出ています)。

**同じ帯を、関わる画面すべてに出します**(日報入力 / 記録を見る /
設定・管理者)。画面ごとに違う帯だと、行った先で順番が途切れます。
済んだものには ✓、いまやることが1つだけ立ち、そこに**行き先のボタン**
が付きます ── 「次にすること」を探させない。

【判断はDBを読んで決める】
画面の入力ではなく**保存されている中身**を見ます。別の端末で保存した
ぶんも、表を直に書き換えたぶんも、同じ関門を通ります
(`services/shift_check.py` と同じ思想)。

【重いことはしない】
「確かめる」の件数は保存前チェックを実際に走らせて数えます。3ページ36行で
2ms 程度(マスタは写しから読む)なので、画面を出すたびに数えても
問題になりません。失敗しても**帯は出します** ── 件数が出ないだけ。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..logging_setup import get_logger

log = get_logger("presenters.flow")

#: 紙は動線に入れない。**要るときだけ**で、順番のどこでもない
PAPER_NOTE = "印刷は要るときだけ(いつでも「記録を見る」から)"


@dataclass
class Step:
    """帯の1つ。**押せる先まで持つ。**"""

    key: str
    label: str
    note: str = ""
    done: bool = False
    current: bool = False
    #: いまやることのとき、そこへ行くボタン。空なら「この画面でやる」
    url: str = ""
    action: str = ""
    #: この画面でやること(ボタンのid)。JSが繋いである押しボタンを指す
    focus: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "note": self.note,
                "done": self.done, "current": self.current,
                "url": self.url, "action": self.action, "focus": self.focus}


@dataclass
class Flow:
    """直ひと回り。画面はこれを写すだけ。"""

    steps: list[Step] = field(default_factory=list)
    #: いまやること(全部済んでいれば None)
    current: Optional[Step] = None
    #: 帯の頭に出す一行
    headline: str = ""
    paper_note: str = PAPER_NOTE

    def as_dict(self) -> dict[str, Any]:
        return {"steps": [s.as_dict() for s in self.steps],
                "headline": self.headline,
                "paper_note": self.paper_note,
                "current": self.current.key if self.current else ""}


def _findings(repo, report_date: str, line: str, shift: str) -> Optional[int]:
    """保存前チェックの指摘件数。数えられなければ None。

    **止める判断はしません。** ここは数を出すだけで、断るのは
    「共有へ保存」の側です(`services/shift_check`)。
    """
    from ..services import shift_check

    try:
        return len(shift_check.run(repo, report_date, line, shift).findings)
    except Exception:                             # noqa: BLE001 - 帯は出す
        log.exception("動線の帯: チェックを数えられませんでした %s/%s/%s",
                      report_date, line, shift)
        return None


def _unsynced(repo, report_date: str, line: str, shift: str) -> Optional[int]:
    """共有へまだ渡していないページの数。数えられなければ None。"""
    try:
        return sum(1 for h in repo.pending_sync_headers()
                   if (h.report_date, h.line, h.shift)
                   == (report_date, line, shift))
    except Exception:                             # noqa: BLE001 - 帯は出す
        log.exception("動線の帯: 未保存を数えられませんでした")
        return None


#: ここから先が「直の終わりにやること」
END_OF_SHIFT = ("verify", "push")


def build(repo, report_date: str, line: str, shift: str, *,
          worker: Optional[str] = None, rows_used: Optional[int] = None,
          recall: bool = False, minutes_left: Optional[int] = None,
          warn_minutes: int = 15) -> Flow:
    """直ひと回りの帯を組み立てる。

    `worker` / `rows_used` を渡すと、**打っている途中の画面の値**で
    前半を判定します(保存前でも帯が進む)。渡さなければ保存済みの
    中身だけを見ます ── 記録や設定の画面には打ちかけがないので。

    `minutes_left` を渡すと、**直の終わりにやること**(確かめる・共有へ
    保存)を終わり間際まで「次にすること」にしません。9時間ある直の
    09:00 に「次は確かめる」と出すのは、案内として嘘です ── その時刻に
    やるべきなのは打ち進めることなので。
    """
    pages = repo.saved_pages(report_date, line, shift)
    saved = bool(pages)

    if worker is None or rows_used is None:
        header = None
        if saved:
            loaded = repo.load(report_date, line, shift, pages[0])
            header = loaded[0] if loaded else None
        worker = (header.worker if header else "") if worker is None else worker
        rows_used = (1 if saved else 0) if rows_used is None else rows_used

    has_worker = bool((worker or "").strip())
    has_rows = rows_used > 0

    # 後半は**保存されているものだけ**を見る。打ちかけは共有へ出せない
    findings = _findings(repo, report_date, line, shift) if saved else None
    unsynced = _unsynced(repo, report_date, line, shift)

    steps = [
        # ラインは**直ひと回りの作業ではない**(据え付けのときに1度決める
        # もの)ので、帯からは外しました。いま何なのかは上の帯に出ていて、
        # 変えるのは設定・管理者です
        Step("worker", "作業者", "「作業者を選ぶ」から", done=has_worker,
             url="/", action="日報入力へ", focus="open-staff"),
        Step("rows", "日報を入力", "上の行から順に", done=has_rows,
             url="/", action="日報入力へ"),
        Step("save", "保存(この端末)",
             f"{len(pages)}ページ 保存済み" if saved else "押すたび + 自動保存",
             done=saved, url="/", action="日報入力へ", focus="save"),
        Step("verify", "確かめる", _verify_note(saved, findings),
             done=saved and findings == 0,
             url="/", action="日報入力へ", focus="check-shift"),
        Step("push", "共有へ保存", _push_note(saved, unsynced),
             done=saved and unsynced == 0,
             url="/settings", action="設定・管理者へ", focus="push"),
    ]

    # **直の終わりの2つを、早すぎる時刻に「次」と言わない。**
    near_end = minutes_left is None or minutes_left <= warn_minutes
    if not near_end:
        for step in steps:
            if step.key in END_OF_SHIFT:
                step.note = f"直の終わりに(あと{minutes_left}分)"

    # **いまどこか**は「まだ済んでいない最初のもの」。終わり間際でなければ
    # 前半だけから選ぶ ── 打っている最中の「次」は、打ち続けることです
    pool = steps if near_end else [s for s in steps
                                   if s.key not in END_OF_SHIFT]
    current = next((s for s in pool if not s.done), None)
    for step in steps:
        step.current = step is current

    return Flow(steps=steps, current=current,
                headline=_headline(current, recall=recall,
                                   near_end=near_end,
                                   minutes_left=minutes_left))


def _verify_note(saved: bool, findings: Optional[int]) -> str:
    if not saved:
        return "保存してから(直の終わりに)"
    if findings is None:
        return "「この直を確かめる」から"
    if findings == 0:
        return "指摘なし"
    return f"直すところが{findings}件"


def _push_note(saved: bool, unsynced: Optional[int]) -> str:
    if not saved:
        return "直の終わりに1回"
    if unsynced is None:
        return "直の終わりに1回"
    if unsynced == 0:
        return "渡し済み"
    return f"未保存 {unsynced}ページ"


def _headline(current: Optional[Step], *, recall: bool, near_end: bool,
              minutes_left: Optional[int]) -> str:
    """帯の頭の一行。**次にやることを言い切る。**"""
    if recall:
        # 呼出中はこの帯の前提(いまの直)が崩れる。**黙って別の直の
        # 進み具合を出すほうが危ない**ので、そう言う
        return "過去データを開いています。下の順序は、いまの直のものです"
    if current is None:
        if near_end:
            return "この直はここまで済んでいます。お疲れさまでした"
        # 打ち終わっているが、まだ直の途中。**締めを急かさない**
        return "打ったぶんは保存できています。このまま作業を続けてください"
    head = f"次にすること: {current.label}"
    # 終わり間際は残り時間を添える ── 急ぐかどうかの判断材料
    if near_end and minutes_left is not None and minutes_left >= 0:
        head = f"直の終わりまであと{minutes_left}分。{head}"
    return head
