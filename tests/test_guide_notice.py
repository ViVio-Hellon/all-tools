"""案内表示 ── 日報複合ツール全体の機能(入口・大設定 ── 1.5.104)

    日報をアクセス権限で表示していないラインにたいして何も表示できなくなっている
    日報の機能ではなく all-tools 全体の機能として扱って
    大設定で 案内表示マスタの編集をできるようにし、案内は日報をアクセス権限で非表示していても表示できるように

表は共有の DB(既定 梱包資材マスタ)の「案内表示」。ラインは大設定 → アクセス権限の表。直の時刻は同じ
フォルダの伝送用ファイルの「時間用」(無ければ既定)。押した記録は 共有の DB のフォルダ\\案内の確認\\<ライン>\\。
2026-11-09 は月曜。既定の直: 1直 7:00〜15:00 / 2直 15:00〜22:50 / 3直 22:50〜7:00。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

import tests  # noqa: F401  (一時フォルダへ向ける)

from portal import guide_notice as rule
from portal import guide_store, user_settings
from tests.test_web import Base

SHARE = Path(os.environ["ALLTOOLS_SHARED_DB_DIR"])
MON, TUE, WED, THU, FRI, SAT, SUN = 9, 10, 11, 12, 13, 14, 15


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute) if day <= 30 else datetime(2026, 12, day - 30, hour, minute)


def label(day: int) -> str:
    return rule.report_date(at(day, 0).date())


def row(**kw):
    return {**rule.FIRST_ROW, **kw}


class RuleTests(unittest.TestCase):
    def hits(self, rows, now, *, line="機側", acked=(), since=None):
        slots = rule.shift_slots(rule.DEFAULT_TIMES, now)
        return [(h.notice.id, h.slot.report_date, h.slot.shift, h.carried)
                for h in rule.due_all(rule.parse(rows), line=line, now=now, slots=slots,
                                      acked=acked, since=since)]

    def test_機側の2直の始まりに出る_ほかのライン_ほかの直では出ない(self) -> None:
        self.assertEqual(self.hits([row()], at(MON, 15, 1)), [("計量器の点検", label(MON), "2直", False)])
        self.assertEqual(self.hits([row()], at(MON, 15, 1), line="L-1"), [])
        self.assertEqual(self.hits([row(過ぎても出す=0)], at(MON, 9)), [])

    def test_3直は始めた日(self) -> None:
        self.assertEqual(self.hits([row(直="3直")], at(TUE, 3)), [("計量器の点検", label(MON), "3直", False)])

    def test_時間用の行を読む(self) -> None:
        times = rule.shift_times([{"直": "2", "開始": "15:10", "終了": "23:00"}, {"直": "x", "開始": "1"}])
        self.assertEqual(times["2直"][0].strftime("%H:%M"), "15:10")
        self.assertEqual(times["1直"], rule.DEFAULT_TIMES["1直"])

    def test_選べる値以外は断る(self) -> None:
        self.assertIn("注意 ・ 警告 ・ 案内", rule.problem_of({"色": "紫"}))
        self.assertEqual(rule.problem_of({"色": "警告", "曜日": "月水金"}), "")


class StoreCase(Base):
    """共有の DB に案内表示の表がある入口。この端末(TEST-PC)のラインは機側(大設定)。"""

    line = "機側"

    def setUp(self) -> None:
        super().setUp()
        guide_store.reset()
        self.addCleanup(guide_store.reset)
        user_settings.update({guide_store.KEY_LINE: self.line or None})
        self.addCleanup(user_settings.update, {guide_store.KEY_LINE: None})
        local = guide_store.app_config.local_dir("data") / "guide_notice.sqlite3"
        if local.exists():
            local.unlink()
        records = SHARE / guide_store.RECORD_DIR
        shutil.rmtree(records, ignore_errors=True)
        for name in guide_store.TRANSMISSION_NAMES:
            (SHARE / name).unlink(missing_ok=True)
        self.when = at(MON, 6)
        for name in ("due_now", "checks"):
            real = getattr(guide_store, name)
            patcher = mock.patch.object(
                guide_store, name,
                side_effect=lambda *a, _real=real, **kw: _real(*a, **{**kw, "now": kw.get("now") or self.when}))
            patcher.start()
            self.addCleanup(patcher.stop)

    def rows(self, *rows: dict, seen: datetime = datetime(2026, 11, 8, 6, 0)) -> None:
        """表の中身をこの行にする。案内は `seen`(既定: 前の日曜の朝)に作った。"""
        path = guide_store.shared_path()
        guide_store.ensure_table(path)
        conn = sqlite3.connect(path)
        conn.execute(f'DELETE FROM "{rule.TABLE}"')
        for r in rows:
            values = {**rule.FIRST_ROW, **r}
            conn.execute(f'INSERT INTO "{rule.TABLE}" ({", ".join(chr(34) + k + chr(34) for k in values)})'
                         f' VALUES ({", ".join("?" * len(values))})', list(values.values()))
        conn.commit()
        conn.close()
        guide_store.reset()
        guide_store.first_seen(rule.parse(rows and [{**rule.FIRST_ROW, **r} for r in rows]), seen)

    def shown(self, when: datetime):
        self.when = when
        body = self.get("/api/notice/due").get_json()
        if not body["notice"]:
            return None
        return (body["notice"]["id"], body["report_date"], body["shift"], body["carried"])

    def press(self, when: datetime, worker: str = "山田"):
        got = self.shown(when)
        self.assertIsNotNone(got, f"{when} に出るものが無い")
        res = self.post("/api/notice/ack", {"id": got[0], "worker": worker,
                                            "report_date": got[1], "shift": got[2]})
        self.assertEqual(res.status_code, 200, res.get_json())
        return got

    def queue(self, when: datetime) -> list:
        out = []
        for _ in range(10):
            if self.shown(when) is None:
                return out
            out.append(self.press(when))
        self.fail("押しても出続ける")

    def csv_text(self, line: str = "機側") -> str:
        files = sorted((SHARE / guide_store.RECORD_DIR / line).glob("*.csv"))
        return "".join(p.read_text(encoding="utf-8-sig") for p in files)


class StoreTests(StoreCase):
    def test_表が無ければ作り_頼まれた案内を入れる_2直に出る(self) -> None:
        body = self.get("/api/notice/settings").get_json()
        self.assertEqual([r["ID"] for r in body["rows"]], ["計量器の点検"])
        self.assertEqual(body["line"]["line"], "機側")
        self.assertEqual(body["line"]["origin"], "大設定")
        guide_store.first_seen(rule.parse([rule.FIRST_ROW]), at(MON, 6))
        self.when = at(MON, 15, 1)
        got = self.get("/api/notice/due").get_json()
        self.assertEqual(got["notice"]["text"], "計量器の点検を行ってください")
        self.assertEqual(got["notice"]["button"], "点検済み")
        self.assertEqual(got["roster"], [], "名簿が無ければ空(打てる)")

    def test_名前が無ければ押せない_押せば共有に記録_消せばまた出る(self) -> None:
        self.rows({"ID": "点検"})
        got = self.shown(at(MON, 15, 1))
        res = self.post("/api/notice/ack", {"id": "点検", "report_date": got[1], "shift": "2直"})
        self.assertEqual(res.status_code, 422)
        self.press(at(MON, 15, 1))
        self.assertIsNone(self.shown(at(MON, 15, 2)))
        text = self.csv_text()
        self.assertIn("報告日,直,ライン,案内,押した人,押した時刻,PC,押した直", text)
        self.assertIn(f"{label(MON)},2直,機側,点検,山田", text)
        for p in (SHARE / guide_store.RECORD_DIR / "機側").glob("*.csv"):
            p.unlink()
        self.assertIsNotNone(self.shown(at(MON, 15, 3)), "CSV を消すとまた出る")

    def test_共有に届かなければこの端末の記録で決める(self) -> None:
        self.rows({"ID": "点検"})
        self.press(at(MON, 15, 1))
        with mock.patch.object(guide_store, "_shared_acks", return_value=None):
            self.assertIsNone(self.shown(at(MON, 15, 2)))

    def test_ラインはアクセス権限の表からも決まる(self) -> None:
        user_settings.update({guide_store.KEY_LINE: None})
        conn = sqlite3.connect(guide_store.shared_path())
        conn.execute('CREATE TABLE "アクセス権限" ("ログインID" TEXT, "PC名" TEXT, "権限" TEXT, "有効" INTEGER)')
        conn.execute('INSERT INTO "アクセス権限" VALUES (\'\', \'TEST-PC\', \'機側,mode:field\', 1)')
        conn.execute('INSERT INTO "アクセス権限" VALUES (\'\', \'OTHER-PC\', \'L-1\', 1)')
        conn.commit()
        conn.close()
        guide_store.reset()
        choice = guide_store.terminal_line()
        self.assertEqual((choice.line, choice.origin), ("機側", "アクセス権限"))

    def test_ラインが決まらなければ出さず_わけを出す(self) -> None:
        user_settings.update({guide_store.KEY_LINE: None})
        guide_store.reset()
        self.rows({"ID": "点検", "ライン": ""})
        self.assertIsNone(self.shown(at(MON, 15, 1)))
        self.assertIn("大設定でこの端末のラインを決める", guide_store.terminal_line().why)

    def test_直の時刻は同じフォルダの伝送用ファイルの時間用(self) -> None:
        conn = sqlite3.connect(SHARE / "伝送用ファイル.sqlite3")
        conn.execute('CREATE TABLE "時間用" ("直" TEXT, "開始" TEXT, "終了" TEXT)')
        conn.execute('INSERT INTO "時間用" VALUES (\'2\', \'16:00\', \'23:00\')')
        conn.commit()
        conn.close()
        guide_store.reset()
        self.rows({"ID": "点検", "過ぎても出す": 0})
        self.assertIsNone(self.shown(at(MON, 15, 30)), "2直はこの表では 16:00 から")
        self.assertIsNotNone(self.shown(at(MON, 16, 1)))
        body = self.get("/api/notice/settings").get_json()
        self.assertEqual(body["times"]["2直"], "16:00〜23:00")
        self.assertIn("伝送用ファイル", body["times_from"])

    def test_班員名簿から名前を選ぶ(self) -> None:
        conn = sqlite3.connect(guide_store.shared_path())
        conn.execute('CREATE TABLE "班員名簿" ("名前" TEXT, "班" TEXT)')
        conn.executemany('INSERT INTO "班員名簿" VALUES (?, ?)', [("山田", "A"), ("佐藤", "A"), ("鈴木", "B")])
        conn.commit()
        conn.close()
        guide_store.reset()
        self.rows({"ID": "点検"})
        self.when = at(MON, 15, 1)
        body = self.get("/api/notice/due").get_json()
        self.assertEqual(body["roster"], [{"team": "A", "names": ["山田", "佐藤"]},
                                          {"team": "B", "names": ["鈴木"]}])

    def test_大設定で足す_直す_消す_選べる値以外は断る_鍵が要る(self) -> None:
        self.get("/api/notice/settings")
        self.assertEqual(self.post("/api/notice/add", {"values": {"ID": "x", "表示文": "x"}}).status_code, 403)
        self.unlock()
        res = self.post("/api/notice/add", {"values": {"ID": "x", "表示文": "x", "色": "紫"}})
        self.assertEqual(res.status_code, 400)
        self.assertIn("注意 ・ 警告 ・ 案内", res.get_json()["error"]["message"])
        res = self.post("/api/notice/add", {"values": {"ID": "夜の点検", "表示文": "夜", "色": "警告", "直": "3直"}})
        self.assertEqual(res.status_code, 200, res.get_json())
        body = res.get_json()
        new = next(r for r in body["rows"] if r["ID"] == "夜の点検")
        cols = {c["name"]: c for c in body["columns"]}
        self.assertEqual(cols["色"]["choices"], ["注意", "警告", "案内"])
        self.assertIn("警告 = 赤", cols["色"]["hint"])
        key = new[body["row_key"]]
        was = {c["name"]: new.get(c["name"], "") for c in body["columns"]}
        res = self.post("/api/notice/save", {"key": key, "values": {**was, "表示文": "夜の点検"}, "was": was})
        self.assertEqual(res.status_code, 200, res.get_json())
        stale = self.post("/api/notice/save", {"key": key, "values": {**was, "表示文": "z"}, "was": was})
        self.assertEqual(stale.status_code, 409, "読んだあとにほかで直された")
        now = next(r for r in stale.get_json()["rows"] if r["ID"] == "夜の点検")
        was2 = {c["name"]: now.get(c["name"], "") for c in body["columns"]}
        res = self.post("/api/notice/delete", {"key": key, "was": was2})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertNotIn("夜の点検", [r["ID"] for r in res.get_json()["rows"]])

    def test_大設定でこの端末のラインを決める(self) -> None:
        self.unlock()
        res = self.post("/api/notice/line", {"line": "NS1"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["line"]["line"], "NS1")
        self.assertEqual(self.post("/api/notice/line", {"line": "ふしぎ"}).status_code, 400)

    def test_前の版の表には列を足す(self) -> None:
        conn = sqlite3.connect(guide_store.shared_path())
        conn.execute(f'CREATE TABLE "{rule.TABLE}" ("ID" TEXT, "ライン" TEXT, "直" TEXT, "表示文" TEXT)')
        conn.commit()
        conn.close()
        guide_store.ensure_table(guide_store.shared_path())
        conn = sqlite3.connect(guide_store.shared_path())
        names = [r[1] for r in conn.execute(f'PRAGMA table_info("{rule.TABLE}")')]
        conn.close()
        self.assertIn("曜日", names)
        self.assertIn("過ぎても出す", names)

    def test_押されていない一覧(self) -> None:
        self.rows({"ID": "点検"})
        self.press(at(TUE, 15, 1))
        self.when = at(TUE, 16)
        view = self.get("/api/notice/checks").get_json()
        got = {(r["report_date"], r["shift"]): (r["acked"], r["worker"]) for r in view["rows"]}
        self.assertEqual(got[(label(TUE), "2直")], (True, "山田"))
        self.assertEqual(got[(label(MON), "2直")], (False, ""))
        self.assertGreaterEqual(view["missed"], 1)


class WeekdayTests(StoreCase):
    """「直の判定は効いているが曜日の判定がされていないと思われる」(1.5.105)── 曜日は効いている。"""

    def test_日曜の案内_初めて起動したのが火曜なら火曜には出ず_次の日曜に出る(self) -> None:
        self.rows({"ID": "点検", "曜日": "日"}, seen=at(TUE, 6))
        self.assertIsNone(self.shown(at(TUE, 15, 1)))
        self.assertEqual(self.shown(at(SUN, 15, 1)), ("点検", label(SUN), "2直", False))

    def test_前からある行を日曜に直したら_火曜には出ない(self) -> None:
        self.rows({"ID": "点検", "曜日": "日"}, seen=at(MON, 6))
        self.assertIsNone(self.shown(at(TUE, 15, 1)))

    def test_最初に入る毎日の行を残すと_日曜の行とは別に毎日出る(self) -> None:
        self.rows({"ID": "計量器の点検"}, {"ID": "日曜の点検", "曜日": "日"}, seen=at(TUE, 6))
        self.assertEqual(self.shown(at(TUE, 15, 1))[0], "計量器の点検")

    def test_前の版の表が伝送用ファイルにあれば大設定で言う(self) -> None:
        conn = sqlite3.connect(SHARE / "伝送用ファイル.sqlite3")
        conn.execute(f'CREATE TABLE "{rule.TABLE}" ("ID" TEXT, "曜日" TEXT)')
        conn.commit()
        conn.close()
        body = self.get("/api/notice/settings").get_json()
        self.assertIn("伝送用ファイル.sqlite3", body["old_table"])


class Scenarios(StoreCase):
    """直をまたぐいろいろな場面(日報の 1.5.103 と同じ21場面を入口で)。案内は前の日曜の朝に作った。"""

    def test_01_2直で見ずに3直_3直で2直のぶん(self) -> None:
        self.rows({"ID": "点検"})
        self.press(at(MON, 9))
        self.assertIsNone(self.shown(at(MON, 14)))
        self.assertEqual(self.shown(at(MON, 23, 30)), ("点検", label(MON), "2直", True))
        self.assertEqual(self.shown(at(TUE, 3)), ("点検", label(MON), "2直", True))

    def test_02_3直も見ずに翌日の1直_まだ月曜の2直のぶん(self) -> None:
        self.rows({"ID": "点検"})
        self.assertEqual(self.shown(at(TUE, 9)), ("点検", label(MON), "2直", True))

    def test_03_火曜の2直が始まると火曜のぶんだけ(self) -> None:
        self.rows({"ID": "点検"})
        self.assertEqual(self.queue(at(TUE, 15, 1)), [("点検", label(TUE), "2直", False)])

    def test_04_何日も開かなければいちばん近い回だけ(self) -> None:
        self.rows({"ID": "点検"})
        self.assertEqual(self.queue(at(FRI, 9)), [("点検", label(THU), "2直", True)])

    def test_05_3直で押せば翌日の1直では出ない(self) -> None:
        self.rows({"ID": "点検"})
        self.press(at(MON, 23, 30))
        self.assertIsNone(self.shown(at(TUE, 9)))

    def test_06_2直で押せば3直でも出ない(self) -> None:
        self.rows({"ID": "点検"})
        self.press(at(MON, 15, 5))
        self.assertIsNone(self.shown(at(MON, 23, 30)))

    def two(self) -> None:
        self.rows({"ID": "月曜の点検", "曜日": "月", "並び": 1}, {"ID": "火曜の点検", "曜日": "火", "並び": 2})

    def test_07_月曜の2直は月曜のぶんだけ(self) -> None:
        self.two()
        self.assertEqual(self.queue(at(MON, 15, 5)), [("月曜の点検", label(MON), "2直", False)])

    def test_08_月曜を見ずに火曜の1直_月曜のぶん(self) -> None:
        self.two()
        self.assertEqual(self.queue(at(TUE, 9)), [("月曜の点検", label(MON), "2直", True)])

    def test_09_月曜を見ずに火曜の2直_月曜のぶん_続けて火曜のぶん(self) -> None:
        self.two()
        self.assertEqual(self.queue(at(TUE, 15, 5)), [("月曜の点検", label(MON), "2直", True),
                                                      ("火曜の点検", label(TUE), "2直", False)])

    def test_10_月火とも見ずに水曜の1直_古い順(self) -> None:
        self.two()
        self.assertEqual(self.queue(at(WED, 9)), [("月曜の点検", label(MON), "2直", True),
                                                  ("火曜の点検", label(TUE), "2直", True)])

    def test_11_木曜に開いても月曜のぶん(self) -> None:
        self.two()
        self.assertEqual(self.queue(at(THU, 9))[0], ("月曜の点検", label(MON), "2直", True))

    def test_12_次の月曜の2直では前の月曜は回さない_火曜はまだ回る(self) -> None:
        self.two()
        self.assertEqual(self.queue(at(MON + 7, 15, 5)), [("火曜の点検", label(TUE), "2直", True),
                                                          ("月曜の点検", label(MON + 7), "2直", False)])

    def test_13_月曜のぶんを火曜の1直で押せば火曜の2直は火曜だけ(self) -> None:
        self.two()
        self.press(at(TUE, 9))
        self.assertEqual(self.queue(at(TUE, 15, 5)), [("火曜の点検", label(TUE), "2直", False)])

    def test_14_同じ直に2つ_並びの順(self) -> None:
        self.rows({"ID": "B", "並び": 2}, {"ID": "A", "並び": 1})
        self.assertEqual([g[0] for g in self.queue(at(MON, 15, 5))], ["A", "B"])

    def test_15_過ぎても出す0はまたいだら出ない(self) -> None:
        self.rows({"ID": "その直だけ", "過ぎても出す": 0})
        self.assertIsNone(self.shown(at(MON, 23, 30)))

    def test_16_直が空の案内はまたいでも回さない(self) -> None:
        self.rows({"ID": "毎直", "直": ""})
        self.assertEqual(self.queue(at(TUE, 9)), [("毎直", label(TUE), "1直", False)])

    def test_17_ほかのラインの案内は出ない(self) -> None:
        self.rows({"ID": "NS1だけ", "ライン": "NS1"})
        self.assertIsNone(self.shown(at(MON, 15, 5)))

    def test_18_土日だけ_月曜の1直で日曜のぶん(self) -> None:
        self.rows({"ID": "週末", "曜日": "土日"})
        self.assertEqual(self.queue(at(MON + 7, 9)), [("週末", label(SUN), "2直", True)])

    def test_19_時刻の案内(self) -> None:
        self.rows({"ID": "18時", "タイミング": "時刻", "時刻": "18:00"})
        self.assertIsNone(self.shown(at(MON, 17, 59)))
        self.assertEqual(self.shown(at(MON, 18)), ("18時", label(MON), "2直", False))
        self.assertEqual(self.shown(at(MON, 23, 30)), ("18時", label(MON), "2直", True))

    def test_20_案内を作る前の回は数えない(self) -> None:
        self.rows({"ID": "週末", "曜日": "土"})
        self.assertIsNone(self.shown(at(MON, 9)))

    def test_21_日曜の2直を見ずに月曜の1直_日曜のぶん(self) -> None:
        self.rows({"ID": "点検"})
        self.assertEqual(self.queue(at(MON, 9)), [("点検", "2026年11月8日", "2直", True)])


if __name__ == "__main__":
    unittest.main()
