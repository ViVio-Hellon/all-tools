"""配布設定 ── 起動用の Start.vbs と同じフォルダの `配布設定\\` を、配った先の端末が読み込む

    1. 一度起動して配布先の設定をする(設定画面の「配布設定」で書き出す)
    2. `配布設定\\` が作られ、配下に必要なものが入る
    3. 配った先は `配布設定\\` があれば読み込む
    4. その端末にすでにあるもの(設定・音の置き場所)は読み込まない

python-web-tools の `tests/test_distribution.py` と同じ組み立てにしてあります
(2台の端末を行き来する)。こちらに足したもの:
    ・空の値は「無い」と数える(「既定に戻す」の空で共有へ届かなくならない)
    ・音声ファイルは端末の中へ写す(あとで `配布設定\\` を消しても鳴る)
    ・この端末のラインは選べば入れられる/決めていなければ日報入力で知らせる
    ・前の版の置き場所(`配布先\\` / `config\\site.json`)も読む
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402

PW = "nisk"
NEW_PW = "newpass123"


class Terminal:
    """1台ぶんの設定ファイルと、ローカル領域。"""

    def __init__(self, case: unittest.TestCase, root: Path, name: str) -> None:
        self.dir = root / name
        self.dir.mkdir()
        self.case = case

    def use(self) -> None:
        os.environ["NIPPOU_APP_DIR"] = str(self.dir / "app")
        os.environ["NIPPOU_LOCAL_DIR"] = str(self.dir / "local")


class DistributionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        keys = ("NIPPOU_APP_DIR", "NIPPOU_LOCAL_DIR", "NIPPOU_DIST_DIR",
                "NIPPOU_GW_REF_DIR", "NIPPOU_SOUND_DIR")
        saved = {k: os.environ.get(k) for k in keys}

        def restore() -> None:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.addCleanup(restore)
        for key in ("NIPPOU_GW_REF_DIR", "NIPPOU_SOUND_DIR"):
            os.environ.pop(key, None)
        os.environ["NIPPOU_DIST_DIR"] = str(self.root / "tool" / "配布設定")
        self.source = Terminal(self, self.root, "source")
        self.dest = Terminal(self, self.root, "dest")
        self.sounds = self.root / "共有" / "音"
        self.sounds.mkdir(parents=True)

    # -- 道具 --------------------------------------------------------
    def configure_source(self) -> None:
        from nippou import admin_password, config, user_settings
        self.source.use()
        user_settings.save(config.KEY_ACCESS_DIR, r"\\srv\日報")
        user_settings.save(config.KEY_REFERENCE_DIR, r"\\srv\参照")
        user_settings.save(config.KEY_REPORT_OUT_DIR, r"\\srv\出力")
        user_settings.save(config.KEY_SOUND_DIR, str(self.sounds))
        user_settings.save(config.KEY_TERMINAL_LINE, "LVC")
        self.assertTrue(admin_password.change(PW, NEW_PW, NEW_PW).ok)

    def export(self, items=None, files=(), password=NEW_PW):
        from nippou import distribution
        items = [i.key for i in distribution.ITEMS if i.default] \
            if items is None else items
        return distribution.export(password, list(items), list(files))

    def written(self) -> dict:
        from nippou import distribution
        return json.loads(distribution.settings_path().read_text(encoding="utf-8"))

    def make_sounds(self) -> None:
        from nippou.logic.sound import SOUNDS
        for spec in SOUNDS:
            if spec.default_file:         # v4.4.0 で足した出来事は既定で鳴らさない
                (self.sounds / spec.default_file).write_bytes(b"RIFF" + spec.key.encode())


class PlaceTests(unittest.TestCase):
    """**起動用の bat と同じ階層**に作る。"""

    def test_置き場所は起動用のファイルと同じフォルダ(self) -> None:
        from nippou import distribution
        with mock.patch.dict(os.environ, {"NIPPOU_DIST_DIR": ""}):
            place = distribution.folder()
        self.assertEqual(place.name, "配布設定")
        for name in ("start.bat", "Start.vbs"):
            self.assertTrue((place.parent / name).is_file(), name)
        self.assertEqual(place.parent, distribution.launch_dir())

    def test_配布設定は追跡しない(self) -> None:
        self.assertIn("/配布設定/", (_ROOT / ".gitignore").read_text(encoding="utf-8"))

    def test_起動で直の時間より先に読む(self) -> None:
        """参照用マスタの置き場所が配布設定から来るので、後にすると既定を見る。"""
        text = (_ROOT / "start_app.py").read_text(encoding="utf-8")
        self.assertIn("    _apply_distribution(srv)\n    _read_shift_times(srv)", text)


class ExportTests(DistributionTestCase):
    def test_パスワードが無ければ作らない(self) -> None:
        from nippou import distribution
        self.configure_source()
        result = self.export(password="ちがう")
        self.assertEqual(result.reason, distribution.REFUSE_NEED_PASSWORD)
        self.assertFalse(distribution.folder().exists())

    def test_配布設定フォルダが作られ配下に入る(self) -> None:
        from nippou import distribution
        self.configure_source()
        self.make_sounds()
        self.assertTrue(self.export(files=["sounds"]).ok)
        names = {p.relative_to(distribution.folder()).as_posix()
                 for p in distribution.folder().rglob("*") if p.is_file()}
        self.assertIn("設定.json", names)
        self.assertIn("はじめに読む.txt", names)
        self.assertTrue(any(n.startswith("音/") for n in names), names)
        self.assertFalse(distribution.folder().with_name("配布設定.作成中").exists())

    def test_選んだものだけ_ラインは既定で入れない(self) -> None:
        from nippou import config, distribution
        self.configure_source()
        self.export()
        data = self.written()
        self.assertEqual(data["format"], distribution.FORMAT)
        self.assertTrue(data["created_at"])
        settings = data["settings"]
        self.assertEqual(settings[config.KEY_ACCESS_DIR], r"\\srv\日報")
        self.assertNotIn(config.KEY_TERMINAL_LINE, settings)
        # パスワードは撹拌した値だけ。平文はどこにも入らない
        for path in distribution.folder().rglob("*"):
            if path.is_file():
                self.assertNotIn(NEW_PW, path.read_text(encoding="utf-8-sig",
                                                        errors="ignore"), path)
        self.assertIn(config.KEY_ADMIN_PASSWORD, settings)

    def test_ラインは選べば入る(self) -> None:
        from nippou import config, distribution
        self.configure_source()
        self.export(items=[distribution.ITEM_LINE])
        self.assertEqual(self.written()["settings"][config.KEY_TERMINAL_LINE], "LVC")

    def test_設定していない項目は入れない(self) -> None:
        """配った先の既定値を「空」で上書きしない。"""
        from nippou import config
        self.configure_source()
        result = self.export()
        self.assertNotIn(config.KEY_MONTHLY_DIR, self.written()["settings"])
        self.assertIn("既定のまま", result.message)

    def test_音を入れると置き場所の項目は入れない(self) -> None:
        """写した音と共有の音の2つがあると、どちらで鳴るのか分からない。"""
        from nippou import config, distribution
        self.configure_source()
        self.make_sounds()
        self.export(files=["sounds"])
        self.assertNotIn(config.KEY_SOUND_DIR, self.written()["settings"])
        self.assertEqual(len(list(distribution.sounds_dir().iterdir())), 5)

    def test_音が1つも無ければ断る(self) -> None:
        self.configure_source()
        result = self.export(files=["sounds"])
        self.assertFalse(result.ok)
        self.assertIn("音声ファイルが見つからない", result.message)

    def test_書き出し直すと前の中身は置き換わる(self) -> None:
        from nippou import distribution
        self.configure_source()
        self.make_sounds()
        self.export(files=["sounds"])
        self.export(files=[])
        self.assertFalse(distribution.sounds_dir().exists())

    def test_画面にパスワードの値を出さない(self) -> None:
        from nippou import distribution, user_settings
        self.configure_source()
        self.export()
        text = json.dumps(distribution.summary(), ensure_ascii=False)
        self.assertNotIn(user_settings.get("admin_password"), text)
        self.assertIn("(設定済み)", text)

    def test_知らない項目は断る(self) -> None:
        from nippou import distribution
        self.configure_source()
        self.assertEqual(self.export(items=["謎"]).reason, distribution.REFUSE_BAD_INPUT)

    def test_消すにもパスワード(self) -> None:
        from nippou import distribution
        self.configure_source()
        self.export()
        self.assertFalse(distribution.remove("ちがう").ok)
        self.assertTrue(distribution.folder().exists())
        self.assertTrue(distribution.remove(NEW_PW).ok)
        self.assertFalse(distribution.folder().exists())

    def test_鍵が開いていればパスワードは聞かない(self) -> None:
        from nippou import distribution
        self.configure_source()
        self.assertTrue(distribution.export("", ["access_dir"], [], admin=True).ok)


class ApplyTests(DistributionTestCase):
    def distributed(self, **kwargs) -> None:
        self.configure_source()
        self.assertTrue(self.export(**kwargs).ok)
        self.dest.use()

    def test_配布設定フォルダがあれば読み込む(self) -> None:
        from nippou import admin_password, config, distribution, user_settings
        self.distributed()
        result = distribution.apply_on_start()
        self.assertIn("共有の日報管理のパス", result.applied)
        self.assertEqual(user_settings.load_all()[config.KEY_ACCESS_DIR], r"\\srv\日報")
        self.assertTrue(admin_password.verify(NEW_PW))
        self.assertFalse(admin_password.verify(PW))
        self.assertIsNone(user_settings.get(config.KEY_TERMINAL_LINE))
        self.assertTrue(distribution.applied_at())

    def test_既存データがある項目は読み込まない(self) -> None:
        from nippou import config, distribution, user_settings
        self.distributed()
        user_settings.save(config.KEY_REFERENCE_DIR, r"\\この端末\参照")
        result = distribution.apply_on_start()
        self.assertEqual(user_settings.get(config.KEY_REFERENCE_DIR), r"\\この端末\参照")
        self.assertIn("参照用マスタの参照パス", result.kept)
        # 無い項目は埋める
        self.assertEqual(user_settings.load_all()[config.KEY_ACCESS_DIR], r"\\srv\日報")

    def test_空の値は既存と数えない(self) -> None:
        """「既定に戻す」の空のまま残すと、共有へ保存が端末の中へ書かれる。"""
        from nippou import config, distribution, user_settings
        self.distributed()
        user_settings.save(config.KEY_ACCESS_DIR, "")
        self.assertIn("共有の日報管理のパス", distribution.apply_on_start().applied)
        self.assertEqual(user_settings.load_all()[config.KEY_ACCESS_DIR], r"\\srv\日報")

    def test_起動のたびに見ても端末で直した値は戻さない(self) -> None:
        from nippou import config, distribution, user_settings
        self.distributed()
        distribution.apply_on_start()
        user_settings.save(config.KEY_REPORT_OUT_DIR, r"\\この端末\出力")
        self.assertEqual(distribution.apply_on_start().applied, [])
        self.assertEqual(user_settings.get(config.KEY_REPORT_OUT_DIR), r"\\この端末\出力")

    def test_読み込み直しはパスワードで上書き(self) -> None:
        from nippou import config, distribution, user_settings
        self.distributed()
        user_settings.save(config.KEY_REFERENCE_DIR, r"\\この端末\参照")
        user_settings.save("admin_password", "")
        # 照合するのは**この端末の**パスワード(まだ読み込んでいないので…
        # ただし空の項目は配布設定の値が効く ── NEW_PW)
        self.assertFalse(distribution.reapply("ちがう").ok)
        self.assertTrue(distribution.reapply(NEW_PW).ok)
        self.assertEqual(user_settings.get(config.KEY_REFERENCE_DIR), r"\\srv\参照")

    def test_読み込む前でも配布設定の値で動く(self) -> None:
        """起動の途中・書き込めなかったときも、同じ値で動くように。"""
        from nippou import config
        self.distributed()
        # (\\srv は Linux では相対の道になるので、同じ読み方で比べる)
        self.assertEqual(config.SETTINGS.access_dir, config.resolve_dir(r"\\srv\日報"))

    def test_読み込んだ値は配布設定を消しても残る(self) -> None:
        import shutil

        from nippou import config, distribution
        self.distributed()
        distribution.apply_on_start()
        shutil.rmtree(distribution.folder())
        self.assertEqual(config.SETTINGS.access_dir, config.resolve_dir(r"\\srv\日報"))

    def test_無い_壊れた_形が違うなら何もしない(self) -> None:
        from nippou import config, distribution, user_settings
        self.dest.use()
        self.assertEqual(distribution.apply_on_start().applied, [])
        distribution.folder().mkdir(parents=True)
        distribution.settings_path().write_text("{壊れ", encoding="utf-8")
        result = distribution.apply_on_start()
        self.assertEqual(result.applied, [])
        self.assertIn("読めません", result.error)
        self.assertIn("読めません", distribution.summary()["error"])
        distribution.settings_path().write_text(json.dumps({"format": 99, "settings": {
            config.KEY_ACCESS_DIR: "x"}}), encoding="utf-8")
        self.assertEqual(distribution.apply_on_start().applied, [])
        self.assertNotIn(config.KEY_ACCESS_DIR, user_settings.load_all())

    def test_知らない鍵は入れない(self) -> None:
        from nippou import config, distribution, user_settings
        self.dest.use()
        distribution.folder().mkdir(parents=True)
        distribution.settings_path().write_text(json.dumps({"format": 1, "settings": {
            "謎の鍵": 1, config.KEY_ACCESS_DIR: r"\\x"}}), encoding="utf-8")
        distribution.apply_on_start()
        self.assertNotIn("謎の鍵", user_settings.load_all())
        self.assertEqual(user_settings.get(config.KEY_ACCESS_DIR), r"\\x")

    def test_ラインを入れたら決めていない端末だけ埋める(self) -> None:
        from nippou import config, distribution, user_settings, work_context
        self.distributed(items=["access_dir", distribution.ITEM_LINE])
        distribution.apply_on_start()
        self.assertEqual(user_settings.load_all()[config.KEY_TERMINAL_LINE], "LVC")
        self.assertEqual(work_context.reset().line, "LVC")

    def test_ラインを決めていなければ日報入力で知らせる(self) -> None:
        from nippou import config, user_settings, work_context
        self.distributed()
        self.assertIn("まだ決まっていません", work_context.terminal_line_note())
        user_settings.save(config.KEY_TERMINAL_LINE, "L-1")
        self.assertEqual(work_context.terminal_line_note(), "")


class SoundTests(DistributionTestCase):
    """音声ファイルは**端末の中へ写す** ── あとで `配布設定\\` を消しても鳴る。"""

    def distributed(self) -> None:
        self.configure_source()
        self.make_sounds()
        self.assertTrue(self.export(files=["sounds"]).ok)
        self.dest.use()

    def test_音の置き場所を決めていない端末には写す(self) -> None:
        import shutil

        from nippou import config, distribution
        self.distributed()
        result = distribution.apply_on_start()
        self.assertTrue(any(a.startswith("音声ファイル") for a in result.applied), result)
        own = distribution.terminal_sound_dir()
        self.assertEqual(config.SETTINGS.sound_dir, own)
        shutil.rmtree(distribution.folder())            # 片付けても
        path = config.SETTINGS.sound_path("print_reminder")
        self.assertTrue(path.is_file(), path)            # 鳴る

    def test_写す前でも配布設定の音で鳴る(self) -> None:
        from nippou import config, distribution
        self.distributed()
        self.assertEqual(config.SETTINGS.sound_dir, distribution.sounds_dir())

    def test_音の置き場所を決めてある端末には写さない(self) -> None:
        from nippou import config, distribution, user_settings
        self.distributed()
        user_settings.save(config.KEY_SOUND_DIR, r"\\この端末\音")
        self.assertIn("音声ファイル", distribution.apply_on_start().kept)
        self.assertFalse(distribution.terminal_sound_dir().exists())
        # 読み込み直せば揃う
        self.assertTrue(distribution.reapply(NEW_PW).ok)
        self.assertEqual(config.SETTINGS.sound_dir, distribution.terminal_sound_dir())


class LegacyTests(DistributionTestCase):
    """v3.73 の `配布先\\`(平らな形)も、`配布設定\\` が無ければ読む。"""

    def test_前の版の配布先を読む(self) -> None:
        from nippou import config, distribution, user_settings
        self.dest.use()
        old = self.root / "tool" / "配布先"
        old.mkdir(parents=True)
        (old / "設定.json").write_text(json.dumps({config.KEY_ACCESS_DIR: r"\\前"}),
                                        encoding="utf-8")
        with mock.patch.object(distribution, "_legacy_dirs", return_value=[old]):
            self.assertTrue(distribution.summary()["legacy"])
            distribution.apply_on_start()
        self.assertEqual(user_settings.load_all()[config.KEY_ACCESS_DIR], r"\\前")


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class WebTests(WebTestCase):
    terminal_line = None          # 配った先の、ラインを決めていない端末

    def configure(self) -> None:
        from nippou import config, user_settings
        user_settings.save(config.KEY_ACCESS_DIR, r"\\srv\日報")
        self.post("/api/settings/admin-password",
                  {"current": PW, "new": NEW_PW, "confirm": NEW_PW})

    def test_パスワードが違えば403(self) -> None:
        self.configure()
        res = self.post("/api/settings/distribution/export",
                        {"items": ["access_dir"], "files": [], "password": "x"})
        self.assertEqual(res.status_code, 403)

    def test_書き出すと状態に出る(self) -> None:
        self.configure()
        res = self.post("/api/settings/distribution/export",
                        {"items": ["access_dir"], "files": [], "password": NEW_PW})
        self.assertEqual(res.status_code, 200, res.get_json())
        dist = res.get_json()["distribution"]
        self.assertTrue(dist["exists"])
        self.assertEqual(dist["contents"], [
            {"label": "共有の日報管理のパス", "value": r"\\srv\日報"}])

    def test_形が違えば400(self) -> None:
        res = self.post("/api/settings/distribution/export",
                        {"items": "access_dir", "files": [], "password": NEW_PW})
        self.assertEqual(res.status_code, 400)

    def test_鍵が開いていればパスワードは要らない(self) -> None:
        self.configure()
        self.post("/api/settings/admin", {"enable": True, "password": NEW_PW})
        res = self.post("/api/settings/distribution/export",
                        {"items": ["access_dir"], "files": []})
        self.assertEqual(res.status_code, 200, res.get_json())

    def test_読み込み直しと消す(self) -> None:
        self.configure()
        self.post("/api/settings/distribution/export",
                  {"items": ["access_dir"], "files": [], "password": NEW_PW})
        res = self.post("/api/settings/distribution/reapply", {"password": NEW_PW})
        self.assertEqual(res.status_code, 200, res.get_json())
        res = self.post("/api/settings/distribution/remove", {"password": NEW_PW})
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.get_json()["distribution"]["exists"])

    def test_画面に面がある(self) -> None:
        html = self.get("/settings?tab=terminal").get_data(as_text=True)
        self.assertIn('id="dist-export"', html)
        self.assertIn('data-dist-item="terminal_line"', html)
        self.assertIn('data-dist-file="sounds"', html)
        self.assertIn("起動用の", html)
        # ラインは既定で外す
        self.assertNotIn('data-dist-item="terminal_line" checked', html)

    def test_このまま配ると困ることを言う(self) -> None:
        html = self.get("/settings?tab=terminal").get_data(as_text=True)
        self.assertIn("共有の日報管理のパス", html)
        self.assertIn("このままでは困る", html)

    def test_日報入力にラインの知らせが出る(self) -> None:
        html = self.get("/").get_data(as_text=True)
        self.assertIn('id="terminal-line-note"', html)
        self.assertIn("まだ決まっていません", html)


if __name__ == "__main__":
    unittest.main()
