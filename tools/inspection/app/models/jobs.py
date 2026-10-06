"""印刷ジョブのデータ構造。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ジョブの状態
WAITING = "waiting"      # Excel の空き待ち（プレビュー処理中など）
RUNNING = "running"
DONE = "done"
CANCELLED = "cancelled"
FAILED = "failed"        # Excel が起動できない等、ジョブ全体の失敗
ACTIVE_STATES = {WAITING, RUNNING}


@dataclass
class PrintItemResult:
    index: int            # 1 始まり（印刷順）
    item_id: str
    name: str
    label: str            # [カテゴリ - サブカテゴリ] 表示名
    status: str = "pending"   # pending / printing / ok / error / skipped
    code: str = ""
    message: str = ""
    detail: str = ""      # Excel / VBScript が返した原因(調べるための手がかり)
    sheet: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"index": self.index, "id": self.item_id, "name": self.name, "label": self.label,
                "status": self.status, "code": self.code, "message": self.message, "detail": self.detail,
                "sheet": self.sheet}


@dataclass
class PrintJob:
    job_id: str
    copies: int
    items: List[PrintItemResult]
    state: str = WAITING
    current_index: int = 0
    started_at: float = 0.0
    finished_at: Optional[float] = None
    cancel_requested: bool = False
    message: str = ""
    fatal_code: str = ""
    printer: str = ""
    screen_id: str = ""       # 始めた画面(同じPCで2つの画面から使うとき、誰の印刷かを示す)
    ref: str = ""             # 問い合わせ番号(印刷を受け付けた要求の番号。ログから引ける)
    log: List[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def succeeded(self) -> int:
        return sum(1 for i in self.items if i.status == "ok")

    @property
    def failed(self) -> int:
        return sum(1 for i in self.items if i.status == "error")

    def to_dict(self) -> Dict[str, Any]:
        current = self.items[self.current_index - 1] if 0 < self.current_index <= len(self.items) else None
        return {
            "job_id": self.job_id,
            "state": self.state,
            "active": self.state in ACTIVE_STATES,
            "copies": self.copies,
            "total": self.total,
            "current_index": self.current_index,
            "current_name": current.name if current else "",
            "current_label": current.label if current else "",
            "succeeded": self.succeeded,
            "failed": self.failed,
            "cancel_requested": self.cancel_requested,
            "message": self.message,
            "fatal_code": self.fatal_code,
            "printer": self.printer,
            "screen_id": self.screen_id,
            "ref": self.ref,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "items": [i.to_dict() for i in self.items],
        }
