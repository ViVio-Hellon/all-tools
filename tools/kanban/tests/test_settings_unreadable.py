"""この端末の設定ファイル(``%APPDATA%\\KanbanSystem\\config.json``)を「読めなかった」のを「無い・空」と取り違えない

別ツールで、共有を読めなかったのを「実績の表が無い」と見て手元の実績を送り直し、重複が大量に
できた事案を受けた点検の続き。看板の設定ファイルにも同じ型があった:

    ・ほかの処理が開いている瞬間・権限が無いときも「JSON が壊れている」と同じに扱っていた
    ・付記を1つ書くだけで(配布設定を読み込んだ日時など)、元のファイルを脇へ移し、
      付記1つだけのほぼ空の設定ファイルに置き換えていた(担当ライン・接続先・管理者パスワードが消える)
    ・起動時に読めないと組み込みの既定で動き、そのまま保存すると既定の値を書いていた
    ・管理者パスワードを決めてある端末でも、読めないと既定の値で通っていた
    ・配布設定の書き出しは「既定のままなので要りません」と答えていた(大設定がそれを信じる)

もう1つ、Windows ではほかの処理がファイルを開いている瞬間に置き換えると「アクセスが拒否
されました」(WinError 5)になり、保存が落ちていた(一時ファイルも残っていた)。

**「読めない」は patch で作ります**(``Path.read_text`` が断る)。待ち時間は 0 にします。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kanban import config, distribution
from tests.test_web_app import HAS_FLASK, TEST_ADMIN_PASSWORD, RouteTestBase

#: 試験で使う管理者パスワード(この端末で決めた値)。既定の値ではない
DECIDED_PASSWORD = "この端末で決めた値"


def refuse_reading(target: Path, times: int | None = None):
    """``target`` を読もうとすると断られる(ほかの処理が開いている・権限が無い)。

    ``times`` を渡すと、その回数だけ断って、あとは読める(一瞬だけの不調)。
    """
    real = Path.read_text
    left = {"n": times}

    def read_text(self, *args, **kwargs):
        if Path(self) == Path(target) and (left["n"] is None or left["n"] > 0):
            if left["n"] is not None:
                left["n"] -= 1
            raise PermissionError(13, "Permission denied", str(self))
        return real(self, *args, **kwargs)

    return mock.patch.object(Path, "read_text", read_text)


def no_wait():
    """読み直し・置き換えのやり直しの待ち時間を 0 にする。"""
    patches = [
        mock.patch.object(config, "READ_WAIT_SEC", 0, create=True),
        mock.patch.object(config, "REPLACE_WAIT_SEC", 0, create=True),
    ]

    class _Both:
        def __enter__(self):
            for p in patches:
                p.start()

        def __exit__(self, *exc):
            for p in reversed(patches):
                p.stop()
            return False

    return _Both()


class _Terminal(unittest.TestCase):
    """この端末の設定ファイルを一時フォルダに置く(担当ライン・接続先・管理者パスワードを決めた端末)。"""

    ENV = ("KANBAN_CONFIG", "KANBAN_SETTINGS_DIR", "KANBAN_DISTRIBUTION_DIR",
           "ALLTOOLS_DISTRIBUTION_DIR", config.ADMIN_PASSWORD_ENV)

    def setUp(self) -> None:
        super().setUp()
        self.dir = Path(tempfile.mkdtemp(prefix="kanban_unreadable_"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for key in self.ENV:
            os.environ.pop(key, None)
        self.pc = self.dir / "appdata" / "config.json"
        os.environ["KANBAN_CONFIG"] = str(self.pc)
        os.environ["KANBAN_SETTINGS_DIR"] = str(self.dir / "旧アプリ" / "data")
        os.environ["KANBAN_DISTRIBUTION_DIR"] = str(self.dir / "app" / "配布設定")
        no = no_wait()
        no.__enter__()
        self.addCleanup(no.__exit__, None, None, None)
        self.original = {
            config.LOCAL_FORMAT_KEY: config.LOCAL_FORMAT,
            "shared_db_path": r"\\server\この端末の\看板マスタ.sqlite3",
            "line": "AIM",
            "admin_password_hash": config.hash_password(DECIDED_PASSWORD),
            "_配布設定を読み込んだ日時": "2026/10/01 08:00:00",
        }
        self.write(self.original)

    def write(self, values: dict) -> None:
        self.pc.parent.mkdir(parents=True, exist_ok=True)
        self.pc.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")

    def stored(self) -> dict:
        return json.loads(self.pc.read_text(encoding="utf-8"))

    def set_aside(self) -> list[str]:
        return sorted(p.name for p in self.pc.parent.iterdir() if "読めなかった" in p.name)

    def leftovers(self) -> list[str]:
        return sorted(p.name for p in self.pc.parent.iterdir() if p.name.endswith(".tmp"))

    def assertUntouched(self) -> None:
        self.assertEqual(self.stored(), self.original, "この端末の設定が書き換わった")
        self.assertEqual(self.set_aside(), [], "読めただけのファイルを「読めなかった」として脇へ移した")


# ---------------------------------------------------------------------------
# 読む
# ---------------------------------------------------------------------------
class ReadTest(_Terminal):
    def test_あるのに読めないときは_無い_とも_壊れている_とも答えない(self) -> None:
        with refuse_reading(self.pc):
            with self.assertRaises(OSError):
                config._read_raw_local(self.pc)

    def test_一瞬だけ読めないなら_読み直して決めた値で動く(self) -> None:
        with refuse_reading(self.pc, times=2):
            cfg = config.load_config()
        self.assertEqual((cfg.line, cfg.shared_db_path), ("AIM", self.original["shared_db_path"]))

    def test_起動時に読めなくても止めずに既定で動く(self) -> None:
        with refuse_reading(self.pc):
            cfg = config.load_config()
        self.assertEqual(cfg.line, "")

    def test_設定ファイルの確認は_読めないのを_JSONの形が違う_と言わない(self) -> None:
        """起動の記録(``start_app._log_settings``)に出る理由。直し方がまったく違う。"""
        with refuse_reading(self.pc):
            status = config.read_settings_file()
        self.assertTrue(status.error)
        self.assertNotIn("JSON の形", status.error)

    def test_中身がJSONとして壊れているときは今までどおり(self) -> None:
        """読めない形のファイルは既定で動き、保存すると脇へ移して残す(直せば取り戻せる)。"""
        self.pc.write_text('{"line": "AIM" "x": 1}', encoding="utf-8")
        cfg = config.load_config()
        self.assertEqual(cfg.line, "")
        cfg.line = "LVC"
        config.save_config(cfg)
        self.assertEqual(len(self.set_aside()), 1)
        self.assertEqual(self.stored()["line"], "LVC")


# ---------------------------------------------------------------------------
# 書く
# ---------------------------------------------------------------------------
class WriteTest(_Terminal):
    def test_付記を書くとき読めなければ書かず_元のファイルも脇へ移さない(self) -> None:
        """以前は付記1つだけのほぼ空の設定ファイルに置き換わっていた。"""
        with refuse_reading(self.pc):
            with self.assertRaises(OSError):
                config.write_meta("_配布設定を読み込んだ日時", "2026/10/29 09:00:00")
        self.assertUntouched()

    def test_起動時に読めなかった設定は_読めるようになっても書き戻さない(self) -> None:
        """既定の値(担当ライン無し・接続先無し・パスワード無し)で上書きしない。"""
        with refuse_reading(self.pc):
            cfg = config.load_config()
        cfg.auto_print = True
        with self.assertRaises(OSError):
            config.save_config(cfg)
        self.assertUntouched()

    def test_保存するとき読めなければ断る(self) -> None:
        cfg = config.load_config()
        cfg.auto_print = True
        with refuse_reading(self.pc):
            with self.assertRaises(OSError):
                config.save_config(cfg)
        self.assertUntouched()

    def test_読めた設定は今までどおり保存できる(self) -> None:
        cfg = config.load_config()
        cfg.auto_print = True
        config.save_config(cfg)
        saved = self.stored()
        self.assertTrue(saved["auto_print"])
        self.assertEqual(saved["line"], "AIM")
        self.assertEqual(saved["_配布設定を読み込んだ日時"], "2026/10/01 08:00:00")

    def test_置き換えが一瞬断られたら_やり直して保存する(self) -> None:
        """Windows で、ほかの処理が開いている瞬間に置き換えると WinError 5 になる。"""
        real = os.replace
        left = {"n": 3}

        def replace(src, dst):
            if Path(dst) == self.pc and left["n"] > 0:
                left["n"] -= 1
                raise PermissionError(13, "アクセスが拒否されました", str(dst))
            return real(src, dst)

        cfg = config.load_config()
        cfg.auto_print = True
        with mock.patch("os.replace", replace):
            config.save_config(cfg)
        self.assertTrue(self.stored()["auto_print"])
        self.assertEqual(self.leftovers(), [])

    def test_置き換えがずっと断られたら諦めて_一時ファイルを残さない(self) -> None:
        real = os.replace

        def replace(src, dst):
            if Path(dst) == self.pc:
                raise PermissionError(13, "アクセスが拒否されました", str(dst))
            return real(src, dst)

        cfg = config.load_config()
        cfg.auto_print = True
        with mock.patch("os.replace", replace):
            with self.assertRaises(PermissionError):
                config.save_config(cfg)
        self.assertEqual(self.leftovers(), [])
        self.assertUntouched()


class MigrateTest(_Terminal):
    """アプリのフォルダの ``data\\config.json``(一時期の版)からの引き継ぎ。"""

    def setUp(self) -> None:
        super().setUp()
        self.legacy = config.legacy_app_settings_path()
        self.legacy.parent.mkdir(parents=True, exist_ok=True)
        self.legacy.write_text(json.dumps({"csv_dir": r"\\server\CSV"}), encoding="utf-8")

    def test_引き継ぎ元を読めないときは_引き継いだ印を付けない(self) -> None:
        """印を付けると、読めるようになっても二度と引き継がない(値が消えたままになる)。"""
        with refuse_reading(self.legacy):
            config.load_config()
        self.assertNotIn(config._MIGRATED_FROM, self.stored())
        self.assertEqual(config.load_config().csv_dir, r"\\server\CSV")

    def test_この端末の設定を読めないときは_引き継ぎで書かない(self) -> None:
        with refuse_reading(self.pc):
            config.load_config()
        self.assertUntouched()


# ---------------------------------------------------------------------------
# 配布設定
# ---------------------------------------------------------------------------
class DistributionTest(_Terminal):
    def setUp(self) -> None:
        super().setUp()
        self.folder = distribution.directory()
        self.folder.mkdir(parents=True)
        self.bundle = {
            "format": distribution.FORMAT, "created_at": "2026/10/20 08:00:00", "created_on": "配る元",
            "settings": {"shared_db_path": r"\\server\配った\看板マスタ.sqlite3",
                         "admin_password_hash": config.hash_password("配った値"),
                         "line": "LVC"},
        }
        (self.folder / distribution.SETTINGS_NAME).write_text(
            json.dumps(self.bundle, ensure_ascii=False), encoding="utf-8")

    def test_起動時の読み込みは_この端末の設定を読めなければ何も書かない(self) -> None:
        """以前は「この端末に無い」とみなして配布設定の値で埋め、担当ライン・パスワードを消していた。"""
        with refuse_reading(self.pc):
            result = distribution.apply_on_start()
        self.assertFalse(result.ok)
        self.assertEqual(result.applied, [])
        self.assertUntouched()

    def test_読み込み直しも_この端末の設定を読めなければ断る(self) -> None:
        with refuse_reading(self.pc):
            result = distribution.reapply()
        self.assertFalse(result.ok)
        self.assertIn("読め", result.message)
        self.assertUntouched()

    def test_書き出しは_読めないのを_既定のまま_と答えない(self) -> None:
        """大設定は「既定のままなので要りません」(nothing)を信じて、その分を入れない。"""
        before = (self.folder / distribution.SETTINGS_NAME).read_text(encoding="utf-8")
        with refuse_reading(self.pc):
            result = distribution.export([k for k, _, d in distribution.ITEMS if d], keep_previous=True)
        self.assertFalse(result.ok)
        self.assertNotEqual(result.reason, distribution.REFUSE_NOTHING)
        self.assertEqual((self.folder / distribution.SETTINGS_NAME).read_text(encoding="utf-8"), before)

    def test_設定画面の一覧は_読めないのを_既定のまま_と出さない(self) -> None:
        with refuse_reading(self.pc):
            summary = distribution.summary()
        self.assertTrue(all(it["here"] for it in summary["items"]),
                        "読めないのに「既定のまま」と出している")

    def test_まとめて書き出すとき_前の配布設定を読めなければ_前に入れたラインを消さない(self) -> None:
        """前の 設定.json が読めないのを「前に入れていない」と取り違えると、担当ラインが消える。"""
        target = self.folder / distribution.SETTINGS_NAME
        with refuse_reading(target):
            distribution.export([k for k, _, d in distribution.ITEMS if d], keep_previous=True)
        after = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(after["settings"].get("line"), "LVC")


# ---------------------------------------------------------------------------
# 設定画面(管理者パスワード・保存)
# ---------------------------------------------------------------------------
@unittest.skipUnless(HAS_FLASK, "Flask が入っていません")
class RouteTest(RouteTestBase):
    """設定画面の経路。試験の土台では、決めていない端末で通る値を環境変数で「秘密」にしている。"""

    def setUp(self) -> None:
        super().setUp()
        no = no_wait()
        no.__enter__()
        self.addCleanup(no.__exit__, None, None, None)
        self.pc = Path(self.app.config["CONFIG_PATH"])
        self.original = {
            config.LOCAL_FORMAT_KEY: config.LOCAL_FORMAT,
            "line": "LVC",
            "csv_dir": str(self.dir),
            "admin_password_hash": config.hash_password(DECIDED_PASSWORD),
        }
        self.pc.write_text(json.dumps(self.original, ensure_ascii=False), encoding="utf-8")

    def stored(self) -> dict:
        return json.loads(self.pc.read_text(encoding="utf-8"))

    def test_読めないときは_決めていない端末の値で通さない(self) -> None:
        """この端末はパスワードを決めてある。読めないからといって、決めていない端末の値で開かない。"""
        with refuse_reading(self.pc):
            res = self.post("/api/admin/unlock", {"password": TEST_ADMIN_PASSWORD})
        self.assertNotEqual(res.status_code, 200)
        self.assertIn("読め", res.json["error"]["message"])

    def test_読めないときは_決めた値でも開けない_読めれば開ける(self) -> None:
        with refuse_reading(self.pc):
            res = self.post("/api/admin/verify", {"password": DECIDED_PASSWORD})
        self.assertNotEqual(res.status_code, 200)
        self.assertEqual(self.post("/api/admin/verify", {"password": DECIDED_PASSWORD}).status_code, 200)

    def test_パスワードを変えるときも_読めなければ既定の値で通さない(self) -> None:
        with refuse_reading(self.pc):
            res = self.post("/api/admin/password", {
                "current_password": TEST_ADMIN_PASSWORD,
                "new_password": "新しい値", "new_password_confirm": "新しい値"})
        self.assertNotEqual(res.status_code, 200)
        self.assertEqual(self.stored(), self.original)

    def test_認証済みでも_読めなければ保存を断り_理由を出す(self) -> None:
        from kanban import admin_lock

        admin_lock.get().unlock()
        with refuse_reading(self.pc):
            res = self.post("/api/behavior", {"values": {
                "import_interval_sec": "30", "export_interval_sec": "60", "auto_print": True}})
        self.assertNotEqual(res.status_code, 200)
        self.assertIn("読め", res.json["error"]["message"])
        self.assertEqual(self.stored(), self.original)

    def test_設定画面は_読めないとき_並んでいる値がこの端末の設定ではないと出す(self) -> None:
        """読めないあいだは既定の値が並ぶ(担当ライン未設定・既定のパスワード)。それを信じさせない。"""
        with refuse_reading(self.pc):
            page = self.get("/settings").get_data(as_text=True)
        self.assertIn("この端末の設定ではありません", page)
        self.assertNotIn("この端末の設定ではありません", self.get("/settings").get_data(as_text=True))

    def test_担当ラインの切り替えも_読めなければ断る(self) -> None:
        """``_switch_line`` は保存の失敗を受け止めていなかった(想定外のエラーとして 500)。"""
        from kanban import admin_lock

        admin_lock.get().unlock()
        with refuse_reading(self.pc):
            res = self.post("/api/line", {"line": "LS"})
        self.assertNotEqual(res.status_code, 200)
        self.assertNotEqual(res.json["error"]["code"], "internal")
        self.assertIn("読め", res.json["error"]["message"])
        self.assertEqual(self.stored(), self.original)


if __name__ == "__main__":
    unittest.main()
