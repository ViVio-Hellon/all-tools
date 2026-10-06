"""紙1枚ぶんの決まりごと ── 埋まりぐあい・理由・作業の順序

【なぜこれが要るのか】
日報の紙(梱包実績日報表)は**12行しかありません。** VBA は使い切ると
別のシートを「新規発行」して続きを打っていましたが、Web版にはその入口が
無く、12行目まで打つと続きを打つ手立てがありませんでした。

理由(header の `reason`)も同じで、紙には
**「ヨ：その他　（理由を記載）」**と刷ってあります ── いつでも書く欄では
なく、決まった記号を選んだときにだけ書き足すものです。空欄が上に並んで
いると「打つべきなのか」が分かりません。

ここが守っているのは、その2つと「作業の順序」です。どれもサーバが決めて、
画面は写すだけにします。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou import constants  # noqa: E402
from nippou.logic import calculations  # noqa: E402
from nippou.presenters import entry as presenter  # noqa: E402


def _choices() -> list[dict]:
    """停止理由マスタの形。実物と同じく分類ごとにまとまっている。"""
    return [
        {"category": "管理ロス", "reasons": [
            {"code": "0", "label": "休憩食事", "text": "0 休憩食事"},
            {"code": "ヨ", "label": "その他（理由を記載）",
             "text": "ヨ その他（理由を記載）"},
        ]},
        {"category": "ハンドリング", "reasons": [
            {"code": "G", "label": "ビニール交換", "text": "G ビニール交換"},
        ]},
    ]


class UsedRowsTests(unittest.TestCase):
    """何行使ったか。**12行しかないので、残りが読めないと手が止まる。**"""

    def test_空なら0行(self) -> None:
        self.assertEqual(presenter.used_rows(presenter.empty_state()), 0)
        self.assertFalse(presenter.sheet_full(presenter.empty_state()))

    def test_ロットを打てば使った行(self) -> None:
        state = presenter.empty_state()
        state.set(1, "LOT", "H5422S0")
        self.assertEqual(presenter.used_rows(state), 1)

    def test_ロットが無くても時刻だけで使った行(self) -> None:
        """全停入力の1行目はロットを持たない ── それを空扱いにすると、
        全停のページが「まだ空です」と言われて次のページを出せない。
        全停の行は開始から終了まで入っています。"""
        state = presenter.empty_state()
        for family, value in (("KZ", "22"), ("KH", "50"), ("SZ", "07"), ("SH", "00")):
            state.set(1, family, value)
        self.assertEqual(presenter.used_rows(state), 1)

    def test_開始時刻だけの行は数えない(self) -> None:
        """11行目の終了を打つと、12行目の開始へ自動で写る(v4.8.0)。
        それを数えると、11行で「使い切りました」と出て次のページも出せていた。"""
        state = presenter.empty_state()
        state.set(12, "KZ", "13")
        state.set(12, "KH", "00")
        self.assertEqual(presenter.used_rows(state), 0)

    def test_飛び飛びでも数える(self) -> None:
        state = presenter.empty_state()
        state.set(1, "LOT", "A")
        state.set(5, "LOT", "B")
        self.assertEqual(presenter.used_rows(state), 2)
        self.assertFalse(presenter.sheet_full(state))

    def test_12行埋まれば使い切り(self) -> None:
        state = presenter.empty_state()
        for row in range(1, constants.ROW_COUNT + 1):
            state.set(row, "LOT", f"L{row}")
        self.assertEqual(presenter.used_rows(state), constants.ROW_COUNT)
        # **12行目が終了まで入って初めて**次のページを出せる(v4.8.0)
        self.assertFalse(presenter.sheet_full(state))
        state.set(constants.ROW_COUNT, "SZ", "14")
        state.set(constants.ROW_COUNT, "SH", "30")
        self.assertTrue(presenter.sheet_full(state))

    def test_空白だけの欄は使っていない(self) -> None:
        state = presenter.empty_state()
        state.set(1, "LOT", "   ")
        self.assertEqual(presenter.used_rows(state), 0)


class OtherReasonTests(unittest.TestCase):
    """理由は「その他」を選んだときだけ書く(紙の「ヨ：その他（理由を記載）」)。"""

    def test_その他の記号を名前で見分ける(self) -> None:
        """**記号そのものを決め打ちにしない。** 記号はマスタが決めるので、
        差し替わると決め打ちは外れる。"""
        self.assertEqual(presenter.other_stop_codes(_choices()), {"ヨ"})

    def test_その他が無いマスタなら空(self) -> None:
        only = [{"category": "管理ロス", "reasons": [
            {"code": "0", "label": "休憩食事", "text": "0 休憩食事"}]}]
        self.assertEqual(presenter.other_stop_codes(only), set())

    def test_選んでいなければ閉じる(self) -> None:
        state = presenter.empty_state()
        self.assertFalse(presenter.reason_open(state, {"ヨ"}))
        self.assertEqual(presenter.reason_rows(state, {"ヨ"}), [])

    def test_選べば開く(self) -> None:
        state = presenter.empty_state()
        state.set(3, "SS", "ヨ")
        self.assertTrue(presenter.reason_open(state, {"ヨ"}))
        self.assertEqual(presenter.reason_rows(state, {"ヨ"}), [3])

    def test_3つのどの停止欄でも開く(self) -> None:
        for family in presenter.STOP_CODE_FAMILIES:
            with self.subTest(family=family):
                state = presenter.empty_state()
                state.set(2, family, "ヨ")
                self.assertTrue(presenter.reason_open(state, {"ヨ"}))

    def test_マスタが読めないときは開けたまま(self) -> None:
        """そのときは停止理由そのものが自由入力に落ちている。こちらだけ
        閉じると、理由を書く手立てが無くなる。"""
        self.assertTrue(presenter.reason_open(presenter.empty_state(), set()))


class TotalPlacementTests(unittest.TestCase):
    """合計は表の下(紙の21〜26行目)。上に置くと、打つ前から枚数欄がある。"""

    def test_上に出すのは作業者名だけ(self) -> None:
        self.assertEqual([n for n, _l, _k in presenter.TOP_FIELDS], ["worker"])

    def test_下に出すのは紙の合計欄(self) -> None:
        self.assertEqual([n for n, _l, _k, _c in presenter.TOTAL_FIELDS],
                         ["lot_count", "count", "weight_kg",
                          "coefficient_lot_count"])

    def test_計算で決まる欄は枚数と重量(self) -> None:
        """`Weight計算` が出す2つ。**打てると明細と食い違ったまま保存できる。**"""
        self.assertEqual(presenter.CALCULATED_HEADER_FIELDS,
                         ("count", "weight_kg"))

    def test_保存する欄は6つのまま(self) -> None:
        """置き場所を分けただけで、DBへ書く欄は増減していない。"""
        self.assertEqual({n for n, _l, _k in presenter.HEADER_FIELDS},
                         {"worker", "count", "weight_kg", "lot_count",
                          "coefficient_lot_count", "reason"})


class TonsTests(unittest.TestCase):
    """紙は「4379.4 Kg」の下に「4.38 T」と刷ってある。読むための添え物。"""

    def test_Kgからトン(self) -> None:
        self.assertEqual(calculations.tons("4379.4"), "4.38")

    def test_空や数字でなければ空(self) -> None:
        for text in ("", "   ", "—", None):
            with self.subTest(text=text):
                self.assertEqual(calculations.tons(text), "")

    def test_0は空(self) -> None:
        """紙も空欄。0.00 T と刷っても読む人には何も足さない。"""
        self.assertEqual(calculations.tons("0"), "")


class StepsTests(unittest.TestCase):
    """作業の順序。**いまどこかを決めるのはサーバ。**"""

    def test_並びは作業の順(self) -> None:
        """**ラインは入らない。** 据え付けのときに決めるもので、毎直
        やることではない(設定・管理者へ移した)。"""
        keys = [s["key"] for s in presenter.steps(presenter.empty_state())]
        self.assertEqual(keys, ["worker", "rows", "save"])

    def test_空の画面では作業者がいまここ(self) -> None:
        steps = presenter.steps(presenter.empty_state())
        self.assertEqual([s["key"] for s in steps if s["current"]], ["worker"])

    def test_作業者を入れたら明細へ進む(self) -> None:
        state = presenter.empty_state()
        state.header["worker"] = "近藤雅幹"
        steps = presenter.steps(state)
        self.assertEqual([s["key"] for s in steps if s["current"]], ["rows"])
        self.assertTrue(next(s for s in steps if s["key"] == "worker")["done"])

    def test_打ち始めたら保存へ進む(self) -> None:
        state = presenter.empty_state()
        state.header["worker"] = "近藤雅幹"
        state.set(1, "LOT", "H5422S0")
        steps = presenter.steps(state)
        self.assertEqual([s["key"] for s in steps if s["current"]], ["save"])

    def test_いまここは常に1つ(self) -> None:
        for state in (presenter.empty_state(),):
            self.assertEqual(sum(1 for s in presenter.steps(state)
                                 if s["current"]), 1)


if __name__ == "__main__":
    unittest.main()
