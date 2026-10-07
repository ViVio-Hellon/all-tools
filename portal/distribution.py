"""配布設定 ── 大設定で決めたこと(共有の DB の置き場所・管理者パスワード)を、配った先の端末でそのまま使う

作りは各ツール・python-web-tools の配布設定と同じ。

【流れ】
    1. 1台で統合ツールを開き、大設定で鍵を開けて、置き場所・管理者パスワードを決める
    2. 大設定の「配布設定を書き出す」→ 一式のフォルダの直下(Start.vbs と同じ階層)に
       `配布設定\\` ができる(`統合ツール.json`・`はじめに読む.txt`)
    3. `scripts\\make_dist.bat` で配布用フォルダを作る(この `配布設定\\` も入る)
    4. 配った先は**起動のたびに** `配布設定\\` を見て、**その端末に無い項目だけ**埋める
       (端末で直した値が戻されることはない。揃えたいときは「読み込み直す」)

タブ表示権限の表そのものは共有の DB にあるので、配る必要はない。
各ツールの配布設定(各ツールの設定画面で書き出す `tools\\<ツール>\\配布設定\\`)とは別。

管理者パスワードは撹拌した値(`pbkdf2$…`)で入る。ここから値は読めない。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import admin_password, app_config, identity, user_settings
from .logging_utils import get_logger

log = get_logger("distribution")

FILE_NAME = "統合ツール.json"
README_NAME = "はじめに読む.txt"
FORMAT = 1

#: 入れられるもの: (鍵, 画面の名前)。鍵は `user_settings.json` の鍵そのもの
ITEMS: tuple[tuple[str, str], ...] = (
    (user_settings.KEY_SHARED_DIR, "共有の DB のフォルダ"),
    (user_settings.KEY_SHARED_NAME, "共有の DB のファイル名"),
    (user_settings.KEY_ADMIN_PASSWORD, "管理者パスワード"),
)
ITEM_KEYS = frozenset(key for key, _ in ITEMS)
LABELS = dict(ITEMS)


def directory() -> Path:
    """`配布設定\\` の置き場所(一式のフォルダの直下。`ALLTOOLS_DISTRIBUTION_DIR` で変えられる)。"""
    custom = os.environ.get("ALLTOOLS_DISTRIBUTION_DIR", "").strip()
    return Path(custom) if custom else app_config.APP_ROOT / "配布設定"


def file_path(base: Optional[Path] = None) -> Path:
    return (base or directory()) / FILE_NAME


@dataclass
class Result:
    ok: bool = True
    message: str = ""
    reason: str = ""
    applied: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)      # すでにあったので読まなかったもの

    def to_dict(self) -> dict:
        return {"ok": self.ok, "message": self.message, "reason": self.reason,
                "applied": self.applied, "kept": self.kept}


@dataclass
class Bundle:
    settings: dict[str, Any]
    created_at: str = ""
    created_on: str = ""
    version: str = ""


def _now() -> str:
    return _dt.datetime.now().strftime("%Y/%m/%d %H:%M:%S")


def _present(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def _show(key: str, value: Any) -> str:
    return "(設定済み)" if key == user_settings.KEY_ADMIN_PASSWORD else str(value)


# ------------------------------------------------------------------
# 読む
# ------------------------------------------------------------------
def read() -> Optional[Bundle]:
    """置いてある配布設定。無い・読めない・形が違うなら None。"""
    path = file_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        log.warning("配布設定を読めませんでした: %s", exc)
        return None
    if not isinstance(data, dict) or data.get("format") != FORMAT or not isinstance(data.get("settings"), dict):
        log.warning("配布設定の形が違うため読みません: %s", path)
        return None
    settings = {k: v for k, v in data["settings"].items() if k in ITEM_KEYS and _present(v)}
    if not settings:
        return None
    return Bundle(settings, str(data.get("created_at", "")), str(data.get("created_on", "")),
                  str(data.get("version", "")))


def tool_settings() -> list[dict[str, Any]]:
    """各ツールの配布設定(`tools\\<ツール>\\配布設定\\`)が書き出してあるか。

        少なくとも日報管理ツールでは配布設定してもフォルダは生成されていない

    各ツールの設定画面で書き出すと、**そのツールのフォルダの中**にできます(一式の直下ではない)。
    どこにあるのか・`scripts\\make_dist.bat` で入るのかを大設定で一目で見られるようにします。
    """
    from . import catalog as catalog_mod

    out: list[dict[str, Any]] = []
    for tool in catalog_mod.load().tools:
        folder = Path(tool.dir) / "配布設定"
        item: dict[str, Any] = {"id": tool.id, "title": tool.title, "path": str(folder),
                                "exists": (folder / "設定.json").is_file(),
                                "created_at": "", "created_on": ""}
        if item["exists"]:
            try:
                data = json.loads((folder / "設定.json").read_text(encoding="utf-8-sig"))
                item["created_at"] = str(data.get("created_at", ""))
                item["created_on"] = str(data.get("created_on", ""))
            except (OSError, ValueError):
                pass
        out.append(item)
    return out


def summary() -> dict[str, Any]:
    """大設定に出す、配布設定のいま。**パスワードの値は出さない。**"""
    bundle = read()
    applied = user_settings.get(user_settings.KEY_DISTRIBUTION_APPLIED)
    out: dict[str, Any] = {
        "exists": bundle is not None, "path": str(directory()),
        "applied_at": applied.get("at", "") if isinstance(applied, dict) else "",
        "contents": [], "created_at": "", "created_on": "", "version": "",
        "tools": tool_settings(),
    }
    if bundle is not None:
        out.update(contents=[{"label": LABELS[k], "value": _show(k, v)} for k, v in bundle.settings.items()],
                   created_at=bundle.created_at, created_on=bundle.created_on, version=bundle.version)
    return out


# ------------------------------------------------------------------
# 書き出す(配る側)。鍵が開いているかは呼ぶ側(web)が確かめる
# ------------------------------------------------------------------
def export() -> Result:
    """**この端末のいまの設定**を `配布設定\\` に書き出す(前の中身は置き換える)。

    この端末で変えていない項目(既定のまま)は入れない ── 配った先も同じ既定で動く。
    """
    current = user_settings.load()
    settings = {k: current[k] for k, _ in ITEMS if _present(current.get(k))}
    defaults = [label for k, label in ITEMS if k not in settings]
    if not settings:
        return Result(False, "書き出せる設定がありません(置き場所も管理者パスワードも既定のままです。"
                             "配った先も既定で動くので、配布設定は要りません)。", "nothing")
    target = directory()
    # **作ってから入れ替える。** 途中で失敗して、半分だけ新しい配布設定を残さない
    staging = target.with_name(target.name + ".作成中")
    meta = {"format": FORMAT, "created_at": _now(), "created_on": identity.current().pc_name,
            "version": app_config.version(), "settings": settings}
    try:
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        file_path(staging).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        (staging / README_NAME).write_text(_readme(meta).replace("\n", "\r\n"), encoding="utf-8-sig",
                                          newline="")
        if target.exists():
            shutil.rmtree(target)
        staging.rename(target)
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        return Result(False, f"{target} に書けませんでした: {exc}", "failed")
    user_settings.update({user_settings.KEY_DISTRIBUTION_APPLIED: {"at": meta["created_at"]}})
    names = [LABELS[k] for k in settings]
    message = (f"配布設定を書き出しました({len(names)}項目: {'・'.join(names)})。一式のフォルダの直下の"
               f"「{target.name}」に入っています。配るときは scripts\\make_dist.bat で配布用フォルダを"
               "作ってください(このフォルダも入ります)。")
    if defaults:
        message += f" 既定のままなので入れていないもの: {'・'.join(defaults)}。"
    folder = str(settings.get(user_settings.KEY_SHARED_DIR, ""))
    if len(folder) >= 2 and folder[1] == ":" and folder[0].isalpha():
        message += (r" 注意: 共有の DB のフォルダがドライブ文字(Z:\ など)で書かれています。"
                    r"PC ごとに割り当てが違うことがあるので、\\サーバ名\共有 の形をおすすめします。")
    log.info("配布設定を書き出しました: %s", ", ".join(names))
    return Result(True, message, applied=names)


def _readme(meta: dict[str, Any]) -> str:
    lines = [f"{app_config.display_name()} 配布設定", "",
             f"作成: {meta['created_at']}({meta['created_on']} / VER{meta['version']})", "",
             "このフォルダに入っているもの"]
    lines += [f"  {LABELS[k]}: {_show(k, v)}" for k, v in meta["settings"].items()]
    lines += ["", "配った先で起きること",
              "  起動したときにこのフォルダを読み込みます。",
              "  その端末にすでにある設定は、読み込みません(上書きしない)。",
              "  揃えたいときは、大設定の「配布設定」→「読み込み直す」。", "",
              "管理者パスワードは撹拌した値が入っています(ここから値は読めません)。",
              f"{FILE_NAME} は手で書き換えず、大設定から書き出し直してください。", ""]
    return "\n".join(lines)


def remove() -> Result:
    target = directory()
    try:
        if target.exists():
            shutil.rmtree(target)
    except OSError as exc:
        return Result(False, f"消せませんでした: {exc}", "failed")
    log.info("配布設定を消しました")
    return Result(True, "配布設定を消しました。この端末の設定はそのままです。")


# ------------------------------------------------------------------
# 読み込む(配られた側)
# ------------------------------------------------------------------
def _apply(bundle: Bundle, *, overwrite: bool) -> Result:
    result = Result()
    current = user_settings.load()
    values: dict[str, Any] = {}
    for key, value in bundle.settings.items():
        if _present(current.get(key)) and not overwrite:
            result.kept.append(LABELS[key])
            continue
        if key == user_settings.KEY_ADMIN_PASSWORD and not str(value).startswith(admin_password.SCHEME + "$"):
            # 手で平文を書かれていても、**端末には撹拌して持つ**
            value = admin_password.hash_text(str(value))
        values[key] = value
        result.applied.append(LABELS[key])
    if values:
        values[user_settings.KEY_DISTRIBUTION_APPLIED] = {"at": _now()}
        user_settings.update(values)
    return result


def apply_on_start() -> Result:
    """起動時に呼ぶ。`配布設定\\` があれば、**その端末に無いものだけ**読む。読めなくても止めない。"""
    try:
        bundle = read()
        if bundle is None:
            return Result(True, "")
        result = _apply(bundle, overwrite=False)
    except Exception as exc:                      # noqa: BLE001 - 起動を止めない
        log.warning("配布設定を読み込めませんでした: %s", exc)
        return Result(False, str(exc), "failed")
    if result.applied:
        result.message = "配布設定を読み込みました"
        log.info("配布設定を読み込みました: %s(すでにあったので読まなかったもの: %s)",
                 ", ".join(result.applied), ", ".join(result.kept) or "なし")
    return result


def reapply() -> Result:
    """大設定から。**すでにあるものも上書きして**読み込み直す。"""
    bundle = read()
    if bundle is None:
        return Result(False, "配布設定が置かれていません。", "missing")
    result = _apply(bundle, overwrite=True)
    result.message = f"配布設定を読み込み直しました({len(result.applied)}項目)"
    log.info("配布設定を読み込み直しました: %s", ", ".join(result.applied))
    return result
