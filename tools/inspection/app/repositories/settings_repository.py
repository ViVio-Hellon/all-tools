"""ユーザー別設定の保存（%LOCALAPPDATA%\\<AppId>\\settings\\user_settings.json）。

VBA版はブック内の名前定義（FolderPath / DarkMode）に保存していた。
Python版では利用者ごとのローカル領域へ JSON で保存し、変更前の内容は
backup フォルダへ残す。
"""
from __future__ import annotations

import json
import os
import shutil
import threading
from datetime import datetime
from typing import Any, Dict

KEEP_BACKUPS = 10


class SettingsRepository:
    def __init__(self, settings_dir: str, backup_dir: str):
        self.path = os.path.join(settings_dir, "user_settings.json")
        self.backup_dir = backup_dir
        self._lock = threading.Lock()

    def load(self) -> Dict[str, Any]:
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8") as fp:
                    data = json.load(fp)
                return data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                return {}

    def save(self, data: Dict[str, Any]) -> None:
        with self._lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self._backup()
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(data, fp, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)

    def _backup(self) -> None:
        if not os.path.isfile(self.path):
            return
        try:
            os.makedirs(self.backup_dir, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            shutil.copy2(self.path, os.path.join(self.backup_dir, f"user_settings_{stamp}.json"))
            backups = sorted(f for f in os.listdir(self.backup_dir) if f.startswith("user_settings_"))
            for old in backups[:-KEEP_BACKUPS]:
                os.remove(os.path.join(self.backup_dir, old))
        except OSError:
            pass  # バックアップ失敗で保存を止めない
