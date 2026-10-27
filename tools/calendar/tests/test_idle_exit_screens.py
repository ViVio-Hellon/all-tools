"""画面が2枚あるとき ── **1枚を閉じただけで、アプリごと終わらない**

【何が起きていたか】
``Start.vbs`` をもう一度押すと、断られた2枚目のタブが開きます(使っている
画面は「すでに別の画面で開いています」を出さない側)。この状態で

* 使っている画面を閉じる(2枚目は表に見えたまま)
* 断られた2枚目を閉じる(使っている画面は別の窓で表に見えたまま)

のどちらでも、**9秒後にサーバが終わって**いました。閉じた合図を受けると、
どの画面が閉じたかを見ずに「猶予8秒のあとで終わる」支度に入り、残った画面の
次の心拍(20秒おき)が間に合わなかったためです。残った画面には少しして
「繋がりません」が出ます(調査で本物の Chromium を使って再現)。

【何を見張るか】
1. 閉じた合図で終わるのは**最後の1枚が閉じたとき**だけ。ほかに最近の心拍が
   ある画面か、裏に回った画面が残っていれば終わらない
2. 最後の1枚が閉じれば、これまでどおり猶予のあとで終わる
3. 心拍が途切れた画面(閉じた合図が届かないまま消えた)は数えない
4. **閉じたページから遅れて届いた合図で、閉じた扱いを取り消さない**
   (ページ番号 ``page``)。再読込で新しいページが先に着いても外さない
5. ``POST /api/alive`` が画面ID・ページ番号を見張りへ渡している

猶予(``grace_sec``)は 0 にして、時間を待たずに確かめる。本物のブラウザで
閉じる試験は ``test_idle_exit_browser``。
"""

from __future__ import annotations

import unittest

from . import _web
from calendar_app import idle_exit, screen_lock

CLOSED = "画面が閉じられました"


class _Box:
    """見張りを1つ。時計は試験が進める。"""

    def __init__(self, **kwargs) -> None:
        self.stopped = 0
        self.clock = 1000.0
        kwargs.setdefault("grace_sec", 0.0)
        self.watch = idle_exit.IdleWatch(self._stop, lambda: False,
                                         clock=lambda: self.clock, **kwargs)

    def _stop(self) -> None:
        self.stopped += 1

    def wait(self, sec: float) -> None:
        self.clock += sec


class TwoScreenTests(unittest.TestCase):
    def test_使っている画面を閉じても_断られた2枚目が開いていれば終わらない(self) -> None:
        """再現A: 同じ窓に2枚。使っている A は裏、断られた B が表。A を閉じる。"""
        box = _Box()
        box.watch.beat("A", hidden=True)
        box.watch.beat("B")                       # 断られた2枚目(表に見えている)
        box.watch.leaving("A")
        self.assertIsNone(box.watch.overdue(), "1枚閉じただけで終わる")
        self.assertEqual(box.watch.screens(), {"visible": 1, "hidden": 0})

    def test_断られた2枚目を閉じても_使っている画面が開いていれば終わらない(self) -> None:
        """再現B: B を別の窓で開き、断られた B を閉じる。A は表に見えたまま。"""
        box = _Box()
        box.watch.beat("A")
        box.watch.beat("B")
        box.watch.leaving("B")
        self.assertIsNone(box.watch.overdue(), "1枚閉じただけで終わる")

    def test_裏に回った画面が残っていれば終わらない(self) -> None:
        """裏の画面は心拍が止まっても居るものとみなす(終わらない)。"""
        box = _Box()
        box.watch.beat("A", hidden=True)
        box.watch.beat("B")
        box.watch.leaving("B")
        box.wait(8 * 3600)                        # 裏のまま一晩
        self.assertIsNone(box.watch.overdue())

    def test_最後の1枚が閉じたら終わる(self) -> None:
        box = _Box()
        box.watch.beat("A")
        box.watch.beat("B")
        box.watch.leaving("A")
        box.watch.leaving("B")
        self.assertEqual(box.watch.overdue(), CLOSED)

    def test_最後の1枚が閉じても猶予のうちは待つ(self) -> None:
        """再読込なら戻ってくる。"""
        box = _Box(grace_sec=8.0)
        box.watch.beat("A")
        box.watch.leaving("A")
        box.wait(7)
        self.assertIsNone(box.watch.overdue())
        box.wait(1)
        self.assertEqual(box.watch.overdue(), CLOSED)

    def test_心拍が途切れた画面は数えない(self) -> None:
        """閉じた合図が届かないまま消えた画面のせいで、いつまでも残らない。"""
        box = _Box()
        box.watch.beat("B")
        box.wait(idle_exit.IDLE_SEC - 10)
        box.watch.beat("A")
        box.wait(11)                              # B の心拍は途切れた
        box.watch.leaving("A")
        self.assertEqual(box.watch.overdue(), CLOSED)

    def test_残った画面の心拍が途切れれば終わる(self) -> None:
        """1枚閉じたあと、残りの画面も居なくなったら、無通信で終わる。"""
        box = _Box()
        box.watch.beat("A")
        box.watch.beat("B")
        box.watch.leaving("A")
        box.wait(idle_exit.IDLE_SEC + 1)
        self.assertIsNotNone(box.watch.overdue())


class LatePageTests(unittest.TestCase):
    """閉じたページから遅れて届く合図(ページ番号で見分ける)。"""

    def test_閉じたページから遅れた心拍で取り消さない(self) -> None:
        """送りかけの心拍(fetch keepalive)は、閉じた合図のあとに着くことがある。"""
        box = _Box()
        box.watch.beat("A", page="p1")
        box.watch.leaving("A", page="p1")
        self.assertFalse(box.watch.beat("A", page="p1"))
        self.assertEqual(box.watch.overdue(), CLOSED)

    def test_閉じたページから遅れた隠れますで取り消さない(self) -> None:
        box = _Box()
        box.watch.beat("A", page="p1")
        box.watch.leaving("A", page="p1")
        self.assertFalse(box.watch.beat("A", hidden=True, page="p1"))
        self.assertFalse(box.watch.in_background())
        self.assertEqual(box.watch.overdue(), CLOSED)

    def test_再読込の新しいページは受け付ける(self) -> None:
        box = _Box()
        box.watch.beat("A", page="p1")
        box.watch.leaving("A", page="p1")
        self.assertTrue(box.watch.beat("A", page="p2"))
        self.assertIsNone(box.watch.overdue())

    def test_新しいページが先に着いても_前のページの閉じた合図で外さない(self) -> None:
        """再読込で、新しいページの心拍が前のページの閉じた合図より先に着いた。"""
        box = _Box()
        box.watch.beat("A", page="p1")
        box.watch.beat("A", page="p2")
        box.watch.leaving("A", page="p1")
        self.assertIsNone(box.watch.overdue())
        self.assertEqual(box.watch.screens(), {"visible": 1, "hidden": 0})


class AliveApiTests(unittest.TestCase):
    """``POST /api/alive`` を通して(画面ID・ページ番号を見張りへ渡しているか)。"""

    def setUp(self) -> None:
        _web.reset_sync()
        screen_lock.reset()
        self.addCleanup(screen_lock.reset)
        idle_exit.reset()
        self.addCleanup(idle_exit.reset)
        self.box = _Box()
        idle_exit._watch = self.box.watch
        _web.bind_db(self)
        self.client = _web.make_client()

    def alive(self, screen_id: str, state: str = "visible", *, leaving: bool = False,
              page: str = ""):
        body = {"screen_id": screen_id, "state": state, "leaving": leaving}
        if page:
            body["page"] = page
        return self.client.post("/api/alive", json=body)

    def claim(self, screen_id: str):
        return self.client.post("/api/screen/claim", json={"screen_id": screen_id},
                                headers=_web.auth())

    def test_使っている画面を閉じても_断られた画面が開いていれば終わらない(self) -> None:
        self.claim("A")
        self.alive("A", "hidden", page="pa")
        self.assertEqual(self.claim("B").status_code, 409)    # 2枚目は断られる
        self.alive("B", page="pb")
        self.alive("A", leaving=True, page="pa")
        self.assertIsNone(self.box.watch.overdue(), "1枚閉じただけで終わる")

    def test_断られた画面を閉じても_使っている画面が開いていれば終わらない(self) -> None:
        self.claim("A")
        self.alive("A", page="pa")
        self.claim("B")
        self.alive("B", page="pb")
        self.alive("B", leaving=True, page="pb")
        self.assertIsNone(self.box.watch.overdue(), "1枚閉じただけで終わる")
        # 使っている画面は使っている画面のまま
        self.assertTrue(self.alive("A", page="pa").get_json()["active"])

    def test_両方閉じたら終わる(self) -> None:
        self.claim("A")
        self.alive("A", page="pa")
        self.claim("B")
        self.alive("B", page="pb")
        self.alive("B", leaving=True, page="pb")
        self.alive("A", leaving=True, page="pa")
        self.assertEqual(self.box.watch.overdue(), CLOSED)

    def test_閉じたページからの遅れた合図は数えない(self) -> None:
        self.claim("A")
        self.alive("A", page="pa")
        self.alive("A", leaving=True, page="pa")
        self.alive("A", "hidden", page="pa")      # 閉じるときの「隠れます」が後から着いた
        self.alive("A", page="pa")                # 送りかけの心拍が後から着いた
        self.assertEqual(self.box.watch.overdue(), CLOSED)


if __name__ == "__main__":
    unittest.main()
