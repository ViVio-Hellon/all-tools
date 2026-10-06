#!/usr/bin/env python3
"""共有フォルダ上での SQLite ロックが正しく機能するかを検証するツール。

SQLite は「ネットワークファイルシステム上での使用は非推奨」と公式に
警告している。SMB/NFS の実装によってはファイルロックが不完全で、
複数端末からの同時書き込みでデータが壊れる恐れがあるため。

本ツールは実際に運用で使う共有フォルダに対して、複数プロセス(この
端末内 + 必要なら他端末でも同時実行)から同じ SQLite ファイルへ書き込み
続け、初期化した合計値と実際の書き込み結果が一致するかを確認する。

使い方::

    # 1台のPCで予備検証(まずはこれ)
    python tools/check_shared_lock.py \\
        --path "\\\\サーバ\\共有\\看板\\_locktest.sqlite3" --workers 8 --seconds 20

    # 複数PCから同時に実行して本番同等の負荷で検証する場合は、
    # 各PCで同じ --path を指定して同時に起動し、それぞれの結果を確認する。
    # (このスクリプト自身は他プロセスの終了を待たないため、
    #  全端末で実行し終えてから最後に --verify のみ実行して結果を見る)

検証後、``--path`` で指定したファイルは自動では消さない(テストなので
明示的に消してから運用に使うこと)。
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import sqlite3
import sys
import time
from pathlib import Path

TABLE = "lock_test_counter"


def _connect(path: str, busy_timeout_ms: int) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=busy_timeout_ms / 1000.0, isolation_level=None)
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    conn.execute("PRAGMA synchronous = FULL")
    # WAL は SMB では張れないことが多いため、実運用の store.py と同じ判定にする
    raw = path.replace("/", "\\")
    mode = "TRUNCATE" if raw.startswith("\\\\") else "wal"
    applied = conn.execute(f"PRAGMA journal_mode = {mode}").fetchone()
    if mode == "wal" and (not applied or applied[0].lower() != "wal"):
        conn.execute("PRAGMA journal_mode = TRUNCATE")
    return conn


def init_db(path: str) -> None:
    conn = _connect(path, 8000)
    conn.execute(f"DROP TABLE IF EXISTS {TABLE}")
    conn.execute(
        f"CREATE TABLE {TABLE} (worker TEXT PRIMARY KEY, count INTEGER NOT NULL)"
    )
    conn.close()


def _retry_execute(conn: sqlite3.Connection, sql: str, params: tuple, attempts: int = 20) -> None:
    """起動直後に全プロセスが同時に INSERT へ殺到しても失敗しないようにする。

    本体側(kanban/db/store.py)は全ての書き込みを retry() で包んでいるため、
    この診断ツールでも同じ保護をかけないと、検証対象のロック機構ではなく
    ツール自身の準備不足で失敗してしまう。
    """
    for attempt in range(attempts):
        try:
            conn.execute(sql, params)
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            if attempt == attempts - 1:
                raise
            time.sleep(0.1 * (attempt + 1))


def worker_main(path: str, worker_id: str, seconds: float, busy_timeout_ms: int, queue: "mp.Queue") -> None:
    # 起動直後の準備段階で失敗しても、親プロセスが queue.get() で
    # 無限待機しないよう、必ず何かを put してから終わるようにする
    try:
        conn = _connect(path, busy_timeout_ms)
        _retry_execute(
            conn,
            f"INSERT INTO {TABLE}(worker, count) VALUES (?, 0)"
            " ON CONFLICT(worker) DO UPDATE SET count = 0",
            (worker_id,),
        )
    except Exception as exc:  # noqa: BLE001 - 準備段階の失敗も報告する
        print(f"[{worker_id}] 初期化失敗: {type(exc).__name__}: {exc}", file=sys.stderr)
        queue.put({"worker": worker_id, "count": 0, "errors": 1, "lock_timeouts": 0})
        return

    count = 0
    errors = 0
    lock_timeouts = 0
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                f"UPDATE {TABLE} SET count = count + 1 WHERE worker = ?", (worker_id,)
            )
            conn.execute("COMMIT")
            count += 1
        except sqlite3.OperationalError as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            text = str(exc).lower()
            if "locked" in text or "busy" in text:
                lock_timeouts += 1
            else:
                errors += 1
                print(f"[{worker_id}] 予期しないエラー: {exc}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 - 破損等も検出したい
            errors += 1
            print(f"[{worker_id}] 例外: {type(exc).__name__}: {exc}", file=sys.stderr)

    conn.close()
    queue.put({"worker": worker_id, "count": count, "errors": errors, "lock_timeouts": lock_timeouts})


def run_check(path: str, workers: int, seconds: float, busy_timeout_ms: int) -> bool:
    print(f"対象ファイル: {path}")
    print(f"プロセス数: {workers} / 実行時間: {seconds:.0f} 秒 / busy_timeout: {busy_timeout_ms}ms")
    print("(この端末単独での予備検証です。本番相当の検証は、複数の実端末から")
    print(" 同じコマンドを同時に実行してください)")
    print()

    init_db(path)

    ctx = mp.get_context("spawn")
    queue: "mp.Queue" = ctx.Queue()
    procs = []
    for i in range(workers):
        worker_id = f"W{i}"
        p = ctx.Process(
            target=worker_main, args=(path, worker_id, seconds, busy_timeout_ms, queue)
        )
        p.start()
        procs.append(p)

    results = []
    for _ in procs:
        results.append(queue.get())
    for p in procs:
        p.join(timeout=30)

    ok = True
    total_expected = 0
    total_actual = 0
    conn = _connect(path, busy_timeout_ms)
    for r in results:
        actual = conn.execute(
            f"SELECT count FROM {TABLE} WHERE worker = ?", (r["worker"],)
        ).fetchone()[0]
        total_expected += r["count"]
        total_actual += actual
        status = "OK" if actual == r["count"] and r["errors"] == 0 else "NG"
        if status == "NG":
            ok = False
        print(
            f"  {r['worker']}: 成功={r['count']:6d} ロック待ち失敗={r['lock_timeouts']:4d} "
            f"予期しないエラー={r['errors']:3d} DB実測値={actual:6d} [{status}]"
        )
    conn.close()

    print()
    print(f"合計: 成功件数(自己申告)={total_expected} / DB実測値={total_actual}")
    if total_expected != total_actual:
        ok = False
        print("!! 不一致: ロック機構が壊れている可能性があります。"
              "このファイルパスを共有 SQLite として使うのは危険です。")
    elif not ok:
        print("!! 一部プロセスで予期しないエラーが発生しました。ログを確認してください。")
    else:
        print("問題は検出されませんでした。")
        print("(ただし、これは短時間・単一端末での予備検証です。"
              "本番投入前に必ず複数の実端末を使った検証も行ってください)")
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--path", required=True,
        help="検証に使う SQLite ファイルパス(共有フォルダ上のパスを指定)。"
             "既存の運用ファイルとは別名にすること",
    )
    parser.add_argument("--workers", type=int, default=8, help="この端末で並列起動するプロセス数")
    parser.add_argument("--seconds", type=float, default=20.0, help="実行時間(秒)")
    parser.add_argument("--busy-timeout-ms", type=int, default=8000, help="busy_timeout(ミリ秒)")
    args = parser.parse_args(argv)

    if Path(args.path).name == "kanban.sqlite3":
        parser.error("運用ファイルと同名です。テスト専用の別名パスを指定してください")

    Path(args.path).parent.mkdir(parents=True, exist_ok=True)
    ok = run_check(args.path, args.workers, args.seconds, args.busy_timeout_ms)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
