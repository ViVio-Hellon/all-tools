"""テストが本番の設定・記録を書き換えないようにする(二段目)

``tests/__init__.py`` は環境変数を先に立てることで安全網を張るが、
これは**その ``__init__.py`` が読まれる場合に限って**効く
(理由はあちらの説明を参照)。

そこでこのモジュールが、環境変数に頼らず**いま掴んでいるパスを見て**
本番領域を指していたら一時フォルダへ向け直す。

【なぜ取り込み時に直せば間に合うのか】
unittest はテストを**全部取り込んでから**走らせる。書き込みが起きるのは
走っている最中なので、どれか1つのテストモジュールが取り込み時にここを
呼んでいれば、その時点で全テストぶんの向き先が直っている。
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

# 何度呼ばれても1回だけ効かせる(取り込み順に依存させない)
_done = False


def _escape(name: str) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix=f"line_calendar_test_{name}_"))
    atexit.register(shutil.rmtree, tmp, True)
    return tmp


def _is_temp(path: Path) -> bool:
    try:
        path.resolve().relative_to(Path(tempfile.gettempdir()).resolve())
    except (ValueError, OSError):
        return False
    return True


def ensure_isolated() -> list[str]:
    """危ない向き先を一時フォルダへ替える。替えた項目の名前を返す。

    既に ``tests/__init__.py`` が逃がしてあれば何もしない(戻り値は空)。
    """
    global _done
    if _done:
        return []
    _done = True

    from calendar_app import app_config, logging_utils

    moved: list[str] = []

    # 業務データ。既定は %LOCALAPPDATA%/LineCalendar/data(本番と同じ場所)
    if not _is_temp(Path(os.environ.get("CALENDAR_HOME", ""))):
        os.environ["CALENDAR_HOME"] = str(_escape("home"))
        moved.append("CALENDAR_HOME")

    # 起動基盤のローカル領域(ロック・ログ・一時ファイル)。
    # 本番の端末で流すと、動いているアプリの記録に混ざる
    if not _is_temp(app_config.local_root()):
        os.environ["CALENDAR_LOCAL_DIR"] = str(_escape("local"))
        moved.append("CALENDAR_LOCAL_DIR")

    # 配布設定: ツールのフォルダの直下の `配布設定/`。
    # **手元で書き出したものが試験に混ざる**(読み込み時に場所が決まるので、属性も替える)
    from calendar_app import distribution
    if not _is_temp(distribution.DIR):
        distribution.DIR = _escape("dist") / "配布設定"
        os.environ["CALENDAR_DISTRIBUTION_DIR"] = str(distribution.DIR)
        moved.append("distribution.DIR")

    # テストの出力が主役なので、業務ログはコンソールへ出さない
    # (ファイルへの出力は残るので、調査に必要な情報は失われない)
    logging_utils.silence_console()
    return moved
