"""何を載せるか(`config/tools.json`)。**外枠(`src-tauri/src/catalog.rs`)と同じファイルを読む。**

並びが大きなタブの並び。ツールの中身(業務)はここでは扱わない ── 名前・場所・
宛先・環境変数の頭だけ。
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import app_config

CATALOG_PATH = app_config.APP_ROOT / "config" / "tools.json"

PORTAL = "portal"


@dataclass(frozen=True)
class Tool:
    id: str
    title: str
    name: str
    dir: Path
    scheme: str
    env_prefix: str
    local_dir_name: str
    aliases: tuple[str, ...] = field(default_factory=tuple)

    @property
    def token_env(self) -> str:
        return f"{self.env_prefix}_TOKEN"

    @property
    def local_dir_env(self) -> str:
        return f"{self.env_prefix}_LOCAL_DIR"

    def app_config(self) -> dict:
        """そのツールの `config/app.json`(版・待ち受けの番号・手元の領域の名前)。"""
        try:
            return json.loads((self.dir / "config" / "app.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {}

    def version(self) -> str:
        return str(self.app_config().get("version", ""))

    def app_id(self) -> str:
        return str(self.app_config().get("app_id", ""))

    def local_root(self) -> Path:
        """そのツールの手元の領域(ツール自身の `app_config.local_root()` と同じ決め方)。"""
        import os

        custom = os.environ.get(self.local_dir_env, "").strip()
        if custom:
            return Path(custom)
        name = str(self.app_config().get("local_dir_name") or self.local_dir_name)
        for env in ("LOCALAPPDATA", "XDG_DATA_HOME"):
            base = os.environ.get(env, "").strip()
            if base:
                return Path(base) / name
        return Path.home() / ".local" / "share" / name

    def words(self) -> set[str]:
        """表示タブの欄で、このツールを指す書き方(比べるときは `fold` してから)。"""
        return {fold(w) for w in (self.id, self.title, self.name, *self.aliases) if w}

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "name": self.name, "version": self.version()}


def fold(text: str) -> str:
    """書き方の揺れを畳む(全角/半角・大文字/小文字・空白)。"""
    text = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return re.sub(r"\s+", "", text)


@dataclass(frozen=True)
class Catalog:
    portal: Tool
    tools: tuple[Tool, ...]

    def by_id(self, tool_id: str) -> Optional[Tool]:
        for tool in self.tools:
            if tool.id == tool_id:
                return tool
        return None

    def ids(self) -> list[str]:
        return [t.id for t in self.tools]

    def find(self, word: str) -> Optional[Tool]:
        """表示タブの欄の1語から、ツールを引く(名前・短い名前・別名のどれでも)。"""
        key = fold(word)
        if not key:
            return None
        for tool in self.tools:
            if key in tool.words():
                return tool
        return None


def _tool(raw: dict, root: Path) -> Tool:
    tool_id = str(raw["id"])
    rel = str(raw.get("dir") or ".")
    return Tool(
        id=tool_id,
        title=str(raw.get("title") or tool_id),
        name=str(raw.get("name") or raw.get("title") or tool_id),
        dir=root if rel in ("", ".") else (root / rel),
        scheme=str(raw.get("scheme") or tool_id),
        env_prefix=str(raw.get("env_prefix") or tool_id.upper()),
        local_dir_name=str(raw.get("local_dir_name") or raw.get("env_prefix") or tool_id),
        aliases=tuple(str(a) for a in raw.get("aliases", []) if a),
    )


_cache: Optional[Catalog] = None


def load(path: Optional[Path] = None) -> Catalog:
    global _cache
    if path is None and _cache is not None:
        return _cache
    source = path or CATALOG_PATH
    raw = json.loads(Path(source).read_text(encoding="utf-8-sig"))
    root = Path(source).resolve().parent.parent
    catalog = Catalog(portal=_tool(raw["portal"], root),
                      tools=tuple(_tool(t, root) for t in raw.get("tools", [])))
    if path is None:
        _cache = catalog
    return catalog
