"""配布設定 ── 1台で決めた設定を、配った先の端末でそのまま使う

【なぜ要るのか】
設定(取り込み元の置き場所・自動同期・管理者パスワード)は端末ごとの
``%LOCALAPPDATA%\\LineCalendar\\data\\settings.json`` に入ります。
配った先で1台ずつ設定画面を開いて打ち直すのは手間で、打ち間違えると
**別の取り込み元へ送る**端末ができてしまいます(置き場所は送り先そのもの)。

【流れ】
    1. 1台で起動して設定し、設定画面の「配布設定」で書き出す
    2. ツールのフォルダの直下(``start.bat`` と同じ階層)に ``配布設定\\`` ができ、
       配るものが全部そこに入る
    3. フォルダごと配る(``tools\\make_dist.bat`` を使うと、配ってはいけない
       ものが紛れない)
    4. 配った先は起動したとき ``配布設定\\`` を見つけて読み込む

【``配布設定\\`` の中身】**配布先に関わるものはここだけ**に置きます。

    配布設定\\
      設定.json          置き場所・自動同期・取り込み間隔・
                         管理者パスワード(撹拌した値)・ライン(選んだときだけ)
      はじめに読む.txt   何が入っているか・配った先で何が起きるか

【読み込むときの決まり】**その端末にすでにあるものは読み込みません。**

    設定 … その端末で値が入っている項目はそのまま。無い項目だけ埋める

起動のたびに見に行きますが、埋まった項目は次から「すでにある」ので、
端末で直した値が戻されることはありません。狙って揃えたいときは、
設定画面の「配布設定を読み込み直す」(管理者パスワード)で上書きします。

【パスワード】書き出す・消す・読み込み直すには管理者パスワードが要ります。
起動時の読み込みには要りません ── ``配布設定\\`` を置いたのは、フォルダを
配った管理者本人だからです。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import admin_password, app_config, settings as user_settings
from .logging_utils import get_logger

log = get_logger("distribution")

def integrated_where(folder) -> str:
    """日報複合ツールの一式の中(`<一式>\\tools\\<ツール>\\配布設定`)なら、置き場所と配り方の一言。違えば空。

    日報複合ツールでは、各ツールの配布設定は**そのツールのフォルダの中**にできます(一式の直下ではない)。
    単品のころの「起動用のファイルと同じフォルダ」と書くと、一式の直下を探して見つからない。
    """
    from pathlib import Path as _Path

    folder = _Path(folder)
    tool_dir = folder.parent
    root = tool_dir.parent.parent
    if tool_dir.parent.name != "tools" or not (root / "config" / "tools.json").is_file():
        return ""
    return (f"日報複合ツールの一式の中の「tools\\{tool_dir.name}\\{folder.name}」に入っています({folder})。"
            "配るときは一式の scripts\\make_dist.bat を実行してください"
            "(大設定と各ツールの配布設定がまとめて入ります。大設定の「配布設定」に一覧があります)。")


# ``配布設定\\`` の置き場所(ツールのフォルダの直下 = ``start.bat`` と同じ階層)
DIR = Path(os.environ.get("CALENDAR_DISTRIBUTION_DIR",
                          str(app_config.APP_ROOT / "配布設定")))
SETTINGS_NAME = "設定.json"
README_NAME = "はじめに読む.txt"

FORMAT = 1

# この端末が最後に読み込んだとき(設定画面に出すだけ)
KEY_APPLIED = "distribution_applied"

# 入れられるもの: (鍵, 画面の名前, 既定で入れるか)
#
# 鍵は ``settings.json`` の鍵そのもの。**ラインだけは既定で外す**(端末ごとに違う)
ITEMS: tuple[tuple[str, str, bool], ...] = (
    (user_settings.KEY_DATA_DB_DIR, "保存用DBの置き場所", True),
    (user_settings.KEY_MASTER_DB_DIR, "マスタDBの置き場所", True),
    (user_settings.KEY_AUTO_SYNC, "自動同期", True),
    (user_settings.KEY_SYNC_INTERVAL, "取り込み間隔(秒)", True),
    (user_settings.KEY_LOG_DIR, "ログフォルダ", True),
    (admin_password.KEY, "管理者パスワード", True),
    (user_settings.KEY_MY_LINE, "ライン(端末ごとに違う)", False),
)
ITEM_KEYS = frozenset(key for key, _, _ in ITEMS)
ITEM_LABELS = {key: label for key, label, _ in ITEMS}

#: 置き場所の項目(ドライブ文字で書かれていたら、書き出すときに注意する)
PATH_KEYS = (user_settings.KEY_DATA_DB_DIR, user_settings.KEY_MASTER_DB_DIR,
             user_settings.KEY_LOG_DIR)

REFUSE_NEED_PASSWORD = admin_password.REFUSE_NEED_PASSWORD
REFUSE_WRONG_PASSWORD = admin_password.REFUSE_WRONG
REFUSE_BAD_INPUT = "bad_input"
REFUSE_FAILED = "failed"


def settings_path(base: Optional[Path] = None) -> Path:
    return (base or DIR) / SETTINGS_NAME


@dataclass
class Result:
    ok: bool = True
    message: str = ""
    reason: str = ""
    applied: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)   # すでにあったので読まなかったもの


def _now() -> str:
    return _dt.datetime.now().strftime("%Y/%m/%d %H:%M:%S")


def _present(value: Any) -> bool:
    """値が入っているか。**空文字は「入っていない」**(この端末では、
    参照パスやパスワードを既定に戻すと空文字で残る)。"""
    return value is not None and value != ""


def _guard(password: str, what: str) -> Optional[Result]:
    """管理者パスワード。「要る」と「違う」は言い分ける(``admin_password.guard``)。"""
    blocked = admin_password.guard(["distribution"], password)
    if blocked is None:
        return None
    message = (f"配布設定を{what}には管理者パスワードが要ります。"
               if blocked.reason == REFUSE_NEED_PASSWORD else blocked.message)
    return Result(False, message, blocked.reason)


# ------------------------------------------------------------------
# 読む
# ------------------------------------------------------------------
@dataclass
class Bundle:
    """置いてある ``配布設定\\`` の中身。"""

    settings: dict[str, Any]
    created_at: str = ""
    created_on: str = ""
    version: str = ""


def read() -> Optional[Bundle]:
    """置いてある配布設定。無い・読めない・形が違うなら None。"""
    if not DIR.is_dir():
        return None
    path = settings_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        log.warning("配布設定を読めませんでした: %s", exc)
        return None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        log.warning("配布設定の形が違うため読みません: %s", path)
        return None
    raw = data.get("settings") or {}
    if not isinstance(raw, dict):
        return None
    settings = {k: v for k, v in raw.items()
                if k in ITEM_KEYS and _present(v)}          # 知らない鍵は捨てる
    if not settings:
        return None
    return Bundle(settings=settings,
                  created_at=str(data.get("created_at", "")),
                  created_on=str(data.get("created_on", "")),
                  version=str(data.get("version", "")))


def summary() -> dict[str, Any]:
    """設定画面に出す、配布設定のいま。**パスワードの値は出さない。**"""
    bundle = read()
    applied = user_settings.get(KEY_APPLIED, None)
    out: dict[str, Any] = {
        "exists": bundle is not None,
        "path": str(DIR),
        "items": [{"key": k, "label": label, "default": default}
                  for k, label, default in ITEMS],
        "applied_at": applied.get("at", "") if isinstance(applied, dict) else "",
        "contents": [],
        "created_at": "",
        "created_on": "",
        "version": "",
    }
    if bundle is None:
        return out
    contents = [{"label": ITEM_LABELS[key],
                 "value": "(設定済み)" if key == admin_password.KEY else _show(value)}
                for key, value in bundle.settings.items()]
    out.update(contents=contents, created_at=bundle.created_at,
               created_on=bundle.created_on, version=bundle.version)
    return out


def _show(value: Any) -> str:
    if isinstance(value, bool):
        return "する" if value else "しない"
    text = str(value)
    return text if text else "(既定)"


# ------------------------------------------------------------------
# 書き出す(配る側)
# ------------------------------------------------------------------
def export(password: str, items: list[str]) -> Result:
    """**この端末のいまの設定**を ``配布設定\\`` に書き出す(前の中身は置き換える)。

    この端末で一度も変えていない項目は入れない ── 配った先も同じ既定で
    動くので要らない(空で上書きしないためにも入れない)。
    """
    blocked = _guard(password, "書き出す")
    if blocked is not None:
        return blocked
    unknown = [k for k in items if k not in ITEM_KEYS]
    if unknown:
        return Result(False, f"知らない項目です: {', '.join(unknown)}",
                      REFUSE_BAD_INPUT)
    if not items:
        return Result(False, "入れる項目を1つ以上選んでください。", REFUSE_BAD_INPUT)

    current = user_settings.load()
    settings = {k: current[k] for k in items if _present(current.get(k))}
    defaults = [ITEM_LABELS[k] for k in items if not _present(current.get(k))]
    if not settings:
        return Result(False, "書き出せる設定がありません(" + "・".join(defaults)
                      + " はこの端末で変えていないため既定のままです)。",
                      REFUSE_BAD_INPUT)

    from . import terminals

    # **作ってから入れ替える。** 途中で失敗して、半分だけ新しい配布設定を
    # 残さない(配った先がそれを読んでしまう)
    staging = DIR.with_name(DIR.name + ".作成中")
    try:
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        meta = {"format": FORMAT, "created_at": _now(),
                "created_on": terminals.identity().pc_name,
                "version": app_config.version(), "settings": settings}
        settings_path(staging).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        (staging / README_NAME).write_text(_readme(meta), encoding="utf-8-sig")
        if DIR.exists():
            shutil.rmtree(DIR)
        staging.rename(DIR)
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        return Result(False, f"{DIR} に書けませんでした: {exc}", REFUSE_FAILED)
    _mark_applied()

    names = [ITEM_LABELS[k] for k in settings]
    message = (f"配布設定を書き出しました({len(names)}項目)。"
               + (integrated_where(DIR)
                  or (f"ツールの直下の「{DIR.name}」フォルダに入っています({DIR})。"
                      "配るときは tools\\make_dist.bat で配布用フォルダを作ってください(このフォルダも入ります)。")))
    if defaults:
        message += (" 既定のままなので入れていないもの(配った先も既定で動きます): "
                    + "・".join(defaults) + "。")
    drives = [ITEM_LABELS[k] for k in PATH_KEYS
              if k in settings and _is_drive_path(str(settings[k]))]
    if drives:
        # 書き出しは止めない(全台が同じ割り当てのこともある)。**気づかせる**
        message += (" 注意: " + "・".join(drives) + r" がドライブ文字(Z:\ など)で"
                    "書かれています。ドライブ文字は PC ごとに割り当てが違うことが"
                    r"あるので、配った先で見つからないときは \\サーバ名\共有 の形で"
                    "決め直してから書き出してください。")
    log.info("配布設定を書き出しました: %s", ", ".join(names))
    return Result(True, message, applied=names)


def _is_drive_path(text: str) -> bool:
    r"""``Z:\...`` のようにドライブ文字で始まるか(``\\サーバ\共有`` や相対は違う)。"""
    return len(text) >= 2 and text[1] == ":" and text[0].isalpha()


def _readme(meta: dict[str, Any]) -> str:
    lines = [
        f"{app_config.display_name()} 配布設定",
        "",
        f"作成: {meta['created_at']}({meta['created_on']} / VER{meta['version']})",
        "",
        "このフォルダに入っているもの",
    ]
    for key, value in meta["settings"].items():
        shown = "(設定済み)" if key == admin_password.KEY else _show(value)
        lines.append(f"  {ITEM_LABELS[key]}: {shown}")
    lines += [
        "",
        "配った先で起きること",
        "  起動したときにこのフォルダを読み込みます。",
        "  その端末にすでにある設定は、読み込みません(上書きしない)。",
        "  揃えたいときは、設定画面の「配布設定」→「配布設定を読み込み直す」。",
        "",
        "管理者パスワードは撹拌した値が入っています(ここから値は読めません)。",
        "設定.json は手で書き換えず、設定画面から書き出し直してください。",
        "",
    ]
    return "\n".join(lines)


def remove(password: str) -> Result:
    blocked = _guard(password, "消す")
    if blocked is not None:
        return blocked
    try:
        if DIR.exists():
            shutil.rmtree(DIR)
    except OSError as exc:
        return Result(False, f"消せませんでした: {exc}", REFUSE_FAILED)
    log.info("配布設定を消しました")
    return Result(True, "配布設定を消しました。この端末の設定はそのままです。")


# ------------------------------------------------------------------
# 読み込む(配られた側)
# ------------------------------------------------------------------
def _mark_applied() -> None:
    user_settings.set_value(KEY_APPLIED, {"at": _now()})


def _apply(bundle: Bundle, *, overwrite: bool) -> Result:
    result = Result()
    # 読んでから書くまでを錠の中で(あいだに入った保存を古い値で消さない)
    with user_settings.LOCK:
        _apply_locked(bundle, overwrite, result)
    if result.applied:
        if ITEM_LABELS[user_settings.KEY_LOG_DIR] in result.applied:
            # ログの書き先が変わった。**その場で付け替える**(次の起動を待つと、
            # 配った初日のログが指定先に無い)
            from . import logging_utils

            logging_utils.reconfigure()
    return result


def _apply_locked(bundle: Bundle, overwrite: bool, result: Result) -> None:
    current = user_settings.load()
    for key, value in bundle.settings.items():
        if _present(current.get(key)) and not overwrite:
            result.kept.append(ITEM_LABELS[key])        # すでにある
            continue
        if key == admin_password.KEY and not str(value).startswith(
                admin_password.SCHEME + "$"):
            # 手で平文を書かれていても、**端末には撹拌して持つ**
            value = admin_password.hash_for_distribution(str(value))
        current[key] = value
        result.applied.append(ITEM_LABELS[key])
    if result.applied:
        current[KEY_APPLIED] = {"at": _now()}
        user_settings.save(current)


def apply_on_start() -> Result:
    """起動時に呼ぶ。``配布設定\\`` があれば、**その端末に無いものだけ**読む。"""
    bundle = read()
    if bundle is None:
        return Result(True, "")
    result = _apply(bundle, overwrite=False)
    if result.applied:
        log.info("配布設定を読み込みました: %s(すでにあったので読まなかったもの: %s)",
                 ", ".join(result.applied), ", ".join(result.kept) or "なし")
        result.message = "配布設定を読み込みました"
    return result


def reapply(password: str) -> Result:
    """設定画面から。**すでにあるものも上書きして**読み込み直す。"""
    blocked = _guard(password, "読み込み直す")
    if blocked is not None:
        return blocked
    bundle = read()
    if bundle is None:
        return Result(False, "配布設定が置かれていません。", REFUSE_BAD_INPUT)
    result = _apply(bundle, overwrite=True)
    result.message = f"配布設定を読み込みました({len(result.applied)}項目)"
    log.info("配布設定を読み込み直しました: %s", ", ".join(result.applied))
    return result
