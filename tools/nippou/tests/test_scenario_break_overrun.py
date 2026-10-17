"""通しの試験 ── すべての停止・3直の連続入力・大きく超過したりしなかったり・休憩あり/なし

【利用者から】

    日報入力すべての停止、3直連続での入力
    超過したりしなかったりの連続入力
    超過した場合に休憩なしあり、休憩なしでテキスト入力で突破できるかどうか
    テストしてください

画面が叩く道(開く → 作業者を選ぶ → LOT を打つ → 保存(確定) → 共有へ保存)を、止めた時計で
3日 × 1直・2直・3直 = 9直つづけて通します。共有への保存も標準作業時間への蓄積も本物です。

【停止のマスタ】本物と同じ形(記号は 管理ロス 0〜6・ヨ / 突発・待ち イロハニホヘト / ハンドリング A〜G)。
名前は例です。**記号 22 個を全部**どこかの直・どこかの欄(①〜⑤)で使います。

【9直の中身】(作業はすべて同じ条件 H176 / 1P0001 / 8×1528×3053、A班 2人、4梱包)

    直           大きく超過   休憩                    共有へ保存
    10/5 1直     しない       あり(時刻あり)          そのまま通る
    10/5 2直     しない       あり(時刻あり)          そのまま通る
    10/5 3直     しない       あり(時刻あり・0時またぎ) そのまま通る
    10/6 1直     する         あり(超過した行の中)    そのまま通る
    10/6 2直     しない       あり(時刻あり)          そのまま通る
    10/6 3直     する         あり(時刻を書かない)    そのまま通る(空は止めない)
    10/7 1直     する         なし                    断られる → 「設備移動のため保存」で通る
    10/7 2直     しない       あり(時刻あり)          そのまま通る
    10/7 3直     する         あり(分と時刻が合わない) 保存(確定)で断られる。一文でも共有へ通らない → 直すと通る
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import break_time  # noqa: E402
from tests import test_standard_time_flow as flow  # noqa: E402
from tests._web import HAS_FLASK, SKIP_REASON  # noqa: E402
from tests.test_scenario_shift_flow import SKIP, ScenarioCase  # noqa: E402

#: 停止のマスタ(本物と同じ3つの表。名前は例)
STOP_MASTER = {
    1: [("休憩食事", "0"), ("TPM活動・清掃", "1"), ("人員不足", "2"), ("朝礼・ミーティング", "3"),
        ("品質確認", "4"), ("応援", "5"), ("教育", "6"), ("その他（理由を記載）", "ヨ")],
    2: [("突発停止(機械)", "イ"), ("ﾌｫｰｸ待ち", "ロ"), ("材料待ち", "ハ"), ("クレーン待ち", "ニ"),
        ("検査待ち", "ホ"), ("指示待ち", "ヘ"), ("停電", "ト")],
    3: [("段取り変更", "A"), ("内径カット", "B"), ("刃替え", "C"), ("梱包材準備", "D"),
        ("ラベル貼り", "E"), ("寸法確認", "F"), ("片付け", "G")],
}
ALL_CODES = [code for rows in STOP_MASTER.values() for _label, code in rows]
OTHER_CODES = [c for c in ALL_CODES if c != "0"]          # 休憩でない停止 21 個

STOP_SLOTS = (("S", "TH"), ("SS", "THS"), ("STH", "THT"), ("S4", "TH4"), ("S5", "TH5"))
SHIFTS = {"1直": ("07:00", 480), "2直": ("15:00", 470), "3直": ("22:50", 490)}
WORKER = "鈴木 山田"                                    # 名簿で A 班
LOTS = {f"N8{n:03d}T0": ("OD1", 8.0, 1528.0, 3053.0, "H176", "ｼﾔ-ｼ", "1000") for n in range(1, 120)}

NORMAL = 60          # ふつうの行の作業時間(分) → 標準 30 人分/梱包 → 4梱包2人で 60 分
OVER = 150           # 大きく超過する行(60 分の 2.5 倍)


def write_stop_master(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    with conn:
        for no, rows in STOP_MASTER.items():
            conn.execute(f'CREATE TABLE "作業停止時間内訳_{no}" '
                         '("管理番号" INTEGER, "内訳" TEXT, "内訳番号" TEXT, "備考" TEXT)')
            conn.executemany(f'INSERT INTO "作業停止時間内訳_{no}" VALUES (?,?,?,?)',
                             [(i, label, code, "") for i, (label, code) in enumerate(rows, 1)])
    conn.close()


class Plan:
    """1直ぶんの行の組み立て。行の長さ = 作業時間 + 停止(+ 休憩 60 分)。直の長さぴったりに並べる。"""

    def __init__(self, shift: str, codes: list[str]):
        self.shift = shift
        self.start, self.length = SHIFTS[shift]
        self.codes = codes                        # この直で使う休憩でない停止(先頭から配る)
        self.rows: list[dict] = []

    def take(self, n: int) -> list[str]:
        out, self.codes = self.codes[:n], self.codes[n:]
        return out

    def add(self, tim: int, n_stops: int = 0, *, rest: str = "", stop_minutes: int = 5) -> None:
        """``rest`` は "time"(休憩60分・時刻を書く) / "blank"(休憩60分・時刻を書かない) /
        "wrong"(休憩60分・時刻は30分ぶん) / ""(休憩なし)。"""
        self.rows.append({"tim": tim, "stops": [(c, stop_minutes) for c in self.take(n_stops)],
                          "rest": rest})

    def used(self) -> int:
        return sum(r["tim"] + 60 * bool(r["rest"]) + sum(m for _c, m in r["stops"]) for r in self.rows)

    def fill(self) -> None:
        """残りを、大きく超過しない行(停止1つずつ)で直の終わりまで埋める。最後の行は 85 分まで。"""
        rest = self.length - self.used()
        while rest > 90:
            self.add(NORMAL, 1)
            rest -= NORMAL + 5
        self.add(rest - 5, 1)

    def build(self, lots: list[str]) -> dict:
        """画面へ渡す行(LOT・時刻・停止・休憩の時刻)。"""
        t = datetime(2026, 1, 1, int(self.start[:2]), int(self.start[3:]))
        out = {}
        for n, spec in enumerate(self.rows, start=1):
            rest = 60 if spec["rest"] else 0
            length = spec["tim"] + rest + sum(m for _c, m in spec["stops"])
            end = t + timedelta(minutes=length)
            values = {"LOT": lots.pop(0), "KZ": t.strftime("%H"), "KH": t.strftime("%M"),
                      "SZ": end.strftime("%H"), "SH": end.strftime("%M"),
                      "HIT": "2", "MAI": "10", "TUT": "4"}
            stops = list(spec["stops"])
            if rest:
                stops.insert(0, ("0", 60))
                b0 = t + timedelta(minutes=10)
                b1 = b0 + timedelta(minutes=30 if spec["rest"] == "wrong" else 60)
                if spec["rest"] != "blank":
                    values["break_range"] = f"{b0:%H:%M}〜{b1:%H:%M}"
            assert len(stops) <= 5, stops
            for (code_f, min_f), (code, minutes) in zip(STOP_SLOTS, stops):
                values[code_f] = code
                values[min_f] = str(minutes)
            if any(c == "ヨ" for c, _m in stops):
                values["reason"] = "通しの試験のその他"
            out[str(n)] = values
            t = end
        total = (t - datetime(2026, 1, 1, int(self.start[:2]), int(self.start[3:]))).seconds // 60
        assert total == self.length, (self.shift, total, self.length)
        return out


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class BreakOverrunScenarioTests(ScenarioCase):

    def setUp(self) -> None:
        super().setUp()
        with patch.object(flow, "LOTS", LOTS):
            flow.write_masters(self.tmp / "ref")
        write_stop_master(self.tmp / "ref" / "伝送用ファイル.sqlite3")
        self.lots = list(LOTS)
        self.codes = list(OTHER_CODES) * 6       # 配りきらないだけ

    # -- 打つ ---------------------------------------------------------------
    def typed(self, rows: dict) -> dict:
        """LOT を打って埋まった行に、手で打つ欄を重ねる(画面と同じ順)。"""
        out = {}
        for n, values in rows.items():
            body = self.post("/api/entry/lot", self.sheet({n: {"LOT": values["LOT"]}}, WORKER,
                                                          row=int(n))).get_json()
            self.assertEqual(body["lot"]["state"], "filled", body)
            out[n] = {**body["rows"][n], **values}
        return out

    def plan(self, shift: str) -> Plan:
        return Plan(shift, self.codes)

    def run_shift(self, day: str, shift: str, plan: Plan, *, first: bool = False) -> dict:
        """直の始まりに開いて作業者を選び、直の終わり前に保存(確定)する。行を返す。"""
        start, length = SHIFTS[shift]
        begin = datetime.strptime(f"{day} {start}", "%Y-%m-%d %H:%M")
        when = (begin + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M")
        body = self.start_shift(when, WORKER) if first else self.next_shift(when, WORKER)
        self.assertEqual(body["shift"], shift, body.get("message"))
        self.codes = plan.codes                  # 配ったぶんを減らす
        rows = self.typed(plan.build(self.lots))
        self.at((begin + timedelta(minutes=length - 3)).strftime("%Y-%m-%d %H:%M"))
        return rows

    def push_end(self, day: str, shift: str, **extra):
        start, length = SHIFTS[shift]
        begin = datetime.strptime(f"{day} {start}", "%Y-%m-%d %H:%M")
        self.at((begin + timedelta(minutes=length - 2)).strftime("%Y-%m-%d %H:%M"))
        return self.push_now(worker=WORKER, **extra)

    def codes_of(self, res) -> list[str]:
        body = res.get_json()
        found = [f["code"] for f in body.get("check_findings", [])]
        for report in body.get("reports", []):
            found += [f["code"] for f in report.get("findings", [])]
        return found

    # -- 標準作業時間 -------------------------------------------------------
    def samples(self) -> list[dict]:
        from nippou.config import SETTINGS
        conn = sqlite3.connect(SETTINGS.standard_time_db_path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute('SELECT * FROM "T_作業実績" ORDER BY 報告日, 直, 行番号')]
        finally:
            conn.close()

    # ======================================================================
    def test_9直つづけて_すべての停止_大きく超過したりしなかったり(self) -> None:
        normal_day = [("1直", "2026-10-05"), ("2直", "2026-10-05"), ("3直", "2026-10-05")]
        typed_codes: set[str] = set()
        slots_used: set[str] = set()
        break_rows: dict[tuple, str] = {}

        def note(rows: dict, day: str, shift: str) -> None:
            for n, values in rows.items():
                for code_f, _m in STOP_SLOTS:
                    if values.get(code_f):
                        typed_codes.add(values[code_f])
                        slots_used.add(code_f)
                if values.get("break_range"):
                    break_rows[(day, shift, int(n))] = values["break_range"]

        def ok_shift(day: str, shift: str, plan: Plan, first: bool = False) -> None:
            rows = self.run_shift(day, shift, plan, first=first)
            res = self.save(rows, WORKER)
            self.assertEqual(res.status_code, 200, res.get_json().get("message"))
            res = self.push_end(day, shift)
            self.assertEqual(res.status_code, 200, (day, shift, res.get_json()))
            note(rows, day, shift)

        # ---- 10/5: 3直とも大きく超過しない。1行は ①〜⑤ ぜんぶ使う ---------------
        for i, (shift, day) in enumerate(normal_day):
            p = self.plan(shift)
            p.add(NORMAL, 1)
            p.add(NORMAL, 4, rest="time")             # 休憩 + 4つ = ①〜⑤
            p.add(NORMAL, 2)
            p.add(NORMAL, 3)
            p.fill()
            ok_shift(day, shift, p, first=(i == 0))
        self.assertGreaterEqual(len(self.samples()), 15)

        # ---- 10/6 1直: 大きく超過(休憩はその行の中・時刻あり) ----------------------
        p = self.plan("1直")
        p.add(NORMAL, 2)
        p.add(OVER, 2, rest="time")                    # 大きく超過する行
        p.add(NORMAL, 2)
        p.fill()
        ok_shift("2026-10-06", "1直", p)

        # ---- 10/6 2直: しない ---------------------------------------------------
        p = self.plan("2直")
        for _ in range(3):
            p.add(NORMAL, 1)
        p.add(NORMAL, 1, rest="time")
        p.fill()
        ok_shift("2026-10-06", "2直", p)

        # ---- 10/6 3直: 大きく超過(休憩はあるが時刻を書かない ── 空は止めない) ------
        p = self.plan("3直")
        p.add(NORMAL, 1)
        p.add(OVER, 1, rest="blank")
        p.fill()
        ok_shift("2026-10-06", "3直", p)

        # ---- 10/7 1直: 大きく超過・休憩なし ── 断られる → 「設備移動のため保存」で通る ----
        p = self.plan("1直")
        p.add(NORMAL, 1)
        p.add(OVER, 2)                                 # 休憩なし
        p.add(NORMAL, 1)
        p.fill()
        rows = self.run_shift("2026-10-07", "1直", p)
        self.assertEqual(self.save(rows, WORKER).status_code, 200, "休憩なしは保存(確定)では止めない")
        refused = self.push_end("2026-10-07", "1直")
        self.assertEqual(refused.status_code, 422)
        self.assertIn("short_break", self.codes_of(refused))
        self.assertNotIn(("2026年10月7日", "1直", 1), self.shared_keys())
        passed = self.push_end("2026-10-07", "1直", skip=SKIP)
        self.assertEqual(passed.status_code, 200, passed.get_json())
        self.assertIn(("2026年10月7日", "1直", 1), self.shared_keys())
        note(rows, "2026-10-07", "1直")

        # ---- 10/7 2直: しない ---------------------------------------------------
        p = self.plan("2直")
        p.add(NORMAL, 1, rest="time")
        p.fill()
        ok_shift("2026-10-07", "2直", p)

        # ---- 10/7 3直: 大きく超過・休憩の時刻が合わない ── 一文でも通らない → 直すと通る --
        p = self.plan("3直")
        p.add(NORMAL, 1)
        p.add(OVER, 1, rest="wrong")                   # 時刻は 30 分、休憩は 60 分
        p.fill()
        rows = self.run_shift("2026-10-07", "3直", p)
        refused = self.save(rows, WORKER)
        self.assertEqual(refused.status_code, 422)
        self.assertEqual(self.codes_of(refused), ["break_time"])
        self.assertIn("時刻は 30 分、作業停止の休憩は 60 分です", refused.get_json()["message"])
        # 打ちかけ(自動保存)は止めないので手元には残る。それでも共有へは一文を添えても通らない
        self.assertEqual(self.autosave(rows, WORKER).status_code, 200)
        for extra in ({}, {"skip": SKIP}):
            res = self.push_end("2026-10-07", "3直", **extra)
            self.assertEqual(res.status_code, 422, extra)
            self.assertIn("break_time", self.codes_of(res), extra)
        self.assertNotIn(("2026年10月7日", "3直", 1), self.shared_keys())
        # 時刻を直す(30 分 → 60 分)と通る
        fixed = dict(rows)
        start = fixed["2"]["break_range"].split("〜")[0]
        b0 = datetime.strptime(start, "%H:%M")
        fixed["2"] = dict(fixed["2"], break_range=f"{b0:%H:%M}〜{b0 + timedelta(minutes=60):%H:%M}")
        self.assertEqual(self.save(fixed, WORKER).status_code, 200)
        res = self.push_end("2026-10-07", "3直")
        self.assertEqual(res.status_code, 200, res.get_json())
        note(fixed, "2026-10-07", "3直")

        # ================= 確かめる =================
        # 1) すべての停止の記号を、①〜⑤ ぜんぶの欄で打った
        self.assertEqual(set(ALL_CODES) - typed_codes, set(), "打っていない記号")
        self.assertEqual(slots_used, {c for c, _m in STOP_SLOTS})

        # 2) 9直とも共有へ出た(3直は始めた日の報告日)
        days = ("2026年10月5日", "2026年10月6日", "2026年10月7日")
        self.assertEqual(self.shared_keys(), {(d, s, 1) for d in days for s in ("1直", "2直", "3直")})

        # 2b) 共有の明細にも、22 個の記号が ①〜⑤ の欄(S/SS/STH/S4/S5)で届いた
        shared_codes = {str(r[c]) for r in self.shared_details() for c, _m in STOP_SLOTS if r.get(c)}
        self.assertEqual(shared_codes, set(ALL_CODES))

        # 3) 手元の停止(集計の明細)に、打った記号と分がそのまま入っている
        repo = self.repo()
        stored: dict[str, float] = {}
        for d in days:
            for s in ("1直", "2直", "3直"):
                report = repo.load_packing_report(d, "L-1", s)
                self.assertIsNotNone(report, (d, s))
                for detail in repo.packing_details(report.id):
                    for stop in detail.stops:
                        stored[stop.stop_code] = stored.get(stop.stop_code, 0) + stop.stop_minutes
        self.assertEqual(set(stored), set(ALL_CODES))
        self.assertEqual(stored["0"], 60 * 8, "休憩は10/7 1直を除く8直で60分ずつ")

        # 4) 停止内訳CSV: 休憩の1件ずつの行に時刻(書いた行だけ)
        from nippou.reporting import csv_export
        csv_breaks = {}
        csv_codes = set()
        for d in days:
            for r in csv_export.build_stop_rows(repo, d, "L-1"):
                if r["区分"] == csv_export.STOP_KIND_ONE:
                    csv_codes.add(r["記号"])
                    if r["記号"] == "0" and r.get("休憩の時刻"):
                        csv_breaks[(r["報告日"] if "報告日" in r else d, r["直"])] = r["休憩の時刻"]
        self.assertEqual(csv_codes, set(ALL_CODES))
        expected = {(k[0], k[1]) for k in break_rows}
        self.assertEqual(len(csv_breaks), len(expected), (csv_breaks, break_rows))

        # 5) 標準作業時間: 全部の行が実績に入り、大きく超過は 4件(超過させた4直に1件ずつ)
        from nippou.presenters import standard_time as view
        from nippou.config import SETTINGS
        samples = self.samples()
        self.assertTrue(all(s["標準に数える"] == 1 for s in samples),
                        [s["数えない理由"] for s in samples if s["標準に数える"] != 1])
        works = view.build_works(SETTINGS.standard_time_db_path,
                                 view.WorkFilters.of({"purpose_code": "H176", "start": "2026-10-05",
                                                      "end": "2026-10-07"}, "L-1"))
        cols = works.table.columns
        over = [dict(zip(cols, r)) for r in works.table.rows if r[cols.index("大きく超過")]]
        self.assertEqual(sorted((o["報告日"], o["直"]) for o in over),
                         [("2026年10月6日", "1直"), ("2026年10月6日", "3直"),
                          ("2026年10月7日", "1直"), ("2026年10月7日", "3直")])
        by_shift = {(o["報告日"], o["直"]): o for o in over}
        self.assertEqual(by_shift[("2026年10月6日", "1直")]["作業時間(分)"], str(OVER))
        self.assertTrue(by_shift[("2026年10月6日", "1直")]["休憩の時刻"])
        self.assertIn("休憩の時刻が空", by_shift[("2026年10月6日", "3直")]["大きく超過"])
        self.assertEqual(by_shift[("2026年10月7日", "1直")]["休憩の時刻"], "", "休憩なし")
        self.assertNotIn("休憩の時刻が空", by_shift[("2026年10月7日", "1直")]["大きく超過"])
        self.assertEqual(by_shift[("2026年10月7日", "3直")]["休憩の時刻"],
                         break_time.normalize(break_rows[("2026-10-07", "3直", 2)]),
                         "直した時刻で入っている")
        std = view.build(SETTINGS.standard_time_db_path, view.Filters.of({}, "L-1"))
        at = std.table.columns.index("大きく超過(件)")
        team_a = next(r for r in std.table.rows if r[1] == "A")
        self.assertEqual(team_a[at], "4")
        self.assertEqual(works.summary[-1] if works.summary[-1][0] == "大きく超過" else
                         next(s for s in works.summary if s[0] == "大きく超過"), ["大きく超過", "4件"])
        # しなかった行は印なし(ふつうの行はすべて標準の 1.5 倍に届かない)
        normal_rows = [dict(zip(cols, r)) for r in works.table.rows if not r[cols.index("大きく超過")]]
        self.assertTrue(normal_rows)
        self.assertTrue(all(float(r["作業時間(分)"]) < OVER for r in normal_rows))


if __name__ == "__main__":
    unittest.main()
