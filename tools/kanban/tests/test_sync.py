"""共有 SQLite <-> 手元 SQLite 同期のテスト。

共有 DB を本物のファイルで用意すると I/O と後始末が要るので、
:class:`kanban.db.shared.SharedDb` と同じ形のダミーを差し替えて検証する。

**書き戻しはプレースホルダ付きの ``(SQL, 値)``** になった(Access 方言の
リテラルを組み立てていた頃と違い、SQL を正規表現で読み解く必要がない)。
ダミー側もそれをそのまま解釈して、テーブルへ反映する。
"""

from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path

from kanban import config
from kanban.db import sync
from kanban.db.shared import StatementResult, TableSnapshot
from kanban.db.store import Store
from kanban.domain import models


class FakeGateway:
    """テスト用の共有 DB。実行された文を記録するだけ。"""

    def __init__(self, tables: dict[str, TableSnapshot], writable: bool = True):
        self.tables = tables
        self.path = "dummy.sqlite3"
        self.mode = "sqlite"
        self._writable = writable
        self.executed: list[tuple[str, list]] = []
        self.affected = 1
        self.fail_with: str | None = None

    @property
    def can_write(self) -> bool:
        return self._writable

    def table_names(self) -> list[str]:
        return list(self.tables)

    def read_table(self, name: str) -> TableSnapshot:
        if name not in self.tables:
            raise RuntimeError(f"テーブルがありません: {name}")
        return self.tables[name]

    def execute(self, statements):
        if self.fail_with:
            raise RuntimeError(self.fail_with)
        self.executed.extend(statements)
        return [StatementResult(ok=True, affected=self.affected) for _ in statements]

    # -- 検証用の小道具 --------------------------------------------------
    def statement_texts(self) -> list[str]:
        """実行された SQL 文だけ。"""
        return [sql for sql, _params in self.executed]

    def assignments_for(self, mgmt_no: str) -> dict[str, object]:
        """その管理番号に対して送られた「列 -> 値」。

        SQL の並びと値の並びを突き合わせて組み直す。**値が本当に何で
        送られたか**を見たいので、文字列を読み解くのではなくこちらで持つ。
        """
        out: dict[str, object] = {}
        for sql, params in self.executed:
            if str(params[-1]) != str(mgmt_no):
                continue
            cols = _columns_of(sql)
            out.update(dict(zip(cols, params[:-1])))
        return out


def _columns_of(sql: str) -> list[str]:
    """``UPDATE [t] SET [a] = ?, [b] = ? WHERE ...`` から列名を取り出す。"""
    body = sql.split(" SET ", 1)[1].split(" WHERE ", 1)[0]
    return [part.split("=")[0].strip().strip("[]") for part in body.split(", ")]


class MutableFakeGateway(FakeGateway):
    """実際に UPDATE をテーブルへ反映する版。

    ``FakeGateway`` は記録するだけでテーブルの中身を書き換えない。
    「手元の SQLite は各端末専用の写しで、正式なデータは常に共有 DB にある」
    という構成では、ある端末の書き込みが共有 DB を経由して別の端末の
    読み取りに現れることそのものを検証したいため、こちらは反映する。
    """

    def __init__(self, tables: dict[str, TableSnapshot]):
        super().__init__(tables, writable=True)

    def read_table(self, name: str) -> TableSnapshot:
        snapshot = super().read_table(name)
        # 呼び出し側が rows を書き換えても内部状態に影響しないようコピーを返す
        # (実際の SharedDb.read_table も毎回新しいスナップショットを返す)
        return TableSnapshot(
            name=snapshot.name,
            columns=list(snapshot.columns),
            categories=dict(snapshot.categories),
            rows=[dict(row) for row in snapshot.rows],
        )

    def execute(self, statements):
        results = []
        for sql, params in statements:
            self.executed.append((sql, list(params)))
            results.append(self._apply(sql, list(params)))
        return results

    def _apply(self, sql: str, params: list) -> StatementResult:
        table = sql.split("UPDATE ", 1)[1].split(" SET ", 1)[0].strip().strip("[]")
        snapshot = self.tables.get(table)
        if snapshot is None:
            return StatementResult(ok=True, affected=0)

        key_col = sql.split(" WHERE ", 1)[1].split("=")[0].strip().strip("[]")
        key_val = params[-1]
        target = next(
            (r for r in snapshot.rows if str(r.get(key_col)) == str(key_val)), None
        )
        if target is None:
            return StatementResult(ok=True, affected=0)

        for col, value in zip(_columns_of(sql), params[:-1]):
            target[col] = "" if value is None else value
        return StatementResult(ok=True, affected=1)


def kanban_snapshot(table: str, rows: list[dict[str, str]], numeric_key: bool = False):
    columns = [
        config.COL_KEY,
        config.COL_MATERIAL,
        config.COL_SIZE,
        config.COL_WANT,
        config.COL_UNWANT,
        config.COL_ORDERED_AT,
        config.COL_SHIPPED,
        config.COL_CONFIRMED_AT,
        config.COL_PERMANENT,
    ]
    categories = {name: "TEXT" for name in columns}
    if numeric_key:
        categories[config.COL_KEY] = "NUMBER"
    return TableSnapshot(name=table, columns=columns, categories=categories, rows=rows)


def make_row(no: str, material="外装紙", size="A", **kwargs) -> dict[str, str]:
    row = {
        config.COL_KEY: no,
        config.COL_MATERIAL: material,
        config.COL_SIZE: size,
        config.COL_WANT: "",
        config.COL_UNWANT: "〇",
        config.COL_ORDERED_AT: "",
        config.COL_SHIPPED: "",
        config.COL_CONFIRMED_AT: "",
        config.COL_PERMANENT: "〇",
    }
    row.update(kwargs)
    return row


class SyncTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="kanban_sync_"))
        self.store = Store(str(self.dir / "kanban.sqlite3"), host_name="PC-TEST")
        self.store.ensure_schema()
        self.gateway = FakeGateway(
            {
                "看板_LVC": kanban_snapshot("看板_LVC", [make_row("1"), make_row("2")]),
                "看板_大板小板": kanban_snapshot(
                    "看板_大板小板", [make_row("1", material="ハードボード")], numeric_key=True
                ),
                config.TABLE_STATE: TableSnapshot(
                    name=config.TABLE_STATE,
                    columns=[config.COL_KEY, "ライン名", "状態"],
                    categories={config.COL_KEY: "TEXT", "ライン名": "TEXT", "状態": "TEXT"},
                    rows=[
                        {config.COL_KEY: "1", "ライン名": "LVC", "状態": "閉"},
                        {config.COL_KEY: "2", "ライン名": "倉庫", "状態": "閉"},
                    ],
                ),
            }
        )

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)


class ImportTest(SyncTestBase):
    def test_import_reads_available_tables_and_skips_missing(self):
        result = sync.import_all(self.store, self.gateway)
        ok = {r.line for r in result.ok_lines}
        self.assertEqual(ok, {"LVC", "大板小板"})
        # 存在しないラインは失敗として記録されるが処理は続く
        self.assertIn("NS1", {r.line for r in result.failed_lines})
        self.assertEqual(len(self.store.items("LVC")), 2)

    def test_import_records_key_category_per_table(self):
        sync.import_all(self.store, self.gateway)
        self.assertEqual(self.store.line_source("LVC")["key_category"], "TEXT")
        self.assertEqual(self.store.line_source("大板小板")["key_category"], "NUMBER")

    def test_import_uses_named_key_column_not_first_column(self):
        """列順が違っても 管理番号 をキーに使うこと。

        看板_L1 / 看板_大板小板 は 管理番号 が後から追加され列順が最後になって
        いる。VBA は「先頭列 = キー」と決め打ちしていたためこの 2 テーブルでは
        資材名をキーにしてしまう危険があった。
        """
        snapshot = kanban_snapshot("看板_L1", [make_row("9")])
        # 管理番号 を末尾に移した列順を再現する
        snapshot.columns = snapshot.columns[1:] + [config.COL_KEY]
        self.gateway.tables["看板_L1"] = snapshot
        sync.import_all(self.store, self.gateway)
        self.assertEqual(self.store.line_source("L1")["key_column"], config.COL_KEY)
        self.assertEqual(self.store.items("L1")[0].mgmt_no, "9")

    def test_import_reads_form_state(self):
        result = sync.import_all(self.store, self.gateway)
        self.assertEqual(result.status_rows, 2)
        lines = {row["line"] for row in self.store.line_statuses()}
        self.assertEqual(lines, {"LVC", "倉庫"})

    def test_import_rejects_table_missing_required_columns(self):
        broken = kanban_snapshot("看板_AIM", [])
        broken.columns = [config.COL_KEY, config.COL_MATERIAL]
        self.gateway.tables["看板_AIM"] = broken
        result = sync.import_all(self.store, self.gateway)
        failed = {r.line: r.message for r in result.failed_lines}
        self.assertIn("AIM", failed)
        self.assertIn("必要な列がありません", failed["AIM"])


class ExportTest(SyncTestBase):
    def setUp(self) -> None:
        super().setUp()
        sync.import_all(self.store, self.gateway)

    def test_export_sends_only_changed_columns(self):
        self.store.apply_transition(
            "LVC", "1", models.order_button_changes, operation="order"
        )
        result = sync.export_pending(self.store, self.gateway)
        self.assertEqual(result.succeeded, 1)
        self.assertEqual(result.failed, 0)
        self.assertEqual(len(self.gateway.executed), 1)
        sql, params = self.gateway.executed[0]
        self.assertIn("UPDATE [看板_LVC] SET", sql)
        self.assertIn("WHERE [管理番号] = ?", sql)
        # **値は文へ埋め込まず、プレースホルダで渡す**
        self.assertNotIn("'〇'", sql)
        self.assertEqual(self.gateway.assignments_for("1")["欲"], "〇")
        self.assertEqual(params[-1], "1")
        # マスタ列は書き換えない
        self.assertNotIn("[資材]", sql)
        self.assertEqual(self.store.pending_count(), 0)

    def test_a_change_with_nowhere_to_go_is_reported_not_dropped(self):
        """**共有DBにその列が無い変更を、黙って飛ばさないこと。**

        ``保留`` と ``注文中日時`` は任意列(:data:`kanban.config.REQUIRED_COLUMNS`)
        なので、持たない看板テーブルが実際にありえます。以前はそういう行を
        ``continue`` で飛ばしていたため、成功にも失敗にもならず ``dirty`` が
        落ちないまま**永久に残り**、「未反映 N 件」が減らなくなっていました。
        減らないと ``start_app._busy_reason`` がアプリを終わらせません。
        """
        # 注文中を付ける(発送列も変わるので送れてしまう)→ 解除する。
        # 解除は 保留 と 注文中日時 だけなので、送り先がまったく無い
        self.store.apply_transition(
            "LVC", "1", models.order_button_changes, operation="order"
        )
        sync.export_pending(self.store, self.gateway)
        self.store.apply_transition(
            "LVC", "1", models.hold_button_changes, operation="hold"
        )
        sync.export_pending(self.store, self.gateway)
        self.store.apply_transition(
            "LVC", "1", models.hold_button_changes, operation="hold"
        )
        self.assertEqual(self.store.pending_count(), 1)

        result = sync.export_pending(self.store, self.gateway)
        self.assertEqual(result.failed, 1, "送れないのに失敗として数えていない")
        self.assertTrue(result.errors)
        # **何が足りないのかを、共有DB側の言葉で言う**(直しに行くのはそちら)
        self.assertIn("保留", result.errors[0])
        self.assertIn(config.COL_HOLD_AT, result.errors[0])

        # 諦めるまで試したら「要確認」に出る = 見えなくならない
        for _ in range(4):
            sync.export_pending(self.store, self.gateway)
        self.assertEqual(len(self.store.sync_failures()), 1)
        # そして、待っても減らないものを「まだ送れる」に数えない
        self.assertEqual(self.store.retryable_pending_count(), 0)

    def test_export_sends_one_statement_per_row(self):
        """行ごとに独立した 1 文で送ること。

        まとめて 1 つのトランザクションにすると、1 文の失敗で他も巻き戻る。
        呼び出し側は 1 文ずつ成否を見て ``dirty`` を落とすので、巻き戻された
        更新を「成功」と誤認してデータを取りこぼす(この経路で実際に
        起こりうる欠陥だった)。``SharedDb.execute`` は 1 文ずつ自動コミット
        する ── VBA 版が 1 行ずつ書き戻していたのとも一致する。
        """
        for no in ("1", "2"):
            self.store.apply_transition(
                "LVC", no, models.order_button_changes, operation="order"
            )
        sync.export_pending(self.store, self.gateway)
        self.assertEqual(len(self.gateway.executed), 2)
        keys = sorted(str(params[-1]) for _sql, params in self.gateway.executed)
        self.assertEqual(keys, ["1", "2"])

    def test_export_uses_numeric_literal_for_numeric_key(self):
        self.store.apply_transition(
            "大板小板", "1", models.order_button_changes, operation="order"
        )
        sync.export_pending(self.store, self.gateway)
        _sql, params = self.gateway.executed[0]
        # **数値列には数値で渡す。** 文字列で問い合わせると、型親和性しだいで
        # 一致せず 0 件更新になり、エラーも出ないまま送れていないことになる
        self.assertEqual(params[-1], 1)
        self.assertIsInstance(params[-1], int)

    def test_export_marks_failure_when_row_missing_in_access(self):
        self.store.apply_transition(
            "LVC", "1", models.order_button_changes, operation="order"
        )
        self.gateway.affected = 0  # Access 側に該当行なし
        result = sync.export_pending(self.store, self.gateway)
        self.assertEqual(result.succeeded, 0)
        self.assertEqual(result.failed, 1)
        self.assertEqual(self.store.pending_count(), 1)

    def test_export_keeps_pending_on_connection_error(self):
        self.store.apply_transition(
            "LVC", "1", models.order_button_changes, operation="order"
        )
        self.gateway.fail_with = "接続できません"
        result = sync.export_pending(self.store, self.gateway)
        self.assertEqual(result.failed, 1)
        self.assertEqual(self.store.pending_count(), 1)
        # 失敗回数の上限までは再送される
        self.gateway.fail_with = None
        result = sync.export_pending(self.store, self.gateway)
        self.assertEqual(result.succeeded, 1)

    def test_export_skipped_when_not_writable(self):
        self.store.apply_transition(
            "LVC", "1", models.order_button_changes, operation="order"
        )
        readonly = FakeGateway(self.gateway.tables, writable=False)
        result = sync.export_pending(self.store, readonly)
        self.assertTrue(result.skipped_reason)
        self.assertEqual(self.store.pending_count(), 1)

    def test_export_skipped_when_another_host_holds_lock(self):
        other = Store(self.store.path, host_name="PC-OTHER")
        try:
            other.acquire_lock(sync.EXPORT_LOCK_NAME)
            self.store.apply_transition(
                "LVC", "1", models.order_button_changes, operation="order"
            )
            result = sync.export_pending(self.store, self.gateway)
            self.assertIn("他の端末", result.skipped_reason)
            self.assertEqual(self.store.pending_count(), 1)
        finally:
            other.close()

    def test_export_never_touches_form_status(self):
        """**開閉を書くのは心拍だけ。** 書き戻しの待ち行列からは送らない。

        以前は待ち行列にも同じ列を書く経路があり、同じマスに書き手が 2 つ
        ある形になっていた。しかも溜まった状態は「未反映 N 件」
        (``pending_count`` は ``kanban_item`` しか数えない)に出ないので、
        詰まっても誰にも見えなかった。
        """
        self.store.apply_transition(
            "LVC", "1", models.order_button_changes, operation="order"
        )
        sync.export_pending(self.store, self.gateway)
        self.assertTrue(self.gateway.executed, "看板の書き戻しは走っているはず")
        self.assertFalse(
            [sql for sql, _ in self.gateway.executed if config.TABLE_STATE in sql],
            "書き戻しが Form状態管理 を触っている",
        )

    def test_roundtrip_import_after_export(self):
        """書き戻した内容が再取り込みで一致すること。"""
        self.store.apply_transition(
            "LVC", "1", models.order_button_changes, operation="order"
        )
        item = self.store.item("LVC", "1")
        sync.export_pending(self.store, self.gateway)

        # Access 側にも反映されたとみなして再取り込み
        self.gateway.tables["看板_LVC"] = kanban_snapshot(
            "看板_LVC",
            [
                make_row(
                    "1",
                    **{
                        config.COL_WANT: "〇",
                        config.COL_UNWANT: "",
                        config.COL_ORDERED_AT: item.ordered_at,
                    },
                ),
                make_row("2"),
            ],
        )
        sync.import_all(self.store, self.gateway)
        reloaded = self.store.item("LVC", "1")
        self.assertTrue(reloaded.is_ordered)
        self.assertEqual(reloaded.ordered_at, item.ordered_at)
        self.assertEqual(self.store.pending_count(), 0)


class HoldColumnSyncTest(SyncTestBase):
    """保留/注文中日時列(Access 側 ``保留``/``注文中日時``)の取り込み・書き戻し。

    ``kanban_snapshot``/``make_row`` は保留/注文中日時列を含まない(= その列が
    無い Access テーブルでの取り込みを既定のテストで確認済み、常設品と
    同じ「無くても取り込み自体は失敗しない」扱い)。ここでは列がある
    場合の取り込み・書き戻しを別テーブルとして確認する。
    """

    def _snapshot_with_hold(self, rows: list[dict[str, str]]):
        columns = [
            config.COL_KEY,
            config.COL_MATERIAL,
            config.COL_SIZE,
            config.COL_WANT,
            config.COL_UNWANT,
            config.COL_ORDERED_AT,
            config.COL_SHIPPED,
            config.COL_CONFIRMED_AT,
            config.COL_PERMANENT,
            config.COL_HOLD,
            config.COL_HOLD_AT,
        ]
        categories = {name: "TEXT" for name in columns}
        return TableSnapshot(
            name="看板_LVC", columns=columns, categories=categories, rows=rows
        )

    def test_import_reads_hold_and_hold_at_when_present(self):
        row = make_row(
            "1", **{config.COL_HOLD: "〇", config.COL_HOLD_AT: "2026/07/14 09:00:00"}
        )
        self.gateway.tables["看板_LVC"] = self._snapshot_with_hold([row])
        sync.import_all(self.store, self.gateway)
        item = self.store.item("LVC", "1")
        self.assertTrue(item.is_held)
        self.assertEqual(item.hold_at, "2026/07/14 09:00:00")

    def test_export_sends_hold_and_hold_at_columns(self):
        self.gateway.tables["看板_LVC"] = self._snapshot_with_hold(
            [make_row("1"), make_row("2")]
        )
        sync.import_all(self.store, self.gateway)

        # 注文中は発注済みの行にしか付かない(現場の発注を倉庫が注文する)
        self.store.apply_transition(
            "LVC", "1", models.order_button_changes, operation="order"
        )
        self.store.apply_transition(
            "LVC",
            "1",
            models.hold_button_changes,
            operation="hold",
        )
        result = sync.export_pending(self.store, self.gateway)
        self.assertEqual(result.succeeded, 1)
        sent = self.gateway.assignments_for("1")
        self.assertEqual(sent[config.COL_HOLD], "〇")
        self.assertTrue(sent[config.COL_HOLD_AT])
        self.assertEqual(sent[config.COL_SHIPPED], "")


class ExporterThreadTest(SyncTestBase):
    def test_run_once_reports_result(self):
        sync.import_all(self.store, self.gateway)
        self.store.apply_transition(
            "LVC", "1", models.order_button_changes, operation="order"
        )
        received: list[sync.ExportResult] = []
        exporter = sync.Exporter(
            self.store, self.gateway, interval_sec=0, on_result=received.append
        )
        result = exporter.run_once()
        self.assertEqual(result.succeeded, 1)
        self.assertEqual(len(received), 1)

    def test_undelivered_is_remembered_until_sent(self):
        """共有へ届かないあいだは「いつから・なぜ」を手元に覚え、届いたら消す(画面の帯)。"""
        sync.import_all(self.store, self.gateway)
        self.store.apply_transition("LVC", "1", models.order_button_changes, operation="order")
        self.gateway.exists = lambda: False
        self.gateway.fail_with = "共有フォルダが見えない"
        exporter = sync.Exporter(self.store, self.gateway, interval_sec=0)
        exporter.run_once()
        self.assertEqual(self.store.pending_count(), 1)
        since = self.store.get_meta(sync.Exporter.META_UNDELIVERED_SINCE, "")
        self.assertTrue(since)
        self.assertIn("見えません", self.store.get_meta(sync.Exporter.META_UNDELIVERED_WHY, ""))

        # 見えるが書けない(ほかの端末が使っている)。いつからは最初のまま
        self.gateway.exists = lambda: True
        exporter.run_once()
        self.assertEqual(self.store.get_meta(sync.Exporter.META_UNDELIVERED_SINCE, ""), since)
        self.assertIn("書けません", self.store.get_meta(sync.Exporter.META_UNDELIVERED_WHY, ""))

        self.gateway.fail_with = None
        exporter.run_once()
        self.assertEqual(self.store.pending_count(), 0)
        self.assertEqual(self.store.get_meta(sync.Exporter.META_UNDELIVERED_SINCE, ""), "")
        self.assertEqual(self.store.get_meta(sync.Exporter.META_UNDELIVERED_WHY, ""), "")

    def test_retry_sends_as_soon_as_shared_db_is_back(self):
        """残りがあるあいだは共有DBが見えたら周期(60秒)を待たずに送る。"""
        import time

        sync.import_all(self.store, self.gateway)
        self.store.apply_transition("LVC", "1", models.order_button_changes, operation="order")
        visible = {"now": False}
        self.gateway.exists = lambda: visible["now"]
        self.gateway.fail_with = "共有フォルダが見えない"
        exporter = sync.Exporter(self.store, self.gateway, interval_sec=3600)
        exporter.RETRY_SEC = 0.05
        exporter.start()
        try:
            exporter.request_now()
            deadline = time.monotonic() + 5
            while not self.store.get_meta(sync.Exporter.META_UNDELIVERED_SINCE, "") \
                    and time.monotonic() < deadline:
                time.sleep(0.02)
            time.sleep(0.3)
            self.assertEqual(exporter.retries, 0, "見えないのに送り直した")

            self.gateway.fail_with = None
            visible["now"] = True
            deadline = time.monotonic() + 5
            while self.store.pending_count() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(self.store.pending_count(), 0, "見えたのに周期まで送らなかった")
            self.assertGreaterEqual(exporter.retries, 1)
        finally:
            exporter.stop(final_export=False, timeout=5)
        self.assertIsNone(exporter._retry_thread)

    def test_unsent_counts_only_local_db(self):
        exporter = sync.Exporter(self.store, self.gateway, interval_sec=0)
        self.assertEqual(exporter.unsent(), 0)
        sync.import_all(self.store, self.gateway)
        self.store.apply_transition("LVC", "1", models.order_button_changes, operation="order")
        # 看板の状態 1件 + 集計のための出来事(押した記録)
        self.assertEqual(exporter.unsent(),
                         self.store.pending_count() + self.store.unsent_event_count())
        self.assertGreaterEqual(exporter.unsent(), 2)


class ImporterThreadTest(SyncTestBase):
    """``Importer``(定期 Access 再取り込み)のテスト。

    ローカル SQLite は他端末と共有していないため、これが動いていないと
    他端末が Access へ書き戻した内容はこの端末の画面にいつまでも
    反映されない(起動時の 1 回きりの取り込みだけでは足りない)。
    """

    def test_run_once_reports_result(self):
        received: list[sync.ImportResult] = []
        importer = sync.Importer(
            self.store, self.gateway, lines=["LVC"], interval_sec=0,
            on_result=received.append,
        )
        result = importer.run_once()
        self.assertTrue(result.ok_lines)
        self.assertEqual(len(received), 1)

    def test_lines_argument_accepts_callable(self):
        """現場モードのライン変更に追従できるよう、呼び出しごとに評価する。"""
        calls: list[int] = []

        def resolve_lines() -> list[str]:
            calls.append(1)
            return ["LVC"]

        importer = sync.Importer(
            self.store, self.gateway, lines=resolve_lines, interval_sec=0
        )
        importer.run_once()
        importer.run_once()
        self.assertEqual(len(calls), 2)

    def test_background_thread_runs_and_stops_cleanly(self):
        importer = sync.Importer(
            self.store, self.gateway, lines=["LVC"], interval_sec=5
        )
        importer.start()
        try:
            importer.request_now()
            for _ in range(50):
                if importer.last_result is not None:
                    break
                time.sleep(0.05)
            self.assertIsNotNone(importer.last_result)
        finally:
            importer.stop(timeout=5)

    def test_failure_does_not_crash_thread(self):
        """Access に接続できない状態でも例外を外へ漏らさない。"""

        class BrokenGateway:
            path = "broken.sqlite3"
            mode = "sqlite"

            @property
            def can_write(self) -> bool:
                return True

            def table_names(self) -> list[str]:
                raise RuntimeError("接続できません")

            def read_table(self, name: str) -> TableSnapshot:
                raise RuntimeError("接続できません")

        importer = sync.Importer(
            self.store, BrokenGateway(), lines=["LVC"], interval_sec=0
        )
        result = importer.run_once()
        # 例外を送出せず、失敗したラインとして記録される
        self.assertTrue(result.failed_lines)


class LocalPerMachineConvergenceTest(unittest.TestCase):
    """「ローカル SQLite は各端末専用の写し、正式なデータは Access」という
    構成が、実際に複数端末間でのデータ伝播として機能することの検証。

    PC ごとに完全に別の SQLite ファイルを使い、Access 役の
    ``MutableFakeGateway`` だけを共有する。VBA 版にあった「共有 SQLite」
    という概念は存在せず、Access を介してのみ端末間の状態が伝わる。
    """

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="kanban_converge_"))
        self.gateway = MutableFakeGateway(
            {"看板_LVC": kanban_snapshot("看板_LVC", [make_row("1"), make_row("2")])}
        )
        self.pc1 = Store(str(self.dir / "pc1.sqlite3"), host_name="PC1")
        self.pc1.ensure_schema()
        self.pc2 = Store(str(self.dir / "pc2.sqlite3"), host_name="PC2")
        self.pc2.ensure_schema()
        sync.import_all(self.pc1, self.gateway, lines=["LVC"])
        sync.import_all(self.pc2, self.gateway, lines=["LVC"])

    def tearDown(self) -> None:
        self.pc1.close()
        self.pc2.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_pc1_order_propagates_to_pc2_via_access(self):
        # PC1: 現場が発注操作 -> 自分のローカル SQLite へ即時反映される
        self.pc1.apply_transition(
            "LVC", "1", models.order_button_changes, operation="order"
        )
        self.assertTrue(self.pc1.item("LVC", "1").is_ordered)

        # まだ Access(共有の正データ)へは届いていないので、
        # PC2 のローカル(別ファイル)には一切見えない
        self.assertFalse(self.pc2.item("LVC", "1").is_ordered)

        # PC1: バックグラウンドの Exporter 相当(SQLite -> Access)
        export_result = sync.export_pending(self.pc1, self.gateway)
        self.assertEqual(export_result.succeeded, 1)
        self.assertEqual(self.pc1.pending_count(), 0)

        # Access には届いたが、PC2 はまだ取り込んでいないので見えない
        self.assertFalse(self.pc2.item("LVC", "1").is_ordered)

        # PC2: バックグラウンドの Importer 相当(Access -> SQLite)
        import_result = sync.import_all(self.pc2, self.gateway, lines=["LVC"])
        self.assertTrue(import_result.ok_lines)

        # ここでようやく PC2 の画面にも反映される
        self.assertTrue(self.pc2.item("LVC", "1").is_ordered)

    def test_pc2_pending_change_survives_reimport_of_pc1_change(self):
        """PC2 自身の未反映操作は、PC1 由来の再取り込みでも消えない。"""
        # PC2: まだ Access へ送っていない自分の操作
        self.pc2.apply_transition(
            "LVC", "2", models.order_button_changes, operation="order"
        )
        self.assertTrue(self.pc2.item("LVC", "2").is_ordered)

        # PC1: 別の行を発注して Access へ反映
        self.pc1.apply_transition(
            "LVC", "1", models.order_button_changes, operation="order"
        )
        sync.export_pending(self.pc1, self.gateway)

        # PC2: この状態で Access を再取り込みしても
        sync.import_all(self.pc2, self.gateway, lines=["LVC"])

        # PC1 由来の変更は反映され、PC2 自身の未反映操作も消えない
        self.assertTrue(self.pc2.item("LVC", "1").is_ordered)
        self.assertTrue(self.pc2.item("LVC", "2").is_ordered)
        self.assertEqual(self.pc2.pending_count(), 1)

        # PC2 の分もその後 Access へ届けば、PC1 側にも伝播する
        sync.export_pending(self.pc2, self.gateway)
        sync.import_all(self.pc1, self.gateway, lines=["LVC"])
        self.assertTrue(self.pc1.item("LVC", "2").is_ordered)

    def test_ship_without_order_is_rejected_even_across_machines(self):
        """PC2 が「発注の無い行」を発送しようとするケースが、
        Access 経由でも正しく業務ルールに従うこと。"""
        from kanban.domain.models import OrderMissingError

        with self.assertRaises(OrderMissingError):
            self.pc2.apply_transition(
                "LVC", "2", models.ship_button_changes, operation="ship"
            )

    def test_site_and_warehouse_do_not_clobber_each_others_column(self):
        """現場(発注列)と倉庫(発送列)が同じ行の別々の列を独立に
        操作しても、Access を介した同期で互いを消し合わないこと。

        これは「ローカル SQLite は各端末専用の写し」という構成にした
        ことで新たに気をつけるべき点だった: 状態列をまとめて 1 つの
        dict として持ち回す設計のままだと、片方の端末が自分の知る
        (Access より古いかもしれない)値を無自覚に書き戻して、
        もう片方の端末の変更を上書きしてしまう恐れがある。
        (kanban/db/store.py の dirty_columns による列単位の保護で対策済み)
        """
        # PC1(現場役): 発注する
        self.pc1.apply_transition(
            "LVC", "1", models.order_button_changes, operation="order"
        )
        sync.export_pending(self.pc1, self.gateway)

        # PC2(倉庫役): Access から最新(発注済み)を取り込んでから発送する
        sync.import_all(self.pc2, self.gateway, lines=["LVC"])
        self.assertTrue(self.pc2.item("LVC", "1").is_ordered)
        self.pc2.apply_transition(
            "LVC", "1", models.ship_button_changes, operation="ship"
        )
        sync.export_pending(self.pc2, self.gateway)

        # ここまでで Access 上は「発注済み・発送済み」のはず
        access_row = self.gateway.tables["看板_LVC"].rows[0]
        self.assertEqual(access_row["欲"], "〇")
        self.assertEqual(access_row["発送"], "〇")

        # PC1(現場役): まだ倉庫の発送を知らないまま、発注を解除する
        # (欲→不。発送列には一切触れない操作)
        self.pc1.apply_transition(
            "LVC", "1", models.order_button_changes, operation="order"
        )
        self.assertFalse(self.pc1.item("LVC", "1").is_ordered)
        sync.export_pending(self.pc1, self.gateway)

        # PC1 の書き戻し後も、Access 上の発送済みは消えていないこと
        access_row = self.gateway.tables["看板_LVC"].rows[0]
        self.assertEqual(access_row["欲"], "", "現場の発注解除は反映される")
        self.assertEqual(
            access_row["発送"], "〇", "倉庫の発送は現場の書き戻しで消えてはいけない"
        )

        # PC2(倉庫役)が改めて取り込んでも、両方の変更が矛盾なく見える
        sync.import_all(self.pc2, self.gateway, lines=["LVC"])
        final = self.pc2.item("LVC", "1")
        self.assertFalse(final.is_ordered)
        self.assertTrue(final.is_shipped)


if __name__ == "__main__":
    unittest.main()


class ImporterWatchTest(unittest.TestCase):
    """共有DBが変わったら、周期(30 秒)を待たずに取り込む。

    相手の端末が書いたコメント・発送が、押さなくても数秒で出るようにするため
    (周期だけだと最大 30 秒かかった)。中身は写さず、大きさと更新時刻だけを見る。
    """

    def test_change_wakes_the_importer_without_waiting_for_the_interval(self):
        import tempfile
        import time
        from pathlib import Path

        from kanban.db.shared import SharedDb
        from kanban.db.sync import Importer

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "看板マスタ.sqlite3"
            path.write_bytes(b"x")
            importer = Importer(None, SharedDb(str(path)), lines=[], interval_sec=3600, watch_sec=0.05)
            woke = []
            importer._task.request_now = lambda: woke.append(time.monotonic())
            importer.start()
            try:
                time.sleep(0.3)
                self.assertEqual(woke, [], "変わっていなければ起こさない")
                path.write_bytes(b"xy")      # 相手の端末が書いた
                deadline = time.monotonic() + 3
                while not woke and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertEqual(len(woke), 1)
                self.assertEqual(importer.wakes, 1)
                path.unlink()                # 共有フォルダに届かない → 何もしない(周期に任せる)
                time.sleep(0.3)
                self.assertEqual(len(woke), 1)
            finally:
                importer.stop(timeout=2)
            self.assertIsNone(importer._watch_thread)

    def test_zero_turns_the_watch_off(self):
        from kanban.db.shared import SharedDb
        from kanban.db.sync import Importer

        importer = Importer(None, SharedDb("どこにも無い.sqlite3"), lines=[], interval_sec=3600, watch_sec=0)
        importer.start()
        try:
            self.assertIsNone(importer._watch_thread)
        finally:
            importer.stop(timeout=2)
