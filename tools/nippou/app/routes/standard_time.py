"""標準作業時間の画面 ── レールの「標準作業時間」

    標準作業時間は別タブ管理とし csv出力 同条件作業抽出 作業時間を表示

面は5つ(並びは `presenters/standard_time.TABS`)。「考え方と計算」は読むだけ:

    GET  /standard-time                       画面
    POST /api/standard-time/standards         標準(条件×班)の表
    POST /api/standard-time/standards/csv     同じ表をCSVに
    POST /api/standard-time/works             同条件の作業(作業時間・標準との差)
    POST /api/standard-time/works/csv         同じ表をCSVに
    POST /api/standard-time/power             梱包力(標準を 100 とした速さ)
    POST /api/standard-time/power/csv         同じ表をCSVに
    POST /api/standard-time/backfill          過去ぶんを入れる(この端末の集計から)

読むのは共有の `標準作業時間.sqlite3`(`services/standard_time`)。
**表を組み立てるのはサーバ**で、画面は写すだけです。
"""
from __future__ import annotations

from datetime import date

from flask import Blueprint, jsonify, render_template, request

from nippou import running, work_context
from nippou.config import SETTINGS
from nippou.logging_setup import get_logger, log_button_click
from nippou.presenters import settings as settings_view
from nippou.presenters import standard_time as view
from nippou.reporting import csv_export
from nippou.services import standard_time

from .. import error_body, get_repo, shell

log = get_logger("app.routes.standard_time")

bp = Blueprint("standard_time", __name__)


@bp.get("/standard-time")
def index():
    ctx = work_context.get_context()
    from .entry import current_calculator
    calc = current_calculator()
    today = ctx.business_date(calc)
    return render_template(
        "standard_time.html",
        line=ctx.line,
        tabs=view.tabs(), default_tab=view.DEFAULT_TAB,
        db_path=str(SETTINGS.standard_time_db_path),
        month_start=today.replace(day=1).isoformat(), today=today.isoformat(),
        out_dir=settings_view.standard_time_output_view(),
        power_groups=view.power_groups(), power_lead=view.POWER_LEAD,
        power_definition=view.POWER_DEFINITION, guide=view.guide(),
        **shell.shell_context("standard", ribbon=_ribbon(ctx, calc)))


def _line(payload: dict):
    """`scope: "all"` なら全ライン、それ以外はこの端末のライン。"""
    return None if payload.get("scope") == "all" else work_context.get_context().line


def _standards(payload: dict):
    filters = view.Filters.of(payload, _line(payload))
    return filters, view.build(SETTINGS.standard_time_db_path, filters)


@bp.post("/api/standard-time/standards")
def standards():
    _, built = _standards(request.get_json(silent=True) or {})
    return jsonify(built.as_dict())


@bp.post("/api/standard-time/standards/csv")
def standards_csv():
    """標準の表を、**画面の表そのまま**CSVにする(BOM付きUTF-8)。"""
    filters, built = _standards(request.get_json(silent=True) or {})
    out = csv_export.standard_time_output_path(
        SETTINGS.standard_time_output_dir, date.today().isoformat(), filters.line or "全ライン",
        by_worker=filters.by_worker, by_operator=filters.by_operator)
    try:
        count = csv_export.write_table(built.table, out)
    except Exception as exc:                      # noqa: BLE001 - 画面に出して継続
        log.exception("標準作業時間のCSVを書けませんでした")
        return jsonify(error_body("write_failed", f"出力に失敗しました: {exc}")), 500
    log_button_click("standard_time_csv", line=filters.line or "全ライン",
                     extra=filters.note())
    return jsonify({"file": str(out), "message": f"{built.table.title} {count}行\n{out}"})


def _works(payload: dict):
    try:
        filters = view.WorkFilters.of(payload, work_context.get_context().line)
    except view.BadRequest as exc:
        return None, None, (jsonify(error_body("bad_condition", str(exc),
                                               field=exc.field)), exc.status)
    return filters, view.build_works(SETTINGS.standard_time_db_path, filters), None


@bp.post("/api/standard-time/works")
def works():
    """同じ条件の作業を1件ずつ(作業時間と、標準でやったら何分かの差)。"""
    _, built, err = _works(request.get_json(silent=True) or {})
    if err:
        return err
    return jsonify(built.as_dict())


@bp.post("/api/standard-time/works/csv")
def works_csv():
    """同条件の作業を、**画面の表そのまま**CSVにする。"""
    filters, built, err = _works(request.get_json(silent=True) or {})
    if err:
        return err
    out = csv_export.works_output_path(SETTINGS.standard_time_output_dir,
                                       date.today().isoformat(), filters.line,
                                       filters.criteria)
    try:
        count = csv_export.write_table(built.table, out)
    except Exception as exc:                      # noqa: BLE001 - 画面に出して継続
        log.exception("同条件の作業のCSVを書けませんでした")
        return jsonify(error_body("write_failed", f"出力に失敗しました: {exc}")), 500
    log_button_click("standard_time_works_csv", line=filters.line or "全ライン",
                     extra=filters.note())
    return jsonify({"file": str(out), "message": f"{built.table.title} {count}行\n{out}"})


def _power(payload: dict):
    try:
        filters = view.PowerFilters.of(payload, _line(payload))
    except view.BadRequest as exc:
        return None, None, (jsonify(error_body("bad_condition", str(exc),
                                               field=exc.field)), exc.status)
    return filters, view.build_power(SETTINGS.standard_time_db_path, filters), None


@bp.post("/api/standard-time/power")
def power():
    """梱包力 ── 標準(全班)を 100 とした梱包の速さを、班・作業者・直・日・ラインごとに。"""
    _, built, err = _power(request.get_json(silent=True) or {})
    if err:
        return err
    return jsonify(built.as_dict())


@bp.post("/api/standard-time/power/csv")
def power_csv():
    """梱包力の表を、**画面の表そのまま**CSVにする。"""
    filters, built, err = _power(request.get_json(silent=True) or {})
    if err:
        return err
    out = csv_export.power_output_path(SETTINGS.standard_time_output_dir,
                                       date.today().isoformat(), filters.line or "全ライン",
                                       filters.group_label)
    try:
        count = csv_export.write_table(built.table, out)
    except Exception as exc:                      # noqa: BLE001 - 画面に出して継続
        log.exception("梱包力のCSVを書けませんでした")
        return jsonify(error_body("write_failed", f"出力に失敗しました: {exc}")), 500
    log_button_click("standard_time_power_csv", line=filters.line or "全ライン",
                     extra=filters.note())
    return jsonify({"file": str(out), "message": f"{built.table.title} {count}行\n{out}"})


@bp.post("/api/standard-time/backfill")
def backfill():
    """期間ぶんの集計(手元)を、共有の 標準作業時間.sqlite3 へ入れる。

    ふだんは「共有へ保存」のたびに入るので押しません。要るのは、この
    仕組みが入る**前に**保存したぶんと、班員名簿が読めなかったときに
    「不明」で入ったぶんを班付きで入れ直すとき。**何度押しても同じ結果**
    (同じ直は置き換え)。
    """
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
    log_button_click("standard_time_backfill", line=ctx.line, extra=f"{start}~{end}")
    try:
        with running.running("標準作業時間の取り込み"):
            result = standard_time.backfill(get_repo(), start, end, ctx.line,
                                            SETTINGS.standard_time_db_path)
    except Exception as exc:                      # noqa: BLE001 - 画面に出して継続
        log.exception("標準作業時間の取り込みに失敗しました")
        return jsonify(error_body(
            "backfill_failed", f"取り込みに失敗しました: {exc}")), 500
    body = result.as_dict()
    if not result.error and not result.shifts:
        body["message"] = "この期間に集計のある直はありませんでした"
    return jsonify(body)


def _ribbon(ctx, calc):
    """帯(いま書いているページで。`entry.ribbon_now`)。"""
    from .entry import ribbon_now

    return ribbon_now(ctx, calc)
