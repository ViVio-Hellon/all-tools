"""取り込み元との同期を、プロセスに1つの背景処理として回す

【tkinter 版から何が変わったか】
tkinter 版は画面(``CalendarWindow``)が ``after()`` のタイマーを持ち、
同期スレッドの結果をキューで受け取っていた。**画面が同期の主体だった**ので、
窓を閉じれば同期も止まった。

Web 版に窓は無く、画面はいつでも開き直される。同期を画面に持たせると
「タブを開いている間だけ送られる」ことになり、閉じたとたんに送信待ちが
溜まる。そこで同期は**サーバ側のプロセスに1つ**だけ置き、画面は
``GET /api/sync`` でその状態を見るだけにした(設計上の規則:
同じ事実を2か所に持たない)。

【何を保証するか】
* 同時に2つ走らせない(``_running``)。取り込み元のロック競合を自分で
  作らないため
* 入力の直後に送る(``request(receive=False)``)。取り込みまでやると
  待たされるので、送りだけ
* 一定間隔で取り込む(``settings.sync_interval()``、既定20秒)
* 送れなくても例外を投げない。状態として持ち、画面が読む
  (入力は送信待ちに残るので失われない)

【接続について】
``sqlite3`` の接続はスレッドをまたげない。``AutoSync`` は自分で開き直すので、
ここでは接続を持ち回さない。
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from . import config, db, settings, sources
from . import logging_utils
from .logging_utils import get_logger
from .sync.autosync import AutoSync, SyncState, SyncStatus

log = get_logger("sync_service")

# 同期が走っているあいだ、``/api/shutdown`` と ``idle_exit`` に見せる名前。
# 「実行中の処理があります」の一覧に出る
BUSY_LABEL = "取り込み元との同期"


class SyncService:
    """送信と取り込みを回す。**プロセスに1つ**。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False
        self._status = SyncStatus()
        self._auto: Optional[AutoSync] = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_error = ""
        self._path = ""
        self._folder = ""
        self.reload()

    # ------------------------------------------------------------------
    # 設定
    # ------------------------------------------------------------------
    def reload(self) -> None:
        """設定を読み直して ``AutoSync`` を作り直す。

        設定画面から**参照パス**が変わったときに呼ぶ。送信先のファイルは
        フォルダの中から探す(``sources.find_data_db``)ので、上流が
        ファイル名を変えても追随する。
        """
        self._find()
        self._wake.set()

    def _find(self) -> bool:
        """参照パスのフォルダから取り込み元を探す。見つかれば ``True``。

        **起動したときに共有が見えなかっただけ**なら、あとで見えた時点で
        ここが見つけ直す(定期実行と「今すぐ同期」が呼ぶ)。以前は起動時の
        1回きりで、共有が落ちていた日はそのあいだずっと「参照パス未設定」
        扱いになり、登録まで断っていた。
        """
        found = sources.find_data_db()
        path = str(found) if found else ""
        # 班員名簿はマスタDBにある。受信のたびに一緒に入れ直す
        master = sources.find_master_db() if path else None
        folder = config.data_db_dir_text() or settings.access_data_path()
        with self._lock:
            changed = path != self._path or folder != self._folder
            self._path = path
            self._folder = folder
            self._auto = (AutoSync(config.sqlite_path(), path,
                                   master_path=str(master) if master else "")
                          if path else None)
        if changed or path:
            log.info("同期の設定を読み直しました: %s",
                     path or f"(見つかりません。参照パス: {folder or '未設定'})")
        return bool(path)

    def _ensure(self) -> bool:
        """取り込み元が決まっているか。参照パスがあって見失っているなら探し直す。"""
        if self._auto is not None:
            return True
        return bool(self._folder) and self._find()

    @property
    def path(self) -> str:
        """いま送信先にしている取り込み元。見つかっていなければ空。"""
        return self._path

    @property
    def enabled(self) -> bool:
        """自動同期が使える状態か(共有ファイルが決まっていて、送る手段がある)。"""
        auto = self._auto
        if auto is None:
            # 参照パスはあるが、いま共有が見えない。見えれば送る(見つけ直す)
            return bool(self._folder) and settings.auto_sync_enabled()
        return bool(auto.enabled and settings.auto_sync_enabled())

    @property
    def configured(self) -> bool:
        """共有の場所(参照パス)が決まっているか。

        **共有がいま見えなくても、参照パスがあれば決まっている。** 登録は
        送信待ちに残り、見えた時点で送る。断るのは場所が無いときだけ。
        """
        return self._auto is not None or bool(self._folder)

    @property
    def reachable(self) -> bool:
        """取り込み元のファイルが見つかっているか(見えているか)。"""
        return self._auto is not None

    def is_busy(self) -> bool:
        """いま同期中か。``/api/shutdown`` と ``idle_exit`` が使う。"""
        return self._running

    def busy_labels(self) -> list[str]:
        """実行中の処理の名前。止める前の確認に出す(基盤仕様書 2.8)。"""
        return [BUSY_LABEL] if self._running else []

    # ------------------------------------------------------------------
    # 状態
    # ------------------------------------------------------------------
    def status(self) -> SyncStatus:
        """いまの同期状態。**通信はしない。**

        送信待ちの件数だけは毎回数え直す。前回の同期のあとに入力が
        あった場合、状態だけを持ち回すと古い件数を出してしまう。
        """
        status = self._status
        status.pending = self._pending_count()
        if not self.configured:
            status.state = SyncState.DISABLED
        elif self._auto is None:
            status.state = SyncState.OFFLINE
            status.message = f"共有フォルダに取り込み元が見えません: {self._folder}"
        elif status.state is not SyncState.OFFLINE:
            status.state = SyncState.PENDING if status.pending else SyncState.SYNCED
        return status

    @staticmethod
    def _pending_count() -> int:
        from .sync import total_pending_count

        try:
            conn = db.connect()
            try:
                return total_pending_count(conn)
            finally:
                conn.close()
        except Exception as exc:                  # noqa: BLE001 - 表示のためだけ
            log.warning("送信待ちの件数を数えられませんでした: %s", exc)
            return 0

    # ------------------------------------------------------------------
    # 実行
    # ------------------------------------------------------------------
    def request(self, *, receive: bool = True) -> bool:
        """同期を1回頼む。すでに走っていれば何もしない。

        戻り値は「始めたか」。呼び出し元(画面)は待たない ── 状態は
        ``GET /api/sync`` で見に来る。
        """
        if not self._ensure():
            return False
        with self._lock:
            if self._running:
                return False
            self._running = True

        threading.Thread(target=self._run_once, args=(receive,),
                         name="sync-once", daemon=True).start()
        return True

    def sync_now(self, *, receive: bool = True) -> SyncStatus:
        """その場で同期して、終わってから状態を返す(「今すぐ同期」用)。

        画面が押した直後に結果を見せたいときだけ使う。定期実行は
        ``request()`` のほうを通す。
        """
        if not self._ensure():
            return self.status()
        with self._lock:
            if self._running:
                return self.status()
            self._running = True
        self._run_once(receive)
        return self.status()

    def _run_once(self, receive: bool) -> None:
        auto = self._auto
        # 同期1回ごとに追跡番号を立てる(``s=XXXXXX``)。その1回の送信・
        # 取り込みの行が揃う。画面の「今すぐ同期」から呼ばれたときは
        # 要求の番号(``r=``)のままにする ── 押した操作とつながるように
        token = (logging_utils.set_trace("s=" + logging_utils.new_id())
                 if logging_utils.current_trace() == "-" else None)
        before = self._status
        try:
            if auto is None:
                return
            status = auto.sync_once(receive=receive)
            self._status = status
            self._last_error = status.message if status.state is SyncState.OFFLINE else ""
        except Exception as exc:                  # noqa: BLE001 - 画面を止めない
            log.exception("同期中に想定外のエラー")
            self._status = SyncStatus(state=SyncState.OFFLINE, message=str(exc))
            self._last_error = str(exc)
        finally:
            with self._lock:
                self._running = False
            _note_change(before, self._status)
            if token is not None:
                logging_utils.reset_trace(token)

    # ------------------------------------------------------------------
    # 定期実行
    # ------------------------------------------------------------------
    def start(self) -> None:
        """定期実行を始める。2度呼んでも1つ。"""
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="sync-loop",
                                        daemon=True)
        self._thread.start()
        log.info("定期同期を始めました(%s秒間隔)", settings.sync_interval())

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:
        # 起動直後に1回。**他のラインの登録を先に見せる**ため、
        # 待たずに取り込む
        self.request(receive=True)
        while not self._stop.is_set():
            interval = max(5, settings.sync_interval())
            # ``_wake`` は設定が変わったときに立つ。間隔の途中でも起き直して、
            # 変えた設定がすぐ効くようにする
            self._wake.wait(timeout=interval)
            self._wake.clear()
            if self._stop.is_set():
                return
            if not settings.auto_sync_enabled():
                continue
            self.request(receive=True)


_offline_since: Optional[float] = None


def _note_change(before: SyncStatus, after: SyncStatus) -> None:
    """**同期の状態が変わったときだけ**残す。

    送れない理由は同期のたびに DEBUG で出ているが、20秒ごとに同じ行が
    並ぶだけで、「いつから送れなくなったか」「いつ戻ったか」が読み取りにくい。
    後追いで最初に知りたいのはその2つなので、変わり目を INFO/WARNING で残す。
    """
    global _offline_since
    if after.state is before.state:
        return
    if after.state is SyncState.OFFLINE:
        _offline_since = time.monotonic()
        log.warning("同期の状態: %s → %s(%s)", before.state.value,
                    after.state.value, after.message or "理由なし")
        return
    if before.state is SyncState.OFFLINE and _offline_since is not None:
        lasted = time.monotonic() - _offline_since
        _offline_since = None
        log.info("同期の状態: %s → %s(%.0f秒ぶりに戻りました。送信待ち %s件)",
                 before.state.value, after.state.value, lasted, after.pending)
        return
    log.info("同期の状態: %s → %s(送信待ち %s件)", before.state.value,
             after.state.value, after.pending)


# ---------------------------------------------------------------------------
# プロセスに1つ
# ---------------------------------------------------------------------------
_service: Optional[SyncService] = None
_service_lock = threading.Lock()


def get_service() -> SyncService:
    global _service
    with _service_lock:
        if _service is None:
            _service = SyncService()
        return _service


#: 未送信を残したまま終わったことを覚えておく鍵(手元の ``_sync_meta``)。
#: **取り込み元には書けない** ── 書きたい場面は共有に届かない場面なので、
#: 手元に置いて、次につながったときに端末一覧へ載せる(``terminals``)
META_UNSENT_AT_EXIT = "unsent_at_exit"
META_UNSENT_EXIT_AT = "unsent_exit_at"


def unsent_count() -> int:
    """まだ取り込み元へ出せていない件数。``idle_exit`` が粘るかを決める。

    **送り先が決まっていなければ 0 を返す。** 待っても送れないので、
    粘る理由が無い(参照パスが未設定の端末を居座らせない)。
    """
    service = get_service()
    if not service.configured:
        return 0
    return service.status().pending


def request_send() -> None:
    """送信だけ1回頼む(``idle_exit`` が粘りはじめに呼ぶ)。

    定期実行を待たずに試させる ── **目覚めた直後**はここが効く。
    """
    get_service().request(receive=False)


def remember_exit(pending: int) -> None:
    """終わるときの未送信件数を手元に覚える。

    残して終わったことがどこにも残らないと、翌朝「あの連絡が来ていない」
    まで誰も気づけない。0 のときは**消す** ── 前回の記録が居座ると、
    直ったのに出続ける。
    """
    try:
        conn = db.connect()
        try:
            db.set_meta(conn, META_UNSENT_AT_EXIT, str(pending) if pending else "")
            db.set_meta(conn, META_UNSENT_EXIT_AT,
                        db.now_string() if pending else "")
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:                      # noqa: BLE001 - 覚え書きで止めない
        log.warning("終了時の未送信件数を覚えられませんでした: %s", exc)


#: 終わる前に送り切るのを待つ上限(秒)。共有に届かない日に、窓を閉じても
#: いつまでも終わらない、にしない(残ったものは次の起動で送る)
EXIT_SEND_SEC = 8.0


def send_before_exit(timeout: float = EXIT_SEND_SEC) -> int:
    """**終わる前に、未送信を送り切る。** 残った件数を返して覚える。

    ブラウザ版はタブが閉じても見張り(``idle_exit``)が粘って送り切る。
    デスクトップ版は窓を閉じたら終わるので、その粘りをここで受け持つ
    ── 閉じた端末の入力が、翌朝まで他のラインに届かない、を起こさない。
    送れなかった件数は ``remember_exit`` で覚え、端末一覧に出す(§8.4)。
    """
    deadline = time.monotonic() + max(0.0, timeout)
    remaining = unsent_count()
    if remaining:
        log.info("終わる前に未送信 %s 件を送ります", remaining)
    while remaining and time.monotonic() < deadline:
        service = get_service()
        if not service.is_busy():
            service.sync_now(receive=False)
        else:
            time.sleep(0.2)
        before, remaining = remaining, unsent_count()
        if remaining and remaining >= before and not service.is_busy():
            break                       # 送っても減らない(届かない)。待っても同じ
    if remaining:
        log.warning("未送信 %s 件を残して終わります(次の起動で送ります)", remaining)
    remember_exit(remaining)
    return remaining


def last_unsent_exit() -> tuple[int, str]:
    """前回、未送信を残したまま終わったか。``(件数, 日時)``。無ければ ``(0, "")``。"""
    try:
        conn = db.connect()
        try:
            raw = db.get_meta(conn, META_UNSENT_AT_EXIT, "")
            when = db.get_meta(conn, META_UNSENT_EXIT_AT, "")
        finally:
            conn.close()
    except Exception as exc:                      # noqa: BLE001 - 表示のためだけ
        log.warning("前回の終了時の記録を読めませんでした: %s", exc)
        return 0, ""
    try:
        return int(str(raw).strip() or 0), str(when)
    except ValueError:
        return 0, ""


#: ``reset()`` が走っている同期を待つ上限 (秒)
RESET_WAIT_SEC = 5.0


def reset() -> None:
    """テスト用。**走っている同期が終わるまで待ってから**捨てる。

    待たないと、送信中のスレッドが取り込み元へ書いている最中に
    試験の一時フォルダが消され、``-journal`` が取り残されて
    「Directory not empty」でこけることがある(しかも**たまにしか**
    起きないので、原因に辿り着くまでが遠い)。
    """
    global _service
    with _service_lock:
        service = _service
        _service = None
    if service is None:
        return

    # 錠は先に離す。待っているあいだ ``get_service()`` を塞がない
    service.stop()
    deadline = time.monotonic() + RESET_WAIT_SEC
    while service.is_busy() and time.monotonic() < deadline:
        time.sleep(0.01)


def wait_idle(timeout: float = 5.0) -> bool:
    """同期が終わるまで待つ(テストと「今すぐ同期」の後始末用)。"""
    service = get_service()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not service.is_busy():
            return True
        time.sleep(0.05)
    return False
