"""集計から除く作業者・班の直は、共有へ送るときに時間を自動で割り振る(v4.51.0)

    指定した班 or 作業者が日報入力の時に梱包作業時間の入力、作業停止の入力を不可とし、入力がなくても
    共有へ送るの処理の時に自動で「時間を割り振る」が発火し時間が入る機能の追加
    (集計から除く に入っていることが条件)。印刷には何か目印(自動とか)いれてください

【いつ】共有へ保存(いまの直・過去分・まとめて)・5分前の自動確定・終わった直の拾い の前。
【どの直】その直の**ページぜんぶ**の作業者欄に、集計から除く人(または除く班の人)がいるとき
(`services/agg_exclude`)。ふつうの人のページが混ざった直は割り振らない(打った時間を消さないため)。
【何を】打ってある梱包作業時間・作業停止を消して、「時間を割り振る」と同じ計算(直の始めから終わり・
梱包数の比・EX・段取り・VC交換・休憩 12:00/18:00/02:00 を足す)で入れ直す。ページを保存し直す。
"""
from __future__ import annotations

from typing import Iterable, Optional

from nippou import constants
from nippou.logging_setup import get_logger

log = get_logger("app.auto_time")

#: 印刷の目印(`services/paper_files.backfill_stamps`)
STAMP = "時間は自動割り振り(集計から除く)"
#: 消す欄(梱包作業時間・作業停止①〜⑤・休憩の時刻)
TIME_FAMILIES = ("KZ", "KH", "SZ", "SH")


def _stop_families() -> tuple[str, ...]:
    from nippou.logic import break_time

    out = [f for pair in constants.STOP_SLOTS for f in pair]
    return (*out, break_time.ROW_FAMILY)


def locked(worker_text: object) -> bool:
    """その作業者欄のページは、時間・停止を打てないか(集計から除く人・班の人がいる)。"""
    from nippou.services import agg_exclude

    try:
        return bool(agg_exclude.load().why(worker_text))
    except Exception:                             # noqa: BLE001 - 読めなければ止めない
        log.exception("集計から除くを読めませんでした(時間の入力は止めません)")
        return False


def shift_is_auto(repo, report_date: str, line: str, shift: str) -> bool:
    """その直のページぜんぶが、集計から除く人・班の人のページか。"""
    pages = repo.saved_pages(report_date, line, shift)
    if not pages:
        return False
    for page in pages:
        loaded = repo.load(report_date, line, shift, page)
        if loaded is None or not locked(getattr(loaded[0], "worker", "")):
            return False
    return True


def fill(keys: Iterable[tuple[str, str, str]]) -> list[str]:
    """渡された直のうち、自動で割り振る直に時間を入れる。言うこと(1直に1つ)を返す。"""
    from . import get_repo

    notes: list[str] = []
    for report_date, line, shift in sorted(set(tuple(k[:3]) for k in keys)):
        try:
            if not shift_is_auto(get_repo(), report_date, line, shift):
                continue
            note = split_shift(report_date, line, shift)
        except Exception as exc:                  # noqa: BLE001 - 送るのは止めない(確かめで止まる)
            log.exception("時間を自動で割り振れませんでした %s/%s/%s", report_date, line, shift)
            notes.append(f"{report_date} {shift} の時間を自動で割り振れませんでした: {exc}")
            continue
        if note:
            notes.append(note)
    return notes


def split_shift(report_date: str, line: str, shift: str) -> Optional[str]:
    """その直の全ページに、直の始めから終わりまでを割り振って保存する(「時間を割り振る」と同じ計算)。"""
    from nippou.logic import break_time, time_split
    from nippou.services import page_writer, shift_check

    from . import get_repo
    from .current import time_split_factors, time_split_stop_kinds
    from .routes.entry import _add_breaks, _last_lot_before, _load_state, _refresh_summary
    from nippou.presenters import entry as presenter

    repo = get_repo()
    start_text, end_text = shift_check.bounds_of(repo.get_shift_times(line), shift)
    start = time_split.parse_hhmm(start_text)
    end = time_split.parse_hhmm(end_text)
    if start is None or end is None:
        return f"{report_date} {shift}: 直の開始・終了が分からないので時間を自動で割り振れませんでした"
    total = (end - start) % (24 * 60)
    pages = {n: _load_state(report_date, line, shift, n) for n in repo.saved_pages(report_date, line, shift)}
    # **打ってある時間・停止は消す**(集計から除く人の入力は使わない)
    clear = (*TIME_FAMILIES, *_stop_families())
    for state in pages.values():
        for row, values in state.rows.items():
            if time_split.is_filled(values):
                for family in clear:
                    if str(values.get(family, "") or "").strip():
                        state.set(row, family, "")
    targets = [(n, row) for n in sorted(pages) for row in sorted(pages[n].rows)
               if time_split.is_filled(pages[n].rows[row])]
    if not targets:
        return None
    target_rows = [pages[n].rows[row] for n, row in targets]
    kinds = time_split.lot_runs(target_rows, _last_lot_before(report_date, line, shift))
    day_work = any(p.day_shift for p in pages.values())
    stops = [time_split.row_stop_minutes(values) for values in target_rows]
    weights, _by_packs = time_split.pack_weights([time_split.row_packs(v) for v in target_rows])
    weights, _notes = time_split.adjust_weights(weights, target_rows, kinds, time_split_factors(),
                                                time_split_stop_kinds())
    breaks_info: dict = {}
    if day_work:
        plan = time_split.split(stops, total, weights=weights, origin=0)
    else:
        bplan = time_split.plan_breaks(target_rows, range(len(targets)), shift_start=start, shift_total=total,
                                       span_start=start, span_total=total, from_start=False,
                                       shift_window=break_time.shift_window(start_text, end_text))
        plan = time_split.split_pinned(stops, total, pins=[pin[:3] for pin in bplan.pins],
                                       rests=bplan.skip, weights=weights, origin=0)
        if plan.ok:
            breaks_info = _add_breaks(bplan, plan, targets, list(targets), pages, start)
            if breaks_info.get("refuse"):
                return f"{report_date} {shift}: {breaks_info['refuse']}"
    if not plan.ok:
        return f"{report_date} {shift}: 時間を自動で割り振れませんでした({plan.message})"
    for (n, row), (a, b) in zip(targets, plan.slots):
        kz, kh = time_split.clock(start, a)
        sz, sh = time_split.clock(start, b)
        for family, value in (("KZ", kz), ("KH", kh), ("SZ", sz), ("SH", sh)):
            pages[n].set(row, family, value)
    for w in breaks_info.get("writes", []):
        for family, value in w["set"]:
            pages[w["page"]].set(w["row"], family, value)
        pages[w["page"]].set(w["row"], break_time.ROW_FAMILY, w["range"])
    from nippou import work_context

    from .current import current_calculator
    from .routes.entry import _recalculate

    ctx, calc = work_context.get_context(), current_calculator()
    for n in sorted(pages):
        _recalculate(pages[n], ctx, calc, shift=shift)   # 作業時間の欄も入れ直す
        header, details = presenter.to_records(pages[n], report_date, line, shift, n)
        page_writer.save_page(repo, header, details, by_screen=False)
    _refresh_summary(report_date, line, shift)
    log.info("集計から除く直の時間を自動で割り振りました %s/%s/%s %s〜%s %d行",
             report_date, line, shift, start_text, end_text, len(targets))
    return f"{report_date} {shift}: 集計から除く作業者の直なので、時間を自動で割り振りました({len(targets)}行)"
