"""音声フォルダを見る (v4.4.0)

出来事ごとに「鳴らさない / 音声フォルダのファイル」を選べるようにしたので、
**フォルダに何があるか**と**いま鳴らせる出来事はどれか**を、ここ1か所で
見ます(`logic/sound.py` が出来事の一覧、ここが置き場所)。

- 設定画面の選択肢 … `folder_files()`(音声フォルダの音声ファイル)
- 画面が鳴らしてよい出来事 … `playable_keys()`(1分ごとの見張りが受け取る)

**届かない・読めないは「無い」と同じに扱います。** 音は補助で、鳴らない
ことで何かを止めることはしません(文言は画面に出ます)。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..config import SETTINGS
from ..logic import sound


def is_audio_name(name: str) -> bool:
    """フォルダの中のファイル名1つとして通せるか(**フォルダは含めない**)。"""
    text = (name or "").strip()
    if not text or "/" in text or "\\" in text or text in (".", ".."):
        return False
    return Path(text).suffix.lower() in sound.AUDIO_SUFFIXES


def folder_files() -> tuple[list[str], str]:
    """音声フォルダの音声ファイル(名前順)と、読めなかったときの理由。"""
    folder = SETTINGS.sound_dir
    try:
        names = sorted((p.name for p in folder.iterdir()
                        if p.is_file() and is_audio_name(p.name)),
                       key=str.casefold)
    except FileNotFoundError:
        return [], f"音声フォルダがありません: {folder}"
    except OSError as exc:                        # 共有に届かない
        return [], f"音声フォルダを読めません: {folder} ({exc})"
    return names, ""


def file_path(name: str) -> Optional[Path]:
    """音声フォルダのファイル(試聴用)。**フォルダの外は指せない。**"""
    if not is_audio_name(name):
        return None
    path = SETTINGS.sound_dir / name.strip()
    try:
        return path if path.is_file() else None
    except OSError:
        return None


def playable(key: str) -> bool:
    """その出来事で、いま鳴らせるか(鳴らすと決めてあり、ファイルがある)。"""
    path = SETTINGS.sound_path(key)
    if path is None or path.suffix.lower() not in sound.AUDIO_SUFFIXES:
        return False
    try:
        return path.is_file()
    except OSError:
        return False


def playable_keys() -> list[str]:
    """いま鳴らせる出来事。**画面はこれに入っているものだけ鳴らします。**"""
    return [spec.key for spec in sound.SOUNDS if playable(spec.key)]


def texts(spec: sound.SoundSpec) -> tuple[str, str]:
    """画面に出す名前と「いつ鳴るか」(設定の分を入れたもの)。"""
    return sound.texts(spec, warn_minutes=SETTINGS.print_warning_minutes,
                       auto_minutes=SETTINGS.auto_print_minutes)
