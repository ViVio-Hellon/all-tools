"""取り込み済みの過去日報を直す ── 2直に入った3直の行を、3直へ戻す

    2025年12月1日_HVC 2直であるのにかかわらず 22:50 以降があり8行目が余分
    入ってしまっているのはどうしたらいいんですか？

【何が入ってしまっていたか】
過去日報(xlsx)の取り込みは、集計シートの区画だけで直を決めていました。3直は
22:50 に始まるので、3直の最初の行は「22時台」の枠 ── 2直の区画に置かれている
ことがあり、それを2直として入れていました(`logic/nippou_sheet` の冒頭)。
取り込みは直しましたが、**もう入っている分は入れ直さないと残ります。**
何百本もあるファイルを入れ直さなくて済むよう、手元のDBの中で戻します。

【戻すもの】
2直にある**3直の始まり(時間用。読めなければ 22:50)以降に始まる行**だけ。
3直の頭へ、並びを変えずに移します。12行ずつのページに組み直し、集計と
集計CSVも作り直します。共有へ出さない扱い(送った印)だった直は、印を保ったまま
にします(古い日報を、直したからといって Access へ飛ばさない)。

**いまの作業日は触りません**(打っている最中の直を動かさない)。
下見(`plan`)で日と行を全部出してから、押したら戻す(`apply`)。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

from .. import constants
from ..db.models import DetailRecord, HeaderRecord
from ..db.repository import NippouRepository
from ..logging_setup import get_logger

log = get_logger("services.reshift")

SECOND, THIRD = "2直", "3直"


@dataclass
class Fix:
    """戻す1日ぶん。"""

    report_date: str
    line: str
    rows: list[str] = field(default_factory=list)   # 移す行の説明(画面に出す)
    third_existed: bool = True                      # 3直が前から有ったか(無ければ作る)

    def as_dict(self) -> dict:
        return {"report_date": self.report_date, "line": self.line,
                "rows": list(self.rows), "third_existed": self.third_existed}


@dataclass
class Result:
    days: int = 0
    rows: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    csv_failed: list[str] = field(default_factory=list)

    @property
    def message(self) -> str:
        if not self.days:
            return "戻すものはありませんでした"
        text = f"{self.days}日ぶん {self.rows}行を3直へ戻しました(集計とCSVも作り直しました)"
        if self.failed:
            text += f"。ただし {len(self.failed)}日は戻せませんでした"
        if self.csv_failed:
            text += f"。{len(self.csv_failed)}日ぶんの集計CSVは出せませんでした(日報は直っています)"
        return text

    def as_dict(self) -> dict:
        return {"days": self.days, "rows": self.rows, "message": self.message,
                "failed": [{"day": d, "error": e} for d, e in self.failed],
                "csv_failed": list(self.csv_failed)}


def _starts(repo: NippouRepository) -> dict[str, int]:
    from ..logic import nippou_sheet

    try:
        return nippou_sheet.shift_starts(repo.get_shift_times())
    except Exception:                             # noqa: BLE001 - 控えで見る
        return nippou_sheet.shift_starts()


def _start(d: DetailRecord) -> Optional[int]:
    try:
        return int(float(d.kz)) * 60 + int(float(d.kh or 0))
    except (TypeError, ValueError):
        return None


def _load_shift(repo: NippouRepository, day: str, line: str, shift: str):
    pages = []
    for page in repo.saved_pages(day, line, shift):
        loaded = repo.load(day, line, shift, page)
        if loaded is not None:
            pages.append(loaded)
    return pages


def _end(d: DetailRecord) -> Optional[int]:
    try:
        return int(float(d.sz)) * 60 + int(float(d.sh or 0))
    except (TypeError, ValueError):
        return None


def _late(details: list[DetailRecord], starts: dict[str, int],
          third: Optional[list[DetailRecord]] = None) -> list[DetailRecord]:
    """2直の行のうち3直のもの(決め方は取り込みと同じ `nippou_sheet.tail_for_next`)。"""
    from ..logic.nippou_sheet import tail_for_next

    filled = [d for d in details if _start(d) is not None or str(d.lot or "").strip()]
    first = next((d for d in (third or []) if _start(d) is not None), None)
    picked = tail_for_next([(_start(d), _end(d), d.lot) for d in filled],
                           (_start(first), first.lot) if first else None,
                           starts[SECOND], starts[THIRD])
    return [filled[i] for i in picked]


def _content(d: DetailRecord) -> tuple:
    return tuple(getattr(d, k) for k in d.__dataclass_fields__
                 if k not in ("report_date", "line", "shift", "page", "row_no"))


def _copies(details: list[DetailRecord], late: list[DetailRecord]) -> list[DetailRecord]:
    """2直に残る行のうち、3直へ戻す行と**中身がまったく同じ**もの(重なって入った写し)。

    2026/1/10 は「HX460Y0 22:25〜23:30 1枚」が2直に4行あった(ファイルでは1行)。
    同じ時刻に同じ行が何本もあることは無いので、戻す行と同じものは写しとして消す。
    """
    wanted = {_content(d) for d in late}
    return [d for d in details if d not in late and _content(d) in wanted]


def _describe(d: DetailRecord) -> str:
    when = f"{d.kz}:{str(d.kh).zfill(2)}〜{d.sz}:{str(d.sh).zfill(2)}"
    what = d.lot or "(ロット空欄)"
    extra = f" 停止{d.s} {d.th}分" if d.s and not d.lot else ""
    count = f" {d.con}枚" if d.con else ""
    return f"2直 {d.page}ページ {d.row_no}行目 {what} {when}{count}{extra}"


def plan(repo: NippouRepository, *, skip_dates: tuple[str, ...] = ()) -> list[Fix]:
    """戻すものを出す。**書きません。**"""
    starts = _starts(repo)
    keys = repo.conn.execute(
        "SELECT DISTINCT report_date, line FROM daily_header WHERE shift=?", (SECOND,)).fetchall()
    out: list[Fix] = []
    for row in keys:
        day, line = row["report_date"], row["line"]
        if day in skip_dates:
            continue
        details = [d for _h, rows in _load_shift(repo, day, line, SECOND) for d in rows]
        third = [d for _h, rows in _load_shift(repo, day, line, THIRD) for d in rows]
        late = _late(details, starts, third)
        if not late:
            continue
        rows = [_describe(d) for d in late]
        rows += [_describe(d) + "(同じ行の重なり。消します)" for d in _copies(details, late)]
        out.append(Fix(day, line, rows, third_existed=bool(repo.saved_pages(day, line, THIRD))))
    from ..logic.shift import parse_business_date

    out.sort(key=lambda f: (str(parse_business_date(f.report_date) or f.report_date), f.line))
    return out


def _rewrite(repo: NippouRepository, day: str, line: str, shift: str,
             header: HeaderRecord, rows: list[DetailRecord], synced: bool) -> None:
    """その直を、`rows` を12行ずつ組んだページで置き換える。"""
    for page in repo.saved_pages(day, line, shift):
        repo.delete_page(day, line, shift, page)
    size = constants.ROW_COUNT
    for index in range(0, len(rows), size):
        page = index // size + 1
        head = replace(header, report_date=day, line=line, shift=shift, page=page)
        chunk = [replace(d, report_date=day, line=line, shift=shift, page=page,
                         row_no=i + 1) for i, d in enumerate(rows[index:index + size])]
        repo.save(head, chunk)
        if synced:
            repo.mark_synced((day, line, shift, page))


def apply(repo: NippouRepository, *, skip_dates: tuple[str, ...] = (),
          out_dir: Optional[Path] = None) -> Result:
    """下見と同じものを、もう一度調べてから戻す。"""
    from . import summary
    from .csv_import import Result as ImportResult
    from .csv_import import _out_dir, _write_csv

    starts = _starts(repo)
    result = Result()
    touched: list[tuple[str, str]] = []
    for fix in plan(repo, skip_dates=skip_dates):
        day, line = fix.report_date, fix.line
        try:
            second = _load_shift(repo, day, line, SECOND)
            third = _load_shift(repo, day, line, THIRD)
            rows2 = [d for _h, rows in second for d in rows]
            late = _late(rows2, starts, [d for _h, rows in third for d in rows])
            if not late:
                continue
            copies = _copies(rows2, late)
            keep = [d for d in rows2 if d not in late and d not in copies]
            rows3 = late + [d for _h, rows in third for d in rows]
            head2 = second[0][0]
            head3 = third[0][0] if third else HeaderRecord(
                report_date=day, line=line, shift=THIRD, page=1, worker="",
                day_shift=head2.day_shift)
            # 送った印(共有へ出さない扱い)は、直す前に全ページ付いていたら保つ
            synced2 = all(not h.dirty for h, _r in second)
            synced3 = all(not h.dirty for h, _r in third) if third else synced2
            _rewrite(repo, day, line, SECOND, head2, keep, synced2)
            _rewrite(repo, day, line, THIRD, head3, rows3, synced3)
            for shift in (SECOND, THIRD):
                try:
                    summary.refresh_shift(repo, day, line, shift)
                except Exception:                 # noqa: BLE001 - 日報は直っている
                    log.exception("戻したあとの集計を作れませんでした: %s %s %s", day, line, shift)
        except Exception as exc:                  # noqa: BLE001 - 1日で止めない
            log.exception("3直へ戻せませんでした: %s %s", day, line)
            result.failed.append((f"{day} {line}", str(exc)))
            continue
        result.days += 1
        result.rows += len(late)
        touched.append((day, line))
        log.info("3直へ戻しました: %s %s %d行", day, line, len(late))

    if touched:
        made = ImportResult()
        _write_csv(repo, made, touched, _out_dir(out_dir))
        result.csv_failed = [d for d, _e in made.csv_failed]
    return result
