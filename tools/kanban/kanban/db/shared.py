"""共有 SQLite(正式なデータの置き場所)への読み書き

**以前は Access(.accdb)でした。** ACE OLEDB の有無で動いたり動かなかったり
し、書き戻しは ``cscript`` で VBScript を起動する経路(``accdb/ado_bridge.py``)
に頼っていました。取り込み元を sqlite3 にしたので、その分岐も外部ドライバも
丸ごと不要になり、標準ライブラリだけでどの端末でも同じように読み書きできます。

【2 層構造は変わりません】

    共有 SQLite(このモジュール) … 正式なデータ。全端末が読み書きする
    手元の作業用 SQLite(``store.py``) … 端末ごとの写し + 送信待ち

Access をやめても、この 2 層は残します。操作のたびに共有フォルダへ往復すると
遅く、共有が落ちている間は何もできなくなるためです(2 層なら手元で操作を
続け、繋がったときにまとめて送れます)。

【共有フォルダの上で気をつけていること】

* **読むときは共有ファイルを直接開きません。** 手元へ写して、その写しを
  開きます。他端末が開いている最中の sqlite3 を読みに行くと、共有フォルダ側に
  一時ファイル(``-journal`` / ``-wal`` / ``-shm``、復旧途中の中間ファイル)が
  残ることがあるためです。現場では ``pending_20260811_164850.sqlite3`` のような
  ファイルが増える形で現れました。写してから読めば、共有フォルダに対しては
  **バイト列をコピーするだけ**になります(:meth:`SharedDb.local_copy`)
* **WAL を使いません。** WAL は共有メモリ(``-shm``)を使いますが、SMB には
  それがありません。共有フォルダに置いた WAL のファイルは**読むだけでも
  開けない**ことがあります。ここでは ``journal_mode = DELETE`` のままにします
* **ロック待ちを長めに取ります**(``busy_timeout``)。SQLite の書き込みは
  DB 全体の排他なので、他端末と当たったら少し待てば通ります
* **書き込みは短く区切ります。** 1 文ずつ自動コミットし、長いトランザクション
  で共有を掴み続けません
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import itertools
import threading
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

from .. import applog
from ..accdb.types import CATEGORY_DATE, CATEGORY_NUMBER, CATEGORY_TEXT
from dbkit.sqlite_toolkit import LOCK_ERROR_SNIPPETS

#: 共有フォルダの上でロックを待つ時間(ミリ秒)。他端末が書いている最中に
#: 当たっても、すぐ諦めずに少し待つ
BUSY_TIMEOUT_MS = 10_000

#: ロック競合で待つ秒数(VBA 版のリトライと同じ 1, 2, 3 秒)
RETRY_WAITS = (1.0, 2.0, 3.0)

#: 共有 DB の拡張子。**この 2 つだけを扱う**
SUFFIXES = (".sqlite3", ".db")

#: 行を 1 つだけ指すための隠し列。SQLite の ``rowid`` をこの名前で持ち回る。
#:
#: **業務の列と衝突しない名前**にしてあります(全角を混ぜているのはそのため)。
#: 管理番号のような業務のキーで指すと、そのキーが重複した表では**1 回の
#: 書き換えが 2 行に当たります** ── 「直したつもりが別の行だった」を
#: 起こさないための逃げ道です。
ROW_KEY = "__行"


class SharedDbError(RuntimeError):
    """共有 DB を読み書きできなかった。"""


@dataclass
class TableSnapshot:
    """1 テーブルの読み取り結果。

    ``accdb/gateway.py`` の同名クラスと同じ形にしてあります ── 取り込み側
    (``sync.py``)がどちらから来たかを気にせず扱えるようにするためです。
    """

    name: str
    columns: list[str]
    categories: dict[str, str]
    """列名 -> 型カテゴリ(NUMBER / DATE / TEXT)。"""

    rows: list[dict[str, Any]]

    def key_column(self) -> str:
        return self.columns[0] if self.columns else ""


@dataclass
class StatementResult:
    """SQL 1 文の実行結果。"""

    ok: bool
    affected: int = 0
    error_message: str = ""

    @property
    def is_lock_conflict(self) -> bool:
        return not self.ok and _looks_like_lock(self.error_message)


#: 実行する 1 文。**リテラルを文字列へ埋め込まない** ── 値はプレース
#: ホルダで渡す。Access 方言の ``#日付#`` を組み立てていた頃に必要だった
#: エスケープ処理(``accdb/sql.py``)が丸ごと不要になった
Statement = tuple[str, Sequence[Any]]


class SharedDb:
    """共有 SQLite への読み書き。

    ``AccdbGateway`` と同じ役目・同じ呼び方(``table_names`` / ``read_table``
    / ``execute``)にしてあるので、``sync.py`` から見た形は変わりません。
    """

    def __init__(
        self,
        path: str,
        *,
        busy_timeout_ms: int = BUSY_TIMEOUT_MS,
        max_retry: int = 3,
        cache_dir: str | None = None,
    ) -> None:
        self.path = str(path)
        self.busy_timeout_ms = int(busy_timeout_ms)
        self.max_retry = max(0, int(max_retry))
        # 写しの置き場。省略するとローカル領域の ``cache``。テストや検証で
        # 差し替えられるようにしてある
        self._cache_dir = cache_dir
        self._copy_path: Path | None = None
        self._copy_stamp: tuple[int, int] | None = None
        # 「次は必ず写し直す」(:meth:`forget_copy`)。ほかの接続が作った写しも使わない
        self._recopy = False
        self.copies_made = 0
        """起動してから共有ファイルを丸ごと写した回数。

        **共有フォルダへの往復が見えるようにするための目盛りです。**
        中身が変わっていなければ写さない作りなので、これが伸び続ける
        なら「誰かが書き続けている」ということになります(心拍の間隔や
        書き戻しの間隔を見直す手がかり)。
        """

    # -- 状態 -----------------------------------------------------------
    @property
    def mode(self) -> str:
        """接続経路。外部ドライバに依存しないので常に ``sqlite``。

        ``AccdbGateway.mode``(ado / odbc / file)と同じ位置づけの値で、
        画面とログに出す。分岐が無くなったので 1 つしかない。
        """
        return "sqlite"

    @property
    def can_write(self) -> bool:
        """書き戻せるか。

        Access の頃は ACE OLEDB の有無で決まっていた。いまはファイルへ
        辿り着けるかどうかだけで、**読めれば書ける**。
        """
        return self.exists()

    def exists(self) -> bool:
        try:
            return Path(self.path).is_file()
        except OSError:
            return False

    def describe(self) -> str:
        return f"共有DB: {self.path} (経路={self.mode})"

    # -- 読み ------------------------------------------------------------
    def table_names(self) -> list[str]:
        """テーブル名の一覧(内部テーブルは除く)。"""
        with closing(self._connect(read_only=True)) as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            ).fetchall()
        return [r[0] for r in rows]

    def select(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        """読むだけの問い合わせ(手元の写しに対して)。表を丸ごと読まずに済ませたいとき。"""
        with closing(self._connect(read_only=True)) as conn:
            conn.row_factory = sqlite3.Row
            try:
                return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]
            except sqlite3.Error as exc:
                raise SharedDbError(f"共有DBを読めませんでした: {exc}") from exc

    def has_kanban_tables(self) -> bool:
        """看板の表(``看板_<ライン>``)が 1 つでもあるか。無ければ看板マスタではない。"""
        from .. import config

        return any(n.startswith(config.KANBAN_TABLE_PREFIX) for n in self.table_names())

    def column_names(self, table_name: str) -> list[str]:
        """表の列名(行は読まない)。表が無ければ空。"""
        with closing(self._connect(read_only=True)) as conn:
            try:
                info = conn.execute(f"PRAGMA table_info({_ident(table_name)})").fetchall()
            except sqlite3.Error as exc:
                raise SharedDbError(f"{table_name} の列を読めませんでした: {exc}") from exc
        return [row[1] for row in info]

    def read_table(
        self, table_name: str, *, with_row_key: bool = False
    ) -> TableSnapshot:
        """1 テーブルを丸ごと読む。

        ``with_row_key`` を真にすると、SQLite の ``rowid`` を
        :data:`ROW_KEY` の名前で各行に添えます。**行を確実に 1 つだけ指す**
        ための値で、マスタ管理の書き換え・削除がこれを使います
        (:mod:`kanban.presenters.master`)。

        取り込み(:mod:`kanban.db.sync`)は列名で対応づけるので、余計な列を
        渡さないよう既定では添えません。
        """
        select = "*"
        if with_row_key:
            select = f'rowid AS {_ident(ROW_KEY)}, *'
        with closing(self._connect(read_only=True)) as conn:
            conn.row_factory = sqlite3.Row
            try:
                cursor = conn.execute(f"SELECT {select} FROM {_ident(table_name)}")
            except sqlite3.Error as exc:
                if with_row_key:
                    # WITHOUT ROWID の表には rowid が無い。**読めないより、
                    # 行を指せないほうがまし** ── 読みだけは通す
                    return self.read_table(table_name)
                raise SharedDbError(f"{table_name} を読めませんでした: {exc}") from exc
            columns = [d[0] for d in cursor.description]
            rows = [{k: r[k] for k in columns} for r in cursor.fetchall()]
            visible = [c for c in columns if c != ROW_KEY]
            categories = self._categories(conn, table_name, visible)
        return TableSnapshot(
            name=table_name, columns=visible, categories=categories, rows=rows
        )

    def _categories(
        self, conn: sqlite3.Connection, table_name: str, columns: list[str]
    ) -> dict[str, str]:
        """列の型カテゴリ(NUMBER / DATE / TEXT)。

        SQLite は型が緩いので、宣言型から素直に決める。Access から変換した
        DB は ``tools/accdb_to_sqlite.py`` が元の型に合わせて宣言している。
        """
        out: dict[str, str] = {}
        try:
            info = conn.execute(f"PRAGMA table_info({_ident(table_name)})").fetchall()
        except sqlite3.Error:
            info = []
        declared = {row[1]: (row[2] or "").upper() for row in info}
        for name in columns:
            decl = declared.get(name, "")
            if "DATE" in decl or "TIME" in decl:
                out[name] = CATEGORY_DATE
            elif any(k in decl for k in ("INT", "REAL", "NUM", "DOUB", "FLOA", "DEC")):
                out[name] = CATEGORY_NUMBER
            else:
                out[name] = CATEGORY_TEXT
        return out

    # -- 書き ------------------------------------------------------------
    def execute(self, statements: Sequence[Statement]) -> list[StatementResult]:
        """まとめて実行する。**1 文ずつ独立にコミットする。**

        トランザクションでまとめないのは、1 文の失敗で他を巻き戻すと
        「送ったつもりで届いていない」状態が起きるためです(呼び出し側は
        1 文ずつ成否を見て ``dirty`` を落とすので、まとめて巻き戻されると
        実際には反映されていない更新を「成功」と誤認します)。VBA 版が
        1 行ずつ書き戻していたのとも一致します。
        """
        if not statements:
            return []

        results: list[StatementResult] = []
        # **必ず閉じる。** ``with conn:`` は確定するだけで閉じない。閉じ忘れた接続は
        # 共有フォルダのファイルを開いたまま残し、Windows ではほかの端末・ほかの
        # 処理が名前を変える・置き換えることを断られる(Windows の試験で見つかった)
        conn = self._connect(read_only=False)
        try:
            conn.isolation_level = None  # 自動コミット(明示的に短く区切る)
            for sql, params in statements:
                results.append(self._execute_one(conn, sql, params))
        finally:
            conn.close()
            if any(r.ok for r in results):
                # **書いたら、次に読むときは必ず写し直す。** 共有フォルダは大きさ・更新
                # 時刻を数秒覚えて返すので、書いた直後の取り込みが「変わっていない」と
                # 読んで書く前の写しを使い、送ったばかりの値を手元で戻していた
                # (:meth:`forget_copy`)
                self.forget_copy()
        return results

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """共有ファイルを開いて、**まとめて 1 回で確定する**書き込み。

        :meth:`execute` は 1 文ずつ確定する(書き戻しの失敗を 1 文ずつ見るため)。
        表の中身を丸ごと入れ替えるような「消して入れる」は、途中で落ちたら
        空の表が残るので、ここで ``BEGIN IMMEDIATE`` 〜 ``COMMIT`` にまとめる。
        失敗したら巻き戻して例外をそのまま上げる。終わったら手元の写しを捨てる。
        """
        conn = self._connect(read_only=False)
        conn.isolation_level = None
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()
            self.forget_copy()

    def _execute_one(
        self, conn: sqlite3.Connection, sql: str, params: Sequence[Any]
    ) -> StatementResult:
        """1 文を実行する。ロック競合だけは待って試し直す。"""
        last = ""
        for attempt in range(self.max_retry + 1):
            try:
                cursor = conn.execute(sql, tuple(params))
                return StatementResult(ok=True, affected=cursor.rowcount)
            except sqlite3.Error as exc:
                last = str(exc)
                if not _looks_like_lock(last) or attempt >= self.max_retry:
                    applog.error("共有DB: 実行に失敗 %s / SQL=%s", last, sql)
                    return StatementResult(ok=False, error_message=last)
                wait = RETRY_WAITS[min(attempt, len(RETRY_WAITS) - 1)]
                applog.warning(
                    "共有DB: ロック競合(%d 回目)。%.0f 秒待ちます", attempt + 1, wait
                )
                time.sleep(wait)
        return StatementResult(ok=False, error_message=last)

    # -- 接続 ------------------------------------------------------------
    def _connect(self, *, read_only: bool) -> sqlite3.Connection:
        """共有 DB を開く。

        **読むときは共有ファイルを直接開きません。** 手元へ写してから、その
        写しを開きます(:meth:`local_copy` と下の説明を参照)。書くときだけ
        共有ファイルそのものを開きます。
        """
        self._require_exists()
        if read_only:
            return self._open_copy()
        # **パスはそのまま渡す。URI にしない。**
        #
        # 以前は ``file:`` の URI に組み直して開いていた。ところが共有フォルダの
        # パス ``\\nlmfangyshrd\各課共有\…`` は ``file://nlmfangyshrd/…`` になり、
        # SQLite はこの ``nlmfangyshrd`` の部分(authority)を**ネットワークに
        # 触る前に**断る::
        #
        #     sqlite3.OperationalError: invalid uri authority: nlmfangyshrd
        #
        # 読むほうは手元へ写してから開くので素のパスで動いており、**書くほう
        # だけが共有フォルダで全滅していた** ── マスタに行を足すと 500、
        # 書き戻しは失敗し続けて「未反映」が減らない。書くときに URI の
        # 指定(``mode=`` など)は何も使っていないので、素のパスで開けば済む。
        # Windows の SQLite は ``\\server\share\…`` をそのまま開ける
        # (接続先を保存するときの確認 ``_is_usable_sqlite`` も素のパスで開いている)
        try:
            conn = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000)
            conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        except sqlite3.Error as exc:
            raise SharedDbError(f"共有DBを開けませんでした ({self.path}): {exc}") from exc
        # **WAL にしない。** 共有メモリを使うので SMB の上では開けなくなる
        return conn

    def _require_exists(self) -> None:
        if not self.exists():
            raise SharedDbError(
                f"共有DBが見つかりません: {self.path}\n"
                "  ・共有フォルダに届いているか\n"
                "  ・設定画面の接続先が正しいか\n"
                "  を確認してください。"
            )

    def _open_copy(self) -> sqlite3.Connection:
        """手元の写しを読み取り専用で開く。"""
        local = self.local_copy()
        conn = sqlite3.connect(str(local), timeout=self.busy_timeout_ms / 1000)
        try:
            # 写しなので書いても誰にも届かない。**書けないようにしておく** ──
            # 書けてしまうと「直したのに反映されない」という一番たちの悪い
            # 壊れ方になる
            conn.execute("PRAGMA query_only = 1")
            return conn
        except sqlite3.Error:
            conn.close()
            raise

    # ------------------------------------------------------------------
    # 手元への写し
    # ------------------------------------------------------------------
    # **読むときは、共有ファイルを直接開かない。**
    #
    # 【なぜそこまでするのか】
    # 他の端末が開いている最中の sqlite3 を読みに行くと、共有フォルダ側に
    # 一時ファイルが残ることがあります(``-journal`` / ``-wal`` / ``-shm``、
    # および復旧途中の中間ファイル)。現場では
    # ``pending_20260811_164850.sqlite3`` のようなファイルが増えていく形で
    # 現れました。読むだけのつもりが共有フォルダを汚し、しかも**誰が消して
    # よいのか分からない**ファイルが残ります。
    #
    # 写してから読めば、共有フォルダに対しては**バイト列をコピーするだけ**に
    # なります。SQLite としては一切開かないので、一時ファイルは手元にしか
    # 作られません。
    #
    # 【書き戻しは写しへ向けない】
    # 写しへ書いても誰にも届きません。書くときは共有ファイルを直接開きます
    # (:meth:`execute`)。
    #
    # 【同じものを何度も写さない】
    # 取り込みはテーブルごとに開き直すので、ラインの数だけ写すと共有への
    # 往復だけで待たされます。大きさと更新時刻が同じなら写しを使い回します。
    _SIDECARS = ("-wal", "-shm", "-journal")

    def cache_dir(self) -> Path:
        """写しの置き場。ローカル領域の ``cache``(基盤仕様書 2.7)。"""
        if self._cache_dir is not None:
            return Path(self._cache_dir)
        from .. import app_config

        return app_config.local_dir("cache")

    def forget_copy(self) -> None:
        """次に読むときは、必ず写し直させる。

        写し直すかどうかは共有 DB の「大きさと更新時刻」で決めています。ところが
        **Windows の共有フォルダは、ファイルの大きさ・更新時刻を数秒ぶん覚えて
        おいて返します**(SMB クライアントの属性の控え)。書いた直後に取り込むと
        「変わっていない」と読み、**書く前の写しをそのまま使う**ことがあります
        ── マスタで看板を足したのに看板画面に出ない、の一因。利用者が「いま
        取り込む」と言ったとき(マスタを直した直後を含む)は、これで写し直させる。
        """
        self._copy_stamp = None
        self._recopy = True

    def local_copy(self, *, force: bool = False) -> Path:
        """共有 DB を手元へ写して、その場所を返す。

        中身(大きさと更新時刻)が変わっていなければ写し直しません。

        【写しは上書きしない】
        同じ共有 DB を、いくつもの要求が同時に読む(設定画面はアクセス権限・マスタ・
        状態を一度に取りに来る。取り込みも背景で写す)。以前は写しの名前が 1 つで、
        ある要求が読んでいる最中の写しへ別の要求が写し直していた ── 読む側は
        書きかけのファイルを読み、**アクセス権限の表が空**と読んで「倉庫モードの
        権限なし」と言い、その空の結果を「前回読めた内容」として覚えた。
        いまは写すたびに**新しい名前**で作り、出来上がってから使わせる。同じ共有 DB
        を写すのは同時に 1 つだけで、待っていた側は出来上がった写しを使う。
        """
        self._require_exists()
        source = Path(self.path)
        stat = source.stat()
        stamp = (stat.st_size, stat.st_mtime_ns)
        fresh = force or self._recopy

        if not fresh and self._copy_stamp == stamp:
            existing = self._copy_path
            if existing is not None and existing.exists():
                return existing

        base = self._copy_target(source)
        with _copy_lock(base):
            # 待っているあいだに、ほかの接続が同じ中身を写し終えていれば、それを使う
            done = _COPIES.get(str(base))
            if not fresh and done is not None and done[0] == stamp and done[1].exists():
                self._copy_stamp, self._copy_path = done
                return done[1]
            # 時刻だけでは重なる(Windows の時計は刻みが粗い)ので、通し番号も付ける
            target = base.with_name(f"{base.stem}_{time.time_ns():x}_{next(_COPY_SERIAL)}{base.suffix}")
            path = self._make_copy(source, target, stamp)
            _COPIES[str(base)] = (stamp, path)
            self._recopy = False
            _sweep_copies(base, keep=path)
            return path

    def _make_copy(self, source: Path, target: Path, stamp: tuple[int, int]) -> Path:
        last_error = ""
        # 写している最中に他端末が書くと、途中の状態を写しうる。開いて
        # 確かめ、駄目ならもう一度だけ写す(**壊れた写しを黙って読まない**)
        for attempt in range(2):
            try:
                self._copy_files(source, target)
            except OSError as exc:
                raise SharedDbError(
                    f"共有DBを手元へ写せませんでした ({self.path}): {exc}"
                ) from exc
            if _is_usable_sqlite(target):
                self._copy_stamp = stamp
                self._copy_path = target
                self.copies_made += 1
                applog.debug("共有DBを手元へ写しました: %s → %s", source, target)
                return target
            _remove_copy(target)
            last_error = "写しを sqlite3 として開けませんでした"
            if attempt == 0:
                applog.warning(
                    "共有DBの写しが不完全でした。写し直します: %s", source
                )
                time.sleep(0.5)

        raise SharedDbError(
            f"共有DBを手元へ写せましたが読めませんでした ({self.path}): {last_error}\n"
            "  他の端末が書き込み中の可能性があります。少し待ってからやり直してください。"
        )

    def _copy_target(self, source: Path) -> Path:
        directory = self.cache_dir()
        directory.mkdir(parents=True, exist_ok=True)
        # 接続先ごとに別の名前にする(接続先を変えても混ざらない)
        digest = hashlib.sha1(str(source).encode("utf-8")).hexdigest()[:12]
        return directory / f"shared_{digest}{source.suffix or '.sqlite3'}"

    def _copy_files(self, source: Path, target: Path) -> None:
        """本体と、あれば付属ファイルを写す。

        ``-wal`` / ``-shm`` を置いていくと、**直前に書かれた分が抜けた**
        中身を読むことになります(WAL にはまだ本体へ移していない変更が
        入っている)。本体だけ写すのは危険なので、あるものは一緒に写します。
        """
        for extra in self._SIDECARS:
            side = Path(str(source) + extra)
            beside = Path(str(target) + extra)
            beside.unlink(missing_ok=True)
            if side.exists():
                shutil.copyfile(side, beside)
        shutil.copyfile(source, target)


# ------------------------------------------------------------------
# 写しの取り合いを防ぐ(:meth:`SharedDb.local_copy`)
# ------------------------------------------------------------------
_COPY_GUARD = threading.Lock()
_COPY_LOCKS: dict[str, threading.Lock] = {}
#: 共有 DB ごとの、いちばん新しい写し ``{名前の元: ((大きさ, 更新時刻), 写し)}``
_COPIES: dict[str, tuple[tuple[int, int], Path]] = {}
_COPY_SERIAL = itertools.count(1)

#: 古い写しを消すまでの時間(秒)。読んでいる最中の写しを消さないよう、読むのに
#: かかる時間より十分長くする(消せなかったものは次の機会に消す)
COPY_KEEP_SEC = 120


def _copy_lock(base: Path) -> threading.Lock:
    with _COPY_GUARD:
        return _COPY_LOCKS.setdefault(str(base), threading.Lock())


def _remove_copy(path: Path) -> None:
    for extra in ("", *SharedDb._SIDECARS):
        try:
            Path(str(path) + extra).unlink(missing_ok=True)
        except OSError:
            pass   # 開かれている(Windows)。次の機会に消す


def _sweep_copies(base: Path, *, keep: Path) -> None:
    """同じ共有 DB の古い写しを片付ける(しばらく前のものだけ)。"""
    limit = time.time() - COPY_KEEP_SEC
    for old in base.parent.glob(f"{base.stem}*{base.suffix}"):
        if old == keep:
            continue
        try:
            if old.stat().st_mtime < limit:
                _remove_copy(old)
        except OSError:
            pass


def kanban_tables_in(path: Path) -> tuple[list[str], list[str]]:
    """そのファイルの ``(看板の表, ほかの表)``。**看板マスタか**を見分けるのに使う。

    梱包資材マスタなど、別のツールの sqlite3 を接続先に選んでも「sqlite3 として
    開ける」ので通ってしまい、このツールの表(看板履歴・看板コメント)をそこへ
    作っていた。看板の表(``看板_<ライン>``)が 1 つも無ければ看板マスタではない。
    """
    from .. import config

    conn = sqlite3.connect(str(path))
    try:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            " ORDER BY name")]
    finally:
        conn.close()
    kanban = [n for n in names if n.startswith(config.KANBAN_TABLE_PREFIX)]
    return kanban, [n for n in names if n not in kanban]


def not_kanban_reason(path: str, others: Sequence[str]) -> str:
    """看板の表が無いファイルを断るときの一言(どれも同じ言い方にする)。"""
    shown = "、".join(list(others)[:5]) + (" …" if len(others) > 5 else "")
    return (f"看板の表(看板_LVC など)が 1 つもありません: {path}\n"
            "梱包資材マスタなど、別のツールのファイルではありませんか。"
            + (f"(入っている表: {shown})" if shown else "(表が 1 つもありません)"))


def _is_usable_sqlite(path: Path) -> bool:
    """写しが sqlite3 として読めるか。**中身まで軽く確かめる。**

    ``sqlite3.connect()`` はファイルを触らないので、開けたつもりのまま
    返ってきて最初の問い合わせで落ちます。ここで1文引いて確かめます。
    """
    try:
        conn = sqlite3.connect(str(path))
    except sqlite3.Error:
        return False
    try:
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return True
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def _looks_like_lock(message: str) -> bool:
    """他の端末が書いている最中か。時間をおけば通る見込みがある。

    判定の語句は :mod:`dbkit.sqlite_toolkit` が持つものをそのまま使う
    (手元の SQLite と共有の SQLite で判定が食い違わないようにする)。
    """
    lowered = (message or "").lower()
    return any(snippet in lowered for snippet in LOCK_ERROR_SNIPPETS)


def _ident(name: str) -> str:
    """識別子を囲む。SQLite は Access と同じ ``[...]`` を受け付ける。"""
    return "[" + str(name).replace("]", "]]") + "]"


def build_update(
    table: str,
    assignments: dict[str, Any],
    key_column: str,
    key_value: Any,
) -> Statement:
    """状態列だけを更新する 1 文を組み立てる。

    **値は埋め込まずプレースホルダで渡します。** Access 方言のリテラル生成
    (``#yyyy-mm-dd hh:nn:ss#`` やクォートの二重化)が不要になったので、
    エスケープ漏れという種類の不具合が構造的に起こりません。
    """
    if not assignments:
        raise ValueError("更新する列がありません")
    sets = ", ".join(f"{_ident(c)} = ?" for c in assignments)
    sql = f"UPDATE {_ident(table)} SET {sets} WHERE {_ident(key_column)} = ?"
    return sql, [*assignments.values(), key_value]
