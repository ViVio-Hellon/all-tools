"""直の終わり間際に、その直を締める (VBA ``ShouldExecuteAutoPrint`` → ``ExecuteAutoPrint``)

【VBAは何をしていたか】
`Application.OnTime` の1分タイマーが `CheckPrintReminder` を回し、

    残り15分  印刷忘れの警告(ラベル + 音 + 5分おきにポップアップ)
    残り 5分  **自動で印刷処理を走らせる**(`Print保存` + `Aggre保存` + 印刷)

どちらも「その直をもう印刷したか」を見て、していれば黙りました。その印は
レジストリの `yyyymmdd_<直名>` に入れた印刷時刻でした ── ここでは
`print_status` の表が同じ役目をします。

【紙は出しません ── 追いかける相手を「印刷」から「確定」に変えました】
VBA は実際にプリンタへ流していました。こちらは**紙を作業者が欲しいとき
だけ出すもの**にしたので(「印刷」ボタンは別にあります)、直の終わりまでに
済ませたいのは紙ではなく

    保存済みの明細を読み直して → チェックを走らせ → 集計を作り直す

のほうです。これを「締める」と呼びます。締めた印が立つと、催促も自動の
締めも止まります。

**刷っても印は立ちません。** 紙が任意になった以上、途中で1枚刷っただけで
催促が止まると、そのあとに打った行が確かめられないまま直が終わります。

【チェックは止めません】
保存前チェックは走らせますが、見つかっても**続けます** ── VBA の
自動印刷(`isAutoMode = True`)も「警告だけで続行」でした。誰も見ていない
ところで止めると、その直ぶんが丸ごと残らないことになります。結果は
`findings` に入れて返すので、画面と記録には出ます。

【打ちかけの値について】
サーバからは画面の入力欄を見られないので、締めるのは**手元DBに入って
いるぶん**です。日報は入力のたびに自動保存されているので差は小さい
ですが、最後の数十秒ぶんが載らないことはありえます ── そこは人が
「保存(確定)」を押したときに入ります。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from ..config import SETTINGS
from ..db.repository import NippouRepository
from ..logging_setup import get_logger
from ..logic.shift import ShiftCalculator, ShiftCloseChecker, parse_business_date

log = get_logger("services.shift_close")


class _Gate:
    """同じ直で二重に走らせない見張り。

    VBA の `AutoPrintExecuted` に当たります。あちらはフォーム1つの
    中の変数でしたが、こちらは**タブが何枚も開けます** ── 2枚が同じ
    分に叩くと、締めが2回走りえます。錠を持った見張りで1回に絞ります。

    **覚えは補助です。** 本当の印は `print_status` の表
    (`mark_shift_closed`)で、アプリを立ち上げ直しても残ります。
    こちらはその1分間の競争を防ぐためだけのもの。
    """

    def __init__(self) -> None:
        self._done: set[tuple[str, str, str]] = set()
        self._lock = threading.Lock()

    def take(self, key: tuple[str, str, str]) -> bool:
        with self._lock:
            if key in self._done:
                return False
            self._done.add(key)
            return True

    def release(self, key: tuple[str, str, str]) -> None:
        """**締められなかったぶんを、覚えたままにしない。**

        印(`print_status`)が書けなかったのに覚えだけ残ると、DBは
        「まだ締まっていない」、こちらは「締めた」と言い続けます ──
        催促は鳴りやまないのに、もう一度締めようとすると「確定済みです」で
        断られる。**どちらも直せない状態**になります。

        書けなかったときはここで手放して、次の1分にもう一度試します。
        """
        with self._lock:
            self._done.discard(key)

    def forget(self) -> None:
        with self._lock:
            self._done.clear()


_gate = _Gate()


def reset() -> None:
    """テスト用。"""
    _gate.forget()


@dataclass
class ShiftCloseResult:
    """締めたかどうかと、その理由。**画面はこれを写すだけ。**"""

    ran: bool = False
    reason: str = ""
    report_date: str = ""
    line: str = ""
    shift: str = ""
    minutes_left: int = -1
    #: 締めた対象のページ数
    pages: int = 0
    #: 保存前チェックで見つかったこと。**止めずに続けた**ぶん
    findings: list[str] = field(default_factory=list)
    #: 共有へ送れたページ数。送らなかったときは 0
    pushed: int = 0
    #: 送らなかった理由。送ったときは空
    push_note: str = ""
    #: 締めた印(`print_status`)を残せたか。**残せなければ確定ではありません**
    marked: bool = True

    @property
    def message(self) -> str:
        if not self.ran:
            return self.reason
        # **負の分を出さない。** 日勤のラインは時刻に関わらず「日勤」なので、
        # 終了時刻(17:00)を過ぎると残りが負になります ── 「残り-60分で
        # 確定しました」は、読んだ人が意味を取れません。`sweep` が
        # あとから締めるぶん(残り0)も、言いたいことは同じです
        when = (f"残り{self.minutes_left}分で" if self.minutes_left > 0
                else "終了時刻を過ぎたので")
        head = (f"{self.shift} の{when}、打ってあるぶんを"
                f"確定しました({self.pages}ページ)")
        if not self.marked:
            # **黙って「確定しました」で終わらせない。** 印が無いままだと
            # 催促が鳴りやみません
            head += "。ただし締めた印を残せませんでした(次にもう一度試します)"
        if self.findings:
            head += f"。直すところが{len(self.findings)}件あります"
        if self.pushed:
            head += f"。共有へも送りました({self.pushed}件)"
        elif self.push_note:
            head += f"。{self.push_note}"
        return head + "。紙が要るときは「印刷」から出してください"

    def as_dict(self) -> dict:
        return {"ran": self.ran, "reason": self.reason,
                "report_date": self.report_date, "line": self.line,
                "shift": self.shift, "minutes_left": self.minutes_left,
                "pages": self.pages, "findings": self.findings,
                "pushed": self.pushed, "push_note": self.push_note,
                "marked": self.marked, "message": self.message}


def _has_data(repo: NippouRepository, report_date: str, line: str,
              shift: str) -> bool:
    """その直に打ってあるものがあるか (VBA ``HasDataInPrintRange``)。

    **無ければ何もしません。** 打っていない直を締めても、空の集計が
    できるだけです(VBA も同じところで打ち切っていました)。
    """
    return bool(repo.saved_pages(report_date, line, shift))


def run(repo: NippouRepository, calc: ShiftCalculator, *,
        line: str, now: Optional[datetime] = None,
        force_day_shift: bool = False, recall_mode: bool = False,
        already_ran: bool = False) -> ShiftCloseResult:
    """残り5分なら、打ってあるぶんを確定する。**走らせるかの判断ごと。**

    断る場面は VBA と同じ5つです ── 呼出モード中 / もう走った /
    その直は締め済み / まだ5分より前 / 打ったものが無い。
    """
    from .nippou_service import format_business_date

    now = now or datetime.now()
    shift = calc.time_check(now, force_day_shift)
    business_date = calc.today_check(now, force_day_shift)
    report_date = format_business_date(business_date)
    minutes_left = calc.minutes_until_shift_end(now, force_day_shift)

    checker = ShiftCloseChecker(
        calc,
        is_shift_closed=lambda s, d: repo.is_shift_closed(s, d, line),
        warn_minutes=SETTINGS.print_warning_minutes,
        auto_close_minutes=SETTINGS.auto_print_minutes)

    base = ShiftCloseResult(report_date=report_date, line=line, shift=shift,
                            minutes_left=minutes_left)

    # **断る理由を1つずつ言う。** 「走らなかった」だけだと追えません
    if recall_mode:
        base.reason = "過去データを開いているので見送ります"
        return base
    if already_ran:
        base.reason = f"{shift} は確定済みです"
        return base
    if repo.is_shift_closed(shift, business_date, line):
        base.reason = f"{shift} は確定済みです"
        return base
    if minutes_left > SETTINGS.auto_print_minutes:
        base.reason = f"{shift} の終了まで{minutes_left}分(まだ早い)"
        return base
    if not _has_data(repo, report_date, line, shift):
        base.reason = f"{shift} には打ってあるものがありません"
        return base
    if not checker.should_auto_close(
            now, force_day_shift, recall_mode=recall_mode,
            already_closed=already_ran, has_pending_data=True):
        # ここまでで弾けているはずだが、判断は1か所(`logic/shift.py`)に
        # 従う ── 条件が増えたときに、こちらだけ古くならないように
        base.reason = f"{shift} は自動確定の条件を満たしません"
        return base

    # **タブが2枚あっても1回だけ。** 印は `print_status` に入るが、
    # 同じ分に2枚が叩くと、入る前にすれ違う
    if not _gate.take((report_date, line, shift)):
        base.reason = f"{shift} は確定済みです"
        return base

    return _close(repo, base)


def sweep(repo: NippouRepository, calc: ShiftCalculator, *, line: str,
          now: Optional[datetime] = None, force_day_shift: bool = False,
          recall_mode: bool = False) -> list[ShiftCloseResult]:
    """**終わったのに締まっていない直**を、あとから締める。

    【なぜ要るのか ── 残り5分の自動確定には2つ穴があります】
    1つめは「画面が開いていなければ動かない」で、これは直しようが
    ありません(タブを閉じるとアプリごと終わります ── `idle_exit`)。

    2つめがここです。`run` が見るのは**いまの直だけ**なので、
    17:05 に開いたときには、もう「2直の終了まで118分(まだ早い)」しか
    言いません ── **17:00 に終わった1直は、二度と自動確定されません。**
    押し忘れたまま直の時間を過ぎると、そこで止まります。

    そこで**次に誰かが開いたときに拾います。** 拾う相手は共有へ未送信の
    直だけです ── 送り終えた直をいまさら締め直しても、変わるものが
    ありません。翌朝いちばんに開いた人の画面で、前の晩のぶんが片付きます。

    **打ちかけの直は触りません**(いまの直は `run` の担当)。
    """
    from .nippou_service import format_business_date

    if recall_mode:
        return []
    now = now or datetime.now()
    current = (format_business_date(calc.today_check(now, force_day_shift)),
               line, calc.time_check(now, force_day_shift))

    done: list[ShiftCloseResult] = []
    for key in _unclosed_keys(repo, line=line, current=current):
        if not _gate.take(key):
            continue
        result = ShiftCloseResult(report_date=key[0], line=key[1],
                                  shift=key[2], minutes_left=0)
        done.append(_close(repo, result, push=False))
    if done:
        # 送るのは最後に1度だけ。**直の数だけ共有を開かない**
        _push_if_clean(repo, done[-1])
        log.info("終わった直を%d件あとから締めました: %s", len(done),
                 "、".join(f"{r.report_date} {r.shift}" for r in done))
    return done


def _unclosed_keys(repo: NippouRepository, *, line: str,
                   current: tuple[str, str, str]) -> list[tuple[str, str, str]]:
    """共有へ未送信で、まだ締まっていない直。**いまの直は外す。**"""
    from ..logic.shift import parse_business_date

    found: list[tuple[str, str, str]] = []
    for header in repo.pending_sync_headers():
        key = (header.report_date, header.line, header.shift)
        if key == current or key in found or header.line != line:
            continue
        parsed = parse_business_date(header.report_date)
        if parsed is None:
            continue
        if repo.is_shift_closed(header.shift, parsed, header.line):
            continue
        found.append(key)
    return found


def _close(repo: NippouRepository, result: ShiftCloseResult, *,
           push: bool = True) -> ShiftCloseResult:
    """チェックを走らせ、集計を作り直し、締めた印を立てる。"""
    from . import shift_check, summary

    key = (result.report_date, result.line, result.shift)

    # 1. 保存前チェック。**見つかっても止めません**(VBA の自動モードと同じ)
    try:
        report = shift_check.run(repo, *key)
        result.findings = [f.message for f in report.findings]
    except Exception:                             # noqa: BLE001 - 確定は続ける
        log.exception("自動確定のチェックでエラー key=%s", key)

    # 2. 集計を作り直す。保存のたびに作っているので、ふつうは同じ値。
    #    **表を直に書き換えられていたとき**にここで追いつく
    try:
        summary.refresh_shift(repo, *key)
    except Exception:                             # noqa: BLE001 - 印は立てる
        log.exception("自動確定の集計でエラー key=%s", key)

    result.pages = len(repo.saved_pages(*key))

    # 3. 締めた印。**ここで催促が止まります**
    #
    # 書けなかったら**見張りの覚えも手放します**(`_gate.release`)。
    # 覚えだけ残ると、DBは「まだ締まっていない」・こちらは「締めた」と
    # 言い続けることになり、催促は鳴りやまないのに、もう一度締めようと
    # すると「確定済みです」で断られます ── どちらからも直せません。
    parsed = parse_business_date(result.report_date)
    if parsed is None:
        result.marked = False
        log.warning("報告日を読めないので締めた印を残せません key=%s", key)
    else:
        try:
            repo.mark_shift_closed(result.shift, parsed, result.line)
        except Exception:                         # noqa: BLE001
            result.marked = False
            log.exception("自動確定の印でエラー key=%s", key)
    if not result.marked:
        _gate.release(key)

    result.ran = True

    # 4. **綺麗なら共有へも送る。**
    #
    # 締めても共有へは出ていませんでした。押し忘れたまま直の時間が過ぎ、
    # 次の直がそのまま進む ── その流れの一番手前がここです。断りが
    # 1件も無いのなら、押すか押さないかを人に委ねる理由がありません。
    #
    # **断りがあるものは今までどおり送りません。** そちらは次の直の
    # 引き継ぎ(`logic/handover.py`)が受け止めます ── つまり引き継ぎに
    # 出てくるのは「本当に直すところがある直」だけになります。
    #
    # `push=False` は `sweep` から。何直ぶんも締めるときに直の数だけ
    # 共有を開かないよう、送るのは最後に1度だけにします
    if push:
        _push_if_clean(repo, result)

    log.info("自動確定しました key=%s 残り%d分 ページ=%d 指摘=%d 送信=%d",
             key, result.minutes_left, result.pages, len(result.findings),
             result.pushed)
    return result


def _push_if_clean(repo: NippouRepository, result: ShiftCloseResult) -> None:
    """共有へ未送信のものが**ぜんぶ綺麗なら**送る。

    見るのは「送ろうとしているぶん全部」です ── 手で押す
    「共有へ保存」(`/api/settings/push`)と同じ範囲・同じ関門
    (`shift_check.run_pending`)。片方だけ緩いと、機械は送れたのに人が
    押すと断られる(またはその逆)が起きます。

    **送れなくても締めた印は消しません。** 送るのは締めのあとの話で、
    共有が落ちている日に直の確定までできなくなるほうが重い。
    """
    from ..access_bridge import pusher
    from ..config import SETTINGS
    from . import shift_check

    try:
        reports = shift_check.run_pending(repo)
    except Exception:                             # noqa: BLE001 - 確定は済んでいる
        log.exception("自動送信の前のチェックでエラー")
        result.push_note = "共有へは送っていません(確かめられませんでした)"
        return

    blocked = shift_check.blocked_reports(reports)
    if blocked:
        names = "、".join(r.key_text for r in blocked)
        result.push_note = (f"直すところがあるので共有へは送っていません"
                            f"({names})")
        log.info("自動送信を見送りました: %s", names)
        return
    if not repo.pending_sync_headers():
        result.push_note = "共有へ未送信のものはありません"
        return

    try:
        summary_ = pusher.push_pending(repo, SETTINGS.access_db_path)
    except Exception as exc:                      # noqa: BLE001 - 締めは済んでいる
        log.exception("自動送信に失敗しました")
        result.push_note = f"共有へ送れませんでした({exc})"
        return

    result.pushed = len(summary_.succeeded)
    if summary_.failed:
        result.push_note = f"共有へ送れなかったものが{len(summary_.failed)}件あります"
