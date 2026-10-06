"""管理者パスワード ── 設定を勝手に変えられないようにする

【何を見張るか】
1. **VBA 版との違いそのもの。** あちらは誰でもどこでもライン設定を
   変えられた。ここでは断られることを固定する ── 緩んでも誰も
   困らないので、試験が無いと静かに元へ戻る
2. **変わらないなら聞かない。** 同じ値で保存し直すだけのときにまで
   聞くと、現場は「押しても何も起きない」と受け取る
3. **断ったのに一部だけ変わった、を作らない。** 関門は書く前に置く
4. 平文で持たない・応答に混ぜない
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from . import _web
from calendar_app import admin_password, config, settings as user_settings


class StoreTests(unittest.TestCase):
    """持ち方(HTTP を通さずに見る)。"""

    def setUp(self) -> None:
        user_settings.set_value(admin_password.KEY, "")
        self.addCleanup(user_settings.set_value, admin_password.KEY, "")

    def test_変えていなければ既定で通る(self) -> None:
        """更新を入れただけで誰も設定を直せなくなる、を作らない。"""
        self.assertTrue(admin_password.verify(config.ADMIN_PASSWORD))
        self.assertFalse(admin_password.is_custom())

    def test_違う値は通らない(self) -> None:
        self.assertFalse(admin_password.verify("でたらめ"))

    def test_変えられる(self) -> None:
        result = admin_password.change(config.ADMIN_PASSWORD, "新しい合言葉",
                                       "新しい合言葉")
        self.assertTrue(result.ok, result.message)
        self.assertTrue(admin_password.verify("新しい合言葉"))
        self.assertTrue(admin_password.is_custom())

    def test_変えたら既定は通らない(self) -> None:
        admin_password.change(config.ADMIN_PASSWORD, "新しい合言葉", "新しい合言葉")
        self.assertFalse(admin_password.verify(config.ADMIN_PASSWORD))

    def test_平文で持たない(self) -> None:
        """配布はフォルダごとコピーなので、設定ファイルは持ち出せる。"""
        admin_password.change(config.ADMIN_PASSWORD, "新しい合言葉", "新しい合言葉")
        stored = user_settings.get(admin_password.KEY, "")
        self.assertNotIn("新しい合言葉", stored)
        self.assertTrue(stored.startswith(admin_password.SCHEME + "$"))

    def test_塩は毎回違う(self) -> None:
        """同じ値でも同じ文字列にならない。"""
        admin_password.change(config.ADMIN_PASSWORD, "合言葉です", "合言葉です")
        first = user_settings.get(admin_password.KEY, "")
        admin_password.reset("合言葉です")
        admin_password.change(config.ADMIN_PASSWORD, "合言葉です", "合言葉です")
        self.assertNotEqual(first, user_settings.get(admin_password.KEY, ""))

    def test_いまの値を知らないと変えられない(self) -> None:
        """肩越しに見ていた人が勝手に変えられる、を作らない。"""
        result = admin_password.change("でたらめ", "新しい合言葉", "新しい合言葉")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, admin_password.REFUSE_WRONG)
        self.assertFalse(admin_password.is_custom())

    def test_短すぎるものは断る(self) -> None:
        result = admin_password.change(config.ADMIN_PASSWORD, "1", "1")
        self.assertEqual(result.reason, admin_password.REFUSE_TOO_SHORT)

    def test_確認用と違えば断る(self) -> None:
        result = admin_password.change(config.ADMIN_PASSWORD, "合言葉A", "合言葉B")
        self.assertEqual(result.reason, admin_password.REFUSE_MISMATCH)

    def test_同じ値は断る(self) -> None:
        result = admin_password.change(config.ADMIN_PASSWORD,
                                       config.ADMIN_PASSWORD,
                                       config.ADMIN_PASSWORD)
        self.assertEqual(result.reason, admin_password.REFUSE_SAME)

    def test_既定に戻せる(self) -> None:
        admin_password.change(config.ADMIN_PASSWORD, "新しい合言葉", "新しい合言葉")
        result = admin_password.reset("新しい合言葉")
        self.assertTrue(result.ok, result.message)
        self.assertTrue(admin_password.verify(config.ADMIN_PASSWORD))

    def test_壊れた値でも落ちない(self) -> None:
        """設定ファイルを手で触られることはある。"""
        user_settings.set_value(admin_password.KEY, "こわれている")
        self.assertFalse(admin_password.verify(config.ADMIN_PASSWORD))
        self.assertFalse(admin_password.verify("でたらめ"))


class GuardTests(unittest.TestCase):
    """関門そのもの。"""

    def setUp(self) -> None:
        user_settings.set_value(admin_password.KEY, "")
        self.addCleanup(user_settings.set_value, admin_password.KEY, "")

    def test_変えるものが無ければ聞かない(self) -> None:
        self.assertIsNone(admin_password.guard([], ""))

    def test_合っていれば通す(self) -> None:
        self.assertIsNone(admin_password.guard(["line"], config.ADMIN_PASSWORD))

    def test_違えば断る(self) -> None:
        result = admin_password.guard(["line"], "でたらめ")
        self.assertIsNotNone(result)
        self.assertEqual(result.reason, admin_password.REFUSE_WRONG)

    def test_何が要るかは言う(self) -> None:
        """「駄目です」だけでは、何をすればよいのか分からない。"""
        result = admin_password.guard(["line"], "")
        self.assertIn(admin_password.PROTECTED_LABELS["line"], result.message)

    def test_違うときは違うと言う(self) -> None:
        """**ここを言い分けないと、終わりの無い問答になる。**

        1回目とまったく同じ問いが返ると、画面は黙って同じ問いを出し直す。
        利用者からは「永遠に聞かれて設定できない」としか見えず、
        打ち間違えたのか壊れているのかも分からない。
        """
        sent = admin_password.guard(["line"], "でたらめ")
        nothing = admin_password.guard(["line"], "")
        self.assertNotEqual(sent.reason, nothing.reason)
        self.assertIn("違います", sent.message)

    def test_断りに先へ進む道を添える(self) -> None:
        """**行き止まりを作らない。** 初期値を知らないまま繰り返させない。"""
        result = admin_password.guard(["line"], "でたらめ")
        self.assertIn("README", result.message)

    def test_変えてある端末には戻し方を出す(self) -> None:
        """初期値の話は的外れなので、そちらは出さない。"""
        admin_password.change(config.ADMIN_PASSWORD, "現場の合言葉", "現場の合言葉")
        result = admin_password.guard(["line"], "でたらめ")
        self.assertIn("settings.json", result.message)
        self.assertNotIn("README", result.message)


class LineTests(unittest.TestCase):
    """ライン設定 ── VBA 版は誰でもどこでも変えられた。"""

    def setUp(self) -> None:
        _web.reset_sync()
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()
        user_settings.save_my_line("L-1")
        self.addCleanup(user_settings.save_my_line, "")
        user_settings.set_value(admin_password.KEY, "")
        self.addCleanup(user_settings.set_value, admin_password.KEY, "")

    def post(self, **body):
        return self.client.post("/api/settings/line", json=body,
                                headers=_web.auth())

    def test_パスワード無しでは変えられない(self) -> None:
        res = self.post(line="コイル")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.get_json()["error"]["code"], "need_password")
        # **断ったのだから、変わっていない**
        self.assertEqual(user_settings.get_my_line(), "L-1")

    def test_違うパスワードでも変えられない(self) -> None:
        res = self.post(line="コイル", password="でたらめ")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(user_settings.get_my_line(), "L-1")

    def test_違うときは種別で分かる(self) -> None:
        """画面は**文言ではなく `code` で見分ける**(設計 §1 の規則4)。

        送っていないのか違うのかが分かれていないと、画面は同じ問いを
        黙って出し直すしかなくなる。
        """
        self.assertEqual(
            self.post(line="コイル")
                .get_json()["error"]["code"], "need_password")
        self.assertEqual(
            self.post(line="コイル", password="でたらめ")
                .get_json()["error"]["code"], "wrong_password")

    def test_合っていれば変えられる(self) -> None:
        res = self.post(line="コイル", password=_web.ADMIN_PASSWORD)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(user_settings.get_my_line(), "コイル")

    def test_同じラインなら聞かない(self) -> None:
        """変わらないものにパスワードを聞くと「押しても何も起きない」になる。"""
        res = self.post(line="L-1")
        self.assertEqual(res.status_code, 200)

    def test_一覧に無いラインはパスワードより先に断る(self) -> None:
        """順番に意味がある ── 打ち間違いをパスワードのせいにしない。"""
        res = self.post(line="存在しない")
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"], "not_listed")

    def test_変えたパスワードが効く(self) -> None:
        admin_password.change(config.ADMIN_PASSWORD, "現場の合言葉", "現場の合言葉")
        self.assertEqual(self.post(line="コイル",
                                   password=_web.ADMIN_PASSWORD).status_code, 403)
        self.assertEqual(self.post(line="コイル",
                                   password="現場の合言葉").status_code, 200)


class PathTests(unittest.TestCase):
    """参照パス ── 送り先が変わると、その端末の入力だけが迷子になる。"""

    def setUp(self) -> None:
        _web.reset_sync()
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        _web.make_source(self.folder, config.SOURCE_FILE_DATA, _web.SOURCE_SCHEMA)
        self.addCleanup(user_settings.set_value, user_settings.KEY_DATA_DB_DIR, "")
        self.addCleanup(user_settings.set_value, user_settings.KEY_MASTER_DB_DIR, "")
        self.addCleanup(_web.reset_sync)

    def post(self, **body):
        return self.client.post("/api/settings/paths", json=body,
                                headers=_web.auth())

    def test_パスワード無しでは変えられない(self) -> None:
        res = self.post(data_db_dir=str(self.folder))
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.get_json()["error"]["code"], "need_password")
        self.assertEqual(user_settings.data_db_dir_setting(), "")

    def test_合っていれば変えられる(self) -> None:
        res = self.post(data_db_dir=str(self.folder),
                        password=_web.ADMIN_PASSWORD)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(user_settings.data_db_dir_setting(), str(self.folder))

    def test_断ったときは片方も変わらない(self) -> None:
        """関門は**書く前**に置く。でないと、断ったのに一部だけ変わる。"""
        user_settings.set_value(user_settings.KEY_MASTER_DB_DIR, "/前の場所")
        res = self.post(data_db_dir=str(self.folder),
                        master_db_dir=str(self.folder))
        self.assertEqual(res.status_code, 403)
        self.assertEqual(user_settings.data_db_dir_setting(), "")
        self.assertEqual(user_settings.master_db_dir_setting(), "/前の場所")

    def test_同じ場所なら聞かない(self) -> None:
        user_settings.set_value(user_settings.KEY_DATA_DB_DIR, str(self.folder))
        _web.reset_sync()
        self.assertEqual(self.post(data_db_dir=str(self.folder)).status_code, 200)

    def test_無いフォルダはパスワードより先に断る(self) -> None:
        res = self.post(data_db_dir=str(self.folder / "無い"))
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"], "not_found")


class ApiTests(unittest.TestCase):
    """パスワードを変える API。"""

    def setUp(self) -> None:
        _web.reset_sync()
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()
        user_settings.set_value(admin_password.KEY, "")
        self.addCleanup(user_settings.set_value, admin_password.KEY, "")

    def post(self, **body):
        return self.client.post("/api/settings/admin-password", json=body,
                                headers=_web.auth())

    def test_変えられる(self) -> None:
        res = self.post(current=_web.ADMIN_PASSWORD, new="現場の合言葉",
                        confirm="現場の合言葉")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(admin_password.verify("現場の合言葉"))

    def test_値は応答に混ぜない(self) -> None:
        self.post(current=_web.ADMIN_PASSWORD, new="現場の合言葉",
                  confirm="現場の合言葉")
        body = _web.json_of(self.client.get("/api/settings", headers=_web.auth()))
        self.assertNotIn("現場の合言葉", str(body))
        # 変えてあるかどうかだけは出す
        self.assertTrue(body["admin_custom"])

    def test_いまの値が違えば断る(self) -> None:
        res = self.post(current="でたらめ", new="現場の合言葉",
                        confirm="現場の合言葉")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error"]["code"], "wrong_password")

    def test_既定に戻せる(self) -> None:
        self.post(current=_web.ADMIN_PASSWORD, new="現場の合言葉",
                  confirm="現場の合言葉")
        res = self.post(current="現場の合言葉", reset=True)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(admin_password.verify(config.ADMIN_PASSWORD))

    def test_トークンが要る(self) -> None:
        self.assertEqual(
            self.client.post("/api/settings/admin-password", json={}).status_code,
            403)


if __name__ == "__main__":
    unittest.main()
