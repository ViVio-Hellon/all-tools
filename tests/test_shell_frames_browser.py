"""入口: **大きなタブの画面と各ツールの枠のやりとり**を本物のブラウザで(段階1)

処理が止まった知らせ・ブラウザ版の終わった知らせ・説明書のキー・画面の色の既定・
ファイルのドラッグ&ドロップ・ファイルを送る・確認ダイアログ。偽の外枠と偽のツールの画面
(`tests/_shell_browser.py`)で、利用者に見えるもの(知らせ・欄の値・読み直したか)と、
外枠への頼み・ツールの画面が受けた合図を見る。

以前は字を探す試験だけでした(`test_shell_contract.py` の LostToolTests・ManualAndEndTests・
DropTests・UploadTests・DialogTests、`test_tool_contracts.py` の ThemeDefaultTests、
`test_manuals.py` の ShellTests)。
"""
from __future__ import annotations

import unittest
from pathlib import Path

from tests._shell_browser import DesktopShellBrowserCase, ShellBrowserCase

ROOT = Path(__file__).resolve().parent.parent


class LostToolTests(DesktopShellBrowserCase):
    """処理(Python)が止まったツールの画面を、黙って読み直さない・消さない。"""

    def test_ツールが落ちても枠は読み直さず知らせを重ね_もう一度開くは確かめてから(self) -> None:
        page = self.open_shell(first="kanban")
        tool = self.tool_frame(page, "kanban")
        tool.fill("#typed", "打ちかけ")
        loads = self.tool_loads["kanban"]
        page.evaluate("window.__shell.toolLost('kanban')")
        bar = page.locator("#panel-kanban .lostbar")
        bar.wait_for()
        self.assertIn("資材発注看板システムの処理が止まりました", bar.inner_text())
        self.assertEqual(tool.input_value("#typed"), "打ちかけ", "画面を読み直した(打ちかけが消える)")
        self.assertEqual(self.tool_loads["kanban"], loads)
        self.assertIs(tool.evaluate("window.__ALLTOOLS__.pythonLost"), True, "ツールの画面へ知らせていない")
        # 「もう一度開く」は読み直す前に確かめる。やめれば起こし直さない
        self.answers_for_dialogs = [False]
        bar.get_by_role("button", name="もう一度開く").click()
        self.wait_until(page, lambda: self.dialogs, "確かめの窓")
        self.assertIn("看板の画面を読み直します", self.dialogs[0])
        page.wait_for_timeout(200)
        self.assertEqual(self.invokes("shell_restart_tool"), [])
        bar.get_by_role("button", name="もう一度開く").click()
        self.wait_until(page, lambda: self.invokes("shell_restart_tool"), "起こし直しの頼み")
        self.assertEqual(self.invokes("shell_restart_tool"), [{"tool": "kanban"}])
        # 外枠が起こし直した → そのタブだけ読み直し、知らせを外す
        page.evaluate("window.__shell.reloadTool('kanban')")
        self.wait_until(page, lambda: self.tool_loads["kanban"] == loads + 1, "読み直し")
        page.wait_for_selector("#panel-kanban .lostbar", state="detached")
        # いま出していないタブのツールが止まったら、お知らせで伝える
        page.evaluate("window.__shell.toolLost('inspection')")
        page.wait_for_function("document.getElementById('toasts').textContent.includes('点検表 の処理が止まりました')")
        self.assertEqual(self.errors, [])

    def test_入口が落ちても窓ごと読み直さない_大設定に知らせを出す(self) -> None:
        page = self.open_shell(first="settings", wait_tools=("nippou",))
        page.fill("#loc-folder", r"\\srv\打ちかけ")
        state = page.locator("#portal-state")
        page.evaluate("window.__shell.portalLost()")
        state.wait_for()
        self.assertIn("起こし直しています", state.inner_text())
        page.evaluate("window.__shell.portalBack()")
        page.wait_for_function("document.getElementById('portal-state').textContent.includes('起こし直しました')")
        page.evaluate("window.__shell.portalDown('理由の文')")
        page.wait_for_function("document.getElementById('portal-state').textContent.includes('(理由の文)')")
        state.get_by_role("button", name="もう一度開く").click()
        self.wait_until(page, lambda: self.invokes("shell_restart_tool"), "起こし直しの頼み")
        self.assertEqual(self.invokes("shell_restart_tool"), [{"tool": "portal"}])
        self.assertEqual(page.evaluate("performance.getEntriesByType('navigation').length"), 1)
        self.assertEqual(page.input_value("#loc-folder"), r"\\srv\打ちかけ", "大設定の打ちかけが消えた")
        self.assertEqual(self.tool_loads["nippou"], 1, "ツールの画面を読み直した")


class BrowserEndedTests(ShellBrowserCase):
    """ブラウザ版: ツールが答えないだけでは終わったとしない。終わっても枠は消さない。"""

    def poll(self, page, count: int) -> None:
        """状態の問い合わせ(5 秒ごと)を、時計を進めて `count` 回まわす。"""
        for _ in range(count):
            before = sum(1 for _, p, _ in self.sent if p.startswith("/api/tools/status"))
            page.clock.fast_forward(5000)
            self.wait_until(page, lambda: sum(1 for _, p, _ in self.sent if p.startswith("/api/tools/status")) > before,
                            "状態の問い合わせ")
            page.wait_for_timeout(150)

    def test_印が消えるまで終わったとしない_終わっても枠は消さず知らせを重ねる(self) -> None:
        self.ctx.clock.install()
        page = self.open_shell(first="kanban")
        tool = self.tool_frame(page, "kanban")
        tool.fill("#typed", "打ちかけ")
        self.tools.states["kanban"] = {"running": False, "ended": False}     # 重い処理の途中で答えない
        self.poll(page, 3)
        self.assertEqual(page.locator("#panel-kanban .lostbar").count(), 0, "答えないだけで終わったとした")
        self.assertEqual(page.get_attribute("#tab-kanban .bigtab__dot", "data-phase"), "starting")
        self.tools.states["kanban"] = {"running": False, "ended": True}
        self.poll(page, 2)
        bar = page.locator("#panel-kanban .lostbar")
        bar.wait_for()
        self.assertIn("資材発注看板システムは終了しました", bar.inner_text())
        self.assertEqual(tool.input_value("#typed"), "打ちかけ", "枠を消した・読み直した")
        opened = len(self.posts("/api/tools/kanban/open"))
        bar.get_by_role("button", name="もう一度開く").click()
        self.wait_until(page, lambda: len(self.posts("/api/tools/kanban/open")) > opened, "開き直し")
        self.assertIn("読み直します", self.dialogs[0])


class ManualTests(ShellBrowserCase):
    """操作説明書: いまのタブの説明書を重ねて開き、Esc・F1 で閉じる(説明書の中にいても)。"""

    def manual_page(self, page) -> str:
        return page.evaluate("document.getElementById('manual-frame').contentWindow.location.pathname")

    def test_説明書の中でもEscとF1で閉じ_開いたままタブを替えてもツールへ入らない(self) -> None:
        page = self.open_shell(first="kanban", wait_tools=("nippou",))
        page.click("#help")
        page.wait_for_selector("#manual:not([hidden])")
        page.wait_for_function("document.getElementById('manual-frame').contentDocument?.readyState === 'complete'"
                               " && document.getElementById('manual-frame').contentWindow.location.pathname !== 'blank'")
        self.assertEqual(self.manual_page(page), "/static/manual/kanban.html")
        manual = page.frame_locator("#manual-frame")
        for key in ("Escape", "F1"):
            with self.subTest(key=key):
                manual.locator("body").click(position={"x": 30, "y": 300})   # キーは説明書のページへ行く
                manual.locator("body").press(key)
                page.wait_for_selector("#manual", state="hidden")
                page.click("#help")
                page.wait_for_selector("#manual:not([hidden])")
        # 開いたまま隣のタブへ。説明書もそのタブのものに替わり、キーは外枠に届いたまま
        page.click("#tab-nippou")
        page.wait_for_function("document.getElementById('manual-frame').contentWindow.location.pathname"
                               " === '/static/manual/nippou.html'")
        page.wait_for_timeout(400)
        page.keyboard.press("Escape")
        page.wait_for_selector("#manual", state="hidden")
        page.focus("#help")                               # 大きなタブの帯の F1 で開け閉めする
        page.keyboard.press("F1")
        page.wait_for_selector("#manual:not([hidden])")
        page.click("#manual-index")
        page.wait_for_function("document.getElementById('manual-frame').contentWindow.location.pathname"
                               " === '/static/manual/index.html'")
        self.assertEqual(self.errors, [])


class DesktopManualTests(DesktopShellBrowserCase):
    def test_ツールの画面の中のF1でそのツールの説明書を開く(self) -> None:
        page = self.open_shell(first="calendar")
        tool = self.tool_frame(page, "calendar")
        tool.click("#typed")
        tool.press("#typed", "F1")
        page.wait_for_selector("#manual:not([hidden])")
        page.wait_for_function("document.getElementById('manual-frame').contentWindow.location.pathname"
                               " === '/static/manual/calendar.html'")
        # 大きなタブに各ツールの版を出す(説明書の版と同じもの)
        self.assertEqual(page.inner_text("#tab-calendar .bigtab__ver"),
                         f"VER{self.catalog.by_id('calendar').version()}")


class ThemeTests(ShellBrowserCase):
    """大設定の「画面の色の既定」を、枠を開くたびと変えたときに各ツールの画面へ渡す。"""

    def themes(self, tool: str) -> list[str]:
        return [d["theme"] for d in self.kinds("tool", tool=tool, type="alltools:theme")]

    def test_枠を開くたびと変えたときに渡す(self) -> None:
        page = self.open_shell(first="kanban")
        self.wait_until(page, lambda: self.themes("kanban"), "開いたときの既定")
        self.assertEqual(self.themes("kanban"), ["light"])
        page.click("#tab-settings")
        page.wait_for_function("document.querySelector('[data-theme-default=dark]').getAttribute('aria-pressed') === 'false'"
                               " && document.getElementById('lock-state').textContent !== ''")
        page.click("[data-theme-default=dark]")
        self.wait_until(page, lambda: self.themes("kanban")[-1:] == ["dark"], "変えた既定")
        self.assertEqual(page.get_attribute("html", "data-theme"), "dark")
        # 枠の画面が移ったら(読み直し)、渡し直す
        page.click("#tab-kanban")
        page.focus("#help")
        page.keyboard.press("F5")
        self.wait_until(page, lambda: self.tool_loads["kanban"] == 2, "読み直し", timeout=10)
        self.wait_until(page, lambda: len(self.themes("kanban")) == 3, "読み直したあとの既定")
        self.assertEqual(self.themes("kanban"), ["light", "dark", "dark"])


class DropTests(DesktopShellBrowserCase):
    """ファイルの落下は外枠が OS から受け、位置と名前だけを画面へ渡す。中身は落とされた枠の頼みにだけ。"""

    def test_落とす枠の上で離すと中身が届き_外では断る_ほかの枠には渡さない(self) -> None:
        page = self.open_shell(first="kanban", wait_tools=("nippou",))
        tool = self.tool_frame(page, "kanban")
        # Windows の位置は物理の画素(表示倍率 150%)
        page.evaluate("Object.defineProperty(window, 'devicePixelRatio', { get: () => 1.5 })")
        frame_box = page.locator("#panel-kanban iframe").bounding_box()
        zone = tool.evaluate("(() => { const r = document.getElementById('zone').getBoundingClientRect();"
                             " return [r.left + r.width / 2, r.top + r.height / 2]; })()")
        x, y = (frame_box["x"] + zone[0]) * 1.5, (frame_box["y"] + zone[1]) * 1.5
        files = [{"name": "看板_L1.csv", "size": 18, "modified": 0}]
        for kind in ("enter", "over", "drop"):
            page.evaluate("(a) => window.__shell.fileDrag(a)",
                          {"kind": kind, "x": x, "y": y, "physical": True, "files": files, "folders": 0})
        tool.wait_for_function("window.__dropped")
        self.assertEqual(tool.evaluate("window.__dropped"), [["看板_L1.csv", "落とした中身"]])
        self.assertEqual(self.invokes("shell_dropped_file"), [{"index": 0}])
        # ほかのタブの画面が中身を頼んでも渡さない
        other = self.tool_frame(page, "nippou")
        other.evaluate("""() => new Promise((resolve) => {
            window.addEventListener("message", (e) => { if (e.data.type === "alltools:dropped-file") resolve(); });
            parent.postMessage({ type: "alltools:dropped-file", id: 9, seq: 1, index: 0 }, "http://portal.test");
        })""")
        reply = [d for d in other.evaluate("window.__got") if d["type"] == "alltools:dropped-file"][0]
        self.assertFalse(reply["ok"])
        self.assertIn("落としたファイルが見当たりません", reply["value"])
        self.assertEqual(len(self.invokes("shell_dropped_file")), 1)
        # 落とす枠の外(ツールの画面の上)・タブの帯の上で離したら、断りを出す
        for where, (px, py) in (("枠の外", ((frame_box["x"] + 600) * 1.5, (frame_box["y"] + 40) * 1.5)),
                                ("帯の上", (20 * 1.5, 20 * 1.5))):
            with self.subTest(where=where):
                page.evaluate("document.getElementById('toasts').replaceChildren()")
                for kind in ("enter", "drop"):
                    page.evaluate("(a) => window.__shell.fileDrag(a)",
                                  {"kind": kind, "x": px, "y": py, "physical": True, "files": files})
                page.wait_for_function("document.getElementById('toasts').textContent.includes('ここには落とせません')")
        self.assertEqual(len(self.invokes("shell_dropped_file")), 1)


class UploadTests(DesktopShellBrowserCase):
    """ファイル入りの FormData は、embed.js が中身を読んでから1つのバイト列にして送る(WebView2 対策)。"""

    def test_ファイル入りのFormDataを組み立て直して送る(self) -> None:
        page = self.open_shell(first="kanban")
        tool = self.tool_frame(page, "kanban")
        tool.evaluate("""async () => {
            const form = new FormData();
            form.append("mode", "置き換え");
            form.append("file", new File(["表の中身です"], "最新.accdb", { type: "application/octet-stream" }));
            await fetch("/api/upload", { method: "POST", body: form });
        }""")
        sent = self.kinds("tool-request", tool="kanban")[-1]
        self.assertTrue(sent["type"].startswith("multipart/form-data; boundary="), sent["type"])
        body = bytes(sent["body"]).decode("utf-8")
        self.assertIn('name="file"; filename="最新.accdb"', body)
        self.assertIn("表の中身です", body)
        self.assertIn("置き換え", body)
        fetched = [d for d in self.kinds("fetch") if d["url"] == "/api/upload"]
        self.assertEqual([d["body"] for d in fetched], ["[object Uint8Array]"], "FormData のまま送った")


class DialogTests(DesktopShellBrowserCase):
    """確認ダイアログはブラウザのもの(答えを待つ)のまま。どの画面が置き換えられたかを記録に残す。"""

    PLUGIN = """(() => { "use strict";
      try { window.confirm = () => Promise.resolve(true); } catch (e) { window.__refused = String(e); } })();"""

    def reports(self) -> list[str]:
        return [b.get("message", "") for b in self.posts("/api/log/client")]

    def open_reported(self, *, guarded: bool):
        if guarded:
            # 外枠は dialog より先に、全部の枠で走らせる
            self.ctx.add_init_script((ROOT / "src-tauri" / "src" / "native_dialogs.js").read_text(encoding="utf-8"))
        self.ctx.add_init_script(self.PLUGIN)                 # tauri-plugin-dialog の置き換え
        page = self.open_shell(first="kanban")
        self.wait_until(page, lambda: any("kanban の画面 [確認ダイアログ:" in m for m in self.reports()),
                        "ツールの画面が出た記録")
        joined = [m for m in self.reports() if "つながりました(デスクトップ版)" in m]
        shown = [m for m in self.reports() if "kanban の画面 [確認ダイアログ:" in m]
        return page, joined[0], shown[0]

    def test_ブラウザの確認ダイアログを固定する_答えを待つ(self) -> None:
        page, joined, shown = self.open_reported(guarded=True)
        self.assertTrue(joined.endswith("[確認ダイアログ: native]"), joined)
        self.assertTrue(shown.endswith("[確認ダイアログ: native]"), shown)
        # 本物の確かめの窓が出て、答え(いいえ)を待つ。大きなタブの画面でも、ツールの画面でも
        tool = self.tool_frame(page, "kanban")
        for where, frame in (("大きなタブ", page), ("ツールの画面", tool)):
            with self.subTest(where=where):
                self.answers_for_dialogs = [False]
                self.assertIs(frame.evaluate("confirm('消しますか?')"), False)
                self.assertEqual(self.dialogs[-1], "消しますか?")

    def test_置き換えられたら起動の記録にそう残す(self) -> None:
        """起動確認(`scripts/desktop_smoke.py`)が、この記録で置き換えに気づく。"""
        _, joined, shown = self.open_reported(guarded=False)
        self.assertTrue(joined.endswith("[確認ダイアログ: replaced]"), joined)
        self.assertTrue(shown.endswith("[確認ダイアログ: replaced]"), shown)


if __name__ == "__main__":
    unittest.main()
