"""直の終わりに、集計を全画面で出して確認させる (VBA ``graphF``)

【何のためのものか】
直が終わる前に、その直の実績を**必ず一度は見せる**。VBA は `graphF` を
最大化して出し、閉じるまで作業に戻れないようにしていました。入力しっ放しで
終わると、間違いに気づくのは翌日の集計になります。

【いつ出すか ── 画面いっぱいは3つだけ(v4.45 の現場の決め)】

    (a) 入力画面の「共有へ保存」が通ったとき   ── `routes/settings._after_push_review`
    (b) 残り5分の自動確定で共有へ自動で送れたとき ── `evaluate_auto`(`STAGE_AUTO`)
    (c) 共有へ渡さないまま直が終わったとき(1度だけ)── `evaluate_ended`(`STAGE_END`)

**直の終わりの15分前は、画面いっぱいにしません**(`evaluate` → 帯の知らせ):

    まだ入力終わってなかったらどうするのさ?

打っている最中に画面ごと持って行かれると、打つ手が止まります。15分前は
「あと15分で直の終わりです。打ち終えたら「共有へ保存」を押してください」の
**帯**にして(閉じられる)、もう共有へ保存してある直には出しません。
5分前の自動確定で送れなかったとき(直すところがある・共有に届かない)も
帯で知らせ、入力画面に留めます(`evaluate_auto` の `notice`)。

判断は `logic/shift.ShiftCalculator` が持つ「あと何分か」をそのまま使います
── 印刷の催促と同じものさしなので、片方だけ直して食い違う、が起きません。

【一度確認したら、その直では出さない】
確認したかどうかは**プロセスが覚えます**(`ReviewGate`)。画面に覚えさせると
タブを開き直すたびに出ます。直が変われば忘れます ── 次の直では、また
その直の実績を見てもらう必要があるからです。

【「全画面」について】
ブラウザは、利用者の操作なしに本当の全画面(Fullscreen API)にできません。
なので既定は**画面いっぱいの覆い**(`position: fixed` で 100dvw×100dvh)に
します ── 操作なしで必ず出せて、下の画面は触れません。本当の全画面に
したいときのボタンも置いてあり、そちらは押した操作が起点になるので通ります。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime

from ..logging_setup import get_logger
from .shift import ShiftCalculator

log = get_logger("shift_review")

#: 出す機会。**同じ機会は直に1度まで**(閉じたら覚える ── `ReviewGate`)
STAGE_WARN = "warn"   # 終わりが近づいたとき(既定15分前)── **帯**(閉じたら覚える)
STAGE_END = "end"     # 直が終わったのに、まだ共有へ渡していないとき ── 画面いっぱい
STAGE_AUTO = "auto"   # 残り5分の自動確定で共有へ自動で送れたとき ── 画面いっぱい
STAGE_AUTO_FAIL = "autofail"  # 自動確定で共有へ送れなかったとき ── **帯**(閉じたら覚える)

#: 画面いっぱいにする機会(確認画面の「確認しました」で覚える)
SCREEN_STAGES = (STAGE_END, STAGE_AUTO)
#: 帯で知らせる機会(帯の「×」で覚える)
NOTICE_STAGES = (STAGE_WARN, STAGE_AUTO_FAIL)

#: 自動確定の送り(`services/push_history` の行から呼ぶ側が読む)
AUTO_SENT = "sent"
AUTO_BLOCKED = "blocked"
AUTO_FAILED = "failed"


@dataclass(frozen=True)
class ReviewState:
    """確認画面を出すかどうかと、その理由。**画面はこれを写すだけ。**"""

    due: bool = False
    report_date: str = ""
    shift: str = ""
    minutes_left: int = -1
    reason: str = ""
    acknowledged: bool = False
    #: どの機会で出しているか(`STAGE_WARN` / `STAGE_END` / `STAGE_AUTO` …)
    stage: str = STAGE_WARN
    #: **帯で知らせるか**(画面いっぱいにはしない)。文言は `reason`
    notice: bool = False
    #: 帯の強さ(`warn` = 黄 / `error` = 赤)
    level: str = "warn"

    def notice_dict(self) -> dict | None:
        """帯の中身。出さないときは None。**画面はこれを写すだけ。**"""
        if not self.notice:
            return None
        return {"stage": self.stage, "report_date": self.report_date,
                "shift": self.shift, "text": self.reason, "level": self.level,
                "minutes_left": self.minutes_left}

    def as_dict(self) -> dict:
        return {
            "due": self.due, "report_date": self.report_date,
            "shift": self.shift, "minutes_left": self.minutes_left,
            "reason": self.reason, "acknowledged": self.acknowledged,
            "stage": self.stage, "notice": self.notice_dict(),
        }


class ReviewGate:
    """「その直の確認は済んだか」を覚える。

    鍵は (報告日, 直, 機会)。直が変われば別の鍵になるので、自然に忘れます。

    【機会を分けている理由】
    終わりの15分前に「あとで」で閉じたまま、共有へ渡さずに直が終わる
    ことがあります。そのときに**もう一度だけ**出したい ── けれど
    何度も出すと読まずに閉じるようになるので、`STAGE_END` も1回きり
    です(合わせて直に2度まで)。
    """

    def __init__(self) -> None:
        self._done: set[tuple[str, str, str]] = set()
        self._lock = threading.Lock()

    def mark(self, report_date: str, shift: str,
             stage: str = STAGE_WARN) -> None:
        with self._lock:
            self._done.add((report_date, shift, stage))
        log.info("直の確認を済ませました: %s %s (%s)", report_date, shift, stage)

    def acknowledged(self, report_date: str, shift: str,
                     stage: str = STAGE_WARN) -> bool:
        with self._lock:
            return (report_date, shift, stage) in self._done

    def forget(self) -> None:
        with self._lock:
            self._done.clear()


_gate = ReviewGate()


def gate() -> ReviewGate:
    return _gate


def reset() -> None:
    """テスト用。"""
    _gate.forget()


def evaluate(now: datetime, calc: ShiftCalculator, report_date: str,
             shift: str, *, force_day_shift: bool = False,
             warn_minutes: int = 15, recall_mode: bool = False,
             has_data: bool = True, pushed: bool = False) -> ReviewState:
    """直の終わりが近いことを**帯で**知らせるか(`notice`)。**画面いっぱいにはしません。**

        まだ入力終わってなかったらどうするのさ?

    前は15分前に確認画面(画面いっぱい)へ連れて行っていました。打っている最中に
    画面ごと持って行かれると打つ手が止まるので、帯にしました(`due` は立てない)。

    出さない場面が5つあります。どれも**出すほうが害になる**もの:

    1. **閉じた(覚えた)。** 一度読んだものを何度も出すと、次から読まずに閉じる
    2. **呼出モード中。** 過去のデータを直している最中なので、
       「この直の終わり」は当てはまらない
    3. **その直のデータがまだ無い。** 共有へ保存するものが無い
    4. **もう共有へ保存してある**(`pushed` ── 入力画面の「共有へ保存済み」の印)。
       済んだことを催促しない
    5. 直の終わりまで、まだ間がある
    """
    acknowledged = _gate.acknowledged(report_date, shift)
    minutes_left = calc.minutes_until_shift_end(now, force_day_shift)
    base = ReviewState(report_date=report_date, shift=shift,
                       minutes_left=minutes_left, acknowledged=acknowledged)

    if acknowledged or recall_mode or not has_data or pushed:
        return base
    if minutes_left < 0 or minutes_left > warn_minutes:
        return base

    if minutes_left <= 0:
        reason = "直の終わりです。打ち終えたら「共有へ保存」を押してください"
    else:
        reason = (f"あと{minutes_left}分で直の終わりです。"
                  "打ち終えたら「共有へ保存」を押してください")
    return ReviewState(report_date=report_date, shift=shift,
                       minutes_left=minutes_left, reason=reason, notice=True)


def evaluate_auto(report_date: str, shift: str, *, outcome: str,
                  recall_mode: bool = False) -> ReviewState:
    """**残り5分の自動確定**で共有へ送った(送れなかった)直の知らせ。

        5分前の自動確定は、共有へ自動で送るまま(現場の決め)

    ``outcome`` はその直の自動確定の送り(呼ぶ側が共有保存の履歴から読む):

        AUTO_SENT     送れた   → 画面いっぱいの実績(`due`。VBA の直の終わりのグラフと同じ合図)
        AUTO_BLOCKED  直すところがあって送らなかった → 帯(入力画面に留める)
        AUTO_FAILED   共有に届かなかった              → 帯
        ""            自動では送っていない(人が押した・まだ5分前でない)→ 何も出さない

    どれも**直に1度だけ**です(画面は「確認しました」、帯は「×」で覚える)。
    """
    if recall_mode or not outcome:
        return ReviewState(report_date=report_date, shift=shift, stage=STAGE_AUTO)
    if outcome == AUTO_SENT:
        acknowledged = _gate.acknowledged(report_date, shift, STAGE_AUTO)
        base = ReviewState(report_date=report_date, shift=shift,
                           acknowledged=acknowledged, stage=STAGE_AUTO)
        if acknowledged:
            return base
        return ReviewState(
            due=True, report_date=report_date, shift=shift, stage=STAGE_AUTO,
            reason=(f"残り5分の自動確定で、{shift} を共有へ自動で保存しました。"
                    "実績を確かめてください"))
    acknowledged = _gate.acknowledged(report_date, shift, STAGE_AUTO_FAIL)
    base = ReviewState(report_date=report_date, shift=shift,
                       acknowledged=acknowledged, stage=STAGE_AUTO_FAIL)
    if acknowledged:
        return base
    if outcome == AUTO_BLOCKED:
        reason = ("自動では共有へ送れませんでした。直すところがあります"
                  "(入力画面の帯を見て直し、「共有へ保存」を押してください)")
    else:
        reason = ("自動では共有へ送れませんでした。共有に届きません"
                  "(つながったら「共有へ保存」を押してください)")
    return ReviewState(report_date=report_date, shift=shift, stage=STAGE_AUTO_FAIL,
                       reason=reason, notice=True, level="error")


def evaluate_ended(report_date: str, shift: str, *,
                   unsynced: bool, recall_mode: bool = False) -> ReviewState:
    """**終わった直を、もう一度だけ呼び出すか。**

        「あとで」で閉じたまま共有へ渡せずに終了時刻を迎えたとき、
        もう一度出す

    終わりの15分前に出した確認を「あとで」で閉じると、その直はそれきりに
    なります。共有へ渡っていればそれで構いません ── **渡っていないまま
    直が終わったときだけ**、もう一度だけ出します。

    終わったかどうかは時計では測りません。**共有へ未送信のまま、いまの直
    ではなくなった**ものを呼ぶ側が選んで渡します(`pending_sync_headers`)
    ── 時計から数え直すと、日をまたぐ3直や日勤で別の答えが出ます。

    2度目も「あとで」で閉じられます。**3度目はありません** ── 何度も
    出すと、読まずに閉じるようになります。そこから先は入力画面の帯と
    引き継ぎ(`logic/handover.py`)が受け持ちます。
    """
    acknowledged = _gate.acknowledged(report_date, shift, STAGE_END)
    base = ReviewState(report_date=report_date, shift=shift,
                       acknowledged=acknowledged, stage=STAGE_END)
    if acknowledged or recall_mode or not unsynced:
        return base
    return ReviewState(
        due=True, report_date=report_date, shift=shift, stage=STAGE_END,
        # **日も添える。** 終わった直は「いまの直」と同じ名前のことがある
        # (前日の1直を後から作って、今日の1直の最中に戻ってきた など)
        reason=(f"{report_date} {shift} は終わりましたが、まだ共有へ渡していません。"
                f"ここで済ませてください"))
