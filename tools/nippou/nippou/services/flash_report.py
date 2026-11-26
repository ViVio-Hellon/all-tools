"""ライン速報 ── 保存のたびに、そのラインの今の直・今日の枚数を小さなファイルに書く(v4.54.0)

    日報にて枚数入力時、リアルタイムでライン毎に枚数カウントを把握したい
    → 案2: 速報ファイルを書く。速報ファイルを指定パスに吐けるように設定に追加してください
    (事務所のモニター all-lines-graph-analysis に欄を足して読む)
    出すもの: 今の直、今日の合計・目標との差 / 集計から除く作業者は数えない

【置き場所】設定・参照設定の「ライン速報の置き場所」。**空なら書かない。**
ファイルは `<置き場所>\\<ライン>.json`(UTF-8)。1ラインに1つで、保存のたびに上書きする。

    {
      "line": "L-1",                 ライン(正規の呼び名)
      "date": "2026-11-26",          報告日(3直は始まった日)
      "date_label": "2026年11月26日",
      "shift": "2直",                いま保存した直
      "shift_count": 120.0,          その直の枚数
      "shifts": {"1直": 140.0, "2直": 120.0},   その日の直ごとの枚数
      "day_total": 260.0,            その日の合計
      "target": 140.0,               1日の目標(ライン毎目標。無ければ null)
      "diff": 120.0,                 合計 - 目標(目標が無ければ null)
      "updated_at": "2026-11-26T16:05:12"   書いた時刻(このPCの時計)
    }

【数え方】集計と同じ(`summary.refresh_shift` が作った直の集計)。**集計から除く作業者・班の
ページは入らない**(テストの直も入らない)。数字はグラフ・集計CSVと同じになる。

【古い日で上書きしない】過去の日を直した・作り直したときは、ファイルの日より古ければ書かない。
**書けなくても保存は止めない**(記録に残すだけ)。
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from ..logging_setup import get_logger

log = get_logger("services.flash_report")

_DATE = re.compile(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})")


def _iso(label: str) -> Optional[date]:
    m = _DATE.search(str(label or ""))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def folder() -> Optional[Path]:
    from ..config import SETTINGS

    return SETTINGS.flash_report_dir


def path_for(line: str) -> Optional[Path]:
    base = folder()
    if base is None:
        return None
    from ..access_bridge.pusher import safe_line_name

    return base / f"{safe_line_name(line)}.json"


def _own_line() -> str:
    from . import day_csv

    return day_csv.owner_line()


def write(repo, work_date: str, line: str, shift: str) -> Optional[Path]:
    """その直を保存したあとに呼ぶ。書いたファイル(書かなければ None)。"""
    target_path = path_for(line)
    if target_path is None or not line or line != _own_line():
        return None                   # 置き場所が空・この端末のラインでない(管理者の直し)は書かない
    day = _iso(work_date)
    if day is None:
        return None
    try:
        if target_path.is_file():
            old = json.loads(target_path.read_text(encoding="utf-8"))
            old_day = date.fromisoformat(str(old.get("date", "")))
            if day < old_day:
                return None           # 古い日の直し・作り直しで、今日の速報を上書きしない
    except (OSError, ValueError, TypeError):
        pass                          # 読めない・壊れたファイルは書き直す
    from . import summary, targets

    shifts: dict[str, float] = {}
    for row in summary.shift_rows(repo, day, day, line):
        shifts[row.shift] = round(shifts.get(row.shift, 0.0) + row.sheet_count, 1)
    total = round(sum(shifts.values()), 1)
    try:
        goal = targets.of(line)
    except Exception:                                 # noqa: BLE001 - 目標が無くても書く
        goal = None
    body = {
        "line": line, "date": day.isoformat(), "date_label": work_date, "shift": shift,
        "shift_count": shifts.get(shift, 0.0), "shifts": shifts, "day_total": total,
        "target": goal, "diff": round(total - goal, 1) if goal else None,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = target_path.with_name(target_path.name + ".tmp")
        tmp.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(target_path)
    except OSError as exc:
        log.warning("ライン速報を書けませんでした(保存は済んでいます) %s: %s", target_path, exc)
        return None
    return target_path


def write_quietly(repo, work_date: str, line: str, shift: str) -> None:
    """`write` を、何が起きても保存を止めずに呼ぶ。"""
    try:
        write(repo, work_date, line, shift)
    except Exception:                                 # noqa: BLE001 - 速報で保存を止めない
        log.exception("ライン速報で予期しないエラー %s/%s/%s", work_date, line, shift)
