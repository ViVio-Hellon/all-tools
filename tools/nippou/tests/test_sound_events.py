"""音: 出来事ごとに「鳴らさない / このファイル」を選ぶ (v4.4.0)

    音声ファイルを追加したいタイミングが増えた場合どうしたらいいですか？
    → 候補になる出来事をあらかじめ一覧にしておく。設定画面で出来事ごとに
      「鳴らさない/このファイルを鳴らす」を選べるので、一覧にある出来事なら
      コードを触らずに音を付けられる

あわせて「本日の梱包結果」(`本日の梱包結果なのだ.wav`)を、直の終わりの全画面
確認を開いたときに鳴らします(鍵も設定の欄もあったのに、鳴らすきっかけが
どこにも繋がっていなかった)。
"""
from __future__ import annotations

import struct
import sys
import unittest
import wave
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HAS_FLASK, NO_TOKEN_HEADERS, SKIP_REASON, WebTestCase  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "app" / "static" / "js"


def _write_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(struct.pack("<100h", *([0] * 100)))


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class SoundEventCase(WebTestCase):
    def setUp(self) -> None:
        super().setUp()
        from nippou import config, user_settings
        from nippou.logic import sound

        sound.reset()
        self.addCleanup(sound.reset)
        self.snd = self.tmp / "sounds"
        self.snd.mkdir()
        user_settings.save(config.KEY_SOUND_DIR, str(self.snd))

    def choose(self, **picked) -> None:
        """設定画面の「音の設定を保存」と同じ道で選ぶ(`sound_file_<出来事>`)。"""
        res = self.post("/api/settings/paths",
                        {f"sound_file_{k}": v for k, v in picked.items()})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True)[:300])

    def cues(self) -> list[str]:
        return self.get("/api/sound/cues").get_json()["cues"]


class ChooseTests(SoundEventCase):
    """出来事ごとに選ぶ。**足した出来事の既定は鳴らさない。**"""

    def test_足した出来事は既定で鳴らさない(self) -> None:
        from nippou.config import SETTINGS

        _write_wav(self.snd / "ぴんぽん.wav")
        for key in ("saved", "pushed", "shift_started", "refused", "error", "auto_closed"):
            with self.subTest(key=key):
                self.assertEqual(SETTINGS.sound_file(key), "")
                self.assertIsNone(SETTINGS.sound_path(key))
        self.assertEqual(self.cues(), [])

    def test_フォルダのファイルを選べば鳴らせる(self) -> None:
        _write_wav(self.snd / "ぴんぽん.wav")
        self.choose(saved="ぴんぽん.wav", error="ぴんぽん.wav")
        self.assertEqual(self.cues(), ["saved", "error"])
        res = self.get("/sound/saved")
        self.assertEqual(res.status_code, 200)
        res.close()

    def test_前からある音も鳴らさないにできる(self) -> None:
        from nippou.config import SETTINGS

        _write_wav(self.snd / "印刷忘れにご注意.wav")
        self.assertIn("print_reminder", self.cues())
        self.choose(print_reminder="鳴らさない")
        self.assertEqual(SETTINGS.sound_file("print_reminder"), "")
        self.assertNotIn("print_reminder", self.cues())
        self.assertEqual(self.get("/sound/print_reminder").status_code, 404)
        # 戻すときは既定のファイルを選び直す
        self.choose(print_reminder="印刷忘れにご注意.wav")
        self.assertIn("print_reminder", self.cues())

    def test_ファイルが無ければ鳴らせる一覧に入らない(self) -> None:
        self.choose(pushed="まだ無い.wav")
        self.assertNotIn("pushed", self.cues())

    def test_音声ファイルでないものは選べない(self) -> None:
        res = self.post("/api/settings/paths", {"sound_file_saved": "メモ.txt"})
        self.assertEqual(res.status_code, 400)
        res = self.post("/api/settings/paths", {"sound_file_saved": r"..\\外.wav"})
        self.assertEqual(res.status_code, 400)


class SettingsScreenTests(SoundEventCase):
    def test_選択肢は鳴らさないとフォルダのファイル(self) -> None:
        from nippou.presenters import settings as presenter

        _write_wav(self.snd / "b.wav")
        _write_wav(self.snd / "a.mp3")
        (self.snd / "メモ.txt").write_text("x", encoding="utf-8")
        views = {v["key"]: v for v in presenter.sound_views()}
        saved = views["saved"]
        self.assertEqual([o["value"] for o in saved["options"]], ["鳴らさない", "a.mp3", "b.wav"])
        self.assertTrue(saved["options"][0]["selected"])
        self.assertTrue(saved["off"])
        self.assertIn("自動保存では鳴らしません", saved["when"])
        # 既定のファイルがフォルダに無くても選択肢に残す(黙ってすり替えない)
        reminder = views["print_reminder"]
        last = reminder["options"][-1]
        self.assertEqual(last["value"], "印刷忘れにご注意.wav")
        self.assertIn("既定", last["label"])
        self.assertIn("フォルダにありません", last["label"])
        self.assertTrue(last["selected"])

    def test_画面に出来事の一覧と選ぶ欄(self) -> None:
        _write_wav(self.snd / "ぴんぽん.wav")
        html = self.get("/settings?tab=sound").get_data(as_text=True)
        for label in ("直の残り15分(終わりの催促)", "直の残り5分(自動で確定)",
                      "本日の梱包結果", "保存した", "共有へ保存した",
                      "直が始まった", "断られた", "エラーが起きた"):
            self.assertIn(label, html)
        self.assertIn('data-sound-file="sound_file_saved"', html)
        self.assertIn('<option value="鳴らさない" selected>鳴らさない</option>', html)
        self.assertIn('<option value="ぴんぽん.wav"', html)
        self.assertIn(str(self.snd), html)

    def test_フォルダが無ければそう言う(self) -> None:
        from nippou import config, user_settings
        from nippou.presenters import settings as presenter

        user_settings.save(config.KEY_SOUND_DIR, str(self.tmp / "無い"))
        folder = presenter.sound_folder_view()
        self.assertEqual(folder["files"], [])
        self.assertIn("ありません", folder["error"])


class PreviewTests(SoundEventCase):
    """試聴は**選んだファイル(保存前)**。音声フォルダの中だけ。"""

    def test_フォルダの中の音声ファイルを返す(self) -> None:
        _write_wav(self.snd / "ぴんぽん.wav")
        res = self.get("/sound/preview?name=ぴんぽん.wav")
        self.assertEqual(res.status_code, 200)
        self.assertIn("wav", res.headers["Content-Type"])
        res.close()

    def test_フォルダの外と音声でないものは返さない(self) -> None:
        _write_wav(self.tmp / "外.wav")
        (self.snd / "メモ.txt").write_text("x", encoding="utf-8")
        for name in ("../外.wav", "..\\外.wav", str(self.tmp / "外.wav"), "メモ.txt",
                     "", "無い.wav"):
            with self.subTest(name=name):
                self.assertEqual(self.get(f"/sound/preview?name={name}").status_code, 404)

    def test_トークンが要る(self) -> None:
        _write_wav(self.snd / "ぴんぽん.wav")
        res = self.client.get("/sound/preview?name=ぴんぽん.wav", headers=NO_TOKEN_HEADERS)
        self.assertEqual(res.status_code, 401)


class CueTests(SoundEventCase):
    """出来事が起きたことを応答に添える(`sound_cue`)。鳴らすかは画面が一覧で決める。"""

    def test_保存したは確定のときだけ(self) -> None:
        from tests.test_auto_export import payload

        body = self.post("/api/entry/save", payload()).get_json()
        self.assertTrue(body["saved"])
        self.assertEqual(body["sound_cue"], "saved")
        for flag in ("silent", "draft"):
            with self.subTest(flag=flag):
                body = self.post("/api/entry/save", payload() | {flag: True}).get_json()
                self.assertNotIn("sound_cue", body)

    def test_共有へ保存したは送れたときだけ(self) -> None:
        from nippou import config, user_settings
        from tests.test_auto_export import payload

        share = self.tmp / "共有"
        share.mkdir()
        user_settings.save_many({config.KEY_ACCESS_DIR: str(share),
                                 config.KEY_REPORT_OUT_DIR: str(self.tmp / "出力")})
        self.post("/api/entry/save", payload())
        with patch("nippou.services.shift_check.run_pending", return_value=[]):
            body = self.post("/api/settings/push", {}).get_json()
        self.assertEqual(body["succeeded"], 1)
        self.assertEqual(body["sound_cue"], "pushed")


class ShiftLeftTests(SoundEventCase):
    """直の残り時間で鳴る2つ (v4.5.0)。

        直残り時間の判定にも鳴らせるようにしてください(残り15分、残り5分でしたっけ？)

    残り15分 … 前からある「終わりの催促」(まだ確定していなければ)
    残り 5分 … 打ってあるぶんを自動で確定したとき(足した出来事)
    """

    def test_名前と説明に設定の分が入る(self) -> None:
        from nippou.config import SETTINGS
        from nippou.presenters import settings as presenter

        views = {v["key"]: v for v in presenter.sound_views()}
        self.assertEqual(views["print_reminder"]["label"], "直の残り15分(終わりの催促)")
        self.assertIn("15分を切ったとき", views["print_reminder"]["when"])
        self.assertEqual(views["auto_closed"]["label"], "直の残り5分(自動で確定)")
        self.assertIn("5分を切って", views["auto_closed"]["when"])
        # 並びは残り時間の2つが続く
        keys = list(views)
        self.assertEqual(keys.index("auto_closed"), keys.index("print_reminder") + 1)
        # 分は設定の値(`print_warning_minutes` / `auto_print_minutes`)から入る
        self.assertEqual((SETTINGS.print_warning_minutes, SETTINGS.auto_print_minutes), (15, 5))
        from nippou.logic import sound

        label, when = sound.texts(sound.spec("print_reminder"), warn_minutes=20, auto_minutes=3)
        self.assertEqual(label, "直の残り20分(終わりの催促)")
        self.assertIn("20分を切った", when)
        label, _when = sound.texts(sound.spec("auto_closed"), warn_minutes=20, auto_minutes=3)
        self.assertEqual(label, "直の残り3分(自動で確定)")

    def test_残り5分で確定した回だけ添える(self) -> None:
        from nippou.services import shift_close
        from nippou.services.shift_close import ShiftCloseResult

        body = self.post("/api/entry/close").get_json()
        self.assertFalse(body["ran"])                     # ふつうは「まだ早い」
        self.assertNotIn("sound_cue", body)
        ran = ShiftCloseResult(ran=True, report_date="2026年10月2日", line="L-1",
                               shift="1直", minutes_left=4, pages=1)
        with patch.object(shift_close, "run", return_value=ran):
            body = self.post("/api/entry/close").get_json()
        self.assertTrue(body["ran"])
        self.assertEqual(body["sound_cue"], "auto_closed")

    def test_選べば鳴らせる(self) -> None:
        _write_wav(self.snd / "あと5分.wav")
        self.choose(auto_closed="あと5分.wav")
        self.assertIn("auto_closed", self.cues())

    def test_見張りの通信でも出来事を知らせる(self) -> None:
        """残り5分の確定は1分ごとの見張り(誰も押していない通信)が叩く。"""
        api = (JS / "api.js").read_text(encoding="utf-8")
        ok = api[api.index("throw error;"):]
        ok = ok[:ok.index("return body;")]
        self.assertIn('if (body && body.sound_cue) announce("sound:cue", body.sound_cue);', ok)
        self.assertNotIn("quiet", ok)
        shift_end = (JS / "shift_end.js").read_text(encoding="utf-8")
        self.assertIn('background.post("/api/entry/close"', shift_end)


class ShiftStartedTests(SoundEventCase):
    """直が始まった: 時計の直が**前に見た直から**進んだとき(起動して最初は数えない)。"""

    def due(self, when: datetime) -> dict:
        from app.routes import sound as sound_route

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return when

        with patch.object(sound_route, "datetime", Clock):
            return self.get("/api/sound/due").get_json()

    def test_直が進んだら1度だけ(self) -> None:
        _write_wav(self.snd / "はじまり.wav")
        self.choose(shift_started="はじまり.wav")
        first = self.due(datetime(2026, 10, 2, 8, 0))
        self.assertIsNone(first["play"])                 # 起動して最初の直は数えない
        self.assertIn("shift_started", first["cues"])
        self.assertIsNone(self.due(datetime(2026, 10, 2, 9, 0))["play"])
        moved = self.due(datetime(2026, 10, 2, 15, 30))
        self.assertEqual(moved["play"]["key"], "shift_started")
        self.assertIn("2直", moved["play"]["message"])
        self.assertTrue(moved["play"]["sound"])
        self.assertIsNone(self.due(datetime(2026, 10, 2, 16, 0))["play"])

    def test_鳴らさないなら文言も出さない(self) -> None:
        self.due(datetime(2026, 10, 2, 8, 0))
        self.assertIsNone(self.due(datetime(2026, 10, 2, 15, 30))["play"])


class DailyResultTests(SoundEventCase):
    """「本日の梱包結果」は全画面確認を開いたときに鳴らす(締めくくる直ごとに1回)。"""

    def cue(self) -> str:
        html = self.get("/graph/review").get_data(as_text=True)
        tag = html[html.index('id="review"'):]
        tag = tag[:tag.index(">")]
        return tag.split('data-sound-cue="')[1].split('"')[0]

    def test_開いたときに1度だけ(self) -> None:
        _write_wav(self.snd / "本日の梱包結果なのだ.wav")
        self.assertEqual(self.cue(), "daily_result")
        self.assertEqual(self.cue(), "")                 # 同じ直ではもう鳴らさない

    def test_鳴らせないときは空(self) -> None:
        self.assertEqual(self.cue(), "")                 # ファイルが無い
        _write_wav(self.snd / "本日の梱包結果なのだ.wav")
        self.choose(daily_result="鳴らさない")
        self.assertEqual(self.cue(), "")


class WiringTests(unittest.TestCase):
    """画面の配線(ブラウザが無くても見られるもの)。"""

    def test_応答の出来事と断りを知らせる(self) -> None:
        api = (JS / "api.js").read_text(encoding="utf-8")
        self.assertIn('if (body && body.sound_cue) announce("sound:cue", body.sound_cue);', api)
        self.assertIn('announce("api:failed"', api)
        # 見張りの通信(誰も押していない)と自動保存(mute)では断りを鳴らさない
        failed = api[api.index("if (!res.ok)"):]
        failed = failed[:failed.index("throw error;")]
        self.assertIn("else if (!quiet && !mute)", failed)
        entry = (JS / "views" / "entry.js").read_text(encoding="utf-8")
        auto = entry[entry.index("async function maybeAutosave"):]
        auto = auto[:auto.index("\n}\n")]
        self.assertIn("{ mute: true }", auto)

    def test_鳴らせる出来事だけ鳴らす(self) -> None:
        js = (JS / "sound.js").read_text(encoding="utf-8")
        play = js[js.index("export async function playKey"):]
        play = play[:play.index("\n}\n")]
        self.assertIn("if (cues && !cues.has(key)) return false;", play)
        self.assertIn("setCues(body.cues);", js)
        failed = js[js.index("function onFailed"):]
        failed = failed[:failed.index("\n}\n")]
        # 決まった音がある断り・聞き返しは「断られた」にしない
        self.assertIn("if (body?.sound) return;", failed)
        self.assertIn("body?.shift_changed?.crossed || body?.needs_confirm", failed)
        self.assertIn('cue("error")', failed)
        self.assertIn('cue("refused")', failed)

    def test_画面のエラーと全画面確認(self) -> None:
        errors = (JS / "errors.js").read_text(encoding="utf-8")
        self.assertIn('new CustomEvent("sound:cue", { detail: "error" })', errors)
        review = (JS / "views" / "review.js").read_text(encoding="utf-8")
        self.assertIn('cue(document.getElementById("review")?.dataset.soundCue);', review)

    def test_設定を保存したら一覧を読み直す(self) -> None:
        js = (JS / "views" / "settings.js").read_text(encoding="utf-8")
        self.assertIn("loadCues();", js)
        self.assertIn("previewFile(name)", js)


if __name__ == "__main__":
    unittest.main()
