"""xlsx を**標準ライブラリだけで**読む

【なぜ openpyxl を入れないのか】
この移植は `requirements.txt` を Flask と waitress の2つに保つと決めて
あります。現場のPCへ配るものなので、入れるものが増えるほど「動かない」
理由が増えます。

`.xlsx` は **ZIP の中に XML が入っているだけ**なので、`zipfile` と
`xml.etree` で開けます ── 読むだけならライブラリは要りません。

    xl/workbook.xml       シートの名前と並び
    xl/worksheets/*.xml   セルの中身
    xl/sharedStrings.xml  文字列は別置き(同じ文字を何度も書かないため)

【書きません】
読む専用です。過去データを取り込むために中身を見るだけで、xlsx を
作ったり直したりはしません ── それをやろうとした瞬間に openpyxl が
要ります(書式・数式・スタイルの整合が要るので)。

【数はどう返すか】
**文字のまま返します。** 日報の欄は文字で持っているので(打った形を
そのまま残すため)、ここで数にしても入れるときに文字へ戻すことになります。
`17` は "17"、`0.505` は "0.505" です。
"""
from __future__ import annotations

import re
import zipfile
from xml.etree import ElementTree as ET

#: SpreadsheetML の名前空間
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_DOC_REL = ("{http://schemas.openxmlformats.org/officeDocument/2006/"
            "relationships}")

_CELL = re.compile(r"^([A-Z]+)(\d+)$")


class NotXlsx(Exception):
    """xlsx として開けないもの。**理由を持って断ります。**"""


def column_index(name: str) -> int:
    """`A`→1, `Z`→26, `AA`→27。並べ替えに使います。"""
    total = 0
    for ch in name:
        total = total * 26 + (ord(ch) - 64)
    return total


def split_ref(ref: str) -> tuple[str, int]:
    """`AB12` → `("AB", 12)`。読めなければ `("", 0)`。"""
    found = _CELL.match(ref or "")
    return (found.group(1), int(found.group(2))) if found else ("", 0)


def _shared_strings(book: zipfile.ZipFile) -> list[str]:
    """文字列の置き場。**無いブックもあります**(全部が数のとき)。"""
    if "xl/sharedStrings.xml" not in book.namelist():
        return []
    root = ET.fromstring(book.read("xl/sharedStrings.xml"))
    return ["".join(t.text or "" for t in si.iter(f"{_NS}t"))
            for si in root.findall(f"{_NS}si")]


def _first_sheet_path(book: zipfile.ZipFile) -> tuple[str, str]:
    """最初のシートの (名前, ZIPの中の道)。

    `sheet1.xml` と決め打ちにしません ── 並べ替えたブックでは番号と
    並び順が一致しないことがあるので、関係ファイルから辿ります。
    """
    names = book.namelist()
    try:
        workbook = ET.fromstring(book.read("xl/workbook.xml"))
        rels = ET.fromstring(book.read("xl/_rels/workbook.xml.rels"))
    except KeyError as exc:                       # xlsx の形をしていない
        raise NotXlsx(f"xlsx の中身が足りません({exc})") from exc

    targets = {rel.get("Id"): rel.get("Target")
               for rel in rels.iter(f"{_REL_NS}Relationship")}
    for sheet in workbook.iter(f"{_NS}sheet"):
        target = targets.get(sheet.get(f"{_DOC_REL}id"), "")
        path = target if target.startswith("xl/") else f"xl/{target.lstrip('/')}"
        if path in names:
            return sheet.get("name", ""), path
    raise NotXlsx("シートが1枚もありません")


def sheet_name(path) -> str:
    """最初のシートの名前(`2026年8月26日_機側` など)。"""
    with zipfile.ZipFile(path) as book:
        return _first_sheet_path(book)[0]


def read_cells(path) -> dict[str, str]:
    """`{"A1": "値"}`。**中身のあるセルだけ**返します。

    空文字や空白だけのセルは入れません ── 「そこに何か書いてあるか」で
    行を拾うので、見た目が空のものは無いのと同じに扱います。
    """
    try:
        book = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise NotXlsx("xlsx ではありません(ZIPとして開けません)") from exc

    with book:
        strings = _shared_strings(book)
        _name, sheet_path = _first_sheet_path(book)
        root = ET.fromstring(book.read(sheet_path))

        found: dict[str, str] = {}
        for cell in root.iter(f"{_NS}c"):
            ref = cell.get("r") or ""
            text = _value_of(cell, strings)
            if ref and text.strip():
                found[ref] = text.strip()
    return found


def _value_of(cell, strings: list[str]) -> str:
    """セル1つの中身。数式は**計算済みの値**のほうを採ります。"""
    kind = cell.get("t")
    if kind == "inlineStr":
        node = cell.find(f"{_NS}is")
        return ("".join(t.text or "" for t in node.iter(f"{_NS}t"))
                if node is not None else "")
    value = cell.find(f"{_NS}v")
    if value is None or value.text is None:
        return ""
    if kind == "s":                               # 共有文字列の番号
        try:
            return strings[int(value.text)]
        except (ValueError, IndexError):
            return ""
    if kind == "e":                               # #REF! などのエラー値
        return ""
    if kind in (None, "", "n"):
        return _short_number(value.text)
    return value.text


def _short_number(text: str) -> str:
    """数のセルを、Excel が見せる形に近い短い書き方へ。

    xlsx には 2進の小数がそのまま 17 桁で入っている(``250.04313999999999``・
    ``1188.9000000000001``)。そのまま取り込むと画面にその桁で出た。
    ``repr(float)`` は**同じ値に戻る一番短い書き方**なので、値は変わらない。
    """
    if "." not in text or "e" in text.lower():
        return text
    try:
        number = float(text)
    except ValueError:
        return text
    if number != number or number in (float("inf"), float("-inf")):
        return text
    short = repr(number)
    return short if len(short) < len(text) else text


def rows_of(cells: dict[str, str]) -> dict[int, dict[str, str]]:
    """セルの一覧を行ごとに畳む。`{12: {"D": "…", "E": "…"}}`"""
    rows: dict[int, dict[str, str]] = {}
    for ref, text in cells.items():
        column, number = split_ref(ref)
        if column:
            rows.setdefault(number, {})[column] = text
    return rows
