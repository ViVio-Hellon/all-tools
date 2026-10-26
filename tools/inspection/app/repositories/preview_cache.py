"""プレビュー画像のキャッシュ（%LOCALAPPDATA%\\<AppId>\\cache\\preview）。

キーには「ファイルのパス・更新日時・サイズ・プレビュー設定」を含めるため、
現場で点検表 Excel を修正すると自動的に別キーになり、新しい画像が作られる。

Word の点検表のプレビュー(PDF。VER2.4.0)も同じ控えに入る(`.pdf`)。画像と同じく、2回目からは
すぐ出て、起動時の `prune` で古いもの・多すぎるものから消える。Word が書いた一時ファイルは
処理プログラムがその場で消すので、ここに残るのは控えだけ。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from typing import Any, Dict, Optional, Tuple

EXT_BY_MIME = {"image/png": ".png", "image/svg+xml": ".svg", "application/pdf": ".pdf"}


class PreviewCache:
    def __init__(self, cache_dir: str, max_entries: int = 300, max_age_days: int = 14):
        self.dir = os.path.join(cache_dir, "preview")
        self.max_entries = max_entries
        self.max_age_sec = max_age_days * 86400
        self._lock = threading.Lock()
        os.makedirs(self.dir, exist_ok=True)

    @staticmethod
    def make_key(path: str, mtime_ns: int, size: int, variant: str) -> str:
        raw = f"{os.path.normcase(path)}|{mtime_ns}|{size}|{variant}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def valid_key(key: str) -> bool:
        return len(key) == 32 and all(c in "0123456789abcdef" for c in key)

    def _meta_path(self, key: str) -> str:
        return os.path.join(self.dir, key + ".json")

    def get_meta(self, key: str) -> Optional[Dict[str, Any]]:
        if not self.valid_key(key):
            return None
        try:
            with open(self._meta_path(key), "r", encoding="utf-8") as fp:
                meta = json.load(fp)
        except (OSError, ValueError):
            return None
        if not isinstance(meta, dict):
            return None
        # **中身が欠けた控えは使わない**(作り直させる)。電源断・掃除ソフトなどで絵・PDF が
        # 0バイトや途中までになっていても、以前は「控えの画像」として出し続けた(PDF の枠は
        # 壊れたまま何も言わない)。書いたときの大きさ(bytes)と違えば、無いものとみなす
        data_path = os.path.join(self.dir, str(meta.get("file", "")))
        if not os.path.isfile(data_path):
            return None
        try:
            size = os.path.getsize(data_path)
        except OSError:
            return None
        if "bytes" in meta and size != meta["bytes"]:
            return None
        return meta

    def read(self, key: str) -> Optional[Tuple[bytes, str]]:
        found = self.read_with_meta(key)
        return (found[0], found[1]) if found else None

    def read_with_meta(self, key: str) -> Optional[Tuple[bytes, str, Dict[str, Any]]]:
        """中身・種類・控えの情報(点検表の名前など)。"""
        meta = self.get_meta(key)
        if not meta:
            return None
        try:
            with open(os.path.join(self.dir, meta["file"]), "rb") as fp:
                return fp.read(), meta.get("mime", "application/octet-stream"), meta
        except OSError:
            return None

    def put(self, key: str, data: bytes, mime: str, meta: Dict[str, Any]) -> Dict[str, Any]:
        file_name = key + EXT_BY_MIME.get(mime, ".bin")
        with self._lock:
            # 動いているあいだに消されても(掃除ソフト・手作業)作り直して続ける
            os.makedirs(self.dir, exist_ok=True)
            tmp = os.path.join(self.dir, file_name + ".tmp")
            with open(tmp, "wb") as fp:
                fp.write(data)
            os.replace(tmp, os.path.join(self.dir, file_name))
            full_meta = {**meta, "file": file_name, "mime": mime, "bytes": len(data), "created": time.time()}
            with open(self._meta_path(key), "w", encoding="utf-8") as fp:
                json.dump(full_meta, fp, ensure_ascii=False)
        return full_meta

    def prune(self) -> int:
        """古いキャッシュを削除する（起動時に実行）。"""
        removed = 0
        with self._lock:
            try:
                metas = [f for f in os.listdir(self.dir) if f.endswith(".json")]
            except OSError:
                return 0
            entries = []
            now = time.time()
            for name in metas:
                path = os.path.join(self.dir, name)
                try:
                    entries.append((os.path.getmtime(path), name[:-5]))
                except OSError:
                    continue
            entries.sort(reverse=True)
            for index, (mtime, key) in enumerate(entries):
                if index >= self.max_entries or now - mtime > self.max_age_sec:
                    for f in os.listdir(self.dir):
                        if f.startswith(key):
                            try:
                                os.remove(os.path.join(self.dir, f))
                            except OSError:
                                pass
                    removed += 1
        return removed
