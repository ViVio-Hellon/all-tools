"""マスタを直したら、開いている画面も**新しい値で**計算・表示するか (v4.44.3)

【利用者から】

    マスタを直しても前の中身のまま動く・表示する事がないかのチェックをしてください。
    例：資材重量 変更後 前の重量のまま計算

サーバの計算はどれも要求のたびにマスタを読み直します(GW計算の資材重量・VC重量は
`test_master_reflect`)。前の中身が残っていたのは**画面の側**でした:

- **VC長さ計算の VC厚・内径**: 品種を選んだときにマスタの値が欄に入り、「計算」は欄の値で
  出します。マスタを直しても欄は前の値のまま ──
    (a) この端末のマスタ管理で直して戻ってくると、控え(`vc:draft`)から前の VC厚 が戻り、
        何も言わずにその値で計算していた
    (b) ほかのPCが直すと、1分の見張りで「選び直すと新しい値が入ります」と出るだけだった
    (c) ほかの道具で直す(更新番号が上がらない)と、見張りが気づかなかった
  いまは、欄が**選んだときのまま**なら新しい値に入れ直し(手で直した欄は触らない)、
  押した「計算」の答えにマスタの変化が付いてきたら、入れ直した値でもう1度計算します。
- **作業者を選ぶ窓・全停入力の窓**: 一覧を画面ごとに1回だけ引いていたので、日報入力を
  開いたまま(1直のあいだずっと)ほかのPCが名簿・停止理由を直しても、窓は前の一覧のまま
  だった。いまは開くたびに引き直す。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._other_screen_browser import ORIGIN, ScreenBrowserCase  # noqa: E402

PRODUCT = "TF200/TF200B"          # 内径の決まっている品種(VC厚 0.06 / 内径 87.0)


class VcFollowsMasterTests(ScreenBrowserCase):
    def setUp(self) -> None:
        super().setUp()
        (self.tmp / "ref").mkdir(parents=True, exist_ok=True)
        self.assertEqual(self.get("/api/vc/state").status_code, 200)   # 初めて使う → 作る
        from nippou.config import SETTINGS
        self.master = Path(SETTINGS.vc_master_path)
        self.assertTrue(self.master.exists())

    # -- マスタを直す -----------------------------------------------------
    def rowid(self) -> int:
        conn = sqlite3.connect(str(self.master))
        try:
            return conn.execute('SELECT rowid FROM "VC品種" WHERE "品種名" = ?', (PRODUCT,)).fetchone()[0]
        finally:
            conn.close()

    def edit_here(self, vcatu: str) -> None:
        """(a) この端末のマスタ管理で直す(更新番号も上がる)。"""
        self.unlock()
        res = self.post("/api/master/row/save", {"file": "vc", "table": "VC品種",
                                                  "key": self.rowid(), "values": {"VC厚": vcatu}})
        self.assertEqual(res.status_code, 200, res.get_json())

    def edit_elsewhere(self, vcatu: float) -> None:
        """(c) ほかの道具で直す(このツールの道を通らない・更新番号は上がらない)。"""
        conn = sqlite3.connect(str(self.master))
        with conn:
            conn.execute('UPDATE "VC品種" SET "VC厚" = ? WHERE "品種名" = ?', (vcatu, PRODUCT))
        conn.close()

    # -- 画面 -------------------------------------------------------------
    def open_vc(self, *, clock: bool = False):
        page = self.watch(self.ctx.new_page())
        if clock:
            page.clock.install()                  # 見張りの1分を飛ばせるように
        page.goto(f"{ORIGIN}/vc", wait_until="load")
        page.wait_for_selector(f'#vc-products button[data-name="{PRODUCT}"]')
        page.wait_for_timeout(300)
        return page

    def pick(self, page) -> None:
        page.click(f'#vc-products button[data-name="{PRODUCT}"]')
        page.wait_for_function("document.getElementById('vc-vcatu').value === '0.06'", timeout=8000)
        page.fill("#vc-coatu", "5")
        page.dispatch_event("#vc-coatu", "input")

    def runs(self) -> list[dict]:
        return self.posts("/api/vc/run")

    def calc(self, page) -> dict:
        before = len(self.runs())
        page.click("#vc-run")
        self.wait_until(page, lambda: len(self.runs()) > before, "計算の要求")
        page.wait_for_timeout(600)                # 入れ直したら、もう1度計算する
        return self.runs()[-1]

    def vcatu(self, page) -> str:
        return page.eval_on_selector("#vc-vcatu", "el => el.value")

    # -- 試験 -------------------------------------------------------------
    def test_この端末のマスタ管理で直して戻ると_新しいVC厚で計算する(self) -> None:
        page = self.open_vc()
        self.pick(page)
        self.assertEqual(self.calc(page)["fields"]["vcatu"], "0.06")
        self.edit_here("0.12")
        # ほかの画面へ行って戻る(控えから欄が戻る道)
        page.click('nav.rail a[href="/gw"]')
        page.wait_for_function("location.pathname === '/gw'", timeout=8000)
        page.click('nav.rail a[href="/vc"]')
        page.wait_for_function("location.pathname === '/vc'", timeout=8000)
        page.wait_for_selector(f'#vc-products button[data-name="{PRODUCT}"]')
        page.wait_for_function("document.getElementById('vc-vcatu').value === '0.12'", timeout=8000)
        self.assertEqual(page.eval_on_selector("#vc-coatu", "el => el.value"), "5.0", "打った肉厚が消えています")
        self.assertEqual(self.calc(page)["fields"]["vcatu"], "0.12", "前の VC厚 で計算しています")
        self.assertEqual(self.errors, [])

    def test_ほかの道具で直しても_見張りが気づいて入れ直す(self) -> None:
        page = self.open_vc(clock=True)
        self.pick(page)
        self.calc(page)
        self.edit_elsewhere(0.15)                  # 更新番号は変わらない
        page.clock.fast_forward(61_000)
        page.wait_for_function("document.getElementById('vc-vcatu').value === '0.15'", timeout=8000)
        self.assertEqual(self.calc(page)["fields"]["vcatu"], "0.15")
        self.assertEqual(self.errors, [])

    def test_見張りより先に計算を押しても_前のVC厚の答えを残さない(self) -> None:
        page = self.open_vc()
        self.pick(page)
        self.edit_elsewhere(0.2)
        before = len(self.runs())
        page.click("#vc-run")
        # 1回目は前の欄のまま送る。答えに付いてきたマスタで気づいて、入れ直してもう1度
        self.wait_until(page, lambda: len(self.runs()) >= before + 2, "入れ直したあとの計算")
        self.assertEqual(self.runs()[before]["fields"]["vcatu"], "0.06")
        self.assertEqual(self.runs()[-1]["fields"]["vcatu"], "0.20")
        page.wait_for_function("document.getElementById('vc-vcatu').value === '0.20'", timeout=8000)
        self.assertFalse(page.eval_on_selector("#vc-result-state", "el => !el.hidden"),
                         "入れ直して計算し直したのに「古い結果」のまま")
        self.assertEqual(self.errors, [])

    def test_手で直したVC厚は_マスタが変わっても触らない(self) -> None:
        page = self.open_vc(clock=True)
        self.pick(page)
        page.fill("#vc-vcatu", "0.07")
        page.dispatch_event("#vc-vcatu", "input")
        self.edit_elsewhere(0.15)
        page.clock.fast_forward(61_000)
        page.wait_for_timeout(800)
        self.assertEqual(self.vcatu(page), "0.07")
        self.assertEqual(self.calc(page)["fields"]["vcatu"], "0.07")
        self.assertEqual(self.errors, [])


class GwFollowsMasterTests(ScreenBrowserCase):
    """梱包資材重量計算: 計算したあと開いたまま資材重量が直されたら、計算し直す。"""

    DIMS = {"thickness_mm": "1.99", "width_mm": "81", "length_mm": "1000", "count": "40",
            "vertical_bands": "2", "horizontal_bands": "2", "pack_height_mm": "200",
            "pallet_weight_kg": "18"}

    def setUp(self) -> None:
        super().setUp()
        from tests import _gw_master
        self.master = _gw_master.write(self.tmp / "ref")

    def open_gw(self):
        page = self.watch(self.ctx.new_page())
        page.clock.install()
        page.goto(f"{ORIGIN}/gw", wait_until="load")
        page.wait_for_selector("#dim-count")
        page.wait_for_timeout(300)
        return page

    def dunplate(self, page) -> str:
        return page.inner_text('[data-result="ダンプレート"] [data-result-value]')

    def other_pc_edits(self, mass: str) -> None:
        conn = sqlite3.connect(str(self.master))
        with conn:
            conn.execute('UPDATE "資材重量" SET "単位質量" = ? WHERE "梱包資材名" = ?', (mass, "ダンプレート"))
        conn.close()

    def calc_count(self) -> int:
        return len(self.posts("/api/gw/calculate"))

    def test_開いたまま資材重量が直されると_いまの重量で計算し直す(self) -> None:
        page = self.open_gw()
        for name, value in self.DIMS.items():
            page.fill(f"#dim-{name}", value)
            page.dispatch_event(f"#dim-{name}", "change")
        page.click("#calc")
        page.wait_for_function("!document.getElementById('print-gw').disabled", timeout=8000)
        before = self.dunplate(page)
        page.click("#calc")                       # 焦点は欄の外(押したボタン)
        page.wait_for_timeout(300)
        count = self.calc_count()
        page.clock.fast_forward(61_000)           # 直していなければ計算し直さない
        page.wait_for_timeout(500)
        self.assertEqual(self.calc_count(), count, "マスタが変わっていないのに計算し直しています")
        self.other_pc_edits("0.5")                 # 0.250 → 0.5
        page.clock.fast_forward(61_000)
        self.wait_until(page, lambda: self.calc_count() > count, "計算し直し")
        page.wait_for_function(
            f"document.querySelector('[data-result=\"ダンプレート\"] [data-result-value]').textContent !== {before!r}",
            timeout=8000)
        self.assertAlmostEqual(float(self.dunplate(page)), float(before) * 2, delta=0.02)
        self.assertEqual(self.errors, [])

    def test_欄で打っている最中は計算し直さず_押し直してと出す(self) -> None:
        page = self.open_gw()
        for name, value in self.DIMS.items():
            page.fill(f"#dim-{name}", value)
            page.dispatch_event(f"#dim-{name}", "change")
        page.click("#calc")
        page.wait_for_function("!document.getElementById('print-gw').disabled", timeout=8000)
        count = self.calc_count()
        page.focus("#dim-count")
        self.other_pc_edits("0.5")
        page.clock.fast_forward(61_000)
        page.wait_for_function("document.getElementById('calc-state').textContent.includes('押し直す')",
                               timeout=8000)
        self.assertEqual(self.calc_count(), count, "打っている最中に計算し直しています")
        self.assertEqual(self.errors, [])


class ModalListsFollowMasterTests(ScreenBrowserCase):
    """作業者を選ぶ窓・全停入力の窓は、**開くたびに**一覧を引き直す。"""

    def setUp(self) -> None:
        super().setUp()
        self.ref = self.tmp / "ref"
        self.ref.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.ref / "梱包資材マスタ.sqlite3"))
        with conn:
            conn.execute('CREATE TABLE "班員名簿" ("管理番号", "苗字", "班", "名前", "読み", "担当ライン")')
            conn.execute('INSERT INTO "班員名簿" VALUES (1, "山田", "A", "山田", "やまだ", "L-1")')
        conn.close()
        conn = sqlite3.connect(str(self.ref / "伝送用ファイル.sqlite3"))
        with conn:
            for table in ("作業停止時間内訳_1", "作業停止時間内訳_2", "作業停止時間内訳_3"):
                conn.execute(f'CREATE TABLE "{table}" ("内訳", "内訳番号")')
            conn.execute('INSERT INTO "作業停止時間内訳_1" VALUES ("段取り", "1")')
        conn.close()

    def other_pc_adds(self) -> None:
        conn = sqlite3.connect(str(self.ref / "梱包資材マスタ.sqlite3"))
        with conn:
            conn.execute('INSERT INTO "班員名簿" VALUES (2, "鈴木", "A", "鈴木", "すずき", "L-1")')
        conn.close()
        conn = sqlite3.connect(str(self.ref / "伝送用ファイル.sqlite3"))
        with conn:
            conn.execute('INSERT INTO "作業停止時間内訳_1" VALUES ("清掃", "2")')
        conn.close()

    def open_and_close(self, page, button: str, modal: str, ready: str) -> list[str]:
        page.click(button)
        page.wait_for_selector(f"#{modal}:not([hidden]) {ready}")
        page.wait_for_timeout(300)
        names = page.eval_on_selector_all(f"#{modal} {ready}", "els => els.map(e => e.value)")
        page.click(f'#{modal} [data-close="{modal}"]')
        page.wait_for_selector(f"#{modal}", state="hidden")
        return names

    def test_開いたまま名簿と停止理由が直されても_次に開いた窓は新しい一覧(self) -> None:
        self.settle_review()
        page = self.open_entry()
        staff = self.open_and_close(page, "#open-staff", "staff-modal", "[data-staff]")
        self.assertEqual(staff, ["山田"])
        stops = self.open_and_close(page, "#open-formstop", "formstop-modal",
                                    'input[name="formstop-reason"]')
        self.assertEqual(stops, ["1"])
        self.other_pc_adds()
        self.assertEqual(sorted(self.open_and_close(page, "#open-staff", "staff-modal", "[data-staff]")),
                         sorted(["山田", "鈴木"]), "名簿を直したのに、前の一覧のままです")
        self.assertEqual(sorted(self.open_and_close(page, "#open-formstop", "formstop-modal",
                                                    'input[name="formstop-reason"]')),
                         ["1", "2"], "停止理由を直したのに、前の一覧のままです")
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    import unittest
    unittest.main()
