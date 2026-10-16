"""連携部の総当たり: **本番と同じ形の共有DB**で、現場と倉庫の 2 台が全部の操作を回す。

VER3.5.2 までの連携の試験は、共有DBをこちらで**型を書いて**作っていた(``管理番号 NUMERIC``
など)。本番の共有DBは Access を変換したもので、変換ツールは ``CREATE TABLE x ("列1", "列2")``
と**型を書かずに**表を作る(python-web-tools の source_db.py に「現物で踏んだ」とある)。
型の無い列に数の ``1`` が入っていると、文字の ``'1'`` の UPDATE は 0 行になる ── 試験は
通るのに現場では「対象行なし」が出た(看板_AIM の件)。

ここでは共有DBの形を総当たりにする:

* 管理番号の列の宣言: 無し(本番)・TEXT・INTEGER・NUMERIC・REAL・LONG・VARCHAR(255)・DOUBLE
* 管理番号の入れ方: 数 1・数 1.0・文字 '1'・文字 '1.0'・全角 '１'・空白つき ' 1 '
* ほかの列: 型無し(本番)/ TEXT。空の欄は NULL(変換ツールの出し方)/ 空文字
* 更新日の書き方: '2026-10-16 08:32:00'(変換ツール)/ '2026/10/16 08:32:00'

それぞれで、現場(1 台)と倉庫(1 台)が**別々の手元 SQLite・別々の写し**を持って、
発注・発注解除・発送・発送解除・注文中・注文中解除・届いた確認・一括発送・一括確認・
取り消された発送の確認・コメントを回す。1 操作ごとに確かめる:

1. 押した端末の手元 == 共有DB == もう 1 台が取り込んだ手元(状態の列すべて)
2. 書き戻しのエラーが無い・未反映 0・要確認 0・帯の「届いていません」が出ない
3. 共有DBの行数・管理番号の値と型(typeof)が変わらない(書き方を変えない・行が増えない)

``KANBAN_SHAPES_REPORT`` に置き場所を渡すと、結果を JSON で書き出す(アーティファクト用)。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from kanban import config
from kanban.db import sync
from kanban.db.shared import SharedDb
from kanban.db.store import Store
from kanban.domain import models
from kanban.domain.service import KanbanService

LINE = "AIM"
TABLE = f"看板_{LINE}"

KEY_DECLS = ["", "TEXT", "INTEGER", "NUMERIC", "REAL", "LONG", "VARCHAR(255)", "DOUBLE"]
KEY_VALUES = [("数 1", lambda n: n), ("数 1.0", lambda n: float(n)), ("文字 '1'", lambda n: str(n)),
              ("文字 '1.0'", lambda n: f"{n}.0"), ("全角 '１'", lambda n: chr(0xFF10 + n)),
              ("空白つき ' 1 '", lambda n: f" {n} ")]
OTHER_DECLS = [("型無し", ""), ("TEXT", "TEXT")]
EMPTIES = [("NULL", None), ("空文字", "")]
DATE_FORMS = [("2026-10-16 08:32:00", "2026-10-16 08:32:00"), ("2026/10/16 08:32:00", "2026/10/16 08:32:00")]

STATE = [(f, models_col) for f, models_col in (
    ("want", config.COL_WANT), ("unwant", config.COL_UNWANT), ("ordered_at", config.COL_ORDERED_AT),
    ("shipped", config.COL_SHIPPED), ("confirmed_at", config.COL_CONFIRMED_AT),
    ("hold", config.COL_HOLD), ("hold_at", config.COL_HOLD_AT))]
COLUMNS = [config.COL_KEY, config.COL_MATERIAL, config.COL_SIZE, config.COL_WANT, config.COL_UNWANT,
           config.COL_ORDERED_AT, config.COL_SHIPPED, config.COL_CONFIRMED_AT, config.COL_PERMANENT,
           config.COL_HOLD, config.COL_HOLD_AT]
ON = config.MARK_ON


def _norm(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _same(a: Any, b: Any) -> bool:
    left, right = _norm(a), _norm(b)
    if left == right:
        return True
    da, db = models.parse_datetime(left), models.parse_datetime(right)
    return da is not None and da == db


def _bodies(thread: dict) -> list[str]:
    return [c.get("body", "") for part in thread.values() for c in part if c.get("kind") == "コメント"]


class Terminal:
    """1 台の端末(手元の SQLite・共有DBの写し・業務の窓口)。"""

    def __init__(self, root: Path, name: str, shared: Path):
        self.name = name
        self.store = Store(str(root / f"{name}.sqlite3"), host_name=name)
        self.store.ensure_schema()
        self.gateway = SharedDb(str(shared), cache_dir=str(root / f"cache_{name}"))
        self.svc = KanbanService(self.store)
        self.errors: list[str] = []

    def send(self) -> None:
        result = sync.export_pending(self.store, self.gateway)
        self.errors += result.errors
        sync.export_comments(self.store, self.gateway)

    def take(self) -> None:
        self.gateway.forget_copy()
        result = sync.import_all(self.store, self.gateway, lines=[LINE])
        if not result.ok_lines:
            self.errors.append(f"{self.name}: 取り込めません {result.summary()}")

    def item(self, no: str):
        return self.store.item(LINE, no)

    def close(self) -> None:
        self.store.close()


class SharedShapesTest(unittest.TestCase):
    """共有DBの形 × 操作の総当たり。"""

    report: list[dict[str, Any]] = []

    @classmethod
    def tearDownClass(cls) -> None:
        path = os.environ.get("KANBAN_SHAPES_REPORT")
        if path:
            Path(path).write_text(json.dumps(cls.report, ensure_ascii=False, indent=1), encoding="utf-8")

    # ------------------------------------------------------------------
    def make_shared(self, root: Path, key_decl: str, key_of, other_decl: str, empty, date_form: str) -> Path:
        path = root / "看板マスタ.sqlite3"
        conn = sqlite3.connect(str(path))
        decls = []
        for c in COLUMNS:
            decl = key_decl if c == config.COL_KEY else other_decl
            decls.append(f"[{c}] {decl}".rstrip())
        conn.execute(f"CREATE TABLE [{TABLE}] ({', '.join(decls)})")
        rows = []
        for n, (material, size, permanent) in enumerate((
                ("中性紙", "1600 × 150M:4本", ON), ("中性紙", "1600 × 150M:2本", ON),
                ("HB", "11000 × 150M:4本", "×"), ("中性紙", "1300 × 100M:4本", ON)), start=1):
            rows.append((key_of(n), material, size, empty, ON if n != 4 else empty, date_form if n == 4 else empty,
                         empty, empty, permanent, empty, empty))
        conn.executemany(f"INSERT INTO [{TABLE}] VALUES ({', '.join('?' for _ in COLUMNS)})", rows)
        conn.commit()
        conn.close()
        return path

    def shared_rows(self, path: Path) -> list[dict[str, Any]]:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) | {"_t": r["t"]} for r in conn.execute(
                f"SELECT *, typeof([{config.COL_KEY}]) AS t FROM [{TABLE}] ORDER BY rowid")]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    def run_shape(self, label: str, key_decl: str, key_of, other_decl: str, empty, date_form: str) -> dict:
        root = Path(tempfile.mkdtemp(prefix="kanban_shape_"))
        steps: list[dict[str, Any]] = []
        site = ware = None
        try:
            shared = self.make_shared(root, key_decl, key_of, other_decl, empty, date_form)
            before = self.shared_rows(shared)
            site = Terminal(root, "現場PC", shared)
            ware = Terminal(root, "倉庫PC", shared)
            # 起動したときにアプリがすること(看板コメントの表を用意する。書き戻しの見張りが呼ぶ)
            sync.ensure_comment_table(site.gateway)
            site.take()
            ware.take()
            nos = sorted({i.mgmt_no for i in site.store.items(LINE)}, key=lambda x: (len(x), x))
            no1, no2 = nos[0], nos[1]

            def check(step: str, actor: Terminal, other: Terminal, nos_to_check=(no1, no2)) -> None:
                actor.send()
                other.take()
                actor.take()
                problems: list[str] = []
                rows = {_norm(r[config.COL_KEY]): r for r in self.shared_rows(shared)}
                from kanban import keys
                for no in nos_to_check:
                    mine, theirs = actor.item(no), other.item(no)
                    row = next((r for k, r in rows.items() if keys.same(k, no)), None)
                    if row is None or mine is None or theirs is None:
                        problems.append(f"{no}: 行が見つからない")
                        continue
                    for field, col in STATE:
                        a, s, b = getattr(mine, field), row.get(col), getattr(theirs, field)
                        if not (_same(a, s) and _same(s, b)):
                            problems.append(f"{no}.{col}: 押した端末={_norm(a)!r} 共有DB={_norm(s)!r} もう1台={_norm(b)!r}")
                for t in (actor, other):
                    if t.errors:
                        problems += [f"{t.name}: {e}" for e in t.errors]
                        t.errors.clear()
                    if t.store.pending_count():
                        problems.append(f"{t.name}: 未反映 {t.store.pending_count()}")
                after = self.shared_rows(shared)
                if len(after) != len(before):
                    problems.append(f"共有DBの行数 {len(before)} → {len(after)}")
                for b, a in zip(before, after):
                    if b[config.COL_KEY] != a[config.COL_KEY] or b["_t"] != a["_t"]:
                        problems.append(f"管理番号の書き方が変わった {b[config.COL_KEY]!r}({b['_t']}) → "
                                        f"{a[config.COL_KEY]!r}({a['_t']})")
                steps.append({"step": step, "ok": not problems, "problems": problems})

            # 1. 発注 → 発注解除(現場から届いた件そのもの)
            site.svc.toggle_order(LINE, no1, expected_rev=site.item(no1).rev)
            check("現場: 発注", site, ware)
            self.assertEqual(ware.item(no1).want, ON) if steps[-1]["ok"] else None
            site.svc.toggle_order(LINE, no1, expected_rev=site.item(no1).rev)
            check("現場: 発注解除", site, ware)
            # 2. 発注 → 倉庫が発送 → 現場が届いた確認
            site.svc.toggle_order(LINE, no1, expected_rev=site.item(no1).rev)
            check("現場: 発注(2回目)", site, ware)
            ware.svc.toggle_ship(LINE, no1, expected_rev=ware.item(no1).rev)
            check("倉庫: 発送", ware, site)
            ware.svc.toggle_ship(LINE, no1, expected_rev=ware.item(no1).rev)
            check("倉庫: 発送解除", ware, site)
            ware.svc.toggle_ship(LINE, no1, expected_rev=ware.item(no1).rev)
            check("倉庫: 発送(もう一度)", ware, site)
            site.svc.toggle_order(LINE, no1, expected_rev=site.item(no1).rev, treat_as_delivered=True)
            check("現場: 届いた(赤と緑を戻す)", site, ware)
            # 3. 注文中
            site.svc.toggle_order(LINE, no2, expected_rev=site.item(no2).rev)
            check("現場: 発注(2枚目)", site, ware)
            ware.svc.toggle_hold(LINE, no2, expected_rev=ware.item(no2).rev)
            check("倉庫: 注文中", ware, site)
            ware.svc.toggle_hold(LINE, no2, expected_rev=ware.item(no2).rev)
            check("倉庫: 注文中解除", ware, site)
            # 4. 一括発送 → 一括確認
            site.svc.toggle_order(LINE, no1, expected_rev=site.item(no1).rev)
            check("現場: 発注(1枚目をもう一度)", site, ware)
            ware.svc.batch_ship(LINE, {i.mgmt_no: i.rev for i in ware.store.items(LINE)})
            check("倉庫: 表示中のラインを全て発送済みに", ware, site)
            site.svc.batch_reset(LINE, {i.mgmt_no: i.rev for i in site.store.items(LINE)})
            check("現場: 届いた資材を一括確認", site, ware)
            # 5. 発送の途中で現場が取り消した(倉庫の発送が届く前に赤を消す)
            site.svc.toggle_order(LINE, no1, expected_rev=site.item(no1).rev)
            check("現場: 発注(取り消しの準備)", site, ware)
            ware.svc.toggle_ship(LINE, no1, expected_rev=ware.item(no1).rev)
            ware.send()
            site.svc.toggle_order(LINE, no1, expected_rev=site.item(no1).rev)   # 倉庫の発送を取り込む前に
            check("現場: 発送が届く前に発注解除", site, ware)
            item = site.item(no1)
            if item.is_cancelled_while_shipping if hasattr(item, "is_cancelled_while_shipping") else (
                    not item.is_ordered and item.is_shipped):
                site.svc.acknowledge_cancelled_shipping(LINE, no1, expected_rev=item.rev)
                check("現場: 取り消された発送を確かめた", site, ware)
            # 6. コメント(現場 → 倉庫 → 現場)
            site.store.add_comment(LINE, no2, "現場", "在庫ありますか")
            site.send()
            ware.take()
            got = _bodies(ware.store.comment_thread(LINE, no2))
            ware.store.add_comment(LINE, no2, "倉庫", "明日入ります")
            ware.send()
            site.take()
            back = _bodies(site.store.comment_thread(LINE, no2))
            ok = "在庫ありますか" in got and "明日入ります" in back
            steps.append({"step": "コメント 現場 → 倉庫 → 現場", "ok": ok,
                          "problems": [] if ok else [f"倉庫が見た {got} / 現場が見た {back}"]})
            # 7. 帯(届いていません)が残らない
            band = [t.name for t in (site, ware) if sync.Exporter(t.store, t.gateway, interval_sec=0) and (
                t.store.pending_count() or t.store.unsent_comment_count())]
            steps.append({"step": "帯「まだ共有に届いていません」が出ない", "ok": not band,
                          "problems": [f"{n} に残り" for n in band]})
        except Exception as exc:  # noqa: BLE001 - 落ちたことも結果に残す
            import traceback
            steps.append({"step": "(試験が途中で落ちた)", "ok": False,
                          "problems": [f"{type(exc).__name__}: {exc}", traceback.format_exc()[-600:]]})
        finally:
            for t in (site, ware):
                if t is not None:
                    t.close()
            shutil.rmtree(root, ignore_errors=True)
        return {"shape": label, "steps": steps, "ok": all(s["ok"] for s in steps)}

    # ------------------------------------------------------------------
    def test_all_shapes(self) -> None:
        started = time.time()
        failures = []
        for key_decl in KEY_DECLS:
            for value_label, key_of in KEY_VALUES:
                for other_label, other_decl in OTHER_DECLS:
                    for empty_label, empty in EMPTIES:
                        for date_label, date_form in DATE_FORMS[:1] if empty is None else DATE_FORMS[1:]:
                            label = (f"管理番号 {key_decl or '(型なし)'} / {value_label} / ほかの列 {other_label}"
                                     f" / 空欄 {empty_label} / 更新日 {date_label}")
                            result = self.run_shape(label, key_decl, key_of, other_decl, empty, date_form)
                            result.update(key_decl=key_decl or "(型なし)", key_value=value_label,
                                          other=other_label, empty=empty_label, date=date_label)
                            self.report.append(result)
                            if not result["ok"]:
                                failures.append(result)
        self.__class__.elapsed = round(time.time() - started, 1)
        if failures:
            lines = []
            for f in failures[:8]:
                bad = [s for s in f["steps"] if not s["ok"]][:2]
                lines.append(f"{f['shape']}: " + " | ".join(f"{s['step']}: {s['problems'][:2]}" for s in bad))
            self.fail(f"{len(failures)} / {len(self.report)} の形で食い違い:\n" + "\n".join(lines))

    def test_two_rows_with_the_same_number_are_not_guessed(self) -> None:
        """同じ番号が 2 行(数の 1 と文字の '１')。**どちらにも書かず**要確認にする(推測しない)。"""
        root = Path(tempfile.mkdtemp(prefix="kanban_shape_dup_"))
        site = None
        try:
            shared = self.make_shared(root, "", lambda n: n, "", None, "2026-10-16 08:32:00")
            conn = sqlite3.connect(str(shared))
            conn.execute(f"INSERT INTO [{TABLE}] ([管理番号], [資材], [サイズ], [欲], [不]) VALUES ('１', '中性紙', 'X', NULL, '〇')")
            conn.commit()
            conn.close()
            site = Terminal(root, "現場PC", shared)
            site.take()
            target = next(i for i in site.store.items(LINE) if i.mgmt_no == "1")
            site.svc.toggle_order(LINE, target.mgmt_no, expected_rev=target.rev)
            site.send()
            rows = self.shared_rows(shared)
            self.assertTrue(all(_norm(r[config.COL_WANT]) == "" for r in rows), "推測してどちらかに書いた")
            self.assertTrue(any("と一致しません" in e for e in site.errors), site.errors)
        finally:
            if site is not None:
                site.close()
            shutil.rmtree(root, ignore_errors=True)


class OtherWritePathsTest(SharedShapesTest.__base__):
    """書き戻し以外で共有DBへ書く道を、**型の無い**共有DB(本番の形)で通す。

    マスタ管理(足す・直す・消す)・開いている端末(Form状態管理)・Access の中身で入れ替える。
    どれも、そのあと現場と倉庫の 2 台で看板を回して食い違わないことまで見る。
    """

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="kanban_paths_"))
        self.shared = SharedShapesTest.make_shared(self, self.root, "", lambda n: n, "", None,
                                                   "2026-10-16 08:32:00")
        self.terminals: list[Terminal] = []

    def tearDown(self) -> None:
        for t in self.terminals:
            t.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def terminal(self, name: str) -> Terminal:
        t = Terminal(self.root, name, self.shared)
        self.terminals.append(t)
        t.take()
        return t

    def rows(self, table: str = TABLE) -> list[dict[str, Any]]:
        conn = sqlite3.connect(str(self.shared))
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(
                f"SELECT *, rowid AS rid, typeof([{config.COL_KEY}]) AS t FROM [{table}] ORDER BY rowid")]
        finally:
            conn.close()

    def round_trip(self, no: str) -> None:
        """現場が発注 → 倉庫が発送 → 現場が届いた。3 か所(現場・共有DB・倉庫)が毎回そろう。"""
        site, ware = self.terminal("現場PC"), self.terminal("倉庫PC")
        site.svc.toggle_order(LINE, no, expected_rev=site.item(no).rev)
        site.send()
        ware.take()
        self.assertEqual(ware.item(no).want, ON, site.errors)
        ware.svc.toggle_ship(LINE, no, expected_rev=ware.item(no).rev)
        ware.send()
        site.take()
        self.assertEqual(site.item(no).shipped, ON, ware.errors)
        site.svc.toggle_order(LINE, no, expected_rev=site.item(no).rev, treat_as_delivered=True)
        site.send()
        ware.take()
        self.assertEqual((ware.item(no).want, ware.item(no).shipped), ("", ""), site.errors)
        self.assertEqual(site.errors + ware.errors, [])
        self.assertEqual(site.store.pending_count() + ware.store.pending_count(), 0)

    # -- マスタ管理 -----------------------------------------------------
    def test_master_add_then_use_the_new_kanban(self):
        from kanban.presenters import master

        gw = SharedDb(str(self.shared), cache_dir=str(self.root / "cache_master"))
        dup = master.insert_row(gw, TABLE, {config.COL_KEY: "1", config.COL_MATERIAL: "中性紙",
                                            config.COL_SIZE: "X", config.COL_PERMANENT: ON})
        self.assertFalse(dup.ok, "数の 1 がある表に文字の '1' を足せた(同じ看板が 2 行)")
        self.assertEqual(dup.reason, "duplicate_key")
        added = master.insert_row(gw, TABLE, {config.COL_KEY: "5", config.COL_MATERIAL: "中性紙",
                                              config.COL_SIZE: "1000 × 100M:4本", config.COL_PERMANENT: ON})
        self.assertTrue(added.ok, added.message)
        self.assertEqual(len(self.rows()), 5)
        self.round_trip("5")

    def test_master_edit_and_delete_hit_the_right_row(self):
        from kanban.presenters import master

        gw = SharedDb(str(self.shared), cache_dir=str(self.root / "cache_master"))
        target = next(r for r in self.rows() if r[config.COL_KEY] == 2)
        res = master.update_cell(gw, TABLE, target["rid"], config.COL_SIZE, "1600 × 150M:3本", expect_key="2")
        self.assertTrue(res.ok, res.message)
        after = {r[config.COL_KEY]: r for r in self.rows()}
        self.assertEqual(after[2][config.COL_SIZE], "1600 × 150M:3本")
        self.assertEqual(after[2]["t"], "integer", "直したら管理番号の書き方が変わった")
        self.assertEqual(after[1][config.COL_SIZE], "1600 × 150M:4本", "別の行を直した")
        self.round_trip("2")
        gw.forget_copy()
        gone = next(r for r in self.rows() if r[config.COL_KEY] == 3)
        res = master.delete_row(gw, TABLE, gone["rid"], expect_key="3")
        self.assertTrue(res.ok, res.message)
        self.assertEqual(sorted(r[config.COL_KEY] for r in self.rows()), [1, 2, 4])

    # -- 開いている端末 ---------------------------------------------------
    def test_presence_on_an_untyped_state_table(self):
        from kanban import presence

        conn = sqlite3.connect(str(self.shared))
        conn.execute(f"CREATE TABLE [{config.TABLE_STATE}] ([{config.COL_KEY}], [{config.COL_STATE_LINE}],"
                     f" [{config.COL_STATE_VALUE}], [{config.COL_STATE_UPDATED_AT}], [{config.COL_STATE_HOST}])")
        conn.execute(f"INSERT INTO [{config.TABLE_STATE}] VALUES (1, ?, NULL, NULL, NULL)", (LINE,))
        conn.commit()
        conn.close()
        site = self.terminal("現場PC")
        beat = presence.Heartbeat(site.gateway, site.store, LINE, host="現場PC")
        for _ in range(3):
            self.assertTrue(beat.beat())
        rows = self.rows(config.TABLE_STATE)
        self.assertEqual(len(rows), 1, "同じラインの行が増えた")
        self.assertEqual(rows[0][config.COL_STATE_VALUE], config.STATE_OPEN)
        self.assertEqual(rows[0][config.COL_STATE_HOST], "現場PC")

    # -- Access の中身で入れ替える ------------------------------------------
    def test_refresh_from_access_keeps_orders_and_key_type(self):
        from kanban import table_refresh as tr

        site, ware = self.terminal("現場PC"), self.terminal("倉庫PC")
        site.svc.toggle_order(LINE, "1", expected_rev=site.item("1").rev)
        site.send()
        # Access を変換したファイル(型なし・管理番号は Double で読める)
        src = self.root / "看板マスタ_最新.sqlite3"
        conn = sqlite3.connect(str(src))
        conn.execute(f"CREATE TABLE [{TABLE}] ({', '.join(f'[{c}]' for c in COLUMNS)})")
        for n, size in ((1, "1600 × 150M:5本"), (2, "1600 × 150M:2本"), (3, "11000 × 150M:4本"),
                        (4, "1300 × 100M:4本")):
            conn.execute(f"INSERT INTO [{TABLE}] VALUES ({', '.join('?' for _ in COLUMNS)})",
                         (float(n), "中性紙", size, None, ON, None, None, None, ON, None, None))
        conn.commit()
        conn.close()
        tr._cache.clear()
        for _ in range(2):
            result = tr.refresh(str(src), SharedDb(str(self.shared), cache_dir=str(self.root / "cache_tr")),
                                [TABLE], self.root / "backup")
            self.assertTrue(result.ok, result.message)
        rows = self.rows()
        self.assertEqual(len(rows), 4, "入れ替えで行が増えた")
        one = next(r for r in rows if r[config.COL_KEY] == 1)
        self.assertEqual(one[config.COL_SIZE], "1600 × 150M:5本", "Access の中身が入っていない")
        self.assertEqual(one[config.COL_WANT], ON, "入れ替えで発注中を消した")
        self.assertEqual(one["t"], "integer", "入れ替えで管理番号の書き方が変わった")
        ware.take()
        self.assertEqual(ware.item("1").want, ON)
        ware.svc.toggle_ship(LINE, "1", expected_rev=ware.item("1").rev)
        ware.send()
        site.take()
        self.assertEqual(site.item("1").shipped, ON, ware.errors)
        self.assertEqual(ware.errors + site.errors, [])


class ThreeTerminalsOutageTest(unittest.TestCase):
    """現場 2 台(同じライン)+ 倉庫 1 台、共有DBは本番の形(型なし・管理番号は数)。

    アプリと同じ書き戻し(:class:`sync.Exporter`)と取り込み(:class:`sync.Importer`)で回す。
    途中で共有フォルダが落ち(ファイルが見えない)、その間に押した分が戻ったあとに行き渡るか。
    """

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="kanban_three_"))
        self.shared = SharedShapesTest.make_shared(self, self.root, "", lambda n: n, "", None,
                                                   "2026-10-16 08:32:00")
        self.t = {name: Terminal(self.root, name, self.shared) for name in ("現場PC1", "現場PC2", "倉庫PC")}
        sync.ensure_comment_table(self.t["倉庫PC"].gateway)
        self.ex = {n: sync.Exporter(t.store, t.gateway, interval_sec=0) for n, t in self.t.items()}
        self.im = {n: sync.Importer(t.store, t.gateway, [LINE], interval_sec=0) for n, t in self.t.items()}
        self.cycle()

    def tearDown(self) -> None:
        for t in self.t.values():
            t.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def cycle(self) -> None:
        """全端末が 1 回ずつ送って、1 回ずつ取り込む(アプリの周期 1 回ぶん)。"""
        for name, ex in self.ex.items():
            result = ex.run_once()
            self.t[name].errors += result.errors if result else []
        for name, im in self.im.items():
            self.t[name].gateway.forget_copy()
            im.run_once()

    def states(self, no: str) -> dict[str, tuple]:
        out = {n: (t.item(no).want, t.item(no).shipped, t.item(no).hold) for n, t in self.t.items()}
        from kanban import keys
        conn = sqlite3.connect(str(self.shared))
        try:
            for k, w, sh, h in conn.execute(f"SELECT 管理番号, 欲, 発送, 保留 FROM [{TABLE}]"):
                if keys.same(k, no):
                    out["共有DB"] = (_norm(w), _norm(sh), _norm(h))
        finally:
            conn.close()
        return out

    def assert_all(self, no: str, want: tuple) -> None:
        got = self.states(no)
        self.assertTrue(all(v == want for v in got.values()), f"{no}: {got}")

    def press(self, name: str, action: str, no: str, **kw) -> None:
        t = self.t[name]
        getattr(t.svc, action)(LINE, no, expected_rev=t.item(no).rev, **kw)

    def test_normal_day(self):
        self.press("現場PC1", "toggle_order", "1")
        self.press("現場PC2", "toggle_order", "2")
        self.cycle()
        self.assert_all("1", (ON, "", ""))
        self.assert_all("2", (ON, "", ""))
        self.press("倉庫PC", "toggle_hold", "2")
        self.press("倉庫PC", "toggle_ship", "1")
        self.cycle()
        self.assert_all("1", (ON, ON, ""))
        self.assert_all("2", (ON, "", ON))
        self.press("現場PC2", "toggle_order", "1", treat_as_delivered=True)   # 別の現場PCが届いた確認
        self.cycle()
        self.assert_all("1", ("", "", ""))
        self.assertEqual([e for t in self.t.values() for e in t.errors], [])

    def test_shared_folder_goes_away_and_comes_back(self):
        hidden = self.shared.with_suffix(".away")
        self.shared.rename(hidden)                     # 共有フォルダが落ちた
        self.press("現場PC1", "toggle_order", "1")
        self.press("現場PC2", "toggle_order", "3")     # もう 1 台は 3 番(非常設品)
        self.cycle()
        self.assertEqual(self.t["現場PC1"].store.pending_count(), 1, "落ちている間の操作を手元に預かっていない")
        why = self.t["現場PC1"].store.get_meta(sync.Exporter.META_UNDELIVERED_WHY, "")
        self.assertIn("見えません", why, "帯の理由が違う")
        hidden.rename(self.shared)                     # 戻った
        self.cycle()
        self.cycle()
        self.assert_all("1", (ON, "", ""))
        self.assert_all("3", (ON, "", ""))
        self.press("倉庫PC", "toggle_hold", "3")
        self.press("倉庫PC", "toggle_ship", "1")
        self.cycle()
        self.assert_all("1", (ON, ON, ""))
        self.assert_all("3", (ON, "", ON))
        for name, t in self.t.items():
            self.assertEqual(t.store.pending_count(), 0, name)
            self.assertEqual(t.store.get_meta(sync.Exporter.META_UNDELIVERED_SINCE, ""), "", f"{name} の帯が消えない")

    def test_two_terminals_touch_the_same_kanban_before_syncing(self):
        """現場が発注を消すのと倉庫の発送が行き違う。どちらの操作も消えない(取り消された発送の確認で戻る)。"""
        self.press("現場PC1", "toggle_order", "1")
        self.cycle()
        self.press("倉庫PC", "toggle_ship", "1")
        self.press("現場PC1", "toggle_order", "1")     # 倉庫の発送を取り込む前に
        self.cycle()
        self.cycle()
        self.assert_all("1", ("", ON, ""))             # 赤なし・緑あり(取り消された発送)
        self.press("現場PC2", "acknowledge_cancelled_shipping", "1")
        self.cycle()
        self.assert_all("1", ("", "", ""))
        self.assertEqual([e for t in self.t.values() for e in t.errors], [])


if __name__ == "__main__":
    unittest.main()
