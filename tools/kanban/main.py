#!/usr/bin/env python3
"""看板発注システム(Python 版)のバッチ実行口。

**画面を開くのはこちらではありません。** 利用者は ``Start.vbs``(通常)か
``start.bat``(診断)を使います。どちらも ``start_app.py`` を呼び、Web サーバ
とブラウザで画面を出します(基盤仕様書 2.1)。

こちらは**画面を使わない実行**のための入口です。定時バッチや調査で使います::

    python main.py --import-only         # 取り込みだけ実行して終了
    python main.py --export-only         # 書き戻しだけ実行して終了
    python main.py --show-config         # 有効な設定を表示

``--import-only`` / ``--export-only`` は Windows のタスクスケジューラから
呼べます。画面を開いていない時間帯にも共有 DB と手元の SQLite を合わせて
おきたい場合に使ってください。

画面を開く場合::

    Start.vbs                            # 通常(コンソールを出さない)
    start.bat                            # 診断(経過とエラーを表示)
    start.bat --mode warehouse           # モードを指定して開く
    python start_app.py --check          # 実行環境の確認だけ
"""

from __future__ import annotations

import argparse
import json
import sys

from kanban import applog, config
from kanban.db.shared import SharedDb
from kanban.db import sync
from kanban.db.store import Store

#: 現場 / 倉庫 / 倉庫参照 はそれぞれ独立した画面(フォーム)で、
#: 1 回の起動ではどれか 1 つだけを開く。
_MODES = config.ALL_MODES


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kanban",
        description="看板発注システム(Access + Excel VBA からの移行版)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--mode",
        choices=_MODES,
        default=None,
        help=(
            "起動する画面をこのプロセスだけ一時的に上書きする"
            "(site=現場 / warehouse=倉庫 / view=倉庫参照。SQLite には保存しない)。"
            "省略時はこの端末の SQLite に保存済みのモード、無ければ選択画面が出る"
        ),
    )
    parser.add_argument("--line", help="担当ラインコード(例: LVC, LS, 大板小板)")
    parser.add_argument("--config", help="設定ファイルのパス")
    parser.add_argument("--shared-db", dest="shared_db", help="共有 SQLite のパス")
    parser.add_argument("--sqlite", help="SQLite ファイルのパス")
    parser.add_argument(
        "--no-import", action="store_true", help="起動時の取り込みを行わない"
    )
    parser.add_argument(
        "--export-interval",
        type=int,
        help="手元 -> 共有DB への書き戻し間隔(秒)。0 で自動書き戻しを止める",
    )
    parser.add_argument(
        "--import-interval",
        type=int,
        help=(
            "共有DB -> 手元 への再取り込み間隔(秒)。0 で自動再取り込みを止める。"
            "他端末の変更をこの端末へ反映する間隔"
        ),
    )
    parser.add_argument(
        "--import-only", action="store_true", help="取り込みだけ実行して終了する"
    )
    parser.add_argument(
        "--export-only", action="store_true", help="書き戻しだけ実行して終了する"
    )
    parser.add_argument("--show-config", action="store_true", help="設定を表示して終了")
    parser.add_argument("--verbose", action="store_true", help="ログを標準エラーにも出す")
    return parser


def resolve_config(args: argparse.Namespace) -> config.Config:
    cfg = config.load_config(args.config)
    if args.shared_db:
        cfg.shared_db_path = args.shared_db
    if args.sqlite:
        cfg.sqlite_path = args.sqlite
    if args.line:
        cfg.line = args.line
    if args.mode:
        cfg.mode = args.mode
    if cfg.mode not in config.ALL_MODES:
        cfg.mode = config.MODE_SITE
    if args.no_import:
        cfg.import_on_startup = False
    if args.export_interval is not None:
        cfg.export_interval_sec = args.export_interval
    if args.import_interval is not None:
        cfg.import_interval_sec = args.import_interval
    return cfg


def _open_store(cfg: config.Config) -> Store:
    store = Store(
        cfg.resolved_sqlite_path(),
        host_name=cfg.host_name,
        busy_timeout_ms=cfg.busy_timeout_ms,
        max_retry=cfg.max_retry,
    )
    store.ensure_schema()
    return store


def run_import(cfg: config.Config) -> int:
    """取り込みだけ実行する(定時バッチ等から呼べる)。"""
    store = _open_store(cfg)
    gateway = SharedDb(
        cfg.resolved_shared_db_path(),
        busy_timeout_ms=cfg.busy_timeout_ms,
        max_retry=cfg.max_retry,
    )
    result = sync.import_all(store, gateway)
    print(result.summary())
    for failed in result.failed_lines:
        print(f"  - {failed.table_name}: {failed.message}")
    store.close()
    return 0 if result.ok_lines else 1


def run_export(cfg: config.Config) -> int:
    """書き戻しだけ実行する。"""
    store = _open_store(cfg)
    gateway = SharedDb(
        cfg.resolved_shared_db_path(),
        busy_timeout_ms=cfg.busy_timeout_ms,
        max_retry=cfg.max_retry,
    )
    result = sync.export_pending(store, gateway)
    if result.skipped_reason:
        print(result.skipped_reason)
    print(f"書き戻し: 対象 {result.attempted} 件 / 成功 {result.succeeded} 件 / 失敗 {result.failed} 件")
    for message in result.errors[:20]:
        print(f"  - {message}")
    store.close()
    return 0 if result.ok else 1


def show_config(cfg: config.Config) -> int:
    data = cfg.to_dict()
    data["(実際に使う共有DB)"] = cfg.resolved_shared_db_path()
    data["(実際に使う sqlite)"] = cfg.resolved_sqlite_path()
    data["(設定ファイル)"] = cfg.source_path
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = resolve_config(args)

    applog.initialize(
        cfg.resolved_log_dir(),
        line_name=cfg.line if cfg.mode == config.MODE_SITE else cfg.mode,
        app_name=config.APP_NAME,
        echo_to_stderr=args.verbose,
    )
    applog.info("起動: mode=%s line=%s", cfg.mode, cfg.line or "(未設定)")

    try:
        if args.show_config:
            return show_config(cfg)
        if args.import_only:
            return run_import(cfg)
        if args.export_only:
            return run_export(cfg)

        # 画面はこちらでは開かない。**入口を1つに絞る**ため、起動は
        # start_app.py(と、それを呼ぶ Start.vbs / start.bat)に任せる
        # (基盤仕様書 2.1 / 2.5)。ここで Flask を読むと、バッチ実行にも
        # Web の依存が要ることになってしまう。
        print(
            "画面を開くには Start.vbs(通常)または start.bat(診断)を使ってください。\n"
            "  Start.vbs                  … コンソールを出さずに起動\n"
            "  start.bat                  … 経過とエラーを表示しながら起動\n"
            "  python start_app.py --check … 実行環境の確認だけ\n"
            "\n"
            "このコマンドは画面を使わない実行(--import-only / --export-only /\n"
            "--show-config)のためのものです。",
            file=sys.stderr,
        )
        return 2
    except KeyboardInterrupt:
        return 130
    finally:
        applog.close()


if __name__ == "__main__":
    sys.exit(main())
