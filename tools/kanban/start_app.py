"""Python 側の起動開始点 (基盤仕様書 2.1 / 2.2 / 2.5)

``Start.vbs`` と ``start.bat`` はどちらもここへ来る。違うのはコンソールを
見せるかどうかだけで、**起動処理は1本**にしてある。

    Start.vbs ─┐
               ├─▶ start_app.py ─▶ launch_guard ─▶ server (waitress)
    start.bat ─┘                                    ├─ boot_server(待機画面)
                                                    └─ 差し替え ─▶ app/(Flask)

【順序に理由がある】

1. **ローカル領域を先に作る。** ログの置き場所が無いと、以降の失敗が
   どこにも残らない
2. **待ち受けを先に始める。** Flask とアプリ本体の import がいちばん重い。
   先に待機画面を出さないと、利用者には「押しても何も起きない」時間ができる
3. **ブラウザは待ち受けの確認が取れてから開く。** 開くのが早すぎると
   「接続できません」が出て、起動に失敗したように見える(基盤仕様書 2.3)
4. **重い初期化(取り込み)は待機画面を出したまま行う。** 終わってから
   ``mark_ready()`` で本体へ移す
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

# アプリ本体を import する前に、キャッシュの置き場所をローカルへ向ける。
# 共有フォルダに ``__pycache__`` を作らせないため(基盤仕様書 2.7)
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from kanban import app_config  # noqa: E402

if not os.environ.get("PYTHONPYCACHEPREFIX"):
    try:
        app_config.ensure_local_dirs()
        os.environ["PYTHONPYCACHEPREFIX"] = str(app_config.local_dir("pycache"))
    except OSError:
        pass  # 作れなくても起動は続ける

from kanban import applog, config, presence  # noqa: E402
from kanban.applog import get_logger  # noqa: E402

log = get_logger("launcher")

#: 起動確認の待ち上限(秒)
LISTEN_TIMEOUT_SEC = 15.0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="start_app",
        description="資材発注看板システム(Web 版)を起動する",
    )
    p.add_argument("--mode", choices=config.ALL_MODES,
                   help="起動するモード。省略時はこの端末に保存されたモード")
    p.add_argument("--line", help="担当ラインコード(現場モード用)")
    p.add_argument("--config", help="設定ファイルのパス")
    p.add_argument("--shared-db", help="共有 SQLite のパス")
    p.add_argument("--sqlite", help="SQLite ファイルのパス")
    p.add_argument("--port", type=int, help="使用ポート(省略時は config/app.json)")
    p.add_argument("--no-browser", action="store_true", help="ブラウザを開かない")
    p.add_argument("--no-import", action="store_true", help="起動時の取り込みを行わない")
    p.add_argument("--diagnose", action="store_true",
                   help="診断情報を出してから起動する(start.bat 用)")
    p.add_argument("--check", action="store_true",
                   help="実行環境を確認して終了する(起動はしない)")
    return p


def check_environment(args=None) -> int:
    """起動せずに実行環境だけ確かめる (基盤仕様書 ステップ5 / 2.6)

    ``start.bat`` が起動の前に呼ぶ。「起動しない」と言われたときに、利用者
    自身が**どこで詰まっているか**を読めるようにするためのもの。

    ``--config`` などの上書きは**起動時と同じように効かせる**。ここだけ
    既定の設定を見ていると、「確認は通るのに起動できない」(あるいはその逆)
    という、いちばん調べにくい食い違いが起きる。
    """
    print(app_config.describe())
    print()

    problems: list[str] = []

    print(f"Python        : {sys.version.split()[0]} ({sys.executable})")
    if sys.version_info < (3, 9):
        problems.append(f"Python 3.9 以上が要ります(いま {sys.version.split()[0]})")

    for name, label in (("flask", "Flask"), ("waitress", "waitress")):
        try:
            __import__(name)
        except ImportError:
            problems.append(
                f"{label} が入っていません。"
                "このフォルダで pip install -r requirements.txt を実行してください"
            )
            continue
        # 版は importlib.metadata から引く。``__version__`` は Flask 3.2 で
        # 消える予定で、waitress にはそもそも無い
        try:
            from importlib.metadata import version as _pkg_version

            print(f"{label:<14}: {_pkg_version(name)}")
        except Exception:  # noqa: BLE001 - 版が読めなくても動作には困らない
            print(f"{label:<14}: (版不明)")

    try:
        root = app_config.ensure_local_dirs()
        print(f"ローカル領域  : 書き込みできます ({root})")
    except OSError as exc:
        problems.append(f"ローカル領域を作れません: {exc}")

    cfg = _load_config(args) if args is not None else config.load_config()
    shared = cfg.resolved_shared_db_path()
    status = config.read_settings_file(Path(cfg.source_path) if cfg.source_path else None)
    print(
        f"設定ファイル  : {status.path}  "
        + (f"[読めません: {status.error}]" if status.error
           else "[あり]" if status.exists else "[なし(組み込みの既定で動きます)]")
    )
    print(f"担当ライン    : {cfg.line or '(未設定)'}")
    print(f"管理者パスワード: {'設定あり' if cfg.admin_password_hash else '未設定'}")
    from kanban import distribution

    bundle = distribution.read()
    print(
        f"配布設定      : {distribution.directory()}  "
        + (f"[あり: {'、'.join(distribution.ITEM_LABELS[k] for k in bundle.settings)}"
           f"。次に起動したとき、この端末に無い項目だけ読み込みます]" if bundle
           else "[なし]")
    )
    # どこから来た接続先か。配った先で「違う場所を見ている」と言われたときの手がかり
    origin = "設定ファイル" if "shared_db_path" in status.values else "組み込みの既定"
    print(f"共有DB        : {shared or '(未設定)'}  [{origin}]")
    print(f"手元のSQLite  : {cfg.resolved_sqlite_path()}")
    if not shared:
        problems.append(
            "shared_db_path が未設定です。設定画面か config.json で指定してください"
        )
    elif not Path(shared).exists():
        # 共有フォルダが一時的に見えないだけのこともあるので、警告に留める。
        # ただし .accdb しか無いなら、変換がまだであることを具体的に言う
        legacy = Path(cfg.resolved_accdb_path())
        if legacy.exists():
            problems.append(
                f"共有DBがまだありません: {shared}\n"
                f"     旧 Access ファイルは見つかりました: {legacy}\n"
                "     次を実行して変換してください:\n"
                f'       python tools/accdb_to_sqlite.py "{legacy}" "{shared}"'
            )
        else:
            print("  【注意】いまこのパスは見えません(共有フォルダが落ちている可能性)")
    else:
        # 別のツールの sqlite3(梱包資材マスタなど)を指していないか
        from kanban.db.shared import kanban_tables_in, not_kanban_reason

        try:
            kanban, others = kanban_tables_in(Path(shared))
        except Exception as exc:  # noqa: BLE001
            problems.append(f"共有DBの中を確かめられません: {exc}")
        else:
            if not kanban:
                problems.append(not_kanban_reason(shared, others))
            else:
                print(f"  看板の表: {len(kanban)} 個({'、'.join(kanban[:4])}{' …' if len(kanban) > 4 else ''})")

    _check_access(cfg, status)
    _check_history(cfg, status)

    for problem in app_config.port_range_conflicts():
        problems.append(problem)

    print()
    if problems:
        print("=== 直す必要があります ===")
        for i, problem in enumerate(problems, 1):
            print(f"  {i}. {problem}")
        return 1
    print("問題は見つかりませんでした。")
    return 0


def _check_history(cfg, status) -> None:
    """``--check`` の「看板履歴」と「CSV の書き出し先」。止める理由にはしない。"""
    from kanban.db import history

    path = cfg.resolved_history_db_path()
    origin = "設定ファイル" if "history_db_path" in status.values else "既定: 共有DBと同じフォルダ"
    print(f"看板履歴      : {path}  [{origin}]")
    problem = history.path_problem(path)
    if problem:
        print(f"  【注意】{problem}")
    elif not Path(path).exists():
        print("  まだありません(起動したとき・最初に記録を送るときに作ります)")
    else:
        rows = history.row_count(history.HistoryDb(path))
        print(f"  {rows if rows is not None else '?'} 件")
    print(f"CSV の書き出し先: {cfg.resolved_csv_dir()}  [{'設定ファイル' if cfg.csv_dir else 'この端末のローカル領域'}]")
    from kanban import trace

    print(f"記録(ログ)    : {cfg.resolved_log_dir()}  [{'設定ファイル' if 'log_dir' in status.values else 'この端末のローカル領域'}]"
          f"  記録は {trace.root_for(cfg.resolved_log_dir())}")


def _check_access(cfg, status) -> None:
    """``--check`` の「アクセス権限」。**止める理由にはしない**(読めなくても現場モードで開ける)。"""
    from kanban import access_control

    me = access_control.current_identity()
    path = cfg.resolved_access_db_path()
    origin = "設定ファイル" if "access_db_path" in status.values else "既定: 共有DBと同じフォルダ"
    print(f"アクセス権限  : {path}  [{origin}]")
    print(f"  この端末: ログインID {me.login_id or '(不明)'} / PC名 {me.pc_name or '(不明)'}")
    problem = access_control.path_problem(path)
    if problem:
        print(f"  【注意】{problem}\n"
              "  (読めないあいだは前回読めた内容で、それも無ければ現場モードだけで開きます)")
        return
    try:
        rules, has_table = access_control.read_rules(access_control.open_db(cfg))
    except access_control.Unavailable as exc:
        print(f"  【注意】{exc}")
        return
    if not has_table:
        print(f"  {access_control.TABLE} の表がまだありません(起動したとき・マスタ管理を開いたときに作ります)")
    grant = access_control.resolve(rules, me)
    print(f"  使えるモード: {_describe_grant(grant)}")
    for note in access_control.problems(rules):
        print(f"  【注意】{note}")


class StartupError(Exception):
    """起動できない。``hint`` は「次に何をすればよいか」(デスクトップ版は窓に出す)。"""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.check:
        return check_environment(args)

    root = app_config.ensure_local_dirs()
    cfg, store, mode, line, grant, access_notice = _prepare(args)

    # --- デスクトップ版が動いていないか -----------------------------------
    # 同じ端末の手元の SQLite と共有DBへ、2 つのプロセスが書きに行かないようにする。
    # デスクトップ版(統合ツールの窓の「看板」)は OS のファイルロックを握っている
    # (``start_bridge`` → ``launch_guard.hold_desktop_lock``)
    import launch_guard

    if launch_guard.desktop_running():
        message = (f"{DESKTOP_RUNNING_MESSAGE}。{DESKTOP_RUNNING_HINT}")
        log.info("起動しません: %s", message)
        print(message)
        store.close()
        return 0

    # --- すでに動いていないか ------------------------------------------
    guard = launch_guard.check_existing(mode)
    if not guard.should_start:
        log.info("起動しません: %s", guard.reason)
        opened = bool(not args.no_browser and guard.url)
        if opened:
            _open_browser(guard.url)
        # **「すでに起動しています」で終わらせない。**
        #
        # このアプリに窓は無く、``pythonw.exe`` はタスクマネージャーの
        # 「アプリ」にも出てこない。利用者からは「どこにも開いていないのに
        # 開いている扱い」に見える ── どこに居るのかと、どう止めるのかを
        # その場で出す。
        info = guard.existing
        tail = "ブラウザを開きました。" if opened else "ブラウザは開きません。"
        print(f"すでに起動しています({guard.reason})。{tail}")
        if info is not None:
            print(
                f"\n  場所 : {info.url}\n"
                f"  PID  : {info.pid}(タスクマネージャーの「バックグラウンド プロセス」にいます)\n"
                f"  起動 : {info.started_text}\n"
                "\n開いた画面が使えない・見覚えが無いときは、いったん止めてください:\n"
                "  stop.bat            … 止める\n"
                "  stop.bat --force    … 未反映の操作があっても止める"
            )
        store.close()
        return 0

    # --- 起動することを、ポートより先に名乗る ---------------------------
    # 以前は待ち受けを始めてから印を書いていた。判定からそこまで 1 秒以上あり、
    # その間にもう一度起動すると 2 つとも「誰も居ない」を見て進み、pick_port が
    # 隣の番号へずれて静かに 2 つ動く(日報管理ツールで実際に起きた。同じ直し方)。
    # 名乗れなかった = ほんの一瞬先に誰かが名乗った、なのでもう一度判定して合流する
    if not launch_guard.claim_lock(launch_guard.build_lock_info(mode, 0)):
        again = launch_guard.check_existing(mode)
        log.info("ほぼ同時にもう 1 つ起動されました。先の 1 つへ合流します: %s", again.reason)
        if again.url and not args.no_browser:
            _open_browser(again.url)
        print(f"すでに起動しています({again.reason or 'ほぼ同時に起動されたもう 1 つ'})。")
        store.close()
        return 0

    port = args.port or launch_guard.pick_port(mode)
    if port is None:
        launch_guard.release_lock(mode)
        message = (
            f"使えるポートがありません(候補 {app_config.port_candidates(mode)})。\n"
            "他のアプリが使っていないか確認してください。"
        )
        log.error(message)
        print(message, file=sys.stderr)
        store.close()
        return 2

    return _run(args, cfg, store, mode, line, port, root,
                grant=grant, access_notice=access_notice)


DESKTOP_RUNNING_MESSAGE = "資材発注看板システムはデスクトップ版(統合ツールの窓)で動いています"
DESKTOP_RUNNING_HINT = ("統合ツールの窓の「看板」のタブをお使いください"
                        "(ブラウザ版は開きません。開くときは統合ツールの窓を閉じてから)。")
BROWSER_RUNNING_MESSAGE = "資材発注看板システムのブラウザ版が動いています"
BROWSER_RUNNING_HINT = ("ブラウザ版とデスクトップ版は同時には使えません。"
                        "ブラウザの画面の「終了」(または stop.bat)で閉じてから、"
                        "このタブの「もう一度開く」を押してください。")


def start_bridge(argv: list[str] | None = None, *, token: str = "", server_factory) -> int:
    """デスクトップ版の起動(``bridge.py`` から)。**ポートも印のファイルも使わない。**

    窓・終了は外枠(統合ツールの Rust/Tauri)が持つ。ここでするのはブラウザ版と
    同じ「設定・権限・モードを決める → 待機画面 → 本体を組み立てる → 取り込み」だけで、
    その中身(:func:`_prepare` と :func:`_serve`)はブラウザ版と共有する ──
    2 本持つと片方だけ直すことになる。

    違うのは次の 3 つだけ:

    * 待ち受けない(``server_factory`` が標準入出力で受けるサーバを作る)
    * 心拍による自動終了を使わない(窓を閉じたら外枠が終わらせる)
    * ブラウザ版が動いていたら**こちらが止まる**(後から開いたほうが止まる。
      同じ SQLite に 2 つが書かないように)。動き始めたら ``runtime/desktop.lock`` を
      握り続ける(ブラウザ版はそれを見て起動しない)
    """
    import secrets

    import server as server_module

    args = build_parser().parse_args(argv or [])
    args.no_browser = True
    args.desktop = True
    _check_bridge_environment()
    try:
        root = app_config.ensure_local_dirs()
    except OSError as exc:
        raise StartupError(
            f"作業用フォルダを作れません: {exc}",
            f"{app_config.local_root()} に書き込めるか確かめてください。",
        ) from exc
    import launch_guard

    # **自分の錠を取ってから**相手を見る(ブラウザ版は印を書いてからこの錠を見直す。
    # 同時に開いても、少なくとも一方が相手に気づく)
    if not launch_guard.hold_desktop_lock():
        raise StartupError("資材発注看板システムがほかの窓で動いています",
                           "開いている窓をお使いください。")
    running = launch_guard.browser_running()
    if running:
        launch_guard.release_desktop_lock()
        log.warning("ブラウザ版が動いているので、デスクトップ版は起動しません: %s", running)
        raise StartupError(f"{BROWSER_RUNNING_MESSAGE}({running})", BROWSER_RUNNING_HINT)
    cfg, store, mode, line, grant, access_notice = _prepare(args)

    srv = server_factory(mode, token or secrets.token_urlsafe(24))
    thread = server_module.run_in_background(srv)
    code = _serve(args, cfg, store, mode, line, root, srv, thread,
                  grant=grant, access_notice=access_notice, watch_idle=False)
    log.info("終了しました: mode=%s(デスクトップ版)", mode)
    from kanban import trace

    trace.write("startup", "終了(デスクトップ版)")
    applog.close()
    return code


def _check_bridge_environment() -> None:
    """デスクトップ版で要るもの。**waitress は要らない**(待ち受けないので)。"""
    if sys.version_info < (3, 9):
        raise StartupError(
            f"Python 3.9 以上が要ります(いま {sys.version.split()[0]})",
            "https://www.python.org/downloads/ から新しい Python を入れてください。",
        )
    try:
        import flask  # noqa: F401
    except ImportError as exc:
        raise StartupError(
            "Flask が入っていません",
            "コマンドプロンプトでこのフォルダへ移り、次を1度だけ実行してください:\n"
            "python -m pip install -r requirements.txt",
        ) from exc


def _prepare(args):
    """設定・ログ・配布設定・モード・アクセス権限・担当ラインを決める(ブラウザ版とデスクトップ版で共通)。

    ``(cfg, store, mode, line, grant, access_notice)`` を返す。
    """
    cfg = _load_config(args)
    applog.initialize(
        cfg.resolved_log_dir(),
        line_name=cfg.line,
        app_name=config.APP_NAME,
        echo_to_stderr=bool(args.diagnose),
    )
    _start_records(cfg)

    log.info("=" * 60)
    log.info("起動します: %s", app_config.display_name())
    for line in app_config.describe().splitlines():
        log.info("  %s", line)
    if args.diagnose:
        print(app_config.describe())
        print(f"ログ        : {applog.log_path()}")

    # --- 配布設定を読み込む(python-web-tools と同じ) --------------------
    # アプリのフォルダの直下に「配布設定」があれば、**この端末に無い項目だけ**
    # 埋める。``--config`` で 1 つのファイルを渡されたとき(検証)は触らない
    if not args.config:
        from kanban import distribution

        loaded = distribution.apply_on_start()
        if loaded.applied:
            cfg = _load_config(args)
            text = f"配布設定を読み込みました: {'、'.join(loaded.applied)}"
            if loaded.kept:
                text += f"(この端末にすでにあったので読まなかったもの: {'、'.join(loaded.kept)})"
            log.info(text)
            if args.diagnose:
                print(text)
        elif not loaded.ok:
            log.warning(loaded.message)
    if cfg.resolved_log_dir() != applog.current_dir():
        # 配布設定で記録の置き場所が来た。その場所で開き直す
        applog.reinitialize(cfg.resolved_log_dir())
    _log_settings(cfg, echo=bool(args.diagnose))

    # --- モードを決める ------------------------------------------------
    store = _open_store(cfg)
    mode = args.mode or store.get_device_mode() or config.MODE_SITE
    if mode not in config.ALL_MODES:
        log.warning("保存されていたモードが不正です: %r → 現場モードで起動します", mode)
        mode = config.MODE_SITE
    if args.mode:
        log.info("モードを一時的に上書き: %s(SQLite には保存しません)", mode)
    elif not store.get_device_mode():
        # 初回。既定を書いておく(次回から選択の手間が無い)。設定画面から変えられる
        store.set_device_mode(mode)
        log.info("初回起動のため既定のモードを保存しました: %s", mode)

    # --- アクセス権限で、開けるモードか確かめる --------------------------
    # 権限の無いモードは開かない。**保存してあるモードは書き換えない** ──
    # 権限の行を足せば、次に開いたときから頼んだモードで開く
    mode, grant, access_notice = _apply_access(cfg, store, mode)
    if args.diagnose:
        print(f"アクセス権限: {_describe_grant(grant)}")
        if access_notice:
            print(access_notice)

    # --- アクセス権限の表のライン(この端末の行に 1 つだけ書いてあれば) ---
    # **起動するたびに表のラインにする。** 管理者パスワードで変えた担当ラインは、その起動の
    # あいだだけ ── 次も変えたままにしたいなら、マスタ(アクセス権限)を書き換える
    if not args.line and mode == config.MODE_SITE:
        from kanban import access_control

        code = access_control.line_at_startup(grant)
        if code:
            if code != cfg.line:
                log.info("アクセス権限の表から担当ラインを %s にします(前: %s)", code, cfg.line or "未設定")
                # 起動用に既定の置き場所を埋めた cfg は保存しない(それを「決めた」ことにしない)
                saved = config.load_config(args.config)
                saved.line = code
                config.save_config(saved)
                cfg.line = code
            access_control.mark_line_applied(store, code)
        if grant.line_problem or grant.ignored:
            log.info("アクセス権限のライン: %s%s", grant.line_problem or "決まり",
                     f"(読まなかった権限: {'・'.join(grant.ignored)})" if grant.ignored else "")

    line = args.line or cfg.line
    from kanban import trace

    trace.set_context(mode=mode, line=line if mode == config.MODE_SITE else "")
    trace.write("startup", f"起動: {config.mode_display_name(mode)}", extra={
        "頼まれたモード": args.mode or store.get_device_mode() or "",
        "アクセス権限": _describe_grant(grant),
        "共有DB": cfg.resolved_shared_db_path(), "看板履歴": cfg.resolved_history_db_path(),
        "梱包資材マスタ": cfg.resolved_access_db_path(), "手元のSQLite": cfg.resolved_sqlite_path(),
        "ログ": str(applog.log_path() or ""), "Python": sys.version.split()[0],
        "画面": "デスクトップ版(ポートなし)" if getattr(args, "desktop", False) else "ブラウザ版",
    })
    if mode == config.MODE_SITE and not config.is_supported_line(line):
        log.warning("担当ラインが未設定/未登録です: %r", line)

    return cfg, store, mode, line, grant, access_notice


def _start_records(cfg) -> None:
    """記録(後追い・なぜなぜ用。:mod:`kanban.trace`)に、この端末の前提を付ける・古い分を片付ける。"""
    from kanban import access_control, trace

    me = access_control.current_identity()
    trace.set_context(pc=me.pc_name, login=me.login_id, version=app_config.version_label())
    try:
        removed = trace.prune()
        if removed:
            log.info("古い記録を %d 件片付けました(%d 日より前・この端末の分だけ)", removed, trace.KEEP_DAYS)
    except Exception:  # noqa: BLE001
        log.debug("古い記録を片付けられませんでした", exc_info=True)


def _apply_access(cfg, store, mode: str):
    """``(開くモード, 権限, 画面に出す一言)``。権限が無ければ**狭いほうのモード**で開く。

    梱包資材マスタの「アクセス権限」をログインIDとPC名で引く
    (:mod:`kanban.access_control`)。届かないときは前回読めた内容で判断する。
    """
    from kanban import access_control

    try:
        grant = access_control.grant_for(cfg, store)
    except Exception as exc:  # noqa: BLE001 - 読めないなら既定(現場モードだけ)
        log.exception("アクセス権限を確かめられませんでした")
        grant = access_control.resolve([], access_control.current_identity())
        grant.reason = f"アクセス権限を確かめられませんでした: {exc}"
    log.info("アクセス権限: %s", _describe_grant(grant))
    if grant.allows(mode):
        return mode, grant, ""

    opened = grant.startup_mode(mode)
    notice = (
        f"この端末({grant.identity.label()})には{config.mode_display_name(mode)}の権限がないので、"
        f"{config.mode_display_name(opened)}で開きました。\n{grant.how_to_allow(mode)}"
        + (f"\n({grant.reason})" if grant.reason else "")
        + f"\n{config.mode_display_name(opened)}のままでよければ、設定 → この端末 でモードを"
        f"{config.mode_display_name(opened)}にすると、この表示は出なくなります。"
    )
    log.warning("権限が無いので %s ではなく %s で開きます: %s", mode, opened, grant.identity.label())
    return opened, grant, notice


def _describe_grant(grant) -> str:
    """ログと ``--check`` に出す 1 行。"""
    from kanban import access_control

    names = [config.mode_display_name(m) for m in grant.allowed_modes()
             if m in access_control.MODE_PERMISSION]
    source = {"fresh": "いま読んだ", "cache": "前回読めた内容", "none": "読めない"}.get(grant.source, "")
    text = f"{grant.identity.label()} → {'・'.join(names) or 'なし'}"
    if source:
        text += f"({source}: {grant.path})"
    if grant.reason:
        text += f" / {grant.reason}"
    return text


def _run(args, cfg, store, mode: str, line: str, port: int, root: Path,
         *, grant=None, access_notice: str = "") -> int:
    """待ち受けを始めて、本体を組み立てて、止められるまで待つ。"""
    import launch_guard
    import server

    srv = server.AppServer(mode, port)
    # 先に立てた印(main の claim_lock)へ、決まったポートとトークンを書き足す。
    # **ここで初めて stop.bat と次の起動が声を掛けられるようになる**
    lock = launch_guard.build_lock_info(mode, port, srv.token)
    if launch_guard.update_lock(mode, port=lock.port, url=lock.url, token=lock.token) is None:
        launch_guard.write_lock(lock)          # 名乗らずに来た(試験から直に呼んだ)
    # 印を書いてから、デスクトップ版をもう一度見る(調べてから印を書くまでの
    # 間に統合ツールの窓で看板が開いていたら、こちらが止まる)
    if launch_guard.desktop_running():
        launch_guard.release_lock(mode)
        message = f"{DESKTOP_RUNNING_MESSAGE}。{DESKTOP_RUNNING_HINT}"
        log.info("起動しません: %s", message)
        print(message)
        store.close()
        return 0

    thread = server.run_in_background(srv)

    # --- 待ち受けの確認 ------------------------------------------------
    srv.mark_stage("接続を確認中", "connect")
    check = server.diagnose_listening(port, timeout=LISTEN_TIMEOUT_SEC)
    if not check.ok:
        log.error("待ち受けを確認できませんでした")
        print("サーバを起動できませんでした。\n" + check.hint, file=sys.stderr)
        launch_guard.release_lock(mode)
        store.close()
        return 3

    if not args.no_browser:
        _open_browser(srv.url)
    print(f"起動しました: {srv.url}")
    log.info("ブラウザを開きました: %s", srv.url)

    # 統合ツールのブラウザ版(予備)が起こしたときは、ブラウザを開かないが
    # 心拍の見張りは立てる(大きなタブの枠が消えたら終わる。入口が先に落ちても残らない)
    under_portal = os.environ.get("ALLTOOLS_PORTAL", "") == "1"
    code = _serve(args, cfg, store, mode, line, root, srv, thread,
                  grant=grant, access_notice=access_notice,
                  watch_idle=(not args.no_browser) or under_portal)
    # **自分の印だけ**を片付ける(入れ替えのとき、新しいほうの印を消さない)
    launch_guard.release_lock(mode)
    from kanban import trace

    trace.write("startup", "終了")
    applog.close()
    return code


def _serve(args, cfg, store, mode: str, line: str, root: Path, srv, thread,
           *, grant=None, access_notice: str = "", watch_idle: bool = True) -> int:
    """重い初期化 → 本体を差し込む → 止められるまで待つ → 後片付け。

    ブラウザ版(:func:`_run`)とデスクトップ版(:func:`start_bridge`)で共通。
    ``srv`` はもう受け付けを始めている(待機画面が出ている)。
    """
    # --- 重い初期化(待機画面を出したまま) ------------------------------
    srv.mark_stage("アプリを準備中", "prepare")
    gateway = importer = exporter = None
    startup_error = ""
    try:
        gateway, importer, exporter = _prepare_data(
            args, cfg, store, mode, line, srv
        )
    except Exception as exc:  # noqa: BLE001 - 理由を画面へ出して起動は続ける
        log.exception("初期化に失敗しました")
        startup_error = (
            f"初期化に失敗しました: {exc}\n"
            f"ログを確認してください: {applog.log_path()}"
        )

    # --- 本体を差し込む ------------------------------------------------
    def _manual_refresh():
        """画面の「更新」・マスタの修正・担当ラインの変更から呼ばれる。今すぐ取り込む。

        **必ず写し直す。** 共有フォルダは大きさ・更新時刻を数秒覚えて返すので、
        書いた直後だと「変わっていない」と読んで古い写しを使うことがある
        (:meth:`kanban.db.shared.SharedDb.forget_copy`)。
        """
        if importer is None:
            return None
        importer.gateway.forget_copy()
        return importer.run_once()

    def _reconnect(new_path: str):
        """接続先が変わったので繋ぎ直して取り込む。**開き直しは要らない。**

        以前は「開き直してください」と案内していたが、その案内どおりに
        ブラウザを閉じても ``pythonw.exe`` は残り、次の起動が
        「すでに起動しています」で止まった ── **利用者は開き直せなかった。**
        接続先はここで差し替えて、その場で取り込み直す。

        ``Importer``/``Exporter`` は毎回 ``self.gateway`` を見る作りなので、
        属性を差し替えるだけで以後の同期から新しい接続先が使われる
        (スレッドを作り直さない)。
        """
        from kanban.db.shared import SharedDb

        fresh = SharedDb(
            new_path, busy_timeout_ms=cfg.busy_timeout_ms, max_retry=cfg.max_retry
        )
        if importer is not None:
            importer.gateway = fresh
        if exporter is not None:
            exporter.gateway = fresh
        # 心拍も付いてこさせる。放っておくと、もう見ていない共有DBへ
        # 「開いています」を打ち続ける(他所に幽霊が残る)
        presence.set_gateway(fresh)
        srv.app.config["GATEWAY"] = fresh
        log.info("接続先を差し替えました: %s", new_path)
        # 看板履歴の置き場所を決めていなければ、看板マスタと同じフォルダへ付いていく
        history = _set_history(_load_config(args).resolved_history_db_path())
        _prepare_history(fresh, mode, history)
        if importer is None:
            return None
        return importer.run_once()

    def _set_history(path: str):
        """看板履歴の置き場所を差し替える(設定画面で変えたとき・接続先を変えたとき)。"""
        from kanban.db.history import HistoryDb

        fresh = HistoryDb(path, busy_timeout_ms=cfg.busy_timeout_ms, max_retry=cfg.max_retry)
        if exporter is not None:
            exporter.history_gateway = fresh
        log.info("看板履歴の置き場所: %s", path)
        return fresh

    def _change_history(path: str):
        history = _set_history(path)
        if gateway is not None:
            _prepare_history(gateway, mode, history)
        return history

    srv.build(
        store=store,
        line=line,
        on_manual_refresh=_manual_refresh if importer else None,
        on_reconnect=_reconnect,
        # **押した直後に書き戻す。** 周期(export_interval_sec)を待たない。
        # 倉庫の発送が現場に届くまでの時間が縮む(待つのは取り込みの周期だけ)
        on_changed=(lambda _line: exporter.request_now()) if exporter else None,
    )
    # ``--config`` を渡されたときだけ、そのファイル 1 つを読み書きさせる。
    # ふだんはアプリのフォルダの data\config.json(+ 担当ラインは %APPDATA%)
    srv.app.config["CONFIG_PATH"] = args.config or ""
    # 「どれだけ取り込めていなければ異常か」を画面側が決められるようにする
    srv.app.config["IMPORT_INTERVAL_SEC"] = cfg.import_interval_sec if importer else 0
    # 共有フォルダをどれだけ往復しているかを設定画面へ出すため
    srv.app.config["GATEWAY"] = gateway
    # 起動したときに読んだ権限と、権限が無くて別のモードで開いた理由(画面の上に出す)
    srv.app.config["ACCESS_GRANT"] = grant
    srv.app.config["STARTUP_NOTICE"] = access_notice
    # 看板履歴の置き場所を変えたら、送り先を差し替えて表を用意する(開き直さなくてよい)
    srv.app.config["SET_HISTORY_DB"] = _change_history

    # エラーのときに「その時の状態」として記録に添えるもの(なぜなぜの材料)
    from kanban import trace

    def _state() -> dict:
        out: dict = {"接続先": cfg.resolved_shared_db_path()}
        try:
            out["未反映"] = store.pending_count()
            out["要確認"] = len(store.sync_failures())
        except Exception as exc:  # noqa: BLE001
            out["手元のSQLite"] = f"読めない: {exc}"
        for name, task in (("取り込み", importer), ("書き戻し", exporter)):
            result = getattr(task, "last_result", None) if task is not None else None
            if result is not None:
                out[f"最後の{name}"] = (getattr(result, "finished_at", "") or "") + " " + (
                    result.summary() if hasattr(result, "summary") else str(result))[:300]
        return out

    trace.set_state_provider(_state)

    # 「いま止めてよいか」を答える関数を登録する(基盤仕様書 2.8)。
    # 書き戻しの途中で落とすと、どこまで送れたのか分からなくなる
    from app.routes import health as health_routes

    health_routes.set_busy_check(lambda: _busy_reason(store))

    # 画面が居なくなったら終わる(基盤仕様書 2.8 / 2.9)。
    # **これが無いと、タブを閉じてもプロセスが残り続ける。** 窓を持たない
    # ので利用者からは見えず、次の起動が「すでに起動しています」で止まる。
    # デスクトップ版は窓を閉じたら外枠が終わらせるので、見張りは要らない
    if watch_idle:
        from kanban import idle_exit

        idle_exit.install(srv.stop, lambda: _busy_reason(store))
    else:
        log.info("自動終了の見張りは立てません(--no-browser またはデスクトップ版)")

    # 「開いています」と名乗る(VBA の Form状態管理)。
    # 倉庫が確認している最中に現場が開いていれば、看板が出るかもしれないと
    # 身構えられる。**倉庫参照モードは名乗らない**(読むだけの端末を
    # 数えると、本物の倉庫と見分けが付かなくなる)
    if presence.registers(mode) and gateway is not None:
        presence.install(
            gateway, store, presence.registered_line(mode, line)
        )
    else:
        log.info("このモードは状態管理には名乗りません: %s", mode)

    if startup_error:
        srv.mark_error(startup_error)
    else:
        srv.mark_ready(True)

    # --- 止められるまで待つ --------------------------------------------
    try:
        while thread.is_alive():
            thread.join(timeout=0.5)
    except KeyboardInterrupt:
        log.info("Ctrl+C を受け取りました")
        srv.stop()
        thread.join(timeout=5)

    log.info("終了処理に入ります")
    _shutdown(store, importer, exporter)
    return 0


def _prepare_data(args, cfg, store, mode: str, line: str, srv):
    """Access への接続と、定期取り込み・書き戻しを用意する。"""
    from kanban.db import sync
    from kanban.db.shared import SharedDb

    gateway = SharedDb(
        cfg.resolved_shared_db_path(),
        busy_timeout_ms=cfg.busy_timeout_ms,
        max_retry=cfg.max_retry,
    )
    lines = _relevant_lines(mode, line)
    # 倉庫の端末だけが「倉庫に表示」(出した看板が倉庫の画面に初めて出た時刻)を書く
    store.records_seen = mode == config.MODE_WAREHOUSE

    if cfg.import_on_startup and not args.no_import:
        srv.mark_stage("データを取り込み中", "prepare")
        log.info("起動時の取り込みを開始します: lines=%s", lines or "(全部)")
        result = sync.import_all(store, gateway, lines=lines or None)
        log.info("起動時の取り込み: %s", getattr(result, "access_mode", "?"))
    from kanban.db import history as history_db

    history = history_db.open_db(cfg)
    _prepare_history(gateway, mode, history)
    _prepare_access(cfg, mode)

    def current_line() -> str:
        """**いまの**担当ライン。起動したときのラインではない。

        設定画面で担当ラインを変えても、以前はここが起動したときのラインを
        取り込み続けていた ── 変えた先のラインは一度も取り込まれず、看板画面は
        空のまま、マスタで足した看板も出なかった。
        """
        app = getattr(srv, "app", None)
        return (app.config.get("LINE") if app is not None else "") or line

    importer = sync.Importer(
        store, gateway,
        lines=lambda: _relevant_lines(mode, current_line()),
        interval_sec=cfg.import_interval_sec,
    )
    # 出来事(看板履歴)は看板マスタとは別のファイル(看板履歴.sqlite3)へ送る
    exporter = sync.Exporter(store, gateway, interval_sec=cfg.export_interval_sec,
                             history_gateway=history)

    # **倉庫参照モードは書き戻さない。** 状態を変えないモードなので、
    # 送るものがそもそも出ない。走らせておく理由が無い
    if mode != config.MODE_WAREHOUSE_VIEW:
        exporter.start()
    importer.start()
    return gateway, importer, exporter


def _prepare_history(gateway, mode: str, history=None) -> None:
    """看板履歴(看板履歴.sqlite3)と、共有DBの看板コメントを用意する(無ければ作る・足りない列を足す)。

    **最初に誰かが看板を出すまで表が無い**、という状態をなくす。集計タブが
    「まだ記録がありません」と言うのは、本当に記録が無いときだけにする。
    倉庫参照モードは共有フォルダへ書かないので、ここでも書かない。

    看板マスタの ``看板履歴``(以前の置き場所)に残っている行は、看板履歴.sqlite3 に
    **まだ無いものだけ**写す(:func:`kanban.db.history.copy_from_shared`)。
    """
    from kanban.db import history as history_db
    from kanban.db import sync

    if mode == config.MODE_WAREHOUSE_VIEW:
        return
    jobs = [(sync.ensure_comment_table, gateway)]
    if history is not None:
        jobs.insert(0, (sync.ensure_history_table, history))
    for ensure, target in jobs:
        try:
            result = ensure(target)
        except Exception:  # noqa: BLE001 - 起動は止めない(送る前にまた確かめる)
            log.exception("記録用の表を用意できませんでした")
            continue
        if not result.ok:
            log.warning("%s", result.message)
    if history is None or not history.exists():
        return
    try:
        history_db.copy_from_shared(gateway, history)
    except Exception:  # noqa: BLE001 - 写すのは次に起動したときでもよい
        log.exception("看板マスタの看板履歴を看板履歴.sqlite3 へ写せませんでした")


def _prepare_access(cfg, mode: str) -> None:
    """梱包資材マスタに「アクセス権限」の表が無ければ作る(**ほかの表には触らない**)。

    無いままだと、最初の 1 行をどこからも足せない(マスタ管理を開いたときにも作る)。
    倉庫参照モードは共有フォルダへ書かないので、ここでも書かない。
    """
    if mode == config.MODE_WAREHOUSE_VIEW:
        return
    from kanban import access_control

    try:
        db = access_control.open_db(cfg)
        if not db.exists():
            log.info("梱包資材マスタが見つからないので、アクセス権限の表は作りません: %s", db.path)
            return
        ok, message = access_control.ensure_table(db)
        if message:
            (log.info if ok else log.warning)("アクセス権限: %s", message)
    except Exception:  # noqa: BLE001 - 作れなくても起動は続ける
        log.exception("アクセス権限の表を用意できませんでした")


def _relevant_lines(mode: str, line: str) -> list[str]:
    """このモードで取り込むライン。

    現場は担当ラインだけ、倉庫・倉庫参照は倉庫対象のライン全部。無駄な
    取り込みは Access への往復を増やし、他端末を待たせる。
    """
    if mode == config.MODE_SITE:
        return [line] if line else []
    return config.warehouse_line_codes()


def _busy_reason(store) -> str:
    """いま止めてはいけない理由。空文字なら止めてよい。

    **数えるのは「待てば減る」ものだけ**(:meth:`kanban.db.store.Unsent.busy_reason`)。
    諦めた行(共有DB側から消された等で何度も失敗した行)や、置き場所が見えないまま
    何日も経った出来事まで数えると、待っても永久に減らないので**二度と終われなくなる**
    ── タブを閉じてもアプリが残り、次の起動が「すでに起動しています」で止まる。
    諦めた行は「要確認」として画面に出るので、見えなくなるわけではない。
    コメントと出来事も待つ(以前は看板の状態だけを数えていて、書いたコメントが共有へ
    届く前に止まり、相手には次にこの端末を開くまで届かなかった)。
    """
    try:
        return store.unsent().busy_reason()
    except Exception:  # noqa: BLE001 - 判定できないなら止めてよい
        return ""


def _shutdown(store, importer, exporter) -> None:
    """止める。**書き戻しを最後に1回やってから**閉じる。"""
    beat = presence.get()
    if beat is not None:
        try:
            # 閉じたことを先に伝える。届かなくても、打つのをやめれば
            # いずれ古くなって「もう居ない」と分かる
            beat.close()
        except Exception:  # noqa: BLE001 - 付加情報のために終了を止めない
            log.exception("状態管理の後始末でエラー")
    if importer is not None:
        try:
            importer.stop(timeout=5)
        except Exception:  # noqa: BLE001
            log.exception("取り込みの停止でエラー")
    if exporter is not None:
        try:
            # 最後の書き戻しはやってから終わる。押した操作を持ったまま
            # 終了すると、次に起動するまで Access へ届かない
            exporter.stop(final_export=True, timeout=20)
        except Exception:  # noqa: BLE001
            log.exception("書き戻しの停止でエラー")
    try:
        store.close()
    except Exception:  # noqa: BLE001
        log.exception("ストアの終了でエラー")


# ------------------------------------------------------------------
# 小道具
# ------------------------------------------------------------------
def _log_settings(cfg: config.Config, *, echo: bool = False) -> None:
    """どの設定ファイルを読んだかを残す。

    **配った先で「効いていない」と言われたとき、まずここを見る。** ファイルが
    無かったのか、読めなかったのか、読んだのかで、直し方がまったく違う。
    """
    status = config.read_settings_file(Path(cfg.source_path) if cfg.source_path else None)
    if status.error:
        text = f"設定ファイル: 読めませんでした({status.error})"
    elif not status.exists:
        text = f"設定ファイル: なし(組み込みの既定で動きます: {status.path})"
    else:
        keys = "、".join(config.setting_label(k) for k in sorted(status.values)) or "(既定のまま)"
        text = f"設定ファイル: 読み込みました({status.path} / {keys})"
    if status.error:
        log.warning(text)
    else:
        log.info(text)
    if echo:
        print(text)


def _load_config(args) -> config.Config:
    cfg = config.load_config(args.config)
    if args.shared_db:
        cfg.shared_db_path = args.shared_db
    if args.sqlite:
        cfg.sqlite_path = args.sqlite
    if args.line:
        cfg.line = args.line
    if not cfg.sqlite_path:
        # 既定は**ローカル領域**(基盤仕様書 2.7)。共有フォルダに置くと
        # 同期の競合とロック待ちで実用にならない
        cfg.sqlite_path = str(app_config.local_dir("data") / "kanban.sqlite3")
    if not cfg.log_dir:
        cfg.log_dir = str(app_config.local_dir("logs"))
    return cfg


def _open_store(cfg):
    """作業用DBを開く。**壊れていたら脇へよけて、空の作業用DBで続ける。**

    よけないと、起動のたびに同じところで止まり、看板を開けないままになる(古い版の
    設定から引き継いだ置き場所の作業用DBでも同じ)。よけたファイルは消さない
    (``<名前>.broken-<日時>``)。看板は共有DBから取り込み直す。
    """
    from kanban.db.store import is_broken_db_error, set_aside_broken

    path = cfg.resolved_sqlite_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    broken = ""
    try:
        return _store_at(path, cfg)
    except Exception as exc:                      # noqa: BLE001 - 壊れたときだけ受ける
        if not is_broken_db_error(exc):
            raise
        broken = str(exc)
    try:
        moved = set_aside_broken(path)
    except OSError as exc:
        raise StartupError(
            f"作業用DBが壊れています({broken})。脇へよけることもできませんでした: {path}",
            f"ほかに開いている看板の画面・プログラムを閉じてから、もう一度開いてください({exc})",
        ) from None
    log.warning("作業用DBが壊れていたので脇へよけ、空の作業用DBで始めます: %s → %s(%s)"
                " ── 送っていなかった操作は、よけたファイルに残っています", path, moved, broken)
    return _store_at(path, cfg)


def _store_at(path, cfg):
    from kanban.db.store import Store

    store = Store(
        path,
        host_name=cfg.host_name,
        busy_timeout_ms=cfg.busy_timeout_ms,
        max_retry=cfg.max_retry,
    )
    try:
        store.ensure_schema()
    except BaseException:
        store.close()   # 開いたまま投げない(Windows ではよけられない)
        raise
    return store


def _open_browser(url: str) -> None:
    """既定のブラウザで開く。開けなくても起動は続ける。"""

    def _later() -> None:
        time.sleep(0.2)
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - 開けなくても URL は表示済み
            log.warning("ブラウザを開けませんでした。手動で開いてください: %s", url)

    threading.Thread(target=_later, name="open-browser", daemon=True).start()


if __name__ == "__main__":
    sys.exit(main())
