"""看板の画面と操作 (tkinter 版の ``material_form`` / ``warehouse_form`` /
``warehouse_view`` をまとめたもの)

3つのモードで**同じ盤面**を出し、操作できる範囲だけが変わる
(:mod:`kanban.presenters.board` の ``BoardPolicy``)。

【断りの返し方】
HTTP のステータスで種類を分ける。文言から推し量らせない:

* ``400`` 入力の形が違う。**サーバの状態は動いていない**
* ``403`` そのモードではできない操作
* ``409`` 別の端末が先に変えていた(``rev`` の楽観ロック)
* ``422`` 形は正しいが業務として断る(発注が無いのに発送、など)
* ``503`` DB が混み合っていて今は書けない

**応答は必ず更新後の盤面ぜんぶを返す**(差分ではない)。この規模なら数 KB で
済み、画面側の不整合バグの温床を作らないほうが得。
"""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify, render_template, request

from kanban import config
from kanban.applog import get_logger
from kanban.db.store import ConflictError, LockTimeout
from kanban.domain.models import (
    CANCELLED_WHILE_SHIPPING,
    CancelledWhileShippingError,
    DomainError,
    OrderMissingError,
)
from kanban.presenters import board as presenter

from .. import current_line, current_mode, get_service, get_store
from ..shell import shell_context

log = get_logger("app.routes.board")

#: 読むだけの経路。どのモードでも登録する
bp = Blueprint("board", __name__)
#: 状態を変える経路。**倉庫参照モードでは登録しない**
write_bp = Blueprint("board_write", __name__)


# ------------------------------------------------------------------
# ライン
# ------------------------------------------------------------------
def visible_lines() -> list[str]:
    """このモードで見えるライン。

    現場は担当ライン1本だけ。倉庫・倉庫参照はマスタの ``倉庫対象`` が立って
    いるライン全部。いずれも**実際に取り込めているものだけ**に絞る ──
    Access に無いラインのタブを出しても空の画面が出るだけで、利用者には
    壊れて見える。
    """
    store = get_store()
    available = set(store.imported_lines())
    mode = current_mode()
    if mode == config.MODE_SITE:
        line = current_line()
        return [line] if line in available else []
    return [code for code in config.warehouse_line_codes() if code in available]


def _resolve_line(requested: str) -> str | None:
    """要求されたラインが、このモードで見てよいものか確かめる。

    画面で絞るだけでなくサーバでも確かめる。現場モードのプロセスに別ラインの
    ラインコードを送られても、担当外のラインは触らせない。
    """
    lines = visible_lines()
    if not lines:
        return None
    if not requested:
        return lines[0]
    return requested if requested in lines else None


# ------------------------------------------------------------------
# 画面
# ------------------------------------------------------------------
@bp.get("/")
def home():
    """``/`` は看板へ。起動直後にここへ入る。"""
    from flask import redirect

    token = request.args.get("t", "")
    return redirect(f"/board?t={token}" if token else "/board")


@bp.get("/board")
def page():
    lines = visible_lines()
    mode = current_mode()
    line = _resolve_line(request.args.get("line", "")) or ""

    stamp = get_store().state_stamp()
    view = presenter.build(get_service(), line, mode) if line else None
    store = get_store()
    # ``shell_context`` は現場モードの担当ラインを ``line`` として渡す。
    # 盤面がいま表示しているラインは倉庫モードだとタブで変わるので、
    # 名前を分けて渡す(同じ名前で2度渡すと Flask が TypeError を出す)
    return render_template(
        "board.html",
        lines=[{"code": c, "label": config.display_name(c), "current": c == line} for c in lines],
        shown_line=line,
        state=board_dict(view, stamp) if view else None,
        pending=store.pending_count(),
        **shell_context("board"),
    )


@bp.get("/api/board")
def board():
    """盤面ぜんぶ。``?line=`` で指定する。"""
    line = _resolve_line(request.args.get("line", ""))
    if line is None:
        return jsonify(_err("no_line", "表示できるラインがありません")), 404
    return jsonify(_payload(line))


@bp.get("/api/status")
def status():
    """帯に出す軽い状態。定期的に叩かれるので盤面は含めない。

    **ここは手元の SQLite しか見ません。** 数秒おきに叩かれる経路なので、
    共有フォルダへ行くものを混ぜてはいけません(「いま誰が開いているか」も
    取り込み済みの写しから作ります)。
    """
    from kanban import presence

    store = get_store()
    mine = presence.registered_line(current_mode(), current_line() or "")
    age, stale, at = _import_freshness(store)
    return jsonify(
        {
            "pending": store.pending_count(),
            "failures": len(store.sync_failures()),
            **_undelivered(store),
            "token": store.change_token(),
            "presence": presence.to_dicts(presence.read(store, exclude=mine)),
            "last_import_at": at,
            "import_age_sec": age,
            "import_stale": stale,
        }
    )


def _undelivered(store) -> dict:
    """共有へ届いていないもの(帯「⚠ N 件がまだ共有に届いていません」)。

    **送れていないコメントも数える。** 以前は看板の状態(``pending``)だけを見て
    いたので、共有フォルダが見えないあいだに書いたコメントは、相手に届いていない
    のに帯にも出なかった。出来事(看板履歴)は相手の画面には出ない集計の記録なので
    別に数える。いつから・なぜは書き戻しが手元に覚えたもの(ここで共有フォルダは
    見に行かない)。
    """
    rows = store.pending_count()
    comments = store.unsent_comment_count()
    waiting = rows + comments
    return {
        "undelivered": waiting,
        "undelivered_comments": comments,
        # 書き戻しが送ってみて残ったときだけ数える(押した直後の数秒は出さない)
        "undelivered_events": (store.unsent_event_count()
                               if store.get_meta("undelivered_events_since", "") else 0),
        "undelivered_since": store.get_meta("undelivered_since", "") if waiting else "",
        "undelivered_why": store.get_meta("undelivered_why", "") if waiting else "",
    }


#: 取り込み間隔の何倍まで待つか。1 回や 2 回の失敗で騒がない
#: (共有フォルダは瞬間的に落ちることがある)。
STALE_IMPORT_FACTOR = 4
#: それでも下限は置く。取り込み間隔が短い端末で、すぐ警告が出ないように
STALE_IMPORT_FLOOR_SEC = 150


def _import_freshness(store) -> tuple[float | None, bool, str]:
    """最後に**取り込めた**のはいつか。``(経過秒, 古いか, その時刻)``

    **自動更新は、動かなくなったことが見えて初めて信用できます。**
    共有フォルダが落ちていると、画面は元気に動いているのに中身だけ古い
    まま ── VBA 版の「更新できていないのに気付かない」と同じ形です。
    取り込めていない時間が延びたら、帯でそう言います。

    ``last_import_at`` は **取り込みに成功したときだけ**書かれるので、
    これが伸びていくこと自体が「届いていない」の証拠になります。
    """
    from datetime import datetime

    from kanban.domain.models import parse_datetime

    cfg = current_app.config
    interval = int(cfg.get("IMPORT_INTERVAL_SEC") or 0)
    if interval <= 0:
        # 定期取り込みをしない端末(手動運用)。ここで警告しても直しようがない
        return None, False, store.get_meta("last_import_at", "")

    at = store.get_meta("last_import_at", "")
    limit = max(interval * STALE_IMPORT_FACTOR, STALE_IMPORT_FLOOR_SEC)
    parsed = parse_datetime(at)
    if parsed is None:
        # **一度も取り込めていない。** 起動時の取り込みは画面を出す前に
        # 終わるので、ここに来るのは本当に失敗しているとき
        return None, True, at
    age = max(0.0, (datetime.now() - parsed).total_seconds())
    return round(age, 1), age > limit, at


# ------------------------------------------------------------------
# 操作
# ------------------------------------------------------------------
@write_bp.post("/api/board/order")
def order():
    """サイズ(資材)ボタン。**現場モードだけ。**

    どのラインで押せるかは :class:`~kanban.presenters.board.BoardPolicy` が
    決める(現場は ``発送無効`` のラインだけサイズを押せる)。発送ボタンと
    同じく**ここでも判断を引き直す** ── 画面が出したボタンだけを信じると、
    URL を直接叩かれたときに素通しになる。
    """
    if current_mode() != config.MODE_SITE:
        return _wrong_mode(config.MODE_SITE)
    return _apply_guarded(
        "size",
        lambda svc, line, no, rev: svc.toggle_order(
            line, no, expected_rev=rev,
            treat_as_delivered=bool((request.get_json(silent=True) or {}).get("delivered")),
        ),
    )


@write_bp.post("/api/board/ship")
def ship():
    """発送ボタン。現場は ``発送無効`` でないラインのみ、倉庫は常に。

    どのラインで押せるかは :class:`~kanban.presenters.board.BoardPolicy` が
    決めている。ここでも**同じ判断を引き直して**確かめる ── 画面が出した
    ボタンだけを信じると、URL を直接叩かれたときに素通しになる。
    """
    return _apply_guarded("ship", lambda svc, line, no, rev: svc.toggle_ship(line, no, expected_rev=rev))


@write_bp.post("/api/board/hold")
def hold():
    """注文中ボタン。**倉庫モードだけ。**

    理由入力も確認ダイアログも無く、即座に切り替わる(VBA ``cls_HoldButton``)。
    """
    if current_mode() != config.MODE_WAREHOUSE:
        return _wrong_mode(config.MODE_WAREHOUSE)
    # **ポリシーを引き直す。** 画面が出したボタンだけを信じると、URL を直接
    # 叩かれたときに素通しになる。``保留`` 列を持たない看板テーブルでは、
    # ここで断らないと**共有DBへ一生届かない操作**を受け付けてしまう
    return _apply_guarded(
        "hold", lambda svc, line, no, rev: svc.toggle_hold(line, no, expected_rev=rev)
    )


@write_bp.post("/api/board/acknowledge")
def acknowledge():
    """「発送処理中に注文が取り消されました」を確かめた。**現場モードだけ。**

    倉庫が発送したのを取り込む前に現場が赤を消すと、共有DBには「赤なし・
    緑あり」が残る(赤を消しても発送の印には触れない。VBA と同じ)。現場が
    資材を確かめて押すと、発送の印を消して元に戻す。サイズを押せるラインで
    だけ受け付ける(現場の画面に確認の道があるライン)。
    """
    if current_mode() != config.MODE_SITE:
        return _wrong_mode(config.MODE_SITE)
    return _apply_guarded(
        "size",
        lambda svc, line, no, rev: svc.acknowledge_cancelled_shipping(line, no, expected_rev=rev),
    )


# ------------------------------------------------------------------
# コメント(倉庫 ⇔ 現場のやり取り)
# ------------------------------------------------------------------
#: 1 件の長さの上限。看板の脇で読む一言なので、長文は受けない
COMMENT_MAX_LEN = 200


def _comment_target():
    """``(line, item)``。見てよいラインの、ある看板でなければ ``(None, 断り)``。"""
    body = request.get_json(silent=True) or {}
    line = _resolve_line(str(body.get("line", request.args.get("line", ""))))
    mgmt_no = str(body.get("mgmt_no", request.args.get("mgmt_no", ""))).strip()
    if line is None:
        return None, (jsonify(_err("no_line", "対象のラインがありません")), 400)
    item = get_store().item(line, mgmt_no) if mgmt_no else None
    if item is None:
        return None, (jsonify(_err("bad_request", "その看板がありません", "mgmt_no")), 400)
    return (line, item), None


def _write_rule(item, thread) -> tuple[bool, str]:
    """この端末(モード)がいまこの看板に書けるか(:mod:`kanban.domain.comments`)。"""
    from kanban.domain import comments as rules

    if current_mode() not in (config.MODE_SITE, config.MODE_WAREHOUSE):
        return False, "倉庫参照は読むだけです。"
    return rules.can_write(item, presenter.comment_side(current_mode()), thread["open"])


def _thread_reply(line: str, item) -> dict:
    from kanban.domain import comments as rules

    thread = get_store().comment_thread(line, item.mgmt_no)
    can, why = _write_rule(item, thread)
    side = presenter.comment_side(current_mode())
    return {
        "line": line, "line_label": config.display_name(line), "mgmt_no": item.mgmt_no,
        "material": item.material, "size": item.size,
        "open": thread["open"], "closed": thread["closed"],
        "side": side,
        "can_comment": can,
        # 書けない理由(段階が違う)と、書けるときに添える一言
        "cannot_reason": why,
        "hint": rules.write_hint(item, side, thread["open"]) if can else "",
        "max_len": COMMENT_MAX_LEN,
    }


@bp.get("/api/comments")
def comments():
    """その看板のやり取り。**開いたら既読にする。**

    現場・倉庫は、同じ側のほかの端末にも既読を知らせる(現場はそのラインの端末、倉庫は
    倉庫の端末。:meth:`kanban.db.store.Store.mark_comments_read`)。倉庫参照は見るだけ
    なので、この端末の中だけ既読にする(倉庫が読んだことにはしない)。
    """
    target, problem = _comment_target()
    if problem:
        return problem
    line, item = target
    mode = current_mode()
    share = presenter.comment_side(mode) if mode in (config.MODE_SITE, config.MODE_WAREHOUSE) else ""
    if get_store().mark_comments_read(line, item.mgmt_no, share_side=share):
        # 既読の印を積んだ。**すぐ送る**(同じ側の端末の未読の帯を早く消す)
        hook = current_app.config.get("ON_CHANGED")
        if hook:
            try:
                hook(line)
            except Exception:  # noqa: BLE001 - 送るのは次の周期でもよい
                log.exception("既読の即時送信の依頼に失敗")
    return jsonify({**_thread_reply(line, item), **_payload(current_line_or(line))})


@write_bp.post("/api/comments")
def post_comment():
    """コメントを書く。**現場・倉庫だけ**(倉庫参照は読むだけ)。押した直後に共有DBへ送る。"""
    if current_mode() not in (config.MODE_SITE, config.MODE_WAREHOUSE):
        return _wrong_mode(config.MODE_SITE, config.MODE_WAREHOUSE)
    target, problem = _comment_target()
    if problem:
        return problem
    line, item = target
    text = " ".join(str((request.get_json(silent=True) or {}).get("body", "")).split())
    if not text:
        return jsonify(_err("empty", "コメントが空です", "body")), 400
    if len(text) > COMMENT_MAX_LEN:
        return jsonify(_err("too_long", f"{COMMENT_MAX_LEN} 文字までです(いま {len(text)} 文字)", "body")), 400
    # **段階に合わないときは書かせない**(現場は赤のあいだ、倉庫は黄・緑のあと。
    # 赤のあいだの倉庫は現場への返事だけ)。画面は書けないときは欄を出さないが、
    # 開いたあとに相手が状態を変えたときのために、ここでも確かめる
    can, why = _write_rule(item, get_store().comment_thread(line, item.mgmt_no))
    if not can:
        body = _err("comment_closed", why)
        body.update(_thread_reply(line, item))
        return jsonify(body), 409
    side = presenter.comment_side(current_mode())
    get_store().add_comment(line, item.mgmt_no, side, text)
    log.info("コメント: line=%s no=%s side=%s", line, item.mgmt_no, side)
    hook = current_app.config.get("ON_CHANGED")
    if hook:
        try:
            hook(line)
        except Exception:  # noqa: BLE001 - 送るのは次の周期でもよい
            log.exception("コメントの即時送信の依頼に失敗")
    return jsonify({**_thread_reply(line, item), **_payload(current_line_or(line))})


def current_line_or(line: str) -> str:
    """盤面を返すライン。画面が見ているライン(``view``)があればそちら。"""
    body = request.get_json(silent=True) or {}
    view = _resolve_line(str(body.get("view", request.args.get("view", ""))))
    return view or line


@write_bp.post("/api/board/batch-reset")
def batch_reset():
    """届いた資材を一括確認(リセット)。**現場モードだけ。**"""
    if current_mode() != config.MODE_SITE:
        return _wrong_mode(config.MODE_SITE)
    line = _resolve_line((request.get_json(silent=True) or {}).get("line", ""))
    if line is None:
        return jsonify(_err("no_line", "対象のラインがありません")), 400

    expected, problem = _batch_items(line)
    if problem is not None:
        return problem
    svc = get_service()
    unread = _unread_guard(line, [i.mgmt_no for i in svc.items(line)
                                  if i.is_delivered_candidate and i.mgmt_no in expected])
    if unread is not None:
        return unread
    try:
        result = svc.batch_reset(line, expected_revs=expected)
    except LockTimeout:
        return _locked()
    skipped = _skipped(expected, result)
    log.info("一括確認: line=%s 件数=%d 飛ばした=%d", line, result.count, len(skipped))
    return jsonify({**_payload(line), "done": result.count, "skipped": skipped})


@write_bp.post("/api/board/batch-ship")
def batch_ship():
    """表示中のラインを全て発送済みにする。**倉庫モードだけ。**"""
    if current_mode() != config.MODE_WAREHOUSE:
        return _wrong_mode(config.MODE_WAREHOUSE)
    line = _resolve_line((request.get_json(silent=True) or {}).get("line", ""))
    if line is None:
        return jsonify(_err("no_line", "対象のラインがありません")), 400

    expected, problem = _batch_items(line)
    if problem is not None:
        return problem
    svc = get_service()
    unread = _unread_guard(line, [i.mgmt_no for i in svc.items(line)
                                  if i.needs_shipping and i.mgmt_no in expected])
    if unread is not None:
        return unread
    try:
        result = svc.batch_ship(line, expected_revs=expected)
    except LockTimeout:
        return _locked()
    skipped = _skipped(expected, result)
    log.info("一括発送: line=%s 件数=%d 飛ばした=%d", line, result.count, len(skipped))
    return jsonify({**_payload(line), "done": result.count, "skipped": skipped})


def _batch_items(line: str):
    """一括の対象 ``[{mgmt_no, rev}]``(画面に出ていて、確認で訊いたもの)。

    **一括は、訊いたときに画面に出ていた行だけを動かす。** 以前は押された時点の
    サーバの状態で選び直していたので、「3 件を発送済みにしますか？」に OK した
    あとに取り込みで赤が増えると、訊いていない看板まで発送済みになった。画面が
    見ていた版と同じ行だけを動かし、変わっていた行は飛ばして知らせる。
    """
    raw = (request.get_json(silent=True) or {}).get("items")
    if not isinstance(raw, list):
        return None, (jsonify({**_payload(line), **_err(
            "bad_request", "画面が古いため一括の対象が分かりません。画面を開き直してください。", "items")}), 400)
    expected: dict[str, int] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        no = str(entry.get("mgmt_no", "")).strip()
        rev = _as_rev(entry.get("rev"))
        if no and rev is not None:
            expected[no] = rev
    return expected, None


def _skipped(expected: dict[str, int], result) -> list[str]:
    """訊いたのに動かさなかった看板(版が変わっていた・もう対象でない)。"""
    done = {item.mgmt_no for item in result.items}
    return sorted((no for no in expected if no not in done), key=lambda n: (len(n), n))


def _as_rev(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


@bp.post("/api/refresh")
def refresh():
    """Access から今すぐ取り込む(tkinter 版のダブルクリック相当)。

    ローカル SQLite は端末ごとの写しなので、これを呼ばずに読み直しても他端末の
    変更は見えない。**どのモードでも呼べる** ── 読むだけの端末こそ、最新を
    見られる必要がある。
    """
    hook = current_app.config["MANUAL_REFRESH"]
    line = _resolve_line(request.args.get("line", ""))
    if hook is None:
        # 取り込みの手段が無い(テスト時など)。読み直すだけで答える
        return jsonify({**(_payload(line) if line else {}), "imported": False})

    ok = True
    message = ""
    try:
        result = hook()
        failed = list(getattr(result, "failed_lines", None) or [])
        ok = not failed
        missing = [f.table_name for f in failed if getattr(f, "message", "") == "テーブルが存在しません"]
        if missing:
            message = (f"共有DBに {'、'.join(missing)} がありません(消されたか、名前が変わりました)。"
                       "そのラインは最後に取り込んだ内容を表示しています。")
        elif failed:
            message = "共有DBから取り込めないラインがありました。前回取り込めた内容を表示しています。"
    except Exception:  # noqa: BLE001 - 取り込みの失敗で画面を壊さない
        log.exception("共有DBからの再取り込みに失敗")
        ok = False
        message = "共有DBから取り込めませんでした。前回取り込めた内容を表示しています。"

    payload = _payload(line) if line else {}
    return jsonify({**payload, "imported": ok, "import_message": message})


# ------------------------------------------------------------------
# 共通
# ------------------------------------------------------------------
def board_dict(view, stamp: int) -> dict:
    """盤(ビューモデル)を JSON の形にし、**その盤を作る前の状態の番号**を添える。

    画面は、いま描いている盤より古い状態の盤を描かない。返事は届く順が入れ替わる
    (押した返事より先に頼んだ自動更新の返事が、あとから届く)ので、頼んだ順ではなく
    サーバの状態の順で決める。番号は盤を作る**前**に読む ── 作っている途中で状態が
    進んでも、盤のほうが新しいだけで、古い盤に新しい番号が付くことはない。
    """
    out = presenter.to_dict(view)
    out["stamp"] = stamp
    return out


def _payload(line: str) -> dict:
    from kanban import presence

    stamp = get_store().state_stamp()
    store = get_store()
    view = presenter.build(get_service(), line, current_mode())
    mine = presence.registered_line(current_mode(), current_line() or "")
    return {
        "board": board_dict(view, stamp),
        "pending": store.pending_count(),
        "failures": len(store.sync_failures()),
        **_undelivered(store),
        "presence": presence.to_dicts(presence.read(store, exclude=mine)),
    }


def _apply_guarded(kind: str, action):
    """ポリシー上そのボタンを押せるかを確かめてから実行する。"""
    body = request.get_json(silent=True) or {}
    line = _resolve_line(body.get("line", ""))
    if line is None:
        return jsonify(_err("no_line", "対象のラインがありません")), 400
    policy = presenter.policy_for(
        current_mode(), line, get_service().storable_columns(line)
    )
    if not getattr(policy, kind):
        return (
            jsonify(_err("not_allowed", "このラインではその操作はできません")),
            403,
        )
    return _apply(action, kind)


def _apply(action, label: str):
    """1 行の操作を適用して、更新後の盤面を返す。"""
    body = request.get_json(silent=True) or {}
    line = _resolve_line(body.get("line", ""))
    if line is None:
        return jsonify(_err("no_line", "対象のラインがありません")), 400
    mgmt_no = str(body.get("mgmt_no", "")).strip()
    if not mgmt_no:
        return jsonify(_err("bad_request", "対象が指定されていません", "mgmt_no")), 400
    # **版は必ず要る。** 以前は版が無ければ確かめずに通していた ── 画面がどの盤を
    # 見て押したのかが分からないまま、いまの行を動かすことになる。版は全行通しの
    # 番号なので(:data:`kanban.db.store._META_REV_SEQ`)、別のラインの盤を見て押した
    # 押下も、ここで必ず断れる
    rev = _as_rev(body.get("rev"))
    if rev is None:
        return jsonify({**_payload(line), **_err(
            "bad_request", "画面が古いため、どの状態を見て押したのか分かりません。画面を開き直してください。",
            "rev")}), 400

    unread = _unread_guard(line, [mgmt_no])
    if unread is not None:
        return unread

    svc = get_service()
    try:
        action(svc, line, mgmt_no, rev)
    except OrderMissingError as exc:
        # 業務として断る。形は正しいので 400 ではない
        return jsonify({**_payload(line), **_err("order_missing", str(exc))}), 422
    except CancelledWhileShippingError as exc:
        # 赤なし・緑あり。画面はこの code を見て「確認した」を出す
        return jsonify({**_payload(line), **_err("cancelled_while_shipping", str(exc))}), 409
    except ConflictError as exc:
        # 他の端末が先に変えていた。画面を取り直せば正しい状態が見える
        log.info("%s: 競合しました line=%s no=%s", label, line, mgmt_no)
        # **変わった先が「赤なし・緑あり」なら、そう言う。** 画面が古いまま押すと
        # 必ずここへ来る(取り込みで版が進んでいる)。ただの「他の端末が更新
        # しました」では、確かめてほしいことが伝わらない
        current = svc.store.item(line, mgmt_no)
        if label == "size" and current is not None and current.is_cancelled_while_shipping:
            return jsonify({**_payload(line), **_err(
                "cancelled_while_shipping",
                f"{CANCELLED_WHILE_SHIPPING}。倉庫はすでに発送しています。"
                "資材を確かめてから「確認した」を押してください。",
            )}), 409
        return jsonify({**_payload(line), **_err("conflict", str(exc))}), 409
    except LockTimeout:
        return _locked()
    except DomainError as exc:
        return jsonify({**_payload(line), **_err("rejected", str(exc))}), 422

    return jsonify(_payload(line))


def _unread_guard(line: str, nos: list[str]):
    """**相手のコメントを読んでからでないと押せない。** 読んでいないものがあれば断る。

    押すと、それまでのコメントは確認済みになる(:mod:`kanban.domain.comments`)。画面は
    未読のある看板では押す前に中身を見せるが、相手のコメントが届いてから画面に出るまでの
    数秒のあいだに押すと、見せないまま進んでしまう。ここで断り、どの看板かを返す
    (画面は中身を見せ = 開いて既読にし、押し直す)。読んだかどうかは盤の未読の印と同じ
    (この端末か、同じ側のほかの端末で開いた)。断らなければ ``None``。
    """
    if current_mode() not in (config.MODE_SITE, config.MODE_WAREHOUSE) or not nos:
        return None
    counts = get_store().comment_counts(line, presenter.comment_side(current_mode()))
    unread = [no for no in nos if counts.get(str(no), (0, 0))[1]]
    if not unread:
        return None
    log.info("未読のコメントがあるので押せません: line=%s no=%s", line, ",".join(unread))
    body = _err("unread_comments", "相手からのコメントがあります。読んでから押してください。")
    return jsonify({**_payload(line), **body, "unread_nos": unread}), 409


def _wrong_mode(*allowed: str):
    labels = " / ".join(config.mode_display_name(m) for m in allowed)
    return (
        jsonify(
            _err("wrong_mode", f"この操作は{labels}でのみ行えます。設定からモードを切り替えてください。")
        ),
        403,
    )


def _locked():
    return (
        jsonify(
            _err(
                "locked",
                "データベースが混み合っています。少し時間をおいてやり直してください。",
            )
        ),
        503,
    )


def _err(code: str, message: str, field: str = "") -> dict:
    body = {"code": code, "message": message}
    if field:
        body["field"] = field
    return {"error": body}
