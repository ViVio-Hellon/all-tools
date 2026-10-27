"""分類1・内訳番号99 は管理ロスでなく「その他停止」/ 管理者だけの「内訳番号99 の時間」

【利用者の決定】(「1と2の組み合わせでお願いします」)

    超えた休憩は 分類1・内訳番号99(名前は空白)で打ってもらうことにした。ところが 99 は
    数字の記号なので、管理ロス(設備停止)に数えられ、設備の止まった時間として集計・
    グラフに足される ── 設備の改善を考える人が読み違える。

    1. 99 だけ管理ロスから外し、「その他」として分けて数える(名前は空白のまま。止まって
       いた時間としては今までどおり数え、作業時間には回さない)
    2. 99 の時間を数える表を管理者だけに出す(ライン別・直別・作業者別の合計と、月ごとの
       移り変わり。始めた日からの増減が分かる)

守るのは:

    ・分け方は1か所(`aggregation.OTHER_STOP_CODES` → `stop_kind_of`。`stop_csv.BLANK_LABEL_OK`
      もここから作る)
    ・99 は管理ロスに入らず「その他停止」に入る。**停止合計・稼働時間・稼働率・操業時間・作業時間は
      前と1分も変わらない**(集計管理・グラフ・途中式)
    ・集計CSV(日々・月の書き出しの 〜_集計.csv)は「その他(分)」の欄を足し、99 はそこへ
      (管理ロスの欄から抜ける。4つの和 = 停止合計 = 1440 − 稼働。停止内訳 CSV と数がそろう)
      ── 利用者:「集計 CSV と停止内訳の CSV で違いがあってはおかしいです」
    ・共有へ送る集計の表と停止の表は**形も中身も前のまま**(99 は管理ロスの欄・分類は
      「管理ロス停止」── 利用者:「共有へ送る表は管理ロス」)。送り済みの直が「未送信」に戻らない
    ・操業時間からは 99 も引く(利用者:「操業時間から引くで良いです」)
    ・分ける前に作った集計も、読むとき(`fill_missing`)・作り直し(`rebuild`)で分かれる
    ・「内訳番号99 の時間」は管理者(管理者モード・Administrator の PC)だけ。ほかの端末には
      面もサーバの口も無い(403)。数は集計管理の「その他」と合う

画面(本物の Chromium)は `tests/test_code99_browser.py`。
"""
from __future__ import annotations

import csv
import sqlite3
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.models import DetailRecord, HeaderRecord                  # noqa: E402
from nippou.logic import aggregation, formula, packing_agg, stop_csv     # noqa: E402
from nippou.logic.aggregation import (OTHER_STOP_KIND, aggregate_shift,  # noqa: E402
                                      stop_kind_of)
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase               # noqa: E402

DAY = "2026年8月3日"
def old_rule(module=None):
    """前の形(99 も管理ロス)に戻す ── 分ける前の版のつもりで集計を作るとき。

    **いま読み込まれている** `aggregation` を差し替えます(Web の試験は1件ごとに
    モジュールを読み直すので、この試験の頭で読んだものとは別物になる)。
    """
    module = module or sys.modules["nippou.logic.aggregation"]
    return patch.object(module, "OTHER_STOP_CODES", frozenset())

#: 集計CSV(日々・月の書き出し)の見出し。**3分類の次・停止合計の前に「その他(分)」**
AGG_FIELDNAMES = [
    "日付", "ライン", "直", "合計枚数", "合計重量(t)",
    "作業時間合計(分)", "稼働時間合計(分)", "操業時間(分)",
    "管理ロス設備停止(分)", "段取り・突発停止(分)", "ハンドリング停止(分)", "その他(分)",
    "停止合計(分)", "稼働率(%)", "生産性(t/h)",
]
#: 停止の4つの欄(和が停止合計)
STOP_COLUMNS = ("管理ロス設備停止(分)", "段取り・突発停止(分)", "ハンドリング停止(分)",
                "その他(分)")
#: 共有へ送る集計の表・停止の表の列。**前の版のまま**
FIXED_SUMMARY_COLUMNS = (
    "報告日", "ライン", "直", "ページ数", "担当者", "昼勤", "理由",
    "枚数", "重量Kg", "Lot数", "係数Lot数", "作業時間分", "操業時間分",
    "管理ロス停止分", "突発停止分", "ハンドリング停止分",
    "稼働時間分", "稼働率", "生産性", "保存日時",
)
FIXED_STOP_COLUMNS = ("報告日", "ライン", "直", "ページ", "行番号", "停止番号",
                      "記号", "内訳", "分類", "時間分", "保存日時")


def shift_rows(code99: str = "99", worker: str = "山田") -> tuple[HeaderRecord, list[DetailRecord]]:
    """99 を2回(30分・15分)、ほかの分類も1つずつ打った1直。

    管理ロス(休憩 0)45 / 突発 20 / ハンドリング 10 / 99 45 → 止まっていた時間 120。
    `code99` を "1" にすると「99 の代わりに管理ロスの記号を打った」同じ直になる。
    """
    key = dict(report_date=DAY, line="L-1", shift="1直", page=1)
    header = HeaderRecord(**key, worker=worker, count="20", weight_kg="2000")
    return header, [
        DetailRecord(**key, row_no=1, lot="N7131T0", kz="08", kh="00", sz="10", sh="00",
                     con="10", wei="1000", mai="10", tim="120",
                     s=code99, th="30", ss="イ", ths="20", sth="A", tht="10"),
        DetailRecord(**key, row_no=2, lot="N7131T1", kz="10", kh="00", sz="12", sh="00",
                     con="10", wei="1000", mai="10", tim="120",
                     s="0", th="45", ss=code99, ths="15"),
    ]


# ==================================================================
# 分け方(純ロジック)
# ==================================================================
class KindTests(unittest.TestCase):
    def test_99は管理ロスでなくその他停止(self) -> None:
        for code in ("99", "９９", " 99 "):
            with self.subTest(code=code):
                self.assertEqual(stop_kind_of(code), OTHER_STOP_KIND)
        # ほかの数字は今までどおり管理ロス、片仮名・英字・混ざりも今までどおり
        for code, kind in (("0", "管理ロス停止"), ("9", "管理ロス停止"), ("98", "管理ロス停止"),
                           ("990", "管理ロス停止"), ("099", "管理ロス停止"),
                           ("イ", "突発停止"), ("A", "ハンドリング停止"), ("1A", "その他")):
            with self.subTest(code=code):
                self.assertEqual(stop_kind_of(code), kind)

    def test_決まりは1か所(self) -> None:
        """名前を空にしてよい記号も、その他停止に数える記号も、同じ1か所から。"""
        self.assertEqual(stop_csv.BLANK_LABEL_OK,
                         frozenset(("1", c) for c in aggregation.OTHER_STOP_CODES))
        with old_rule(aggregation):
            self.assertEqual(stop_kind_of("99"), "管理ロス停止")
            header, details = shift_rows()
            self.assertEqual(aggregate_shift(header, details).other_stop_minutes, 0)

    def test_99は管理ロスから抜けその他に入る_合計は変わらない(self) -> None:
        header, details = shift_rows()
        got = aggregate_shift(header, details)
        self.assertEqual((got.management_loss_minutes, got.unplanned_stop_minutes,
                          got.handling_stop_minutes, got.other_stop_minutes), (45, 20, 10, 45))
        # 比べ相手: 同じ直を前の分け方で(99 は管理ロス)
        with old_rule(aggregation):
            old = aggregate_shift(*shift_rows())
        self.assertEqual(old.management_loss_minutes, 90)
        for name in ("total_stop_minutes", "operating_minutes", "operating_rate_pct",
                     "operational_minutes", "work_minutes", "productivity_t_per_h"):
            with self.subTest(name=name):
                self.assertEqual(getattr(got, name), getattr(old, name))
        self.assertEqual(got.total_stop_minutes, 120)

    def test_ロット行から出した合計も同じ(self) -> None:
        header, details = shift_rows()
        rows = packing_agg.build_rows(header, details)
        self.assertEqual(packing_agg.totals(rows), aggregate_shift(header, details))
        kinds = {s.stop_code: s.stop_kind for r in rows for s in r.stops}
        self.assertEqual(kinds["99"], OTHER_STOP_KIND)
        self.assertEqual(kinds["0"], "管理ロス停止")

    def test_項目ごとの停止でもその他停止(self) -> None:
        header, details = shift_rows()
        items = {i.code: i for i in aggregation.stop_items([(header, details)])}
        self.assertEqual((items["99"].kind, items["99"].count, items["99"].minutes),
                         (OTHER_STOP_KIND, 2, 45))
        self.assertEqual(aggregation.stop_kind_label(OTHER_STOP_KIND), "その他停止")

    def test_CSVで99を分類2に書いたらその他停止と言う(self) -> None:
        found = stop_csv.parse("2,99,何か\n")
        self.assertEqual([p.level for p in found.problems], [stop_csv.WARN])
        self.assertIn("その他停止に数えられます", found.problems[0].reason)

    def test_途中式(self) -> None:
        header, details = shift_rows()
        agg = aggregate_shift(header, details)
        steps = {s.key: s for s in formula.shift_steps(agg)}
        self.assertEqual(steps["stop_total"].formula, "管理ロス + 突発 + ハンドリング + その他")
        self.assertEqual(steps["stop_total"].substituted, "45 + 20 + 10 + 45")
        self.assertEqual(steps["stop_total"].result, "120 分")
        self.assertEqual(steps["operational_minutes"].substituted, "1440 − 45 − 45")
        self.assertEqual(steps["operational_minutes"].result, "1350 分")
        # 99 の無い直の式は前と1文字も変わらない
        plain = aggregate_shift(*shift_rows(code99="1"))
        steps = {s.key: s for s in formula.shift_steps(plain)}
        self.assertEqual(steps["stop_total"].formula, "管理ロス + 突発 + ハンドリング")
        self.assertEqual(steps["operational_minutes"].formula, "1440分 − 管理ロス停止")


# ==================================================================
# 集計の出口
# ==================================================================
@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class OutletTests(WebTestCase):
    def save_shift(self, worker: str = "山田") -> None:
        self.repo().save(*shift_rows(worker=worker))

    def rebuild(self) -> None:
        from nippou.services import summary
        summary.rebuild(self.repo(), date(2026, 8, 1), date(2026, 8, 31))

    def agg_shift_row(self) -> dict:
        from nippou.presenters import agg_admin
        view = agg_admin.build(self.repo(), line="L-1", start=date(2026, 8, 1),
                               end=date(2026, 8, 31))
        table = view.table("shifts")
        self.assertEqual(len(table.rows), 1, table.rows)
        return dict(zip(table.columns, table.rows[0]))

    def test_集計管理の直別_管理ロスに入らずその他に入る(self) -> None:
        self.save_shift()
        row = self.agg_shift_row()
        self.assertEqual((row["管理ロス"], row["突発"], row["ﾊﾝﾄﾞﾘﾝｸﾞ"], row["その他"]),
                         ("45", "20", "10", "45"))
        self.assertEqual(row["稼働(分)"], "1320")       # 1440 − 120(止まっていた時間は前と同じ)
        self.assertEqual(row["操業(分)"], "1350")       # 1440 − 管理ロス − その他(前と同じ)
        self.assertEqual(row["作業(分)"], "240")

    def test_グラフの分類_その他停止が別の扇(self) -> None:
        self.save_shift()
        body = self.post("/api/graph/dashboard", {"start": "2026-08-01", "end": "2026-08-31",
                                                  "mode": "period", "line": "L-1"}).get_json()
        tile = next(t for t in body["tiles"] if t["key"] == "stop_kinds")
        slices = {s["label"]: s["value"] for s in tile["slices"]}
        self.assertEqual(slices, {"管理ロス設備停止": 45, "その他停止": 45,
                                  "段取り・突発停止": 20, "ハンドリング停止": 10})
        self.assertEqual(tile["total"], "120")

    def test_停止内訳CSVの分類はその他停止(self) -> None:
        from nippou.reporting import csv_export
        self.save_shift()
        rows = csv_export.build_stop_rows(self.repo(), DAY, "L-1")
        total = next(r for r in rows if r["記号"] == "99"
                     and r["区分"] == csv_export.STOP_KIND_TOTAL)
        self.assertEqual((total["分類"], total["回数"], total["停止時間(分)"]),
                         ("その他停止", "2", "45"))
        # 割合はその直の停止ぜんぶ(120分)に対して(前と同じ)
        self.assertEqual(total["その直の停止に占める割合(%)"], "37.5")

    def check_agg_csv(self, path: Path) -> None:
        """集計CSVの1直: 99 は「その他(分)」、管理ロスから抜ける、4つの和 = 停止合計。"""
        from nippou.reporting import csv_export
        with open(path, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(list(rows[0]), AGG_FIELDNAMES)
        row = rows[0]
        self.assertEqual((row["管理ロス設備停止(分)"], row["その他(分)"]), ("45", "45"))
        self.assertEqual(row["停止合計(分)"], "120")
        self.assertEqual(sum(int(row[c]) for c in STOP_COLUMNS), int(row["停止合計(分)"]))
        self.assertEqual(row["稼働時間合計(分)"], "1320")          # 1440 − 停止合計
        self.assertEqual(row["操業時間(分)"], "1350")              # 1440 − 管理ロス − その他
        # 停止内訳 CSV と同じ数え方(管理ロス・その他停止の分がそろう)
        stops = csv_export.build_stop_rows(self.repo(), DAY, "L-1")
        by_kind: dict[str, int] = {}
        for r in stops:
            if r["区分"] == csv_export.STOP_KIND_TOTAL:
                by_kind[r["分類"]] = by_kind.get(r["分類"], 0) + int(r["停止時間(分)"])
        self.assertEqual((by_kind["管理ロス設備停止"], by_kind["その他停止"]),
                         (int(row["管理ロス設備停止(分)"]), int(row["その他(分)"])))

    def test_集計CSVにその他の欄_99はそこへ(self) -> None:
        from nippou.reporting import csv_export
        self.assertEqual(csv_export.FIELDNAMES, AGG_FIELDNAMES)
        self.save_shift()
        out = self.tmp / "day.csv"
        csv_export.write_daily_aggregate_csv(self.repo(), DAY, "L-1", out)
        self.check_agg_csv(out)

    def test_月の書き出し_集計にもその他の欄_停止集計はその他停止(self) -> None:
        from nippou.reporting import month_export
        self.save_shift()
        made = month_export.write_month(self.repo(), year=2026, month=8, line="L-1",
                                        base_dir=self.tmp / "month")
        files = {Path(p).name: p for p in made.files}
        self.check_agg_csv(next(p for n, p in files.items() if n.endswith("_集計.csv")))
        stops = next(p for n, p in files.items() if n.endswith("_停止集計.csv"))
        with open(stops, encoding="utf-8-sig", newline="") as f:
            got = [r for r in csv.DictReader(f) if r["記号"] == "99"]
        self.assertEqual({r["分類"] for r in got}, {"その他停止"})

    def test_CSVの読み方にその他の欄の説明(self) -> None:
        from nippou.reporting import csv_legend
        text = csv_legend.DESCRIPTIONS["その他(分)"]
        self.assertIn("内訳番号99", text)
        self.assertIn("v4.44.14 から増えた欄", text)
        self.assertIn("その他", csv_legend.DESCRIPTIONS["停止合計(分)"])
        self.assertIn("内訳番号99 は入りません", csv_legend.DESCRIPTIONS["管理ロス設備停止(分)"])

    def test_共有へ送る集計は欄も中身も前のまま(self) -> None:
        from nippou.access_bridge import sqlite_backend
        from nippou.services import summary
        self.assertEqual(sqlite_backend.SUMMARY_COLUMNS, FIXED_SUMMARY_COLUMNS)
        self.assertEqual(sqlite_backend.STOP_DETAIL_COLUMNS, FIXED_STOP_COLUMNS)
        self.save_shift()

        def push(name: str) -> dict:
            report, details = summary.build(summary._shift_pages(self.repo(), DAY, "L-1", "1直"),
                                            DAY, "L-1", "1直", {})
            shared = self.tmp / f"{name}.sqlite3"
            res = sqlite_backend.push_summary(shared, report, details, "T_集計", "T_集計明細",
                                              "T_停止", saved_at="2026-08-03T10:00:00")
            self.assertTrue(res.success, res)
            conn = sqlite3.connect(str(shared))
            conn.row_factory = sqlite3.Row
            try:
                return {t: [dict(r) for r in conn.execute(f'SELECT * FROM "{t}"')]
                        for t in ("T_集計", "T_集計明細", "T_停止")}
            finally:
                conn.close()

        now = push("now")
        head = now["T_集計"][0]
        self.assertEqual(head["管理ロス停止分"], 90)
        self.assertEqual(head["管理ロス停止分"] + head["突発停止分"] + head["ハンドリング停止分"],
                         1440 - head["稼働時間分"])
        self.assertEqual({r["分類"] for r in now["T_停止"] if r["記号"] == "99"}, {"管理ロス停止"})
        # 前の分け方で送ったものと**1つも違わない**
        with old_rule():
            old = push("old")
        self.assertEqual(now, old)

    def test_送り済みの直は_分けても未送信に戻らない(self) -> None:
        self.save_shift()
        with old_rule():                 # 分ける前の版で作って送った
            self.rebuild()
        key = (DAY, "L-1", "1直")
        self.repo().mark_packing_report_synced(key)
        self.assertFalse(self.repo().load_packing_report(*key).dirty)
        self.rebuild()                                 # この版で作り直す
        report = self.repo().load_packing_report(*key)
        self.assertEqual((report.equipment_stop_total, report.other_stop_total), (45, 45))
        self.assertFalse(report.dirty, "99 を分けただけで送り直しになっています")

    def test_分ける前に作った集計も読むときに分かれる(self) -> None:
        from nippou.services import summary
        self.save_shift()
        with old_rule():
            self.rebuild()
        stored = self.repo().load_packing_report(DAY, "L-1", "1直")
        self.assertEqual((stored.equipment_stop_total, stored.other_stop_total), (90, 0))
        # 何も押さずに集計管理を開く(`fill_missing` が作り直す)
        row = self.agg_shift_row()
        self.assertEqual((row["管理ロス"], row["その他"]), ("45", "45"))
        # もう作り直すものは無い(次に開いたときは明細を読まない)
        self.assertEqual(summary.fill_missing(self.repo(), date(2026, 8, 1),
                                              date(2026, 8, 31)), 0)

    def test_設定の作り直しでも分かれる_止まっていた時間は前と同じ(self) -> None:
        from nippou.services import summary
        self.save_shift()
        start, end = date(2026, 8, 1), date(2026, 8, 31)

        def numbers() -> list:
            points = summary.day_points(self.repo(), start, end, "L-1")
            return [(p.stop_minutes, p.operating_minutes, p.operating_rate_pct,
                     p.work_minutes, p.productivity_t_per_h) for p in points]

        with old_rule():
            self.rebuild()
            before = numbers()
        self.rebuild()
        self.assertEqual(numbers(), before)
        stored = self.repo().load_packing_report(DAY, "L-1", "1直")
        self.assertEqual((stored.equipment_stop_total, stored.other_stop_total), (45, 45))

    def test_印刷の集計にその他が出る(self) -> None:
        from nippou.reporting import print_format
        from nippou.services import summary
        self.save_shift()
        report = summary.for_date(self.repo(), DAY, "L-1")[0]
        html = print_format._summary_html(report, None)
        self.assertIn("<b>管理ロス(分):</b> 45", html)
        self.assertIn("<b>その他(分):</b> 45", html)
        # 99 の無い直の紙には「その他」の行を出さない(前と同じ)
        report.other_stop_total = 0
        self.assertNotIn("その他(分)", print_format._summary_html(report, None))


# ==================================================================
# 内訳番号99 の時間(管理者だけ)
# ==================================================================
@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class Code99Tests(WebTestCase):
    SPAN = {"start": "2026-08-01", "end": "2026-08-31"}

    def setUp(self) -> None:
        super().setUp()
        from tests.test_scenario_shift_flow import _Now, install_clock
        from datetime import datetime
        _Now.value = datetime(2026, 8, 31, 10, 0)
        install_clock(self)

    def admin(self) -> None:
        from nippou import work_context
        work_context.get_context().admin = True

    def save(self, day: str, shift: str, worker: str, code99: str = "99") -> None:
        header, details = shift_rows(code99=code99, worker=worker)
        for rec in (header, *details):
            rec.report_date, rec.shift = day, shift
        self.repo().save(header, details)

    def tables(self, body: dict) -> dict:
        return {t["key"]: [dict(zip(t["columns"], r)) for r in t["rows"]] for t in body["tables"]}

    def test_一般の端末には面も口も無い(self) -> None:
        self.save(DAY, "1直", "山田")
        page = self.get("/agg").get_data(as_text=True)
        self.assertNotIn('id="code99"', page)
        self.assertNotIn("内訳番号99", page)
        for path in ("/api/agg/code99", "/api/agg/code99/csv"):
            with self.subTest(path=path):
                res = self.post(path, self.SPAN)
                self.assertEqual(res.status_code, 403)
                self.assertEqual(res.get_json()["error"]["code"], "not_admin")
        self.assertFalse(list(self.tmp.rglob("集計_内訳番号99_*.csv")))

    def test_管理者モードには面が出る(self) -> None:
        self.admin()
        page = self.get("/agg").get_data(as_text=True)
        self.assertIn('id="code99"', page)
        self.assertIn("内訳番号99 の時間", page)
        self.assertEqual(self.post("/api/agg/code99", self.SPAN).status_code, 200)

    def test_Administratorの端末にも出る(self) -> None:
        from nippou.services import access_rights
        with patch.object(access_rights, "is_administrator", return_value=True):
            self.assertIn('id="code99"', self.get("/agg").get_data(as_text=True))
            self.assertEqual(self.post("/api/agg/code99", self.SPAN).status_code, 200)

    def test_数が合う_ライン別_直別_作業者別_月ごと(self) -> None:
        self.admin()
        self.save("2026年7月20日", "2直", "佐藤")              # 使い始めた日(期間の前)
        self.save(DAY, "1直", "山田 佐藤")                     # 2人の直
        self.save("2026年8月4日", "3直", "鈴木", code99="1")   # 99 の無い直
        body = self.post("/api/agg/code99", self.SPAN).get_json()
        t = self.tables(body)
        self.assertEqual(body["first_date"], "2026-07-20")
        self.assertEqual(body["days_since"], 42)               # 7/20 → 8/31
        self.assertIn("2026-07-20(今日まで 42日)", body["first_text"])
        line = t["code99_lines"][0]
        self.assertEqual((line["ライン"], line["回数"], line["合計(分)"], line["打った直"],
                          line["保存のある直"], line["1直あたり(分)"], line["最初に打った日"],
                          line["その日から(日)"]),
                         ("L-1", "2", "45", "1", "2", "22.5", "2026-07-20", "42"))
        shifts = {r["直"]: r for r in t["code99_shifts"]}
        self.assertEqual(set(shifts), {"1直", "3直"})          # 期間の中の保存のある直
        self.assertEqual((shifts["1直"]["合計(分)"], shifts["3直"]["合計(分)"]), ("45", "0"))
        workers = {r["作業者"]: r["合計(分)"] for r in t["code99_workers"]}
        self.assertEqual(workers, {"山田": "45", "佐藤": "45"})   # 2人の直は2人に同じ分
        self.assertEqual([r["月"] for r in t["code99_months"]], ["2026-08"])
        # 期間を空にすると使い始めた月の1日から。月ごとに前の月との差
        body = self.post("/api/agg/code99", {"start": "", "end": "2026-08-31"}).get_json()
        self.assertEqual(body["start"], "2026-07-01")
        months = self.tables(body)["code99_months"]
        self.assertEqual([(r["月"], r["合計(分)"], r["前の月との差(分)"]) for r in months],
                         [("2026-07", "45", ""), ("2026-08", "45", "0")])

    def test_集計管理のその他と同じ数(self) -> None:
        self.admin()
        self.save(DAY, "1直", "山田")
        self.save("2026年8月5日", "2直", "佐藤")
        agg = self.post("/api/agg/tables", self.SPAN).get_json()
        shifts = next(t for t in agg["tables"] if t["key"] == "shifts")
        other = sum(int(r[shifts["columns"].index("その他")]) for r in shifts["rows"])
        body = self.post("/api/agg/code99", self.SPAN).get_json()
        self.assertEqual(other, 90)
        self.assertEqual(self.tables(body)["code99_lines"][0]["合計(分)"], str(other))

    def test_Administratorはライン別に控えのラインも並ぶ_混ぜない(self) -> None:
        from nippou import config, user_settings
        from nippou.db.connection import connect
        from nippou.db.repository import NippouRepository
        from nippou.services import access_rights
        from nippou.services import local_backup as lb
        share = self.tmp / "共有"
        share.mkdir()
        user_settings.save_many({config.KEY_ACCESS_DIR: str(share)})
        lb.reset()
        self.addCleanup(lb.reset)
        self.save(DAY, "1直", "山田")
        other = NippouRepository(connect(self.tmp / "hvc-pc.sqlite3"))
        self.addCleanup(other.conn.close)
        header, details = shift_rows(worker="田中")
        for rec in (header, *details):
            rec.line = "HVC"
        details[1].ss = ""                                   # HVC は 99 が 30分だけ
        other.save(header, details)
        lb.flush(other, force=True)
        before = lb.path_for("HVC").read_bytes()
        with patch.object(access_rights, "is_administrator", return_value=True):
            body = self.post("/api/agg/code99", self.SPAN).get_json()
            lines = {r["ライン"]: r["合計(分)"] for r in self.tables(body)["code99_lines"]}
            self.assertEqual((lines.get("L-1"), lines.get("HVC")), ("45", "30"), lines)
            # 直別・作業者別は選んだラインだけ(混ぜない)
            workers = {r["作業者"] for r in self.tables(body)["code99_workers"]}
            self.assertEqual(workers, {"山田"})
            picked = self.post("/api/agg/code99", {**self.SPAN, "line": "HVC"}).get_json()
            self.assertEqual({r["作業者"] for r in self.tables(picked)["code99_workers"]},
                             {"田中"})
        self.assertEqual(lb.path_for("HVC").read_bytes(), before)  # 控えには書かない

    def test_CSVに4本(self) -> None:
        from nippou.config import SETTINGS
        self.admin()
        self.save(DAY, "1直", "山田")
        body = self.post("/api/agg/code99/csv", self.SPAN).get_json()
        self.assertEqual(len(body["files"]), 4, body)
        out = Path(SETTINGS.report_output_dir) / "集計_内訳番号99_作業者別_2026-08-01_2026-08-31_L-1.csv"
        with open(out, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows, [["作業者", "回数", "合計(分)", "打った直"], ["山田", "2", "45", "1"]])


if __name__ == "__main__":
    unittest.main()
