"""直の終わりの実績確認 (VBA `graphF`) と 音 (VBA `音楽を流す`)

【どちらも「判断はサーバ、出すのは画面」】
    いつ出すか・いつ鳴らすか      … `logic/shift_review.py` / `logic/sound.py`
    実際に描く・鳴らす            … ブラウザ

tkinter版は `winsound` でサーバ側から鳴らしていた。Web版でそれをやると
`pythonw` のセッションで鳴らすことになり、画面を見ている人に届く保証が
ない ── だから鳴らすのはブラウザに移し、サーバは判断だけを持つ。
"""
from __future__ import annotations

import struct
import sys
import unittest
import wave
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import shift_review, sound
from nippou.logic.shift import ShiftCalculator, ShiftTimes
from tests._web import WebTestCase


def _write_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(struct.pack("<100h", *([0] * 100)))


# ==================================================================
# 直の終わりの確認 (純ロジック)
# ==================================================================
#: このファイルの時刻は、**工場の既定値とは切り離した直**で考えます
#: (`tests/test_shift.py` と同じ理由 ── 既定値は現場の
#: 07:00-15:00-22:50 に合わせて動くので、算数のテストを縛らせない)。
SHEET_TIMES = ShiftTimes(start1="08:00", end1="17:00",
                         start2="17:00", end2="22:00",
                         start3="22:00", end3="08:00",
                         start_day="08:00", end_day="18:00")


class ReviewRuleTests(unittest.TestCase):
    """`shift_review.evaluate` ── **出さない場面のほうが多い。**"""

    def setUp(self) -> None:
        shift_review.reset()
        self.addCleanup(shift_review.reset)
        self.calc = ShiftCalculator(SHEET_TIMES)
        # 1直は 08:00-17:00(既定)。終わりの10分前と、まだ間があるとき
        self.near = datetime(2026, 1, 5, 16, 50)
        self.far = datetime(2026, 1, 5, 10, 0)

    def _eval(self, now, **kw):
        return shift_review.evaluate(now, self.calc, "2026年1月5日", "1直", **kw)

    def test_終わりが近ければ出す(self) -> None:
        state = self._eval(self.near)
        self.assertTrue(state.due)
        self.assertIn("確認してください", state.reason)

    def test_まだ間があれば出さない(self) -> None:
        self.assertFalse(self._eval(self.far).due)

    def test_確認済みなら出さない(self) -> None:
        """一度見たものを何度も出すと、次から読まずに閉じる。"""
        shift_review.gate().mark("2026年1月5日", "1直")
        state = self._eval(self.near)
        self.assertFalse(state.due)
        self.assertTrue(state.acknowledged)

    def test_直が変わればまた出す(self) -> None:
        shift_review.gate().mark("2026年1月5日", "1直")
        other = shift_review.evaluate(self.near, self.calc, "2026年1月5日", "2直")
        self.assertFalse(other.acknowledged)

    def test_呼出モード中は出さない(self) -> None:
        """過去のデータを直している最中。「この直の実績」は当てはまらない。"""
        self.assertFalse(self._eval(self.near, recall_mode=True).due)

    def test_データが無ければ出さない(self) -> None:
        """空のグラフを全画面で出しても確かめようがない。"""
        self.assertFalse(self._eval(self.near, has_data=False).due)

    def test_理由に残り時間が入る(self) -> None:
        self.assertIn("分", self._eval(self.near).reason)

    def test_終わり間際でも出す(self) -> None:
        # 17:00ちょうどは次の直(2直)に入るので、その1分前で見る
        state = self._eval(datetime(2026, 1, 5, 16, 59))
        self.assertTrue(state.due)
        self.assertIn("1分", state.reason)

    def test_猶予はサーバが決める(self) -> None:
        # 15分前が既定。5分にすれば10分前では出ない
        self.assertTrue(self._eval(self.near, warn_minutes=15).due)
        self.assertFalse(self._eval(self.near, warn_minutes=5).due)


# ==================================================================
# 音 (純ロジック)
# ==================================================================
class SoundGateTests(unittest.TestCase):
    """`SoundGate` ── VBA `soundPlayed` の見張り。

    同じ判断は1分ごとに真になり続けるので、押さえないと鳴り続ける。
    """

    def setUp(self) -> None:
        sound.reset()
        self.addCleanup(sound.reset)

    def test_1回だけ通す(self) -> None:
        gate = sound.gate()
        self.assertTrue(gate.take("a"))
        self.assertFalse(gate.take("a"))

    def test_別の鍵は別に数える(self) -> None:
        gate = sound.gate()
        self.assertTrue(gate.take("a"))
        self.assertTrue(gate.take("b"))

    def test_忘れればまた通す(self) -> None:
        gate = sound.gate()
        gate.take("a")
        gate.forget("a")
        self.assertTrue(gate.take("a"))


class SoundSpecTests(unittest.TestCase):
    def test_鍵と仕様が1対1(self) -> None:
        # VBA `音楽を流す` が鳴らしていた3つに、保存前チェックの2つ
        # (梱包数が多い / 登録に存在しない停止)を足して5つ。v4.4.0 で
        # 出来事を5つ足して10(保存した・共有へ保存した・直が始まった・
        # 断られた・エラーが起きた)、v4.5.0 で「直の残り5分」を足して11
        self.assertEqual(len(sound.SOUNDS), 11)
        self.assertEqual(sound.SOUND_KEYS, tuple(s.key for s in sound.SOUNDS))
        self.assertEqual(len(set(sound.SOUND_KEYS)), 11)

    def test_足した出来事の既定は鳴らさない(self) -> None:
        """入れただけで現場の音が増えないように。**前からある5つは前のまま。**"""
        added = (sound.KEY_SAVED, sound.KEY_PUSHED, sound.KEY_SHIFT_STARTED,
                 sound.KEY_REFUSED, sound.KEY_ERROR, sound.KEY_AUTO_CLOSED)
        for spec in sound.SOUNDS:
            with self.subTest(key=spec.key):
                self.assertEqual(spec.default_file == "", spec.key in added)
                self.assertTrue(spec.when, spec.key)        # 設定画面の説明

    def test_どれにも文言がある(self) -> None:
        """**音は補助。** 鳴らせなくても伝わるように、文言を持つ。"""
        for spec in sound.SOUNDS:
            self.assertTrue(spec.message, spec.key)
            self.assertTrue(spec.label, spec.key)

    def test_既定のファイル名はVBAのまま(self) -> None:
        by_key = {s.key: s.default_file for s in sound.SOUNDS}
        self.assertEqual(by_key[sound.KEY_PRINT_REMINDER], "印刷忘れにご注意.wav")
        self.assertEqual(by_key[sound.KEY_DAILY_RESULT], "本日の梱包結果なのだ.wav")
        self.assertEqual(by_key[sound.KEY_NEGATIVE_TIME], "作業時間がマイナスです.wav")

    def test_知らない鍵はNone(self) -> None:
        self.assertIsNone(sound.spec("nope"))


# ==================================================================
# 画面から
# ==================================================================
class ReviewScreenTests(WebTestCase):
    def setUp(self) -> None:
        super().setUp()
        from nippou.logic import shift_review as sr

        sr.reset()
        self.addCleanup(sr.reset)

    def _save_today(self) -> None:
        self.post("/api/entry/save",
                  {"rows": {"1": {"LOT": "A", "WEI": "10"}},
                   "header": {}, "checks": {}})

    def test_確認画面が出る(self) -> None:
        res = self.get("/graph/review")
        self.assertEqual(res.status_code, 200)
        body = res.get_data(as_text=True)
        # **画面いっぱいの覆い。** レールも帯も覆う
        self.assertIn('class="review"', body)
        self.assertIn("確認しました", body)

    def test_本当の全画面にするボタンもある(self) -> None:
        """ブラウザは操作なしに全画面にできないので、押す道を用意する。"""
        body = self.get("/graph/review").get_data(as_text=True)
        self.assertIn("review-fullscreen", body)

    def test_データが無ければ出さない(self) -> None:
        state = self.get("/api/graph/review").get_json()
        self.assertFalse(state["due"])

    def test_確認すると出なくなる(self) -> None:
        self._save_today()
        res = self.post("/api/graph/review/ack", {})
        self.assertEqual(res.status_code, 200)
        self.assertIn("確認しました", res.get_json()["message"])
        self.assertFalse(self.get("/api/graph/review").get_json()["due"])

    def test_呼出モード中は出さない(self) -> None:
        from nippou import work_context

        self._save_today()
        ctx = work_context.get_context()
        ctx.admin = True
        ctx.recall = work_context.RecallState(True, "2020年1月1日", ctx.line, "3直", 1)
        self.assertFalse(self.get("/api/graph/review").get_json()["due"])


class SoundScreenTests(WebTestCase):
    def setUp(self) -> None:
        super().setUp()
        from nippou.logic import sound as s

        s.reset()
        self.addCleanup(s.reset)

    def _use_sound_dir(self) -> Path:
        snd = self.tmp / "sounds"
        snd.mkdir(exist_ok=True)
        # パスの変更は**全部**管理者パスワードが要ります(v3.49.0)
        self.post("/api/settings/paths",
                  {"sound_dir": str(snd), "password": "nisk"})
        return snd

    def test_状態が取れる(self) -> None:
        body = self.get("/api/sound/state").get_json()
        self.assertEqual(len(body["sounds"]), len(sound.SOUNDS))
        self.assertIn("dir", body)

    def test_ファイルが無くても止めない(self) -> None:
        """音が鳴らないだけで、催促そのものは画面に出る。"""
        picked = self.get("/api/sound/state").get_json()["sounds"][0]
        self.assertFalse(picked["playable"])
        self.assertIn("文言だけ", picked["error"])

    def test_置けば鳴らせる(self) -> None:
        snd = self._use_sound_dir()
        _write_wav(snd / "印刷忘れにご注意.wav")
        picked = next(s for s in self.get("/api/sound/state").get_json()["sounds"]
                      if s["key"] == "print_reminder")
        self.assertTrue(picked["playable"])
        self.assertGreater(picked["size"], 0)

    def test_音声ファイルを配る(self) -> None:
        snd = self._use_sound_dir()
        _write_wav(snd / "印刷忘れにご注意.wav")
        res = self.get("/sound/print_reminder")
        self.assertEqual(res.status_code, 200)
        self.assertIn("wav", res.headers["Content-Type"])

    def test_知らない鍵は404(self) -> None:
        self.assertEqual(self.get("/sound/nope").status_code, 404)

    def test_ファイルが無ければ404(self) -> None:
        self.assertEqual(self.get("/sound/print_reminder").status_code, 404)

    def test_トークンが要る(self) -> None:
        """設定で決まった道のファイルを返すので、素通しにはしない。"""
        res = self.client.get("/sound/print_reminder", headers={"Host": "127.0.0.1"})
        self.assertEqual(res.status_code, 401)

    def test_配れない拡張子は断る(self) -> None:
        snd = self._use_sound_dir()
        (snd / "うそ.txt").write_text("x")
        self.post("/api/settings/paths",
                  {"sound_file_print_reminder": "うそ.txt", "password": "nisk"})
        # そもそも保存で断られる
        state = self.get("/api/sound/state").get_json()["sounds"][0]
        self.assertEqual(state["file"], "印刷忘れにご注意.wav")

    def test_ファイル名を変えられる(self) -> None:
        snd = self._use_sound_dir()
        _write_wav(snd / "べつの音.wav")
        res = self.post("/api/settings/paths",
                        {"sound_file_print_reminder": "べつの音.wav"})
        self.assertEqual(res.status_code, 200)
        picked = next(s for s in self.get("/api/sound/state").get_json()["sounds"]
                      if s["key"] == "print_reminder")
        self.assertEqual(picked["file"], "べつの音.wav")
        self.assertTrue(picked["playable"])

    def test_音の名前はパスワード不要(self) -> None:
        """**守るのはパスだけ。** 名前は間違えても鳴らないだけです。

        置き場所(`sound_dir`)のほうはパスなので要ります ── 読み書きの
        相手が変わるかどうかが分かれ目で、鳴る/鳴らないではありません。
        """
        res = self.post("/api/settings/paths",
                        {"sound_file_print_reminder": "べつの音.wav"})
        self.assertEqual(res.status_code, 200)
        res = self.post("/api/settings/paths",
                        {"sound_dir": str(self.tmp / "ほか")})
        self.assertEqual(res.status_code, 403)

    def test_変な拡張子は断る(self) -> None:
        res = self.post("/api/settings/paths",
                        {"sound_file_print_reminder": "うそ.txt"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("拡張子", res.get_json()["error"]["message"])

    def test_設定画面に音の面が出る(self) -> None:
        body = self.get("/settings").get_data(as_text=True)
        self.assertIn("直の残り15分(終わりの催促)", body)
        self.assertIn("試聴", body)

    def test_出番は1度しか来ない(self) -> None:
        """同じ判断は1分ごとに真になる。押さえないと鳴り続ける。"""
        first = self.get("/api/sound/due").get_json()
        second = self.get("/api/sound/due").get_json()
        if first["play"] is not None:
            self.assertIsNone(second["play"], "2度目も鳴らそうとしています")

    def test_出番には文言が付く(self) -> None:
        body = self.get("/api/sound/due").get_json()
        if body["play"] is not None:
            self.assertTrue(body["play"]["message"])
            self.assertTrue(body["play"]["url"].startswith("/sound/"))


class NoWinsoundTests(unittest.TestCase):
    """**サーバ側で鳴らさない。**

    tkinter版は `winsound.PlaySound` を呼んでいた。Web版でそれをやると
    `pythonw` のセッションで鳴らすことになり、画面を見ている人に届く
    保証がない。掴んでいないことを機械で見ておく。
    """

    def test_winsoundを掴んでいない(self) -> None:
        import re

        # **文中の「winsound」は見逃す。** なぜ使わないのかを説明で
        # 書いてあるので、単語で探すとそこに当たる。使っている形だけを見る
        using = re.compile(r"^\s*(import\s+winsound|from\s+winsound\b)"
                           r"|winsound\.", re.MULTILINE)
        root = Path(__file__).resolve().parent.parent
        offenders = []
        for path in list((root / "nippou").rglob("*.py")) \
                + list((root / "app").rglob("*.py")):
            if using.search(path.read_text(encoding="utf-8", errors="replace")):
                offenders.append(str(path.relative_to(root)))
        self.assertEqual(offenders, [],
                         f"winsound を掴んでいるファイルがあります: {offenders}")


class DueSoundTokenTests(WebTestCase):
    """催促の音が**永久に鳴らなかった**不具合。

    `/api/sound/due` が返す `url` は `/sound/<鍵>` で、トークンが
    付いていません。`/sound/` はトークンが要る経路なので、画面がその道を
    そのまま使うと 401 が返り、**押して鳴らす音は鳴るのに、催促の音だけが
    鳴らない**状態になります(ブラウザで通したときに 401 で見つかりました)。

    鍵から組み立て直せば `playKey` がトークンを付けるので、ここでは
    「鍵が返ること」と「画面が鍵のほうを使うこと」を守ります。
    """

    def test_出番には鍵が付く(self) -> None:
        body = self.get("/api/sound/due").get_json()
        # 出番が無いときもある(直の途中)。鍵の綴りだけ確かめる
        if body.get("play"):
            self.assertIn(body["play"]["key"],
                          {s.key for s in sound.SOUNDS})

    def test_素の道はトークンで断られる(self) -> None:
        """**ここが 401 の出どころ。** `<audio src>` はヘッダを付けられない。"""
        res = self.client.get("/sound/print_reminder",
                              headers={"Host": "127.0.0.1"})
        self.assertEqual(res.status_code, 401)

    def test_画面は鍵から組み立て直す(self) -> None:
        root = Path(__file__).resolve().parent.parent
        text = (root / "app" / "static" / "js" / "sound.js").read_text(
            encoding="utf-8")
        self.assertIn("playKey(due.key)", text)
        # 素の `due.url` を鳴らしに行かない
        self.assertNotIn("play(due.url)", text)


if __name__ == "__main__":
    unittest.main()
