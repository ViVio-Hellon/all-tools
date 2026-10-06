"""設定画面のビューモデル

tkinter 版では [ライン設定] [同期設定] [取り込み] [今すぐ同期] が
上部のボタンに散らばっていた。Web 版は**1画面に集める** ── どれも
「この端末をどう動かすか」を決める操作で、頻度も低い。

出すもの:

* この端末のライン設定(休み・コメントの絞り込みに効く)
* 取り込み元 (共有の sqlite3) の場所と、いま同期できているか
* 班員名簿の取り込み状況
* いまの状態(版・置き場所・ログ・取り込み元を開けているか)

**「押せるか」「何と出すか」はここで決める。** 画面はその通りに描くだけ。
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import (
    access_control,
    admin_password,
    app_config,
    config,
    db,
    distribution,
    logging_utils,
    master_admin,
    settings as user_settings,
    sources,
    sync_service,
)
from ..repository import Repository
from ..sync import notices as sync_notices
from ..sync.autosync import SyncState, describe_environment

#: 参照で1回に返す上限。共有フォルダには数千の項目があることがあるので、
#: 全部返すと画面もJSONも重くなる
MAX_ENTRIES = 300

#: 参照で名前を出すファイル。これ以外は出さない ──
#: ここを「サーバの中を読む窓口」にしないため
SOURCE_SUFFIXES = config.SOURCE_SUFFIXES + (".csv",)


@dataclass
class SettingsView:
    """設定画面ぜんぶ。"""

    my_line: str = ""
    line_options: list[str] = field(default_factory=list)
    # 参照パス。**フォルダで持ち、ファイルはその中から探す**
    data_db_dir: str = ""
    master_db_dir: str = ""
    #: 端末で決めていないときに効く値(環境変数)。**欄が空でも動いて
    #: いる**理由を画面に出すために持つ
    data_db_dir_default: str = ""
    master_db_dir_default: str = ""
    data_db_path: str = ""
    master_db_path: str = ""
    data_db_note: str = ""
    master_db_note: str = ""
    auto_sync: bool = True
    sync_interval: int = 20
    sync_state: str = ""
    sync_text: str = ""
    pending: int = 0
    last_received_at: str = ""
    last_sent_at: str = ""
    transport_note: str = ""
    can_auto_sync: bool = False
    member_count: int = 0
    member_note: str = ""
    # マスタ管理。直せないなら理由も出す
    master_editable: bool = False
    master_why: str = ""
    # いまの状態(診断用)
    version: str = ""
    app_root: str = ""
    data_dir: str = ""
    log_dir: str = ""
    #: 管理者パスワードをこの端末で変えてあるか。**値そのものは出さない**
    admin_custom: bool = False
    #: 忘れたときに消す行がどのファイルにあるか
    settings_file: str = ""
    #: アクセス権限で決まる、この端末が使えるライン(``access_control``)
    access: dict[str, Any] = field(default_factory=dict)
    #: 配布設定(``calendar_app/distribution.py``)。パスワードの値は入らない
    distribution: dict[str, Any] = field(default_factory=dict)
    #: 画面の見た目(自動 / ライト / ダーク)と、選べるもの
    theme: str = "auto"
    theme_choices: list[dict[str, str]] = field(default_factory=list)
    #: ログの書き先(``logging_utils.status``)。指定先に書けず逃げていれば理由も
    log: dict[str, Any] = field(default_factory=dict)
    #: ファイルの置き場所(この端末だけ / 全端末で共有 / アプリのフォルダ)
    storage: list[dict[str, Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def view(conn: sqlite3.Connection) -> SettingsView:
    """設定画面を組み立てる。**通信はしない**(状態は同期側が持っている)。"""
    service = sync_service.get_service()
    status = service.status()
    repo = Repository(conn)
    members = repo.member_count()

    # 参照パスから実ファイルを探して、**開いてみる**。
    # exists() で済ませると、壊れているファイルを「見つかった」と答えてしまう
    data_db = sources.find_data_db()
    master_db = sources.find_master_db()
    data_probe = sources.probe(data_db)
    master_probe = sources.probe(master_db)
    ready = master_admin.readiness()

    problems: list[str] = []
    if app_config.load_error():
        problems.append(app_config.load_error())
    if app_config.version_problem():
        problems.append(app_config.version_problem())
    if not user_settings.get_my_line():
        problems.append("この端末のラインが未設定です。"
                        "休みとコメントの絞り込みが効きません。")
    grant = access_control.resolve(conn)
    if grant.managed and not grant.allows(user_settings.get_my_line()):
        problems.append(
            f"この端末のラインがアクセス権限({'・'.join(grant.lines)})と"
            "合っていません。次に起動したときに合わせます"
            "(いま合わせるには「1. この端末のライン」で選び直してください)。")
    problems.extend(f"アクセス権限: {text}"
                    for text in access_control.problems(conn))
    log_status = logging_utils.status()
    if log_status["fallback_reason"]:
        # **ログが途切れていることは、起きたときに知らせる。** 後追いが
        # 要る日になってから「指定先に何も無い」と分かっても遅い
        problems.append("ログフォルダに書けないので、この端末の既定の場所へ"
                        f"書いています: {log_status['fallback_reason']}")
    if data_db is None:
        # **登録そのものを断る状態**なので、はっきり言う
        problems.append(
            "保存用DB(連絡帳)が見つかりません。"
            "参照パスを設定するまで、休み・連絡の登録はできません。")
    elif not data_probe.ok:
        problems.append(f"保存用DB を開けません: {data_probe.error}")
    if master_db is None:
        problems.append(
            "マスタDB(班員名簿)が見つかりません。"
            "参照パスを設定するまで、班員名簿の取り込みと修正はできません。")
    elif not master_probe.ok:
        problems.append(f"マスタDB を開けません: {master_probe.error}")
    elif not members:
        # **休みの作業者を選べない。** 同期で入るはずなので、入らないなら
        # マスタDBの中身か、同期そのものを疑う
        problems.append(
            "班員名簿が0件です。休みの作業者を選べません。"
            "同期を待っても入らないときは、「3. 取り込む」の"
            "[マスタDBだけ] を押すか、マスタDBに班員名簿があるか確かめてください。")

    return SettingsView(
        my_line=user_settings.get_my_line(),
        # **表で決まっていれば、選べるのはそのラインだけ**(一覧に無いものは
        # 選べない。1つなら固定)。決まっていなければ全部(パスワードで守る)
        line_options=list(grant.lines) if grant.managed
        else list(config.ALL_LINE_NAMES),
        access=grant.to_dict(),
        data_db_dir=user_settings.data_db_dir_setting(),
        master_db_dir=user_settings.master_db_dir_setting(),
        data_db_dir_default=config.default_data_db_dir(),
        master_db_dir_default=config.default_master_db_dir(),
        data_db_path=str(data_db) if data_db else "",
        master_db_path=str(master_db) if master_db else "",
        data_db_note=data_probe.describe(),
        master_db_note=master_probe.describe(),
        master_editable=ready.ok,
        master_why=ready.why,
        auto_sync=user_settings.auto_sync_enabled(),
        sync_interval=user_settings.sync_interval(),
        sync_state=status.state.name.lower(),
        sync_text=status.describe(),
        pending=status.pending,
        last_received_at=db.get_meta(conn, "last_received_at"),
        last_sent_at=db.get_meta(conn, "last_sent_at"),
        transport_note=describe_environment(),
        can_auto_sync=service.enabled,
        member_count=members,
        member_note=("" if members else
                     "班員名簿がありません。マスタDB か CSV を取り込んでください。"),
        version=app_config.version_label(),
        app_root=str(app_config.APP_ROOT),
        data_dir=str(config.app_home()),
        log_dir=logging_utils.current_folder(),
        admin_custom=admin_password.is_custom(),
        settings_file=str(config.settings_path()),
        distribution=distribution.summary(),
        log=log_status,
        theme=user_settings.get_theme(),
        theme_choices=[{"value": value, "label": label}
                       for value, label in user_settings.THEME_CHOICES],
        storage=storage(),
        problems=problems,
    )


def to_dict(v: SettingsView) -> dict[str, Any]:
    return {
        "my_line": v.my_line,
        "line_options": v.line_options,
        "data_db_dir": v.data_db_dir,
        "master_db_dir": v.master_db_dir,
        "data_db_dir_default": v.data_db_dir_default,
        "master_db_dir_default": v.master_db_dir_default,
        "data_db_path": v.data_db_path,
        "master_db_path": v.master_db_path,
        "data_db_note": v.data_db_note,
        "master_db_note": v.master_db_note,
        "master_editable": v.master_editable,
        "master_why": v.master_why,
        "auto_sync": v.auto_sync,
        "sync_interval": v.sync_interval,
        "sync_state": v.sync_state,
        "sync_text": v.sync_text,
        "pending": v.pending,
        "last_received_at": v.last_received_at,
        "last_sent_at": v.last_sent_at,
        "transport_note": v.transport_note,
        "can_auto_sync": v.can_auto_sync,
        "member_count": v.member_count,
        "member_note": v.member_note,
        "version": v.version,
        "app_root": v.app_root,
        "data_dir": v.data_dir,
        "log_dir": v.log_dir,
        "admin_custom": v.admin_custom,
        "settings_file": v.settings_file,
        "access": v.access,
        "distribution": v.distribution,
        "log": v.log,
        "theme": v.theme,
        "theme_choices": v.theme_choices,
        "storage": v.storage,
        "problems": v.problems,
    }


# ---------------------------------------------------------------------------
# ファイルの置き場所 ── この端末だけのもの / 全端末で共有するもの
# ---------------------------------------------------------------------------
# **置き場所で性質が違う。** 取り違えると、消してはいけないものを消す
# (送信待ちが消える)、1台で直したつもりのものが他の端末に効かない、が
# 起きる。設定画面に一覧で出す(画面は写すだけ)。
GROUP_LOCAL = "この端末だけ(このPC・このWindows利用者で引き継ぐ)"
GROUP_SHARED = "全端末で共有(参照パスの先)"
GROUP_APP = "アプリのフォルダ(配ると一緒に届く)"


def storage() -> list[dict[str, Any]]:
    """ファイルとフォルダの一覧。**どこに何があり、消したらどうなるか。**"""
    from .. import distribution

    local = app_config.local_root()

    def item(group: str, name: str, path: Path | None, holds: str,
             note: str, keep: bool) -> dict[str, Any]:
        return {"group": group, "name": name,
                "path": str(path) if path else "(見つかりません)",
                "exists": bool(path and path.exists()),
                "holds": holds, "note": note, "keep": keep}

    data_db, master_db = sources.find_data_db(), sources.find_master_db()
    return [
        # --- この端末だけ ---
        item(GROUP_LOCAL, "settings.json", config.settings_path(),
             "この端末の設定: ライン・参照パス・自動同期・この端末で変えた"
             "管理者パスワード・配布設定を読んだ記録・ログフォルダ・画面の見た目",
             "消すと、この端末の設定が初めの状態に戻ります"
             "(配布設定があれば次の起動で読み込み直します)。",
             True),
        item(GROUP_LOCAL, "calendar.db", config.sqlite_path(),
             "共有の写し(休み管理・削除履歴・班員名簿・アクセス権限)と、"
             "まだ送れていない入力(送信待ち)",
             "消さないでください。送信待ちの入力が他の端末へ届かなくなります"
             "(写しは次の同期で作り直されます)。",
             True),
        item(GROUP_LOCAL, "backup", app_config.local_dir("backup"),
             "マスタを直す前・変換する前に取った控え",
             "壊したときに戻すためのもの。古いものは消してかまいません。",
             False),
        item(GROUP_LOCAL, "logs", app_config.local_dir("logs"),
             "動きの記録(ログ)。ログフォルダを指定していなければここへ書きます。"
             "指定先に書けないときもここへ書きます",
             f"エラーの後追いに使います({logging_utils.KEEP_DAYS}日で消えます)。"
             "消してもアプリは動きます。",
             False),
        item(GROUP_LOCAL, "runtime / cache / work / pycache", local,
             "実行中の情報・一時ファイル",
             "アプリを止めているときなら消してかまいません。",
             False),
        *_custom_log_item(),
        # --- 全端末で共有 ---
        item(GROUP_SHARED, config.SOURCE_FILE_DATA, data_db,
             "休み管理・削除履歴・端末一覧(どのPCがどのラインか)",
             "全端末の正式なデータです。ここを直すと全端末に効きます。",
             True),
        item(GROUP_SHARED, config.SOURCE_FILE_MASTER, master_db,
             "班員名簿・アクセス権限(梱包資材総合ツールと共用)",
             "マスタ管理から直します。全端末(と別ツール)に効きます。",
             True),
        # --- アプリのフォルダ ---
        item(GROUP_APP, "配布設定", distribution.DIR,
             "配る前に書き出した設定(参照パス・管理者パスワードなど)",
             "配った先が起動したときに読み込みます。その端末にすでにある"
             "設定は上書きしません。",
             False),
        item(GROUP_APP, "config/app.json", app_config.CONFIG_PATH,
             "版番号・ポートなど、アプリそのものの値",
             "新しい版を入れると差し替わります。端末ごとの設定は入っていません。",
             True),
    ]


def _custom_log_item() -> list[dict[str, Any]]:
    """ログフォルダを指定していれば、その場所も一覧に出す。

    置き場所が共有かどうかはこちらには分からない(指定したのは人)ので、
    「指定した場所」として出し、**PC名のフォルダに分かれる**ことを書く。
    """
    text = user_settings.log_dir_setting()
    if not text:
        return []
    try:
        path: Path | None = logging_utils.folder_for(text)
    except ValueError:
        path = None
    return [{
        "group": "設定で指定した場所(9. ログ)",
        "name": "ログフォルダ",
        "path": str(path) if path else f"{text}(読めない指定です)",
        "exists": bool(path and path.exists()),
        "holds": "この端末の動きの記録(ログ)。PC名のフォルダに分けて書くので、"
                 "共有フォルダを指定しても他の端末と混ざりません",
        "note": f"{logging_utils.KEEP_DAYS}日より古いものは、その端末が消します。"
                "消してもアプリは動きます。",
        "keep": False,
    }]


# ---------------------------------------------------------------------------
# 同期の状態だけ(帯と画面が短い間隔で読む)
# ---------------------------------------------------------------------------
def sync_dict() -> dict[str, Any]:
    """``GET /api/sync`` が返すもの。**軽いことが要点。**

    帯は数秒ごとにこれを読むので、DBを開くのは送信待ちの件数を数える
    1回だけに抑える。
    """
    service = sync_service.get_service()
    status = service.status()
    return {
        "state": status.state.name.lower(),
        "text": status.describe(),
        "pending": status.pending,
        "message": status.message,
        "busy": service.is_busy(),
        "configured": service.configured,
        "enabled": service.enabled,
        # 送れていないことは**画面で目立たせる**。黙って古い表示を
        # 出し続けない(基盤仕様書 2.9)
        "offline": status.state is SyncState.OFFLINE,
        # ここから下の2つは「画面を描き直す時機」を決めるためのもの。
        # 帯がこれを数秒ごとに読んでいるので、カレンダーは別に見張らずに
        # 済む(同じ事実を2か所で持たない)。
        #
        # **開けっぱなしでも古い表示のままにしない。** 取り込み直すと
        # 他ラインの登録が手元へ入るが、画面がそれを知る手立てが無いと、
        # 帯だけが「同期済み」と言う嘘の状態になる
        "last_received_at": status.last_received_at,
        # 日付が変わったら「今日」の枠も動かす。ここはサーバが決める
        # (端末の時計とずれても、塗る根拠は1つ)
        "today": _dt.date.today().strftime(config.DATE_KEY_FORMAT),
        # 送らずに取りやめた登録(先に他の端末が登録していた)。**確かめたと
        # 押されるまで出す**。文言はここで組む(``sync/notices.py``)
        "skipped": sync_notices.describe(
            sync_notices.pending_at(str(config.sqlite_path()))),
    }


# ---------------------------------------------------------------------------
# サーバ側のフォルダ参照 (tkinter 版 filedialog.askopenfilename の置き換え)
# ---------------------------------------------------------------------------
# ブラウザのファイル選択ダイアログは**中身**しか渡してくれない。
# アプリが要るのは「このPCから見た共有フォルダのパス」なので、選ばせても
# 意味が無い。そこでサーバ側から見えるフォルダを一覧する。
#
# **名前しか返さない**(中身は返さない)ことと、件数に上限を置くことで、
# ここが「サーバの中を読む窓口」にならないようにしてある。守りそのものは
# ``app/__init__.py`` の 127.0.0.1 バインド + 起動トークン + 同一オリジン確認
# が担う。
def browse(path_text: str) -> dict[str, Any]:
    """フォルダを1つ開いて、中のフォルダと取り込み元の名前を返す。"""
    raw = (path_text or "").strip()
    if not raw:
        return {"path": "", "parent": "", "exists": False, "readable": False,
                "message": "フォルダのパスを入力してください。",
                "dirs": _roots(), "files": []}

    target = Path(raw).expanduser()
    # ファイルを渡されたら、その親を開く(利用者はたいてい貼り付ける)
    if target.is_file():
        target = target.parent

    if not target.exists():
        return {"path": str(target), "parent": _parent(target), "exists": False,
                "readable": False,
                "message": f"見つかりません: {target}", "dirs": [], "files": []}

    dirs: list[dict[str, str]] = []
    files: list[dict[str, str]] = []
    try:
        for entry in sorted(target.iterdir(), key=lambda p: p.name.lower()):
            if len(dirs) + len(files) >= MAX_ENTRIES:
                break
            try:
                if entry.is_dir():
                    dirs.append({"name": entry.name, "path": str(entry)})
                elif entry.suffix.lower() in SOURCE_SUFFIXES:
                    files.append({"name": entry.name, "path": str(entry)})
            except OSError:
                continue          # 読めない項目は飛ばす(一覧そのものは返す)
    except PermissionError:
        return {"path": str(target), "parent": _parent(target), "exists": True,
                "readable": False,
                "message": f"読み取りの権限がありません: {target}",
                "dirs": [], "files": []}
    except OSError as exc:
        return {"path": str(target), "parent": _parent(target), "exists": True,
                "readable": False, "message": f"開けません: {exc}",
                "dirs": [], "files": []}

    message = ""
    if not files:
        message = "このフォルダに取り込み元(.sqlite3/.db)はありません。"
    return {"path": str(target), "parent": _parent(target), "exists": True,
            "readable": True, "message": message, "dirs": dirs, "files": files}


def _parent(path: Path) -> str:
    parent = path.parent
    return "" if parent == path else str(parent)


def _roots() -> list[dict[str, str]]:
    """出発点。Windows はドライブ、それ以外はホームと ``/``。"""
    import os

    if os.name == "nt":
        found = []
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            drive = Path(f"{letter}:/")
            if drive.exists():
                found.append({"name": f"{letter}:", "path": str(drive)})
        return found
    return [{"name": "ホーム", "path": str(Path.home())},
            {"name": "/", "path": "/"}]
