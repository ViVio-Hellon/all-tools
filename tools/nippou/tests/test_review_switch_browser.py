"""直の実績: グラフに7割・左は「実数 / 停止」の切り替え

    左に数値、右にグラフとなって半分半分ぐらいですが、グラフに7割使用してください。
    左の数値画面は切り替え可能にして、切り替えには『実数』と『停止』をつけ、実数に
    切り替えで『集計・グラフ』で確認できる 本日の合計重量/本日の合計枚数/本日の稼働率/
    本日の停止時間/本日の稼働時間/本日の生産性/累積枚数 を『集計・グラフ』と同じように
    表示させてください。『停止』に切り替えで右グラフを停止のグラフに切り替えてください

ここで確かめること:

    サーバ   実数の札の値が、集計・グラフ(`/graph`)の上の7つと同じ字になる
             停止の絵は、集計・グラフの停止のタイルと同じ中身
    画面     既定は実数(左に札・右に今月の推移)、右が7割(全画面でも)
             「停止」で左が停止の表・右が停止の絵に変わる。画面最大化で1画面に収まる
             選んだほうを覚えて、開き直すとそちらで開く。覚えられない端末でも切り替わる

本物のブラウザは `test_review_fit` と同じく、要求を Flask のテストクライアントへ回します。
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._browser import chromium_or_skip  # noqa: E402
from tests._web import HEADERS, WebTestCase  # noqa: E402

#: 集計・グラフの上の7つ(並びも同じ)
FIGURES = ["本日の合計重量", "本日の合計枚数", "本日の稼働率", "本日の停止時間",
           "本日の稼働時間", "本日の生産性", "累積枚数"]


def seed_month(case: WebTestCase) -> None:
    """月初から今日まで、毎日 3直ぶん(停止もある)。`test_review_fit` と同じ中身。"""
    from nippou.db.models import DetailRecord, HeaderRecord
    from nippou.services import summary
    from nippou.work_context import get_context

    repo = case.repo()
    line = get_context().line
    today = date.today()
    day = today.replace(day=1)
    while day <= today:
        label = f"{day.year}年{day.month}月{day.day}日"
        for shift, hour in (("1直", "07"), ("2直", "15"), ("3直", "22")):
            key = dict(report_date=label, line=line, shift=shift, page=1)
            rows = [DetailRecord(**key, row_no=n, lot=f"N71{n:02d}T0", kz=hour,
                                 kh=f"{n * 5:02d}", sz=hour, sh=f"{n * 5 + 4:02d}",
                                 con=str(100 + n), wei=str(1200 + n * 90),
                                 s="ｱｲｳｴｵｶｷｸ"[(n + day.day) % 8], th=str(5 + n))
                    for n in range(1, 9)]
            repo.save(HeaderRecord(**key, worker="山田"), rows)
            summary.refresh_shift(repo, label, line, shift)
        day += timedelta(days=1)


def tile_values(page_html: str) -> dict[str, str]:
    """描かれた数字の札 `{題: "値 単位"}`(`_number_tile.html` の形)。"""
    found = {}
    for m in re.finditer(
            r'<h2 class="tile__title">(.*?)</h2>.*?<div class="tile__value">\s*(.*?)'
            r'<span class="tile__unit">(.*?)</span>', page_html, re.S):
        found.setdefault(html.unescape(m.group(1)),
                         f"{html.unescape(m.group(2)).strip()} {html.unescape(m.group(3))}")
    return found


def seed_json(page_html: str, element_id: str):
    m = re.search(rf'<script id="{element_id}" type="application/json">(.*?)</script>',
                  page_html, re.S)
    return json.loads(m.group(1)) if m else None


class FigureTests(WebTestCase):
    """**実数の札は集計・グラフと同じ数。** 数え直さず、同じ組み立てから抜き出しています。"""

    def setUp(self) -> None:
        super().setUp()
        seed_month(self)

    def span(self) -> dict:
        """集計・グラフを開いたときの期間(月初〜その日。**日付は業務日**で、時計の日付ではない)。"""
        board = seed_json(self.get("/graph").get_data(as_text=True), "dashboard-data")
        return {"start": board["start"], "end": board["end"], "mode": "day"}

    def test_実数の札は集計グラフの上の7つと同じ値(self) -> None:
        graph = self.get("/graph").get_data(as_text=True)
        review = self.get("/graph/review").get_data(as_text=True)
        want = tile_values(graph)
        got = tile_values(review)
        self.assertEqual([t for t in got if t in FIGURES], FIGURES, "札の並びが違います")
        for title in FIGURES:
            with self.subTest(title=title):
                self.assertEqual(got[title], want[title])
        # 中身のある日(0 ばかりで同じ、では確かめたことにならない)
        self.assertNotEqual(got["本日の合計重量"].split()[0], "0.00")
        self.assertNotEqual(got["累積枚数"].split()[0], "0")

    def test_札の値はダッシュボードの数とも同じ(self) -> None:
        """画面の字ではなく、集計・グラフが返す数(`/api/graph/dashboard`)とも突き合わせる。"""
        board = self.post("/api/graph/dashboard", self.span()).get_json()
        want = {t["title"]: f'{t["value"]} {t["unit"]}' for t in board["tiles"]
                if t["kind"] == "number"}
        got = tile_values(self.get("/graph/review").get_data(as_text=True))
        for title in FIGURES:
            with self.subTest(title=title):
                self.assertEqual(got[title], want[title])

    def test_途中式も同じ(self) -> None:
        """稼働率などの「計算内容を見る」も、集計・グラフと同じ式・同じ数。"""
        graph = self.get("/graph").get_data(as_text=True)
        review = self.get("/graph/review").get_data(as_text=True)
        steps = re.compile(r'<code class="calc__sub">(.*?)</code>', re.S)
        self.assertTrue(steps.findall(review))
        self.assertEqual(steps.findall(review), steps.findall(graph)[:len(steps.findall(review))])

    def test_停止の絵は集計グラフの停止のタイルと同じ中身(self) -> None:
        board = self.post("/api/graph/dashboard", self.span()).get_json()
        want = {t["key"]: t for t in board["tiles"]}
        got = seed_json(self.get("/graph/review").get_data(as_text=True), "review-stop-data")
        self.assertEqual([t["key"] for t in got], ["stop_kinds", "stop_items", "shift_stop"])
        for tile in got:
            with self.subTest(key=tile["key"]):
                for field in ("title", "kind", "slices", "values", "labels", "total"):
                    self.assertEqual(tile[field], want[tile["key"]][field], field)
        self.assertTrue(got[0]["slices"], "停止のある日なのに分類の輪が空です")

    def test_札から画面を出る道は置かない(self) -> None:
        """「紙を見る」などの行き先は置かない ── 出る道は「確認しました」だけ。"""
        review = self.get("/graph/review").get_data(as_text=True)
        tiles = review[review.index('id="review-tiles"'):review.index('id="review-shifts"')]
        self.assertNotIn("<a ", tiles)


class EmptyDayTests(WebTestCase):
    def test_何も無い日でも札と停止の絵の題は出る(self) -> None:
        body = self.get("/graph/review").get_data(as_text=True)
        got = tile_values(body)
        for title in FIGURES:
            self.assertIn(title, got)
        stops = seed_json(body, "review-stop-data")
        self.assertEqual(len(stops), 3)
        self.assertIn("この日の停止(項目ごと)", body)


_LAYOUT = """() => {
  const vis = (el) => !!el && !el.hidden && el.getClientRects().length > 0;
  const box = (sel) => { const el = document.querySelector(sel);
    if (!vis(el)) return null; const r = el.getBoundingClientRect();
    return {left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width,
            height: r.height}; };
  const body = document.querySelector('.review__body');
  const side = box('.review__side');
  const right = box('#review-charts-card') || box('#review-stop-charts');
  return {
    view: body.dataset.reviewView,
    selected: [...document.querySelectorAll('[data-review-tab]')]
      .filter(t => t.getAttribute('aria-selected') === 'true').map(t => t.textContent.trim()),
    figures: vis(document.getElementById('review-figures')),
    stops: vis(document.getElementById('review-stops')),
    trend: vis(document.getElementById('review-charts-card')),
    stopCharts: vis(document.getElementById('review-stop-charts')),
    tiles: [...document.querySelectorAll('#review-tiles .tile')].filter(vis)
      .map(t => t.querySelector('.tile__title').textContent.trim()),
    trendSvg: [...document.querySelectorAll('#review-charts svg')].filter(vis).length,
    donuts: [...document.querySelectorAll('#review-stop-charts svg.donut')].filter(vis).length,
    stopBars: [...document.querySelectorAll('#review-stop-charts svg.chart rect.bar')]
      .filter(vis).length,
    stopSvgBottom: Math.max(0, ...[...document.querySelectorAll('#review-stop-charts svg')]
      .filter(vis).map(s => s.getBoundingClientRect().bottom)),
    reason: vis(document.getElementById('review-reason')),
    closingTop: box('#closing')?.top ?? null,
    switchTop: box('.review-switch')?.top ?? null,
    side, right,
    scroll: body.scrollHeight, client: body.clientHeight,
    w: innerWidth, h: innerHeight,
  };
}"""


class SwitchBrowserTests(WebTestCase):
    """本物のブラウザで開いて、押して、測る。"""

    MAXIMIZED = ((1920, 969), (1366, 657), (1280, 913), (1920, 1080))

    @classmethod
    def setUpClass(cls) -> None:
        cls.pw, cls.browser = chromium_or_skip()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.pw.stop()

    def setUp(self) -> None:
        super().setUp()
        seed_month(self)

    def context(self, width: int = 1366, height: int = 657, *, init: str = ""):
        ctx = self.browser.new_context(viewport={"width": width, "height": height})
        self.addCleanup(ctx.close)
        if init:
            ctx.add_init_script(init)
        ctx.route("http://app.test/**", self._forward)
        return ctx

    def open(self, ctx, path: str = "/graph/review"):
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        self.addCleanup(lambda: self.assertEqual(errors, []))
        page.goto(f"http://app.test{path}", wait_until="load")
        page.wait_for_function("document.querySelector('.review__body')?.dataset.reviewView")
        # 出ているほうの絵が描けるまで(隠れているほうは描かない)
        page.wait_for_function("""() => [...document.querySelectorAll(
            '#review-charts svg, #review-stop-charts svg.donut')]
            .some((s) => s.getClientRects().length > 0)""")
        page.wait_for_timeout(200)
        return page

    def _forward(self, route) -> None:
        req = route.request
        path = req.url.split("http://app.test", 1)[1]
        res = self.client.open(path, method=req.method, headers=HEADERS,
                               data=req.post_data_buffer,
                               content_type=req.headers.get("content-type"))
        try:
            route.fulfill(status=res.status_code, body=res.get_data(),
                          headers={"content-type": res.headers.get("Content-Type", "text/plain")})
        finally:
            res.close()

    def layout(self, page) -> dict:
        return page.evaluate(_LAYOUT)

    def to_stops(self, page) -> dict:
        page.click("#review-tab-stops")
        page.wait_for_selector("#review-stop-charts svg.donut")
        page.wait_for_selector("#review-stop-charts svg.chart")
        return self.layout(page)

    def assert_seventy(self, got: dict) -> None:
        total = got["right"]["right"] - got["side"]["left"]
        self.assertAlmostEqual(got["right"]["width"] / total, 0.7, delta=0.03,
                               msg=f"右のグラフが7割になっていません: {got}")
        self.assertLess(got["side"]["width"] / total, 0.32)

    def test_既定は実数_左に7つの札_右に今月の推移で7割(self) -> None:
        page = self.open(self.context(1920, 969))
        got = self.layout(page)
        self.assertEqual(got["view"], "figures")
        self.assertEqual(got["selected"], ["実数"])
        self.assertTrue(got["figures"] and got["trend"])
        self.assertFalse(got["stops"] or got["stopCharts"])
        self.assertEqual(got["tiles"], FIGURES)
        self.assertEqual(got["trendSvg"], 3)
        self.assert_seventy(got)
        # 札の値は、サーバが描いたまま(集計・グラフと同じ字)
        weight = page.inner_text('#review-tiles [data-tile="today_weight"] .tile__value')
        self.assertRegex(weight.replace("\n", ""), r"^\d+\.\d\dt$")

    def test_停止に切り替えると左は停止の表_右は停止の絵(self) -> None:
        page = self.open(self.context(1920, 969))
        got = self.to_stops(page)
        self.assertEqual(got["view"], "stops")
        self.assertEqual(got["selected"], ["停止"])
        self.assertTrue(got["stops"] and got["stopCharts"])
        self.assertFalse(got["figures"] or got["trend"], "実数の札・推移が残っています")
        self.assertEqual(got["donuts"], 2)
        self.assertEqual(got["stopBars"], 3, "直別の停止時間の棒(3直)が出ていません")
        self.assertIn("この日の停止(項目ごと)", page.inner_text("#review-stops"))
        self.assert_seventy(got)
        # 戻すと元どおり
        page.click("#review-tab-figures")
        got = self.layout(page)
        self.assertTrue(got["figures"] and got["trend"])
        self.assertFalse(got["stops"] or got["stopCharts"])
        self.assertEqual(got["trendSvg"], 3)

    def test_切り替えの上に締めくくりと知らせが残る(self) -> None:
        page = self.open(self.context(1366, 657))
        for view in ("figures", "stops"):
            with self.subTest(view=view):
                page.click(f"#review-tab-{view}")
                got = self.layout(page)
                self.assertIsNotNone(got["closingTop"])
                self.assertLess(got["closingTop"], got["switchTop"])
                self.assertTrue(page.locator("#review-act").is_visible())
                self.assertTrue(page.locator("#review-fullscreen").is_visible())

    def test_画面最大化で1画面に収まる_どちらの切り替えでも(self) -> None:
        for width, height in self.MAXIMIZED:
            with self.subTest(size=f"{width}x{height}"):
                page = self.open(self.context(width, height))
                got = self.layout(page)
                self.assertLessEqual(got["scroll"], got["client"] + 1)
                self.assert_seventy(got)
                got = self.to_stops(page)
                self.assertLessEqual(got["scroll"], got["client"] + 1,
                                     "停止の絵が画面の高さを超えています")
                self.assertLessEqual(got["stopSvgBottom"], height + 0.5)
                self.assert_seventy(got)

    def test_選んだほうを覚えて_開き直すとそちらで開く(self) -> None:
        ctx = self.context()
        page = self.open(ctx)
        self.assertEqual(self.layout(page)["view"], "figures")
        self.to_stops(page)
        self.assertEqual(page.evaluate("localStorage.getItem('nippou.review.view')"), "stops")
        page.reload(wait_until="load")
        page.wait_for_selector("#review-stop-charts svg.donut")
        got = self.layout(page)
        self.assertEqual((got["view"], got["selected"]), ("stops", ["停止"]))
        self.assertTrue(got["stops"] and got["stopCharts"])
        self.assertFalse(got["figures"] or got["trend"])
        # 別の窓(同じ端末)で開いても停止で開き、実数へ戻すと覚え直す
        again = self.open(ctx)
        self.assertEqual(self.layout(again)["view"], "stops")
        again.click("#review-tab-figures")
        again.wait_for_selector("#review-charts svg")
        self.assertEqual(again.evaluate("localStorage.getItem('nippou.review.view')"),
                         "figures")
        third = self.open(ctx)
        got = self.layout(third)
        self.assertEqual(got["view"], "figures")
        self.assertEqual(got["trendSvg"], 3, "隠れていた推移が描けていません")

    def test_覚えられない端末でも切り替わる(self) -> None:
        broken = """
          for (const name of ['getItem', 'setItem']) {
            Storage.prototype[name] = () => { throw new Error('denied'); };
          }"""
        page = self.open(self.context(init=broken))
        self.assertEqual(self.layout(page)["view"], "figures")
        got = self.to_stops(page)
        self.assertTrue(got["stops"] and got["stopCharts"])

    def test_知らない値を覚えていたら実数で開く(self) -> None:
        page = self.open(self.context(
            init="try { localStorage.setItem('nippou.review.view', 'nanika'); } catch {}"))
        self.assertEqual(self.layout(page)["view"], "figures")

    def test_矢印キーでも切り替わる(self) -> None:
        page = self.open(self.context())
        page.focus("#review-tab-figures")
        page.keyboard.press("ArrowRight")
        page.wait_for_selector("#review-stop-charts svg.donut")
        self.assertEqual(self.layout(page)["view"], "stops")
        self.assertEqual(page.evaluate("document.activeElement.id"), "review-tab-stops")

    def test_狭い画面では縦に流す_停止でも(self) -> None:
        page = self.open(self.context(900, 800))
        got = self.layout(page)
        self.assertGreater(got["scroll"], got["client"])
        self.assertLess(got["side"]["bottom"], got["right"]["top"] + 1, "縦に並んでいません")
        got = self.to_stops(page)
        self.assertLess(got["side"]["bottom"], got["right"]["top"] + 1)
        self.assertLessEqual(got["right"]["right"], 900 + 0.5)

    def test_全画面にしても7割(self) -> None:
        page = self.open(self.context(1366, 657))
        page.click("#review-fullscreen")
        try:
            page.wait_for_function("!!document.fullscreenElement", timeout=3000)
        except Exception:                          # noqa: BLE001 - 全画面にできない環境
            self.skipTest("このブラウザでは全画面にできません")
        page.wait_for_timeout(300)
        self.assert_seventy(self.layout(page))
        self.assert_seventy(self.to_stops(page))

    @unittest.skipUnless(os.environ.get("NIPPOU_REVIEW_SHOT"), "画面写真は頼まれたときだけ")
    def test_画面写真(self) -> None:
        out = Path(os.environ["NIPPOU_REVIEW_SHOT"])
        out.mkdir(parents=True, exist_ok=True)
        for width, height in ((1366, 657), (1920, 1080)):
            page = self.open(self.context(width, height))
            page.screenshot(path=str(out / f"review_figures_{width}x{height}.png"))
            self.to_stops(page)
            page.screenshot(path=str(out / f"review_stops_{width}x{height}.png"))


from tests.test_push_after_review_browser import AfterPushBrowserCase  # noqa: E402


class AfterPushFlowTests(AfterPushBrowserCase):
    """**本当の道**: 日報入力の「共有へ保存」→ 直の実績(画面の差し替えで来る)。"""

    _LOOK = """() => ({
      page: document.body.dataset.page,
      tile: getComputedStyle(document.querySelector('#review-tiles .tile')).backgroundColor,
      h3: getComputedStyle(document.querySelector('#review-charts h3')).color,
    })"""

    def test_共有へ保存のあと_札は濃紺の島で_切り替えても確認しましたで戻る(self) -> None:
        self.save_clean("1直", "山田")
        self.at("2026-10-05 14:20")
        page = self.open_screen("/", "#push-shared")
        self.assertEqual(page.evaluate("document.body.dataset.page"), "entry")
        self.push_to_review(page)
        # 差し替えで来ても「集計・グラフのなかま」の見た目(`body[data-page]` を移す ──
        # 移さないと札がくすんだ灰色、推移の題が濃紺に沈む)
        look = page.evaluate(self._LOOK)
        self.assertEqual(look["page"], "graph")
        self.assertEqual(look["tile"], "rgb(6, 10, 30)", "札が濃紺の島になっていません")
        self.assertEqual(look["h3"], "rgb(232, 240, 255)", "推移の題が地に沈んでいます")
        self.assertIn("1直 を共有へ渡しました", page.inner_text("#closing-headline"))
        titles = page.locator("#review-tiles .tile__title").all_inner_texts()
        self.assertEqual([t.strip() for t in titles], FIGURES)

        page.click("#review-tab-stops")
        page.wait_for_selector("#review-stop-charts svg.donut")
        self.assertIn("この日の停止(項目ごと)", page.inner_text("#review-stops"))
        self.assertTrue(page.locator("#review-charts-card").is_hidden())
        # 知らせと締めくくりは切り替えても残る
        self.assertIn("共有へ保存しました", page.inner_text("#review-reason"))
        self.assertEqual(page.inner_text("#review-act").strip(), "確認しました")

        self.back_to_entry(page)
        self.assertEqual(page.evaluate("document.body.dataset.page"), "entry")
        self.wait_mark(page, "saved", f"{SAVED_MARK}(14:20 山田)")
        self.assertEqual(self.errors, [])


SAVED_MARK = "この直は共有へ保存済み"


if __name__ == "__main__":
    unittest.main()
