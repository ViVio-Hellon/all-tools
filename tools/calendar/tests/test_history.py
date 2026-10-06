"""履歴表示 ── 過去の月を、編集できない形で見せる

【見張ること】
1. 選べるのは**登録がある最も古い月から先月まで**(一覧に無いものは選べない)
2. 途中の登録が無い月も選べる(0件も記録)
3. 今月以降・登録より前は、画面を通さずに叩いても断る
4. **書き込む道が無い** ── API にも画面のスクリプトにも
5. 日の中身は読める。削除に使う印(ID・中身の印)は返さない
6. マス目はカレンダー画面と同じ組み立て(見え方が食い違わない)
"""

from __future__ import annotations

import datetime as _dt
import re
import unittest
from pathlib import Path

from . import _web
from calendar_app import config, repository
from calendar_app.presenters import calendar as cal
from calendar_app.presenters import history

_ROOT = Path(__file__).resolve().parent.parent


def _months_ago(n: int) -> tuple[int, int]:
    today = _dt.date.today()
    return cal.shift_month(today.year, today.month, -n)


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        _web.reset_sync()
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()
        self.repo = repository.Repository(self.conn)

    def add(self, months_ago: int, day: int = 10, name: str = "山田太郎") -> None:
        year, month = _months_ago(months_ago)
        self.repo.save_record(_dt.date(year, month, day), config.KUBUN_REST,
                              name, "10", shift="1", group="A", line="コイル")

    def get(self, url: str):
        return self.client.get(url, headers=_web.auth())


class ChoiceTests(_Base):
    def test_登録が無ければ選べる月は無い(self) -> None:
        listed = history.choices(self.conn)
        self.assertEqual(listed["years"], [])
        self.assertIsNone(listed["latest"])
        self.assertIn("ありません", listed["message"])

    def test_最も古い月から先月まで(self) -> None:
        self.add(5)
        self.add(2)
        self.add(0)                                 # 今月の分は数えない
        listed = history.choices(self.conn)
        self.assertEqual((listed["earliest"]["year"], listed["earliest"]["month"]),
                         _months_ago(5))
        self.assertEqual((listed["latest"]["year"], listed["latest"]["month"]),
                         _months_ago(1))
        selectable = {(y["year"], m["month"]) for y in listed["years"]
                      for m in y["months"] if m["selectable"]}
        self.assertEqual(selectable, {_months_ago(n) for n in range(1, 6)})

    def test_登録の無い月も選べて0件と出る(self) -> None:
        """「その月は誰も休んでいない」も記録。見られないと困る。"""
        self.add(3)
        listed = history.choices(self.conn)
        year, month = _months_ago(2)
        tile = next(m for y in listed["years"] if y["year"] == year
                    for m in y["months"] if m["month"] == month)
        self.assertTrue(tile["selectable"])
        self.assertEqual(tile["count"], 0)

    def test_件数は月ごと(self) -> None:
        self.add(2, day=3)
        self.add(2, day=4, name="鈴木一郎")
        year, month = _months_ago(2)
        tile = next(m for y in history.choices(self.conn)["years"]
                    if y["year"] == year for m in y["months"] if m["month"] == month)
        self.assertEqual(tile["count"], 2)


class ApiTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.add(4)
        self.add(1)

    def test_年月を省けば先月(self) -> None:
        body = _web.json_of(self.get("/api/history"))
        month = body["month"]
        self.assertEqual((month["year"], month["month"]), _months_ago(1))
        self.assertTrue(month["readonly"])
        self.assertFalse(month["can_next"], "先月の次(今月)へは進ませない")
        self.assertTrue(month["can_prev"])
        self.assertEqual(month["count"], 1)

    def test_最も古い月より前へは戻れない(self) -> None:
        year, month = _months_ago(4)
        body = _web.json_of(self.get(f"/api/history?year={year}&month={month}"))
        self.assertFalse(body["month"]["can_prev"])

    def test_今月は断る(self) -> None:
        """今月はまだ動いている。カレンダー画面で見る。"""
        today = _dt.date.today()
        res = self.get(f"/api/history?year={today.year}&month={today.month}")
        self.assertEqual(res.status_code, 422)
        self.assertEqual(_web.json_of(res)["error"]["code"], history.REFUSE_NOT_LISTED)

    def test_未来と登録より前も断る(self) -> None:
        for n in (-3, 12):
            year, month = _months_ago(n)
            res = self.get(f"/api/history?year={year}&month={month}")
            self.assertEqual(res.status_code, 422, n)

    def test_形がおかしければ400(self) -> None:
        self.assertEqual(self.get("/api/history?year=abc&month=1").status_code, 400)
        self.assertEqual(self.get("/api/history?year=2020&month=13").status_code, 422)

    def test_マス目はカレンダー画面と同じ(self) -> None:
        year, month = _months_ago(1)
        mine = _web.json_of(self.get(f"/api/history?year={year}&month={month}"))["month"]
        theirs = _web.json_of(self.get(f"/api/calendar?year={year}&month={month}"))
        self.assertEqual(mine["cells"], theirs["cells"])
        self.assertEqual(mine["weekdays"], theirs["weekdays"])

    def test_日の中身は読めて_削除の印は返さない(self) -> None:
        year, month = _months_ago(1)
        body = _web.json_of(self.get(f"/api/history/day/{year:04d}/{month:02d}/10"))
        self.assertEqual(body["count"], 1)
        self.assertIn("山田太郎", body["detail"])
        self.assertNotIn("items", body)
        self.assertNotIn("id", str(body.keys()))

    def test_今月の日は断る(self) -> None:
        today = _dt.date.today()
        res = self.get(f"/api/history/day/{today.strftime(config.DATE_KEY_FORMAT)}")
        self.assertEqual(res.status_code, 422)

    def test_登録が無ければ月は返らない(self) -> None:
        self.conn.execute(f'DELETE FROM "{config.TABLE_DATA}"')
        self.conn.commit()
        body = _web.json_of(self.get("/api/history"))
        self.assertIsNone(body["month"])
        self.assertIn("ありません", body["choices"]["message"])


class ReadOnlyTests(unittest.TestCase):
    """**書き込む道が無い**こと。"""

    def test_履歴のAPIは読み取りだけ(self) -> None:
        from app import create_app

        app = create_app(token="t")
        rules = [r for r in app.url_map.iter_rules() if "history" in r.rule]
        self.assertTrue(rules)
        for rule in rules:
            self.assertEqual(rule.methods - {"GET", "HEAD", "OPTIONS"}, set(), rule.rule)

    def test_画面のスクリプトは書き込むAPIを呼ばない(self) -> None:
        js = (_ROOT / "app" / "static" / "js" / "views" / "history.js").read_text(
            encoding="utf-8")
        self.assertNotIn("api.post", js)
        for path in ("/api/rest", "/api/comment", "/api/delete", "/api/day/"):
            self.assertNotIn(path, js, path)

    def test_画面にもボタンが無い(self) -> None:
        html = (_ROOT / "app" / "templates" / "history.html").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r">\s*(休み|削除|コメント・連絡)\s*<", html))
        self.assertIn("閲覧のみ", html)


class LookTests(unittest.TestCase):
    """**現行のカレンダーと見た目を変える**(取り違えない)。"""

    def setUp(self) -> None:
        css = _ROOT / "app" / "static" / "css"
        self.tokens = (css / "tokens.css").read_text(encoding="utf-8")
        self.components = (css / "components.css").read_text(encoding="utf-8")
        self.html = (_ROOT / "app" / "templates" / "history.html").read_text(
            encoding="utf-8")

    def test_履歴の色は明暗どちらにもある(self) -> None:
        """明るい表示・OSのダーク・明示のダークの3か所。"""
        self.assertEqual(self.tokens.count("--hist-accent:"), 3)
        self.assertEqual(self.tokens.count("--hist-day-bg:"), 3)

    def test_マス目の色を履歴の色に差し替える(self) -> None:
        block = self.components.split(".cal--history{", 1)[1].split("}", 1)[0]
        for token in ("--day-bg:var(--hist-day-bg)", "--head-bg:var(--hist-head-bg)",
                      "--day-sun-bg:var(--hist-day-sun-bg)",
                      "--day-sat-bg:var(--hist-day-sat-bg)"):
            self.assertIn(token, block)
        # 色はトークンからだけ取る(直書きしない)
        self.assertNotRegex(block, r"#[0-9a-fA-F]{3,6}")

    def test_色だけで運ばない(self) -> None:
        """文字でも「履歴・閲覧のみ」と出す。"""
        self.assertIn('class="cal cal--history"', self.html)
        self.assertIn("履歴・閲覧のみ", self.html)


class NavTests(_Base):
    def test_レールに履歴がある(self) -> None:
        res = self.get("/history")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn('href="/history"', html)
        self.assertIn("編集不可", html)
        self.assertRegex(html, r'href="/history"\s+aria-current="page"')


if __name__ == "__main__":
    unittest.main()
