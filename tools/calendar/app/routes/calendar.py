"""カレンダー画面とその API (tkinter 版 ``CalendarWindow`` の移植)

    GET  /calendar              画面の器
    GET  /print                 印刷用の1枚(ブラウザの印刷機能で出す)
    GET  /api/calendar          月のビューモデル
    GET  /api/day/<date>        1日の登録内容(閲覧と削除が読む)
    GET  /api/workers           休みにする作業者の選択肢
    GET  /api/comment-targets   コメントの宛先(ライン→班)
    POST /api/rest              休みの登録
    POST /api/comment           コメント・連絡の登録
    POST /api/delete            削除

【返し方の約束】(``docs/設計.md`` §1)
* 更新した API は**更新後の月ビューモデル一式**を返す。画面は返ってきた
  ものを描き直すだけでよく、差分の当て方を持たない
* 断りの種類はHTTPステータスで分ける
  ``400`` 入力の形が違う / ``422`` 形は正しいが業務として断る /
  ``409`` 別の誰かが先に変えていた
* 断りの理由は ``reason`` 定数で運ぶ。文言から推し量らない

【入力の検査はどこまでか】
受け口は**形だけ**を見る(日付として読めるか、必須項目があるか)。
「その作業者が選べるか」「その日にもう登録されていないか」といった
業務の判断は presenters / repository が持つ ── 画面で絞ってあっても
**サーバでも確かめる**(設計 §1 の規則6「一覧に無いものは選べない」)。
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
import threading
import time
from collections import OrderedDict
from typing import Any, Optional

from flask import Blueprint, Response, jsonify, render_template, request

from calendar_app import config, printing, settings as user_settings, sync_service
from calendar_app.logging_utils import get_logger
from calendar_app.presenters import calendar as presenter
from calendar_app.repository import Repository

from .. import shell
from . import get_db

log = get_logger("app.routes.calendar")

bp = Blueprint("calendar", __name__)


# ---------------------------------------------------------------------------
# 画面
# ---------------------------------------------------------------------------
@bp.get("/calendar")
def page():
    """画面の器。**業務データは載せない**(``/api/calendar`` から取る)。

    載せてしまうと、同じ事実が HTML と JSON の2か所に出て、
    画面を移って戻ったときに古いほうが残る。
    """
    return render_template("calendar.html", **shell.shell_context("calendar"))


@bp.get("/print")
def print_page():
    """表示中の月を A4 横1ページの体裁で返す。

    tkinter 版は HTML をファイルに書き出して ``webbrowser`` で開いていたが、
    Web 版はもうブラウザの中に居るので、**ファイルを経由しない**。
    利用者はこのページで Ctrl+P を押す(「PDF に保存」も選べる)。
    """
    year, month, error = _month_args()
    if error is not None:
        return error
    start = presenter.grid_start(year, month)
    records = Repository(get_db()).get_month_records(
        start, start + _dt.timedelta(days=presenter.CELL_COUNT - 1))
    document = printing.render_month_document(
        year=year, month=month, start=start, records=records,
        my_line=user_settings.get_my_line())
    log.info("印刷用ページを返しました: %s年%s月", year, month)
    return Response(document, mimetype="text/html; charset=utf-8")


# ---------------------------------------------------------------------------
# 読み取り
# ---------------------------------------------------------------------------
@bp.get("/api/calendar")
def month():
    """月のビューモデル。前月・翌月・当月の移動もこれ1本で足りる。"""
    year, month_no, error = _month_args()
    if error is not None:
        return error
    return jsonify(_month_payload(year, month_no))


@bp.get("/api/day/<path:day>")
def day(day: str):
    """1日の登録内容。閲覧(ViewerDialog)と削除(DeletePicker)が読む。"""
    parsed = _parse_date(day)
    if parsed is None:
        return _bad_request("日付の形式が正しくありません。")
    return jsonify(presenter.day_dict(presenter.day_view(get_db(), parsed)))


@bp.get("/api/workers")
def workers():
    """休みにする作業者の選択肢。ライン設定で絞ったもの。"""
    return jsonify(presenter.worker_options(get_db()))


@bp.get("/api/comment-targets")
def comment_targets():
    """コメント・連絡の宛先(ライン → 班)。"""
    return jsonify(presenter.comment_targets(get_db()))


# ---------------------------------------------------------------------------
# 登録・削除
# ---------------------------------------------------------------------------
def _require_source() -> Optional[Any]:
    """書き先が決まっていなければ断る。通れば ``None``。

    **正式なデータは共有の取り込み元だけ**で、手元の SQLite は写しと
    送信待ちの置き場にすぎない(設計 §4)。送り先が決まっていないのに
    登録を受けると、入力はこの端末に溜まり続け、他のラインには
    永遠に届きません。**「保存できたつもり」が一番たちが悪い**ので、
    入り口で断ります。

    設定してある共有が一時的に見えないだけ(ネットワーク断・共有の停止)
    なら断りません ── そのための送信待ちです。断るのは
    **そもそも場所が決まっていない**ときだけ。
    """
    if sync_service.get_service().configured:
        return None
    return _refuse(
        "no_source",
        "取り込み元(連絡帳)が見つからないため、登録できません。\n"
        "設定画面の「参照パス」で保存用DBのフォルダを指定してください。")


@bp.post("/api/rest")
def register_rest():
    """休みを登録する (tkinter 版 ``_register_rest``)。

    直・残業繋ぎ・早出繋ぎは、**3交替(A/B/C/D班)の人だけ**に聞く。
    昼勤・日勤の人は直そのものが無いので、送られてきても捨てる。
    """
    blocked = _require_source()
    if blocked is not None:
        return blocked

    body = request.get_json(silent=True) or {}
    day = _parse_date(str(body.get("date", "")))
    code = str(body.get("code", "")).strip()
    if day is None or not code:
        return _bad_request("日付と作業者を指定してください。")

    conn = get_db()
    repo = Repository(conn)

    # **一覧に無いものは選べない。** 画面で絞ってあっても、ここでも確かめる
    worker = _find_worker(repo, code)
    if worker is None:
        return _refuse("not_listed",
                       "その作業者は選べません。ライン設定と班員名簿を確認してください。")

    shift = overtime = early = ""
    if not worker.is_day_shift:
        shift = str(body.get("shift", "")).strip()
        if shift not in ("1", "2", "3"):
            return _refuse("shift_required", "直を選択してください。")
        # 繋ぎは未選択でよい。VBA と同じく「未登録」で保存する
        overtime = str(body.get("overtime", "")).strip() or config.UNREGISTERED
        early = str(body.get("early", "")).strip() or config.UNREGISTERED

    message = f"{worker.name} さんの休みを登録しました"

    def save() -> Optional[Any]:
        # 二重登録。ここで見えるのは**手元に取り込んだぶんまで**で、
        # 他のラインとの重複は取り込み元へ送る時点で防ぐ(sync/business_rules.py)。
        # **確かめてから書くまでを1つの錠の中で行う** ── 別々だと、同時に
        # 届いた2つの登録(ダブルクリック・2枚の画面)が両方とも
        # 「まだ無い」と見て、同じ人・同じ日が2行できた
        if repo.exists_record(day, worker.code):
            return _refuse("already_registered",
                           f"{worker.name} さんは既に登録済みです。", status=409)
        repo.save_record(day, config.KUBUN_REST, worker.name, worker.code,
                         shift=shift, overtime=overtime, early=early,
                         group=worker.group, line=worker.line)
        log.info("休みを登録しました: %s %s(%s)", day, worker.name, worker.code)
        return None

    return _register(conn, body, day, message, save)


@bp.post("/api/comment")
def register_comment():
    """コメント・連絡を登録する (tkinter 版 ``_register_comment``)。"""
    blocked = _require_source()
    if blocked is not None:
        return blocked

    body = request.get_json(silent=True) or {}
    day = _parse_date(str(body.get("date", "")))
    line = str(body.get("line", "")).strip()
    group = str(body.get("group", "")).strip()
    text = str(body.get("text", "")).strip()
    if day is None or not line or not group:
        return _bad_request("日付とライン・班を指定してください。")
    if not text:
        return _refuse("empty_text", "連絡の内容を入力してください。")

    conn = get_db()
    # **実在する組み合わせだけ。** 画面のプルダウンは絞ってあるが、
    # 要求を直接投げられても通らないようにする
    if (line, group) not in Repository(conn).get_line_group_pairs():
        return _refuse("not_listed", "そのライン・班の組み合わせはありません。")

    def save() -> Optional[Any]:
        Repository(conn).save_record(day, config.KUBUN_OTHER, text, "-",
                                     group=group, line=line)
        log.info("連絡を登録しました: %s %s %s班", day, line, group)
        return None

    return _register(conn, body, day, f"{line} {group}班 への連絡を登録しました", save)


@bp.post("/api/delete")
def delete():
    """選んだものを削除する (tkinter 版 ``_delete_day``)。

    削除内容は必ず履歴に残る(``Repository.delete_records_by_ids``)。
    最終確認は画面側のモーダルで取る ── 取り消せない操作なので、
    トーストではなく止める確認にしてある。
    """
    blocked = _require_source()
    if blocked is not None:
        return blocked

    body = request.get_json(silent=True) or {}
    day = _parse_date(str(body.get("date", "")))
    raw_ids = body.get("ids")
    if day is None or not isinstance(raw_ids, list) or not raw_ids:
        return _bad_request("削除する項目を選択してください。")

    try:
        ids = [int(value) for value in raw_ids]
    except (TypeError, ValueError):
        return _bad_request("削除する項目の指定が正しくありません。")

    # 画面が見ていた中身の印 (``{ID: 印}``)。**あれば必ず照らし合わせる**。
    # JSON の鍵は文字列で来るので、ID に揃えてから使う
    raw_marks = body.get("marks")
    marks: dict[int, str] = {}
    if isinstance(raw_marks, dict):
        for key, value in raw_marks.items():
            try:
                marks[int(key)] = str(value)
            except (TypeError, ValueError):
                return _bad_request("削除する項目の指定が正しくありません。")

    conn = get_db()
    # **照らし合わせてから消すまでを1つの錠の中で行う。** 別々だと、
    # 照らし合わせた直後に背景の取り込みが表を入れ替え、同じIDの別の行を
    # 消すことがあった(照らし合わせた意味が無くなる)
    try:
        _begin_immediate(conn)
    except sqlite3.OperationalError as exc:
        return _busy(exc)
    try:
        # **その日の行だけを消す。** 別の日のIDを混ぜて投げられても通さない
        records = {record.id: record
                   for record in Repository(conn).get_day_records(day)}
        unknown = [value for value in ids if value not in records]
        if unknown:
            conn.rollback()
            # 取り込み直しで消えた直後などに起きる。「先に変わっていた」を伝える
            return _refuse("gone", "選んだ項目は既に削除されています。"
                                   "画面を更新してからやり直してください。", status=409)

        # **中身まで見る。** ダイアログを開いたまま放置しているあいだに取り込み
        # 直しが走ると、同じIDが別の行になっていることがある(番号は再利用
        # されうる)。ここを飛ばすと、最悪「別の人の休みを消して、履歴にも
        # その人が残る」── 押した人には気づく手立てが無い
        changed = [record_id for record_id in ids
                   if record_id in marks
                   and str(marks[record_id]) != presenter.record_mark(records[record_id])]
        if changed:
            conn.rollback()
            return _refuse("gone", "選んだ項目の内容が変わっています。"
                                   "画面を更新してからやり直してください。", status=409)

        # ``delete_records_by_ids`` は ``with conn`` で確定させる(この錠ごと)
        count = Repository(conn).delete_records_by_ids(ids)
    except sqlite3.OperationalError as exc:
        conn.rollback()
        if not _is_lock(exc):
            raise
        return _busy(exc)
    except BaseException:
        conn.rollback()
        raise
    log.info("削除しました: %s 件数=%s", day, count)
    return jsonify(_after_change(day, f"{count} 件を削除しました"))


# ---------------------------------------------------------------------------
# 登録の書き込み(錠・送り直し・使用中)
# ---------------------------------------------------------------------------
#: 受け付けた送信の札(画面が「登録する」1回ごとに付けてくる ``submit_id``)。
#: **同じ札がもう一度来たら、書かずに「登録しました」を返す。** 返事が
#: 届く前に通信が切れると、画面は失敗に見えて同じ中身を送り直す ──
#: 札が無いと、同じ連絡が2行できていた。プロセスの中だけ覚えれば足りる
#: (送り直しは数秒〜数分のうちに、同じプロセスへ来る)
_SEEN_SUBMITS: "OrderedDict[str, float]" = OrderedDict()
_SEEN_LOCK = threading.Lock()
_SEEN_MAX = 500
#: 札を覚えておく長さ(秒)。これより古い札は捨てる
_SEEN_TTL_SEC = 6 * 3600


def _submit_id(body: dict[str, Any]) -> str:
    value = str(body.get("submit_id") or "").strip()
    return value[:80]


def _seen(submit_id: str) -> bool:
    if not submit_id:
        return False
    with _SEEN_LOCK:
        now = time.monotonic()
        while _SEEN_SUBMITS:
            oldest, at = next(iter(_SEEN_SUBMITS.items()))
            if now - at <= _SEEN_TTL_SEC and len(_SEEN_SUBMITS) <= _SEEN_MAX:
                break
            _SEEN_SUBMITS.pop(oldest, None)
        return submit_id in _SEEN_SUBMITS


def _remember(submit_id: str) -> None:
    if submit_id:
        with _SEEN_LOCK:
            _SEEN_SUBMITS[submit_id] = time.monotonic()


def _forget(submit_id: str) -> None:
    if submit_id:
        with _SEEN_LOCK:
            _SEEN_SUBMITS.pop(submit_id, None)


def _begin_immediate(conn) -> None:
    """手元の書き込みの錠を取る。取れなければ ``OperationalError``。

    背景の取り込みは、表を入れ替えるあいだこの錠を持つ。待つのは
    ``busy_timeout``(``db.connect``)まで。
    """
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")


def _is_lock(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def _busy(exc: BaseException):
    """手元が使用中で書けなかった。**入力は画面に残したまま**もう一度を頼む。

    取り込み(数秒に1回)が表を入れ替えている最中などに起きる。黙って
    失敗させると打った連絡が消えたように見えるので、503 で
    「待てば通る」と伝える(画面はダイアログを開いたまま残す)。
    """
    log.warning("手元が使用中のため登録を断りました: %s", exc)
    return _refuse("busy",
                   "いま取り込み中のため、まだ登録していません。"
                   "入力はそのまま残してあります。少し待ってから、"
                   "もう一度「登録する」を押してください。", status=503)


def _register(conn, body: dict[str, Any], day: _dt.date, message: str, save):
    """登録を1件書く。**確かめと書き込みを1つの錠の中で**行う。

    ``save()`` は錠の中で呼ばれ、断るときは返事(``_refuse``)を返す。
    同じ札(``submit_id``)の送り直しは書かずに成功を返す。
    """
    submit_id = _submit_id(body)
    try:
        _begin_immediate(conn)
    except sqlite3.OperationalError as exc:
        return _busy(exc)
    try:
        if _seen(submit_id):
            conn.rollback()
            log.info("同じ送信の送り直しを受けました(書かずに返します): %s", submit_id)
            return jsonify(_after_change(day, message))
        # 札は**書く前に、錠の中で**覚える。書き込みは確定と同時に錠を離す
        # (``save_record``)ので、後で覚えると、並んで来た同じ札が
        # その隙に「まだ見ていない」と見てしまう
        _remember(submit_id)
        refused = save()
        if refused is not None:
            conn.rollback()
            _forget(submit_id)
            return refused
        if conn.in_transaction:
            conn.commit()
    except sqlite3.OperationalError as exc:
        conn.rollback()
        _forget(submit_id)
        if not _is_lock(exc):
            raise
        return _busy(exc)
    except BaseException:
        conn.rollback()
        _forget(submit_id)
        raise
    return jsonify(_after_change(day, message))


# ---------------------------------------------------------------------------
# 組み立て
# ---------------------------------------------------------------------------
def _after_change(day: _dt.date, message: str) -> dict[str, Any]:
    """登録・削除のあとに返すもの。

    **入力はまず手元へ保存して即座に画面へ出し、その直後に取り込み元へ送る。**
    取り込み(受信)まで待つと押してから数秒固まるので、送りだけを頼む。
    受信は定期実行に任せる(``sync_service``)。
    """
    sync_service.get_service().request(receive=False)
    payload = _month_payload(day.year, day.month)
    payload["message"] = message
    payload["day"] = presenter.day_dict(presenter.day_view(get_db(), day))
    return payload


def _month_payload(year: int, month_no: int) -> dict[str, Any]:
    from calendar_app.presenters import settings as settings_presenter

    payload = presenter.month_dict(presenter.month_view(get_db(), year, month_no))
    # 帯に出す同期の状態も一緒に返す。画面が別に取りに行くと、
    # 登録した直後の「送信待ち1件」が1拍遅れて出る
    payload["sync"] = settings_presenter.sync_dict()
    return payload


def _month_args() -> tuple[int, int, Optional[Any]]:
    """``?year=&month=`` を読む。省略されたら当月。"""
    today = _dt.date.today()
    try:
        year = int(request.args.get("year", today.year))
        month_no = int(request.args.get("month", today.month))
    except (TypeError, ValueError):
        return 0, 0, _bad_request("年月の指定が正しくありません。")
    year, month_no = presenter.clamp(year, month_no)
    return year, month_no, None


def _parse_date(text: str) -> Optional[_dt.date]:
    try:
        return presenter.parse_date(text)
    except (ValueError, AttributeError):
        return None


def _find_worker(repo: Repository, code: str):
    """ライン設定で絞った一覧の中から探す。**一覧に無ければ None**。"""
    for worker in repo.get_workers(user_settings.get_my_line()):
        if worker.code == code:
            return worker
    return None


# ---------------------------------------------------------------------------
# 断り方
# ---------------------------------------------------------------------------
def _bad_request(message: str):
    """入力の形が違う。**サーバの状態は動いていない。**"""
    return jsonify({"error": {"code": "bad_request", "message": message}}), 400


def _refuse(reason: str, message: str, *, status: int = 422):
    """形は正しいが業務として断る。

    ``409`` は「別の誰かが先に変えていた」場合に使う。画面はどちらも
    ``reason`` で見分ける ── 文言を直した日に区別が壊れないように。
    """
    return jsonify({"error": {"code": reason, "message": message}}), status
