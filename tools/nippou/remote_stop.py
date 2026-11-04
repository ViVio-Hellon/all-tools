"""管理者以外停止モード ── 管理者の PC から、ほかの起動中の PC の日報複合ツール・4ツールを止める

現場の声:「ランチャーを介さず、アクセス権限・管理者の条件がそろっている PC から、他の PC を停止したい」
(python-web-tools VER4.8.129・4.8.130 と同じ決め)。

    お願いを出せる      管理者パスワード **かつ** この端末のアクセス権限に Administrator(大設定)
    止める範囲          お願いを出した PC 以外の、起動中の PC(日報複合ツールの exe・ブラウザ版、単体の4ツール)
    保存していない・     画面の上に帯で「管理者が更新のため停止を求めています」と出して利用者に任せる。
    処理中               何も残っていなければまもなく止まる。**5分たっても止まらなければ強制**で止める
                        (止める前に画面の打ちかけを手元に置いてもらい、残っていたものを手元の控えに書く。
                        次の起動で知らせる)
    更新中               お願いが出ているあいだ、お願いを出した PC 以外では**起動させない**
    ランチャー           「全 PC で止める」は据え置き。ただし**同時には使わない**
                        (お願いが出ているあいだ、止める入口は理由を返して 3。止めない)

**このファイルは5か所に同じ中身で置く**(`portal/remote_stop.py`・`tools/<ツール>/remote_stop.py`)。
4ツールは単体でも動くので、入口(portal)を読み込めない。違うところ(ログ・手元の領域・止め方・
残っているもの)は、各プロセスの入口が `configure` と `install` で渡す。同じ中身であることは
`tests/test_remote_stop.py` が確かめる。

【どこに置くか】
日報複合ツールの大設定の「共有DBの置き場所」のフォルダ(既定は梱包資材マスタのフォルダ)の
`日報複合ツール_管理者以外停止\\` に、小さな JSON を置く(python-web-tools は同じ共有に `管理者以外停止\\` を置くので、
名前を分けて互いに止め合わない。前の名前のお願いは見ない)。共有の SQLite には書かない(錠の取り合いをしない)。

    日報複合ツール_管理者以外停止\\お願い.json  管理者の PC だけが書く。{"active": true/false, …}
    日報複合ツール_管理者以外停止\\稼働中\\<PC>__<ID>__<アプリ>__<pid>.json
                                     動いている各プロセスが**自分のぶんだけ**書く(1分ごとの知らせと、
                                     お願いへの答え)。管理者の画面はこれを並べて「どの PC が止まったか」を見る

単体の4ツールも**同じフォルダを見る**(`default_root`): この PC の大設定(`%LOCALAPPDATA%\\AllTools\\data\\
user_settings.json`)→ 一式の `配布設定\\日報複合ツール.json` → `ALLTOOLS_SHARED_DB_DIR` → 既定。
だから大設定で出したお願いは、日報複合ツールから起こした4ツールにも、単体で起動した4ツールにも効く。

**丸ごと書いてから入れ替える**(書きかけを読ませない)。
**読めないときは「お願いは無い」にしない**: 見張りは前の判断のまま(期限は進む)。起動は、この PC が
最後に見たお願い(手元に覚える。読めた回だけ書く・消す)が出ていれば止める。覚えが無い・古ければ起動して記録に残す
(共有が落ちているだけで、全部の PC が起動できなくなるのを避ける)。

【日報複合ツールの中の4ツール】(`hosted`)
大きなタブの中で動く4ツールは、自分では止まらない。お願いへの答え(残っているもの・止めてよいか)を
手元(`%LOCALAPPDATA%\\AllTools\\runtime\\管理者以外停止\\`)に書き、入口(portal)がそれを見て、
全部が止めてよくなったら(か期限で)画面の打ちかけを置いてもらってから、まとめて閉じる
(ブラウザ版は各ツールに終わってもらい、デスクトップ版は外枠に「確認なしで閉じて」と頼む)。
入口が閉じないまま長く残ったら、ツールは自分で止まる(`ORPHAN_SEC`)。
"""
from __future__ import annotations

import getpass
import hashlib
import json
import logging
import os
import platform
import re
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

FOLDER = "日報複合ツール_管理者以外停止"  # python-web-tools の「管理者以外停止\\」とは分ける(同じ共有でも互いに止め合わない)
REQUEST_NAME = "お願い.json"
PRESENCE_DIR = "稼働中"
#: 手元(この PC・この利用者)の置き場所の名前
LOCAL_DIR = "管理者以外停止"
KEEP_DIR = "停止控え"
DONE_DIR = "戻した"
MEMORY_NAME = "管理者以外停止_最後に見たお願い.json"

#: 日報複合ツールの共有 DB の既定の置き場所(`portal/shared_db.py` の `DEFAULT_DIR` と同じ)
DEFAULT_SHARED_DIR = r"\\nlmfangyshrd\各課共有\0130_日軽稲沢\梱包課\AIM\【■】_参照用ファイル"
DISTRIBUTION_FILES = ("日報複合ツール.json", "統合ツール.json")

# 保存していない・処理中の PC を待つ長さ(秒)。過ぎたら強制で止める(現場の決め)
GRACE_SEC = 300.0
# 何も残っていない PC でも、帯を見せてから止めるまでの間(秒)
NOTICE_SEC = 30.0
# 見張りの間隔(秒)と、「動いています」の知らせの間隔(秒)
POLL_SEC = 5.0
BEAT_SEC = 60.0
# 知らせがこれより古ければ、管理者の画面で「応答なし」
STALE_SEC = 180.0
# 起動を止めたとき、理由の画面を出しておく長さ(秒)。過ぎたら終わる
BLOCK_SHOW_SEC = 60.0
# 止めると決めてから、画面に打ちかけを置いてもらう間(秒)
FLUSH_SEC = 10.0
# 入口(portal)は、中のツールが期限で控えを書き終えるのを待ってから強制で閉じる(秒)
HOST_EXTRA_SEC = 20.0
# 中のツール: 止めてよいと答えてから入口が閉じないまま、これだけたったら自分で止まる(秒)
ORPHAN_SEC = 90.0
# 中のツールの手元の答えがこれより古ければ、そのツールはもう居ない(秒)
LOCAL_STALE_SEC = 20.0
# 共有を読めないとき、この PC が最後に見たお願いを効かせる長さ(秒)
MEMORY_KEEP_SEC = 6 * 3600.0
# 残っているものを数える・控えを作る処理が、これより長く答えなければ「確かめられない」として進む(秒)
CALL_LIMIT_SEC = 15.0
# 止める処理を呼んでから、これだけたってもプロセスが残っていれば、確実に落とす(秒)
HARD_EXIT_SEC = 60.0
# 共有へ書いている最中(日報の共有へ保存・看板の送信・カレンダーの同期・点検表の印刷)は、期限を過ぎても
# 書き終わるのを待つ。延ばすのは最長これだけ(秒。過ぎたら強制。現場の決め)
WRITING_MAX_SEC = 300.0
# お願いを書いてから読み直すまで(秒)。2台の管理者がほぼ同時に出したとき、あとから書いたほうが残る。
# 読み直して自分のお願いでなければ、この PC は止めない(ほかの管理者のお願いが先に出ている)
ISSUE_SETTLE_SEC = 1.0
# お願いに書く待つ長さの上限(秒。手で書いた・壊れた値で、いつまでも強制にならないことを防ぐ)
GRACE_MAX_SEC = 24 * 3600.0
# たまたま読めない(書いている最中・掴まれている)ときの読み直し(秒)
READ_RETRY_SEC = (0.2, 0.5, 1.0)

#: 試験用: 時間を短くする(`grace=6,notice=1,poll=0.3,flush=1`)。現場では決めない
TIMES_ENV = "ALLTOOLS_REMOTE_STOP_TIMES"
#: 置き場所を直に決める(試験・調べもの用)。決めていなければ大設定と同じ決め方
ROOT_ENV = "ALLTOOLS_REMOTE_STOP_DIR"
#: 試験用: このファイルがあれば、中の行を「残っているもの」に数える(現場では決めない)
TEST_LEFT_ENV = "ALLTOOLS_REMOTE_STOP_TEST_LEFT"

_TIME_KEYS = {"grace": "GRACE_SEC", "notice": "NOTICE_SEC", "poll": "POLL_SEC", "beat": "BEAT_SEC",
              "stale": "STALE_SEC", "block": "BLOCK_SHOW_SEC", "flush": "FLUSH_SEC",
              "host_extra": "HOST_EXTRA_SEC", "orphan": "ORPHAN_SEC", "local_stale": "LOCAL_STALE_SEC",
              "call_limit": "CALL_LIMIT_SEC", "hard_exit": "HARD_EXIT_SEC",
              "writing": "WRITING_MAX_SEC", "settle": "ISSUE_SETTLE_SEC"}


def _apply_times(text: str) -> None:
    for part in (text or "").split(","):
        key, _, value = part.partition("=")
        name = _TIME_KEYS.get(key.strip())
        if name:
            try:
                globals()[name] = float(value)
            except ValueError:
                pass


_apply_times(os.environ.get(TIMES_ENV, ""))

# 各プロセスの答え
STATE_RUNNING = "running"        # 動いている(お願いは無い)
STATE_NOTICE = "notice"          # お願いを受けた。帯を出している(何も残っていなければまもなく止まる)
STATE_WAITING = "waiting"        # 保存していない・処理中。利用者を待っている
STATE_STOPPED = "stopped"        # 止まった(利用者が終えた・何も残っていなかった)
STATE_FORCED = "forced"          # 5分たったので強制で止めた
STATE_WRITING = "writing"        # 期限を過ぎたが、共有へ書いている最中なので書き終わるのを待っている
FINISHED = (STATE_STOPPED, STATE_FORCED)
STATE_LABELS = {
    STATE_RUNNING: "動いています",
    STATE_NOTICE: "お願いを受けました(まもなく止まります)",
    STATE_WAITING: "利用者を待っています",
    STATE_STOPPED: "止まりました",
    STATE_FORCED: "強制で止めました",
    STATE_WRITING: "共有へ書いている最中なので、終わるのを待っています",
}
WRITING_NOTE = STATE_LABELS[STATE_WRITING]

# 断りの種類(文言から推し量らない)
REFUSE_PASSWORD = "wrong_password"
REFUSE_NOT_ADMIN = "not_administrator"
REFUSE_NO_FOLDER = "no_folder"
REFUSE_UNREADABLE = "unreadable"
REFUSE_ALREADY = "already_active"
REFUSE_NOT_ACTIVE = "not_active"
REFUSE_WRITE = "write_failed"


class Unreadable(Exception):
    """お願いのファイル(か置き場所の設定)が読めない・届かない。「無い」とは別。"""


# ------------------------------------------------------------------
# このプロセスの事情(入口が `configure` で渡す)
# ------------------------------------------------------------------
@dataclass
class Setup:
    app: str = "日報複合ツール"         # 管理者の一覧に出す名前(日報複合ツール / 日報 / 看板 …)
    app_id: str = "portal"              # 短い名前(portal / nippou / kanban / calendar / inspection)
    version: str = ""
    local_root: Optional[Path] = None   # このアプリの手元の領域(控え・最後に見たお願い)
    root: Optional[Callable[[], Path]] = None   # 置き場所の決め方(入口は大設定をそのまま使う)
    bundle_root: Optional[Path] = None  # 一式のフォルダ(`配布設定\\` を見る)
    hosted: bool = False                # 日報複合ツールの中で動いている(自分では止まらない)
    logger: Optional[logging.Logger] = None


_setup = Setup()


def configure(**values: Any) -> Setup:
    for key, value in values.items():
        if not hasattr(_setup, key):
            raise TypeError(key)
        setattr(_setup, key, value)
    return _setup


def setup() -> Setup:
    return _setup


def log() -> logging.Logger:
    return _setup.logger or logging.getLogger("remote_stop")


def hosted_from_env() -> bool:
    """日報複合ツールの中で起こされたか(ブラウザ版は `ALLTOOLS_PORTAL=1`、デスクトップ版は `ALLTOOLS_SHELL=1`)。"""
    return os.environ.get("ALLTOOLS_PORTAL", "") == "1" or os.environ.get("ALLTOOLS_SHELL", "") == "1"


# ------------------------------------------------------------------
# 置き場所
# ------------------------------------------------------------------
def confirmed_missing(path: Any) -> bool:
    """`path` が**無いと確かめられた**か(`portal/app_config.confirmed_missing` と同じ考え)。

    `FileNotFoundError` だけでは「無い」と決めない(Windows は共有に届かないときも同じ例外になる)。
    いちばん近い**在る親フォルダを一覧でき**、次の名前がその中に無いときだけ True。
    """
    child = os.path.abspath(os.fspath(path))
    parent = os.path.dirname(child)
    while True:
        try:
            names = os.listdir(parent)
        except FileNotFoundError:
            up = os.path.dirname(parent)
            if up == parent:
                return False
            child, parent = parent, up
            continue
        except OSError:
            return False
        want = os.path.normcase(os.path.basename(child))
        return all(os.path.normcase(name) != want for name in names)


def _bundle_root() -> Optional[Path]:
    if _setup.bundle_root is not None:
        return Path(_setup.bundle_root)
    env = os.environ.get("ALLTOOLS_ROOT", "").strip()
    return Path(env) if env else None


def _local_dir_name() -> str:
    """日報複合ツールの手元の領域の名前(一式の `config\\app.json` の `local_dir_name`。無ければ AllTools)。

    入口と中のツールで違う名前を使うと、中のツールの答えを入口が読めず(待たずに閉じる)、
    単体の4ツールはこの PC の大設定を読めない(別の置き場所を見る)。
    """
    base = _bundle_root()
    if base is not None:
        try:
            data = json.loads((base / "config" / "app.json").read_text(encoding="utf-8-sig"))
            name = str(data.get("local_dir_name") or "").strip() if isinstance(data, dict) else ""
            if name:
                return name
        except (OSError, ValueError):
            pass
    return "AllTools"


def alltools_local_root() -> Path:
    """日報複合ツールの手元の領域(`portal/app_config.local_root` と同じ決め方)。"""
    custom = os.environ.get("ALLTOOLS_LOCAL_DIR", "").strip()
    if custom:
        return Path(custom)
    name = _local_dir_name()
    for env in ("LOCALAPPDATA", "XDG_DATA_HOME"):
        base = os.environ.get(env, "").strip()
        if base:
            return Path(base) / name
    return Path.home() / ".local" / "share" / name


def _integrated_setting() -> str:
    """この PC の大設定で決めた共有 DB のフォルダ。決めていなければ空。**読めなければ `Unreadable`。**"""
    custom = os.environ.get("ALLTOOLS_SETTINGS_PATH", "").strip()
    path = Path(custom) if custom else alltools_local_root() / "data" / "user_settings.json"
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        if confirmed_missing(path):
            return ""
        raise Unreadable(f"大設定の設定ファイルに届きません: {path}") from None
    except OSError as exc:
        raise Unreadable(f"大設定の設定ファイルを読めません: {path}: {exc}") from None
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise Unreadable(f"大設定の設定ファイルが壊れています: {path}: {exc}") from None
    if not isinstance(data, dict):
        raise Unreadable(f"大設定の設定ファイルの形が違います: {path}")
    return str(data.get("shared_db_dir") or "").strip()


def _distributed_setting() -> str:
    """一式の `配布設定\\日報複合ツール.json` の共有 DB のフォルダ(手がかり。読めなければ空)。"""
    base = _bundle_root()
    if base is None:
        return ""
    for name in DISTRIBUTION_FILES:
        try:
            data = json.loads((base / "配布設定" / name).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        settings = data.get("settings") if isinstance(data, dict) else None
        if isinstance(settings, dict):
            return str(settings.get("shared_db_dir") or "").strip()
    return ""


def default_root() -> Path:
    """お願いを置く共有のフォルダ(の親)。大設定と同じ決め方。**読めなければ `Unreadable`。**"""
    env = os.environ.get(ROOT_ENV, "").strip()
    if env:
        return Path(env)
    folder = _integrated_setting() or _distributed_setting() \
        or os.environ.get("ALLTOOLS_SHARED_DB_DIR", "").strip()
    return Path(folder or DEFAULT_SHARED_DIR)


def shared_root() -> Path:
    try:
        return (_setup.root or default_root)()
    except Unreadable:
        raise
    except Exception as exc:                  # noqa: BLE001 - 決め方の失敗も「読めない」
        raise Unreadable(f"置き場所を決められません: {exc}") from None


def folder() -> Path:
    return shared_root() / FOLDER


def request_path() -> Path:
    return folder() / REQUEST_NAME


def presence_dir() -> Path:
    return folder() / PRESENCE_DIR


def local_status_dir() -> Path:
    """中のツールの答え(入口が読む)。日報複合ツールの手元の領域の `runtime\\`。"""
    return alltools_local_root() / "runtime" / LOCAL_DIR


def identity() -> tuple[str, str]:
    """(PC名, ログインID)。`portal/identity.py` と同じ取り方(`ALLTOOLS_PC_NAME` で差し替えられる)。"""
    login = (os.environ.get("ALLTOOLS_LOGIN_ID") or os.environ.get("USERNAME") or "").strip()
    if not login:
        try:
            login = getpass.getuser().strip()
        except Exception:                     # noqa: BLE001 - 環境依存で落ちうる
            login = ""
    pc = (os.environ.get("ALLTOOLS_PC_NAME") or os.environ.get("COMPUTERNAME") or "").strip()
    return pc or platform.node().strip(), login


def _fold(text: Any) -> str:
    return unicodedata.normalize("NFKC", str(text or "")).strip().casefold()


def same_pc(a: str, b: str) -> bool:
    """同じ PC か(大文字小文字・全角半角・前後の空白を問わない)。空なら同じとしない。"""
    return bool(_fold(a)) and _fold(a) == _fold(b)


SPARED_NAME = "管理者以外停止_止めないお願い.json"


def spared_path() -> Path:
    """この PC で「止めないお願い」の覚え(日報複合ツールの手元の領域。この PC の全ツールが同じものを見る)。"""
    return alltools_local_root() / "runtime" / SPARED_NAME


def spare(request_id: str) -> None:
    """このお願いでは、この PC を止めない(この PC の管理者も同時にお願いを出したが、ほかの管理者のお願いが残った)。"""
    try:
        data = json.loads(spared_path().read_text(encoding="utf-8"))
        ids = [str(x) for x in data.get("ids") or []] if isinstance(data, dict) else []
    except (OSError, ValueError):
        ids = []
    if request_id and request_id not in ids:
        ids = (ids + [request_id])[-20:]
    write_json(spared_path(), {"ids": ids, "saved_at": time.time()}, warn=True)


def _spared_ids() -> list:
    try:
        data = json.loads(spared_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [str(x) for x in data.get("ids") or []] if isinstance(data, dict) else []


def exempt(pc: str, req: Optional[dict]) -> bool:
    """このお願いで `pc` を止めないか: お願いを出した PC か、同時に出して譲った PC(`spare`)。"""
    if not req:
        return False
    if same_pc(pc, req.get("by_pc", "")):
        return True
    return bool(req.get("id")) and str(req.get("id")) in _spared_ids()


def _span(sec: float) -> str:
    """「5分」「40秒」。"""
    return f"{int(sec // 60)}分" if sec >= 60 and sec % 60 == 0 else f"{int(sec)}秒"


def _safe(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", text or "") or "不明"


# ------------------------------------------------------------------
# 読み書き
# ------------------------------------------------------------------
def _read_once(path: Path) -> Optional[dict]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        if confirmed_missing(path):
            return None
        raise OSError(f"{path} に届きません") from None
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("中身が JSON のまとまりではありません")
    return data


def read_json(path: Path, *, retry: bool = True) -> Optional[dict]:
    """読む。**無いと確かめられたときだけ** None。読めなければ `Unreadable`(たまたまなら読み直す)。"""
    waits = READ_RETRY_SEC if retry else ()
    last: Optional[BaseException] = None
    for wait in (0.0, *waits):
        if wait:
            time.sleep(wait)
        try:
            return _read_once(path)
        except (OSError, ValueError) as exc:
            last = exc
    raise Unreadable(f"{path}: {last}")


def write_json(path: Path, data: dict, *, warn: bool = True) -> bool:
    """丸ごと書いてから入れ替える(書きかけを読ませない)。書けたら True。"""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        (log().warning if warn else log().debug)("管理者以外停止のファイルを書けませんでした(%s): %s", path, exc)
        return False


def _active(value: Any) -> bool:
    """`active` が出ている印か。手で書いた "false"・0 を「出ている」と読まない。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    return _fold(value) in ("true", "1", "yes", "はい")


def _number(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number else default          # NaN を数にしない


def normalize(data: dict) -> dict:
    """お願いの形を揃える(手で書いた・古い形・おかしな値でも、判定の途中で落ちない)。

    - `id` が無ければ中身から作る(読むたびに同じ。「出し直した」と取り違えない)
    - `grace_sec` は 0 より大きく `GRACE_MAX_SEC` 以下。読めなければ既定(5分)
    - `created_at` は数。`by_pc`・`by_login`・`reason` は文字
    """
    out = dict(data)
    if not str(out.get("id") or "").strip():
        raw = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
        out["id"] = "manual-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    out["id"] = str(out["id"])
    grace = _number(out.get("grace_sec"), GRACE_SEC)
    out["grace_sec"] = grace if 0 < grace <= GRACE_MAX_SEC else GRACE_SEC
    out["created_at"] = _number(out.get("created_at"), 0.0)
    for key in ("by_pc", "by_login", "reason", "by_app"):
        out[key] = "" if out.get(key) is None else str(out.get(key))
    return out


def read_request(*, retry: bool = True) -> Optional[dict]:
    """いまのお願い。**出ていなければ None**(無い・解除済み)。読めなければ `Unreadable`。

    **壊れた JSON は「読めない」**(「お願いは無い」とは読まない)。
    """
    data = read_json(request_path(), retry=retry)
    if data is None or not _active(data.get("active")):
        return None
    return normalize(data)


def describe_request(req: dict) -> str:
    """「誰が・いつ・何のために」の一言。"""
    try:
        when = time.strftime("%m/%d %H:%M", time.localtime(_number(req.get("created_at"), 0.0)))
    except (OverflowError, OSError, ValueError):
        when = "?"
    who = f"{req.get('by_pc') or '?'}({req.get('by_login') or '?'})"
    reason = req.get("reason") or "更新のため"
    return f"{who} が {when} に出しました: {reason}"


# --- この PC が最後に見たお願い(共有を読めないときの判断に使う) -------------
def memory_path() -> Optional[Path]:
    base = _setup.local_root
    return base / "runtime" / MEMORY_NAME if base is not None else None


def remember(req: Optional[dict]) -> None:
    """**読めた回だけ**呼ぶ。出ていれば覚え、出ていなければ忘れる。"""
    path = memory_path()
    if path is None:
        return
    if req is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log().debug("最後に見たお願いを消せませんでした: %s", exc)
        return
    write_json(path, {"request": req, "seen_at": time.time()}, warn=False)


def remembered() -> Optional[dict]:
    """最後に見たお願い(古い・読めない・無いなら None)。"""
    path = memory_path()
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        req = data["request"]
        seen = float(data["seen_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    # PC の時計を直した(戻した・進めた)ときも、離れすぎた覚えは使わない
    if not isinstance(req, dict) or abs(time.time() - seen) > MEMORY_KEEP_SEC:
        return None
    return normalize(req)


# ------------------------------------------------------------------
# 管理者の PC: お願いを出す・解除する・各 PC の様子
# ------------------------------------------------------------------
@dataclass
class Result:
    ok: bool
    message: str
    reason: str = ""


def can_issue(password_ok: bool, administrator: bool) -> Result:
    """お願いを出せるか(管理者パスワード **かつ** この端末の権限に Administrator)。判定は呼び手が渡す。"""
    if not password_ok:
        return Result(False, "管理者パスワードが違います。", REFUSE_PASSWORD)
    if not administrator:
        pc, login = identity()
        return Result(False, f"この端末({pc} / {login})のアクセス権限に Administrator がありません。"
                      "管理者以外停止モードは Administrator の端末からだけ使えます。", REFUSE_NOT_ADMIN)
    try:
        root = shared_root()
    except Unreadable as exc:
        return Result(False, f"お願いを置く共有のフォルダを決められません: {exc}", REFUSE_NO_FOLDER)
    if not root.is_dir():
        return Result(False, f"共有DBの置き場所のフォルダに届きません: {root}(お願いはそのフォルダに置きます)",
                      REFUSE_NO_FOLDER)
    return Result(True, "")


def issue(password_ok: bool, administrator: bool, reason: str = "") -> Result:
    """お願いを出す。**出ているお願いがあれば出さない**(読めなければ出さない)。"""
    allowed = can_issue(password_ok, administrator)
    if not allowed.ok:
        return allowed
    try:
        current = read_request()
    except Unreadable as exc:
        return Result(False, f"いまのお願いを読めないので出しません(上書きしないため): {exc}", REFUSE_UNREADABLE)
    if current is not None:
        return Result(False, "もうお願いが出ています。" + describe_request(current), REFUSE_ALREADY)
    pc, login = identity()
    data = {
        "active": True, "id": uuid.uuid4().hex, "by_pc": pc, "by_login": login,
        "by_app": _setup.app, "reason": (reason or "").strip()[:200] or "更新のため",
        "created_at": time.time(), "grace_sec": GRACE_SEC, "version": _setup.version,
    }
    if not write_json(request_path(), data):
        return Result(False, f"お願いを書けませんでした: {request_path()}", REFUSE_WRITE)
    # ほかの管理者がほぼ同時に出していないか(あとから書いたほうが残る)。読み直して自分のでなければ、この PC は止めない
    _settle()
    try:
        now_req = read_request()
    except Unreadable:
        now_req = data
    if now_req is not None and now_req.get("id") != data["id"]:
        spare(str(now_req.get("id") or ""))
        log().warning("管理者以外停止: ほかの管理者のお願いが先に出ていました(この PC は止めません): %s",
                      describe_request(now_req))
        return Result(False, "ほかの管理者のお願いが先に出ています(この PC は止めません)。"
                      + describe_request(now_req), REFUSE_ALREADY)
    log().warning("管理者以外停止のお願いを出しました: %s", describe_request(data))
    _forget_old_answers()
    return Result(True, "お願いを出しました。ほかの PC は保存していない入力・実行中の処理が無ければまもなく止まり、"
                  f"残っていても {_span(GRACE_SEC)}で強制で止まります。")


def _settle() -> None:
    time.sleep(ISSUE_SETTLE_SEC)


def clear(password_ok: bool, administrator: bool) -> Result:
    """解除する(更新が終わった)。ほかの PC がまた起動できるようになる。"""
    allowed = can_issue(password_ok, administrator)
    if not allowed.ok:
        return allowed
    try:
        current = read_request()
    except Unreadable as exc:
        return Result(False, f"いまのお願いを読めないので解除しません: {exc}", REFUSE_UNREADABLE)
    if current is None:
        return Result(False, "お願いは出ていません。", REFUSE_NOT_ACTIVE)
    pc, login = identity()
    done = {**current, "active": False, "cleared_at": time.time(), "cleared_by_pc": pc, "cleared_by_login": login}
    if not write_json(request_path(), done):
        return Result(False, f"解除を書けませんでした: {request_path()}", REFUSE_WRITE)
    log().warning("管理者以外停止を解除しました(%s / %s)。ほかの PC も起動できます", pc, login)
    _forget_old_answers()
    return Result(True, "解除しました。ほかの PC も起動できます。")


def _forget_old_answers() -> None:
    """止まった・古い答えのファイルを片付ける(**読めたものだけ**。読めないものは消さない)。"""
    try:
        files = list(presence_dir().glob("*.json"))
    except (OSError, Unreadable):
        return
    now = time.time()
    for path in files:
        try:
            data = read_json(path, retry=False)
        except Unreadable:
            continue
        if data is None:
            continue
        old = now - float(data.get("beat_at") or 0) > STALE_SEC * 10
        if data.get("state") in FINISHED or old:
            try:
                path.unlink()
            except OSError:
                pass


@dataclass
class Overview:
    """管理者の画面に出すもの。"""

    available: bool
    why: str = ""
    request: Optional[dict] = None
    request_text: str = ""
    unreadable: str = ""
    pcs: list = field(default_factory=list)
    me_pc: str = ""
    folder: str = ""

    def to_dict(self) -> dict:
        return {"available": self.available, "why": self.why, "request": self.request,
                "request_text": self.request_text, "unreadable": self.unreadable,
                "pcs": self.pcs, "me_pc": self.me_pc, "folder": self.folder,
                "grace_sec": GRACE_SEC}


def overview(*, full: bool = True) -> Overview:
    """いまのお願いと、各 PC の様子。`full=False` ならお願いだけ(各 PC の知らせは読まない)。"""
    pc, _login = identity()
    try:
        root = shared_root()
    except Unreadable as exc:
        return Overview(False, f"お願いを置く共有のフォルダを決められません: {exc}", me_pc=pc)
    if not root.is_dir():
        return Overview(False, f"共有DBの置き場所のフォルダに届きません: {root}", me_pc=pc, folder=str(root / FOLDER))
    view = Overview(True, me_pc=pc, folder=str(root / FOLDER))
    try:
        view.request = read_request(retry=False)
    except Unreadable as exc:
        view.unreadable = f"お願いのファイルを読めません: {exc}"
    if view.request:
        view.request_text = describe_request(view.request)
    if not full:
        return view
    rid = (view.request or {}).get("id")
    by_pc = (view.request or {}).get("by_pc", "")
    now = time.time()
    try:
        files = sorted(presence_dir().glob("*.json"))
    except OSError:
        files = []
    for path in files:
        try:
            data = read_json(path, retry=False)
        except Unreadable:
            view.pcs.append({"pc": path.stem, "state": "", "label": "読めません", "beat": "", "stale": True})
            continue
        if data is None:
            continue
        state = data.get("state") or STATE_RUNNING
        answered = bool(rid) and data.get("request_id") == rid
        finished = state in FINISHED
        stale = (not finished) and now - float(data.get("beat_at") or 0) > STALE_SEC
        if finished and not answered:
            continue                              # 前のお願いで止まったもの
        label = STATE_LABELS.get(state, state)
        mine = same_pc(data.get("pc", ""), by_pc)
        if stale:
            label = "応答なし(知らせが途絶えています)"
        elif rid and not answered and not mine:
            label = "まだお願いを受けていません"
        if mine and rid:
            label += "(お願いを出した PC。止めません)"
        view.pcs.append({
            "pc": data.get("pc", ""), "login": data.get("login", ""), "app": data.get("app", ""),
            "kind": data.get("kind", ""), "version": data.get("version", ""), "state": state,
            "label": label, "detail": data.get("detail", ""), "stale": stale,
            "beat": time.strftime("%H:%M:%S", time.localtime(float(data.get("beat_at") or 0))),
        })
    return view


# ------------------------------------------------------------------
# 起動: 更新中は起動させない / 止める入口は断る
# ------------------------------------------------------------------
def _escape_hint() -> str:
    try:
        where = str(request_path())
    except Unreadable:
        where = f"共有DBの置き場所の {FOLDER}\\{REQUEST_NAME}"
    return (f"(管理者へ: お願いを出した PC で解除できないときは、{where} を別の名前に変えると解除できます)")


def startup_block() -> Optional[str]:
    """起動してよいか。止めるなら理由の文、よければ None。

    お願いを出した PC は止めない(更新したあと、その PC で起動して確かめ、解除する)。
    **読めないときは、この PC が最後に見たお願い**で決める(覚えが無ければ起動する。記録には残す)。
    """
    pc, _login = identity()
    try:
        req = read_request()
    except Unreadable as exc:
        last = remembered()
        if last is not None and not exempt(pc, last):
            log().warning("管理者以外停止のお願いを確かめられません。最後に見たお願いが出ていたので起動しません: %s", exc)
            return ("管理者が更新のためツールを止めていました(管理者以外停止モード)。いまは共有のフォルダを読めないため、"
                    "解除されたかを確かめられません。共有に届くようになれば起動できます。"
                    f"\n{describe_request(last)}"
                    f"\n読めない理由: {exc}"
                    f"\n急ぐときは、管理者に解除されたことを確かめてから、この PC の {memory_path()} を消すと起動できます。")
        log().warning("管理者以外停止のお願いを確かめられないまま起動します: %s", exc)
        return None
    except Exception as exc:                  # noqa: BLE001 - 起動の判定で落ちない
        log().warning("管理者以外停止のお願いを確かめられませんでした: %s", exc)
        return None
    remember(req)
    if req is None:
        return None
    if exempt(pc, req):
        log().info("管理者以外停止のお願いが出ていますが、お願いを出した PC なので起動します: %s", describe_request(req))
        return None
    log().warning("管理者以外停止モード中のため起動しません: %s", describe_request(req))
    return ("管理者が更新のためツールを止めています(管理者以外停止モード)。更新が終わるまで起動できません。"
            f"\n{describe_request(req)}"
            "\nしばらくしてから起動し直してください。\n" + _escape_hint())


def stop_bat_refusal() -> Optional[str]:
    """止める入口(stop.bat・ランチャーの「全 PC で止める」)を断るなら1行目の理由。

    **同時には使わない**(現場の決め)。読めなければ断らない(止める道を閉じない)。
    **お願いを出した PC では断らない**(その PC は止めないので、管理者が自分の PC をふつうに止めて更新する)。
    """
    try:
        req = read_request()
    except Exception:                         # noqa: BLE001
        return None
    if req is None or exempt(identity()[0], req):
        return None
    return ("管理者以外停止モード中のため、stop.bat(ランチャーの「全 PC で止める」)では止めません。"
            f"解除してから使ってください({describe_request(req)})")


# ------------------------------------------------------------------
# 手元の控え(強制で止める前に、残っていたものを書く。次の起動で知らせる)
# ------------------------------------------------------------------
def keep_dir() -> Optional[Path]:
    base = _setup.local_root
    return base / "data" / KEEP_DIR if base is not None else None


def save_keep(left: list, kept: list, request: Optional[dict] = None, *, need: bool = False) -> bool:
    """控えを書く(この PC の手元。共有には書かない)。`left` 残っていたもの、`kept` 手元に置いたもの。

    書けたら True。書くものが無ければ `need` のとき True(書く要が無い)、ほかは False。
    """
    base = keep_dir()
    if not (left or kept):
        return need
    if base is None:
        return False
    stamp = time.strftime("%Y%m%d-%H%M%S")
    data = {"saved_at": time.time(), "app": _setup.app, "left": list(left), "kept": list(kept),
            "request": describe_request(request) if request else ""}
    ok = write_json(base / f"keep-{stamp}-{os.getpid()}.json", data)
    if ok:
        log().warning("管理者以外停止: 強制で止める前に控えを書きました(残っていたもの: %s)", "、".join(left) or "なし")
    return ok


def restore_keeps() -> list:
    """控えがあれば、知らせる一言にして返し、控えは `戻した\\` へ移す(もう一度は知らせない。消さない)。

    **読めない控えは知らせず、そのまま残す**(記録に出す)。
    """
    base = keep_dir()
    if base is None:
        return []
    try:
        found = sorted(base.glob("keep-*.json"))
    except OSError:
        return []
    notes = []
    for path in found:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            left = [str(x) for x in data.get("left") or []]
            kept = [str(x) for x in data.get("kept") or []]
        except (OSError, ValueError, AttributeError, TypeError) as exc:
            log().warning("管理者以外停止の控えを読めないので知らせません(控えは残しています: %s): %s", path, exc)
            continue
        when = time.strftime("%m/%d %H:%M", time.localtime(float(data.get("saved_at") or 0)))
        note = f"{when} に管理者の停止(更新)で、{_setup.app}を強制で止めました。"
        if left:
            note += f"そのとき残っていたもの: {'、'.join(left)}。"
        if kept:
            note += " ".join(kept)
        notes.append(note.strip())
        log().warning("%s", note)
        try:
            (base / DONE_DIR).mkdir(parents=True, exist_ok=True)
            os.replace(path, base / DONE_DIR / path.name)
        except OSError as exc:
            log().warning("知らせた控えを移せませんでした(次の起動でもう一度知らせます): %s", exc)
    return notes


# --- 画面の「保存していない入力」(帯が問い合わせのたびに言い添える) ------------------
#: 画面が言い添えた、保存していない入力(ページ → (時刻, 呼び名))。サーバには無い打ちかけ
#: (カレンダーの設定の欄・大設定の置き場所など)を「残っているもの」に数える
_screens: dict = {}
_screens_lock = threading.Lock()
#: 言わなくなった画面を忘れるまで(秒)。裏に回したタブは問い合わせが1分に1回まで間引かれるので、それより長く
#: (閉じた画面は閉じるときに空を言う。言えずに消えた画面は、これだけ止めるのが遅れる。期限で強制なのは同じ)
SCREEN_REPORT_SEC = 90.0


def screen_report(page: str, items: Any) -> None:
    """画面(帯)が言う「この画面で保存していない入力」。言わなくなった画面は `SCREEN_REPORT_SEC` で忘れる。"""
    page = str(page or "")[:80]
    if not page:
        return
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except ValueError:
            items = [items]
    names = [str(x).strip()[:200] for x in (items if isinstance(items, list) else []) if str(x).strip()][:20]
    with _screens_lock:
        if names:
            _screens[page] = (time.monotonic(), names)
        else:
            _screens.pop(page, None)


def screen_left() -> list:
    now = time.monotonic()
    out: list = []
    with _screens_lock:
        for page in [p for p, (at, _n) in _screens.items() if now - at > SCREEN_REPORT_SEC]:
            del _screens[page]
        for _at, names in _screens.values():
            out += [n for n in names if n not in out]
    return out


def test_left() -> list:
    """試験用(`ALLTOOLS_REMOTE_STOP_TEST_LEFT`)。現場では空。"""
    path = os.environ.get(TEST_LEFT_ENV, "").strip()
    if not path:
        return []
    try:
        return [line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return []


def call_limited(fn: Callable[[], Any], limit: Optional[float] = None) -> tuple:
    """`fn()` を呼ぶ。(答えたか, 答え)。落ちた・`limit` 秒で答えない(固まった)なら (False, None)。

    見張りが、数える処理(手元の DB の錠待ちなど)と一緒に固まらないように。固まったら、期限で強制になる。
    """
    box: list = []

    def run() -> None:
        try:
            box.append((True, fn()))
        except Exception:                     # noqa: BLE001
            log().debug("管理者以外停止: 呼んだ処理が失敗しました", exc_info=True)
            box.append((False, None))

    worker = threading.Thread(target=run, name="remote-stop-call", daemon=True)
    worker.start()
    worker.join(CALL_LIMIT_SEC if limit is None else limit)
    return box[0] if box else (False, None)


# ------------------------------------------------------------------
# 受け側: 動いているプロセスの見張り
# ------------------------------------------------------------------
class Watcher:
    """お願いを見張り、帯に出すものを持ち、止める。プロセスに1つ。

    - `stop`        止め方(`/api/shutdown` と同じ道)
    - `left`        残っているもの(保存していない入力・実行中の処理)の呼び名
    - `hold`        True のあいだは「何も残っていない」でも止めない(入口: 中のツールがまだ答えていない)
    - `before_force` 強制で止める前に手元へ置いたもの(呼び名)を返す
    - `on_leave`    お願いの最中に利用者が自分で終えたとき、手元へ置いたものを返す
    - `hosted`      日報複合ツールの中のツール。自分では止まらず、手元に答えを書く(入口が閉じる)
    - `extra_sec`   期限に足す秒(入口は、中のツールが控えを書き終えるのを待つ)
    - `flush`       止める前に画面に打ちかけを置いてもらう(画面がこの状態を見て置く)
    """

    def __init__(self, stop: Callable[[], None], left: Callable[[], list], *,
                 kind: str = "browser", hosted: bool = False, extra_sec: float = 0.0,
                 hold: Optional[Callable[[], bool]] = None,
                 before_force: Optional[Callable[[], list]] = None,
                 writing: Optional[Callable[[], list]] = None,
                 on_leave: Optional[Callable[[], list]] = None,
                 flush: bool = True, hard_exit: bool = False,
                 force_ok: Optional[Callable[[], bool]] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._stop = stop
        self._left_fn = left
        self._hold = hold
        self._before_force = before_force
        self._writing_fn = writing
        self.writing: list = []
        self._on_leave = on_leave
        self._force_ok = force_ok
        self._clock = clock
        self.kind = kind
        self.hosted = hosted
        self.extra_sec = extra_sec
        self.flush = flush and not hosted
        self.hard_exit = hard_exit and not hosted
        self.pc, self.login = identity()
        # 期限は各 PC の **monotonic**(PC の時計がずれていても・直されても、5分は5分)。
        # 管理者の一覧に出す時刻(知らせの時刻)だけは壁の時計
        self.started_at = time.time()
        self.state = STATE_RUNNING
        self.detail = ""
        self.left_now: list = []
        self.request: Optional[dict] = None
        self.received_at = 0.0
        self.deadline = 0.0
        self.notice_until = 0.0
        self.decided = False          # 止めると決めた(入口・単体は画面に置いてもらう間、中のツールは入口待ち)
        self.stop_at = 0.0            # 単体: この時刻に止める / 中のツール: この時刻まで入口を待つ
        self.stopping = False         # 止める処理を呼んだ(呼ぶ)
        self.kept: list = []
        self._last_beat = 0.0
        self._beat_failing = False
        self._keep_warned = False
        self._stop_called = False
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._halt = threading.Event()
        self._wake = threading.Event()

    # --- 自分の知らせ ---------------------------------------------------
    def presence_path(self) -> Path:
        return presence_dir() / f"{_safe(self.pc)}__{_safe(self.login)}__{_safe(_setup.app_id)}__{os.getpid()}.json"

    def local_path(self) -> Path:
        return local_status_dir() / f"{_safe(_setup.app_id)}-{os.getpid()}.json"

    def _beat(self, *, force: bool = False) -> None:
        now = self._clock()
        if not force and now - self._last_beat < BEAT_SEC:
            return
        try:
            target = self.presence_path()
            reachable = shared_root().is_dir()
        except (Unreadable, OSError):
            return
        if not reachable:
            return
        self._last_beat = now
        ok = write_json(target, {
            "pc": self.pc, "login": self.login, "pid": os.getpid(), "app": _setup.app,
            "app_id": _setup.app_id, "kind": self.kind, "hosted": self.hosted, "version": _setup.version,
            "started_at": self.started_at, "beat_at": time.time(), "state": self.state, "detail": self.detail,
            "request_id": (self.request or {}).get("id", ""),
        }, warn=not self._beat_failing)
        # 書けないあいだは記録をためない(最初の1回と、直ったときだけ)
        if not ok and not self._beat_failing:
            log().warning("管理者以外停止の知らせを共有に書けません(書けるようになるまで記録しません): %s", target)
        elif ok and self._beat_failing:
            log().info("管理者以外停止の知らせを共有に書けるようになりました")
        self._beat_failing = not ok

    def _write_local(self) -> None:
        """中のツール: 入口が読む答え(手元)。

        **お願いが無くても「居ます」を書く**(`request_id` は空)。入口は、居るのにまだこのお願いに
        答えていない中のツールを待つ(以前は答えの無いツールを数えず、共有をたまたま読めなかった・
        見張りが遅れたツールの処理中の保存を待たずに、30秒で閉じていた)。
        """
        if not self.hosted:
            return
        path = self.local_path()
        if self.request is None:
            write_json(path, {"app": _setup.app, "app_id": _setup.app_id, "pid": os.getpid(),
                              "request_id": "", "state": STATE_RUNNING, "left": [], "ready": False,
                              "beat_at": time.time()}, warn=False)
            return
        write_json(path, {"app": _setup.app, "app_id": _setup.app_id, "pid": os.getpid(),
                          "request_id": self.request.get("id", ""), "state": self.state,
                          "left": list(self.left_now), "ready": self.decided, "writing": list(self.writing),
                          "beat_at": time.time()},
                   warn=False)

    def leave(self) -> None:
        """終わるとき。止まった答えは残す(管理者が見る)、ふだんの終わりなら消す。

        **お願いを受けている最中に利用者が自分で終えた**ときも「止まりました」を残す
        (消すと管理者の一覧から消えて、止まったのか分からない)。
        """
        self._halt.set()
        self._wake.set()
        with self._lock:
            if self.hosted:
                try:
                    self.local_path().unlink()
                except OSError:
                    pass
            if self.state in FINISHED:
                self._beat(force=True)
                return
            if self.request is not None:
                kept = []
                if self._on_leave is not None:
                    try:
                        kept = list(self._on_leave() or [])
                    except Exception:         # noqa: BLE001 - 控えに失敗しても終わりは止めない
                        log().exception("終わるときの控えを残せませんでした")
                self.state = STATE_STOPPED
                self.detail = "利用者が終了しました" + (f"(手元に置きました: {'、'.join(kept)})" if kept else "")
                self._beat(force=True)
                return
        try:
            self.presence_path().unlink()
        except (OSError, Unreadable):
            pass

    # --- 見張り ---------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="remote-stop", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._halt.is_set():
            try:
                self.tick()
            except Exception:                 # noqa: BLE001 - 見張りは止めない
                log().debug("管理者以外停止の見張りで失敗しました(次で試します)", exc_info=True)
            if self.stopping:
                return
            wait = POLL_SEC
            if self.decided and self.stop_at:
                wait = max(0.05, min(POLL_SEC, self.stop_at - self._clock()))
            self._wake.wait(wait)
            self._wake.clear()

    def hurry(self) -> None:
        """画面が打ちかけを置き終えた。待たずに止める(止めると決めたあとだけ。中のツールには効かない)。"""
        with self._lock:
            if self.decided and not self.hosted and not self.stopping:
                self.stop_at = min(self.stop_at, self._clock())
        self._wake.set()

    def tick(self) -> None:
        """1回ぶん見る。**止めるのは錠の外で**(止めると終わりの後始末 `leave` が同じ錠を取りに来る)。"""
        with self._lock:
            self._tick()
            stop_now = self.stopping and not self._stop_called
            if stop_now:
                self._stop_called = True
        if stop_now:
            if self.hard_exit:
                # 止める道が固まっても(終わりの後始末が返らない)、確実に落とす
                timer = threading.Timer(HARD_EXIT_SEC, self._exit_hard)
                timer.daemon = True
                timer.start()
            try:
                self._stop()
            except Exception:                 # noqa: BLE001
                log().exception("管理者以外停止で止められませんでした")

    def _exit_hard(self) -> None:
        log().warning("管理者以外停止: 止める処理を呼んでから %.0f秒たっても終わらないので、プロセスを終了します",
                      HARD_EXIT_SEC)
        try:
            self.leave()
        finally:
            logging.shutdown()
            os._exit(0)

    def _tick(self) -> None:
        if self.stopping:
            return
        now = self._clock()
        try:
            req = read_request()
            remember(req)
        except Unreadable as exc:
            # **読めないことを「お願いは無い」にしない。** いまの判断のまま待つ(期限は進める)
            log().debug("管理者以外停止のお願いを読めません(前の判断のまま): %s", exc)
            req = self.request
        if req is not None and exempt(self.pc, req):
            req = None                            # お願いを出した PC(同時に出して譲った PC も)は止めない
        if req is None:
            if self.request is not None:
                log().info("管理者以外停止のお願いが取り下げられました。止めずに続けます")
                user_note("管理者が停止のお願いを取り下げました。そのまま使えます")
            self.request = None
            self.state, self.detail, self.left_now = STATE_RUNNING, "", []
            self.decided, self.stop_at, self.kept, self.writing = False, 0.0, [], []
            self._write_local()
            self._beat()
            return
        if self.request is None or self.request.get("id") != req.get("id"):
            self.request = req
            self.received_at = now
            self.deadline = now + self.grace_of(req)
            self.notice_until = now + NOTICE_SEC
            self.decided, self.stop_at, self.kept = False, 0.0, []
            log().warning("管理者以外停止のお願いを受けました: %s", describe_request(req))
        if self.decided:
            if now >= self.stop_at:
                if self.hosted:
                    log().warning("管理者以外停止: 日報複合ツールが閉じないので、自分で止まります")
                self._go(now)
            else:
                self._write_local()
            return
        left = self._left()
        self.left_now = left
        holding = False
        if self._hold is not None:
            ok, got = call_limited(self._hold)
            holding = bool(got) if ok else False
        # 何も残っていなければ止める。帯を見せる間(NOTICE_SEC)は待つが、利用者を待っていた(帯はもう見えている)
        # あとに保存・終了して何も残らなくなったなら、すぐ止める
        if not left and not holding and (now >= self.notice_until or self.state == STATE_WAITING):
            self._decide(STATE_STOPPED, "保存していない入力・実行中の処理が無いので止めました", now)
            return
        self.writing = []
        if now >= self.deadline + self.extra_sec:
            writing = self._writing_now()
            if writing and now < self.deadline + self.extra_sec + WRITING_MAX_SEC:
                # 共有へ書いている最中は強制しない(途中で落ちると共有が半端になる)。書き終わったら止める
                if self.state != STATE_WRITING:
                    log().warning("管理者以外停止: %s(%s)", WRITING_NOTE, "、".join(writing))
                self.writing = writing
                self.state = STATE_WRITING
                self.detail = f"{WRITING_NOTE}({'、'.join(writing)})"
                self._write_local()
                self._beat(force=True)
                return
        if now >= self.deadline + self.extra_sec and self._can_force():
            grace = self.grace_of(self.request)
            detail = f"{_span(grace)}たっても止まらないので強制で止めました"
            if left:
                detail += f"(残っていたもの: {'、'.join(left)})"
            self._decide(STATE_FORCED, detail, now)
            return
        self.state = STATE_WAITING if self.left_now else STATE_NOTICE
        self.detail = "、".join(self.left_now)
        self._write_local()
        self._beat(force=True)

    def _writing_now(self) -> list:
        """共有へ書いている最中のもの(呼び名)。確かめられなければ空(書いていないとみなす。期限で強制)。"""
        if self._writing_fn is None:
            return []
        ok, got = call_limited(self._writing_fn)
        return [str(x) for x in (got or []) if str(x).strip()] if ok else []

    @staticmethod
    def grace_of(req: Optional[dict]) -> float:
        """待つ長さ。**この版の決め(5分)より短くはしない**(手で置いた `grace_sec: 1` で、利用者に
        保存する間を与えずに強制で止めない)。長くするのは上限(`GRACE_MAX_SEC`)まで。"""
        return max(GRACE_SEC, _number((req or {}).get("grace_sec"), GRACE_SEC))

    def _left(self) -> list:
        ok, got = call_limited(self._left_fn)
        if ok:
            names = [str(x) for x in (got or []) if str(x).strip()]
        else:
            # 数えられない・答えない(固まっている)なら「残っている」とみなす。期限で強制になる
            names = ["残っているものを確かめられません"]
        for item in screen_left() + test_left():
            if item not in names:
                names.append(item)
        return names

    def _save_before_force(self) -> Optional[list]:
        """強制で止める前に、手元へ置いたもの(呼び名)。控え(`save_keep`)も書く。

        **控えを手元に書けなければ None**(強制では止めない。利用者が保存・終了するのを待ち、
        管理者の一覧に理由を出す)。中身を確かめる処理が答えないだけなら、そう書いて進む。
        """
        if self._before_force is None:
            return []
        ok, got = call_limited(self._before_force)
        kept = [str(x) for x in (got or [])] if ok else ["(手元に置いたものを確かめられませんでした)"]
        if not save_keep(self.left_now, kept, self.request, need=True):
            return None
        return kept

    KEEP_FAILED = "手元に控えを書けないため、強制では止めません(保存・終了してください)"

    def _can_force(self) -> bool:
        """強制してよいか: 控えを書けた(書く要が無い)。書けなければ待ち続け、理由を出す。

        入口は、中のツールのどれかが「控えを書けない」と答えていれば強制で閉じない(`force_ok`)。
        """
        if self._force_ok is not None:
            ok, got = call_limited(self._force_ok)
            if ok and not got:
                return False
        kept = self._save_before_force()
        if kept is None:
            self.left_now.append(self.KEEP_FAILED)
            if not self._keep_warned:
                self._keep_warned = True
                log().warning("管理者以外停止: %s", self.KEEP_FAILED)
            return False
        self.kept = kept
        return True

    def _decide(self, state: str, detail: str, now: float) -> None:
        self.state, self.detail = state, detail
        self.decided = True
        if state == STATE_FORCED and self.kept:
            self.detail += f"。手元に置きました: {'、'.join(self.kept)}"
        if self.hosted:
            # 入口が閉じる。閉じないまま長く残ったら自分で止まる
            self.stop_at = now + ORPHAN_SEC
            self.detail += "(日報複合ツールが閉じます)"
        elif self.flush:
            self.stop_at = now + FLUSH_SEC        # 画面に打ちかけを置いてもらう間
        else:
            self.stop_at = now
        log().warning("管理者以外停止: %s", self.detail)
        self._write_local()
        self._beat(force=True)
        if now >= self.stop_at:
            self._go(now)

    def _go(self, now: float) -> None:
        self.stopping = True
        self._beat(force=True)

    # --- 画面へ --------------------------------------------------------
    def screen_state(self) -> Optional[dict]:
        """帯に出すもの。お願いが無ければ None。"""
        req = self.request
        if req is None:
            return None
        return {
            "id": req.get("id", ""),
            "text": describe_request(req),
            "by": f"{req.get('by_pc') or '?'}({req.get('by_login') or '?'})",
            "reason": req.get("reason") or "更新のため",
            "deadline_ms": int((self.deadline + self.extra_sec) * 1000),
            "now_ms": int(self._clock() * 1000),
            "left": list(self.left_now) if not self.decided else [],
            "state": self.state,
            "label": STATE_LABELS.get(self.state, self.state),
            "detail": self.detail,
            "stopping": self.decided,
            "hosted": self.hosted,
            "writing": list(self.writing),
            "writing_until_ms": int((self.deadline + self.extra_sec + WRITING_MAX_SEC) * 1000),
        }


_watcher: Optional[Watcher] = None
_notes: list = []
_notes_lock = threading.Lock()


def install(stop: Callable[[], None], left: Callable[[], list], **kw: Any) -> Watcher:
    """見張りを立てる(プロセスに1つ。2度目は前のものを返す)。"""
    global _watcher
    if _watcher is not None:
        return _watcher
    kw.setdefault("hard_exit", True)
    _watcher = Watcher(stop, left, **kw)
    _watcher.start()
    return _watcher


def get() -> Optional[Watcher]:
    return _watcher


def leave() -> None:
    if _watcher is not None:
        _watcher.leave()


def screen_state() -> Optional[dict]:
    return _watcher.screen_state() if _watcher is not None else None


def for_screen(take: bool = False, *, page: str = "", unsaved: Any = None) -> dict:
    """画面(帯)に渡すもの。`take` なら一度だけ出す一言も(画面の問い合わせのときだけ)。

    `page`・`unsaved` は画面が言い添える「この画面で保存していない入力」(お願いを受けているあいだだけ)。
    """
    if page and unsaved is not None:
        screen_report(page, unsaved)
    return {"state": screen_state(), "notes": take_notes() if take else []}


def from_query(args: Any) -> Optional[dict]:
    """各ツールの `/api/health` に足すもの。帯の問い合わせ(`?remote_stop=1`)のときだけ一言を渡す。"""
    if _watcher is None and not _notes:
        return None
    take = args.get("remote_stop") == "1"
    return for_screen(take, page=args.get("page", "") if take else "",
                      unsaved=args.get("unsaved") if take else None)


# --- 各ツールの入口から使う ----------------------------------------------------
def bundle_of(tool_dir: Path) -> Optional[Path]:
    """ツールのフォルダ(`<一式>\\tools\\<ツール>`)から一式のフォルダ。単体で配ったツールなら None。"""
    try:
        base = Path(tool_dir).resolve().parent.parent
        return base if (base / "portal").is_dir() else None
    except OSError:
        return None


def gate(mark_error: Callable[[str], None], stop: Callable[[], None]) -> bool:
    """起動の入口(本体を組み立てる前)。止めるなら理由を待機画面に出して True。

    単体なら `BLOCK_SHOW_SEC` 秒で終わる。日報複合ツールの中のツールは終わらない(終わると外枠が
    窓ごと閉じにかかる。入口も止まっているので、理由を出したまま待つ)。
    """
    try:
        why = startup_block()
    except Exception as exc:                  # noqa: BLE001 - 判定で起動を止めない
        log().warning("管理者以外停止のお願いを確かめられませんでした: %s", exc)
        return False
    if not why:
        return False
    try:
        mark_error(why)
    except Exception:                         # noqa: BLE001
        log().exception("起動を止めた理由を画面に出せませんでした")
    if not _setup.hosted:
        timer = threading.Timer(BLOCK_SHOW_SEC, stop)
        timer.daemon = True
        timer.start()
    return True


def watch(stop: Callable[[], None], left: Callable[[], list], *, kind: str = "browser",
          before_force: Optional[Callable[[], list]] = None, on_leave: Optional[Callable[[], list]] = None,
          restore: bool = True, writing: Optional[Callable[[], list]] = None) -> Optional[Watcher]:
    """各ツールの見張りを立てる。前に強制で止められたときの控えがあれば、画面に一度だけ知らせる。"""
    try:
        if restore:
            for note in restore_keeps():
                user_note(note)
        return install(stop, left, kind=kind, hosted=_setup.hosted, before_force=before_force,
                       on_leave=on_leave, writing=writing)
    except Exception as exc:                  # noqa: BLE001 - 見張りが立たなくても起動は続ける
        log().warning("管理者以外停止の見張りを立てられませんでした: %s", exc)
        return None


def user_note(message: str) -> None:
    """画面に一度だけ出す一言(次の画面の問い合わせで渡す)。"""
    with _notes_lock:
        _notes.append(message)


def take_notes() -> list:
    with _notes_lock:
        out = list(_notes)
        _notes.clear()
    return out


# --- 入口(portal): 中のツールの答えを読む ----------------------------------
def _local_answers() -> list:
    """中のツールの答え(新しいものだけ。お願いを受けていない「居ます」も)。"""
    try:
        files = list(local_status_dir().glob("*.json"))
    except OSError:
        return []
    now = time.time()
    out = []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if now - _number(data.get("beat_at"), 0.0) > LOCAL_STALE_SEC:
            continue
        out.append(data)
    return out


def hosted_answers(request_id: str) -> list:
    """中のツールの答え(このお願いに答えた、新しいものだけ)。"""
    return [data for data in _local_answers() if data.get("request_id") == request_id]


def hosted_writing(request_id: str) -> list:
    """中のツールのうち、共有へ書いている最中のもの(「日報: 共有への保存」)。入口も強制で閉じずに待つ。"""
    out = []
    for data in hosted_answers(request_id):
        name = str(data.get("app") or data.get("app_id") or "?")
        out += [f"{name}: {x}" for x in data.get("writing") or []]
    return out


def hosted_keep_failed(request_id: str) -> bool:
    """中のツールのどれかが「手元に控えを書けない」と答えているか(入口は強制で閉じない)。"""
    return any(Watcher.KEEP_FAILED in (data.get("left") or []) for data in hosted_answers(request_id))


def hosted_left(request_id: str) -> tuple:
    """(まだ止めてよいと答えていないツールがあるか, 残っているもの「看板: …」の並び)。"""
    pending = False
    items = []
    for data in _local_answers():
        name = str(data.get("app") or data.get("app_id") or "?")
        if data.get("request_id") != request_id:
            # 居るのに、まだこのお願いに答えていない(読めていない・見張りが遅れている)。待つ(期限で強制)
            pending = True
            items.append(f"{name}: 停止のお願いを確かめています")
            continue
        if data.get("ready"):
            continue
        pending = True
        items += [f"{name}: {x}" for x in data.get("left") or []]
    return pending, items


def reset() -> None:
    """試験用。"""
    global _watcher
    if _watcher is not None:
        _watcher._halt.set()
        _watcher._wake.set()
    _watcher = None
    take_notes()
    with _screens_lock:
        _screens.clear()
