"""複数台から同じDBへ書いたとき ── **黙って消えないか**

    複数PC・複数ユーザーが同時にDBへ書き込む場合
    SQLiteのロック / database is locked / 同時更新によるデータ消失
    トランザクション / 書き込み待ち・リトライ
    読み取り中に別PCが書き込む場合

読んだだけでは分からないので、**実際に同時に書いて**確かめます。
ここで押さえるのは4つです。

    1. 共有の日報データへ、何台ぶんが同時に書いても**1件も落ちない**
    2. 書いている最中に**読んでも** `database is locked` にならない
    3. 同じキーを2方向から書くと**あとの人が勝つ**(壊れはしない)。
       これは避けようがないので、運用で避けます ── 1ライン=1端末で、
       表もラインごとに分かれているので、ふつうは重なりません
    4. 途中で落ちたら**何も残さない**(1キー=1トランザクション)

【心拍を待たせない ── 見つかった不具合】
書く要求は1つずつ通します(`app._WRITE_LOCK`)。取り込みのような長い
POST は、終わるまでこの錠を持ったままです。**そこへ心拍まで並ばせて
いました。**

    取り込み(60秒) ─────────────────→ 終わり
      └ そのあいだ /api/tab/ping は待たされる
         → 15秒で「閉じられた」とみなされる(`LOST_AFTER_SEC`)
         → 明けた拍子に、先に通ったタブが打てる側を引き継ぐ

日報入力を2枚開いていると、**押してもいないのに打てる側が移ります。**
心拍はDBに触りません(覚えているのはプロセスの中だけ)。待たせる理由が
無いので、`/api/jobs`・`/api/alive` と同じ扱いにします。
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.access_bridge import sqlite_backend  # noqa: E402
from nippou.db.models import DetailRecord, HeaderRecord  # noqa: E402
from nippou.logic import tab_lock  # noqa: E402

try:
    from app import _is_write
    HAS_FLASK = True
except Exception:                                 # noqa: BLE001
    HAS_FLASK = False
SKIP = "Flask が入っていません"


def key_of(day: str, line: str) -> dict:
    return dict(report_date=day, line=line, shift="1直", page=1)


def rows_of(day: str, line: str, tag: str, count: int = 6
            ) -> list[DetailRecord]:
    return [DetailRecord(**key_of(day, line), row_no=r, lot=f"{tag}-{r}")
            for r in range(1, count + 1)]


class SharedPushTests(unittest.TestCase):
    """共有の日報データ(sqlite3)へ、**何台ぶんも同時に書く。**"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="shared-")
        self.addCleanup(self.tmp.cleanup)
        self.shared = Path(self.tmp.name) / "日報データ.sqlite3"

    def push(self, line: str, day: str, tag: str = "A"):
        return sqlite_backend.push_records(
            self.shared, HeaderRecord(**key_of(day, line), worker=tag),
            rows_of(day, line, tag),
            f"T_日報ヘッダー_{line}", f"T_日報明細_{line}")

    def count(self, table: str) -> int:
        conn = sqlite3.connect(str(self.shared))
        try:
            return conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        finally:
            conn.close()

    def test_4台が同時に書いても1件も落ちない(self) -> None:
        lines = ["L-1", "L2", "機側", "NS1"]
        failures: list[str] = []
        lock = threading.Lock()

        def one(line: str) -> None:
            for i in range(5):
                result = self.push(line, f"2026年9月{i + 1}日", line)
                if not result.success:
                    with lock:
                        failures.append(f"{line}: {result.error}")

        threads = [threading.Thread(target=one, args=(ln,)) for ln in lines]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)

        self.assertEqual(failures, [], "書けなかったものがあります")
        for line in lines:
            self.assertEqual(self.count(f"T_日報ヘッダー_{line}"), 5)
            self.assertEqual(self.count(f"T_日報明細_{line}"), 30)

    def test_書いている最中に読んでも断られない(self) -> None:
        """**読み取り中に別PCが書き込む場合。** ロックで落ちないこと。"""
        self.push("L-1", "2026年9月1日")
        stop = threading.Event()
        errors: list[str] = []

        def writer() -> None:
            try:
                for i in range(20):
                    self.push("L-1", f"2026年9月{i + 1}日", f"W{i}")
            finally:
                stop.set()

        thread = threading.Thread(target=writer)
        thread.start()
        reads = 0
        while not stop.is_set():
            try:
                conn = sqlite3.connect(str(self.shared), timeout=10)
                conn.execute("PRAGMA busy_timeout = 10000")
                conn.execute('SELECT COUNT(*) FROM "T_日報ヘッダー_L-1"'
                             ).fetchone()
                conn.close()
                reads += 1
            except sqlite3.Error as exc:          # noqa: PERF203
                errors.append(str(exc))
                break
        thread.join(60)

        self.assertEqual(errors, [], "読みがロックで断られました")
        self.assertGreater(reads, 0, "1度も読めていません")

    def test_同じキーはあとの人が勝つ(self) -> None:
        """**消えます。** 直せないので、書いておきます。

        1ライン=1端末で、表もラインごとに分かれているので、ふつうは
        重なりません ── 重なるのは、2台に同じラインを入れたときだけです。
        """
        self.push("L-1", "2026年9月1日", "先の人")
        self.push("L-1", "2026年9月1日", "あとの人")
        conn = sqlite3.connect(str(self.shared))
        try:
            worker = conn.execute(
                'SELECT "担当者" FROM "T_日報ヘッダー_L-1"').fetchall()
        finally:
            conn.close()
        self.assertEqual([r[0] for r in worker], ["あとの人"])
        self.assertEqual(self.count("T_日報明細_L-1"), 6,
                         "明細が二重になっています")


@unittest.skipUnless(HAS_FLASK, SKIP)
class WriteLockTests(unittest.TestCase):
    """**待たせてよい POST と、待たせてはいけない POST。**"""

    class Req:
        def __init__(self, method: str, path: str) -> None:
            self.method = method
            self.path = path

    def test_保存は1つずつ通す(self) -> None:
        self.assertTrue(_is_write(self.Req("POST", "/api/entry/save")))

    def test_見るだけは待たせない(self) -> None:
        self.assertFalse(_is_write(self.Req("GET", "/api/entry/load")))

    def test_心拍は待たせない(self) -> None:
        """**ここが抜けていました。** 15秒で打てる側が移ります。"""
        for path in ("/api/tab/ping", "/api/tab/claim", "/api/tab/release"):
            self.assertFalse(_is_write(self.Req("POST", path)), path)

    def test_進み具合と生存も待たせない(self) -> None:
        self.assertFalse(_is_write(self.Req("POST", "/api/jobs/progress")))
        self.assertFalse(_is_write(self.Req("POST", "/api/alive")))


class HeartbeatStallTests(unittest.TestCase):
    """待たせると何が起きるか ── **打てる側が黙って移る。**"""

    def test_心拍が途切れると別のタブへ移る(self) -> None:
        desk = tab_lock.TabDesk()
        desk.claim("打っているタブ", now=0.0)
        desk.claim("見ているタブ", now=1.0)
        self.assertEqual(desk.editor, "打っているタブ")

        # 取り込みが錠を握っているあいだ、どちらの心拍も通らない。
        # 明けたとき、先に通ったほうが引き継ぐ
        after = tab_lock.LOST_AFTER_SEC + 1.0
        got = desk.ping("見ているタブ", now=after)

        self.assertTrue(got.may_edit, "先に通ったほうが打てる側になります")
        self.assertEqual(
            desk.ping("打っているタブ", now=after + 0.1).role,
            tab_lock.ROLE_VIEWER,
            "押してもいないのに「見るだけ」になりました")


if __name__ == "__main__":
    unittest.main()
