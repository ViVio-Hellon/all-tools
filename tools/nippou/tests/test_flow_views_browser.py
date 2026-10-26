"""日報の一連動作 ── **入力画面以外の画面の流れ**を本物のブラウザで通す (2) 見る・計算する側

    日報周りの一連動作チェックお願いします

設定・管理者の側は `test_flow_screens_browser`。ここは保存された日報を**見る**画面と、
日報の脇で使う計算の画面を、左の並びで移りながら通します:

    3. 記録を見る(一覧・直すところのある直は赤・並べ替え・ロットを探す・CSV・紙)
       → 集計管理(期間・4本のCSV)→ 集計・グラフ(期間・CSV)
    4. GW計算(資材マスタ・記録の形式を JSON Lines に・計算・印刷・計算の記録)
       → VC長さ計算の早見表(マスタを直すと付いていく)→ 標準作業時間(作業者名で見る・梱包力)
    5. マスタ管理で表を直す(作業者・停止理由・ライン毎目標)→ **直したものが日報入力と
       グラフに効く**

画面の誤り・console の error・「操作を読み込めませんでした」の札が出ないことも見ます。
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._flow_browser import ORIGIN, FlowCase  # noqa: E402

#: 直すところの出ない1行(7:00〜15:00、休憩60分)
CLEAN = dict(ken="10", mai="5", tut="1", kz="07", kh="00", sz="15", sh="00", s="0", th="60")
#: 休憩の入っていない1行(「0分しか休憩の入力がありません」)
NO_BREAK = dict(ken="10", mai="5", tut="1", kz="07", kh="00", sz="15", sh="00")


class ViewsCase(FlowCase):
    def save_shift(self, day: str, lot: str, values: dict, *, line: str = "L-1") -> None:
        """保存して共有へ渡した直(集計も作る)。"""
        from nippou.db.models import DetailRecord, HeaderRecord
        from nippou.services import summary

        repo = self.repo()
        key = dict(report_date=day, line=line, shift="1直", page=1)
        repo.save(HeaderRecord(**key, worker="前の人"),
                  [DetailRecord(**key, row_no=1, lot=lot, **values)])
        repo.mark_synced((day, line, "1直", 1))
        summary.refresh_shift(repo, day, line, "1直", labels={})

    def reports(self) -> Path:
        from nippou.config import SETTINGS
        return Path(SETTINGS.report_output_dir)

    @staticmethod
    def column(page, table: str, index: int) -> list[str]:
        return page.eval_on_selector_all(
            f"{table} tbody tr", f"(rows) => rows.filter((r) => r.cells.length > 1)"
                                 f".map((r) => r.cells[{index}].textContent.trim())")


# ======================================================================
# 3. 記録を見る → 集計管理 → 集計・グラフ
# ======================================================================
class RecordsAggGraphFlowTests(ViewsCase):
    def setUp(self) -> None:
        super().setUp()
        self.save_shift("2026年10月20日", "N71", CLEAN)
        self.save_shift("2026年10月21日", "N72", NO_BREAK)
        self.save_shift("2026年10月22日", "N73", CLEAN)
        self.settle_review()

    def test_記録を見る_直すところのある直は赤く_並べ替え_ロットを探してCSVと紙(self) -> None:
        page = self.open_screen("/records", "#records-table")
        self.assertIn("1直 あります", page.inner_text("#records-bad"))
        red = page.eval_on_selector_all("#records-table tbody tr.row-bad",
                                        "(rows) => rows.map((r) => r.cells[0].textContent.trim())")
        self.assertEqual(red, ["2026年10月21日"])
        self.assertIn("休憩", page.text_content("#records-table tbody tr.row-bad"), "直すところの中身が出ません")

        # 「確認」の見出しで赤い直が上へ。報告日は日の順
        page.click('#records-table th:has-text("確認") button')
        self.assertEqual(self.column(page, "#records-table", 0)[0], "2026年10月21日")
        page.click('#records-table th[data-sort="date"] button')
        self.assertEqual(self.column(page, "#records-table", 0),
                         ["2026年10月20日", "2026年10月21日", "2026年10月22日"])

        # ロットを探す(小文字・Enter)→ CSV
        page.fill("#lot-no", "n72")
        page.press("#lot-no", "Enter")
        page.wait_for_function("document.getElementById('lot-rows').textContent.includes('N72')")
        lots = self.column(page, "#lot-table", 5)
        self.assertEqual(lots, ["N72"], lots)
        page.click("#lot-csv")
        page.wait_for_function("document.getElementById('lot-note').textContent.includes('.csv')")
        found = list(self.reports().glob("ロットを探す_N72_*.csv"))
        self.assertEqual(len(found), 1, list(self.reports().iterdir()))
        self.assertIn("N72", found[0].read_text(encoding="utf-8-sig"))

        # 紙を見る(誰でも押せる): その直の全ページの紙が別の窓で開く
        with page.expect_popup() as popup:
            page.click('#records-table tbody tr.row-bad [data-open-all]')
        paper = popup.value
        paper.wait_for_load_state()
        self.assertTrue(paper.url.split(ORIGIN, 1)[1].startswith("/report/nippou?"), paper.url)
        self.assertIn("page=all", paper.url)
        self.assertIn("2026年10月21日", paper.title())
        self.assert_clean(page)

    def test_集計管理で期間を変えて4本のCSV_グラフでも期間を変えてCSV(self) -> None:
        page = self.open_screen("/agg", "#tables")
        page.fill("#start", "2026-10-20")
        page.fill("#end", "2026-10-21")
        page.click("#reload")
        page.wait_for_function("document.getElementById('agg-lead').textContent.includes('2026-10-20 〜 2026-10-21')")
        self.assertEqual(self.column(page, '[data-table="days"]', 0), ["2026年10月20日", "2026年10月21日"])
        self.assertEqual(self.column(page, '[data-table="shifts"]', 0), ["2026年10月20日", "2026年10月21日"])
        page.click("#csv")
        page.wait_for_selector("#agg-note:not([hidden])")
        csvs = sorted(p.name for p in self.reports().glob("集計_*_2026-10-20_2026-10-21_L-1.csv"))
        self.assertEqual(len(csvs), 4, csvs)
        note = page.inner_text("#agg-note")
        for name in csvs:
            self.assertIn(name, note, "出したファイルの道が画面に出ていません")

        self.go_rail(page, "/graph", "#tiles")
        page.select_option("#mode", "period")
        page.fill("#start", "2026-10-20")
        page.fill("#end", "2026-10-22")
        page.click("#reload")
        page.wait_for_function("document.getElementById('graph-lead').textContent.includes('2026')")
        page.click("#csv")
        page.wait_for_selector("#csv-note:not([hidden])")
        self.assertIn("3日ぶん", page.inner_text("#csv-note"))
        self.assertTrue(list((self.reports() / "L-1" / "集計").rglob("*.csv")),
                        "集計CSVがラインのフォルダに出ていません")
        self.assert_clean(page)


# ======================================================================
# 4. GW計算 → VC長さ計算 → 標準作業時間
# ======================================================================
class CalcScreensFlowTests(ViewsCase):
    DIMS = {"thickness_mm": "1.99", "width_mm": "81", "length_mm": "1000", "count": "40",
            "vertical_bands": "2", "horizontal_bands": "2", "pack_height_mm": "200",
            "pallet_weight_kg": "18"}

    def test_GWは記録をJSON_Linesで残し_印刷へ_記録の画面に出る(self) -> None:
        from nippou import app_config
        from tests import _gw_master

        _gw_master.write(self.tmp / "ref")
        self.admin()
        page = self.open_screen("/settings?tab=output", "#gw-record-choices")
        page.click("[data-gw-record-format='jsonl']")
        page.wait_for_function("document.getElementById('gw-record-now').textContent.includes('JSON Lines')")

        self.go_rail(page, "/gw", "#dim-count")
        self.assertTrue(page.is_disabled("#print-gw"), "計算する前から印刷を押せます")
        for name, value in self.DIMS.items():
            page.fill(f"#dim-{name}", value)
            page.dispatch_event(f"#dim-{name}", "change")
        page.click("#calc")
        page.wait_for_selector("#gw-provenance:not([hidden])")
        self.assertIn(f"日報 {app_config.version()}", page.inner_text("#gw-provenance"))
        self.assertFalse(page.is_disabled("#print-gw"))
        with page.expect_popup() as popup:
            page.click("#print-gw")
        self.assertIn("/report/gw?", popup.value.url)

        page.click("#gw-records-link")
        page.wait_for_selector("#gw-records-table")
        kinds = self.column(page, "#gw-records-table", 1)
        self.assertIn("計算", kinds)
        self.assertIn("JSON Lines", page.inner_text("#gw-records-where"))
        folder = Path(app_config.local_dir("data")) / "GW計算記録"
        self.assertTrue(list(folder.glob("*.jsonl")), list(folder.iterdir()) if folder.exists() else folder)
        self.assertFalse(list(folder.glob("*.csv")), "JSON Lines を選んだのに CSV にも書きました")
        self.assert_clean(page)

    def test_VCの早見表はマスタを直すと付いていき_標準作業時間は作業者名と梱包力を出す(self) -> None:
        from nippou.config import SETTINGS

        (self.tmp / "ref").mkdir(parents=True, exist_ok=True)
        self.assertEqual(self.get("/api/vc/state").status_code, 200)   # 初めて使う → マスタを作る
        page = self.open_screen("/vc?tab=quick", "#vc-quick-blocks")
        page.wait_for_function("document.querySelector('#vc-quick-blocks')?.textContent.includes('肉厚')")
        before = page.inner_text("#vc-quick-blocks")
        name, thick = self.first_product()
        self.assertIn(name, before)
        self.assertIn(f"VC厚 {thick:g}", before)

        # マスタ管理で VC厚 を倍にする(この端末の道)→ 早見表を開き直すと新しい VC厚 の長さ
        self.unlock()
        res = self.post("/api/master/row/save", {"file": "vc", "table": "VC品種",
                                                  "key": self.rowid(name), "values": {"VC厚": str(thick * 2)}})
        self.assertEqual(res.status_code, 200, res.get_json())
        page = self.open_screen("/vc?tab=quick", "#vc-quick-blocks")
        page.wait_for_function(f"document.querySelector('#vc-quick-blocks')?.textContent.includes('VC厚 {thick * 2:g}')")
        self.assertNotEqual(page.inner_text("#vc-quick-blocks"), before, "マスタを直しても早見表が前のままです")
        self.assertTrue(Path(SETTINGS.vc_master_path).exists())

        # 標準作業時間: 普段は「作業者ごと」を出さない。作業者名で見ると選べる。梱包力の面
        self.go_rail(page, "/standard-time", "#standardTabs")
        options = lambda: page.eval_on_selector_all("#standard-group option", "(os) => os.filter((o) => !o.hidden && !o.disabled).map((o) => o.textContent.trim())")
        self.assertNotIn("作業者ごと", options())
        page.check("#worker-mode")
        page.wait_for_function("[...document.querySelectorAll('#standard-group option')].some((o) => o.textContent.includes('作業者ごと') && !o.hidden && !o.disabled)")
        page.click('#standardTabs [role="tab"][data-key="power"]')
        page.wait_for_selector("#panel-power:not([hidden])")
        page.wait_for_function("document.getElementById('panel-power').textContent.includes('梱包の速さ')")
        self.assert_clean(page)

    # -- VC計算マスタ ------------------------------------------------------
    def first_product(self) -> tuple[str, float]:
        from nippou.config import SETTINGS
        conn = sqlite3.connect(str(SETTINGS.vc_master_path))
        try:
            name, thick = conn.execute('SELECT "品種名", "VC厚" FROM "VC品種" ORDER BY rowid LIMIT 1').fetchone()
        finally:
            conn.close()
        return name, float(thick)

    def rowid(self, name: str) -> int:
        from nippou.config import SETTINGS
        conn = sqlite3.connect(str(SETTINGS.vc_master_path))
        try:
            return conn.execute('SELECT rowid FROM "VC品種" WHERE "品種名" = ?', (name,)).fetchone()[0]
        finally:
            conn.close()


# ======================================================================
# 5. マスタを直す → 日報入力・グラフに効く
# ======================================================================
class MasterFlowTests(ViewsCase):
    def setUp(self) -> None:
        super().setUp()
        ref = self.tmp / "ref"
        ref.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(ref / "梱包資材マスタ.sqlite3"))
        with conn:
            conn.execute('CREATE TABLE "班員名簿" ("管理番号", "苗字", "班", "名前", "読み", "担当ライン")')
            conn.execute('INSERT INTO "班員名簿" VALUES (1, "山田", "A", "山田", "やまだ", "L-1")')
            conn.execute('CREATE TABLE "ライン毎目標" ("ライン", "目標")')
            conn.execute('INSERT INTO "ライン毎目標" VALUES ("L-1", 400)')
        conn.close()
        conn = sqlite3.connect(str(ref / "伝送用ファイル.sqlite3"))
        with conn:
            for table in ("作業停止時間内訳_1", "作業停止時間内訳_2", "作業停止時間内訳_3"):
                conn.execute(f'CREATE TABLE "{table}" ("内訳", "内訳番号")')
            conn.execute('INSERT INTO "作業停止時間内訳_1" VALUES ("段取り", "1")')
        conn.close()
        self.settle_review()

    def choose(self, page, table: str) -> None:
        page.click(f'#m-list .mitem:has(.mitem__name:text-is("{table}"))')
        page.wait_for_selector(f'#m-list .mitem[aria-current="true"]:has(.mitem__name:text-is("{table}"))')
        page.wait_for_function("!document.getElementById('tbl-body').textContent.includes('読み込み中')")

    def add_row(self, page, values: dict[str, str]) -> None:
        page.click("#tbl-add")
        page.wait_for_selector("#row-modal:not([hidden])")
        for col, value in values.items():
            page.fill(f'#row-fields [data-col="{col}"]', value)
        page.click("#row-save")
        page.wait_for_selector("#row-modal", state="hidden")
        page.wait_for_function(f"document.getElementById('tbl-body').textContent.includes({list(values.values())[0]!r})")

    def picked(self, page, button: str, modal: str, ready: str) -> list[str]:
        page.click(button)
        page.wait_for_selector(f"#{modal}:not([hidden]) {ready}")
        names = page.eval_on_selector_all(f"#{modal} {ready}", "(els) => els.map((e) => e.value)")
        page.click(f'#{modal} [data-close="{modal}"]')
        page.wait_for_selector(f"#{modal}", state="hidden")
        return names

    def test_作業者と停止理由を足すと日報入力の窓に出る(self) -> None:
        page = self.open_screen("/settings?tab=master", "#m-list .mitem")
        self.choose(page, "班員名簿")
        self.assertTrue(page.is_hidden("#tbl-add"), "鍵が閉まっているのに行を足せます")
        self.assertIn("管理者パスワードが要ります", page.inner_text("#tbl-why"))
        self.unlock_bar(page, "master")
        self.settle(page)
        self.assertEqual(self.tab(page), "master")
        page.wait_for_selector("#tbl-add:not([hidden])")
        self.assertEqual(page.eval_on_selector_all('#m-list .mitem[aria-current="true"] .mitem__name',
                                                   "(els) => els.map((e) => e.textContent)"),
                         ["班員名簿"], "鍵を開けたら、選んでいた表から別の表へ移りました")
        self.add_row(page, {"苗字": "鈴木", "班": "B", "名前": "鈴木", "担当ライン": "L-1"})
        self.choose(page, "作業停止時間内訳_1")
        self.add_row(page, {"内訳": "清掃", "内訳番号": "2"})

        self.go_rail(page, "/", "#grid-body")
        self.assertEqual(sorted(self.picked(page, "#open-staff", "staff-modal", "[data-staff]")),
                         sorted(["山田", "鈴木"]), "名簿に足した人が作業者を選ぶ窓に出ません")
        self.name_worker(page)
        self.assertEqual(sorted(self.picked(page, "#open-formstop", "formstop-modal",
                                            'input[name="formstop-reason"]')),
                         ["1", "2"], "足した停止理由が全停入力の窓に出ません")
        self.assert_clean(page)

    def test_ライン毎目標をCSVで直して読み直すと_グラフに目標線が出る(self) -> None:
        from nippou.config import SETTINGS

        self.save_shift("2026年10月20日", "N71", CLEAN)
        self.admin()
        page = self.open_screen("/settings?tab=master", "#m-list .mitem")
        page.click("#tab-master-targets")
        page.wait_for_selector("#panel-master-targets:not([hidden])")
        row = lambda: page.locator("#line-targets table.list tbody tr").first.inner_text()
        self.assertIn("400", row())
        self.assertIn("マスタ", row())

        # CSV で直して「読み直す」── その場で出どころごと描き直す(CSV がマスタに勝つ)
        Path(SETTINGS.line_target_path).parent.mkdir(parents=True, exist_ok=True)
        Path(SETTINGS.line_target_path).write_text("ライン,目標\nL-1,500\n", encoding="utf-8")
        page.click("#line-target-reload")
        page.wait_for_function("document.querySelector('#line-targets table.list tbody tr')?.textContent.includes('500')")
        self.assertIn("CSV", row(), "読み直したら「出どころ」が消えました")
        self.assertIn("マスタの 400 枚に勝っています", row())
        repainted = row()
        fresh = self.open_screen("/settings?tab=master", "#m-list .mitem")
        fresh.click("#tab-master-targets")
        self.assertEqual(repainted.split(), fresh.locator("#line-targets table.list tbody tr").first.inner_text().split(),
                         "読み直したあとの行が、画面を開き直したときと違います")

        self.go_rail(page, "/graph", "#tiles")
        note = page.query_selector("#target-note")
        self.assertTrue(note is None or "目標が設定されていない" not in note.inner_text(),
                        "目標を入れたのに、グラフは目標が無いと言います")
        self.assert_clean(page)


if __name__ == "__main__":
    unittest.main()
