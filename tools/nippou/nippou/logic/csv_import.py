"""CSVから日報を戻す ── **Excelにしか無い過去のぶんを、この道具に載せる**

【なぜ要るのか】
このツールに切り替える前の日報は、VBAのブックの中にしかありません。
紙もグラフも集計も、`daily_header` / `daily_detail` から作っているので、
**そこに戻せなければ過去は一生出てきません。**

逆に言えば、戻す口を1つ作れば全部の出口に載ります ── 保存 → 集計 →
出口、の一直線に揃えてあるので、グラフ用と印刷用を別々に作る必要が
ありません。

    集計明細CSV ──▶ daily_header / daily_detail
                       ↓ 集計を作り直す(services/summary.py)
                     紙 ・ グラフ ・ 集計管理の表 ・ 集計CSV

【なぜ列の位置ではなく名前で合わせるのか】
Excelのシートをそのまま位置で読む案もありました。**やめました** ──
人が書き出したCSVには、表題の行が残っていたり、列が1つずれていたり、
月によって並びが違ったりします。位置で読むと、それが**静かに**ずれた
データになります(納入先の欄に包装仕様書Noが入る、など)。

名前で合わせれば、合わないときに「この列が見つかりません」と言えます。
**間違ったまま入るより、入らないほうがましです。**

そのかわり、名前は寛容に見ます ── 全角/半角、空白、丸かっこの違い、
それに VBA の集計シートで使われていた別の呼び名も受け取ります。

【前の名前で書いたCSVも読めます(v4.13.0)】
VBA・変換器(excel-layout-csv-converter)・v4.12 までのこの道具が書き出したCSVは、
ライン列が前の名前(`LS` `TOT` `BALA` `MARU` `L1`)です。読むときに正規の呼び名
(`機側` `トット` `バランサー` `中板` `L-1`)へ揃えます(`line_names.upgrade`)。

【この道具が書き出したCSVも読めます】
`集計明細_<日付>_<ライン>.csv` と、月別フォルダの `〜_明細.csv` は
そのまま読み戻せます。**月別に書き出したものが控えとして使える**という
ことで、これは過去データの取り込みより使う場面が多いかもしれません。

【行ごとの理由】
CSVには `作業コメント` が1つあるだけですが、中身は
`3行目: 棚卸し準備 / 7行目: 点検表の差し替え` の形です。`reasons.split`
で行ごとに割り戻してから `DetailRecord.reason` へ入れます ── まとめた
ままヘッダに置くと、**次に保存したときに行から作り直されて消えます。**
"""
from __future__ import annotations

import csv
import io
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .. import layout
from ..db.models import DetailRecord, HeaderRecord
from . import reasons
from .shift import parse_business_date
from . import line_names

#: 直の呼び名。**紙に出る4つだけ**を受け取ります ── 「1」「1直」の
#: どちらで書かれていても同じ直です
SHIFT_ALIASES: dict[str, str] = {
    "1": "1直", "1直": "1直", "一直": "1直",
    "2": "2直", "2直": "2直", "二直": "2直",
    "3": "3直", "3直": "3直", "三直": "3直",
    "日勤": "日勤", "昼": "日勤", "日": "日勤",
}


def _fold(text: str) -> str:
    """見出しを突き合わせる形にする。

    全角→半角、大文字小文字、空白と記号の違いを均します ──
    `ＶＣ種別 両面・片面` と `VC種別両面片面` を同じものとして扱うため。
    **意味のある文字だけ**を残すので、`作業停止① 記号` と
    `作業停止①記号` も揃います。
    """
    folded = unicodedata.normalize("NFKC", text or "").lower()
    drop = " 　\t()（）[]【】・/／,、.。_-‐―ー:："
    return "".join(ch for ch in folded if ch not in drop)


def _column_names() -> dict[str, str]:
    """この道具が書くCSVの見出し → 中で使う名前。"""
    names = {"日付": "日付", "ライン": "ライン", "直": "直",
             "ページ": "ページ", "行": "行", "係数処理ﾛｯﾄ数": "keisu",
             "作業者": "worker", "昼稼働": "day_shift",
             "作業コメント": "reason", "引当番号": "hiki_no"}
    for column in layout.COLUMNS:
        names[layout.csv_header(column)] = column.field
    for column in layout.EXTRA_COLUMNS:
        names[layout.csv_header(column)] = column.family
    return names


#: 別の呼び名。**VBAの集計シートと、人が付けがちな短い名前。**
#: 左は畳んだ形(`_fold`)で持ちます
#:
#: **「頁」は残します。** 見出しを「ページ」に言い換えたのは 3.47.0 で、
#: それより前に出したCSVは「頁」で書かれています。取り込む側が古い名前を
#: 知らないと、**自分が出したファイルを自分で読めなくなります。**
ALIASES: dict[str, str] = {
    "報告日": "日付", "作業日": "日付", "年月日": "日付",
    "設備": "ライン", "ライン名": "ライン",
    "勤務": "直", "直別": "直",
    "頁": "ページ", "頁番号": "ページ", "ページ番号": "ページ",
    "行番号": "行", "行no": "行",
    "lotno": "lot", "ロットno": "lot", "ロット番号": "lot",
    "材質": "zai", "調質": "zai",
    "寸法": "siz", "製品寸法": "siz", "厚幅丈": "siz",
    "検入": "ken",
    "人数": "hit", "作業人数": "hit",
    "合紙有無": "ai",
    "包装仕様no": "others4", "包装仕様": "others4",
    "コイル横割": "others6",
    "理由": "reason", "コメント": "reason",
    "係数lot数": "keisu", "係数処理lot数": "keisu",
    "単重kg": "uni",
}


def _lookup() -> dict[str, str]:
    """畳んだ見出し → 中で使う名前。見本の名前と別名を1つの表に。"""
    table = {_fold(head): field for head, field in _column_names().items()}
    for alias, target in ALIASES.items():
        # 別名が指すのは**見本の見出し**なので、そこからもう一段引く
        table.setdefault(_fold(alias),
                         _column_names().get(target, target))
    return table


#: 無いと行の置き場所が決まらない列
REQUIRED: tuple[str, ...] = ("日付", "ライン", "直")


@dataclass
class Problem:
    """読めなかった1件。**落とした理由を必ず持ちます。**"""

    row: int                      # CSVの行番号(見出しを1行目と数える)
    reason: str
    text: str = ""

    def as_dict(self) -> dict:
        return {"row": self.row, "reason": self.reason, "text": self.text}


@dataclass
class Page:
    """取り込む1ページぶん(ヘッダ + 明細)。"""

    header: HeaderRecord
    details: list[DetailRecord] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str, str, int]:
        return self.header.key()

    @property
    def label(self) -> str:
        d, line, shift, page = self.key
        return f"{d} {line_names.label(line)} {shift} {page}ページ"


@dataclass
class Parsed:
    """読んだ結果。**書く前に、これを見せます。**"""

    pages: list[Page] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    missing_columns: list[str] = field(default_factory=list)
    rows_read: int = 0

    @property
    def ok(self) -> bool:
        """入れられる形か。**見出しが合わなければ1行も入れません。**"""
        return not self.missing_columns and bool(self.pages)

    @property
    def row_count(self) -> int:
        return sum(len(p.details) for p in self.pages)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "rows_read": self.rows_read,
                "pages": [p.label for p in self.pages],
                "row_count": self.row_count,
                "missing_columns": list(self.missing_columns),
                "problems": [p.as_dict() for p in self.problems]}


def template_header() -> str:
    """見本の1行目。**これを自分のデータの上に貼れば読めます。**"""
    from ..reporting.csv_export import DETAIL_FIELDNAMES

    return ",".join(DETAIL_FIELDNAMES)


def _shift_of(text: str) -> str:
    """「1」「1直」「一直」→「1直」。読めなければ空。"""
    raw = (text or "").strip()
    if raw in SHIFT_ALIASES:
        return SHIFT_ALIASES[raw]
    folded = _fold(raw)
    return SHIFT_ALIASES.get(folded, "")


def _int_of(text: str, default: int) -> int:
    try:
        return int(float(str(text).strip()))
    except (TypeError, ValueError):
        return default


def _has_content(values: dict[str, str]) -> bool:
    """打ってある行か。鍵とページ・行しか無い行は、紙の余白と同じです。"""
    watched = [c.field for c in layout.COLUMNS]
    watched += [c.family for c in layout.EXTRA_COLUMNS]
    watched.append("keisu")
    return any((values.get(name) or "").strip() for name in watched)


def parse(text: str) -> Parsed:
    """CSVの中身を読んで、入れられる形にする。**DBには触りません。**"""
    found = Parsed()
    table = _lookup()
    reader = csv.reader(io.StringIO(text))
    try:
        head = next(reader)
    except StopIteration:
        found.problems.append(Problem(0, "ファイルが空です"))
        return found

    # 見出し → その列が何か。読めない見出しは黙って捨てます
    # (人が足したメモの列を、欠けているとは言いません)
    columns = {index: table[_fold(name)]
               for index, name in enumerate(head) if _fold(name) in table}
    have = set(columns.values())
    found.missing_columns = [name for name in REQUIRED if name not in have]
    if found.missing_columns:
        return found

    pages: dict[tuple[str, str, str, int], Page] = {}
    order: list[tuple[str, str, str, int]] = []
    for number, row in enumerate(reader, start=2):
        if not any((cell or "").strip() for cell in row):
            continue                              # 空行は黙って飛ばす
        found.rows_read += 1
        values = {columns[i]: (row[i] if i < len(row) else "")
                  for i in columns}

        problem = _row_problem(values)
        if problem:
            found.problems.append(Problem(number, problem,
                                          (values.get("lot") or "").strip()))
            continue
        if not _has_content(values):
            continue                              # 紙の余白。落としたとは言わない

        _add(pages, order, values, number, found)

    found.pages = [pages[key] for key in order]
    _spread_reasons(found.pages)
    return found


def _row_problem(values: dict[str, str]) -> str:
    """その行を置けない理由。置けるなら空。"""
    date_text = (values.get("日付") or "").strip()
    if not date_text:
        return "日付がありません"
    if parse_business_date(date_text) is None:
        return f"日付が読めません: {date_text}"
    if not (values.get("ライン") or "").strip():
        return "ラインがありません"
    if not _shift_of(values.get("直", "")):
        return f"直が読めません: {(values.get('直') or '').strip()}"
    return ""


def _add(pages: dict, order: list, values: dict[str, str], number: int,
         found: Parsed) -> None:
    """1行を、そのページへ足す。"""
    # ラインは**正規の呼び名へ揃える**(v4.13.0)── VBA と変換器(excel-layout-csv-converter)
    # の CSV は前の名前(LS・TOT …)で書いてあるので、そのままだと別のラインになる
    key = ((values["日付"]).strip(), line_names.upgrade((values["ライン"]).strip()),
           _shift_of(values["直"]), _int_of(values.get("ページ", ""), 1))
    page = pages.get(key)
    if page is None:
        page = Page(header=HeaderRecord(
            report_date=key[0], line=key[1], shift=key[2], page=key[3],
            worker=(values.get("worker") or "").strip(),
            day_shift=(values.get("day_shift") or "").strip(),
            reason=(values.get("reason") or "").strip()))
        pages[key] = page
        order.append(key)
    else:
        # 直に1つしかないものは**行ごとに繰り返して**書き出しています。
        # 先に出たほうを採り、空なら後から埋めます
        _fill(page.header, values)

    detail = DetailRecord(report_date=key[0], line=key[1], shift=key[2],
                          page=key[3],
                          row_no=_int_of(values.get("行", ""),
                                         len(page.details) + 1))
    for column in layout.COLUMNS:
        setattr(detail, column.field,
                (values.get(column.field) or "").strip())
    for column in layout.EXTRA_COLUMNS:
        setattr(detail, column.family,
                (values.get(column.family) or "").strip())
    detail.keisu = (values.get("keisu") or "").strip()
    detail.hiki_no = (values.get("hiki_no") or "").strip()
    page.details.append(detail)


def _fill(header: HeaderRecord, values: dict[str, str]) -> None:
    for name, key in (("worker", "worker"), ("day_shift", "day_shift"),
                      ("reason", "reason")):
        if not getattr(header, name):
            setattr(header, name, (values.get(key) or "").strip())


def _spread_reasons(pages: Iterable[Page]) -> None:
    """まとめてある理由を、行ごとに割り戻す。

    CSVの `作業コメント` は `3行目: … / 7行目: …` の形です。**まとめた
    ままヘッダに置くと、次に保存したときに行から作り直されて消えます。**
    """
    for page in pages:
        by_row = reasons.split(page.header.reason)
        if not by_row:
            continue
        for detail in page.details:
            text = by_row.get(detail.row_no, "")
            if text:
                detail.reason = text


def parse_file(path, encoding: Optional[str] = None) -> Parsed:
    """ファイルから読む。**文字コードは決め打ちにしません。**

    BOM付きUTF-8(この道具が書いたもの)と、cp932(Excelが「CSV」で
    保存したもの)の両方が来ます。前者から順に試します。
    """
    with open(path, "rb") as f:
        raw = f.read()
    for name in ([encoding] if encoding else ["utf-8-sig", "cp932", "utf-8"]):
        try:
            return parse(raw.decode(name))
        except UnicodeDecodeError:
            continue
    found = Parsed()
    found.problems.append(Problem(0, "文字コードが読めません"
                                     "(BOM付きUTF-8 か cp932 で保存してください)"))
    return found
