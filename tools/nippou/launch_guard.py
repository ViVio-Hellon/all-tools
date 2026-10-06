"""多重起動の防止とインスタンス管理 (基盤仕様書 2.4)

同じアプリが二重に起動すると、ポート競合・二重処理・設定ファイル競合が
起きる。そこで起動ごとに**ロックファイル**(PID・ポート・起動時刻)を
書き、次の起動はそれを見て判断する。

    生きている同じアプリが居る → 新しく起動せず、ブラウザだけ開く
    死んだロックが残っている   → 消して続行する(記録は残す)

「生きているか」の判定は2段構えにする:

    1. PIDのプロセスが存在するか
    2. そのポートの `/api/health` が**同じ app_id** を返すか

1だけでは足りない。PIDは使い回されるので、無関係なプロセスが同じ番号を
持っていることがある。2だけでも足りない。別のアプリがHTTPを返している
場合があるので、`app_id` の照合が要る(基盤仕様書 2.3)。

このモジュールは Flask に依存しない。起動の判断だけを持つので、
サーバが立たない状況でも動く。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from nippou import app_config
from nippou.logging_setup import get_launch_logger

log = get_launch_logger("guard", filename="guard.log")

# `/api/health` を叩くときの待ち時間(秒)。ローカルなので短くてよい。
# 長いと、死んだロックの掃除に毎回この分だけ待たされる
HEALTH_TIMEOUT_SEC = 1.5

# 古い版が動いていたとき、終わるのを待つ上限(秒)
REPLACE_WAIT_SEC = 10.0

# 先に印を立ててから、待ち受けが始まるまでの猶予(秒)
#
# 【この猶予が無いと、二重に立ちます】
# 印(ロック)は**ポートを取る前**に立てます。そうしないと、2回続けて
# 起動した2つが「どちらもロックが無い」を見て、両方が立ってしまう。
# ただしその瞬間から数十秒は、まだ `/api/health` が答えません ──
# そこで「応答しない = 死んでいる」と決めつけると、後から来たほうが
# ロックを消して**結局2つ立つ**。立ったばかりの印は、答えなくても
# 「起動中」として扱います。
#
# 45秒は、この端末で実測した起動時間(共有が遠いと十数秒)に
# 余裕を足した値です。
STARTUP_GRACE_SEC = 45.0

# 起動中のものを待つとき、様子を見る間隔(秒)
POLL_INTERVAL_SEC = 0.3

# ロックファイルの名前。日報ツールは待ち受けるポートが1つなので1本だけ
# (参照実装はモードごとに分けていた)
LOCK_NAME = "nippou.lock"


@dataclass
class LockInfo:
    """`runtime/nippou.lock` の中身。"""

    app_id: str
    pid: int
    port: int
    url: str
    started_at: float
    version: str = ""
    python: str = ""
    app_root: str = ""
    # 起動トークン。`stop.bat` が「正常終了を要求する」ために要る
    # (基盤仕様書 2.8 は、PIDで落とす前にまずアプリ自身へ頼むことを
    # 求めている)。ロックは利用者ごとのローカル領域にあり、読めるのは
    # 同じ利用者だけ。そこまで入れる相手はプロセスを直接落とせるので、
    # ここに置いても守りの強さは変わらない。逆に、悪意あるWebページは
    # ローカルのファイルを読めないため、トークンによる防御(別オリジン
    # からの操作を弾く)はそのまま効く。
    token: str = ""

    @property
    def started_text(self) -> str:
        return time.strftime("%Y/%m/%d %H:%M:%S", time.localtime(self.started_at))


def lock_path() -> Path:
    return app_config.local_dir("runtime") / LOCK_NAME


# ------------------------------------------------------------------
# ロックファイル
# ------------------------------------------------------------------
def write_lock(info: LockInfo) -> Path:
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(info), ensure_ascii=False, indent=2),
                    encoding="utf-8")
    # トークンを含むので、本人だけが読める権限にする。Windows には
    # chmod が効かないが、`%LOCALAPPDATA%` 自体が利用者ごとのフォルダな
    # ので実質同じ扱いになる
    try:
        os.chmod(path, 0o600)
    except OSError as exc:                        # noqa: BLE001 - best effort
        log.debug("ロックの権限を変更できませんでした: %s", exc)
    log.info("ロックを書きました: %s (pid=%s port=%s)", path, info.pid, info.port)
    return path


def claim_lock(info: LockInfo) -> bool:
    r"""**まだ誰も立てていなければ**印を立てる。立てられたら ``True``。

    【なぜ `write_lock` と分けるのか ── 実際に2つ立ちました】
    以前は「調べる → ポートを取る → 待ち受けを始める → 印を立てる」の
    順でした。調べてから印を立てるまでに**1秒以上**空きます。その間に
    もう一度起動すると(バッチを2回押す、ショートカットを連打する)、
    2つとも「印が無い」を見て、2つとも先へ進みます。

    しかも `pick_port()` は塞がっているポートを避けて**隣の番号へずれる**
    ので、ぶつかって落ちることすらありません ── 8733 と 8734 で静かに
    2つ動き、印はあとから書いたほうだけが残ります。片方で打った日報は
    もう片方の画面に出ません。

    そこで、**ポートを取る前に**この関数で印を立てます。作るのと
    「無いことを確かめる」のを `O_CREAT | O_EXCL` で1回の操作にまとめる
    ので、2つが同時に来ても立てられるのは1つだけです(この判断を
    Python の側で2手に分けると、そこがまた隙間になります)。
    """
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(asdict(info), ensure_ascii=False, indent=2)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        log.info("印はすでに立っています: %s", path)
        return False
    except OSError as exc:                        # noqa: BLE001
        # 印を立てられないことを理由に起動そのものを止めない。
        # **二重起動より、一度も起動できないことのほうが困ります**
        log.warning("印を立てられませんでした (%s): %s。続行します", path, exc)
        return True
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
    except OSError as exc:                        # noqa: BLE001
        log.warning("印の中身を書けませんでした: %s", exc)
    log.info("印を立てました: %s (pid=%s)", path, info.pid)
    return True


def update_lock(**changes) -> Optional[LockInfo]:
    """自分が立てた印に、あとから決まったこと(ポート・トークン)を書く。

    ポートは印を立てたあとに決まります(`claim_lock` の説明)。決まった
    ところで書き足さないと、`stop.bat` も次の起動の判定も、どこへ声を
    掛ければよいか分かりません。
    """
    info = read_lock()
    if info is None:
        return None
    if info.pid != os.getpid():
        # **自分の印でなければ触らない。** 上書きすると、動いている
        # ほうの居場所が消えます
        log.warning("自分の印ではないので書き換えません (pid=%s)", info.pid)
        return info
    for key, value in changes.items():
        setattr(info, key, value)
    write_lock(info)
    return info


def read_lock() -> Optional[LockInfo]:
    """壊れていれば `None`。読めないことを理由に起動を止めない。"""
    path = lock_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return LockInfo(**{k: raw[k] for k in LockInfo.__dataclass_fields__ if k in raw})
    except FileNotFoundError:
        return None
    except Exception as exc:                      # noqa: BLE001 - 壊れたロックは無視
        log.warning("ロックを読めませんでした (%s): %s", path, exc)
        return None


def remove_lock() -> None:
    try:
        lock_path().unlink()
        log.info("ロックを消しました")
    except FileNotFoundError:
        pass
    except OSError as exc:                        # noqa: BLE001
        log.warning("ロックを消せませんでした: %s", exc)


def release_lock() -> None:
    """**自分が立てた印だけ**を片付ける。

    終わるときに無条件で消すと、入れ替えのときに困ります ── 古いほうが
    終わる頃には新しいほうがもう印を立てているので、古いほうの後始末が
    新しいほうの印を消してしまい、次の起動で「誰も居ない」と判定されて
    2つ目が立ちます。
    """
    info = read_lock()
    if info is None:
        return
    if info.pid != os.getpid():
        log.info("自分の印ではないので残します (pid=%s)", info.pid)
        return
    remove_lock()


# ------------------------------------------------------------------
# プロセスの生死
# ------------------------------------------------------------------
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
    """Windows では `tasklist` で確認する。

    `OpenProcess` を ctypes で叩く手もあるが、権限やハンドルの後始末を
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
    コマンドラインとアプリの場所を照合する。取れなければ空文字
    (呼び出し側は安全側に倒して落とさない)。
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


# ------------------------------------------------------------------
# 起動確認API
# ------------------------------------------------------------------
# 自分自身(127.0.0.1)への通信に**プロキシを通さない**ための送信口。
#
# `urllib.request.urlopen()` の既定は、環境変数 `HTTP_PROXY` や
# Windowsのインターネット設定からプロキシを拾う。社内PCではたいてい
# プロキシが入っており、除外一覧に `127.0.0.1` が無いと、**自分自身への
# 通信までプロキシへ送られて失敗する**。ループバックにプロキシを挟む
# 理由は無いので、ここで明示的に外す。
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def local_request(url: str, *, timeout: float, data: Optional[bytes] = None,
                  method: Optional[str] = None,
                  headers: Optional[dict] = None):
    """127.0.0.1 への要求。プロキシを経由しない。

    このアプリが外に出す通信はこれだけなので、送信口を1つに集約しておく。
    """
    request = urllib.request.Request(url, data=data, method=method,
                                     headers=headers or {})
    return _LOCAL_OPENER.open(request, timeout=timeout)


def probe_health(port: int, *, timeout: float = HEALTH_TIMEOUT_SEC) -> Optional[dict]:
    """`GET /api/health` を叩く。応答しなければ `None`。"""
    url = f"http://127.0.0.1:{port}/api/health"
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
        return sock.connect_ex(("127.0.0.1", port)) == 0


def proxy_settings() -> dict[str, str]:
    """いま効いているプロキシ設定。原因調査のためだけに使う。"""
    try:
        return {k: v for k, v in urllib.request.getproxies().items()
                if k in ("http", "https")}
    except Exception:                    # noqa: BLE001 - 調査用なので握る
        return {}


def is_our_app(health: Optional[dict]) -> bool:
    """その応答が**自分と同じアプリ**か。

    `app_id` を見ないと、たまたま同じポートを使っている別のアプリを
    自分だと誤認する(基盤仕様書 2.3)。
    """
    if not health:
        return False
    return health.get("app_id") == app_config.app_id()


# ------------------------------------------------------------------
# 既存インスタンスの判定
# ------------------------------------------------------------------
@dataclass
class GuardResult:
    """起動してよいか。"""

    should_start: bool
    url: str = ""
    existing: Optional[LockInfo] = None
    reason: str = ""
    # 動いているのが**古い版**だった場合、その版。合流してはいけない
    stale_version: str = ""
    # 相手がまだ起動の途中(印は立っているが、まだ応答しない)
    starting: bool = False
    # 相手が応答しないまま居座っている。**合流もできない**
    hung: bool = False
    # **印に載っていない、もう1つ以上の自分。** ここが空でないなら
    # すでに多重起動しています(`find_running`)
    extras: list = field(default_factory=list)


@dataclass
class Running:
    """いま待ち受けている、自分と同じアプリ1つぶん。"""

    port: int
    pid: int = 0
    version: str = ""
    app_root: str = ""

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"


def find_running() -> list[Running]:
    r"""**印を当てにせず、ポートを舐めて自分を探す。**

    【印(ロック)だけを見ていたので、取り逃していました】
    印には**1つぶんしか書けません。** そして印が無い状態から起動すると:

        印を読む → 無い → 「起動してよい」
        ポートを選ぶ → 8733 は塞がっている → **8734 へずらす**
        → 2つ目が立つ

    `pick_port` は「塞がっているのは別のアプリだろう」という前提でずらし
    ますが、塞いでいるのが**自分自身**のことがあります(印が消えた・
    印を書く前に落ちた・古い版の不具合で取り逃した残り)。こうなると、
    印は片方しか指していないので、次からの起動は毎回そちらへ合流し、
    **もう片方は誰にも気づかれないまま動き続けます。**
    「合流しました」と出ているのに2つ動いている、はこれです。

    ここでは候補のポートを実際に叩いて、`app_id` が一致したものを全部
    返します ── **印よりポートのほうが正直**です。
    """
    found: list[Running] = []
    for port in app_config.port_candidates():
        health = probe_health(port, timeout=0.6)
        if not is_our_app(health):
            continue
        body = health or {}
        found.append(Running(
            port=port,
            pid=int(body.get("pid") or 0),
            version=str(body.get("version", "")),
            app_root=str(body.get("app_root", ""))))
    return found


def check_existing() -> GuardResult:
    """すでに同じアプリが動いていないか調べる。

    動いていれば `should_start=False` と、開くべきURLを返す。

    **ただし版が違えば合流しない。** 入れ替えたのに古いプロセスが
    残っていると、ここが「すでに起動しています」と答えて古いほうの
    ブラウザを開き、**新しい版がいつまでも動かない**(画面の版バッジも
    古いままになる)。版が違うときは古いほうを終わらせて立て直す。
    """
    info = read_lock()
    if info is None:
        # **印が無くても、ポートは見る。** 印だけを当てにしていたので、
        # 「印は消えているが自分は動いている」を取り逃していました
        # (`find_running`)。ここを飛ばすと `pick_port` が隣の番号へ
        # ずれて、2つ目が静かに立ちます
        return _join_whatever_is_running("ロックなし")

    if not is_process_alive(info.pid):
        log.info("死んだロックを掃除します (pid=%s は不在)", info.pid)
        remove_lock()
        return _join_whatever_is_running(
            f"ロックは残っていたがpid {info.pid} は不在")

    health = probe_health(info.port) if info.port else None
    if not is_our_app(health):
        # 【ここで「死んでいる」と決めつけない】
        # 印はポートを取る**前**に立ちます(`claim_lock`)。立った直後は
        # ポートが 0 で、`/api/health` も当然答えません。以前はそれを
        # 「別のアプリが居る」と読んで印を消し、**結局2つ立って**いました。
        health = _wait_until_answers(info)
        if not is_our_app(health):
            return _not_answering(info)

    running_version = str((health or {}).get("version", ""))
    current_version = app_config.version()
    if running_version and running_version != current_version:
        log.warning("古い版が動いています (実行中=%s / これから=%s)。"
                    "終わらせてから立て直します", running_version, current_version)
        replaced = _replace_stale(info, running_version)
        if replaced.should_start:
            return replaced

    url = info.url or f"http://127.0.0.1:{info.port}/"
    log.info("すでに起動しています: pid=%s port=%s", info.pid, info.port)
    return GuardResult(False, url=url, existing=info,
                       reason="同じアプリが起動済みです")


def _join_whatever_is_running(why: str) -> GuardResult:
    """印が無い(または死んでいる)。**それでもポートを見てから決める。**

    答えるものが1つでもあれば、それが自分自身です ── 印を立て直して
    合流します。**ここで起動してしまうと、印に載っていない2つ目が
    できます。** 「合流しました」と出ているのに2つ動いている、の正体は
    これでした。

    2つ以上答えたら、**すでに多重起動しています。** 黙って1つへ合流すると
    残りは見えないままなので、数と居場所を返して画面に出します。
    """
    running = find_running()
    if not running:
        return GuardResult(True, reason=why)

    first = running[0]
    log.warning("印はありませんが、%d個が待ち受けています: %s",
                len(running), [r.port for r in running])
    # 次からは印で見つけられるように、立て直しておく
    remove_lock()
    claim_lock(LockInfo(
        app_id=app_config.app_id(), pid=first.pid, port=first.port,
        url=first.url, started_at=time.time(), version=first.version,
        app_root=first.app_root))
    return GuardResult(
        False, url=first.url, existing=read_lock(), extras=running[1:],
        reason=f"{why}が、ポート {first.port} で動いていました")


def _wait_until_answers(info: LockInfo) -> Optional[dict]:
    """立ったばかりの印なら、答えるようになるまで少しだけ待つ。

    待つのは**猶予の残りぶんだけ**(`STARTUP_GRACE_SEC`)。相手の起動が
    終われば合流でき、押した人には「押したら画面が出た」ようにしか
    見えません。猶予を過ぎている印は待ちません ── そこまで待って
    答えないものは、待っても答えません。
    """
    left = STARTUP_GRACE_SEC - (time.time() - info.started_at)
    if left <= 0:
        return None
    log.info("pid=%s は起動中のようです。あと最大 %.0f秒 待ちます",
             info.pid, left)
    deadline = time.monotonic() + left
    while time.monotonic() < deadline:
        if not is_process_alive(info.pid):
            return None                           # 起動に失敗して消えた
        fresh = read_lock()
        if fresh is None or fresh.pid != info.pid:
            return None                           # 相手が諦めて印を片付けた
        if fresh.port:
            health = probe_health(fresh.port)
            if is_our_app(health):
                info.port = fresh.port            # 決まったポートを引き取る
                info.url = fresh.url or info.url
                info.token = fresh.token or info.token
                return health
        time.sleep(POLL_INTERVAL_SEC)
    return None


def _not_answering(info: LockInfo) -> GuardResult:
    """印は立っていて、PIDも生きているのに、応答が無い。

    【勝手に消して立て直さない】
    ここで印を消して起動すると、**応答しないほうも動いたまま**2つに
    なります。片方で打った日報はもう片方の画面に出ないので、
    「保存したのに消えた」に見えます ── 止まっていることより質が悪い。

    そのPIDが本当にこのアプリなら、**居座っていることを名指しで伝えて
    止まります**(押した人は `stop.bat` で片付けられます)。別のアプリが
    同じ番号を持っていただけなら、印を消して起動します ── PIDは
    使い回されるので、これは普通に起きます。
    """
    if looks_like_our_process(info):
        log.warning("pid=%s (port=%s) が応答しません。二重に立てないため"
                    "起動を見送ります", info.pid, info.port)
        return GuardResult(False, url=info.url, existing=info, hung=True,
                           reason="前の起動が終わっていません(応答がありません)")
    log.info("pid=%s は生きていますが、このアプリではないようです", info.pid)
    remove_lock()
    return GuardResult(True, reason="印は残っていましたが、別のプロセスでした")


def looks_like_our_process(info: LockInfo) -> bool:
    """そのPIDが本当にこのアプリか。**コマンドラインで照合する。**

    別のフォルダにある同じツールや、無関係なPythonを巻き添えにしない。
    コマンドラインが取れない環境では「このアプリだ」に倒します ──
    **分からないときに起動してしまうと二重になる**ので、分からない
    ときは見送るほうを選びます(止め方は画面に出します)。
    """
    cmdline = process_command_line(info.pid)
    if not cmdline:
        log.warning("pid=%s のコマンドラインを取得できませんでした", info.pid)
        return True
    normalized = cmdline.replace("\\", "/")
    root = (info.app_root or str(app_config.APP_ROOT)).replace("\\", "/")
    if root and root in normalized:
        return True
    return "start_app.py" in normalized


def _replace_stale(info: LockInfo, running_version: str) -> GuardResult:
    """古い版に終了を頼み、終わったら起動してよいと答える。

    頼んでも終わらない場合は**合流する**(古いほうのブラウザを開く)。
    落とせないものを落とそうとして起動そのものを失敗させると、
    利用者はどちらの画面にも辿り着けなくなる。
    """
    if not request_shutdown(info.port, info.token):
        return GuardResult(False, url=info.url, existing=info,
                           stale_version=running_version,
                           reason="古い版が動いていますが、終了を頼めませんでした")

    deadline = time.monotonic() + REPLACE_WAIT_SEC
    while time.monotonic() < deadline:
        if not is_process_alive(info.pid) or probe_health(info.port) is None:
            remove_lock()
            log.info("古い版が終了しました。新しい版で起動します")
            return GuardResult(True, stale_version=running_version,
                               reason=f"古い版 {running_version} を終了させました")
        time.sleep(0.2)

    log.warning("古い版が %.0f秒待っても終わりませんでした", REPLACE_WAIT_SEC)
    return GuardResult(False, url=info.url, existing=info,
                       stale_version=running_version,
                       reason="古い版が終了しませんでした")


def request_shutdown(port: int, token: str) -> bool:
    """`POST /api/shutdown` で正常終了を頼む(基盤仕様書 2.8)。

    **落とす前にまず頼む。** 実行中の処理があると 409 が返るので、
    そのときは「頼めなかった」として False を返し、呼び出し側に
    判断を委ねる。
    """
    url = f"http://127.0.0.1:{port}/api/shutdown"
    body = json.dumps({}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Tool-Token"] = token
    try:
        with local_request(url, timeout=HEALTH_TIMEOUT_SEC, data=body,
                           method="POST", headers=headers) as res:
            res.read()
        return True
    except urllib.error.HTTPError as exc:
        log.info("停止を頼みましたが断られました: HTTP %s", exc.code)
        return False
    except (urllib.error.URLError, OSError) as exc:
        log.info("停止を頼めませんでした: %s", exc)
        return False


# ------------------------------------------------------------------
# デスクトップ版(統合ツールの窓)との取り合い
# ------------------------------------------------------------------
# ブラウザ版とデスクトップ版は**同時に動かさない**(同じ手元の SQLite・設定・
# タブの取り合いの状態を、2つのプロセスが別々に持ってしまう)。決まりは
# **後から開いたほうが止まる**(統合ツールの docs/統合_事前確認.md)。
#
# デスクトップ版の Python(`bridge.py`)は、動いているあいだ
# `runtime/desktop.lock` を OS のファイルロックで握る。ブラウザ版はそれを
# 取れるか試して、取れなければ止まる。錠を握るのも確かめるのも**同じ Python**
# なので、Microsoft Store 版の Python がファイルの置き場所を振り替えても、
# 両方が同じ場所を見る(外枠の Rust が握ると、振り替えのせいで見えない)。
# 錠はプロセスが終われば OS が外すので、落ちても残らない。
DESKTOP_LOCK_NAME = "desktop.lock"

# 握っている錠(プロセスが終わるまで持っておく)
_desktop_lock = None


def desktop_lock_path() -> Path:
    return app_config.local_dir("runtime") / DESKTOP_LOCK_NAME


def _try_lock(fh) -> bool:
    """開いたファイルの錠を取る(取れなければ `False`)。待たない。"""
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
    """デスクトップ版として動くあいだ、錠を握る。取れたら(握っていたら)`True`。

    立て直し(前の Python が終わってから次が起きる)の終わり際の重なりで
    断らないよう、`wait_sec` 秒までは取り直す。
    """
    global _desktop_lock
    if _desktop_lock is not None:
        return True
    path = desktop_lock_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "a+b")
    except OSError as exc:                        # noqa: BLE001
        # 錠そのものが作れない(めったに無い)。止めずに進む ── 守りは
        # 弱まるが、日報が打てないよりはよい
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
    """デスクトップ版がこの利用者のこの PC で動いているか。

    錠を**取れるか試してすぐ放す**。取れなければ動いている。
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


def browser_running() -> Optional[LockInfo]:
    """ブラウザ版がこの利用者のこの PC で動いていれば、その印。

    印(`nippou.lock`)があり、その PID が生きていて、ポートがこのアプリと
    答える(または起動の途中・応答が無いがコマンドラインがこのアプリ)とき。
    **印が無ければポートは叩かない**(ソケットを開かない)。
    """
    info = read_lock()
    if info is None or info.pid == os.getpid():
        return None
    if not is_process_alive(info.pid):
        return None
    if info.port and info.port > 0:
        health = probe_health(info.port)
        if health is not None:
            return info if is_our_app(health) else None
    return info if looks_like_our_process(info) else None


# ------------------------------------------------------------------
# ポート選び
# ------------------------------------------------------------------
def is_port_free(port: int, host: str = "") -> bool:
    """そのポートで待ち受けられるか。

    実際に bind して確かめる。「繋がらない=空き」とは限らない
    (待ち受けていないだけで、別の理由で bind できないことがある)。
    """
    host = host or app_config.host()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def pick_port() -> Optional[int]:
    """使えるポートを1つ選ぶ。全部塞がっていれば `None`。

    塞がっているときは候補をずらします(基盤仕様書 2.4)── ただし
    **塞いでいるのが自分自身なら、ずらしてはいけません。**

    【ここが「合流したのに2つ動く」のもう半分でした】
    以前のここは「多重起動は `check_existing()` が弾いているのだから、
    塞いでいるのは別のアプリだろう」という前提でした。その前提が崩れる
    ことがあります ── 印が消えていた・印を書く前に落ちた・古い版の
    不具合で取り逃した残りがある。そのとき 8733 を塞いでいるのは自分で、
    ここが 8734 へずらすと**2つ目が立ちます。** しかも印は1つしか
    指せないので、もう片方は誰にも気づかれません。

    ずらす前に叩いて、`app_id` が自分なら**ずらさずに諦めます**
    (呼び出し元が合流へ回します)。
    """
    for candidate in app_config.port_candidates():
        if is_port_free(candidate):
            return candidate
        if is_our_app(probe_health(candidate, timeout=0.6)):
            # **自分の上に積まない。** ここでずらすと多重起動になる
            log.warning("ポート %s は自分自身が使っています。"
                        "ずらさずに合流へ回します", candidate)
            return None
        log.info("ポート %s は別のアプリが使用中です", candidate)
    return None


# ------------------------------------------------------------------
# ロックの組み立て
# ------------------------------------------------------------------
def build_lock_info(port: int, token: str = "") -> LockInfo:
    import sys

    return LockInfo(
        app_id=app_config.app_id(),
        pid=os.getpid(),
        port=port,
        url=f"http://{app_config.host()}:{port}/",
        started_at=time.time(),
        version=app_config.version(),
        python=sys.executable,
        app_root=str(app_config.APP_ROOT),
        token=token,
    )
