"""Web版テストの共通の下ごしらえ

**1件ごとに隔離する。** `work_context` はプロセスに1つの状態を持ち、
`config.SETTINGS` は環境変数から作られるので、そのままだとテストが
互いの結果を引きずる。

Flask が入っていない環境では、Web版のテストをまとめてスキップする
(ロジック層のテストは Flask 無しでも通る)。
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import flask  # noqa: F401

    HAS_FLASK = True
except ImportError:                              # pragma: no cover
    HAS_FLASK = False

SKIP_REASON = "Flask が入っていないためスキップ (pip install -r requirements.txt)"

TOKEN = "test-token"
HEADERS = {"Host": "127.0.0.1", "X-Tool-Token": TOKEN}
# トークンを付けない要求(素通しの経路を確かめる用)
NO_TOKEN_HEADERS = {"Host": "127.0.0.1"}


def close_log_files_under(folder: Path) -> None:
    """`folder` の中へ書いているログのハンドラを閉じて外す。

    ロガーはプロセスに1つずつ残るので、付けたファイルのハンドラも次の
    テストまで開いたまま残る。**開いたままのファイルは Windows では消せない**
    ので、一時フォルダを消す前に閉じる。
    """
    root = str(Path(folder).resolve())
    loggers = [logging.getLogger()] + [
        lg for lg in logging.root.manager.loggerDict.values() if isinstance(lg, logging.Logger)]
    for logger in loggers:
        for handler in list(logger.handlers):
            name = getattr(handler, "baseFilename", "")
            if name and str(Path(name).resolve()).startswith(root):
                logger.removeHandler(handler)
                handler.close()


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class WebTestCase(unittest.TestCase):
    """Flask のテストクライアントを1件ごとに作り直す。"""

    #: 準備完了の状態で始めるか。False にすると起動待機画面が出る
    ready = True
    #: この端末のライン。**決めてある端末として始める**(v4.3.0 から、決まって
    #: いないと日報入力は打てない)。決めていない端末を試すテストは None にする
    terminal_line: str | None = "L-1"

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        # **一時フォルダは一番最後に消す。** 後片付け(`addCleanup`)は tearDown の
        # あとに、登録と逆の順で走る。`repo()` の接続を閉じるのもそこなので、
        # tearDown で消すと開いたままのファイルを消そうとする(Windows は断る)
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name)
        self.addCleanup(close_log_files_under, self.tmp)

        # ローカル領域と実行時ファイルを、この1件ぶんの一時フォルダへ寄せる。
        #
        # **参照用マスタの置き場所もここへ寄せます。** 既定は
        # `~/NippouGwRef` で、テストが書き出すものを本物のフォルダへ
        # 置いてしまいます(目標CSVの見本を出すテストで実際に起きました)。
        # テストは自分の一時フォルダの外へ書かない、が約束です。
        self._env = {
            "NIPPOU_LOCAL_DIR": str(self.tmp / "local"),
            "NIPPOU_APP_DIR": str(self.tmp / "app"),
            "NIPPOU_GW_REF_DIR": str(self.tmp / "ref"),
            # 配布設定も一時フォルダへ。**本物の `配布設定` を
            # 読まない・書かない**(`distribution`)
            "NIPPOU_DIST_DIR": str(self.tmp / "配布設定"),
        }
        self._saved_env = {k: os.environ.get(k) for k in self._env}
        os.environ.update(self._env)

        # `config.SETTINGS` は import 時に作られるので、環境変数を入れて
        # から読み込み直す
        for name in [m for m in sys.modules if m.startswith(("nippou", "app"))]:
            del sys.modules[name]

        from app import create_app
        from nippou import config, idle_exit, running, source_db, user_settings, work_context

        if self.terminal_line:
            user_settings.save(config.KEY_TERMINAL_LINE, self.terminal_line)
        # **プロセスに1つのものは、全部ここで戻す。** モジュールを読み直して
        # いるので普段は消えるが、先に読み込んだテストが掴んだままの
        # 見張りスレッドは生き残る(daemon なので落ちもしない)
        work_context.reset()
        idle_exit.reset()
        running.reset()
        source_db.forget()
        self.addCleanup(idle_exit.reset)
        self.addCleanup(running.reset)
        self.app = create_app(token=TOKEN, port=8733)
        self.app.config["READY"] = self.ready
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        # 出来事の記録は裏のスレッドが書く。**片付ける前に書き終えさせる**
        # (書きかけが残ると、消した一時フォルダを作り直してしまう)
        event_log = sys.modules.get("nippou.services.event_log")
        if event_log is not None:
            event_log.flush(5.0)
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # -- 短く書くための助け --------------------------------------------
    def get(self, path: str, **kwargs):
        return self.client.get(path, headers=HEADERS, **kwargs)

    def post(self, path: str, payload=None, **kwargs):
        return self.client.post(path, json=payload if payload is not None else {},
                                headers=HEADERS, **kwargs)

    def repo(self):
        """テストから直接DBを触るためのリポジトリ。

        アプリのリクエストとは別の接続になるが、同じファイルを見る。
        """
        from nippou.config import SETTINGS
        from nippou.db.connection import connect
        from nippou.db.repository import NippouRepository

        conn = connect(SETTINGS.sqlite_path)
        self.addCleanup(conn.close)
        return NippouRepository(conn)
