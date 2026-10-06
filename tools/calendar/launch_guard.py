"""多重起動の防止とインスタンス管理 (基盤仕様書 2.4)

同じアプリが二重に起動すると、ポート競合・二重処理・設定ファイル競合が
起きる。そこで起動ごとに**ロックファイル**(PID・ポート・起動時刻)を書き、
次の起動はそれを見て判断する。

    生きている同じアプリが居る → 新しく起動せず、ブラウザだけ開く
    死んだロックが残っている   → 消して続行する(記録は残す)

「生きているか」の判定は2段構えにする:

    1. PIDのプロセスが存在するか
    2. そのポートの ``/api/health`` が**同じ app_id** を返すか

1だけでは足りない。PIDは使い回されるので、無関係なプロセスが同じ番号を
持っていることがある。2だけでも足りない。別のアプリがHTTPを返している
場合があるので、``app_id`` の照合が要る(基盤仕様書 2.3)。

このモジュールは Flask に依存しない。起動の判断だけを持つので、
サーバが立たない状況でも動く。
"""

from __future__ import annotations

import json
import os
import socket
import threading
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from calendar_app import app_config
from calendar_app.logging_utils import get_logger

log = get_logger("launch_guard")

# ``/api/health`` を叩くときの待ち時間(秒)。ローカルなので短くてよい。
# 長いと、死んだロックの掃除に毎回この分だけ待たされる
HEALTH_TIMEOUT_SEC = 1.5

# 古い版が動いていたとき、終わるのを待つ上限(秒)
REPLACE_WAIT_SEC = 10.0

# 自分のロックが消えていないかを見に行く間隔(秒)。**ふだんは見るだけ**で、
# 書くのは消えていたときだけ(``keep_lock``)
LOCK_KEEP_SEC = 10.0


@dataclass
class LockInfo:
    """``runtime/app.lock`` の中身。"""

    app_id: str
    pid: int
    port: int
    url: str
    started_at: float
    python: str = ""
    app_root: str = ""
    # 起動トークン。``stop.bat`` が「正常終了を要求する」ために要る
    # (基盤仕様書 2.8 は、PIDで落とす前にまずアプリ自身へ頼むことを
    # 求めている)。ロックは利用者ごとのローカル領域にあり、読めるのは
    # 同じ利用者だけ。そこまで入れる相手はプロセスを直接落とせるので、
    # ここに置いても守りの強さは変わらない。逆に、悪意あるWebページは
    # ローカルのファイルを読めないため、トークンによる防御
    # (別オリジンからの操作を弾く)はそのまま効く。
    token: str = ""

    @property
    def started_text(self) -> str:
        return time.strftime("%Y/%m/%d %H:%M:%S", time.localtime(self.started_at))


def lock_path() -> Path:
    """ロックファイルの場所。このアプリは1台につき1つしか起動しない。"""
    return app_config.local_dir("runtime") / "app.lock"


def instance_lock_path() -> Path:
    """**OSに預ける**排他ロックの場所。

    中身は使わない(空でよい)。``app.lock`` と分けてあるのは、
    Windows の ``msvcrt.locking`` がバイト範囲ロックで、
    **同じ範囲を読もうとした側が読めなくなる**ため。合流する側は
    ``app.lock`` の中身(ポート・URL)を読む必要がある。
    """
    return app_config.local_dir("runtime") / "instance.lock"


# ---------------------------------------------------------------------------
# 起動の入口をひとつに絞る (OSの排他ロック)
# ---------------------------------------------------------------------------
class StartupLock:
    """起動しようとしているプロセスを、1台に1つだけ通す。

    【なぜファイルの中身だけでは足りないか】
    ``check_existing()`` は ``app.lock`` を読んで判断するが、その**ロックが
    書かれるのは待ち受けが始まったあと**で、判断から書き込みまでに1〜2秒の
    窓がある。``Start.vbs`` は画面が出ないので「反応が無い」と思った人が
    もう一度ダブルクリックする ── ちょうどこの窓に入る。
    実際に両方とも「ロックなし」と判断して起動し、**2つ動いた**
    (片方は bind に失敗したのに成功と報告して、生きているほうのロックを
    消して去った)。

    【OSに預ける理由】
    自分で「起動中です」という印を置くと、途中で電源が落ちたときに
    **消されない印が残る**。そこを賢く判定しようとするほど壊れやすくなる。
    OSのファイルロックなら、プロセスが死んだ時点で必ず解放される ──
    stale という状態が原理的に生まれない。

        POSIX   : fcntl.flock(LOCK_EX | LOCK_NB)
        Windows : msvcrt.locking(LK_NBLCK)

    どちらも標準ライブラリで、追加のパッケージは要らない。
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path
        self._fd: Optional[int] = None

    @property
    def path(self) -> Path:
        return self._path if self._path is not None else instance_lock_path()

    def acquire(self) -> bool:
        """取れたら ``True``。取れなければ**他が起動中/起動しようとしている**。"""
        if self._fd is not None:
            return True
        path = self.path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            # ロックを置けないことを理由に起動を止めない。**守りが1枚減る
            # だけ**で、ここで止めると誰も起動できなくなる
            log.warning("起動ロックを作れませんでした (%s): %s", path, exc)
            return True
        try:
            _lock_file(fd)
        except OSError:
            os.close(fd)
            return False
        except Exception as exc:                  # noqa: BLE001 - 仕組みが無い環境
            log.warning("起動ロックを掛けられませんでした: %s", exc)
            os.close(fd)
            return True
        self._fd = fd
        return True

    def release(self) -> None:
        """離す。**プロセスが死んでもOSが離す**ので、ここは後始末にすぎない。"""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock_file(fd)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    def __enter__(self) -> "StartupLock":
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()


def _lock_file(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)    # type: ignore[attr-defined]
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)    # type: ignore[attr-defined]
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# ロックファイル
# ---------------------------------------------------------------------------
def write_lock(info: LockInfo) -> Path:
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(info), ensure_ascii=False, indent=2),
                    encoding="utf-8")
    # トークンを含むので、本人だけが読める権限にする。
    # Windows には chmod が効かないが、``%LOCALAPPDATA%`` 自体が
    # 利用者ごとのフォルダなので実質同じ扱いになる
    try:
        os.chmod(path, 0o600)
    except OSError as exc:                        # noqa: BLE001 - 権限設定は best effort
        log.debug("ロックの権限を変更できませんでした: %s", exc)
    log.info("ロックを書きました: %s (pid=%s port=%s)", path, info.pid, info.port)
    return path


def read_lock() -> Optional[LockInfo]:
    """壊れていれば ``None``。読めないことを理由に起動を止めない。"""
    path = lock_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return LockInfo(**{k: raw[k] for k in LockInfo.__dataclass_fields__ if k in raw})
    except FileNotFoundError:
        return None
    except Exception as exc:                      # noqa: BLE001 - 壊れたロックは無視する
        log.warning("ロックを読めませんでした (%s): %s", path, exc)
        return None


def remove_lock(*, only_mine: bool = False) -> None:
    """ロックを消す。

    :param only_mine: **自分が書いたものだけ**消す(終了時はこちら)。

    無条件に消すと、あとから起動して落ちたプロセスが、**生きているほうの
    ロックを消して**去る。実際にそれが起きた ── 残ったのは
    「動いているのにロックが無い」状態で、次の起動が素通りして
    二重起動が増える。
    """
    if only_mine:
        info = read_lock()
        if info is not None and info.pid != os.getpid():
            log.info("ロックは別のプロセス(pid=%s)のものなので消しません", info.pid)
            return
    try:
        lock_path().unlink()
        log.info("ロックを消しました")
    except FileNotFoundError:
        pass
    except OSError as exc:                        # noqa: BLE001
        log.warning("ロックを消せませんでした: %s", exc)


# ---------------------------------------------------------------------------
# プロセスの生死
# ---------------------------------------------------------------------------
def is_process_alive(pid: int) -> bool:
    """そのPIDのプロセスが存在するか。中身までは見ない。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        return _is_alive_windows(pid)
    try:
        os.kill(pid, 0)          # シグナル0は存在確認だけ
    except ProcessLookupError:
        return False
    except PermissionError:
        return True              # 別ユーザーのプロセス = 生きている
    return True


def _is_alive_windows(pid: int) -> bool:
    """Windows では ``tasklist`` で確認する。

    ``OpenProcess`` を ctypes で叩く手もあるが、権限やハンドルの後始末を
    誤ると別の不具合を招く。起動時に1回だけの判定なので、外部コマンドの
    数十msは問題にならない。
    """
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("tasklist を実行できませんでした: %s", exc)
        return True              # 分からないときは「生きている」に倒す
    return f'"{pid}"' in out.stdout


def process_command_line(pid: int) -> str:
    """そのPIDが何を実行しているか。**停止前の確認に使う**。

    基盤仕様書 2.8 は「Pythonをプロセス名だけで一括終了しない」ことを
    求めている。無関係なPythonアプリを巻き添えにしないため、落とす前に
    コマンドラインとアプリの場所を照合する。
    取れなければ空文字(呼び出し側は安全側に倒して落とさない)。
    """
    if pid <= 0:
        return ""
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["wmic", "process", "where", f"ProcessId={pid}", "get",
                 "CommandLine", "/format:list"],
                capture_output=True, text=True, timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            for line in out.stdout.splitlines():
                if line.startswith("CommandLine="):
                    return line.split("=", 1)[1].strip()
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("wmic を実行できませんでした: %s", exc)
        return ""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# 起動確認API
# ---------------------------------------------------------------------------
# 自分自身(127.0.0.1)への通信に**プロキシを通さない**ための送信口。
#
# ``urllib.request.urlopen()`` の既定は、環境変数 ``HTTP_PROXY`` や
# Windowsのインターネット設定からプロキシを拾う。社内PCではたいてい
# プロキシが入っており、除外一覧に ``127.0.0.1`` が無いと、**自分自身への
# 通信までプロキシへ送られて失敗する**。ループバックにプロキシを挟む
# 理由は無いので、ここで明示的に外す。
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def local_request(url: str, *, timeout: float, data: Optional[bytes] = None,
                  method: Optional[str] = None,
                  headers: Optional[dict] = None):
    """127.0.0.1 への要求。プロキシを経由しない。

    このアプリが出す HTTP はこれだけなので、送信口を1つに集約しておく。
    """
    request = urllib.request.Request(url, data=data, method=method,
                                     headers=headers or {})
    return _LOCAL_OPENER.open(request, timeout=timeout)


def probe_health(port: int, *, timeout: float = HEALTH_TIMEOUT_SEC) -> Optional[dict]:
    """``GET /api/health`` を叩く。応答しなければ ``None``。"""
    url = f"http://{app_config.host()}:{port}/api/health"
    try:
        with local_request(url, timeout=timeout) as res:
            return json.loads(res.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None


def is_port_accepting(port: int, *, timeout: float = 0.5) -> bool:
    """TCPで繋がるか。HTTPまでは見ない。

    「待ち受けていない」のか「待ち受けてはいるが応答が返らない」のかを
    分けるために使う。後者はプロキシやセキュリティ製品が挟まっている
    ことが多く、直し方がまったく違う。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((app_config.host(), port)) == 0


def proxy_settings() -> dict[str, str]:
    """いま効いているプロキシ設定。原因調査のためだけに使う。"""
    try:
        return {k: v for k, v in urllib.request.getproxies().items()
                if k in ("http", "https")}
    except Exception:                    # noqa: BLE001 - 調査用なので握る
        return {}


def is_our_app(health: Optional[dict]) -> bool:
    """その応答が**自分と同じアプリ**か。

    ``app_id`` を見ないと、たまたま同じポートを使っている別のアプリを
    自分だと誤認する(基盤仕様書 2.3)。
    """
    if not health:
        return False
    return health.get("app_id") == app_config.app_id()


# ---------------------------------------------------------------------------
# 既存インスタンスの判定
# ---------------------------------------------------------------------------
@dataclass
class GuardResult:
    """起動してよいか。"""

    should_start: bool
    url: str = ""
    existing: Optional[LockInfo] = None
    reason: str = ""
    # 動いているのが**古い版**だった場合、その版。合流してはいけない
    stale_version: str = ""


def check_existing() -> GuardResult:
    """すでに同じアプリが動いていないか調べる。

    動いていれば ``should_start=False`` と、開くべきURLを返す。

    **ただし版が違えば合流しない。** 入れ替えたのに古いプロセスが
    残っていると、ここが「すでに起動しています」と答えて古いほうの
    ブラウザを開き、**新しい版がいつまでも動かない**(画面の版バッジも
    古いままになる)。版が違うときは古いほうを終わらせて、こちらで立て直す。
    """
    info = read_lock()
    if info is None:
        return _search_ports("ロックなし")

    if not is_process_alive(info.pid):
        log.info("死んだロックを掃除します (pid=%s は不在)", info.pid)
        remove_lock()
        return _search_ports(f"ロックは残っていたがpid {info.pid} は不在")

    health = probe_health(info.port)
    if not is_our_app(health):
        # プロセスは居るがアプリではない(PIDの使い回し、または
        # 起動途中で落ちた)。ロックを消して新しく起動する
        log.info("pid=%s は生きているが :%s は自分ではない", info.pid, info.port)
        remove_lock()
        return _search_ports("ロックのプロセスは別物だった")

    running = str((health or {}).get("version", ""))
    mine = app_config.version()
    if running and running != mine:
        return _replace_stale(info, running, mine)

    log.info("すでに起動しています: pid=%s port=%s 版=%s",
             info.pid, info.port, running or "(不明)")
    return GuardResult(False, url=info.url, existing=info,
                       reason="同じアプリが起動中")


def _search_ports(why: str) -> GuardResult:
    """ロックが当てにならないとき、**常用ポートに自分が居ないか**を見る。

    ロックファイルは消える ── 手で消された、掃除ツールに消された、
    あとから落ちたプロセスに消された(``remove_lock`` の説明)、
    ローカル領域の場所が変わった。そのたびに「ロックが無い = 誰も
    居ない」と決めると、**8730 で自分が動いているのに 8731 に
    もう1つ立てる**。実際にそうなった。

    ポートを1つずつ叩くだけなので、判断の材料をロック1つに預けない。

    **ここでロックを書き直さない。** 外から分かるのはポートまでで、
    起動トークンは分からない。トークンの無いロックで上書きすると、
    ``stop.bat`` と版の入れ替えが「止められない」状態に落ちる。
    書き直すのは**持ち主の役目**(``keep_lock``)。
    """
    for port in app_config.port_candidates():
        health = probe_health(port, timeout=0.6)
        if not is_our_app(health):
            continue

        running = str((health or {}).get("version", ""))
        pid = int((health or {}).get("pid", 0) or 0)
        url = f"http://{app_config.host()}:{port}/"
        found = LockInfo(app_id=app_config.app_id(), pid=pid, port=port,
                         url=url, started_at=time.time())
        if running and running != app_config.version():
            return _replace_stale(found, running, app_config.version())

        log.warning("ロックは当てになりませんでしたが(%s)、"
                    ":%s で自分が動いていました。合流します", why, port)
        return GuardResult(False, url=url, existing=found,
                           reason=f"{why} / :{port} で起動中のものを見つけました")
    return GuardResult(True, reason=why)


def _replace_stale(info: LockInfo, running: str, mine: str) -> GuardResult:
    """動いているのが違う版。**終わらせてから立て直す。**

    処理の途中(取り込みなど)なら終わらせない ── 中途半端なデータを
    残すほうが害が大きいので、そのときは合流して次の起動に任せる。
    """
    log.warning("動いているのは別の版です(動作中=%s / ファイル=%s)。"
                "古いほうを終わらせます", running, mine)
    if not request_shutdown(info.port, info.token):
        log.warning("古い版を終わらせられませんでした。合流します")
        return GuardResult(False, url=info.url, existing=info,
                           stale_version=running,
                           reason=f"別の版({running})が動いていますが止められません")

    deadline = time.monotonic() + REPLACE_WAIT_SEC
    while time.monotonic() < deadline:
        if probe_health(info.port, timeout=0.3) is None:
            remove_lock()
            log.info("古い版(%s)を終わらせました。%s で立て直します", running, mine)
            return GuardResult(True, stale_version=running,
                               reason=f"別の版({running})を終わらせました")
        time.sleep(0.2)

    log.warning("古い版が %.0f秒 で終わりませんでした。合流します", REPLACE_WAIT_SEC)
    return GuardResult(False, url=info.url, existing=info,
                       stale_version=running,
                       reason=f"別の版({running})が動いています")


def request_shutdown(port: int, token: str) -> bool:
    """``POST /api/shutdown`` を送る。受け付けられたら ``True``。

    **``force`` は付けない。** 同期の途中なら 409 が返り、そのときは
    止めない(中途半端なデータを残さない)。``process_manager`` にも同じ
    処理があるが、あちらは停止そのものが仕事で、こちらは**起動の途中で
    古い版をどけるだけ**。こちらから ``process_manager`` を読むと、起動の
    入口が停止側のモジュールに依存する(待機画面より前なので import を
    増やさない)。
    """
    body = json.dumps({"force": False}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Tool-Token"] = token
    try:
        with local_request(f"http://{app_config.host()}:{port}/api/shutdown",
                           timeout=5, data=body, method="POST",
                           headers=headers) as res:
            return bool(json.loads(res.read().decode("utf-8")).get("stopped"))
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            log.info("古い版は処理の途中なので止めません")
        else:
            log.info("古い版の停止要求が %s で断られました", exc.code)
        return False
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.info("古い版へ停止要求を送れませんでした: %s", exc)
        return False


# ---------------------------------------------------------------------------
# ポート
# ---------------------------------------------------------------------------
def is_port_free(port: int, host: str = "") -> bool:
    """そのポートで待ち受けを開始できるか。

    ``SO_REUSEADDR`` は**付けない**。付けると TIME_WAIT のポートまで
    「空いている」と判定してしまい、実際の bind で失敗する。
    """
    host = host or app_config.host()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def pick_port() -> Optional[int]:
    """使えるポートを1つ選ぶ。全部塞がっていれば ``None``。"""
    for port in app_config.port_candidates():
        if is_port_free(port):
            return port
        log.info("ポート %s は使用中です", port)
    return None


# ---------------------------------------------------------------------------
# 起動時の記録
# ---------------------------------------------------------------------------
def keep_lock(info: LockInfo, stop: "threading.Event",
              interval: float = LOCK_KEEP_SEC) -> "threading.Thread":
    """自分のロックが消えていたら書き直し続ける。

    【なぜ要るのか】
    ロックは**動いている自分についての公示**で、他のプロセスはこれを見て
    合流するかどうかを決める。ところがファイルは消える ── 手で消された、
    掃除ツールに消された、古い版のプロセスに持っていかれた。

    消えたままだと、外から分かるのは「ポートで応答している」ことだけで
    **起動トークンが失われる**。すると ``stop.bat`` も、版の入れ替えも
    「止められない」状態になり、**新しい版を入れた日にいつまでも古い版が
    開く**。実際にそうなった(403 で断られて合流した)。

    書き直すのは持ち主にしかできない(トークンを知っているのは自分だけ)。
    ふだんは ``exists()`` を見るだけで、書くのは消えていたときだけ。
    """
    import threading

    def loop() -> None:
        while not stop.wait(interval):
            try:
                current = read_lock()
                if current is not None and current.pid == info.pid:
                    continue                      # 自分のものがある
                if current is not None and is_process_alive(current.pid):
                    # 別の生きているプロセスのもの。**奪わない**
                    continue
                log.warning("ロックが失われていたので書き直します")
                write_lock(info)
            except Exception as exc:              # noqa: BLE001 - 見張りを止めない
                log.warning("ロックを見張れませんでした: %s", exc)

    thread = threading.Thread(target=loop, name="lock-keeper", daemon=True)
    thread.start()
    return thread


def build_lock_info(port: int, token: str = "") -> LockInfo:
    return LockInfo(
        app_id=app_config.app_id(),
        pid=os.getpid(),
        port=port,
        url=f"http://{app_config.host()}:{port}/",
        started_at=time.time(),
        python=sys.executable,
        app_root=str(app_config.APP_ROOT),
        token=token,
    )
