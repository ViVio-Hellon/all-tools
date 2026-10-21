"""どの画面からも使う「いまの直」の組み立て(段階2・案3)

直の境界の計算機・このリクエストのサービス・いま書いているページ・帯・動線の帯・時間の割り振りを
使う端末か。前は日報入力の道(`app/routes/entry.py`)にあり、ほかの画面の道が 17 か所から
**画面の道から借りて**いました。中身はそのまま移しています(`entry.py` からも同じ名前で使える)。
"""
from __future__ import annotations

from datetime import datetime

from nippou import work_context
from nippou.config import SETTINGS
from nippou.logging_setup import get_logger
from nippou.logic.shift import ShiftCalculator, calculator_from_master
from nippou.services.nippou_service import NippouService

from . import get_repo

log = get_logger("app.current")


def build_shift_calculator(raw: dict[str, tuple[str, str]]) -> ShiftCalculator:
    """直の境界時刻。tkinter版 `ui/app.py:build_shift_calculator` と同じ。

    **読み方は `logic/shift.times_from_master` の 1 か所**(片側でも欠けていたら・時:分として
    読めなければ両方とも既定に落とす)。保存前チェック・押した記録・過去日報の取り込み・
    全停入力も同じ関数で読む ── 同じ直の終わりを 2 通りに決めていたことがある。
    """
    return calculator_from_master(raw)


def current_calculator() -> ShiftCalculator:
    return build_shift_calculator(get_repo().get_shift_times())


def current_target(ctx: work_context.WorkContext, calc: ShiftCalculator):
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
    return ctx.ribbon(calc, current_target(ctx, calc)[3])


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


def time_split_enabled() -> bool:
    """「時間を割り振る」を使う端末か(設定・管理者でオンにする。既定はオフ)。"""
    from nippou import user_settings
    from nippou.config import KEY_TIME_SPLIT

    value = user_settings.get(KEY_TIME_SPLIT, False)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "on", "yes")
    return bool(value)


def build_service(ctx, calc) -> NippouService:
    """このリクエスト用のサービス。**状態は入れものから借りる。**

    呼出モードと最後の自動保存時刻は `work_context` が持つので、
    要求ごとに作り直しても忘れない(`ServiceState` の説明)。
    """
    return NippouService(get_repo(), calc, is_admin=lambda: ctx.editor,
                         confirm_other_shift_edit=lambda *a: True,
                         state=ctx.service_state)
