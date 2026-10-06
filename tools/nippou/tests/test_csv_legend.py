"""CSVの列の説明 ── **説明だけ古くなる、を起こさない**

【なぜこのテストが要るのか】
説明の見出しを手で並べると、CSVに列が増えたときに説明だけ取り残されます。
そして**取り残された説明は、無い説明より悪い** ── 読んだ人は、そこに
書いてあるとおりだと思って数字を扱います。

だから、列の並びは `csv_export` の `FIELDNAMES` などからそのまま読み、
説明はこのモジュールの `DESCRIPTIONS` から引いています。片方に漏れが
あれば、ここで落ちます。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import formula
from nippou.reporting import csv_export, csv_legend, month_export


def every_column() -> set[str]:
    """CSVに出る列を全部(日ごとの3本 + 年月の計算内容 + 月別の3本)。"""
    names: set[str] = set()
    for fields in (csv_export.FIELDNAMES, csv_export.STOP_FIELDNAMES,
                   csv_export.DETAIL_FIELDNAMES, formula.GUIDE_FIELDNAMES,
                   month_export.STOP_HEADERS):
        names.update(fields)
    return names


class CoverageTests(unittest.TestCase):
    """**漏れも余りも作らない。**"""

    def test_説明の無い列がない(self) -> None:
        missing = sorted(every_column() - set(csv_legend.DESCRIPTIONS))
        self.assertEqual(missing, [], f"説明が抜けています: {missing}")

    def test_使われていない説明がない(self) -> None:
        """列を消したのに説明だけ残っている、を防ぐ。"""
        extra = sorted(set(csv_legend.DESCRIPTIONS) - every_column())
        self.assertEqual(extra, [], f"どのCSVにも無い列です: {extra}")

    def test_説明が空でない(self) -> None:
        for name, text in csv_legend.DESCRIPTIONS.items():
            with self.subTest(column=name):
                self.assertTrue(text.strip(), name)

    def test_間違えやすい組は実在する列(self) -> None:
        """説明のほうにしか無い列名を挙げない。"""
        columns = every_column()
        for _, pairs, _ in csv_legend.CONFUSABLE:
            for name, _text in pairs:
                with self.subTest(column=name):
                    self.assertIn(name, columns)

    def test_間違えやすい列には印が付いている(self) -> None:
        """上の一覧から「これは下に説明がある」と分かるように。"""
        for _, pairs, _ in csv_legend.CONFUSABLE:
            for name, _text in pairs:
                with self.subTest(column=name):
                    self.assertIn("★", csv_legend.DESCRIPTIONS[name], name)


class TextTests(unittest.TestCase):
    """出す形。**メモ帳でそのまま読める**こと。"""

    def body(self) -> str:
        return csv_legend.month_text(2026, 8)

    def test_日の3本と計算内容の列が並ぶ(self) -> None:
        text = self.body()
        for name in ("集計_", "停止内訳_", "集計明細_", "計算内容.csv"):
            self.assertIn(name, text, name)
        for column in csv_export.DETAIL_FIELDNAMES:
            self.assertIn(column, text, column)
        # **日の数は入れない**(どの日にも当てはまる説明なので)
        self.assertIn("2026年8月", text)
        self.assertNotIn("8月3日", text)
        self.assertIn("日のフォルダ", text)

    def test_飾りを書かない(self) -> None:
        """メモ帳では `**…**` はアスタリスクがそのまま見えます。"""
        text = self.body()
        self.assertNotIn("**", text)
        self.assertNotIn("`", text)

    def test_行が長すぎない(self) -> None:
        """全角を2つぶんで数えて、メモ帳の既定の幅に収める。"""
        for line in self.body().split("\r\n"):
            with self.subTest(line=line):
                self.assertLessEqual(csv_legend._width_of(line), 80)

    def test_行末に空白を残さない(self) -> None:
        for line in self.body().split("\r\n"):
            self.assertEqual(line, line.rstrip())

    def test_改行はCRLF(self) -> None:
        """古いメモ帳は LF だけだと1行に潰します。

        **裸の LF が1つも無いこと。** CRLF を抜いて、まだ LF が残って
        いたら、それは途中で混ざったものです。
        """
        text = self.body()
        self.assertIn("\r\n", text)
        self.assertNotIn("\n", text.replace("\r\n", ""))

    def test_間違えやすいところが後ろに出る(self) -> None:
        text = self.body()
        self.assertIn("間違えやすいところ", text)
        self.assertIn("稼働率を出すのに使うのは", text)
        self.assertLess(text.index("集計明細_"), text.index("間違えやすいところ"))

    def test_月別は月別のファイル名で出る(self) -> None:
        text = csv_legend.build("題", csv_legend.monthly_sections(2026, 8, "HVC"))
        self.assertIn("2026年08月_HVC_集計.csv", text)
        self.assertIn("2026年08月_HVC_停止集計.csv", text)
        self.assertIn("2026年08月_HVC_明細.csv", text)


class FileTests(unittest.TestCase):
    """置き場所 ── **年月のフォルダに1つだけ。**

        日フォルダ内に毎回入れる CSVの読み方、計算内容は年月フォルダに
        1回だけ入れてください 無ければ入れる
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_日付を名前に付けない(self) -> None:
        """その日の数字ではなく「読み方」なので、データと見分けが付くように。"""
        placed = csv_legend.ensure_month(self.tmp, 2026, 8)
        self.assertEqual(sorted(p.name for p in placed),
                         sorted(["CSVの読み方.txt", "計算内容.csv"]))

    def test_BOM付きUTF8で読める(self) -> None:
        csv_legend.ensure_month(self.tmp, 2026, 8)
        for name in (csv_legend.FILENAME, csv_legend.GUIDE_FILENAME):
            with self.subTest(name=name):
                raw = (self.tmp / name).read_bytes()
                self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
                self.assertIn("稼働率(%)", (self.tmp / name).read_text(encoding="utf-8-sig"))

    def test_計算内容は式の形と注記だけ_数は入れない(self) -> None:
        csv_legend.ensure_month(self.tmp, 2026, 8)
        with open(self.tmp / csv_legend.GUIDE_FILENAME, encoding="utf-8-sig",
                  newline="") as f:
            text = f.read()
        lines = text.split("\r\n")
        self.assertEqual(lines[0], "対象,項目,式,注記")
        self.assertIn("稼働時間 ÷ 1440分 × 100", text)
        self.assertIn("その日ぜんぶ", text)
        self.assertNotIn("数を入れた式", text)
        self.assertNotIn("答え", lines[0])

    def test_フォルダが無ければ作る(self) -> None:
        folder = self.tmp / "L-1" / "集計" / "2026.08"
        csv_legend.ensure_month(folder, 2026, 8)
        self.assertTrue((folder / csv_legend.FILENAME).exists())
        self.assertTrue((folder / csv_legend.GUIDE_FILENAME).exists())

    def test_ラインの名前を入れない(self) -> None:
        """列の説明はラインで変わりません。"""
        text = csv_legend.month_text(2026, 8)
        self.assertIn("集計_<日付>_<ライン>.csv", text)
        self.assertNotIn("L-1", text)


class EnsureTests(unittest.TestCase):
    """**無いときだけ置く。** 共有に置くものは、消される。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.path = self.tmp / csv_legend.FILENAME
        self.guide = self.tmp / csv_legend.GUIDE_FILENAME

    def test_無ければ書く(self) -> None:
        placed = csv_legend.ensure_month(self.tmp, 2026, 8)
        self.assertEqual(set(placed), {self.path, self.guide})

    def test_あれば書かない_2回目からは触らない(self) -> None:
        """共有への書き込みは高い。更新日時が毎日動くのも気持ちが悪い。"""
        csv_legend.ensure_month(self.tmp, 2026, 8)
        stamps = (self.path.stat().st_mtime_ns, self.guide.stat().st_mtime_ns)
        self.assertEqual(csv_legend.ensure_month(self.tmp, 2026, 8), [])
        self.assertEqual((self.path.stat().st_mtime_ns,
                          self.guide.stat().st_mtime_ns), stamps)

    def test_消されたら置き直す(self) -> None:
        csv_legend.ensure_month(self.tmp, 2026, 8)
        self.guide.unlink()
        self.assertEqual(csv_legend.ensure_month(self.tmp, 2026, 8), [self.guide])
        self.assertTrue(self.guide.is_file())

    def test_中身が古ければ出し直す(self) -> None:
        """版が上がって列が増えたのに説明だけ前のまま、がいちばん困る形。"""
        self.path.write_text("むかしの説明", encoding="utf-8-sig")
        self.assertIn(self.path, csv_legend.ensure_month(self.tmp, 2026, 8))
        self.assertNotIn("むかしの説明",
                         self.path.read_text(encoding="utf-8-sig"))

    def test_月別も同じように見る(self) -> None:
        self.assertTrue(csv_legend.ensure_monthly(self.tmp, 2026, 8, "HVC"))
        self.assertFalse(csv_legend.ensure_monthly(self.tmp, 2026, 8, "HVC"))
        self.path.unlink()
        self.assertTrue(csv_legend.ensure_monthly(self.tmp, 2026, 8, "HVC"))


class SweepTests(unittest.TestCase):
    """過ぎた月のぶんも ── **書くたびに見るのはその月だけ。**

    先月のぶんが消されても、そこへはもう書かないので気づけません。
    ボタンを押したときに、出力先の下を一通り見て回ります。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def day(self, year: int, month: int, day: int, *, csv: bool = True,
            line: str = "L-1") -> Path:
        """`<出力先>/ライン/集計/年月/日`。**書き手と同じ並べ方で置く。**"""
        folder = (self.tmp / line / "集計"
                  / f"{year:04d}.{month:02d}" / f"{day:02d}")
        folder.mkdir(parents=True, exist_ok=True)
        if csv:
            (folder / f"集計_{year}-{month:02d}-{day:02d}_{line}.csv").write_text(
                "日付\n", encoding="utf-8-sig")
        return folder

    def test_欠けている年月へ置き直す_日のフォルダには置かない(self) -> None:
        a, b = self.day(2026, 8, 3), self.day(2026, 9, 13)
        self.day(2026, 9, 14)                              # 同じ月は1回
        csv_legend.ensure_month(a.parent, 2026, 8)         # 片方だけ揃っている
        swept = csv_legend.sweep(self.tmp)
        self.assertEqual(swept.checked, 2)
        self.assertEqual({p.parent for p in swept.reissued}, {b.parent})
        self.assertTrue((b.parent / csv_legend.FILENAME).is_file())
        self.assertTrue((b.parent / csv_legend.GUIDE_FILENAME).is_file())
        self.assertFalse((b / csv_legend.FILENAME).exists())

    def test_揃っていれば何もしない(self) -> None:
        for folder, (y, m) in ((self.day(2026, 8, 3), (2026, 8)),
                               (self.day(2026, 9, 13), (2026, 9))):
            csv_legend.ensure_month(folder.parent, y, m)
        swept = csv_legend.sweep(self.tmp)
        self.assertEqual(swept.checked, 2)
        self.assertEqual(swept.reissued, ())
        self.assertEqual(swept.message, "")       # 何も無いときは黙る

    def test_日付のフォルダだけ見る(self) -> None:
        """出力先には人が作ったフォルダも混ざります。"""
        self.day(2026, 8, 3)
        other = self.tmp / "むかしの" / "書類" / "控え" / "そのまた下"
        other.mkdir(parents=True)
        (other / "なにか.csv").write_text("x", encoding="utf-8")
        swept = csv_legend.sweep(self.tmp)
        self.assertEqual(swept.checked, 1)
        self.assertFalse((other.parent / csv_legend.FILENAME).exists())

    def test_ラインごとに分かれていても全部見る(self) -> None:
        """**ラインの名前は見ません。** マスタで増えるので、ここで一覧を
        持つと「増やしたのにここだけ古い」が起きます。"""
        a = self.day(2026, 8, 3, line="L-1")
        b = self.day(2026, 8, 3, line="LVC")
        swept = csv_legend.sweep(self.tmp)
        self.assertEqual(swept.checked, 2)
        self.assertEqual({p.parent for p in swept.reissued}, {a.parent, b.parent})

    def test_印刷のフォルダも見る(self) -> None:
        """種類の名前も見ません ── 後から足りうるので深さで判断します。"""
        folder = self.tmp / "L-1" / "印刷" / "2026.08" / "03"
        folder.mkdir(parents=True)
        (folder / "集計_2026-08-03_L-1.csv").write_text("日付\n",
                                                      encoding="utf-8-sig")
        swept = csv_legend.sweep(self.tmp)
        self.assertEqual(swept.checked, 1)

    def test_CSVの無い月には置かない(self) -> None:
        """説明だけ残っているのは「ここに何かあったはず」と思わせるだけ。"""
        self.day(2026, 8, 3, csv=False)
        swept = csv_legend.sweep(self.tmp)
        self.assertEqual(swept.checked, 0)

    def test_出力先がまだ無くても落ちない(self) -> None:
        swept = csv_legend.sweep(self.tmp / "まだ無い")
        self.assertEqual(swept.checked, 0)
        self.assertEqual(swept.message, "")

    def test_その年月で書かれる(self) -> None:
        """フォルダの名前から年月を組み立てる(ゼロ詰めは外す)。"""
        folder = self.day(2026, 8, 3)
        csv_legend.sweep(self.tmp)
        text = (folder.parent / csv_legend.FILENAME).read_text(encoding="utf-8-sig")
        self.assertIn("2026年8月", text)

    def test_直したことを言う(self) -> None:
        self.day(2026, 8, 3)
        swept = csv_legend.sweep(self.tmp)
        self.assertIn("年月のフォルダ", swept.message)
        self.assertIn(csv_legend.FILENAME, swept.message)


if __name__ == "__main__":                       # pragma: no cover
    unittest.main()
