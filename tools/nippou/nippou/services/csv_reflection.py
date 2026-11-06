"""直しが集計CSVに入ったかを、**共有フォルダの集計CSVを読んで**確かめる (v4.46.9)

    直しが入ったかの確認は実際開いて確認しかないですよね
    → 共有フォルダの集計CSVを読み、直したあとに書き直されたかを見る仕組みを作ってください

ほかのラインの直を管理者のPCで直すと、集計CSVはそのラインのPCが直しを受け取ったときに
書き直します(`day_csv.refusal`)。それが済んだかは、CSVを開いて目で比べるしかありませんでした。

ここでは**出力先(1つ目・共有)にあるその日の集計CSV**を読み、その直の行を**このPCの中身**
(直した中身)と1行ずつ比べます。

    明細CSV   その直の行(ページ・行で突き合わせ)。全部の列を比べる
    集計CSV   その直の1行。全部の列を比べる

ファイルの日時では見ません ── 書き直されていても、古い中身で書かれていれば意味が無いので。

比べない列は **係数処理ﾛｯﾄ数** だけです。負荷係数はそのラインのPCが前の直を見て数え直す
(このPCではほかのラインの係数を数えない)ので、このPCの値と違っていて当たり前です。

食い違ったら「まだ」です。直したあとに、そのラインのPCでさらに直したときも「違う」に
なります(このPCの中身が古い)── どちらかは、出す一言に「どこが違うか」を添えて見分けます。
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

#: 比べない列(そのラインのPCが数え直す)
SKIP_COLUMNS = frozenset({"係数処理ﾛｯﾄ数"})

#: 出す食い違いの数(多いと読めない)
MAX_DIFFS = 5

DONE = "done"
NOT_YET = "not_yet"
NO_CSV = "no_csv"
UNREADABLE = "unreadable"
TEST = "test"
EMPTY = "empty"


@dataclass
class Check:
    state: str
    text: str
    diffs: list[str] = field(default_factory=list)
    folder: str = ""

    @property
    def done(self) -> bool:
        return self.state == DONE

    def as_dict(self) -> dict:
        return {"state": self.state, "text": self.text, "diffs": list(self.diffs),
                "folder": self.folder, "done": self.done}


def _read(path: Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return [{k: (v or "").strip() for k, v in row.items() if k is not None}
                for row in csv.DictReader(f)]


def _same_shift(row: dict[str, str], report_date: str, line: str, shift: str) -> bool:
    return (row.get("日付", "") == str(report_date) and row.get("ライン", "") == str(line)
            and row.get("直", "") == str(shift))


def _where(row: dict[str, str]) -> str:
    return f"{row.get('ページ', '?')}ページ {row.get('行', '?')}行目"


def _compare(expected: list[dict[str, str]], found: list[dict[str, str]],
             columns: list[str], keyed: bool) -> list[str]:
    """食い違いを、人が読める形で。`keyed` なら ページ・行 で突き合わせる。"""
    diffs: list[str] = []
    if keyed:
        want = {(r.get("ページ", ""), r.get("行", "")): r for r in expected}
        got = {(r.get("ページ", ""), r.get("行", "")): r for r in found}
        for key in sorted(set(want) | set(got), key=lambda k: (_num(k[0]), _num(k[1]))):
            a, b = want.get(key), got.get(key)
            if a is None:
                diffs.append(f"{_where(b)}: 集計CSVにあるが、このPCには無い行")
            elif b is None:
                diffs.append(f"{_where(a)}: 集計CSVに無い")
            else:
                diffs.extend(f"{_where(a)} {c}: 集計CSV「{b.get(c, '')}」・このPC「{a.get(c, '')}」"
                             for c in columns
                             if c not in SKIP_COLUMNS and a.get(c, "") != b.get(c, ""))
        return diffs
    if len(expected) != len(found):
        return [f"集計の行: 集計CSV {len(found)}行・このPC {len(expected)}行"]
    for a, b in zip(expected, found):
        diffs.extend(f"集計 {c}: 集計CSV「{b.get(c, '')}」・このPC「{a.get(c, '')}」"
                     for c in columns
                     if c not in SKIP_COLUMNS and a.get(c, "") != b.get(c, ""))
    return diffs


def _num(text: str) -> int:
    try:
        return int(text)
    except (TypeError, ValueError):
        return 0


def _text_of(rows: dict[str, object]) -> dict[str, str]:
    return {k: ("" if v is None else str(v)).strip() for k, v in rows.items()}


def expected_rows(repo, report_date: str, line: str, shift: str
                  ) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """このPCの中身から作る、その直の (明細の行, 集計の行)。CSVを書くときと同じ道で作る。"""
    from ..reporting import csv_export
    from . import summary

    reports = [r for r in summary.for_date(repo, report_date, line) if r.shift == shift]
    lots = [r for r in summary.day_lot_rows(repo, report_date, line) if r.shift == shift]
    detail = [_text_of(r) for r in csv_export.detail_rows_of(lots, reports)]
    aggregate = [_text_of(csv_export.aggregate_row_dict(summary.as_aggregate(r)))
                 for r in reports]
    return detail, aggregate


def check(repo, report_date: str, line: str, shift: str, *,
          base: Optional[Path] = None, cache: Optional[dict] = None) -> Check:
    """その直の直しが、出力先(1つ目)の集計CSVに入っているか。"""
    from ..config import SETTINGS
    from ..reporting import csv_export

    out_dir = Path(base if base is not None else SETTINGS.report_output_dir)
    paths = csv_export.daily_output_paths(out_dir, report_date, line)
    folder = str(paths["aggregate"].parent)
    if repo.is_test_shift(report_date, line, shift):
        return Check(TEST, "テストの直なので集計CSVに出しません", folder=folder)
    detail, aggregate = expected_rows(repo, report_date, line, shift)
    missing = [p for key, p in paths.items() if key in ("aggregate", "detail") and not p.exists()]
    if missing:
        if not detail and not aggregate:
            return Check(EMPTY, "打った行が無い直です(集計CSVに出る行がありません)", folder=folder)
        return Check(NO_CSV, f"まだです(集計CSVがまだありません: {missing[0].name})", folder=folder)
    # 一覧では同じ日の直が並ぶ。共有のファイルは1日1回だけ読む
    cache = {} if cache is None else cache

    def read(path: Path) -> list[dict[str, str]]:
        if path not in cache:
            cache[path] = _read(path)
        return cache[path]

    try:
        found_detail = [r for r in read(paths["detail"])
                        if _same_shift(r, report_date, line, shift)]
        found_aggregate = [r for r in read(paths["aggregate"])
                           if _same_shift(r, report_date, line, shift)]
    except (OSError, UnicodeError, csv.Error) as exc:
        return Check(UNREADABLE, f"集計CSVを読めません: {exc}", folder=folder)
    diffs = (_compare(detail, found_detail, csv_export.DETAIL_FIELDNAMES, keyed=True)
             + _compare(aggregate, found_aggregate, csv_export.FIELDNAMES, keyed=False))
    if not diffs:
        return Check(DONE, "入っています", folder=folder)
    shown = diffs[:MAX_DIFFS]
    if len(diffs) > MAX_DIFFS:
        shown.append(f"ほか {len(diffs) - MAX_DIFFS}件")
    return Check(NOT_YET, f"まだです(違うところ {len(diffs)}件)", diffs=shown, folder=folder)


def check_many(repo, keys, *, base: Optional[Path] = None) -> list[dict]:
    """一覧の直をまとめて。1つ読めなくても残りは続ける。"""
    from ..logging_setup import get_logger

    out: list[dict] = []
    cache: dict = {}
    for report_date, line, shift in keys:
        try:
            got = check(repo, report_date, line, shift, base=base, cache=cache)
        except Exception as exc:                  # noqa: BLE001 - 一覧は出す
            get_logger("services.csv_reflection").exception(
                "集計CSVと比べられませんでした %s/%s/%s", report_date, line, shift)
            got = Check(UNREADABLE, f"比べられませんでした: {exc}")
        out.append({"report_date": report_date, "line": line, "shift": shift, **got.as_dict()})
    return out
