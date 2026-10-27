"""管理者パスワードの既定の値(日報・カレンダー・点検表と同じ値で開く)。

    看板の管理者パスワードには既定の値がありません
    → 日報・カレンダー・点検表と同じ既定の値にする

以前の看板は、``admin_password_hash`` が空の端末では**最初に鍵を開けようとした人が、
その場で新しいパスワードを決められました**(配布設定にパスワードを入れずに配った先でも)。
いまは決めていない端末は、ほかの3ツールと同じ既定の値で開きます。

**既定の値そのものは、この試験にも書きません。** ほかの3ツールのコードから、試験のたびに
読み出して使います(3つが同じ値であることも、ここで確かめる)。看板のコードには撹拌した
値だけを置く(``kanban.config.DEFAULT_ADMIN_PASSWORD_HASH``)ので、平文で書いていないことも
確かめます。
"""
from __future__ import annotations

import ast
import os
import unittest
from pathlib import Path
from unittest import mock

from kanban import config
from tests.test_web_app import HAS_FLASK, RouteTestBase

TOOLS = Path(__file__).resolve().parents[2]
KANBAN = TOOLS / "kanban"


def _env_default(path: Path, name: str) -> str:
    """``NAME = os.environ.get("...", "<既定>")`` の既定を読む。"""
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == name for t in node.targets):
            return node.value.args[1].value
    raise LookupError(f"{path}: {name}")


def _property_default(path: Path, name: str) -> str:
    """``def <name>(self): return "<既定>"`` の値を読む(日報の ``Settings.admin_password``)。"""
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            for inner in ast.walk(node):
                if isinstance(inner, ast.Return) and isinstance(inner.value, ast.Constant):
                    return inner.value.value
    raise LookupError(f"{path}: {name}")


def shared_defaults() -> dict[str, str]:
    """ほかの3ツールの既定の値(ツールの名前 → 値)。一式が無ければ飛ばす。"""
    places = {
        "日報": (TOOLS / "nippou" / "nippou" / "config.py", _property_default, "admin_password"),
        "カレンダー": (TOOLS / "calendar" / "calendar_app" / "config.py", _env_default, "ADMIN_PASSWORD"),
        "点検表": (TOOLS / "inspection" / "app" / "services" / "admin_password.py", _env_default, "DEFAULT"),
    }
    missing = [str(path) for path, _read, _name in places.values() if not path.is_file()]
    if missing:
        raise unittest.SkipTest(f"日報複合ツールの一式の中でないので比べられません: {missing}")
    return {tool: read(path, name) for tool, (path, read, name) in places.items()}


class _NoEnv(unittest.TestCase):
    """既定の値を変える環境変数を外して試す(試験の土台は「秘密」に変えているため)。"""

    def setUp(self) -> None:
        super().setUp()
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(config.ADMIN_PASSWORD_ENV, None)
        values = shared_defaults()
        self.assertEqual(len(set(values.values())), 1, "ほかの3ツールの既定の値がそろっていない")
        self.shared = next(iter(values.values()))


class DefaultValueTest(_NoEnv):
    def test_決めていない端末は_ほかの3ツールと同じ既定の値で開く(self) -> None:
        self.assertTrue(config.verify_admin_password(self.shared, ""))

    def test_違う値や空では開かない(self) -> None:
        self.assertFalse(config.verify_admin_password("でたらめ", ""))
        self.assertFalse(config.verify_admin_password("", ""))
        self.assertFalse(config.verify_admin_password(self.shared + " ", ""))

    def test_この端末で決めた値があれば_既定の値では開かない(self) -> None:
        own = config.hash_password("この端末の値")
        self.assertTrue(config.verify_admin_password("この端末の値", own))
        self.assertFalse(config.verify_admin_password(self.shared, own), "決めたあとも既定の値で開く")

    def test_配布設定の値が既定の値より先(self) -> None:
        """配った値(配布設定で入った ``admin_password_hash``)→ 既定の値 の順(ほかのツールと同じ)。"""
        from kanban import distribution

        self.assertIn("admin_password_hash", distribution.ITEM_KEYS)
        cfg = config.Config(admin_password_hash=config.hash_password("配った値"))
        self.assertTrue(config.verify_admin_password("配った値", cfg.admin_password_hash))
        self.assertFalse(config.verify_admin_password(self.shared, cfg.admin_password_hash))

    def test_環境変数で既定の値を変えられる(self) -> None:
        with mock.patch.dict(os.environ, {config.ADMIN_PASSWORD_ENV: "起動で決めた値"}):
            self.assertTrue(config.verify_admin_password("起動で決めた値", ""))
            self.assertFalse(config.verify_admin_password(self.shared, ""))

    def test_看板のコードに既定の値を平文で書いていない(self) -> None:
        found = []
        for folder in ("kanban", "app"):
            for path in sorted((KANBAN / folder).rglob("*")):
                if path.suffix not in (".py", ".js", ".html") or "__pycache__" in path.parts:
                    continue
                text = path.read_text(encoding="utf-8")
                for quoted in (f'"{self.shared}"', f"'{self.shared}'"):
                    if quoted in text:
                        found.append(str(path.relative_to(KANBAN)))
        for name in ("start_app.py", "main.py", "server.py", "bridge.py", "export_distribution.py"):
            path = KANBAN / name
            if path.is_file() and any(q in path.read_text(encoding="utf-8")
                                      for q in (f'"{self.shared}"', f"'{self.shared}'")):
                found.append(name)
        self.assertEqual(found, [], "既定の値を平文で書いている(撹拌した値だけを置く)")


@unittest.skipUnless(HAS_FLASK, "Flask が入っていない")
class DefaultOverRoutesTest(RouteTestBase):
    """経路から: 決めていない端末で、最初に入れた値がパスワードにならない。"""

    def setUp(self) -> None:
        super().setUp()
        os.environ.pop(config.ADMIN_PASSWORD_ENV, None)    # 土台の「秘密」を外す(片付けで戻る)
        values = shared_defaults()
        self.assertEqual(len(set(values.values())), 1)
        self.shared = next(iter(values.values()))

    def stored(self) -> str:
        return config.load_config(self.dir / "config.json").admin_password_hash

    def test_最初に入れた値では開かず_パスワードにもならない(self) -> None:
        res = self.post("/api/line", {"line": "LS", "password": "はじめて", "password_confirm": "はじめて"})
        self.assertEqual(res.status_code, 401, res.json)
        self.assertEqual(res.json["error"]["code"], "bad_password")
        self.assertEqual(self.stored(), "", "最初に入れた値でパスワードが決まった")
        self.assertEqual(self.post("/api/admin/unlock", {"password": "はじめて"}).status_code, 401)

    def test_既定の値で開き_変えるにも既定の値が要る(self) -> None:
        self.assertFalse(self.get("/api/settings").json["admin_password_custom"])
        self.assertEqual(self.post("/api/admin/unlock", {"password": self.shared}).status_code, 200)
        body = {"new_password": "あたらしい", "new_password_confirm": "あたらしい"}
        self.assertEqual(self.post("/api/admin/password", body).status_code, 401,
                         "今のパスワード(既定の値)無しで変えられた")
        res = self.post("/api/admin/password", {**body, "current_password": self.shared})
        self.assertEqual(res.status_code, 200, res.json)
        self.assertTrue(config.verify_password("あたらしい", self.stored()))
        self.assertEqual(self.post("/api/admin/verify", {"password": self.shared}).status_code, 401,
                         "変えたあとも既定の値で開く")


if __name__ == "__main__":
    unittest.main()
