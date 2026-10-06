"""VC長さ計算が使うマスタの「いまの中身」

vc-calculator の `services/masters.py` を、日報管理ツールの読み方に合わせたもの。

【どこにあるか】
参照用マスタのフォルダの `VC計算マスタ.sqlite3`(`config.SETTINGS.vc_master_path`)。
vc-calculator の `vc_master.sqlite3` を同じフォルダに置けば、**そのまま読みます**
(`config.OLDER_NAMES`)。表の形は同じなので、2つのツールで1つのマスタを
共有できます ── こちらで直したときも更新番号を上げるので、vc-calculator の
端末にも次の操作から効きます(`nippou/master_admin.py`)。

【無ければ作る】
初めて開いたとき、フォルダにファイルが無ければ **VBA の直書きと早見表
(R.1.10/1)の初期値**で作ります(`db.ensure_database`)。古い版の vc_master
なら、同じ取引の中で今の形へ直します。

【どう読むか】
ほかの参照マスタと同じく**手元に写してから読みます**(`source_db`)。共有の
ファイルを開いたままにしませんし、変わっていなければ写し直しもしません。

【読めないとき】
計算は止めません ── VBA は値がコードに入っていたので、共有が落ちても計算
できました。そこを下回らないよう、次の順で使えるものを使います。

    1. 最後に読めた中身(このプロセスが覚えているもの)
    2. 最後に読めた中身の控え(アプリのフォルダの JSON)
    3. VBA の直書きの値(`seed`)

画面には「どれで動いているか」と理由を出します(`Snapshot.status`)。
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Optional

from ..logging_setup import get_logger
from . import db, seed
from .calc import SheetSize
from .db import quote

log = get_logger("vc.masters")

#: 作れなかった・読めなかったあと、次に共有を見に行くまで(秒)。毎回待たせない
RETRY_AFTER_SEC = 30.0

SOURCE_LABELS = {
    "db": "マスタ",
    "cache": "最後に読めたマスタの控え",
    "seed": "VBA の初期値",
}


@dataclass
class InnerChoice:
    label: str
    inside: float
    order: int = 0


@dataclass
class Product:
    name: str
    vendor: str
    vcatu: float
    inside: Optional[float]
    order: int = 0
    note: str = ""
    choices: list[InnerChoice] = field(default_factory=list)

    @property
    def chooses_inside(self) -> bool:
        """内径を大/小などから選ぶ品種か(VBA の Nittou 枠が出る品種)。"""
        return bool(self.choices)


@dataclass
class QuickRow:
    inside: float
    cells: dict[float, Optional[int]]     # 肉厚 → 長さ(式で出す枠では None)


@dataclass
class QuickBlock:
    name: str
    vendor: str
    tone: str
    order: int
    rows: list[QuickRow] = field(default_factory=list)
    product: str = ""                     # 計算品種(空なら 早見表値.長さ をそのまま出す)
    vcatu: Optional[float] = None         # 計算品種の VC厚(品種が見つからなければ None)


@dataclass
class Snapshot:
    revision: int = 0
    products: list[Product] = field(default_factory=list)
    sheets: list[SheetSize] = field(default_factory=list)
    quick: list[QuickBlock] = field(default_factory=list)
    settings: dict[str, str] = field(default_factory=dict)
    path: str = ""
    read_at: str = ""
    source: str = "none"      # "db" | "cache" | "seed" | "none"
    problem: str = ""

    @property
    def usable(self) -> bool:
        return self.source in ("db", "cache", "seed")

    def product(self, name: str) -> Optional[Product]:
        for p in self.products:
            if p.name == name:
                return p
        return None

    def setting(self, key: str, default: str = "") -> str:
        return self.settings.get(key, default)

    def reverse_enabled(self) -> bool:
        return self.setting("肉厚の逆算", "0").strip() == "1"

    def status(self) -> dict[str, Any]:
        return {"revision": self.revision, "source": self.source,
                "source_label": SOURCE_LABELS.get(self.source, "なし"),
                "read_at": self.read_at, "problem": self.problem,
                "path": self.path}


# ------------------------------------------------------------------
# 読む
# ------------------------------------------------------------------
def read_all(conn, path: Path) -> Snapshot:
    """開いた接続から全部読む(表はどれも数十行)。"""
    snap = Snapshot(revision=db.revision(conn), path=str(path),
                    read_at=db.now_text(), source="db")
    choices: dict[str, list[InnerChoice]] = {}
    for r in conn.execute(f"SELECT 品種名, 表示順, 表示名, 内径 FROM {quote('VC内径選択肢')}"
                          " ORDER BY 品種名, 表示順, 内径"):
        choices.setdefault(r["品種名"], []).append(
            InnerChoice(label=str(r["表示名"]), inside=float(r["内径"]),
                        order=int(r["表示順"] or 0)))
    for r in conn.execute(f"SELECT * FROM {quote('VC品種')} WHERE 有効 = 1"
                          " ORDER BY 表示順, rowid"):
        snap.products.append(Product(
            name=str(r["品種名"]), vendor=str(r["業者名"] or ""),
            vcatu=float(r["VC厚"]),
            inside=None if r["内径"] is None else float(r["内径"]),
            order=int(r["表示順"] or 0), note=str(r["備考"] or ""),
            choices=choices.get(r["品種名"], [])))
    for r in conn.execute(f"SELECT * FROM {quote('枚数定尺')} WHERE 有効 = 1"
                          " ORDER BY 表示順, rowid"):
        snap.sheets.append(SheetSize(key=str(r["記号"]),
                                     label=str(r["表示名"] or r["記号"]),
                                     length_mm=float(r["長さmm"])))
    cells: dict[str, dict[float, dict[float, Optional[int]]]] = {}
    for r in conn.execute(f"SELECT 品種名, 内径, 肉厚, 長さ FROM {quote('早見表値')}"):
        cells.setdefault(r["品種名"], {}).setdefault(float(r["内径"]), {})[
            float(r["肉厚"])] = None if r["長さ"] is None else int(r["長さ"])
    # 計算品種の VC厚 は VC品種 から引く(有効かどうかは問わない。早見表だけ残す品種がある)
    for r in conn.execute(
            f"SELECT b.*, p.VC厚 AS 計算VC厚 FROM {quote('早見表ブロック')} b"
            f" LEFT JOIN {quote('VC品種')} p ON p.品種名 = b.計算品種"
            " WHERE b.有効 = 1 ORDER BY b.表示順, b.rowid"):
        rows = cells.get(r["品種名"], {})
        snap.quick.append(QuickBlock(
            name=str(r["品種名"]), vendor=str(r["業者名"] or ""),
            tone=str(r["枠色"] or ""), order=int(r["表示順"] or 0),
            rows=[QuickRow(inside=k, cells=dict(sorted(v.items())))
                  for k, v in sorted(rows.items())],
            product=str(r["計算品種"] or ""),
            vcatu=None if r["計算VC厚"] is None else float(r["計算VC厚"])))
    for r in conn.execute(f"SELECT キー, 値 FROM {quote('アプリ設定')}"):
        snap.settings[str(r["キー"])] = str(r["値"] or "")
    return snap


def seed_snapshot(path: Path, problem: str) -> Snapshot:
    """VBA の直書きの値だけで作った中身(ファイルに届かないときの最後の砦)。"""
    import sqlite3

    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        db.create_schema(conn)
        db.insert_seed(conn)
        conn.execute("COMMIT")
        snap = read_all(conn, path)
    finally:
        conn.close()
    return replace(snap, source="seed", problem=problem, revision=0)


# ------------------------------------------------------------------
# 控え(アプリのフォルダの JSON)
# ------------------------------------------------------------------
def cache_path() -> Path:
    from ..config import SETTINGS
    return SETTINGS.app_dir / "cache" / "vc_master_snapshot.json"


def _save_cache(snap: Snapshot) -> None:
    try:
        target = cache_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(asdict(snap), ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        log.warning("VC計算マスタの控えを書けませんでした: %s", exc)


def _load_cache() -> Optional[Snapshot]:
    try:
        raw = json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        return Snapshot(
            revision=int(raw.get("revision", 0)),
            products=[Product(**{**p, "choices": [InnerChoice(**c) for c in p.get("choices", [])]})
                      for p in raw.get("products", [])],
            sheets=[SheetSize(**s) for s in raw.get("sheets", [])],
            quick=[QuickBlock(**{**b, "rows": [
                QuickRow(inside=float(r["inside"]),
                         cells={float(k): (None if v is None else int(v))
                                for k, v in r["cells"].items()})
                for r in b.get("rows", [])]}) for b in raw.get("quick", [])],
            settings={str(k): str(v) for k, v in raw.get("settings", {}).items()},
            path=str(raw.get("path", "")), read_at=str(raw.get("read_at", "")),
            source="cache")
    except (TypeError, KeyError, ValueError) as exc:
        log.warning("VC計算マスタの控えが壊れています: %s", exc)
        return None


# ------------------------------------------------------------------
# いまの中身
# ------------------------------------------------------------------
_lock = threading.Lock()
_ensure_lock = threading.Lock()
_current: Optional[Snapshot] = None
_ensured: set[str] = set()
_failed_at = 0.0
_failed_why = ""


def reset() -> None:
    """試験用・置き場所を変えたとき。**次の要求で作り直しから見る。**"""
    global _current, _failed_at, _failed_why
    with _lock:
        _current = None
        _ensured.clear()
        _failed_at, _failed_why = 0.0, ""


def invalidate() -> None:
    """マスタを直した直後。次の要求で必ず読み直す。"""
    global _failed_at
    with _lock:
        _failed_at = 0.0


def master_path() -> Path:
    from ..config import SETTINGS
    return SETTINGS.vc_master_path


def ensure(path: Path) -> str:
    """無ければ作る・古ければ直す。**プロセスで1回**(同じ道なら)。

    **1つずつ。** 画面を開くと計算(`/api/vc/state`)と設定(`/api/vc/settings`)の
    要求が同時に来る。2つが同時に作り始めると、片方が落ちて、しばらく
    「最後に読めた中身」で動いていた(v3.96.0 で直した)。
    """
    key = str(path)
    if key in _ensured:
        return "ok"
    with _ensure_lock:
        if key in _ensured:                      # 待っているあいだに済んだ
            return "ok"
        done = db.ensure_database(path)
        _ensured.add(key)
    if done != "ok":
        log.info("VC計算マスタ: %s (%s)", done, path)
    return done


def current() -> Snapshot:
    """計算・早見表が使うマスタ。**ファイルに届かなくても何かしら返す。**"""
    global _current, _failed_at, _failed_why
    from .. import source_db

    path = master_path()
    now = time.monotonic()
    if _failed_at and now - _failed_at < RETRY_AFTER_SEC:
        return _fallback(path, _failed_why)
    try:
        ensure(path)
        with source_db.open_source(path) as conn:
            snap = read_all(conn, path)
    except (db.DbError, source_db.SourceError, OSError) as exc:
        why = f"VC計算マスタを読めません: {exc}"
        if why != _failed_why:
            log.warning("%s", why)
        _failed_at, _failed_why = time.monotonic(), why
        return _fallback(path, why)
    except Exception as exc:                     # noqa: BLE001 - 表が欠けている など
        why = f"VC計算マスタの中身を読めません: {exc}"
        log.warning("%s", why)
        _failed_at, _failed_why = time.monotonic(), why
        return _fallback(path, why)
    with _lock:
        changed = _current is None or _current.revision != snap.revision \
            or _current.path != snap.path
        _current = snap
        _failed_at, _failed_why = 0.0, ""
    if changed:
        log.info("VC計算マスタを読みました: 更新番号 %s (%s)", snap.revision, path)
        _save_cache(snap)
    return snap


def _fallback(path: Path, why: str) -> Snapshot:
    with _lock:
        cached = _current
    if cached is None or cached.path != str(path):
        cached = _load_cache()
    if cached is not None and cached.path == str(path):
        return replace(cached, source="cache", problem=why)
    return seed_snapshot(path, why)
