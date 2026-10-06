"""打てるタブを1枚に絞る (`nippou/logic/tab_lock.py`)

    POST /api/tab/claim    このタブが開いた。打てるか見るだけかを返す
    POST /api/tab/ping     心拍(5秒ごと)。役割が変わっていれば返す
    POST /api/tab/take     「このタブで入力する」を押した
    POST /api/tab/hide     タブが裏に回った(`sendBeacon`)。権利は持ったまま
    POST /api/tab/release  タブを閉じた(`sendBeacon`)

claim の本文 `{"loaded": 秒}` は、その画面を描いた時刻(`entry.html` の
`data-tab-loaded`)。**ほかのタブが書いたあとに描かれた画面か**を見るのに
使います(`TabDesk.stale`)。

claim / ping の本文 `{"hidden": true}` は「いま裏に回っている」。ブラウザが
間引いた心拍でも居ることは伝わりますが、空いた権利は拾いません
(`nippou/logic/tab_lock.py`)。

時刻は**スリープを数えない時計**で渡します(`nippou/awake_clock.py`)。

【なぜ画面を無効にするだけでは足りないか】
無効にしたのは**見た目**で、要求そのものは止まっていません。戻る/進む、
開きっぱなしの古いタブ、二重送信 ── どれも無効化をすり抜けます。だから
書き込みの口(`app/__init__.py` の `_register_tab_guard`)が最後に見ます。
ここはその判断を画面へ知らせるためだけの口です。
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from nippou import awake_clock
from nippou.logging_setup import get_logger
from nippou.logic import tab_lock

from .. import error_body

log = get_logger("app.routes.tab")

bp = Blueprint("tab", __name__)


def tab_token() -> str:
    """要求に付いてきたタブの名札(`api.js` が全部の要求に付ける)。"""
    return (request.headers.get("X-Tab", "") or "").strip()[:80]


def _answer(verdict) -> tuple:
    return jsonify(verdict.as_dict()), 200


def _loaded():
    """画面を描いた時刻(`entry.html` の `data-tab-loaded`)。無ければ None。"""
    raw = (request.get_json(silent=True) or {}).get("loaded")
    try:
        return float(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _hidden() -> bool:
    """本文の `hidden`(裏に回ったタブの心拍)。"""
    return bool((request.get_json(silent=True) or {}).get("hidden"))


def _beacon_token() -> str:
    """`sendBeacon` から来たときの名札。**ヘッダは一切付けられない**ので本文から拾う。"""
    token = tab_token()
    if not token:
        body = request.get_json(silent=True) or {}
        token = str(body.get("tab", "")).strip()[:80]
    return token


@bp.post("/api/tab/claim")
def claim():
    """タブが開いた。**空いていれば打てる、居れば見るだけ。**"""
    token = tab_token()
    if not token:
        return jsonify(error_body("no_tab", "タブを見分けられませんでした")), 400
    verdict = tab_lock.get_desk().claim(token, now=awake_clock.now(),
                                        hidden=_hidden(), loaded=_loaded())
    if not verdict.may_edit:
        log.info("2枚目のタブが開きました(見るだけ): others=%s", verdict.others)
    return _answer(verdict)


@bp.post("/api/tab/ping")
def ping():
    """心拍。**役割が変わっていれば、ここで画面が気づく。**"""
    token = tab_token()
    if not token:
        return jsonify(error_body("no_tab", "タブを見分けられませんでした")), 400
    return _answer(tab_lock.get_desk().ping(token, now=awake_clock.now(),
                                            hidden=_hidden()))


@bp.post("/api/tab/take")
def take():
    """「このタブで入力する」。**押したときだけ動かす。**

    勝手に移すと、打っている最中に後ろのタブへ権利が移ることがあります。
    """
    token = tab_token()
    if not token:
        return jsonify(error_body("no_tab", "タブを見分けられませんでした")), 400
    log.info("入力するタブが移りました")
    return _answer(tab_lock.get_desk().take(token, now=awake_clock.now()))


@bp.post("/api/tab/hide")
def hide():
    """タブが裏に回った。**権利は持ったまま、心拍が止まっても外さない。**

    裏に回ったタブのタイマーはブラウザが間引く・止めます。黙っていると
    15秒で「閉じられた」とみなされ、押してもいないのに権利がほかの
    タブへ移ります。隠れる瞬間なので `sendBeacon` で送られてきます
    (名札は本文、合言葉はクエリ)。応答は誰も読みません。
    """
    token = _beacon_token()
    if token:
        tab_lock.get_desk().hide(token, now=awake_clock.now())
    return jsonify({"ok": True})


@bp.post("/api/tab/release")
def release():
    """タブを閉じた。**待たずに次へ渡す。**

    閉じ際なので `navigator.sendBeacon` で送られてきます。あれは
    **ヘッダを一切付けられない**ので、名札はヘッダではなく本文から
    拾います(合言葉のほうはクエリで受けます)。応答は誰も読みません。
    """
    token = _beacon_token()
    if token:
        tab_lock.get_desk().release(token, now=awake_clock.now())
    return jsonify({"ok": True})
