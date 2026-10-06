"""Concurrent-access detection for the .accdb file.

Two independent signals, both referenced in the requirements:

1. The ``.laccdb`` lock file Access creates next to a database while
   *anyone* has it open (multi-user "who's in the file" signal -- Access
   itself uses this for its own "already opened" warnings). Its mere
   presence does not mean a write will fail (Access supports concurrent
   record-level access), so this alone is only used to warn the user
   before a push, not to block it.
2. A direct exclusive-open probe, porting ``IsBookOpened`` (standard
   module ~line 21650): attempts to open the file the same way VBA's
   ``Open filePath For Append`` did. If that raises, something holds an
   incompatible lock on the whole file right now.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


def laccdb_path(accdb_path: Path) -> Path:
    return accdb_path.with_suffix(".laccdb")


def has_laccdb_lock(accdb_path: Path) -> bool:
    return laccdb_path(accdb_path).exists()


def is_book_opened(accdb_path: Path) -> bool:
    """Port of ``IsBookOpened``: True if the file cannot even be opened
    for append right now (i.e. someone holds an exclusive lock)."""
    if not accdb_path.exists():
        return False
    try:
        with open(accdb_path, "ab"):
            pass
        return False
    except OSError:
        return True


@dataclass
class ConcurrencyStatus:
    laccdb_present: bool
    exclusively_locked: bool

    @property
    def others_may_be_editing(self) -> bool:
        return self.laccdb_present or self.exclusively_locked

    def message(self) -> str:
        if self.exclusively_locked:
            return "現在他の人が使用中です。しばらく待ってから再度お試しください。"
        if self.laccdb_present:
            return "他の利用者がこのファイルを開いています。書き込みは可能ですが競合に注意してください。"
        return ""


def check_concurrent_access(accdb_path: Path) -> ConcurrencyStatus:
    return ConcurrencyStatus(
        laccdb_present=has_laccdb_lock(accdb_path),
        exclusively_locked=is_book_opened(accdb_path),
    )
