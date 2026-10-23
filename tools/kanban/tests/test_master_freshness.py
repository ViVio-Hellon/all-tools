"""マスタを直したあと、**前の中身のまま動く・出す**ところが無いか

【現場から】

    マスタを直しても前の中身のまま動く・表示する事がないかのチェックをしてください。
    例: 資材重量 変更後 前の重量のまま計算

看板のマスタは共有DB(看板マスタ.sqlite3)の ``看板_<ライン>`` の ``資材``・``サイズ``(1 回の量
``…:3本`` もここ)・``常設品``。各端末は共有DBを手元へ写して読み、手元の SQLite へ取り込んで
盤・発注・印刷に使う。直し方は 3 通りあり、それぞれ新しい値が届くまでを確かめる。

* **この端末のマスタ管理で直す** → 書いたその場で取り込む(盤・発注・印刷がすぐ新しい値)
* **ほかの PC が直す** → 次の取り込み(共有DBが変わったのを 2 秒ごとに見る。周期は 30 秒)
* **外(Access の最新で入れ替える等)で直す** → 入れ替えた端末はその場、ほかは次の取り込み

あわせて、新しい値が**届かない**ときにそれが見えることを確かめる(取り込めていないのに
「取り込めた」ことにしない・「すぐ反映されます」と言わない・マスタ管理が古い写しを出さない)。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from kanban import admin_lock, config
from kanban.db import sync
from kanban.db.shared import SharedDb
from kanban.db.store import Store
from kanban.domain import events

LINE = "LVC"
TABLE = f"看板_{LINE}"
COLUMNS = ("管理番号", "資材", "サイズ", "欲", "不", "更新日", "発送", "倉庫確認日時", "常設品", "保留",
           "注文中日時")
ON = config.MARK_ON
TOKEN = "test-token"
PASSWORD = "秘密"


class MasterFreshnessBase(unittest.TestCase):
    """現場の端末 1 台(本物の共有DBファイル・写し・取り込み)とアプリの経路。"""

    MODE = config.MODE_SITE

    def setUp(self) -> None:
        from app import create_app

        self.dir = Path(tempfile.mkdtemp(prefix="kanban_master_fresh_"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        patcher = mock.patch.dict(os.environ, {"KANBAN_LOCAL_DIR": str(self.dir / "local")})
        patcher.start()
        self.addCleanup(patcher.stop)
        admin_lock.reset()
        self.addCleanup(admin_lock.reset)

        self.shared = self.dir / "看板マスタ.sqlite3"
        conn = sqlite3.connect(str(self.shared))
        conn.execute(f"CREATE TABLE [{TABLE}] ({', '.join(f'[{c}]' for c in COLUMNS)})")
        conn.executemany(
            f"INSERT INTO [{TABLE}] VALUES ({', '.join('?' for _ in COLUMNS)})",
            [(1, "外装紙", "1600 × 150M:3本", "", "", "", "", "", ON, "", ""),
             (2, "外装紙", "1300 × 100M:4本", "", "", "", "", "", ON, "", ""),
             (3, "テープ", "幅50:2巻", "", "", "", "", "", ON, "", "")])
        conn.commit()
        conn.close()
        (self.dir / "config.json").write_text(json.dumps({
            "shared_db_path": str(self.shared), "admin_password_hash": config.hash_password(PASSWORD),
            "line": LINE}, ensure_ascii=False), encoding="utf-8")

        self.store = Store(str(self.dir / "kanban.sqlite3"), host_name="現場PC")
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        # アプリと同じ組み方: 取り込みは周期の口(Importer)、「いま取り込む」は写し直してから
        # (start_app._manual_refresh)
        self.gateway = SharedDb(str(self.shared), cache_dir=str(self.dir / "cache"))
        self.importer = sync.Importer(self.store, self.gateway, [LINE], interval_sec=0)
        self.app = create_app(self.MODE, token=TOKEN, port=8741, store=self.store, line=LINE)
        self.app.config["CONFIG_PATH"] = str(self.dir / "config.json")
        self.app.config["IMPORT_INTERVAL_SEC"] = 30
        self.app.config["MANUAL_REFRESH"] = self.manual_refresh
        self.client = self.app.test_client()
        self.importer.run_once()          # 起動時の取り込み

    def manual_refresh(self):
        self.gateway.forget_copy()
        return self.importer.run_once()

    # -- 小道具 ---------------------------------------------------------
    def get(self, path: str):
        return self.client.get(path, headers={"X-Tool-Token": TOKEN})

    def post(self, path: str, body: dict | None = None):
        return self.client.post(path, json=body or {}, headers={"X-Tool-Token": TOKEN})

    def board_rows(self) -> dict[str, tuple[str, str, bool]]:
        """盤の看板ごとの (資材, サイズ, 非常設品か)。"""
        board = self.get(f"/api/board?line={LINE}").json["board"]
        return {r["mgmt_no"]: (g["material"], r["size"]["label"], r["non_permanent"])
                for g in board["groups"] for r in g["rows"]}

    def master_row(self, no: int) -> tuple[dict, str]:
        data = self.get(f"/api/master/table?table={TABLE}").json
        row = next(r for r in data["rows"] if int(float(r["管理番号"])) == no)
        return row, data["row_key"]

    def edit(self, no: int, column: str, value: str):
        """マスタ管理で 1 マス直す(画面と同じく、開いて出ていた値を添える)。"""
        row, key = self.master_row(no)
        return self.post("/api/master/update", {
            "table": TABLE, "row_key": row[key], "key": row["管理番号"], "column": column,
            "value": value, "before": row[column], "password": PASSWORD})

    def other_pc_writes(self, sql: str, params=(), *, keep_stamp: bool = False) -> None:
        """ほかの PC が共有DBを直す(このプロセスの写しの印には触れない)。

        ``keep_stamp`` は Windows の共有フォルダの「大きさ・更新時刻を数秒覚えて返す」を
        まねる(書いたあとも、こちらからは前と同じ大きさ・更新時刻に見える)。
        """
        before = self.shared.stat()
        conn = sqlite3.connect(str(self.shared))
        conn.execute(sql, params)
        conn.commit()
        conn.close()
        if keep_stamp:
            self.assertEqual(self.shared.stat().st_size, before.st_size, "試験の前提: 大きさが変わった")
            os.utime(self.shared, ns=(before.st_atime_ns, before.st_mtime_ns))

    def order(self, no: str):
        res = self.post("/api/board/order",
                        {"line": LINE, "mgmt_no": no, "rev": self.store.item(LINE, no).rev})
        self.assertEqual(res.status_code, 200, res.json)
        return res

    def report(self) -> str:
        res = self.get(f"/report/site?line={LINE}")
        self.assertEqual(res.status_code, 200)
        return res.get_data(as_text=True)


class ThisTerminalEditsTest(MasterFreshnessBase):
    """(a) この端末のマスタ管理で直す → 盤・発注・印刷がその場で新しい値。"""

    def test_size_material_and_permanent_reach_board_order_and_print(self):
        self.assertEqual(self.board_rows()["1"], ("外装紙", "1600 × 150M:3本", False))
        for column, value in (("サイズ", "1600 × 150M:5本"), ("資材", "中性紙"), ("常設品", "×")):
            res = self.edit(1, column, value)
            self.assertEqual(res.status_code, 200, res.json)
            self.assertIn("看板画面にもすぐ反映されます", res.json["message"])

        # 盤: 新しい資材の枠に、新しいサイズ・非常設品として出る(前の枠に残らない)
        rows = self.board_rows()
        self.assertEqual(rows["1"], ("中性紙", "1600 × 150M:5本", True), "盤が直す前のまま")
        self.assertEqual(rows["2"][0], "外装紙", "直していない看板まで変わった")

        # 発注: 押した記録(看板履歴へ送る出来事)は新しい資材・サイズで残る(集計の量はここから読む)
        self.order("1")
        ordered = [e for e in self.store.unsent_events() if e["kind"] == events.ORDERED]
        self.assertEqual([(e["material"], e["size"]) for e in ordered], [("中性紙", "1600 × 150M:5本")],
                         "発注の記録が直す前の資材・サイズ(集計の量が前のまま)")

        # 印刷: 注文票は新しい資材・サイズ
        page = self.report()
        self.assertIn("中性紙", page)
        self.assertIn("1600 × 150M:5本", page)
        self.assertNotIn("1600 × 150M:3本", page, "注文票が直す前のサイズ")
        self.assertEqual(self.get(f"/api/report/available?line={LINE}").json["reports"][0]["count"], 1)

    def test_an_ordered_kanban_not_yet_sent_still_takes_the_new_size(self):
        """発注を押してまだ共有DBへ送っていない看板も、直したサイズで盤・注文票に出る(発注は残る)。"""
        self.order("2")
        self.assertTrue(self.store.has_unsent(LINE, "2"), "試験の前提: 発注をまだ送っていない")
        res = self.edit(2, "サイズ", "1300 × 100M:6本")
        self.assertEqual(res.status_code, 200, res.json)
        item = self.store.item(LINE, "2")
        self.assertEqual(item.size, "1300 × 100M:6本", "送っていない発注がある看板だけ前のサイズのまま")
        self.assertTrue(item.is_ordered, "取り込みで発注を消した")
        self.assertIn("1300 × 100M:6本", self.report())


class OtherTerminalEditsTest(MasterFreshnessBase):
    """(b) ほかの PC が直す → 次の取り込み(周期・共有DBが変わったのを見たとき)で新しい値。"""

    def test_next_import_brings_the_new_size_to_board_and_print(self):
        self.order("1")
        token = self.get("/api/status").json["token"]
        self.other_pc_writes(f"UPDATE [{TABLE}] SET [サイズ] = ? WHERE [管理番号] = 1", ("1600 × 150M:8本",))
        self.importer.run_once()           # 周期の取り込み(写し直せとは言われていない)
        self.assertNotEqual(self.get("/api/status").json["token"], token, "盤が描き直されない")
        self.assertEqual(self.board_rows()["1"][1], "1600 × 150M:8本")
        self.assertIn("1600 × 150M:8本", self.report())
        self.assertTrue(self.store.item(LINE, "1").is_ordered, "取り込みで発注を消した")


class MasterScreenReadsFreshTest(MasterFreshnessBase):
    """マスタ管理の表は、開くたびに共有DBの**いまの**中身を出す。"""

    def test_opening_the_table_right_after_another_pc_edited_shows_the_new_value(self):
        """共有フォルダが大きさ・更新時刻を前のまま返しても(Windows の共有フォルダの控え)。

        以前は写しを「大きさと更新時刻が同じ」で使い回していたので、ほかの PC が直した直後に
        開くと直す前の値が出た。直そうとすると「開き直してください」と断られ、開き直しても同じ。
        """
        row, _key = self.master_row(1)
        self.assertEqual(row["サイズ"], "1600 × 150M:3本")
        self.other_pc_writes(f"UPDATE [{TABLE}] SET [サイズ] = ? WHERE [管理番号] = 1", ("1600 × 150M:7本",),
                             keep_stamp=True)
        row, _key = self.master_row(1)
        self.assertEqual(row["サイズ"], "1600 × 150M:7本", "マスタ管理が直す前の中身を出した")
        # 出ている値で直せる(「開き直してください」と断られない)
        res = self.edit(1, "サイズ", "1600 × 150M:9本")
        self.assertEqual(res.status_code, 200, res.json)
        self.assertEqual(self.board_rows()["1"][1], "1600 × 150M:9本")


class OutsideEditsTest(MasterFreshnessBase):
    """(c) 外で直す: Access の最新で表の中身を入れ替える → 入れ替えた端末はその場で新しい値。"""

    def setUp(self) -> None:
        super().setUp()
        from kanban import table_refresh

        self.addCleanup(table_refresh._cache.clear)
        self.src = self.dir / "看板マスタ_最新.sqlite3"
        conn = sqlite3.connect(str(self.src))
        conn.execute(f"CREATE TABLE [{TABLE}] ({', '.join(f'[{c}]' for c in COLUMNS)})")
        conn.executemany(
            f"INSERT INTO [{TABLE}] VALUES ({', '.join('?' for _ in COLUMNS)})",
            [(1, "外装紙", "1600 × 150M:10本", "", "", "", "", "", ON, "", ""),
             (2, "外装紙", "1300 × 100M:4本", "", "", "", "", "", ON, "", ""),
             (3, "テープ", "幅50:2巻", "", "", "", "", "", ON, "", "")])
        conn.commit()
        conn.close()

    def refresh(self):
        plan = self.post("/api/table-refresh/plan", {"path": str(self.src), "password": PASSWORD}).json["plan"]
        self.assertEqual([t["name"] for t in plan["tables"] if t["can_refresh"]], [TABLE])
        res = self.post("/api/table-refresh/run", {"path": str(self.src), "tables": [TABLE], "password": PASSWORD})
        self.assertEqual(res.status_code, 200, res.json)
        return res.json

    def test_refresh_from_access_reaches_the_board_and_print(self):
        self.order("1")
        sync.export_pending(self.store, self.gateway)     # 発注は共有DBへ届いている
        out = self.refresh()
        self.assertIn("この端末は取り込み直しました", out["reimported"])
        self.assertEqual(self.board_rows()["1"][1], "1600 × 150M:10本")
        self.assertIn("1600 × 150M:10本", self.report())
        self.assertTrue(self.store.item(LINE, "1").is_ordered, "入れ替えで発注を消した")

    def test_refresh_does_not_say_reimported_when_the_import_failed(self):
        self.importer.gateway = SharedDb(str(self.dir / "届かない.sqlite3"), cache_dir=str(self.dir / "cache_x"))
        out = self.refresh()
        self.assertNotIn("取り込み直しました", out["reimported"], "取り込めていないのに取り込み直したと言った")
        self.assertIn("まだ取り込めていません", out["reimported"])


class ImportFailureIsVisibleTest(MasterFreshnessBase):
    """新しい値が**届かない**ときは、届いたことにしない(盤は前の中身のまま、と分かるように)。"""

    def age_last_import(self, minutes: int = 20) -> None:
        old = datetime.now() - timedelta(minutes=minutes)
        self.store.set_meta("last_import_at", old.strftime("%Y/%m/%d %H:%M:%S"))

    def status(self) -> dict:
        return self.get("/api/status").json

    def test_unreachable_shared_db_is_not_counted_as_imported(self):
        """共有DBが見えないのに「最後に取り込めた時刻」を進めると、「取り込めていません」が出ない。"""
        self.age_last_import()
        self.shared.rename(self.dir / "どこか.sqlite3")
        self.importer.run_once()
        s = self.status()
        self.assertTrue(s["import_stale"], "共有DBが見えないのに取り込めたことになった")
        self.assertIn(TABLE, s["import_problem"])

    def test_a_table_that_cannot_be_read_is_not_counted_as_imported(self):
        """共有フォルダは見えるが、ラインの表が読めない(列の名前が変わった)。盤は前の中身のまま。"""
        self.age_last_import()
        self.other_pc_writes(f"ALTER TABLE [{TABLE}] RENAME COLUMN [資材] TO [品名]")
        self.gateway.forget_copy()
        result = self.importer.run_once()
        self.assertEqual([r.line for r in result.failed_lines], [LINE])
        s = self.status()
        self.assertTrue(s["import_stale"], "読めない表があるのに取り込めたことになった")
        self.assertIn("資材", s["import_problem"])
        # 直れば元どおり(帯は消える)
        self.other_pc_writes(f"ALTER TABLE [{TABLE}] RENAME COLUMN [品名] TO [資材]")
        self.gateway.forget_copy()
        self.importer.run_once()
        s = self.status()
        self.assertFalse(s["import_stale"])
        self.assertEqual(s["import_problem"], "")

    def test_a_missing_table_has_its_own_banner(self):
        """表が無いラインは盤に別の帯が出る。「取り込めていません」は出さない(二重に言わない)。"""
        self.age_last_import()
        self.other_pc_writes(f"ALTER TABLE [{TABLE}] RENAME TO [{TABLE}_old]")
        self.gateway.forget_copy()
        self.importer.run_once()
        self.assertFalse(self.status()["import_stale"])
        self.assertIn("がありません", self.get(f"/api/board?line={LINE}").json["board"]["missing_table"])

    def test_master_edit_does_not_say_reflected_when_the_import_failed(self):
        """書けたあと、この端末へ取り込めなかったら「看板画面にもすぐ反映されます」と言わない。"""
        broken = SharedDb(str(self.dir / "届かない.sqlite3"), cache_dir=str(self.dir / "cache_x"))
        self.importer.gateway = broken     # この端末の取り込みだけが共有DBを読めない
        res = self.edit(1, "サイズ", "1600 × 150M:5本")
        self.assertEqual(res.status_code, 200, res.json)
        self.assertNotIn("すぐ反映", res.json["message"], "取り込めていないのに反映されると言った")
        self.assertIn("まだ取り込めていません", res.json["message"])
        self.assertEqual(self.board_rows()["1"][1], "1600 × 150M:3本", "試験の前提: 盤は前の中身のまま")
        # 取り込みが途中で止まった(Importer.run_once は空の結果を返す)ときも同じ
        self.app.config["MANUAL_REFRESH"] = lambda: sync.ImportResult()
        res = self.edit(1, "サイズ", "1600 × 150M:6本")
        self.assertIn("まだ取り込めていません", res.json["message"])


if __name__ == "__main__":
    unittest.main()
