"""直の開始・終了をマスタで直したら、すぐ効くか (v4.39.1)

【利用者から】

    直の開始時間終了時間をマスタで修正後即座に反映するかのチェックをしてください

止めた時計で日報入力を開いたまま、マスタの「時間用」を直して、次の要求から
直の終わりの札・見るだけの切り替え・15分前の催促・5分前の自動確定・保存(確定)・
共有へ保存の最終時間の確かめ・時間を割り振る が新しい時刻で動くかを確かめます。

- **直したPC**: マスタ管理で保存した瞬間に手元へ写す(`master_admin.after_write`。前から)
- **ほかのPC**: 前は起動し直すか「時間マスタを取り込む」を押すまで前の時刻のままだった。
  画面の要求のついでにマスタのファイルの更新時刻を見て、変わっていれば読み直す
  (`services/shift_times.refresh_if_changed`。30秒に1回まで)
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HAS_FLASK, SKIP_REASON  # noqa: E402
from tests import test_scenario_shift_flow as flow  # noqa: E402

ROWS_1530 = flow.rows_of(flow.row("A01", "07:00", "10:00"), flow.rest("10:00", "11:00"),
                         flow.row("A02", "11:00", "15:30"))
ROWS_1500 = flow.clean_shift_rows("1直", "A")
ROWS_1430 = flow.rows_of(flow.row("A01", "07:00", "10:00"), flow.rest("10:00", "11:00"),
                         flow.row("A02", "11:00", "14:30"))


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class ShiftMasterReflectTests(flow.ScenarioCase):
    def setUp(self) -> None:
        super().setUp()
        from nippou.services import shift_times
        shift_times.reset()
        self.addCleanup(shift_times.reset)
        # 試験では30秒の間を空けない(要求のたびに見る)
        p = patch.object(shift_times, "CHECK_EVERY", 0.0)
        p.start()
        self.addCleanup(p.stop)
        self.master = self.tmp / "ref" / "伝送用ファイル.sqlite3"
        conn = sqlite3.connect(str(self.master))
        with conn:
            conn.execute('CREATE TABLE "時間用" ("番号", "直", "開始", "終了", "更新日")')
            conn.executemany('INSERT INTO "時間用" VALUES (?,?,?,?,"")',
                             [(1, "1", "07:00", "15:00"), (2, "2", "15:00", "22:50"),
                              (3, "3", "22:50", "07:00"), (4, "昼", "08:00", "17:00")])
        conn.close()
        self.assertEqual(self.post("/api/master/unlock", {"enable": True, "password": "nisk"}).status_code, 200)

    # -- マスタを直す -------------------------------------------------------
    def edit(self, key: str, values: dict) -> dict:
        """このPCのマスタ管理で直す(画面と同じ道)。"""
        conn = sqlite3.connect(str(self.master))
        rowid = conn.execute('SELECT rowid FROM "時間用" WHERE "直" = ?', (key,)).fetchone()[0]
        conn.close()
        res = self.post("/api/master/row/save", {"file": "transmission", "table": "時間用",
                                                  "key": rowid, "values": values})
        self.assertEqual(res.status_code, 200, res.get_json())
        return res.get_json()

    def other_pc_edits(self, key: str, values: dict) -> None:
        """ほかのPCが同じマスタを直した(この端末の道は通らない)。"""
        conn = sqlite3.connect(str(self.master))
        with conn:
            for column, value in values.items():
                conn.execute(f'UPDATE "時間用" SET "{column}" = ? WHERE "直" = ?', (value, key))
        conn.close()

    def look(self, when: str, rows: dict) -> tuple[dict, dict]:
        self.at(when)
        state = self.post("/api/entry/state", self.sheet(rows)).get_json()
        key = self.get(f"/api/shift/key?report_date={state['report_date']}"
                       f"&shift={state['shift']}&page=1").get_json()
        return state, key

    def extend_to_1530(self, *, other_pc: bool) -> None:
        do = self.other_pc_edits if other_pc else self.edit
        do("1", {"終了": "15:30"})
        do("2", {"開始": "15:30"})

    # -- 延ばす ---------------------------------------------------------------
    def check_extended(self) -> None:
        state, key = self.look("2026-10-05 14:25", ROWS_1530)
        self.assertEqual(state["shift_end"], "15:30", "直の終わりの札")
        # 前の終わり(15:00)を過ぎても見るだけにならず、保存できる
        state, key = self.look("2026-10-05 15:10", ROWS_1530)
        self.assertFalse(state["read_only"], state.get("read_only_reason"))
        self.assertFalse(key["anchor_standing"]["expired"])
        self.assertEqual(self.save(ROWS_1530).status_code, 200)
        # 催促は新しい終わりの15分前、自動確定は5分前
        self.at("2026-10-05 14:46")
        self.assertIsNone(self.get("/api/sound/due").get_json()["play"], "前の終わりで催促しない")
        self.assertFalse(self.close_tick("2026-10-05 14:55")["ran"], "前の終わりで確定しない")
        self.at("2026-10-05 15:16")
        play = self.get("/api/sound/due").get_json()["play"]
        self.assertEqual(play["key"], "print_reminder")
        self.assertIn("1直 終了14分前", play["message"])
        done = self.close_tick("2026-10-05 15:25")
        self.assertTrue(done["ran"])
        self.assertEqual(done["findings"], [], "最終時間 15:30 で通る")

    def test_このPCのマスタ管理で延ばすとすぐ効く(self) -> None:
        self.start_shift("2026-10-05 07:05", "山田")
        self.at("2026-10-05 14:20")
        body = self.edit("1", {"終了": "15:30"})
        self.assertIn("直の時間にもすぐ効かせました", body["message"])
        self.edit("2", {"開始": "15:30"})
        self.check_extended()

    def test_ほかのPCが延ばしても次の要求で効く(self) -> None:
        self.start_shift("2026-10-05 07:05", "山田")
        self.at("2026-10-05 14:20")
        self.extend_to_1530(other_pc=True)
        self.check_extended()
        self.assertEqual(self.repo().get_shift_times()["1"], ("07:00", "15:30"))

    def test_延ばしたら前の終わりの行では共有へ保存で止まる(self) -> None:
        """最終時間の確かめも新しい終わり(15:30)で見る。"""
        self.start_shift("2026-10-05 07:05", "山田")
        self.at("2026-10-05 14:20")
        self.extend_to_1530(other_pc=True)
        self.at("2026-10-05 15:20")
        self.save(ROWS_1500)
        res = self.push_now()
        self.assertEqual(res.status_code, 422, res.get_json())
        self.assertIn("15:30", res.get_json()["error"]["message"])

    def test_割り振りも新しい直の時間で(self) -> None:
        from nippou import user_settings
        from nippou.config import KEY_TIME_SPLIT
        user_settings.save(KEY_TIME_SPLIT, True)
        self.start_shift("2026-10-05 07:05", "山田")
        self.at("2026-10-05 14:20")
        self.extend_to_1530(other_pc=True)
        rows = flow.rows_of({"LOT": "A01", "MAI": "10", "TUT": "1"}, {"LOT": "A02", "MAI": "10", "TUT": "1"})
        plan = self.post("/api/entry/distribute-times", self.sheet(rows, dry_run=True)).get_json()
        self.assertEqual((plan["start"], plan["end"], plan["last"]), ("07:00", "15:30", "15:30"), plan)

    # -- 縮める ---------------------------------------------------------------
    def test_縮めると新しい終わりで見るだけになる(self) -> None:
        self.start_shift("2026-10-05 07:05", "山田")
        self.at("2026-10-05 14:10")
        self.other_pc_edits("2", {"開始": "14:30"})
        self.other_pc_edits("1", {"終了": "14:30"})
        state, _key = self.look("2026-10-05 14:16", ROWS_1430)
        self.assertEqual(state["shift_end"], "14:30")
        self.assertFalse(state["read_only"])
        self.assertIn("1直 終了14分前", self.get("/api/sound/due").get_json()["play"]["message"])
        state, key = self.look("2026-10-05 14:31", ROWS_1430)
        self.assertTrue(state["read_only"])
        self.assertTrue(key["anchor_standing"]["expired"])
        self.assertEqual(self.save(ROWS_1430).status_code, 403)

    # -- 読み直しの決まり -------------------------------------------------------
    def test_変わっていなければ中身は読まない(self) -> None:
        self.start_shift("2026-10-05 07:05", "山田")
        with patch("nippou.access_bridge.importer.import_shift_times") as read:
            for when in ("2026-10-05 10:00", "2026-10-05 10:01", "2026-10-05 10:02"):
                self.look(when, ROWS_1500)
        read.assert_not_called()

    def test_30秒に1回までしか見に行かない(self) -> None:
        from nippou.services import shift_times
        self.start_shift("2026-10-05 07:05", "山田")
        with patch.object(shift_times, "CHECK_EVERY", 3600.0):
            self.look("2026-10-05 10:00", ROWS_1500)        # ここで見た
            self.other_pc_edits("1", {"終了": "15:30"})
            state, _key = self.look("2026-10-05 10:01", ROWS_1500)
            self.assertEqual(state["shift_end"], "15:00", "間を空けるまでは前の時刻")

    def test_マスタが読めなくなっても前の時刻で動く(self) -> None:
        self.start_shift("2026-10-05 07:05", "山田")
        conn = sqlite3.connect(str(self.master))
        with conn:
            conn.execute('DROP TABLE "時間用"')                # 直す途中で表が消えた
        conn.close()
        state, _key = self.look("2026-10-05 10:00", ROWS_1500)
        self.assertEqual(state["shift_end"], "15:00")
        self.assertEqual(self.repo().get_shift_times()["1"], ("07:00", "15:00"))

    def test_長い書き込みのあいだは待たせずに見送る(self) -> None:
        """書き込みのロックが取れないときは、その要求では読み直さない(次の要求で)。"""
        import threading

        from app import _WRITE_LOCK
        self.start_shift("2026-10-05 07:05", "山田")
        self.other_pc_edits("1", {"終了": "15:30"})
        held, release = threading.Event(), threading.Event()

        def hold() -> None:
            with _WRITE_LOCK:
                held.set()
                release.wait(10)

        t = threading.Thread(target=hold)
        t.start()
        held.wait(5)
        probe = "/api/shift/key?report_date=2026年10月5日&shift=1直&page=1"   # 1分の見張り(GET)
        try:
            self.at("2026-10-05 10:00")
            self.assertEqual(self.get(probe).status_code, 200, "待たされずに返る")
            self.assertEqual(self.repo().get_shift_times()["1"], ("07:00", "15:00"),
                             "ロックが取れないので今回は見送り")
        finally:
            release.set()
            t.join(5)
        self.at("2026-10-05 10:01")
        self.get(probe)
        self.assertEqual(self.repo().get_shift_times()["1"], ("07:00", "15:30"), "次の要求で読み直す")


    # -- 心拍では読み直さない(v4.39.2) -----------------------------------------
    def test_心拍と進み具合では読み直さない(self) -> None:
        """タブの心拍(5秒ごと)は書き込みのロックの外に置いた道。そこへ読み直しを持ち込まない。"""
        self.start_shift("2026-10-05 07:05", "山田")
        self.other_pc_edits("1", {"終了": "15:30"})
        with patch("nippou.services.shift_times.reload") as reload:
            for path in ("/api/tab/ping", "/api/progress", "/api/jobs"):
                self.client.post(path, json={}) if path == "/api/tab/ping" else self.client.get(path)
            reload.assert_not_called()
        self.assertEqual(self.repo().get_shift_times()["1"], ("07:00", "15:00"))
        self.look("2026-10-05 10:00", ROWS_1500)           # ふつうの要求では読み直す
        self.assertEqual(self.repo().get_shift_times()["1"], ("07:00", "15:30"))

    def test_間を空けているあいだは設定もDBも開かない(self) -> None:
        from nippou.services import shift_times
        self.start_shift("2026-10-05 07:05", "山田")
        with patch.object(shift_times, "CHECK_EVERY", 3600.0):
            self.look("2026-10-05 10:00", ROWS_1500)        # ここで見た
            with patch.object(shift_times, "_stamp") as stamp, \
                    patch("app.get_repo") as repo:
                self.look("2026-10-05 10:01", ROWS_1500)
            stamp.assert_not_called()                       # マスタのファイルを見に行かない
            repo.assert_not_called()

for _name in [n for n in dir(flow.ScenarioCase) if n.startswith("test_")]:
    setattr(ShiftMasterReflectTests, _name, None)


if __name__ == "__main__":
    unittest.main()
