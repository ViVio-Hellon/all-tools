"""エラーの後追い ── ログから「なぜなぜ分析」ができるか

【なぜ要るか】
エラーが起きたあとで困るのは、**事実が残っていない**こと。利用者に聞いても
「押したら赤いのが出た」くらいしか分からない。ここで確かめるのは:

* ログの置き場所を設定で変えられる。共有を指定しても**PC名で分かれる**
* 指定先に書けなくても**ローカルへ書き続ける**(ログが要る日ほどネットワークが不調)
* 思わぬエラーに**記録番号**が付き、画面にも同じ番号が出る
* 断った理由・操作・画面のエラーが残る。合言葉は残さない
* 記録番号から、なぜなぜ分析の材料が1枚にまとまる
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from . import _isolation

_isolation.ensure_isolated()

from calendar_app import (  # noqa: E402
    config,
    distribution,
    log_report,
    logging_utils,
    settings as user_settings,
)

from . import _web  # noqa: E402


class LogPlace(unittest.TestCase):
    """設定ファイル・既定のログ置き場を一時フォルダへ向ける。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.default = self.root / "local" / "logs"
        # **最後に戻す。** 差し替えを外したあとで付け替え直すと、
        # 他の試験が使う本来の書き先に戻る
        self.addCleanup(logging_utils.reconfigure)
        self._patch(config, "settings_path", lambda: self.root / "settings.json")
        self._patch(logging_utils, "default_dir", lambda: self.default)
        self._patch(logging_utils, "_pc_name", lambda: "PC-01")
        logging_utils.reconfigure()

    def _patch(self, target, name, value) -> None:
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def today_file(self, folder: Path) -> Path:
        return folder / logging_utils._log_name(_dt.date.today())

    def read(self, folder: Path) -> str:
        path = self.today_file(folder)
        return path.read_text(encoding="utf-8") if path.exists() else ""


# ---------------------------------------------------------------------------
# 置き場所
# ---------------------------------------------------------------------------
class FolderTests(LogPlace):
    def test_未設定なら既定の場所(self) -> None:
        logging_utils.get_logger("t").info("既定へ")
        self.assertEqual(logging_utils.status()["folder"], str(self.default))
        self.assertIn("既定へ", self.read(self.default))

    def test_指定するとPC名のフォルダに書く(self) -> None:
        """共有を指定しても、端末ごとに分かれて混ざらない。"""
        share = self.root / "share"
        share.mkdir()
        user_settings.set_value(user_settings.KEY_LOG_DIR, str(share))
        logging_utils.reconfigure()

        logging_utils.get_logger("t").info("共有へ")
        self.assertEqual(logging_utils.status()["folder"], str(share / "PC-01"))
        self.assertIn("共有へ", self.read(share / "PC-01"))
        self.assertNotIn("共有へ", self.read(self.default))

    def test_指定先に書けなければローカルへ書き続ける(self) -> None:
        blocker = self.root / "ファイル"
        blocker.write_text("x", encoding="utf-8")       # フォルダを作れない場所
        user_settings.set_value(user_settings.KEY_LOG_DIR, str(blocker))
        logging_utils.reconfigure()

        logging_utils.get_logger("t").info("逃げた先へ")
        state = logging_utils.status()
        self.assertEqual(state["folder"], str(self.default))
        self.assertTrue(state["fallback_reason"])
        text = self.read(self.default)
        self.assertIn("ログフォルダに書けないので", text)
        self.assertIn("逃げた先へ", text)

    def test_途中で書けなくなってもその行を落とさない(self) -> None:
        """共有が途中で切れた。**切れた瞬間の行**こそ後で要る。"""
        share = self.root / "share"
        share.mkdir()
        handler = logging_utils.DailyFileHandler(share, fallback=self.default)
        handler.setFormatter(logging.Formatter("%(message)s"))
        self.addCleanup(handler.close)

        class Broken:
            def write(self, _text):
                raise OSError("ネットワーク名が見つかりません")

            def flush(self):
                pass

            def close(self):
                pass

        handler.stream.close()
        handler.stream = Broken()
        handler.emit(logging.LogRecord("t", logging.ERROR, "", 1, "切れた瞬間", None, None))
        self.assertIn("切れた瞬間", self.read(self.default))
        self.assertIn("ネットワーク名が見つかりません", handler.fallback_reason)

    def test_日付が変わったら指定先へ戻る(self) -> None:
        share = self.root / "share"
        handler = logging_utils.DailyFileHandler(share, fallback=self.default)
        handler.setFormatter(logging.Formatter("%(message)s"))
        self.addCleanup(handler.close)
        handler._use_fallback(OSError("一時的"))
        self.assertEqual(handler.folder, self.default)

        tomorrow = _dt.date.today() + _dt.timedelta(days=1)
        record = logging.LogRecord("t", logging.INFO, "", 1, "あす", None, None)
        record.created = _dt.datetime.combine(tomorrow, _dt.time(9)).timestamp()
        handler.emit(record)
        self.assertEqual(handler.folder, share)
        self.assertEqual(handler.fallback_reason, "")
        self.assertIn("あす", (share / logging_utils._log_name(tomorrow))
                      .read_text(encoding="utf-8"))

    def test_配布設定に入れられる(self) -> None:
        self.assertIn(user_settings.KEY_LOG_DIR, distribution.ITEM_KEYS)
        self.assertIn(user_settings.KEY_LOG_DIR, distribution.PATH_KEYS)


# ---------------------------------------------------------------------------
# 1行の形
# ---------------------------------------------------------------------------
class LineTests(LogPlace):
    def test_ERROR以上には記録番号が付く(self) -> None:
        log = logging_utils.get_logger("t")
        log.info("ふつう")
        log.error("こまった")
        lines = self.read(self.default).splitlines()
        self.assertNotIn("記録番号=", next(l for l in lines if "ふつう" in l))
        self.assertRegex(next(l for l in lines if "こまった" in l),
                         logging_utils.REF_PATTERN)

    def test_渡した記録番号をそのまま使う(self) -> None:
        """画面に出した番号とログの番号が違うと、たどれない。"""
        logging_utils.get_logger("t").error("番号つき", extra={"ref": "E20261001-120000-AAA"})
        self.assertIn("記録番号=E20261001-120000-AAA", self.read(self.default))

    def test_追跡番号が載る(self) -> None:
        token = logging_utils.set_trace("r=ABCDEF")
        try:
            logging_utils.get_logger("t").info("操作の中")
        finally:
            logging_utils.reset_trace(token)
        logging_utils.get_logger("t").info("操作の外")
        text = self.read(self.default)
        self.assertIn("| r=ABCDEF | 操作の中", text)
        self.assertIn("| - | 操作の外", text)

    def test_裏のスレッドで落ちた例外も残す(self) -> None:
        import threading

        def boom() -> None:
            raise RuntimeError("見張りが落ちた")

        with mock.patch("sys.stderr"):             # 既定の出力は黙らせる
            thread = threading.Thread(target=boom, name="sync-loop")
            thread.start()
            thread.join()
        text = self.read(self.default)
        self.assertIn("スレッド sync-loop が例外で止まりました", text)
        self.assertIn("RuntimeError: 見張りが落ちた", text)
        self.assertIn("記録番号=", text)


# ---------------------------------------------------------------------------
# 画面からの要求
# ---------------------------------------------------------------------------
class WebTests(LogPlace):
    def setUp(self) -> None:
        super().setUp()
        _web.bind_db(self)
        _web.reset_sync()
        self.addCleanup(_web.reset_sync)
        from app import tracing
        from app.routes import diagnostics

        tracing.reset()
        diagnostics.reset()
        self.client = _web.make_client()

    def test_思わぬエラーは記録番号を返して全文を残す(self) -> None:
        with mock.patch("app.routes.settings.presenter.view",
                        side_effect=ZeroDivisionError("わざと")):
            res = self.client.get("/api/settings", headers=_web.auth())
        self.assertEqual(res.status_code, 500)
        error = res.get_json()["error"]
        self.assertEqual(error["code"], "internal")
        self.assertIn(error["ref"], error["message"])   # 画面にも番号が出る

        text = self.read(self.default)
        self.assertIn(f"記録番号={error['ref']}", text)
        self.assertIn("ZeroDivisionError: わざと", text)
        self.assertIn("Traceback", text)

    def test_断った理由が残る(self) -> None:
        res = self.client.post("/api/settings/log-dir", headers=_web.auth(),
                               json={"log_dir": str(self.root / "無い")})
        self.assertEqual(res.status_code, 422)
        text = self.read(self.default)
        self.assertIn("断りました POST /api/settings/log-dir → 422 not_found", text)
        self.assertIn("ログフォルダが見つかりません", text)

    def test_同じ断りが続いてもまとめる(self) -> None:
        """古いタブが1秒ごとに断られ続けても、ログを埋めない。"""
        for _ in range(5):
            self.client.get("/api/settings")              # トークン無し
        text = self.read(self.default)
        self.assertEqual(text.count("断りました GET /api/settings → 403 bad_token"), 1)

    def test_合言葉は残さない(self) -> None:
        self.client.post("/api/settings/line", headers=_web.auth(),
                         json={"line": "コイル", "password": "ひみつの合言葉"})
        text = self.read(self.default)
        self.assertIn("/api/settings/line", text)
        self.assertNotIn("ひみつの合言葉", text)
        self.assertIn('"password": "***"', text)

    def test_読むだけの要求は残さない(self) -> None:
        """1秒ごとの問い合わせで、肝心の操作が埋もれないように。"""
        self.client.get("/api/sync", headers=_web.auth())
        self.client.get("/api/settings", headers=_web.auth())
        self.assertNotIn("/api/sync", self.read(self.default))

    def test_画面のエラーを残す(self) -> None:
        body = {"kind": "error", "message": "TypeError: x is undefined",
                "source": "/static/js/views/calendar.js", "line": 10, "col": 5,
                "stack": "at draw (calendar.js:10:5)\nat start (calendar.js:3:1)",
                "page": "/calendar"}
        res = self.client.post("/api/client-log", headers=_web.auth(), json=body)
        ref = res.get_json()["ref"]
        self.assertTrue(ref)
        text = self.read(self.default)
        self.assertIn(f"記録番号={ref}", text)
        self.assertIn("画面でエラー: TypeError: x is undefined", text)
        self.assertIn("    at draw (calendar.js:10:5)", text)

        # 同じ文言は1回だけ(壊れた画面が毎秒投げても埋めない)
        again = self.client.post("/api/client-log", headers=_web.auth(), json=body)
        self.assertFalse(again.get_json()["ok"])

    def test_画面のエラーもまとめられる(self) -> None:
        """JS のエラーは Python の形ではない。文言と場所が直接の原因になる。"""
        ref = self.client.post("/api/client-log", headers=_web.auth(), json={
            "message": "Uncaught TypeError: row is null",
            "source": "/static/js/views/master.js", "line": 42, "col": 7,
            "stack": "at draw (master.js:42:7)", "page": "/settings"}).get_json()["ref"]
        text = self.client.get(f"/api/logs/report?ref={ref}",
                               headers=_web.auth()).get_json()["text"]
        cause = text.split("■ 直接の原因")[1].split("■")[0]
        self.assertIn("Uncaught TypeError: row is null", cause)
        self.assertIn("/static/js/views/master.js:42:7", cause)
        self.assertIn("どこで   : 画面(ブラウザ)", text)

    def test_画面のエラーもトークンが要る(self) -> None:
        res = self.client.post("/api/client-log", json={"message": "x"})
        self.assertEqual(res.status_code, 403)

    def test_ログフォルダを設定画面から変える(self) -> None:
        share = self.root / "share"
        share.mkdir()
        res = self.client.post("/api/settings/log-dir", headers=_web.auth(),
                               json={"log_dir": str(share)})
        self.assertEqual(res.status_code, 200, res.get_json())
        payload = res.get_json()
        self.assertEqual(payload["log"]["folder"], str(share / "PC-01"))
        self.assertEqual(user_settings.log_dir_setting(), str(share))
        # **前の書き先に、どこへ移ったかを残す**
        self.assertIn(f"ログフォルダを変えます: {self.default} → {share / 'PC-01'}",
                      self.read(self.default))
        moved = self.read(share / "PC-01")
        self.assertIn("ログフォルダを変えました", moved)
        # **新しい書き先にも見出しを残す。** そこだけ渡されても、どの端末の
        # どの設定のログかが読めるように
        self.assertIn("見出し: ログの書き先が変わったので", moved)
        self.assertIn("端末: PC名=", moved)

        res = self.client.post("/api/settings/log-dir", headers=_web.auth(),
                               json={"log_dir": ""})
        self.assertEqual(res.get_json()["log"]["folder"], str(self.default))

    def test_一覧とまとめ(self) -> None:
        with mock.patch("app.routes.settings.presenter.view",
                        side_effect=KeyError("ライン")):
            ref = self.client.get("/api/settings", headers=_web.auth()) \
                .get_json()["error"]["ref"]

        listing = self.client.get("/api/logs", headers=_web.auth()).get_json()
        self.assertIn(ref, [row["ref"] for row in listing["errors"]])
        self.assertEqual(listing["status"]["folder"], str(self.default))

        res = self.client.get(f"/api/logs/report?ref={ref.lower()}", headers=_web.auth())
        self.assertEqual(res.status_code, 200)
        text = res.get_json()["text"]
        self.assertIn(f"記録番号 {ref}", text)
        self.assertIn("KeyError: 'ライン'", text)
        self.assertIn("■ なぜなぜ", text)

        missing = self.client.get("/api/logs/report?ref=E20000101-000000-AAA",
                                  headers=_web.auth())
        self.assertEqual(missing.status_code, 404)

    def test_設定画面にログの書き先が出る(self) -> None:
        payload = self.client.get("/api/settings", headers=_web.auth()).get_json()
        self.assertEqual(payload["log"]["folder"], str(self.default))
        self.assertEqual(payload["log_dir"], str(self.default))


# ---------------------------------------------------------------------------
# なぜなぜ分析の材料
# ---------------------------------------------------------------------------
SAMPLE = """\
2026/09/30 07:58:00.000 | INFO | calendar_app.launcher | MainThread | - | 起動: pid=1 VER3.0.0
2026/09/30 07:58:00.001 | INFO | calendar_app.launcher | MainThread | - | 端末: PC名=PC-01 ログインID=u1 ライン=コイル
"""

TODAY = """\
2026/10/01 08:00:00.000 | INFO | calendar_app.app.request | waitress-1 | r=AAAAAA | 操作 POST /api/rest → 200 (12ms)
2026/10/01 08:00:05.000 | WARNING | calendar_app.sync_service | sync-once | s=CCCCCC | 同期の状態: 同期済み → 取り込み元へ送れません(共有に届きません)
2026/10/01 08:00:09.000 | DEBUG | calendar_app.repository | waitress-2 | r=DDDDDD | 登録を始めます
2026/10/01 08:00:09.500 | ERROR | calendar_app.app.request | waitress-2 | r=DDDDDD 記録番号=E20261001-080009-K3Q | 思わぬエラー POST /api/rest: OperationalError: database is locked
  入力: body={"day": "2026/10/01"}
Traceback (most recent call last):
  File "C:\\tool\\calendar_app\\dbkit\\source_db.py", line 50, in connect
    raise OSError("共有に届きません")
OSError: 共有に届きません

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "C:\\Python\\Lib\\site-packages\\flask\\app.py", line 900, in dispatch
    return view()
  File "C:\\tool\\calendar_app\\repository.py", line 123, in add_rest
    conn.execute(sql)
sqlite3.OperationalError: database is locked
2026/10/01 08:00:10.000 | INFO | calendar_app.app.request | waitress-3 | r=EEEEEE | 操作 POST /api/comment → 200 (8ms)
"""


class ReportTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name) / "share" / "PC-01"
        self.folder.mkdir(parents=True)
        (self.folder / "calendar_20260930.log").write_text(SAMPLE, encoding="utf-8")
        (self.folder / "calendar_20261001.log").write_text(TODAY, encoding="utf-8")

    def report(self) -> str:
        text = log_report.report("E20261001-080009-K3Q", folders=[self.folder])
        self.assertIsNotNone(text)
        return text

    def test_直接の原因とこちらのコードの場所(self) -> None:
        text = self.report()
        self.assertIn("sqlite3.OperationalError: database is locked", text)
        # ライブラリの中ではなく、こちらのコードのどこか
        self.assertIn("calendar_app/repository.py 123行目 (add_rest)", text)

    def test_原因のつながりは根本に近い順(self) -> None:
        text = self.report()
        chain = text.split("■ 原因のつながり")[1]
        self.assertLess(chain.index("OSError: 共有に届きません"),
                        chain.index("sqlite3.OperationalError"))

    def test_同じ操作の行と直前の流れ(self) -> None:
        text = self.report()
        same = text.split("■ 同じ操作の記録")[1].split("■")[0]
        self.assertIn("登録を始めます", same)              # DEBUG でも同じ操作なら出す
        before = text.split("■ 直前の流れ")[1].split("■")[0]
        self.assertIn("送れません", before)                 # 何をした後だったか
        self.assertNotIn("/api/comment", before)            # 後の行は出さない

    def test_起動が前の日でも起動時の様子を拾う(self) -> None:
        boot = self.report().split("■ 起動時の様子")[1].split("■")[0]
        self.assertIn("VER3.0.0", boot)
        self.assertIn("PC名=PC-01", boot)

    def test_端末名とどこで(self) -> None:
        text = self.report()
        self.assertIn("端末     : PC-01", text)
        self.assertIn("画面からの操作(追跡番号 r=DDDDDD)", text)

    def test_推測は書かない_なぜの欄は空(self) -> None:
        text = self.report()
        self.assertIn("なぜ1     : \n", text)

    def test_一覧は記録番号の付いた行だけ(self) -> None:
        rows = log_report.recent_errors(days=100000, folders=[self.folder])
        self.assertEqual([r["ref"] for r in rows], ["E20261001-080009-K3Q"])
        self.assertEqual(rows[0]["pc"], "PC-01")

    def test_集めたフォルダを下まで探す(self) -> None:
        """共有に全台ぶん集めたとき(tools/log_report.py --dir)。"""
        top = self.folder.parent
        self.assertIsNotNone(log_report.report("E20261001-080009-K3Q",
                                               folders=[top], recursive=True))
        self.assertIsNone(log_report.report("E20261001-080009-K3Q", folders=[top]))

    def test_番号の形が違えば探さない(self) -> None:
        self.assertIsNone(log_report.report("../../etc", folders=[self.folder]))

    def test_前の形の行も読める(self) -> None:
        entries = log_report.parse_lines(
            ["2026/09/01 10:00:00 | calendar_app.launcher | INFO | 起動: pid=1\n"])
        self.assertEqual(entries[0].level, "INFO")
        self.assertEqual(entries[0].message, "起動: pid=1")


class SyncChangeTests(unittest.TestCase):
    def test_送れなくなった時と戻った時を残す(self) -> None:
        from calendar_app import sync_service
        from calendar_app.sync.autosync import SyncState, SyncStatus

        with self.assertLogs("calendar_app.sync_service", level="INFO") as seen:
            sync_service._note_change(SyncStatus(state=SyncState.SYNCED),
                                      SyncStatus(state=SyncState.OFFLINE,
                                                 message="共有に届きません"))
            sync_service._note_change(SyncStatus(state=SyncState.OFFLINE),
                                      SyncStatus(state=SyncState.SYNCED))
            sync_service._note_change(SyncStatus(state=SyncState.SYNCED),
                                      SyncStatus(state=SyncState.SYNCED))
        self.assertEqual(len(seen.records), 2)
        self.assertEqual(seen.records[0].levelname, "WARNING")
        self.assertIn("共有に届きません", seen.records[0].getMessage())
        self.assertIn("ぶりに戻りました", seen.records[1].getMessage())


if __name__ == "__main__":
    unittest.main()
