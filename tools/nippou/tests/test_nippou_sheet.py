"""VBAの印刷フォーマット(xlsx)を取り込む

【実物で確かめたこと】
移植のときに、現場の日付シート10本(5日 × コイル/板)を通しました。
**シート自身が持っている合計と、取り込んだあとの集計が全部一致**
しました ── 直合計(枚数・重量)、日合計、停止の3分類、どれも。

その10本はここには置きません。**作業者の名前とお客様の名前が入って
いる**ので、リポジトリに入れるものではありません。代わりに、同じ形の
シートをここで組み立てて確かめます。

【なぜ形を固定するのか】
このシートは「6行で1時間、55/103/151行が直の切れ目」という
**位置に意味がある**作りです。1行ずれると、2直の行が1直に入ったまま
静かに通ります ── 数が合っているので、誰も気づきません。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import nippou_sheet as ns
from nippou.logic import xlsx_sheet

DAY = "2026年8月26日"

_BOOK = """<?xml version="1.0"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <sheets><sheet name="{name}" sheetId="1" r:id="rId1"/></sheets></workbook>"""

_RELS = """<?xml version="1.0"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Target="worksheets/sheet1.xml"
  Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>
</Relationships>"""


def write_xlsx(path: Path, cells: dict[str, str], name: str = "日付_機側") -> Path:
    """`{"A1": "値"}` から xlsx を作る。**この道具は書きません**が、
    テストで読ませる相手が要るので、ここだけ最小の形で組み立てます。"""
    rows: dict[int, list[str]] = {}
    for ref, text in cells.items():
        column, number = xlsx_sheet.split_ref(ref)
        safe = (str(text).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;"))
        rows.setdefault(number, []).append(
            f'<c r="{ref}" t="inlineStr"><is><t>{safe}</t></is></c>')
    body = "".join(
        f'<row r="{n}">' + "".join(rows[n]) + "</row>" for n in sorted(rows))
    sheet = ('<?xml version="1.0"?><worksheet xmlns="http://schemas.'
             'openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
             + body + "</sheetData></worksheet>")
    with zipfile.ZipFile(path, "w") as book:
        book.writestr("xl/workbook.xml", _BOOK.format(name=name))
        book.writestr("xl/_rels/workbook.xml.rels", _RELS)
        book.writestr("xl/worksheets/sheet1.xml", sheet)
    return path


def sheet_cells(**extra) -> dict[str, str]:
    """実物と同じ形の、小さな1日ぶん。"""
    cells = {
        "E1": DAY, "E2": "機側", "E3": "大竹 及川",
        "T4": "60", "V4": "30", "X4": "20",
        "AA3": "20", "AB3": "2000",
        # 1直(8〜54)に1行、2直(56〜102)に1行、3直(104〜150)に1行
        "C8": "07:00", "AA8": "1直の人",
        "D8": "L6101N0", "E8": "52S- O", "F8": "1.500×195.0×ｺｲﾙ", "I8": "12",
        "J8": "7", "K8": "0", "L8": "8", "M8": "30", "N8": "2",
        "P8": "3", "Q8": "1", "Y8": "10", "Z8": "1000", "AC8": "90",
        "AD8": "100", "AE8": "H176", "AF8": "JISIMI", "AG8": "ｱｲｴﾑｱｲ",
        "AH8": "1C1282", "AI8": "EX", "AJ8": "0", "AK8": "60",
        "AP8": "立ち合い", "AQ8": "2", "AR8": "6", "B8": "1",
        "AA55": "10", "AB55": "1000",
        "C56": "15:00", "AA56": "2直の人",
        "D56": "L4336H0", "Y56": "10", "Z56": "1000", "AC56": "60",
        "AJ56": "タ", "AK56": "30",
        "AA103": "10", "AB103": "1000",
        "C104": "23:00", "AA104": "3直の人",
        "AJ104": "A", "AK104": "20", "AC104": "0",
        "AA151": "0", "AB151": "0",
    }
    cells.update(extra)
    return cells


class ReadTests(unittest.TestCase):
    """xlsx を標準ライブラリだけで読む。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def book(self, **extra) -> Path:
        return write_xlsx(self.tmp / "日付.xlsx", sheet_cells(**extra))

    def test_追加ライブラリを取り込まない(self) -> None:
        """**増やさない**という決まりを、ここで縛ります。

        `openpyxl` を入れれば楽に読めますが、現場のPCへ配るものなので、
        入れるものが増えるほど「動かない」理由が増えます。
        (説明の文には名前が出るので、**取り込みの行だけ**を見ます)
        """
        import re

        root = Path(xlsx_sheet.__file__).resolve().parent.parent
        pattern = re.compile(r"^\s*(?:import|from)\s+(\w+)", re.M)
        outside = set()
        for source in root.rglob("*.py"):
            for name in pattern.findall(source.read_text(encoding="utf-8")):
                outside.add(name)
        self.assertNotIn("openpyxl", outside)
        self.assertNotIn("xlrd", outside)
        self.assertNotIn("pandas", outside)
        # 使っているのは標準ライブラリだけ
        self.assertIn("zipfile", outside)

    def test_セルが読める(self) -> None:
        cells = xlsx_sheet.read_cells(self.book())
        self.assertEqual(cells["E1"], DAY)
        self.assertEqual(cells["D8"], "L6101N0")

    def test_空のセルは持たない(self) -> None:
        """「そこに何か書いてあるか」で行を拾うので、空は無いのと同じ。"""
        cells = xlsx_sheet.read_cells(write_xlsx(
            self.tmp / "空.xlsx", {"A1": "あり", "B1": "  ", "C1": ""}))
        self.assertEqual(set(cells), {"A1"})

    def test_シートの名前が読める(self) -> None:
        self.assertEqual(xlsx_sheet.sheet_name(self.book()), "日付_機側")

    def test_列の番号(self) -> None:
        self.assertEqual(xlsx_sheet.column_index("A"), 1)
        self.assertEqual(xlsx_sheet.column_index("Z"), 26)
        self.assertEqual(xlsx_sheet.column_index("AA"), 27)
        self.assertEqual(xlsx_sheet.column_index("AR"), 44)

    def test_xlsxでなければ理由を言う(self) -> None:
        path = self.tmp / "ちがう.xlsx"
        path.write_text("これはExcelではありません", encoding="utf-8")
        with self.assertRaises(xlsx_sheet.NotXlsx):
            xlsx_sheet.read_cells(path)


class MappingTests(unittest.TestCase):
    """シート → 日報。**位置に意味がある。**"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def parsed(self, line: str = "機側", **extra):
        path = write_xlsx(self.tmp / "日付.xlsx", sheet_cells(**extra))
        return ns.parse_file(path, line)

    def test_1ファイルで3直ぶん入る(self) -> None:
        """**1日ぶん・3直まとめて**が1ファイルです。"""
        found = self.parsed()
        self.assertEqual([(p.header.shift, p.header.page) for p in found.pages],
                         [("1直", 1), ("2直", 1), ("3直", 1)])

    def test_直の切れ目は行で決まる(self) -> None:
        """55 / 103 / 151 が直の切れ目。**1行ずれると直が入れ替わります。**"""
        found = self.parsed()
        by_shift = {p.header.shift: p for p in found.pages}
        self.assertEqual(by_shift["1直"].details[0].lot, "L6101N0")
        self.assertEqual(by_shift["2直"].details[0].lot, "L4336H0")
        self.assertEqual(by_shift["3直"].details[0].s, "A")

    def test_紙の欄に正しく入る(self) -> None:
        detail = self.parsed().pages[0].details[0]
        self.assertEqual(detail.lot, "L6101N0")
        self.assertEqual(detail.zai, "52S- O")
        self.assertEqual(detail.siz, "1.500×195.0×ｺｲﾙ")   # F列に1つの文字で
        self.assertEqual(detail.ken, "12")
        self.assertEqual((detail.kz, detail.kh), ("7", "0"))
        self.assertEqual((detail.sz, detail.sh), ("8", "30"))
        self.assertEqual(detail.hit, "2")
        self.assertEqual((detail.mai, detail.tut), ("3", "1"))
        self.assertEqual((detail.con, detail.wei), ("10", "1000"))
        self.assertEqual(detail.tim, "90")
        self.assertEqual(detail.uni, "100")
        self.assertEqual(detail.et, "EX")
        self.assertEqual((detail.s, detail.th), ("0", "60"))
        self.assertEqual(detail.keisu, "1")

    def test_紙に載らない欄も入る(self) -> None:
        """用途コード・納入先・コイル縦割は**集計にしか出ない**欄です。"""
        detail = self.parsed().pages[0].details[0]
        self.assertEqual(detail.others1, "H176")     # 用途コード
        self.assertEqual(detail.others3, "ｱｲｴﾑｱｲ")   # 納入先
        self.assertEqual(detail.others4, "1C1282")   # 包装仕様NO
        self.assertEqual(detail.others5, "2")        # コイル縦割
        self.assertEqual(detail.others6, "6")        # コイル横割

    def test_理由は行ごとに入る(self) -> None:
        self.assertEqual(self.parsed().pages[0].details[0].reason, "立ち合い")

    def test_作業者は直の頭から(self) -> None:
        by_shift = {p.header.shift: p.header.worker for p in self.parsed().pages}
        self.assertEqual(by_shift["1直"], "1直の人")
        self.assertEqual(by_shift["2直"], "2直の人")

    def test_時刻の目盛りは中身に数えない(self) -> None:
        """C列の 07:00 は印刷のための飾りで、打った内容ではありません。"""
        found = self.parsed(**{"C14": "08:00", "C20": "09:00"})
        self.assertEqual(len(found.pages[0].details), 1)

    def test_ロット番号を下へ引き継がない(self) -> None:
        """継続行(時間だけの行)にロットを入れると、**1ページに同じLOTが並びます。**

        そのままでは保存前のLOT重複チェックに引っかかり、取り込んだページを
        あとから開いて直せなくなります。VBA の集計もシートに書いてある
        まま(空欄は空欄)でした。
        """
        found = self.parsed(**{"J14": "8", "K14": "30", "L14": "9", "M14": "0",
                               "Y14": "5", "Z14": "500", "AC14": "30"})
        lots = [d.lot for d in found.pages[0].details]
        self.assertEqual(lots, ["L6101N0", ""])

    def test_12行を超えたら次のページ(self) -> None:
        """紙は12行で1枚。**13行目からは次のページ**です。"""
        extra = {f"Y{n}": "1" for n in range(9, 24)}   # 1直の枠に15行足す
        found = self.parsed(**extra)
        pages = [p for p in found.pages if p.header.shift == "1直"]
        self.assertEqual([p.header.page for p in pages], [1, 2])
        self.assertEqual(len(pages[0].details), 12)

    def test_何も無い直はページを作らない(self) -> None:
        """時刻の目盛りしか無い直。**空の紙を残さない。**"""
        cells = sheet_cells()
        for ref in list(cells):
            if ref.endswith("104") and ref != "C104":
                cells.pop(ref)
        path = write_xlsx(self.tmp / "空直.xlsx", cells)
        found = ns.parse_file(path, "機側")
        self.assertNotIn("3直", [p.header.shift for p in found.pages])

    def test_作業日が読めなければ入れない(self) -> None:
        found = self.parsed(**{"E1": "よめない"})
        self.assertFalse(found.ok)
        self.assertIn("作業日", found.problems[0].reason)

    def test_ラインが選ばれていなければ入れない(self) -> None:
        found = self.parsed(line="")
        self.assertFalse(found.ok)
        self.assertIn("ライン", found.problems[0].reason)


class ShiftBoundaryTests(unittest.TestCase):
    """直は区画ではなく、行の開始時刻で決める(現場の取り込みで見つけた件)。

    行は**開始時刻の時間帯の枠**に置かれます。3直は 22:50 に始まるので、3直の
    最初の行は「22時台」の枠 ── 2直の区画(98〜102行)に入っていることがあります。
    区画だけで分けていたころは2直として取り込み、2直が 23:30 まで続いて
    「最終時間まで入力がないのでは？」、3直は続きの行(ロット空欄)から始まって
    いました。3直が全停の日は 22:50〜07:00 の全停が2直に入っていました。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def parsed(self, starts=None, **extra):
        path = write_xlsx(self.tmp / "日付.xlsx", sheet_cells(**extra))
        return ns.parse_file(path, "HVC", starts)

    def by_shift(self, found):
        return {p.header.shift: p for p in found.pages}

    # 2直の最後(21:45〜22:50 でロットを渡す)と、3直の最初の行(22:50〜23:30)が22時台の枠に
    LATE = {"D93": "H9461S0", "J93": "21", "K93": "45", "L93": "22", "M93": "50",
            "D98": "H9461S0", "J98": "22", "K98": "50", "L98": "23", "M98": "30",
            "Y98": "1", "Z98": "750.9", "AA98": "3直の人",
            "J104": "23", "K104": "30", "L104": "0", "M104": "10", "Y104": "1", "Z104": "750.9",
            "AA104": ""}

    def test_3直の始まり以降に始まる行は3直の頭へ(self) -> None:
        found = self.parsed(**self.LATE)
        shifts = self.by_shift(found)
        self.assertEqual([(d.lot, d.kz, d.kh) for d in shifts["2直"].details],
                         [("L4336H0", "", ""), ("H9461S0", "21", "45")])
        third = shifts["3直"].details
        self.assertEqual((third[0].lot, third[0].kz, third[0].kh, third[0].con), ("H9461S0", "22", "50", "1"))
        self.assertEqual((third[1].lot, third[1].kz), ("", "23"), "3直の続きの行が頭に来た")
        self.assertIn("3直へ移しました", " ".join(found.notes))

    def test_3直の作業者が区画の頭に無ければ移した行から(self) -> None:
        self.assertEqual(self.by_shift(self.parsed(**self.LATE))["3直"].header.worker, "3直の人")

    def test_3直の全停が22時台の枠にあっても3直(self) -> None:
        found = self.parsed(**{"J98": "22", "K98": "50", "L98": "7", "M98": "0",
                               "AJ98": "2", "AK98": "490", "AJ104": "", "AK104": "", "AC104": ""})
        shifts = self.by_shift(found)
        self.assertNotIn("490", [d.th for d in shifts["2直"].details])
        self.assertEqual((shifts["3直"].details[0].kz, shifts["3直"].details[0].th), ("22", "490"))

    def test_22時台でも3直の始まりより前は2直のまま(self) -> None:
        found = self.parsed(**{"D98": "X1", "J98": "22", "K98": "0", "L98": "22", "M98": "50"})
        self.assertIn("X1", [d.lot for d in self.by_shift(found)["2直"].details])
        self.assertEqual(found.notes, [])

    def test_またいだ最後の行は3直が続きの行から始まるなら3直(self) -> None:
        """2026/1/10: 2直は全停(15:00〜22:50)、3直の人が 22:25 から始め、3直は 23:30 の続きの行から。"""
        found = self.parsed(**{"D56": "", "J56": "15", "K56": "0", "L56": "22", "M56": "50",
                               "AJ56": "2", "AK56": "470", "Y56": "", "Z56": "",
                               "D98": "HX460Y0", "J98": "22", "K98": "25", "L98": "23", "M98": "30",
                               "Y98": "1", "Z98": "562.6",
                               "J104": "23", "K104": "30", "L104": "0", "M104": "0", "Y104": "1",
                               "Z104": "562.6", "AA3": "12", "AB3": "1562.6"})
        shifts = self.by_shift(found)
        self.assertEqual([(d.kz, d.th) for d in shifts["2直"].details], [("15", "470")])
        self.assertEqual((shifts["3直"].details[0].lot, shifts["3直"].details[0].kz), ("HX460Y0", "22"))
        self.assertIn("またぎ", " ".join(found.notes))

    def test_またいでも3直がロット入りで始まるなら2直のまま(self) -> None:
        found = self.parsed(**{"D98": "X9", "J98": "22", "K98": "25", "L98": "23", "M98": "30",
                               "D104": "Y1", "J104": "23", "K104": "30", "L104": "0", "M104": "0"})
        self.assertIn("X9", [d.lot for d in self.by_shift(found)["2直"].details])

    def test_直の始まりは時間用のとおり(self) -> None:
        """3直が 23:00 始まりの現場なら、22:50 の行は2直のまま。"""
        starts = ns.shift_starts({"1": ("07:00", "15:00"), "2": ("15:00", "23:00"), "3": ("23:00", "07:00")})
        found = self.parsed(starts, **{**self.LATE, "D104": "Y1"})   # 3直はロット入りで始まる
        self.assertIn(("H9461S0", "22"), [(d.lot, d.kz) for d in self.by_shift(found)["2直"].details])

    def test_直の始まりが読めなければ控え(self) -> None:
        self.assertEqual(ns.shift_starts({}), {"1直": 420, "2直": 900, "3直": 1370})
        self.assertEqual(ns.shift_starts({"3": ("", "")})["3直"], 1370)

    def test_シートの日合計と合わなければ言う(self) -> None:
        """直の間で行を移しても日合計は変わらない。合わなければ読み落としがある。"""
        self.assertEqual(self.parsed().notes, [])
        found = self.parsed(AA3="21")
        self.assertIn("シートの日合計と合いません", " ".join(found.notes))


class LineTests(unittest.TestCase):
    """入れる先のライン ── **シートは書き換えず、人が選ぶ。**"""

    def test_シートの呼び名から当たりを付ける(self) -> None:
        self.assertEqual(ns.guess_line("機側"), "機側")
        self.assertEqual(ns.guess_line("HVC"), "HVC")
        self.assertEqual(ns.guess_line("ﾄｯﾄ"), "トット")

    def test_知らない呼び名なら空(self) -> None:
        """**当てずっぽうで入れない。** 選んでもらいます。"""
        self.assertEqual(ns.guess_line("なにか"), "")
        self.assertEqual(ns.guess_line(""), "")

    def test_選んだラインで入る(self) -> None:
        """シートに「機側」と書いてあっても、選んだ先へ入ります。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = write_xlsx(Path(tmp) / "日付.xlsx", sheet_cells())
            found = ns.parse_file(path, "AIM")
        self.assertEqual({p.header.line for p in found.pages}, {"AIM"})
        self.assertEqual({d.line for p in found.pages for d in p.details}, {"AIM"})


class TotalsTests(unittest.TestCase):
    """シート自身が持っている数 ── **取り込みの検算に使う相手。**"""

    def test_直合計と日合計を拾う(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_xlsx(Path(tmp) / "日付.xlsx", sheet_cells())
            rows = xlsx_sheet.rows_of(xlsx_sheet.read_cells(path))
        totals = ns.read_totals(rows)
        self.assertEqual(totals.by_shift["1直"], (10.0, 1000.0))
        self.assertEqual(totals.day_count, 20.0)
        self.assertEqual(totals.stops["管理ロス停止"], 60.0)

    def test_取り込んだ数がシートの直合計と合う(self) -> None:
        """**ここが本題。** 合わなければ取り込みが間違っています。"""
        from nippou.logic.aggregation import aggregate_day

        with tempfile.TemporaryDirectory() as tmp:
            path = write_xlsx(Path(tmp) / "日付.xlsx", sheet_cells())
            rows = xlsx_sheet.rows_of(xlsx_sheet.read_cells(path))
            found = ns.parse_file(path, "機側")
        totals = ns.read_totals(rows)
        for agg in aggregate_day([(p.header, p.details) for p in found.pages]):
            want_count, want_weight = totals.by_shift[agg.shift]
            with self.subTest(shift=agg.shift):
                self.assertEqual(agg.sheet_count, want_count)
                self.assertEqual(agg.weight_kg, want_weight)

    def test_停止の分類がシートの合計と合う(self) -> None:
        """記号の文字種で振り分けた結果が、シートの3つの合計と合うこと。"""
        from nippou.logic.aggregation import aggregate_day

        with tempfile.TemporaryDirectory() as tmp:
            path = write_xlsx(Path(tmp) / "日付.xlsx", sheet_cells())
            rows = xlsx_sheet.rows_of(xlsx_sheet.read_cells(path))
            found = ns.parse_file(path, "機側")
        totals = ns.read_totals(rows)
        rows_agg = aggregate_day([(p.header, p.details) for p in found.pages])
        self.assertEqual(sum(r.management_loss_minutes for r in rows_agg),
                         totals.stops["管理ロス停止"])
        self.assertEqual(sum(r.unplanned_stop_minutes for r in rows_agg),
                         totals.stops["突発停止"])
        self.assertEqual(sum(r.handling_stop_minutes for r in rows_agg),
                         totals.stops["ハンドリング停止"])


# ======================================================================
# 画面から (Flask が要る)
# ======================================================================
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class WebSheetTests(WebTestCase):
    """入れる先のラインは**人が選ぶ** ── シート側は書き換えない。"""

    def admin(self) -> None:
        from nippou import work_context

        work_context.get_context().admin = True

    def book(self, **extra) -> str:
        return str(write_xlsx(self.tmp / "日付.xlsx", sheet_cells(**extra)))

    def test_シートに書いてあるラインを見せる(self) -> None:
        """当てずっぽうで選ばせない ── 何と書いてあるかを出します。"""
        self.admin()
        body = self.post("/api/settings/import/preview",
                         {"path": self.book()}).get_json()
        self.assertEqual(body["sheet"]["line_text"], "機側")
        self.assertEqual(body["sheet"]["report_date"], DAY)
        self.assertEqual(body["line"], "機側")      # 当たり

    def test_選べるラインの一覧を返す(self) -> None:
        """画面が自前で持つと、ラインが増えたとき2か所直すことになる。"""
        self.admin()
        body = self.post("/api/settings/import/preview",
                         {"path": self.book()}).get_json()
        from nippou import constants

        self.assertEqual(body["lines"], list(constants.LINE_NAMES))

    def test_選んだラインへ入る(self) -> None:
        """シートに「機側」と書いてあっても、選んだ先が勝ちます。"""
        self.admin()
        body = self.post("/api/settings/import/apply",
                         {"path": self.book(), "line": "AIM"}).get_json()
        self.assertEqual(body["pages"], 3)
        self.assertEqual({k[1] for k in self.repo().list_keys()}, {"AIM"})

    def test_選ばなければ当たりで入る(self) -> None:
        self.admin()
        self.post("/api/settings/import/apply", {"path": self.book()})
        self.assertEqual({k[1] for k in self.repo().list_keys()}, {"機側"})

    def test_当たりも付かなければ断る(self) -> None:
        """**当てずっぽうで入れない。** 知らない呼び名のときは選ばせます。"""
        self.admin()
        res = self.post("/api/settings/import/apply",
                        {"path": self.book(**{"E2": "しらない設備"})})
        self.assertEqual(res.status_code, 422)
        self.assertIn("ライン", res.get_json()["message"])
        self.assertEqual(self.repo().list_keys(), [])

    def test_下見は書かない(self) -> None:
        self.admin()
        self.post("/api/settings/import/preview", {"path": self.book()})
        self.assertEqual(self.repo().list_keys(), [])

    def test_入れたら3直ぶん揃う(self) -> None:
        self.admin()
        self.post("/api/settings/import/apply", {"path": self.book()})
        shifts = sorted({k[2] for k in self.repo().list_keys()})
        self.assertEqual(shifts, ["1直", "2直", "3直"])

    def test_入れたらグラフにも紙にも出る(self) -> None:
        """**入り口は1つ。** 下流は全部ついてきます。"""
        from nippou import work_context

        self.admin()
        self.post("/api/settings/import/apply", {"path": self.book()})
        # グラフはこの端末のラインで絞ります。入れた先を見に行く
        work_context.get_context().line = "機側"

        history = self.post("/api/graph/history",
                            {"start": "2026-08-01", "end": "2026-08-31"}
                            ).get_json()
        counts = dict(zip(history["labels"], next(
            s for s in history["series"] if s["key"] == "count")["values"]))
        self.assertEqual(counts[DAY], 20)         # シートの日合計と同じ

        paper = self.get(f"/report/nippou?report_date={DAY}&line=機側&shift=1直")
        self.assertIn("L6101N0", paper.get_data(as_text=True))

    def test_画面にラインを選ぶ欄がある(self) -> None:
        html = self.get("/settings").get_data(as_text=True)
        self.assertIn("入れる先のライン", html)
        self.assertIn('id="import-line"', html)
        self.assertIn(".xlsx", html)


if __name__ == "__main__":                       # pragma: no cover
    unittest.main()
