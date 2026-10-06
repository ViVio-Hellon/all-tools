"""保存前チェックと月替わり ── **DBを読み直して確かめる**

`services/shift_check.py` と `services/month_rollover.py`、およびそれを
使う画面(日報入力の「確かめる」・共有への保存・設定の月替わり)。

ここで確かめたいのは、VBA から引き継いだ思想のほうです:
**画面の入力ではなく、保存されている中身を見る。** 「表を見る/直す」
画面や他の道具から直に書き換えられた行も、同じ関門を通ります。
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase

DAY = "2026年8月31日"
LINE = "HVC"
SHIFT = "3直"


def detail(row_no: int, page: int = 1, **values):
    from nippou.db.models import DetailRecord

    base = dict(report_date=DAY, line=LINE, shift=SHIFT, page=page, row_no=row_no)
    base.update(values)
    return DetailRecord(**base)


def header(page: int = 1, day_shift: str = "無", **values):
    from nippou.db.models import HeaderRecord

    base = dict(report_date=DAY, line=LINE, shift=SHIFT, page=page,
                day_shift=day_shift)
    base.update(values)
    return HeaderRecord(**base)


# ======================================================================
# サービス層 (Flask 不要)
# ======================================================================
class ServiceTestCase(unittest.TestCase):
    """一時DBを1件ごとに作る。Web を通さずサービスだけを見る。"""

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

        from nippou.db.connection import connect
        from nippou.db.repository import NippouRepository
        from nippou.db.schema import ensure_schema

        conn = connect(self.tmp / "t.sqlite3")
        ensure_schema(conn)
        self.addCleanup(conn.close)
        self.repo = NippouRepository(conn)

    def save(self, rows, page: int = 1, **head) -> None:
        self.repo.save(header(page=page, **head), rows)


class ReadsFromDbTests(ServiceTestCase):
    """**画面ではなくDBを見る**(シート直書き対策の思想)。"""

    def test_保存済みの全ページを読む(self) -> None:
        from nippou.services import shift_check

        self.save([detail(12, page=1, lot="A", ken="10", mai="5", tut="1")], page=1)
        self.save([detail(1, page=2, lot="", mai="6", tut="1")], page=2)
        report = shift_check.run(self.repo, DAY, LINE, SHIFT, codes=["0"])
        self.assertEqual(report.pages, 2)
        self.assertEqual(report.rows, 2)
        # ページをまたいで足さないと、この超過は見つからない
        self.assertIn("pack_over", [f.code for f in report.findings])

    def test_あとから直に書き換えた値も見る(self) -> None:
        """「表を見る/直す」画面や他の道具で直された行も同じ関門を通る。"""
        from nippou.services import shift_check

        self.save([detail(1, lot="A", ken="10", mai="1", tut="1")])
        self.repo.conn.execute(
            "UPDATE daily_detail SET mai='99' WHERE report_date=?", (DAY,))
        self.repo.conn.commit()
        report = shift_check.run(self.repo, DAY, LINE, SHIFT, codes=["0"])
        self.assertIn("pack_over", [f.code for f in report.findings])

    def test_1ページも無ければ何も言わない(self) -> None:
        from nippou.services import shift_check

        report = shift_check.run(self.repo, DAY, LINE, SHIFT)
        self.assertTrue(report.ok)
        self.assertEqual(report.pages, 0)

    def test_昼稼働ならヘッダから読んで休憩を見ない(self) -> None:
        from nippou.services import shift_check

        self.save([detail(1, lot="A")], day_shift="有")
        report = shift_check.run(self.repo, DAY, LINE, SHIFT, codes=["0"])
        self.assertNotIn("short_break", [f.code for f in report.findings])

    def test_直の長さは時間マスタから引く(self) -> None:
        from nippou.services import shift_check

        self.repo.set_shift_time("3", "22:30", "06:30")
        start, end = shift_check.bounds_of(self.repo.get_shift_times(), "3直")
        self.assertEqual((start, end), ("22:30", "06:30"))

    def test_時間マスタが無ければ既定に落ちる(self) -> None:
        """取り込む前でも動かす ── マスタが無いことを理由に止めない。"""
        from nippou.logic.shift import DEFAULT_SHIFT_TIMES
        from nippou.services import shift_check

        start, end = shift_check.bounds_of({}, "1直")
        self.assertEqual((start, end),
                         (DEFAULT_SHIFT_TIMES.start1, DEFAULT_SHIFT_TIMES.end1))


class SkipPhraseTests(ServiceTestCase):
    """逃げ道は VBA と同じ範囲(最終時間・休憩)だけ。"""

    def _saved(self) -> None:
        self.save([detail(1, lot="A", ken="1", mai="9", tut="9", sz="4", sh="0")])

    def test_打った一文だけ通す(self) -> None:
        from nippou.services import shift_check

        self.assertTrue(shift_check.normalize_skip("設備移動のため保存"))
        self.assertTrue(shift_check.normalize_skip(" 設備移動のため保存 "))
        self.assertFalse(shift_check.normalize_skip("はい"))

    def test_真偽値では通さない(self) -> None:
        """ボタン1つで通せると押し流される。打つと一度は考える。"""
        from nippou.services import shift_check

        self.assertFalse(shift_check.normalize_skip(True))
        self.assertFalse(shift_check.normalize_skip(1))

    def test_通しても梱包数は残る(self) -> None:
        from nippou.services import shift_check

        self._saved()
        report = shift_check.run(self.repo, DAY, LINE, SHIFT, skip=True,
                                 codes=["0"])
        self.assertEqual([f.code for f in report.findings], ["pack_over"])
        self.assertTrue({f.code for f in report.skipped}
                        <= {"shift_end", "short_break"})


class PendingTests(ServiceTestCase):
    """送ろうとしている直を、直の単位でまとめて見る。"""

    def test_送る直だけ見る(self) -> None:
        from nippou.services import shift_check

        self.save([detail(1, lot="A")])
        reports = shift_check.run_pending(self.repo, skip=True)
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].shift, SHIFT)

    def test_送り終えたものは見ない(self) -> None:
        from nippou.services import shift_check

        self.save([detail(1, lot="A")])
        self.repo.mark_synced((DAY, LINE, SHIFT, 1))
        self.assertEqual(shift_check.run_pending(self.repo), [])

    def test_ページが2つでも直は1つ(self) -> None:
        from nippou.services import shift_check

        self.save([detail(1, page=1)], page=1)
        self.save([detail(1, page=2)], page=2)
        self.assertEqual(len(shift_check.run_pending(self.repo)), 1)

    def test_断りの文にどの直かが出る(self) -> None:
        from nippou.services import shift_check

        self.save([detail(1, lot="A", ken="1", mai="9", tut="9")])
        reports = shift_check.run_pending(self.repo)
        text = shift_check.combined_message(reports)
        self.assertIn(DAY, text)
        self.assertIn("梱包数", text)
        self.assertIn(shift_check.SKIP_PHRASE, text)


# ======================================================================
# 月替わり
# ======================================================================
class MonthDecisionTests(unittest.TestCase):
    """`月替わりチェック実行` の判断そのもの。"""

    def test_今月だけなら月の途中(self) -> None:
        from nippou.logic.month_roll import decide

        self.assertFalse(decide([(2026, 9)], date(2026, 9, 10)).due)

    def test_先月ぶんだけなら月替わり(self) -> None:
        from nippou.logic.month_roll import decide

        found = decide([(2026, 8)], date(2026, 9, 1))
        self.assertTrue(found.due)
        self.assertEqual((found.year, found.month), (2026, 8))

    def test_混ざっていたら古いほうから(self) -> None:
        """月初の何日かは前月の3直を打つ ── 新しいほうを消してはいけない。"""
        from nippou.logic.month_roll import decide

        found = decide([(2026, 8), (2026, 9)], date(2026, 9, 2))
        self.assertTrue(found.due)
        self.assertEqual((found.year, found.month), (2026, 8))

    def test_データが無ければ何もしない(self) -> None:
        from nippou.logic.month_roll import decide

        self.assertFalse(decide([], date(2026, 9, 1)).due)

    def test_年をまたぐ(self) -> None:
        from nippou.logic.month_roll import decide

        found = decide([(2025, 12)], date(2026, 1, 5))
        self.assertEqual((found.year, found.month), (2025, 12))

    def test_フォルダとファイルの名前(self) -> None:
        from nippou.logic import month_roll

        self.assertEqual(month_roll.month_folder_name(2026, 8), "2026.08")
        self.assertEqual(month_roll.month_file_stem(2026, 8, "HVC"),
                         "2026年08月_HVC")

    def test_使えない文字は落とす(self) -> None:
        from nippou.logic.month_roll import safe_name

        self.assertEqual(safe_name("L/1"), "L1")
        self.assertEqual(safe_name(""), "Unknown")

    def test_月の読み取り(self) -> None:
        from nippou.logic.month_roll import parse_month

        for text in ("2026-08", "2026.08", "2026年8月"):
            self.assertEqual(parse_month(text), (2026, 8), text)
        self.assertIsNone(parse_month("2026"))
        self.assertIsNone(parse_month("2026-13"))


class RolloverTests(ServiceTestCase):
    def _month(self) -> None:
        self.save([detail(1, lot="A", s="0", th="30", tim="60", con="10",
                          wei="1000")])

    def test_書き出しても手元は残る(self) -> None:
        """**日報は値。消したら二度と同じ紙を出せない。**

        VBA の `月初削除` が消していたのは Excelシート(見た目の写し)で、
        元データは Access に残っていた。ここは元データそのもの。
        """
        from nippou.services import month_rollover

        self._month()
        self.repo.mark_synced((DAY, LINE, SHIFT, 1))
        result = month_rollover.run(self.repo, self.tmp / "out",
                                    today=date(2026, 9, 10))
        self.assertTrue(result.ran)
        self.assertEqual(self.repo.month_keys(), [(2026, 8)])
        self.assertIsNotNone(self.repo.load(DAY, LINE, SHIFT, 1))
        self.assertIn("そのまま残ります", result.message)

    def test_3本のCSVが出る(self) -> None:
        from nippou.services import month_rollover

        self._month()
        self.repo.mark_synced((DAY, LINE, SHIFT, 1))
        result = month_rollover.run(self.repo, self.tmp / "out",
                                    today=date(2026, 9, 10))
        names = sorted(p.name for p in result.exports[0].files)
        self.assertEqual(names, ["2026年08月_HVC_停止集計.csv",
                                 "2026年08月_HVC_明細.csv",
                                 "2026年08月_HVC_集計.csv"])
        folder = result.exports[0].folder
        # **ラインが先。** 自分のラインの1年ぶんを見るのに12個のフォルダを
        # 開かずに済みます(VBA は `yyyy.mm\ライン` の順でした)
        self.assertEqual(folder.name, "2026.08")
        self.assertEqual(folder.parent.name, "HVC")

    def test_明細まで残す(self) -> None:
        """消したあとに残るのはこのファイルだけ ── 中身が要る。"""
        from nippou.services import month_rollover

        self._month()
        self.repo.mark_synced((DAY, LINE, SHIFT, 1))
        result = month_rollover.run(self.repo, self.tmp / "out",
                                    today=date(2026, 9, 10))
        text = [p for p in result.exports[0].files
                if "明細" in p.name][0].read_text(encoding="utf-8-sig")
        head, first = text.splitlines()[0], text.splitlines()[1]
        # 読める名前で出す(`others3` では誰も分からない)
        self.assertIn("ロット№", head)
        self.assertIn("納入先", head)
        self.assertIn("包装仕様書No", head)
        self.assertIn("A", first)

    def test_月別も日別と同じ出どころ(self) -> None:
        """**月別だけ別の道を通らない。**

        月別の書き出しも、日ごとのCSVと同じく残してある集計から作ります
        ── 片方だけ打った明細を計算し直していると、並べて見たときに
        「どちらが正か」が言えません。集計がまだ無い月でも、読む前に
        `summary.fill_missing` が作るので空になりません。
        """
        from nippou.services import month_rollover

        self._month()
        self.repo.mark_synced((DAY, LINE, SHIFT, 1))
        # この仕組みが入る前に保存されたぶん(集計がまだ無い)を作る
        self.repo.conn.execute("DELETE FROM packing_report")
        self.repo.conn.commit()

        result = month_rollover.run(self.repo, self.tmp / "out",
                                    today=date(2026, 9, 10))
        agg = [p for p in result.exports[0].files
               if "集計" in p.name and "停止" not in p.name][0]
        lines = agg.read_text(encoding="utf-8-sig").splitlines()
        self.assertEqual(len(lines), 2, lines)        # 見出し + 1直ぶん
        self.assertIn(SHIFT, lines[1])

    def test_月別の停止集計も分類ごとに出る(self) -> None:
        """日ごとの `停止内訳_` と同じ名前・同じ畳み方で。"""
        from nippou.services import month_rollover

        self._month()
        self.repo.mark_synced((DAY, LINE, SHIFT, 1))
        result = month_rollover.run(self.repo, self.tmp / "out",
                                    today=date(2026, 9, 10))
        text = [p for p in result.exports[0].files
                if "停止集計" in p.name][0].read_text(encoding="utf-8-sig")
        self.assertIn("分類", text.splitlines()[0])

    def test_送っていないページがあれば添えて言う(self) -> None:
        from nippou.services import month_rollover

        self._month()                                  # dirty のまま
        result = month_rollover.run(self.repo, self.tmp / "out",
                                    today=date(2026, 9, 10))
        self.assertTrue(result.ran)                    # 書き出しはする
        self.assertTrue(result.pending)
        self.assertIn("共有へ送っていない", result.message)

    def test_月を指定して写しを取り直せる(self) -> None:
        """月替わりでなくても、過ぎた月を書き出せる。"""
        from nippou.services import month_rollover

        self._month()
        result = month_rollover.run(self.repo, self.tmp / "out",
                                    today=date(2026, 8, 20), month=(2026, 8))
        self.assertTrue(result.ran)

    def test_月の途中なら何もしない(self) -> None:
        from nippou.services import month_rollover

        self._month()
        result = month_rollover.run(self.repo, self.tmp / "out",
                                    today=date(2026, 8, 20))
        self.assertFalse(result.ran)
        self.assertFalse((self.tmp / "out").exists())

    def test_ラインごとに分けて出す(self) -> None:
        from nippou.services import month_rollover

        self._month()
        other = header(); other.line = "LVC"
        row = detail(1, lot="B"); row.line = "LVC"
        self.repo.save(other, [row])
        for line in (LINE, "LVC"):
            self.repo.mark_synced((DAY, line, SHIFT, 1))
        result = month_rollover.run(self.repo, self.tmp / "out",
                                    today=date(2026, 9, 10))
        self.assertEqual([e.line for e in result.exports], ["HVC", "LVC"])


class MonthRepoTests(ServiceTestCase):
    def test_入っている月が分かる(self) -> None:
        self.save([detail(1)])
        self.assertEqual(self.repo.month_keys(), [(2026, 8)])

    def test_月ぶんを取り出せる(self) -> None:
        self.save([detail(1)])
        other = header(); other.report_date = "2026年9月1日"
        row = detail(1); row.report_date = "2026年9月1日"
        self.repo.save(other, [row])
        self.assertEqual(len(self.repo.month_headers(2026, 8)), 1)
        self.assertEqual(len(self.repo.month_headers(2026, 9)), 1)

    def test_消す道は持たない(self) -> None:
        """**日報を消す入口はどこにも無い。**"""
        self.assertFalse(hasattr(self.repo, "delete_month"))

    def test_送っていないページがあるか分かる(self) -> None:
        self.save([detail(1)])
        self.assertTrue(self.repo.month_has_pending(2026, 8))
        self.repo.mark_synced((DAY, LINE, SHIFT, 1))
        self.assertFalse(self.repo.month_has_pending(2026, 8))


# ======================================================================
# 停止の項目ごと (Stop_Agg)
# ======================================================================
class StopItemTests(unittest.TestCase):
    def _records(self):
        return [(header(), [detail(1, s="0", th="30", ss="イ", ths="90"),
                            detail(2, s="0", th="15")])]

    def test_記号ごとに回数と時間(self) -> None:
        from nippou.logic.aggregation import stop_items

        items = stop_items(self._records())
        by_code = {i.code: i for i in items}
        self.assertEqual(by_code["0"].count, 2)
        self.assertEqual(by_code["0"].minutes, 45)
        self.assertEqual(by_code["イ"].minutes, 90)

    def test_長い順に並ぶ(self) -> None:
        from nippou.logic.aggregation import stop_items

        self.assertEqual([i.code for i in stop_items(self._records())],
                         ["イ", "0"])

    def test_文字種で分類が付く(self) -> None:
        from nippou.logic.aggregation import stop_items

        kinds = {i.code: i.kind for i in stop_items(self._records())}
        self.assertEqual(kinds["0"], "管理ロス停止")
        self.assertEqual(kinds["イ"], "突発停止")

    def test_内訳名を添えられる(self) -> None:
        from nippou.logic.aggregation import stop_items

        items = stop_items(self._records(), {"0": "休憩食事"})
        self.assertEqual([i.text for i in items if i.code == "0"], ["0 休憩食事"])

    def test_名前が無ければ記号だけ(self) -> None:
        from nippou.logic.aggregation import stop_items

        items = stop_items(self._records())
        self.assertEqual([i.text for i in items if i.code == "0"], ["0"])


# ======================================================================
# 画面 (Flask)
# ======================================================================
@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class CheckScreenTests(WebTestCase):
    """日報入力の「確かめる」と、共有への保存の関門。"""

    def setUp(self) -> None:
        self._saved_access = os.environ.get("NIPPOU_ACCESS_DIR")
        self._saved_monthly = os.environ.get("NIPPOU_MONTHLY_DIR")
        super().setUp()
        # **共有の書き先も月別フォルダも、この1件ぶんの一時領域へ**
        os.environ["NIPPOU_ACCESS_DIR"] = str(self.tmp / "shared")
        os.environ["NIPPOU_MONTHLY_DIR"] = str(self.tmp / "monthly")
        (self.tmp / "shared").mkdir(exist_ok=True)
        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        for key, value in (("NIPPOU_ACCESS_DIR", self._saved_access),
                           ("NIPPOU_MONTHLY_DIR", self._saved_monthly)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _current(self) -> tuple[str, str, str]:
        """いまの画面が書き込む先の (報告日, ライン, 直)。"""
        from nippou import work_context

        from app.routes.entry import build_shift_calculator

        ctx = work_context.get_context()
        calc = build_shift_calculator(self.repo().get_shift_times())
        return ctx.current_key(calc)

    def _save_bad(self) -> tuple[str, str, str]:
        """梱包数が検入枚数を超えているページを、いまの直に1つ作る。"""
        from nippou.db.models import DetailRecord, HeaderRecord

        day, line, shift = self._current()
        rows = [DetailRecord(report_date=day, line=line, shift=shift, page=1,
                             row_no=1, lot="A", ken="1", mai="9", tut="9")]
        self.repo().save(HeaderRecord(report_date=day, line=line, shift=shift,
                                      page=1), rows)
        return day, line, shift

    def test_確かめるは止めずに返す(self) -> None:
        self._save_bad()
        body = self.post("/api/entry/verify").get_json()
        self.assertFalse(body["ok"])
        self.assertIn("pack_over", [f["code"] for f in body["findings"]])
        self.assertEqual(body["sound"], "pack_over")

    def test_保存が1ページも無ければそう言う(self) -> None:
        body = self.post("/api/entry/verify").get_json()
        self.assertTrue(body["ok"])
        self.assertIn("保存されて", body["message"])

    def test_共有への保存は断る(self) -> None:
        self._save_bad()
        res = self.post("/api/settings/push", {})
        self.assertEqual(res.status_code, 422)
        body = res.get_json()
        self.assertEqual(body["error"]["code"], "check_failed")
        self.assertIn("梱包数", body["error"]["message"])
        self.assertTrue(body["reports"])
        self.assertEqual(body["skip_phrase"], "設備移動のため保存")

    def test_通せない断りは一文でも通らない(self) -> None:
        self._save_bad()
        res = self.post("/api/settings/push", {"skip": "設備移動のため保存"})
        self.assertEqual(res.status_code, 422)

    def test_通せる断りだけなら一文で通る(self) -> None:
        from nippou.db.models import DetailRecord, HeaderRecord

        day, line, shift = self._current()
        rows = [DetailRecord(report_date=day, line=line, shift=shift, page=1,
                             row_no=1, lot="A", ken="99", mai="1", tut="1")]
        self.repo().save(HeaderRecord(report_date=day, line=line, shift=shift,
                                      page=1), rows)
        # 最終時間も休憩も入っていないので、一文なしでは断られる
        self.assertEqual(self.post("/api/settings/push", {}).status_code, 422)
        res = self.post("/api/settings/push", {"skip": "設備移動のため保存"})
        self.assertNotEqual(res.status_code, 422)

    def test_何も溜まっていなければ関門は素通り(self) -> None:
        res = self.post("/api/settings/push", {})
        self.assertNotEqual(res.status_code, 422)

    def test_画面にボタンがある(self) -> None:
        """**何をするボタンかが名前から読めること。**

        以前は「この直を確かめる」でした ── 何を確かめるのかが分からず、
        「下見？結局どういう機能ですか？」と聞かれました。やっている
        ことは1つで、保存済みのこの直を7項目に当ててみて並べるだけです。
        """
        body = self.get("/").get_data(as_text=True)
        self.assertIn("この直をチェック", body)
        # 押しても保存しない・止めないことを、ボタン自身が言う
        self.assertIn("保存も、止めもしません", body)

    def test_次ページ発行という名前になっている(self) -> None:
        """「新しいページを出す」では、何が起きるのか分かりにくい。"""
        body = self.get("/").get_data(as_text=True)
        self.assertIn("次ページ発行", body)
        self.assertNotIn("新しいページを出す", body)


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class RolloverScreenTests(WebTestCase):
    def setUp(self) -> None:
        self._saved = os.environ.get("NIPPOU_MONTHLY_DIR")
        super().setUp()
        os.environ["NIPPOU_MONTHLY_DIR"] = str(self.tmp / "monthly")
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._saved is None:
            os.environ.pop("NIPPOU_MONTHLY_DIR", None)
        else:
            os.environ["NIPPOU_MONTHLY_DIR"] = self._saved

    def _old_month(self) -> None:
        from nippou.db.models import DetailRecord, HeaderRecord

        day = "2020年1月5日"
        self.repo().save(
            HeaderRecord(report_date=day, line="HVC", shift="1直", page=1),
            [DetailRecord(report_date=day, line="HVC", shift="1直", page=1,
                          row_no=1, lot="A", con="5", wei="100")])

    def test_状態が読める(self) -> None:
        self._old_month()
        body = self.get("/api/settings/rollover").get_json()
        self.assertTrue(body["due"])
        self.assertEqual(body["label"], "2020年1月")
        self.assertIn("monthly", body["dir"])

    def test_月の途中なら出ない(self) -> None:
        body = self.get("/api/settings/rollover").get_json()
        self.assertFalse(body["due"])

    def test_押すと書き出す(self) -> None:
        self._old_month()
        body = self.post("/api/settings/rollover", {"delete": False}).get_json()
        self.assertTrue(body["ran"])
        out = self.tmp / "monthly" / "HVC" / "2020.01"
        self.assertTrue((out / "2020年01月_HVC_集計.csv").exists())
        # 消さないと言ったので、手元には残る
        self.assertEqual(self.repo().month_keys(), [(2020, 1)])

    def test_画面に説明がある(self) -> None:
        body = self.get("/settings").get_data(as_text=True)
        self.assertIn("月替わりの書き出し", body)
        self.assertIn("月別書き出しの出力パス", body)


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class StopTableTests(WebTestCase):
    def test_集計画面に停止の項目が出る(self) -> None:
        body = self.get("/graph").get_data(as_text=True)
        # ダッシュボードのタイル(ドーナツと表の2枚)
        self.assertIn("停止の項目ごと", body)
        self.assertIn("停止の項目(表)", body)

    def test_直終わりの確認にも出る(self) -> None:
        body = self.get("/graph/review").get_data(as_text=True)
        self.assertIn("この日の停止(項目ごと)", body)


if __name__ == "__main__":
    unittest.main()
