"""リポジトリのルートを import できるようにする。

`python -m unittest discover` / 単体ファイルの直接実行 / IDE のテスト
ランナー、どの呼ばれ方でも `nippou` `dbkit` `app` を読めるようにする。
`pip install -e .` は要らない(配布はフォルダごとコピーなので、
インストール手順を増やさない)。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ------------------------------------------------------------------
# **本物の配布設定フォルダに書かない。**
#
# 配布設定(`nippou/distribution.py`)は、置き場所を `NIPPOU_DIST_DIR` で
# 差し替えないと起動用の Start.vbs と同じフォルダ(= このリポジトリの直下)に
# 作られます。1件ごとに差し替えるテスト(`tests/_web.py` ほか)が差し替え
# 忘れても書かないよう、テスト全体の既定をここで一時フォルダへ向けておきます
# ── 一度、差し替え忘れた回にリポジトリの直下へ `配布先` ができました。
#
# 差し替えている間は、前の版の置き場所(`配布先\` / `config\site.json`)も
# 読みません(`distribution._legacy_dirs`)。
# ------------------------------------------------------------------
import atexit as _atexit
import os as _os
import shutil as _shutil
import tempfile as _tempfile

if not _os.environ.get("NIPPOU_DIST_DIR"):
    _DIST_GUARD = _tempfile.mkdtemp(prefix="nippou-dist-guard-")
    _os.environ["NIPPOU_DIST_DIR"] = str(Path(_DIST_GUARD) / "配布設定")
    _atexit.register(_shutil.rmtree, _DIST_GUARD, True)
