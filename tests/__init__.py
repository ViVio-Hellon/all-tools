"""統合ツールの入口(portal)・外枠との取り決め・起動ファイルの試験

**本番の領域を汚さない。** このパッケージを読み込んだ時点で、手元の領域・共有の DB の
置き場所・この端末の設定を一時フォルダへ向ける(`portal` を読み込む前に)。

    python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile

# 試験から起こした子のプロセス(錠を握る役など)は、親と同じ一時フォルダを使う
_BASE = os.environ.get("ALLTOOLS_TEST_BASE", "")
if not _BASE:
    _BASE = tempfile.mkdtemp(prefix="alltools_test_")
    os.environ["ALLTOOLS_TEST_BASE"] = _BASE
    atexit.register(shutil.rmtree, _BASE, True)

os.environ["ALLTOOLS_LOCAL_DIR"] = os.path.join(_BASE, "local")
os.environ["ALLTOOLS_SHARED_DB_DIR"] = os.path.join(_BASE, "share")
os.environ["ALLTOOLS_SETTINGS_PATH"] = os.path.join(_BASE, "local", "data", "user_settings.json")
os.environ["ALLTOOLS_LOGIN_ID"] = "tester"
os.environ["ALLTOOLS_PC_NAME"] = "TEST-PC"
os.environ["ALLTOOLS_LOG_QUIET"] = "1"
# 配布設定を一式のフォルダ(このリポジトリ)に書かせない
os.environ["ALLTOOLS_DISTRIBUTION_DIR"] = os.path.join(_BASE, "配布設定")
os.environ.pop("ALLTOOLS_SHARED_DB_NAME", None)
os.environ.pop("ALLTOOLS_ADMIN_PASSWORD", None)
os.makedirs(os.environ["ALLTOOLS_SHARED_DB_DIR"], exist_ok=True)

BASE = _BASE
