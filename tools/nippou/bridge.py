"""デスクトップ版(Tauri)からの入口 ── **ポートを使わない**

ブラウザ版(`Start.vbs` → `start_app.py`)は、127.0.0.1 のポート(8733〜)で
待ち受けてブラウザから繋いでもらっていた。デスクトップ版では、窓を持つ外枠
(統合ツールの Rust/Tauri、`../../src-tauri/`)がこのプロセスを子として起動し、
**標準入出力**で要求を渡す。ソケットは1つも開かない。作りは python-web-tools・
資材発注看板システム・ライン管理カレンダー・点検表のデスクトップ版と同じ。

    外枠(Rust) ──標準入力──▶ bridge.py ──▶ Flask アプリ(WSGI として呼ぶだけ)
               ◀──標準出力──

Flask は「URL を関数に振り分ける部品」として使い続ける(130 の経路と
試験 3,700 件余りがその形で書かれている)。待ち受け(waitress)は使わない。

【やりとりの形】1件 = 見出し1行(JSON)+ 本文(見出しの `len` バイト)

    要求  {"id": 7, "method": "POST", "path": "/api/x", "query": "a=1",
           "headers": [["Content-Type", "application/json"], ...], "len": 12}\\n<本文>
    応答  {"id": 7, "status": 200, "headers": [[...], ...], "len": 345}\\n<本文>
    知らせ {"event": "quit"}\\n                     (`len` なし。本文なし)

【知らせ】
    started  … 受け付けを始めた(待機画面を出せる)
    quit     … 終了してよい(画面の「終了」・閉じる確認を通った)
    fatal    … 起動できない(`message` `hint` `log_dir`)。外枠が理由を画面に出す

【応答を返してから後片付け ── 日報だけの事情】
日報は、書いた要求の応答を**返し終わってから**共有の控え(LocalBackup)へ写す
(`app/__init__.py` の `call_on_close(local_backup.flush_soon)`)。控えは共有
フォルダにあり遅いことがあるので、写すのを待たせないためです。ほかのツールの
入口は応答を書く前に後片付け(WSGI の `close()`)を呼んでいたので、そのまま
写すと**保存のたびに共有フォルダの写しを待たされる**。ここでは応答を先に
外枠へ書き、そのあとで `close()` を呼ぶ(`call_wsgi` が `close` を返す)。

【標準出力を守る】
やりとりに使う標準出力へ、ほかの誰かが1文字でも書くと、以降すべてずれる。
そこで**最初に**本物の標準出力を別に取っておき、ファイル記述子 1 は標準エラーへ
付け替える(`print` も C 拡張の出力も、cscript の出力も標準エラーへ行く)。
"""
from __future__ import annotations

import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, BinaryIO, Callable, Optional

APP_ROOT = Path(__file__).resolve().parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

# 同時に捌く要求の数(ブラウザ版の waitress と同じ)。画面は「操作 + 進み具合の
# 見張り + 心拍 + タブの合図」を同時に投げる。書く要求はアプリ側の錠が1つずつ
# 通す(`app._WRITE_LOCK`)
WORKERS = 8

# 1件の本文の上限(壊れた見出しで巨大な読み込みをしないため)。
# 取り込み(Excel / CSV)と音のファイルが最大
MAX_BODY = 256 * 1024 * 1024

# 外枠がデスクトップ版の画面を読み込む宛先のホスト名(`app_config.BRIDGE_HOSTS`)
DEFAULT_HOST = "app.localhost"

# 外枠から渡される合言葉(起動ごと)
TOKEN_ENV = "NIPPOU_TOKEN"


# ==================================================================
# やりとりの形
# ==================================================================
def read_frame(stream: BinaryIO) -> Optional[tuple[dict, bytes]]:
    """1件読む。終わり(相手が閉じた)なら `None`。"""
    line = stream.readline()
    if not line:
        return None
    head = json.loads(line.decode("utf-8"))
    size = int(head.get("len", 0) or 0)
    if size < 0 or size > MAX_BODY:
        raise ValueError(f"本文の長さが不正です: {size}")
    chunks = []
    left = size
    while left > 0:
        chunk = stream.read(left)
        if not chunk:
            raise EOFError("本文の途中で終わりました")
        chunks.append(chunk)
        left -= len(chunk)
    return head, b"".join(chunks)


class FrameWriter:
    """応答と知らせを書く。**1件ずつ丸ごと**書く(複数のスレッドから来る)。"""

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def write(self, head: dict, body: bytes = b"") -> None:
        head = dict(head)
        if body or "id" in head:
            head["len"] = len(body)
        data = json.dumps(head, ensure_ascii=False).encode("utf-8") + b"\n" + body
        with self._lock:
            self._stream.write(data)
            self._stream.flush()

    def event(self, name: str, **fields: Any) -> None:
        self.write({"event": name, **fields})


def call_wsgi(app: Callable, head: dict, body: bytes) -> tuple[int, list, bytes, Callable[[], None]]:
    """要求1件を WSGI アプリに渡す。応答と、**あとで呼ぶ後片付け**を返す。

    後片付け(WSGI の `close()`)は、応答を外枠へ書いてから呼ぶこと
    (上の説明。日報は応答のあとで共有の控えへ写す)。
    """
    import io

    from werkzeug.datastructures import Headers
    from werkzeug.test import EnvironBuilder

    headers = Headers([(str(k), str(v)) for k, v in head.get("headers", [])])
    host = headers.get("Host") or DEFAULT_HOST
    builder = EnvironBuilder(
        path=head.get("path") or "/",
        method=(head.get("method") or "GET").upper(),
        query_string=head.get("query") or "",
        headers=headers,
        base_url=f"http://{host}",
        input_stream=io.BytesIO(body),
        content_length=len(body),
    )
    try:
        environ = builder.get_environ()
    finally:
        builder.close()
    environ["REMOTE_ADDR"] = "127.0.0.1"

    captured: dict[str, Any] = {}

    def start_response(status: str, response_headers: list, exc_info=None):
        captured["status"] = int(status.split(" ", 1)[0])
        captured["headers"] = [[k, v] for k, v in response_headers]
        return lambda data: captured.setdefault("written", []).append(data)

    result = app(environ, start_response)
    close = getattr(result, "close", None) or (lambda: None)
    try:
        chunks = list(captured.pop("written", [])) + [c for c in result]
    except Exception:
        close()
        raise
    return (captured.get("status", 500), captured.get("headers", []),
            b"".join(chunks), close)


# ==================================================================
# サーバ(待ち受けの代わり)
# ==================================================================
def _protect_stdout() -> BinaryIO:
    """本物の標準出力を取っておき、記述子 1 は標準エラーへ向ける。"""
    proto = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    sys.stdout.flush()
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    return proto


def make_server_class():
    """`server.AppServer` を受け継ぐ。**段・準備完了・失敗の伝え方は共通**。

    違うのは「どう待ち受けて、どう止めるか」だけ。ここで遅れて読むのは、
    `server` が `nippou` を読むので、標準出力を守ったあとにしたいから。
    """
    from server import AppServer

    class BridgeServer(AppServer):
        def __init__(self, token: str, writer: FrameWriter, reader: BinaryIO) -> None:
            # **受け付けを始める前に、この(主)スレッドで読み込み終えておく。**
            # 1件目の要求は本体(Flask)を組み立てている最中に届く。2つの
            # スレッドが werkzeug を同時に読み始めると、片方が読みかけの
            # モジュールを掴んで「cannot import name ... from partially
            # initialized module」で落ちる(python-web-tools で両方向とも起きた)
            import werkzeug  # noqa: F401
            import werkzeug.datastructures  # noqa: F401
            import werkzeug.test  # noqa: F401
            super().__init__(0, token=token)
            self.writer = writer
            self.reader = reader
            self.url = f"http://{DEFAULT_HOST}/"

        def build(self) -> None:
            # 画面は外枠の窓から来る。Host の確認(DNS リバインディング対策)は
            # TCP の待ち受けのためのものなので、外枠の宛先名を足すだけにする。
            # **引き継ぐ前に**入れる(引き継いだ一瞬に届いた要求を断らないため)。
            # 待機画面が読んでいた段の鍵も引き継ぐ(ブラウザ版は段の名前だけ)
            super().build(BRIDGE=True,
                          STAGE_KEY=getattr(self.boot, "stage_key", "prepare"))

        def serve_forever(self) -> None:
            """標準入力から要求を読み、別スレッドで答える。相手が閉じたら終わる。"""
            from nippou.logging_setup import get_launch_logger
            log = get_launch_logger("bridge", filename="guard.log")
            pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="bridge")

            def answer(head: dict, body: bytes) -> None:
                close: Callable[[], None] = lambda: None
                try:
                    status, headers, data, close = call_wsgi(self.wsgi, head, body)
                except Exception as exc:          # noqa: BLE001 - 1件の失敗で止めない
                    log.exception("要求を処理できませんでした: %s", head.get("path"))
                    status, headers = 500, [["Content-Type", "text/plain; charset=utf-8"]]
                    data = f"内部エラー: {exc}".encode("utf-8")
                path = str(head.get("path") or "")
                if (head.get("method") == "GET"
                        and not path.startswith(("/api/", "/static/", "/sv/", "/sound/"))):
                    # 画面(ページ)を届けた足跡。窓 → 外枠 → ここ → 外枠 → 窓 が1周した印
                    log.info("画面 GET %s → %s", path, status)
                try:
                    self.writer.write({"id": head.get("id"), "status": status,
                                       "headers": headers}, data)
                except (BrokenPipeError, OSError):
                    pass                          # 外枠が先に終わった
                finally:
                    # **応答を書いてから**後片付け(共有の控えへの写しはここで走る)
                    try:
                        close()
                    except Exception:             # noqa: BLE001 - 後片付けの失敗で止めない
                        log.exception("応答の後片付けで失敗しました: %s", path)

            log.info("外枠からの要求を受け付けます(ポートは使いません)")
            self.writer.event("started")
            try:
                while not self._stop_requested:
                    try:
                        frame = read_frame(self.reader)
                    except (ValueError, EOFError) as exc:
                        log.error("要求を読めませんでした: %s", exc)
                        break
                    if frame is None:
                        log.info("外枠が閉じました")
                        break
                    pool.submit(answer, *frame)
            finally:
                pool.shutdown(wait=True, cancel_futures=False)
                self._stop_requested = True
                self._stopped.set()

        def stop(self) -> None:
            """終了してよいことを外枠へ知らせる。外枠が標準入力を閉じて終わる。

            `/api/shutdown`(画面の「終了」・窓の ×)から呼ばれる。
            """
            with self._stop_lock:
                if self._stop_requested:
                    return
                self._stop_requested = True
            try:
                self.writer.event("quit")
            except (BrokenPipeError, OSError):
                pass
            self._stopped.set()

    return BridgeServer


# ==================================================================
# 入口
# ==================================================================
def main(argv: Optional[list[str]] = None) -> int:
    proto = _protect_stdout()
    writer = FrameWriter(proto)
    reader = sys.stdin.buffer

    import start_app

    def fatal(error: "start_app.StartupError") -> int:
        try:
            from nippou import app_config
            log_dir = str(app_config.local_dir("logs"))
        except Exception:                         # noqa: BLE001 - 失敗の報告で失敗しない
            log_dir = ""
        try:
            start_app.log().error("起動に失敗: %s / %s", error, error.hint)
        except Exception:                         # noqa: BLE001
            pass
        writer.event("fatal", message=str(error), hint=error.hint, log_dir=log_dir)
        return 1

    try:
        start_app.run_environment_checks(bridge=True)
        return start_app.start_bridge(
            token=os.environ.get(TOKEN_ENV, ""),
            server_factory=lambda token: make_server_class()(token, writer, reader))
    except start_app.StartupError as exc:
        return fatal(exc)
    except Exception as exc:                      # noqa: BLE001 - 理由を外枠へ渡す
        return fatal(start_app.StartupError(f"起動中に思わぬエラー: {exc}",
                                            "ログを確認してください。"))


if __name__ == "__main__":
    raise SystemExit(main())
