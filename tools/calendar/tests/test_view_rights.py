"""閲覧権限(カレンダーマスタ.sqlite3)── どの端末に、どのラインの休み・連絡を見せるか (VER4.3.0)

    カレンダー設定部でどのPCがどのラインの閲覧権をもつかを設定できるようにしてほしい
    カレンダーマスタsqlite3 として作ってください。なければ作るが、デフォルト設定値は
    プログラムに直接書いてある設定値
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from calendar_app import config, repository, view_rights
from calendar_app import settings as user_settings
from calendar_app.presenters import calendar as presenter
from calendar_app.repository import record_shown

from tests import _web
from tests.test_line_visibility import EXPECTED, ROWS


def records():
    return [repository.DayRecord(id=i, kubun=k, naiyou=name, code=str(i), shift=s, overtime="",
                                 early="", group="A", line=line)
            for i, (name, k, line, s) in enumerate(ROWS, 1)]


class RuleTests(unittest.TestCase):
    def test_いままでの決まりと同じ(self) -> None:
        rows = [dict(r, 有効=1) for r in view_rights.DEFAULT_ROWS]
        for my_line, want in EXPECTED.items():
            with self.subTest(my_line=my_line):
                rights = view_rights.decide(rows, my_line)
                got = {r.naiyou for r in records()
                       if (rights.rest_shown(r.line, my_line) if r.is_rest else rights.comment_shown(r.line, my_line))}
                self.assertEqual(got, want)
        self.assertTrue(view_rights.decide(rows, "作業長").choose)
        self.assertFalse(view_rights.decide(rows, "L-1").choose)

    def test_書き方(self) -> None:
        self.assertTrue(view_rights.allows("全部", "HVC", "L-1"))
        self.assertTrue(view_rights.allows("自分", "L-1", "L-1"))
        self.assertFalse(view_rights.allows("自分", "HVC", "L-1"))
        self.assertTrue(view_rights.allows("コイル,HVC", "HVC", "L-1"))
        self.assertFalse(view_rights.allows("コイル以外", "コイル", "L-1"))
        self.assertTrue(view_rights.allows("コイル以外", "", "L-1"))
        self.assertFalse(view_rights.allows("全部,HVC以外", "HVC", "L-1"))
        self.assertEqual(view_rights.spec_problem("全部,コイル以外"), "")
        self.assertIn("読めません", view_rights.spec_problem("コイルー"))

    def test_端末の行が先(self) -> None:
        rows = [dict(r, 有効=1) for r in view_rights.DEFAULT_ROWS]
        rows.append({"PC名": "coil-pc", "休みを見るライン": "全部", "連絡を見るライン": "コイル,HVC",
                     "宛先を選べる": 1, "有効": 1})
        rights = view_rights.decide(rows, "コイル", "who", "COIL-PC")
        self.assertTrue(rights.rest_shown("L-1", "コイル"))
        self.assertTrue(rights.comment_shown("HVC", "コイル"))
        self.assertTrue(rights.choose)
        self.assertEqual(view_rights.decide(rows, "コイル", "who", "OTHER").rest, "コイル")


class FileTests(unittest.TestCase):
    def setUp(self) -> None:
        _web.reset_sync()
        _web.with_source(self)
        self.conn = _web.bind_db(self)
        self.addCleanup(user_settings.save_my_line, "")

    def test_無ければいままでの決まりで作る(self) -> None:
        target = view_rights.path()
        self.assertFalse(target.exists())
        got = {r.naiyou for r in records() if record_shown(r, "コイル")}
        self.assertEqual(got, EXPECTED["コイル"])
        self.assertTrue(target.exists(), "読むときに作る")
        conn = sqlite3.connect(target)
        lines = [r[0] for r in conn.execute(f'SELECT "ライン" FROM "{view_rights.TABLE}"')]
        conn.close()
        self.assertEqual(lines, ["作業長", "コイル", "その他"])

    def test_表を直すとコードを変えずに見え方が変わる(self) -> None:
        view_rights.ensure()
        conn = sqlite3.connect(view_rights.path())
        with conn:
            conn.execute(f'UPDATE "{view_rights.TABLE}" SET "休みを見るライン"=\'全部\', '
                         '"連絡を見るライン"=\'全部\', "宛先を選べる"=1 WHERE "ライン"=\'コイル\'')
        conn.close()
        view_rights.reset()
        got = {r.naiyou for r in records() if record_shown(r, "コイル")}
        self.assertEqual(got, {r[0] for r in ROWS})
        self.assertTrue(presenter.can_choose_comment_line("コイル"))

    def test_マスタ管理で直す_読めない書き方は断る(self) -> None:
        from calendar_app import master_admin

        ready = master_admin.readiness(view_rights.TABLE)
        self.assertTrue(ready.ok, ready.why)
        self.assertIn("読めません", master_admin._check_view({"休みを見るライン": "HVCX"}, False))
        self.assertEqual(master_admin._check_view({"休みを見るライン": "コイル以外", "有効": ""}, False), "")


if __name__ == "__main__":
    unittest.main()
