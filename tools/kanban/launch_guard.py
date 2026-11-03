"""多重起動の防止とインスタンス管理 (基盤仕様書 2.4)

同じアプリが二重に起動すると、ポート競合・二重処理・設定ファイル競合が
起きる。看板システムの場合はさらに悪く、**Access への書き戻しが同じ端末
から2重に走る**(ローカル SQLite の ``dirty`` を2つのプロセスが取り合う)。

そこで起動ごとに**ロックファイル**(PID・ポート・起動時刻・mode)を書き、
次の起動はそれを見て判断する::

    生きている同じアプリが居る → 新しく起動せず、ブラウザだけ開く
    死んだロックが残っている   → 消して続行する(記録は残す)

「生きているか」の判定は2段構えにする:

1. PID のプロセスが存在するか
2. そのポートの ``/api/health`` が**同じ app_id と mode** を返すか

1 だけでは足りない。PID は使い回されるので、無関係なプロセスが同じ番号を
持っていることがある。2 だけでも足りない。別のアプリが HTTP を返している
場合があるので、``app_id`` の照合が要る(基盤仕様書 2.3)。

このモジュールは Flask に依存しない。起動の判断だけを持つので、サーバが
立たない状況でも動く。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from kanban import app_config, config
from kanban.applog import get_logger

#: Windows のコマンド(tasklist・wmic)の出力の文字コード。**コンソールの文字コード(oem)で読む。**
#: 日本語の Windows では Shift-JIS(「情報: 指定された条件に一致するタスクは…」)。Python の
#: 既定(UTF-8 モードのとき UTF-8)で読むと、読めずに落ちて起動が止まっていた。読めない字は置き換える
CONSOLE_ENCODING = "oem" if os.name == "nt" else None

log = get_logger("launch_guard")

#: ``/api/health`` を叩くときの待ち時間(秒)。ローカルなので短くてよい。
#: 長いと、死んだロックの掃除に毎回この分だけ待たされる
HEALTH_TIMEOUT_SEC = 1.5


@dataclass
class LockInfo:
    """``runtime/<mode>.lock`` の中身。"""

    app_id: str
    mode: str
    pid: int
    port: int
    url: str
    started_at: float
    python: str = ""
    app_root: str = ""
    # 起動トークン。``stop.bat`` が「正常終了を要求する」ために要る
    # (基盤仕様書 2.8 は、PID で落とす前にまずアプリ自身へ頼むことを
    # 求めている)。ロックは利用者ごとのローカル領域にあり、読めるのは
    # 同じ利用者だけ。そこまで入れる相手はプロセスを直接落とせるので、
    # ここに置いても守りの強さは変わらない。逆に、悪意ある Web ページは
    # ローカルのファイルを読めないため、トークンによる防御(別オリジン
    # からの操作を弾く)はそのまま効く。
    token: str = ""
    #: プロセスの起動した時刻(エポック秒。Windows は GetProcessTimes の作成時刻)。**PID は
    #: 使い回される**ので、PID と起動時刻の両方が合うときだけ「動いている」とみなす(:func:`lock_owner`)。
    #: 0 は前の版の印(起動時刻が無い)
    process_started: float = 0.0

    @property
    def started_text(self) -> str:
        return time.strftime("%Y/%m/%d %H:%M:%S", time.localtime(self.started_at))


def lock_path(mode: str) -> Path:
    """mode ごとに別のロック。

    **モードごとに分けてあるのは、同時に動かしてよいからではありません。**
    どのモードで動いているのかをロックの置き場所でも表すため、そして
    入れ替えのときに「どのモードを終わらせたか」を残すためです。
    この PC で動いてよいのは 1 つだけで、その判断は
    :func:`check_existing` が**全モードのロックを見て**行います。
    """
    if mode not in app_config.MODE_KEYS:
        raise ValueError(f"未知のmode: {mode!r}")
    return app_config.local_dir("runtime") / f"{mode}.lock"


# ------------------------------------------------------------------
# ロックファイル
# ------------------------------------------------------------------
def write_lock(info: LockInfo) -> Path:
    path = lock_path(info.mode)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(info), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # トークンを含むので、本人だけが読める権限にする。Windows には chmod が
    # 効かないが、``%LOCALAPPDATA%`` 自体が利用者ごとのフォルダなので
    # 実質同じ扱いになる
    try:
        os.chmod(path, 0o600)
    except OSError as exc:  # noqa: BLE001 - 権限設定は best effort
        log.debug("ロックの権限を変更できませんでした: %s", exc)
    log.info("ロックを書きました: %s (pid=%s port=%s)", path, info.pid, info.port)
    return path


def read_lock(mode: str) -> LockInfo | None:
    """壊れていれば ``None``。読めないことを理由に起動を止めない。"""
    path = lock_path(mode)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return LockInfo(
            **{k: raw[k] for k in LockInfo.__dataclass_fields__ if k in raw}
        )
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001 - 壊れたロックは無視する
        log.warning("ロックを読めませんでした (%s): %s", path, exc)
        return None


def remove_lock(mode: str) -> None:
    try:
        lock_path(mode).unlink()
        log.info("ロックを消しました: %s", mode)
    except FileNotFoundError:
        pass
    except OSError as exc:  # noqa: BLE001
        log.warning("ロックを消せませんでした: %s", exc)


def claim_lock(info: LockInfo) -> bool:
    r"""**まだ誰も立てていなければ**印を立てる。立てられたら ``True``。

    以前は「調べる → ポートを取る → 待ち受けを始める → 印を書く」の順で、
    調べてから印を書くまでに 1 秒以上空いていた。その間にもう一度起動すると
    (バッチを 2 回押す・ショートカットを連打する)、2 つとも「印が無い」を見て
    先へ進み、``pick_port`` が塞がったポートを避けて**隣の番号へずれる**ので、
    8741 と 8742 で静かに 2 つ動く(日報管理ツールで実際に起きた。同じ直し方)。

    そこで**ポートを取る前に**印を立てる。作るのと「無いことを確かめる」のを
    ``O_CREAT | O_EXCL`` の 1 回にまとめるので、同時に来ても立てられるのは 1 つだけ。
    ポートはまだ決まっていないので 0 で立て、決まってから :func:`update_lock` で書き足す。
    """
    path = lock_path(info.mode)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(asdict(info), ensure_ascii=False, indent=2)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        log.info("印はすでに立っています: %s", path)
        return False
    except OSError as exc:  # noqa: BLE001
        # 印を立てられないことを理由に起動そのものを止めない
        # (二重起動より、一度も起動できないことのほうが困る)
        log.warning("印を立てられませんでした (%s): %s。続行します", path, exc)
        return True
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
    except OSError as exc:  # noqa: BLE001
        log.warning("印の中身を書けませんでした: %s", exc)
    log.info("印を立てました: %s (pid=%s)", path, info.pid)
    return True


def update_lock(mode: str, **changes) -> LockInfo | None:
    """自分が立てた印に、あとから決まったこと(ポート・トークン・URL)を書く。

    **自分の印でなければ触らない。** 上書きすると、動いているほうの居場所が消える。
    """
    info = read_lock(mode)
    if info is None:
        return None
    if info.pid != os.getpid():
        log.warning("自分の印ではないので書き換えません (pid=%s)", info.pid)
        return info
    for key, value in changes.items():
        setattr(info, key, value)
    write_lock(info)
    return info


def release_lock(mode: str) -> None:
    """**自分が立てた印だけ**を片付ける。

    終わるときに無条件で消すと、入れ替えのときに困る ── 古いほうが終わる頃には
    新しいほうがもう印を立てているので、古いほうの後始末が新しいほうの印を消し、
    次の起動で「誰も居ない」と判定されて 2 つ目が立つ(日報・カレンダーで直した不具合)。
    """
    info = read_lock(mode)
    if info is None:
        return
    if info.pid != os.getpid():
        log.info("自分の印ではないので残します: %s (pid=%s)", mode, info.pid)
        return
    remove_lock(mode)


# ------------------------------------------------------------------
# プロセスの生死
# ------------------------------------------------------------------
def is_process_alive(pid: int) -> bool:
    """その PID のプロセスが存在するか。中身までは見ない。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        return _is_alive_windows(pid)
    try:
        os.kill(pid, 0)  # シグナル0は存在確認だけ
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 別ユーザーのプロセス = 生きている
    return True


def _is_alive_windows(pid: int) -> bool:
    """Windows では ``tasklist`` で確認する。

    ``OpenProcess`` を ctypes で叩く手もあるが、権限やハンドルの後始末を
    誤ると別の不具合を招く。起動時に1回だけの判定なので、外部コマンドの
    数十 ms は問題にならない。
    """
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True,
            text=True, encoding=CONSOLE_ENCODING, errors="replace",
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("tasklist を実行できませんでした: %s", exc)
        return True  # 分からないときは「生きている」に倒す
    return f'"{pid}"' in (out.stdout or "")


def process_command_line(pid: int) -> str:
    """その PID が何を実行しているか。**停止前の確認に使う**。

    基盤仕様書 2.8 は「Python をプロセス名だけで一括終了しない」ことを
    求めている。無関係な Python アプリを巻き添えにしないため、落とす前に
    コマンドラインとアプリの場所を照合する。取れなければ空文字(呼び出し側は
    安全側に倒して落とさない)。
    """
    if pid <= 0:
        return ""
    if os.name == "nt":
        try:
            out = subprocess.run(
                [
                    "wmic", "process", "where", f"ProcessId={pid}",
                    "get", "CommandLine", "/format:list",
                ],
                capture_output=True,
                text=True, encoding=CONSOLE_ENCODING, errors="replace",
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            for line in (out.stdout or "").splitlines():
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


# ------------------------------------------------------------------
# 印の PID が、印を書いた本人のままか(PID の使い回し)
# ------------------------------------------------------------------
# 印には PID を書き、「その PID が生きていれば動いている」と見ていた。**Windows は PID を
# 使い回す。** 動いたまま PC を終了・再起動すると印が残り、再起動のあとで**まったく別の
# プログラムが同じ番号を持つ**ことがある。すると「起動中」と見続けて画面に接続できず、
# stop.bat も「接続できない」で終わって印を消さなかった(別のツールで見つかった不具合)。
#
#     印に書くもの   PID と、そのプロセスの**起動した時刻**(`process_started`。エポック秒)
#     本人とみなす   PID が生きていて、起動時刻が印のものと合うときだけ
#     前の版の印     起動時刻が無い。「印を書いた時刻(ファイルの更新時刻)より後に起動した
#                    プロセスは、書いた本人ではない」とみなす(本人は書く前に起動している)
#
# 本人が `NO_ANSWER_WAIT_SEC` 秒たっても答えないときは、**本人と確かめたうえで**終わらせて
# 起動し直す(stop.bat も同じ)。関係のないプログラムは終わらせない。
# 日報複合ツールの入口(`portal/proc_identity.py`)と同じ考え。直すときは揃える。

#: 起動時刻を比べるときの許し(秒)。Windows は同じ値が返る。Linux は btime が秒単位
START_TOLERANCE_SEC = 2.0
#: 印の本人が答えないとき、答えるのを待つ上限(秒)。過ぎたら本人を終わらせる
NO_ANSWER_WAIT_SEC = 5.0
#: 残っていた印を片付けたときのログ(stop.bat・起動。日報複合ツールの入口と同じ言葉)
CLEANED = "片付けました。Start.vbs で起動できます"

OWNER_GONE, OWNER_SAME, OWNER_OTHER, OWNER_UNKNOWN = "gone", "same", "other", "unknown"

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    _k32.GetProcessTimes.restype = wintypes.BOOL
    _k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _k32.WaitForSingleObject.restype = wintypes.DWORD
    _k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.TerminateProcess.restype = wintypes.BOOL
    _QUERY = 0x1000                 # PROCESS_QUERY_LIMITED_INFORMATION
    _TERMINATE = 0x0001
    _SYNCHRONIZE = 0x00100000

    def _handle_started(handle) -> Optional[float]:
        if _k32.WaitForSingleObject(handle, 0) == 0:          # もう終わっている
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not _k32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        return filetime_to_epoch((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)

    def _started(pid: int) -> Optional[float]:
        handle = _k32.OpenProcess(_QUERY | _SYNCHRONIZE, False, int(pid))
        if not handle:
            return None             # 居ない・開けない(同じ利用者のツールなら必ず開ける)
        try:
            return _handle_started(handle)
        finally:
            _k32.CloseHandle(handle)

    def _terminate(pid: int, matches, wait_sec: float) -> bool:
        """開いた握りで起動時刻を確かめてから止める(確かめてから止めるまでに PID が入れ替わらない)。"""
        handle = _k32.OpenProcess(_TERMINATE | _QUERY | _SYNCHRONIZE, False, int(pid))
        if not handle:
            return False
        try:
            started = _handle_started(handle)
            if started is None:
                return True
            if not matches(started):
                return False
            _k32.TerminateProcess(handle, 1)
            return _k32.WaitForSingleObject(handle, int(max(0.0, wait_sec) * 1000)) == 0
        finally:
            _k32.CloseHandle(handle)

else:
    def _boot_time() -> Optional[float]:
        try:
            with open("/proc/stat", encoding="ascii") as fh:
                for line in fh:
                    if line.startswith("btime "):
                        return float(line.split()[1])
        except (OSError, ValueError):
            pass
        return None

    def _started(pid: int) -> Optional[float]:
        try:
            with open(f"/proc/{int(pid)}/stat", encoding="utf-8", errors="replace") as fh:
                stat = fh.read()
        except OSError:
            return None
        fields = stat[stat.rfind(")") + 2:].split()     # 名前に空白があっても崩れない
        boot = _boot_time()
        if len(fields) < 20 or fields[0] == "Z" or boot is None:
            return None
        try:
            return boot + int(fields[19]) / os.sysconf("SC_CLK_TCK")
        except (ValueError, OSError):
            return None

    def _terminate(pid: int, matches, wait_sec: float) -> bool:
        import signal

        started = _started(pid)
        if started is None:
            return True
        if not matches(started):
            return False
        for sig in (signal.SIGTERM, getattr(signal, "SIGKILL", signal.SIGTERM)):
            try:
                os.kill(int(pid), sig)
            except ProcessLookupError:
                return True
            except OSError:
                return False
            deadline = time.monotonic() + max(0.0, wait_sec) / 2
            while time.monotonic() < deadline:
                if _started(pid) is None:
                    return True
                time.sleep(0.1)
        return _started(pid) is None


def filetime_to_epoch(filetime: int) -> float:
    """Windows の FILETIME(1601年からの 100ns)をエポック秒にする。"""
    return filetime / 10_000_000 - 11_644_473_600


def process_started(pid: int) -> Optional[float]:
    """その PID のプロセスが起動した時刻(エポック秒)。居ない・確かめられないときは None。"""
    if not pid or int(pid) <= 0:
        return None
    try:
        return _started(int(pid))
    except Exception:                             # noqa: BLE001 - 確かめられない
        return None


def my_started() -> float:
    """自分の起動時刻(印に書く)。確かめられない環境では 0(前の版の印と同じ扱いになる)。"""
    return process_started(os.getpid()) or 0.0


def _start_matches(recorded: float, written: Optional[float]):
    def matches(started: float) -> Optional[bool]:
        if recorded:
            return abs(started - float(recorded)) <= START_TOLERANCE_SEC
        if written is None:
            return None
        return started <= float(written) + START_TOLERANCE_SEC   # 本人は印を書く前に起動している
    return matches


def owner_state(pid: int, recorded: float = 0.0, written: Optional[float] = None) -> str:
    """印の PID が書いた本人か。

    `OWNER_GONE` 居ない / `OWNER_SAME` 本人 / `OWNER_OTHER` 別のプロセス(PID の使い回し) /
    `OWNER_UNKNOWN` 確かめられない(呼び出し側は今までどおり PID の生き死にだけで決める)。
    `recorded` は印の起動時刻(無ければ 0)、`written` は印の更新時刻(読めなければ None)。
    """
    if int(pid or 0) == os.getpid():
        return OWNER_UNKNOWN                      # 自分自身の印(起動時刻で見分けることではない)
    if not is_process_alive(int(pid or 0)):
        return OWNER_GONE
    started = process_started(pid)
    if started is None:
        # 自分のは取れるのに相手のが取れない = 居ない・別の利用者・守られたプロセス。
        # 同じ利用者のツールなら必ず取れるので、書いた本人ではない
        return OWNER_OTHER if process_started(os.getpid()) is not None else OWNER_UNKNOWN
    verdict = _start_matches(recorded, written)(started)
    if verdict is None:
        return OWNER_UNKNOWN
    return OWNER_SAME if verdict else OWNER_OTHER


def terminate_owner(pid: int, recorded: float = 0.0, written: Optional[float] = None,
                    wait_sec: float = 5.0) -> bool:
    """**本人と確かめたプロセスだけ**を止める。止まった(居なくなった)ら True。"""
    if int(pid or 0) == os.getpid():
        return False                              # 自分は止めない
    check = _start_matches(recorded, written)
    try:
        return _terminate(int(pid), lambda started: bool(check(started)), wait_sec)
    except Exception:                             # noqa: BLE001
        return False


def file_mtime(path) -> Optional[float]:
    """印の更新時刻(前の版の印の判定に使う)。読めなければ None。"""
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def lock_owner(info: LockInfo) -> str:
    """印(``<mode>.lock``)の PID が、印を書いた本人のままか(:func:`owner_state`)。"""
    return owner_state(info.pid, info.process_started, file_mtime(lock_path(info.mode)))


def remove_lock_if(info: LockInfo) -> bool:
    """読んだ印が**まだそのまま**なら消す。

    読んでから消すまでに、新しく起動したほうが印を立てていたら消さない。**読めない印も
    消さない**(1.5.79〜1.5.81 の決まり: 読めないのを「無い・壊れている」と取り違えない)。
    """
    now = read_lock(info.mode)
    if now is None or now.pid != info.pid or now.started_at != info.started_at:
        return False
    remove_lock(info.mode)
    return True


def _end_unanswered(info: LockInfo) -> bool:
    """印の本人(PID と起動時刻が合う)が答えない。**本人だけ**を終わらせ、印を片付ける。"""
    log.warning("pid=%s (%s:%s) が応答しません。終わらせて起動し直します",
                info.pid, info.mode, info.port)
    if not terminate_owner(info.pid, info.process_started, file_mtime(lock_path(info.mode))):
        log.warning("pid=%s を終わらせられませんでした", info.pid)
        return False
    remove_lock_if(info)
    log.info("応答しない前の起動を終わらせました。%s", CLEANED)
    return True


# ------------------------------------------------------------------
# 起動確認 API
# ------------------------------------------------------------------
# 自分自身(127.0.0.1)への通信に**プロキシを通さない**ための送信口。
#
# ``urllib.request.urlopen()`` の既定は、環境変数 ``HTTP_PROXY`` や
# Windows のインターネット設定からプロキシを拾う。社内 PC ではたいてい
# プロキシが入っており、除外一覧に ``127.0.0.1`` が無いと、**自分自身への
# 通信までプロキシへ送られて失敗する**。ループバックにプロキシを挟む理由は
# 無いので、ここで明示的に外す。
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def local_request(
    url: str,
    *,
    timeout: float,
    data: bytes | None = None,
    method: str | None = None,
    headers: dict | None = None,
):
    """127.0.0.1 への要求。プロキシを経由しない。

    このアプリが自分自身へ出す通信はこれだけなので、送信口を1つに集約する。
    """
    request = urllib.request.Request(
        url, data=data, method=method, headers=headers or {}
    )
    return _LOCAL_OPENER.open(request, timeout=timeout)


def probe_health(port: int, *, timeout: float = HEALTH_TIMEOUT_SEC) -> dict | None:
    """``GET /api/health`` を叩く。応答しなければ ``None``。"""
    url = f"http://{app_config.host()}:{port}/api/health"
    try:
        with local_request(url, timeout=timeout) as res:
            return json.loads(res.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None


def is_port_accepting(port: int, *, timeout: float = 0.5) -> bool:
    """TCP で繋がるか。HTTP までは見ない。

    「待ち受けていない」のか「待ち受けてはいるが応答が返らない」のかを
    分けるために使う。後者はプロキシやセキュリティ製品が挟まっていることが
    多く、直し方がまったく違う。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((app_config.host(), port)) == 0


def proxy_settings() -> dict[str, str]:
    """いま効いているプロキシ設定。原因調査のためだけに使う。"""
    try:
        return {
            k: v
            for k, v in urllib.request.getproxies().items()
            if k in ("http", "https")
        }
    except Exception:  # noqa: BLE001 - 調査用なので握る
        return {}


def is_our_app(health: dict | None, mode: str) -> bool:
    """その応答が**自分と同じアプリの同じ mode** か。

    ``app_id`` を見ないと、たまたま同じポートを使っている別のアプリを自分だと
    誤認する(基盤仕様書 2.3)。mode まで見るのは、現場・倉庫・倉庫参照が別
    ポートで動く設計なので、取り違えると操作できる範囲が変わってしまうため。
    """
    if not health:
        return False
    return health.get("app_id") == app_config.app_id() and health.get("mode") == mode


# ------------------------------------------------------------------
# 既存インスタンスの判定
# ------------------------------------------------------------------
@dataclass
class GuardResult:
    """起動してよいか。"""

    should_start: bool
    url: str = ""
    existing: LockInfo | None = None
    reason: str = ""
    #: 動いているのが**古い版**だった場合、その版。合流してはいけない
    stale_version: str = ""


#: 古い版が動いていたとき、終わるのを待つ上限(秒)
REPLACE_WAIT_SEC = 10.0


def check_existing(mode: str) -> GuardResult:
    """すでに同じアプリが動いていないか調べる。

    動いていれば ``should_start=False`` と、開くべき URL を返す。

    **この PC で動いてよいのは 1 つだけ**なので、これから開こうとしている
    モードだけでなく**すべてのモードのロックを見ます**。モードごとにしか
    見ていなかったころは、こうなりました:

    1. 現場モードで起動(``site.lock`` / ポート 8741)
    2. 設定画面でモードを倉庫に変える
    3. もう一度ダブルクリック → ``warehouse.lock`` は無いので**2 つめが起動**

    しかも 2 で名乗りだけ倉庫に変わると、``site.lock`` の相手が「倉庫です」と
    答えるので ``is_our_app`` が別人と判断し、ロックを消して**3 つめ**まで
    立ち上がりました。名乗りを変えないようにしたうえで、ここでも全部見ます。

    見つけたものの扱いは 3 通りです。

    * **同じモード・同じ版** … 合流する(そのブラウザを開く)
    * **違う版** … 古いほうを終わらせて立て直す(更新の経路)
    * **違うモード** … 同じく終わらせて立て直す。モードはポートとロックに
      結びついているので、走ったまま入れ替えられない
    """
    mine = app_config.version()
    for other in _live_instances():
        if other.starting:
            return GuardResult(
                False, url=other.info.url, existing=other.info,
                reason="ほぼ同時に起動されたもう 1 つが起動の途中です",
            )
        if other.mode != mode:
            # モードを変えたあとの起動。**古いモードを終わらせて入れ替える**
            return _replace(
                other.mode, other.info,
                f"別のモード({config.mode_display_name(other.mode)})",
            )
        if other.version and other.version != mine:
            return _replace(
                mode, other.info, f"別の版({other.version})",
                stale_version=other.version,
            )
        log.info(
            "すでに起動しています: %s (pid=%s port=%s 版=%s)",
            mode, other.info.pid, other.info.port, other.version or "(不明)",
        )
        return GuardResult(
            False, url=other.info.url, existing=other.info,
            reason="同じアプリが起動中",
        )
    running = find_running()
    if running:
        found_mode, port, health = running[0]
        url = f"http://{app_config.host()}:{port}/"
        log.warning("印はありませんが、%d 個が待ち受けています: %s",
                    len(running), [(m, p) for m, p, _ in running])
        return GuardResult(
            False, url=url,
            reason=(f"印はありませんが、{config.mode_display_name(found_mode)}が"
                    f"ポート {port} で動いていました(pid={health.get('pid', '?')})"),
        )
    return GuardResult(True, reason="動いているものは無い")


@dataclass
class _Live:
    """いま生きている、このアプリのプロセス 1 つ。"""

    mode: str
    info: LockInfo
    version: str
    #: 印は立っているがまだ待ち受けていない(ポートを取る前の印)
    starting: bool = False


#: 起動の途中の印(ポート 0)を、答えるようになるまで待つ上限(秒)
STARTUP_GRACE_SEC = 45.0
POLL_INTERVAL_SEC = 0.3


def _wait_until_answers(mode: str, info: LockInfo) -> dict | None:
    """ポートを取る前の印なら、答えるようになるまで**猶予の残りぶんだけ**待つ。

    相手の起動が終われば合流でき、押した人には「押したら画面が出た」ようにしか見えない。
    猶予を過ぎた印は待たない(待っても答えない)。
    """
    left = STARTUP_GRACE_SEC - (time.time() - info.started_at)
    deadline = time.monotonic() + max(0.0, left)
    while True:
        fresh = read_lock(mode)
        if fresh is None or fresh.pid != info.pid or not is_process_alive(info.pid):
            return None
        if fresh.port:
            health = probe_health(fresh.port)
            if is_our_app(health, mode):
                return health
        if time.monotonic() >= deadline:
            return None
        time.sleep(POLL_INTERVAL_SEC)


def _wait_answer(mode: str, info: LockInfo) -> dict | None:
    """印の本人が答えるのを ``NO_ANSWER_WAIT_SEC`` 秒まで待つ。答えたら ``/api/health`` の中身。"""
    deadline = time.monotonic() + NO_ANSWER_WAIT_SEC
    while True:
        fresh = read_lock(mode) or info
        health = probe_health(fresh.port) if fresh.port else None
        if is_our_app(health, mode):
            return health
        if time.monotonic() >= deadline:
            return None
        time.sleep(POLL_INTERVAL_SEC)


def find_running() -> list[tuple[str, int, dict]]:
    """**印を当てにせず、ポートを叩いて自分を探す** ``[(mode, port, health)]``。

    印が無い(消えた・書く前に落ちた)のに自分が待ち受けていると、``pick_port`` は
    「塞がっているのは別のアプリ」と読んで隣の番号へずれ、2 つ目が静かに立つ
    (日報管理ツールの ``find_running`` と同じ直し方)。
    """
    found: list[tuple[str, int, dict]] = []
    for mode in app_config.MODE_KEYS:
        for port in app_config.port_candidates(mode):
            if is_port_free(port):
                continue
            health = probe_health(port, timeout=0.6)
            if is_our_app(health, mode):
                found.append((mode, port, health or {}))
    return found


def _live_instances() -> list[_Live]:
    """この PC で生きている、このアプリのプロセスを全部返す。

    ついでに**死んだロックを掃除します** ── 残しておくと、次の起動が
    毎回ここで無駄に足踏みします。
    """
    live: list[_Live] = []
    for other in app_config.MODE_KEYS:
        info = read_lock(other)
        if info is None:
            continue
        state = lock_owner(info)
        if state == OWNER_GONE:
            log.info("死んだロックを掃除します: %s (pid=%s は不在)", other, info.pid)
            if remove_lock_if(info):
                log.info(CLEANED)
            continue
        if state == OWNER_OTHER:
            # **PID の使い回し。** PC の終了・再起動で残った印の番号を、別のプログラムが持っている。
            # ポート 0 の印(起動の途中で切れた)を「起動の途中」と見続けると、二度と起動できない
            log.info("残っていた印を片付けます: %s (pid=%s は別のプロセスです。起動時刻が合いません)",
                     other, info.pid)
            if remove_lock_if(info):
                log.info(CLEANED)
            continue
        if not info.port:
            # **ポートを取る前に立てた印**(:func:`claim_lock`)。相手は起動の途中なので、
            # 答えないのを「別人」と読んで消さない ── 消すと 2 つ立つ
            health = _wait_until_answers(other, info)
            if health is None:
                if (state == OWNER_SAME and time.time() - info.started_at > STARTUP_GRACE_SEC
                        and _wait_answer(other, info) is None and _end_unanswered(info)):
                    continue                      # 立ち上がりきらずに固まった本人を終わらせた
                log.info("%s は起動の途中です (pid=%s)。二重に立てないため合流します", other, info.pid)
                live.append(_Live(other, info, "", starting=True))
                continue
            info = read_lock(other) or info
            live.append(_Live(other, info, str(health.get("version", ""))))
            continue
        health = probe_health(info.port)
        if not is_our_app(health, other) and state == OWNER_SAME:
            # 起動時刻まで合った本人が答えない。少し待って、答えなければ本人だけを終わらせる
            health = _wait_answer(other, info)
            if health is None and _end_unanswered(info):
                continue
        if not is_our_app(health, other):
            # プロセスは居るがこのアプリではない(PID の使い回し、または
            # 起動途中で落ちた)
            log.info("pid=%s は生きているが %s:%s は自分ではない",
                     info.pid, other, info.port)
            remove_lock(other)
            continue
        live.append(_Live(other, info, str((health or {}).get("version", ""))))
    return live


def _replace(
    mode: str, info: LockInfo, what: str, *, stale_version: str = ""
) -> GuardResult:
    """動いているものと入れ替える。**終わらせてから立て直す。**

    ``what`` は入れ替える理由(「別の版(3.0.0)」「別のモード(倉庫モード)」)。
    そのまま利用者の目に入るので、**何が起きたか**が読める言葉にする。

    処理の途中(共有DBへの取り込み・書き戻しなど)なら終わらせません ──
    中途半端な書き戻しを残すほうが害が大きいので、そのときは合流して、
    次の起動に任せます。
    """
    log.warning("%sが動いています(pid=%s port=%s)。終わらせます",
                what, info.pid, info.port)
    if not request_shutdown(info.port, info.token):
        log.warning("%sを終わらせられませんでした。合流します", what)
        return GuardResult(
            False, url=info.url, existing=info, stale_version=stale_version,
            reason=f"{what}が動いていますが止められません",
        )

    deadline = time.monotonic() + REPLACE_WAIT_SEC
    while time.monotonic() < deadline:
        if probe_health(info.port, timeout=0.3) is None:
            remove_lock(mode)
            log.info("%sを終わらせました。立て直します", what)
            return GuardResult(
                True, stale_version=stale_version,
                reason=f"{what}を終わらせました",
            )
        time.sleep(0.2)

    log.warning("%sが %.0f秒 で終わりませんでした。合流します", what, REPLACE_WAIT_SEC)
    return GuardResult(
        False, url=info.url, existing=info, stale_version=stale_version,
        reason=f"{what}が動いています",
    )


def request_shutdown(port: int, token: str, *, force: bool = False) -> bool:
    """``POST /api/shutdown`` を送る。受け付けられたら ``True``。

    既定では **``force`` を付けません。** 取り込み・書き戻しの途中なら 409 が
    返り、そのときは止めません(中途半端なデータを残さない)。

    ``force=True`` は「処理中でも構わないから閉じてくれ」と頼むもので、
    **プロセスを殺すのとは違います。** アプリ自身に閉じさせれば、終了処理
    (最後の書き戻し)が走ります。``process_manager`` はまずこれを試してから
    PID を使います。

    ``process_manager`` にも似た処理がありますが、あちらは停止そのものが
    仕事で、こちらは**起動の途中で古い版をどけるだけ**です。こちらから
    ``process_manager`` を読むと、起動の入口が停止側のモジュールに依存します
    (待機画面より前なので import を増やさない)。
    """
    body = json.dumps({"force": bool(force)}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Tool-Token"] = token
    try:
        with local_request(
            f"http://{app_config.host()}:{port}/api/shutdown",
            timeout=5, data=body, method="POST", headers=headers,
        ) as res:
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


# ------------------------------------------------------------------
# デスクトップ版(日報複合ツールの窓)との取り合い
# ------------------------------------------------------------------
#: デスクトップ版として動いている Python が握る錠(``start_app.start_bridge``)。
#: 中身は無い。**OS のファイルロックを握っているか**だけを見る(プロセスが終われば外れる)。
#: 日報複合ツールの窓は看板の Python を子として起こし、その Python がこれを握る
DESKTOP_LOCK_NAME = "desktop.lock"

# 握っている錠(プロセスが終わるまで持っておく)
_desktop_lock = None


def desktop_lock_path() -> Path:
    return app_config.local_root() / "runtime" / DESKTOP_LOCK_NAME


def _try_lock(fh) -> bool:
    """開いたファイルの錠を取る(取れなければ ``False``)。待たない。"""
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fh) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def hold_desktop_lock(wait_sec: float = 3.0) -> bool:
    """デスクトップ版として動くあいだ、錠を握る。取れたら(握っていたら)``True``。

    モードを切り替えたときは、前の Python が終わってから次が起きる。終わり際の
    わずかな重なりで断らないよう、``wait_sec`` 秒までは取り直す。
    """
    global _desktop_lock
    if _desktop_lock is not None:
        return True
    path = desktop_lock_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "a+b")
    except OSError as exc:  # noqa: BLE001
        # 錠そのものが作れない(めったに無い)。止めずに進む ── 守りは
        # 1枚減るが、看板が使えないよりはよい
        log.warning("デスクトップ版の錠を作れませんでした: %s", exc)
        return True
    deadline = time.monotonic() + max(0.0, wait_sec)
    while not _try_lock(fh):
        if time.monotonic() >= deadline:
            fh.close()
            return False
        time.sleep(0.2)
    _desktop_lock = fh
    log.info("デスクトップ版の錠を握りました: %s", path)
    return True


def release_desktop_lock() -> None:
    """握っている錠を放す(試験用。ふだんはプロセスが終われば外れる)。"""
    global _desktop_lock
    fh, _desktop_lock = _desktop_lock, None
    if fh is not None:
        _unlock(fh)
        fh.close()


def desktop_running() -> bool:
    """デスクトップ版(日報複合ツールの窓の「看板」)がこの利用者のこの PC で動いているか。

    錠を**取れるか試してすぐ放す**。取れなければ動いている。
    ブラウザ版とデスクトップ版が同時に動くと、同じ手元の SQLite と共有DBへ
    2 つのプロセスが書き戻しに行く(押した操作が二重に届く)。
    """
    if _desktop_lock is not None:
        return True
    path = desktop_lock_path()
    if not path.is_file():
        return False
    try:
        with open(path, "a+b") as fh:
            if not _try_lock(fh):
                return True
            _unlock(fh)
    except OSError as exc:
        log.info("デスクトップ版の錠を確かめられませんでした: %s", exc)
    return False


def browser_running() -> str:
    """ブラウザ版がこの PC で動いていれば、その説明(居なければ空文字)。

    **後から開いたほうが止まる**(デスクトップ版は、動いているブラウザ版を止めない)。
    印(``<mode>.lock``)が無ければポートは叩かない。
    """
    try:
        live = _live_instances()
    except Exception:  # noqa: BLE001 - 確かめられないなら居ないものとして進む
        log.exception("動いているブラウザ版を確かめられませんでした")
        return ""
    for other in live:
        if other.info.pid == os.getpid():
            continue
        return f"{config.mode_display_name(other.mode)} pid={other.info.pid}"
    return ""


# ------------------------------------------------------------------
# ポート
# ------------------------------------------------------------------
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


def pick_port(mode: str) -> int | None:
    """使えるポートを1つ選ぶ。全部塞がっていれば ``None``。

    候補が mode どうしで重ならないことは ``app_config.port_range_conflicts()``
    が保証する(現場が繰り上がって倉庫のポートを奪わないようにするため)。
    """
    for port in app_config.port_candidates(mode):
        if is_port_free(port):
            return port
        log.info("ポート %s は使用中です", port)
    return None


# ------------------------------------------------------------------
# 起動時の記録
# ------------------------------------------------------------------
def build_lock_info(mode: str, port: int, token: str = "") -> LockInfo:
    return LockInfo(
        app_id=app_config.app_id(),
        mode=mode,
        pid=os.getpid(),
        port=port,
        url=f"http://{app_config.host()}:{port}/",
        started_at=time.time(),
        python=sys.executable,
        app_root=str(app_config.APP_ROOT),
        token=token,
        process_started=my_started(),
    )
