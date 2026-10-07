"""集計管理 (VBA の「集計シート」を人が読む場所にしたもの)

    GET  /agg              画面(日別 / 直別 / ロット別 / ロット一覧)
    POST /api/agg/tables   期間を変えて取り直す
    POST /api/agg/csv      画面に出ている表をCSVにする(4本)
    POST /api/agg/push-log      共有保存の履歴(表とグラフ)
    POST /api/agg/push-log/csv  共有保存の履歴をCSVにする

【共有保存の履歴は、画面が開いてから読む】
履歴は共有の `日報データ.sqlite3` にもあり、全端末の行はそちらにしか
ありません。共有が遅い・届かないときに画面ごと待たせないよう、表とグラフは
画面を出したあとで取りに行きます(`views/agg.js`)。

【VBAとの対応】
`Agg_OutPut` が集計シートへ並べていたものを表にしたのが「ロット一覧」、
`Aggre_Calcul` が帯に出していたものが「直別」です。**座標は捨て、内容と
計算だけ残しました** ── 列の意味は名前で決まります。

「ロット別」はVBAに無い追加です。ロット№・用途コード・用途名・納入先・
包装仕様NO を鍵にしてまとめ、**直をまたいだぶんは終わった直に足します**。

【出るものは画面と同じ】
CSVは画面の表をそのまま書き出します。列や並びが違うと、突き合わせる
たびに読み替えることになるので。
"""
from __future__ import annotations

from datetime import date

from flask import Blueprint, jsonify, render_template, request

from nippou import work_context
from nippou.config import SETTINGS
from nippou.logging_setup import get_logger, log_button_click
from nippou.presenters import agg_admin, push_log
from nippou.presenters import settings as settings_view
from nippou.reporting import csv_export

from .. import error_body, get_repo, shell

log = get_logger("app.routes.agg")

bp = Blueprint("agg", __name__)


def _range(payload: dict):
    """期間を読む。**形が違えば400、逆さなら422。**"""
    try:
        start = date.fromisoformat(str(payload.get("start", "")))
        end = date.fromisoformat(str(payload.get("end", "")))
    except ValueError:
        return None, (jsonify(error_body(
            "bad_date", "日付は yyyy-mm-dd の形式で入れてください")), 400)
    if start > end:
        return None, (jsonify(error_body(
            "bad_range", "開始日は終了日以前にしてください", field="start")), 422)
    return (start, end), None


@bp.get("/agg")
def index():
    ctx = work_context.get_context()
    from .entry import current_calculator
    calc = current_calculator()

    # 既定は月初〜当日。VBA の集計シートが常にその範囲を描いていた
    end = ctx.business_date(calc)
    start = end.replace(day=1)
    return render_template(
        "agg.html",
        agg=agg_admin.build(get_repo(), line=ctx.line, start=start,
                            end=end).as_dict(),
        # 共有保存の履歴の期間は、上の表とは別に選べる(既定は同じ月初〜当日)
        push_presets=push_log.period_presets(end),
        # 押す前に「どこへ出るか」を出す(設定・参照パスで変えられる)
        out_dir=settings_view.output_dir_view(),
        **shell.shell_context("agg", ribbon=_ribbon(ctx, calc)))


@bp.post("/api/agg/tables")
def tables():
    """期間を変えて取り直す。**表を作るのはいつもサーバ側の1か所。**"""
    span, err = _range(request.get_json(silent=True) or {})
    if err:
        return err
    start, end = span

    ctx = work_context.get_context()
    log_button_click("agg_tables", line=ctx.line, extra=f"{start}~{end}")
    return jsonify(agg_admin.build(get_repo(), line=ctx.line, start=start,
                                   end=end).as_dict())


@bp.post("/api/agg/csv")
def export_csv():
    """画面の4つの表を、そのままCSVにする。

    **ツールが無くても読める形**にしておくのが目的なので、Excel が
    そのまま開ける BOM付きUTF-8で書きます。
    """
    payload = request.get_json(silent=True) or {}
    span, err = _range(payload)
    if err:
        return err
    start, end = span

    ctx = work_context.get_context()
    view = agg_admin.build(get_repo(), line=ctx.line, start=start, end=end)
    written: list[str] = []
    try:
        for table in view.tables:
            out = csv_export.agg_output_path(
                SETTINGS.report_output_dir, table.key,
                start.isoformat(), end.isoformat(), ctx.line)
            count = csv_export.write_table(table, out)
            written.append(f"{table.title} {count}行\n{out}")
    except Exception as exc:                      # noqa: BLE001 - 画面に出して継続
        log.exception("集計CSVの書き出しに失敗しました")
        return jsonify(error_body("write_failed", f"出力に失敗しました: {exc}")), 500

    log_button_click("agg_csv", line=ctx.line, extra=f"{start}~{end}")
    return jsonify({"files": written, "message": "\n".join(written)})


def _push_log_scope(payload: dict):
    """`scope: "all"` なら全ライン、それ以外はこの端末のライン。"""
    ctx = work_context.get_context()
    return None if payload.get("scope") == "all" else ctx.line


@bp.post("/api/agg/push-log")
def push_log_view():
    """共有保存の履歴(期間は報告日)。**共有を読めなくても、この端末の記録は返す。**"""
    payload = request.get_json(silent=True) or {}
    span, err = _range(payload)
    if err:
        return err
    start, end = span
    line = _push_log_scope(payload)
    view = push_log.build(get_repo(), line=line, start=start, end=end,
                          db_path=SETTINGS.access_db_path)
    return jsonify(view.as_dict())


@bp.post("/api/agg/push-log/csv")
def push_log_csv():
    """共有保存の履歴を、**画面の表そのまま**CSVにする(BOM付きUTF-8)。"""
    payload = request.get_json(silent=True) or {}
    span, err = _range(payload)
    if err:
        return err
    start, end = span
    line = _push_log_scope(payload)
    view = push_log.build(get_repo(), line=line, start=start, end=end,
                          db_path=SETTINGS.access_db_path)
    out = csv_export.agg_output_path(SETTINGS.report_output_dir, "push_log",
                                     start.isoformat(), end.isoformat(),
                                     line or "全ライン")
    try:
        count = csv_export.write_table(view.table, out)
    except Exception as exc:                      # noqa: BLE001 - 画面に出して継続
        log.exception("共有保存の履歴のCSVを書けませんでした")
        return jsonify(error_body("write_failed", f"出力に失敗しました: {exc}")), 500
    log_button_click("push_log_csv", line=line or "全ライン", extra=f"{start}~{end}")
    return jsonify({"file": str(out),
                    "message": f"{view.table.title} {count}行\n{out}"})


def _ribbon(ctx, calc):
    """帯(いま書いているページで。`entry.ribbon_now`)。"""
    from .entry import ribbon_now

    return ribbon_now(ctx, calc)
