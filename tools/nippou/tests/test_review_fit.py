"""直の実績を画面最大化で1画面に収める / 共有保存の履歴の期間 (v3.86.0)

    直 の実績：画面いっぱいにする 大きすぎて見えません
    画面最大化で収まるサイズに表示してください

    共有保存の履歴：期間も選べるようにしてください

【直の実績】推移の絵(720×220)を幅いっぱいに伸ばしていたので、幅 1900px
の画面では1枚が高さ 560px、3枚で画面の2倍を超えていました。左に締めくくり
と表、右に推移を置き、**画面ごとは動かさず**、絵は枠の大きさで描き直します。
ここでは**本物のブラウザ(Chromium)で開いて測ります**(Playwright が無い
環境では飛ばします)。

【共有保存の履歴】期間は上の表の開始日・終了日に付いていくだけでした。
カードの中に開始日・終了日と「今月 / 先月 / 直近7日 / 直近30日」を置きます。
"""
from __future__ import annotations

import glob
import re
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.presenters import push_log  # noqa: E402
from tests._web import HEADERS, WebTestCase  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"


def _read(*parts: str) -> str:
    return ROOT.joinpath(*parts).read_text(encoding="utf-8")


class PresetTests(unittest.TestCase):
    """期間をすぐ選ぶボタンの日付(`push_log.period_presets`)。"""

    def spans(self, today: date) -> dict[str, tuple[str, str]]:
        return {p["label"]: (p["start"], p["end"]) for p in push_log.period_presets(today)}

    def test_並びと日付(self) -> None:
        got = push_log.period_presets(date(2026, 9, 25))
        self.assertEqual([p["label"] for p in got], ["今月", "先月", "直近7日", "直近30日"])
        self.assertEqual(self.spans(date(2026, 9, 25)), {
            "今月": ("2026-09-01", "2026-09-25"),
            "先月": ("2026-08-01", "2026-08-31"),
            "直近7日": ("2026-09-19", "2026-09-25"),
            "直近30日": ("2026-08-27", "2026-09-25"),
        })

    def test_1月の先月は去年の12月(self) -> None:
        self.assertEqual(self.spans(date(2027, 1, 3))["先月"], ("2026-12-01", "2026-12-31"))

    def test_うるう年の2月(self) -> None:
        self.assertEqual(self.spans(date(2028, 3, 1))["先月"], ("2028-02-01", "2028-02-29"))

    def test_直近は今日を含めて数える(self) -> None:
        start, end = self.spans(date(2026, 9, 25))["直近7日"]
        self.assertEqual((date.fromisoformat(end) - date.fromisoformat(start)).days + 1, 7)

    def test_30日ある月の30日は今月と直近30日が同じ幅_印は1つ(self) -> None:
        """9月30日は「今月」も「直近30日」も 9/1〜9/30。**2つとも押されて見えない**
        ように、画面は合うもののうち1つだけに印を付ける(日付によって落ちていた)。"""
        spans = self.spans(date(2026, 9, 30))
        self.assertEqual(spans["今月"], spans["直近30日"])
        root = Path(__file__).resolve().parent.parent
        html = (root / "app/templates/agg.html").read_text(encoding="utf-8")
        self.assertIn("marked.done", html)
        js = (root / "app/static/js/views/agg.js").read_text(encoding="utf-8")
        self.assertIn("pickedPreset", js)
        self.assertIn("|| fits[0]", js)


class WiringTests(unittest.TestCase):
    """画面の配線(ブラウザが無くても見られるもの)。"""

    def test_履歴はカードの期間で引く(self) -> None:
        js = _read("app", "static", "js", "views", "agg.js")
        self.assertIn('getElementById("push-log-start")', js)
        self.assertIn('getElementById("push-log-end")', js)
        # 上の表の「表示」は、もう履歴を引き直さない(期間が別なので)
        reload_handler = js[js.index('getElementById("reload")'):js.index('getElementById("push-log-reload")')]
        self.assertNotIn("loadPushLog", reload_handler)

    def test_直の実績は枠の大きさで描く(self) -> None:
        self.assertIn("{ fit: true }", _read("app", "static", "js", "views", "review.js"))
        chart = _read("app", "static", "js", "chart.js")
        self.assertIn("export function paintCharts(host, history, { fit = false } = {})", chart)
        self.assertIn("ResizeObserver", chart)
        html = _read("app", "templates", "graph_review.html")
        for part in ('class="review__side"', 'class="fit-charts" id="review-charts"',
                     "review__card--grow"):
            self.assertIn(part, html)

    def test_濃紺の画面でも字が地に溶けない(self) -> None:
        """集計・グラフ・直の実績(`data-page=graph/agg`)の濃紺の地。

        載せたときのボタンの地・知らせの地が明るい画面の値のままで、
        明るい字が淡い地に溶けていました(ボタンの名前・直すところの一覧)。
        """
        css = _read("app", "static", "css", "tokens.css")
        # v4.19.0 から「ライトを選んでいなければ」の条件つき(`test_theme_switch`)
        block = css[css.index(':root[data-skin="neon"]:not([data-theme="light"]) '
                              'body[data-page="graph"]'):]
        block = block[:block.index("}")]
        for token in ("--btn-hover:", "--state-error-bg:", "--state-warn-bg:",
                      "--state-ok-bg:", "--state-info-bg:", "--accent-soft:"):
            with self.subTest(token=token):
                self.assertIn(token, block)


class PageTests(WebTestCase):
    """集計管理の画面と、期間つきの履歴。"""

    def seed(self) -> None:
        rows = []
        for day in (date(2026, 8, 20), date(2026, 9, 3), date(2026, 9, 20)):
            label = f"{day.year}年{day.month}月{day.day}日"
            rows.append(dict(id=f"r{day.isoformat()}", report_date=label, line="L-1",
                             shift="1直", pages=1, worker="山田", result="送れた",
                             pressed_at=f"{day.isoformat()}T14:50:00", pressed_by="山田"))
        self.repo().add_push_history(rows)

    def test_カードに期間とすぐ選ぶボタンがある(self) -> None:
        body = self.get("/agg").get_data(as_text=True)
        start = re.search(r'id="push-log-start" type="date" value="([\d-]+)"', body)
        end = re.search(r'id="push-log-end" type="date" value="([\d-]+)"', body)
        self.assertIsNotNone(start)
        self.assertIsNotNone(end)
        # 既定は上の表と同じ月初〜当日で、「今月」に印
        self.assertTrue(start.group(1).endswith("-01"))
        pressed = re.findall(r'data-preset="(\w+)"\s+aria-pressed="true"', body)
        self.assertEqual(pressed, ["this_month"])
        self.assertNotIn("期間は上の開始日〜終了日", body)

    def test_選んだ期間だけ出る(self) -> None:
        self.seed()
        got = self.post("/api/agg/push-log", {"start": "2026-09-01", "end": "2026-09-30",
                                             "scope": "line"}).get_json()
        self.assertEqual(len(got["table"]["rows"]), 2)
        self.assertEqual((got["start"], got["end"]), ("2026-09-01", "2026-09-30"))
        got = self.post("/api/agg/push-log", {"start": "2026-08-01", "end": "2026-08-31",
                                             "scope": "line"}).get_json()
        self.assertEqual(len(got["table"]["rows"]), 1)

    def test_逆さの期間は断る(self) -> None:
        res = self.post("/api/agg/push-log", {"start": "2026-09-30", "end": "2026-09-01"})
        self.assertEqual(res.status_code, 422)
        self.assertIn("開始日は終了日以前", res.get_json()["error"]["message"])


def _chromium():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, None
    pw = sync_playwright().start()
    for path in [None] + sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome")):
        try:
            return pw, (pw.chromium.launch(executable_path=path) if path
                        else pw.chromium.launch())
        except Exception:                          # noqa: BLE001 - 次を試す
            continue
    pw.stop()
    return None, None


_MEASURE = """() => {
  const body = document.querySelector('.review__body');
  const charts = [...document.querySelectorAll('#review-charts svg')].map(s => {
    const r = s.getBoundingClientRect();
    const label = s.querySelector('text.label');
    const zoom = r.width / s.viewBox.baseVal.width;
    return {bottom: r.bottom, right: r.right, height: r.height, vbw: s.viewBox.baseVal.width,
            label: label ? parseFloat(getComputedStyle(label).fontSize) * zoom : 0};
  });
  return {scroll: body.scrollHeight, client: body.clientHeight,
          page: document.documentElement.scrollHeight, w: innerWidth, h: innerHeight, charts};
}"""


class ReviewFitTests(WebTestCase):
    """**本物のブラウザで直の実績を開いて測る。**

    要求は Flask のテストクライアントへ回します(ポートを開かない)。
    中身は1か月ぶん3直ずつ、直すところも停止もある日です。
    """

    #: 画面最大化したときの見える大きさ(タブやタスクバーを除く)と全画面
    MAXIMIZED = ((1920, 969), (1366, 657), (1280, 913), (1920, 1080))

    @classmethod
    def setUpClass(cls) -> None:
        cls.pw, cls.browser = _chromium()
        if cls.browser is None:
            raise unittest.SkipTest("Chromium(Playwright)がありません")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.pw.stop()

    def setUp(self) -> None:
        super().setUp()
        from nippou.db.models import DetailRecord, HeaderRecord
        from nippou.services import summary
        from nippou.work_context import get_context

        repo = self.repo()
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

    def open(self, width: int, height: int):
        page = self.browser.new_page(viewport={"width": width, "height": height})
        self.addCleanup(page.close)
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.route("http://app.test/**", self._forward)
        page.goto("http://app.test/graph/review", wait_until="load")
        page.wait_for_selector("#review-charts svg")
        page.wait_for_timeout(300)
        self.assertEqual(errors, [])
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
            res.close()                     # 静的ファイルは開いたまま返ってくる

    def test_画面最大化で1画面に収まる(self) -> None:
        for width, height in self.MAXIMIZED:
            with self.subTest(size=f"{width}x{height}"):
                page = self.open(width, height)
                got = page.evaluate(_MEASURE)
                self.assertLessEqual(got["scroll"], got["client"] + 1,
                                     "中身が画面の高さを超えています(画面ごと動かさない)")
                self.assertEqual(len(got["charts"]), 3)
                for chart in got["charts"]:
                    self.assertLessEqual(chart["bottom"], height + 0.5)
                    self.assertLessEqual(chart["right"], width + 0.5)
                    # 3枚とも同じ画面に。1枚で画面の半分を超えない
                    self.assertLess(chart["height"], height / 2)
                    # 字は読める大きさ(伸ばしも潰しもしない)
                    self.assertGreaterEqual(chart["label"], 9.5)

    def test_窓の大きさが変わったら描き直す(self) -> None:
        page = self.open(1366, 657)
        before = page.evaluate(_MEASURE)
        page.set_viewport_size({"width": 1920, "height": 1080})
        page.wait_for_timeout(400)
        after = page.evaluate(_MEASURE)
        self.assertNotEqual(before["charts"][0]["vbw"], after["charts"][0]["vbw"])
        self.assertLessEqual(after["scroll"], after["client"] + 1)

    def test_狭い画面では縦に流す(self) -> None:
        page = self.open(900, 800)
        got = page.evaluate(_MEASURE)
        self.assertGreater(got["scroll"], got["client"], "狭い画面は箱の中で縦に流します")
        for chart in got["charts"]:
            self.assertAlmostEqual(chart["height"], 220, delta=1)
            self.assertLessEqual(chart["right"], 900 + 0.5)


if __name__ == "__main__":
    unittest.main()
