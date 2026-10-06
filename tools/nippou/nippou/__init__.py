"""Excel VBA + Access(.accdb) 日報ツールの Python 版。

画面は Flask + HTML(`app/` 以下)。この `nippou` パッケージは
**画面に依存しない側** ── 業務ロジック(`logic/`)、SQLite の読み書き
(`db/`)、Access 連携(`access_bridge/`)、帳票・CSV(`reporting/`)を
持つ。

外部依存は Flask と waitress だけ(`requirements.txt`)で、しかもその
2つを使うのは `app/` と起動基盤のみ。このパッケージ自体は標準ライブラリ
(sqlite3, subprocess, logging, csv, dataclasses, ...)だけで動く ──
つまり**画面を通さずに業務ロジックを呼べる**。
"""

__version__ = "0.1.0"
