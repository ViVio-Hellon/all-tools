"""コンソールの進み具合(start.bat の窓に出す)

ブラウザが開くまでのあいだ、利用者に見えているのは start.bat の黒い窓だけ。
何も出ないと「固まった」と見分けがつかないので、1行の進み具合を出す:

    [■■■■■■□□□□□□□□□□]  37%  3/8 Excel を確かめています  2秒

- 段が進むたびに棒が伸びる。段の中で待っているあいだも秒数が増える
  (止まって見えない)
- 窓に出しているとき(端末)だけ同じ行を書き換える。ファイルへ流している
  ときは1段1行で出す(ログとして読める)
- `pythonw`(Start.vbs)には窓が無い(`sys.stdout` が None)。何もしない
- 出しているあいだ、窓へのログは注意(WARNING)以上だけにする。INFO の行が
  棒の行に割り込んで崩れるため(ログのファイルには全部残る)。注意の行は
  棒を消してから出し、棒はその下に出し直す。終われば元に戻す
- 標準ライブラリだけ(Flask より前に使う)
"""
from __future__ import annotations

import logging
import sys
import threading
import time
import unicodedata
from typing import List, Optional, TextIO, Tuple

BAR_WIDTH = 16
TICK_SEC = 0.5
FULL, EMPTY = "■", "□"


def display_width(text: str) -> int:
    """窓の上での幅(全角は2)。書き換えるときに前の行を消しきるため。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WFA" else 1 for c in text)


def bar(ratio: float, width: int = BAR_WIDTH) -> str:
    filled = int(round(max(0.0, min(1.0, ratio)) * width))
    return "[" + FULL * filled + EMPTY * (width - filled) + "]"


class ConsoleProgress:
    """全 `total` 段の進み具合。`step()` で次の段へ、`finish()` で終わり。"""

    def __init__(self, total: int, stream: Optional[TextIO] = None, *,
                 live: Optional[bool] = None) -> None:
        self.total = max(1, total)
        self.stream = stream if stream is not None else sys.stdout
        if live is None:
            try:
                live = bool(self.stream is not None and self.stream.isatty())
            except (AttributeError, ValueError):
                live = False
        self.live = live
        self.index = 0
        self.label = ""
        self.started = time.monotonic()
        self.step_started = self.started
        self._width = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ticker: Optional[threading.Thread] = None
        self._quieted: List[Tuple[logging.Handler, int, logging.Filter]] = []

    # ---- 外から呼ぶ -----------------------------------------------
    def step(self, label: str) -> None:
        """次の段に入る。"""
        if self.live:
            self._quiet_console_logs()            # 途中で作られたログの出口も静かにする
        with self._lock:
            self.index = min(self.index + 1, self.total)
            self.label = label
            self.step_started = time.monotonic()
            if self.live:
                self._draw()
            else:
                self._write_line(f"[{self.index}/{self.total}] {label}")
        if self.live and self._ticker is None:
            self._ticker = threading.Thread(target=self._tick, name="console-progress", daemon=True)
            self._ticker.start()

    def finish(self, message: str = "") -> float:
        """100% にして行を閉じる。かかった秒数を返す。"""
        self._stop.set()
        self._restore_console_logs()
        elapsed = time.monotonic() - self.started
        text = message or "完了"
        with self._lock:
            if self.live:
                self.index = self.total
                self._draw(override=f"{bar(1.0)} 100%  {text}  ({elapsed:.1f}秒)")
                self._write("\n")
            else:
                self._write_line(f"{text} ({elapsed:.1f}秒)")
        return elapsed

    def stop(self, message: str = "") -> None:
        """途中で止める(失敗・合流)。棒はそのまま残して、次の行に `message` を出す。"""
        self._stop.set()
        self._restore_console_logs()
        with self._lock:
            if self.live:
                self._write("\n")
            if message:
                self._write_line(message)

    # ---- 中身 -------------------------------------------------------
    def line(self) -> str:
        done = (self.index - 1) / self.total if self.index else 0.0
        waited = int(time.monotonic() - self.step_started)
        tail = f"  {waited}秒" if waited >= 1 else ""
        return (f"{bar(done)} {int(done * 100):3d}%  {self.index}/{self.total} "
                f"{self.label}{tail}")

    def _tick(self) -> None:
        while not self._stop.wait(TICK_SEC):
            with self._lock:
                if not self._stop.is_set():
                    self._draw()

    def _draw(self, override: str = "") -> None:
        text = override or self.line()
        pad = max(0, self._width - display_width(text))
        self._width = display_width(text)
        self._write("\r" + text + " " * pad)

    def note(self, text: str) -> None:
        """棒の上に1行出す(棒は次の刻みで出し直す)。"""
        self.clear_line()
        with self._lock:
            self._write_line(text)
            if self.live and not self._stop.is_set():
                self._draw()

    def clear_line(self) -> None:
        """棒の行を消す(ほかの出力がその行に割り込む前に)。次の刻みで出し直す。"""
        with self._lock:
            if self.live and self._width and not self._stop.is_set():
                self._write("\r" + " " * self._width + "\r")
                self._width = 0

    def _quiet_console_logs(self) -> None:
        progress = self

        class _ClearFirst(logging.Filter):
            def filter(self, record: logging.LogRecord) -> bool:
                progress.clear_line()
                return True

        done = {id(h) for h, _, _ in self._quieted}
        # 出口はアプリのロガー("inspection")に付いている。どのロガーのものも見る
        loggers = [logging.getLogger()] + [lg for lg in list(logging.Logger.manager.loggerDict.values())
                                           if isinstance(lg, logging.Logger)]
        handlers = {id(h): h for lg in loggers for h in lg.handlers}
        for handler in handlers.values():
            if id(handler) in done:
                continue
            if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
                flt = _ClearFirst()
                self._quieted.append((handler, handler.level, flt))
                handler.setLevel(max(handler.level, logging.WARNING))
                handler.addFilter(flt)

    def _restore_console_logs(self) -> None:
        while self._quieted:
            handler, level, flt = self._quieted.pop()
            handler.removeFilter(flt)
            handler.setLevel(level)

    def _write_line(self, text: str) -> None:
        self._write(text + "\n")

    def _write(self, text: str) -> None:
        if self.stream is None:
            return
        try:
            try:
                self.stream.write(text)
            except UnicodeEncodeError:            # ■□ を出せない文字コードの窓
                self.stream.write(text.replace(FULL, "#").replace(EMPTY, "-")
                                  .encode("ascii", "replace").decode("ascii"))
            self.stream.flush()
        except (OSError, ValueError, UnicodeError):
            pass                                  # 窓が閉じられた・文字が出せない。起動は続ける
