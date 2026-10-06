"""管理者パスワードの鍵 (:mod:`kanban.admin_lock`)

**1 度開けたら、しばらく訊かない。** ただし開けっぱなしにはしない ──
現場の端末は 1 日つけっぱなしのことがあるので、使わない時間が続いたら閉める。
"""

from __future__ import annotations

import unittest

from kanban import admin_lock


class AdminLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.t = 1000.0
        self.lock = admin_lock.AdminLock(idle_sec=600, clock=lambda: self.t)

    def test_starts_locked(self):
        self.assertFalse(self.lock.is_unlocked())
        self.assertEqual(self.lock.remaining_sec(), 0)

    def test_unlock_then_lock(self):
        self.lock.unlock()
        self.assertTrue(self.lock.is_unlocked())
        self.lock.lock()
        self.assertFalse(self.lock.is_unlocked())

    def test_closes_after_being_left_alone(self):
        self.lock.unlock()
        self.t += 599
        self.assertTrue(self.lock.is_unlocked())
        self.t += 1
        self.assertFalse(self.lock.is_unlocked(), "放っておいても閉まらない")

    def test_using_it_keeps_it_open(self):
        """守られた操作をしているあいだは閉めない(作業の途中で訊き直さない)。"""
        self.lock.unlock()
        for _ in range(5):
            self.t += 500
            self.assertTrue(self.lock.is_unlocked(touch=True))
        self.t += 601
        self.assertFalse(self.lock.is_unlocked())

    def test_just_looking_does_not_keep_it_open(self):
        """画面が状態を確かめるだけでは延ばさない(開きっぱなしのタブで閉まらなくなる)。"""
        self.lock.unlock()
        for _ in range(3):
            self.t += 300
            self.lock.is_unlocked()
        self.assertFalse(self.lock.is_unlocked())

    def test_the_process_has_one(self):
        admin_lock.reset()
        self.addCleanup(admin_lock.reset)
        self.assertIs(admin_lock.get(), admin_lock.get())


if __name__ == "__main__":
    unittest.main()
