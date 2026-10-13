"""記録を見る「ロットを探す」 (v4.33.0)

    集計機能は充実してきたが、履歴機能としてはどうですか?
    期間を絞ってロットNo検索したい場合に何を検索したらいいですか?

    「ロットを探す」を作ってください。CSV に出す機能も付けます

【約束】
    ・ロット№は**前方一致**(頭の数桁)。全角・小文字・前後の空白は揃えて比べる
      (日報入力のロット欄と同じ直し方)。保存してある側が全角でも当たる
    ・空欄は断る(期間の全部を出さない)。期間が逆・空・形違いも断る
    ・期間は報告日で、両端を含む。既定は直近3か月(2か月前の月初〜当日)
    ・ラインは**だれでも選べる**(定義のラインと「全ライン」。見るだけ)。1つのラインを
      選べばそのラインだけ。共有を読めないとき、ほかのラインは控えから読む(控えには
      書かない)が、控えを読めるのは Administrator の PC・管理者モードだけ
    ・探す元は共有の日報データ + この端末(共有と控えは `test_lot_search_shared.py`)
    ・並びは 作業日 → 直 → ページ → 行。直をまたいだロットは全部の行が出る
    ・上限を超えたら先のぶんだけ出して「多すぎるので絞ってください」
    ・当たった行の鍵で、紙・下見・呼び出しの既存の道がそのまま通る
    ・CSV は出力先のフォルダへ(ダウンロードにしない)。BOM付きUTF-8・画面の表そのまま
"""
from __future__ import annotations

import csv
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou import config  # noqa: E402
from nippou.db.connection import connect  # noqa: E402
from nippou.db.models import DetailRecord, HeaderRecord  # noqa: E402
from nippou.db.repository import NippouRepository  # noqa: E402
from nippou.presenters import lot_search  # noqa: E402
from nippou.reporting import csv_export  # noqa: E402
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402

DAY = "2026年10月1日"
SPAN = {"start": "2026-09-01", "end": "2026-10-31"}


def save(repo: NippouRepository, *, day: str = DAY, line: str = "L-1", shift: str = "1直",
         page: int = 1, worker: str = "山田", lots=("N7131T0",), dirty: bool = True) -> None:
    """1ページぶんを保存する。`lots` は1行目から順に。"""
    key = dict(report_date=day, line=line, shift=shift, page=page)
    rows = [DetailRecord(**key, row_no=n, lot=lot, zai="A5052-H34", siz="8.000×1528.0×3053.0",
                         ken="10", kz="08", kh="05", sz="09", sh="30", con="10", wei="100",
                         others2="一般", others3="〇〇工業")
            for n, lot in enumerate(lots, 1)]
    repo.save(HeaderRecord(**key, worker=worker), rows)
    if not dirty:
        repo.mark_synced((day, line, shift, page))


class LotSearchTests(unittest.TestCase):
    """探す処理(`presenters/lot_search.py`)。DB は一時ファイル。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.conn = connect(Path(self._tmp.name) / "t.sqlite3")
        self.addCleanup(self.conn.close)
        self.repo = NippouRepository(self.conn)
        # 共有の日報データは**無い**(フォルダごと)。この端末の分だけで探す。
        # 本物の置き場所を読みに行かないよう、必ず一時フォルダの道を渡す
        self.shared = Path(self._tmp.name) / "共有" / "日報データ.sqlite3"

    def find(self, lot: str, *, line: str = "L-1", start: str = SPAN["start"],
             end: str = SPAN["end"], own_line: str = "L-1", editor: bool = False):
        query = lot_search.LotQuery.of({"lot": lot, "start": start, "end": end}, line)
        return lot_search.search(self.repo, query, own_line=own_line, editor=editor,
                                 shared_path=self.shared)

    def lots(self, found) -> list[str]:
        return [h["lot"] for h in found.hits]

    # -- 一致のしかた -----------------------------------------------------
    def test_頭の数桁で当たる_前方一致(self) -> None:
        save(self.repo, lots=("N7131T0", "N715C50", "L715C50", "AN71000"))
        found = self.find("N71")
        self.assertEqual(self.lots(found), ["N7131T0", "N715C50"])
        # 途中に含むだけ(AN71000)・別の頭(L715C50)は当たらない
        self.assertEqual(self.lots(self.find("715")), [])
        self.assertEqual(self.lots(self.find("N7131T0")), ["N7131T0"])

    def test_全角_小文字_前後の空白を揃えて比べる(self) -> None:
        save(self.repo, lots=("N7131T0",))
        for typed in ("ｎ７１", " N71 ", "　Ｎ７１３", "n7131t0"):
            with self.subTest(typed=typed):
                self.assertEqual(self.lots(self.find(typed)), ["N7131T0"])

    def test_保存してある側が全角でも当たる(self) -> None:
        """取り込みなどで全角のまま入った行も、同じ直し方で比べる。"""
        save(self.repo, lots=("Ｎ７１３１Ｔ０",))
        self.assertEqual(len(self.find("N713").hits), 1)

    def test_空欄は断る(self) -> None:
        for typed in ("", "   "):
            with self.subTest(typed=typed):
                with self.assertRaises(lot_search.BadRequest) as caught:
                    self.find(typed)
                self.assertEqual(caught.exception.field, "lot")
                self.assertIn("頭の数桁だけでも", str(caught.exception))

    def test_英数字が1つも無ければ断る(self) -> None:
        with self.assertRaises(lot_search.BadRequest) as caught:
            self.find("-・-")
        self.assertIn("英数字", str(caught.exception))

    # -- 期間 -------------------------------------------------------------
    def test_期間は報告日で両端を含む(self) -> None:
        save(self.repo, day="2026年8月31日", lots=("N710001",))
        save(self.repo, day="2026年9月1日", lots=("N710002",))
        save(self.repo, day="2026年10月31日", lots=("N710003",))
        save(self.repo, day="2026年11月1日", lots=("N710004",))
        self.assertEqual(self.lots(self.find("N71")), ["N710002", "N710003"])

    def test_期間が逆_空_形違いは断る(self) -> None:
        cases = (({"start": "2026-10-31", "end": "2026-09-01"}, "開始日は終了日以前", 422),
                 ({"start": "", "end": "2026-09-01"}, "開始日と終了日を入れて", 400),
                 ({"start": "2026/09/01", "end": "2026-09-30"}, "yyyy-mm-dd", 400))
        for span, words, status in cases:
            with self.subTest(span=span):
                with self.assertRaises(lot_search.BadRequest) as caught:
                    lot_search.LotQuery.of({"lot": "N71", **span}, "L-1")
                self.assertIn(words, str(caught.exception))
                self.assertEqual(caught.exception.status, status)
                self.assertEqual(caught.exception.field, "start")

    def test_既定は直近3か月(self) -> None:
        self.assertEqual(lot_search.default_span(date(2026, 10, 13)),
                         (date(2026, 8, 1), date(2026, 10, 13)))
        # 年をまたぐ
        self.assertEqual(lot_search.default_span(date(2026, 1, 5)),
                         (date(2025, 11, 1), date(2026, 1, 5)))
        self.assertEqual(lot_search.default_span(date(2026, 3, 31))[0], date(2026, 1, 1))

    # -- ライン -----------------------------------------------------------
    def test_選んだラインだけ(self) -> None:
        save(self.repo, line="L-1", lots=("N710001",))
        save(self.repo, line="HVC", lots=("N710002",))
        self.assertEqual(self.lots(self.find("N71", line="L-1")), ["N710001"])
        self.assertEqual(self.lots(self.find("N71", line="HVC")), ["N710002"])
        found = self.find("N71", line="L-1")
        self.assertTrue(all(h["line"] == "L-1" for h in found.hits))

    # -- 並び・またぎ -----------------------------------------------------
    def test_並びは作業日_直_ページ_行(self) -> None:
        # 保存の順をばらばらに。報告日の字の並び(10月1日 < 9月30日)にもしない
        save(self.repo, day=DAY, shift="2直", lots=("N710005",))
        save(self.repo, day=DAY, shift="1直", page=2, lots=("N710004",))
        save(self.repo, day=DAY, shift="1直", page=1, lots=("X000000", "N710003"))
        save(self.repo, day="2026年9月30日", shift="3直", lots=("N710002",))
        save(self.repo, day="2026年9月30日", shift="1直", lots=("N710001",))
        found = self.find("N71")
        self.assertEqual(self.lots(found), [f"N71000{n}" for n in range(1, 6)])
        self.assertEqual([(h["shift"], h["page"], h["row_no"]) for h in found.hits],
                         [("1直", 1, 1), ("3直", 1, 1), ("1直", 1, 2), ("1直", 2, 1),
                          ("2直", 1, 1)])

    def test_直をまたいだロットは全部の行が出る(self) -> None:
        save(self.repo, shift="1直", lots=("N7131T0",))
        save(self.repo, shift="2直", worker="佐藤", lots=("N7131T0",))
        found = self.find("N7131")
        self.assertEqual([(h["shift"], h["page"], h["row_no"]) for h in found.hits],
                         [("1直", 1, 1), ("2直", 1, 1)])
        self.assertIn("2行", found.summary)
        self.assertIn("1ロット・2直", found.summary)
        # どの直・ページの何行目かが、行ごとに言える
        self.assertEqual(found.hits[1]["where"], f"{DAY} L-1 2直 1ページ 1行目")

    # -- 上限 -------------------------------------------------------------
    def test_上限を超えたら先のぶんだけ出して絞らせる(self) -> None:
        save(self.repo, lots=tuple(f"N71000{n}" for n in range(1, 6)))
        with patch.object(lot_search, "LIMIT", 3):
            found = self.find("N71")
        self.assertEqual(found.total, 5)
        self.assertTrue(found.too_many)
        self.assertEqual(self.lots(found), ["N710001", "N710002", "N710003"])
        self.assertEqual(len(found.table.rows), 3)
        self.assertIn("多すぎるので", found.warning)
        self.assertIn("絞ってください", found.warning)

    def test_上限の数(self) -> None:
        self.assertEqual(lot_search.LIMIT, 200)

    def test_当たりが無ければそう言う(self) -> None:
        save(self.repo, lots=("N7131T0",))
        found = self.find("Z9")
        self.assertEqual(found.total, 0)
        self.assertIn("Z9 で始まるロットの行は", found.warning)
        self.assertIn("ありません", found.table.empty)

    # -- 表の中身 ---------------------------------------------------------
    def test_表の列と字(self) -> None:
        save(self.repo, page=2, worker="佐藤", lots=("N7131T0",), dirty=False)
        found = self.find("N7131")
        row = dict(zip(found.table.columns, found.table.rows[0]))
        self.assertEqual(row["作業日"], DAY)
        self.assertEqual(row["直"], "1直")
        self.assertEqual(row["ページ"], "2")
        self.assertEqual(row["行"], "1")
        self.assertEqual(row["ロット№"], "N7131T0")
        self.assertEqual(row["材・調質"], "A5052-H34")
        self.assertEqual(row["厚×幅×丈"], "8.000×1528.0×3053.0")
        self.assertEqual(row["開始"], "8:05")
        self.assertEqual(row["終了"], "9:30")
        self.assertEqual(row["実績枚数"], "10")
        # 作業者は**そのページの**名前(直の1ページ目ではない)
        self.assertEqual(row["作業者"], "佐藤")
        # 送り済み。共有を読めなかったので「この端末」(読めれば「共有」)
        self.assertEqual(row["どこから"], "この端末")

    def test_打ちかけ_未送信の行も出る(self) -> None:
        """自動保存で置いた行は明細にある。共有へ渡していなければ「この端末・未送信」。"""
        save(self.repo, lots=("N7131T0",), dirty=True)
        found = self.find("N71")
        row = dict(zip(found.table.columns, found.table.rows[0]))
        self.assertEqual(row["どこから"], "この端末・未送信")

    # -- 呼び出し ---------------------------------------------------------
    def test_呼び出すは管理者だけ_行き先はラインで決まる(self) -> None:
        save(self.repo, line="L-1", lots=("N7131T0",))
        save(self.repo, line="HVC", lots=("N7131T0",))
        self.assertFalse(self.find("N71").recall_allowed)
        own = self.find("N71", editor=True)
        self.assertTrue(own.recall_allowed)
        self.assertEqual(own.recall_path, "/api/settings/recall")
        other = self.find("N71", line="HVC", editor=True)
        self.assertEqual(other.recall_path, "/api/records/backup/open")


class LotSearchCsvTests(unittest.TestCase):
    def test_ファイル名(self) -> None:
        out = csv_export.lot_search_output_path(Path("/出力"), "N71", "2026-08-01",
                                                "2026-10-13", "L-1")
        self.assertEqual(out.name, "ロットを探す_N71_2026-08-01_2026-10-13_L-1.csv")
        self.assertEqual(out.parent, Path("/出力"))


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class LotSearchWebTests(WebTestCase):
    """画面と経路: `/records` のカード・`/api/records/lot-search`(と CSV)。"""

    def setUp(self) -> None:
        super().setUp()
        from nippou import user_settings
        from nippou.services import local_backup as lb
        self.lb = lb
        self.share = self.tmp / "共有"
        self.share.mkdir()
        self.out = self.tmp / "出力"
        user_settings.save_many({config.KEY_ACCESS_DIR: str(self.share),
                                 config.KEY_REPORT_OUT_DIR: str(self.out)})
        lb.reset()
        self.addCleanup(lb.reset)
        save(self.repo(), shift="1直", lots=("N7131T0", "L715C50"))
        save(self.repo(), shift="2直", worker="佐藤", lots=("N7131T0",))

    def administrator(self) -> None:
        from nippou.services import access_rights
        patcher = patch.object(access_rights, "is_administrator", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def backup_of(self, line: str, lot: str) -> Path:
        """そのラインの PC が保存して控えへ写した、を作る(別の PC)。"""
        other = NippouRepository(connect(self.tmp / f"{line}-pc.sqlite3"))
        self.addCleanup(other.conn.close)
        save(other, line=line, lots=(lot,))
        self.lb.flush(other, force=True)
        return self.lb.path_for(line)

    def search(self, lot: str = "n71", **extra):
        return self.post("/api/records/lot-search", {**SPAN, "lot": lot, **extra})

    # -- 画面 -------------------------------------------------------------
    def test_記録を見るにカードがある(self) -> None:
        page = self.get("/records").get_data(as_text=True)
        self.assertIn('id="lot-card"', page)
        self.assertIn("<h2>ロットを探す</h2>", page)
        self.assertIn('id="lot-no"', page)
        self.assertIn('maxlength="7"', page)              # ロット欄と同じ制限
        self.assertIn('id="lot-search"', page)
        self.assertIn('id="lot-csv"', page)
        self.assertIn("出力先:", page)                   # 押す前に出る先が見える
        self.assertIn(str(self.out), page)
        # 並び: いまの直・保存した直のあと、「日付を指定して見る」(下見の先)の前
        self.assertLess(page.index('id="records-table"'), page.index('id="lot-card"'))
        self.assertLess(page.index('id="lot-card"'), page.index("<h2>日付を指定して見る</h2>"))
        # 地図にも載る
        self.assertIn('<a href="#lot-card">ロットを探す</a>', page)

    def test_既定の期間は直近3か月(self) -> None:
        import re

        page = self.get("/records").get_data(as_text=True)
        start = re.search(r'id="lot-start" type="date" value="([0-9-]+)"', page).group(1)
        end = re.search(r'id="lot-end" type="date" value="([0-9-]+)"', page).group(1)
        self.assertEqual(lot_search.default_span(date.fromisoformat(end))[0].isoformat(), start)
        self.assertTrue(start.endswith("-01"))

    def test_一般の端末にもラインの欄が出る_既定はこの端末のライン(self) -> None:
        page = self.get("/records").get_data(as_text=True)
        self.assertIn('id="lot-line"', page)
        at = page.index('id="lot-line"')
        select = page[at:page.index("</select>", at)]
        self.assertIn('<option value="L-1" selected>', select)
        self.assertIn('<option value="HVC"', select)          # 控えが無くても定義のライン
        self.assertIn('<option value="*"', select)
        self.assertIn("全ライン", select)

    def test_Administratorにはラインの欄が出る(self) -> None:
        self.administrator()
        self.backup_of("HVC", "N719999")
        page = self.get("/records").get_data(as_text=True)
        self.assertIn('id="lot-line"', page)
        at = page.index('id="lot-line"')
        self.assertIn('<option value="L-1" selected>', page[at:at + 600])
        self.assertIn('<option value="HVC"', page[at:at + 600])

    # -- 経路 -------------------------------------------------------------
    def test_探す(self) -> None:
        res = self.search()
        self.assertEqual(res.status_code, 200, res.get_json())
        body = res.get_json()
        self.assertEqual(body["lot"], "N71")
        self.assertEqual(body["line"], "L-1")
        self.assertEqual([(h["shift"], h["row_no"]) for h in body["hits"]],
                         [("1直", 1), ("2直", 1)])
        self.assertEqual(body["table"]["count"], 2)
        self.assertEqual(len(body["table"]["rows"]), len(body["hits"]))
        self.assertFalse(body["too_many"])
        self.assertFalse(body["recall"]["allowed"])          # 管理者モードではない

    def test_断りは欄つきで返す(self) -> None:
        res = self.search("")
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["field"], "lot")
        res = self.search("N71", start="2026-10-31", end="2026-09-01")
        self.assertEqual(res.status_code, 422)
        self.assertIn("開始日は終了日以前", res.get_json()["error"]["message"])
        self.assertEqual(self.search("N71", start="あした").status_code, 400)

    def test_当たった行から紙と下見が開ける(self) -> None:
        for hit in self.search().get_json()["hits"]:
            with self.subTest(hit=hit["where"]):
                key = {k: hit[k] for k in ("report_date", "line", "shift", "page")}
                loaded = self.post("/api/print/load", key).get_json()
                self.assertTrue(loaded["found"], loaded)
                self.assertIn(hit["row_no"], [r["row_no"] for r in loaded["rows"]])
                paper = self.get("/report/nippou", query_string=key)
                self.assertEqual(paper.status_code, 200)
                self.assertIn("N7131T0", paper.get_data(as_text=True))

    def test_管理者は当たった行を呼び出せる(self) -> None:
        from nippou import work_context

        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        body = self.search().get_json()
        self.assertTrue(body["recall"]["allowed"])
        hit = body["hits"][1]
        res = self.post(body["recall"]["path"], {k: hit[k] for k in (
            "report_date", "line", "shift", "page")})
        self.assertEqual(res.status_code, 200, res.get_json())
        ctx = work_context.get_context()
        self.assertTrue(ctx.recall.active)
        self.assertEqual((ctx.recall.shift, ctx.recall.page), ("2直", 1))

    def test_一般の端末もほかのラインを選べる_共有が無ければ控えは読まずそう言う(self) -> None:
        """見るのは誰でも。ただし**ほかのラインの控え**は今の決まりのまま(管理者だけ)。"""
        self.backup_of("HVC", "N719999")
        body = self.search(line="HVC").get_json()
        self.assertEqual(body["line"], "HVC")
        self.assertNotIn("N719999", str(body["hits"]))
        self.assertEqual(body["shared"], "missing")
        self.assertIn("がまだ無いので、この端末の分だけ探しました", body["notice"])
        self.assertIn("Administrator の PC か管理者モードのときだけ", body["notice"])

    def test_欄に無いラインは断る(self) -> None:
        res = self.search(line="コイル")
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["field"], "line")

    def test_Administratorは選んだラインだけ_控えには書かない(self) -> None:
        self.administrator()
        path = self.backup_of("HVC", "N719999")
        before = path.read_bytes()
        body = self.search(line="HVC").get_json()
        self.assertEqual(body["line"], "HVC", body)
        self.assertEqual([h["lot"] for h in body["hits"]], ["N719999"])
        self.assertEqual(body["recall"]["path"], "/api/records/backup/open")
        self.assertEqual(path.read_bytes(), before)
        # 控えの紙も、当たった鍵で開ける(Administrator は控えを読める)
        hit = body["hits"][0]
        loaded = self.post("/api/print/load", {k: hit[k] for k in (
            "report_date", "line", "shift", "page")}).get_json()
        self.assertTrue(loaded["found"] and loaded["from_backup"], loaded)
        # この端末のラインへ戻せば、この端末の行だけ
        back = self.search(line="L-1").get_json()
        self.assertNotIn("N719999", str(back["hits"]))

    def test_控えの無いラインはそう言う_探すのはやめない(self) -> None:
        self.administrator()
        res = self.search(line="AIM")
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertIn("控えが無い・開けないライン: AIM", res.get_json()["notice"])

    # -- CSV --------------------------------------------------------------
    def read_csv(self, path: Path) -> list[list[str]]:
        raw = path.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "BOM付きUTF-8(Excel がそのまま開ける)")
        with open(path, encoding="utf-8-sig", newline="") as f:
            return list(csv.reader(f))

    def test_CSVは出力先のフォルダへ_画面の表そのまま(self) -> None:
        shown = self.search().get_json()
        res = self.post("/api/records/lot-search/csv", {**SPAN, "lot": "n71"})
        self.assertEqual(res.status_code, 200, res.get_json())
        body = res.get_json()
        out = Path(body["file"])
        self.assertEqual(out.parent, self.out)
        self.assertEqual(out.name, "ロットを探す_N71_2026-09-01_2026-10-31_L-1.csv")
        self.assertIn(str(out), body["message"])
        self.assertIn("2行", body["message"])
        rows = self.read_csv(out)
        self.assertEqual(rows[0], shown["table"]["columns"])
        self.assertEqual(rows[1:], shown["table"]["rows"])
        self.assertEqual(rows[0][:5], ["作業日", "ライン", "直", "ページ", "行"])
        self.assertEqual([r[2] for r in rows[1:]], ["1直", "2直"])
        self.assertEqual([r[13] for r in rows[1:]], ["山田", "佐藤"])     # 作業者

    def test_CSVはダウンロードにしない(self) -> None:
        res = self.post("/api/records/lot-search/csv", {**SPAN, "lot": "N71"})
        self.assertEqual(res.mimetype, "application/json")
        self.assertNotIn("Content-Disposition", res.headers)

    def test_当たりが無ければCSVを書かない(self) -> None:
        body = self.post("/api/records/lot-search/csv", {**SPAN, "lot": "Z9"}).get_json()
        self.assertEqual(body["file"], "")
        self.assertIn("書きませんでした", body["message"])
        self.assertFalse(self.out.exists() and any(self.out.iterdir()))

    def test_CSVも断りは同じ(self) -> None:
        res = self.post("/api/records/lot-search/csv", {**SPAN, "lot": ""})
        self.assertEqual(res.status_code, 422)

    def test_多すぎるときはCSVも画面と同じ先のぶん(self) -> None:
        from nippou.presenters import lot_search as live

        with patch.object(live, "LIMIT", 1):
            body = self.post("/api/records/lot-search/csv", {**SPAN, "lot": "N71"}).get_json()
        self.assertIn("当たった 2行のうち", body["message"])
        self.assertEqual(len(self.read_csv(Path(body["file"]))), 2)     # 見出し + 1行

    def test_AdministratorのCSVは選んだライン(self) -> None:
        self.administrator()
        self.backup_of("HVC", "N719999")
        body = self.post("/api/records/lot-search/csv",
                         {**SPAN, "lot": "N71", "line": "HVC"}).get_json()
        self.assertTrue(body["file"].endswith("_HVC.csv"), body)
        rows = self.read_csv(Path(body["file"]))
        self.assertEqual([r[5] for r in rows[1:]], ["N719999"])

    def test_ほかのラインの控えをCSVにしても控えには書かない(self) -> None:
        self.administrator()
        path = self.backup_of("HVC", "N719999")
        before = path.read_bytes()
        self.post("/api/records/lot-search/csv", {**SPAN, "lot": "N71", "line": "HVC"})
        self.assertEqual(path.read_bytes(), before)
        conn = sqlite3.connect(str(path))
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM packing_report").fetchone()[0], 0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
