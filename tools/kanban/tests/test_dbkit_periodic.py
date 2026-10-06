"""dbkit.periodic のテスト。"""

from __future__ import annotations

import threading
import time
import unittest

from dbkit.periodic import PeriodicTask


class RunOnceTest(unittest.TestCase):
    def test_run_once_returns_and_stores_result(self):
        task = PeriodicTask(run=lambda: 42, interval_sec=0)
        result = task.run_once()
        self.assertEqual(result, 42)
        self.assertEqual(task.last_result, 42)

    def test_run_once_notifies_on_result(self):
        received = []
        task = PeriodicTask(run=lambda: "ok", interval_sec=0, on_result=received.append)
        task.run_once()
        self.assertEqual(received, ["ok"])

    def test_exception_is_caught_and_previous_result_kept(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                return "first"
            raise RuntimeError("boom")

        task = PeriodicTask(run=flaky, interval_sec=0)
        self.assertEqual(task.run_once(), "first")
        result = task.run_once()
        # on_error未指定時は例外を握りつぶし、直前の結果を維持する
        self.assertEqual(result, "first")
        self.assertEqual(task.last_result, "first")

    def test_on_error_can_convert_exception_to_result(self):
        def always_fails():
            raise RuntimeError("boom")

        task = PeriodicTask(
            run=always_fails, interval_sec=0,
            on_error=lambda exc: f"failed: {exc}",
        )
        result = task.run_once()
        self.assertEqual(result, "failed: boom")

    def test_on_result_error_does_not_propagate(self):
        def bad_callback(_result):
            raise ValueError("callback exploded")

        task = PeriodicTask(run=lambda: 1, interval_sec=0, on_result=bad_callback)
        # on_result内の例外はログに残るだけで、run_once自体は例外を投げない
        result = task.run_once()
        self.assertEqual(result, 1)


class BackgroundThreadTest(unittest.TestCase):
    def test_start_runs_periodically(self):
        counter = {"n": 0}
        lock = threading.Lock()

        def bump():
            with lock:
                counter["n"] += 1
            return counter["n"]

        task = PeriodicTask(run=bump, interval_sec=0.05, name="test-periodic")
        task.start()
        try:
            time.sleep(0.5)
        finally:
            task.stop(timeout=5)
        self.assertGreaterEqual(counter["n"], 3)

    def test_request_now_triggers_immediate_run(self):
        counter = {"n": 0}

        def bump():
            counter["n"] += 1
            return counter["n"]

        task = PeriodicTask(run=bump, interval_sec=100, name="test-periodic-2")
        task.start()
        try:
            task.request_now()
            for _ in range(50):
                if counter["n"] >= 1:
                    break
                time.sleep(0.05)
            self.assertGreaterEqual(counter["n"], 1)
        finally:
            task.stop(timeout=5)

    def test_interval_zero_does_not_start_thread(self):
        task = PeriodicTask(run=lambda: 1, interval_sec=0, name="test-periodic-3")
        task.start()
        self.assertIsNone(task._thread)

    def test_stop_is_idempotent(self):
        task = PeriodicTask(run=lambda: 1, interval_sec=0.05, name="test-periodic-4")
        task.start()
        task.stop(timeout=5)
        task.stop(timeout=5)  # 2回目も例外を出さない


if __name__ == "__main__":
    unittest.main()
