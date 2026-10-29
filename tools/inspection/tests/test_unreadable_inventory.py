"""「読めなかった」と「本当に無い」を分ける(点検表 その2)

1.5.79 / 1.5.80 の点検の続き。読めなかったときは**書かない・消さない・画面から消さない**。

- 点検表フォルダの一部(または全部)を読めないと、そのフォルダを空として一覧を作り直していた ──
  共有の瞬断・ロック・権限だけで、その点検表が画面から消え、選んでおいたものも外れた。
  読めなかったフォルダの点検表は前の一覧のまま出す。読めたのに無いもの・本当に無いフォルダは今までどおり
- 共有フォルダ・サーバに届かないと、Windows は「見つからない」(FileNotFoundError)と答える。
  それだけで「無い」とみなし、この端末の設定ファイルを空から書き直していた・点検表のフォルダの
  配布設定が無いとみなして、古いまとめたファイルの値に戻していた
- 起動した処理(Excel・Word)の記録を一瞬読めないと空とみなし、1件足す・片付けるたびに
  ほかの記録を消していた(消えた記録の Excel は、あとで片付けられずに残る)
- この端末の設定ファイルの置き換えを Windows が一瞬断ると、保存に失敗していた
"""
from __future__ import annotations

import builtins
import contextlib
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import make_cfg, make_tree, quiet_logger, temp_dir

from app.repositories import settings_repository
from app.repositories.settings_repository import SettingsRepository
from app.services import admin_password, distribution, inspection_service
from app.services.admin_password import AdminPassword
from app.services.distribution import Distribution
from app.services.inspection_service import InspectionService
from app.services.settings_service import SettingsService
from core import app_config, process_tracking
from core.process_tracking import ProcessIdentity, TrackedProcessRegistry


def _norm(p) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(p)))


@contextlib.contextmanager
def unreachable(*paths):
    """そのファイル・フォルダに**届かない**ようにする(Windows で共有に届かないときと同じ:
    中身は `FileNotFoundError`、親フォルダの一覧も `OSError`)。"""
    targets = {_norm(p) for p in paths}
    parents = {os.path.dirname(t) for t in targets}
    real_open, real_read_text, real_listdir = builtins.open, Path.read_text, os.listdir
    real_isdir, real_scandir = os.path.isdir, os.scandir

    def hit(p) -> bool:
        return isinstance(p, (str, os.PathLike)) and _norm(p) in targets

    def fake_open(file, mode="r", *args, **kwargs):
        if hit(file) and "r" in mode and "+" not in mode:
            raise FileNotFoundError(2, "ネットワーク パスが見つかりません", str(file))
        return real_open(file, mode, *args, **kwargs)

    def fake_read_text(self, *args, **kwargs):
        if hit(self):
            raise FileNotFoundError(2, "ネットワーク パスが見つかりません", str(self))
        return real_read_text(self, *args, **kwargs)

    def fake_listdir(path="."):
        if _norm(path) in parents or _norm(path) in targets:
            raise OSError(64, "指定されたネットワーク名は利用できなくなりました", str(path))
        return real_listdir(path)

    def fake_isdir(path):
        return False if hit(path) else real_isdir(path)

    def fake_scandir(path="."):
        if hit(path):
            raise FileNotFoundError(2, "ネットワーク パスが見つかりません", str(path))
        return real_scandir(path)

    with mock.patch.object(builtins, "open", fake_open), \
            mock.patch.object(Path, "read_text", fake_read_text), \
            mock.patch.object(os, "listdir", fake_listdir), \
            mock.patch.object(os.path, "isdir", fake_isdir), \
            mock.patch.object(os, "scandir", fake_scandir), \
            mock.patch.object(settings_repository, "READ_RETRY_SEC", 0):
        yield


@contextlib.contextmanager
def denied_folder(path):
    """そのフォルダの中を一覧できない(権限・ロック・共有の瞬断)。"""
    target = _norm(path)
    real_scandir = os.scandir

    def fake_scandir(p="."):
        if _norm(p) == target:
            raise PermissionError(13, "アクセスが拒否されました", str(p))
        return real_scandir(p)

    with mock.patch.object(os, "scandir", fake_scandir):
        yield


class ConfirmedMissingTests(unittest.TestCase):
    def test_親を一覧できて無いときだけ無いとする(self) -> None:
        tmp = temp_dir()
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name)
        self.assertTrue(app_config.confirmed_missing(folder / "無い.json"))
        self.assertTrue(app_config.confirmed_missing(folder / "無い" / "その下" / "x.json"))
        (folder / "ある.json").write_text("{}", encoding="utf-8")
        self.assertFalse(app_config.confirmed_missing(folder / "ある.json"), "一覧にあるなら読めないだけ")
        with unreachable(folder / "x.json"):
            self.assertFalse(app_config.confirmed_missing(folder / "x.json"), "親に届かないなら無いとは言えない")


# ======================================================================
# 点検表の一覧
# ======================================================================
FILES = ["梱包/日常/梱包_日常_台車.xlsx", "梱包/日常/梱包_日常_コンベア.xlsx",
         "梱包/月次/梱包_月次_外観.xlsx", "設備/週次/設備_週次_クレーン.xlsx"]


class InventoryUnreadableTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = temp_dir()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = str(self.tmp / "共有" / "点検表")
        make_tree(self.root, FILES)
        cfg = make_cfg(root_folder=self.root)
        settings = SettingsService(cfg, SettingsRepository(str(self.tmp / "data"), str(self.tmp / "backup")),
                                   quiet_logger())
        self.cache = str(self.tmp / "data" / "inventory_cache.json")
        self.service = InspectionService(cfg, settings, quiet_logger(), cache_path=self.cache)
        self.events = []
        real_record = inspection_service.event_log.record
        patcher = mock.patch.object(inspection_service.event_log, "record",
                                    lambda op, result="info", **kw: (self.events.append((op, result, kw)),
                                                                     real_record(op, result, **kw))[1])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.first = self.scan()
        self.ids = sorted(self.first.by_id)

    def scan(self):
        self.assertTrue(self.service.start_scan("試験"))
        self.assertTrue(self.service.wait(20))
        return self.service.inventory()

    def names(self, inv) -> list:
        return sorted(i.name for c in inv.categories for s in c.subcategories for i in s.items)

    def cached_names(self) -> list:
        with open(self.cache, encoding="utf-8") as fp:
            data = json.load(fp)
        return sorted(i["name"] for c in data["categories"] for s in c["subcategories"] for i in s["items"])

    def test_サブフォルダを読めなくても_その点検表を画面から消さない(self) -> None:
        with denied_folder(os.path.join(self.root, "梱包", "日常")):
            inv = self.scan()
        self.assertEqual(self.names(inv), ["クレーン", "コンベア", "台車", "外観"],
                         "読めなかったフォルダの点検表が画面から消えた")
        self.assertEqual(sorted(inv.by_id), self.ids, "同じ番号のまま(選んだものが外れない・刷れる)")
        self.assertEqual(inv.kept, 2)
        self.assertIn("前の一覧のまま", inv.to_dict()["notice"])
        self.assertEqual(inv.errors[0]["path"], os.path.join(self.root, "梱包", "日常"))
        self.assertEqual(self.cached_names(), ["クレーン", "コンベア", "台車", "外観"])
        # 読めたら今回の中身(本当に消したものは消える)
        os.remove(os.path.join(self.root, "梱包", "日常", "梱包_日常_台車.xlsx"))
        inv = self.scan()
        self.assertEqual(self.names(inv), ["クレーン", "コンベア", "外観"])
        self.assertEqual(inv.kept, 0)
        self.assertEqual(inv.to_dict()["notice"], "")

    def test_カテゴリーを読めなくても_その点検表を画面から消さない(self) -> None:
        with denied_folder(os.path.join(self.root, "設備")):
            inv = self.scan()
        self.assertEqual(self.names(inv), ["クレーン", "コンベア", "台車", "外観"])
        self.assertEqual([c.name for c in inv.categories], ["梱包", "設備"])

    def test_読めたフォルダから消えたものは残さない(self) -> None:
        os.remove(os.path.join(self.root, "梱包", "月次", "梱包_月次_外観.xlsx"))
        with denied_folder(os.path.join(self.root, "設備", "週次")):
            inv = self.scan()
        self.assertEqual(self.names(inv), ["クレーン", "コンベア", "台車"],
                         "読めたのに無い(消された)ものまで前の一覧から残した")

    def test_点検表フォルダに届かなくても_前の一覧のまま出す(self) -> None:
        with unreachable(self.root):
            inv = self.scan()
            data = inv.to_dict()
        self.assertTrue(inv.root_exists, "届かないのを「見つかりません」とし、一覧を画面から消した")
        self.assertTrue(data["unreadable"])
        self.assertEqual(self.names(inv), ["クレーン", "コンベア", "台車", "外観"])
        self.assertEqual(sorted(inv.by_id), self.ids)
        self.assertEqual(inv.scanned_at, self.first.scanned_at, "出しているのは前の一覧(その時刻)")
        self.assertIn("前の一覧のまま", data["notice"])
        self.assertEqual(self.cached_names(), ["クレーン", "コンベア", "台車", "外観"])
        scan = [kw for op, result, kw in self.events if op == "scan"]
        self.assertEqual(scan[-1]["code"], "FOLDER_UNREADABLE")

    def test_点検表フォルダに届かず前の一覧も無ければ_読めないと出す(self) -> None:
        self.service.clear()
        with unreachable(self.root):
            inv = self.scan()
        self.assertFalse(inv.root_exists)
        self.assertTrue(inv.unreadable, "届かないのを「見つかりません」と言わない")
        self.assertEqual(self.names(inv), [])

    def test_点検表フォルダが本当に無ければ今までどおり見つからない(self) -> None:
        os.rename(self.root, self.root + "_別の名前")
        inv = self.scan()
        self.assertFalse(inv.root_exists)
        self.assertFalse(inv.unreadable)
        self.assertEqual(self.names(inv), [])
        scan = [kw for op, result, kw in self.events if op == "scan"]
        self.assertEqual(scan[-1]["code"], "FOLDER_NOT_FOUND")


# ======================================================================
# この端末の設定ファイル・配布設定に届かない
# ======================================================================
class Terminal:
    def __init__(self, local: Path, dist_dir: Path):
        self.settings_file = local / "data" / "user_settings.json"
        self.settings = SettingsService(make_cfg(root_folder=r"\\srv\既定の点検表"),
                                        SettingsRepository(str(local / "data"), str(local / "backup")),
                                        quiet_logger())
        self.admin = AdminPassword(self.settings, quiet_logger())
        self.dist = Distribution(self.settings, self.admin, quiet_logger(), base_dir=dist_dir)

    def saved(self) -> dict:
        return json.loads(self.settings_file.read_text(encoding="utf-8"))


class SettingsUnreachableTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = temp_dir()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.dist_dir = self.tmp / "app" / "配布設定"
        self.dist_dir.parent.mkdir()

    def test_設定ファイルに届かないのを無いとみなして空から書き直さない(self) -> None:
        t = Terminal(self.tmp / "line1", self.dist_dir)
        t.settings.update(root_folder=r"\\srv\ライン1の点検表")
        t.settings.put("log_dir", r"\\srv\ログ")
        with unreachable(t.settings_file):
            again = Terminal(self.tmp / "line1", self.dist_dir)
            try:
                again.settings.update(theme="dark")
                raised = None
            except settings_repository.SettingsUnreadable as exc:
                raised = exc
        saved = t.saved()
        self.assertEqual(saved.get("root_folder"), r"\\srv\ライン1の点検表",
                         "届かないのを「無い」とみなし、空に足した1項目で上書きした")
        self.assertEqual(saved.get("log_dir"), r"\\srv\ログ")
        self.assertNotIn("theme_choice", saved)
        self.assertIsInstance(raised, settings_repository.SettingsUnreadable)

    def test_設定ファイルが本当に無ければ空から始める(self) -> None:
        t = Terminal(self.tmp / "新しい端末", self.dist_dir)
        self.assertEqual(t.settings.load_error, "")
        t.settings.update(theme="dark")
        self.assertEqual(t.saved(), {"theme_choice": "dark"})

    def test_置き換えを一瞬断られても保存する(self) -> None:
        t = Terminal(self.tmp / "line1", self.dist_dir)
        real = os.replace
        refused = []

        def replace_once(src, dst):
            if not refused:
                refused.append(dst)
                raise PermissionError(13, "ほかのプロセスが使用中です", str(dst))
            return real(src, dst)

        with mock.patch.object(settings_repository.os, "replace", replace_once), \
                mock.patch.object(settings_repository, "REPLACE_WAIT_SEC", 0, create=True):
            t.settings.update(root_folder=r"\\srv\点検表")
        self.assertTrue(refused)
        self.assertEqual(t.saved().get("root_folder"), r"\\srv\点検表")

    def test_点検表のフォルダの配布設定に届かなければ_古いまとめたファイルで戻さない(self) -> None:
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
        self.dist_dir.mkdir()
        (self.dist_dir / distribution.SETTINGS_NAME).write_text(json.dumps({
            "format": 1, "app_id": app_config.app_id(), "created_at": "2026-10-28 09:00:00",
            "settings": {"root_folder": r"\\srv\新しい点検表"}}, ensure_ascii=False), encoding="utf-8")
        line = Terminal(self.tmp / "line2", self.dist_dir)
        self.assertEqual(line.dist.apply_on_start().applied, ["点検表フォルダ"])
        with unreachable(self.dist_dir / distribution.SETTINGS_NAME):
            result = line.dist.apply_on_start()
            summary = line.dist.summary()
            again = line.dist.reapply(admin_password.DEFAULT)
        self.assertEqual(line.saved()["root_folder"], r"\\srv\新しい点検表",
                         "届かないのを「無い」とみなし、古いまとめたファイルの値に戻した")
        self.assertFalse(result.ok)
        self.assertEqual(result.applied, [])
        self.assertIn(distribution.SETTINGS_NAME, summary["read_error"])
        self.assertFalse(again.ok)
        self.assertEqual(line.saved()["root_folder"], r"\\srv\新しい点検表")


# ======================================================================
# 起動した処理(Excel・Word)の記録
# ======================================================================
class RegistryUnreadableTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = temp_dir()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "tracked_processes.json")
        self.registry = TrackedProcessRegistry(self.path, quiet_logger())
        # 使い回し中の Excel(この試験のプロセスがまだ動いているので、ほかのアプリのものとして残る)
        me = process_tracking.current_identity()
        self.other = {"pid": 4242, "create_time": 1, "exe": "EXCEL.EXE", "label": "excel",
                      "owner_pid": me.pid, "owner_create_time": me.create_time}
        with open(self.path, "w", encoding="utf-8") as fp:
            json.dump([self.other], fp)
        patcher = mock.patch.object(process_tracking, "READ_RETRY_SEC", 0, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def saved(self) -> list:
        with open(self.path, encoding="utf-8") as fp:
            return json.load(fp)

    @contextlib.contextmanager
    def locked(self):
        real_open = builtins.open
        target = _norm(self.path)

        def fake_open(file, mode="r", *args, **kwargs):
            if isinstance(file, (str, os.PathLike)) and _norm(file) == target and mode.startswith("r"):
                raise PermissionError(13, "ほかのプロセスが使用中です", str(file))
            return real_open(file, mode, *args, **kwargs)

        with mock.patch.object(builtins, "open", fake_open):
            yield

    def test_記録を読めなければ足すときにほかの記録を消さない(self) -> None:
        with self.locked():
            self.registry.add(ProcessIdentity(5151, 2, "WINWORD.EXE"), "word")
        self.assertEqual([e["pid"] for e in self.saved()], [4242], "読めないのを空とみなし、ほかの記録を消した")
        self.registry.add(ProcessIdentity(5151, 2, "WINWORD.EXE"), "word")
        self.assertEqual([e["pid"] for e in self.saved()], [4242, 5151], "読めれば今までどおり足す")

    def test_記録を読めなければ片付けで消さない(self) -> None:
        with mock.patch.object(process_tracking.os, "getpid", lambda: 1):
            with self.locked():
                self.registry.cleanup("試験")
                self.registry.remove(9999)
        self.assertEqual([e["pid"] for e in self.saved()], [4242], "読めないのを空とみなし、記録を消した")

    def test_記録が壊れていれば脇へ移して空から始める(self) -> None:
        with open(self.path, "w", encoding="utf-8") as fp:
            fp.write("{壊れて")
        self.registry.add(ProcessIdentity(5151, 2, "WINWORD.EXE"), "word")
        self.assertEqual([e["pid"] for e in self.saved()], [5151])
        aside = [n for n in os.listdir(os.path.dirname(self.path)) if ".broken-" in n]
        self.assertEqual(len(aside), 1, "壊れた記録は消さずに脇へ移す")


if __name__ == "__main__":
    unittest.main()
