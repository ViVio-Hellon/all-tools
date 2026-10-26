"""日報の一連動作 ── **入力画面以外の画面の流れ**を本物のブラウザで通す (1) 設定・管理者の側

    日報周りの一連動作チェックお願いします

入力画面(1台で1直を打ち切るまで)と、直の切り替わり・自動確定・複数台は別の試験です。
ここは設定・管理者の面を、据え付けの最初から順に押して通します:

    1. 初めての起動: 設定がまっさらで開く → 「ラインを決める」の案内 → 設定・管理者
       → 参照設定の面で鍵を開ける(**Enter でも開く**・**開けても面が戻されない**)
       → 参照パス → この端末のライン → 管理者パスワード → 日報入力が打てる状態へ
    2. 配布設定: 設定画面から書き出す(中身と置き場所)/ 配った先は起動で読み、
       **配り直すと新しい値**・この端末で直した値は残る・「読み込み直す」で揃う /
       大設定が**まとめたファイル**の日報の分も読み、中身を画面に出す
    6. 取り込み: 過去日報(xlsx)を道で渡す → **まず下見**(入れない)→ 入れる → 記録に出る
       → 3直へ戻す・中身の無いページを片付けるの下見
    7. ログ: 断りが一覧に出る → なぜなぜを残す → CSV → 出力パスを変えると `…/nippou` へ書く
    8. アクセス権限: 表を読み直すと**その場で**ラインと左のタブが変わる

見つけて直したもの(直す前に戻すと落ちる):

    - 鍵の帯で合言葉を打って Enter を押しても何も起きなかった(ボタンしか効かない)
    - 名指し(`?tab=terminal`。日報入力の「ラインを決める」から来るとこれ)で開いた設定画面で、
      別の面(参照設定)へ移って鍵を開けると、**開けた途端に名指しの面へ戻された**
      (描き直しがいまのアドレスを取り直し、名指しが覚えた面より勝つため。`tabs.js`)
    - 取り込みで道を打って「取り込む」を押すと、「入る前に下見の結果が出ます」と書いてあるのに
      **下見を出さずに入れていた**(置き換わるページを見ないまま、いまある日報が入れ替わる)
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._flow_browser import FlowCase  # noqa: E402


# ======================================================================
# 1. 初めての起動
# ======================================================================
class FirstStartTests(FlowCase):
    """配った先の1台目。この端末の設定(`user_config.json`)がまだ無い。"""

    terminal_line = None
    share_in_tmp = False                      # 共有の置き場所も「まだ決めていない」まま

    def test_まっさらで開くと案内からラインを決め_参照パスと合言葉を決めて日報入力へ(self) -> None:
        from nippou import admin_password, config, user_settings

        self.assertEqual(user_settings.load_all(), {}, "まっさらで始まっていません")
        page = self.open_screen("/", "#grid-body")

        # -- 案内: ラインが決まるまで打てない。行き先のボタンがある -------------------
        self.assertTrue(page.evaluate("document.body.classList.contains('needs-line')"))
        self.assertTrue(page.is_visible("#terminal-line-note"))
        self.assertIn("まだ決まっていません", page.inner_text("#terminal-line-text"))
        self.assertTrue(page.is_disabled("#hd-worker"), "ラインが決まる前に作業者を打てます")
        page.click("#start-pick-line")
        page.wait_for_function("location.pathname === '/settings'")
        page.wait_for_selector("#panel-terminal:not([hidden])")
        self.settle(page)
        self.assertEqual(self.tab(page), "terminal")
        # 面の見出し(参照設定・この端末と配布・保存と出力・取り込みと直し・ログ …)
        heads = page.locator("#settingsTabs > .tabs__bar > .tab .tab__label").all_text_contents()
        for want in ("保存と出力", "マスタ", "音", "取り込みと直し", "参照設定", "この端末と配布", "ログ"):
            self.assertIn(want, heads)
        # 鍵が閉まっているあいだ、ラインのボタンは押せない(押せない理由が付いている)
        self.assertTrue(page.is_disabled("#line-choices [data-line='L-1']"))

        # -- 参照設定の面へ移り、合言葉を打って Enter で鍵を開ける -------------------
        page.click("#tab-paths")
        page.wait_for_selector("#panel-paths:not([hidden])")
        self.assertTrue(page.evaluate("document.getElementById('path-gw_reference_dir').readOnly"),
                        "鍵が閉まっているのに守られた欄を打てます")
        page.fill("#lock-pw-paths", "違う")
        page.press("#lock-pw-paths", "Enter")
        page.wait_for_selector(".toast:has-text('パスワードが違います')")
        page.fill("#lock-pw-paths", "nisk")
        page.press("#lock-pw-paths", "Enter")
        page.wait_for_selector("[data-lock='paths'].lockbar--open", state="attached")
        self.settle(page)
        self.assertEqual(self.tab(page), "paths",
                         "鍵を開けたら、名指しされていた「この端末と配布」の面へ戻されました")
        self.assertFalse(page.evaluate("document.getElementById('path-gw_reference_dir').readOnly"))

        ref = self.tmp / "共有" / "参照"
        ref.mkdir(parents=True)
        row = self.save_path(page, "gw_reference_dir", ref)
        self.assertIn("このフォルダはあります", row)
        self.assertSamePath(user_settings.get(config.KEY_REFERENCE_DIR), ref)

        # -- この端末のライン ---------------------------------------------------
        page.click("#tab-terminal")
        page.wait_for_selector("#panel-terminal:not([hidden])")
        page.click("#line-choices [data-line='L-1']")
        page.wait_for_function(
            "document.querySelector('#line-setting .line-now')?.textContent.trim() === 'L-1'")
        self.assertTrue(any("この端末のラインを L-1 にします" in d for d in self.dialogs), self.dialogs)
        self.assertEqual(self.tab(page), "terminal")
        self.assertEqual(user_settings.get(config.KEY_TERMINAL_LINE), "L-1")

        # -- 管理者パスワードを変える(短すぎるものは断る) -------------------------
        page.locator("#panel-terminal summary", has_text="管理者パスワードを変える").click()
        for field, value in (("pw-current", "nisk"), ("pw-new", "ab"), ("pw-confirm", "ab")):
            page.fill(f"#{field}", value)
        page.click("#pw-change")
        page.wait_for_selector("#pw-note:not([hidden])")
        self.assertIn("文字以上", page.inner_text("#pw-note"))
        for field, value in (("pw-current", "nisk"), ("pw-new", "genba01"), ("pw-confirm", "genba01")):
            page.fill(f"#{field}", value)
        page.click("#pw-change")
        page.wait_for_function("document.getElementById('pw-note')?.textContent.includes('変えました')")
        self.assertTrue(admin_password.verify("genba01"))
        self.assertFalse(admin_password.verify("nisk"))

        # -- 日報入力へ: ラインの関門は消え、作業者を選ぶところから --------------------
        self.go_rail(page, "/", "#grid-body")
        page.wait_for_function("!document.body.classList.contains('needs-line')")
        self.assertTrue(page.is_hidden("#terminal-line-note"))
        self.assertTrue(page.evaluate("document.body.classList.contains('needs-worker')"))
        self.assertFalse(page.is_disabled("#hd-worker"))
        self.assertIn("L-1", page.inner_text("header"))
        self.assert_clean(page)


# ======================================================================
# 2. 配布設定
# ======================================================================
class DistributionFlowTests(FlowCase):
    def dist_settings(self) -> dict:
        from nippou import distribution
        return json.loads(distribution.settings_path().read_text(encoding="utf-8"))["settings"]

    def put_own(self, settings: dict, created_at: str) -> None:
        """配る元が書き出した配布設定(日報のフォルダの `配布設定\\設定.json`)を置く。"""
        from nippou import distribution
        distribution.folder().mkdir(parents=True, exist_ok=True)
        distribution.settings_path().write_text(json.dumps(
            {"format": 1, "created_at": created_at, "created_on": "配る元", "settings": settings},
            ensure_ascii=False), encoding="utf-8")

    def path_value(self, page, key: str) -> Path:
        return Path(page.input_value(f"#path-{key}"))

    def test_設定画面から書き出すと選んだ項目と値が入り_置いてある中身が画面に出る(self) -> None:
        from nippou import config, user_settings

        ref = self.tmp / "共有" / "参照"
        ref.mkdir(parents=True)
        user_settings.save(config.KEY_REFERENCE_DIR, str(ref))
        page = self.open_screen("/settings?tab=terminal", "#dist-export")
        self.assertEqual(page.inner_text("#dist-exists").strip(), "なし")
        # 鍵が閉まっていれば断る(理由をその場で言う)
        page.click("#dist-export")
        page.wait_for_selector("#dist-why:not([hidden])")
        self.assertIn("管理者パスワードが要ります", page.inner_text("#dist-why"))
        # 合言葉を添えれば書ける。ラインは既定で入れない
        self.assertFalse(page.is_checked("[data-dist-item='terminal_line']"))
        page.fill("#dist-password", "nisk")
        page.click("#dist-export")
        page.wait_for_function("document.getElementById('dist-exists')?.textContent.trim() === 'あり'")
        self.assertEqual(page.input_value("#dist-password"), "", "合言葉が画面に残っています")
        rows = page.eval_on_selector_all("#dist-rows tr", "(rs) => rs.map((r) => [...r.cells].map((c) => c.textContent.trim()))")
        got = {label: value for label, value in rows}
        self.assertSamePath(got["参照用マスタの参照パス"], ref)
        written = self.dist_settings()
        self.assertSamePath(written[config.KEY_REFERENCE_DIR], ref)
        self.assertNotIn(config.KEY_TERMINAL_LINE, written, "ラインを選んでいないのに入りました")
        # このまま配ると困ること(共有の日報管理のパスが決まっていない …)は、このテストでは決めてある
        self.assertNotIn("「共有の日報管理のパス」が決まっていません", page.inner_text("#dist-export-card"))
        self.assert_clean(page)

    def test_配った先は起動で読み_配り直すと新しい値_直した値は残り_読み込み直しで揃う(self) -> None:
        from nippou import config, distribution, user_settings

        first, second = self.tmp / "参照1", self.tmp / "参照2"
        share1, mine, share2 = self.tmp / "日報1", self.tmp / "この端末の日報", self.tmp / "日報2"
        for folder in (first, second, share1, mine, share2):
            folder.mkdir()
        user_settings.save(config.KEY_ACCESS_DIR, "")         # 配った先はまだ決めていない
        self.put_own({config.KEY_REFERENCE_DIR: str(first), config.KEY_ACCESS_DIR: str(share1)},
                     "2026-10-25 09:00")
        distribution.apply_on_start()                       # 起動のとき
        self.admin()
        page = self.open_screen("/settings?tab=paths", "#path-gw_reference_dir")
        self.assertSamePath(self.path_value(page, "gw_reference_dir"), first)
        self.assertSamePath(self.path_value(page, "access_dir"), share1)
        # この端末で共有の日報管理のパスだけ直す
        self.save_path(page, "access_dir", mine)

        # 配る元が新しい値で配り直した → 次の起動
        self.put_own({config.KEY_REFERENCE_DIR: str(second), config.KEY_ACCESS_DIR: str(share2)},
                     "2026-10-26 09:00")
        distribution.apply_on_start()
        page = self.open_screen("/settings?tab=paths", "#path-gw_reference_dir")
        self.assertSamePath(self.path_value(page, "gw_reference_dir"), second,
                         "配り直したのに、配布設定から読んだ値が前のままです")
        self.assertSamePath(self.path_value(page, "access_dir"), mine, "この端末で直した値が上書きされました")

        # 「既存が優先」と言い、読み込み直すと配布設定の値で揃う
        page.click("#tab-terminal")
        page.wait_for_selector("#panel-terminal:not([hidden])")
        state = page.inner_text("#dist-state")
        self.assertIn("既存が優先", state)
        self.assertIn("共有の日報管理のパス", state)
        page.click("#dist-reapply")
        self.wait_until(page, lambda: Path(user_settings.get(config.KEY_ACCESS_DIR) or ".").resolve() == share2.resolve(),
                        "読み込み直したあとの共有の日報管理のパス")
        self.assertTrue(any("上書きします" in d for d in self.dialogs), self.dialogs)
        page.click("#tab-paths")
        page.wait_for_function(
            f"document.getElementById('path-access_dir')?.value.endsWith({share2.name!r})")
        self.assertSamePath(self.path_value(page, "access_dir"), share2)
        self.assert_clean(page)

    def test_大設定がまとめたファイルの日報の分も読み_中身を画面に出す(self) -> None:
        from nippou import config, distribution, user_settings

        whole_dir = self.tmp / "一式" / "配布設定"
        whole_dir.mkdir(parents=True)
        saved = os.environ.get("ALLTOOLS_DISTRIBUTION_DIR")
        self.addCleanup(lambda: os.environ.__setitem__("ALLTOOLS_DISTRIBUTION_DIR", saved)
                        if saved is not None else os.environ.pop("ALLTOOLS_DISTRIBUTION_DIR", None))
        os.environ["ALLTOOLS_DISTRIBUTION_DIR"] = str(whole_dir)
        ref = self.tmp / "まとめた参照"
        ref.mkdir()
        (whole_dir / distribution.INTEGRATED_FILE).write_text(json.dumps({
            "format": 1, "created_at": "2026/10/26 09:00:00", "settings": {},
            "tools": {"nippou": {"format": 1, "created_at": "2026/10/26 09:00:00",
                                 "created_on": "配る元", "settings": {config.KEY_REFERENCE_DIR: str(ref)}}}},
            ensure_ascii=False), encoding="utf-8")
        distribution.apply_on_start()
        self.assertSamePath(user_settings.get(config.KEY_REFERENCE_DIR), ref)

        page = self.open_screen("/settings?tab=terminal", "#dist-state")
        self.assertEqual(page.inner_text("#dist-exists").strip(), "あり")
        self.assertIn("まとめたファイル", page.inner_text("#dist-meta"))
        rows = page.eval_on_selector_all("#dist-rows tr", "(rs) => rs.map((r) => [...r.cells].map((c) => c.textContent.trim()))")
        self.assertSamePath(dict(rows)["参照用マスタの参照パス"], ref)
        self.assert_clean(page)


# ======================================================================
# 6. 取り込み
# ======================================================================
class ImportFlowTests(FlowCase):
    def test_過去日報を道で渡すとまず下見_もう一度押して入り_記録に出る(self) -> None:
        from tests.test_nippou_sheet import DAY, sheet_cells, write_xlsx

        book = write_xlsx(self.tmp / "2026.08.26.機側日報.xlsx", sheet_cells())
        self.settle_review()
        page = self.open_screen("/settings?tab=data", "#import-path")
        self.assertTrue(page.is_disabled("#import-path"), "鍵が閉まっているのに道を打てます")
        self.unlock_bar(page, "data")
        self.settle(page)
        page.fill("#import-path", str(book))
        self.assertIn("まず中身(下見)を出します", page.inner_text("#import-go-why"))

        # 1回目: 下見だけ。**まだ入れない**
        page.click("#import-go")
        page.wait_for_selector("#import-plan table")
        plan = page.inner_text("#import-plan")
        self.assertIn("機側 に入れます", plan)
        self.assertIn("このファイル(Excel・CSV)の中身", plan, "Excel を渡したのに「このCSV」と言います")
        self.assertEqual(self.posts("/api/settings/import/apply"), [], "下見を出さずに入れました")
        self.assertEqual(self.repo().saved_pages(DAY, "機側", "1直"), [])
        self.assertIn("押すと入ります", page.inner_text("#import-go-why"))

        # 2回目: 入れる
        page.click("#import-go")
        page.wait_for_selector(".toast:has-text('取り込みました')")
        self.assertEqual(self.repo().saved_pages(DAY, "機側", "1直"), [1])
        self.assertEqual(len(self.posts("/api/settings/import/apply")), 1)

        # 3直へ戻す・中身の無いページ: 下見が答える
        self.settle(page)
        page.click("#reshift-preview")
        page.wait_for_selector("#reshift-note:not([hidden])")
        self.assertIn("戻すものはありません", page.inner_text("#reshift-note"))
        page.click("#empty-pages-find")
        page.wait_for_function("document.getElementById('empty-pages-head').textContent.includes('ありません')")

        # 記録を見る: 管理者はラインを選んで、入れた直を見る
        self.go_rail(page, "/records", "#records-table")
        page.select_option("#saved-line", "機側")
        page.click("#saved-reload")
        page.wait_for_function("document.querySelector('#records-table tbody').textContent.includes('2026年8月26日')")
        lines = page.eval_on_selector_all("#records-table tbody tr", "(rs) => rs.map((r) => r.cells[1]?.textContent.trim())")
        self.assertEqual(set(lines), {"機側"})
        self.assert_clean(page)

    def test_道を打ち直したら下見からやり直す(self) -> None:
        from tests.test_nippou_sheet import sheet_cells, write_xlsx

        one = write_xlsx(self.tmp / "2026.08.26.機側日報.xlsx", sheet_cells())
        two = write_xlsx(self.tmp / "2026.08.27.機側日報.xlsx", sheet_cells())
        self.admin()
        self.settle_review()
        page = self.open_screen("/settings?tab=data", "#import-path")
        page.fill("#import-path", str(one))
        page.click("#import-preview")
        page.wait_for_selector("#import-plan table")
        page.fill("#import-path", str(two))             # 下見のあとで別のファイルに打ち直した
        self.assertIn("まず中身(下見)を出します", page.inner_text("#import-go-why"))
        mark = len(self.sent)
        page.click("#import-go")
        self.wait_until(page, lambda: any(p == "/api/settings/import/preview" for p, _ in self.sent[mark:]),
                        "打ち直した道の下見")
        self.assertEqual(self.posts("/api/settings/import/apply"), [], "下見していない道を入れました")
        self.assert_clean(page)


# ======================================================================
# 7. ログ
# ======================================================================
class LogFlowTests(FlowCase):
    def test_断りが一覧に出て_なぜなぜを残しCSVに出し_出力パスを変えるとその下のnippouへ書く(self) -> None:
        from nippou.config import SETTINGS
        from nippou.services import event_log

        page = self.open_screen("/settings?tab=terminal", "#panel-terminal")
        page.fill("#lock-pw-terminal", "違う")
        page.click("[data-lock='terminal'] [data-lock-on]")
        page.wait_for_selector(".toast:has-text('パスワードが違います')")
        event_log.flush(5.0)

        page.click("#tab-logs")
        page.wait_for_function("document.getElementById('log-counts').textContent.includes('件を表示')")
        where = Path(page.inner_text("#log-dir").strip())
        self.assertSamePath(where, Path(SETTINGS.default_log_dir) / "nippou")
        page.select_option("#log-scope", "problems")
        row = page.locator("#log-rows tr.log-row", has_text="パスワードが違います")
        row.wait_for()
        self.assertIn("管理者モードにする", row.inner_text())

        # なぜなぜシート: 記録から言えることは入っている。人が書いて残す
        row.click()
        page.wait_for_selector("#log-sheet:not([hidden])")
        self.assertIn("パスワードが違います", page.inner_text("#ls-facts"))
        page.fill("#ls-why-0", "合言葉を打ち間違えた")
        page.fill("#ls-measure", "合言葉を紙に貼らない")
        page.select_option("#ls-status", "done")
        page.fill("#ls-by", "班長")
        page.click("#ls-save")
        page.wait_for_function("document.getElementById('ls-saved').textContent.includes('前に残したもの')")
        page.wait_for_selector("#log-rows tr.log-row:has-text('対策済')")
        notes = list(where.glob("nippou_なぜなぜ_*.jsonl"))
        self.assertEqual(len(notes), 1, list(where.iterdir()))
        self.assertIn("合言葉を打ち間違えた", notes[0].read_text(encoding="utf-8"))

        page.click("#log-csv")
        page.wait_for_selector("#log-msg:not([hidden])")
        self.assertIn("CSVに出しました", page.inner_text("#log-msg"))
        csvs = list((where / event_log.EXPORT_DIR).glob("nippou_*.csv"))
        self.assertEqual(len(csvs), 1)
        self.assertIn("合言葉を打ち間違えた", csvs[0].read_text(encoding="utf-8-sig"))

        # ログの出力パスを変える → 書く先はその下の nippou。前の記録も一覧に残る
        shared = self.tmp / "共有ログ"
        shared.mkdir()
        page.click("#tab-paths")
        self.unlock_bar(page, "paths")
        self.settle(page)
        self.assertEqual(self.tab(page), "paths")
        self.save_path(page, "log_dir", shared)
        page.click("#tab-logs")
        page.wait_for_function("document.getElementById('log-counts').textContent.includes('件を表示')")
        self.assertSamePath(page.inner_text("#log-dir").strip(), shared / "nippou")
        page.select_option("#log-scope", "problems")
        page.locator("#log-rows tr.log-row", has_text="パスワードが違います").wait_for()
        event_log.flush(5.0)
        self.assertTrue(list((shared / "nippou").glob("nippou_出来事_*.jsonl")),
                        "変えた出力パスの nippou フォルダに書いていません")
        self.assertFalse(list(shared.glob("*.jsonl")), "出力パスの直下に書きました(nippou フォルダの外)")
        self.assert_clean(page)


# ======================================================================
# 8. アクセス権限で左のタブとラインが変わる
# ======================================================================
class AccessRightsFlowTests(FlowCase):
    terminal_line = None
    ALL = ["日報入力", "梱包資材重量計算", "VC長さ計算", "記録を見る", "集計・グラフ", "集計管理",
           "標準作業時間", "設定・管理者"]

    def test_表を読み直すとその場でラインと左のタブが変わり_Administratorはラインを選べる(self) -> None:
        from nippou.db.models import DetailRecord, HeaderRecord

        from nippou.config import SETTINGS
        from nippou.services import local_backup

        # 選べるのは控え(LocalBackup)のあるライン。L-1 の1直を控えへ写しておく
        key = dict(report_date="2026年10月1日", line="L-1", shift="1直", page=1)
        repo = self.repo()
        repo.save(HeaderRecord(**key, worker="前の人"), [DetailRecord(**key, row_no=1, lot="N71")])
        Path(SETTINGS.local_backup_base).mkdir(parents=True, exist_ok=True)
        local_backup.flush(repo, force=True)
        self.assertEqual(local_backup.lines_available(), ["L-1"])
        page = self.open_screen("/settings?tab=terminal", "#access-reload")
        self.assertEqual(self.rail(page), self.ALL, "表をまだ読んでいないのに絞られています")

        self.put_access_table("LVC")
        page.click("#access-reload")
        page.wait_for_function(
            "document.querySelector('#line-setting .line-now')?.textContent.trim() === 'LVC'")
        self.assertEqual(self.rail(page), ["日報入力", "梱包資材重量計算", "VC長さ計算",
                                           "集計・グラフ", "設定・管理者"])
        self.assertIn("LVC", page.inner_text("header"))
        self.assertIn("1・2・3・5・8 だけ", page.inner_text("#access-tabs"))

        self.put_access_table("LVC", "Administrator, mode:fullaccess")
        page.click("#access-reload")
        page.wait_for_function("[...document.querySelectorAll('.rail li')].every((li) => !li.hidden)")
        self.assertEqual(self.rail(page), self.ALL)
        # Administrator は記録・集計・グラフでラインを選べる
        self.go_rail(page, "/records", "#saved-line")
        self.assertEqual(page.input_value("#saved-line"), "LVC")
        self.go_rail(page, "/agg", "#agg-line")
        self.go_rail(page, "/graph", "#graph-line")
        page.select_option("#graph-line", "L-1")
        page.wait_for_function("document.getElementById('graph-lead').textContent.includes('L-1')")
        self.assert_clean(page)


# ======================================================================
# 9. 字のまま読めないソースが無い(新しい Python では \H などが誤りになる)
# ======================================================================
class SourceEscapeTests(unittest.TestCase):
    def test_日報のソースに読めないエスケープが無い(self) -> None:
        import warnings

        root = Path(__file__).resolve().parent.parent
        bad = []
        for path in sorted([*root.glob("nippou/**/*.py"), *root.glob("app/**/*.py"), *root.glob("*.py")]):
            with warnings.catch_warnings():
                warnings.simplefilter("error", SyntaxWarning)
                try:
                    compile(path.read_text(encoding="utf-8"), str(path), "exec")
                except SyntaxError as exc:
                    bad.append(f"{path.relative_to(root)}: {exc}")
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
