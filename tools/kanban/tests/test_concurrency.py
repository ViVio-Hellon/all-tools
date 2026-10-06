"""複数端末からの同時アクセスの検証。

VBA 版は Access ファイルを共有し、ロック競合をリトライで凌いでいた。
Python 版が同等以上に守れているかを、**実際に別プロセスを起動して**
同じ SQLite を同時に叩くことで確認する(スレッドではなく別プロセスに
するのは、端末が別々である状況に近づけるため)。

確認する不変条件

* 更新が失われない / 二重に適用されない
  → ``rev`` の増分合計と操作履歴の件数が一致する
* 矛盾した状態にならない(欲と不が同時に ``〇`` など)
* ロック競合で異常終了しない
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from kanban.db.store import DEFAULT_COLUMN_MAP, ConflictError, Store
from kanban.domain import models

REPO_ROOT = Path(__file__).resolve().parent.parent

#: 別プロセスで動かすワーカー。ランダムに項目を選んで発注 / 発送を繰り返す。
_WORKER = r"""
import json, random, sys
sys.path.insert(0, {repo!r})
from kanban.db.store import Store, ConflictError, LockTimeout
from kanban.domain import models

db, host, n_ops, seed = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
random.seed(seed)
store = Store(db, host_name=host, busy_timeout_ms=8000, max_retry=3)
result = {{"host": host, "ok": 0, "conflicts": 0, "locktimeouts": 0, "errors": []}}
for _ in range(n_ops):
    line = random.choice(["LVC", "LS"])
    target = random.choice(store.items(line))
    try:
        if random.random() < 0.5:
            store.apply_transition(line, target.mgmt_no, models.order_button_changes,
                                   operation="order", expected_rev=target.rev)
        else:
            store.apply_transition(
                line, target.mgmt_no,
                lambda it: models.ship_button_changes(it) if (it.is_ordered or it.is_shipped) else {{}},
                operation="ship", expected_rev=target.rev)
        result["ok"] += 1
    except ConflictError:
        result["conflicts"] += 1
    except LockTimeout:
        result["locktimeouts"] += 1
    except Exception as exc:
        result["errors"].append(f"{{type(exc).__name__}}: {{exc}}")
store.close()
print(json.dumps(result))
"""


def build_rows(count: int) -> list[dict[str, str]]:
    return [
        {
            "mgmt_no": str(i),
            "material": f"資材{i % 5}",
            "size": f"S{i}",
            "want": "",
            "unwant": "〇",
            "ordered_at": "",
            "shipped": "",
            "confirmed_at": "",
            "permanent": "",
        }
        for i in range(1, count + 1)
    ]


class MultiProcessConcurrencyTest(unittest.TestCase):
    """別プロセス(別端末相当)からの同時更新。"""

    ITEMS_PER_LINE = 20
    WORKERS = 4
    OPS_PER_WORKER = 20

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="kanban_conc_"))
        self.db_path = str(self.dir / "kanban.sqlite3")
        store = Store(self.db_path, host_name="PC-SETUP")
        store.ensure_schema()
        for line in ("LVC", "LS"):
            store.import_line(
                line=line,
                table_name=f"看板_{line}",
                key_column="管理番号",
                key_category="TEXT",
                column_map=dict(DEFAULT_COLUMN_MAP),
                categories={name: "TEXT" for name in DEFAULT_COLUMN_MAP.values()},
                rows=build_rows(self.ITEMS_PER_LINE),
                source_path="dummy.accdb",
            )
        store.close()

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_no_lost_updates_across_processes(self):
        script = _WORKER.format(repo=str(REPO_ROOT))
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", script, self.db_path, f"PC-{i}",
                 str(self.OPS_PER_WORKER), str(i)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for i in range(1, self.WORKERS + 1)
        ]
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=120)
            self.assertEqual(process.returncode, 0, f"ワーカーが異常終了: {stderr}")
            results.append(json.loads(stdout.strip().splitlines()[-1]))

        for result in results:
            self.assertEqual(result["errors"], [], f"{result['host']} でエラー")
            self.assertEqual(
                result["locktimeouts"], 0, f"{result['host']} でロック待ちが解消しなかった"
            )

        attempted = self.WORKERS * self.OPS_PER_WORKER
        succeeded = sum(r["ok"] for r in results)
        conflicts = sum(r["conflicts"] for r in results)
        self.assertEqual(succeeded + conflicts, attempted)
        # 競合で弾かれるのは一部だけ(全部弾かれていたら実質動いていない)
        self.assertGreater(succeeded, attempted * 0.5)

        store = Store(self.db_path, host_name="PC-CHECK")
        try:
            conn = store.connection
            total_items, rev_sum = conn.execute(
                "SELECT COUNT(*), SUM(rev) FROM kanban_item"
            ).fetchone()
            applied = conn.execute(
                "SELECT COUNT(*) FROM operation_log WHERE operation IN ('order','ship')"
            ).fetchone()[0]

            # rev は更新が確定するたびに 1 だけ増える。
            # 「増分の合計 == 実際に適用された操作の件数」であれば、
            # 更新の取りこぼしも二重適用も起きていない。
            self.assertEqual(rev_sum - total_items, applied)

            # 状態の矛盾がないこと
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM kanban_item WHERE want='〇' AND unwant='〇'"
                ).fetchone()[0],
                0,
                "欲と不が同時に〇になっている",
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM kanban_item WHERE want='' AND unwant=''"
                ).fetchone()[0],
                0,
                "欲も不も空になっている",
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM kanban_item"
                    " WHERE shipped='〇' AND confirmed_at=''"
                ).fetchone()[0],
                0,
                "発送済みなのに倉庫確認日時が入っていない",
            )
            # 更新された行はすべて Access への書き戻し待ちになっている
            self.assertGreater(store.pending_count(), 0)
        finally:
            store.close()


class SiteAndWarehouseConflictTest(unittest.TestCase):
    """現場と倉庫が同じ項目をほぼ同時に操作した場合。

    現場(発注ボタン)と倉庫(発送ボタン)は別々の画面だが、同じ行を
    触ることがある。片方の操作が黙って消えないことを確認する。
    """

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="kanban_conflict_"))
        self.site = Store(str(self.dir / "kanban.sqlite3"), host_name="PC-現場")
        self.site.ensure_schema()
        self.site.import_line(
            line="LVC",
            table_name="看板_LVC",
            key_column="管理番号",
            key_category="TEXT",
            column_map=dict(DEFAULT_COLUMN_MAP),
            categories={name: "TEXT" for name in DEFAULT_COLUMN_MAP.values()},
            rows=build_rows(3),
            source_path="dummy.accdb",
        )
        self.warehouse = Store(self.site.path, host_name="PC-倉庫")

    def tearDown(self) -> None:
        self.site.close()
        self.warehouse.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_warehouse_ship_then_stale_site_click_is_rejected(self):
        # 現場が発注 -> 倉庫が発送
        self.site.apply_transition(
            "LVC", "1", models.order_button_changes, operation="order"
        )
        snapshot = self.site.item("LVC", "1")  # 現場の画面が持っている版
        self.warehouse.apply_transition(
            "LVC", "1", models.ship_button_changes, operation="ship"
        )

        # 現場が古い画面のままサイズボタンを押す -> 競合として弾かれる
        with self.assertRaises(ConflictError):
            self.site.apply_transition(
                "LVC",
                "1",
                models.order_button_changes,
                operation="order",
                expected_rev=snapshot.rev,
            )
        # 倉庫の発送は消えていない
        current = self.site.item("LVC", "1")
        self.assertTrue(current.is_shipped)
        self.assertTrue(current.is_ordered)

    def test_reload_then_retry_succeeds(self):
        """再読み込みしてからやり直せば通ること。"""
        self.site.apply_transition(
            "LVC", "1", models.order_button_changes, operation="order"
        )
        self.warehouse.apply_transition(
            "LVC", "1", models.ship_button_changes, operation="ship"
        )
        # 画面を更新してから「届いた」として押し直す
        fresh = self.site.item("LVC", "1")
        updated = self.site.apply_transition(
            "LVC",
            "1",
            lambda item: models.order_button_changes(item, treat_as_delivered=True),
            operation="order",
            expected_rev=fresh.rev,
        )
        self.assertFalse(updated.is_ordered)
        self.assertFalse(updated.is_shipped)

    def test_batch_reset_does_not_touch_rows_changed_meanwhile(self):
        """一括処理の対象選定と更新が 1 トランザクションで行われること。"""
        for no in ("1", "2"):
            self.site.apply_transition(
                "LVC", no, models.order_button_changes, operation="order"
            )
            self.warehouse.apply_transition(
                "LVC", no, models.ship_button_changes, operation="ship"
            )
        # 3 番は発注のみ(一括リセットの対象外)
        self.site.apply_transition(
            "LVC", "3", models.order_button_changes, operation="order"
        )

        updated = self.site.apply_batch(
            "LVC",
            select=lambda item: item.is_delivered_candidate,
            changes_for=lambda item: models.batch_reset_changes(),
            operation="batch_reset",
        )
        self.assertEqual({item.mgmt_no for item in updated}, {"1", "2"})
        self.assertTrue(self.site.item("LVC", "3").is_ordered)


if __name__ == "__main__":
    unittest.main()
