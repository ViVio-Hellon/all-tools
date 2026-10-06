"""点検表の検索・一覧（フォルダ構成から自動取得）。

VBA版との対応:
    GetWorkbooksFromFolder     → scan_folder
    GetWorkbooksByFilter       → Inventory のカテゴリー/サブカテゴリー構造
    CreateCheckBoxes の接頭辞判定 → _display_name（「カテゴリ_サブカテゴリ_」で始まるものだけ表示）
    SortCollectionAscending    → 大文字小文字を区別しない昇順（vbTextCompare 相当）

フォルダ構成:
    ルート ─ カテゴリー ─ サブカテゴリー ─ Excel（.xlsm / .xlsx / .xls）
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from core import event_log
from app.models.inspection import Category, InspectionItem, Inventory, SubCategory, UnmatchedFile

ProgressCallback = Callable[[int, str], None]


def _sort_key(name: str) -> str:
    return name.casefold()


def make_item_id(category: str, subcategory: str, file_name: str) -> str:
    raw = f"{category}/{subcategory}/{file_name}".casefold()
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def display_name(category: str, subcategory: str, base_name: str,
                 require_prefix: bool) -> Optional[str]:
    """VBA版 CreateCheckBoxes と同じ判定で表示名を決める。表示しない場合は None。

        prefix = category & "_" & subCategory & "_"
        If LCase(Left(fileName, Len(prefix))) = LCase(prefix) Then
            displayName = Mid(fileName, Len(prefix) + 1)
    """
    file_name = base_name.strip()
    prefix = f"{category}_{subcategory}_"
    if file_name[:len(prefix)].lower() == prefix.lower():
        return file_name[len(prefix):]
    if require_prefix:
        return None
    return file_name


def _list_dirs(path: str, errors: List[Dict[str, str]]) -> List[os.DirEntry]:
    try:
        with os.scandir(path) as it:
            dirs = [e for e in it if _is_dir(e)]
    except OSError as exc:
        errors.append({"path": path, "message": f"フォルダを読み取れません: {exc.strerror or exc}"})
        return []
    return sorted(dirs, key=lambda e: _sort_key(e.name))


def _is_dir(entry: os.DirEntry) -> bool:
    try:
        return entry.is_dir()
    except OSError:
        return False


def _list_files(path: str, exts: Tuple[str, ...], errors: List[Dict[str, str]]) -> List[os.DirEntry]:
    try:
        with os.scandir(path) as it:
            files = []
            for entry in it:
                name = entry.name
                if name.startswith("~$"):
                    continue  # Excel が作る一時ロックファイル
                ext = os.path.splitext(name)[1][1:].lower()
                if ext not in exts:
                    continue
                try:
                    if entry.is_file():
                        files.append(entry)
                except OSError:
                    continue
    except OSError as exc:
        errors.append({"path": path, "message": f"フォルダを読み取れません: {exc.strerror or exc}"})
        return []
    return sorted(files, key=lambda e: _sort_key(e.name))


def scan_folder(root: str, extensions: List[str], require_prefix: bool = True,
                progress: Optional[ProgressCallback] = None) -> Inventory:
    started = time.perf_counter()
    exts = tuple(e.lower().lstrip(".") for e in extensions)
    inventory = Inventory(root=root, root_exists=bool(root) and os.path.isdir(root), scanned_at=time.time())
    if not inventory.root_exists:
        return inventory
    errors = inventory.errors
    scanned = 0
    for cat_entry in _list_dirs(root, errors):
        category = Category(name=cat_entry.name)
        for sub_entry in _list_dirs(cat_entry.path, errors):
            sub = SubCategory(name=sub_entry.name)
            for file_entry in _list_files(sub_entry.path, exts, errors):
                scanned += 1
                inventory.total_files += 1
                base, ext = os.path.splitext(file_entry.name)
                name = display_name(category.name, sub.name, base, require_prefix)
                if name is None:
                    sub.unmatched.append(UnmatchedFile(file_entry.name, f"{category.name}_{sub.name}_"))
                    continue
                try:
                    st = file_entry.stat()
                    size, mtime = st.st_size, st.st_mtime
                except OSError:
                    size, mtime = 0, 0.0
                item = InspectionItem(
                    id=make_item_id(category.name, sub.name, file_entry.name), name=name,
                    file_name=file_entry.name, category=category.name, subcategory=sub.name,
                    path=file_entry.path, ext=ext[1:].lower(), size=size, modified=mtime)
                sub.items.append(item)
                inventory.by_id[item.id] = item
                inventory.listed_files += 1
            if progress:
                progress(scanned, f"{category.name} > {sub.name}")
            # VBA版と同様、Excel ファイルが1つもないサブカテゴリーは表示しない
            if sub.items or sub.unmatched:
                category.subcategories.append(sub)
        if category.subcategories:
            inventory.categories.append(category)
    inventory.scan_ms = int((time.perf_counter() - started) * 1000)
    return inventory


# ----------------------------------------------------------------------
# 前回の一覧の控え
# ----------------------------------------------------------------------
# 共有フォルダの確認には数秒かかる。起動のたびにそれを待たせていたので
# (ラインPCで「起動がやや重い」)、前回の一覧を控えておき、**起動したら
# まずそれを出す**。確認は裏で続け、終わったら画面が新しい一覧に替わる。
CACHE_VERSION = 1


def inventory_to_cache(inv: Inventory) -> Dict[str, Any]:
    """控えに書く形。画面に返す形(`to_dict`)と違い、フルパスも持つ。"""
    return {
        "version": CACHE_VERSION, "root": inv.root, "root_exists": inv.root_exists,
        "scanned_at": inv.scanned_at, "scan_ms": inv.scan_ms, "total_files": inv.total_files,
        "listed_files": inv.listed_files, "errors": inv.errors[:50],
        "categories": [{
            "name": c.name,
            "subcategories": [{
                "name": sc.name,
                "items": [{"id": i.id, "name": i.name, "file_name": i.file_name, "path": i.path,
                           "ext": i.ext, "size": i.size, "modified": i.modified} for i in sc.items],
                "unmatched": [u.to_dict() for u in sc.unmatched],
            } for sc in c.subcategories],
        } for c in inv.categories],
    }


def inventory_from_cache(data: Dict[str, Any]) -> Inventory:
    if data.get("version") != CACHE_VERSION:
        raise ValueError("控えの形が違います")
    inv = Inventory(root=str(data["root"]), root_exists=bool(data["root_exists"]),
                    scanned_at=float(data["scanned_at"]), scan_ms=int(data.get("scan_ms", 0)),
                    total_files=int(data.get("total_files", 0)), listed_files=int(data.get("listed_files", 0)),
                    errors=list(data.get("errors", [])))
    for c in data["categories"]:
        category = Category(name=c["name"])
        for sc in c["subcategories"]:
            sub = SubCategory(name=sc["name"])
            for i in sc["items"]:
                item = InspectionItem(id=i["id"], name=i["name"], file_name=i["file_name"],
                                      category=category.name, subcategory=sub.name, path=i["path"],
                                      ext=i["ext"], size=int(i["size"]), modified=float(i["modified"]))
                sub.items.append(item)
                inv.by_id[item.id] = item
            sub.unmatched = [UnmatchedFile(u["file_name"], u["expected_prefix"]) for u in sc["unmatched"]]
            category.subcategories.append(sub)
        inv.categories.append(category)
    return inv


class InspectionService:
    """フォルダ検索を裏で実行し、最新の一覧を保持する。"""

    def __init__(self, cfg: Any, settings_service: Any, logger: Any, cache_path: Optional[str] = None):
        self.cfg = cfg
        self.settings = settings_service
        self.log = logger
        self.cache_path = cache_path
        self._lock = threading.Lock()
        self._inventory: Optional[Inventory] = None
        self._from_cache = False
        self._done = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._status: Dict[str, Any] = {"state": "idle", "scanned": 0, "current": "", "message": ""}

    # ---- 前回の控え -------------------------------------------------
    def load_cache(self) -> bool:
        """前回の一覧を出しておく。フォルダ設定が変わっていれば使わない。"""
        if not self.cache_path:
            return False
        try:
            with open(self.cache_path, "r", encoding="utf-8") as fp:
                inv = inventory_from_cache(json.load(fp))
        except FileNotFoundError:
            return False
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.log.warning("前回の一覧の控えを読めませんでした(確認し直します): %s", exc)
            return False
        if os.path.normcase(inv.root) != os.path.normcase(self.settings.effective_root_folder()):
            return False
        with self._lock:
            if self._inventory is None:
                self._inventory = inv
                self._from_cache = True
        self.log.info("前回の一覧を先に表示します(%d 件・%s 時点)", inv.listed_files,
                      time.strftime("%m/%d %H:%M", time.localtime(inv.scanned_at)))
        return True

    def _save_cache(self, inv: Inventory) -> None:
        if not self.cache_path or not inv.root_exists:
            return
        try:
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            tmp = self.cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(inventory_to_cache(inv), fp, ensure_ascii=False)
            os.replace(tmp, self.cache_path)
        except OSError as exc:
            self.log.warning("一覧の控えを保存できませんでした: %s", exc)

    def clear(self) -> None:
        """いまの一覧を捨てる(フォルダ設定を変えたとき。別のフォルダの一覧を出さない)。"""
        with self._lock:
            self._inventory = None
            self._from_cache = False

    # ---- 検索 -----------------------------------------------------
    def start_scan(self, reason: str = "") -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._done.clear()
            self._status = {"state": "scanning", "scanned": 0, "current": "", "message": "",
                            "started_at": time.time(), "reason": reason}
            self._thread = threading.Thread(target=self._scan_worker, name="folder-scan", daemon=True)
            self._thread.start()
            return True

    def _scan_worker(self) -> None:
        root = self.settings.effective_root_folder()
        self.log.info("フォルダ検索開始: %s", root)

        def progress(scanned: int, current: str) -> None:
            with self._lock:
                self._status.update(scanned=scanned, current=current)

        try:
            inventory = scan_folder(root, self.cfg.extensions, self.cfg.require_name_prefix, progress)
        except Exception as exc:  # noqa: BLE001
            self.log.exception("フォルダ検索でエラーが発生しました")
            with self._lock:
                self._status.update(state="error", message=f"フォルダ検索でエラーが発生しました: {exc}")
                reason = self._status.get("reason", "")
            event_log.record("scan", event_log.NG, code="SCAN_FAILED", message="フォルダ検索でエラーが発生しました",
                             detail=f"{type(exc).__name__}: {exc}", folder=root, reason=reason)
            self._done.set()
            return
        with self._lock:
            self._inventory = inventory
            self._from_cache = False
            self._status.update(state="done", scanned=inventory.total_files, current="")
        self._save_cache(inventory)
        with self._lock:
            reason = self._status.get("reason", "")
        if not inventory.root_exists:
            self.log.warning("点検表フォルダが見つかりません: %s", root)
            event_log.record(
                "scan", event_log.NG, code="FOLDER_NOT_FOUND", message="点検表フォルダが見つかりません",
                why=event_log.why_chain(
                    "点検表フォルダが見つかりません", f"見に行ったフォルダ: {root}",
                    "共有フォルダなら: ネットワーク・サーバ・権限・ドライブの割り当てを確かめる"),
                folder=root, reason=reason, elapsed_ms=inventory.scan_ms)
        else:
            event_log.record(
                "scan", event_log.OK if not inventory.errors else event_log.NG,
                code="" if not inventory.errors else "READ_ERRORS",
                message="" if not inventory.errors else f"読めなかったフォルダ・ファイルが {len(inventory.errors)} 件",
                folder=root, reason=reason, found=inventory.total_files, listed=inventory.listed_files,
                categories=len(inventory.categories), elapsed_ms=inventory.scan_ms,
                read_errors=inventory.errors[:20])
            self.log.info("フォルダ検索完了: 検出 %d 件 / 表示 %d 件 / カテゴリ %d / %d ms / 読取エラー %d",
                          inventory.total_files, inventory.listed_files, len(inventory.categories),
                          inventory.scan_ms, len(inventory.errors))
            for err in inventory.errors[:20]:
                self.log.warning("フォルダ読取エラー: %s (%s)", err["path"], err["message"])
        self._done.set()

    def wait(self, timeout: float) -> bool:
        return self._done.wait(timeout)

    # ---- 参照 -----------------------------------------------------
    def status(self) -> Dict[str, Any]:
        with self._lock:
            status = dict(self._status)
            inv = self._inventory
        status["has_inventory"] = inv is not None
        status["scanned_at"] = inv.scanned_at if inv else None
        # いま出している一覧が前回の控えか(確認の途中)
        with self._lock:
            status["from_cache"] = self._from_cache
        return status

    def inventory(self) -> Optional[Inventory]:
        with self._lock:
            return self._inventory

    def get_item(self, item_id: str) -> Optional[InspectionItem]:
        inv = self.inventory()
        return inv.find(item_id) if inv else None

    def summary(self) -> Dict[str, Any]:
        inv = self.inventory()
        if inv is None:
            return {"scanned_at": None, "total_files": 0, "listed_files": 0}
        return {"scanned_at": inv.scanned_at, "total_files": inv.total_files,
                "listed_files": inv.listed_files, "root": inv.root, "root_exists": inv.root_exists}
