#!/usr/bin/env python3
"""デスクトップ版の窓を**実際に操作して**確かめる(開発機の Linux 用)

`desktop_smoke.py` は「起動して画面が届く・待ち受けない・止めたら終わる」まで。
ここでは窓の中を WebDriver(tauri-driver)で操作し、外枠(Rust)に頼む機能を
1 つずつ通す。保存・選択の標準ダイアログは xdotool で Enter を押して閉じる。

    1. デスクトップ版として開き、担当ラインがアクセス権限の表から決まる
    2. 帳票が別の窓で開く(盤の窓はそのまま)
    3. 記録の CSV・なぜなぜシート(画面で作った中身)を「名前を付けて保存」で保存する
    4. 「参照...」が標準のフォルダ選択になり、パスワードを訊かない
    5. モードを変えると Python だけを立て直して切り替わる(窓はそのまま)
    6. 「終了」で Python が後片付けして終わり、exe も終わる
    7. ブラウザ版とデスクトップ版は同時に動かない(どちらから開いても)

要るもの(Ubuntu): libwebkit2gtk-4.1-dev・webkit2gtk-driver・xvfb・xdotool、
`cargo install tauri-driver`、`pip install selenium`。

    cd src-tauri && cargo build && cd ..
    xvfb-run -a python scripts/desktop_e2e.py --exe src-tauri/target/debug/KanbanSystem

作業用のフォルダ(共有フォルダの代わり・設定・ログ)は一時フォルダに作る。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from desktop_smoke import make_share  # noqa: E402

LOGIN, PC = "e2e", "E2E-PC"   # 試し用の利用者(本物の ID は使わない)


class Run:
    def __init__(self, exe: str) -> None:
        from kanban import access_control

        self.exe = str(Path(exe).resolve())
        self.ok = True
        self.work = Path(tempfile.mkdtemp(prefix="desktop_e2e_"))
        make_share(self.work / "share")
        db = sqlite3.connect(str(self.work / "share" / "梱包資材マスタ.sqlite3"))
        db.execute(access_control.DDL)
        db.executemany('INSERT INTO "アクセス権限" ("ログインID","PC名","権限") VALUES (?,?,?)',
                       [(LOGIN, PC, "mode:field"), (LOGIN, "", "mode:material"), (LOGIN, PC, "L-1")])
        db.commit()
        db.close()
        settings = self.work / "settings"
        settings.mkdir()
        (settings / "config.json").write_text(json.dumps({
            "shared_db_path": str(self.work / "share" / "看板マスタ.sqlite3"),
            "sqlite_path": str(self.work / "local" / "kanban.sqlite3"),
            "log_dir": str(self.work / "logs"),
            "export_interval_sec": 5, "import_interval_sec": 5}), encoding="utf-8")
        self.env = dict(os.environ, KANBAN_ROOT=str(ROOT), KANBAN_CONFIG=str(settings / "config.json"),
                        KANBAN_SETTINGS_DIR=str(settings), KANBAN_DISTRIBUTION_DIR=str(self.work / "dist"),
                        KANBAN_LOCAL_DIR=str(self.work / "local"), KANBAN_LOGIN_ID=LOGIN,
                        KANBAN_PC_NAME=PC, KANBAN_PYTHON=sys.executable)
        self.export = self.work / "local" / "export"

    # -- 小道具 ---------------------------------------------------------
    def check(self, cond, label: str) -> None:
        print(("[OK] " if cond else "[NG] ") + label, flush=True)
        self.ok &= bool(cond)

    def log(self) -> str:
        return "".join(p.read_text(encoding="utf-8", errors="replace")
                       for p in sorted((self.work / "logs").rglob("DebugLog_*.txt")))

    @staticmethod
    def wait(cond, timeout: float = 60) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cond():
                return True
            time.sleep(0.3)
        return False

    @staticmethod
    def press_enter_in_dialog(timeout: float = 20) -> bool:
        """標準のダイアログ(保存・選択)が出たら Enter で閉じる(既定の名前・場所のまま)。"""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            found = subprocess.run(["xdotool", "search", "--onlyvisible", "--name", "名前を付けて保存|を選ぶ"],
                                   capture_output=True, text=True).stdout.split()
            if found:
                time.sleep(0.8)
                # ウィンドウマネージャが無いので windowactivate は使えない
                subprocess.run(["xdotool", "windowfocus", "--sync", found[-1]], capture_output=True)
                time.sleep(0.3)
                subprocess.run(["xdotool", "key", "Return"], capture_output=True)
                return True
            time.sleep(0.3)
        return False

    def new_files(self, before: set, pattern: str) -> list[Path]:
        return sorted(set(self.export.rglob(pattern)) - before) if self.export.exists() else []

    # -- 窓の中 ---------------------------------------------------------
    def window(self) -> None:
        from selenium import webdriver
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.options import ArgOptions
        from selenium.webdriver.support.ui import WebDriverWait

        driver = subprocess.Popen(["tauri-driver"], env=self.env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.5)
        opts = ArgOptions()
        opts.set_capability("tauri:options", {"application": self.exe})
        opts.set_capability("browserName", "wry")
        # 確かめのダイアログ(confirm)は「はい」で答える
        opts.set_capability("unhandledPromptBehavior", "accept")
        d = webdriver.Remote(command_executor="http://127.0.0.1:4444", options=opts)

        def mode():
            try:
                return d.execute_script("return window.APP && window.APP.mode")
            except Exception:  # noqa: BLE001 - 立て直しの途中は読めない
                return None

        try:
            WebDriverWait(d, 60).until(lambda d: "/board" in d.current_url
                                       and d.execute_script("return !!window.APP"))
            self.check(d.execute_script("return window.APP.desktop === true && !!window.__TAURI__"),
                       "デスクトップ版として開き、外枠の機能が見える")
            self.check(mode() == "site", "現場モードで開いた")
            self.check(d.execute_script("return window.APP.line") == "L1", "担当ラインはアクセス権限の表から L1")

            # 帳票: 別の窓
            WebDriverWait(d, 20).until(lambda d: d.find_elements(By.CSS_SELECTOR, "#reports a"))
            before = len(d.window_handles)
            d.find_elements(By.CSS_SELECTOR, "#reports a")[0].click()
            self.check(self.wait(lambda: "画面 GET /report/site" in self.log(), 15), "帳票を開いた(Python が帳票を返した)")
            self.check(len(d.window_handles) == before + 1, f"帳票は別の窓(窓 {before} → {len(d.window_handles)})")
            self.check(d.execute_script("return location.pathname") == "/board", "盤の窓はそのまま")

            # 設定
            d.get(d.current_url.split("/board")[0] + "/settings")
            WebDriverWait(d, 20).until(lambda d: d.find_elements(By.ID, "mode"))
            time.sleep(1.5)

            # 保存: 記録の CSV(アプリの中の経路)
            before = set(self.export.rglob("*")) if self.export.exists() else set()
            d.find_element(By.ID, "tab-trace").click()
            WebDriverWait(d, 20).until(lambda d: "/api/trace/csv" in (
                d.find_element(By.ID, "trace-csv").get_attribute("href") or ""))
            d.find_element(By.ID, "trace-csv").click()
            self.check(self.press_enter_in_dialog(), "記録の CSV: 保存ダイアログが開いた")
            saved: list[Path] = []
            self.wait(lambda: bool(saved.extend(self.new_files(before, "*.csv")) or saved), 15)
            self.check(saved and saved[0].stat().st_size > 0, f"記録の CSV を保存した: {[p.name for p in saved]}")
            self.check(d.execute_script("return location.pathname") == "/settings", "ダウンロードで画面が移らない")
            time.sleep(1)
            toast = d.execute_script("return [...document.querySelectorAll('.toast')].map(t => t.textContent).join('|')")
            self.check("保存しました" in toast, f"保存した場所を知らせる: {toast[:80]}")

            # 保存: 画面で作った中身(なぜなぜシートと同じ作り)
            d.execute_script("""
              const a = document.createElement('a');
              a.href = URL.createObjectURL(new Blob(['なぜなぜ'], {type: 'text/plain'}));
              a.download = 'なぜなぜ_R-0101-000000-abcd.txt';
              document.body.appendChild(a); a.click(); a.remove();""")
            self.check(self.press_enter_in_dialog(), "画面で作った中身: 保存ダイアログが開いた")
            sheet = self.export / "なぜなぜ_R-0101-000000-abcd.txt"
            self.wait(sheet.exists, 15)
            self.check(sheet.exists() and sheet.read_text(encoding="utf-8") == "なぜなぜ", "なぜなぜシートを保存した")

            # 参照: 標準のフォルダ選択
            d.find_element(By.ID, "tab-source").click()
            d.execute_script("document.getElementById('csv-dir').value = arguments[0]", str(self.export))
            d.execute_script("document.getElementById('csv-browse').click()")
            self.check(self.press_enter_in_dialog(), "参照: 標準のフォルダ選択が開いた(パスワードは訊かない)")
            time.sleep(1.5)
            value = d.execute_script("return document.getElementById('csv-dir').value")
            self.check(d.execute_script("return document.getElementById('fs').hidden")
                       and value.startswith(str(self.work)), f"選んだフォルダが入った(一覧は開かない): {value}")

            # モード: その場で切り替える
            d.find_element(By.ID, "tab-device").click()
            label = d.execute_script("return [...document.querySelectorAll('#mode option')].map(o => o.textContent.trim()).join(' / ')")
            self.check("ポート" not in label and "権限なし" not in label, f"モードの選択肢: {label}")
            d.execute_script("const s = document.getElementById('mode'); s.value = 'warehouse';"
                             " s.dispatchEvent(new Event('change'));")
            t0 = time.monotonic()
            WebDriverWait(d, 90).until(lambda d: mode() == "warehouse")
            self.check(True, f"倉庫モードに切り替わった({time.monotonic() - t0:.1f}秒・窓はそのまま)")
            text = self.log()
            self.check(text.count("外枠からの要求を受け付けます") >= 2, "Python を立て直した(2 代目が受け付けた)")
            self.check("外枠が閉じました" in text and "終了処理に入ります" in text,
                       "前の Python は後片付け(最後の書き戻し)をして終わった")
            self.check("止まりました" not in d.page_source, "落ちたと取り違えていない")

            # 終了: × と同じ /api/shutdown を通る(仮想画面にはウィンドウマネージャが無く × を押せない)
            exe_pids = [p for p in subprocess.run(["pgrep", "-f", self.exe], capture_output=True,
                                                  text=True).stdout.split()
                        if Path(f"/proc/{p}/cmdline").read_bytes().startswith(self.exe.encode())]
            d.find_element(By.ID, "quit").click()
            self.wait(lambda: "終了しました: mode=warehouse" in self.log(), 40)
            tail = self.log().split("要求を受け付けます(ポートは使いません): mode=warehouse")[-1]
            self.check("停止要求を受け付けました" in tail, "「終了」で /api/shutdown を通った(未反映があれば訊く道)")
            self.check("終了しました: mode=warehouse(デスクトップ版)" in tail, "Python は後片付けして終わった")
            self.wait(lambda: not any(Path(f"/proc/{p}").exists() for p in exe_pids), 10)
            self.check(exe_pids and not any(Path(f"/proc/{p}").exists() for p in exe_pids), "exe も終わった")
        finally:
            try:
                d.quit()
            except Exception:  # noqa: BLE001 - 窓はもう閉じている
                pass
            driver.terminate()

    # -- ブラウザ版との取り合い -----------------------------------------
    def exclusive(self) -> None:
        start = [sys.executable, str(ROOT / "start_app.py"), "--no-browser"]
        before = self.log().count("要求を受け付けます(ポートは使いません)")
        started = lambda n: self.log().count("要求を受け付けます(ポートは使いません)") >= n  # noqa: E731

        desk = subprocess.Popen([self.exe], env=self.env, stderr=subprocess.DEVNULL)
        self.check(self.wait(lambda: started(before + 1)), "デスクトップ版が起動した")
        out = subprocess.run(start, env=self.env, capture_output=True, text=True, timeout=60)
        self.check(out.returncode == 0 and "デスクトップ版" in out.stdout,
                   f"ブラウザ版は開かずに案内する: {out.stdout.strip()[:60]}")
        second = subprocess.run([self.exe], env=self.env, capture_output=True, timeout=30)
        self.check(second.returncode == 0 and desk.poll() is None, "2 つ目の exe はすぐ終わり、1 つ目は動いたまま")
        desk.terminate()
        desk.wait(timeout=30)
        self.wait(lambda: self.log().count("(デスクトップ版)\n") >= before + 1, 30)

        browser = subprocess.Popen(start, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def ready() -> bool:
            try:
                with urllib.request.urlopen("http://127.0.0.1:8751/api/health", timeout=1) as r:
                    return bool(json.loads(r.read())["ready"])
            except Exception:  # noqa: BLE001
                return False
        self.check(self.wait(ready), "ブラウザ版が起動した(倉庫モード・ポート 8751)")
        desk = subprocess.Popen([self.exe], env=self.env, stderr=subprocess.DEVNULL)
        self.check(self.wait(lambda: browser.poll() is not None), "ブラウザ版は終わった")
        self.check(self.wait(lambda: started(before + 2)), "デスクトップ版が開いた")
        self.check("ブラウザ版を終わらせました" in self.log(), "ログに残る: ブラウザ版を終わらせました")
        desk.terminate()
        desk.wait(timeout=30)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", required=True)
    args = parser.parse_args()
    run = Run(args.exe)
    print(f"作業: {run.work}", flush=True)
    run.window()
    run.exclusive()
    print("結果:", "OK" if run.ok else "NG")
    return 0 if run.ok else 1


if __name__ == "__main__":
    sys.exit(main())
