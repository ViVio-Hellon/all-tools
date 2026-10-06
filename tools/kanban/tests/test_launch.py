"""起動基盤のテスト (基盤仕様書 2.1 / 2.2 / 2.3 / 2.4 / 2.7)

Flask を読まないものだけをここに置く。起動の判断はサーバが立たない状況でも
動く必要があるので、テストも同じ条件で回す。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from kanban import app_config, boot_screen, config

APP_ROOT = Path(__file__).resolve().parent.parent


class LocalDirTest(unittest.TestCase):
    """ユーザー別ローカル領域 (基盤仕様書 2.7 / 4.6)。"""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="kanban_local_"))
        self._saved = os.environ.get("KANBAN_LOCAL_DIR")
        os.environ["KANBAN_LOCAL_DIR"] = str(self.dir)

    def tearDown(self) -> None:
        if self._saved is None:
            os.environ.pop("KANBAN_LOCAL_DIR", None)
        else:
            os.environ["KANBAN_LOCAL_DIR"] = self._saved
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_override_is_honoured(self):
        self.assertEqual(app_config.local_root(), self.dir)

    def test_ensure_creates_every_subdir(self):
        app_config.ensure_local_dirs()
        for name in app_config.LOCAL_SUBDIRS:
            self.assertTrue((self.dir / name).is_dir(), f"{name} が作られていない")

    def test_unknown_subdir_is_rejected(self):
        with self.assertRaises(ValueError):
            app_config.local_dir("どこか")

    def test_data_is_separate_from_cache(self):
        """DB と利用者設定は消せない。``cache``/``work`` と分けてある。"""
        self.assertIn("data", app_config.LOCAL_SUBDIRS)
        self.assertIn("cache", app_config.LOCAL_SUBDIRS)


class AppConfigTest(unittest.TestCase):
    def test_reads_the_real_config(self):
        self.assertEqual(app_config.app_id(), "nlm.kanban-system")
        self.assertTrue(app_config.display_name())
        self.assertEqual(app_config.load_error(), "")

    def test_version_is_well_formed(self):
        """版が読めない形だと「どれが新しいか」を並べて比べられなくなる。"""
        self.assertEqual(app_config.version_problem(), "")
        self.assertTrue(app_config.version_label().startswith("VER"))

    def test_every_mode_has_a_port(self):
        for mode in config.ALL_MODES:
            self.assertIsInstance(app_config.port(mode), int)

    def test_port_ranges_do_not_overlap(self):
        """重なると、現場が繰り上がって倉庫のポートを奪う事故になる。

        利用者からは「倉庫モードが開けない」としか見えず原因が分からない。
        """
        self.assertEqual(app_config.port_range_conflicts(), [])

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            app_config.port("架空モード")

    def test_broken_config_falls_back_instead_of_crashing(self):
        """設定が壊れていても**起動はして、理由を出す**(基盤仕様書 ステップ5)。"""
        broken = Path(tempfile.mkdtemp(prefix="kanban_cfg_")) / "app.json"
        broken.write_text("{ これは JSON ではない", encoding="utf-8")
        saved_path, saved_cache = app_config.CONFIG_PATH, app_config._cache
        try:
            app_config.CONFIG_PATH = broken
            app_config.load(force=True)
            self.assertIn("読めませんでした", app_config.load_error())
            # 既定値で動く
            self.assertEqual(app_config.app_id(), "nlm.kanban-system")
        finally:
            # **読み込みエラーまで戻す。** `_cache` だけ戻しても
            # `_load_error` は壊れたままで、`load()` はキャッシュがあると
            # そこで返るので拾い直されない ── 後続のテストに、このテストが
            # 作った「壊れています」が漏れる(実行順で落ちる)
            app_config.CONFIG_PATH = saved_path
            app_config._cache = saved_cache
            app_config.load(force=True)
            shutil.rmtree(broken.parent, ignore_errors=True)


class BootScreenTest(unittest.TestCase):
    """起動待機画面 (基盤仕様書 2.2)。"""

    def _render(self) -> str:
        return boot_screen.render(
            display_name="資材発注看板システム",
            version_label="VER3.0.0",
            token="tok",
            app_id="nlm.kanban-system",
        )

    def test_is_self_contained(self):
        """外部への要求を出さない。本体がまだ立ち上がっていないため。"""
        html = self._render()
        self.assertNotIn("/static/", html)
        self.assertNotIn("<link", html)

    def test_shows_stages(self):
        html = self._render()
        for _key, label in boot_screen.STAGES:
            self.assertIn(label, html)

    def test_checks_app_id_before_entering(self):
        """同じポートに別のアプリが居ることがある(基盤仕様書 2.3)。"""
        self.assertIn("nlm.kanban-system", self._render())

    def test_escapes_the_display_name(self):
        html = boot_screen.render(
            display_name='<script>alert(1)</script>',
            version_label="VER0.0.0", token="t", app_id="a",
        )
        self.assertNotIn("<script>alert(1)</script>", html)


class LaunchFileTest(unittest.TestCase):
    """起動用ファイルの文字コード。

    **これが崩れると現場で起動しない。** WSH は ``.vbs`` をシステムの ANSI
    コードページ(日本語 Windows では 932)で読み、cmd.exe は ``.bat`` を
    コンソールのコードページで解釈する。UTF-8 で置くと日本語が化けるだけで
    なく、CP932 の 2 バイト目が ``0x5C``(``\\``)や ``0x40``(``@``)になる
    文字があるため、**構文そのものが壊れる**。
    """

    FILES = ("Start.vbs", "start.bat", "stop.bat")

    def test_all_exist(self):
        for name in self.FILES:
            self.assertTrue((APP_ROOT / name).is_file(), f"{name} が無い")

    def test_encoded_as_cp932(self):
        for name in self.FILES:
            raw = (APP_ROOT / name).read_bytes()
            try:
                raw.decode("cp932")
            except UnicodeDecodeError as exc:
                self.fail(f"{name} が CP932 で読めない: {exc}")

    def test_uses_crlf(self):
        for name in self.FILES:
            raw = (APP_ROOT / name).read_bytes()
            self.assertNotIn(b"\n", raw.replace(b"\r\n", b""),
                             f"{name} に LF だけの行がある")

    def test_bat_sets_codepage_before_any_japanese(self):
        """``chcp 932`` より前は ASCII だけにする。

        cmd.exe は**そのときのコードページ**で .bat を読む。切り替える前に
        非 ASCII のバイトがあると、コメントの中の文字がバックスラッシュや
        アットマークに化けて構文が壊れる。
        """
        for name in ("start.bat", "stop.bat"):
            raw = (APP_ROOT / name).read_bytes()
            at = raw.find(b"chcp 932")
            self.assertGreater(at, 0, f"{name} に chcp 932 が無い")
            head = raw[:at]
            self.assertTrue(all(b < 0x80 for b in head),
                            f"{name} の chcp より前に非ASCIIバイトがある")

    def test_vbs_checks_python_before_running(self):
        """pythonw はコンソールを出さない。無いまま起動すると**無言で失敗する**。"""
        text = (APP_ROOT / "Start.vbs").read_bytes().decode("cp932")
        self.assertIn("python --version", text)
        self.assertIn("pythonw --version", text)
        self.assertIn("start_app.py", text)

    def test_bat_runs_the_check_first(self):
        text = (APP_ROOT / "start.bat").read_bytes().decode("cp932")
        self.assertIn("--check", text)


class LaunchGuardTest(unittest.TestCase):
    """多重起動の判定 (基盤仕様書 2.4)。"""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="kanban_guard_"))
        self._saved = os.environ.get("KANBAN_LOCAL_DIR")
        os.environ["KANBAN_LOCAL_DIR"] = str(self.dir)
        app_config.ensure_local_dirs()

    def tearDown(self) -> None:
        if self._saved is None:
            os.environ.pop("KANBAN_LOCAL_DIR", None)
        else:
            os.environ["KANBAN_LOCAL_DIR"] = self._saved
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_lock_roundtrip(self):
        import launch_guard

        info = launch_guard.build_lock_info(config.MODE_SITE, 8741, token="tok")
        launch_guard.write_lock(info)
        back = launch_guard.read_lock(config.MODE_SITE)
        self.assertIsNotNone(back)
        self.assertEqual(back.port, 8741)
        self.assertEqual(back.token, "tok")
        self.assertEqual(back.mode, config.MODE_SITE)

    def test_each_mode_has_its_own_lock(self):
        """モードごとに別のロックにする。

        同時に動かしてよいからではなく、**どのモードで動いているか**を
        置き場所でも表すため(この PC で動いてよいのは 1 つだけ)。
        """
        import launch_guard

        paths = {launch_guard.lock_path(m) for m in config.ALL_MODES}
        self.assertEqual(len(paths), len(config.ALL_MODES))

    def test_broken_lock_does_not_block_startup(self):
        """読めないことを理由に起動を止めない。"""
        import launch_guard

        path = launch_guard.lock_path(config.MODE_SITE)
        path.write_text("これは JSON ではない", encoding="utf-8")
        self.assertIsNone(launch_guard.read_lock(config.MODE_SITE))

    def test_missing_lock_means_start(self):
        import launch_guard

        result = launch_guard.check_existing(config.MODE_SITE)
        self.assertTrue(result.should_start)

    def test_dead_lock_is_cleaned_up(self):
        """PID が居なければ掃除して起動する。"""
        import launch_guard

        info = launch_guard.build_lock_info(config.MODE_SITE, 8741)
        info.pid = 999_999_999  # 存在しない
        launch_guard.write_lock(info)
        result = launch_guard.check_existing(config.MODE_SITE)
        self.assertTrue(result.should_start)
        self.assertIsNone(launch_guard.read_lock(config.MODE_SITE))

    def test_is_our_app_checks_id_and_mode(self):
        """app_id を見ないと、同じポートの別アプリを自分だと誤認する。"""
        import launch_guard

        ours = {"app_id": app_config.app_id(), "mode": config.MODE_SITE}
        self.assertTrue(launch_guard.is_our_app(ours, config.MODE_SITE))
        # モードが違う
        self.assertFalse(launch_guard.is_our_app(ours, config.MODE_WAREHOUSE))
        # 別のアプリ
        other = {"app_id": "nlm.something-else", "mode": config.MODE_SITE}
        self.assertFalse(launch_guard.is_our_app(other, config.MODE_SITE))
        self.assertFalse(launch_guard.is_our_app(None, config.MODE_SITE))

    # -- 版の入れ替え ---------------------------------------------------
    #
    # **更新のたびに通る道なのに、実地で試せるのは更新のときだけ**なので、
    # ここで押さえておく。壊れていると「新しいコードを置いたのに、古い版が
    # 動き続ける」という一番たちの悪い形になり、画面の版バッジも古いまま
    # なので気付けない。
    def _running(self, version: str, port: int = 8741):
        """``port`` で ``version`` が動いていることにする。"""
        import launch_guard

        info = launch_guard.build_lock_info(config.MODE_SITE, port, token="tok")
        info.pid = os.getpid()  # 生きている PID
        launch_guard.write_lock(info)
        return {
            "app_id": app_config.app_id(),
            "mode": config.MODE_SITE,
            "version": version,
        }

    def test_same_version_joins_the_running_one(self):
        """版が同じなら合流する(いつもの多重起動)。"""
        import launch_guard
        from unittest import mock

        health = self._running(app_config.version())
        with mock.patch.object(launch_guard, "probe_health", return_value=health):
            result = launch_guard.check_existing(config.MODE_SITE)
        self.assertFalse(result.should_start)
        self.assertEqual(result.reason, "同じアプリが起動中")

    def test_old_version_is_terminated_and_replaced(self):
        """**版が違えば合流しない。** 古いほうを終わらせて立て直す。

        合流してしまうと、新しいコードを置いたのに古い版が動き続ける。
        """
        import launch_guard
        from unittest import mock

        health = self._running("0.0.1")
        asked = []
        with mock.patch.object(launch_guard, "probe_health",
                               side_effect=[health, None]), \
             mock.patch.object(launch_guard, "request_shutdown",
                               side_effect=lambda *a, **k: asked.append(a) or True):
            result = launch_guard.check_existing(config.MODE_SITE)

        self.assertTrue(result.should_start, "古い版を残して合流している")
        self.assertEqual(result.stale_version, "0.0.1")
        self.assertTrue(asked, "終了を頼んでいない")
        self.assertIsNone(
            launch_guard.read_lock(config.MODE_SITE), "ロックを掃除していない"
        )

    def test_an_unstoppable_old_version_is_not_left_hanging(self):
        """止められなければ**合流する**。

        止まらない相手を待ち続けて起動しないより、古い画面でも開くほうが
        まし ── 理由は画面に出る。
        """
        import launch_guard
        from unittest import mock

        health = self._running("0.0.1")
        with mock.patch.object(launch_guard, "probe_health", return_value=health), \
             mock.patch.object(launch_guard, "request_shutdown", return_value=False):
            result = launch_guard.check_existing(config.MODE_SITE)

        self.assertFalse(result.should_start)
        self.assertEqual(result.stale_version, "0.0.1")
        self.assertIn("0.0.1", result.reason)

    def test_unknown_running_version_joins_instead_of_killing(self):
        """版が読めない相手は殺さない。

        読めないことを「古い」と決めつけると、健全な端末を落としうる。
        """
        import launch_guard
        from unittest import mock

        health = self._running("")
        with mock.patch.object(launch_guard, "probe_health", return_value=health):
            result = launch_guard.check_existing(config.MODE_SITE)
        self.assertFalse(result.should_start)
        self.assertEqual(result.reason, "同じアプリが起動中")

    # -- この PC で動いてよいのは1つだけ ---------------------------------
    #
    # モードごとにしかロックを見ていなかったころ、こうなった:
    #
    #   1. 現場モードで起動(site.lock / ポート 8741)
    #   2. 設定画面でモードを倉庫に変える
    #   3. もう一度ダブルクリック → warehouse.lock は無いので**2つめが起動**
    #
    # しかも 2 で名乗りだけ倉庫に変わると、site.lock の相手が「倉庫です」と
    # 答えるので is_our_app が別人と判断し、ロックを消して**3つめ**まで
    # 立ち上がった。実機で再現済み。
    def _running_as(self, mode: str, port: int, version: str | None = None):
        """``mode`` が ``port`` で動いていることにして、その health を返す。"""
        import launch_guard

        info = launch_guard.build_lock_info(mode, port, token="tok")
        info.pid = os.getpid()
        launch_guard.write_lock(info)
        return {
            "app_id": app_config.app_id(),
            "mode": mode,
            "version": app_config.version() if version is None else version,
        }

    def test_another_mode_is_terminated_before_starting(self):
        """**モードを変えたあとの起動。** 古いモードを終わらせて入れ替える。

        合流すると新しいモードで動けず、放っておくと2つ動く。
        """
        import launch_guard
        from unittest import mock

        health = self._running_as(config.MODE_SITE, 8741)
        asked = []
        with mock.patch.object(launch_guard, "probe_health",
                               side_effect=[health, None]), \
             mock.patch.object(launch_guard, "request_shutdown",
                               side_effect=lambda *a, **k: asked.append(a) or True):
            result = launch_guard.check_existing(config.MODE_WAREHOUSE)

        self.assertTrue(result.should_start, "古いモードを残したまま起動している")
        self.assertIn("現場モード", result.reason)
        self.assertTrue(asked, "終了を頼んでいない")
        self.assertIsNone(launch_guard.read_lock(config.MODE_SITE),
                          "古いモードのロックが残っている")

    def test_another_mode_that_will_not_stop_is_joined(self):
        """止められなければ合流する。**2つ動かすよりまし。**"""
        import launch_guard
        from unittest import mock

        health = self._running_as(config.MODE_SITE, 8741)
        with mock.patch.object(launch_guard, "probe_health", return_value=health), \
             mock.patch.object(launch_guard, "request_shutdown", return_value=False):
            result = launch_guard.check_existing(config.MODE_WAREHOUSE)
        self.assertFalse(result.should_start)
        self.assertIn("現場モード", result.reason)

    def test_a_dead_lock_in_another_mode_does_not_block(self):
        """よそのモードの死んだロックは掃除して、そのまま起動する。"""
        import launch_guard

        info = launch_guard.build_lock_info(config.MODE_SITE, 8741)
        info.pid = 999_999_999
        launch_guard.write_lock(info)

        result = launch_guard.check_existing(config.MODE_WAREHOUSE)
        self.assertTrue(result.should_start)
        self.assertIsNone(launch_guard.read_lock(config.MODE_SITE))

    def test_an_unrelated_mode_lock_is_ignored_when_it_is_not_ours(self):
        """ロックのポートに別のアプリが居るだけなら、掃除して起動する。"""
        import launch_guard
        from unittest import mock

        self._running_as(config.MODE_SITE, 8741)
        with mock.patch.object(launch_guard, "probe_health", return_value=None):
            result = launch_guard.check_existing(config.MODE_WAREHOUSE)
        self.assertTrue(result.should_start)
        self.assertIsNone(launch_guard.read_lock(config.MODE_SITE))

    def test_lock_is_not_world_readable(self):
        """ロックには起動トークンが入っている。"""
        import launch_guard

        if os.name == "nt":
            self.skipTest("Windows では %LOCALAPPDATA% 自体が利用者ごと")
        path = launch_guard.write_lock(
            launch_guard.build_lock_info(config.MODE_SITE, 8741, token="tok")
        )
        self.assertEqual(path.stat().st_mode & 0o077, 0)


if __name__ == "__main__":
    unittest.main()
