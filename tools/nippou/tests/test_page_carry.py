"""ページを足すと、前のページの最後の終了を1行目の開始に入れる (v4.35.1)

    日報入力:ページを追加した場合に前ページの最終行の終了が開始に引き継がれていない
    例:1ページ12行目終了14:55
      2ページ1行目開始14:55

ページの中では、終了を打つと次の行の開始に入ります(`navigation.same_text`)。
12行目からは写す先が無いので、**ページの変わり目だけ途切れていました** ──
次ページ発行は次のページを空で作るだけで、前のページの終わりを誰も見ていなかった。

【決め方】
    ・入れるのは**ページを描くとき**(`routes/entry._carry_from_previous_page`)。
      次ページ発行で出したばかりのページも、開き直したページも同じ道
    ・**12行に1文字も入っていないページだけ。** 何か打ってあれば、1行目の開始が
      空でも触らない(人が消したのかもしれない)。入っている開始は上書きしない
    ・前のページの「最後に打った行」を見る(空いた行・開始だけの行は飛ばす)。
      その行に終了が無ければ(打ちかけ)何もしない
    ・直の終わりの時刻(15:00 / 22:50 / 07:00)なら入れない(ページの中と同じ)
    ・入れた値は画面に出るだけ。打ち始めたときの保存でほかの欄と一緒に書く
    ・判断は `logic/navigation.carry_across_pages` の1か所

経路の試験は `test_scenario_shift_flow` と同じ止めた時計で、画面と同じ順に叩きます。
最後に本物のブラウザで「次ページ発行」を押して確かめます(無い環境では飛ばします)。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import navigation  # noqa: E402
from tests._web import HAS_FLASK, HEADERS, SKIP_REASON  # noqa: E402
from tests.test_scenario_shift_flow import (ScenarioCase, full_page_rows,  # noqa: E402
                                            row, rows_of)

#: 現場の終了時刻(`test_same_text` と同じ)
SITE = {"1直": ("15", "00"), "2直": ("22", "50"), "3直": ("07", "00"),
        "日勤": ("17", "00")}


def done(start: str, end: str, lot: str = "A1") -> dict:
    """終了まで打った行。"""
    return {"LOT": lot, "KZ": start[:2], "KH": start[3:], "SZ": end[:2], "SH": end[3:]}


def page_of(*items: dict) -> dict[int, dict]:
    return {n: values for n, values in enumerate(items, start=1)}


def carry(prev: dict[int, dict], *, first: dict | None = None, blank: bool = True,
          shift: str = "1直"):
    return navigation.carry_across_pages(
        prev, first or {}, page_is_blank=blank, day_temp=shift, shift_end_times=SITE)


class CarryAcrossPagesTests(unittest.TestCase):
    """入れるかどうか(`logic/navigation.carry_across_pages`)。"""

    def twelve(self, last_end: str = "14:55") -> dict[int, dict]:
        rows = {n: done(f"{12 + n // 6:02d}:00", f"{12 + n // 6:02d}:05", f"A{n:02d}")
                for n in range(1, 12)}
        rows[12] = done("14:45", last_end, "A12")
        return rows

    def test_12行目の終了14時55分が1行目の開始に入る(self) -> None:
        self.assertEqual(carry(self.twelve()), ("14", "55"))

    def test_8行目までなら8行目の終了(self) -> None:
        prev = page_of(*[done("10:00", "10:30", f"B{n}") for n in range(1, 8)],
                       done("10:30", "11:10", "B8"))
        self.assertEqual(carry(prev), ("11", "10"))

    def test_空いた行と開始だけの行は飛ばす(self) -> None:
        """9行目は8行目の終了から写っただけ・10〜12行目は空 → 8行目を見る。"""
        prev = page_of(*[done("10:00", "10:30", f"B{n}") for n in range(1, 8)],
                       done("10:30", "11:10", "B8"))
        prev[9] = {"KZ": "11", "KH": "10"}
        prev[10] = {}
        self.assertEqual(carry(prev), ("11", "10"))

    def test_途中の空いた行も飛ばして最後に打った行(self) -> None:
        prev = page_of(done("10:00", "10:30"), {}, {}, done("10:40", "11:00", "B4"))
        self.assertEqual(carry(prev), ("11", "00"))

    def test_最後の行に終了が無ければ何もしない(self) -> None:
        """打ちかけ。**上の行まで遡って古い時刻を入れない。**"""
        prev = self.twelve()
        prev[12] = {"LOT": "A12", "KZ": "14", "KH": "45"}
        self.assertIsNone(carry(prev))
        # 終了の分がまだ(時だけ)も打ちかけ
        prev[12] = done("14:45", "14:55", "A12") | {"SH": ""}
        self.assertIsNone(carry(prev))

    def test_前のページに打った行が無ければ何もしない(self) -> None:
        self.assertIsNone(carry({}))
        self.assertIsNone(carry({1: {"KZ": "08", "KH": "00"}}))

    def test_1行目に開始が入っていれば上書きしない(self) -> None:
        self.assertIsNone(carry(self.twelve(), first={"KZ": "14", "KH": "58"}))
        self.assertIsNone(carry(self.twelve(), first={"KZ": "", "KH": "58"}))

    def test_何か打ってあるページには入れない(self) -> None:
        """1行目の開始が空でも、ほかに何かあれば触らない(人が消したのかもしれない)。"""
        self.assertIsNone(carry(self.twelve(), first={"LOT": "C1"}, blank=False))

    def test_直の終わりの時刻なら入れない(self) -> None:
        for shift, end in (("1直", "15:00"), ("2直", "22:50"), ("3直", "07:00")):
            with self.subTest(shift=shift):
                self.assertIsNone(carry(self.twelve(end), shift=shift))

    def test_3直の0時またぎはそのまま入る(self) -> None:
        for end, want in (("23:55", ("23", "55")), ("00:10", ("00", "10"))):
            with self.subTest(end=end):
                self.assertEqual(carry(self.twelve(end), shift="3直"), want)

    def test_止め方はページの中と同じ(self) -> None:
        """`same_text` と `carry_across_pages` は同じ `ends_shift` で止める。"""
        for sz, sh in (("15", "00"), ("22", "50"), ("07", "00"), ("14", "55")):
            inside = navigation.same_text(row=1, sz_text=sz, sh_text=sh,
                                          day_temp="1直", shift_end_times=SITE)
            across = carry(self.twelve(f"{sz}:{sh}"))
            self.assertEqual(inside, across, (sz, sh))


def start_of(html: str, n: int = 1) -> str:
    """描いた画面の n 行目の開始(時:分)。"""
    kz = re.search(rf'id="KZ{n}"[^>]*value="([^"]*)"', html).group(1)
    kh = re.search(rf'id="KH{n}"[^>]*value="([^"]*)"', html).group(1)
    return f"{kz}:{kh}"


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class PageCarryRouteTests(ScenarioCase):
    """画面と同じ順に経路を叩く。"""

    def typed(self, rows: dict):
        """打ちかけを置く(`draft`)── 自動保存の間引き・関門に当たらずに必ず書く。"""
        return self.save(rows, draft=True)

    def drawn(self) -> str:
        res = self.get("/")
        self.assertEqual(res.status_code, 200)
        return res.get_data(as_text=True)

    def issue_first_page(self, rows: dict | None = None, *, at: str = "2026-10-05 14:57"):
        """1直の第1ページを12行(12:55〜14:55)打って、次ページ発行。"""
        self.start_shift("2026-10-05 07:05", "山田")
        self.at(at)
        res = self.new_page(rows or full_page_rows("A", "12:55"))
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(res.get_json()["page"], 2)

    def test_次ページ発行で2ページ目の1行目の開始が14時55分(self) -> None:
        self.issue_first_page()
        html = self.drawn()
        self.assertIn('data-opened-page="2"', html)
        self.assertEqual(start_of(html), "14:55")
        # 2行目から下は空のまま
        self.assertEqual(start_of(html, 2), ":")
        # **書くのは画面の保存。** 描いただけでは手元のページは空のまま
        # (出したまま使わなかったページは「中身の無い紙」で片付けられる)
        _header, details = self.repo().load("2026年10月5日", "L-1", "1直", 2)
        self.assertEqual((details[0].kz, details[0].kh), ("", ""))

    def test_打ち始めると開始もほかの欄と一緒に書かれる(self) -> None:
        self.issue_first_page()
        self.drawn()
        # 画面は描かれた開始ごと送ってくる(1行目のロットを打ったあとの保存)
        res = self.typed(rows_of({"LOT": "C01", "KZ": "14", "KH": "55"}))
        self.assertEqual(res.status_code, 200, res.get_json())
        _header, details = self.repo().load("2026年10月5日", "L-1", "1直", 2)
        self.assertEqual((details[0].lot, details[0].kz, details[0].kh), ("C01", "14", "55"))

    def test_1行目に開始が入っていれば上書きしない(self) -> None:
        self.issue_first_page()
        self.drawn()
        self.assertEqual(self.typed(rows_of({"LOT": "C01", "KZ": "14", "KH": "58"})
                                    ).status_code, 200)
        self.assertEqual(start_of(self.drawn()), "14:58")

    def test_何か打ってあれば開始が空でも入れない(self) -> None:
        """開始を消してロットだけ打った ── 人が消したのかもしれないので、入れ直さない。"""
        self.issue_first_page()
        self.drawn()
        self.assertEqual(self.typed(rows_of({"LOT": "C01"})).status_code, 200)
        self.assertEqual(start_of(self.drawn()), ":")

    def go_back_to_page_one(self) -> None:
        """表の下の「ページ」で第1ページを開く(まだ何も打っていない第2ページから)。"""
        res = self.post("/api/settings/page", {"page": 1})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.opened = None
        self.open_screen()
        self.assertEqual(self.opened["page"], "1")

    def test_最後の行が8行目までなら8行目の終了(self) -> None:
        """第1ページへ戻って9〜12行目を消し、**保存せずに**「最新のページに戻る」。

        戻る前に画面の中身が保存され(`settings._save_recalled`)、まだ何も打っていない
        第2ページには8行目の終了が入る ── 前のページが画面にだけある、の確かめも兼ねる。
        次ページ発行そのものは12行目まで打たないと通らないので、8行目で終わる前のページは
        こうして直したときにだけできます。
        """
        self.issue_first_page()
        self.go_back_to_page_one()
        rows = full_page_rows("A", "12:55")
        for n in range(9, 13):
            rows[str(n)] = {}
        res = self.post("/api/settings/back", self.sheet(rows))
        self.assertEqual(res.status_code, 200, res.get_json())
        self.opened = None
        _header, details = self.repo().load("2026年10月5日", "L-1", "1直", 1)
        self.assertEqual((details[7].sz, details[7].sh, details[8].lot), ("14", "15", ""))
        html = self.drawn()
        self.assertIn('data-opened-page="2"', html)
        self.assertEqual(start_of(html), "14:15")

    def test_8行目までのページからは次ページ発行が通らない(self) -> None:
        self.start_shift("2026-10-05 07:05", "山田")
        self.at("2026-10-05 14:20")
        rows = full_page_rows("A", "12:55")
        for n in range(9, 13):
            rows.pop(str(n))
        res = self.new_page(rows)
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"], "page_not_full")
        self.assertEqual(self.repo().saved_pages("2026年10月5日", "L-1", "1直"), [])

    def test_前のページの最後の行に終了が無ければ何もしない(self) -> None:
        """第1ページへ戻って12行目の終了を消す(打ちかけ)→ 戻っても第2ページの開始は空。"""
        self.issue_first_page()
        self.go_back_to_page_one()
        rows = full_page_rows("A", "12:55")
        rows["12"] = dict(rows["12"], SZ="", SH="")
        self.assertEqual(self.post("/api/settings/back", self.sheet(rows)).status_code, 200)
        self.opened = None
        self.assertEqual(start_of(self.drawn()), ":")

    def test_前のページの終了を直して戻ると直したあとの時刻(self) -> None:
        """まだ何も打っていない第2ページは、描くたびに前のページから入れ直す。"""
        self.issue_first_page()
        self.drawn()
        self.go_back_to_page_one()
        rows = full_page_rows("A", "12:55")
        rows["12"] = dict(rows["12"], SH="50")
        self.assertEqual(self.post("/api/settings/back", self.sheet(rows)).status_code, 200)
        self.opened = None
        self.assertEqual(start_of(self.drawn()), "14:50")

    def test_第1ページを開いても何も入れない(self) -> None:
        self.issue_first_page()
        self.go_back_to_page_one()
        self.assertEqual(start_of(self.drawn()), "12:55")

    def test_直の終わりの15時で終わっていれば入れない(self) -> None:
        self.issue_first_page(full_page_rows("A", "13:00"), at="2026-10-05 14:59")
        self.assertEqual(start_of(self.drawn()), ":")

    def test_3直の0時をまたいだ終了がそのまま入る(self) -> None:
        """3直 22:50〜07:00。第1ページ 22:55〜00:55 → 第2ページの1行目は 00:55(報告日は始めた日)。"""
        self.start_shift("2026-10-05 22:55", "夜勤")
        self.at("2026-10-06 00:58")
        res = self.new_page(full_page_rows("N", "22:55"))
        self.assertEqual(res.status_code, 200, res.get_json())
        self.at("2026-10-06 01:00")
        html = self.drawn()
        self.assertIn('data-opened-page="2"', html)
        self.assertIn('data-opened-shift="3直"', html)
        self.assertIn('data-opened-date="2026年10月5日"', html)
        self.assertEqual(start_of(html), "00:55")

    def test_3直の終わり7時で終わっていれば入れない(self) -> None:
        self.start_shift("2026-10-05 22:55", "夜勤")
        self.at("2026-10-06 06:58")
        res = self.new_page(full_page_rows("N", "05:00"))
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(start_of(self.drawn()), ":")

    def test_割り振ると1行目の開始は前のページの終わりとそろう(self) -> None:
        """「時間を割り振る」は全ページを通しで割り振る。第2ページの1行目の開始は
        第1ページの最後の終了と同じになり、描くときに入れた値とも食い違わない。"""
        from nippou import config, user_settings
        user_settings.save(config.KEY_TIME_SPLIT, True)
        self.issue_first_page()
        self.drawn()
        rows = rows_of(row("C01", "14:55", "15:00"))
        res = self.post("/api/entry/distribute-times", self.sheet(rows))
        self.assertEqual(res.status_code, 200, res.get_json())
        body = res.get_json()
        _header, details = self.repo().load("2026年10月5日", "L-1", "1直", 1)
        last = f"{details[11].sz}:{details[11].sh}"
        self.assertEqual(f"{body['rows']['1']['KZ']}:{body['rows']['1']['KH']}", last)


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class PageCarryBrowserTests(ScenarioCase):
    """本物のブラウザで「次ページ発行」を押す。"""

    @classmethod
    def setUpClass(cls) -> None:
        from tests._browser import chromium_or_skip
        cls.pw, cls.browser = chromium_or_skip()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.pw.stop()

    def setUp(self) -> None:
        super().setUp()
        self.ctx = self.browser.new_context(viewport={"width": 1400, "height": 900})
        self.addCleanup(self.ctx.close)
        self.ctx.route("http://app.test/**", self._forward)
        self.errors: list[str] = []

    def _forward(self, route) -> None:
        req = route.request
        path = req.url.split("http://app.test", 1)[1]
        headers = dict(HEADERS)
        if req.headers.get("x-tab"):
            headers["X-Tab"] = req.headers["x-tab"]
        res = self.client.open(path, method=req.method, headers=headers,
                               data=req.post_data_buffer,
                               content_type=req.headers.get("content-type"))
        try:
            route.fulfill(status=res.status_code, body=res.get_data(),
                          headers={"content-type": res.headers.get("Content-Type", "text/plain")})
        finally:
            res.close()

    def test_次ページ発行を押すと1行目の開始に前のページの終了(self) -> None:
        self.start_shift("2026-10-05 07:05", "山田")
        # 14:55 を過ぎると直の終わりの確認画面へ移るので、少し前の時刻で試す
        self.at("2026-10-05 14:05")
        # 12行(12:00〜14:00)を打って置いてある第1ページ
        res = self.save(full_page_rows("A", "12:00"), draft=True)
        self.assertEqual(res.status_code, 200, res.get_json())
        page = self.ctx.new_page()
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.on("dialog", lambda d: d.accept())
        page.goto("http://app.test/", wait_until="load")
        page.wait_for_selector("#grid-body")
        page.wait_for_timeout(600)
        self.assertEqual((page.input_value("#SZ12"), page.input_value("#SH12")), ("14", "00"))
        page.click("#new-page")
        page.wait_for_function(
            "document.getElementById('sheet-key')?.dataset.openedPage === '2'", timeout=10000)
        page.wait_for_function("document.getElementById('KZ1')?.value === '14'", timeout=10000)
        self.assertEqual((page.input_value("#KZ1"), page.input_value("#KH1")), ("14", "00"))
        self.assertEqual(page.input_value("#LOT1"), "")
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
