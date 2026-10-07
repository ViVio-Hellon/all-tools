"""VC長さ計算の画面と API・マスタ管理への組み込み(vc-calculator の移植)

    ViVio-Hellon/vc-calculator を参照して、本ツールにタブを追加して
    vc-calculator 内の機能を移植してください

vc-calculator `tests/test_web.py`(計算 API・早見表・コイル枠)を日報管理ツールの
道に写したものと、こちらで足した載せ方の試験:

- レールに「VC長さ計算」。面は 計算 / 早見表 / コイル・平板
- マスタは参照用マスタのフォルダの `VC計算マスタ.sqlite3`。**無ければ初期値で作る**
- vc-calculator の `vc_master.sqlite3` が置いてあれば**そちらを読む**(共有できる)
- 直すのは 設定・管理者 → マスタ管理(同じ鍵)。書いたら変更履歴と更新番号
"""
from __future__ import annotations

import re
import sqlite3
import sys
import unittest
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HEADERS, WebTestCase  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"
NITTO = "2008系/2001SR/310GH5"


class VcCase(WebTestCase):
    def setUp(self) -> None:
        super().setUp()
        (self.tmp / "ref").mkdir(parents=True, exist_ok=True)

    def unlock(self) -> None:
        res = self.post("/api/master/unlock", {"enable": True, "password": "nisk"})
        self.assertEqual(res.status_code, 200, res.get_json())

    def master_path(self) -> Path:
        from nippou.config import SETTINGS
        return SETTINGS.vc_master_path


class ScreenTests(VcCase):
    def test_rail_has_vc(self) -> None:
        page = self.get("/vc").get_data(as_text=True)
        self.assertIn('href="/vc"', page)
        self.assertIn("VC長さ計算", page)
        for key in ("calc", "quick", "coil", "settings"):
            self.assertIn(f'data-panel="{key}"', page)
        # コイル・平板は**開いたときに読み込む**(data-src。3D のライブラリは大きい)
        self.assertRegex(page, r'data-src="/sv/[^"]+/vc/coil/index\.html"')

    def test_first_visit_creates_the_master_in_the_reference_folder(self) -> None:
        self.assertFalse(self.master_path().exists())
        body = self.get("/api/vc/state").get_json()
        self.assertTrue(self.master_path().exists())
        self.assertEqual(self.master_path().name, "VC計算マスタ.sqlite3")
        self.assertEqual(body["state"]["master"]["source"], "db")
        self.assertEqual(len(body["state"]["products"]), 6)

    def test_missing_master_is_not_a_problem_before_first_use(self) -> None:
        """初めて使うときに作るので、無いことを設定画面の困りごとにしない。"""
        from nippou.presenters import settings as view
        files = view.file_views()
        vc = next(f for f in files if f.key == "vc")
        self.assertFalse(vc.exists)
        self.assertFalse([t for t in view._problems(view.path_views(), files)
                          if "VC計算マスタ" in t])

    def test_no_reference_folder_falls_back_to_vba_values(self) -> None:
        """参照用マスタのフォルダに届かなくても、VBA の初期値で計算は止めない。"""
        (self.tmp / "ref").rmdir()
        body = self.get("/api/vc/state").get_json()["state"]
        self.assertEqual(body["master"]["source"], "seed")
        self.assertIn("VBA に直書きされていた初期値", body["master"]["note"])
        self.assertEqual(len(body["products"]), 6)
        res = self.post("/api/vc/run", {"fields": {"coatu": "20", "vcatu": "0.10",
                                                    "inside": "87"}})
        self.assertEqual(res.get_json()["result"]["fields"]["vclen"], "67.2")
        self.assertFalse((self.tmp / "ref").exists())       # フォルダは作らない

    def test_vc_calculator_master_is_used_as_is(self) -> None:
        """vc-calculator の `vc_master.sqlite3` を置けばそのまま読む(`OLDER_NAMES`)。"""
        from nippou.vc import db
        older = self.tmp / "ref" / "vc_master.sqlite3"
        self.assertEqual(db.ensure_database(older), "created")
        conn = sqlite3.connect(older)
        conn.execute("UPDATE VC品種 SET VC厚 = 0.11 WHERE 品種名 = 'V325系'")
        conn.commit()
        conn.close()
        self.assertEqual(self.master_path(), older)
        products = self.get("/api/vc/state").get_json()["state"]["products"]
        self.assertEqual(next(p for p in products if p["name"] == "V325系")["vcatu"], "0.11")
        self.assertFalse((self.tmp / "ref" / "VC計算マスタ.sqlite3").exists())


class CalcApiTests(VcCase):
    def test_select_choose_and_run(self) -> None:
        body = self.post("/api/vc/select", {"product": NITTO,
                                            "fields": {"coatu": "9", "prolen": "1000"}}).get_json()
        self.assertEqual(body["fields"], {"coatu": "", "vcatu": "0.10", "inside": "",
                                          "vclen": "", "prolen": "1000"})
        body = self.post("/api/vc/inside", {"product": NITTO, "inside": "87",
                                            "fields": body["fields"]}).get_json()
        self.assertEqual(body["fields"]["inside"], "87.0")
        res = self.post("/api/vc/run", {"product": NITTO,
                                        "fields": {**body["fields"], "coatu": "20"}})
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(body["result"]["fields"]["vclen"], "67.2")
        self.assertEqual([c["value"] for c in body["result"]["counts"]], ["33.4", "26.8", "21.9"])
        self.assertEqual([c["label"] for c in body["result"]["counts"]], ["1×2", "2×4", "5×10"])
        self.assertEqual(body["result"]["mk"], "67.2")
        self.assertTrue(body["result"]["steps"])
        self.assertAlmostEqual(body["result"]["geometry"]["outer"], 127.0)

    def test_choice_not_in_master(self) -> None:
        res = self.post("/api/vc/inside", {"product": "TF200/TF200B", "inside": "95"})
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"], "no_choice")

    def test_unknown_product(self) -> None:
        res = self.post("/api/vc/select", {"product": "無い品種"})
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"], "no_product")
        self.assertEqual(len(res.get_json()["state"]["products"]), 6)

    def test_reverse_is_on(self) -> None:
        """VC長さだけ入れて計算すると肉厚を逆算する(ブック「長さから肉厚」)。"""
        body = self.post("/api/vc/run", {"fields": {"vcatu": "0.10", "inside": "87",
                                                    "vclen": "67.2"}}).get_json()
        self.assertEqual(body["result"]["fields"]["coatu"], "20.0")
        self.assertEqual(body["result"]["computed"], ["coatu"])
        self.assertTrue(body["state"]["reverse"])

    def test_refusals_carry_field_errors(self) -> None:
        res = self.post("/api/vc/run", {"fields": {"coatu": "x", "vcatu": "0.1", "inside": "87"}})
        self.assertEqual(res.status_code, 422)
        self.assertIn("coatu", res.get_json()["result"]["errors"])
        res = self.post("/api/vc/run", {"fields": {"vcatu": "0.1", "inside": "87"}})
        self.assertEqual((res.status_code, res.get_json()["error"]["code"]), (422, "missing"))
        self.assertIn("肉厚かVC長さ", res.get_json()["error"]["message"])

    def test_bad_fields_shape(self) -> None:
        res = self.post("/api/vc/run", {"fields": ["x"]})
        self.assertEqual(res.status_code, 400)

    def test_quick(self) -> None:
        body = self.get("/api/vc/quick").get_json()
        self.assertEqual(len(body["quick"]["blocks"]), 7)
        self.assertEqual(body["master"]["source"], "db")
        self.assertFalse(body["grid"]["can_edit"])
        self.assertEqual(len(body["grid"]["blocks"]), 7)

    def test_calc_is_not_tab_guarded(self) -> None:
        """計算は打つ画面ではないので、2枚目のタブからも使える。"""
        headers = {**HEADERS, "X-Tab": "B"}
        self.client.post("/api/tab/claim", headers={**HEADERS, "X-Tab": "A"}, json={})
        self.client.post("/api/tab/claim", headers=headers, json={})
        res = self.client.post("/api/vc/run", headers=headers,
                               json={"fields": {"coatu": "20", "vcatu": "0.1", "inside": "87"}})
        self.assertEqual(res.status_code, 200)


class QuickGridApiTests(VcCase):
    def test_needs_the_admin_password(self) -> None:
        body = {"block": "VE系", "insides": "98", "thicknesses": "5, 999"}
        res = self.post("/api/vc/quick-grid", body)
        self.assertEqual((res.status_code, res.get_json()["error"]["code"]), (403, "need_password"))
        res = self.post("/api/vc/quick-grid", {**body, "password": "nisk"})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertIn("1 マス足しました", res.get_json()["message"])
        ve = next(b for b in res.get_json()["grid"]["blocks"] if b["name"] == "VE系")
        self.assertIn("999", ve["thicknesses"])
        res = self.post("/api/vc/quick-grid", {**body, "thicknesses": "x", "password": "nisk"})
        self.assertEqual((res.status_code, res.get_json()["error"]["code"]), (400, "bad_value"))

    def test_admin_mode_opens_it_without_asking_again(self) -> None:
        self.unlock()
        self.assertTrue(self.get("/api/vc/quick").get_json()["grid"]["can_edit"])
        res = self.post("/api/vc/quick-grid", {"block": "R575B", "insides": "88",
                                                "thicknesses": "6"})
        self.assertEqual(res.status_code, 200, res.get_json())


class FixedBlockLengthTests(VcCase):
    """計算品種の無い枠(固定値)にも、VC厚 を決めれば長さを式で入れる(v3.96.0)。

        計算値を当ててくれればいいのでは? マスタの早見表値を編集しないとだめですか?
    """

    def test_fill_from_a_product_via_the_api(self) -> None:
        import math
        from nippou.vc.calc import vc_length
        self.unlock()
        state = self.get("/api/vc/quick").get_json()
        products = {p["name"]: p["vcatu"] for p in state["grid"]["products"]}
        self.assertEqual(products["TF200/TF200B"], "0.06")
        # 固定値の枠(R575B)に、空のマスを足してから長さを入れる
        self.post("/api/vc/quick-grid", {"block": "R575B", "insides": "88",
                                         "thicknesses": "6, 7"})
        r575 = next(b for b in self.get("/api/vc/quick").get_json()["grid"]["blocks"]
                    if b["name"] == "R575B")
        self.assertEqual(r575["empty"], 2)
        res = self.post("/api/vc/quick-grid", {
            "block": "R575B", "insides": "88", "thicknesses": "6, 7",
            "length": "product", "product": "TF200/TF200B"})
        body = res.get_json()
        self.assertEqual(res.status_code, 200, body)
        self.assertIn("長さが空だった 2 マスに長さを入れました", body["message"])
        block = next(b for b in body["quick"]["blocks"] if b["name"] == "R575B")
        heads = block["headers"]
        row = block["rows"][0]["cells"]
        self.assertEqual(row[heads.index("6")], str(math.floor(vc_length(6, 0.06, 88))))
        self.assertEqual(row[heads.index("5")], "26")        # 紙の数はそのまま

    def test_bad_vcatu_is_a_400(self) -> None:
        self.unlock()
        res = self.post("/api/vc/quick-grid", {"block": "R575B", "insides": "88",
                                               "thicknesses": "6", "length": "value",
                                               "vcatu": "10"})
        self.assertEqual((res.status_code, res.get_json()["error"]["code"]), (400, "bad_value"))

    def test_screen_offers_the_choice(self) -> None:
        page = self.get("/vc").get_data(as_text=True)
        for id_ in ("vc-grid-length", "vc-grid-vcatu", "vc-grid-fill", "vc-grid-overwrite"):
            self.assertIn(f'id="{id_}"', page)
        js = (STATIC / "js" / "views" / "vc.js").read_text(encoding="utf-8")
        self.assertIn('fill_empty: $("vc-grid-fill").checked', js)
        self.assertIn("(空ならこのまま)", js)


class MasterAdminTests(VcCase):
    """VC計算マスタを 設定・管理者 → マスタ管理 で直す。"""

    def browse(self, table: str) -> dict:
        self.get("/api/vc/state")                        # 初めて使う → 作る
        return self.get(f"/api/master/browse?file=vc&table={quote(table)}").get_json()

    def row(self, table: str, **match) -> dict:
        rows = self.browse(table)["page"]["rows"]
        return next(r for r in rows if all(r[k] == v for k, v in match.items()))

    def save(self, table: str, key, values: dict):
        return self.post("/api/master/row/save",
                         {"file": "vc", "table": table, "key": key, "values": values})

    def test_listed_as_an_editable_file(self) -> None:
        body = self.browse("VC品種")
        group = next(g for g in body["catalog"] if g["file"] == "vc")
        self.assertTrue(group["editable"])
        self.assertEqual(group["label"], "VC計算マスタ")
        tables = [t["table"] for t in group["tables"]]
        self.assertIn("VC品種", tables)
        self.assertNotIn("_メタ", tables)                # 内部の値は出さない
        self.assertIn("VC長さ計算", next(t["note"] for t in group["tables"]
                                        if t["table"] == "VC品種"))

    def test_edit_reaches_the_calculator_with_history(self) -> None:
        self.unlock()
        before = self.get("/api/vc/state").get_json()["state"]["master"]["revision"]
        row = self.row("VC品種", 品種名="V325系")
        res = self.save("VC品種", row["__行"], {"VC厚": "0.2"})
        self.assertEqual(res.status_code, 200, res.get_json())
        state = self.get("/api/vc/state").get_json()["state"]
        self.assertEqual(next(p for p in state["products"] if p["name"] == "V325系")["vcatu"],
                         "0.20")
        self.assertEqual(state["master"]["revision"], before + 1)   # vc-calculator にも効く
        history = self.browse("変更履歴")["page"]
        self.assertFalse(history["editable"])
        self.assertIn("直せません", history["why"])
        self.assertEqual([(r["表"], r["操作"]) for r in history["rows"]], [("VC品種", "更新")])
        self.assertIn('"VC厚": 0.13', history["rows"][0]["変更前"])

    def test_needs_the_key(self) -> None:
        row = self.row("VC品種", 品種名="V325系")
        res = self.save("VC品種", row["__行"], {"VC厚": "0.2"})
        self.assertEqual(res.status_code, 403)

    def test_table_rules(self) -> None:
        self.unlock()
        v325 = self.row("VC品種", 品種名="V325系")
        res = self.save("VC品種", v325["__行"], {"VC厚": "0"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("値の範囲", res.get_json()["error"]["message"])
        nitto = self.row("VC品種", 品種名=NITTO)
        res = self.post("/api/master/row/delete", {"file": "vc", "table": "VC品種",
                                                    "key": nitto["__行"]})
        self.assertEqual(res.status_code, 400)
        self.assertIn("使われているので消せません", res.get_json()["error"]["message"])
        res = self.post("/api/master/row/add", {"file": "vc", "table": "VC内径選択肢",
                                                 "values": {"品種名": "無い品種", "表示順": "1",
                                                            "表示名": "特", "内径": "90"}})
        self.assertEqual(res.status_code, 400)
        self.assertIn("親の表に無い品種名", res.get_json()["error"]["message"])

    def test_rename_follows_to_children(self) -> None:
        self.unlock()
        nitto = self.row("VC品種", 品種名=NITTO)
        res = self.save("VC品種", nitto["__行"], {"品種名": "2008系"})
        self.assertEqual(res.status_code, 200, res.get_json())
        names = {r["品種名"] for r in self.browse("VC内径選択肢")["page"]["rows"]}
        self.assertIn("2008系", names)
        self.assertNotIn(NITTO, names)
        blocks = {r["計算品種"] for r in self.browse("早見表ブロック")["page"]["rows"]}
        self.assertIn("2008系", blocks)

    def test_settings_rows_are_fixed_and_checked(self) -> None:
        self.unlock()
        page = self.browse("アプリ設定")["page"]
        self.assertTrue(page["editable"])
        self.assertFalse(page["can_add"])
        rounding = self.row("アプリ設定", キー="早見表の丸め")
        res = self.save("アプリ設定", rounding["__行"], {"値": "切り上げ"})
        self.assertEqual(res.status_code, 400)
        res = self.save("アプリ設定", rounding["__行"], {"キー": "別の名前"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("キーは変えられません", res.get_json()["error"]["message"])
        res = self.save("アプリ設定", rounding["__行"], {"値": "四捨五入"})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(self.get("/api/vc/quick").get_json()["quick"]["rounding"], "四捨五入")
        res = self.post("/api/master/row/delete", {"file": "vc", "table": "アプリ設定",
                                                    "key": rounding["__行"]})
        self.assertEqual(res.status_code, 422)

    def test_tone_is_one_of_the_paper_colors(self) -> None:
        self.unlock()
        ve = self.row("早見表ブロック", 品種名="VE系")
        res = self.save("早見表ブロック", ve["__行"], {"枠色": "pink"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("枠色", res.get_json()["error"]["message"])

    def test_hidden_meta_table_cannot_be_opened_or_written(self) -> None:
        self.unlock()
        body = self.browse("_メタ")
        self.assertIn("ありません", body["page"]["error"])
        res = self.save("_メタ", 1, {"値": "0"})
        self.assertEqual(res.status_code, 404)

    def test_backup_is_taken_next_to_the_master(self) -> None:
        self.unlock()
        row = self.row("VC品種", 品種名="V325系")
        self.save("VC品種", row["__行"], {"業者名": "スミロン2"})
        backups = list((self.master_path().parent / "backup").glob("VC計算マスタ_*.sqlite3"))
        self.assertEqual(len(backups), 1)


class CoilTests(VcCase):
    """コイル・平板: 枠の中のツールと、同梱したライブラリ。**外へは取りに行かない。**"""

    def test_offline_and_served(self) -> None:
        coil = STATIC / "vc" / "coil"
        for f in sorted(coil.iterdir()):
            text = f.read_text(encoding="utf-8")
            urls = [u for u in re.findall(r"https?://[^\s\"')]+", text)
                    if not u.startswith("http://www.w3.org/")]
            self.assertEqual(urls, [], f.name)
        html = (coil / "index.html").read_text(encoding="utf-8")
        for src in re.findall(r'(?:src|href)="([^"]+)"', html):
            path = f"/static/vc/coil/{src}"
            res = self.client.get(path, headers=HEADERS)
            self.assertEqual(res.status_code, 200, path)
            res.close()
        for lib in ("three/three.min.js", "three/OrbitControls.js", "chartjs/chart.min.js",
                    "chartjs/chartjs-plugin-annotation.min.js",
                    "fontawesome/css/all.min.css", "fontawesome/webfonts/fa-solid-900.woff2"):
            self.assertTrue((STATIC / "vc" / "vendor" / lib).is_file(), lib)
        for license_file in ("three/LICENSE.txt", "chartjs/LICENSE-chartjs.md",
                             "fontawesome/LICENSE.txt"):
            self.assertTrue((STATIC / "vc" / "vendor" / license_file).is_file(), license_file)

    def test_fit_on_screen_wiring(self) -> None:
        """1920×1080 で「少しスクロールがいる」を縮めて収める (v4.3.0)。

        段は そのまま → 詰める → 0.75倍まで縮める → ページごと動かす。
        実際に収まるかはブラウザで測っています(README の v4.3.0)。
        """
        coil = STATIC / "vc" / "coil"
        html = (coil / "index.html").read_text(encoding="utf-8")
        self.assertIn('href="coil-fit.css"', html)
        # 形状の切替が済んでから、いまの中身をはかる(最後に読む)
        self.assertLess(html.index('src="shape-switch.js"'), html.index('src="coil-fit.js"'))
        js = (coil / "coil-fit.js").read_text(encoding="utf-8")
        self.assertIn("const MIN_ZOOM = 0.75;", js)
        for cls in ("fit-compact", "fit-zoom", "fit-scroll", "fit-measure"):
            self.assertIn(cls, js)
        # 後から高さが変わるもの(アイコンの字・グラフ・エラーの文)を見張る
        self.assertIn("new ResizeObserver(later)", js)
        css = (coil / "coil-fit.css").read_text(encoding="utf-8")
        self.assertIn("calc(100vh / var(--fit-zoom, 1))", css)
        # 3D の枠は固定の 500px をやめて残りいっぱい / グラフは高さを決める
        self.assertRegex(css, r"\.model-container \{\s*flex: 1 1 auto;")
        self.assertRegex(css, r"\.chart-container \{\s*position: relative;\s*height: 150px;")
        self.assertIn("@media print", css)
        # 外のページも動かさない: 枠の高さは作業面の残りちょうど
        vc = (STATIC / "js" / "views" / "vc.js").read_text(encoding="utf-8")
        self.assertIn("function fitFrame()", vc)
        self.assertIn('window.addEventListener("resize", fitFrame, { signal });', vc)

    def test_theme_light_and_dark(self) -> None:
        """背景をダーク、ライト選べるように (v4.3.0)。"""
        coil = STATIC / "vc" / "coil"
        html = (coil / "index.html").read_text(encoding="utf-8")
        head = html[:html.index("</head>")]
        # 本文を描く前に決める(白く光ってから暗くならないように)
        self.assertIn('<script src="coil-theme.js"></script>', head)
        self.assertIn('href="coil-theme.css"', head)
        for choice in ("light", "dark"):
            self.assertIn(f'data-theme-choice="{choice}"', html)
        js = (coil / "coil-theme.js").read_text(encoding="utf-8")
        self.assertIn("localStorage.setItem(KEY, theme)", js)
        # 選んでいなければライト(日報・統合ツールの4ツールとそろえる。OS には付いていかない)
        self.assertIn("root.dataset.theme = saved() || DEFAULT", js)
        self.assertIn("const DEFAULT = 'light'", js)
        self.assertIn("Chart.register(", js)
        css = (coil / "coil-theme.css").read_text(encoding="utf-8")
        # **印刷はいつも白い紙**: ダークは画面だけ
        self.assertLess(css.index("@media screen"), css.index(':root[data-theme="dark"]'))
        # 色は変数に寄せた(白・黒の書き込みが残っていない)
        for name in ("aluminum-coil-calculator.css", "plate-calculator.css"):
            text = (coil / name).read_text(encoding="utf-8")
            with self.subTest(name=name):
                self.assertNotRegex(text, r"background(-color)?:\s*white")
                self.assertNotRegex(text, r"(?<![-\w])color:\s*#333")


class WiringTests(unittest.TestCase):
    """画面の配線(ブラウザが無くても見られるもの)。"""

    def test_class_names_do_not_collide(self) -> None:
        """日報の画面にも .steps / .fields などがある。VC の部品は `vc-` で始める。"""
        js = (STATIC / "js" / "views" / "vc.js").read_text(encoding="utf-8")
        for name in re.findall(r'className = "([^"]+)"', js):
            for part in name.split():
                # アプリ共通の部品(ほかの画面と同じ見た目にするために使う)
                if part in ("btn", "lead", "corner", "num",
                            "badge", "badge--done", "badge--todo"):
                    continue
                self.assertTrue(part.startswith("vc-") or part.startswith("msg"), part)

    def test_print_is_scoped(self) -> None:
        """早見表の紙の形はほかの画面の印刷に効かせない(名前つきの頁・刷るあいだだけ)。"""
        css = (STATIC / "css" / "components.css").read_text(encoding="utf-8")
        self.assertIn("@page vcquick{", css)
        self.assertNotRegex(css, r"@page\s*\{\s*size:\s*A4 landscape")
        js = (STATIC / "js" / "views" / "vc.js").read_text(encoding="utf-8")
        self.assertIn('document.body.classList.add("vc-printing")', js)
        self.assertIn("onLeave(endPrint)", js)

    def test_colors_come_from_tokens(self) -> None:
        tokens = (STATIC / "css" / "tokens.css").read_text(encoding="utf-8")
        for name in ("--vc-roll-hl", "--vc-tone-navy", "--vc-choose-bg"):
            # 明るい画面・OS のダーク・明示のダーク の3か所
            self.assertEqual(tokens.count(f"{name}:"), 3, name)


if __name__ == "__main__":
    unittest.main()


class SettingsTabTests(VcCase):
    """設定の面(v3.94.0)

        設定タブを作ってそこに 早見表のマスをまとめて作る(管理者) を移動させて
        ください / 置き場所(パス)も追加してください / VC計算マスタ.sqlite3 と
        vc_master.sqlite3 の関係も表示しておいてください(どちらが優先)
    """

    def place(self) -> dict:
        return self.get("/api/vc/settings").get_json()["place"]

    def test_grid_maker_moved_to_the_settings_panel(self) -> None:
        page = self.get("/vc").get_data(as_text=True)
        quick = page[page.index('data-panel="quick"'):page.index('data-panel="coil"')]
        settings = page[page.index('data-panel="settings"'):]
        self.assertNotIn('id="vc-grid"', quick)
        self.assertIn('data-vc-tab="settings"', quick)      # 早見表からの案内
        self.assertIn('id="vc-grid"', settings)
        self.assertIn('id="vc-place"', settings)
        body = self.get("/api/vc/settings").get_json()
        self.assertTrue(body["grid"]["blocks"])            # マスを作る枠も読める

    def test_priority_is_shown_in_order(self) -> None:
        self.get("/api/vc/state")                           # 初めて使う → 1 を作る
        place = self.place()
        self.assertEqual([n["name"] for n in place["names"]],
                         ["VC計算マスタ.sqlite3", "vc_master.sqlite3"])
        self.assertTrue(place["names"][0]["used"])
        self.assertFalse(place["names"][1]["exists"])
        self.assertEqual(place["level"], "ok")

    def test_neither_exists_yet(self) -> None:
        place = self.place()
        # /api/vc/settings も先に作るので、読む前の状態は presenter で見る
        from nippou.presenters import vc as view
        for f in (self.tmp / "ref").glob("*.sqlite3"):
            f.unlink()
        place = view.place_view()
        self.assertEqual(place["level"], "info")
        self.assertIn("VC計算マスタ.sqlite3 を VBA の初期値で作ります", place["status"])

    def test_both_exist_second_is_not_read(self) -> None:
        from nippou.vc import db
        self.get("/api/vc/state")
        db.ensure_database(self.tmp / "ref" / "vc_master.sqlite3")
        place = self.place()
        self.assertEqual(place["level"], "warn")
        self.assertIn("vc_master.sqlite3 もありますが、読んでいません", place["status"])
        self.assertTrue(place["names"][1]["exists"])
        self.assertFalse(place["names"][1]["used"])

    def test_folder_can_point_at_vc_calculator(self) -> None:
        """置き場所を vc-calculator のフォルダへ。**鍵が要る**(ほかのパスと同じ)。"""
        from nippou.config import SETTINGS
        from nippou.vc import db
        other = self.tmp / "vccalc"
        other.mkdir()
        db.ensure_database(other / "vc_master.sqlite3")
        res = self.post("/api/settings/paths", {"vc_master_dir": str(other)})
        self.assertEqual(res.status_code, 403)
        res = self.post("/api/settings/paths",
                        {"vc_master_dir": str(other), "password": "nisk"})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(SETTINGS.vc_master_path, other / "vc_master.sqlite3")
        place = self.place()
        self.assertEqual(place["used"], str(other / "vc_master.sqlite3"))
        self.assertTrue(place["names"][1]["used"])
        self.assertEqual(self.get("/api/vc/state").get_json()["state"]["master"]["source"], "db")

    def test_saving_the_folder_retries_at_once(self) -> None:
        """読めなかった直後は30秒読みに行かない。**置き場所を直したら待たせない。**"""
        from nippou.vc import db
        missing = self.tmp / "まだ無い"
        self.post("/api/settings/paths", {"vc_master_dir": str(missing), "password": "nisk"})
        self.assertEqual(self.get("/api/vc/state").get_json()["state"]["master"]["source"], "seed")
        good = self.tmp / "vccalc"
        good.mkdir()
        db.ensure_database(good / "vc_master.sqlite3")
        self.post("/api/settings/paths", {"vc_master_dir": str(good), "password": "nisk"})
        self.assertEqual(self.get("/api/vc/state").get_json()["state"]["master"]["source"], "db")

    def test_listed_in_reference_settings_and_distribution(self) -> None:
        from nippou import config, distribution
        from nippou.presenters import settings as view
        read = next(g for g in view.PATH_GROUPS if g[0] == "read")
        self.assertIn(config.KEY_VC_MASTER_DIR, read[2])
        self.assertIn(config.KEY_VC_MASTER_DIR, distribution.ITEM_KEYS)
        html = self.get("/settings").get_data(as_text=True)
        self.assertIn(f'id="path-{config.KEY_VC_MASTER_DIR}"', html)
        self.assertIn("VC計算マスタの置き場所", html)
        # マスタ管理のファイルの状態でも、どの置き場所から来たかを出す
        vc = next(f for f in view.file_views() if f.key == "vc")
        self.assertEqual(vc.source_label, "VC計算マスタの置き場所")

    def test_tab_select_is_not_shadowed(self) -> None:
        """vc.js には品種を選ぶ `select` がある。面を替える `select` と取り違えない。"""
        js = (STATIC / "js" / "views" / "vc.js").read_text(encoding="utf-8")
        self.assertIn("select as selectTab", js)
        self.assertIn("selectTab(root, link.dataset.vcTab)", js)


class QuickBlockApiTests(VcCase):
    """早見表の品種を1回で足す・消す・切り替える口(v3.97.0)。

        1行消すがないと追加したVCが永遠に消せない(早見表を先に消せ)
        早見表に追加でVCを足すのもわかりにくい
        固定値(式なし)に知らない間になっていたがそれもよくわからない
    """

    ADD = {"new_product": "畳論マン", "new_vcatu": "0.1", "insides": "88",
           "thicknesses": "5, 20, 30", "length": "formula"}

    def blocks(self, body: dict) -> dict[str, dict]:
        return {b["name"]: b for b in body["grid"]["blocks"]}

    def test_every_write_needs_the_key(self) -> None:
        for url, body in (("/api/vc/quick-block", self.ADD),
                          ("/api/vc/quick-block/delete", {"block": "R575B"}),
                          ("/api/vc/quick-block/source", {"block": "R575B", "product": "V325系"}),
                          ("/api/vc/quick-cells/delete", {"block": "R575B", "insides": "88"}),
                          ("/api/vc/product/delete", {"product": "V325系"})):
            with self.subTest(url=url):
                res = self.post(url, body)
                self.assertEqual((res.status_code, res.get_json()["error"]["code"]),
                                 (403, "need_password"))
        # 断ったので何も変わっていない
        blocks = self.blocks(self.get("/api/vc/settings").get_json())
        self.assertIn("R575B", blocks)
        self.assertEqual(blocks["R575B"]["product"], "")

    def test_add_then_remove_a_vc_without_master_admin(self) -> None:
        """足して、行を消して、枠を消して、VC品種も消す ── マスタ管理を開かずに。"""
        res = self.post("/api/vc/quick-block", {**self.ADD, "password": "nisk"})
        body = res.get_json()
        self.assertEqual(res.status_code, 200, body)
        self.assertIn("早見表に「畳論マン」を足しました(マス 3)", body["message"])
        added = self.blocks(body)["畳論マン"]
        self.assertEqual((added["product"], added["cells"]), ("畳論マン", 3))
        self.assertIn("畳論マン", [b["name"] for b in body["quick"]["blocks"]])
        # 計算の品種一覧にも出る
        state = self.get("/api/vc/state").get_json()["state"]
        self.assertIn("畳論マン", [p["name"] for p in state["products"]])

        self.unlock()                                   # 以降は鍵を開けたまま
        res = self.post("/api/vc/quick-cells/delete", {"block": "畳論マン", "thicknesses": "30"})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(self.blocks(res.get_json())["畳論マン"]["thicknesses"], ["5", "20"])

        res = self.post("/api/vc/quick-block/delete", {"block": "畳論マン"})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertNotIn("畳論マン", self.blocks(res.get_json()))
        products = {p["name"]: p for p in res.get_json()["grid"]["products"]}
        self.assertEqual(products["畳論マン"]["blocks"], [])

        res = self.post("/api/vc/product/delete", {"product": "畳論マン"})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertNotIn("畳論マン", [p["name"] for p in res.get_json()["grid"]["products"]])
        state = self.get("/api/vc/state").get_json()["state"]
        self.assertNotIn("畳論マン", [p["name"] for p in state["products"]])

    def test_switch_between_formula_and_fixed(self) -> None:
        self.unlock()
        res = self.post("/api/vc/quick-block/source", {"block": "R575B", "product": "TF200/TF200B"})
        self.assertEqual(res.status_code, 200, res.get_json())
        r575 = self.blocks(res.get_json())["R575B"]
        self.assertEqual(r575["product"], "TF200/TF200B")
        self.assertIn("式: VC品種「TF200/TF200B」", r575["kind"])
        res = self.post("/api/vc/quick-block/source", {"block": "R575B", "product": ""})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertIn("固定値", self.blocks(res.get_json())["R575B"]["kind"])

    def test_refusals_are_400_and_say_why(self) -> None:
        self.unlock()
        for url, body, words in (
                ("/api/vc/quick-block", {**self.ADD, "new_vcatu": ""}, "VC厚"),
                ("/api/vc/quick-block", {**self.ADD, "new_product": "", "product": "V325系"},
                 "枠はもうあります"),
                ("/api/vc/quick-cells/delete", {"block": "R575B"}, "消す内径か肉厚"),
                ("/api/vc/quick-block/delete", {"block": "無い枠"}, "ありません"),
                ("/api/vc/product/delete", {"product": "無い品種"}, "ありません")):
            with self.subTest(url=url, body=body):
                res = self.post(url, body)
                self.assertEqual((res.status_code, res.get_json()["error"]["code"]),
                                 (400, "bad_value"))
                self.assertIn(words, res.get_json()["error"]["message"])

    def test_master_admin_points_to_the_one_step_delete(self) -> None:
        """マスタ管理で VC品種 を消そうとして断られたら、1回で消せる場所を言う。"""
        self.get("/api/vc/state")
        self.unlock()
        rows = self.get(f"/api/master/browse?file=vc&table={quote('VC品種')}").get_json()
        row = next(r for r in rows["page"]["rows"] if r["品種名"] == "V325系")
        res = self.post("/api/master/row/delete", {"file": "vc", "table": "VC品種",
                                                    "key": row["__行"]})
        self.assertEqual(res.status_code, 400)
        message = res.get_json()["error"]["message"]
        self.assertIn("使われているので消せません", message)
        self.assertIn("早見表ブロック", message)
        self.assertIn("VC長さ計算 →「設定」→ 早見表の品種", message)

    def test_screen_has_the_parts(self) -> None:
        page = self.get("/vc").get_data(as_text=True)
        settings = page[page.index('data-panel="settings"'):]
        for id_ in ("vc-blocks", "vc-add-product", "vc-add-make", "vc-edit-kind",
                    "vc-edit-source", "vc-edit-insides", "vc-edit-delete",
                    "vc-del-product", "vc-del-product-go", "vc-grid-make"):
            self.assertIn(f'id="{id_}"', settings, id_)
        self.assertIn('<option value="teal">青緑</option>', settings)
        self.assertIn("早見表の品種(枠とマス)", settings)
        js = (STATIC / "js" / "views" / "vc.js").read_text(encoding="utf-8")
        for url in ("/api/vc/quick-block", "/api/vc/quick-block/delete",
                    "/api/vc/quick-block/source", "/api/vc/quick-cells/delete",
                    "/api/vc/product/delete"):
            self.assertIn(f'"{url}"', js)
        # 消す前に、何が消えるかを数で言う
        self.assertIn("の枠とマス ${b.cells} を消します", js)
        self.assertIn("固定値(打った長さ)", js)
