"""入口: **閉じる前に各ツールの画面へ打ちかけを置いてもらう**を本物のブラウザで(段階1)

窓の × ・「終了」・外からの停止・F5 の前に、大きなタブの画面(`shell.js`)が各ツールの画面へ
`alltools:before-close` を送り、「受けた」「済んだ」を待ってから進む。偽のツールの画面
(`tests/_shell_browser.py`)が返事をし、何が・どの順で送られたかを見る。

以前は字を探す試験だけでした(`test_shell_contract.py` の PrepareCloseTests・CloseWaitTests・
BrowserOriginTests・ReloadTests・FallbackCloseTests・ManualAndEndTests)。
"""
from __future__ import annotations

import unittest

from tests._shell_browser import DesktopShellBrowserCase, ShellBrowserCase

#: 「受けた」をすぐ返し、「済んだ」を `window.__doneAfter` ミリ秒あとに `window.__doneOk` で返すツールの画面
ANSWERING = """
window.__alltoolsHandlesClose = true;
window.__doneAfter = 0; window.__doneOk = true;
window.addEventListener("message", (e) => {
  if (!e.data || e.data.type !== "alltools:before-close") return;
  const seq = e.data.seq, to = e.origin;
  parent.postMessage({ type: "alltools:before-close-ack", seq }, to);
  setTimeout(() => {
    if (window.__note) window.__note("tool-done", { tool: TOOL_ID, seq });
    parent.postMessage({ type: "alltools:before-close-done", seq, ok: window.__doneOk }, to);
  }, window.__doneAfter);
});
"""


def answering(tool_id: str) -> str:
    return f"const TOOL_ID = {tool_id!r};" + ANSWERING


class BrowserQuitTests(ShellBrowserCase):
    """ブラウザ版の「終了」。"""

    def test_終了は各ツールの画面が済んだと返してから止める_受けたあとは短い上限で見切らない(self) -> None:
        """ブラウザ版はツールに宛先を渡さない(`origin: ""`)。枠の宛先を覚えて頼み、返事も受ける。

        「受けた」のあと「済んだ」を 2.5 秒待たせる(受けない画面を見切る 1.2 秒より長い)。
        """
        self.tool_script["kanban"] = answering("kanban")
        page = self.open_shell(first="kanban")
        tool = self.tool_frame(page, "kanban")
        tool.evaluate("window.__doneAfter = 2500")
        page.click("#quit")
        page.wait_for_selector("#ended[open]", timeout=10000)
        order = [k if k != "portal" else d["path"] for k, d in self.events
                 if k == "tool-done" or (k == "portal" and d["path"] == "/api/shutdown")]
        self.assertEqual(order, ["tool-done", "/api/shutdown"], "済んだの前に止めています")
        self.assertEqual(self.posts("/api/shutdown"), [{"screens_ready": True}],
                         "置いたあとは入口に「画面へ頼む」をさせない")
        self.wait_until(page, lambda: self.stopped, "入口が止まる")
        self.assertEqual(self.errors, [])

    def test_起動してすぐ終了しても_終了したあとに残りのツールを起こしに行かない(self) -> None:
        """入口 VER1.5.42 で直した不具合(段階1の試験で見つかった)。

        最初のタブが開くと、ほかのツールも 1.5 秒おきに裏で起こします(`preloadOthers`)。この順番待ちは
        定期の問い合わせと別のタイマーで、終了しても止まらず、**終了したあとに** ツールを起こす要求を
        送っていました。入口が止める(`stop_all`)のと競り合い、起こしたツールが残り得ます。
        """
        self.tool_script["kanban"] = answering("kanban")
        page = self.open_shell(first="kanban")
        self.tool_frame(page, "kanban")
        page.click("#quit")                               # 残りのツールを起こしきる前に終了
        page.wait_for_selector("#ended[open]", timeout=10000)
        at = next(i for i, (k, d) in enumerate(self.events)
                  if k == "portal" and d["path"] == "/api/shutdown")
        page.wait_for_timeout(6000)                       # 順番待ちが残っていれば、この間に送る
        later = [d["path"] for k, d in self.events[at + 1:]
                 if k == "portal" and d["path"].startswith("/api/tools/")]
        self.assertEqual(later, [], "終了したあとにツールを起こしに行っています")
        self.assertEqual(self.errors, [])

    def test_受けたあと済んだが来なければ_上限まで待って閉じない(self) -> None:
        """画面が固まった(受けたのに済まない)。10 分は待ち、来なければ**閉じない**ほうに倒す。"""
        self.ctx.clock.install()
        self.tool_script["kanban"] = answering("kanban")
        page = self.open_shell(first="kanban")
        self.tool_frame(page, "kanban").evaluate("window.__doneAfter = 24 * 3600 * 1000")
        page.click("#quit")
        self.wait_until(page, lambda: self.kinds("tool", type="alltools:before-close"), "閉じる前の頼み")
        page.wait_for_timeout(300)
        page.clock.fast_forward("09:00")                  # 本人が「続けますか?」を読んでいる
        page.wait_for_timeout(300)
        self.assertEqual(self.posts("/api/shutdown"), [], "受けたのに見切って止めた")
        self.assertNotIn("閉じるのをやめました", self.toasts(page))
        page.clock.fast_forward("01:01")
        page.wait_for_function("document.getElementById('toasts').textContent.includes('閉じるのをやめました')")
        page.wait_for_timeout(300)
        self.assertEqual(self.posts("/api/shutdown"), [])
        self.assertFalse(page.is_visible("#ended"))

    def test_画面が閉じないを選んだら止めない(self) -> None:
        self.tool_script["kanban"] = answering("kanban")
        page = self.open_shell(first="kanban")
        self.tool_frame(page, "kanban").evaluate("window.__doneOk = false")
        page.click("#quit")
        self.wait_until(page, lambda: self.kinds("tool-done"), "済んだ")
        page.wait_for_timeout(500)
        self.assertEqual(self.posts("/api/shutdown"), [])
        self.assertFalse(page.is_visible("#ended"))

    def test_大設定の打ちかけがあれば先に訊く_閉じないなら大設定を出して止めない(self) -> None:
        self.tool_script["kanban"] = answering("kanban")
        page = self.open_shell(first="settings", wait_tools=("kanban",))
        page.wait_for_function("document.getElementById('loc-state').textContent.includes('反映しています')")
        page.fill("#loc-folder", r"\\srv\打ちかけ")
        page.click("#tab-kanban")
        self.answers_for_dialogs = [False]
        page.click("#quit")
        page.wait_for_function("document.getElementById('tab-settings').getAttribute('aria-selected') === 'true'")
        self.assertEqual(len(self.dialogs), 1)
        self.assertIn("大設定に、まだ反映していない入力があります", self.dialogs[0])
        self.assertIn("共有の DB の置き場所", self.dialogs[0])
        page.wait_for_timeout(300)
        self.assertEqual(self.kinds("tool", type="alltools:before-close"), [], "閉じないのにツールへ頼んだ")
        self.assertEqual(self.posts("/api/shutdown"), [])
        self.assertEqual(page.input_value("#loc-folder"), r"\\srv\打ちかけ")

    def test_終了したら定期の問い合わせを止める(self) -> None:
        self.ctx.clock.install()
        # 裏で起こしておく残りのツール(1.5 秒おき)が出揃ってから
        page = self.open_shell(first="kanban", wait_tools=("nippou", "calendar", "inspection"))
        self.wait_until(page, lambda: any(p.startswith("/api/close-ask") for _, p, _ in self.sent), "外からの頼みの問い合わせ")
        page.click("#quit")
        page.wait_for_selector("#ended[open]", timeout=10000)
        after = len(self.sent)
        # 定期の問い合わせ(外からの頼み 1 秒・状態 5 秒・心拍 20 秒・タブの見直し 60 秒)が一回りする
        page.clock.fast_forward("01:05")
        page.wait_for_timeout(800)
        self.assertEqual([p for _, p, _ in self.sent[after:]], [], "終了したのに入口を叩いています")
        self.assertEqual(page.locator("iframe[src*='.test']").count(), 0, "ツールの枠を残しています")


class CloseAskTests(ShellBrowserCase):
    """ランチャー・stop.bat の停止(外からの頼み)でも、開いている画面に打ちかけを置いてもらう。"""

    def test_外から頼まれたら先に置いていますと返し_置けたら置けたと返す(self) -> None:
        from portal import web

        self.tool_script["kanban"] = answering("kanban")
        page = self.open_shell(first="kanban")
        tool = self.tool_frame(page, "kanban")
        tool.evaluate("window.__doneAfter = 1500")
        self.wait_until(page, lambda: web.close_ask.live(), "画面が問い合わせに来る")
        seq = web.close_ask.ask()
        self.wait_until(page, lambda: any(b.get("state") == "ok" for b in self.posts("/api/close-answer")),
                        "置けたの返事", timeout=10)
        order = []
        for kind, data in self.events:
            if kind == "portal" and data["path"] == "/api/close-answer":
                order.append(data["body"]["state"])
            elif kind == "tool" and data["type"] == "alltools:before-close":
                order.append("頼んだ")
            elif kind == "tool-done":
                order.append("済んだ")
        self.assertEqual(order, ["working", "頼んだ", "済んだ", "ok"])
        self.assertEqual({b["seq"] for b in self.posts("/api/close-answer")}, {seq})

    def test_閉じないを選んだら閉じないと返す(self) -> None:
        from portal import web

        self.tool_script["kanban"] = answering("kanban")
        page = self.open_shell(first="kanban")
        self.tool_frame(page, "kanban").evaluate("window.__doneOk = false")
        self.wait_until(page, lambda: web.close_ask.live(), "画面が問い合わせに来る")
        web.close_ask.ask()
        self.wait_until(page, lambda: any(b.get("state") == "refused" for b in self.posts("/api/close-answer")),
                        "閉じないの返事", timeout=10)
        page.wait_for_function("document.getElementById('toasts').textContent.includes('閉じるのをやめました')")
        self.assertFalse(page.is_visible("#ended"))

    def test_閉じた画面は閉じたと知らせる(self) -> None:
        page = self.open_shell(first="kanban")
        page.goto("about:blank")
        beacons = [b for b in self.kinds("beacon") if b["url"].endswith("/api/alive")]
        self.assertTrue(any('"leaving":true' in b["parts"] for b in beacons), beacons)


class DesktopPrepareTests(DesktopShellBrowserCase):
    """デスクトップ版: 外枠が `window.__shell.prepareClose()` を呼び、画面は `shell_prepared` で答える。"""

    def test_先に待っていますを返し_各ツールの答えをまとめて返す(self) -> None:
        self.tool_script["kanban"] = answering("kanban")
        page = self.open_shell(first="kanban")
        self.tool_frame(page, "kanban").evaluate("window.__doneOk = false; window.__doneAfter = 300")
        page.evaluate("window.__shell.prepareClose()")
        self.wait_until(page, lambda: len(self.invokes("shell_prepared")) >= 2, "外枠への2つめの返事")
        order = [("invoke", d["args"]) if k == "invoke" else ("tool", d["tool"])
                 for k, d in self.events
                 if (k == "invoke" and d["cmd"] == "shell_prepared")
                 or (k == "tool" and d["type"] == "alltools:before-close")]
        self.assertEqual(order[0], ("invoke", {"ok": True, "waiting": True}), "まず「待っています」")
        self.assertIn(("tool", "kanban"), order[1:-1])
        self.assertEqual(order[-1], ("invoke", {"ok": False}), "ツールが閉じないを選んだ")


class ReloadTests(DesktopShellBrowserCase):
    """F5 / Ctrl+R は、打ちかけを置いてもらってから、その画面だけ読み直す。"""

    def test_帯のF5もツールの画面の中のF5も_置いてからその画面だけ読み直す(self) -> None:
        self.tool_script["kanban"] = answering("kanban")
        page = self.open_shell(first="kanban")
        tool = self.tool_frame(page, "kanban")
        for where in ("帯", "ツールの画面"):
            with self.subTest(where=where):
                loads = dict(self.tool_loads)
                self.events.clear()
                tool.evaluate("window.__doneAfter = 1500")
                if where == "帯":
                    page.focus("#help")
                    page.keyboard.press("F5")
                else:
                    tool.click("#typed")
                    tool.press("#typed", "F5")
                self.wait_until(page, lambda: self.tool_loads.get("kanban") == loads["kanban"] + 1,
                                "その画面の読み直し", timeout=10)
                kinds = [k for k, _ in self.events if k in ("tool-done",)]
                self.assertEqual(kinds, ["tool-done"], "済んだの前に読み直した")
                # ほかのタブは、裏で起こしておいた1度だけ
                self.assertEqual({k: v for k, v in self.tool_loads.items() if k != "kanban" and v > 1}, {},
                                 "ほかのタブまで読み直した")
                tool = self.tool_frame(page, "kanban")
        self.assertEqual(page.evaluate("performance.getEntriesByType('navigation').length"), 1)

    def test_画面が閉じないを選んだら読み直さない(self) -> None:
        self.tool_script["kanban"] = answering("kanban")
        page = self.open_shell(first="kanban")
        tool = self.tool_frame(page, "kanban")
        tool.evaluate("window.__doneOk = false")
        loads = self.tool_loads["kanban"]
        tool.click("#typed")
        tool.press("#typed", "F5")
        self.wait_until(page, lambda: self.kinds("tool-done"), "済んだ")
        page.wait_for_timeout(600)
        self.assertEqual(self.tool_loads["kanban"], loads)

    def test_大きなタブの外の画面ではF5でそのまま読み直す(self) -> None:
        page = self.ctx.new_page()
        page.goto("http://kanban.test/", wait_until="load")
        loads = self.tool_loads["kanban"]
        page.click("#typed")
        page.keyboard.press("F5")
        self.wait_until(page, lambda: self.tool_loads["kanban"] == loads + 1, "読み直し")


class FallbackCloseTests(DesktopShellBrowserCase):
    """自分で「閉じる前」を受けないツールの画面は、embed.js が受け皿になる。"""

    def ask(self, page, seq: int) -> dict:
        """大きなタブの画面と同じ頼みを送り、ツールの画面の返事を待つ。"""
        self.events.clear()
        page.evaluate("window.__shell.prepareClose()")
        self.wait_until(page, lambda: [a for a in self.invokes("shell_prepared") if "waiting" not in a],
                        "外枠への答え", timeout=10)
        return [a for a in self.invokes("shell_prepared") if "waiting" not in a][0]

    def test_打った欄や開いた入力の窓があれば訊き_答えを返す(self) -> None:
        self.tool_script["kanban"] = """
          document.body.insertAdjacentHTML("beforeend",
            '<dialog id="form"><input id="in-dialog"></dialog>');
        """
        page = self.open_shell(first="kanban")
        tool = self.tool_frame(page, "kanban")
        with self.subTest("何もしていなければ訊かない"):
            tool.fill("#given", "サーバの値")              # 開いたときの値のまま
            self.assertEqual(self.ask(page, 1), {"ok": True})
            self.assertEqual(self.dialogs, [])
        with self.subTest("打った欄があれば訊く・閉じないなら閉じない"):
            tool.fill("#typed", "打ちかけ")
            self.answers_for_dialogs = [False]
            self.assertEqual(self.ask(page, 2), {"ok": False})
            self.assertEqual(len(self.dialogs), 1)
            self.assertIn("保存していない入力があります。閉じますか?", self.dialogs[0])
        with self.subTest("戻したら訊かない・入力の窓が開いていれば訊く"):
            tool.fill("#typed", "")
            self.dialogs.clear()
            self.assertEqual(self.ask(page, 3), {"ok": True})
            self.assertEqual(self.dialogs, [])
            tool.evaluate("document.getElementById('form').show()")
            self.assertEqual(self.ask(page, 4), {"ok": True})
            self.assertEqual(len(self.dialogs), 1)

    def test_自分で受けるツールには任せる(self) -> None:
        self.tool_script["kanban"] = answering("kanban")
        page = self.open_shell(first="kanban")
        tool = self.tool_frame(page, "kanban")
        tool.fill("#typed", "打ちかけ")
        tool.evaluate("window.__doneOk = false")
        self.assertEqual(self.ask(page, 1), {"ok": False})
        self.assertEqual(self.dialogs, [], "受け皿も訊いた(2重に訊く)")


if __name__ == "__main__":
    unittest.main()
