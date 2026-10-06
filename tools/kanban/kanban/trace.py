"""記録(後追い・なぜなぜ分析のための記録)

【なぜ要るのか】
これまでのログ(``DebugLog_*.txt``)は「時刻 | 文」だけで、**どの端末で・誰が・どの
画面の何の操作で・どんな入力で・何が原因で**起きたのかが残っていなかった。画面に出た
エラーと記録を突き合わせる手立ても無く、あとから「なぜ?」を重ねて調べられなかった。

ここでは 1 件 = 1 行の JSON(``.jsonl``)で、次のものを残す。

=========== ==========================================================
種類         いつ残すか
=========== ==========================================================
error        エラー(例外は**原因の連鎖**とトレースバックまで)
warning      警告(届かない・送れない、など。業務は続いたもの)
refused      画面の操作が断られた(4xx。画面に出した文言とその理由コード)
operation    状態を変えた操作(POST が通った)── **直前に何をしたか**の足跡
client       ブラウザの中で起きたエラー(画面の不具合)
startup      起動・終了(版・モード・接続先などの前提)
=========== ==========================================================

どの記録にも **端末(PC名・ログインID)・モード・担当ライン・版・プロセス** を付け、
画面の操作から来たものには **要求(経路・入力。パスワードは除く)と記録番号** を付ける。
エラーの記録番号は画面のエラー表示にも出す(「記録番号 R-…」を言ってもらえば、
その 1 件にたどり着ける)。

【置き場所】
``<記録の置き場所>/KanbanSystem/記録/<年月>/<PC名>_<年月日>.jsonl``。置き場所は設定で
決める(既定はこの端末のローカル領域の ``logs``)。共有フォルダにすると**全端末の記録を
1 か所で見られる**(ファイルは PC ごとに分かれるので混ざらない)。

【なぜなぜ分析】
設定の「記録」タブで、エラーを選ぶと 何が・いつ・どこで・誰が・直前の操作・原因の連鎖・
その時の状態 をまとめて出し、**なぜなぜ分析シート**(なぜ 1〜5・真因・対策)の下書きを
作る(:func:`why_sheet`)。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import traceback
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

APP_FOLDER = "KanbanSystem"
FOLDER = "記録"

LEVEL_ERROR, LEVEL_WARNING, LEVEL_INFO = "ERROR", "WARNING", "INFO"
KIND_LABEL = {
    "error": "エラー",
    "warning": "警告",
    "refused": "断った操作",
    "operation": "操作",
    "client": "画面のエラー",
    "startup": "起動・終了",
}

#: 既定で残す日数(この端末の記録だけを消す。共有フォルダでもほかの PC の分には触らない)
KEEP_DAYS = 365

#: 入力のうち、記録に残さないもの(名前に含まれていたら伏せる)
SECRET_WORDS = ("password", "パスワード", "token", "secret", "hash")
#: 入力の 1 項目の長さの上限(長文で記録が膨らまないように)
VALUE_MAX = 300

_lock = threading.Lock()
_local = threading.local()
_dir: Path | None = None
_context: dict[str, Any] = {}
_state_provider: Callable[[], dict[str, Any]] | None = None

#: 同じ警告(同じ場所・同じ文)を続けて書かない間隔(秒)。届かない共有フォルダは取り込みの
#: たびに同じ警告を出すので、そのまま書くと記録が同じ行で埋まる。間引いた数は次の 1 件に添える
REPEAT_SEC = 600
_repeats: dict[tuple[str, str], list[float]] = {}


# ------------------------------------------------------------------
# 置き場所・前提
# ------------------------------------------------------------------
def configure(log_dir: str | os.PathLike[str] | None, **context: Any) -> Path | None:
    """記録の置き場所と、すべての記録に付ける前提(端末・モード・版など)を決める。"""
    global _dir
    _context.update({k: v for k, v in context.items() if v is not None})
    _context.setdefault("pid", os.getpid())
    if log_dir is None:
        return _dir
    _dir = Path(log_dir) / APP_FOLDER / FOLDER
    return _dir


def set_context(**context: Any) -> None:
    _context.update(context)


def set_state_provider(provider: Callable[[], dict[str, Any]] | None) -> None:
    """エラーのときに「その時の状態」(未反映の件数など)を集める関数。"""
    global _state_provider
    _state_provider = provider


def directory() -> Path | None:
    return _dir


def root_for(log_dir: str | os.PathLike[str]) -> Path:
    return Path(log_dir) / APP_FOLDER / FOLDER


def _pc_tag(pc: str) -> str:
    text = re.sub(r'[\\/:*?"<>|\s]+', "_", str(pc or "").strip())
    return text or "不明なPC"


def _file_for(at: datetime) -> Path | None:
    if _dir is None:
        return None
    return _dir / at.strftime("%Y%m") / f"{_pc_tag(_context.get('pc', ''))}_{at:%Y%m%d}.jsonl"


# ------------------------------------------------------------------
# 書く
# ------------------------------------------------------------------
def new_ref(prefix: str = "R") -> str:
    """記録番号。画面にも出すので短く、日時が読める形にする。"""
    return f"{prefix}-{datetime.now():%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"


def write(kind: str, what: str, *, level: str = LEVEL_INFO, ref: str = "", where: str = "",
          request: dict[str, Any] | None = None, response: dict[str, Any] | None = None,
          exception: dict[str, Any] | None = None, state: dict[str, Any] | None = None,
          extra: dict[str, Any] | None = None) -> str:
    """1 件書く。書いた記録番号を返す。**書けなくても例外を出さない**(記録のために業務を止めない)。"""
    if getattr(_local, "busy", False):
        return ref
    _local.busy = True
    try:
        now = datetime.now()
        ref = ref or new_ref("E" if level == LEVEL_ERROR else "W" if level == LEVEL_WARNING else "I")
        record: dict[str, Any] = {
            "ref": ref, "at": now.strftime("%Y/%m/%d %H:%M:%S.") + f"{now.microsecond // 1000:03d}",
            "level": level, "kind": kind, "where": where, "what": what,
            **{k: _context.get(k, "") for k in ("pc", "login", "mode", "line", "version", "pid")},
        }
        if request:
            record["request"] = request
        if response:
            record["response"] = response
        if exception:
            record["exception"] = exception
        if state is None and level in (LEVEL_ERROR, LEVEL_WARNING) and _state_provider is not None:
            try:
                state = _state_provider()
            except Exception as exc:  # noqa: BLE001
                state = {"取れなかった": str(exc)}
        if state:
            record["state"] = state
        if extra:
            record["extra"] = extra
        target = _file_for(now)
        if target is None:
            return ref
        line = json.dumps(record, ensure_ascii=False, default=str)
        with _lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            # 1 件ずつ開いて閉じる(共有フォルダに置いても、開きっぱなしで掴まない)
            with open(target, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        return ref
    except Exception:  # noqa: BLE001 - 記録が書けなくても業務は続ける
        return ref
    finally:
        _local.busy = False


def describe_exception(exc: BaseException | None, tb_text: str = "") -> dict[str, Any] | None:
    """例外の**原因の連鎖**(外側 → 内側)と、それぞれがどこで起きたか。なぜなぜの材料。"""
    if exc is None:
        return None
    chain: list[dict[str, str]] = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen and len(chain) < 10:
        seen.add(id(cur))
        frames = traceback.extract_tb(cur.__traceback__) if cur.__traceback__ else []
        last = frames[-1] if frames else None
        chain.append({
            "type": type(cur).__name__,
            "message": str(cur)[:1000],
            "at": f"{_short(last.filename)}:{last.lineno} {last.name}" if last else "",
        })
        cur = cur.__cause__ or (None if cur.__suppress_context__ else cur.__context__)
    if not tb_text:
        tb_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return {"chain": chain, "traceback": tb_text[-8000:]}


def _short(path: str) -> str:
    """ファイルの場所は、アプリのフォルダからの相対で残す(PC ごとに違う前半を落とす)。"""
    text = str(path).replace("\\", "/")
    for mark in ("/kanban/", "/app/", "/dbkit/"):
        if mark in text:
            return mark.strip("/") + "/" + text.split(mark, 1)[1]
    return Path(text).name


def clean_input(data: Any) -> Any:
    """入力を記録に残せる形にする。**パスワードなどは伏せ、長い値は切る。**"""
    if isinstance(data, dict):
        out = {}
        for k, v in list(data.items())[:40]:
            if any(w in str(k).lower() for w in SECRET_WORDS):
                out[k] = "(伏せました)" if v not in (None, "") else ""
            else:
                out[k] = clean_input(v)
        return out
    if isinstance(data, (list, tuple)):
        return [clean_input(v) for v in list(data)[:40]]
    if isinstance(data, str) and len(data) > VALUE_MAX:
        return data[:VALUE_MAX] + f"…({len(data)} 文字)"
    return data


class Handler(logging.Handler):
    """``kanban`` のログの**警告以上**を記録へも書く。

    これまで各所が ``applog.warning`` / ``log.exception`` で書いていたものが、そのまま
    原因の連鎖・要求・状態つきの記録になる(取り込み・書き戻しのスレッドの失敗も)。
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            exc = record.exc_info[1] if record.exc_info else None
            level = LEVEL_ERROR if record.levelno >= logging.ERROR else LEVEL_WARNING
            request = current_request()
            where = f"{record.name} ({_short(record.pathname)}:{record.lineno} {record.funcName})"
            extra: dict[str, Any] = {"thread": record.threadName}
            if level == LEVEL_WARNING and request is None:
                # 裏の処理の同じ警告は間引く(画面の操作から来たものは毎回残す)
                import time as _time

                key = (where, record.getMessage()[:300])
                now = _time.monotonic()
                last = _repeats.get(key)
                if last and now - last[0] < REPEAT_SEC:
                    last[1] += 1
                    return
                if last and last[1]:
                    extra["前の記録からの同じ警告"] = f"{last[1]} 回(書かずに数えた)"
                _repeats[key] = [now, 0]
                if len(_repeats) > 500:
                    _repeats.clear()
            write(
                "error" if level == LEVEL_ERROR else "warning",
                record.getMessage()[:2000],
                level=level,
                ref=getattr(record, "trace_ref", "") or (request or {}).get("ref", ""),
                where=where,
                request=request,
                exception=describe_exception(exc) if exc else None,
                extra=extra,
            )
        except Exception:  # noqa: BLE001
            self.handleError(record)


class RefFilter(logging.Filter):
    """警告以上に記録番号を振り、テキストのログにも同じ番号を出す(``%(trace_ref)s``)。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "trace_ref"):
            ref = ""
            if record.levelno >= logging.WARNING:
                req = current_request()
                ref = (req or {}).get("ref") or new_ref("E" if record.levelno >= logging.ERROR else "W")
            record.trace_ref = ref
        record.trace_tail = f"  [記録 {record.trace_ref}]" if record.trace_ref else ""
        return True


def current_request() -> dict[str, Any] | None:
    """いま処理している画面の要求(Flask の中でだけ)。"""
    try:
        from flask import g, has_request_context, request
    except Exception:  # noqa: BLE001
        return None
    if not has_request_context():
        return None
    try:
        ref = g.get("trace_ref", "")
        body = None
        if request.method not in ("GET", "HEAD") and request.is_json:
            body = clean_input(request.get_json(silent=True))
        return {
            "ref": ref, "method": request.method, "path": request.path,
            "args": clean_input(request.args.to_dict()) or None, "body": body,
            "screen": request.headers.get("X-Screen", "")[:12],
        }
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------
# 読む(設定の「記録」タブ)
# ------------------------------------------------------------------
@dataclass
class Query:
    since: date
    until: date
    kinds: frozenset[str] = frozenset({"error", "warning", "refused", "client"})
    pc: str = ""
    """空ならすべての端末。"""
    text: str = ""
    limit: int = 500


def _files(root: Path, since: date, until: date, pc: str = "") -> list[Path]:
    out = []
    month = date(since.year, since.month, 1)
    while month <= until:
        folder = root / month.strftime("%Y%m")
        if folder.is_dir():
            for f in folder.glob("*.jsonl"):
                m = re.match(r"(.+)_(\d{8})\.jsonl$", f.name)
                if not m:
                    continue
                day = datetime.strptime(m.group(2), "%Y%m%d").date()
                if since <= day <= until and (not pc or m.group(1) == _pc_tag(pc)):
                    out.append(f)
        month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    return sorted(out)


def _read(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def search(q: Query, root: Path | None = None) -> list[dict[str, Any]]:
    """条件に合う記録(新しい順)。"""
    root = root or _dir
    if root is None or not root.exists():
        return []
    text = q.text.strip().casefold()
    found = []
    for f in _files(root, q.since, q.until, q.pc):
        for r in _read(f):
            if r.get("kind") not in q.kinds:
                continue
            if text and text not in json.dumps(r, ensure_ascii=False).casefold():
                continue
            found.append(r)
    # 同じ操作(同じ記録番号)の記録は 1 行にまとめる(主になるものを残す。:func:`find` と同じ順)
    best: dict[str, dict[str, Any]] = {}
    single: list[dict[str, Any]] = []
    for r in found:
        ref = r.get("ref", "")
        if not ref:
            single.append(r)
        elif ref not in best or _PRIMARY.get(r.get("kind", ""), 9) < _PRIMARY.get(best[ref].get("kind", ""), 9):
            best[ref] = r
    found = single + list(best.values())
    found.sort(key=lambda r: str(r.get("at", "")), reverse=True)
    return found[: q.limit]


def pcs(root: Path | None = None, since: date | None = None, until: date | None = None) -> list[str]:
    """記録のある端末(ファイル名の PC名)。"""
    root = root or _dir
    if root is None or not root.exists():
        return []
    until = until or date.today()
    since = since or until - timedelta(days=31)
    names = {re.sub(r"_\d{8}\.jsonl$", "", f.name) for f in _files(root, since, until)}
    return sorted(names)


#: 同じ記録番号(同じ操作)の記録が複数あるとき、どれを主にするか(小さいほど先)
_PRIMARY = {"error": 0, "client": 1, "refused": 2, "warning": 3, "startup": 4, "operation": 5}


def find(ref: str, root: Path | None = None, days: int = 400) -> dict[str, Any] | None:
    """記録番号から 1 件と、同じ端末の直前の記録・**同じ操作のほかの記録**。

    1 回の操作で、途中の警告(「パスワードが違います」)と断った結果の 2 件が同じ番号で
    残ることがある。エラー → 画面のエラー → 断った → 警告 の順で主を決め、残りは
    ``related`` に入れる。番号に月日が入っているので、まずその日のファイルを探す。
    """
    root = root or _dir
    if root is None or not ref:
        return None
    today = date.today()
    guess = None
    m = re.match(r"[A-Z]-(\d{2})(\d{2})-", ref)
    if m:
        for year in (today.year, today.year - 1):
            try:
                d = date(year, int(m.group(1)), int(m.group(2)))
            except ValueError:
                continue
            if d <= today:
                guess = d
                break
    ranges = [(guess, guess)] if guess else []
    ranges.append((today - timedelta(days=days), today))
    for since, until in ranges:
        for f in _files(root, since, until):
            rows = list(_read(f))
            hits = [i for i, r in enumerate(rows) if r.get("ref") == ref]
            if not hits:
                continue
            main = min(hits, key=lambda i: (_PRIMARY.get(rows[i].get("kind", ""), 9), i))
            first = min(hits)
            record = rows[main]
            trail = [x for x in rows[:first] if x.get("pid") == record.get("pid")][-15:]
            related = [rows[i] for i in hits if i != main]
            return {"record": record, "trail": trail, "related": related, "file": str(f)}
    return None


def prune(keep_days: int = KEEP_DAYS, root: Path | None = None) -> int:
    """**この端末の**古い記録を消す。ほかの PC の分には触らない。消した数。"""
    root = root or _dir
    if root is None or not root.exists():
        return 0
    limit = date.today() - timedelta(days=max(30, int(keep_days)))
    mine = _pc_tag(_context.get("pc", ""))
    removed = 0
    for folder in root.iterdir():
        if not folder.is_dir():
            continue
        for f in folder.glob(f"{mine}_*.jsonl"):
            m = re.search(r"_(\d{8})\.jsonl$", f.name)
            if m and datetime.strptime(m.group(1), "%Y%m%d").date() < limit:
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
        try:
            if not any(folder.iterdir()):
                folder.rmdir()
        except OSError:
            pass
    return removed


# ------------------------------------------------------------------
# なぜなぜ分析シート
# ------------------------------------------------------------------
def why_sheet(found: dict[str, Any]) -> str:
    """1 件の記録から、なぜなぜ分析シートの下書きを作る(画面でそのまま直して保存する)。

    **なぜ 1 は画面に出た理由、なぜ 2 から先は例外の連鎖を内側へ**たどった候補を入れる。
    候補は候補で、本当の「なぜ」は人が確かめて書く ── 空欄を残しておく。
    """
    r = found["record"]
    req = r.get("request") or {}
    res = r.get("response") or {}
    chain = (r.get("exception") or {}).get("chain") or []
    lines = [
        "なぜなぜ分析シート",
        "=" * 40,
        f"記録番号 : {r.get('ref', '')}",
        f"発生日時 : {r.get('at', '')}",
        f"端末     : {r.get('pc', '')}(ログインID {r.get('login', '')})",
        f"モード   : {r.get('mode', '')} / 担当ライン {r.get('line', '') or '-'} / 版 {r.get('version', '')}",
        f"どこで   : {r.get('where', '')}" + (f" / {req.get('method', '')} {req.get('path', '')}" if req else ""),
        "",
        "【事象】(何が起きたか)",
        f"  {r.get('what', '')}",
    ]
    if res.get("message"):
        lines.append(f"  画面に出した文言: {res.get('message')}(理由コード {res.get('code', '')} / {res.get('status', '')})")
    if req.get("body") or req.get("args"):
        lines.append(f"  そのときの入力: {json.dumps(req.get('body') or req.get('args'), ensure_ascii=False)}")
    for rel in found.get("related") or []:
        lines.append(f"  同じ操作の記録: {KIND_LABEL.get(rel.get('kind', ''), rel.get('kind', ''))}"
                     f" {rel.get('what', '')}({rel.get('where', '')})")
    lines += ["", "【直前の操作】(同じ端末・同じ起動のあいだ。古い順)"]
    trail = found.get("trail") or []
    if trail:
        for t in trail[-10:]:
            tr = t.get("request") or {}
            lines.append(f"  {t.get('at', '')[11:19]} {KIND_LABEL.get(t.get('kind', ''), t.get('kind', ''))}"
                         f" {tr.get('path', '') or t.get('where', '')} {t.get('what', '')[:80]}")
    else:
        lines.append("  (記録なし)")
    state = r.get("state") or {}
    if state:
        lines += ["", "【その時の状態】"] + [f"  {k}: {v}" for k, v in state.items()]
    lines += ["", "【原因の連鎖】(外側 → 内側。プログラムが残した事実)"]
    if chain:
        for i, c in enumerate(chain, 1):
            lines.append(f"  {i}. {c.get('type', '')}: {c.get('message', '')}  ({c.get('at', '')})")
    else:
        lines.append("  (例外は無し。断った理由・警告の文を事象として扱う)")

    candidates = [res.get("message") or r.get("what", "")]
    candidates += [f"{c.get('type', '')}: {c.get('message', '')}" for c in reversed(chain)][:4]
    lines += ["", "【なぜなぜ】(候補を確かめて書き直す。わかったところまでで止めない)"]
    for i in range(5):
        hint = candidates[i] if i < len(candidates) and candidates[i] else ""
        lines.append(f"  なぜ{i + 1}: {hint}")
        lines.append("    → 確かめたこと: ")
    lines += ["", "【真因】", "  ", "", "【対策】(再発防止・誰が・いつまでに)", "  ", "",
              "【確認】(対策の効果をどう確かめたか)", "  "]
    return "\n".join(lines) + "\n"


def to_csv_rows(records: Iterable[dict[str, Any]]) -> list[list[str]]:
    """一覧の CSV(Excel で開く)。"""
    rows = [["記録番号", "日時", "種類", "端末", "ログインID", "モード", "ライン", "どこで",
             "経路", "何が", "画面に出した文言", "理由コード", "原因(いちばん内側)", "版"]]
    for r in records:
        req = r.get("request") or {}
        res = r.get("response") or {}
        chain = (r.get("exception") or {}).get("chain") or []
        rows.append([
            r.get("ref", ""), r.get("at", ""), KIND_LABEL.get(r.get("kind", ""), r.get("kind", "")),
            r.get("pc", ""), r.get("login", ""), r.get("mode", ""), r.get("line", ""), r.get("where", ""),
            f"{req.get('method', '')} {req.get('path', '')}".strip(), r.get("what", ""),
            res.get("message", ""), res.get("code", ""),
            f"{chain[-1].get('type', '')}: {chain[-1].get('message', '')}" if chain else "",
            r.get("version", ""),
        ])
    return rows
