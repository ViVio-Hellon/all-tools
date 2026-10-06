"""音を鳴らすかどうかを決める (VBA ``音楽を流す`` + ``soundPlayed`` の見張り)

【鳴らすのはブラウザ、決めるのはここ】
tkinter版は winsound の PlaySound でサーバ側(=Pythonのプロセス)から
鳴らしていました。Web版でそれをやると、**`pythonw` のセッションで鳴らす**
ことになります ── 画面を見ている人に届く保証がなく、そもそも音が出る
デバイスがそのセッションに割り当たっているとは限りません。

そこで役割を分けます。

    ここ(サーバ)  … 「どの音を、いま鳴らすべきか」を決める
    ブラウザ      … 実際に鳴らす(`<audio>`)

【二重に鳴らさない】
VBA はモジュール変数 `soundPlayed As Boolean` で「もう鳴らした」を
覚えていました。同じ判断が1分ごとに真になるので、押さえないと1分おきに
鳴り続けます。ここでも同じ見張りを持ちますが、**プロセスが覚えます** ──
画面側に持たせると、タブを開き直すたびにまた鳴ります。

【自動再生の制限は、ここでは扱わない】
ブラウザは「利用者がそのページを一度も触っていない」あいだ、音を鳴らせ
ません。それはブラウザ側の事情なので `app/static/js/sound.js` が扱い、
鳴らせなかったときは画面に出します(音は補助で、文言が本体)。

【出来事の一覧 (v4.4.0)】
音は「出来事」ごとに1つ持ちます(`SOUNDS`)。**どの出来事で何を鳴らすかは
設定画面で選びます** ── 「鳴らさない」か、音声フォルダのファイルか。

    音声ファイルを追加したいタイミングが増えた場合どうしたらいいですか？
    → 一覧にある出来事なら、設定画面で選ぶだけ(コードは触らない)

前からある5つ(VBA と同じ既定のファイル)に、v4.4.0 で5つ足しました
(保存した・共有へ保存した・直が始まった・断られた・エラーが起きた)。
v4.5.0 で「直の残り5分(自動で確定)」を足しました(残り15分は前からある
「終わりの催促」)。**足した出来事の既定は「鳴らさない」** ── 入れただけで
現場の音が増えないように。
一覧に無い出来事(新しいチェックなど)を足すときだけ、ここに1行と、鳴らす
きっかけ(下の「どこで鳴らすか」)を足します。

    どこで鳴らすか
      時刻で決まるもの … `/api/sound/due`(1分ごとに画面が訊きに来る)
      押した操作の結果 … 応答に `sound_cue` を付ける(`static/js/api.js` が鳴らす)
      残り5分の確定   … `/api/entry/close`(1分ごとの見張り)の応答に `sound_cue`
      保存前のチェック … 断りの `sound`(`logic/save_checks.py`)
      断り・エラー     … 画面が応答の形から決める(`static/js/sound.js`)
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional

from ..logging_setup import get_logger

log = get_logger("sound")

# 鳴らす音の種類(出来事)。**鍵とファイル名は分けてある** ── 名前は現場で
# 差し替えられる(設定画面)ので、コードが名前を持つと変えられない
KEY_PRINT_REMINDER = "print_reminder"
KEY_DAILY_RESULT = "daily_result"
KEY_NEGATIVE_TIME = "negative_time"
# 保存前のチェックで鳴らす2つ(`logic/save_checks.py`)。VBA も
# `Number_Count` / `CheckAndColorCells_NotExist` の中で鳴らしていた
KEY_PACK_OVER = "pack_over"
KEY_UNKNOWN_STOP = "unknown_stop"
# v4.4.0 で足した出来事。**既定は鳴らさない**
KEY_AUTO_CLOSED = "auto_closed"          # v4.5.0 直の残り5分(自動で確定)
KEY_SAVED = "saved"
KEY_PUSHED = "pushed"
KEY_SHIFT_STARTED = "shift_started"
KEY_REFUSED = "refused"
KEY_ERROR = "error"

#: 音声ファイルとして受け付ける拡張子。**配る口(`/sound/…`)・設定の欄・
#: フォルダの一覧はこれ1つを見ます**
AUDIO_SUFFIXES: tuple[str, ...] = (".wav", ".mp3", ".ogg", ".m4a", ".aac", ".flac")

#: 「鳴らさない」を選んだときに設定へ入れる字。**拡張子が無いので、
#: ファイル名とは混ざりません**(音声ファイルは拡張子で受け付ける)
SOUND_OFF = "鳴らさない"


@dataclass(frozen=True)
class SoundSpec:
    """1つの出来事の決めごと。**文言も持つ。**

    鳴らせなかったとき(自動再生の制限・ファイルが無い)に画面へ出すのは
    この文言です ── 音は補助で、伝えたいことは文字のほうにあります。
    """

    key: str
    label: str          # 設定画面に出す名前(出来事)
    default_file: str   # 既定のファイル名。**空なら既定は「鳴らさない」**
    message: str        # 鳴らせないときに画面へ出す文言
    when: str = ""      # いつ鳴るか(設定画面の説明)
    #: 音を鳴らさないと決めてあっても、文言は画面に出す(催促のように、
    #: 伝えること自体に意味があるもの)
    tell_without_sound: bool = False


SOUNDS: tuple[SoundSpec, ...] = (
    # ---- 前からある5つ。鍵と既定のファイル名は VBA のまま(現場にその .wav がある)
    # 文言だけ直しました ── 紙は任意になったので、催促しているのは印刷ではなく
    # 「直が終わる前に日報を確かめること」です
    # **直の残り時間で鳴るのは2つ**(`{warn}` / `{auto}` は設定の分 ──
    # `print_warning_minutes` / `auto_print_minutes`。名前と説明に入れて出す)
    SoundSpec(KEY_PRINT_REMINDER, "直の残り{warn}分(終わりの催促)",
              "印刷忘れにご注意.wav", "直が終わります。日報の入力をお確かめください",
              when="直の残りが{warn}分を切ったとき、まだ確定していなければ(直に1回)",
              tell_without_sound=True),
    SoundSpec(KEY_AUTO_CLOSED, "直の残り{auto}分(自動で確定)", "",
              "直の終わりが近いので、打ってあるぶんを確定しました",
              when="直の残りが{auto}分を切って、打ってあるぶんを自動で確定したとき"
                   "(直に1回。もう確定してあれば鳴りません)"),
    SoundSpec(KEY_DAILY_RESULT, "本日の梱包結果",
              "本日の梱包結果なのだ.wav", "本日の梱包結果を確認してください",
              when="直の終わりの全画面確認(実績のグラフ)を開いたとき(直に1回)"),
    SoundSpec(KEY_NEGATIVE_TIME, "作業時間がマイナス",
              "作業時間がマイナスです.wav", "作業時間がマイナスです",
              when="打った行の作業時間がマイナスになったとき"),
    SoundSpec(KEY_PACK_OVER, "梱包数が検入枚数より多い",
              "梱包数が多い.wav", "梱包数が検入枚数を超えています",
              when="保存の前のチェックで、梱包数が検入枚数より多い行があったとき"),
    SoundSpec(KEY_UNKNOWN_STOP, "停止内訳にない記号",
              "登録に存在しない停止.wav", "停止内訳にない記号があります",
              when="保存の前のチェックで、停止内訳にない記号があったとき"),
    # ---- v4.4.0 で足した出来事。**既定は鳴らさない**(選んだ端末だけ鳴る)
    SoundSpec(KEY_SAVED, "保存した", "", "保存しました",
              when="日報を「保存(確定)」したとき(自動保存では鳴らしません)"),
    SoundSpec(KEY_PUSHED, "共有へ保存した", "", "共有へ保存しました",
              when="「共有へ保存」が済んだとき(送れなかったページがあれば鳴らしません)"),
    SoundSpec(KEY_SHIFT_STARTED, "直が始まった", "", "直が変わりました",
              when="開いているあいだに、時計の直が次の直へ進んだとき(直に1回)"),
    SoundSpec(KEY_REFUSED, "断られた", "", "断られました",
              when="押した操作が断られたとき(打ち間違い・関門など)。"
                   "上の決まった音(梱包数・停止記号)があるときはそちらを鳴らします"),
    SoundSpec(KEY_ERROR, "エラーが起きた", "", "エラーが起きました",
              when="思わぬエラーが起きたとき(エラー番号が出るもの・保存に失敗したとき)"),
)

SOUND_KEYS: tuple[str, ...] = tuple(s.key for s in SOUNDS)


def texts(spec: SoundSpec, *, warn_minutes: int, auto_minutes: int) -> tuple[str, str]:
    """画面に出す名前と「いつ鳴るか」。**分は設定の値を入れる**(15分・5分)。"""
    fill = {"warn": warn_minutes, "auto": auto_minutes}
    return spec.label.format(**fill), spec.when.format(**fill)

_BY_KEY = {s.key: s for s in SOUNDS}


def spec(key: str) -> Optional[SoundSpec]:
    return _BY_KEY.get(key)


class SoundGate:
    """「もう鳴らした」を覚える見張り (VBA ``soundPlayed``)。

    同じ判断(直の終わりが近い等)は1分ごとに真になり続けるので、
    押さえないと鳴り続けます。**直が変われば忘れます** ── 鍵に直を
    混ぜるのは呼び出し側の仕事で、ここは渡された鍵をそのまま覚えます。
    """

    def __init__(self) -> None:
        self._played: set[str] = set()
        self._lock = threading.Lock()

    def take(self, key: str) -> bool:
        """まだ鳴らしていなければ True(そして鳴らしたことにする)。"""
        with self._lock:
            if key in self._played:
                return False
            self._played.add(key)
        log.info("音を鳴らします: %s", key)
        return True

    def forget(self, key: Optional[str] = None) -> None:
        """覚えを捨てる。直が変わったときと、テストで使う。"""
        with self._lock:
            if key is None:
                self._played.clear()
            else:
                self._played.discard(key)

    def already(self, key: str) -> bool:
        with self._lock:
            return key in self._played


# プロセスに1つ。**画面側に持たせない** ── タブを開き直すたびに鳴る
_gate = SoundGate()


def gate() -> SoundGate:
    return _gate


class ShiftWatch:
    """**直が変わったのを見たか**(「直が始まった」の出来事)。

    覚えるのはプロセスです(画面が訊きに来るたびに見る)。**起動して最初に
    見た直は数えません** ── 起動しただけで「直が始まった」と鳴ると、
    直の途中で開いた人を驚かせます。
    """

    def __init__(self) -> None:
        self._last: Optional[tuple[str, str]] = None
        self._lock = threading.Lock()

    def changed(self, key: tuple[str, str]) -> bool:
        with self._lock:
            before, self._last = self._last, key
        return before is not None and before != key

    def forget(self) -> None:
        with self._lock:
            self._last = None


_shift_watch = ShiftWatch()


def shift_watch() -> ShiftWatch:
    return _shift_watch


def reset() -> None:
    """テスト用。"""
    _gate.forget()
    _shift_watch.forget()
