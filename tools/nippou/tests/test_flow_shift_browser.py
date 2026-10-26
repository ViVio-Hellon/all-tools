"""通しの流れ: **1台で1直を打ち切るまで**の入力画面(本物のブラウザ。現場の人が押す順)

    日報周りの一連動作チェックお願いします

1つの部品ではなく、**押す順につないで**確かめます。部品ごとの細かい決まりは、それぞれの
試験(`test_seq_entry`・`test_lot_carry_browser`・`test_break_popup_browser`・
`test_time_split_lot_browser`・`test_push_after_review_browser` など)にあります。

    1. 起動 → 作業者を選ぶ(選ぶ前は表も「全停入力」などの紙を書くボタンも押せない)→ 帯が進む
    2. 行を打つ: ロット№を引く・枚数/包数・停止①(記号と分)・休憩(時刻を聞く)・その他(理由を聞く)
       ・開始/終了のダブルクリック(終了は次の行の開始へ写る)・行番号のダブルクリックで順に打つ
       ・前の直から続くロットは検入数を訊く・打ちかけは自動保存で手元に残る
    3. 時間を割り振る・LOTで時間を割り振る(知らせは1回)
    4. 保存(確定)・この直をチェック・12行で次ページ発行(1行目の開始に前の終了)
       ・前のページを見る(打ちかけを置いてから)→ 最新に戻る
    5. 共有へ保存 → グラフが画面いっぱい → 「確認しました」→ 入力画面の「保存済み」→ 直すと
       「保存したあとに直した行があります」→ 押し直す
    6. 断る場面: 梱包数(直して保存すれば断りの欄は消える)・最終時間(「設備移動のため保存」で
       通せば、確認画面は「渡しました」と言う)・共有へ届かない・LOT の重複(自動保存の一言に理由)
    7. 印刷(この直の全ページを別窓)・梱包資材重量計算へ行って戻っても打ちかけが残る

どの試験も、画面の例外(`pageerror`)とコンソールのエラー(`console.error`。断った答えの
「Failed to load resource」は除く)が出ていないことも見ます。

**時間では待ちません**(Windows の CI は遅い)。欄の値・画面の道・送った要求を待ちます。
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._other_screen_browser import ScreenBrowserCase  # noqa: E402
from tests.test_entry_shell_browser import INIT_SCRIPT  # noqa: E402
from tests.test_scenario_shift_flow import SHIFT_TIMES, SKIP, _Now  # noqa: E402
from tests.test_web_entry_lot import HIKI_COLUMNS, LOT_COLUMNS, ODR_COLUMNS  # noqa: E402

#: ふつうのロット(検入 9 枚・合紙あり・VC は表裏 VE-20NF)
LOT_A = "H5422S0"
#: 輸出のロット(検入 30 枚・合紙なし・VC が違う)── 引くと etc に「EX」が自動で付く
LOT_EX = "H5423S0"

PUSH = "/api/settings/push"
SAVED = "この直は共有へ保存済み"
STALE = "共有へ保存したあとに直した行があります"

#: 欄の値をまとめて読む(行の `data-family` の欄ぜんぶ。空は除く)
ROW_JS = """(n) => Object.fromEntries(
  [...document.querySelectorAll(`[data-row="${n}"][data-family]`)]
    .filter((el) => el.value).map((el) => [el.dataset.family, el.value]))"""


def write_wip(path: Path, columns, rows) -> None:
    conn = sqlite3.connect(str(path))
    try:
        with conn:
            cols = ", ".join(f'"{c}"' for c in columns)
            marks = ", ".join("?" for _ in columns)
            conn.execute(f'CREATE TABLE "仕掛" ({cols})')
            conn.executemany(f'INSERT INTO "仕掛" VALUES ({marks})', rows)
    finally:
        conn.close()


def write_stop_master(path: Path) -> None:
    """停止の記号のマスタ: 休憩(0)・人員不足(2)・その他(ヨ)/ 突発(イ)/ 段取り(A)。"""
    conn = sqlite3.connect(str(path))
    try:
        with conn:
            for table, rows in (
                ("作業停止時間内訳_1", [("1", "休憩食事", "0"), ("2", "人員不足", "2"),
                                        ("3", "その他（理由を記載）", "ヨ")]),
                ("作業停止時間内訳_2", [("1", "突発停止(機械)", "イ")]),
                ("作業停止時間内訳_3", [("1", "段取り変更", "A")]),
            ):
                conn.execute(f'CREATE TABLE "{table}" ("管理番号", "内訳", "内訳番号", "備考")')
                conn.executemany(f'INSERT INTO "{table}" VALUES (?, ?, ?, "")', rows)
    finally:
        conn.close()


class FlowCase(ScreenBrowserCase):
    """1直(07:00〜15:00)の 07:05。仕掛ファイル・停止マスタ・時間マスタ・共有のフォルダ・名簿を置く。"""

    def setUp(self) -> None:
        super().setUp()
        from nippou import config, user_settings
        from nippou.logic import shift_review
        from nippou.logic.staff import StaffMember
        from nippou.services import shift_close

        self.share = self.tmp / "共有"
        self.share.mkdir()
        ref = self.tmp / "ref"
        ref.mkdir(exist_ok=True)
        write_stop_master(ref / "伝送用ファイル.sqlite3")
        write_wip(ref / "SIKALOT.sqlite3", LOT_COLUMNS, [
            (LOT_A, "AB2234", "K1", "52S", "R", 20.0, 1528.0, 3053.0, "H283", "JIS", "",
             "取引先", "納入", "送り先", "9", "0", "C1"),
            (LOT_EX, "AB1234", "K2", "52S", "R", 20.0, 1528.0, 3053.0, "H283", "JIS", "",
             "取引先", "納入", "送り先", "30", "0", "C1"),
        ])
        write_wip(ref / "SIKAHIKI.sqlite3", HIKI_COLUMNS, [
            (LOT_A, "AB2234", 100, "", "60717001"), (LOT_EX, "AB1234", 100, "", "60717002")])
        write_wip(ref / "SIKAODR.sqlite3", ODR_COLUMNS, [
            ("AB2234", "VE-20NF", "VE-20NF", "1", 250.04314, "取引先", "納入先", "送り先", "1P1186", ""),
            ("AB1234", "V325NW", "", "0", 1626.87679, "取引先", "納入先", "送り先", "1P0001", "1")])
        user_settings.save_many({config.KEY_ACCESS_DIR: str(self.share),
                                 config.KEY_REPORT_OUT_DIR: str(self.tmp / "出力"),
                                 config.KEY_TIME_SPLIT: True})
        res = self.post("/api/settings/paths", {"gw_reference_dir": str(ref), "password": "nisk"})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.repo().replace_shift_times(SHIFT_TIMES)
        shift_review.reset()
        shift_close.reset()
        self.addCleanup(shift_review.reset)
        self.addCleanup(shift_close.reset)
        roster = [StaffMember("山田", "A", "やまだ"), StaffMember("青木", "A", "あおき")]
        patcher = patch("app.routes.staff.staff_master.load_staff_members", return_value=roster)
        patcher.start()
        self.addCleanup(patcher.stop)
        # 1分ごとの見張り(`shift_end.js` の tick)を掴んでおく(勝手に回さない ── 回すのは試験)
        self.ctx.add_init_script(INIT_SCRIPT)
        self.console: list[str] = []
        self.ctx.on("console", self._console)
        self.at("07:05")

    def _console(self, msg) -> None:
        # 断った答え(422 など)はブラウザが「Failed to load resource」と書く。それは断りで、壊れではない
        if msg.type == "error" and "Failed to load resource" not in msg.text:
            self.console.append(msg.text)

    def assert_clean(self) -> None:
        self.assertEqual(self.errors, [], "画面で例外が出ました")
        self.assertEqual(self.console, [], "コンソールにエラーが出ました")

    # -- 時計 -----------------------------------------------------------
    @staticmethod
    def at(hhmm: str, day: str = "2026-10-05") -> None:
        _Now.value = datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M")

    # -- 画面 -----------------------------------------------------------
    def boot(self, *, pick: bool = True):
        """入口を開いて起動しきるまで。`pick` なら「作業者を選ぶ」で 山田 を選ぶ。"""
        page = self.watch(self.ctx.new_page())
        page.goto("http://app.test/", wait_until="load")
        page.wait_for_function("window.APP && window.APP.booted === true", timeout=10000)
        page.wait_for_selector("#grid-body")
        if pick:
            self.pick_worker(page)
        return page

    def pick_worker(self, page, name: str = "山田") -> None:
        page.click("#start-pick-staff")
        page.wait_for_selector("#staff-modal:not([hidden]) [data-staff]", timeout=8000)
        page.check(f'[data-staff][value="{name}"]')
        page.click("#staff-apply")
        page.wait_for_function("!document.body.classList.contains('needs-worker')", timeout=8000)
        page.wait_for_function("!document.getElementById('LOT1').disabled", timeout=8000)

    def row(self, page, n: int) -> dict:
        return page.evaluate(ROW_JS, n)

    def toasts(self, page) -> list[str]:
        return page.evaluate(
            "[...document.querySelectorAll('.toast__text')].map((t) => t.textContent)")

    def wait_value(self, page, field: str, value: str, timeout: int = 10000) -> None:
        page.wait_for_function("([id, v]) => document.getElementById(id)?.value === v",
                               arg=[field, value], timeout=timeout)

    def wait_filled(self, page, field: str, timeout: int = 10000) -> None:
        page.wait_for_function("(id) => (document.getElementById(id)?.value || '') !== ''",
                               arg=field, timeout=timeout)

    def type_lot(self, page, n: int, lot: str) -> None:
        """ロット№を人と同じに打つ(7桁そろった時点で引く)。材・調質が入るまで待つ。"""
        self.type_in(page, f"LOT{n}", lot)
        self.wait_filled(page, f"ZAI{n}")

    def fill(self, page, n: int, **values: str) -> None:
        """欄へ値を入れる(打った合図つき)。**離れはしない** ── 離れるのは `settle`。"""
        page.evaluate("""([n, values]) => {
          for (const [family, value] of Object.entries(values)) {
            const el = document.getElementById(family + n);
            el.value = value;
            el.dispatchEvent(new Event('input', {bubbles: true}));
          }
        }""", [n, values])

    def settle(self, page, n: int, field: str = "MAI") -> None:
        """欄を離れた合図を出し、サーバの答え(`/api/entry/state`)を受け取るまで待つ。"""
        with page.expect_response(lambda r: r.url.endswith("/api/entry/state")):
            page.dispatch_event(f"#{field}{n}", "blur")

    def stamp(self, page, field: str) -> None:
        """空の開始/終了の時をダブルクリック ── いまの時刻が時と分に入るまで待つ。

        欄そのものへ合図を送る(マウスで位置を決めると、押すまでに表が動いて別の行に当たる
        ことがある ── `test_input_shortcuts_browser` と同じ)。
        """
        minute = {"KZ": "KH", "SZ": "SH"}[field[:2]] + field[2:]
        page.dispatch_event(f"#{field}", "dblclick")
        self.wait_filled(page, minute)

    def click_save(self, page) -> int:
        """「保存(確定)」を押し、その答えの番号を返す(自動保存とは見分ける)。"""
        with page.expect_response(
                lambda r: r.url.endswith("/api/entry/save")
                and '"silent"' not in (r.request.post_data or "")
                and '"draft"' not in (r.request.post_data or "")) as res:
            page.click("#save")
        return res.value.status

    def saved_row(self, n: int, page_no: int = 1) -> dict:
        """手元の DB(この端末)に書けている、いまの直の `n` 行目(無ければ空)。"""
        if getattr(self, "_reader", None) is None:
            self._reader = self.repo()                    # 1本だけ開いて使い回す(待つあいだ何度も読む)
        loaded = self._reader.load("2026年10月5日", "L-1", "1直", page_no)
        if not loaded:
            return {}
        return next((vars(d) for d in loaded[1] if d.row_no == n), {})

    def push_count(self) -> int:
        return sum(1 for p, _ in self.sent if p.split("?")[0] == PUSH)

    def mark(self, page) -> dict:
        return page.evaluate("""() => {
          const el = document.getElementById('push-mark');
          return el ? {text: el.textContent.trim(), hidden: el.hidden, state: el.dataset.state} : null;
        }""")

    def wait_mark(self, page, state: str, text: str) -> None:
        page.wait_for_function(
            """([state, text]) => {
              const el = document.getElementById('push-mark');
              return el && !el.hidden && el.dataset.state === state && el.textContent.includes(text);
            }""", arg=[state, text], timeout=15000)

    def to_review(self, page) -> None:
        """送れたら、送った直の実績が画面いっぱいに出る(グラフが描けるまで)。"""
        page.wait_for_function(
            "location.pathname === '/graph/review'"
            " && document.getElementById('review')?.dataset.after === 'push'", timeout=20000)
        page.wait_for_selector("#review-charts svg", timeout=15000)

    def back_to_entry(self, page) -> None:
        page.click("#review-act")
        page.wait_for_function("location.pathname === '/'", timeout=15000)
        page.wait_for_selector("#grid-body")
        page.wait_for_function("window.APP && window.APP.booted === true", timeout=10000)

    def type_clean_shift(self, page, *, end: str = "15:00") -> None:
        """1直を綺麗に打つ: 07:00〜10:00 LOT_A / 10:00〜11:00 休憩60分 / 11:00〜`end` LOT_EX。"""
        self.type_lot(page, 1, LOT_A)
        self.fill(page, 1, KZ="07", KH="00", SZ="10", SH="00", MAI="3", TUT="1")
        self.fill(page, 2, KZ="10", KH="00", SZ="11", SH="00", S="0", TH="60",
                  break_range="10:00〜11:00")
        self.type_lot(page, 3, LOT_EX)
        self.fill(page, 3, KZ="11", KH="00", SZ=end[:2], SH=end[3:], MAI="5", TUT="2")
        self.settle(page, 3)


# ======================================================================
# 1. 起動 → 作業者を選ぶ
# ======================================================================
class StartTests(FlowCase):
    def test_作業者を選ぶまでは表も紙を書くボタンも押せず_選ぶと帯が進む(self) -> None:
        page = self.boot(pick=False)
        self.assertTrue(page.evaluate("document.body.classList.contains('needs-worker')"))
        self.assertTrue(page.is_visible("#start-shift"))
        self.assertIn("まず作業者を選んでください", page.inner_text("#start-shift"))
        # 表の欄も、押した時点で1ページを書くボタンも押せない(理由は札に出す)
        self.assertTrue(page.is_disabled("#LOT1"))
        for button in ("#open-formstop", "#new-page", "#time-split", "#time-split-lot"):
            self.assertTrue(page.is_disabled(button), button)
            self.assertIn("作業者を選ぶ", page.get_attribute(button, "title") or "", button)
        # 打つ前の帯は「作業者」が次
        self.assertEqual(page.get_attribute('#steps [data-step="worker"]', "data-current"), "1")

        # 何も打たずに「保存(確定)」── 何が足りないかを言って止まる(固まらない・何も書かない)
        self.assertEqual(self.click_save(page), 422)
        page.wait_for_function(
            "[...document.querySelectorAll('.toast__text')]"
            ".some((t) => t.textContent.includes('まだ1行も打っていません'))", timeout=8000)
        self.assertEqual(self.repo().list_keys(), [])

        self.pick_worker(page)
        self.assertEqual(self.value(page, "hd-worker"), "山田")
        self.assertTrue(page.is_hidden("#start-shift"))
        for button in ("#open-formstop", "#new-page", "#time-split", "#time-split-lot"):
            self.assertTrue(page.is_enabled(button), button)
        page.wait_for_function(
            "document.querySelector('#steps [data-step=\"worker\"]')?.dataset.done === '1'",
            timeout=8000)
        self.assertIn("2026年10月5日 1直", page.inner_text("#sheet-key"))
        self.assert_clean()


# ======================================================================
# 2. 行を打つ
# ======================================================================
class TypingTests(FlowCase):
    def test_ロットを引き_時刻はダブルクリック_停止_休憩_その他_順に打つまで1直ぶん打てて手元に残る(self) -> None:
        page = self.boot()

        with self.subTest("1行目: ロット№を引くと品名・寸法・検入・VC・合紙が入る"):
            self.type_lot(page, 1, LOT_A)
            got = self.row(page, 1)
            self.assertEqual((got["ZAI"], got["SIZ"], got["KEN"], got["VC"], got["AI"]),
                             ("52S- R", "20.000×1528.0×3053.0", "9", "A:VE-20NF_B:VE-20NF", "有"))
            self.assertNotIn("ET", got)                      # 輸出ではない

        with self.subTest("開始のダブルクリックでいまの時刻・終了は次の行の開始へ写る"):
            self.stamp(page, "KZ1")
            self.assertEqual((self.value(page, "KZ1"), self.value(page, "KH1")), ("07", "05"))
            self.type_in(page, "MAI1", "3")
            self.type_in(page, "TUT1", "1")
            self.at("08:02")                                  # 分は 0 か 5 に寄せる
            self.stamp(page, "SZ1")
            self.assertEqual((self.value(page, "SZ1"), self.value(page, "SH1")), ("08", "00"))
            self.wait_value(page, "KH2", "00")
            self.assertEqual(self.value(page, "KZ2"), "08")
            self.wait_value(page, "TIM1", "55")               # 作業時間(分)が出る

        with self.subTest("2行目: 輸出のロットは etc に EX が自動で付く・停止①は記号と分"):
            self.type_lot(page, 2, LOT_EX)
            self.wait_value(page, "ET2", "EX")
            self.type_in(page, "MAI2", "5")
            self.type_in(page, "TUT2", "2")
            page.select_option("#S2", "イ")
            self.type_in(page, "TH2", "10")
            self.at("09:00")
            self.stamp(page, "SZ2")
            self.wait_value(page, "TIM2", "50")               # 60分 − 停止10分
            self.assertIn("①", page.inner_text('[data-stopsum-row="2"]'))

        with self.subTest("3行目: 休憩を選ぶと分と時刻を聞き、時刻は表の下の欄に残る"):
            page.select_option("#S3", "0")
            page.wait_for_selector("#break-modal:not([hidden])", timeout=8000)
            self.assertIn("3行目", page.inner_text("#break-why"))
            page.fill("#break-minutes", "60")
            page.fill("#break-text", "0900-1000")
            page.click("#break-apply")
            page.wait_for_selector("#break-modal", state="hidden", timeout=8000)
            self.wait_value(page, "TH3", "60")
            self.wait_value(page, "break-3", "9:00〜10:00")
            self.at("10:00")
            self.stamp(page, "SZ3")
            self.wait_value(page, "TIM3", "0")

        with self.subTest("4行目(2行目のロットの続き。ロット№は空): その他を選ぶと理由を聞き、理由欄に残る"):
            self.type_in(page, "MAI4", "2")
            page.select_option("#S4", "ヨ")
            page.wait_for_selector("#reason-modal:not([hidden])", timeout=8000)
            self.assertIn("4行目", page.inner_text("#reason-why"))
            page.fill("#reason-text", "フォーク待ち")
            page.click("#reason-apply")
            page.wait_for_selector("#reason-modal", state="hidden", timeout=8000)
            self.wait_value(page, "reason-4", "フォーク待ち")
            self.type_in(page, "TH4", "15")
            self.at("11:00")
            self.stamp(page, "SZ4")
            self.wait_value(page, "TIM4", "45")

        with self.subTest("5行目: 行番号のダブルクリックで順に打つ(開始は前の終了が入っている)"):
            page.dblclick('#grid-body tr[data-row="5"] .rowno__n')
            page.wait_for_selector("#seq-entry:not([hidden])", timeout=8000)
            self.assertEqual(page.text_content("#seq-entry-label"), "作業開始")
            self.assertEqual(page.input_value("#seq-entry-input"), "1100")
            for text in ("", "1130", "2", "", "4", "1"):
                box = page.locator("#seq-entry-input")
                box.fill(text)
                box.press("Enter")
            page.wait_for_selector("#seq-entry", state="hidden", timeout=8000)
            self.wait_value(page, "TIM5", "30")
            self.assertEqual(self.row(page, 5)["HIT"], "2")

        with self.subTest("打ちかけは自動保存で手元に残る(開き直しても消えない)"):
            # 自動保存は1分に1回。時計を進めて欄を離れると書く(どの要求が書いたかは問わず、
            # **手元の DB に5行目まで入る**のを待つ)
            self.at("11:32")
            self.settle(page, 5)
            self.wait_until(page, lambda: self.saved_row(5).get("sh") == "30",
                            "自動保存で5行目まで手元に入る", timeout=15.0)
            page.reload(wait_until="load")
            page.wait_for_function("window.APP && window.APP.booted === true", timeout=10000)
            self.assertEqual(self.value(page, "hd-worker"), "山田")
            for n, lot in ((1, LOT_A), (2, LOT_EX)):
                self.assertEqual(self.value(page, f"LOT{n}"), lot)
            self.assertEqual(self.value(page, "reason-4"), "フォーク待ち")
            self.assertEqual(self.value(page, "break-3"), "9:00〜10:00")
            self.assertEqual((self.value(page, "SZ5"), self.value(page, "SH5")), ("11", "30"))
        self.assert_clean()

    def test_前の直で作業していたロットは検入数を訊く(self) -> None:
        from nippou import work_context
        from nippou.db.models import DetailRecord, HeaderRecord

        # 1直にロット A を 3 枚 × 2 行(6枚。2行目は続きの行でロット№が空 ── 紙の書き方)。共有へも出してある
        at = dict(report_date="2026年10月5日", line=work_context.get_context().line,
                  shift="1直", page=1)
        self.repo().save(HeaderRecord(**at, worker="青木"),
                         [DetailRecord(**at, row_no=n, lot=LOT_A if n == 1 else "", ken="9",
                                       mai="3", tut="1", con="3") for n in (1, 2)])
        self.repo().mark_synced(("2026年10月5日", at["line"], "1直", 1))
        self.at("15:30")                                       # 2直のなか
        self.settle_review()
        page = self.boot()
        self.type_in(page, "LOT1", LOT_A)
        self.wait_until(page, lambda: self.dialogs, "検入数を訊く窓")
        # 続きの行(ロット№が空)も同じロット。**2行で6枚**(前は1行目の3枚しか数えなかった)
        self.assertIn("1直で6枚作業しています", self.dialogs[0])
        self.assertIn("検入数を変えますか?(9枚 → 3枚)", self.dialogs[0])
        self.wait_value(page, "KEN1", "3")
        self.assert_clean()


# ======================================================================
# 3. 時間を割り振る
# ======================================================================
class SplitTests(FlowCase):
    def test_時間を割り振る_LOTで割り振る_知らせは1回(self) -> None:
        page = self.boot()
        self.type_lot(page, 1, LOT_A)
        self.fill(page, 1, MAI="5", TUT="1")
        self.fill(page, 2, MAI="5", TUT="1")                  # ロット A の続き(ロット№は空)
        self.type_lot(page, 3, LOT_EX)
        self.fill(page, 3, MAI="5", TUT="1")
        self.settle(page, 3)
        self.at("12:00")

        page.click("#time-split")
        self.wait_value(page, "SH3", "00")
        self.assertIn("1直 07:00〜15:00(480分)を、打った 3 行に割り振ります。", self.dialogs[0])
        self.assertIn("EX のロット", self.dialogs[0])
        self.assertIn("休憩 12:00〜13:00(60分)は飛ばします。", self.dialogs[0])
        self.assertEqual((self.value(page, "KZ1"), self.value(page, "KH1")), ("07", "00"))
        self.assertEqual((self.value(page, "SZ3"), self.value(page, "SH3")), ("15", "00"))
        # 行の時刻はつながる・休憩をまたぐ行に休憩(0)60分が入る
        self.assertEqual((self.value(page, "SZ1"), self.value(page, "SH1")),
                         (self.value(page, "KZ2"), self.value(page, "KH2")))
        self.assertEqual((self.value(page, "S3"), self.value(page, "TH3")), ("0", "60"))
        done = "を 1ページ 3行に割り振りました"
        page.wait_for_function(
            "(t) => [...document.querySelectorAll('.toast__text')].some((x) => x.textContent.includes(t))",
            arg=done, timeout=8000)
        self.assertEqual(sum(done in t for t in self.toasts(page)), 1,
                         f"同じ知らせが2つ出ています: {self.toasts(page)}")

        # LOTで時間を割り振る: 選んでいる行(3行目)のロットだけ
        self.dialogs.clear()
        page.click("#MAI3")
        with page.expect_response(lambda r: r.url.endswith("/api/entry/distribute-lot")
                                  and '"dry_run"' not in (r.request.post_data or "")):
            page.click("#time-split-lot")
        self.assertIn(f"ロット {LOT_EX} の 1 行(3行目〜3行目)", self.dialogs[0])
        self.assertIn("開始・終了の時刻が入っている 1 行も書き換えます。", self.dialogs[0])
        done = f"ロット {LOT_EX} の 1行に"
        page.wait_for_function(
            "(t) => [...document.querySelectorAll('.toast__text')].some((x) => x.textContent.includes(t))",
            arg=done, timeout=8000)
        self.assertEqual(sum(done in t for t in self.toasts(page)), 1,
                         f"同じ知らせが2つ出ています: {self.toasts(page)}")
        self.assertEqual((self.value(page, "KZ1"), self.value(page, "KH1")), ("07", "00"),
                         "ほかのロットの行を書き換えました")
        self.assert_clean()


# ======================================================================
# 4. 保存・チェック・次ページ・前のページ
# ======================================================================
class PageTests(FlowCase):
    def fill_page(self, page, *, start: int = 7 * 60, prefix: str = "X") -> None:
        """12行ぜんぶ(1行30分。6行目は休憩30分)。ロット№は仕掛に無い番号(手で打った形)。"""
        t = start
        for n in range(1, 13):
            a, b = t, t + 30
            times = dict(KZ=f"{a // 60:02d}", KH=f"{a % 60:02d}", SZ=f"{b // 60:02d}", SH=f"{b % 60:02d}")
            if n == 6:
                self.fill(page, n, **times, S="0", TH="30",
                          break_range=f"{a // 60}:{a % 60:02d}〜{b // 60}:{b % 60:02d}")
            else:
                self.fill(page, n, **times, LOT=f"{prefix}{n:06d}", MAI="2", TUT="1", KEN="10")
            t = b
        self.settle(page, 12)

    def test_保存してチェックし_12行で次ページを出し_前のページを見て最新に戻る(self) -> None:
        page = self.boot()
        self.fill_page(page)
        page.wait_for_selector("#page-guide:not([hidden])", timeout=8000)
        self.assertIn("12行目まで埋まりました", page.inner_text("#page-guide"))
        self.assertIn("btn--primary", page.get_attribute("#new-page", "class"))

        with self.subTest("保存(確定)"):
            self.at("13:05")
            self.assertEqual(self.click_save(page), 200)
            page.wait_for_function(
                "document.querySelector('#steps [data-step=\"save\"]')?.dataset.done === '1'",
                timeout=8000)
            self.assertEqual(self.repo().saved_pages("2026年10月5日", "L-1", "1直"), [1])

        with self.subTest("この直をチェック: 直の終わりまでに要るもの(最終時間・休憩)を並べる"):
            page.click("#check-shift")
            page.wait_for_selector("#check-panel:not([hidden])", timeout=8000)
            text = page.inner_text("#check-panel")
            self.assertIn("最終時間まで入力がないのでは？", text)
            self.assertIn("30分しか休憩の入力がありません。60分必要です", text)
            self.assertIn("直の終わりまでに", text)

        with self.subTest("次ページ発行: サーバの文言で訊き、1行目の開始に前の最終の終了"):
            page.click("#new-page")
            page.wait_for_function(
                "document.getElementById('sheet-key')?.dataset.openedPage === '2'", timeout=15000)
            page.wait_for_function("window.APP && window.APP.booted === true", timeout=10000)
            self.assertIn("第1ページを保存して、第2ページを出します。よろしいですか?", self.dialogs)
            self.assertEqual((self.value(page, "KZ1"), self.value(page, "KH1")), ("13", "00"))
            self.assertEqual(self.value(page, "LOT1"), "")
            self.assertEqual(self.value(page, "hd-worker"), "山田", "作業者を引き継いでいません")
            self.assertIn("第2ページ / 全2ページ", page.inner_text("#page-lead"))

        with self.subTest("前のページを見る: 打ちかけを置いてから移る・見ているあいだは発行と共有へは押せない"):
            self.fill(page, 1, LOT="Y000001", SZ="13", SH="30", MAI="2", TUT="1", KEN="10")
            page.select_option("#page-switch", "1")
            page.wait_for_function(
                "document.getElementById('sheet-key')?.dataset.openedPage === '1'", timeout=15000)
            page.wait_for_function("window.APP && window.APP.booted === true", timeout=10000)
            self.assertEqual(self.value(page, "LOT12"), "X000012")
            self.assertIn("第1ページを直しています", page.inner_text("#page-guide"))
            self.assertTrue(page.is_disabled("#new-page"))
            self.assertTrue(page.is_disabled("#push-shared"))

        with self.subTest("最新のページに戻る: 2ページ目に打っていた行が残っている"):
            page.click("#back-to-current")
            page.wait_for_function(
                "document.getElementById('sheet-key')?.dataset.openedPage === '2'", timeout=15000)
            page.wait_for_function("window.APP && window.APP.booted === true", timeout=10000)
            self.assertEqual(self.value(page, "LOT1"), "Y000001")
            self.assertEqual((self.value(page, "SZ1"), self.value(page, "SH1")), ("13", "30"))
        self.assert_clean()


# ======================================================================
# 5. 共有へ保存 → グラフ → 確認しました → 印
# ======================================================================
class PushTests(FlowCase):
    def test_打ち切って共有へ保存_グラフ_確認しました_印_直すと変わり_押し直すと戻る(self) -> None:
        page = self.boot()
        self.type_clean_shift(page)
        self.at("14:40")
        page.click("#check-shift")
        page.wait_for_selector("#check-panel:not([hidden])", timeout=8000)
        self.assertIn("直すところはありません", page.inner_text("#check-head"))
        self.assertEqual(self.mark(page)["state"], "unsent")

        # 共有へ保存(打ちかけは先に置いて送る)→ 送った直の実績が画面いっぱい
        page.click("#push-shared")
        self.to_review(page)
        self.assertIn("1直 を共有へ渡しました(1ページ)", page.inner_text("#closing-headline"))
        self.assertEqual(page.inner_text("#review-act").strip(), "確認しました")
        self.assertIn("共有へ保存しました", page.inner_text("#review-reason"))
        self.back_to_entry(page)
        self.wait_mark(page, "saved", f"{SAVED}(14:40 山田)")
        self.assertEqual(self.value(page, "LOT3"), LOT_EX)

        # 行を直して保存(確定)→ 印が変わる → 押し直す → 戻る
        self.at("14:45")
        self.fill(page, 3, MAI="6")
        self.settle(page, 3)
        self.assertEqual(self.click_save(page), 200)
        self.wait_mark(page, "stale", STALE)
        self.at("14:46")
        page.click("#push-shared")
        self.to_review(page)
        self.back_to_entry(page)
        self.wait_mark(page, "saved", f"{SAVED}(14:46 山田)")
        self.assertEqual(self.value(page, "MAI3"), "6")
        self.assert_clean()


# ======================================================================
# 6. 断る場面
# ======================================================================
class RefusalTests(FlowCase):
    def test_梱包数で断られたら理由を並べ_直して保存すれば断りの欄は消える(self) -> None:
        """前は直して「保存しました」が出ても、赤い「保存できません」の欄が残っていた。"""
        page = self.boot()
        self.type_lot(page, 1, LOT_A)
        self.fill(page, 1, KZ="07", KH="00", SZ="10", SH="00", MAI="5", TUT="2")   # 10 > 検入 9
        self.settle(page, 1)
        self.assertEqual(self.click_save(page), 422)
        page.wait_for_selector("#check-panel:not([hidden])", timeout=8000)
        self.assertIn("梱包数 10 が検入枚数 9 を超えています", page.inner_text("#check-list"))
        # 見出しは一言(中身は下の一覧に1回だけ)
        self.assertNotIn("超えています", page.inner_text("#check-head"))

        # ほかの理由で断られたら、前の断りは残さない(開始=終了)
        self.fill(page, 1, TUT="1", SZ="07", SH="00")
        self.settle(page, 1, "TUT")
        self.assertEqual(self.click_save(page), 422)
        page.wait_for_selector("#check-panel", state="hidden", timeout=8000)
        self.assertIn("開始時間と終了時間が同じ", page.inner_text("#bad-note") + page.inner_text("#time-note"))

        self.fill(page, 1, SZ="10", SH="00")
        self.settle(page, 1, "SH")
        self.assertEqual(self.click_save(page), 200)
        self.assertTrue(page.is_hidden("#check-panel"), "直して保存したのに断りの欄が残っています")
        self.assert_clean()

    def test_途中で共有へ保存すると断り_設備移動のため保存で通すと確認画面は渡しましたと言う(self) -> None:
        page = self.boot()
        self.type_clean_shift(page, end="13:00")              # 13:00 で設備移動
        self.at("13:30")
        before = self.push_count()
        page.click("#push-shared")
        page.wait_for_selector("#check-panel:not([hidden])", timeout=15000)
        self.assertGreater(self.push_count(), before)
        self.assertIn("最終時間まで入力がないのでは？", page.inner_text("#check-list"))
        page.wait_for_selector("#entry-skip:not([hidden])", timeout=8000)
        self.assertTrue(page.is_enabled("#entry-skip-phrase"))
        self.assertEqual(page.evaluate("location.pathname"), "/")
        self.assertEqual(self.mark(page)["state"], "unsent")

        # 打たずに押すと、打つものを言う(何も送らない)
        before = self.push_count()
        page.click("#entry-skip-push")
        page.wait_for_function(
            "[...document.querySelectorAll('.toast__text')]"
            ".some((t) => t.textContent.includes('と打ってください'))", timeout=8000)
        self.assertEqual(self.push_count(), before)

        page.fill("#entry-skip-phrase", SKIP)
        page.click("#entry-skip-push")
        self.to_review(page)
        # **送れたのに「直すところがあります ── 共有へ保存できません」と言わない**
        headline = page.inner_text("#closing-headline")
        self.assertIn("1直 を共有へ渡しました", headline)
        self.assertNotIn("共有へ保存できません", headline)
        self.assertEqual(page.inner_text("#review-act").strip(), "確認しました")
        self.back_to_entry(page)
        self.wait_mark(page, "saved", SAVED)
        self.assert_clean()

    def test_共有へ届かないときは入力画面に留まり_送れなかったと言う(self) -> None:
        page = self.boot()
        self.type_clean_shift(page)
        self.at("14:40")
        self.share.rename(self.tmp / "共有_切れた")
        before = self.push_count()
        page.click("#push-shared")
        self.wait_until(page, lambda: self.push_count() > before, "共有へ保存の要求")
        self.wait_mark(page, "failed", "送れなかったページがあります")
        self.assertEqual(page.evaluate("location.pathname"), "/")
        self.assertEqual(page.locator("#review").count(), 0, "送れていないのに確認画面が出ました")
        self.assertEqual(self.value(page, "LOT3"), LOT_EX, "打った行が消えました")
        self.assert_clean()

    def test_LOTが重複したら自動保存の一言に理由が出る(self) -> None:
        """同じロット№を2行に打つと保存できない。自動保存も書けないので、**黙らずに理由を言う。**"""
        page = self.boot()
        self.type_lot(page, 1, LOT_A)
        self.fill(page, 1, MAI="3", TUT="1")
        self.type_lot(page, 2, LOT_A)
        self.fill(page, 2, MAI="2", TUT="1")
        self.at("07:20")                                       # 自動保存の間引きを過ぎる
        with page.expect_response(lambda r: r.url.endswith("/api/entry/save")
                                  and '"silent"' in (r.request.post_data or "")
                                  and r.status == 422, timeout=15000):
            self.settle(page, 2)
        page.wait_for_function(
            "document.getElementById('save-note')?.dataset.state === 'failed'", timeout=8000)
        note = page.inner_text("#save-note")
        self.assertIn("自動保存できませんでした", note)
        self.assertIn(f"LOTが重複しています: {LOT_A}", note)
        self.assert_clean()


# ======================================================================
# 7. 印刷・梱包資材重量計算への行き来
# ======================================================================
class PaperAndGwTests(FlowCase):
    def test_印刷はこの直の全ページを別窓で開く(self) -> None:
        page = self.boot()
        self.type_clean_shift(page)
        self.assertEqual(self.click_save(page), 200)
        with self.ctx.expect_page(timeout=15000) as opened:
            page.click("#print-page")
        paper = opened.value
        paper.wait_for_load_state("load")
        self.assertIn("/report/nippou?", paper.url)
        self.assertIn("page=all", paper.url)
        text = paper.inner_text("body")
        self.assertIn("印刷する", text)
        self.assertIn(LOT_A, text)
        self.assertIn(LOT_EX, text)
        self.assertEqual(page.evaluate("location.pathname"), "/", "入力画面が移りました")
        self.assert_clean()

    def test_梱包資材重量計算へ行って戻っても打ちかけが残る(self) -> None:
        page = self.boot()
        self.type_lot(page, 1, LOT_A)
        self.type_in(page, "MAI1", "3")                       # 打ったまま(自動保存の前)
        page.click('nav.rail a[href="/gw"]')
        page.wait_for_selector("#lot-no", timeout=15000)
        self.type_in(page, "lot-no", LOT_A)
        self.wait_filled(page, "dim-thickness_mm")
        page.click('nav.rail a[href="/"]')
        page.wait_for_selector("#grid-body", timeout=15000)
        page.wait_for_function("window.APP && window.APP.booted === true", timeout=10000)
        self.wait_value(page, "LOT1", LOT_A)
        self.assertEqual(self.value(page, "MAI1"), "3", "打ちかけが消えました")
        self.assertEqual(self.value(page, "hd-worker"), "山田")
        self.assert_clean()


if __name__ == "__main__":
    unittest.main()
