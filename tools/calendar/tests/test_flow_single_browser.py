"""カレンダーを**1台の端末で使う一連の流れ**を、本物のブラウザで通しで確かめる

現場の人が押す順に、画面のボタン・窓を実際に押して回ります(API を直に叩かない)。

1. 初めての起動: 設定がまっさらで開く → 班員名簿が無いときの案内 → 設定でライン・参照パス・
   班員名簿(CSV)を入れる → カレンダーに戻る
2. 休みの登録: 日付 → 休み → 班員 → 直・残業繋ぎ・早出繋ぎ → マス・マウスの説明・「登録 N 件」。
   同じ人を2回・直の無い班・前後の月のマス・日曜祝日・今日の枠
3. 連絡: 「xx班からの連絡」・時刻・ライン(作業長だけ選べる)→ マス・説明・内容閲覧
4. 内容閲覧 → 削除(選んで消す・何も選ばずに押す・消したあと)
5. 月の移動(端の年まで)・印刷(マスと同じ中身)
6. 履歴: 月選び・件数・過去の日の内容閲覧(削除の道が無い)
7. 設定: 画面の色・自動同期の間隔・管理者パスワード。保存して開き直すと残る
8. 端末のラインでの絞り込み(マス・説明・内容閲覧・削除・件数・印刷が同じ決まり)
9. どの場面でも画面の誤り(pageerror)と console.error が出ない

共有フォルダを使った複数台の見え方は別の試験(``test_sync`` など)が見ます。
無い環境では飛ばします(CI では飛ばしません: ``ALLTOOLS_REQUIRE_BROWSER=1``)。
"""
from __future__ import annotations

import datetime as _dt
import re
import tempfile
import unittest
from pathlib import Path

from calendar_app import config, repository
from calendar_app import settings as user_settings
from calendar_app.holiday import holiday_name
from calendar_app.presenters import calendar as cal
from tests import _web
from tests._page_browser import ORIGIN, PageBrowserCase

#: 画面に出す時刻の形(秒は出さない)
SHOWN_TIME = r"\d{4}/\d{2}/\d{2} \d{2}:\d{2}(?!:)"

#: 班員名簿の CSV(Excel が書き出す形: BOM 付き UTF-8)
MEMBERS_CSV = (
    "管理番号,苗字,班,名前,読み,担当ライン\n"
    "101,山田,A,山田太郎,やまだたろう,L-1\n"
    "102,加藤,B,加藤眞一,かとうしんいち,L-1\n"
    "103,佐藤,昼勤,佐藤花子,さとうはなこ,L-1\n"
    "201,小林,A,小林コイル,こばやしこいる,コイル\n"
)


def _key(day: _dt.date) -> str:
    return day.strftime(config.DATE_KEY_FORMAT)


def _cell(day: _dt.date) -> str:
    return f'.day[data-date="{_key(day)}"]'


def _workday(year: int, month: int, *, avoid: tuple[_dt.date, ...] = ()) -> _dt.date:
    """その月の平日(祝日でない月〜金)。"""
    for n in range(1, cal.month_days(year, month) + 1):
        day = _dt.date(year, month, n)
        if day.weekday() < 5 and not holiday_name(day) and day not in avoid:
            return day
    raise AssertionError("平日がありません")


def _sunday(year: int, month: int) -> _dt.date:
    for n in range(1, 8):
        day = _dt.date(year, month, n)
        if day.weekday() == 6:
            return day
    raise AssertionError


class FlowCase(PageBrowserCase):
    """画面の誤りに加えて console.error も数える。

    数えないもの:
    - 印刷のページが自分で取りに行く favicon(取り次ぎを通らない。画面の誤りではない)
    - **わざと断った返事(4xx)にブラウザ自身が出す「Failed to load resource」**。パスワードを
      求める 403・無いフォルダの 422 など、画面が理由を出して受け止める断りで、JS の誤りではない。
      `refusals` に分けて残し、5xx は数える(サーバの思わぬ誤り)
    """

    def setUp(self) -> None:
        super().setUp()
        self.console: list[str] = []
        self.refusals: list[str] = []

    def _on_console(self, m) -> None:
        if m.type != "error":
            return
        url = str(m.location.get("url", ""))
        if url.endswith("/favicon.ico"):
            return
        if re.match(r"Failed to load resource: the server responded with a status of 4\d\d", m.text):
            self.refusals.append(f"{m.text} @ {url}")
            return
        self.console.append(f"{m.text} @ {url}")

    def watch(self, page) -> None:
        page.on("console", self._on_console)

    def open(self, path: str, ctx=None, *, wait_for: str = ".ribbon", prepare=None):
        def both(page) -> None:
            self.watch(page)
            if prepare:
                prepare(page)
        return super().open(path, ctx, wait_for=wait_for, prepare=both)

    maxDiff = None

    def keep_settings(self, *, clear: bool = False) -> None:
        """この試験で変えた設定(settings.json)を、終わったら元に戻す。

        :param clear: 始める前に消す(**この端末で一度も開いていない**状態にする)
        """
        path = config.settings_path()
        before = path.read_bytes() if path.exists() else None
        if clear and path.exists():
            path.unlink()

        def restore() -> None:
            if before is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(before)
        self.addCleanup(restore)

    def assert_quiet(self) -> None:
        self.assertEqual(self.errors, [], "画面の誤り(pageerror)")
        self.assertEqual(self.console, [], "console.error")

    # -- よく使う手順 ------------------------------------------------------
    def modal_title(self, page) -> str:
        return page.inner_text(".modal:last-of-type .modal__head h2")

    def modal_text(self, page) -> str:
        return page.inner_text(".modal:last-of-type")

    def press(self, page, label: str) -> None:
        """いちばん上の窓のボタンを押す。"""
        page.click(f".modal:last-of-type .modal__foot button:text-is('{label}')")

    def wait_closed(self, page) -> None:
        page.wait_for_selector(".modal", state="detached")

    def choose(self, page, day: _dt.date, action: str) -> None:
        page.click(_cell(day))
        page.wait_for_selector(".modal button.choice")
        page.click(f".modal button.choice:has-text('{action}')")

    def pick_worker(self, page, name: str) -> None:
        page.click(f".modal:last-of-type button.worker:text-is('{name}')")

    def toast_text(self, page) -> str:
        return page.inner_text("#toasts")

    def wait_cell(self, page, day: _dt.date, text: str, *, gone: bool = False) -> None:
        page.wait_for_function(
            "([sel, text, gone]) => { const c = document.querySelector(sel);"
            " const has = !!c && (c.innerText || '').includes(text); return gone ? !has : has; }",
            arg=[_cell(day), text, gone])

    def rail(self, page, label: str, wait_for: str) -> None:
        """左のレールで画面を移る(同じタブ)。"""
        page.click(f".rail a:has-text('{label}')")
        page.wait_for_selector(wait_for, state="attached")


# ---------------------------------------------------------------------------
# 1. 初めての起動
# ---------------------------------------------------------------------------
class FirstRunFlowTests(FlowCase):
    def setUp(self) -> None:
        super().setUp()
        # **設定がまっさら**(この端末で一度も開いていない)。あとで元に戻す
        self.keep_settings(clear=True)

        from calendar_app.dbkit import outbox_sync
        outbox_sync.reset_op_id_cache()
        self.addCleanup(outbox_sync.reset_op_id_cache)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(_web._remove_when_released, tmp)
        # 画面で参照パスを決めると同期が走り出す。**一時フォルダを消す前に止める**
        # (後に積んだものが先に動く。Windows は開いているファイルを消せない)
        self.addCleanup(_web.reset_sync)
        self.share = Path(tmp.name) / "共有"
        self.share.mkdir()
        _web.make_source(self.share, config.SOURCE_FILE_DATA, _web.SOURCE_SCHEMA)
        self.csv = Path(tmp.name) / "班員名簿.csv"
        self.csv.write_text(MEMBERS_CSV, encoding="utf-8-sig")
        self.today = _dt.date.today()
        self.day = _workday(self.today.year, self.today.month)

    def password(self, page, value: str = _web.ADMIN_PASSWORD) -> None:
        page.wait_for_selector("#admin-pass")
        page.fill("#admin-pass", value)
        self.press(page, "続ける")

    def test_まっさらから_案内_設定_名簿の取り込み_カレンダーで休みを登録できるまで(self) -> None:
        page = self.open("/calendar")
        page.wait_for_selector(".day[data-date]")
        # 班員名簿が無いときの案内。ラインは「未設定」
        page.wait_for_selector("#cal-notice:not([hidden])")
        self.assertIn("班員名簿が取り込まれていません", page.inner_text("#cal-notice"))
        self.assertIn("設定画面", page.inner_text("#cal-notice"))
        self.assertEqual(page.inner_text("#rb-line").strip(), "未設定")

        # 名簿が無いまま休みを押す → 何をすればよいかの案内
        self.choose(page, self.day, "休み")
        page.wait_for_selector(".modal .modal__head h2:text-is('作業者が居ません')")
        self.assertIn("設定画面で班員名簿の取り込みとライン設定", self.modal_text(page))
        self.press(page, "閉じる")
        self.wait_closed(page)
        # 連絡も同じく案内
        self.choose(page, self.day, "コメント・連絡")
        page.wait_for_selector(".modal .modal__head h2:text-is('班がありません')")
        self.assertIn("設定画面", self.modal_text(page))
        self.press(page, "閉じる")
        self.wait_closed(page)

        # 参照パスが無いまま「今すぐ同期」→ どこで何をするかを言う
        page.click("#sync-now")
        page.wait_for_selector(".modal .modal__head h2:text-is('同期の設定が要ります')")
        self.assertIn("参照パス", self.modal_text(page))
        self.press(page, "閉じる")
        self.wait_closed(page)
        # 履歴: 過去の登録が無い → 空のマス目を出さずに言う。押すボタンは押せない
        self.rail(page, "履歴", "#his-title")
        page.wait_for_selector("#his-empty:not([hidden])")
        self.assertIn("過去の月の登録がありません", page.inner_text("#his-empty"))
        for button in ("#his-prev", "#his-next", "#his-pick", "#his-print"):
            self.assertTrue(page.is_disabled(button), button)

        # -- 設定へ ----------------------------------------------------------
        self.rail(page, "設定", "#st-line option")
        page.wait_for_selector("#st-problems .note")
        problems = page.inner_text("#st-problems")
        self.assertIn("この端末のラインが未設定です", problems)
        self.assertIn("保存用DB(連絡帳)が見つかりません", problems)
        self.assertIn("班員名簿がありません", page.inner_text("#st-members"))

        # ライン: 管理者パスワードを聞かれる。打ち間違えると「違います」と言う
        page.select_option("#st-line", "L-1")
        page.click("#st-line-save")
        self.password(page, "ちがう")
        page.wait_for_selector(".modal .modal__head h2:text-is('管理者パスワードが違います')")
        self.password(page)
        self.wait_closed(page)
        page.wait_for_function("document.getElementById('rb-line').textContent.trim() === 'L-1'")
        self.assertIn("ラインを「L-1」に設定しました", self.toast_text(page))
        self.assertEqual(user_settings.get_my_line(), "L-1")

        # 班員名簿の CSV: 空のまま押す・無いファイル → 分かる文で断る
        page.click("#st-csv-run")
        page.wait_for_selector(".modal .modal__head h2:text-is('パスが要ります')")
        self.press(page, "閉じる")
        self.wait_closed(page)
        page.fill("#st-csv", str(self.csv.with_name("無い.csv")))
        page.click("#st-csv-run")
        page.wait_for_selector(".modal .modal__head h2:text-is('取り込めません')")
        self.assertIn("ファイルが見つかりません", self.modal_text(page))
        self.press(page, "閉じる")
        self.wait_closed(page)
        page.fill("#st-csv", str(self.csv))
        page.click("#st-csv-run")
        page.wait_for_function(
            "document.getElementById('st-members').textContent.includes('班員名簿 4 件')")
        self.assertIn("班員名簿を 4 件取り込みました", self.toast_text(page))

        # -- 参照パスを決める前にカレンダーへ: 名簿は出るが、登録は断る -----------
        self.rail(page, "カレンダー", ".day[data-date]")
        page.wait_for_function("document.getElementById('cal-notice').hidden")
        self.choose(page, self.day, "休み")
        self.pick_worker(page, "佐藤花子")
        page.wait_for_selector(".modal .modal__head h2:text-is('登録できません')")
        self.assertIn("参照パス", self.modal_text(page))
        self.press(page, "閉じる")
        self.wait_closed(page)
        self.assertNotIn("佐藤花子", page.inner_text(_cell(self.day)))

        # -- 設定へ戻って参照パス ------------------------------------------------
        self.rail(page, "設定", "#st-line option")
        # 無いフォルダは(パスワードを聞く前に)断る。打った値は残す
        page.fill("#st-data-dir", str(self.share / "無いフォルダ"))
        page.click("#st-data-save")
        page.wait_for_selector(".modal .modal__head h2:text-is('保存できません')")
        self.assertIn("見つかりません", self.modal_text(page))
        self.press(page, "閉じる")
        self.wait_closed(page)
        self.assertEqual(page.input_value("#st-data-dir"), str(self.share / "無いフォルダ"))
        # あるフォルダなら通る
        page.fill("#st-data-dir", str(self.share))
        page.click("#st-data-save")
        self.password(page)
        self.wait_closed(page)
        page.wait_for_function(
            "document.getElementById('toasts').innerText.includes('フォルダを保存しました')")
        self.assertEqual(user_settings.data_db_dir_setting(), str(self.share))
        page.wait_for_function(
            "!document.getElementById('st-problems').innerText.includes('保存用DB(連絡帳)が見つかりません')")
        self.assertEqual(page.input_value("#st-auto"), "1", "はじめて場所を決めた端末は自動同期が入る")

        # -- カレンダーへ戻る ------------------------------------------------
        self.rail(page, "カレンダー", ".day[data-date]")
        page.wait_for_function("document.getElementById('cal-notice').hidden")
        self.assertEqual(page.inner_text("#rb-line").strip(), "L-1")
        # L-1 の端末で選べるのは L-1 の人だけ(コイルの人は出ない)
        self.choose(page, self.day, "休み")
        page.wait_for_selector(".modal button.worker")
        names = page.eval_on_selector_all(".modal button.worker", "(bs) => bs.map((b) => b.textContent)")
        self.assertEqual(sorted(names), sorted(["山田太郎", "加藤眞一", "佐藤花子"]))
        self.pick_worker(page, "佐藤花子")          # 昼勤: 直を聞かずに登録
        self.wait_closed(page)
        self.wait_cell(page, self.day, "佐藤花子 休")
        self.assert_quiet()


def _add_member(conn, code: str, name: str, group: str, line: str) -> None:
    conn.execute(
        f'INSERT INTO "{config.TABLE_MEMBER}" '
        '("管理番号","苗字","班","名前","読み","担当ライン") VALUES (?,?,?,?,?,?)',
        (code, name, group, name, name, line))
    conn.commit()


class LineTerminalCase(FlowCase):
    """L-1 の端末。参照パスあり・班員名簿あり。"""

    def setUp(self) -> None:
        super().setUp()
        self.source = _web.with_source(self)
        _add_member(self.conn, "101", "山田太郎", "A", "L-1")
        _add_member(self.conn, "102", "加藤眞一", "B", "L-1")
        _add_member(self.conn, "103", "佐藤花子", "昼勤", "L-1")
        _add_member(self.conn, "104", "鈴木一郎", "C", "LVC")
        _add_member(self.conn, "201", "小林コイル", "A", "コイル")
        self.addCleanup(user_settings.save_my_line, "")
        user_settings.save_my_line("L-1")
        self.today = _dt.date.today()
        self.day = _workday(self.today.year, self.today.month)

    def rows(self, kubun: str | None = None) -> list:
        sql = f'SELECT * FROM "{config.TABLE_DATA}"'
        if kubun:
            return self.conn.execute(sql + ' WHERE "区分"=?', (kubun,)).fetchall()
        return self.conn.execute(sql).fetchall()

    def view_text(self, page, day: _dt.date) -> str:
        self.choose(page, day, "内容閲覧")
        page.wait_for_selector(".modal .viewer")
        text = page.inner_text(".modal .viewer")
        self.press(page, "閉じる")
        self.wait_closed(page)
        return text

    def count_note(self, page, day: _dt.date) -> str:
        """日付を押したときの「内容閲覧」の添え書き(登録 N 件)。"""
        page.click(_cell(day))
        page.wait_for_selector(".modal button.choice")
        note = page.inner_text(".modal button.choice:has-text('内容閲覧') small")
        page.keyboard.press("Escape")
        self.wait_closed(page)
        return note


# ---------------------------------------------------------------------------
# 2. 休みの登録
# ---------------------------------------------------------------------------
class RestFlowTests(LineTerminalCase):
    def test_休みを直と繋ぎつきで登録し_マスと説明と件数に出る_同じ人の2回目は断る(self) -> None:
        page = self.open("/calendar")
        page.wait_for_selector(_cell(self.day))
        self.assertEqual(self.count_note(page, self.day), "登録 0 件")

        self.choose(page, self.day, "休み")
        page.wait_for_selector(".modal button.worker")
        self.assertEqual(page.inner_text(".modal .modal__step"), "1 / 4")
        # ライン → 班 で束ねてある。L-1 の端末には LVC の人も出る(兼務)、コイルの人は出ない
        lines = page.eval_on_selector_all(".modal .workers__line h3", "(hs) => hs.map((h) => h.textContent)")
        self.assertEqual(sorted(lines), ["L-1", "LVC"])
        groups = page.eval_on_selector_all(".modal .workers__group h4", "(hs) => hs.map((h) => h.textContent)")
        self.assertIn("A班", groups)
        self.assertNotIn("小林コイル", self.modal_text(page))
        self.pick_worker(page, "山田太郎")

        # 直(A/B/C/D 班だけ聞く)
        page.wait_for_selector(".modal .modal__head h2:text-is('山田太郎 さんの直')")
        self.assertEqual(page.inner_text(".modal .modal__step"), "2 / 4")
        page.click(".modal button.choice:has-text('2直')")
        # 残業繋ぎ: どの直か・ここまでに選んだもの
        page.wait_for_selector(".modal .modal__head h2:text-is('1直の残業で繋ぐ人')")
        self.assertEqual(page.inner_text(".modal .modal__tag"), "残業繋ぎ")
        chosen = page.inner_text(".modal .chosen")
        self.assertIn("山田太郎(A班)", chosen)
        self.assertIn("2直", chosen)
        self.pick_worker(page, "加藤眞一")
        # 早出繋ぎ: 直前に選んだ残業繋ぎが出ている
        page.wait_for_selector(".modal .modal__head h2:text-is('3直の早出で繋ぐ人')")
        self.assertEqual(page.inner_text(".modal .modal__tag"), "早出繋ぎ")
        self.assertIn("加藤眞一", page.inner_text(".modal .chosen__item--last"))
        self.press(page, "未登録のまま")
        self.wait_closed(page)

        self.wait_cell(page, self.day, "L-1:A班:山田太郎 休")
        self.assertIn("山田太郎 さんの休みを登録しました", self.toast_text(page))
        tip = page.get_attribute(_cell(self.day), "title") or ""
        self.assertIn("1直残業　加藤眞一", tip)
        self.assertIn("3直早出　未登録", tip)
        self.assertIn("登録1件", page.get_attribute(_cell(self.day), "aria-label"))
        self.assertEqual(self.count_note(page, self.day), "登録 1 件")
        viewer = self.view_text(page, self.day)
        self.assertIn("[休み] L-1 A班 山田太郎 (2直)", viewer)
        self.assertIn("1直残業: 加藤眞一", viewer)
        self.assertIn("3直早出: 未登録", viewer)
        row = self.rows(config.KUBUN_REST)[0]
        self.assertEqual((row["直"], row["残業者"], row["早出者"]), ("2", "加藤眞一", config.UNREGISTERED))

        # 同じ日に同じ人をもう一度 → 分かる文で断り、増えない
        self.choose(page, self.day, "休み")
        self.pick_worker(page, "山田太郎")
        page.click(".modal button.choice:has-text('1直')")
        page.wait_for_selector(".modal .modal__head h2:text-is('3直の残業で繋ぐ人')")
        self.press(page, "未登録のまま")
        page.wait_for_selector(".modal .modal__head h2:text-is('2直の早出で繋ぐ人')")
        self.press(page, "未登録のまま")
        page.wait_for_selector(".modal .modal__head h2:text-is('登録できません')")
        self.assertIn("山田太郎 さんは既に登録済みです", self.modal_text(page))
        self.press(page, "閉じる")
        self.wait_closed(page)
        self.assertEqual(len(self.rows(config.KUBUN_REST)), 1)
        self.assertEqual(self.count_note(page, self.day), "登録 1 件")

        # 直の無い班(昼勤): 直も繋ぎも聞かずにすぐ登録。マウスの説明に繋ぎは出ない
        self.choose(page, self.day, "休み")
        self.pick_worker(page, "佐藤花子")
        self.wait_closed(page)
        self.wait_cell(page, self.day, "L-1:昼勤班:佐藤花子 休")
        tip = page.get_attribute(_cell(self.day), "title") or ""
        self.assertNotIn("佐藤花子", tip)
        self.assertEqual(self.count_note(page, self.day), "登録 2 件")
        row = next(r for r in self.rows(config.KUBUN_REST) if r["識別コード"] == "103")
        self.assertEqual((row["直"], row["残業者"], row["早出者"]), ("", "", ""))
        self.assert_quiet()

    def test_途中で取り消すと何も書かない(self) -> None:
        page = self.open("/calendar")
        for stop in ("worker", "shift", "overtime", "early"):
            with self.subTest(stop=stop):
                self.choose(page, self.day, "休み")
                page.wait_for_selector(".modal button.worker")
                if stop != "worker":
                    self.pick_worker(page, "山田太郎")
                    page.wait_for_selector(".modal button.choice:has-text('1直')")
                if stop in ("overtime", "early"):
                    page.click(".modal button.choice:has-text('1直')")
                    page.wait_for_selector(".modal .modal__tag:text-is('残業繋ぎ')")
                if stop == "early":
                    self.press(page, "未登録のまま")
                    page.wait_for_selector(".modal .modal__tag:text-is('早出繋ぎ')")
                self.press(page, "取り消し")
                self.wait_closed(page)
        self.assertEqual(self.posts_to("/api/rest"), [])
        self.assertEqual(self.rows(), [])
        self.assert_quiet()

    def test_前後の月のマスは押せず_日曜祝日の色と名前_今日の枠(self) -> None:
        page = self.open("/calendar")
        page.wait_for_selector(".day[data-date]")
        cells = page.evaluate("""() => [...document.querySelectorAll('#cal-grid .day')].map((c) => ({
            tag: c.tagName, date: c.dataset.date || '', tone: c.dataset.tone, today: c.dataset.today || '',
            text: c.innerText }))""")
        self.assertEqual(len(cells), 42)
        start = cal.grid_start(self.today.year, self.today.month)
        for i, got in enumerate(cells):
            day = start + _dt.timedelta(days=i)
            with self.subTest(day=day):
                if day.month != self.today.month:
                    # 前後の月: 押せない(button でない)、日付も出さない
                    self.assertEqual((got["tag"], got["date"], got["tone"], got["text"].strip()),
                                     ("DIV", "", "out", ""))
                    continue
                self.assertEqual(got["tag"], "BUTTON")
                name = holiday_name(day)
                tone = ("holiday" if name or day.weekday() == 6 else
                        "sat" if day.weekday() == 5 else "weekday")
                self.assertEqual(got["tone"], tone)
                if name:
                    self.assertIn(name, got["text"])
                self.assertEqual(got["today"], "1" if day == self.today else "")
        # 前後の月のマスを押しても何も開かない
        out = page.locator("#cal-grid div.day").first
        if out.count():
            out.click()
            page.wait_for_timeout(300)
            self.assertEqual(page.locator(".modal").count(), 0)
        # 日曜も押せて、休みを登録できる
        sunday = _sunday(self.today.year, self.today.month)
        self.choose(page, sunday, "休み")
        self.pick_worker(page, "佐藤花子")
        self.wait_closed(page)
        self.wait_cell(page, sunday, "佐藤花子 休")
        self.assert_quiet()


# ---------------------------------------------------------------------------
# 3. 連絡  /  4. 内容閲覧 → 削除
# ---------------------------------------------------------------------------
class CommentDeleteFlowTests(LineTerminalCase):
    def write_comment(self, page, day: _dt.date, group: str, text: str) -> None:
        self.choose(page, day, "コメント・連絡")
        page.wait_for_selector("#cm-group")
        page.select_option("#cm-group", group)
        self.press(page, "次へ")
        page.wait_for_selector("#cm-text")
        page.fill("#cm-text", text)
        self.press(page, "登録する")
        self.wait_closed(page)

    def test_連絡を残すと_マス_説明_内容閲覧に班と時刻が出る_空の本文と打ちかけの取り消し(self) -> None:
        page = self.open("/calendar")
        self.choose(page, self.day, "コメント・連絡")
        page.wait_for_selector("#cm-line")
        self.assertEqual(page.inner_text(".modal .modal__step"), "1 / 2")
        self.assertTrue(page.is_disabled("#cm-line"))
        self.assertEqual(page.input_value("#cm-line"), "L-1")
        self.assertIn("選べるのは作業長の端末だけ", page.inner_text("#cm-line-note"))
        groups = page.eval_on_selector("#cm-group", "(s) => [...s.options].map((o) => o.value)")
        self.assertEqual(sorted(groups), ["A", "B", "昼勤"])
        page.select_option("#cm-group", "B")
        self.press(page, "次へ")
        page.wait_for_selector("#cm-text")
        self.assertIn("L-1 B班からの連絡", self.modal_title(page))
        self.assertEqual(page.inner_text(".modal .modal__step"), "2 / 2")

        # 本文が空のまま「登録する」→ 送らない。黙らずに何をすればよいかを言う
        self.press(page, "登録する")
        page.wait_for_selector("#cm-empty")
        self.assertIn("連絡の内容を入力してから", page.inner_text("#cm-empty"))
        self.assertEqual(self.posts_to("/api/comment"), [])
        self.assertEqual(page.locator("#cm-text").count(), 1)

        # 打ちかけで Esc → 消してよいか訊く。「入力に戻る」で本文が残る
        page.fill("#cm-text", "段取り替え")
        page.keyboard.press("Escape")
        page.wait_for_selector(".modal .modal__head h2:text-is('入力した内容を消しますか?')")
        self.press(page, "入力に戻る")
        page.wait_for_function("document.querySelectorAll('.modal').length === 1")
        self.assertEqual(page.input_value("#cm-text"), "段取り替え")

        page.fill("#cm-text", "2号機の段取り替えをします")
        self.press(page, "登録する")
        self.wait_closed(page)
        self.wait_cell(page, self.day, "L-1:B班 コメント有")
        self.assertIn("L-1 B班からの連絡を登録しました", self.toast_text(page))
        tip = page.get_attribute(_cell(self.day), "title") or ""
        self.assertRegex(tip, r"L-1 B班からの連絡　" + SHOWN_TIME)
        viewer = self.view_text(page, self.day)
        self.assertRegex(viewer, r"\[連絡\] L-1 B班からの連絡　" + SHOWN_TIME)
        self.assertIn("2号機の段取り替えをします", viewer)
        self.assertEqual(self.count_note(page, self.day), "登録 1 件")

        # 同じ班の連絡が2つでも、マスは1行(内容閲覧には2つ)
        self.write_comment(page, self.day, "B", "午後から3号機")
        page.wait_for_function("(sel) => document.querySelector(sel).getAttribute('aria-label').includes('登録2件')",
                               arg=_cell(self.day))
        body = page.inner_text(f"{_cell(self.day)} .day__body")
        self.assertEqual(body.count("コメント有"), 1)
        self.assertIn("午後から3号機", self.view_text(page, self.day))

        # 打ちかけを取り消す → 訊いてから閉じる。書かない
        self.choose(page, self.day, "コメント・連絡")
        self.press(page, "次へ")
        page.wait_for_selector("#cm-text")
        page.fill("#cm-text", "やっぱりやめる")
        self.press(page, "取り消し")
        page.wait_for_selector(".modal .modal__head h2:text-is('入力した内容を消しますか?')")
        self.press(page, "消して閉じる")
        self.wait_closed(page)
        self.assertEqual(len(self.rows(config.KUBUN_OTHER)), 2)
        self.assert_quiet()

    def test_内容閲覧から削除_選ばずに押す_取り消す_選んで消すと履歴に残りマスから消える(self) -> None:
        repo = repository.Repository(self.conn)
        repo.save_record(self.day, config.KUBUN_REST, "山田太郎", "101", shift="2",
                         overtime="加藤眞一", early=config.UNREGISTERED, group="A", line="L-1")
        repo.save_record(self.day, config.KUBUN_REST, "佐藤花子", "103", group="昼勤", line="L-1")
        repo.save_record(self.day, config.KUBUN_OTHER, "2号機の段取り替え", "-", group="B",
                         line="L-1", writer_group="B")
        empty = _workday(self.today.year, self.today.month, avoid=(self.day,))
        page = self.open("/calendar")
        self.wait_cell(page, self.day, "山田太郎 休")
        self.assertEqual(self.count_note(page, self.day), "登録 3 件")

        # 何も無い日: 閲覧は「ありません」、削除は「削除するものがありません」
        self.assertIn("この日に登録データはありません", self.view_text(page, empty))
        self.choose(page, empty, "削除")
        page.wait_for_selector(".modal .modal__head h2:text-is('削除するものがありません')")
        self.press(page, "閉じる")
        self.wait_closed(page)

        self.choose(page, self.day, "削除")
        page.wait_for_selector("#del-list")
        items = page.eval_on_selector_all("#del-list .pick__text", "(xs) => xs.map((x) => x.innerText)")
        self.assertEqual(len(items), 3)
        self.assertTrue(any("[休み] 山田太郎 (2直)" in t and "L-1 A班" in t for t in items), items)
        self.assertTrue(any("[連絡] 2号機の段取り替え" in t and "L-1 B班からの連絡" in t for t in items), items)

        # 何も選ばずに押す → 送らない。黙らずに何をすればよいかを言う(前は何も起きなかった)
        self.press(page, "選んだものを削除")
        page.wait_for_timeout(300)
        self.assertEqual(self.posts_to("/api/delete"), [])
        self.assertEqual(page.locator("#del-list").count(), 1, "窓が閉じた")
        self.assertEqual(page.locator("#del-empty").count(), 1, "何も選ばずに押しても、何も言わない")
        self.assertIn("削除するものにチェックを入れて", page.inner_text("#del-empty"))
        # 2回押しても案内は1つ
        self.press(page, "選んだものを削除")
        page.wait_for_timeout(200)
        self.assertEqual(page.locator("#del-empty").count(), 1)

        # 選んで押す → 最終確認で「やめる」→ 消さない
        page.check("#del-list .pick:has-text('山田太郎') input")
        self.press(page, "選んだものを削除")
        page.wait_for_selector(".modal .modal__head h2:text-is('削除の確認')")
        self.assertIn("チェックした 1 件を削除します", self.modal_text(page))
        self.press(page, "やめる")
        self.wait_closed(page)
        self.assertEqual(self.posts_to("/api/delete"), [])
        self.assertEqual(len(self.rows()), 3)

        # もう一度。今度は消す
        self.choose(page, self.day, "削除")
        page.wait_for_selector("#del-list")
        page.check("#del-list .pick:has-text('山田太郎') input")
        page.check("#del-list .pick:has-text('2号機') input")
        self.press(page, "選んだものを削除")
        page.wait_for_selector(".modal .modal__head h2:text-is('削除の確認')")
        self.assertIn("チェックした 2 件を削除します", self.modal_text(page))
        self.press(page, "削除する")
        self.wait_closed(page)
        self.wait_cell(page, self.day, "山田太郎", gone=True)
        self.assertIn("2 件を削除しました", self.toast_text(page))
        body = page.inner_text(_cell(self.day))
        self.assertIn("佐藤花子 休", body)
        self.assertNotIn("コメント有", body)
        self.assertEqual(page.get_attribute(_cell(self.day), "title") or "", "")
        self.assertEqual(self.count_note(page, self.day), "登録 1 件")
        viewer = self.view_text(page, self.day)
        self.assertNotIn("山田太郎", viewer)
        self.assertIn("佐藤花子", viewer)
        # 消したものは履歴(削除履歴)に残る
        deleted = self.conn.execute(f'SELECT "登録内容" FROM "{config.TABLE_DEL_HISTORY}"').fetchall()
        self.assertEqual(sorted(r[0] for r in deleted), ["2号機の段取り替え", "山田太郎"])
        self.assert_quiet()


def _print_cells(page) -> dict[str, str]:
    """印刷のページの当月のマス {日: 本文}。"""
    return page.evaluate("""() => {
        const out = {};
        for (const td of document.querySelectorAll('tbody td')) {
            const num = td.querySelector('.daynum');
            if (!num) continue;
            const body = td.querySelector('.body');
            out[num.textContent.trim()] = body ? body.innerText.trim() : '';
        }
        return out; }""")


def _screen_cells(page) -> dict[str, str]:
    """画面のマス目の当月のマス {日: 本文}。"""
    return page.evaluate("""() => {
        const out = {};
        for (const c of document.querySelectorAll('#cal-grid .day[data-date], #his-grid .day[data-date]')) {
            const body = c.querySelector('.day__body');
            out[String(Number(c.dataset.date.slice(8, 10)))] = body ? body.innerText.trim() : '';
        }
        return out; }""")


def _pdf_pages(pdf: bytes) -> list[tuple[float, float]]:
    """PDF の各ページの大きさ(pt)。"""
    sizes = []
    for match in re.finditer(rb"/Type\s*/Page[^s].*?/MediaBox\s*\[\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)",
                             pdf, re.DOTALL):
        x0, y0, x1, y1 = (float(v) for v in match.groups())
        sizes.append((x1 - x0, y1 - y0))
    if not sizes:          # MediaBox が Pages 側にある作り
        count = len(re.findall(rb"/Type\s*/Page[^s]", pdf))
        box = re.search(rb"/MediaBox\s*\[\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)", pdf)
        x0, y0, x1, y1 = (float(v) for v in box.groups())
        sizes = [(x1 - x0, y1 - y0)] * count
    return sizes


# ---------------------------------------------------------------------------
# 5. 月の移動・印刷  /  8. 端末のラインでの絞り込み
# ---------------------------------------------------------------------------
class MonthPrintFlowTests(LineTerminalCase):
    def setUp(self) -> None:
        super().setUp()
        repo = repository.Repository(self.conn)
        repo.save_record(self.day, config.KUBUN_REST, "山田太郎", "101", shift="1",
                         overtime="加藤眞一", early="未登録", group="A", line="L-1")
        repo.save_record(self.day, config.KUBUN_REST, "小林コイル", "201", shift="2",
                         overtime="未登録", early="未登録", group="A", line="コイル")
        repo.save_record(self.day, config.KUBUN_OTHER, "HVC宛ての連絡", "-", group="A",
                         line="HVC", writer_group="A")
        repo.save_record(self.day, config.KUBUN_OTHER, "L-1宛ての連絡", "-", group="B",
                         line="L-1", writer_group="B")
        ny, nm = cal.shift_month(self.today.year, self.today.month, 1)
        self.next_day = _workday(ny, nm)
        repo.save_record(self.next_day, config.KUBUN_REST, "加藤眞一", "102", shift="3",
                         group="B", line="L-1")

    def title(self, page) -> str:
        return page.inner_text("#cal-title").strip()

    def wait_title(self, page, year: int, month: int) -> None:
        page.wait_for_function("(t) => document.getElementById('cal-title').textContent.trim() === t",
                               arg=f"{year}年 {month}月")

    def test_前の月_次の月_当月_続けて押す_キー_端の年で止まる(self) -> None:
        y, m = self.today.year, self.today.month
        page = self.open("/calendar")
        self.wait_title(page, y, m)
        page.click("#cal-next")
        ny, nm = cal.shift_month(y, m, 1)
        self.wait_title(page, ny, nm)
        self.wait_cell(page, self.next_day, "加藤眞一 休")
        page.click("#cal-prev")
        page.click("#cal-prev")
        py, pm = cal.shift_month(y, m, -1)
        self.wait_title(page, py, pm)
        page.click("#cal-today")
        self.wait_title(page, y, m)
        # 返事を待たずに3回続けて押しても、3か月先まで進む
        for _ in range(3):
            page.click("#cal-next")
        self.wait_title(page, *cal.shift_month(y, m, 3))
        page.wait_for_timeout(300)
        self.assertEqual(self.title(page), "{}年 {}月".format(*cal.shift_month(y, m, 3)))
        # キーボード(PageUp / PageDown)でも
        page.keyboard.press("PageUp")
        self.wait_title(page, *cal.shift_month(y, m, 2))
        page.keyboard.press("PageDown")
        self.wait_title(page, *cal.shift_month(y, m, 3))

        # 端の年: 2100年12月より先・2000年1月より前へは進ませない(ボタンが押せない)
        page.clock.set_system_time(_dt.datetime(2100, 12, 15, 10, 0))
        page.click("#cal-today")
        self.wait_title(page, 2100, 12)
        self.assertTrue(page.is_disabled("#cal-next"))
        self.assertFalse(page.is_disabled("#cal-prev"))
        page.keyboard.press("PageDown")
        page.wait_for_timeout(400)
        self.assertEqual(self.title(page), "2100年 12月", "2100年12月で翌月を押すと別の月へ飛んだ")
        page.clock.set_system_time(_dt.datetime(2000, 1, 10, 10, 0))
        page.click("#cal-today")
        self.wait_title(page, 2000, 1)
        self.assertTrue(page.is_disabled("#cal-prev"))
        page.keyboard.press("PageUp")
        page.wait_for_timeout(400)
        self.assertEqual(self.title(page), "2000年 1月", "2000年1月で前月を押すと別の月へ飛んだ")
        page.click("#cal-next")
        self.wait_title(page, 2000, 2)
        self.assertFalse(page.is_disabled("#cal-prev"))
        self.assert_quiet()

    def open_print(self, page, button: str = "#cal-print"):
        with page.context.expect_page() as info:
            page.click(button)
        sheet = info.value
        sheet.on("pageerror", lambda e: self.errors.append(f"印刷: {e}"))
        self.watch(sheet)
        sheet.wait_for_load_state("load")
        sheet.wait_for_selector("tbody td")
        return sheet

    def test_印刷はA4横1ページで_中身はマス目と同じ_端末のラインで絞る(self) -> None:
        page = self.open("/calendar")
        self.wait_cell(page, self.day, "山田太郎 休")
        screen = _screen_cells(page)
        sheet = self.open_print(page)
        self.assertIn(f"/print?year={self.today.year}&month={self.today.month}", sheet.url)
        self.assertIn(f"{self.today.year}年 {self.today.month}月", sheet.inner_text("h1"))
        printed = _print_cells(sheet)
        self.assertEqual(printed, screen)
        self.assertEqual(len(printed), cal.month_days(self.today.year, self.today.month))
        whole = sheet.inner_text("table")
        self.assertNotIn("小林コイル", whole)
        self.assertNotIn("HVC", whole)
        self.assertIn("ライン設定: L-1", sheet.inner_text(".footer"))
        # 紙にすると A4 横で1枚(2枚目の白紙ができない)
        sizes = _pdf_pages(sheet.pdf(prefer_css_page_size=True))
        self.assertEqual(len(sizes), 1, sizes)
        w, h = sizes[0]
        self.assertAlmostEqual(w, 841.9, delta=2)
        self.assertAlmostEqual(h, 595.3, delta=2)
        # 「閉じる」で閉じる(画面から開いたタブ)
        from playwright.sync_api import Error as PlaywrightError

        try:
            sheet.click("#do-close", no_wait_after=True)
        except PlaywrightError:
            # 押した瞬間にタブが閉じると、押し終わりを待つ側が「閉じられた」と言う(閉じたなら正しい)
            if not sheet.is_closed():
                raise
        self.wait_until(page, sheet.is_closed, "印刷のタブが閉じる")
        # 次の月を開いて印刷すると、その月
        page.click("#cal-next")
        self.wait_cell(page, self.next_day, "加藤眞一 休")
        sheet = self.open_print(page)
        ny, nm = cal.shift_month(self.today.year, self.today.month, 1)
        self.assertIn(f"{ny}年 {nm}月", sheet.inner_text("h1"))
        self.assertEqual(_print_cells(sheet), _screen_cells(page))
        sheet.close()
        self.assert_quiet()

    def test_端末のラインを画面で切り替えると_マス_説明_閲覧_削除_件数_印刷がそろって変わる(self) -> None:
        page = self.open("/calendar")
        self.wait_cell(page, self.day, "山田太郎 休")

        def seen() -> dict[str, str]:
            body = page.inner_text(_cell(self.day))
            tip = page.get_attribute(_cell(self.day), "title") or ""
            note = self.count_note(page, self.day)
            viewer = self.view_text(page, self.day)
            self.choose(page, self.day, "削除")
            page.wait_for_selector("#del-list")
            deletable = page.inner_text("#del-list")
            page.keyboard.press("Escape")
            self.wait_closed(page)
            sheet = self.open_print(page)
            printed = _print_cells(sheet)[str(self.day.day)]
            sheet.close()
            return {"body": body, "tip": tip, "note": note, "viewer": viewer,
                    "delete": deletable, "print": printed}

        # L-1: コイルの休みも HVC 宛の連絡も、どこにも出ない。件数もそろう(休み1 + 連絡1)
        got = seen()
        for where, text in got.items():
            with self.subTest(line="L-1", where=where):
                self.assertNotIn("小林コイル", text)
                self.assertNotIn("HVC", text)
        self.assertEqual(got["note"], "登録 2 件")
        self.assertIn("登録2件", page.get_attribute(_cell(self.day), "aria-label"))
        self.assertIn("山田太郎", got["viewer"])
        self.assertIn("L-1宛ての連絡", got["viewer"])
        self.assertIn("L-1:B班 コメント有", got["print"])

        # 設定画面で作業長に切り替える(パスワードを聞かれる)→ カレンダーに戻ると全部出る
        self.rail(page, "設定", "#st-line option")
        page.select_option("#st-line", config.LINE_SUPERVISOR)
        page.click("#st-line-save")
        page.wait_for_selector("#admin-pass")
        page.fill("#admin-pass", _web.ADMIN_PASSWORD)
        self.press(page, "続ける")
        self.wait_closed(page)
        page.wait_for_function("document.getElementById('rb-line').textContent.trim() === '作業長'")
        self.rail(page, "カレンダー", ".day[data-date]")
        self.wait_cell(page, self.day, "小林コイル")
        got = seen()
        self.assertEqual(got["note"], "登録 4 件")
        for where in ("body", "viewer", "delete", "print"):
            with self.subTest(line="作業長", where=where):
                self.assertIn("小林コイル", got[where])
                self.assertIn("HVC", got[where])
        self.assertIn("小林コイル", got["tip"])
        self.assertIn("HVC A班からの連絡", got["tip"])
        # 作業長はラインを選べる
        self.choose(page, self.day, "コメント・連絡")
        page.wait_for_selector("#cm-line")
        self.assertFalse(page.is_disabled("#cm-line"))
        self.press(page, "取り消し")
        self.wait_closed(page)

        # コイル: コイルの休みだけ
        user_settings.save_my_line(config.LINE_COIL)
        page.reload()
        self.wait_cell(page, self.day, "小林コイル")
        got = seen()
        self.assertEqual(got["note"], "登録 1 件")
        for where, text in got.items():
            with self.subTest(line="コイル", where=where):
                self.assertNotIn("山田太郎", text)
                self.assertNotIn("宛ての連絡", text)
        self.assert_quiet()


# ---------------------------------------------------------------------------
# 6. 履歴(過去の月)
# ---------------------------------------------------------------------------
class HistoryFlowTests(LineTerminalCase):
    def setUp(self) -> None:
        super().setUp()
        repo = repository.Repository(self.conn)
        y1, m1 = cal.shift_month(self.today.year, self.today.month, -1)
        y2, m2 = cal.shift_month(self.today.year, self.today.month, -2)
        self.last = (y1, m1)
        self.before = (y2, m2)
        self.d10 = _dt.date(y1, m1, 10)
        repo.save_record(self.d10, config.KUBUN_REST, "山田太郎", "101", shift="1",
                         overtime="加藤眞一", early="未登録", group="A", line="L-1")
        repo.save_record(_dt.date(y1, m1, 11), config.KUBUN_REST, "小林コイル", "201",
                         shift="1", group="A", line="コイル")
        repo.save_record(_dt.date(y1, m1, 12), config.KUBUN_OTHER, "先月の連絡", "-", group="B",
                         line="L-1", writer_group="B")
        self.d5 = _dt.date(y2, m2, 5)
        repo.save_record(self.d5, config.KUBUN_REST, "加藤眞一", "102", shift="2",
                         group="B", line="L-1")
        # 今月の登録は履歴に出ない(カレンダーで見る)
        repo.save_record(self.day, config.KUBUN_REST, "佐藤花子", "103", group="昼勤", line="L-1")

    def wait_title(self, page, year: int, month: int, count: int) -> None:
        page.wait_for_function("(t) => document.getElementById('his-title').textContent.trim() === t",
                               arg=f"{year}年 {month}月(登録 {count} 件)")

    def test_月を選び_件数は端末のラインで数え_過去の日は読むだけ(self) -> None:
        cal_page = self.open("/calendar")
        cal_page.wait_for_selector(".day[data-date]")
        self.rail(cal_page, "履歴", "#his-title")
        page = cal_page
        # 先月が開く。件数はこの端末に見えるものだけ(コイルの休みは数えない)
        self.wait_title(page, *self.last, 2)
        self.assertIn("履歴・閲覧のみ", page.inner_text("#main"))
        self.assertTrue(page.is_disabled("#his-next"), "今月へは進めない")
        self.assertNotIn("小林コイル", page.inner_text("#his-grid"))
        self.assertIn("山田太郎", page.inner_text(_cell(self.d10)))
        # 1直の休みを繋ぐのは 3直の残業
        self.assertIn("3直残業　加藤眞一", page.get_attribute(_cell(self.d10), "title") or "")

        # 年月を選ぶ: 月ごとの件数。今月と、登録より前の月は選べない(「—」)
        page.click("#his-pick")
        page.wait_for_selector(".mpick__month")
        y2, m2 = self.before
        if not page.inner_text(".mpick__year").startswith(f"{y2}年"):
            page.click(".mpick__head button:has-text('前の年')")
        tiles = page.evaluate("""() => [...document.querySelectorAll('.mpick__month')].map((t) => ({
            name: t.querySelector('.mpick__name').textContent, count: t.querySelector('.mpick__count').textContent,
            disabled: t.disabled }))""")
        by_month = {t["name"]: t for t in tiles}
        self.assertEqual(by_month[f"{m2}月"]["count"], "1 件")
        self.assertFalse(by_month[f"{m2}月"]["disabled"])
        if m2 > 1:
            self.assertEqual((by_month[f"{m2 - 1}月"]["count"], by_month[f"{m2 - 1}月"]["disabled"]), ("—", True))
        page.click(f".mpick__month:has(.mpick__name:text-is('{m2}月'))")
        self.wait_closed(page)
        self.wait_title(page, y2, m2, 1)
        self.assertTrue(page.is_disabled("#his-prev"), "登録より前へは戻れない")

        # 過去の日を押す → 中身は読める。ボタンは「閉じる」だけ(削除の道が無い)
        page.click(_cell(self.d5))
        page.wait_for_selector(".modal .viewer")
        self.assertIn("加藤眞一 (2直)", page.inner_text(".modal .viewer"))
        self.assertEqual(page.inner_text(".modal .modal__tag"), "閲覧のみ")
        buttons = page.eval_on_selector_all(".modal button", "(bs) => bs.map((b) => b.innerText.trim())")
        self.assertEqual(buttons, ["閉じる"])
        self.press(page, "閉じる")
        self.wait_closed(page)
        page.click("#his-next")
        self.wait_title(page, *self.last, 2)
        page.click(_cell(self.d10))
        page.wait_for_selector(".modal .viewer")
        self.assertIn("3直残業: 加藤眞一", page.inner_text(".modal .viewer"))
        page.keyboard.press("Escape")
        self.wait_closed(page)

        # 印刷も同じ中身
        with page.context.expect_page() as info:
            page.click("#his-print")
        sheet = info.value
        self.watch(sheet)
        sheet.wait_for_selector("tbody td")
        self.assertEqual(_print_cells(sheet), _screen_cells(page))
        sheet.close()
        self.assertEqual(self.writes(), [], "履歴の画面から書き込む要求が出た")
        self.assert_quiet()


# ---------------------------------------------------------------------------
# 7. 設定
# ---------------------------------------------------------------------------
class SettingsFlowTests(LineTerminalCase):
    def setUp(self) -> None:
        super().setUp()
        self.keep_settings()

    def test_画面の色_自動同期_管理者パスワードを保存し_開き直しても残る(self) -> None:
        page = self.open("/settings", wait_for="#st-theme input")
        # 画面の色: ダークを選ぶ → すぐ変わる → 開き直しても残る
        page.check("#st-theme input[value='dark']")
        page.wait_for_function("document.documentElement.dataset.theme === 'dark'")
        page.wait_for_function("document.getElementById('toasts').innerText.includes('ダーク')")
        page.reload()
        page.wait_for_selector("#st-theme input")
        self.assertEqual(page.evaluate("document.documentElement.dataset.theme"), "dark")
        self.assertTrue(page.is_checked("#st-theme input[value='dark']"))
        # 帯のボタンで明るくする → 設定画面の印も合う
        self.assertIn("明るくする", page.inner_text("#theme-toggle"))
        page.click("#theme-toggle")
        page.wait_for_function("document.documentElement.dataset.theme === 'light'")
        page.wait_for_function("document.querySelector(\"#st-theme input[value='light']\").checked")
        self.assertEqual(user_settings.get_theme(), "light")

        # 自動同期: 範囲の外は断る(打った値は残す)→ 正しい値は保存され、開き直しても残る
        page.select_option("#st-auto", "0")
        page.fill("#st-interval", "3")
        page.click("#st-auto-save")
        page.wait_for_selector(".modal .modal__head h2:text-is('保存できません')")
        self.assertIn("5〜3600 秒", self.modal_text(page))
        self.press(page, "閉じる")
        self.wait_closed(page)
        self.assertEqual(page.input_value("#st-interval"), "3")
        self.assertTrue(user_settings.auto_sync_enabled(), "断ったのに入切が変わった")
        page.fill("#st-interval", "45")
        page.click("#st-auto-save")
        page.wait_for_function("document.getElementById('toasts').innerText.includes('同期の設定を変更しました')")
        self.assertEqual((user_settings.auto_sync_enabled(), user_settings.sync_interval()), (False, 45))
        page.reload()
        page.wait_for_selector("#st-theme input")
        page.wait_for_function("document.getElementById('st-interval').value === '45'")
        self.assertEqual(page.input_value("#st-auto"), "0")

        # 管理者パスワード: 確認用が違う → そばに理由。合っていれば変わり、開き直しても残る
        self.assertEqual(page.inner_text("#st-pw-pill").strip(), "既定のまま")
        page.fill("#st-pw-now", _web.ADMIN_PASSWORD)
        page.fill("#st-pw-new", "kaizen1")
        page.fill("#st-pw-confirm", "kaizen2")
        page.click("#st-pw-save")
        page.wait_for_selector("#st-pw-why:not([hidden])")
        why = page.inner_text("#st-pw-why")
        self.assertTrue(why.strip(), "理由が空")
        page.fill("#st-pw-confirm", "kaizen1")
        page.click("#st-pw-save")
        page.wait_for_function("document.getElementById('st-pw-pill').textContent.includes('変えてあります')")
        self.assertTrue(page.is_hidden("#st-pw-why"))
        self.assertEqual(page.input_value("#st-pw-new"), "", "打った値が残っている")
        page.reload()
        page.wait_for_function("document.getElementById('st-pw-pill').textContent.includes('変えてあります')")
        # 前のパスワードではラインを変えられない。新しいパスワードで変えられる
        page.select_option("#st-line", "HVC")
        page.click("#st-line-save")
        page.wait_for_selector("#admin-pass")
        page.fill("#admin-pass", _web.ADMIN_PASSWORD)
        self.press(page, "続ける")
        page.wait_for_selector(".modal .modal__head h2:text-is('管理者パスワードが違います')")
        page.fill("#admin-pass", "kaizen1")
        self.press(page, "続ける")
        self.wait_closed(page)
        page.wait_for_function("document.getElementById('rb-line').textContent.trim() === 'HVC'")
        # 既定に戻す
        page.fill("#st-pw-now", "kaizen1")
        page.click("#st-pw-reset")
        page.wait_for_function("document.getElementById('st-pw-pill').textContent.includes('既定のまま')")
        self.assert_quiet()

    def test_そのほかの面_参照_取り込み_今すぐ同期_ログ_配布設定の断り(self) -> None:
        page = self.open("/settings", wait_for="#st-theme input")
        # 参照: フォルダを選ぶ窓が開き、取り消せる
        page.click("#st-data-browse")
        page.wait_for_selector(".modal .browse__path")
        page.wait_for_function("document.querySelector('.modal .browse__path').textContent.length > 0")
        self.press(page, "取り消し")
        self.wait_closed(page)
        # 取り込み: マスタDB が無い → 保存用DBだけ取り込み、無いほうを言う
        page.click("#st-import-all")
        page.wait_for_function("document.getElementById('toasts').innerText.includes('見つかりませんでした')")
        # 今すぐ同期
        page.click("#sync-now")
        page.wait_for_function("document.getElementById('toasts').innerText.includes('同期しました')")
        # ログ: 記録番号なし・無い番号 → 分かる文
        page.click("#st-log-report")
        page.wait_for_selector(".modal .modal__head h2:text-is('記録番号が要ります')")
        self.press(page, "閉じる")
        self.wait_closed(page)
        page.fill("#st-log-ref", "E20000101-000000-XXX")
        page.click("#st-log-report")
        page.wait_for_selector(".modal .modal__head h2:text-is('見つかりません')")
        self.press(page, "閉じる")
        self.wait_closed(page)
        page.click("#st-log-recent")
        page.wait_for_selector("#st-log-list > *")
        # 配布設定: パスワードなしで書き出す → そばに理由。何も書かない
        page.click("#st-dist-export")
        page.wait_for_selector("#st-dist-why:not([hidden])")
        self.assertIn("パスワード", page.inner_text("#st-dist-why"))
        self.assertEqual(page.inner_text("#st-dist-state").strip(), "なし")
        # いまの状態・置き場所が出ている
        self.assertIn("送信待ち", page.inner_text("#st-status"))
        self.assertTrue(page.inner_text("#st-storage").strip())
        self.assert_quiet()


# ---------------------------------------------------------------------------
# 9. 送れない・読めない場面: 文が分かり、入力は残り、画面は固まらない
# ---------------------------------------------------------------------------
class FailureFlowTests(LineTerminalCase):
    def expected_failures(self, *paths: str) -> None:
        """わざと 5xx にした要求の「Failed to load resource」だけを除く(ほかの誤りは残す)。"""
        self.console = [c for c in self.console
                        if not (c.startswith("Failed to load resource: the server responded with a status of 5")
                                and any(f"{ORIGIN}{p}" in c for p in paths))]

    def test_送れなかった休み_連絡_削除_月の読み込み_どれも入力を残して言い_やり直せる(self) -> None:
        page = self.open("/calendar")
        page.wait_for_selector(_cell(self.day))
        label = f"{self.day.month}月{self.day.day}日"

        # 休み: 送れない → 選んだ内容を添えて「もう一度送る」。通れば1行だけ
        self.refuse["/api/rest"] = 503
        self.choose(page, self.day, "休み")
        self.pick_worker(page, "佐藤花子")
        page.wait_for_selector(".modal .modal__head h2:text-is('まだ登録していません')")
        text = self.modal_text(page)
        self.assertIn("試験で断りました", text)
        self.assertIn(f"選んだ内容: 佐藤花子 さん {label} の休み", text)
        del self.refuse["/api/rest"]
        self.press(page, "もう一度送る")
        self.wait_closed(page)
        self.wait_cell(page, self.day, "佐藤花子 休")
        self.assertEqual(len(self.rows(config.KUBUN_REST)), 1)
        # 送り直しは同じ札(返事だけ届かなかった登録を2行にしない)
        ids = {b["submit_id"] for b in self.posts_to("/api/rest")}
        self.assertEqual(len(ids), 1)

        # 「やめる」も選べる(書かない)
        self.refuse["/api/rest"] = 500
        self.choose(page, self.day, "休み")
        self.pick_worker(page, "加藤眞一")
        page.click(".modal button.choice:has-text('1直')")
        self.press(page, "未登録のまま")
        page.wait_for_selector(".modal .modal__tag:text-is('早出繋ぎ')")
        self.press(page, "未登録のまま")
        page.wait_for_selector(".modal .modal__head h2:text-is('まだ登録していません')")
        self.assertIn("加藤眞一 さん(1直)", self.modal_text(page))
        self.press(page, "やめる")
        self.wait_closed(page)
        del self.refuse["/api/rest"]
        self.assertEqual(len(self.rows(config.KUBUN_REST)), 1)

        # 連絡: 送れない → 本文を持ったまま開き直し、理由を上に出す
        self.choose(page, self.day, "コメント・連絡")
        page.select_option("#cm-group", "A")
        self.press(page, "次へ")
        page.fill("#cm-text", "送れないかもしれない連絡")
        # (道の頭で断るので、班の一覧 /api/comment-targets を読んだあとに断り始める)
        self.refuse["/api/comment"] = 500
        self.press(page, "登録する")
        page.wait_for_selector("#cm-note")
        self.assertIn("入力はそのまま残してあります", page.inner_text("#cm-note"))
        self.assertEqual(page.input_value("#cm-text"), "送れないかもしれない連絡")
        del self.refuse["/api/comment"]
        self.press(page, "登録する")
        self.wait_closed(page)
        self.wait_cell(page, self.day, "L-1:A班 コメント有")
        self.assertEqual(len(self.rows(config.KUBUN_OTHER)), 1)

        # 削除: 送れない → 知らせが出て、消えていない。画面はそのまま使える
        self.refuse["/api/delete"] = 500
        self.choose(page, self.day, "削除")
        page.check("#del-list .pick:has-text('佐藤花子') input")
        self.press(page, "選んだものを削除")
        self.press(page, "削除する")
        self.wait_closed(page)
        page.wait_for_selector(".toast--error")
        self.assertIn("試験で断りました", page.inner_text(".toast--error"))
        del self.refuse["/api/delete"]
        self.assertIn("佐藤花子 休", page.inner_text(_cell(self.day)))
        self.assertEqual(len(self.rows(config.KUBUN_REST)), 1)

        # 月の読み込み: 読めない → 知らせ、いまの月のまま。もう一度押せば進む
        title = page.inner_text("#cal-title")
        self.refuse["/api/calendar"] = 500
        page.click("#cal-next")
        page.wait_for_function("document.querySelectorAll('.toast--error').length >= 2")
        self.assertEqual(page.inner_text("#cal-title"), title)
        del self.refuse["/api/calendar"]
        page.click("#cal-next")
        page.wait_for_function("(t) => document.getElementById('cal-title').textContent !== t", arg=title)

        self.expected_failures("/api/rest", "/api/comment", "/api/delete", "/api/calendar")
        self.assert_quiet()


class MonthEdgeTests(unittest.TestCase):
    """端の月で「翌月」「前月」を押しても、別の月へ飛ばない(ブラウザ無しでも回る確かめ)。"""

    def test_端を越えたら端の月で止まる(self) -> None:
        self.assertEqual(cal.clamp(cal.MAX_YEAR, 13), (cal.MAX_YEAR, 12))
        self.assertEqual(cal.clamp(cal.MIN_YEAR, 0), (cal.MIN_YEAR, 1))
        self.assertEqual(cal.shift_month(cal.MAX_YEAR, 12, 1), (cal.MAX_YEAR, 12))
        self.assertEqual(cal.shift_month(cal.MIN_YEAR, 1, -1), (cal.MIN_YEAR, 1))
        self.assertEqual(cal.clamp(3000, 5), (cal.MAX_YEAR, 12))
        self.assertEqual(cal.clamp(1800, 5), (cal.MIN_YEAR, 1))
        # 端でなければ今までどおり
        self.assertEqual(cal.clamp(2026, 13), (2027, 1))
        self.assertEqual(cal.clamp(2026, 0), (2025, 12))

if __name__ == "__main__":
    unittest.main()
