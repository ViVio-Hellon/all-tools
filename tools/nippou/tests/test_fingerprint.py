"""差分がなければ、共有へ保存をもう一度求めない

    共有へ保存 をしてから 保存(確定) を行うと
    共有へ未送信 1直ぶん になる
    差分のことを考えてだろうけども... 差分チェックすると重い？
    差分がなければやはり 共有へ保存 を何度も求める必要がないのでは

保存は押すたびに「まだ送っていない」(`dirty`)を立てていました。1文字も
変えずに「保存(確定)」を押しただけで未送信へ戻り、共有へ保存をもう一度
求めます ── 押しても、共有には同じものが書き直されるだけです。

**重くありません。** 12行35欄を並べて SHA-256 を1回。保存の中でやって
いる DELETE+INSERT 13本のほうが、比べものにならないほど重い。
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.connection import connect
from nippou.db.models import DetailRecord, HeaderRecord
from nippou.db.repository import NippouRepository
from nippou.logic import fingerprint

try:
    from tests._web import WebTestCase
    HAS_FLASK = True
except Exception:                                 # noqa: BLE001
    HAS_FLASK = False
    WebTestCase = object                          # type: ignore
SKIP = "Flask が入っていません"

KEY = dict(report_date="2026年9月18日", line="L-1", shift="1直", page=1)


def head(**over) -> HeaderRecord:
    return HeaderRecord(**{**KEY, "worker": "山田", **over})


def rows(count: int = 12, **over) -> list[DetailRecord]:
    """1行目だけ打ってある紙。`over` でその行の欄を差し替えます。"""
    first = dict(lot="N7131T0", ken="10", kz="08", kh="00", sz="09", sh="00")
    first.update(over)
    made = [DetailRecord(**KEY, row_no=i) for i in range(1, count + 1)]
    made[0] = DetailRecord(**KEY, row_no=1, **first)
    return made


class PureTests(unittest.TestCase):
    """**指紋そのもの。** DBも時計も要りません。"""

    def test_同じ中身なら同じ指紋(self) -> None:
        self.assertEqual(fingerprint.page_fingerprint(head(), rows()),
                         fingerprint.page_fingerprint(head(), rows()))

    def test_1文字でも違えば別の指紋(self) -> None:
        self.assertNotEqual(fingerprint.page_fingerprint(head(), rows()),
                            fingerprint.page_fingerprint(head(), rows(ken="11")))

    def test_保存日時は数えない(self) -> None:
        """**押すたびに変わる欄**を数えると、指紋の意味がありません。"""
        self.assertEqual(
            fingerprint.page_fingerprint(head(saved_at="10:00"), rows()),
            fingerprint.page_fingerprint(head(saved_at="23:59"), rows()))

    def test_送った印も数えない(self) -> None:
        self.assertEqual(
            fingerprint.page_fingerprint(head(dirty=True, synced_at=None), rows()),
            fingerprint.page_fingerprint(head(dirty=False, synced_at="x"), rows()))

    def test_手元だけの欄は数えない(self) -> None:
        """引当番号は共有へ出ません ── 変わっても送り直す先がない。"""
        self.assertEqual(fingerprint.page_fingerprint(head(), rows()),
                         fingerprint.page_fingerprint(head(), rows(hiki_no="12345678")))

    def test_理由は数える(self) -> None:
        """行ごとの理由は、まとめてヘッダーの欄で共有へ出ます。"""
        self.assertNotEqual(fingerprint.page_fingerprint(head(), rows()),
                            fingerprint.page_fingerprint(head(reason="3行目: 棚卸し"),
                                                         rows()))

    def test_空の行は数えない(self) -> None:
        """12行そろえても1行だけでも、共有から見た中身は同じです。"""
        self.assertEqual(fingerprint.page_fingerprint(head(), rows(count=12)),
                         fingerprint.page_fingerprint(head(), rows(count=1)))

    def test_行の順は問わない(self) -> None:
        made = rows()
        self.assertEqual(fingerprint.page_fingerprint(head(), made),
                         fingerprint.page_fingerprint(head(), list(reversed(made))))

    def test_前後の空白は同じものと見る(self) -> None:
        self.assertEqual(fingerprint.page_fingerprint(head(worker="山田"), rows()),
                         fingerprint.page_fingerprint(head(worker=" 山田 "), rows()))

    def test_軽い(self) -> None:
        """**重くないこと。** 1ページ100回で 50ms を切ります。"""
        header, details = head(), rows()
        started = time.perf_counter()
        for _ in range(100):
            fingerprint.page_fingerprint(header, details)
        self.assertLess(time.perf_counter() - started, 0.05)


class SaveTests(unittest.TestCase):
    """**保存のふるまい。** 押しただけでは未送信に戻らない。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.conn = connect(Path(self._tmp.name) / "t.sqlite3")
        self.addCleanup(self.conn.close)
        self.repo = NippouRepository(self.conn)

    def pending(self) -> int:
        return len(self.repo.pending_sync_headers())

    def push(self) -> None:
        self.repo.mark_synced((KEY["report_date"], KEY["line"],
                               KEY["shift"], KEY["page"]))

    def test_打ったら未送信になる(self) -> None:
        self.repo.save(head(), rows())
        self.assertEqual(self.pending(), 1)

    def test_送ったあと同じ中身で保存しても未送信に戻らない(self) -> None:
        """**ここが本題。**"""
        self.repo.save(head(), rows())
        self.push()
        self.repo.save(head(), rows())
        self.assertEqual(self.pending(), 0)

    def test_送った印も残る(self) -> None:
        """「いつ共有へ出たか」を、同じ中身の保存で消さない。"""
        self.repo.save(head(), rows())
        self.push()
        before = self.repo.load(**KEY)[0].synced_at
        self.repo.save(head(), rows())
        self.assertEqual(self.repo.load(**KEY)[0].synced_at, before)

    def test_中身が変われば未送信になる(self) -> None:
        self.repo.save(head(), rows())
        self.push()
        self.repo.save(head(), rows(ken="11"))
        self.assertEqual(self.pending(), 1)

    def test_直して戻せば未送信も消える(self) -> None:
        """**指紋は送ったときのものを残します。** 元に戻したなら同じ。"""
        self.repo.save(head(), rows())
        self.push()
        self.repo.save(head(), rows(ken="11"))
        self.assertEqual(self.pending(), 1)
        self.repo.save(head(), rows())
        self.assertEqual(self.pending(), 0)

    def test_送る前は何度保存しても未送信のまま(self) -> None:
        for _ in range(3):
            self.repo.save(head(), rows())
        self.assertEqual(self.pending(), 1)

    def test_保存日時は毎回書き換わる(self) -> None:
        """未送信に戻さないだけで、**打った記録は新しくします。**"""
        self.repo.save(head(), rows())
        self.push()
        first = self.repo.load(**KEY)[0].saved_at
        time.sleep(1.05)                       # 保存日時は秒まで
        self.repo.save(head(), rows())
        self.assertNotEqual(self.repo.load(**KEY)[0].saved_at, first)

    def test_返したヘッダーも同じことを言う(self) -> None:
        """`save` が書き換える `header.dirty` も本当のことを言うこと。"""
        header = head()
        self.repo.save(header, rows())
        self.assertTrue(header.dirty)
        self.push()
        again = head()
        self.repo.save(again, rows())
        self.assertFalse(again.dirty)

    def test_引当番号だけ変えても未送信にしない(self) -> None:
        """共有へ出ない欄です ── 送り直す先がありません。"""
        self.repo.save(head(), rows())
        self.push()
        self.repo.save(head(), rows(hiki_no="12345678"))
        self.assertEqual(self.pending(), 0)
        # 手元には入っている(数えないだけで、捨てていない)
        self.assertEqual(self.repo.load(**KEY)[1][0].hiki_no, "12345678")

    def test_古い端末の紙は1度だけ未送信に戻る(self) -> None:
        """指紋を付ける前に送ったページ ── 控えが無いので1度は送り直す。

        **黙って「同じ」と言わない**ためです。共有に何が入っているか
        分からないものを、分かったことにはできません。
        """
        self.repo.save(head(), rows())
        self.push()
        self.conn.execute("UPDATE daily_header SET synced_hash=NULL")
        self.conn.commit()
        self.repo.save(head(), rows())
        self.assertEqual(self.pending(), 1)
        self.push()
        self.repo.save(head(), rows())
        self.assertEqual(self.pending(), 0)


@unittest.skipUnless(HAS_FLASK, SKIP)
class ScreenTests(WebTestCase):
    """画面から見たところ ── **押しただけで「未送信」に戻らない。**"""

    def setUp(self) -> None:
        super().setUp()
        self.post("/api/entry/line", {"line": "L-1"})

    def sheet(self, **over) -> dict:
        row = {"LOT": "1111111", "ZAI": "SPCC", "SIZ": "1.0",
               "KEN": "10", "KZ": "08", "KH": "00", "SZ": "09", "SH": "00",
               "HIT": "1", "MAI": "10", "TUT": "1"}
        row.update(over)
        return {"rows": {"1": row}, "header": {"worker": "山田"}, "checks": {}}

    def push_all(self) -> None:
        """共有へ出たことにする(送り先は要らない ── 印だけの話)。"""
        repo = self.repo()
        for header in repo.pending_sync_headers():
            repo.mark_synced(header.key())

    def pending(self) -> int:
        return len(self.repo().pending_sync_headers())

    def test_共有へ出したあと保存だけしても未送信に戻らない(self) -> None:
        self.post("/api/entry/save", self.sheet())
        self.push_all()
        self.assertEqual(self.pending(), 0)
        self.post("/api/entry/save", self.sheet())
        self.assertEqual(self.pending(), 0, "同じ中身で未送信に戻っています")

    def test_レールの印も出ない(self) -> None:
        """「共有へ未送信 1直ぶん」が、押しただけで戻らないこと。"""
        self.post("/api/entry/save", self.sheet())
        self.push_all()
        self.post("/api/entry/save", self.sheet())
        html = self.get("/").get_data(as_text=True)
        self.assertNotIn("共有へ未送信", html)

    def test_直せば出る(self) -> None:
        self.post("/api/entry/save", self.sheet())
        self.push_all()
        self.post("/api/entry/save", self.sheet(KEN="20"))
        self.assertEqual(self.pending(), 1)
        self.assertIn("共有へ未送信", self.get("/").get_data(as_text=True))

    def test_自動保存でも戻らない(self) -> None:
        """打っているあいだの自動保存でも同じです。"""
        self.post("/api/entry/save", self.sheet())
        self.push_all()
        self.post("/api/entry/save", {**self.sheet(), "silent": True})
        self.assertEqual(self.pending(), 0)


if __name__ == "__main__":
    unittest.main()
