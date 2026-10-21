"""時間を割り振る: EX・ロットの変わり目(段取り)・VC交換 (v4.42.0)

    EX が付いている場合は作業時間が EX でないものより多いです
    (EX:そのLOTすべてが長くなるEX梱包のため)
    LOTが変わるタイミングの1梱包目は通常よりもやや長いです
    (1梱包目の前に段取りが入るためです 1梱包目だけ長めです)
    LOTが変わるタイミングで前のLOTとVCが違う場合も通常より長くなります
    (1梱包目の前にVC交換が入るためです 1梱包目だけ長めです)

【約束】
    ・ロット№が空の行は上の行と同じロット(ページをまたいでも)
    ・EX は etc 欄の「EX」。そのロットの行すべてを EX の倍率で
    ・ロット№が変わる行は、1梱包目(行の梱包数 ÷ 包数)だけ段取りの倍率で長く
    ・前のロットと VC が違えば、1梱包目に VC交換のぶんを足す
    ・直の最初の行は前の直の最後のロットと比べる(同じなら変わり目ではない)
    ・倍率は設定・管理者で変えられる(1〜5。管理者モードだけ)
    ・段取り変更の停止がある行は長くしない(v4.42.1。段取りは停止の分として先に渡してある)
"""
from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.access_bridge.stop_master import StopReason  # noqa: E402
from nippou.logic import time_split as ts  # noqa: E402
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402

D = Decimal


def weights_of(rows, before=None, factors=None, setup_codes=frozenset()):
    base, _ = ts.pack_weights([ts.row_packs(r) for r in rows])
    kinds = ts.lot_runs(rows, before)
    return ts.adjust_weights(base, rows, kinds, factors or ts.Factors(), setup_codes)


class LotRunsTests(unittest.TestCase):
    def test_ロット番号が空の行は上の行と同じロット(self) -> None:
        rows = [{"LOT": "A", "MAI": "5"}, {"MAI": "5"}, {"LOT": "B", "MAI": "5"}, {"MAI": "5"}]
        kinds = ts.lot_runs(rows)
        self.assertEqual([k.lot for k in kinds], ["A", "A", "B", "B"])
        self.assertEqual([k.change for k in kinds], [True, False, True, False])

    def test_EXはそのロットの行すべて(self) -> None:
        """EX の印はロット№を引いた行にしか付かない。続きの行も EX として数える。"""
        rows = [{"LOT": "A", "ET": "EX"}, {"MAI": "5"}, {"LOT": "B"}, {"MAI": "5"}]
        self.assertEqual([k.ex for k in ts.lot_runs(rows)], [True, True, False, False])

    def test_全角のEXも同じ(self) -> None:
        self.assertTrue(ts.is_ex({"ET": "ストア ＥＸ"}))
        self.assertFalse(ts.is_ex({"ET": "反転作業"}))

    def test_VCが前のロットと違えばVC交換(self) -> None:
        rows = [{"LOT": "A", "VC": "両面"}, {"LOT": "B", "VC": "両面"}, {"LOT": "C", "VC": "片面"}]
        self.assertEqual([k.vc_change for k in ts.lot_runs(rows)], [False, False, True])

    def test_前の直と同じロットなら最初の行は変わり目ではない(self) -> None:
        rows = [{"LOT": "A", "VC": "両面"}, {"LOT": "B", "VC": "片面"}]
        kinds = ts.lot_runs(rows, ("A", "両面"))
        self.assertEqual([k.change for k in kinds], [False, True])
        self.assertEqual([k.vc_change for k in kinds], [False, True])

    def test_前の直と違うロットならVCも比べる(self) -> None:
        kinds = ts.lot_runs([{"LOT": "B", "VC": "片面"}], ("A", "両面"))
        self.assertTrue(kinds[0].change)
        self.assertTrue(kinds[0].vc_change)

    def test_前の直が分からなければ段取りから始まるがVCは比べない(self) -> None:
        kinds = ts.lot_runs([{"LOT": "A", "VC": "片面"}])
        self.assertTrue(kinds[0].change)
        self.assertFalse(kinds[0].vc_change)

    def test_最初の行にロット番号が無ければ変わり目とは言わない(self) -> None:
        kinds = ts.lot_runs([{"S": "ｲ", "TH": "10"}, {"LOT": "A"}])
        self.assertEqual([k.change for k in kinds], [False, True])


class AdjustWeightsTests(unittest.TestCase):
    def test_EXのロットは重い(self) -> None:
        same = {"MAI": "5", "TUT": "1"}
        w, notes = weights_of([{"LOT": "A", **same}, {**same}, {"LOT": "B", "ET": "EX", **same}, {**same}],
                              before=("Z", ""))
        # A の続き(5)と B の続き(5 × 1.2)
        self.assertEqual(w[1], D(5))
        self.assertEqual(w[3], D(6))
        self.assertIn("EX のロットの 2 行は 1.2倍で数えます", notes)

    def test_変わり目は1梱包目だけ長め(self) -> None:
        """5枚×2包の行は、1包目だけ 1.5倍(5×1.5 + 5 = 12.5)。続きの行は 10 のまま。"""
        w, notes = weights_of([{"LOT": "A", "MAI": "5", "TUT": "2"}, {"MAI": "5", "TUT": "2"}])
        self.assertEqual(w, [D("12.5"), D(10)])
        self.assertIn("ロットの変わり目 1 か所の1梱包目は段取りのぶん長め(1.5倍)", notes)

    def test_VCが変われば段取りとVC交換を足す(self) -> None:
        rows = [{"LOT": "A", "VC": "両面", "MAI": "5", "TUT": "1"},
                {"LOT": "B", "VC": "片面", "MAI": "5", "TUT": "1"}]
        w, notes = weights_of(rows, before=("A", "両面"))
        # A は前の直の続き(5)。B は 5 × (1 + 0.5 + 0.5)
        self.assertEqual(w, [D(5), D(10)])
        self.assertIn("そのうち VC が前のロットと違う 1 か所は、VC交換のぶんさらに長め(+0.5倍)", notes)

    def test_EXの変わり目はEXの重さの1梱包目を長く(self) -> None:
        w, _ = weights_of([{"LOT": "A", "ET": "EX", "MAI": "5", "TUT": "1"}], before=("Z", ""))
        self.assertEqual(w, [D(5) * D("1.2") * D("1.5")])

    def test_倍率を1にすれば梱包数の比のまま(self) -> None:
        rows = [{"LOT": "A", "ET": "EX", "VC": "両面", "MAI": "10"}, {"LOT": "B", "VC": "片面", "MAI": "5"}]
        w, notes = weights_of(rows, factors=ts.Factors(D(1), D(1), D(1)))
        self.assertEqual(w, [D(10), D(5)])
        self.assertEqual(notes, [])

    def test_長めにした行はほかより長く割り振られる(self) -> None:
        """依頼の形: 同じ梱包数でも、EX・変わり目の行の時間が長い。"""
        rows = [{"LOT": "A", "VC": "両面", "MAI": "5", "TUT": "1"}, {"MAI": "5", "TUT": "1"},
                {"MAI": "5", "TUT": "1"}, {"LOT": "B", "VC": "片面", "MAI": "5", "TUT": "1"},
                {"MAI": "5", "TUT": "1"}, {"LOT": "C", "VC": "片面", "ET": "EX", "MAI": "5", "TUT": "1"},
                {"MAI": "5", "TUT": "1"}]
        w, _ = weights_of(rows, before=("A", "両面"))
        plan = ts.split([D(0)] * len(rows), 420, weights=w)
        spans = [b - a for a, b in plan.slots]
        plain, vc_change, ex_change, ex_plain = spans[1], spans[3], spans[5], spans[6]
        self.assertGreater(vc_change, plain, spans)       # 段取り + VC交換
        self.assertGreater(ex_plain, plain, spans)        # EX の続きの行
        self.assertGreater(ex_change, ex_plain, spans)    # EX の変わり目(段取り)
        # 同じ重みでも区切りは5分刻みに寄せるので、5分までは違ってよい
        self.assertLessEqual(abs(spans[0] - spans[1]), 5, "前の直の続きのロットは段取りを足さない")
        self.assertEqual(w[0], w[1])
        self.assertEqual(sum(spans), 420)


class SetupStopTests(unittest.TestCase):
    """段取り変更の停止がある行は長くしないでください (v4.42.1)"""

    CODES = ts.setup_stop_codes({
        "設備停止": [StopReason("休憩食事", "0")],
        "不稼働": [StopReason("突発停止(機械)", "ｲ")],
        "ハンドリング": [StopReason("段取り変更", "A"), StopReason("内径カット", "B")],
    })

    def test_段取りの停止の記号は内訳の名前で見分ける(self) -> None:
        self.assertEqual(self.CODES, frozenset({"A"}))
        self.assertEqual(ts.setup_stop_codes({"x": [StopReason("段取替え", "Ｃ")]}), frozenset({"C"}))

    def test_段取り変更の停止がある変わり目は長くしない(self) -> None:
        rows = [{"LOT": "A", "VC": "両面", "MAI": "5", "TUT": "1", "S": "A", "TH": "15"},
                {"LOT": "B", "VC": "片面", "MAI": "5", "TUT": "1"}]
        w, notes = weights_of(rows, before=("Z", "両面"), setup_codes=self.CODES)
        self.assertEqual(w[0], D(5), "段取り変更の停止がある行は長くしない(VC交換も足さない)")
        self.assertEqual(w[1], D(10), "停止の無い変わり目は今までどおり(段取り + VC交換)")
        self.assertIn("段取り変更の停止がある 1 か所は長くしません(段取りは停止の分として渡します)", notes)
        self.assertIn("ロットの変わり目 1 か所の1梱包目は段取りのぶん長め(1.5倍)", notes)

    def test_停止の2つめ以降に打ってあっても同じ(self) -> None:
        rows = [{"LOT": "A", "MAI": "5", "S": "ｲ", "TH": "5", "STH": "A", "THT": "10"}]
        w, _ = weights_of(rows, setup_codes=self.CODES)
        self.assertEqual(w, [D(5)])

    def test_EXの倍率はそのまま(self) -> None:
        rows = [{"LOT": "A", "ET": "EX", "MAI": "5", "S": "A", "TH": "10"}]
        w, _ = weights_of(rows, setup_codes=self.CODES)
        self.assertEqual(w, [D(6)])

    def test_ほかの停止では長くする(self) -> None:
        rows = [{"LOT": "A", "MAI": "5", "TUT": "1", "S": "B", "TH": "10"}]
        w, _ = weights_of(rows, setup_codes=self.CODES)
        self.assertEqual(w, [D("7.5")])


class FactorsTests(unittest.TestCase):
    def test_既定(self) -> None:
        f = ts.Factors.read(None)
        self.assertEqual((f.ex, f.setup, f.vc), (D("1.2"), D("1.5"), D("1.5")))

    def test_設定の値を読む_読めない値は既定(self) -> None:
        f = ts.Factors.read({"ex": "1.3", "setup": 2, "vc": "abc"})
        self.assertEqual((f.ex, f.setup, f.vc), (D("1.3"), D(2), D("1.5")))

    def test_範囲は1から5(self) -> None:
        self.assertIsNone(ts.parse_factor("0.9"))
        self.assertIsNone(ts.parse_factor("5.1"))
        self.assertIsNone(ts.parse_factor("nan"))
        self.assertEqual(ts.parse_factor("１．５"), D("1.5"))


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class DistributeLotWebTests(WebTestCase):
    def setUp(self) -> None:
        super().setUp()
        from nippou import user_settings
        from nippou.config import KEY_TIME_SPLIT
        user_settings.save(KEY_TIME_SPLIT, True)

    def key(self):
        from nippou import work_context
        from app.routes.entry import build_shift_calculator

        ctx = work_context.get_context()
        return ctx.current_key(build_shift_calculator(self.repo().get_shift_times()))

    def plan(self, rows: dict) -> dict:
        res = self.post("/api/entry/distribute-times",
                        {"rows": rows, "header": {"worker": "山田"}, "day_shift": True, "dry_run": True})
        self.assertEqual(res.status_code, 200, res.get_json())
        return res.get_json()

    @staticmethod
    def spans(plan: dict) -> list[int]:
        return [(ts.parse_hhmm(r["end"]) - ts.parse_hhmm(r["start"])) % 1440 for r in plan["rows"]]

    def save_previous_shift(self, lot: str, vc: str) -> None:
        """前の直の最後のロット(手元の作業用DB)。"""
        from nippou.db.models import DetailRecord, HeaderRecord
        from nippou.logic.prev_shift import previous_of
        from nippou.logic.shift import parse_business_date
        from nippou.services.nippou_service import format_business_date

        day, line, shift = self.key()
        prev_shift, prev_day = previous_of(shift, parse_business_date(day))
        at = dict(report_date=format_business_date(prev_day), line=line, shift=prev_shift, page=1)
        self.repo().save(HeaderRecord(**at, worker="鈴木"),
                         [DetailRecord(**at, row_no=1, lot="Z", vc="片面", mai="5", tut="1"),
                          DetailRecord(**at, row_no=2, lot=lot, vc=vc, mai="5", tut="1")])

    def test_EXのロットの行はほかより長い(self) -> None:
        self.save_previous_shift("A", "両面")
        plan = self.plan({"1": {"LOT": "A", "VC": "両面", "MAI": "5", "TUT": "1"},
                          "2": {"MAI": "5", "TUT": "1"},
                          "3": {"LOT": "B", "VC": "両面", "ET": "EX", "MAI": "5", "TUT": "1"},
                          "4": {"MAI": "5", "TUT": "1"}})
        spans = self.spans(plan)
        self.assertGreater(spans[3], spans[1], spans)
        self.assertIn("EX のロットの 2 行は 1.2倍で数えます", plan["adjust_notes"])

    def test_変わり目の1梱包目とVC交換(self) -> None:
        self.save_previous_shift("A", "両面")
        plan = self.plan({"1": {"LOT": "A", "VC": "両面", "MAI": "5", "TUT": "1"},
                          "2": {"MAI": "5", "TUT": "1"},
                          "3": {"LOT": "B", "VC": "両面", "MAI": "5", "TUT": "1"},
                          "4": {"LOT": "C", "VC": "片面", "MAI": "5", "TUT": "1"}})
        spans = self.spans(plan)
        # 同じ重みでも区切りは5分刻みに寄せるので、5分までは違ってよい
        self.assertLessEqual(abs(spans[0] - spans[1]), 5, "前の直から続くロットは段取りを足さない")
        self.assertNotIn("3 か所", " ".join(plan["adjust_notes"]))
        self.assertGreater(spans[2], spans[1], spans)    # 段取り
        self.assertGreater(spans[3], spans[2], spans)    # 段取り + VC交換
        self.assertIn("ロットの変わり目 2 か所の1梱包目は段取りのぶん長め(1.5倍)", plan["adjust_notes"])

    def test_前の直の保存が無ければ最初の行も段取り(self) -> None:
        plan = self.plan({"1": {"LOT": "A", "MAI": "5", "TUT": "1"}, "2": {"MAI": "5", "TUT": "1"}})
        spans = self.spans(plan)
        self.assertGreater(spans[0], spans[1], spans)

    def test_ページをまたいでもロットは続く(self) -> None:
        """1ページ目の最後のロットが、2ページ目のロット№が空の行へ続く(EX も)。"""
        from nippou.db.models import DetailRecord, HeaderRecord

        self.save_previous_shift("A", "両面")
        day, line, shift = self.key()
        for number, details in ((1, [dict(row_no=1, lot="A", vc="両面"), dict(row_no=2, lot="B", vc="両面", et="EX")]),
                                (2, [dict(row_no=1)])):
            at = dict(report_date=day, line=line, shift=shift, page=number)
            self.repo().save(HeaderRecord(**at, worker="山田"),
                             [DetailRecord(**at, mai="5", tut="1", **d) for d in details])
        # いま開いているのは2ページ目(保存した最後のページ)。1行目はロット№が空
        plan = self.plan({"1": {"MAI": "5", "TUT": "1"}})
        self.assertEqual(plan["pages"], [1, 2], plan)
        self.assertIn("EX のロットの 2 行は 1.2倍で数えます", plan["adjust_notes"])

    def test_段取り変更の停止がある行は長くしない(self) -> None:
        """停止の分を引いた残り(作業の分)が、変わり目でない行と同じ。"""
        from unittest import mock

        reasons = {"ハンドリング": [StopReason("段取り変更", "A")]}
        self.save_previous_shift("A", "両面")
        rows = {"1": {"LOT": "A", "VC": "両面", "MAI": "5", "TUT": "1"},
                "2": {"LOT": "B", "VC": "片面", "MAI": "5", "TUT": "1", "S": "A", "TH": "20"},
                "3": {"LOT": "C", "VC": "片面", "MAI": "5", "TUT": "1"}}
        with mock.patch("nippou.access_bridge.stop_master.load_all_categories", return_value=reasons):
            plan = self.plan(rows)
        spans = self.spans(plan)
        work = [spans[0], spans[1] - 20, spans[2]]
        self.assertLessEqual(abs(work[1] - work[0]), 5, work)   # 段取りは停止の20分だけ
        self.assertGreater(work[2], work[0] + 5, work)           # 停止の無い変わり目は長め
        self.assertIn("段取り変更の停止がある 1 か所は長くしません(段取りは停止の分として渡します)",
                      plan["adjust_notes"])

    def test_停止理由を読めなくても割り振る(self) -> None:
        from unittest import mock

        with mock.patch("nippou.access_bridge.stop_master.load_all_categories", side_effect=OSError("届かない")):
            plan = self.plan({"1": {"LOT": "A", "MAI": "5", "S": "A", "TH": "10"}})
        self.assertEqual(plan["count"], 1)

    def test_倍率は管理者だけが変えられる(self) -> None:
        res = self.post("/api/settings/time-split-factors", {"ex": "2", "setup": "1.5", "vc": "1.5"})
        self.assertEqual(res.status_code, 403)
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        res = self.post("/api/settings/time-split-factors", {"ex": "6", "setup": "1.5", "vc": "1.5"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("EXの倍率は", res.get_json()["error"]["message"])
        body = self.post("/api/settings/time-split-factors", {"ex": "2", "setup": "1", "vc": "1"}).get_json()
        self.assertEqual(body["factors"], {"ex": 2.0, "setup": 1.0, "vc": 1.0})
        page = self.get("/settings").get_data(as_text=True)
        self.assertIn('id="factor-ex"', page)
        # 変えた倍率で割り振る: EX 2倍、段取りは足さない
        plan = self.plan({"1": {"LOT": "A", "ET": "EX", "MAI": "5", "TUT": "1"},
                          "2": {"LOT": "B", "MAI": "5", "TUT": "1"}})
        a, b = self.spans(plan)
        self.assertEqual(a + b, sum(self.spans(plan)))
        self.assertAlmostEqual(a / b, 2, delta=0.1)
        self.assertEqual(plan["adjust_notes"], ["EX のロットの 1 行は 2倍で数えます"])


if __name__ == "__main__":
    unittest.main()
