"""設定・管理者 (tkinter版 `ui/admin_panel.py` + 共有への保存ボタン)

    GET  /settings                       画面
    POST /api/settings/admin             管理者モードの認証
    POST /api/settings/recall            過去データを呼び出す
    POST /api/settings/back              いまの直に戻る
    POST /api/settings/push              共有の日報管理へ保存する(③反映)
    GET  /api/progress                   いま走っている仕事の進み具合(取り込み・共有へ保存・マスタ)
    POST /api/settings/sync-shift        時間マスタを取り込む
    GET  /api/settings/state             設定ぜんぶ(参照パス・マスタの状態)
    POST /api/settings/paths             参照パスを保存する
    POST /api/settings/line-targets/import  目標CSVをマスタから作り直す
    POST /api/settings/stop-reasons/template  停止内訳の見本CSVを出す(鍵なし)
    POST /api/settings/stop-reasons/upload    書き換えた停止内訳CSVを渡す(鍵なし)
    POST /api/settings/stop-reasons/reload    停止内訳を読み直す
    POST /api/settings/import/preview    過去データの下見(**書かない**)
    POST /api/settings/import/apply      過去データを取り込む(CSV / xlsx)
    POST /api/settings/import/template   取り込みの見本(見出しだけ)を出す
    POST /api/settings/admin-password    管理者パスワードを変える
    GET  /api/fs/list                    サーバ側のフォルダを一覧する
    (マスタの中身を見る/直すは `app/routes/master.py`)

【参照パスを変えるには管理者パスワードが要る】
参照パスを変えると、**このツールが読みに行く相手そのもの**が変わる。
間違った先を指したまま使うと、画面はふつうに出るのに中身だけが別物に
なる。関門は `presenters/settings.save()` が持っていて、ここは断りの
種類を HTTP に写すだけ(403)。

【この画面がひとつにまとまっている理由】
tkinter版では「管理者タブ」「Accessへ反映ボタン」「時間マスタ同期ボタン」
が別々の場所にあった。どれも**日々の入力ではなく、環境を整える操作**
なので1か所に集める ── 探して回らずに済み、危ない操作が入力画面から
離れる。
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request

from nippou import (admin_password, app_config, config, constants,
                    job_progress, running, work_context)
from nippou.access_bridge import pusher
from nippou.config import SETTINGS
from nippou.logging_setup import get_logger, log_button_click
from nippou.logic import progress as progress_logic
from nippou.logic.shift import default_times_text
from nippou.presenters import fs_browse
from nippou.presenters import settings as settings_presenter
from nippou.reporting import print_format
from nippou.services import push_history, second_output, standard_time

from .. import error_body, get_repo, shell

log = get_logger("app.routes.settings")

bp = Blueprint("settings", __name__)


@bp.get("/settings")
def index():
    ctx = work_context.get_context()
    from .entry import current_calculator, current_flow
    calc = current_calculator()

    pending = get_repo().pending_sync_headers()
    # **数えるのは直、数えているのはページ。** `pending` は
    # `daily_header` の行(=ページ)なので、そのまま「◯直」と書くと
    # 1直を4ページ打っただけで「4直」になります。送るのは直の単位
    # (`shift_check.run_pending` が直でまとめる)なので、直で数えます。
    # **出し先は2つ**(面の見出しと本文)あるので、ここで1度だけ数えます
    pending_shifts = len({(h.report_date, h.line, h.shift) for h in pending})
    report_date, line, shift = ctx.current_key(calc)
    # **最初の描画をJSに任せない。** 開いた瞬間に、参照パスとマスタの
    # 状態が読める。ここで共有フォルダを開くことはない(`file_views` の説明)
    state = settings_presenter.to_dict()
    return render_template(
        "settings.html",
        admin=ctx.admin,
        # 管理者モードか、アクセス権限の表で Administrator(コンバートのボタン。v4.13.0)
        editor=ctx.editor,
        recall=ctx.recall,
        # 取り込みの「入れる先のライン」。**一覧の出どころは1つ**
        # 並びは定義の表の順、見せる字は正規の呼び名(v4.12.5)
        lines=_line_codes(),
        # 直ひと回りの動線。**3つの画面で同じ帯**(`presenters/flow.py`)
        flow=current_flow(report_date, line, shift,
                          recall=ctx.recall.active).as_dict(),
        pending_count=pending_shifts,
        # 控え(LocalBackup)に届くか・まだ写せていないページ(v4.12.0)
        backup=_backup_status(),
        shift_times=get_repo().get_shift_times(),
        shift_defaults=default_times_text(),
        state=state,
        paths=state["paths"],
        # 面ごとに欄を置くので、鍵で引ける形も渡す(出どころは `paths` と同じ)
        path_map=state["path_map"],
        # 生の設定キーをテンプレートに書かないための短い名前
        keys=settings_presenter.PATH_KEY_NAMES,
        # 参照設定の面に出す欄と、その区切り(読みに行く先 / 書き出す先)
        path_groups=[
            {"key": key, "label": label,
             "rows": [state["path_map"][k] for k in members
                      if k in state["path_map"]]}
            for key, label, members in settings_presenter.PATH_GROUPS],
        files=state["files"],
        # 面(タブ)の並び・鍵の要否・見出しの印。**決めるのはサーバ**
        tabs=settings_presenter.tab_views(admin=ctx.admin),
        default_tab=settings_presenter.DEFAULT_TAB,
        tab_badges=_tab_badges(state, pending_shifts),
        # 「どのフォルダの、どの版が動いているか」。入れ替えたのに古いまま、
        # を電話で調べられるようにする
        diagnostics={
            "app_root": str(app_config.APP_ROOT),
            "local_root": str(app_config.local_root()),
            "sqlite": str(SETTINGS.sqlite_path),
            "user_config": str(config.USER_CONFIG_PATH),
            "access_db": str(SETTINGS.access_db_path),
            "access_backend": SETTINGS.access_backend,
            "config_error": app_config.load_error(),
            "version_problem": app_config.version_problem(),
        },
        **shell.shell_context("settings", ribbon=_ribbon(ctx, calc)))


def _tab_badges(state: dict, pending: int) -> dict:
    """面の見出しに添える印。

    困りごと(`presenters/settings.tab_badges`)に、**共有へ未保存の件数**を
    足します。未保存は困りごとではなく、直の途中ならふつうの状態なので
    **色は付けません** ── 毎直ふつうに出るものを赤くすると、本当に
    赤いときに気づけなくなります。
    """
    badges = dict(state["tab_badges"])
    # **まだなぜなぜを済ませていないエラー**(この2日)。済ませれば消える
    try:
        from nippou.services import event_log

        open_errors = event_log.open_errors()
        if open_errors:
            badges["logs"] = {"level": "ng", "text": f"エラー{open_errors}件"}
    except Exception:                             # noqa: BLE001 - 印が出ないだけ
        log.exception("ログの件数を数えられませんでした")
    if pending and "output" not in badges:
        # **「共有へ未保存 3直」と書いてはいけません。**
        #
        # 「3直」は**第3直**に読めます(「3直の保存が無い」)。「未保存」も
        # 「手元にも残っていない」に読めますが、手元には保存済みで、
        # **共有へ渡していない**だけです。数だと分かる「ぶん」を付け、
        # どこへ行っていないのかを書きます
        badges["output"] = {"text": f"共有へ未送信 {pending}直ぶん"}
    return badges


@bp.post("/api/settings/admin")
def admin():
    """管理者モードの認証(`AuthenticateAdmin` 相当)。"""
    payload = request.get_json(silent=True) or {}
    if not payload.get("enable"):
        work_context.get_context().admin = False
        return jsonify({"admin": False, "message": "管理者モードを解除しました"})

    password = str(payload.get("password", ""))
    # **照合は `admin_password` でしかしない。** この端末で変えてあれば
    # 撹拌した値と、変えていなければ既定と突き合わせる
    if not admin_password.verify(password):
        log.warning("管理者モードの認証に失敗しました")
        return jsonify(error_body("bad_password", "パスワードが違います",
                                  field="password")), 403
    work_context.get_context().admin = True
    log_button_click("admin_mode_on")
    return jsonify({"admin": True, "message": "管理者モードにしました"})


def _backup_status() -> dict:
    """控え(LocalBackup)の様子。**読めなくても画面は出す。**"""
    from nippou.services import local_backup

    try:
        return local_backup.status(get_repo())
    except Exception:                             # noqa: BLE001 - 出ないだけ
        log.exception("控えの様子を読めませんでした")
        return {"base": "", "root": "", "reachable": False, "pending": 0}


@bp.post("/api/settings/access-rights")
def access_rights_reload():
    """マスタの「アクセス権限」の表を読み直して当てる(v4.12.0)。**誰でも押せる。**

    当てるのは表に書いてあるラインだけで、表の値が前に当てたものから
    変わったときだけです(`logic/access_rights.should_apply`)── 押した人が
    ラインを好きに選べるわけではないので、鍵は要りません。
    """
    from nippou.presenters import settings as settings_presenter
    from nippou.services import access_rights

    status = access_rights.apply(work_context.get_context())
    log_button_click("access_rights_reload", extra=status.summary())
    body = settings_presenter.access_view()
    body["line_now"] = work_context.get_context().terminal_line
    body["message"] = status.applied or f"アクセス権限: {status.summary()}"
    return jsonify(body)


@bp.post("/api/settings/recall")
def recall():
    """過去データを呼び出す(`NippouDB_Recall` 相当)。

    **通してよいかを決めるのは `services/nippou_service.recall_refusal`。**
    ここは断りの種類を HTTP に写すだけです ── 同じ規則をここにも書くと、
    設定画面のページ移動とこちらで片方だけが緩くなります。

    分け目は「自分の直か、他人の記録か」:

        いまの直・いまのライン … 誰でも(ページを戻るのと同じこと)
        それ以外               … 管理者モード
    """
    ctx = work_context.get_context()
    payload = request.get_json(silent=True) or {}
    try:
        page = int(payload.get("page", 1))
    except (TypeError, ValueError):
        page = 1
    report_date = str(payload.get("report_date", "")).strip()
    line = str(payload.get("line", "")).strip()
    shift = str(payload.get("shift", "")).strip()
    if not (report_date and line and shift):
        return jsonify(error_body("empty", "呼び出す対象を選んでください")), 400

    from .entry import build_service, current_calculator
    calc = current_calculator()
    service = build_service(ctx, calc)
    current_shift, current_date = service.current_shift_info(
        datetime.now(), ctx.force_day_shift())
    refusal = service.recall_refusal(report_date, line, shift, ctx.line,
                                     current_date, current_shift)
    if refusal:
        log.info("呼び出しを断りました: %s %s %s", report_date, line, shift)
        return jsonify(error_body("not_admin", refusal)), 403

    if get_repo().load(report_date, line, shift, page) is None:
        return jsonify(error_body(
            "not_found", "該当するデータがありません")), 404

    ctx.recall = work_context.RecallState(
        active=True, report_date=report_date, line=line, shift=shift, page=page)
    ctx.line = line
    log_button_click("recall", line=line, extra=f"{report_date}/{shift}/{page}")
    from nippou.logic import line_names
    return jsonify({"message": (f"{report_date} {line_names.label(line)} {shift} "
                                f"第{page}ページ を開きました"),
                    "next": "/"})


@bp.post("/api/settings/backfill")
def backfill():
    """**終わった直を後から作る**(管理者)。`dry_run` なら確かめるだけ。

    「呼び出す」は保存のある直しか開けません。丸ごと抜けた直(打ち忘れ)は、
    ここで**空いている枠**(保存が無ければ1ページ目、あれば続き)を入力画面に
    開き、作り始めたことを `backfill` 表に残します。通すかは
    `logic/backfill.decide` が決めます ── 管理者・終わった直だけ・空き枠だけ・
    共有に別のPCのページが無いこと。
    """
    from nippou.logic import backfill as rule
    from nippou.services import backfill as backfill_service
    from nippou.services.push_history import terminal_name

    ctx = work_context.get_context()
    payload = request.get_json(silent=True) or {}
    report_date = str(payload.get("report_date", "")).strip()
    line = str(payload.get("line", "")).strip()
    shift = str(payload.get("shift", "")).strip()
    if not (report_date and line and shift):
        return jsonify(error_body("empty", "作る直(報告日・ライン・直)を選んでください")), 400

    from .entry import anchor_standing, current_calculator
    calc = current_calculator()
    repo = get_repo()
    # この端末がまだ打てる直(作業者を選んで固定した直で、終わりの前)は作らせない
    writing = bool(ctx.anchor.filled and tuple(ctx.anchor.key) == (report_date, line, shift)
                   and not anchor_standing(ctx, calc).expired)
    decision = backfill_service.check(ctx, calc, repo, report_date, line, shift,
                                      writing=writing)
    if not decision.ok:
        status = 403 if decision.code == rule.NOT_ADMIN else 409
        body = error_body(decision.code, decision.message)
        body["decision"] = decision.as_dict()
        return jsonify(body), status
    if payload.get("dry_run"):
        return jsonify({"decision": decision.as_dict()})

    repo.record_backfill(report_date, line, shift, decision.page,
                         terminal=terminal_name(),
                         note=rule.clean_note(str(payload.get("note", ""))))
    ctx.recall = work_context.RecallState(
        active=True, report_date=report_date, line=line, shift=shift,
        page=decision.page)
    ctx.line = line
    log_button_click("backfill", line=line,
                     extra=f"{report_date}/{shift}/{decision.page}")
    log.info("後日作成を始めました: %s %s %s 第%dページ", report_date, line, shift,
             decision.page)
    return jsonify({
        "message": (f"{report_date} {_line_label(line)} {shift} の第{decision.page}ページを開きました"
                    " ── 作業者を選んで打ち、「保存(確定)」でこの直のぶんとして入ります"),
        "decision": decision.as_dict(), "next": "/"})


def _line_codes() -> list[str]:
    from nippou.logic import line_names
    return line_names.codes()


def _line_label(line: str) -> str:
    from nippou.logic import line_names
    return line_names.label(line)


@bp.post("/api/settings/back")
def back_to_current():
    """いまの直に戻る(`NippouDB_BackToCurrent` 相当)。

    **解除する前に、直していたページを保存する。** 押した人は「直して、戻る」
    としか思っていないので、保存を挟まないと直した内容が黙って消える。
    """
    ctx = work_context.get_context()
    from .entry import build_service, current_calculator
    calc = current_calculator()
    service = build_service(ctx, calc)

    saved = False
    moved = None
    if ctx.recall.active:
        # 呼び出し元(画面)が持っている値をそのまま保存する。無ければ
        # DBにあるものがそのまま残るだけなので、保存はしない
        payload = request.get_json(silent=True) or {}
        if payload.get("rows"):
            # **呼び出している紙と、画面に出ている紙が同じときだけ**(v4.24.0)。
            # 違う紙の中身を呼び出した紙へ書くと、打った行が別のページへ
            # 入ります(`entry.screen_mismatch`)。戻ること自体は止めません
            from .entry import screen_mismatch
            moved = screen_mismatch(ctx, calc, payload)
            if moved:
                log.warning("画面のページと呼び出した紙が違うので、戻る前の保存を"
                            "しませんでした %s → %s", moved["opened"], moved["target"])
            else:
                saved = _save_recalled(ctx, calc, payload)

    # **この端末のラインへ戻る。** 他ラインの紙を呼び出していると、
    # ここまで `ctx.line` はそのラインのまま ── 戻さないと、いまの直に
    # 戻ったあとも他ラインの端末として保存します
    ctx.return_to_terminal_line()
    current_shift, current_date = service.current_shift_info(
        datetime.now(), ctx.force_day_shift())
    service.back_to_current(current_date, current_shift, ctx.line)
    log_button_click("back_to_current")
    message = ("直した内容を保存して、いまの直に戻りました" if saved
               else "いまの直に戻りました")
    if moved:
        message = ("いまの直に戻りました。画面のページと呼び出した紙が違ったので、"
                   "画面の中身は保存していません")
    return jsonify({"message": message, "saved": saved, "next": "/",
                    "page_moved": moved})


def _block_if_recall(action_name: str):
    """呼出モード中なら断りの応答、通してよければ `None`。

    port of ``NippouDB_BlockIfRecallMode``。印刷・共有への保存のように
    **当直の最新データを前提にする操作**の先頭で呼ぶ。

    呼出中に印刷すると、直している過去のページが「今日の帳票」として出る。
    共有へ保存すれば、その過去データがみんなの見る側へ行く。どちらも
    押した人には見分けが付かない。

    **422 で断る**(形は正しいが業務として通せない)。`can_return` を
    添えるので、画面は「いま作業に戻りますか」を出せる ── VBA の
    `MsgBox ... vbYesNo` と同じ逃げ道。
    """
    ctx = work_context.get_context()
    from .entry import build_service, current_calculator

    service = build_service(ctx, current_calculator())
    reason = service.block_if_recall_mode(action_name)
    if not reason:
        return None
    log.info("呼出モード中のため断りました: %s", action_name)
    body = error_body("recall_mode", reason)
    body["can_return"] = True
    body["recall"] = service.recall_info()
    return jsonify(body), 422


def _save_recalled(ctx, calc, payload) -> bool:
    """呼出中のページを、いま画面にある値で保存する。**失敗しても止めない。**

    ここで例外を外へ出すと「戻る」を押せなくなり、呼出モードから抜け出せ
    なくなる ── 保存できなかったことより、そちらのほうが困る。
    """
    from nippou.presenters import entry as entry_presenter

    try:
        state = entry_presenter.parse_state(payload)
        # **まだ無いページを、空のまま作らない**(v4.0.0)。後から作る枠を開いて
        # 何も打たずに戻ると、画面は空の12行を送ってきます ── それを保存すると、
        # 抜けた直に「空のページ」ができて共有へも送られます
        if (not entry_presenter.sheet_has_anything(state)
                and get_repo().load(ctx.recall.report_date, ctx.recall.line,
                                    ctx.recall.shift, ctx.recall.page) is None):
            return False
        # `recalculate` は state をその場で書き換え、**断りの理由**を返す。
        # ここでは断りを見ない ── 時間が変でも、直した値は保存して戻す
        # ほうがよい(戻れないほうが困る)
        entry_presenter.recalculate(
            state, package_calc=ctx.package_calc_enabled(),
            shift_times=get_repo().get_shift_times(),
            current_shift=ctx.recall.shift, admin=ctx.editor)
        header, details = entry_presenter.to_records(
            state, ctx.recall.report_date, ctx.recall.line,
            ctx.recall.shift, ctx.recall.page)
        from nippou.services import page_writer

        page_writer.save_page(get_repo(), header, details, by_screen=True)   # 呼出中の画面そのもの
        return True
    except Exception:                             # noqa: BLE001 - 戻れなくしない
        log.exception("戻る前の保存に失敗しました key=%s", ctx.recall)
        return False


# ------------------------------------------------------------------
# ページ移動 (`NippouDB_InitPageSpinner` / `btnGoToPage`)
# ------------------------------------------------------------------
@bp.get("/api/settings/page")
def page_state():
    """ページ移動の状態。**押せるかどうかもサーバが決める。**"""
    ctx = work_context.get_context()
    from .entry import build_service, current_calculator
    calc = current_calculator()
    service = build_service(ctx, calc)
    current_shift, current_date = service.current_shift_info(
        datetime.now(), ctx.force_day_shift())
    spinner = service.page_spinner(current_date, current_shift, ctx.line)
    body = spinner.as_dict()
    body["pages"] = get_repo().saved_pages(current_date, ctx.line, current_shift)
    return jsonify(body)


@bp.post("/api/settings/page")
def go_to_page():
    """当直の指定ページを開いて直す(`NippouDB_EditPage` 相当)。

    **管理者モードは要りません。** 動くのはページだけで、報告日もラインも
    直も「いま」のものに固定されます ── 自分がさっき打った紙を自分で
    直しているだけなので、誰の記録かは変わりません(`edit_page` の説明)。

    VBA は「過去ページの修正は管理者モードでのみ可能です」と断っていました。
    12行を使い切って2ページ目を出したあと1ページ目の打ち間違いに気づくのは
    普通に起きるので、そのたびに人を呼ばせると現場が止まります。
    """
    ctx = work_context.get_context()
    payload = request.get_json(silent=True) or {}
    try:
        page = int(payload.get("page", 0))
    except (TypeError, ValueError):
        page = 0
    if page < 1:
        return jsonify(error_body("bad_page", "ページを選んでください",
                                  field="page")), 400

    from .entry import build_service, current_calculator
    calc = current_calculator()
    service = build_service(ctx, calc)
    current_shift, current_date = service.current_shift_info(
        datetime.now(), ctx.force_day_shift())
    if not current_shift:
        return jsonify(error_body(
            "no_shift", "直が判定できないためページを切り替えられません")), 422

    if service.edit_page(page, current_date, current_shift, ctx.line) is None:
        return jsonify(error_body(
            "not_found", f"ページ{page} のデータがありません")), 404

    log_button_click("edit_page", line=ctx.line, extra=str(page))
    return jsonify({
        # 直し方と戻り方は、表の上に一行で出ます(`page_guide` の editing)。
        # 前はここで「「作業に戻る」を押して」と言っていましたが、そのボタンは
        # ありません(画面は「最新のページに戻る」)
        "message": f"第{page}ページを開きました",
        "page": page, "next": "/"})


# ------------------------------------------------------------------
# DB保存済み一覧・当直の復旧
# ------------------------------------------------------------------
@bp.get("/api/settings/db-keys")
def db_keys():
    """このラインのDB保存済みキー一覧(`NippouDB_FillDBList` 相当)。

    **保存日時の新しい順**。報告日順だと、直したばかりの過去データが
    下のほうに埋もれて探せない。
    """
    ctx = work_context.get_context()
    only_line = request.args.get("all") != "1"
    return jsonify({
        "line": ctx.line,
        "keys": get_repo().saved_keys(ctx.line if only_line else None),
    })


@bp.get("/api/settings/empty-pages")
def empty_pages_list():
    """中身の無い紙の一覧。

        残っているのか見えなければ消せないですよね

    v3.61.2 より前は、作業者を選んだだけで1行も打っていない紙が保存されて
    いました。作らないようにはしましたが、**すでにできてしまったぶんは
    残ったまま**です。数が出るだけでは消せないので、どの日の・どの直の・
    どのページなのかを出します。

    **見るだけなので、管理者でなくても読めます** ── 消すほうは管理者
    だけです。
    """
    from nippou.services import empty_pages

    return jsonify(empty_pages.find(get_repo()).as_dict())


@bp.post("/api/settings/empty-pages/delete")
def empty_pages_delete():
    """中身の無い紙を1枚消す。**管理者だけ。**

    消してよいかは**消す直前にサーバがもう一度確かめます**(打ってある
    紙は消せない / 共有へ渡したものは消せない)── 画面が「空だ」と
    言ってきても、そのまま信じません。
    """
    from nippou.services import empty_pages

    ctx = work_context.get_context()
    if not ctx.admin:
        return jsonify(error_body(
            "not_admin", "紙を消すのは管理者モードでのみ可能です")), 403

    payload = request.get_json(silent=True) or {}
    report_date = str(payload.get("report_date", "")).strip()
    line = str(payload.get("line", "")).strip()
    shift = str(payload.get("shift", "")).strip()
    try:
        page = int(payload.get("page", 0))
    except (TypeError, ValueError):
        page = 0
    if not (report_date and line and shift and page > 0):
        return jsonify(error_body(
            "bad_input", "どの紙を消すのかを指定してください")), 400

    log_button_click("empty_page_delete", line=line,
                     extra=f"{report_date}/{shift}/{page}")
    removed, message = empty_pages.remove(get_repo(), report_date, line,
                                          shift, page)
    body = empty_pages.find(get_repo()).as_dict()
    body["message"] = message
    body["removed"] = removed
    # **断りは422。** 形は正しいが、業務として通せない(打ってある/渡した)
    return jsonify(body), (200 if removed else 422)


@bp.post("/api/settings/restore-shift")
def restore_shift():
    """当直の全ページをDBから読み直す(`NippouDB_RestoreShift` 相当)。

    VBAでは、ブックが壊れて雛形から再開したときに印刷シートを1ページずつ
    作り直す操作だった。Web版にシートは無いので、**DBが持っている全ページで
    印刷用HTMLを作り直す**。
    """
    ctx = work_context.get_context()
    if not ctx.admin:
        return jsonify(error_body(
            "not_admin", "当直の復旧は管理者モードでのみ可能です")), 403

    payload = request.get_json(silent=True) or {}
    from .entry import build_service, current_calculator
    calc = current_calculator()
    service = build_service(ctx, calc)
    current_shift, current_date = service.current_shift_info(
        datetime.now(), ctx.force_day_shift())

    report_date = str(payload.get("report_date", "")).strip() or current_date
    shift = str(payload.get("shift", "")).strip() or current_shift
    line = str(payload.get("line", "")).strip() or ctx.line
    if not (report_date and shift and line):
        return jsonify(error_body(
            "no_shift", "直またはラインが未確定のため復旧できません")), 422

    pages = service.restore_shift(report_date, line, shift)
    if not pages:
        return jsonify(error_body(
            "not_found", "DBにその当直のデータがありません")), 404

    # 紙の頭に載せる集計。**1直ぶんなのでページごとに読み直さない**
    from .printing import _aggregate_of
    aggregate = _aggregate_of(report_date, line, shift)

    written: list[str] = []
    for header, details in pages:
        try:
            out = print_format.default_output_path(
                SETTINGS.report_output_dir, header)
            # **生成日時を渡す。** 渡さないと紙の脚が「生成日時: 」で
            # 終わり、いつ出した紙なのかが分からなくなります ── 現場に
            # 貼る紙なので、古いものが残っていても見分けが付きません
            written.append(str(print_format.write_print_html(
                header, details, out,
                generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                **aggregate)))
        except Exception as exc:                  # noqa: BLE001 - 続けられるだけ続ける
            log.exception("ページ%s の印刷用出力に失敗しました", header.page)
            written.append(f"(ページ{header.page} は出力できませんでした: {exc})")

    log_button_click("restore_shift", line=line,
                     extra=f"{report_date}/{shift}/{len(pages)}ページ")
    return jsonify({
        "pages": len(pages), "files": written,
        "message": f"DBから復元しました({len(pages)}ページ)。"
                   "続けて「再計算」「保存」で反映してください。",
    })


#: 押す人に見せる名前。**「反映」は使わない。**
#
# やっていることは「この端末に溜まった日報を、みんなが見る側へ書き写す」
# ── つまり保存です。「反映」はソースの中の言い方(③反映)で、押す人の
# 言葉ではありませんでした。日報入力の「保存(確定)」と紛れないよう、
# 行き先まで名前に入れます。
PUSH_ACTION = "共有への保存"


@bp.post("/api/settings/push")
def push():
    """共有の日報管理へ保存する(`NippouDB_Save` + `ExecuteSQLTransaction` 相当)。

    **書き先は1つだけです。** 「日報管理のファイル名」の拡張子で
    sqlite3 か Access かが決まり(`pusher.is_sqlite_target`)、両方へ書く
    ことはありません。手元の `nippou_local.sqlite3` は**この端末の作業用**で、
    どちらを選んでも使う別のファイルです。

    **ここで例外を外へ出さない。** 書き先の異常(ファイル破損・
    ネットワーク断)で自分のラインのアプリごと落ちる事態を防ぐ。
    tkinter版 `_push_to_access` と同じ最終防衛ライン。

    【出す前に確かめる ── VBA の印刷保存と同じ関門】
    VBA は直に1回の印刷保存のところで5つの手続き(いまの数えかたで
    **7項目**)を通し、1つでも
    引っかかれば**保存そのものを止めて**いました。ここが同じ場所です:
    共有へ出てしまえば、他のラインも上の集計もそれを読みます。

    見るのは**送ろうとしているページが入っているDBの中身**で、画面の入力では
    ありません(`services/shift_check`)。「表を見る/直す」画面や他の道具から
    直に書き換えられた行も、同じ関門を通ります。

    止まったときの逃げ道は VBA と同じ範囲だけ ── `{"skip": "設備移動の
    ため保存"}` を添えると、最終時間と休憩の2本だけ通ります。梱包数や
    停止記号の間違いは通せません。
    """
    ctx = work_context.get_context()
    # **呼出モード中は断る**(`NippouDB_BlockIfRecallMode` 相当)。
    # 直している過去のデータが、そのまま共有へ行ってしまう
    blocked = _block_if_recall(PUSH_ACTION)
    if blocked is not None:
        return blocked
    # **進み具合を置きながら進む**(`nippou/job_progress.py`)。
    #
    #     保存処理にもプログレスを表示し進捗がわかるようにしてください
    #
    # 確かめる(直ごと)→ 送る(ページごと)→ 集計CSV・履歴・標準作業時間
    # → 月替わり。画面は `GET /api/progress` を見に来て棒を伸ばします
    with job_progress.watching(job=progress_logic.JOB_PUSH):
        return _push(ctx)


def _push(ctx):
    """`push` の本体。進み具合を置いたまま最後まで走る。"""
    from nippou.services import shift_check

    payload = request.get_json(silent=True) or {}
    skip = shift_check.normalize_skip(payload.get("skip"))
    reports = shift_check.run_pending(get_repo(), skip=skip, admin=ctx.editor)
    # **押したことを残す**(時刻・いまの直・担当者)。止めたときも送れた
    # ときも同じ1回の「押した」から数える(`services/push_history`)
    pressed, current = _pressed(ctx, reports, payload.get("worker"))
    if shift_check.blocked_reports(reports):
        log.info("共有への保存を断りました: %d直に直すところがあります",
                 len(shift_check.blocked_reports(reports)))
        _keep_history(lambda: push_history.record_blocked(
            get_repo(), pressed, shift_check.blocked_reports(reports)))
        body = error_body("check_failed", shift_check.combined_message(reports))
        body["reports"] = [r.as_dict() for r in reports]
        body["skip_phrase"] = shift_check.SKIP_PHRASE
        # **逃げ道で通せないものを、名指しで返す。**
        #
        # 断り文には「設備移動のため保存と打つと通せます」と書いてあり
        # ますが、通るのは最終時間と休憩の2本だけです。梱包数や停止記号
        # が混ざっているときに欄だけ出すと、打っても通らず「打ったのに
        # 駄目だった」になります。**何が残っているか**を一緒に返して、
        # 打つ前に読めるようにします(画面は `showSkipBox`)
        from nippou.logic import save_checks

        blockers = []
        for report in shift_check.blocked_reports(reports):
            for finding in save_checks.unskippable(report.findings):
                item = finding.as_dict()
                # **どの直の話かまで書く。** 直が複数まとまって断られる
                # ので、「1ページ 1行目」だけでは探しようがありません
                item["where"] = f"{report.key_text} {item['where']}".strip()
                # **そこへ行く道も一緒に。**
                #
                #     直してほしいといいつつないから直せないんですが
                #
                # 「先に次を直してください」と並べるだけで、開く道が
                # ありませんでした。記録を見るから探すしかなく、古い直は
                # 一覧からも溢れます ── 断りの隣に開くボタンを出します
                # (画面は `findingItem` が `at` を見て描く)
                item["at"] = {"report_date": report.report_date,
                              "line": report.line, "shift": report.shift,
                              "page": item.get("page") or 0}
                blockers.append(item)
        body["skip_blockers"] = blockers
        body["skippable_only"] = not blockers
        # **422**(形は正しいが業務として通せない)。断りの中身も一緒に返す
        return jsonify(body), 422

    log_button_click("push_to_access", line=ctx.line)
    # **押す前に、送るものがあったかを数えておく。**
    #
    #     差分がなければやはり 共有へ保存 を何度も求める必要がないのでは
    #
    # 中身が変わっていなければ未送信は立ちません(`logic/fingerprint`)。
    # そのとき「共有へ保存: 0件」とだけ出ると、**押せなかった**ように
    # 読めます ── 何も起きなかったのではなく、送るものが無いのだと書きます
    try:
        nothing_to_send = not get_repo().pending_sync_headers()
    except Exception:                             # noqa: BLE001 - 押せるように
        nothing_to_send = False
    try:
        # **このあいだは終わらせない。** 途中で落ちると、ヘッダだけ送れて
        # 明細が送れていない状態が**共有側に**残る。手元のSQLiteと
        # 違って、そこは他のラインも読む場所(`nippou/running.py`)
        with running.running(PUSH_ACTION):
            summary = pusher.push_pending(get_repo(), SETTINGS.access_db_path)
    except Exception as exc:                      # noqa: BLE001 - 自ラインを落とさない
        log.exception("共有への保存で予期しないエラーが発生しました")
        return jsonify(error_body(
            "push_failed",
            f"共有への保存中に予期しないエラーが発生しました: {exc}")), 500

    if nothing_to_send and not summary.succeeded and not summary.failed:
        lines = ["共有はいまの中身と同じです ── 送るものはありませんでした"]
    else:
        lines = [f"共有へ保存: {len(summary.succeeded)}件"]
    if summary.failed:
        lines.append(f"失敗: {len(summary.failed)}件")
        lines.extend(f"  {key}: {outcome.error}"
                     for key, outcome in
                     ((o.key, o) for o in summary.failed))
    if summary.stopped:
        lines.append(f"中止しました: {summary.stopped}ページは未送信のまま残しています"
                     "(次に「共有へ保存」を押すと続きから送ります)")
    if summary.concurrency_warning:
        lines.append(summary.concurrency_warning)
    skipped = sum(len(r.skipped) for r in reports)
    if skipped:
        lines.append(f"※ {skipped}件を「{shift_check.SKIP_PHRASE}」で通しました")

    # **送れた日を、集計CSVの2つ目の出力先へ。**
    #
    #     ２つ目に追加した出力先に出すタイミングは共有保存を押したタイミングに
    #
    # 2つ目は見る人とアクセス権の違う所なので、共有へ送った中身だけを
    # 出します(`services/second_output`)。届かなくても保存は成功のまま
    job_progress.step(phase=progress_logic.PHASE_AFTER, total=3, done=0,
                      label="集計CSV(2つ目の出力先)")
    sent = _second_output(lambda: second_output.after_push(
        get_repo(), summary.succeeded))
    if sent.text("送った日の集計CSV"):
        lines.append(sent.text("送った日の集計CSV"))

    # 押した記録を残して、共有の T_共有保存履歴 へも写す
    job_progress.step(done=1, label="共有保存の履歴")
    _keep_history(lambda: push_history.record_push(
        get_repo(), pressed, summary, current))

    # **送れた直の実績を、共有の 標準作業時間.sqlite3 へ。** 届かなければ
    # 待ちに残して次の保存で写す(`services/standard_time`)
    job_progress.step(done=2, label="標準作業時間")
    standard = _standard_time(lambda: standard_time.after_push(
        get_repo(), summary.succeeded, SETTINGS.standard_time_db_path))
    if standard.message:
        lines.append(standard.message)
    job_progress.step(done=3)

    # **保存が済んでから月替わりを見る。** VBA も印刷保存の最後に
    # 置いていました ── 送っていないページがあるうちに片付け始めない
    job_progress.step(phase=progress_logic.PHASE_MONTH)
    rolls = _rollover_after_push(summary)
    roll_lines, monthly = _roll_lines(rolls)

    from nippou.logic import sound

    return jsonify({
        # **音の出来事「共有へ保存した」**(v4.4.0)。送れなかったページが
        # あるときは鳴らしません ── 済んだ音で、残りがあることを隠さない
        "sound_cue": "" if summary.failed else sound.KEY_PUSHED,
        "succeeded": len(summary.succeeded),
        "failed": len(summary.failed),
        "stopped": summary.stopped,
        "warning": summary.concurrency_warning,
        "skipped": skipped,
        "rollover": rolls[0][0].as_dict() if rolls else None,
        "rollovers": [result.as_dict() for result, _ in rolls],
        "second_output": {"daily": sent.as_dict(), "monthly": monthly.as_dict()},
        "standard_time": standard.as_dict(),
        "message": "\n".join(lines + roll_lines),
    })


def _standard_time(write) -> "standard_time.Outcome":
    """標準作業時間へ写す。**何が起きても保存の結果は返す。**"""
    try:
        return write()
    except Exception as exc:                      # noqa: BLE001 - 次の保存で写す
        log.exception("標準作業時間へ写せませんでした")
        return standard_time.Outcome(error=str(exc))


def _pressed(ctx, reports, worker=None):
    """押したときの様子と、いまの直 `(報告日, ライン, 直)`。**読めなくても押せる。**

    `worker` は押した画面に出ていた作業者(日報入力が添えてくる)。
    """
    from nippou.services import shift_check

    from .entry import current_calculator

    via = (shift_check.SKIP_PHRASE
           if any(r.skipped for r in reports) else "")
    try:
        current = ctx.current_key(current_calculator())
        return push_history.Pressed.now(get_repo(), current, via=via,
                                        by=str(worker or "")), current
    except Exception:                             # noqa: BLE001 - 押すのは止めない
        log.exception("共有保存の履歴: いまの直を読めませんでした")
        return push_history.Pressed(at=datetime.now().isoformat(timespec="seconds"),
                                    via=via), ("", ctx.line, "")


def _keep_history(record) -> None:
    """共有保存の履歴を残し、共有へ写す。**何が起きても保存の結果は返す。**"""
    try:
        record()
    except Exception:                             # noqa: BLE001 - 記録で保存を止めない
        log.exception("共有保存の履歴を残せませんでした")
        return
    try:
        push_history.share(get_repo(), SETTINGS.access_db_path)
    except Exception:                             # noqa: BLE001 - 次の保存で写す
        log.exception("共有保存の履歴を共有へ写せませんでした")


def _roll_lines(rolls) -> tuple[list[str], "second_output.Outcome"]:
    """月替わりの結果を知らせの行に。2つ目の結果は1つにまとめる。"""
    lines: list[str] = []
    monthly = second_output.Outcome()
    for result, extra in rolls:
        lines.append(result.message)
        if extra is not None:
            if extra.text("月別の書き出し"):
                lines.append(extra.text("月別の書き出し"))
            monthly = monthly.merge(extra)
    return lines, monthly


def _second_output(write):
    """2つ目の出力先へ出す。**何が起きても共有への保存の結果は返す。**"""
    try:
        return write()
    except Exception:                             # noqa: BLE001 - 保存は成功している
        log.exception("集計CSVの2つ目の出力先へ出せませんでした")
        return second_output.Outcome()


def _rollover_after_push(summary) -> list:
    """送り終えたところで、**まだ書き出していない月**を片付ける。

    書き出した月は覚えておき、二度は書き出しません(直した・取り込んだ
    月は書き出し直す)。覚えていなかった頃は、9月の保存のたびに8月を
    書き出し直し、10月になっても9月が書き出されませんでした
    (`services/month_rollover.catch_up`)。

    **失敗しても保存の結果は返す。** 月に1度の後始末で、押した人が
    いま知りたいのは「共有へ出たかどうか」のほうです。
    """
    if summary.failed:
        # 送れていないページがあるうちは、まだ月を片付けない
        return []
    return _catch_up_months()


def _catch_up_months() -> list:
    """済んでいない月を1つ目・2つ目へ。(1つ目の結果, 2つ目の結果) の並び。"""
    from nippou.services import month_rollover

    try:
        if not month_rollover.check(get_repo()).due:
            return []
        with running.running("月替わりの書き出し"):
            return month_rollover.catch_up(
                get_repo(), SETTINGS.monthly_dir,
                second=lambda result: _second_output(
                    lambda: second_output.after_rollover(get_repo(), result)))
    except Exception:                             # noqa: BLE001 - 保存は成功している
        log.exception("月替わりの後始末に失敗しました")
        return []


@bp.get("/api/settings/rollover")
def rollover_state():
    """月が替わっているか(`月替わりチェック実行` 相当)。**何もしない。**"""
    from nippou.services import month_rollover

    decision = month_rollover.check(get_repo())
    body = decision.as_dict()
    body["dir"] = str(SETTINGS.monthly_dir)
    body["message"] = decision.reason
    return jsonify(body)


@bp.post("/api/settings/rollover")
def rollover_run():
    """月替わりの書き出しを、押して走らせる。**手元は消しません。**

    自動では**共有への保存の直後にしか**動きません(VBA も手動印刷の
    ときだけでした)。月初に端末を触らなかった、途中で失敗した、という
    ときにここから追いかけられるようにしてあります。

    `{"month": "2026-08"}` を添えると、過ぎた月の写しを取り直せます。
    """
    from nippou.logic.month_roll import parse_month
    from nippou.services import month_rollover

    payload = request.get_json(silent=True) or {}
    raw = str(payload.get("month", "")).strip()
    month = parse_month(raw) if raw else None
    if raw and month is None:
        return jsonify(error_body(
            "bad_month", "月は 2026-08 のように入れてください", field="month")), 400

    log_button_click("month_rollover", extra=raw or "auto")
    try:
        with running.running("月替わりの書き出し"):
            if month is not None:
                # 月を指定した取り直し。**済んでいても書き出す**
                result = month_rollover.run(get_repo(), SETTINGS.monthly_dir,
                                            month=month)
                # 押して走らせたときも、2つ目の出力先の「自動集計」へ同じ月を
                monthly = _second_output(lambda: second_output.after_rollover(
                    get_repo(), result))
                month_rollover.settle(get_repo(), result, monthly.ok)
                rolls = [(result, monthly)]
            else:
                rolls = month_rollover.catch_up(
                    get_repo(), SETTINGS.monthly_dir,
                    second=lambda r: _second_output(
                        lambda: second_output.after_rollover(get_repo(), r)))
    except Exception as exc:                      # noqa: BLE001 - 自ラインを落とさない
        log.exception("月替わりの書き出しに失敗しました")
        return jsonify(error_body(
            "rollover_failed", f"書き出しに失敗しました: {exc}")), 500
    if not rolls:
        # 月替わりではない。**なぜかを返す**(「押したのに何も起きない」にしない)
        body = month_rollover.RolloverResult(month_rollover.check(get_repo())).as_dict()
        body["second_output"] = second_output.Outcome().as_dict()
        body["months"] = []
        return jsonify(body)
    lines, monthly = _roll_lines(rolls)
    body = rolls[-1][0].as_dict()
    body["months"] = [result.as_dict() for result, _ in rolls]
    body["second_output"] = monthly.as_dict()
    body["message"] = "\n".join(lines)
    return jsonify(body)


@bp.post("/api/settings/rebuild-summary")
def rebuild_summary():
    """期間ぶんの集計を明細から作り直す。

    **ふだんは押しません。** 集計は日報を保存するたびに作り直して
    います。ここが要るのは2つの場合だけです ── この仕組みが入る前に
    保存されたぶんと、共有DBの表を直に書き換えたぶん。

    管理者に限りません。**読んだ値を作り直すだけ**で、明細には触らず、
    何度押しても同じ結果になります(消す操作ではないので)。
    """
    from nippou.services import summary as summary_service

    payload = request.get_json(silent=True) or {}
    try:
        start = date.fromisoformat(str(payload.get("start", "")))
        end = date.fromisoformat(str(payload.get("end", "")))
    except ValueError:
        return jsonify(error_body(
            "bad_date", "日付は yyyy-mm-dd の形式で入れてください")), 400
    if start > end:
        return jsonify(error_body(
            "bad_range", "開始日は終了日以前にしてください", field="start")), 422

    ctx = work_context.get_context()
    # ラインを跨がない。**他ラインの集計まで作り直す理由がない**
    log_button_click("rebuild_summary", line=ctx.line, extra=f"{start}~{end}")
    try:
        with running.running("集計の作り直し"):
            result = summary_service.rebuild(get_repo(), start, end,
                                             line=ctx.line)
    except Exception as exc:                      # noqa: BLE001 - 画面に出して継続
        log.exception("集計の作り直しに失敗しました")
        return jsonify(error_body(
            "rebuild_failed", f"作り直しに失敗しました: {exc}")), 500
    return jsonify(result.as_dict())


def _line_rename_refusal():
    """コンバートは**管理者だけ**(管理者モードか、アクセス権限の表で Administrator)。"""
    if work_context.get_context().editor:
        return None
    return jsonify(error_body(
        "not_admin", "コンバートは管理者モード(または Administrator)でのみ使えます")), 403


@bp.get("/api/settings/line-rename")
def line_rename_survey():
    """**書かずに試す**(v4.13.0)。共有の日報データの写しを読んで、写すページを数えるだけ。"""
    from nippou.services import line_rename

    refused = _line_rename_refusal()
    if refused:
        return refused
    report = line_rename.survey()
    return jsonify(report.as_dict())


@bp.post("/api/settings/line-rename")
def line_rename_run():
    """**コンバートする**(v4.13.0)。前の名前の表(VBA)のページを、正規の名前の表へ写す。

    前の表には触りません。何度押しても同じ結果になります(写し済みは飛ばす)。
    """
    from nippou.services import line_rename

    refused = _line_rename_refusal()
    if refused:
        return refused
    log_button_click("line_rename")
    with running.running("コンバート"):
        report = line_rename.run()
    log.info("コンバート: %s", report.message())
    status = 200 if report.ok else (409 if report.error else 207)
    return jsonify(report.as_dict()), status


@bp.post("/api/settings/sync-shift")
def sync_shift():
    """時間マスタ(`時間用`)を取り込む(`Set_Shift`/`shiftTime` 相当)。

    **出どころは伝送用ファイル。** 日報管理(=③反映の書き込み先)ではない
    ── 時間用が置いてあるのはそちらで、日報管理には無い。
    """
    log_button_click("sync_shift_times")
    from nippou.services import shift_times

    source = SETTINGS.transmission_master_path
    try:
        # 写し方は起動・マスタ管理と同じ(マスタに揃える)
        times = shift_times.reload(get_repo())
    except Exception as exc:                      # noqa: BLE001 - 自ラインを落とさない
        log.exception("時間マスタの取り込みで予期しないエラーが発生しました")
        return jsonify(error_body(
            "import_failed", f"取り込みに失敗しました: {exc}")), 500

    if times is None:
        return jsonify(error_body(
            "import_failed",
            f"時間マスタを取り込めませんでした。{source} を確認してください。")), 502

    return jsonify({"count": len(times),
                    "times": {k: list(v) for k, v in times.items()},
                    "message": f"時間マスタを取り込みました({len(times)}件)"})


# ------------------------------------------------------------------
# 参照パスとマスタ管理
# ------------------------------------------------------------------
@bp.get("/api/settings/state")
def state():
    """設定ぜんぶ。**書いたあとも毎回まるごと返す**(食い違いを作らない)。"""
    return jsonify(settings_presenter.to_dict())


# 断りの種類を HTTP に写す。
#   400 … 入力の形が違う。**サーバの状態は動いていない**
#   403 … 管理者パスワードが要る / 合っていない
_SAVE_STATUS = {
    settings_presenter.REFUSE_NEED_PASSWORD: 403,
    settings_presenter.REFUSE_BAD_INPUT: 400,
}


@bp.post("/api/settings/paths")
def save_paths():
    """参照パスを保存する。**本文に入っている項目だけ**を触る。

    保存しただけで終わらせず、その設定で何が見つかるかまで返す ──
    「保存しました」だけでは、直ったのかどうかが分からない。
    """
    body = request.get_json(silent=True) or {}
    # 参照パスと、音のファイル名。**受け取る鍵の出どころは presenter** ──
    # ここで別に並べると、増やしたときに片方だけ直すことになる
    accepted = tuple(config.PATH_KEYS) + settings_presenter.SOUND_FILE_KEYS
    values = {key: str(body[key]) for key in accepted if key in body}
    if not values:
        return jsonify(error_body("empty", "変える項目がありません")), 400

    result = settings_presenter.save(
        values,
        # 参照パスを**変えるとき**だけ要る。値は保存も応答もしない
        password=str(body.get("password", "")) or None,
        # 面の上で鍵を開けてあるなら、もう一度は聞かない(同じ合言葉)
        admin=work_context.get_context().admin)
    if not result.ok:
        payload = error_body(result.reason, result.message)
        payload["changing"] = result.changing
        return jsonify(payload), _SAVE_STATUS.get(result.reason, 400)

    log_button_click("save_paths", extra=",".join(sorted(values)))
    payload = settings_presenter.to_dict()
    payload["message"] = result.message
    return jsonify(payload)


@bp.post("/api/settings/paths/recheck")
def recheck_paths():
    """置き場所を**見に行き直す**。

    【なぜ要るのか】
    「読みに行く先の読み込みがない ── 初めに失敗したらそのまま？」
    そのとおりでした。画面に出ている「あります / ありません」は、画面を
    描いたときの結果です。共有が後から繋がった・ファイルを置いた、の
    ときに確かめる先がありませんでした。

    **手元の写しも捨ててから**読み直します ── 参照用マスタは共有から
    手元へ写して読むので(`source_db`)、捨てないと繋がる前に作った古い
    写しをそのまま読みます。
    """
    from nippou import source_db

    source_db.forget()
    state = settings_presenter.to_dict()
    problems = state["problems"]
    log_button_click("recheck_paths", extra=f"{len(problems)}件")
    return jsonify({
        "problems": problems,
        "message": ("読み直しました。困りごとはありません"
                    if not problems
                    else f"読み直しました。{len(problems)}件 見つかりません"),
    })


@bp.post("/api/settings/line-targets/template")
def line_target_template():
    r"""ライン毎目標の**見本CSV**を書き出す。

    「見本出力を作ってください。それ書き換えたら済むし」── そのとおりで、
    いちばん早い道です。**全ラインぶんの行**を書き出すので、数字を
    書き換えて保存すれば終わります。

    既にあるときは**上書きしません**(手で入れた目標が消えます)。
    隣に `.見本.csv` を作って、そちらを見てもらいます。
    """
    from nippou import constants
    from nippou.services import targets as targets_service

    ctx = work_context.get_context()
    if not ctx.admin:
        return jsonify(error_body(
            "not_admin", "見本の書き出しは管理者モードでのみ可能です")), 403

    target = SETTINGS.line_target_path
    found = targets_service.load()
    # **いま効いている値を初期値にします** ── 空の雛形より、いまの値が
    # 入っているほうが「何をどう書き換えるのか」が読めます
    lines = [f"{name},{_target_text(found.of(name))}"
             for name in constants.LINE_NAMES]
    text = ("# ライン毎目標(45度線) ── 1日あたりの枚数\r\n"
            "# 行頭 # は覚え書き。空行は飛ばします\r\n"
            "# 目標を引かないラインは、行ごと消すか 0 にしてください\r\n"
            + "\r\n".join(lines) + "\r\n")

    out = target if not target.exists() else target.with_suffix(".見本.csv")
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8-sig", newline="")
    except OSError as exc:
        log.exception("目標の見本を書けませんでした: %s", out)
        return jsonify(error_body("write_failed", f"書けませんでした: {exc}")), 500

    log_button_click("line_target_template", extra=str(out))
    note = ("" if out == target
            else "(いまのファイルは残してあります。中身を移し替えてください)")
    return jsonify({"path": str(out),
                    "message": f"見本を出しました: {out} {note}".strip()})


def _target_text(value) -> str:
    """目標を見本の文字に。**整数は整数のまま**(12.0 と出さない)。"""
    if not value:
        return "0"
    return str(int(value)) if value == int(value) else str(value)


@bp.post("/api/settings/line-targets/reload")
def line_targets_reload():
    """いまのCSVを**読み直して**、画面の表を描き直す。

    メモ帳で直したあと、効いたかどうかを確かめる口です。グラフを開けば
    どのみち読み直されますが、**そこまで行かないと分からない**のでは
    「直したつもり」で終わります ── 直したその場で、読めた値と
    読めなかった行が出ます。

    **鍵は要りません。** 読むだけで、何も書きません。
    """
    log_button_click("line_targets_reload")
    view = settings_presenter.line_target_view()
    if view["error"]:
        text = f"読み直しましたが、読めませんでした: {view['error']}"
    elif not view["exists"]:
        text = f"{view['source']} はまだありません(目標線は出ません)"
    else:
        text = f"読み直しました。{view['count']}ラインに目標が入っています"
        if view["problems"]:
            text += f"(読めなかった行が {len(view['problems'])}行)"
    return jsonify({"targets": view, "message": text})


@bp.post("/api/settings/line-targets/upload")
def line_targets_upload():
    r"""**書き換えたCSVを、そのまま効かせる。**

    利用者の言葉:「マスタから読み取りしかないけど CSVから読む は
    必要ですよ(見本を出す意味がないよ)」── そのとおりでした。
    見本は、いまのファイルがあると隣に `.見本.csv` として出ます。
    書き換えても**それを本物にする道が無い**ので、書き換えたあと
    手でファイルを置き換えることになっていました。

        見本を出す → 数字を書き換える → ここで渡す → 効く

    【下見してから決める】
    `apply` が無ければ**1文字も書きません。** 何ラインぶん入るのか、
    読めない行があるかを先に出します ── 書き換える先は共有に置いた
    1つのファイルで、押した瞬間に全端末のグラフが変わります。

    **管理者専用**で、`apply` のときだけ書きます。
    """
    from nippou.logic import line_target
    from nippou.services import targets as targets_service

    ctx = work_context.get_context()
    if not ctx.admin:
        return jsonify(error_body(
            "not_admin", "目標CSVの取り込みは管理者モードでのみ可能です")), 403

    upload = request.files.get("file")
    if upload is None or not (upload.filename or "").strip():
        return jsonify(error_body("no_file", "ファイルが選ばれていません")), 400

    raw = upload.read()
    if not raw.strip():
        return jsonify(error_body(
            "empty", f"{upload.filename} は空でした")), 422
    text = _decode_csv(raw)

    found = line_target.parse(text, source=upload.filename)
    if not found.values:
        # **読めないCSVで、いま効いている目標を消さない。**
        why = found.problems[0].reason if found.problems else "行がありません"
        return jsonify(error_body(
            "no_rows",
            f"{upload.filename} から目標を読めませんでした({why})。"
            "「ライン名,目標」の形で書いてください")), 422

    target = SETTINGS.line_target_path
    rows = [{"line": name, "text": _target_text(value)}
            for name, value in found.values.items()]
    preview = {
        "file": upload.filename,
        "rows": rows,
        "problems": [p.as_dict() for p in found.problems],
        "dest": str(target),
    }

    if not request.form.get("apply"):
        text_note = (f"{upload.filename}: {len(rows)}ラインぶん読めました。"
                     f"「決定」で {target} を書き換えます")
        if found.problems:
            text_note += f"(読めなかった行が {len(found.problems)}行)"
        return jsonify({**preview, "applied": False, "message": text_note})

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # **BOM付き。** 付けないとメモ帳もExcelも cp932 で開いて化けます
        target.write_text(
            line_target.as_csv(found, order=constants.LINE_NAMES,
                               note=targets_service.CSV_NOTE),
            encoding=targets_service.CSV_ENCODING)
    except OSError as exc:
        log.exception("目標CSVを書けませんでした: %s", target)
        return jsonify(error_body(
            "write_failed", f"書けませんでした: {exc}")), 500

    log_button_click("line_targets_upload", extra=f"{upload.filename}/{len(rows)}")
    return jsonify({
        **preview, "applied": True,
        "targets": settings_presenter.line_target_view(),
        "message": f"{target} を書き換えました({len(rows)}ライン)。"
                   "次にグラフを開いたときから効きます",
    })


def _decode_csv(raw: bytes) -> str:
    """打たれたCSVを文字に。**読めないことを理由に断らない。**

    現場のCSVは Excel から出た Shift_JIS のことも、メモ帳の BOM付き
    UTF-8 のこともあります。決め打ちにすると、片方が「読めません」で
    止まります ── 順に試して、通ったものを使います。
    """
    for encoding in ("utf-8-sig", "cp932", "utf-8"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    # どれでも読めないときは、読める文字だけ拾う ── 1文字のせいで
    # 全部を捨てるより、読めた行を見せて直してもらうほうがよい
    return raw.decode("utf-8", "replace")


# ------------------------------------------------------------------
# 停止内訳(停止内訳.csv)── **鍵は要りません**
#
# 利用者の言葉:「停止内訳も管理者以外が触れるようにしたい」。停止理由を
# 足すたびに管理者を呼ばなくて済むように、CSVを置けばそちらを読みます
# (`access_bridge/stop_master`)。見本を出す・渡して効かせる・読み直す
# の3つは**誰でも**押せます。置き場所(参照設定)を変えるのは、ほかの
# パスと同じく管理者パスワードが要ります。
# ------------------------------------------------------------------
def _stop_reason_rows(by_category) -> dict:
    """`stop_master` の形 → `stop_csv.Reason` の形(記号の無い行は落とす)。"""
    from nippou.logic import stop_csv

    return {category: [stop_csv.Reason(r.code, r.label)
                       for r in rows if r.code]
            for category, rows in by_category.items()}


@bp.post("/api/settings/stop-reasons/template")
def stop_reason_template():
    """停止内訳の**見本CSV**を書き出す。いま使っている一覧がそのまま入る。

    既にあるときは**上書きしません**(手で直した一覧が消えます)。隣に
    `停止内訳.見本.csv` を作るので、書き換えたら「CSVから読む」で渡すか、
    名前を変えて置き換えてもらいます。
    """
    from nippou.access_bridge import stop_master
    from nippou.logic import stop_csv

    target = SETTINGS.stop_reason_csv_path
    try:
        current = stop_master.load_all_categories()
    except Exception:                             # noqa: BLE001 - 空の見本でも出す
        log.exception("停止内訳を読めませんでした(見本は空で出します)")
        current = {}
    text = stop_csv.as_csv(_stop_reason_rows(current))

    out = target if not target.exists() else target.with_suffix(".見本.csv")
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8-sig", newline="")
    except OSError as exc:
        log.exception("停止内訳の見本を書けませんでした: %s", out)
        return jsonify(error_body("write_failed", f"書けませんでした: {exc}")), 500

    log_button_click("stop_reason_template", extra=str(out))
    if out == target:
        note = "(このファイルをメモ帳で直せば、次に日報入力を開いたときから効きます)"
    else:
        note = ("(いまのファイルは残してあります。書き換えたら「CSVから読む」で"
                "渡してください)")
    return jsonify({"path": str(out),
                    "reasons": settings_presenter.stop_reason_view(),
                    "message": f"見本を出しました: {out} {note}"})


@bp.post("/api/settings/stop-reasons/reload")
def stop_reasons_reload():
    """いまのCSV(と表)を**読み直して**、一覧を描き直す。読むだけ。"""
    log_button_click("stop_reasons_reload")
    view = settings_presenter.stop_reason_view()
    if view["error"]:
        text = f"読み直しましたが、CSVを読めませんでした: {view['error']}"
    elif not view["exists"]:
        text = (f"{view['source']} はまだありません"
                "(伝送用ファイルの表をそのまま使っています)")
    else:
        text = (f"読み直しました。{view['csv_count']}分類をCSVから、"
                f"合わせて {view['count']}件の理由を使っています")
        if view["problems"]:
            text += f"(使えなかった行が {len(view['problems'])}行)"
    return jsonify({"reasons": view, "message": text})


@bp.post("/api/settings/stop-reasons/upload")
def stop_reasons_upload():
    r"""**書き換えたCSVを渡して、そのまま効かせる。** 鍵は要りません。

        見本を出す → 書き換える → ここで渡す → 下見 → 決定 → 効く

    `apply` が無ければ**1文字も書きません。** 増える・消える・名前が
    変わる理由を先に出します ── 書き換える先は共有に置いた1つの
    ファイルで、決定した瞬間に全端末の停止理由の一覧が変わります。
    消える記号は、昔の日報に残っていれば「内訳にない記号」になるので、
    押す前に見せます。

    書き換える前のファイルは隣に `停止内訳.前回.csv` として残します
    (鍵なしで書けるので、戻す道を置いておく)。
    """
    from nippou.access_bridge import stop_master
    from nippou.logic import stop_csv

    upload = request.files.get("file")
    if upload is None or not (upload.filename or "").strip():
        return jsonify(error_body("no_file", "ファイルが選ばれていません")), 400
    raw = upload.read()
    if not raw.strip():
        return jsonify(error_body("empty", f"{upload.filename} は空でした")), 422
    found = stop_csv.parse(_decode_csv(raw), source=upload.filename)
    if not found.count:
        # **読めないCSVで、いま使っている一覧を消さない**
        why = found.problems[0].reason if found.problems else "行がありません"
        return jsonify(error_body(
            "no_rows",
            f"{upload.filename} から停止内訳を読めませんでした({why})。"
            "「分類,内訳番号,内訳」の形で書いてください")), 422

    target = SETTINGS.stop_reason_csv_path
    try:
        current = stop_master.load_all_categories()
    except Exception:                             # noqa: BLE001 - 比べられなくても下見は出す
        log.exception("いまの停止内訳を読めませんでした")
        current = {}
    before = _stop_reason_rows(current)
    # 決定したあとの一覧: CSVに無い分類は表のまま(`stop_csv.pick`)
    after = {name: (found.of(name) or before.get(name, []))
             for name in stop_csv.CATEGORY_NAMES}
    preview = {
        "file": upload.filename,
        "dest": str(target),
        "groups": [{"category": name, "call": stop_csv.call_of(name),
                    "number": stop_csv.number_of(name),
                    "count": len(found.of(name)),
                    # この分類はCSVに無い ── 決定しても表のまま
                    "from_master": not found.of(name)}
                   for name in stop_csv.CATEGORY_NAMES],
        "diff": stop_csv.diff(before, after),
        "problems": [p.as_dict() for p in found.problems
                     if p.level == stop_csv.SKIP],
        "warnings": [p.as_dict() for p in found.problems
                     if p.level == stop_csv.WARN],
    }

    if not request.form.get("apply"):
        note = (f"{upload.filename}: {found.count}件 読めました。"
                f"「決定」で {target} を書き換えます")
        if preview["problems"]:
            note += f"(使えない行が {len(preview['problems'])}行)"
        return jsonify({**preview, "applied": False, "message": note})

    kept = ""
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            previous = target.with_suffix(".前回.csv")
            previous.write_bytes(target.read_bytes())
            kept = str(previous)
        # **BOM付き。** 付けないとメモ帳もExcelも cp932 で開いて化けます
        target.write_text(stop_csv.as_csv(found.values),
                          encoding="utf-8-sig", newline="")
    except OSError as exc:
        log.exception("停止内訳CSVを書けませんでした: %s", target)
        return jsonify(error_body("write_failed", f"書けませんでした: {exc}")), 500

    log_button_click("stop_reasons_upload",
                     extra=f"{upload.filename}/{found.count}件")
    message = (f"{target} を書き換えました({found.count}件)。"
               "次に日報入力を開いたときから効きます")
    if kept:
        message += f"。前のファイルは {kept} に残しました"
    return jsonify({**preview, "applied": True, "kept": kept,
                    "reasons": settings_presenter.stop_reason_view(),
                    "message": message})


@bp.post("/api/settings/line-targets/import")
def import_line_targets():
    """梱包資材マスタ「ライン毎目標」を読んで、目標CSVを作り直す。

    **上書きします。** 初期値づくり用で、以降はCSVのほうをメモ帳で
    直してもらいます ── 両方を読むと「マスタを直したのに古いCSVが
    勝って変わらない」は、値ごとの出どころを画面に出して解いています
    (`services/targets.py`)。

    **管理者専用。** 押した人の端末ではなく、共有に置いた1つのファイルを
    書き換えます ── 他のラインのグラフの目標線も、同時に変わります。
    """
    from nippou.services import targets as targets_service

    ctx = work_context.get_context()
    if not ctx.admin:
        return jsonify(error_body(
            "not_admin", "目標の取り込みは管理者モードでのみ可能です")), 403

    log_button_click("import_line_targets")
    try:
        found = targets_service.import_from_master()
    except Exception as exc:                      # noqa: BLE001 - 自ラインを落とさない
        log.exception("ライン毎目標の取り込みで予期しないエラーが発生しました")
        return jsonify(error_body(
            "import_failed", f"取り込みに失敗しました: {exc}")), 500

    if not found.values:
        # マスタが読めなかった/空だった。**CSVは触っていない** ──
        # 読めないマスタで、いま効いている目標を消さない
        why = found.problems[0].reason if found.problems else "行がありません"
        return jsonify(error_body(
            "import_failed",
            f"マスタから読めませんでした({why})。"
            f"{SETTINGS.gw_material_master_path} を確かめてください。")), 502

    payload = settings_presenter.to_dict()
    payload["message"] = (f"マスタから取り込みました({len(found.values)}件)。"
                          f"書き出し先: {found.source}")
    return jsonify(payload)


# ======================================================================
# 過去の日報をCSVから取り込む
#
# このツールに切り替える前の日報は VBA のブックの中にしかありません。
# 紙もグラフも集計も `daily_header` / `daily_detail` から作っているので、
# **そこに戻せなければ過去は一生出てきません。**
#
# **押すまで書きません。** 取り込みは、いま入っているものを黙って
# 置き換えられる操作です ── 打ち間違えたファイルを選んだだけで今日の
# 12行が消えます。下見で「何ページ入るか / どれが置き換わるか」を出してから
# 押させます。
# ======================================================================
def _import_request():
    """管理者か・ファイルはあるか。断るなら (応答, 状態) を返します。"""
    ctx = work_context.get_context()
    if not ctx.admin:
        return jsonify(error_body(
            "not_admin", "過去データの取り込みは管理者モードでのみ可能です")), 403
    body = request.get_json(silent=True) or {}
    text = str(body.get("path", "")).strip()
    if not text:
        return jsonify(error_body("no_path", "読み込むファイル(CSV・xlsx)かフォルダを指定してください")), 400
    path = Path(text).expanduser()
    if not path.is_file():
        return jsonify(error_body(
            "not_found", f"ファイルが見つかりません: {path}")), 404
    return path, body


@bp.post("/api/settings/import/upload")
def import_upload():
    r"""ファイルそのものを受け取って取り込む。**何本でも、まとめて。**

    【なぜ道を打たせるだけでは駄目だったのか】
    「ドラッグでも渡せるように / 複数同時は無理ですか？」── 道を打つのは
    共有フォルダの深いところだと現実的ではありませんし、10本あれば10回
    打つことになります。掴んで落とせるほうが早い。

    **ブラウザは落とされたファイルの本当の道を教えません**(安全のため
    `C:\fakepath\…` になります)。なので道ではなく**中身**を受け取り、
    手元の作業用フォルダへ一旦置いてから、道で渡すのと同じ道筋に載せます
    ── 取り込みの判断は1か所(`services/csv_import`)のままです。

    `dry_run` が真なら下見だけ(**1行も書きません**)。
    """
    import shutil
    import tempfile

    from nippou.services import csv_import as import_service

    ctx = work_context.get_context()
    if not ctx.admin:
        return jsonify(error_body(
            "not_admin", "過去データの取り込みは管理者モードでのみ可能です")), 403

    uploads = [f for f in request.files.getlist("files") if f and f.filename]
    if not uploads:
        return jsonify(error_body("no_file", "ファイルを渡してください")), 400

    line = str(request.form.get("line", "")).strip()
    dry_run = str(request.form.get("dry_run", "")).strip() in ("1", "true")
    mark_synced = str(request.form.get("mark_synced", "1")).strip() in ("1", "true")
    sizes = _upload_sizes(request.form.get("sizes", ""), len(uploads))

    # **名前は残します。** 拡張子で xlsx か CSV かを見分けるので
    # (`is_workbook`)、名無しの一時ファイルに入れると読めません
    folder = Path(tempfile.mkdtemp(prefix="nippou-import-"))
    results: list[dict] = []
    # **進み具合を置きながら進みます**(`nippou/job_progress.py`)。
    #
    #     件数が多いときはプログレス表示させてください
    #
    # 取り込みは終わるまで返らないので、画面は別の口
    # (`/api/settings/import/progress`)を見に来ます
    try:
        with job_progress.watching(file_count=len(uploads)):
            # まず全部置く(同じ作業日のファイルを比べるため)
            placed: list[tuple[str, Path | None, str]] = []
            for no, upload in enumerate(uploads, start=1):
                name = Path(upload.filename).name or "取り込み"
                target = folder / name
                upload.save(str(target))
                got = target.stat().st_size
                if sizes[no - 1] is not None and got != sizes[no - 1]:
                    # **欠けて届いたものは読まない**(読めば「読めませんでした」になり、
                    # ファイルが悪いように見える)
                    log.warning("取り込むファイルが欠けて届きました: %s (%d / %d バイト)",
                                name, got, sizes[no - 1])
                    placed.append((name, None, f"欠けて届きました({got:,} / {sizes[no - 1]:,} バイト)。"
                                               "もう一度渡すか、道を打って渡してください"))
                    continue
                placed.append((name, target, ""))
            # 同じ作業日のファイルが何本もあれば、中身の多いほうだけ入れる
            # (昔の日報は1日に何度も保存されている。途中で保存したほうで上書きしない)
            losers = import_service.same_day_losers(
                [t for _n, t, why in placed if t is not None and not why], line)
            for no, (name, target, why) in enumerate(placed, start=1):
                job_progress.step(file_no=no, file_name=name)
                why = why or (losers.get(target, "") if target is not None else "")
                if why:
                    results.append({"name": name, "ok": False, "preview": None, "result": None,
                                    "error": why})
                    continue
                results.append(_import_one(
                    import_service, target, name, line,
                    dry_run=dry_run, mark_synced=mark_synced))
    finally:
        shutil.rmtree(folder, ignore_errors=True)

    log_button_click("import_upload",
                     extra=f"{len(uploads)}本 / {'下見' if dry_run else '取り込み'}")
    return jsonify({
        "files": results,
        "lines": list(constants.LINE_NAMES),
        "dry_run": dry_run,
        "message": _upload_message(results, dry_run),
    })


def _upload_sizes(text: str, count: int) -> list:
    """画面が添えた、渡したファイルの大きさ(並びはファイルと同じ)。分からなければ None。"""
    import json

    try:
        given = json.loads(text) if text else []
    except ValueError:
        given = []
    if not isinstance(given, list) or len(given) != count:
        return [None] * count
    return [v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None for v in given]


def _import_one(import_service, path: Path, name: str, line: str, *,
                dry_run: bool, mark_synced: bool) -> dict:
    """1本ぶん。**1本が読めなくても、残りは続けます。**"""
    try:
        if dry_run:
            preview = import_service.preview(get_repo(), path, line)
            return {"name": name, "ok": preview.ok,
                    "preview": preview.as_dict(), "result": None}
        preview, result = import_service.apply(
            get_repo(), path, line=line, mark_synced=mark_synced)
        return {"name": name, "ok": preview.ok,
                "preview": preview.as_dict(), "result": result.as_dict()}
    except Exception as exc:                      # noqa: BLE001 - 残りは続ける
        log.exception("取り込めませんでした: %s", name)
        return {"name": name, "ok": False, "preview": None, "result": None,
                "error": f"読めませんでした: {exc}"}


def _upload_message(results: list[dict], dry_run: bool) -> str:
    """まとめの1行。**何本中何本が通ったか**を先に言う。"""
    good = [r for r in results if r["ok"]]
    if dry_run:
        pages = sum(len(r["preview"]["targets"]) for r in good)
        text = f"{len(good)}/{len(results)}本 読めました({pages}ページぶん)"
    else:
        pages = sum(r["result"]["pages"] for r in good if r["result"])
        days = sum(r["result"]["csv_days"] for r in good if r["result"])
        text = (f"{len(good)}/{len(results)}本 取り込みました"
                f"({pages}ページ / 集計CSV {days}日ぶん)")
    bad = [r["name"] for r in results if not r["ok"]]
    if bad:
        text += " ── 読めなかった: " + " / ".join(bad[:5])
    return text


@bp.get("/api/progress")
def job_progress_now():
    """いま走っている仕事の進み具合(取り込み・共有へ保存・マスタを直す)。

    **読むだけで、何も変えません。** 画面は仕事を投げたあと 0.4 秒ごとに
    ここを見に来て棒を伸ばします(`static/js/progress.js`)。
    """
    return jsonify(job_progress.snapshot().as_dict())


@bp.post("/api/progress/stop")
def job_progress_stop():
    """「中止」(共有へ保存)。**いまの束を送り終えたら止まる。**

    送った分は共有に入っていて、残りは未送信のまま。次に「共有へ保存」を
    押せば続きから送ります。止められる段でなければ 409。
    """
    if not job_progress.request_stop():
        return jsonify(error_body("not_stoppable",
                                  "いま止められる仕事は走っていません")), 409
    log_button_click("push_stop")
    return jsonify({"ok": True, "message": "いまの束を送り終えたら止めます"})


#: これより多く送るときは、押す前に数を見せて確かめる(`GET /api/settings/push/plan`)
PUSH_CONFIRM_PAGES = 100


@bp.get("/api/settings/push/plan")
def push_plan():
    """共有へ保存の**下見**。何ページ・何直・いつからいつまでを送るか。

        数が多い場合は分割するか処理前に確認を取ってください

    取り込んだ過去の日報がまとめて未送信になっていると、押した瞬間に
    何百ページも送り始めていました。多いときは画面が先に数を見せて訊きます
    (`confirm_needed`)。**読むだけで、共有には触りません。**
    """
    from nippou.logic.shift import parse_business_date
    from nippou.services.nippou_service import format_business_date

    try:
        headers = list(get_repo().pending_sync_headers())
    except Exception as exc:                      # noqa: BLE001 - 押すのは止めない
        log.exception("共有へ保存の下見ができませんでした")
        return jsonify({"pages": 0, "shifts": 0, "confirm_needed": False,
                        "error": str(exc)})
    shifts = {(h.report_date, h.line, h.shift) for h in headers}
    days = sorted({parse_business_date(h.report_date) for h in headers} - {None})
    pages = len(headers)
    # 共有フォルダの上で 25ページの束がおおむね数秒(`pusher.BATCH_PAGES`)
    minutes = max(1, round(pages / 25 * 4 / 60)) if pages else 0
    return jsonify({
        "pages": pages, "shifts": len(shifts),
        "first_day": format_business_date(days[0]) if days else "",
        "last_day": format_business_date(days[-1]) if days else "",
        "minutes": minutes,
        "confirm_needed": pages >= PUSH_CONFIRM_PAGES,
    })


@bp.get("/api/settings/import/progress")
def import_progress():
    """いまどこまで進んだか。**何度でも聞ける、軽い口。**

        件数が多いときはプログレス表示させてください

    取り込みは終わるまで返りません(途中まで入った日報を残さないため)。
    画面はこちらを 0.4 秒ごとに見に来て棒を伸ばします ── waitress は
    スレッドで動くので、取り込み中でもこの口は答えられます。

    **走っていなければ「走っていない」と返すだけ**です(数も文も空)。
    """
    return jsonify(job_progress.snapshot().as_dict())


@bp.post("/api/settings/import/preview")
def import_preview():
    """取り込む前の下見。**1行も書きません。**"""
    from nippou.services import csv_import as import_service

    found = _import_request()
    if not isinstance(found, tuple) or not isinstance(found[0], Path):
        return found                              # 断り(応答, 状態)
    path, _body = found

    line = str(_body.get("line", "")).strip()
    try:
        preview = import_service.preview(get_repo(), path, line)
    except Exception as exc:                      # noqa: BLE001 - 画面に出して継続
        log.exception("取り込みの下見に失敗しました: %s", path)
        return jsonify(error_body("read_failed", f"読めませんでした: {exc}")), 500
    payload = preview.as_dict()
    # **どのラインを選べるか**も返します ── 画面が自前で一覧を持つと、
    # ラインが増えたときに2か所直すことになります
    payload["lines"] = list(constants.LINE_NAMES)
    return jsonify(payload)


@bp.post("/api/settings/import/apply")
def import_apply():
    """取り込む。**下見と同じものを、もう一度読んでから入れます。**

    `mark_synced` の既定は真 ── 古いぶんは VBA の時代に既に共有へ
    入っているのが普通なので、取り込んだ瞬間に何か月ぶんもが Access へ
    飛ばないようにします。送りたいときだけ偽にします。
    """
    from nippou.services import csv_import as import_service

    found = _import_request()
    if not isinstance(found, tuple) or not isinstance(found[0], Path):
        return found
    path, body = found
    mark_synced = bool(body.get("mark_synced", True))
    line = str(body.get("line", "")).strip()

    log_button_click("import_nippou", extra=f"{path} → {line or '(ファイルの通り)'}")
    try:
        # 道を打って渡したときも、進み具合は同じように出します
        with job_progress.watching(file_count=1):
            job_progress.step(file_no=1, file_name=path.name)
            preview, result = import_service.apply(
                get_repo(), path, line=line, mark_synced=mark_synced)
    except Exception as exc:                      # noqa: BLE001 - 画面に出して継続
        log.exception("取り込みに失敗しました: %s", path)
        return jsonify(error_body("import_failed",
                                  f"取り込めませんでした: {exc}")), 500
    if not preview.ok:
        # 読めないものは 422(断り)。**0件を「成功」と言わない**
        return jsonify({**preview.as_dict(), "code": "not_importable"}), 422
    return jsonify({"preview": preview.as_dict(), **result.as_dict()})


@bp.post("/api/settings/import/reshift")
def import_reshift():
    """取り込み済みの過去日報で、2直に入った3直の行を3直へ戻す(`services/reshift`)。

    `{"apply": false}` は下見(**書かない**)、`{"apply": true}` で戻す。管理者モードだけ。
    **いまの作業日は触りません**(打っている最中の直を動かさない)。
    """
    from nippou.services import reshift
    from nippou.services.nippou_service import format_business_date

    ctx = work_context.get_context()
    if not ctx.admin:
        return jsonify(error_body(
            "not_admin", "取り込み済みの日報を直すのは管理者モードでのみ可能です")), 403
    from .entry import build_service, current_calculator
    service = build_service(ctx, current_calculator())
    _shift, today = service.current_shift_info(datetime.now(), ctx.force_day_shift())
    skip = (today if isinstance(today, str) else format_business_date(today),)
    body = request.get_json(silent=True) or {}
    repo = get_repo()
    if not body.get("apply"):
        fixes = reshift.plan(repo, skip_dates=skip)
        return jsonify({"fixes": [f.as_dict() for f in fixes],
                        "days": len(fixes), "rows": sum(len(f.rows) for f in fixes),
                        "message": (f"{len(fixes)}日ぶん {sum(len(f.rows) for f in fixes)}行を3直へ戻します"
                                    if fixes else "戻すものはありません(2直に3直の行は入っていません)")})
    log_button_click("import_reshift")
    result = reshift.apply(repo, skip_dates=skip)
    return jsonify(result.as_dict())


@bp.post("/api/settings/import/template")
def import_template():
    """見本(見出しだけのCSV)を書き出す。**これに貼れば読めます。**"""
    from nippou.services import csv_import as import_service

    ctx = work_context.get_context()
    if not ctx.admin:
        return jsonify(error_body(
            "not_admin", "見本の書き出しは管理者モードでのみ可能です")), 403
    path = import_service.default_template_path(SETTINGS.report_output_dir)
    try:
        written = import_service.write_template(path)
    except Exception as exc:                      # noqa: BLE001 - 画面に出して継続
        log.exception("見本を書けませんでした: %s", path)
        return jsonify(error_body("write_failed", f"書けませんでした: {exc}")), 500
    return jsonify({"path": str(written),
                    "message": f"見本を出しました: {written}"})


@bp.post("/api/settings/admin-password")
def change_admin_password():
    """管理者パスワードを変える。**値は保存も応答もしない**(撹拌して持つ)。"""
    body = request.get_json(silent=True) or {}
    if body.get("reset"):
        result = admin_password.reset(str(body.get("current", "")))
    else:
        result = admin_password.change(str(body.get("current", "")),
                                       str(body.get("new", "")),
                                       str(body.get("confirm", "")))
    if not result.ok:
        # どれも入力の形の誤り。**サーバの状態は動いていない**
        return jsonify(error_body(result.reason, result.message)), 400
    payload = settings_presenter.to_dict()
    payload["message"] = result.message
    return jsonify(payload)


# ------------------------------------------------------------------
# 配布設定(`nippou/distribution.py`)。python-web-tools と同じ3つの道で、
# どれも管理者パスワードが要る(面の鍵を開けてあれば、もう一度は聞かない)
# ------------------------------------------------------------------
def _distribution_reply(result):
    from nippou import distribution

    if not result.ok:
        status = 403 if result.reason == distribution.REFUSE_NEED_PASSWORD else 400
        if result.reason == distribution.REFUSE_FAILED:
            status = 500
        return jsonify(error_body(result.reason, result.message)), status
    # 読み込み直したら、参照の写しも捨てる(置き場所が変わっているかもしれない)
    from nippou import source_db

    source_db.forget()
    payload = settings_presenter.to_dict()
    alerts = [w for w in payload["distribution"]["warnings"] if w["level"] == "alert"]
    payload["message"] = result.message + (
        " ── ただし、このまま配ると困ることがあります(赤い札)" if alerts else "")
    return jsonify(payload)


@bp.post("/api/settings/distribution/export")
def export_distribution():
    """**この端末のいまの設定**を、配布設定として書き出す。

        {"items": ["access_dir", ...], "files": ["sounds"], "password": "..."}
    """
    from nippou import distribution

    body = request.get_json(silent=True) or {}
    items, files = body.get("items"), body.get("files", [])
    if not isinstance(items, list) or not isinstance(files, list) \
            or not all(isinstance(x, str) for x in items + files):
        return jsonify(error_body("bad_input", "入れる項目の形が違います。")), 400
    log_button_click("distribution_export", extra=",".join(items + files))
    return _distribution_reply(distribution.export(
        str(body.get("password", "")), items, files,
        admin=work_context.get_context().admin))


@bp.post("/api/settings/distribution/reapply")
def reapply_distribution():
    """置いてある配布設定を読み込み直す(**この端末にある値も上書き**)。"""
    from nippou import distribution

    body = request.get_json(silent=True) or {}
    log_button_click("distribution_reapply")
    return _distribution_reply(distribution.reapply(
        str(body.get("password", "")), admin=work_context.get_context().admin))


@bp.post("/api/settings/distribution/remove")
def remove_distribution():
    from nippou import distribution

    body = request.get_json(silent=True) or {}
    log_button_click("distribution_remove")
    return _distribution_reply(distribution.remove(
        str(body.get("password", "")), admin=work_context.get_context().admin))


@bp.get("/api/fs/list")
def fs_list():
    """サーバから見えるフォルダの一覧。

    返すのは**フォルダの名前**と、そこにある**取り込み元らしいファイルの
    名前**だけ。中身は返さない ── 読み取り口をここに作らない。
    """
    return jsonify(fs_browse.to_dict(fs_browse.browse(request.args.get("path", ""))))


def _ribbon(ctx, calc):
    """帯(いま書いているページで。`entry.ribbon_now`)。"""
    from .entry import ribbon_now

    return ribbon_now(ctx, calc)
