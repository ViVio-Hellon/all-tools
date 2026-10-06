"""標準作業時間 ── 同じ条件・同じ班で「ふつうのペース」の作業時間 (v3.87.0)

    日報入力データで 用途コードと包装仕様NOとワークサイズと梱包数と
    作業人数と作業時間を使って標準作業時間を取りたい
    同サイズ 同用途 同包装仕様の時 / 各班ごとに集計する
    標準作業時間.sqlite3を作成しデータ蓄積していきたい

    標準作業時間は別タブ管理とし csv出力 同条件作業抽出 作業時間を表示 (v3.88.0)

    計算(純ロジック)            … `logic/standard_time.py`
    いつ・どこへ書くか          … `services/standard_time.py`
    画面(レールの「標準作業時間」)… `presenters/standard_time.py`
                                  / `app/routes/standard_time.py`

【約束】
    ・1行の指標は 人・分(作業時間×作業人数)を 梱包数 / 枚数 で割ったもの
    ・標準は**中央値**。件数 3 未満は「仮」
    ・鍵は ライン・班・用途コード・包装仕様NO・サイズ(書き方を揃える)
    ・班は作業者欄を班員名簿で引いた**いちばん多い班**(同数=混成, 無し=不明)
    ・書くのは「共有へ保存」で送れた直のぶん。開けなければ待って次に写す
    ・同じ直を送り直したら**置き換え**(消した行が残らない)
    ・専用ファイルなので無ければ作る。**フォルダが無ければ作らない**
"""
from __future__ import annotations

import re
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.connection import connect  # noqa: E402
from nippou.db.models import DetailRecord, HeaderRecord  # noqa: E402
from nippou.db.repository import NippouRepository  # noqa: E402
from nippou.db.schema import ensure_schema  # noqa: E402
from nippou.logic import standard_time as logic  # noqa: E402
from nippou.presenters import standard_time as view  # noqa: E402
from nippou.services import standard_time as svc  # noqa: E402
from nippou.services import summary  # noqa: E402
from tests._web import WebTestCase  # noqa: E402

TEAMS = {"山田": "A", "鈴木": "A", "佐藤": "B", "高橋": "B", "田中": "C"}
ROOT = Path(__file__).resolve().parent.parent


def sample(**over) -> logic.Sample:
    base = dict(report_date="2026年9月1日", line="L-1", shift="1直", page=1, row_no=1,
                worker="山田", team="A", purpose_code="H176", packing_spec_no="1P0001",
                dimension="8.000×1528.0×3053.0", packages=4, sheets=40, workers=2,
                work_minutes=60)
    base.update(over)
    return logic.sample_of(**base)


# ======================================================================
# 純ロジック
# ======================================================================
class SizeTests(unittest.TestCase):
    def test_書き方が違っても同じサイズ(self) -> None:
        for text in ("8.000×1528.0×3053.0", "8x1528x3053", "8.0 × 1528 × 3053", "8*1528*3053"):
            with self.subTest(text=text):
                self.assertEqual(logic.normalize_size(text), "8×1528×3053")

    def test_コイルはそのまま(self) -> None:
        self.assertEqual(logic.normalize_size("20.000×1528.0×ｺｲﾙ"), "20×1528×ｺｲﾙ")

    def test_小数は残る(self) -> None:
        self.assertEqual(logic.normalize_size("0.500×1000.0×2000.0"), "0.5×1000×2000")

    def test_空(self) -> None:
        self.assertEqual(logic.normalize_size(""), "")
        self.assertEqual(logic.normalize_size(None), "")


class TeamTests(unittest.TestCase):
    def test_いちばん多い班(self) -> None:
        self.assertEqual(logic.team_of("山田 鈴木 佐藤", TEAMS), "A")

    def test_同数なら混成(self) -> None:
        self.assertEqual(logic.team_of("山田 佐藤", TEAMS), logic.TEAM_MIXED)

    def test_名簿に無ければ不明(self) -> None:
        self.assertEqual(logic.team_of("誰か", TEAMS), logic.TEAM_UNKNOWN)
        self.assertEqual(logic.team_of("", TEAMS), logic.TEAM_UNKNOWN)
        self.assertEqual(logic.team_of("山田", {}), logic.TEAM_UNKNOWN)

    def test_注記と区切りの違いを飲む(self) -> None:
        self.assertEqual(logic.team_of("山田(新人教育) 鈴木", TEAMS), "A")
        self.assertEqual(logic.team_of("佐藤・高橋", TEAMS), "B")
        self.assertEqual(logic.team_of("田中,田中", TEAMS), "C")


class SampleTests(unittest.TestCase):
    def test_1行の指標(self) -> None:
        s = sample()
        self.assertEqual(s.person_minutes, 120)
        self.assertEqual(s.per_package, 30)        # 120人分 ÷ 4梱包
        self.assertEqual(s.per_sheet, 3)           # 120人分 ÷ 40枚
        self.assertTrue(s.usable)
        self.assertEqual(s.size, "8×1528×3053")
        self.assertEqual(s.key, ("L-1", "A", "H176", "1P0001", "8×1528×3053"))

    def test_数えない理由は1つ目だけ(self) -> None:
        cases = [
            (dict(purpose_code=""), "用途コードが空"),
            (dict(packing_spec_no=""), "包装仕様NOが空"),
            (dict(dimension=""), "サイズが空"),
            (dict(workers=0), "作業人数が0"),
            (dict(work_minutes=0), "作業時間が0"),
            (dict(packages=0, sheets=0), "梱包数も枚数も0"),
        ]
        for over, reason in cases:
            with self.subTest(reason=reason):
                s = sample(**over)
                self.assertEqual(s.excluded_reason, reason)
                self.assertFalse(s.usable)

    def test_梱包数だけ無ければ枚あたりだけ(self) -> None:
        s = sample(packages=0)
        self.assertIsNone(s.per_package)
        self.assertEqual(s.per_sheet, 3)
        self.assertTrue(s.usable)


class StandardTests(unittest.TestCase):
    def test_標準は中央値で全班の行も付く(self) -> None:
        rows = [sample(work_minutes=m, row_no=i) for i, m in enumerate((60, 70, 200), 1)]
        rows.append(sample(team="B", worker="佐藤", work_minutes=100, shift="2直"))
        found = logic.standards(rows)
        by_team = {st.team: st for st in found}
        self.assertEqual(set(by_team), {logic.TEAM_ALL, "A", "B"})
        a = by_team["A"]
        self.assertEqual(a.count, 3)
        self.assertEqual(a.per_package.median, 35)       # 30, 35, 100 → 35(平均なら 55)
        self.assertEqual(a.per_package.mean, 55)
        self.assertEqual((a.per_package.low, a.per_package.high), (30, 100))
        self.assertFalse(a.provisional)
        self.assertTrue(by_team["B"].provisional)       # 1件
        self.assertEqual(by_team[logic.TEAM_ALL].count, 4)
        self.assertEqual(by_team[logic.TEAM_ALL].per_package.median, 42.5)  # 30,35,50,100

    def test_数えない行は標準に入らない(self) -> None:
        found = logic.standards([sample(), sample(workers=0, row_no=2)])
        self.assertEqual(found[0].count, 1)

    def test_条件が違えば別の行(self) -> None:
        found = logic.standards([sample(), sample(dimension="6×1250×2500", row_no=2),
                                 sample(packing_spec_no="1P0002", row_no=3)])
        self.assertEqual(len([st for st in found if st.team == "A"]), 3)

    def test_並びは全班が先(self) -> None:
        found = logic.standards([sample(), sample(team="B", worker="佐藤", row_no=2)])
        self.assertEqual([st.team for st in found], [logic.TEAM_ALL, "A", "B"])

    def test_見積り(self) -> None:
        st = logic.standards([sample(work_minutes=m, row_no=i) for i, m in enumerate((60, 70, 80), 1)])[1]
        self.assertEqual(st.per_package.median, 35)
        self.assertEqual(st.minutes_for(10, 2), 175)      # 35人分/梱包 × 10梱包 ÷ 2人
        self.assertIsNone(st.minutes_for(10, 0))
        self.assertIsNone(logic.estimate_minutes(None, 10, 2))


# ======================================================================
# 蓄積(共有の 標準作業時間.sqlite3)
# ======================================================================
class ServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        conn = connect(self.tmp / "local.sqlite3")
        ensure_schema(conn)
        self.addCleanup(conn.close)
        self.repo = NippouRepository(conn)
        self.share = self.tmp / "共有"
        self.db = self.share / "標準作業時間.sqlite3"
        patcher = patch.object(svc, "load_teams", return_value=dict(TEAMS))
        patcher.start()
        self.addCleanup(patcher.stop)

    def page(self, day: str, shift: str, worker: str, rows: list[tuple]) -> tuple[str, str, str]:
        """(梱包数, 人数, 作業時間, 枚数) の並びで1ページ保存し、集計を作る。"""
        key = dict(report_date=day, line="L-1", shift=shift, page=1)
        self.repo.save(HeaderRecord(**key, worker=worker), [
            DetailRecord(**key, row_no=i + 1, lot=f"N71{i}T0", siz="8.000×1528.0×3053.0",
                         tut=str(t), hit=str(h), tim=str(m), con=str(c),
                         others1="H176", others2="ｼﾔ-ｼ", others4="1P0001")
            for i, (t, h, m, c) in enumerate(rows)])
        summary.refresh_shift(self.repo, day, "L-1", shift)
        return (day, "L-1", shift)

    def query(self, sql: str, *params) -> list[tuple]:
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def pushed(self, *keys):
        return svc.after_push(self.repo, [(*k, 1) for k in keys], self.db)


class ServiceTests(ServiceTestCase):
    def test_送れた直の実績が入り標準ができる(self) -> None:
        key = self.page("2026年9月1日", "1直", "山田 鈴木", [(4, 2, 60, 40), (4, 2, 70, 40)])
        self.share.mkdir()
        out = self.pushed(key)
        self.assertEqual((out.shifts, out.rows, out.pending, out.error), (1, 2, 0, ""))
        self.assertIn("1直ぶん(2行)", out.message)
        self.assertTrue(self.db.exists())
        rows = self.query('SELECT 班, 作業者, サイズ, 梱包あたり人分, 枚あたり人分, 標準に数える'
                          f' FROM "{svc.SAMPLE_TABLE}" ORDER BY 行番号')
        self.assertEqual(rows, [("A", "山田 鈴木", "8×1528×3053", 30.0, 3.0, 1),
                                ("A", "山田 鈴木", "8×1528×3053", 35.0, 3.5, 1)])
        std = self.query('SELECT 班, 件数, "標準(人分/梱包)", 仮'
                         f' FROM "{svc.STANDARD_TABLE}" ORDER BY 班')
        self.assertEqual(std, [("A", 2, 32.5, 1), (logic.TEAM_ALL, 2, 32.5, 1)])
        self.assertEqual(self.repo.standard_time_pending(), [])

    def test_フォルダが無ければ作らず待つ(self) -> None:
        key = self.page("2026年9月1日", "1直", "山田", [(4, 2, 60, 40)])
        out = self.pushed(key)
        self.assertEqual(out.pending, 1)
        self.assertIn("開けません", out.error)
        self.assertIn("次の「共有へ保存」で写します", out.message)
        self.assertFalse(self.db.exists())
        self.assertEqual(self.repo.standard_time_pending(), [key])
        # 次の保存で、待っていたぶんも一緒に写る
        self.share.mkdir()
        key2 = self.page("2026年9月2日", "1直", "山田", [(2, 1, 40, 20)])
        out = self.pushed(key2)
        self.assertEqual((out.shifts, out.rows, out.pending), (2, 2, 0))
        self.assertEqual(self.repo.standard_time_pending(), [])

    def test_送り直したら置き換え(self) -> None:
        key = self.page("2026年9月1日", "1直", "山田", [(4, 2, 60, 40), (4, 2, 70, 40), (4, 2, 80, 40)])
        self.share.mkdir()
        self.pushed(key)
        self.assertEqual(self.query(f'SELECT COUNT(*) FROM "{svc.SAMPLE_TABLE}"'), [(3,)])
        # 3行目を消して送り直す → 2行に減り、標準も出し直される
        self.page("2026年9月1日", "1直", "山田", [(4, 2, 60, 40), (4, 2, 70, 40)])
        self.pushed(key)
        self.assertEqual(self.query(f'SELECT COUNT(*) FROM "{svc.SAMPLE_TABLE}"'), [(2,)])
        self.assertEqual(self.query(f'SELECT 件数 FROM "{svc.STANDARD_TABLE}" WHERE 班=?', "A"),
                         [(2,)])

    def test_数えない行も実績には残す(self) -> None:
        key = self.page("2026年9月1日", "1直", "山田", [(4, 2, 60, 40), (0, 0, 0, 0)])
        self.share.mkdir()
        self.pushed(key)
        rows = self.query(f'SELECT 標準に数える, 数えない理由 FROM "{svc.SAMPLE_TABLE}" ORDER BY 行番号')
        self.assertEqual(rows, [(1, ""), (0, "作業人数が0")])
        self.assertEqual(self.query(f'SELECT 件数 FROM "{svc.STANDARD_TABLE}" WHERE 班=?', "A"),
                         [(1,)])

    def test_名簿が読めなければ不明で残し_あとで入れ直せる(self) -> None:
        key = self.page("2026年9月1日", "1直", "山田", [(4, 2, 60, 40)])
        self.share.mkdir()
        with patch.object(svc, "load_teams", return_value={}):
            self.pushed(key)
        self.assertEqual(self.query(f'SELECT DISTINCT 班 FROM "{svc.SAMPLE_TABLE}"'),
                         [(logic.TEAM_UNKNOWN,)])
        out = svc.backfill(self.repo, date(2026, 9, 1), date(2026, 9, 30), "L-1", self.db)
        self.assertEqual(out.shifts, 1)
        self.assertEqual(self.query(f'SELECT DISTINCT 班 FROM "{svc.SAMPLE_TABLE}"'), [("A",)])
        self.assertEqual(sorted(r[0] for r in self.query(f'SELECT 班 FROM "{svc.STANDARD_TABLE}"')),
                         ["A", logic.TEAM_ALL])

    def test_過去ぶんは期間とラインで(self) -> None:
        self.page("2026年8月31日", "1直", "山田", [(4, 2, 60, 40)])
        self.page("2026年9月1日", "1直", "山田", [(4, 2, 60, 40)])
        self.share.mkdir()
        out = svc.backfill(self.repo, date(2026, 9, 1), date(2026, 9, 30), "L-1", self.db)
        self.assertEqual(out.shifts, 1)
        self.assertEqual(self.query(f'SELECT 報告日 FROM "{svc.SAMPLE_TABLE}"'), [("2026年9月1日",)])
        self.assertEqual(svc.backfill(self.repo, date(2026, 9, 1), date(2026, 9, 30), "L2",
                                      self.db).shifts, 0)

    def test_列が足りない古い表にも書ける(self) -> None:
        self.share.mkdir()
        conn = sqlite3.connect(self.db)
        conn.execute(f'CREATE TABLE "{svc.SAMPLE_TABLE}" (報告日, ライン, 直, ページ, 行番号,'
                     ' PRIMARY KEY (報告日, ライン, 直, ページ, 行番号))')
        conn.commit()
        conn.close()
        key = self.page("2026年9月1日", "1直", "山田", [(4, 2, 60, 40)])
        out = self.pushed(key)
        self.assertEqual((out.rows, out.error), (1, ""))
        have = {r[1] for r in self.query(f'PRAGMA table_info("{svc.SAMPLE_TABLE}")')}
        self.assertIn("梱包あたり人分", have)

    def test_読む(self) -> None:
        self.assertIn("まだありません", svc.read(self.db).source)
        key = self.page("2026年9月1日", "1直", "山田", [(4, 2, 60, 40), (0, 0, 0, 0)])
        self.share.mkdir()
        self.pushed(key)
        got = svc.read(self.db)
        self.assertEqual((got.samples, got.excluded, got.teams, got.lines), (1, 1, ["A"], ["L-1"]))
        self.assertEqual([st.team for st in got.standards], [logic.TEAM_ALL, "A"])
        self.assertEqual(got.standards[1].per_package.median, 30)


# ======================================================================
# 表(presenter)
# ======================================================================
class ViewTests(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.share.mkdir()
        self.page("2026年9月1日", "1直", "山田", [(4, 2, 60, 40), (4, 2, 70, 40), (4, 2, 80, 40)])
        self.page("2026年9月1日", "2直", "佐藤", [(4, 2, 100, 40)])
        self.pushed(("2026年9月1日", "L-1", "1直"), ("2026年9月1日", "L-1", "2直"))

    def build(self, **payload):
        line = None if payload.pop("all", False) else "L-1"
        return view.build(self.db, view.Filters.of(payload, line))

    def test_表の形(self) -> None:
        got = self.build()
        self.assertEqual(got.table.columns[:7],
                         ["ライン", "班", "用途コード", "用途名", "包装仕様NO", "サイズ(厚×幅×丈)", "件数"])
        self.assertEqual([r[1] for r in got.table.rows], [logic.TEAM_ALL, "A", "B"])
        a = got.table.rows[1]
        self.assertEqual(a[:8], ["L-1", "A", "H176", "ｼﾔ-ｼ", "1P0001", "8×1528×3053", "3", "35.0"])
        self.assertEqual(a[-1], "")                  # 3件なので仮ではない
        self.assertEqual(got.table.rows[2][-1], view.PROVISIONAL_MARK)
        self.assertEqual(got.teams, ["A", "B"])
        self.assertEqual(got.table.note, "L-1 / 全部の班")       # 正規の呼び名(v4.12.5)

    def test_絞り込み(self) -> None:
        self.assertEqual([r[1] for r in self.build(team="B").table.rows], ["B"])
        self.assertEqual(len(self.build(purpose_code="h17").table.rows), 3)
        self.assertEqual(len(self.build(purpose_code="X").table.rows), 0)
        self.assertEqual(len(self.build(size="8x1528x3053").table.rows), 3)   # 書き方を揃えて比べる
        self.assertEqual(len(self.build(packing_spec_no="1P0001").table.rows), 3)
        self.assertEqual(len(self.build(all=True).table.rows), 3)
        self.assertIn("この絞り込みに合う条件はありません", self.build(team="Z").table.empty)

    def test_見積りの列(self) -> None:
        got = self.build(packages="10", workers="2")
        self.assertEqual(got.estimate_label, "10梱包を2人で(分)")
        self.assertEqual(got.table.columns[-2], "10梱包を2人で(分)")
        self.assertEqual(got.table.rows[1][-2], "175.0")     # 35 × 10 ÷ 2
        self.assertTrue(got.table.numeric[-2])
        # 片方だけでは出さない
        self.assertNotIn("(分)", "".join(self.build(packages="10").table.columns))

    def test_ファイルが無いとき(self) -> None:
        got = view.build(self.tmp / "x" / "標準作業時間.sqlite3", view.Filters.of({}, "L-1"))
        self.assertEqual(got.table.rows, [])
        self.assertIn("まだ標準がありません", got.table.empty)
        self.assertIn("まだありません", got.source)


class WorksFixture(ServiceTestCase):
    """同条件の作業の下ごしらえ(3つの直・2つの条件)。"""

    def setUp(self) -> None:
        super().setUp()
        self.share.mkdir()
        self.page("2026年9月1日", "1直", "山田", [(4, 2, 60, 40), (4, 2, 70, 40), (4, 2, 80, 40)])
        self.page("2026年9月2日", "2直", "佐藤", [(4, 2, 100, 40), (0, 0, 0, 0)])
        # 別の条件(用途コード H162・包装仕様NO 7P0106・サイズ 6×1250×2500)
        key = dict(report_date="2026年9月3日", line="L-1", shift="1直", page=1)
        self.repo.save(HeaderRecord(**key, worker="山田"), [
            DetailRecord(**key, row_no=1, lot="N7200T0", siz="6.000×1250.0×2500.0",
                         tut="2", hit="1", tim="30", con="20", others1="H162",
                         others4="7P0106"),
            # 用途コードだけ H176 と同じで、包装仕様NO・サイズが違う
            DetailRecord(**key, row_no=2, lot="N7201T0", siz="6.000×1250.0×2500.0",
                         tut="2", hit="1", tim="50", con="20", others1="H176",
                         others4="7P0106")])
        summary.refresh_shift(self.repo, "2026年9月3日", "L-1", "1直")
        self.pushed(("2026年9月1日", "L-1", "1直"), ("2026年9月2日", "L-1", "2直"),
                    ("2026年9月3日", "L-1", "1直"))

    def crit(self, purpose="H176", spec="1P0001", size="8x1528x3053", join="and"):
        return logic.Criteria.parse(purpose, spec, size, join)

    def works(self, crit=None, **kw):
        return svc.works(self.db, crit or self.crit(), line=kw.pop("line", "L-1"), **kw)

    def filters(self, **payload) -> view.WorkFilters:
        base = dict(purpose_code="H176", packing_spec_no="1P0001", size="8x1528x3053")
        base.update(payload)
        return view.WorkFilters.of(base, "L-1")


class WorksTests(WorksFixture):
    """同条件の作業 ── 1件ずつ、作業時間と標準との差。**AND / OR を選べる。**"""

    # -- AND / OR ---------------------------------------------------------
    def test_ANDは全部の欄が一致(self) -> None:
        got = self.works(self.crit())
        self.assertEqual(len(got.samples), 4)
        self.assertEqual({(s.purpose_code, s.packing_spec_no, s.size) for s in got.samples},
                         {("H176", "1P0001", "8×1528×3053")})

    def test_ANDで欄を空けたら問わない(self) -> None:
        got = self.works(self.crit(spec="", size=""))
        self.assertEqual(len(got.samples), 5)          # H176 の行ぜんぶ
        self.assertEqual({s.purpose_code for s in got.samples}, {"H176"})

    def test_ORはどれかの欄が一致(self) -> None:
        got = self.works(self.crit(purpose="H162", spec="1P0001", size="", join="or"))
        # H162 の1行 + 1P0001 の4行
        self.assertEqual(len(got.samples), 5)
        self.assertEqual({s.purpose_code for s in got.samples}, {"H162", "H176"})
        # AND なら当たらない組み合わせ
        self.assertEqual(self.works(self.crit(purpose="H162", spec="1P0001", size="")).samples,
                         [])

    def test_欄の中はカンマでどれか(self) -> None:
        got = self.works(self.crit(purpose="H176,H162", spec="", size=""))
        self.assertEqual(len(got.samples), 6)
        got = self.works(self.crit(purpose="h176、h162", spec="", size="6x1250x2500"))
        self.assertEqual(len(got.samples), 2)          # 大文字小文字・区切りの違いを飲む

    def test_何も入れなければ断る(self) -> None:
        with self.assertRaises(view.BadRequest) as ctx:
            view.WorkFilters.of({"purpose_code": " ", "join": "or"}, "L-1")
        self.assertIn("どれか", str(ctx.exception))
        self.assertEqual(ctx.exception.field, "purpose_code")

    def test_説明文にANDかORかが出る(self) -> None:
        self.assertEqual(self.crit().describe(),
                         "用途コード H176 かつ 包装仕様NO 1P0001 かつ サイズ 8×1528×3053")
        self.assertEqual(self.crit(purpose="H176,H162", spec="", size="", join="or")
                         .describe(), "用途コード H176・H162 のどれか")
        self.assertEqual(self.crit(size="", join="or").describe(),
                         "用途コード H176 または 包装仕様NO 1P0001")

    # -- 絞り込み(いつも AND) ------------------------------------------
    def test_班を決めればその班だけ_標準も班のもの(self) -> None:
        got = self.works(team="A")
        self.assertEqual([s.work_minutes for s in got.samples], [60, 70, 80])
        self.assertEqual(got.standard_team, "A")
        self.assertEqual(got.standard_for(got.samples[0]).per_package.median, 35)

    def test_全班なら班を問わず_標準は全班(self) -> None:
        for team in ("", logic.TEAM_ALL):
            with self.subTest(team=team):
                got = self.works(team=team)
                self.assertEqual(len(got.samples), 4)
                self.assertEqual(got.standard_for(got.samples[0]).team, logic.TEAM_ALL)

    def test_ラインが空なら全ライン(self) -> None:
        self.assertEqual(len(self.works(line="").samples), 4)
        self.assertEqual(self.works(line="L2").samples, [])

    def test_数えない作業は既定では出さない(self) -> None:
        self.assertEqual(len(self.works().samples), 4)
        got = self.works(include_excluded=True)
        self.assertEqual(len(got.samples), 5)
        self.assertEqual(got.samples[-1].excluded_reason, "作業人数が0")

    def test_期間は報告日で(self) -> None:
        got = self.works(start=date(2026, 9, 2))
        self.assertEqual({s.report_date for s in got.samples}, {"2026年9月2日"})
        got = self.works(end=date(2026, 9, 1))
        self.assertEqual({s.report_date for s in got.samples}, {"2026年9月1日"})

    def test_並びは紙をめくる順(self) -> None:
        got = self.works()
        self.assertEqual([(s.report_date, s.shift, s.row_no) for s in got.samples],
                         [("2026年9月1日", "1直", 1), ("2026年9月1日", "1直", 2),
                          ("2026年9月1日", "1直", 3), ("2026年9月2日", "2直", 1)])

    def test_ファイル無し(self) -> None:
        got = svc.works(self.tmp / "無い" / "x.sqlite3", self.crit())
        self.assertEqual(got.samples, [])
        self.assertIn("まだありません", got.source)

    # -- 表 -------------------------------------------------------------
    def test_表は作業時間と標準との差(self) -> None:
        got = view.build_works(self.db, self.filters(team="A"))
        cols = got.table.columns
        self.assertEqual(cols[:10], ["報告日", "ライン", "直", "班", "作業者", "オペレーター構成",
                                     "LOT", "用途コード", "包装仕様NO", "サイズ"])
        row = dict(zip(cols, got.table.rows[0]))
        self.assertEqual(row["作業時間(分)"], "60")
        # 標準 35人分/梱包 × 4梱包 ÷ 2人 = 70分 → 60 − 70 = −10
        self.assertEqual(row["標準作業時間(分)"], "70.0")
        self.assertEqual(row["標準との差(分・+は標準より長い)"], "-10.0")
        self.assertEqual(dict(got.summary)["件数"], "3件")
        self.assertEqual(dict(got.summary)["作業時間 合計"], "210分")
        self.assertEqual(dict(got.summary)["中央値"], "70分")
        self.assertEqual(dict(got.summary)["標準との差 平均"], "+0.0分")
        self.assertIn("この条件の標準(A): 35.0 人分/梱包", got.standard)
        self.assertEqual(got.table.note, "L-1 / A / 用途コード H176 かつ 包装仕様NO 1P0001"
                                         " かつ サイズ 8×1528×3053")
        self.assertNotIn("式: 標準作業時間", cols)          # 既定では式を出さない

    def test_条件が混ざったら行ごとにその条件の標準と比べる(self) -> None:
        got = view.build_works(self.db, self.filters(purpose_code="H176", packing_spec_no="",
                                                     size=""))
        rows = [dict(zip(got.table.columns, r)) for r in got.table.rows]
        by_spec = {r["包装仕様NO"]: r for r in rows}
        # 7P0106 の H176 は1件だけ → 全班の標準 = 50分×1人÷2梱包 = 25.0 → 25×2÷1 = 50
        self.assertEqual(by_spec["7P0106"]["標準作業時間(分)"], "50.0")
        self.assertEqual(by_spec["7P0106"]["標準との差(分・+は標準より長い)"], "+0.0")
        self.assertIn("条件が 2通り", got.standard)

    def test_梱包数が無ければ枚あたりの標準で(self) -> None:
        st = logic.standards([sample(), sample(row_no=2, work_minutes=80)])[0]
        s = sample(packages=0, sheets=20, workers=2)
        # 枚あたり中央値 (3.0 と 4.0) = 3.5 × 20枚 ÷ 2人 = 35分
        self.assertEqual(st.minutes_for_sample(s), 35)

    def test_数えない作業には差を付けない(self) -> None:
        got = view.build_works(self.db, self.filters(include_excluded=True))
        last = dict(zip(got.table.columns, got.table.rows[-1]))
        self.assertEqual(last["標準作業時間(分)"], "")
        self.assertEqual(last["標準に数えない理由"], "作業人数が0")

    def test_日付の形と向き(self) -> None:
        with self.assertRaises(view.BadRequest) as ctx:
            self.filters(start="9/1")
        self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(view.BadRequest) as ctx:
            self.filters(start="2026-09-30", end="2026-09-01")
        self.assertEqual(ctx.exception.status, 422)
        self.assertEqual(self.filters(start="", end="").start, None)

    def test_標準が無い条件でも作業は出る(self) -> None:
        conn = sqlite3.connect(self.db)
        conn.execute(f'DELETE FROM "{svc.STANDARD_TABLE}"')
        conn.commit()
        conn.close()
        got = view.build_works(self.db, self.filters())
        self.assertEqual(len(got.table.rows), 4)
        self.assertIn("まだありません", got.standard)
        self.assertEqual(dict(zip(got.table.columns, got.table.rows[0]))["標準作業時間(分)"], "")

    def test_標準の表の行に条件が付く(self) -> None:
        got = view.build(self.db, view.Filters.of({}, "L-1"))
        self.assertEqual(len(got.conditions), len(got.table.rows))
        self.assertIn({"line": "L-1", "team": "A", "purpose_code": "H176",
                       "packing_spec_no": "1P0001", "size": "8×1528×3053"}, got.conditions)

    def test_標準の表もANDとOR(self) -> None:
        both = view.build(self.db, view.Filters.of(
            {"purpose_code": "H162", "packing_spec_no": "1P0001"}, "L-1"))
        self.assertEqual(both.table.rows, [])
        either = view.build(self.db, view.Filters.of(
            {"purpose_code": "H162", "packing_spec_no": "1P0001", "join": "or"}, "L-1"))
        self.assertEqual({(r[2], r[4]) for r in either.table.rows},
                         {("H162", "7P0106"), ("H176", "1P0001")})


class FormulaTests(WorksFixture):
    """計算式 ── **式の答えが表の数と同じ**であること。"""

    def test_同条件の作業の式(self) -> None:
        got = view.build_works(self.db, self.filters(team="A", formulas=True))
        cols = got.table.columns
        self.assertEqual(cols[-4:], list(view.WORK_FORMULA_COLUMNS))
        row = dict(zip(cols, got.table.rows[0]))
        self.assertEqual(row["式: 梱包あたり人分"], "60分 × 2人 ÷ 4梱包 = 30.0 人分/梱包")
        self.assertEqual(row["式: 枚あたり人分"], "60分 × 2人 ÷ 40枚 = 3.0 人分/枚")
        self.assertEqual(row["式: 標準作業時間"], "標準 35.0人分/梱包 × 4梱包 ÷ 2人 = 70.0分")
        self.assertEqual(row["式: 標準との差"], "60分 − 70.0分 = -10.0分")
        for r in got.table.rows:
            r = dict(zip(cols, r))
            with self.subTest(row=r["LOT"]):
                self.assertTrue(r["式: 梱包あたり人分"].endswith(f"= {r['梱包あたり人分']} 人分/梱包"))
                self.assertTrue(r["式: 標準作業時間"].endswith(f"= {r['標準作業時間(分)']}分"))
                self.assertTrue(r["式: 標準との差"].endswith(
                    f"= {r['標準との差(分・+は標準より長い)']}分"))

    def test_数えない作業の式は理由を言う(self) -> None:
        got = view.build_works(self.db, self.filters(include_excluded=True, formulas=True))
        last = dict(zip(got.table.columns, got.table.rows[-1]))
        self.assertEqual(last["式: 梱包あたり人分"], "梱包数が0なので出せません")
        self.assertIn("標準に数えない作業です", last["式: 標準作業時間"])
        self.assertEqual(last["式: 標準との差"], "")

    def test_標準の表の式(self) -> None:
        got = view.build(self.db, view.Filters.of(
            {"team": "A", "purpose_code": "H176", "packing_spec_no": "1P0001",
             "formulas": True, "packages": 10, "workers": 2}, "L-1"))
        cols = got.table.columns
        self.assertEqual(cols[-5:], list(view.STANDARD_FORMULA_COLUMNS) + ["式: 見積り"])
        row = dict(zip(cols, got.table.rows[0]))
        self.assertEqual(row["式: 標準(人分/梱包)"],
                         "3件を小さい順に並べた2件目 = 35.0 人分/梱包")
        self.assertEqual(row["式: 平均(人分/梱包)"], "合計 105.0 ÷ 3件 = 35.0 人分/梱包")
        self.assertEqual(row["式: 見積り"], "標準 35.0人分/梱包 × 10梱包 ÷ 2人 = 175.0分")
        self.assertEqual(row["10梱包を2人で(分)"], "175.0")
        # 全班の行(4件): 30, 35, 40, 50 → 2件目と3件目の平均
        whole = view.build(self.db, view.Filters.of(
            {"team": logic.TEAM_ALL, "purpose_code": "H176", "packing_spec_no": "1P0001",
             "formulas": "1"}, "L-1")).table
        all_row = dict(zip(whole.columns, whole.rows[0]))
        self.assertEqual(all_row["式: 標準(人分/梱包)"],
                         "4件を小さい順に並べた2件目と3件目の平均 = (35.0 + 40.0) ÷ 2"
                         " = 37.5 人分/梱包")
        self.assertEqual(all_row["標準(人分/梱包)"], "37.5")

    def test_式は既定では出さない(self) -> None:
        got = view.build(self.db, view.Filters.of({}, "L-1"))
        self.assertFalse(any(c.startswith("式:") for c in got.table.columns))


# ======================================================================
# 画面の道(Flask)
# ======================================================================
class WebTests(WebTestCase):
    def setUp(self) -> None:
        super().setUp()
        from nippou import config, user_settings
        self.share = self.tmp / "共有"
        self.share.mkdir()
        user_settings.save_many({config.KEY_ACCESS_DIR: str(self.share),
                                 config.KEY_REPORT_OUT_DIR: str(self.tmp / "出力")})
        patcher = patch("nippou.services.standard_time.load_teams", return_value=dict(TEAMS))
        patcher.start()
        self.addCleanup(patcher.stop)

    def save_shift(self, day="2026年9月1日", shift="1直"):
        key = dict(report_date=day, line="L-1", shift=shift, page=1)
        self.repo().save(HeaderRecord(**key, worker="山田 鈴木"), [
            DetailRecord(**key, row_no=1, lot="N7131T0", siz="8.000×1528.0×3053.0",
                         tut="4", hit="2", tim="60", con="40", others1="H176", others4="1P0001")])
        summary.refresh_shift(self.repo(), day, "L-1", shift)
        return (day, "L-1", shift, 1)

    def push(self, *keys):
        from nippou.access_bridge import pusher

        def fake_push(repo, _path, *args, **kwargs):
            for key in keys:
                repo.mark_synced(key)
            return pusher.PushSummary(succeeded=list(keys))
        with patch("nippou.services.shift_check.run_pending", return_value=[]), \
                patch("nippou.access_bridge.pusher.push_pending", side_effect=fake_push), \
                patch("app.routes.settings._rollover_after_push", return_value=[]):
            return self.post("/api/settings/push", {})

    def db(self) -> Path:
        return self.share / "標準作業時間.sqlite3"

    def test_共有へ保存で蓄積される(self) -> None:
        key = self.save_shift()
        body = self.push(key).get_json()
        self.assertEqual(body["standard_time"]["shifts"], 1, body)
        self.assertIn("標準作業時間: 1直ぶん(1行)を蓄積し", body["message"])
        self.assertTrue(self.db().exists())
        got = self.post("/api/standard-time/standards", {"scope": "line"}).get_json()
        self.assertEqual([r[1] for r in got["table"]["rows"]], [logic.TEAM_ALL, "A"])
        self.assertEqual(got["table"]["rows"][1][7], "30.0")
        self.assertEqual(got["teams"], ["A"])

    def test_共有が無くても保存は成功のまま(self) -> None:
        self.share.rmdir()
        key = self.save_shift()
        body = self.push(key).get_json()
        self.assertEqual(body["succeeded"], 1)
        self.assertEqual(body["standard_time"]["pending"], 1)
        self.assertIn("次の「共有へ保存」で写します", body["message"])

    def test_CSV(self) -> None:
        self.push(self.save_shift())
        body = self.post("/api/standard-time/standards/csv", {"scope": "line", "packages": 10,
                                                        "workers": 2}).get_json()
        out = Path(body["file"])
        self.assertTrue(out.exists(), body)
        self.assertTrue(out.name.startswith("標準作業時間_"))
        self.assertTrue(out.name.endswith("_L-1.csv"))
        text = out.read_text(encoding="utf-8-sig").splitlines()
        self.assertIn("10梱包を2人で(分)", text[0])
        self.assertEqual(len(text), 3)

    def test_設定から過去ぶんを入れる(self) -> None:
        self.save_shift()
        body = self.post("/api/standard-time/backfill",
                         {"start": "2026-09-01", "end": "2026-09-30"}).get_json()
        self.assertEqual(body["shifts"], 1, body)
        self.assertTrue(self.db().exists())
        body = self.post("/api/standard-time/backfill",
                         {"start": "2026-10-01", "end": "2026-10-31"}).get_json()
        self.assertEqual(body["shifts"], 0)
        self.assertIn("ありませんでした", body["message"])
        res = self.post("/api/standard-time/backfill",
                        {"start": "2026-09-30", "end": "2026-09-01"})
        self.assertEqual(res.status_code, 422)

    def test_同条件の作業(self) -> None:
        self.push(self.save_shift())
        body = self.post("/api/standard-time/works", {
            "purpose_code": "H176", "packing_spec_no": "1P0001", "size": "8.000×1528.0×3053.0",
            "team": "A"}).get_json()
        self.assertEqual(len(body["table"]["rows"]), 1, body)
        row = dict(zip(body["table"]["columns"], body["table"]["rows"][0]))
        self.assertEqual(row["作業時間(分)"], "60")
        self.assertEqual(row["標準作業時間(分)"], "60.0")
        self.assertEqual(body["summary"][0], ["件数", "1件"])
        self.assertEqual(body["condition"]["line"], "L-1")     # ライン未指定ならこの端末
        self.assertEqual(body["condition"]["join"], "and")

    def test_同条件の作業_OR_と計算式(self) -> None:
        self.push(self.save_shift())
        body = self.post("/api/standard-time/works", {
            "purpose_code": "X999", "packing_spec_no": "1P0001", "join": "or",
            "formulas": True, "line": ""}).get_json()
        self.assertEqual(len(body["table"]["rows"]), 1, body)
        self.assertIn("または", body["table"]["note"])
        self.assertIn("全ライン", body["table"]["note"])
        self.assertIn("式: 標準作業時間", body["table"]["columns"])

    def test_同条件の作業_何も無ければ422(self) -> None:
        res = self.post("/api/standard-time/works", {"purpose_code": ""})
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["field"], "purpose_code")
        res = self.post("/api/standard-time/works", {"purpose_code": "H176",
                                                     "start": "2026/09/01"})
        self.assertEqual(res.status_code, 400)

    def test_同条件の作業のCSV(self) -> None:
        self.push(self.save_shift())
        body = self.post("/api/standard-time/works/csv", {
            "purpose_code": "H176", "packing_spec_no": "1P0001",
            "size": "8x1528x3053", "formulas": True}).get_json()
        out = Path(body["file"])
        self.assertTrue(out.exists(), body)
        self.assertTrue(out.name.startswith("同条件作業_"))
        self.assertTrue(out.name.endswith("_L-1_H176_1P0001_8×1528×3053.csv"), out.name)
        lines = out.read_text(encoding="utf-8-sig").splitlines()
        self.assertTrue(lines[0].startswith("報告日,ライン,直,班,作業者,オペレーター構成,LOT"))
        self.assertTrue(lines[0].endswith("式: 標準作業時間,式: 標準との差"))
        self.assertEqual(len(lines), 2)
        # OR の名前は AND と別(同じ条件で上書きしない)
        body = self.post("/api/standard-time/works/csv", {
            "purpose_code": "H176", "packing_spec_no": "1P0001", "join": "or"}).get_json()
        self.assertIn("H176_又は_1P0001", Path(body["file"]).name)

    def test_別の画面になっている(self) -> None:
        """レールの「標準作業時間」。集計管理・設定からは外した。"""
        from app import shell

        keys = [key for key, *_ in shell.NAV]
        self.assertIn("standard", keys)
        self.assertIn("standard", shell.READY_SCREENS)
        self.assertEqual(keys.index("standard"), keys.index("agg") + 1)

        body = self.get("/standard-time").get_data(as_text=True)
        for part in ('name="works-join" value="and" checked', 'name="works-join" value="or"',
                     'name="standard-join" value="or"', 'id="works-formulas"',
                     'id="standard-formulas"',
                     'data-tabs="standard"', 'data-key="standards"', 'data-key="works"',
                     'data-key="backfill"', 'id="standard-csv"', 'id="works-run"',
                     'id="works-csv"', 'id="backfill-run"', 'aria-current="page"',
                     'js/views/standard_time.js'):
            self.assertIn(part, body)
        self.assertEqual(re.findall(r'role="tab" data-key="(\w+)"', body),
                         [k for k, _, _ in view.TABS])

        agg = self.get("/agg").get_data(as_text=True)
        self.assertNotIn('id="standard-time"', agg)
        self.assertIn('href="/standard-time"', agg)
        self.assertNotIn('id="standard-backfill"', self.get("/settings").get_data(as_text=True))
        self.assertEqual(self.post("/api/agg/standard-time", {}).status_code, 404)

        js = (ROOT / "app" / "static" / "js" / "views" / "standard_time.js").read_text(encoding="utf-8")
        for path in ("/api/standard-time/standards", "/api/standard-time/works",
                     "/api/standard-time/works/csv", "/api/standard-time/backfill"):
            self.assertIn(path, js)


if __name__ == "__main__":
    unittest.main()
