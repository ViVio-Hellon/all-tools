"""見せる登録の決まりは1つ ── マス目・マウスを乗せたとき・内容閲覧・削除 (VER4.2.6)

    3つともマス目と同じ決まりでそろえてください
    (マウスを乗せたときの休み・内容閲覧・削除の選択肢が、ラインで絞られていなかった)

【決まり】(`repository.record_shown`)
    作業長             : 休み・連絡とも全ライン
    コイル             : 休みはコイルだけ、連絡はコイル宛だけ
    コイル以外の各ライン : 休みはコイル以外全部、連絡は自分のライン宛だけ

    削除は、この端末に見えない登録をサーバが断る(古い画面・手で組んだ要求でも通さない)
"""
from __future__ import annotations

import datetime as _dt
import unittest

from calendar_app import config, repository
from calendar_app import settings as user_settings
from calendar_app.presenters import calendar as presenter
from calendar_app.repository import build_day_text, build_day_tooltip, record_shown

from tests import _web

DAY = _dt.date(2026, 10, 21)
D = DAY.strftime(config.DATE_KEY_FORMAT)

#: その日の登録(名前, 区分, ライン, 直)
ROWS = (
    ("小林コイル", config.KUBUN_REST, "コイル", "1"),
    ("山田エル", config.KUBUN_REST, "L-1", "1"),
    ("鈴木エイチ", config.KUBUN_REST, "HVC", "2"),
    ("コイル宛の連絡", config.KUBUN_OTHER, "コイル", ""),
    ("L-1宛の連絡", config.KUBUN_OTHER, "L-1", ""),
    ("HVC宛の連絡", config.KUBUN_OTHER, "HVC", ""),
)

#: 端末のライン → 見える登録
EXPECTED = {
    config.LINE_SUPERVISOR: {r[0] for r in ROWS},
    "コイル": {"小林コイル", "コイル宛の連絡"},
    "L-1": {"山田エル", "鈴木エイチ", "L-1宛の連絡"},
    "HVC": {"山田エル", "鈴木エイチ", "HVC宛の連絡"},
}


class RuleTests(unittest.TestCase):
    def records(self):
        return [repository.DayRecord(id=i, kubun=k, naiyou=name, code=str(i), shift=s, overtime="",
                                     early="", group="A", line=line)
                for i, (name, k, line, s) in enumerate(ROWS, 1)]

    def test_端末のラインごとに見える登録(self) -> None:
        for my_line, want in EXPECTED.items():
            with self.subTest(my_line=my_line):
                got = {r.naiyou for r in self.records() if record_shown(r, my_line)}
                self.assertEqual(got, want)

    def test_マウスを乗せたときの休みもマス目と同じ(self) -> None:
        """前は休みの繋ぎ(残業・早出)が全ラインの分出ていた。"""
        for my_line, want in EXPECTED.items():
            with self.subTest(my_line=my_line):
                tip = build_day_tooltip(self.records(), my_line)
                cell = build_day_text(self.records(), my_line)
                for name in ("小林コイル", "山田エル", "鈴木エイチ"):
                    self.assertEqual(name in tip, name in want, (name, tip))
                    self.assertEqual(name in cell, name in want, (name, cell))


class WebTests(unittest.TestCase):
    def setUp(self) -> None:
        _web.reset_sync()
        _web.with_source(self)
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()
        self.addCleanup(user_settings.save_my_line, "")
        repo = repository.Repository(self.conn)
        self.ids = {name: repo.save_record(DAY, kubun, name, str(i), shift=shift, group="A", line=line)
                    for i, (name, kubun, line, shift) in enumerate(ROWS, 1)}

    def day(self, my_line: str) -> dict:
        user_settings.save_my_line(my_line)
        return _web.json_of(self.client.get(f"/api/day/{D}", headers=_web.auth()))

    def test_内容閲覧と削除の選択肢はマス目と同じ(self) -> None:
        for my_line, want in EXPECTED.items():
            with self.subTest(my_line=my_line):
                body = self.day(my_line)
                for name, *_ in ROWS:
                    self.assertEqual(name in body["detail"], name in want, (name, body["detail"]))
                shown = {item["id"] for item in body["items"]}
                self.assertEqual(shown, {self.ids[n] for n in want})
                # マス目と同じ
                month = _web.json_of(self.client.get("/api/calendar?year=2026&month=10", headers=_web.auth()))
                cell = next(c for c in month["cells"] if c["date"] == D)
                for name in ("小林コイル", "山田エル", "鈴木エイチ"):
                    self.assertEqual(name in cell["body"], name in want)
                    self.assertEqual(name in cell["tip"], name in want)

    def test_登録の件数も見えるものだけ(self) -> None:
        """内容閲覧の「登録 N 件」・マスの読み上げ・履歴の月の件数は、マス目と同じ決まりで数える。"""
        from calendar_app.presenters import history
        repo = repository.Repository(self.conn)
        past = _dt.date(2026, 9, 10)
        for i, (name, kubun, line, shift) in enumerate(ROWS, 1):
            repo.save_record(past, kubun, name, str(i), shift=shift, group="A", line=line)
        for my_line, want in EXPECTED.items():
            with self.subTest(my_line=my_line):
                user_settings.save_my_line(my_line)
                month = _web.json_of(self.client.get("/api/calendar?year=2026&month=10", headers=_web.auth()))
                cell = next(c for c in month["cells"] if c["date"] == D)
                self.assertEqual(cell["count"], len(want))
                self.assertEqual(cell["count"], len(self.day(my_line)["items"]), "内容閲覧の中身と同じ数")
                tile = next(m for y in history.choices(self.conn, _dt.date(2026, 10, 23))["years"]
                            if y["year"] == 2026 for m in y["months"] if m["month"] == 9)
                self.assertEqual(tile["count"], len(want))

    def delete(self, my_line: str, name: str):
        user_settings.save_my_line(my_line)
        return self.client.post("/api/delete", json={"date": D, "ids": [self.ids[name]]},
                                headers=_web.auth())

    def left(self) -> set[str]:
        return {r.naiyou for r in repository.Repository(self.conn).get_day_records(DAY)}

    def test_見えない登録は削除をサーバが断る(self) -> None:
        res = self.delete("L-1", "小林コイル")
        self.assertEqual(res.status_code, 403)
        body = res.get_json()
        self.assertEqual(body["error"]["code"], "not_your_line")
        self.assertIn("ほかのラインの登録は削除できません", body["error"]["message"])
        self.assertIn("小林コイル", self.left(), "消えてはいけない")
        res = self.delete("L-1", "HVC宛の連絡")
        self.assertEqual(res.status_code, 403)
        self.assertIn("HVC宛の連絡", self.left())

    def test_見える登録は消せる_作業長はどれでも(self) -> None:
        self.assertEqual(self.delete("L-1", "鈴木エイチ").status_code, 200, "コイル以外の休みは見えるので消せる")
        self.assertEqual(self.delete("コイル", "小林コイル").status_code, 200)
        self.assertEqual(self.delete(config.LINE_SUPERVISOR, "HVC宛の連絡").status_code, 200)
        self.assertEqual(self.left(), {"山田エル", "コイル宛の連絡", "L-1宛の連絡"})

    def test_ひとつでも見えない登録が混ざれば何も消さない(self) -> None:
        user_settings.save_my_line("L-1")
        res = self.client.post("/api/delete", json={"date": D, "ids": [self.ids["山田エル"], self.ids["小林コイル"]]},
                               headers=_web.auth())
        self.assertEqual(res.status_code, 403)
        self.assertEqual(self.left(), {r[0] for r in ROWS})


if __name__ == "__main__":
    unittest.main()
