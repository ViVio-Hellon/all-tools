"""コメント・連絡は「どの班からか」と「いつ残したか」を持つ (VER4.2.4)

現場の声:「連絡の宛先(xx班 への連絡)ではなく、コメントを残したのはどの班か
(xx班からの連絡)にしたい」「コメントを残した時刻が分かるように(マウスを
乗せたときと内容閲覧の両方)」「宛先ラインは作業長以外は選べなくして
(ほかのラインへの連絡は、そのラインでしか見えないので意味がない)」。

ここで確かめること:

- 登録すると、書いた班(``記入班``)と保存した時刻(``登録日時``)が残る
- 内容閲覧・マス目のツールチップ・削除の選択肢に「L-1 B班からの連絡」と
  「2026/10/21 14:05」の形の時刻が出る
- **古い行**(書いた班が残っていない)は、宛先として選ばれていた班を
  「からの連絡」と読ませない ──「(班なし)」と出す
- **作業長以外は別のラインへ送れない**(画面で選べないだけでなく、サーバも断る)
- 列は**後ろに足す**: 手元の古い DB も、共有の古い表も、そのまま読める・送れる
"""

from __future__ import annotations

import datetime as _dt
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from . import _web
from .test_sync import _Base
from calendar_app import config, db, importer, repository, settings as user_settings
from calendar_app.presenters import calendar as cal

D = "2026/08/03"
DAY = _dt.date(2026, 8, 3)

#: 画面に出す時刻の形(秒は出さない)
SHOWN_TIME = re.compile(r"\d{4}/\d{2}/\d{2} \d{2}:\d{2}(?!:)")
#: 保存する時刻の形
STORED_TIME = re.compile(r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}$")

#: VER4.2.3 までの手元の休み管理と送信用の写し(列を足す前の形)
_OLD_LOCAL = f"""
CREATE TABLE "{config.TABLE_DATA}" (
    "ID"        INTEGER PRIMARY KEY AUTOINCREMENT,
    "access_id" INTEGER,
    "日付"       TEXT NOT NULL,
    "区分"       TEXT NOT NULL,
    "登録内容"    TEXT NOT NULL DEFAULT '',
    "識別コード"  TEXT NOT NULL DEFAULT '',
    "直"        TEXT NOT NULL DEFAULT '',
    "残業者"     TEXT NOT NULL DEFAULT '',
    "早出者"     TEXT NOT NULL DEFAULT '',
    "班"        TEXT NOT NULL DEFAULT '',
    "ライン"     TEXT NOT NULL DEFAULT ''
);
CREATE VIEW "{config.TABLE_DATA}_送信用" AS
SELECT "ID","日付","区分","登録内容","識別コード","直","残業者","早出者","班","ライン"
FROM "{config.TABLE_DATA}";
"""

_OLD_COLUMNS = ["ID", "日付", "区分", "登録内容", "識別コード",
                "直", "残業者", "早出者", "班", "ライン"]


def _add_member(conn, code, name, group, line):
    conn.execute(
        f'INSERT INTO "{config.TABLE_MEMBER}" '
        '("管理番号","苗字","班","名前","読み","担当ライン") VALUES (?,?,?,?,?,?)',
        (code, name, group, name, name, line))
    conn.commit()


def _comment_rows(conn) -> list[sqlite3.Row]:
    return conn.execute(
        f'SELECT * FROM "{config.TABLE_DATA}" WHERE "区分"=? ORDER BY "ID"',
        (config.KUBUN_OTHER,)).fetchall()


class _WebBase(unittest.TestCase):
    def setUp(self) -> None:
        _web.with_source(self)
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()
        _add_member(self.conn, "10", "山田太郎", "B", "L-1")
        _add_member(self.conn, "11", "加藤眞一", "A", "L-1")
        _add_member(self.conn, "20", "鈴木一郎", "A", "HVC")
        self.addCleanup(user_settings.save_my_line, "")
        user_settings.save_my_line("L-1")

    def post(self, **body):
        body.setdefault("date", D)
        return self.client.post("/api/comment", json=body, headers=_web.auth())

    def day(self) -> dict:
        return _web.json_of(self.client.get(f"/api/day/{D}", headers=_web.auth()))

    def cell(self) -> dict:
        body = _web.json_of(self.client.get("/api/calendar?year=2026&month=8",
                                            headers=_web.auth()))
        return next(c for c in body["cells"] if c["date"] == D)


class SavedTests(_WebBase):
    """書いた班と時刻が残る。"""

    def test_書いた班と保存した時刻が残る(self) -> None:
        before = _dt.datetime.now().replace(microsecond=0)
        res = self.post(line="L-1", group="B", text="2号機の段取り替え")
        self.assertEqual(res.status_code, 200, res.get_json())
        after = _dt.datetime.now()
        row = _comment_rows(self.conn)[0]
        self.assertEqual(row[config.COL_WRITER_GROUP], "B")
        stamp = row[config.COL_REGISTERED_AT]
        self.assertRegex(stamp, STORED_TIME)
        saved = _dt.datetime.strptime(stamp, config.DATETIME_FORMAT)
        self.assertTrue(before <= saved <= after, stamp)
        # 前からの列はそのまま(ほかの版・ほかのツールが読んでも同じに見える)
        self.assertEqual((row["班"], row["ライン"], row["識別コード"]), ("B", "L-1", "-"))

    def test_登録した知らせはからの連絡(self) -> None:
        body = self.post(line="L-1", group="B", text="朝礼あり").get_json()
        self.assertEqual(body["message"], "L-1 B班からの連絡を登録しました")
        self.assertNotIn("への連絡", body["message"])

    def test_休みにも保存した時刻が残る(self) -> None:
        res = self.client.post("/api/rest", json={"date": D, "code": "10", "shift": "1"},
                               headers=_web.auth())
        self.assertEqual(res.status_code, 200, res.get_json())
        row = self.conn.execute(f'SELECT * FROM "{config.TABLE_DATA}"').fetchone()
        self.assertRegex(row[config.COL_REGISTERED_AT], STORED_TIME)
        self.assertEqual(row[config.COL_WRITER_GROUP], "")


class ShownTests(_WebBase):
    """内容閲覧・ツールチップ・削除の選択肢に、どの班からといつ。"""

    def setUp(self) -> None:
        super().setUp()
        repository.Repository(self.conn).save_record(
            DAY, config.KUBUN_OTHER, "2号機の段取り替え", "-", group="B", line="L-1",
            writer_group="B", registered_at="2026/08/03 14:05:33")

    def test_内容閲覧にどの班からと時刻(self) -> None:
        detail = self.day()["detail"]
        self.assertIn("L-1 B班からの連絡", detail)
        self.assertIn("2026/08/03 14:05", detail)
        self.assertNotIn("14:05:33", detail)            # 秒は出さない
        self.assertIn("2号機の段取り替え", detail)

    def test_マス目のツールチップにどの班からと時刻(self) -> None:
        cell = self.cell()
        self.assertEqual(cell["body"], "L-1:B班 コメント有")
        self.assertIn("L-1 B班からの連絡", cell["tip"])
        self.assertIn("2026/08/03 14:05", cell["tip"])

    def test_ツールチップはマスと同じく自分のライン宛だけ(self) -> None:
        repository.Repository(self.conn).save_record(
            DAY, config.KUBUN_OTHER, "HVCの話", "-", group="A", line="HVC",
            writer_group="A", registered_at="2026/08/03 09:00:00")
        tip = self.cell()["tip"]
        self.assertIn("L-1 B班からの連絡", tip)
        self.assertNotIn("HVC", tip)
        user_settings.save_my_line(config.LINE_SUPERVISOR)
        self.assertIn("HVC A班からの連絡", self.cell()["tip"])

    def test_休みの繋ぎのツールチップも残る(self) -> None:
        self.client.post("/api/rest", json={"date": D, "code": "10", "shift": "1"},
                         headers=_web.auth())
        tip = self.cell()["tip"]
        self.assertIn("3直残業", tip)
        self.assertIn("L-1 B班からの連絡", tip)

    def test_削除の選択肢にもどの班からと時刻(self) -> None:
        item = next(i for i in self.day()["items"] if i["kind"] == "comment")
        self.assertIn("B班から", item["meta"])
        self.assertIn("2026/08/03 14:05", item["meta"])


class OldRowTests(_WebBase):
    """書いた班が残っていない古い行は、宛先の班を「から」と読ませない。"""

    def setUp(self) -> None:
        super().setUp()
        # VER4.2.3 までの行: 班は**宛先として選ばれた班**。書いた班も時刻も無い
        repository.Repository(self.conn).save_record(
            DAY, config.KUBUN_OTHER, "前の版の連絡", "-", group="B", line="L-1",
            registered_at="")

    def test_内容閲覧は班なし(self) -> None:
        detail = self.day()["detail"]
        self.assertIn("前の版の連絡", detail)
        self.assertIn("(班なし)", detail)
        self.assertNotIn("B班", detail)
        self.assertIn("時刻の記録なし", detail)

    def test_マス目も班なし(self) -> None:
        cell = self.cell()
        self.assertEqual(cell["body"], "L-1:(班なし) コメント有")
        self.assertNotIn("B班", cell["tip"])
        self.assertIn("(班なし)", cell["tip"])

    def test_削除の選択肢も宛先の班を出さない(self) -> None:
        item = next(i for i in self.day()["items"] if i["kind"] == "comment")
        self.assertNotIn("B班", item["meta"])
        self.assertIn("(班なし)", item["meta"])


class LineLockTests(_WebBase):
    """宛先のラインは作業長だけが選べる。**サーバも断る**。"""

    def targets(self) -> dict:
        return _web.json_of(self.client.get("/api/comment-targets", headers=_web.auth()))

    def test_作業長以外はこの端末のラインだけ(self) -> None:
        body = self.targets()
        self.assertFalse(body["can_choose_line"])
        self.assertEqual(body["my_line"], "L-1")
        self.assertEqual(body["lines"], ["L-1"])
        self.assertEqual(body["groups"]["L-1"], ["A", "B"])

    def test_作業長は全ラインから選べる(self) -> None:
        user_settings.save_my_line(config.LINE_SUPERVISOR)
        body = self.targets()
        self.assertTrue(body["can_choose_line"])
        self.assertEqual(sorted(body["lines"]), ["HVC", "L-1"])

    def test_作業長以外が別のラインへ送ると断る(self) -> None:
        res = self.post(line="HVC", group="A", text="よそのラインへ")
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"], "line_locked")
        self.assertIn("作業長", res.get_json()["error"]["message"])
        self.assertEqual(_comment_rows(self.conn), [])

    def test_ラインが決まっていない端末も別のラインへは送れない(self) -> None:
        user_settings.save_my_line("")
        res = self.post(line="L-1", group="B", text="未設定の端末から")
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"], "line_locked")
        self.assertEqual(_comment_rows(self.conn), [])

    def test_自分のラインへは送れる(self) -> None:
        self.assertEqual(self.post(line="L-1", group="A", text="自分の班").status_code, 200)

    def test_作業長は別のラインへ送れる(self) -> None:
        user_settings.save_my_line(config.LINE_SUPERVISOR)
        res = self.post(line="HVC", group="A", text="作業長から")
        self.assertEqual(res.status_code, 200, res.get_json())
        row = _comment_rows(self.conn)[0]
        self.assertEqual((row["ライン"], row[config.COL_WRITER_GROUP]), ("HVC", "A"))


class LocalUpgradeTests(unittest.TestCase):
    """手元の古い DB(列を足す前)を開くと、後ろに列が足され、写しも作り直される。"""

    def test_古い手元のDBに列を後ろへ足す(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "calendar.db"
            old = sqlite3.connect(path)
            old.executescript(_OLD_LOCAL)
            old.execute(f'INSERT INTO "{config.TABLE_DATA}" ("日付","区分","登録内容",'
                        '"識別コード","班","ライン") VALUES (?,?,?,?,?,?)',
                        (D, config.KUBUN_OTHER, "前からの連絡", "-", "B", "L-1"))
            old.commit()
            old.close()

            conn = db.connect(path)
            try:
                columns = [r[1] for r in conn.execute(
                    f'PRAGMA table_info("{config.TABLE_DATA}")')]
                self.assertEqual(columns[:11], ["ID", "access_id"] + _OLD_COLUMNS[1:])
                self.assertEqual(columns[11:], list(config.DATA_ADDED_COLUMNS))
                view = [r[1] for r in conn.execute(
                    f'PRAGMA table_info("{config.TABLE_DATA}_送信用")')]
                self.assertEqual(view, _OLD_COLUMNS + list(config.DATA_ADDED_COLUMNS))
                records = repository.Repository(conn).get_day_records(DAY)
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0].writer_group, "")
                self.assertEqual(records[0].registered_at, "")
            finally:
                conn.close()
            # 2回目に開いても何も変わらない(足すのは1度だけ)
            again = db.connect(path)
            again.close()


class SharedTableTests(_Base):
    """共有の表(取り込み元)は**既存の列を変えず、後ろに足す**。古い表でも読める。"""

    def source_columns(self) -> list[str]:
        conn = sqlite3.connect(self.data_db)
        try:
            return [r[1] for r in conn.execute(f'PRAGMA table_info("{config.TABLE_DATA}")')]
        finally:
            conn.close()

    def test_古い共有の表へ送ると列を後ろに足して届く(self) -> None:
        self.assertNotIn(config.COL_WRITER_GROUP, self.source_columns())
        self.repo.save_record(DAY, config.KUBUN_OTHER, "共有へ", "-", group="B",
                              line="L-1", writer_group="B",
                              registered_at="2026/08/03 14:05:33")
        self.send()
        columns = self.source_columns()
        self.assertEqual(columns[:10], _OLD_COLUMNS)          # 前からの列は同じ順のまま
        for name in config.DATA_ADDED_COLUMNS:
            self.assertIn(name, columns[10:])
        row = self.rows_in_source()[0]
        self.assertEqual(row[config.COL_WRITER_GROUP], "B")
        self.assertEqual(row[config.COL_REGISTERED_AT], "2026/08/03 14:05:33")
        self.assertEqual(row["班"], "B")

    def _refuse_alter(self, message: str):
        """共有の表に列を足す ALTER だけを断らせる。"""
        from unittest import mock

        from calendar_app.dbkit import source_db

        real = source_db.SourceConnection.execute

        def execute(conn, sql, params=(), **kwargs):
            if sql.lstrip().upper().startswith("ALTER TABLE") and "送信ID" not in sql:
                raise source_db.SourceError(message)
            return real(conn, sql, params, **kwargs)

        patcher = mock.patch.object(source_db.SourceConnection, "execute", execute)
        patcher.start()
        return patcher

    def test_列を足せない共有の表へも連絡そのものは届く(self) -> None:
        patcher = self._refuse_alter("not authorized")
        try:
            self.repo.save_record(DAY, config.KUBUN_OTHER, "足せなくても", "-", group="B",
                                  line="L-1", writer_group="B")
            self.send()
        finally:
            patcher.stop()
        self.assertEqual([r["登録内容"] for r in self.rows_in_source()], ["足せなくても"])
        self.assertNotIn(config.COL_WRITER_GROUP, self.source_columns())

    def test_使用中で列を足せなければ_省かずに次の周期で届ける(self) -> None:
        from calendar_app.sync import total_pending_count
        from calendar_app.sync.autosync import TransportError

        self.repo.save_record(DAY, config.KUBUN_OTHER, "次の周期", "-", group="B",
                              line="L-1", writer_group="B",
                              registered_at="2026/08/03 14:05:33")
        patcher = self._refuse_alter("database is locked")
        try:
            with self.assertRaises(TransportError):
                self.send()
        finally:
            patcher.stop()
        self.assertEqual(self.rows_in_source(), [])
        self.assertEqual(total_pending_count(self.conn), 1)    # 送信待ちに残る
        self.send()
        row = self.rows_in_source()[0]
        self.assertEqual((row[config.COL_WRITER_GROUP], row[config.COL_REGISTERED_AT]),
                         ("B", "2026/08/03 14:05:33"))

    def test_古い共有の表の行も取り込める(self) -> None:
        remote = sqlite3.connect(self.data_db)
        remote.execute(f'INSERT INTO "{config.TABLE_DATA}" ("ID","日付","区分","登録内容",'
                       '"識別コード","班","ライン") VALUES (?,?,?,?,?,?,?)',
                       (7, D, config.KUBUN_OTHER, "前の版から", "-", "B", "L-1"))
        remote.commit()
        remote.close()
        importer.import_source(self.conn, self.data_db)
        record = self.repo.get_day_records(DAY)[0]
        self.assertEqual((record.naiyou, record.writer_group, record.registered_at),
                         ("前の版から", "", ""))
        detail = cal.day_dict(cal.day_view(self.conn, DAY))["detail"]
        self.assertIn("(班なし)", detail)
        self.assertNotIn("B班", detail)

    def test_送って取り込み直しても班と時刻が残る(self) -> None:
        self.repo.save_record(DAY, config.KUBUN_OTHER, "往復", "-", group="A",
                              line="L-1", writer_group="A",
                              registered_at="2026/08/03 07:30:00")
        self.send()
        importer.import_source(self.conn, self.data_db)
        record = self.repo.get_day_records(DAY)[0]
        self.assertEqual((record.writer_group, record.registered_at),
                         ("A", "2026/08/03 07:30:00"))


if __name__ == "__main__":
    unittest.main()
