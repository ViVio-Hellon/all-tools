"""いま誰が開いているか (:mod:`kanban.presence`)

倉庫が確認している最中に現場が開いていれば、これから看板が出るかもしれないと
身構えられる ── そのための表示。**嘘をつかないこと**を主に確かめる。

VBA 版は開いた瞬間に印を付けるだけだったので、異常終了すると開いたままに
なった。一度でもそうなると表示は信じてもらえなくなるので、
「印は立っているが、もう打たれていない」を見分けられることを確かめる。
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from kanban import config, presence
from kanban.db.shared import SharedDb
from kanban.db.store import Store


def _stamp(at: datetime) -> str:
    return at.strftime("%Y/%m/%d %H:%M:%S")


class PresenceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        presence.reset()
        self.dir = Path(tempfile.mkdtemp(prefix="kanban_presence_"))
        self.store = Store(str(self.dir / "local.sqlite3"))
        self.store.ensure_schema()

    def tearDown(self) -> None:
        presence.reset()
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def saw(self, line: str, status: str, stamp: str, host: str = "PC-A") -> None:
        """共有DBから取り込んだことにする。"""
        self.store.upsert_line_status_rows(
            [{"line": line, "status": status, "sort_order": 1,
              "stamp": stamp, "host": host}]
        )


class ReadTest(PresenceTestBase):
    def test_open_terminal_is_live(self):
        self.saw("LVC", config.STATE_OPEN, "2026/08/31 09:00:00")
        seen = presence.read(self.store)
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].live)
        self.assertFalse(seen[0].stale)
        self.assertEqual(seen[0].host, "PC-A")

    def test_closed_terminal_is_not_listed(self):
        self.saw("LVC", config.STATE_CLOSED, "2026/08/31 09:00:00")
        self.assertEqual(presence.read(self.store), [])

    def test_my_own_line_is_excluded(self):
        """自分が開いていることは画面を見れば分かる。並べても場所を取るだけ。"""
        self.saw("LVC", config.STATE_OPEN, "2026/08/31 09:00:00")
        self.assertEqual(presence.read(self.store, exclude="LVC"), [])

    def test_stale_flag_is_not_reported_as_open(self):
        """**異常終了で立ったままの印を信じない。**

        VBA 版はここで嘘をつき、その表示は二度と信じてもらえなくなった。
        """
        self.saw("LVC", config.STATE_OPEN, "2026/08/31 09:00:00")
        later = datetime.now() + timedelta(seconds=presence.STALE_SEC + 60)
        seen = presence.read(self.store, now=later)
        self.assertEqual(len(seen), 1, "消してしまうと理由が分からない")
        self.assertFalse(seen[0].live)
        self.assertTrue(seen[0].stale)

    def test_a_new_stamp_revives_it(self):
        """打ち直されたら、また生きているとみなす。

        いったん見捨てた相手が戻ってきたときに、そのまま「居ない」で
        固まらないこと。
        """
        self.saw("LVC", config.STATE_OPEN, "2026/08/31 09:00:00")
        later = datetime.now() + timedelta(seconds=presence.STALE_SEC + 60)
        self.assertTrue(presence.read(self.store, now=later)[0].stale)

        # 打ち直された(``seen_at`` はこちらの時計で打ち直される)
        self.saw("LVC", config.STATE_OPEN, "2026/08/31 09:30:00")
        self.assertFalse(presence.read(self.store)[0].stale)

    def test_does_not_compare_the_other_clock_with_ours(self):
        """**相手の時計とこちらの時計を引き算しない。**

        工場の端末どうしで時刻が揃っている保証は無い。10 年ずれた
        ``更新日時`` でも、打ち直され続けているなら生きている。
        """
        self.saw("LVC", config.STATE_OPEN, "2016/01/01 00:00:00")
        seen = presence.read(self.store)
        self.assertTrue(seen[0].live)
        self.assertEqual(seen[0].stamp, "2016/01/01 00:00:00")

    def test_unreadable_stamp_is_treated_as_stale(self):
        """読めない値を「生きている」に倒さない(安全側)。"""
        self.saw("LVC", config.STATE_OPEN, "?")
        self.store.connection.execute("UPDATE line_status SET seen_at = 'こわれた'")
        self.store.connection.commit()
        self.assertTrue(presence.read(self.store)[0].stale)

    def test_warehouse_is_labelled_as_itself(self):
        self.saw(config.WAREHOUSE_LINE_NAME, config.STATE_OPEN, "2026/08/31 09:00:00")
        self.assertEqual(
            presence.read(self.store)[0].label, config.WAREHOUSE_LINE_NAME
        )

    def test_an_unchanged_stamp_does_not_refresh_seen_at(self):
        """**同じ値を取り込み直しても「見た」ことにしない。**

        毎回打ち直すと、落ちた端末がいつまでも生きているように見える。
        """
        self.saw("LVC", config.STATE_OPEN, "2026/08/31 09:00:00")
        first = self.store.line_statuses()[0]["seen_at"]
        self.saw("LVC", config.STATE_OPEN, "2026/08/31 09:00:00")
        self.assertEqual(self.store.line_statuses()[0]["seen_at"], first)


class RegistersTest(unittest.TestCase):
    def test_site_and_warehouse_register(self):
        self.assertTrue(presence.registers(config.MODE_SITE))
        self.assertTrue(presence.registers(config.MODE_WAREHOUSE))

    def test_view_mode_does_not_register(self):
        """**読むだけの端末は名乗らない。**

        名乗らせると「倉庫が開いている」が常時点灯し、本物の倉庫と
        見分けが付かなくなる。
        """
        self.assertFalse(presence.registers(config.MODE_WAREHOUSE_VIEW))

    def test_warehouse_registers_under_its_own_name(self):
        self.assertEqual(
            presence.registered_line(config.MODE_WAREHOUSE, "LVC"),
            config.WAREHOUSE_LINE_NAME,
        )
        self.assertEqual(presence.registered_line(config.MODE_SITE, "LVC"), "LVC")

    def test_view_mode_names_nobody(self):
        """**名乗らない端末は、隠す相手も持たない。**

        倉庫参照でも ``config.json`` にラインは書いてある。それを自分だと
        扱うと、名乗ってもいないのに、その現場が開いていることだけが
        見えなくなる。
        """
        self.assertEqual(
            presence.registered_line(config.MODE_WAREHOUSE_VIEW, "LVC"), ""
        )


class HeartbeatTest(PresenceTestBase):
    """名乗る側。共有DBを本当に置いて確かめる。"""

    def setUp(self) -> None:
        super().setUp()
        self.shared = self.dir / "看板マスタ.sqlite3"
        conn = sqlite3.connect(str(self.shared))
        conn.execute(
            f"CREATE TABLE [{config.TABLE_STATE}] "
            "([管理番号] NUMERIC, [ライン名] TEXT, [状態] TEXT,"
            " [更新日時] DATETIME, [ホスト名] TEXT)"
        )
        conn.execute(
            f"INSERT INTO [{config.TABLE_STATE}] VALUES (1, 'LVC', '閉', '', '')"
        )
        conn.commit()
        conn.close()
        self.db = SharedDb(str(self.shared), cache_dir=str(self.dir / "cache"))

    def row(self, line: str):
        conn = sqlite3.connect(str(self.shared))
        conn.row_factory = sqlite3.Row
        try:
            found = conn.execute(
                f"SELECT * FROM [{config.TABLE_STATE}] WHERE [ライン名] = ?", (line,)
            ).fetchone()
            return dict(found) if found else None
        finally:
            conn.close()

    def beat_for(self, line: str, **kwargs) -> presence.Heartbeat:
        return presence.Heartbeat(self.db, self.store, line, host="PC-TEST", **kwargs)

    def test_beat_marks_open_with_time_and_host(self):
        self.assertTrue(self.beat_for("LVC").beat())
        row = self.row("LVC")
        self.assertEqual(row["状態"], config.STATE_OPEN)
        self.assertEqual(row["ホスト名"], "PC-TEST")
        self.assertTrue(row["更新日時"], "更新日時が空だと、古さを判じられない")

    def test_close_marks_closed(self):
        beat = self.beat_for("LVC")
        beat.beat()
        beat.close()
        self.assertEqual(self.row("LVC")["状態"], config.STATE_CLOSED)

    def test_adds_a_row_for_an_unknown_line(self):
        """まだ登録されていないラインでも名乗れること。"""
        self.assertTrue(self.beat_for("HVC").beat())
        row = self.row("HVC")
        self.assertEqual(row["状態"], config.STATE_OPEN)
        self.assertEqual(row["管理番号"], 1, "手元の写しから番号を決める")

    def test_does_not_duplicate_a_line(self):
        """同じラインの行を 2 つ作らない。

        重複すると、どちらが本当の状態か分からなくなる。
        """
        beat = self.beat_for("HVC")
        beat.beat()
        beat.beat()
        conn = sqlite3.connect(str(self.shared))
        try:
            count = conn.execute(
                f"SELECT COUNT(*) FROM [{config.TABLE_STATE}] WHERE [ライン名] = 'HVC'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 1)

    def test_a_broken_share_does_not_raise(self):
        """**付加情報のために業務を止めない。**

        共有フォルダが落ちている程度のことで、看板の操作が道連れに
        なってはいけない。
        """
        beat = presence.Heartbeat(
            SharedDb(str(self.dir / "ない.sqlite3")), self.store, "LVC"
        )
        self.assertFalse(beat.beat())

    def test_no_gateway_is_harmless(self):
        self.assertFalse(presence.Heartbeat(None, self.store, "LVC").beat())

    def test_writes_only_the_columns_that_exist(self):
        """``更新日時`` が無い共有DBでも、``状態`` だけは書けること。

        読む側は列が無くても既定値で済ませている。書く側だけ落ちると
        釣り合わない。
        """
        thin = self.dir / "薄い.sqlite3"
        conn = sqlite3.connect(str(thin))
        conn.execute(
            f"CREATE TABLE [{config.TABLE_STATE}] ([ライン名] TEXT, [状態] TEXT)"
        )
        conn.execute(f"INSERT INTO [{config.TABLE_STATE}] VALUES ('LVC', '閉')")
        conn.commit()
        conn.close()
        self.store.set_meta("state_table_columns", "ライン名,状態")

        beat = presence.Heartbeat(
            SharedDb(str(thin), cache_dir=str(self.dir / "cache2")),
            self.store, "LVC", host="PC-TEST",
        )
        self.assertTrue(beat.beat())
        conn = sqlite3.connect(str(thin))
        try:
            self.assertEqual(
                conn.execute(
                    f"SELECT [状態] FROM [{config.TABLE_STATE}]"
                ).fetchone()[0],
                config.STATE_OPEN,
            )
        finally:
            conn.close()

    def test_the_other_side_sees_it(self):
        """**書いた側と読む側が噛み合うこと。**

        別々に正しくても、繋がっていなければ意味がない。
        """
        from kanban.db import sync

        self.beat_for("HVC").beat()
        sync._import_line_status(self.store, self.db)

        seen = presence.read(self.store, exclude="LVC")
        self.assertEqual([s.line for s in seen], ["HVC"])
        self.assertTrue(seen[0].live)
        self.assertEqual(seen[0].host, "PC-TEST")


class InstallTest(PresenceTestBase):
    def test_install_is_idempotent(self):
        first = presence.install(None, self.store, "LVC", interval_sec=999)
        second = presence.install(None, self.store, "LVC", interval_sec=999)
        self.assertIs(first, second)
        self.assertIs(presence.get(), first)

    def test_no_line_means_no_heartbeat(self):
        """担当ラインが決まっていない端末は名乗らない(名乗る先が無い)。"""
        self.assertIsNone(presence.install(None, self.store, "", interval_sec=999))

    def test_gateway_can_be_swapped(self):
        """接続先を変えたら、心拍も付いてくること。

        放っておくと、もう見ていない共有DBへ「開いています」を打ち続け、
        よそに幽霊が残る。
        """
        beat = presence.install(None, self.store, "LVC", interval_sec=999)
        fresh = SharedDb("/dev/null/ない.sqlite3")
        presence.set_gateway(fresh)
        self.assertIs(beat.gateway, fresh)

    def test_changing_the_line_moves_the_claim(self):
        """**担当ラインを変えたら、名乗る行も移す。**

        移さないと心拍は前のラインを名乗り続けます。ラインを切り替えながら
        試すと必ず踏み、こうなります:

        * 前のラインが共有DBで「開」のまま残り、他端末には**居もしない
          端末が開いて見える**(やがて ``L-1?`` のように破線の ``?`` になる)
        * 新しいラインは一度も名乗らないので、本当に開いているほうが見えない
        * 除外は**いまの**ラインで引くのに印は**前の**ラインに立つので、
          自分の画面の「開いている端末」に**自分自身が他人として並ぶ**
        """
        first = presence.install(None, self.store, "LVC", interval_sec=999)
        self.assertEqual(first.line, "LVC")

        moved = presence.set_line("LS")
        self.assertIsNotNone(moved)
        self.assertEqual(moved.line, "LS", "名乗る行が移っていない")
        self.assertIs(presence.get(), moved)
        self.assertIsNot(moved, first, "前の心拍を使い回している")

    def test_changing_to_the_same_line_keeps_the_same_heartbeat(self):
        """同じラインを選び直しただけで名乗り直さない(無駄な開閉を書かない)。"""
        first = presence.install(None, self.store, "LVC", interval_sec=999)
        self.assertIs(presence.set_line("LVC"), first)

    def test_moving_to_no_line_just_stops(self):
        """名乗らないモードへ移ったら、前のラインを畳んで終わり。"""
        presence.install(None, self.store, "LVC", interval_sec=999)
        self.assertIsNone(presence.set_line(""))
        self.assertIsNone(presence.get())

    def test_heartbeat_is_well_inside_the_stale_window(self):
        """打つ間隔より、見捨てるまでの時間が十分長いこと。

        逆転すると、開いているのに「もう居ない」と出る。取り込みの遅れも
        乗るので、2 倍では足りない。
        """
        self.assertGreater(presence.STALE_SEC, presence.HEARTBEAT_SEC * 3)

    def test_heartbeat_is_not_chatty(self):
        """**共有ファイルへの書き込みは高くつく。**

        書けば更新時刻が変わり、変われば全端末の「中身が同じなら写さない」
        が外れる ── 心拍 1 回が、全端末にファイル 1 個ぶんのコピーを
        やらせる。誰も操作していない夜間にも効くので、ここは業務の速さでは
        なく共有フォルダの静けさで決める。
        """
        self.assertGreaterEqual(
            presence.HEARTBEAT_SEC, 60.0,
            "1 分より短く打つと、共有フォルダが休まる時間が無くなる",
        )


if __name__ == "__main__":
    unittest.main()
