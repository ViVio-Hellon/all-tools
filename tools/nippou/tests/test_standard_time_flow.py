"""標準作業時間 ── 一連の流れを通しで確かめる

    一連の流れ、サブ機能 テストをお願いします

`tests/test_standard_time.py` が部品ごとの約束を縛るのに対し、ここは
**画面が叩く道を順番に通します**:

    作業者を名簿から選ぶ → LOT を打って行が埋まる → 保存 →
    共有へ保存(本物の sqlite3 の書き先へ) → 標準作業時間.sqlite3 に溜まる →
    標準の表 → 抽出(同条件の作業) → 計算式 → CSV →
    直して送り直す(置き換え) → 共有に書けない(待って次で写す) →
    班の決まり方(混成・不明) → 過去ぶんを入れる → ほかの端末と同時に書く

マスタは本物と同じ形(SIKALOT / SIKAHIKI / SIKAODR、梱包資材マスタの
班員名簿)で一時フォルダに置きます。**共有への保存は差し替えません**
(本物の書き込み)。差し替えるのは関門(7項目のチェック)だけで、これは
いまの時刻(直の終わりまで打ってあるか)に左右されるためです ── 関門を
通すところは、本物のブラウザで画面を操作して別に確かめました。
"""
from __future__ import annotations

import csv
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.connection import connect  # noqa: E402
from nippou.db.models import DetailRecord, HeaderRecord  # noqa: E402
from nippou.db.repository import NippouRepository  # noqa: E402
from nippou.db.schema import ensure_schema  # noqa: E402
from nippou.logic import standard_time as logic  # noqa: E402
from nippou.services import standard_time as svc  # noqa: E402
from nippou.services import summary  # noqa: E402
from tests import _gw_master  # noqa: E402
from tests._web import WebTestCase  # noqa: E402
from tests.test_web_entry_lot import HIKI_COLUMNS, LOT_COLUMNS, ODR_COLUMNS  # noqa: E402

#: LOT → (受注, 板厚, 板幅, 板丈, 用途コード, 用途名, 検入枚数)
LOTS = {
    "N7131T0": ("OD1", 8.0, 1528.0, 3053.0, "H176", "ｼﾔ-ｼ", "40"),
    "N7132T0": ("OD1", 8.0, 1528.0, 3053.0, "H176", "ｼﾔ-ｼ", "40"),
    "N7200T0": ("OD2", 6.0, 1250.0, 2500.0, "H162", "ｶﾞｲｿｳ", "20"),
}
#: 受注 → 包装仕様NO
ORDERS = {"OD1": "1P0001", "OD2": "7P0106"}
#: 班員名簿(名前, 班, 読み)
STAFF = (("山田", "A", "ヤマダ"), ("鈴木", "A", "スズキ"), ("佐藤", "B", "サトウ"),
         ("高橋", "B", "タカハシ"), ("田中", "C", "タナカ"))


def write_masters(ref: Path) -> None:
    """参照マスタを本物と同じ形で置く(テーブル名「仕掛」、列名は半角カナ)。"""
    ref.mkdir(parents=True, exist_ok=True)

    def table(name, columns, rows, tname="仕掛"):
        conn = sqlite3.connect(str(ref / name))
        with conn:
            conn.execute(f'CREATE TABLE "{tname}" ('
                         + ", ".join(f'"{c}"' for c in columns) + ")")
            conn.executemany(f'INSERT INTO "{tname}" VALUES ('
                             + ", ".join("?" for _ in columns) + ")", rows)
        conn.close()

    table("SIKALOT.sqlite3", LOT_COLUMNS, [
        (lot, od, "K1", "52S", "R", t, w, ln, pc, pn, "", "取引先", "納入先", "送り先",
         ken, "0", "C1") for lot, (od, t, w, ln, pc, pn, ken) in LOTS.items()])
    table("SIKAHIKI.sqlite3", HIKI_COLUMNS, [
        (lot, od, 100, "", f"H{i}") for i, (lot, (od, *_)) in enumerate(LOTS.items())])
    table("SIKAODR.sqlite3", ODR_COLUMNS, [
        (od, "V1", "", "0", 100.0, "取引先", "納入先", "送り先", spec, "")
        for od, spec in ORDERS.items()])
    _gw_master.write(ref)
    conn = sqlite3.connect(str(ref / _gw_master.FILE_NAME))
    with conn:
        conn.execute('CREATE TABLE "班員名簿" ("管理番号", "苗字", "班", "名前", "読み", "担当ライン")')
        conn.executemany('INSERT INTO "班員名簿" VALUES (?, ?, ?, ?, ?, ?)', [
            (str(i), name, team, name, reading, "L-1")
            for i, (name, team, reading) in enumerate(STAFF, start=1)])
    conn.close()


def read_csv(path: Path) -> list[list[str]]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.reader(f))


class FlowTests(WebTestCase):
    """画面が叩く道を順番に。**共有への保存は本物。**"""

    #: 行ごとの打ち込み(LOT を打ったあとに入れるもの)
    ROWS = {
        "1": dict(LOT="N7131T0", KZ="07", KH="00", SZ="10", SH="00", HIT="2", MAI="10", TUT="4"),
        "2": dict(LOT="N7132T0", KZ="10", KH="00", SZ="12", SH="30", HIT="2", MAI="10", TUT="4",
                  S="0", TH="60"),
        "3": dict(LOT="N7200T0", KZ="12", KH="30", SZ="15", SH="00", HIT="1", MAI="10", TUT="2"),
    }

    def setUp(self) -> None:
        super().setUp()
        from nippou import config, user_settings
        self.share = self.tmp / "共有"
        self.share.mkdir()
        self.out = self.tmp / "出力"
        write_masters(self.tmp / "ref")
        user_settings.save_many({config.KEY_ACCESS_DIR: str(self.share),
                                 config.KEY_REPORT_OUT_DIR: str(self.out)})
        # 関門(7項目のチェック)だけ差し替える。**いまの時刻に左右される**ので
        gate = patch("nippou.services.shift_check.run_pending", return_value=[])
        gate.start()
        self.addCleanup(gate.stop)
        from nippou.config import SETTINGS
        self.db = SETTINGS.standard_time_db_path

    # -- 画面が叩く道 ------------------------------------------------
    def typed_rows(self, **changes) -> dict:
        """LOT を打って埋まった行に、手で打つ欄を重ねる(画面と同じ順)。"""
        rows: dict = {}
        for n, typed in self.ROWS.items():
            body = self.post("/api/entry/lot", {"rows": {n: {"LOT": typed["LOT"]}},
                                                "row": int(n)}).get_json()
            self.assertEqual(body["lot"]["state"], "filled", body)
            rows[n] = {**body["rows"][n], **typed, **changes.get(n, {})}
        return rows

    def save(self, worker="鈴木 山田", **changes) -> dict:
        body = self.post("/api/entry/save", {"rows": self.typed_rows(**changes),
                                             "header": {"worker": worker},
                                             "checks": {}}).get_json()
        self.assertTrue(body["saved"], body.get("message"))
        return body

    def push(self) -> dict:
        res = self.post("/api/settings/push", {"worker": "鈴木 山田"})
        self.assertEqual(res.status_code, 200, res.get_json())
        return res.get_json()

    def q(self, sql: str, *params) -> list[tuple]:
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def today(self) -> str:
        return self.repo().list_keys()[0][0]

    # -- 一連の流れ -------------------------------------------------------
    def test_打って保存して共有へ送ると溜まり_標準と同条件の作業に出る(self) -> None:
        # 1. 作業者は名簿の班ごとに出る
        teams = {t["team"]: t["names"] for t in self.get("/api/staff/members").get_json()["teams"]}
        self.assertEqual(teams["A"], ["鈴木", "山田"])            # 読み順

        # 2. LOT を打つと 用途コード・包装仕様NO・サイズが埋まる
        rows = self.typed_rows()
        self.assertEqual((rows["1"]["others1"], rows["1"]["others4"], rows["1"]["SIZ"]),
                         ("H176", "1P0001", "8.000×1528.0×3053.0"))

        # 3. 保存 ── 作業時間は停止を引いて、実績枚数は 個装枚数×梱包数 で出る
        saved = self.save()
        self.assertEqual([saved["rows"][n]["TIM"] for n in "123"], ["180", "90", "150"])
        self.assertEqual([saved["rows"][n]["CON"] for n in "123"], ["40", "40", "20"])
        self.assertFalse(self.db.exists(), "保存(確定)だけでは溜めない")

        # 4. 共有へ保存 ── 日報と一緒に、標準作業時間.sqlite3 に溜まる
        body = self.push()
        self.assertEqual(body["succeeded"], 1)
        self.assertEqual(body["standard_time"]["shifts"], 1, body)
        self.assertIn("標準作業時間: 1直ぶん(3行)を蓄積し", body["message"])
        self.assertTrue(self.db.exists())
        got = self.q('SELECT 行番号, 班, 作業者, 用途コード, 包装仕様NO, サイズ, 梱包数, 枚数,'
                     ' 作業人数, "作業時間(分)", 梱包あたり人分, 枚あたり人分 FROM "T_作業実績"'
                     ' ORDER BY 行番号')
        self.assertEqual(got, [
            (1, "A", "鈴木 山田", "H176", "1P0001", "8×1528×3053", 4.0, 40.0, 2.0, 180.0, 90.0, 9.0),
            (2, "A", "鈴木 山田", "H176", "1P0001", "8×1528×3053", 4.0, 40.0, 2.0, 90.0, 45.0, 4.5),
            (3, "A", "鈴木 山田", "H162", "7P0106", "6×1250×2500", 2.0, 20.0, 1.0, 150.0, 75.0, 7.5)])

        # 5. 標準の表 ── 条件×班(A と 全班)
        std = self.post("/api/standard-time/standards",
                        {"scope": "line", "formulas": True}).get_json()
        rows = [dict(zip(std["table"]["columns"], r)) for r in std["table"]["rows"]]
        a176 = next(r for r in rows if r["班"] == "A" and r["用途コード"] == "H176")
        self.assertEqual((a176["件数"], a176["標準(人分/梱包)"]), ("2", "67.5"))   # (90+45)/2
        self.assertEqual(a176["仮"], "仮")
        self.assertEqual(a176["式: 標準(人分/梱包)"],
                         "2件を小さい順に並べた1件目と2件目の平均 = (45.0 + 90.0) ÷ 2"
                         " = 67.5 人分/梱包")

        # 6. 抽出 ── 標準の行の条件そのまま(AND)で、作業を1件ずつ
        cond = std["conditions"][std["table"]["rows"].index(
            next(r for r in std["table"]["rows"] if r[1] == "A" and r[2] == "H176"))]
        works = self.post("/api/standard-time/works", {**cond, "join": "and",
                                                       "formulas": True}).get_json()
        wrows = [dict(zip(works["table"]["columns"], r)) for r in works["table"]["rows"]]
        self.assertEqual([r["作業時間(分)"] for r in wrows], ["180", "90"])
        # 標準 67.5 × 4梱包 ÷ 2人 = 135分 → 差 +45 / -45
        self.assertEqual([r["標準作業時間(分)"] for r in wrows], ["135.0", "135.0"])
        self.assertEqual([r["標準との差(分・+は標準より長い)"] for r in wrows], ["+45.0", "-45.0"])
        self.assertEqual(wrows[0]["式: 標準作業時間"], "標準 67.5人分/梱包 × 4梱包 ÷ 2人 = 135.0分")
        self.assertEqual(wrows[1]["式: 標準との差"], "90分 − 135.0分 = -45.0分")
        self.assertEqual(dict(works["summary"])["作業時間 合計"], "270分")

        # 7. CSV ── どちらも画面の表そのまま
        path = Path(self.post("/api/standard-time/standards/csv",
                              {"scope": "line", "formulas": True}).get_json()["file"])
        self.assertEqual(read_csv(path)[0], std["table"]["columns"])
        self.assertEqual(len(read_csv(path)) - 1, len(std["table"]["rows"]))
        path = Path(self.post("/api/standard-time/works/csv",
                              {**cond, "formulas": True}).get_json()["file"])
        data = read_csv(path)
        self.assertEqual(data[0], works["table"]["columns"])
        self.assertEqual(data[1:], works["table"]["rows"])
        self.assertTrue(str(path).startswith(str(self.out)))

    def test_CSVの出力先は画面から指定でき_空なら集計CSVと同じ先(self) -> None:
        from nippou import config
        key = config.KEY_STANDARD_TIME_OUT_DIR
        self.save()
        self.push()
        # 決めていなければ集計CSVと同じ先。画面はそれを言い添える
        page = self.get("/standard-time").get_data(as_text=True)
        self.assertIn('id="std-out-dir"', page)
        self.assertIn("集計CSVと同じ先", page)
        path = Path(self.post("/api/standard-time/standards/csv",
                              {"scope": "line"}).get_json()["file"])
        self.assertEqual(path.parent, self.out)

        # 変えるには管理者パスワード(ほかの出力パスと同じ関門)
        std_out = self.tmp / "標準の出力"
        res = self.post("/api/settings/paths", {key: str(std_out)})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.get_json()["changing"], ["標準作業時間CSVの出力パス"])
        res = self.post("/api/settings/paths", {key: str(std_out), "password": "nisk"})
        self.assertEqual(res.status_code, 200, res.get_json())
        view = next(v for v in res.get_json()["paths"] if v["key"] == key)
        self.assertEqual(view["resolved"], str(std_out))

        # 標準の表も同条件の作業も、指定した先へ。集計CSVの先は変わらない
        path = Path(self.post("/api/standard-time/standards/csv",
                              {"scope": "line"}).get_json()["file"])
        self.assertEqual(path.parent, std_out)
        self.assertTrue(path.is_file())
        path = Path(self.post("/api/standard-time/works/csv",
                              {"purpose_code": "H176", "join": "and"}).get_json()["file"])
        self.assertEqual(path.parent, std_out)
        from nippou.config import SETTINGS
        self.assertEqual(SETTINGS.report_output_dir, self.out)
        page = self.get("/standard-time").get_data(as_text=True)
        self.assertIn(str(std_out), page)

        # 空に戻せば、また集計CSVと同じ先
        self.post("/api/settings/paths", {key: "", "password": "nisk"})
        path = Path(self.post("/api/standard-time/standards/csv",
                              {"scope": "line"}).get_json()["file"])
        self.assertEqual(path.parent, self.out)

    def test_直して送り直すと置き換わる(self) -> None:
        self.save()
        self.push()
        self.save(**{"1": {"HIT": "3"}})
        body = self.push()
        self.assertIn("標準作業時間: 1直ぶん(3行)", body["message"])
        self.assertEqual(self.q('SELECT COUNT(*) FROM "T_作業実績"'), [(3,)])
        self.assertEqual(self.q('SELECT 作業人数, 梱包あたり人分 FROM "T_作業実績" WHERE 行番号=1'),
                         [(3.0, 180 * 3 / 4)])
        # 行を減らして送り直すと、消した行は残らない
        rows = self.typed_rows()
        rows["3"] = {k: "" for k in rows["3"]}
        self.post("/api/entry/save", {"rows": rows, "header": {"worker": "鈴木 山田"},
                                      "checks": {}})
        self.push()
        self.assertEqual(self.q('SELECT 行番号 FROM "T_作業実績" ORDER BY 行番号'), [(1,), (2,)])

    def test_共有に書けないと待って_次の共有へ保存で写す(self) -> None:
        self.db.mkdir()                               # 同じ名前のフォルダ → 開けない
        self.save()
        body = self.push()
        self.assertEqual(body["succeeded"], 1, "日報の共有への保存そのものは成功")
        self.assertIn("標準作業時間に写せませんでした", body["message"])
        self.assertEqual(body["standard_time"]["pending"], 1)
        self.assertEqual(len(self.repo().standard_time_pending()), 1)

        self.db.rmdir()
        body = self.push()                            # 送る日報は無くても、待ちは写す
        self.assertIn("送るものはありませんでした", body["message"])
        self.assertIn("標準作業時間: 1直ぶん(3行)", body["message"])
        self.assertEqual(self.repo().standard_time_pending(), [])
        self.assertEqual(self.q('SELECT COUNT(*) FROM "T_作業実績"'), [(3,)])

    def test_共有のフォルダが無ければファイルを作らない(self) -> None:
        self.share.rename(self.tmp / "外した共有")
        self.save()
        body = self.post("/api/settings/push", {}).get_json()
        self.assertEqual(body["succeeded"], 0, "共有に届かないので日報も送れていない")
        self.assertFalse(self.db.exists())
        self.assertFalse(self.db.parent.exists(), "共有のフォルダを勝手に作らない")
        # 送れていない直は待ちにも入らない(送れたときに入る)
        self.assertEqual(self.repo().standard_time_pending(), [])

    def test_班は名簿で決まる_同数なら混成_名簿に無ければ不明(self) -> None:
        cases = (("鈴木 山田", "A"), ("山田 佐藤", logic.TEAM_MIXED),
                 ("田中 高橋 佐藤", "B"), ("応援の人", logic.TEAM_UNKNOWN))
        for worker, team in cases:
            with self.subTest(worker=worker):
                self.save(worker=worker)
                self.push()
                self.assertEqual(self.q('SELECT DISTINCT 班, 作業者 FROM "T_作業実績"'),
                                 [(team, worker)])
                teams = {r[0] for r in self.q('SELECT 班 FROM "T_標準作業時間"')}
                self.assertEqual(teams, {team, logic.TEAM_ALL})

    def test_過去ぶんを入れる_何度押しても二重にならない(self) -> None:
        repo = self.repo()
        for day, shift, worker in (("2026年9月1日", "1直", "山田"), ("2026年9月1日", "2直", "佐藤"),
                                   ("2026年8月31日", "1直", "山田")):
            key = dict(report_date=day, line="L-1", shift=shift, page=1)
            repo.save(HeaderRecord(**key, worker=worker), [DetailRecord(
                **key, row_no=1, lot="N7131T0", siz="8.000×1528.0×3053.0", hit="2", tut="4",
                con="40", tim="120", others1="H176", others4="1P0001")])
            repo.mark_synced((day, "L-1", shift, 1))
            summary.refresh_shift(repo, day, "L-1", shift)
        for _ in range(2):
            body = self.post("/api/standard-time/backfill",
                             {"start": "2026-09-01", "end": "2026-09-30"}).get_json()
            self.assertEqual(body["shifts"], 2, body)
        self.assertEqual(sorted(self.q('SELECT 報告日, 直, 班 FROM "T_作業実績"')),
                         [("2026年9月1日", "1直", "A"), ("2026年9月1日", "2直", "B")])
        page = self.get("/standard-time").get_data(as_text=True)
        self.assertIn('id="backfill-run"', page)


class SharedWriteTests(unittest.TestCase):
    """**全ライン・全端末が同じファイルへ書く。** 同時に書いても壊れない。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.db = self.tmp / "共有" / "標準作業時間.sqlite3"
        self.db.parent.mkdir()
        patcher = patch.object(svc, "load_teams",
                               return_value={name: team for name, team, _ in STAFF})
        patcher.start()
        self.addCleanup(patcher.stop)

    def open(self, line: str) -> NippouRepository:
        """その端末の手元のDBを開く。**開いたスレッドの中で使う**(sqlite3 の決まり)。"""
        conn = connect(self.tmp / f"{line}.sqlite3")
        ensure_schema(conn)
        self.addCleanup(conn.close)
        return NippouRepository(conn)

    def terminal(self, line: str, days: int) -> NippouRepository:
        """1ラインぶんの端末(手元のDB)。`days` 日ぶんの直を保存して待ちに入れる。"""
        repo = self.open(line)
        for d in range(1, days + 1):
            day = f"2026年9月{d}日"
            key = dict(report_date=day, line=line, shift="1直", page=1)
            repo.save(HeaderRecord(**key, worker="山田"), [DetailRecord(
                **key, row_no=n, lot="N7131T0", siz="8×1528×3053", hit="2", tut="4",
                con="40", tim=str(100 + n), others1="H176", others4="1P0001")
                for n in range(1, 4)])
            summary.refresh_shift(repo, day, line, "1直")
            repo.queue_standard_time([(day, line, "1直")])
        return repo

    def test_2つの端末が同時に写しても両方入る(self) -> None:
        lines = ("L-1", "L2")
        for line in lines:
            self.terminal(line, days=6)
        results: dict = {}
        start = threading.Barrier(len(lines))

        def run(line: str) -> None:
            conn = connect(self.tmp / f"{line}.sqlite3")      # 端末ごとに別の手元DB
            try:
                start.wait()                                  # 同時に書き始める
                results[line] = svc.flush(NippouRepository(conn), self.db)
            finally:
                conn.close()

        threads = [threading.Thread(target=run, args=(line,)) for line in lines]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        for line, out in results.items():
            with self.subTest(line=line):
                self.assertEqual((out.error, out.shifts, out.rows, out.pending), ("", 6, 18, 0))
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute(
                'SELECT ライン, COUNT(*) FROM "T_作業実績" GROUP BY ライン ORDER BY ライン')
                .fetchall(), [("L-1", 18), ("L2", 18)])
            self.assertEqual(conn.execute(
                'SELECT ライン, 班, 件数 FROM "T_標準作業時間" ORDER BY ライン, 班').fetchall(),
                [("L-1", "A", 18), ("L-1", "全班", 18), ("L2", "A", 18), ("L2", "全班", 18)])
        finally:
            conn.close()
        # ラインをまたいで読む(全ライン)・ラインで絞る
        both = svc.works(self.db, logic.Criteria.parse("H176"), line="")
        self.assertEqual({s.line for s in both.samples}, {"L-1", "L2"})
        self.assertEqual(len(svc.works(self.db, logic.Criteria.parse("H176"), line="L2").samples), 18)
        self.assertEqual({st.line for st in svc.read(self.db).standards}, {"L-1", "L2"})

    def test_片方のラインを送り直しても_もう片方には触らない(self) -> None:
        l1, l2 = self.terminal("L-1", days=1), self.terminal("L2", days=1)
        svc.flush(l1, self.db)
        svc.flush(l2, self.db)
        l1.queue_standard_time([("2026年9月1日", "L-1", "1直")])
        svc.flush(l1, self.db)
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute(
                'SELECT ライン, COUNT(*) FROM "T_作業実績" GROUP BY ライン').fetchall(),
                [("L-1", 3), ("L2", 3)])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
