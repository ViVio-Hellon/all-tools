"""共有へ保存の流れ ── **手で押す「共有へ保存」と自動確定を1本に** (v4.39.2)

    手で押す・自動確定を1本にする
    自動確定も月替わりの書き出しを行う

共有へ送る道は2つあります:

    手で押す「共有へ保存」/「過去分を共有へ送る」  `app/routes/settings.push`
    自動確定(残り5分)・次の直の画面の拾い          `services/shift_close`

前は「送る → 後始末」がそれぞれに書いてあり、食い違っていました:

    - 自動確定は送っている途中で閉じない守り(`running`)が無かった(途中で終われば、ヘッダだけ
      送れて明細が送れていない状態が共有に残り得る)
    - 自動確定は紙(印刷用HTML)を残していなかった(v4.39.0 で手で押す道にだけ入れた)
    - 自動確定は月替わりの書き出しをしていなかった
    - 共有保存の履歴を残す処理が2つ写してあった

いまは**送る(`send`)と後始末(`after_push`)をここの1か所**にし、両方の道がこれを呼びます。
後始末は手で押したときの順で、どれか1つが失敗しても残りは続け、送れたことは変えません:

    1. 集計CSVの2つ目の出力先    `second_output.after_push`
    2. 共有保存の履歴            `push_history.record_push`(呼び手が何を残すか渡す)
    3. 標準作業時間              `standard_time.after_push`
    4. 紙(印刷用HTML)          `paper_files.after_push`
    5. 月替わりの書き出し        `rollover_after_push`(1ページでも送れなかったら見送る)

確かめ(関門)は呼び手が決めます。手で押すときは押した人の権限(管理者なら作業時間の上限を
いちばん長い直で見る)、**自動確定は誰も見ていないので緩めません**(管理者の PC でも同じ)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..config import SETTINGS
from ..logging_setup import get_logger

log = get_logger("services.push_flow")

#: 送っているあいだ終わらせないための名札(`running.running`)。終了の確かめに出る名前
PUSH_ACTION = "共有への保存"
#: 月替わりの書き出しのあいだの名札
ROLLOVER_ACTION = "月替わりの書き出し"


# ----------------------------------------------------------------------
# 送る
# ----------------------------------------------------------------------
def send(repo: Any, keys: Optional[list]) -> Any:
    """未送信を共有へ送る(`pusher.push_pending`)。**送っているあいだは終わらせない。**

    途中で落ちると、ヘッダだけ送れて明細が送れていない状態が**共有側に**残ります。手元の
    SQLite と違って、そこは他のラインも読む場所です(`nippou/running.py`)。例外は呼び手へ。
    """
    from .. import running
    from ..access_bridge import pusher

    with running.running(PUSH_ACTION):
        return pusher.push_pending(repo, SETTINGS.access_db_path, keys=keys)


def keep_history(repo: Any, record: Callable[[], object]) -> None:
    """共有保存の履歴を残し、共有の T_共有保存履歴 へ写す。**何が起きても送った結果は変えない。**

    写せなかった行は手元に `shared=0` で残り、次に誰かが送ったとき(手でも自動でも)に写ります。
    """
    from . import push_history

    try:
        record()
    except Exception:                             # noqa: BLE001 - 記録で保存を止めない
        log.exception("共有保存の履歴を残せませんでした")
        return
    try:
        push_history.share(repo, SETTINGS.access_db_path)
    except Exception:                             # noqa: BLE001 - 次の保存で写す
        log.exception("共有保存の履歴を共有へ写せませんでした")


# ----------------------------------------------------------------------
# 後始末
# ----------------------------------------------------------------------
def guarded_second(write: Callable[[], Any]) -> Any:
    """2つ目の出力先へ出す。**何が起きても共有への保存の結果は返す。**"""
    from . import second_output

    try:
        return write()
    except Exception:                             # noqa: BLE001 - 保存は成功している
        log.exception("集計CSVの2つ目の出力先へ出せませんでした")
        return second_output.Outcome()


def catch_up_months(repo: Any, *, hold_pending: bool = False) -> list:
    """済んでいない月を1つ目・2つ目へ。(1つ目の結果, 2つ目の結果) の並び。**失敗しても空で返す。**"""
    from .. import running
    from . import month_rollover, second_output

    try:
        if not month_rollover.check(repo).due:
            return []
        with running.running(ROLLOVER_ACTION):
            return month_rollover.catch_up(
                repo, SETTINGS.monthly_dir, hold_pending=hold_pending,
                second=lambda result: guarded_second(
                    lambda: second_output.after_rollover(repo, result)))
    except Exception:                             # noqa: BLE001 - 保存は成功している
        log.exception("月替わりの後始末に失敗しました")
        return []


def rollover_after_push(repo: Any, summary: Any) -> list:
    """送り終えたところで、**まだ書き出していない月**を片付ける。

    書き出した月は覚えておき、二度は書き出しません(`services/month_rollover.catch_up`)。
    送れていないページがあるうちは片付けません。**その月に未送信の直が残っていれば書き出さない**
    (`hold_pending`。いまの直だけ送れて前の月の直がまだ止まっている、が起きるため)。
    """
    if getattr(summary, "failed", None):
        return []
    return catch_up_months(repo, hold_pending=True)


def roll_lines(rolls: list) -> tuple[list[str], Any]:
    """月替わりの結果を知らせの行に。2つ目の結果は1つにまとめる。"""
    from . import second_output

    lines: list[str] = []
    monthly = second_output.Outcome()
    for result, extra in rolls:
        lines.append(result.message)
        if extra is not None:
            if extra.text("月別の書き出し"):
                lines.append(extra.text("月別の書き出し"))
            monthly = monthly.merge(extra)
    return lines, monthly


@dataclass
class AfterPush:
    """後始末の結果(手で押した道は JSON に、自動確定は一言にする)。"""

    second: Any = None
    standard: Any = None
    paper: str = ""
    rolls: list = field(default_factory=list)

    def lines(self) -> list[str]:
        """知らせの行(月替わりは別。`roll_lines`)。手で押したときと同じ並び。"""
        out: list[str] = []
        if self.second is not None and self.second.text("送った日の集計CSV"):
            out.append(self.second.text("送った日の集計CSV"))
        if self.standard is not None and getattr(self.standard, "message", ""):
            out.append(self.standard.message)
        if self.paper:
            out.append(self.paper)
        return out

    def roll_lines(self) -> tuple[list[str], Any]:
        return roll_lines(self.rolls)


def after_push(repo: Any, summary: Any, *,
               history: Optional[Callable[[], object]] = None) -> AfterPush:
    """**手で押した・自動、どちらも同じ後始末**(上の 1〜5)。`history` は履歴に残すもの。

    進み具合(`job_progress`)は見ている画面があるときだけ動きます(自動確定では何もしない)。
    """
    from .. import job_progress
    from ..logic import progress as progress_logic
    from . import paper_files, second_output, standard_time

    succeeded = list(getattr(summary, "succeeded", []))
    done = AfterPush()
    job_progress.step(phase=progress_logic.PHASE_AFTER, total=4, done=0,
                      label="集計CSV(2つ目の出力先)")
    done.second = guarded_second(lambda: second_output.after_push(repo, succeeded))
    job_progress.step(done=1, label="共有保存の履歴")
    if history is not None:
        keep_history(repo, history)
    job_progress.step(done=2, label="標準作業時間")
    try:
        done.standard = standard_time.after_push(repo, succeeded, SETTINGS.standard_time_db_path)
    except Exception as exc:                      # noqa: BLE001 - 次の保存で写す
        log.exception("標準作業時間へ写せませんでした")
        done.standard = standard_time.Outcome(error=str(exc))
    job_progress.step(done=3, label="印刷用HTML")
    try:
        done.paper = paper_files.after_push(repo, summary)
    except Exception:                             # noqa: BLE001 - 送れてはいる
        log.exception("送った直の印刷用HTMLを残せませんでした")
    job_progress.step(done=4)
    # **保存が済んでから月替わりを見る。** VBA も印刷保存の最後に置いていました
    job_progress.step(phase=progress_logic.PHASE_MONTH)
    try:
        done.rolls = rollover_after_push(repo, summary)
    except Exception:                             # noqa: BLE001 - 送れてはいる
        log.exception("月替わりの後始末に失敗しました")
    return done
