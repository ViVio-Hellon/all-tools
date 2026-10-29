"""この端末の設定ファイル(user_config.json)を「読めなかった」のを「無い・空」と取り違えない

別ツールで、共有を読めなかったのを「実績の表が無い」と見て手元の実績を送り直し、重複が大量に
できた事案を受けた点検の続き。日報の端末の設定ファイルも同じ型で、以前は読めない(ロック中・
権限)と空の設定として返していた:

    ・そのまま1項目を保存すると、ほかの設定(置き場所・管理者パスワード・音など)が全部消えた
    ・管理者パスワードを変えた端末でも、既定のパスワードで通った
    ・配布設定の書き出しは「既定のまま(要りません)」と答え、読み込みは全部を配布設定の値で埋めた

もう1つ、設定ファイルへ直に書いていたので、書いている途中で落ちると中身が半分になり、次に
読んだときに全部消えていた(Windows では、ほかの処理が開いている瞬間に置き換えると
「アクセスが拒否されました」(WinError 5)にもなる)。

**読めない状態は `Path.read_text` を差し替えて作る**(この端末の設定ファイルだけを断る)。
待ち時間は 0 にする。
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

from tests._web import WebTestCase  # noqa: E402

NEW_PW = "newpass123"

#: 試験の前からこの端末にある設定(読めなかったときに消えてはいけないもの)
BEFORE = {
    "access_dir": r"\\srv\日報",
    "reference_dir": r"\\srv\参照",
    "sound_dir": r"\\srv\音",
}


def _refuse_reading(target: Path, times: int | None = None):
    """`target` を読むと断られる(ほかの処理が掴んでいる・権限)。``times`` 回だけなら一瞬のこと。"""
    real = Path.read_text
    count = {"n": 0}

    def refuse(self, *args, **kwargs):
        if Path(self) == target and (times is None or count["n"] < times):
            count["n"] += 1
            raise PermissionError(13, "Permission denied", str(self))
        return real(self, *args, **kwargs)

    return mock.patch.object(Path, "read_text", refuse), count


class _Terminal(unittest.TestCase):
    """1台ぶんの設定ファイル・配布設定を一時フォルダに置く。"""

    def setUp(self) -> None:
        # **消し残しで落とさない。** `NIPPOU_APP_DIR` はプロセス全体の環境変数なので、前の試験の
        # 後ろで動き続けている処理(控えへ写す・記録)が、この間だけここへ書くことがある。
        # 消している最中に書かれると「フォルダが空でない」で落ちていた(試験一式のときだけ)
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        keys = ("NIPPOU_APP_DIR", "NIPPOU_LOCAL_DIR", "NIPPOU_DIST_DIR",
                "ALLTOOLS_DISTRIBUTION_DIR")
        saved = {k: os.environ.get(k) for k in keys}

        def restore() -> None:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.addCleanup(restore)
        os.environ.pop("ALLTOOLS_DISTRIBUTION_DIR", None)
        os.environ["NIPPOU_APP_DIR"] = str(self.root / "app")
        os.environ["NIPPOU_LOCAL_DIR"] = str(self.root / "local")
        os.environ["NIPPOU_DIST_DIR"] = str(self.root / "tool" / "配布設定")

        from nippou import config, user_settings
        self.config = config
        self.us = user_settings
        self.path = config.USER_CONFIG_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.write(dict(BEFORE))
        # 待ち時間は 0(直す前の版には無い名前なので create=True ── 名前が無いから落ちる、にしない)
        for name in ("READ_WAIT_SEC", "REPLACE_WAIT_SEC"):
            patcher = mock.patch.object(user_settings, name, 0, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, data: dict) -> None:
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def stored(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def unreadable(self, times: int | None = None):
        patcher, _count = _refuse_reading(self.path, times)
        return patcher

    def leftovers(self) -> list[str]:
        """置き換えに使った一時ファイルの残り(同じフォルダにできるログのフォルダなどは数えない)。"""
        return sorted(p.name for p in self.path.parent.iterdir()
                      if p.is_file() and p.name != self.path.name)


# ------------------------------------------------------------------
# 読む
# ------------------------------------------------------------------
class ReadTests(_Terminal):
    def test_無ければ空(self) -> None:
        self.path.unlink()
        self.assertEqual(self.us.load_all(), {})
        self.assertEqual(self.us.load_checked(), {})

    def test_中身が壊れていれば空(self) -> None:
        self.path.write_text("{ 書きかけ", encoding="utf-8")
        self.assertEqual(self.us.load_all(), {})
        self.assertEqual(self.us.load_checked(), {})

    def test_読めなければ書く前の読みは例外_画面に出す読みは空で続ける(self) -> None:
        with self.unreadable():
            with self.assertRaises(self.us.SettingsUnreadable):
                self.us.load_checked()
            self.assertEqual(self.us.load_all(), {})                 # 起動・画面は止めない
            self.assertIsNone(self.us.get(self.config.KEY_ACCESS_DIR))

    def test_一瞬だけ読めないなら読み直す(self) -> None:
        patcher, count = _refuse_reading(self.path, times=2)
        with patcher:
            self.assertEqual(self.us.load_checked(), BEFORE)
        self.assertEqual(count["n"], 2)


# ------------------------------------------------------------------
# 書く
# ------------------------------------------------------------------
class WriteTests(_Terminal):
    def test_読めなければ1項目の保存で_ほかの設定を消さない(self) -> None:
        with self.unreadable():
            ok = self.us.save(self.config.KEY_GW_RECORD_FORMAT, "csv")
        self.assertFalse(ok)                                         # 書けなかった、と言う
        self.assertEqual(self.stored(), BEFORE)
        self.assertIn("読めませんでした", self.us.last_problem())

    def test_読めなければまとめての保存もしない(self) -> None:
        with self.unreadable():
            ok = self.us.save_many({self.config.KEY_TERMINAL_LINE: "LVC",
                                    self.config.KEY_TERMINAL_MARU: ""})
        self.assertFalse(ok)
        self.assertEqual(self.stored(), BEFORE)

    def test_読めなければ消すのもしない(self) -> None:
        with self.unreadable():
            ok = self.us.forget(self.config.KEY_ACCESS_DIR)
        # 直す前は「無い」とみなして True(消したことになる)と答えていた
        self.assertFalse(ok)
        self.assertEqual(self.stored(), BEFORE)

    def test_一瞬だけ読めないなら読み直して保存する(self) -> None:
        with self.unreadable(times=1):
            self.assertTrue(self.us.save(self.config.KEY_GW_RECORD_FORMAT, "csv"))
        self.assertEqual(self.stored(), {**BEFORE, self.config.KEY_GW_RECORD_FORMAT: "csv"})

    def test_書いている途中で落ちても_前の中身が残る(self) -> None:
        # 直に書いていたころは、ここで中身が半分になり、次に読むと全部消えていた
        real_write = Path.write_text

        def half(self_path, text, *args, **kwargs):
            real_write(self_path, text[: len(text) // 2], *args, **kwargs)
            raise OSError(28, "No space left on device")

        with mock.patch.object(Path, "write_text", half), \
                mock.patch("os.fsync", side_effect=OSError(28, "No space left on device")):
            ok = self.us.save(self.config.KEY_GW_RECORD_FORMAT, "csv")
        self.assertFalse(ok)
        self.assertEqual(self.stored(), BEFORE)
        self.assertEqual(self.us.load_all(), BEFORE)
        self.assertEqual(self.leftovers(), [])                       # 一時ファイルは残さない

    def test_置き換えが一瞬断られたら_待ってやり直す(self) -> None:
        real = os.replace
        calls = {"n": 0}

        def busy(src, dst):
            calls["n"] += 1
            if calls["n"] <= 3:                                      # ほかの処理が開いている
                raise PermissionError(13, "アクセスが拒否されました")
            return real(src, dst)

        with mock.patch("os.replace", busy):
            self.assertTrue(self.us.save(self.config.KEY_GW_RECORD_FORMAT, "csv"))
        self.assertEqual(calls["n"], 4)
        self.assertEqual(self.stored(), {**BEFORE, self.config.KEY_GW_RECORD_FORMAT: "csv"})
        self.assertEqual(self.leftovers(), [])

    def test_置き換えがずっと断られたら書けないと言い_前の中身と一時ファイルを残さない(self) -> None:
        with mock.patch("os.replace", side_effect=PermissionError(13, "アクセスが拒否されました")) as rep:
            ok = self.us.save(self.config.KEY_GW_RECORD_FORMAT, "csv")
        self.assertFalse(ok)
        self.assertEqual(rep.call_count, self.us.REPLACE_TRIES)
        self.assertEqual(self.stored(), BEFORE)
        self.assertEqual(self.leftovers(), [])


# ------------------------------------------------------------------
# 管理者パスワード
# ------------------------------------------------------------------
class AdminPasswordTests(_Terminal):
    def setUp(self) -> None:
        super().setUp()
        from nippou import admin_password
        self.ap = admin_password
        self.default = self.config.SETTINGS.admin_password
        self.assertTrue(admin_password.change(self.default, NEW_PW, NEW_PW).ok)
        self.before = self.stored()

    def test_読めなくても既定のパスワードでは通さない(self) -> None:
        with self.unreadable():
            try:
                passed = self.ap.verify(self.default)
            except self.us.SettingsUnreadable:
                passed = None
            # 直す前は「変えていない」とみなして True(既定のパスワードで通っていた)
            self.assertIsNot(passed, True)
            with self.assertRaises(self.us.SettingsUnreadable):
                self.ap.verify(self.default)

    def test_読めれば変えたパスワードで通り_既定では通らない(self) -> None:
        self.assertTrue(self.ap.verify(NEW_PW))
        self.assertFalse(self.ap.verify(self.default))

    def test_読めなければ変えない_ほかの設定も消さない(self) -> None:
        with self.unreadable():
            result = self.ap.change(self.default, "other456", "other456")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, self.ap.REFUSE_UNREADABLE)
        self.assertIn("読めなかった", result.message)
        self.assertEqual(self.stored(), self.before)

    def test_読めなければ既定に戻さない(self) -> None:
        with self.unreadable():
            result = self.ap.reset(self.default)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, self.ap.REFUSE_UNREADABLE)
        self.assertEqual(self.stored(), self.before)
        self.assertTrue(self.ap.verify(NEW_PW))

    def test_書けなければ変えたと言わない(self) -> None:
        with mock.patch.object(self.us, "save", return_value=False):
            changed = self.ap.change(NEW_PW, "other456", "other456")
            reset = self.ap.reset(NEW_PW)
        self.assertFalse(changed.ok)
        self.assertFalse(reset.ok)
        self.assertTrue(self.ap.verify(NEW_PW))


# ------------------------------------------------------------------
# 配布設定
# ------------------------------------------------------------------
class DistributionTests(_Terminal):
    def setUp(self) -> None:
        super().setUp()
        from nippou import distribution
        self.dist = distribution

    def place(self, settings: dict) -> Path:
        """配布設定を置く(書き出したのと同じ形)。"""
        base = self.dist.folder()
        base.mkdir(parents=True, exist_ok=True)
        path = self.dist.settings_path(base)
        path.write_text(json.dumps({"format": self.dist.FORMAT, "created_at": "2026-10-01 08:00",
                                    "settings": settings}, ensure_ascii=False), encoding="utf-8")
        return path

    def test_読めなければ書き出さず_既定のままとも答えない(self) -> None:
        with self.unreadable():
            result = self.dist.export("", [self.config.KEY_ACCESS_DIR], [], admin=True)
        self.assertFalse(result.ok)
        # 直す前は「この端末で変えていない = 要りません」(nothing)と答えていた
        self.assertNotEqual(result.reason, self.dist.REFUSE_NOTHING)
        self.assertEqual(result.reason, self.dist.REFUSE_UNREADABLE)
        self.assertFalse(self.dist.folder().exists())

    def test_読めなければまとめての書き出しで_前の配布設定を減らさない(self) -> None:
        path = self.place({self.config.KEY_ACCESS_DIR: r"\\srv\日報",
                           self.config.KEY_TERMINAL_LINE: "LVC"})
        before = path.read_text(encoding="utf-8")
        items = [i.key for i in self.dist.ITEMS if i.default]
        with self.unreadable():
            result = self.dist.export("", items, [], admin=True, keep_previous=True)
        self.assertFalse(result.ok)
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_前の配布設定を開けなければまとめての書き出しで消さない(self) -> None:
        path = self.place({self.config.KEY_ACCESS_DIR: r"\\srv\前の日報",
                           self.config.KEY_TERMINAL_LINE: "LVC"})
        before = path.read_text(encoding="utf-8")
        items = [i.key for i in self.dist.ITEMS if i.default]
        patcher, _count = _refuse_reading(path)
        with patcher:
            result = self.dist.export("", items, [], admin=True, keep_previous=True)
        # 直す前は「前の中身が無い」とみなし、ラインを落として書き出していた
        self.assertFalse(result.ok)
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_読めなければ起動時に読み込まない_この端末の値を上書きしない(self) -> None:
        self.place({self.config.KEY_ACCESS_DIR: r"\\srv\配った日報",
                    self.config.KEY_REPORT_OUT_DIR: r"\\srv\出力"})
        with self.unreadable():
            result = self.dist.apply_on_start()
        self.assertEqual(result.applied, [])
        self.assertIn("読めなかった", result.error)
        self.assertEqual(self.stored(), BEFORE)
        # 読めるようになれば、いつもの決まりで読む(この端末で決めた値は残し、無い項目だけ埋める)
        result = self.dist.apply_on_start()
        self.assertEqual(self.stored()[self.config.KEY_ACCESS_DIR], BEFORE["access_dir"])
        self.assertEqual(self.stored()[self.config.KEY_REPORT_OUT_DIR], r"\\srv\出力")

    def test_読めなければ読み込み直さない(self) -> None:
        self.place({self.config.KEY_ACCESS_DIR: r"\\srv\配った日報"})
        with self.unreadable():
            result = self.dist.reapply("", admin=True)
        self.assertFalse(result.ok)
        self.assertEqual(self.stored(), BEFORE)

    def test_読めなければ既定のパスワードで消させない(self) -> None:
        from nippou import admin_password
        default = self.config.SETTINGS.admin_password
        self.assertTrue(admin_password.change(default, NEW_PW, NEW_PW).ok)
        self.place({self.config.KEY_ACCESS_DIR: r"\\srv\日報"})
        with self.unreadable():
            removed = self.dist.remove(default)
            exported = self.dist.export(default, [self.config.KEY_ACCESS_DIR], [])
        self.assertFalse(removed.ok)
        self.assertEqual(removed.reason, self.dist.REFUSE_UNREADABLE)
        self.assertTrue(self.dist.folder().exists())
        self.assertFalse(exported.ok)
        self.assertEqual(exported.reason, self.dist.REFUSE_UNREADABLE)


# ------------------------------------------------------------------
# 画面(Web)
# ------------------------------------------------------------------
class WebTests(WebTestCase):
    def setUp(self) -> None:
        super().setUp()
        from nippou import admin_password, config, user_settings
        self.config = config
        self.us = user_settings
        self.ap = admin_password
        self.default = config.SETTINGS.admin_password
        self.assertTrue(user_settings.save_many(dict(BEFORE)))
        self.assertTrue(admin_password.change(self.default, NEW_PW, NEW_PW).ok)
        self.path = config.USER_CONFIG_PATH
        self.before = json.loads(self.path.read_text(encoding="utf-8"))
        patcher = mock.patch.object(user_settings, "READ_WAIT_SEC", 0, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def unreadable(self):
        patcher, _count = _refuse_reading(self.path)
        return patcher

    def stored(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_読めなければ管理者モードに既定のパスワードで入れない(self) -> None:
        with self.unreadable():
            res = self.post("/api/settings/admin", {"enable": True, "password": self.default})
        self.assertNotEqual(res.status_code, 200)
        self.assertEqual(res.status_code, 503)
        self.assertIn("読めなかった", res.get_json()["error"]["message"])
        from nippou import work_context
        self.assertFalse(work_context.get_context().admin)

    def test_読めなければマスタの編集を既定のパスワードで開けない(self) -> None:
        with self.unreadable():
            res = self.post("/api/master/unlock", {"enable": True, "password": self.default})
        self.assertEqual(res.status_code, 503)
        from nippou import work_context
        self.assertFalse(work_context.get_context().master_edit)

    def test_読めなければ参照パスを保存せず_理由を言う(self) -> None:
        new_dir = str(self.tmp / "新しい日報")
        with self.unreadable():
            res = self.post("/api/settings/paths", {self.config.KEY_ACCESS_DIR: new_dir,
                                                    "password": self.default})
        self.assertEqual(res.status_code, 503)
        self.assertIn("読めなかった", res.get_json()["error"]["message"])
        self.assertEqual(self.stored(), self.before)

    def test_鍵が開いていても読めなければ参照パスを保存しない(self) -> None:
        from nippou import work_context
        work_context.get_context().admin = True
        with self.unreadable():
            res = self.post("/api/settings/paths",
                            {self.config.KEY_ACCESS_DIR: str(self.tmp / "新しい日報")})
        self.assertEqual(res.status_code, 503)
        self.assertIn("保存していません", res.get_json()["error"]["message"])
        self.assertEqual(self.stored(), self.before)

    def test_読めなければ管理者の面の切り替えは変えたと言わない(self) -> None:
        from nippou import work_context
        work_context.get_context().admin = True
        with self.unreadable():
            res = self.post("/api/settings/gw-record-format", {"format": "csv"})
        body = res.get_json()
        self.assertFalse(body["remembered"])
        self.assertIn("変えていません", body["message"])
        self.assertEqual(self.stored(), self.before)

    def test_読めなければ管理者パスワードを変えない(self) -> None:
        with self.unreadable():
            res = self.post("/api/settings/admin-password",
                            {"current": self.default, "new": "other456", "confirm": "other456"})
        self.assertEqual(res.status_code, 503)
        self.assertEqual(self.stored(), self.before)


if __name__ == "__main__":
    unittest.main()
