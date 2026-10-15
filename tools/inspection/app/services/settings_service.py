"""業務の設定(`config/inspection.json`)と利用者設定(フォルダ設定・ダークモード)。

VBA版との対応:
    GetBaseFolderPath / SaveBaseFolderPath  → effective_root_folder / update(root_folder=...)
    DarkModeEnabled(既定 True)             → theme_choice(選んでいなければ大設定の既定、無ければライト)

【config/inspection.json の各キー】(JSON はコメントを書けないのでここに置く)

    root_folder          点検表ルートフォルダ(VBA版の DEFAULT_Path \\ DEFAULT_FOLDER)
    extensions           一覧に載せる拡張子(VBA版の xlsm / xlsx / xls に、VER2.3.0 で Word の
                         doc / docx / docm を足した)。Word のものは Word で開いて刷る。
                         前の版の既定(xlsm / xlsx / xls の3つだけ)のままなら Word も足して読む
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
    excel.word_preview_pdf   Word の点検表のプレビューを PDF にする(既定 true。VER2.4.0)。
                         false にすると前の版と同じ1ページ目の絵(落ちる文書があった)
    excel.word_preview_max_pages  PDF に入れるページ数の上限(既定 20。0 = 全ページ)。
                         長い文書で待たせないため。超えたら先頭からこのページ数まで
    excel.close_open_copy  この PC で開いたままの点検表を、保存せずに閉じてから開く(既定 true)
    print.max_copies     印刷部数の上限(VBA版は 100)

利用者が画面の「フォルダ設定」で変えた値は、ローカル領域の
`data/user_settings.json` に保存し、`config/inspection.json` は書き換えない。

【画面の明るさ(VER2.4.0)】
    > 大設定で ダークにしているのに ライト表示される(大設定の値がデフォルトになっていない)

選んだものは `theme_choice`(light / dark)。**無ければ「既定」** ── 日報複合ツールの大設定の
「画面の色の既定」に従う(届いていなければライト。決めるのは画面の JS。`static/js/inspection.js`)。
前の版は `dark_mode`(真偽)に覚えていて、明暗のボタンを1度でも押した端末・配布設定で
配られた端末は、値が入っているだけで「選んだ」扱いになり、大設定の既定を一切受けなかった
(しかも選ばない状態へ戻る手段が無かった。看板 VER3.5.0 と同じ形)。そこで
    ・前の版の `dark_mode` は**読み込んだときに一度だけ**移す。ライト(= 既定の値)は
      選んだに数えずに捨てる。ダークだけ「ダークを選んだ」として `theme_choice` に移す
    ・ボタンの回り順に「◇ 既定」(選ばない = 大設定に従う)を足した
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.repositories.settings_repository import SettingsRepository

#: 画面の明るさで選べるもの。`theme_choice` が無い(= "default")なら大設定の既定に従う
THEMES = ("light", "dark")
THEME_DEFAULT = "default"
#: 前の版が明暗を覚えていた鍵(真偽)。読み込んだときに一度だけ `theme_choice` へ移す
LEGACY_DARK_MODE = "dark_mode"

APP_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = Path(os.environ.get("INSPECTION_BUSINESS_CONFIG",
                                  str(APP_ROOT / "config" / "inspection.json")))


#: 一覧に載せる拡張子の既定(Excel と Word)。Word の点検表は Word で開く(VER2.3.0)
DEFAULT_EXTENSIONS = ("xlsm", "xlsx", "xls", "doc", "docx", "docm")
#: 前の版の既定(Excel だけ)。設定ファイルがこれのままなら、Word も足して読む
OLD_DEFAULT_EXTENSIONS = ("xlsm", "xlsx", "xls")


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
    # この PC の Excel・Word でその点検表が開いたままなら、保存せずに閉じてから開く
    # (点検表は原紙で、印刷するだけ。閉じるのはそのファイルだけ。VER2.3.2)
    close_open_copy: bool = True
    # Word の点検表のプレビューは PDF(ExportAsFixedFormat)。画面が PDF のまま出す(VER2.4.0)
    word_preview_pdf: bool = True
    # PDF に入れるページ数の上限(0 = 全ページ)
    word_preview_max_pages: int = 20


@dataclass
class InspectionConfig:
    root_folder: str = ""
    extensions: List[str] = field(default_factory=lambda: list(DEFAULT_EXTENSIONS))
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
        # 前の版の既定のまま(アプリのフォルダの設定ファイルを入れ替えていない端末)なら、
        # Word も一覧に載せる。自分で決めた並びは触らない
        if sorted(cfg.extensions) == sorted(OLD_DEFAULT_EXTENSIONS):
            cfg.extensions = list(DEFAULT_EXTENSIONS)
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
        close_open_copy=bool(ex.get("close_open_copy", d.close_open_copy)),
        word_preview_pdf=bool(ex.get("word_preview_pdf", d.word_preview_pdf)),
        word_preview_max_pages=int(_clamp(ex.get("word_preview_max_pages"), d.word_preview_max_pages, 0, 1000)),
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
        self._data = self._migrate_theme(repository.load())

    def _migrate_theme(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """前の版の `dark_mode` を一度だけ移す(上の【画面の明るさ】)。

        ライト(False)は既定の値なので「選んだ」に数えない ── 値が入っているだけで大設定の
        既定を受けなくなっていた。ダーク(True)だけ、ダークを選んだものとして残す。
        """
        if LEGACY_DARK_MODE not in data:
            return data
        data = dict(data)
        legacy = data.pop(LEGACY_DARK_MODE)
        if legacy is True and data.get("theme_choice") not in THEMES:
            data["theme_choice"] = "dark"
        try:
            self.repo.save(data)
        except OSError as exc:                       # 書けなくても、この起動のあいだは移したもので動く
            self.log.warning("前の版の明暗の設定を移せませんでした: %s", exc)
        self.log.info("前の版の明暗の設定(dark_mode=%s)を移しました: %s", legacy,
                      "ダークを選んだもの" if data.get("theme_choice") == "dark" else "既定(大設定に従う)")
        return data

    @property
    def default_root_folder(self) -> str:
        return normalize_folder(self.cfg.root_folder)

    def effective_root_folder(self) -> str:
        if self.forced_root_folder:
            return self.forced_root_folder
        with self._lock:
            user = normalize_folder(str(self._data.get("root_folder") or ""))
        return user or self.default_root_folder

    def theme_choice(self) -> str:
        """この端末で選んだ明暗(light / dark)。選んでいなければ "default"(大設定の既定に従う)。"""
        with self._lock:
            value = self._data.get("theme_choice")
        return value if value in THEMES else THEME_DEFAULT

    def dark_mode(self) -> bool:
        """サーバが描くときの明暗。選んでいなければライト(大設定の既定は画面が描く前に当てる)。"""
        # 既定はライト(日報複合ツールの4ツールでそろえる。VBA版の既定はダークだった)
        return self.theme_choice() == "dark"

    def theme_chosen(self) -> bool:
        """この端末で明暗を選んだか。選んでいなければ、日報複合ツールの大設定の既定に従う。"""
        return self.theme_choice() != THEME_DEFAULT

    def to_dict(self) -> Dict[str, Any]:
        if self.forced_root_folder:
            return {"root_folder": self.forced_root_folder, "root_folder_default": self.default_root_folder,
                    "root_folder_source": "demo", "dark_mode": self.dark_mode(), "theme": self.theme_choice()}
        with self._lock:
            user_root = normalize_folder(str(self._data.get("root_folder") or ""))
        return {
            "root_folder": user_root or self.default_root_folder,
            "root_folder_default": self.default_root_folder,
            "root_folder_source": "user" if user_root else "config",
            "dark_mode": self.dark_mode(),
            "theme": self.theme_choice(),
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
               theme: Optional[str] = None) -> Dict[str, Any]:
        """`theme` は light / dark(選んだ)か "default"(覚えを消す = 大設定の既定に従う)。"""
        if theme is not None and theme not in THEMES + (THEME_DEFAULT,):
            raise ValueError(f"知らない明暗です: {theme!r}")
        with self._lock:
            data = dict(self._data)
            if reset_root_folder:
                data.pop("root_folder", None)
                self.log.info("点検表ルートフォルダを既定値に戻しました")
            elif root_folder is not None:
                folder = normalize_folder(root_folder)
                data["root_folder"] = folder
                self.log.info("点検表ルートフォルダを変更しました: %s", folder)
            if theme == THEME_DEFAULT:
                data.pop("theme_choice", None)
            elif theme is not None:
                data["theme_choice"] = theme
            self.repo.save(data)
            self._data = data
        return self.to_dict()
