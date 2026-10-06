"""開けっぱなしにしたときに、古い表示のままにならないか

【なぜまとめて見るか】
このツールは朝に開いて一日中そのまま、という使い方をされる。
そのとき起きることを1か所に集めてある。

1. **取り込み直したら描き直せること**(A)。背景の同期は他ラインの登録を
   手元へ入れるが、画面がそれを知る手立てが無いと、帯だけが「同期済み」と
   言う嘘の状態になる ── 古い画面を最新だと読ませるのが一番たちが悪い
2. **日付が変わったら分かること**(B)。翌朝も昨日のセルに枠が付いたまま
   にしない
3. **どのプロセスが出した画面か分かること**(D)。起動し直されると起動
   トークンが変わるので、古いタブは読み込み直さないと何も通らない

判断そのものは画面側(``sync.js`` / ``health.js``)が持つが、**根拠は
すべてサーバが返す**。ここではその根拠が返ることを固定する。
"""

from __future__ import annotations

import datetime as _dt
import os
import unittest

from . import _web
from calendar_app import config


class SyncSignalTests(unittest.TestCase):
    """``GET /api/sync`` が描き直しの根拠を返すか。"""

    def setUp(self) -> None:
        _web.reset_sync()
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()

    def status(self) -> dict:
        return _web.json_of(self.client.get("/api/sync", headers=_web.auth()))

    def test_取り込んだ時刻が返る(self) -> None:
        """これが変わったら、手元の中身が入れ替わったということ。"""
        self.assertIn("last_received_at", self.status())

    def test_今日の日付が返る(self) -> None:
        """端末の時計とずれても、塗る根拠は1つにする。"""
        self.assertEqual(self.status()["today"],
                         _dt.date.today().strftime(config.DATE_KEY_FORMAT))

    def test_取り込んだら時刻が変わる(self) -> None:
        """**本物の同期を1回通して**確かめる。ここが動かないと、画面は
        描き直す時機を知る手立てを失う。"""
        _web.with_source(self)
        before = self.status()["last_received_at"]

        res = self.client.post("/api/sync/now", headers=_web.auth())
        self.assertEqual(res.status_code, 200)

        after = self.status()["last_received_at"]
        self.assertTrue(after)
        self.assertNotEqual(after, before)

    def test_月の応答にも同じものが載る(self) -> None:
        """登録の直後に1拍遅れないよう、月と一緒に返している。"""
        body = _web.json_of(self.client.get("/api/calendar", headers=_web.auth()))
        for key in ("last_received_at", "today"):
            self.assertIn(key, body["sync"])

    def test_軽いままであること(self) -> None:
        """帯が数秒ごとに読むので、増やしすぎない。"""
        self.assertLessEqual(len(self.status()), 12)


class PageIdentityTests(unittest.TestCase):
    """画面が「自分を出したサーバ」を覚えているか。"""

    def setUp(self) -> None:
        _web.reset_sync()
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()

    def test_版とプロセスを持たせる(self) -> None:
        from calendar_app import app_config

        html = self.client.get(f"/calendar?t={_web.TOKEN}").get_data(as_text=True)
        self.assertIn(f'"{app_config.version()}"', html)
        self.assertIn(str(os.getpid()), html)

    def test_健康確認も同じものを返す(self) -> None:
        """見比べる相手なので、両方に無いと判断できない。"""
        body = _web.json_of(self.client.get("/api/health"))
        for key in ("version", "pid"):
            self.assertIn(key, body)


if __name__ == "__main__":
    unittest.main()
