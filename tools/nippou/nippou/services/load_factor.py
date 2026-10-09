"""負荷係数を直ぶんで計算し直す

判断そのものは `logic/load_factor.py`(純粋)。ここが受け持つのは3つ:

    1. 用途名負荷係数算出マスタを引く
    2. **前の直**のページを新しい順に3枚さがす(VBA `GetPreviousFromCurrent`)
    3. 計算した結果をDBへ書き戻す(各行の係数処理ﾛｯﾄ数 + 合計欄の2つ)

【いつ走るか】
VBA は「Weight計算」ボタンと、直の終わりの確定処理の中で呼んでいました
(`負荷係数反映(27)` → `不要係数処理ロットクリア` → `Lot数計算`)。
ここでは**保存のたび**に直ぶんを計算し直します ── 直の終わりに1回だけ
にすると、途中で見た合計欄が最後まで古いままになります。数えるのは
手元のDBだけなので、毎回やっても重くありません。

【前の直をどうさがすか】
VBA はシート名を正規表現で解いて、同じライン・1つ前の直・その最大ページ、
というふうに遡っていました。こちらはシートが無いので、保存済みの
(報告日, 直, ページ)を**時間の順に並べて**、今の直より前のものを新しい順に
取ります。並べ方だけ VBA と違いますが、拾う相手は同じです。

VBA の並びは `Array("1直","2直","3直")` の3つだけで、**日勤があるライン
では順が決まりません**。ここでは直の始まる時刻で並べます(1直 → 日勤 →
2直 → 3直)。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from ..db.models import DetailRecord, HeaderRecord
from ..db.repository import NippouRepository
from ..logging_setup import get_logger
from . import page_writer
from ..logic import load_factor
from ..logic.load_factor import Result
from ..logic.shift import parse_business_date

log = get_logger("services.load_factor")

#: 直の並び。**始まる時刻の順**(1直 08:00 / 日勤 08:00 / 2直 17:00 /
#: 3直 22:00)。日勤と1直は同じ時刻に始まるので、日勤を後ろに置く。
#:
#: **`logic/shift.SHIFT_NAMES` とはわざと違う並びです。** あちらは紙に
#: 出る順(1直→2直→3直→日勤)で、こちらは係数を引き継ぐ順 ── どちらかに
#: 揃えると、係数が1直から日勤へ渡らなくなります
SHIFT_ORDER: dict[str, int] = {"1直": 0, "日勤": 1, "2直": 2, "3直": 3}
_LAST = max(SHIFT_ORDER.values()) + 1


def _rank(report_date: str, shift: str) -> tuple[date, int]:
    """時間の順に並べるための鍵。読めない日付はいちばん古い扱い。"""
    return (parse_business_date(report_date) or date.min,
            SHIFT_ORDER.get(shift, _LAST))


def previous_pages(repo: NippouRepository, report_date: str, line: str,
                   shift: str,
                   limit: int = load_factor.LOOK_BACK_SHEETS
                   ) -> list[list[DetailRecord]]:
    """今の直より前のページを、**新しい順に** `limit` 枚。

    同じラインのぶんだけ見ます ── 別のラインで同じロット番号が動くのは
    次の工程の話で、この直の仕事が消えるわけではありません。
    """
    here = _rank(report_date, shift)
    keys = [k for k in repo.list_keys(line)
            if _rank(k[0], k[2]) < here]
    keys.sort(key=lambda k: (_rank(k[0], k[2]), k[3]), reverse=True)

    pages: list[list[DetailRecord]] = []
    for key in keys[:limit]:
        loaded = repo.load(*key)
        if loaded is not None:
            pages.append(loaded[1])
    return pages


@dataclass
class Applied:
    """走らせた結果。**書き戻したページ数まで返す。**"""

    result: Result
    pages: int = 0
    written: int = 0

    @property
    def applied(self) -> bool:
        return self.result.applied

    def as_dict(self) -> dict:
        body = self.result.as_dict()
        body.update({"pages": self.pages, "written": self.written})
        return body


def factors() -> dict[str, float]:
    """用途コード → 換算係数。**読めなければ空**(係数は全部1になる)。"""
    from ..access_bridge import factor_master

    try:
        return factor_master.load_factors()
    except Exception:                             # noqa: BLE001 - 保存を止めない
        log.warning("用途名負荷係数算出を読めませんでした。係数は1で通します")
        return {}


def recalculate(repo: NippouRepository, report_date: str, line: str,
                shift: str, *, table: Optional[dict[str, float]] = None
                ) -> Applied:
    """1直ぶんを計算し直して書き戻す。

    **機側・NS1・AIM 以外では何もしません**(`logic/load_factor.applies`)。
    そこでは係数処理ﾛｯﾄ数もﾛｯﾄ数も手入力のままです。**AIM は ﾛｯﾄ数だけ**
    書きます(係数処理ﾛｯﾄ数・係数Lot数は打った値を触らない ── v4.20.0)。
    """
    if not load_factor.applies(line):
        return Applied(Result(applied=False))

    pages = repo.saved_pages(report_date, line, shift)
    loaded: list[tuple[HeaderRecord, list[DetailRecord]]] = []
    rows: list[DetailRecord] = []
    for page in pages:
        found = repo.load(report_date, line, shift, page)
        if found is None:
            continue
        loaded.append(found)
        rows.extend(found[1])
    if not loaded:
        return Applied(Result(applied=True))

    before = [(d.page, d.row_no, d.keisu) for d in rows]
    result = load_factor.recalculate(
        rows, line=line,
        factors=factors() if table is None else table,
        previous=previous_pages(repo, report_date, line, shift))

    changed = {(d.page, d.row_no) for d, (p, r, keisu) in zip(rows, before)
               if d.keisu != keisu}
    written = 0
    for header, details in loaded:
        totals_changed = (
            header.lot_count != result.totals.lot_count_text
            or (result.coefficient
                and header.coefficient_lot_count != result.totals.coefficient_text))
        rows_changed = any((d.page, d.row_no) in changed for d in details)
        if not (totals_changed or rows_changed):
            continue
        # 合計欄は**どのページにも同じ値**を入れる。ページごとに違う数字が載ると、
        # 紙を1枚だけ見た人が直の合計を読み違える
        header.lot_count = result.totals.lot_count_text
        if result.coefficient:                    # AIM は係数Lot数を打った値のまま
            header.coefficient_lot_count = result.totals.coefficient_text
        # 保存の続きで計算し直すだけ(係数・合計)。印は付けない ── 付けると保存した画面
        # 自身が「画面の外から書き直された」と断られる
        page_writer.save_page(repo, header, details, by_screen=True)
        written += 1

    if written:
        log.info("負荷係数を計算し直しました %s %s %s: %s",
                 report_date, line, shift, result.message)
    return Applied(result, pages=len(loaded), written=written)
