"""音 (`/api/sound/*`, `/sound/<key>`)

【役割の分け方】
    ここ(サーバ)  … 「どの音を、いま鳴らすべきか」を決める
    ブラウザ      … 実際に鳴らす(`app/static/js/sound.js`)

tkinter版は `winsound` でサーバ側から鳴らしていました。Web版でそれを
やると `pythonw` のセッションで鳴らすことになり、画面を見ている人に
届く保証がありません。**判断はサーバ、再生はブラウザ**に分けます。

【音は補助】
ブラウザは「利用者がそのページを一度も触っていない」あいだ音を鳴らせ
ません。鳴らせなかったときのために、どの音にも**文言**が付いています
── 伝えたいことは文字のほうにあり、音はそれを気づかせるためのものです。
"""
from __future__ import annotations

import mimetypes
from datetime import datetime

from flask import Blueprint, jsonify, send_file

from nippou import work_context
from nippou.config import SETTINGS
from nippou.logging_setup import get_logger
from nippou.logic import sound
from nippou.services import sound_files

from .. import error_body

log = get_logger("app.routes.sound")

bp = Blueprint("sound", __name__)

# 配ってよい拡張子。**ここに無いものは配らない** ── `/sound/<key>` は
# 設定で決まった道のファイルを返すので、何でも配れる口にしない
ALLOWED_SUFFIXES = frozenset(sound.AUDIO_SUFFIXES)


@bp.get("/api/sound/state")
def state():
    """出来事ごとの音の状態(設定画面用)。**あるか・鳴らせるか。**"""
    files, problem = sound_files.folder_files()
    return jsonify({"sounds": [_describe(s) for s in sound.SOUNDS],
                    "dir": str(SETTINGS.sound_dir),
                    "files": files, "dir_error": problem})


@bp.get("/api/sound/cues")
def cues():
    """**いま鳴らせる出来事。** 画面はここに入っているものだけ鳴らします。

    1分ごとの見張り(`/api/sound/due`)も同じものを返します。ここは設定を
    保存した直後など、待たずに読み直したいとき用です。
    """
    return jsonify({"cues": sound_files.playable_keys()})


def _describe(spec: sound.SoundSpec) -> dict:
    path = SETTINGS.sound_path(spec.key)
    label, when = sound_files.texts(spec)
    out = {
        "key": spec.key, "label": label, "message": spec.message,
        "when": when, "default_file": spec.default_file,
        "file": SETTINGS.sound_file(spec.key),
        "off": not SETTINGS.sound_file(spec.key),
        "path": str(path) if path else "",
        "exists": False, "size": 0, "playable": False, "error": "",
    }
    if path is None:
        out["error"] = "鳴らしません"
        return out
    try:
        out["exists"] = path.is_file()
        if out["exists"]:
            out["size"] = path.stat().st_size
    except OSError as exc:                           # 共有に届かない
        out["error"] = str(exc)
        return out
    if not out["exists"]:
        # **無くても止めない。** 音が鳴らないだけで、文言は画面に出る
        out["error"] = "ファイルがありません(音は鳴らず、画面の文言だけになります)"
        return out
    if path.suffix.lower() not in ALLOWED_SUFFIXES:
        out["error"] = ("この拡張子は配れません: "
                        + " / ".join(sorted(ALLOWED_SUFFIXES)))
        return out
    out["playable"] = True
    return out


@bp.get("/sound/<key>")
def play(key: str):
    """音声ファイルそのもの。`<audio>` が読む。

    `/sound/` はトークンが要る経路(`app/__init__.py`)。設定で決まった
    フォルダのファイルを返すので、**出来事の鍵で決まるもの以外は配りません**
    ── 道を受け取る形にすると、そこが読み取り口になる。
    """
    spec = sound.spec(key)
    if spec is None:
        return jsonify(error_body("not_found", "その音はありません")), 404

    path = SETTINGS.sound_path(key)
    if path is None or not path.is_file():
        return jsonify(error_body("no_file", "音声ファイルがありません")), 404
    if path.suffix.lower() not in ALLOWED_SUFFIXES:
        return jsonify(error_body("bad_type", "この拡張子は配れません")), 415

    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return send_file(path, mimetype=mime, conditional=True)


@bp.get("/sound/preview")
def preview_file():
    """試聴。**音声フォルダの中の音声ファイルだけ**を名前で返す。

    設定画面で選んだファイルを、保存する前に聞くためのものです。道は
    受け取りません ── 名前1つ(フォルダの区切りを含まない・音声の拡張子)
    だけで、音声フォルダの外は指せません(`services/sound_files.file_path`)。
    `/sound/` の下なのでトークンが要ります(`/sound/<key>` より先に当たる)。
    """
    from flask import request

    path = sound_files.file_path(request.args.get("name", ""))
    if path is None:
        return jsonify(error_body("no_file", "その音声ファイルはありません")), 404
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return send_file(path, mimetype=mime, conditional=True)


@bp.get("/api/sound/due")
def due():
    """いま鳴らすべき音。無ければ `null`。**鳴らせる出来事の一覧も一緒に。**

    **二重に鳴らさない判断もここでする**(`SoundGate`)。同じ判断は1分
    ごとに真になり続けるので、押さえないと1分おきに鳴ります。画面側に
    覚えさせると、タブを開き直すたびに鳴ります。
    """
    from .entry import build_service, current_calculator

    ctx = work_context.get_context()
    calc = current_calculator()
    service = build_service(ctx, calc)
    now = datetime.now()
    current_shift, current_date = service.current_shift_info(
        now, ctx.force_day_shift())
    playable = sound_files.playable_keys()

    for spec, note in _pick(now, ctx, calc, current_date, current_shift):
        on = spec.key in playable
        # **鳴らさないと決めてある出来事は、文言も出さない**(催促は別 ──
        # 伝えること自体に意味がある)。見張りの覚えも使わない
        if not on and not spec.tell_without_sound:
            continue
        # **鍵に直を混ぜる。** 直が変われば、また鳴ってよい
        if not sound.gate().take(f"{spec.key}:{current_date}:{current_shift}"):
            continue
        log.info("音の出番です: %s (%s)", spec.key, note)
        return jsonify({"cues": playable, "play": {
            "key": spec.key,
            # **文言も一緒に返す。** 鳴らせなくても伝わるように
            "message": note or spec.message,
            "url": f"/sound/{spec.key}",
            "sound": on,
        }})
    return jsonify({"cues": playable, "play": None})


def _pick(now, ctx, calc, current_date: str, current_shift: str):
    """いま鳴らすかもしれない出来事と、その文言(優先する順)。

    判断そのものは `logic/shift.py` / `logic/sound.ShiftWatch` が持つ ──
    ここは「どの出来事に対応するか」を結び付けるだけ。
    """
    from nippou.logic.shift import ShiftCloseChecker

    from .. import get_repo

    found = []
    # **直が始まった。** 時計の直が前に見たものから進んだとき(起動して
    # 最初に見た直は数えない)。呼出中でも覚えは進める(戻ったときに鳴らない)
    if sound.shift_watch().changed((current_date, current_shift)) \
            and not ctx.recall.active:
        found.append((sound.spec(sound.KEY_SHIFT_STARTED),
                      f"{current_date} {current_shift} が始まりました"))

    # 呼出モード中は催促しない。過去のデータを見ているだけなので、
    # 「いまの直が終わる」の催促は当てはまらない
    if ctx.recall.active:
        return found

    repo = get_repo()
    checker = ShiftCloseChecker(
        calc,
        is_shift_closed=lambda shift, day: repo.is_shift_closed(shift, day, ctx.line),
        warn_minutes=SETTINGS.print_warning_minutes,
        auto_close_minutes=SETTINGS.auto_print_minutes)
    if checker.close_forgotten(now, ctx.force_day_shift()):
        found.insert(0, (sound.spec(sound.KEY_PRINT_REMINDER),
                         checker.realtime_warning_message(now, ctx.force_day_shift())))
    return found
