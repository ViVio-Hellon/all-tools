"""印刷（Excel の Worksheet.PrintOut）。

VBA版 PrintWorkbooksFromFolder / DoPrint との対応:
    * 印刷部数: 数値なら整数化して 1～100 に丸める。数値でなければエラー
    * 各ブックを読み取り専用で開き、最初の表示シートだけを PrintOut
    * 印刷設定（範囲・用紙・向き・余白・改ページ・ヘッダー/フッター）は Excel 側を正とし、
      Python 側では一切変更しない
追加:
    * 選択した順番に印刷し、「2 / 5 点検表Bを印刷しています」の進捗を返す
    * 印刷中は二重実行を受け付けない
    * 「中止」は現在の1件が終わった後で止まる
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from app.models.jobs import (ACTIVE_STATES, CANCELLED, DONE, FAILED, RUNNING, WAITING, PrintItemResult,
                             PrintJob)
from app.services.excel_service import ExcelError, ExcelService, message_for
from app.errors import ApiError
from core import event_log

MIN_COPIES = 1
MAX_COPIES = 100
COPIES_ERROR = "印刷部数は1～100の数値で入力してください。"


def parse_copies(raw: Any, max_copies: int = MAX_COPIES) -> int:
    """VBA版と同じ: IsNumeric → CInt（銀行丸め）→ 1～max_copies に丸める。"""
    if isinstance(raw, bool):
        raise ValueError(COPIES_ERROR)
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        raise ValueError(COPIES_ERROR)
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError(COPIES_ERROR)
    copies = int(round(value))
    return max(MIN_COPIES, min(max_copies, copies))


class PrintService:
    def __init__(self, inspection: Any, excel: ExcelService, printer_provider: Any, logger: Any,
                 max_copies: int = MAX_COPIES):
        self.max_copies = max_copies
        self.inspection = inspection
        self.excel = excel
        self.printer_provider = printer_provider
        self.log = logger
        self._lock = threading.Lock()
        self._job: Optional[PrintJob] = None

    def start(self, item_ids: List[str], copies_raw: Any, screen_id: str = "") -> Dict[str, Any]:
        try:
            copies = parse_copies(copies_raw, self.max_copies)
        except ValueError as exc:
            raise ApiError(400, "INVALID_COPIES", str(exc))
        ids = [str(i) for i in item_ids if i]
        if not ids:
            raise ApiError(422, "NO_SELECTION", "印刷する点検表を選択してください。")
        items: List[PrintItemResult] = []
        missing: List[str] = []
        paths: List[str] = []
        for index, item_id in enumerate(ids, 1):
            item = self.inspection.get_item(item_id)
            if item is None:
                missing.append(item_id)
                continue
            items.append(PrintItemResult(index=len(items) + 1, item_id=item.id, name=item.name,
                                         label=f"[{item.category} - {item.subcategory}] {item.name}"))
            paths.append(item.path)
        if missing:
            raise ApiError(409, "ITEM_NOT_FOUND",
                            f"一覧に見つからない点検表が {len(missing)} 件あります。「再読込」してから選び直してください。")
        with self._lock:
            if self._job is not None and self._job.state in ACTIVE_STATES:
                running = self._job
                where = "ほかの画面で" if running.screen_id and running.screen_id != screen_id else ""
                # 1件目に入る前(Excel の空き待ち・起動中)は「0件目」と言わない
                now = (f"印刷中です({running.current_index}/{running.total} 件目)"
                       if running.current_index > 0 else f"印刷の準備中です(全 {running.total} 件)")
                raise ApiError(409, "PRINT_RUNNING", f"{where}{now}。終わってから印刷してください。")
            job = PrintJob(job_id=uuid.uuid4().hex[:12], copies=copies, items=items, started_at=time.time(),
                           printer=self.printer_provider() or "", screen_id=screen_id,
                           ref=event_log.current_ref.get() or event_log.new_ref())
            self._job = job
        self.log.info("印刷開始 job=%s 件数=%d 部数=%d プリンター=%s", job.job_id, job.total, copies, job.printer)
        for item, path in zip(items, paths):
            self.log.info("  %d. %s (%s)", item.index, item.label, path)
        event_log.record("print.start", event_log.INFO, ref=job.ref, job=job.job_id, total=job.total,
                         copies=copies, printer=job.printer or "(既定のプリンターを取得できず)",
                         targets=[i.label for i in items], screen=screen_id)
        threading.Thread(target=self._run, args=(job, paths), name=f"print-{job.job_id}", daemon=True).start()
        return self.status()

    def _run(self, job: PrintJob, paths: List[str]) -> None:
        def on_event(kind: str, index: int, ok: bool = False) -> None:
            with self._lock:
                item = job.items[index - 1]
                if kind == "begin":
                    job.current_index = index
                    item.status = "printing"
                    job.message = f"{item.name}を印刷しています"
                elif kind == "end":
                    item.status = "ok" if ok else "error"

        def should_continue() -> bool:
            return not job.cancel_requested

        try:
            with self.excel.acquire("print", timeout=600):
                with self._lock:
                    job.state = RUNNING
                    job.message = "Excelを起動しています..."
                outcomes = self.excel.print_files(paths, job.copies, on_event, should_continue)
        except ExcelError as exc:
            with self._lock:
                job.state = FAILED
                job.fatal_code = exc.code
                job.message = exc.message
                for item in job.items:
                    if item.status in ("pending", "printing"):
                        item.status, item.code, item.message = "error", exc.code, exc.message
                        item.detail = exc.detail or ""
                job.finished_at = time.time()
            self.log.error("印刷失敗 job=%s [%s] %s %s", job.job_id, exc.code, exc.message, exc.detail)
            self._record_end(job, paths, code=exc.code, message=exc.message, detail=exc.detail or "")
            return
        except Exception as exc:  # noqa: BLE001
            self.log.exception("印刷処理で予期しないエラー job=%s [ref=%s]", job.job_id, job.ref)
            with self._lock:
                job.state = FAILED
                job.fatal_code = "INTERNAL_ERROR"
                job.message = message_for("PRINT_FAILED")
                job.finished_at = time.time()
            self._record_end(job, paths, code="INTERNAL_ERROR", message=job.message,
                             detail=f"{type(exc).__name__}: {exc}")
            return

        with self._lock:
            for outcome, item in zip(outcomes, job.items):
                item.sheet = outcome.sheet
                if outcome.ok:
                    item.status, item.code, item.message = "ok", "", ""
                elif outcome.code == "STOPPED":
                    item.status, item.code, item.message = "skipped", outcome.code, outcome.message
                else:
                    item.status, item.code = "error", outcome.code or "PRINT_FAILED"
                    item.message = outcome.message or message_for(item.code)
                item.detail = outcome.detail
            job.finished_at = time.time()
            if job.cancel_requested and any(i.status == "skipped" for i in job.items):
                job.state = CANCELLED
                job.message = "印刷を中止しました"
            else:
                job.state = DONE
                job.message = "印刷完了" if job.failed == 0 else "一部の点検表を印刷できませんでした"
            summary = (job.job_id, job.succeeded, job.failed, job.state, job.finished_at - job.started_at)
        self.log.info("印刷終了 job=%s 成功=%d 失敗=%d 状態=%s %.1f秒", *summary)
        for item, outcome in zip(job.items, outcomes):
            if item.status == "error":
                self.log.warning("  印刷エラー %d. %s [%s] %s", item.index, item.label, item.code, outcome.detail)
        self._record_end(job, paths)

    def _record_end(self, job: PrintJob, paths: List[str], *, code: str = "", message: str = "",
                    detail: str = "") -> None:
        """1件ずつの結果と、印刷全体の結果を残す(なぜなぜ分析用)。"""
        try:
            for item, path in zip(job.items, paths):
                if item.status == "pending":
                    continue
                result = {"ok": event_log.OK, "skipped": event_log.INFO}.get(item.status, event_log.NG)
                event_log.record(
                    "print.item", result, ref=job.ref, job=job.job_id, index=item.index,
                    target=item.label, file=path, sheet=getattr(item, "sheet", ""), copies=job.copies,
                    printer=job.printer or "(既定のプリンターを取得できず)", code=item.code if result != event_log.OK else "",
                    message=item.message if result != event_log.OK else "",
                    detail=item.detail if result != event_log.OK else "", screen=job.screen_id)
            ok = job.state == DONE and job.failed == 0
            event_log.record(
                "print.end", event_log.OK if ok else (event_log.INFO if job.state == CANCELLED else event_log.NG),
                ref=job.ref, job=job.job_id, state=job.state, succeeded=job.succeeded, failed=job.failed,
                total=job.total, elapsed_ms=int(((job.finished_at or time.time()) - job.started_at) * 1000),
                printer=job.printer, code=code or job.fatal_code, message=message or (job.message if not ok else ""),
                detail=detail, screen=job.screen_id)
        except Exception:                          # noqa: BLE001 - 記録の失敗で印刷を止めない
            self.log.exception("印刷結果を記録できませんでした job=%s", job.job_id)

    def cancel(self) -> Dict[str, Any]:
        with self._lock:
            if self._job is None or self._job.state not in ACTIVE_STATES:
                raise ApiError(409, "NOT_RUNNING", "実行中の印刷はありません。")
            self._job.cancel_requested = True
            self._job.message = "中止しています（現在の1件が終わると止まります）..."
        self.log.info("印刷の中止要求 job=%s", self._job.job_id)
        return self.status()

    def status(self) -> Dict[str, Any]:
        with self._lock:
            if self._job is None:
                return {"state": "idle", "active": False}
            return self._job.to_dict()

    def busy_labels(self) -> List[str]:
        """停止してよいかの判断に使う(`/api/shutdown`・自動終了)。"""
        return ["印刷"] if self.is_busy() else []

    def is_busy(self) -> bool:
        with self._lock:
            return self._job is not None and self._job.state in ACTIVE_STATES

    def wait_idle(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while self.is_busy():
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.2)
        return True
