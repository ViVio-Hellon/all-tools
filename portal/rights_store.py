"""タブ表示権限の手元の写しと、この端末に出すタブの決定

共有の DB はいつも届くとは限らない(共有フォルダが落ちている日がある)。
**前回読めた中身を手元に写しておき**、届かない日はそれで決める(python-web-tools の
アクセス権限と同じ作り)。写しは `<手元の領域>\\data\\tab_rights_cache.json`。

読み直すのは:
- 起動したとき(待たせないよう裏で)
- 大きなタブの画面が訊いてきたとき、前に確かめてから 30 秒たっていれば
  (ファイルの大きさと更新時刻が変わっていなければ、中身は読まない)
- 大設定で書き換えたとき・「読み直す」を押したとき
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import app_config, shared_db, tab_rights
from .catalog import Catalog
from .identity import Identity
from .logging_utils import get_logger

log = get_logger("rights")

#: 共有を確かめ直す間隔(秒)。大きさと更新時刻を見るだけなので軽い
RECHECK_SEC = 30

STATE_OK = "ok"            # 表を読めた(空でもよい)
STATE_MISSING = "missing"  # 共有の DB に表が無い
STATE_NEVER = "never"      # まだ一度も読めていない


@dataclass
class SyncResult:
    reached: bool
    changed: bool = False
    state: str = STATE_NEVER
    message: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return {"reached": self.reached, "changed": self.changed, "state": self.state,
                "message": self.message, "error": self.error}


@dataclass
class Snapshot:
    state: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    source: str = ""
    imported_at: float = 0.0
    checked_at: float = 0.0
    last_error: str = ""


def cache_path() -> Path:
    return app_config.local_root() / "data" / "tab_rights_cache.json"


class Store:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._checked_at = 0.0
        self._last_error = ""
        self._syncing: Optional[threading.Thread] = None

    # -- 手元の写し --------------------------------------------------
    def _read_cache(self) -> dict[str, Any]:
        try:
            data = json.loads(cache_path().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_cache(self, data: dict[str, Any]) -> None:
        target = cache_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, target)

    def snapshot(self) -> Snapshot:
        data = self._read_cache()
        state = data.get("state") or STATE_NEVER
        # 置き場所を変えたら、前の場所の写しでは決めない
        if data.get("source") and data.get("source") != str(shared_db.location().path):
            state = STATE_NEVER
        return Snapshot(state=state, rows=list(data.get("rows") or []) if state == STATE_OK else [],
                        source=str(data.get("source") or ""),
                        imported_at=float(data.get("imported_at") or 0),
                        checked_at=self._checked_at, last_error=self._last_error)

    # -- 共有から読み直す ----------------------------------------------
    def sync(self, *, force: bool = False) -> SyncResult:
        """共有の表を読み直す(変わっていなければ読まない)。"""
        with self._sync_lock:
            self._checked_at = time.time()
            path = shared_db.location().path
            if not shared_db.reachable(path):
                self._last_error = f"共有の DB に届きません: {path}"
                log.info("%s(手元の写しで決めます)", self._last_error)
                return SyncResult(False, state=self.snapshot().state, error=self._last_error)
            cached = self._read_cache()
            try:
                stamp = list(shared_db.stamp(path))
            except OSError as exc:
                self._last_error = f"共有の DB を確かめられません: {exc}"
                return SyncResult(False, state=self.snapshot().state, error=self._last_error)
            if (not force and cached.get("source") == str(path)
                    and cached.get("stamp") == stamp and cached.get("state") in (STATE_OK, STATE_MISSING)):
                self._last_error = ""
                return SyncResult(True, False, cached["state"], "変わっていません")
            try:
                rows = shared_db.read_rows(path)
            except shared_db.SourceError as exc:
                self._last_error = str(exc)
                log.warning("タブ表示権限を読めませんでした: %s", exc)
                return SyncResult(True, state=self.snapshot().state, error=str(exc))
            state = STATE_MISSING if rows is None else STATE_OK
            self._write_cache({"source": str(path), "stamp": stamp, "state": state,
                               "imported_at": time.time(), "rows": rows or []})
            self._last_error = ""
            message = ("共有の DB にタブ表示権限の表がありません" if rows is None
                       else f"タブ表示権限を読みました({len(rows)} 行)")
            log.info("%s: %s", message, path)
            return SyncResult(True, True, state, message)

    def sync_in_background(self) -> None:
        """裏で読み直す(画面を待たせない)。読み直し中なら何もしない。"""
        with self._lock:
            if self._syncing is not None and self._syncing.is_alive():
                return
            self._syncing = threading.Thread(target=self._safe_sync, name="rights-sync", daemon=True)
            self._syncing.start()

    def _safe_sync(self) -> None:
        try:
            self.sync()
        except Exception:                         # noqa: BLE001 - 裏の読み直しで落とさない
            log.exception("タブ表示権限の読み直しで思わぬエラー")

    def wait_first(self, limit: float) -> None:
        """まだ一度も読めていなければ、少しだけ待つ(初めての起動)。"""
        if self.snapshot().state != STATE_NEVER:
            return
        thread = self._syncing
        if thread is not None:
            thread.join(limit)

    def maybe_recheck(self) -> None:
        if time.time() - self._checked_at >= RECHECK_SEC:
            self.sync_in_background()

    # -- 決める ------------------------------------------------------
    def rules(self) -> list[tab_rights.Rule]:
        return [tab_rights.Rule.from_row(r) for r in self.snapshot().rows]

    def decide(self, identity: Identity, catalog: Catalog) -> tab_rights.Decision:
        snap = self.snapshot()
        table_state = {STATE_OK: "ok", STATE_MISSING: "missing"}.get(snap.state, "unreadable")
        return tab_rights.decide([tab_rights.Rule.from_row(r) for r in snap.rows],
                                 identity, catalog, table_state=table_state)


store = Store()
