"""共有フォルダの sqlite3 を、**手元に写してから**読む

【何のためにあるか】
参照用のマスタ(仕掛ロット・仕掛受注・梱包資材・停止理由)は、共有
フォルダに置かれた**このツールの外**のファイルです。手元の作業用DB
(`config.SETTINGS.sqlite_path`)とは扱いを分けます。

    取り込み元(外)  … このモジュール。**読むだけ**
    作業用DB(手元)  … `db/connection.py`。アプリが自由に読み書きする

【なぜ写すのか】
共有の sqlite3 を**開くと、開いた側もそのファイルに手を出します。**
読み取り専用で開いても、SQLite はロックを取り、置かれ方によっては
付き添いのファイル(`-wal` / `-shm` / `-journal`)を触ります。別の人が
書いている最中に重なると、取り残しが出ます。

現場では、参照しただけで `pending_20260811_164850.sqlite3` のような
ファイルが残る、という形で出ました。だから**共有のファイルは開きません。**
バイト列として写して、写しのほうを開きます。写すだけなら、相手の
ファイルに対しては「読む」以上のことをしません。

    共有フォルダ                     手元(%LOCALAPPDATA%\\...\\cache)
    ┌──────────────┐    写す    ┌──────────────┐
    │ 梱包資材マスタ  │ ────────→ │ (写し)        │ ← ここを開く
    │  + -wal/-shm   │           │  + -wal/-shm  │
    └──────────────┘           └──────────────┘
         触らない                      好きに開く

【写しが破れていたら】
相手が書いている最中に写すと、**中途半端なところで切り取った写し**が
できることがあります。そのまま読むと、静かに欠けた中身を取り込みます。
写したあとに `PRAGMA quick_check` を通し、破れていれば少し待って
写し直します(`COPY_RETRY`)。

【参照実装との違い】
`ViVio-Hellon/python-web-tools` の `source_db.py` は、写しを
**開けなかったときの最後の手**にしています。こちらは**最初から写します**
── あちらは取り込み元が1つの変換ツールに管理されていますが、こちらは
複数の人が同時に開くファイルなので、開かずに済ませることそのものに
意味があります。
"""
from __future__ import annotations

import atexit
import os
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from . import app_config
from .logging_setup import get_logger

log = get_logger("source_db")

# 取り込み元の拡張子。**この2つだけを探す**
SUFFIXES = (".sqlite3", ".db")

# 一緒に写すファイル。**本体だけ写すと、直前に書かれた分が抜ける**
SIDECARS = ("-wal", "-shm", "-journal")

# 共有フォルダの上でロックを待つ時間(ミリ秒)
BUSY_TIMEOUT_MS = 10_000

# 破れた写しを引いたときに、写し直す回数と、そのあいだ待つ秒数。
# 相手が書き終わるのを待つだけなので、長くは待たない
COPY_RETRY = 3
COPY_RETRY_WAIT_SEC = 0.4

# sqlite3 のファイルの先頭にある印
MAGIC = b"SQLite format 3\x00"


class SourceError(RuntimeError):
    """取り込み元を読めなかった。"""


def quote_identifier(name: str) -> str:
    """テーブル名・列名を囲む。日本語の名前がそのまま出てくるので必須。"""
    return '"' + str(name).replace('"', '""') + '"'


# ==================================================================
# URI では開きません ── **開くのは手元の写しだけ**
#
# ここには以前 `to_uri()` がありました。共有フォルダ(UNC)を
# `file:////サーバ/共有/x.sqlite3` の形に組み立てる関数です ──
# `Path.as_uri()` は `//サーバ` を authority として書き、SQLite は
# 空か `localhost` 以外の authority を受け付けないためです。
#
# **一度も呼ばれていませんでした。** 書いた回(b107eb4)で同時に
# 「読むときは写しを開く」に決めたので、URI で共有を開く道がその場で
# 無くなっています。残しておくと「ここを通っている」と読めてしまうので
# 消しました ── **効いていないものを置くと、効いているつもりで
# 次の人が数えます。**
#
# いま共有のファイルそのものを開くのは、マスタ管理から人が書くときだけ
# (`SourceConnection`)。そちらは素のパスで開きます ── `sqlite3.connect`
# は UNC のパスをそのまま受けるので、URI に直す必要がありません
# (URI に直すほうが壊れる、というのが上の話です)。
# ==================================================================


def looks_like_sqlite(path: Path) -> bool:
    """先頭の印だけを見る。**中身の正しさまでは見ない。**"""
    try:
        with open(path, "rb") as handle:
            return handle.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


# ==================================================================
# 手元への写し
# ==================================================================
# 写しの置き場。**同じファイルを何度も写さない** ── 画面は開くたびに
# 何度もマスタを引くので、そのつど共有から写すと待たされる
_COPIES: dict[str, tuple[tuple[int, int], Path]] = {}
_COPY_DIR: Optional[Path] = None


def copy_dir() -> Path:
    """写しの置き場所。利用者ごとのローカル領域の下に置く。

    共有フォルダにもアプリ本体にも書かない(基盤仕様書 2.7)。終了時に
    消さないのは、次の起動でそのまま使い回せるようにするためです ──
    元が変わっていなければ写し直しません。

    **毎回、あることを確かめます。** 一度作ったから在り続ける、とは
    言えません ── ディスクの掃除で消えることもあれば、利用者ごとの
    ローカル領域そのものが差し替わることもあります(検証用の起動、
    `NIPPOU_LOCAL_DIR`)。無い場所へ写そうとすると、参照マスタが
    まるごと「読めません」になります。
    """
    global _COPY_DIR
    if _COPY_DIR is None:
        _COPY_DIR = app_config.local_dir("cache") / "source"
    try:
        _COPY_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:                                  # pragma: no cover
        # 作れないなら、いまのローカル領域から取り直して作る
        _COPY_DIR = app_config.local_dir("cache") / "source"
        _COPY_DIR.mkdir(parents=True, exist_ok=True)
    return _COPY_DIR


def copy_dir_path() -> Path:
    """写しの置き場所。**作らない**(見せるだけのとき ── 設定画面の一覧)。"""
    return _COPY_DIR or app_config.local_dir("cache") / "source"


def _copy_name(resolved: Path) -> str:
    """写しのファイル名。**元の名前が読める形にする。**

    調べるときに開くのは写しのほうなので、`3f2a1b.sqlite3` では
    どのマスタか分かりません。名前を残したうえで、同じ名前が別のフォルダ
    にある場合に備えて道の指紋を付けます。
    """
    import hashlib

    digest = hashlib.sha1(str(resolved).encode("utf-8")).hexdigest()[:8]
    return f"{resolved.stem}_{digest}{resolved.suffix}"


def _do_copy(resolved: Path, target: Path) -> None:
    """本体と付き添いを写す。**付き添いを先に**。

    本体を先に写すと、そのあと付き添いを写すまでのあいだに相手が
    書き進めることがあり、本体より新しい付き添いを掴みます。先に
    付き添いを取っておけば、少なくとも本体より古い側に倒れます。
    """
    for extra in SIDECARS:
        side = Path(str(resolved) + extra)
        beside = Path(str(target) + extra)
        beside.unlink(missing_ok=True)
        if side.exists():
            shutil.copyfile(side, beside)
    shutil.copyfile(resolved, target)


def _is_intact(path: Path) -> bool:
    """写しが破れていないか。**引いてみて確かめる。**

    `quick_check` は `integrity_check` より軽く、ページの壊れは拾います。
    共有の上で写しが切り取られる形の壊れ方は、ここで出ます。
    """
    try:
        conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000)
    except sqlite3.Error:
        return False
    try:
        row = conn.execute("PRAGMA quick_check(1)").fetchone()
        return bool(row) and str(row[0]).lower() == "ok"
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def local_copy(path: Path) -> Path:
    """共有のファイルを手元に写して、写しの道を返す。

    中身が変わっていなければ写し直しません(大きさと更新時刻で見ます)。
    **破れた写しは返しません** ── 引けないと分かったら写し直し、
    それでも駄目なら `SourceError` にします。黙って欠けた中身を
    読ませるより、読めないと言うほうがましです。

    【控えを先に見る ── `pending_...` が残る件】
    ここは以前、控えを見る**前に** `looks_like_sqlite()` を呼んでいました。
    あれは中身を確かめるために**共有のファイルを実際に開きます。** つまり
    写しが新しくても、読むたびに共有のファイルを開いていました ──
    GWでロットを1本引くだけで3〜4回です。

    上流は `SIKALOT.pending_20260914_091528.sqlite3` に書いてから
    `SIKALOT.sqlite3` へ置き換えます(rename)。**Windowsでは、誰かが開いて
    いるファイルは置き換えられません。** こちらの開閉とぶつかると置き換えが
    失敗し、`pending_...` が取り残されます。

    いまは**控えが効いているかを先に見ます。** 大きさと更新時刻だけ
    (`stat`。開きません)で足りるので、ふつうの読みでは共有のファイルを
    1度も開きません。開くのは、本当に写し直すときだけです。
    """
    path = Path(path)

    # 絶対の道にするだけ。**共有には問い合わせない** ── `resolve()` は
    # 実体を開いて確かめに行くので、共有の上では往復が増える
    resolved = Path(os.path.abspath(path))
    key = str(resolved)
    try:
        stat = resolved.stat()
    except FileNotFoundError:
        raise SourceError(f"ファイルが見つかりません: {path}") from None
    except OSError as exc:
        raise SourceError(f"{path.name} を確かめられません: {exc}") from exc
    stamp = (stat.st_size, stat.st_mtime_ns)

    # **ここを先に。** 効いていれば、共有のファイルは1度も開かない
    known = _COPIES.get(key)
    if known is not None and known[0] == stamp and known[1].exists():
        return known[1]

    # 【写し直しは1つずつ・写しは置き換えで入れる】(v3.96.0)
    # 画面は同じマスタを**同時に**何本も引く(VC長さ計算を開くと、計算と
    # 設定の2本)。前は写しを**その場で上書き**していたので、ほかの要求が
    # 開いて読んでいる最中の写しが一度空になり、「no such table」で落ちて
    # いた。いまは別の名前へ写して確かめてから置き換える ── 読んでいる
    # 側は、読み終わるまで前の写しを持ったまま
    with _refresh_lock(key):
        known = _COPIES.get(key)                 # 待っているあいだに写し終わった
        if known is not None and known[0] == stamp and known[1].exists():
            return known[1]
        if not looks_like_sqlite(path):
            # 中身が別物なら写しても開けない。**共有を無駄に往復させない**
            raise SourceError(f"sqlite3 のファイルではありません: {path.name}")
        target = copy_dir() / _copy_name(resolved)
        part = target.with_name(f".{target.name}.{os.getpid()}.part")
        try:
            last = ""
            for attempt in range(1, COPY_RETRY + 1):
                try:
                    _do_copy(resolved, part)
                except OSError as exc:
                    raise SourceError(f"{path.name} を手元に写せません: {exc}") from exc
                if _is_intact(part):
                    if attempt > 1:
                        log.info("%s は %s回目の写しで揃いました", path.name, attempt)
                    _swap_in(part, target, path.name)
                    _COPIES[key] = (stamp, target)
                    return target
                last = "写しが途中で切れています"
                log.warning("%s の写しが揃っていません(%s回目)。写し直します",
                            path.name, attempt)
                time.sleep(COPY_RETRY_WAIT_SEC)
        finally:
            _discard(part)

    raise SourceError(
        f"{path.name} を読める形で写せませんでした({last})。"
        "書き込みが終わってからもう一度お試しください。")


# 写し直しの鍵(元のファイルごと)。**別のマスタの写し直しは待たせない**
_REFRESH_LOCKS: dict[str, threading.Lock] = {}
_REFRESH_GUARD = threading.Lock()

# 写しを置き換えるとき、ほかの要求がまだ前の写しを開いていれば待つ回数と間隔。
# **Windows は開いているファイルを置き換えられない。** 読みは数ミリ秒で終わる
SWAP_RETRY = 30
SWAP_RETRY_WAIT_SEC = 0.1


def _refresh_lock(key: str) -> threading.Lock:
    with _REFRESH_GUARD:
        return _REFRESH_LOCKS.setdefault(key, threading.Lock())


def _swap_in(part: Path, target: Path, name: str) -> None:
    """確かめ終えた写し(`part`)を `target` に置き換える。

    付き添いが残っていれば**先に**置く(`_do_copy` と同じ順)。前の写しの
    付き添いは消す ── 新しい本体に古い `-wal` が付くのが一番たちが悪い。
    """
    for attempt in range(1, SWAP_RETRY + 1):
        try:
            for extra in SIDECARS:
                side = Path(str(part) + extra)
                beside = Path(str(target) + extra)
                if side.exists():
                    os.replace(side, beside)
                else:
                    beside.unlink(missing_ok=True)
            os.replace(part, target)
            return
        except PermissionError:                  # Windows: まだ前の写しを読んでいる
            if attempt == SWAP_RETRY:
                break
            time.sleep(SWAP_RETRY_WAIT_SEC)
        except OSError as exc:
            raise SourceError(f"{name} の写しを置き換えられません: {exc}") from exc
    raise SourceError(f"{name} の写しを置き換えられません(ほかの読み込みが使っています)。"
                      "少し待ってからもう一度お試しください。")


def _discard(part: Path) -> None:
    """置き換えなかった(途中で断った)写しを片づける。"""
    for extra in ("", *SIDECARS):
        try:
            Path(str(part) + extra).unlink(missing_ok=True)
        except OSError:
            pass


def forget(path: Optional[Path] = None) -> None:
    """写しの控えを捨てる。次に読むとき写し直す。

    元のファイルが更新されたことを、大きさも更新時刻も変えずに
    知らせてくる場面はまず無いので、ふだんは要りません。設定で置き場所を
    変えたときと、テストのために使います。
    """
    if path is None:
        _COPIES.clear()
        return
    _COPIES.pop(str(Path(os.path.abspath(path))), None)


@atexit.register
def _cleanup() -> None:                              # pragma: no cover - 終了時
    """写しは残す。**消さない**のは、次の起動で使い回すため。

    利用者ごとのローカル領域なので、他の人には見えません。置き場所を
    知りたいときは設定画面の「マスタ管理」に出ます。
    """
    return


# ==================================================================
# 開く
# ==================================================================
@contextmanager
def open_source(path: Path):
    """写しを開く。**共有のファイルは開かない。**

    読み取り専用(`query_only`)で開きます。写しを書き換えても誰にも
    届かないので、書けてしまうこと自体が間違いのもとです。
    """
    copy = local_copy(path)
    conn = sqlite3.connect(str(copy), timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = 1")
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        yield conn
    except sqlite3.Error as exc:
        raise SourceError(f"{Path(path).name} を読めません: {exc}") from exc
    finally:
        conn.close()


# ==================================================================
# 読む
# ==================================================================
def list_tables(path: Path) -> list[str]:
    """このファイルにあるテーブルの一覧(sqlite の内部表は除く)。"""
    try:
        with open_source(path) as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
        return [r["name"] for r in rows]
    except SourceError as exc:
        log.warning("テーブル一覧を引けません (%s): %s", path, exc)
        return []


def columns(path: Path, table: str) -> list[str]:
    """テーブルの列名。読む前に「その列があるか」を見るのに使う。"""
    try:
        with open_source(path) as conn:
            rows = conn.execute(
                f"PRAGMA table_info({quote_identifier(table)})").fetchall()
        return [r["name"] for r in rows]
    except SourceError:
        return []


def read_table(path: Path, table: str) -> list[dict[str, Any]]:
    """1テーブルを丸ごと読む。"""
    with open_source(path) as conn:
        try:
            cursor = conn.execute(f"SELECT * FROM {quote_identifier(table)}")
            names = [d[0] for d in cursor.description or []]
            return [dict(zip(names, row)) for row in cursor.fetchall()]
        except sqlite3.Error as exc:
            raise SourceError(
                f"{Path(path).name} の {table} を読めませんでした: {exc}") from exc


def read_query(path: Path, sql: str,
               params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    """1文だけ引く。**書ける口をここに作らない**(`query_only` で開く)。"""
    with open_source(path) as conn:
        try:
            cursor = conn.execute(sql, tuple(params))
            names = [d[0] for d in cursor.description or []]
            return [dict(zip(names, row)) for row in cursor.fetchall()]
        except sqlite3.Error as exc:
            raise SourceError(
                f"{Path(path).name} を読めませんでした: {exc}") from exc


def table_counts(path: Path) -> dict[str, int]:
    """テーブルごとの行数。**1回開いて全部数える。**"""
    counts: dict[str, int] = {}
    try:
        with open_source(path) as conn:
            names = [r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()]
            for name in names:
                try:
                    row = conn.execute(
                        f"SELECT COUNT(*) AS n FROM {quote_identifier(name)}"
                    ).fetchone()
                    counts[name] = int(row["n"])
                except sqlite3.Error:
                    # 1つ数えられなくても残りは出す。見えないより見えるほうがよい
                    counts[name] = -1
    except SourceError as exc:
        log.warning("行数を数えられません (%s): %s", path, exc)
        return {}
    return counts


# ==================================================================
# 診断 ── なぜ読めないのかを、そのまま画面に出せる形で
# ==================================================================
@dataclass
class Probe:
    """1ファイルを見た結果。**分かったことを全部持つ。**

    「読めません」だけでは現場も直せません。どこまで届いていて、
    何で止まったのかを出します。
    """

    path: str = ""
    exists: bool = False
    size: int = 0
    is_sqlite: bool = False              # 先頭16バイトが sqlite3 の印か
    sidecars: list[str] = field(default_factory=list)
    copied_to: str = ""                  # 手元の写し(写せなければ空)
    copied_at: str = ""
    journal: str = ""                    # WAL / delete / ...
    tables: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.copied_to) and not self.error


def probe(path: Path) -> Probe:
    """写して開いてみて、分かったことを返す。**例外は投げない。**"""
    path = Path(path)
    out = Probe(path=str(path))
    try:
        out.exists = path.exists()
    except OSError as exc:                           # 共有に届かない
        out.error = str(exc)
        return out
    if not out.exists:
        out.error = "ファイルがありません"
        return out

    try:
        out.size = path.stat().st_size
        out.is_sqlite = looks_like_sqlite(path)
        out.sidecars = [e.lstrip("-") for e in SIDECARS
                        if Path(str(path) + e).exists()]
    except OSError as exc:
        out.error = str(exc)
        return out

    try:
        copy = local_copy(path)
    except SourceError as exc:
        out.error = str(exc)
        return out
    out.copied_to = str(copy)
    try:
        out.copied_at = time.strftime("%Y/%m/%d %H:%M:%S",
                                      time.localtime(copy.stat().st_mtime))
    except OSError:                                  # pragma: no cover
        pass

    try:
        with open_source(path) as conn:
            out.journal = str(conn.execute("PRAGMA journal_mode").fetchone()[0])
            out.tables = [r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()]
    except SourceError as exc:                       # pragma: no cover - 写せた後
        out.error = str(exc)
    return out


# ==================================================================
# 探す
# ==================================================================
def find(directory: Path, *names: str) -> Optional[Path]:
    """フォルダの中から、名前が一致するファイルを探す。

    拡張子違い(`.sqlite3` / `.db`)も見ます。上流の付け方に合わせて
    こちらが折れるほうが、現場でファイル名を直させるより早い。
    """
    directory = Path(directory)
    for name in names:
        stem = Path(name).stem
        for suffix in SUFFIXES:
            candidate = directory / f"{stem}{suffix}"
            try:
                if candidate.exists():
                    return candidate
            except OSError:                          # 共有に届かない
                return None
    return None


def is_leftover(path: Path) -> bool:
    """上流の書きかけが取り残されたものか。

    参照マスタは上流が `SIKALOT.pending_20260914_091528.sqlite3` に書いて
    から `SIKALOT.sqlite3` へ置き換えます。置き換えに失敗すると、
    書きかけのほうが残ります ── **中身は本物ですが、いつのものか
    分かりません。**

    こちらからは消しません(上流のファイルなので)。マスタの一覧から
    外すだけです ── 一覧に並ぶと、どれが本物か分からなくなります。
    """
    return ".pending_" in Path(path).name


def list_source_files(directory: Path) -> list[Path]:
    """フォルダにある取り込み元らしいファイル。設定画面に出す。

    **上流の書きかけ(`.pending_...`)は並べません**(:func:`is_leftover`)。
    """
    directory = Path(directory)
    found: list[Path] = []
    for suffix in SUFFIXES:
        try:
            found.extend(p for p in sorted(directory.glob(f"*{suffix}"))
                         if not is_leftover(p))
        except OSError:
            return []
    return sorted(set(found))


# ==================================================================
# 書く ── **元のファイルへ直に書く**
#
# 読むときは写しを開きます(共有のファイルに触らないため)。書くときは
# そうはいきません。写しを書き換えても誰にも届かないので、**元へ直に
# 書きます。**
#
# 書いたら写しの控えを捨てます(`forget`)。捨てないと、次に読んだときに
# 古い写しが出てきて、**直したのに変わっていないように見えます。**
#
# 元へ書くのは、設定画面のマスタ管理から人が押したときだけです
# (`nippou/master_admin.py`)。日々の入力がここを通ることはありません。
# ==================================================================
class SourceConnection:
    """書き込み用に開いた取り込み元。**書けるのはここだけ。**"""

    def __init__(self, path: Path, *, foreign_keys: bool = False) -> None:
        self.path = Path(path)
        try:
            self._conn = sqlite3.connect(str(self.path),
                                         timeout=BUSY_TIMEOUT_MS / 1000)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            if foreign_keys:
                # 表どうしの結びつき(品種名で親子)を守らせる。**表で決めてある
                # ファイルだけ**(VC計算マスタ)── 上流が作ったファイルに
                # 途中から効かせると、関係の無い行まで書けなくなりうる
                self._conn.execute("PRAGMA foreign_keys = ON")
        except sqlite3.Error as exc:
            raise SourceError(f"{self.path.name} を開けません: {exc}") from exc

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:                        # pragma: no cover
            pass
        # **写しの控えを捨てる。** 次に読むとき写し直させないと、
        # 直したのに変わっていないように見える
        forget(self.path)

    def __enter__(self) -> "SourceConnection":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- 読む(書く前の確認用) --------------------------------------
    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        try:
            cursor = self._conn.execute(sql, tuple(params))
            names = [d[0] for d in cursor.description or []]
            return [dict(zip(names, row)) for row in cursor.fetchall()]
        except sqlite3.Error as exc:
            raise SourceError(str(exc)) from exc

    def table_names(self) -> list[str]:
        return [r["name"] for r in self.query(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%' ORDER BY name")]

    # -- 書く --------------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        """1文を実行して、動いた行数を返す。"""
        try:
            cursor = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cursor.rowcount
        except sqlite3.Error as exc:
            raise SourceError(str(exc)) from exc

    def insert(self, table: str, values: dict[str, Any]) -> int:
        """1行足す。**値はプレースホルダで渡す**(文字列に埋め込まない)。"""
        if not values:
            raise SourceError("入れる値がありません。")
        cols = ", ".join(quote_identifier(k) for k in values)
        marks = ", ".join("?" for _ in values)
        return self.execute(
            f"INSERT INTO {quote_identifier(table)} ({cols}) VALUES ({marks})",
            list(values.values()))

    def update(self, table: str, values: dict[str, Any],
               where: dict[str, Any]) -> int:
        """条件に合う行を書き換える。

        **`where` が空なら断ります。** 空の `WHERE` は「全行を書き換える」で、
        相手は共有のマスタです ── 1回の取り違えで全員が困ります。
        呼び手が渡し忘れたのか全件を狙ったのかはここでは分からないので、
        分からないほうに倒します。
        """
        if not values:
            raise SourceError("書き換える値がありません。")
        if not where:
            raise SourceError("どの行かが決まっていません。")
        sets = ", ".join(f"{quote_identifier(k)} = ?" for k in values)
        conds = " AND ".join(f"{quote_identifier(k)} = ?" for k in where)
        return self.execute(
            f"UPDATE {quote_identifier(table)} SET {sets} WHERE {conds}",
            list(values.values()) + list(where.values()))

    def delete(self, table: str, where: dict[str, Any]) -> int:
        """条件に合う行を消す。**`where` が空なら断る**(理由は `update` と同じ)。"""
        if not where:
            raise SourceError("どの行かが決まっていません。")
        conds = " AND ".join(f"{quote_identifier(k)} = ?" for k in where)
        return self.execute(
            f"DELETE FROM {quote_identifier(table)} WHERE {conds}",
            list(where.values()))

    # -- まとめて確定する ---------------------------------------------
    @contextmanager
    def transaction(self):
        """**何文かを1回で確定する。** 途中で落ちたら何も残さない(列を足す ── v4.14.0)。

        `execute` は1文ごとに確定するので、「列を足す → 最初の値を入れる → 記録する」
        の途中で落ちると、半分だけ足された表が共有に残ります。ここは書き始めに
        ロックを取り(`BEGIN IMMEDIATE`)、最後にまとめて確定します。
        """
        try:
            self._conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise SourceError(str(exc)) from exc
        tx = _Transaction(self._conn)
        try:
            yield tx
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise


class _Transaction:
    """`SourceConnection.transaction` の中で書く口(1文ごとには確定しない)。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        try:
            cursor = self._conn.execute(sql, tuple(params))
            names = [d[0] for d in cursor.description or []]
            return [dict(zip(names, row)) for row in cursor.fetchall()]
        except sqlite3.Error as exc:
            raise SourceError(str(exc)) from exc

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        try:
            return self._conn.execute(sql, tuple(params)).rowcount
        except sqlite3.Error as exc:
            raise SourceError(str(exc)) from exc


def connect(path: Path, *, foreign_keys: bool = False) -> SourceConnection:
    """書き込み用に開く。**読むだけなら `read_query` を使う。**"""
    return SourceConnection(path, foreign_keys=foreign_keys)
