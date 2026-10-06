"""一定間隔でコールバックを実行するだけの、業務に依存しないバックグラウンドスレッド。

Access への定期書き戻し・定期取り込みは、どのVBA移行プロジェクトでも
「同じ形のスレッド」を必要とする(間隔を置いて呼ぶ・今すぐ呼ばせる・
止める・最後の結果を覚えておく)。この繰り返し部分だけを切り出し、
「何を呼ぶか」はプロジェクト側の関数(例: ``sync.export_pending``)に
任せる。

    exporter = PeriodicTask(run=lambda: export_pending(store, access),
                            interval_sec=60, name="myapp-exporter")
    exporter.start()
    ...
    exporter.request_now()   # 次の周期を待たずに今すぐ実行
    exporter.stop()          # スレッド停止(実行中の分は完了を待つ)
"""
from __future__ import annotations

import threading
from typing import Callable, Generic, Optional, TypeVar

from .logging_utils import get_logger

log = get_logger("periodic")

T = TypeVar("T")


class PeriodicTask(Generic[T]):
    """``run`` を一定間隔で呼び続けるバックグラウンドスレッド。

    ``run`` の戻り値の型は問わない(呼び出し側の同期関数が返す結果
    オブジェクトをそのまま ``last_result`` / ``on_result`` へ渡す)。
    """

    def __init__(
        self,
        run: Callable[[], T],
        *,
        interval_sec: float,
        name: str = "periodic-task",
        on_result: Optional[Callable[[T], None]] = None,
        on_error: Optional[Callable[[BaseException], T]] = None,
    ) -> None:
        self.run_callback = run
        self.interval_sec = max(0.0, float(interval_sec))
        self.name = name
        self.on_result = on_result
        self.on_error = on_error
        """``run`` が例外を送出したときに呼ぶ変換関数。

        指定すると、その戻り値が ``last_result``/``on_result`` に使われ、
        スレッドは継続する(業務側の「失敗も結果オブジェクトとして
        表現したい」設計に合わせるため)。指定しない場合は例外をログに
        残すだけでスレッドを継続する(結果は更新しない)。
        """
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_result: Optional[T] = None

    def start(self) -> None:
        if self.interval_sec <= 0:
            log.info("%s: 間隔が0以下のため自動実行は無効です", self.name)
            return
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name=self.name, daemon=True)
        self._thread.start()
        log.info("%s: 開始 (間隔=%.0f秒)", self.name, self.interval_sec)

    def request_now(self) -> None:
        """次の周期を待たずに実行する(バックグラウンドスレッド上で)。"""
        self._wake.set()

    def stop(self, timeout: float = 30.0) -> None:
        """スレッドを止める。実行中の分がある場合は完了まで待つ。"""
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self._thread = None

    def run_once(self) -> T:
        """呼び出したスレッド上で同期的に1回だけ実行する。

        「更新」ボタンのような、ユーザー操作に応じて今すぐ結果が欲しい
        場面から直接呼ぶ(バックグラウンドスレッドの周期を待たない)。
        """
        try:
            result = self.run_callback()
        except Exception as exc:  # スレッドを落とさない
            log.exception("%s: 実行中に例外が発生しました", self.name)
            if self.on_error is not None:
                result = self.on_error(exc)
            else:
                return self.last_result  # type: ignore[return-value]
        self.last_result = result
        if self.on_result is not None:
            try:
                self.on_result(result)
            except Exception:
                log.exception("%s: 結果通知でエラー", self.name)
        return result

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=self.interval_sec)
            self._wake.clear()
            if self._stop.is_set():
                break
            self.run_once()
