"""設定画面 (tkinter 版の ``settings`` + ``mode_select`` + ``admin_auth``)

【どのモードでも開ける】
取り込みの状態と接続先を確かめる場所なので、倉庫参照モードでも開けるように
する。ここへ来られないと「なぜデータが古いのか」を調べる手段が無くなる。

【管理者パスワード】
**この画面で守らないのは「読むこと」だけ**(マスタ・接続先の状態・看板集計)。 それ以外はすべて管理者
パスワードを要求する。

======================================== ==========
経路                                      パスワード
======================================== ==========
``GET  /settings`` / ``GET /api/settings``  不要(この画面自体の描画)
``GET  /api/master``                        **不要**(マスタの一覧)
``GET  /api/master/table``                  **不要**(マスタの中身を読む)
``POST /api/master/update``                 必要(1 マス直す)
``POST /api/master/insert``                 必要(1 行足す=看板を増やす)
``POST /api/master/delete``                 必要(1 行消す)
``POST /api/mode``                          **不要**(アクセス権限で決める。下を参照)
``GET  /api/access``                        **不要**(この端末の権限を読み直す)
``POST /api/access-db-path``                必要(アクセス権限の置き場所)
``GET  /api/history-db-status``             **不要**(看板履歴の置き場所と、届いているか)
``POST /api/history-db-path``               必要(看板履歴.sqlite3 の置き場所)
``POST /api/csv-dir``                       必要(CSV の書き出し先)
``POST /api/stats/csv/save``                **不要**(集計・看板履歴の CSV を書き出し先へ保存する)
``POST /api/stats/mistake-minutes``         必要(押し間違いとみなす時間)
``POST /api/line``                          必要
``POST /api/shared-db-path``                必要
``GET  /api/shared-db-status``              **不要**(いまの接続先と、届いているか)
``GET  /api/stats`` / ``/api/stats/csv``    **不要**(看板集計を読む)
``POST /api/reimport``                      必要
``POST /api/sync/discard-failed``           必要(送れない操作を捨てて共有DBの状態に戻す)
``POST /api/sync/retry-failed``             必要(あきらめた操作をもう一度送る)
``POST /api/fs/list``                       必要(この PC のフォルダを返す)
``POST /api/behavior``                      必要(取り込み・書き戻し間隔、自動印刷)
``POST /api/admin/password``                必要(今のパスワード。未設定なら不要)
``POST /api/admin/unlock``                  (パスワードを確かめて認証する)
``POST /api/admin/lock``                    不要(認証を解除するだけ)
``GET  /api/admin/state``                   不要(認証済みか)
``POST /api/table-refresh/upload|plan|run``  必要(Access の最新で表の中身を入れ替える)
``POST /api/distribution/export``           必要(配布設定を書き出す)
``POST /api/distribution/reapply``          必要(配布設定を読み込み直す。上書き)
``POST /api/distribution/remove``           必要(配布設定を消す)
``POST /api/distribution/build``            必要(配布用フォルダを作る)
======================================== ==========

読むことだけを開けておくのは、**調べる手段を塞がない**ため。「なぜデータが
古いのか」「マスタに何が入っているのか」を確かめるのに、いちいち管理者を
呼ばずに済む。一方、**状態を変えるものは 1 つ残らず守る** ── この道具は
共有 DB への書き込みが壊れると業務が成立しないので、誤操作の入口を作らない。

【モードはアクセス権限で決める】(:mod:`kanban.access_control`)
モードの切り替えだけはパスワードではなく、梱包資材マスタの「アクセス権限」に
この端末(ログインID・PC名)の行があるかで決める。パスワードは教え合えるが、
ログインIDとPC名は画面から変えられない。**権限の行を足すのはマスタ管理**で、
そこは管理者パスワードで守る(python-web-tools と同じ復旧経路)。

平文は保存せず、``config.json`` にはソルト付きハッシュだけを置く
(:func:`kanban.config.hash_password`)。まだパスワードが設定されていない
端末では、**最初の1回だけ登録を求める**(tkinter 版の
``admin_auth.authenticate`` と同じ流れ)。

【認証する】(:mod:`kanban.admin_lock`)
押すたびに訊いていたころは、マスタを 5 マス直せば 5 回打つことになり、
パスワードだけを入れる場所もありませんでした。いまは python-web-tools の
「マスタ編集の認証」と同じく、**1 度開けたら認証が切れるまで訊きません。**
閉まるのは「認証を解除する」・:data:`kanban.admin_lock.IDLE_LOCK_SEC` 使わない・
アプリを閉じる、のどれか。開きっぱなしのタブが解錠のまま残らないように、
使わない時間が続いたら自動で閉めます。
"""

from __future__ import annotations

import os
from pathlib import Path

from flask import Blueprint, current_app, jsonify, render_template, request

from kanban import admin_lock, app_config, config
from kanban.applog import get_logger
from kanban.db import store as store_module

from .. import current_mode, get_store
from ..shell import shell_context

log = get_logger("app.routes.settings")

bp = Blueprint("settings", __name__)


def _cfg() -> config.Config:
    """いまの設定。**毎回読み直す** ── 別のプロセス(別モード)が書き換えて
    いることがあるため。"""
    return config.load_config(current_app.config.get("CONFIG_PATH") or None)


@bp.get("/settings")
def page():
    store = get_store()
    cfg = _cfg()
    return render_template(
        "settings.html",
        state=_state(cfg, store),
        **shell_context("settings"),
    )


@bp.get("/api/settings")
def state():
    return jsonify(_state(_cfg(), get_store()))


def _state(cfg: config.Config, store) -> dict:
    """設定画面に出す値ぜんぶ。"""
    failures = store.sync_failures()
    # 起動したときに読んだ権限(描くたびに共有フォルダを待たせない。最新は JS が取り直す)
    grant = current_app.config.get("ACCESS_GRANT")
    return {
        "mode": current_mode(),
        "mode_label": config.mode_display_name(current_mode()),
        "modes": [
            {
                "key": key,
                "label": config.mode_display_name(key),
                "current": key == current_mode(),
                "port": app_config.port(key),
                "allowed": True if grant is None else grant.allows(key),
            }
            for key in config.ALL_MODES
        ],
        "access": _access_state(grant),
        "line_names": _line_names(),
        "history": _history_state(cfg),
        "csv_dir": cfg.resolved_csv_dir(),
        "mistake_minutes": cfg.mistake_minutes,
        "csv_dir_configured": bool(cfg.csv_dir.strip()),
        "places": _file_places(cfg, store),
        "line": cfg.line,
        "line_label": config.display_name(cfg.line) if cfg.line else "",
        "lines": [
            {"code": info.code, "label": info.display_name, "current": info.code == cfg.line}
            for info in config.LINE_MASTER
        ],
        "shared_db_path": cfg.resolved_shared_db_path(),
        # 決めてあるか(未設定なら既定の場所を使っている。「変更」で決められる)
        "shared_db_configured": bool(cfg.shared_db_path.strip()),
        "sqlite_path": store.path,
        "config_path": cfg.source_path,
        "app_id": app_config.app_id(),
        "version": app_config.version(),
        "version_label": app_config.version_label(),
        "version_problem": app_config.version_problem(),
        "app_config_path": str(app_config.CONFIG_PATH),
        "app_config_error": app_config.load_error(),
        "local_root": str(app_config.local_root()),
        "monitor_level": app_config.monitor_level(),
        "has_admin_password": bool(cfg.admin_password_hash),
        **_lock_state(),
        "imported_lines": store.imported_lines(),
        "pending": store.pending_count(),
        "pending_items": [_pending_row(r) for r in store.pending_details()],
        **_share_traffic(cfg),
        "failures": [
            {"line": f.get("line", ""), "mgmt_no": f.get("mgmt_no", ""),
             "error": f.get("sync_error", "")}
            for f in failures
        ],
        "distribution": _distribution_state(),
        "behavior": {
            "import_interval_sec": cfg.import_interval_sec,
            "export_interval_sec": cfg.export_interval_sec,
            "auto_print": cfg.auto_print,
        },
    }


# ------------------------------------------------------------------
# 看板集計(kanban/presenters/stats.py)
# ------------------------------------------------------------------
# **読むだけなのでパスワードは要らない**(マスタを読むのと同じ扱い)。
# 共有DBの [看板履歴] と各ラインの看板の表を読んで集計する。

def _stats_args():
    return _stats_args_from(request.args)


def _stats_args_from(args):
    from kanban.presenters import stats

    default_from, default_to = stats.default_months()
    start = stats.parse_month(str(args.get("from", "") or "")) or default_from
    end = stats.parse_month(str(args.get("to", "") or "")) or default_to
    months = stats.months_between(start, end)[-36:]      # 3 年を上限に(描き切れない)
    line = str(args.get("line", "") or "").strip()
    if line and not config.is_supported_line(line):
        line = ""
    return months, line


def _stats_data():
    """集計の材料。看板の枚数とコメントは共有DB、出来事は看板履歴.sqlite3 から。"""
    from kanban.db import history
    from kanban.presenters import stats

    shared, _configured = _shared_db()
    return stats.load(shared, history.open_db(_cfg()))


@bp.get("/api/stats")
def stats_view():
    from kanban.presenters import stats

    months, line = _stats_args()
    cfg = _cfg()
    data = stats.view(_stats_data(), months, line, cfg.mistake_minutes)
    data["csv_dir"] = cfg.resolved_csv_dir()
    return jsonify(data)


@bp.post("/api/stats/mistake-minutes")
def set_mistake_minutes():
    """看板集計で押し間違いとみなす時間(分)を変える。**管理者パスワードが要る。**

    記録はそのまま残しているので、変えればすぐ、過去の分も新しい時間で数え直す。
    """
    body = request.get_json(silent=True) or {}
    text = str(body.get("minutes", "")).strip()
    try:
        minutes = int(text)
    except ValueError:
        return jsonify(_err("bad_value", "分は 0 以上の整数で入れてください", "minutes")), 400
    if not 0 <= minutes <= 1440:
        return jsonify(_err("bad_value", "分は 0〜1440 で入れてください", "minutes")), 400
    ok, problem = _check_admin(body)
    if not ok:
        return problem
    cfg = _cfg()
    cfg.mistake_minutes = minutes
    try:
        config.save_config(cfg)
    except OSError as exc:
        return jsonify(_err("not_writable", f"設定を書けませんでした: {exc}")), 500
    log.info("押し間違いとみなす時間を %d 分にしました", minutes)
    return jsonify({"ok": True, "minutes": minutes,
                    "message": (f"押し間違いとみなす時間を {minutes} 分にしました(過去の分も数え直します)。"
                                if minutes else "時間では押し間違いを除かないようにしました。")})


def _csv_request(args):
    """``(kind, months, line, 中身)``。断るときは ``(None, 応答)``。"""
    from kanban.presenters import stats

    kind = str(args.get("kind", ""))
    if kind not in stats.CSV_KINDS:
        return None, (jsonify(_err("bad_kind", "その種類の CSV はありません")), 400)
    months, line = _stats_args_from(args)
    data = _stats_data()
    if not data.available:
        return None, (jsonify(_err("unavailable", data.why)), 503)
    # **Excel でそのまま開けるよう、BOM 付き UTF-8・CRLF**
    body = ("\ufeff" + stats.build_csv(kind, data, months, line, _cfg().mistake_minutes)).encode("utf-8")
    return (kind, months, line, body), None


@bp.post("/api/stats/csv/save")
def stats_csv_save():
    """CSV を**書き出し先のフォルダへ保存する**(設定の「CSV の書き出し先」)。

    読むだけの集計を書き出すだけなのでパスワードは要らない。同じ名前のファイルがあれば
    上書きせず、時刻を付けた名前にする(前に書き出したものを黙って消さない)。
    """
    from datetime import datetime

    from kanban.presenters import stats

    body = request.get_json(silent=True) or {}
    got, refused = _csv_request(body)
    if refused:
        return refused
    kind, months, line, content = got
    cfg = _cfg()
    folder = Path(cfg.resolved_csv_dir())
    try:
        if not cfg.csv_dir.strip():
            folder.mkdir(parents=True, exist_ok=True)   # 既定の場所(この端末のローカル領域)は作る
        if not folder.is_dir():
            return jsonify(_err("no_folder", f"CSV の書き出し先のフォルダが見つかりません: {folder}\n"
                                "「接続先」タブの「CSV の書き出し先」を確かめてください。")), 400
        name = stats.csv_file_name(kind, months, line)
        target = folder / f"{name}.csv"
        if target.exists():
            target = folder / f"{name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        target.write_bytes(content)
    except OSError as exc:
        return jsonify(_err("not_writable", f"CSV を書けませんでした: {folder}({exc})")), 500
    log.info("CSV を書き出しました: %s", target)
    return jsonify({"ok": True, "path": str(target), "folder": str(folder),
                    "message": f"CSV を書き出しました: {target}"})


@bp.get("/api/stats/csv")
def stats_csv():
    """集計を CSV で渡す(ブラウザでダウンロード)。**Excel でそのまま開けるよう、BOM 付き UTF-8・CRLF。**"""
    from urllib.parse import quote

    from kanban.presenters import stats

    got, refused = _csv_request(request.args)
    if refused:
        return refused
    kind, months, line, body = got
    name = stats.csv_file_name(kind, months, line)
    return current_app.response_class(
        body,
        mimetype="text/csv",
        headers={
            # 日本語のファイル名はそのままでは送れない(RFC 5987 の形で送る)
            "Content-Disposition": f"attachment; filename=kanban_stats.csv; "
                                   f"filename*=UTF-8''{quote(name + '.csv')}",
        },
    )


# ------------------------------------------------------------------
# 表の中身だけを Access の最新に入れ替える(kanban/table_refresh.py)
# ------------------------------------------------------------------
# Access をまるごと変換して差し替えると、このツールが共有DBに足した表・列・行
# (看板履歴・看板コメント・看板の状態)が消える。両方にある表の中身だけを入れ替える。
# どれも管理者パスワードが要る(この PC のファイルを読む・共有DBを書き換える)。

def _refresh_work_dir():
    return app_config.local_dir("work") / "中身を入れ替える"


@bp.post("/api/table-refresh/upload")
def table_refresh_upload():
    """落とされたファイル(.accdb / .sqlite3)を受け取り、そのまま中を見る。"""
    from kanban import table_refresh

    ok, problem = _check_admin({"password": request.form.get("password", ""),
                                "password_confirm": request.form.get("password_confirm", "")})
    if not ok:
        return problem
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify(_err("no_file", "ファイルを落としてください")), 400
    saved, why = table_refresh.save_upload(upload.filename, upload.stream, _refresh_work_dir())
    if saved is None:
        return jsonify(_err("bad_file", why)), 400
    shared, _configured = _shared_db()
    return jsonify({"path": str(saved),
                    "plan": table_refresh.plan_dict(table_refresh.plan(str(saved), shared))})


@bp.post("/api/table-refresh/plan")
def table_refresh_plan():
    """選んだファイルの中を見る(書かない)。両方にある表と、入れ替えられるか。"""
    from kanban import table_refresh

    body = request.get_json(silent=True) or {}
    ok, problem = _check_admin(body)
    if not ok:
        return problem
    shared, _configured = _shared_db()
    return jsonify({"plan": table_refresh.plan_dict(table_refresh.plan(str(body.get("path", "")), shared))})


@bp.post("/api/table-refresh/run")
def table_refresh_run():
    """選んだ表の中身を入れ替える。`{"path": …, "tables": […]}`。終わったら取り込み直す。"""
    from kanban import table_refresh

    body = request.get_json(silent=True) or {}
    ok, problem = _check_admin(body)
    if not ok:
        return problem
    tables = body.get("tables")
    if not isinstance(tables, list):
        return jsonify(_err("bad_tables", "入れ替える表の指定が正しくありません")), 400
    shared, _configured = _shared_db()
    path = str(body.get("path", ""))
    result = table_refresh.refresh(path, shared, [str(t) for t in tables], app_config.local_dir("backup"))

    # この端末は、いま取り込み直す(看板・マスタ管理にすぐ出す)。ほかの端末は次の取り込みで
    reimported = ""
    if result.refreshed:
        hook = current_app.config.get("MANUAL_REFRESH")
        if hook is not None:
            try:
                hook()
                reimported = "この端末は取り込み直しました。ほかの端末には次の取り込み(30 秒ほど)で届きます。"
            except Exception as exc:  # noqa: BLE001 - 入れ替えは済んでいる
                log.exception("入れ替えのあとの取り込みに失敗")
                reimported = f"入れ替えは済みましたが、この端末の取り込みに失敗しました: {exc}"
    payload = {
        "ok": result.ok, "message": result.message, "notes": result.notes, "reimported": reimported,
        "refreshed": [{"name": n, "before": b, "after": a} for n, b, a in result.refreshed],
        "backup": result.backup,
        "written": result.written, "written_at": result.written_at, "verified": result.verified,
        "plan": table_refresh.plan_dict(table_refresh.plan(path, shared)),
    }
    if result.ok:
        return jsonify(payload)
    payload["error"] = {"code": result.reason, "message": result.message}
    status = {table_refresh.REFUSE_NOTHING: 400, table_refresh.REFUSE_NO_FILE: 400,
              table_refresh.REFUSE_SAME_FILE: 400}.get(result.reason, 422)
    return jsonify(payload), status


# ------------------------------------------------------------------
# 配布設定(python-web-tools と同じつくり。kanban/distribution.py)
# ------------------------------------------------------------------
# 1 台で決めた設定を、アプリのフォルダごと配った先でそのまま使う。
# 書き出し・読み込み直し・消す・配布用フォルダを作る、はどれも管理者
# パスワードが要る(認証済みなら訊かない)。

def _distribution_state() -> dict:
    from kanban import distribution

    return distribution.summary()


def _distribution_reply(result, **extra):
    from kanban import distribution

    if not result.ok:
        status = 500 if result.reason in ("failed", "verify_failed") else 400
        return jsonify({
            **_err(result.reason or "failed", result.message),
            "checks": result.checks,
            "distribution": _distribution_state(),
        }), status
    return jsonify({
        "ok": True,
        "message": result.message,
        "applied": result.applied,
        "kept": result.kept,
        "checks": result.checks,
        "distribution": distribution.summary(),
        **extra,
    })


@bp.post("/api/distribution/export")
def distribution_export():
    """この端末の設定を ``配布設定\\`` に書き出す。**書き出した直後に読み戻して確かめる。**"""
    from kanban import distribution

    body = request.get_json(silent=True) or {}
    items = [str(k) for k in (body.get("items") or [])]
    ok, refused = _check_admin(body)
    if not ok:
        return refused
    return _distribution_reply(distribution.export(items))


@bp.post("/api/distribution/reapply")
def distribution_reapply():
    """配布設定を**上書きして**読み込み直す(この端末で直した値も配布設定に揃える)。"""
    from kanban import distribution

    body = request.get_json(silent=True) or {}
    ok, refused = _check_admin(body)
    if not ok:
        return refused
    before = _cfg().resolved_shared_db_path()
    result = distribution.reapply()
    if result.ok:
        after = _cfg().resolved_shared_db_path()
        reconnect = current_app.config.get("RECONNECT")
        if after != before and reconnect is not None:
            # 接続先が変わったなら、その場で繋ぎ直して取り込む(開き直しは要らない)
            try:
                reconnect(after)
                result.message += " 新しい接続先に繋ぎ直して取り込みました。"
            except Exception as exc:  # noqa: BLE001 - 読み込んだことは変わらない
                log.exception("配布設定の読み込み後の繋ぎ直しに失敗")
                result.message += f" 新しい接続先への繋ぎ直しに失敗しました: {exc}"
        result.message += " 取り込み・書き戻しの間隔は次にアプリを開いたときから効きます。"
    return _distribution_reply(result)


@bp.post("/api/distribution/remove")
def distribution_remove():
    from kanban import distribution

    body = request.get_json(silent=True) or {}
    ok, refused = _check_admin(body)
    if not ok:
        return refused
    return _distribution_reply(distribution.remove())


def _make_dist_module():
    """``scripts/make_dist.py`` を読み込む(scripts はパッケージではないので、場所で読む)。"""
    import importlib.util

    path = config.APP_ROOT / "scripts" / "make_dist.py"
    spec = importlib.util.spec_from_file_location("kanban_make_dist", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@bp.post("/api/distribution/build")
def distribution_build():
    """配布用フォルダを作る(``scripts\\make_dist.bat`` と同じもの)。

    アプリの隣に日時付きの名前で作る ── 前に作ったものに重ねない。
    一時期の版がアプリのフォルダに置いていた ``data\\`` は入れない。
    """
    body = request.get_json(silent=True) or {}
    ok, refused = _check_admin(body)
    if not ok:
        return refused
    make_dist = _make_dist_module()
    try:
        out, lines = make_dist.build(
            make_dist.default_out(stamp=True),
            with_settings=bool(body.get("with_settings", True)),
        )
    except SystemExit as exc:
        return jsonify(_err("build_failed", str(exc))), 400
    except OSError as exc:
        return jsonify(_err("build_failed", f"配布用フォルダを作れませんでした: {exc}")), 500
    log.info("配布用フォルダを作りました: %s", out)
    return jsonify({"ok": True, "path": str(out), "lines": lines,
                    "message": f"配布用フォルダを作りました: {out}"})


_BEHAVIOR_LABELS = {
    "import_interval_sec": "取り込み間隔(秒)",
    "export_interval_sec": "書き戻し間隔(秒)",
}


@bp.post("/api/behavior")
def save_behavior():
    """取り込み・書き戻しの間隔と自動印刷を保存する。**管理者パスワードが要る。**

    この端末の設定(``%APPDATA%\\KanbanSystem\\config.json``)に入る。配った先も
    同じにしたいときは「配布設定」で書き出す。効くのは次に起動したときから。
    """
    body = request.get_json(silent=True) or {}
    raw = body.get("values") or {}
    values: dict = {}
    for key, label in _BEHAVIOR_LABELS.items():
        text = str(raw.get(key, "")).strip()
        if not text:
            return jsonify(_err("bad_value", f"「{label}」を入れてください", key)), 400
        try:
            number = int(text)
        except ValueError:
            return jsonify(_err("bad_value", f"「{label}」は数字で入れてください", key)), 400
        if not 0 <= number <= 86400:
            return jsonify(_err("bad_value", f"「{label}」は 0〜86400 で入れてください", key)), 400
        values[key] = number
    values["auto_print"] = bool(raw.get("auto_print"))

    ok, refused = _check_admin(body)
    if not ok:
        return refused

    cfg = _cfg()
    for key, value in values.items():
        setattr(cfg, key, value)
    try:
        config.save_config(cfg)
    except OSError as exc:
        return jsonify(_err("not_writable", f"設定を書けませんでした: {exc}")), 500
    log.info("動作の設定を保存しました: %s", values)
    return jsonify({
        "ok": True,
        "message": "保存しました。取り込み・書き戻しの間隔は、次にアプリを開いたときから効きます。",
    })


#: 未反映の列(論理名)を、利用者の言葉へまとめる。
#:
#: **列名で出しても伝わらない。** ``want,unwant,ordered_at`` と並べられても、
#: 見たいのは「その看板の何を押したのか」です。3 つの操作にまとめます。
_PENDING_LABELS = {
    "want": "発注", "unwant": "発注", "ordered_at": "発注",
    "shipped": "発送", "confirmed_at": "発送",
    "hold": "注文中", "hold_at": "注文中",
}


def _pending_row(row: dict) -> dict:
    """未反映 1 件を、画面に出せる形にする。"""
    columns = [c for c in (row.get("dirty_columns") or "").split(",") if c]
    what: list[str] = []
    for col in columns:
        label = _PENDING_LABELS.get(col, col)
        if label not in what:
            what.append(label)
    attempts = int(row.get("sync_attempts") or 0)
    return {
        "line": config.display_name(row.get("line", "")),
        "mgmt_no": row.get("mgmt_no", ""),
        "material": row.get("material", ""),
        "size": row.get("size", ""),
        # 列が分からない古い行は「すべて」と言う(安全側に倒して全列送る)
        "what": " / ".join(what) or "すべて",
        "at": row.get("updated_at", ""),
        "attempts": attempts,
        "error": row.get("sync_error", ""),
        # 諦めた行(要確認)。待っても減らないので、そう言う
        "given_up": attempts >= store_module.MAX_SYNC_ATTEMPTS,
    }


def _path_problem(path: str) -> str:
    """その場所を接続先にしてよいか。よければ空文字。

    **「あとで届くかもしれない」は通します。** 共有フォルダはよく一時的に
    落ちるので、そこまで厳しくすると直せなくなります。ここで断るのは
    **打った本人にその場で分かること**だけ ── 無い・ファイルではない・
    SQLite として開けない、の 3 つです。
    """
    target = Path(path)
    try:
        if not target.exists():
            return (
                f"その場所が見つかりません: {path}\n"
                "綴りを確かめるか、「参照...」から選んでください。"
            )
        if not target.is_file():
            return f"ファイルではありません: {path}"
    except OSError as exc:
        # UNC の綴り違いなどは exists() の時点で例外になることがある
        return f"その場所を確かめられません: {path}({exc})"

    from kanban.db.shared import _is_usable_sqlite

    if not _is_usable_sqlite(target):
        return (
            f"SQLite として開けません: {path}\n"
            "変換前の .accdb を指していないか確かめてください(1.2.1)。"
        )
    # **看板マスタでなければ断る。** 開けるだけで通していたので、梱包資材マスタを
    # 選んでも通り、このツールの表(看板履歴・看板コメント)をそこへ作っていた
    from kanban.db.shared import kanban_tables_in, not_kanban_reason

    try:
        kanban, others = kanban_tables_in(target)
    except Exception as exc:  # noqa: BLE001 - 読めないなら断る
        return f"中の表を確かめられません: {path}({exc})"
    if not kanban:
        return not_kanban_reason(path, others)
    return ""


def _share_traffic(cfg: config.Config) -> dict:
    """共有フォルダをどれだけ往復しているか。

    **見えないと調整のしようがありません。** 読むときは共有ファイルを丸ごと
    手元へ写す作りなので、費用は「ファイルの大きさ × 写した回数」です。
    中身が変わっていなければ写さないため、**写した回数が伸び続けるなら
    誰かが書き続けている**ということになります(心拍・書き戻しの間隔を
    見直す手がかり)。

    ファイルに届かない端末で騒がないよう、読めなければ黙って 0 を返します。
    """
    size = 0
    try:
        path = Path(cfg.resolved_shared_db_path())
        if path.is_file():
            size = path.stat().st_size
    except OSError:
        size = 0

    gateway = current_app.config.get("GATEWAY")
    return {
        "shared_db_size": size,
        "shared_db_copies": int(getattr(gateway, "copies_made", 0) or 0),
        "started_at": current_app.config.get("STARTED_AT", 0),
    }


# ------------------------------------------------------------------
# 管理者パスワード
# ------------------------------------------------------------------
def _check_admin(body: dict) -> tuple[bool, object]:
    """管理者パスワードを確かめる。

    戻り値は ``(通ったか, 通らなかったときの応答)``。
    """
    cfg = _cfg()
    supplied = str(body.get("password", ""))

    # **認証済みなら訊かない**(開いているあいだに使えば、閉まるまでを延ばす)
    if cfg.admin_password_hash and not supplied and admin_lock.get().is_unlocked(touch=True):
        return True, None

    if not cfg.admin_password_hash:
        # まだ登録されていない。**最初の1回だけ**ここで登録する
        confirm = str(body.get("password_confirm", ""))
        if not supplied.strip():
            return False, (
                jsonify(
                    _err("need_password_setup", "管理者パスワードが未設定です。新しく設定してください。")
                ),
                401,
            )
        if supplied != confirm:
            return False, (
                jsonify(_err("password_mismatch", "確認用のパスワードが一致しません", "password_confirm")),
                400,
            )
        cfg.admin_password_hash = config.hash_password(supplied)
        config.save_config(cfg)
        admin_lock.get().unlock()
        log.info("管理者パスワードを新しく設定しました")
        return True, None

    if not supplied:
        # 認証が切れている(閉めた・時間が経った)。画面はこれを見て訊き直す
        return False, (
            jsonify(_err("locked", "認証が切れています。管理者パスワードを入れてください", "password")),
            401,
        )
    if not config.verify_password(supplied, cfg.admin_password_hash):
        log.warning("管理者パスワードが違います")
        return False, (jsonify(_err("bad_password", "パスワードが違います", "password")), 401)
    # **確認画面でパスワードを入れたら、認証済みになる。** 続けて操作するたびに
    # 打たせない(閉めるのは「認証を解除する」か、使わない時間が続いたとき)
    admin_lock.get().unlock()
    return True, None


def _lock_state() -> dict:
    lock = admin_lock.get()
    unlocked = lock.is_unlocked()
    return {
        "admin_unlocked": unlocked,
        "admin_unlock_remaining_sec": lock.remaining_sec() if unlocked else 0,
        "admin_idle_lock_min": int(lock.idle_sec // 60),
    }


@bp.get("/api/admin/state")
def admin_state():
    """認証済みか。画面は、訊く前にこれで確かめる。"""
    return jsonify({"has_admin_password": bool(_cfg().admin_password_hash), **_lock_state()})


@bp.post("/api/admin/unlock")
def admin_unlock():
    """パスワードを確かめて認証する(設定画面の「パスワード認証」)。

    **認証するためだけの入口。** 以前は、守られた操作を押したときに出る
    確認画面でしか入れられなかった。
    """
    body = request.get_json(silent=True) or {}
    cfg = _cfg()
    if not cfg.admin_password_hash:
        return jsonify(_err(
            "need_password_setup",
            "管理者パスワードがまだありません。「パスワード認証」で決めてください。",
        )), 400
    if not str(body.get("password", "")):
        return jsonify(_err("bad_password", "パスワードを入れてください", "password")), 400
    ok, refused = _check_admin({"password": body.get("password")})
    if not ok:
        return refused
    log.info("管理者パスワードで認証しました")
    return jsonify({"ok": True, "message": "認証しました。", "has_admin_password": True,
                    **_lock_state()})


@bp.post("/api/admin/lock")
def admin_lock_now():
    """認証を解除する。何も変えないのでパスワードは要らない。"""
    admin_lock.get().lock()
    log.info("管理者パスワードの認証を解除しました")
    return jsonify({"ok": True, "message": "認証を解除しました。", **_lock_state()})


@bp.post("/api/admin/verify")
def verify():
    """パスワードだけ確かめる(画面が操作を出す前の確認)。"""
    ok, problem = _check_admin(request.get_json(silent=True) or {})
    if not ok:
        return problem
    return jsonify({"ok": True})


def _new_password_problem(new: str, confirm: str, *, field: str, confirm_field: str):
    """新しいパスワードの形を確かめる。問題なければ ``None``。"""
    if not new.strip():
        return jsonify(_err("bad_password", "新しいパスワードを入れてください", field)), 400
    if new != confirm:
        return (
            jsonify(_err("password_mismatch", "確認用のパスワードが一致しません", confirm_field)),
            400,
        )
    return None


@bp.post("/api/admin/password")
def change_password():
    """この端末の管理者パスワードを設定する・変える。

    **パスワードを入れる場所が、操作のときに出る確認画面しか無かった。**
    そのため 1 度決めたパスワードを変える手段が無く、未設定の端末では
    「最初に保護された操作をした人」が決めるしかなかった。ここはそれ専用の口。

    * まだ無ければ: 新しいパスワード + 確認 で設定する
    * もうあれば: **今のパスワード**を確かめてから変える(配布設定から来た
      パスワードでも同じ)

    **配布設定に入れたパスワードは変わりません。** 配布先も変えるときは
    配布設定を保存し直す必要がある ── 画面にもそう出す。
    """
    body = request.get_json(silent=True) or {}
    new = str(body.get("new_password", ""))
    confirm = str(body.get("new_password_confirm", ""))
    problem = _new_password_problem(
        new, confirm, field="new_password", confirm_field="new_password_confirm"
    )
    if problem:
        return problem

    cfg = _cfg()
    had = bool(cfg.admin_password_hash)
    if had and not config.verify_password(
        str(body.get("current_password", "")), cfg.admin_password_hash
    ):
        log.warning("管理者パスワードの変更: 今のパスワードが違います")
        return jsonify(_err("bad_password", "今のパスワードが違います", "current_password")), 401

    cfg.admin_password_hash = config.hash_password(new)
    try:
        config.save_config(cfg)
    except OSError as exc:
        return jsonify(_err("not_writable", f"設定を書けませんでした: {exc}")), 500
    admin_lock.get().unlock()
    log.info("管理者パスワードを%sしました", "変更" if had else "設定")

    message = (
        f"管理者パスワードを{'変更' if had else '設定'}しました。"
        "配った先も同じパスワードにしたいときは、「配布設定」で書き出してください。"
    )
    return jsonify({"ok": True, "changed": had, "message": message})


# ------------------------------------------------------------------
# モード
# ------------------------------------------------------------------
@bp.post("/api/mode")
def set_mode():
    """この端末のモードを変える。

    モードは**ローカル SQLite**に持つ(tkinter 版と同じ)。``config.json`` では
    なく SQLite に置くのは、設定ファイルを共有フォルダに置いた運用でも端末
    ごとに別のモードで動けるようにするため。

    **動いているプロセスのモードは変えません。** モードはポートとロックに
    結びついています(現場 8741 / 倉庫 8751 / 倉庫参照 8761)。走ったまま
    名乗りだけ変えると、``site.lock`` を掴んだまま「倉庫です」と答える
    プロセスになり、**次の起動が別人と誤認して 2 つめを立ち上げます**
    ── 実際にそうなりました。

    ここでは「次に開いたときのモード」を記録するだけです。開き直すと
    :func:`launch_guard.check_existing` が古いモードのプロセスを終わらせて
    新しいモードで立て直します。

    【関門はアクセス権限】(:mod:`kanban.access_control`)
    以前は管理者パスワードだけが関門だった。パスワードは教え合えるので、
    **梱包資材マスタの「アクセス権限」にこの端末(ログインID・PC名)の行が
    あるか**で決める。パスワードは訊かない。倉庫参照は何も変えないので問わない。
    """
    from kanban import access_control

    body = request.get_json(silent=True) or {}
    mode = str(body.get("mode", "")).strip()
    if mode not in config.ALL_MODES:
        return jsonify(_err("bad_mode", "そのモードは選べません", "mode")), 400

    # **切り替えるたびに読み直す。** マスタ管理で行を足した直後にも効くように
    grant = access_control.grant_for(_cfg(), get_store())
    current_app.config["ACCESS_GRANT"] = grant
    if not grant.allows(mode):
        label = config.mode_display_name(mode)
        log.info("モードを変えませんでした(権限なし): %s %s", grant.identity.label(), mode)
        body = _err("no_permission",
                    f"この端末({grant.identity.label()})には{label}の権限がありません。"
                    f"{grant.how_to_allow(mode)}"
                    + (f"\n({grant.reason})" if grant.reason else ""))
        body["access"] = _access_state(grant)
        return jsonify(body), 403

    store = get_store()
    if mode == current_mode() and store.get_device_mode() == mode:
        return jsonify({"ok": True, "changed": False, "mode": mode})

    store.set_device_mode(mode)
    label = config.mode_display_name(mode)
    log.info(
        "次に開いたときのモードを %s にしました(いまは %s のまま動いています / %s)",
        mode, current_mode(), grant.identity.label(),
    )
    return jsonify({
        "ok": True, "changed": True, "mode": mode, "label": label,
        "running_mode": current_mode(),
        "message": f"次にアプリを開いたときから{label}になります。",
    })


# ------------------------------------------------------------------
# アクセス権限(kanban/access_control.py)
# ------------------------------------------------------------------
def _line_names() -> list[dict]:
    from kanban import line_names

    return line_names.table_rows()


def _access_state(grant=None) -> dict:
    """この端末の権限と、アクセス権限の置き場所。

    ``grant`` が無ければ**起動したときに読んだもの**(``ACCESS_GRANT``)を使う ──
    設定画面を描くたびに共有フォルダを待たせない。最新は ``GET /api/access`` で取る。
    """
    from kanban import access_control

    cfg = _cfg()
    grant = grant or current_app.config.get("ACCESS_GRANT")
    data = {
        "path": cfg.resolved_access_db_path(),
        "configured": bool(cfg.access_db_path.strip()),
        "default_path": config.Config(shared_db_path=cfg.resolved_shared_db_path())
        .resolved_access_db_path(),
        "table": access_control.TABLE,
        "grant": grant.to_dict() if grant is not None else None,
        "problems": [],
    }
    return data


@bp.get("/api/access")
def access_state():
    """この端末の権限を**いま**読み直す。読むだけなのでパスワードは要らない。"""
    from kanban import access_control

    cfg = _cfg()
    db = access_control.open_db(cfg)
    grant = access_control.grant_for(cfg, get_store(), db=db)
    current_app.config["ACCESS_GRANT"] = grant
    data = _access_state(grant)
    # **読み直したら当てる**(表のラインが変わっていれば。日報の「読み直す」と同じ)
    data["line_applied"] = _apply_line_from_access(grant)
    data["current_line"] = current_app.config.get("LINE", "")
    data["path_problem"] = access_control.path_problem(db.path)
    data["table_exists"] = False
    data["rows"] = 0
    if not data["path_problem"]:
        try:
            rules, has_table = access_control.read_rules(db)
        except access_control.Unavailable as exc:
            data["path_problem"] = str(exc)
        else:
            data["table_exists"] = has_table
            data["rows"] = len(rules)
            data["problems"] = access_control.problems(rules)
    return jsonify(data)


@bp.post("/api/access-db-path")
def set_access_db_path():
    """アクセス権限を読む梱包資材マスタの場所を変える。**管理者パスワードが要る。**

    空にすると既定(共有DBと同じフォルダの ``梱包資材マスタ.sqlite3``)に戻す。
    **看板マスタは断る**(ほかのツールと同じ表を使うため、置き場所は梱包資材マスタ)。
    保存したら表が無ければ作る。
    """
    from kanban import access_control

    body = request.get_json(silent=True) or {}
    path = str(body.get("path", "")).strip()

    ok, problem = _check_admin(body)
    if not ok:
        return problem

    cfg = _cfg()
    target = path or config.Config(shared_db_path=cfg.resolved_shared_db_path()).resolved_access_db_path()
    bad = access_control.path_problem(target)
    if bad and path:
        # 打ったパスが違う。**保存しない**(接続先と同じ考え方)
        return jsonify(_err("unreachable_path", bad, "path")), 400

    cfg.access_db_path = path
    try:
        config.save_config(cfg)
    except OSError as exc:
        return jsonify(_err("not_writable", f"設定を書けませんでした: {exc}")), 500
    log.info("アクセス権限の置き場所を変えました: %s", path or "(既定)")

    message = (f"アクセス権限の置き場所を{'既定に戻しました' if not path else '変えました'}: {target}。")
    if bad:
        message += f" ただし、いまは使えません: {bad}"
    else:
        created_ok, created = access_control.ensure_table(access_control.open_db(cfg))
        if created:
            message += f" {created}。" if created_ok else f" ただし: {created}"
    grant = access_control.grant_for(cfg, get_store())
    current_app.config["ACCESS_GRANT"] = grant
    return jsonify({"ok": True, "path": target, "configured": bool(path), "message": message,
                    "access": _access_state(grant)})


# ------------------------------------------------------------------
# 看板履歴の置き場所(kanban/db/history.py)
# ------------------------------------------------------------------
def _history_state(cfg: config.Config | None = None, *, look: bool = False) -> dict:
    """看板履歴.sqlite3 の場所。``look`` なら届くか・何行あるかまで見る(共有フォルダを読む)。"""
    from kanban.db import history

    cfg = cfg or _cfg()
    path = cfg.resolved_history_db_path()
    data = {
        "path": path,
        "configured": bool(cfg.history_db_path.strip()),
        "default_path": config.Config(shared_db_path=cfg.resolved_shared_db_path())
        .resolved_history_db_path(),
    }
    if look:
        db = history.HistoryDb(path)
        data["exists"] = db.exists()
        data["problem"] = history.path_problem(path)
        data["rows"] = history.row_count(db) if data["exists"] and not data["problem"] else None
    return data


@bp.get("/api/history-db-status")
def history_db_status():
    """看板履歴の置き場所と、届いているか・何行あるか。読むだけなのでパスワードは要らない。"""
    return jsonify(_history_state(look=True))


@bp.post("/api/history-db-path")
def set_history_db_path():
    """看板履歴.sqlite3 の場所を変える。**管理者パスワードが要る。**

    空にすると既定(共有DBと同じフォルダの ``看板履歴.sqlite3``)に戻す。まだ無いファイルは
    作る(フォルダがあれば)。看板マスタ・ほかのツールのファイルは断る。変えたら、この端末は
    その場で送り先を差し替え、看板マスタに残っている以前の記録のうち、まだ無いものを写す。
    **ほかの端末は、それぞれの設定(か配布設定)で同じ場所を指すまで前の場所へ書きます。**
    """
    from kanban.db import history

    body = request.get_json(silent=True) or {}
    path = str(body.get("path", "")).strip()
    ok, problem = _check_admin(body)
    if not ok:
        return problem

    cfg = _cfg()
    target = path or config.Config(shared_db_path=cfg.resolved_shared_db_path()).resolved_history_db_path()
    bad = history.path_problem(target)
    if bad:
        return jsonify(_err("bad_path", bad, "path")), 400
    existed = Path(target).exists()
    before = cfg.resolved_history_db_path()
    cfg.history_db_path = path
    try:
        config.save_config(cfg)
    except OSError as exc:
        return jsonify(_err("not_writable", f"設定を書けませんでした: {exc}")), 500
    log.info("看板履歴の置き場所を変えました: %s", path or "(既定)")

    hook = current_app.config.get("SET_HISTORY_DB")
    note = ""
    if hook is not None:
        try:
            hook(target)
        except Exception as exc:  # noqa: BLE001 - 保存はできている
            log.exception("看板履歴の送り先を差し替えられませんでした")
            note = f" ただし、この端末の送り先を差し替えられませんでした: {exc}"
    else:
        # 起動側が居ない(試験など)。ファイルと表だけ用意する
        from kanban.db import sync

        sync.ensure_history_table(history.HistoryDb(target))
    # **前の場所の記録も連れていく**(まだ無い行だけ。前のファイルは消さない)
    moved = 0
    if not _same_path(before, target) and Path(before).is_file() and Path(target).is_file():
        try:
            moved = history.copy_from_shared(history.HistoryDb(before), history.HistoryDb(target))
        except Exception as exc:  # noqa: BLE001 - 置き場所は変わっている
            log.exception("前の看板履歴を写せませんでした")
            note += f" 前の場所の記録を写せませんでした: {exc}"
    message = (f"看板履歴の置き場所を{'既定に戻しました' if not path else '変えました'}: {target}。"
               + ("" if existed else " ファイルを作りました。")
               + (f" 前の場所({before})の記録を {moved} 件写しました(前のファイルは残してあります)。"
                  if moved else "")
               + " ほかの端末も同じ場所にするときは、配布設定で配るか、各端末で同じ場所を指定してください。"
               + note)
    return jsonify({"ok": True, "message": message, **_history_state(cfg, look=True)})


# ------------------------------------------------------------------
# CSV の書き出し先
# ------------------------------------------------------------------
@bp.post("/api/csv-dir")
def set_csv_dir():
    """CSV を書き出すフォルダを変える。**管理者パスワードが要る。** 空なら既定(この端末のローカル領域)。"""
    body = request.get_json(silent=True) or {}
    path = str(body.get("path", "")).strip()
    ok, problem = _check_admin(body)
    if not ok:
        return problem
    if path:
        folder = Path(path)
        try:
            if not folder.is_dir():
                return jsonify(_err("bad_path", f"フォルダが見つかりません: {path}", "path")), 400
            probe = folder / f".kanban_write_test_{os.getpid()}"
            probe.write_bytes(b"")
            probe.unlink()
        except OSError as exc:
            return jsonify(_err("bad_path", f"そのフォルダに書けません: {path}({exc})", "path")), 400
    cfg = _cfg()
    cfg.csv_dir = path
    try:
        config.save_config(cfg)
    except OSError as exc:
        return jsonify(_err("not_writable", f"設定を書けませんでした: {exc}")), 500
    log.info("CSV の書き出し先を変えました: %s", path or "(既定)")
    where = cfg.resolved_csv_dir()
    return jsonify({"ok": True, "path": where, "configured": bool(path),
                    "message": f"CSV の書き出し先を{'既定に戻しました' if not path else '変えました'}: {where}"})


# ------------------------------------------------------------------
# ファイルの置き場所(共有するもの / この端末だけのもの)
# ------------------------------------------------------------------
def _file_places(cfg: config.Config, store) -> dict:
    """設定画面「この端末の場所」に出す一覧。**共有するファイルと、この端末だけのファイルを分けて言う。**

    共有するものは全端末が同じファイルを読み書きする(置き場所は設定で決める。配布設定で揃える)。
    この端末だけのものはローカル(%APPDATA% / %LOCALAPPDATA%)にあり、**アプリを入れ替えても
    残って、この PC で次に開いたときに引き継がれる**(ほかの PC へは移らない)。
    """
    from kanban import distribution

    def item(label: str, path, what: str, *, folder: bool = False) -> dict:
        text = str(path)
        try:
            exists = Path(text).is_dir() if folder else Path(text).is_file()
        except OSError:
            exists = False
        return {"label": label, "path": text, "what": what, "exists": exists}

    local = app_config.local_root()
    return {
        "shared": [
            item("看板マスタ(共有DB)", cfg.resolved_shared_db_path(),
                 "看板・発注/発送の状態・看板コメント・開いている端末(Form状態管理)"),
            item("看板履歴", cfg.resolved_history_db_path(),
                 "看板を出した・発送した・届いた記録(看板集計はここを読む)"),
            item("梱包資材マスタ", cfg.resolved_access_db_path(),
                 "アクセス権限(誰がどのモードを使えるか)。python-web-tools と共有"),
        ],
        "local": [
            item("設定ファイル", cfg.source_path or config.settings_path(),
                 "接続先・看板履歴/アクセス権限/CSV の置き場所・取り込み/書き戻し間隔・自動印刷・"
                 "管理者パスワード(ハッシュ)・担当ライン"),
            item("手元の SQLite", store.path,
                 "看板の写し・まだ送れていない操作・この端末のモード・送る前の看板履歴とコメント・"
                 "アクセス権限の控え(届かないときに使う)"),
            item("CSV の書き出し先", cfg.resolved_csv_dir(), "看板集計・看板履歴の CSV", folder=True),
            item("ログ・記録", cfg.resolved_log_dir(),
                 "DebugLog(動作のログ)と 記録(エラー・断った操作・直前の操作。設定の「記録」タブで見る)。"
                 "共有フォルダを指定すれば全端末の分を 1 か所に集められる", folder=True),
            item("控え", local / "backup", "中身を入れ替える前の共有DBの控え", folder=True),
            item("共有DBの写し", local / "cache", "読むときに手元へ写したもの(消してもまた写す)", folder=True),
            item("起動の印", local / "runtime", "いま動いているモードとポート(閉じると消える)", folder=True),
        ],
        "app": [
            item("アプリ設定", app_config.CONFIG_PATH, "アプリID・版・ポート"),
            item("配布設定", distribution.directory(), "配った先が起動時に読む設定", folder=True),
        ],
    }


# ------------------------------------------------------------------
# 担当ライン
# ------------------------------------------------------------------
@bp.post("/api/line")
def set_line():
    """現場モードの担当ラインを変える。**管理者パスワードが要る。**

    tkinter 版では通常操作(画面の「ライン変更」ボタン)でしたが、Web 版では
    保護します。担当ラインを変えると**取り込むラインと書き戻すラインが
    入れ替わります** ── 現場の端末が別ラインを指したまま操作されると、
    そのラインの発注が本来の担当者の知らないところで動きます。

    設定画面で守らないのは「マスタを読むこと」だけ、という方針
    (:mod:`app.routes.settings` のモジュール docstring)に合わせています。
    """
    if current_mode() != config.MODE_SITE:
        return jsonify(_err("wrong_mode", "担当ラインは現場モードでのみ変更できます")), 403

    body = request.get_json(silent=True) or {}
    line = str(body.get("line", "")).strip()
    if not config.is_supported_line(line):
        return jsonify(_err("bad_line", "そのラインは登録されていません", "line")), 400

    # **書く前に通す関門。** ここより下で書いてしまうと、断ったのに
    # 一部だけ変わった状態が残る
    ok, problem = _check_admin(body)
    if not ok:
        return problem

    imported = _switch_line(line, "管理者パスワードで")
    # **表でラインが決まっている端末は、次に起動すると表のラインに戻る。** そう言っておく
    note = ""
    grant = current_app.config.get("ACCESS_GRANT")
    table_line = getattr(grant, "line", "") if grant is not None else ""
    if table_line and table_line != line:
        note = (f"次に起動すると、アクセス権限の表のライン「{config.display_name(table_line)}」に戻ります。"
                "変えたままにするには、マスタ管理でアクセス権限を書き換えてください。")
    return jsonify({"ok": True, "line": line, "label": config.display_name(line),
                    "imported": imported, "note": note})


def _switch_line(line: str, how: str) -> bool:
    """担当ラインを切り替える(設定に保存・名乗り・その場で取り込み)。取り込めたら True。

    画面で選んだとき(管理者パスワード)と、アクセス権限の表から当てたときの両方が通る。
    """
    cfg = _cfg()
    cfg.line = line
    config.save_config(cfg)
    current_app.config["LINE"] = line

    # **名乗る行も一緒に移す。** ここを忘れると、心拍は前のラインを名乗り
    # 続ける ── 前のラインが共有DBで「開」のまま残って他端末に幽霊が見え、
    # 新しいラインは一度も名乗らない。しかも自分の画面の「開いている端末」
    # から自分を外せなくなり、**自分自身が他人として並ぶ**
    # (ラインを切り替えながら試すと必ず踏む)
    from kanban import presence

    presence.set_line(presence.registered_line(current_mode(), line))
    from kanban import trace

    trace.set_context(line=line)

    log.info("担当ラインを変更しました(%s): %s", how, line)

    # **変えた先のラインをその場で取り込む。** 取り込まないと、看板画面は
    # 次の定期取り込みまで(0 秒設定なら開き直すまで)空のままになる
    imported = False
    hook = current_app.config.get("MANUAL_REFRESH")
    if hook is not None:
        try:
            hook()
            imported = True
        except Exception:  # noqa: BLE001 - ラインを変えたことは変わらない
            log.exception("担当ラインを変えたあとの取り込みに失敗")
    return imported


def _apply_line_from_access(grant) -> str:
    """アクセス権限の表のラインを担当ラインに当てる(表のラインが前に当てたものから変わったときだけ)。

    当てたら、その一言を返す(画面に出す)。現場モードの端末だけ(倉庫・倉庫参照は担当ラインを使わない)。
    """
    from kanban import access_control

    store = get_store()
    code = access_control.line_to_apply(grant, store)
    if not code:
        return ""
    if current_mode() != config.MODE_SITE:
        return ""
    if current_app.config.get("LINE") != code:
        _switch_line(code, "アクセス権限の表から")
    access_control.mark_line_applied(store, code)
    return f"アクセス権限の表から、担当ラインを「{config.display_name(code)}」にしました。"


# ------------------------------------------------------------------
# 接続先
# ------------------------------------------------------------------
@bp.post("/api/shared-db-path")
def set_shared_db_path():
    """共有 SQLite(正式なデータの置き場所)を変える。

    **ここは本番データの居場所そのもの。** 誤って別のファイルを指すと、
    取り込みが空になったり、書き戻し先を間違えたりする。管理者パスワードで守る。
    """
    body = request.get_json(silent=True) or {}
    path = str(body.get("path", "")).strip()
    if not path:
        return jsonify(_err("bad_path", "パスが空です", "path")), 400

    ok, problem = _check_admin(body)
    if not ok:
        return problem

    cfg = _cfg()
    # **同じ場所なら変えない。ただし、決めていない(既定の場所を使っている)
    # ときは保存する。** 以前は既定の場所と同じだと「変更はありません」と
    # 返して何も保存しなかった ── 押した人は決めたつもりでいた。
    # 綴りの揺れ(大文字小文字・/ と \・末尾の区切り)は同じ場所とみなす
    if cfg.shared_db_path.strip() and _same_path(cfg.shared_db_path, path):
        problem = _path_problem(path)
        return jsonify({
            "ok": True, "changed": False, "path": cfg.shared_db_path,
            "problem": problem,
            "message": "いまの接続先と同じ場所です。変わっていません。"
                       + ("" if not problem else f"\nただし、いまは届きません: {problem}"),
        })

    # **届かないパスは保存しない。**
    #
    # 保存してしまうと、この端末は次の起動からも届かない場所を指し続けます
    # ── 画面は「取り込めていません」と言うだけなので、**打ち間違いなのか
    # 共有が落ちているのか**が分かりません。打った直後なら、どちらかは
    # はっきりしています。
    bad = _path_problem(path)
    if bad:
        log.info("接続先を変えませんでした: %s (%s)", path, bad)
        return jsonify(_err("unreachable_path", bad, "path")), 400

    cfg.shared_db_path = path
    config.save_config(cfg)
    log.info("接続先を変更しました: %s", path)

    # **その場で繋ぎ直して取り込む。開き直しは要らない。**
    #
    # 以前は「開き直してください」と案内していたが、その案内どおりに
    # ブラウザを閉じても ``pythonw.exe`` は残り、次の起動が「すでに起動
    # しています」で止まった ── 利用者は開き直せなかった。
    reconnect = current_app.config.get("RECONNECT")
    if reconnect is None:
        # 起動側が注入していない(テスト等)。保存だけして、そう伝える
        return jsonify(
            {"ok": True, "changed": True, "path": path,
             "reconnected": False, "message": "接続先を保存しました。"}
        )

    try:
        result = reconnect(path)
    except Exception as exc:  # noqa: BLE001 - 取り込みの失敗で画面を壊さない
        log.exception("接続先の差し替えに失敗")
        return jsonify(
            {"ok": True, "changed": True, "path": path, "reconnected": False,
             "message": f"接続先は保存しましたが、取り込みに失敗しました: {exc}"}
        )

    failed = list(getattr(result, "failed_lines", []) or [])
    ok_lines = list(getattr(result, "ok_lines", []) or [])
    message = f"接続先を変えて取り込みました({len(ok_lines)} ライン)。"
    if failed:
        message += f" 取り込めなかったライン: {len(failed)} 件。"
    return jsonify(
        {"ok": True, "changed": True, "path": path, "reconnected": True,
         "imported_lines": len(ok_lines), "failed_lines": len(failed),
         "message": message}
    )


def _same_path(a: str, b: str) -> bool:
    """同じ場所か(Windows の綴りの揺れを吸収する)。"""
    def norm(p: str) -> str:
        p = p.strip().replace("/", "\\").rstrip("\\")
        return p.casefold()
    return norm(a) == norm(b)


@bp.get("/api/shared-db-status")
def shared_db_status():
    """いまの接続先と、届いているか。**読むだけなのでパスワードは要らない。**

    設定画面が開いたあとで取りに来る(共有フォルダの確認で画面の表示を
    待たせない)。「変更」が受け付けられたかを、画面に残して見せるための値。
    """
    cfg = _cfg()
    path = cfg.resolved_shared_db_path()
    return jsonify({
        "path": path,
        "configured": bool(cfg.shared_db_path.strip()),
        "problem": _path_problem(path),
    })


@bp.post("/api/sync/discard-failed")
def discard_failed():
    """送れなかった操作(1 回以上失敗した未反映)を捨てて、共有DBの今の状態に戻す。

    **管理者パスワードが要る**(この端末で押した操作を捨てる)。捨てたあと取り込み
    直すので、共有DBにある看板は共有DBの状態に、共有DBに無い看板は手元からも消える。
    """
    body = request.get_json(silent=True) or {}
    ok, problem = _check_admin(body)
    if not ok:
        return problem
    store = get_store()
    rows = store.discard_failed()
    if not rows:
        return jsonify({"ok": True, "discarded": [], "message": "捨てる操作はありませんでした。"})
    note = ""
    hook = current_app.config.get("MANUAL_REFRESH")
    if hook is not None:
        try:
            hook()
            note = " 共有DBから取り込み直しました(共有DBにある看板は共有DBの状態に、無い看板は手元からも消えました)。"
        except Exception as exc:  # noqa: BLE001 - 捨てたことは変わらない
            log.exception("捨てたあとの取り込みに失敗")
            note = f" ただし取り込み直せませんでした: {exc}"
    log.info("送れない操作を捨てました: %d 件", len(rows))
    return jsonify({
        "ok": True,
        "discarded": [{"line": r["line"], "mgmt_no": r["mgmt_no"], "error": r["sync_error"]} for r in rows],
        "message": f"送れない操作を {len(rows)} 件 捨てました。" + note,
    })


@bp.post("/api/sync/retry-failed")
def retry_failed():
    """あきらめた操作(要確認)を、もう一度送る。共有DBの側を直したあとに使う。"""
    body = request.get_json(silent=True) or {}
    ok, problem = _check_admin(body)
    if not ok:
        return problem
    n = get_store().retry_failed()
    hook = current_app.config.get("ON_CHANGED")
    if n and hook:
        try:
            hook("")
        except Exception:  # noqa: BLE001 - 次の周期で送られる
            log.exception("もう一度送る依頼に失敗")
    return jsonify({"ok": True, "retried": n,
                    "message": f"{n} 件をもう一度送ります。数秒で結果が出ます。" if n else "送り直す操作はありません。"})


@bp.post("/api/reimport")
def reimport():
    """いまの接続先から取り込み直す。**管理者パスワードが要る。**

    「開き直してください」と言われても、窓の無いアプリでは開き直せない
    ことがある ── だから**取り込み直す手段を画面に置く**。接続先を変えた
    直後だけでなく、共有フォルダが一時的に落ちていて取り込めなかったとき
    にも使う。
    """
    body = request.get_json(silent=True) or {}
    ok, problem = _check_admin(body)
    if not ok:
        return problem

    hook = current_app.config.get("MANUAL_REFRESH")
    if hook is None:
        return jsonify(_err("no_importer", "この起動では取り込みを実行できません")), 400

    try:
        result = hook()
    except Exception as exc:  # noqa: BLE001
        log.exception("取り込みに失敗")
        return jsonify(_err("import_failed", f"取り込みに失敗しました: {exc}")), 500

    failed = list(getattr(result, "failed_lines", []) or [])
    ok_lines = list(getattr(result, "ok_lines", []) or [])
    message = f"{len(ok_lines)} ラインを取り込みました。"
    if failed:
        message += f" 取り込めなかったライン: {len(failed)} 件。"
    log.info("画面から取り込み: 成功=%d 失敗=%d", len(ok_lines), len(failed))
    return jsonify(
        {"ok": True, "imported_lines": len(ok_lines),
         "failed_lines": len(failed), "message": message}
    )


# ------------------------------------------------------------------
# フォルダ参照(サーバ側)
# ------------------------------------------------------------------
@bp.post("/api/fs/list")
def fs_list():
    """サーバから見えるフォルダを一覧する。**管理者パスワードが要る。**

    ブラウザのファイル選択ダイアログは**クライアント側**のパスしか返さない
    ので、それでは接続先を選べない(tkinter 版は同じプロセスに画面があった
    ので ``askopenfilename`` がそのまま使えていた)。詳しくは
    :mod:`kanban.presenters.fs_browse`。

    【なぜ守るのか / なぜ POST なのか】
    これはこの PC のフォルダ構成を返す口です。中身は返さない・名前だけ・
    件数上限あり、と絞ってはありますが、**マスタを読むこと以外は守る**という
    方針に含めます。パスワードを本文で受けるため ``GET`` ではなく ``POST``
    にしてあります(URL に載せると履歴とログに残る)。

    毎回パスワードを訊くと、フォルダを 1 つ辿るたびに入力させることになる
    ので、**画面は「参照...」を開いているあいだだけ手元に持って毎回送ります**
    (``views/settings.js``)。サーバ側には解錠状態を持ちません ── 開き
    っぱなしのタブが解錠のまま残るのを避けるためです。
    """
    from kanban.presenters import fs_browse

    body = request.get_json(silent=True) or {}
    ok, problem = _check_admin(body)
    if not ok:
        return problem

    view = fs_browse.browse(str(body.get("path", "")))
    return jsonify(fs_browse.to_dict(view))


# ------------------------------------------------------------------
# マスタ管理
# ------------------------------------------------------------------
def _shared_db():
    """設定されている共有 DB と、**明示的に設定されているか**。

    設定が空でも ``config`` は既定の置き場所へ落とすので、届かない理由が
    「設定していない」のか「設定したが届かない」のかを言い分けられるよう、
    その旨も一緒に返す(直し方が違う)。
    """
    from kanban.db.shared import SharedDb

    cfg = _cfg()
    return SharedDb(cfg.resolved_shared_db_path()), bool(cfg.shared_db_path.strip())


#: マスタ管理で扱うファイル。``""`` = 共有DB(看板マスタ)/ ``access`` = 梱包資材マスタ
DB_ACCESS = "access"


def _master_db(which: str = ""):
    """マスタ管理が読み書きするファイル。**梱包資材マスタはアクセス権限の表だけを見せる。**"""
    if str(which or "") != DB_ACCESS:
        return _shared_db()
    from kanban import access_control

    cfg = _cfg()
    return access_control.open_db(cfg), bool(cfg.access_db_path.strip() or cfg.shared_db_path.strip())


def _access_entry() -> dict:
    """マスタ管理の一覧に足す「アクセス権限(梱包資材マスタ)」。

    **表が無ければここで作る**(梱包資材マスタにアクセス権限がまだ無い現場がある。
    無いままだと、最初の 1 行をどこからも足せない)。作れないときは理由を出す。
    """
    from kanban import access_control

    db = access_control.open_db(_cfg())
    entry = {"db": DB_ACCESS, "name": access_control.TABLE,
             "label": f"{access_control.TABLE}(梱包資材マスタ)", "path": db.path,
             "available": False, "why": "", "created": ""}
    if not db.exists():
        entry["why"] = (f"梱包資材マスタが見つかりません: {db.path}(「接続先」タブの"
                        "「アクセス権限の置き場所」で指定できます)")
        return entry
    ok, message = access_control.ensure_table(db)
    entry["available"] = ok
    entry["why" if not ok else "created"] = message
    return entry


@bp.get("/api/master")
def master_frame():
    """テーブルの一覧。**中身は読まない**(共有への往復を増やさない)。

    共有DBの表のあとに、梱包資材マスタの「アクセス権限」を 1 つ足す(``extra``)。
    """
    from kanban.presenters import master

    shared, configured = _shared_db()
    data = master.to_dict(master.frame(shared, configured=configured))
    try:
        data["extra"] = [_access_entry()]
    except Exception as exc:  # noqa: BLE001 - 共有DBの表は出す
        log.exception("アクセス権限の表を確かめられませんでした")
        data["extra"] = [{"db": DB_ACCESS, "name": "アクセス権限", "label": "アクセス権限(梱包資材マスタ)",
                          "available": False, "why": f"確かめられませんでした: {exc}", "path": ""}]
    return jsonify(data)


@bp.get("/api/master/table")
def master_table():
    """表を 1 つ開く。``?table=&page=&db=&sort=&dir=``(``sort`` の列で表全体を並べ替える)"""
    from kanban.presenters import master

    try:
        page = int(request.args.get("page", "0"))
    except ValueError:
        page = 0
    shared, _configured = _master_db(request.args.get("db", ""))
    view = master.open_table(shared, request.args.get("table", ""), page,
                             sort=request.args.get("sort", ""), sort_dir=request.args.get("dir", "asc"))
    return jsonify(master.to_dict(view))


@bp.post("/api/master/update")
def master_update():
    """1 マスだけ直す。**管理者パスワードが要る。**

    共有 DB は全端末が見る正式なデータなので、モード変更・接続先変更と
    同じ関門を通す。
    """
    from kanban.presenters import master

    body = request.get_json(silent=True) or {}
    table = str(body.get("table", ""))
    column = str(body.get("column", ""))
    if not table or not column:
        return jsonify(_err("bad_request", "対象が指定されていません")), 400

    # **書く前に通す関門。** ここより下で書いてしまうと、断ったのに
    # 一部だけ変わった状態が残る
    ok, problem = _check_admin(body)
    if not ok:
        return problem

    shared, configured = _master_db(body.get("db", ""))
    result = master.update_cell(
        shared, table, body.get("row_key"), column, body.get("value"),
        configured=configured, expect_key=body.get("key", master.NO_KEY),
    )
    return _master_result(result, table, "update", db=body.get("db", ""))


@bp.post("/api/master/insert")
def master_insert():
    """1 行足す。**管理者パスワードが要る。**

    **看板を 1 枚増やすのはこの経路です。** 直すだけでは看板を増やせず、
    増やせないと結局 Access を開くことになります(その Access をやめた)。
    """
    from kanban.presenters import master

    body = request.get_json(silent=True) or {}
    table = str(body.get("table", ""))
    values = body.get("values")
    if not table or not isinstance(values, dict):
        return jsonify(_err("bad_request", "対象が指定されていません")), 400

    ok, problem = _check_admin(body)
    if not ok:
        return problem

    shared, configured = _master_db(body.get("db", ""))
    return _master_result(
        master.insert_row(shared, table, values, configured=configured), table, "insert",
        db=body.get("db", ""),
    )


@bp.post("/api/master/delete")
def master_delete():
    """1 行消す。**管理者パスワードが要る。**

    足せるなら消せないと困ります ── 番号を打ち間違えて足した行は、
    キー列を直せない以上、消すしか取り除く手がありません。
    """
    from kanban.presenters import master

    body = request.get_json(silent=True) or {}
    table = str(body.get("table", ""))
    if not table or body.get("row_key") is None:
        return jsonify(_err("bad_request", "対象が指定されていません")), 400

    ok, problem = _check_admin(body)
    if not ok:
        return problem

    shared, configured = _master_db(body.get("db", ""))
    result = master.delete_row(shared, table, body.get("row_key"), configured=configured,
                               expect_key=body.get("key", master.NO_KEY))
    if body.get("db", "") == DB_ACCESS:
        return _master_result(result, table, "delete", db=DB_ACCESS)
    if result.ok and result.key and table.startswith(config.KANBAN_TABLE_PREFIX):
        # その看板のやり取りを片付ける(同じ番号で足し直したときに出てこないように)
        line = table[len(config.KANBAN_TABLE_PREFIX):]
        try:
            get_store().close_comments(line, result.key)
            hook = current_app.config.get("ON_CHANGED")
            if hook:
                hook(line)
        except Exception:  # noqa: BLE001 - 消せたことは変わらない
            log.exception("消した看板のコメントを片付けられませんでした")
    return _master_result(result, table, "delete")


#: 書けたあと手元へ取り込めたときに足す一言(操作ごと)
_BOARD_NOTE = {
    "insert": " 看板画面にもすぐ出ます。",
    "update": " 看板画面にもすぐ反映されます。",
    "delete": " 看板画面からも消えます。",
}


def _master_result(result, table: str = "", action: str = "update", *, db: str = ""):
    """マスタを書いた結果を応答にする。**書けたら手元も追いつかせる。**

    ここを飛ばすと、共有DBは直っているのに画面の動きが変わりません ──
    「直したのに効かない」が一番たちが悪い(看板を足したのに盤に出て
    こなければ、足せていないのと区別が付かない)。

    取り込みに失敗しても**書けたことは書けた**ので、成功として返して
    「取り込みは別途」と付け足します。ここで失敗を返すと、実際には
    入っているのに「入らなかった」と読ませてしまいます。

    アクセス権限(梱包資材マスタ)は看板の取り込みと関係ないので取り込まず、
    **この端末の権限を読み直して**どう変わったかを添える(:func:`_access_result`)。
    """
    if not result.ok:
        # 共有DBに届かない(開けない)のは入力の誤りではない。503 で分ける
        status = {"not_found": 409, "unreachable": 503, "in_use": 409}.get(result.reason, 400)
        return jsonify(_err(result.reason, result.message)), status

    if db == DB_ACCESS:
        return _access_result(result)

    message = result.message
    hook = current_app.config.get("MANUAL_REFRESH")
    if hook is not None:
        try:
            hook()
        except Exception as exc:  # noqa: BLE001 - 書けたことは変わらない
            log.exception("マスタ更新後の取り込みに失敗")
            message += f" ただし手元へ取り込めませんでした: {exc}"
        else:
            message += _BOARD_NOTE.get(action, "")
    # **この端末の看板画面に出ないラインなら、そう言う。** 看板画面は担当
    # ラインだけを出すので、別のラインの表を直しても「出ない」ように見える
    if current_mode() == config.MODE_SITE and table:
        own = current_app.config.get("LINE") or ""
        if table != config.table_name_for(own):
            for note in _BOARD_NOTE.values():
                message = message.replace(note, "")
            message += (
                f" ※この端末の担当ラインは「{config.display_name(own) if own else '未設定'}」"
                f"なので、{table} の看板はこの端末の看板画面には出ません"
                "(その担当ラインの端末で反映されます)。"
            )
    return jsonify({"ok": True, "message": message, **_written_to()})


def _access_result(result):
    """アクセス権限を書いたあと。**この端末の権限がどうなったか**まで言う。

    権限は起動するときとモードを切り替えるときに読むので、書いた直後から
    モードの切り替えには効く。いま動いているモードは開き直すまでそのまま。
    """
    from kanban import access_control

    cfg = _cfg()
    grant = access_control.grant_for(cfg, get_store())
    current_app.config["ACCESS_GRANT"] = grant
    labels = "・".join(config.mode_display_name(m) for m in grant.allowed_modes()
                      if m in access_control.MODE_PERMISSION)
    message = (f"{result.message} この端末({grant.identity.label()})で使えるモード: "
               f"{labels or 'なし'}(倉庫参照はいつでも使えます)。モードの切り替えには、すぐ効きます。")
    running = current_mode()
    if not grant.allows(running):
        message += (f" ※いま動いている{config.mode_display_name(running)}の権限はなくなりました。"
                    f"次に開いたときは{config.mode_display_name(grant.startup_mode(running))}で開きます。")
    return jsonify({"ok": True, "message": message, "access": _access_state(grant),
                    **_written_to(cfg.resolved_access_db_path())})


def _written_to(path: str = "") -> dict:
    """**どのファイルに書いたか**(共有DBの場所と、書いたあとの更新日時)。

    画面の表は手元の写しから出るので、「表示は変わったがファイルは?」を確かめる
    手がかりとして、書いた先を名指しする。
    """
    from datetime import datetime

    path = path or _cfg().resolved_shared_db_path()
    try:
        at = datetime.fromtimestamp(Path(path).stat().st_mtime).strftime("%Y/%m/%d %H:%M:%S")
    except OSError:
        at = ""
    return {"written": path, "written_at": at}


def _err(code: str, message: str, field: str = "") -> dict:
    body = {"code": code, "message": message}
    if field:
        body["field"] = field
    return {"error": body}
