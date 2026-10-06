"""Subprocess execution of generated VBScripts, with the same
retry/backoff policy ``adoSQL`` used around lock conflicts.

Kept dependency-injectable (``executor`` / ``sleep``) so the retry logic
and output parsing can be unit tested on any OS without actually shelling
out to ``cscript.exe``.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..config import SETTINGS
from ..logging_setup import get_logger
from .errors import AccessBridgeError, ErrorKind, classify_error

_logger = get_logger("access_bridge")

Executor = Callable[[str, int], subprocess.CompletedProcess]


@dataclass
class ScriptResult:
    success: bool
    stdout: str = ""
    stderr: str = ""
    return_code: int = -1
    err_number: Optional[int] = None
    err_desc: str = ""
    rows: Optional[int] = None
    attempts: int = 1
    error: Optional[AccessBridgeError] = field(default=None)


def _default_executor(script_path: str, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        [SETTINGS.cscript_path, "//nologo", "//B", script_path],
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )


def _parse_output(stdout: str) -> ScriptResult:
    for raw_line in stdout.splitlines():
        stripped_line = raw_line.strip()
        if stripped_line.startswith("ERRCODE="):
            body = stripped_line[len("ERRCODE="):]
            code_str, _, desc = body.partition("|ERRDESC=")
            try:
                err_number = int(code_str)
            except ValueError:
                err_number = None
            return ScriptResult(success=False, stdout=stdout, err_number=err_number, err_desc=desc)
        if stripped_line.startswith("OK"):
            rows = None
            if ":" in stripped_line:
                _, _, count_str = stripped_line.partition(":")
                try:
                    rows = int(count_str)
                except ValueError:
                    rows = None
            return ScriptResult(success=True, stdout=stdout, rows=rows)
    return ScriptResult(success=False, stdout=stdout, err_desc="スクリプトの出力を解釈できませんでした。")


class ScriptRunner:
    """Runs a generated VBScript once via ``cscript.exe`` and parses its
    single-line stdout contract (see ``script_gen.py``'s module
    docstring)."""

    def __init__(
        self,
        timeout: Optional[int] = None,
        executor: Optional[Executor] = None,
    ) -> None:
        self.timeout = timeout or SETTINGS.subprocess_timeout_sec
        self._executor = executor or _default_executor

    def run(self, script_text: str) -> ScriptResult:
        """1本のVBScriptを実行し結果を返す。

        「他ラインを落とさない」ことを最優先し、この関数からは原則として
        例外を外に投げない -- 権限エラーやディスク容量不足など
        ``FileNotFoundError``/``TimeoutExpired`` 以外の予期しない ``OSError``
        （共有ネットワークドライブが一時的に応答しない場合などWindows環境
        で起こりうる）も含め、すべて失敗を表す :class:`ScriptResult` に
        変換して返す。呼び出し元（自分のライン）のアプリがクラッシュしない
        ことを保証するための最終防衛ラインとして機能する。
        """
        try:
            script_path = self._write_temp_script(script_text)
        except OSError as exc:
            _logger.exception("一時スクリプトファイルの作成に失敗しました")
            return ScriptResult(success=False, err_desc=f"一時ファイルの作成に失敗しました: {exc}")

        try:
            try:
                proc = self._executor(script_path, self.timeout)
            except FileNotFoundError:
                _logger.error("cscript.exe not found; cannot reach Access on this OS")
                return ScriptResult(
                    success=False,
                    err_desc="cscript.exe が見つかりません（ドライバ未導入またはWindows以外の環境です）。",
                )
            except subprocess.TimeoutExpired:
                _logger.error("Access bridge script timed out after %ss", self.timeout)
                return ScriptResult(success=False, err_desc="タイムアウトしました。")
            except OSError as exc:
                # 権限エラー、共有ネットワークドライブの一時切断など、
                # 想定外だが起こりうる OS 起因のエラーはここで拾う。
                _logger.exception("cscript.exe の実行中に予期しないエラーが発生しました")
                return ScriptResult(success=False, err_desc=f"プロセス起動時に予期しないエラーが発生しました: {exc}")

            result = _parse_output(proc.stdout or "")
            result.stderr = proc.stderr or ""
            result.return_code = proc.returncode
            if proc.returncode != 0 and result.success:
                # Script printed "OK" but exited non-zero -- treat as failure.
                result = ScriptResult(
                    success=False, stdout=result.stdout, stderr=result.stderr,
                    return_code=proc.returncode, err_desc="プロセスが異常終了しました。",
                )
            return result
        finally:
            try:
                os.unlink(script_path)
            except OSError:
                pass

    @staticmethod
    def _write_temp_script(script_text: str) -> str:
        fd, script_path = tempfile.mkstemp(suffix=".vbs", prefix="nippou_")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(script_text)
        return script_path


def run_with_retry(
    build_script_text: Callable[[], str],
    runner: Optional[ScriptRunner] = None,
    max_retry: Optional[int] = None,
    base_wait_sec: Optional[float] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> ScriptResult:
    """Port of the retry loop inside ``adoSQL``: retries only on lock
    conflicts, up to ``max_retry`` times, sleeping
    ``base_wait_sec * attempt`` seconds between tries.

    ``build_script_text`` 自体（SQL文の組み立て）が予期しない例外を投げても
    ここで拾って失敗として返す -- 1件のデータ異常が呼び出し元のプロセス
    全体を巻き込んでクラッシュさせないための最終防衛ライン。
    """
    runner = runner or ScriptRunner()
    max_retry = SETTINGS.max_retry if max_retry is None else max_retry
    base_wait_sec = SETTINGS.retry_base_wait_sec if base_wait_sec is None else base_wait_sec

    attempt = 0
    while True:
        attempt += 1
        try:
            script_text = build_script_text()
        except Exception as exc:  # noqa: BLE001 - 呼び出し元を落とさないための意図的な広い捕捉
            _logger.exception("スクリプト生成中に予期しないエラーが発生しました")
            return ScriptResult(success=False, attempts=attempt, err_desc=f"スクリプト生成に失敗しました: {exc}")

        try:
            result = runner.run(script_text)
        except Exception as exc:  # noqa: BLE001 - runner実装側の想定漏れも含めて呼び出し元を落とさない
            _logger.exception("スクリプト実行中に予期しないエラーが発生しました")
            return ScriptResult(success=False, attempts=attempt, err_desc=f"スクリプト実行に失敗しました: {exc}")

        if result.success:
            result.attempts = attempt
            return result

        result.error = classify_error(result.err_number, result.err_desc, result.stderr)
        result.attempts = attempt

        if result.error.kind is ErrorKind.LOCK_CONFLICT and attempt <= max_retry:
            wait = base_wait_sec * attempt
            _logger.warning("lock conflict, retry %d/%d in %.1fs: %s", attempt, max_retry, wait, result.error)
            sleep(wait)
            continue

        _logger.error("access bridge script failed permanently: %s", result.error)
        return result
