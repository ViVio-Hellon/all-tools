"""停止内訳: 分類1の内訳番号99だけは、内訳(名前)が空でもよい (VER4.44.12 から)

【利用者から】

    設定・管理者:停止内訳 CSVから読ませているが『16行目: 1,99, , ── 内訳(理由の名前)が
    ありません』となる。特殊ルールで、分類1 内訳番号99 で登録したものに限り空白を許して
    ください。かつそれに伴い 分類1 内訳番号99 の 停止集計も 空白 で計上してください
    集計に及ぼす影響が大きいですか?

守るのは:

    ・CSV: `1,99, , ` / `1,99,,` / `1,99` / 全角の `1,９９,` を読む。ほかの分類・ほかの記号は
      今までどおり「内訳(理由の名前)がありません」(決まりは `stop_csv.BLANK_LABEL_OK` の1か所)
    ・伝送用ファイルの表(作業停止時間内訳_1)の 99 も、名前が空で捨てない(_2・_3 や 98 は捨てる)
    ・99 は「停止内訳にない記号」にならない(選択欄に「99(名前なし)」・保存の確かめを通る・全停入力)
    ・集計: 99 は内訳の名前を空で計上。**分・回数・管理ロスなどの数字は、名前があるときと1つも
      変わらない**(集計管理・グラフ・日々のCSV・共有へ送る集計・月の書き出し)
"""
from __future__ import annotations

import csv
import json
import sqlite3
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import stop_csv                               # noqa: E402
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase      # noqa: E402

#: 利用者の16行目(半角の 99・名前の欄は空白1つ・備考も空白1つ)
USER_LINE = "1,99, , "


# ==================================================================
# CSV の読み(純ロジック)
# ==================================================================
class ParseTests(unittest.TestCase):
    def test_利用者の16行目を読む(self) -> None:
        text = "1,0,休憩食事\n" + USER_LINE + "\n"
        found = stop_csv.parse(text)
        self.assertEqual(found.problems, [], [p.reason for p in found.problems])
        self.assertEqual([(r.code, r.label) for r in found.of("設備停止")],
                         [("0", "休憩食事"), ("99", "")])

    def test_書き方の揺れ(self) -> None:
        """名前の欄が空・欄ごと省いた・全角の９９・分類を呼び名で、のどれでも読む。"""
        for line in ("1,99,,", "1,99,", "1,99", "1\t99\t\t", "管理ロス,99,,", "１,99,,",
                     "1,９９,,"):
            with self.subTest(line=line):
                found = stop_csv.parse(line + "\n")
                self.assertEqual(found.problems, [], [p.reason for p in found.problems])
                self.assertEqual(len(found.of("設備停止")), 1)
                self.assertEqual(found.of("設備停止")[0].label, "")

    def test_記号は書いたとおりに残す(self) -> None:
        """全角の９９を 99 とみなすのは「名前を空にしてよいか」だけ。記号は変えない。"""
        found = stop_csv.parse("1,９９,\n")
        self.assertEqual(found.of("設備停止")[0].code, "９９")

    def test_ほかは今までどおり断る(self) -> None:
        for line in ("1,98,", "1,9,", "1,990,", "2,99,", "3,99,", "2,ロ,", "3,A,", "1,98"):
            with self.subTest(line=line):
                found = stop_csv.parse(line + "\n")
                self.assertEqual(found.count, 0)
                self.assertEqual(len(found.problems), 1)
                self.assertEqual(found.problems[0].level, stop_csv.SKIP)
        self.assertEqual(stop_csv.parse("1,98,\n").problems[0].reason,
                         "内訳(理由の名前)がありません")
        self.assertIn("欄が足りません", stop_csv.parse("1,98\n").problems[0].reason)

    def test_名前を書いた99もそのまま読む(self) -> None:
        found = stop_csv.parse("1,99,その他(管理)\n")
        self.assertEqual(found.of("設備停止")[0].label, "その他(管理)")

    def test_決まりは1か所(self) -> None:
        self.assertEqual(stop_csv.BLANK_LABEL_OK, frozenset({("1", "99")}))
        self.assertTrue(stop_csv.blank_label_ok("設備停止", "99"))
        self.assertTrue(stop_csv.blank_label_ok("1", "９９"))
        self.assertFalse(stop_csv.blank_label_ok("不稼働", "99"))
        self.assertFalse(stop_csv.blank_label_ok("設備停止", "98"))
        self.assertFalse(stop_csv.blank_label_ok("設備停止", ""))

    def test_見本に書き出して読み直せる(self) -> None:
        found = stop_csv.parse("1,0,休憩食事\n" + USER_LINE + "\n2,イ,突発\n")
        text = stop_csv.as_csv(found.values)
        self.assertIn("\r\n1,99,,\r\n", text)
        again = stop_csv.parse(text)
        self.assertEqual(again.problems, [])
        self.assertEqual(again.values, found.values)
        self.assertIn("空にできるのは 1 の 99 だけ", text)      # 見本の覚え書き

    def test_画面に出す書き方(self) -> None:
        self.assertEqual(stop_csv.shown_text("99", ""), "99(名前なし)")
        self.assertEqual(stop_csv.shown_text("0", "休憩食事"), "0 休憩食事")


# ==================================================================
# 伝送用ファイルの表から読む(stop_master)
# ==================================================================
def make_transmission(path: Path, extra_1=(), extra_2=()) -> None:
    """作業停止時間内訳_1〜3。`extra_*` は (内訳, 内訳番号) を足す(None は NULL)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        for no, rows in ((1, [("休憩食事", "0"), ("TPM活動・清掃", "1"), *extra_1]),
                         (2, [("突発停止(機械)", "イ"), *extra_2]),
                         (3, [("段取り変更", "A")])):
            conn.execute(f'CREATE TABLE "作業停止時間内訳_{no}" '
                         '("管理番号" INTEGER, "内訳" TEXT, "内訳番号" TEXT, "備考" TEXT)')
            conn.executemany(f'INSERT INTO "作業停止時間内訳_{no}" VALUES (?,?,?,?)',
                             [(i, label, code, "") for i, (label, code) in enumerate(rows, 1)])
        conn.commit()
    finally:
        conn.close()


class MasterTableTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        from unittest import mock

        from nippou import source_db

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.master = root / "ref" / "伝送用ファイル.sqlite3"
        self.csv = root / "ref" / "停止内訳.csv"           # 置かない(表だけ)
        (root / "cache").mkdir()
        patcher = mock.patch.object(source_db, "copy_dir", return_value=root / "cache")
        patcher.start()
        self.addCleanup(patcher.stop)
        source_db.forget()
        self.addCleanup(source_db.forget)

    def load(self):
        from nippou.access_bridge import stop_master
        return stop_master.load_all_categories(self.master, csv_path=self.csv)

    def test_表の99は名前が空でも残す(self) -> None:
        make_transmission(self.master, extra_1=[("", "99"), (None, "98"), ("", "")],
                          extra_2=[("", "ロ")])
        got = self.load()
        self.assertEqual([(r.code, r.label) for r in got["設備停止"]],
                         [("0", "休憩食事"), ("1", "TPM活動・清掃"), ("99", "")])
        self.assertEqual([r.code for r in got["不稼働"]], ["イ"])  # _2 の名前なしは今までどおり捨てる

    def test_表の99の名前がNULLでも残す(self) -> None:
        make_transmission(self.master, extra_1=[(None, "99")])
        self.assertIn("99", [r.code for r in self.load()["設備停止"]])

    def test_表の99が数で入っていても残す(self) -> None:
        """Access の表では内訳番号が数の列のことがある(99 / 99.0)。"""
        self.master.parent.mkdir(parents=True, exist_ok=True)
        make_transmission(self.master)
        conn = sqlite3.connect(str(self.master))
        with conn:
            conn.execute('INSERT INTO "作業停止時間内訳_1" VALUES (9, NULL, 99, "")')
        conn.close()
        self.assertIn("99", [r.code for r in self.load()["設備停止"]])


# ==================================================================
# 選択欄・保存の確かめ・全停入力・集計
# ==================================================================
CSV_TEXT = "分類,内訳番号,内訳,備考\n1,0,休憩食事,\n1,1,TPM活動・清掃,\n{line99}\n2,イ,突発停止(機械),\n3,A,段取り変更,\n"
#: 名前を付けたときの比べ相手。**画面にもCSVにも出ない字**にして、消せば同じになるかで比べる
NAMED = "ナマエアリ"
DAY = "2026年8月3日"


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class WebTests(WebTestCase):
    def setUp(self) -> None:
        super().setUp()
        make_transmission(self.tmp / "ref" / "伝送用ファイル.sqlite3")
        self.csv = self.tmp / "ref" / "停止内訳.csv"
        self.write_csv(USER_LINE)

    def write_csv(self, line99: str) -> None:
        self.csv.parent.mkdir(parents=True, exist_ok=True)
        self.csv.write_text(CSV_TEXT.format(line99=line99), encoding="utf-8-sig")

    # ---- 選択欄 ------------------------------------------------------
    def test_選択欄に99が名前なしで出る(self) -> None:
        from nippou.presenters import entry

        choices = entry.stop_choices()
        first = next(g for g in choices if g["category"] == "設備停止")
        row = next(r for r in first["reasons"] if r["code"] == "99")
        self.assertEqual(row["label"], "")
        self.assertEqual(row["text"], "99(名前なし)")
        self.assertIn("99", entry.stop_codes(choices))           # 「マスタに無い」にしない
        html = self.get("/").get_data(as_text=True)
        self.assertIn('<option value="99"', html)
        self.assertIn("99(名前なし)", html)
        self.assertNotIn("99(マスタに無い)", html)

    def test_表の99も選択欄に出る(self) -> None:
        """CSVを置かずに、伝送用ファイルの表で名前を空にした 99。"""
        self.csv.unlink()
        path = self.tmp / "ref" / "伝送用ファイル.sqlite3"
        path.unlink()
        make_transmission(path, extra_1=[("", "99")])
        from nippou import source_db
        from nippou.presenters import entry

        source_db.forget()
        first = next(g for g in entry.stop_choices() if g["category"] == "設備停止")
        self.assertIn(("99", "99(名前なし)"), [(r["code"], r["text"]) for r in first["reasons"]])

    # ---- 保存の確かめ ----------------------------------------------------
    def test_保存の確かめで内訳にない記号にならない(self) -> None:
        from nippou.db.models import DetailRecord
        from nippou.logic import save_checks
        from nippou.services import shift_check

        codes = shift_check.known_stop_codes()
        self.assertIn("99", codes)
        row = DetailRecord(report_date=DAY, line="L-1", shift="1直", page=1, row_no=1,
                           s="99", th="30", ss="0", ths="60")
        self.assertEqual(save_checks.check_stop_codes([row], codes), [])
        # 比べ: マスタに無い記号はいつもどおり断る
        row.s = "98"
        self.assertEqual([f.code for f in save_checks.check_stop_codes([row], codes)],
                         [save_checks.UNKNOWN_STOP])

    # ---- 設定画面の一覧 ------------------------------------------------
    def test_設定画面の一覧に名前なしで出て_困りごとにしない(self) -> None:
        from nippou.presenters import settings as view

        sr = view.stop_reason_view()
        self.assertEqual(sr["problems"], [])
        first = next(g for g in sr["groups"] if g["category"] == "設備停止")
        self.assertIn({"code": "99", "label": "", "shown": "(名前なし)"}, first["reasons"])
        html = self.get("/settings").get_data(as_text=True)
        self.assertIn('<code>99</code> <span class="lead">(名前なし)</span>', html)

    def test_渡したCSVの下見でも断らない(self) -> None:
        import io

        from tests._web import HEADERS
        data = {"file": (io.BytesIO(("1,0,休憩食事\n" + USER_LINE + "\n").encode("utf-8-sig")),
                         "停止内訳.csv")}
        res = self.client.post("/api/settings/stop-reasons/upload", data=data,
                               headers=HEADERS, content_type="multipart/form-data")
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(res.get_json()["problems"], [])

    # ---- 全停入力 --------------------------------------------------------
    def test_全停入力の一覧に出て_記号だけで入れられる(self) -> None:
        body = self.get("/api/formstop/reasons").get_json()
        first = next(c for c in body["categories"] if c["category"] == "設備停止")
        self.assertIn({"label": "", "code": "99", "shown": "99(名前なし)"}, first["reasons"])
        res = self.post("/api/formstop/execute", {"reason": "", "code": "99", "worker": "山田"})
        self.assertEqual(res.status_code, 200, res.get_json())
        key = self.repo().list_keys()[0]
        self.assertEqual(self.repo().load(*key)[1][0].s, "99")

    def test_全停入力は記号も名前も無ければ今までどおり断る(self) -> None:
        res = self.post("/api/formstop/execute", {"reason": "", "code": "", "worker": "山田"})
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"], "empty")

    # ---- 段取り・VC交換の見分け(名前で見る) -------------------------------
    def test_時間の割り振りで段取りやVC交換にしない(self) -> None:
        from app.current import time_split_stop_kinds

        kinds = time_split_stop_kinds()
        self.assertNotIn("99", kinds.setup | kinds.vc)

    # ---- 集計 ------------------------------------------------------------
    def save_shift(self) -> None:
        """99 を2回(30分・15分)、ほかの分類も1つずつ打った1直。"""
        from nippou.db.models import DetailRecord, HeaderRecord

        key = dict(report_date=DAY, line="L-1", shift="1直", page=1)
        self.repo().save(HeaderRecord(**key, worker="山田", count="20", weight_kg="2000"), [
            DetailRecord(**key, row_no=1, lot="N7131T0", kz="08", kh="00", sz="10", sh="00",
                         con="10", wei="1000", mai="10", tim="120",
                         s="99", th="30", ss="イ", ths="20", sth="A", tht="10"),
            DetailRecord(**key, row_no=2, lot="N7131T1", kz="10", kh="00", sz="12", sh="00",
                         con="10", wei="1000", mai="10", tim="120",
                         s="0", th="45", ss="99", ths="15"),
        ])

    def collect(self) -> dict:
        """集計に出るものぜんぶ。**名前を入れ直してから作り直す**(`summary.rebuild`)。"""
        from dataclasses import asdict

        from nippou.access_bridge import sqlite_backend
        from nippou.presenters import agg_admin
        from nippou.reporting import csv_export, month_export
        from nippou.services import summary

        repo = self.repo()
        summary.rebuild(repo, date(2026, 8, 1), date(2026, 8, 31))
        out: dict = {}
        # 集計管理の画面(4つの表)
        out["agg"] = agg_admin.build(repo, line="L-1", start=date(2026, 8, 1),
                                     end=date(2026, 8, 31)).as_dict()
        # 集計グラフ(期間)
        out["graph"] = self.post("/api/graph/dashboard",
                                 {"start": "2026-08-01", "end": "2026-08-31",
                                  "mode": "period", "line": "L-1"}).get_json()
        # 日々のCSV(集計・停止内訳・明細)
        out["day_agg"] = [asdict(r) for r in csv_export.build_daily_aggregate_rows(repo, DAY, "L-1")]
        out["day_stop"] = csv_export.build_stop_rows(repo, DAY, "L-1")
        out["day_detail"] = csv_export.build_detail_rows(repo, DAY, "L-1")
        # 共有へ送る集計(3つの表)
        report, details = summary.build(summary._shift_pages(repo, DAY, "L-1", "1直"),
                                        DAY, "L-1", "1直", summary.stop_labels())
        shared = self.tmp / "shared" / f"共有_{len(list(self.tmp.glob('shared/*')))}.sqlite3"
        shared.parent.mkdir(exist_ok=True)
        res = sqlite_backend.push_summary(shared, report, details, "T_集計", "T_集計明細",
                                          "T_停止", saved_at="2026-08-03T10:00:00")
        self.assertTrue(res.success, res)
        conn = sqlite3.connect(str(shared))
        conn.row_factory = sqlite3.Row
        out["shared"] = {t: [dict(r) for r in conn.execute(f'SELECT * FROM "{t}"')]
                         for t in ("T_集計", "T_集計明細", "T_停止")}
        conn.close()
        # 月の書き出し(3本のCSV)
        base = self.tmp / f"month_{len(list(self.tmp.glob('month_*')))}"
        made = month_export.write_month(repo, year=2026, month=8, line="L-1", base_dir=base)
        out["month"] = {}
        for p in made.files:
            with open(p, encoding="utf-8-sig", newline="") as f:
                out["month"][Path(p).name] = list(csv.reader(f))
        return out

    def test_99は名前を空で計上し_数字は名前があるときと変わらない(self) -> None:
        from nippou.logic import save_checks
        from nippou.services import shift_check

        self.save_shift()
        blank = self.collect()
        # 保存・共有へ送る前の確かめで「停止内訳にない記号」にならない(送らずに残る直にならない)
        found = shift_check.run(self.repo(), DAY, "L-1", "1直")
        self.assertNotIn(save_checks.UNKNOWN_STOP, [f.code for f in found.findings],
                         [f.message for f in found.findings])

        # 停止内訳CSV: 99 は記号「99」・内訳は空・管理ロス・2回 45分
        from nippou.reporting import csv_export
        row99 = next(r for r in blank["day_stop"]
                     if r["記号"] == "99" and r["区分"] == csv_export.STOP_KIND_TOTAL)
        self.assertEqual((row99["内訳"], row99["分類"], row99["回数"], row99["停止時間(分)"]),
                         ("", "管理ロス設備停止", "2", "45"))
        # 管理ロスの合計に入っている(休憩 45 + 99 の 45)
        self.assertEqual(blank["day_agg"][0]["management_loss_minutes"], 90)
        # 共有の停止の表も、99 は内訳の欄が空・分類は管理ロス
        stop99 = [r for r in blank["shared"]["T_停止"] if r["記号"] == "99"]
        self.assertEqual([(r["内訳"], r["時間分"]) for r in stop99], [("", 30), ("", 15)])
        self.assertEqual({r["分類"] for r in stop99}, {blank["shared"]["T_停止"][0]["分類"]})

        # 名前を付けて作り直す → **名前を消せば全部同じ**(数字は1つも変わらない)
        self.write_csv(f"1,99,{NAMED},")
        named = self.collect()
        named_text = json.dumps(named, ensure_ascii=False, sort_keys=True)
        # 名前は確かに**どの出口にも**入っている(入っていない出口は比べたことにならない)
        for part in ("agg", "graph", "day_stop", "shared", "month"):
            with self.subTest(part=part):
                self.assertIn(NAMED, json.dumps(named[part], ensure_ascii=False))
        scrubbed = named_text.replace(f" {NAMED}", "").replace(NAMED, "")
        self.assertEqual(json.loads(scrubbed), blank)


if __name__ == "__main__":
    unittest.main()
