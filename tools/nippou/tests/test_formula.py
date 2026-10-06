"""計算の途中式 ── **画面の数字と、式の答えが一致すること**

【なぜこのテストが要るのか】
途中式は「その数がどこから来たか」を見せるためのものです。ところが式の
文字列は**元の計算とは別に組み立てている**ので、片方だけ直すと静かに
ずれます。ずれた途中式は、無いより悪い ── 疑って開いた人が、間違った
根拠で納得します。

だから**答え合わせをします。** `formula.py` が出した答えと、
`ShiftAggregate` の元の計算が一致しない限り、ここで落ちます。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import formula
from nippou.logic.aggregation import MINUTES_PER_DAY, ShiftAggregate

DAY = "2026年9月12日"


def agg(shift: str = "1直", **values) -> ShiftAggregate:
    base = dict(report_date=DAY, line="L-1", shift=shift,
                sheet_count=342, weight_kg=4720.0, work_minutes=1008,
                management_loss_minutes=120, unplanned_stop_minutes=24,
                handling_stop_minutes=27)
    base.update(values)
    return ShiftAggregate(**base)


def result_number(step: formula.Step) -> float:
    """「88.1 %」→ 88.1。答えの行から数だけ取り出す。"""
    head = step.result.split(" ")[0].replace(",", "")
    return float(head)


class AgreementTests(unittest.TestCase):
    """**式の答え == 元の計算。** ここが本題。"""

    def steps(self, a: ShiftAggregate) -> dict[str, formula.Step]:
        return {s.key: s for s in formula.shift_steps(a)}

    def test_稼働率が一致する(self) -> None:
        a = agg()
        self.assertAlmostEqual(
            result_number(self.steps(a)["operating_rate"]),
            round(a.operating_rate_pct, 1), places=1)

    def test_生産性が一致する(self) -> None:
        a = agg()
        self.assertAlmostEqual(
            result_number(self.steps(a)["productivity"]),
            round(a.productivity_t_per_h, 2), places=2)

    def test_稼働時間が一致する(self) -> None:
        a = agg()
        self.assertEqual(result_number(self.steps(a)["operating_minutes"]),
                         a.operating_minutes)

    def test_操業時間が一致する(self) -> None:
        a = agg()
        self.assertEqual(result_number(self.steps(a)["operational_minutes"]),
                         a.operational_minutes)

    def test_重量tが一致する(self) -> None:
        a = agg()
        self.assertAlmostEqual(result_number(self.steps(a)["weight_ton"]),
                               round(a.weight_ton, 3), places=3)

    def test_停止合計が一致する(self) -> None:
        a = agg()
        self.assertEqual(result_number(self.steps(a)["stop_total"]),
                         a.total_stop_minutes)

    def test_どんな数でも一致する(self) -> None:
        """**端の値でも崩れないこと。** 0・大きい値・停止なし。"""
        cases = [
            agg(management_loss_minutes=0, unplanned_stop_minutes=0,
                handling_stop_minutes=0),
            agg(weight_kg=0.0),
            agg(weight_kg=99999.0, management_loss_minutes=600,
                unplanned_stop_minutes=300, handling_stop_minutes=200),
        ]
        for a in cases:
            with self.subTest(stop=a.total_stop_minutes, kg=a.weight_kg):
                steps = self.steps(a)
                self.assertAlmostEqual(
                    result_number(steps["operating_rate"]),
                    round(a.operating_rate_pct, 1), places=1)
                self.assertAlmostEqual(
                    result_number(steps["productivity"]),
                    round(a.productivity_t_per_h, 2), places=2)


class DayAgreementTests(unittest.TestCase):
    """その日ぶん。**直の平均ではない**ことを、式でも数でも確かめる。"""

    def rows(self) -> list[ShiftAggregate]:
        return [agg("1直"), agg("2直", handling_stop_minutes=60),
                agg("3直", unplanned_stop_minutes=90)]

    def steps(self) -> dict[str, formula.Step]:
        return {s.key: s for s in formula.day_steps(self.rows())}

    def test_その日の稼働率が一致する(self) -> None:
        from nippou.logic.aggregation import day_rate_pct

        self.assertAlmostEqual(result_number(self.steps()["day_rate"]),
                               round(day_rate_pct(self.rows()), 1), places=1)

    def test_直の平均ではないと書いてある(self) -> None:
        """**式を見る人がいちばん誤解するところ。** 注記で言っておく。"""
        note = self.steps()["day_rate"].note
        self.assertIn("平均ではありません", note)

    def test_のべ時間は直の数だけ伸びる(self) -> None:
        rows = self.rows()
        whole = result_number(self.steps()["day_whole"])
        self.assertEqual(whole, MINUTES_PER_DAY * len(rows))

    def test_直が無ければ落ちない(self) -> None:
        """保存が1件も無い日。**画面は出す**ので、式も出せなければ困る。"""
        steps = {s.key: s for s in formula.day_steps([])}
        self.assertEqual(result_number(steps["day_rate"]), 0.0)
        self.assertEqual(result_number(steps["day_productivity"]), 0.0)

    def test_累積は求めたときだけ出る(self) -> None:
        """累積は1日の数ではないので、渡されないかぎり式も出しません。"""
        without = {s.key for s in formula.day_steps(self.rows())}
        self.assertNotIn("day_cumulative", without)
        withit = {s.key for s in formula.day_steps(self.rows(), 4050)}
        self.assertIn("day_cumulative", withit)


class ShapeTests(unittest.TestCase):
    """出す形。**3行**(式 / 数を入れた式 / 答え)。"""

    def test_3行で出る(self) -> None:
        for step in formula.shift_steps(agg()):
            with self.subTest(step=step.key):
                self.assertEqual(len(step.lines), 3)
                self.assertTrue(step.formula)
                self.assertTrue(step.substituted)
                self.assertTrue(step.result)

    def test_数を入れた式に実際の数が入っている(self) -> None:
        """**ここが読む人の目当て。** 式の形だけでは確かめられない。"""
        steps = {s.key: s for s in formula.shift_steps(agg())}
        # 停止合計 = 120 + 24 + 27
        self.assertIn("120", steps["stop_total"].substituted)
        self.assertIn("24", steps["stop_total"].substituted)
        self.assertIn("27", steps["stop_total"].substituted)
        # 稼働時間 = 1440 − 171
        self.assertIn("1440", steps["operating_minutes"].substituted)
        self.assertIn("171", steps["operating_minutes"].substituted)

    def test_値の出どころが分かる(self) -> None:
        """疑ったときに辿る先。**定数か、打った値の合計か。**"""
        steps = {s.key: s for s in formula.shift_steps(agg())}
        sources = {t.label: t.source for t in steps["operating_minutes"].terms}
        self.assertEqual(sources["1440"], "定数(1日)")
        sources = {t.label: t.source for t in steps["stop_total"].terms}
        self.assertEqual(sources["管理ロス停止"], "明細の合計")

    def test_VBAと違うところは書いてある(self) -> None:
        """**生産性の分母はVBAと違います。** 黙って変えない。"""
        steps = {s.key: s for s in formula.shift_steps(agg())}
        self.assertIn("VBAとは分母が違います", steps["productivity"].note)

    def test_足すだけのものに式は付けない(self) -> None:
        """枚数の合計に式を出しても、読むものがありません。"""
        keys = {s.key for s in formula.shift_steps(agg())}
        self.assertNotIn("sheet_count", keys)

    def test_稼働時間が0でも落ちない(self) -> None:
        a = agg(management_loss_minutes=MINUTES_PER_DAY,
                unplanned_stop_minutes=0, handling_stop_minutes=0)
        steps = {s.key: s for s in formula.shift_steps(a)}
        self.assertEqual(result_number(steps["productivity"]), 0.0)
        self.assertIn("割れません", steps["productivity"].note)

    def test_式に飾りを書かない(self) -> None:
        """式も注記も**そのまま画面に出る文字**です。

        `<details>` の中身は `{{ step.note }}` で出しますし、CSVには生の
        文字が並びます。`**…**` と書けば、アスタリスクがそのまま見えます
        ── ソースのコメントの調子で書いてしまいがちなので、縛ります。

        走った式だけを見ても足りません。0除算のときの注記のように、
        **ふだん通らない枝**にも文字はあります。だから `Step(...)` と
        `Term(...)` に渡している文字を、ソースから全部拾って調べます。
        """
        import ast

        src = Path(formula.__file__).read_text(encoding="utf-8")
        found = 0
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "id", "") not in ("Step", "Term"):
                continue
            for text in ast.walk(node):
                if isinstance(text, ast.Constant) and isinstance(text.value, str):
                    found += 1
                    with self.subTest(line=text.lineno, text=text.value):
                        self.assertNotIn("**", text.value)
                        self.assertNotIn("`", text.value)
        self.assertGreater(found, 40, "式の文字が拾えていない")


class CsvRowTests(unittest.TestCase):
    """CSVの行。**画面と同じ式が出る**(組み立てが1か所なので当然)。"""

    def test_1本の式が1行(self) -> None:
        steps = formula.shift_steps(agg())
        rows = formula.as_rows(DAY, "L-1", "1直", steps)
        self.assertEqual(len(rows), len(steps))

    def test_見出しがそろっている(self) -> None:
        rows = formula.as_rows(DAY, "L-1", "1直", formula.shift_steps(agg()))
        for row in rows:
            self.assertEqual(set(row), set(formula.CSV_FIELDNAMES))

    def test_画面とCSVで式が同じ(self) -> None:
        """**別々に組み立てていない**ことの確かめ。"""
        steps = formula.shift_steps(agg())
        rows = formula.as_rows(DAY, "L-1", "1直", steps)
        for step, row in zip(steps, rows):
            self.assertEqual(row["式"], step.formula)
            self.assertEqual(row["数を入れた式"], step.substituted)
            self.assertEqual(row["答え"], step.result)


# ======================================================================
# 画面とCSV (Flask が要る)
# ======================================================================
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class ScreenTests(WebTestCase):
    """画面から途中式が読めるか。"""

    def save(self) -> str:
        """1直ぶん保存して、報告日を返す。"""
        from nippou.db.models import DetailRecord, HeaderRecord
        from nippou.services import summary

        repo = self.repo()
        rd = DAY
        header = HeaderRecord(report_date=rd, line="L-1", shift="1直", page=1,
                              worker="作業者A")
        details = [DetailRecord(
            report_date=rd, line="L-1", shift="1直", page=1, row_no=1,
            lot="1111111", zai="F52S", siz="1.0x1000x2000", ken="9",
            kz="8", kh="00", sz="9", sh="00", hit="1", mai="120", tut="2",
            con="120", wei="4720", tim="60",
            s="1", th="120", ss="イ", ths="24", sth="A", tht="27")]
        repo.save(header, details)
        summary.refresh_shift(repo, rd, "L-1", "1直")
        return rd

    def test_稼働率のタイルに計算内容が付く(self) -> None:
        """**畳んである**(`<details>`)。数字を見に来た人の邪魔をしない。"""
        self.save()
        html = self.get("/graph").get_data(as_text=True)
        self.assertIn("計算内容を見る", html)
        self.assertIn('class="calc"', html)

    def test_式と数を入れた式と答えが出る(self) -> None:
        self.save()
        html = self.get("/graph").get_data(as_text=True)
        self.assertIn("calc__formula", html)
        self.assertIn("calc__sub", html)
        self.assertIn("calc__result", html)
        # 稼働率の式そのもの
        self.assertIn("のべ時間", html)

    def test_足すだけのタイルには付かない(self) -> None:
        """合計重量の中に `<details>` が入っていないこと。"""
        self.save()
        body = self.post("/api/graph/dashboard",
                         {"start": "2026-09-01", "end": "2026-09-30"}).get_json()
        tiles = {t["key"]: t for t in body["tiles"]}
        self.assertEqual(tiles["today_weight"]["steps"], [])
        self.assertEqual(tiles["today_count"]["steps"], [])
        self.assertTrue(tiles["today_rate"]["steps"])
        self.assertTrue(tiles["today_productivity"]["steps"])

    def test_期間を変えても式が消えない(self) -> None:
        """**画面を作り直すのはJS。** 式を作り直し忘れると式だけ消える。"""
        js = (Path(__file__).resolve().parent.parent / "app" / "static"
              / "js" / "views" / "graph.js").read_text(encoding="utf-8")
        self.assertIn("calcBox", js)
        self.assertIn("body.append(calc)", js)


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class CsvTests(WebTestCase):
    """「出力できるように」── CSVで出る。"""

    def test_計算内容は年月のフォルダに1つだけ_日のフォルダには出さない(self) -> None:
        """日フォルダ内に毎回入れる CSVの読み方、計算内容は年月フォルダに
        1回だけ入れてください 無ければ入れる"""
        from nippou.db.models import DetailRecord, HeaderRecord
        from nippou.services import summary

        repo = self.repo()
        header = HeaderRecord(report_date=DAY, line="L-1", shift="1直", page=1,
                              worker="作業者A")
        details = [DetailRecord(
            report_date=DAY, line="L-1", shift="1直", page=1, row_no=1,
            lot="1111111", mai="120", con="120", wei="4720", tim="60",
            s="1", th="120")]
        repo.save(header, details)
        summary.refresh_shift(repo, DAY, "L-1", "1直")

        body = self.post("/api/graph/csv", {"report_date": DAY}).get_json()
        day_dir = Path(body["dir"])
        self.assertEqual(sorted(p.name.split("_")[0] for p in day_dir.iterdir()),
                         ["停止内訳", "集計", "集計明細"])
        guide = Path(body["guide_path"])
        self.assertEqual(guide.parent, day_dir.parent)
        text = guide.read_text(encoding="utf-8-sig")
        self.assertIn("稼働率(%)", text)
        self.assertIn("生産性(t/h)", text)
        self.assertIn("その日ぜんぶ", text)
        self.assertTrue((day_dir.parent / "CSVの読み方.txt").is_file())
        self.assertIn("年月のフォルダに", body["message"])

        # 2回目は置かない(あるので)
        again = self.post("/api/graph/csv", {"report_date": DAY}).get_json()
        self.assertNotIn("年月のフォルダに", again["message"])

    def test_画面にも3本と年月のフォルダの説明が書いてある(self) -> None:
        html = self.get("/graph").get_data(as_text=True)
        self.assertIn("1日ずつ3本", html)
        self.assertIn("計算内容.csv", html)
        self.assertIn("年月のフォルダに1つだけ", html)


if __name__ == "__main__":                       # pragma: no cover
    unittest.main()
