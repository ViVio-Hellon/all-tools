"""点検表: **1台の端末で画面を使う一連の流れ**を、本物のブラウザで通しで確かめる

現場の人の操作の順に、画面のボタン・窓を実際に押して進めます(要求は Flask のテストクライアントへ
回すのでポートを開かない。土台は `tests/_page_browser.py`)。

    1. 初めての起動 … 設定がまっさら → 案内 → 設定(点検表フォルダ・↑上へ・各フォルダ・管理者パスワード・
                       配布設定・ログの保存先)→ 戻る
    2. 探す         … 検索欄・検出・絞り込み・一覧(ファイル名・更新日時)・該当なし・全角半角/大文字小文字の揺れ
    3. 選ぶ         … 選択一覧へ足す・◧・↑↓・×・同じものを2回・開き直しても残る(sessionStorage)
    4. プレビュー   … 開く・全体表示・作り直す・↓キー・閉じる・失敗して「もう一度」
    5. 印刷         … 確認 → 進み具合 → 終わり / 途中のエラー / 中止。印刷中にほかのボタン・Esc・Ctrl+P
                       (処理プログラムの偽物 `fake_worker.py` を通して、失敗の中身が画面に出るところまで)
    6. なぜなぜ・ログ … 問い合わせ番号で引く・最近のエラー・保存先を替えても前の番号を引ける(`<保存先>/inspection/`)
    7. 画面の色     … 切り替えて開き直しても残る・設定の面を覚えている
    8. 断る場面     … 文が分かる・画面が固まらない・押しても何も起きないボタンが無い

どの流れも最後に、画面の誤り(pageerror)とコンソールのエラー(console.error)が無いことを見ます。
ただし、サーバが断った要求にブラウザが添える「Failed to load resource」は、断りを試す流れでは
当たり前に出るので数えません(断りの文が画面に出たかは、それぞれの流れで見ます)。

**本物のユーザーのフォルダを汚さない**: 試験ごとにローカル領域(設定・ログ・控え)を新しい一時フォルダにする。
**時間に頼らない**(Windows の CI は遅い): 画面の状態・サーバの状態が変わるのを待ってから進める。
「何も起きない」ことを見るときだけ、少し待ってから数を比べる。

無い環境では飛ばします(CI では飛ばしません: ``ALLTOOLS_REQUIRE_BROWSER=1``)。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

from app.services import admin_password, distribution
from app.services.excel_service import DummyExcelBackend, ExcelError, VbsExcelBackend
from core import logging_utils

from tests._page_browser import PageBrowserCase
from tests.helpers import make_tree
from tests.test_web import TOKEN

FAKE_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_worker.py")
#: サーバが断った要求にブラウザが添えるもの(断りを試す流れでは当たり前に出る)
NETWORK_NOISE = ("Failed to load resource",)
#: 問い合わせ番号(例: 1025-2334-SHWH)
REF = re.compile(r"\d{4}-\d{4}-[0-9A-Z]{4}")


class FakeWorkerBackend(VbsExcelBackend):
    """Excel・Word の代わりに `fake_worker.py` と話す(本物と同じ1行コマンドの手順)。"""

    extra: dict = {}

    def _base_params(self):
        params = super()._base_params()
        params.update(self.extra)
        return params

    def _command(self):
        return [sys.executable, FAKE_WORKER, "session"]


class FlowCase(PageBrowserCase):
    def setUp(self) -> None:
        # 試験ごとにまっさらなローカル領域(設定・ログ・控え)。本物の %LOCALAPPDATA% を触らない
        self.local = tempfile.mkdtemp(prefix="isp_flow_")
        self.addCleanup(shutil.rmtree, self.local, True)
        # ログの書き出し先も、この領域で決め直す。終わったら**環境変数を戻してから**決め直す
        # (後片付けは登録の逆順。先に決め直すと、消した一時フォルダへ次の試験のログを書きに行く)
        self.addCleanup(logging_utils.reset_for_tests)
        env = mock.patch.dict(os.environ, {"INSPECTION_LOCAL_DIR": self.local})
        env.start()
        self.addCleanup(env.stop)
        logging_utils.reset_for_tests()
        super().setUp()
        self.console: list[str] = []

    # -- 画面 --------------------------------------------------------------
    def open_flow(self, path: str = "/", ctx=None, *, wait_list: bool = True, selection=None):
        ctx = ctx or self.new_context()
        if selection is not None:
            ids = json.dumps([it["id"] for it in selection])
            ctx.add_init_script("if (!sessionStorage.getItem('isp.selection')) "
                                f"sessionStorage.setItem('isp.selection', {json.dumps(ids)});")

        def watch(page):
            page.on("console", lambda m: self.console.append(m.text) if m.type == "error" else None)

        return self.open(path, ctx, wait_list=wait_list, prepare=watch)

    def assert_clean(self) -> None:
        """画面の誤り・コンソールのエラーが無い(断りに添えられる通信の知らせは除く)。"""
        self.assertEqual(self.errors, [], "画面の誤り(pageerror)")
        self.assertEqual([t for t in self.console if not t.startswith(NETWORK_NOISE)], [],
                         "コンソールのエラー")

    @staticmethod
    def wait_text(page, selector: str, text: str, *, exact: bool = False, timeout: int = 8000) -> None:
        page.wait_for_function(
            """([s, t, exact]) => { const n = document.querySelector(s); if (!n) return false;
                 const v = n.innerText.trim(); return exact ? v === t : v.includes(t); }""",
            arg=[selector, text, exact], timeout=timeout)

    @staticmethod
    def wait_toast(page, text: str) -> None:
        page.wait_for_selector(f".toast:has-text('{text}')")

    @staticmethod
    def selected(page) -> list[str]:
        return page.evaluate("() => [...document.querySelectorAll('#sel-list .sel-item__name')]"
                             ".map((n) => n.textContent)")

    @staticmethod
    def disabled(page, *ids: str) -> dict:
        return page.evaluate("(ids) => Object.fromEntries(ids.map((i) => [i, document.getElementById(i).disabled]))",
                             list(ids))

    @staticmethod
    def card(page, name: str):
        return page.locator("#items .item", has_text=name)

    # -- 模擬モードでない(配布したそのままの)アプリ -----------------------
    def use_plain_app(self, backend=None) -> None:
        """設定がまっさらな、模擬モードでないアプリに替える(点検表フォルダは config の既定)。

        config の既定は会社の共有フォルダ(`\\\\nlmsrvngy03\\…`)。試験の PC からは見えないが、Windows で
        無いサーバを探しに行くと長く待たされるので、**見えないローカルのフォルダ**に差し替える
        ── 「初めての起動で共有フォルダが見えない」のと同じ姿。
        """
        from app import create_app

        self.biz.printing.wait_idle(10)
        self.app = create_app(token=TOKEN, port=8799, options={})
        self.app.config["TESTING"] = True
        self.biz = self.app.extensions["inspection"]
        self.biz.cfg.root_folder = os.path.join(self.local, "見えない共有", "一括管理_点検表")
        # Windows の CI では "auto" が本物の Excel(cscript)になる。試験では使わない
        self.biz.excel.backend = backend or DummyExcelBackend(print_delay=0.05, preview_delay=0)
        self.biz.inspection.start_scan("test")
        self.assertTrue(self.biz.inspection.wait(10))
        self.app.config["READY"] = True
        self.client = self.app.test_client()


# =====================================================================
# 1. 初めての起動
# =====================================================================
class FirstRunFlowTests(FlowCase):
    def setUp(self) -> None:
        super().setUp()
        self.use_plain_app()
        folder = distribution.default_dir()
        self.addCleanup(shutil.rmtree, folder, True)
        # 点検表の共有フォルダ(の代わり)。ルート > カテゴリー > サブカテゴリー > Excel・Word
        self.root = os.path.join(self.local, "共有", "点検表")
        make_tree(self.root, ["梱包/日常/梱包_日常_台車点検.xlsx", "梱包/日常/梱包_日常_作業手順.docx",
                              "押出/設備/押出_設備_プレス点検.xlsm"])

    def test_まっさらで開き_案内に従って設定し_一覧が出るまで(self) -> None:
        page = self.open_flow(wait_list=False)

        # --- 案内: 点検表フォルダが見えない。どこを押せばよいかを言う ---
        page.wait_for_selector("#items-empty:not([hidden])")
        empty = page.inner_text("#items-empty")
        self.assertIn("点検表フォルダが見つかりません", empty)
        self.assertIn("「設定」→「点検表フォルダ」", empty)
        self.assertEqual(page.get_attribute("#rb-folder", "data-state"), "unset")
        self.assertEqual(page.get_attribute("html", "data-theme"), "light", "まっさらならライト")
        self.assertEqual(page.inner_text("#btn-theme"), "◇ 既定")
        # 選べるものが無いので、押しても何もできないボタンは押せない
        self.assertEqual(self.disabled(page, "btn-preview", "btn-print", "btn-clear-sel", "btn-sub-all",
                                       "btn-sub-none", "btn-cat-all", "btn-cat-none", "btn-refresh", "btn-settings"),
                         {"btn-preview": True, "btn-print": True, "btn-clear-sel": True, "btn-sub-all": True,
                          "btn-sub-none": True, "btn-cat-all": True, "btn-cat-none": True,
                          "btn-refresh": False, "btn-settings": False})

        # --- 再読込: まだ見えない。選んでいないのに「選んだものは残しています」と言わない ---
        page.click("#btn-refresh")
        self.wait_toast(page, "点検表フォルダが見えません")
        warn = page.inner_text(".toast--warn")
        self.assertNotIn("選んだもの", warn, "何も選んでいないのに「選んだものはそのまま残しています」と言った")
        self.assertIn("設定", warn)

        # --- 設定: 最初の面は点検表フォルダ。既定(config)のフォルダが入っていて、見えないと言う ---
        page.click("#btn-settings")
        page.wait_for_selector("#settings-dialog[open]")
        self.assertTrue(page.is_visible("#panel-folder"))
        self.assertEqual(page.input_value("#settings-folder"), self.biz.settings.default_root_folder)
        self.wait_text(page, "#settings-source", "既定(config")
        self.wait_text(page, "#fs-note", "このPCから見えません")
        self.assertTrue(page.is_disabled("#settings-reset"), "既定のままなのに「既定に戻す」を押せる")
        self.assertEqual(page.inner_text("#tab-password .tab__badge"), "既定")

        # 空のまま保存 → 言って止まる(窓は閉じない)
        page.fill("#settings-folder", "")
        page.click("#settings-save")
        self.wait_text(page, "#settings-error", "点検表フォルダを入力してください。")
        # 無いフォルダ → サーバが断る。打ったパスも添える
        missing = os.path.join(self.local, "無いフォルダ")
        page.fill("#settings-folder", missing)
        page.click("#settings-save")
        self.wait_text(page, "#settings-error", "指定されたフォルダが見つかりません")
        self.assertIn(missing, page.inner_text("#settings-error"))
        self.assertTrue(page.evaluate("document.getElementById('settings-dialog').open"))

        # 1つ深いフォルダ(カテゴリー)を選んでしまった → 一覧は出ない
        page.fill("#settings-folder", os.path.join(self.root, "梱包"))
        page.press("#settings-folder", "Enter")                    # Enter でも保存する
        page.wait_for_selector("#settings-dialog:not([open])", state="attached")
        self.wait_toast(page, "点検表フォルダを変更しました")
        self.wait_text(page, "#items-empty", "点検表が見つかりません")
        self.assertIn("ルート > カテゴリー > サブカテゴリー", page.inner_text("#items-empty"))

        # --- 設定し直す: ↑上へ・各フォルダで辿って、正しいフォルダにする ---
        page.click("#btn-settings")
        page.wait_for_selector("#settings-dialog[open]")
        self.wait_text(page, "#settings-source", "この PC で設定したフォルダ")
        self.assertFalse(page.is_disabled("#settings-reset"))
        self.wait_text(page, "#fs-path", os.path.join(self.root, "梱包"))
        # 中に点検表のファイルがある(Word も数える。「Excel 2 件」と Word を Excel と言わない)
        page.click("#fs-list button:has-text('日常')")
        self.wait_text(page, "#fs-path", os.path.join(self.root, "梱包", "日常"))
        self.assertEqual(page.input_value("#settings-folder"), os.path.join(self.root, "梱包", "日常"))
        self.wait_text(page, "#fs-note", "Excel・Word 2 件")
        self.assertIn("梱包_日常_作業手順.docx", page.inner_text("#fs-list"))
        # ↑上へ を2回: 欄にも入る
        page.click("#fs-up")
        self.wait_text(page, "#fs-path", os.path.join(self.root, "梱包"), exact=True)
        page.click("#fs-up")
        self.wait_text(page, "#fs-path", self.root, exact=True)
        self.assertEqual(page.input_value("#settings-folder"), self.root)
        self.wait_text(page, "#fs-note", "フォルダ 2 件")
        page.click("#settings-save")
        page.wait_for_selector("#settings-dialog:not([open])", state="attached")
        page.wait_for_selector("#items .item")
        self.wait_text(page, "#rb-found", "3件", exact=True)
        self.assertEqual(page.inner_text("#rb-folder"), self.root)
        self.assertIsNone(page.get_attribute("#rb-folder", "data-state"))
        self.assertEqual(page.evaluate("[...document.querySelectorAll('#cat-tabs .tab > span:first-child')]"
                                       ".map((n) => n.textContent)"), ["押出", "梱包"])

        # --- 管理者パスワード ---
        page.click("#btn-settings")
        page.wait_for_selector("#settings-dialog[open]")
        page.click("#tab-password")
        self.assertEqual(page.inner_text("#adm-state"), "既定のまま")
        page.fill("#adm-now", "まちがい")
        page.fill("#adm-new", "line-1234")
        page.fill("#adm-confirm", "line-1234")
        page.click("#adm-save")
        page.wait_for_selector("#adm-why:not([hidden])")
        self.assertTrue(page.inner_text("#adm-why").strip(), "断りの理由が出ない")
        self.assertEqual(page.input_value("#adm-new"), "", "打ったパスワードが欄に残っている")
        page.fill("#adm-now", admin_password.DEFAULT)
        page.fill("#adm-new", "line-1234")
        page.fill("#adm-confirm", "line-9999")
        page.click("#adm-save")
        page.wait_for_function("document.getElementById('adm-why').innerText.trim() !== ''")
        self.assertEqual(page.inner_text("#adm-state"), "既定のまま")
        page.fill("#adm-now", admin_password.DEFAULT)
        page.fill("#adm-new", "line-1234")
        page.fill("#adm-confirm", "line-1234")
        page.click("#adm-save")
        self.wait_text(page, "#adm-state", "変更済み", exact=True)
        self.assertTrue(page.is_hidden("#adm-why"))
        self.assertTrue(page.is_hidden("#tab-password .tab__badge"), "変えたのに見出しが「既定」のまま")

        # --- 配布設定: パスワード無しは断る → 新しいパスワードで書き出す → 消す ---
        page.click("#tab-distribution")
        page.wait_for_selector("#dist-items input[data-dist-item]")
        self.assertEqual(page.inner_text("#dist-state"), "なし")
        page.click("#dist-export")
        page.wait_for_selector("#dist-why:not([hidden])")
        self.assertTrue(page.inner_text("#dist-why").strip())
        page.fill("#dist-password", admin_password.DEFAULT)              # 変えたので既定は通らない
        page.click("#dist-export")
        page.wait_for_function("document.getElementById('dist-why').innerText.trim() !== ''")
        self.assertEqual(page.inner_text("#dist-state"), "なし")
        page.fill("#dist-password", "line-1234")
        page.click("#dist-export")
        self.wait_text(page, "#dist-state", "あり", exact=True)
        self.assertEqual(page.inner_text("#tab-distribution .tab__badge"), "あり")
        self.assertTrue((distribution.default_dir() / distribution.SETTINGS_NAME).exists())
        self.assertIn(self.root, page.inner_text("#dist-rows"), "書き出した点検表フォルダが表に出ない")
        page.fill("#dist-password", "line-1234")
        page.click("#dist-remove")
        self.wait_text(page, "#dist-state", "なし", exact=True)

        # --- ログの保存先: いま書いているのは <保存先>/inspection/。相対パスは断る。共有へ替えて戻す ---
        page.click("#tab-logs")
        self.wait_text(page, "#log-state", "この PC", exact=True)
        self.assertEqual(os.path.basename(page.inner_text("#log-dir")), "inspection")
        self.assertTrue(page.inner_text("#log-file").startswith("inspection_"))
        page.fill("#log-dir-input", "ログ")
        page.click("#log-save")
        self.wait_text(page, "#log-why", "フルパス")
        shared = os.path.join(self.local, "共有", "ログ")
        os.makedirs(shared)
        page.fill("#log-dir-input", shared)
        page.click("#log-save")
        self.wait_text(page, "#log-state", "指定のフォルダ", exact=True)
        self.assertEqual(page.inner_text("#log-dir"), os.path.join(shared, "inspection"))
        self.assertTrue(page.is_hidden("#log-why"))
        self.assertTrue(os.path.isdir(os.path.join(shared, "inspection")))
        page.click("#log-reset")
        self.wait_text(page, "#log-state", "この PC", exact=True)

        # --- 閉じて戻る: 選んで印刷できる画面になっている ---
        page.click("#settings-close")
        page.wait_for_selector("#settings-dialog:not([open])", state="attached")
        page.click("#cat-tabs .tab:has-text('梱包')")
        self.card(page, "台車点検").click()
        page.wait_for_selector("#sel-list li")
        self.assertFalse(page.is_disabled("#btn-print"))
        self.assert_clean()

    def test_設定は開き直しても残り_設定の窓は前に見ていた面で開く(self) -> None:
        ctx = self.new_context()
        page = self.open_flow(ctx=ctx, wait_list=False)
        page.wait_for_selector("#items-empty:not([hidden])")
        page.click("#btn-settings")
        page.wait_for_selector("#settings-dialog[open]")
        page.fill("#settings-folder", self.root)
        page.click("#settings-save")
        page.wait_for_selector("#items .item")
        page.click("#btn-settings")
        page.wait_for_selector("#settings-dialog[open]")
        page.click("#tab-logs")
        self.wait_text(page, "#log-state", "この PC")
        page.fill("#log-keep", "45")
        page.click("#log-save")
        self.wait_toast(page, "ログの設定を保存しました")

        # 開き直す: 点検表フォルダ・ログの保存日数はサーバが覚えている。窓は「ログ」の面で開く
        page.reload(wait_until="load")
        page.wait_for_selector("#items .item")
        self.assertEqual(page.inner_text("#rb-folder"), self.root)
        page.click("#btn-settings")
        page.wait_for_selector("#settings-dialog[open]")
        self.assertTrue(page.is_visible("#panel-logs"))
        self.wait_text(page, "#log-keep-now", "45", exact=True)   # 面を開いた時点で読み込む
        self.assertEqual(page.input_value("#log-keep"), "45")
        page.click("#tab-folder")
        self.assertEqual(page.input_value("#settings-folder"), self.root)
        self.assert_clean()


# =====================================================================
# 2. 探す / 3. 選ぶ
# =====================================================================
class FindAndSelectFlowTests(FlowCase):
    def go(self, page, category: str, sub: str) -> None:
        page.click(f"#cat-tabs .tab:has-text('{category}')")
        page.click(f"#subnav .subnav__item:has-text('{sub}')")
        self.wait_text(page, "#items-title", f"{category} > {sub}", exact=True)

    def search(self, page, text: str) -> str:
        page.fill("#search", text)
        self.wait_text(page, "#items-count", f"「{text.strip()}」")
        return page.inner_text("#items-count")

    def test_検索の揺れ_該当なし_絞り込み_一覧の名前と更新日時(self) -> None:
        page = self.open_flow()
        # 検出: 命名規則に合わない1件は数えるが出さない
        self.assertEqual(page.inner_text("#rb-found"), "21件(表示20)")

        # 絞り込み: カテゴリー → サブカテゴリー。件数・ファイルの種類・更新日時
        self.go(page, "梱包", "月次点検")
        self.assertEqual(page.inner_text("#items-count"), "4 件")
        meta = self.card(page, "作業手順の確認").locator(".item__meta").inner_text()
        self.assertRegex(meta, r"^DOCX ・ 更新 \d{4}/\d{2}/\d{2} \d{2}:\d{2}$")
        self.assertIn("梱包_月次点検_作業手順の確認.docx", self.card(page, "作業手順の確認").get_attribute("title"))
        # 命名規則に合わないファイルは、そのフォルダを開くと理由と一緒に出る
        self.go(page, "品質", "検査記録")
        self.assertTrue(page.is_visible("#unmatched"))
        self.assertIn("1 件", page.inner_text("#unmatched-summary"))
        page.click("#unmatched-summary")
        self.assertIn("旧様式_検査記録.xlsx", page.inner_text("#unmatched-list"))

        # 検索: 全カテゴリーから。全角半角・大文字小文字の揺れを吸収する
        self.assertEqual(self.search(page, "消火器"), "「消火器」 1 件")
        self.assertEqual(page.inner_text("#items-title"), "検索結果")
        self.assertIn("梱包 > 月次点検", page.inner_text("#items"))    # どこにあるかの見出し
        self.assertTrue(page.is_disabled("#btn-cat-all"), "検索中にカテゴリ全選択を押せる")
        self.assertTrue(self.card(page, "フォークリフト").count() == 0)
        self.assertEqual(self.search(page, "ﾌｫｰｸﾘﾌﾄ"), "「ﾌｫｰｸﾘﾌﾄ」 1 件")      # 半角カナ
        self.assertEqual(self.card(page, "フォークリフト始業点検").count(), 1)
        self.assertEqual(self.search(page, "ＸＬＳＭ"), "「ＸＬＳＭ」 3 件")    # 全角英字・大文字
        self.assertEqual(self.search(page, "xlsm"), "「xlsm」 3 件")
        self.assertEqual(self.search(page, "  台車  "), "「台車」 1 件")      # 前後の空白

        # 該当なし: 言う。表示中を全選択は押せない
        self.assertEqual(self.search(page, "存在しない点検表"), "「存在しない点検表」 0 件")
        self.wait_text(page, "#items-empty", "一致する点検表はありません")
        self.assertTrue(page.is_disabled("#btn-sub-all"))

        # 検索結果をまとめて選ぶ → Esc で検索をやめると、元のサブカテゴリーに戻る
        self.search(page, "点検表")
        page.click("#btn-sub-all")
        self.assertEqual(self.selected(page), ["巡回点検表"])
        page.press("#search", "Escape")
        self.wait_text(page, "#items-title", "品質 > 検査記録", exact=True)
        self.assertEqual(page.input_value("#search"), "")
        self.assertTrue(page.is_hidden("#items-empty"))
        self.assert_clean()

    def test_選ぶ_並べ替え_外す_同じものを2回_開き直しても残る(self) -> None:
        ctx = self.new_context()
        page = self.open_flow(ctx=ctx)
        self.go(page, "梱包", "日常点検")
        for name in ("台車点検", "フォークリフト始業点検", "結束機点検"):
            self.card(page, name).click()
        self.assertEqual(self.selected(page), ["台車点検", "フォークリフト始業点検", "結束機点検"])
        self.assertEqual(page.inner_text("#rb-selected"), "3件")
        self.assertEqual(page.inner_text("#sel-count"), "3")
        self.assertEqual(self.card(page, "結束機点検").locator(".item__order").inner_text(), "3")
        self.assertEqual(page.inner_text("#cat-tabs .tab[aria-selected='true'] .tab__badge"), "3/11")

        # 同じものをもう一度押す = 外す。もう一度押すと最後に足す(2回入らない)
        self.card(page, "台車点検").click()
        self.assertEqual(self.selected(page), ["フォークリフト始業点検", "結束機点検"])
        self.card(page, "台車点検").click()
        self.assertEqual(self.selected(page), ["フォークリフト始業点検", "結束機点検", "台車点検"])

        # ↑↓ で印刷の順番を替える。端では押せない
        rows = page.locator("#sel-list li")
        self.assertTrue(rows.nth(0).locator("button[aria-label='上へ(印刷順)']").is_disabled())
        self.assertTrue(rows.nth(2).locator("button[aria-label='下へ(印刷順)']").is_disabled())
        rows.nth(2).locator("button[aria-label='上へ(印刷順)']").click()
        self.assertEqual(self.selected(page), ["フォークリフト始業点検", "台車点検", "結束機点検"])
        page.locator("#sel-list li").nth(0).locator("button[aria-label='下へ(印刷順)']").click()
        self.assertEqual(self.selected(page), ["台車点検", "フォークリフト始業点検", "結束機点検"])
        self.assertEqual(self.card(page, "台車点検").locator(".item__order").inner_text(), "1")

        # × で外す
        page.locator("#sel-list li").nth(1).locator("button[aria-label='選択を解除']").click()
        self.assertEqual(self.selected(page), ["台車点検", "結束機点検"])
        self.assertFalse(self.card(page, "フォークリフト始業点検").locator("input").is_checked())

        # 表示中を全選択: 残りを後ろに足す(選んであるものは2回入れない)。カテゴリ全選択を2回押すと知らせる
        page.click("#btn-sub-all")
        self.assertEqual(len(self.selected(page)), 5)
        self.assertEqual(self.selected(page)[:2], ["台車点検", "結束機点検"])
        page.click("#btn-cat-all")
        self.assertEqual(len(self.selected(page)), 11)
        page.click("#btn-cat-all")
        self.wait_toast(page, "このカテゴリはすべて選択済みです")
        page.click("#btn-cat-none")
        self.assertEqual(self.selected(page), [])
        for name in ("結束機点検", "台車点検"):
            self.card(page, name).click()

        # 開き直しても、選んだものと順番・見ていたフォルダが残る(sessionStorage)
        page.reload(wait_until="load")
        page.wait_for_selector("#sel-list li")
        self.assertEqual(self.selected(page), ["結束機点検", "台車点検"])
        self.assertEqual(page.inner_text("#items-title"), "梱包 > 日常点検")
        self.assertTrue(self.card(page, "台車点検").locator("input").is_checked())

        # ◧ でその点検表のプレビューを開く
        page.locator("#sel-list li").nth(1).locator("button[aria-label='プレビュー']").click()
        page.wait_for_selector("#preview-dialog[open]")
        self.assertEqual(page.inner_text("#pv-list li[aria-selected='true'] b"), "台車点検")
        page.wait_for_selector("#pv-image:not([hidden])")
        page.click("#pv-close")

        # すべて解除 → 開き直しても空のまま
        page.click("#btn-clear-sel")
        self.assertEqual(self.selected(page), [])
        self.assertTrue(page.is_visible("#sel-empty"))
        self.assertTrue(page.is_disabled("#btn-print"))
        page.reload(wait_until="load")
        page.wait_for_selector("#items .item")
        self.assertEqual(self.selected(page), [])
        self.assert_clean()


# =====================================================================
# 4. プレビュー
# =====================================================================
class PreviewFlowTests(FlowCase):
    def test_開く_全体表示_作り直す_キーで送る_閉じる(self) -> None:
        first, second = self.item("消火器点検"), self.item("クレーン点検")
        page = self.open_flow(selection=[first, second])
        page.wait_for_selector("#sel-list li")

        # 「プレビュー」ボタン: 選ぶまで作らない。まだ「作り直す」ものも無い(押せない)
        page.click("#btn-preview")
        page.wait_for_selector("#preview-dialog[open]")
        self.assertEqual(page.inner_text("#pv-status"), "点検表を選んでください")
        self.assertTrue(page.is_disabled("#pv-regenerate"), "何も出していないのに「作り直す」を押せる(押しても何も起きない)")
        self.assertTrue(page.is_hidden("#pv-fit"))
        self.assertEqual(self.posts_to("/api/preview"), [])

        # 1つ目を選ぶ → 出る。全体表示 ⇄ 実寸表示
        page.click("#pv-list li:nth-child(1)")
        page.wait_for_selector("#pv-image:not([hidden])")
        self.wait_text(page, "#pv-status", f"表示中: {first['name']}")
        self.assertFalse(page.is_disabled("#pv-regenerate"))
        self.assertEqual(page.inner_text("#pv-fit"), "全体表示")
        page.click("#pv-fit")
        self.assertEqual(page.inner_text("#pv-fit"), "実寸表示")
        self.assertFalse(page.evaluate("document.getElementById('pv-view').classList.contains('fit')"))
        page.click("#pv-image")                                    # 絵を押しても切り替わる
        self.assertEqual(page.inner_text("#pv-fit"), "全体表示")

        # 作り直す: 控えを使わずに作り直す
        page.click("#pv-regenerate")
        self.wait_until(page, lambda: len(self.posts_to("/api/preview")) == 2, "作り直しの要求")
        self.assertTrue(self.posts_to("/api/preview")[-1]["force"])
        page.wait_for_selector("#pv-image:not([hidden])")

        # ↓ キーで次の点検表へ
        page.keyboard.press("ArrowDown")
        self.wait_text(page, "#pv-status", f"表示中: {second['name']}")
        self.assertEqual(page.inner_text("#pv-list li[aria-selected='true'] b"), second["name"])
        self.assertEqual(self.posts_to("/api/preview")[-1]["id"], second["id"])

        # 閉じる。帯の「いま動いているもの」も消える
        page.click("#pv-close")
        page.wait_for_selector("#preview-dialog:not([open])", state="attached")
        self.assertTrue(page.is_hidden("#rb-running"))
        # 開き直すと、また選ぶところから(前の絵を出したままにしない)
        page.click("#btn-preview")
        page.wait_for_selector("#preview-dialog[open]")
        self.assertTrue(page.is_hidden("#pv-image"))
        self.assertTrue(page.is_disabled("#pv-regenerate"))
        page.keyboard.press("Escape")
        page.wait_for_selector("#preview-dialog:not([open])", state="attached")
        self.assert_clean()

    def test_作れなかったら理由と問い合わせ番号を出し_もう一度で作り直せる(self) -> None:
        excel = self.item("消火器点検")
        original = self.biz.excel.backend.preview
        broken = {"on": True}

        def maybe_fail(path, pdf=True):
            if broken["on"]:
                raise ExcelError("OPEN_FAILED", "パスワードが正しくありません。(0x800A1520)")
            return original(path, pdf=pdf)

        self.biz.excel.backend.preview = maybe_fail
        page = self.open_flow(selection=[excel])
        page.wait_for_selector("#sel-list li")
        page.click("#sel-list li button[aria-label='プレビュー']")
        page.wait_for_selector("#pv-message[data-error]")
        message = page.inner_text("#pv-message")
        self.assertIn("表示できません", page.inner_text("#pv-status"))
        self.assertIn("0x800A1520", message, "原因(詳しい理由)が出ない")
        self.assertRegex(message, r"問い合わせ番号: \d{4}-\d{4}-[0-9A-Z]{4}")
        self.assertTrue(page.is_hidden("#rb-running"), "失敗したのに「プレビュー」が動いたまま")

        broken["on"] = False
        page.click("#pv-message button:has-text('もう一度')")
        page.wait_for_selector("#pv-image:not([hidden])")
        self.assertTrue(self.posts_to("/api/preview")[-1]["force"])
        self.assertIsNone(page.get_attribute("#pv-message", "data-error"))
        self.assert_clean()


# =====================================================================
# 5. 印刷
# =====================================================================
class PrintFlowTests(FlowCase):
    def gate_first_item(self):
        """1件目を刷り始めたところで止める(放すまで進まない)。時間に頼らずに途中の画面を見るため。"""
        backend = self.biz.excel.backend
        original = backend.print_files
        started, go = threading.Event(), threading.Event()
        self.addCleanup(go.set)

        def gated(files, copies, on_event, should_continue):
            def event(kind, index, *rest):
                on_event(kind, index, *rest)
                if kind == "begin" and index == 1:
                    started.set()
                    go.wait(15)
            return original(files, copies, event, should_continue)

        backend.print_files = gated
        return started, go

    def test_確認_進み具合_途中のエラー_終わり_印刷中のほかのボタン(self) -> None:
        items = [self.item(n) for n in ("消火器点検", "エラー確認用", "クレーン点検")]
        page = self.open_flow(selection=items)
        page.wait_for_selector("#sel-list li")

        # 部数の ＋ − (1 より下にはしない)
        page.click("#copies-inc")
        self.assertEqual(page.input_value("#copies"), "2")
        page.click("#copies-dec")
        page.click("#copies-dec")
        self.assertEqual(page.input_value("#copies"), "1")
        # 部数が数でなければ、確認を出す前に言う
        page.fill("#copies", "たくさん")
        self.assertEqual(page.get_attribute("#copies", "aria-invalid"), "true")
        page.click("#btn-print")
        self.wait_toast(page, "印刷部数は1～100の数値で入力してください。")
        self.assertFalse(page.evaluate("document.getElementById('confirm-dialog').open"))
        self.assertEqual(page.evaluate("document.activeElement.id"), "copies")

        # 全角の数字も部数にする。確認で「キャンセル」なら何も送らない
        page.fill("#copies", "２")
        page.click("#btn-print")
        page.wait_for_selector("#confirm-dialog[open]")
        self.assertIn("3 件の点検表を 2 部ずつ印刷します。", page.inner_text("#confirm-body"))
        page.click("#confirm-no")
        page.wait_for_selector("#confirm-dialog:not([open])", state="attached")
        self.assertEqual(self.posts_to("/api/print"), [])

        # 印刷する → 1件目の途中で止めておき、進み具合とほかのボタンを見る
        started, go = self.gate_first_item()
        page.click("#btn-print")
        page.click("#confirm-yes")
        page.wait_for_selector("#print-dialog[open]")
        self.assertTrue(started.wait(10))
        self.wait_text(page, "#print-current", "消火器点検を印刷しています")
        self.assertTrue(page.inner_text("#print-counter").startswith("1 / 3"))
        self.assertEqual(self.posts_to("/api/print"), [{"ids": [it["id"] for it in items], "copies": 2}])
        self.assertTrue(page.is_visible("#print-cancel"))
        self.assertTrue(page.is_hidden("#print-close"))
        self.assertTrue(page.is_visible("#rb-running"))
        # 印刷中は選び直し・読み直し・設定・もう1回の印刷をさせない
        self.assertEqual(set(self.disabled(page, "btn-refresh", "btn-settings", "btn-preview", "btn-print",
                                           "btn-clear-sel").values()), {True})
        self.assertTrue(all(page.evaluate("""() => [...document.querySelectorAll('#sel-list button')]
            .filter((b) => b.getAttribute('aria-label') !== 'プレビュー').map((b) => b.disabled)""")))
        # Esc で閉じない・Ctrl+P でもう1回始めない・窓の外は押せない(モーダル)
        page.keyboard.press("Escape")
        page.keyboard.press("Control+p")
        page.wait_for_timeout(300)
        self.assertTrue(page.evaluate("document.getElementById('print-dialog').open"))
        self.assertEqual(len(self.posts_to("/api/print")), 1, "印刷中に Ctrl+P でもう1回始めた")
        self.assertFalse(page.evaluate("""() => { const r = document.getElementById('btn-refresh').getBoundingClientRect();
            const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
            return !!hit && !!hit.closest('#btn-refresh'); }"""), "印刷の窓の外のボタンが押せる")

        # 放す → 終わり(エラーあり)。失敗した1件は理由と原因、まとめに問い合わせ番号
        go.set()
        self.wait_text(page, "#print-title", "印刷完了(エラーあり)", exact=True, timeout=15000)
        summary = page.inner_text("#print-summary")
        self.assertIn("印刷完了: 2 ファイル(2 部ずつ)", summary)
        self.assertIn("エラー: 1 ファイル", summary)
        self.assertRegex(summary, r"問い合わせ番号: \d{4}-\d{4}-[0-9A-Z]{4}")
        failed = page.inner_text("#print-items li:nth-child(2)")
        self.assertIn("✕", failed)
        self.assertIn("印刷に失敗しました。", failed)
        self.assertIn("模擬エラー", failed)
        self.assertEqual(page.inner_text("#print-counter"), "3 / 3")
        self.assertTrue(page.is_hidden("#print-cancel"))
        self.assertEqual(page.evaluate("document.activeElement.id"), "print-close")

        # 閉じる → 選んだものはそのまま。また押せる
        page.click("#print-close")
        page.wait_for_selector("#print-dialog:not([open])", state="attached")
        self.assertEqual(len(self.selected(page)), 3)
        self.assertEqual(set(self.disabled(page, "btn-refresh", "btn-settings", "btn-preview", "btn-print",
                                           "btn-clear-sel").values()), {False})
        self.assertTrue(page.is_hidden("#rb-running"))
        self.assert_clean()

    def test_中止すると_いまの1件で止まり_残りは刷らない(self) -> None:
        items = [self.item(n) for n in ("消火器点検", "台車点検", "クレーン点検")]
        page = self.open_flow(selection=items)
        page.wait_for_selector("#sel-list li")
        started, go = self.gate_first_item()
        page.click("#btn-print")
        page.click("#confirm-yes")
        page.wait_for_selector("#print-dialog[open]")
        self.assertTrue(started.wait(10))
        self.wait_text(page, "#print-current", "消火器点検を印刷しています")

        page.click("#print-cancel")
        self.wait_text(page, "#print-summary", "中止しています(いまの1件が終わると止まります)")
        self.assertTrue(page.is_disabled("#print-cancel"), "中止を2回押せる")
        go.set()
        self.wait_text(page, "#print-title", "印刷を中止しました", exact=True, timeout=15000)
        self.assertIn("中止したため印刷していない点検表があります", page.inner_text("#print-summary"))
        self.assertIn("✓", page.inner_text("#print-items li:nth-child(1)"))
        for n in (2, 3):
            self.assertIn("中止したため印刷していません。", page.inner_text(f"#print-items li:nth-child({n})"))
        page.click("#print-close")
        page.wait_for_selector("#print-dialog:not([open])", state="attached")
        self.assertFalse(page.is_disabled("#btn-print"))
        self.assert_clean()


class PrintThroughWorkerFlowTests(FlowCase):
    """処理プログラム(`excel_worker.vbs`)の偽物 `fake_worker.py` を通して印刷する。

    見本の模擬(`DummyExcelBackend`)は Python の中で済むので、処理プログラムから返った失敗の中身
    (プリンターがオフライン・Excel が起動できない)が、画面に届くところまでは通らない。
    """

    def setUp(self) -> None:
        super().setUp()
        from app.services.settings_service import ExcelConfig
        FakeWorkerBackend.extra = {}
        self.addCleanup(setattr, FakeWorkerBackend, "extra", {})
        self.root = os.path.join(self.local, "点検表")
        make_tree(self.root, ["梱包/日常/梱包_日常_台車点検.xlsx", "梱包/日常/梱包_日常_fail_コンベア点検.xlsx",
                              "梱包/日常/梱包_日常_結束機点検.xlsm"])
        self.use_plain_app()
        cfg = self.biz.cfg
        cfg.excel = ExcelConfig(print_item_timeout_sec=20, preview_timeout_sec=20, excel_exit_grace_sec=1,
                                keep_alive_sec=0)
        backend = FakeWorkerBackend(cfg, os.path.join(self.local, "work_fake"), self.biz.registry, self.biz.log)
        self.biz.excel.backend = backend
        self.addCleanup(backend.shutdown, "試験の後片付け")
        self.biz.settings.update(root_folder=self.root)
        self.biz.inspection.start_scan("test", supersede=True)
        self.assertTrue(self.biz.inspection.wait(10))

    def print_all(self, page) -> None:
        page.click("#btn-cat-all")
        page.wait_for_selector("#sel-list li")
        page.click("#btn-print")
        page.click("#confirm-yes")
        page.wait_for_selector("#print-dialog[open]")

    def test_処理プログラムの失敗の中身が1件ずつ画面に出る(self) -> None:
        page = self.open_flow()
        self.print_all(page)
        self.wait_text(page, "#print-title", "印刷完了(エラーあり)", exact=True, timeout=30000)
        failed = page.inner_text("#print-items li:nth-child(1)")     # 名前順で fail_コンベア点検 が先
        self.assertIn("fail_コンベア点検", failed)
        self.assertIn("印刷に失敗しました。", failed)
        self.assertIn("プリンターがオフラインです", failed, "処理プログラムが返した原因が画面に出ない")
        self.assertIn("✓", page.inner_text("#print-items li:nth-child(2)"))
        self.assertIn("✓", page.inner_text("#print-items li:nth-child(3)"))
        self.assertIn("エラー: 1 ファイル", page.inner_text("#print-summary"))
        page.click("#print-close")

        # Excel が起動できない: 全部刷れない。理由を言って、固まらずに閉じられる
        FakeWorkerBackend.extra = {"fake_no_excel": "1"}
        page.click("#btn-print")
        page.click("#confirm-yes")
        page.wait_for_selector("#print-dialog[open]")
        page.wait_for_selector("#print-close:not([hidden])", timeout=30000)
        # 1件も刷れていないのに「印刷完了」と言わない(VER2.4.7)
        self.wait_text(page, "#print-title", "印刷できませんでした", exact=True, timeout=15000)
        self.assertNotIn("印刷完了", page.inner_text("#print-summary"))
        self.assertIn("印刷できた点検表はありません", page.inner_text("#print-summary"))
        items = page.inner_text("#print-items")
        self.assertEqual(items.count("Excelを起動できませんでした。"), 3, items)
        self.assertIn("ActiveX component can't create object", items, "処理プログラムが返した原因が出ない")
        self.assertIn("エラー: 3 ファイル", page.inner_text("#print-summary"))
        self.assertRegex(page.inner_text("#print-summary"), r"問い合わせ番号: \d{4}-\d{4}-[0-9A-Z]{4}")
        page.click("#print-close")
        page.wait_for_selector("#print-dialog:not([open])", state="attached")
        self.assertFalse(page.is_disabled("#btn-print"))
        self.assert_clean()


# =====================================================================
# 6. なぜなぜ・ログ
# =====================================================================
class LogsFlowTests(FlowCase):
    def failed_print_ref(self, page) -> str:
        page.click("#btn-print")
        page.click("#confirm-yes")
        page.wait_for_selector("#print-dialog[open]")
        # 1件も刷れなければ「印刷できませんでした」、一部なら「印刷完了(エラーあり)」(VER2.4.7)
        page.wait_for_selector("#print-close:not([hidden])", timeout=15000)
        self.assertIn(page.inner_text("#print-title").strip(), ("印刷完了(エラーあり)", "印刷できませんでした"))
        ref = REF.search(page.inner_text("#print-summary")).group(0)
        page.click("#print-close")
        page.wait_for_selector("#print-dialog:not([open])", state="attached")
        return ref

    def open_logs(self, page) -> None:
        page.click("#btn-settings")
        page.wait_for_selector("#settings-dialog[open]")
        page.click("#tab-logs")
        self.wait_text(page, "#log-dir", "inspection")

    def test_問い合わせ番号で引き_保存先を替えても前の番号を引ける(self) -> None:
        page = self.open_flow(selection=[self.item("エラー確認用")])
        page.wait_for_selector("#sel-list li")
        ref = self.failed_print_ref(page)
        self.open_logs(page)

        # 最近のエラー・断りに出ている。押すと、なぜなぜが出る
        self.wait_text(page, "#log-recent", ref)
        page.click(f"#log-recent button:has-text('{ref}')")
        page.wait_for_selector("#log-detail-card:not([hidden])")
        self.assertEqual(page.inner_text("#log-detail-ref"), ref)
        detail = page.inner_text("#log-detail")
        for part in ("なぜなぜ", "現象", "そのときの状況", "直前に起きていたこと"):
            self.assertIn(part, detail)
        self.assertIn("エラー確認用", detail)

        # 小文字で打っても・Enter でも引ける
        page.fill("#log-ref", ref.lower())
        page.press("#log-ref", "Enter")
        self.wait_text(page, "#log-detail-ref", ref, exact=True)
        self.assertEqual(page.input_value("#log-ref"), ref)
        # 無い番号・空: 言う(ほかの端末の番号なら、の手がかりも)
        page.fill("#log-ref", "0000-0000-ZZZZ")
        page.click("#log-find")
        self.wait_text(page, "#log-find-why", "記録が見つかりません")
        self.assertIn("別の端末", page.inner_text("#log-find-why"))
        page.fill("#log-ref", "")
        page.click("#log-find")
        self.wait_text(page, "#log-find-why", "問い合わせ番号を入力してください。", exact=True)

        # 最近の断りの名前は現場の言葉(要求の宛先 `logs.find` をそのまま出さない)
        page.click("#log-reload")
        self.wait_text(page, "#log-recent", "0000-0000-ZZZZ")
        recent = page.inner_text("#log-recent")
        self.assertIn("問い合わせ番号で探す", recent)
        self.assertNotIn("logs.find", recent)

        # 文にして写す(この試験の宛先は 127.0.0.1 でないのでクリップボードを使えない →
        # 写すための窓に文を出す。本物の画面はクリップボードへ写して「写しました」と言う)
        prompts: list[str] = []
        page.on("dialog", lambda d: prompts.append(d.default_value) if d.type == "prompt" else None)
        page.click("#log-copy")
        self.wait_until(page, lambda: bool(prompts), "写す文")
        self.assertIn(f"【問い合わせ番号】{ref}", prompts[0])
        self.assertIn("【現象】", prompts[0])

        # 保存先を共有フォルダへ: そこの inspection/ に書く。前の(この PC の)番号も引ける
        shared = os.path.join(self.local, "共有ログ")
        os.makedirs(shared)
        page.fill("#log-dir-input", shared)
        page.click("#log-save")
        self.wait_text(page, "#log-state", "指定のフォルダ", exact=True)
        self.assertTrue(any(n.startswith("inspection_") for n in os.listdir(os.path.join(shared, "inspection"))),
                        "保存先の inspection/ にログが無い")
        self.assertFalse(any(n.startswith("inspection_") for n in os.listdir(shared)),
                         "保存先の直下に書いた(inspection/ の中に書く)")
        page.fill("#log-ref", ref)
        page.click("#log-find")
        self.wait_text(page, "#log-detail-ref", ref, exact=True)
        self.assertTrue(page.is_hidden("#log-find-why"))
        page.click("#log-reload")
        self.wait_text(page, "#log-recent", ref)

        # 保存先を替えたあとの失敗も引ける(共有の inspection/ から)
        page.click("#settings-close")
        page.wait_for_selector("#settings-dialog:not([open])", state="attached")
        later = self.failed_print_ref(page)
        self.open_logs(page)
        page.fill("#log-ref", later)
        page.click("#log-find")
        self.wait_text(page, "#log-detail-ref", later, exact=True)
        page.click("#log-reset")
        self.wait_text(page, "#log-state", "この PC", exact=True)
        self.assert_clean()

    def test_断りの名前はどれも現場の言葉(self) -> None:
        """要求の宛先から作る名前(`settings.admin-password` など)を、そのまま「最近のエラー」に出さない。"""
        page = self.open_flow()
        page.click("#btn-settings")
        page.wait_for_selector("#settings-dialog[open]")
        page.click("#tab-password")
        page.fill("#adm-now", "まちがい")
        page.fill("#adm-new", "line-1234")
        page.fill("#adm-confirm", "line-1234")
        page.click("#adm-save")
        page.wait_for_selector("#adm-why:not([hidden])")
        page.click("#tab-distribution")
        page.fill("#dist-password", "まちがい")
        page.click("#dist-export")
        page.wait_for_selector("#dist-why:not([hidden])")
        page.fill("#dist-password", "まちがい")
        page.click("#dist-remove")
        page.wait_for_function("document.getElementById('dist-why').innerText.trim() !== ''")
        # 消えた PDF(控えが片付いた)を開いた
        self.assertEqual(self.get("/api/preview/pdf/0123456789abcdef").status_code, 404)

        page.click("#tab-logs")
        page.wait_for_selector("#log-recent li button")
        recent = page.inner_text("#log-recent")
        for raw in ("settings.admin-password", "settings.distribution.export", "settings.distribution.remove",
                    "preview.pdf"):
            self.assertNotIn(raw, recent, f"宛先 {raw} をそのまま出した")
        for name in ("管理者パスワード", "配布設定の書き出し", "配布設定の削除", "プレビュー"):
            self.assertIn(name, recent)
        self.assert_clean()


# =====================================================================
# 7. 画面の色 / 8. 断る場面
# =====================================================================
class ThemeAndRefusalFlowTests(FlowCase):
    def test_画面の色を切り替えて開き直しても残る(self) -> None:
        ctx = self.new_context()
        page = self.open_flow(ctx=ctx)
        self.assertEqual(page.inner_text("#btn-theme"), "◇ 既定")
        page.click("#btn-theme")
        page.wait_for_function("document.documentElement.dataset.theme === 'dark'")
        self.assertEqual(page.inner_text("#btn-theme"), "☾ ダーク")
        self.wait_until(page, lambda: self.biz.settings.theme_choice() == "dark", "ダークを覚える")
        page.reload(wait_until="load")
        page.wait_for_selector("#items .item")
        self.assertEqual(page.get_attribute("html", "data-theme"), "dark", "開き直したらライトに戻った")
        self.assertEqual(page.inner_text("#btn-theme"), "☾ ダーク")
        # 既定へ戻す(大設定に従う)→ 開き直しても既定
        page.click("#btn-theme")
        page.wait_for_function("document.documentElement.dataset.theme === 'light'")
        self.assertEqual(page.inner_text("#btn-theme"), "◇ 既定")
        self.wait_until(page, lambda: self.biz.settings.theme_choice() == "default", "既定に戻す")
        page.reload(wait_until="load")
        page.wait_for_selector("#items .item")
        self.assertEqual(page.inner_text("#btn-theme"), "◇ 既定")
        self.assert_clean()

    def test_サーバに届かない_断られた_ときも固まらずに言う(self) -> None:
        excel = self.item("消火器点検")
        ctx = self.new_context()
        page = self.open_flow(ctx=ctx, selection=[excel])
        page.wait_for_selector("#sel-list li")

        # 再読込がサーバに届かない → つながらないと言う。ボタンは戻る
        ctx.route("**/api/inventory/refresh", lambda route: route.abort())
        page.click("#btn-refresh")
        self.wait_toast(page, "サーバーに接続できません")
        page.wait_for_function("!document.getElementById('btn-refresh').disabled")
        ctx.unroute("**/api/inventory/refresh")

        # 画面の色を覚えられない → 元の色に戻して言う
        self.refuse["/api/settings"] = 500
        page.click("#btn-theme")
        self.wait_toast(page, "試験で断りました")
        page.wait_for_function("document.documentElement.dataset.theme === 'light'")
        self.assertEqual(page.inner_text("#btn-theme"), "◇ 既定")
        # 設定を開けない → 言う。設定の窓は開かない(半端な窓を出さない)
        page.click("#btn-settings")
        page.wait_for_function("document.querySelectorAll('.toast--error').length >= 3")
        self.assertFalse(page.evaluate("document.getElementById('settings-dialog').open"))
        del self.refuse["/api/settings"]

        # 印刷の受付で断られた → 言う。印刷中のまま固まらない
        self.refuse["/api/print"] = 409
        page.click("#btn-print")
        page.click("#confirm-yes")
        page.wait_for_function("document.querySelectorAll('.toast--error').length >= 4")
        page.wait_for_function("!document.getElementById('btn-print').disabled")
        self.assertFalse(page.is_disabled("#btn-refresh"))
        self.assertFalse(page.evaluate("document.getElementById('print-dialog').open"))
        del self.refuse["/api/print"]

        # エラーの知らせは勝手に消えない代わりに、閉じられる
        page.locator(".toast--error button:has-text('閉じる')").first.click()
        page.wait_for_function("document.querySelectorAll('.toast--error').length === 3")
        self.assert_clean()


if __name__ == "__main__":
    unittest.main()
