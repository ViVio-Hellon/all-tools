"""ユーザー別設定の保存（%LOCALAPPDATA%\\<AppId>\\settings\\user_settings.json）。

VBA版はブック内の名前定義（FolderPath / DarkMode）に保存していた。
Python版では利用者ごとのローカル領域へ JSON で保存し、変更前の内容は
backup フォルダへ残す。

【読めないのと、無いのは違う】
ファイルが無い(一度も設定していない)なら空。**ファイルはあるのに読めない**(ほかのプロセスが
入れ替えている最中・ロック・権限・中身が壊れている)なら `SettingsUnreadable`。以前はどちらも
空として扱い、次に1項目保存すると**空に足した1項目だけで上書き**していた(点検表フォルダ・
管理者パスワード・ログの保存先が消え、起動時の配布設定の読み込みも「この端末に無い」と見て上書きした)。
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from datetime import datetime
from typing import Any, Dict

from app.errors import ApiError
from core import app_config

KEEP_BACKUPS = 10
#: 読めなかったとき、読み直す回数と間(秒)。Windows では入れ替えの最中に一瞬読めないことがある
READ_TRIES = 3
READ_RETRY_SEC = 0.1


#: 置き換えを断られたとき、やり直す回数と間(秒)。Windows では読んでいる最中のファイルを
#: 置き換えると一瞬 PermissionError になる
REPLACE_TRIES = 5
REPLACE_WAIT_SEC = 0.1


def _replace(tmp: str, target: str) -> None:
    """一時ファイルで置き換える。**Windows で一瞬断られたら、少し待ってやり直す。**"""
    for attempt in range(REPLACE_TRIES):
        try:
            os.replace(tmp, target)
            return
        except PermissionError:
            if attempt + 1 >= REPLACE_TRIES:
                raise
            time.sleep(REPLACE_WAIT_SEC)


class SettingsUnreadable(ApiError):
    """設定ファイルはあるのに読めない(**無いのとは違う**)。読めないあいだは保存しない。"""

    def __init__(self, path: str, cause: Any):
        super().__init__(503, "SETTINGS_UNREADABLE",
                         "この端末の設定ファイルを読めないため、保存しませんでした。"
                         "少し待ってもう一度試してください(続くときはファイルの中身・権限を確かめてください)。",
                         f"{path}: {cause}")
        self.path = path
        self.cause = cause


class SettingsRepository:
    def __init__(self, settings_dir: str, backup_dir: str):
        self.path = os.path.join(settings_dir, "user_settings.json")
        self.backup_dir = backup_dir
        self._lock = threading.Lock()

    def load(self) -> Dict[str, Any]:
        """中身。**無ければ空、あるのに読めなければ `SettingsUnreadable`。**"""
        with self._lock:
            last: Any = None
            for attempt in range(READ_TRIES):
                try:
                    with open(self.path, "r", encoding="utf-8") as fp:
                        text = fp.read()
                except FileNotFoundError as exc:
                    # 「無い」は、親フォルダを一覧できて本当に無いときだけ(届かないのは読めないのと同じ)
                    if app_config.confirmed_missing(self.path):
                        return {}
                    last = exc
                    if attempt + 1 < READ_TRIES:
                        time.sleep(READ_RETRY_SEC)
                    continue
                except OSError as exc:            # ロック中・権限など(一瞬のことがある)
                    last = exc
                    if attempt + 1 < READ_TRIES:
                        time.sleep(READ_RETRY_SEC)
                    continue
                except ValueError as exc:         # 文字の符号が壊れている
                    raise SettingsUnreadable(self.path, f"中身が壊れています: {exc}") from exc
                try:
                    data = json.loads(text)
                except ValueError as exc:
                    raise SettingsUnreadable(self.path, f"中身が壊れています: {exc}") from exc
                if not isinstance(data, dict):
                    raise SettingsUnreadable(self.path, "中身の形が違います")
                return data
            raise SettingsUnreadable(self.path, last)

    def save(self, data: Dict[str, Any]) -> None:
        with self._lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self._backup()
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(data, fp, ensure_ascii=False, indent=2)
            _replace(tmp, self.path)

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
