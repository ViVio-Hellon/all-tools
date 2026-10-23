"""ログの**置き場所と名前**を決める所 (v4.44.4)。名前の作り方はここ1か所。

    ログの出力先がいろいろなツールで吐き出しているので、これでは何の
    ツールが出したものかわからない。ファイル名にツール名を入れる、
    ツール名フォルダを作ってその階層に作る、の両方をお願いしたい
    (取り出したとき結局開かないとわからなくなるため)

【いまの形】(`<ログの出力パス>` は設定の「ログの出力パス」。決めていなければ
この端末の logs。どちらでも同じ形)

    <ログの出力パス>/nippou/
      nippou_動作記録_PC-L1.log               動いた記録(5MBで回す。.log.1〜.log.5)
      nippou_出来事_2026-10-15_PC-L1.jsonl    エラー・操作の記録(端末ごと・日ごと)
      nippou_なぜなぜ_PC-L1.jsonl             書いてもらったなぜなぜ
      書き出し/nippou_エラー一覧_20261015_120000.csv

**動いた記録にも端末の名前を入れます。** 前の `nippou.log` は名前に端末が
無く、共有のフォルダを指すと**何台もの端末が同じ1つのファイルへ**書いて
いました(行が混ざり、5MBで回すときに別の端末の名前替えとぶつかる)。

【前の形】(v4.44.3 まで。**読むだけ**。移さない・書かない)

    <ログの出力パス>/nippou.log
    <ログの出力パス>/出来事_2026-10-15_PC-L1.jsonl
    <ログの出力パス>/なぜなぜ_PC-L1.jsonl

前の形の出来事・なぜなぜは、一覧・なぜなぜ・古いものを消す、で新しい形と
同じに扱います。読む・消すのは**この名前で始まるもの**だけ ── 同じフォルダに
ほかのツールのファイル(`dbviewer_….log` `記録_….jsonl` など)があっても
触りません。前のファイルを新しいフォルダへ移すことはしません(共有の
フォルダでは、ほかの端末が古い版のまま書いている最中のことがあるので)。
"""
from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from .file_names import UNSAFE_OR_SPACE

#: ツールの名前。フォルダの名前で、ファイル名の頭(`nippou_`)
TOOL_ID = "nippou"
PREFIX = f"{TOOL_ID}_"

TEXT_KIND = "動作記録"
EVENT_KIND = "出来事"
NOTE_KIND = "なぜなぜ"
EXPORT_KIND = "エラー一覧"
EXPORT_DIR = "書き出し"

#: 前の形(v4.44.3 まで)。**読む・消すときだけ**使う
LEGACY_TEXT_LOG = "nippou.log"
LEGACY_EVENT_PREFIX = f"{EVENT_KIND}_"
LEGACY_NOTE_PREFIX = f"{NOTE_KIND}_"

#: 新しい形の頭
EVENT_PREFIX = f"{PREFIX}{EVENT_KIND}_"
NOTE_PREFIX = f"{PREFIX}{NOTE_KIND}_"

_EVENT_DATE = re.compile(
    rf"^(?:{re.escape(PREFIX)})?{re.escape(EVENT_KIND)}_(\d{{4}}-\d{{2}}-\d{{2}})_")


def safe(name: str) -> str:
    """ファイル名に入れる端末の名前(使えない字と空白は `_`)。"""
    return UNSAFE_OR_SPACE.sub("_", str(name or "")).strip("_") or "端末"


def tool_dir(base: Path) -> Path:
    """`<ログの出力パス>/nippou`。"""
    return Path(base) / TOOL_ID


def text_log_name(terminal: str) -> str:
    return f"{PREFIX}{TEXT_KIND}_{safe(terminal)}.log"


def event_name(day: date, terminal: str) -> str:
    return f"{EVENT_PREFIX}{day:%Y-%m-%d}_{safe(terminal)}.jsonl"


def note_name(terminal: str) -> str:
    return f"{NOTE_PREFIX}{safe(terminal)}.jsonl"


def export_name(now: datetime) -> str:
    return f"{PREFIX}{EXPORT_KIND}_{now:%Y%m%d_%H%M%S}.csv"


def event_places(base: Path) -> list[tuple[Path, str]]:
    """出来事を読む(消す)ところ ── **(フォルダ, 名前の頭)**。新しい形が先。"""
    return [(tool_dir(base), EVENT_PREFIX), (Path(base), LEGACY_EVENT_PREFIX)]


def note_places(base: Path) -> list[tuple[Path, str]]:
    """なぜなぜを読むところ ── **(フォルダ, 名前の頭)**。新しい形が先。"""
    return [(tool_dir(base), NOTE_PREFIX), (Path(base), LEGACY_NOTE_PREFIX)]


def event_date(name: str) -> Optional[date]:
    """出来事のファイル名の日付(新旧どちらの形でも)。出来事でなければ None。"""
    m = _EVENT_DATE.match(name)
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(1))
    except ValueError:
        return None
