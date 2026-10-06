"""見るだけの過去データと、モードの終わり方

【何が起きていたか】
> 管理者モード終わった後でも1直データが開いてたらいじれるのはNG
> （過去データにはなっている）
> この直をチェック → 2026年9月16日 L-1 1直: 直すところはありません
> いやいやそもそも今2直だぞ
> 過去データの終了方法 / 管理者モードの終了方法 これらがわからない

3つとも**同じ1つの状態**から出ています ── 他の直の過去データを、
管理者モードでないまま開いている状態です。

    保存 … 前から 403 で断っていた。**打つことはできた**
    表示 … 帯には「過去データ」と出ていたが、やめる場所は別の画面の奥
    確認 … 「この直」が、いまの直ではなく開いている直を指していた

そしてこの状態は、押し間違いだけで起きるのではありません。管理者が
過去の直を開いたまま直の変わり目をまたぐと、管理者モードは自動で外れ
(`work_context.roll_over`)、呼出モードは**わざと残ります**(消すと保存先が
黙って動くため)── **誰も何も操作していないのに**この状態になります。

利用者自身の見立てはこうでした:

> 直が完了した→クリアする がないから残っちゃうのか
> 新規発行 も クリアもないからずっと残ったデータいじるしかないんだね

そのとおりで、呼出中は「次ページ発行」も止まります。**出口を出すのが
答え**で、消すことではありません。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HEADERS, WebTestCase                     # noqa: E402


class RibbonTestCase(WebTestCase):
    """帯を直に組み立てるテスト。**`get_repo()` は Flask の `g` に載って
    いる**ので、要求の外から呼ぶにはアプリの文脈が要ります。"""

    def chips(self, **kwargs):
        from nippou import work_context
        from app.routes.entry import current_calculator

        with self.app.app_context():
            ctx = work_context.get_context()
            return ctx.ribbon(current_calculator(), 1, **kwargs)["chips"]

    def clock_shift(self) -> str:
        from datetime import datetime

        from nippou import work_context
        from app.routes.entry import current_calculator

        with self.app.app_context():
            ctx = work_context.get_context()
            return current_calculator().time_check(
                datetime.now(), ctx.force_day_shift())


class ReadOnlyRecallTests(WebTestCase):
    """他の直を、管理者モードでないまま開いている。"""

    def setUp(self) -> None:
        super().setUp()
        from nippou.db.models import DetailRecord, HeaderRecord

        # **いまが 2直**になるように境界を置く(真夜中でも動くよう相対で)
        now = datetime.now()
        repo = self.repo()
        def hhmm(delta):
            return (now + timedelta(hours=delta)).strftime("%H:%M")
        repo.set_shift_time("1", hhmm(-9), hhmm(-1))
        repo.set_shift_time("2", hhmm(-1), hhmm(+7))
        repo.set_shift_time("3", hhmm(+7), hhmm(+14))
        repo.set_shift_time("昼", "08:00", "18:00")

        self.day = self.today()
        repo.save(
            HeaderRecord(report_date=self.day, line="L-1", shift="1直", page=1,
                         worker="前の人"),
            [DetailRecord(report_date=self.day, line="L-1", shift="1直",
                          page=1, row_no=1, lot="1111111", zai="SPCC")])

    def repo(self):
        from nippou.config import SETTINGS
        from nippou.db.connection import connect
        from nippou.db.repository import NippouRepository

        conn = connect(SETTINGS.sqlite_path)
        self.addCleanup(conn.close)   # 開いたままでは、Windows で一時フォルダを消せない
        return NippouRepository(conn)

    def today(self) -> str:
        from app.routes.entry import build_shift_calculator
        from nippou.services.nippou_service import format_business_date

        calc = build_shift_calculator(self.repo().get_shift_times())
        return format_business_date(calc.today_check(datetime.now(), False))

    def open_past(self, *, admin_after: bool = False) -> None:
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        res = self.post("/api/settings/recall",
                        {"report_date": self.day, "line": "L-1",
                         "shift": "1直", "page": 1})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        if not admin_after:
            self.post("/api/settings/admin", {"enable": False})

    # -- 見るだけ ------------------------------------------------------
    def test_管理者モードを抜けたら見るだけになる(self) -> None:
        """**ここが本体。** 誰も操作していなくてもこの状態になります。"""
        self.open_past()
        body = self.post("/api/entry/state", {}).get_json()
        self.assertTrue(body["read_only"])

    def test_管理者モードのあいだは直せる(self) -> None:
        """開いたのは直すためなので、鍵が開いているうちは止めません。"""
        self.open_past(admin_after=True)
        self.assertFalse(self.post("/api/entry/state", {}).get_json()["read_only"])

    def test_いまの直を開いているだけなら見るだけにしない(self) -> None:
        """同じ直のページを戻るのは、自分がさっき打った紙を直すこと。"""
        body = self.post("/api/entry/state", {}).get_json()
        self.assertFalse(body["read_only"])

    def test_見るだけでも保存は断られたまま(self) -> None:
        """**画面を触れなくするのは見た目の話。** 関門はサーバのまま。"""
        self.open_past()
        res = self.post("/api/entry/save", {})
        self.assertEqual(res.status_code, 403)

    def test_画面に帯と出口が出る(self) -> None:
        self.open_past()
        html = self.client.get("/", headers=HEADERS).get_data(as_text=True)
        start = html.index('id="read-only"')
        band = html[start:html.index("</div>", html.index("read-only-back"))]
        self.assertNotIn("hidden", html[start:start + 60])
        self.assertIn("見るだけです", band)
        self.assertIn("read-only-back", band)
        self.assertIn("管理者モードにする", band)

    def test_見るだけでないときは帯を出さない(self) -> None:
        html = self.client.get("/", headers=HEADERS).get_data(as_text=True)
        start = html.index('id="read-only"')
        self.assertIn("hidden", html[start:start + 90])

    def test_描かれた直後から触れなくする(self) -> None:
        """`paint()` を待つと、**最初の1回だけ打ててしまいます。**"""
        js = (Path(__file__).resolve().parent.parent / "app" / "static" / "js"
              / "views" / "entry.js").read_text(encoding="utf-8")
        self.assertIn(
            'applyReadOnly(!document.getElementById("read-only")?.hidden)', js)


class ClockShiftTests(RibbonTestCase):
    """「直時間超えても何も言わないね」"""

    def test_出している直と時計が違えば帯に出す(self) -> None:
        """過去データを開いているあいだ、直の変わり目の見張りは**わざと
        黙ります**(その人は分かって開いているので)。ですが「いま何直か」は
        別の話で、黙る理由がありません。"""
        clock = self.clock_shift()
        other = "2直" if clock != "2直" else "1直"
        texts = [c["text"] for c in
                 self.chips(key=("2026年9月16日", "L-1", other))]
        self.assertIn(f"いま {clock}", texts)

    def test_同じならその印は出さない(self) -> None:
        for chip in self.chips():
            self.assertNotIn("いま ", chip["text"])


class TerminalLineChipTests(RibbonTestCase):
    """**ラインを決めていない端末では、帯に L-1 と出さない**(v3.82.0)。

        結局 未設定 があっても L-1 って出てればどっちがあってかは
        わからないから駄目です　未設定なら L-1と出さないようにしてください

    v3.81.0 では帯に「L-1」を出したまま、右に「ライン未設定」の印を並べて
    いました。**どちらが本当か分からない。**
    """

    terminal_line = None          # ラインを決めていない端末

    def ribbon(self):
        from nippou import work_context
        from app.routes.entry import current_calculator

        with self.app.app_context():
            return work_context.get_context().ribbon(current_calculator(), 1)

    def test_決めていなければラインの枠は未設定(self) -> None:
        body = self.ribbon()
        self.assertEqual(body["line"], "未設定")
        self.assertEqual(body["warn"], ["line"])
        # 決まるまで打てないことは吹き出しでも言う(v4.3.0。それまでは L-1 で保存)
        self.assertIn("決まるまで日報は打てません", body["titles"]["line"])
        self.assertNotIn("L-1 として保存します", body["titles"]["line"])

    def test_印は並べない(self) -> None:
        """同じことを2か所で言うと、また「どっち」になる。"""
        self.assertNotIn("ライン未設定", [c["text"] for c in self.ribbon()["chips"]])

    def test_決めればラインが出る(self) -> None:
        from nippou import config, user_settings
        user_settings.save(config.KEY_TERMINAL_LINE, "L-1")
        body = self.ribbon()
        self.assertEqual(body["line"], "L-1")                  # 帯は正規の呼び名(v4.12.5)
        self.assertEqual(body["warn"], [])

    def test_画面の帯にもL_1と出さない(self) -> None:
        body = self.client.get("/", headers=HEADERS).get_data(as_text=True)
        slot = body[body.index('id="rb-line"'):]
        slot = slot[:slot.index("</span>")]
        self.assertIn("未設定", slot)
        self.assertNotIn(">L-1", slot)
        self.assertIn('data-warn="1"', slot)

    def test_知らせは帯と同じことを言う(self) -> None:
        from nippou import work_context
        note = work_context.terminal_line_note()
        self.assertIn("まだ決まっていません", note)
        self.assertIn("帯のラインは「未設定」", note)
        # v4.3.0 から L-1 として保存しない ── 決まるまで打てない
        self.assertIn("決まるまで日報は打てません", note)
        self.assertNotIn("L-1 として保存します", note)
        self.assertNotIn("ライン未設定", note)


class ChipExitTests(RibbonTestCase):
    """入った印を押して、そのモードを終われる。"""

    def test_過去データの印は押せる(self) -> None:
        from nippou import work_context

        work_context.get_context().recall = work_context.RecallState(
            active=True, report_date="2026年9月16日", line="L-1",
            shift="1直", page=1)
        past = next(c for c in self.chips()
                    if c["text"].startswith("過去データ"))
        self.assertEqual(past["action"], "back")
        # **どの直を開いているのかも印に出す**(「過去データ」だけでは、
        # どれを閉じることになるのか分からない)
        self.assertIn("1直", past["text"])

    def test_管理者の印は押せる(self) -> None:
        from nippou import work_context

        work_context.get_context().admin = True
        admin = next(c for c in self.chips() if c["text"] == "管理者")
        self.assertEqual(admin["action"], "admin-off")

    def test_押せない印にはactionを付けない(self) -> None:
        """「いま 2直」は状態を言うだけ。押しても終われるものがない。"""
        clock = self.clock_shift()
        other = "2直" if clock != "2直" else "1直"
        now_chip = next(c for c in
                        self.chips(key=("2026年9月16日", "L-1", other))
                        if c["text"].startswith("いま "))
        self.assertNotIn("action", now_chip)

    def test_画面が印を押す道を持っている(self) -> None:
        root = Path(__file__).resolve().parent.parent / "app" / "static" / "js"
        app_js = (root / "app.js").read_text(encoding="utf-8")
        self.assertIn("/api/settings/back", app_js)
        self.assertIn('"/api/settings/admin", { enable: false }', app_js)
        ribbon = (root / "ribbon.js").read_text(encoding="utf-8")
        self.assertIn("wireChips", ribbon)
        self.assertIn("data-chip-action", ribbon)


class VerifyShiftTests(WebTestCase):
    """「この直をチェック」が、どの直を見たのかを言う。"""

    def test_いまの直でなければそう言う(self) -> None:
        """「2026年9月16日 L-1 1直: 直すところはありません / いやいや
        そもそも今2直だぞ」。**止めはしません。添えるだけです。**"""
        from nippou import work_context

        ctx = work_context.get_context()
        ctx.recall = work_context.RecallState(
            active=True, report_date="2020年1月1日", line="L-1",
            shift="1直", page=1)
        body = self.post("/api/entry/verify", {}).get_json()
        self.assertIn("other_shift", body)
        self.assertIn("開いている過去データ", body["message"])
        self.assertIn("いまは", body["message"])

    def test_いまの直なら何も添えない(self) -> None:
        body = self.post("/api/entry/verify", {}).get_json()
        self.assertNotIn("other_shift", body)
        self.assertNotIn("開いている過去データ", body["message"])
