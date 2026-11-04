"""日報複合ツールの入口(大きなタブの画面・大設定)── デスクトップ版の入口。**ポートを使わない**

外枠(`日報複合ツール.exe` = Rust/Tauri、`src-tauri/`)がこのプロセスを子として起動し、
`portal://localhost/`(Windows は `http://portal.localhost/`)で受けた要求を**標準入出力**で
渡す。ソケットは1つも開かない。各ツールも同じ形で、ツールごとに別の Python が動く
(`tools/<名前>/bridge.py`)。

    外枠(Rust) ──標準入力──▶ bridge.py ──▶ 入口の Flask アプリ(portal/web.py)
               ◀──標準出力──

【知らせ】
    started  … 受け付けを始めた
    quit     … 終了してよい
    remote_stop … 管理者以外停止: 確認なしで全部を閉じて(各ツールに強制で終わってもらう)
    fatal    … 起動できない(`message` `hint` `log_dir`)。外枠が理由を画面に出す
"""
from __future__ import annotations

import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

APP_ROOT = Path(__file__).resolve().parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

# 一式のフォルダに `__pycache__` を書かない(ブラウザ版の入口と同じ。置き場所は手元の領域の pycache)
_WROTE_BYTECODE = sys.dont_write_bytecode
sys.dont_write_bytecode = True
from portal import app_config as _app_config  # noqa: E402

_app_config.bytecode_to_local()
if sys.pycache_prefix:
    sys.dont_write_bytecode = _WROTE_BYTECODE

from portal import bridge_proto  # noqa: E402

WORKERS = 6
#: 管理者以外停止で外枠に「確認なしで閉じて」と頼んでから、閉じなければ頼み直すまで(秒)
REMOTE_STOP_FALLBACK_SEC = 30.0
TOKEN_ENV = "ALLTOOLS_TOKEN"
MIN_PYTHON = (3, 9)


def main(argv: Optional[list[str]] = None) -> int:
    proto = bridge_proto.protect_stdout()
    writer = bridge_proto.FrameWriter(proto)
    reader = sys.stdin.buffer

    def fatal(message: str, hint: str) -> int:
        try:
            from portal.logging_utils import log_dir
            where = str(log_dir())
        except Exception:                         # noqa: BLE001 - 失敗の報告で失敗しない
            where = ""
        writer.event("fatal", message=message, hint=hint, log_dir=where)
        return 1

    if sys.version_info < MIN_PYTHON:
        return fatal(f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 以上が必要です(いまは {sys.version.split()[0]})",
                     "https://www.python.org/downloads/ から新しい Python を入れてください。")
    import importlib.util

    if importlib.util.find_spec("flask") is None:
        return fatal("必要なパッケージが入っていません: Flask",
                     "コマンドプロンプトで次を実行してください:\n    python -m pip install -r requirements.txt")
    try:
        # **受け付けを始める前に、この(主)スレッドで読み込み終えておく**
        # (2つのスレッドが werkzeug を同時に読み始めると、読みかけを掴んで落ちる)
        import werkzeug.datastructures  # noqa: F401
        import werkzeug.test  # noqa: F401

        from portal import app_config, web
        from portal.logging_utils import get_logger
        from portal.rights_store import store

        app_config.ensure_local_dirs()
        log = get_logger("bridge")
        # 配布設定(置き場所・管理者パスワード)を、共有の DB を読みに行く前に
        from portal import distribution, stop_mode
        distribution.apply_on_start()
        # 管理者以外停止モード中(更新中)なら起動しない。理由は外枠が窓に出す(お願いを出した PC は起動する)
        blocked = stop_mode.startup_block()
        if blocked:
            head, _, rest = blocked.partition("\n")
            return fatal(head, rest.strip())
        app = web.create_app(token=os.environ.get(TOKEN_ENV, ""), bridge=True)
    except Exception as exc:                      # noqa: BLE001 - 理由を外枠へ渡す
        return fatal(f"入口を組み立てられませんでした: {exc}", "ログを確認してください。")

    stopping = {"flag": False}

    def stop() -> None:
        if stopping["flag"]:
            return
        stopping["flag"] = True
        try:
            writer.event("quit")
        except (BrokenPipeError, OSError):
            pass

    web.set_shutdown_hook(stop)

    def close_for_remote_stop() -> None:
        """管理者以外停止: 外枠に「確認なしで閉じて」と頼む(各ツールに強制で終わってもらい、窓を閉じる)。

        外枠が古くてこの知らせを知らない・閉じないときは、少し待ってから「終了してよい」で頼み直す
        (ふだんの終わり方。途中の処理があれば外枠が確認を出す)。
        """
        try:
            writer.event("remote_stop")
        except (BrokenPipeError, OSError):
            return
        retry = threading.Timer(REMOTE_STOP_FALLBACK_SEC, stop)
        retry.daemon = True
        retry.start()

    # 管理者以外停止のお願いを見張る(各ツールが止めてよいと答える・期限で、まとめて閉じる)
    stop_mode.install(close_for_remote_stop, desktop=True)
    # タブ表示権限を裏で読み直しておく(画面を待たせない)
    store.sync_in_background()
    pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="portal")

    def answer(head: dict, body: bytes) -> None:
        close: Callable[[], None] = lambda: None
        try:
            status, headers, data, close = bridge_proto.call_wsgi(app, head, body)
        except Exception as exc:                  # noqa: BLE001 - 1件の失敗で止めない
            log.exception("要求を処理できませんでした: %s", head.get("path"))
            status, headers = 500, [["Content-Type", "text/plain; charset=utf-8"]]
            data = f"内部エラー: {exc}".encode("utf-8")
        try:
            writer.write({"id": head.get("id"), "status": status, "headers": headers}, data)
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                close()
            except Exception:                     # noqa: BLE001
                pass

    log.info("外枠からの要求を受け付けます(ポートは使いません): 日報複合ツール %s", app_config.version())
    writer.event("started")
    try:
        while True:
            try:
                frame = bridge_proto.read_frame(reader)
            except bridge_proto.FrameTooLarge as exc:
                # その1件だけ断って続ける(入口ごと終わらない。v1.5.36)
                log.warning("要求が大きすぎるので断りました: %s %s", exc.head.get("path"), exc)
                status, headers, data = bridge_proto.too_large_reply(exc)
                try:
                    writer.write({"id": exc.head.get("id"), "status": status, "headers": headers}, data)
                except (BrokenPipeError, OSError):
                    break
                continue
            except (ValueError, EOFError) as exc:
                log.error("要求を読めませんでした: %s", exc)
                break
            if frame is None:
                log.info("外枠が閉じました")
                break
            pool.submit(answer, *frame)
    finally:
        pool.shutdown(wait=True, cancel_futures=False)
        stop_mode.leave()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
