"""GW計算の記録 ── **どの版で・どの取得元の・いつ時点の資材マスタで計算したか** を残す (v4.44.0)

    GW計算にソフトの版、どの取得元の、いつ時点の資材マスタで計算したか まで残して下さい
    (coil-packing-tools のペナラベルがその仕組みを使っているので参照)

ペナラベルの「計算・印刷の記録」(`work_record.py`・`material_repo.describe`)と同じ考え方です。
計算・印刷のたびに1行ずつ、**月ごとのファイルへ足します**(足すだけで、消さない・書き換えない):

    この端末   <ローカル領域>/data/GW計算記録/GW計算記録_YYYY-MM.csv          (いつも)
    出力先     <集計CSV・印刷用HTMLの出力先>/<ライン>/GW計算記録/GW計算記録_<PC名>_YYYY-MM.csv
               (設定で出力先を決めてあるときだけ。PC ごとにファイルを分けるので取り合わない)

**形式は選べます**(設定・管理者。v4.44.1): **CSV(既定。Excel でそのまま開ける)**・JSON Lines(ほかの仕組みで
読むとき)・両方。CSV は UTF-8 の BOM 付き(日報のほかの CSV と同じ)で、新しいファイルは見出しから始まります。

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
#: 記録の形式(設定・管理者で選ぶ。v4.44.1)。**既定は CSV**(Excel でそのまま開ける)
FORMAT_CSV = "csv"
FORMAT_JSONL = "jsonl"
FORMAT_BOTH = "both"
FORMATS: dict[str, str] = {
    FORMAT_CSV: "CSV(Excel で開ける)",
    FORMAT_JSONL: "JSON Lines(ほかの仕組みで読むとき)",
    FORMAT_BOTH: "両方",
}
DEFAULT_FORMAT = FORMAT_CSV

#: CSV の列(見出し, 記録の中身から取り出すもの)。**並びは見る人の順**(何を → 結果 → 誰が → 版 → 資材マスタ)
CSV_COLUMNS: tuple[str, ...] = (
    "時刻", "種類", "LotNo", "オーダーNo", "包装仕様", "梱包数",
    "梱包資材重量(kg)", "風袋総重量(kg)", "GW(kg)",
    "PC", "ライン", "利用者",
    "日報の版", "日報複合ツールの版", "画面の中身の印",
    "資材マスタの取得元", "資材マスタの場所", "資材マスタの時点", "資材マスタを読んだ時刻",
    "使った値(単位質量×係数)",
)


def record_format() -> str:
    """いまの記録の形式(`KEY_GW_RECORD_FORMAT`)。読めない値は既定(CSV)。"""
    from .. import user_settings
    from ..config import KEY_GW_RECORD_FORMAT

    value = str(user_settings.get(KEY_GW_RECORD_FORMAT, DEFAULT_FORMAT) or "").strip().lower()
    return value if value in FORMATS else DEFAULT_FORMAT


def rates_text(rates: dict) -> str:
    """「ダンプレート 0.25×1.0・外装紙 0.0842×1.0」の形。"""
    return "・".join(f"{name} {v.get('単位質量')}×{v.get('係数')}" for name, v in (rates or {}).items())


def _number(value: Any) -> str:
    return "" if value is None or value == "" else f"{float(value):.2f}"


def csv_row(entry: dict[str, Any]) -> list[str]:
    """記録1件を CSV の1行に(`CSV_COLUMNS` の並び)。"""
    subject = entry.get("subject") or {}
    results = entry.get("results") or {}
    who_ = entry.get("who") or {}
    version = entry.get("version") or {}
    material = entry.get("material") or {}
    return [
        entry.get("at", ""), entry.get("kind", ""),
        subject.get("lot_no", ""), subject.get("order_no", ""), subject.get("pack_spec_no", ""),
        str(entry.get("packs") or ""),
        _number(results.get("material_total")), _number(results.get("tare_weight")),
        _number(results.get("gross_weight")),
        who_.get("pc", ""), who_.get("line", ""), who_.get("user", ""),
        version.get("nippou", ""), version.get("app", ""), version.get("stamp", ""),
        material.get("source", ""), material.get("path", ""), material.get("asOf", ""),
        material.get("readAt", ""), rates_text(entry.get("rates") or {}),
    ]


def _month_file(folder: Path, when: datetime, *, pc: str = "", suffix: str = ".csv") -> Path:
    middle = f"_{pc}" if pc else ""
    return folder / f"{FILE_PREFIX}{middle}_{when:%Y-%m}{suffix}"


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


def _write_csv(path: Path, entry: dict[str, Any]) -> None:
    """CSV へ1行足す。**新しいファイルなら見出しから**(UTF-8 の BOM 付き ── Excel で文字化けしない。
    日報のほかの CSV と同じ)。足すときは BOM を付け直さない(Python が先頭でないと分かって省く)。"""
    import csv

    fresh = not path.exists() or path.stat().st_size == 0
    with open(path, "a", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        if fresh:
            writer.writerow(CSV_COLUMNS)
        writer.writerow(csv_row(entry))


def _write_jsonl(path: Path, entry: dict[str, Any]) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def append(entry: dict[str, Any], *, line: str = "", when: Optional[datetime] = None,
           fmt: Optional[str] = None) -> dict[str, str]:
    """1行足す。**書けなくても止めない**(返すのは 場所 → 書けなかった理由。書けたら空)。

    形式は設定(`record_format`。既定 CSV)。CSV のファイルを Excel で開いたままだと書けない
    (Windows が掴む)── そのときは理由を残し、ほかの場所(この端末)には書く。
    """
    when = when or datetime.now()
    fmt = fmt or record_format()
    writers = []
    if fmt in (FORMAT_CSV, FORMAT_BOTH):
        writers.append((".csv", _write_csv))
    if fmt in (FORMAT_JSONL, FORMAT_BOTH):
        writers.append((".jsonl", _write_jsonl))
    places = [("local", local_folder(), "")]
    shared = shared_folder(line)
    if shared is not None:
        places.append(("shared", shared, pc_name()))
    result: dict[str, str] = {}
    with _lock:
        for key, folder, pc in places:
            problems = []
            for suffix, write in writers:
                path = _month_file(folder, when, pc=pc, suffix=suffix)
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    write(path, entry)
                except OSError as exc:
                    problems.append(f"{path}: {exc}")
                    log.warning("GW計算の記録を書けませんでした(%s): %s", key, exc)
            _problems[key] = " / ".join(problems)
            result[key] = _problems[key]
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
def display_row(entry: dict[str, Any]) -> dict[str, Any]:
    """記録1件(JSON の形)を、記録の画面の1行に。"""
    return _display_of(dict(zip(CSV_COLUMNS, csv_row(entry))))


def _display_of(cells: dict[str, str]) -> dict[str, Any]:
    """CSV の1行(見出し → 字)を、記録の画面の1行に。CSV と JSON Lines のどちらからも同じ形。"""
    def num(text: str) -> Optional[float]:
        try:
            return float(text) if str(text).strip() else None
        except ValueError:
            return None

    subject = " / ".join(x for x in (
        f"LotNo {cells.get('LotNo')}" if cells.get("LotNo") else "",
        f"オーダーNo {cells.get('オーダーNo')}" if cells.get("オーダーNo") else "",
        f"包装仕様 {cells.get('包装仕様')}" if cells.get("包装仕様") else "") if x)
    version = version_label({"nippou": cells.get("日報の版", ""), "app": cells.get("日報複合ツールの版", "")})
    return {
        "at": cells.get("時刻", ""), "kind": cells.get("種類", ""), "subject": subject,
        "gross": num(cells.get("GW(kg)", "")), "tare": num(cells.get("風袋総重量(kg)", "")),
        "packs": cells.get("梱包数", ""),
        "who": cells.get("PC", "") + (f"({cells['ライン']})" if cells.get("ライン") else ""),
        "version": version, "stamp": cells.get("画面の中身の印", ""),
        "source": f"{cells.get('資材マスタの取得元', '')} {cells.get('資材マスタの場所', '')}".strip(),
        "as_of": cells.get("資材マスタの時点") or "日時不明",
        "rates": cells.get("使った値(単位質量×係数)", ""),
    }


def recent(limit: int = 300) -> list[dict[str, Any]]:
    """この端末の記録を新しい順に、記録の画面の形で(今月と先月。CSV と JSON Lines のどちらも読む)。

    形式を途中で変えても前の記録が見えるように両方読む。「両方」で書いた記録は1つにする。
    """
    import csv

    folder = local_folder()
    try:
        months = sorted({p.stem.rsplit("_", 1)[-1] for p in folder.glob(f"{FILE_PREFIX}_*.*")},
                        reverse=True)[:2]
    except OSError:
        return []
    from collections import Counter

    rows: list[dict[str, Any]] = []
    for month in months:
        from_csv: list[dict[str, Any]] = []
        path = folder / f"{FILE_PREFIX}_{month}.csv"
        try:
            with open(path, encoding="utf-8-sig", newline="") as fh:
                from_csv = [_display_of(cells) for cells in csv.DictReader(fh)]
        except OSError:
            pass
        rows.extend(from_csv)
        # 「両方」で書いた月は、同じ記録が CSV と JSON Lines の両方にある。JSON の1件ごとに、同じ
        # (時刻・種類・何を)の CSV の行を1つだけ打ち消す(同じ秒の別の計算まで1つにしない)
        twins = Counter((r["at"], r["kind"], r["subject"]) for r in from_csv)
        path = folder / f"{FILE_PREFIX}_{month}.jsonl"
        try:
            texts = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            texts = []
        for text in texts:
            try:
                row = display_row(json.loads(text))
            except ValueError:
                continue
            key = (row["at"], row["kind"], row["subject"])
            if twins[key] > 0:
                twins[key] -= 1
                continue
            rows.append(row)
    rows.sort(key=lambda r: r["at"], reverse=True)
    return rows[:limit]


def status(line: str = "") -> dict[str, str]:
    """どこへ書いているか・書けていないか(記録の画面に出す)。"""
    shared = shared_folder(line)
    return {"local": str(local_folder()), "localProblem": _problems["local"],
            "shared": str(shared) if shared else "", "sharedProblem": _problems["shared"],
            "format": FORMATS[record_format()]}
