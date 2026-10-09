"""同期の状態と「いま同期」の重なり (calendar_app/sync_service.py)

- 画面が状態を見に来ても、同期が覚えた「前の状態」を書き換えない(写しを返す)
- 「いま同期」が背景の同期と重なっても、頼みを黙って捨てて「同期しました」と答えない
  (走っている 1 回が終わったあとに、もう 1 回回り終えるまで待つ)
"""
from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

from calendar_app import sync_service
from calendar_app.sync.autosync import SyncState, SyncStatus


class FakeAuto:
    enabled = True

    def __init__(self, delay: float = 0.0) -> None:
        self.calls = 0
        self.delay = delay
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def sync_once(self, receive: bool = True) -> SyncStatus:
        self.calls += 1
        self.started.set()
        self.release.wait(5)
        time.sleep(self.delay)
        return SyncStatus(state=SyncState.SYNCED, message=f"{self.calls} 回目")


def make_service(auto: FakeAuto) -> sync_service.SyncService:
    with mock.patch.object(sync_service.SyncService, "reload"):
        service = sync_service.SyncService()
    service._auto = auto
    service._path = "x.sqlite3"
    service._folder = "share"
    return service


class StatusCopyTests(unittest.TestCase):
    def test_状態は写しで返し_持っている状態を書き換えない(self) -> None:
        service = make_service(FakeAuto())
        with mock.patch.object(service, "_pending_count", return_value=3):
            shown = service.status()
        self.assertEqual(shown.pending, 3)
        shown.state = SyncState.OFFLINE
        self.assertIsNot(service._status, shown)
        self.assertEqual(service._status.pending, 0, "見に来ただけで持っている状態を書き換えた")

    def test_同期の最中に見に来ても前の状態は変わらない(self) -> None:
        auto = FakeAuto()
        auto.release.clear()
        service = make_service(auto)
        seen = []
        with mock.patch.object(sync_service, "_note_change", side_effect=lambda b, a: seen.append((b, a))), \
                mock.patch.object(service, "_pending_count", return_value=7):
            worker = threading.Thread(target=service._run_one, args=(True,))
            worker.start()
            auto.started.wait(5)
            service.status()                      # 同期の最中に画面が見に来る
            auto.release.set()
            worker.join(5)
        before, after = seen[0]
        self.assertEqual(before.pending, 0, "同期が覚えた前の状態を、見に来た側が書き換えた")
        self.assertEqual(after.message, "1 回目")


class RunNowTests(unittest.TestCase):
    def test_背景の同期と重なったら_もう1回回り終えるまで待つ(self) -> None:
        auto = FakeAuto()
        auto.release.clear()
        service = make_service(auto)
        self.assertTrue(service.request(receive=True))   # 背景の同期が走り出す
        auto.started.wait(5)
        result = []
        waiter = threading.Thread(target=lambda: result.append(service.run_now(receive=True)))
        waiter.start()
        time.sleep(0.2)
        self.assertEqual(result, [], "背景の同期が終わる前に返した")
        auto.release.set()
        waiter.join(5)
        self.assertEqual(result, [True])
        self.assertEqual(auto.calls, 2, "頼んだ同期が回っていない(背景の 1 回だけで「同期しました」)")

    def test_待ちきれなければFalse(self) -> None:
        auto = FakeAuto()
        auto.release.clear()
        service = make_service(auto)
        service.request(receive=True)
        auto.started.wait(5)
        self.assertFalse(service.run_now(receive=True, wait_sec=0.2))
        auto.release.set()
        deadline = time.monotonic() + 5
        while service.is_busy() and time.monotonic() < deadline:
            time.sleep(0.05)

    def test_走っていなければその場で回す(self) -> None:
        auto = FakeAuto()
        service = make_service(auto)
        self.assertTrue(service.run_now(receive=False))
        self.assertEqual(auto.calls, 1)
        self.assertFalse(service.is_busy())


if __name__ == "__main__":
    unittest.main()
