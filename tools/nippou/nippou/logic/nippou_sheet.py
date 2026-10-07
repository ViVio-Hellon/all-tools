"""VBAの印刷フォーマット(日付シート)を、日報に写す

【実物はこういう形でした】
過去のブックの1ファイルは **1日ぶん・3直ぜんぶ**で、24時間の時間割です。

    E1  作業日       2026年8月26日
    E2  梱包ライン   機側 / HVC
    5〜7行           見出し(3段)
    8〜151行         6行で1時間 × 24時間 = 144行
    55 / 103 / 151   直合計(枚数・重量)。**ここが直の切れ目**

        8〜 54  1直 (07:00〜)
       56〜102  2直 (15:00〜)
      104〜150  3直 (23:00〜)

【直は区画ではなく、行の開始時刻で決めます】
行は**開始時刻の時間帯の枠**に置かれます。3直は 22:50 に始まるので、3直の
最初の行(22:50〜)は「22時台」の枠 ── **2直の区画**に入っていることがあります。
区画だけで分けていたころは、その行を2直として取り込んでいました(2直は
23:30 まで続き、3直は続きの行から始まる。3直が全停の日は 22:50〜07:00 の
全停が2直に入る)。そのせいで「最終時間まで入力がないのでは？」「休憩が
0分」が大量に出ていました。

いまは、区画の終わりにある**次の直の始まり以降に始まる行**を次の直へ移します
(次の直の始まりは「時間用」の直の時刻。読めなければ 07:00 / 15:00 / 22:50)。

【ラインはシートから採りません】
シートの呼び名(`機側` など)は書き方が揃っていないので(半角・書き間違い)、
入れる先のラインは**人が選びます**(`logic/line_names` の定義の表で当たりを
付けて、既定として出すだけ)。選ばせるほうが、当てを外したときに気づけます。

【ロット№を下へ引き継ぎません】
同じロットが時間帯をまたいで何行にも出てきます(10:30-11:15 のあと
11:15-11:30 …)。継続行にはロット№が書かれていません。

引き継ぐと、**1ページ12行の中に同じLOTが4回並びます** ── そのままでは
保存前のLOT重複チェック(`logic/validation`)に引っかかり、取り込んだページを
あとから開いて直せなくなります。VBA の `Agg_OutPut` もシートに書いて
あるまま(空欄は空欄)で集計していたので、**書いてあるとおり**にします。

枚数・重量・作業時間は行ごとに入っているので、**合計は引き継がなくても
合います**(実物10本で確かめました)。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .. import constants
from ..db.models import DetailRecord, HeaderRecord
from .csv_import import Page, Parsed, Problem
from .shift import DEFAULT_SHIFT_TIMES, parse_business_date

#: 見出しの下、中身が並ぶ範囲
FIRST_ROW, LAST_ROW = 8, 151

#: 直ごとの行の範囲と、その直の合計が載っている行。
#: **合計の行は中身として読みません**(枚数・重量しか入っていない)
SHIFT_BANDS: tuple[tuple[str, int, int, int], ...] = (
    ("1直", 8, 54, 55),
    ("2直", 56, 102, 103),
    ("3直", 104, 150, 151),
)

#: 1ページに入る行数(紙と同じ)
ROWS_PER_PAGE = constants.ROW_COUNT

#: シートの列 → 日報の欄。**紙の見出しのとおり**に対応させます
#: (`layout.py` の並びと突き合わせながら作りました)
COLUMNS: dict[str, str] = {
    "D": "lot",        # ロット№
    "E": "zai",        # 材・調質
    "F": "siz",        # 製品寸法 厚×幅×丈(1つの文字で入っています)
    "I": "ken",        # 検入枚数
    "J": "kz", "K": "kh",   # 梱包作業時間 開始 時・分
    "L": "sz", "M": "sh",   # 　　　　　　 終了 時・分
    "N": "hit",        # 作業人数
    "O": "ai",         # 合紙
    "P": "mai",        # 梱包数量 個装単位 枚数
    "Q": "tut",        # 梱包数量 梱包単位 包数
    "R": "vc",         # ＶＣ種別 両面・片面
    "Y": "con",        # 実績合計 枚数
    "Z": "wei",        # 実績合計 重量(kg)
    "AC": "tim",       # 作業時間(分)
    "AD": "uni",       # 単重(Kg)
    "AE": "others1",   # 用途コード
    "AF": "others2",   # 用途名
    "AG": "others3",   # 納入先
    "AH": "others4",   # 包装仕様NO
    "AI": "et",        # etc 反転・EX etc
    "AJ": "s", "AK": "th",      # 作業停止① 記号・時間
    "AL": "ss", "AM": "ths",    # 作業停止② 記号・時間
    "AN": "sth", "AO": "tht",   # 作業停止③ 記号・時間
    "AP": "reason",    # 作業コメント(行ごとの理由)
    "AQ": "others5",   # コイル縦割
    "AR": "others6",   # コイル横割
    "B": "keisu",      # 係数処理ﾛｯﾄ数
}

#: 作業者名。直の頭の行に入っています
WORKER_COLUMN = "AA"

#: 中身があるとみなす欄。**時刻の目盛り(C)と直合計(AA/AB)は数えません** ──
#: 目盛りは印刷のための飾りで、打った内容ではないので
_CONTENT_COLUMNS = tuple(COLUMNS)


@dataclass
class SheetHead:
    """シートの頭。**入れる前に画面へ出すもの。**"""

    report_date: str = ""
    line_text: str = ""           # シートに書いてある呼び名(機側 など)
    worker: str = ""
    sheet_name: str = ""

    def as_dict(self) -> dict:
        return {"report_date": self.report_date, "line_text": self.line_text,
                "worker": self.worker, "sheet_name": self.sheet_name}


@dataclass
class SheetTotals:
    """シート自身が持っている合計。**取り込んだ数と突き合わせる相手。**"""

    by_shift: dict[str, tuple[float, float]] = field(default_factory=dict)
    day_count: float = 0.0
    day_weight: float = 0.0
    stops: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"by_shift": {k: list(v) for k, v in self.by_shift.items()},
                "day_count": self.day_count, "day_weight": self.day_weight,
                "stops": dict(self.stops)}


def _num(text: str) -> float:
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return 0.0


def read_head(rows: dict[int, dict[str, str]], sheet_name: str = "") -> SheetHead:
    """作業日・ライン・作業者。"""
    return SheetHead(
        report_date=rows.get(1, {}).get("E", "").strip(),
        line_text=rows.get(2, {}).get("E", "").strip(),
        worker=rows.get(3, {}).get("E", "").strip(),
        sheet_name=sheet_name)


def read_totals(rows: dict[int, dict[str, str]]) -> SheetTotals:
    """シートが自分で持っている合計を拾う。**検算に使います。**"""
    head3, head4 = rows.get(3, {}), rows.get(4, {})
    return SheetTotals(
        by_shift={shift: (_num(rows.get(total, {}).get("AA", "")),
                          _num(rows.get(total, {}).get("AB", "")))
                  for shift, _s, _e, total in SHIFT_BANDS},
        day_count=_num(head3.get("AA", "")),
        day_weight=_num(head3.get("AB", "")),
        stops={"管理ロス停止": _num(head4.get("T", "")),
               "突発停止": _num(head4.get("V", "")),
               "ハンドリング停止": _num(head4.get("X", ""))})


def guess_line(line_text: str) -> str:
    """シートの呼び名 → ツールのライン名。**当たりを付けるだけ。**

    外したときに人が気づけるよう、これは画面の既定値にするだけで、
    実際に入れる先は選んでもらいます。
    """
    from . import line_names

    # 正規の呼び名(機側・L-1・ﾄｯﾄ…)を定義の表で。シートに VBA の名前(LS など)が
    # 書いてあれば正規へ読み替える(当たりを付けるだけなので)
    code = line_names.to_code(line_text)
    if code:
        return code
    text = line_names.upgrade(line_names.same(line_text))
    return text if text in constants.LINE_NAMES else ""


def _has_content(values: dict[str, str]) -> bool:
    return any((values.get(c, "") or "").strip() for c in _CONTENT_COLUMNS)


def _minutes(text: str) -> int | None:
    """「22:50」→ 1370。読めなければ None。"""
    try:
        hour, minute = str(text).strip().split(":")[:2]
        return int(hour) * 60 + int(minute)
    except (ValueError, AttributeError):
        return None


def shift_starts(times: dict | None = None) -> dict[str, int]:
    """直の始まり(分)。`times` は `shift_config` の形 ``{"2": ("15:00", "22:50"), …}``。

    読めない直は控え(07:00 / 15:00 / 22:50)にします。
    """
    fallback = {"1直": DEFAULT_SHIFT_TIMES.start1, "2直": DEFAULT_SHIFT_TIMES.start2,
                "3直": DEFAULT_SHIFT_TIMES.start3}
    out: dict[str, int] = {}
    for shift, default in fallback.items():
        given = (times or {}).get(shift[0])
        value = _minutes(given[0]) if given else None
        out[shift] = value if value is not None else _minutes(default)
    return out


def _row_start(values: dict[str, str]) -> int | None:
    """その行の開始時刻(分)。時・分が数でなければ None。"""
    try:
        return int(float(values.get("J", ""))) * 60 + int(float(values.get("K", "") or 0))
    except (TypeError, ValueError):
        return None


def _row_end(values: dict[str, str]) -> int | None:
    try:
        return int(float(values.get("L", ""))) * 60 + int(float(values.get("M", "") or 0))
    except (TypeError, ValueError):
        return None


def tail_for_next(rows: list[tuple], next_first: tuple | None, own: int, boundary: int) -> list[int]:
    """区画の行のうち、**次の直のもの**の番号(0 始まり)。

    `rows` は ``(開始分, 終了分, ロット)`` の並び、`next_first` は次の直の1行目の
    ``(開始分, ロット)``(無ければ None)。`own` はこの直の始まり、`boundary` は次の直の始まり。

    1. **次の直の始まり以降に始まる行**(22:50〜 の行・22:50〜07:00 の全停)
    2. **最後の行が次の直の始まりをまたぎ**(22:25〜23:30 など)、次の直の1行目が
       その終わりから始まる**続きの行(ロット空欄)**のとき、その行も。日報の1行目が
       続きの行になることは無いので、またいだ行は次の直の日報の1行目です
       (2026/1/10: 2直は全停、3直の人が 22:25 から始めていた)
    """
    if boundary <= own:
        return []
    picked = [i for i, (start, _end, _lot) in enumerate(rows)
              if start is not None and own <= start and start >= boundary]
    if picked or not rows or next_first is None:
        return picked
    start, end, _lot = rows[-1]
    first_start, first_lot = next_first
    if start is None or end is None or first_start is None or str(first_lot or "").strip():
        return picked
    if end < start:
        end += 24 * 60
    if start < boundary < end and end % (24 * 60) == first_start:
        return [len(rows) - 1]
    return picked


def parse_rows(rows: dict[int, dict[str, str]], line: str,
               sheet_name: str = "", starts: dict[str, int] | None = None) -> Parsed:
    """シートを、入れられる形(ページの並び)にする。**DBには触りません。**

    `line` は**入れる先のライン**です。シートに書いてある呼び名ではなく、
    人が選んだものを使います。`starts` は直の始まり(分。:func:`shift_starts`)。
    """
    starts = starts or shift_starts()
    found = Parsed()
    head = read_head(rows, sheet_name)

    if not head.report_date or parse_business_date(head.report_date) is None:
        found.problems.append(Problem(
            1, f"作業日(E1)が読めません: {head.report_date or '(空)'}"))
        return found
    if not line.strip():
        found.problems.append(Problem(2, "入れる先のラインが選ばれていません"))
        return found

    bands = []
    for shift, start, end, _total in SHIFT_BANDS:
        worker = rows.get(start, {}).get(WORKER_COLUMN, "").strip()
        entries = []
        for number in range(start, end + 1):
            values = rows.get(number, {})
            if not values:
                continue
            found.rows_read += 1
            if _has_content(values):
                entries.append((number, values))
        bands.append([shift, worker, entries])

    # 区画の終わりにある「次の直の始まり以降に始まる行」は次の直のもの
    # (3直の 22:50〜 は2直の区画の「22時台」の枠に置かれている。冒頭の説明)
    for here, after in zip(bands, bands[1:]):
        own, boundary = starts.get(here[0]), starts.get(after[0])
        if own is None or boundary is None or boundary <= own:
            continue
        nxt = after[2][0][1] if after[2] else None
        picked = tail_for_next(
            [(_row_start(v), _row_end(v), v.get("D", "")) for _n, v in here[2]],
            (_row_start(nxt), nxt.get("D", "")) if nxt else None, own, boundary)
        moved = [here[2][i] for i in picked]
        if not moved:
            continue
        here[2] = [e for e in here[2] if e not in moved]
        after[2] = moved + after[2]
        if not after[1]:
            after[1] = next((v.get(WORKER_COLUMN, "").strip() for _n, v in moved
                             if v.get(WORKER_COLUMN, "").strip()), "")
        first = moved[0][1]
        when = f"{boundary // 60:02d}:{boundary % 60:02d}"
        how = (f"{after[0]}の始まり({when})以降に始まる行" if (_row_start(first) or 0) >= boundary
               else f"{after[0]}の始まり({when})をまたぎ、{after[0]}が続きの行から始まっている行")
        found.notes.append(
            f"{how} {len(moved)}行を{after[0]}へ移しました(シートでは{here[0]}の区画の"
            f"{first.get('J', '')}時台の枠にありました)")

    for shift, worker, entries in bands:
        for index, chunk in enumerate(_chunks(entries), start=1):
            found.pages.append(_page(head.report_date, line, shift, index,
                                     chunk, worker))

    # **検算**: 取り込む行の枚数・重量の合計が、シート自身の日合計(AA3 / AB3)と
    # 合うか。直の間で行を移しても日合計は変わらないので、ここが合わなければ
    # 読み落とし・読み違いがあります
    totals = read_totals(rows)
    if totals.day_count or totals.day_weight:
        count = sum(_num(v.get("Y", "")) for _s, _w, es in bands for _n, v in es)
        weight = sum(_num(v.get("Z", "")) for _s, _w, es in bands for _n, v in es)
        if abs(count - totals.day_count) > 1e-6 or abs(weight - totals.day_weight) > 0.05:
            found.notes.append(
                f"⚠ シートの日合計と合いません: 取り込む行の合計 {count:g}枚 {weight:.1f}kg / "
                f"シートの日合計(AA3・AB3) {totals.day_count:g}枚 {totals.day_weight:.1f}kg。"
                "シートと見比べてください")
    return found


def _chunks(entries: list):
    """12行ずつに切る(紙と同じ)。"""
    for start in range(0, len(entries), ROWS_PER_PAGE):
        yield entries[start:start + ROWS_PER_PAGE]


def _page(report_date: str, line: str, shift: str, page: int,
          chunk: list, worker: str) -> Page:
    header = HeaderRecord(report_date=report_date, line=line, shift=shift,
                          page=page, worker=worker)
    made = Page(header=header)
    for row_no, (_number, values) in enumerate(chunk, start=1):
        detail = DetailRecord(report_date=report_date, line=line, shift=shift,
                              page=page, row_no=row_no)
        for column, name in COLUMNS.items():
            text = (values.get(column, "") or "").strip()
            if text:
                setattr(detail, name, text)
        made.details.append(detail)
    return made


def parse_file(path, line: str, starts: dict[str, int] | None = None) -> Parsed:
    """xlsx を読んで、入れられる形にする。`starts` は直の始まり(:func:`shift_starts`)。"""
    from . import xlsx_sheet

    try:
        cells = xlsx_sheet.read_cells(path)
        name = xlsx_sheet.sheet_name(path)
    except xlsx_sheet.NotXlsx as exc:
        found = Parsed()
        found.problems.append(Problem(0, str(exc)))
        return found
    return parse_rows(xlsx_sheet.rows_of(cells), line, name, starts)


def read_file_head(path) -> SheetHead:
    """入れる前に、シートの頭だけ見る(ラインの既定を出すため)。"""
    from . import xlsx_sheet

    try:
        rows = xlsx_sheet.rows_of(xlsx_sheet.read_cells(path))
        return read_head(rows, xlsx_sheet.sheet_name(path))
    except xlsx_sheet.NotXlsx:
        return SheetHead()
