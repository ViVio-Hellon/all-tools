"""Excel の操作（Python → cscript.exe → VBScript → Excel COM）。

Python 側に pywin32 等を入れられないため、Excel COM は VBScript
（app/vbscript/excel_worker.vbs）から操作する。Python と VBScript は
標準入出力の「1行コマンド」でやり取りする。

    Python → VBScript（標準入力・最初に）
    SET <key> <hex> …  ENDJOB        … 処理の指示(file.1 / copies など)

    VBScript → Python（標準出力・ASCII）      Python → VBScript（標準入力）
    READY <mode>
    EXCEL <hwnd>        … Excel の PID を記録
    COPIED 1            → OK / EXPORT       … プレビュー画像をコピーした
    EXPORTED 1 / EXPORT_FAILED 1
    NEXT <i>            → GO / STOP         … 印刷: 次の1件へ進むか
    BEGIN <i> / END <i> OK|NG
    FINISH <rc>
    JOB <n>             … 読めたジョブの項目数(0 = ジョブファイルを読めなかった)
    RESULT <key> <hex>  … 結果(item.1.code など)
    PNGDATA <base64>    … グラフ経由で書き出したプレビュー画像(クリップボードが使えないとき)

<hex> は UTF-16 の1単位を4桁の16進にしたもの。日本語を含む値(ファイルパス・
エラー内容)もこれで送るので、標準入出力は ASCII だけで済む(文字化けしない)。

**ファイルは一切受け渡さない。** Microsoft Store 版の Python は %LOCALAPPDATA% や
%TEMP% への書き込みを自分専用の控えへ振り替えるため、Python が書いたファイルが
cscript / Excel からは「パスが見つかりません」(0x4C)になる。ラインPCで実際に
起きた(VER1.1.0 まではジョブファイル・結果ファイルを使っていた)。

Excel プロセス管理:
    * 1回の処理 = Excel 起動 → Workbook を開く → 処理 → Close → Excel 終了
    * Application.Hwnd から Excel の PID と作成時刻を記録し、終了しなければ
      その PID だけを強制終了する（利用者が開いている Excel には触れない）
    * タイムアウト・異常時も必ず後処理する
"""
from __future__ import annotations

import base64
import hashlib
import binascii
import os
import queue
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from app.services import clipboard_image
from core import event_log
from core.process_tracking import (ProcessIdentity, TrackedProcessRegistry, get_process_identity,
                                   pid_from_hwnd, terminate_verified, wait_for_exit)

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER_VBS = os.path.join(APP_DIR, "vbscript", "excel_worker.vbs")
IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000
DUMMY_PASSWORD = "__isp_no_password__"

# 利用者に表示するメッセージ（要件定義書 16. エラー処理）
ERROR_MESSAGES: Dict[str, str] = {
    "FILE_NOT_FOUND": "対象の点検表ファイルが見つかりません。",
    "OPEN_FAILED": "点検表を開けませんでした。",
    "EXCEL_BUSY": "Excelが使用中です。しばらく待ってから再度お試しください。",
    "EXCEL_CREATE_FAILED": "Excelを起動できませんでした。Excelがインストールされているか確認してください。",
    "NO_VISIBLE_SHEET": "表示できるシートがありません。",
    "RANGE_FAILED": "印刷範囲の取得に失敗しました。",
    "COPY_FAILED": "プレビュー画像を取得できませんでした。",
    "PREVIEW_FAILED": "プレビュー画像を取得できませんでした。",
    "PRINT_FAILED": "印刷に失敗しました。",
    "TIMEOUT": "Excelが応答しませんでした。パスワード付きのブックや、確認メッセージが表示される"
               "ブックの可能性があります。",
    "WORKER_FAILED": "Excel処理プログラム（VBScript）の実行に失敗しました。",
    "JOB_READ_FAILED": "Excel処理プログラム（VBScript）が処理の指示を読み込めませんでした。",
    "STOPPED": "中止したため印刷していません。",
    "UNSUPPORTED": "この環境ではExcelを操作できません（Windows専用の機能です）。",
    "CLIPBOARD_BUSY": "ほかのアプリがクリップボードを使っていたため、プレビューを作れませんでした。"
                      "もう一度プレビューしてください。",
    "SUPERSEDED": "次の要求に切り替えたため、この要求は取りやめました。",
    "APP_UPDATED": "アプリが新しい版に入れ替えられました。いったん「終了」して、Start.vbs から起動し直してください。",
}


def message_for(code: str) -> str:
    return ERROR_MESSAGES.get(code, "Excelの処理でエラーが発生しました。")


class ExcelError(Exception):
    def __init__(self, code: str, detail: str = "", message: Optional[str] = None):
        self.code = code
        self.detail = detail
        self.message = message or message_for(code)
        super().__init__(f"{code}: {self.message} {detail}".strip())


@dataclass
class PreviewImage:
    data: bytes
    mime: str
    sheet: str = ""
    range_address: str = ""
    range_source: str = ""
    method: str = ""
    width: int = 0
    height: int = 0


@dataclass
class ItemOutcome:
    index: int
    ok: bool = False
    code: str = ""
    message: str = ""
    detail: str = ""
    sheet: str = ""
    processed: bool = False


@dataclass
class JobOutcome:
    results: Dict[str, str]
    logs: List[str] = field(default_factory=list)
    finish_code: Optional[int] = None
    timed_out: bool = False
    stderr: str = ""
    elapsed_ms: int = 0

    def summary(self) -> str:
        """画面とログに出す「なぜ失敗したか」の手がかり(細部が何も無いとき用)。"""
        parts = []
        fatal = self.results.get("fatal.message") or self.results.get("fatal.code")
        if fatal:
            parts.append(fatal)
        if self.finish_code is not None:
            parts.append(f"VBScript 終了コード {self.finish_code}")
        if self.logs:
            parts.append(self.logs[-1])
        if self.stderr:
            parts.append(self.stderr[:300])
        return " / ".join(parts)


def decode_hex_utf16(text: str) -> str:  # noqa: D401
    """VBScript の HexW で送られた値を戻す(4桁 = UTF-16 の1単位)。"""
    try:
        return bytes.fromhex(text).decode("utf-16-be", errors="replace")
    except ValueError:
        return text


# ======================================================================
# ジョブファイル・結果ファイル
# ======================================================================
def encode_hex_utf16(value: Any) -> str:
    """excel_worker.vbs の UnHexW で戻せる形(UTF-16 の1単位を4桁の16進)。

    改行は値に入れない(1行1項目のため)。
    """
    text = "" if value is None else str(value)
    text = text.replace("\r", " ").replace("\n", " ")
    data = text.encode("utf-16-be")
    return data.hex().upper()


# ======================================================================
# cscript プロセス
# ======================================================================
class _WorkerProcess:
    def __init__(self, cmd: List[str], cwd: str):
        self.cmd = cmd
        self.cwd = cwd
        self.proc: Optional[subprocess.Popen] = None
        self._lines: "queue.Queue[Optional[str]]" = queue.Queue()
        self._stderr: List[str] = []

    @property
    def pid(self) -> Optional[int]:
        return self.proc.pid if self.proc else None

    def start(self) -> None:
        flags = CREATE_NO_WINDOW if IS_WINDOWS else 0
        self.proc = subprocess.Popen(self.cmd, cwd=self.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, creationflags=flags)
        threading.Thread(target=self._pump_stdout, name="vbs-stdout", daemon=True).start()
        threading.Thread(target=self._pump_stderr, name="vbs-stderr", daemon=True).start()

    def _pump_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            for raw in iter(self.proc.stdout.readline, b""):
                line = raw.decode("ascii", errors="replace").strip()
                if line:
                    self._lines.put(line)
        except (OSError, ValueError):
            pass
        finally:
            self._lines.put(None)
            _close_quietly(self.proc.stdout)

    def _pump_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        try:
            data = self.proc.stderr.read()
        except (OSError, ValueError):
            data = b""
        finally:
            _close_quietly(self.proc.stderr)
        for encoding in ("oem", "mbcs", "utf-8"):
            try:
                self._stderr.append(data.decode(encoding, errors="replace"))
                break
            except LookupError:
                continue

    def next_line(self, timeout: float) -> Optional[str]:
        """次の1行。None は終了（EOF）。timeout 秒応答が無ければ TimeoutError。"""
        try:
            return self._lines.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError()

    def send(self, text: str) -> None:
        if self.proc and self.proc.stdin:
            try:
                self.proc.stdin.write((text + "\r\n").encode("ascii"))
                self.proc.stdin.flush()
            except (OSError, ValueError):
                pass

    def close_stdin(self) -> None:
        if self.proc and self.proc.stdin:
            try:
                self.proc.stdin.close()
            except (OSError, ValueError):
                pass

    def wait(self, timeout: float) -> Optional[int]:
        if not self.proc:
            return None
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.kill()
            except OSError:
                pass

    @property
    def stderr_text(self) -> str:
        return "".join(self._stderr).strip()


class _Session:
    """使い回している cscript(excel_worker.vbs)と、その中の Excel 1つ。"""

    def __init__(self, worker: _WorkerProcess, cscript: Optional[ProcessIdentity]):
        self.worker = worker
        self.cscript = cscript
        self.excel: Optional[ProcessIdentity] = None
        self.jobs = 0
        self.printer = ""


# ======================================================================
# VBScript 経由の実装（Windows）
# ======================================================================
class VbsExcelBackend:
    name = "vbscript"

    def __init__(self, cfg: Any, work_dir: str, registry: Any, logger: Any):
        self.cfg = cfg
        self.excel_cfg = cfg.excel
        self.work_dir = work_dir
        self.registry = registry
        self.log = logger
        os.makedirs(work_dir, exist_ok=True)
        self.keep_alive_sec = int(getattr(self.excel_cfg, "keep_alive_sec", 120) or 0)
        # 既定のプリンター(Excel は起動したときのプリンターへ印刷する)
        from app.services.system_info import default_printer
        self.printer_provider = default_printer
        # 起動したときの excel_worker.vbs。共有フォルダのアプリが途中で入れ替えられたら
        # 古い Python と新しい VBScript を組み合わせない
        self._worker_digest = _file_digest(WORKER_VBS)
        self._session: Optional[_Session] = None
        self._session_lock = threading.RLock()
        self._idle_timer: Optional[threading.Timer] = None
        self.cscript = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cscript.exe")
        if not os.path.isfile(self.cscript):
            self.cscript = shutil.which("cscript") or self.cscript

    # ---- 共通設定 -------------------------------------------------
    def _base_params(self) -> Dict[str, Any]:
        e = self.excel_cfg
        return {
            "disable_macros": 1 if e.disable_macros else 0,
            "open_password": DUMMY_PASSWORD if e.block_password_prompt else "",
        }

    # ---- プレビュー -----------------------------------------------
    def preview(self, path: str) -> PreviewImage:
        e = self.excel_cfg
        params = {
            **self._base_params(),
            "count": 1,
            "file.1": path,
            "appearance": 2 if e.preview_appearance == "printer" else 1,
            "max_rows": e.preview_max_rows,
            "max_cols": e.preview_max_cols,
        }
        scale = e.preview_scale
        max_pixels = e.preview_max_pixels
        seq_before = clipboard_image.sequence_number()
        state: Dict[str, Any] = {"png": None, "info": {}, "clip_error": "", "exported": None,
                                 "chunks": [], "recopies": 0, "owner_error": False}

        def handler(cmd: str, args: List[str], worker: _WorkerProcess) -> None:
            if cmd == "COPIED":
                excel = self._session.excel if self._session else None
                try:
                    png, info = clipboard_image.read_image_png(
                        scale, max_pixels, previous_sequence=seq_before,
                        expected_owner=excel.pid if excel else None)
                    state.update(png=png, info=info)
                    worker.send("OK")
                except clipboard_image.ClipboardOwnerError as exc:
                    # 別のものを点検表として出さない。コピーし直させる(グラフ経由も
                    # クリップボードを使うので、そちらへは逃がさない)
                    self.log.warning("プレビュー: %s", exc)
                    if state["recopies"] < 2:
                        state["recopies"] += 1
                        worker.send("RECOPY")
                    else:
                        state.update(clip_error=str(exc), owner_error=True)
                        worker.send("SKIP")
                except clipboard_image.ClipboardImageError as exc:
                    state["clip_error"] = str(exc)
                    self.log.warning("クリップボードから画像を取得できないため、Excel のグラフ機能で"
                                     "画像を書き出します: %s", exc)
                    worker.send("EXPORT")
            elif cmd == "PNGDATA" and args:
                state["chunks"].append(args[0])
            elif cmd == "EXPORTED":
                state["exported"] = True
            elif cmd == "EXPORT_FAILED":
                state["exported"] = False

        outcome = self._run_job("preview", params, handler, e.preview_timeout_sec)
        res = outcome.results
        meta = dict(sheet=res.get("item.1.sheet", ""), range_address=res.get("item.1.range", ""),
                    range_source=res.get("item.1.range_source", ""))
        if state["png"]:
            # 画像取得後に Excel の終了で止まった場合も、画像は使える（Excel は強制終了済み）
            info = state["info"]
            return PreviewImage(state["png"], "image/png", method=f"clipboard-{info.get('format')}",
                                width=info.get("width", 0), height=info.get("height", 0), **meta)
        if state["exported"] and state["chunks"]:
            try:
                data = base64.b64decode("".join(state["chunks"]))
            except (ValueError, binascii.Error) as exc:
                raise ExcelError("PREVIEW_FAILED", f"書き出した画像を読めませんでした: {exc}")
            return PreviewImage(data, "image/png", method="chart-export", **meta)
        if outcome.timed_out:
            raise ExcelError("TIMEOUT", self._detail(res, 1))
        if state["owner_error"]:
            raise ExcelError("CLIPBOARD_BUSY", state["clip_error"])
        if res.get("fatal.code") == "JOB_READ_FAILED":
            # 指示が届いていなければ、ほかの断り(「ファイルが見つかりません」)は結果にすぎない
            raise ExcelError("JOB_READ_FAILED", outcome.summary())
        code = res.get("item.1.code") or res.get("fatal.code") or ("WORKER_FAILED" if outcome.finish_code is None
                                                                    else "PREVIEW_FAILED")
        detail = self._detail(res, 1) or state["clip_error"] or outcome.summary()
        raise ExcelError(code, detail)

    # ---- 印刷 -----------------------------------------------------
    def print_files(self, files: List[str], copies: int, on_event: Callable[..., None],
                    should_continue: Callable[[], bool]) -> List[ItemOutcome]:
        params: Dict[str, Any] = {**self._base_params(), "count": len(files), "copies": copies}
        for index, path in enumerate(files, 1):
            params[f"file.{index}"] = path
        outcomes = {i: ItemOutcome(i) for i in range(1, len(files) + 1)}
        current = {"index": 0, "stop_sent": False}

        def handler(cmd: str, args: List[str], worker: _WorkerProcess) -> None:
            index = _int(args[0]) if args else 0
            if cmd == "NEXT":
                go = should_continue()
                current["stop_sent"] = current["stop_sent"] or not go
                worker.send("GO" if go else "STOP")
            elif cmd == "BEGIN" and index in outcomes:
                current["index"] = index
                on_event("begin", index)
            elif cmd == "END" and index in outcomes:
                outcomes[index].processed = True
                outcomes[index].ok = len(args) > 1 and args[1].upper() == "OK"
                on_event("end", index, outcomes[index].ok)

        outcome = self._run_job("print", params, handler, self.excel_cfg.print_item_timeout_sec)
        res = outcome.results
        for index, item in outcomes.items():
            item.sheet = res.get(f"item.{index}.sheet", "")
            if item.ok:
                continue
            if not item.processed:
                if outcome.timed_out and index == current["index"]:
                    item.code = "TIMEOUT"
                elif res.get("fatal.code") and not any(o.processed for o in outcomes.values()):
                    item.code = res["fatal.code"]
                elif current["stop_sent"] or outcome.timed_out:
                    # **中止と言えるのは、こちらが止めたときだけ。** 以前は処理されなかった
                    # ものを全部「中止したため」にしていて、原因が画面から消えていた
                    item.code = "STOPPED"
                else:
                    item.code = "WORKER_FAILED"
                item.detail = self._detail(res, index) or (
                    "" if item.code == "STOPPED" else outcome.summary())
            else:
                item.code = res.get(f"item.{index}.code") or "PRINT_FAILED"
                item.detail = self._detail(res, index)
            item.message = message_for(item.code)
        return [outcomes[i] for i in sorted(outcomes)]

    # ---- 動作確認 -------------------------------------------------
    def selftest(self) -> Dict[str, Any]:
        started = time.perf_counter()
        outcome = self._run_job("selftest", self._base_params(), lambda *a: None, 60)
        res = outcome.results
        ok = outcome.finish_code == 0 and not outcome.timed_out
        return {"ok": ok, "version": res.get("excel.version", ""),
                "code": res.get("fatal.code", "") if not ok else "",
                "detail": res.get("fatal.message", "") or outcome.stderr,
                "seconds": round(time.perf_counter() - started, 1)}

    # ---- ジョブ実行 -----------------------------------------------
    # **Excel は続けて使い回す。** Excel の起動と終了はそれぞれ数秒かかるので、
    # 1回ごとに起動していたころはプレビュー1枚・印刷1回のたびに待たされていた
    # (ラインPCで「起動も印刷もやや重い」)。処理が終わっても `keep_alive_sec`
    # のあいだは残し、次の処理はすぐに始める。使われなければ閉じる。
    def _command(self) -> List[str]:
        return [self.cscript, "//nologo", "//E:vbscript", WORKER_VBS, "session"]

    def _open_session(self) -> "_Session":
        if _file_digest(WORKER_VBS) != self._worker_digest:
            raise ExcelError("APP_UPDATED", f"{WORKER_VBS} が起動後に変わりました")
        # 作業フォルダも AppData を避ける(Store 版 Python が作ったフォルダは cscript から見えない)
        worker = _WorkerProcess(self._command(), os.path.dirname(WORKER_VBS))
        try:
            worker.start()
        except OSError as exc:
            raise ExcelError("WORKER_FAILED", f"cscript.exe を起動できません: {exc}")
        session = _Session(worker, get_process_identity(worker.pid or 0))
        session.printer = self._current_printer()
        if session.cscript:
            self.registry.add(session.cscript, "cscript:session")
        self.log.info("Excel処理プログラムを起動しました pid=%s", worker.pid)
        return session

    def _run_job(self, mode: str, params: Dict[str, Any],
                 handler: Callable[[str, List[str], _WorkerProcess], None], idle_timeout: int) -> JobOutcome:
        if not os.path.isfile(WORKER_VBS):
            raise ExcelError("WORKER_FAILED", f"{WORKER_VBS} が見つかりません")
        with self._session_lock:
            self._cancel_idle_timer()
            return self._run_job_locked(mode, params, handler, idle_timeout)

    def _run_job_locked(self, mode: str, params: Dict[str, Any],
                        handler: Callable[[str, List[str], _WorkerProcess], None],
                        idle_timeout: int) -> JobOutcome:
        started = time.perf_counter()
        job = {"mode": mode, **params}
        session = self._session
        if session is not None and not session.worker.alive():
            self._close_session(session, force=True, reason="処理プログラムが終わっていた")
            session = None
        if session is not None and session.printer != self._current_printer():
            # **Excel は起動したときのプリンターへ印刷する。** 使い回している間に既定の
            # プリンターを替えると(1台で複数ラインを受け持つ端末)、前のラインの
            # プリンターへ出てしまう。替わっていたら Excel を起動し直す
            self._close_session(session, force=False, reason="既定のプリンターが替わった")
            session = None
        if session is None:
            session = self._session = self._open_session()
        reused = session.jobs > 0
        session.jobs += 1
        worker = session.worker
        finish_code: Optional[int] = None
        timed_out = False
        ended = False
        job_keys: Optional[int] = None
        results: Dict[str, str] = {}
        logs: List[str] = []
        self.log.info("Excel処理開始 mode=%s 件数=%s %s", mode, params.get("count", "-"),
                      "(起動済みの Excel を使用)" if reused else "")
        for key, value in job.items():
            worker.send(f"SET {key} {encode_hex_utf16(value)}")
        worker.send("ENDJOB")
        while True:
            try:
                line = worker.next_line(idle_timeout)
            except TimeoutError:
                timed_out = True
                self.log.error("Excelが %s 秒応答しないため処理を中断します mode=%s", idle_timeout, mode)
                break
            if line is None:
                ended = True
                break
            parts = line.split()
            command = parts[0].upper()
            if command == "EXCEL" and len(parts) > 1:
                excel = self._track_excel(parts[1])
                if session.excel and excel and session.excel.pid != excel.pid:
                    self.registry.remove(session.excel.pid)     # 前の Excel は終わっていた
                session.excel = excel or session.excel
            elif command == "FINISH":
                finish_code = _int(parts[1]) if len(parts) > 1 else 0
                break
            elif command == "RESULT" and len(parts) > 1:
                value = decode_hex_utf16(parts[2]) if len(parts) > 2 else ""
                if parts[1] == "log":
                    logs.append(value)
                else:
                    results[parts[1]] = value
            elif command == "JOB":
                job_keys = _int(parts[1]) if len(parts) > 1 else 0
            elif command in ("READY", "OPENED", "BYE"):
                self.log.debug("worker: %s", line)
            else:
                handler(command, parts[1:], worker)

        expected = len(job)
        if job_keys is not None and job_keys < expected and "fatal.code" not in results:
            results["fatal.code"] = "JOB_READ_FAILED"
            results["fatal.message"] = f"処理の指示が {job_keys}/{expected} 項目しか届きませんでした"
        broken = timed_out or ended or finish_code is None or results.get("fatal.code") == "JOB_READ_FAILED"
        stderr = ""
        if broken:
            # 途中で止まった・応答が無い処理プログラムは使い回さない
            self._close_session(session, force=timed_out, reason="処理の途中で止まった")
            stderr = worker.stderr_text
        elif self.keep_alive_sec <= 0:
            self._close_session(session, force=False, reason="使い回さない設定")
        else:
            self._schedule_idle_close()

        for entry in logs:
            self.log.debug("worker log: %s", entry)
        outcome = JobOutcome(results, logs, finish_code, timed_out, stderr,
                             int((time.perf_counter() - started) * 1000))
        if timed_out or finish_code not in (0, None) or stderr or finish_code is None:
            self.log.warning("Excel処理結果 mode=%s finish=%s timeout=%s 結果=%s 指示=%s stderr=%s",
                             mode, finish_code, timed_out, results, job, stderr[:500])
            # なぜなぜ用: 処理プログラム(VBScript)と Excel が何を返したかを丸ごと残す
            event_log.record(
                "excel.job", event_log.NG, code=results.get("fatal.code", "") or
                ("TIMEOUT" if timed_out else "WORKER_FAILED"),
                message=results.get("fatal.message", "") or ("Excel が応答しませんでした" if timed_out
                                                             else "処理プログラムが途中で終わりました"),
                detail=stderr[:2000], mode=mode, finish_code=finish_code, timed_out=timed_out,
                reused_excel=reused, excel_pid=session.excel.pid if session.excel else None,
                printer=session.printer, elapsed_ms=outcome.elapsed_ms,
                results={k: v for k, v in results.items() if not k.startswith("png")},
                worker_log=logs[-30:])
        else:
            self.log.info("Excel処理終了 mode=%s %d ms", mode, outcome.elapsed_ms)
        return outcome

    def _current_printer(self) -> str:
        try:
            return self.printer_provider() or ""
        except Exception:                              # noqa: BLE001 - 取れなければ比べない
            return ""

    def warm_up(self) -> None:
        """Excel を先に起動しておく(点検表を選び始めたとき)。起動済みなら残す時間を延ばすだけ。"""
        with self._session_lock:
            self._cancel_idle_timer()
            if self._session is not None and self._session.worker.alive():
                self._schedule_idle_close()
                return
            self.log.info("Excelを先に起動しておきます(点検表が選ばれたため)")
            self._run_job_locked("selftest", self._base_params(), lambda *a: None, 60)

    # ---- 使い回しの後始末 ------------------------------------------
    def _schedule_idle_close(self) -> None:
        timer = threading.Timer(self.keep_alive_sec, self._idle_close)
        timer.daemon = True
        timer.name = "excel-idle-close"
        self._idle_timer = timer
        timer.start()

    def _cancel_idle_timer(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    def _idle_close(self) -> None:
        # 処理が始まっていれば閉じない(終わった処理がまた予約する)
        if not self._session_lock.acquire(blocking=False):
            return
        try:
            if self._session is not None:
                self._close_session(self._session, force=False,
                                    reason=f"{self.keep_alive_sec}秒 使われなかった")
        finally:
            self._session_lock.release()

    def shutdown(self, reason: str = "アプリの終了") -> None:
        """使い回している Excel を閉じる(アプリの終了・診断のあと)。"""
        if not self._session_lock.acquire(timeout=30):
            self.log.warning("Excel処理が終わらないため、閉じずに終了します")
            return
        try:
            self._cancel_idle_timer()
            if self._session is not None:
                self._close_session(self._session, force=False, reason=reason)
        finally:
            self._session_lock.release()

    def _close_session(self, session: "_Session", *, force: bool, reason: str) -> None:
        """標準入力を閉じる → VBScript が Excel を終了して抜ける。残れば止める。"""
        worker = session.worker
        worker.close_stdin()
        if force:
            worker.kill()
        if worker.wait(5 if force else self.excel_cfg.excel_exit_grace_sec + 5) is None:
            worker.kill()
            worker.wait(5)
        if session.cscript:
            self.registry.remove(session.cscript.pid)
        self._ensure_excel_exit(session.excel, force=force)
        if self._session is session:
            self._session = None
        self.log.info("Excelを閉じました(%s・%d件処理)", reason, session.jobs)
        event_log.record("excel.close", event_log.INFO, reason=reason, jobs=session.jobs, forced=force,
                         excel_pid=session.excel.pid if session.excel else None, printer=session.printer)

    def _track_excel(self, hwnd_text: str) -> Optional[ProcessIdentity]:
        hwnd = _int(hwnd_text)
        pid = pid_from_hwnd(hwnd) if hwnd else None
        identity = get_process_identity(pid) if pid else None
        if identity:
            self.registry.add(identity, "excel")
            self.log.info("Excelを起動しました pid=%s", identity.pid)
            event_log.record("excel.open", event_log.INFO, excel_pid=identity.pid,
                             printer=self._current_printer())
        else:
            self.log.warning("Excel の PID を取得できませんでした hwnd=%s", hwnd_text)
        return identity

    def _ensure_excel_exit(self, excel: Optional[ProcessIdentity], force: bool) -> None:
        """Excel が確実に終了したことを確認する（残っていればその PID だけ強制終了）。"""
        if excel is None:
            return
        grace = 0 if force else self.excel_cfg.excel_exit_grace_sec
        if not wait_for_exit(excel.pid, excel.create_time, grace):
            self.log.warning("Excelプロセスが終了しないため強制終了します pid=%s", excel.pid)
            if not terminate_verified(excel.pid, excel.create_time, ["excel.exe"]):
                self.log.error("Excelプロセスを終了できませんでした pid=%s", excel.pid)
                return
        self.registry.remove(excel.pid)

    @staticmethod
    def _detail(results: Dict[str, str], index: int) -> str:
        parts = [results.get(f"item.{index}.message", ""), results.get(f"item.{index}.errno", "")]
        return " ".join(p for p in parts if p).strip()


# ======================================================================
# 模擬実装（Excel なしで画面や流れを確認する用途）
# ======================================================================
class DummyExcelBackend:
    name = "dummy"

    def __init__(self, print_delay: float = 1.2, preview_delay: float = 0.6):
        self.print_delay = print_delay
        self.preview_delay = preview_delay

    def preview(self, path: str) -> PreviewImage:
        time.sleep(self.preview_delay)
        if not os.path.isfile(path):
            raise ExcelError("FILE_NOT_FOUND", path)
        name = os.path.splitext(os.path.basename(path))[0]
        return PreviewImage(_dummy_svg(name).encode("utf-8"), "image/svg+xml", sheet="Sheet1",
                            range_address="A1:P40", range_source="dummy", method="dummy", width=794, height=1123)

    def print_files(self, files: List[str], copies: int, on_event: Callable[..., None],
                    should_continue: Callable[[], bool]) -> List[ItemOutcome]:
        outcomes: List[ItemOutcome] = []
        for index, path in enumerate(files, 1):
            item = ItemOutcome(index)
            outcomes.append(item)
            if not should_continue():
                item.code, item.message = "STOPPED", message_for("STOPPED")
                continue
            on_event("begin", index)
            time.sleep(self.print_delay)
            base = os.path.basename(path).lower()
            item.processed = True
            if not os.path.isfile(path):
                item.code = "FILE_NOT_FOUND"
            elif "error" in base or "エラー" in base:
                item.code, item.detail = "PRINT_FAILED", "模擬エラー（ファイル名に「エラー」を含むため）"
            else:
                item.ok, item.sheet = True, "Sheet1"
            item.message = "" if item.ok else message_for(item.code)
            on_event("end", index, item.ok)
        return outcomes

    def selftest(self) -> Dict[str, Any]:
        return {"ok": True, "version": "dummy", "code": "", "detail": "模擬モード", "seconds": 0}


def _dummy_svg(title: str) -> str:
    from html import escape
    rows = "".join(
        f'<rect x="60" y="{150 + i * 38}" width="674" height="38" fill="{"#f4f6f8" if i % 2 else "#fff"}" '
        f'stroke="#c9ced6"/><text x="72" y="{175 + i * 38}" font-size="15" fill="#555">点検項目 {i + 1}</text>'
        f'<rect x="560" y="{158 + i * 38}" width="22" height="22" fill="none" stroke="#888"/>'
        f'<rect x="620" y="{158 + i * 38}" width="22" height="22" fill="none" stroke="#888"/>'
        for i in range(22))
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="794" height="1123" viewBox="0 0 794 1123">'
        '<rect width="794" height="1123" fill="#fff"/>'
        f'<text x="397" y="80" text-anchor="middle" font-size="30" font-weight="bold" fill="#222">{escape(title)}</text>'
        '<text x="397" y="115" text-anchor="middle" font-size="15" fill="#c0392b">'
        '模擬プレビュー（Excelを使用していません）</text>'
        f'{rows}'
        '<text x="60" y="1060" font-size="14" fill="#555">点検者：＿＿＿＿＿＿＿＿　　確認者：＿＿＿＿＿＿＿＿</text>'
        '</svg>')


# ======================================================================
# サービス
# ======================================================================
class ExcelService:
    """Excel 処理の窓口。Excel は同時に1つの処理だけ実行する。"""

    # 順番待ちのあいだ「もう要らなくなったか」を見る間隔(秒)
    GIVE_UP_POLL_SEC = 0.2

    def __init__(self, cfg: Any, logger: Any, registry: Optional[TrackedProcessRegistry] = None,
                 work_dir: Optional[str] = None):
        from core import app_config, process_tracking
        self.cfg = cfg
        self.log = logger
        self.registry = registry or process_tracking.registry(logger=logger)
        work_dir = work_dir or str(app_config.local_dir("work"))
        self._lock = threading.Lock()
        self._activity: Optional[str] = None
        backend = cfg.excel.backend
        if backend == "auto":
            backend = "vbscript" if IS_WINDOWS else "dummy"
        if backend == "dummy":
            self.backend: Any = DummyExcelBackend()
        else:
            self.backend = VbsExcelBackend(cfg, work_dir, self.registry, logger)
        self.log.info("Excel連携方式: %s", self.backend.name)

    @property
    def backend_name(self) -> str:
        return self.backend.name

    @property
    def activity(self) -> Optional[str]:
        return self._activity

    @contextmanager
    def acquire(self, activity: str, timeout: float,
                give_up: Optional[Callable[[], bool]] = None) -> Iterator[None]:
        """Excel の順番を取る。空くまで最大 `timeout` 秒待つ。

        `give_up` を渡すと、待っているあいだそれを見て、真になったら待つのを
        やめる(`ExcelError("SUPERSEDED")`)。プレビューで次の点検表に移ったのに、
        前の要求が順番を待ち続けて Excel とサーバの手を塞がないようにする。
        """
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if give_up is not None and give_up():
                raise ExcelError("SUPERSEDED")
            remaining = deadline - time.monotonic()
            slice_ = remaining if give_up is None else min(remaining, self.GIVE_UP_POLL_SEC)
            if self._lock.acquire(timeout=max(0.0, slice_)):
                break
            if remaining <= 0 or give_up is None:
                current = {"print": "印刷", "preview": "プレビュー"}.get(self._activity or "", "他の処理")
                raise ExcelError("EXCEL_BUSY", message=f"{current}の処理中です。完了後に再度お試しください。")
        self._activity = activity
        try:
            yield
        finally:
            self._activity = None
            self._lock.release()

    def preview(self, path: str) -> PreviewImage:
        if not os.path.isfile(path):
            raise ExcelError("FILE_NOT_FOUND", path)
        return self.backend.preview(path)

    def print_files(self, files: List[str], copies: int, on_event: Callable[..., None],
                    should_continue: Callable[[], bool]) -> List[ItemOutcome]:
        return self.backend.print_files(files, copies, on_event, should_continue)

    def selftest(self, keep: bool = False) -> Dict[str, Any]:
        """Excel を起動できるか確かめる。

        `keep=True` のときは閉じずに残す(アプリの中で確かめたとき)。
        閉じきるまで PC によって十数秒かかるうえ、すぐ次のプレビューで
        また起動することになる。残した Excel は使われなければ
        `keep_alive_sec` のあとで閉じる。
        """
        try:
            with self.acquire("prepare" if keep else "selftest", 5):
                try:
                    return self.backend.selftest()
                finally:
                    if not keep:
                        self.shutdown("動作確認のあと")
        except ExcelError as exc:
            return {"ok": False, "code": exc.code, "detail": exc.detail or exc.message, "version": ""}

    def warm_up(self) -> bool:
        """裏で Excel を起動しておく。**ほかの処理中なら何もしない**(待たせない)。

        プレビュー・印刷がそのあいだに押されたら、起動が終わるのを待ってから
        同じ Excel で始まる(1から起動するより早い)。
        """
        warm = getattr(self.backend, "warm_up", None)
        if warm is None or self._lock.locked():
            return False

        def run() -> None:
            try:
                with self.acquire("prepare", timeout=0):
                    warm()
            except ExcelError:
                pass                                  # ほかの処理が先に始まった
            except Exception:                         # noqa: BLE001 - 先回りの失敗で画面を止めない
                self.log.exception("Excelの先回り起動に失敗しました")

        threading.Thread(target=run, name="excel-warm-up", daemon=True).start()
        return True

    def shutdown(self, reason: str = "アプリの終了") -> None:
        """使い回している Excel を閉じる。"""
        close = getattr(self.backend, "shutdown", None)
        if close is not None:
            close(reason)

    def cleanup_leftovers(self, reason: str) -> int:
        """前回の異常終了などで残った Excel / cscript を片付ける（記録した PID のみ）。"""
        cleaned = self.registry.cleanup(reason, ["excel.exe", "cscript.exe"] if IS_WINDOWS else None)
        return len(cleaned)


def _file_digest(path: str) -> str:
    try:
        with open(path, "rb") as fp:
            return hashlib.sha1(fp.read()).hexdigest()
    except OSError:
        return ""


def _int(text: str) -> int:
    try:
        return int(text)
    except (TypeError, ValueError):
        return 0


def _close_quietly(stream: Any) -> None:
    try:
        if stream is not None:
            stream.close()
    except (OSError, ValueError):
        pass

