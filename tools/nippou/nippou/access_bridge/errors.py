"""Error classification for the Access bridge.

Ports ``IsLockError`` (standard module ~line 711) and extends it to also
recognise driver-mismatch (32/64-bit ACE provider missing) and
network/path-unreachable failures, per the "予期せぬエラーを捕捉できること"
requirement. The generated VBScript (see ``script_gen.py``) always prints
``ERRCODE=<n>|ERRDESC=<text>`` on failure so this classifier has the same
inputs the original ``Err.Number`` / ``Err.Description`` gave VBA.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class ErrorKind(Enum):
    LOCK_CONFLICT = auto()      # another user/process holds the record/table
    DRIVER_MISMATCH = auto()    # ACE OLEDB provider missing / 32-64bit mismatch
    NETWORK_UNREACHABLE = auto()  # UNC path / network share unreachable
    SQL_SYNTAX = auto()         # malformed SQL (bug in script_gen, or bad data)
    TIMEOUT = auto()            # subprocess exceeded the configured timeout
    UNKNOWN = auto()


# Ports IsLockError's Select Case exactly.
_LOCK_ERROR_NUMBERS = {-2147467259, -2147217887, 3260, 3261, 3045, 3050, 3218}

_DRIVER_ERROR_SNIPPETS = (
    "provider cannot be found",
    "class not registered",
    "がインストールされていません",
    "acedao",
    "ace.oledb",
)

_NETWORK_ERROR_SNIPPETS = (
    "network path was not found",
    "ネットワークパス",
    "could not find file",
    "見つかりません",
    "unreachable",
)

_SQL_SYNTAX_SNIPPETS = (
    "syntax error",
    "構文エラー",
)


@dataclass
class AccessBridgeError:
    kind: ErrorKind
    err_number: int | None
    message: str
    raw_stderr: str = ""

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return f"[{self.kind.name}] {self.err_number}: {self.message}"


def is_lock_error(err_num: int | None, err_desc: str) -> bool:
    """Direct port of ``IsLockError``."""
    if err_num in _LOCK_ERROR_NUMBERS:
        return True
    lowered = err_desc.lower()
    return "lock" in lowered or "ロック" in err_desc


def classify_error(err_num: int | None, err_desc: str, raw_stderr: str = "") -> AccessBridgeError:
    if is_lock_error(err_num, err_desc):
        kind = ErrorKind.LOCK_CONFLICT
    else:
        lowered = err_desc.lower()
        if any(s in lowered for s in _DRIVER_ERROR_SNIPPETS):
            kind = ErrorKind.DRIVER_MISMATCH
        elif any(s in lowered or s in err_desc for s in _NETWORK_ERROR_SNIPPETS):
            kind = ErrorKind.NETWORK_UNREACHABLE
        elif any(s in lowered or s in err_desc for s in _SQL_SYNTAX_SNIPPETS):
            kind = ErrorKind.SQL_SYNTAX
        else:
            kind = ErrorKind.UNKNOWN
    return AccessBridgeError(kind=kind, err_number=err_num, message=err_desc, raw_stderr=raw_stderr)
