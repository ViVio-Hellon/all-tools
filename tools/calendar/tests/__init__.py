"""テストが本番の設定・記録を書き換えないようにする

【なぜここでやるのか】
``calendar_app.config`` は既定で**利用者ごとのローカル領域**
(``%LOCALAPPDATA%\\LineCalendar\\data``)を掴む。本番の端末でテストを
流すと、動いているアプリの DB・設定・ログに混ざる。

環境変数はモジュールが読み込まれる**前**に立てる必要があるので、
``tests`` パッケージの取り込み時にここで立てる。

【注意: 探し方によっては読まれない】
unittest の探索は、開始フォルダをそのまま最上位フォルダとして扱うと、
テストを**パッケージ配下ではなく単体のモジュールとして**取り込む::

    python -m unittest discover -s tests        # この __init__.py は読まれない
    python -m unittest discover -s tests -t .   # 読まれる(README はこちら)

前者で流すと安全網が外れるので、``tests/_isolation.py`` が
「いま掴んでいるパスを見て、危なければ逃がす」二段目を持っている。
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile

_TMP = tempfile.mkdtemp(prefix="line_calendar_test_")
atexit.register(shutil.rmtree, _TMP, True)

# 業務データ(calendar.db / settings.json)の置き場所
os.environ.setdefault("CALENDAR_HOME", os.path.join(_TMP, "home"))
# 起動基盤のローカル領域(runtime / logs / work …)
os.environ.setdefault("CALENDAR_LOCAL_DIR", os.path.join(_TMP, "local"))
# 配布設定。**手元で書き出した ツール直下の 配布設定/ を試験に混ぜない**
os.environ.setdefault("CALENDAR_DISTRIBUTION_DIR",
                      os.path.join(_TMP, "配布設定"))
