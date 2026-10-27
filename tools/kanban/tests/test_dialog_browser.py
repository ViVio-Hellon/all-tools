"""確かめの窓(「この資材は届いていますか？」など)を、画面の中に出す。

    確かめの窓(`confirm()`)を出したまま約90秒置くと終わる

ブラウザ標準の確かめの窓を出しているあいだは、画面のタイマーが全部止まる ── 心拍
(``/api/alive``)も止まる。サーバは心拍が 20 秒途切れた画面を一覧から外し
(``kanban/screen.py``)、そこから 90 秒心拍が無いと終わる(``kanban/idle_exit.py``)。
実際に、窓を出して 91 秒でサーバ(ブラウザ版)が終わり、そのあと「OK」を押すと
「繋がりません」が出た。

いまは画面の中に窓を出す(``app/static/js/dialog.js``)。ここで確かめること:

    - 窓を出したまま置いても心拍が続き、**サーバが終わらない**(待ち時間を短くした
      見張りで。戻すと心拍が止まり、見張りがサーバを止める)
    - Enter で決める・Esc でやめる。焦点は既定のボタンへ移り、閉じたら押した升へ戻る
    - 消すなど危ない操作は「やめる」が既定(Enter を続けて押しただけでは消えない)
    - コメントの窓の上で Esc を押しても、上の確かめの窓だけが閉じる
    - ライト・ダークのどちらでも、窓は画面の色(``--surface``・``--fg``)で出る
    - 看板の JS が、ブラウザ標準の確かめの窓(confirm・alert・prompt)を呼ばない

本物の Chromium で動かす。待ち合わせは時間ではなく画面・要求の状態で待つ。
"""
from __future__ import annotations

import re
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests._kanban_browser import KanbanBrowserCase
from tests.test_web_app import sample_rows
from kanban.db.store import DEFAULT_COLUMN_MAP

JS_DIR = Path(__file__).resolve().parents[1] / "app" / "static" / "js"

SIZE_2 = '#board .row[data-no="2"] .cell[data-kind="size"]'
BOX = ".ask__box"


class _ShippedRowCase(KanbanBrowserCase):
    """2 番(発注中)を倉庫が発送した盤。サイズを押すと「この資材は届いていますか？」と訊く。"""

    def setUp(self) -> None:
        super().setUp()
        rows = sample_rows()
        rows[1]["shipped"] = "〇"
        self.store.import_line(
            line="LVC", table_name="看板_LVC", key_column="管理番号", key_category="TEXT",
            column_map=dict(DEFAULT_COLUMN_MAP),
            categories={name: "TEXT" for name in DEFAULT_COLUMN_MAP.values()},
            rows=rows, source_path="dummy.accdb", keep_local_changes=False)

    def open_board(self):
        page = self.open("/board")
        page.wait_for_selector('#board .row[data-no="2"] .cell--ship[data-state="shipped"]')
        return page

    def orders(self) -> list[dict]:
        return self.posted("/api/board/order")


class HeartbeatWhileAskingTest(_ShippedRowCase):
    """確かめの窓を出したまま手を止めても、心拍が続いてサーバが終わらない。"""

    #: 答えずに置く(出したまま手を止めた人)
    accept_dialogs = None

    #: 心拍の間隔(ミリ秒)。本物は 5 秒。試験では縮める(画面へはサーバが渡す)
    BEAT_MS = 200

    def setUp(self) -> None:
        from kanban import idle_exit

        super().setUp()
        patch = mock.patch.object(idle_exit, "HEARTBEAT_MS", self.BEAT_MS)
        patch.start()
        self.addCleanup(patch.stop)
        self.stopped = threading.Event()
        # 本物は「20 秒で一覧から外し、そこから 90 秒で終わる」。同じ関係で縮める
        self.watch = idle_exit.install(
            self.stopped.set, lambda: "",
            idle_sec=1.2, grace_sec=0.5, tick_sec=0.05, stale_sec=0.5, resume_grace_sec=0.0)
        self.held: list = []

    def watch_page(self, page):
        # 戻したとき(ブラウザ標準の窓)は、**答えずに置く**(閉じると心拍が戻ってしまい、
        # 不具合が見えない)。片付けで閉じる
        page.on("pageerror", lambda e: self.errors.append(str(e)))

        def hold(dialog) -> None:
            self.native_dialogs.append(f"{dialog.type}: {dialog.message}")
            self.held.append(dialog)
        page.on("dialog", hold)
        self.addCleanup(self._dismiss_held)
        return page

    def _dismiss_held(self) -> None:
        for dialog in self.held:
            try:
                dialog.dismiss()
            except Exception:                       # noqa: BLE001 - もう閉じた画面
                pass

    def beats(self) -> int:
        return sum(1 for method, path, _ in self.sent
                   if method == "POST" and path.startswith("/api/alive"))

    def test_確かめの窓を出したままでも心拍が続き_サーバは終わらない(self) -> None:
        page = self.watch_page(self.ctx.new_page())
        page.goto("http://kanban.test/board", wait_until="load")
        page.wait_for_selector('#board .row[data-no="2"] .cell--ship[data-state="shipped"]')
        self.wait_until(page, lambda: self.watch.connected, "画面が繋がった状態")

        # 押したあとで窓を出す(``page.click`` は、戻したとき標準の窓に止められて返らない)
        page.evaluate("(sel) => { setTimeout(() => document.querySelector(sel).click(), 0); }", SIZE_2)
        self.wait_until(page, lambda: self.dialogs or self.held, "届いたかを訊いた状態")

        # 見張りが「誰も居ない」と読むまで(一覧から外れる 0.5 秒 + 終わるまで 1.2 秒)より
        # ずっと長いあいだ、窓を出したまま置く。**時間ではなく心拍の数で待つ**
        start = self.beats()
        want = 15                     # 200ms ごとに 15 回 = 3 秒(見張りの 1.7 秒より長い)
        self.wait_until(page, lambda: self.beats() - start >= want or self.stopped.is_set(),
                        f"窓を出したまま心拍が {want} 回届いた状態(いま {self.beats() - start} 回)",
                        timeout=30.0)
        self.assertFalse(self.stopped.is_set(),
                         f"確かめの窓を出しているあいだに、サーバが終わった(心拍 {self.beats() - start} 回。"
                         f"窓: {self.native_dialogs or self.dialogs})")
        self.assertIsNone(self.watch.overdue())
        self.assertEqual(self.native_dialogs, [], "ブラウザ標準の確かめの窓を出した")
        self.assertIn("届いていますか", self.dialogs[0])
        self.assertTrue(page.locator(BOX).is_visible(), "窓が消えた")
        self.assertEqual(self.orders(), [], "答える前に送った")

        # 置いたあとで「届いた」を押す → 送れて、両方戻る(「繋がりません」にならない)
        page.click(f'{BOX} [data-ask="yes"]')
        page.wait_for_selector(BOX, state="detached")
        page.wait_for_selector(f'{SIZE_2}[data-state="neutral"]')
        self.assertTrue(self.orders()[-1].get("delivered"), self.orders())
        self.assertFalse(self.stopped.is_set())


class AskKeysAndFocusTest(_ShippedRowCase):
    """標準の窓と同じ約束: Enter で決める・Esc でやめる・焦点の行き来。"""

    accept_dialogs = None

    def ask_delivered(self, page) -> None:
        page.click(SIZE_2)
        page.wait_for_selector(BOX)
        self.assertIn("届いていますか", self.text(page, f"{BOX} .ask__msg"))
        self.assertIn("「届いた」を押すと", self.text(page, f"{BOX} .ask__msg"),
                      "文言が古いボタン名([OK])のまま")

    @staticmethod
    def focused(page) -> str:
        return page.evaluate("""() => { const el = document.activeElement;
            return el ? (el.dataset.ask || el.dataset.kind || el.id || el.tagName) : ''; }""")

    def test_Escでやめると何も送らず_焦点は押した升へ戻る(self) -> None:
        page = self.open_board()
        self.ask_delivered(page)
        self.assertEqual(self.focused(page), "yes", "既定のボタン(届いた)に焦点が無い")
        self.assertEqual(self.text(page, f'{BOX} [data-ask="yes"]'), "届いた")
        self.assertEqual(self.text(page, f'{BOX} [data-ask="no"]'), "やめる")
        page.keyboard.press("Escape")
        page.wait_for_selector(BOX, state="detached")
        self.assertEqual(self.focused(page), "size", "閉じたあと、押した升へ焦点が戻らない")
        self.assertEqual(page.get_attribute(SIZE_2, "data-state"), "ordered", "やめたのに戻した")
        # やめたあとも升は押せる(押している最中の印が残っていない)
        self.ask_delivered(page)
        page.click(f'{BOX} [data-ask="no"]')
        page.wait_for_selector(BOX, state="detached")
        self.assertEqual(self.orders(), [], "やめたのに送った")

    def test_Enterで決める(self) -> None:
        page = self.open_board()
        self.ask_delivered(page)
        page.keyboard.press("Enter")
        page.wait_for_selector(BOX, state="detached")
        page.wait_for_selector(f'{SIZE_2}[data-state="neutral"]')
        self.assertEqual(len(self.orders()), 1, self.orders())
        self.assertTrue(self.orders()[0].get("delivered"))

    def test_窓の外を押しても答えたことにならず_後ろの升も押せない(self) -> None:
        page = self.open_board()
        self.ask_delivered(page)
        page.mouse.click(5, 5)                           # 覆いの隅
        self.assertTrue(page.locator(BOX).is_visible())
        self.assertEqual(self.focused(page), "yes", "焦点が窓の外へ出た")
        page.keyboard.press("Tab")
        self.assertEqual(self.focused(page), "no")
        page.keyboard.press("Tab")
        self.assertEqual(self.focused(page), "yes", "Tab で窓の外へ出た")
        page.keyboard.press("Shift+Tab")
        self.assertEqual(self.focused(page), "no")
        page.keyboard.press("Enter")                     # 焦点のあるボタン(やめる)
        page.wait_for_selector(BOX, state="detached")
        self.assertEqual(self.orders(), [])

    def test_危ない操作はやめるが既定で_Enterでは決めない(self) -> None:
        page = self.open_board()
        page.evaluate("""async () => {
            const d = await import('/static/js/dialog.js');
            window.__answer = 'open';
            d.ask('この看板の書きかけ(下書き)を消しますか？', { ok: '消す', cancel: 'やめる', danger: true })
              .then((v) => { window.__answer = v; });
        }""")
        page.wait_for_selector(BOX)
        self.assertEqual(self.focused(page), "no", "危ない操作なのに「消す」が既定")
        self.assertIn("btn--danger", page.get_attribute(f'{BOX} [data-ask="yes"]', "class"))
        page.keyboard.press("Enter")
        page.wait_for_function("window.__answer !== 'open'")
        self.assertIs(page.evaluate("window.__answer"), False)

    def test_コメントの窓の上でEscを押すと_確かめの窓だけが閉じる(self) -> None:
        page = self.open_board()
        page.click('#board .row[data-no="4"] .cell--comment')          # 発注中(まだ発送していない)
        page.wait_for_selector("#comment-modal:not([hidden])")
        page.fill("#cm-body", "急ぎでお願いします")
        page.keyboard.press("Escape")                    # コメントの窓を閉じようとする → 訊く
        page.wait_for_selector(BOX)
        self.assertIn("まだ送っていないコメント", self.text(page, f"{BOX} .ask__msg"))
        page.keyboard.press("Escape")                    # 確かめの窓だけを閉じる(書き続ける)
        page.wait_for_selector(BOX, state="detached")
        self.assertTrue(self.visible(page, "#comment-modal"), "Esc がコメントの窓まで閉じた")
        self.assertEqual(page.evaluate("document.activeElement.id"), "cm-body")
        page.keyboard.press("Escape")
        page.wait_for_selector(BOX)
        page.keyboard.press("Enter")                     # 閉じる(下書きは残る)
        page.wait_for_selector("#comment-modal", state="hidden")

    def test_ライトでもダークでも画面の色で出る(self) -> None:
        page = self.open_board()
        self.ask_delivered(page)
        got = {}
        for theme in ("light", "dark"):
            got[theme] = page.evaluate("""(theme) => {
                document.documentElement.setAttribute('data-theme', theme);
                const probe = document.createElement('div');
                probe.style.background = 'var(--surface)';
                probe.style.color = 'var(--fg)';
                document.body.appendChild(probe);
                const want = getComputedStyle(probe);
                const box = getComputedStyle(document.querySelector('.ask__box'));
                const out = { bg: box.backgroundColor, fg: box.color,
                              wantBg: want.backgroundColor, wantFg: want.color };
                probe.remove();
                return out;
            }""", theme)
            self.assertEqual(got[theme]["bg"], got[theme]["wantBg"], f"{theme}: 窓の地が画面の色でない")
            self.assertEqual(got[theme]["fg"], got[theme]["wantFg"], f"{theme}: 窓の文字が画面の色でない")
        self.assertNotEqual(got["light"]["bg"], got["dark"]["bg"], "ダークでも白い窓のまま")


class NoNativeDialogsTest(unittest.TestCase):
    """看板の JS は、ブラウザ標準の確かめの窓(confirm・alert・prompt)を呼ばない。

    出しているあいだ心拍が止まり、サーバが終わってしまう(このファイルの冒頭)。
    確かめたいときは ``js/dialog.js`` の ``ask``(知らせるだけなら ``tell``)を使う。
    """

    CALL = re.compile(r"(?<![\w$.])(?:window\s*\.\s*)?(confirm|alert|prompt)\s*\(")

    @staticmethod
    def code_only(text: str) -> str:
        """コメントを除く(説明に「confirm()」と書くのはかまわない)。"""
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        return "\n".join(re.sub(r"(^|\s)//.*$", r"\1", line) for line in text.splitlines())

    def test_看板のJSは標準の確かめの窓を呼ばない(self) -> None:
        found = []
        for path in sorted(JS_DIR.rglob("*.js")):
            for no, line in enumerate(self.code_only(path.read_text(encoding="utf-8")).splitlines(), 1):
                if self.CALL.search(line):
                    found.append(f"{path.relative_to(JS_DIR)}:{no}: {line.strip()}")
        self.assertEqual(found, [], "ブラウザ標準の確かめの窓を呼んでいる(dialog.js の ask・tell を使う)")

    def test_見張りは呼んだのを見つける(self) -> None:
        for bad in ("if (!confirm('消しますか')) return;", "okToClose = window.confirm(x);",
                    "  alert(message);", "const v = prompt('名前');"):
            self.assertTrue(self.CALL.search(self.code_only(bad)), bad)
        for fine in ("await ask('消しますか')", "confirmComments(nos)", "// confirm() は使わない",
                     "x.confirm(1)", "/* alert(1) */"):
            self.assertFalse(self.CALL.search(self.code_only(fine)), fine)


if __name__ == "__main__":
    unittest.main()
