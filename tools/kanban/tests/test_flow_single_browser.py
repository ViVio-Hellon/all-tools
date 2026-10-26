"""看板を**1台の端末で使う一連の流れ**を、本物のブラウザで通しで確かめる

現場の人が押す順に、画面のボタン・窓を実際に押して回ります(API を直に叩かない)。
共有DB(看板マスタ.sqlite3)・看板履歴・梱包資材マスタは試験の一時フォルダに本物の
SQLite で作り、取り込み・書き戻しも本物(:mod:`kanban.db.sync`)を通します。起動
(``start_app._serve`` がする組み立て)は :meth:`FlowCase.boot` が同じ順に真似ます。
「開き直す」は、同じ手元の SQLite・同じ設定ファイルでもう一度 :meth:`FlowCase.boot` します。

1. 初めての起動: 設定がまっさら → 盤の案内 → 設定(接続先・管理者パスワード・担当ライン)→ 盤
2. 現場: 看板を押して提出 → 色・状態・「未反映」→ 取り消し → 届いた確認。押せない看板の理由
3. 倉庫: 提出の届いた看板の発送 → 注文中(押せる条件・理由)→ 発送済み。倉庫参照は押せない
4. モードの切り替え(⇄)・担当ラインの切り替え・ラインのタブ
5. 印刷: 注文票・明細が無いとき・印刷のページの中身が盤と同じ
6. 看板集計・看板履歴: 期間・ライン・CSV
7. マスタ管理: 看板の追加・直す・消す(提出中などは消せない)・並び順・Access の最新で入れ替え
8. 記録タブ・なぜなぜ分析(記録番号から引く)・ログの置き場所(``<置き場所>/kanban/…``)
9. 設定のその他の面(画面の色・取り込みの間隔・配布設定・パスワード)。開き直しても残る
10. どの場面でも画面の誤り(pageerror)と console.error が出ない・断るときは理由を言う

共有フォルダを使った複数台の見え方は別の試験が見ます。
無い環境では飛ばします(CI では飛ばしません: ``ALLTOOLS_REQUIRE_BROWSER=1``)。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from kanban import config
from kanban.db.store import Store
from tests._kanban_browser import TOKEN, KanbanBrowserCase

#: 共有DBの看板の表(本番と同じ列。管理番号は数)
COLS = ("[管理番号] NUMERIC, [資材] TEXT, [サイズ] TEXT, [欲] TEXT, [不] TEXT, [更新日] TEXT,"
        " [発送] TEXT, [倉庫確認日時] TEXT, [常設品] TEXT, [保留] TEXT, [注文中日時] TEXT")

#: 看板(管理番号, 資材, サイズ, 常設品)。ハードボードは非常設品
CARDS = (
    (1, "外装紙", "2200 × 50M : 3本", "〇"),
    (2, "外装紙", "1900 × 50M : 5本", "〇"),
    (3, "アングル", "2510 : 3束", "〇"),
    (4, "アングル", "2010 : 3束", "〇"),
    (5, "ハードボード", "660 × 1050 : 100枚", "×"),
)

PASSWORD = "秘密"


def make_shared(path: Path, lines=("LVC", "HVC", "LS")) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path))
    for line in lines:
        db.execute(f"CREATE TABLE [看板_{line}] ({COLS})")
        db.executemany(f"INSERT INTO [看板_{line}] VALUES (?,?,?,'','〇','','','',?,'','')",
                       [(no, material, size, permanent) for no, material, size, permanent in CARDS])
    db.commit()
    db.close()


class FlowCase(KanbanBrowserCase):
    """本物の共有DB・取り込み・書き戻しをつないだ 1 台の端末。

    数えないもの(console.error):
    - 印刷のページが自分で取りに行く favicon(取り次ぎを通らない。画面の誤りではない)
    - **わざと断った返事(4xx)にブラウザ自身が出す「Failed to load resource」**。パスワードを
      求める 401・業務として断る 422 など、画面が理由を出して受け止める断りで、JS の誤りではない。
      ``refusals`` に分けて残し、5xx は数える(サーバの思わぬ誤り)
    """

    MODE = config.MODE_SITE
    LINE = "LVC"
    #: True なら、設定がまっさら・手元の SQLite も空で始める(初めての起動)
    FIRST_RUN = False
    #: この端末(ログインID・PC名)に付けるアクセス権限(梱包資材マスタ)。空なら行を作らない(現場だけ)
    PERMISSION = ""
    #: 押したらその場で共有DBへ送る(起動側の ``exporter.request_now()`` の代わり)。
    #: False なら ``send()`` を呼ぶまで送らない(「未反映」を見るとき)
    AUTO_SEND = True

    maxDiff = None

    def setUp(self) -> None:
        super().setUp()
        from kanban import applog, trace

        # 本物のユーザーのフォルダを汚さない: 設定・手元領域・記録をこの試験の中へ
        self.home = self.dir / "端末"
        env = {
            "KANBAN_LOCAL_DIR": str(self.home / "local"),
            "KANBAN_CONFIG": str(self.dir / "config.json"),
            "APPDATA": str(self.home / "appdata"),
            "LOCALAPPDATA": str(self.home / "localappdata"),
            "KANBAN_DISTRIBUTION_DIR": str(self.dir / "アプリ" / "配布設定"),
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        saved = (trace._dir, dict(trace._context), applog.current_dir())

        def restore() -> None:
            applog.close()
            trace._dir = saved[0]
            trace._context.clear()
            trace._context.update(saved[1])

        self.addCleanup(restore)
        from kanban import table_refresh

        self.addCleanup(table_refresh._cache.clear)

        self.shared_dir = self.dir / "共有"
        self.shared = self.shared_dir / "看板マスタ.sqlite3"
        make_shared(self.shared)
        self.packing = self.shared_dir / config.ACCESS_DB_NAME
        sqlite3.connect(str(self.packing)).close()
        if self.PERMISSION:
            self.grant(self.PERMISSION)

        # 手元の SQLite は空から(RouteTestBase の見本の行は使わない)
        self.store.close()
        self.store = Store(str(self.home / "kanban.sqlite3"), host_name="PC-FLOW")
        self.store.ensure_schema()
        if not self.FIRST_RUN:
            self.write_config(shared_db_path=str(self.shared), line=self.LINE or "LVC",
                              admin_password_hash=config.hash_password(PASSWORD))
            self.store.set_device_mode(self.MODE)
        self.console: list[str] = []
        self.refusals: list[str] = []
        # 画面を移るときの「閉じます」(sendBeacon)は Playwright の取り次ぎを通らない。届かないと
        # 前のページが持ち主のまま残り、移った先に「この画面では操作できません」が出る。本物の
        # ブラウザは移る**前に**送るので、ページを移る要求(_route)が来た時点で前の名札を閉じる
        # (keepalive の fetch で送らせると、新しいページの心拍と届く順が決まらない)
        self._screen_of: dict = {}
        self.ctx.add_init_script("navigator.sendBeacon = () => true;")
        # 出た知らせ(トースト)を Python 側に残す。保存のあと画面が読み直すと、画面の中の控え
        # (``window.__toasts``)は消える
        self.seen: list[str] = []
        self.ctx.expose_binding("__toastSeen", lambda _src, text: self.seen.append(text))
        self.ctx.add_init_script("""
          new MutationObserver((list) => {
            for (const m of list) for (const n of m.addedNodes) {
              if (n.nodeType === 1 && n.classList && n.classList.contains('toast')) {
                window.__toastSeen(n.textContent);
              }
            }
          }).observe(document, { childList: true, subtree: true });
        """)
        self.boot()

    def grant(self, permission: str) -> None:
        """この端末にアクセス権限の行を足す(マスタ管理で足したのと同じ)。"""
        from kanban import access_control as ac

        me = ac.current_identity()
        conn = sqlite3.connect(str(self.packing))
        try:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (ac.TABLE,)).fetchone():
                conn.execute(ac.DDL)
            conn.execute(f'INSERT INTO "{ac.TABLE}" ("ログインID", "PC名", "権限", "有効") VALUES (?, ?, ?, 1)',
                         (me.login_id, me.pc_name, permission))
            conn.commit()
        finally:
            conn.close()

    # -- 起動(start_app._prepare / _serve と同じ組み立て) ------------------
    @property
    def config_path(self) -> Path:
        return self.dir / "config.json"

    def write_config(self, **values) -> None:
        self.config_path.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")

    def cfg(self) -> config.Config:
        return config.load_config(self.config_path)

    def boot(self) -> None:
        """アプリを開く(開き直す)。モード・担当ライン・権限は設定と手元の SQLite から決める。"""
        import start_app
        from app import create_app
        from kanban import admin_lock, idle_exit, screen
        from kanban.db import history as history_db
        from kanban.db import sync
        from kanban.db.history import HistoryDb
        from kanban.db.shared import SharedDb

        from kanban import applog

        for part in (screen, idle_exit, admin_lock):
            part.reset()
        cfg = self.cfg()
        applog.initialize(cfg.resolved_log_dir(), line_name=cfg.line)
        from kanban import access_control, trace

        me = access_control.current_identity()
        trace.set_context(pc=me.pc_name, login=me.login_id)
        mode = self.store.get_device_mode() or config.MODE_SITE
        if not self.store.get_device_mode():
            self.store.set_device_mode(mode)
        mode, grant, notice = start_app._apply_access(cfg, self.store, mode)
        line = cfg.line
        self.mode = mode
        trace.set_context(mode=mode, line=line if mode == config.MODE_SITE else "")
        self.store.records_seen = mode == config.MODE_WAREHOUSE
        cache = str(self.home / "cache")
        gateway = SharedDb(cfg.resolved_shared_db_path(), busy_timeout_ms=cfg.busy_timeout_ms,
                           max_retry=cfg.max_retry, cache_dir=cache)
        sync.import_all(self.store, gateway, lines=start_app._relevant_lines(mode, line) or None)
        history = history_db.open_db(cfg)
        start_app._prepare_history(gateway, mode, history)
        start_app._prepare_access(cfg, mode)

        holder: dict = {}
        self.importer = sync.Importer(
            self.store, gateway, interval_sec=cfg.import_interval_sec,
            lines=lambda: start_app._relevant_lines(mode, holder["app"].config.get("LINE") or line))
        self.exporter = sync.Exporter(self.store, gateway, interval_sec=cfg.export_interval_sec,
                                      history_gateway=history)

        def manual_refresh():
            self.importer.gateway.forget_copy()
            return self.importer.run_once()

        def reconnect(new_path: str):
            fresh = SharedDb(new_path, busy_timeout_ms=cfg.busy_timeout_ms, max_retry=cfg.max_retry,
                             cache_dir=cache)
            self.importer.gateway = fresh
            self.exporter.gateway = fresh
            holder["app"].config["GATEWAY"] = fresh
            hist = set_history(self.cfg().resolved_history_db_path())
            start_app._prepare_history(fresh, mode, hist)
            return self.importer.run_once()

        def set_history(path: str):
            fresh = HistoryDb(path, busy_timeout_ms=cfg.busy_timeout_ms, max_retry=cfg.max_retry)
            self.exporter.history_gateway = fresh
            return fresh

        def change_history(path: str):
            hist = set_history(path)
            start_app._prepare_history(self.importer.gateway, mode, hist)
            return hist

        def changed(_line):
            if self.AUTO_SEND:
                self.exporter.run_once()

        app = create_app(mode, token=TOKEN, port=8741, store=self.store, line=line,
                         on_manual_refresh=manual_refresh, on_reconnect=reconnect,
                         on_changed=changed if mode != config.MODE_WAREHOUSE_VIEW else None)
        holder["app"] = app
        app.config.update(CONFIG_PATH=str(self.config_path), IMPORT_INTERVAL_SEC=cfg.import_interval_sec,
                          GATEWAY=gateway, ACCESS_GRANT=grant, STARTUP_NOTICE=notice,
                          SET_HISTORY_DB=change_history, READY=True)
        self.app = app
        self.client = app.test_client()

    def send(self) -> None:
        """書き戻しを 1 回(起動側の周期の代わり)。"""
        self.exporter.run_once()

    # -- 共有DB --------------------------------------------------------------
    def shared_row(self, line: str, no: int) -> dict:
        conn = sqlite3.connect(str(self.cfg().resolved_shared_db_path()))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(f"SELECT * FROM [看板_{line}] WHERE [管理番号] = ?", (no,)).fetchone()
            return dict(row) if row else {}
        finally:
            conn.close()

    def set_shared(self, line: str, no: int, **cols) -> None:
        """ほかの端末が共有DBへ書いたのと同じにする。"""
        conn = sqlite3.connect(str(self.shared))
        try:
            sets = ", ".join(f"[{k}] = ?" for k in cols)
            conn.execute(f"UPDATE [看板_{line}] SET {sets} WHERE [管理番号] = ?", (*cols.values(), no))
            conn.commit()
        finally:
            conn.close()

    # -- 画面 ----------------------------------------------------------------
    def _route(self, route) -> None:
        from urllib.parse import parse_qs, urlsplit

        from kanban import screen

        req = route.request
        try:
            page = req.frame.page
        except Exception:                          # noqa: BLE001 - 働き手(worker)の要求など
            page = None
        parts = urlsplit(req.url)
        if parts.path == "/api/alive" and page is not None:
            sid = (parse_qs(parts.query).get("screen") or [""])[0]
            if sid and "leaving=1" not in parts.query:
                self._screen_of[page] = sid
        elif req.is_navigation_request() and page is not None and page in self._screen_of:
            screen.get().leave(self._screen_of.pop(page))      # 移る前の「閉じます」
        super()._route(route)

    def _on_console(self, m) -> None:
        if m.type != "error":
            return
        url = str(m.location.get("url", ""))
        if url.endswith("/favicon.ico"):
            return              # 印刷のページが自分で取りに行く(取り次ぎを通らない。画面の誤りではない)
        if re.match(r"Failed to load resource: the server responded with a status of 4\d\d", m.text):
            self.refusals.append(f"{m.text} @ {url}")
            return
        self.console.append(f"{m.text} @ {url}")

    #: 確かめの窓(confirm)に「はい」と答えるか。False なら「キャンセル」
    accept_dialogs = True

    def watch(self, page):
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.on("console", self._on_console)

        def answer(dialog) -> None:
            self.dialogs.append(dialog.message)
            if self.accept_dialogs:
                dialog.accept()
            else:
                dialog.dismiss()

        page.on("dialog", answer)
        return page

    def assert_quiet(self) -> None:
        self.assertEqual(self.errors, [], "画面の誤り(pageerror)")
        self.assertEqual(self.console, [], "console.error")

    def open_board(self, page=None, query: str = ""):
        page = self.open(f"/board{query}", page)
        page.wait_for_selector("#board .row, .empty")
        return page

    def row(self, page, no: int):
        return page.locator(f'#board .row[data-no="{no}"]')

    def cell(self, page, no: int, kind: str):
        return page.locator(f'#board .row[data-no="{no}"] .cell[data-kind="{kind}"]')

    def state(self, page, no: int, kind: str) -> str:
        return self.cell(page, no, kind).get_attribute("data-state") or ""

    def wait_state(self, page, no: int, kind: str, want: str) -> None:
        page.wait_for_function(
            "([no, kind, want]) => { const c = document.querySelector("
            "`#board .row[data-no=\"${no}\"] .cell[data-kind=\"${kind}\"]`);"
            " return c && c.dataset.state === want; }", arg=[str(no), kind, want])

    def pending(self, page) -> str:
        return self.text(page, "#rb-pending").strip()

    def wait_pending(self, page, want: str, timeout: float = 12.0) -> None:
        self.wait_until(page, lambda: self.pending(page) == want,
                        f"帯の「未反映」が {want}(いま {self.pending(page)})", timeout)

    @staticmethod
    def mark(page) -> None:
        """読み直したかを見る印(読み直すと消える)。"""
        page.evaluate("window.__stay = 1")

    def wait_reloaded(self, page, timeout: float = 10.0) -> None:
        """画面が自分で読み直す(設定を保存したあとの ``reloadSoon``)のを待つ。"""
        from playwright.sync_api import Error

        def gone() -> bool:
            try:
                return page.evaluate("window.__stay !== 1")
            except Error:                          # 読み直しの最中
                return False
        self.wait_until(page, gone, "画面を読み直した状態", timeout)
        page.wait_for_load_state("load")
        if page.url.split("?")[0].endswith("/settings"):
            page.wait_for_function("(document.getElementById('trace-from') || {}).value")

    def guarded(self, page, selector: str, path: str, password: str = PASSWORD, *, how: str = "click",
                value: str = "") -> bool:
        """守られた操作を押す。パスワードの覆いが出たら入れる(認証済みなら出ずに ``path`` へ送る)。

        訊いたら True。``how`` は ``click``(押す)か ``select``(選ぶ。``value`` を選ぶ)。
        """
        before = len(self.posted(path))
        if how == "select":
            page.select_option(selector, value)
        else:
            page.click(selector)
        self.wait_until(page, lambda: self.visible(page, "#pw") or len(self.posted(path)) > before,
                        f"パスワードを訊くか {path} へ送った状態")
        if len(self.posted(path)) > before:
            return False
        self.answer_password(page, password)
        return True

    def wait_seen(self, page, part: str, timeout: float = 8.0) -> None:
        """「part」を含む知らせが出た(読み直したあとでも見える)。"""
        self.wait_until(page, lambda: any(part in t for t in self.seen),
                        f"「{part}」の知らせが出た状態(いままで {self.seen})", timeout)

    def go(self, page, href: str) -> None:
        """左のレールで移る。"""
        page.click(f'nav.rail a[href="{href}"]')
        page.wait_for_load_state("load")
        if href == "/settings":
            page.wait_for_function("(document.getElementById('trace-from') || {}).value")
        else:
            page.wait_for_selector("#board .row, .empty")

    def color(self, page, no: int, kind: str) -> str:
        return self.cell(page, no, kind).evaluate("(el) => getComputedStyle(el).backgroundColor")


# ===========================================================================
# 1. 初めての起動
# ===========================================================================
class FirstRunFlowTest(FlowCase):
    FIRST_RUN = True

    def test_まっさらで開き_案内から設定して盤に戻る(self) -> None:
        self.assertFalse(self.config_path.exists(), "試験の前提: 設定はまっさら")
        page = self.open_board()

        with self.subTest("盤: 表示できるラインが無いことと、行き先を言う"):
            text = self.text(page, ".empty")
            self.assertIn("表示できるラインがありません", text)
            self.assertIn("担当ラインが設定されていません", text)
            page.get_by_role("link", name="設定を開く").click()
            page.wait_for_function("(document.getElementById('trace-from') || {}).value")
            self.assertTrue(page.url.split("?")[0].endswith("/settings"), page.url)

        with self.subTest("設定: パスワードが未設定なことを帯で言う"):
            self.assertIn("未設定", self.text(page, "#lockbar-text"))

        with self.subTest("接続先: まだ決めていない → 欄に入れて変更 → パスワードをその場で決める"):
            page.click("#tab-source")
            page.wait_for_function(
                "document.getElementById('source-now-state').textContent.indexOf('確かめています') < 0")
            # 既定の場所(本番の共有フォルダ)はこの PC から届かない。届かないと言う
            self.assertIn("使えません", self.text(page, "#source-now-state"))
            page.fill("#accdb", str(self.shared))
            self.assertFalse(page.locator("#save-accdb").is_disabled())
            self.mark(page)
            page.click("#save-accdb")
            page.wait_for_selector("#pw:not([hidden])")
            self.assertTrue(self.visible(page, "#pw-confirm"), "未設定なのに確認欄が無い")
            self.answer_password(page, PASSWORD)
            page.wait_for_function(
                "document.getElementById('source-result').textContent.includes('接続先を変えました')")
            self.assertIn("取り込みました", self.text(page, "#source-result"))
            self.assertEqual(self.cfg().shared_db_path, str(self.shared))
            self.assertTrue(config.verify_password(PASSWORD, self.cfg().admin_password_hash))
            self.wait_reloaded(page)
            # 読み直したあとも結果が残り、いまの接続先として出る
            self.assertIn("接続先を変えました", self.text(page, "#source-result"))
            page.click("#tab-source")
            page.wait_for_function("document.getElementById('source-now-state').textContent.includes('✔ 届いています')")
            self.assertNotIn("まだ決めていません", self.text(page, "#source-now-state"))

        with self.subTest("担当ライン: 未設定のときは選ばれていないと分かり、選ぶと決まる"):
            page.click("#tab-device")
            chosen = page.evaluate("document.getElementById('line').selectedOptions[0].textContent.trim()")
            self.assertIn("未設定", chosen, "未設定なのに LVC が選ばれて見える(LVC の現場は選び直せない)")
            self.mark(page)
            self.guarded(page, "#line", "/api/line", how="select", value="LVC")
            self.wait_seen(page, "担当ラインを LVC にしました")
            self.wait_reloaded(page)
            self.assertEqual(self.cfg().line, "LVC")
            self.assertEqual(page.input_value("#line"), "LVC")
            self.assertEqual(page.locator("#line option[value='']").count(), 0, "決めたのに「未設定」が残る")

        with self.subTest("モード: はじめは現場モード"):
            self.assertEqual(page.input_value("#mode"), config.MODE_SITE)
            self.assertIn("現場", self.text(page, ".mode-chip"))

        with self.subTest("記録の置き場所: 変えると <置き場所>/kanban/<ライン>/ に書く"):
            logs = self.dir / "記録の置き場所"
            logs.mkdir()
            page.click("#tab-trace")
            page.wait_for_function("document.getElementById('log-now-path').textContent !== ''")
            page.fill("#log-dir", str(logs))
            self.guarded(page, "#log-save", "/api/log-dir")
            page.wait_for_selector("#log-dir-result:not([hidden])")
            self.assertIn("✔", self.text(page, "#log-dir-result"), self.text(page, "#log-dir-result"))
            self.assertEqual(self.text(page, "#log-now-path"), str(logs))
            now_file = self.text(page, "#log-now-file")
            # ラインのフォルダは起動したときのライン(この起動は未設定で始めたので Unknown)
            self.assertTrue(now_file.startswith(str(logs / "kanban") + os.sep), now_file)
            self.assertTrue(Path(now_file).name.startswith("kanban_DebugLog_"), now_file)
            self.assertEqual(self.cfg().log_dir, str(logs))

        with self.subTest("盤に戻る: 担当ラインの看板が並ぶ"):
            self.go(page, "/board")
            self.assertEqual(self.row(page, 1).count(), 1, "取り込んだ看板が出ない")
            self.assertEqual(page.locator("#board .frame__title").all_inner_texts(),
                             ["外装紙", "アングル", "ハードボード"])
            self.assertIn("LVC", self.text(page, ".ribbon__slots"))
            self.assertIn("非常設品", self.text(page, "#note"))
            self.assertEqual(page.locator(".empty").count(), 0)
        self.assert_quiet()


    def test_接続先は参照で辿って選べる(self) -> None:
        page = self.open_settings("source")
        with self.subTest("参照...: パスワードが未設定ならその場で決めて、この PC のフォルダを一覧する"):
            page.click("#browse")
            page.wait_for_selector("#pw:not([hidden])")
            self.assertIn("ここで新しく設定します", self.text(page, "#pw-note"))
            self.answer_password(page, PASSWORD)
            page.wait_for_selector("#fs:not([hidden])")
            page.fill("#fs-path", str(self.shared_dir))
            page.press("#fs-path", "Enter")
            item = page.locator("#fs-list .fs-item", has_text="看板マスタ.sqlite3")
            item.wait_for()
            item.click()
            page.wait_for_selector("#fs", state="hidden")
            self.assertEqual(page.input_value("#accdb"), str(self.shared))
            self.assertIn("まだ切り替わっていません", self.text(page, "#source-edit"))
            self.assertEqual(self.cfg().shared_db_path, "", "選んだだけで保存した")
        with self.subTest("変更: 決めたばかりのパスワードで通る"):
            self.mark(page)
            self.guarded(page, "#save-accdb", "/api/shared-db-path")
            page.wait_for_function(
                "document.getElementById('source-result').textContent.includes('接続先を変えました')")
            self.wait_reloaded(page)
            self.assertEqual(self.cfg().shared_db_path, str(self.shared))
            self.assertTrue(config.verify_password(PASSWORD, self.cfg().admin_password_hash))
        self.assert_quiet()


# ===========================================================================
# 2. 現場モード
# ===========================================================================
class SiteFlowTest(FlowCase):
    MODE = config.MODE_SITE
    LINE = "LVC"
    AUTO_SEND = False

    def refresh(self, page) -> None:
        """「↻ 更新」(共有DBから取り込み直す)。"""
        page.click("#refresh")
        page.wait_for_function("document.getElementById('refresh').textContent.trim() === '↻ 更新'")

    def test_提出して取り消し_届いた確認まで(self) -> None:
        page = self.open_board()
        neutral = self.color(page, 1, "size")

        with self.subTest("はじめ: どれも提出していない。現場は発送・注文中を押せない"):
            self.assertEqual(self.state(page, 1, "size"), "neutral")
            self.assertTrue(self.cell(page, 1, "size").is_enabled())
            for kind in ("ship", "hold"):
                self.assertTrue(self.cell(page, 1, kind).is_disabled(), f"現場で {kind} を押せる")
            self.assertTrue(page.locator("#batch-reset").is_disabled(), "0 件なのに一括確認を押せる")
            self.wait_pending(page, "0")
            self.assertEqual(page.locator("[data-switch-mode]").count(), 0, "権限が無いのに切り替えが出る")

        with self.subTest("提出: 押すと赤になり、未反映が 1、届くと 0"):
            self.cell(page, 1, "size").click()
            self.wait_state(page, 1, "size", "ordered")
            self.assertNotEqual(self.color(page, 1, "size"), neutral, "色が変わらない")
            self.assertIn("発注:", self.cell(page, 1, "size").get_attribute("title") or "")
            self.wait_pending(page, "1")
            self.assertEqual(self.shared_row("LVC", 1)["欲"], "", "送る前に共有DBが変わった")
            page.wait_for_function("document.querySelector('#reports .count')?.textContent === '1'")
            self.send()
            self.wait_pending(page, "0")
            shared = self.shared_row("LVC", 1)
            self.assertEqual((shared["欲"], shared["不"]), ("〇", ""))
            self.assertTrue(shared["更新日"], "提出の時刻が届かない")

        with self.subTest("取り消し: もう一度押すと戻る"):
            self.cell(page, 1, "size").click()
            self.wait_state(page, 1, "size", "neutral")
            self.wait_pending(page, "1")
            self.send()
            self.wait_pending(page, "0")
            shared = self.shared_row("LVC", 1)
            self.assertEqual((shared["欲"], shared["不"], shared["更新日"]), ("", "〇", ""))
            page.wait_for_function("document.querySelector('#reports .count')?.textContent === '0'")

        with self.subTest("届いた確認: 倉庫が発送した看板を押すと「届いていますか」と訊き、両方戻る"):
            self.cell(page, 2, "size").click()
            self.wait_state(page, 2, "size", "ordered")
            self.send()
            self.set_shared("LVC", 2, 発送="〇", 倉庫確認日時="2026/10/25 10:00:00")
            self.refresh(page)
            self.wait_state(page, 2, "ship", "shipped")
            self.assertIn("発送:", self.cell(page, 2, "ship").get_attribute("title") or "")
            self.assertFalse(page.locator("#batch-reset").is_disabled(), "届いた看板があるのに一括確認を押せない")
            self.assertEqual(self.text(page, "#c-reset"), "1")

            self.accept_dialogs = False      # 「キャンセル」なら何も変えない
            self.cell(page, 2, "size").click()
            self.wait_until(page, lambda: any("届いていますか" in d for d in self.dialogs), "届いたかを訊いた状態")
            page.wait_for_timeout(300)
            self.assertEqual(self.state(page, 2, "size"), "ordered", "キャンセルしたのに戻った")
            self.accept_dialogs = True
            self.cell(page, 2, "size").click()
            self.wait_state(page, 2, "size", "neutral")
            self.wait_state(page, 2, "ship", "neutral")
            self.send()
            shared = self.shared_row("LVC", 2)
            self.assertEqual((shared["欲"], shared["発送"]), ("", ""))

        with self.subTest("届いた資材を一括確認"):
            for no in (3, 4):
                self.cell(page, no, "size").click()
                self.wait_state(page, no, "size", "ordered")
            self.send()
            self.set_shared("LVC", 3, 発送="〇", 倉庫確認日時="2026/10/25 10:00:00")
            self.refresh(page)
            page.wait_for_function("document.getElementById('c-reset').textContent === '1'")
            self.dialogs.clear()
            page.click("#batch-reset")
            self.wait_seen(page, "1 件をリセットしました")
            self.assertTrue(any("1 件の資材到着を確認" in d for d in self.dialogs), self.dialogs)
            self.wait_state(page, 3, "size", "neutral")
            self.assertEqual(self.state(page, 4, "size"), "ordered", "届いていない看板まで戻した")
            self.assertTrue(page.locator("#batch-reset").is_disabled())

        with self.subTest("押せない看板: 倉庫が注文中にした提出は、押せない理由を出す"):
            self.send()
            self.set_shared("LVC", 4, 保留="〇", 注文中日時="2026/10/25 11:00:00")
            self.refresh(page)
            self.wait_state(page, 4, "hold", "held")
            size = self.cell(page, 4, "size")
            self.assertTrue(size.is_disabled(), "注文中なのに現場が押せる")
            self.assertIn("倉庫が対応中です", size.get_attribute("title") or "")
            self.assertIn("注文中", self.cell(page, 4, "hold").get_attribute("title") or "")
            self.assertIn("row--locked", self.row(page, 4).get_attribute("class") or "")
            # 注文中は注文票に載せない(倉庫が対応中)
            page.wait_for_function("document.querySelector('#reports .count')?.textContent === '0'")
        self.assert_quiet()

    def test_看板にコメントを書き_書きかけは下書きに残る(self) -> None:
        page = self.open_board()
        with self.subTest("提出していない看板には書けず、理由を言う"):
            page.click('#board .row[data-no="1"] .cell--comment')
            page.wait_for_selector("#comment-modal:not([hidden])")
            self.assertIn("管理番号 1", self.text(page, "#cm-sub"))
            self.assertTrue(self.visible(page, "#cm-readonly"))
            self.assertTrue(self.text(page, "#cm-readonly").strip())
            self.assertFalse(self.visible(page, "#cm-send"))
            page.click("#cm-close")
            page.wait_for_selector("#comment-modal", state="hidden")
        self.cell(page, 1, "size").click()
        self.wait_state(page, 1, "size", "ordered")
        page.click('#board .row[data-no="1"] .cell--comment')
        page.wait_for_selector("#comment-modal:not([hidden])")
        with self.subTest("書きかけで閉じると訊き、開き直すと残っている"):
            page.fill("#cm-body", "急ぎでお願いします")
            self.dialogs.clear()
            page.click("#cm-close")
            self.wait_until(page, lambda: self.dialogs, "訊いた状態")
            self.assertIn("下書き", self.dialogs[0])
            page.wait_for_selector("#comment-modal", state="hidden")
            page.click('#board .row[data-no="1"] .cell--comment')
            page.wait_for_selector("#comment-modal:not([hidden])")
            self.assertEqual(page.input_value("#cm-body"), "急ぎでお願いします")
        with self.subTest("送ると数が付き、共有DBへ届く"):
            page.press("#cm-body", "Control+Enter")
            self.wait_seen(page, "コメントを送りました")
            page.wait_for_function("document.getElementById('cm-body').value === ''")
            self.assertIn("急ぎでお願いします", self.text(page, "#cm-thread"))
            page.click("#cm-close")
            page.wait_for_selector("#comment-modal", state="hidden")
            page.wait_for_function("document.querySelector('#board .row[data-no=\"1\"] .cell--comment').textContent === '💬1'")
            self.send()
            conn = sqlite3.connect(str(self.shared))
            try:
                rows = [" ".join(map(str, r)) for r in conn.execute("SELECT * FROM [看板コメント]")]
            finally:
                conn.close()
            self.assertEqual(len(rows), 1, rows)
            self.assertIn("急ぎでお願いします", rows[0])
        self.assert_quiet()

    def test_取り込みの状態に送っていない操作が並び_いま取り込むでも消えない(self) -> None:
        page = self.open_board()
        self.cell(page, 3, "size").click()
        self.wait_state(page, 3, "size", "ordered")
        self.go(page, "/settings")
        page.click("#tab-import")
        self.assertEqual(self.text(page, "#pending"), "1 件")
        table = self.text(page, ".pending-list")
        for part in ("アングル", "2510 : 3束", "発注", "送信待ち"):
            self.assertIn(part, table)
        self.mark(page)
        self.guarded(page, "#reimport", "/api/reimport")
        self.wait_seen(page, "ラインを取り込みました")
        self.wait_reloaded(page)
        page.click("#tab-import")
        self.assertEqual(self.text(page, "#pending"), "1 件", "取り込みで送っていない操作が消えた")
        self.send()
        page.reload(wait_until="load")
        page.wait_for_function("(document.getElementById('trace-from') || {}).value")
        self.assertEqual(self.text(page, "#pending"), "0 件")
        self.assertEqual(page.locator(".pending-list").count(), 0)
        self.assertEqual(self.shared_row("LVC", 3)["欲"], "〇")
        self.assert_quiet()

    def test_発送のあとに取り消した看板は確かめてから戻す(self) -> None:
        """倉庫が発送したのを取り込む前に取り消した(赤なし・緑あり)。帯で知らせ「確認した」で戻す。"""
        page = self.open_board()
        self.cell(page, 5, "size").click()
        self.wait_state(page, 5, "size", "ordered")
        self.send()
        self.set_shared("LVC", 5, 発送="〇", 倉庫確認日時="2026/10/25 10:00:00")
        self.cell(page, 5, "size").click()           # まだ発送を取り込んでいない画面で取り消す
        self.wait_state(page, 5, "size", "neutral")
        self.send()
        self.refresh(page)
        page.wait_for_selector("#alerts:not([hidden])")
        self.assertIn("発送処理中に注文が取り消されました", self.text(page, "#alerts"))
        self.assertIn("確かめてから「確認した」", self.text(page, "#alerts"))
        self.assertIn("row--alert", self.row(page, 5).get_attribute("class") or "")
        page.click("#alerts button:has-text('確認した')")
        self.wait_seen(page, "確認しました")
        page.wait_for_selector("#alerts", state="hidden")
        self.wait_state(page, 5, "ship", "neutral")
        self.send()
        self.assertEqual(self.shared_row("LVC", 5)["発送"], "")
        self.assert_quiet()



# ===========================================================================
# 3. 倉庫モード
# ===========================================================================
class WarehouseFlowTest(FlowCase):
    MODE = config.MODE_WAREHOUSE
    LINE = ""
    PERMISSION = "mode:material"

    def setUp(self) -> None:
        super().setUp()
        # 現場が提出した看板(共有DBに届いている)。LVC 1・2・5 と HVC 3
        for line, no in (("LVC", 1), ("LVC", 2), ("LVC", 5), ("HVC", 3)):
            self.set_shared(line, no, 欲="〇", 不="", 更新日="2026/10/25 08:00:00")
        self.boot()

    def tab(self, page, label: str) -> None:
        page.click(f"#line-tabs .tab:has-text('{label}')")
        page.wait_for_function("(l) => document.getElementById('board').dataset.line === l"
                               " && !document.getElementById('board').hasAttribute('aria-busy')",
                               arg={"HVC": "HVC", "機側": "LS", "LVC": "LVC"}[label])

    def reports(self, page) -> dict[str, str]:
        page.wait_for_function("document.querySelectorAll('#reports a').length > 0")
        return page.evaluate("""Object.fromEntries([...document.querySelectorAll('#reports a')].map(
            (a) => [a.dataset.title, a.querySelector('.count').textContent]))""")

    def test_提出の届いた看板を発送し_注文中にもできる(self) -> None:
        page = self.open_board()

        with self.subTest("はじめ: 倉庫のラインのタブ。倉庫はサイズを押さない"):
            self.assertEqual(page.locator("#line-tabs .tab").all_inner_texts(), ["LVC", "HVC", "機側"])
            self.assertEqual(page.get_attribute("#line-tabs .tab[aria-selected='true']", "data-line"), "LVC")
            self.assertTrue(self.cell(page, 1, "size").is_disabled(), "倉庫でサイズを押せる")
            self.assertEqual(self.state(page, 1, "size"), "ordered")
            self.assertEqual(self.text(page, "#c-ship"), "3")
            self.assertIn("倉庫", self.text(page, ".mode-chip"))

        with self.subTest("提出の無い看板は、発送も注文中も押せず、理由を出す"):
            for kind in ("ship", "hold"):
                cell = self.cell(page, 3, kind)
                self.assertTrue(cell.is_disabled(), f"提出の無い看板の {kind} を押せる(押しても断られるだけ)")
                self.assertIn("発注がありません", cell.get_attribute("title") or "", kind)

        with self.subTest("発送: 押すと緑になり共有DBへ届く。もう一度押すと戻る"):
            self.cell(page, 1, "ship").click()
            self.wait_state(page, 1, "ship", "shipped")
            shared = self.shared_row("LVC", 1)
            self.assertEqual(shared["発送"], "〇")
            self.assertTrue(shared["倉庫確認日時"])
            self.assertIn("発送:", self.cell(page, 1, "ship").get_attribute("title") or "")
            self.assertEqual(self.text(page, "#c-ship"), "2")
            self.cell(page, 1, "ship").click()
            self.wait_state(page, 1, "ship", "neutral")
            self.assertEqual(self.shared_row("LVC", 1)["発送"], "")

        with self.subTest("注文中: 提出のある看板は押せ、時刻が出る。発送すると注文中は外れる"):
            hold = self.cell(page, 2, "hold")
            self.assertTrue(hold.is_enabled())
            hold.click()
            self.wait_state(page, 2, "hold", "held")
            self.assertIn("注文中:", self.cell(page, 2, "hold").get_attribute("title") or "")
            self.assertEqual(self.shared_row("LVC", 2)["保留"], "〇")
            self.cell(page, 2, "ship").click()
            self.wait_state(page, 2, "ship", "shipped")
            self.wait_state(page, 2, "hold", "neutral")
            shared = self.shared_row("LVC", 2)
            self.assertEqual((shared["発送"], shared["保留"]), ("〇", ""))

        with self.subTest("印刷のボタンの数: 発送明細・非常設品リスト"):
            page.wait_for_function("""() => { const c = [...document.querySelectorAll('#reports a')]
                .map((a) => a.querySelector('.count').textContent); return c.join() === '1,1'; }""")
            got = self.reports(page)
            self.assertEqual(got.get("発送明細"), "1", got)
            self.assertEqual(got.get("非常設品リスト"), "1", got)

        with self.subTest("表示中のラインを全て発送済みに"):
            self.dialogs.clear()
            page.click("#batch-ship")
            self.wait_seen(page, "件を発送済みにしました")
            self.assertTrue(any("未発送品 2 件" in d for d in self.dialogs), self.dialogs)
            for no in (1, 5):
                self.wait_state(page, no, "ship", "shipped")
            self.assertTrue(page.locator("#batch-ship").is_disabled(), "0 件なのに押せる")
            self.assertEqual(self.shared_row("LVC", 5)["発送"], "〇")
            self.assertEqual(self.shared_row("LVC", 3)["発送"], "", "提出の無い看板まで発送した")

        with self.subTest("タブ: HVC へ移ると HVC の盤と数"):
            self.tab(page, "HVC")
            self.assertEqual(self.state(page, 3, "size"), "ordered")
            self.assertEqual(self.state(page, 1, "size"), "neutral")
            self.assertEqual(self.text(page, "#c-ship"), "1")
            self.cell(page, 3, "ship").click()
            self.wait_state(page, 3, "ship", "shipped")
            self.assertEqual(self.shared_row("HVC", 3)["発送"], "〇")
            self.assertEqual(self.shared_row("LVC", 3)["発送"], "", "LVC の同じ番号を発送した")
            self.tab(page, "機側")
            self.assertEqual(self.text(page, "#c-ship"), "0")
            self.tab(page, "LVC")
            self.assertEqual(self.state(page, 1, "ship"), "shipped")
        self.assert_quiet()

    def test_印刷のページは盤と同じ中身(self) -> None:
        self.set_shared("LVC", 2, 発送="〇", 倉庫確認日時="2026/10/25 09:00:00")
        self.set_shared("LVC", 5, 保留="〇", 注文中日時="2026/10/25 09:30:00")
        self.boot()
        page = self.open_board()
        page.wait_for_function("""() => [...document.querySelectorAll('#reports a .count')]
            .map((c) => c.textContent).join() === '1,1'""")
        shipped = [self.cell(page, no, "size").inner_text() for no in (1, 2, 3, 4, 5)
                   if self.state(page, no, "ship") == "shipped" and self.state(page, no, "hold") != "held"]
        with self.subTest("発送明細: 発送した看板(注文中は載せない)"):
            rows = report_rows(self, page, "発送明細")
            self.assertEqual([(r[0], f"{r[1]} : {r[2]}", r[3]) for r in rows],
                             [("外装紙", shipped[0], "発送")])
        with self.subTest("非常設品リスト: 提出のある非常設品と、いまの状態"):
            rows = report_rows(self, page, "非常設品リスト")
            self.assertEqual(rows, [["ハードボード", "660 × 1050", "100枚", "注文中"]])
        self.assert_quiet()


def report_rows(case: FlowCase, page, title: str) -> list[list[str]]:
    """盤の「🖨 〜」を押して開いた印刷のページの明細(区切りの空行は除く)。"""
    link = page.locator(f'#reports a[data-title="{title}"]')
    with case.ctx.expect_page() as info:
        link.click()
    report = info.value
    case.watch(report)
    report.wait_for_load_state("load")
    case.assertIn("印刷する", report.inner_text("body"))
    rows = report.evaluate("""[...document.querySelectorAll('tbody tr:not(.separator)')]
        .map((tr) => [...tr.querySelectorAll('td:not(.pick)')].map((td) => td.textContent.trim()))""")
    report.close()
    return rows


class WarehouseViewFlowTest(FlowCase):
    MODE = config.MODE_WAREHOUSE_VIEW
    LINE = ""

    def test_倉庫参照は見るだけ(self) -> None:
        self.set_shared("LVC", 1, 欲="〇", 不="", 更新日="2026/10/25 08:00:00")
        self.boot()
        page = self.open_board()
        self.assertIn("参照専用モードです", self.text(page, "main"))
        self.assertEqual(page.locator("#line-tabs .tab").count(), 3)
        self.assertEqual(self.state(page, 1, "size"), "ordered")
        cells = page.locator("#board .cell:not(.cell--comment)")
        self.assertGreater(cells.count(), 0)
        self.assertEqual(page.locator("#board .cell:not(.cell--comment):enabled").count(), 0, "押せる升がある")
        self.assertEqual(page.locator("#batch-ship, #batch-reset").count(), 0, "一括のボタンがある")
        page.wait_for_timeout(500)
        self.assertEqual(page.locator("#reports a").count(), 0, "印刷のボタンがある")
        # 💬 は開けるが読むだけ
        page.click('#board .row[data-no="1"] .cell--comment')
        page.wait_for_selector("#comment-modal:not([hidden])")
        self.assertTrue(self.visible(page, "#cm-readonly"))
        self.assertFalse(self.visible(page, "#cm-send"), "倉庫参照で送るボタンが出る")
        page.click("#cm-close")
        page.wait_for_selector("#comment-modal", state="hidden")
        self.assertEqual(self.posted("/api/board"), [], "倉庫参照で状態を変えに行った")
        self.assert_quiet()



# ===========================================================================
# 4. モード・担当ラインの切り替え
# ===========================================================================
class SwitchFlowTest(FlowCase):
    MODE = config.MODE_SITE
    LINE = "LVC"
    PERMISSION = "Administrator"

    def reopen(self, page):
        """アプリを開き直す(ブラウザ版のモードは次に開いたときから)。"""
        self.boot()
        return self.open_board(page)

    def test_盤の切り替えボタンと設定でモードを変え_開き直すと変わる(self) -> None:
        page = self.open_board()

        with self.subTest("現場 → 倉庫(盤の ⇄)"):
            button = page.get_by_role("button", name="⇄ 倉庫モードへ")
            self.assertTrue(button.is_visible(), "現場・倉庫の両方を使える端末に切り替えが無い")
            button.click()
            self.wait_seen(page, "次にアプリを開いたときから倉庫モード")
            self.assertEqual(self.store.get_device_mode(), config.MODE_WAREHOUSE)
            self.assertIn("現場", self.text(page, ".mode-chip"), "開き直す前に札が変わった")
            page = self.reopen(page)
            self.assertIn("倉庫", self.text(page, ".mode-chip"))
            self.assertEqual(page.locator("#line-tabs .tab").count(), 3)
            self.assertEqual(page.locator("#batch-ship").count(), 1)

        with self.subTest("倉庫 → 現場(盤の ⇄)"):
            page.get_by_role("button", name="⇄ 現場モードへ").click()
            self.wait_seen(page, "次にアプリを開いたときから現場モード")
            page = self.reopen(page)
            self.assertIn("現場", self.text(page, ".mode-chip"))
            self.assertEqual(page.locator("#line-tabs").count(), 0, "現場にタブが出る")
            self.assertEqual(page.locator("#batch-reset").count(), 1)

        with self.subTest("設定から倉庫参照へ(パスワードは訊かない)"):
            self.go(page, "/settings")
            page.click("#tab-device")
            page.select_option("#mode", config.MODE_WAREHOUSE_VIEW)
            page.wait_for_selector("#mode-notice:not([hidden])")
            self.assertIn("次にアプリを開いたときから倉庫参照モード", self.text(page, "#mode-notice"))
            self.assertFalse(self.visible(page, "#pw"), "モードでパスワードを訊いた")
            page = self.reopen(page)
            self.assertIn("参照専用モードです", self.text(page, "main"))
            # 倉庫参照からは現場・倉庫のどちらへも戻れる
            self.assertEqual(page.locator("[data-switch-mode]").count(), 2)
        self.assert_quiet()

    def test_担当ラインを変えると_その場で盤がそのラインになる(self) -> None:
        page = self.open_board()
        self.assertIn("LVC", self.text(page, ".ribbon__slots"))
        self.cell(page, 1, "size").click()
        self.wait_state(page, 1, "size", "ordered")
        self.go(page, "/settings")
        page.click("#tab-device")
        self.assertEqual(page.input_value("#line"), "LVC")

        with self.subTest("パスワードの窓で「やめる」なら変わらない"):
            page.select_option("#line", "LS")
            page.wait_for_selector("#pw:not([hidden])")
            self.assertIn("機側", self.text(page, "#pw-note"))
            page.click("#pw-cancel")
            page.wait_for_selector("#pw", state="hidden")
            self.assertEqual(page.input_value("#line"), "LVC", "やめたのに欄が戻らない")
            self.assertEqual(self.cfg().line, "LVC")

        with self.subTest("違うパスワードなら断り、変わらない"):
            page.select_option("#line", "LS")
            self.answer_password(page, "ちがう")
            self.wait_seen(page, "パスワード")
            self.assertEqual(self.cfg().line, "LVC")
            self.assertEqual(page.input_value("#line"), "LVC")

        with self.subTest("正しいパスワードで変わり、盤が機側になる"):
            self.mark(page)
            page.select_option("#line", "LS")
            self.answer_password(page, PASSWORD)
            self.wait_seen(page, "担当ラインを 機側 にしました")
            self.wait_reloaded(page)
            self.assertEqual(self.cfg().line, "LS")
            self.go(page, "/board")
            self.assertIn("機側", self.text(page, ".ribbon__slots"))
            self.assertEqual(self.state(page, 1, "size"), "neutral", "LVC の提出が機側に出た")
            self.assertEqual(self.shared_row("LVC", 1)["欲"], "〇", "LVC の提出が消えた")
        self.assert_quiet()


# ===========================================================================
# 5. 印刷(現場の注文票)
# ===========================================================================
class SitePrintFlowTest(FlowCase):
    MODE = config.MODE_SITE
    LINE = "LVC"

    def test_注文票は盤の赤と同じ_明細が無いときはそう言う(self) -> None:
        page = self.open_board()
        with self.subTest("明細が無いとき: 0 件と出し、開くと「明細はありません」"):
            page.wait_for_function("document.querySelector('#reports .count')?.textContent === '0'")
            link = page.locator('#reports a[data-title="注文票"]')
            self.assertEqual(link.get_attribute("title"), "印刷する明細はありません")
            with self.ctx.expect_page() as info:
                link.click()
            empty = info.value
            self.watch(empty)
            empty.wait_for_load_state("load")
            self.assertIn("印刷する明細はありません", empty.inner_text("body"))
            self.assertIn("LVC", empty.inner_text("body"))
            empty.close()

        for no in (1, 3, 5):
            self.cell(page, no, "size").click()
            self.wait_state(page, no, "size", "ordered")
        page.wait_for_function("document.querySelector('#reports .count')?.textContent === '3'")
        with self.subTest("注文票: 盤で赤い看板(資材・サイズ・時刻)"):
            board = [(page.locator(f'#board .row[data-no="{no}"]').evaluate(
                "(r) => r.closest('.frame').querySelector('.frame__title').textContent"),
                self.cell(page, no, "size").inner_text()) for no in (1, 3, 5)]
            rows = report_rows(self, page, "注文票")
            self.assertEqual([(r[0], r[1]) for r in rows], board)
            self.assertEqual({r[2] for r in rows}, {"注文"})
            self.assertTrue(all(re.match(r"\d{4}/\d{2}/\d{2}", r[3]) for r in rows), rows)
        self.assert_quiet()


# ===========================================================================
# 6. 看板集計・看板履歴
# ===========================================================================
class StatsFlowTest(FlowCase):
    MODE = config.MODE_SITE
    LINE = "LVC"

    def test_出して届いた回が集計に出て_ラインと期間で絞り_CSVに書き出せる(self) -> None:
        import csv
        import io

        page = self.open_board()
        # 出す → 倉庫が発送 → 届いた(赤を消す)= 1 回
        self.cell(page, 1, "size").click()
        self.wait_state(page, 1, "size", "ordered")
        self.set_shared("LVC", 1, 発送="〇", 倉庫確認日時="2026/10/25 10:00:00")
        page.click("#refresh")
        self.wait_state(page, 1, "ship", "shipped")
        self.cell(page, 1, "size").click()
        self.wait_state(page, 1, "size", "neutral")
        # もう 1 枚は出しただけ(未着)
        self.cell(page, 3, "size").click()
        self.wait_state(page, 3, "size", "ordered")

        self.go(page, "/settings")
        page.click("#tab-stats")
        page.wait_for_function("document.getElementById('stats-since').textContent !== ''")
        with self.subTest("開くと集計する(パスワードは訊かない)"):
            self.assertFalse(self.visible(page, "#pw"))
            self.assertIn("回", self.text(page, "#stats-since"))
            self.assertEqual(self.text(page, "#ov-n"), "1 回")
            summary = self.text(page, "#stats-summary")
            self.assertIn("LVC", summary)
            self.assertFalse(self.visible(page, "#stats-why"), self.text(page, "#stats-why"))
            month = page.input_value("#stats-to")
            self.assertTrue(re.match(r"\d{4}-\d{2}$", month), month)

        with self.subTest("ラインで絞る: 押していないラインは 0 回"):
            page.select_option("#stats-line", "HVC")
            page.wait_for_function("document.getElementById('ov-n').textContent === '0 回'")
            self.assertNotIn("LVC", self.text(page, "#stats-summary"))
            page.select_option("#stats-line", "LVC")
            page.wait_for_function("document.getElementById('ov-n').textContent === '1 回'")

        with self.subTest("期間で絞る: 記録の無い月は 0 回"):
            page.fill("#stats-from", "2025-01")
            page.fill("#stats-to", "2025-02")
            page.click("#stats-run")
            page.wait_for_function("document.getElementById('ov-n').textContent === '0 回'")
            self.assertEqual((page.input_value("#stats-from"), page.input_value("#stats-to")),
                             ("2025-01", "2025-02"), "選んだ期間が書き換わった")
            page.fill("#stats-from", month)
            page.fill("#stats-to", month)
            page.click("#stats-run")
            page.wait_for_function("document.getElementById('ov-n').textContent === '1 回'")

        with self.subTest("CSV のダウンロード: 選んだ期間・ラインのまま"):
            href = page.get_attribute('[data-csv="cycles"]', "href")
            self.assertIn(f"from={month}", href)
            self.assertIn("line=LVC", href)
            got = page.evaluate("async (u) => { const r = await fetch(u); return [r.status, await r.text()]; }", href)
            self.assertEqual(got[0], 200)
            rows = list(csv.reader(io.StringIO(got[1].lstrip("\ufeff"))))
            self.assertGreaterEqual(len(rows), 3, rows)          # 見出し + 届いた回 + 未着の回
            self.assertTrue(any("外装紙" in ",".join(r) for r in rows[1:]), rows)

        with self.subTest("看板履歴を書き出し先へ保存"):
            page.click('[data-csv-save="history"]')
            page.wait_for_selector("#csv-result:not([hidden])")
            text = self.text(page, "#csv-result")
            self.assertIn("✔ 保存しました", text)
            saved = Path(text.split("保存しました:", 1)[1].strip())
            self.assertTrue(saved.is_file(), saved)
            self.assertTrue(str(saved).startswith(str(self.home)), "試験のフォルダの外へ書いた")
            body = saved.read_text(encoding="utf-8-sig")
            self.assertIn("LVC", body)
            self.assertGreaterEqual(len(body.strip().splitlines()), 4, body)   # 出す・届く・出す
        self.assert_quiet()


# ===========================================================================
# 7. マスタ管理
# ===========================================================================
class MasterFlowTest(FlowCase):
    MODE = config.MODE_SITE
    LINE = "LVC"

    def open_master(self, page, table: str = "看板_LVC") -> None:
        page.click("#tab-master")
        page.wait_for_selector("#master-pick:not([hidden])")
        page.select_option("#master-table", table)
        page.click("#master-open")
        page.wait_for_selector("#master-view table")

    def master_rows(self, page) -> list[dict[str, str]]:
        return page.evaluate("""() => {
          const t = document.querySelector('#master-view table');
          const head = [...t.rows[0].cells].map((c) => c.textContent.replace(/[ ▲▼]+$/, ''));
          return [...t.rows].slice(1).map((r) => Object.fromEntries(
            head.filter((h) => h).map((h, i) => [h, r.cells[i].textContent])));
        }""")

    def board_sizes(self, page) -> list[str]:
        return page.locator("#board .cell--size").all_inner_texts()

    def test_看板を足して直して消す_動いている看板は消せない(self) -> None:
        page = self.open_board()
        self.cell(page, 1, "size").click()            # 1 番は提出中にしておく
        self.wait_state(page, 1, "size", "ordered")
        self.go(page, "/settings")
        self.open_master(page)

        with self.subTest("開く: 看板の状態は直せないと言う。直す前でも足すボタンは押せる"):
            self.assertEqual(len(self.master_rows(page)), 5)
            self.assertIn("看板の状態なので、ここでは直せません", self.text(page, "#master-view"))
            self.assertFalse(page.locator("#master-add").is_disabled())
            self.assertIn(str(self.shared), self.text(page, "#master-dest"))

        with self.subTest("足す: 番号は次の連番。足すと盤にも出る"):
            page.click("#master-add")
            page.wait_for_selector("#master-form form")
            self.assertEqual(page.input_value("#new-管理番号"), "6")
            self.assertEqual(page.locator("#new-欲").count(), 0, "看板の状態の欄が出る")
            page.fill("#new-資材", "テープ")
            page.fill("#new-サイズ", "50mm : 10巻")
            if page.locator("#new-常設品").count():
                page.fill("#new-常設品", "〇")
            page.click("#master-form button[type=submit]")
            self.answer_password(page)
            page.wait_for_selector("#master-written:not([hidden])")
            self.assertIn("看板画面にもすぐ出ます", self.text(page, "#master-written"))
            page.wait_for_function("document.querySelectorAll('#master-view table tr').length === 7")
            added = self.shared_row("LVC", 6)
            self.assertEqual((added["資材"], added["サイズ"], added["不"]), ("テープ", "50mm : 10巻", "〇"))

        with self.subTest("直す: マスを押して打ち、Enter(認証済みなので訊かない)"):
            cell = page.locator("#master-view td.editable[data-column='サイズ']").last
            cell.click()
            cell.locator("input").fill("50mm : 12巻")
            cell.locator("input").press("Enter")
            self.wait_seen(page, "更新しました")
            self.assertFalse(self.visible(page, "#pw"), "認証済みなのに訊いた")
            page.wait_for_function("[...document.querySelectorAll('#master-view td')].some((td) => td.textContent === '50mm : 12巻')")
            self.assertEqual(self.shared_row("LVC", 6)["サイズ"], "50mm : 12巻")

        with self.subTest("並び順: 列名を押すと小さい順 → 大きい順 → 元の順(盤の並びは変えない)"):
            page.click("#master-view .master-sort:text-is('サイズ')")
            page.wait_for_function("document.querySelector('#master-view th[aria-sort]')?.getAttribute('aria-sort') === 'ascending'")
            sizes = [r["サイズ"] for r in self.master_rows(page)]
            self.assertEqual(sizes, sorted(sizes))
            page.click("#master-view .master-sort:has-text('サイズ')")
            page.wait_for_function("document.querySelector('#master-view th[aria-sort]')?.getAttribute('aria-sort') === 'descending'")
            self.assertEqual([r["サイズ"] for r in self.master_rows(page)], sorted(sizes, reverse=True))
            page.click("#master-view button:text-is('元の順に戻す')")
            page.wait_for_function("!document.querySelector('#master-view th[aria-sort]')")
            self.assertEqual([r["管理番号"] for r in self.master_rows(page)], ["1", "2", "3", "4", "5", "6"])

        with self.subTest("消せない: 提出中の看板は、確かめの窓で言い、消さずに理由を出す"):
            self.dialogs.clear()
            page.locator("#master-view tr", has=page.locator("td.key", has_text="1")).first \
                .locator("button:text-is('削除')").click()
            self.wait_until(page, lambda: self.dialogs, "確かめの窓が出た状態")
            self.assertIn("いま「発注中」", self.dialogs[0])
            self.wait_seen(page, "消すと、その注文の行き場が無くなります")
            self.assertEqual(self.shared_row("LVC", 1)["欲"], "〇")

        with self.subTest("消せない: 発送済み・注文中も"):
            self.set_shared("LVC", 2, 欲="〇", 不="", 発送="〇")
            self.set_shared("LVC", 3, 欲="〇", 不="", 保留="〇")
            page.click("#master-open")
            page.wait_for_function("[...document.querySelectorAll('#master-view td')].some((td) => td.textContent === '〇')")
            for no in (2, 3):
                self.seen.clear()
                page.locator("#master-view tr", has=page.locator(f"td.key:text-is('{no}')")) \
                    .locator("button:text-is('削除')").click()
                self.wait_seen(page, "消すと、その注文の行き場が無くなります")
                self.assertTrue(self.shared_row("LVC", no), f"{no} 番が消えた")

        with self.subTest("消す: 動いていない看板は消え、盤からも消える"):
            self.dialogs.clear()
            page.locator("#master-view tr", has=page.locator("td.key:text-is('6')")) \
                .locator("button:text-is('削除')").click()
            page.wait_for_function("document.querySelectorAll('#master-view table tr').length === 6")
            self.assertNotIn("いま「", self.dialogs[0])
            self.assertEqual(self.shared_row("LVC", 6), {})
            self.assertIn("看板画面からも消えます", self.text(page, "#master-written"))

        with self.subTest("盤: 足した看板・直したサイズ・消した看板が追いついている"):
            self.go(page, "/board")
            self.assertNotIn("50mm : 12巻", self.board_sizes(page))
            self.assertEqual(self.row(page, 6).count(), 0)
            self.assertEqual(self.state(page, 1, "size"), "ordered")
        self.assert_quiet()

    def test_足した看板がすぐ盤に出る(self) -> None:
        page = self.open_settings()
        self.open_master(page)
        page.click("#master-add")
        page.wait_for_selector("#master-form form")
        page.fill("#new-資材", "テープ")
        page.fill("#new-サイズ", "50mm : 10巻")
        page.click("#master-form button[type=submit]")
        self.answer_password(page)
        page.wait_for_selector("#master-written:not([hidden])")
        self.go(page, "/board")
        self.assertIn("50mm : 10巻", self.board_sizes(page))
        self.assertEqual(page.locator("#board .frame__title").last.inner_text(), "テープ")
        self.assert_quiet()

    def test_Accessの最新で中身を入れ替えても_提出の状態は残る(self) -> None:
        src = self.dir / "最新.sqlite3"
        conn = sqlite3.connect(str(src))
        conn.execute(f"CREATE TABLE [看板_LVC] ({COLS})")
        rows = [(no, material, size + ("(新)" if no == 2 else ""), permanent) for no, material, size, permanent in CARDS]
        rows.append((7, "テープ", "50mm : 10巻", "〇"))
        conn.executemany("INSERT INTO [看板_LVC] VALUES (?,?,?,'','〇','','','',?,'','')", rows)
        conn.commit()
        conn.close()

        page = self.open_board()
        self.cell(page, 1, "size").click()
        self.wait_state(page, 1, "size", "ordered")
        self.go(page, "/settings")
        page.click("#tab-master")
        page.set_input_files("#rf-file", str(src))
        self.answer_password(page)
        box = page.locator('#rf-table input[data-table="看板_LVC"]')
        box.wait_for()
        box.check()
        page.wait_for_function("!document.getElementById('rf-run').disabled")
        page.click("#rf-run")
        page.wait_for_selector("#rf-result:not([hidden])")
        self.assertIn("✔", self.text(page, "#rf-result"), self.text(page, "#rf-result"))
        self.assertTrue(any("入れ替えます" in d for d in self.dialogs), self.dialogs)
        self.assertEqual(self.shared_row("LVC", 1)["欲"], "〇", "提出の状態が消えた")
        self.assertEqual(self.shared_row("LVC", 7)["資材"], "テープ")

        self.go(page, "/board")
        self.assertEqual(self.state(page, 1, "size"), "ordered")
        self.assertIn("(新)", self.cell(page, 2, "size").inner_text())
        self.assertEqual(self.row(page, 7).count(), 1, "入れ替えで足した看板が盤に出ない")
        self.assert_quiet()


# ===========================================================================
# 8. 記録・なぜなぜ分析
# ===========================================================================
class TraceFlowTest(FlowCase):
    MODE = config.MODE_SITE
    LINE = "LVC"

    def test_断った操作を記録で引き_なぜなぜ分析シートを保存する(self) -> None:
        from kanban import trace

        logs = Path(self.cfg().resolved_log_dir())
        self.assertTrue(str(logs).startswith(str(self.home)), "試験のフォルダの外に書いている")
        # VER3.5.9 までの置き場所(<置き場所>/KanbanSystem/記録/<年月>/<PC名>_<日付>.jsonl)の記録
        old_ref = trace.write("refused", "前の版で断った操作", level=trace.LEVEL_WARNING)
        new_file = next((logs / "kanban" / "記録").rglob("*.jsonl"))
        legacy = trace.legacy_root_for(logs) / new_file.parent.name
        legacy.mkdir(parents=True)
        lines = [x for x in new_file.read_text(encoding="utf-8").splitlines() if old_ref in x]
        (legacy / new_file.name.replace("kanban_記録_", "")).write_text(lines[0] + "\n", encoding="utf-8")
        new_file.write_text("".join(x + "\n" for x in new_file.read_text(encoding="utf-8").splitlines()
                                    if old_ref not in x), encoding="utf-8")

        page = self.open_settings()
        with self.subTest("違うパスワードで担当ラインを変えようとして断られる"):
            page.click("#tab-device")
            page.select_option("#line", "LS")
            self.answer_password(page, "ちがう")
            self.wait_seen(page, "パスワード")
            self.assertEqual(self.cfg().line, "LVC")

        with self.subTest("記録: 開くと探し、置き場所は <置き場所>/kanban/記録"):
            page.click("#tab-trace")
            page.wait_for_function("document.getElementById('trace-summary').textContent.includes('件')")
            self.assertIn(str(logs / "kanban" / "記録"), self.text(page, "#trace-summary"))
            row = page.locator("#trace-rows tr", has_text="/api/line")
            row.first.wait_for()
            ref = row.first.locator("td").last.inner_text()
            self.assertTrue(ref, "記録番号が無い")

        with self.subTest("行を押すと、何が・どこで・画面に出した文言と、なぜなぜの下書き"):
            row.first.click()
            page.wait_for_function("(r) => document.getElementById('trace-sheet').value.includes(r)", arg=ref)
            self.assertIn(ref, self.text(page, "#trace-detail-title"))
            facts = self.text(page, "#trace-facts")
            self.assertIn("/api/line", facts)
            self.assertIn("パスワード", facts)
            self.assertNotIn("ちがう", page.input_value("#trace-sheet") + facts, "打ったパスワードが記録に残る")
            self.assertIn("なぜなぜ分析シート", page.input_value("#trace-sheet"))

        with self.subTest("前の版の置き場所の記録も、記録番号で引ける"):
            page.fill("#trace-ref", old_ref)
            page.press("#trace-ref", "Enter")
            page.wait_for_function("(r) => document.getElementById('trace-sheet').value.includes(r)", arg=old_ref)
            self.assertIn("前の版で断った操作", self.text(page, "#trace-facts"))

        with self.subTest("無い記録番号は、無いと言う"):
            self.seen.clear()
            page.fill("#trace-ref", "R-0000-000000-zzzz")
            page.click("#trace-open-ref")
            self.wait_until(page, lambda: self.seen, "知らせが出た状態")
            self.assertTrue(any("見つかりません" in t or "ありません" in t for t in self.seen), self.seen)

        with self.subTest("テキストで保存・コピー"):
            with page.expect_download() as info:
                page.click("#trace-sheet-save")
            download = info.value
            self.assertEqual(download.suggested_filename, f"なぜなぜ_{old_ref}.txt")
            self.assertIn(old_ref, Path(download.path()).read_text(encoding="utf-8-sig"))
            # 試験の宛先(http://kanban.test)はクリップボードを使えない。使えないときは選んだ状態に
            # して Ctrl+C を案内する(本物は 127.0.0.1 なので使える)。どちらでも黙らない
            page.click("#trace-sheet-copy")
            self.wait_seen(page, "コピー")

        with self.subTest("一覧を CSV で"):
            got = page.evaluate("""async () => { const r = await fetch(document.getElementById('trace-csv').href);
                                   return [r.status, r.headers.get('content-type'), await r.text()]; }""")
            self.assertEqual(got[0], 200)
            self.assertIn("text/csv", got[1])
            self.assertIn(ref, got[2])
        self.assert_quiet()


# ===========================================================================
# 9. 設定のその他の面
# ===========================================================================
class OtherSettingsFlowTest(FlowCase):
    MODE = config.MODE_SITE
    LINE = "LVC"

    def test_画面の色は選んだまま_ほかの画面でも開き直しても残る(self) -> None:
        page = self.open_board()
        toggle = page.locator("#theme-toggle")
        self.assertEqual(page.get_attribute("html", "data-theme"), "light")
        for _ in range(4):
            if "ダーク" in toggle.inner_text():
                break
            toggle.click()
        self.assertIn("ダーク", toggle.inner_text())
        self.assertEqual(page.get_attribute("html", "data-theme"), "dark")
        self.go(page, "/settings")
        self.assertEqual(page.get_attribute("html", "data-theme"), "dark", "設定の画面で戻った")
        self.boot()
        page = self.open_board(page)
        self.assertEqual(page.get_attribute("html", "data-theme"), "dark", "開き直すと戻った")
        self.assertIn("ダーク", page.locator("#theme-toggle").inner_text())
        self.assert_quiet()

    def test_動作の設定は保存して開き直しても残り_自動印刷が効く(self) -> None:
        page = self.open_settings("source")
        with self.subTest("形の違う値は送る前・送ったあとに断り、どの欄か分かる"):
            page.fill("#behavior-import", "")
            self.guarded(page, "#behavior-save", "/api/behavior")
            self.wait_seen(page, "取り込み間隔(秒)")
            self.assertEqual(page.evaluate("document.activeElement.id"), "behavior-import")
            self.assertEqual(self.cfg().import_interval_sec, config.Config().import_interval_sec)

        with self.subTest("保存すると残る"):
            page.fill("#behavior-import", "45")
            page.fill("#behavior-export", "90")
            page.check("#behavior-autoprint")
            self.mark(page)
            self.guarded(page, "#behavior-save", "/api/behavior")
            self.wait_seen(page, "次にアプリを開いたときから効きます")
            self.wait_reloaded(page)
            page.click("#tab-source")
            self.assertEqual((page.input_value("#behavior-import"), page.input_value("#behavior-export")),
                             ("45", "90"))
            self.assertTrue(page.is_checked("#behavior-autoprint"))
            cfg = self.cfg()
            self.assertEqual((cfg.import_interval_sec, cfg.export_interval_sec, cfg.auto_print), (45, 90, True))

        with self.subTest("開き直すと効く: 取り込みの間隔・印刷画面を開くと印刷ダイアログ"):
            self.boot()
            self.assertEqual(self.app.config["IMPORT_INTERVAL_SEC"], 45)
            self.assertEqual(self.importer.interval_sec, 45)
            self.ctx.add_init_script("window.__printed = 0; window.print = () => { window.__printed += 1; };")
            page = self.open_board(page)
            self.cell(page, 1, "size").click()
            self.wait_state(page, 1, "size", "ordered")
            page.wait_for_function("document.querySelector('#reports .count')?.textContent === '1'")
            with self.ctx.expect_page() as info:
                page.click('#reports a[data-title="注文票"]')
            report = self.watch(info.value)
            report.wait_for_function("window.__printed > 0")
            report.close()
        self.assert_quiet()

    def test_押し間違いとみなす時間とCSVの書き出し先(self) -> None:
        page = self.open_settings("stats")
        page.wait_for_function("document.getElementById('stats-since') !== null")
        page.fill("#mistake-minutes", "9")
        self.guarded(page, "#mistake-save", "/api/stats/mistake-minutes")
        self.wait_until(page, lambda: self.cfg().mistake_minutes == 9, "押し間違いの時間が 9 分")
        page.wait_for_function("[...document.querySelectorAll('.mistake-min')].every((e) => e.textContent === '9')")

        out = self.dir / "書き出し"
        out.mkdir()
        page.click("#tab-source")
        page.fill("#csv-dir", str(out))
        page.click("#csv-save")
        self.wait_until(page, lambda: self.cfg().csv_dir == str(out), "CSV の書き出し先が変わった状態")
        page.click("#tab-stats")
        page.wait_for_function("(p) => document.getElementById('csv-dest').textContent === p", arg=str(out))
        page.click('[data-csv-save="summary"]')
        page.wait_for_selector("#csv-result:not([hidden])")
        self.assertIn(str(out), self.text(page, "#csv-result"))
        self.assertTrue(list(out.glob("*.csv")), "書き出し先に CSV が無い")

        with self.subTest("無いフォルダは断り、元のまま"):
            page.click("#tab-source")
            page.fill("#csv-dir", str(self.dir / "無いフォルダ"))
            self.seen.clear()
            page.click("#csv-save")
            self.wait_until(page, lambda: self.seen, "知らせが出た状態")
            self.assertTrue(any("無いフォルダ" in t or "見つかりません" in t for t in self.seen), self.seen)
            self.assertEqual(self.cfg().csv_dir, str(out))
        self.assert_quiet()

    def test_配布設定を書き出し_読み込み直し_消し_配布用フォルダを作る(self) -> None:
        from app.routes import settings as settings_routes

        real = settings_routes._make_dist_module
        built = self.dir / "配布用"

        def fake():
            module = real()
            module.default_out = lambda stamp=False: built       # アプリの隣(リポジトリ)を汚さない
            return module

        patcher = mock.patch.object(settings_routes, "_make_dist_module", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        folder = Path(os.environ["KANBAN_DISTRIBUTION_DIR"])

        page = self.open_settings("dist")
        self.assertEqual(self.text(page, "#dist-state"), "なし")
        self.assertTrue(page.locator("#dist-reapply").is_disabled())
        with self.subTest("書き出す: 担当ラインは既定で入れない"):
            self.assertFalse(page.is_checked('[data-dist-item="line"]'))
            page.click("#dist-export")
            self.answer_password(page)
            page.wait_for_function("document.getElementById('dist-state').textContent === 'あり'")
            self.assertTrue(folder.is_dir())
            text = (folder / "設定.json").read_text(encoding="utf-8-sig")
            body = json.loads(text)
            self.assertNotIn('"line"', text, body)
            self.assertIn(str(self.shared), text, "接続先が入っていない")
            self.assertFalse(page.locator("#dist-checks li.is-ng").count(), self.text(page, "#dist-checks"))
            self.assertFalse(page.locator("#dist-reapply").is_disabled())

        with self.subTest("読み込み直す"):
            self.mark(page)
            page.click("#dist-reapply")
            self.wait_seen(page, "配布設定")
            self.wait_reloaded(page)
            self.assertEqual(self.cfg().shared_db_path, str(self.shared))

        with self.subTest("配布用フォルダを作る"):
            page.click("#tab-dist")
            page.click("#dist-build")
            page.wait_for_selector("#dist-build-result:not([hidden])", timeout=60000)
            self.assertTrue(built.is_dir(), self.text(page, "#dist-build-result"))
            self.assertTrue((built / "配布設定").is_dir() or any(built.rglob("配布設定")),
                            "配布用フォルダに配布設定が入らない")

        with self.subTest("消す"):
            page.click("#tab-dist")
            page.click("#dist-remove")
            page.wait_for_function("document.getElementById('dist-state').textContent === 'なし'")
            self.assertFalse(folder.exists() and any(folder.iterdir()))
            self.assertTrue(page.locator("#dist-remove").is_disabled())
        self.assert_quiet()

    def test_管理者パスワードを変えると_開き直しても新しいほうだけ通る(self) -> None:
        page = self.open_settings("auth")
        page.fill("#admin-pw-current", PASSWORD)
        page.fill("#admin-pw-new", "あたらしい")
        page.fill("#admin-pw-new-confirm", "あたらしい")
        page.click("#password-save")
        page.wait_for_function("document.getElementById('admin-pw-new').value === ''")
        self.boot()                               # 開き直す(認証は切れる)
        page.reload(wait_until="load")
        page.wait_for_function("(document.getElementById('trace-from') || {}).value")
        page.click("#tab-auth")
        self.assertEqual(self.text(page, "#auth-state").strip(), "未認証")
        page.fill("#unlock-pw", PASSWORD)
        page.click("#unlock-save")
        page.wait_for_function("document.getElementById('unlock-pw').value === ''")
        self.assertEqual(self.text(page, "#auth-state").strip(), "未認証", "古いパスワードで開いた")
        page.fill("#unlock-pw", "あたらしい")
        page.click("#unlock-save")
        page.wait_for_function("document.getElementById('auth-state').textContent.trim() === '認証済み'")
        self.assert_quiet()


# ===========================================================================
# 10. 断る場面・思わぬ誤り
# ===========================================================================
class RefusalFlowTest(FlowCase):
    MODE = config.MODE_SITE
    LINE = "LVC"

    def test_置き場所の打ち間違いは理由を言って断り_元のまま(self) -> None:
        page = self.open_settings("source")
        with self.subTest("接続先: 無い場所"):
            page.fill("#accdb", str(self.dir / "無い" / "看板マスタ.sqlite3"))
            self.guarded(page, "#save-accdb", "/api/shared-db-path")
            page.wait_for_function("document.getElementById('source-result').textContent.includes('変えられませんでした')")
            self.assertIn("その場所が見つかりません", self.text(page, "#source-result"))
            self.assertIn("接続先は元のままです", self.text(page, "#source-result"))
            self.assertEqual(self.cfg().shared_db_path, str(self.shared))
        with self.subTest("接続先: 看板マスタでないファイル(梱包資材マスタ)"):
            self.grant("mode:field")                    # 表のある梱包資材マスタにしておく
            page.fill("#accdb", str(self.packing))
            self.guarded(page, "#save-accdb", "/api/shared-db-path")
            page.wait_for_function("document.getElementById('source-result').textContent.includes('変えられませんでした')"
                                   " && !document.getElementById('source-result').textContent.includes('その場所が')")
            self.assertIn("看板", self.text(page, "#source-result"))
            self.assertEqual(self.cfg().shared_db_path, str(self.shared))
        with self.subTest("看板履歴の置き場所: 看板マスタを指すと断る"):
            page.fill("#history-path", str(self.shared))
            self.guarded(page, "#history-save", "/api/history-db-path")
            page.wait_for_selector("#history-path-result:not([hidden])")
            self.assertIn("banner--bad", page.get_attribute("#history-path-result", "class"))
            self.assertEqual(self.cfg().history_db_path, "")
        with self.subTest("記録の置き場所: 無いフォルダ"):
            page.click("#tab-trace")
            page.fill("#log-dir", str(self.dir / "無いフォルダ"))
            self.guarded(page, "#log-save", "/api/log-dir")
            page.wait_for_selector("#log-dir-result:not([hidden])")
            self.assertIn("フォルダが見つかりません", self.text(page, "#log-dir-result"))
            self.assertEqual(self.cfg().log_dir, "")
        self.assert_quiet()

    def test_思わぬ誤りでも記録番号を出して固まらず_記録で引ける(self) -> None:
        from kanban.domain.service import KanbanService

        page = self.open_board()
        with mock.patch.object(KanbanService, "toggle_order", side_effect=RuntimeError("試験で起こした失敗")):
            self.cell(page, 1, "size").click()
            self.wait_until(page, lambda: any("記録番号" in t for t in self.seen), "記録番号の付いた知らせ")
        shown = next(t for t in self.seen if "記録番号" in t)
        ref = re.search(r"記録番号 ([A-Z]-[0-9a-f-]+)", shown).group(1)
        self.assertEqual(self.state(page, 1, "size"), "neutral")
        # 固まっていない: もう一度押せば通る
        self.cell(page, 1, "size").click()
        self.wait_state(page, 1, "size", "ordered")
        # わざと起こした 500 にブラウザが出す 1 行だけは数えない
        self.assertEqual(len([c for c in self.console if "status of 500" in c]), 1, self.console)
        self.console = [c for c in self.console if "status of 500" not in c]

        self.go(page, "/settings")
        page.click("#tab-trace")
        page.fill("#trace-ref", ref)
        page.press("#trace-ref", "Enter")
        page.wait_for_function("(r) => document.getElementById('trace-sheet').value.includes(r)", arg=ref)
        self.assertIn("RuntimeError: 試験で起こした失敗", self.text(page, "#trace-chain"))
        self.assertIn("/api/board/order", self.text(page, "#trace-facts"))
        self.assert_quiet()


    def test_共有DBに表の無いラインを選ぶと_取り込めないわけを言う(self) -> None:
        page = self.open_settings("device")
        self.mark(page)
        self.guarded(page, "#line", "/api/line", how="select", value="AIM")
        self.wait_seen(page, "共有DBに 看板_AIM がありません")
        self.assertFalse(any("AIM の看板を取り込みました" in t for t in self.seen), self.seen)
        self.wait_reloaded(page)
        self.assertIn("看板_AIM がありません", self.text(page, "#mode-notice"))
        self.go(page, "/board")
        text = self.text(page, ".empty")
        self.assertIn("共有DBに 看板_AIM がありません", text)
        self.assertNotIn("担当ラインが設定されていません", text, "決めてあるのに未設定と言う")
        with self.subTest("開き直しても同じ"):
            self.boot()
            page = self.open_board(page)
            self.assertIn("共有DBに 看板_AIM がありません", self.text(page, ".empty"))
        self.assert_quiet()


class NoPermissionFlowTest(FlowCase):
    MODE = config.MODE_SITE

    def test_権限の無いモードを頼まれたら_現場で開いて理由を出す(self) -> None:
        self.store.set_device_mode(config.MODE_WAREHOUSE)
        self.boot()
        page = self.open_board()
        self.assertIn("現場", self.text(page, ".mode-chip"))
        notice = self.text(page, "#startup-notice")
        self.assertIn("倉庫モードの権限がないので、現場モードで開きました", notice)
        self.go(page, "/settings")
        self.assertTrue(self.visible(page, "#startup-notice"), "設定の画面では理由が消える")
        self.assertTrue(page.evaluate(
            "document.querySelector(`#mode option[value='warehouse']`).disabled"), "権限の無いモードを選べる")
        self.assert_quiet()


if __name__ == "__main__":
    unittest.main()
