"""配布設定 ── 1台で決めた設定を、ほかのラインの端末でそのまま使う
(python-web-tools の `packaging_tool/distribution.py` を移植)

【なぜ要るのか】
設定(点検表フォルダ・管理者パスワード・表示)は、端末ごと・利用者ごとの
`%LOCALAPPDATA%\\InspectionSheetPrint\\data\\user_settings.json` に入ります。
ラインが増えるたびに1台ずつ設定画面を開いて打ち直すのは手間で、打ち間違えると
**別のフォルダの点検表を印刷する**ラインができてしまいます。

【流れ】
    1. 1台で起動して設定し、設定画面の「配布設定」で書き出す
    2. アプリのフォルダの直下に `配布設定\\` ができ、配るものが全部そこに入る
    3. アプリを共有フォルダに置いているなら、それで各ラインに届く
       (各PCへコピーして配るなら、フォルダごとコピーする)
    4. 各ラインは起動したとき `配布設定\\` を見つけて読み込む

【`配布設定\\` の中身】

    配布設定\\
      設定.json          点検表フォルダ・管理者パスワード(撹拌した値)・表示(選んだときだけ)
      はじめに読む.txt   何が入っているか・配った先で何が起きるか

`config\\` の設定(Excel の扱い・部数の上限など)はアプリのフォルダに入って
いるので、フォルダごと配れば届きます。ここに入れるのは**端末ごとに保存される
値**だけです。

【読み込むときの決まり】**その端末にすでにあるものは読み込みません。**
その端末で値が入っている項目はそのまま、無い項目だけ埋めます。起動のたびに
見に行きますが、埋まった項目は次から「すでにある」ので、端末で直した値が
戻されることはありません。狙って揃えたいときは、設定画面の「配布設定を
読み込み直す」(管理者パスワード)で上書きします。

【パスワード】書き出す・消す・読み込み直すには管理者パスワードが要ります。
起動時の読み込みには要りません ── `配布設定\\` を置いたのは、フォルダを
配った管理者本人だからです。
"""
from __future__ import annotations

import json
import os
import platform
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.services import admin_password
from core import app_config

DIR_NAME = "配布設定"
SETTINGS_NAME = "設定.json"
README_NAME = "はじめに読む.txt"
FORMAT = 1


def default_dir() -> Path:
    """`配布設定\\` の置き場所(アプリのフォルダの直下)。試験では差し替える。"""
    override = (os.environ.get("INSPECTION_DISTRIBUTION_DIR") or "").strip()
    # 空で渡されたら使わない(`Path("")` は「いまのフォルダ」になってしまう)
    return Path(override) if override else app_config.APP_ROOT / DIR_NAME


# この端末が最後に読み込んだとき(設定画面に出すだけ)
KEY_APPLIED = "distribution_applied"


def _is_folder(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _is_keep_days(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 7 <= value <= 3650


# 入れられるもの: (鍵, 画面の名前, 既定で入れるか, 値の確かめ方)
#
# 鍵は `user_settings.json` の鍵そのもの。**表示だけは既定で外す**(人ごとの好み)
ITEMS: tuple = (
    ("root_folder", "点検表フォルダ", True, _is_folder),
    (admin_password.KEY, "管理者パスワード", True, admin_password.looks_stored),
    ("dark_mode", "表示(ダーク/ライト)", False, _is_bool),
    # 共有フォルダを指定して配れば、全ラインのログが1か所に集まる(後追いのため)
    ("log_dir", "ログの保存先", True, _is_folder),
    ("log_keep_days", "ログの保存日数", True, _is_keep_days),
)
ITEM_KEYS = frozenset(key for key, *_ in ITEMS)
ITEM_LABELS = {key: label for key, label, *_ in ITEMS}
ITEM_CHECKS: Dict[str, Callable[[Any], bool]] = {key: check for key, _, _, check in ITEMS}

REFUSE_NEED_PASSWORD = "need_password"
REFUSE_BAD_INPUT = "bad_input"
REFUSE_FAILED = "failed"


@dataclass
class Result:
    ok: bool = True
    message: str = ""
    reason: str = ""
    applied: List[str] = field(default_factory=list)
    kept: List[str] = field(default_factory=list)      # すでにあったので読まなかったもの
    root_changed: bool = False                           # 点検表フォルダが替わった(一覧を読み直す)
    logs_changed: bool = False                           # ログの保存先・日数が替わった(書き出しに反映)


@dataclass
class Bundle:
    """置いてある `配布設定\\` の中身。"""
    settings: Dict[str, Any]
    created_at: str = ""
    created_on: str = ""
    created_version: str = ""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _show(key: str, value: Any) -> str:
    if key == admin_password.KEY:
        return "(設定済み)"                              # **値は出さない**
    if key == "dark_mode":
        return "ダーク" if value else "ライト"
    if key == "log_keep_days":
        return f"{value} 日"
    if isinstance(value, bool):
        return "する" if value else "しない"
    text = str(value)
    return text if text else "(既定)"


class Distribution:
    def __init__(self, settings: Any, admin: Any, logger: Any, base_dir: Optional[Path] = None):
        self.settings = settings
        self.admin = admin
        self.log = logger
        self.dir = Path(base_dir) if base_dir else default_dir()

    @property
    def settings_path(self) -> Path:
        return self.dir / SETTINGS_NAME

    # ------------------------------------------------------------------
    # 読む
    # ------------------------------------------------------------------
    def read(self) -> Optional[Bundle]:
        """置いてある配布設定。無い・読めない・形が違う・ほかの道具のものなら None。"""
        path = self.settings_path
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            self.log.warning("配布設定を読めませんでした: %s", exc)
            return None
        if not isinstance(data, dict) or data.get("format") != FORMAT:
            self.log.warning("配布設定の形が違うため読みません: %s", path)
            return None
        if data.get("app_id") not in (None, app_config.app_id()):
            # 同じ名前のフォルダでも、ほかの道具(梱包資材総合ツールなど)のものは読まない
            self.log.warning("ほかの道具の配布設定なので読みません: %s", data.get("app_id"))
            return None
        raw = data.get("settings") or {}
        settings = {k: v for k, v in raw.items()
                    if k in ITEM_KEYS and ITEM_CHECKS[k](v)}           # 知らない鍵・壊れた値は捨てる
        if not settings:
            return None
        return Bundle(settings=settings, created_at=str(data.get("created_at", "")),
                      created_on=str(data.get("created_on", "")),
                      created_version=str(data.get("app_version", "")))

    def summary(self) -> Dict[str, Any]:
        """設定画面に出す、配布設定のいま。**パスワードの値は出さない。**"""
        bundle = self.read()
        applied = self.settings.get(KEY_APPLIED)
        current = self.settings.values()
        out: Dict[str, Any] = {
            "exists": bundle is not None,
            "path": str(self.dir),
            "items": [{"key": k, "label": label, "default": default,
                       "set": k in current,
                       "current": _show(k, current[k]) if k in current else "(既定のまま)"}
                      for k, label, default, _ in ITEMS],
            "applied_at": applied.get("at", "") if isinstance(applied, dict) else "",
            "contents": [],
            "created_at": "",
            "created_on": "",
            "created_version": "",
        }
        if bundle is None:
            return out
        out.update(contents=[{"label": ITEM_LABELS[k], "value": _show(k, v)}
                             for k, v in bundle.settings.items()],
                   created_at=bundle.created_at, created_on=bundle.created_on,
                   created_version=bundle.created_version)
        return out

    # ------------------------------------------------------------------
    # 書き出す(配る側)
    # ------------------------------------------------------------------
    def export(self, password: str, items: List[str]) -> Result:
        """**この端末のいまの設定**を `配布設定\\` に書き出す(前の中身は置き換える)。

        この端末で一度も変えていない項目は入れない ── 配った先も同じ既定で
        動くので要らない(空で上書きしないためにも入れない)。
        """
        if not self.admin.verify(str(password or "")):
            return Result(False, "配布設定を書き出すには管理者パスワードが要ります。", REFUSE_NEED_PASSWORD)
        unknown = [k for k in items if k not in ITEM_KEYS]
        if unknown:
            return Result(False, f"知らない項目です: {', '.join(unknown)}", REFUSE_BAD_INPUT)
        if not items:
            return Result(False, "入れる項目を1つ以上選んでください。", REFUSE_BAD_INPUT)

        current = self.settings.values()
        settings = {k: current[k] for k in items if k in current and ITEM_CHECKS[k](current[k])}
        defaults = [ITEM_LABELS[k] for k in items if k not in settings]
        if not settings:
            return Result(False, "書き出せる設定がありません(" + "・".join(defaults)
                          + " はこの端末で変えていないため既定のままです)。", REFUSE_BAD_INPUT)

        meta = {"format": FORMAT, "app_id": app_config.app_id(), "app_version": app_config.version(),
                "created_at": _now(), "created_on": platform.node(), "settings": settings}
        # **作ってから入れ替える。** 途中で失敗して、半分だけ新しい配布設定を残さない
        # (各ラインがそれを読んでしまう)。作業用の名前はPCごとに分ける ── 共有フォルダで
        # 2台が同時に書き出しても、互いの作りかけを消さない
        staging = self.dir.with_name(f"{self.dir.name}.作成中-{platform.node()}-{os.getpid()}")
        try:
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True)
            (staging / SETTINGS_NAME).write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                                 encoding="utf-8")
            (staging / README_NAME).write_text(self._readme(meta), encoding="utf-8-sig", newline="")
            if self.dir.exists():
                shutil.rmtree(self.dir)
            staging.rename(self.dir)
        except OSError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            return Result(False, f"{self.dir} に書けませんでした: {exc}"
                          "(ほかのPCが読み込んでいる最中なら、少し待ってもう一度押してください)",
                          REFUSE_FAILED)
        self._mark_applied(meta["created_at"])

        names = [ITEM_LABELS[k] for k in settings]
        message = (f"配布設定を書き出しました({len(names)}項目: {'・'.join(names)})。"
                   f"アプリのフォルダの直下の「{self.dir.name}」に入っています。"
                   "各ラインは次に起動したときに読み込みます。")
        if defaults:
            message += " 既定のままなので入れていないもの(配った先も既定で動きます): " + "・".join(defaults) + "。"
        self.log.info("配布設定を書き出しました: %s", ", ".join(names))
        return Result(True, message, applied=names)

    def _readme(self, meta: Dict[str, Any]) -> str:
        lines = [
            f"{app_config.display_name()} 配布設定",
            "",
            f"作成: {meta['created_at']}({meta['created_on']}・VER{meta['app_version']})",
            "",
            "このフォルダに入っているもの",
        ]
        for key, value in meta["settings"].items():
            lines.append(f"  {ITEM_LABELS[key]}: {_show(key, value)}")
        lines += [
            "",
            "配った先で起きること",
            "  起動したときにこのフォルダを読み込みます。",
            "  その端末にすでにある設定は、読み込みません(上書きしない)。",
            "  揃えたいときは、設定の「配布設定」タブ →「配布設定を読み込み直す」。",
            "",
            "このフォルダを消すと、以後は読み込まれません(各端末の設定はそのまま残ります)。",
            "",
        ]
        return "\r\n".join(lines)

    def remove(self, password: str) -> Result:
        if not self.admin.verify(str(password or "")):
            return Result(False, "配布設定を消すには管理者パスワードが要ります。", REFUSE_NEED_PASSWORD)
        try:
            if self.dir.exists():
                shutil.rmtree(self.dir)
        except OSError as exc:
            return Result(False, f"消せませんでした: {exc}", REFUSE_FAILED)
        self.log.info("配布設定を消しました")
        return Result(True, "配布設定を消しました。この端末の設定はそのままです。")

    # ------------------------------------------------------------------
    # 読み込む(配られた側)
    # ------------------------------------------------------------------
    def _mark_applied(self, created_at: str) -> None:
        self.settings.put(KEY_APPLIED, {"at": _now(), "created_at": created_at})

    def _apply(self, bundle: Bundle, *, overwrite: bool) -> Result:
        result = Result()
        current = self.settings.values()
        updates: Dict[str, Any] = {}
        for key, value in bundle.settings.items():
            if key in current and not overwrite:
                result.kept.append(ITEM_LABELS[key])          # すでにある
                continue
            if current.get(key) != value:
                updates[key] = value
            result.applied.append(ITEM_LABELS[key])
        if updates:
            self.settings.put_many(updates)
            result.root_changed = "root_folder" in updates
            result.logs_changed = bool({"log_dir", "log_keep_days"} & set(updates))
            from core import event_log
            event_log.record("settings.distribution", event_log.INFO, action="読み込み",
                             overwrite=overwrite, changed=[ITEM_LABELS[k] for k in updates],
                             kept=result.kept or None, created_at=bundle.created_at)
        if result.applied:
            self._mark_applied(bundle.created_at)
        return result

    def apply_on_start(self) -> Result:
        """起動時に呼ぶ。`配布設定\\` があれば、**その端末に無いものだけ**読む。"""
        bundle = self.read()
        if bundle is None:
            return Result(True, "")
        result = self._apply(bundle, overwrite=False)
        if result.applied:
            self.log.info("配布設定を読み込みました: %s(すでにあったので読まなかったもの: %s)",
                          ", ".join(result.applied), ", ".join(result.kept) or "なし")
            result.message = "配布設定を読み込みました"
        return result

    def reapply(self, password: str) -> Result:
        """設定画面から。**すでにあるものも上書きして**読み込み直す。"""
        if not self.admin.verify(str(password or "")):
            return Result(False, "配布設定を読み込み直すには管理者パスワードが要ります。", REFUSE_NEED_PASSWORD)
        bundle = self.read()
        if bundle is None:
            return Result(False, "配布設定が置かれていません。", REFUSE_BAD_INPUT)
        result = self._apply(bundle, overwrite=True)
        result.message = f"配布設定を読み込みました({len(result.applied)}項目: {'・'.join(result.applied)})。"
        self.log.info("配布設定を読み込み直しました: %s", ", ".join(result.applied))
        return result
