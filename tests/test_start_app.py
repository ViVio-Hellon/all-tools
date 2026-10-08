"""ブラウザ版の入口(`start_app.py`)の心拍の見張り

画面(大きなタブのページ)の心拍が途絶えたら、入口は各ツールを止めて終わる。
**PC のスリープから戻った瞬間に「心拍が無い」で全ツールを止めない**(Windows の
`time.monotonic()` は眠っているあいだも進む)。各ツールの見張りと同じく、見張りの1回が
長く空いたら数え直す。
"""
from __future__ import annotations

import unittest

import tests  # noqa: F401  (一時フォルダへ向ける)

import start_app


class IdleWatchTests(unittest.TestCase):
    def test_心拍が途絶えたら終わる(self) -> None:
        watch = start_app.IdleWatch(0.0, idle_sec=90, first_contact_sec=300)
        beat = 10.0
        for now in range(1, 100):
            self.assertIsNone(watch.tick(float(now), beat))
        self.assertIsNone(watch.tick(100.0, beat))
        self.assertAlmostEqual(watch.tick(101.0, beat), 91.0)

    def test_最初の心拍を待つ上限(self) -> None:
        watch = start_app.IdleWatch(0.0, idle_sec=90, first_contact_sec=300)
        for now in range(1, 301):
            self.assertIsNone(watch.tick(float(now), 0.0))
        self.assertIsNotNone(watch.tick(301.0, 0.0))

    def test_スリープから戻ったら数え直す(self) -> None:
        """フタを閉じて2時間。起きた瞬間は終わらず、起きてから心拍が来なければ終わる。"""
        watch = start_app.IdleWatch(0.0, idle_sec=90, first_contact_sec=300, wake_gap_sec=30)
        beat = 5.0
        for now in range(6, 20):
            self.assertIsNone(watch.tick(float(now), beat))
        woke = 20.0 + 7200
        self.assertIsNone(watch.tick(woke, beat), "起きた瞬間に止めない")
        self.assertEqual(watch.slept, 1)
        for step in range(1, 91):
            self.assertIsNone(watch.tick(woke + step, beat))
        self.assertIsNotNone(watch.tick(woke + 91, beat), "起きてからも心拍が無ければ終わる")
        # 起きてから心拍が来れば続ける
        watch = start_app.IdleWatch(0.0, idle_sec=90, wake_gap_sec=30)
        self.assertIsNone(watch.tick(7200.0, 1.0))
        self.assertIsNone(watch.tick(7250.0 - 20, 1.0))
        self.assertIsNone(watch.tick(7231.0, 7230.5))
        self.assertIsNone(watch.tick(7300.0, 7230.5))


if __name__ == "__main__":
    unittest.main()
