"""月替わりの書き出しは**1か月に1度**、そして**次の月も必ず** (v3.80.0)

    月替わりをこっちではテストできないのでそちらで行ってください

8月 → 9月 → 10月と日付を進めて本物の「共有へ保存」を通したところ、

    9月の共有へ保存のたびに 8月をまた書き出す(知らせも毎回出る)
    10月になっても 8月を書き出し、**9月はいつまでも書き出されない**

になっていました。VBA は書き出したあとにその月のシートを消していたので、
書き出した月は二度と候補に上がりませんでした。このツールは手元の日報を
消さないので、**書き出した月を覚えておく**必要がありました。

ここで押さえるのは:

    1. 候補は「今月より前の、まだ済んでいない月」。古いものから
    2. 書き出した月は二度と出さない。ただし**中身を直したら出し直す**
    3. 未送信が残った・2つ目を待った月は、済んだことにしない
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.connection import connect  # noqa: E402
from nippou.db.models import DetailRecord, HeaderRecord  # noqa: E402
from nippou.db.repository import NippouRepository  # noqa: E402
from nippou.db.schema import ensure_schema  # noqa: E402
from nippou.logic.month_roll import decide  # noqa: E402
from nippou.services import month_rollover  # noqa: E402

AUG, SEP, OCT = (2026, 8), (2026, 9), (2026, 10)


class DecideTests(unittest.TestCase):
    """判断(純ロジック)。"""

    def test_今月だけなら月の途中(self) -> None:
        self.assertFalse(decide([SEP], date(2026, 9, 25)).due)

    def test_前の月がまだなら片付ける(self) -> None:
        found = decide([AUG, SEP], date(2026, 9, 2))
        self.assertTrue(found.due)
        self.assertEqual((found.year, found.month), AUG)

    def test_前の月しか無くても片付ける(self) -> None:
        """VBA「いちばん新しい月 ≠ 今月」。10月1日の朝、まだ10月を打っていない。"""
        found = decide([SEP], date(2026, 10, 1))
        self.assertEqual((found.year, found.month), SEP)

    def test_済んだ月は候補にしない(self) -> None:
        """**ここが直したところ。** 済んだ8月をまた出していた。"""
        found = decide([AUG, SEP], date(2026, 9, 3), settled=[AUG])
        self.assertFalse(found.due)
        self.assertIn("8月までは書き出し済み", found.reason)

    def test_10月には9月を出す(self) -> None:
        """**ここも直したところ。** 10月になっても8月を出し、9月が出なかった。"""
        found = decide([AUG, SEP, OCT], date(2026, 10, 1), settled=[AUG])
        self.assertTrue(found.due)
        self.assertEqual((found.year, found.month), SEP)

    def test_済んでいない月が複数なら古いほうから(self) -> None:
        found = decide([AUG, SEP, OCT], date(2026, 11, 1))
        self.assertEqual((found.year, found.month), AUG)
        self.assertIn("ほかに2か月", found.reason)

    def test_未来の日付は片付けない(self) -> None:
        self.assertFalse(decide([SEP, (2027, 1)], date(2026, 9, 25)).due)


class RepoCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.conn = connect(self.tmp / "local.sqlite3")
        ensure_schema(self.conn)
        self.repo = NippouRepository(self.conn)
        self.addCleanup(self.conn.close)

    def save(self, label: str, ken: str = "30", shift: str = "1直") -> tuple:
        key = dict(report_date=label, line="L-1", shift=shift, page=1)
        self.repo.save(HeaderRecord(**key, worker="山田"), [DetailRecord(
            **key, row_no=1, lot="N7131T0", ken=ken, mai="10", tut="1",
            kz="07", kh="00", sz="15", sh="00", tim="420", wei="360", con=ken)])
        return (label, "L-1", shift, 1)


class SettledTests(RepoCase):
    """済んだ月を覚える。**中身が変わったら済んでいないことにする。**"""

    def test_覚えた月は済んでいる(self) -> None:
        self.save("2026年8月20日")
        self.repo.mark_rolled(*AUG)
        self.assertEqual(self.repo.settled_months(), {AUG})

    def test_同じ中身を保存し直しても済んだまま(self) -> None:
        self.save("2026年8月20日")
        self.repo.mark_rolled(*AUG)
        self.save("2026年8月20日")
        self.assertIn(AUG, self.repo.settled_months())

    def test_直したら出し直す(self) -> None:
        """書き出したあとに数字を直した。1秒のうちでも見落とさない(時刻ではなく中身で見る)。"""
        self.save("2026年8月20日")
        self.repo.mark_rolled(*AUG)
        self.save("2026年8月20日", ken="31")
        self.assertNotIn(AUG, self.repo.settled_months())

    def test_ページが増えたら出し直す(self) -> None:
        """取り込みで過去の直が入った、など。"""
        self.save("2026年8月20日")
        self.repo.mark_rolled(*AUG)
        self.save("2026年8月21日")
        self.assertNotIn(AUG, self.repo.settled_months())

    def test_ほかの月を直しても済んだまま(self) -> None:
        self.save("2026年8月20日")
        self.repo.mark_rolled(*AUG)
        self.save("2026年9月1日")
        self.assertIn(AUG, self.repo.settled_months())


class CatchUpTests(RepoCase):
    """共有へ保存の直後に走る片付け(`month_rollover.catch_up`)。"""

    def roll(self, today: date, second=None):
        return month_rollover.catch_up(self.repo, self.tmp / "月別", today=today,
                                       second=second)

    def sent(self, *keys) -> None:
        for key in keys:
            self.repo.mark_synced(key)

    def months(self, rolls) -> list:
        return [(r.decision.year, r.decision.month) for r, _ in rolls]

    def test_8月から10月まで(self) -> None:
        """本物の流れ。**各月1度ずつ、漏れなく。**"""
        self.sent(self.save("2026年8月20日"))
        self.assertEqual(self.months(self.roll(date(2026, 8, 31))), [])
        self.sent(self.save("2026年9月2日"))
        self.assertEqual(self.months(self.roll(date(2026, 9, 2))), [AUG])
        self.sent(self.save("2026年9月3日"))
        self.assertEqual(self.months(self.roll(date(2026, 9, 3))), [],
                         "9月の2回目の保存で8月をまた書き出しています")
        self.sent(self.save("2026年10月1日"))
        self.assertEqual(self.months(self.roll(date(2026, 10, 1))), [SEP],
                         "10月になっても9月が書き出されていません")
        self.assertEqual(self.months(self.roll(date(2026, 10, 2))), [])
        out = self.tmp / "月別" / "L-1"
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["2026.08", "2026.09"])

    def test_溜まった月は1回でぜんぶ片付ける(self) -> None:
        """取り込みで何か月ぶんも入ったとき。古い順に。"""
        for label in ("2026年6月1日", "2026年7月1日", "2026年8月1日"):
            self.sent(self.save(label))
        rolls = self.roll(date(2026, 9, 1))
        self.assertEqual(self.months(rolls), [(2026, 6), (2026, 7), AUG])

    def test_上限を超えたぶんは次の保存で(self) -> None:
        for m in range(1, 13):
            self.sent(self.save(f"2025年{m}月1日"))
        self.sent(self.save("2026年1月5日"))
        first = self.roll(date(2026, 3, 1))
        self.assertEqual(len(first), month_rollover.MAX_MONTHS_PER_RUN)
        rest = self.roll(date(2026, 3, 1))
        self.assertEqual(self.months(rest), [(2026, 1)])

    def test_未送信が残った月は済んだことにしない(self) -> None:
        """書き出しは出す(知らせる)が、送り終えてからもう1度出す。"""
        self.sent(self.save("2026年8月20日"))
        self.save("2026年8月20日", shift="2直")          # 送っていない
        rolls = self.roll(date(2026, 9, 1))
        self.assertTrue(rolls[0][0].pending)
        self.assertNotIn(AUG, self.repo.settled_months())
        self.sent(("2026年8月20日", "L-1", "2直", 1))
        self.assertEqual(self.months(self.roll(date(2026, 9, 2))), [AUG])
        self.assertIn(AUG, self.repo.settled_months())

    def test_済ませられない月があればそこで止める(self) -> None:
        """次の月へ進むと、古い月を置き去りにしたまま済んだ扱いが増える。"""
        self.save("2026年7月20日")                       # 送っていない
        self.sent(self.save("2026年8月20日"))
        self.assertEqual(self.months(self.roll(date(2026, 9, 1))), [(2026, 7)])

    def test_2つ目を待った月は済んだことにしない(self) -> None:
        class Held:
            ok = False
        self.sent(self.save("2026年8月20日"))
        self.roll(date(2026, 9, 1), second=lambda result: Held())
        self.assertNotIn(AUG, self.repo.settled_months())
        self.assertEqual(self.months(self.roll(date(2026, 9, 2))), [AUG])
        self.assertIn(AUG, self.repo.settled_months())

    def test_直した月は次の保存で出し直す(self) -> None:
        self.sent(self.save("2026年8月20日"))
        self.roll(date(2026, 9, 1))
        self.sent(self.save("2026年8月20日", ken="31"))
        self.assertEqual(self.months(self.roll(date(2026, 9, 2))), [AUG])


if __name__ == "__main__":
    unittest.main()
