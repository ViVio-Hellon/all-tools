"""画面とAPIの試験で毎回使う土台

【なぜまとめるか】
``test_web_*.py`` はどれも同じ3つを組む。

    1. 手元のDBを ``:memory:`` で作ってスキーマを当てる
    2. その接続を掴ませるため ``routes.get_db`` を差し替える
    3. ``create_app()`` して ``test_client()`` を取る

2つ目が厄介で、**元に戻し忘れると次の試験が閉じた接続を掴む**。
症状は「別のファイルの試験だけが落ちる」なので、原因に辿り着くまでが遠い。
戻す約束をここ1か所に閉じ込めて、呼ぶ側が忘れられないようにする。

**判断は何もしない。** 組み立ての手順を短く書けるようにするだけ。
"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from typing import Any, Optional

from . import _isolation

_isolation.ensure_isolated()

from calendar_app import config, db  # noqa: E402

# 試験用の起動トークン。**本物と同じ経路を通す**ために必ず付ける
TOKEN = "test-token-abc123"

#: 既定の管理者パスワード。ライン設定と参照パスを変えるのに要る
#: (``calendar_app/admin_password.py``)。**ここで作らず、本物を読む** ──
#: 値を書き写すと、既定を変えた日に試験だけが通らなくなる
ADMIN_PASSWORD = config.ADMIN_PASSWORD


def memory_db() -> sqlite3.Connection:
    """スキーマを当てた手元のDB。1件の試験のあいだだけ生きる。

    ``db.connect`` はファイルにも ``:memory:`` にも同じスキーマを当てる。
    """
    return db.connect(":memory:")


def bind_db(case: unittest.TestCase, conn: Optional[sqlite3.Connection] = None,
            ) -> sqlite3.Connection:
    """``app.routes.get_db`` にこの試験の接続を掴ませる。

    **戻すのはここが約束する。** ``case.addCleanup`` に閉じるところまで
    積むので、呼ぶ側は受け取った接続を使うだけでよい。
    """
    from app import routes

    conn = conn if conn is not None else memory_db()
    case.addCleanup(conn.close)
    original = routes.get_db
    routes.get_db = lambda: conn
    case.addCleanup(setattr, routes, "get_db", original)

    # ルートは ``from . import get_db`` で**名前を取り込んでいる**ので、
    # モジュール側だけ差し替えても効かない。取り込んだ先も入れ替える
    for name in ("calendar", "history", "settings", "master"):
        module = __import__(f"app.routes.{name}", fromlist=["get_db"])
        original_ref = module.get_db
        module.get_db = lambda: conn
        case.addCleanup(setattr, module, "get_db", original_ref)
    return conn


def make_client(*, ready: bool = True, port: int = 8730):
    """トークン付きで叩ける ``test_client``。"""
    from app import create_app

    app = create_app(token=TOKEN, port=port)
    app.config["TESTING"] = True
    app.config["READY"] = ready
    return app.test_client()


def auth() -> dict[str, str]:
    """トークンを載せるヘッダ。"""
    return {"X-Tool-Token": TOKEN}


def reset_sync() -> None:
    """同期サービスをまっさらにする。

    プロセスに1つなので、**試験の間で持ち越すと前の試験の設定が効く**。
    """
    from calendar_app import sync_service

    sync_service.reset()


#: 取り込み元の作成 SQL。**変換ツールが作るものと同じ形**
#: (tools/convert_accdb.py の SCHEMA)。
SOURCE_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS "{config.TABLE_DATA}" (
    "ID" INTEGER PRIMARY KEY, "日付" TEXT NOT NULL DEFAULT '',
    "区分" TEXT NOT NULL DEFAULT '', "登録内容" TEXT NOT NULL DEFAULT '',
    "識別コード" TEXT NOT NULL DEFAULT '', "直" TEXT NOT NULL DEFAULT '',
    "残業者" TEXT NOT NULL DEFAULT '', "早出者" TEXT NOT NULL DEFAULT '',
    "班" TEXT NOT NULL DEFAULT '', "ライン" TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS "{config.TABLE_DEL_HISTORY}" (
    "ID" INTEGER PRIMARY KEY, "削除日時" TEXT NOT NULL DEFAULT '',
    "削除実行者" TEXT NOT NULL DEFAULT '', "対象日付" TEXT NOT NULL DEFAULT '',
    "区分" TEXT NOT NULL DEFAULT '', "登録内容" TEXT NOT NULL DEFAULT '',
    "識別コード" TEXT NOT NULL DEFAULT '', "班" TEXT NOT NULL DEFAULT '',
    "ライン" TEXT NOT NULL DEFAULT ''
);
"""

MASTER_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS "{config.TABLE_MEMBER}" (
    "管理番号" TEXT PRIMARY KEY, "苗字" TEXT NOT NULL DEFAULT '',
    "班" TEXT NOT NULL DEFAULT '', "名前" TEXT NOT NULL DEFAULT '',
    "読み" TEXT NOT NULL DEFAULT '', "担当ライン" TEXT NOT NULL DEFAULT ''
);
"""


def make_source(folder: Path, name: str, schema: str) -> Path:
    """本物の取り込み元 (sqlite3) を1つ作る。

    **取り込み元が sqlite3 になってから、書き込み経路も試験できる。**
    Access のころは ODBC ドライバが要り、Linux では1行も試せなかった。
    """
    path = folder / name
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.executescript(schema)
        conn.commit()
    finally:
        conn.close()
    return path


def with_source(case: unittest.TestCase, *, master: bool = False) -> Path:
    """参照パスを立てて、**登録できる状態**にする。

    Web 版は「書き先が決まっていなければ登録を断る」ので
    (``app/routes/calendar.py`` の ``_require_source``)、登録を試す試験は
    先にここを通す。

    中身は**本物の sqlite3**。空ファイルで済ませていたのは取り込み元が
    Access だったころの名残で、いまは実際に書けるところまで試せる。

    :param master: マスタDB(班員名簿)側も置くか
    """
    import tempfile

    from calendar_app import settings as user_settings, sync_service
    from calendar_app.dbkit import outbox_sync

    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    folder = Path(tmp.name)
    make_source(folder, config.SOURCE_FILE_DATA, SOURCE_SCHEMA)
    if master:
        make_source(folder, config.SOURCE_FILE_MASTER, MASTER_SCHEMA)

    user_settings.set_value(user_settings.KEY_DATA_DB_DIR, str(folder))
    user_settings.set_value(user_settings.KEY_MASTER_DB_DIR, str(folder))
    case.addCleanup(user_settings.set_value, user_settings.KEY_DATA_DB_DIR, "")
    case.addCleanup(user_settings.set_value, user_settings.KEY_MASTER_DB_DIR, "")

    # 送信ID列を作れたかどうかの覚えは**取り込み元ごと**。
    # 試験のたびに作り直すので、前の試験の結果を持ち越さない
    outbox_sync.reset_op_id_cache()
    case.addCleanup(outbox_sync.reset_op_id_cache)

    sync_service.reset()
    sync_service.get_service()          # いまの設定で作り直す
    case.addCleanup(sync_service.reset)
    return folder


def read_source(path: Path, table: str) -> list[dict]:
    """取り込み元の中身を直に読む(試験の確認用)。"""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(f'SELECT * FROM "{table}"')]
    finally:
        conn.close()


def json_of(response) -> Any:
    return response.get_json()
