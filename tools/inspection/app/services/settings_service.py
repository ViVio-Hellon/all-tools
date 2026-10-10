"""業務の設定(`config/inspection.json`)と利用者設定(フォルダ設定・ダークモード)。

VBA版との対応:
    GetBaseFolderPath / SaveBaseFolderPath  → effective_root_folder / update(root_folder=...)
    DarkModeEnabled(既定 True)             → dark_mode(既定 True)

【config/inspection.json の各キー】(JSON はコメントを書けないのでここに置く)

    root_folder          点検表ルートフォルダ(VBA版の DEFAULT_Path \\ DEFAULT_FOLDER)
    extensions           一覧に載せる拡張子(VBA版と同じ xlsm / xlsx / xls)
    require_name_prefix  true: ファイル名が「カテゴリ_サブカテゴリ_」で始まるものだけ
                         表示し、表示名は接頭辞を除いた部分(VBA版と同じ)
    excel.backend        auto(Windows は VBScript 経由)/ vbscript / dummy(模擬)
    excel.disable_macros 点検表を開くときマクロとイベントを無効化する
                         (非表示の Excel が MsgBox で止まるのを防ぐ)
    excel.block_password_prompt  パスワード付きブックで入力待ちにならないよう、
                         ダミーのパスワードを渡して「開けない」エラーにする
    excel.preview_timeout_sec / print_item_timeout_sec  Excel の応答を待つ上限
    excel.excel_exit_grace_sec  Excel が終わらないとき、止めるまでの猶予
    excel.keep_alive_sec 処理のあと Excel を残しておく秒数(既定 120)。続けて
                         プレビュー・印刷するときに Excel の起動を待たなくて済む。
                         0 にすると毎回起動して終了する(VER1.1.1 までの動き)
    excel.preview_appearance  screen(VBA版と同じ xlScreen)/ printer(xlPrinter)
    excel.preview_scale / preview_max_pixels  プレビュー画像の拡大率と最大辺
    excel.preview_max_rows / preview_max_cols  印刷範囲が無いときの行数・列数(40×16)
    print.max_copies     印刷部数の上限(VBA版は 100)

利用者が画面の「フォルダ設定」で変えた値は、ローカル領域の
`data/user_settings.json` に保存し、`config/inspection.json` は書き換えない。
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.repositories.settings_repository import SettingsRepository

APP_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = Path(os.environ.get("INSPECTION_BUSINESS_CONFIG",
                                  str(APP_ROOT / "config" / "inspection.json")))


@dataclass
class ExcelConfig:
    backend: str = "auto"
    disable_macros: bool = True
    block_password_prompt: bool = True
    preview_timeout_sec: int = 90
    print_item_timeout_sec: int = 180
    excel_exit_grace_sec: int = 15
    keep_alive_sec: int = 120
    preview_appearance: str = "screen"
    preview_scale: float = 2.0
    preview_max_pixels: int = 3200
    preview_max_rows: int = 40
    preview_max_cols: int = 16


@dataclass
class InspectionConfig:
    root_folder: str = ""
    extensions: List[str] = field(default_factory=lambda: ["xlsm", "xlsx", "xls"])
    require_name_prefix: bool = True
    excel: ExcelConfig = field(default_factory=ExcelConfig)
    max_copies: int = 100
    load_error: str = ""


def _clamp(value: Any, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def load_inspection_config(path: Optional[Path] = None) -> InspectionConfig:
    """`config/inspection.json` を読む。壊れていても既定値で起動し、理由を残す。"""
    target = Path(path) if path else CONFIG_PATH
    cfg = InspectionConfig()
    try:
        raw = json.loads(target.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            raise ValueError("トップレベルがオブジェクトではありません")
    except FileNotFoundError:
        cfg.load_error = f"設定ファイルがありません: {target}"
        return cfg
    except Exception as exc:  # noqa: BLE001
        cfg.load_error = f"設定ファイルを読めませんでした ({target}): {exc}"
        return cfg
    cfg.root_folder = str(raw.get("root_folder") or "")
    exts = raw.get("extensions")
    if isinstance(exts, list) and exts:
        cfg.extensions = [str(e).lower().lstrip(".") for e in exts if str(e).strip()]
    cfg.require_name_prefix = bool(raw.get("require_name_prefix", True))
    ex = raw.get("excel") if isinstance(raw.get("excel"), dict) else {}
    d = ExcelConfig()
    cfg.excel = ExcelConfig(
        backend=str(ex.get("backend", d.backend)).lower(),
        disable_macros=bool(ex.get("disable_macros", d.disable_macros)),
        block_password_prompt=bool(ex.get("block_password_prompt", d.block_password_prompt)),
        preview_timeout_sec=int(_clamp(ex.get("preview_timeout_sec"), d.preview_timeout_sec, 10, 3600)),
        print_item_timeout_sec=int(_clamp(ex.get("print_item_timeout_sec"), d.print_item_timeout_sec, 10, 3600)),
        excel_exit_grace_sec=int(_clamp(ex.get("excel_exit_grace_sec"), d.excel_exit_grace_sec, 1, 120)),
        keep_alive_sec=int(_clamp(ex.get("keep_alive_sec"), d.keep_alive_sec, 0, 1800)),
        preview_appearance="printer" if str(ex.get("preview_appearance", "")).lower() == "printer" else "screen",
        preview_scale=_clamp(ex.get("preview_scale"), d.preview_scale, 0.5, 4.0),
        preview_max_pixels=int(_clamp(ex.get("preview_max_pixels"), d.preview_max_pixels, 400, 10000)),
        preview_max_rows=int(_clamp(ex.get("preview_max_rows"), d.preview_max_rows, 1, 10000)),
        preview_max_cols=int(_clamp(ex.get("preview_max_cols"), d.preview_max_cols, 1, 1000)),
    )
    printing = raw.get("print") if isinstance(raw.get("print"), dict) else {}
    cfg.max_copies = int(_clamp(printing.get("max_copies"), 100, 1, 999))
    return cfg


def normalize_folder(path: str) -> str:
    """前後の空白・引用符・末尾の区切り文字を除き、重複した区切りを整理する。

    VBA版の既定パスは「...参照用ファイル\\\\一括管理_点検表」のように区切りが
    二重になっていたため、ここで正規化する。
    """
    text = (path or "").strip().strip('"').strip()
    if not text:
        return ""
    return os.path.normpath(text)


class SettingsService:
    def __init__(self, cfg: InspectionConfig, repository: SettingsRepository, logger: Any,
                 forced_root_folder: Optional[str] = None):
        self.cfg = cfg
        self.repo = repository
        self.log = logger
        self.forced_root_folder = normalize_folder(forced_root_folder or "")  # 模擬モード用
        self._lock = threading.Lock()
        self._data = repository.load()

    @property
    def default_root_folder(self) -> str:
        return normalize_folder(self.cfg.root_folder)

    def effective_root_folder(self) -> str:
        if self.forced_root_folder:
            return self.forced_root_folder
        with self._lock:
            user = normalize_folder(str(self._data.get("root_folder") or ""))
        return user or self.default_root_folder

    def dark_mode(self) -> bool:
        with self._lock:
            value = self._data.get("dark_mode")
        # 既定はライト(日報複合ツールの4ツールでそろえる。VBA版の既定はダークだった)
        return False if value is None else bool(value)

    def theme_chosen(self) -> bool:
        """この端末で明暗を選んだか。選んでいなければ、日報複合ツールの大設定の既定に従う。"""
        with self._lock:
            return self._data.get("dark_mode") is not None

    def to_dict(self) -> Dict[str, Any]:
        if self.forced_root_folder:
            return {"root_folder": self.forced_root_folder, "root_folder_default": self.default_root_folder,
                    "root_folder_source": "demo", "dark_mode": self.dark_mode()}
        with self._lock:
            user_root = normalize_folder(str(self._data.get("root_folder") or ""))
        return {
            "root_folder": user_root or self.default_root_folder,
            "root_folder_default": self.default_root_folder,
            "root_folder_source": "user" if user_root else "config",
            "dark_mode": self.dark_mode(),
        }

    # ---- 値そのもの(管理者パスワード・配布設定が使う) -----------------
    def values(self) -> Dict[str, Any]:
        """保存してある値の写し。**この端末で変えた項目だけ**が入っている。"""
        with self._lock:
            return dict(self._data)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def put(self, key: str, value: Any) -> None:
        """1項目を保存する。`None` なら消す(既定に戻る)。"""
        self.put_many({key: value})

    def put_many(self, values: Dict[str, Any]) -> None:
        with self._lock:
            data = dict(self._data)
            for key, value in values.items():
                if value is None:
                    data.pop(key, None)
                else:
                    data[key] = value
            self.repo.save(data)
            self._data = data

    def update(self, root_folder: Optional[str] = None, reset_root_folder: bool = False,
               dark_mode: Optional[bool] = None) -> Dict[str, Any]:
        with self._lock:
            data = dict(self._data)
            if reset_root_folder:
                data.pop("root_folder", None)
                self.log.info("点検表ルートフォルダを既定値に戻しました")
            elif root_folder is not None:
                folder = normalize_folder(root_folder)
                data["root_folder"] = folder
                self.log.info("点検表ルートフォルダを変更しました: %s", folder)
            if dark_mode is not None:
                data["dark_mode"] = bool(dark_mode)
            self.repo.save(data)
            self._data = data
        return self.to_dict()
