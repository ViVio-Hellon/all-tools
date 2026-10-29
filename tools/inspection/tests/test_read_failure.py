"""「読めなかった」と「本当に無い/空」を分ける(点検表)

ほかのツールで起きたこと:

    共有ファイルを読めなかった(ネットワークが一瞬切れた・ロック中・権限)のを「実績の表が無い」と
    取り違え、手元の実績を送り直していた。共有側に重複が大量にできた

点検表にも同じ形の取り違えがあった。読めなかったときは**書かない・消さない・既定に戻さない**
(次の回にやり直す)。理由はログと画面に出す。

- この端末の設定ファイル(`user_settings.json`)を読めないと空とみなし、1項目保存すると
  点検表フォルダ・管理者パスワード・ログの保存先が消えた。起動時の配布設定の読み込みも
  「この端末に無い」と見て上書きし、変えた端末でも既定のパスワードで通した
- 点検表のフォルダの配布設定を読めないと「無い」とみなし、古いまとめたファイルの値で戻した
- 設定を読めないと保存日数を既定(90日)とみなし、長く残すはずの古いログを消した。
  ログの保存先も既定(ローカル)に戻した
- 一部のフォルダを読めなかった一覧で、前回の一覧の控えを上書きした
"""
from __future__ import annotations

import builtins
import contextlib
import json
import os
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from tests.helpers import make_cfg, make_tree, quiet_logger, temp_dir

from app.repositories import settings_repository
from app.repositories.settings_repository import SettingsRepository
from app.services import admin_password, distribution
from app.services.admin_password import AdminPassword
from app.services.distribution import Distribution
from app.services.inspection_service import InspectionService
from app.services.settings_service import SettingsService
from core import app_config, logging_utils

NEW_PW = "line2026"


@contextlib.contextmanager
def unreadable(*paths):
    """そのファイルを、**あるのに読めない**ようにする(ほかが使用中・権限)。書くのはできる。"""
    targets = {os.path.normcase(os.path.abspath(str(p))) for p in paths}
    real_open = builtins.open
    real_read_text = Path.read_text

    def is_target(file) -> bool:
        return isinstance(file, (str, os.PathLike)) and \
            os.path.normcase(os.path.abspath(os.fspath(file))) in targets

    def fake_open(file, mode="r", *args, **kwargs):
        if is_target(file) and "r" in mode and "+" not in mode:
            raise PermissionError(13, "ほかのプロセスが使用中です", str(file))
        return real_open(file, mode, *args, **kwargs)

    def fake_read_text(self, *args, **kwargs):
        if is_target(self):
            raise PermissionError(13, "ほかのプロセスが使用中です", str(self))
        return real_read_text(self, *args, **kwargs)

    with mock.patch.object(builtins, "open", fake_open), \
            mock.patch.object(Path, "read_text", fake_read_text), \
            mock.patch.object(settings_repository, "READ_RETRY_SEC", 0, create=True):
        yield


def attempt(work):
    """やってみる。(戻り値, 起きた例外) ── 先に「消していない」を確かめるため。"""
    try:
        return work(), None
    except Exception as exc:                          # noqa: BLE001
        return None, exc


class Terminal:
    def __init__(self, local: Path, dist_dir: Path):
        self.local = local
        self.settings_file = local / "data" / "user_settings.json"
        self.settings = SettingsService(make_cfg(root_folder=r"\\srv\既定の点検表"),
                                        SettingsRepository(str(local / "data"), str(local / "backup")),
                                        quiet_logger())
        self.admin = AdminPassword(self.settings, quiet_logger())
        self.dist = Distribution(self.settings, self.admin, quiet_logger(), base_dir=dist_dir)

    def saved(self) -> dict:
        return json.loads(self.settings_file.read_text(encoding="utf-8"))


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = temp_dir()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.dist_dir = self.tmp / "app" / "配布設定"
        self.dist_dir.parent.mkdir()

    def configured(self, name: str = "line1") -> Terminal:
        """この端末で点検表フォルダ・パスワード・ログの保存先を決めた端末。"""
        t = Terminal(self.tmp / name, self.dist_dir)
        t.settings.update(root_folder=r"\\srv\ライン1の点検表")
        self.assertTrue(t.admin.change(admin_password.DEFAULT, NEW_PW, NEW_PW).ok)
        t.settings.put(logging_utils.KEY_DIR, r"\\srv\ログ")
        return t

    def reopen(self, t: Terminal) -> Terminal:
        """同じ端末で起動し直す。"""
        return Terminal(t.local, self.dist_dir)

    def put_distribution(self, settings: dict, created_at: str = "2026-10-28 09:00:00") -> None:
        self.dist_dir.mkdir(parents=True, exist_ok=True)
        (self.dist_dir / distribution.SETTINGS_NAME).write_text(json.dumps({
            "format": 1, "app_id": app_config.app_id(), "created_at": created_at, "settings": settings},
            ensure_ascii=False), encoding="utf-8")


class SettingsTests(_Case):
    def test_起動時に読めなければ_保存しても前の設定を消さない(self) -> None:
        t = self.configured()
        with unreadable(t.settings_file):
            line = self.reopen(t)
            _, raised = attempt(lambda: line.settings.update(theme="dark"))
        saved = t.saved()
        self.assertEqual(saved.get("root_folder"), os.path.normpath(r"\\srv\ライン1の点検表"),
                         "読めなかったのに空とみなして上書きした")
        self.assertTrue(admin_password.looks_stored(saved.get(admin_password.KEY)))
        self.assertEqual(saved.get(logging_utils.KEY_DIR), r"\\srv\ログ")
        self.assertNotIn("theme_choice", saved, "読めないあいだは保存しない")
        self.assertIsInstance(raised, settings_repository.SettingsUnreadable)
        self.assertEqual(raised.status, 503)
        self.assertTrue(line.settings.load_error)
        # 読めるようになれば、読み直してから保存する(前の設定はそのまま)
        line.settings.update(theme="dark")
        saved = t.saved()
        self.assertEqual(saved["theme_choice"], "dark")
        self.assertEqual(saved.get("root_folder"), os.path.normpath(r"\\srv\ライン1の点検表"))
        self.assertEqual(line.settings.load_error, "")
        self.assertEqual(line.settings.effective_root_folder(), os.path.normpath(r"\\srv\ライン1の点検表"))

    def test_無ければ今までどおり空から始めて保存する(self) -> None:
        t = Terminal(self.tmp / "new", self.dist_dir)
        t.settings.update(theme="dark")
        self.assertEqual(t.saved(), {"theme_choice": "dark"})

    def test_読めなければ既定のパスワードで通さない(self) -> None:
        t = self.configured()
        with unreadable(t.settings_file):
            line = self.reopen(t)
            ok, raised = attempt(lambda: line.admin.verify(admin_password.DEFAULT))
        self.assertNotEqual(ok, True, "変えた端末なのに、読めないあいだ既定のパスワードで通した")
        self.assertIsInstance(raised, settings_repository.SettingsUnreadable)
        self.assertTrue(line.admin.verify(NEW_PW), "読めるようになれば変えたパスワードで通る")


class DistributionTests(_Case):
    def test_起動時に設定を読めなければ_配布設定で上書きしない(self) -> None:
        t = self.configured()
        self.put_distribution({"root_folder": r"\\srv\配布設定の点検表"})
        with unreadable(t.settings_file):
            line = self.reopen(t)
            result = line.dist.apply_on_start()
        saved = t.saved()
        self.assertEqual(saved.get("root_folder"), os.path.normpath(r"\\srv\ライン1の点検表"),
                         "読めなかったのを「この端末に無い」とみなして配布設定で上書きした")
        self.assertTrue(admin_password.looks_stored(saved.get(admin_password.KEY)), "変えたパスワードを消した")
        self.assertNotIn(distribution.KEY_FROM_DIST, saved)
        self.assertEqual(result.applied, [])
        self.assertFalse(result.ok)
        self.assertIn("設定ファイルを読めない", result.message)
        # 読めるようになれば今までどおり(この端末で決めた値は残す)
        self.assertEqual(self.reopen(t).dist.apply_on_start().kept, ["点検表フォルダ"])

    def test_書き出すときに読めなければ_既定のままと取り違えない(self) -> None:
        t = self.configured()

        class Checked:                               # 大設定の「まとめて書き出す」と同じ(確かめ済み)
            def verify(self, password):  # noqa: ARG002
                return True

        with unreadable(t.settings_file):
            line = self.reopen(t)
            result = Distribution(line.settings, Checked(), quiet_logger(), base_dir=self.dist_dir).export(
                "", ["root_folder", admin_password.KEY])
        self.assertNotEqual(result.reason, distribution.REFUSE_NOTHING,
                            "読めなかったのを「既定のままなので要りません」と答えた")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, distribution.REFUSE_FAILED)
        self.assertFalse((self.dist_dir / distribution.SETTINGS_NAME).exists())

    def test_点検表のフォルダの配布設定を読めなければ_古いまとめたファイルで戻さない(self) -> None:
        whole_dir = self.tmp / "一式" / "配布設定"
        env = mock.patch.dict(os.environ, {"ALLTOOLS_DISTRIBUTION_DIR": str(whole_dir)})
        env.start()
        self.addCleanup(env.stop)
        whole_dir.mkdir(parents=True)
        (whole_dir / distribution.INTEGRATED_FILE).write_text(json.dumps({
            "format": 1, "created_at": "2026/10/01 09:00:00", "settings": {},
            "tools": {"inspection": {"format": 1, "app_id": app_config.app_id(),
                                     "created_at": "2026-10-01 09:00:00",
                                     "settings": {"root_folder": r"\\srv\古い点検表"}}}},
            ensure_ascii=False), encoding="utf-8")
        self.put_distribution({"root_folder": r"\\srv\新しい点検表"}, "2026-10-28 09:00:00")
        line = Terminal(self.tmp / "line2", self.dist_dir)
        self.assertEqual(line.dist.apply_on_start().applied, ["点検表フォルダ"])
        self.assertEqual(line.saved()["root_folder"], r"\\srv\新しい点検表")
        with unreadable(self.dist_dir / distribution.SETTINGS_NAME):
            result = line.dist.apply_on_start()
            summary = line.dist.summary()
        self.assertEqual(line.saved()["root_folder"], r"\\srv\新しい点検表",
                         "読めなかったのを「無い」とみなし、古いまとめたファイルの値に戻した")
        self.assertEqual(result.applied, [])
        self.assertFalse(result.ok)
        self.assertIn(distribution.SETTINGS_NAME, summary["read_error"])
        with unreadable(self.dist_dir / distribution.SETTINGS_NAME):
            again = line.dist.reapply(admin_password.DEFAULT)
        self.assertFalse(again.ok)
        self.assertIn("読めませんでした", again.message)
        self.assertEqual(line.saved()["root_folder"], r"\\srv\新しい点検表")


class LogTests(unittest.TestCase):
    """ログの保存日数・保存先。読めないのを「決めていない(既定)」と取り違えない。"""

    def setUp(self) -> None:
        tmp = temp_dir()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.path = logging_utils.settings_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.addCleanup(self._restore)
        logging_utils.reset_for_tests()

    def _restore(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass
        logging_utils.reset_for_tests()

    def test_設定を読めなければ古いログを消さない(self) -> None:
        self.path.write_text(json.dumps({logging_utils.KEY_KEEP_DAYS: 365}), encoding="utf-8")
        logging_utils.reset_for_tests()
        folder = logging_utils.tool_dir(app_config.local_dir("logs"))
        folder.mkdir(parents=True, exist_ok=True)
        old = (datetime.now() - timedelta(days=200)).strftime("%Y%m%d")
        kept = folder / f"inspection_{old}.log"          # 365 日残す設定なので、まだ消さない
        kept.write_text("x", encoding="utf-8")
        self.addCleanup(lambda: kept.unlink(missing_ok=True))
        with unreadable(self.path):
            logging_utils.reset_for_tests()
            removed = logging_utils.prune()
            status = logging_utils.status()
        self.assertTrue(kept.exists(), "読めなかったのを保存日数の既定(90日)とみなして消した")
        self.assertEqual(removed, [])
        self.assertTrue(status["settings_error"])
        # 読めるようになれば、決めた日数(365日)で片付ける
        self.assertEqual(logging_utils.prune(), [])
        self.assertTrue(kept.exists())
        self.assertEqual(logging_utils.status()["keep_days"], 365)

    def test_設定を読めなければログの保存先を既定に戻さない(self) -> None:
        from app.business import InspectionBusiness

        shared = str(self.tmp / "共有" / "ログ")
        logging_utils.apply_settings(shared, 30)
        self.assertEqual(logging_utils.status()["requested"], os.path.normpath(shared))
        fake = types.SimpleNamespace(settings=types.SimpleNamespace(load_error="読めません", get=lambda k: None),
                                     log=quiet_logger())
        InspectionBusiness.apply_log_settings(fake)
        self.assertEqual(logging_utils.status()["requested"], os.path.normpath(shared),
                         "読めなかったのを「決めていない」とみなし、ログの保存先をローカルに戻した")


class InventoryCacheTests(unittest.TestCase):
    """前回の一覧の控え。一部のフォルダを読めなかった一覧で上書きしない。"""

    def setUp(self) -> None:
        tmp = temp_dir()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = str(self.tmp / "点検表")
        make_tree(self.root, ["梱包/日常/梱包_日常_台車.xlsx", "梱包/日常/梱包_日常_コンベア.xlsx",
                              "設備/月次/設備_月次_外観.xlsx"])
        cfg = make_cfg(root_folder=self.root)
        settings = SettingsService(cfg, SettingsRepository(str(self.tmp / "data"), str(self.tmp / "backup")),
                                   quiet_logger())
        self.cache = str(self.tmp / "data" / "inventory_cache.json")
        self.service = InspectionService(cfg, settings, quiet_logger(), cache_path=self.cache)

    def scan(self):
        self.assertTrue(self.service.start_scan("試験"))
        self.assertTrue(self.service.wait(20))
        return self.service.inventory()

    def cached_names(self):
        with open(self.cache, encoding="utf-8") as fp:
            data = json.load(fp)
        return sorted(i["name"] for c in data["categories"] for s in c["subcategories"] for i in s["items"])

    def test_一部のフォルダを読めなければ前回の控えを残す(self) -> None:
        self.scan()
        self.assertEqual(self.cached_names(), ["コンベア", "台車", "外観"])
        from app.services import inspection_service
        original = inspection_service._list_dirs
        denied = os.path.join(self.root, "設備")

        def deny(path, errors):
            if os.path.normcase(path) == os.path.normcase(denied):
                errors.append({"path": path, "message": "フォルダを読み取れません: アクセスが拒否されました。"})
                return []
            return original(path, errors)

        with mock.patch.object(inspection_service, "_list_dirs", deny):
            inv = self.scan()
        self.assertEqual(inv.errors[0]["path"], denied, "読めなかったことは一覧に出す")
        self.assertEqual(self.cached_names(), ["コンベア", "台車", "外観"],
                         "読めなかったフォルダの点検表が抜けた一覧で、前回の控えを上書きした")
        # 読めるようになれば今までどおり控えを書き直す(本当に消したものは控えからも消える)
        os.remove(os.path.join(self.root, "梱包", "日常", "梱包_日常_台車.xlsx"))
        self.scan()
        self.assertEqual(self.cached_names(), ["コンベア", "外観"])


if __name__ == "__main__":
    unittest.main()
