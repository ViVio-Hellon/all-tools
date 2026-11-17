"""全ラインの集計を作り直す ── 管理者のPCが頼み、各ラインのPCが作る(v4.52.0)

    「集計を作り直す」は管理者PCで全ライン管理できないのですか？ → 作ってください

日報の明細と集計CSVを「直ぜんぶ」で持っているのはそのラインのPCだけ(`day_csv`)。
なので管理者のPCは**頼みを置くだけ**で、作り直すのは各ラインのPCです。

【置き場所】控えの置き場所(`LocalBackup`)の

    集計の作り直し.json            頼み  {"id", "start", "end", "by", "at"}
    <ライン>/集計の作り直し済み.json  そのラインのPCが終えた印 {"id", "at", "shifts", "error"}

【ラインのPC】1分タイマー(`/api/entry/close`)で見て、まだ終えていない頼みなら、自分のラインの
期間ぶんの集計を作り直し、集計CSVも書き直して、終えた印を書く(動いていれば5分以内、
止まっていれば次に起動して画面が開いたとき)。
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from ..logging_setup import get_logger

log = get_logger("services.rebuild_all")

REQUEST_FILE = "集計の作り直し.json"
DONE_FILE = "集計の作り直し済み.json"
#: ラインのPCが頼みを見に行く間隔(秒)
CHECK_EVERY_SEC = 300.0
_checked_at = 0.0


def _root() -> Path:
    from . import local_backup

    return local_backup.root()


def _read(path: Path) -> Optional[dict[str, Any]]:
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError) as exc:
        log.warning("%s を読めませんでした: %s", path, exc)
        return None


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def request(start: date, end: date, by: str = "") -> dict[str, Any]:
    """管理者のPC: 頼みを置く(前の頼みは置き換える)。"""
    from . import local_backup

    if not local_backup.reachable():
        raise OSError("控えの置き場所に届きません(設定・参照設定の「日報入力データの控えの置き場所」)")
    data = {"id": f"{time.time():.3f}", "start": start.isoformat(), "end": end.isoformat(),
            "by": by, "at": _now()}
    _root().mkdir(parents=True, exist_ok=True)
    _write(_root() / REQUEST_FILE, data)
    log.info("全ラインの集計の作り直しを頼みました %s〜%s (%s)", data["start"], data["end"], by)
    return data


def status() -> dict[str, Any]:
    """管理者の画面: いまの頼みと、ラインごとの終わったか。"""
    from . import local_backup

    req = _read(_root() / REQUEST_FILE)
    lines = []
    for line in local_backup.lines_available():
        done = _read(_root() / local_backup._safe(line) / DONE_FILE) or {}
        finished = bool(req) and done.get("id") == req.get("id")
        lines.append({"line": line, "done": finished, "at": done.get("at", "") if finished else "",
                      "shifts": done.get("shifts", 0) if finished else 0,
                      "error": done.get("error", "") if finished else ""})
    return {"request": req, "lines": lines}


def run_if_asked(repo, line: str, *, force: bool = False) -> Optional[str]:
    """ラインのPC: まだ終えていない頼みがあれば、自分のラインを作り直す。言うことを返す。"""
    global _checked_at
    from . import local_backup

    now = time.monotonic()
    if not force and _checked_at and now - _checked_at < CHECK_EVERY_SEC:
        return None
    _checked_at = now
    if not line or not local_backup.reachable():
        return None
    req = _read(_root() / REQUEST_FILE)
    if not req:
        return None
    done_path = _root() / local_backup._safe(line) / DONE_FILE
    if (_read(done_path) or {}).get("id") == req.get("id"):
        return None
    mark: dict[str, Any] = {"id": req.get("id"), "at": "", "shifts": 0, "error": ""}
    try:
        start, end = date.fromisoformat(req["start"]), date.fromisoformat(req["end"])
        from . import agg_exclude, day_csv
        from . import summary as summary_service

        agg_exclude.load(force=True)
        result = summary_service.rebuild(repo, start, end, line=line)
        keys = repo.shift_keys_between(start, end, line=line)
        for note in day_csv.settle(repo, keys, factors=False):
            log.info("全ラインの作り直し: %s", note)
        mark["shifts"] = result.shifts
    except Exception as exc:                      # noqa: BLE001 - 印に残して管理者に見せる
        log.exception("頼まれた集計の作り直しに失敗しました")
        mark["error"] = str(exc)
    mark["at"] = _now()
    try:
        _write(done_path, mark)
    except OSError as exc:
        log.warning("作り直し済みの印を書けませんでした(次の回にやり直します): %s", exc)
        _checked_at = 0.0
        return None
    msg = (f"{line}: 頼まれた集計の作り直しをしました({mark['shifts']}直)" if not mark["error"]
           else f"{line}: 頼まれた集計の作り直しに失敗しました: {mark['error']}")
    log.info(msg)
    return msg


def reset() -> None:
    global _checked_at
    _checked_at = 0.0
