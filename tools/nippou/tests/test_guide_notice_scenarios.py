"""案内表示: 直をまたぐいろいろな場面 (v4.47.4)

    案内を見ることなく次の直へまたぐケース / さらにまたぐケース /
    月曜に出す案内・火曜に出す案内が同じラインにあった場合に次の直へまたぐケース どう表示されるのか
    いろんなケースでテストしてください

時計を進めながら、その時刻に画面を開いたら何が出るか(`/api/notice/due`)を確かめる。直の時刻は既定
(1直 7:00〜15:00 / 2直 15:00〜22:50 / 3直 22:50〜7:00)。2026-11-09 は月曜。
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import guide_notice as rule  # noqa: E402
from tests import test_guide_notice as base  # noqa: E402

MON, TUE, WED, THU, FRI, SAT, SUN = 9, 10, 11, 12, 13, 14, 15


def at(day: int, hour: int, minute: int = 0) -> datetime:
    if day > 30:
        return datetime(2026, 12, day - 30, hour, minute)
    return datetime(2026, 11, day, hour, minute)


def label(day: int) -> str:
    return f"2026年11月{day}日"


class Scenarios(base.NoticeWebTests):

    def rows(self, *rows: dict) -> None:
        """表「案内表示」の中身をこの行にする。"""
        from nippou import source_db
        from nippou.services import guide_notice

        self.at(at(MON, 6))
        self.get("/api/notice/due")                   # 表を作る
        conn = sqlite3.connect(self.master)
        conn.execute(f'DELETE FROM "{rule.TABLE}"')
        for r in rows:
            values = {**rule.FIRST_ROW, **r}
            names = ", ".join(f'"{k}"' for k in values)
            conn.execute(f'INSERT INTO "{rule.TABLE}" ({names}) VALUES ({", ".join("?" * len(values))})',
                         list(values.values()))
        conn.commit()
        conn.close()
        source_db.forget()
        guide_notice.reset()
        # 案内は**前の日曜の朝に作った**(その前の回は数えない ── 作る前の回を「押されていない」にしない)
        self.at(datetime(2026, 11, 8, 6, 0))
        self.get("/api/notice/due")

    def shown(self, when: datetime):
        """その時刻に画面を開いたら出るもの: (案内, 報告日, 直, あとの直で出しているか)。無ければ None。"""
        self.at(when)
        body = self.get("/api/notice/due").get_json()
        if not body["notice"]:
            return None
        return (body["notice"]["id"], body["report_date"], body["shift"], body["carried"])

    def press_now(self, when: datetime, worker: str = "山田"):
        """その時刻に出ているものを1つ押す。押したもの(shown と同じ形)。"""
        got = self.shown(when)
        self.assertIsNotNone(got, f"{when} に出るものが無い")
        res = self.post("/api/notice/ack", {"id": got[0], "worker": worker,
                                            "report_date": got[1], "shift": got[2]})
        self.assertEqual(res.status_code, 200, res.get_json())
        return got

    def queue(self, when: datetime) -> list:
        """その時刻に開いて、出るものを順に全部押したときの並び。"""
        out = []
        for _ in range(10):
            if self.shown(when) is None:
                return out
            out.append(self.press_now(when))
        self.fail("押しても出続ける")

    # ------------------------------------------------------------------
    # 1つの案内(機側・2直の始まり・毎日)
    # ------------------------------------------------------------------
    def test_1_2直で見ずに3直へ_3直で2直のぶんが出る(self) -> None:
        self.rows({"ID": "点検"})
        self.press_now(at(MON, 9))                        # 日曜の2直のぶん(月曜の1直で押した)
        self.assertIsNone(self.shown(at(MON, 14)), "月曜の1直: もう出ない")
        self.assertEqual(self.shown(at(MON, 23, 30)), ("点検", label(MON), "2直", True))
        self.assertEqual(self.shown(at(TUE, 3, 0)), ("点検", label(MON), "2直", True), "3直の夜中も")

    def test_2_2直_3直も見ずに翌日の1直_まだ月曜の2直のぶんが出る(self) -> None:
        self.rows({"ID": "点検"})
        self.assertEqual(self.shown(at(TUE, 9)), ("点検", label(MON), "2直", True))
        self.assertEqual(self.shown(at(TUE, 14, 59)), ("点検", label(MON), "2直", True))

    def test_3_さらにまたいで火曜の2直が始まると_火曜のぶんだけ_月曜のぶんは押されていないまま(self) -> None:
        self.rows({"ID": "点検"})
        self.assertEqual(self.queue(at(TUE, 15, 1)), [("点検", label(TUE), "2直", False)],
                         "火曜の2直を押せば済む(月曜のぶんは出し直さない)")
        self.assertIsNone(self.shown(at(TUE, 23, 30)))
        rows = self.get("/api/notice/checks").get_json()["rows"]
        got = {(r["report_date"], r["shift"]): r["acked"] for r in rows}
        self.assertEqual(got[(label(MON), "2直")], False, "月曜の2直は押されていない(一覧で赤)")
        self.assertEqual(got[(label(TUE), "2直")], True)

    def test_4_何日も開かなかった_いちばん近い2直のぶんだけ出る(self) -> None:
        self.rows({"ID": "点検"})
        self.assertEqual(self.queue(at(FRI, 9)), [("点検", label(THU), "2直", True)],
                         "木曜の2直のぶん1つだけ(月〜水のぶんは一覧で赤)")
        self.assertIsNone(self.shown(at(FRI, 10)))

    def test_5_3直で押せば_翌日の1直では出ない(self) -> None:
        self.rows({"ID": "点検"})
        self.assertEqual(self.press_now(at(MON, 23, 30)), ("点検", label(MON), "2直", True))
        self.assertIsNone(self.shown(at(TUE, 3)))
        self.assertIsNone(self.shown(at(TUE, 9)))

    def test_6_2直で押せば_3直でも翌日の1直でも出ない(self) -> None:
        self.rows({"ID": "点検"})
        self.press_now(at(MON, 15, 5))
        self.assertIsNone(self.shown(at(MON, 23, 30)))
        self.assertIsNone(self.shown(at(TUE, 9)))

    # ------------------------------------------------------------------
    # 月曜の案内・火曜の案内(同じライン・同じ2直)
    # ------------------------------------------------------------------
    def two(self) -> None:
        self.rows({"ID": "月曜の点検", "曜日": "月", "並び": 1},
                  {"ID": "火曜の点検", "曜日": "火", "並び": 2})

    def test_7_月曜の2直で月曜のぶんだけ_火曜のぶんは出ない(self) -> None:
        self.two()
        self.assertEqual(self.queue(at(MON, 15, 5)), [("月曜の点検", label(MON), "2直", False)])

    def test_8_月曜の2直を見ずに火曜の1直_月曜のぶんが出る_火曜のぶんはまだ(self) -> None:
        self.two()
        self.assertEqual(self.queue(at(TUE, 9)), [("月曜の点検", label(MON), "2直", True)])

    def test_9_月曜の2直を見ずに火曜の2直_月曜のぶん_続けて火曜のぶん(self) -> None:
        """月曜の案内の「次の同じ直」は来週の月曜の2直。火曜の2直でもまだ出す(古いほうから)。"""
        self.two()
        self.assertEqual(self.queue(at(TUE, 15, 5)),
                         [("月曜の点検", label(MON), "2直", True),
                          ("火曜の点検", label(TUE), "2直", False)])

    def test_10_月曜も火曜も見ずに水曜の1直_両方が古い順に出る(self) -> None:
        self.two()
        self.assertEqual(self.queue(at(WED, 9)),
                         [("月曜の点検", label(MON), "2直", True),
                          ("火曜の点検", label(TUE), "2直", True)])
        self.assertIsNone(self.shown(at(WED, 15, 5)), "水曜の案内は無い")

    def test_11_月曜のぶんを木曜に開いても出る_次の月曜の2直が始まるまで(self) -> None:
        self.two()
        self.assertEqual(self.queue(at(THU, 9)),
                         [("月曜の点検", label(MON), "2直", True),
                          ("火曜の点検", label(TUE), "2直", True)])

    def test_12_次の月曜の2直が始まれば_前の月曜のぶんは回さない_火曜のぶんはまだ回る(self) -> None:
        """1週間だれも開かず、次の月曜の2直に開いた。前の月曜のぶんは次の月曜の回が始まったので回さない
        (一覧で赤)。火曜のぶんは次の火曜の2直までまだ回る(古いほうから)。"""
        self.two()
        self.assertEqual(self.queue(at(MON + 7, 15, 5)),
                         [("火曜の点検", label(TUE), "2直", True),
                          ("月曜の点検", label(MON + 7), "2直", False)])

    def test_13_月曜のぶんを火曜の1直で押せば_火曜の2直は火曜のぶんだけ(self) -> None:
        self.two()
        self.press_now(at(TUE, 9))
        self.assertEqual(self.queue(at(TUE, 15, 5)), [("火曜の点検", label(TUE), "2直", False)])

    # ------------------------------------------------------------------
    # そのほか
    # ------------------------------------------------------------------
    def test_14_同じ直に2つの案内_並びの順に1つずつ(self) -> None:
        self.rows({"ID": "B", "並び": 2}, {"ID": "A", "並び": 1})
        self.assertEqual([g[0] for g in self.queue(at(MON, 15, 5))], ["A", "B"])

    def test_15_過ぎても出すが0の案内は_またいだら出ない_一覧では赤(self) -> None:
        self.rows({"ID": "その直だけ", "過ぎても出す": 0})
        self.assertIsNone(self.shown(at(MON, 23, 30)))
        rows = self.get("/api/notice/checks").get_json()["rows"]
        self.assertFalse(next(r for r in rows if r["report_date"] == label(MON))["acked"])

    def test_16_直を決めていない案内は_またいでも回さない_いまの直のぶん(self) -> None:
        self.rows({"ID": "毎直", "直": ""})
        self.assertEqual(self.queue(at(TUE, 9)), [("毎直", label(TUE), "1直", False)])

    def test_17_ほかのラインの案内は出ない(self) -> None:
        self.rows({"ID": "NS1だけ", "ライン": "NS1"})
        self.assertIsNone(self.shown(at(MON, 15, 5)))
        self.assertIsNone(self.shown(at(MON, 23, 30)))

    def test_18_土日だけの案内_金曜に見ずに月曜_日曜の2直のぶん(self) -> None:
        self.rows({"ID": "週末", "曜日": "土日"})
        self.assertEqual(self.queue(at(MON + 7, 9)), [("週末", label(SUN), "2直", True)])

    def test_19_時刻の案内_その時刻より前に開けば出ない_過ぎてから開けば出る(self) -> None:
        self.rows({"ID": "18時", "タイミング": "時刻", "時刻": "18:00"})
        self.assertIsNone(self.shown(at(MON, 17, 59)))
        self.assertEqual(self.shown(at(MON, 18, 0)), ("18時", label(MON), "2直", False))
        self.assertEqual(self.shown(at(MON, 23, 30)), ("18時", label(MON), "2直", True))

    def test_20_案内を作る前の回は数えない(self) -> None:
        """日曜の朝に作った案内: その前の土曜の2直のぶんは、月曜に開いても出ない・一覧にも無い。"""
        self.rows({"ID": "週末", "曜日": "土"})
        self.assertIsNone(self.shown(at(MON, 9)))
        rows = self.get("/api/notice/checks").get_json()["rows"]
        self.assertEqual([r for r in rows if r["notice"] == "週末"], [])

    def test_21_日曜の2直を見ずに月曜の1直_日曜のぶんが出る(self) -> None:
        """作ったのは日曜の朝 ── 日曜の2直は作ったあとの回なので、見ていなければ月曜に回る。"""
        self.rows({"ID": "点検"})
        self.assertEqual(self.queue(at(MON, 9)), [("点検", "2026年11月8日", "2直", True)])


# 親の試験はここでは流さない(親のファイルで流れる)
for _name in [n for n in dir(base.NoticeWebTests) if n.startswith("test_")]:
    setattr(Scenarios, _name, None)
