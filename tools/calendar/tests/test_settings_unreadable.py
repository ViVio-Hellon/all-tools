"""この端末の設定ファイル(settings.json)を「読めなかった」のを「無い・空」と取り違えない

別ツールで、共有を読めなかったのを「実績の表が無い」と見て手元の実績を送り直し、重複が大量に
できた事案を受けた点検の続き。設定ファイルも同じ型で、以前は読めない(ロック中・権限)と空の
設定として返していた:

    ・そのまま1項目を保存すると、ほかの設定(ライン・置き場所・管理者パスワード)が全部消えた
    ・管理者パスワードを変えた端末でも、既定のパスワードで通った

もう1つ、Windows ではほかの処理がファイルを開いている瞬間に置き換えると「アクセスが拒否
されました」(WinError 5)になり、保存が落ちていた(Windows の CI で起きた)。
"""
from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

from calendar_app import admin_password, config, settings as user_settings


class _Keep(unittest.TestCase):
    """試験の前の設定ファイルを残して、終わったら戻す。"""

    def setUp(self) -> None:
        self.path = config.settings_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.saved = self.path.with_name("settings.keep-for-test.json")
        if self.path.exists():
            shutil.copy2(self.path, self.saved)
            self.addCleanup(self._restore)
        else:
            self.addCleanup(self._remove)
        self.write({"PCライン": "L-1", "ログフォルダ": r"\\server\logs"})

    def _restore(self) -> None:
        shutil.copy2(self.saved, self.path)
        self.saved.unlink()

    def _remove(self) -> None:
        if self.path.exists():
            self.path.unlink()

    def write(self, data: dict) -> None:
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def stored(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def unreadable(self):
        """設定ファイルを開くと断られる(ほかの処理が掴んでいる・権限)。"""
        real = Path.open
        target = self.path

        def refuse(path, *args, **kwargs):
            if Path(path) == target and "w" not in (args[0] if args else kwargs.get("mode", "r")):
                raise PermissionError(13, "Permission denied", str(path))
            return real(path, *args, **kwargs)

        return patch.object(Path, "open", refuse)


class ReadTests(_Keep):
    def test_読めないときは空と答えない(self) -> None:
        with self.unreadable(), patch.object(user_settings, "READ_WAIT_SEC", 0):
            with self.assertRaises(user_settings.SettingsUnreadable):
                user_settings.load_checked()
            self.assertEqual(user_settings.load(), {})     # 画面に出すだけなら空で続ける

    def test_読めないときは保存せず_ほかの設定を消さない(self) -> None:
        with self.unreadable(), patch.object(user_settings, "READ_WAIT_SEC", 0):
            with self.assertRaises(user_settings.SettingsUnreadable):
                user_settings.set_value("自動同期", False)
            with self.assertRaises(user_settings.SettingsUnreadable):
                user_settings.update({"自動同期": False})
        self.assertEqual(self.stored(), {"PCライン": "L-1", "ログフォルダ": r"\\server\logs"})

    def test_一瞬だけ読めないなら読み直して保存する(self) -> None:
        real = Path.open
        calls = {"n": 0}

        def once(path, *args, **kwargs):
            if Path(path) == self.path and calls["n"] == 0 and not (args and "w" in args[0]):
                calls["n"] += 1
                raise PermissionError(13, "Permission denied", str(path))
            return real(path, *args, **kwargs)

        with patch.object(Path, "open", once), patch.object(user_settings, "READ_WAIT_SEC", 0):
            user_settings.set_value("自動同期", False)
        self.assertEqual(self.stored()["PCライン"], "L-1")
        self.assertIs(self.stored()["自動同期"], False)

    def test_本当に無いなら今までどおり空から書く(self) -> None:
        self.path.unlink()
        self.assertEqual(user_settings.load_checked(), {})
        user_settings.set_value("PCライン", "HVC")
        self.assertEqual(self.stored(), {"PCライン": "HVC"})


class PasswordTests(_Keep):
    def setUp(self) -> None:
        super().setUp()
        result = admin_password.change(config.ADMIN_PASSWORD, "この端末の合言葉", "この端末の合言葉")
        self.assertTrue(result.ok, result.message)

    def test_読めないときに既定のパスワードで通さない(self) -> None:
        with self.unreadable(), patch.object(user_settings, "READ_WAIT_SEC", 0):
            with self.assertRaises(user_settings.SettingsUnreadable):
                admin_password.verify(config.ADMIN_PASSWORD)
            result = admin_password.guard(["line"], config.ADMIN_PASSWORD)
        self.assertIsNotNone(result)
        self.assertEqual(result.reason, admin_password.REFUSE_UNREADABLE)

    def test_読めないときは変えない・戻さない(self) -> None:
        with self.unreadable(), patch.object(user_settings, "READ_WAIT_SEC", 0):
            changed = admin_password.change("この端末の合言葉", "別の合言葉", "別の合言葉")
            reset = admin_password.reset("この端末の合言葉")
        self.assertEqual((changed.ok, changed.reason), (False, admin_password.REFUSE_UNREADABLE))
        self.assertEqual((reset.ok, reset.reason), (False, admin_password.REFUSE_UNREADABLE))
        self.assertTrue(admin_password.verify("この端末の合言葉"))
        self.assertEqual(self.stored()["PCライン"], "L-1")


class ReplaceTests(_Keep):
    def test_置き換えを一瞬断られたらやり直す(self) -> None:
        """Windows: ほかの処理が開いている瞬間の置き換えは WinError 5 になる。"""
        real = Path.replace
        calls = {"n": 0}

        def refuse_twice(src, dst):
            if calls["n"] < 2:
                calls["n"] += 1
                raise PermissionError(13, "Access is denied", str(src))
            return real(src, dst)

        with patch.object(Path, "replace", refuse_twice), \
                patch.object(user_settings, "REPLACE_WAIT_SEC", 0):
            user_settings.set_value("自動同期", False)
        self.assertEqual(calls["n"], 2)
        self.assertIs(self.stored()["自動同期"], False)
        self.assertEqual(list(self.path.parent.glob("settings.*.tmp")), [])

    def test_ずっと断られたら一時ファイルを残さず断る(self) -> None:
        def refuse(src, dst):
            raise PermissionError(13, "Access is denied", str(src))

        with patch.object(Path, "replace", refuse), \
                patch.object(user_settings, "REPLACE_WAIT_SEC", 0):
            with self.assertRaises(PermissionError):
                user_settings.set_value("自動同期", False)
        self.assertEqual(self.stored(), {"PCライン": "L-1", "ログフォルダ": r"\\server\logs"})
        self.assertEqual(list(self.path.parent.glob("settings.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
