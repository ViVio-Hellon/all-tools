"""ログの後追い(保存先の指定・退避・片付け)と、なぜなぜ用の出来事の記録

現場から「エラーが出た」と言われても、これまでは後から辿れなかった:
- ログは各PCのローカル(Store 版 Python ではエクスプローラーからも見えない場所)
- 画面の断りの文と、ログの行を結びつけるものが無かった
- 画面(ブラウザ)の中の失敗はどこにも残らなかった
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import unittest
from datetime import date, datetime, timedelta

from tests.helpers import temp_dir

from core import app_config, event_log, logging_utils


def _settings(**values):
    path = logging_utils.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = temp_dir()
        self.addCleanup(self._tmp.cleanup)
        self.shared = os.path.join(self._tmp.name, "共有", "ログ")
        self.addCleanup(self._restore)
        _settings()
        logging_utils.reset_for_tests()

    def _restore(self) -> None:
        try:
            logging_utils.settings_path().unlink()
        except OSError:
            pass
        logging_utils.reset_for_tests()

    def last_line(self, path) -> str:
        with open(path, encoding="utf-8") as fp:
            return fp.read().splitlines()[-1]


class LogFolderTest(_Base):
    def test_既定はこのPCのローカルで名前に端末名を入れない(self) -> None:
        status = logging_utils.status()
        self.assertTrue(status["is_local"])
        self.assertEqual(status["dir"], str(app_config.local_dir("logs")))
        self.assertEqual(os.path.basename(status["file"]), f"inspection_{date.today():%Y%m%d}.log")

    def test_指定したフォルダには端末名と利用者名入りで書く(self) -> None:
        _settings(log_dir=self.shared)
        logging_utils.reset_for_tests()
        logging_utils.get_logger("logtest").info("共有に書く行")
        status = logging_utils.status()
        self.assertFalse(status["is_local"])
        name = os.path.basename(status["file"])
        self.assertEqual(name, f"inspection_{date.today():%Y%m%d}_{logging_utils.owner_tag()}.log")
        self.assertIn("共有に書く行", self.last_line(status["file"]))

    def test_設定を変えたら再起動せずに書き出し先が替わり_前後の行が残る(self) -> None:
        log = logging_utils.get_logger("logtest")
        local_file = logging_utils.status()["file"]
        log.info("替える前")
        logging_utils.apply_settings(self.shared, 30)
        log.info("替えたあと")
        status = logging_utils.status()
        self.assertEqual(status["keep_days"], 30)
        with open(local_file, encoding="utf-8") as fp:
            local = fp.read()
        with open(status["file"], encoding="utf-8") as fp:
            shared = fp.read()
        self.assertIn("ログの保存先を替えます", local)            # 前の書き出し先から辿れる
        self.assertIn("(前: ", shared)                            # 新しいほうから前を辿れる
        self.assertIn("替えたあと", shared)
        self.assertNotIn("替えたあと", local)

    def test_相対パスは使わない(self) -> None:
        self.assertIn("フルパス", logging_utils.check_dir("logs"))

    def test_書けないフォルダはローカルに退避して理由を出す(self) -> None:
        blocker = os.path.join(self._tmp.name, "ファイル")
        with open(blocker, "w") as fp:
            fp.write("x")
        logging_utils.apply_settings(os.path.join(blocker, "ログ"))   # ファイルの下には作れない
        status = logging_utils.status()
        self.assertTrue(status["is_local"])
        self.assertTrue(status["fallback_reason"])
        logging_utils.get_logger("logtest").info("退避中も書く")
        self.assertIn("退避中も書く", self.last_line(status["file"]))

    def test_書いている途中で共有が切れたらローカルへ切り替えて書き続け_戻れば戻る(self) -> None:
        log = logging_utils.get_logger("logtest")
        logging_utils.apply_settings(self.shared)
        log.info("共有に1行")
        # 共有が切れた(フォルダが消え、同じ名前のファイルに塞がれた)
        for handler in logging.getLogger(logging_utils.ROOT_LOGGER).handlers:
            if isinstance(handler, logging_utils.DailyFileHandler):
                handler.close()
        shutil.rmtree(self.shared)
        with open(self.shared, "w") as fp:
            fp.write("x")
        log.info("切れたあとの行")
        status = logging_utils.status()
        self.assertTrue(status["is_local"])
        self.assertTrue(status["fallback_reason"])
        with open(status["file"], encoding="utf-8") as fp:
            text = fp.read()
        self.assertIn("切れたあとの行", text)
        self.assertIn("ローカルに切り替えました", text)
        # 共有が戻った
        os.remove(self.shared)
        logging_utils._target.retry_at = 0
        log.info("戻ったあとの行")
        status = logging_utils.status()
        self.assertFalse(status["is_local"])
        self.assertIn("戻ったあとの行", self.last_line(status["file"]))

    def test_古いログは自分の端末のものだけ消す(self) -> None:
        logging_utils.apply_settings(self.shared, 7)
        old = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")
        new = date.today().strftime("%Y%m%d")
        own = logging_utils.owner_tag()
        names = {
            f"inspection_{old}_{own}.log": True,          # 自分・古い → 消す
            f"events_{old}_{own}.jsonl": True,
            f"inspection_{old}_LINE9-PC_someone.log": False,   # ほかの端末 → 触らない
            f"inspection_{new}_{own}.log": False,          # 新しい → 残す
            "メモ.txt": False,                              # ログではない → 触らない
        }
        for name in names:
            with open(os.path.join(self.shared, name), "w") as fp:
                fp.write("x")
        local_old = os.path.join(str(app_config.local_dir("logs")), f"inspection_{old}.log")
        with open(local_old, "w") as fp:
            fp.write("x")
        removed = logging_utils.prune()
        for name, gone in names.items():
            self.assertEqual(not os.path.exists(os.path.join(self.shared, name)), gone, name)
        self.assertFalse(os.path.exists(local_old))
        self.assertIn(f"inspection_{old}.log", removed)

    def test_保存日数は範囲に収める(self) -> None:
        self.assertEqual(logging_utils.keep_days("abc"), logging_utils.DEFAULT_KEEP_DAYS)
        self.assertEqual(logging_utils.keep_days(1), logging_utils.MIN_KEEP_DAYS)
        self.assertEqual(logging_utils.keep_days(99999), logging_utils.MAX_KEEP_DAYS)


class EventLogTest(_Base):
    def setUp(self) -> None:
        super().setUp()
        event_log.set_hints(lambda code: ("Excel が印刷の命令に失敗した", ["プリンターの電源"])
                            if code == "PRINT_FAILED" else ("", []))
        self.addCleanup(event_log.set_hints, lambda code: ("", []))

    def events(self):
        path = logging_utils.events_path_for(date.today())
        with open(path, encoding="utf-8") as fp:
            return [json.loads(line) for line in fp]

    def test_問い合わせ番号は読み上げやすい形(self) -> None:
        ref = event_log.new_ref(datetime(2026, 10, 1, 15, 2))
        self.assertRegex(ref, r"^1001-1502-[2-9A-HJKMNP-Z]{4}$")

    def test_1行に1つの出来事を残し_なぜの段と確かめることを添える(self) -> None:
        ref = event_log.record("print.item", event_log.NG, code="PRINT_FAILED", message="印刷に失敗しました。",
                               detail="0x800A03EC プリンターが見つかりません", target="点検表A", printer="P1")
        entry = self.events()[-1]
        self.assertEqual(entry["ref"], ref)
        for key in ("ts", "pc", "user", "ver", "pid"):
            self.assertTrue(entry[key], key)
        self.assertEqual(entry["why"], ["印刷に失敗しました。", "Excel が印刷の命令に失敗した(PRINT_FAILED)",
                                        "0x800A03EC プリンターが見つかりません"])
        self.assertEqual(entry["checks"], ["プリンターの電源"])
        self.assertEqual(entry["target"], "点検表A")
        # テキストのログにも同じ番号で残る(どちらからでも引ける)
        self.assertIn(f"[ref={ref}] print.item ng PRINT_FAILED",
                      self.last_line(logging_utils.status()["file"]))

    def test_要求の番号があればそれを使う(self) -> None:
        token = event_log.current_ref.set("1001-0000-AAAA")
        try:
            self.assertEqual(event_log.record("preview", event_log.OK), "1001-0000-AAAA")
        finally:
            event_log.current_ref.reset(token)

    def test_番号で探すとほかの端末の記録も見つかる_一覧は自分の端末だけ(self) -> None:
        logging_utils.apply_settings(self.shared)
        mine = event_log.record("preview", event_log.NG, code="OPEN_FAILED", message="開けません")
        other = {"ts": datetime.now().isoformat(timespec="milliseconds"), "ref": "1001-0000-OTHR",
                 "pc": "LINE9-PC", "user": "u", "pid": 1, "op": "print.item", "result": "ng"}
        with open(os.path.join(self.shared, f"events_{date.today():%Y%m%d}_LINE9-PC_u.jsonl"), "w",
                  encoding="utf-8") as fp:
            fp.write(json.dumps(other) + "\n")
        refs = [e["ref"] for e in event_log.read()]
        self.assertIn(mine, refs)
        self.assertNotIn("1001-0000-OTHR", refs)
        self.assertEqual(event_log.read(ref="1001-0000-OTHR")[0]["pc"], "LINE9-PC")

    def test_直前に起きていたことを古い順に返す(self) -> None:
        event_log.record("startup", event_log.OK)
        event_log.record("preview", event_log.OK, target="A")
        ref = event_log.record("print.item", event_log.NG, code="PRINT_FAILED")
        entry = event_log.read(ref=ref)[0]
        ops = [e["op"] for e in event_log.timeline(entry)]
        self.assertEqual(ops[-3:], ["startup", "preview", "print.item"])

    def test_長すぎる値は切る(self) -> None:
        event_log.record("excel.job", event_log.NG, detail="x" * 10000)
        self.assertLess(len(self.events()[-1]["detail"]), 4100)


@unittest.skipUnless(__import__("importlib").util.find_spec("flask"), "Flask が無い")
class LogApiTest(_Base):
    def setUp(self) -> None:
        super().setUp()
        from app import create_app
        from core import idle_exit
        idle_exit.reset()
        self.addCleanup(idle_exit.reset)
        self.app = create_app(token="t", port=8799, options={"demo": True})
        self.biz = self.app.extensions["inspection"]
        self.biz.excel.backend.print_delay = 0
        self.biz.excel.backend.preview_delay = 0
        self.biz.inspection.start_scan("test")
        self.biz.inspection.wait(10)
        self.app.config["READY"] = True
        self.client = self.app.test_client()
        self.addCleanup(self.biz.settings.put, logging_utils.KEY_DIR, None)
        self.addCleanup(self.biz.printing.wait_idle, 10)

    def get(self, path, **kw):
        return self.client.get(path, headers={"X-Tool-Token": "t", "X-Screen-Id": "s1"}, **kw)

    def post(self, path, json=None):
        return self.client.post(path, json=json or {}, headers={"X-Tool-Token": "t", "X-Screen-Id": "s1"})

    def test_断りには問い合わせ番号が付き_ログから引ける(self) -> None:
        res = self.post("/api/preview", {"id": "no-such-item"})
        self.assertEqual(res.status_code, 409)
        ref = res.get_json()["error"]["ref"]
        self.assertEqual(res.headers["X-Request-Ref"], ref)
        found = self.get(f"/api/logs/find?ref={ref.lower()}").get_json()
        self.assertEqual(found["events"][0]["code"], "ITEM_NOT_FOUND")
        self.assertEqual(found["events"][0]["screen"], "s1")

    def test_業務の断りに対象とファイルを添える(self) -> None:
        inv = self.get("/api/inventory").get_json()["inventory"]
        item = inv["categories"][0]["subcategories"][0]["items"][0]
        path = self.biz.inspection.get_item(item["id"]).path
        os.rename(path, path + ".moved")
        self.addCleanup(os.rename, path + ".moved", path)
        ref = self.post("/api/preview", {"id": item["id"]}).get_json()["error"]["ref"]
        entry = event_log.read(ref=ref)[0]
        self.assertEqual(entry["code"], "FILE_NOT_FOUND")
        self.assertEqual(entry["file"], path)
        self.assertIn(item["name"], entry["target"])
        self.assertTrue(entry["checks"])                 # app/why_hints.py から

    def test_印刷は開始から1件ずつ結果まで同じ番号で残る(self) -> None:
        inv = self.get("/api/inventory").get_json()["inventory"]
        ids = [it["id"] for c in inv["categories"] for s in c["subcategories"] for it in s["items"]
               if "エラー" in it["name"]][:1]
        ids += [inv["categories"][0]["subcategories"][0]["items"][0]["id"]]
        job = self.post("/api/print", {"ids": ids, "copies": 1}).get_json()["job"]
        self.assertTrue(self.biz.printing.wait_idle(10))
        ops = [(e["op"], e["result"]) for e in reversed(event_log.read(ref=job["ref"]))]   # 古い順
        self.assertEqual(ops[0], ("print.start", "info"))
        self.assertIn(("print.item", "ng"), ops)
        self.assertIn(("print.item", "ok"), ops)
        self.assertEqual(ops[-1], ("print.end", "ng"))

    def test_別オリジンの要求も断った記録が残る(self) -> None:
        res = self.client.get("/api/inventory", headers={"Sec-Fetch-Site": "cross-site", "X-Tool-Token": "t"})
        self.assertEqual(res.status_code, 403)
        entry = event_log.read(ref=res.get_json()["error"]["ref"])[0]
        self.assertEqual(entry["code"], "cross_origin")
        self.assertEqual(entry["result"], "refused")

    def test_保存先を画面から変える_書けないフォルダは保存しない(self) -> None:
        bad = self.post("/api/settings/logs", {"log_dir": "relative"})
        self.assertEqual(bad.status_code, 422)
        self.assertIsNone(self.biz.settings.get(logging_utils.KEY_DIR))
        ok = self.post("/api/settings/logs", {"log_dir": self.shared, "keep_days": 30}).get_json()
        self.assertEqual(ok["logs"]["dir"], os.path.normpath(self.shared))
        self.assertEqual(ok["logs"]["keep_days"], 30)
        self.assertEqual(self.biz.settings.get(logging_utils.KEY_DIR), os.path.normpath(self.shared))
        back = self.post("/api/settings/logs", {"reset_log_dir": True}).get_json()
        self.assertTrue(back["logs"]["is_local"])

    def test_保存日数の範囲(self) -> None:
        self.assertEqual(self.post("/api/settings/logs", {"keep_days": 1}).status_code, 400)

    def test_画面のエラーを残す_送りすぎは捨てる(self) -> None:
        res = self.post("/api/client-log", {"kind": "js_error", "message": "x is undefined", "detail": "app.js:1"})
        ref = res.get_json()["ref"]
        self.assertEqual(event_log.read(ref=ref)[0]["op"], "client.js_error")
        self.assertEqual(self.post("/api/client-log", {"kind": "evil"}).status_code, 400)
        from app.routes import logs
        for _ in range(logs.CLIENT_LIMIT):
            self.post("/api/client-log", {"kind": "offline", "message": "m"})
        self.assertFalse(self.post("/api/client-log", {"kind": "offline"}).get_json()["recorded"])

    def test_最近のエラー一覧(self) -> None:
        self.post("/api/preview", {"id": "no-such-item"})
        data = self.get("/api/logs").get_json()
        self.assertTrue(data["recent"])
        self.assertTrue(all(e["result"] in ("ng", "refused") for e in data["recent"]))
        self.assertIn("dir", data["logs"])

    def test_番号が無ければ理由を返す(self) -> None:
        res = self.get("/api/logs/find?ref=0101-0000-ZZZZ")
        self.assertEqual(res.status_code, 404)
        self.assertIn("共有フォルダ", res.get_json()["error"]["detail"])


class DistributionHasLogItemsTest(unittest.TestCase):
    def test_ログの保存先と日数を配れる(self) -> None:
        from app.services import distribution
        self.assertIn("log_dir", distribution.ITEM_KEYS)
        self.assertIn("log_keep_days", distribution.ITEM_KEYS)
        self.assertTrue(distribution.ITEM_CHECKS["log_keep_days"](30))
        self.assertFalse(distribution.ITEM_CHECKS["log_keep_days"](True))


if __name__ == "__main__":
    unittest.main()
