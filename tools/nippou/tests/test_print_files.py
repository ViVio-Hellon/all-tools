"""印刷用HTMLを出力先へ残す ── 「印刷」を押したとき・「共有へ保存」のとき (v4.39.0)

【利用者から】

    共有へ保存実行時に一緒に残すようにしてください
    「印刷」を押したときにも出力先へ HTML を保存するようにする

前は出力先へ書くのは「当直をDBから復旧」(管理者)だけで、「印刷」は別窓に出すだけでした。
置き場所は集計CSVと同じ並べ方 `<出力先>\\<ライン>\\印刷\\<yyyy.mm>\\<dd>\\印刷_…_<ページ>.html`。
同じ直・同じページは上書き(残るのは最新)。書けなくても印刷・共有への保存は止めません。
記録を見るの「紙を見る」・紙の札は見るだけなので残しません。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402
from tests.test_print_buttons import _page  # noqa: E402


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class PrintSaveRouteTests(WebTestCase):
    def setUp(self) -> None:
        super().setUp()
        from nippou import config, user_settings
        self.out = self.tmp / "出力"
        user_settings.save_many({config.KEY_REPORT_OUT_DIR: str(self.out)})
        for n in (1, 2):
            self.repo().save(*_page(n, lot=f"LOT{n}"))

    def paper(self, page: str, *, save: bool):
        q = f"/report/nippou?report_date=2026年8月3日&line=L-1&shift=1直&page={page}"
        return self.get(q + ("&save=1" if save else ""))

    def files(self) -> list[Path]:
        return sorted(self.out.rglob("*.html")) if self.out.exists() else []

    def test_印刷を押すと直の全ページを残す(self) -> None:
        res = self.paper("all", save=True)
        self.assertEqual(res.status_code, 200)
        files = self.files()
        self.assertEqual([f.name for f in files],
                         ["印刷_2026年8月3日_L-1_1直_1.html", "印刷_2026年8月3日_L-1_1直_2.html"])
        self.assertEqual(files[0].parent, self.out / "L-1" / "印刷" / "2026.08" / "03")
        self.assertIn("LOT1", files[0].read_text(encoding="utf-8"))
        self.assertIn("LOT2", files[1].read_text(encoding="utf-8"))
        text = res.get_data(as_text=True)
        self.assertIn("印刷用HTMLを 2ページぶん残しました", text, "どこへ残したかを窓に出す")
        self.assertNotIn("残しました", files[0].read_text(encoding="utf-8"), "一言は残す紙には入れない")

    def test_1ページだけ開いたときはそのページ(self) -> None:
        self.paper("2", save=True)
        self.assertEqual([f.name for f in self.files()], ["印刷_2026年8月3日_L-1_1直_2.html"])

    def test_見るだけなら残さない(self) -> None:
        """記録を見るの「紙を見る」・紙の札(`save` を付けない)。"""
        text = self.paper("all", save=False).get_data(as_text=True)
        self.assertEqual(self.files(), [])
        self.assertNotIn("残しました", text)

    def test_もう一度押すと上書きして最新の紙(self) -> None:
        self.paper("all", save=True)
        from nippou.db.models import DetailRecord as D, HeaderRecord as H
        header, _details = _page(1, lot="LOT1")
        self.repo().save(header, [D(report_date=header.report_date, line=header.line, shift=header.shift,
                                    page=1, row_no=1, lot="NEWLOT")])
        self.paper("all", save=True)
        self.assertEqual(len(self.files()), 2)
        self.assertIn("NEWLOT", self.files()[0].read_text(encoding="utf-8"))

    def test_書けなくても紙は出る(self) -> None:
        from nippou import config, user_settings
        blocker = self.tmp / "ファイル"
        blocker.write_text("x", encoding="utf-8")       # フォルダを作れない場所
        user_settings.save_many({config.KEY_REPORT_OUT_DIR: str(blocker / "下")})
        res = self.paper("all", save=True)
        self.assertEqual(res.status_code, 200)
        text = res.get_data(as_text=True)
        self.assertIn("LOT1", text)
        self.assertIn("印刷用HTMLを出力先へ残せませんでした", text)

    def test_日報入力の印刷ボタンは残すと頼む(self) -> None:
        js = (Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "views" / "entry.js"
              ).read_text(encoding="utf-8")
        start = js.index('getElementById("print-page")')
        self.assertIn('save: "1"', js[start:start + 600])
        records = (Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "views" / "records.js"
                   ).read_text(encoding="utf-8")
        self.assertNotIn("save:", records, "記録を見るは見るだけ")


try:
    from tests import test_scenario_break_overrun as overrun
except Exception:                                       # noqa: BLE001 - 無い環境
    overrun = None


if overrun is not None:
    @unittest.skipUnless(HAS_FLASK, SKIP_REASON)
    class PushSavesPaperTests(overrun.BreakOverrunScenarioTests):
        """「共有へ保存」で、送った直の紙が出力先に残る(本物の共有への保存を通す)。"""

        def test_共有へ保存すると送った直の紙が残る(self) -> None:
            plan = self.plan("1直")
            plan.add(overrun.NORMAL, 0, rest="time")
            plan.fill()
            rows = self.run_shift("2026-10-05", "1直", plan, first=True)
            self.assertEqual(self.save(rows, overrun.WORKER).status_code, 200)
            out = self.tmp / "出力"
            self.assertEqual(sorted(p.name for p in out.rglob("印刷_*.html")), [], "保存(確定)では残さない")
            res = self.push_end("2026-10-05", "1直")
            self.assertEqual(res.status_code, 200, res.get_json())
            files = sorted(out.rglob("印刷_*.html"))
            self.assertEqual([f.name for f in files], ["印刷_2026年10月5日_" + files[0].name.split("_")[2]
                                                       + "_1直_1.html"])
            self.assertEqual(files[0].parent.parts[-3:], ("印刷", "2026.10", "05"))
            self.assertIn(rows["1"]["LOT"], files[0].read_text(encoding="utf-8"))
            self.assertIn("印刷用HTML: 1ページを残しました", res.get_json()["message"])

        def test_送れなかったときは残さない(self) -> None:
            plan = self.plan("1直")
            plan.add(overrun.OVER, 0)                    # 大きく超過・休憩なし → 断られる
            plan.fill()
            rows = self.run_shift("2026-10-05", "1直", plan, first=True)
            self.save(rows, overrun.WORKER)
            res = self.push_end("2026-10-05", "1直")
            self.assertEqual(res.status_code, 422)
            self.assertEqual(sorted((self.tmp / "出力").rglob("印刷_*.html")), [])

    for _name in [n for n in dir(overrun.BreakOverrunScenarioTests) if n.startswith("test_")]:
        setattr(PushSavesPaperTests, _name, None)


if __name__ == "__main__":
    unittest.main()
