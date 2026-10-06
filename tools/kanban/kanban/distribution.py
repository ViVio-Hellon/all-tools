"""配布設定 ── 1 台で決めた設定を、配った先の端末でそのまま使う

python-web-tools(梱包資材総合ツール)の ``packaging_tool/distribution.py`` と
同じつくりです。

【なぜ要るのか】
設定(接続先・取り込み/書き戻し間隔・自動印刷・管理者パスワード)は端末ごとの
``%APPDATA%\\KanbanSystem\\config.json`` に入ります。配った先で 1 台ずつ設定画面を開いて打ち直すのは
手間で、打ち間違えると**別のファイルを読み書きする**端末ができてしまいます
(接続先は取り込みと書き戻しの相手そのもの)。しかも管理者パスワードが空の
まま配ると、**最初に押した人がパスワードを決められます。**

【流れ】
    1. 1 台で起動して設定し、設定画面の「配布設定」で書き出す
    2. アプリのフォルダの直下に ``配布設定\\`` ができ、配るものが全部そこに入る
    3. フォルダごと配る(「配布用フォルダを作る」/ ``scripts\\make_dist.bat`` を
       使うと、配るものだけが入る)
    4. 配った先は起動したとき ``配布設定\\`` を見つけて読み込む

【``配布設定\\`` の中身】**配布先に関わるものはここだけ**に置きます。

    配布設定\\
      設定.json            接続先・間隔・自動印刷・管理者パスワード(撹拌した値)・
                           担当ライン(選んだときだけ)
      はじめに読む.txt     何が入っているか・配った先で何が起きるか

この端末の設定ファイルはアプリのフォルダの外(利用者ごとのフォルダ)にあるので、
フォルダをコピーしても付いていきません。配るものはここだけです。

【読み込むときの決まり】**その端末にすでにあるものは読み込みません。**
その端末で値が入っている項目はそのまま。無い項目だけ埋めます。

起動のたびに見に行きますが、埋まった項目は次から「すでにある」ので、端末で
直した値が戻されることはありません。狙って揃えたいときは、設定画面の
「配布設定を読み込み直す」(管理者パスワード)で上書きします。

【パスワード】書き出す・消す・読み込み直すには管理者パスワードが要ります
(設定画面の経路が確かめる)。起動時の読み込みには要りません ──
``配布設定\\`` を置いたのは、フォルダを配った管理者本人だからです。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config
from .applog import get_logger

log = get_logger("distribution")

DIR_NAME = "配布設定"
SETTINGS_NAME = "設定.json"
README_NAME = "はじめに読む.txt"
FORMAT = 1

#: 前の版が書いていた形(``配布設定\\config.json``、項目が平らに並ぶ)。読むだけ
LEGACY_SETTINGS_NAME = "config.json"
LEGACY_README_NAME = "説明.txt"

#: 入れられるもの: (鍵, 画面の名前, 既定で入れるか)
#:
#: 鍵は :class:`kanban.config.Config` の項目名そのもの。**担当ラインだけは既定で
#: 外す**(端末ごとに違う。入れたまま配ると、全部の現場が同じラインを名乗る)
ITEMS: tuple[tuple[str, str, bool], ...] = (
    ("shared_db_path", "接続先(共有DB)", True),
    ("access_db_path", "アクセス権限の置き場所(梱包資材マスタ)", True),
    ("history_db_path", "看板履歴の置き場所(看板履歴.sqlite3)", True),
    ("import_interval_sec", "取り込み間隔", True),
    ("export_interval_sec", "書き戻し間隔", True),
    ("auto_print", "自動印刷", True),
    ("mistake_minutes", "押し間違いとみなす時間(看板集計)", True),
    ("admin_password_hash", "管理者パスワード", True),
    ("line", "担当ライン(端末ごとに違う)", False),
    ("csv_dir", "CSV の書き出し先(端末ごとに違ってよい)", False),
    ("log_dir", "記録(ログ)の置き場所(共有フォルダに集めるなら配る)", False),
)
ITEM_KEYS = frozenset(key for key, _, _ in ITEMS)
ITEM_LABELS = {key: label for key, label, _ in ITEMS}

#: この端末が最後に配布設定を読み込んだ日時(この端末の設定ファイルの付記)
META_APPLIED = "_配布設定を読み込んだ日時"


def directory() -> Path:
    """``配布設定\\`` の置き場所(アプリのフォルダの直下)。

    環境変数 ``KANBAN_DISTRIBUTION_DIR`` で差し替えられます(検証用)。
    """
    override = os.environ.get("KANBAN_DISTRIBUTION_DIR")
    if override:
        return Path(override)
    return config.APP_ROOT / DIR_NAME


def settings_path(base: Path | None = None) -> Path:
    return (base or directory()) / SETTINGS_NAME


@dataclass
class Result:
    ok: bool = True
    message: str = ""
    reason: str = ""
    applied: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    """すでにあったので読まなかったもの。"""
    checks: list[dict[str, Any]] = field(default_factory=list)
    """書き出した直後の確認(配った先と同じ読み方で読み戻す)。"""


# ------------------------------------------------------------------
# 読む
# ------------------------------------------------------------------
@dataclass
class Bundle:
    """置いてある ``配布設定\\`` の中身。"""

    settings: dict[str, Any]
    created_at: str = ""
    created_on: str = ""
    legacy: bool = False
    """前の版の形(``config.json``)で置かれていた。"""


def read(base: Path | None = None) -> Bundle | None:
    """置いてある配布設定。無い・読めない・形が違うなら ``None``。"""
    folder = base or directory()
    if not folder.is_dir():
        return None
    path = settings_path(folder)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            log.warning("配布設定を読めませんでした: %s", exc)
            return None
        if not isinstance(data, dict) or data.get("format") != FORMAT:
            log.warning("配布設定の形が違うため読みません: %s", path)
            return None
        return Bundle(
            settings=_clean(data.get("settings") or {}),
            created_at=str(data.get("created_at", "")),
            created_on=str(data.get("created_on", "")),
        )
    legacy = folder / LEGACY_SETTINGS_NAME
    if legacy.is_file():
        # 前の版が書いた形。項目が平らに並び、付記は "_保存日時" などの名前
        try:
            data = json.loads(legacy.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            log.warning("前の版の配布設定を読めませんでした: %s", exc)
            return None
        if not isinstance(data, dict):
            return None
        return Bundle(
            settings=_clean(data),
            created_at=str(data.get("_保存日時", "")),
            created_on=str(data.get("_保存した端末", "")),
            legacy=True,
        )
    return None


def _clean(raw: dict[str, Any]) -> dict[str, Any]:
    """知らない鍵・空欄を捨て、項目の型にそろえる(手で書いた "60" なども読む)。"""
    if not isinstance(raw, dict):
        return {}
    wanted = {
        k: v for k, v in raw.items()
        if k in ITEM_KEYS and str(v if v is not None else "").strip() != ""
    }
    values, _bad = config._clean_values(wanted)
    return values


def terminal_values() -> dict[str, Any]:
    """この端末で**値が入っている**項目(組み込みの既定のままのものは入らない)。"""
    cfg = config.load_config()
    present = dict(config.read_settings_file(Path(cfg.source_path)).values)
    if cfg.line:
        present["line"] = cfg.line
    return {k: v for k, v in present.items() if k in ITEM_KEYS}


def _show(key: str, value: Any) -> str:
    if key == "admin_password_hash":
        return "(設定済み)"
    if key == "line":
        return config.display_name(str(value))
    if isinstance(value, bool):
        return "する" if value else "しない"
    if key.endswith("_sec"):
        return f"{value} 秒"
    if key == "mistake_minutes":
        return f"{value} 分"
    text = str(value)
    return text if text else "(既定)"


def summary() -> dict[str, Any]:
    """設定画面に出す、配布設定のいま。**パスワードの値は出さない。**"""
    bundle = read()
    here = terminal_values()
    builtin = config.Config()
    out: dict[str, Any] = {
        "exists": bundle is not None,
        "path": str(directory()),
        "legacy": bool(bundle and bundle.legacy),
        "items": [
            {
                "key": key, "label": label, "default": default,
                # この端末のいまの値(書き出すとこれが入る)
                "here": _show(key, here[key]) if key in here else "",
                "builtin": (
                    "" if key in here
                    else "未設定" if key in ("admin_password_hash", "line")
                    else _show(key, builtin.resolved_shared_db_path()
                               if key == "shared_db_path"
                               else "共有DBと同じフォルダ" if key in ("access_db_path", "history_db_path")
                               else "この端末のローカル領域の export" if key == "csv_dir"
                               else "この端末のローカル領域の logs" if key == "log_dir"
                               else getattr(builtin, key))
                ),
            }
            for key, label, default in ITEMS
        ],
        "applied_at": _applied_at(),
        "contents": [],
        "created_at": "",
        "created_on": "",
    }
    if bundle is None:
        return out
    out.update(
        contents=[{"label": ITEM_LABELS[k], "value": _show(k, v)}
                  for k, v in bundle.settings.items()],
        created_at=bundle.created_at,
        created_on=bundle.created_on,
    )
    return out


def _applied_at() -> str:
    try:
        return str(config.read_meta(META_APPLIED) or "")
    except OSError:
        return ""


# ------------------------------------------------------------------
# 書き出す(配る側)
# ------------------------------------------------------------------
def export(items: list[str]) -> Result:
    """**この端末のいまの設定**を ``配布設定\\`` に書き出す(前の中身は置き換える)。

    この端末で一度も変えていない項目は入れない ── 配った先も同じ既定で動くので
    要らない(空で上書きしないためにも入れない)。

    管理者パスワードは呼ぶ側(設定画面の経路)が確かめる。
    """
    unknown = [k for k in items if k not in ITEM_KEYS]
    if unknown:
        return Result(False, f"知らない項目です: {', '.join(unknown)}", "bad_input")
    if not items:
        return Result(False, "入れる項目を 1 つ以上選んでください。", "bad_input")

    here = terminal_values()
    settings = {k: here[k] for k in items if k in here}
    defaults = [ITEM_LABELS[k] for k in items if k not in here]
    if not settings:
        return Result(
            False,
            "書き出せる設定がありません(" + "・".join(defaults)
            + " はこの端末で変えていないため既定のままです)。",
            "bad_input",
        )

    folder = directory()
    meta = {
        "format": FORMAT,
        "created_at": datetime.now().strftime("%Y/%m/%d %H:%M:%S"),
        "created_on": socket.gethostname(),
        "settings": settings,
    }
    # **作ってから入れ替える。** 途中で失敗して、半分だけ新しい配布設定を
    # 残さない(配った先がそれを読んでしまう)
    staging = folder.with_name(folder.name + ".作成中")
    try:
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        settings_path(staging).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        # メモ帳で開かれる前提で書く。**BOM 付き UTF-8 + CRLF** ── 古い
        # Windows 10 のメモ帳は BOM の無い UTF-8 の日本語を化けさせる
        (staging / README_NAME).write_text(
            _readme(meta).replace("\n", "\r\n"), encoding="utf-8-sig", newline="")
        if folder.exists():
            shutil.rmtree(folder)
        staging.rename(folder)
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        return Result(False, f"{folder} に書けませんでした: {exc}", "failed")

    checks = verify(settings)
    names = [ITEM_LABELS[k] for k in settings]
    if not all(c["ok"] for c in checks):
        failed = "、".join(c["label"] for c in checks if not c["ok"])
        return Result(False, f"配布設定を書き出しましたが、確認で問題が見つかりました: {failed}",
                      "verify_failed", applied=names, checks=checks)

    message = (
        f"配布設定を書き出しました({len(names)} 項目)。アプリのフォルダの直下の"
        f"「{folder.name}」フォルダに入っています。"
    )
    if defaults:
        message += (" 既定のままなので入れていないもの(配った先も既定で動きます): "
                    + "・".join(defaults) + "。")
    log.info("配布設定を書き出しました: %s", ", ".join(names))
    return Result(True, message, applied=names, checks=checks)


def verify(expected: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """``配布設定\\`` が**本当にできていて、配った先と同じ読み方で読めるか**。

    書き出した直後に呼び、結果を画面に出します。「書き出しました」と言うだけでは、
    フォルダができたのか、中に何が入ったのかを配る人が確かめようがない。
    """
    folder = directory()
    checks: list[dict[str, Any]] = []

    def add(label: str, ok: bool, detail: str = "") -> bool:
        checks.append({"label": label, "ok": ok, "detail": detail})
        return ok

    if not add("「配布設定」フォルダがある", folder.is_dir(), str(folder)):
        return checks
    if not add(f"{SETTINGS_NAME} がある", settings_path().is_file(), str(settings_path())):
        return checks
    bundle = read()
    if not add(f"{SETTINGS_NAME} を読める(配った先と同じ読み方で)", bundle is not None,
               f"{len(bundle.settings)} 項目" if bundle else "読めませんでした"):
        return checks
    assert bundle is not None
    if expected is not None:
        wrong = [ITEM_LABELS[k] for k, v in expected.items() if bundle.settings.get(k) != v]
        add("書き出した値がそのまま入っている", not wrong,
            f"違う: {'・'.join(wrong)}" if wrong else "・".join(ITEM_LABELS[k] for k in expected))
    add("管理者パスワードが入っている", "admin_password_hash" in bundle.settings,
        "" if "admin_password_hash" in bundle.settings
        else "入れずに配ると、配った先では最初に押した人がパスワードを決められます")
    add(f"{README_NAME} がある", (folder / README_NAME).is_file())
    return checks


def _readme(meta: dict[str, Any]) -> str:
    lines = [
        "資材発注看板システム 配布設定",
        "",
        f"作成: {meta['created_at']}({meta['created_on']})",
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
        "  揃えたいときは、設定画面の「配布設定」→「配布設定を読み込み直す」。",
        "",
        "担当ライン・モードは端末ごとに違うので、配った先で選んでください",
        "(担当ラインは、書き出すときに選んだ場合だけ入っています)。",
        "",
        "このフォルダには管理者パスワードのハッシュと社内の共有フォルダのパスが",
        "入っています。関係のない場所へは置かないでください。",
        "",
    ]
    return "\n".join(lines)


def remove() -> Result:
    folder = directory()
    try:
        if folder.exists():
            shutil.rmtree(folder)
    except OSError as exc:
        return Result(False, f"消せませんでした: {exc}", "failed")
    log.info("配布設定を消しました")
    return Result(True, "配布設定を消しました。この端末の設定はそのままです。")


# ------------------------------------------------------------------
# 読み込む(配られた側)
# ------------------------------------------------------------------
def _apply(bundle: Bundle, *, overwrite: bool) -> Result:
    result = Result()
    here = terminal_values()
    cfg = config.load_config()
    for key, value in bundle.settings.items():
        if key in here and not overwrite:
            result.kept.append(ITEM_LABELS[key])       # すでにある
            continue
        setattr(cfg, key, value)
        result.applied.append(ITEM_LABELS[key])
    if result.applied:
        config.save_config(cfg)
        config.write_meta(META_APPLIED, datetime.now().strftime("%Y/%m/%d %H:%M:%S"))
    return result


def apply_on_start() -> Result:
    """起動時に呼ぶ。``配布設定\\`` があれば、**その端末に無いものだけ**読む。"""
    bundle = read()
    if bundle is None:
        return Result(True, "")
    try:
        result = _apply(bundle, overwrite=False)
    except OSError as exc:
        log.warning("配布設定を読み込めませんでした(設定ファイルに書けません): %s", exc)
        return Result(False, f"配布設定を読み込めませんでした: {exc}", "failed")
    if result.applied:
        log.info("配布設定を読み込みました: %s(すでにあったので読まなかったもの: %s)",
                 ", ".join(result.applied), ", ".join(result.kept) or "なし")
        result.message = "配布設定を読み込みました"
    return result


def reapply() -> Result:
    """設定画面から。**すでにあるものも上書きして**読み込み直す。"""
    bundle = read()
    if bundle is None:
        return Result(False, "配布設定が置かれていません。", "bad_input")
    try:
        result = _apply(bundle, overwrite=True)
    except OSError as exc:
        return Result(False, f"この端末の設定に書けませんでした: {exc}", "failed")
    result.message = f"配布設定を読み込みました({len(result.applied)} 項目)。"
    return result
