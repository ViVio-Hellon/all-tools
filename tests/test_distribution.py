"""大設定の配布設定(`portal/distribution.py`)

- 書き出すのは**この端末で変えた項目だけ**(既定のままは入れない)。パスワードは撹拌した値で入る
- 配った先は起動のたびに、**その端末に無い項目だけ**埋める(端末で直した値は戻さない)
- 「読み込み直す」は上書きする。手で平文のパスワードを書かれていても、端末には撹拌して持つ
- 書き出す・読み込み直す・消すには、大設定の鍵(管理者パスワード)が要る
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import tests  # noqa: F401  (一時フォルダへ向ける)

from portal import admin_password, distribution, shared_db, user_settings, web

ROOT = Path(__file__).resolve().parent.parent
DIST = Path(os.environ["ALLTOOLS_DISTRIBUTION_DIR"])


def clear_terminal() -> None:
    """この端末の設定を、配られたばかりの端末と同じにする。"""
    user_settings.update({k: None for k in (user_settings.KEY_SHARED_DIR, user_settings.KEY_SHARED_NAME,
                                            user_settings.KEY_ADMIN_PASSWORD,
                                            user_settings.KEY_DISTRIBUTION_APPLIED)})


class DistributionTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_terminal()
        shutil.rmtree(DIST, ignore_errors=True)
        self.addCleanup(clear_terminal)
        self.addCleanup(shutil.rmtree, DIST, True)

    def test_既定のままなら書き出すものが無い(self) -> None:
        result = distribution.export()
        self.assertFalse(result.ok)
        self.assertFalse(DIST.exists())

    def test_書き出して_配った先で無い項目だけ埋める(self) -> None:
        user_settings.update({user_settings.KEY_SHARED_DIR: r"\\srv\共有\AIM"})
        self.assertTrue(admin_password.change("nisk", "line1234", "line1234").ok)
        result = distribution.export()
        self.assertTrue(result.ok, result.message)
        data = json.loads(distribution.file_path().read_text(encoding="utf-8"))
        self.assertEqual(set(data["settings"]), {user_settings.KEY_SHARED_DIR, user_settings.KEY_ADMIN_PASSWORD})
        self.assertTrue(data["settings"][user_settings.KEY_ADMIN_PASSWORD].startswith("pbkdf2$"))
        self.assertNotIn("line1234", distribution.file_path().read_text(encoding="utf-8"))
        readme = (DIST / distribution.README_NAME).read_text(encoding="utf-8-sig")
        self.assertIn("管理者パスワード: (設定済み)", readme)
        self.assertNotIn("line1234", readme)

        # 配った先(何も設定していない端末)
        clear_terminal()
        result = distribution.apply_on_start()
        self.assertEqual(sorted(result.applied), sorted(["共有の DB のフォルダ", "管理者パスワード"]))
        self.assertEqual(shared_db.location().folder, r"\\srv\共有\AIM")
        self.assertEqual(shared_db.location().origin, "大設定")
        self.assertTrue(admin_password.verify("line1234"))
        self.assertFalse(admin_password.verify("nisk"))

        # その端末で直した値は、次の起動で戻さない
        user_settings.update({user_settings.KEY_SHARED_DIR: r"\\srv\別"})
        result = distribution.apply_on_start()
        self.assertEqual(result.applied, [])
        self.assertIn("共有の DB のフォルダ", result.kept)
        self.assertEqual(shared_db.location().folder, r"\\srv\別")

        # 読み込み直す(上書き)
        result = distribution.reapply()
        self.assertIn("共有の DB のフォルダ", result.applied)
        self.assertEqual(shared_db.location().folder, r"\\srv\共有\AIM")

    def test_手で平文が書かれていても撹拌して持つ(self) -> None:
        DIST.mkdir(parents=True)
        distribution.file_path().write_text(json.dumps({
            "format": distribution.FORMAT, "settings": {"admin_password": "abcd", "知らない鍵": "x"}}),
            encoding="utf-8")
        result = distribution.apply_on_start()
        self.assertEqual(result.applied, ["管理者パスワード"])
        stored = user_settings.get(user_settings.KEY_ADMIN_PASSWORD)
        self.assertTrue(stored.startswith("pbkdf2$"))
        self.assertTrue(admin_password.verify("abcd"))
        self.assertIsNone(user_settings.get("知らない鍵"), "知らない鍵は捨てる")

    def test_名前を変える前に書き出した配布設定も読む(self) -> None:
        """1.3.x までは「統合ツール.json」。配った先の 配布設定\ を作り直さなくても効く。"""
        DIST.mkdir(parents=True)
        (DIST / "統合ツール.json").write_text(json.dumps({
            "format": distribution.FORMAT, "settings": {"shared_db_name": "古い名前.sqlite3"}}),
            encoding="utf-8")
        self.assertEqual(distribution.apply_on_start().applied, ["共有の DB のファイル名"])

    def test_形の違うものは読まない(self) -> None:
        DIST.mkdir(parents=True)
        distribution.file_path().write_text('{"format": 99, "settings": {"shared_db_dir": "x"}}', encoding="utf-8")
        self.assertIsNone(distribution.read())
        distribution.file_path().write_text("壊れた", encoding="utf-8")
        self.assertIsNone(distribution.read())
        self.assertEqual(distribution.apply_on_start().applied, [])

    def test_消す(self) -> None:
        user_settings.update({user_settings.KEY_SHARED_NAME: "別.sqlite3"})
        distribution.export()
        self.assertTrue(distribution.summary()["exists"])
        distribution.remove()
        self.assertFalse(DIST.exists())
        self.assertEqual(user_settings.get(user_settings.KEY_SHARED_NAME), "別.sqlite3", "端末の設定はそのまま")


class WebTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_terminal()
        shutil.rmtree(DIST, ignore_errors=True)
        self.addCleanup(clear_terminal)
        self.addCleanup(shutil.rmtree, DIST, True)
        admin_password.session.lock()
        self.client = web.create_app(token="t", bridge=False).test_client()

    def post(self, path: str, body=None):
        return self.client.post(path, json=body or {}, headers={"X-Tool-Token": "t"},
                                base_url="http://127.0.0.1:8700")

    def test_鍵が要る_書き出すと大設定に出る(self) -> None:
        res = self.post("/api/distribution/export")
        self.assertEqual(res.status_code, 403)
        self.post("/api/settings/auth", {"password": "nisk"})
        res = self.post("/api/distribution/export")
        self.assertEqual(res.status_code, 422, "既定のままなら書き出せない")
        user_settings.update({user_settings.KEY_SHARED_DIR: r"\\srv\共有"})
        res = self.post("/api/distribution/export")
        self.assertEqual(res.status_code, 200, res.get_json())
        view = res.get_json()["distribution"]
        self.assertTrue(view["exists"])
        self.assertEqual(view["contents"], [{"label": "共有の DB のフォルダ", "value": r"\\srv\共有"}])
        res = self.post("/api/distribution/remove")
        self.assertFalse(res.get_json()["distribution"]["exists"])


class ToolSettingsTests(unittest.TestCase):
    """大設定に、各ツールの配布設定(そのツールのフォルダの中)が書き出してあるかを出す。

        少なくとも日報管理ツールでは配布設定してもフォルダは生成されていない
        配布設定後に make_dist.bat 実行 が正しい手順ですか？

    各ツールの配布設定は `tools\\<ツール>\\配布設定\\` にできる(一式の直下ではない)。
    どこにあるか・make_dist.bat で入るのかが分からなかった。
    """

    def test_各ツールの有り無しと置き場所と作成日時(self) -> None:
        from unittest import mock

        from portal import catalog as catalog_mod

        with tempfile.TemporaryDirectory() as tmp:
            made = Path(tmp) / "nippou" / "配布設定"
            made.mkdir(parents=True)
            (made / "設定.json").write_text(json.dumps(
                {"created_at": "2026/10/07 09:00", "created_on": "LINE1-PC", "settings": {}}), encoding="utf-8")
            real = catalog_mod.load()
            tools = [type(t)(**{**t.__dict__, "dir": Path(tmp) / t.id}) for t in real.tools]
            fake = mock.Mock(tools=tools)
            with mock.patch.object(catalog_mod, "load", return_value=fake):
                found = {t["id"]: t for t in distribution.summary()["tools"]}
        self.assertTrue(found["nippou"]["exists"])
        self.assertEqual(found["nippou"]["created_at"], "2026/10/07 09:00")
        self.assertTrue(found["nippou"]["path"].endswith(str(Path("nippou") / "配布設定")))
        self.assertFalse(found["kanban"]["exists"])

    def test_大設定の画面に一覧と手順がある(self) -> None:
        html = (ROOT / "portal" / "templates" / "settings.html").read_text(encoding="utf-8")
        self.assertIn('id="dist-tools"', html)
        self.assertIn("scripts\\make_dist.bat", html)


class MakeDistTests(unittest.TestCase):
    def test_配布メモには項目の名前だけ_値は出さない(self) -> None:
        spec = importlib.util.spec_from_file_location("alltools_make_dist_t", ROOT / "scripts" / "make_dist.py")
        make_dist = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(make_dist)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "日報複合ツール.json").write_text(json.dumps({
                "format": 1, "settings": {"shared_db_dir": r"\\srv\x", "admin_password": "pbkdf2$1$a$b"}}),
                encoding="utf-8")
            self.assertEqual(make_dist._portal_settings_lines(folder),
                             ["共有の DB のフォルダ", "管理者パスワード"])


if __name__ == "__main__":
    unittest.main()
