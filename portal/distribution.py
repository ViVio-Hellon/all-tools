"""配布設定 ── 大設定で決めたこと(共有の DB の置き場所・管理者パスワード)を、配った先の端末でそのまま使う

作りは各ツール・python-web-tools の配布設定と同じ。

【流れ】
    1. 1台で日報複合ツールを開き、大設定で鍵を開けて、置き場所・管理者パスワードを決める
    2. 大設定の「配布設定を書き出す」→ 一式のフォルダの直下(Start.vbs と同じ階層)に
       `配布設定\\` ができる(`日報複合ツール.json`・`はじめに読む.txt`)
    3. `scripts\\make_dist.bat` で配布用フォルダを作る(この `配布設定\\` も入る)
    4. 配った先は**起動のたびに** `配布設定\\` を見て、**その端末に無い項目だけ**埋める
       (端末で直した値が戻されることはない。揃えたいときは「読み込み直す」)。
       配布設定から読み込んだ値のまま(`distribution_values` に覚えておく)の項目は、
       この端末で決めた値ではないので、配り直した配布設定の新しい値に置き換える

タブ表示権限の表そのものは共有の DB にあるので、配る必要はない。
各ツールの配布設定(各ツールの設定画面で書き出す `tools\\<ツール>\\配布設定\\`)とは別。
大設定の「4ツールの分もまとめて書き出す」(`export_all`)は、大設定の分と4ツールの分を
1度に書き出す(各ツールの設定画面で1つずつ書き出すのは手間で、抜けやすい)。

管理者パスワードは撹拌した値(`pbkdf2$…`)で入る。ここから値は読めない。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import admin_password, app_config, identity, user_settings
from .logging_utils import get_logger

log = get_logger("distribution")

FILE_NAME = "日報複合ツール.json"
#: 名前を変える前(1.3.x まで)に書き出した配布設定。読むだけ(次に書き出すと新しい名前になる)
OLD_FILE_NAMES: tuple[str, ...] = ("統合ツール.json",)
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
    #: まとめて書き出すとき、そのツールが前の配布設定から**そのまま残したもの**
    #: (「音声ファイル 3 個」「ライン(L-1)」。各ツールの入口の答えの ``kept``)
    carried: list[str] = field(default_factory=list)

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
def _source() -> tuple[Optional[Path], str]:
    """読む配布設定のファイルと、確かめられなかった理由。

    いまの名前のファイルが**無いと確かめられたときだけ**、前の名前(`統合ツール.json`)を見る。
    以前は `is_file()` で見ていたので、いまのファイルに届かない(共有の瞬断・権限)と「無い」とみなして
    前の名前の古い配布設定を読み、配布設定から入れた値を古い値に戻していた(読めると、また戻る)。
    """
    for path in (file_path(), *(directory() / name for name in OLD_FILE_NAMES)):
        try:
            path.stat()
        except FileNotFoundError:
            if app_config.confirmed_missing(path):
                continue
            return None, f"{path} に届きません(配布設定のフォルダを読めません)"
        except OSError as exc:
            return None, f"{path} を読めません: {exc}"
        return path, ""
    return None, ""


def read() -> Optional[Bundle]:
    """置いてある配布設定。無い・読めない・形が違うなら None(読めない理由は `read_problem`)。"""
    path, problem = _source()
    if problem:
        log.warning("配布設定を読めませんでした(次の起動で読み直します): %s", problem)
        return None
    if path is None:
        return None
    try:
        data = _read_json(path)
    except FileUnreadable as exc:
        log.warning("配布設定を読めませんでした: %s", exc)
        return None
    if data is None:
        return None
    if not isinstance(data, dict) or data.get("format") != FORMAT or not isinstance(data.get("settings"), dict):
        log.warning("配布設定の形が違うため読みません: %s", path)
        return None
    settings = {k: v for k, v in data["settings"].items() if k in ITEM_KEYS and _present(v)}
    if not settings:
        return None
    return Bundle(settings, str(data.get("created_at", "")), str(data.get("created_on", "")),
                  str(data.get("version", "")))


def read_problem() -> str:
    """置いてある配布設定を読めない理由。無い・読めるなら空。"""
    path, problem = _source()
    if problem or path is None:
        return problem
    try:
        _read_json(path)
    except FileUnreadable as exc:
        return str(exc)
    return ""


#: 大設定の「4ツールの分もまとめて書き出す」は、4ツールの配布設定もこのファイル(`日報複合ツール.json`)の
#: `tools` → `<ツール>` に入れる(各ツールの `tools\\<ツール>\\配布設定\\設定.json` と同じ中身)。
#: 配った先の各ツールは、自分のフォルダの配布設定と、このファイルの自分の分の**作った時刻が新しいほう**を
#: 丸ごと使う(同じ時刻ならツールのフォルダのほう。項目ごとには混ぜない)。各ツールの `distribution.read`
TOOLS_KEY = "tools"
#: 各ツールの配布設定のファイル(そのツールのフォルダの中)
TOOL_DIR_NAME = "配布設定"
TOOL_FILE_NAME = "設定.json"
#: まとめたファイルに入れられないもの(音声ファイル)の置き場所(日報のフォルダの `配布設定\\音`)
TOOL_SOUNDS_DIR = "音"


def stamp(text: Any) -> str:
    """作った時刻を比べられる形に(数字だけ・14桁)。ツールごとに書き方が違う(`2026-10-26 08:57` /
    `2026/10/26 08:57:11`)ので、数字だけにして並べる。各ツールの `distribution.stamp` と同じ。"""
    digits = "".join(ch for ch in str(text or "") if ch.isdigit())
    return digits[:14].ljust(14, "0") if len(digits) >= 8 else ""


class FileUnreadable(RuntimeError):
    """配布設定のファイルはあるのに読めない(**無いのとは違う**)。"""


def _read_json(path: Path) -> Optional[Any]:
    """JSON の中身。**無ければ None、あるのに読めなければ `FileUnreadable`。**"""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        # 「無い」は、親フォルダを一覧できて本当に無いときだけ(届かないのは読めないのと同じ)
        if app_config.confirmed_missing(path):
            return None
        raise FileUnreadable(f"{path} に届きません: {exc}") from exc
    except OSError as exc:
        raise FileUnreadable(f"{path} を読めません: {exc}") from exc
    except ValueError as exc:                     # 文字の符号が壊れている
        raise FileUnreadable(f"{path} の中身が壊れています: {exc}") from exc
    try:
        return json.loads(text)
    except ValueError as exc:
        raise FileUnreadable(f"{path} の中身が壊れています: {exc}") from exc


def _file_data(*, strict: bool = False) -> dict[str, Any]:
    """`日報複合ツール.json` の中身そのもの(無ければ空)。

    読めないときは空(画面に出すだけ)。**`strict=True` なら `FileUnreadable`** ── 書き出し直すとき、
    読めなかったのを「4ツールの分が無い」と取り違えて、まとめた4ツールの分を消さない。
    """
    try:
        data = _read_json(file_path())
    except FileUnreadable:
        if strict:
            raise
        return {}
    return data if isinstance(data, dict) else {}


def _existing_tools(*, strict: bool = False) -> dict[str, Any]:
    tools = _file_data(strict=strict).get(TOOLS_KEY)
    return dict(tools) if isinstance(tools, dict) else {}


def _tool_folder_file(tool: Any) -> Path:
    return Path(tool.dir) / TOOL_DIR_NAME / TOOL_FILE_NAME


def _tool_folder_data(tool: Any) -> Optional[dict[str, Any]]:
    """そのツールのフォルダの配布設定(`tools\\<ツール>\\配布設定\\設定.json`)の中身。無い・読めなければ None。

    無いのか読めないのかは `_tool_folder_problem` で分ける(まとめて書き出すとき)。
    """
    try:
        data = _read_json(_tool_folder_file(tool))
    except FileUnreadable:
        return None
    return data if isinstance(data, dict) and isinstance(data.get("settings"), dict) else None


def _tool_folder_problem(tool: Any) -> str:
    """そのツールのフォルダの配布設定を**あるのに読めない**理由。無い・読めるなら空。"""
    try:
        _read_json(_tool_folder_file(tool))
    except FileUnreadable as exc:
        return str(exc)
    return ""


def _secret(key: str) -> bool:
    """値を画面に出さない項目(管理者パスワード。各ツールで鍵の名前が違う)。"""
    return "password" in key.lower() or "パスワード" in key


def _show_tool_value(key: str, value: Any) -> str:
    if _secret(key):
        return "(設定済み)"
    if isinstance(value, bool):
        return "する" if value else "しない"
    return str(value)


def _tool_version(tool: Any) -> str:
    try:
        conf = json.loads((Path(tool.dir) / "config" / "app.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return ""
    return str(conf.get("version", "")) if isinstance(conf, dict) else ""


def _made(data: Optional[dict[str, Any]]) -> dict[str, str]:
    if not data:
        return {"created_at": "", "created_on": "", "version": ""}
    return {"created_at": str(data.get("created_at", "")), "created_on": str(data.get("created_on", "")),
            "version": str(data.get("version") or data.get("app_version") or "")}


def tool_settings(portal_created_at: str = "") -> list[dict[str, Any]]:
    """4ツールの配布設定の中身(**読むだけ**。配る前に確かめるため)。

        日報・看板・カレンダーの参照設定は大設定のファイルに入らず、各ツールのフォルダの別ファイルに入る
        ──「そりゃぁ反映しないわな」「両方やってください」

    ツールごとに、そのツールのフォルダの配布設定と、まとめたファイルのそのツールの分を見て、
    **配った先が使うほう**(作った時刻が新しいほう。同じならツールのフォルダ)の中身を項目ごとに出す。
    パスワードの値は出さない。大設定の書き出しより古いもの・作った版がいまの版と違うものは一言添える。
    """
    from . import catalog as catalog_mod

    whole = _existing_tools()
    out: list[dict[str, Any]] = []
    for tool in catalog_mod.load().tools:
        folder = Path(tool.dir) / TOOL_DIR_NAME
        own = _tool_folder_data(tool)
        entry = whole.get(tool.id)
        entry = entry if isinstance(entry, dict) and isinstance(entry.get("settings"), dict) else None
        if own is not None and (entry is None or stamp(own.get("created_at")) >= stamp(entry.get("created_at"))):
            use, data = "folder", own
        elif entry is not None:
            use, data = "integrated", entry
        else:
            use, data = "", None
        made = _made(data)
        item: dict[str, Any] = {
            "id": tool.id, "title": tool.title, "path": str(folder),
            "exists": data is not None, "use": use,
            "use_label": {"folder": f"ツールのフォルダ(tools\\{Path(tool.dir).name}\\{TOOL_DIR_NAME})",
                          "integrated": f"まとめたファイル({TOOL_DIR_NAME}\\{FILE_NAME})",
                          "": ""}[use],
            "in_folder": own is not None, "in_file": entry is not None,
            **made, "contents": [], "notes": [],
        }
        if data is not None:
            labels = data.get("labels") if isinstance(data.get("labels"), dict) else {}
            item["contents"] = [{"key": k, "label": str(labels.get(k, k)), "value": _show_tool_value(k, v)}
                                for k, v in data["settings"].items()]
            if own is not None and entry is not None and own.get("settings") != entry.get("settings"):
                other = _made(entry if use == "folder" else own)
                item["notes"].append(
                    f"ツールのフォルダとまとめたファイルの両方にあり、中身が違います。作った時刻が新しい"
                    f"{'ツールのフォルダ' if use == 'folder' else 'まとめたファイル'}のほうを使います"
                    f"(もう一方は 作成 {other['created_at'] or '?'})")
            if portal_created_at and stamp(made["created_at"]) and stamp(made["created_at"]) < stamp(portal_created_at):
                item["notes"].append(f"大設定の書き出し({portal_created_at})より古いものです"
                                     "(配る前に、まとめて書き出し直すか中身を確かめてください)")
            now = _tool_version(tool)
            if made["version"] and now and made["version"] != now:
                item["notes"].append(f"作った版は VER{made['version']}(いまは VER{now})")
        try:
            sounds = sorted(p.name for p in (folder / TOOL_SOUNDS_DIR).iterdir() if p.is_file())
        except OSError:
            sounds = []
        if sounds:
            item["notes"].append(f"音声ファイル {len(sounds)}件は、まとめたファイルには入りません。"
                                 f"tools\\{Path(tool.dir).name}\\{TOOL_DIR_NAME}\\{TOOL_SOUNDS_DIR} のまま配ります"
                                 "(scripts\\make_dist.bat が入れます)")
        out.append(item)
    return out


def summary() -> dict[str, Any]:
    """大設定に出す、配布設定のいま。**パスワードの値は出さない。**"""
    bundle = read()
    applied = user_settings.get(user_settings.KEY_DISTRIBUTION_APPLIED)
    data = _file_data()
    out: dict[str, Any] = {
        "exists": bundle is not None, "path": str(directory()),
        "applied_at": applied.get("at", "") if isinstance(applied, dict) else "",
        "contents": [], "created_at": "", "created_on": "", "version": "",
        # まとめたファイルを作った時刻(大設定の分が既定のままでも、4ツールの分だけ入っていることがある)
        "file_exists": bool(data), "file_created_at": str(data.get("created_at", "")),
        "tools": tool_settings(str(data.get("created_at", ""))),
    }
    if bundle is not None:
        out.update(contents=[{"label": LABELS[k], "value": _show(k, v)} for k, v in bundle.settings.items()],
                   created_at=bundle.created_at, created_on=bundle.created_on, version=bundle.version)
    return out


# ------------------------------------------------------------------
# 書き出す(配る側)。鍵が開いているかは呼ぶ側(web)が確かめる
# ------------------------------------------------------------------
def export(tools: Optional[dict[str, Any]] = None) -> Result:
    """**この端末のいまの設定**を `配布設定\\` に書き出す(前の中身は置き換える)。

    この端末で変えていない項目(既定のまま)は入れない ── 配った先も同じ既定で動く。

    ``tools`` は4ツールの配布設定(まとめて書き出すとき。`export_all`)。渡さなければ、いまの
    ファイルに入っている4ツールの分をそのまま残す(大設定の分だけ書き出し直しても消さない)。
    大設定の分が既定のままでも、4ツールの分があればファイルは作る(結果は「要りません」)。
    """
    # **読めないのを「既定のまま」「4ツールの分が無い」と取り違えない。** 読めなければ書き出さない
    # (この端末の設定は `user_settings.Unreadable`。呼ぶ側が理由を出す)
    current = user_settings.load_checked()
    settings = {k: current[k] for k, _ in ITEMS if _present(current.get(k))}
    defaults = [label for k, label in ITEMS if k not in settings]
    if tools is None:
        try:
            keep = _existing_tools(strict=True)
        except FileUnreadable as exc:
            log.warning("配布設定を書き出しません(いまのファイルを読めない): %s", exc)
            return Result(False, f"いまの配布設定を読めないため、書き出しませんでした"
                                 f"(書き出すと、まとめて入れた4ツールの分が消えます): {exc}", "failed")
    else:
        keep = dict(tools)
    nothing = ("書き出せる設定がありません(置き場所も管理者パスワードも既定のままです。"
               "配った先も既定で動くので、配布設定は要りません)。")
    if not settings and (tools is None or not keep):
        return Result(False, nothing, "nothing")
    target = directory()
    # **作ってから入れ替える。** 途中で失敗して、半分だけ新しい配布設定を残さない
    staging = target.with_name(target.name + ".作成中")
    meta = {"format": FORMAT, "created_at": _now(), "created_on": identity.current().pc_name,
            "version": app_config.version(), "settings": settings}
    if keep:
        meta[TOOLS_KEY] = keep
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
    if not settings:
        log.info("配布設定に4ツールの分だけ入れました(大設定の分は既定のまま): %s", ", ".join(keep))
        return Result(False, nothing, "nothing")
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


# ------------------------------------------------------------------
# まとめて書き出す(大設定 + 4ツール)。鍵が開いているかは呼ぶ側(web)が確かめる
#
#     大設定の「配布設定を書き出す」だけ押して、各ツールの設定画面の配布設定を
#     書き出していなかったため、配った先で4ツールとも何も反映されなかった
#
# 各ツールは別のプロセス・別の手元の領域で動き、パッケージの名前(`app` など)もぶつかるので、
# ここからは読み込まない。そのツールのフォルダで、書き出すだけの入口(`export_distribution.py`)を
# 子として起こし、標準出力の JSON 1行を受け取る。書き出すものは、各ツールの設定画面の既定の
# 選び方と同じ(端末ごとに違うライン・表示などは入れない)。
#
# ただし、**前に各ツールの設定画面で入れていたライン(値も前のまま)と、日報の音声ファイル
# (`tools\\nippou\\配布設定\\音`)は消さずに残す**(各ツールの `export(keep_previous=True)`):
#
#     まとめて書き出したら、日報の設定画面で入れていた音声ファイルとラインが消えた
#     →「音声ファイルとラインは残して書き出す」
#
# 残したラインは、ツールのフォルダの 設定.json に入るので、まとめたファイルの tools にも同じく入る。
# 音声ファイルは今までどおり、まとめたファイルには入らない(日報のフォルダの 音 のまま配る)。
# ------------------------------------------------------------------
#: 各ツールの、配布設定を書き出すだけの入口(そのツールのフォルダの直下)
TOOL_EXPORTER = "export_distribution.py"
#: 1ツールを待つ上限(秒)。ふつうは数秒で終わる(共有フォルダが遅い日の分を見込む)
TOOL_EXPORT_TIMEOUT_SEC = 60

#: まとめて書き出したときの1つずつの結果
STATE_DONE = "done"           # 書き出した
STATE_NOT_NEEDED = "nothing"  # 既定のままなので要らない(配った先も既定で動く)
STATE_FAILED = "failed"       # 書き出せなかった(理由は message)


def export_tool(tool: Any) -> Result:
    """そのツールの配布設定を、そのツールのプロセスで書き出す(`tools\\<ツール>\\配布設定\\`)。

    環境変数はブラウザ版の各ツールを起こすときと同じ(`browser_tools.child_env`)。
    手元の領域の指定(`<頭>_LOCAL_DIR` など)もそのまま渡るので、ツールが単体で動くときと
    同じ設定を読む。
    """
    from .browser_tools import child_env

    script = Path(tool.dir) / TOOL_EXPORTER
    if not script.is_file():
        return Result(False, f"配布設定を書き出す入口({script})がありません。", STATE_FAILED)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        done = subprocess.run([sys.executable, str(script)], cwd=str(tool.dir), env=child_env(),
                              stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=TOOL_EXPORT_TIMEOUT_SEC, creationflags=flags)
    except subprocess.TimeoutExpired:
        return Result(False, f"{TOOL_EXPORT_TIMEOUT_SEC} 秒待っても終わりませんでした"
                             "(共有フォルダが遅い・止まっているかもしれません)。", STATE_FAILED)
    except OSError as exc:
        return Result(False, f"書き出す処理を起こせませんでした: {exc}", STATE_FAILED)
    reply = _reply_of(done.stdout)
    if reply is None:
        tail = [line for line in done.stderr.decode("utf-8", errors="replace").splitlines() if line.strip()][-3:]
        return Result(False, f"書き出す処理の答えが読めませんでした(終了コード {done.returncode})。"
                             + (" " + " / ".join(tail) if tail else ""), STATE_FAILED)
    applied = reply.get("applied")
    kept = reply.get("kept")
    return Result(bool(reply.get("ok")), str(reply.get("message") or ""), str(reply.get("reason") or ""),
                  applied=[str(a) for a in applied] if isinstance(applied, list) else [],
                  carried=[str(k) for k in kept] if isinstance(kept, list) else [])


def _reply_of(stdout: bytes) -> Optional[dict]:
    """入口の答え(標準出力の最後の JSON 1行)。読めなければ None。"""
    for line in reversed(stdout.decode("utf-8", errors="replace").splitlines()):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except ValueError:
            return None
        return data if isinstance(data, dict) and "ok" in data else None
    return None


def _outcome(tool_id: str, title: str, result: Result) -> dict[str, Any]:
    """画面に1行で出す形。"""
    if result.ok:
        state = STATE_DONE
        line = (f"書き出しました({len(result.applied)}項目: {'・'.join(result.applied)})"
                if result.applied else "書き出しました")
        if result.carried:
            # 前に設定画面で入れていたもの(既定では入らない)を消さずに残した
            line += f"。{'・'.join(result.carried)}はそのまま残しました"
    elif result.reason == "nothing":
        state = STATE_NOT_NEEDED
        line = "既定のままなので要りません(この端末で変えた設定がありません。配った先も既定で動きます)"
    else:
        state = STATE_FAILED
        line = f"書き出せませんでした: {result.message}"
    return {"id": tool_id, "title": title, "state": state, "line": line,
            "message": result.message, "reason": result.reason, "applied": result.applied,
            "kept": result.carried}


def export_all() -> dict[str, Any]:
    """大設定の分と、catalog の4ツールの分を順に書き出す。**1つ書き出せなくても残りは続ける。**

    戻り値の `results` はツールごとの結果(`_outcome`)。`ok` は書き出せなかったものが無いか。
    """
    from . import catalog as catalog_mod

    catalog = catalog_mod.load()
    results = []
    for tool in catalog.tools:
        try:
            result = export_tool(tool)
        except Exception as exc:                  # noqa: BLE001 - 残りのツールは続ける
            result = Result(False, str(exc), STATE_FAILED)
        results.append(_outcome(tool.id, tool.title, result))
    # **4ツールの分も大設定の1つのファイルに入れる**(ツールのフォルダに書き出したものと同じ中身)。
    # 配った先の各ツールは、ツールのフォルダのものが無くても、このファイルの自分の分を読む
    tools: dict[str, Any] = {}
    unread: dict[str, str] = {}
    for tool in catalog.tools:
        data = _tool_folder_data(tool)
        if data is None:
            problem = _tool_folder_problem(tool)
            if problem:
                # **読めないのを「無い」と取り違えて、まとめたファイルからそのツールの分を消さない**
                unread[tool.id] = problem
                continue
            data = _tool_folder_data(tool)        # 読めなかったのが一瞬だったとき
        if data is not None:
            tools[tool.id] = data
    portal_result: Optional[Result] = None
    if unread:
        try:
            before = _existing_tools(strict=True)
        except FileUnreadable as exc:
            portal_result = Result(False, f"いまの配布設定を読めないため、書き出しませんでした: {exc}", STATE_FAILED)
            before = {}
        for tool_id, why in unread.items():
            log.warning("まとめて書き出す: %s の配布設定を読めないので、前の分を残します: %s", tool_id, why)
            if tool_id in before:
                tools[tool_id] = before[tool_id]
    if portal_result is None:
        try:
            portal_result = export(tools)
        except user_settings.Unreadable as exc:
            portal_result = Result(False, str(exc), STATE_FAILED)
    portal = _outcome(catalog.portal.id, catalog.portal.title, portal_result)
    for item in results:
        if item["id"] in unread:
            item["line"] += (f"。ツールのフォルダの配布設定を読めなかったので、まとめたファイルには"
                             f"前の分を残しました({unread[item['id']]})")
    if tools:
        titles = "・".join(t.title for t in catalog.tools if t.id in tools)
        portal["line"] += f"。4ツールの分({titles})も {TOOL_DIR_NAME}\\{FILE_NAME} にまとめました"
    results.insert(0, portal)
    for item in results:
        log.info("まとめて書き出す: %s → %s(%s)", item["title"], item["state"], item["message"])
    count = {state: sum(1 for r in results if r["state"] == state)
             for state in (STATE_DONE, STATE_NOT_NEEDED, STATE_FAILED)}
    message = (f"配布設定をまとめて書き出しました(書き出した {count[STATE_DONE]}・"
               f"既定のままで要らない {count[STATE_NOT_NEEDED]}・書き出せなかった {count[STATE_FAILED]})。")
    if count[STATE_FAILED]:
        message += " 書き出せなかったものは、下の結果の理由を見て直し、もう一度押してください。"
    elif count[STATE_DONE]:
        message += " 配るときは scripts\\make_dist.bat で配布用フォルダを作ってください(書き出したものが全部入ります)。"
    return {"ok": not count[STATE_FAILED], "message": message, "results": results}


def _readme(meta: dict[str, Any]) -> str:
    lines = [f"{app_config.display_name()} 配布設定", "",
             f"作成: {meta['created_at']}({meta['created_on']} / VER{meta['version']})", "",
             "このフォルダに入っているもの"]
    lines += [f"  {LABELS[k]}: {_show(k, v)}" for k, v in meta["settings"].items()]
    tools = meta.get(TOOLS_KEY) or {}
    if tools:
        lines += ["", "4ツールの配布設定(大設定の「4ツールの分もまとめて書き出す」で入れたもの)"]
        for tool_id, data in tools.items():
            labels = data.get("labels") if isinstance(data.get("labels"), dict) else {}
            names = "・".join(str(labels.get(k, k)) for k in (data.get("settings") or {}))
            lines.append(f"  {tool_id}: {names}(作成 {data.get('created_at', '?')})")
        lines += ["  各ツールは、ツールのフォルダ(tools\\<ツール>\\配布設定)のものと、ここにある分の",
                  "  作った時刻が新しいほうを読みます。日報の音声ファイルはここには入りません",
                  "  (tools\\nippou\\配布設定\\音 のまま配ります)。"]
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
#: 配布設定から読み込んで**この端末に書いた値**({鍵: 値})。この端末で決めた値と見分ける
#: ── 値がこれと同じままなら、配り直した配布設定の新しい値で置き換える(各ツールと同じ決まり)
KEY_FROM_DIST = "distribution_values"


def _apply(bundle: Bundle, *, overwrite: bool) -> Result:
    result = Result()
    # **この端末の設定を読めなければ読み込まない**(`user_settings.Unreadable`)。以前は読めないと
    # 「この端末に無い」とみなして全部を配布設定の値で埋め、ほかの設定も消していた
    current = user_settings.load_checked()
    found = current.get(KEY_FROM_DIST)
    came = dict(found) if isinstance(found, dict) else {}
    values: dict[str, Any] = {}
    for key, value in bundle.settings.items():
        mine = current.get(key)
        if _present(mine) and not overwrite:
            if mine == value:
                continue                                  # もう同じ
            if not (key in came and came[key] == mine):
                result.kept.append(LABELS[key])           # この端末で決めた違う値
                continue
            # 配布設定から読み込んだ値のまま ── 配り直した新しい値にする
        if key == user_settings.KEY_ADMIN_PASSWORD and not str(value).startswith(admin_password.SCHEME + "$"):
            # 手で平文を書かれていても、**端末には撹拌して持つ**
            if _present(mine) and not overwrite and admin_password._matches(str(value), str(mine)):
                continue
            value = admin_password.hash_text(str(value))
        values[key] = value
        result.applied.append(LABELS[key])
    remembered = {k: v for k, v in came.items() if {**current, **values}.get(k) == v}
    remembered.update(values)
    if values:
        values[user_settings.KEY_DISTRIBUTION_APPLIED] = {"at": _now()}
    if remembered != came:
        values[KEY_FROM_DIST] = remembered
    if values:
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
        problem = read_problem()
        if problem:
            return Result(False, f"配布設定を読めませんでした: {problem}", "failed")
        return Result(False, "配布設定が置かれていません。", "missing")
    result = _apply(bundle, overwrite=True)
    result.message = f"配布設定を読み込み直しました({len(result.applied)}項目)"
    log.info("配布設定を読み込み直しました: %s", ", ".join(result.applied))
    return result
