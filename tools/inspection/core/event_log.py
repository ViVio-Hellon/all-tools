"""出来事の記録(なぜなぜ分析のため)

テキストのログ(`inspection_*.log`)は流れを追うためのもの。こちらは**あとから
「なぜ」を辿るため**に、1つの出来事を1行の JSON にして残す
(`events_YYYYMMDD[_<PC>_<利用者>].jsonl`、ログと同じフォルダ)。

1行の形(使う項目だけ入る)::

    {"ts": "2026-10-01T15:02:18.123", "ref": "1001-1502-7KQ2",
     "pc": "LINE3-PC", "user": "xfa-ngykonpo", "ver": "1.6.0", "pid": 2064,
     "screen": "16ffe2e4…", "op": "print.item", "result": "ng",
     "code": "PRINT_FAILED", "message": "印刷に失敗しました。",
     "why": ["印刷に失敗しました。", "PRINT_FAILED: Excel が印刷を断った",
             "0x800A03EC プリンターが見つかりません"],
     "target": "[梱包 - 日常点検] コンベア日常点検", "file": "\\\\server\\…\\x.xlsx",
     "printer": "EPSON LP-S180DN", "elapsed_ms": 1840}

- **ref(問い合わせ番号)** … 画面の断りの文に出す番号。現場から「1001-1502-7KQ2 が
  出た」と聞けば、その行と、同じ画面の直前の操作を引ける(設定 →「ログ」)
- **result** … ok(できた)/ ng(できなかった)/ refused(業務として断った)/ info
- **why** … 「現象 → 直接の原因 → その奥の原因」の順。なぜなぜ分析の最初の段を
  そのまま埋められる形にする
- 書けなくても**業務は止めない**(記録の失敗で印刷を止めない)

同じ内容をテキストのログにも `[ref=…]` を付けて1行残す(どちらからでも引ける)。
"""
from __future__ import annotations

import contextvars
import json
import os
import random
import threading
from datetime import date, datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from . import app_config, logging_utils

OK, NG, REFUSED, INFO = "ok", "ng", "refused", "info"

# 要求ごとの問い合わせ番号(Flask の要求の初めに入れる)
current_ref: contextvars.ContextVar[str] = contextvars.ContextVar("event_ref", default="")
current_screen: contextvars.ContextVar[str] = contextvars.ContextVar("event_screen", default="")

_lock = threading.Lock()
# エラーの種類 → (何が起きたか, 確かめること)。アプリが入れる(`app/why_hints.py`)
_hints: Callable[[str], Tuple[str, List[str]]] = lambda code: ("", [])


def set_hints(func: Callable[[str], Tuple[str, List[str]]]) -> None:
    global _hints
    _hints = func
# 読み違えやすい文字(0/O・1/I/L)を使わない
_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
_MAX_TEXT = 4000


def new_ref(now: Optional[datetime] = None) -> str:
    """問い合わせ番号。`月日-時分-4文字`(電話で読み上げやすく、日時で探せる)。

    **同じ1分の中で同じ番号を2度出さない。** 番号は要求ごとに振る(画面の見張りの
    問い合わせを含む)ので、1分に数百件出ることがあり、くじの4文字だけでは重なる
    (300件で約5%)。重なると別々の出来事が同じ番号で記録に並び、問い合わせで
    取り違える。出した番号をその1分のあいだ覚えておき、重なったら引き直す
    (日報管理ツールの ``new_id`` と同じ直し方)。
    """
    now = now or datetime.now()
    head = f"{now:%m%d-%H%M}-"
    minute = f"{now:%m%d%H%M}"
    with _ISSUED_LOCK:
        if _ISSUED.get("minute") != minute:
            _ISSUED.clear()
            _ISSUED["minute"] = minute
        used = _ISSUED.setdefault("refs", set())
        for _ in range(64):
            candidate = head + "".join(random.choice(_ALPHABET) for _ in range(4))
            if candidate not in used:
                break
        used.add(candidate)
    return candidate


#: この1分に出した問い合わせ番号(``new_ref``)。分が替われば捨てる
_ISSUED: dict = {}
_ISSUED_LOCK = threading.Lock()


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _MAX_TEXT:
        return value[:_MAX_TEXT] + "…(省略)"
    return value


def why_chain(*steps: Any) -> List[str]:
    """「現象 → 原因 → その奥」を空でないものだけ並べる。"""
    out: List[str] = []
    for step in steps:
        text = str(step or "").strip()
        if text and text not in out:
            out.append(_clip(text))
    return out


def record(op: str, result: str = INFO, *, ref: str = "", message: str = "", code: str = "",
           detail: str = "", why: Optional[Iterable[Any]] = None, **fields: Any) -> str:
    """出来事を1行残す。問い合わせ番号を返す(無ければ作る)。"""
    ref = ref or current_ref.get() or new_ref()
    entry: Dict[str, Any] = {
        "ts": datetime.now().isoformat(timespec="milliseconds"),
        "ref": ref,
        "pc": logging_utils.pc_name(),
        "user": logging_utils.user_name(),
        "ver": app_config.version(),
        "pid": os.getpid(),
        "screen": fields.pop("screen", None) or current_screen.get() or "",
        "op": op,
        "result": result,
    }
    if code:
        entry["code"] = code
    if message:
        entry["message"] = _clip(message)
    if detail:
        entry["detail"] = _clip(detail)
    meaning, checks = _hints(code) if code else ("", [])
    if why is not None:
        chain = list(why)
    elif result in (NG, REFUSED):
        # 現象(画面に出した文)→ 何が起きたか(種類の説明)→ 詳しい原因(Excel・OS の返答)
        chain = why_chain(message, f"{meaning}({code})" if meaning else code, detail)
    else:
        chain = []
    if chain:
        entry["why"] = [_clip(str(s)) for s in chain]
    if checks and result in (NG, REFUSED) and "checks" not in fields:
        entry["checks"] = checks
    for key, value in fields.items():
        if value is not None and value != "":
            entry[key] = _clip(value)
    _write(entry)
    _mirror(entry)
    return ref


def _write(entry: Dict[str, Any]) -> None:
    line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
    with _lock:
        for _ in range(2):
            path = logging_utils.events_path_for(date.today())
            try:
                with open(path, "a", encoding="utf-8") as fp:
                    fp.write(line)
                return
            except OSError as exc:
                # 指定のフォルダ(共有)に書けない。ローカルへ移ってもう一度
                if not logging_utils._target.fall_back(f"{exc.strerror or exc}"):
                    return


def _mirror(entry: Dict[str, Any]) -> None:
    log = logging_utils.get_logger("event")
    text = " ".join(str(entry.get(k, "")) for k in ("op", "result", "code") if entry.get(k))
    extra = entry.get("message") or ""
    if entry.get("detail"):
        extra += f" / {entry['detail']}"
    level = 30 if entry["result"] == NG else 20     # WARNING / INFO
    log.log(level, "[ref=%s] %s %s", entry["ref"], text, extra)


# ------------------------------------------------------------------
# 読む(設定画面の「ログ」タブ)
# ------------------------------------------------------------------
def _files(days: int, own_only: bool) -> List[str]:
    folder = logging_utils.log_dir()
    folders = [folder]
    local = app_config.local_dir("logs")
    if local != folder:
        folders.append(local)                 # 退避していた期間のぶんも読む
    since = (datetime.now().date().toordinal() - days)
    out = []
    for f in folders:
        try:
            names = os.listdir(f)
        except OSError:
            continue
        for name in names:
            if not (name.startswith("events_") and name.endswith(".jsonl")):
                continue
            stamp = name[7:15]
            try:
                if datetime.strptime(stamp, "%Y%m%d").date().toordinal() < since:
                    continue
            except ValueError:
                continue
            rest = name[15:-6]
            if own_only and rest and rest != "_" + logging_utils.owner_tag():
                continue
            out.append(os.path.join(f, name))
    return sorted(out)


def read(*, days: int = 7, own_only: bool = True, ref: str = "", result: str = "",
         limit: int = 200) -> List[Dict[str, Any]]:
    """新しい順。`ref` を渡すと**ほかの端末のファイルも**探す(共有フォルダで後追いするため)。"""
    rows: List[Dict[str, Any]] = []
    seq = 0                                       # 同じ時刻(ミリ秒)の行は書いた順に並べる
    for path in _files(days if not ref else 400, own_only and not ref):
        try:
            with open(path, "r", encoding="utf-8") as fp:
                for line in fp:
                    if ref and ref not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if result and entry.get("result") != result:
                        continue
                    if ref and entry.get("ref") != ref:
                        continue
                    seq += 1
                    entry["_seq"] = seq
                    rows.append(entry)
        except OSError:
            continue
    rows.sort(key=lambda e: (e.get("ts", ""), e["_seq"]), reverse=True)
    for e in rows:
        e.pop("_seq", None)
    return rows[:limit]


def timeline(entry: Dict[str, Any], before: int = 15) -> List[Dict[str, Any]]:
    """その出来事の**直前**に、同じ端末・同じアプリで起きていたこと(古い順)。"""
    ts = entry.get("ts", "")
    pid, pc = entry.get("pid"), entry.get("pc")
    try:
        age = (date.today() - datetime.fromisoformat(ts).date()).days
    except ValueError:
        age = 0
    rows = [e for e in read(days=age + 1, own_only=False, limit=5000)
            if e.get("pc") == pc and e.get("pid") == pid and e.get("ts", "") <= ts]
    return list(reversed(rows[:before + 1]))
