"""点検表一覧のデータ構造（ルート → カテゴリー → サブカテゴリー → Excel）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class InspectionItem:
    """画面に表示する点検表 1 件（= Excel ファイル 1 つ）。"""
    id: str               # 相対パスから作る安定した ID（画面⇔サーバー間はこの ID だけでやり取り）
    name: str             # 表示名（VBA版: ファイル名から「カテゴリ_サブカテゴリ_」を除いた部分）
    file_name: str        # ファイル名（拡張子付き）
    category: str
    subcategory: str
    path: str             # フルパス（サーバー内部でのみ使用）
    ext: str
    size: int
    modified: float       # 更新日時（UNIX 時間）

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "file_name": self.file_name,
            "category": self.category,
            "subcategory": self.subcategory,
            "ext": self.ext,
            "size": self.size,
            "modified": self.modified,
        }


@dataclass
class UnmatchedFile:
    """拡張子は対象だが、命名規則（カテゴリ_サブカテゴリ_）に合わず表示しないファイル。"""
    file_name: str
    expected_prefix: str

    def to_dict(self) -> Dict[str, Any]:
        return {"file_name": self.file_name, "expected_prefix": self.expected_prefix}


@dataclass
class SubCategory:
    name: str
    items: List[InspectionItem] = field(default_factory=list)
    unmatched: List[UnmatchedFile] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "items": [i.to_dict() for i in self.items],
                "unmatched": [u.to_dict() for u in self.unmatched]}


@dataclass
class Category:
    name: str
    subcategories: List[SubCategory] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "subcategories": [s.to_dict() for s in self.subcategories]}


@dataclass
class Inventory:
    root: str
    root_exists: bool
    scanned_at: float
    scan_ms: int = 0
    categories: List[Category] = field(default_factory=list)
    total_files: int = 0      # 対象拡張子のファイル数（VBA版の「検出: n」）
    listed_files: int = 0     # 画面に表示する点検表の数
    errors: List[Dict[str, str]] = field(default_factory=list)
    by_id: Dict[str, InspectionItem] = field(default_factory=dict, repr=False)

    def find(self, item_id: str) -> Optional[InspectionItem]:
        return self.by_id.get(item_id)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "root": self.root,
            "root_exists": self.root_exists,
            "scanned_at": self.scanned_at,
            "scan_ms": self.scan_ms,
            "total_files": self.total_files,
            "listed_files": self.listed_files,
            "unmatched_files": self.total_files - self.listed_files,
            "errors": self.errors[:50],
            "categories": [c.to_dict() for c in self.categories],
        }
