"""日報入力画面 (tkinter版 `ui/app.py` + `ui/entry_grid.py`)

    GET  /                    画面
    POST /api/entry/state     入力内容を送り、決まる値を返してもらう
    POST /api/entry/save      保存する(`NippouDB_Save` 相当)
    POST /api/entry/line      ラインを変える
    POST /api/entry/check     チェックボックスの排他制御
    POST /api/entry/verify    この直をチェック(共有へ保存が断る7項目)
    POST /api/entry/close     直の終わり間際の自動確定(画面が1分ごとに叩く)
    POST /api/entry/prev-shift-ack  前の直が空の警告を閉じる

【なぜ入力のたびにサーバへ送るのか】
単重・包み数・排他制御・時刻の引き継ぎは**業務ルール**で、tkinter版では
`logic/` の純関数が持っていた。JS側に写すとテストの無い側に業務が分裂
するので、欄から離れたとき(blur)にまとめてサーバへ送り、決まった値を
受け取って画面に写す。相手は 127.0.0.1 なので往復は1ms程度で済む。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from flask import Blueprint, jsonify, render_template, request

from nippou import constants, work_context
from nippou.config import SETTINGS
from nippou.logging_setup import get_logger, log_button_click
from nippou.logic import (
    etc_marks,
    g_course,
    input_rules,
    load_factor,
    lot_fill,
    pages,
    validation,
)
from nippou.logic.shift import (ShiftCalculator, ShiftTimes, calculator_from_master,
                                parse_business_date, usable_pair)
from nippou.presenters import entry as presenter
from nippou.presenters import settings as settings_view
from nippou.services import lot_lookup
from nippou.services.nippou_service import (NippouService,
                                            format_business_date)

from .. import error_body, get_repo, shell

log = get_logger("app.routes.entry")

bp = Blueprint("entry", __name__)


# ------------------------------------------------------------------
# 共通の組み立て
# ------------------------------------------------------------------
def build_shift_calculator(raw: dict[str, tuple[str, str]]) -> ShiftCalculator:
    """直の境界時刻。tkinter版 `ui/app.py:build_shift_calculator` と同じ。

    **読み方は `logic/shift.times_from_master` の 1 か所**(片側でも欠けていたら・時:分として
    読めなければ両方とも既定に落とす)。保存前チェック・押した記録・過去日報の取り込み・
    全停入力も同じ関数で読む ── 同じ直の終わりを 2 通りに決めていたことがある。
    """
    return calculator_from_master(raw)


def current_calculator() -> ShiftCalculator:
    return build_shift_calculator(get_repo().get_shift_times())


def _shift_end_times(calc: ShiftCalculator) -> dict[str, tuple[str, str]]:
    """`Same_Text` が使う「直の正規の終了時刻」。"""
    t = calc.times

    def split(hhmm: str) -> tuple[str, str]:
        hour, minute = hhmm.split(":")
        return hour, minute

    return {"日勤": split(t.end_day), "1直": split(t.end1),
            "2直": split(t.end2), "3直": split(t.end3)}


def _carried_rows(payload: dict) -> set[int]:
    """画面が持っている「写した印」を読む。**読めないものは印にしません。**

    印が無い = 人が打った値、として扱います ── 迷ったら**消さない**
    ほうへ倒します(`logic/navigation.carry_decision`)。
    """
    found = payload.get("carried")
    if not isinstance(found, (list, tuple)):
        return set()
    rows = set()
    for item in found:
        try:
            row = int(item)
        except (TypeError, ValueError):
            continue
        if 1 <= row <= constants.ROW_COUNT:
            rows.add(row)
    return rows


def _target(ctx: work_context.WorkContext, calc: ShiftCalculator):
    """いま入力すべき (報告日, ライン, 直, ページ)。"""
    report_date, line, shift = ctx.current_key(calc)
    if ctx.recall.active:
        return report_date, line, shift, ctx.recall.page
    page = get_repo().latest_page(report_date, line, shift) or 1
    return report_date, line, shift, page


def ribbon_now(ctx: work_context.WorkContext, calc: ShiftCalculator) -> dict:
    """日報入力以外の画面の帯。**ページもいま書いているページで出す。**

    以前は `ctx.ribbon(calc)` で既定の1ページを出していて、2ページ目を打って
    いても、ほかの画面を開くと帯が「ページ 1」に戻っていた(1分後の見張りで直る)。
    """
    return ctx.ribbon(calc, _target(ctx, calc)[3])


def _load_state(report_date: str, line: str, shift: str, page: int):
    """保存済みがあれば読み、無ければ空。"""
    loaded = get_repo().load(report_date, line, shift, page)
    if loaded is None:
        return presenter.empty_state()
    header, details = loaded
    return presenter.from_records(header, details)


def _stop_choices() -> list:
    """停止理由の一覧。**1リクエストにつき1度だけマスタを引きます。**

    引き先は共有の伝送用ファイルで、ネットワークの向こうにあることも
    あります。1回の応答の中で「その他」の判定と7項目の記号チェックが
    別々に引くと、**欄から離れるたびに2往復**することになります。

    マスタが差し替わったら次の要求から新しいほうに合います ──
    覚えておくのは1リクエストのあいだだけなので。
    """
    from flask import g

    found = getattr(g, "_nippou_stop_choices", None)
    if found is None:
        found = presenter.stop_choices()
        g._nippou_stop_choices = found
    return found


def _other_codes() -> set[str]:
    """「その他」に当たる停止理由の記号。理由欄を開ける鍵。

    要求のたびにマスタを引き直します ── 入力の途中でマスタが差し替わって
    も、次の応答からは新しいほうに合います。読めなければ空集合
    (理由欄は開いたまま)。
    """
    return presenter.other_stop_codes(_stop_choices())


def _view(state, ctx, calc, *, message: str = "", focus: str = "",
          problem=None, other_codes=None, key=None) -> dict:
    """画面ぜんぶ。

    `key` を渡すと、`_target` の代わりにそれを保存先として描きます ──
    直の変わり目をまたいで「打っていた直へ入れる」が選ばれたときに要ります。
    **渡さないと、書いた先と画面の表示が食い違います**(1直へ書いたのに
    画面は2直、という形で出ます)。
    """
    report_date, line, shift, page = key or _target(ctx, calc)
    _computed_totals_from_saved(state, report_date, line, shift, page)
    # 赤い行は先に数える ── ページの案内が「先に赤い行を直して」と言うため
    bad = _bad_rows(state, ctx, calc, shift=shift)
    body = presenter.view_model(
        state, report_date=report_date, line=line, shift=shift, page=page,
        page_count=get_repo().page_count(report_date, line, shift),
        # ページを選び直すための一覧。**開いている直のもの**を出す ──
        # 呼出中なら、その直のページ(いまの直ではない)
        pages=get_repo().saved_pages(report_date, line, shift),
        recall=ctx.recall.active,
        recall_other=_recall_is_other_shift(ctx, calc),
        # **見るだけかどうかもサーバが決める。** 判定は保存を断る条件と
        # 同じものです(下の 403)── 画面とサーバで別々に決めると、
        # 打てるのに保存できない(またはその逆)が起きます
        read_only=_is_read_only(ctx, calc),
        package_calc=ctx.package_calc_enabled(),
        message=message, focus=focus,
        other_codes=_other_codes() if other_codes is None else other_codes,
        bad_rows=len(bad))
    # 時間計算の断り。**入力は受け付けたうえで**画面に出す ── 打っている
    # 途中で保存できなくなるほうが困る(VBA は MsgBox で止めていた)
    body["time_problem"] = None if problem is None else {
        "row": problem.row, "reason": problem.reason,
        "message": problem.message, "field": problem.field,
        "sound": problem.sounds,
    }
    # **直っていない行の印。** トーストは数秒で消えるので、画面から目を
    # 離すと「出ていた」ことごと消えます。行そのものに印を残して、
    # 保存(確定)でも止めます(`/api/entry/save`)
    body["bad_rows"] = bad
    # 直ひと回りの動線。**保存したあとに何が残っているか**まで出す
    # (前半は打ちかけの値で進める ── 保存前でも帯が動くように)
    body["flow"] = current_flow(
        report_date, line, shift, recall=ctx.recall.active,
        worker=state.header.get("worker", ""),
        rows_used=presenter.used_rows(state)).as_dict()
    # 前の直に1ページも無ければ、一度だけ知らせる(`logic/prev_shift.py`)
    body["prev_shift"] = prev_shift_warning(ctx, calc).as_dict()
    # **後から作っている枠なら、そう言う**(v4.0.0「後日作ったことがわかる」)
    body["backfill"] = _backfill_view(ctx, report_date, line, shift, page)
    # **帯も一緒に返す。** 帯はサーバが描いた文字のままで、画面を移るまで
    # 更新されませんでした ── 開いたまま 17:00 をまたぐと、**画面が
    # 「1直」と言いながら2直へ書く**ことになります。応答のたびに渡して、
    # 画面はそのとおりに塗り替えます(`static/js/ribbon.js`)
    body["ribbon"] = ctx.ribbon(calc, page, key=(report_date, line, shift))
    body["prev_shift"] = prev_shift_warning(ctx, calc).as_dict()
    # 「直の終わり」の押しボタンに出す時刻(**開いている直の**終わり)
    body["shift_end"] = _shift_end_text(calc, shift)
    # **直の始めに「必ずやること」を1つ決める。**
    #
    # 「結局初めに何をするのかルール化が決まっていない」と言われました。
    # 開けば12行が打てる状態なので、直が変わったことにも、まだ何も
    # していないことにも、画面からは気づけません。
    #
    # 始まりは**作業者を選ぶこと**にします ── 作業者はどのみち必ず要る
    # ものなので、手間が1つ増えるわけではありません。選ぶまでは表を
    # 伏せ、選んだ時点で打てるようにします。
    #
    # **見るだけの画面では出しません。** 過去の直を開いているときに
    # 「作業者を選んでください」と出すと、直せないものを直せと言う
    # ことになります。
    #
    # **ラインが決まるまでは、作業者より先にそちらを決める**(v4.3.0)。
    # どのラインの日報かが決まらないまま作業者を選ばせても、保存は
    # 断るので(`line_refusal`)、手順が1つ先へ進んだように見えるだけです
    body["needs_line"] = not work_context.line_known(ctx)
    # **見せるラインの名前。** 決まっていなければ L1 と出さない(帯と同じ ──
    # 「未設定なら L1と出さないようにしてください」)。保存先のキーは
    # `line` のまま(画面が持ち回すもので、見せる字ではない)
    from nippou.logic import line_names
    body["line_label"] = (work_context.LINE_UNSET if body["needs_line"]
                          else line_names.label(line, ctx.maru_sub if line == ctx.line else ""))
    # **呼び出して過去の直を開いているあいだは伏せない。** 「作業者を先に選ぶ」は
    # 新しい直を始めるときの決まりです。前の直を直しに来た画面にまで当てると、
    # 作業者が空のこと自体が直すところの1つなので、**どこも直せない行き止まり**
    # になっていました(「記録を見るから呼び出したが、画面がロックされていて
    # 直せない、触れない」)。作業者が空なことは、直すところとして出し続けます
    body["needs_worker"] = bool(
        not body["read_only"] and not body["needs_line"] and not ctx.recall.active
        and not str(state.header.get("worker", "")).strip())
    # **直の始めに、前の直の始末をつける。**
    #
    # 押し忘れ・打ち間違いのまま直の時間が過ぎ、次の直がそのまま入力を
    # 進めてしまう ── ここに気づく人が居ないと、翌月の集計まで誰も
    # 気づきません。**気づく人が必ず1人いる場所**は直の始まりだけです。
    #
    # 止めるのは「まだ1ページも打っていない直」のあいだだけ
    # (`logic/handover.decide`)。打ち始めた人を途中で止めません。
    #
    # ラインが決まっていなければ数えません ── 仮の L1 の「前の直」を
    # 片付けさせても、この端末の直ではないかもしれません
    if body["needs_line"]:
        from nippou.logic.handover import Gate

        body["handover"] = Gate().as_dict()
    else:
        body["handover"] = _handover(ctx, key=(report_date, line, shift),
                                     read_only=body["read_only"]).as_dict()
    # **控えの時刻で動いているなら、黙らない。**
    #
    # 直の境界は時間マスタ(`shift_config`)から来ます。読めないときは
    # 控え(`ShiftTimes` の既定)に落ちますが、**落ちたことが画面のどこ
    # にも出ていませんでした** ── 催促も自動確定も最終時間チェックも
    # 控えの時刻で動くのに、誰も気づけません。
    from nippou.logic import shift as shift_logic

    try:
        body["shift_times_note"] = shift_logic.shift_times_note(
            get_repo().get_shift_times())
    except Exception:                             # noqa: BLE001 - 画面は出す
        log.exception("直の時間の出どころを確かめられませんでした")
        body["shift_times_note"] = ""
    # **ラインを決めていない端末は、黙って L1 で動かない**(配った先の端末)
    body["line_note"] = work_context.terminal_line_note()
    # **固定した直の様子。** まだ打てるのか、あと何分で見るだけになるのか
    # (`logic/shift_anchor`)。固定していなければ空で返ります
    body["anchor"] = ctx.anchor.as_dict()
    body["anchor_standing"] = anchor_standing(ctx, calc).as_dict()
    # **なぜ見るだけなのかで、出口が変わります**(`_read_only_reason`)
    body["read_only_reason"] = _read_only_reason(ctx, calc)
    # **紙の束。** その日・そのラインの紙を、直→ページの順に並べます
    # (`logic/sheet_strip`)。VBA の シートタブ に当たるものです ──
    # 前の直の紙が残っていること・いまどれを開いていることが、
    # 一目で分かるようにするため
    body["sheets"] = _sheet_strip(report_date, line, shift, page)
    # **この直の直すところ。押す前から出しておきます。**
    #
    #     「設定タブ内で不備は当然確認できるとしても（管理者用）
    #       入力しか一般作業者は開かないので
    #       そこで何故ダメかが見えないと意味がないかと」
    #
    # 前の直のぶんは引き継ぎの帯が理由まで出したままにしていますが、
    # **いま打っている直**は、直の終わりに断られるまで分かりませんでした。
    #
    # **画面のページは画面の中身で見ます**(v4.18.0)── 「入れてるんだけどずっと
    # 出てますね」(休憩)。保存済みだけを見ていたので、打った休憩が届いていません
    # でした(過去データを開いているあいだは自動保存もしない)
    body["shift_findings"] = _shift_findings(
        report_date, line, shift, admin=ctx.editor,
        screen=_screen_page(state, report_date, line, shift, page)).as_dict()
    return body


def _computed_totals_from_saved(state, report_date: str, line: str, shift: str,
                                page: int) -> None:
    """計算で決まる合計欄(ﾛｯﾄ数・係数Lot数)は、**保存済みの値**で描く(v4.20.0)。

    保存のたびにサーバが数える欄で、画面からは打てません。なのに応答は画面が
    送ってきた値をそのまま返していたので、**保存の直前に出ていた自動保存の応答が
    あとから届くと、数えたばかりの値が空で塗り直されていました**(次の応答で戻る)。
    計算で決まる欄の出どころは保存済みのページだけ、にします。
    """
    from nippou.logic import load_factor

    fields = [name for name, auto in (
        ("lot_count", load_factor.applies(line)),
        ("coefficient_lot_count", load_factor.counts_coefficient(line))) if auto]
    if not fields:
        return
    try:
        loaded = get_repo().load(report_date, line, shift, page)
    except Exception:                             # noqa: BLE001 - 画面は出す
        log.exception("合計欄を読み直せませんでした key=%s", (report_date, line, shift, page))
        return
    if loaded is None:
        return
    header, _details = loaded
    for name in fields:
        state.header[name] = getattr(header, name, "") or ""


def _screen_page(state, report_date: str, line: str, shift: str, page: int):
    """画面の1ページを、確かめに渡す形へ(`shift_check.ScreenPage`)。"""
    from nippou.services import shift_check

    header, details = presenter.to_records(state, report_date, line, shift, page)
    return shift_check.ScreenPage(page=page, header=header, details=details)


def _shift_findings(report_date: str, line: str, shift: str, *,
                    admin: bool = False, screen=None):
    """いまの直を7項目に当てた結果(`logic/entry_findings`)。

    **覚えません。** 保存のすぐあとにも描くので、覚えると「直したのに
    帯が減らない」になります。読むのは手元の SQLite の数ページぶんで、
    重いのは停止記号のマスタのほう ── そちらは1リクエストに1度だけ
    引いたものを使い回します(`_stop_choices`)。

    読めなければ「確かめられなかった」を返します ── 「直すところは
    ありません」と出すと、**確かめていないことを確かめたことにする**
    ので、そちらのほうが危ない。
    """
    from nippou.logic import entry_findings
    from nippou.services import shift_check

    try:
        # 帯は**1ページでも保存してから**(これまでどおり)。打ち始めの1文字目から
        # 「休憩が足りません」を出さない
        if not get_repo().saved_pages(report_date, line, shift):
            return entry_findings.none()
        report = shift_check.run(
            get_repo(), report_date, line, shift, admin=admin,
            codes=sorted(presenter.stop_codes(_stop_choices())), screen=screen)
        if not report.pages:
            return entry_findings.none()
        return entry_findings.split(
            [f.as_dict() for f in report.findings], counted=True,
            report_date=report_date, line=line, shift=shift)
    except Exception:                             # noqa: BLE001 - 画面は出す
        log.exception("この直の直すところを数えられませんでした key=%s",
                      (report_date, line, shift))
        return entry_findings.none()


def _sheet_strip(report_date: str, line: str, shift: str, page: int) -> dict:
    """その日・そのラインの紙の束(`logic/sheet_strip`)。

    **読めなくても画面は出します。** 束は「どれを開いているか」を見せる
    ためのもので、これが引けないことを理由に入力を止める筋はありません。
    """
    from nippou.logic import sheet_strip

    try:
        saved = get_repo().saved_keys(line)
    except Exception:                             # noqa: BLE001 - 画面は出す
        log.exception("紙の束を読めませんでした key=%s/%s", report_date, line)
        saved = []
    return sheet_strip.build(saved, report_date=report_date, line=line,
                             current_shift=shift, current_page=page).as_dict()


def _handover(ctx, *, key: tuple[str, str, str], read_only: bool = False):
    """前の直の始末。**判断は `services/handover`。**

    読めなければ素通りさせます ── 関門そのものが画面を開けなくする
    ほうが重い(共有へ未送信の数は、レールの印にも出ています)。

    **1リクエストにつき1度だけ数えます。** 残っている直があると、
    その直の全ページを読んで7項目を走らせます ── 保存の関門と画面の
    描画で2度やると、そのぶんだけ押してから戻るまでが延びます。
    未送信が1つも無いときは `dirty` を1回引くだけで終わります。
    """
    from flask import g

    from nippou.logic.handover import Gate
    from nippou.services import handover

    memo = f"handover:{key}:{read_only}:{ctx.editor}"
    found = getattr(g, memo, None)
    if found is not None:
        return found
    try:
        found = handover.gate(get_repo(), current_key=key, admin=ctx.editor,
                              read_only=read_only)
    except Exception:                             # noqa: BLE001 - 画面は出す
        log.exception("前の直の始末を確かめられませんでした key=%s", key)
        found = Gate()
    setattr(g, memo, found)
    return found


def _opened_key(payload: dict):
    """画面が**自分を何の直だと思っているか**。

    `entry.html` が描かれた時の (報告日, 直, ページ) を、画面がそのまま
    送り返してきます。無ければ空 ── 判断は `logic/shift_boundary` が
    行い、空なら「またいでいない」に倒します(開いた直後・古い画面)。
    """
    from nippou.logic import shift_boundary

    raw = payload.get("opened")
    if not isinstance(raw, dict):
        return shift_boundary.Key()
    try:
        page = int(raw.get("page", 1) or 1)
    except (TypeError, ValueError):
        page = 1
    return shift_boundary.Key(report_date=str(raw.get("report_date", "")),
                              shift=str(raw.get("shift", "")),
                              page=max(1, page))


#: 画面のページと書き先が食い違ったとき、打ちかけ(自動保存・移る前の保存)を
#: 見送る理由。**書かずに 200 で返す**(誰も押していないので騒がない)
PAGE_MOVED_SKIP = "画面のページと書き先が違うので保存しませんでした"

#: **画面の外から書いたページ**と、その時刻(`awake_clock`)。
#:
#: 全停入力は、画面の12行を使わずにサーバが1ページを書きます。書く先が
#: 空のページ(=画面に出ているページ)のこともあるので、ページ番号だけでは
#: 「古い画面」を見分けられません ── 閉じる間際の送信が、打ちかけの空の行を
#: 全停の行の上へ書いていました。**その画面を描いた時刻より後に、画面の
#: 外から書かれたページ**へは、その画面からは書かせません。
_WRITTEN_ASIDE: dict[tuple, float] = {}


def note_written_aside(key: tuple) -> None:
    """画面の12行を通さずにページを書いた(全停入力など)。`screen_mismatch` が見ます。"""
    from nippou import awake_clock

    _WRITTEN_ASIDE[tuple(key)] = awake_clock.now()
    while len(_WRITTEN_ASIDE) > 64:               # 覚えるのは最近のぶんだけ
        del _WRITTEN_ASIDE[next(iter(_WRITTEN_ASIDE))]


def screen_mismatch(ctx, calc, payload: dict, target=None) -> Optional[dict]:
    """画面が出しているページと、**いまの書き先**が食い違っていないか(v4.24.0)。

    【打った行が、別のページに書かれていました】

        打った行が消えることがありました
        打った値が勝手に戻ることがありました

    ── とんでもない話である。書き先(`_target`)はサーバが覚えている1つ
    だけで、**画面がどのページを出しているかを見ていませんでした。**
    比べていたのは直の変わり目(報告日と直)だけです(`_crossing`)。そのため、

        次ページ発行   … 第1ページの12行が、出したばかりの空の第2ページへ
        最新のページに戻る / 帯の「✕」 … 直していた第1ページの中身が、
                         閉じる間際の送信で最新のページへ
        全停入力       … 打ちかけの空の行が、全停の行の上へ

    と、**画面に出ていないページへ、画面の中身が黙って書かれていました。**
    ほかにも同じ処理をしている箇所があると思われるので、比べるのはこの
    1か所にして、画面の中身を受け取る口(保存・発行・決まる値・ロット・印・
    排他・戻る前の保存)はどれもここを通します。

    見分け方:

        ライン       … 名乗っていれば必ず同じでなければならない
        呼出中       … 報告日・直・ページの3つとも、呼び出した紙と同じ
        呼出していない … 報告日と直が同じなのにページだけ違う
                       (報告日か直が違うのは直の変わり目 ── そちらは
                        `_crossing` が「どちらへ入れるか」を訊きます)

    画面が名乗らなければ比べません(開いた直後・古い画面。これまでどおり)。
    食い違っていれば、その中身(言うこと・画面のキー・書き先)を返します。
    """
    opened = _opened_key(payload)
    if not opened.filled:
        return None
    report_date, line, shift, page = target or _target(ctx, calc)
    raw = payload.get("opened") if isinstance(payload.get("opened"), dict) else {}
    seen_line = str(raw.get("line", "") or "").strip()
    differs = bool(seen_line) and seen_line != line
    if ctx.recall.active:
        differs = differs or ((opened.report_date, opened.shift, opened.page)
                              != (report_date, shift, page))
    elif (opened.report_date, opened.shift) == (report_date, shift):
        differs = differs or opened.page != page
    where = f"{opened.report_date} {opened.shift} 第{opened.page}ページ"
    now = f"{report_date} {shift} 第{page}ページ"
    found = {
        "opened": {**opened.as_dict(), "line": seen_line},
        "target": {"report_date": report_date, "line": line, "shift": shift,
                   "page": page},
    }
    if differs:
        found["message"] = (
            f"この画面は {where} を出していますが、書き先は {now} に"
            "変わっています。別のページへ書かないよう、保存しませんでした。"
            "画面を読み直してください(打ちかけは、出ているページを開き直して"
            "打ち直してください)")
        return found
    # 同じページでも、**この画面を描いたあとに画面の外から書かれていれば**古い
    # (全停入力。`note_written_aside`)。描いた時刻を名乗らない画面は比べない
    written = _WRITTEN_ASIDE.get((report_date, line, shift, page))
    try:
        loaded = float(raw.get("loaded")) if raw.get("loaded") not in (None, "") else None
    except (TypeError, ValueError):
        loaded = None
    if written is not None and loaded is not None and loaded < written:
        found["message"] = (
            f"{now} は、この画面を開いたあとに全停入力などで書き直されています。"
            "上書きしないよう、保存しませんでした。画面を読み直してください")
        return found
    return None


def page_moved_refusal(found: dict, *, silent: bool = False):
    """`screen_mismatch` の答えを応答にする。**どちらも書いていない。**

    打ちかけ(`silent`)は 200 で見送る ── 押した人がいないので騒がない。
    押した操作は 409(ぶつかった)── 送られたものは正しく、画面が古いだけ。

    **画面ぜんぶは返しません。** 返すと、画面は書き先のキーと**古いページの
    中身**を一緒に塗り、次の保存でそのまま書き先へ送ってしまいます。
    """
    if silent:
        return jsonify({"saved": False, "skipped": PAGE_MOVED_SKIP,
                        "page_moved": found}), 200
    body = error_body("page_moved", found["message"])
    body["saved"] = False
    body["page_moved"] = found
    return jsonify(body), 409


def _screen_key(ctx, calc, payload: dict):
    """決まる値を返すときに描く紙(キー)。**直の変わり目なら、画面が開いた直のまま。**

    `None` ならいつもどおり書き先(`_target`)で描きます。

    書き先で描くと、17:00 をまたいだ画面へ「2直」のキーが返り、画面は
    自分を2直だと思い直します ── 次の保存は「またいだ」と見なされず、
    1直のつもりで打った行が黙って2直へ入ります。どちらへ入れるかを訊く
    のは保存(`save`)なので、それまでは開いた紙のまま描きます。
    """
    if ctx.recall.active:
        return None
    opened = _opened_key(payload)
    if not opened.filled:
        return None
    report_date, line, shift, _page = _target(ctx, calc)
    if (opened.report_date, opened.shift) == (report_date, shift):
        return None
    return (opened.report_date, line, opened.shift, opened.page)


def _crossing(ctx, payload: dict, report_date: str, shift: str, page: int,
              current_date: str, current_shift: str):
    """直の変わり目をまたいだか。**2つの道を1つの判断に寄せる。**

        呼出していない … 画面が開いた直 と いま書こうとしている直 を比べる
        当直のページ移動中 … 開いたページの直 と 時計の直 を比べる
                         (`_target` は呼出のキーを返すので、画面と比べても
                          必ず一致してしまう ── 時計と比べなければならない)

    わざわざ過去を開いている場合(`was_current=False`)は**ここでは見ません。**
    その人は他の直を開いていると分かっていて開いているので、断るなら
    これまでどおり `recall_refusal` の 403 です。
    """
    from nippou.logic import shift_boundary

    current = shift_boundary.Key(current_date, current_shift,
                                 get_repo().latest_page(
                                     current_date, ctx.line, current_shift) or 1)
    if ctx.recall.active:
        if not ctx.recall.was_current:
            return shift_boundary.Crossing(opened=current, current=current)
        opened = shift_boundary.Key(ctx.recall.report_date, ctx.recall.shift,
                                    ctx.recall.page)
        return shift_boundary.check(opened, current)
    opened = _opened_key(payload)
    # 画面が何も言ってこないときは、いま書こうとしている先を写しておく
    # (またいでいない扱い)
    if not opened.filled:
        return shift_boundary.Crossing(
            opened=shift_boundary.Key(report_date, shift, page),
            current=current)
    return shift_boundary.check(opened, current)


def _backfill_view(ctx, report_date: str, line: str, shift: str,
                   page: int) -> Optional[dict]:
    """後から作っている(作った)枠なら、その印。そうでなければ None。

    呼出中だけ見ます ── 後から作る枠はいつも「記録を見る」から呼出の形で
    開くので(`/api/settings/backfill`)、いまの直を打っているときには出ません。
    """
    if not ctx.recall.active:
        return None
    from nippou.logic import backfill as rule
    try:
        found = get_repo().backfill_opened(report_date, line, shift, page)
    except Exception:                             # noqa: BLE001 - 帯が出ないだけ
        log.exception("後日作成の印を読めませんでした")
        return None
    if found is None:
        return None
    return {"stamp": rule.stamp_text(found),
            "key": f"{report_date} {_label(line)} {shift} 第{page}ページ"}


def _recall_is_other_shift(ctx, calc) -> bool:
    """開いているのが**他の直**か(同じ直のページ移動なら False)。

    言葉を分けるためだけに使います ── 同じ直のページ1を開いているのに
    「過去データを開いています」と出すと、自分がさっき打った紙を
    直しているだけなのに、触ってはいけないものに見えます。
    """
    # **時計から決まる直と比べる**(判定は `WorkContext.recall_is_this_shift`。
    # 帯の「第Nページを直し中」も同じものを見る)
    return ctx.recall.active and not ctx.recall_is_this_shift(calc)


def anchor_standing(ctx, calc, now: Optional[datetime] = None):
    """固定した直が、いまどうなっているか(`logic/shift_anchor.decide`)。

    固定していなければ「何も無い」が返るので、呼ぶ側は分岐せずに使えます。
    """
    from nippou.logic import shift_anchor

    now = now or datetime.now()
    if not ctx.anchor.filled:
        return shift_anchor.Standing()
    business = parse_business_date(ctx.anchor.report_date)
    end = (calc.shift_end_at(ctx.anchor.shift, business)
           if business is not None else None)
    return shift_anchor.decide(
        ctx.anchor, now=now, shift_end=end,
        grace_minutes=SETTINGS.shift_grace_minutes)


def anchor_on_worker(ctx, calc, state, now: Optional[datetime] = None) -> bool:
    """作業者が決まったら、**その瞬間の時計の直に固定する。**

    直の始まりは「作業者を選ぶこと」(v3.54.0)。その1つの操作で、
    **どの直へ書くかも決まる**ようにします ── 決めずに時計へ任せると、
    15:00 をまたいだ瞬間に書き先だけが動きます。

    15:05 に選んだなら 2直 です。迷いがありません。15:00 を過ぎてから
    1直の続きを打つのは「過ぎた直を直す」話なので、「記録を見る」から
    呼び出す道を通します。

    **呼出中は固定しません** ── 過去の紙を開いているだけなので。
    ラインが変わっていたら取り直します(別の紙になるため)。
    """
    from nippou.logic import shift_anchor

    if ctx.recall.active:
        return False
    if not str(state.header.get("worker", "")).strip():
        return False
    now = now or datetime.now()
    shift = calc.time_check(now, ctx.force_day_shift())
    if ctx.anchor.filled and not shift_anchor.should_restart(
            ctx.anchor, ("", ctx.line, shift)):
        return False

    report_date = format_business_date(
        calc.today_check(now, ctx.force_day_shift()))
    ctx.anchor = shift_anchor.start_key((report_date, ctx.line, shift), now)
    log.info("直を固定しました: %s %s %s (作業者が決まった時点)",
             report_date, ctx.line, shift)
    return True


def _label(line: str) -> str:
    """画面に出すライン名(正規の呼び名。v4.12.5)。"""
    from nippou.logic import line_names
    return line_names.label(line)


def line_refusal():
    """**この端末のラインが決まるまで、日報を書く操作は断る**(v4.3.0)。

    決まっていれば `None`。決まっていなければ 409 の応答を返すので、
    呼び手はそのまま返します(次ページ発行・全停入力など、押した時点で
    1ページを書くもの)。保存は画面ぜんぶを返したいので `save` が自分で
    断ります(同じ判定 `work_context.terminal_line_decided`)。
    """
    if work_context.line_known():
        return None
    return jsonify(error_body("line_undecided", work_context.LINE_GATE_MESSAGE)), 409


def _is_read_only(ctx, calc) -> bool:
    """**見るだけの画面か。**

    2つあります:

    1. 他の直の過去データを、管理者モードでないまま開いている
    2. **固定した直の終わりの時刻を過ぎた**(`logic/shift_anchor`)

    2 を入れた理由。終わりの時刻を過ぎても打てたままにすると、忘れて
    立ち去った人の紙が開いたまま残り、**次に座った人がその続きを打つ
    羽目になります** ── それは頼めません。終わりの時刻ちょうどで閉じて、
    次の人は「作業者を選ぶ」から自分の直を始めます。

    条件は保存を断る条件(`/api/entry/save` の 403)と同じもので、
    **判定を2か所に置かない**ためにここ1つにまとめてあります。

    こうなるのは押し間違いだけではありません ── 管理者が過去の直を
    開いたまま直の変わり目をまたぐと、管理者モードは自動で外れ
    (`work_context.roll_over`)、呼出モードは**わざと残ります**
    (消すと保存先が黙って動くため)。誰も何も操作していないのに、
    この状態になります。
    """
    return bool(_read_only_reason(ctx, calc))


def _read_only_reason(ctx, calc) -> str:
    """**なぜ見るだけなのか。** 通せていないなら空。

    理由で出口が変わります ── 過去データを開いているなら「最新のページへ
    戻る」、直の時間が過ぎたなら「次の直を始める」。**理由を出さずに
    閉じると、どちらを押せばよいのか画面から読めません。**
    """
    if ctx.recall.active and not ctx.editor and _recall_is_other_shift(ctx, calc):
        return "recall"
    # **直しに来ている人は閉じません。**(`_fixing_past`)
    if _fixing_past(ctx):
        return ""
    # 過ぎた直を直すのは「記録を見る」から呼び出す道で、こちらは
    # 「いま打っている紙」の画面です
    if anchor_standing(ctx, calc).expired:
        return "anchor"
    return ""


def _fixing_past(ctx) -> bool:
    """**過去を直しに来ているか**(呼出モード + 管理者)。

    【ここが抜けていて、直す道が塞がっていました】

        直すために 記録を見るから 呼び出し 検入枚数を10に直して
        保存（確定）してもこれでよかったのかがわからない
        …は出たままだし

    出たままになるのは当然で、**保存そのものが通っていませんでした**。
    「終わった直へは打てない」(v3.57.1)の関門が、**過去を直しに来た
    画面にも当たっていた**からです。

    前の直の帯は「前の直の間違いは、この画面からは直せません」と書き、
    直す道として「記録を見る → 呼び出す」を指しています。その道の先で
    同じ関門に当たれば、**どこからも直せません。**

    呼び出しそのものが管理者モードでなければ通らない関門なので、ここまで
    来た人は「過去のページを直しに来た人」です。固定(いま打っている紙が
    どの直のものか)は、その人には当てはまりません。
    """
    return bool(ctx.recall.active and ctx.editor)


def prev_shift_warning(ctx, calc):
    """前の直が空かどうか。**タイマーで拾えなかったぶんの網。**

    催促も自動確定も画面が開いていないと動きません。抜けた直は次の直が
    気づくしかないので、ここで一度だけ知らせます。判断そのものは
    `logic/prev_shift.py` が持ち、ここは**事実を集めて渡すだけ**です。

    集めるのに失敗しても画面は出します ── 警告が出ないだけ。
    """
    from nippou.logic import prev_shift as prev

    report_date, line, shift = ctx.current_key(calc)
    try:
        business_date = ctx.business_date(calc)
        found = prev.previous_of(shift, business_date)
        if found is None:
            return prev.EmptyPrevShift(line=line)
        prev_name, prev_date = found
        prev_report_date = format_business_date(prev_date)
        return prev.evaluate(
            report_date=report_date, line=line, shift=shift,
            prev_report_date=prev_report_date, prev_shift=prev_name,
            prev_has_data=bool(get_repo().saved_pages(
                prev_report_date, line, prev_name)),
            seen_recently=_shift_seen_recently(line, prev_name, business_date),
            recall_mode=ctx.recall.active)
    except Exception:                             # noqa: BLE001 - 画面は出す
        log.exception("前直の警告を組み立てられませんでした %s/%s/%s",
                      report_date, line, shift)
        return prev.EmptyPrevShift(line=line)


def _shift_seen_recently(line: str, shift: str, business_date) -> bool:
    """そのラインは、最近その直を回しているか。

    **回していない直の「空」を知らせても狼少年になります。** 3直の無い
    ラインで毎朝「3直が空です」と出せば、本当に抜けた日にも読まれません。
    最近の保存を1件でも見つけたら「回している」と見なします。
    """
    from nippou.logic import prev_shift as prev
    from nippou.logic.shift import parse_business_date

    since = prev.seen_since(business_date)
    for row in get_repo().saved_keys(line):
        if row["shift"] != shift:
            continue
        day = parse_business_date(str(row["report_date"]))
        if day is not None and day >= since:
            return True
    return False


def current_flow(report_date: str, line: str, shift: str, *,
                 recall: bool = False, worker=None, rows_used=None):
    """直ひと回りの帯。**3つの画面が同じものを読む。**

    残り時間を渡すのは、**直の終わりの2つを早すぎる時刻に「次」と
    言わない**ため(`presenters/flow.py`)。時計が読めなければ渡さない
    ── そのときは順番どおりに出ます。
    """
    from nippou.presenters import flow

    ctx = work_context.get_context()
    minutes_left = None
    try:
        left = current_calculator().minutes_until_shift_end(
            datetime.now(), ctx.force_day_shift())
        minutes_left = left if left >= 0 else None
    except Exception:                             # noqa: BLE001 - 帯は出す
        log.exception("動線の帯: 残り時間を読めませんでした")

    return flow.build(get_repo(), report_date, line, shift,
                      worker=worker, rows_used=rows_used, recall=recall,
                      minutes_left=minutes_left,
                      warn_minutes=SETTINGS.print_warning_minutes)


def _frequent_stops(line: str, choices: list[dict]) -> list[dict]:
    """停止記号の一覧の上に並べる「よく使う」(このラインの最近の保存から)。

    **読めなくても画面は出す。** 並ばないだけで、一覧そのものはいつもどおり。
    """
    from nippou.logic import input_shortcuts

    texts = {r["code"]: r["text"] for group in choices for r in group["reasons"]}
    if not texts:
        return []
    try:
        used = get_repo().recent_stop_codes(line)
    except Exception:                             # noqa: BLE001 - 並ばないだけ
        log.exception("よく使う停止記号を数えられませんでした line=%s", line)
        return []
    return [{"code": code, "text": texts[code]}
            for code in input_shortcuts.frequent_codes(used, texts)]


def _recalculate(state, ctx, calc, *, shift: str = ""):
    """決まる値を決める。**時間計算の断りを返す。**

    直の規定時間(`GetShiftLimitMin`)は時間マスタから引く。管理者モード中は
    全直の最大を使う ── 他の直のデータを開いて精査することがあるため。

    `shift` を渡すとその直の規定時間で見ます。直の変わり目をまたいで
    「打っていた直へ入れる」を選んだとき、**書く先と同じ直の規定時間で
    見なければ**、直の終わり間際の行が理由もなく赤くなります。
    """
    if not shift:
        _, _, shift, _ = _target(ctx, calc)
    return presenter.recalculate(
        state, package_calc=ctx.package_calc_enabled(),
        shift_times=get_repo().get_shift_times(),
        current_shift=shift, admin=ctx.editor)


def _bad_rows(state, ctx, calc, *, shift: str = "") -> list[dict]:
    """**直っていない行、ぜんぶ。** 印を付けるのと、保存を止めるのに使う。

    打つのは自由です。止めるのは「保存(確定)」だけ ── 打っている途中で
    手が止まるほうが困るので、**間違ったまま残らないこと**のほうを
    関門にしています。

    `shift` の意味は `_recalculate` と同じ(書く先と同じ直で見る)。
    """
    if not shift:
        _, _, shift, _ = _target(ctx, calc)
    return [{"row": p.row, "reason": p.reason, "message": p.message,
             "field": p.field, "sound": p.sounds}
            for p in presenter.time_problems(
                state, shift_times=get_repo().get_shift_times(),
                current_shift=shift, admin=ctx.editor)]


# ------------------------------------------------------------------
# 画面
# ------------------------------------------------------------------
@bp.get("/")
def index():
    ctx = work_context.get_context()
    calc = current_calculator()
    report_date, line, shift, page = _target(ctx, calc)
    # **読む前に**時刻を取る。読んだあとに取ると、そのあいだに別のタブが
    # 書いた行が「この画面より前」に数えられ、古い表が新しく見えます
    # (`logic/tab_lock.py`「引き継いだタブの画面は古い」)
    from nippou import awake_clock
    tab_loaded_at = awake_clock.now()
    state = _load_state(report_date, line, shift, page)
    choices = presenter.stop_choices()
    others = presenter.other_stop_codes(choices)
    from nippou.logic import input_shortcuts
    enter = input_shortcuts.enter_targets(
        c.family for c in presenter.COLUMNS if c.kind != "calc")

    return render_template(
        "entry.html",
        columns=presenter.COLUMNS,
        column_groups=presenter.column_groups(),
        # 紙の上(作業者名)と下(直実績合計)に分かれている。**同じ並びを
        # 画面でも使う** ── 上に合計を置くと「まだ打っていないのに
        # 枚数欄がある」ように見える
        top_fields=presenter.TOP_FIELDS,
        # 合計欄。**機側・NS1 は ﾛｯﾄ数・係数Lot数、AIM は ﾛｯﾄ数 も計算で決まる**
        total_fields=presenter.total_fields(line),
        lot_count_auto=load_factor.applies(line),
        coefficient_auto=load_factor.counts_coefficient(line),
        reason_field=presenter.REASON_FIELD,
        stop_pairs=presenter.STOP_PAIRS,
        stop_slot_of=presenter.STOP_SLOT_OF,
        row_count=constants.ROW_COUNT,
        lines=constants.LINE_NAMES,
        # **入力制限はサーバが決める**(`logic/input_rules.py`)。画面は
        # 欄の属性として受け取って、そのとおりに振る舞うだけ
        input_attrs=input_rules.as_attributes,
        # 停止理由の記号は「打つ」ではなく「選ぶ」。伝送用ファイルの
        # 作業停止時間内訳_1/_2/_3 から引く
        stop_choices=choices,
        stop_codes=presenter.stop_codes(choices),
        # **入力の近道**(v4.7.0 `logic/input_shortcuts`)── 一覧の上の
        # 「よく使う」・欄の簡易説明(マウスを乗せると出る)・Enter の行き先
        frequent_stops=_frequent_stops(line, choices),
        input_hint=input_shortcuts.hint,
        enter_attrs=lambda family: input_shortcuts.enter_attributes(enter.get(family)),
        shift_end=_shift_end_text(calc, shift),
        # etc欄の押しボタン。**文言も並びもサーバが持つ**
        marks=etc_marks.choices(),
        stop_code_families=presenter.STOP_CODE_FAMILIES,
        # 紙にも表にも出ないが、行が持って回る欄(印刷範囲外の6つ+引当番号)。
        # **画面に置き場所が無いと、次に送るときに消える**
        extra_families=presenter.EXTRA_FAMILIES,
        interleaf_choices=presenter.INTERLEAF_CHOICES,
        view=_view(state, ctx, calc, other_codes=others),
        # 保存を押すとここへ集計CSVが出ます。**押す前に見えるところへ**
        # ── グラフ画面と同じ `_out_dir.html` を使います
        out_dir=settings_view.output_dir_view(),
        maru_sub=ctx.maru_sub,
        tab_loaded_at=f"{tab_loaded_at:.3f}",
        **shell.shell_context("entry", ribbon=ctx.ribbon(calc, page)))


# ------------------------------------------------------------------
# API
# ------------------------------------------------------------------
@bp.post("/api/entry/state")
def update_state():
    """入力内容を受け取り、**決まる値を決めて**返す。

    `row` が付いていれば、その行の終了時刻を次の行へ引き継ぐかどうかも
    判定する(`Same_Text`)。保存はしない。
    """
    payload = request.get_json(silent=True) or {}
    ctx = work_context.get_context()
    calc = current_calculator()
    # **古いページの中身で、いまのページの値を決めない**(v4.24.0)。決めた値と
    # 一緒に書き先のキーが返ると、画面は古い12行を新しいページのものだと
    # 思い直し、次の自動保存でそのまま書きます(`screen_mismatch`)
    moved = screen_mismatch(ctx, calc, payload)
    if moved:
        return page_moved_refusal(moved)
    key = _screen_key(ctx, calc, payload)

    state = presenter.parse_state(payload)

    # **作業者が決まった時点で、書き先の直を固定する。**
    #
    # 作業者は「作業者を選ぶ」窓からでも、欄へ直に打ってでも決まります。
    # どちらも最後はここ(`settle`)を通るので、固定するならここ1か所です。
    anchor_on_worker(ctx, calc, state)

    # **入力の近道**(v4.7.0 `logic/input_shortcuts`)。重量を聞くかどうかの
    # 判定より先に入れる ── 写した枚数で重量が変わるなら、打ったときと
    # 同じように聞きます
    copied, filled = _apply_shortcuts(state, payload)

    # --- 重量を計算し直す前に、一声かける ---
    #
    # VBA は重量が入っている行を触りませんでした(現物を量った値のほうが
    # 正しいため)。そのぶん、枚数を打ち直しても重量は前のまま ──
    # **食い違ったまま保存できます。** 黙って上書きも、黙って据え置きも
    # 重すぎるので聞きます(`calculations.weight_needs_asking`)。
    ask = _weight_question(state, payload)
    if payload.get("redo_weight"):
        # 「はい」で戻ってきた。**その行の重量を空にしてから**計算させる
        # ── `apply_weights` は空の行だけ埋めるので、これで入り直す
        for row_no in payload.get("redo_weight") or []:
            values = state.rows.get(int(row_no))
            if values is not None:
                values["WEI"] = ""
        ask = None

    # **ダブルクリックで、いまの時刻を時と分の2欄へ**(`logic/work_time.stamp`)。
    # 計算し直す前に入れる ── 作業時間・引き継ぎも、打ったときと同じに通す
    stamped = _apply_stamp(state, ctx, payload.get("stamp"), calc=calc)

    problem = _recalculate(state, ctx, calc, shift=key[2] if key else "")

    marks = None
    row = payload.get("row")
    if isinstance(row, int) and 1 <= row <= constants.ROW_COUNT:
        _, _, shift, _ = key or _target(ctx, calc)
        # **値だけ運ぶ。焦点は動かさない。**
        #
        # VBA `Same_Text` は `KZ(num+1)` `KH(num+1)` に文字を入れるだけで、
        # 焦点はその行に残していました。Web版はここで `KZ{row+1}` へ
        # 飛ばしていて、**同じ行の梱包数・重量・実働・停止がまだ空なのに
        # 下の行へ連れて行かれる**状態でした。打つ順は紙のとおり
        # 「1行を左から右へ」なので、`FOCUS_CHAIN` に任せます。
        #
        # 直の終わりの時刻を打ったときは写さず、**前に写したぶんが
        # 残っていれば引っ込めます**(`logic/navigation.carry_decision`)。
        marks = presenter.carry_end_time(
            state, row, day_temp=shift, shift_end_times=_shift_end_times(calc),
            carried_rows=_carried_rows(payload))

    body = _view(state, ctx, calc, problem=problem, key=key)
    if marks is not None:
        # **写した印を画面へ返す。** 画面はこれを持ち直して、次に送って
        # きます ── 「ツールが入れた値が、まだ人に触られずに残っている
        # 行」が分かるのはこの往復だけです(`logic/navigation.carry_decision`)
        body["carried"] = sorted(marks)
    if ask:
        # 画面はこれを見て聞く。**聞くかどうかを決めるのはサーバ**で、
        # 画面は文言を出して「はい」を送り返すだけ
        body["weight_ask"] = ask
    if stamped is not None:
        body["stamp"] = stamped
    if copied is not None:
        body["copied"] = copied
    if filled:
        # 画面はこの欄を一瞬光らせる(**入ったことが見えるように**)
        body["filled"] = filled
    return jsonify(body)


def _apply_shortcuts(state, payload: dict) -> tuple[dict | None, list[dict]]:
    """「上の行と同じ」と「包数が空なら 1」(`logic/input_shortcuts`)。

    返すのは(写した結果, 自動で入れた欄)。どちらも**押した・離れた**とき
    だけで、黙って先回りはしません。
    """
    from nippou.logic import input_shortcuts

    copied = None
    filled: list[dict] = []
    ask = payload.get("copy_above")
    if isinstance(ask, dict):
        try:
            row = int(ask.get("row"))
        except (TypeError, ValueError):
            row = 0
        result = input_shortcuts.copy_above(state.rows, row)
        if result.ok:
            for family, value in result.values.items():
                state.set(row, family, value)
        copied = result.as_dict()

    row = payload.get("row")
    changed = str(payload.get("changed", ""))
    if isinstance(row, int) and 1 <= row <= constants.ROW_COUNT:
        packs = input_shortcuts.default_packs(state.rows.get(row, {}), changed)
        if packs is not None:
            state.set(row, "TUT", packs)
            filled.append({"row": row, "family": "TUT", "value": packs})
    return copied, filled


def _clock_now() -> datetime:
    """いまの時刻(試験が差し替える口)。"""
    return datetime.now()


#: 「直の終わり」を押したときの `stamp.which`
SHIFT_END_STAMP = "shift_end"


def _stamp_shift_end(state, ctx, calc, row: int) -> dict:
    """「直の終わり」── **開いている直の**終わりの時刻を、終了の時と分へ。

    いまの時刻ではないので、過去の直を開いていても入れます(その直の
    終わりの時刻は、その直のもの)。直の終わりの時刻を打ったのと同じ
    なので、次の行の開始へは写りません(`navigation.same_text`)。
    """
    from nippou.logic import input_shortcuts, work_time

    _, _, shift, _ = _target(ctx, calc)
    found = input_shortcuts.shift_end(shift, _shift_end_times(calc))
    if found is None:
        return {"ok": False, "row": row, "which": SHIFT_END_STAMP,
                "message": f"{shift}の終わりの時刻が分かりません。"
                           f"{row}行目の終了は打ってください。"}
    hour_family, minute_family = work_time.STAMP_FIELDS["end"]
    state.set(row, hour_family, found[0])
    state.set(row, minute_family, found[1])
    return {"ok": True, "row": row, "which": SHIFT_END_STAMP,
            "text": f"{found[0]}:{found[1]}", "message": ""}


def _shift_end_text(calc, shift: str) -> str:
    """その直の終わりの時刻(`15:00` の形)。分からなければ空。"""
    from nippou.logic import input_shortcuts

    found = input_shortcuts.shift_end(shift, _shift_end_times(calc))
    return f"{found[0]}:{found[1]}" if found else ""


def _apply_stamp(state, ctx, stamp, *, calc=None) -> dict | None:
    """開始・終了の時/分をダブルクリックされた。**いまの時刻を2欄へ。**

        開始終了の時間テキストボックスをダブルクリックするとそのタイミングの
        時間を入力するようにしてください 時間 分 両方入れてください

    分の1の位は 0 か 5 に寄せます(1〜4切り捨て・5そのまま・6〜9切り上げ。
    `logic/work_time.stamp`)。**時計はサーバのもの**(画面と同じPC)。

    **過去の直を開いているあいだは入れません** ── いまの時刻は、その直の
    時刻ではないので(後から作った前日の1直に、今日の時刻が入ります)。
    そのときは何も変えずに理由を返します。
    """
    from nippou.logic import work_time

    if not isinstance(stamp, dict):
        return None
    which = str(stamp.get("which", ""))
    try:
        row = int(stamp.get("row"))
    except (TypeError, ValueError):
        return None
    if which == SHIFT_END_STAMP and 1 <= row <= constants.ROW_COUNT and calc is not None:
        return _stamp_shift_end(state, ctx, calc, row)
    if which not in work_time.STAMP_FIELDS or not 1 <= row <= constants.ROW_COUNT:
        return None
    label = "開始" if which == "start" else "終了"
    if ctx.recall.active:
        return {"ok": False, "row": row, "which": which,
                "message": (f"過去の直を開いているあいだは、ダブルクリックでいまの"
                            f"時刻は入れません。{row}行目の{label}は打ってください。")}
    hour, minute = work_time.stamp(_clock_now())
    hour_family, minute_family = work_time.STAMP_FIELDS[which]
    state.set(row, hour_family, hour)
    state.set(row, minute_family, minute)
    return {"ok": True, "row": row, "which": which, "text": f"{hour}:{minute}",
            "message": ""}


def _weight_question(state, payload: dict):
    """「重量を変更しますか？」と聞くべきか。聞くなら中身を返す。

    直した欄(`changed`)が個装単位 枚数か梱包単位 包数で、その行の重量に
    すでに値があり、計算し直すと違う値になるとき**だけ**聞きます
    (`calculations.weight_needs_asking` に、それぞれの理由があります)。
    """
    from nippou.logic import calculations

    row = payload.get("row")
    changed = str(payload.get("changed", ""))
    if not isinstance(row, int) or not (1 <= row <= constants.ROW_COUNT):
        return None
    fresh = calculations.weight_needs_asking(state.rows, row, changed)
    if not fresh:
        return None
    return {
        "row": row,
        "now": (state.rows.get(row, {}).get("WEI") or "").strip(),
        "next": fresh,
        "message": (f"{row}行目の重量を {fresh} に変更しますか？\n"
                    f"(いまは "
                    f"{(state.rows.get(row, {}).get('WEI') or '').strip()})"),
    }


@bp.post("/api/entry/check")
def toggle_check():
    """チェックボックスの排他制御(`HdCh_排他制御` / `CheckBox_排他制御`)。"""
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name", ""))
    known = set(constants.HDCH_NAMES) | set(constants.DETAIL_CHECKBOX_NAMES)
    if name not in known:
        return jsonify(error_body("bad_check", "そのチェックはありません")), 400

    ctx = work_context.get_context()
    calc = current_calculator()
    moved = screen_mismatch(ctx, calc, payload)
    if moved:
        return page_moved_refusal(moved)
    state = presenter.parse_state(payload)
    state = presenter.apply_exclusion(state, name)
    log_button_click(f"exclusion:{name}", line=ctx.line)
    return jsonify(_view(state, ctx, calc, key=_screen_key(ctx, calc, payload)))


def build_service(ctx, calc) -> NippouService:
    """このリクエスト用のサービス。**状態は入れものから借りる。**

    呼出モードと最後の自動保存時刻は `work_context` が持つので、
    要求ごとに作り直しても忘れない(`ServiceState` の説明)。
    """
    return NippouService(get_repo(), calc, is_admin=lambda: ctx.editor,
                         confirm_other_shift_edit=lambda *a: True,
                         state=ctx.service_state)


@bp.post("/api/entry/save")
def save():
    """保存する(`NippouDB_Save` / `NippouDB_AutoSave` 相当)。

    LOT重複は**保存する前に**断る。重複したまま保存すると後段の集計が
    壊れる。

    `silent=true` は自動保存 ── **保存してよいかはサーバが決める**
    (`NippouService.autosave_reason`)。間引きの間隔も、呼出モード中に
    他の直へ書かないことも、画面には判断できない。応答の `saved` で
    「実際に保存したか」を返す。

    【3つめ: `draft=true` ── 打ちかけを置いて、どこかへ移る】
    ページを切り替える前に呼ばれます。**確定ではありません。**

    保存前チェック(7項目)も、行ごとの時間の断りも通しません ── 通すと
    **1ページ目に間違いがあるだけで1ページ目を見に行けなくなります。**
    直すために開きたいのに、直っていないから開けない。

    間引き(`autosave_reason`)も外します。自動保存と違って、これは
    「移る前に1度だけ」なので、待たされると打ちかけが消えます。

    止めるのは確定のほう ── 「保存(確定)」と「共有へ保存」です。
    """
    payload = request.get_json(silent=True) or {}
    # 打ちかけとして置くだけ。**確定ではないので関門を通さない**
    draft = bool(payload.get("draft"))
    silent = bool(payload.get("silent")) or draft
    ctx = work_context.get_context()
    calc = current_calculator()
    service = build_service(ctx, calc)

    from nippou.logic import shift_boundary

    state = presenter.parse_state(payload)
    # **この端末のラインが決まるまで書かない**(v4.3.0)。画面は伏せて
    # ありますが(`needs_line`)、断るのはここです ── 古い画面や別の
    # タブから来たぶんまで止めないと、別のラインの日報が L1 に混ざります。
    # 自動保存・打ちかけは黙って見送ります(誰も押していないので)
    if not work_context.line_known(ctx):
        body = _view(state, ctx, calc)
        body["saved"] = False
        if silent:
            body["skipped"] = "この端末のラインが決まっていないため保存しません"
            return jsonify(body)
        body["message"] = work_context.LINE_GATE_MESSAGE
        return jsonify(body), 409
    # **画面に出ていないページへは書かない**(v4.24.0 `screen_mismatch`)。
    # 何かを決める前・固定を動かす前に見ます ── 古い画面の送信で、書き先の
    # 直まで動かさないために
    moved = screen_mismatch(ctx, calc, payload)
    if moved:
        log.warning("画面のページと書き先が違うので保存しませんでした %s → %s (%s)",
                    moved["opened"], moved["target"],
                    "打ちかけ" if silent else "押した保存")
        return page_moved_refusal(moved, silent=silent)
    # **保存が最初の1手のこともある。** 作業者を打ってそのまま
    # 「保存(確定)」を押されると `settle` を通らないので、ここでも見ます
    # ── 固定は「作業者が決まった最初の1回」だけ働きます
    anchor_on_worker(ctx, calc, state)
    report_date, line, shift, page = _target(ctx, calc)
    now = datetime.now()
    current_shift, current_date = service.current_shift_info(
        now, ctx.force_day_shift())

    # **直を固定してあるなら、またぎの聞き返しは起きません。**
    #
    # 「どちらの直へ入れますか」は、書き先が時計まかせだったから必要
    # でした ── 15:00 をまたいだ瞬間に書き先が動くので、打っている人に
    # 訊くしかなかった。固定してあるなら書き先は動かないので、訊くことが
    # ありません(`logic/shift_anchor.py`)。
    #
    # 比べる相手を「時計の直」から「書き先の直」に替えます。
    if ctx.anchor.filled and ctx.anchor.line == line and not ctx.recall.active:
        current_date, current_shift = report_date, shift

    # --- 開いたままで直の変わり目をまたいでいないか ---
    #
    # **どこへ書くかが決まる前に、ほかの関門を通さない。** LOT重複も
    # 時間の断りも「その直として見て」判定するので、直が決まっていない
    # うちに走らせると、書く先とは違う直の規定時間で行を赤くします。
    #
    # VBA は保存処理の最後で `Unload UFdaily` していたので、フォームが
    # 17:00 をまたぐことがまずありませんでした。開きっぱなしにできる
    # こちらは、ここで確かめるしかありません
    # (`logic/shift_boundary.py` に理由を書いてあります)。
    crossing = _crossing(ctx, payload, report_date, shift, page,
                         current_date, current_shift)
    choice = str(payload.get("shift_choice", ""))
    if crossing.crossed:
        # **打ちかけでも、人が選んだのなら選んだ直へ置く**(v4.24.0)。移る前・
        # 読み直す前の保存は「どちらへ入れるか」を訊いてから、選ばれた答えを
        # 添えて打ちかけのまま置きます ── 確定(7項目の関門)に通すと、直して
        # いない行が1つあるだけで置けず、読み直しで打ちかけが消えていました
        if silent and not (draft and choice in shift_boundary.CHOICES):
            # **自動保存は黙って見送る。** 誰も押していないところに
            # 聞き返しを出しても選べませんし、どちらかへ勝手に書けば
            # それこそ黙って間違った直に入ります。気づかせる役は
            # 1分ごとの見張り(`/api/shift/key`)が持ちます
            key = (crossing.opened.report_date, line,
                   crossing.opened.shift, crossing.opened.page)
            body = _view(state, ctx, calc, key=key,
                         problem=_recalculate(state, ctx, calc,
                                              shift=crossing.opened.shift))
            body["saved"] = False
            body["skipped"] = "直が変わったため自動保存を止めています"
            body["shift_changed"] = crossing.as_dict()
            return jsonify(body)
        chosen = shift_boundary.resolve(crossing, choice)
        if chosen is None:
            # **409。** 形は正しく、業務としても通せるが、どちらへ通すかが
            # まだ決まっていない ── 押した人に選んでもらう。
            # 画面は**開いていた直のまま**返す(勝手に飛ばさない)
            key = (crossing.opened.report_date, line,
                   crossing.opened.shift, crossing.opened.page)
            body = _view(state, ctx, calc, key=key,
                         problem=_recalculate(state, ctx, calc,
                                              shift=crossing.opened.shift))
            body["message"] = crossing.message
            body["shift_changed"] = crossing.as_dict()
            body["saved"] = False
            return jsonify(body), 409
        report_date, shift, page = chosen.report_date, chosen.shift, chosen.page
        if choice == shift_boundary.CHOICE_CURRENT and ctx.recall.active:
            # 今の直を選んだのなら、開いていたページからは離れる。**離さないと
            # 次の保存でまた同じことを訊かれます**
            service.clear_recall_mode()
        # **選ばれた直を、そのまま固定にする。**
        #
        # 選ぶという操作は「ここへ書きます」と言うことです。固定を
        # 置きっぱなしにすると、次の保存で**選んだ先と固定が食い違い、
        # また同じことを訊かれます** ── 選んだ意味がありません。
        from nippou.logic import shift_anchor

        ctx.anchor = shift_anchor.start_key((report_date, line, shift), now)
        log_button_click("shift_changed", line=line,
                         extra=f"{crossing.opened.label()}→{report_date} {shift}"
                               f" / 選んだのは {choice}")

    key = (report_date, line, shift, page)
    problem = _recalculate(state, ctx, calc, shift=shift)

    lots = [state.value(r, "LOT") for r in range(1, constants.ROW_COUNT + 1)]
    duplicates = validation.find_duplicate_lots(lots)
    if duplicates:
        # **422 で断る**(形は正しいが業務として通せない)。画面ぜんぶも
        # 一緒に返すので、断られた画面が古いままにならない
        body = _view(state, ctx, calc, key=key, problem=problem)
        body["message"] = "LOTが重複しています: " + ", ".join(sorted(duplicates))
        body["duplicates"] = sorted(duplicates)
        return jsonify(body), 422

    # --- 時間の断りが残っている行があれば、保存させない ---
    #
    # **ここまでは警告を出すだけでした。** 出しても入力は進められるので、
    # 間違ったまま保存され、気づくのは翌日の集計です。打つのは自由な
    # ままにして、**関門は保存(確定)に置きます** ── 直すべき行が
    # そのまま残ることが無くなります。
    #
    # 自動保存(`silent`)は止めません。打ちかけを手元に残すためのもので、
    # ここで止めると**打っている最中に何も残らない**ことになります
    # (共有へ出るのは「共有へ保存」で、そちらは別の関門を通ります)。
    bad = _bad_rows(state, ctx, calc, shift=shift)
    if bad and not silent:
        body = _view(state, ctx, calc, key=key, problem=problem)
        body["message"] = ("直すところがあるので保存できません。\n"
                           + "\n".join(b["message"] for b in bad))
        body["bad_rows"] = bad
        return jsonify(body), 422

    # 他直のデータを上書きしようとしていないか(`CheckShiftGuard` 相当)
    #
    # **またいだぶんは上で片付いています。** ここに残るのは、わざわざ
    # 過去を開いている場合だけ ── そちらは断るのが正しい
    if ctx.recall.active and not ctx.editor and not crossing.crossed \
            and (report_date, shift) != (current_date, current_shift):
        body = _view(state, ctx, calc, key=key, problem=problem)
        body["message"] = "他の直のデータです。管理者モードで開いてください。"
        return jsonify(body), 403

    # **固定した直の時間が過ぎている。**
    #
    # 画面は伏せてありますが(`_is_read_only`)、伏せるのは画面の親切で、
    # 断るのはここです ── 古い画面や別のタブから来たぶんまで止めないと、
    # 「打てるのに保存だけ通る」が残ります。
    #
    # 止めるのは `draft`(見るために置くだけ)以外。自動保存も止めます
    # ── 終わった直へ勝手に書き足すのは、誰も頼んでいません。
    #
    # **例外は、いま人が選んだ1回だけ。** 直の変わり目で「打っていた直へ
    # 入れる」を押したのなら、打ってある12行はその直のものです。ここで
    # 断ると行き先が無くなります ── 終わった直を直す道は管理者モード
    # なので、打った本人には開けません。通すのは押したその1回で、固定は
    # 選ばれた直に替わっているので、**応答が返った時点で画面は閉じます。**
    chose_here = bool(crossing.crossed and choice)
    standing = anchor_standing(ctx, calc, now)
    # **過去を直しに来た人は通します。**(`_fixing_past`)
    # 画面を閉じる側(`_read_only_reason`)と同じ見方にします ── 片方だけ
    # 開けると「打てるのに保存だけ通らない」が残ります
    if standing.expired and not draft and not chose_here and not _fixing_past(ctx):
        body = _view(state, ctx, calc, key=key, problem=problem)
        body["message"] = standing.message
        body["anchor_standing"] = standing.as_dict()
        log.info("終わった直への保存を断りました key=%s", (report_date, shift))
        return jsonify(body), 403

    # --- 自動保存は、書く前に「いま書いてよいか」を訊く ---
    #
    # **`draft` は訊きません。** 移る前に1度だけ呼ばれるもので、間引きに
    # 当たって見送られると、そのまま打ちかけが消えます
    if silent and not draft:
        reason = service.autosave_reason(
            now, SETTINGS.autosave_interval_sec, current_date, current_shift)
        if reason:
            body = _view(state, ctx, calc, key=key, problem=problem)
            body["saved"] = False
            body["skipped"] = reason
            # **200 で返す。** 断りではなく「まだその時ではない」だけ
            return jsonify(body)

    header, details = presenter.to_records(state, report_date, line, shift, page)
    # --- 保存前チェック5項目 (VBA `ExecutePrintProcess`) ---
    #
    # **ここまで、この5つはどこからも呼ばれていませんでした。** 書いて
    # あって、テストも通っていて、「この直をチェック」では並ぶのに、
    # **保存を止めていたのは行ごとの時間の断りだけ**でした。つまり
    # 梱包数が検入枚数を超えていても、マスタに無い停止記号が入っていても、
    # 保存(確定)はそのまま通っていました ── 気づくのは共有へ保存する
    # ときで、直の終わりまで分かりません。
    #
    # **見るのは「保存したあとの直ぜんぶ」**です(`run_for_save`)。梱包数も
    # 作業時間の合計もページをまたいで足すので、いま押したページだけでは
    # 答えが出ません。入れてから確かめるのでは遅いので、DB の他ページへ
    # **このページを差し込んでから**走らせます。
    #
    # 最終時間と休憩の2つはここでは見ません。直が終わっていなければ必ず
    # 足りないので、見たら1行目から保存できなくなります
    # (`logic/save_checks.AT_SHIFT_END`)── そちらは「共有へ保存」が見ます。
    #
    # 自動保存(`silent`)は上の断りと同じく止めません。打ちかけを手元に
    # 残すためのもので、ここで止めると打っている最中に何も残りません。
    if not silent:
        # **前の直の始末が先。** 押し忘れ・打ち間違いのまま次の直が
        # 入力を進めてしまう流れを、ここで1度だけ止めます。通すのは
        # 「共有へ保存」か「直せないまま引き継ぐ」のどちらか1押しで、
        # その直に1ページでも保存があれば二度と止めません
        handed = _handover(ctx, key=(report_date, line, shift))
        if handed.blocked:
            body = _view(state, ctx, calc, key=key, problem=problem)
            body["message"] = handed.message
            body["handover"] = handed.as_dict()
            body.update(error_body("handover_required", handed.message))
            log.info("前の直が片付いていないので保存を止めました key=%s 残=%d",
                     header.key(), len(handed.left))
            return jsonify(body), 422

        from nippou.services import shift_check

        gate = shift_check.run_for_save(
            get_repo(), report_date, line, shift, page, details,
            day_work=header.day_shift == "有", admin=ctx.editor)
        if gate.findings:
            body = _view(state, ctx, calc, key=key, problem=problem)
            body["message"] = ("保存できません。\n" + "\n".join(
                f"{f.where} {f.message}".strip() for f in gate.findings))
            body["check_findings"] = [f.as_dict() for f in gate.findings]
            body["sound"] = gate.sound
            log.info("保存前チェックで止めました key=%s 件数=%d",
                     header.key(), len(gate.findings))
            return jsonify(body), 422

    # **1行も打っていない紙は、作らない。**
    #
    # 作業者を選んだだけで自動保存が走り、**中身の無いページが1枚
    # 書かれていました**。害が無いように見えて、そうではありません:
    #
    #     共有へ未送信 1直ぶん に数えられる
    #     紙の束に空の紙が並ぶ
    #     直が終わると「まだ共有へ渡していません」と全画面が出る
    #     ところが休憩60分が無いので**共有へは出せない** ── 片付かない
    #
    # 誰も何もしていない直が、片付けようのない宿題として残ります。
    #
    # **すでに保存してあるページは、空でも書きます。** 12行を消して
    # 保存し直す(打ち間違いを丸ごと消す)のは正しい操作で、ここで
    # 止めると消せなくなります。
    if (not presenter.sheet_has_anything(state)
            and page not in get_repo().saved_pages(report_date, line, shift)):
        body = _view(state, ctx, calc, key=key, problem=problem)
        body["saved"] = False
        if silent:
            # 自動保存。**騒がない** ── 打ち始めれば自然に保存されます
            body["skipped"] = "まだ1行も打っていないので保存していません"
            return jsonify(body)
        body["message"] = ("まだ1行も打っていません。"
                           "1行でも打ってから保存してください")
        body.update(error_body("empty_sheet", body["message"]))
        log.info("空の紙は作りません key=%s", header.key())
        return jsonify(body), 422

    try:
        get_repo().save(header, details)
    except Exception as exc:                      # noqa: BLE001 - 画面に出して継続
        log.exception("保存に失敗しました key=%s", header.key())
        body = _view(state, ctx, calc, key=key, problem=problem)
        body["message"] = f"保存に失敗しました: {exc}"
        body["saved"] = False
        return jsonify(body), 500

    if silent and not draft:
        service.mark_autosaved(now)
    # **画面で打っているページ**。控えから「新しいほう」を戻すときに、打って
    # いる最中のこのページは戻さない(`local_backup.note_editing`)
    from nippou.services import local_backup
    local_backup.note_editing(key)
    factor = _recalculate_factors(state, report_date, line, shift)
    _refresh_summary(report_date, line, shift)
    exported = _export_daily_csv(report_date, line) if not silent else None
    log.info("save ok silent=%s key=%s", silent, header.key())
    # **書いた先をそのまま返す。** `_target` を引き直すと、またいだ直後に
    # 「1直へ書いたのに画面は2直」になります
    body = _view(state, ctx, calc, key=key,
                 message="" if silent else "保存しました", problem=problem)
    body["saved"] = True
    body["saved_at"] = now.strftime("%H:%M:%S")
    body["load_factor"] = factor
    body["export"] = exported
    if not silent:
        # **音の出来事「保存した」**(v4.4.0)。鳴らすかどうかは設定で決まり、
        # 画面は鳴らせる出来事のときだけ鳴らす(`static/js/api.js` → `sound.js`)。
        # 自動保存・打ちかけでは鳴らしません(打っている最中ずっと鳴る)
        from nippou.logic import sound

        body["sound_cue"] = sound.KEY_SAVED
    return jsonify(body)


def _export_daily_csv(report_date: str, line: str) -> Optional[dict]:
    """その日の集計CSVを**確定保存のたびに書き直す**(4本)。

    【なぜ保存のたびなのか】
    押して出すボタンは残してありますが、押し忘れると共有のフォルダは
    前の直のままです。**古いCSVは、無いCSVより危ない** ── 開いた人は
    それが最新だと思って読みます。集計を保存のたびに作り直しているのと
    同じ理由で、書き出しも同じ場所に置きます。

    【自動保存では出しません】
    自動保存は欄から焦点が外れるたびに走ります。そのたびに共有の
    フォルダへ4本書くと、**打っている最中ずっと書き続ける**ことに
    なります。人が「保存」を押したときだけです。

    【落ちても保存は成功のまま】
    書き出し先が共有で、そこへ届かないことは普通にあります。CSVが
    出ないことより、打った12行が消えるほうが困ります ── ここで
    握りつぶして、結果だけ画面へ返します。
    """
    from nippou.reporting import csv_export

    # **1つ目だけ。** 2つ目の出力先へは「共有へ保存」で送れたときに出します
    # (`services/second_output` ── 共有にまだ無い中身を先に届けない)
    base = SETTINGS.report_output_dir
    try:
        written = csv_export.write_daily_set(get_repo(), report_date, line, base)
    except Exception as exc:                      # noqa: BLE001 - 保存は済んでいる
        log.exception("集計CSVの自動書き出しに失敗しました key=%s/%s",
                      report_date, line)
        # **黙って諦めません。** 出ていないことは画面に出す
        return {"ok": False, "dir": str(base), "message": f"集計CSVは出せませんでした: {exc}"}
    message = f"集計CSVを {written.folder} に出しました"
    if written.placed:
        # **黙って置きません。** 年月のフォルダに説明が無かった(初めての月か、
        # 消された)ことに気づく機会は、ここだけです
        names = "・".join(p.name for p in written.placed)
        log.info("年月のフォルダに置きました: %s", ", ".join(map(str, written.placed)))
        message += f"(年月のフォルダに {names} を置きました)"
    return {
        "ok": True,
        "dir": str(written.folder),
        "counts": written.counts(),
        "legend_reissued": written.legend_reissued,
        "message": message,
    }


def _refresh_summary(report_date: str, line: str, shift: str) -> None:
    """その直の集計を**値で残す**(VBA の集計シートに当たるもの)。

    保存のたびに作り直します ── 集計を「直の終わりに1回」にすると、
    途中で見たグラフが最後まで古いままになります。**明細が正**で、
    こちらはそこから何度でも作れます(`services/summary.py`)。

    **ここで落ちても保存は成功している。** 集計が古いことより、打った
    12行が消えるほうが困ります。
    """
    from nippou.services import summary

    try:
        summary.refresh_shift(get_repo(), report_date, line, shift)
    except Exception:                             # noqa: BLE001 - 保存は済んでいる
        log.exception("集計の作り直しに失敗しました key=%s/%s/%s",
                      report_date, line, shift)


def _recalculate_factors(state, report_date: str, line: str, shift: str):
    """負荷係数を直ぶんで計算し直す(VBA `負荷係数反映`〜`Lot数計算`)。

    **機側・NS1・AIM だけ**です。係数(用途コードごとの重み)まで出すのは
    コイル形状の機側・NS1 で、AIM(板)は ﾛｯﾄ数だけ ── `logic/load_factor.py`。

    保存したページだけでなく**直ぜんぶ**を見ます。1つのロットがページをまたぐ
    ことも、前の直から続いていることもあり、そのままでは同じロットを
    2回数えます。

    **ここで落ちても保存は成功している。** 係数が出ないことより、
    打った12行が消えるほうが困ります。
    """
    from nippou.services import load_factor as service

    try:
        applied = service.recalculate(get_repo(), report_date, line, shift)
    except Exception:                             # noqa: BLE001 - 保存は済んでいる
        log.exception("負荷係数の計算に失敗しました key=%s/%s/%s",
                      report_date, line, shift)
        return None
    if not applied.applied:
        return None
    # 画面の合計欄も、計算し直した値に差し替える(係数Lot数は計算したラインだけ)
    state.header["lot_count"] = applied.result.totals.lot_count_text
    if applied.result.coefficient:
        state.header["coefficient_lot_count"] = applied.result.totals.coefficient_text
    return applied.as_dict()


@bp.post("/api/entry/start-next")
def start_next_shift():
    """**次の直を始める。** 固定していた直を手放します。

    直の時間が過ぎて見るだけになった画面から押します ── 手放すと、
    書き先はまた時計から決まり、作業者を選んだ時点で新しい直に固定
    されます(`logic/shift_anchor`)。

    **止めっぱなしにしないための出口です。** 閉じるだけで次へ進む道が
    無いと、次に座った人は何を押せばよいのか分かりません。
    """
    from nippou.logic.shift_anchor import Anchor

    ctx = work_context.get_context()
    before = ctx.anchor.as_dict()
    ctx.anchor = Anchor()
    # **開いていた過去のページからも離れます。**
    #
    # 手放すのが固定だけだと、呼出中(過去のページを開いている)に押した
    # ときに**何も起きていないように見えます** ── 書き先は呼出の鍵の
    # ままなので、画面は前の直の紙を映したままです。次の直を始めると
    # いう1押しで、次の直の白紙まで行き着くようにします。
    if ctx.recall.active:
        build_service(ctx, current_calculator()).clear_recall_mode()
        ctx.return_to_terminal_line()
    log_button_click("start_next_shift", line=ctx.line,
                     extra=f"{before.get('report_date')}/{before.get('shift')}")
    calc = current_calculator()
    report_date, line, shift = ctx.current_key(calc)
    return jsonify({
        "message": f"{report_date} {shift} を始めます。"
                   "作業者を選んでください",
        "report_date": report_date, "line": line, "shift": shift,
        "next": "/",
    })


@bp.post("/api/entry/handover")
def handover_take():
    """前の直を**直せないまま引き継ぐ**。

    **片付けではありません。** 引き継いだ直は共有へ未送信のままで、
    レールの数にも入力画面の帯にも出続けます ── 消えるのは共有へ
    出たときだけです。ここが記録するのは「誰が・いつ・何を残したまま
    引き継いだか」の3つで、直ったことにはしません。

    中身が綺麗(押し忘れだけ)なら、そもそもこちらは要りません ──
    「共有へ保存」を押せば済みます(`/api/settings/push`)。押すことは
    直すことではないので、誰でも押せます。
    """
    from nippou.logic import handover as handover_logic
    from nippou.services import handover

    ctx = work_context.get_context()
    calc = current_calculator()
    report_date, line, shift = ctx.current_key(calc)
    payload = request.get_json(silent=True) or {}

    # **誰が引き継いだかを残す。** 名前が無ければ断ります ── 記録に
    # 「誰か」が入らないなら、引き継ぎを残す意味がありません
    saved = _load_state(report_date, line, shift,
                        get_repo().latest_page(report_date, line, shift) or 1)
    worker = handover_logic.worker_of([
        payload.get("worker"), saved.header.get("worker")])
    if not worker:
        return jsonify(error_body(
            "no_worker",
            "先に「作業者を選ぶ」で、この直の作業者を決めてください。\n"
            "引き継ぎには、引き継いだ人の名前が要ります。")), 422

    before = _handover(ctx, key=(report_date, line, shift))
    if not before.any_left:
        return jsonify({"message": "引き継ぐものはありません",
                        "handover": before.as_dict()})

    after = handover.take_over(get_repo(), current_key=(report_date, line, shift),
                               worker=worker, admin=ctx.editor)
    log_button_click("handover_take", line=line,
                     extra=f"{worker} ← {len(before.left)}直")
    return jsonify({
        "message": ("前の直を引き継ぎました。入力を始められます。\n"
                    + "\n".join(handover.notes(after.left))),
        "handover": after.as_dict(),
        "next": "/",
    })


@bp.post("/api/entry/prev-shift-ack")
def prev_shift_ack():
    """前の直の警告を「分かりました」で閉じる。

    **覚えるのはサーバ**(プロセス)。画面に覚えさせると、タブを開き
    直すたびにまた出ます ── そうなると読まずに閉じる癖がつき、本当に
    抜けた日にも気づけません。直が変われば忘れます。
    """
    from nippou.logic import prev_shift

    ctx = work_context.get_context()
    report_date, line, shift = ctx.current_key(current_calculator())
    prev_shift.gate().mark(report_date, line, shift)
    log_button_click("prev_shift_ack", line=line,
                     extra=f"{report_date}/{shift}")
    return jsonify({"acknowledged": True})


@bp.get("/api/shift/key")
def shift_key():
    """いまの (報告日, ライン, 直) と帯。**押さなくても効く見張りの口。**

    画面の1分タイマー(`static/js/shift_end.js`)がここを叩きます。
    **これが無いと、直の変わり目に気づくのが「次に何かを押したとき」に
    なります** ── 17:00 に打つ手を止めて休憩に入り、17:20 に戻って保存を
    押す、という場面で、20分間ずっと「1直」と表示されたままになります。

    ここを通ることで、もう1つ効きます: `before_request` の
    `_roll_over`(`app/__init__.py`)がこの道でも走るので、**誰も触って
    いなくても、変わり目から1分以内に管理者モードが落ちます。**

    `opened` に画面が開いた直を添えると、またいだかどうかまで返します
    ── 見分けはサーバの `logic/shift_boundary.py` が持ち、画面は
    出すだけです(判断を2か所に置かない)。
    """
    from nippou.logic import shift_boundary

    ctx = work_context.get_context()
    calc = current_calculator()
    report_date, line, shift, page = _target(ctx, calc)
    opened = _opened_key({"opened": {
        "report_date": request.args.get("report_date", ""),
        "shift": request.args.get("shift", ""),
        "page": request.args.get("page", "1"),
    }})
    crossing = shift_boundary.check(
        opened, shift_boundary.Key(report_date, shift, page))
    # **固定した直の様子も返す。**
    #
    # 直を固定してからは、時計が進んでも書き先は動きません ── そのぶん
    # 「直が変わりました」は出なくなります。かわりに出すのがこちらで、
    # 「1直の時間は◯分前に終わっています。あと◯分で見るだけになります」。
    # 見るだけになった瞬間は画面を塗り直す合図でもあります
    # (`static/js/shift_end.js`)
    standing = anchor_standing(ctx, calc)
    return jsonify({
        "report_date": report_date, "line": line, "shift": shift, "page": page,
        "recall": ctx.recall.active,
        "admin": ctx.editor,
        "ribbon": ctx.ribbon(calc, page, key=(report_date, line, shift)),
        "shift_changed": crossing.as_dict(),
        "anchor": ctx.anchor.as_dict(),
        "anchor_standing": standing.as_dict(),
    })


@bp.post("/api/entry/close")
def close_shift():
    """直の残り5分になったら、打ってあるぶんを確定する。

    **叩くのは画面の1分タイマー**(`static/js/shift_end.js`)。VBA の
    `Application.OnTime` が回していた `CheckPrintReminder` に当たります。
    走らせるかどうかの判断は全部 `services/shift_close.py` 側 ── 画面は
    1分ごとに叩くだけで、早すぎる・もう締めた・呼出中、といった見分けは
    しません(**判断を2か所に置かない**)。

    **紙は出しません。** チェック → 集計の作り直し → 締めた印、までです。
    紙は「印刷」から、欲しいときだけ出します。

    走らなかったときも 200 で返します ── ふつうはいつも「まだ早い」で、
    それは失敗ではありません。何が起きたかは `ran` と `reason` に。
    """
    from nippou.services import shift_close

    ctx = work_context.get_context()
    calc = current_calculator()
    result = shift_close.run(get_repo(), calc,
                             line=ctx.line,
                             force_day_shift=ctx.force_day_shift(),
                             recall_mode=ctx.recall.active)
    if result.ran:
        log_button_click("shift_close", line=ctx.line,
                         extra=f"{result.report_date}/{result.shift}/"
                               f"残り{result.minutes_left}分")

    # **終わったのに締まっていない直も拾う。**
    #
    # 上の `run` が見るのは**いまの直だけ**です。17:05 に開いたときには
    # 「2直の終了まで118分(まだ早い)」しか言わないので、17:00 に終わった
    # 1直は二度と自動確定されませんでした ── 押し忘れたまま直の時間を
    # 過ぎると、そこで止まります。次に誰かが開いたときに拾います。
    swept = shift_close.sweep(get_repo(), calc, line=ctx.line,
                              force_day_shift=ctx.force_day_shift(),
                              recall_mode=ctx.recall.active)
    body = result.as_dict()
    body["swept"] = [r.as_dict() for r in swept]
    if result.ran:
        # **音の出来事「直の残り5分(自動で確定)」**(v4.5.0)。走った回だけ ──
        # 確定済み・打ってあるものが無い・まだ早い、では鳴らさない。タブが
        # 2枚あっても走るのは1回(`shift_close` の見張り)なので、鳴るのも1回。
        # あとから拾った直(`swept`)は「残り5分」ではないので鳴らさない
        from nippou.logic import sound

        body["sound_cue"] = sound.KEY_AUTO_CLOSED
    if swept:
        log_button_click("shift_close_sweep", line=ctx.line,
                         extra="、".join(f"{r.report_date}/{r.shift}"
                                        for r in swept))
        done = "、".join(f"{r.report_date} {r.shift}" for r in swept)
        note = f"終わっていた {done} を確定しました"
        sent = swept[-1].pushed
        if sent:
            note += f"。共有へも送りました({sent}件)"
        elif swept[-1].push_note:
            note += f"。{swept[-1].push_note}"
        body["message"] = (f"{body['message']}\n{note}" if result.ran else note)
    return jsonify(body)


@bp.post("/api/entry/verify")
def verify_shift():
    """いまの直をチェックする(VBA `ExecutePrintProcess` の5つの手続き)。

    **保存もしませんし、止めもしません。** 保存済みのこの直の全ページを
    読み直して、「共有へ保存」が断る7項目に当ててみて、結果を返すだけ。

    **押しても止まりません。** ここは「いま何件あるか」を見せるだけで、
    止めるのは共有への保存のとき(`/api/settings/push`)です。直の途中で
    止められても、まだ打っていないだけの行がほとんどなので意味が
    ありません ── 押した人が自分で確かめるための窓口です。

    **画面に出ているページは、画面の中身で見ます**(v4.18.0)。ほかのページは
    保存済みの中身です。以前は保存済みだけを見ていたので、

        入れてるんだけどずっと出てますね(休憩 0分)

    ── 打った休憩が「保存(確定)」を押すまで届かず、過去データを開いている
    あいだは自動保存もしないので、いつまでも 0分 のままでした。まだ保存して
    いない中身で見たときは、結果にそう添えます(共有へ出るのは保存してから)。
    """
    from nippou.services import shift_check

    ctx = work_context.get_context()
    calc = current_calculator()
    payload = request.get_json(silent=True) or {}
    report_date, line, shift, page = _target(ctx, calc)
    # 呼出モード中は、開いている過去の直を見る(画面と同じものを見せる)
    report_date = str(payload.get("report_date") or report_date)
    shift = str(payload.get("shift") or shift)
    # 画面の中身。**画面が開いたのと同じページのときだけ**(直の変わり目を
    # またいで開いたままの画面の中身を、いまの直のページとして見ない)
    screen = None
    opened = _opened_key(payload)
    if isinstance(payload.get("rows"), dict) and (
            not opened.filled
            or (opened.report_date, opened.shift, opened.page) == (report_date, shift, page)):
        screen = _screen_page(presenter.parse_state(payload), report_date, line, shift, page)

    log_button_click("entry_check", line=line, extra=f"{report_date}/{shift}")
    report = shift_check.run(get_repo(), report_date, line, shift,
                             admin=ctx.editor, screen=screen)
    body = report.as_dict()
    if not report.pages:
        body["message"] = (f"{report_date} {shift} は、まだ1ページも保存されて"
                           "いません。保存してから確かめてください")
    # ------------------------------------------------------------------
    # **見たのが「いまの直」でないなら、そう言う。**
    #
    # 「2026年9月16日 L1 1直: 直すところはありません / いやいやそもそも
    # 今2直だぞ」と言われたところです。過去データを開いていると、この
    # ボタンは**画面に出ているぶん**(開いている直)を見ます ── 画面と
    # 同じものを見るのが正しいのですが、結果の1行だけを読むと
    # 「いまの直を見た」と読めてしまいます。
    #
    # 止めはしません。**どの直を見たのかを添えるだけ**です。
    # ------------------------------------------------------------------
    now = datetime.now()
    clock_shift = calc.time_check(now, ctx.force_day_shift())
    clock_date = format_business_date(
        calc.today_check(now, ctx.force_day_shift()))
    if clock_shift and (clock_date, clock_shift) != (report_date, shift):
        body["other_shift"] = {"report_date": clock_date, "shift": clock_shift}
        body["message"] = (f"{body['message']}\n"
                           f"── これは開いている過去データ"
                           f"({report_date} {shift})のぶんです。"
                           f"いまは {clock_date} {clock_shift} です。")
    return jsonify(body)


@bp.post("/api/entry/lot")
def lookup_lot():
    """ロット番号から行を埋める(VBA `SQLiteLot検索`)。

    辿る道は1本だけです:

        SIKALOT  ﾛｯﾄ番号 → 材・調質 / 寸法 / 検入枚数 / 合紙 / 用途
              ↓ (ﾛｯﾄ番号)
        SIKAHIKI ﾛｯﾄ番号 → 受注番号        ← ここが唯一の橋
              ↓ (受注番号)
        SIKAODR  受注番号 → VC / 単重 / 合紙 / 包装仕様NO / EX
              ↓ (ﾛｯﾄ番号 / 包装仕様NO)
        LS4LOT   コイル縦割・横縦割(機側・NS1 のときだけ)
        注意_包装仕様  その番号に付いている注意 → etc欄

    **引当が複数あるときは選ばせます。** VBA は最初の1件を黙って使って
    いましたが、1つのロットが複数の受注に引き当てられていると、拾った
    受注の VC・単重・合紙が実際の相手と食い違います ── 押した人には
    見分けが付きません。`hiki_no` を添えて呼び直すと、その引当で埋めます。
    """
    payload = request.get_json(silent=True) or {}
    try:
        row = int(payload.get("row", 0))
    except (TypeError, ValueError):
        row = 0
    if not 1 <= row <= constants.ROW_COUNT:
        return jsonify(error_body("bad_row", "行が分かりません", field="row")), 400

    ctx = work_context.get_context()
    calc = current_calculator()
    moved = screen_mismatch(ctx, calc, payload)
    if moved:
        return page_moved_refusal(moved)
    key = _screen_key(ctx, calc, payload)
    state = presenter.parse_state(payload)
    lot_no = state.value(row, "LOT").strip()
    # Gコースの印は**引けたときだけ**付け直す(v4.17.0)。打ち直した・引けなかった・
    # 引当を選ぶ前は、前のロットの印を残さない
    state.set(row, g_course.ROW_FAMILY, "")

    # 7桁そろうまでは**黙って何もしない**(VBA も `Exit Sub` するだけ)。
    # 打っている途中に毎回断られると、打ち終われない
    if lot_fill.check_length(lot_no) is not None:
        body = _view(state, ctx, calc, key=key)
        body["lot"] = {"row": row, "state": lot_fill.REFUSE_SHORT}
        return jsonify(body)

    found = lot_lookup.fetch(lot_no, line=ctx.line,
                             hiki_no=str(payload.get("hiki_no", "")).strip())

    if found.refusal is not None:
        # **入力そのものは受け付ける。** 引けなかったからといって、打った
        # ロット番号を消したり保存を止めたりしない(手で埋められる)
        body = _view(state, ctx, calc, key=key)
        body["lot"] = {"row": row, "state": found.refusal.reason,
                       "message": found.refusal.message}
        return jsonify(body)

    if found.choices:
        # 選ばせる。**まだ何も書き換えない** ── 選ぶ前に書くと、選び直した
        # ときにどこまでが前の引当のものか分からなくなる
        body = _view(state, ctx, calc, key=key)
        body["lot"] = {"row": row, "state": "choose", "lot_no": lot_no,
                       "choices": found.choices,
                       "message": f"引当が{len(found.choices)}件あります。選んでください。"}
        return jsonify(body)

    for family, value in found.fill.values.items():
        state.set(row, family, value)
    state.set(row, "hiki_no", found.fill.hiki_no)
    problem = _recalculate(state, ctx, calc, shift=key[2] if key else "")

    body = _view(state, ctx, calc, problem=problem, key=key)
    body["lot"] = {
        "row": row, "state": "filled", "lot_no": lot_no,
        "order_no": found.fill.order_no, "hiki_no": found.fill.hiki_no,
        "auto_marks": found.fill.auto_marks,
        # 包装仕様の注意。**コイルは画面の前に出す**(VBA の `MsgBox`)。
        # 板は etc 欄へ入るだけ ── VBA も「板***警告なし(検査側で出力)」
        # として出していなかった
        "note": found.note.as_dict(),
        "message": found.message,
        # Gコースのロット(寸法は BOX最終実績)。**引いたときに言う**(v4.17.0)
        "g_course": found.fill.g_course.as_dict() if found.fill.g_course else None,
    }
    log_button_click("lot_lookup", line=ctx.line, extra=lot_no)
    return jsonify(body)


@bp.post("/api/entry/mark")
def toggle_mark():
    """etc欄の押しボタン(VBA `SPCommand`〜`MICommand`)。

    **値は etc 欄の文字そのもの。** ボタンは書き足す・消すだけで、
    押された状態をどこにも持ちません ── 持たせると、過去データを開いた
    ときやページを移ったときに、ボタンと etc の中身が食い違います
    (VBA も `CommandLook_For` で etc を読み直して塗り直していました)。
    """
    payload = request.get_json(silent=True) or {}
    key = str(payload.get("key", ""))
    if key not in etc_marks.BY_KEY:
        return jsonify(error_body("bad_mark", "その印はありません",
                                  field="key")), 400
    try:
        row = int(payload.get("row", 0))
    except (TypeError, ValueError):
        row = 0
    if not 1 <= row <= constants.ROW_COUNT:
        return jsonify(error_body("bad_row", "行が分かりません", field="row")), 400

    ctx = work_context.get_context()
    calc = current_calculator()
    moved = screen_mismatch(ctx, calc, payload)
    if moved:
        return page_moved_refusal(moved)
    sheet = _screen_key(ctx, calc, payload)
    state = presenter.parse_state(payload)
    state.set(row, "ET", etc_marks.toggle(state.value(row, "ET"), key))
    problem = _recalculate(state, ctx, calc, shift=sheet[2] if sheet else "")
    return jsonify(_view(state, ctx, calc, problem=problem, key=sheet))


@bp.post("/api/entry/newpage")
def new_page():
    """新しいページを出す(VBA の「新規発行」に当たる)。

    **紙は12行しかありません。** VBA は使い切ると `印刷用シート(2)`
    `(3)` を新規発行して続きを打っていました(`UFdaily.Note` の
    「分無し　新規発行してください」)。Web版にはその入口が無く、
    12行目まで打つと**続きを打つ手立てがありませんでした。**

    ここがやることは2つだけです:

        1. いま画面にある12行を、いまのページとして保存する
        2. 次のページを空で作る(以後 `_target` がそちらを開く)

    **保存が先です。** 先にページを進めると、いま打った12行は
    どこにも書かれないまま画面から消えます。

    断るのは2つ:

        呼出モード中  … 過去のページを直している最中に新しいページを作ると、
                        どの直に足したのか押した人には分からない
        いまのページが空  … 押し間違いで空のページが積み上がる。
                        `page_count` は MAX([ページ]) なので、空のページを作ると
                        ページ数だけが増える

    【4ページ目からは一声かける ── **止めはしない**】
    VBA は印刷用シートが3枚しか無く、そこが天井でした(12行×3ページ=36行)。
    20年間それで足りていたので、4ページ目に来た時点でたいていは誤操作です。
    それでも**打てなくはしません** ── 直の途中で「もう打てません」に
    なるのが、日報ツールとして一番まずい止まり方だからです
    (`logic/pages.new_page_warning`)。

    聞き返しは 422 + `needs_confirm` で返し、画面が「はい」を取ってから
    `{"confirm": true}` で通します。**判断はここではなくロックの外**に
    置くので、他の道から呼んでも同じところを通ります。
    """
    payload = request.get_json(silent=True) or {}
    refused = line_refusal()
    if refused:
        return refused
    ctx = work_context.get_context()
    calc = current_calculator()
    service = build_service(ctx, calc)

    blocked = service.block_if_recall_mode("新しいページを出す")
    if blocked:
        body = error_body("recall_mode", blocked)
        body["can_return"] = True
        return jsonify(body), 422

    # 直の変わり目をまたいでいたら、**ここでは選ばせず、保存へ送ります。**
    # この道は「いまのページを保存して、次のページを作る」の2つを1度にやるので、
    # 途中でどちらの直かを訊くと、片方だけが済んだ状態になりかねません。
    # 選ぶ場所は1つ(保存)にしておくほうが、何が起きたか読めます
    report_date, line, shift, page = _target(ctx, calc)
    # **画面に出ていないページを「いまのページ」として保存しない**(v4.24.0)。
    # 発行は「画面の12行を書き先へ保存する」入口なので、保存と同じ関門です
    moved = screen_mismatch(ctx, calc, payload,
                            target=(report_date, line, shift, page))
    if moved:
        log.warning("画面のページと書き先が違うので発行しませんでした %s → %s",
                    moved["opened"], moved["target"])
        return page_moved_refusal(moved)
    now = datetime.now()
    current_shift, current_date = service.current_shift_info(
        now, ctx.force_day_shift())
    crossing = _crossing(ctx, payload, report_date, shift, page,
                         current_date, current_shift)
    if crossing.crossed:
        body = error_body(
            "shift_changed",
            f"{crossing.message}\n"
            "先に「保存(確定)」を押して、どちらの直に入れるかを決めてください。")
        body["shift_changed"] = crossing.as_dict()
        return jsonify(body), 409

    state = presenter.parse_state(payload)
    problem = _recalculate(state, ctx, calc, shift=shift)

    # **12行目が終了まで入ってから。** 1行だけ打って押せてしまうので、1行の紙が
    # 何枚も残りました ── 紙は12行の罫線が引かれた用紙で、途中で切り上げて
    # 次の紙へ行くものではありません。v4.8.0 からは12行目の**中身**で見ます
    # (11行目の終了から写っただけの開始時刻で通っていたため。
    # `logic/pages.new_page_check`)
    check = presenter.new_page_check(state)
    if not check.ready:
        body = error_body("page_not_full", check.refusal)
        body["used_rows"] = check.used
        return jsonify(body), 422

    # **赤い行が残っていれば出さない**(v4.9.0)。「保存(確定)」は前から断って
    # いましたが、発行も「いまのページを保存する」入口なのに素通りでした
    bad = _bad_rows(state, ctx, calc, shift=shift)
    if bad:
        body = _view(state, ctx, calc, problem=problem)
        body["message"] = ("直すところがあるので、新しいページを出せません。\n"
                           + "\n".join(b["message"] for b in bad))
        body["bad_rows"] = bad
        body.update(error_body("bad_rows", body["message"]))
        return jsonify(body), 422

    lots = [state.value(r, "LOT") for r in range(1, constants.ROW_COUNT + 1)]
    duplicates = validation.find_duplicate_lots(lots)
    if duplicates:
        body = _view(state, ctx, calc, problem=problem)
        body["message"] = "LOTが重複しています: " + ", ".join(sorted(duplicates))
        body["duplicates"] = sorted(duplicates)
        return jsonify(body), 422

    # いつもより多い・12行目より上に空いた行がある。**聞くだけ**で、はいと
    # 言われたら通す。**聞くのは1回にまとめる**(2つ続けて聞かれると、
    # 1つ目で「はい」を押した勢いで2つ目を読まない)
    questions = [q for q in (check.gap_question(), pages.new_page_warning(page)) if q]
    if questions and not payload.get("confirm"):
        code = "many_pages" if pages.new_page_warning(page) else "page_gaps"
        body = error_body(code, "\n\n".join(questions))
        body["needs_confirm"] = True
        body["page"] = page
        body["gaps"] = list(check.gaps)
        return jsonify(body), 422

    header, details = presenter.to_records(state, report_date, line, shift, page)

    # --- 保存前チェック ── **ここも確定の入口です** ---
    #
    # 「新規発行」は *いまのページを保存してから* 次を作ります。関門を
    # 付けていなかったので、**間違いのあるページがそのまま確定され、
    # 次のページが出ていました** ── 気づくのは直の終わりです。
    #
    # 止まったら1ページ目へ戻って直します。ページの切り替えは
    # 打ちかけのまま置くだけなので(`draft`)、直っていなくても開けます。
    #
    # **前の直の始末も同じ関門を通します。** 発行は保存の入口なので、
    # ここだけ素通りにすると「保存は止まるが発行はできる」になります
    handed = _handover(ctx, key=(report_date, line, shift))
    if handed.blocked:
        body = _view(state, ctx, calc, problem=problem)
        body["message"] = handed.message
        body["handover"] = handed.as_dict()
        body.update(error_body("handover_required", handed.message))
        return jsonify(body), 422

    from nippou.services import shift_check

    gate = shift_check.run_for_save(
        get_repo(), report_date, line, shift, page, details,
        day_work=header.day_shift == "有", admin=ctx.editor)
    if gate.findings:
        body = _view(state, ctx, calc, problem=problem)
        body["message"] = ("直すところがあるので、新しいページを出せません。\n"
                           + "\n".join(f"{f.where} {f.message}".strip()
                                       for f in gate.findings))
        body["check_findings"] = [f.as_dict() for f in gate.findings]
        body["sound"] = gate.sound
        # 表の上の一言も「先に直すところを」へ(v4.9.0)
        body["page_guide"] = presenter.guide_after_findings(
            body["page_guide"], len(gate.findings))
        log.info("新規発行を止めました key=%s 件数=%d",
                 header.key(), len(gate.findings))
        return jsonify(body), 422

    try:
        get_repo().save(header, details)
        # 次のページを空で作る。**作らないと `latest_page` が動かない**ので、
        # 画面はいまのページを開いたままになる
        next_page = get_repo().latest_page(report_date, line, shift) + 1
        empty_header, empty_details = presenter.to_records(
            presenter.empty_state(), report_date, line, shift, next_page)
        # 作業者と昼稼働は引き継ぐ。**同じ直の続き**なので、選び直させない
        empty_header.worker = header.worker
        empty_header.day_shift = header.day_shift
        get_repo().save(empty_header, empty_details)
    except Exception as exc:                      # noqa: BLE001 - 画面に出して継続
        log.exception("新しいページを作れませんでした key=%s", header.key())
        return jsonify(error_body(
            "save_failed", f"新しいページを作れませんでした: {exc}")), 500

    # ページが増えたので、直ぶんの負荷係数も数え直す(同じロットがページを
    # またいでも1回だけ数える ── `logic/load_factor.count`)
    _recalculate_factors(state, report_date, line, shift)
    _refresh_summary(report_date, line, shift)

    log_button_click("new_page", line=line, extra=str(next_page))
    log.info("new page key=%s -> ページ%s", header.key(), next_page)
    return jsonify({
        "page": next_page,
        # **戻り方も一緒に言う**(「また戻って直したい際の案内」)。出した直後が
        # いちばん前のページの間違いに気づきやすい
        # 戻り方は、新しいページの表の上に一行で出ます(`page_guide` の issued)
        "message": f"第{page}ページを保存し、第{next_page}ページを出しました",
    })


@bp.post("/api/entry/line")
def change_line():
    """ラインを変える(`Line_AutoSelection` 相当)。**管理者モード専用。**

    【なぜ日報入力から外したか】
    この端末はどのラインの端末か、が決まっているものです。**毎直
    選び直すものではありません** ── それが入力画面の1番目に並んでいると、
    「まず選ぶもの」に見えて、押し間違えたまま打ち始められます。
    ラインが違えば保存先のキーごと変わるので、気づくのは翌日の集計です。

    据え付けのときに1度決めるもの、として設定画面へ移しました。日々の
    入力からは触れません。
    """
    payload = request.get_json(silent=True) or {}
    line = str(payload.get("line", ""))
    if line not in constants.LINE_NAMES:
        return jsonify(error_body("bad_line", "そのラインはありません")), 400

    ctx = work_context.get_context()
    if not ctx.admin:
        return jsonify(error_body(
            "not_admin",
            "ラインを変えるには管理者モードが要ります"
            "(設定・管理者の「この端末のライン」)")), 403
    # 丸徳以外は設備番号を持たない(残しておくと帯に嘘が出る)。
    # **この端末に覚えます** ── 覚えないと、起動し直すたびに L1 に戻ります
    remembered = ctx.set_terminal_line(line, str(payload.get("maru_sub", "")))
    log_button_click("line_select", line=line)

    calc = current_calculator()
    report_date, _, shift, page = _target(ctx, calc)
    state = _load_state(report_date, line, shift, page)
    message = (f"{_label(line)} に切り替えました(この端末に覚えました。"
               "起動し直しても変わりません)" if remembered else
               f"{_label(line)} に切り替えましたが、この端末に覚えられませんでした ── "
               "起動し直すと元に戻ります。設定ファイルに書けるか確かめてください")
    body = _view(state, ctx, calc, message=message)
    body["ribbon"] = ctx.ribbon(calc, page)
    body["maru_sub"] = ctx.maru_sub
    body["remembered"] = remembered
    return jsonify(body)
