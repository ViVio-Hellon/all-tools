"""起動の開始点 (基盤仕様書 2.1 / 2.5)

`Start.vbs`(通常)と `start.bat`(診断)の両方がここへ来る。順に:

    1. 実行環境の確認   … Python の版・必須パッケージ・書き込み権限
    2. 多重起動の判定   … `launch_guard`
    3. 待ち受け開始     … 待機画面だけの小さいサーバ
    4. ブラウザを開く
    5. 本体を組み立てて差し替え → 重い初期化

**アプリ本体を読み込む前に環境を確認する。** 先に読み込むと、
パッケージが足りない場合の失敗が ImportError のトレースバックになり、
利用者には何をすればよいか分からない。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import webbrowser
from pathlib import Path
from typing import Optional

APP_ROOT = Path(__file__).resolve().parent
# 自分の隣を import できるようにする(`pip install -e .` を要らなくする)。
# 配布はフォルダごとコピーなので、インストール手順を増やさない
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

# 待機画面が出るまでにかかった時間を測る起点
_BOOT_AT = time.monotonic()

# 動かせる最低の Python。3.9 は dict の `|` などを使っていないので通る
MIN_PYTHON = (3, 9)

# 無いと動かないパッケージ。(import名, pip名)
REQUIRED_PACKAGES = (("flask", "Flask"), ("waitress", "waitress"))

# デスクトップ版(`bridge.py`)は待ち受けないので waitress は要らない
BRIDGE_PACKAGES = (("flask", "Flask"),)

# 待ち受けの確認にかける上限(秒)
LISTEN_TIMEOUT_SEC = 15


class StartupError(RuntimeError):
    """起動できない理由と、**次に何をすればよいか**。"""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


# ------------------------------------------------------------------
# 1. 実行環境の確認
# ------------------------------------------------------------------
def check_python_version() -> None:
    if sys.version_info < MIN_PYTHON:
        raise StartupError(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 以上が必要です"
            f"(いまは {sys.version.split()[0]})",
            "https://www.python.org/downloads/ から新しいPythonを入れてください。")


def check_packages(packages=None) -> None:
    """必須パッケージの有無。**入れ方まで示す**(基盤仕様書 ステップ5)。

    既定(`None`)はブラウザ版の一覧。呼ぶたびに引く(定義のときに決めない)。
    """
    import importlib.util

    if packages is None:
        packages = REQUIRED_PACKAGES
    missing = [pip_name for module, pip_name in packages
               if importlib.util.find_spec(module) is None]
    if missing:
        raise StartupError(
            f"必要なパッケージが入っていません: {', '.join(missing)}",
            f"コマンドプロンプトで次を実行してください:\n"
            f"    {console_python()} -m pip install -r requirements.txt")


def console_python() -> str:
    """`pip` を実行するときに使うPythonの名前。

    `Start.vbs` は画面を出さないために **pythonw.exe** で起動する。
    そのまま `sys.executable` を案内すると `pythonw.exe -m pip install ...`
    と出るが、pythonw には画面が無いので**実行しても何も表示されない**
    (成否すら分からない)。案内するときは必ずコンソール側の `python.exe`
    に読み替える。
    """
    # Windowsのパスを他のOSで扱うと `Path` が `\` を区切りとみなさない。
    # 案内文を組み立てるだけなので、両方の区切りで切っておく
    name = sys.executable.replace("\\", "/").rsplit("/", 1)[-1]
    if name.lower().startswith("pythonw"):
        return "python" + name[len("pythonw"):]
    return name


def should_abort(check) -> bool:
    """起動確認が取れなかったとき、起動そのものを中止するか。

    中止するのは **TCPでも繋がらないとき**だけ。TCPが通っているなら
    サーバ自身は待ち受けており、応答を取れないのはこちら側の確認経路の
    都合(プロキシ・セキュリティ製品)であることが多い。ブラウザは
    ローカルアドレスをプロキシから除外するのが普通なので、そのまま
    開けばつながる。
    """
    return not check.ok and not check.tcp_ok


def describe_loopback() -> str:
    """自分自身への通信まわりの状態(診断用)。

    「サーバは起動しているのに画面が出ない」の原因はここに集まる。
    社内PCではプロキシ設定に 127.0.0.1 の除外が無いことがあり、
    **自分自身への通信までプロキシへ送られて失敗する**。
    """
    import launch_guard

    lines = ["", "--- 自分自身への通信 ---"]
    proxies = launch_guard.proxy_settings()
    if proxies:
        lines.append("プロキシ設定  : "
                     + ", ".join(f"{k}={v}" for k, v in sorted(proxies.items())))
        lines.append("  ※このアプリはプロキシを経由せずに 127.0.0.1 へ接続します。")
        lines.append("    ブラウザ側でも 127.0.0.1 / localhost が除外されているか"
                     "確認してください。")
    else:
        lines.append("プロキシ設定  : なし")
    return "\n".join(lines)


def check_writable() -> Path:
    """ローカル領域を作れるか(基盤仕様書 2.7)。"""
    from nippou import app_config

    try:
        root = app_config.ensure_local_dirs()
    except OSError as exc:
        raise StartupError(
            f"作業用フォルダを作れません: {exc}",
            "書き込みの権限があるか、ディスクの空きがあるか確認してください。") from None

    probe = root / "runtime" / ".write-test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise StartupError(
            f"作業用フォルダに書き込めません: {root}", f"{exc}") from None
    return root


def check_config() -> None:
    """アプリ固有値が読めているか。読めなくても既定値で動くが、記録は残す。"""
    from nippou import app_config

    error = app_config.load_error()
    if error:
        log().warning("%s — 既定値で起動します", error)
    problem = app_config.version_problem()
    if problem:
        log().warning("%s", problem)


def run_environment_checks(*, bridge: bool = False) -> Path:
    """順に確認する。落ちたところで理由が分かるように分けてある。"""
    check_python_version()
    check_packages(BRIDGE_PACKAGES if bridge else None)
    root = check_writable()
    check_config()
    return root


# ------------------------------------------------------------------
# ログ
# ------------------------------------------------------------------
_log = None


def log():
    """起動入口のログ(基盤仕様書 2.6 の `launcher.log`)。

    業務ログ(`nippou.log`)とは別のファイルにする ── 「起動前に失敗
    したのか」「業務処理で失敗したのか」を切り分けられるようにするため。
    """
    global _log
    if _log is None:
        from nippou.logging_setup import get_launch_logger
        _log = get_launch_logger("launcher", filename="launcher.log")
    return _log


def log_environment() -> None:
    """起動のたびに残す1枚(基盤仕様書 2.6)。"""
    from nippou import app_config

    log().info("=" * 60)
    log().info("起動: pid=%s", os.getpid())
    log().info("Python: %s (%s)", sys.version.split()[0], sys.executable)
    log().info("アプリ本体: %s", APP_ROOT)
    log().info("ローカル領域: %s", app_config.local_root())
    log().info("版: %s", app_config.version())


# ------------------------------------------------------------------
# 2. 起動
# ------------------------------------------------------------------
def start(*, open_browser: bool = True) -> int:
    """戻り値はプロセスの終了コード。

    【順番が要点】
    待機画面より前に置くものを、**できるだけ減らしてある**。先に Flask と
    アプリ本体を組み立ててからブラウザを開くと、いちばん重い import を
    「待たせるために見せるもの」より先にやることになる ── 出るころには
    待つ理由がほぼ終わっている。

        ポートを決める → 待ち受け開始(待機画面だけ)→ ブラウザ
            → 本体を組み立てて差し替え → 重い初期化

    最初の3つは標準ライブラリと `app_config` だけで済む。
    """
    import launch_guard
    import server as server_module
    from nippou import app_config

    log_environment()

    # --- デスクトップ版(統合ツールの窓)が動いていれば、こちらが止まる ---
    #
    # 同じ手元の SQLite と設定を、2つのプロセスが書きに行かないため。
    # **後から開いたほうが止まる**(統合ツールの決まり。docs/統合_事前確認.md)
    if launch_guard.desktop_running():
        log().warning("デスクトップ版が動いているので、ブラウザ版は起動しません")
        raise StartupError(DESKTOP_RUNNING_MESSAGE, DESKTOP_RUNNING_HINT)

    # --- 多重起動の判定 (基盤仕様書 2.4) ---
    guard = launch_guard.check_existing()
    if not guard.should_start:
        return _join_or_explain(guard, open_browser=open_browser)
    log().info("多重起動の判定: %s", guard.reason)

    # --- 起動することを、ポートより先に名乗る ---
    #
    # 【ここを後ろに置いていたので、2つ立ちました】
    # 印(ロック)は待ち受けが始まってから書いていました。判定から
    # そこまでは1秒以上あり、その間にもう一度起動すると、2つとも
    # 「誰も居ない」を見て2つとも進みます。しかも `pick_port()` は
    # 塞がった番号を避けて**隣へずれる**ので、ぶつかって落ちることも
    # なく、8733 と 8734 で静かに2つ動きます。
    #
    # ポートはまだ決まっていないので 0 で名乗り、決まってから書き足す
    # (`update_lock`)。名乗れなかった = ほんの一瞬先に誰かが名乗った、
    # なので、もう一度判定して**そちらへ合流**します。
    if not launch_guard.claim_lock(launch_guard.build_lock_info(0)):
        log().info("ほぼ同時にもう1つ起動されました。先の1つへ合流します")
        return _join_or_explain(launch_guard.check_existing(),
                                open_browser=open_browser)
    # 名乗ってから、デスクトップ版をもう一度見る(調べてから名乗るまでの間に
    # 統合ツールの窓で日報が開いていたら、こちらが止まる。デスクトップ版は
    # 自分の錠を取ってからこの印を見るので、少なくとも一方が相手に気づく)
    if launch_guard.desktop_running():
        launch_guard.release_lock()
        log().warning("デスクトップ版が動いているので、ブラウザ版は起動しません")
        raise StartupError(DESKTOP_RUNNING_MESSAGE, DESKTOP_RUNNING_HINT)

    # --- ポート選び ---
    port = launch_guard.pick_port()
    if port is None:
        launch_guard.release_lock()
        # **自分自身が塞いでいたなら、それは起動失敗ではなく合流です。**
        # ここを「ポートがありません」で終わらせると、押した人は
        # 「壊れた」と受け取ります(実際は動いているものがある)
        running = launch_guard.find_running()
        if running:
            log().info("ポートは自分自身が使っていました。合流します: %s",
                       [r.port for r in running])
            return _join_or_explain(launch_guard.check_existing(),
                                    open_browser=open_browser)
        candidates = app_config.port_candidates()
        raise StartupError(
            f"使えるポートがありません(試した番号: {candidates})",
            "他のアプリが使っている可能性があります。"
            "config/app.json の port を変えるか、そのアプリを終了してください。")

    # --- 待ち受けを始める(この時点ではまだ待機画面だけ) ---
    srv = server_module.AppServer(port)
    thread = server_module.run_in_background(srv)

    check = server_module.diagnose_listening(port, timeout=LISTEN_TIMEOUT_SEC)
    if should_abort(check):
        launch_guard.release_lock()
        raise StartupError(
            "サーバを起動できませんでした",
            f"{check.hint}\n\nログ: {app_config.local_dir('logs')}")
    if not check.ok:
        log().warning("起動確認の応答を取れませんでしたが、待ち受けは"
                      "できているので続行します:\n%s", check.hint)
        print("[注意] 起動の確認応答を取れませんでした。"
              "画面が出ない場合は次を確認してください:")
        print(check.hint)

    # 決まったポートとトークンを、先に立てた印へ書き足す。
    # **ここで初めて `stop.bat` と次の起動が声を掛けられるようになる**
    launch_guard.update_lock(port=port, token=srv.token, url=srv.url)

    try:
        # --- ブラウザを開く ---
        # **ここまでが最短**。待機画面はもう出せる状態で、本体の
        # 組み立ては次の行から始まる
        if open_browser:
            log().info("ブラウザを開きます: %s", srv.url)
            webbrowser.open(srv.url)
        else:
            print(f"起動しました: {srv.url}")
        log().info("待機画面まで %.2f秒", time.monotonic() - _BOOT_AT)

        # --- 本体を組み立てて差し替える ---
        srv.mark_stage("アプリを準備中", "prepare")
        try:
            srv.build()
        except Exception as exc:                  # noqa: BLE001 - 画面に出して継続
            log().exception("アプリを組み立てられませんでした")
            srv.boot.mark_error(f"アプリを組み立てられませんでした: {exc}")
            _hold_until_stopped(srv, thread)
            return 1

        _initialize(srv)

        # --- 待ち受けが終わるまでここで止まる ---
        _hold_until_stopped(srv, thread)
        return 0
    finally:
        # **自分が立てた印だけ**を片付ける。無条件に消すと、入れ替えの
        # ときに古いほうの後始末が新しいほうの印を消してしまい、次の
        # 起動が「誰も居ない」と読んで2つ目を立てます
        launch_guard.release_lock()
        log().info("終了しました")


# デスクトップ版(統合ツール)とブラウザ版は同時に動かさない。断るときの文言
DESKTOP_RUNNING_MESSAGE = "日報管理ツールはデスクトップ版(統合ツールの窓)で動いています"
DESKTOP_RUNNING_HINT = ("統合ツールの窓の「日報」のタブをお使いください。"
                        "ブラウザ版で開くときは、統合ツールの窓を閉じてからにしてください。")
BROWSER_RUNNING_MESSAGE = "日報管理ツールのブラウザ版が動いています"
BROWSER_RUNNING_HINT = ("ブラウザ版とデスクトップ版は同時には使えません。"
                        "ブラウザの画面の「終了」で閉じてから、このタブの「もう一度開く」を押してください。")


def start_bridge(*, token: str = "", server_factory) -> int:
    """デスクトップ版の起動(`bridge.py` から)。**ポートもロックも使わない。**

    多重起動の防止・窓・終了は外枠(統合ツールの Rust/Tauri)が持つ。ここでするのは
    ブラウザ版と同じ「待機画面 → 本体を組み立てる → 重い初期化」だけで、その中身
    (`_initialize`)は共有する ── 2本持つと片方だけ直すことになる。

    ブラウザ版とは同時に動かさない: ブラウザ版が先に動いていれば**こちらが止まる**。
    動き始めたら、ブラウザ版が見る錠(`runtime/desktop.lock`)を握り続ける。
    """
    import secrets

    import launch_guard
    import server as server_module

    log_environment()
    # **自分の錠を取ってから**相手を見る(同時に開いても、少なくとも一方が気づく)
    if not launch_guard.hold_desktop_lock():
        raise StartupError("日報管理ツールがほかの窓で動いています",
                           "開いている窓をお使いください。")
    running = launch_guard.browser_running()
    if running is not None:
        launch_guard.release_desktop_lock()
        log().warning("ブラウザ版が動いているので、デスクトップ版は起動しません: "
                      "pid=%s port=%s", running.pid, running.port)
        raise StartupError(BROWSER_RUNNING_MESSAGE, BROWSER_RUNNING_HINT)

    srv = server_factory(token or secrets.token_urlsafe(24))
    thread = server_module.run_in_background(srv)
    log().info("待機画面まで %.2f秒(デスクトップ版)", time.monotonic() - _BOOT_AT)
    srv.mark_stage("アプリを準備中", "prepare")
    try:
        srv.build()
    except Exception as exc:                      # noqa: BLE001 - 画面に出して継続
        log().exception("アプリを組み立てられませんでした")
        srv.boot.mark_error(f"アプリを組み立てられませんでした: {exc}")
        _hold_until_stopped(srv, thread)
        return 1
    # 窓を閉じたら外枠が終わらせるので、心拍による自動終了は使わない
    _initialize(srv, watch_idle=False)
    _hold_until_stopped(srv, thread)
    log().info("終了しました(デスクトップ版)")
    return 0


# 停止を頼んでから、受付の輪が終わるのを待つ上限(秒)。ここを過ぎたら
# **確実に落とす** ── 残ったプロセスは次回の起動で「すでに起動しています」
# と判定され、入れ替えた新しい版が動かない
EXIT_WAIT_SEC = 6.0


def _hold_until_stopped(srv, thread) -> None:
    """待ち受けが終わるまで止まる。**終わらなくても必ず抜ける。**

    waitress の受付の輪は、つながったままの接続が1本でもあると回り続ける
    (`server.AppServer.stop` の説明)。そちらは直してあるが、**ここでも
    受け止めておく** ── 止まらないことの害が大きすぎるため。
    """
    while thread.is_alive():
        thread.join(timeout=0.5)
        if not srv.stop_requested:
            continue
        # 停止を頼んである。ここからは上限つきで待つ
        thread.join(timeout=EXIT_WAIT_SEC)
        if thread.is_alive():
            log().warning("待ち受けが %.0f秒 で終わらないので、"
                          "プロセスを終了します", EXIT_WAIT_SEC)
            _hard_exit()
        return


def _hard_exit() -> None:
    """後始末をしてから、確実に落とす。

    `sys.exit()` は例外なので、受付の輪が回っている別スレッドには効かない。
    `os._exit()` は後始末をしないので、**残すべきものは先に書いてから**
    呼ぶ(ログは追記なので閉じなくても失われない)。
    """
    import logging

    logging.shutdown()
    os._exit(0)


def _open_local_db(path, connect) -> None:
    """作業用DBを1度開く。**壊れていたら脇へよけて、空の作業用DBで続ける。**

    よけないと、起動のたびに同じところで止まり、日報を打てないままになる。
    よけたファイルは消さない(`<名前>.broken-<日時>`)。この端末の控え(共有へ
    保存した分)はこのあと `_restore_from_backup` が戻す。
    """
    from nippou.db.connection import is_broken_db_error, set_aside_broken

    broken = ""
    try:
        connect(path).close()
        return
    except Exception as exc:                      # noqa: BLE001 - 壊れたときだけ受ける
        if not is_broken_db_error(exc):
            raise
        broken = str(exc)
    try:
        moved = set_aside_broken(path)
    except OSError as exc:
        raise RuntimeError(f"作業用DBが壊れています({broken})。脇へよけることもできませんでした: "
                           f"{path} ── ほかに開いている日報の画面・プログラムを閉じてから、"
                           f"もう一度開いてください({exc})") from None
    log().warning("作業用DBが壊れていたので脇へよけ、空の作業用DBで始めます: %s → %s(%s)",
                  path, moved, broken)
    _note_event(f"作業用DBが壊れていたので脇へよけました({broken})。空の作業用DBで始めます。"
                f"よけたファイル: {moved}")
    connect(path).close()


def _initialize(srv, *, watch_idle: bool = True) -> None:
    """重い初期化。サーバが立ってから行う。

    ここで失敗しても**サーバは落とさない**。落とすと利用者のブラウザには
    「接続できません」としか出ず、理由が伝わらない。画面に理由を出す。
    """
    srv.mark_stage("接続を確認中", "connect")
    try:
        from nippou.config import SETTINGS
        from nippou.db.connection import connect
        from nippou.logging_setup import init_logging

        init_logging()
        # 出来事の記録にも「起動した」を残す ── なぜなぜの経過で、どこから
        # どこまでが1回の起動なのかを見分けるため
        from nippou import app_config as _app_config
        _note_event(f"起動しました(版 {_app_config.version()})")
        # スキーマの用意。`connect()` が `ensure_schema()` まで面倒を見る。
        # ここで1度開いておくと、最初の画面表示で待たされない
        _open_local_db(SETTINGS.sqlite_path, connect)
        log().info("ローカルDBを確認しました: %s", SETTINGS.sqlite_path)
    except Exception as exc:                      # noqa: BLE001 - 画面に出して継続
        log().exception("初期化に失敗しました")
        srv.mark_error(f"ローカルDBを用意できませんでした: {exc}")
        _note_event(f"起動の途中で止まりました: ローカルDBを用意できませんでした({exc})",
                    error=exc)
        return

    # **直の時間より先に。** 参照用マスタの置き場所が配布先から来ることがある
    _apply_distribution(srv)
    _read_shift_times(srv)
    # **ラインを決めてから控えを戻す**(戻すのはこの端末のラインのぶん)
    _read_access_rights(srv)
    _restore_from_backup(srv)
    _watch_for_idle(srv, watch_idle=watch_idle)
    srv.mark_ready(True)
    log().info("起動完了まで %.2f秒", time.monotonic() - _BOOT_AT)


def _note_event(message: str, error: Exception | None = None) -> None:
    """出来事の記録へ1件。**書けなくても起動は止めない。**"""
    try:
        from nippou.logic import event_log as rule
        from nippou.services import event_log

        fields = event_log.describe_exception(error) if error is not None else {}
        event_log.note(message, kind=rule.KIND_ERROR if error else rule.KIND_INFO,
                       label="起動", **fields)
    except Exception:                             # noqa: BLE001 - 記録できないだけ
        pass


def _apply_distribution(srv) -> None:
    """**配布設定があれば、中の設定をこの端末へ読み込む。**

        起動時に配布先フォルダがある場合は配布先フォルダ内部を読み込む
        既存データがある場合はそちらが優先とする

    起動用の Start.vbs と同じフォルダの `配布設定\\` を見ます
    (`distribution.apply_on_start`、python-web-tools と同じ)。この端末で
    すでに決めてある項目は触りません。**直の時間を読むより先に**読みます
    ── 参照用マスタの置き場所が配布設定から来るので、後にすると既定の
    場所を見てしまいます。

    **読めなくても起動は続けます** ── 配布設定が壊れていることより、日報が
    打てないことのほうが重いので。何を読んだか・残したかは記録に1行残し
    ます(配った先で「効いていない」と言われたとき、まず見るのがここ)。
    """
    srv.mark_stage("配布設定を確認中", "connect")
    try:
        from nippou import distribution

        log().info("%s", distribution.describe())
        result = distribution.apply_on_start()
        if result.error:
            log().warning("%s", result.as_text())
        elif result.applied or result.kept:
            log().info("%s", result.as_text())
    except Exception:                             # noqa: BLE001 - 起動は続ける
        log().exception("配布設定の読み込みで予期しないエラー(起動は続けます)")


def _read_shift_times(srv) -> None:
    """直の時間を、**起動のたびにマスタから読み直す。**

        起動時に時間マスタが読めてない　なぜ

    読みに行っていなかったからです。取り込みは「時間マスタを取り込む」を
    **押したときだけ**走り、結果を手元のDBに残す作りでした。押し忘れれば
    控えの時刻(既定値)のまま動きます ── 催促も自動確定も最終時間チェックも
    ぜんぶその時刻で動くのに、押した覚えのある人しか気づけません。

    直の時刻は**上流(伝送用ファイル)が持っている値**で、こちらが決める
    ものではありません。ならば起動のたびに読み直すのが素直です。

    **読めなくても起動は続けます。** 共有が落ちている日に日報が打てなく
    なるほうが重いので ── そのときは手元の値(前回読めたぶん、無ければ
    控え)のまま動き、画面の帯が「マスタから読めていません」と言い続けます。
    """
    srv.mark_stage("直の時間を読み込み中", "connect")
    try:
        from nippou.config import SETTINGS
        from nippou.db.connection import connect
        from nippou.db.repository import NippouRepository
        from nippou.services import shift_times

        # 写し方は3か所(起動・取り込むボタン・マスタ管理)で同じ
        conn = connect(SETTINGS.sqlite_path)
        try:
            shift_times.reload(NippouRepository(conn))
        finally:
            conn.close()
    except Exception:                             # noqa: BLE001 - 起動は続ける
        log().exception("直の時間の読み込みで予期しないエラー(起動は続けます)")


def _read_access_rights(srv) -> None:
    """マスタの「アクセス権限」の表から、この端末のラインと管理者を決める(v4.12.0)。

    **読めなくても起動は続けます**(これまでどおり設定・管理者でラインを選ぶ)。
    """
    srv.mark_stage("アクセス権限を確認中", "connect")
    try:
        from nippou.services import access_rights

        access_rights.apply()
    except Exception:                             # noqa: BLE001 - 起動は続ける
        log().exception("アクセス権限の読み込みで予期しないエラー(起動は続けます)")


def _restore_from_backup(srv) -> None:
    """控え(LocalBackup)にあって手元に無い・手元より新しいページを戻す(v4.12.0)。

        ローカルになければパスを見る

    管理者が別のPCで直したぶんも、ここで手元に入ります。あわせて、
    前の起動で写せなかったページを控えへ写します。**どちらも届かなくても
    起動は続けます。**
    """
    srv.mark_stage("控え(LocalBackup)を確認中", "connect")
    try:
        from nippou import work_context
        from nippou.config import SETTINGS
        from nippou.db.connection import connect
        from nippou.db.repository import NippouRepository
        from nippou.services import local_backup

        conn = connect(SETTINGS.sqlite_path)
        try:
            repo = NippouRepository(conn)
            line = work_context.get_context().terminal_line
            if work_context.terminal_line_decided():
                result = local_backup.pull(repo, line)
                if result.pulled:
                    log().info("%s", result.message)
            local_backup.flush(repo)
        finally:
            conn.close()
    except Exception:                             # noqa: BLE001 - 起動は続ける
        log().exception("控え(LocalBackup)の確認で予期しないエラー(起動は続けます)")


def _watch_for_idle(srv, *, watch_idle: bool = True) -> None:
    """画面が居なくなったら止める見張りを立てる(基盤仕様書 2.8)。

    止め方は `/api/shutdown` と同じ道(`srv.stop`)を通す ── 止め方が
    2つあると、片方だけ直した状態を作ってしまう。処理中かどうかの判断も
    同じもの(`nippou/running.py`)を使う。

    **先にスリープを数えない時計を立てる**(`nippou/awake_clock.py`)。
    Windows ではスリープ中も時計が進むので、立てずにおくとフタを開けた
    瞬間に「90秒 心拍がありません」で終わる。

    デスクトップ版(`watch_idle=False`)では見張りを立てない ── 窓を閉じたら
    外枠が終わらせる。**時計は立てる**(タブの取り合い `tab_lock` も使う)。
    """
    from nippou import awake_clock, idle_exit, running

    awake_clock.start()
    if watch_idle:
        idle_exit.install(srv.stop, running.busy)


# ------------------------------------------------------------------
# 3. すでに起動していたとき
# ------------------------------------------------------------------
def _join_or_explain(guard, *, open_browser: bool) -> int:
    """すでに動いているものがある。**合流するか、居場所と止め方を出す。**

    2つの場面がある。

        合流できる … 同じ版が動いている。**それでよい** ── そのまま
                     ブラウザを開く。押した人にとっては起動したのと同じ
        合流できない … 古い版が残っていて、終わらせようとしたが終わらない。
                     このとき**断るだけで終わらせない**

    後者を黙って諦めると、押した人には「何も起きない」としか見えません。
    入れ替えたはずの新しい版はいつまでも動かず、古い画面が開くだけです。
    そこで、**何が動いているのか(版・PID・ポート・起動時刻・場所)と、
    どうすれば止まるのか**を書いて出します。
    """
    if guard.hung:
        # 印は立っていてプロセスも生きているのに、応答が無い。
        # **ここで立ててしまうと2つ動きます** ── 片方で打った日報は
        # もう片方の画面に出ないので、「保存したのに消えた」に見えます
        log().warning("応答しない起動が残っています: %s", guard.reason)
        for line in _hung_report(guard):
            print(line)
        return 1

    if not guard.stale_version:
        # ふつうの合流。同じ版が動いているので、これでよい
        log().info("既存のインスタンスに合流します: %s", guard.url)
        print(f"すでに起動しています。ブラウザを開きます: {guard.url}")
        # **もう1つ以上動いていたら、黙って合流しない。**
        # 印は1つしか指せないので、残りは誰にも気づかれません ──
        # 「合流しました」と出ているのに2つ動いている、がこれでした
        if guard.extras:
            for line in _extras_report(guard):
                print(line)
        if open_browser and guard.url:
            webbrowser.open(guard.url)
        return 0

    log().warning("合流できません: %s (実行中=%s / これから=%s)",
                  guard.reason, guard.stale_version, _version())
    for line in _stale_report(guard):
        print(line)
    if open_browser:
        try:
            page = _write_conflict_page(guard)
            webbrowser.open(page.as_uri())
        except Exception as exc:                      # noqa: BLE001 - 報告で失敗しない
            print(f"(案内を出せませんでした: {exc})", file=sys.stderr)
    # **0 では返さない。** 起動しなかったことは、呼んだ側にも伝える
    return 1


def _version() -> str:
    from nippou import app_config

    return app_config.version()


def _stale_report(guard) -> list[str]:
    """コンソール向けの同じ内容。`start.bat` から起動した人はこれを読む。"""
    info = guard.existing
    lines = ["", "[起動できません] すでに別の版が動いていて、終了させられませんでした。", ""]
    lines.append(f"  理由        : {guard.reason}")
    lines.append(f"  動いている版: {guard.stale_version}")
    lines.append(f"  これから出す版: {_version()}")
    if info is not None:
        lines.append(f"  プロセス    : PID {info.pid} / ポート {info.port}")
        lines.append(f"  起動した時刻: {info.started_text}")
        lines.append(f"  そのアプリの場所: {info.app_root or '(不明)'}")
        lines.append(f"  画面        : {info.url}")
    lines += ["", "  止め方(上から順に試してください):",
              "    1. その画面を開いて右上の「終了」を押す",
              "    2. stop.bat を実行する",
              "    3. stop.bat --force を実行する(処理中でも止める)", ""]
    return lines


def _extras_report(guard) -> list[str]:
    """**すでに多重起動しています。** 何がどこで動いているかを出す。

    印(ロック)には1つぶんしか書けないので、残りは画面にもログにも
    出ないまま動き続けます ── 片方で打った日報はもう片方に出ないので、
    気づくのは翌日「打ったはずの直が無い」という形です。
    """
    lines = ["", "[注意] このアプリが **2つ以上** 動いています。", ""]
    here = guard.existing
    if here is not None:
        lines.append(f"  いま開いたもの: ポート {here.port} / PID {here.pid}")
    for extra in guard.extras:
        lines.append(f"  もう1つ      : ポート {extra.port} / PID {extra.pid}"
                     f" / 版 {extra.version or '(不明)'}")
    lines += ["",
              "  **打った日報が片方にしか入りません。**",
              "  片方を止めてください:",
              "    stop.bat を実行する(全部止めます)",
              "  そのあと start.bat で開き直してください。", ""]
    return lines


def _hung_report(guard) -> list[str]:
    """応答しない起動が残っているときの案内。

    **黙って2つ目を立てない。** 立てれば画面は出ますが、日報の入り先が
    2つに分かれます ── 打ったものが次に開いた画面に無い、という形で
    後から効いてきます。止まっていることは見えますが、分かれたことは
    見えません。
    """
    import launch_guard

    info = guard.existing
    lines = ["", "[起動できません] 前に起動したものがまだ終わっていません。", ""]
    lines.append(f"  理由        : {guard.reason}")
    if info is not None:
        port = info.port or "(まだ決まっていません)"
        lines.append(f"  プロセス    : PID {info.pid} / ポート {port}")
        lines.append(f"  起動した時刻: {info.started_text}")
        lines.append(f"  そのアプリの場所: {info.app_root or '(不明)'}")
    lines += ["",
              "  起動の途中かもしれません。**30秒ほど置いてから**もう一度",
              "  実行してください。それでも同じなら、次の順で止めます:",
              "    1. stop.bat を実行する",
              "    2. stop.bat --force を実行する(処理中でも止める)",
              f"    3. それでも残るときは、この印を消す: {launch_guard.lock_path()}",
              ""]
    return lines


def _write_conflict_page(guard) -> Path:
    """居場所と止め方を書いたHTMLを出す。

    `Start.vbs` はコンソールを出さないので、**ブラウザに出さないと
    誰にも届きません**(`report_failure` と同じ理由)。
    """
    import html
    import tempfile

    info = guard.existing
    try:
        from nippou import app_config
        target = app_config.local_dir("work") / "多重起動.html"
        target.parent.mkdir(parents=True, exist_ok=True)
    except Exception:                                 # noqa: BLE001
        target = Path(tempfile.gettempdir()) / "nippou_多重起動.html"

    def row(label: str, value: str) -> str:
        return (f"<tr><th>{html.escape(label)}</th>"
                f"<td><code>{html.escape(value)}</code></td></tr>")

    rows = [row("動いている版", guard.stale_version),
            row("これから出す版", _version())]
    if info is not None:
        rows += [row("プロセス", f"PID {info.pid} / ポート {info.port}"),
                 row("起動した時刻", info.started_text),
                 row("そのアプリの場所", info.app_root or "(不明)")]

    open_link = ""
    if info is not None and info.url:
        open_link = (f'<p><a class="open" href="{html.escape(info.url)}">'
                     "いま動いているほうを開く</a></p>")

    target.write_text(f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<title>すでに起動しています</title>
<style>
 body{{margin:0;min-height:100vh;display:grid;place-items:center;
       background:#eef1f5;color:#101720;
       font-family:system-ui,"Yu Gothic UI","Meiryo UI",sans-serif;line-height:1.7}}
 .box{{width:min(620px,calc(100vw - 48px));background:#fff;border:1px solid #c9d2dc;
       border-radius:4px;padding:32px;box-shadow:0 6px 20px rgba(16,23,32,.08)}}
 h1{{margin:0 0 4px;font-size:19px;color:#8a6100}}
 .why{{margin:12px 0 20px;padding:14px;background:#fff6e0;border-left:4px solid #d79b00;
       border-radius:0 3px 3px 0}}
 h2{{font-size:14px;margin:24px 0 8px;color:#556171}}
 table{{border-collapse:collapse;width:100%}}
 th{{text-align:left;font-weight:400;color:#556171;font-size:13px;
     padding:5px 12px 5px 0;white-space:nowrap;vertical-align:top}}
 td{{padding:5px 0}}
 code{{font-family:ui-monospace,Consolas,monospace;font-size:13px;
       background:rgba(0,0,0,.06);padding:2px 5px;border-radius:2px;word-break:break-all}}
 ol{{margin:8px 0;padding-left:22px}} li{{margin:6px 0}}
 .open{{display:inline-block;margin-top:8px;padding:8px 16px;background:#2f7d5f;
        color:#fff;text-decoration:none;border-radius:3px}}
</style></head>
<body><main class="box">
<h1>すでに起動しています</h1>
<div class="why">
  <strong>古い版が動いていて、終了させられませんでした。</strong><br>
  そのまま開くと<b>古いほうの画面</b>が出ます。入れ替えた新しい版を使うには、
  下の手順で動いているほうを止めてから、もう一度起動してください。<br>
  <small>{html.escape(guard.reason)}</small>
</div>

<h2>いま動いているもの</h2>
<table>{''.join(rows)}</table>

<h2>止め方(上から順に)</h2>
<ol>
  <li>いま動いている画面を開いて、右上の<b>「終了」</b>を押す</li>
  <li><code>stop.bat</code> を実行する</li>
  <li><code>stop.bat --force</code> を実行する(処理中でも止める)</li>
</ol>
{open_link}
</main></body></html>
""", encoding="utf-8")
    return target


# ------------------------------------------------------------------
# 4. 失敗の伝え方
# ------------------------------------------------------------------
def report_failure(error: StartupError, *, open_browser: bool) -> None:
    """コンソールとブラウザの両方に出す。

    `Start.vbs`(コンソール非表示)で起動された場合、標準出力は誰にも
    見えない。基盤仕様書 2.2 の「起動できない場合は、確認すべきログの
    場所を表示します」を満たすため、HTMLを書いてブラウザで開く。
    """
    print(f"\n[エラー] {error}", file=sys.stderr)
    if error.hint:
        print(error.hint, file=sys.stderr)

    try:
        from nippou import app_config
        log_dir = str(app_config.local_dir("logs"))
    except Exception:                             # noqa: BLE001 - 報告で失敗しない
        log_dir = "(ローカル領域を特定できませんでした)"

    try:
        log().error("起動に失敗: %s / %s", error, error.hint)
    except Exception:                             # noqa: BLE001
        pass

    if not open_browser:
        return
    try:
        path = _write_error_page(str(error), error.hint, log_dir)
        webbrowser.open(path.as_uri())
    except Exception as exc:                      # noqa: BLE001
        print(f"(エラー画面を出せませんでした: {exc})", file=sys.stderr)


def _write_error_page(message: str, hint: str, log_dir: str) -> Path:
    """起動に失敗したことを伝えるHTMLを一時領域に書く。"""
    import html
    import tempfile

    try:
        from nippou import app_config
        target = app_config.local_dir("work") / "起動エラー.html"
        target.parent.mkdir(parents=True, exist_ok=True)
    except Exception:                             # noqa: BLE001
        target = Path(tempfile.gettempdir()) / "nippou_起動エラー.html"

    target.write_text(f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<title>起動できませんでした</title>
<style>
 body{{margin:0;min-height:100vh;display:grid;place-items:center;
       background:#eef1f5;color:#101720;
       font-family:system-ui,"Yu Gothic UI","Meiryo UI",sans-serif;line-height:1.7}}
 .box{{width:min(560px,calc(100vw - 48px));background:#fff;border:1px solid #c9d2dc;
       border-radius:4px;padding:32px;box-shadow:0 6px 20px rgba(16,23,32,.08)}}
 h1{{margin:0 0 12px;font-size:19px;color:#b4232a}}
 .hint{{margin-top:16px;padding:14px;background:#fdeaea;border-left:4px solid #b4232a;
        border-radius:0 3px 3px 0;white-space:pre-wrap}}
 code{{font-family:ui-monospace,Consolas,monospace;font-size:13px;
       background:rgba(0,0,0,.06);padding:2px 5px;border-radius:2px;word-break:break-all}}
 dt{{color:#556171;font-size:13px;margin-top:12px}}
</style></head>
<body><main class="box">
<h1>起動できませんでした</h1>
<p>{html.escape(message)}</p>
{f'<div class="hint">{html.escape(hint)}</div>' if hint else ''}
<dt>ログの場所</dt>
<p><code>{html.escape(log_dir)}</code></p>
<dt>診断</dt>
<p>コンソールで詳しく見るには <code>start.bat</code> を実行してください。</p>
</main></body></html>
""", encoding="utf-8")
    return target


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="日報管理ツールを起動する")
    parser.add_argument("--no-browser", action="store_true",
                        help="ブラウザを開かない(検証用)")
    parser.add_argument("--check", action="store_true",
                        help="実行環境の確認だけして終わる(診断用)")
    args = parser.parse_args(argv)

    open_browser = not args.no_browser

    try:
        run_environment_checks()
    except StartupError as exc:
        report_failure(exc, open_browser=open_browser)
        return 1

    if args.check:
        from nippou import app_config
        print("実行環境の確認: 問題ありません\n")
        print(app_config.describe())
        print(describe_loopback())
        return 0

    try:
        return start(open_browser=open_browser)
    except StartupError as exc:
        report_failure(exc, open_browser=open_browser)
        return 1
    except KeyboardInterrupt:
        print("\n中断しました")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
