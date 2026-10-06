"""Mutual-exclusion logic for HdCh1-9 / CheckBox8-13,75-80.

Ports ``HdCh_排他制御`` and ``CheckBox_排他制御`` (standard module ~line
6130). Both take the name of the control that was just checked and clear
every other control the maps in :mod:`nippou.constants` say should be
turned off, but only touch controls that are currently ``True`` (the VBA
guarded against re-firing its own change events on already-unchecked
boxes; keeping the guard keeps the write set identical).

This module is UI-framework agnostic: callers pass in a ``get(name)`` /
``set(name, value)`` pair -- in the web version, backed by the ``checks``
dict of the posted screen state (see :mod:`nippou.presenters.entry`).
"""
from __future__ import annotations

from typing import Callable, Iterable

from .. import constants

BoolGetter = Callable[[str], bool]
BoolSetter = Callable[[str, bool], None]


def _clear_all(names: Iterable[str], get: BoolGetter, set_: BoolSetter) -> None:
    for name in names:
        if get(name):
            set_(name, False)


def apply_hdch_exclusion(trigger_name: str, get: BoolGetter, set_: BoolSetter) -> None:
    """Port of ``HdCh_排他制御(triggerName)``."""
    if trigger_name not in constants.HDCH_EXCLUDES_HDCH:
        return
    if not get(trigger_name):
        return
    _clear_all(constants.HDCH_EXCLUDES_HDCH[trigger_name], get, set_)
    _clear_all(constants.HDCH_EXCLUDES_CHECKBOX[trigger_name], get, set_)


def apply_checkbox_exclusion(trigger_name: str, get: BoolGetter, set_: BoolSetter) -> None:
    """Port of ``CheckBox_排他制御(triggerName)``."""
    if trigger_name not in constants.CHECKBOX_EXCLUDES_HDCH:
        return
    if not get(trigger_name):
        return
    _clear_all(constants.CHECKBOX_EXCLUDES_HDCH[trigger_name], get, set_)
    _clear_all(constants.CHECKBOX_EXCLUDES_CHECKBOX[trigger_name], get, set_)


def apply_exclusion(trigger_name: str, get: BoolGetter, set_: BoolSetter) -> None:
    """Dispatch to the right exclusion table based on the control's name,
    matching the way the form wired ``HdChN_Click`` vs ``CheckBoxN_Click``
    to two separate Subs."""
    if trigger_name in constants.HDCH_NAMES:
        apply_hdch_exclusion(trigger_name, get, set_)
    elif trigger_name in constants.DETAIL_CHECKBOX_NAMES:
        apply_checkbox_exclusion(trigger_name, get, set_)
