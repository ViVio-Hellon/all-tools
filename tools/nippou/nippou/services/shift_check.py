"""保存の前に、**保存されている中身を**確かめる

【VBA から引き継ぐ思想】
VBA はフォームで入力チェックを通してからシートへ書き、そのうえで
印刷保存のときに `ExecutePrintProcess` でもう一度まとめて見ていました。
二度手間に見えますが、そうではありません ── **シートは直接書き換え
られる**からです。フォームを通らずに入った値は、フォームのチェックを
一度も通っていません。

このツールにシートはありませんが、同じ穴があります:

    ・「表を見る/直す」画面から `nippou_local.sqlite3` を直に直せる
    ・他の道具(DB Browser 等)でも開けば直せる
    ・古い版で保存された行が、そのまま残っていることもある

だから**画面が持っている入力ではなく、DBに入っている全ページを読み直して**
チェックします。画面を経由しないので、どの道で入った値でも同じ関門を
通ります。

【いつ止めるか】
    保存(確定)        … 止める。ただし**5項目だけ**(:func:`run_for_save`)
    共有への保存      … 止める。**7項目ぜんぶ**(直に1回の関門)
    直の確定(自動)    … 7項目ぜんぶ
    日報入力の「この直をチェック」… 止めない(いま何件あるかを見せるだけ)

**保存(確定)で7項目ぜんぶを見てはいけません。** 最終時間と休憩は
「直が終わったか」の話なので、打っている途中は必ず足りません ──
8時に1行目を打った時点で断られ、誰も1行も保存できなくなります。
どれがどちらかは `logic/save_checks.AT_SHIFT_END` が持ちます。

VBA の自動印刷(`isAutoMode=True`)が「警告だけで続行」だったのと同じで、
**人が押していない保存は止めません** ── 誰も見ていないところで止まると、
共有側が古いまま朝を迎えます。

【逃げ道】
VBA は `UF印刷` に「設備移動のため印刷」と打たせて `Last_Confi` の2本
だけを飛ばしていました。ここも同じ範囲(最終時間・休憩)だけ、
`skip` で通せます ── 梱包数や停止記号の間違いは飛ばせません。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional, Sequence

from ..db.models import DetailRecord
from ..db.repository import NippouRepository
from ..logging_setup import get_logger
from ..logic import save_checks
from ..logic.save_checks import Finding
from ..logic.shift import bounds_text
from ..logic.work_time import shift_limit
from ..logic import line_names

log = get_logger("services.shift_check")

#: 「設備移動のため保存」。VBA の「設備移動のため印刷」と同じ役割で、
#: 行き先が印刷ではなく保存になったぶんだけ言い換えてある
SKIP_PHRASE = "設備移動のため保存"

#: 画面のページを、まだ保存していない中身で見たときに添える一言(v4.18.0)
UNSAVED_NOTE = ("── 画面のページは、まだ保存していない中身(打ったとおり)で確かめました。"
                "「保存(確定)」を押すまで、共有へは出ません")


@dataclass(frozen=True)
class ScreenPage:
    """画面に出ている1ページ(まだ保存していないかもしれない中身)。

        入れてるんだけどずっと出てますね(休憩)

    確かめが**保存済みの中身だけ**を読んでいたので、打った休憩が届いていません
    でした。過去データ(他の直)を開いているあいだは自動保存もしないので、
    「保存(確定)」を押すまで、いつまでも 0分 のままです。画面のページは
    **画面の中身で**見ます(v4.18.0)。
    """

    page: int
    header: object                 # HeaderRecord
    details: Sequence[DetailRecord]


@dataclass(frozen=True)
class CheckReport:
    """1つの直(全ページ)を見た結果。**画面はこれを写すだけ。**"""

    report_date: str
    line: str
    shift: str
    pages: int = 0
    rows: int = 0
    findings: list[Finding] = field(default_factory=list)
    skipped: list[Finding] = field(default_factory=list)
    #: 画面のページを**まだ保存していない中身で**見たか(:func:`run` の `screen`)。
    #: 結果は打ったとおりでも、共有へ出るのは「保存(確定)」を押してから(v4.18.0)
    unsaved: bool = False

    @property
    def ok(self) -> bool:
        """止める理由が無い。`skip` で外したぶんは数えない。"""
        return not self.findings

    @property
    def key_text(self) -> str:
        return f"{self.report_date} {line_names.label(self.line)} {self.shift}"

    @property
    def message(self) -> str:
        if self.ok and not self.skipped:
            head = f"{self.key_text}: 直すところはありません"
        elif self.ok:
            head = (f"{self.key_text}: {len(self.skipped)}件を"
                    f"「{SKIP_PHRASE}」で通しました")
        else:
            head = f"{self.key_text}: {save_checks.summary(self.findings)}"
        return f"{head}\n{UNSAVED_NOTE}" if self.unsaved else head

    @property
    def sound(self) -> Optional[str]:
        return save_checks.first_sound(self.findings)

    def as_dict(self) -> dict:
        return {
            "report_date": self.report_date, "line": self.line,
            "shift": self.shift, "pages": self.pages, "rows": self.rows,
            "ok": self.ok, "message": self.message, "sound": self.sound,
            "unsaved": self.unsaved,
            "findings": [f.as_dict() for f in self.findings],
            "skipped": [f.as_dict() for f in self.skipped],
        }


def bounds_of(shift_times: dict[str, tuple[str, str]], shift: str
              ) -> tuple[str, str]:
    """その直の始まりと終わり("HH:MM")。

    出どころは伝送用ファイルの `時間用`(取り込み済みなら `shift_config`)。
    **取り込む前でも動かす**ため、無い直は `ShiftTimes` の既定に落とします
    ── 時間マスタが無いことを理由に保存を止めない。読み方は
    `logic/shift.times_from_master` の 1 か所(画面の直の計算機と同じ)。
    """
    return bounds_text(shift_times, shift)


def known_stop_codes() -> set[str]:
    """内訳マスタにある記号ぜんぶ。

    **読めなければ空**を返します。空のときチェック側は何も言いません
    (`save_checks.check_stop_codes`) ── マスタに届かない日に、
    入っている記号を全部「無い記号」と言うわけにはいきません。
    """
    from ..access_bridge import stop_master

    try:
        by_category = stop_master.load_all_categories()
    except Exception:                             # noqa: BLE001 - 保存を止めない
        log.warning("停止内訳マスタを読めませんでした。記号の確認は飛ばします")
        return set()
    return {r.code for items in by_category.values() for r in items if r.code}


def _details(repo: NippouRepository, report_date: str, line: str, shift: str,
             *, skip_page: int = 0) -> tuple[list[DetailRecord], int, bool]:
    """その直の**全ページ**を DB から読み直す。戻りは (明細, ページ数, 昼稼働)。

    `skip_page` は読まない(画面の中身で差し替える)ページ。
    """
    details: list[DetailRecord] = []
    pages = 0
    day_work = False
    for page in repo.saved_pages(report_date, line, shift):
        if skip_page and page == skip_page:
            pages += 1
            continue
        loaded = repo.load(report_date, line, shift, page)
        if loaded is None:
            continue
        header, rows = loaded
        pages += 1
        day_work = day_work or header.day_shift == "有"
        details.extend(rows)
    return details, pages, day_work


def run(repo: NippouRepository, report_date: str, line: str, shift: str, *,
        skip: bool = False, admin: bool = False,
        codes: Optional[Sequence[str]] = None,
        screen: Optional[ScreenPage] = None) -> CheckReport:
    """1つの直を確かめる。**DBを読み直す。**

    `screen` を渡すと、そのページだけは**画面の中身**で見ます(日報入力の
    「この直をチェック」と帯 ── v4.18.0)。共有へ保存・直の確定は渡しません
    (出ていくのは保存済みの中身なので)。

    **画面のページが空なら渡さなかったのと同じ**(保存済みで見る)。中身の無い
    画面で、保存済みのページを消したことにしない(空の状態で描く道もあるため)。
    """
    if screen is not None and not _has_content(screen.details):
        screen = None
    details, pages, day_work = _details(repo, report_date, line, shift,
                                        skip_page=screen.page if screen else 0)
    unsaved = False
    if screen is not None:
        details = list(details) + list(screen.details)
        day_work = day_work or getattr(screen.header, "day_shift", "") == "有"
        unsaved = _differs(repo, report_date, line, shift, screen)
        pages = max(pages, 1)
    if not details:
        return CheckReport(report_date, line, shift)

    shift_times = repo.get_shift_times()
    start, end = bounds_of(shift_times, shift)
    found = save_checks.run_all(
        details, shift=shift, shift_start=start, shift_end=end,
        limit_minutes=shift_limit(shift_times, shift, admin=admin),
        known_stop_codes=known_stop_codes() if codes is None else codes,
        day_work=day_work)

    blocking = save_checks.blocking(found, skip=skip)
    skipped = [f for f in found if skip and f.skippable]
    return CheckReport(report_date, line, shift, pages=pages,
                       rows=len(details), findings=blocking, skipped=skipped,
                       unsaved=unsaved)


def _has_content(details: Sequence[DetailRecord]) -> bool:
    from ..logic import fingerprint

    return any(any(fingerprint.text(getattr(d, f, "")) for f in fingerprint.DETAIL_FIELDS)
               for d in details)


def _differs(repo: NippouRepository, report_date: str, line: str, shift: str,
             screen: ScreenPage) -> bool:
    """画面のページが、保存済みのそのページと**共有へ出る中身で**違うか。"""
    from ..logic import fingerprint

    loaded = repo.load(report_date, line, shift, screen.page)
    if loaded is None:
        return _has_content(screen.details)
    header, rows = loaded
    # 係数処理ﾛｯﾄ数(keisu)は**保存のときサーバが入れる**もので、画面は持ちません
    # (機側・NS1)。比べるときは保存済みの値を写してから(写さないと、保存した
    # 直後でも「まだ保存していない」と言う)
    saved_keisu = {d.row_no: d.keisu for d in rows}
    shown = [replace(d, keisu=saved_keisu.get(d.row_no, d.keisu)) for d in screen.details]
    return (fingerprint.page_fingerprint(header, rows)
            != fingerprint.page_fingerprint(screen.header, shown))


def run_for_save(repo: NippouRepository, report_date: str, line: str,
                 shift: str, page: int, details: Sequence[DetailRecord],
                 *, day_work: bool = False, admin: bool = False,
                 codes: Optional[Sequence[str]] = None) -> CheckReport:
    """**これから保存する1ページを差し込んで**、その直ぜんぶを確かめる。

    【なぜ保存の前に、保存する中身を混ぜるのか】
    `run` は DB を読み直します。保存(確定)の関門では、**まだ DB に
    入っていないページ**を見なければ意味がありません ── 入れてから
    確かめたのでは、間違った中身が一度は入ってしまいます。

    そこで「いま押したページ」だけ画面から来たもので置き換え、残りの
    ページは DB から読みます。**梱包数も作業時間の合計もページを
    またいで足す**ので、1ページだけ見ても答えは出ません。

    返すのは :func:`save_checks.during_input` を通したものだけです ──
    最終時間と休憩は直の終わりの話で、ここでは見ません。
    """
    others: list[DetailRecord] = []
    for saved in repo.saved_pages(report_date, line, shift):
        if saved == page:
            continue                       # 差し替える側なので読まない
        loaded = repo.load(report_date, line, shift, saved)
        if loaded is None:
            continue
        header, rows = loaded
        day_work = day_work or header.day_shift == "有"
        others.extend(rows)

    merged = list(others) + list(details)
    if not merged:
        return CheckReport(report_date, line, shift)

    shift_times = repo.get_shift_times()
    start, end = bounds_of(shift_times, shift)
    found = save_checks.run_all(
        merged, shift=shift, shift_start=start, shift_end=end,
        limit_minutes=shift_limit(shift_times, shift, admin=admin),
        known_stop_codes=known_stop_codes() if codes is None else codes,
        day_work=day_work)
    return CheckReport(report_date, line, shift, pages=1, rows=len(merged),
                       findings=save_checks.during_input(found))


def run_pending(repo: NippouRepository, *, skip: bool = False,
                admin: bool = False) -> list[CheckReport]:
    """これから共有へ送る直を、ぜんぶ確かめる。

    送るのは「まだ送っていないページ」(`pending_sync_headers`)で、日も直も
    混ざりえます。**ページではなく直の単位**で見ます ── 梱包数も休憩も
    作業時間も、ページをまたいで足してからでないと判断できません。
    """
    keys: list[tuple[str, str, str]] = []
    for header in repo.pending_sync_headers():
        key = (header.report_date, header.line, header.shift)
        if key not in keys:
            keys.append(key)
    # 同じマスタを直の数だけ読み直さない(共有に何度も取りに行くことになる)
    codes = list(known_stop_codes())
    # **進み具合を置く**(共有へ保存の棒。走っていなければ何もしない)
    from .. import job_progress
    from ..logic import progress

    job_progress.step(phase=progress.PHASE_CHECK, total=len(keys), done=0)
    reports = []
    for no, key in enumerate(keys, start=1):
        job_progress.step(label=" ".join(key))
        reports.append(run(repo, *key, skip=skip, admin=admin, codes=codes))
        job_progress.step(done=no)
    return reports


def normalize_skip(value: object) -> bool:
    """「設備移動のため保存」と**打ってあるか**。

    真偽値では通しません。VBA も `UF印刷` にこの一文を打たせていました
    ── ボタン1つで通せると押し流されますが、打つとなると「なぜ通すのか」
    を一度は考えます。それがこの仕掛けの目的です。
    """
    if not isinstance(value, str):
        return False
    return save_checks.normalize(value) == save_checks.normalize(SKIP_PHRASE)


def blocked_reports(reports: Sequence[CheckReport]) -> list[CheckReport]:
    return [r for r in reports if not r.ok]


def combined_message(reports: Sequence[CheckReport]) -> str:
    """断りを1つの文にまとめる。**どの直の、どこか**まで出す。"""
    blocked = blocked_reports(reports)
    if not blocked:
        return ""
    lines = [f"保存できません。直すところが"
             f" {sum(len(r.findings) for r in blocked)}件あります"]
    for report in blocked:
        lines.append(f"■ {report.key_text}")
        for finding in report.findings:
            where = f"{finding.where} " if finding.where else ""
            lines.append(f"  ・{where}{finding.message}")
    lines.append(f"※ 設備移動などで途中までのときは「{SKIP_PHRASE}」で"
                 "最終時間と休憩のぶんだけ通せます")
    return "\n".join(lines)
