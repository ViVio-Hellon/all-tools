"""画面をまたいで持つ作業状態 (プロセスに1つ)

tkinter版では、選択中のライン・過去データ呼出(recall)の状態が
`ui/app.py` のインスタンス変数に入っていた。Web版では画面が要求ごとに
作り直されるので、その置き場所をここに移す。

**プロセスに1つで、保存しない**(落ちたら消える)。1サーバ=1作業者で、
複数タブを開いても同じ状態を共有する ── ラインごとに1台のPCで1プロセス
という運用(tkinter版と同じ)が前提なので、これで足りる。

帯(リボン)に出す値もここから作る。画面を移っても消えないのが要点で、
**同じ事実を2か所に持たない**ため、画面側は自分で覚えない。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

from . import constants
from .logic.shift import ShiftCalculator
from .logic.shift_anchor import Anchor as ShiftAnchor
from .services.nippou_service import (RecallState, ServiceState,
                                      format_business_date)

__all__ = ["RecallState", "ShiftAnchor", "WorkContext", "get_context", "reset"]

#: 設備番号を持つのは中板(丸徳。VBA の MARU)だけ(1..7)
MARU_LINE = "中板"
MARU_SUBS = frozenset(str(i) for i in range(1, 8))


@dataclass
class WorkContext:
    """いまの作業状態。"""

    line: str = constants.LINE_NAMES[0]
    # 丸徳ラインだけが持つ設備番号(1..7)
    maru_sub: str = ""
    # 呼出モードと最後の自動保存。**サービスと同じ入れものを共有する** ──
    # Web版のサービスは要求ごとに作り直されるので、あちらに持たせると
    # 毎回まっさらになり、間引きも呼出中の判定も効かない
    service_state: ServiceState = field(default_factory=ServiceState)
    # 管理者モード(`chkAdminMode` 相当)。認証に通ると True
    admin: bool = False
    # マスタの**編集**を許したか。見るのは誰でもできて、直すときだけ
    # 管理者パスワードが要る(`app/routes/master.py`)。管理者モードとは
    # 別に持つ ── 過去データを見るために管理者モードにした人が、その
    # ついでに共有マスタを書き換えられてしまうのは筋が違う
    master_edit: bool = False
    # 最後に見た (ライン, 報告日, 直)。**直の変わり目を知るためだけ**に
    # 持ちます(`roll_over`)。保存先を決めるのはこれではなく、そのつど
    # 時計から引いた値です ── ここを保存先にすると、時計と食い違ったまま
    # 固まったときに気づけません。
    #
    # **ラインまで持つ。** 丸徳・トット・バランサーは常に日勤なので、
    # ラインを変えると直の名前も変わります。それを「直が変わった」と
    # 読むと、**ラインを変えたその操作のために開けた管理者モードが、
    # その場で落ちます**(ラインの切り替えは管理者モード専用)
    seen_key: tuple[str, str, str] = ("", "", "")

    # **入力先の直。作業者を選んだ時点で決まります**(`logic/shift_anchor`)。
    #
    # ここが空のあいだは時計から引きます(まだ直が始まっていないので)。
    # 選んだ時点で固定し、以後その端末はその直へ書き続けます ── 時計に
    # 任せると 15:00 をまたいだ瞬間に、打っている本人は1直のつもりなのに
    # 画面とサーバが2直を指すことになります。
    #
    # **保存しません**(プロセスに1つ)。タブを閉じるとアプリごと終わる
    # 作りなので(`idle_exit`)、開き直したときは作業者を選ぶところから
    # ── それが直の始まりの合図です。
    anchor: ShiftAnchor = field(default_factory=ShiftAnchor)

    # **この端末のライン**(据え付けで決めたもの)。`line` は過去データを
    # 呼び出すと一時的に別のラインになる(管理者が他ラインの紙を開いた
    # とき)ので、戻る先をここに別に持ちます。保存するのはこちらだけ
    terminal_line: str = constants.LINE_NAMES[0]
    terminal_maru_sub: str = ""

    @classmethod
    def from_settings(cls) -> "WorkContext":
        """**覚えておいたラインで始める。** 覚えていなければ L-1。

        以前は毎回 L1 から始めていたので、画面を閉じて起動し直す
        (`idle_exit`)たびに、LVC の端末が L1 として保存していました。
        """
        from . import config, user_settings

        ctx = cls()
        line = stored_terminal_line()
        if line in constants.LINE_NAMES:
            ctx.line = ctx.terminal_line = line
            sub = str(user_settings.get(config.KEY_TERMINAL_MARU) or "")
            if line == MARU_LINE and sub in MARU_SUBS:
                ctx.maru_sub = ctx.terminal_maru_sub = sub
        return ctx

    def set_terminal_line(self, line: str, maru_sub: str = "") -> bool:
        """この端末のラインを変えて**覚える**。覚えられなければ False。

        変えるのはその場で(画面はすぐ新しいラインで動く)。覚えるのに
        失敗したときは呼ぶ側が一言添えます ── 次に起動したとき元に
        戻るので、黙っていると同じ取り違えがまた起きます。
        """
        from . import config, user_settings

        sub = maru_sub if (line == MARU_LINE and maru_sub in MARU_SUBS) else ""
        self.line = self.terminal_line = line
        self.maru_sub = self.terminal_maru_sub = sub
        return user_settings.save_many({config.KEY_TERMINAL_LINE: line,
                                        config.KEY_TERMINAL_MARU: sub})

    def return_to_terminal_line(self) -> None:
        """過去データを閉じたら、**この端末のライン**へ戻す。

        他ラインの紙を呼び出すと `line` はそのラインになります。戻さないと、
        「いまの直に戻る」を押したあとも他ラインの端末として動き続けます。
        """
        self.line = self.terminal_line
        self.maru_sub = self.terminal_maru_sub

    @property
    def editor(self) -> bool:
        """**他の直・他のラインの記録を開いて直せるか**(v4.12.0)。

        管理者モード(パスワード)か、マスタの「アクセス権限」の表で
        Administrator のPC(`services/access_rights`)。

            権限 が Administrator のユーザーは LocalBackup すべてのライン
            データを 記録を見る で参照でき編集もできる

        **記録を直すところだけ**に使います。この端末のラインを変える・
        パスワードや参照パスを変える、などの設定は、これまでどおり
        管理者モード(`admin`)だけです(「パスワードがあれば現行の通り変更可」)。
        """
        from .services import access_rights

        return self.admin or access_rights.is_administrator()

    # `ctx.recall` はこれまでどおり読めるようにしておく(呼び出し側を
    # 変えないため)。実体は `service_state` の中の1つだけ
    @property
    def recall(self) -> RecallState:
        return self.service_state.recall

    @recall.setter
    def recall(self, value: RecallState) -> None:
        self.service_state.recall = value

    # -- いまの対象キー ---------------------------------------------
    def force_day_shift(self) -> bool:
        """このラインは常に日勤扱いか(`TimeCheck` の分岐)。"""
        return self.line in constants.DAY_SHIFT_ALWAYS_LINES

    def package_calc_enabled(self) -> bool:
        """このラインで包み数の自動計算を行うか。"""
        return self.line in constants.PACKAGE_CALC_LINES

    def current_key(self, calc: ShiftCalculator,
                    now: Optional[datetime] = None) -> tuple[str, str, str]:
        """いま入力すべき(報告日, ライン, 直)。

        呼出中(recall)ならそのキー、そうでなければ現在時刻から決まる直。
        """
        if self.recall.active:
            return (self.recall.report_date, self.recall.line, self.recall.shift)
        # **作業者を選んだ時点で決まった直**があれば、そちらへ書きます。
        # 時計から引き直すと、15:00 をまたいだ瞬間に、打っている本人は
        # 1直のつもりなのに書き先だけが2直へ動きます
        # (`logic/shift_anchor.py` に経緯を書いてあります)
        if self.anchor.filled and self.anchor.line == self.line:
            return self.anchor.key
        now = now or datetime.now()
        shift = calc.time_check(now, self.force_day_shift())
        business_date = calc.today_check(now, self.force_day_shift())
        return (format_business_date(business_date), self.line, shift)

    def business_date(self, calc: ShiftCalculator,
                      now: Optional[datetime] = None) -> date:
        now = now or datetime.now()
        return calc.today_check(now, self.force_day_shift())

    def clock_key(self, calc: ShiftCalculator,
                  now: Optional[datetime] = None) -> tuple[str, str]:
        """**時計だけ**から決まる (報告日, 直)。

        `current_key` は呼出中だと呼出のキーを返すので、直が変わったかを
        見るのには使えません(必ず一致してしまう)。
        """
        now = now or datetime.now()
        shift = calc.time_check(now, self.force_day_shift())
        business_date = calc.today_check(now, self.force_day_shift())
        return (format_business_date(business_date), shift)

    # -- 直の変わり目 ---------------------------------------------------
    def roll_over(self, calc: ShiftCalculator,
                  now: Optional[datetime] = None) -> str:
        """直が変わっていたら、**その直だけのもの**を落とす。

        VBA は保存処理の最後で ``Unload UFdaily`` していました。フォームを
        捨てるので、フォームが持っていたもの ── なかでも
        ``chkAdminMode`` ── は次に開いたとき必ず消えていました。Web版の
        画面は開きっぱなしにできるので、同じことを時刻で行います。

        【落とすもの】
            管理者モード     … 昼に開けた人が帰っても、夜勤がその権限を
                               引き継いでしまう。VBA では有り得なかった
            マスタ編集の許可 … 同じ理由。共有のマスタに書ける状態が
                               持ち越されるのは、いちばん重い

        【落とさないもの ── 呼出モード】
        VBA でも ``g_NDB_RecallMode`` は標準モジュールの変数で、
        ``Unload UFdaily`` では消えませんでした。**こちらも消しません。**
        消すと保存先が黙って動きます ── 管理者が先週の2直を開いたまま
        17:00 をまたいだ瞬間に呼出が解けると、直していた内容が
        **今日の2直へ**入ります。またいだことは保存の関門
        (`logic/shift_boundary.py`)が止めるので、ここで消す必要も
        ありません。

        落としたものがあればその一言、無ければ空文字を返します。
        """
        report_date, shift = self.clock_key(calc, now)
        key = (self.line, report_date, shift)
        if self.seen_key == key:
            return ""
        seen_line, seen_date, seen_shift = self.seen_key
        self.seen_key = key
        if not seen_shift:
            # 起動して最初の1回。**何も落としません** ── 「変わった」の
            # ではなく、まだ一度も見ていなかっただけ
            return ""
        if seen_line != self.line:
            # ラインが変わっただけ。**直が変わったのではありません** ──
            # 日勤固定のラインへ移ると直の名前も変わるので、ここを
            # 見分けないと、切り替えた本人の管理者モードを落とします
            return ""
        dropped: list[str] = []
        if self.admin:
            self.admin = False
            dropped.append("管理者モード")
        if self.master_edit:
            self.master_edit = False
            dropped.append("マスタ編集の許可")
        if not dropped:
            return ""
        return (f"直が変わったので、{'と'.join(dropped)}を解除しました"
                f"({key[0]} {key[1]})")

    # -- 帯 -----------------------------------------------------------
    def recall_is_this_shift(self, calc: ShiftCalculator,
                             now: Optional[datetime] = None) -> bool:
        """呼出中のキーが、**時計から決まるいまの直**と同じか(同じ直のページ移動)。

        `current_key` は呼出中だと呼出のキーを返すので、それと比べても必ず
        一致します ── 時計から決まる直と比べます。
        """
        if not self.recall.active:
            return False
        now = now or datetime.now()
        shift = calc.time_check(now, self.force_day_shift())
        report_date = format_business_date(calc.today_check(now, self.force_day_shift()))
        return (self.recall.report_date == report_date and self.recall.shift == shift
                and self.recall.line == self.line)

    def ribbon(self, calc: ShiftCalculator, page: int = 1,
               now: Optional[datetime] = None,
               key: Optional[tuple[str, str, str]] = None) -> dict[str, Any]:
        """帯に出す値。**空欄にせず「—」を出す**(空欄は読めない)。

        `key` を渡すと、そちらを(報告日, ライン, 直)として出します ──
        **帯は「いまどこへ書くか」を出すもの**なので、書く先が
        `current_key` と違うとき(直の変わり目で「打っていた直へ入れる」を
        選んだとき)は、書く先のほうを出さなければ嘘になります。
        """
        report_date, line, shift = key or self.current_key(calc, now)
        chips: list[dict[str, str]] = []
        # ------------------------------------------------------------
        # **終わり方を、状態と同じ場所に置く。**
        #
        # 「過去データの終了方法 / 管理者モードの終了方法 これらがわからない」
        # と言われたところです。どちらも帯に「いま入っている」ことは
        # 出していましたが、**出るだけ**で、やめる場所は別の画面の奥に
        # ありました(呼出は日報入力の下のほう、管理者モードは
        # 設定・管理者の「この端末」)。
        #
        # 入った印をそのまま押して出られるようにします ── 状態の隣に
        # 出口があれば、探しに行かずに済みます。
        # ------------------------------------------------------------
        if self.recall.active and self.recall_is_this_shift(calc, now):
            # **同じ直の前のページを直しているだけ**(v4.8.0)。「過去データ」と
            # 出すと、自分がさっき打った紙なのに触ってはいけないものに見える
            # (日報入力の見出しも「この直の第Nページを直しています」)
            chips.append({
                "text": f"第{self.recall.page}ページを直し中",
                "kind": "alert", "action": "back",
                "title": "押すと、このページを保存してから最新のページに戻ります",
                "confirm": (f"第{self.recall.page}ページを保存して、最新のページに戻ります。"
                            "よろしいですか?"),
            })
        elif self.recall.active:
            chips.append({
                "text": f"過去データ {self.recall.shift}",
                "kind": "alert", "action": "back",
                "title": "押すと、過去データを閉じて、いまの直の入力に戻ります",
            })
        if self.admin:
            chips.append({
                "text": "管理者", "kind": "ok", "action": "admin-off",
                "title": "押すと、管理者モードを終わります",
            })
        # ------------------------------------------------------------
        # **時計の直が、出している直と違うなら言う。**
        #
        # 「直時間超えても何も言わないね」。過去データを開いているあいだ、
        # 直の変わり目の見張り(`shift_boundary`)は**わざと黙ります** ──
        # その人は他の直を開いていると分かって開いているので、
        # 「打っていた直へ入れますか」と訊くほうが嘘になります。
        #
        # ですが「いま何直なのか」は別の話で、**それは黙る理由がない。**
        # 出している直と時計が食い違っているあいだ、ここに出します。
        # ------------------------------------------------------------
        clock_shift = calc.time_check(now or datetime.now(),
                                      self.force_day_shift())
        if clock_shift and shift and clock_shift != shift:
            chips.append({
                "text": f"いま {clock_shift}", "kind": "warn",
                "title": f"時計は {clock_shift} です。"
                         f"画面に出ているのは {shift} のぶんです",
            })
        # ------------------------------------------------------------
        # **ラインを決めていない端末では、帯に L1 と出さない。**
        #
        #     結局 未設定 があっても L1 って出てればどっちがあってかは
        #     わからないから駄目です
        #     未設定なら L1と出さないようにしてください
        #
        # 帯に「L1」と出すと決まっているように読めます(隣に「未設定」の
        # 印を並べても、どちらが本当か分からない)。**ラインの枠そのものを
        # 「未設定」にして**、警告の色で出します。決まるまで日報を打てない
        # ことは、枠の吹き出しと日報入力の関門(`terminal_line_note`)が
        # 言います(v4.3.0 から。それまでは L1 として保存していた)。
        # ------------------------------------------------------------
        # 過去の紙を呼び出しているあいだは、そのラインを出す(ラインを持たない管理者のPCでも)
        unset = not (terminal_line_decided() or self.recall.active)
        from .logic import line_names

        # **見せるのは正規の呼び名**(v4.12.5)。中板は設備番号つき(「中板4」── 前は
        # 「設備4」の印を別に出していた)
        sub = self.maru_sub if line == self.line else ""
        return {
            "report_date": report_date or "—",
            "line": LINE_UNSET if unset else (line_names.label(line, sub) if line else "—"),
            "shift": shift or "—",
            "page": str(page) if page else "—",
            "chips": chips,
            # 警告の色で出す枠と、その吹き出し。**決めるのはここ**(画面は塗るだけ)
            "warn": ["line"] if unset else [],
            "titles": {"line": ("この端末のラインをまだ決めていません。決まるまで日報は"
                                "打てません(保存もしません)。設定・管理者 →"
                                "「この端末と配布」→「この端末のライン」で決めてください")
                       } if unset else {},
        }


# ------------------------------------------------------------------
# プロセスに1つ
# ------------------------------------------------------------------
_lock = threading.RLock()
_context: Optional[WorkContext] = None


def get_context() -> WorkContext:
    global _context
    with _lock:
        if _context is None:
            _context = WorkContext.from_settings()
        return _context


def reset() -> WorkContext:
    """作り直す。**テストが状態を持ち越さないために使う。**

    覚えておいたライン(`user_config.json`)からは読み直します ──
    アプリを起動し直したときと同じ始まり方にするためです。
    """
    global _context
    with _lock:
        _context = WorkContext.from_settings()
        return _context


def terminal_line_note() -> str:
    """この端末のラインを**まだ決めていなければ**、そう言う文。決めてあれば空。

        配布設定挙動に関して不備がないかチェックしてください
        からの状態からきれいに引き継げるか不安です

    まっさらな端末で配布設定から起動して確かめたところ、参照パスも
    パスワードも直の時間も引き継げていましたが、**ラインだけは黙って L1**
    で動いていました。ラインは端末ごとに違うので配布設定には既定で入れず、
    配った先で1度決めてもらう決まりです ── ところが決めていないことが
    日報入力のどこにも出ないので、LVC の端末が L1 のキーで保存し、
    共有の `T_日報ヘッダー_L1` へ送ります。気づくのは翌日の集計です。

    **v4.3.0 から、決まるまで日報は打たせません**(関門。表を伏せ、保存も
    断る ── `routes/entry.py` の `needs_line` / `line_refusal`)。

        この端末のラインがまだ決まっていません…決まるまでは L1 として
        保存します。：決めるまで入力させないほうがいいね

    それまでは止めずに L1 として保存していました(L1 の端末は決めなくても
    正しく動くので)。ところが知らせを読まずに打ち始めると、**別のラインの
    日報が L1 に混ざり**、直すにはその直を全部打ち直すことになります。
    決めるのは1度きりなので、最初に止めるほうが安くつきます。

    **帯には L1 と出さない**(「未設定」と出す ── `WorkContext.ribbon`)。
    帯に「ライン L1」が出ていたので、「決まってるように見えますが、
    なんですか？」と読まれました。L1 の端末でも1度決めれば、この
    関門は消えます。
    """
    if terminal_line_decided():
        return ""
    from .services import access_rights

    # **表で決まらなかった理由も言う**(v4.12.3)。マスタの書き間違いなら、そこで気づける
    reason = access_rights.gate_reason()
    return ("この端末のラインがまだ決まっていません(上の帯のラインは「未設定」)。"
            "決まるまで日報は打てません(保存もしません)── どのラインの日報か"
            "分からないまま保存すると、別のラインの紙に混ざります。L1 の端末でも"
            "1度決めてください。決める場所: マスタの「アクセス権限」の表にこのPCの行を"
            "書くか、設定・管理者 →「この端末と配布」→「この端末のライン」(管理者モード)。"
            + (f"\n{reason}" if reason else ""))


#: ラインを決めていない端末で、保存などを断るときの文(短いほう)
LINE_GATE_MESSAGE = ("この端末のラインがまだ決まっていないので、日報は打てません"
                     "(保存もしません)。設定・管理者 →「この端末と配布」→"
                     "「この端末のライン」で決めてください(管理者モード)。")


#: ラインを決めていない端末で、帯のラインの枠に出す字
LINE_UNSET = "未設定"


def line_known(ctx: Optional["WorkContext"] = None) -> bool:
    """**どのラインの日報を書くのか分かっているか**(v4.12.0)。

    この端末のラインを決めてあるか、**過去の紙を呼び出して直している**か。
    呼び出した紙はそのラインのもの(キーに入っている)なので、ラインを
    持たない管理者のPC(アクセス権限の表で Administrator だけ)でも直せます。
    """
    ctx = ctx or get_context()
    return terminal_line_decided() or bool(ctx.recall.active)


def stored_terminal_line() -> str:
    """覚えてあるこの端末のライン。**v4.12 までの名前(`LS`)は正規(`機側`)へ読み替える**。

    v4.13.0 でツールの名前を正規の呼び名にしました。それより前に決めた端末・配布設定には
    VBA の名前が残っているので、読むときに揃えます(書き直すのは次にラインを変えたとき)。
    """
    from . import config, user_settings
    from .logic import line_names

    return line_names.upgrade(str(user_settings.get(config.KEY_TERMINAL_LINE) or ""))


def terminal_line_decided() -> bool:
    """この端末のラインを決めてあるか(`user_config.json` / 配布設定)。"""
    return stored_terminal_line() in constants.LINE_NAMES
