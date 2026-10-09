"""人員(作業者選択) / 全停入力

tkinter版では独立した Toplevel(`ui/staff_window.py` / `ui/formstop_window.py`)
だった。Web版では**日報入力画面のモーダル**にする ── どちらも「選んで、
いまの入力へ返す」操作で、選び終えたら入力画面へ戻るため。行き先が
変わらないものをレールに並べると、作業の順序が読めなくなる。

    GET  /api/staff/members     班員名簿を班ごとに返す
    POST /api/staff/apply       選んだ名前を担当者欄の文字列にする
    GET  /api/formstop/reasons  停止理由内訳を3カテゴリぶん返す
    POST /api/formstop/execute  全停入力を実行して保存する
"""
from __future__ import annotations

from datetime import datetime

from flask import Blueprint, jsonify, request

from nippou import work_context
from nippou.access_bridge import staff_master, stop_master
from nippou.logging_setup import get_logger, log_button_click
from nippou.logic import pages
from nippou.logic import staff as staff_logic
from nippou.logic import stop_reason
from nippou.presenters import entry as presenter

from .. import error_body, get_repo

log = get_logger("app.routes.staff")

bp = Blueprint("staff", __name__)


# ------------------------------------------------------------------
# 人員 (作業者選択)
# ------------------------------------------------------------------
@bp.get("/api/staff/members")
def members():
    """班員名簿。班ごとにまとめて返す(`人員()` + `GenerateOptionButtons`)。

    **マスタが読めなくても画面は開く。** 名前は手入力もできるので、
    取れなかったことを伝えて空で返す(起動そのものを失敗させない)。
    """
    try:
        found = staff_master.load_staff_members()
    except Exception:                             # noqa: BLE001 - 画面を落とさない
        log.exception("班員名簿の取得で予期しないエラーが発生しました")
        found = []

    groups = staff_logic.group_by_team(found)
    return jsonify({
        "teams": [{"team": team, "names": names}
                  for team, names in groups.items()],
        "total": len(found),
        "message": "" if found else
                   "班員名簿を取得できませんでした。名前は直接入力できます。",
    })


def _names(value) -> list[str]:
    return [str(n) for n in value] if isinstance(value, list) else []


@bp.post("/api/staff/preselect")
def preselect():
    """窓を開いたとき、**最初からチェックしておく名前**(担当者欄にもう居る人)。

    `roster` は窓に並んでいる名前(画面が持っている名簿そのもの)。名簿を
    もう一度読みに行かないので、窓を開くたびに共有へ行きません。
    """
    payload = request.get_json(silent=True) or {}
    return jsonify({"checked": staff_logic.preselect(
        str(payload.get("current", "")), _names(payload.get("roster")))})


@bp.post("/api/staff/apply")
def apply_names():
    """選んだ名前を担当者欄の文字列にする(`FindCheckedCheckboxes` 相当)。

    `current` と `roster` が付いていれば、**名簿に無い字(手で打った名前・
    書き添え)を消さずに残します**(v4.7.0 `staff.merge_worker`)。
    """
    payload = request.get_json(silent=True) or {}
    names = payload.get("names")
    if not isinstance(names, list) or not names:
        return jsonify(error_body("empty", "作業者を選んでください")), 422

    text = staff_logic.merge_worker(
        str(payload.get("current", "")), [str(n) for n in names],
        _names(payload.get("roster")), str(payload.get("trainee_suffix", "")))
    log_button_click("staff_names_output", extra=text)
    return jsonify({"worker": text, "message": f"{len(names)}名を反映しました"})


# ------------------------------------------------------------------
# 全停入力
# ------------------------------------------------------------------
@bp.get("/api/formstop/reasons")
def reasons():
    """停止理由内訳(`boxin()` 相当)。3カテゴリぶんまとめて返す。"""
    try:
        by_category = stop_master.load_all_categories()
    except Exception:                             # noqa: BLE001 - 画面を落とさない
        log.exception("停止理由マスタの取得で予期しないエラーが発生しました")
        by_category = {c: [] for c in stop_master.CATEGORY_TABLES}

    total = sum(len(v) for v in by_category.values())
    return jsonify({
        "categories": [
            {"category": category,
             "reasons": [{"label": r.label, "code": r.code} for r in items]}
            for category, items in by_category.items()
        ],
        "message": "" if total else
                   "停止理由を取得できませんでした。停止内訳.csv か伝送用ファイルを確認してください。",
    })


def _parse_hhmm(text: str):
    """`"22:50"`(全角も)を `time` に。読めなければ None(`logic/shift.parse_hhmm`)。"""
    from nippou.logic.shift import parse_hhmm

    return parse_hhmm(text)


def _reason_code_of(label: str) -> str:
    """停止理由の名前から**記号**(内訳番号)を引く。無ければ空。

    画面は記号も一緒に送ってきますが、古い画面から名前だけで来ることが
    あるので、ここで引き直せるようにしておきます。
    """
    try:
        by_category = stop_master.load_all_categories()
    except Exception:                             # noqa: BLE001 - 画面は出す
        log.exception("停止理由マスタを読めませんでした")
        return ""
    wanted = label.strip()
    for items in by_category.values():
        for r in items:
            if r.label.strip() == wanted and r.code:
                return r.code
    return ""


def _all_stop_page(report_date: str, line: str, shift: str):
    """その直に**既にある全停入力**のページ。無ければ None。

    見るのは1行目だけです ── 全停はそこにしか書きません
    (`logic/stop_reason.build_all_stop_entry`)。
    """
    for number in get_repo().saved_pages(report_date, line, shift):
        loaded = get_repo().load(report_date, line, shift, number)
        if loaded is None:
            continue
        rows = loaded[1]
        first = next((r for r in rows if r.row_no == 1), None)
        if first is not None and pages.is_all_stop_row(
                lot=first.lot, kz=first.kz, kh=first.kh,
                sz=first.sz, sh=first.sh, s=first.s):
            return number
    return None


def _all_stop_target_page(report_date: str, line: str, shift: str) -> int:
    """全停入力を書き込むページ。**空のページがあれば、それを使う。**

    VBA は全停のときに発行済みのシートを**消して**から書いていました
    (「すでにシート発行済みだった場合1を消す(残すと(2)がでてうざい)」)。
    空のページを残したまま足すと、1枚目が白紙の日報が出るためです。

    こちらは**消しません。** 打ってあるものを黙って消さないほうが大事
    なので、空のページがあればそこへ書き、打ってあれば次のページに足します
    ── VBA の狙い(白紙を残さない)は同じで、壊し方だけが違います。
    """
    repo = get_repo()
    saved = repo.saved_pages(report_date, line, shift)
    if not saved:
        return 1
    last = saved[-1]
    loaded = repo.load(report_date, line, shift, last)
    if loaded is not None and not pages.used_page(r.lot for r in loaded[1]):
        return last                               # 白紙のページを使い回す
    return repo.latest_page(report_date, line, shift) + 1


@bp.post("/api/formstop/execute")
def execute():
    """全停入力を実行する(`全停入力()` 相当)。

    その直の開始〜終了時刻・選んだ理由・実働分を**日報1行目にまとめて**
    書き込み、**新しいページとして**保存する。tkinter版 `_execute_all_stop`
    と同じ手順。
    """
    payload = request.get_json(silent=True) or {}
    label = str(payload.get("reason", "")).strip()
    # **欄に入るのは記号のほう。** 名前("ﾌｫｰｸ待ち")を入れると保存前チェックが
    # 「停止内訳にない記号です」で断り、そのページは以後保存できなくなります
    # (消した形を保存するところで同じ断りに当たるため)
    code = str(payload.get("code", "")).strip()
    if not label:
        return jsonify(error_body("empty", "停止理由を選んでください")), 422

    # **作業者を決める前には押させない。**
    #
    # 「作業者を選ぶ前に全停入力できてしまう」と言われました。全停は
    # 押した時点で**1ページを書いて保存します**。直の始まりは作業者を
    # 選ぶことだと決めた(`_view` の `needs_worker`)のに、その前に紙が
    # 1枚できてしまうと、**誰の直か分からないページ**が残ります。
    #
    # 画面側でもボタンを伏せますが(`paintNeedsWorker`)、決めるのはここ
    # です ── 画面だけで止めると、古い画面から押せてしまいます。
    # **この端末のラインが決まるまでは書かない**(v4.3.0。`entry.line_refusal`)
    from .entry import line_refusal

    refused = line_refusal()
    if refused:
        return refused
    worker = str(payload.get("worker", "")).strip()
    if not worker:
        return jsonify(error_body(
            "no_worker",
            "先に「作業者を選ぶ」で、この直の作業者を決めてください。\n"
            "全停入力は、押した時点でその直の1ページとして保存されます。")), 422
    if not code:
        # 古い画面から名前だけで来たときの保険。名前から記号を引き直す
        code = _reason_code_of(label)
        if not code:
            return jsonify(error_body(
                "unknown_reason",
                f"「{label}」の記号が内訳マスタに見つかりません。"
                "一覧から選び直してください")), 422

    ctx = work_context.get_context()
    from .entry import current_calculator

    calc = current_calculator()
    now = datetime.now()
    shift = calc.time_check(now, ctx.force_day_shift())

    # **直の開始・終了は、保存前チェックと同じところから取る。**
    #
    # ここは `calc.current_shift_*_time()` を使っていました。チェックの
    # ほうは `shift_check.bounds_of` を使うので、マスタの入り方によっては
    # **同じ直の終わりが2通り**になります ── 全停で書いた終了(既定の
    # 22:00)とチェックの定時(マスタの 22:50)が食い違い、「最終時間まで
    # 入力がないのでは？」が出続けました。打った覚えのない行のことを
    # 言われるので、何を直せばよいのか分かりません。
    from nippou.services import shift_check

    start_text, end_text = shift_check.bounds_of(
        get_repo().get_shift_times(), shift)
    start_t = _parse_hhmm(start_text)
    end_t = _parse_hhmm(end_text)
    if start_t is None or end_t is None:
        return jsonify(error_body(
            "no_shift", "いまの直の開始・終了時刻が分かりません。"
                        "設定画面で時間マスタを取り込んでください。")), 422

    entry = stop_reason.build_all_stop_entry(start_t, end_t, code)

    # 1行目だけを使う。他の行は空
    #
    # **作業者は残します。** VBA は `.Sname = ""` で消していましたが、
    # 全停のページも「その直の紙」です ── 誰が居た直なのか分からない
    # 紙が1枚だけ混ざると、あとから見たときに読めません。
    state = presenter.empty_state()
    state.header["worker"] = worker
    state.set(1, "KZ", entry.kz)
    state.set(1, "KH", entry.kh)
    state.set(1, "SZ", entry.sz)
    state.set(1, "SH", entry.sh)
    state.set(1, "S", entry.s)
    state.set(1, "TH", entry.th)

    report_date = _business_date_text(calc, ctx, now)

    # **直に1回きり。** 2回押せば全停が2つ並んだ日報ができてしまう
    already = _all_stop_page(report_date, ctx.line, shift)
    refusal = pages.all_stop_refusal(already)
    if refusal:
        log.info("全停入力を断りました(既に第%sページにあります) line=%s shift=%s",
                 already, ctx.line, shift)
        body = error_body("already_all_stop", refusal)
        body["page"] = already
        return jsonify(body), 422

    # **前の直の始末も同じ関門を通します。** 全停も1ページを書いて
    # 保存するので、ここだけ素通りだと「保存は止まるが全停は通る」に
    # なります(`logic/handover.py`)
    from .entry import _handover

    handed = _handover(ctx, key=(report_date, ctx.line, shift))
    if handed.blocked:
        body = error_body("handover_required", handed.message)
        body["handover"] = handed.as_dict()
        return jsonify(body), 422

    next_page = _all_stop_target_page(report_date, ctx.line, shift)
    header, details = presenter.to_records(
        state, report_date, ctx.line, shift, next_page)

    try:
        get_repo().save(header, details)
    except Exception as exc:                      # noqa: BLE001 - 画面に出して継続
        log.exception("全停入力の保存に失敗しました")
        return jsonify(error_body("save_failed", f"保存に失敗しました: {exc}")), 500

    # **画面の12行を通さずに書いた。** このあと古い画面から届く打ちかけ(閉じる
    # 間際の送信など)が、全停の行を空の行で上書きしないように(v4.24.0)
    from .entry import note_written_aside

    note_written_aside((report_date, ctx.line, shift, next_page))
    log.info("all stop executed shift=%s line=%s reason=%s page=%s",
             shift, ctx.line, label, next_page)
    return jsonify({
        "message": f"全停入力しました(第{next_page}ページ)",
        "page": next_page,
        "next": "/",
    })


def _business_date_text(calc, ctx, now) -> str:
    from nippou.services.nippou_service import format_business_date
    return format_business_date(calc.today_check(now, ctx.force_day_shift()))
