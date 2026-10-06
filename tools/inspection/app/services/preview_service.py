"""点検表プレビュー（Excel の CopyPicture → 画像 → ブラウザ）。

VBA版 UFPreview.GeneratePreview と同じ判定を VBScript 側で行う:
    * 最初の表示可能なワークシートを対象にする
    * 印刷範囲が設定されていればその範囲（複数範囲なら1つ目）
    * 未設定なら UsedRange（最大 40 行 × 16 列）、取得できなければ A1:P40

画像はファイルの更新日時をキーにキャッシュするため、2回目以降は即座に表示され、
Excel を修正した場合は自動的に作り直される。

**複数のタブ・↓キーの押しっぱなし**でも、Excel とサーバの手を塞がないように:
    * 同じ画面から次のプレビューが来たら、前の要求は順番待ちをやめる
      (もう誰も見ない画像を Excel に作らせない)
    * 順番待ちは `MAX_WAITING` 件まで。それ以上は断る(状態の問い合わせや
      印刷の進み具合を返す手を残す。サーバの手は `server.py` の threads=8)
"""
from __future__ import annotations

import os
import threading
import time
from contextlib import ExitStack, contextmanager
from typing import Any, Callable, Dict, Iterator, Tuple

from app.repositories.preview_cache import PreviewCache
from app.services.excel_service import ExcelError, ExcelService
from app.errors import ApiError
from core import event_log


# Excel の順番を待てるプレビュー要求の数(作っている1件は数えない)
MAX_WAITING = 4


class PreviewService:
    def __init__(self, cfg: Any, inspection: Any, excel: ExcelService, cache: PreviewCache, logger: Any):
        self.cfg = cfg
        self.inspection = inspection
        self.excel = excel
        self.cache = cache
        self.log = logger
        self._lock = threading.Lock()
        self._seq = 0
        self._latest: Dict[str, int] = {}     # 画面の番号 → その画面の最新の要求
        self._waiters: Dict[int, Callable[[], bool]] = {}   # 順番待ちの要求 → 取りやめたか

    def _ticket(self, screen_id: str) -> Tuple[int, Callable[[], bool]]:
        """この要求の番号と、「同じ画面から次が来たか」を返す関数。"""
        with self._lock:
            self._seq += 1
            ticket = self._seq
            if not screen_id:
                return ticket, lambda: False
            self._latest[screen_id] = ticket
            if len(self._latest) > 64:         # 閉じた画面の分を溜めない
                for old in sorted(self._latest, key=self._latest.get)[:len(self._latest) - 64]:
                    del self._latest[old]
        return ticket, lambda: self._latest.get(screen_id, ticket) != ticket

    @contextmanager
    def _waiting_slot(self, ticket: int, superseded: Callable[[], bool]) -> Iterator[None]:
        """順番待ちに並ぶ。**取りやめた要求は数えない**(すぐ抜けるので、最新の要求を断らない)。"""
        with self._lock:
            if sum(1 for gone in self._waiters.values() if not gone()) >= MAX_WAITING:
                raise ApiError(409, "PREVIEW_QUEUE_FULL",
                               "プレビューの順番待ちが多いため受け付けられませんでした。"
                               "少し待ってから「もう一度」を押してください。")
            self._waiters[ticket] = superseded
        try:
            yield
        finally:
            with self._lock:
                self._waiters.pop(ticket, None)

    def _variant(self) -> str:
        e = self.cfg.excel
        return "|".join(str(v) for v in (self.excel.backend_name, e.preview_appearance, e.preview_scale,
                                         e.preview_max_pixels, e.preview_max_rows, e.preview_max_cols))

    def get_preview(self, item_id: str, force: bool = False, screen_id: str = "") -> Dict[str, Any]:
        ticket, superseded = self._ticket(screen_id)
        item = self.inspection.get_item(item_id)
        if item is None:
            raise ApiError(409, "ITEM_NOT_FOUND", "点検表が一覧に見つかりません。「再読込」してから選び直してください。")
        context = {"target": f"[{item.category} - {item.subcategory}] {item.name}", "file": item.path}
        try:
            stat = os.stat(item.path)
        except OSError as exc:
            raise ApiError(422, "FILE_NOT_FOUND", "対象の点検表ファイルが見つかりません。", item.file_name,
                           context={**context, "os_error": f"{exc.strerror} ({exc.errno})"})
        key = PreviewCache.make_key(item.path, stat.st_mtime_ns, stat.st_size, self._variant())
        if not force:
            meta = self.cache.get_meta(key)
            if meta:
                event_log.record("preview", event_log.OK, cached=True, **context)
                return self._reply(key, meta, cached=True)
        started = time.perf_counter()
        timeout = self.cfg.excel.preview_timeout_sec
        try:
            with ExitStack() as turn:
                with self._waiting_slot(ticket, superseded):   # 待つあいだだけ数える
                    turn.enter_context(self.excel.acquire("preview", timeout=timeout, give_up=superseded))
                if superseded():                  # 順番が来たときには、もう次に移っていた
                    raise ExcelError("SUPERSEDED")
                meta = None if force else self.cache.get_meta(key)  # 待っている間に他の要求が作った場合
                if meta:
                    event_log.record("preview", event_log.OK, cached=True, **context)
                    return self._reply(key, meta, cached=True)
                self.log.info("プレビュー生成開始: %s", item.path)
                image = self.excel.preview(item.path)
        except ExcelError as exc:
            if exc.code == "SUPERSEDED":
                # 画面はもう次の点検表を見ている。失敗ではないので記録は控えめに
                self.log.debug("プレビュー要求を取りやめ(次の要求あり): %s", item.file_name)
                raise ApiError(409, exc.code, exc.message)
            self.log.warning("プレビュー失敗: %s [%s] %s %s", item.path, exc.code, exc.message, exc.detail)
            status = 409 if exc.code == "EXCEL_BUSY" else 422
            raise ApiError(status, exc.code, exc.message, exc.detail or None,
                           context={**context, "force": force,
                                    "elapsed_ms": int((time.perf_counter() - started) * 1000),
                                    "excel_busy_with": self.excel.activity or ""})
        elapsed = int((time.perf_counter() - started) * 1000)
        try:
            meta = self.cache.put(key, image.data, image.mime, {
                "item_id": item.id, "name": item.name, "sheet": image.sheet, "range": image.range_address,
                "range_source": image.range_source, "method": image.method, "width": image.width,
                "height": image.height, "elapsed_ms": elapsed, "source_mtime": stat.st_mtime})
        except OSError as exc:
            self.log.warning("プレビュー画像を保存できません: %s %s", self.cache.dir, exc)
            raise ApiError(500, "CACHE_WRITE_FAILED",
                           "プレビュー画像を保存できませんでした。PCの空き容量を確認してください。", str(exc),
                           context={**context, "cache_dir": self.cache.dir})
        self.log.info("プレビュー生成完了: %s sheet=%s range=%s method=%s %dms", item.file_name, image.sheet,
                      image.range_address, image.method, elapsed)
        event_log.record("preview", event_log.OK, cached=False, elapsed_ms=elapsed, sheet=image.sheet,
                         range=image.range_address, range_source=image.range_source, method=image.method,
                         **context)
        return self._reply(key, meta, cached=False)

    @staticmethod
    def _reply(key: str, meta: Dict[str, Any], cached: bool) -> Dict[str, Any]:
        return {
            "ok": True,
            "key": key,
            "image_url": f"/api/preview/image/{key}",
            "cached": cached,
            "sheet": meta.get("sheet", ""),
            "range": meta.get("range", ""),
            "method": meta.get("method", ""),
            "width": meta.get("width", 0),
            "height": meta.get("height", 0),
            "generated_at": meta.get("created"),
            "elapsed_ms": meta.get("elapsed_ms", 0),
        }

    def get_image(self, key: str) -> Tuple[bytes, str]:
        found = self.cache.read(key)
        if not found:
            raise ApiError(404, "PREVIEW_NOT_FOUND", "プレビュー画像が見つかりません。もう一度表示してください。")
        return found
