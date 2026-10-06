#!/usr/bin/env python3
"""Python側の起動開始点 (基盤仕様書 2.5)

アプリ本体を読む前に実行環境を整え、多重起動を判定し、サーバを立てて
ブラウザを開く。**業務機能はここに書かない。**
(python-web-tools の `start_app.py` と同じ流れ。この道具はモードが1つ)

    Start.vbs (通常) / start.bat (診断)
        └─ start_app.py            ← ここ
             ├─ 実行環境の確認      (Python版数・必須パッケージ・書込権限)
             ├─ launch_guard        (多重起動の判定・ポート選び)
             ├─ server              (waitress。先に待機画面だけ出す)
             ├─ ブラウザを開く
             └─ 本体(app/ Flask)を組み立てて差し替え → 点検表フォルダの確認

使い方:

    python start_app.py                 通常(ブラウザを開く)
    python start_app.py --no-browser    ブラウザを開かない(検証用)
    python start_app.py --check         環境の確認だけして終わる(診断用)
    python start_app.py --demo          Excel を使わない模擬モード(見本フォルダ)
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Optional

APP_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_ROOT))

# 起動の起点。**待機画面が出るまでの時間**をログに残すために持つ
_BOOT_AT = time.monotonic()

MIN_PYTHON = (3, 9)

# `requirements.txt` に対応する import 名
REQUIRED_PACKAGES = (("flask", "Flask"), ("waitress", "waitress"))
# デスクトップ版(`bridge.py`)は待ち受けないので waitress は要らない
BRIDGE_PACKAGES = (("flask", "Flask"),)

# ブラウザを開いたあと、待ち受けが始まるのを待つ上限(秒)
LISTEN_TIMEOUT_SEC = 15


class StartupError(RuntimeError):
    """利用者に見せる、次の行動が分かる形のエラー。"""

    def __init__(self, message: str, hint: str = "", *, show_page: bool = True) -> None:
        super().__init__(message)
        self.hint = hint
        # 起動エラーの画面をブラウザで開くか。デスクトップ版が動いているときは
        # 開かない(ブラウザが前に出ると、使ってほしい窓が隠れる)
        self.show_page = show_page


def desktop_running_error() -> StartupError:
    """デスクトップ版が動いているので、ブラウザ版は起動しない(`core/instance_guard.py`)。"""
    return StartupError(
        "点検表はデスクトップ版(統合ツールの窓)で動いています",
        "ブラウザ版とデスクトップ版は同時には使えません。統合ツールの窓の「点検表」のタブで操作してください。\n"
        "ブラウザ版を使うときは、統合ツールの窓を閉じてから開き直してください。",
        show_page=False)


def browser_running_error() -> StartupError:
    """ブラウザ版が動いているので、デスクトップ版(統合ツールの窓の「点検表」)は起動しない。"""
    return StartupError(
        "点検表のブラウザ版が動いています",
        "ブラウザ版とデスクトップ版は同時には使えません。"
        "ブラウザの画面の「終了」(または stop.bat)で閉じてから、このタブの「もう一度開く」を押してください。")


# ------------------------------------------------------------------
# 1. 実行環境の確認
# ------------------------------------------------------------------
def _bootstrap_pycache() -> None:
    """Python キャッシュは、アプリ本体(共有フォルダでもよい)ではなくローカル領域へ。

    アプリのモジュールを読む前に決める必要がある(基盤仕様書 2.7)。
    """
    if os.environ.get("PYTHONPYCACHEPREFIX"):
        return
    # **置き場所を決めるための import でも書かせない。** 以前はここで読む
    # core/app_config のキャッシュだけが、アプリ本体の __pycache__ へ書かれていた。
    # 本体を共有フォルダに置くと、全ラインのPCが同じフォルダへ書きに行く
    before = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        from core import app_config
        sys.pycache_prefix = str(app_config.local_dir("pycache"))
    except Exception:                              # noqa: BLE001 - 起動は止めない
        return                                     # 置き場所が無いなら書かないまま
    finally:
        if sys.pycache_prefix:
            sys.dont_write_bytecode = before


def check_python_version() -> None:
    if sys.version_info < MIN_PYTHON:
        raise StartupError(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 以上が必要です"
            f"(いまは {sys.version.split()[0]})",
            "https://www.python.org/downloads/ から新しいPythonを入れてください。")


def check_packages(packages=None) -> None:
    """必須パッケージの有無。**入れ方まで示す**。既定はブラウザ版の一覧。"""
    import importlib.util

    missing = [pip_name for module, pip_name in (packages or REQUIRED_PACKAGES)
               if importlib.util.find_spec(module) is None]
    if missing:
        raise StartupError(
            f"必要なパッケージが入っていません: {', '.join(missing)}",
            f"コマンドプロンプトで次を実行してください:\n"
            f"    {console_python()} -m pip install -r requirements.txt")


def console_python() -> str:
    """`pip` を案内するときの Python の名前(pythonw.exe は python.exe に読み替える)。"""
    name = sys.executable.replace("\\", "/").rsplit("/", 1)[-1]
    if name.lower().startswith("pythonw"):
        return "python" + name[len("pythonw"):]
    return name


def check_writable() -> Path:
    """ローカル領域を作れるか(基盤仕様書 2.7)。"""
    from core import app_config

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
        raise StartupError(f"作業用フォルダに書き込めません: {root}", f"{exc}") from None
    return root


def check_config() -> None:
    """アプリ固有値が読めているか。読めなくても既定値で動くが、記録は残す。"""
    from core import app_config

    error = app_config.load_error()
    if error:
        log().warning("%s — 既定値で起動します", error)


ENV_CHECK_STEPS = 3


def run_environment_checks(progress=None, *, bridge: bool = False) -> Path:
    """順に確認する。落ちたところで理由が分かるように分けてある。

    `progress`(`core.console_progress.ConsoleProgress`)を渡すと、段ごとに
    進み具合を出す(`ENV_CHECK_STEPS` 段)。
    """
    def step(label: str) -> None:
        if progress is not None:
            progress.step(label)

    step("Python と部品を確かめています")
    check_python_version()
    check_packages(BRIDGE_PACKAGES if bridge else None)
    step("作業用フォルダを確かめています")
    root = check_writable()
    step("設定を読んでいます")
    check_config()
    return root


def describe_loopback() -> str:
    """自分自身への通信まわりの状態(診断用)。社内PCのプロキシで失敗することがある。"""
    import launch_guard

    lines = ["", "--- 自分自身への通信 ---"]
    proxies = launch_guard.proxy_settings()
    if proxies:
        lines.append("プロキシ設定  : " + ", ".join(f"{k}={v}" for k, v in sorted(proxies.items())))
        lines.append("  ※このアプリはプロキシを経由せずに 127.0.0.1 へ接続します。")
        lines.append("    ブラウザ側でも 127.0.0.1 / localhost が除外されているか確認してください。")
    else:
        lines.append("プロキシ設定  : なし")
    return "\n".join(lines)


def describe_excel(check_excel: bool, progress=None) -> str:
    """Excel まわりの確認(診断用)。`--check` のときに出す。

    `check_excel` のときは Excel を起動して確かめ、閉じきるまで待つ
    (`progress` があれば2段: 起動して確かめる・閉じる)。
    """
    lines = ["", "--- Excel と印刷 ---"]
    try:
        from app.services import system_info
        printer = system_info.default_printer()
        lines.append(f"既定のプリンター: {printer or '取得できません'}")
    except Exception as exc:                      # noqa: BLE001
        lines.append(f"既定のプリンター: 取得できません({exc})")
    if os.name == "nt":
        cscript = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cscript.exe"
        lines.append(f"cscript.exe    : {'あり' if cscript.is_file() else '見つかりません'} ({cscript})")
    if check_excel:
        from app.services.excel_service import ExcelService
        from app.services.settings_service import load_inspection_config
        excel = ExcelService(load_inspection_config(), log())
        if progress is not None:
            progress.step("Excel を起動して確かめています")
        result = excel.selftest(keep=True)
        if progress is not None:
            progress.step("Excel を閉じています")
        excel.shutdown("動作確認のあと")
        lines.append(excel_result_line(result))
    return "\n".join(lines)


def excel_result_line(result: dict) -> str:
    if result.get("ok"):
        return f"Excel の起動   : OK(Excel {result.get('version')}、{result.get('seconds')}秒)"
    return f"Excel の起動   : 失敗 {result.get('code')} {result.get('detail')}"


# ------------------------------------------------------------------
# ログ
# ------------------------------------------------------------------
_log = None


def log():
    """起動入口のログ(基盤仕様書 2.6 の launcher にあたる)。

    業務ログ(`inspection.app.*`)と名前を分け、「起動前に失敗したのか」を
    切り分けられるようにする。
    """
    global _log
    if _log is None:
        from core.logging_utils import get_logger
        _log = get_logger("launcher")
    return _log


def log_environment() -> None:
    """起動のたびに残す1枚(基盤仕様書 2.6)。"""
    from core import app_config

    log().info("=" * 60)
    log().info("起動: pid=%s 版=%s", os.getpid(), app_config.version())
    log().info("Python: %s (%s)", sys.version.split()[0], sys.executable)
    log().info("アプリ本体: %s", APP_ROOT)
    log().info("ローカル領域: %s", app_config.local_root())
    if app_config.store_python_family():
        log().warning("Microsoft Store 版の Python です。ログ等はエクスプローラーでは %s に見えます",
                      app_config.visible_local_root())


# ------------------------------------------------------------------
# 2. 起動
# ------------------------------------------------------------------
def _claim_after_old_exits(instance_guard, limit_sec: float = 15.0):
    """入れ替えで止めた古いブラウザ版が、同時起動の錠を放すのを待つ。"""
    deadline = time.monotonic() + limit_sec
    while True:
        claim = instance_guard.claim(instance_guard.BROWSER)
        if claim.ok:
            return claim
        if claim.other_kind_running:
            raise desktop_running_error()
        if time.monotonic() >= deadline:
            log().warning("前のブラウザ版が同時起動の錠を放しません。錠なしで続けます")
            return claim
        time.sleep(0.3)


# 起動の段の数(start.bat の窓の進み具合)。環境の確認 + ここから先の6段
LAUNCH_STEPS = ENV_CHECK_STEPS + 6


def start(*, open_browser: bool = True, options: Optional[dict] = None,
          progress=None, check_excel: bool = False) -> int:
    """戻り値はプロセスの終了コード。

    【順番が要点】待機画面より前に置くものを、できるだけ減らしてある。

        ポートを決める → 待ち受け開始(待機画面だけ)→ ブラウザ
            → 本体を組み立てて差し替え → 点検表フォルダの確認
            → (`check_excel` のとき)Excel の確認

    `progress` は start.bat の窓の進み具合(`main` が環境の確認から続けて渡す)。
    """
    import launch_guard
    import server as server_module
    from core import app_config, logging_utils
    from core.console_progress import ConsoleProgress

    from core import instance_guard

    progress = progress or ConsoleProgress(LAUNCH_STEPS, live=False, stream=_NoStream())
    log_environment()
    progress.step("多重起動と使える番号(ポート)を確かめています")

    # --- デスクトップ版と同時に動かさない(後から開いたほうが止まる) ---
    instance = instance_guard.claim(instance_guard.BROWSER)
    if instance.other_kind_running:
        log().warning("デスクトップ版が動いているので、ブラウザ版は起動しません")
        instance_guard.bring_desktop_to_front()
        raise desktop_running_error()

    # --- 多重起動の判定 (基盤仕様書 2.4) ---
    # **まずロックを取る。** 判定してから書くまでのあいだに始まった2つ目が
    # 「ロックなし」を見て一緒に立ち上がらないよう、作れたかどうかで決める
    while not launch_guard.try_acquire():
        guard = launch_guard.check_existing()
        if not guard.should_start:
            log().info("既存のインスタンスに合流します: %s", guard.url)
            progress.stop(f"すでに起動しています。ブラウザを開きます: {guard.url}")
            if open_browser:
                webbrowser.open(guard.url)
            return 0
        log().info("多重起動の判定: %s", guard.reason)

    # ここから先の失敗は**必ずロックを手放してから**投げる
    try:
        if not instance.ok:
            # 古いブラウザ版を止めて入れ替える途中。止まって錠が空くのを待って取る
            instance = _claim_after_old_exits(instance_guard)
        port = launch_guard.pick_port()
        if port is None:
            raise StartupError(
                f"使えるポートがありません(試した番号: {app_config.port_candidates()})",
                "他のアプリが使っている可能性があります。"
                "config/app.json の port を変えるか、そのアプリを終了してください。")

        progress.step("待ち受けを始めています")
        srv = server_module.AppServer(port, options=options)
        thread = server_module.run_in_background(srv)

        check = server_module.diagnose_listening(port, timeout=LISTEN_TIMEOUT_SEC)
        if not check.ok and not check.tcp_ok:
            raise StartupError(
                "サーバを起動できませんでした",
                f"{check.hint}\n\nログ: {logging_utils.log_dir()}")
    except BaseException:
        launch_guard.remove_lock()
        instance_guard.release_all()
        raise
    if not check.ok:
        log().warning("起動確認の応答を取れませんでしたが、待ち受けは"
                      "できているので続行します:\n%s", check.hint)
        print("[注意] 起動の確認応答を取れませんでした。画面が出ない場合は次を確認してください:")
        print(check.hint)

    # 待ち受けが始まってからロックを書く
    launch_guard.write_lock(launch_guard.build_lock_info(port, srv.token))

    try:
        progress.step("ブラウザを開いています")
        if open_browser:
            log().info("ブラウザを開きます: %s", srv.url.split("?")[0])
            webbrowser.open(srv.url)
        else:
            progress.note(f"起動しました: {srv.url}")
        log().info("待機画面まで %.2f秒", time.monotonic() - _BOOT_AT)

        # --- 本体を組み立てて差し替える ---
        progress.step("アプリを組み立てています")
        try:
            srv.build()
        except Exception as exc:                  # noqa: BLE001 - 画面に出して継続
            log().exception("アプリを組み立てられませんでした")
            srv.boot.mark_error(f"アプリを組み立てられませんでした: {exc}")
            progress.stop(f"[エラー] アプリを組み立てられませんでした: {exc}")
            _hold_until_stopped(srv, thread)
            return 1

        _initialize(srv, progress, check_excel=check_excel)
        _hold_until_stopped(srv, thread)
        return 0
    finally:
        _shutdown_business(srv)
        launch_guard.remove_lock()
        instance_guard.release_all()
        log().info("終了しました(稼働 %.0f秒)", time.monotonic() - _BOOT_AT)


# 停止を頼んでから、受付の輪が終わるのを待つ上限(秒)。
# ここを過ぎたら**確実に落とす** ── 残ったプロセスは次回の起動で
# 「すでに起動しています」と判定され、入れ替えた新しい版が動かない
EXIT_WAIT_SEC = 6.0


def _hold_until_stopped(srv, thread) -> None:
    """待ち受けが終わるまで止まる。**終わらなくても必ず抜ける。**"""
    while thread.is_alive():
        thread.join(timeout=0.5)
        if not srv.stop_requested:
            continue
        thread.join(timeout=EXIT_WAIT_SEC)
        if thread.is_alive():
            log().warning("待ち受けが %.0f秒 で終わらないので、プロセスを終了します", EXIT_WAIT_SEC)
            _hard_exit(srv)
        return


def _hard_exit(srv) -> None:
    """後始末をしてから、確実に落とす(`os._exit` は後始末をしないので先に書く)。"""
    import logging

    _shutdown_business(srv)
    try:
        import launch_guard
        if not getattr(srv, "bridge", False):     # デスクトップ版はブラウザ版のロックを持たない
            launch_guard.remove_lock()
    except Exception:                              # noqa: BLE001
        pass
    logging.shutdown()
    os._exit(0)


def _shutdown_business(srv) -> None:
    """業務の後始末(印刷の中止要求・残った Excel の片付け)。2回呼ばれても安全。"""
    if getattr(srv, "_business_closed", False):
        return
    srv._business_closed = True
    try:
        from core import event_log
        event_log.record("shutdown", event_log.INFO, uptime_sec=int(time.monotonic() - _BOOT_AT))
    except Exception:                              # noqa: BLE001
        pass
    business = _business(srv)
    if business is not None:
        try:
            business.on_shutdown("アプリの終了")
        except Exception:                          # noqa: BLE001
            log().exception("業務の終了処理でエラー")


def _business(srv):
    app = getattr(srv, "app", None)
    return None if app is None else app.extensions.get("inspection")


# 点検表フォルダの確認を待つ上限。共有フォルダに届かない端末では、
# OS 側で数十秒かかることがある。**待たせきりにはしない**
SCAN_WAIT_LIMIT_SEC = 60


def start_bridge(*, token: str = "", options: Optional[dict] = None, server_factory) -> int:
    """デスクトップ版の起動(`bridge.py` から)。**ポートも、ブラウザ版のロックも使わない。**

    窓・終わり方は外枠(統合ツールの Rust/Tauri)が持つ。ここでするのはブラウザ版と同じ
    「待機画面 → 本体を組み立てる → 重い初期化」だけで、中身(`_initialize`)は
    共有する ── 2本持つと片方だけ直すことになる。

    ブラウザ版とは同時に動かさない(`core/instance_guard.py`): デスクトップ版の
    錠を取れなければ(ブラウザ版が先に動いている)**こちらが止まる**。取れたら、
    終わるまで握る(ブラウザ版はそれを見て起動しない)。
    """
    import secrets

    import server as server_module
    from core import instance_guard
    from core.console_progress import ConsoleProgress

    log_environment()
    claim = instance_guard.claim(instance_guard.DESKTOP)
    if not claim.ok:
        if claim.other_kind_running:
            log().warning("ブラウザ版が動いているので、デスクトップ版は起動しません")
            raise browser_running_error()
        raise StartupError("点検表がほかの窓で動いています", "開いている窓をお使いください。")
    srv = server_factory(token or secrets.token_urlsafe(24), options or {})
    thread = server_module.run_in_background(srv)
    log().info("待機画面まで %.2f秒(デスクトップ版)", time.monotonic() - _BOOT_AT)
    progress = ConsoleProgress(LAUNCH_STEPS, live=False, stream=_NoStream())
    try:
        try:
            srv.build()
        except Exception as exc:                  # noqa: BLE001 - 画面に出して継続
            log().exception("アプリを組み立てられませんでした")
            srv.boot.mark_error(f"アプリを組み立てられませんでした: {exc}")
            _hold_until_stopped(srv, thread)
            return 1
        # 窓を閉じたら外枠が終わらせるので、心拍による自動終了は使わない
        _initialize(srv, progress, watch_idle=False)
        _hold_until_stopped(srv, thread)
        return 0
    finally:
        _shutdown_business(srv)
        log().info("終了しました(デスクトップ版・稼働 %.0f秒)", time.monotonic() - _BOOT_AT)


def _initialize(srv, progress, *, check_excel: bool = False, watch_idle: bool = True) -> None:
    """重い初期化。サーバが立ってから行う。

    ここで失敗しても**サーバは落とさない**。落とすと利用者のブラウザには
    「接続できません」としか出ず、理由が伝わらない。画面に理由を出す。
    """
    business = _business(srv)
    try:
        progress.step("前回の一覧を読み込み、前回の残りを片付けています")
        srv.mark_stage("アプリを準備中")
        business.on_startup()
    except Exception as exc:                      # noqa: BLE001 - 画面に出して継続
        log().exception("初期化に失敗しました")
        srv.mark_error(f"初期化に失敗しました: {exc}")
        progress.stop(f"[エラー] 初期化に失敗しました: {exc}(ブラウザの画面にも出ています)")
        return

    # 画面が居なくなったら終わる(基盤仕様書 2.8)。デスクトップ版は窓が決める
    if watch_idle:
        _watch_for_idle(srv, business)

    # **点検表フォルダの確認が終わるまで準備完了にしない。** 一覧が空のまま
    # 画面に入ると「点検表がありません」を読むことになる。ただし待たせきりに
    # しない ── 上限を過ぎたら入れるようにして、確認は背景で続ける
    srv.mark_stage("点検表フォルダを確認中", key="scan")
    progress.step("点検表フォルダを確かめています")
    threading.Thread(target=_ready_when_scanned, args=(srv, business, progress, check_excel),
                     name="ready-watch", daemon=True).start()


def _watch_for_idle(srv, business) -> None:
    """画面が居なくなったら止める見張り。止め方は `/api/shutdown` と同じ道(`srv.stop`)。"""
    from core import idle_exit

    idle_exit.install(srv.stop, lambda: bool(business.busy_labels()))


def _ready_when_scanned(srv, business, progress=None, check_excel: bool = False) -> None:
    if business.inspection.inventory() is not None:
        # 前回の一覧がある。**待たずに入る** ── 確認は裏で続き、終われば画面が替わる
        srv.mark_ready(True)
    else:
        finished = business.inspection.wait(SCAN_WAIT_LIMIT_SEC)
        if not finished:
            log().warning("点検表フォルダの確認が %s秒 で終わらないので、先に画面を開きます",
                          SCAN_WAIT_LIMIT_SEC)
        srv.mark_ready(True)
    _record_startup(srv, business)
    if progress is not None:
        progress.finish("起動しました。ブラウザの画面で操作してください")
    if check_excel and not business.demo:
        _check_excel_in_app(business)


def _record_startup(srv, business) -> None:
    """起動できたことと、そのときの環境(なぜなぜで「そのときどうだったか」を引くため)。"""
    try:
        import launch_guard
        from core import app_config, event_log, logging_utils
        event_log.record(
            "startup", event_log.OK, seconds=round(time.monotonic() - _BOOT_AT, 2),
            edition="デスクトップ版" if getattr(srv, "bridge", False) else "ブラウザ版",
            port=srv.port or None,
            skipped_ports=launch_guard.SKIPPED_PORTS or None, python=sys.version.split()[0],
            python_exe=sys.executable, store_python=app_config.store_python_family() or None,
            app_root=str(APP_ROOT), log_dir=str(logging_utils.log_dir()),
            log_fallback=logging_utils.status()["fallback_reason"] or None,
            folder=business.settings.effective_root_folder(), demo=bool(business.demo),
            printer=business.default_printer() or "", excel=business.excel.backend_name)
    except Exception:                              # noqa: BLE001 - 記録の失敗で起動を止めない
        log().exception("起動の記録に失敗しました")


def _check_excel_in_app(business) -> None:
    """start.bat(`--check-excel`): 画面が出たあとで Excel を確かめ、窓に結果を出す。

    **確かめた Excel は閉じない。** 最初のプレビュー・印刷がそれを使うので、
    そこでまた起動を待たない(使われなければ `keep_alive_sec` のあとで閉じる)。
    """
    from core.console_progress import ConsoleProgress

    progress = ConsoleProgress(1)
    progress.step("Excel を起動して確かめています(画面はもう使えます)")
    result = business.excel.selftest(keep=True)
    if result.get("ok"):
        message = (f"Excel の確認: OK(Excel {result.get('version')})"
                   "。この Excel を最初のプレビュー・印刷に使います")
    else:
        message = f"Excel の確認: 失敗 {result.get('code')} {result.get('detail')}"
        log().warning("Excel の確認に失敗しました: %s %s", result.get("code"), result.get("detail"))
    progress.finish(message)


class _NoStream:
    """進み具合を出さないとき(試験・`start()` を直接呼んだとき)の行き先。"""

    def write(self, _text: str) -> None:
        pass

    def flush(self) -> None:
        pass


# ------------------------------------------------------------------
# 3. 失敗の伝え方
# ------------------------------------------------------------------
def report_failure(error: StartupError, *, open_browser: bool) -> None:
    """コンソールとブラウザの両方に出す。

    `Start.vbs`(コンソール非表示)で起動された場合、標準出力は誰にも
    見えない。起動できない場合は確認すべきログの場所を表示する(基盤仕様書 2.2)。
    """
    if sys.stderr is not None:
        print(f"\n[エラー] {error}", file=sys.stderr)
        if error.hint:
            print(error.hint, file=sys.stderr)
    try:
        from core import logging_utils
        info = logging_utils.status()
        # Store 版の Python では、ローカルのログはエクスプローラーから見える場所を案内する
        log_dir = info["visible_local_dir"] if info["is_local"] else info["dir"]
    except Exception:                             # noqa: BLE001
        log_dir = "(ローカル領域を特定できませんでした)"
    try:
        log().error("起動に失敗: %s / %s", error, error.hint)
        from core import event_log
        event_log.record("startup", event_log.NG, message=str(error), detail=error.hint,
                         log_dir=log_dir)
    except Exception:                             # noqa: BLE001
        pass
    if not open_browser:
        return
    # pythonw(Start.vbs)にはコンソールが無い。エラー画面のファイルが
    # ブラウザから見えないこと(Store 版の Python)もあるので、まず窓で知らせる
    if sys.stderr is None:
        _message_box(f"{error}\n\n{error.hint}\n\nログの場所: {log_dir}".strip())
    if not getattr(error, "show_page", True):
        return
    try:
        path = _write_error_page(str(error), error.hint, log_dir)
        webbrowser.open(path.as_uri())
    except Exception as exc:                      # noqa: BLE001
        if sys.stderr is not None:
            print(f"(エラー画面を出せませんでした: {exc})", file=sys.stderr)


def _message_box(text: str) -> None:
    """Windows の標準の窓で知らせる(ctypes。追加のライブラリは要らない)。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        title = "点検表 選択・印刷 - 起動できませんでした"
        try:
            from core import app_config
            title += f" ({app_config.version_label()})"
        except Exception:                         # noqa: BLE001
            pass
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)   # MB_ICONERROR
    except Exception:                             # noqa: BLE001
        pass


def _write_error_page(message: str, hint: str, log_dir: str) -> Path:
    import html
    import tempfile

    try:
        from core import app_config
        version = app_config.version_label()
    except Exception:                             # noqa: BLE001
        version = "(版を読めませんでした)"
    try:
        from core import app_config
        target = app_config.local_dir("work") / "起動エラー.html"
        target.parent.mkdir(parents=True, exist_ok=True)
    except Exception:                             # noqa: BLE001
        target = Path(tempfile.gettempdir()) / "inspection_sheet_起動エラー.html"
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
<dt>版</dt>
<p><code>{html.escape(version)}</code></p>
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
    parser = argparse.ArgumentParser(description="点検表 選択・印刷を起動する")
    parser.add_argument("--no-browser", action="store_true", help="ブラウザを開かない(検証用)")
    parser.add_argument("--check", action="store_true", help="実行環境の確認だけして終わる(診断用)")
    parser.add_argument("--check-excel", action="store_true",
                        help="Excel の起動も確かめる。--check のときはその場で(閉じきるまで待つ)、"
                             "起動のときは画面が出たあとで確かめ、その Excel を残して使う")
    parser.add_argument("--demo", action="store_true",
                        help="Excel を使わない模擬モード(見本フォルダで画面を確かめる)")
    args = parser.parse_args(argv)

    _bootstrap_pycache()
    open_browser = not args.no_browser

    # start.bat の窓に進み具合を出す(Start.vbs の pythonw には窓が無いので何も出ない)
    from core.console_progress import ConsoleProgress
    log()                                         # ログの出口を先に作る(棒が静かにできるように)
    if args.check:
        progress = ConsoleProgress(ENV_CHECK_STEPS + 1 + (2 if args.check_excel else 0))
    else:
        progress = ConsoleProgress(LAUNCH_STEPS)

    try:
        run_environment_checks(progress)
    except StartupError as exc:
        progress.stop()
        report_failure(exc, open_browser=open_browser)
        return 1

    if args.check:
        from core import app_config
        progress.step("通信とプリンターを確かめています")
        loopback = describe_loopback()
        excel = describe_excel(args.check_excel, progress)
        progress.finish("実行環境の確認: 問題ありません")
        print()
        print(app_config.describe())
        print(loopback)
        print(excel)
        return 0

    try:
        return start(open_browser=open_browser, options={"demo": args.demo},
                     progress=progress, check_excel=args.check_excel)
    except StartupError as exc:
        progress.stop()
        report_failure(exc, open_browser=open_browser)
        return 1
    except KeyboardInterrupt:
        print("\n中断しました")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
