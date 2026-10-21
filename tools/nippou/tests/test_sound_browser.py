"""音: **どの出来事で鳴らし、どれで鳴らさないか**を本物のブラウザで (段階1)

前は `api.js` / `sound.js` / `errors.js` / `review.js` / `settings.js` の字を探して、
「配線の行が入っていること」だけを見ていました(`test_sound_events` の `WiringTests`・
`test_review_and_sound` の `DueSoundTokenTests`)。ここでは画面を開いて、サーバの答えを
試験が決め、**ブラウザが鳴らそうとした道**(`<audio>` の `play()`)を見ます:

    - 押した操作の断りは「断られた」、500 は「エラーが起きた」
    - 決まった音がある断り(`sound`)・聞き返し(直の変わり目)では「断られた」を鳴らさない
    - 自動保存(押していない)の断りでは鳴らさない(`mute`)
    - 鳴らしてよい一覧(`cues`)に無い出来事は鳴らしに行かない
    - 1分ごとの見張り(誰も押していない通信)でも、添えられた出来事は鳴らす。断りは鳴らさない
    - 催促の音は鍵から組み立て直してトークンを付ける(素の `/sound/<鍵>` は 401 で永久に鳴らない)
    - 全画面確認を開いたら「本日の梱包結果」
    - 設定で選んで保存したら、1分待たずにその選び方で鳴る(画面のエラーで「エラーが起きた」)
    - 試聴は選んだファイル(保存前)を鳴らす

本当には鳴らしません(`play()` を差し替えて道を覚えるだけ)。
"""
from __future__ import annotations

import struct
import sys
import unittest
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._screen_browser import ScreenBrowserCase  # noqa: E402

REFUSED = {"error": {"code": "test", "message": "試験で断りました"}}


def _write_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(struct.pack("<100h", *([0] * 100)))


class SoundBrowserCase(ScreenBrowserCase):
    def setUp(self) -> None:
        super().setUp()
        from nippou import config, user_settings
        from nippou.logic import sound

        sound.reset()
        self.addCleanup(sound.reset)
        self.snd = self.tmp / "sounds"
        _write_wav(self.snd / "ぴんぽん.wav")
        user_settings.save(config.KEY_SOUND_DIR, str(self.snd))

    def choose(self, *keys: str) -> None:
        """設定画面の「音の設定を保存」と同じ道で、出来事に音を付ける。"""
        res = self.post("/api/settings/paths", {f"sound_file_{k}": "ぴんぽん.wav" for k in keys})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True)[:300])

    def settle(self, page, prefix: str, before: int, method: str = "POST") -> None:
        """`prefix` の要求が `before` 本より増え、答えを塗り終えるまで待つ。"""
        self.wait_until(page, lambda: len(self.asked(prefix, method)) > before,
                        f"{prefix} の要求")
        page.wait_for_timeout(500)


class EntrySoundTests(SoundBrowserCase):
    """日報入力で押した保存の断り。**何に鳴らして、何に鳴らさないか。**"""

    def test_押した保存の断りとエラーは鳴らし_自動保存と聞き返しと一覧に無いものは鳴らさない(self) -> None:
        self.choose("refused", "error")                  # 「保存した」は選ばない(一覧に無い)
        page = self.open_entry()
        self.wait_until(page, lambda: self.asked("/api/sound/due", "GET"), "鳴らせる一覧の読み込み")
        page.wait_for_timeout(300)

        def autosave(answer) -> None:
            self.answers["/api/entry/save"] = answer
            before = len(self.asked("/api/entry/save", "POST"))
            self.type_in(page, "MAI1", str(10 + before))
            page.locator("#MAI1").press("Tab")
            self.settle(page, "/api/entry/save", before)

        def press_save(answer) -> list[str]:
            self.answers["/api/entry/save"] = answer
            mark = len(self.played_keys(page))
            before = len(self.asked("/api/entry/save", "POST"))
            page.click("#save")
            self.settle(page, "/api/entry/save", before)
            return self.played_keys(page)[mark:]

        with self.subTest("自動保存が断られても鳴らさない(押していない)"):
            autosave((422, REFUSED))
            self.assertEqual(self.played_keys(page), [])
        with self.subTest("一覧に無い出来事(保存した)は鳴らしに行かない"):
            autosave((200, {"saved": False, "skipped": "", "sound_cue": "saved"}))
            self.assertEqual(self.played_keys(page), [])
        with self.subTest("押した保存が断られたら「断られた」"):
            self.assertEqual(press_save((422, REFUSED)), ["refused"])
        with self.subTest("押した保存がサーバのエラーなら「エラーが起きた」"):
            self.assertEqual(press_save((500, REFUSED)), ["error"])
        with self.subTest("決まった音がある断りは「断られた」にしない"):
            self.assertEqual(press_save((422, {**REFUSED, "sound": "too_many_packs"})), [])
        with self.subTest("直の変わり目の聞き返しは断りではない"):
            crossed = {**REFUSED, "shift_changed": {"crossed": True, "message": "2直に変わりました",
                                                    "from": "1直", "to": "2直"}}
            self.assertEqual(press_save((409, crossed)), [])


class WatchSoundTests(SoundBrowserCase):
    """1分ごとの見張り(誰も押していない通信)。開いた直後に1回走ります。"""

    def test_見張りの答えの出来事は鳴らし_催促はトークン付き_見張りの断りは鳴らさない(self) -> None:
        self.choose("auto_closed", "error", "print_reminder")
        # 残り5分の自動確定(`/api/entry/close`)が「確定した」と添えてきた
        self.answers["/api/entry/close"] = (200, {"ran": False, "swept": [],
                                                   "sound_cue": "auto_closed"})
        # 催促の出番。**素の道(`url`)はトークンが無いので 401 になる**
        self.answers["/api/sound/due"] = (200, {
            "play": {"key": "print_reminder", "url": "/sound/print_reminder",
                     "message": "印刷を忘れずに(試験)", "sound": True},
            "cues": ["auto_closed", "error", "print_reminder"]})
        # 見張りの通信が 500 で返っても「エラーが起きた」は鳴らさない
        self.answers["/api/graph/review"] = (500, {"error": {"code": "test", "message": "x",
                                                             "event_id": "E-TEST"}})
        page = self.open_screen("/records")
        self.wait_played(page, "auto_closed")
        self.wait_played(page, "print_reminder")
        self.wait_until(page, lambda: self.asked("/api/graph/review", "GET"), "見張りの確認")
        page.wait_for_timeout(600)
        reminder = next(u for u in self.played(page) if u.startswith("/sound/print_reminder"))
        self.assertIn("t=test-token", reminder, "トークンを付けずに鳴らしに行っています(401 で鳴らない)")
        self.assertNotIn("error", self.played_keys(page))
        # **鳴っても文言は出す**(音は補助)
        page.wait_for_selector("text=印刷を忘れずに(試験)", timeout=3000)


class ReviewSoundTests(SoundBrowserCase):
    def test_全画面確認を開いたら本日の梱包結果を鳴らす(self) -> None:
        _write_wav(self.snd / "本日の梱包結果なのだ.wav")
        page = self.open_screen("/graph/review")
        self.wait_played(page, "daily_result")


class SettingsSoundTests(SoundBrowserCase):
    """設定画面の音の面。**2度目に来た画面でも**ボタンが効くこと(差し替えのあとに `start()`)。"""

    def test_選んで保存したらすぐその選び方で鳴る_試聴は選んだファイル(self) -> None:
        page = self.open_screen("/settings?tab=sound")
        # いちど別の画面へ移って戻る(`<main>` が差し替わる。配線し直さないとボタンが死ぬ)
        page.click('nav.rail a[href="/records"]')
        page.wait_for_function("location.pathname === '/records'", timeout=8000)
        page.wait_for_timeout(500)
        page.go_back()
        page.wait_for_function("location.pathname === '/settings'", timeout=8000)
        page.wait_for_selector("#sound-error", timeout=8000)
        page.wait_for_timeout(500)

        with self.subTest("試聴は選んだファイル(保存前)"):
            page.select_option("#sound-error", "ぴんぽん.wav")
            page.click('[data-preview="error"]')
            self.wait_until(page, lambda: any(u.startswith("/sound/preview") for u in self.played(page)),
                            "試聴")
            url = next(u for u in self.played(page) if u.startswith("/sound/preview"))
            self.assertIn("name=%E3%81%B4%E3%82%93%E3%81%BD%E3%82%93.wav", url)
            self.assertIn("t=test-token", url)

        with self.subTest("保存したら一覧を読み直し、1分待たずに鳴る"):
            before = len(self.asked("/api/sound/cues", "GET"))
            page.select_option("#sound-error", "ぴんぽん.wav")
            page.click("#save-sounds")
            self.wait_until(page, lambda: len(self.asked("/api/sound/cues", "GET")) > before,
                            "鳴らせる一覧の読み直し")
            page.wait_for_timeout(500)
            # 画面の中で起きたエラー(番号つきで記録へ届く)→「エラーが起きた」
            page.evaluate("setTimeout(() => { throw new Error('試験で起こしたエラー'); })")
            self.wait_played(page, "error")


if __name__ == "__main__":
    unittest.main()
