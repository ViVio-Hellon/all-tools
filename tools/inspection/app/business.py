"""点検表システムの業務をひとまとめに持つ入れ物(プロセスに1つ)。

Flask の外(`start_app` の初期化・終了処理)からも触るので、アプリの
`extensions["inspection"]` に入れておく。
"""
from __future__ import annotations

import os
import shutil
import time
from typing import Any, Dict, List, Optional

from core import app_config, event_log, logging_utils, process_tracking
from core.logging_utils import get_logger


class InspectionBusiness:
    def __init__(self, options: Optional[Dict[str, Any]] = None):
        from app.repositories.preview_cache import PreviewCache
        from app.repositories.settings_repository import SettingsRepository
        from app.services.excel_service import ExcelService
        from app.services.inspection_service import InspectionService
        from app.services.preview_service import PreviewService
        from app.services.print_service import PrintService
        from app.services.settings_service import SettingsService, load_inspection_config
        from app.services.system_info import default_printer

        self.options = dict(options or {})
        self.log = get_logger("app")
        self.cfg = load_inspection_config()
        if self.cfg.load_error:
            self.log.warning("%s — 既定値で動作します", self.cfg.load_error)
        forced_root = None
        if self.options.get("demo"):
            from app.services.demo_data import ensure_demo_folder
            self.cfg.excel.backend = "dummy"
            forced_root = ensure_demo_folder(str(app_config.local_dir("work")))
            self.log.info("模擬モード: 見本フォルダ %s を使用します", forced_root)
        self.settings = SettingsService(
            self.cfg, SettingsRepository(str(app_config.local_dir("data")), str(app_config.local_dir("backup"))),
            get_logger("app.settings"), forced_root_folder=forced_root)
        from app.services.admin_password import AdminPassword
        from app.services.distribution import Distribution
        self.admin = AdminPassword(self.settings, get_logger("app.admin"))
        self.distribution = Distribution(self.settings, self.admin, get_logger("app.distribution"))
        self.inspection = InspectionService(
            self.cfg, self.settings, get_logger("app.inspection"),
            cache_path=os.path.join(str(app_config.local_dir("data")), "inventory_cache.json"))
        self.registry = process_tracking.registry(logger=get_logger("app.excel"))
        self.excel = ExcelService(self.cfg, get_logger("app.excel"), self.registry,
                                  str(app_config.local_dir("work")))
        self.preview_cache = PreviewCache(str(app_config.local_dir("cache")))
        self.preview = PreviewService(self.cfg, self.inspection, self.excel, self.preview_cache,
                                      get_logger("app.preview"))
        self.printing = PrintService(self.inspection, self.excel, default_printer, get_logger("app.print"),
                                     max_copies=self.cfg.max_copies)
        self.default_printer = default_printer

    @property
    def demo(self) -> bool:
        return bool(self.options.get("demo"))

    # ---- 起動・終了 ---------------------------------------------------
    def on_startup(self) -> None:
        cleaned = process_tracking.cleanup_leftovers("起動時の確認(前回の残留)", self.log)
        if cleaned:
            self.log.warning("前回残っていた Excel 関連プロセスを %d 件終了しました", cleaned)
            event_log.record("cleanup", event_log.INFO, leftovers=cleaned,
                             message="前回残っていた Excel 関連プロセスを終了しました(前回は正しく終われていない)")
        removed = self.preview_cache.prune()
        if removed:
            self.log.info("古いプレビュー画像を %d 件削除しました", removed)
        # VER1.1.0 までの受け渡しファイルの残り(いまはファイルを使わない)
        _prune_old_dirs(os.path.join(str(app_config.local_dir("work")), "excel_jobs"), 86400)
        # 配布設定(アプリ直下の `配布設定/`)があれば、**一覧より先に**読む ──
        # 点検表フォルダが入っているので、読む前に探すと既定の場所を見る
        if not self.demo:
            self.distribution.apply_on_start()
        # ログの保存先(配布設定で入ったかもしれないので、そのあと)と古いログの片付け
        self.apply_log_settings()
        removed_logs = logging_utils.prune()
        if removed_logs:
            self.log.info("保存日数を過ぎたログを %d 件削除しました", len(removed_logs))
        # 前回の一覧を先に出し、確認は裏で続ける(起動を待たせない)
        self.inspection.load_cache()
        self.inspection.start_scan("起動時")

    def on_shutdown(self, reason: str) -> None:
        if self.printing.is_busy():
            self.log.warning("終了時に印刷中のため、いまの1件が終わったところで中止します")
            try:
                self.printing.cancel()
            except Exception:  # noqa: BLE001
                pass
            self.printing.wait_idle(60)
        self.excel.shutdown(f"アプリの終了({reason})")
        cleaned = process_tracking.cleanup_leftovers(f"アプリ終了時({reason})", self.log)
        if cleaned:
            self.log.warning("終了時に Excel 関連プロセスを %d 件終了しました", cleaned)
        self.log.info("業務を終了しました(%s)", reason)

    def apply_log_settings(self) -> Dict[str, Any]:
        """設定の「ログの保存先・保存日数」を書き出しに反映する(再起動は要らない)。"""
        return logging_utils.apply_settings(self.settings.get(logging_utils.KEY_DIR),
                                            self.settings.get(logging_utils.KEY_KEEP_DAYS))

    def busy_labels(self) -> List[str]:
        """止めてはいけない処理の名前(`/api/shutdown`・自動終了が見る)。"""
        labels = self.printing.busy_labels()
        if not labels and self.excel.activity == "preview":
            labels = ["プレビューの作成"]
        return labels


def _prune_old_dirs(folder: str, max_age_sec: int) -> None:
    try:
        entries = os.listdir(folder)
    except OSError:
        return
    now = time.time()
    for name in entries:
        path = os.path.join(folder, name)
        try:
            if now - os.path.getmtime(path) > max_age_sec:
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    os.remove(path)
        except OSError:
            continue
