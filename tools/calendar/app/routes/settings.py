"""設定画面とその API

    GET  /settings                 画面の器
    GET  /api/settings             設定ぜんぶ
    GET  /api/sync                 同期の状態だけ(帯が短い間隔で読む)
    POST /api/settings/line        この端末のライン
    POST /api/settings/paths       参照パス(取り込み元のフォルダ)
    POST /api/settings/auto-sync   自動同期の入切と間隔
    POST /api/settings/log-dir     ログフォルダ(ログの書き先)
    POST /api/settings/theme       画面の見た目(自動 / ライト / ダーク)
    POST /api/settings/distribution/export   この端末の設定を配布設定に書き出す
    POST /api/settings/distribution/reapply  置いてある配布設定を読み込み直す
    POST /api/settings/distribution/remove   配布設定を消す
    POST /api/settings/browse      サーバ側のフォルダ参照
    POST /api/sync/now             今すぐ同期
    POST /api/sync/skipped/ack     送らなかった登録の知らせを確かめた
    POST /api/import               参照パスから取り込み
    POST /api/import/csv           班員名簿を CSV から取り込み(逃げ道)

マスタ管理 (``/api/master/*``) は ``routes/master.py`` にある。
ログの一覧と後追い (``/api/logs*``) は ``routes/diagnostics.py`` にある。

tkinter 版では上部のボタンに散らばっていた
[ライン設定] [同期設定] [取り込み] [今すぐ同期] をここに集めた。
並びは**決める順**: ライン → 参照パス → 取り込み → マスタ管理 → いまの状態。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, render_template, request

from calendar_app import (
    access_control,
    admin_password,
    config,
    distribution,
    logging_utils,
    settings as user_settings,
    sources,
    sync_service,
    terminals,
)
from calendar_app.dbkit.source_db import SourceError
from calendar_app.importer import (
    PendingChangesError,
    import_members_csv,
    import_source,
)
from calendar_app.logging_utils import get_logger
from calendar_app.presenters import settings as presenter

from .. import shell
from . import get_db

log = get_logger("app.routes.settings")

bp = Blueprint("settings", __name__)

#: CSV の拡張子 (班員名簿の逃げ道)
CSV_SUFFIXES = (".csv",)


# ---------------------------------------------------------------------------
# 画面と読み取り
# ---------------------------------------------------------------------------
@bp.get("/settings")
def page():
    return render_template("settings.html", **shell.shell_context("settings"))


@bp.get("/api/settings")
def read():
    return jsonify(presenter.to_dict(presenter.view(get_db())))


@bp.get("/api/sync")
def sync_status():
    """同期の状態だけ。**帯が数秒ごとに読むので軽くしてある。**"""
    return jsonify(presenter.sync_dict())


# ---------------------------------------------------------------------------
# 設定の変更
# ---------------------------------------------------------------------------
@bp.post("/api/settings/line")
def set_line():
    """この端末のライン (tkinter 版 ``on_line_setting``)。

    休み・コメントの**表示絞り込み**に効く。変えると見えるものが変わるので、
    更新後の設定一式を返して画面を描き直させる。

    **変えるには管理者パスワードが要る。** VBA 版は誰でもどこでも
    変えられたが、押し間違えた端末は「見えるはずのものが消えた」ことに
    自分では気づけない(``calendar_app/admin_password.py``)。
    """
    body = request.get_json(silent=True) or {}
    line = str(body.get("line", "")).strip()
    # **一覧に無いものは選べない。**
    if line not in config.ALL_LINE_NAMES:
        return _refuse("not_listed", "そのラインはありません。")

    # **アクセス権限でこの端末のラインが決まっていれば、表に無いラインは
    # 選べない**(パスワードを知っていても)。1つなら固定
    # (``calendar_app/access_control.py``)
    grant = access_control.resolve(get_db())
    if grant.managed and not grant.allows(line):
        return _refuse(
            "not_granted",
            f"この端末({grant.label()})はアクセス権限で"
            + (f"「{grant.lines[0]}」に固定されています。" if grant.fixed else
               f"「{'・'.join(grant.lines)}」に限られています。")
            + f"「{line}」にするには、マスタ管理の「アクセス権限」を"
            "直してください。")

    # **切り替えにはいつでも管理者パスワード**(表で決まっていても)。
    # 同じ値で保存し直すだけなら聞かない ── 変わらないものにパスワードを
    # 聞くと「押しても何も起きない」になる
    blocked = _guard({"line": line != user_settings.get_my_line()}, body)
    if blocked is not None:
        return blocked

    user_settings.save_my_line(line)
    # どのPCがどのラインかを**その場で共有へ載せる**(マスタ確認で見る)。
    # 次の同期まで待つと、直したのに一覧が古いままに見える
    terminals.publish()
    log.info("ライン設定を変更しました: %s", line)
    return jsonify(_ok(f"ラインを「{line}」に設定しました"))


@bp.post("/api/settings/paths")
def set_paths():
    """参照パス ── 取り込み元の**フォルダ**を決める。

    ファイルのフルパスではなくフォルダを持つ。フルパスにすると、上流が
    ファイル名を変えただけで動かなくなり、現場からは「急に読めなくなった」
    としか見えない(``config.default_data_db_dir`` の説明)。

    **存在しないフォルダは受け付けない。** 受け付けてしまうと、設定できた
    つもりのまま送信待ちだけが溜まる。空文字は「未設定に戻す」なので通す。

    保存は2つ別々に受ける ── 片方を直したいだけのときに、もう片方まで
    送って上書きしないため。

    **変えるには管理者パスワードが要る。** 送り先が変わると、その端末の
    入力だけが別の場所へ行き、他のラインには永遠に届かない。
    """
    body = request.get_json(silent=True) or {}
    fields = (
        ("data_db_dir", user_settings.KEY_DATA_DB_DIR, "保存用DB",
         user_settings.data_db_dir_setting),
        ("master_db_dir", user_settings.KEY_MASTER_DB_DIR, "マスタDB",
         user_settings.master_db_dir_setting),
    )

    # **先に全部確かめてから、まとめて書く。** 途中で断ると、断ったのに
    # 片方だけ変わった状態が残る
    pending: list[tuple[str, str]] = []
    changing: dict[str, bool] = {}
    changed: list[str] = []
    for field, key, label, current in fields:
        if field not in body:
            continue
        raw = str(body.get(field, "")).strip()
        if raw:
            try:
                resolved = config.resolve_dir(raw)
            except ValueError:
                return _bad_request(f"{label}のフォルダの指定が正しくありません。")
            if not resolved.is_dir():
                return _refuse("not_found",
                               f"{label}のフォルダが見つかりません: {resolved}")
        pending.append((key, raw))
        changing[field] = raw != current()
        changed.append(label)

    if not changed:
        return _bad_request("変更するフォルダが指定されていません。")

    blocked = _guard(changing, body)
    if blocked is not None:
        return blocked

    for key, raw in pending:
        user_settings.set_value(key, raw)

    service = sync_service.get_service()
    service.reload()

    found = sources.find_data_db()
    if found is None:
        message = (f"{' と '.join(changed)}のフォルダを保存しました。"
                   "ただし、そのフォルダに取り込み元(.sqlite3)が見つかりません。")
    else:
        # 取り込み元が sqlite3 になってから、**書けない端末は無い**
        user_settings.set_auto_sync(True)
        service.start()
        message = (f"{' と '.join(changed)}のフォルダを保存しました。"
                   f"{found.name} を使います。"
                   f"入力のたびに送信し、{user_settings.sync_interval()} 秒ごとに"
                   "取り込み直します。")
    log.info("参照パスを保存しました: %s (保存用DB=%s / 自動同期=%s)",
             changed, found, service.enabled)
    return jsonify(_ok(message))


@bp.post("/api/settings/log-dir")
def set_log_dir():
    """ログフォルダ ── どこへログを書くか。空なら既定(この端末のローカル)。

    **書けるか確かめてから受ける。** 受けてから書けないと分かっても、
    アプリはローカルへ逃げて動き続けるので、設定した人は気づかない。
    確かめるのは、実際に書く PC名 のフォルダを作って1度書いてみること
    (フォルダが見えるだけでは、書く権限があるかは分からない)。

    パスワードは要らない。業務データの行き先は変わらず、変えても
    困るのは「ログがどこにあるか」だけで、それは画面に出ている。
    """
    body = request.get_json(silent=True) or {}
    if "log_dir" not in body:
        return _bad_request("ログフォルダが指定されていません。")
    raw = str(body.get("log_dir", "")).strip()
    if raw:
        try:
            base = config.resolve_dir(raw)
        except ValueError:
            return _bad_request("ログフォルダの指定が正しくありません。")
        if not base.is_dir():
            return _refuse("not_found", f"ログフォルダが見つかりません: {base}")
        target = logging_utils.folder_for(raw)
        try:
            target.mkdir(parents=True, exist_ok=True)
            probe = target / ".書けるか確認"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            return _refuse("not_writable",
                           f"ログフォルダに書けません: {target} ({exc})")

    before = logging_utils.status()["folder"]
    user_settings.set_value(user_settings.KEY_LOG_DIR, raw)
    # 書き先を変える前に、前の書き先へ「どこへ移ったか」を残す
    # (前のログを読んだ人が、続きを探せるように)
    after = logging_utils.folder_for(raw)
    log.info("ログフォルダを変えます: %s → %s", before, after)
    status = logging_utils.reconfigure()
    log.info("ログフォルダを変えました(前の書き先: %s)", before)
    if status["fallback_reason"]:
        return jsonify(_ok("ログフォルダを保存しましたが、書けないのでこの端末の"
                           f"既定の場所へ書いています: {status['fallback_reason']}"))
    where = status["folder"]
    return jsonify(_ok(f"ログをここへ書きます: {where}" if raw else
                       f"ログフォルダを既定に戻しました: {where}"))


@bp.post("/api/settings/theme")
def set_theme():
    """画面の見た目 ── 自動(OS に合わせる)/ ライト / ダーク。

    **この端末だけ**の好みなので、パスワードは要らない(業務の動きは変わらない)。
    帯のボタンからも呼ぶので、返すのは見た目だけ(設定一式は返さない)。
    """
    body = request.get_json(silent=True) or {}
    theme = str(body.get("theme", "")).strip()
    if theme not in user_settings.THEME_LABELS:
        return _refuse("not_listed", "その見た目は選べません。")
    user_settings.set_theme(theme)
    label = user_settings.THEME_LABELS[theme]
    log.info("画面の見た目を変えました: %s", label)
    return jsonify({"theme": theme, "label": label,
                    "message": f"画面の見た目を「{label}」にしました。"})


@bp.post("/api/settings/admin-password")
def change_admin_password():
    """管理者パスワードを変える。**値は保存も応答もしない**(撹拌して持つ)。

    設定を誤って押されないためのUIガードで、アクセス制御ではない
    (``calendar_app/admin_password.py``)。
    """
    body = request.get_json(silent=True) or {}
    if body.get("reset"):
        result = admin_password.reset(str(body.get("current", "")))
    else:
        result = admin_password.change(str(body.get("current", "")),
                                       str(body.get("new", "")),
                                       str(body.get("confirm", "")))
    if not result.ok:
        # 入力の形の誤り。**サーバの状態は動いていない**
        return _refuse(result.reason, result.message, status=400)
    return jsonify(_ok(result.message))


# ---------------------------------------------------------------------------
# 配布設定(``calendar_app/distribution.py``)。どれも管理者パスワードが要る
# ---------------------------------------------------------------------------
def _distribution_reply(result):
    if not result.ok:
        status = 403 if result.reason in (distribution.REFUSE_NEED_PASSWORD,
                                          distribution.REFUSE_WRONG_PASSWORD) else 400
        if result.reason == distribution.REFUSE_FAILED:
            status = 500
        return _refuse(result.reason, result.message, status=status)
    return jsonify(_ok(result.message))


@bp.post("/api/settings/distribution/export")
def export_distribution():
    """この端末のいまの設定を、配布設定として書き出す。"""
    body = request.get_json(silent=True) or {}
    items = body.get("items")
    if not isinstance(items, list) or not all(isinstance(x, str) for x in items):
        return _refuse("bad_input", "入れる項目の形が違います。", status=400)
    return _distribution_reply(distribution.export(
        str(body.get("password", "")), items))


@bp.post("/api/settings/distribution/remove")
def remove_distribution():
    body = request.get_json(silent=True) or {}
    return _distribution_reply(distribution.remove(str(body.get("password", ""))))


@bp.post("/api/settings/distribution/reapply")
def reapply_distribution():
    """置いてある配布設定を読み込み直す(すでにある設定も上書き)。"""
    body = request.get_json(silent=True) or {}
    result = distribution.reapply(str(body.get("password", "")))
    if result.ok:
        # 置き場所が変わったかもしれない。読み直さないと、画面は
        # 「読み込んだ」と言うのに同期は古い場所を見続ける
        service = sync_service.get_service()
        service.reload()
        if sources.find_data_db() is not None:
            service.start()
        terminals.publish()
    return _distribution_reply(result)


@bp.post("/api/settings/auto-sync")
def set_auto_sync():
    """自動同期の入切と間隔。"""
    body = request.get_json(silent=True) or {}
    if "enabled" in body:
        user_settings.set_auto_sync(bool(body["enabled"]))
    if "interval" in body:
        try:
            interval = int(body["interval"])
        except (TypeError, ValueError):
            return _bad_request("間隔は秒数で指定してください。")
        # 短すぎると共有フォルダを叩き続けることになる。
        # 下限は ``settings.sync_interval()`` が持っているので、ここは形だけ見る
        if interval < 5 or interval > 3600:
            return _refuse("out_of_range", "間隔は 5〜3600 秒で指定してください。")
        user_settings.set_value(user_settings.KEY_SYNC_INTERVAL, interval)

    sync_service.get_service().reload()
    log.info("自動同期の設定を変更しました: 有効=%s 間隔=%s",
             user_settings.auto_sync_enabled(), user_settings.sync_interval())
    return jsonify(_ok("同期の設定を変更しました"))


@bp.post("/api/settings/browse")
def browse():
    """サーバ側から見えるフォルダを一覧する。

    返すのは**名前だけ**で、ファイルの中身は返さない。
    """
    path = str((request.get_json(silent=True) or {}).get("path", ""))
    return jsonify(presenter.browse(path))


# ---------------------------------------------------------------------------
# 同期
# ---------------------------------------------------------------------------
@bp.post("/api/sync/now")
def sync_now():
    """今すぐ同期する (tkinter 版 ``on_sync_now``)。

    **終わってから返す。** 押した人は結果を見たいので、ここだけは待つ
    (定期実行のほうは待たない)。
    """
    service = sync_service.get_service()
    if not service.configured:
        return _refuse("not_configured",
                       "取り込み元が見つかりません。"
                       "設定画面の「参照パス」でフォルダを指定してください。")
    if service.is_busy():
        return _refuse("busy", "いま同期しています。少し待ってからお試しください。",
                       status=409)

    service.sync_now(receive=True)
    status = presenter.sync_dict()
    message = "同期しました" if not status["offline"] else f"送れませんでした: {status['message']}"
    payload = _ok(message)
    payload["sync"] = status
    return jsonify(payload)


@bp.post("/api/sync/skipped/ack")
def acknowledge_skipped():
    """送らずに取りやめた登録を「確かめた」。知らせを消す。"""
    from calendar_app.sync import notices

    count = notices.acknowledge(get_db())
    log.info("送らなかった登録の知らせを確かめました: %s 件", count)
    return jsonify(presenter.sync_dict())


# ---------------------------------------------------------------------------
# 取り込み
# ---------------------------------------------------------------------------
@bp.post("/api/import")
def do_import():
    """**参照パスから**取り込む (tkinter 版 ``on_import``)。

    取り込む先はフォルダの中から探す(``sources``)ので、ここでファイルの
    パスは受け取らない ── 受け取ると、設定した参照パスと違う場所から
    取り込めてしまい、次の同期でどこを見ているのか分からなくなる。

    未反映の変更があるときは**既定では取り込まない** ── 取り込みは
    テーブルを入れ替えるので、まだ取り込み元に届いていない入力が消える。
    ``force=true`` を付けて呼び直すと破棄して取り込む。
    """
    body = request.get_json(silent=True) or {}
    target = str(body.get("target", "all")).strip() or "all"
    force = bool(body.get("force"))
    if target not in ("all", "data", "master"):
        return _bad_request("取り込む対象の指定が正しくありません。")

    paths: list[Path] = []
    missing: list[str] = []
    if target in ("all", "data"):
        found = sources.find_data_db()
        paths.append(found) if found else missing.append("保存用DB(連絡帳)")
    if target in ("all", "master"):
        found = sources.find_master_db()
        paths.append(found) if found else missing.append("マスタDB(班員名簿)")

    if not paths:
        return _refuse(
            "no_source",
            f"{' と '.join(missing)} が見つかりません。\n"
            "設定画面の「参照パス」でフォルダを指定してください。")

    conn = get_db()
    done: list[str] = []
    try:
        for index, path in enumerate(paths):
            # 未反映チェックは最初の1回だけでよい(2つ目からは同じ取り込み)
            result = import_source(conn, str(path), force=force or index > 0)
            done.append(result.describe())
    except PendingChangesError as exc:
        # **破棄してよいかは利用者が決める。** ここで勝手に消さない
        return jsonify({"error": {
            "code": "pending_changes",
            "message": f"{exc}",
            "hint": "未反映の変更を破棄して取り込みますか?",
        }}), 409
    except (SourceError, ValueError, OSError) as exc:
        log.warning("取り込みに失敗しました: %s", exc)
        return _refuse("import_failed", str(exc))

    message = "\n".join(done)
    if missing:
        message += f"\n({' と '.join(missing)} は見つかりませんでした)"
    log.info("参照パスから取り込みました: %s", message.replace("\n", " / "))
    return jsonify(_ok(message))


@bp.post("/api/import/csv")
def import_csv():
    """班員名簿を CSV から取り込む(マスタDB が手元に無いとき用)。

    参照パスを通らない**逃げ道**なので、ファイルのパスを受け取る。
    マスタDB が見られない端末でも、名簿さえあれば休みを登録できる
    ようにするためのもの。
    """
    path = str((request.get_json(silent=True) or {}).get("path", "")).strip()
    if not path:
        return _bad_request("CSV のパスを入力してください。")

    target = Path(path).expanduser()
    if not target.exists():
        return _refuse("not_found", f"ファイルが見つかりません: {target}")
    if target.suffix.lower() not in CSV_SUFFIXES:
        return _refuse("bad_suffix", "班員名簿の CSV を指定してください。")

    try:
        count = import_members_csv(get_db(), str(target))
    except (ValueError, OSError) as exc:
        log.warning("CSV を取り込めませんでした (%s): %s", target, exc)
        return _refuse("import_failed", str(exc))
    log.info("班員名簿を CSV から取り込みました: %s 件 (%s)", count, target)
    return jsonify(_ok(f"班員名簿を {count} 件取り込みました。"))


# ---------------------------------------------------------------------------
# 返し方
# ---------------------------------------------------------------------------
def _ok(message: str) -> dict[str, Any]:
    """更新後の設定一式に、ひとことを添えて返す。

    画面は返ってきたものを描き直すだけでよい(差分を当てない)。
    """
    payload = presenter.to_dict(presenter.view(get_db()))
    payload["message"] = message
    return payload


def _guard(changing: dict[str, bool], body: dict[str, Any]):
    """守っている設定を変えるなら、管理者パスワードを確かめる。

    ``changing`` は「この項目は**本当に値が変わるか**」。変わらないものに
    パスワードを聞くと、現場は「押しても何も起きない」と受け取る。

    通ってよければ ``None``。断るときは **403** ── 形は正しく、
    この端末では**まだ**できない、という意味(401 は認証の仕組みが
    別にあるときの符号なので使わない)。
    """
    names = [name for name, yes in changing.items() if yes]
    result = admin_password.guard(names, str(body.get("password", "")))
    if result is None:
        return None
    return _refuse(result.reason, result.message, status=403)


def _bad_request(message: str):
    return jsonify({"error": {"code": "bad_request", "message": message}}), 400


def _refuse(reason: str, message: str, *, status: int = 422):
    return jsonify({"error": {"code": reason, "message": message}}), status
