"""共有を**読めなかった**ことを「無い」「空」と取り違えない

    共有ファイルが読み込めなかった(ネットワークが一瞬切れた・ロック中・権限・開けない等)
    ことを「実績の表が無い」と誤認し、手元の実績を送り直していた。これにより共有側に
    重複データが大量に作られた(別のツールの事案)

日報管理で、共有側を読んだ結果で「送る・作る・送り直す・印を付ける」を決めている所のうち、
読めなかったときに空・無いと同じに扱っていた所を、1つずつ押さえます。決まりは1つです:

    読めなかった … 送らない・作らない・印を付けない(次の回にやり直す)。理由は知らせに出す
    本当に無い   … 今までどおり(ファイルはあり開けたが表が無い・置き場所は見えてファイルが無い)

    1. 停止内訳を読めない → 名前が空の集計を作り、送り済みの直を「未送信」に戻して
       共有の「内訳」を空で上書きしていた(`services/summary.refresh_shift`)
    2. 班員名簿を読めない → 共有の標準作業時間の班を「不明」で上書きしていた
       (`services/standard_time.flush`)
    3. 書き先の .accdb を開けない → ④⑤の列が無いとみなし、④⑤を出さないまま送り済みに
       していた(`access_bridge/pusher`)
    4. 共有の日報データを読めない → ほかのラインの月の書き出しを、この端末の一部だけで
       上書きして済んだことにしていた(`services/month_rollover._partial_lines`)
    5. 書き戻しの部品(`dbkit/outbox_sync`): 届いたか分からない失敗を「届いていない」として
       新しい送信IDで送り直していた・送信ID列を確かめられなかったことを「作れない」と覚えて
       以後ずっと送信ID無しで送っていた
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock
from unittest.mock import PropertyMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.access_bridge.errors import AccessBridgeError, ErrorKind  # noqa: E402
from nippou.access_bridge.importer import ImportResult  # noqa: E402
from nippou.db.connection import connect  # noqa: E402
from nippou.db.models import DetailRecord, HeaderRecord  # noqa: E402
from nippou.db.repository import NippouRepository  # noqa: E402
from nippou.db.schema import ensure_schema  # noqa: E402
from nippou.logic import stop_csv  # noqa: E402

DAY = "2026年9月1日"


def _failed(message: str = "ネットワーク パスが見つかりません") -> ImportResult:
    return ImportResult(success=False,
                        error=AccessBridgeError(ErrorKind.UNKNOWN, None, message))


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.conn = connect(self.tmp / "local.sqlite3")
        ensure_schema(self.conn)
        self.addCleanup(self.conn.close)
        self.repo = NippouRepository(self.conn)


# ======================================================================
# 1. 停止内訳を読めない → 集計を作り直しても、送り済みのまま・名前もそのまま
# ======================================================================
class StopLabelsTests(_Case):
    KEY = (DAY, "L-1", "1直")

    def setUp(self) -> None:
        super().setUp()
        # 停止内訳.csv は置かない(表だけを読む)
        patcher = patch("nippou.access_bridge.stop_master.read_csv",
                        return_value=(stop_csv.Parsed(source="停止内訳.csv"), False, ""))
        patcher.start()
        self.addCleanup(patcher.stop)

    def save(self, th: str = "10") -> None:
        key = dict(report_date=DAY, line="L-1", shift="1直", page=1)
        self.repo.save(HeaderRecord(**key, worker="山田"), [DetailRecord(
            **key, row_no=1, lot="N7131T0", ken="30", mai="10", tut="1",
            kz="07", kh="00", sz="15", sh="00", tim="420", wei="360", con="30",
            s="A", th=th)])

    @staticmethod
    def master(table_name: str):
        """伝送用ファイルの停止内訳の表(読めたとき)。記号 A は設備停止の「段取り」。"""
        rows = [{"内訳": "段取り", "内訳番号": "A"}] if table_name == "作業停止時間内訳_1" else []
        return ImportResult(success=True, rows=rows)

    def refresh(self, readable: bool):
        from nippou.services import summary

        side = ((lambda path, table_name, **_: self.master(table_name)) if readable
                else (lambda *a, **k: _failed()))
        with patch("nippou.access_bridge.stop_master.import_table", side_effect=side):
            return summary.refresh_shift(self.repo, *self.KEY)

    def names(self) -> list[str]:
        report = self.repo.load_packing_report(*self.KEY)
        return [s.stop_reason for d in self.repo.packing_details(report.id) for s in d.stops]

    def test_読めなくても送り済みの直を未送信に戻さず_名前も空にしない(self) -> None:
        self.save()
        self.refresh(readable=True)
        self.assertEqual(self.names(), ["段取り"])
        self.repo.mark_packing_report_synced(self.KEY)          # 共有へ送れた
        self.assertEqual(self.repo.pending_packing_reports(), [])

        # 同じ中身で保存し直した(自動保存など)ときに、共有の停止内訳が一瞬届かなかった
        self.save()
        self.refresh(readable=False)
        self.assertEqual(self.names(), ["段取り"])
        self.assertEqual(self.repo.pending_packing_reports(), [],
                         "読めなかっただけで「未送信」に戻り、空の内訳で送り直される")

    def test_読めないときに中身を直せば送り直す_名前は前のまま(self) -> None:
        self.save()
        self.refresh(readable=True)
        self.repo.mark_packing_report_synced(self.KEY)
        self.save(th="20")                                     # 停止の分を直した
        self.refresh(readable=False)
        self.assertEqual([r.key() for r in self.repo.pending_packing_reports()], [self.KEY])
        self.assertEqual(self.names(), ["段取り"])

    def test_本当に無い記号は今までどおり名前なし(self) -> None:
        """表は読めたが、その記号が無い(マスタから消した)── 名前は空で作る(今までどおり)。"""
        self.save()
        self.refresh(readable=True)
        self.repo.mark_packing_report_synced(self.KEY)
        with patch("nippou.access_bridge.stop_master.import_table",
                   return_value=ImportResult(success=True, rows=[])):
            from nippou.services import summary

            summary.refresh_shift(self.repo, *self.KEY)
        self.assertEqual(self.names(), [""])
        self.assertEqual([r.key() for r in self.repo.pending_packing_reports()], [self.KEY])

    def test_読めなかった理由を返す(self) -> None:
        from nippou.services import month_rollover

        with patch("nippou.access_bridge.stop_master.import_table", return_value=_failed()):
            labels, problem = month_rollover.read_stop_labels()
        self.assertEqual(labels, {})
        self.assertIn("ネットワーク パスが見つかりません", problem)
        with patch("nippou.access_bridge.stop_master.import_table",
                   return_value=ImportResult(success=True, rows=[])):
            self.assertEqual(month_rollover.read_stop_labels(), ({}, ""))


# ======================================================================
# 2. 班員名簿を読めない → 共有にある班を「不明」で上書きしない
# ======================================================================
class StandardTimeTeamTests(_Case):
    def setUp(self) -> None:
        super().setUp()
        from nippou.services import standard_time as svc

        self.svc = svc
        self.share = self.tmp / "共有"
        self.share.mkdir()
        self.db = self.share / "標準作業時間.sqlite3"

    def page(self, worker: str = "山田", minutes: int = 60) -> tuple[str, str, str]:
        from nippou.services import summary

        key = dict(report_date=DAY, line="L-1", shift="1直", page=1)
        self.repo.save(HeaderRecord(**key, worker=worker), [
            DetailRecord(**key, row_no=1, lot="N710T0", siz="8.000×1528.0×3053.0",
                         tut="4", hit="2", tim=str(minutes), con="40",
                         others1="H176", others2="ｼﾔ-ｼ", others4="1P0001")])
        summary.refresh_shift(self.repo, DAY, "L-1", "1直", labels={})
        return (DAY, "L-1", "1直")

    def push(self, key, roster):
        """`roster` が None なら名簿を読めない(共有が届かない)。"""
        result = (_failed() if roster is None
                  else ImportResult(success=True, rows=[{"名前": n, "班": t}
                                                        for n, t in roster.items()]))
        with patch("nippou.access_bridge.staff_master.import_table", return_value=result):
            return self.svc.after_push(self.repo, [(*key, 1)], self.db)

    def teams(self) -> list[str]:
        conn = sqlite3.connect(self.db)
        try:
            return [r[0] for r in conn.execute(
                f'SELECT DISTINCT 班 FROM "{self.svc.SAMPLE_TABLE}"')]
        finally:
            conn.close()

    def test_名簿を読めなくても共有にある班のまま(self) -> None:
        key = self.page()
        self.push(key, {"山田": "A"})
        self.assertEqual(self.teams(), ["A"])
        # 中身を直して送り直したとき、名簿が一瞬読めなかった
        key = self.page(minutes=70)
        out = self.push(key, None)
        self.assertEqual(self.teams(), ["A"], "読めなかっただけで班が「不明」に上書きされる")
        self.assertEqual(out.rows, 1)                  # 実績(作業時間)は新しい中身で入る
        self.assertIn("班員名簿を読めなかった", out.message)
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute(
                f'SELECT "作業時間(分)" FROM "{self.svc.SAMPLE_TABLE}"').fetchone()[0], 70)
        finally:
            conn.close()

    def test_共有に無い直は今までどおり不明で入れる(self) -> None:
        out = self.push(self.page(), None)
        self.assertEqual(self.teams(), ["不明"])
        self.assertEqual(out.shifts, 1)

    def test_名簿が読めて名前が無ければ不明に変わる(self) -> None:
        """名簿は読めた(本当に載っていない)── 名簿のとおりにする(今までどおり)。"""
        key = self.page()
        self.push(key, {"山田": "A"})
        self.push(key, {"鈴木": "B"})
        self.assertEqual(self.teams(), ["不明"])


# ======================================================================
# 3. 書き先の .accdb を開けない → ④⑤を打ったページは送らない
# ======================================================================
class AccessColumnsUnreadTests(_Case):
    def setUp(self) -> None:
        super().setUp()
        from nippou.access_bridge import pusher
        from tests import _accdb

        self.pusher = pusher
        self.scripts: list[str] = []
        self.target = _accdb.write(
            self.tmp / "共有" / "日報データ.accdb",
            _accdb.nippou_tables("L-1", [_accdb.nippou_page("L-1")], stops45=True))

    def save(self, page: int, s4: str = "") -> None:
        key = dict(report_date=DAY, line="L-1", shift="1直", page=page)
        self.repo.save(HeaderRecord(**key, worker="青木"),
                       [DetailRecord(**key, row_no=1, lot="H6081A1", s4=s4,
                                     th4="10" if s4 else "")])

    def runner(self):
        from nippou.access_bridge.runner import ScriptResult

        scripts = self.scripts

        class Fake:
            def run(self, script_text: str) -> ScriptResult:
                scripts.append(script_text)
                return ScriptResult(success=True)

        return Fake()

    def push(self, target=None, error: Exception = None):
        error = error or PermissionError(13, "別のプロセスが使用中です")
        with patch("nippou.accdb.reader.AccdbReader", side_effect=error):
            return self.pusher.push_pending(self.repo, target or self.target,
                                            runner=self.runner())

    def pending(self) -> list[int]:
        return sorted(h.page for h in self.repo.pending_sync_headers())

    def test_開けなければ45のあるページは送らず未送信のまま(self) -> None:
        self.save(1, s4="5")
        summary = self.push()
        self.assertEqual(summary.succeeded, [])
        self.assertEqual(len(summary.failed), 1)
        self.assertIn("確かめられなかった", summary.failed[0].error.message)
        self.assertIn("次の共有へ保存で送ります", summary.failed[0].error.message)
        self.assertEqual(self.scripts, [], "④⑤を書かないまま送っていた")
        self.assertEqual(self.pending(), [1])
        # 次に押したとき読めれば、④⑤の列がある表へ④⑤も書いて送る
        summary = self.pusher.push_pending(self.repo, self.target, runner=self.runner())
        self.assertEqual(len(summary.succeeded), 1)
        self.assertIn("[S4],[TH4],[S5],[TH5]", "\n".join(self.scripts))
        self.assertEqual(self.pending(), [])

    def test_開けなくても45の無いページは送る(self) -> None:
        self.save(1, s4="5")
        self.save(2)
        summary = self.push()
        self.assertEqual(summary.succeeded, [(DAY, "L-1", "1直", 2)])
        self.assertEqual(self.pending(), [1])

    def test_置き場所ごと見えないのも読めなかった扱い(self) -> None:
        self.save(1, s4="5")
        summary = self.push(self.tmp / "届かない" / "日報データ.accdb",
                            error=FileNotFoundError(2, "ネットワーク パスが見つかりません"))
        self.assertEqual(len(summary.failed), 1)
        self.assertEqual(self.pending(), [1])

    def test_本当に無いファイル_形の読めないファイルは今までどおりVBAの形(self) -> None:
        from nippou.accdb.reader import AccdbError

        self.save(1, s4="5")
        summary = self.pusher.push_pending(self.repo, self.tmp / "共有" / "無い.accdb",
                                           runner=self.runner())
        self.assertEqual(len(summary.succeeded), 1)
        self.assertEqual(len(summary.notices), 1)
        self.save(1, s4="6")
        summary = self.push(error=AccdbError("暗号化されています"))
        self.assertEqual(len(summary.succeeded), 1)


# ======================================================================
# 4. 共有の日報データを読めない → ほかのラインの月を書き出さない(済んだことにしない)
# ======================================================================
class MonthUnverifiedTests(_Case):
    SEP = (2026, 9)

    def setUp(self) -> None:
        super().setUp()
        from nippou import config, work_context
        from nippou.services import month_rollover

        self.shared = self.tmp / "届かない" / "日報データ.sqlite3"
        self.path = PropertyMock(side_effect=lambda: self.shared)
        for patcher in (
                patch.object(type(config.SETTINGS), "access_db_path", new_callable=lambda: self.path),
                # 置き場所は設定で決めてある(決めていない端末は今までどおり ── 下の試験)
                patch.object(month_rollover, "_shared_place_set", create=True, return_value=True),
                patch.object(work_context, "terminal_line_decided", return_value=True),
                patch.object(work_context, "stored_terminal_line", return_value="L-1")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def put(self, line: str, shift: str) -> None:
        key = dict(report_date="2026年9月30日", line=line, shift=shift, page=1)
        self.repo.save(HeaderRecord(**key, worker="山田"), [DetailRecord(
            **key, row_no=1, lot="N7131T0", ken="30", mai="10", tut="1",
            kz="07", kh="00", sz="15", sh="00", tim="420", wei="360", con="30")])

    def run_month(self):
        from nippou.services import month_rollover

        return month_rollover.run(self.repo, self.tmp / "月別", month=self.SEP)

    def test_届かなければほかのラインは書き出さず_済んだことにしない(self) -> None:
        from nippou.services import month_rollover

        self.put("L-1", "1直")
        self.put("HVC", "1直")
        result = self.run_month()
        self.assertEqual([e.line for e in result.exports], ["L-1"],
                         "共有に届かないのに、ほかのラインの月をこの端末の分だけで上書きしていた")
        self.assertIn("HVC", result.unverified)
        self.assertIn("読めなかったので今回は書き出していません", result.message)
        self.assertFalse((self.tmp / "月別" / "HVC").exists())
        self.assertFalse(month_rollover.settle(self.repo, result))

    def test_置き場所を決めていない端末は今までどおり書き出す(self) -> None:
        """共有を使っていない端末(置き場所が既定のまま・そこが無い)── 「無い」と同じ。"""
        from nippou.services import month_rollover

        self.put("HVC", "1直")
        with patch.object(month_rollover, "_shared_place_set", create=True, return_value=False):
            result = self.run_month()
        self.assertEqual([e.line for e in result.exports], ["HVC"])
        self.assertEqual(getattr(result, "unverified", {}), {})

    def test_開けない共有も読めなかった扱い(self) -> None:
        self.shared = self.tmp / "共有" / "日報データ.sqlite3"
        self.shared.parent.mkdir()
        self.shared.write_bytes(b"not a database" * 100)
        self.put("HVC", "1直")
        result = self.run_month()
        self.assertEqual(result.exports, [])
        self.assertIn("HVC", result.unverified)

    def test_読めるようになれば次の回に書き出して済ませる(self) -> None:
        from nippou.services import month_rollover

        self.put("L-1", "1直")
        self.put("HVC", "1直")
        for line in ("L-1", "HVC"):
            self.repo.mark_synced(("2026年9月30日", line, "1直", 1))   # 共有へは送れている
        today = date(2026, 10, 2)
        first = month_rollover.catch_up(self.repo, self.tmp / "月別", today=today)
        self.assertEqual([r.unverified for r, _ in first], [{"HVC": mock.ANY}])
        self.assertNotIn(self.SEP, self.repo.settled_months())
        # 共有に届くようになった(まだ1度も送っていない ── 本当に無い)
        self.shared.parent.mkdir()
        again = month_rollover.catch_up(self.repo, self.tmp / "月別", today=today)
        self.assertEqual([e.line for e in again[0][0].exports], ["HVC", "L-1"])
        self.assertIn(self.SEP, self.repo.settled_months())


# ======================================================================
# 5. 書き戻しの部品(dbkit/outbox_sync)
# ======================================================================
class _Access:
    """`AccessConnection` の代わり。`insert_error` / `ddl_error` を1回だけ投げる。"""

    def __init__(self, *, insert_error: str = "", ddl_error: str = "") -> None:
        self.path = "fake.accdb"
        self.inserted: list[dict] = []
        self.insert_error = insert_error
        self.ddl_error = ddl_error

    def execute(self, sql: str) -> None:
        from dbkit import access_odbc

        if self.ddl_error:
            message, self.ddl_error = self.ddl_error, ""
            raise access_odbc.AccessError(message)

    def insert(self, table: str, values: dict) -> None:
        from dbkit import access_odbc

        if self.insert_error:
            message, self.insert_error = self.insert_error, ""
            # 届いたかどうか分からない(入っていることもある)
            self.inserted.append(dict(values))
            raise access_odbc.AccessError(message)
        self.inserted.append(dict(values))


class OutboxTests(unittest.TestCase):
    LINK = "[08S01] Communication link failure(ネットワークが切れました)"

    def setUp(self) -> None:
        from dbkit import outbox_sync

        self.sync = outbox_sync
        outbox_sync._op_id_column_cache.clear()
        self.addCleanup(outbox_sync._op_id_column_cache.clear)
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute("CREATE TABLE Src (行ID INTEGER PRIMARY KEY, 値 TEXT)")
        self.conn.executemany("INSERT INTO Src VALUES (?, ?)", [(1, "a"), (2, "b")])
        self.conn.commit()
        self.spec = outbox_sync.WriteBackSpec(sqlite_table="Src", access_table="Dst",
                                              key_column="行ID")

    def age_claims(self) -> None:
        self.conn.execute(f"UPDATE [{self.sync.SYNC_LOG_TABLE}]"
                          " SET 同期日時 = datetime('now','localtime','-30 minutes')")
        self.conn.commit()

    def test_届いたか分からない行は同じ送信IDで送り直す(self) -> None:
        access = _Access(insert_error=self.LINK)
        result = self.sync.write_back(self.conn, access, [self.spec])
        self.assertFalse(result.ok)
        first_id = access.inserted[0][self.sync.DEFAULT_OP_ID_COLUMN]
        # すぐ次に回しても、届いたか分からない行は新しい送信IDで送らない
        self.sync.write_back(self.conn, access, [self.spec])
        ids = [v[self.sync.DEFAULT_OP_ID_COLUMN] for v in access.inserted if v["値"] == "a"]
        self.assertEqual(ids, [first_id],
                         "届いたか分からない行を、新しい送信IDで送り直していた(二重登録の元)")
        # 期限が過ぎたら、同じ送信IDで送り直す(Access の一意インデックスが二重を弾く)
        self.age_claims()
        self.sync.write_back(self.conn, access, [self.spec])
        ids = [v[self.sync.DEFAULT_OP_ID_COLUMN] for v in access.inserted if v["値"] == "a"]
        self.assertEqual(ids, [first_id, first_id])
        self.assertEqual(self.sync.pending_rows(self.conn, self.spec), [])

    def test_送信ID列を確かめられなければ送らず_覚えない(self) -> None:
        access = _Access(ddl_error=self.LINK)
        result = self.sync.write_back(self.conn, access, [self.spec])
        self.assertEqual(access.inserted, [], "送信ID無しで送っていた")
        self.assertIn("確かめられなかった", "\n".join(result.errors))
        self.assertEqual(self.sync._op_id_column_cache, {})
        # 次は確かめられて、送信ID付きで送る
        result = self.sync.write_back(self.conn, access, [self.spec])
        self.assertTrue(result.ok)
        self.assertTrue(all(self.sync.DEFAULT_OP_ID_COLUMN in v for v in access.inserted))
        self.assertEqual(len(access.inserted), 2)

    def test_届いていないと分かる失敗は今までどおり予約を外す(self) -> None:
        access = _Access(insert_error="column not found")
        self.sync.write_back(self.conn, access, [self.spec])
        self.assertEqual(len(self.sync.pending_rows(self.conn, self.spec)), 1)


if __name__ == "__main__":
    unittest.main()
