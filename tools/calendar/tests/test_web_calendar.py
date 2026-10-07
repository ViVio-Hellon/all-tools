"""カレンダー画面の API

**tkinter 版の試験(test_ui.py)が確かめていたことを引き継ぐ。**
あちらはウィジェットの中身を見ていたが、こちらは同じ判断を
ビューモデルの側で確かめる ── 判断はサーバに移したので、
確かめる場所もサーバ側になる。
"""

from __future__ import annotations

import datetime as _dt
import unittest

from . import _web
from calendar_app import config, repository, settings as user_settings

D = "2026/08/03"


def _add_member(conn, code, name, group, line):
    conn.execute(
        f'INSERT INTO "{config.TABLE_MEMBER}" '
        '("管理番号","苗字","班","名前","読み","担当ライン") VALUES (?,?,?,?,?,?)',
        (code, name, group, name, name, line))
    conn.commit()


class MonthViewTests(unittest.TestCase):
    """月のビューモデル (tkinter 版 show_month)。"""

    def setUp(self) -> None:
        _web.reset_sync()
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()

    def test_42セル返る(self) -> None:
        body = _web.json_of(self.client.get("/api/calendar?year=2026&month=8",
                                            headers=_web.auth()))
        self.assertEqual(len(body["cells"]), 42)

    def test_先頭は日曜(self) -> None:
        """VBA と同じく、1日を含む週の日曜から始まる。"""
        body = _web.json_of(self.client.get("/api/calendar?year=2026&month=8",
                                            headers=_web.auth()))
        first = _dt.datetime.strptime(body["cells"][0]["date"], "%Y/%m/%d").date()
        self.assertEqual(first.weekday(), 6)      # 6 = 日曜

    def test_日付が連続している(self) -> None:
        body = _web.json_of(self.client.get("/api/calendar?year=2026&month=8",
                                            headers=_web.auth()))
        days = [_dt.datetime.strptime(c["date"], "%Y/%m/%d").date()
                for c in body["cells"]]
        for before, after in zip(days, days[1:]):
            self.assertEqual((after - before).days, 1)

    def test_前後月は押せず中身も出さない(self) -> None:
        body = _web.json_of(self.client.get("/api/calendar?year=2026&month=8",
                                            headers=_web.auth()))
        out = [c for c in body["cells"] if not c["in_month"]]
        self.assertTrue(out)
        for cell in out:
            self.assertEqual(cell["tone"], "out")
            self.assertEqual(cell["body"], "")

    def test_曜日の色分け(self) -> None:
        """日曜・祝日は赤系、土曜は青系(VBA から変えない)。"""
        body = _web.json_of(self.client.get("/api/calendar?year=2026&month=8",
                                            headers=_web.auth()))
        tones = {}
        for cell in body["cells"]:
            if not cell["in_month"]:
                continue
            date = _dt.datetime.strptime(cell["date"], "%Y/%m/%d").date()
            tones.setdefault(date.weekday(), set()).add(cell["tone"])
        self.assertEqual(tones[6], {"holiday"})            # 日曜
        self.assertEqual(tones[5], {"sat"})                # 土曜
        self.assertTrue(tones[2] <= {"weekday", "holiday"})  # 水曜(祝日はありうる)

    def test_祝日名が出る(self) -> None:
        """2026/08/11 は山の日。"""
        body = _web.json_of(self.client.get("/api/calendar?year=2026&month=8",
                                            headers=_web.auth()))
        cell = next(c for c in body["cells"] if c["date"] == "2026/08/11")
        self.assertEqual(cell["holiday"], "山の日")
        self.assertEqual(cell["tone"], "holiday")

    def test_当日に印がつく(self) -> None:
        today = _dt.date.today()
        body = _web.json_of(self.client.get(
            f"/api/calendar?year={today.year}&month={today.month}",
            headers=_web.auth()))
        marked = [c for c in body["cells"] if c["today"]]
        self.assertEqual(len(marked), 1)
        self.assertEqual(marked[0]["date"], today.strftime("%Y/%m/%d"))

    def test_月をまたぐ移動(self) -> None:
        body = _web.json_of(self.client.get("/api/calendar?year=2026&month=13",
                                            headers=_web.auth()))
        self.assertEqual((body["year"], body["month"]), (2027, 1))

    def test_登録した内容がセルに出る(self) -> None:
        _web.with_source(self)
        _add_member(self.conn, "10", "山田太郎", "B", "L-1")
        user_settings.save_my_line("L-1")
        self.client.post("/api/rest", json={"date": D, "code": "10", "shift": "1"},
                         headers=_web.auth())
        body = _web.json_of(self.client.get("/api/calendar?year=2026&month=8",
                                            headers=_web.auth()))
        cell = next(c for c in body["cells"] if c["date"] == D)
        self.assertIn("山田太郎", cell["body"])
        self.assertEqual(cell["count"], 1)

    def test_班員名簿が無ければ案内が出る(self) -> None:
        body = _web.json_of(self.client.get("/api/calendar", headers=_web.auth()))
        self.assertIn("班員名簿", body["notice"])


class RestTests(unittest.TestCase):
    """休みの登録 (tkinter 版 _register_rest)。"""

    def setUp(self) -> None:
        _web.with_source(self)          # 書き先が無いと登録は断られる
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()
        _add_member(self.conn, "10", "山田太郎", "B", "L-1")   # 3交替
        _add_member(self.conn, "20", "佐藤花子", "昼勤", "L-1")  # 日勤
        user_settings.save_my_line("L-1")

    def post(self, **body):
        return self.client.post("/api/rest", json=body, headers=_web.auth())

    def test_登録できる(self) -> None:
        res = self.post(date=D, code="10", shift="1")
        self.assertEqual(res.status_code, 200)
        self.assertIn("山田太郎", res.get_json()["message"])

    def test_3交替は直が要る(self) -> None:
        res = self.post(date=D, code="10")
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"], "shift_required")

    def test_日勤は直を聞かない(self) -> None:
        """A/B/C/D 以外は3交替に属さないので、直なしで登録できる(VBA と同じ)。"""
        res = self.post(date=D, code="20")
        self.assertEqual(res.status_code, 200)

    def test_日勤には直を保存しない(self) -> None:
        self.post(date=D, code="20", shift="2")
        row = self.conn.execute(
            f'SELECT "直" FROM "{config.TABLE_DATA}" WHERE "識別コード"=?', ("20",)
        ).fetchone()
        self.assertEqual(row["直"], "")

    def test_二重登録を断る(self) -> None:
        self.post(date=D, code="10", shift="1")
        res = self.post(date=D, code="10", shift="1")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()["error"]["code"], "already_registered")

    def test_一覧に無い作業者は選べない(self) -> None:
        """画面で絞ってあっても、サーバでも確かめる。"""
        res = self.post(date=D, code="9999", shift="1")
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"], "not_listed")

    def test_繋ぎ未選択は未登録で保存(self) -> None:
        self.post(date=D, code="10", shift="1", overtime="", early="")
        row = self.conn.execute(
            f'SELECT "残業者","早出者" FROM "{config.TABLE_DATA}" '
            'WHERE "識別コード"=?', ("10",)).fetchone()
        self.assertEqual(row["残業者"], config.UNREGISTERED)
        self.assertEqual(row["早出者"], config.UNREGISTERED)

    def test_日付の形が違えば400(self) -> None:
        res = self.post(date="2026-08-03", code="10", shift="1")
        self.assertEqual(res.status_code, 400)

    def test_登録後は更新後の月が返る(self) -> None:
        """画面は返ってきたものを描き直すだけでよい(差分を当てない)。"""
        body = self.post(date=D, code="10", shift="1").get_json()
        self.assertEqual(len(body["cells"]), 42)
        self.assertIn("sync", body)
        cell = next(c for c in body["cells"] if c["date"] == D)
        self.assertIn("山田太郎", cell["body"])


class CommentTests(unittest.TestCase):
    """コメント・連絡 (tkinter 版 _register_comment)。"""

    def setUp(self) -> None:
        _web.with_source(self)          # 書き先が無いと登録は断られる
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()
        _add_member(self.conn, "10", "山田太郎", "B", "L-1")
        user_settings.save_my_line("L-1")

    def test_登録できる(self) -> None:
        res = self.client.post("/api/comment",
                               json={"date": D, "line": "L-1", "group": "B",
                                     "text": "工程変更あり"},
                               headers=_web.auth())
        self.assertEqual(res.status_code, 200)

    def test_本文が空なら断る(self) -> None:
        res = self.client.post("/api/comment",
                               json={"date": D, "line": "L-1", "group": "B",
                                     "text": "   "},
                               headers=_web.auth())
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"], "empty_text")

    def test_実在しない組み合わせは断る(self) -> None:
        res = self.client.post("/api/comment",
                               json={"date": D, "line": "コイル", "group": "Z",
                                     "text": "test"},
                               headers=_web.auth())
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"], "not_listed")

    def test_宛先は実在する組み合わせだけ(self) -> None:
        body = _web.json_of(self.client.get("/api/comment-targets",
                                            headers=_web.auth()))
        self.assertEqual(body["lines"], ["L-1"])
        self.assertEqual(body["groups"]["L-1"], ["B"])


class DayViewTests(unittest.TestCase):
    """1日の内容 (tkinter 版 ViewerDialog / DeletePickerDialog)。"""

    def setUp(self) -> None:
        _web.with_source(self)          # 書き先が無いと登録は断られる
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()
        _add_member(self.conn, "10", "山田太郎", "B", "L-1")
        user_settings.save_my_line("L-1")
        self.client.post("/api/rest", json={"date": D, "code": "10", "shift": "1"},
                         headers=_web.auth())

    def test_閲覧できる(self) -> None:
        body = _web.json_of(self.client.get(f"/api/day/{D}", headers=_web.auth()))
        self.assertIn("山田太郎", body["detail"])
        self.assertEqual(len(body["items"]), 1)

    def test_削除の選択肢に区分が出る(self) -> None:
        body = _web.json_of(self.client.get(f"/api/day/{D}", headers=_web.auth()))
        item = body["items"][0]
        self.assertEqual(item["kind"], "rest")
        self.assertIn("[休み]", item["caption"])

    def test_登録が無い日は空(self) -> None:
        body = _web.json_of(self.client.get("/api/day/2026/08/04",
                                            headers=_web.auth()))
        self.assertEqual(body["items"], [])

    def test_日付の形が違えば400(self) -> None:
        res = self.client.get("/api/day/not-a-date", headers=_web.auth())
        self.assertEqual(res.status_code, 400)


class DeleteTests(unittest.TestCase):
    """削除 (tkinter 版 _delete_day)。"""

    def setUp(self) -> None:
        _web.with_source(self)          # 書き先が無いと登録は断られる
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()
        _add_member(self.conn, "10", "山田太郎", "B", "L-1")
        user_settings.save_my_line("L-1")
        self.client.post("/api/rest", json={"date": D, "code": "10", "shift": "1"},
                         headers=_web.auth())
        self.record_id = _web.json_of(
            self.client.get(f"/api/day/{D}", headers=_web.auth()))["items"][0]["id"]

    def test_削除できる(self) -> None:
        res = self.client.post("/api/delete", json={"date": D, "ids": [self.record_id]},
                               headers=_web.auth())
        self.assertEqual(res.status_code, 200)
        remains = _web.json_of(self.client.get(f"/api/day/{D}", headers=_web.auth()))
        self.assertEqual(remains["items"], [])

    def test_履歴が残る(self) -> None:
        self.client.post("/api/delete", json={"date": D, "ids": [self.record_id]},
                         headers=_web.auth())
        row = self.conn.execute(
            f'SELECT COUNT(*) AS cnt FROM "{config.TABLE_DEL_HISTORY}"').fetchone()
        self.assertEqual(row["cnt"], 1)

    def test_別の日のIDは通さない(self) -> None:
        """その日の行だけを消す。混ぜて投げられても通らない。"""
        res = self.client.post("/api/delete",
                               json={"date": "2026/08/04", "ids": [self.record_id]},
                               headers=_web.auth())
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()["error"]["code"], "gone")

    def test_空の指定は400(self) -> None:
        res = self.client.post("/api/delete", json={"date": D, "ids": []},
                               headers=_web.auth())
        self.assertEqual(res.status_code, 400)


class StaleDeleteTests(unittest.TestCase):
    """開いたままのダイアログが**別の行を指していないか**

    【なぜ要るか】
    削除のダイアログは開いたまま放置される。そのあいだに背景の取り込み
    直しが走ると、同じ ID が別の行になっていることがある ── 取り込み元の
    ``ID`` はオートインクリメントではないので、**消えた番号は再利用される**。

    ID だけで消すと、最悪「別の人の休みを消して、履歴にもその人が残る」。
    押した人には気づく手立てが無いので、ここで止める。
    """

    def setUp(self) -> None:
        _web.with_source(self)
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()
        _add_member(self.conn, "10", "山田太郎", "B", "L-1")
        _add_member(self.conn, "20", "佐藤次郎", "B", "L-1")
        user_settings.save_my_line("L-1")
        self.client.post("/api/rest", json={"date": D, "code": "10", "shift": "1"},
                         headers=_web.auth())
        self.item = _web.json_of(
            self.client.get(f"/api/day/{D}", headers=_web.auth()))["items"][0]

    def delete(self, **body):
        return self.client.post("/api/delete",
                                json={"date": D, **body}, headers=_web.auth())

    def test_印が付いて返る(self) -> None:
        """画面はこれを**そのまま返すだけ**でよい。"""
        self.assertIn("mark", self.item)
        self.assertTrue(self.item["mark"])

    def test_合っていれば消せる(self) -> None:
        res = self.delete(ids=[self.item["id"]],
                          marks={str(self.item["id"]): self.item["mark"]})
        self.assertEqual(res.status_code, 200)

    def test_中身が変わっていたら断る(self) -> None:
        """同じIDが別の人になっていた、という状況。"""
        record_id = self.item["id"]
        self.conn.execute(
            f'UPDATE "{config.TABLE_DATA}" SET "登録内容"=?, "識別コード"=? '
            'WHERE "ID"=?', ("佐藤次郎", "20", record_id))
        self.conn.commit()

        res = self.delete(ids=[record_id],
                          marks={str(record_id): self.item["mark"]})
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()["error"]["code"], "gone")

    def test_断ったなら消していない(self) -> None:
        record_id = self.item["id"]
        self.conn.execute(
            f'UPDATE "{config.TABLE_DATA}" SET "登録内容"=? WHERE "ID"=?',
            ("佐藤次郎", record_id))
        self.conn.commit()
        self.delete(ids=[record_id], marks={str(record_id): self.item["mark"]})

        row = self.conn.execute(
            f'SELECT COUNT(*) AS cnt FROM "{config.TABLE_DATA}"').fetchone()
        self.assertEqual(row["cnt"], 1)

    def test_印が無ければ今までどおり(self) -> None:
        """コマンドから消す経路(印を持たない)は塞がない。"""
        res = self.delete(ids=[self.item["id"]])
        self.assertEqual(res.status_code, 200)

    def test_印の形が変なら400(self) -> None:
        res = self.delete(ids=[self.item["id"]], marks={"あ": "x"})
        self.assertEqual(res.status_code, 400)


class SourceUnreachableAtStartTests(unittest.TestCase):
    """参照パスはあるが、**起動したときに共有が見えなかった**。

    以前は起動時に1回探すだけで、見つからなければそのあいだずっと「参照パス
    未設定」扱いだった。登録を断り(「参照パスで指定してください」)、共有が
    戻っても直らず、設定を保存し直すか起動し直すまで続いた。
    """

    def setUp(self) -> None:
        import shutil
        import tempfile
        from pathlib import Path

        from calendar_app import sync_service
        from calendar_app.dbkit import outbox_sync

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.share = Path(tmp.name) / "共有"
        self.share.mkdir()
        _web.make_source(self.share, config.SOURCE_FILE_DATA, _web.SOURCE_SCHEMA)
        user_settings.set_value(user_settings.KEY_DATA_DB_DIR, str(self.share))
        user_settings.set_value(user_settings.KEY_MASTER_DB_DIR, str(self.share))
        self.addCleanup(user_settings.set_value, user_settings.KEY_DATA_DB_DIR, "")
        self.addCleanup(user_settings.set_value, user_settings.KEY_MASTER_DB_DIR, "")
        outbox_sync.reset_op_id_cache()
        self.addCleanup(outbox_sync.reset_op_id_cache)
        # 送信待ちは同期側も読むので、手元のDBは本物のファイルにする
        from unittest import mock

        from calendar_app import db

        local = Path(tmp.name) / "calendar.db"
        patcher = mock.patch.object(config, "sqlite_path", return_value=local)
        patcher.start()
        self.addCleanup(patcher.stop)
        # 共有が落ちた状態で起動する
        self.hidden = Path(tmp.name) / "落ちている"
        shutil.move(str(self.share), str(self.hidden))
        sync_service.reset()
        self.service = sync_service.get_service()
        self.addCleanup(sync_service.reset)
        self.conn = _web.bind_db(self, db.connect(local))
        self.client = _web.make_client()
        _add_member(self.conn, "10", "山田太郎", "B", "L-1")
        user_settings.save_my_line("L-1")

    def back(self) -> None:
        import shutil

        shutil.move(str(self.hidden), str(self.share))

    def test_登録は断らずに預かる(self) -> None:
        self.assertTrue(self.service.configured)
        self.assertFalse(self.service.reachable)
        res = self.client.post("/api/comment",
                               json={"date": D, "line": "L-1", "group": "B",
                                     "text": "共有が落ちている間"},
                               headers=_web.auth())
        self.assertEqual(res.status_code, 200, res.get_json())
        status = self.service.status()
        self.assertEqual(status.state.name, "OFFLINE")
        self.assertIn("見えません", status.message)
        self.assertEqual(status.pending, 1)

    def test_共有が戻れば見つけ直して送る(self) -> None:
        self.client.post("/api/comment",
                         json={"date": D, "line": "L-1", "group": "B", "text": "戻ったら送る"},
                         headers=_web.auth())
        self.assertFalse(self.service.request(receive=False), "見えないのに送り始めた")
        self.back()
        res = self.client.post("/api/sync/now", headers=_web.auth())
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertTrue(self.service.reachable)
        self.assertEqual(self.service.status().pending, 0)
        rows = _web.read_source(self.share / config.SOURCE_FILE_DATA, config.TABLE_DATA)
        self.assertIn("戻ったら送る", [r["登録内容"] for r in rows])

    def test_設定画面は登録できないとは言わない(self) -> None:
        body = self.client.get("/api/settings", headers=_web.auth()).get_json()
        text = "\n".join(body.get("problems", []))
        self.assertIn("いま見えません", text)
        self.assertNotIn("登録はできません", text)


class NoSourceTests(unittest.TestCase):
    """参照パスが無ければ**登録できない**。

    正式なデータは共有 Access だけで、手元の SQLite は写しと送信待ちの
    置き場にすぎない。送り先が決まっていないのに登録を受けると、入力は
    この端末に溜まり続け、他のラインには永遠に届かない。
    **「保存できたつもり」が一番たちが悪い**ので、入り口で断る。
    """

    def setUp(self) -> None:
        _web.reset_sync()               # 参照パスを立てない
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()
        _add_member(self.conn, "10", "山田太郎", "B", "L-1")
        user_settings.save_my_line("L-1")

    def test_休みを断る(self) -> None:
        res = self.client.post("/api/rest",
                               json={"date": D, "code": "10", "shift": "1"},
                               headers=_web.auth())
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"], "no_source")

    def test_連絡を断る(self) -> None:
        res = self.client.post("/api/comment",
                               json={"date": D, "line": "L-1", "group": "B",
                                     "text": "test"},
                               headers=_web.auth())
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"], "no_source")

    def test_削除を断る(self) -> None:
        res = self.client.post("/api/delete", json={"date": D, "ids": [1]},
                               headers=_web.auth())
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"], "no_source")

    def test_断り方が入力検査より先(self) -> None:
        """**書き先が無いことを先に言う。** 入力の直し方を案内しても、
        直したところで登録できないので意味がない。"""
        res = self.client.post("/api/rest", json={}, headers=_web.auth())
        self.assertEqual(res.get_json()["error"]["code"], "no_source")

    def test_読み取りは断らない(self) -> None:
        """見ることはできる。書き先が無いことと、読めないことは別。"""
        self.assertEqual(
            self.client.get("/api/calendar", headers=_web.auth()).status_code, 200)
        self.assertEqual(
            self.client.get(f"/api/day/{D}", headers=_web.auth()).status_code, 200)
        self.assertEqual(
            self.client.get("/api/workers", headers=_web.auth()).status_code, 200)


class WorkerOptionTests(unittest.TestCase):
    """作業者の選択肢 (tkinter 版 WorkerPickerDialog)。"""

    def setUp(self) -> None:
        _web.reset_sync()
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()

    def test_ラインと班で束ねて返る(self) -> None:
        _add_member(self.conn, "10", "山田太郎", "B", "L-1")
        _add_member(self.conn, "11", "鈴木一郎", "A", "L-1")
        user_settings.save_my_line("L-1")

        body = _web.json_of(self.client.get("/api/workers", headers=_web.auth()))
        self.assertEqual(body["total"], 2)
        groups = {g["group"] for line in body["lines"] for g in line["groups"]}
        self.assertEqual(groups, {"A", "B"})

    def test_直を聞くかどうかをサーバが決める(self) -> None:
        _add_member(self.conn, "10", "山田太郎", "B", "L-1")     # 3交替
        _add_member(self.conn, "20", "佐藤花子", "昼勤", "L-1")  # 日勤
        user_settings.save_my_line("L-1")

        body = _web.json_of(self.client.get("/api/workers", headers=_web.auth()))
        flags = {w["code"]: w["needs_shift"]
                 for line in body["lines"] for g in line["groups"] for w in g["workers"]}
        self.assertTrue(flags["10"])
        self.assertFalse(flags["20"])

    def test_どの直が繋ぐのかも返す(self) -> None:
        """**繋ぎを続けて2回聞くので、どちらを聞いているか名乗れるように。**

        休んだ直の**前が残業**で、**次が早出**。画面が数えると
        閲覧・ツールチップと言い方がずれるので、サーバが渡す。
        """
        body = _web.json_of(self.client.get("/api/workers", headers=_web.auth()))
        self.assertEqual(body["shift_links"]["2"], {"overtime": "1", "early": "3"})

    def test_直は輪になっている(self) -> None:
        """1直の前は3直、3直の次は1直(``shift_neighbours`` と同じ)。"""
        body = _web.json_of(self.client.get("/api/workers", headers=_web.auth()))
        self.assertEqual(body["shift_links"]["1"]["overtime"], "3")
        self.assertEqual(body["shift_links"]["3"]["early"], "1")

    def test_閲覧と同じ言い方になる(self) -> None:
        """**同じ登録が2通りに見えない。** 求め方を1つにしてある。"""
        for shift in ("1", "2", "3"):
            prev, nxt = repository.shift_neighbours(shift)
            link = _web.json_of(
                self.client.get("/api/workers",
                                headers=_web.auth()))["shift_links"][shift]
            self.assertEqual((link["overtime"], link["early"]),
                             (str(prev), str(nxt)))


class PrintTests(unittest.TestCase):
    """印刷 (tkinter 版 on_print)。"""

    def setUp(self) -> None:
        _web.reset_sync()
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()

    def test_HTMLが返る(self) -> None:
        res = self.client.get("/print?year=2026&month=8", headers=_web.auth())
        self.assertEqual(res.status_code, 200)
        text = res.get_data(as_text=True)
        self.assertIn("2026年 8月", text)
        self.assertIn("A4 landscape", text)

    def test_トークンが要る(self) -> None:
        """業務データを含むので、素通しにしない。"""
        self.assertEqual(self.client.get("/print").status_code, 403)


if __name__ == "__main__":
    unittest.main()
