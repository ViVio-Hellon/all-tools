#!/usr/bin/env python3
"""Python側の起動開始点 (基盤仕様書 2.5)

アプリ本体を読む前に実行環境を整え、多重起動を判定し、サーバを立てて
ブラウザを開く。**業務機能はここに書かない。**

    Start.vbs (通常) / start.bat (診断)       … ブラウザ版
        └─ start_app.py            ← ここ
             ├─ 実行環境の確認      (Python版数・必須パッケージ・書込権限)
             ├─ launch_guard        (多重起動の判定・ポート選び)
             ├─ server              (waitress + Flask)
             └─ ブラウザを開く

デスクトップ版(``ライン管理カレンダー.exe`` → ``bridge.py``)も、起動の流れ
(待機画面 → 本体を組み立てる → 重い初期化)はここを通る(``start_bridge``)。
違うのは待ち受け方だけで、ポート・ロック・ブラウザは使わない。

使い方::

    python start_app.py               起動してブラウザを開く
    python start_app.py --no-browser  ブラウザを開かない(検証用)
    python start_app.py --check       環境の確認だけして終わる(診断用)
"""

from __future__ import annotations

import argparse
import os
import threading
import sys
import time
import webbrowser
from pathlib import Path
from typing import Optional

APP_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_ROOT))


def _bytecode_to_local() -> None:
    """**アプリのフォルダに何も書かない**(基盤仕様書 2.7)。

    Python は読み込んだ .py の写し(``__pycache__``)を、既定では
    その .py の隣に作ります。アプリのフォルダを共有に置いて配ると、
    **起動した端末の数だけ共有へ書き込みに行く**ことになり、読み取り専用の
    共有なら毎回作り直しで起動が遅くなります。

    ローカル領域の ``pycache`` へ向けます。向け先を知るには ``app_config``
    を読む必要があるので、**その1回だけは写しを作らずに読みます**
    (2ファイルぶん毎回読み直すだけで、目に見える差はありません)。
    """
    if os.environ.get("PYTHONPYCACHEPREFIX"):
        return                                    # 明示されていればそちらに従う
    before = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        from calendar_app import app_config

        target = app_config.local_dir("pycache")
        target.mkdir(parents=True, exist_ok=True)
        sys.pycache_prefix = str(target)
    except Exception:                             # noqa: BLE001 - 速さのためだけ
        pass
    finally:
        sys.dont_write_bytecode = before


_bytecode_to_local()

# 起動の起点。**待機画面が出るまでの時間**をログに残すために持つ ──
# 「遅い」という感想を、測れる数字にしておく
_BOOT_AT = time.monotonic()

# 必要なPython。README が 3.9 以上を掲げているので、それに合わせる
MIN_PYTHON = (3, 9)

# ``requirements.txt`` に対応する import 名(pip の名前と違うものがある)
REQUIRED_PACKAGES = (("flask", "Flask"), ("waitress", "waitress"))

# デスクトップ版(``bridge.py``)は待ち受けないので waitress は要らない
BRIDGE_PACKAGES = (("flask", "Flask"),)

# ブラウザを開いたあと、待ち受けが始まるのを待つ上限(秒)
LISTEN_TIMEOUT_SEC = 15

# 別のプロセスが先に起動していたとき、その待ち受けが始まるのを待つ上限(秒)。
# **待てなくても新しく立てない** ── 立てると2つになる
JOIN_WAIT_SEC = 20.0

# 停止を頼んでから、受付の輪が終わるのを待つ上限(秒)。
# ここを過ぎたら**確実に落とす** ── 残ったプロセスは次回の起動で
# 「すでに起動しています」と判定され、入れ替えた新しい版が動かない
EXIT_WAIT_SEC = 6.0


class StartupError(RuntimeError):
    """利用者に見せる、次の行動が分かる形のエラー。"""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


# ---------------------------------------------------------------------------
# 1. 実行環境の確認
# ---------------------------------------------------------------------------
def check_python_version() -> None:
    if sys.version_info < MIN_PYTHON:
        raise StartupError(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 以上が必要です"
            f"(いまは {sys.version.split()[0]})",
            "https://www.python.org/downloads/ から新しいPythonを入れてください。")


def check_packages(packages=None) -> None:
    """必須パッケージの有無。**入れ方まで示す**(基盤仕様書 ステップ5)。

    既定(``None``)はブラウザ版の一覧。呼ぶたびに引く(定義のときに決めない)。
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
    """``pip`` を実行するときに使うPythonの名前。

    ``Start.vbs`` は画面を出さないために **pythonw.exe** で起動する。
    そのまま ``sys.executable`` を案内すると
    ``pythonw.exe -m pip install ...`` と出るが、pythonw には画面が無いので
    **実行しても何も表示されない**(成否すら分からない)。
    案内するときは必ずコンソール側の ``python.exe`` に読み替える。
    """
    # Windowsのパスを他のOSで扱うと ``Path`` が ``\`` を区切りとみなさない。
    # 案内文を組み立てるだけなので、両方の区切りで切っておく
    name = sys.executable.replace("\\", "/").rsplit("/", 1)[-1]
    if name.lower().startswith("pythonw"):
        return "python" + name[len("pythonw"):]
    return name


def check_writable() -> Path:
    """ローカル領域を作れるか(基盤仕様書 2.7)。"""
    from calendar_app import app_config

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
    from calendar_app import app_config

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


def should_abort(check) -> bool:
    """起動確認が取れなかったとき、起動そのものを中止するか。

    中止するのは **TCPでも繋がらないとき**だけ。
    TCPが通っているならサーバ自身は待ち受けており、応答を取れないのは
    こちら側の確認経路の都合(プロキシ・セキュリティ製品)であることが多い。
    ブラウザはローカルアドレスをプロキシから除外するのが普通なので、
    そのまま開けばつながる。区別せずに中止すると、**動いているサーバごと**
    終わらせてしまい、現場では「起動できません」としか見えない。
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


# ---------------------------------------------------------------------------
# ログ
# ---------------------------------------------------------------------------
_log = None


def log():
    """起動入口のログ(基盤仕様書 2.6)。

    ロガー名を ``launcher`` にすることで、「起動前に失敗したのか」を
    業務処理のログと切り分けられる。
    """
    global _log
    if _log is None:
        from calendar_app.logging_utils import get_logger
        _log = get_logger("launcher")
    return _log


def _log_folder() -> str:
    from calendar_app import logging_utils

    return logging_utils.current_folder()


def log_environment() -> None:
    """起動のたびに残す1枚(基盤仕様書 2.6)。

    中身は ``logging_utils.write_header`` ── ログの書き先を変えたときにも
    同じものを書くので、1か所に置いてある。
    """
    from calendar_app import logging_utils

    logging_utils.write_header()


# ---------------------------------------------------------------------------
# 2. 起動
# ---------------------------------------------------------------------------
def start(*, open_browser: bool = True) -> int:
    """戻り値はプロセスの終了コード。

    【順番が要点】
    待機画面より前に置くものを、**できるだけ減らしてある**。
    Flask とアプリ本体を組み立ててからブラウザを開くと、いちばん重い
    import を「待たせるために見せるもの」より先にやることになる ──
    出るころには待つ理由がほぼ終わっている。

        ポートを決める → 待ち受け開始(待機画面だけ)→ ブラウザ
            → 本体を組み立てて差し替え → 重い初期化

    最初の3つは標準ライブラリと ``app_config`` だけで済む。
    """
    import launch_guard

    log_environment()

    # --- 起動の入口をひとつに絞る (基盤仕様書 2.4) ---
    # **判定より先に取る。** ロックファイルを読む判定だけだと、
    # 読んでから書くまでの1〜2秒の窓で2つとも通ってしまう
    # (``launch_guard.StartupLock`` の説明)。
    startup = launch_guard.StartupLock()
    if not startup.acquire():
        joined = _handle_existing(open_browser, startup)
        if joined is not None:
            return joined
    try:
        return _start_locked(open_browser=open_browser)
    finally:
        startup.release()


def _handle_existing(open_browser: bool, startup) -> Optional[int]:
    """入口は別のプロセスが押さえていた。合流するか、順番を待つ。

    戻り値が ``None`` なら「起動を続けてよい」(入口を取り直せた)。

    **判定は ``check_existing`` に任せる。** ここで「先に居るから合流」と
    決め打ちすると、**古い版が動いているときに新しい版が永久に動かない**
    ── 古い版は入口を握ったままなので、入れ替えた日にいつまでも
    古いほうのブラウザが開くことになる。
    """
    import launch_guard

    deadline = time.monotonic() + JOIN_WAIT_SEC
    waiting = False
    while True:
        guard = launch_guard.check_existing()
        if not guard.should_start:
            log().info("既存のインスタンスに合流します: %s", guard.url)
            print(f"すでに起動しています。ブラウザを開きます: {guard.url}")
            if open_browser:
                webbrowser.open(guard.url)
            return 0

        # 起動してよい状態になった(古い版を止めた / 相手が消えた)。
        # **入口を取り直せたときだけ**進む
        if startup.acquire():
            log().info("入口が空いたので起動を続けます: %s", guard.reason)
            return None

        if time.monotonic() >= deadline:
            # 相手が立ち上がりきらなかった。**新しく立てない** ──
            # 立てると、遅れて立ち上がった相手と2つになる
            log().warning("先に起動したプロセスが %.0f秒 で"
                          "待ち受けを始めませんでした", JOIN_WAIT_SEC)
            print("起動中です。少し待ってからもう一度お試しください。")
            return 0

        if not waiting:
            log().info("別のプロセスが起動中です。待ち受けが始まるのを待ちます")
            waiting = True
        time.sleep(0.3)


def _start_locked(*, open_browser: bool) -> int:
    """起動の入口を通ったあと。**ここに居るのは1プロセスだけ。**"""
    import launch_guard
    import server as server_module
    from calendar_app import app_config

    # --- 多重起動の判定 (基盤仕様書 2.4) ---
    guard = launch_guard.check_existing()
    if not guard.should_start:
        log().info("既存のインスタンスに合流します: %s", guard.url)
        print(f"すでに起動しています。ブラウザを開きます: {guard.url}")
        if open_browser:
            webbrowser.open(guard.url)
        return 0
    log().info("多重起動の判定: %s", guard.reason)

    # --- ポート選び ---
    port = launch_guard.pick_port()
    if port is None:
        candidates = app_config.port_candidates()
        raise StartupError(
            f"使えるポートがありません(試した番号: {candidates})",
            "他のアプリが使っている可能性があります。"
            "config/app.json の port を変えるか、そのアプリを終了してください。")

    # --- 待ち受けを始める(この時点ではまだ待機画面だけ) ---
    srv = server_module.AppServer(port)
    thread = server_module.run_in_background(srv)

    check = server_module.diagnose_listening(port, timeout=LISTEN_TIMEOUT_SEC)
    # **そのポートで応答しているのが自分とは限らない。** 取り合いに負けた
    # ときは、先に立った別のインスタンスが答えている ── 「待ち受けできて
    # いる」と読むと、bind に失敗したまま「起動しました」と言うことになる
    if srv.listen_error:
        raise StartupError(
            f"待ち受けを開始できませんでした (ポート {port})",
            f"{srv.listen_error}\n"
            "同じアプリがすでに起動している可能性があります。\n"
            f"ログ: {_log_folder()}")
    if should_abort(check):
        raise StartupError(
            "サーバを起動できませんでした",
            f"{check.hint}\n\nログ: {_log_folder()}")
    if not check.ok:
        log().warning("起動確認の応答を取れませんでしたが、待ち受けは"
                      "できているので続行します:\n%s", check.hint)
        print("[注意] 起動の確認応答を取れませんでした。"
              "画面が出ない場合は次を確認してください:")
        print(check.hint)

    # 待ち受けが始まってからロックを書く。先に書くと、起動に失敗した
    # ロックが残って次回の判定を惑わせる
    lock_info = launch_guard.build_lock_info(port, srv.token)
    launch_guard.write_lock(lock_info)

    # **消えていたら書き直す。** ロックは動いている自分についての公示で、
    # 失われるとトークンも失われる ── ``stop.bat`` も版の入れ替えも
    # 「止められない」状態に落ちる(``launch_guard.keep_lock``)
    keeper_stop = threading.Event()
    launch_guard.keep_lock(lock_info, keeper_stop)

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
        try:
            srv.build()
        except Exception as exc:                  # noqa: BLE001 - 画面に出して継続
            log().exception("アプリを組み立てられませんでした")
            srv.mark_error(f"アプリを組み立てられませんでした: {exc}")
            _hold_until_stopped(srv, thread)
            return 1

        _initialize(srv)

        # --- 待ち受けが終わるまでここで止まる ---
        _hold_until_stopped(srv, thread)
        return 0
    finally:
        keeper_stop.set()
        # **自分が書いたものだけ消す。** 無条件に消すと、あとから起動して
        # 落ちたプロセスが生きているほうのロックを持っていってしまう
        launch_guard.remove_lock(only_mine=True)
        log().info("終了しました")


def _hold_until_stopped(srv, thread) -> None:
    """待ち受けが終わるまで止まる。**終わらなくても必ず抜ける。**

    waitress の受付の輪は、つながったままの接続が1本でもあると回り続ける
    (``server.AppServer.stop`` の説明)。そちらは直してあるが、**ここでも
    受け止めておく** ── 止まらないことの害が大きすぎるため。
    残ったプロセスは、次の起動で「すでに起動しています」と判定され、
    入れ替えた新しい版がいつまでも動かない。
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

    ``sys.exit()`` は例外なので、受付の輪が回っている別スレッドには効かない。
    ``os._exit()`` は後始末をしないので、**残すべきものは先に書いてから**呼ぶ
    (ログは追記なので閉じなくても失われない)。
    """
    import logging

    logging.shutdown()
    os._exit(0)


def start_bridge(*, token: str = "", server_factory) -> int:
    """デスクトップ版の起動(``bridge.py`` から)。**ポートもロックも使わない。**

    多重起動の防止・窓・終了は外枠(Rust/Tauri)が持つ。ここでするのは
    ブラウザ版と同じ「待機画面 → 本体を組み立てる → 重い初期化」だけで、
    その中身(``_initialize``)は共有する ── 2本持つと片方だけ直すことになる。
    """
    import secrets

    import server as server_module

    log_environment()
    srv = server_factory(token or secrets.token_urlsafe(24))
    thread = server_module.run_in_background(srv)
    log().info("待機画面まで %.2f秒(デスクトップ版)", time.monotonic() - _BOOT_AT)
    try:
        srv.build()
    except Exception as exc:                      # noqa: BLE001 - 画面に出して継続
        log().exception("アプリを組み立てられませんでした")
        srv.mark_error(f"アプリを組み立てられませんでした: {exc}")
        _hold_until_stopped(srv, thread)
        return 1
    # 窓を閉じたら外枠が終わらせるので、心拍による自動終了は使わない。
    # **未送信は終わる前に送り切る**(``BridgeServer.stop`` →
    # ``sync_service.send_before_exit``)
    _initialize(srv, watch_idle=False)
    _hold_until_stopped(srv, thread)
    log().info("終了しました(デスクトップ版)")
    return 0


def _initialize(srv, *, watch_idle: bool = True) -> None:
    """重い初期化。サーバが立ってから行う。

    ここで失敗しても**サーバは落とさない**。落とすと利用者のブラウザには
    「接続できません」としか出ず、理由が伝わらない。画面に理由を出す。
    """
    from calendar_app import db, sync_service

    # 配布設定(ツール直下の `配布設定/`)があれば、**同期より先に**読む
    # ── 置き場所が入っているので、読む前に同期を始めると既定の場所を見る。
    # その端末にすでにある設定は読まない(`calendar_app/distribution.py`)
    try:
        from calendar_app import distribution

        loaded = distribution.apply_on_start()
        if loaded.applied:
            # 読む前に同期が出来ていたら、古い参照パスのまま動いている
            sync_service.get_service().reload()
    except Exception as exc:                      # noqa: BLE001 - 起動を止めない
        log().warning("配布設定を読み込めませんでした: %s", exc)

    try:
        srv.mark_stage("アプリを準備中", "prepare")
        conn = db.connect()
        conn.close()
    except Exception as exc:                      # noqa: BLE001 - 画面に出して継続
        log().exception("初期化に失敗しました")
        srv.mark_error(f"初期化に失敗しました: {exc}")
        return

    # 画面が居なくなったら終わる(基盤仕様書 2.8)。
    # **窓が無いアプリなので、タブを閉じたら終わったつもりになる。**
    # 残っていると、次の起動が「すでに起動しています」と判定して
    # 入れ替えた新しい版が動かない。
    # デスクトップ版は窓が終わりを決めるので立てない(``start_bridge``)
    if watch_idle:
        _watch_for_idle(srv)

    # Access との同期を始める。**繋がらなくても起動は続ける** ──
    # 送れないことと、アプリが使えないことは別(送信待ちに残って
    # 次の機会にやり直す)
    srv.mark_stage("Accessと同期しています", "sync")
    # **前もって決めておいたラインがあれば、ここで受け取る。**
    # 同期を始める前に済ませる ── 受け取った直後の報告で、実績として
    # 一覧に出したい(マスタ確認で「効いた」ことが分かる)
    try:
        from calendar_app import terminals

        taken = terminals.apply_reservation()
        if taken:
            log().info("予約されていたラインを設定しました: %s", taken)
    except Exception as exc:                      # noqa: BLE001 - 起動を止めない
        log().warning("予約を受け取れませんでした: %s", exc)

    # **アクセス権限でこの端末のラインが決まっていれば、それに合わせる。**
    # 予約より後に見る ── 表に無いラインが予約されていても表が勝つ
    # (管理の出どころは表1つ。``calendar_app/access_control.py``)
    try:
        from calendar_app import access_control

        conn = db.connect()
        try:
            access_control.enforce(conn)
        finally:
            conn.close()
    except Exception as exc:                      # noqa: BLE001 - 起動を止めない
        log().warning("アクセス権限を確かめられませんでした: %s", exc)

    try:
        sync_service.get_service().start()
    except Exception as exc:                      # noqa: BLE001 - 同期で起動を止めない
        log().warning("同期を始められませんでした: %s", exc)

    srv.mark_ready(True)


def _watch_for_idle(srv) -> None:
    """画面が居なくなったら止める見張りを立てる。

    止め方は ``/api/shutdown`` と同じ道(``srv.stop``)を通す ── 止め方が
    2つあると、片方だけ直した状態を作ってしまう。処理中かどうかの判断も
    同じものを使う。

    **未送信があるあいだは終わらせない**(``idle_exit`` の説明を参照)。
    件数の数え方も送り直しの頼み方も同期側が持っているので、
    見張りには渡すだけにする ── 基盤側が業務の事情を知らないでいられる。
    """
    from calendar_app import idle_exit, sync_service

    idle_exit.install(srv.stop, sync_service.get_service().is_busy,
                      sync_service.unsent_count,
                      sync_service.request_send,
                      on_exit=sync_service.remember_exit)


# ---------------------------------------------------------------------------
# 3. 失敗の伝え方
# ---------------------------------------------------------------------------
def report_failure(error: StartupError, *, open_browser: bool) -> None:
    """コンソールとブラウザの両方に出す。

    ``Start.vbs``(コンソール非表示)で起動された場合、標準出力は誰にも
    見えない。基盤仕様書 2.2 の「起動できない場合は、確認すべきログの場所を
    表示します」を満たすため、HTMLを書いてブラウザで開く。
    """
    print(f"\n[エラー] {error}", file=sys.stderr)
    if error.hint:
        print(error.hint, file=sys.stderr)

    try:
        from calendar_app import logging_utils
        log_dir = logging_utils.current_folder()
    except Exception:                             # noqa: BLE001 - 失敗の報告で失敗しない
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
    """起動に失敗したことを伝えるHTMLを一時領域に書く。

    **版も出す。** 起動できないときこそ問い合わせになるので、
    「入っているのはどれか」をこの画面だけで答えられるようにする。
    """
    import html
    import tempfile

    version = ""
    try:
        from calendar_app import app_config
        target = app_config.local_dir("work") / "起動エラー.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        version = app_config.version_label()
    except Exception:                             # noqa: BLE001
        target = Path(tempfile.gettempdir()) / "line_calendar_起動エラー.html"

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
 .ver{{float:right;font-family:ui-monospace,Consolas,monospace;font-size:12px;
       font-weight:700;color:#556171;background:rgba(0,0,0,.06);
       padding:3px 8px;border-radius:999px}}
</style></head>
<body><main class="box">
{f'<span class="ver">{html.escape(version)}</span>' if version else ''}
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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="ライン管理カレンダーを起動する")
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
        from calendar_app import app_config
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
