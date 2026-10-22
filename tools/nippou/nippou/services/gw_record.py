"""GW計算の記録 ── **どの版で・どの取得元の・いつ時点の資材マスタで計算したか** を残す (v4.44.0)

    GW計算にソフトの版、どの取得元の、いつ時点の資材マスタで計算したか まで残して下さい
    (coil-packing-tools のペナラベルがその仕組みを使っているので参照)

ペナラベルの「計算・印刷の記録」(`work_record.py`・`material_repo.describe`)と同じ考え方です。
計算・印刷のたびに1行ずつ、**月ごとのファイルへ足します**(JSON Lines。足すだけで、消さない・書き換えない):

    この端末   <ローカル領域>/data/GW計算記録/GW計算記録_YYYY-MM.jsonl          (いつも)
    出力先     <集計CSV・印刷用HTMLの出力先>/<ライン>/GW計算記録/GW計算記録_<PC名>_YYYY-MM.jsonl
               (設定で出力先を決めてあるときだけ。PC ごとにファイルを分けるので取り合わない)

1行に残すもの:

- **版**: 日報管理ツールの版・日報複合ツールの版(一式の中で動いているとき)・画面の中身の印
- **資材マスタの素性**: 取得元(sqlite3 / Access)・ファイルの場所・**ファイルの更新日時(いつ時点のマスタか)**・
  大きさ・読んだ時刻。VC重量も同じファイルの表
- **使った値**: 資材ごとの単位質量・係数(マスタが後で直されても、その計算に何を使ったかが分かる)
- **誰が**: PC 名・ライン・Windows の利用者名
- 何を計算したか(LotNo・オーダーNo・包装仕様・梱包資材重量・風袋総重量・GW)

**書けなくても計算・印刷は止めません**(理由はログと記録の画面に出します)。
"""
from __future__ import annotations

import getpass
import json
import os
import socket
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ..logging_setup import get_logger

log = get_logger("services.gw_record")

FILE_PREFIX = "GW計算記録"
FOLDER = "GW計算記録"

#: 記録の種類
KIND_CALC = "計算"
KIND_PRINT = "印刷画面"

_lock = threading.Lock()
#: 最後に書けなかった理由(記録の画面に出す)
_problems: dict[str, str] = {"local": "", "shared": ""}

_STAMP = "%Y-%m-%d %H:%M:%S"


# ----------------------------------------------------------------------
# 素性
# ----------------------------------------------------------------------
def pc_name() -> str:
    name = os.environ.get("COMPUTERNAME") or socket.gethostname() or "PC"
    from ..file_names import UNSAFE_OR_SPACE
    return UNSAFE_OR_SPACE.sub("_", name).strip("_") or "PC"


def user_name() -> str:
    try:
        return os.environ.get("USERNAME") or getpass.getuser() or ""
    except Exception:                                   # noqa: BLE001 - 名前が引けない端末
        return ""


def who(line: str = "") -> dict[str, str]:
    """どの PC(ライン)・誰の計算か。"""
    return {"pc": pc_name(), "line": str(line or "").strip(), "user": user_name()}


def integrated_version(tool_dir: Optional[Path] = None) -> str:
    """日報複合ツールの一式の中で動いていれば、その版(`<一式>/config/app.json`)。違えば空。"""
    from .. import app_config

    here = Path(tool_dir) if tool_dir else app_config.APP_ROOT
    root = here.parent.parent
    if here.parent.name != "tools" or not (root / "config" / "tools.json").is_file():
        return ""
    try:
        return str(json.loads((root / "config" / "app.json").read_text(encoding="utf-8")).get("version", ""))
    except (OSError, ValueError):
        return ""


def versions(stamp: str = "") -> dict[str, str]:
    """ソフトの版。`stamp` は画面の中身の印(`app.config["STATIC_STAMP"]`)。"""
    from .. import app_config

    out = {"nippou": app_config.version()}
    whole = integrated_version()
    if whole:
        out["app"] = whole
    if stamp:
        out["stamp"] = stamp
    return out


def master_source(path: Path, *, read_at: Optional[datetime] = None) -> dict[str, Any]:
    """資材マスタの素性: 取得元・場所・**いつ時点か(ファイルの更新日時)**・大きさ・読んだ時刻。"""
    from .. import source_db
    from ..config import SETTINGS

    path = Path(path)
    if path.suffix.lower() in source_db.SUFFIXES:
        kind = "sqlite3"
    elif SETTINGS.access_backend == "odbc":
        kind = "Access(ODBC)"
    else:
        kind = "Access"
    info: dict[str, Any] = {"source": kind, "path": str(path), "asOf": "", "size": None,
                            "readAt": (read_at or datetime.now()).strftime(_STAMP)}
    try:
        st = path.stat()
        info["asOf"] = datetime.fromtimestamp(st.st_mtime).strftime(_STAMP)
        info["size"] = st.st_size
    except OSError as exc:
        info["problem"] = f"ファイルの日時を読めません: {exc}"
    return info


def label(material: dict) -> str:
    """「sqlite3 \\\\srv\\共有\\梱包資材マスタ.sqlite3(2026-10-20 14:03:11 時点)」の形。"""
    if not material:
        return "(記録なし)"
    when = material.get("asOf") or "日時不明"
    return f"{material.get('source', '')} {material.get('path', '')}({when} 時点)"


def version_label(version: dict) -> str:
    """「日報 4.44.0 / 日報複合ツール 1.5.50」の形。"""
    if not version:
        return "(記録なし)"
    parts = [f"日報 {version['nippou']}" if version.get("nippou") else "",
             f"日報複合ツール {version['app']}" if version.get("app") else ""]
    return " / ".join(p for p in parts if p) or "(記録なし)"


# ----------------------------------------------------------------------
# 書く
# ----------------------------------------------------------------------
def _month_file(folder: Path, when: datetime, *, pc: str = "") -> Path:
    middle = f"_{pc}" if pc else ""
    return folder / f"{FILE_PREFIX}{middle}_{when:%Y-%m}.jsonl"


def local_folder() -> Path:
    from .. import app_config
    return app_config.local_dir("data") / FOLDER


def shared_folder(line: str) -> Optional[Path]:
    """出力先(集計CSV・印刷用HTML)を設定で決めてあれば、その <ライン>/GW計算記録。無ければ None。"""
    from .. import user_settings
    from ..config import KEY_REPORT_OUT_DIR, SETTINGS

    if not str(user_settings.get(KEY_REPORT_OUT_DIR, "") or "").strip():
        return None
    from ..file_names import safe_name
    name = safe_name(line, "ライン未設定")
    return Path(SETTINGS.report_output_dir) / name / FOLDER


def append(entry: dict[str, Any], *, line: str = "", when: Optional[datetime] = None) -> dict[str, str]:
    """1行足す。**書けなくても止めない**(返すのは 場所 → 書けなかった理由。書けたら空)。"""
    when = when or datetime.now()
    text = json.dumps(entry, ensure_ascii=False) + "\n"
    targets = [("local", _month_file(local_folder(), when))]
    shared = shared_folder(line)
    if shared is not None:
        targets.append(("shared", _month_file(shared, when, pc=pc_name())))
    result: dict[str, str] = {}
    with _lock:
        for key, path in targets:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(text)
                _problems[key] = ""
                result[key] = ""
            except OSError as exc:
                _problems[key] = f"{path}: {exc}"
                result[key] = _problems[key]
                log.warning("GW計算の記録を書けませんでした(%s): %s", key, exc)
    return result


def record(kind: str, *, material: dict, rates: dict, subject: dict, results: dict,
           line: str = "", stamp: str = "", extra: Optional[dict] = None) -> dict[str, Any]:
    """計算・印刷の記録を1行残す。残した中身を返す(画面・紙にも同じものを出す)。"""
    now = datetime.now()
    entry: dict[str, Any] = {
        "at": now.strftime(_STAMP), "kind": kind,
        "version": versions(stamp), "who": who(line),
        "material": material, "rates": rates,
        "subject": subject, "results": results,
    }
    if extra:
        entry.update(extra)
    try:
        append(entry, line=line, when=now)
        log.info("[GW計算の記録] %s %s 資材マスタ=%s %s", kind,
                 subject.get("lot_no") or subject.get("order_no") or "", label(material),
                 version_label(entry["version"]))
    except Exception:                                   # noqa: BLE001 - 計算・印刷は止めない
        log.exception("GW計算の記録を残せませんでした")
    return entry


# ----------------------------------------------------------------------
# 読む
# ----------------------------------------------------------------------
def recent(limit: int = 300) -> list[dict[str, Any]]:
    """この端末の記録を新しい順に(今月と先月のファイル)。読めない行は飛ばす。"""
    folder = local_folder()
    try:
        files = sorted(folder.glob(f"{FILE_PREFIX}_*.jsonl"), reverse=True)[:2]
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for text in reversed(lines):
            try:
                rows.append(json.loads(text))
            except ValueError:
                continue
            if len(rows) >= limit:
                return rows
    return rows


def status(line: str = "") -> dict[str, str]:
    """どこへ書いているか・書けていないか(記録の画面に出す)。"""
    shared = shared_folder(line)
    return {"local": str(local_folder()), "localProblem": _problems["local"],
            "shared": str(shared) if shared else "", "sharedProblem": _problems["shared"]}
