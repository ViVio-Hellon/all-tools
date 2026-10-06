"""画面は1台につき1つだけ ── タブを2枚開かせない

【プロセスの二重起動とは別の話】
起動しているのが1つでも、ブラウザのタブは何枚でも開けます。2枚開くと
**どちらでも登録できて、どちらも「自分が最新」の顔をします。**

* 片方で登録 → もう片方は古いまま
* 設定を両方で開く → 後から保存したほうが黙って勝つ
* 削除のダイアログを開いたまま、もう片方で同じ日を触る

壊れたようには見えず、「どちらが本当か分からない」という形で出ます。

【何を見張るか】
1. 2枚目は**断る**(開いた時点で)
2. 断ったのに書ける、を作らない ── **守りはサーバ側**
3. 同じタブの中の移動(カレンダー↔設定)は2枚目扱いにしない
4. **取って代われる**。前の画面が異常終了しても締め出されない
5. 閉じたら次がすぐ使える
"""

from __future__ import annotations

import time
import unittest

from . import _web
from calendar_app import config, screen_lock, settings as user_settings


class LockTests(unittest.TestCase):
    """判断そのもの(HTTP を通さずに見る)。"""

    def setUp(self) -> None:
        self.lock = screen_lock.ScreenLock()

    def test_1枚目は通る(self) -> None:
        self.assertTrue(self.lock.claim("A").ok)

    def test_2枚目は断る(self) -> None:
        self.lock.claim("A")
        result = self.lock.claim("B")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, screen_lock.REFUSE_OTHER_SCREEN)

    def test_断るときは誰が使っているか言う(self) -> None:
        """「使えません」だけでは、どうすればよいか分からない。"""
        self.lock.claim("A")
        result = self.lock.claim("B")
        self.assertIsNotNone(result.holder)
        self.assertTrue(result.holder.since_text())

    def test_同じ画面は何度でも通る(self) -> None:
        """同じタブの中で移動しただけ(カレンダー↔設定)。"""
        self.assertTrue(self.lock.claim("A").ok)
        self.assertTrue(self.lock.claim("A").ok)

    def test_心拍が途切れていれば次が入れる(self) -> None:
        """前の画面が異常終了して合図を送れなかった場合。"""
        lock = screen_lock.ScreenLock(stale_sec=0.05)
        lock.claim("A")
        # 余裕を持って待つ(Windows の時計は約 15ms 刻み。0.06 秒では届かないことがある)
        time.sleep(0.15)
        self.assertTrue(lock.claim("B").ok)

    def test_取って代われる(self) -> None:
        """締め出されないための逃げ道。"""
        self.lock.claim("A")
        self.assertTrue(self.lock.claim("B", force=True).ok)
        self.assertEqual(self.lock.active().id, "B")

    def test_取って代わられた画面は外れる(self) -> None:
        self.lock.claim("A")
        self.lock.claim("B", force=True)
        self.assertFalse(self.lock.beat("A"))
        self.assertTrue(self.lock.beat("B"))

    def test_閉じたら次が使える(self) -> None:
        self.lock.claim("A")
        self.lock.release("A")
        self.assertTrue(self.lock.claim("B").ok)

    def test_他人の分は閉じられない(self) -> None:
        """2枚目が閉じても、使っている画面を空けてしまわない。"""
        self.lock.claim("A")
        self.lock.release("B")
        self.assertEqual(self.lock.active().id, "A")

    def test_名前が無ければ判定しない(self) -> None:
        """画面を持たない相手(コマンドなど)を巻き込まない。"""
        self.lock.claim("A")
        self.assertTrue(self.lock.beat(""))
        self.assertTrue(self.lock.is_active(""))

    def test_心拍で生き続ける(self) -> None:
        lock = screen_lock.ScreenLock(stale_sec=0.3)     # 眠りが延びても途切れない幅
        lock.claim("A")
        for _ in range(3):
            time.sleep(0.04)
            lock.beat("A")
        self.assertFalse(lock.claim("B").ok)


class ApiTests(unittest.TestCase):
    """名乗りの入口。"""

    def setUp(self) -> None:
        _web.reset_sync()
        screen_lock.reset()
        self.addCleanup(screen_lock.reset)
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()

    def claim(self, screen_id: str, force: bool = False):
        return self.client.post("/api/screen/claim",
                                json={"screen_id": screen_id, "force": force},
                                headers=_web.auth())

    def test_1枚目は通る(self) -> None:
        self.assertEqual(self.claim("A").status_code, 200)

    def test_2枚目は409(self) -> None:
        """形は正しく、**別の誰かが先に使っている**(設計 §1)。"""
        self.claim("A")
        res = self.claim("B")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()["error"]["code"], "other_screen")

    def test_2枚目にも理由を返す(self) -> None:
        self.claim("A")
        body = self.claim("B").get_json()
        self.assertTrue(body["holder_since"])

    def test_取って代われる(self) -> None:
        self.claim("A")
        self.assertEqual(self.claim("B", force=True).status_code, 200)

    def test_名前が無ければ断る(self) -> None:
        res = self.client.post("/api/screen/claim", json={}, headers=_web.auth())
        self.assertEqual(res.status_code, 409)

    def test_トークンが要る(self) -> None:
        self.assertEqual(
            self.client.post("/api/screen/claim", json={"screen_id": "A"}).status_code,
            403)


class GuardTests(unittest.TestCase):
    """**断ったのに書ける、を作らない。**

    画面側でも入口を塞ぐが、守りはサーバに置く ── 画面の作りに
    関係なく、使ってよい画面以外からは書けないようにしておく。
    """

    def setUp(self) -> None:
        _web.with_source(self)
        screen_lock.reset()
        self.addCleanup(screen_lock.reset)
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()
        self.conn.execute(
            f'INSERT INTO "{config.TABLE_MEMBER}" '
            '("管理番号","苗字","班","名前","読み","担当ライン") '
            'VALUES (?,?,?,?,?,?)', ("10", "山田", "B", "山田太郎", "ヤマダ", "L-1"))
        self.conn.commit()
        user_settings.save_my_line("L-1")
        self.addCleanup(user_settings.save_my_line, "")
        self.client.post("/api/screen/claim", json={"screen_id": "A"},
                         headers=_web.auth())

    def register(self, screen_id: str | None):
        headers = dict(_web.auth())
        if screen_id is not None:
            headers["X-Screen-Id"] = screen_id
        return self.client.post(
            "/api/rest", json={"date": "2026/08/03", "code": "10", "shift": "1"},
            headers=headers)

    def test_使っている画面からは登録できる(self) -> None:
        self.assertEqual(self.register("A").status_code, 200)

    def test_2枚目からは登録できない(self) -> None:
        res = self.register("B")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()["error"]["code"], "other_screen")

    def test_断ったなら1件も入っていない(self) -> None:
        self.register("B")
        row = self.conn.execute(
            f'SELECT COUNT(*) AS cnt FROM "{config.TABLE_DATA}"').fetchone()
        self.assertEqual(row["cnt"], 0)

    def test_読み取りは断らない(self) -> None:
        """見るだけなら害が無い。断るのは**書くとき**。"""
        res = self.client.get("/api/calendar",
                              headers={**_web.auth(), "X-Screen-Id": "B"})
        self.assertEqual(res.status_code, 200)

    def test_名乗っていない相手は素通し(self) -> None:
        """コマンドから使う経路(画面を持たない)を塞がない。"""
        screen_lock.reset()
        self.assertEqual(self.register(None).status_code, 200)

    def test_停止は画面を問わない(self) -> None:
        """``stop.bat`` は画面を持たない。止められなくなるのが一番困る。"""
        res = self.client.post("/api/shutdown", json={},
                               headers={**_web.auth(), "X-Screen-Id": "B"})
        self.assertNotEqual(res.status_code, 409)

    def test_心拍は画面を問わない(self) -> None:
        """外れた画面も送ってくる ── そう返すために受ける必要がある。"""
        res = self.client.post("/api/alive", json={"screen_id": "B"})
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.get_json()["active"])


class HeartbeatTests(unittest.TestCase):
    """心拍で、外れたことを画面へ伝える。"""

    def setUp(self) -> None:
        _web.reset_sync()
        screen_lock.reset()
        self.addCleanup(screen_lock.reset)
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()

    def beat(self, screen_id: str, leaving: bool = False):
        return self.client.post(
            "/api/alive", json={"screen_id": screen_id, "leaving": leaving})

    def test_使っている画面にはそう返す(self) -> None:
        self.client.post("/api/screen/claim", json={"screen_id": "A"},
                         headers=_web.auth())
        self.assertTrue(self.beat("A").get_json()["active"])

    def test_外れた画面にはそう返す(self) -> None:
        self.client.post("/api/screen/claim", json={"screen_id": "A"},
                         headers=_web.auth())
        self.client.post("/api/screen/claim", json={"screen_id": "B", "force": True},
                         headers=_web.auth())
        self.assertFalse(self.beat("A").get_json()["active"])

    def test_閉じたら次が使える(self) -> None:
        self.client.post("/api/screen/claim", json={"screen_id": "A"},
                         headers=_web.auth())
        self.beat("A", leaving=True)
        res = self.client.post("/api/screen/claim", json={"screen_id": "B"},
                               headers=_web.auth())
        self.assertEqual(res.status_code, 200)


if __name__ == "__main__":
    unittest.main()
