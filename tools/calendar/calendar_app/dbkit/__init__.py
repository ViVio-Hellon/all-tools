"""dbkit : VBA → Python 移行プロジェクト共通の DB 基盤。

特定の業務・プロジェクトに一切依存しない、汎用の部品だけを集めている。
このディレクトリごと他の移行プロジェクトへコピーして使い回すことを想定しており、
``calendar_app`` の他モジュールへは依存しない (依存は常に外向き)。

構成:

* ``sqlite_toolkit`` : SQLite のロック競合リトライ・簡易 CRUD
* ``source_db``       : 共有フォルダの sqlite3 取り込み元 (SMB 対策つき)
* ``outbox_sync``     : SQLite → Access の書き戻しエンジン (二重送信防止つき)
* ``logging_utils``   : 上記 3 つが使う最小限のロガー取得口
"""
