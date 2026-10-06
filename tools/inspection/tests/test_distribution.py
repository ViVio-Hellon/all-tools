"""配布設定と管理者パスワード(python-web-tools の test_distribution / test_admin_password を移植)

1台で決めた設定(点検表フォルダ・管理者パスワード・表示)を、ほかのラインの端末で
そのまま使う。**その端末にすでにある設定は上書きしない**のが要点。
"""
from __future__ import annotations

import json
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import make_cfg, quiet_logger, temp_dir

from app.repositories.settings_repository import SettingsRepository
from app.services import admin_password, distribution
from app.services.admin_password import AdminPassword
from app.services.distribution import Distribution
from app.services.settings_service import SettingsService
from core import app_config

_ROOT = Path(__file__).resolve().parent.parent
NEW_PW = "line2026"


class Terminal:
    """1台の端末(利用者ごとのローカル領域)。配布設定のフォルダは共有する。"""

    def __init__(self, local: str, dist_dir: Path, root: str = r"\\srv\点検表"):
        self.settings = SettingsService(make_cfg(root_folder=root),
                                        SettingsRepository(os.path.join(local, "data"),
                                                           os.path.join(local, "backup")),
                                        quiet_logger())
        self.admin = AdminPassword(self.settings, quiet_logger(), default="nisk")
        self.dist = Distribution(self.settings, self.admin, quiet_logger(), base_dir=dist_dir)


class DistributionTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = temp_dir()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.dist_dir = self.tmp / "app" / "配布設定"
        self.dist_dir.parent.mkdir()

    def terminal(self, name: str) -> Terminal:
        return Terminal(str(self.tmp / name), self.dist_dir)

    def configured(self) -> Terminal:
        """配る側: 点検表フォルダを決め、パスワードを変えた端末。"""
        t = self.terminal("line1")
        t.settings.update(root_folder=r"\\srv\各課共有\点検表")
        self.assertTrue(t.admin.change("nisk", NEW_PW, NEW_PW).ok)
        return t


class AdminPasswordTests(DistributionTestCase):
    def test_変えていなければ既定で通る(self):
        t = self.terminal("a")
        self.assertTrue(t.admin.verify("nisk"))
        self.assertFalse(t.admin.is_custom())

    def test_変えると平文では持たない(self):
        t = self.terminal("a")
        self.assertTrue(t.admin.change("nisk", NEW_PW, NEW_PW).ok)
        stored = t.settings.get(admin_password.KEY)
        self.assertNotIn(NEW_PW, stored)
        self.assertTrue(admin_password.looks_stored(stored))
        self.assertTrue(t.admin.verify(NEW_PW))
        self.assertFalse(t.admin.verify("nisk"))

    def test_断りの種類(self):
        t = self.terminal("a")
        self.assertEqual(t.admin.change("違う", NEW_PW, NEW_PW).reason, admin_password.REFUSE_WRONG)
        self.assertEqual(t.admin.change("nisk", "abc", "abc").reason, admin_password.REFUSE_TOO_SHORT)
        self.assertEqual(t.admin.change("nisk", NEW_PW, "x").reason, admin_password.REFUSE_MISMATCH)
        self.assertEqual(t.admin.change("nisk", "nisk", "nisk").reason, admin_password.REFUSE_SAME)

    def test_既定に戻す(self):
        t = self.terminal("a")
        t.admin.change("nisk", NEW_PW, NEW_PW)
        self.assertFalse(t.admin.reset("nisk").ok)
        self.assertTrue(t.admin.reset(NEW_PW).ok)
        self.assertTrue(t.admin.verify("nisk"))
        self.assertNotIn(admin_password.KEY, t.settings.values())

    def test_壊れた値では通さない(self):
        t = self.terminal("a")
        t.settings.put(admin_password.KEY, "pbkdf2$壊れている")
        self.assertFalse(t.admin.verify("nisk"))


class ExportTests(DistributionTestCase):
    def test_パスワードが無ければ作らない(self):
        t = self.configured()
        result = t.dist.export("nisk", ["root_folder"])
        self.assertEqual(result.reason, distribution.REFUSE_NEED_PASSWORD)
        self.assertFalse(self.dist_dir.exists())

    def test_配布設定フォルダが作られ中身が入る(self):
        t = self.configured()
        result = t.dist.export(NEW_PW, ["root_folder", admin_password.KEY])
        self.assertTrue(result.ok, result.message)
        self.assertEqual(sorted(p.name for p in self.dist_dir.iterdir()),
                         sorted([distribution.README_NAME, distribution.SETTINGS_NAME]))
        data = json.loads((self.dist_dir / distribution.SETTINGS_NAME).read_text(encoding="utf-8"))
        self.assertEqual(data["app_id"], app_config.app_id())
        self.assertEqual(data["settings"]["root_folder"], r"\\srv\各課共有\点検表")
        self.assertNotIn(NEW_PW, json.dumps(data), "パスワードを平文で書かない")
        readme = (self.dist_dir / distribution.README_NAME).read_text(encoding="utf-8-sig")
        self.assertIn("点検表フォルダ", readme)
        self.assertNotIn(NEW_PW, readme)

    def test_設定していない項目は入れない(self):
        t = self.configured()
        result = t.dist.export(NEW_PW, ["root_folder", "dark_mode"])
        self.assertTrue(result.ok)
        self.assertIn("既定のまま", result.message)
        data = json.loads((self.dist_dir / distribution.SETTINGS_NAME).read_text(encoding="utf-8"))
        self.assertEqual(list(data["settings"]), ["root_folder"])

    def test_何も設定していなければ断る(self):
        t = self.terminal("empty")
        result = t.dist.export("nisk", ["root_folder"])
        self.assertEqual(result.reason, distribution.REFUSE_BAD_INPUT)
        self.assertFalse(self.dist_dir.exists())

    def test_知らない項目は断る(self):
        t = self.configured()
        self.assertEqual(t.dist.export(NEW_PW, ["port"]).reason, distribution.REFUSE_BAD_INPUT)
        self.assertEqual(t.dist.export(NEW_PW, []).reason, distribution.REFUSE_BAD_INPUT)

    def test_書き出し直すと前の中身は置き換わる(self):
        t = self.configured()
        t.dist.export(NEW_PW, ["root_folder", admin_password.KEY])
        t.dist.export(NEW_PW, ["root_folder"])
        self.assertEqual(list(t.dist.read().settings), ["root_folder"])
        self.assertEqual([p.name for p in self.dist_dir.parent.iterdir() if "作成中" in p.name], [],
                         "作りかけを残さない")

    def test_画面にパスワードの値を出さない(self):
        t = self.configured()
        t.dist.export(NEW_PW, ["root_folder", admin_password.KEY])
        summary = t.dist.summary()
        text = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn("pbkdf2", text)
        self.assertIn("(設定済み)", text)
        self.assertTrue(summary["exists"])

    def test_消すにもパスワード(self):
        t = self.configured()
        t.dist.export(NEW_PW, ["root_folder"])
        self.assertEqual(t.dist.remove("nisk").reason, distribution.REFUSE_NEED_PASSWORD)
        self.assertTrue(self.dist_dir.exists())
        self.assertTrue(t.dist.remove(NEW_PW).ok)
        self.assertFalse(self.dist_dir.exists())
        self.assertEqual(t.settings.to_dict()["root_folder"], r"\\srv\各課共有\点検表",
                         "消しても端末の設定はそのまま")

    def test_書けない場所なら理由を返す(self):
        t = self.configured()
        with mock.patch.object(Path, "rename", side_effect=OSError("アクセスが拒否されました")):
            result = t.dist.export(NEW_PW, ["root_folder"])
        self.assertEqual(result.reason, distribution.REFUSE_FAILED)
        self.assertIn("アクセスが拒否", result.message)
        self.assertEqual([p.name for p in self.dist_dir.parent.iterdir()], [], "作りかけを残さない")


class ApplyTests(DistributionTestCase):
    def exported(self) -> Terminal:
        t = self.configured()
        self.assertTrue(t.dist.export(NEW_PW, ["root_folder", admin_password.KEY]).ok)
        return t

    def test_配布設定フォルダがあれば読み込む(self):
        self.exported()
        line2 = self.terminal("line2")
        result = line2.dist.apply_on_start()
        self.assertEqual(result.applied, ["点検表フォルダ", "管理者パスワード"])
        self.assertTrue(result.root_changed)
        self.assertEqual(line2.settings.effective_root_folder(), os.path.normpath(r"\\srv\各課共有\点検表"))
        self.assertTrue(line2.admin.verify(NEW_PW), "パスワードも揃う")
        self.assertTrue(line2.dist.summary()["applied_at"])

    def test_すでにある項目は読み込まない(self):
        self.exported()
        line3 = self.terminal("line3")
        line3.settings.update(root_folder=r"\\srv\ライン3専用")
        result = line3.dist.apply_on_start()
        self.assertEqual(result.kept, ["点検表フォルダ"])
        self.assertEqual(line3.settings.to_dict()["root_folder"], os.path.normpath(r"\\srv\ライン3専用"))

    def test_起動のたびに見ても端末で直した値は戻さない(self):
        self.exported()
        line2 = self.terminal("line2")
        line2.dist.apply_on_start()
        line2.settings.update(root_folder=r"\\srv\直した")
        line2.dist.apply_on_start()
        self.assertEqual(line2.settings.to_dict()["root_folder"], os.path.normpath(r"\\srv\直した"))

    def test_読み込み直しはパスワードで上書き(self):
        self.exported()
        line3 = self.terminal("line3")
        line3.settings.update(root_folder=r"\\srv\ライン3専用")
        self.assertEqual(line3.dist.reapply("違う").reason, distribution.REFUSE_NEED_PASSWORD)
        result = line3.dist.reapply("nisk")          # line3 はまだ既定のパスワード
        self.assertTrue(result.ok)
        self.assertTrue(result.root_changed)
        self.assertEqual(line3.settings.to_dict()["root_folder"], os.path.normpath(r"\\srv\各課共有\点検表"))

    def test_無い_壊れた_形が違う_ほかの道具のものなら何もしない(self):
        line2 = self.terminal("line2")
        self.assertEqual(line2.dist.apply_on_start().applied, [])
        self.dist_dir.mkdir()
        path = self.dist_dir / distribution.SETTINGS_NAME
        for text in ("{壊れている", json.dumps({"format": 99, "settings": {"root_folder": "x"}}),
                     json.dumps({"format": 1, "app_id": "nlm.packaging-tool",
                                 "settings": {"root_folder": "x"}})):
            with self.subTest(text=text[:30]):
                path.write_text(text, encoding="utf-8")
                self.assertEqual(line2.dist.apply_on_start().applied, [])
                self.assertEqual(line2.settings.values(), {})

    def test_知らない鍵_壊れた値は入れない(self):
        self.dist_dir.mkdir()
        (self.dist_dir / distribution.SETTINGS_NAME).write_text(json.dumps({
            "format": 1, "app_id": app_config.app_id(),
            "settings": {"root_folder": r"\\srv\点検表", "port": 1, "dark_mode": "はい",
                         admin_password.KEY: "平文のパスワード"}}, ensure_ascii=False), encoding="utf-8")
        line2 = self.terminal("line2")
        self.assertEqual(line2.dist.apply_on_start().applied, ["点検表フォルダ"])
        self.assertEqual(set(line2.settings.values()), {"root_folder", distribution.KEY_APPLIED})


class StartupTests(unittest.TestCase):
    def test_一覧より先に読む(self):
        """点検表フォルダが入っているので、読む前に探すと既定の場所を見る。"""
        text = (_ROOT / "app" / "business.py").read_text(encoding="utf-8")
        body = text[text.index("def on_startup"):]
        self.assertLess(body.index("distribution.apply_on_start()"), body.index("load_cache()"))
        self.assertLess(body.index("distribution.apply_on_start()"), body.index("start_scan("))

    def test_配布設定は追跡しない(self):
        text = (_ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("配布設定/", text)
        self.assertIn("配布設定.作成中", text)

    def test_置き場所はアプリの直下(self):
        with mock.patch.dict(os.environ, {"INSPECTION_DISTRIBUTION_DIR": ""}):
            self.assertEqual(distribution.default_dir(), app_config.APP_ROOT / "配布設定")


try:
    import flask  # noqa: F401
    HAS_WEB = True
except ImportError:                                 # pragma: no cover
    HAS_WEB = False


@unittest.skipUnless(HAS_WEB, "Flask が入っていないためスキップ")
class WebTests(unittest.TestCase):
    TOKEN = "t"

    def setUp(self):
        from app import create_app
        from core import idle_exit
        idle_exit.reset()
        self.addCleanup(idle_exit.reset)
        self._tmp = temp_dir()
        self.addCleanup(self._tmp.cleanup)
        dist_dir = Path(self._tmp.name) / "配布設定"
        env = mock.patch.dict(os.environ, {"INSPECTION_DISTRIBUTION_DIR": str(dist_dir)})
        env.start()
        self.addCleanup(env.stop)
        self.app = create_app(token=self.TOKEN, port=8797, options={})
        self.biz = self.app.extensions["inspection"]
        self.addCleanup(lambda: shutil.rmtree(os.path.join(str(app_config.local_dir("data"))), True))
        self.biz.settings.put_many({k: None for k in list(self.biz.settings.values())})
        self.app.config["READY"] = True
        self.client = self.app.test_client()

    def post(self, path, body):
        return self.client.post(path, json=body, headers={"X-Tool-Token": self.TOKEN})

    def test_状態にパスワードの値を出さない(self):
        self.post("/api/settings/admin-password", {"current": "nisk", "new": NEW_PW, "confirm": NEW_PW})
        data = self.client.get("/api/settings", headers={"X-Tool-Token": self.TOKEN}).get_json()
        self.assertTrue(data["admin"]["custom"])
        self.assertNotIn("pbkdf2", json.dumps(data))

    def test_パスワード変更の断りは400(self):
        res = self.post("/api/settings/admin-password", {"current": "違う", "new": NEW_PW, "confirm": NEW_PW})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error"]["code"], admin_password.REFUSE_WRONG)

    def test_配布設定_パスワードが違えば403_形が違えば400(self):
        self.biz.settings.update(root_folder=self._tmp.name)
        self.assertEqual(self.post("/api/settings/distribution/export",
                                   {"items": ["root_folder"], "password": "x"}).status_code, 403)
        self.assertEqual(self.post("/api/settings/distribution/export",
                                   {"items": "root_folder", "password": "nisk"}).status_code, 400)

    def test_書き出すと状態に出る(self):
        self.biz.settings.update(root_folder=self._tmp.name)
        res = self.post("/api/settings/distribution/export", {"items": ["root_folder"], "password": "nisk"})
        self.assertEqual(res.status_code, 200, res.get_json())
        dist = res.get_json()["distribution"]
        self.assertTrue(dist["exists"])
        self.assertEqual(dist["contents"], [{"label": "点検表フォルダ", "value": self._tmp.name}])

    def test_読み込み直してフォルダが替われば一覧を読み直す(self):
        self.biz.settings.update(root_folder=self._tmp.name)
        self.post("/api/settings/distribution/export", {"items": ["root_folder"], "password": "nisk"})
        self.biz.settings.put("root_folder", None)
        res = self.post("/api/settings/distribution/reapply", {"password": "nisk"})
        self.assertTrue(res.get_json()["rescan"])
        self.biz.inspection.wait(10)

    def test_画面にタブと面がある(self):
        html = self.client.get("/").get_data(as_text=True)
        for marker in ('id="settings-tabs"', 'id="panel-folder"', 'id="panel-password"',
                       'id="panel-distribution"', 'id="dist-export"', 'id="adm-save"', 'js/app.js'):
            self.assertIn(marker, html)
        self.assertIn(">設定</button>", html.replace('<span class="gl">▤</span>', ""))


if __name__ == "__main__":
    unittest.main()
