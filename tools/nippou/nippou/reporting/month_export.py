"""1か月ぶんを月別フォルダへ書き出す (VBA ``シートコピー保存処理``)

【VBA は何を残していたか】
月が替わると、ブックから

    集計シート / 停止集計シート / その月の日付シート

を**新しいブックへ丸ごとコピー**して、
`…\\日報管理\\自動集計\\yyyy.mm\\<ライン>\\yyyy年mm月_<ライン>.xlsx`
に保存していました。そのあとブック側からは消します(`月初削除`)。

【ここで xlsx を作らない理由】
xlsx を書くには外部ライブラリ(openpyxl 等)が要ります。この移植は
`pip install -r requirements.txt` を Flask と waitress の2つだけに
保つことを決めてあるので、**標準の csv で3本**書きます:

    〜_集計.csv     日ごと・直ごとの集計(集計シートの**上の帯**に当たる)
    〜_停止集計.csv 停止の記号ごとの回数と時間(停止集計シートに当たる)
    〜_明細.csv     **打った行そのもの**(集計シートの**表**に当たる)

3本目が要になります。書き出したあと手元のDBからその月を消すので、
**明細まで残しておかないと、その月は手元から見えなくなります。**
集計だけ残しても、あとから「この行の停止は何だったのか」は追えません。
**紙に載らない用途コード・納入先・包装仕様書No なども、残るのはここ
だけです**(`csv_export.DETAIL_FIELDNAMES` ── 日ごとの書き出しと同じ形)。

BOM付き UTF-8 なので、Windows の Excel でそのまま開けます
(`csv_export.py` と同じ)。
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ..db.repository import NippouRepository
from ..logic.month_roll import month_file_stem, month_folder_name, safe_name
from ..logic.shift import parse_business_date
from . import csv_legend
from .csv_export import (
    DETAIL_FIELDNAMES,
    FIELDNAMES,
    aggregate_row_dict,
    detail_rows_of,
    stop_rows_of,
)

#: 停止集計CSVの見出し
STOP_HEADERS: tuple[str, ...] = ("記号", "内訳", "分類", "回数", "時間(分)")


@dataclass(frozen=True)
class MonthExport:
    """書き出した結果。**どこに何件出したか**を画面とログに出す。"""

    year: int
    month: int
    line: str
    folder: Path
    files: list[Path]
    shifts: int = 0
    rows: int = 0

    @property
    def label(self) -> str:
        return f"{self.year}年{self.month}月 {self.line}"

    def as_dict(self) -> dict:
        return {"year": self.year, "month": self.month, "line": self.line,
                "folder": str(self.folder), "files": [str(p) for p in self.files],
                "shifts": self.shifts, "rows": self.rows, "label": self.label}


def month_dir(base_dir: Path, year: int, month: int, line: str) -> Path:
    r"""`<base>/<ライン>/yyyy.mm`。

    【VBA の並び(``yyyy.mm\ライン``)から入れ替えました】
    VBA は月が先でした。年月が先だと、**自分のラインの1年ぶんを見るのに
    12個のフォルダを開く**ことになります ── 引き継ぎでも月末の確認でも、
    人が続けて見るのは「同じラインの別の月」であって「同じ月の別ライン」
    ではありませんでした。

    集計CSV・印刷用HTML の出力先(`csv_export.line_dir`)も同じく
    **ラインが先**です。出力先が3つあって並べ方が3通りだと、探す人は
    毎回どれだったかを思い出すことになります。
    """
    return base_dir / safe_name(line) / month_folder_name(year, month)


def _write(path: Path, headers, rows) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(headers))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def write_month(repo: NippouRepository, *, year: int, month: int, line: str,
                base_dir: Path) -> MonthExport:
    """その月ぶんを3本のCSVにして返す。**空でも書きます。**

    空のまま書くのは、「その月は本当に何も無かった」と後から言えるように
    するためです。ファイルが無いと、書き出しに失敗したのか、もともと
    無かったのかが分かりません。

    出どころは**残してある集計**です(日ごとの書き出しと同じ道)。
    まだ集計の無い直は `summary.fill_missing` がその場で作ります ──
    月替わりは「この仕組みが入る前のぶん」を拾う場面でもあるので、
    ここで作れないと**その月だけ空のまま写される**ことになります。
    """
    from ..services import summary

    folder = month_dir(base_dir, year, month, line)
    stem = month_file_stem(year, month, line)
    start, end = _month_range(year, month)

    reports = [r for r in summary.for_period(repo, start, end, line)]
    lots = summary.lot_rows(repo, start, end, line)

    aggregate_rows = [aggregate_row_dict(summary.as_aggregate(r))
                      for r in sorted(reports, key=_report_sort_key)]

    # 停止は日ごとの `停止内訳_` と**同じ組み立て**(直ごと・記号ごと)。
    # 月別のほうだけ形が違うと、並べて見たときに突き合わせられない
    stop_rows = [
        {"記号": row["記号"], "内訳": row["内訳"], "分類": row["分類"],
         "回数": row["回数"], "時間(分)": row["停止時間(分)"]}
        for row in stop_rows_of(lots)
    ]

    # 明細も日ごとの書き出しと**同じ形**(`csv_export.DETAIL_FIELDNAMES`)
    detail_rows = detail_rows_of(lots, reports)

    files = [
        _write(folder / f"{stem}_集計.csv", FIELDNAMES, aggregate_rows),
        _write(folder / f"{stem}_停止集計.csv", STOP_HEADERS, stop_rows),
        _write(folder / f"{stem}_明細.csv", DETAIL_FIELDNAMES, detail_rows),
    ]
    # 列の説明も一緒に。**月別は「消えたあとに残る唯一の写し」**なので、
    # 読み方が付いていないと、何年か先に開いた人が列を読み解けません。
    # 消されていたら出し直します(中身が合っていれば書きません)
    csv_legend.ensure_monthly(folder, year, month, line)
    return MonthExport(year=year, month=month, line=line, folder=folder,
                       files=files, shifts=len(aggregate_rows),
                       rows=len(detail_rows))


def _month_range(year: int, month: int) -> tuple[date, date]:
    """その月の初日と終わりの日。**月末は暦で出します**(28/29/30/31)。"""
    from calendar import monthrange

    return date(year, month, 1), date(year, month, monthrange(year, month)[1])


def _report_sort_key(report):
    """並びは 日付 → 直(紙の順)。"""
    from ..logic.aggregation import shift_sort_key

    return (parse_business_date(report.work_date) or date.min,
            shift_sort_key(report.shift))
