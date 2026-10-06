"""Recall-mode state machine tying the repository and shift calculator
together for the admin "view/edit a past shift" workflow.

Ports ``NippouDB_Recall`` / ``NippouDB_EditPage`` / ``NippouDB_BackToCurrent``
/ ``NippouDB_CheckShiftGuard`` / ``NippouDB_ClearRecallMode`` /
``NippouDB_AutoSave`` (standard module ~line 13757-14120). Kept
independent of any presentation layer: ``app/routes/settings.py`` calls
these methods from its handlers, and the "他直データ操作" (other-shift
edit) confirmation prompt is injected rather than hard-coded, matching
how the underlying VBA delegated to ``MsgBox``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable, Optional

from ..db.models import DetailRecord, HeaderRecord
from ..db.repository import NippouRepository
from ..logic.shift import ShiftCalculator
from ..logic import line_names


def format_business_date(d: date) -> str:
    """Matches VBA's ``Format$(Date, "yyyy""年""m""月""d""日"")`` -- note
    month/day are *not* zero-padded."""
    return f"{d.year}年{d.month}月{d.day}日"


@dataclass
class RecallState:
    active: bool = False
    report_date: str = ""
    line: str = ""
    shift: str = ""
    page: int = 1
    #: 開いた**その時に**、これは当直だったか。
    #:
    #: 同じ直の中のページ移動(`edit_page`)と、わざわざ過去を開いたの
    #: (`recall_data`)を見分けるために持ちます。ページ移動のまま 17:00 を
    #: またぐと、**さっきまで自分の直だった紙が「他の直」になり**、
    #: そのままでは保存を断られます(403)。そこは断るところではなく
    #: 「直が変わりました」と知らせるところなので、ここで見分けます。
    was_current: bool = False


@dataclass
class ServiceState:
    """**要求をまたいで残る状態。**

    Web版のサービスは要求ごとに作り直されるので、呼出モードと最後の
    自動保存時刻をサービス自身が持つと、毎回まっさらになります ──
    間引きが効かず、呼出中かどうかも忘れます。

    プロセスに1つの入れもの(:class:`nippou.work_context.WorkContext`)が
    これを持ち、サービスは**参照を借りるだけ**にします。
    """

    recall: RecallState = field(default_factory=RecallState)
    last_autosave: Optional[datetime] = None


class NippouService:
    def __init__(
        self,
        repo: NippouRepository,
        shift_calc: ShiftCalculator,
        is_admin: Callable[[], bool],
        confirm_other_shift_edit: Callable[[str, str, str, str, str], bool],
        state: Optional[ServiceState] = None,
    ) -> None:
        self.repo = repo
        self.shift_calc = shift_calc
        self.is_admin = is_admin
        self.confirm_other_shift_edit = confirm_other_shift_edit
        # **借りものにする。** 渡されなければ自前で持つ(テスト・単体利用)
        self.state = state if state is not None else ServiceState()

    # 呼出モードと最後の自動保存は `state` が持つ。ここは読み書きの窓口で、
    # 借りた入れものへそのまま通す ── 2か所に持つと必ず食い違う
    @property
    def recall(self) -> RecallState:
        return self.state.recall

    @recall.setter
    def recall(self, value: RecallState) -> None:
        self.state.recall = value

    @property
    def _last_autosave(self) -> Optional[datetime]:
        return self.state.last_autosave

    @_last_autosave.setter
    def _last_autosave(self, value: Optional[datetime]) -> None:
        self.state.last_autosave = value

    # ------------------------------------------------------------------
    def current_shift_info(self, now: datetime, force_day_shift: bool) -> tuple[str, str]:
        """Port of ``NippouDB_GetShiftInfo``: returns (shift, business_date_str)."""
        shift = self.shift_calc.time_check(now, force_day_shift)
        business_date = self.shift_calc.today_check(now, force_day_shift)
        return shift, format_business_date(business_date)

    def check_shift_guard(
        self, target_date: str, target_shift: str, current_date: str, current_shift: str, action_name: str
    ) -> bool:
        """Port of ``NippouDB_CheckShiftGuard``."""
        if target_date == current_date and target_shift == current_shift:
            return True
        if not self.is_admin():
            return False
        return self.confirm_other_shift_edit(target_date, target_shift, current_date, current_shift, action_name)

    # ------------------------------------------------------------------
    def is_current_key(self, report_date: str, line: str, shift: str,
                       current_line: str, current_date: str,
                       current_shift: str) -> bool:
        """それは**いま自分が入力している直**か。

        「自分の直か、他人の記録か」の分かれ目です。関門(`recall_refusal`)
        も、画面の言葉も、ここ1つの比べ方を使います ── 別々に比べると、
        許すほうと見せ方がずれます。
        """
        return (report_date == current_date and shift == current_shift
                and line == current_line)

    def recall_refusal(self, report_date: str, line: str, shift: str,
                       current_line: str, current_date: str,
                       current_shift: str) -> str:
        """その呼び出しを断るなら理由、通してよければ空文字。

        **判断はここ1か所。** 画面も道(route)も自分では決めません ──
        「どこまでなら誰でも触ってよいか」が2か所にあると、必ず片方
        だけが緩くなります。

        分け目は**自分の直か、他人の記録か**:

            いまの直・いまのライン … 誰でも。ページを戻るのと同じこと
                                     (`edit_page` と同じ範囲)
            それ以外               … 管理者モード

        呼び出しは入力画面に開き直して**直せる**ので、他の直まで誰でも
        開けると、誰が入れた記録なのかが分からなくなります。
        """
        if self.is_current_key(report_date, line, shift, current_line,
                               current_date, current_shift):
            return ""
        if not self.is_admin():
            # **どこで切り替えるかまで書く。** 断られた人が次にする
            # ことが書いていないと、そこで止まります
            return ("他の日・他の直・他のラインを開くには管理者モードが"
                    "要ります(設定・管理者の「端末」で切り替えます)")
        return ""

    def recall_data(
        self,
        report_date: str,
        line: str,
        shift: str,
        page: int,
        current_line: str,
        current_date: str,
        current_shift: str,
    ) -> Optional[tuple[HeaderRecord, list[DetailRecord]]]:
        """過去データを入力画面に開き直す (``NippouDB_Recall``)。

        **通してよいかは `recall_refusal` が決めます**(道も画面も
        自分では決めない)。そのうえで `check_shift_guard` の確認を
        通します ── 他の直を開くときに一言確かめる差し込み口で、
        Web版は常に通しますが、埋め込み側が握れるように残してあります。
        """
        if self.recall_refusal(report_date, line, shift, current_line,
                               current_date, current_shift):
            return None
        if not self.check_shift_guard(report_date, shift, current_date, current_shift, "過去データ呼出"):
            return None

        loaded = self.repo.load(report_date, line, shift, page)
        if loaded is None:
            return None

        self.recall = RecallState(
            True, report_date, line, shift, page,
            was_current=self.is_current_key(report_date, line, shift,
                                            current_line, current_date,
                                            current_shift))
        return loaded

    def clear_recall_mode(self) -> None:
        """Port of ``NippouDB_ClearRecallMode``."""
        self.recall = RecallState()

    def edit_page(
        self, page: int, current_date: str, current_shift: str, current_line: str
    ) -> Optional[tuple[HeaderRecord, list[DetailRecord]]]:
        """当直の指定ページを開いて直す (``NippouDB_EditPage``)。

        **動くのはページだけ。** 報告日もラインも直も、呼び出し側が渡した
        「いま」のものをそのまま使います ── 他の直へは、この道では
        入れません(そちらは `recall_data`)。

        【管理者モードは要りません】
        VBA は「過去ページの修正は管理者モードでのみ可能です」と断って
        いました。同じ直の中でページを戻るだけなら、**自分がさっき打った
        紙を自分で直している**だけで、誰の記録かは変わりません。
        12行を使い切って2ページ目を出したあと1ページ目の打ち間違いに気づく、
        は普通に起きるので、そのたびに人を呼ばせると現場が止まります。

        他の日・他の直・他のラインを開くほうは管理者モードのままです
        (`recall_refusal`)── そちらは**他人の記録**に触る話なので。
        """
        loaded = self.repo.load(current_date, current_line, current_shift, page)
        if loaded is None:
            return None
        # **当直のページ移動なので、開いた時点では必ず当直。** そのあと直が
        # 変わったら、断るのではなく知らせる(`logic/shift_boundary.py`)
        self.recall = RecallState(True, current_date, current_line,
                                  current_shift, page, was_current=True)
        return loaded

    def back_to_current(
        self, current_date: str, current_shift: str, current_line: str,
        save: Optional[Callable[[], bool]] = None,
    ) -> Optional[tuple[HeaderRecord, list[DetailRecord]]]:
        """Port of ``NippouDB_BackToCurrent``.

        **解除する前に、直していたページを保存する。** VBA が

            If g_NDB_RecallMode Then
                If NippouDB_Save(silentMode:=True) Then g_NDB_LastAutoSave = Now

        を先頭に置いているのがこれで、押した人は「直して、戻る」としか
        思っていない ── 保存を挟まないと、直した内容が黙って消えます。

        そのあと**DBから当直の最新ページを読み直します**。画面に残っている値を
        使うより確実で、呼出中に触った値が混ざりません。
        """
        if self.recall.active and save is not None:
            if save():
                self.mark_autosaved(datetime.now())
        self.clear_recall_mode()
        latest = self.repo.latest_page(current_date, current_line, current_shift)
        if latest >= 1:
            return self.repo.load(current_date, current_line, current_shift, latest)
        return None

    # ------------------------------------------------------------------
    # 自動保存 (``NippouDB_AutoSave``)
    # ------------------------------------------------------------------
    def recall_is_current_shift(self, current_date: str, current_shift: str) -> bool:
        """呼出中のキーが**いまの直そのもの**か。

        同じ直の中でページだけ動かしている状態(``NippouDB_EditPage``)を、
        他日・他直を開いている状態と区別します。
        """
        return (self.recall.report_date == current_date
                and self.recall.shift == current_shift)

    def autosave_reason(self, now: datetime, interval_sec: int,
                        current_date: str, current_shift: str) -> str:
        """いま自動保存してよいか。よければ空文字、駄目ならその理由。

        VBA ``NippouDB_AutoSave`` の判断をそのまま持ちます。

        1. **他日・他直を開いている最中は自動保存しない。** 管理者が
           先週の2直を見ているだけのつもりでも、画面の値が流れ込んで
           過去のデータを書き換えてしまいます。そちらへ書くのは
           「確定保存」を押したときだけ(ガードを通る)
        2. 同じ直の中でページだけ動かしているなら、自動保存してよい
        3. 前回から ``interval_sec`` 経っていなければ待つ

        **判断をここに置くのが要点です。** 画面側に置くと、タブを開き
        直すたびに間引きが最初からになり、呼出中かどうかも画面が
        知らないまま送ることになります。
        """
        if self.recall.active and not self.recall_is_current_shift(
                current_date, current_shift):
            return "他の直のデータを開いているため自動保存しません"
        if self._last_autosave is not None:
            waited = (now - self._last_autosave).total_seconds()
            if waited < interval_sec:
                return f"前回の自動保存から{int(waited)}秒しか経っていません"
        return ""

    def should_autosave(self, now: datetime, interval_sec: int,
                        current_date: str = "", current_shift: str = "") -> bool:
        """`autosave_reason` の真偽版。"""
        return not self.autosave_reason(now, interval_sec,
                                        current_date, current_shift)

    def mark_autosaved(self, now: datetime) -> None:
        self._last_autosave = now

    @property
    def last_autosave(self) -> Optional[datetime]:
        return self._last_autosave

    # ------------------------------------------------------------------
    # 呼出モード中の関門 (``NippouDB_BlockIfRecallMode``)
    # ------------------------------------------------------------------
    def recall_info(self) -> str:
        """port of ``NippouDB_RecallInfo``: いま何を開いているかの一言。"""
        if not self.recall.active:
            return "通常モード(呼出なし)"
        return (f"呼出中: {self.recall.report_date} / {line_names.label(self.recall.line)}"
                f" / {self.recall.shift} / ページ{self.recall.page}")

    def block_if_recall_mode(self, action_name: str) -> str:
        """その操作を断るか。断るなら理由、通すなら空文字。

        port of ``NippouDB_BlockIfRecallMode``。印刷・Accessへの反映の
        ように**当直の最新データを前提にする操作**の先頭で呼びます。

        呼出中に印刷すると、直している過去のページが「今日の帳票」として
        出ます。反映すれば、その過去データが共有のAccessへ行きます。
        どちらも押した人には見分けが付きません。
        """
        if not self.recall.active:
            return ""
        return (f"{self.recall_info()} です。"
                f"{action_name}の前に「最新のページに戻る」を押してください。")

    # ------------------------------------------------------------------
    # ページ移動 (``NippouDB_InitPageSpinner``)
    # ------------------------------------------------------------------
    def page_spinner(self, current_date: str, current_shift: str,
                     current_line: str) -> "PageSpinner":
        """ページ移動の見た目に要るもの一式。

        port of ``NippouDB_InitPageSpinner``。**押せるかどうかまでここで
        決めます** ── 画面が「保存データがあるか」「管理者か」を自分で
        判断すると、同じ規則が2か所に散ります。
        """
        if not (current_shift and current_line):
            return PageSpinner(status="現在: 判定不可")

        pages = self.repo.page_count(current_date, current_line, current_shift)
        if pages == 0:
            # **押せない理由を必ず書く。** 空の選択欄と灰色のボタンだけを
            # 置くと、壊れているのか、まだ何も無いだけなのか分からない
            return PageSpinner(
                status=f"当直: {current_date} {current_shift} / 保存データなし",
                note="この直はまだ保存されていないので、移れるページがありません。")

        current = self.recall.page if self.recall.active else pages
        return PageSpinner(
            # **この直の中でページを戻るのは誰でも。** 管理者モードが要るのは
            # 他の日・他の直・他のラインを開くときだけ(`edit_page` の説明)
            enabled=True, minimum=1, maximum=pages,
            current=min(max(current, 1), pages),
            status=(f"当直: {current_date} {current_shift}"
                    f" / 最新: ページ{pages} / 保存済み: ページ1-{pages}"),
            note="" if pages > 1 else "この直はまだ1ページだけです",
            recalling=self.recall.active)

    # ------------------------------------------------------------------
    # 当直の復旧 (``NippouDB_RestoreShift`` / ``NippouDB_RestoreForAdminOps``)
    # ------------------------------------------------------------------
    def restore_shift(self, report_date: str, line: str, shift: str
                      ) -> list[tuple[HeaderRecord, list[DetailRecord]]]:
        """その当直の**全ページ**をDBから読み直す。

        VBA では、ブックが壊れて雛形から再開したときに印刷シートを
        1ページずつ作り直す操作でした(``NippouDB_RestoreShift``)。Web版に
        シートは無いので、**DBが持っている全ページをそのまま返します** ──
        呼び出し側はそれを印刷用HTMLの作り直しに使えます。

        DBに何も無ければ空のリスト。**例外にはしません** ── 復旧できない
        ことと、アプリが壊れていることは別です。
        """
        pages = self.repo.page_count(report_date, line, shift)
        out: list[tuple[HeaderRecord, list[DetailRecord]]] = []
        for page in range(1, pages + 1):
            loaded = self.repo.load(report_date, line, shift, page)
            if loaded is None:
                # ページが飛んでいる(1と3だけ保存されている)ことはありうる。
                # **そこで止めない** ── 読めたぶんは復旧できる
                continue
            out.append(loaded)
        return out


@dataclass
class PageSpinner:
    """ページ移動の見た目。**画面はこれを写すだけ。**"""

    enabled: bool = False
    minimum: int = 1
    maximum: int = 1
    current: int = 1
    status: str = ""
    note: str = ""
    recalling: bool = False

    def as_dict(self) -> dict:
        return {
            "enabled": self.enabled, "min": self.minimum, "max": self.maximum,
            "current": self.current, "status": self.status, "note": self.note,
            "recalling": self.recalling,
        }
