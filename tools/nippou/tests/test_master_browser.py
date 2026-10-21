"""設定 → マスタ → 表を見る / 直す を**本物のブラウザ**で確かめる (段階1)

前は `views/master.js` と CSS の字を探して「仕組みが入っていること」だけを見ていました
(`test_master_admin`・`test_master_list`・`test_master_edit_checks`・`test_master_add_column`・
`test_settings_tabs` の一部)。ここでは画面を開いて押します:

    - 見出しを押すと並び替え(▲ → ▼ → 元の並び)。キー(Enter)でも押せる。押せることを言う(⇅・添え書き)
    - 表の一覧は、広い画面ではそれだけで流れる(作業面を流しても一覧は残る)。狭い画面で
      表を選ぶと、中身の頭が見えるところまで送る
    - 時間用を直すと、同じ画面の「直の境界時刻」もすぐ描き直す
    - 列を足す: 確かめは1回だけ・足した列がすぐ一覧に出る
    - 行を直す窓の欄は広くとる(名前・PC名が見切れない)
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._other_screen_browser import ScreenBrowserCase  # noqa: E402


class MasterBrowserCase(ScreenBrowserCase):
    #: 伝送用ファイルに置く表(名前 → (列, 行))
    TABLES: dict[str, tuple[tuple[str, ...], list[tuple]]] = {}
    FILE = "伝送用ファイル.sqlite3"

    def setUp(self) -> None:
        super().setUp()
        ref = self.tmp / "ref"
        ref.mkdir(exist_ok=True)
        conn = sqlite3.connect(str(ref / self.FILE))
        with conn:
            for name, (columns, rows) in self.TABLES.items():
                conn.execute(f'CREATE TABLE "{name}" ({", ".join(f"{c!r}" for c in columns)})'
                             .replace("'", '"'))
                marks = ", ".join("?" for _ in columns)
                conn.executemany(f'INSERT INTO "{name}" VALUES ({marks})', rows)
        conn.close()
        self.post("/api/settings/paths", {"gw_reference_dir": str(ref), "password": "nisk"})

    def open_master(self):
        return self.open_screen("/settings?tab=master", "#m-list .mitem")

    def choose(self, page, table: str) -> None:
        mark = len(self.sent)
        page.click(f'#m-list .mitem:has(.mitem__name:text-is("{table}"))')
        self.wait_until(page, lambda: any("/api/master/browse" in p and "table=" in p
                                          for p in self.paths(mark)), f"{table} を読む要求")
        page.wait_for_selector(f'#m-list .mitem[aria-current="true"]:has(.mitem__name:text-is("{table}"))')
        page.wait_for_timeout(200)

    @staticmethod
    def column(page, index: int) -> list[str]:
        return page.eval_on_selector_all(
            "#tbl-body tr", f"(rows) => rows.map((r) => r.cells[{index}]?.textContent ?? '')")


class SortTests(MasterBrowserCase):
    TABLES = {"作業停止時間内訳_1": (("管理番号", "内訳", "内訳番号"),
                                   [(1, "休憩食事", "0"), (2, "TPM活動/清掃", "1"),
                                    (3, "段取り", "10"), (4, "故障", "2")])}

    def click_head(self, page, name: str, *, key: str = "") -> None:
        mark = len(self.sent)
        th = page.locator(f'#tbl-head th:text-is("{name}")')
        if key:
            th.focus()
            th.press(key)
        else:
            th.click()
        self.wait_until(page, lambda: any("/api/master/browse" in p for p in self.paths(mark)),
                        "並べ直す要求")
        page.wait_for_timeout(300)

    def test_見出しを押すと並び替え_3回目で元の並び_キーでも押せる(self) -> None:
        page = self.open_master()                      # 見るのは鍵なしでよい
        self.choose(page, "作業停止時間内訳_1")
        self.assertIn("見出しを押すと、その列で並び替え", page.inner_text("#tbl-sort"))
        mark = page.evaluate("""() => getComputedStyle(
            [...document.querySelectorAll('#tbl-head th')].find((t) => t.textContent === '内訳番号'),
            '::after').content""")
        self.assertIn("⇅", mark, "見出しが押せる印がありません")
        self.assertEqual(self.column(page, 2), ["0", "1", "10", "2"])         # 表の順
        self.click_head(page, "内訳番号")
        self.assertEqual(self.column(page, 2), ["0", "1", "2", "10"])         # 数として小さい順
        self.assertIn("小さい順", page.inner_text("#tbl-sort"))
        self.click_head(page, "内訳番号")
        self.assertEqual(self.column(page, 2), ["10", "2", "1", "0"])
        self.click_head(page, "内訳番号")
        self.assertEqual(self.column(page, 2), ["0", "1", "10", "2"], "3回目で元の並びに戻りません")
        self.click_head(page, "管理番号", key="Enter")
        self.assertEqual(page.get_attribute('#tbl-head th:text-is("管理番号")', "aria-sort"), "ascending",
                         "キーで押せません")
        self.assertEqual(self.errors, [])


class ListScrollTests(MasterBrowserCase):
    """表が40近くある。**一覧はそれだけで流し、作業面ごと流さない**(v3.94.0)。"""

    TABLES = {f"表{i:02d}": (("a", "b"), [(j, j) for j in range(30)]) for i in range(40)}

    def test_広い画面では一覧がそれだけで流れ_作業面を流しても残る(self) -> None:
        page = self.open_master()
        page.set_viewport_size({"width": 1400, "height": 700})
        page.wait_for_timeout(300)
        box = page.evaluate("""() => { const l = document.getElementById('m-list');
            return { scroll: l.scrollHeight, client: l.clientHeight,
                     bottom: l.getBoundingClientRect().bottom, room: innerHeight }; }""")
        self.assertGreater(box["scroll"], box["client"], "一覧が自分で流れません(作業面ごと伸びています)")
        self.assertLess(box["client"], box["room"], "一覧の高さが画面に収まりません")
        page.evaluate("document.getElementById('main').scrollTop = 100000")
        page.wait_for_timeout(200)
        at = page.evaluate("""() => ({ aside: document.querySelector('.masterwrap > aside').getBoundingClientRect().top,
                                     list: document.getElementById('m-list').getBoundingClientRect().bottom,
                                     main: document.getElementById('main').getBoundingClientRect().top,
                                     room: innerHeight })""")
        self.assertGreaterEqual(at["aside"], at["main"] - 1, f"作業面を流すと一覧が上へ消えます: {at}")
        self.assertLessEqual(at["list"], at["room"] + 1, f"一覧の下が画面の外です: {at}")

    def middle_item(self, page) -> str:
        """一覧の真ん中あたりに見えている表の名前。"""
        return page.evaluate("""() => { const l = document.getElementById('m-list');
            const mid = l.getBoundingClientRect().top + l.clientHeight / 2;
            const hit = [...l.querySelectorAll('.mitem')].find((b) => {
                const r = b.getBoundingClientRect(); return r.top <= mid && r.bottom >= mid; });
            return hit.querySelector('.mitem__name').textContent; }""")

    def test_狭い画面で一覧から表を選ぶと中身の頭が見えるところまで送る(self) -> None:
        """狭い画面では一覧の下に中身が積まれる。選んだ表の頭が画面の下に隠れたままにしない。"""
        page = self.open_master()
        page.set_viewport_size({"width": 900, "height": 440})
        page.wait_for_timeout(300)
        page.evaluate("document.getElementById('m-list').scrollIntoView({ block: 'start' })")
        page.wait_for_timeout(200)
        self.choose(page, self.middle_item(page))
        page.wait_for_timeout(800)                      # なめらかに送る
        head = page.evaluate("""() => ({ bar: document.querySelector('.masterwrap .mbar').getBoundingClientRect().top,
                                       main: document.getElementById('main').getBoundingClientRect(),
                                       room: innerHeight })""")
        self.assertGreaterEqual(head["bar"], head["main"]["top"] - 1, head)
        self.assertLessEqual(head["bar"], head["room"] - 160, f"選んだ表の頭が画面の下に隠れています: {head}")
        self.assertEqual(self.errors, [])


class ShiftTimesTests(MasterBrowserCase):
    TABLES = {"時間用": (("番号", "直", "開始", "終了", "更新日"),
                        [(1, "1", "07:00", "15:00", ""), (2, "2", "15:00", "22:50", ""),
                         (3, "3", "22:50", "07:00", "")])}

    def test_時間用を直すと同じ画面の直の境界時刻もすぐ描き直す(self) -> None:
        self.post("/api/settings/sync-shift", {})
        self.unlock()
        page = self.open_master()
        self.choose(page, "時間用")
        page.click('#tbl-body tr:has(td:text-is("07:00")) >> text=直す')
        page.wait_for_selector("#row-modal:not([hidden])")
        # 行を直す窓の欄は広くとる(90px では入れた字が見切れる)
        width = page.evaluate("document.querySelector('#row-fields [data-col=\"開始\"]').getBoundingClientRect().width")
        self.assertGreater(width, 200, "行を直す窓の欄が狭いままです")
        page.fill('#row-fields [data-col="開始"]', "06:55")
        page.click("#row-save")
        page.wait_for_selector("#row-modal", state="hidden", timeout=8000)
        page.click("#tab-master-shift")
        page.wait_for_selector("#panel-master-shift:not([hidden])")
        rows = page.eval_on_selector_all(
            "#shift-times-body tr", "(rows) => rows.map((r) => [...r.cells].map((c) => c.textContent))")
        self.assertIn(["1", "06:55", "15:00"], rows, f"直の境界時刻が古いままです: {rows}")
        self.assertEqual(self.errors, [])


class AddColumnTests(MasterBrowserCase):
    FILE = "梱包資材マスタ.sqlite3"
    TABLES = {"班員名簿": (("氏名", "班", "Code"), [("青木", "A", 1), ("木村", "B", 2)])}

    def test_列を足すは確かめを1回だけ出し_足した列がすぐ一覧に出る(self) -> None:
        self.unlock()
        page = self.open_master()
        self.choose(page, "班員名簿")
        page.click("#tbl-add-col")
        page.wait_for_selector("#col-modal:not([hidden])")
        page.fill("#col-name", "電話番号")
        page.fill("#col-initial", "未登録")
        mark, asked = len(self.sent), len(self.dialogs)
        page.click("#col-save")
        page.wait_for_selector("#col-modal", state="hidden", timeout=8000)
        self.assertEqual(len(self.dialogs) - asked, 1, self.dialogs[asked:])
        self.assertIn("足した列は消せません。よろしいですか?", self.dialogs[asked])
        self.assertIn("いまある 2行 に「未登録」を入れます", self.dialogs[asked])
        body = next(b for p, b in self.sent[mark:] if p == "/api/master/column/add")
        self.assertEqual((body["table"], body["name"], body["initial"]), ("班員名簿", "電話番号", "未登録"))
        heads = page.locator("#tbl-head th").all_text_contents()
        self.assertIn("電話番号", heads)
        self.assertEqual(self.column(page, heads.index("電話番号")), ["未登録", "未登録"])
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    import unittest

    unittest.main()
