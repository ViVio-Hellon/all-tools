"""記録を見る「保存した直」で Administrator はラインを選ぶ (v4.35.3)

    記録を見る:保存した直:Administrator であれば全ラインが見れるが全ライン表示している見づらい。
    ほかのPCの日報のようにライン選択させてください

【約束】
    ・欄は「ほかのPCの日報」と同じ形(ライン + 表示)。Administrator・管理者モードだけ
    ・既定はこの端末のライン。選んだラインの直だけ(混ぜない。「全ライン」は無い)
    ・選んだラインはサーバが覚える(`?line=` の無い `/records` でもそのラインのまま)
    ・ほかの PC は欄が出ず、名指ししてもこの端末のラインの直だけ
    ・赤くする・後日作成の印・紙・並べ替えは、選んだラインでも同じに効く
    ・ほかのラインの直の「呼び出す」は控えから入れて開く(ロットを探すと同じ決まり)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou import config  # noqa: E402
from nippou.db.models import DetailRecord, HeaderRecord  # noqa: E402
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402

DAY = "2026年9月15日"


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class SavedLineCase(WebTestCase):
    """下ごしらえ: この端末(L-1)の直と、手元にある HVC の直。"""

    def setUp(self) -> None:
        super().setUp()
        from nippou import user_settings
        from nippou.services import local_backup as lb
        # 控えの置き場所は一時フォルダの中(本物の共有を読まない・書かない)
        self.share = self.tmp / "共有"
        self.share.mkdir()
        user_settings.save_many({config.KEY_ACCESS_DIR: str(self.share)})
        lb.reset()
        self.addCleanup(lb.reset)
        self.lb = lb
        # この端末(L-1)の直と、控えから入れた・取り込んだ HVC の直(管理者の PC にはある)
        self.save(DAY, "L-1", "1直", lot="OWNLOT1")
        self.save(DAY, "HVC", "1直", lot="HVCLOT1")

    def save(self, report_date: str, line: str, shift: str, *, lot: str = "A1",
             page: int = 1, synced: bool = True, mai: str = "5", tut: str = "1",
             saved_at: str = "") -> None:
        key = dict(report_date=report_date, line=line, shift=shift, page=page)
        repo = self.repo()
        repo.save(HeaderRecord(**key, worker="山田"),
                  [DetailRecord(**key, row_no=1, lot=lot, ken="15", mai=mai, tut=tut,
                                kz="07", kh="00", sz="15", sh="00", s="0", th="60")])
        if synced:
            repo.mark_synced((report_date, line, shift, page))
        if saved_at:
            repo.conn.execute(
                "UPDATE daily_header SET saved_at=? WHERE report_date=?"
                " AND line=? AND shift=? AND page=?",
                (saved_at, report_date, line, shift, page))
            repo.conn.commit()

    def administrator(self) -> None:
        from nippou.services import access_rights
        patcher = patch.object(access_rights, "is_administrator", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def html(self, query: str = "") -> str:
        res = self.get(f"/records{query}")
        self.assertEqual(res.status_code, 200)
        return res.get_data(as_text=True)

    @staticmethod
    def table(html: str) -> str:
        """「保存した直」の表だけ(ほかのカードの欄・ボタンと取り違えない)。"""
        at = html.index('id="records-table"')
        start = html.rindex("<table", 0, at)
        return html[start:html.index("</table>", at)]

    @staticmethod
    def picker(html: str) -> str:
        start = html.index('id="saved-line"')
        return html[start:html.index("</select>", start)]

    @staticmethod
    def row_of(table: str, line: str) -> str:
        start = table.index(f'data-line="{line}"')
        return table[table.rindex("<tr", 0, start):table.index("</tr>", start)]



class SavedLineTests(SavedLineCase):
    # -- 欄 ---------------------------------------------------------------
    def test_一般の端末には欄が出ず_この端末のラインの直だけ(self) -> None:
        html = self.html()
        self.assertNotIn('id="saved-line"', html)
        self.assertNotIn('id="saved-reload"', html)
        table = self.table(html)
        self.assertIn('data-line="L-1"', table)
        self.assertNotIn('data-line="HVC"', table)
        # 名指ししても、この端末のライン
        table = self.table(self.html("?line=HVC"))
        self.assertIn('data-line="L-1"', table)
        self.assertNotIn('data-line="HVC"', table)

    def test_Administratorには欄が出て_既定はこの端末のライン(self) -> None:
        self.administrator()
        html = self.html()
        # 「ほかのPCの日報」と同じ形: ライン + 表示。「保存した直」の表の上
        self.assertIn('<label for="saved-line">ライン</label>', html)
        self.assertIn('id="saved-reload">表示</button>', html)
        self.assertLess(html.index("<h2>保存した直</h2>"), html.index('id="saved-line"'))
        self.assertLess(html.index('id="saved-line"'), html.index('id="records-table"'))
        picker = self.picker(html)
        self.assertIn('<option value="L-1" selected>', picker)
        self.assertIn('<option value="HVC"', picker)
        self.assertNotIn("全ライン", picker)                 # 混ぜない(ほかのPCの日報と同じ)
        table = self.table(html)
        self.assertIn('data-line="L-1"', table)
        self.assertNotIn('data-line="HVC"', table)

    def test_管理者モードにも欄が出る(self) -> None:
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        self.assertIn('id="saved-line"', self.html())

    def test_選べるのは手元に保存のあるラインとこの端末のライン(self) -> None:
        self.administrator()
        picker = self.picker(self.html())
        self.assertIn('value="L-1"', picker)
        self.assertIn('value="HVC"', picker)
        self.assertNotIn('value="AIM"', picker)              # 保存の無いラインは出さない
        # 並びはラインの表の順(L-1 が HVC より前)
        self.assertLess(picker.index('value="L-1"'), picker.index('value="HVC"'))

    # -- 選んだラインだけ ---------------------------------------------------
    def test_Administratorは選んだラインの直だけを見る_混ぜない(self) -> None:
        self.administrator()
        html = self.html("?line=HVC")
        self.assertIn('<option value="HVC" selected>', self.picker(html))
        table = self.table(html)
        self.assertIn('data-line="HVC"', table)
        self.assertNotIn('data-line="L-1"', table)
        # この端末のラインへ戻せば、この端末の直だけ
        table = self.table(self.html("?line=L-1"))
        self.assertIn('data-line="L-1"', table)
        self.assertNotIn('data-line="HVC"', table)

    def test_選んだラインを覚える(self) -> None:
        self.administrator()
        self.html("?line=HVC")
        # 日報入力へ行って戻る(`?line=` の無い /records)── 選び直させない
        self.get("/")
        html = self.html()
        self.assertIn('<option value="HVC" selected>', self.picker(html))
        self.assertIn('data-line="HVC"', self.table(html))
        # 選べないラインを名指しされても、覚えたラインのまま
        html = self.html("?line=AIM")
        self.assertIn('<option value="HVC" selected>', self.picker(html))

    def test_権限が無くなれば覚えたラインは使わない(self) -> None:
        from nippou.services import access_rights
        with patch.object(access_rights, "is_administrator", return_value=True):
            self.html("?line=HVC")
        html = self.html()
        self.assertNotIn('id="saved-line"', html)
        self.assertNotIn('data-line="HVC"', self.table(html))

    def test_ほかのラインの保存が多くても選んだラインの直は溢れない(self) -> None:
        """「済」の20直は、選んだラインの中で数える。"""
        self.administrator()
        for day in range(1, 26):                          # この端末の済を25直(新しい)
            self.save(f"2026年10月{day}日", "L-1", "1直",
                      saved_at=f"2026-10-{day:02d}T07:00:00")
        table = self.table(self.html("?line=HVC"))
        self.assertIn('data-line="HVC"', table)
        self.assertEqual(table.count("data-open-all"), 1)

    def test_保存の無くなったラインは選べず_この端末のラインに戻る(self) -> None:
        self.administrator()
        self.html("?line=HVC")
        repo = self.repo()
        repo.delete_page(DAY, "HVC", "1直", 1)
        html = self.html()
        self.assertNotIn('value="HVC"', self.picker(html))
        self.assertIn('<option value="L-1" selected>', self.picker(html))
        self.assertIn('data-line="L-1"', self.table(html))

    def test_この端末のラインが空ならそう言う(self) -> None:
        self.administrator()
        self.repo().delete_page(DAY, "L-1", "1直", 1)
        html = self.html()
        self.assertIn('<option value="L-1" selected>', self.picker(html))   # 保存が無くても選べる
        self.assertIn("L-1 の保存データがありません。", self.table(html))

    def test_ほかのラインの未送信は欄に数を出す(self) -> None:
        """一覧は1ラインずつ。**未送信は古くても必ず見える**を、欄の字で守る。"""
        self.administrator()
        self.save("2026年8月1日", "HVC", "1直", synced=False)
        html = self.html()
        self.assertIn("HVC(未送信 1直)", self.picker(html))
        self.assertIn('id="saved-unsent"', html)
        self.assertIn("ほかのラインにも共有へ未送信の直があります", html)
        # そのラインを選べば、一覧の上に出る(未送信が先)
        html = self.html("?line=HVC")
        self.assertNotIn('id="saved-unsent"', html)
        table = self.table(html)
        first = table.index("2026年8月1日")
        self.assertLess(first, table.index(DAY))

    # -- 赤くする・後日作成の印・紙 -------------------------------------------
    def test_選んだラインでも直すところがある直は赤い(self) -> None:
        self.administrator()
        # 梱包数 20 が検入枚数 15 を超えている(入力画面でも赤くなる間違い)
        self.save("2026年9月16日", "HVC", "1直", mai="10", tut="2")
        self.save("2026年9月16日", "L-1", "1直", mai="10", tut="2")
        html = self.html("?line=HVC")
        table = self.table(html)
        start = table.index(">2026年9月16日</td>")
        bad = table[table.rindex("<tr", 0, start):table.index("</tr>", start)]
        self.assertIn('class="row-bad"', bad)
        self.assertIn("検入枚数 15 を超えています", bad)
        # 数は選んだラインのぶんだけ(L-1 の赤い直は数えない)
        self.assertIn("直すところがある直が 1直 あります", html)

    def test_選んだラインでも後日作成の印が出る(self) -> None:
        self.administrator()
        self.repo().record_backfill(DAY, "HVC", "1直", 1, note="打ち忘れ")
        html = self.html("?line=HVC")
        self.assertIn("後日作成</span>", self.row_of(self.table(html), "HVC"))
        # この端末のラインの一覧には出ない(HVC の印を混ぜない)
        self.assertNotIn("後日作成</span>", self.table(self.html("?line=L-1")))

    def test_選んだラインの紙を見る(self) -> None:
        self.administrator()
        row = self.row_of(self.table(self.html("?line=HVC")), "HVC")
        self.assertIn("data-open-all", row)
        paper = self.get(f"/report/nippou?report_date={DAY}&line=HVC&shift=1直&page=all")
        self.assertEqual(paper.status_code, 200)
        self.assertIn("HVCLOT1", paper.get_data(as_text=True))

    def test_選んだラインでも見出しで並べ替えられる(self) -> None:
        self.administrator()
        table = self.table(self.html("?line=HVC"))
        self.assertIn("list--sortable", table)
        self.assertIn('data-sort="date"', table)

    # -- 呼び出すの行き先 ---------------------------------------------------
    def test_ほかのラインの行は控えから入れて開く(self) -> None:
        self.administrator()
        own = self.row_of(self.table(self.html("?line=L-1")), "L-1")
        self.assertIn('data-recall-path="/api/settings/recall"', own)
        other = self.row_of(self.table(self.html("?line=HVC")), "HVC")
        self.assertIn('data-recall-path="/api/records/backup/open"', other)
        self.assertIn("控えからこのPCへ入れて", other)
        self.assertNotIn("disabled", other)

    def test_ほかのラインの行の行き先で開ける(self) -> None:
        """控えの道で開く。手元のほうが新しくなければ控えのページで、同じなら手元のまま。"""
        from nippou import work_context
        self.administrator()
        # HVC の PC が保存して控えへ写した(管理者の手元にあるのと同じページ)
        self.lb.flush(self.repo(), force=True)
        body = self.post("/api/records/backup/open", {
            "report_date": DAY, "line": "HVC", "shift": "1直", "page": 1}).get_json()
        self.assertEqual(body.get("next"), "/", body)
        ctx = work_context.get_context()
        self.assertTrue(ctx.recall.active)
        self.assertEqual((ctx.recall.line, ctx.line, ctx.terminal_line), ("HVC", "HVC", "L-1"))

    def test_一般の端末の行はこれまでどおり手元から(self) -> None:
        row = self.row_of(self.table(self.html()), "L-1")
        self.assertIn('data-recall-path="/api/settings/recall"', row)

    def test_画面は行き先と欄をサーバに聞く(self) -> None:
        js = (Path(__file__).resolve().parent.parent
              / "app/static/js/views/records.js").read_text(encoding="utf-8")
        self.assertIn("btn.dataset.recallPath", js)
        self.assertIn('getElementById("saved-line")', js)
        self.assertIn("/records?line=", js)


class SavedLineBrowserTests(SavedLineCase):
    """欄を選び直すと一覧が描き直され、ほかのラインの「呼び出す」が控えの道で開く(本物のブラウザで)。

    配線は `views/records.js` の中で、Python の試験では壊れても分からないので。
    """

    @classmethod
    def setUpClass(cls) -> None:
        from tests._browser import chromium_or_skip
        cls.pw, cls.browser = chromium_or_skip()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.pw.stop()

    def open_records(self):
        from tests.test_seq_entry import BrowserTests
        # 直の実績の確認(`shift_end.js`)へ連れて行かれないように、先に確認を済ませる
        for _ in range(3):
            state = self.get("/api/graph/review").get_json()
            if not state.get("due"):
                break
            self.post("/api/graph/review/ack", {"stage": state.get("stage", "")})
        ctx = self.browser.new_context(viewport={"width": 1400, "height": 900})
        self.addCleanup(ctx.close)
        ctx.route("http://app.test/**", lambda route: BrowserTests._forward(self, route))
        page = ctx.new_page()
        self.errors: list[str] = []
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.on("dialog", lambda d: d.accept())
        page.goto("http://app.test/records", wait_until="load")
        page.wait_for_selector("#saved-line")
        page.wait_for_timeout(400)
        return page

    def test_ラインを選ぶとそのラインの直だけになり_控えの道で呼び出す(self) -> None:
        from nippou import work_context
        self.administrator()
        self.lb.flush(self.repo(), force=True)            # HVC の控えもある
        page = self.open_records()
        rows = "#records-table tbody [data-open-all]"
        self.assertEqual(page.eval_on_selector_all(rows, "bs => bs.map(b => b.dataset.line)"),
                         ["L-1"])
        page.select_option("#saved-line", "HVC")
        page.wait_for_function(
            "document.querySelector('#records-table tbody [data-open-all]')?.dataset.line === 'HVC'",
            timeout=5000)
        self.assertEqual(page.eval_on_selector_all(rows, "bs => bs.map(b => b.dataset.line)"),
                         ["HVC"])
        self.assertIn("/records?line=HVC", page.url)
        self.assertEqual(page.eval_on_selector("#saved-line", "s => s.value"), "HVC")
        # 選び直したあとも欄は効く(差し替えた画面で繋ぎ直されている)── 戻して、また選ぶ
        page.select_option("#saved-line", "L-1")
        page.wait_for_function(
            "document.querySelector('#records-table tbody [data-open-all]')?.dataset.line === 'L-1'",
            timeout=5000)
        page.select_option("#saved-line", "HVC")
        page.wait_for_function(
            "document.querySelector('#records-table tbody [data-open-all]')?.dataset.line === 'HVC'",
            timeout=5000)
        # ほかのラインの行の「呼び出す」は控えから入れて開く → 日報入力へ
        page.click("#records-table tbody [data-recall]")
        page.wait_for_function("location.pathname === '/'", timeout=5000)
        ctx = work_context.get_context()
        self.assertTrue(ctx.recall.active)
        self.assertEqual((ctx.recall.line, ctx.terminal_line), ("HVC", "L-1"))
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
