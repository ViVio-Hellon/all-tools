"""配布設定 ── ツールの直下の `配布設定\\` を、配った先の端末が読み込む

    1. 一度起動して配布先の設定をする(設定画面の「配布設定」で書き出す)
    2. `配布設定\\` が作られ、配下に必要なものが入る(start.bat と同じ階層)
    3. 配った先は `配布設定\\` があれば読み込む
    4. その端末にすでにあるもの(値が入っている設定)は読み込まない
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from . import _web
from calendar_app import (
    admin_password,
    app_config,
    config,
    distribution,
    settings as user_settings,
)

_ROOT = Path(__file__).resolve().parent.parent

PW = config.ADMIN_PASSWORD
NEW_PW = "newpass123"
SHARE = r"\\srv\共有\カレンダー"


class Terminal:
    """1台ぶんの設定ファイルの置き場。"""

    def __init__(self, case: unittest.TestCase, root: Path, name: str) -> None:
        self.dir = root / name
        self.dir.mkdir()
        self.case = case

    def use(self) -> None:
        path = self.dir / "settings.json"
        patcher = mock.patch.object(config, "settings_path", lambda: path)
        patcher.start()
        self.case.addCleanup(patcher.stop)


class DistributionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patcher = mock.patch.object(distribution, "DIR", self.root / "tool" / "配布設定")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.source = Terminal(self, self.root, "source")
        self.dest = Terminal(self, self.root, "dest")

    def configure_source(self) -> None:
        self.source.use()
        user_settings.set_value(user_settings.KEY_DATA_DB_DIR, SHARE)
        user_settings.set_value(user_settings.KEY_AUTO_SYNC, False)
        user_settings.set_value(user_settings.KEY_SYNC_INTERVAL, 30)
        user_settings.save_my_line("コイル")
        admin_password.change(PW, NEW_PW, NEW_PW)

    def export(self, items=None, password=NEW_PW):
        items = [k for k, _, default in distribution.ITEMS if default] \
            if items is None else items
        return distribution.export(password, list(items))

    def written(self) -> dict:
        return json.loads(distribution.settings_path().read_text(encoding="utf-8"))


class ExportTests(DistributionTestCase):
    def test_パスワードが無ければ作らない(self) -> None:
        self.configure_source()
        self.assertEqual(self.export(password="").reason,
                         distribution.REFUSE_NEED_PASSWORD)
        # 打ったものが違うときは「違う」と言う(同じ問いを繰り返させない)
        self.assertEqual(self.export(password="ちがう").reason,
                         distribution.REFUSE_WRONG_PASSWORD)
        self.assertFalse(distribution.DIR.exists())

    def test_配布設定フォルダが作られ配下に入る(self) -> None:
        self.configure_source()
        self.assertTrue(self.export().ok)
        names = {p.relative_to(distribution.DIR).as_posix()
                 for p in distribution.DIR.rglob("*") if p.is_file()}
        self.assertEqual(names, {"設定.json", "はじめに読む.txt"})
        self.assertFalse(distribution.DIR.with_name("配布設定.作成中").exists())
        # メモ帳で化けないよう BOM 付き
        self.assertTrue((distribution.DIR / distribution.README_NAME)
                        .read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_選んだものだけ_ラインは既定で入れない(self) -> None:
        self.configure_source()
        self.export()
        written = self.written()
        settings = written["settings"]
        self.assertEqual(settings[user_settings.KEY_DATA_DB_DIR], SHARE)
        self.assertIs(settings[user_settings.KEY_AUTO_SYNC], False)
        self.assertEqual(settings[user_settings.KEY_SYNC_INTERVAL], 30)
        self.assertNotIn(user_settings.KEY_MY_LINE, settings)
        self.assertEqual(written["version"], app_config.version())
        # パスワードは撹拌した値だけ。平文はどこにも入らない
        for path in distribution.DIR.rglob("*"):
            if path.is_file():
                self.assertNotIn(NEW_PW, path.read_text(encoding="utf-8-sig"), path)
        self.assertIn(admin_password.KEY, settings)

    def test_ラインも選べば入る(self) -> None:
        self.configure_source()
        self.export(items=[user_settings.KEY_MY_LINE])
        self.assertEqual(self.written()["settings"], {user_settings.KEY_MY_LINE: "コイル"})

    def test_設定していない項目は入れない(self) -> None:
        """配った先の既定値を「空」で上書きしない。"""
        self.configure_source()
        result = self.export()
        self.assertNotIn(user_settings.KEY_MASTER_DB_DIR, self.written()["settings"])
        self.assertIn("既定のまま", result.message)

    def test_既定に戻した項目も入れない(self) -> None:
        """この端末では、既定に戻すと空文字で残る。空を配らない。"""
        self.configure_source()
        user_settings.set_value(user_settings.KEY_DATA_DB_DIR, "")
        self.export()
        self.assertNotIn(user_settings.KEY_DATA_DB_DIR, self.written()["settings"])

    def test_書き出し直すと前の中身は置き換わる(self) -> None:
        self.configure_source()
        self.export()
        self.export(items=[user_settings.KEY_DATA_DB_DIR])
        self.assertEqual(list(self.written()["settings"]), [user_settings.KEY_DATA_DB_DIR])

    def test_画面にパスワードの値を出さない(self) -> None:
        self.configure_source()
        self.export()
        text = json.dumps(distribution.summary(), ensure_ascii=False)
        self.assertNotIn(user_settings.get(admin_password.KEY), text)
        self.assertIn("(設定済み)", text)

    def test_知らない項目は断る(self) -> None:
        self.configure_source()
        self.assertEqual(self.export(items=["謎"]).reason, distribution.REFUSE_BAD_INPUT)

    def test_消すにもパスワード(self) -> None:
        self.configure_source()
        self.export()
        self.assertFalse(distribution.remove("ちがう").ok)
        self.assertTrue(distribution.DIR.exists())
        self.assertTrue(distribution.remove(NEW_PW).ok)
        self.assertFalse(distribution.DIR.exists())

    def test_ドライブ文字の置き場所は注意する(self) -> None:
        """Z: の割り当ては PC ごとに違うことがある。配った先で見つからない。"""
        self.configure_source()
        user_settings.set_value(user_settings.KEY_DATA_DB_DIR, r"Z:\共有\カレンダー")
        result = self.export()
        self.assertTrue(result.ok)
        self.assertIn("ドライブ文字", result.message)
        self.assertIn("保存用DBの置き場所", result.message)

    def test_UNCなら注意しない(self) -> None:
        self.configure_source()
        self.assertNotIn("ドライブ文字", self.export().message)

    def test_書けなければ理由を返す(self) -> None:
        self.configure_source()
        blocker = self.root / "ファイル"
        blocker.write_text("x", encoding="utf-8")
        distribution.DIR = blocker / "配布設定"
        result = self.export()
        self.assertEqual(result.reason, distribution.REFUSE_FAILED)
        self.assertIn("書けませんでした", result.message)


class ApplyTests(DistributionTestCase):
    def distributed(self, **kwargs) -> None:
        self.configure_source()
        self.assertTrue(self.export(**kwargs).ok)
        self.dest.use()

    def test_配布設定フォルダがあれば読み込む(self) -> None:
        self.distributed()
        result = distribution.apply_on_start()
        self.assertIn("保存用DBの置き場所", result.applied)
        self.assertEqual(user_settings.data_db_dir_setting(), SHARE)
        self.assertIs(user_settings.auto_sync_enabled(), False)
        self.assertEqual(user_settings.sync_interval(), 30)
        self.assertTrue(admin_password.verify(NEW_PW))
        self.assertEqual(user_settings.get_my_line(), "")
        self.assertTrue(distribution.summary()["applied_at"])

    def test_既存データがある項目は読み込まない(self) -> None:
        self.distributed()
        user_settings.set_value(user_settings.KEY_DATA_DB_DIR, r"\\この端末\共有")
        result = distribution.apply_on_start()
        self.assertEqual(user_settings.data_db_dir_setting(), r"\\この端末\共有")
        self.assertIn("保存用DBの置き場所", result.kept)
        # 無い項目は埋める
        self.assertEqual(user_settings.sync_interval(), 30)

    def test_空の項目は既存と見なさない(self) -> None:
        """既定に戻した(空文字の)項目は、配布設定で埋める。"""
        self.distributed()
        user_settings.set_value(user_settings.KEY_DATA_DB_DIR, "")
        distribution.apply_on_start()
        self.assertEqual(user_settings.data_db_dir_setting(), SHARE)

    def test_起動のたびに見ても端末で直した値は戻さない(self) -> None:
        self.distributed()
        distribution.apply_on_start()
        user_settings.set_auto_sync(True)                  # 端末で直した
        self.assertEqual(distribution.apply_on_start().applied, [])
        self.assertIs(user_settings.auto_sync_enabled(), True)

    def test_読み込み直しはパスワードで上書き(self) -> None:
        self.distributed()
        user_settings.set_value(user_settings.KEY_DATA_DB_DIR, r"\\この端末\共有")
        # 照合するのは**この端末の**パスワード(まだ読み込んでいないので既定)
        self.assertFalse(distribution.reapply("ちがう").ok)
        self.assertTrue(distribution.reapply(PW).ok)
        self.assertEqual(user_settings.data_db_dir_setting(), SHARE)

    def test_無い_壊れた_形が違うなら何もしない(self) -> None:
        self.dest.use()
        self.assertEqual(distribution.apply_on_start().applied, [])
        distribution.DIR.mkdir(parents=True)
        distribution.settings_path().write_text("{壊れ", encoding="utf-8")
        self.assertEqual(distribution.apply_on_start().applied, [])
        distribution.settings_path().write_text(json.dumps({"format": 99, "settings": {
            user_settings.KEY_DATA_DB_DIR: "x"}}), encoding="utf-8")
        self.assertEqual(distribution.apply_on_start().applied, [])
        self.assertEqual(user_settings.data_db_dir_setting(), "")

    def test_知らない鍵は入れない(self) -> None:
        self.dest.use()
        distribution.DIR.mkdir(parents=True)
        distribution.settings_path().write_text(json.dumps({"format": 1, "settings": {
            "謎の鍵": 1, user_settings.KEY_DATA_DB_DIR: r"\\x"}}), encoding="utf-8")
        distribution.apply_on_start()
        self.assertNotIn("謎の鍵", user_settings.load())
        self.assertEqual(user_settings.data_db_dir_setting(), r"\\x")

    def test_平文で書かれていても端末には撹拌して持つ(self) -> None:
        self.dest.use()
        distribution.DIR.mkdir(parents=True)
        distribution.settings_path().write_text(json.dumps({"format": 1, "settings": {
            admin_password.KEY: "手書きの合言葉"}}, ensure_ascii=False), encoding="utf-8")
        distribution.apply_on_start()
        self.assertTrue(user_settings.get(admin_password.KEY).startswith("pbkdf2$"))
        self.assertTrue(admin_password.verify("手書きの合言葉"))

    def test_配布設定で決めたパスワードの断りは_README_を指さない(self) -> None:
        """配った人が変えたのに README の初期値を案内すると、行き止まりになる。"""
        self.distributed()
        distribution.apply_on_start()
        message = admin_password.guard(["line"], "でたらめ").message
        self.assertNotIn("README", message)
        self.assertIn("配布", message)


class FreshTerminalTests(DistributionTestCase):
    """**まっさらな端末で、起動と同じ順に動かして、使える状態になるか。**

    配布設定を読む → 同期を始める → 取り込み元から受け取る、までを通す。
    班員名簿はマスタDBにあるので、ここで入らないと休みの作業者を選べない
    (実際に0件のまま始まっていた)。
    """

    def test_読み込んだあとの最初の同期で_休みも名簿も入る(self) -> None:
        import sqlite3

        from calendar_app import db, sources, sync_service
        from calendar_app.repository import Repository

        share = self.root / "共有"
        share.mkdir()
        data = _web.make_source(share, config.SOURCE_FILE_DATA, _web.SOURCE_SCHEMA)
        master = _web.make_source(share, config.SOURCE_FILE_MASTER, _web.MASTER_SCHEMA)
        conn = sqlite3.connect(master)
        conn.execute(f'INSERT INTO "{config.TABLE_MEMBER}" VALUES (?,?,?,?,?,?)',
                     ("10", "山田", "A", "山田太郎", "ヤマダ", "コイル"))
        conn.commit()
        conn.close()
        conn = sqlite3.connect(data)
        conn.execute(f'INSERT INTO "{config.TABLE_DATA}" ("日付","区分","登録内容",'
                     '"識別コード","直","班","ライン") VALUES (?,?,?,?,?,?,?)',
                     ("2026/09/24", config.KUBUN_REST, "山田太郎", "10", "1", "A", "コイル"))
        conn.commit()
        conn.close()

        # 配る元: 参照パスだけ決めて書き出す(マスタDBは同じフォルダ = 空のまま)
        self.source.use()
        user_settings.set_value(user_settings.KEY_DATA_DB_DIR, str(share))
        self.assertTrue(distribution.export(PW, [user_settings.KEY_DATA_DB_DIR]).ok)

        # 配った先: 何も無い端末
        self.dest.use()
        _web.reset_sync()
        self.addCleanup(_web.reset_sync)
        self.assertEqual(distribution.apply_on_start().applied, ["保存用DBの置き場所"])
        self.assertEqual(sources.find_master_db(), master)

        local = self.root / "dest" / "calendar.db"
        with mock.patch.object(config, "sqlite_path", lambda: local):
            service = sync_service.get_service()
            service.reload()
            service.sync_now(receive=True)
        conn = db.connect(str(local))
        self.addCleanup(conn.close)
        repo = Repository(conn)
        self.assertEqual(repo.member_count(), 1, "班員名簿が入っていない")
        import datetime as _dt
        self.assertEqual(len(repo.get_day_records(_dt.date(2026, 9, 24))), 1)


class StartupTests(unittest.TestCase):
    def test_同期より先に読む(self) -> None:
        """置き場所が入っているので、読む前に同期を始めると既定の場所を見る。"""
        text = (_ROOT / "start_app.py").read_text(encoding="utf-8")
        body = text[text.index("def _initialize"):]
        self.assertLess(body.index("distribution.apply_on_start()"),
                        body.index("terminals.apply_reservation()"))
        self.assertLess(body.index("distribution.apply_on_start()"),
                        body.index("sync_service.get_service().start()"))

    def test_配布設定は追跡しない(self) -> None:
        done = subprocess.run(["git", "check-ignore", "-q", "配布設定/設定.json"],
                              cwd=_ROOT)
        self.assertEqual(done.returncode, 0, "配布設定 フォルダが .gitignore に無い")

    def test_置き場所はツールの直下_startbatと同じ階層(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "CALENDAR_DISTRIBUTION_DIR"}
        done = subprocess.run(
            [sys.executable, "-c",
             "from calendar_app import distribution; print(distribution.DIR)"],
            cwd=_ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
            timeout=60)
        self.assertEqual(Path(done.stdout.strip()), _ROOT / "配布設定")
        self.assertTrue((_ROOT / "start.bat").is_file())

    def test_コピーした先で起動すると読み込む(self) -> None:
        """フォルダごと配った先で、実際の起動と同じ入口を通る。"""
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "配った先"
            shutil.copytree(_ROOT, app, ignore=shutil.ignore_patterns(
                "__pycache__", ".git", "tests", "docs", "配布設定"))
            (app / "配布設定").mkdir()
            (app / "配布設定" / "設定.json").write_text(json.dumps({
                "format": 1, "settings": {
                    user_settings.KEY_DATA_DB_DIR: SHARE,
                    admin_password.KEY: admin_password.hash_for_distribution(NEW_PW)},
            }, ensure_ascii=False), encoding="utf-8")
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith("CALENDAR_")}
            env["CALENDAR_LOCAL_DIR"] = str(Path(tmp) / "local")
            env["CALENDAR_HOME"] = str(Path(tmp) / "home")
            script = ("from calendar_app import distribution, settings, admin_password;"
                      "distribution.apply_on_start();"
                      "print(settings.data_db_dir_setting());"
                      f"print(admin_password.verify({NEW_PW!r}))")
            done = subprocess.run([sys.executable, "-c", script], cwd=app, env=env,
                                  capture_output=True, text=True, encoding="utf-8",
                                  timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(done.stdout.split("\n")[:2], [SHARE, "True"])


class WebTests(DistributionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.configure_source()
        _web.reset_sync()
        self.addCleanup(_web.reset_sync)
        _web.bind_db(self)
        self.client = _web.make_client()

    def post(self, path, body):
        return self.client.post(path, json=body, headers=_web.auth())

    def test_パスワードが違えば403(self) -> None:
        res = self.post("/api/settings/distribution/export",
                        {"items": [user_settings.KEY_DATA_DB_DIR], "password": "x"})
        self.assertEqual(res.status_code, 403)

    def test_書き出すと状態に出る(self) -> None:
        res = self.post("/api/settings/distribution/export",
                        {"items": [user_settings.KEY_DATA_DB_DIR], "password": NEW_PW})
        self.assertEqual(res.status_code, 200, res.get_json())
        dist = res.get_json()["distribution"]
        self.assertTrue(dist["exists"])
        self.assertEqual(dist["contents"], [
            {"label": "保存用DBの置き場所", "value": SHARE}])
        self.assertIn("配布設定", res.get_json()["message"])

    def test_形が違えば400(self) -> None:
        res = self.post("/api/settings/distribution/export",
                        {"items": "data", "password": NEW_PW})
        self.assertEqual(res.status_code, 400)

    def test_読み込み直しと消す(self) -> None:
        self.post("/api/settings/distribution/export",
                  {"items": [user_settings.KEY_DATA_DB_DIR], "password": NEW_PW})
        user_settings.set_value(user_settings.KEY_DATA_DB_DIR, r"\\別")
        res = self.post("/api/settings/distribution/reapply", {"password": NEW_PW})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(res.get_json()["data_db_dir"], SHARE)
        res = self.post("/api/settings/distribution/remove", {"password": NEW_PW})
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.get_json()["distribution"]["exists"])

    def test_画面に項目の一覧が渡る(self) -> None:
        body = self.client.get("/api/settings", headers=_web.auth()).get_json()
        keys = {it["key"]: it["default"] for it in body["distribution"]["items"]}
        self.assertIs(keys[user_settings.KEY_MY_LINE], False)
        self.assertIs(keys[user_settings.KEY_DATA_DB_DIR], True)


class NoWritesInAppFolderTests(unittest.TestCase):
    """**アプリのフォルダに何も書かない**(基盤仕様書 2.7)。

    共有に置いて配ると、起動した端末の数だけ共有へ書きに行く。
    ``__pycache__`` もその1つだった。
    """

    def test_起動しても_pycache_を作らない(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "app"
            shutil.copytree(_ROOT, app, ignore=shutil.ignore_patterns(
                "__pycache__", ".git", "tests", "docs"))
            env = {k: v for k, v in os.environ.items()
                   if k != "PYTHONPYCACHEPREFIX"}
            env["CALENDAR_LOCAL_DIR"] = str(Path(tmp) / "local")
            env.pop("PYTHONDONTWRITEBYTECODE", None)
            subprocess.run([sys.executable, "start_app.py", "--check"],
                           cwd=app, env=env, capture_output=True, timeout=60)
            left = [p.relative_to(app) for p in app.rglob("__pycache__")]
            self.assertEqual(left, [], "アプリのフォルダに写しが残っている")
            self.assertTrue(any((Path(tmp) / "local" / "pycache").rglob("*.pyc")),
                            "写しがローカル領域にも無い(向け先がずれている)")


if __name__ == "__main__":
    unittest.main()
